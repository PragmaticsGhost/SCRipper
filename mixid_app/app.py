#!/usr/bin/env python3
"""
SCRipper suite — web GUI combining:
  - SCRipper: SoundCloud/YouTube track downloader (320kbps MP3 + metadata)
  - MixID:    DJ mix track identification via Panako fingerprinting

Runs inside the Docker container built from mixid_app/Dockerfile.
Music folders are mounted read-write at /music, the Panako DB persists
at /root/.panako/dbs, uploads land in /uploads.
"""
import os
import re
import json
import time
import uuid
import queue
import audioop
import hashlib
import logging
import threading
import subprocess
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_from_directory, send_file

import yt_dlp
import requests as http_requests
from mutagen.mp3 import MP3
from mutagen.id3 import TIT2, TPE1, TALB, APIC

MUSIC_ROOT = "/music"
UPLOAD_DIR = "/uploads"
DB_DIR = os.path.expanduser("~/.panako/dbs")
MANIFEST_PATH = os.path.join(DB_DIR, "mixid_manifest.json")
STRATEGY = "panako"
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aiff", ".aif")
UNIDENTIFIED_GAP_S = 40   # a hole this long in the tracklist = missed track
DOWNLOAD_WORKERS = 3

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB uploads

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DB_DIR, exist_ok=True)

# —— Host-header allowlist (defeats DNS-rebinding / cross-site access) ——
# This app is localhost-only; reject requests whose Host header isn't a
# loopback address so a website you visit can't drive the API via rebinding.
ALLOWED_HOSTS = {
    "localhost:8080", "127.0.0.1:8080", "[::1]:8080",
    "localhost", "127.0.0.1",
}

@app.before_request
def _enforce_host():
    host = (request.host or "").lower()
    if host not in ALLOWED_HOSTS:
        return ("Forbidden: unexpected Host header. This app only serves "
                "localhost.", 403)

# —— Logging: rotating file (mounted to mixid_app/logs on the host) ——
LOG_DIR = "/logs"
os.makedirs(LOG_DIR, exist_ok=True)
logger = logging.getLogger("scripper")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = RotatingFileHandler(
    os.path.join(LOG_DIR, "scripper.log"),
    maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
logger.addHandler(_fh)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_sh)

# —— Manifest: which files have been indexed ——
_manifest_lock = threading.Lock()

def load_manifest():
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"files": {}}

def save_manifest(m):
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)

def manifest_add(path):
    with _manifest_lock:
        m = load_manifest()
        m["files"][path] = True
        save_manifest(m)

def manifest_files():
    with _manifest_lock:
        return set(load_manifest()["files"].keys())

# —— Job queue (Panako LMDB writes must be serialized) ——
_jobs = {}
_jobs_lock = threading.Lock()
_job_queue = queue.Queue()

def _sanitize_log(s):
    """Strip control chars (incl. newlines) so user-controlled strings
    can't forge extra log lines."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(s))

def new_job(jtype, label):
    jid = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid, "type": jtype, "label": label,
            "status": "queued", "progress": None,
            "detail": "", "result": None, "error": None,
            "log": [], "active": {},
        }
    logger.info(f"job {jid} queued: {jtype} — {_sanitize_log(label)}")
    return jid

def update_job(jid, **kw):
    with _jobs_lock:
        _jobs[jid].update(kw)

def job_log(jid, line):
    """Append a timestamped line to the job's console feed (and logfile)."""
    line = _sanitize_log(line)
    stamp = time.strftime("%H:%M:%S")
    with _jobs_lock:
        j = _jobs.get(jid)
        if j is not None:
            j["log"].append(f"[{stamp}] {line}")
            del j["log"][:-300]
    logger.info(f"job {jid}: {line}")

def job_active(jid, key, val):
    """Track live per-file progress; val=None clears the entry."""
    with _jobs_lock:
        j = _jobs.get(jid)
        if j is None:
            return
        if val is None:
            j["active"].pop(key, None)
        else:
            j["active"][key] = val

def get_job(jid):
    with _jobs_lock:
        j = _jobs.get(jid)
        if not j:
            return None
        out = dict(j)
        out["log"] = list(j["log"])
        out["active"] = dict(j["active"])
        return out

