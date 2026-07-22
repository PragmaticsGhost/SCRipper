"""Bounded cleanup for scratch files and regenerable application caches."""

import os
import shutil
import time


def _is_within(path, root):
    try:
        return os.path.commonpath(
            (os.path.realpath(path), os.path.realpath(root))
        ) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


def _old_enough(path, cutoff):
    try:
        return os.path.getmtime(path) < cutoff
    except OSError:
        return False


def prune_files(directory, *, suffix=None, max_age_seconds=None, max_entries=None, now=None):
    """Prune only regular files directly inside ``directory``."""
    now = time.time() if now is None else now
    if not os.path.isdir(directory):
        return 0
    candidates = []
    for entry in os.scandir(directory):
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        if suffix and not entry.name.endswith(suffix):
            continue
        try:
            candidates.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
        except OSError:
            continue
    candidates.sort(reverse=True)
    removed = 0
    cutoff = None if max_age_seconds is None else now - max_age_seconds
    for index, (mtime, path) in enumerate(candidates):
        expired = cutoff is not None and mtime < cutoff
        excess = max_entries is not None and index >= max_entries
        if not (expired or excess) or not _is_within(path, directory):
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def cleanup_runtime_artifacts(music_root, upload_dir, waveform_cache, *, now=None):
    """Remove abandoned app-owned scratch data and bound waveform cache size."""
    now = time.time() if now is None else now
    day = 24 * 60 * 60
    removed = {
        "uploads": prune_files(
            upload_dir,
            max_age_seconds=day,
            now=now,
        ),
        "waveforms": prune_files(
            waveform_cache,
            suffix=".json",
            max_age_seconds=90 * day,
            max_entries=5000,
            now=now,
        ),
        "downloads": 0,
    }
    cutoff = now - day
    if os.path.isdir(music_root):
        for root, dirs, _files in os.walk(music_root, topdown=True, followlinks=False):
            retained = []
            for name in dirs:
                path = os.path.join(root, name)
                if (
                    name.startswith(".scripper-download-")
                    and not os.path.islink(path)
                    and _is_within(path, music_root)
                    and _old_enough(path, cutoff)
                ):
                    try:
                        shutil.rmtree(path)
                        removed["downloads"] += 1
                    except OSError:
                        retained.append(name)
                else:
                    retained.append(name)
            dirs[:] = retained
    return removed
