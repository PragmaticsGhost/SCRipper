SoundCloud Scraper

A Python-based command-line tool to download tracks and playlists from SoundCloud, convert them to 320kbps MP3, and embed metadata and album art.

Features

Individual Tracks & Playlists: Download a single track or an entire SoundCloud playlist.

Cookie-Based Authentication: Extract and use Chrome/Chromium cookies for private or age-restricted content.

High-Quality MP3 Conversion: Convert to 320kbps MP3 using ffmpeg under the hood.

Rich Metadata Embedding: Embed ID3 tags (Title, Artist, Album) and cover art automatically.

Progress Indicators: Real-time tqdm progress bars for track processing.

Rate Limit Handling: Automatic retries and back-off on HTTP 429 (Too Many Requests).


🛠️ Prerequisites

Python ≥ 3.7

Git (for cloning the repository)

Google Chrome or Chromium (for cookie extraction)

FFmpeg (managed automatically via imageio-ffmpeg)

🛠️ Installation

# 1. Clone the repo
git clone https://github.com/<your-username>/soundcloud-scraper.git
cd soundcloud-scraper

# 2. (Optional) Create and activate a venv
python -m venv venv
# macOS/Linux
source venv/bin/activate
# Windows
venv\Scripts\activate

# 3. Install dependencies (or let the script auto-bootstrap)
pip install -r requirements.txt

Tip: The script will auto-install any missing Python packages on first run.

🚀 Usage

python SCRipper2.py

When prompted, paste a SoundCloud track or playlist URL.

The script will:

Kill any running Chrome/Chromium instances to ensure fresh cookie access.

Download and convert audio to MP3.

Embed ID3 tags and album art.

⚙️ Configuration

All major settings live in the YDL_OPTS dictionary at the top of SCRipper2.py:

YDL_OPTS = {
    "format": "bestaudio/best",
    "outtmpl": f"{OUTPUT_DIR}/%(title)s.%(ext)s",
    "writethumbnail": True,
    "ffmpeg_location": FFMPEG_BIN,
    "cookiesfrombrowser": ("chrome",),
    "retries": 5,                # HTTP retry count
    "extractor_retries": 5,      # extractor retry count
    "retry_sleep": [
        "http:30",               # sleep 30s on HTTP errors (e.g. 429)
        "extractor:30"           # sleep 30s on extractor errors
    ],
    "sleep_requests": 5,         # pause 5s between each HTTP request
}

Feel free to tweak these values to match your rate-limit needs or environment.

🐞 Troubleshooting

"No Chrome/Chromium found": Make sure Chrome/Chromium is installed and has been launched at least once.

Rate limit warnings persist: Increase sleep_requests (e.g. to 10 s) or adjust retry_sleep intervals.

FFmpeg errors: Verify ffmpeg is installed or let imageio-ffmpeg download the correct binary.

🤝 Contributing

Contributions welcome! Please:

Fork the repo

Create a feature branch (git checkout -b feature/YourFeature)

Commit your changes (git commit -m 'Add YourFeature')

Push to the branch (git push origin feature/YourFeature)

Open a Pull Request

📄 License

This project is licensed under some kinda License. See LICENSE for details.


_____________________________________________________________________________

Readme generated with GenAI, excuse any weirdness it's close enough