def worker_loop():
    while True:
        jid, fn, args = _job_queue.get()
        update_job(jid, status="running")
        logger.info(f"job {jid} started")
        try:
            fn(jid, *args)
            logger.info(f"job {jid} finished: {get_job(jid)['status']}")
        except Exception as e:
            logger.exception(f"job {jid} crashed")
            update_job(jid, status="error", error=str(e))

threading.Thread(target=worker_loop, daemon=True).start()

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

def compute_peaks(path, buckets, rate=8000):
    """Decode audio to low-rate mono PCM via ffmpeg and reduce it to
    `buckets` normalised (0..1) RMS-energy peaks. RMS (not max amplitude)
    is used so loud, heavily-limited masters still show dynamic contour
    instead of a solid block. Pure stdlib (audioop)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", path, "-ac", "1",
             "-filter:a", f"aresample={rate}", "-f", "s16le", "-"],
            capture_output=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return []
    pcm = r.stdout
    width = 2
    total = len(pcm) // width
    if total == 0:
        return []
    per = max(1, total // buckets)
    peaks = []
    for i in range(0, total, per):
        chunk = pcm[i * width:(i + per) * width]
        if not chunk:
            break
        peaks.append(audioop.rms(chunk, width))
    hi = max(peaks) or 1
    # sqrt curve lifts quieter sections so they stay visible
    return [round((p / hi) ** 0.7, 3) for p in peaks]

def cached_peaks(path, buckets):
    """Peaks for a library track, cached on disk keyed by path+mtime+buckets."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    key = hashlib.sha1(
        f"{path}|{mtime}|{buckets}|{WAVEFORM_ALGO}".encode()).hexdigest()
    cache_file = os.path.join(WAVEFORM_CACHE, key + ".json")
    if os.path.isfile(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    peaks = compute_peaks(path, buckets)
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(peaks, f)
    except OSError:
        pass
    return peaks

# ============================================================
# MixID: Panako helpers
# ============================================================
def run_panako(args, timeout=None):
    return subprocess.run(
        ["panako"] + args, capture_output=True, text=True, timeout=timeout,
    )

def audio_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
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
            "match_name": os.path.basename(parts[5]),
            "score": float(parts[9]),
            "time_factor": float(parts[10].replace("%", "").strip()),
        }
    except (ValueError, IndexError):
        return None

def collapse_matches(matches, min_segments=2, duration=None):
    """Collapse segment matches into tracklist entries, inserting
    'unidentified' placeholders where the mix has coverage gaps."""
    entries, current = [], None
    for m in sorted(matches, key=lambda m: m["query_start"]):
        if current and m["match_name"] == current["file"]:
            current["end"] = m["query_stop"]
            current["segments"] += 1
            current["time_factors"].append(m["time_factor"])
        else:
            if current:
                entries.append(current)
            current = {
                "file": m["match_name"],
                "start": m["query_start"], "end": m["query_stop"],
                "segments": 1, "time_factors": [m["time_factor"]],
            }
    if current:
        entries.append(current)

    accepted = []
    for e in entries:
        if e["segments"] < min_segments:
            continue
        avg_tf = sum(e["time_factors"]) / len(e["time_factors"])
        accepted.append({
            "title": os.path.splitext(e["file"])[0],
            "start": e["start"], "end": e["end"],
            "tempo_pct": round((avg_tf - 1.0) * 100, 1),
            "segments": e["segments"],
            "unidentified": False,
        })

    # Insert placeholders for gaps nothing was matched to
    final, prev_end = [], 0.0
    for e in accepted:
        if e["start"] - prev_end > UNIDENTIFIED_GAP_S:
            final.append({
                "title": None, "start": prev_end, "end": e["start"],
                "tempo_pct": 0, "segments": 0, "unidentified": True,
            })
        final.append(e)
        prev_end = max(prev_end, e["end"])
    if duration and duration - prev_end > UNIDENTIFIED_GAP_S:
        final.append({
            "title": None, "start": prev_end, "end": duration,
            "tempo_pct": 0, "segments": 0, "unidentified": True,
        })
    for e in final:
        e.pop("end", None)
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
        "id": hid, "name": name, "date": time.time(),
        "duration": duration, "tracklist": tracklist, "waveform": waveform,
    }
    with _history_lock:
        f = _history_file(hid)
        if f:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(rec, fh)
        # prune oldest beyond HISTORY_MAX
        entries = [os.path.join(HISTORY_DIR, x)
                   for x in os.listdir(HISTORY_DIR) if x.endswith(".json")]
        entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for stale in entries[HISTORY_MAX:]:
            try:
                os.remove(stale)
            except OSError:
                pass

