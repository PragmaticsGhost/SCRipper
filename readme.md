📥 SoundCloud MP3 Downloader
A Python-based tool for downloading SoundCloud tracks & playlists in 320kbps MP3.

🚀 Features:

✅ Download single tracks & entire playlists
✅ Convert audio to 320kbps MP3 (using ffmpeg-python)
✅ Embed metadata & album art automatically
✅ No system-wide FFmpeg required (supports portable FFmpeg)
✅ Works on Windows, Mac, and Linux

🔧 Installation

1. Install Dependencies
Run:

sh
Copy
Edit
pip install -r requirements.txt

2. Install FFmpeg
You have three options:

Option 1: Let yt-dlp Download FFmpeg
Run:

sh
Copy
Edit
yt-dlp --install-ffmpeg

Option 2: Use a Portable FFmpeg
Download from Gyan.dev

Extract to SCRipper/ffmpeg/
The script will use this local version.

Option 3: Install via Python Package
Run:

sh
Copy
Edit
pip install imageio[ffmpeg]
This avoids needing a separate FFmpeg binary.

🚀 Usage

1. Run the script
sh
Copy
Edit
python SCRipper.py
Paste a SoundCloud track or playlist URL when prompted.
The downloaded MP3 files will be saved in the downloads/ folder.

2. Example Output
java
Copy
Edit
Enter SoundCloud URL (track or playlist): https://soundcloud.com/artist/song
Downloading...
Converted to 320kbps MP3
Metadata added: Title, Artist, Album Art
Download complete!

🛠 Troubleshooting

1. yt-dlp Not Found?
Try reinstalling:

sh
Copy
Edit
pip install --upgrade yt-dlp

2. FFmpeg Not Found?
Check by running:

sh
Copy
Edit
ffmpeg -version
If it’s missing, follow the installation steps above.

3. Metadata Not Showing?
Try opening the MP3 in VLC or MP3Tag to verify.

⭐ Like This Project?
Feel free to star ⭐ the repository and contribute! 🚀
