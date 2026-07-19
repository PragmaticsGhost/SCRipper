# SCRipper Suite

A personal-use, self-hosted web app for DJs, with two tabs:

- **SCRipper** — download tracks from SoundCloud/YouTube as 320kbps MP3s
  with embedded metadata and cover art; browse, play, upload, and export
  your music library.
- **MixID** — fingerprint your music library, then upload a recorded DJ mix
  and get back a timestamped tracklist. Works even when tracks were
  tempo/pitch-shifted (harmonic mixing), thanks to
  [Panako](https://github.com/JorenSix/Panako). Matching runs entirely
  against *your* library — it is not a Shazam-style global lookup, and no
  audio ever leaves your machine.

> **Personal use only.** This app downloads with *your* streaming-service
> session and has no user authentication. Run it on your own machine —
> do not expose it to the internet.

---

## Setup

### Windows (easiest — one script)

1. Download or clone this repo.
2. Open the `mixid_app` folder and **double-click `setup.bat`**.

The script (`setup.bat` → `setup.ps1`) does everything for you:

- Installs **Docker Desktop** if it isn't present (via `winget`).
- Starts the Docker engine and waits for it to be ready.
- Builds the app and starts it (`docker compose up -d --build`).
- Waits until the app is live, then opens **http://localhost:8080**.

The **first build takes ~10 minutes** because it compiles the Panako
fingerprinting engine from source; later starts take seconds. Just re-run
`setup.bat` any time you want to start the app — it's safe to run
repeatedly.

> If Windows had to install Docker Desktop fresh, it may require a
> sign-out or restart before the engine works. The script detects this and
> tells you to re-run it once that's done.

### Manual (any OS)

Requirements:
[Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker
Engine + Compose on Linux).

```bash
git clone <this-repo>
cd <repo>/mixid_app
docker compose up -d --build     # first build ~10 min (compiles Panako)
```

Open **http://localhost:8080**.

### Where your music lives

By default the folder *above* `mixid_app/` (the repo root) is your music
root — every subfolder there is browsable, indexable, and a valid download
destination. To point somewhere else, create a `.env` file next to
`docker-compose.yml`:

```
MUSIC_DIR=C:/Users/you/Music        # music library root
PUID=1000                           # Linux only: your host user id (`id -u`)
PGID=1000                           # Linux only: your host group id (`id -g`)
LOGIN_BROWSER_PASSWORD=somepassword # optional, see Security
```

On **Linux**, if downloads fail with permission errors writing to your music
folder, set `PUID`/`PGID` to your own ids so the container writes as you. On
**Windows/macOS** (Docker Desktop) the defaults are fine.

---

## SCRipper tab — downloading & managing music

### Download tracks

1. Paste one or more SoundCloud/YouTube URLs (single tracks or playlists),
   separated by commas or new lines.
2. Click **Check URLs**. Playlists are expanded and shown with a track
   count and a checkbox so you can confirm before grabbing hundreds of
   tracks.
3. Pick a destination folder (or create a new one), optionally tick
   **Index into MixID after download**, and hit **Download**.

Downloads are 320kbps MP3 with title/artist/cover-art tags, run 3 at a
time, and skip anything already present (a per-folder download archive).
A live console shows per-file progress; failures are reported in plain
language (deleted track, region-locked, rate-limited, needs cookies, etc.).

### Files panel — getting music in and out

You never need to touch the container or project folders directly:

- **Play** any track in the browser.
- **⬇ Download** a single file, or **⬇ Download folder (.zip)** — straight
  to your browser's Downloads folder.
- **⬆ Add tracks…** — upload your own audio files into a folder so they can
  be indexed and played.

### Cookies (optional — for private / Go+ tracks)

Without cookies, only publicly available tracks can be downloaded. To use
your own account session, use the built-in login browser — no extensions,
no manual exports:

1. In the 🍪 Cookies section, click **Open login browser** — a real Firefox
   running inside the container opens in a new tab (it starts on
   soundcloud.com).
2. Log in normally. Your password goes directly to soundcloud.com — the app
   never sees it. Visit youtube.com too if you want YouTube cookies.
3. Back in the app, click **Capture cookies**.

Cookies stay on your machine in `mixid_app/cookies/` (gitignored) and can be
removed anytime with one click. Re-capture when downloads start failing —
sessions expire.

Tips:
- If you sign in to SoundCloud with Google SSO, Google can be picky about
  unfamiliar browsers — the direct email login is smoother.
- The login browser uses ~500MB RAM while running. After capturing you can
  stop it with `docker compose stop login-browser`; captured cookies keep
  working, and `docker compose up -d` brings it back.

Alternatives: upload a `cookies.txt` export (Netscape format, e.g. from the
"Get cookies.txt LOCALLY" extension) in the same 🍪 section, or — for Chrome
users on the host — run `python dump_cookies.py`.

---

## MixID tab — identifying your mixes

### Build your library

- **Add a folder from my computer…** — pick any folder (including other
  drives); its audio is uploaded into the library and fingerprinted.
- **Or re-index an existing folder** — a dropdown of folders already in your
  music root, for tracks you've downloaded or added.

Indexing skips **duplicates** automatically: before storing a track it's
checked against the existing library with the same fingerprint matcher, so a
copy of a track you already have is reported and skipped rather than
double-stored.

Expand **Indexed folders** to browse what's fingerprinted, each track with a
mini-waveform and a play button.

### Identify a mix

1. Drop a recorded mix (wav/mp3/m4a/flac) into the Identify panel.
2. While it scans, a neon waveform of the mix fills in with a moving scan
   head.
3. Read the tracklist: timestamps, per-track tempo shift, and
   `— track not identified —` markers where nothing in your library matched.

From the tracklist you can:

- **▶ Play any slice** — plays the mix from that timestamp, with a marker
  tracking position on the mix waveform (click the waveform to seek).
- **Name unidentified sections** — click the ✎, type the correct title; it's
  saved with the scan (marked with `*` in the copied tracklist).
- **Copy** the whole tracklist, or **Clear** it.

### Past scans

Every scan is saved. Open **Past scans** to reload a previous mix's
tracklist and waveform without re-uploading (view-only — the audio isn't
retained, so slice playback is disabled for recalled scans). Only the
most-recently scanned mix is kept on disk for slice playback; it's replaced
when you scan a new one.

### Now-playing bar

Playing any track (library or mix slice) shows a bottom player bar with a
clickable/scrubbable waveform, and a live neon 3D EQ visualizer that reacts
to the audio — bass pumps the ridge, snares flash, height maps to a
blue→pink gradient.

---

## Security

- Both the app (`8080`) and the login browser (`5800`) bind to `127.0.0.1`
  only — not reachable from other machines.
- The app rejects requests with a non-localhost `Host` header (blocks
  DNS-rebinding from sites you visit).
- The container drops to an unprivileged user for all work (yt-dlp, ffmpeg,
  Panako).
- Captured cookies are written owner-only (`0600`) and gitignored — treat
  `cookies/cookies.txt` as a password.

If your machine has **other local users** you don't fully trust, set
`LOGIN_BROWSER_PASSWORD` in `.env` so they can't drive the login browser
(which holds your streaming session).

Do **not** put this behind a public reverse proxy or expose the ports — it
has no user authentication and downloads with your session.

---

## Notes

- The fingerprint database and scan history live in a Docker volume
  (`panako-db`) and survive container rebuilds. `docker volume rm` it to
  start fresh.
- Jobs (indexing, identifying, downloading) run one at a time by design —
  the fingerprint store is single-writer.
- A rotating debug log is written to `mixid_app/logs/scripper.log`
  (gitignored).
- Respect the terms of service of the platforms you download from and the
  rights of the artists whose music you use.