def find_duplicate(fpath):
    """Check a track against the existing library using the same
    segment-by-segment matcher as identification (`monitor`), so
    near-identical files are caught even across formats/bitrates. Returns
    the title of a strongly-matching already-indexed track, or None."""
    r = run_panako(["monitor", f"STRATEGY={STRATEGY}", fpath], timeout=600)
    tally = {}
    for line in (r.stdout or "").splitlines():
        parsed = parse_monitor_line(line)
        if parsed and parsed != "nomatch":
            m = tally.setdefault(parsed["match_name"],
                                 {"segments": 0, "score": 0.0})
            m["segments"] += 1
            m["score"] += parsed["score"]
    if not tally:
        return None
    best_name, best = max(tally.items(),
                          key=lambda kv: (kv[1]["segments"], kv[1]["score"]))
    # Require a solid multi-segment match to call it a duplicate.
    if best["segments"] >= 3:
        return os.path.splitext(best_name)[0]
    return None

def do_index(jid, folder):
    files = sorted(
        f for f in os.listdir(folder) if f.lower().endswith(AUDIO_EXTS)
    )
    if not files:
        update_job(jid, status="error", error="No audio files in folder")
        return
    already = manifest_files()
    done, failed, duplicates = 0, [], []
    for i, fname in enumerate(files):
        fpath = os.path.join(folder, fname)
        update_job(jid, progress=i / len(files), detail=fname)
        if fpath in already:
            done += 1
            continue
        # Duplicate check: query the track against the existing DB first.
        dup_of = find_duplicate(fpath)
        if dup_of:
            duplicates.append({"file": fname, "duplicate_of": dup_of})
            logger.info(f"index: skipping duplicate {fname} (matches {dup_of})")
            continue
        r = run_panako(["store", f"STRATEGY={STRATEGY}", fpath], timeout=600)
        if r.returncode == 0:
            manifest_add(fpath)
            done += 1
        else:
            failed.append(fname)
            logger.warning(
                f"panako store failed for {fpath}: "
                f"{(r.stderr or r.stdout or '').strip()[-300:]}")
    update_job(
        jid, status="done", progress=1.0, detail="",
        result={"indexed": done, "failed": failed, "duplicates": duplicates,
                "total": len(files)},
    )

def do_identify(jid, mix_path, min_segments, mix_name=None):
    register_mix(jid, mix_path)
    duration = audio_duration(mix_path)
    logger.info(f"identify start: {os.path.basename(mix_path)} "
                f"({duration and round(duration) or '?'}s, min_segments={min_segments})")
    # Render the waveform first so the GUI can draw it while scanning.
    update_job(jid, detail="Rendering waveform...")
    waveform = compute_peaks(mix_path, buckets=700)
    update_job(jid, waveform=waveform, wf_duration=duration)
    update_job(jid, detail="Fingerprinting mix segments...")
    proc = subprocess.Popen(
        ["panako", "monitor", f"STRATEGY={STRATEGY}", mix_path],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
    )
    matches = []
    for line in proc.stdout:
        parsed = parse_monitor_line(line)
        if parsed and parsed != "nomatch":
            matches.append(parsed)
        seg = re.search(r"-([\d.]+)_[\d.]+ ", line)
        if seg and duration:
            update_job(jid, progress=min(float(seg.group(1)) / duration, 1.0))
    proc.wait()
    # NB: the mix file is intentionally kept (register_mix) for slice
    # playback; it's replaced when the next mix is scanned or cleared.
    if proc.returncode != 0 and not matches:
        update_job(jid, status="error", error="Panako monitor failed")
        return
    tracklist = collapse_matches(matches, min_segments, duration)
    logger.info(f"identify done: {len(matches)} segment matches -> "
                f"{sum(1 for t in tracklist if not t['unidentified'])} tracks, "
                f"{sum(1 for t in tracklist if t['unidentified'])} unidentified sections")
    save_history(jid, mix_name or os.path.basename(mix_path),
                 duration, tracklist, waveform)
    update_job(jid, status="done", progress=1.0, detail="",
               result={"tracklist": tracklist, "duration": duration,
                       "waveform": waveform, "job": jid})

