"""Resolve fingerprint matches and build file-aware analysis cache keys."""

import json
import os


def build_resolution_index(current_paths):
    """Precompute realpath and basename lookups for the whole library.

    Resolving one reference otherwise costs a realpath() call per library
    file, which is very slow on bind mounts. Callers build this once and
    reuse it for a burst of lookups.
    """
    exact = {}
    by_basename = {}
    for path in current_paths:
        real = os.path.realpath(path)
        exact[os.path.normcase(real)] = real
        by_basename.setdefault(os.path.normcase(os.path.basename(real)), []).append(real)
    return {"exact": exact, "by_basename": by_basename}


def resolve_with_index(reference, index):
    """Resolve a Panako reference against a prebuilt resolution index."""
    if not isinstance(reference, str) or not reference:
        return None

    hit = index["exact"].get(os.path.normcase(os.path.realpath(reference)))
    if hit:
        return hit

    basename = os.path.normcase(os.path.basename(reference))
    matches = index["by_basename"].get(basename, ())
    return matches[0] if len(matches) == 1 else None


def resolve_library_path(reference, current_paths):
    """Resolve a Panako reference without guessing between duplicate names."""
    if not isinstance(reference, str) or not reference:
        return None
    return resolve_with_index(reference, build_resolution_index(current_paths))


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
