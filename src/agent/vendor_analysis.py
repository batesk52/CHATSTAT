"""Analysis tools over the vendored CMU.49.011 electrochemistry code.

Exposes :func:`build_analysis_tools`, which returns ``(tool_def,
handler)`` pairs ready for ``ToolRegistry.register`` (or the
``extra_tools`` argument of ``src.agent.tools.build_registry``).  The
handlers wrap the READ-ONLY vendored analyzers in
``src/vendor/electrochem_analysis`` -- they call the real public APIs
(``CVAnalyzer``, ``EISAnalyzer``, ``CAAnalyzer``, ``CPAnalyzer``,
``ECSAAnalyzer``, ``CoganCICAnalyzer``) and never reimplement the math.

Figure pipeline: each analysis handler renders matplotlib figures (Agg
backend, forced by the vendor package ``__init__``) to PNG bytes and
hands them to the optional ``figure_sink`` callable as
``{"title": str, "tool": str, "png": bytes}``.  The sink is invoked on
the AGENT thread; the GUI adapts it to a queued Qt signal (see
``src.gui.agent_dock.FigureSink``).  When ``figure_sink`` is None the
figures are skipped entirely and only the metric summaries are
returned.  Figures are ALWAYS closed (no pyplot figure leaks),
including on render errors.

Handlers are synchronous (the registry dispatch supports sync handlers)
and never raise: every failure is returned as a structured
``{"ok": false, "error": ...}`` dict.  Result JSON stays compact --
metrics only, never raw data arrays, never PNG bytes.
"""

from __future__ import annotations

# Eager native imports at module top (blueprint constraint).  The
# vendor package import comes FIRST: its __init__ forces the matplotlib
# Agg backend before any pyplot import can pick a GUI backend.
import src.vendor.electrochem_analysis  # noqa: F401  - forces Agg
import matplotlib  # noqa: F401  - eager native import
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd  # noqa: F401  - eager native import

import io
import logging
import math
import os
from typing import Any, Callable, Optional

from src.vendor.electrochem_analysis.analysis.ca import CAAnalyzer
from src.vendor.electrochem_analysis.analysis.cic import CoganCICAnalyzer
from src.vendor.electrochem_analysis.analysis.cp import CPAnalyzer
from src.vendor.electrochem_analysis.analysis.cv import (
    CVAnalyzer,
    DEFAULT_DIFFUSION_COEFF_CM2_S,
)
from src.vendor.electrochem_analysis.analysis.ecsa import ECSAAnalyzer
from src.vendor.electrochem_analysis.analysis.eis import EISAnalyzer
from src.vendor.electrochem_analysis.dataloaders import (
    filter_scans,
    load_cic,
    load_psession,
)

logger = logging.getLogger(__name__)

__all__ = ["FigureSinkCallable", "build_analysis_tools"]

#: Sink signature: receives {"title": str, "tool": str, "png": bytes}.
FigureSinkCallable = Callable[[dict[str, Any]], None]

#: Technique labels understood by the vendored filter_scans().
_TECHNIQUES = ("CV", "EIS", "CA", "CP")

#: Cap on per-tool table rows (e.g. CP plateaus) kept in the result.
_MAX_ROWS = 25

#: cm^2 -> mm^2 area conversion (1 cm^2 = 100 mm^2).
CM2_TO_MM2 = 100.0