# ============================================================
# SCRipper: downloader
# ============================================================
COOKIE_FILE = "/cookies/cookies.txt"

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
        "format":           "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl":          f"{folder}/%(title)s.%(ext)s",
        "writethumbnail":   False,
        "download_archive": os.path.join(folder, ".download_archive.txt"),
        "retries":          3,
        "sleep_requests":   0.5,
        "fixup":            "never",
        "postprocessors":   [],
        "quiet":            True,
        "no_warnings":      True,
        "noprogress":       True,
    }
    if os.path.isfile(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts

def convert_to_mp3(inp, out):
    same = (inp == out)
    target = os.path.splitext(inp)[0] + ".tmp.mp3" if same else out
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", inp, "-b:a", "320k", "-f", "mp3",
         "-loglevel", "error", target],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        if same and os.path.exists(target):
            os.remove(target)
        raise RuntimeError(f"ffmpeg: {r.stderr.strip()[-300:]}")
    if same:
        os.replace(target, out)
    else:
        os.remove(inp)

def embed_metadata(mp3_file, title, artist, art_url):
    audio = MP3(mp3_file)
    if audio.tags is None:
        audio.add_tags()
    audio.tags["TIT2"] = TIT2(encoding=3, text=title)
    audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
    audio.tags["TALB"] = TALB(encoding=3, text="SCRipper")
    if art_url:
        try:
            r = http_requests.get(art_url, timeout=10)
            r.raise_for_status()
            mime = r.headers.get("Content-Type", "image/jpeg")
            audio.tags["APIC"] = APIC(
                encoding=3, mime=mime, type=3, desc="Cover", data=r.content)
        except Exception:
            pass  # art is best-effort
    audio.save(v2_version=3)

def make_progress_hook(jid):
    def hook(d):
        fname = os.path.basename(d.get("filename") or "")
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            got = d.get("downloaded_bytes", 0)
            if total:
                job_active(jid, fname,
                           f"{got / total * 100:.0f}% of {total / 1048576:.1f} MB")
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
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            return "skipped", url
        title = info.get("title", "Unknown")
        inp = ydl.prepare_filename(info)
    out = os.path.splitext(inp)[0] + ".mp3"
    if not os.path.isfile(inp):
        if jid:
            job_log(jid, f"↷ skipped (already in archive): {title}")
        return "skipped", title
    if jid:
        job_log(jid, f"♪ converting to 320kbps mp3: {title}")
    convert_to_mp3(inp, out)
    embed_metadata(out, title, info.get("uploader", "Unknown"),
                   (info.get("thumbnails") or [{}])[-1].get("url"))
    if jid:
        job_log(jid, f"✔ done: {title}")
    return "downloaded", title

def friendly_dl_error(url, exc):
    """Turn a yt-dlp exception into a short human-readable failure line."""
    msg = str(exc)
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if "api-v2.soundcloud.com/tracks/" in url:
        name = f"SoundCloud track #{slug}"
    else:
        name = slug
    low = msg.lower()
    if "404" in msg:
        reason = "no longer available (deleted or removed from the platform)"
    elif "401" in msg or "403" in msg or "authorization" in low:
        reason = ("blocked by the platform (403) — either temporary rate "
                  "limiting (wait and retry) or authentication is needed "
                  "(capture cookies in the 🍪 section)")
    elif "country" in low or "geo" in low or "region" in low:
        reason = "not available in your region"
    elif "429" in msg or "too many requests" in low:
        reason = "rate limited by the platform — wait a while and retry"
    elif "private" in low:
        reason = "private track — capture cookies for an account that can access it"
    else:
        reason = msg.replace("ERROR: ", "")[:160]
    return f"{name} — {reason}"

