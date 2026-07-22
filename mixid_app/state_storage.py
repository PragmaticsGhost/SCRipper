"""Crash-safe JSON persistence helpers used by application state and caches."""

import copy
import json
import os
import tempfile
import time


def _default_value(default):
    return default() if callable(default) else copy.deepcopy(default)


def quarantine_file(path, label="invalid"):
    """Move a malformed state file aside, preserving it for diagnosis."""
    if not os.path.isfile(path):
        return None
    backup = f"{path}.{label}-{time.time_ns()}"
    os.replace(path, backup)
    return backup


def load_json(path, default, quarantine_invalid=True):
    """Load JSON or return an independent default value.

    Syntax-invalid JSON is preserved beside the original before the default is
    returned. Ordinary I/O failures are left to the caller to diagnose.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return _default_value(default)
    except json.JSONDecodeError:
        if quarantine_invalid:
            try:
                quarantine_file(path)
            except OSError:
                pass
        return _default_value(default)


def atomic_write_json(path, value, *, indent=None, mode=None):
    """Durably replace a JSON file without exposing a partially written file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        if mode is not None:
            os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(value, handle, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if mode is not None:
            os.chmod(path, mode)
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass
