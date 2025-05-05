#!/usr/bin/env python3
import sys
import subprocess
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# —— Bootstrap Python dependencies ——
def ensure_package(pkg_name, import_name=None):
    """Import import_name (or pkg_name); if missing, install pkg_name via pip."""
    mod = import_name or pkg_name
    try:
        __import__(mod)
    except ImportError:
        print(f"→ {mod!r} not found, installing {pkg_name}…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg_name])

for pkg, imp in [
    ("imageio-ffmpeg", "imageio_ffmpeg"),
    ("yt-dlp",       "yt_dlp"),
    ("ffmpeg-python","ffmpeg"),
    ("mutagen",      "mutagen"),
    ("tqdm",         "tqdm"),
    ("requests",     "requests"),
    ("psutil",       "psutil"),
]:
    ensure_package(pkg, imp)

import yt_dlp
import ffmpeg
import requests
import psutil
from tqdm import tqdm
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from imageio_ffmpeg import get_ffmpeg_exe

# —— Kill Chrome/Chromium for cookies ——
def kill_chrome_if_running():
    print("Search and Destroy Initiated for all Chrome instances (required for yt-dlp to read cookies)")
    killed = False
    for proc in psutil.process_iter(("name","cmdline")):
        name = (proc.info["name"] or "").lower()
        cmd  = " ".join(proc.info.get("cmdline") or []).lower()
        if "chrome" in name or "chromium" in name or "chrome" in cmd or "chromium" in cmd:
            try:
                proc.kill()
                killed = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    print("→ Chrome/Chromium instances eradicated." if killed else "→ No Chrome/Chromium found.")

# —— FFmpeg setup ——
FFMPEG_BIN = get_ffmpeg_exe()
if not os.path.isfile(FFMPEG_BIN):
    raise RuntimeError(f"FFmpeg not found at {FFMPEG_BIN}")

session = requests.Session()
OUTPUT_DIR = "downloads"
os.makedirs(OUTPUT_DIR, exist_ok=True)
failed_downloads = []

# —— yt-dlp options ——
YDL_OPTS = {
    "format":           "bestaudio/best",
    "outtmpl":          f"{OUTPUT_DIR}/%(title)s.%(ext)s",
    "writethumbnail":   True,
    "ffmpeg_location":  FFMPEG_BIN,
    "cookiesfrombrowser": ("chrome",),
    # let our code handle retries
    "retries":          0,
    "sleep_requests":   5,
}

def download_soundcloud_track(url):
    backoff = 30           # initial wait on rate-limit
    max_backoff = 600      # cap at 10 minutes
    while True:
        try:
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                info = ydl.extract_info(url, download=True)
            break   # success!
        except Exception as e:
            msg = str(e)
            if "HTTP Error 429" in msg or "Too Many Requests" in msg or "rate limit" in msg.lower():
                print(f"↺ Rate limited on {url}. Sleeping for {backoff}s before retrying…")
                time.sleep(backoff)
                # exponential back‐off
                backoff = min(backoff * 2, max_backoff)
                continue
            # non‐rate-limit error: let caller handle it
            raise

    title  = info.get("title","Unknown")
    artist = info.get("uploader","Unknown")
    art    = info.get("thumbnails",[{}])[-1].get("url")
    ext    = info.get("ext","m4a")
    return (
        os.path.join(OUTPUT_DIR, f"{title}.{ext}"),
        os.path.join(OUTPUT_DIR, f"{title}.mp3"),
        title, artist, art
    )

def convert_to_mp3(inp, out):
    (
        ffmpeg
        .input(inp)
        .output(out, audio_bitrate="320k", format="mp3", threads=5, loglevel="quiet")
        .run(overwrite_output=True, cmd=FFMPEG_BIN)
    )
    os.remove(inp)

def embed_metadata(mp3_file, title, artist, art_url):
    try:
        audio = MP3(mp3_file, ID3=ID3)
    except ID3NoHeaderError:
        audio = MP3(mp3_file); audio.add_tags()
    audio.tags["TIT2"] = TIT2(encoding=3, text=title)
    audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
    audio.tags["TALB"] = TALB(encoding=3, text="SoundCloud")
    if art_url:
        try:
            r = session.get(art_url, timeout=10); r.raise_for_status()
            mime = r.headers.get("Content-Type","image/jpeg")
            audio.tags["APIC"] = APIC(
                encoding=3, mime=mime, type=3, desc="Cover", data=r.content
            )
        except:
            failed_downloads.append(f"embed_art:{title}")
    audio.save(v2_version=3)

def process_track(url):
    try:
        inp, out, title, artist, art = download_soundcloud_track(url)
        convert_to_mp3(inp, out)
        embed_metadata(out, title, artist, art)
    except Exception:
        failed_downloads.append(url)

def clean_non_mp3():
    for f in os.listdir(OUTPUT_DIR):
        if not f.lower().endswith(".mp3"):
            os.remove(os.path.join(OUTPUT_DIR, f))

if __name__ == "__main__":
    kill_chrome_if_running()
    url = input("Enter SoundCloud URL (track or playlist): ")
    if "/sets/" in url:
        with yt_dlp.YoutubeDL({"quiet":True,"extract_flat":True}) as ydl:
            info = ydl.extract_info(url, download=False)
        urls = [e["url"] for e in info.get("entries",[])]
    else:
        urls = [url]

    print(f"Found {len(urls)} track{'s' if len(urls)!=1 else ''}. Processing…")
    with ThreadPoolExecutor(max_workers=10) as ex:
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
