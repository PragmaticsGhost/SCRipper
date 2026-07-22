"""Versioned, content-aware manifest helpers for the Panako index."""

import json
import os

MANIFEST_VERSION = 2
INVALID_MANIFEST_MARKER = "__mixid_invalid_manifest__"


def empty_manifest():
    return {"version": MANIFEST_VERSION, "files": {}}


def invalid_manifest(error):
    return {
        "version": None,
        "files": {},
        "invalid": True,
        "error": str(error),
    }


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return empty_manifest()
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return invalid_manifest(exc)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return invalid_manifest("manifest files field is invalid")
    return {
        "version": data.get("version", 1),
        "files": files,
    }


def save(path, manifest):
    """Atomically replace the manifest so crashes cannot leave partial JSON."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def signature(path):
    stat = os.stat(path)
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def entry_is_current(path, entry):
    if not isinstance(entry, dict):
        return False
    try:
        return entry == signature(path)
    except OSError:
        return False


def current_files(manifest):
    return {
        path for path, entry in manifest.get("files", {}).items() if entry_is_current(path, entry)
    }


def stale_files(manifest):
    if manifest.get("invalid"):
        return {INVALID_MANIFEST_MARKER}
    files = manifest.get("files", {})
    if manifest.get("version") != MANIFEST_VERSION:
        # Legacy manifests stored only booleans, so there is no safe way to
        # prove that the bytes on disk still match the fingerprint database.
        return set(files)
    return {path for path, entry in files.items() if not entry_is_current(path, entry)}


def record(manifest, path):
    manifest["version"] = MANIFEST_VERSION
    manifest.setdefault("files", {})[path] = signature(path)
