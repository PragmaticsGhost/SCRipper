"""Validation and mutation helpers for saved identification history."""

import math


def apply_manual_title(tracklist, start, manual_title):
    """Set a title on the unidentified entry at ``start``.

    Raises ValueError for invalid input and LookupError when no entry matches.
    """
    if (
        isinstance(start, bool)
        or not isinstance(start, (int, float))
        or not math.isfinite(float(start))
    ):
        raise ValueError("start must be a finite number")
    if manual_title is not None and not isinstance(manual_title, str):
        raise ValueError("manual_title must be a string or null")
    title = (manual_title or "").strip() or None

    for track in tracklist:
        track_start = track.get("start")
        if (
            not track.get("unidentified")
            or isinstance(track_start, bool)
            or not isinstance(track_start, (int, float))
        ):
            continue
        if math.isfinite(float(track_start)) and abs(float(track_start) - start) < 0.01:
            track["manual_title"] = title
            return title
    raise LookupError("unidentified history entry not found")