def do_download(jid, urls, folder, index_after):
    os.makedirs(folder, exist_ok=True)
    ydl_opts = build_ydl_opts(folder)
    downloaded, skipped, failed = [], [], []
    done_count = [0]
    lock = threading.Lock()

    def work(u):
        try:
            opts = {**ydl_opts, "progress_hooks": [make_progress_hook(jid)]}
            status, name = download_one(u, folder, opts, jid=jid)
            with lock:
                (downloaded if status == "downloaded" else skipped).append(name)
        except Exception as e:
            line = friendly_dl_error(u, e)
            job_log(jid, f"✘ failed: {line}")
            logger.warning(f"download failed: {u}: {e}")
            with lock:
                failed.append(line)
        finally:
            with lock:
                done_count[0] += 1
                update_job(jid, progress=done_count[0] / len(urls),
                           detail=f"{done_count[0]}/{len(urls)} tracks")

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        list(ex.map(work, urls))

    result = {
        "downloaded": sorted(downloaded), "skipped": sorted(skipped),
        "failed": failed, "total": len(urls), "index_job": None,
    }
    if index_after and (downloaded or skipped):
        ijid = new_job("index", os.path.relpath(folder, MUSIC_ROOT))
        _job_queue.put((ijid, do_index, (folder,)))
        result["index_job"] = ijid
    update_job(jid, status="done", progress=1.0, detail="", result=result)

# ============================================================
# Routes — shared
# ============================================================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/job/<jid>")
def api_job(jid):
    j = get_job(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(j)

BROWSER_PROFILE_DIR = "/browser-profile"
CAPTURE_DOMAINS = ("soundcloud.com", "youtube.com", "google.com")

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
    import sqlite3
    import shutil
    import tempfile

    db = find_cookie_db()
    if not db:
        raise RuntimeError(
            "Login browser profile not found. Is the login-browser "
            "container running? (docker compose up -d)")

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
            "SELECT host, path, isSecure, expiry, name, value "
            "FROM moz_cookies").fetchall()
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    counts = {d: 0 for d in CAPTURE_DOMAINS}
    lines = ["# Netscape HTTP Cookie File", "# Captured from SCRipper login browser"]
    for host, path, is_secure, expiry, name, value in rows:
        bare = host.lstrip(".")
        matched = next(
            (d for d in CAPTURE_DOMAINS
             if bare == d or bare.endswith("." + d)), None)
        if not matched:
            continue
        counts[matched] += 1
        include_sub = "TRUE" if host.startswith(".") else "FALSE"
        secure = "TRUE" if is_secure else "FALSE"
        lines.append(
            f"{host}\t{include_sub}\t{path}\t{secure}\t{expiry or 0}\t{name}\t{value}")

    if not any(counts.values()):
        raise RuntimeError(
            "No SoundCloud/YouTube cookies found in the login browser. "
            "Open it and log in first.")

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

@app.route("/api/cookies/capture", methods=["POST"])
def api_cookies_capture():
    """One-click capture from the in-container login browser."""
    try:
        counts = capture_browser_cookies()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"present": True, "counts": counts})

@app.route("/api/cookies", methods=["GET", "POST", "DELETE"])
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
        return jsonify({"error":
            "This looks like a JSON export. Export in Netscape/cookies.txt "
            "format instead (the 'Get cookies.txt LOCALLY' extension does "
            "this by default)."}), 400
    if not looks_like_netscape_cookies(text):
        return jsonify({"error":
            "Not a valid cookies.txt (Netscape format) file."}), 400
    write_cookie_file(text)
    return jsonify({"present": True, "age_days": 0.0})

@app.route("/api/folders")
def api_folders():
    """Top-level music folders (for destination pickers)."""
    folders = []
    for name in sorted(os.listdir(MUSIC_ROOT), key=str.lower):
        full = os.path.join(MUSIC_ROOT, name)
        if name.startswith(".") or not os.path.isdir(full):
            continue
        try:
            n = sum(1 for f in os.listdir(full)
                    if f.lower().endswith(AUDIO_EXTS))
        except PermissionError:
            n = 0
        folders.append({"name": name, "audio_count": n})
    return jsonify({"folders": folders})

