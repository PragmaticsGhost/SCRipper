#!/usr/bin/env python3
"""
SCRipper suite — web GUI combining:
  - SCRipper: SoundCloud/YouTube track downloader (320kbps MP3 + metadata)
  - MixID:    DJ mix track identification via Panako fingerprinting

Runs inside the Docker container built from mixid_app/Dockerfile.
Music folders are mounted read-write at /music, the Panako DB persists
at /root/.panako/dbs, uploads land in /uploads.
"""

import hashlib
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

import requests as http_requests
import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

from app_config import AppSettings
from application_state import ApplicationState
from download_service import (
    convert_to_mp3,
    embed_metadata,
    friendly_download_error,
)
from fingerprint_manifest import (
    INVALID_MANIFEST_MARKER,
    empty_manifest,
)
from fingerprint_manifest import (
    current_files as current_manifest_files,
)
from fingerprint_manifest import (
    load as load_manifest_file,
)
from fingerprint_manifest import (
    record as record_manifest_file,
)
from fingerprint_manifest import (
    save as save_manifest_file,
)
from fingerprint_manifest import (
    stale_files as stale_manifest_files,
)
from history_updates import apply_manual_title
from job_service import JobService, sanitize_log
from metadata_store import MISSING, MetadataStores
from process_utils import (
    ProcessDeadlineExceeded,
    audio_process_timeout,
    iter_chunks_with_deadline,
    iter_lines_with_deadline,
    terminate_process_tree,
)
from request_security import is_cross_origin_browser_request
from request_validation import (
    MAX_DOWNLOAD_URLS,
    MAX_RESOLVE_URLS,
    RequestValidationError,
    require_object,
    split_and_validate_urls,
    validate_download_request,
    validate_path_request,
    validate_urls,
)
from runtime_hygiene import cleanup_runtime_artifacts
from safe_download import promote_unique, safe_mp3_name
from safe_upload import copy_exclusive
from state_storage import atomic_write_json, load_json
from track_identity import analysis_cache_key, build_resolution_index, resolve_with_index
from waveform import (
    WaveformLimitError,
    aggregate_pcm,
    validate_waveform_request,
)
from web_routes import create_blueprint
from worker_runtime import WorkerRuntime

SETTINGS = AppSettings.from_environment()
MUSIC_ROOT = SETTINGS.music_root
UPLOAD_DIR = SETTINGS.upload_dir
DB_DIR = SETTINGS.db_dir
MANIFEST_PATH = os.path.join(DB_DIR, "mixid_manifest.json")
PANAKO_LMDB_DIR = os.path.join(DB_DIR, "panako_db")
STRATEGY = "panako"
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aiff", ".aif")
UNIDENTIFIED_GAP_S = 40  # a hole this long in the tracklist = missed track
DOWNLOAD_WORKERS = 3
JOB_RETENTION_SECONDS = 24 * 60 * 60
JOB_MAX_FINISHED = 100
PANAKO_QUEUE_MAX = 8
DOWNLOAD_QUEUE_MAX = 4
METADATA_QUEUE_MAX = 1000
MAX_TRACK_UPLOADS = 500
MAX_AUDIO_DURATION_SECONDS = 8 * 60 * 60
WAVEFORM_TIMEOUT_SECONDS = 10 * 60

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)

# —— Host-header allowlist (defeats DNS-rebinding / cross-site access) ——
# This app is localhost-only; reject requests whose Host header isn't a
# loopback address so a website you visit can't drive the API via rebinding.
ALLOWED_HOSTS = {
    "localhost:8080",
    "127.0.0.1:8080",
    "[::1]:8080",
    "localhost",
    "127.0.0.1",
}


def _enforce_local_origin():
    host = (request.host or "").lower()
    if host not in ALLOWED_HOSTS:
        return ("Forbidden: unexpected Host header. This app only serves localhost.", 403)
    if is_cross_origin_browser_request(
        request.scheme,
        host,
        origin=request.headers.get("Origin"),
        sec_fetch_site=request.headers.get("Sec-Fetch-Site"),
        referer=request.headers.get("Referer"),
    ):
        return ("Forbidden: cross-origin browser request.", 403)


