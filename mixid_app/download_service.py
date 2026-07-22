"""Media conversion, tagging, and user-facing download error helpers."""

import os
import subprocess

import requests
from mutagen.id3 import APIC, TALB, TIT2, TPE1
from mutagen.mp3 import MP3


def convert_to_mp3(source, destination):
    if os.path.exists(destination):
        raise FileExistsError(
            f"refusing to replace existing output: {destination}",
        )
    result = subprocess.run(
        [
            "ffmpeg",
            "-n",
            "-i",
            source,
            "-b:a",
            "320k",
            "-f",
            "mp3",
            "-loglevel",
            "error",
            destination,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        if os.path.exists(destination):
            os.remove(destination)
        raise RuntimeError(f"ffmpeg: {result.stderr.strip()[-300:]}")
    if source != destination:
        os.remove(source)


def embed_metadata(mp3_file, title, artist, art_url):
    audio = MP3(mp3_file)
    if audio.tags is None:
        audio.add_tags()
    audio.tags["TIT2"] = TIT2(encoding=3, text=title)
    audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
    audio.tags["TALB"] = TALB(encoding=3, text="SCRipper")
    if art_url:
        try:
            response = requests.get(art_url, timeout=10)
            response.raise_for_status()
            mime = response.headers.get("Content-Type", "image/jpeg")
            audio.tags["APIC"] = APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=response.content,
            )
        except requests.RequestException:
            pass
    audio.save(v2_version=3)


def friendly_download_error(url, error):
    message = str(error)
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    name = f"SoundCloud track #{slug}" if "api-v2.soundcloud.com/tracks/" in url else slug
    lowered = message.lower()
    if "404" in message:
        reason = "no longer available (deleted or removed from the platform)"
    elif "401" in message or "403" in message or "authorization" in lowered:
        reason = (
            "blocked by the platform (403) — either temporary rate limiting "
            "(wait and retry) or authentication is needed (capture cookies "
            "in the 🍪 section)"
        )
    elif any(word in lowered for word in ("country", "geo", "region")):
        reason = "not available in your region"
    elif "429" in message or "too many requests" in lowered:
        reason = "rate limited by the platform — wait a while and retry"
    elif "private" in lowered:
        reason = "private track — capture cookies for an account that can access it"
    else:
        reason = message.replace("ERROR: ", "")[:160]
    return f"{name} — {reason}"
