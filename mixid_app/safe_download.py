"""Conflict-safe promotion of completed downloads into the music library."""

import errno
import os
import re

from safe_upload import copy_exclusive

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_mp3_name(title, maximum=180):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title or "Unknown"))
    name = name.strip().rstrip(". ") or "Unknown"
    name = name[:maximum].rstrip(". ") or "Unknown"
    if name.upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name + ".mp3"


def numbered_candidate(preferred_path, number):
    if number == 1:
        return preferred_path
    stem, extension = os.path.splitext(preferred_path)
    return f"{stem} ({number}){extension}"


def promote_unique(temp_path, preferred_path, maximum_attempts=1000):
    """Publish a completed file without ever replacing an existing path."""
    for number in range(1, maximum_attempts + 1):
        candidate = numbered_candidate(preferred_path, number)
        try:
            # A hard link publishes the already-complete file atomically and
            # fails with EEXIST instead of replacing user data.
            os.link(temp_path, candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno not in {
                errno.EPERM,
                errno.EACCES,
                errno.EXDEV,
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
            }:
                raise
            # Some Docker Desktop bind mounts do not support hard links.
            # Exclusive streaming preserves the same no-overwrite guarantee.
            with open(temp_path, "rb") as source:
                if not copy_exclusive(source, candidate):
                    continue
        os.remove(temp_path)
        return candidate
    raise FileExistsError("could not allocate a conflict-free output filename")
