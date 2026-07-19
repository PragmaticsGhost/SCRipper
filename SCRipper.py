#!/usr/bin/env python3
import sys
import subprocess
import os
import time
import logging
import threading
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# —— Bootstrap Python dependencies ——
def ensure_package(pkg_name, import_name=None):
    """Import import_name (or pkg_name); if missing, install pkg_name via pip."""
    mod = import_name or pkg_name
    try:
        __import__(mod)
    except ImportError:
        print(f"-> {mod!r} not found, installing {pkg_name}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg_name])

for pkg, imp in [
    ("imageio-ffmpeg", "imageio_ffmpeg"),
    ("yt-dlp",         "yt_dlp"),
    ("ffmpeg-python",  "ffmpeg"),
    ("mutagen",        "mutagen"),
    ("tqdm",           "tqdm"),
    ("requests",       "requests"),
    ("browser_cookie3","browser_cookie3"),
]:
    ensure_package(pkg, imp)

# —— Logging setup ——
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Stdout handler: INFO and above
_stdout_handler = logging.StreamHandler(
    open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
)
_stdout_handler.setLevel(logging.INFO)
_stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)
_stdout_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

# Stderr handler: WARNING and above
_stderr_handler = logging.StreamHandler(
    open(sys.stderr.fileno(), mode="w", encoding="utf-8", closefd=False)
)
_stderr_handler.setLevel(logging.WARNING)
_stderr_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s\n  %(pathname)s:%(lineno)d"))

logger.addHandler(_stdout_handler)
logger.addHandler(_stderr_handler)

# -- Imports
import browser_cookie3
from http.cookiejar import MozillaCookieJar
import yt_dlp
import ffmpeg
import requests
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from imageio_ffmpeg import get_ffmpeg_exe

FFMPEG_EXE = get_ffmpeg_exe()
MAX_WORKERS = 5

# —— Thread-safe counters ——
_stats_lock = threading.Lock()
_stats = {"downloaded": 0, "skipped": 0, "failed": 0}

failed_downloads = []
_failed_lock = threading.Lock()

# —— Dump just soundcloud.com cookies ——
def dump_soundcloud_cookies(path):
    logger.debug(f"Dumping SoundCloud cookies to {path}")
    cj = MozillaCookieJar(path)
    for ck in browser_cookie3.chrome(domain_name="soundcloud.com"):
        cj.set_cookie(ck)
    cj.save(ignore_discard=True, ignore_expires=True)
    return path

def build_ydl_opts(output_dir, cookie_file, archive_file):
    return {
        "format":           "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl":          f"{output_dir}/%(title)s.%(ext)s",
        "writethumbnail":   False,
        "ffmpeg_location":  FFMPEG_EXE,
        "cookiefile":       cookie_file,
        "download_archive": archive_file,
        "retries":          3,
        "sleep_requests":   0.5,
        "fixup":            "never",
        "postprocessors":   [],
        "quiet":            True,
        "no_warnings":      True,
        "logger":           logger,
    }

# One YoutubeDL instance per thread to avoid re-initialization overhead
_thread_local = threading.local()

def _get_ydl(ydl_opts):
    if not hasattr(_thread_local, "ydl"):
        _thread_local.ydl = yt_dlp.YoutubeDL(ydl_opts)
    return _thread_local.ydl

