"""
Data Loaders for Electrochemistry

Simplified API for PalmSens and Gamry data loading.

PalmSens Workflow (.pssession files for EIS/CV/CA/CP):
    from src.dataloaders import load_psession

    # Load all scans from PalmSens session file
    scans = load_psession('data.pssession')
    # Returns: {'Scan1': DataFrame, 'Scan2': DataFrame, ...}
    # Auto-detects EIS / CV / CA / CP and standardizes column names

    # Use with batch analysis
    from src.analysis import EISAnalyzer, CVAnalyzer
    summary, figs = EISAnalyzer.batch_analyze(scans)
    summary, figs = CVAnalyzer.batch_analyze(scans, scan_rate=0.1, electrode_area=0.01)

Gamry Workflow (.DTA files for CIC):
    from src.dataloaders import load_cic

    # Load single CIC measurement
    data = load_cic('measurement.DTA')
    # Returns: DataFrame with Time (s), Potential (V), Current (A)

    # Use with analyzer
    from src.analysis.cic import CICAnalyzer
    analyzer = CICAnalyzer(data)

Channel Grouping (multi-channel PalmSens sessions):
    from src.dataloaders import load_psession, group_by_channel

    scans = load_psession('multichannel.pssession')
    grouped = group_by_channel(scans)
    # Returns: {'CV Measurement': {'Ch1': df1, 'Ch2': df2}, ...}
"""

import logging
import re
from typing import Dict

import pandas as pd

# PalmSens workflow - .pssession files for EIS and CV
from .psession_parser import (
    load_all_scans_from_psession as load_psession,
    parse_pssession_file,
    _parse_channel_list,
    _parse_channel_range,
)

# Gamry workflow - .DTA files for CIC only
from .gamry_dta_parser import GamryDTAParser, DTAMetadata, DTATable

# ---- MUX -> BLADE channel mapping (for multi-channel PalmSens sessions) ----

_BLADE_CH_PATTERN = re.compile(r'Channel\s*(\d+)', re.IGNORECASE)
_MUX_CH_PATTERN = re.compile(r'channel(\d+)', re.IGNORECASE)

logger = logging.getLogger(__name__)


def load_cic(file_path: str):
    """
    Load CIC data from Gamry DTA file.

    Args:
        file_path: Path to Gamry .DTA file (Windows or WSL format)

    Returns:
        DataFrame with columns: Time (s), Potential (V), Current (A)

    Example:
        >>> from src.dataloaders import load_cic
        >>> from src.analysis.cic import CICAnalyzer
        >>> data = load_cic('measurement.DTA')
        >>> analyzer = CICAnalyzer(data)
        >>> analyzer.calculate_cic(electrode_area=0.01)
    """
    parser = GamryDTAParser(file_path)
    tables = parser.parse()

    if not tables or len(tables) == 0:
        raise ValueError(f"No data tables found in {file_path}")

    # Use first table (standard for CIC measurements)
    return tables[0].data


def filter_scans(scans: dict, technique: str):
    """
    Filter scans by technique type (EIS, CV, CA, or CP).

    Args:
        scans: Dictionary of scan_name -> DataFrame from load_psession()
        technique: 'EIS', 'CV', 'CA', or 'CP'

    Returns:
        Filtered dictionary containing only scans of the specified technique

    Example:
        >>> scans = load_psession('mixed_data.pssession')
        >>> eis_scans = filter_scans(scans, 'EIS')
        >>> cv_scans = filter_scans(scans, 'CV')
        >>> ca_scans = filter_scans(scans, 'CA')
        >>> cp_scans = filter_scans(scans, 'CP')
    """
    filtered = {}
    technique = technique.upper()

    for name, df in scans.items():
        if not isinstance(df, pd.DataFrame):
            continue

        # Check columns to determine technique
        columns = set(df.columns)

        if technique == 'EIS':
            # EIS has impedance columns
            eis_cols = {'Frequency_Hz', 'Z_real_Ohm', 'Z_imag_Ohm'}
            if eis_cols.issubset(columns):
                filtered[name] = df

        elif technique == 'CV':
            # CV has potential and current columns
            cv_cols = {'Potential (V)', 'Current (A)'}
            if cv_cols.issubset(columns):
                filtered[name] = df

        elif technique == 'CA':
            # CA has time and current columns
            ca_cols = {'Time (s)', 'Current (A)'}
            if ca_cols.issubset(columns):
                filtered[name] = df

        elif technique == 'CP':
            # CP (galvanostatic chronopotentiometry) has time and
            # potential columns
            cp_cols = {'Time (s)', 'Potential (V)'}
            if cp_cols.issubset(columns):
                filtered[name] = df

    return filtered


