import os
import yt_dlp
import ffmpeg
import requests
from tqdm import tqdm
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from pathlib import Path

# Output directory
OUTPUT_DIR = str(Path.home() / "Downloads")
# print (OUTPUT_DIR)


def download_soundcloud_track(url):
    """Download SoundCloud track and return file path & metadata."""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{OUTPUT_DIR}/%(title)s.%(ext)s",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}],
        "writethumbnail": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    title = info.get("title", "Unknown")
    artist = info.get("uploader", "Unknown")
    album_art = info.get("thumbnails", [{}])[-1].get("url", None)
    filename = f"{OUTPUT_DIR}/{title}.mp3"

    return filename, title, artist, album_art


def embed_metadata(mp3_file, title, artist, album_art_url):
    """Embed metadata and album art into the MP3 file."""
    try:
        audio = MP3(mp3_file, ID3=ID3)
    except ID3NoHeaderError:
        audio = MP3(mp3_file)
        audio.add_tags()

    audio.tags["TIT2"] = TIT2(encoding=3, text=title)
    audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
    audio.tags["TALB"] = TALB(encoding=3, text="SoundCloud")

    if album_art_url:
        try:
            response = requests.get(album_art_url, stream=True)
            if response.status_code == 200:
                with open("temp.jpg", "wb") as img:
                    for chunk in response.iter_content(1024):
                        img.write(chunk)
                with open("temp.jpg", "rb") as img:
                    audio.tags["APIC"] = APIC(
                        encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img.read()
                    )
                os.remove("temp.jpg")
        except Exception as e:
            print(f"Failed to download album art: {e}")

    audio.save()
    print(f"Metadata added to {mp3_file}")


def download_playlist(url):
    """Download all tracks from a SoundCloud playlist."""
    ydl_opts = {
        "quiet": True,
        "extract_flat": True,  # Get list of track URLs
        "force_generic_extractor": True
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        playlist_info = ydl.extract_info(url, download=False)

    if "entries" in playlist_info:
        track_urls = [entry["url"] for entry in playlist_info["entries"]]
        print(f"Found {len(track_urls)} tracks. Downloading...")

        for track_url in tqdm(track_urls, desc="Downloading Tracks"):
            try:
                mp3_file, title, artist, album_art_url = download_soundcloud_track(track_url)
                embed_metadata(mp3_file, title, artist, album_art_url)
            except Exception as e:
                print(f"Error downloading {track_url}: {e}")

        print("Playlist download complete!")
    else:
        print("No tracks found in playlist.")


if __name__ == "__main__":
    url = input("Enter SoundCloud URL (track or playlist): ")

    if "/sets/" in url:  # Playlists contain "/sets/"
        download_playlist(url)
    else:
        mp3_file, title, artist, album_art_url = download_soundcloud_track(url)
        embed_metadata(mp3_file, title, artist, album_art_url)

    print("Download complete!")
