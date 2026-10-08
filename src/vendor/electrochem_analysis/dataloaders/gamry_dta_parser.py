"""
Gamry DTA File Parser

This module provides comprehensive parsing capabilities for Gamry Instruments
.DTA files used by Framework™ software and Echem Analyst™.

Based on research of the Gamry DTA format specifications, this parser handles:
- CV (Cyclic Voltammetry) data
- EIS (Electrochemical Impedance Spectroscopy) data
- CA/CC (Chronoamperometry/Chronopotentiometry) data
- CIC (Charge Injection Capacity) data with voltage transients
- Metadata extraction
- Multiple table support

Adapted from Device Characterization (CMU.49.005) gamry_dta_parser.py.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union
import pandas as pd
from dataclasses import dataclass
from datetime import datetime

from ..utils.path_utils import intelligent_path_handler


@dataclass
class DTAMetadata:
    """Container for DTA file metadata."""
    experiment_type: Optional[str] = None
    label: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    potentiostat: Optional[str] = None
    area: Optional[float] = None
    title: Optional[str] = None
    initial_potential: Optional[float] = None
    final_potential: Optional[float] = None
    scan_rate: Optional[float] = None
    raw_metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.raw_metadata is None:
            self.raw_metadata = {}


@dataclass
class DTATable:
    """Container for a DTA data table."""
    table_type: str  # CURVE, ZCURVE, etc.
    table_number: int
    headers: List[str]
    units: List[str]
    data: pd.DataFrame
    metadata: Dict[str, Any]


class GamryDTAParser:
    """
    Parser for Gamry .DTA files.

    This parser handles the complex structure of Gamry DTA files including:
    - Multiple data tables per file
    - Metadata extraction
    - Proper handling of scientific notation
    - Column name standardization
    """

    # Column name mappings for different techniques
    COLUMN_MAPPINGS = {
        'CV': {
            'T': 'Time (s)',
            'V': 'Potential (V)',  # Sometimes appears as just 'V'
            'Vf': 'Potential (V)',
            'A': 'Current (A)',  # Sometimes appears as just 'A'
            'Im': 'Current (A)',
            'Vu': 'Uncompensated Voltage (V)',
            'Sig': 'Signal Voltage (V)',
            'Ach': 'AC Voltage (V)',
            'IERange': 'Current Range',
            'Over': 'Overload Bits'
        },
        'EIS': {
            'Freq': 'Frequency_Hz',
            'Zreal': 'Z_real_Ohm',
            'Zimag': 'Z_imag_Ohm',
            'Zmod': 'Z_mod_Ohm',
            'Zphz': 'Z_phase_deg',
            'Idc': 'DC Current (A)',
            'Vdc': 'DC Voltage (V)',
            'IERange': 'Current Range'
        },
        'CA': {
            'T': 'Time (s)',
            'Vf': 'Applied Potential (V)',
            'Im': 'Current (A)',
            'Vu': 'Uncompensated Voltage (V)',
            'Sig': 'Signal Voltage (V)'
        },
        'CC': {
            'T': 'Time (s)',
            'If': 'Applied Current (A)',
            'Vm': 'Voltage (V)',
            'Vu': 'Uncompensated Voltage (V)',
            'Sig': 'Signal Voltage (V)'
        },
        'CIC': {
            'T': 'Time (s)',
            'Vf': 'Potential (V)',
            'Im': 'Current (A)',
            'Vu': 'Uncompensated Voltage (V)',
            'Sig': 'Signal Voltage (V)'
        }
    }

    def __init__(self):
        """Initialize the DTA parser."""
        self.metadata = DTAMetadata()
        self.tables = []
        self.raw_lines = []

    @intelligent_path_handler
    def parse_file(self, file_path: Union[str, Path]) -> Tuple[DTAMetadata, List[DTATable]]:
        """
        Parse a Gamry DTA file.

        Args:
            file_path: Path to the DTA file

        Returns:
            Tuple of (metadata, list of data tables)

        Raises:
            ValueError: If file is not a valid DTA file
        """
        path = Path(file_path)

        # Read file with encoding handling (latin-1 for Gamry binary data)
        # Let FileNotFoundError propagate to intelligent_path_handler decorator
        try:
            with open(path, 'r', encoding='latin-1', errors='ignore') as f:
                self.raw_lines = f.readlines()
        except (FileNotFoundError, OSError, PermissionError):
            # Let these propagate to the decorator for path conversion
            raise
        except Exception as e:
            # Only catch other exceptions (like encoding errors)
            raise ValueError(f"Could not read DTA file {path.name}: {e}")

        # Validate DTA file
        if not self._is_valid_dta_file():
            raise ValueError(f"File {path.name} is not a valid Gamry DTA file")

        # Parse metadata
        self.metadata = self._parse_metadata()

        # Parse data tables
        self.tables = self._parse_tables()

        return self.metadata, self.tables

    def _is_valid_dta_file(self) -> bool:
        """Check if file is a valid DTA file."""
        if not self.raw_lines:
            return False

        # Check for EXPLAIN line (required first line)
        first_line = self.raw_lines[0].strip()
        if not first_line.startswith('EXPLAIN'):
            return False

        # Check for common DTA file markers
        has_valid_marker = any(
            line.strip().startswith(('TAG', 'EXPERIMENT TYPE', 'CURVE', 'TABLE'))
            for line in self.raw_lines[:20]  # Check first 20 lines
        )

        return has_valid_marker

    def _parse_metadata(self) -> DTAMetadata:
        """Parse metadata from the file header."""
        metadata = DTAMetadata()
        metadata.raw_metadata = {}

        for line in self.raw_lines:
            line = line.strip()

            if not line:
                continue

            parts = line.split('\t')

            # Parse different metadata types
            if line.startswith('EXPLAIN'):
                metadata.raw_metadata['explain'] = '\t'.join(parts[1:]) if len(parts) > 1 else ''

            elif line.startswith('TAG'):
                if len(parts) >= 2:
                    tag_value = parts[1].strip()
                    # Handle TAG IMPEDANCE, TAG CYCLIC, etc.
                    metadata.experiment_type = tag_value

            elif line.startswith('DATE'):
                if len(parts) >= 3:
                    metadata.date = parts[2].strip()

            elif line.startswith('TIME'):
                if len(parts) >= 3:
                    metadata.time = parts[2].strip()

            elif line.startswith('TITLE'):
                if len(parts) >= 3:
                    metadata.title = parts[2].strip()

            elif line.startswith('PSTAT'):
                if len(parts) >= 3:
                    metadata.potentiostat = parts[2].strip()

            elif line.startswith('AREA'):
                if len(parts) >= 3:
                    try:
                        metadata.area = float(parts[2].strip())
                    except ValueError:
                        pass

            elif line.startswith('POTEN'):
                if len(parts) >= 3:
                    poten_type = parts[1]
                    try:
                        poten_value = float(parts[2])
                        if poten_type == 'INITIAL':
                            metadata.initial_potential = poten_value
                        elif poten_type == 'FINAL':
                            metadata.final_potential = poten_value
                        metadata.raw_metadata[f'poten_{poten_type.lower()}'] = poten_value
                    except (ValueError, IndexError):
                        pass

            elif line.startswith('EINIT'):
                if len(parts) >= 3:
                    try:
                        # Handle format: EINIT	QUANT	-0.8	V
                        metadata.initial_potential = float(parts[2])
                        metadata.raw_metadata['einit'] = metadata.initial_potential
                    except (ValueError, IndexError):
                        pass

            elif line.startswith('EFINAL'):
                if len(parts) >= 3:
                    try:
                        # Handle format: EFINAL	QUANT	-0.8	V
                        metadata.final_potential = float(parts[2])
                        metadata.raw_metadata['efinal'] = metadata.final_potential
                    except (ValueError, IndexError):
                        pass

            elif line.startswith('SCANRATE'):
                if len(parts) >= 3:
                    try:
                        # Handle format: SCANRATE	QUANT	0.05	V/s
                        metadata.scan_rate = float(parts[2])
                        metadata.raw_metadata['scan_rate'] = metadata.scan_rate
                    except (ValueError, IndexError):
                        if len(parts) >= 2:
                            try:
                                metadata.scan_rate = float(parts[1])
                                metadata.raw_metadata['scan_rate'] = metadata.scan_rate
                            except ValueError:
                                pass

            # Stop parsing metadata when we hit data tables
            elif line.startswith(('CURVE', 'ZCURVE')):
                break

        return metadata

    def _parse_tables(self) -> List[DTATable]:
        """Parse all data tables from the file."""
        tables = []
        i = 0

        while i < len(self.raw_lines):
            line = self.raw_lines[i].strip()
            line_upper = line.upper()

            # Look for table headers (case-insensitive)
            if line_upper.startswith(('CURVE', 'ZCURVE')):
                table = self._parse_single_table(i)
                if table:
                    tables.append(table)
                    # Skip past this table (header + columns + optional units + data)
                    header_lines = 3 if table.units else 2
                    i += len(table.data) + header_lines
                else:
                    i += 1
            else:
                i += 1

        return tables

    def _parse_single_table(self, start_idx: int) -> Optional[DTATable]:
        """Parse a single data table starting at the given line index."""
        if start_idx >= len(self.raw_lines):
            return None

        # Parse table header
        header_line = self.raw_lines[start_idx].strip()
        header_parts = header_line.split('\t')

        table_type = header_parts[0]  # CURVE, ZCURVE, etc.

        # Handle different header formats
        if len(header_parts) >= 3:
            table_number = int(header_parts[2]) if header_parts[2].isdigit() else 1
        else:
            # Simple format like just "CURVE"
            table_number = 1

        # Get column headers (next line)
        if start_idx + 1 >= len(self.raw_lines):
            return None

        headers_line = self.raw_lines[start_idx + 1].rstrip()
        # Handle tab-prefixed headers
        if headers_line.startswith('\t'):
            headers_line = headers_line[1:]  # Remove leading tab
        headers = [h.strip() for h in headers_line.split('\t')]
        # Remove empty headers at the end
        while headers and not headers[-1]:
            headers.pop()

        # Get units (line after headers, starts with #)
        units = []
        data_start_idx = start_idx + 2

        if data_start_idx < len(self.raw_lines):
            units_line = self.raw_lines[data_start_idx].rstrip()
            if units_line.startswith('#') or units_line.startswith('\t#'):
                # Handle tab-prefixed units line
                if units_line.startswith('\t#'):
                    units_line = units_line[2:]  # Remove tab and #
                else:
                    units_line = units_line[1:]  # Remove just #
                units = [u.strip() for u in units_line.split('\t')]
                # Remove empty units at the end
                while units and not units[-1]:
                    units.pop()
                data_start_idx = start_idx + 3

        # Parse data rows
        data_rows = []
        for i in range(data_start_idx, len(self.raw_lines)):
            line = self.raw_lines[i].rstrip()

            # Stop at empty line or next table
            if not line or line.lstrip().startswith(('CURVE', 'ZCURVE', 'TAG', 'EXPLAIN')):
                break

            # Parse data values
            # Handle tab-prefixed data lines
            if line.startswith('\t'):
                line = line[1:]  # Remove leading tab

            parts = [p.strip() for p in line.split('\t')]
            # Remove empty parts at the end only
            while parts and not parts[-1]:
                parts.pop()
            # Accept rows that have at least the minimum needed columns
            # (handle trailing empty columns in files)
            min_cols = min(len(headers), len(parts))
            if min_cols >= 2:  # At least 2 columns for meaningful data
                row = []
                # Pad parts with empty strings to match header count
                padded_parts = parts + [''] * (len(headers) - len(parts))

                for j, part in enumerate(padded_parts[:len(headers)]):
                    try:
                        # Handle scientific notation
                        if part and ('E' in part.upper() or '.' in part or '-' in part):
                            value = float(part)
                        elif part and part.isdigit():
                            value = int(part)
                        elif part:
                            value = part  # Keep as string
                        else:
                            value = ''  # Empty string for missing values
                    except ValueError:
                        value = part  # Keep as string
                    row.append(value)
                data_rows.append(row)

        if not data_rows:
            return None

        # Create DataFrame using actual headers, not units
        df = pd.DataFrame(data_rows, columns=headers)

        # Standardize column names based on technique
        df = self._standardize_column_names(df, table_type)

        # Create table object
        table = DTATable(
            table_type=table_type,
            table_number=table_number,
            headers=headers,
            units=units,
            data=df,
            metadata={'original_headers': headers, 'units': units}
        )

        return table

    def _standardize_column_names(self, df: pd.DataFrame, table_type: str) -> pd.DataFrame:
        """Standardize column names based on the measurement technique."""
        # Determine technique from table type and metadata
        technique = self._determine_technique(table_type)

        if technique not in self.COLUMN_MAPPINGS:
            return df  # Return unchanged if technique not recognized

        mapping = self.COLUMN_MAPPINGS[technique]

        # Create new DataFrame with standardized names
        new_df = df.copy()
        rename_dict = {}

        for old_name in df.columns:
            if old_name in mapping:
                rename_dict[old_name] = mapping[old_name]

        new_df = new_df.rename(columns=rename_dict)
        return new_df

    def _determine_technique(self, table_type: str) -> str:
        """Determine the measurement technique from table type and metadata."""
        # Use experiment type from metadata if available
        if self.metadata.experiment_type:
            exp_type = self.metadata.experiment_type.upper()
            # Map Gamry terminology to our technique names
            if exp_type == 'IMPEDANCE':
                return 'EIS'
            elif exp_type in self.COLUMN_MAPPINGS:
                return exp_type

        # Fallback to table type
        if table_type == 'ZCURVE':
            return 'EIS'
        elif table_type == 'CURVE':
            # Could be CV, CA, CC, or CIC - need more context
            # For now, default to CV
            return 'CV'

        return 'CV'  # Default

    def get_primary_data_table(self) -> Optional[DTATable]:
        """Get the primary data table (usually the first one)."""
        if not self.tables:
            return None
        return self.tables[0]

    def get_table_by_type(self, table_type: str) -> Optional[DTATable]:
        """Get the first table of the specified type."""
        for table in self.tables:
            if table.table_type == table_type:
                return table
        return None

    def to_standard_format(self, technique: str) -> pd.DataFrame:
        """
        Convert the parsed data to a standard format for the specified technique.

        Args:
            technique: Target technique ('CV', 'EIS', 'CIC', etc.)

        Returns:
            pd.DataFrame: Standardized data
        """
        table = self.get_primary_data_table()
        if not table:
            raise ValueError("No data tables found")

        # The data should already be standardized by column mapping
        return table.data


# Utility functions for working with DTA files

def is_dta_file(file_path: Union[str, Path]) -> bool:
    """
    Check if a file is a valid Gamry DTA file.

    Args:
        file_path: Path to the file

    Returns:
        bool: True if file is a valid DTA file
    """
    try:
        parser = GamryDTAParser()
        parser.parse_file(file_path)
        return True
    except Exception:
        return False


def extract_dta_metadata(file_path: Union[str, Path]) -> DTAMetadata:
    """
    Extract metadata from a DTA file without parsing all data.

    Args:
        file_path: Path to the DTA file

    Returns:
        DTAMetadata: Extracted metadata
    """
    parser = GamryDTAParser()
    metadata, _ = parser.parse_file(file_path)
    return metadata


def convert_dta_to_csv(dta_path: Union[str, Path],
                      csv_path: Union[str, Path],
                      technique: Optional[str] = None) -> None:
    """
    Convert a DTA file to CSV format.

    Args:
        dta_path: Path to input DTA file
        csv_path: Path to output CSV file
        technique: Target technique for column naming
    """
    parser = GamryDTAParser()
    metadata, tables = parser.parse_file(dta_path)

    if not tables:
        raise ValueError("No data tables found in DTA file")

    # Use primary table
    table = tables[0]

    # Save to CSV
    table.data.to_csv(csv_path, index=False)

    # Also save metadata as comments
    csv_path = Path(csv_path)
    with open(csv_path, 'r') as f:
        content = f.read()

    # Prepend metadata as comments
    metadata_lines = [
        f"# Gamry DTA File: {dta_path}",
        f"# Experiment Type: {metadata.experiment_type}",
        f"# Date: {metadata.date}",
        f"# Time: {metadata.time}",
        f"# Label: {metadata.label}",
        ""
    ]

    with open(csv_path, 'w') as f:
        f.write('\n'.join(metadata_lines))
        f.write(content)