def group_by_channel(
    scans: dict,
) -> Dict[str, Dict[str, pd.DataFrame]]:
    """
    Group scan dictionaries by channel.

    Takes the scan dict returned by load_psession() and organizes scans
    by their base measurement name, splitting off ` - Ch{N}` suffixes.

    Args:
        scans: Dictionary of scan_name -> DataFrame from load_psession()

    Returns:
        Nested dict: {measurement_name: {channel_name: DataFrame}}
        Scans without a channel suffix use '_ungrouped' as the key.

    Example:
        >>> scans = load_psession('multichannel.pssession')
        >>> grouped = group_by_channel(scans)
        >>> # {'CV Measurement': {'Ch1': df1, 'Ch2': df2},
        >>> #  'EIS Scan': {'_ungrouped': df3}}
    """
    pattern = re.compile(r' - Ch(\d+)$')
    grouped: Dict[str, Dict[str, pd.DataFrame]] = {}

    for name, df in scans.items():
        match = pattern.search(name)
        if match:
            base_name = name[:match.start()]
            channel = f"Ch{match.group(1)}"
        else:
            base_name = name
            channel = '_ungrouped'

        if base_name not in grouped:
            grouped[base_name] = {}
        if channel in grouped[base_name]:
            logger.warning(
                "Duplicate key ('%s', '%s') in group_by_channel; "
                "overwriting existing entry from scan '%s'",
                base_name, channel, name,
            )
        grouped[base_name][channel] = df

    return grouped