def download_track(url, ydl_opts):
    """Download a track. Returns (inp, out, title, artist, art_url) or None if skipped by archive."""
    backoff, max_backoff = 30, 600
    while True:
        try:
            ydl = _get_ydl(ydl_opts)
            info = ydl.extract_info(url, download=True)
            break
        except yt_dlp.utils.ExistingVideoReached:
            return None
        except yt_dlp.utils.DownloadError as e:
            if "has already been recorded in the archive" in str(e):
                return None
            logger.error(f"Error during extract_info for {url}: {e}", exc_info=True)
            msg = str(e)
            if any(term in msg for term in ("HTTP Error 429", "Too Many Requests", "rate limit")):
                logger.warning(f"Rate limited on {url}, sleeping {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff*2, max_backoff)
                continue
            raise
        except Exception as e:
            logger.error(f"Error during extract_info for {url}: {e}", exc_info=True)
            msg = str(e)
            if any(term in msg for term in ("HTTP Error 429", "Too Many Requests", "rate limit")):
                logger.warning(f"Rate limited on {url}, sleeping {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff*2, max_backoff)
                continue
            raise
    if info is None:
        return None
    title = info.get("title", "Unknown")
    artist = info.get("uploader", "Unknown")
    art = info.get("thumbnails", [{}])[-1].get("url")
    inp = ydl.prepare_filename(info)
    out = os.path.splitext(inp)[0] + ".mp3"
    # Archive skip: yt-dlp returns info but doesn't download the file
    if not os.path.isfile(inp) and os.path.isfile(out):
        return None
    # Archive skip: neither source nor output exists (no download happened)
    if not os.path.isfile(inp) and not os.path.isfile(out):
        return None
    return inp, out, title, artist, art

def convert_to_mp3(inp, out):
    same_file = (inp == out)
    target = out
    if same_file:
        target = os.path.splitext(inp)[0] + ".tmp.mp3"
        logger.info(f"Re-encoding {inp} to 320kbps")
    else:
        logger.info(f"Converting {inp} to mp3 -> {out}")
    try:
        (
            ffmpeg
            .input(inp)
            .output(target, audio_bitrate="320k", format="mp3", threads=0, loglevel="quiet")
            .run(overwrite_output=True, cmd=FFMPEG_EXE)
        )
        if same_file:
            os.replace(target, out)
        else:
            os.remove(inp)
    except Exception as e:
        if same_file and os.path.exists(target):
            os.remove(target)
        logger.error(f"Error converting {inp}: {e}", exc_info=True)
        raise

def fetch_art(art_url, session):
    """Fetch cover art bytes and mime type. Returns (data, mime) or (None, None)."""
    if not art_url:
        return None, None
    try:
        r = session.get(art_url, timeout=10)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        logger.warning(f"Failed to fetch cover art: {e}")
        return None, None

def embed_metadata(mp3_file, title, artist, art_data=None, art_mime=None):
    logger.info(f"Embedding metadata for {title}")
    try:
        audio = MP3(mp3_file)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
        audio.tags["TALB"] = TALB(encoding=3, text="SoundCloud")
        if art_data:
            audio.tags["APIC"] = APIC(encoding=3, mime=art_mime, type=3, desc="Cover", data=art_data)
        audio.save(v2_version=3)
    except Exception as e:
        logger.error(f"Error embedding metadata/art for {title}: {e}", exc_info=True)
        with _failed_lock:
            failed_downloads.append(f"embed_meta:{title}")

KEEP_FILES = {".download_archive.txt"}

def clean_non_mp3(output_dir):
    logger.info("Cleaning up non-mp3 files...")
    for f in os.listdir(output_dir):
        if not f.lower().endswith(".mp3") and f not in KEEP_FILES:
            os.remove(os.path.join(output_dir, f))

def process_track(url, ydl_opts, output_dir, session, pbar):
    try:
        result = download_track(url, ydl_opts)

        # Skipped by download archive
        if result is None:
            logger.info(f"Skipping (already in archive): {url}")
            with _stats_lock:
                _stats["skipped"] += 1
            return

        inp, out, title, artist, art_url = result
        pbar.set_postfix_str(title[:40], refresh=True)

        # Convert and fetch art in parallel
        with ThreadPoolExecutor(max_workers=2) as mini_pool:
            convert_future = mini_pool.submit(convert_to_mp3, inp, out)
            art_future = mini_pool.submit(fetch_art, art_url, session)
            convert_future.result()
            art_data, art_mime = art_future.result()

        embed_metadata(out, title, artist, art_data, art_mime)
        with _stats_lock:
            _stats["downloaded"] += 1

    except Exception as e:
        logger.error(f"Unexpected error processing {url}: {e}", exc_info=True)
        with _failed_lock:
            failed_downloads.append(url)
        with _stats_lock:
            _stats["failed"] += 1

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IGNORE_DIRS = {".venv", ".git", ".idea", ".claude", "__pycache__", "logs"}

def choose_output_dir():
    """Prompt user to pick an existing folder or create a new one."""
    existing = sorted([
        d for d in os.listdir(BASE_DIR)
        if os.path.isdir(os.path.join(BASE_DIR, d)) and d not in IGNORE_DIRS
    ])

    print("\n--- Output Folder ---")
    print("  0) Create a new folder")
    for i, name in enumerate(existing, start=1):
        print(f"  {i}) {name}")

    while True:
        choice = input(f"\nSelect folder [0-{len(existing)}]: ").strip()
        if choice == "0":
            name = input("Enter new folder name: ").strip()
            if not name:
                print("Folder name cannot be empty.")
                continue
            path = os.path.join(BASE_DIR, name)
            os.makedirs(path, exist_ok=True)
            return path
        if choice.isdigit() and 1 <= int(choice) <= len(existing):
            return os.path.join(BASE_DIR, existing[int(choice) - 1])
        print("Invalid selection, try again.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download tracks from SoundCloud/YouTube as 320kbps MP3s")
    parser.add_argument("urls", nargs="?", default=None,
                        help="URL(s) comma-separated, or omit for interactive prompt")
    parser.add_argument("-o", "--output", default=None,
                        help="Output directory (skip folder prompt)")
    parser.add_argument("-w", "--workers", type=int, default=MAX_WORKERS,
                        help=f"Number of parallel downloads (default: {MAX_WORKERS})")
    args = parser.parse_args()

    if args.output:
        output_dir = args.output
    else:
        output_dir = choose_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    cookie_file = os.path.join(output_dir, "soundcloud_cookies.txt")
    archive_file = os.path.join(output_dir, ".download_archive.txt")
    dump_soundcloud_cookies(cookie_file)

    ydl_opts = build_ydl_opts(output_dir, cookie_file, archive_file)

    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=args.workers, pool_maxsize=args.workers)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    if args.urls:
        url_input = args.urls
    else:
        url_input = input("Enter URL(s) (single or playlist, separate multiple with commas): ")

    urls = []
    for url in [u.strip() for u in url_input.split(",") if u.strip()]:
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("_type") == "playlist" and info.get("entries"):
            entries = info["entries"]
            playlist_title = info.get("title", "Unknown playlist")
            confirm = input(
                f"'{playlist_title}' is a playlist with {len(entries)} tracks. "
                f"Download all? (y/n): "
            ).strip().lower()
            if confirm == "y":
                urls.extend([e["url"] for e in entries])
            else:
                print(f"Skipping playlist: {playlist_title}")
        else:
            urls.append(url)

    if not urls:
        print("No tracks to download.")
        sys.exit(0)

    print(f"Found {len(urls)} track{'s' if len(urls) != 1 else ''}. Processing...")
    with tqdm(total=len(urls), desc="Tracks", unit="track") as pbar:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(process_track, u, ydl_opts, output_dir, session, pbar): u
                for u in urls
            }
            for future in as_completed(futures):
                pbar.update(1)

    clean_non_mp3(output_dir)

    # —— Summary ——
    print(f"\n{'='*40}")
    print(f"  Downloaded: {_stats['downloaded']}")
    print(f"  Skipped:    {_stats['skipped']}")
    print(f"  Failed:     {_stats['failed']}")
    print(f"  Total:      {len(urls)}")
    print(f"{'='*40}")
    if failed_downloads:
        print("\nFailed items:")
        for it in failed_downloads:
            print(f"  - {it}")