# ============================================================
# Routes — MixID
# ============================================================
@app.route("/api/resolve-folder", methods=["POST"])
def api_resolve_folder():
    """Map a folder name (from the native OS picker) to a path under
    /music. Searches 3 levels deep; case-insensitive."""
    name = ((request.json or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "no folder name"}), 400
    hits = []
    for root, dirs, _files in os.walk(MUSIC_ROOT):
        depth = os.path.relpath(root, MUSIC_ROOT).count(os.sep)
        if depth >= 3:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith(".")
                   and d not in ("__pycache__", "node_modules")]
        for d in dirs:
            if d.lower() == name.lower():
                hits.append(os.path.relpath(os.path.join(root, d), MUSIC_ROOT))
    return jsonify({"matches": hits})

@app.route("/api/index", methods=["POST"])
def api_index():
    rel = (request.json or {}).get("path", "")
    folder = safe_music_path(rel)
    if folder is None or not os.path.isdir(folder):
        return jsonify({"error": "invalid path"}), 400
    jid = new_job("index", rel or "/")
    _job_queue.put((jid, do_index, (folder,)))
    return jsonify({"job": jid})

@app.route("/api/identify", methods=["POST"])
def api_identify():
    f = request.files.get("mix")
    if not f or not f.filename:
        return jsonify({"error": "no file"}), 400
    try:
        min_segments = int(request.form.get("min_segments", 2))
    except (TypeError, ValueError):
        min_segments = 2
    min_segments = max(1, min(min_segments, 20))
    safe_name = re.sub(r"[^\w .()\[\]&-]", "_", os.path.basename(f.filename))
    dest = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{safe_name}")
    f.save(dest)
    jid = new_job("identify", f.filename)
    _job_queue.put((jid, do_identify, (dest, min_segments, f.filename)))
    return jsonify({"job": jid})

@app.route("/api/mix-stream")
def api_mix_stream():
    """Stream the retained (most-recent) mix with Range support, so
    tracklist slices can be played from their timestamps."""
    jid = request.args.get("job", "")
    path = get_mix_path(jid)
    if not path or not os.path.isfile(path):
        return jsonify({"error": "mix not available"}), 404
    return send_file(path, conditional=True)

@app.route("/api/mix", methods=["DELETE"])
def api_mix_delete():
    """Drop the retained mix (used when the tracklist is cleared)."""
    clear_mix()
    return jsonify({"ok": True})

# —— Scan history ——
@app.route("/api/history")
def api_history():
    items = []
    for x in os.listdir(HISTORY_DIR):
        if not x.endswith(".json"):
            continue
        try:
            with open(os.path.join(HISTORY_DIR, x), encoding="utf-8") as fh:
                r = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        tl = r.get("tracklist", [])
        items.append({
            "id": r.get("id"), "name": r.get("name"), "date": r.get("date"),
            "duration": r.get("duration"),
            "tracks": sum(1 for t in tl if not t.get("unidentified")),
            "unidentified": sum(1 for t in tl if t.get("unidentified")),
        })
    items.sort(key=lambda i: i.get("date") or 0, reverse=True)
    return jsonify({"items": items})

@app.route("/api/history/<hid>", methods=["GET", "PATCH", "DELETE"])
def api_history_one(hid):
    f = _history_file(hid)
    if not f or not os.path.isfile(f):
        return jsonify({"error": "not found"}), 404
    if request.method == "DELETE":
        try:
            os.remove(f)
        except OSError:
            pass
        return jsonify({"ok": True})
    with open(f, encoding="utf-8") as fh:
        rec = json.load(fh)
    if request.method == "PATCH":
        # Persist manual renames of unidentified sections, keyed by start.
        data = request.json or {}
        start = data.get("start")
        title = (data.get("manual_title") or "").strip() or None
        for t in rec.get("tracklist", []):
            if t.get("unidentified") and abs((t.get("start") or 0) - (start or -1)) < 0.01:
                t["manual_title"] = title
                break
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
    return jsonify(rec)

@app.route("/api/library")
def api_library():
    files = sorted(manifest_files())
    by_folder = {}
    for p in files:
        rel = os.path.relpath(p, MUSIC_ROOT)
        folder = os.path.dirname(rel) or "/"
        by_folder.setdefault(folder, []).append(os.path.basename(p))
    return jsonify({
        "total": len(files),
        "folders": [
            {"name": k, "tracks": sorted(v)}
            for k, v in sorted(by_folder.items())
        ],
    })