def get_mux_blade_map(
    pssession_path: str,
) -> Dict[str, Dict[int, int]]:
    """Return {measurement_title: {mux_port_int: blade_channel_int}}.

    PalmSens .pssession files from MUX8-R2 sessions encode the BLADE
    electrode number only in each curve's Title (e.g.
    ``"CA i vs t Channel 6"``), not in the measurement title or MUX
    settings. The default load_psession() labels output keys with the
    MUX port (``" - Ch1"``), which does not reveal which physical
    BLADE electrode was sampled. This helper returns the per-
    measurement translation table so downstream analysis can tag
    scans with the true BLADE channel.

    The mapping commonly varies between measurements within a single
    .pssession (Karl re-wires MUX cables between sessions), so the
    map must be queried per-measurement title.

    Args:
        pssession_path: Path to .pssession file (Windows or WSL).

    Returns:
        Nested dict: {measurement_title: {mux_port: blade_channel}}.
        Measurements with no parseable curve titles are omitted.
    """
    docs = parse_pssession_file(pssession_path)
    out: Dict[str, Dict[int, int]] = {}
    for doc in docs:
        for m in doc.get('Measurements', []):
            title = m.get('Title') or m.get('title') or ''
            mapping: Dict[int, int] = {}
            # Track whether every curve's blade channel equals its
            # mux port (i.e. PalmSens default per-curve titles like
            # "Channel 1" against y-axis "channel1"). When this is
            # true for all curves, the per-curve titles carry no
            # operator-edited wiring info and we should fall through
            # to measurement-title parsers instead.
            curve_pairs_seen = 0
            curve_pairs_default = 0
            for c in m.get('Curves', []) or m.get('curves', []):
                if not isinstance(c, dict):
                    continue
                ctitle = c.get('Title') or c.get('title') or ''
                yax = (
                    c.get('YAxisDataArray')
                    or c.get('yaxisdataarray')
                    or {}
                )
                mux_desc = (
                    yax.get('Description')
                    or yax.get('description')
                    or ''
                )
                blade_match = _BLADE_CH_PATTERN.search(ctitle)
                mux_match = _MUX_CH_PATTERN.match(mux_desc)
                if blade_match and mux_match:
                    mux_port = int(mux_match.group(1))
                    blade_ch = int(blade_match.group(1))
                    mapping[mux_port] = blade_ch
                    curve_pairs_seen += 1
                    if blade_ch == mux_port:
                        curve_pairs_default += 1

            # Priority 1: operator-edited per-curve titles win when
            # at least one curve has blade_ch != mux_port (real
            # operator labeling like E049 Ch2/4/6/8 or E054
            # Ch10/12/14/16). If every curve had blade_ch == mux_port
            # (E046-style default labeling), drop the mapping and
            # fall through.
            if mapping and curve_pairs_seen > 0 and (
                curve_pairs_default < curve_pairs_seen
            ):
                out[title] = mapping
                continue

            # Priority 2: comma-separated channel list in the
            # measurement title, e.g. "Channels 4, 5, 13, 15" or
            # "Channels 1, 3, 5, 7, 9, 11, 13, 15".
            list_map = _parse_channel_list(title)
            if list_map:
                out[title] = list_map
                continue

            # Priority 3: dash range in the measurement title,
            # e.g. "channels 18-11" or "channels 1-8".
            range_map = _parse_channel_range(title)
            if range_map:
                out[title] = range_map
                continue

            # Priority 4 (last resort): if curves were seen but
            # only default-labeled, fall back to a 1:1 mapping
            # and warn so the operator can spot ambiguous sessions.
            if curve_pairs_seen > 0:
                logger.warning(
                    "No explicit MUX->BLADE mapping found for "
                    "measurement '%s'; falling back to 1:1 "
                    "(MUX port N -> physical channel N). If the "
                    "MUX cables were rewired, encode the mapping "
                    "in the measurement title (e.g. "
                    "'Channels 4, 5, 13, 15') or in each curve's "
                    "title.",
                    title,
                )
                out[title] = {
                    port: port
                    for port in range(1, curve_pairs_seen + 1)
                }
    return out


def tag_scans_with_blade_channel(
    scans: dict,
    pssession_path: str,
) -> pd.DataFrame:
    """Return a DataFrame of (scan_name, measurement, mux_channel, blade_channel).

    Joins scan keys from load_psession() to the MUX->BLADE map
    extracted from the raw .pssession file. Useful as a lookup table
    alongside a batch analysis summary DataFrame.

    Args:
        scans: Dict from load_psession() (scan_name -> DataFrame).
        pssession_path: Path to the same .pssession file.

    Returns:
        DataFrame with columns:
            - scan_name: full key from load_psession()
            - measurement: measurement title (scan_name minus '- ChN')
            - mux_channel: int MUX port (1-based)
            - blade_channel: int BLADE electrode number (or None if
              no mapping available for this measurement).
    """
    mux_blade = get_mux_blade_map(pssession_path)
    ch_pat = re.compile(r' - Ch(\d+)$')
    rows = []
    for name in scans.keys():
        m = ch_pat.search(name)
        if m:
            measurement = name[:m.start()]
            mux_ch = int(m.group(1))
        else:
            measurement = name
            mux_ch = None
        blade_ch = mux_blade.get(measurement, {}).get(mux_ch)
        rows.append({
            'scan_name': name,
            'measurement': measurement,
            'mux_channel': mux_ch,
            'blade_channel': blade_ch,
        })
    return pd.DataFrame(rows)


__all__ = [
    # Primary API
    'load_psession',      # PalmSens .pssession → EIS/CV batch analysis
    'load_cic',           # Gamry .DTA → CIC single analysis
    'filter_scans',       # Filter mixed scans by technique
    'group_by_channel',   # Group scans by channel suffix

    # MUX/BLADE channel mapping
    'get_mux_blade_map',
    'tag_scans_with_blade_channel',

    # Advanced (if needed)
    'parse_pssession_file',
    'GamryDTAParser',
    'DTAMetadata',
    'DTATable',
]
