"""
Export Manager for Electrochemistry Analysis

This module provides centralized export management with automatic directory creation,
timestamping, symlink management, and manifest file generation for analysis outputs.

Adapted from ClaudeSort (CMU.49.009) export_manager.py for electrochemistry workflows.
The manifest system tracks all generated files with metadata for easier discovery and
reproducibility.
"""

import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List, Union
import json
import pandas as pd
import matplotlib.pyplot as plt
import hashlib
import logging


class ExportManager:
    """Manages export directories and file outputs for analysis scripts."""

    MANIFEST_VERSION = "1.0"
    MANIFEST_FILENAME = "manifest.json"

    def __init__(self, script_name: Optional[str] = None, base_dir: Optional[Path] = None,
                 auto_manifest: bool = True, metadata: Optional[Dict[str, Any]] = None,
                 timestamp: Optional[str] = None):
        """
        Initialize export manager.

        Args:
            script_name: Name of the analysis script (auto-detected if None)
                        Can be a path like "eis/electrode_batch" which will be parsed
            base_dir: Base directory for exports (defaults to project exports/)
            auto_manifest: Automatically create/update manifest on file operations
            metadata: Initial metadata to include in manifest (analysis parameters, etc.)
            timestamp: Optional timestamp string (YYYYMMDD_HHMMSS format) to use instead of generating new one
        """
        self.timestamp = timestamp  # Store provided timestamp
        # Handle script_name that contains path components (e.g., "eis/electrode_batch")
        if script_name and '/' in script_name:
            # Parse the path components
            parts = script_name.split('/')
            # Use all but last as subdirectory, last as script name
            subdir_parts = parts[:-1]
            self.script_name = parts[-1]
            # Set base_dir to include the subdirectory structure
            if base_dir is not None:
                self.base_dir = Path(base_dir) / '/'.join(subdir_parts)
            else:
                self.base_dir = Path(__file__).parent.parent.parent / "exports" / '/'.join(subdir_parts)
        else:
            # Original behavior for simple script names
            if base_dir is not None:
                self.base_dir = Path(base_dir) if not isinstance(base_dir, Path) else base_dir
            else:
                self.base_dir = Path(__file__).parent.parent.parent / "exports"

            # Get script name or use _inbox as default
            self.script_name = script_name or self._get_script_name()

            # If still no script name, default to _inbox
            if not self.script_name:
                self.base_dir = self.base_dir / "_inbox"
                self.script_name = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.export_path = self._create_export_path()

        # Manifest tracking
        self.auto_manifest = auto_manifest
        self.manifest_metadata = metadata or {}
        self._tracked_files: List[Dict[str, Any]] = []

        # Setup logging
        self._setup_logging()

        # Initialize manifest if auto_manifest is enabled
        if self.auto_manifest:
            self._initialize_manifest()

    def _get_script_name(self) -> Optional[str]:
        """Auto-detect script name from sys.argv or stack."""
        # Try to get from command line
        if sys.argv[0]:
            script_path = Path(sys.argv[0])
            if script_path.suffix == '.py':
                return script_path.stem

        # Return None to trigger _inbox default
        return None

    def _create_export_path(self) -> Path:
        """Create timestamped export directory."""
        # Use provided timestamp or generate new one
        if self.timestamp:
            timestamp = self.timestamp
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = self.base_dir / self.script_name / timestamp

        # Create subdirectories
        (export_path / "plots").mkdir(parents=True, exist_ok=True)
        (export_path / "data").mkdir(parents=True, exist_ok=True)
        (export_path / "reports").mkdir(parents=True, exist_ok=True)
        (export_path / "logs").mkdir(parents=True, exist_ok=True)

        return export_path

    def get_output_dir(self, subdir: str = "") -> Path:
        """
        Get output directory path.

        Args:
            subdir: Subdirectory within export path (e.g., 'plots', 'data', 'reports')

        Returns:
            Path to output directory
        """
        if subdir:
            return self.export_path / subdir
        return self.export_path

    def save_dataframe(self, df: pd.DataFrame, filename: str, subdir: str = "data", **kwargs):
        """
        Save DataFrame to CSV with automatic path management.

        Args:
            df: DataFrame to save
            filename: Output filename (with or without .csv extension)
            subdir: Subdirectory for output
            **kwargs: Additional arguments for to_csv()
        """
        if not filename.endswith('.csv'):
            filename += '.csv'

        output_path = self.get_output_dir(subdir) / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, **kwargs)
        print(f"Saved DataFrame to: {output_path}")

        # Track file in manifest
        if self.auto_manifest:
            self._track_file(output_path, 'data')

        return output_path

    def save_figure(self, fig: Optional[plt.Figure] = None, filename: str = None,
                   subdir: str = "plots", dpi: int = 300, save_png: bool = False, save_svg: bool = True, **kwargs):
        """
        Save matplotlib figure with automatic path management.
        Saves SVG format by default (PNG disabled for vector editing workflow).

        Args:
            fig: Figure to save (uses current figure if None)
            filename: Output filename (auto-generated if None)
            subdir: Subdirectory for output
            dpi: Resolution for saved figure (PNG only, if enabled)
            save_png: If True, also saves a PNG version (default: False)
            save_svg: If True, saves an SVG version (default: True)
            **kwargs: Additional arguments for savefig()
        """
        if fig is None:
            fig = plt.gcf()

        if filename is None:
            timestamp = datetime.now().strftime("%H%M%S")
            filename = f"figure_{timestamp}"
        else:
            # Remove extension if provided (we'll add it ourselves)
            for ext in ['.png', '.pdf', '.svg', '.jpg']:
                if filename.endswith(ext):
                    filename = filename[:-len(ext)]
                    break

        output_dir = self.get_output_dir(subdir)
        output_dir.mkdir(parents=True, exist_ok=True)

        saved_paths = []

        # Save SVG version (default for vector editing workflow)
        if save_svg:
            svg_path = output_dir / f"{filename}.svg"
            # Ensure parent directory exists if filename contains subdirectories
            svg_path.parent.mkdir(parents=True, exist_ok=True)
            # SVG doesn't use DPI, remove it from kwargs if present
            svg_kwargs = {k: v for k, v in kwargs.items() if k != 'dpi'}
            fig.savefig(svg_path, format='svg', bbox_inches='tight', **svg_kwargs)
            print(f"Saved SVG figure to: {svg_path}")
            saved_paths.append(svg_path)

            # Track SVG file in manifest
            if self.auto_manifest:
                self._track_file(svg_path, 'plot')

        # Save PNG version (optional, for preview)
        if save_png:
            png_path = output_dir / f"{filename}.png"
            # Ensure parent directory exists if filename contains subdirectories
            png_path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(png_path, dpi=dpi, bbox_inches='tight', **kwargs)
            print(f"Saved PNG figure to: {png_path}")
            saved_paths.append(png_path)

            # Track PNG file in manifest
            if self.auto_manifest:
                self._track_file(png_path, 'plot')

        plt.close(fig)

        # Return single path or list of paths
        return saved_paths[0] if len(saved_paths) == 1 else saved_paths

    def save_json(self, data: Dict[str, Any], filename: str, subdir: str = "data"):
        """
        Save dictionary as JSON with automatic path management.

        Args:
            data: Dictionary to save
            filename: Output filename (with or without .json extension)
            subdir: Subdirectory for output
        """
        if not filename.endswith('.json'):
            filename += '.json'

        output_path = self.get_output_dir(subdir) / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        print(f"Saved JSON to: {output_path}")

        # Track file in manifest (but skip if it's the manifest itself or analysis_summary)
        if self.auto_manifest and filename != self.MANIFEST_FILENAME and filename != 'analysis_summary.json':
            self._track_file(output_path, 'data')

        return output_path

    def save_text(self, text: str, filename: str, subdir: str = "reports"):
        """
        Save text content to file.

        Args:
            text: Text content to save
            filename: Output filename
            subdir: Subdirectory for output
        """
        output_path = self.get_output_dir(subdir) / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(text)
        print(f"Saved text to: {output_path}")

        # Track file in manifest
        if self.auto_manifest:
            self._track_file(output_path, 'report')

        return output_path

    def log(self, message: str, level: str = "INFO"):
        """
        Log message to analysis log file.

        Args:
            message: Message to log
            level: Log level (INFO, WARNING, ERROR)
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}\n"

        log_file = self.get_output_dir("logs") / "analysis.log"
        with open(log_file, 'a') as f:
            f.write(log_entry)

    def create_summary(self, metadata: Dict[str, Any]):
        """
        Create analysis summary file with metadata.

        Args:
            metadata: Dictionary containing analysis metadata
        """
        metadata['export_path'] = str(self.export_path)
        metadata['timestamp'] = datetime.now().isoformat()
        metadata['script_name'] = self.script_name

        self.save_json(metadata, "analysis_summary.json", subdir="")

    def _setup_logging(self):
        """Setup logging for the export manager."""
        self.logger = logging.getLogger(f"ExportManager.{self.script_name}")
        self.logger.setLevel(logging.INFO)

        # Only add handler if not already present
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('[%(name)s] %(levelname)s: %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def _initialize_manifest(self):
        """Initialize manifest file with basic metadata."""
        manifest_path = self.export_path / self.MANIFEST_FILENAME

        if manifest_path.exists():
            # Load existing manifest
            try:
                with open(manifest_path, 'r') as f:
                    manifest = json.load(f)
                    self._tracked_files = manifest.get('files', [])
                    self.logger.info(f"Loaded existing manifest with {len(self._tracked_files)} files")
            except (json.JSONDecodeError, IOError) as e:
                self.logger.warning(f"Failed to load existing manifest: {e}, creating new one")
                self._tracked_files = []
                self._save_manifest()  # Save empty manifest
        else:
            # Create new manifest
            self.logger.info("Initializing new manifest")
            self._tracked_files = []
            self._save_manifest()  # Save empty manifest immediately

    def _get_file_metadata(self, file_path: Path, file_type: str = "unknown") -> Dict[str, Any]:
        """
        Extract metadata for a single file.

        Args:
            file_path: Path to the file
            file_type: Type classification (e.g., 'plot', 'data', 'report')

        Returns:
            Dictionary with file metadata
        """
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        stat = file_path.stat()

        # Get relative path from export directory
        try:
            rel_path = file_path.relative_to(self.export_path)
        except ValueError:
            rel_path = file_path

        return {
            "path": str(rel_path),
            "type": file_type,
            "size_bytes": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "extension": file_path.suffix
        }

    def _track_file(self, file_path: Path, file_type: str = "unknown"):
        """
        Add a file to the tracked files list.

        Args:
            file_path: Path to the file
            file_type: Type classification
        """
        try:
            metadata = self._get_file_metadata(file_path, file_type)

            # Check if file already tracked (update if so)
            existing_idx = None
            for idx, tracked in enumerate(self._tracked_files):
                if tracked['path'] == metadata['path']:
                    existing_idx = idx
                    break

            if existing_idx is not None:
                self._tracked_files[existing_idx] = metadata
                self.logger.debug(f"Updated tracked file: {metadata['path']}")
            else:
                self._tracked_files.append(metadata)
                self.logger.debug(f"Added tracked file: {metadata['path']}")

            # Auto-save manifest if enabled
            if self.auto_manifest:
                self._save_manifest()

        except Exception as e:
            self.logger.error(f"Failed to track file {file_path}: {e}")

    def _save_manifest(self):
        """Save the current manifest to disk."""
        try:
            manifest = self._build_manifest_dict()
            manifest_path = self.export_path / self.MANIFEST_FILENAME

            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2, default=str)

            self.logger.debug(f"Manifest saved with {len(self._tracked_files)} files")

        except Exception as e:
            self.logger.error(f"Failed to save manifest: {e}")

    def _build_manifest_dict(self) -> Dict[str, Any]:
        """
        Build the complete manifest dictionary.

        Returns:
            Dictionary with full manifest structure
        """
        # Calculate summary statistics
        total_size = sum(f['size_bytes'] for f in self._tracked_files)
        total_size_mb = total_size / (1024 * 1024)

        # Count by type
        type_counts = {}
        for file_info in self._tracked_files:
            file_type = file_info['type']
            type_counts[file_type] = type_counts.get(file_type, 0) + 1

        # Build manifest
        manifest = {
            "version": self.MANIFEST_VERSION,
            "created": self.manifest_metadata.get('created', datetime.now().isoformat()),
            "updated": datetime.now().isoformat(),
            "export_path": str(self.export_path),
            "script_name": self.script_name,
            "analysis_type": self.manifest_metadata.get('analysis_type', self.script_name),
            "technique": self.manifest_metadata.get('technique'),
            "electrode": self.manifest_metadata.get('electrode'),
            "parameters": self.manifest_metadata.get('parameters', {}),
            "files": self._tracked_files,
            "summary": {
                "total_files": len(self._tracked_files),
                "total_size_mb": round(total_size_mb, 2),
                "total_size_bytes": total_size,
                "by_type": type_counts
            }
        }

        # Add custom metadata fields
        for key, value in self.manifest_metadata.items():
            if key not in manifest:
                manifest[key] = value

        return manifest

    def create_manifest(self, export_path: Optional[Path] = None,
                       metadata: Optional[Dict[str, Any]] = None) -> Path:
        """
        Create a manifest file for the export directory.

        Args:
            export_path: Path to export directory (defaults to current export_path)
            metadata: Additional metadata to include

        Returns:
            Path to created manifest file
        """
        if export_path is None:
            target_path = self.export_path
        else:
            target_path = Path(export_path)

        # Update metadata if provided
        if metadata:
            self.manifest_metadata.update(metadata)

        # Temporarily store original export_path if creating for different directory
        original_export_path = self.export_path
        if target_path != self.export_path:
            self.export_path = target_path

        try:
            # Scan export directory for all files
            self._scan_export_directory(target_path)

            # Save manifest
            self._save_manifest()

            manifest_path = target_path / self.MANIFEST_FILENAME
            self.logger.info(f"Created manifest at: {manifest_path}")

            return manifest_path
        finally:
            # Restore original export_path
            if target_path != original_export_path:
                self.export_path = original_export_path

    def _scan_export_directory(self, export_path: Path):
        """
        Scan export directory and track all files.

        Args:
            export_path: Path to export directory
        """
        # Clear existing tracked files
        self._tracked_files = []

        # Define file type mappings
        type_mappings = {
            'plots': {'.png', '.svg', '.pdf', '.jpg', '.jpeg'},
            'data': {'.csv', '.json', '.npy', '.pkl', '.h5', '.mat'},
            'report': {'.txt', '.md', '.html', '.pdf'},
            'log': {'.log'},
            'code': {'.py', '.ipynb', '.m', '.r'},
        }

        # Scan all files
        for file_path in export_path.rglob('*'):
            if not file_path.is_file():
                continue

            # Skip the manifest file itself
            if file_path.name == self.MANIFEST_FILENAME:
                continue

            # Determine file type
            file_type = "other"
            for type_name, extensions in type_mappings.items():
                if file_path.suffix.lower() in extensions:
                    file_type = type_name
                    break

            # Add to tracked files
            try:
                metadata = self._get_file_metadata(file_path, file_type)
                self._tracked_files.append(metadata)
            except Exception as e:
                self.logger.warning(f"Failed to track file {file_path}: {e}")

        self.logger.info(f"Scanned directory, found {len(self._tracked_files)} files")

    def __str__(self) -> str:
        return f"ExportManager({self.script_name} -> {self.export_path})"

    def __repr__(self) -> str:
        return self.__str__()