class _AnalysisError(Exception):
    """Structured tool failure; ``extra`` merges into the error dict."""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_path(raw: Any, suffixes: tuple[str, ...] = ()) -> str:
    """Validate *raw* and return an absolute, existing file path.

    Args:
        raw: The model-supplied path (absolute or relative to the
            application working directory).
        suffixes: When non-empty, the file extension must match one of
            these (case-insensitive).

    Raises:
        _AnalysisError: For a missing/empty/non-existent path or a
            wrong extension.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise _AnalysisError(
            "A 'path' string is required (absolute, or relative to "
            "the application working directory)."
        )
    path = os.path.abspath(os.path.expanduser(raw.strip()))
    if not os.path.isfile(path):
        raise _AnalysisError(
            f"File not found: {path!r}. Provide an existing file "
            "path (absolute, or relative to the application working "
            f"directory {os.getcwd()!r})."
        )
    if suffixes and not path.lower().endswith(
        tuple(s.lower() for s in suffixes)
    ):
        raise _AnalysisError(
            f"Expected a {' / '.join(suffixes)} file, got {path!r}."
        )
    return path


def _load_scans(path: str) -> dict[str, Any]:
    """Load a .pssession into ``{scan_name: DataFrame}`` (non-empty)."""
    scans = load_psession(path)
    scans = {
        name: df
        for name, df in (scans or {}).items()
        if isinstance(df, pd.DataFrame) and not df.empty
    }
    if not scans:
        raise _AnalysisError(
            f"No data tables could be parsed from {path!r}."
        )
    return scans


def _technique_map(scans: dict[str, Any]) -> dict[str, list[str]]:
    """Map technique label -> scan names, via the vendored classifier."""
    return {
        tech: sorted(filter_scans(scans, tech))
        for tech in _TECHNIQUES
    }


def _available_text(scans: dict[str, Any]) -> dict[str, Any]:
    """Build the 'what IS in this file' part of structured errors."""
    return {
        "available_scans": sorted(scans),
        "available_techniques": {
            tech: names
            for tech, names in _technique_map(scans).items()
            if names
        },
    }


def _pick_scan(
    scans: dict[str, Any],
    technique: str,
    scan_name: Optional[str],
    prefer_keyword: Optional[str] = None,
) -> tuple[str, pd.DataFrame]:
    """Select one scan of *technique* from the session.

    Args:
        scans: Full ``{name: DataFrame}`` dict from the loader.
        technique: One of ``CV``/``EIS``/``CA``/``CP``.
        scan_name: Explicit scan to use; must exist and carry the
            columns the technique requires.
        prefer_keyword: Lowercase substring used to break ties when
            several scans match (e.g. ``"cyclic"`` so DPV data does
            not shadow the CV scan).

    Raises:
        _AnalysisError: When the name is unknown or no scan of this
            technique exists; the error lists what IS available.
    """
    candidates = filter_scans(scans, technique)
    if scan_name:
        if scan_name not in scans:
            raise _AnalysisError(
                f"Scan {scan_name!r} not found in this session. "
                "Call load_session to list the exact scan names.",
                **_available_text(scans),
            )
        if scan_name not in candidates:
            raise _AnalysisError(
                f"Scan {scan_name!r} does not carry the columns "
                f"required for {technique} analysis.",
                **_available_text(scans),
            )
        return scan_name, _clean_frame(candidates[scan_name])
    if not candidates:
        raise _AnalysisError(
            f"This session contains no {technique} data.",
            **_available_text(scans),
        )
    if prefer_keyword:
        for name in sorted(candidates):
            if prefer_keyword in name.lower():
                return name, _clean_frame(candidates[name])
    name = sorted(candidates)[0]
    return name, _clean_frame(candidates[name])


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Drop NaN rows before analysis (data prep, not analysis math).

    PalmSens exports occasionally carry a trailing all-NaN row that
    poisons the vendored integrators (CSC -> NaN), so incomplete rows
    are removed here in the tool layer; the vendored code is read-only.
    """
    return df.dropna().reset_index(drop=True)


