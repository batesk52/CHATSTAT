"""
Utilities for Electrochemistry Analysis

This package provides cross-cutting utilities including export management
and cross-platform path handling.
"""

from .export_manager import ExportManager
from .path_utils import (
    windows_to_wsl_path,
    is_windows_path,
    intelligent_path_handler,
    smart_path
)

__all__ = [
    'ExportManager',
    'windows_to_wsl_path',
    'is_windows_path',
    'intelligent_path_handler',
    'smart_path',
]