# —— Logging: rotating file (mounted to mixid_app/logs on the host) ——
LOG_DIR = SETTINGS.log_dir
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("scripper")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = RotatingFileHandler(
    os.path.join(LOG_DIR, "scripper.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_fh.setFormatter(_fmt)
if not any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
    logger.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
if not any(type(handler) is logging.StreamHandler for handler in logger.handlers):
    logger.addHandler(_sh)

# —— Manifest: which files have been indexed ——
_manifest_lock = threading.Lock()


def load_manifest():
    manifest = load_manifest_file(MANIFEST_PATH)
    if not os.path.exists(MANIFEST_PATH) and os.path.isdir(PANAKO_LMDB_DIR):
        try:
            database_has_files = any(os.scandir(PANAKO_LMDB_DIR))
        except OSError as exc:
            manifest.update(invalid=True, error=str(exc), version=None)
        else:
            if database_has_files:
                manifest.update(
                    invalid=True,
                    error="fingerprint database exists without a manifest",
                    version=None,
                )
    return manifest


def save_manifest(m):
    save_manifest_file(MANIFEST_PATH, m)


def manifest_add(path):
    with _manifest_lock:
        m = load_manifest()
        record_manifest_file(m, path)
        save_manifest(m)


_manifest_files_cache = {"sig": object(), "value": frozenset()}
_library_index_cache = {"sig": object(), "value": None}
_library_index_lock = threading.Lock()


def _manifest_signature():
    """Cheap signature of the manifest file itself (one stat)."""
    try:
        st = os.stat(MANIFEST_PATH)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def manifest_files():
    # current_manifest_files() os.stat()s every indexed file to prove it is
    # current, which is slow on a Docker Desktop bind mount (~0.7s for a
    # large library). During one scan the manifest itself never changes, so
    # cache the result keyed on the manifest file's own signature: the burst
    # of per-track lookups in collapse_matches then costs one stat sweep, not
    # dozens. Any manifest write (indexing) changes the signature and
    # invalidates the cache automatically.
    sig = _manifest_signature()
    with _manifest_lock:
        cache = _manifest_files_cache
        if sig is not None and cache["sig"] == sig:
            return cache["value"]
        value = current_manifest_files(load_manifest())
        cache["sig"] = sig
        cache["value"] = value
        return value


def library_resolution_index():
    """Memoized realpath/basename index used to resolve Panako references.

    Building it walks the whole library once; resolving against it is a
    dict lookup. Without this, one identification spends minutes repeating
    the same realpath sweep for every matched track.
    """
    sig = _manifest_signature()
    with _library_index_lock:
        cache = _library_index_cache
        if sig is not None and cache["sig"] == sig and cache["value"] is not None:
            return cache["value"]
    # Build outside the lock: manifest_files() takes a different lock, and a
    # duplicate concurrent build is harmless.
    index = build_resolution_index(manifest_files())
    with _library_index_lock:
        _library_index_cache["sig"] = sig
        _library_index_cache["value"] = index
    return index


def manifest_stale_paths():
    with _manifest_lock:
        return stale_manifest_files(load_manifest())


def manifest_snapshot():
    with _manifest_lock:
        m = load_manifest()
        return {
            "version": m.get("version"),
            "files": dict(m["files"]),
            "invalid": bool(m.get("invalid")),
            "error": m.get("error"),
        }


def manifest_begin_rebuild(paths):
    """Persist the rebuild candidates so an interrupted rebuild retries."""
    with _manifest_lock:
        pending = empty_manifest()
        pending["files"] = {path: {"pending": True} for path in paths}
        save_manifest(pending)


def manifest_remove(path):
    with _manifest_lock:
        m = load_manifest()
        m["files"].pop(path, None)
        save_manifest(m)


def quarantine_invalid_manifest():
    """Preserve an invalid manifest before a full consistency rebuild."""
    if not os.path.isfile(MANIFEST_PATH):
        return None
    backup = f"{MANIFEST_PATH}.invalid-{int(time.time())}"
    os.replace(MANIFEST_PATH, backup)
    return backup


# —— Job queue (Panako LMDB writes must be serialized) ——
_job_service = JobService(
    logger,
    retention_seconds=JOB_RETENTION_SECONDS,
    max_finished=JOB_MAX_FINISHED,
    panako_capacity=PANAKO_QUEUE_MAX,
    download_capacity=DOWNLOAD_QUEUE_MAX,
    metadata_capacity=METADATA_QUEUE_MAX,
)
_jobs = _job_service.jobs
_jobs_lock = _job_service.lock
_panako_job_queue = _job_service.panako_queue
_download_job_queue = _job_service.download_queue
_metadata_queue = _job_service.metadata_queue


def _sanitize_log(s):
    """Strip control chars (incl. newlines) so user-controlled strings
    can't forge extra log lines."""
    return sanitize_log(s)


def new_job(jtype, label):
    return _job_service.new(jtype, label)


def update_job(jid, **kw):
    _job_service.update(jid, **kw)


def job_log(jid, line):
    """Append a timestamped line to the job's console feed (and logfile)."""
    _job_service.append_log(jid, line)


def job_active(jid, key, val):
    """Track live per-file progress; val=None clears the entry."""
    _job_service.set_active(jid, key, val)


def get_job(jid):
    return _job_service.get(jid)


def submit_job(jtype, label, job_queue, fn, args):
    """Create and enqueue a job, returning None when capacity is exhausted."""
    return _job_service.submit(jtype, label, job_queue, fn, args)


def is_cancelled(jid):
    return _job_service.is_cancelled(jid)


def run_panako_cancellable(args, jid, timeout=None):
    """Run panako as a killable child so a job cancel stops it immediately.
    Mirrors run_panako's CompletedProcess return shape."""
    proc = subprocess.Popen(
        ["panako"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    _job_service.register_process(jid, proc)
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        terminate_process_tree(proc)
        out, err = proc.communicate()
        return subprocess.CompletedProcess(args, proc.returncode or 1, out, err)
    finally:
        _job_service.clear_process(jid, proc)


def worker_loop(job_queue, worker_name, stop_event=None):
    while stop_event is None or not stop_event.is_set():
        try:
            jid, fn, args = job_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        update_job(jid, status="running", worker=worker_name)
        logger.info(f"job {jid} started on {worker_name} worker")
        try:
            fn(jid, *args)
            logger.info(f"job {jid} finished: {get_job(jid)['status']}")
        except Exception as e:
            logger.exception(f"job {jid} crashed")
            update_job(jid, status="error", error=str(e))
        finally:
            job_queue.task_done()


# —— Path safety ——
def safe_music_path(rel):
    p = os.path.realpath(os.path.join(MUSIC_ROOT, rel.lstrip("/\\")))
    if not (p == MUSIC_ROOT or p.startswith(MUSIC_ROOT + os.sep)):
        return None
    return p


# —— Waveform peak generation (for the neon mix/track visualisations) ——
WAVEFORM_CACHE = os.path.join(DB_DIR, "waveform_cache")
os.makedirs(WAVEFORM_CACHE, exist_ok=True)

WAVEFORM_ALGO = "rms1"  # bump to invalidate the on-disk peak cache


def compute_peaks(
    path, buckets, rate=8000, duration=None, timeout_seconds=WAVEFORM_TIMEOUT_SECONDS
):
    """Decode audio to low-rate mono PCM via ffmpeg and reduce it to
    `buckets` normalised RMS-energy peaks without buffering decoded audio."""
    duration = duration if duration is not None else audio_duration(path)
    try:
        total_samples = validate_waveform_request(
            duration,
            buckets,
            rate,
            MAX_AUDIO_DURATION_SECONDS,
        )
    except WaveformLimitError:
        return []
    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "quiet",
            "-i",
            path,
            "-ac",
            "1",
            "-filter:a",
            f"aresample={rate}",
            "-f",
            "s16le",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        chunks = iter_chunks_with_deadline(proc, timeout_seconds)
        peaks = aggregate_pcm(chunks, buckets, total_samples)
        if proc.returncode != 0:
            return []
        return peaks
    except ProcessDeadlineExceeded:
        return []
    except Exception:
        terminate_process_tree(proc)
        raise
    finally:
        if proc.stdout:
            proc.stdout.close()


def cached_peaks(path, buckets):
    """Peaks for a library track, cached on disk keyed by path+mtime+buckets."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    key = hashlib.sha1(f"{path}|{mtime}|{buckets}|{WAVEFORM_ALGO}".encode()).hexdigest()
    cache_file = os.path.join(WAVEFORM_CACHE, key + ".json")
    if os.path.isfile(cache_file):
        try:
            peaks = load_json(cache_file, None)
            if isinstance(peaks, list):
                return peaks
        except OSError:
            pass
    peaks = compute_peaks(path, buckets)
    try:
        atomic_write_json(cache_file, peaks)
    except OSError:
        pass
    return peaks


# ============================================================
# MixID: Panako helpers
# ============================================================
def run_panako(args, timeout=None):
    return subprocess.run(
        ["panako"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def audio_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


def parse_monitor_line(line):
    parts = [p.strip() for p in line.split(";")]
    if len(parts) < 13 or parts[0].lower().startswith(("index", "#")):
        return None
    if parts[5].lower() in ("null", ""):
        return "nomatch"
    try:
        seg = re.search(r"-([\d.]+)_([\d.]+)$", parts[2])
        seg_offset = float(seg.group(1)) if seg else 0.0
        return {
            "query_start": seg_offset + float(parts[3]),
            "query_stop": seg_offset + float(parts[4]),
            "match_path": parts[5],
            "match_name": os.path.basename(parts[5]),
            "match_stop": float(parts[8]),  # position reached in the source track
            "score": float(parts[9]),
            "time_factor": float(parts[10].replace("%", "").strip()),
        }
    except (ValueError, IndexError):
        return None


_metadata_stores = MetadataStores(DB_DIR, logger)


def _library_path(reference):
    return resolve_with_index(reference, library_resolution_index())


def _analysis_target(reference):
    path = _library_path(reference)
    if not path or not os.path.isfile(path):
        return None, None
    try:
        return path, analysis_cache_key(path, MUSIC_ROOT)
    except OSError:
        return None, None


def library_track_duration(reference, analyze=True):
    """Duration of an indexed library track, cached by path and signature."""
    path, cache_key = _analysis_target(reference)
    if cache_key is None:
        return None
    cached = _metadata_stores.duration.get(cache_key)
    if cached is not MISSING:
        return cached
    if not analyze:
        return None
    d = audio_duration(path) if path and os.path.isfile(path) else None
    _metadata_stores.duration.set(cache_key, d)
    return d


# —— BPM detection (TBPM tag first, else aubio; cached on disk) ——
# v3: cache keys include the relative path, size, and modification time.


def _tag_bpm(path):
    """BPM from an existing TBPM tag (e.g. written by Rekordbox)."""
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        if mf and mf.tags and "TBPM" in mf.tags:
            return float(str(mf.tags["TBPM"].text[0]))
    except Exception:
        pass
    return None


def _snap_bpm(bpm):
    """Snap to the musical grid: DJ tracks are nearly always integer or
    half BPMs, and the beat tracker's phase jitter is ~±0.3 BPM. Leaves
    genuinely odd tempos untouched."""
    if abs(bpm - round(bpm)) <= 0.35:
        return float(round(bpm))
    half = round(bpm * 2) / 2
    if abs(bpm - half) <= 0.15:
        return half
    return round(bpm, 1)


def _detect_bpm(path):
    """Detect BPM from aubio's beat timestamps: find the longest run of
    stable inter-beat intervals and fit the tempo grid over its full span.
    Far more accurate (±0.05 BPM) than aubio's single tempo estimate,
    which is routinely off by ~1%."""
    import statistics
    import tempfile

    dur = audio_duration(path) or 0
    start = 30 if dur > 180 else 0
    length = min(120, max(30, dur - start)) if dur else 120
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=UPLOAD_DIR)
    tmp.close()
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "quiet",
                "-ss",
                str(start),
                "-t",
                str(length),
                "-i",
                path,
                "-ac",
                "1",
                "-ar",
                "44100",
                "-y",
                tmp.name,
            ],
            capture_output=True,
            timeout=300,
        )
        if r.returncode != 0:
            return None
        r = subprocess.run(
            ["aubio", "beat", "-i", tmp.name], capture_output=True, text=True, timeout=300
        )
        beats = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line:
                try:
                    beats.append(float(line.split()[0]))
                except ValueError:
                    pass
        if len(beats) < 12:
            # Too few beats for a grid fit — fall back to aubio tempo.
            r = subprocess.run(
                ["aubio", "tempo", "-i", tmp.name], capture_output=True, text=True, timeout=300
            )
            m = re.search(r"([\d.]+)\s*bpm", r.stdout or "")
            return float(m.group(1)) if m else None
        ibis = [b - a for a, b in zip(beats, beats[1:])]
        med = statistics.median(ibis)
        if med <= 0:
            return None
        # Longest consecutive run of stable intervals (skips breakdowns
        # and FX sections where the beat tracker wanders).
        best_start = best_len = run_start = run_len = 0
        for i, ibi in enumerate(ibis):
            if abs(ibi - med) / med < 0.08:
                if run_len == 0:
                    run_start = i
                run_len += 1
                if run_len > best_len:
                    best_start, best_len = run_start, run_len
            else:
                run_len = 0
        if best_len < 8:
            return 60.0 / med
        span = beats[best_start + best_len] - beats[best_start]
        return 60.0 * best_len / span
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


# —— Key detection (TKEY tag first, else Essentia's EDM-tuned extractor) ——
# v3: Essentia's EDM-tuned extractor, with file-signature cache keys.
# Essentia reports sharps; normalise to the flat spelling DJs/Rekordbox use.
_KEY_FLAT = {"C#": "Db", "D#": "Eb", "F#": "Gb", "G#": "Ab", "A#": "Bb"}


def _tag_key(path):
    """Key from an existing TKEY tag (e.g. written by Rekordbox)."""
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        if mf and mf.tags and "TKEY" in mf.tags:
            k = str(mf.tags["TKEY"].text[0]).strip()
            return k or None
    except Exception:
        pass
    return None


def _detect_key(path):
    """Detect key with Essentia's KeyExtractor using the 'edmm' profile
    (tuned for electronic dance music). Analyses a stable window, skipping
    the intro. Validated at 9/10 vs Rekordbox on this library."""
    import essentia.standard as es
    import numpy as np

    dur = audio_duration(path) or 0
    start = 20 if dur > 200 else 0
    length = min(180, max(30, dur - start)) if dur else 180
    sr = 44100
    try:
        r = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "quiet",
                "-ss",
                str(start),
                "-t",
                str(length),
                "-i",
                path,
                "-ac",
                "1",
                "-ar",
                str(sr),
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            timeout=300,
        )
        x = np.frombuffer(r.stdout, dtype=np.float32).copy()
        if x.size < sr * 10:
            return None
        key, scale, _strength = es.KeyExtractor(profileType="edmm")(x)
        if not key:
            return None
        key = _KEY_FLAT.get(key, key)
        return key + ("m" if scale == "minor" else "")
    except Exception:
        logger.warning(f"key detection failed for {os.path.basename(path)}", exc_info=True)
        return None


def track_key(reference, analyze=True):
    """Musical key of a library track: TKEY tag if present, else Essentia
    EDM key detection. Cached persistently."""
    path, cache_key = _analysis_target(reference)
    if cache_key is None:
        return None
    cached = _metadata_stores.key.get(cache_key)
    if cached is not MISSING:
        return cached
    if not analyze:
        return None
    key = None
    if path and os.path.isfile(path):
        key = _tag_key(path) or _detect_key(path)
    _metadata_stores.key.set(cache_key, key)
    return key


def track_bpm(reference, analyze=True):
    """Original BPM of a library track: TBPM tag if present, else aubio
    analysis. Folded into the 60–190 range typical of DJ material."""
    path, cache_key = _analysis_target(reference)
    if cache_key is None:
        return None
    cached = _metadata_stores.bpm.get(cache_key)
    if cached is not MISSING:
        return cached
    if not analyze:
        return None
    bpm, from_tag = None, False
    if path and os.path.isfile(path):
        bpm = _tag_bpm(path)
        from_tag = bpm is not None
        if bpm is None:
            bpm = _detect_bpm(path)
    if bpm:
        if bpm < 60:
            bpm *= 2
        elif bpm > 190:
            bpm /= 2
        if not (40 <= bpm <= 250):
            bpm = None
        elif from_tag:
            bpm = round(bpm, 1)  # tags are authoritative — don't snap
        else:
            bpm = _snap_bpm(bpm)
    _metadata_stores.bpm.set(cache_key, bpm)
    return bpm


_metadata_pending = set()
_metadata_pending_lock = threading.Lock()


def submit_metadata(reference):
    path = _library_path(reference)
    if not path:
        return False
    with _metadata_pending_lock:
        if path in _metadata_pending:
            return True
        _metadata_pending.add(path)
    try:
        _metadata_queue.put_nowait(path)
    except queue.Full:
        with _metadata_pending_lock:
            _metadata_pending.discard(path)
        logger.warning("metadata queue full; deferred analysis for %s", os.path.basename(path))
        return False
    return True


def metadata_worker_loop(stop_event=None):
    while stop_event is None or not stop_event.is_set():
        try:
            path = _metadata_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        try:
            library_track_duration(path, analyze=True)
            track_bpm(path, analyze=True)
            track_key(path, analyze=True)
        except Exception:
            logger.exception("metadata analysis failed for %s", os.path.basename(path))
        finally:
            with _metadata_pending_lock:
                _metadata_pending.discard(path)
            _metadata_queue.task_done()


_worker_runtime = WorkerRuntime()


def start_workers():
    _worker_runtime.start("panako", worker_loop, _panako_job_queue, "panako")
    _worker_runtime.start(
        "download",
        worker_loop,
        _download_job_queue,
        "download",
    )
    _worker_runtime.start("metadata", metadata_worker_loop)


def stop_workers(timeout=5):
    _worker_runtime.stop(timeout)


def worker_health():
    return _worker_runtime.health()


def collapse_matches(matches, min_segments=2, duration=None, analyze_metadata=False):
    """Collapse segment matches into tracklist entries, inserting
    'unidentified' placeholders where the mix has coverage gaps.

    A matched track that stops matching mid-way (blends, FX, loops) is
    assumed to keep playing for its remaining runtime: gaps covered by
    that expected continuation are NOT flagged as unidentified."""
    entries, current = [], None
    for m in sorted(matches, key=lambda m: m["query_start"]):
        match_reference = m.get("match_path") or m["match_name"]
        if current and match_reference == current["reference"]:
            current["end"] = m["query_stop"]
            current["segments"] += 1
            current["time_factors"].append(m["time_factor"])
            current["match_stop"] = max(current["match_stop"], m.get("match_stop") or 0)
        else:
            if current:
                entries.append(current)
            current = {
                "file": m["match_name"],
                "reference": match_reference,
                "start": m["query_start"],
                "end": m["query_stop"],
                "segments": 1,
                "time_factors": [m["time_factor"]],
                "match_stop": m.get("match_stop") or 0,
            }
    if current:
        entries.append(current)

    accepted = []
    for e in entries:
        if e["segments"] < min_segments:
            continue
        avg_tf = sum(e["time_factors"]) / len(e["time_factors"])
        # Expected continuation: how far into the mix this track would
        # plausibly still be playing, given where matching stopped within
        # the source file and the source file's total length.
        expected_end = e["end"]
        track_dur = library_track_duration(
            e["reference"],
            analyze=analyze_metadata,
        )
        if track_dur and e["match_stop"]:
            remaining = max(0.0, track_dur - e["match_stop"])
            tf = avg_tf if avg_tf > 0.5 else 1.0
            expected_end = e["end"] + remaining / tf
        bpm = track_bpm(e["reference"], analyze=analyze_metadata)
        played = None
        if bpm:
            played = bpm * avg_tf
            # The time factor is quantized to 0.1% — snap a near-integer
            # played BPM (DJs sync to round numbers) to the clean value.
            if abs(played - round(played)) <= 0.15:
                played = float(round(played))
            else:
                played = round(played, 1)
        accepted.append(
            {
                "title": os.path.splitext(e["file"])[0],
                "start": e["start"],
                "end": e["end"],
                "expected_end": expected_end,
                "tempo_pct": round((avg_tf - 1.0) * 100, 1),
                "bpm": bpm,
                "played_bpm": played,
                "key": track_key(e["reference"], analyze=analyze_metadata),
                "segments": e["segments"],
                "unidentified": False,
            }
        )

    # Insert placeholders only for gaps NOT covered by the previous
    # track's expected continuation.
    final, prev_end, prev_expected = [], 0.0, 0.0
    for e in accepted:
        gap_start = max(prev_end, min(prev_expected, e["start"]))
        if e["start"] - gap_start > UNIDENTIFIED_GAP_S:
            final.append(
                {
                    "title": None,
                    "start": gap_start,
                    "end": e["start"],
                    "tempo_pct": 0,
                    "segments": 0,
                    "unidentified": True,
                }
            )
        final.append(e)
        prev_end = max(prev_end, e["end"])
        prev_expected = max(prev_expected, e["expected_end"])
    if duration:
        tail_start = max(prev_end, min(prev_expected, duration))
        if duration - tail_start > UNIDENTIFIED_GAP_S:
            final.append(
                {
                    "title": None,
                    "start": tail_start,
                    "end": duration,
                    "tempo_pct": 0,
                    "segments": 0,
                    "unidentified": True,
                }
            )
    for e in final:
        e.pop("end", None)
        e.pop("expected_end", None)
    return final


# —— Retained mix (only the most-recent, for slice playback) ——
_mix_lock = threading.Lock()
_current_mix = {"job": None, "path": None}


def register_mix(jid, path):
    """Keep only the newest scanned mix on disk; delete the previous one."""
    with _mix_lock:
        old = _current_mix["path"]
        _current_mix["job"], _current_mix["path"] = jid, path
    if old and old != path:
        try:
            os.remove(old)
        except OSError:
            pass


def get_mix_path(jid):
    with _mix_lock:
        if _current_mix["job"] == jid:
            return _current_mix["path"]
    return None


def clear_mix():
    with _mix_lock:
        old = _current_mix["path"]
        _current_mix["job"], _current_mix["path"] = None, None
    if old:
        try:
            os.remove(old)
        except OSError:
            pass


def discard_mix(jid, path):
    """Remove a failed mix without clearing a newer successful scan."""
    with _mix_lock:
        if _current_mix["job"] == jid:
            _current_mix["job"], _current_mix["path"] = None, None
    try:
        os.remove(path)
    except OSError:
        pass


# —— Scan history (view-only recall of past tracklists) ——
HISTORY_DIR = os.path.join(DB_DIR, "scan_history")
os.makedirs(HISTORY_DIR, exist_ok=True)
HISTORY_MAX = 50
_history_lock = threading.Lock()


def _history_file(hid):
    safe = re.sub(r"[^0-9a-f]", "", str(hid))[:32]
    return os.path.join(HISTORY_DIR, safe + ".json") if safe else None


def save_history(hid, name, duration, tracklist, waveform):
    rec = {
        "id": hid,
        "name": name,
        "date": time.time(),
        "duration": duration,
        "tracklist": tracklist,
        "waveform": waveform,
    }
    with _history_lock:
        f = _history_file(hid)
        if f:
            atomic_write_json(f, rec)
        # prune oldest beyond HISTORY_MAX
        entries = [
            os.path.join(HISTORY_DIR, x) for x in os.listdir(HISTORY_DIR) if x.endswith(".json")
        ]
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for stale in entries[HISTORY_MAX:]:
            try:
                os.remove(stale)
            except OSError:
                pass


def find_duplicate(fpath, jid=None):
    """Check a track against the existing library using the same
    segment-by-segment matcher as identification (`monitor`), so
    near-identical files are caught even across formats/bitrates. Returns
    the title of a strongly-matching already-indexed track, or None."""
    if jid is not None:
        r = run_panako_cancellable(["monitor", f"STRATEGY={STRATEGY}", fpath], jid, timeout=600)
    else:
        r = run_panako(["monitor", f"STRATEGY={STRATEGY}", fpath], timeout=600)
    tally = {}
    for line in (r.stdout or "").splitlines():
        parsed = parse_monitor_line(line)
        if parsed and parsed != "nomatch":
            reference = parsed.get("match_path") or parsed["match_name"]
            m = tally.setdefault(reference, {"segments": 0, "score": 0.0})
            m["segments"] += 1
            m["score"] += parsed["score"]
    if not tally:
        return None
    best_name, best = max(tally.items(), key=lambda kv: (kv[1]["segments"], kv[1]["score"]))
    # Require a solid multi-segment match to call it a duplicate.
    if best["segments"] >= 3:
        return os.path.splitext(os.path.basename(best_name))[0]
    return None


def _library_audio_file(path):
    """Return a canonical in-library audio path, or None."""
    real = os.path.realpath(path)
    root = os.path.realpath(MUSIC_ROOT)
    if not (
        real.startswith(root + os.sep)
        and os.path.isfile(real)
        and real.lower().endswith(AUDIO_EXTS)
    ):
        return None
    return real


def _all_library_audio_files():
    files = []
    excluded = {"mixid_app", "__pycache__", "node_modules", "logs", "cookies"}
    for root, dirs, names in os.walk(MUSIC_ROOT):
        dirs[:] = [name for name in dirs if not name.startswith(".") and name not in excluded]
        for name in names:
            candidate = _library_audio_file(os.path.join(root, name))
            if candidate:
                files.append(candidate)
    return sorted(set(files))


def _reset_panako_lmdb(candidates):
    """Remove only Panako's LMDB directory before a full rebuild."""
    db_root = os.path.realpath(DB_DIR)
    target = os.path.realpath(PANAKO_LMDB_DIR)
    if os.path.dirname(target) != db_root or os.path.basename(target) != "panako_db":
        raise RuntimeError("refusing to reset an unexpected Panako DB path")
    if os.path.isdir(target):
        shutil.rmtree(target)
    elif os.path.exists(target):
        os.remove(target)
    os.makedirs(target, exist_ok=True)
    manifest_begin_rebuild(candidates)


def do_index(jid, folder):
    folder_files = sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(AUDIO_EXTS) and os.path.isfile(os.path.join(folder, f))
    )
    if not folder_files:
        update_job(jid, status="error", error="No audio files in folder")
        return

    snapshot = manifest_snapshot()
    stale = stale_manifest_files(snapshot)
    rebuilt = bool(stale)
    if rebuilt:
        # A path-only legacy manifest or changed/deleted bytes cannot be
        # reconciled safely one resource at a time. Rebuild the dedicated
        # LMDB from every still-present indexed file plus the selected folder.
        if snapshot.get("invalid"):
            candidates = set(_all_library_audio_files())
            backup = quarantine_invalid_manifest()
            detail = f"; preserved as {os.path.basename(backup)}" if backup else ""
            job_log(jid, f"Manifest was invalid; rebuilding every library fingerprint{detail}")
        else:
            candidates = set(folder_files)
            candidates.update(snapshot.get("files", {}).keys())
        files = sorted(filter(None, (_library_audio_file(p) for p in candidates)))
        job_log(jid, f"Library changed; rebuilding {len(files)} fingerprints safely")
        _reset_panako_lmdb(files)
    else:
        files = folder_files

    already = manifest_files()
    done, failed, duplicates = 0, [], []

    def _finish(status):
        update_job(
            jid,
            status=status,
            progress=1.0 if status == "done" else None,
            detail="",
            result={
                "indexed": done,
                "failed": failed,
                "duplicates": duplicates,
                "total": len(files),
                "rebuilt": rebuilt,
                "cancelled": status == "cancelled",
            },
        )

    for i, fpath in enumerate(files):
        if is_cancelled(jid):
            job_log(jid, f"Indexing cancelled ({done}/{len(files)} done)")
            _finish("cancelled")
            return
        fname = os.path.basename(fpath)
        update_job(jid, progress=i / len(files), detail=fname)
        if fpath in already:
            done += 1
            submit_metadata(fpath)
            continue
        # Duplicate check: query the track against the existing DB first.
        dup_of = find_duplicate(fpath, jid=jid)
        if is_cancelled(jid):
            job_log(jid, f"Indexing cancelled ({done}/{len(files)} done)")
            _finish("cancelled")
            return
        if dup_of:
            duplicates.append({"file": fname, "duplicate_of": dup_of})
            manifest_remove(fpath)
            logger.info(f"index: skipping duplicate {fname} (matches {dup_of})")
            continue
        r = run_panako_cancellable(["store", f"STRATEGY={STRATEGY}", fpath], jid, timeout=600)
        if is_cancelled(jid):
            manifest_remove(fpath)
            job_log(jid, f"Indexing cancelled ({done}/{len(files)} done)")
            _finish("cancelled")
            return
        if r.returncode == 0:
            manifest_add(fpath)
            done += 1
            submit_metadata(fpath)
        else:
            failed.append(fname)
            manifest_remove(fpath)
            logger.warning(
                f"panako store failed for {fpath}: {(r.stderr or r.stdout or '').strip()[-300:]}"
            )
    _finish("done")


def _fmt_clock(seconds):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "0:00"
    m, s = divmod(max(0, seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def do_identify(jid, mix_path, min_segments, mix_name=None):
    stale = manifest_stale_paths()
    if stale:
        update_job(
            jid,
            status="error",
            error="Fingerprint library changed while this job was queued; re-index it",
        )
        discard_mix(jid, mix_path)
        return
    duration = audio_duration(mix_path)
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        update_job(jid, status="error", error="Could not determine mix duration")
        discard_mix(jid, mix_path)
        return
    if duration > MAX_AUDIO_DURATION_SECONDS:
        update_job(
            jid,
            status="error",
            error=f"Mix exceeds the {MAX_AUDIO_DURATION_SECONDS / 3600:g} hour limit",
        )
        discard_mix(jid, mix_path)
        return
    job_timeout = audio_process_timeout(duration)
    deadline = time.monotonic() + job_timeout
    register_mix(jid, mix_path)
    logger.info(
        f"identify start: {os.path.basename(mix_path)} "
        f"({duration and round(duration) or '?'}s, min_segments={min_segments})"
    )
    # Render the waveform first so the GUI can draw it while scanning.
    update_job(jid, detail="Rendering waveform...")
    waveform = compute_peaks(
        mix_path,
        buckets=700,
        duration=duration,
        timeout_seconds=min(WAVEFORM_TIMEOUT_SECONDS, max(1, deadline - time.monotonic())),
    )
    if time.monotonic() >= deadline:
        update_job(jid, status="error", error="Identification job timed out")
        discard_mix(jid, mix_path)
        return
    if is_cancelled(jid):
        update_job(jid, status="cancelled", detail="", progress=None)
        discard_mix(jid, mix_path)
        return
    update_job(jid, waveform=waveform, wf_duration=duration)
    update_job(jid, detail="Fingerprinting mix segments...")
    proc = subprocess.Popen(
        ["panako", "monitor", f"STRATEGY={STRATEGY}", mix_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    _job_service.register_process(jid, proc)
    matches = []
    seen_tracks = set()
    cancelled = False
    timeout = max(1, deadline - time.monotonic())
    # Heartbeat: keep an elapsed-time detail ticking so the final-segment
    # tail (Panako finishing after its last output line) never looks frozen.
    scan_active = threading.Event()
    scan_active.set()
    scan_started = time.monotonic()
    scan_pos = [0.0]

    def _heartbeat():
        while scan_active.is_set():
            time.sleep(1.0)
            if not scan_active.is_set():
                break
            elapsed = int(time.monotonic() - scan_started)
            n = len(seen_tracks)
            update_job(
                jid,
                detail=(
                    f"Scanning {_fmt_clock(scan_pos[0])} / {_fmt_clock(duration)}"
                    f" · {n} track{'' if n == 1 else 's'} found · {elapsed}s"
                ),
            )

    hb = threading.Thread(target=_heartbeat, name="scan-heartbeat", daemon=True)
    hb.start()
    try:
        for line in iter_lines_with_deadline(proc, timeout):
            if is_cancelled(jid):
                cancelled = True
                terminate_process_tree(proc)
                break
            parsed = parse_monitor_line(line)
            if parsed and parsed != "nomatch":
                matches.append(parsed)
                ref = parsed.get("match_path") or parsed["match_name"]
                name = os.path.splitext(os.path.basename(ref))[0]
                if name not in seen_tracks:
                    seen_tracks.add(name)
                    job_log(jid, f"♪ {_fmt_clock(parsed.get('query_start', 0))} — {name}")
            seg = re.search(r"-([\d.]+)_[\d.]+ ", line)
            if seg and duration:
                scan_pos[0] = float(seg.group(1))
                update_job(jid, progress=min(scan_pos[0] / duration, 1.0))
    except ProcessDeadlineExceeded:
        logger.error("identify timed out after %.0fs: %s", timeout, os.path.basename(mix_path))
        update_job(
            jid,
            status="error",
            error=f"Fingerprint scan timed out after {timeout / 60:.0f} minutes",
        )
        discard_mix(jid, mix_path)
        return
    except Exception:
        terminate_process_tree(proc)
        discard_mix(jid, mix_path)
        raise
    finally:
        scan_active.clear()
        _job_service.clear_process(jid, proc)
        if proc.stdout:
            proc.stdout.close()
    if cancelled or is_cancelled(jid):
        job_log(jid, "Mix scan cancelled")
        update_job(jid, status="cancelled", detail="", progress=None)
        discard_mix(jid, mix_path)
        return
    # NB: the mix file is intentionally kept (register_mix) for slice
    # playback; it's replaced when the next mix is scanned or cleared.
    if proc.returncode != 0 and not matches:
        update_job(jid, status="error", error="Panako monitor failed")
        discard_mix(jid, mix_path)
        return
    if manifest_stale_paths():
        update_job(
            jid,
            status="error",
            error="Fingerprint library changed during identification; re-index it",
        )
        discard_mix(jid, mix_path)
        return
    update_job(jid, progress=1.0, detail="Building tracklist…")
    tracklist = collapse_matches(
        matches,
        min_segments,
        duration,
        analyze_metadata=False,
    )
    for reference in {match.get("match_path") or match["match_name"] for match in matches}:
        submit_metadata(reference)
    if time.monotonic() >= deadline:
        update_job(jid, status="error", error="Identification job timed out")
        discard_mix(jid, mix_path)
        return
    logger.info(
        f"identify done: {len(matches)} segment matches -> "
        f"{sum(1 for t in tracklist if not t['unidentified'])} tracks, "
        f"{sum(1 for t in tracklist if t['unidentified'])} unidentified sections"
    )
    try:
        save_history(jid, mix_name or os.path.basename(mix_path), duration, tracklist, waveform)
    except Exception:
        discard_mix(jid, mix_path)
        raise
    update_job(
        jid,
        status="done",
        progress=1.0,
        detail="",
        result={"tracklist": tracklist, "duration": duration, "waveform": waveform, "job": jid},
    )


# ============================================================
# SCRipper: downloader
# ============================================================
COOKIE_FILE = SETTINGS.cookie_file


def write_cookie_file(text):
    """Write cookies.txt with owner-only permissions — the file is a live
    session, so keep it off other users on the host."""
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    # Create with 0600 from the start (umask-independent) to avoid a window
    # where the session is world-readable.
    fd = os.open(COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(COOKIE_FILE, 0o600)
    except OSError:
        pass  # best-effort on filesystems that don't support chmod


def build_ydl_opts(folder):
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        # Each worker overrides this with a job-private, ID-based path.
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),
        "writethumbnail": False,
        "download_archive": os.path.join(folder, ".download_archive.txt"),
        "retries": 3,
        "sleep_requests": 0.5,
        "fixup": "never",
        "postprocessors": [],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if os.path.isfile(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts


class JobCancelled(Exception):
    """Raised inside a running job when the user cancels it."""


def make_progress_hook(jid):
    def hook(d):
        if is_cancelled(jid):
            # Abort the in-flight yt-dlp download immediately.
            raise JobCancelled()
        fname = os.path.basename(d.get("filename") or "")
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            got = d.get("downloaded_bytes", 0)
            if total:
                job_active(jid, fname, f"{got / total * 100:.0f}% of {total / 1048576:.1f} MB")
            else:
                job_active(jid, fname, f"{got / 1048576:.1f} MB")
        elif d["status"] == "finished":
            job_active(jid, fname, None)

    return hook


def download_one(url, folder, ydl_opts, jid=None):
    """Returns ('downloaded'|'skipped', display_name)."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if jid:
        job_log(jid, f"▶ starting: {slug}")
    workdir = tempfile.mkdtemp(prefix=".scripper-download-", dir=folder)
    try:
        private_opts = {
            **ydl_opts,
            "outtmpl": os.path.join(workdir, "%(id).100s.%(ext)s"),
        }
        with yt_dlp.YoutubeDL(private_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return "skipped", url
            title = info.get("title", "Unknown")
            inp = ydl.prepare_filename(info)
        if not os.path.isfile(inp):
            if jid:
                job_log(jid, f"↷ skipped (already in archive): {title}")
            return "skipped", title
        if jid:
            job_log(jid, f"♪ converting to 320kbps mp3: {title}")
        converted = os.path.join(workdir, uuid.uuid4().hex + ".mp3")
        convert_to_mp3(inp, converted)
        embed_metadata(
            converted,
            title,
            info.get("uploader", "Unknown"),
            (info.get("thumbnails") or [{}])[-1].get("url"),
        )
        preferred = os.path.join(folder, safe_mp3_name(title))
        final_path = promote_unique(converted, preferred)
        final_title = os.path.splitext(os.path.basename(final_path))[0]
        if jid:
            job_log(jid, f"✔ done: {final_title}")
        return "downloaded", final_title
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def friendly_dl_error(url, exc):
    return friendly_download_error(url, exc)


def do_download(jid, urls, folder, index_after):
    os.makedirs(folder, exist_ok=True)
    ydl_opts = build_ydl_opts(folder)
    downloaded, skipped, failed = [], [], []
    done_count = [0]
    lock = threading.Lock()

    def work(u):
        if is_cancelled(jid):
            return
        try:
            opts = {**ydl_opts, "progress_hooks": [make_progress_hook(jid)]}
            status, name = download_one(u, folder, opts, jid=jid)
            with lock:
                (downloaded if status == "downloaded" else skipped).append(name)
        except JobCancelled:
            return
        except Exception as e:
            if is_cancelled(jid):
                return
            line = friendly_dl_error(u, e)
            job_log(jid, f"✘ failed: {line}")
            logger.warning(f"download failed: {u}: {e}")
            with lock:
                failed.append(line)
        finally:
            with lock:
                done_count[0] += 1
                if not is_cancelled(jid):
                    update_job(
                        jid,
                        progress=done_count[0] / len(urls),
                        detail=f"{done_count[0]}/{len(urls)} tracks",
                    )

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        list(ex.map(work, urls))

    if is_cancelled(jid):
        job_log(jid, f"Downloads cancelled ({len(downloaded)} completed)")
        update_job(
            jid,
            status="cancelled",
            detail="",
            progress=None,
            result={
                "downloaded": sorted(downloaded),
                "skipped": sorted(skipped),
                "failed": failed,
                "total": len(urls),
                "index_job": None,
                "cancelled": True,
            },
        )
        return

    result = {
        "downloaded": sorted(downloaded),
        "skipped": sorted(skipped),
        "failed": failed,
        "total": len(urls),
        "index_job": None,
    }
    if index_after and (downloaded or skipped):
        ijid = submit_job(
            "index",
            os.path.relpath(folder, MUSIC_ROOT),
            _panako_job_queue,
            do_index,
            (folder,),
        )
        result["index_job"] = ijid
        if ijid is None:
            result["index_error"] = "Panako queue is full; index this folder later"
    update_job(jid, status="done", progress=1.0, detail="", result=result)


# ============================================================
# Routes — shared
# ============================================================
def index():
    return send_from_directory("static", "index.html")


def api_job(jid):
    j = get_job(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(j)


def api_cancel_job(jid):
    if not get_job(jid):
        return jsonify({"error": "unknown job"}), 404
    accepted = _job_service.request_cancel(jid)
    if not accepted:
        return jsonify({"cancelled": False, "reason": "already finished"}), 409
    return jsonify({"cancelled": True})


def api_active_jobs():
    return jsonify({"jobs": _job_service.active_jobs()})


def api_health():
    return jsonify({"status": "ok"})


def api_ready():
    expected_workers = {"panako", "download", "metadata"}
    workers = worker_health()
    workers_ok = os.environ.get("SCRIPPER_START_WORKERS", "1") == "0" or (
        expected_workers <= set(workers) and all(workers[name] for name in expected_workers)
    )
    storage_ok = all(
        os.path.isdir(path) and os.access(path, os.W_OK) for path in (UPLOAD_DIR, DB_DIR, LOG_DIR)
    )
    panako_ok = shutil.which("panako") is not None
    ready = workers_ok and storage_ok and panako_ok
    return jsonify(
        {
            "status": "ready" if ready else "not-ready",
            "workers": workers,
            "storage": storage_ok,
            "panako": panako_ok,
            "queues": {
                "panako": _panako_job_queue.qsize(),
                "download": _download_job_queue.qsize(),
                "metadata": _metadata_queue.qsize(),
            },
        }
    ), 200 if ready else 503


BROWSER_PROFILE_DIR = SETTINGS.browser_profile_dir
CAPTURE_DOMAINS = ("soundcloud.com", "youtube.com", "google.com")

# —— On-demand login browser (via an isolated, fixed-purpose controller) ——
LOGIN_BROWSER_PORT = 5800
BROWSER_CONTROLLER_URL = SETTINGS.browser_controller_url


def _browser_controller(path, method="GET", timeout=30):
    """Call the narrow browser controller API.

    The main application deliberately has no Docker socket or Docker CLI.
    """
    try:
        r = http_requests.request(method, BROWSER_CONTROLLER_URL + path, timeout=timeout)
    except http_requests.exceptions.RequestException as e:
        raise RuntimeError(f"login browser controller unavailable: {e}") from e
    try:
        data = r.json()
    except ValueError as e:
        raise RuntimeError("login browser controller returned invalid data") from e
    if not r.ok or data.get("error"):
        raise RuntimeError(
            data.get("error") or f"login browser controller returned HTTP {r.status_code}"
        )
    return data


def login_browser_running():
    return bool(_browser_controller("/status").get("running"))


def start_login_browser():
    """Spin up the login browser through the isolated controller."""
    data = _browser_controller("/start", method="POST", timeout=310)
    if not data.get("running"):
        raise RuntimeError("login browser did not start")


def stop_login_browser():
    data = _browser_controller("/stop", method="POST", timeout=70)
    if data.get("running"):
        raise RuntimeError("login browser is still running")


def api_login_browser_start():
    try:
        start_login_browser()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"running": True, "port": LOGIN_BROWSER_PORT})


def api_login_browser_stop():
    try:
        stop_login_browser()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    return jsonify({"running": False})


def api_login_browser_status():
    try:
        return jsonify({"running": login_browser_running()})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503


def find_cookie_db():
    """Locate cookies.sqlite in the login-browser's Firefox profile."""
    for root, _dirs, files in os.walk(BROWSER_PROFILE_DIR):
        if "cookies.sqlite" in files:
            return os.path.join(root, "cookies.sqlite")
    return None


def capture_browser_cookies():
    """Read the login-browser's Firefox cookie DB and write a Netscape
    cookies.txt for the domains SCRipper cares about. Returns per-domain
    cookie counts."""
    import shutil
    import sqlite3
    import tempfile

    db = find_cookie_db()
    if not db:
        raise RuntimeError(
            "No login-browser profile yet. Click 'Open login browser' and "
            "log in first, then capture."
        )

    # Firefox holds the DB locked with WAL; copy DB + sidecars, then read.
    tmpdir = tempfile.mkdtemp()
    try:
        tmp_db = os.path.join(tmpdir, "cookies.sqlite")
        shutil.copy2(db, tmp_db)
        for ext in ("-wal", "-shm"):
            side = db + ext
            if os.path.exists(side):
                shutil.copy2(side, tmp_db + ext)
        conn = sqlite3.connect(tmp_db)
        rows = conn.execute(
            "SELECT host, path, isSecure, expiry, name, value FROM moz_cookies"
        ).fetchall()
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    counts = {d: 0 for d in CAPTURE_DOMAINS}
    lines = ["# Netscape HTTP Cookie File", "# Captured from SCRipper login browser"]
    for host, path, is_secure, expiry, name, value in rows:
        bare = host.lstrip(".")
        matched = next((d for d in CAPTURE_DOMAINS if bare == d or bare.endswith("." + d)), None)
        if not matched:
            continue
        counts[matched] += 1
        include_sub = "TRUE" if host.startswith(".") else "FALSE"
        secure = "TRUE" if is_secure else "FALSE"
        lines.append(f"{host}\t{include_sub}\t{path}\t{secure}\t{expiry or 0}\t{name}\t{value}")

    if not any(counts.values()):
        raise RuntimeError(
            "No SoundCloud/YouTube cookies found in the login browser. Open it and log in first."
        )

    write_cookie_file("\n".join(lines) + "\n")
    return counts


def looks_like_netscape_cookies(text):
    """Loose validation: Netscape header, or at least one 7-field
    tab-separated cookie line."""
    if "# Netscape HTTP Cookie File" in text[:512]:
        return True
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if len(line.split("\t")) >= 7:
            return True
    return False


def api_cookies_capture():
    """One-click capture from the on-demand login browser, then shut it
    down so it isn't left running."""
    try:
        counts = capture_browser_cookies()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    stopped = False
    stop_error = None
    try:
        if login_browser_running():
            stop_login_browser()
            stopped = True
    except RuntimeError as e:
        stop_error = str(e)
        logger.warning("cookies captured but login browser stop failed: %s", e)
    return jsonify(
        {
            "present": True,
            "counts": counts,
            "browser_stopped": stopped,
            "browser_stop_error": stop_error,
        }
    )


def api_cookies():
    """GET: status. POST: upload a cookies.txt. DELETE: remove it."""
    import time as _time

    if request.method == "GET":
        if os.path.isfile(COOKIE_FILE):
            age_days = (_time.time() - os.path.getmtime(COOKIE_FILE)) / 86400
            return jsonify({"present": True, "age_days": round(age_days, 1)})
        return jsonify({"present": False})

    if request.method == "DELETE":
        try:
            os.remove(COOKIE_FILE)
        except FileNotFoundError:
            pass
        return jsonify({"present": False})

    f = request.files.get("cookies")
    if not f:
        return jsonify({"error": "no file"}), 400
    raw = f.read(2 * 1024 * 1024)  # cookie files are tiny; cap at 2MB
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return jsonify({"error": "not a text file"}), 400
    if text.lstrip().startswith(("{", "[")):
        return jsonify(
            {
                "error": "This looks like a JSON export. Export in Netscape/cookies.txt "
                "format instead (the 'Get cookies.txt LOCALLY' extension does "
                "this by default)."
            }
        ), 400
    if not looks_like_netscape_cookies(text):
        return jsonify({"error": "Not a valid cookies.txt (Netscape format) file."}), 400
    write_cookie_file(text)
    return jsonify({"present": True, "age_days": 0.0})


def api_folders():
    """Top-level music folders (for destination pickers)."""
    folders = []
    for name in sorted(os.listdir(MUSIC_ROOT), key=str.lower):
        full = os.path.join(MUSIC_ROOT, name)
        if name.startswith(".") or not os.path.isdir(full):
            continue
        try:
            n = sum(1 for f in os.listdir(full) if f.lower().endswith(AUDIO_EXTS))
        except PermissionError:
            n = 0
        folders.append({"name": name, "audio_count": n})
    return jsonify({"folders": folders})


# ============================================================
# Routes — MixID
# ============================================================
def api_resolve_folder():
    """Map a folder name (from the native OS picker) to a path under
    /music. Searches 3 levels deep; case-insensitive."""
    try:
        name = validate_path_request(request.get_json(silent=True), "name").strip()
    except RequestValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    if not name:
        return jsonify({"error": "no folder name"}), 400
    hits = []
    for root, dirs, _files in os.walk(MUSIC_ROOT):
        depth = os.path.relpath(root, MUSIC_ROOT).count(os.sep)
        if depth >= 3:
            dirs[:] = []
            continue
        dirs[:] = [
            d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "node_modules")
        ]
        for d in dirs:
            if d.lower() == name.lower():
                hits.append(os.path.relpath(os.path.join(root, d), MUSIC_ROOT))
    return jsonify({"matches": hits})


def api_index():
    try:
        rel = validate_path_request(request.get_json(silent=True))
    except RequestValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    folder = safe_music_path(rel)
    if folder is None or not os.path.isdir(folder):
        return jsonify({"error": "invalid path"}), 400
    jid = submit_job("index", rel or "/", _panako_job_queue, do_index, (folder,))
    if jid is None:
        return jsonify({"error": "Panako queue is full; try again later"}), 429
    return jsonify({"job": jid})


def api_identify():
    stale = manifest_stale_paths()
    if stale:
        if INVALID_MANIFEST_MARKER in stale:
            return jsonify(
                {
                    "error": (
                        "The fingerprint manifest is missing or invalid. "
                        "Run an index job to rebuild the full library safely."
                    )
                }
            ), 409
        return jsonify(
            {
                "error": (
                    f"The fingerprint library has {len(stale)} changed or "
                    "legacy file(s). Re-index a library folder first; MixID "
                    "will rebuild the index safely."
                )
            }
        ), 409
    if _panako_job_queue.full():
        return jsonify({"error": "Panako queue is full; try again later"}), 429
    f = request.files.get("mix")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    if not f.filename.lower().endswith(AUDIO_EXTS):
        return jsonify({"error": "unsupported audio file type"}), 400
    try:
        min_segments = int(request.form.get("min_segments", 2))
    except (TypeError, ValueError):
        min_segments = 2
    min_segments = max(1, min(min_segments, 20))
    # A new scan supersedes any earlier one still running or queued (e.g. a
    # scan left behind when the page was closed), so it never blocks forever
    # on "Waiting for previous job". Only the latest mix is kept anyway.
    superseded = _job_service.active_ids_of_type("identify")
    for old in superseded:
        _job_service.request_cancel(old)
    if superseded:
        logger.info(f"superseding {len(superseded)} prior scan(s) for a new one")
    safe_name = re.sub(r"[^\w .()\[\]&-]", "_", os.path.basename(f.filename))
    dest = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{safe_name}")
    f.save(dest)
    jid = submit_job(
        "identify",
        f.filename,
        _panako_job_queue,
        do_identify,
        (dest, min_segments, f.filename),
    )
    if jid is None:
        try:
            os.remove(dest)
        except OSError:
            pass
        return jsonify({"error": "Panako queue is full; try again later"}), 429
    return jsonify({"job": jid})


def api_mix_stream():
    """Stream the retained (most-recent) mix with Range support, so
    tracklist slices can be played from their timestamps."""
    jid = request.args.get("job", "")
    path = get_mix_path(jid)
    if not path or not os.path.isfile(path):
        return jsonify({"error": "mix not available"}), 404
    return send_file(path, conditional=True)


def api_mix_delete():
    """Drop the retained mix (used when the tracklist is cleared)."""
    clear_mix()
    return jsonify({"ok": True})


# —— Scan history ——
def api_history():
    items = []
    for x in os.listdir(HISTORY_DIR):
        if not x.endswith(".json"):
            continue
        try:
            r = load_json(os.path.join(HISTORY_DIR, x), None)
        except OSError:
            continue
        if not isinstance(r, dict):
            continue
        tl = r.get("tracklist", [])
        items.append(
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "date": r.get("date"),
                "duration": r.get("duration"),
                "tracks": sum(1 for t in tl if not t.get("unidentified")),
                "unidentified": sum(1 for t in tl if t.get("unidentified")),
            }
        )
    items.sort(key=lambda i: i.get("date") or 0, reverse=True)
    return jsonify({"items": items})


def api_history_one(hid):
    f = _history_file(hid)
    if not f or not os.path.isfile(f):
        return jsonify({"error": "not found"}), 404
    if request.method == "DELETE":
        with _history_lock:
            try:
                os.remove(f)
            except FileNotFoundError:
                return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True})
    with _history_lock:
        try:
            rec = load_json(f, None)
        except OSError:
            return jsonify({"error": "history could not be read"}), 500
        if not isinstance(rec, dict):
            return jsonify({"error": "history was corrupt and was quarantined"}), 409
        if request.method == "PATCH":
            # Persist manual renames of unidentified sections, keyed by start.
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"error": "a JSON object is required"}), 400
            try:
                apply_manual_title(
                    rec.get("tracklist", []),
                    data.get("start"),
                    data.get("manual_title"),
                )
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            except LookupError as exc:
                return jsonify({"error": str(exc)}), 404
            atomic_write_json(f, rec)
    return jsonify(rec)


def api_library():
    files = sorted(manifest_files())
    stale = manifest_stale_paths()
    by_folder = {}
    for p in files:
        rel = os.path.relpath(p, MUSIC_ROOT)
        folder = os.path.dirname(rel) or "/"
        by_folder.setdefault(folder, []).append(os.path.basename(p))
    return jsonify(
        {
            "total": len(files),
            "stale": len(stale),
            "folders": [{"name": k, "tracks": sorted(v)} for k, v in sorted(by_folder.items())],
        }
    )


def api_files():
    """Audio files inside one music folder, with sizes."""
    rel = request.args.get("path", "")
    folder = safe_music_path(rel)
    if folder is None or not os.path.isdir(folder):
        return jsonify({"error": "invalid path"}), 400
    indexed = manifest_files()
    files = []
    for f in sorted(os.listdir(folder), key=str.lower):
        full = os.path.join(folder, f)
        if not f.startswith(".") and os.path.isfile(full) and f.lower().endswith(AUDIO_EXTS):
            files.append(
                {
                    "name": f,
                    "size": os.path.getsize(full),
                    "indexed": full in indexed,
                }
            )
    return jsonify({"path": rel, "files": files})


def api_download():
    """Download one audio file to the user's machine."""
    rel = request.args.get("path", "")
    path = safe_music_path(rel)
    if path is None or not os.path.isfile(path) or not path.lower().endswith(AUDIO_EXTS):
        return jsonify({"error": "not found"}), 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


def api_zip():
    """Download a whole folder's audio as a zip (stored, not recompressed)."""
    import tempfile
    import zipfile

    rel = request.args.get("path", "")
    folder = safe_music_path(rel)
    if folder is None or not os.path.isdir(folder):
        return jsonify({"error": "invalid path"}), 400
    files = [
        f
        for f in sorted(os.listdir(folder), key=str.lower)
        if not f.startswith(".")
        and f.lower().endswith(AUDIO_EXTS)
        and os.path.isfile(os.path.join(folder, f))
    ]
    if not files:
        return jsonify({"error": "no audio files in folder"}), 400
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=UPLOAD_DIR)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:
            for f in files:
                z.write(os.path.join(folder, f), arcname=f)
    except Exception:
        os.remove(tmp.name)
        raise
    name = (os.path.basename(folder.rstrip(os.sep)) or "music") + ".zip"
    resp = send_file(tmp.name, as_attachment=True, download_name=name)
    resp.call_on_close(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
    return resp


def api_upload_tracks():
    """Add the user's own audio files into a music folder."""
    rel = request.form.get("folder", "")
    folder = safe_music_path(rel)
    if folder is None:
        return jsonify({"error": "invalid folder"}), 400
    os.makedirs(folder, exist_ok=True)
    tracks = request.files.getlist("tracks")
    if len(tracks) > MAX_TRACK_UPLOADS:
        return jsonify({"error": f"at most {MAX_TRACK_UPLOADS} tracks may be uploaded"}), 400
    saved, rejected, conflicts = [], [], []
    for f in tracks:
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.path.basename(f.filename or ""))
        if not name or not name.lower().endswith(AUDIO_EXTS):
            rejected.append(name or "unnamed")
            continue
        target = os.path.join(folder, name)
        if not copy_exclusive(f.stream, target):
            conflicts.append(name)
            continue
        saved.append(name)
    logger.info(
        f"upload-tracks: {len(saved)} saved to {rel}, "
        f"{len(conflicts)} conflicts, {len(rejected)} rejected"
    )
    return jsonify({"saved": saved, "rejected": rejected, "conflicts": conflicts})


def api_stream():
    """Stream a library audio file with Range support (enables seeking)."""
    rel = request.args.get("path", "")
    path = safe_music_path(rel)
    if path is None or not os.path.isfile(path) or not path.lower().endswith(AUDIO_EXTS):
        return jsonify({"error": "not found"}), 404
    return send_file(path, conditional=True)


def api_waveform():
    """Cached mini-waveform peaks for one library track (thumbnail use)."""
    rel = request.args.get("path", "")
    try:
        buckets = int(request.args.get("buckets", 120))
    except (TypeError, ValueError):
        buckets = 120
    buckets = max(20, min(buckets, 400))
    path = safe_music_path(rel)
    if path is None or not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    return jsonify({"peaks": cached_peaks(path, buckets)})


# ============================================================
# Routes — SCRipper
# ============================================================
def api_scripper_resolve():
    """Expand the URL input: detect playlists so the GUI can confirm
    before downloading hundreds of tracks."""
    try:
        data = require_object(request.get_json(silent=True))
        urls = split_and_validate_urls(data.get("input"), MAX_RESOLVE_URLS)
    except RequestValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    items = []
    opts = {"quiet": True, "extract_flat": True, "no_warnings": True}
    if os.path.isfile(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    for url in urls:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            items.append({"url": url, "kind": "error", "title": str(e)[:200]})
            continue
        if not isinstance(info, dict):
            items.append(
                {"url": url, "kind": "error", "title": "platform returned no track information"}
            )
            continue
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e["url"] for e in info["entries"] if e.get("url")]
            if len(entries) > MAX_DOWNLOAD_URLS:
                items.append(
                    {
                        "url": url,
                        "kind": "error",
                        "title": f"playlist exceeds the {MAX_DOWNLOAD_URLS} track limit",
                    }
                )
                continue
            try:
                entries = validate_urls(entries, MAX_DOWNLOAD_URLS)
            except RequestValidationError as exc:
                items.append({"url": url, "kind": "error", "title": str(exc)})
                continue
            items.append(
                {
                    "url": url,
                    "kind": "playlist",
                    "title": info.get("title", "Unknown playlist"),
                    "count": len(entries),
                    "tracks": entries,
                }
            )
        else:
            items.append(
                {
                    "url": url,
                    "kind": "track",
                    "title": info.get("title", url),
                    "count": 1,
                    "tracks": [url],
                }
            )
    return jsonify({"items": items})


def api_scripper_download():
    try:
        urls, folder_name, index_after = validate_download_request(request.get_json(silent=True))
    except RequestValidationError as exc:
        return jsonify({"error": str(exc)}), 400
    folder = safe_music_path(folder_name)
    if folder is None:
        return jsonify({"error": "invalid folder"}), 400
    jid = submit_job(
        "download",
        f"{len(urls)} tracks -> {folder_name}",
        _download_job_queue,
        do_download,
        (urls, folder, index_after),
    )
    if jid is None:
        return jsonify({"error": "Download queue is full; try again later"}), 429
    return jsonify({"job": jid})


APP_STATE = ApplicationState(
    settings=SETTINGS,
    jobs=_job_service,
    metadata=_metadata_stores,
    workers=_worker_runtime,
)


def create_app(config=None):
    application = Flask(__name__, static_folder="static", static_url_path="")
    application.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024
    application.config["START_WORKERS"] = SETTINGS.start_workers
    if config:
        application.config.update(config)
    application.extensions["scripper"] = APP_STATE
    application.register_blueprint(create_blueprint(sys.modules[__name__]))
    if application.config["START_WORKERS"]:
        start_workers()
    try:
        removed = cleanup_runtime_artifacts(
            MUSIC_ROOT,
            UPLOAD_DIR,
            WAVEFORM_CACHE,
        )
        if any(removed.values()):
            logger.info("startup cleanup removed app artifacts: %s", removed)
    except OSError:
        logger.warning("startup artifact cleanup failed", exc_info=True)
    return application


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