def _compact(value: Any) -> Any:
    """Recursively shrink a result payload for the model context.

    Converts numpy scalars to Python, rounds floats to 6 significant
    digits, and replaces non-finite floats with None (NaN is not valid
    JSON).
    """
    if isinstance(value, dict):
        return {str(k): _compact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_compact(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return float(f"{value:.6g}")
    return value


def _emit_figures(
    figure_sink: Optional[FigureSinkCallable],
    tool: str,
    figures: list[tuple[str, Any]],
) -> int:
    """Render figures to PNG and hand them to the sink; ALWAYS close.

    Args:
        figure_sink: Optional sink callable; when None the figures are
            closed unrendered (summaries only).
        tool: Tool name stamped into each payload.
        figures: ``(title, matplotlib Figure)`` pairs.

    Returns:
        Number of figures actually delivered to the sink.
    """
    delivered = 0
    for title, fig in figures:
        try:
            if figure_sink is None:
                continue
            buffer = io.BytesIO()
            fig.savefig(buffer, format="png", dpi=100)
            try:
                figure_sink({
                    "title": title,
                    "tool": tool,
                    "png": buffer.getvalue(),
                })
                delivered += 1
            except Exception:  # noqa: BLE001 - sink must not kill the tool
                logger.exception("figure_sink failed for %r", title)
        except Exception:  # noqa: BLE001 - render failure is non-fatal
            logger.exception("Figure render failed for %r", title)
        finally:
            plt.close(fig)
    return delivered


def _safe(tool_name: str, fn: Callable[[dict[str, Any]], dict[str, Any]]):
    """Wrap a handler so it can never raise into the dispatch layer."""

    def handler(tool_input: dict[str, Any]) -> dict[str, Any]:
        try:
            return fn(dict(tool_input or {}))
        except _AnalysisError as exc:
            result = {"ok": False, "tool": tool_name, "error": str(exc)}
            result.update(_compact(exc.extra))
            return result
        except Exception as exc:  # noqa: BLE001 - structured error only
            logger.exception("Analysis tool %r failed", tool_name)
            return {
                "ok": False,
                "tool": tool_name,
                "error": f"{type(exc).__name__}: {exc}",
            }

    return handler


def _eis_apex_assessment(df: pd.DataFrame) -> dict[str, Any]:
    """Decide whether the EIS semicircle apex was actually captured.

    The vendored ``EISAnalyzer`` derives Rct from the maximum of ``|Z''|`` (the
    semicircle top). If that maximum lands on the LOWEST measured frequency —
    i.e. ``-Z''`` is still rising at ``freq_end`` — the arc never turned over,
    so the reported Rct is not a measurement at all (just ``Z'`` at the sweep
    floor minus Rs) and ``peak_frequency`` is just ``freq_end``. This flags that
    case so the agent reports Rct as unreliable / a lower bound instead of
    quoting a confident value.

    Returns a dict of fields to MERGE into the metrics (overriding ``rct_ohm`` /
    ``peak_frequency_hz`` / ``time_constant_s`` with None when the apex was not
    reached), or ``{}`` when it cannot assess. Best-effort: never raises.
    """
    try:
        if not {"Frequency_Hz", "Z_imag_Ohm"}.issubset(df.columns):
            return {}
        d = df.sort_values("Frequency_Hz", ascending=False).reset_index(drop=True)
        neg = -d["Z_imag_Ohm"].to_numpy(dtype=float)  # +ve up the capacitive arc
        freqs = d["Frequency_Hz"].to_numpy(dtype=float)
        n = len(neg)
        if n < 4 or not np.isfinite(neg).any():
            return {}
        imax = int(np.nanargmax(neg))
        peak = float(neg[imax])
        end = float(neg[-1])  # lowest-frequency point
        freq_min = float(freqs[-1])
        # Apex captured only if the -Z'' maximum is INTERIOR (not the lowest
        # frequency) AND the low-frequency tail has come back down from it. If
        # the max is the last point, the arc is still climbing -> not reached.
        apex_reached = bool(imax < n - 1 and peak > 0 and end < 0.9 * peak)
        out: dict[str, Any] = {
            "apex_reached": apex_reached,
            "lowest_frequency_hz": freq_min,
        }
        if apex_reached:
            out["rct_reliable"] = True
            out["rct_note"] = (
                "Semicircle apex captured within the swept range, so Rct is a "
                "supported estimate."
            )
            return out
        lb = 2.0 * peak if peak > 0 else None
        out["rct_reliable"] = False
        # Suppress the unreliable extractions rather than quote them.
        out["rct_ohm"] = None
        out["peak_frequency_hz"] = None
        out["time_constant_s"] = None
        if lb is not None:
            out["rct_lower_bound_ohm"] = lb
        out["rct_note"] = (
            f"-Z'' is still rising at the lowest measured frequency "
            f"({freq_min:.3g} Hz) -- the semicircle apex was NOT reached, so "
            "Rct cannot be reliably determined from this sweep"
            + (f" (it is at least ~{lb:.3g} ohm, a lower bound)" if lb else "")
            + ". Lower freq_end (e.g. to 0.1 Hz) to capture the apex before "
            "quoting Rct."
        )
        return out
    except Exception:  # noqa: BLE001 - assessment is best-effort, never fatal
        logger.exception("EIS apex assessment failed")
        return {}


def _path_prop() -> dict[str, Any]:
    """Schema for the session-file path argument."""
    return {
        "type": "string",
        "description": (
            "Path to the PalmSens .pssession file, absolute or "
            "relative to the application working directory."
        ),
    }


def _scan_name_prop(technique: str) -> dict[str, Any]:
    """Schema for the optional explicit scan-name argument."""
    return {
        "type": "string",
        "description": (
            f"Exact scan/measurement name to analyze (from "
            f"load_session). Omit to auto-select the first {technique} "
            "scan in the file."
        ),
    }


def _schema(
    properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    """Object schema with additionalProperties always false."""
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def build_analysis_tools(
    figure_sink: Optional[FigureSinkCallable] = None,
) -> list[tuple[dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]]:
    """Build the analysis ``(tool_def, handler)`` pairs.

    Args:
        figure_sink: Optional callable receiving one
            ``{"title": str, "tool": str, "png": bytes}`` dict per
            rendered figure.  Called from the agent thread; the GUI
            side must adapt it to a queued Qt signal.  When None,
            figures are skipped and only summaries are returned.

    Returns:
        List of pairs ready for ``ToolRegistry.register(tool_def,
        handler)``.  Handlers are synchronous and never raise.
    """

    # ---- load_session -----------------------------------------------------

    def load_session(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_path(args.get("path"))
        scans = _load_scans(path)
        return _compact({
            "ok": True,
            "path": path,
            "n_scans": len(scans),
            "scans": [
                {
                    "name": name,
                    "rows": int(len(df)),
                    "columns": [str(c) for c in df.columns],
                }
                for name, df in sorted(scans.items())
            ],
            "techniques": {
                tech: names
                for tech, names in _technique_map(scans).items()
                if names
            },
        })

    # ---- analyze_cv ---------------------------------------------------------

    def analyze_cv(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_path(args.get("path"))
        scans = _load_scans(path)
        name, df = _pick_scan(
            scans, "CV", args.get("scan_name"), prefer_keyword="cyclic"
        )
        scan_rate = args.get("scan_rate")
        area = args.get("electrode_area_cm2")
        concentration_mM = args.get("concentration_mM")
        analyzer = CVAnalyzer(df)
        notes = []

        # Redox-peak / reversibility metrics (Epa/Epc, delta_ep, ipa/ipc,
        # reversible flag) -- the area-INDEPENDENT cleanliness gate. Fold into
        # get_summary() by extracting before it runs; skip gracefully if the
        # scan has no resolvable peaks.
        try:
            analyzer.extract_peaks()
        except ValueError as exc:
            notes.append(f"Peak extraction skipped: {exc}")

        metrics = analyzer.get_summary(
            scan_rate=scan_rate, electrode_area=area
        )
        if (scan_rate is None) != (area is None):
            notes.append(
                "CSC needs BOTH scan_rate and electrode_area_cm2; "
                "only one was given, so CSC was not computed."
            )

        # Randles-Sevcik electroactive area (single-rate) when a redox-probe
        # concentration is supplied with the scan rate. Gives the number that
        # turns a high absolute Rct into an area-normalized Rct (Rct x A) the
        # caller can compare to clean (~65-300 Ohm*cm^2) vs fouled bands.
        if concentration_mM is not None:
            if scan_rate is None:
                notes.append(
                    "Randles-Sevcik area needs scan_rate alongside "
                    "concentration_mM."
                )
            elif analyzer.peaks is None:
                notes.append(
                    "Randles-Sevcik area needs resolvable CV peaks; "
                    "peak extraction did not succeed."
                )
            elif not analyzer.peaks.get("peaks_well_defined", True):
                notes.append(
                    "Randles-Sevcik area skipped: the CV peaks are not "
                    "well-defined (they sit at a sweep endpoint -- a rising "
                    "background, not a true faradaic peak), so an "
                    "electroactive-area estimate would be meaningless."
                )
            else:
                # Optional numerics default when ABSENT; tolerate an
                # explicit JSON null (a common model habit for optional
                # fields) instead of letting int(None)/float(None) raise a
                # TypeError that fails the whole tool and discards the
                # peak/reversibility metrics already computed above.
                n_raw = args.get("n_electrons")
                d_raw = args.get("diffusion_coeff_cm2_s")
                try:
                    a_cm2 = analyzer.calculate_electroactive_area(
                        scan_rate=float(scan_rate),
                        concentration_mM=float(concentration_mM),
                        n=int(n_raw) if n_raw is not None else 1,
                        diffusion_coeff=(
                            float(d_raw)
                            if d_raw is not None
                            else DEFAULT_DIFFUSION_COEFF_CM2_S
                        ),
                        peak=args.get("peak") or "anodic",
                    )
                    metrics["electroactive_area_cm2"] = a_cm2
                    metrics["electroactive_area_mm2"] = a_cm2 * CM2_TO_MM2
                except ValueError as exc:
                    notes.append(f"Randles-Sevcik area skipped: {exc}")
        fig = (
            analyzer.plot_cv_with_cathodic_area()
            if analyzer.csc is not None
            else analyzer.plot_cv()
        )
        n_figs = _emit_figures(
            figure_sink, "analyze_cv", [(f"CV: {name}", fig)]
        )
        result = {
            "ok": True,
            "scan_name": name,
            "metrics": metrics,
            "figures_emitted": n_figs,
        }
        if notes:
            result["notes"] = notes
        return _compact(result)

    # ---- analyze_eis --------------------------------------------------------

    def analyze_eis(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_path(args.get("path"))
        scans = _load_scans(path)
        name, df = _pick_scan(scans, "EIS", args.get("scan_name"))
        analyzer = EISAnalyzer(df)
        analyzer.scan_name = name
        # get_summary() runs the vendored Rs/Rct extraction (the cheap
        # built-in fit info: high-frequency intercept + semicircle).
        metrics = analyzer.get_summary()
        # Guard against quoting an Rct when the semicircle apex was never
        # captured (-Z'' still rising at freq_end): merge apex_reached /
        # rct_reliable / rct_note and null out the unreliable extractions.
        metrics.update(_eis_apex_assessment(df))
        n_figs = _emit_figures(
            figure_sink,
            "analyze_eis",
            [
                (f"Nyquist: {name}", analyzer.plot_nyquist()),
                (f"Bode: {name}", analyzer.plot_bode()),
            ],
        )
        return _compact({
            "ok": True,
            "scan_name": name,
            "metrics": metrics,
            "figures_emitted": n_figs,
        })

    # ---- analyze_ca ---------------------------------------------------------

    def analyze_ca(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_path(args.get("path"))
        scans = _load_scans(path)
        name, df = _pick_scan(scans, "CA", args.get("scan_name"))
        analyzer = CAAnalyzer(df)
        b_start = args.get("baseline_start_s")
        b_end = args.get("baseline_end_s")
        window = (
            (float(b_start), float(b_end))
            if b_start is not None and b_end is not None
            else None
        )
        baseline_mean, baseline_std = analyzer.get_baseline_stats(
            baseline_window=window
        )
        time = analyzer.data["Time (s)"]
        current = analyzer.data["Current (A)"]
        metrics = {
            "technique": "CA",
            "n_data_points": int(len(analyzer.data)),
            "duration_s": float(time.max() - time.min()),
            "current_mean_a": float(current.mean()),
            "current_min_a": float(current.min()),
            "current_max_a": float(current.max()),
            "baseline_mean_a": float(baseline_mean),
            "baseline_std_a": float(baseline_std),
        }
        n_figs = _emit_figures(
            figure_sink,
            "analyze_ca",
            [(f"CA: {name}", analyzer.plot_raw_timeseries())],
        )
        return _compact({
            "ok": True,
            "scan_name": name,
            "metrics": metrics,
            "figures_emitted": n_figs,
        })

    # ---- analyze_cp ---------------------------------------------------------

    def analyze_cp(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_path(args.get("path"))
        scans = _load_scans(path)
        name, df = _pick_scan(scans, "CP", args.get("scan_name"))
        analyzer = CPAnalyzer(df)
        fraction = args.get("plateau_fraction")
        steady = analyzer.analyze_steady_state(
            plateau_fraction=(
                float(fraction) if fraction is not None else 0.2
            )
        )
        plateaus = steady.head(_MAX_ROWS).to_dict(orient="records")
        transition = analyzer.analyze_transition_time()
        metrics = {
            "technique": "CP",
            "n_data_points": int(len(analyzer.data)),
            "n_plateaus": int(len(steady)),
            "plateaus": plateaus,
            "transition_time": transition,
        }
        n_figs = _emit_figures(
            figure_sink,
            "analyze_cp",
            [(
                f"CP: {name}",
                analyzer.plot_chronopotentiogram(shade_steps=True),
            )],
        )
        return _compact({
            "ok": True,
            "scan_name": name,
            "metrics": metrics,
            "figures_emitted": n_figs,
        })

    # ---- analyze_ecsa --------------------------------------------------------

    def analyze_ecsa(args: dict[str, Any]) -> dict[str, Any]:
        path = _resolve_path(args.get("path"))
        scans = _load_scans(path)
        cv_scans = filter_scans(scans, "CV")
        if not cv_scans:
            raise _AnalysisError(
                "This session contains no CV data; ECSA needs CV "
                "scans recorded at two or more scan rates.",
                **_available_text(scans),
            )
        scan_rates = args.get("scan_rates")
        if scan_rates:
            unknown = sorted(set(scan_rates) - set(cv_scans))
            if unknown:
                raise _AnalysisError(
                    f"scan_rates references unknown CV scans: "
                    f"{unknown}.",
                    **_available_text(scans),
                )
            cv_scans = {
                name: cv_scans[name] for name in scan_rates
            }
            scan_rates = {
                name: float(rate)
                for name, rate in scan_rates.items()
            }
        if len(cv_scans) < 2:
            raise _AnalysisError(
                "ECSA (Cdl) needs CV scans at two or more scan "
                f"rates; this session offers {len(cv_scans)} usable "
                "CV scan(s).",
                **_available_text(scans),
            )
        cv_scans = {
            name: _clean_frame(df) for name, df in cv_scans.items()
        }
        try:
            analyzer = ECSAAnalyzer(cv_scans, scan_rates=scan_rates)
        except ValueError as exc:
            raise _AnalysisError(
                f"Could not build the ECSA analyzer: {exc} Provide "
                "explicit scan_rates ({scan_name: V_per_s}) when the "
                "scan names do not encode the rate.",
                **_available_text(scans),
            ) from None
        v_midpoint = args.get("v_midpoint")
        cdl = analyzer.calculate_cdl(
            v_midpoint=(
                float(v_midpoint) if v_midpoint is not None else None
            ),
            electrode_area_cm2=args.get("electrode_area_cm2"),
            specific_capacitance=args.get("specific_capacitance"),
        )
        metrics = {
            key: cdl.get(key)
            for key in (
                "cdl_F", "cdl_uF", "cdl_uF_cm2", "r_squared",
                "slope", "intercept", "v_midpoint", "scan_rates",
                "roughness_factor", "electrode_area_cm2",
            )
            if key in cdl
        }
        metrics["n_scans_used"] = len(cdl.get("scan_rates", []))
        n_figs = _emit_figures(
            figure_sink,
            "analyze_ecsa",
            [("ECSA: Cdl fit", analyzer.plot_cdl_fit())],
        )
        return _compact({
            "ok": True,
            "scan_names": sorted(cv_scans),
            "metrics": metrics,
            "figures_emitted": n_figs,
        })

    # ---- analyze_cic ---------------------------------------------------------

    def analyze_cic(args: dict[str, Any]) -> dict[str, Any]:
        raw_path = args.get("path")
        if isinstance(raw_path, str) and raw_path.strip().lower(
        ).endswith(".pssession"):
            # CIC comes from Gamry .DTA waveforms, never .pssession.
            path = _resolve_path(raw_path)
            scans = _load_scans(path)
            raise _AnalysisError(
                "CIC analysis needs a Gamry .DTA voltage-transient "
                "file; a PalmSens .pssession was given and contains "
                "no CIC data.",
                **_available_text(scans),
            )
        path = _resolve_path(raw_path, suffixes=(".dta",))
        area = args.get("electrode_area_cm2")
        if not isinstance(area, (int, float)) or area <= 0:
            raise _AnalysisError(
                "electrode_area_cm2 (positive number, cm^2) is "
                "required for CIC."
            )
        data = load_cic(path)
        kwargs: dict[str, Any] = {}
        if args.get("e_safe_cath") is not None:
            kwargs["e_safe_cath"] = float(args["e_safe_cath"])
        if args.get("e_safe_an") is not None:
            kwargs["e_safe_an"] = float(args["e_safe_an"])
        analyzer = CoganCICAnalyzer(data, **kwargs)
        cic = analyzer.determine_cic(float(area))
        metrics = {
            "technique": "CIC",
            "n_pulses": len(analyzer.all_pulse_results),
            "e_safe_cath_v": analyzer.e_safe_cath,
            "e_safe_an_v": analyzer.e_safe_an,
        }
        metrics.update(cic)
        figures = []
        try:
            figures.append((
                f"CIC transient: {os.path.basename(path)}",
                analyzer.plot_voltage_transient(),
            ))
        except Exception:  # noqa: BLE001 - figure is best-effort
            logger.exception("CIC transient plot failed")
        n_figs = _emit_figures(figure_sink, "analyze_cic", figures)
        return _compact({
            "ok": True,
            "path": path,
            "metrics": metrics,
            "figures_emitted": n_figs,
        })

    # ---- Tool definitions ------------------------------------------------------

    defs: list[tuple[dict[str, Any], Any]] = [
        (
            {
                "name": "load_session",
                "description": (
                    "List the contents of a saved PalmSens .pssession "
                    "data file: every scan/technique name with its row "
                    "count and column names, plus which analyze_* "
                    "techniques (CV/EIS/CA/CP) the file supports. Call "
                    "this FIRST whenever the user points you at a data "
                    "file, and before any analyze_* tool when the scan "
                    "names are not already known. Read-only and safe."
                ),
                "input_schema": _schema({"path": _path_prop()}, ["path"]),
            },
            _safe("load_session", load_session),
        ),
        (
            {
                "name": "analyze_cv",
                "description": (
                    "Analyze a cyclic voltammetry scan from a saved "
                    ".pssession file with the vendored CVAnalyzer: peak "
                    "anodic/cathodic currents, potential range, redox-peak "
                    "reversibility metrics (epa_v/epc_v, delta_ep_mv, "
                    "ipa_ipc_ratio, e_half_v, peaks_well_defined, reversible "
                    "-- the area-INDEPENDENT cleanliness gate: clean fast "
                    "kinetics give delta_ep near 59 mV and reversible=true, a "
                    "passivated surface gives drawn-out/absent peaks), and -- "
                    "when BOTH scan_rate and electrode_area_cm2 are given -- "
                    "charge storage capacity (CSC, mC/cm^2) with open/closed "
                    "loop detection. When concentration_mM is given with "
                    "scan_rate, also returns the Randles-Sevcik "
                    "electroactive_area_cm2/_mm2 (divide a paired EIS Rct by "
                    "this area to area-normalize it). Emits a CV figure into "
                    "the app. Call this when the user asks to analyze, "
                    "summarize, or plot saved CV data; use load_session first "
                    "if the scan name is unknown."
                ),
                "input_schema": _schema(
                    {
                        "path": _path_prop(),
                        "scan_name": _scan_name_prop("CV"),
                        "scan_rate": {
                            "type": "number",
                            "description": (
                                "Scan rate in V/s; required together "
                                "with electrode_area_cm2 for CSC and with "
                                "concentration_mM for Randles-Sevcik area."
                            ),
                        },
                        "electrode_area_cm2": {
                            "type": "number",
                            "description": (
                                "Electrode area in cm^2; required "
                                "together with scan_rate for CSC."
                            ),
                        },
                        "concentration_mM": {
                            "type": "number",
                            "description": (
                                "Redox-probe bulk concentration in mM "
                                "(e.g. 5 for 5 mM ferri/ferrocyanide). With "
                                "scan_rate, triggers a single-rate "
                                "Randles-Sevcik electroactive-area estimate."
                            ),
                        },
                        "n_electrons": {
                            "type": "integer",
                            "description": (
                                "Electrons transferred for the redox couple "
                                "(default 1). Used only for the area estimate."
                            ),
                        },
                        "diffusion_coeff_cm2_s": {
                            "type": "number",
                            "description": (
                                "Diffusion coefficient D in cm^2/s for the "
                                "area estimate (default ~7.6e-6, ferricyanide)."
                            ),
                        },
                        "peak": {
                            "type": "string",
                            "enum": ["anodic", "cathodic", "mean"],
                            "description": (
                                "Which peak current the area estimate uses "
                                "(default 'anodic')."
                            ),
                        },
                    },
                    ["path"],
                ),
            },
            _safe("analyze_cv", analyze_cv),
        ),
        (
            {
                "name": "analyze_eis",
                "description": (
                    "Analyze an impedance (EIS) scan from a saved "
                    ".pssession file with the vendored EISAnalyzer: Rs "
                    "(high-frequency intercept), Rct (semicircle), |Z| "
                    "at 1 kHz, peak frequency and time constant. Emits "
                    "Nyquist and Bode figures into the app. Call this "
                    "when the user asks to analyze or plot saved EIS / "
                    "impedance data. IMPORTANT: check metrics.apex_reached "
                    "and metrics.rct_reliable. When apex_reached is false the "
                    "-Z'' semicircle never turned over (still rising at the "
                    "lowest frequency), so rct_ohm is null and "
                    "rct_lower_bound_ohm / rct_note are given instead -- report "
                    "that Rct is NOT reliable (only a lower bound) and suggest a "
                    "lower freq_end; do NOT quote a semicircle Rct. Rs and |Z| "
                    "at 1 kHz stay valid either way."
                ),
                "input_schema": _schema(
                    {
                        "path": _path_prop(),
                        "scan_name": _scan_name_prop("EIS"),
                    },
                    ["path"],
                ),
            },
            _safe("analyze_eis", analyze_eis),
        ),
        (
            {
                "name": "analyze_ca",
                "description": (
                    "Analyze a chronoamperometry (i-t) scan from a "
                    "saved .pssession file with the vendored "
                    "CAAnalyzer: duration, current statistics, and "
                    "baseline mean/std (default window: first 60 s, or "
                    "baseline_start_s..baseline_end_s). Emits the time-"
                    "series figure into the app. Call this when the "
                    "user asks to analyze saved CA / amperometry data. "
                    "Returns ok=false listing available techniques if "
                    "the file holds no CA data."
                ),
                "input_schema": _schema(
                    {
                        "path": _path_prop(),
                        "scan_name": _scan_name_prop("CA"),
                        "baseline_start_s": {
                            "type": "number",
                            "description": (
                                "Baseline window start in seconds "
                                "(use with baseline_end_s)."
                            ),
                        },
                        "baseline_end_s": {
                            "type": "number",
                            "description": (
                                "Baseline window end in seconds "
                                "(use with baseline_start_s)."
                            ),
                        },
                    },
                    ["path"],
                ),
            },
            _safe("analyze_ca", analyze_ca),
        ),
        (
            {
                "name": "analyze_cp",
                "description": (
                    "Analyze a chronopotentiometry (E-t) scan from a "
                    "saved .pssession file with the vendored "
                    "CPAnalyzer: plateau detection, steady-state "
                    "potential per plateau, and Sand's transition time "
                    "for single-step records. Emits the "
                    "chronopotentiogram figure into the app. Call this "
                    "when the user asks to analyze saved CP / "
                    "galvanostatic data. Returns ok=false listing "
                    "available techniques if the file holds no CP data."
                ),
                "input_schema": _schema(
                    {
                        "path": _path_prop(),
                        "scan_name": _scan_name_prop("CP"),
                        "plateau_fraction": {
                            "type": "number",
                            "description": (
                                "Fraction (0, 1] of each plateau tail "
                                "used for the steady-state average "
                                "(default 0.2)."
                            ),
                        },
                    },
                    ["path"],
                ),
            },
            _safe("analyze_cp", analyze_cp),
        ),
        (
            {
                "name": "analyze_ecsa",
                "description": (
                    "Estimate double-layer capacitance (Cdl) and "
                    "electrochemical surface area metrics from CV "
                    "scans at MULTIPLE scan rates in one .pssession "
                    "file, using the vendored ECSAAnalyzer. Provide "
                    "scan_rates ({scan_name: V_per_s}) unless the scan "
                    "names already encode the rate (e.g. '50mVps'). "
                    "Emits the Cdl linear-fit figure into the app. "
                    "Call this only when the user asks for ECSA / Cdl "
                    "/ roughness; returns ok=false listing available "
                    "techniques when the file lacks multi-rate CV data."
                ),
                "input_schema": _schema(
                    {
                        "path": _path_prop(),
                        "scan_rates": {
                            "type": "object",
                            "description": (
                                "Map of CV scan name -> scan rate in "
                                "V/s, e.g. {\"CV 1\": 0.05}. Omit to "
                                "auto-detect rates from scan names."
                            ),
                            "additionalProperties": {"type": "number"},
                        },
                        "v_midpoint": {
                            "type": "number",
                            "description": (
                                "Potential (V) where the forward/"
                                "reverse current difference is read; "
                                "omit to auto-center."
                            ),
                        },
                        "electrode_area_cm2": {
                            "type": "number",
                            "description": (
                                "Electrode area in cm^2 to report Cdl "
                                "as uF/cm^2."
                            ),
                        },
                        "specific_capacitance": {
                            "type": "number",
                            "description": (
                                "Smooth-surface specific capacitance "
                                "(uF/cm^2) to compute the roughness "
                                "factor (needs electrode_area_cm2)."
                            ),
                        },
                    },
                    ["path"],
                ),
            },
            _safe("analyze_ecsa", analyze_ecsa),
        ),
        (
            {
                "name": "analyze_cic",
                "description": (
                    "Determine charge injection capacity (CIC, Cogan "
                    "2008 voltage-transient method) from a Gamry .DTA "
                    "biphasic-pulse file using the vendored "
                    "CoganCICAnalyzer: pulse detection, safety-limit "
                    "evaluation, and CIC in mC/cm^2. Requires "
                    "electrode_area_cm2. Emits the annotated transient "
                    "figure into the app. Call this only for Gamry "
                    ".DTA stimulation data -- .pssession files contain "
                    "no CIC data and return ok=false."
                ),
                "input_schema": _schema(
                    {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path to the Gamry .DTA voltage-"
                                "transient file (absolute or relative)."
                            ),
                        },
                        "electrode_area_cm2": {
                            "type": "number",
                            "description": (
                                "Electrode geometric area in cm^2 "
                                "(required)."
                            ),
                        },
                        "e_safe_cath": {
                            "type": "number",
                            "description": (
                                "Cathodic safety limit in V (default "
                                "-0.6, water reduction)."
                            ),
                        },
                        "e_safe_an": {
                            "type": "number",
                            "description": (
                                "Anodic safety limit in V (default "
                                "0.8, water oxidation)."
                            ),
                        },
                    },
                    ["path", "electrode_area_cm2"],
                ),
            },
            _safe("analyze_cic", analyze_cic),
        ),
    ]
    return defs
