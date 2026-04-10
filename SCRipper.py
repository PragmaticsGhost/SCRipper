#!/usr/bin/env python3
import sys
import subprocess
import os
import time
import logging
import threading
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

# -- Import BS
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
MAX_WORKERS = 3
session = requests.Session()
adapter = HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS)
session.mount("https://", adapter)
session.mount("http://", adapter)
OUTPUT_DIR = "Jazz House"
COOKIE_FILE = os.path.join(OUTPUT_DIR, "soundcloud_cookies.txt")
os.makedirs(OUTPUT_DIR, exist_ok=True)
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

dump_soundcloud_cookies(COOKIE_FILE)

# —— yt-dlp options ——
YDL_OPTS = {
    "format":          "bestaudio[ext=m4a]/bestaudio/best",
    "outtmpl":         f"{OUTPUT_DIR}/%(title)s.%(ext)s",
    "writethumbnail":  True,
    "ffmpeg_location": FFMPEG_EXE,
    "cookiefile":      COOKIE_FILE,
    "retries":         3,
    "sleep_requests":  1,
    "fixup":           "never",
    "postprocessors":  [],
    "verbose":         True,
    "logger":          logger,
}

# One YoutubeDL instance per thread to avoid re-initialization overhead
_thread_local = threading.local()

def _get_ydl():
    if not hasattr(_thread_local, "ydl"):
        _thread_local.ydl = yt_dlp.YoutubeDL(YDL_OPTS)
    return _thread_local.ydl

def download_track(url):
    logger.info(f"Starting download for: {url}")
    backoff, max_backoff = 30, 600
    while True:
        try:
            ydl = _get_ydl()
            info = ydl.extract_info(url, download=True)
            logger.debug(f"Extracted info: title={info.get('title')} ext={info.get('ext')}")
            break
        except Exception as e:
            logger.error(f"Error during extract_info for {url}: {e}", exc_info=True)
            msg = str(e)
            if any(term in msg for term in ("HTTP Error 429", "Too Many Requests", "rate limit")):
                logger.warning(f"Rate limited on {url}, sleeping {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff*2, max_backoff)
                continue
            raise
    title = info.get("title", "Unknown")
    artist = info.get("uploader", "Unknown")
    art = info.get("thumbnails", [{}])[-1].get("url")
    inp = ydl.prepare_filename(info)
    out = os.path.splitext(inp)[0] + ".mp3"
    return inp, out, title, artist, art

def convert_to_mp3(inp, out):
    if inp == out:
        tmp = os.path.splitext(inp)[0] + ".tmp.mp3"
        logger.info(f"Re-encoding {inp} to 320kbps")
        try:
            (
                ffmpeg
                .input(inp)
                .output(tmp, audio_bitrate="320k", format="mp3", threads=5, loglevel="quiet")
                .run(overwrite_output=True, cmd=FFMPEG_EXE)
            )
            os.replace(tmp, out)
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            logger.error(f"Error re-encoding {inp}: {e}", exc_info=True)
            raise
        return
    logger.info(f"Converting {inp} to mp3 -> {out}")
    try:
        (
            ffmpeg
            .input(inp)
            .output(out, audio_bitrate="320k", format="mp3", threads=5, loglevel="quiet")
            .run(overwrite_output=True, cmd=FFMPEG_EXE)
        )
        os.remove(inp)
    except Exception as e:
        logger.error(f"Error converting {inp}: {e}", exc_info=True)
        raise

def embed_metadata(mp3_file, title, artist, art_url):
    logger.info(f"Embedding metadata for {title}")
    try:
        audio = MP3(mp3_file)
        if audio.tags is None:
            audio.add_tags()
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
        audio.tags["TALB"] = TALB(encoding=3, text="SoundCloud")
        if art_url:
            r = session.get(art_url, timeout=10)
            r.raise_for_status()
            mime = r.headers.get("Content-Type", "image/jpeg")
            audio.tags["APIC"] = APIC(encoding=3, mime=mime, type=3, desc="Cover", data=r.content)
        audio.save(v2_version=3)
    except Exception as e:
        logger.error(f"Error embedding metadata/art for {title}: {e}", exc_info=True)
        with _failed_lock:
            failed_downloads.append(f"embed_meta:{title}")

def clean_non_mp3():
    logger.info("Cleaning up non-mp3 files...")
    for f in os.listdir(OUTPUT_DIR):
        if not f.lower().endswith(".mp3"):
            os.remove(os.path.join(OUTPUT_DIR, f))

def process_track(url):
    logger.info(f"Processing track: {url}")
    try:
        inp, out, title, artist, art = download_track(url)
        convert_to_mp3(inp, out)
        embed_metadata(out, title, artist, art)
    except Exception as e:
        logger.error(f"Unexpected error processing {url}: {e}", exc_info=True)
        with _failed_lock:
            failed_downloads.append(url)

if __name__ == "__main__":
    url_input = input("Enter SoundCloud URL(s) (single or playlist, separate multiple with commas): ")
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

    print(f"Found {len(urls)} track{'s' if len(urls) != 1 else ''}. Processing...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(process_track, u) for u in urls]
        for _ in tqdm(as_completed(futures), total=len(futures), desc="Tracks", unit="track"):
            pass

    clean_non_mp3()
    if failed_downloads:
        print("\nFailed items:")
        for it in failed_downloads:
            print(" -", it)
    else:
        print("\nAll tasks completed successfully!")
