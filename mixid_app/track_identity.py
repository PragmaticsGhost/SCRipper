"""Resolve fingerprint matches and build file-aware analysis cache keys."""

import json
import os


def resolve_library_path(reference, current_paths):
    """Resolve a Panako reference without guessing between duplicate names."""
    if not isinstance(reference, str) or not reference:
        return None

    candidates = [os.path.realpath(path) for path in current_paths]
    reference_real = os.path.realpath(reference)
    for candidate in candidates:
        if os.path.normcase(candidate) == os.path.normcase(reference_real):
            return candidate

    basename = os.path.basename(reference)
    matches = [
        candidate
        for candidate in candidates
        if os.path.normcase(os.path.basename(candidate)) == os.path.normcase(basename)
    ]
    return matches[0] if len(matches) == 1 else None


def analysis_cache_key(path, library_root):
    """Key analysis by relative path and the current file signature."""
    real = os.path.realpath(path)
    stat = os.stat(real)
    relative = os.path.relpath(real, os.path.realpath(library_root))
    return json.dumps(
        [relative, stat.st_size, stat.st_mtime_ns],
        ensure_ascii=True,
        separators=(",", ":"),
    )
