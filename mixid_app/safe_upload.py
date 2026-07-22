"""Helpers for non-destructive library uploads."""

import os
import shutil


def copy_exclusive(stream, target):
    """Copy a binary stream to a new file without replacing existing data.

    Returns False when the target already exists. A partial file created by
    this call is removed if streaming fails.
    """
    try:
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o644,
        )
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(stream, out, length=1024 * 1024)
    except Exception:
        try:
            os.remove(target)
        except OSError:
            pass
        raise
    return True
