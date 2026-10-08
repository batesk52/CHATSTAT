r"""
Cross-platform path handling utilities for Windows/WSL environments.

This module provides transparent conversion between Windows paths (e.g., D:\SynologyDrive\...)
and WSL paths (e.g., /mnt/d/SynologyDrive/...) with automatic retry on file operations.

Adapted from Device Characterization (CMU.49.005) datatraveler.py.
"""

import re
import sys
import functools
import warnings
from pathlib import Path
from typing import Union, Callable


def windows_to_wsl_path(windows_path: str) -> str:
    """
    Convert Windows path to WSL path format.

    Examples:
        C:\\Users\\Karl\\file.txt -> /mnt/c/Users/Karl/file.txt
        C:/Users/Karl/file.txt -> /mnt/c/Users/Karl/file.txt
        D:\\Data\\experiment.csv -> /mnt/d/Data/experiment.csv
        D:\\SynologyDrive\\CMU.80 Data\\file.csv -> /mnt/d/SynologyDrive/CMU.80 Data/file.csv

    Args:
        windows_path: Windows-style path string

    Returns:
        WSL-formatted path string
    """
    # Handle different Windows path formats
    path_str = str(windows_path)

    # Replace backslashes with forward slashes
    path_str = path_str.replace('\\', '/')

    # Handle drive letters (C:, D:, etc.)
    drive_pattern = re.match(r'^([A-Za-z]):', path_str)
    if drive_pattern:
        drive_letter = drive_pattern.group(1).lower()
        path_str = re.sub(r'^[A-Za-z]:', f'/mnt/{drive_letter}', path_str)

    # Handle UNC paths (\\server\share or //server/share)
    elif path_str.startswith('//'):
        # Remove leading slashes and prepend /mnt/
        path_str = '/mnt/' + path_str.lstrip('/')

    return path_str


def is_windows_path(path_str: str) -> bool:
    """
    Check if a path string is a Windows-style path.

    Detects:
        - Drive letters (C:, D:, etc.)
        - Backslashes
        - UNC paths (\\\\server\\share)

    Args:
        path_str: Path string to check

    Returns:
        True if path is Windows-style, False otherwise
    """
    path_str = str(path_str)
    # Check for drive letters, backslashes, or UNC paths
    return bool(
        re.match(r'^[A-Za-z]:', path_str) or  # Drive letter
        '\\' in path_str or  # Backslashes
        path_str.startswith('\\\\') or  # UNC path with backslashes
        (path_str.startswith('//') and not path_str.startswith('/mnt/'))  # UNC forward slashes
    )


def intelligent_path_handler(func: Callable) -> Callable:
    """
    Decorator that automatically retries file operations with WSL path translation.

    If a file operation fails with a Windows path, it automatically converts
    the path to WSL format and retries the operation. Emits a warning when
    conversion occurs.

    Example:
        @intelligent_path_handler
        def load_data(file_path: str):
            with open(file_path, 'r') as f:
                return f.read()

        # User passes Windows path: D:\\SynologyDrive\\data.csv
        # Decorator tries original, fails, converts to /mnt/d/SynologyDrive/data.csv
        # Emits warning and retries successfully

    Args:
        func: Function to decorate (must accept path as argument)

    Returns:
        Decorated function with automatic path conversion
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            # First attempt with original path
            return func(*args, **kwargs)
        except (FileNotFoundError, OSError, PermissionError) as e:
            # Extract path from args or kwargs
            path_arg = None
            path_idx = None
            path_kwarg = None

            # Check positional arguments for path-like objects
            for idx, arg in enumerate(args):
                if isinstance(arg, (str, Path)) and is_windows_path(str(arg)):
                    path_arg = str(arg)
                    path_idx = idx
                    break

            # Check keyword arguments for path-like objects
            for key, value in kwargs.items():
                if isinstance(value, (str, Path)) and is_windows_path(str(value)):
                    path_kwarg = key
                    path_arg = str(value)
                    break

            if path_arg:
                # Try converting to WSL path
                wsl_path = windows_to_wsl_path(path_arg)

                if wsl_path != path_arg:  # Only retry if path actually changed
                    warnings.warn(
                        f"Windows path detected. Retrying with WSL path: {path_arg} -> {wsl_path}",
                        UserWarning
                    )

                    # Prepare new arguments with converted path
                    if path_idx is not None:
                        new_args = list(args)
                        new_args[path_idx] = Path(wsl_path) if isinstance(args[path_idx], Path) else wsl_path
                        return func(*new_args, **kwargs)
                    elif path_kwarg:
                        new_kwargs = kwargs.copy()
                        new_kwargs[path_kwarg] = Path(wsl_path) if isinstance(kwargs[path_kwarg], Path) else wsl_path
                        return func(*args, **new_kwargs)

            # If no Windows path found or conversion didn't help, re-raise original error
            raise e

    return wrapper


def smart_path(path: Union[str, Path]) -> Path:
    """
    Convert any path to a Path object, with automatic Windows-to-WSL conversion if needed.

    This function can be used anywhere in the codebase to ensure paths work across platforms.
    Emits a warning when Windows path conversion occurs.

    Examples:
        >>> smart_path("D:\\SynologyDrive\\data.csv")
        Path('/mnt/d/SynologyDrive/data.csv')  # Warning emitted (WSL only)

        >>> smart_path("/home/user/data.csv")
        Path('/home/user/data.csv')  # No conversion

        >>> smart_path("C:/Users/Karl/experiment.DTA")
        Path('/mnt/c/Users/Karl/experiment.DTA')  # Warning emitted (WSL only)

    Note:
        On Windows (sys.platform == 'win32'), the path is returned as-is without
        conversion. WSL-to-Windows translation only runs on Linux/WSL hosts where
        Windows-form paths would otherwise be unresolvable.

    Args:
        path: Path string or Path object (Windows or Unix format)

    Returns:
        Path object in WSL format (if Windows path detected on WSL/Linux),
        or unchanged Path on Windows-native Python.
    """
    path_str = str(path)

    if sys.platform == 'win32':
        return Path(path_str)

    if is_windows_path(path_str):
        wsl_path_str = windows_to_wsl_path(path_str)
        warnings.warn(
            f"Converted Windows path to WSL: {path} -> {wsl_path_str}",
            UserWarning
        )
        return Path(wsl_path_str)

    return Path(path_str)