@app.route("/api/files")
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
        if (not f.startswith(".") and os.path.isfile(full)
                and f.lower().endswith(AUDIO_EXTS)):
            files.append({
                "name": f,
                "size": os.path.getsize(full),
                "indexed": full in indexed,
            })
    return jsonify({"path": rel, "files": files})

@app.route("/api/download")
def api_download():
    """Download one audio file to the user's machine."""
    rel = request.args.get("path", "")
    path = safe_music_path(rel)
    if (path is None or not os.path.isfile(path)
            or not path.lower().endswith(AUDIO_EXTS)):
        return jsonify({"error": "not found"}), 404
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path))

@app.route("/api/zip")
def api_zip():
    """Download a whole folder's audio as a zip (stored, not recompressed)."""
    import zipfile
    import tempfile
    rel = request.args.get("path", "")
    folder = safe_music_path(rel)
    if folder is None or not os.path.isdir(folder):
        return jsonify({"error": "invalid path"}), 400
    files = [f for f in sorted(os.listdir(folder), key=str.lower)
             if not f.startswith(".") and f.lower().endswith(AUDIO_EXTS)
             and os.path.isfile(os.path.join(folder, f))]
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

@app.route("/api/upload-tracks", methods=["POST"])
def api_upload_tracks():
    """Add the user's own audio files into a music folder."""
    rel = request.form.get("folder", "")
    folder = safe_music_path(rel)
    if folder is None:
        return jsonify({"error": "invalid folder"}), 400
    os.makedirs(folder, exist_ok=True)
    saved, rejected = [], []
    for f in request.files.getlist("tracks"):
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_",
                      os.path.basename(f.filename or ""))
        if not name or not name.lower().endswith(AUDIO_EXTS):
            rejected.append(name or "unnamed")
            continue
        f.save(os.path.join(folder, name))
        saved.append(name)
    logger.info(f"upload-tracks: {len(saved)} saved to {rel}, "
                f"{len(rejected)} rejected")
    return jsonify({"saved": saved, "rejected": rejected})

@app.route("/api/stream")
def api_stream():
    """Stream a library audio file with Range support (enables seeking)."""
    rel = request.args.get("path", "")
    path = safe_music_path(rel)
    if (path is None or not os.path.isfile(path)
            or not path.lower().endswith(AUDIO_EXTS)):
        return jsonify({"error": "not found"}), 404
    return send_file(path, conditional=True)

@app.route("/api/waveform")
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
@app.route("/api/scripper/resolve", methods=["POST"])
def api_scripper_resolve():
    """Expand the URL input: detect playlists so the GUI can confirm
    before downloading hundreds of tracks."""
    raw = ((request.json or {}).get("input") or "")
    urls = [u.strip() for u in re.split(r"[,\n]+", raw) if u.strip()]
    if not urls:
        return jsonify({"error": "no URLs"}), 400
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
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = [e["url"] for e in info["entries"] if e.get("url")]
            items.append({
                "url": url, "kind": "playlist",
                "title": info.get("title", "Unknown playlist"),
                "count": len(entries), "tracks": entries,
            })
        else:
            items.append({
                "url": url, "kind": "track",
                "title": info.get("title", url), "count": 1, "tracks": [url],
            })
    return jsonify({"items": items})

@app.route("/api/scripper/download", methods=["POST"])
def api_scripper_download():
    data = request.json or {}
    urls = data.get("urls") or []
    folder_name = re.sub(r"[<>:\"/\\|?*]", "", (data.get("folder") or "").strip())
    index_after = bool(data.get("index_after"))
    if not urls:
        return jsonify({"error": "no tracks selected"}), 400
    if not folder_name:
        return jsonify({"error": "no destination folder"}), 400
    folder = safe_music_path(folder_name)
    if folder is None:
        return jsonify({"error": "invalid folder"}), 400
    jid = new_job("download", f"{len(urls)} tracks -> {folder_name}")
    _job_queue.put((jid, do_download, (urls, folder, index_after)))
    return jsonify({"job": jid})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
