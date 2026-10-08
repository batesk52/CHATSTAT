"""Incremental CSV writer for auto-saving during measurement.

Writes CSV data per channel incrementally at each MUX loop boundary,
ensuring data is preserved even if the application crashes or the
user aborts mid-experiment.  File format is identical to
:class:`CSVExporter` output for downstream compatibility, electrode-config
provenance included -- both build those header lines from the shared
:func:`src.data.exporters.electrode_provenance_lines` helper.

That identity claim was false between the Req 10 electrode-config work and
2026-07-28: the provenance lines were added to ``CSVExporter`` only, so
auto-saved CSVs (the crash-safety copy for in-vivo runs) silently lacked the
wiring record while this docstring claimed otherwise.  The shared helper plus
``tests/data/test_incremental_electrode_provenance.py`` exist to stop the two
drifting apart again -- do not inline a second copy of that logic here.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import threading
from datetime import datetime
from typing import Any, Optional

from src.data.exporters import (
    _TECHNIQUE_COLUMN_MAP,
    _ordered_columns,
    electrode_provenance_lines,
)
from src.data.models import DataPoint

logger = logging.getLogger(__name__)


class IncrementalCSVWriter:
    """Writes CSV data incrementally during a measurement run.

    Lifecycle::

        writer = IncrementalCSVWriter()
        writer.start(technique, params, device_info, channels, output_dir)
        # ... at each MUX loop boundary:
        writer.flush_points(new_points)
        # ... on completion or abort:
        paths = writer.finish()

    Thread safety: :meth:`flush_points` is called from the engine
    QThread while :meth:`finish` may be called from the GUI thread
    during abort.  A lock guards both methods.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[int, io.TextIOWrapper] = {}
        self._writers: dict[int, csv.writer] = {}
        self._columns: dict[int, list[str]] = {}
        self._header_written: set[int] = set()
        self._output_dir: str = ""
        self._technique: str = ""
        self._params: dict[str, Any] = {}
        self._device_info: dict[str, str] = {}
        self._start_time: Optional[datetime] = None
        self._started = False
        self._channels: list[int] = []
        self._electrode_config_mode: str = ""
        self._re_ce_channels: list[int] = []

    def start(
        self,
        technique: str,
        params: dict[str, Any],
        device_info: dict[str, str],
        channels: list[int],
        output_dir: str,
        exact_dir: bool = False,
        electrode_config_mode: str = "",
        re_ce_channels: Optional[list[int]] = None,
    ) -> str:
        """Initialise the writer and create output directory.

        Args:
            technique: Lowercase technique identifier.
            params: Technique parameter dict.
            device_info: Device metadata (serial, firmware).
            channels: List of channels that will produce data. Also
                positionally matched against ``re_ce_channels`` to
                resolve each channel's RE/CE provenance.
            output_dir: Base directory; a timestamped subdirectory
                is created automatically unless ``exact_dir`` is set.
            electrode_config_mode: Wiring mode active for this run.
                Written into each channel's CSV header so an auto-saved
                file carries the same provenance as a manual export.
            re_ce_channels: Per-channel RE/CE positions, positionally
                matched to ``channels``. Omitted/None falls back to the
                position the mode implies.
            exact_dir: When True, write directly into ``output_dir``
                (the caller guarantees its uniqueness). The sequencer
                uses this so same-second repeats of one technique
                cannot collide on the second-resolution timestamped
                name and silently overwrite each other.

        Returns:
            Absolute path to the created output directory.
        """
        self._technique = technique
        self._params = params
        self._device_info = device_info
        self._channels = list(channels or [])
        self._electrode_config_mode = electrode_config_mode
        self._re_ce_channels = list(re_ce_channels or [])
        self._start_time = datetime.now()

        if exact_dir:
            self._output_dir = output_dir
        else:
            timestamp = self._start_time.strftime("%Y%m%d_%H%M%S")
            dirname = f"{timestamp}_{technique}_autosave"
            self._output_dir = os.path.join(output_dir, dirname)
        os.makedirs(self._output_dir, exist_ok=True)

        self._started = True
        logger.info(
            "Incremental writer started: %s", self._output_dir
        )
        return os.path.abspath(self._output_dir)

    def flush_points(self, points: list[DataPoint]) -> int:
        """Append new data points to per-channel CSV files.

        Each point is routed to the appropriate channel file.
        Headers are written on first write per channel.  Files are
        flushed and synced to disk for crash safety.

        Args:
            points: New data points since the last flush.

        Returns:
            Number of points written.
        """
        if not self._started or not points:
            return 0

        with self._lock:
            written = 0
            # Group points by channel
            by_channel: dict[int, list[DataPoint]] = {}
            for dp in points:
                by_channel.setdefault(dp.channel, []).append(dp)

            for ch, ch_points in by_channel.items():
                self._ensure_channel_file(ch, ch_points)
                writer = self._writers[ch]
                columns = self._columns[ch]

                for dp in ch_points:
                    row: list[Any] = [dp.timestamp]
                    for col in columns:
                        row.append(dp.variables.get(col, ""))
                    writer.writerow(row)
                    written += 1

                # Flush to OS and sync to disk
                handle = self._handles[ch]
                handle.flush()
                os.fsync(handle.fileno())

            return written

    def finish(self) -> list[str]:
        """Close all file handles and return written file paths.

        Safe to call multiple times; subsequent calls return an
        empty list.

        Returns:
            List of absolute file paths written.
        """
        with self._lock:
            if not self._started:
                return []

            paths: list[str] = []
            for ch in sorted(self._handles.keys()):
                handle = self._handles[ch]
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                    paths.append(
                        os.path.abspath(handle.name)
                    )
                    handle.close()
                except OSError:
                    logger.warning(
                        "Error closing file for channel %d", ch
                    )

            self._handles.clear()
            self._writers.clear()
            self._columns.clear()
            self._header_written.clear()
            self._started = False

            logger.info(
                "Incremental writer finished: %d files",
                len(paths),
            )
            return paths

    # -- Internal -----------------------------------------------------------

    def _ensure_channel_file(
        self, channel: int, points: list[DataPoint]
    ) -> None:
        """Open file and write header for a channel if not yet done."""
        if channel in self._header_written:
            return

        # Determine columns from the first batch of points, UNIONED with
        # the technique's canonical column schema. The header is frozen
        # here (CSV headers can't be rewritten mid-stream), so seeding the
        # known schema ensures a variable that only appears in a LATER
        # packet still gets a column — otherwise it would be silently
        # dropped by the variables.get(col, "") lookup in flush_points.
        all_vars: set[str] = set(
            _TECHNIQUE_COLUMN_MAP.get(self._technique, [])
        )
        for dp in points:
            all_vars.update(dp.variables.keys())
        columns = _ordered_columns(self._technique, all_vars)
        self._columns[channel] = columns

        filepath = os.path.join(
            self._output_dir, f"ch{channel:02d}.csv"
        )
        handle = open(  # noqa: SIM115
            filepath, "w", newline="", encoding="utf-8"
        )
        self._handles[channel] = handle
        self._writers[channel] = csv.writer(handle)

        # Metadata header (matches CSVExporter format)
        handle.write(f"# Technique: {self._technique}\n")
        handle.write(f"# Channel: {channel}\n")
        if self._start_time is not None:
            handle.write(
                f"# Timestamp: {self._start_time.isoformat()}\n"
            )
        serial = self._device_info.get("serial", "")
        firmware = self._device_info.get("firmware", "")
        if serial:
            handle.write(f"# Device Serial: {serial}\n")
        if firmware:
            handle.write(f"# Firmware Version: {firmware}\n")
        if self._params:
            params_str = ", ".join(
                f"{k}={v}" for k, v in self._params.items()
            )
            handle.write(f"# Parameters: {params_str}\n")
        # Electrode-config provenance, byte-identical to CSVExporter's
        # (shared helper). Auto-saved CSVs are the crash-safety copy for
        # in-vivo runs, so they must carry the same wiring record.
        for line in electrode_provenance_lines(
            self._electrode_config_mode,
            self._re_ce_channels,
            self._channels,
            channel,
        ):
            handle.write(line)
        handle.write("#\n")

        # Column header row
        self._writers[channel].writerow(
            ["timestamp"] + columns
        )
        self._header_written.add(channel)

    @property
    def output_dir(self) -> str:
        """Return the output directory path."""
        return self._output_dir

    @property
    def is_active(self) -> bool:
        """Return whether the writer is currently active."""
        return self._started
