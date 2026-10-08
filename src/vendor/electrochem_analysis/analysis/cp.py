"""
CP Analyzer - Chronopotentiometry (Galvanostatic E vs t).

Lightweight analyzer for chronopotentiometry data with step detection,
per-plateau steady-state extraction, optional Sand's-time estimate, and a
chronopotentiogram plotter. Mirrors the EIS / CV / CA analyzer pattern.

PalmSens METHOD_ID=pot data is standardized upstream by psession_parser to
columns ['Time (s)', 'Potential (V)'].
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class CPAnalyzer:
    """Analyzer for Chronopotentiometry (galvanostatic E vs t) data."""

    def __init__(self, data: pd.DataFrame):
        """
        Initialize CP analyzer with raw time-series data.

        Args:
            data: DataFrame with columns ['Time (s)', 'Potential (V)'].

        Raises:
            ValueError: If the DataFrame is empty.
            KeyError: If required columns are missing.
        """
        if data.empty:
            raise ValueError("Data DataFrame is empty")

        required_cols = {'Time (s)', 'Potential (V)'}
        missing = sorted(required_cols - set(data.columns))
        if missing:
            raise KeyError(f"Missing required columns: {missing}")

        self.data = data.reset_index(drop=True)

    def detect_steps(
        self,
        threshold_v: float = 0.02,
        min_gap_samples: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Detect plateau boundaries via potential-jump scanning.

        Scans ``np.diff(Potential)`` for transitions whose absolute jump
        exceeds ``threshold_v``. A running cursor enforces a minimum sample
        gap between accepted transitions so noise doesn't fragment a plateau.
        Returns one dict per plateau (a single-step trace returns two
        plateaus: before- and after-step).

        Args:
            threshold_v: Minimum |dE| (V) to qualify as a step.
            min_gap_samples: Minimum sample distance between accepted
                transitions. Suppresses noise-driven false positives.

        Returns:
            List of dicts ``{step_index, t_start_s, t_end_s, n_samples}``.
            Zero detected jumps -> single plateau covering the whole trace.
            Trace with < 2 samples -> empty list.
        """
        potential = self.data['Potential (V)'].values
        time = self.data['Time (s)'].values
        n = len(potential)

        if n < 2:
            return []

        diff = np.abs(np.diff(potential))
        above = np.where(diff > threshold_v)[0]

        accepted: List[int] = []
        last_accepted = -min_gap_samples - 1
        for idx in above:
            if idx - last_accepted >= min_gap_samples:
                accepted.append(int(idx))
                last_accepted = int(idx)

        if not accepted:
            return [{
                'step_index': 0,
                't_start_s': float(time[0]),
                't_end_s': float(time[-1]),
                'n_samples': int(n),
            }]

        plateaus: List[Dict[str, Any]] = []
        start = 0
        for ordinal, trans in enumerate(accepted):
            plateaus.append({
                'step_index': ordinal,
                't_start_s': float(time[start]),
                't_end_s': float(time[trans]),
                'n_samples': int(trans - start + 1),
            })
            start = trans + 1

        plateaus.append({
            'step_index': len(accepted),
            't_start_s': float(time[start]),
            't_end_s': float(time[n - 1]),
            'n_samples': int(n - start),
        })

        return plateaus

    def analyze_steady_state(
        self,
        plateau_fraction: float = 0.2,
    ) -> pd.DataFrame:
        """
        Compute steady-state potential over the tail of each plateau.

        For each plateau from :meth:`detect_steps`, takes the final
        ``plateau_fraction`` of samples and computes mean / standard
        deviation of the potential.

        Args:
            plateau_fraction: Fraction (0, 1] of plateau samples used for
                the tail average.

        Returns:
            DataFrame with columns ``[step_index, t_plateau_start_s,
            t_plateau_end_s, E_steady_V, E_std_V, n_samples_used]``.

        Raises:
            ValueError: If ``plateau_fraction`` is outside (0, 1].
        """
        if not (0 < plateau_fraction <= 1):
            raise ValueError(
                "plateau_fraction must be in (0, 1], "
                f"got {plateau_fraction}"
            )

        potential = self.data['Potential (V)'].values
        time = self.data['Time (s)'].values

        rows: List[Dict[str, Any]] = []
        for plateau in self.detect_steps():
            t_start = plateau['t_start_s']
            t_end = plateau['t_end_s']
            mask = (time >= t_start) & (time <= t_end)
            plateau_potential = potential[mask]
            if plateau_potential.size == 0:
                continue

            n_tail = max(1, int(round(plateau_potential.size * plateau_fraction)))
            n_tail = min(n_tail, plateau_potential.size)
            tail = plateau_potential[-n_tail:]

            e_mean = float(np.mean(tail))
            e_std = float(np.std(tail, ddof=1)) if n_tail > 1 else float('nan')

            rows.append({
                'step_index': plateau['step_index'],
                't_plateau_start_s': float(t_start),
                't_plateau_end_s': float(t_end),
                'E_steady_V': e_mean,
                'E_std_V': e_std,
                'n_samples_used': int(n_tail),
            })

        return pd.DataFrame(rows, columns=[
            'step_index', 't_plateau_start_s', 't_plateau_end_s',
            'E_steady_V', 'E_std_V', 'n_samples_used',
        ])

    def analyze_transition_time(self) -> Optional[Dict[str, float]]:
        """
        Estimate Sand's time for a single-step galvanostatic record.

        Only fires when :meth:`detect_steps` returns exactly one plateau
        (no detected jump). Approximates the transition time as the
        t-location of the maximum ``|dE/dt|``.

        Returns:
            Dict ``{'tau_s', 'max_abs_dEdt'}`` or ``None`` when the record
            has multiple plateaus or fewer than 5 samples.
        """
        time = self.data['Time (s)'].values
        potential = self.data['Potential (V)'].values

        if time.size < 5:
            return None

        if len(self.detect_steps()) != 1:
            return None

        dt = np.diff(time)
        dt_safe = np.where(dt > 0, dt, np.nan)
        abs_dedt = np.abs(np.diff(potential) / dt_safe)

        if not np.isfinite(abs_dedt).any():
            return None

        finite_indices = np.where(np.isfinite(abs_dedt))[0]
        local_argmax = int(np.argmax(abs_dedt[finite_indices]))
        idx = int(finite_indices[local_argmax])
        tau_s = float(0.5 * (time[idx] + time[idx + 1]))

        return {'tau_s': tau_s, 'max_abs_dEdt': float(abs_dedt[idx])}

    def plot_chronopotentiogram(
        self,
        shade_steps: bool = False,
        figsize: Tuple[float, float] = (8, 5),
    ) -> plt.Figure:
        """
        Plot Potential (V) vs Time (s).

        Minimal-annotation style per CLAUDE.md: axis labels with units,
        light grid, no in-plot value annotations. Optional alternating
        ``axvspan`` shading per plateau exposes step boundaries.

        Args:
            shade_steps: If True, shade alternating plateaus.
            figsize: Matplotlib figure size.

        Returns:
            The matplotlib Figure. Caller is responsible for show / save;
            ``ExportManager.save_figure`` closes the figure for you.
        """
        fig, ax = plt.subplots(figsize=figsize)
        time = self.data['Time (s)'].values
        potential = self.data['Potential (V)'].values

        ax.plot(time, potential, 'k-', linewidth=1.0)

        if shade_steps:
            for plateau in self.detect_steps():
                if plateau['step_index'] % 2 == 1:
                    ax.axvspan(
                        plateau['t_start_s'], plateau['t_end_s'],
                        alpha=0.15, color='steelblue', linewidth=0,
                    )

        ax.set_xlabel('Time (s)')
        ax.set_ylabel('Potential (V)')
        ax.set_title('Chronopotentiogram')
        ax.grid(True, alpha=0.3, linestyle='--')

        plt.tight_layout()
        return fig

    @classmethod
    def batch_analyze(
        cls,
        scans: Dict[str, pd.DataFrame],
        applied_currents: Optional[Dict[str, float]] = None,
        electrode_area: Optional[float] = None,
        export_manager=None,
    ) -> Tuple[pd.DataFrame, Dict[str, plt.Figure]]:
        """
        Batch-analyze a dict of CP scans.

        Iterates ``scans``, runs :meth:`analyze_steady_state` per scan,
        generates one chronopotentiogram per scan. When ``export_manager``
        is supplied, writes per-scan figures and the aggregate summary
        DataFrame.

        Args:
            scans: Mapping ``{scan_name: DataFrame}`` from ``load_psession``
                + ``filter_scans('CP')``.
            applied_currents: Optional ``{scan_name: I_A}`` (amperes); when
                supplied, propagates to ``applied_current_A`` column.
            electrode_area: Optional electrode area in cm^2; propagates to
                ``electrode_area_cm2`` column when supplied.
            export_manager: Optional ExportManager. When provided, each
                figure is saved via ``save_figure`` and the summary via
                ``save_dataframe('cp_batch_summary')``.

        Returns:
            ``(summary_df, figures_dict)`` with one summary row per
            (scan, plateau) and one figure per scan.
        """
        applied_currents = applied_currents or {}
        rows: List[Dict[str, Any]] = []
        figures: Dict[str, plt.Figure] = {}

        for scan_name, df in scans.items():
            try:
                analyzer = cls(df)
            except (ValueError, KeyError) as exc:
                logger.warning("Skipping CP scan %s: %s", scan_name, exc)
                continue

            try:
                steady = analyzer.analyze_steady_state()
            except Exception as exc:  # noqa: BLE001 - per-scan isolation
                logger.error(
                    "analyze_steady_state failed for %s: %s", scan_name, exc
                )
                continue

            i_app = applied_currents.get(scan_name)
            i_app_val = (
                float(i_app)
                if (i_app is not None
                    and not (isinstance(i_app, float) and np.isnan(i_app)))
                else np.nan
            )
            area_val = (
                float(electrode_area) if electrode_area is not None
                else np.nan
            )

            for _, plateau_row in steady.iterrows():
                rows.append({
                    'scan_name': scan_name,
                    'step_index': int(plateau_row['step_index']),
                    't_plateau_start_s': float(plateau_row['t_plateau_start_s']),
                    't_plateau_end_s': float(plateau_row['t_plateau_end_s']),
                    'E_steady_V': float(plateau_row['E_steady_V']),
                    'E_std_V': float(plateau_row['E_std_V']),
                    'n_samples_used': int(plateau_row['n_samples_used']),
                    'applied_current_A': i_app_val,
                    'electrode_area_cm2': area_val,
                })

            fig = analyzer.plot_chronopotentiogram()
            figures[scan_name] = fig

            if export_manager is not None:
                try:
                    export_manager.save_figure(fig, scan_name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "save_figure failed for %s: %s", scan_name, exc
                    )

        summary_df = pd.DataFrame(rows, columns=[
            'scan_name', 'step_index', 't_plateau_start_s',
            't_plateau_end_s', 'E_steady_V', 'E_std_V', 'n_samples_used',
            'applied_current_A', 'electrode_area_cm2',
        ])

        if export_manager is not None and not summary_df.empty:
            try:
                export_manager.save_dataframe(summary_df, 'cp_batch_summary')
            except Exception as exc:  # noqa: BLE001
                logger.warning("save_dataframe failed: %s", exc)

        return summary_df, figures
