"""
CIC Analyzer - Charge Injection Capacity using Cogan 2008 Voltage Transient Methodology

Implements the voltage transient analysis method from:
Cogan, S.F. (2008). Neural Stimulation and Recording Electrodes.
Annual Review of Biomedical Engineering, 10, 275-309.

Key features:
- Analyzes last pulse in train (steady-state)
- Interpolates access voltage (Va) at pulse transitions
- Calculates activation voltage (ηa) and concentration voltage (ηc)
- Computes maximum polarization (Emc, Ema)
- Measures equilibrium potential (Eo)
- Determines CIC by finding max safe charge within safety limits
"""

import logging
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.integrate import trapezoid
from scipy.interpolate import interp1d
from typing import Tuple, Dict, Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)


class CoganCICAnalyzer:
    """
    CIC analyzer implementing Cogan 2008 voltage transient methodology.

    This analyzer processes biphasic electrical stimulation pulses to determine
    the charge injection capacity of neural electrodes using voltage transient
    analysis. It evaluates electrode safety by comparing maximum polarization
    voltages against water window limits.

    Key features:
    - Pulse detection with configurable edge padding
    - Voltage transient decomposition (Va, Ep, ηa, ηc)
    - Safety limit evaluation (water reduction/oxidation)
    - CIC interpolation between safe and unsafe pulses
    - Detailed plotting with Cogan-style annotations
    """

    def __init__(self, data: pd.DataFrame,
                 e_safe_cath: float = -0.6,
                 e_safe_an: float = 0.8):
        """
        Initialize with Gamry DTA data.

        Args:
            data: DataFrame with columns from Gamry parser (T, Vf, Im) or
                  standardized names (Time (s), Potential (V), Current (A))
            e_safe_cath: Cathodic safety limit in V vs reference
                        (default: -0.6V for water reduction)
            e_safe_an: Anodic safety limit in V vs reference
                      (default: 0.8V for water oxidation)
        """
        self.data = data

        # Safety limits
        self.e_safe_cath = e_safe_cath
        self.e_safe_an = e_safe_an

        # Handle different column name formats
        if 'Time (s)' in data.columns:
            self.time = data['Time (s)'].values
            self.voltage = data['Potential (V)'].values
            self.current = data['Current (A)'].values
        elif 'T' in data.columns:
            self.time = data['T'].values
            self.voltage = data['Vf'].values
            self.current = data['Im'].values
        else:
            raise ValueError(f"Unrecognized column format. Available columns: {list(data.columns)}")

        # Results storage
        self.pulses = []
        self.last_pulse_idx = None
        self.analysis_results = {}
        self.all_pulse_results = []

    def _estimate_current_amplitude(self) -> float:
        """
        Estimate the pulse current amplitude from the data.

        Uses the 95th percentile of absolute current to be robust to noise.

        Returns:
            Estimated current amplitude in Amps
        """
        # Use absolute current values
        abs_current = np.abs(self.current)

        # Filter out noise floor (bottom 10%)
        noise_threshold = np.percentile(abs_current, 10)
        signal_current = abs_current[abs_current > noise_threshold]

        if len(signal_current) == 0:
            # Fallback if all data looks like noise
            return np.percentile(abs_current, 95)

        # Use 95th percentile to estimate amplitude (robust to spikes)
        amplitude = np.percentile(signal_current, 95)

        return amplitude

    def detect_pulses(self, edge_padding: int = 50, min_pulse_current: Optional[float] = None) -> list:
        """
        Detect individual biphasic pulses in the waveform.

        For square-wave biphasic pulses (cathodic + anodic phases).

        Args:
            edge_padding: Number of samples to include before/after threshold
                         crossings to capture full rising/falling edges
            min_pulse_current: Minimum current to consider as pulse (Amps).
                             If None, auto-detects based on signal amplitude.

        Returns:
            List of pulse dictionaries with start/end indices and phases
        """
        # Auto-detect threshold if not provided
        if min_pulse_current is None:
            # Estimate current amplitude from data
            estimated_amplitude = self._estimate_current_amplitude()
            # Use 10% of amplitude as threshold (works for most pulse shapes)
            current_threshold = estimated_amplitude * 0.1
            # For very low amplitudes, use a more conservative threshold
            # to avoid detecting noise as pulses
            if estimated_amplitude < 50e-6:  # Less than 50 µA
                current_threshold = estimated_amplitude * 0.3  # Use 30% for low signals
            # Ensure minimum threshold of 2 µA for very low currents
            current_threshold = max(current_threshold, 2e-6)
        else:
            current_threshold = min_pulse_current

        # Label cathodic and anodic regions
        cathodic = self.current < -current_threshold
        anodic = self.current > current_threshold

        # Find cathodic pulse starts
        cathodic_starts = np.where(np.diff(cathodic.astype(int)) == 1)[0] + 1
        cathodic_ends = np.where(np.diff(cathodic.astype(int)) == -1)[0] + 1

        pulses = []
        last_pulse_end = -1000  # Track last pulse to avoid overlaps

        for i, cat_start in enumerate(cathodic_starts):
            if i >= len(cathodic_ends):
                break

            cat_end = cathodic_ends[i]

            # Skip if this pulse starts too close to the last one
            if cat_start < last_pulse_end + 50:  # Minimum 50 samples between pulses
                continue

            # Filter out very short pulses (likely noise)
            # Expected pulse width is ~1ms, at 10kHz that's ~100 samples minimum
            min_phase_samples = 20  # Minimum samples for a valid phase
            cathodic_duration = cat_end - cat_start
            if cathodic_duration < min_phase_samples:
                continue  # Skip noise spikes

            # Look for anodic phase after cathodic
            search_start = cat_end
            search_end = min(len(self.time), cat_end + 1000)

            anodic_in_window = anodic[search_start:search_end]
            if np.any(anodic_in_window):
                # Find anodic phase boundaries
                local_anodic_starts = np.where(np.diff(anodic_in_window.astype(int)) == 1)[0]
                local_anodic_ends = np.where(np.diff(anodic_in_window.astype(int)) == -1)[0]

                if len(local_anodic_starts) > 0 and len(local_anodic_ends) > 0:
                    anodic_start = search_start + local_anodic_starts[0] + 1
                    anodic_end = search_start + local_anodic_ends[0] + 1

                    # Check anodic phase duration
                    anodic_duration = anodic_end - anodic_start
                    if anodic_duration < min_phase_samples:
                        continue  # Skip if anodic phase is too short

                    # Add padding to capture full pulse edges
                    pulse_start = max(0, cat_start - edge_padding)
                    pulse_end = min(len(self.time), anodic_end + edge_padding)

                    # Extract pulse data
                    pulse_time = self.time[pulse_start:pulse_end]
                    pulse_voltage = self.voltage[pulse_start:pulse_end]
                    pulse_current = self.current[pulse_start:pulse_end]

                    # Create masks relative to pulse
                    pulse_cathodic_mask = np.zeros(len(pulse_time), dtype=bool)
                    cat_mask_start = cat_start - pulse_start
                    cat_mask_end = cat_end - pulse_start
                    pulse_cathodic_mask[cat_mask_start:cat_mask_end] = True
                    pulse_cathodic_mask &= (pulse_current < -current_threshold)

                    pulse_anodic_mask = np.zeros(len(pulse_time), dtype=bool)
                    anod_mask_start = anodic_start - pulse_start
                    anod_mask_end = anodic_end - pulse_start
                    pulse_anodic_mask[anod_mask_start:anod_mask_end] = True
                    pulse_anodic_mask &= (pulse_current > current_threshold)

                    pulses.append({
                        'pulse_num': len(pulses) + 1,
                        'start_idx': pulse_start,
                        'end_idx': pulse_end,
                        'time': pulse_time,
                        'voltage': pulse_voltage,
                        'current': pulse_current,
                        'cathodic_mask': pulse_cathodic_mask,
                        'anodic_mask': pulse_anodic_mask
                    })

                    # Update last pulse end to avoid overlaps
                    last_pulse_end = pulse_end

        self.pulses = pulses
        return pulses

    def analyze_last_pulse(self, electrode_area: float,
                          eipp: Optional[float] = None) -> Dict:
        """
        Analyze the last pulse using Cogan voltage transient methodology.

        Args:
            electrode_area: Electrode geometric surface area in cm²
            eipp: Interpulse potential in V (if None, will estimate)

        Returns:
            Dictionary with all voltage transient parameters
        """
        if not self.pulses:
            self.detect_pulses()

        if len(self.pulses) == 0:
            raise ValueError("No pulses detected in data")

        # Get last pulse (steady-state)
        last_pulse = self.pulses[-1]
        self.last_pulse_idx = len(self.pulses) - 1

        time = last_pulse['time']
        voltage = last_pulse['voltage']
        current = last_pulse['current']
        cathodic_mask = last_pulse['cathodic_mask']
        anodic_mask = last_pulse['anodic_mask']

        # 1. Estimate interpulse potential (Eipp)
        if eipp is None:
            pre_pulse_idx = max(0, last_pulse['start_idx'] - 10)
            eipp = np.mean(self.voltage[pre_pulse_idx:last_pulse['start_idx']])

        # 2. Find cathodic phase boundaries
        cathodic_indices = np.where(cathodic_mask)[0]
        if len(cathodic_indices) > 0:
            cathodic_start = cathodic_indices[0]
            cathodic_end = cathodic_indices[-1]

            # 3. Find maximum cathodic polarization (Emc)
            emc = np.min(voltage[cathodic_mask])
            v_cathodic_end = voltage[cathodic_end]

            # 4. Measure access voltage (Va) from instantaneous iR drop
            va_cathodic = self._interpolate_access_voltage(
                time, voltage, current, cathodic_start, cathodic_end
            )

            # 5. Calculate electrode polarization (Ep)
            # For cathodic phase: add |Va| to get interface potential
            e_interface_cathodic = emc + abs(va_cathodic)
            ep_cathodic = e_interface_cathodic - eipp

            # 6. Measure equilibrium potential (Eo)
            eo = self._measure_equilibrium_potential(
                time, voltage, current, cathodic_end
            )

            # 7. Calculate activation overpotential (ηa)
            eta_a_cathodic = ep_cathodic - (eo - eipp)

            # 8. Estimate concentration overpotential (ηc)
            # For cathodic phase, the concentration overpotential calculation also needs sign correction
            eta_c_cathodic = v_cathodic_end - eo + abs(va_cathodic)

            # 9. Calculate charge injected
            q_cathodic = trapezoid(
                -current[cathodic_mask],
                time[cathodic_mask]
            )
            cic_cathodic = (q_cathodic / electrode_area) * 1000  # mC/cm²

        else:
            va_cathodic = emc = eo = eta_a_cathodic = eta_c_cathodic = np.nan
            q_cathodic = cic_cathodic = 0

        # 10. Analyze anodic phase
        anodic_indices = np.where(anodic_mask)[0]
        if len(anodic_indices) > 0:
            anodic_start = anodic_indices[0]
            anodic_end = anodic_indices[-1]

            ema = np.max(voltage[anodic_mask])
            v_anodic_end = voltage[anodic_end]

            va_anodic = self._interpolate_access_voltage(
                time, voltage, current, anodic_start, anodic_end
            )

            ep_anodic = ema - eipp - va_anodic
            eta_a_anodic = ep_anodic
            eta_c_anodic = v_anodic_end - eipp - va_anodic

            q_anodic = trapezoid(
                current[anodic_mask],
                time[anodic_mask]
            )
            cic_anodic = (q_anodic / electrode_area) * 1000
        else:
            va_anodic = ema = ep_anodic = eta_a_anodic = eta_c_anodic = np.nan
            q_anodic = cic_anodic = 0

        # Store results
        self.analysis_results = {
            'pulse_number': len(self.pulses),
            'electrode_area_cm2': electrode_area,
            'E_ipp': eipp,
            'V_a_cathodic': va_cathodic,
            'E_mc': emc,
            'E_o': eo,
            'E_p_cathodic': ep_cathodic,
            'eta_a_cathodic': eta_a_cathodic,
            'eta_c_cathodic': eta_c_cathodic,
            'Q_cathodic_C': q_cathodic,
            'CIC_cathodic_mC_cm2': cic_cathodic,
            'V_a_anodic': va_anodic,
            'E_ma': ema,
            'E_p_anodic': ep_anodic,
            'eta_a_anodic': eta_a_anodic,
            'eta_c_anodic': eta_c_anodic,
            'Q_anodic_C': q_anodic,
            'CIC_anodic_mC_cm2': cic_anodic,
            'safe_cathodic': emc >= self.e_safe_cath,
            'safe_anodic': ema <= self.e_safe_an
        }

        return self.analysis_results

    def analyze_all_pulses(self, electrode_area: float,
                          eipp: Optional[float] = None) -> list:
        """
        Analyze all pulses in the dataset to determine CIC limits.

        Args:
            electrode_area: Electrode geometric surface area in cm²
            eipp: Interpulse potential in V (if None, will estimate)

        Returns:
            List of analysis results for each pulse
        """
        if not self.pulses:
            self.detect_pulses()

        if len(self.pulses) == 0:
            raise ValueError("No pulses detected in data")

        self.all_pulse_results = []

        # Analyze each pulse
        for idx in range(len(self.pulses)):
            self.last_pulse_idx = idx
            pulse = self.pulses[idx]

            time = pulse['time']
            voltage = pulse['voltage']
            current = pulse['current']
            cathodic_mask = pulse['cathodic_mask']
            anodic_mask = pulse['anodic_mask']

            # Estimate interpulse potential
            if eipp is None:
                pre_pulse_idx = max(0, pulse['start_idx'] - 10)
                eipp_pulse = np.mean(self.voltage[pre_pulse_idx:pulse['start_idx']])
            else:
                eipp_pulse = eipp

            # Cathodic analysis
            cathodic_indices = np.where(cathodic_mask)[0]
            if len(cathodic_indices) > 0:
                cathodic_start = cathodic_indices[0]
                cathodic_end = cathodic_indices[-1]

                emc = np.min(voltage[cathodic_mask])
                v_cathodic_end = voltage[cathodic_end]

                va_cathodic = self._interpolate_access_voltage(
                    time, voltage, current, cathodic_start, cathodic_end
                )

                # For cathodic phase: add |Va| to get interface potential
                e_interface_cathodic = emc + abs(va_cathodic)
                ep_cathodic = e_interface_cathodic - eipp_pulse

                eo = self._measure_equilibrium_potential(
                    time, voltage, current, cathodic_end
                )

                eta_a_cathodic = ep_cathodic - (eo - eipp_pulse)
                # For cathodic phase, the concentration overpotential calculation also needs sign correction
                eta_c_cathodic = v_cathodic_end - eo + abs(va_cathodic)

                q_cathodic = trapezoid(
                    -current[cathodic_mask],
                    time[cathodic_mask]
                )
                cic_cathodic = (q_cathodic / electrode_area) * 1000
            else:
                va_cathodic = emc = eo = ep_cathodic = eta_a_cathodic = eta_c_cathodic = np.nan
                q_cathodic = cic_cathodic = 0

            # Anodic analysis
            anodic_indices = np.where(anodic_mask)[0]
            if len(anodic_indices) > 0:
                anodic_start = anodic_indices[0]
                anodic_end = anodic_indices[-1]

                ema = np.max(voltage[anodic_mask])
                v_anodic_end = voltage[anodic_end]

                va_anodic = self._interpolate_access_voltage(
                    time, voltage, current, anodic_start, anodic_end
                )

                ep_anodic = ema - eipp_pulse - va_anodic
                eta_a_anodic = ep_anodic
                eta_c_anodic = v_anodic_end - eipp_pulse - va_anodic

                q_anodic = trapezoid(
                    current[anodic_mask],
                    time[anodic_mask]
                )
                cic_anodic = (q_anodic / electrode_area) * 1000
            else:
                va_anodic = ema = ep_anodic = eta_a_anodic = eta_c_anodic = np.nan
                q_anodic = cic_anodic = 0

            # Store results
            pulse_results = {
                'pulse_number': idx + 1,
                'electrode_area_cm2': electrode_area,
                'E_ipp': eipp_pulse,
                'V_a_cathodic': va_cathodic,
                'E_mc': emc,
                'E_o': eo,
                'E_p_cathodic': ep_cathodic,
                'eta_a_cathodic': eta_a_cathodic,
                'eta_c_cathodic': eta_c_cathodic,
                'Q_cathodic_C': q_cathodic,
                'CIC_cathodic_mC_cm2': cic_cathodic,
                'V_a_anodic': va_anodic,
                'E_ma': ema,
                'E_p_anodic': ep_anodic,
                'eta_a_anodic': eta_a_anodic,
                'eta_c_anodic': eta_c_anodic,
                'Q_anodic_C': q_anodic,
                'CIC_anodic_mC_cm2': cic_anodic,
                'safe_cathodic': emc >= self.e_safe_cath,
                'safe_anodic': ema <= self.e_safe_an,
                'safe': (emc >= self.e_safe_cath) and (ema <= self.e_safe_an)
            }

            self.all_pulse_results.append(pulse_results)

        return self.all_pulse_results

    def determine_cic(self, electrode_area: float,
                     current_levels: Optional[np.ndarray] = None,
                     pulse_width: Optional[float] = None) -> Dict:
        """
        Determine CIC by finding the maximum safe charge.

        If multiple current levels are tested, interpolates between the last
        safe sweep and first unsafe sweep to find the threshold.

        Args:
            electrode_area: Electrode geometric surface area in cm²
            current_levels: Array of current levels tested (A). If None, assumes single level.
            pulse_width: Pulse width in seconds. If None, will estimate from data.

        Returns:
            Dictionary with CIC results
        """
        # Analyze all pulses
        if not self.all_pulse_results:
            self.analyze_all_pulses(electrode_area)

        # Find last safe pulse
        safe_pulses = [p for p in self.all_pulse_results if p['safe']]
        unsafe_pulses = [p for p in self.all_pulse_results if not p['safe']]

        if len(safe_pulses) == 0:
            raise ValueError("No safe pulses found! All pulses violate safety limits.")

        # Get the last safe pulse
        last_safe = safe_pulses[-1]

        # If all pulses are safe, CIC is at least the last measured value
        if len(unsafe_pulses) == 0:
            return {
                'CIC_cathodic_mC_cm2': last_safe['CIC_cathodic_mC_cm2'],
                'CIC_anodic_mC_cm2': last_safe['CIC_anodic_mC_cm2'],
                'Q_cathodic_C': last_safe['Q_cathodic_C'],
                'Q_anodic_C': last_safe['Q_anodic_C'],
                'interpolated': False,
                'note': 'All tested pulses are safe. CIC is at least this value.'
            }

        # Find first unsafe pulse
        first_unsafe = unsafe_pulses[0]

        # Check if there's a gap (discontinuous current levels)
        if first_unsafe['pulse_number'] != last_safe['pulse_number'] + 1:
            return {
                'CIC_cathodic_mC_cm2': last_safe['CIC_cathodic_mC_cm2'],
                'CIC_anodic_mC_cm2': last_safe['CIC_anodic_mC_cm2'],
                'Q_cathodic_C': last_safe['Q_cathodic_C'],
                'Q_anodic_C': last_safe['Q_anodic_C'],
                'interpolated': False,
                'note': 'Non-consecutive safe/unsafe pulses. Using last safe value.'
            }

        # Interpolate to find threshold
        violates_cathodic = first_unsafe['E_mc'] < self.e_safe_cath
        violates_anodic = first_unsafe['E_ma'] > self.e_safe_an

        if violates_cathodic:
            # Interpolate based on E_mc
            e_limit = self.e_safe_cath
            e_0 = last_safe['E_mc']
            e_1 = first_unsafe['E_mc']
            q_0 = last_safe['Q_cathodic_C']
            q_1 = first_unsafe['Q_cathodic_C']

            if abs(e_1 - e_0) < 1e-12:
                # Identical voltages - use last safe value instead of interpolating
                q_star = q_0
            else:
                q_star = q_0 + (e_limit - e_0) / (e_1 - e_0) * (q_1 - q_0)
            cic_star = (q_star / electrode_area) * 1000

            return {
                'CIC_cathodic_mC_cm2': cic_star,
                'CIC_anodic_mC_cm2': last_safe['CIC_anodic_mC_cm2'],
                'Q_cathodic_C': q_star,
                'Q_anodic_C': last_safe['Q_anodic_C'],
                'interpolated': True,
                'limiting_phase': 'cathodic',
                'E_limit': e_limit,
                'last_safe_E_mc': e_0,
                'first_unsafe_E_mc': e_1,
                'note': f'Interpolated between pulses {last_safe["pulse_number"]} and {first_unsafe["pulse_number"]}'
            }
        elif violates_anodic:
            # Interpolate based on E_ma
            e_limit = self.e_safe_an
            e_0 = last_safe['E_ma']
            e_1 = first_unsafe['E_ma']
            q_0 = last_safe['Q_anodic_C']
            q_1 = first_unsafe['Q_anodic_C']

            if abs(e_1 - e_0) < 1e-12:
                # Identical voltages - use last safe value instead of interpolating
                q_star = q_0
            else:
                q_star = q_0 + (e_limit - e_0) / (e_1 - e_0) * (q_1 - q_0)
            cic_star = (q_star / electrode_area) * 1000

            return {
                'CIC_cathodic_mC_cm2': last_safe['CIC_cathodic_mC_cm2'],
                'CIC_anodic_mC_cm2': cic_star,
                'Q_cathodic_C': last_safe['Q_cathodic_C'],
                'Q_anodic_C': q_star,
                'interpolated': True,
                'limiting_phase': 'anodic',
                'E_limit': e_limit,
                'last_safe_E_ma': e_0,
                'first_unsafe_E_ma': e_1,
                'note': f'Interpolated between pulses {last_safe["pulse_number"]} and {first_unsafe["pulse_number"]}'
            }

    def _interpolate_access_voltage(self, time: np.ndarray,
                                    voltage: np.ndarray,
                                    current: np.ndarray,
                                    pulse_start_idx: int,
                                    pulse_end_idx: int) -> float:
        """
        Measure access voltage (Va) from instantaneous iR drop.

        Per Cogan 2008: Va is the instantaneous voltage drop due to
        solution resistance when current turns on/off.

        Args:
            time: Time array
            voltage: Voltage array
            current: Current array
            pulse_start_idx: Index where pulse current starts
            pulse_end_idx: Index where pulse current ends

        Returns:
            Access voltage Va in V (magnitude of iR drop)
        """
        # Measure voltage BEFORE pulse starts
        pre_pulse_window = max(0, pulse_start_idx - 10)
        v_before = np.mean(voltage[pre_pulse_window:pulse_start_idx])

        # Measure voltage AFTER the instantaneous iR drop settles
        settle_start = pulse_start_idx + 2
        settle_end = min(pulse_start_idx + 10, pulse_end_idx)
        v_after = np.mean(voltage[settle_start:settle_end])

        # Va is the magnitude of the instantaneous voltage step
        va = abs(v_after - v_before)

        return va

    def _measure_equilibrium_potential(self, time: np.ndarray,
                                       voltage: np.ndarray,
                                       current: np.ndarray,
                                       cathodic_end_idx: int) -> float:
        """
        Measure equilibrium potential (Eo) in interphase period.

        Per Cogan: measure ~1.1 ms after pulse end, before anodic recharge.

        Args:
            time: Time array
            voltage: Voltage array
            current: Current array
            cathodic_end_idx: Index where cathodic phase ends

        Returns:
            Equilibrium potential Eo in V
        """
        # Find interphase period (i≈0)
        current_threshold = np.std(current) * 0.05

        # Look for quiet period after cathodic phase
        search_start = cathodic_end_idx + 1
        search_end = min(len(current), cathodic_end_idx + 50)

        interphase_mask = np.abs(current[search_start:search_end]) < current_threshold

        if np.any(interphase_mask):
            interphase_indices = np.where(interphase_mask)[0] + search_start
            mid_idx = interphase_indices[len(interphase_indices)//2]
            eo = voltage[mid_idx]
        else:
            eo = voltage[min(cathodic_end_idx + 1, len(voltage)-1)]

        return eo

    def plot_voltage_transient(self, save_path: Optional[str] = None) -> plt.Figure:
        """
        Plot voltage transient with minimal annotations for SVG export.

        Creates a clean plot suitable for post-processing in vector editors.
        Includes voltage trace and shaded phase regions only.

        Args:
            save_path: Optional path to save figure

        Returns:
            matplotlib Figure object
        """
        if not self.analysis_results:
            raise ValueError("Run analyze_last_pulse() first")

        last_pulse = self.pulses[self.last_pulse_idx]
        time = last_pulse['time'] * 1000  # Convert to ms
        voltage = last_pulse['voltage']

        fig, ax = plt.subplots(figsize=(10, 6))

        # Plot voltage transient
        ax.plot(time, voltage, 'k-', linewidth=1.5, label='Voltage')

        cathodic_mask = last_pulse['cathodic_mask']
        anodic_mask = last_pulse['anodic_mask']

        # Shade phases
        if np.any(cathodic_mask):
            ax.fill_between(time, voltage.min()-0.1, voltage.max()+0.1,
                           where=cathodic_mask, alpha=0.2, color='blue',
                           label='Cathodic phase')
        if np.any(anodic_mask):
            ax.fill_between(time, voltage.min()-0.1, voltage.max()+0.1,
                           where=anodic_mask, alpha=0.2, color='red',
                           label='Anodic phase')

        ax.set_xlabel('Time (ms)', fontsize=11)
        ax.set_ylabel('Potential (V vs Ag|AgCl)', fontsize=11)
        ax.set_title('Voltage Transient - CIC Analysis', fontsize=12)
        ax.legend(loc='best', fontsize=9)
        ax.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')

        return fig

    def plot_last_pulse_annotated(self, save_path: Optional[str] = None) -> plt.Figure:
        """
        Plot last pulse with detailed Cogan-style voltage transient annotations.

        Creates figure showing:
        - Voltage transient with marked Eipp, Emc, Ema, Eo, Va
        - Current waveform
        - Shaded cathodic/anodic regions
        - Arrows and measurements for voltage components

        Args:
            save_path: Optional path to save figure

        Returns:
            matplotlib Figure object
        """
        if not self.analysis_results:
            raise ValueError("Run analyze_last_pulse() first")

        last_pulse = self.pulses[self.last_pulse_idx]
        time = last_pulse['time'] * 1000  # Convert to ms
        voltage = last_pulse['voltage']
        current = last_pulse['current'] * 1e6  # Convert to µA

        # Validate pulse data
        if len(time) < 10:
            raise ValueError("Pulse data too short for annotation")

        # Check for valid voltage variation
        v_range = np.ptp(voltage)  # peak-to-peak
        if v_range < 0.01:  # Less than 10 mV variation
            # Create simple plot without annotations for malformed pulses
            fig, ax = plt.subplots(figsize=(10, 6))
            ax.plot(time, voltage, 'b-', linewidth=1.5)
            ax.set_xlabel('Time (ms)')
            ax.set_ylabel('Voltage (V)')
            ax.set_title('Pulse Data (Unable to annotate - insufficient voltage variation)')
            ax.grid(True, alpha=0.3)
            return fig

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

        # Plot voltage transient
        ax1.plot(time, voltage, 'b-', linewidth=2, label='Voltage', zorder=3)

        r = self.analysis_results
        cathodic_mask = last_pulse['cathodic_mask']
        anodic_mask = last_pulse['anodic_mask']

        # Find key time points
        if np.any(cathodic_mask):
            cat_start_idx = np.where(cathodic_mask)[0][0]
            cat_end_idx = np.where(cathodic_mask)[0][-1]
            t_cat_start = time[cat_start_idx]
            t_cat_end = time[cat_end_idx]

        if np.any(anodic_mask):
            anod_start_idx = np.where(anodic_mask)[0][0]
            anod_end_idx = np.where(anodic_mask)[0][-1]
            t_anod_start = time[anod_start_idx]
            t_anod_end = time[anod_end_idx]

        # Shade regions
        if np.any(cathodic_mask):
            ax1.fill_between(time, voltage.min()-0.1, voltage.max()+0.1,
                            where=cathodic_mask, alpha=0.15, color='blue',
                            label='Cathodic phase', zorder=1)
        if np.any(anodic_mask):
            ax1.fill_between(time, voltage.min()-0.1, voltage.max()+0.1,
                            where=anodic_mask, alpha=0.15, color='red',
                            label='Anodic phase', zorder=1)

        # Reference lines
        ax1.axhline(r['E_ipp'], color='gray', linestyle='--', linewidth=1.5, alpha=0.7, zorder=2)
        ax1.axhline(r['E_o'], color='green', linestyle='--', linewidth=1.5, alpha=0.7, zorder=2)
        ax1.axhline(-0.6, color='red', linestyle=':', linewidth=1, alpha=0.4,
                   label='Water reduction limit', zorder=1)
        ax1.axhline(0.8, color='orange', linestyle=':', linewidth=1, alpha=0.4,
                   label='Water oxidation limit', zorder=1)

        # Annotations
        x_left = time[0] + (time[-1] - time[0]) * 0.05
        x_right = time[-1] - (time[-1] - time[0]) * 0.05

        ax1.text(x_left, r['E_ipp'] + 0.02, f"$E_{{ipp}}$ = {r['E_ipp']:.3f} V",
                fontsize=10, color='gray', verticalalignment='bottom',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='gray', alpha=0.8))

        ax1.text(x_right, r['E_o'] - 0.02, f"$E_o$ = {r['E_o']:.3f} V",
                fontsize=10, color='green', verticalalignment='top',
                bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='green', alpha=0.8))

        # Cathodic voltage components
        if np.any(cathodic_mask):
            emc_idx = np.argmin(np.abs(voltage - r['E_mc']))
            t_emc = time[emc_idx]

            v_after_va = r['E_ipp'] - r['V_a_cathodic']
            v_emc = r['E_mc']

            # Va arrow
            ax1.annotate('', xy=(t_emc, v_after_va), xytext=(t_emc, r['E_ipp']),
                        arrowprops=dict(arrowstyle='<->', color='purple', lw=2.5))
            ax1.text(t_emc + (time[-1]-time[0])*0.015, (r['E_ipp'] + v_after_va)/2,
                    f"$V_a$ = {r['V_a_cathodic']:.3f} V",
                    fontsize=9, color='purple', verticalalignment='center',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='purple', alpha=0.9))

            # Ep arrow
            ax1.annotate('', xy=(t_emc, v_emc), xytext=(t_emc, v_after_va),
                        arrowprops=dict(arrowstyle='<->', color='orange', lw=2.5))
            ax1.text(t_emc + (time[-1]-time[0])*0.015, (v_after_va + v_emc)/2,
                    f"$E_p$ = {r['E_p_cathodic']:.3f} V",
                    fontsize=9, color='orange', verticalalignment='center',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='orange', alpha=0.9))

            ax1.text(t_emc - (time[-1]-time[0])*0.02, r['E_mc'] - 0.05,
                    f"$E_{{mc}}$ = {r['E_mc']:.3f} V",
                    fontsize=10, color='red', verticalalignment='top',
                    horizontalalignment='right',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='red', alpha=0.9))

        # Anodic voltage components
        if np.any(anodic_mask) and not np.isnan(r['E_ma']):
            ema_idx = np.argmin(np.abs(voltage - r['E_ma']))
            t_ema = time[ema_idx]

            v_after_va_anodic = r['E_ipp'] + r['V_a_anodic']
            v_ema = r['E_ma']

            ax1.annotate('', xy=(t_ema, v_after_va_anodic), xytext=(t_ema, r['E_ipp']),
                        arrowprops=dict(arrowstyle='<->', color='purple', lw=2.5))
            ax1.text(t_ema + (time[-1]-time[0])*0.015, (r['E_ipp'] + v_after_va_anodic)/2,
                    f"$V_a$ = {r['V_a_anodic']:.3f} V",
                    fontsize=9, color='purple', verticalalignment='center',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='purple', alpha=0.9))

            ax1.annotate('', xy=(t_ema, v_ema), xytext=(t_ema, v_after_va_anodic),
                        arrowprops=dict(arrowstyle='<->', color='orange', lw=2.5))
            ax1.text(t_ema + (time[-1]-time[0])*0.015, (v_after_va_anodic + v_ema)/2,
                    f"$E_p$ = {r['E_p_anodic']:.3f} V",
                    fontsize=9, color='orange', verticalalignment='center',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='orange', alpha=0.9))

            ax1.text(t_ema - (time[-1]-time[0])*0.02, r['E_ma'] + 0.05,
                    f"$E_{{ma}}$ = {r['E_ma']:.3f} V",
                    fontsize=10, color='darkorange', verticalalignment='bottom',
                    horizontalalignment='right',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='darkorange', alpha=0.9))

        ax1.set_ylabel('Potential (V vs Ag|AgCl)', fontsize=11, fontweight='bold')
        ax1.set_title(f'Voltage Transient Analysis - Pulse {r["pulse_number"]} (Cogan Method)',
                     fontsize=12, fontweight='bold')
        ax1.legend(loc='right', fontsize=8)
        ax1.grid(True, alpha=0.3)
        ax1.set_ylim(voltage.min()-0.15, voltage.max()+0.15)

        # Plot current waveform
        ax2.plot(time, current, 'k-', linewidth=2, zorder=3)
        ax2.axhline(0, color='gray', linestyle='-', linewidth=0.5)

        if np.any(cathodic_mask):
            ax2.fill_between(time, 0, current,
                            where=cathodic_mask, alpha=0.15, color='blue',
                            label='Cathodic', zorder=1)
            i_c = current[cathodic_mask].mean()
            ax2.text(t_cat_start + (t_cat_end - t_cat_start)/2, i_c*1.1,
                    f"$i_c$ = {i_c:.1f} µA",
                    fontsize=9, color='blue', verticalalignment='bottom',
                    horizontalalignment='center',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='blue', alpha=0.8))

        if np.any(anodic_mask):
            ax2.fill_between(time, 0, current,
                            where=anodic_mask, alpha=0.15, color='red',
                            label='Anodic', zorder=1)
            i_a = current[anodic_mask].mean()
            ax2.text(t_anod_start + (t_anod_end - t_anod_start)/2, i_a*1.1,
                    f"$i_a$ = {i_a:.1f} µA",
                    fontsize=9, color='red', verticalalignment='bottom',
                    horizontalalignment='center',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='red', alpha=0.8))

        ax2.set_xlabel('Time (ms)', fontsize=11, fontweight='bold')
        ax2.set_ylabel('Current (µA)', fontsize=11, fontweight='bold')
        ax2.set_title('Current Waveform', fontsize=12, fontweight='bold')
        ax2.legend(loc='lower right', fontsize=8)
        ax2.grid(True, alpha=0.3)

        # Dynamic y-axis limits for current
        current_max = np.max(np.abs(current))
        y_limit = current_max * 1.3  # More padding for annotations
        ax2.set_ylim(-y_limit, y_limit)

        # Use try-except for tight_layout to handle annotation errors
        try:
            plt.tight_layout(pad=1.5)
        except (ValueError, StopIteration) as e:
            # Fallback if tight_layout fails with annotations
            fig.subplots_adjust(left=0.1, right=0.95, top=0.95, bottom=0.1, hspace=0.3)

        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"Saved figure to: {save_path}")

        return fig

    def print_results(self):
        """Print analysis results in readable format."""
        if not self.analysis_results:
            raise ValueError("Run analyze_last_pulse() first")

        r = self.analysis_results

        print("\n" + "="*60)
        print("COGAN VOLTAGE TRANSIENT ANALYSIS RESULTS")
        print("="*60)
        print(f"\nElectrode Area: {r['electrode_area_cm2']:.4f} cm²")
        print(f"Analyzed Pulse: {r['pulse_number']} (last pulse)")

        print("\n" + "-"*60)
        print("VOLTAGE PARAMETERS")
        print("-"*60)
        print(f"Interpulse potential (E_ipp):     {r['E_ipp']:>8.4f} V")
        print(f"Equilibrium potential (E_o):      {r['E_o']:>8.4f} V")

        print("\n--- Cathodic Phase ---")
        print(f"Access voltage (V_a):             {r['V_a_cathodic']:>8.4f} V")
        print(f"Electrode polarization (E_p):     {r['E_p_cathodic']:>8.4f} V")
        print(f"Max cathodic potential (E_mc):    {r['E_mc']:>8.4f} V")
        print(f"Activation overpotential (η_a):   {r['eta_a_cathodic']:>8.4f} V")
        print(f"Concentration overpotential (η_c):{r['eta_c_cathodic']:>8.4f} V")
        print(f"Safe (E_mc > -0.6 V):             {r['safe_cathodic']}")

        print("\n--- Anodic Phase ---")
        print(f"Access voltage (V_a):             {r['V_a_anodic']:>8.4f} V")
        print(f"Electrode polarization (E_p):     {r['E_p_anodic']:>8.4f} V")
        print(f"Max anodic potential (E_ma):      {r['E_ma']:>8.4f} V")
        print(f"Activation overpotential (η_a):   {r['eta_a_anodic']:>8.4f} V")
        print(f"Concentration overpotential (η_c):{r['eta_c_anodic']:>8.4f} V")
        print(f"Safe (E_ma < 0.8 V):              {r['safe_anodic']}")

        print("\n" + "-"*60)
        print("CHARGE INJECTION CAPACITY")
        print("-"*60)
        print(f"Cathodic charge (Q):              {r['Q_cathodic_C']*1e9:>8.2f} nC")
        print(f"Cathodic CIC:                     {r['CIC_cathodic_mC_cm2']:>8.2f} mC/cm²")
        print(f"\nAnodic charge (Q):                {r['Q_anodic_C']*1e9:>8.2f} nC")
        print(f"Anodic CIC:                       {r['CIC_anodic_mC_cm2']:>8.2f} mC/cm²")
        print("\n" + "="*60 + "\n")

    def get_results_dataframe(self) -> pd.DataFrame:
        """
        Export analysis results as a DataFrame.

        Returns:
            DataFrame with all voltage transient parameters
        """
        if not self.analysis_results:
            raise ValueError("Run analyze_last_pulse() first")

        return pd.DataFrame([self.analysis_results])

    def plot_complete_waveform(self, figsize: Tuple[float, float] = (14, 8),
                              title: Optional[str] = None,
                              save_path: Optional[str] = None) -> plt.Figure:
        """
        Plot the complete waveform showing all pulses with safety limits.

        Args:
            figsize: Figure size (width, height)
            title: Optional custom title
            save_path: Optional path to save the figure

        Returns:
            Matplotlib figure object
        """
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=figsize, sharex=True)

        # Get data - handle both raw and standardized column names
        if 'T' in self.data.columns:
            time_ms = self.data['T'].values * 1000  # Convert to ms
            voltage = self.data['Vf'].values
            current = self.data['Im'].values * 1e6  # Convert to µA
        else:
            time_ms = self.data['Time (s)'].values * 1000  # Convert to ms
            voltage = self.data['Potential (V)'].values
            current = self.data['Current (A)'].values * 1e6  # Convert to µA

        # Detect number of pulses if not already done
        if not hasattr(self, '_pulse_count'):
            pulses = self.detect_pulses(edge_padding=50)
            self._pulse_count = len(pulses)

        # Plot voltage
        ax1.plot(time_ms, voltage, 'b-', linewidth=0.5, alpha=0.8)
        ax1.axhline(0.366, color='gray', linestyle='--', alpha=0.5, label='Eipp')
        ax1.axhline(self.e_safe_cath, color='red', linestyle=':', alpha=0.4, linewidth=1, label='Water reduction')
        ax1.axhline(self.e_safe_an, color='orange', linestyle=':', alpha=0.4, linewidth=1, label='Water oxidation')
        ax1.set_ylabel('Voltage (V)', fontsize=11)
        ax1.set_title(title or f'Complete CIC Waveform - {self._pulse_count} Biphasic Pulses',
                      fontsize=12, fontweight='bold')
        ax1.grid(True, alpha=0.3)
        ax1.legend(loc='upper right', fontsize=9)
        ax1.set_ylim(-0.8, 1.0)

        # Plot current
        ax2.plot(time_ms, current, 'k-', linewidth=0.5, alpha=0.8)
        ax2.axhline(0, color='gray', linestyle='-', linewidth=0.5, alpha=0.5)
        ax2.set_xlabel('Time (ms)', fontsize=11)
        ax2.set_ylabel('Current (µA)', fontsize=11)
        ax2.grid(True, alpha=0.3)

        # Dynamic y-axis limits with 10% padding
        current_max = np.max(np.abs(current))
        y_limit = current_max * 1.1
        ax2.set_ylim(-y_limit, y_limit)

        # Use try-except for tight_layout
        try:
            plt.tight_layout()
        except (ValueError, StopIteration):
            fig.subplots_adjust(left=0.1, right=0.95, top=0.95, bottom=0.1, hspace=0.2)

        if save_path:
            fig.savefig(save_path, dpi=100, bbox_inches='tight')

        return fig

    def plot_pulse_comparison(self, first_pulse_idx: int = 0,
                             last_pulse_idx: Optional[int] = None,
                             figsize: Tuple[float, float] = (12, 8),
                             save_path: Optional[str] = None) -> plt.Figure:
        """
        Plot comparison between two pulses (default: first vs last).

        Args:
            first_pulse_idx: Index of first pulse to compare (default: 0)
            last_pulse_idx: Index of second pulse (default: last pulse)
            figsize: Figure size (width, height)
            save_path: Optional path to save the figure

        Returns:
            Matplotlib figure object
        """
        # Detect pulses if not already done
        if not hasattr(self, 'pulses') or self.pulses is None:
            self.pulses = self.detect_pulses(edge_padding=50)

        if len(self.pulses) < 2:
            raise ValueError(f"Need at least 2 pulses for comparison, found {len(self.pulses)}")

        # Get pulse indices
        if last_pulse_idx is None:
            last_pulse_idx = len(self.pulses) - 1

        first_pulse = self.pulses[first_pulse_idx]
        second_pulse = self.pulses[last_pulse_idx]

        # Create comparison plot
        fig, axes = plt.subplots(2, 2, figsize=figsize)

        # First pulse - voltage
        ax = axes[0, 0]
        time_first = (first_pulse['time'] - first_pulse['time'][0]) * 1000  # Relative time in ms
        ax.plot(time_first, first_pulse['voltage'], 'b-', linewidth=1.5)
        ax.axhline(0.366, color='gray', linestyle='--', alpha=0.3)
        ax.axhline(self.e_safe_cath, color='red', linestyle=':', alpha=0.3)
        ax.axhline(self.e_safe_an, color='orange', linestyle=':', alpha=0.3)
        ax.set_ylabel('Voltage (V)', fontsize=10)
        ax.set_title(f'Pulse {first_pulse["pulse_num"]} (Initial)', fontsize=11, fontweight='bold')
        ax.grid(True, alpha=0.3)
        ax.set_ylim(-0.8, 1.0)

        # Second pulse - voltage
        ax = axes[0, 1]
        time_last = (second_pulse['time'] - second_pulse['time'][0]) * 1000  # Relative time in ms
        ax.plot(time_last, second_pulse['voltage'], 'r-', linewidth=1.5)
        ax.axhline(0.366, color='gray', linestyle='--', alpha=0.3)
        ax.axhline(self.e_safe_cath, color='red', linestyle=':', alpha=0.3)
        ax.axhline(self.e_safe_an, color='orange', linestyle=':', alpha=0.3)
        ax.set_ylabel('Voltage (V)', fontsize=10)
        ax.set_title(f'Pulse {second_pulse["pulse_num"]} (Steady-state)', fontsize=11, fontweight='bold')
        ax.grid(True, alpha=0.3)
        ax.set_ylim(-0.8, 1.0)

        # First pulse - current
        ax = axes[1, 0]
        current_first = first_pulse['current'] * 1e6
        ax.plot(time_first, current_first, 'b-', linewidth=1.5)
        ax.axhline(0, color='gray', linestyle='-', alpha=0.3)
        ax.set_xlabel('Time (ms)', fontsize=10)
        ax.set_ylabel('Current (µA)', fontsize=10)
        ax.grid(True, alpha=0.3)

        # Dynamic y-axis for first pulse
        current_max_first = np.max(np.abs(current_first))
        y_limit_first = current_max_first * 1.1
        ax.set_ylim(-y_limit_first, y_limit_first)

        # Second pulse - current
        ax = axes[1, 1]
        current_second = second_pulse['current'] * 1e6
        ax.plot(time_last, current_second, 'r-', linewidth=1.5)
        ax.axhline(0, color='gray', linestyle='-', alpha=0.3)
        ax.set_xlabel('Time (ms)', fontsize=10)
        ax.set_ylabel('Current (µA)', fontsize=10)
        ax.grid(True, alpha=0.3)

        # Dynamic y-axis for second pulse
        current_max_second = np.max(np.abs(current_second))
        y_limit_second = current_max_second * 1.1
        ax.set_ylim(-y_limit_second, y_limit_second)

        plt.suptitle('Pulse Comparison', fontsize=12, fontweight='bold', y=1.02)
        # Use try-except for tight_layout
        try:
            plt.tight_layout()
        except (ValueError, StopIteration):
            fig.subplots_adjust(left=0.1, right=0.95, top=0.95, bottom=0.1, hspace=0.2)

        if save_path:
            fig.savefig(save_path, dpi=100, bbox_inches='tight')

        return fig

    @staticmethod
    def batch_analyze(data_dict: dict, electrode_area: float,
                     export_manager=None) -> Tuple[pd.DataFrame, dict]:
        """
        Batch analyze multiple CIC measurements at different current amplitudes.

        Args:
            data_dict: Dictionary of {filename: DataFrame} with CIC data
            electrode_area: Electrode area in cm²
            export_manager: Optional ExportManager for organized export

        Returns:
            Tuple of (summary_df, figures_dict) where:
                summary_df: DataFrame with all trial results
                figures_dict: Dictionary of {filename: {plot_type: figure}}
        """
        results_list = []
        figures_dict = {}

        for filename, data in data_dict.items():
            print(f"\nAnalyzing {filename}...")

            try:
                # Create analyzer
                analyzer = CoganCICAnalyzer(data)

                # Detect pulses
                pulses = analyzer.detect_pulses(edge_padding=50)
                print(f"  Detected {len(pulses)} pulses")

                if len(pulses) == 0:
                    print(f"  ⚠ No pulses detected, skipping")
                    continue

                # Analyze last pulse
                results = analyzer.analyze_last_pulse(electrode_area=electrode_area)

                # Extract current amplitude from results or filename
                # Try to parse from filename first (e.g., "chip-100ua1ms.DTA" -> 100)
                import re
                match = re.search(r'(\d+)ua', filename.lower())
                if match:
                    current_ua = float(match.group(1))
                else:
                    # Fall back to measured current
                    current_ua = np.max(np.abs(analyzer.current)) * 1e6

                # Add metadata
                results['filename'] = filename
                results['current_amplitude_ua'] = current_ua
                results['num_pulses'] = len(pulses)

                # Calculate interface voltage (for interpolation)
                results['E_interface_cathodic'] = results['E_mc'] + abs(results['V_a_cathodic'])

                # Extract pulse width (assuming 1ms from filename or measure)
                match = re.search(r'(\d+)ms', filename.lower())
                if match:
                    pulse_width_ms = float(match.group(1))
                else:
                    # Measure from data
                    last_pulse = pulses[-1]
                    cathodic_mask = last_pulse['cathodic_mask']
                    if np.any(cathodic_mask):
                        pulse_width_ms = np.sum(cathodic_mask) * np.mean(np.diff(last_pulse['time'])) * 1000
                    else:
                        pulse_width_ms = 1.0  # Default

                results['pulse_width_ms'] = pulse_width_ms
                results['charge_injected_nC'] = results['Q_cathodic_C'] * 1e9

                results_list.append(results)

                # Generate figures
                figures = {}

                # Voltage transient plot
                try:
                    fig = analyzer.plot_voltage_transient()
                    figures['voltage_transient'] = fig
                    plt.close(fig)
                except Exception as e:
                    print(f"  ⚠ Failed to create voltage transient plot: {e}")

                # Annotated plot
                try:
                    fig = analyzer.plot_last_pulse_annotated()
                    figures['annotated'] = fig
                    plt.close(fig)
                except Exception as e:
                    print(f"  ⚠ Failed to create annotated plot: {e}")

                # Pulse comparison
                if len(pulses) >= 2:
                    try:
                        fig = analyzer.plot_pulse_comparison()
                        figures['pulse_comparison'] = fig
                        plt.close(fig)
                    except Exception as e:
                        print(f"  ⚠ Failed to create pulse comparison: {e}")

                figures_dict[filename] = figures

                # Save plots if export manager provided
                if export_manager:
                    # Create subfolder for this trial
                    trial_name = filename.replace('.DTA', '').replace('.dta', '')
                    for plot_name, fig in figures.items():
                        # save_figure already adds "plots/" subdirectory
                        save_path = f"{trial_name}/{plot_name}"
                        export_manager.save_figure(fig, save_path, save_svg=True)

                print(f"  ✓ Analysis complete: CIC = {results['CIC_cathodic_mC_cm2']:.2f} mC/cm²")

            except Exception as e:
                print(f"  ✗ Analysis failed: {e}")
                continue

        # Create summary DataFrame
        if results_list:
            summary_df = pd.DataFrame(results_list)
            # Sort by current amplitude for better visualization
            summary_df = summary_df.sort_values('current_amplitude_ua')
        else:
            summary_df = pd.DataFrame()

        return summary_df, figures_dict

    @staticmethod
    def interpolate_cic_at_safety_limit(current_amplitudes: np.ndarray,
                                       interface_voltages: np.ndarray,
                                       pulse_width_ms: float,
                                       electrode_area: float,
                                       e_safe_cath: float = -0.6) -> dict:
        """
        Interpolate CIC at the cathodic safety limit.

        NOTE: This method assumes all measurements use the same pulse width.
        For mixed pulse widths (e.g., 300µs and 1ms data), use
        interpolate_charge_at_safety_limit() instead, which directly
        interpolates charge values.

        Uses linear interpolation to find the maximum current that keeps
        the interface voltage above the safety limit.

        Args:
            current_amplitudes: Array of current amplitudes (µA)
            interface_voltages: Array of interface cathodic voltages (V)
            pulse_width_ms: Pulse width in milliseconds
            electrode_area: Electrode area in cm²
            e_safe_cath: Cathodic safety limit (default -0.6V)

        Returns:
            Dictionary with interpolation results:
                - interpolated_current_ua: Maximum safe current
                - interpolated_charge_nC: Charge at safe current
                - interpolated_cic_mC_cm2: CIC at safety limit
                - safety_margin_V: Distance from nearest measurement to limit
        """
        # Sort by interface voltage for interpolation
        sort_idx = np.argsort(interface_voltages)
        e_int_sorted = interface_voltages[sort_idx]
        i_sorted = current_amplitudes[sort_idx]

        # Find points that bracket the safety limit
        below_limit = e_int_sorted < e_safe_cath
        above_limit = e_int_sorted >= e_safe_cath

        if not any(below_limit) or not any(above_limit):
            # All points on one side - extrapolate from two closest points
            if all(e_int_sorted >= e_safe_cath):
                # All safe - extrapolate from two lowest voltage points
                print("Warning: All measurements safe, extrapolating to safety limit")
                if len(e_int_sorted) >= 2:
                    # Use two points closest to safety limit
                    idx1, idx2 = 0, 1  # Already sorted ascending
                    e1, e2 = e_int_sorted[idx1], e_int_sorted[idx2]
                    i1, i2 = i_sorted[idx1], i_sorted[idx2]

                    # Linear extrapolation (guard against identical voltages)
                    if abs(e2 - e1) < 1e-12:
                        i_safe = i1
                    else:
                        slope = (i2 - i1) / (e2 - e1)
                        i_safe = i1 + slope * (e_safe_cath - e1)
                    is_extrapolated = True
                else:
                    # Only one point - can't extrapolate
                    i_safe = i_sorted[0]
                    is_extrapolated = False
                margin = np.min(e_int_sorted) - e_safe_cath
            else:
                # All unsafe - extrapolate from two highest voltage points
                print("Warning: All measurements violate safety limit, extrapolating to safety limit")
                if len(e_int_sorted) >= 2:
                    # Use two points closest to safety limit
                    idx1, idx2 = -1, -2  # Two highest voltages (closest to safety)
                    e1, e2 = e_int_sorted[idx1], e_int_sorted[idx2]
                    i1, i2 = i_sorted[idx1], i_sorted[idx2]

                    # Linear extrapolation (guard against identical voltages)
                    if abs(e1 - e2) < 1e-12:
                        i_safe = i1
                    else:
                        slope = (i1 - i2) / (e1 - e2)
                        i_safe = i1 + slope * (e_safe_cath - e1)
                    is_extrapolated = True
                else:
                    # Only one point - can't extrapolate
                    i_safe = i_sorted[0]
                    is_extrapolated = False
                margin = np.max(e_int_sorted) - e_safe_cath
        else:
            # Find bracketing points
            idx_below = np.where(below_limit)[0][-1]  # Last point below limit
            idx_above = np.where(above_limit)[0][0]   # First point above limit

            e_below = e_int_sorted[idx_below]
            e_above = e_int_sorted[idx_above]
            i_below = i_sorted[idx_below]
            i_above = i_sorted[idx_above]

            # Linear interpolation (guard against identical voltages)
            if abs(e_below - e_above) < 1e-12:
                i_safe = (i_below + i_above) / 2
            else:
                slope = (i_below - i_above) / (e_below - e_above)
                i_safe = i_above + slope * (e_safe_cath - e_above)
            is_extrapolated = False

            margin = min(abs(e_above - e_safe_cath), abs(e_below - e_safe_cath))

        # Calculate charge and CIC
        charge_nC = i_safe * pulse_width_ms  # µA * ms = nC
        charge_C = charge_nC * 1e-9
        cic_mC_cm2 = (charge_C / electrode_area) * 1000

        result = {
            'interpolated_current_ua': i_safe,
            'interpolated_charge_nC': charge_nC,
            'interpolated_cic_mC_cm2': cic_mC_cm2,
            'safety_margin_V': margin,
            'e_safe_used': e_safe_cath,
            'is_extrapolated': is_extrapolated
        }

        # Add bracketing info if interpolation was performed
        if any(below_limit) and any(above_limit):
            result['current_below'] = i_below
            result['current_above'] = i_above
            result['voltage_below'] = e_below
            result['voltage_above'] = e_above

        return result

    @staticmethod
    def interpolate_charge_at_safety_limit(charges_nC: np.ndarray,
                                           interface_voltages: np.ndarray,
                                           electrode_area: float,
                                           e_safe_cath: float = -0.6) -> dict:
        """
        Interpolate maximum safe charge at the cathodic safety limit.

        Works with mixed pulse widths by interpolating charge directly instead of current.
        Use this method when analyzing data with different pulse widths.

        Args:
            charges_nC: Array of charge per phase (nC)
            interface_voltages: Array of interface cathodic voltages (V)
            electrode_area: Electrode area in cm²
            e_safe_cath: Cathodic safety limit (default -0.6V)

        Returns:
            Dictionary with interpolation results:
                - interpolated_charge_nC: Maximum safe charge
                - interpolated_cic_mC_cm2: CIC at safety limit
                - safety_margin_V: Distance from nearest measurement to limit
                - bracketing information if available
        """
        # Sort by interface voltage for interpolation
        sort_idx = np.argsort(interface_voltages)
        e_int_sorted = interface_voltages[sort_idx]
        q_sorted = charges_nC[sort_idx]

        # Find points that bracket the safety limit
        below_limit = e_int_sorted < e_safe_cath
        above_limit = e_int_sorted >= e_safe_cath

        if not any(below_limit) or not any(above_limit):
            # All points on one side - extrapolate from two closest points
            if all(e_int_sorted >= e_safe_cath):
                # All safe - extrapolate from two lowest voltage points
                print("Warning: All measurements safe, extrapolating to safety limit")
                if len(e_int_sorted) >= 2:
                    # Use two points closest to safety limit
                    idx1, idx2 = 0, 1  # Already sorted ascending
                    e1, e2 = e_int_sorted[idx1], e_int_sorted[idx2]
                    q1, q2 = q_sorted[idx1], q_sorted[idx2]

                    # Linear extrapolation (guard against identical voltages)
                    if abs(e2 - e1) < 1e-12:
                        q_safe = q1
                    else:
                        slope = (q2 - q1) / (e2 - e1)
                        q_safe = q1 + slope * (e_safe_cath - e1)
                    is_extrapolated = True
                else:
                    # Only one point - can't extrapolate
                    q_safe = q_sorted[0]
                    is_extrapolated = False
                margin = np.min(e_int_sorted) - e_safe_cath
            else:
                # All unsafe - extrapolate from two highest voltage points
                print("Warning: All measurements violate safety limit, extrapolating to safety limit")
                if len(e_int_sorted) >= 2:
                    # Use two points closest to safety limit
                    idx1, idx2 = -1, -2  # Two highest voltages (closest to safety)
                    e1, e2 = e_int_sorted[idx1], e_int_sorted[idx2]
                    q1, q2 = q_sorted[idx1], q_sorted[idx2]

                    # Linear extrapolation (guard against identical voltages)
                    if abs(e1 - e2) < 1e-12:
                        q_safe = q1
                    else:
                        slope = (q1 - q2) / (e1 - e2)
                        q_safe = q1 + slope * (e_safe_cath - e1)
                    is_extrapolated = True
                else:
                    # Only one point - can't extrapolate
                    q_safe = q_sorted[0]
                    is_extrapolated = False
                margin = np.max(e_int_sorted) - e_safe_cath
        else:
            # Find bracketing points
            idx_below = np.where(below_limit)[0][-1]  # Last point below limit
            idx_above = np.where(above_limit)[0][0]   # First point above limit

            e_below = e_int_sorted[idx_below]
            e_above = e_int_sorted[idx_above]
            q_below = q_sorted[idx_below]
            q_above = q_sorted[idx_above]

            # Linear interpolation (guard against identical voltages)
            if abs(e_below - e_above) < 1e-12:
                q_safe = (q_below + q_above) / 2
            else:
                slope = (q_below - q_above) / (e_below - e_above)
                q_safe = q_above + slope * (e_safe_cath - e_above)
            is_extrapolated = False

            margin = min(abs(e_above - e_safe_cath), abs(e_below - e_safe_cath))

        # Calculate CIC from charge
        charge_C = q_safe * 1e-9
        cic_mC_cm2 = (charge_C / electrode_area) * 1000

        result = {
            'interpolated_charge_nC': q_safe,
            'interpolated_cic_mC_cm2': cic_mC_cm2,
            'safety_margin_V': margin,
            'e_safe_used': e_safe_cath,
            'is_extrapolated': is_extrapolated
        }

        # Add bracketing info if interpolation was performed
        if any(below_limit) and any(above_limit):
            result['charge_below'] = q_below
            result['charge_above'] = q_above
            result['voltage_below'] = e_below
            result['voltage_above'] = e_above

        return result

    @staticmethod
    def plot_charge_vs_interface_voltage(results_df: pd.DataFrame,
                                        interpolation: dict = None,
                                        e_safe_cath: float = -0.6,
                                        show_pulse_widths: bool = False,
                                        save_path: Optional[str] = None) -> plt.Figure:
        """
        Plot charge injected vs interface cathodic voltage with safety limit.

        Args:
            results_df: DataFrame from batch_analyze with columns:
                       'E_interface_cathodic', 'charge_injected_nC', 'current_amplitude_ua'
            interpolation: Optional dict from interpolate_cic_at_safety_limit or
                          interpolate_charge_at_safety_limit
            e_safe_cath: Cathodic safety limit to display
            show_pulse_widths: If True, show pulse widths in point labels
            save_path: Optional path to save figure

        Returns:
            matplotlib Figure object
        """
        fig, ax = plt.subplots(figsize=(10, 7))

        # Plot measured points
        scatter = ax.scatter(results_df['E_interface_cathodic'],
                           results_df['charge_injected_nC'],
                           s=100, alpha=0.7, c=results_df['current_amplitude_ua'],
                           cmap='viridis', edgecolors='black', linewidth=1.5)

        # Add labels for each point
        for _, row in results_df.iterrows():
            if show_pulse_widths and 'pulse_width_ms' in results_df.columns:
                label = f"{row['current_amplitude_ua']:.0f}µA/{row['pulse_width_ms']:.1f}ms"
            else:
                label = f"{row['current_amplitude_ua']:.0f}µA"

            ax.annotate(label,
                       (row['E_interface_cathodic'], row['charge_injected_nC']),
                       xytext=(5, 5), textcoords='offset points',
                       fontsize=9, alpha=0.7)

        # Connect points with line
        ax.plot(results_df['E_interface_cathodic'],
               results_df['charge_injected_nC'],
               'k--', alpha=0.3, linewidth=1)

        # Add safety limit line
        ax.axvline(e_safe_cath, color='red', linestyle='--', linewidth=2,
                  label=f'Safety Limit ({e_safe_cath}V)', alpha=0.7)

        # Shade unsafe region
        xlim = ax.get_xlim()
        ax.fill_betweenx([0, ax.get_ylim()[1]], xlim[0], e_safe_cath,
                        alpha=0.1, color='red', label='Unsafe Region')

        # Add interpolation point if provided
        if interpolation:
            # Handle both current-based and charge-based interpolations
            if 'interpolated_current_ua' in interpolation:
                label = f'Interpolated: {interpolation["interpolated_current_ua"]:.1f}µA'
            else:
                label = f'Interpolated: {interpolation["interpolated_charge_nC"]:.1f}nC'

            ax.plot(e_safe_cath, interpolation['interpolated_charge_nC'],
                   'r*', markersize=15, label=label)
            ax.annotate(f'CIC = {interpolation["interpolated_cic_mC_cm2"]:.2f} mC/cm²',
                       (e_safe_cath, interpolation['interpolated_charge_nC']),
                       xytext=(-80, 20), textcoords='offset points',
                       fontsize=11, fontweight='bold',
                       bbox=dict(boxstyle='round,pad=0.5', facecolor='yellow', alpha=0.7),
                       arrowprops=dict(arrowstyle='->', connectionstyle='arc3,rad=0.3'))

        ax.set_xlabel('Interface Cathodic Voltage (V)', fontsize=12)
        ax.set_ylabel('Charge Injected (nC)', fontsize=12)
        ax.set_title('Charge Injection Capacity Interpolation at Safety Limit', fontsize=14, fontweight='bold')
        ax.grid(True, alpha=0.3)
        ax.legend(loc='best', fontsize=10)

        # Add colorbar for current
        cbar = plt.colorbar(scatter, ax=ax, label='Current Amplitude (µA)')

        plt.tight_layout()

        if save_path:
            fig.savefig(save_path, dpi=100, bbox_inches='tight')

        return fig

    @staticmethod
    def load_batch_data_from_folder(folder_path: str, pattern: str = "*.DTA") -> dict:
        """
        Load all DTA files from a folder for batch analysis.

        Args:
            folder_path: Path to folder containing DTA files
            pattern: Glob pattern for files to load (default: "*.DTA")

        Returns:
            Dictionary of {filename: DataFrame} suitable for batch_analyze()

        Example:
            >>> data_dict = CoganCICAnalyzer.load_batch_data_from_folder(
            ...     "/path/to/CIC_data/",
            ...     pattern="chip-*ua*.DTA"
            ... )
            >>> results_df, figs = CoganCICAnalyzer.batch_analyze(
            ...     data_dict, electrode_area=0.00707
            ... )
        """
        from pathlib import Path
        from ..dataloaders.gamry_dta_parser import GamryDTAParser
        from ..utils.path_utils import smart_path

        folder = smart_path(folder_path)
        if not folder.exists():
            raise FileNotFoundError(f"Folder not found: {folder_path}")

        data_dict = {}
        parser = GamryDTAParser()

        # Find all matching files
        files = sorted(folder.glob(pattern))

        if len(files) == 0:
            raise ValueError(f"No files matching '{pattern}' found in {folder_path}")

        print(f"Found {len(files)} files to load...")

        for file_path in files:
            print(f"  Loading {file_path.name}...")
            try:
                metadata, tables = parser.parse_file(str(file_path))

                # Find the Curve table
                curve_table = next((t for t in tables if t.table_type == 'Curve'), None)
                if curve_table is None:
                    print(f"    ⚠ Warning: No Curve table found in {file_path.name}, skipping")
                    continue

                # Use filename as key
                data_dict[file_path.name] = curve_table.data
                print(f"    ✓ Loaded successfully")

            except Exception as e:
                print(f"    ✗ Error loading {file_path.name}: {e}")
                continue

        if len(data_dict) == 0:
            raise ValueError("No valid DTA files could be loaded")

        print(f"\nSuccessfully loaded {len(data_dict)} files")
        return data_dict

    @staticmethod
    def batch_analyze_grouped(groups_dict: Dict[str, List[str]],
                              data_dict: Dict[str, pd.DataFrame],
                              electrode_area: float,
                              export_manager=None) -> Tuple[pd.DataFrame, Dict]:
        """
        Grouped CIC analysis with mean ± SEM statistics.

        Analyzes CIC measurements grouped by category (e.g., by electrode type,
        treatment condition) and calculates group-level statistics.

        Args:
            groups_dict: {group_name: [filename1, filename2, ...]}
            data_dict: {filename: DataFrame} from load_dta_folder()
            electrode_area: Electrode area in cm²
            export_manager: Optional ExportManager for saving results

        Returns:
            (grouped_summary_df, figures_dict)
            - grouped_summary_df: Group-level statistics (mean CIC, SEM, etc.)
            - figures_dict: Dictionary containing grouped comparison plots
        """
        group_results = []
        figures = {}

        for group_name, filenames in groups_dict.items():
            logger.info(f"Analyzing group: {group_name}")

            # Filter data for this group
            group_data = {name: data_dict[name] for name in filenames
                         if name in data_dict}

            if not group_data:
                logger.warning(f"No data found for group {group_name}")
                continue

            # Collect metrics for this group
            cic_values = []
            eipp_values = []
            e_mc_values = []
            q_cathodic_values = []

            for filename, data in group_data.items():
                try:
                    analyzer = CoganCICAnalyzer(data)
                    pulses = analyzer.detect_pulses(edge_padding=50)

                    if len(pulses) == 0:
                        logger.warning(f"No pulses detected in {filename}")
                        continue

                    results = analyzer.analyze_last_pulse(electrode_area)

                    cic_values.append(results.get('CIC_cathodic_mC_cm2', np.nan))
                    eipp_values.append(results.get('E_ipp', np.nan))
                    e_mc_values.append(results.get('E_mc', np.nan))
                    q_cathodic_values.append(results.get('Q_cathodic_C', np.nan) * 1e9)  # nC

                    logger.info(f"  ✓ {filename}: CIC = {results.get('CIC_cathodic_mC_cm2', np.nan):.2f} mC/cm²")

                except Exception as e:
                    logger.error(f"  ✗ Failed to analyze {filename}: {e}")

            # Calculate group statistics
            if cic_values:
                # Filter out NaN values for SD calculation
                cic_clean = [v for v in cic_values if not np.isnan(v)]
                eipp_clean = [v for v in eipp_values if not np.isnan(v)]

                group_summary = {
                    'group_name': group_name,
                    'n_samples': len(cic_clean),
                    'cic_mean': np.nanmean(cic_values),
                    'cic_std': np.std(cic_clean, ddof=1) if len(cic_clean) > 1 else 0,
                    'eipp_mean': np.nanmean(eipp_values),
                    'eipp_std': np.std(eipp_clean, ddof=1) if len(eipp_clean) > 1 else 0,
                    'e_mc_mean': np.nanmean(e_mc_values),
                    'q_cathodic_mean_nC': np.nanmean(q_cathodic_values),
                }
                group_results.append(group_summary)

                logger.info(f"  Group stats: CIC = {group_summary['cic_mean']:.2f} ± "
                           f"{group_summary['cic_std']:.2f} mC/cm²")

        # Create grouped summary DataFrame
        if group_results:
            summary_df = pd.DataFrame(group_results)

            if export_manager:
                export_manager.save_dataframe(summary_df, 'grouped_cic_results',
                                             subdir='data')

            return summary_df, figures
        else:
            return pd.DataFrame(), {}

    @staticmethod
    def plot_cic_grouped(results_dict: Dict[str, dict] = None,
                         summary_df: pd.DataFrame = None,
                         figsize: Tuple[int, int] = (8, 6),
                         export_manager=None) -> plt.Figure:
        """
        Bar chart comparing CIC values across groups with error bars.

        Can be called with either:
        - results_dict: {group_name: {'cic_mean': X, 'cic_std': Y, ...}}
        - summary_df: DataFrame from batch_analyze_grouped()

        Args:
            results_dict: Dictionary of group results (alternative to summary_df)
            summary_df: DataFrame from batch_analyze_grouped()
            figsize: Figure size tuple
            export_manager: Optional ExportManager for saving

        Returns:
            matplotlib Figure with grouped bar chart
        """
        # Convert results_dict to DataFrame if provided
        if summary_df is None and results_dict is not None:
            summary_df = pd.DataFrame([
                {'group_name': k, **v} for k, v in results_dict.items()
            ])

        if summary_df is None or summary_df.empty:
            raise ValueError("Either results_dict or summary_df must be provided")

        fig, ax = plt.subplots(figsize=figsize)

        groups = summary_df['group_name'].tolist()
        cic_means = summary_df['cic_mean'].tolist()
        cic_stds = summary_df['cic_std'].tolist() if 'cic_std' in summary_df.columns else [0] * len(groups)

        # Create bar chart with error bars
        x = np.arange(len(groups))
        bars = ax.bar(x, cic_means, yerr=cic_stds, capsize=5,
                     color='steelblue', edgecolor='black', alpha=0.8)

        # Add value labels on bars
        for bar, mean, std_val in zip(bars, cic_means, cic_stds):
            height = bar.get_height()
            label = f'{mean:.2f}' if std_val == 0 else f'{mean:.2f} ± {std_val:.2f}'
            ax.text(bar.get_x() + bar.get_width()/2, height + std_val + 0.02,
                   label, ha='center', va='bottom', fontsize=9)

        ax.set_xticks(x)
        ax.set_xticklabels(groups, rotation=45, ha='right')
        ax.set_ylabel('CIC (mC/cm²)', fontsize=11)
        ax.set_title('Charge Injection Capacity Comparison', fontsize=12)
        ax.grid(True, alpha=0.3, linestyle='--', axis='y')

        # Add sample size annotations
        if 'n_samples' in summary_df.columns:
            for i, (group, n) in enumerate(zip(groups, summary_df['n_samples'])):
                ax.text(i, 0, f'n={n}', ha='center', va='top', fontsize=8,
                       transform=ax.get_xaxis_transform())

        plt.tight_layout()

        if export_manager:
            export_manager.save_figure(fig, 'cic_grouped_comparison', subdir='plots')

        return fig
