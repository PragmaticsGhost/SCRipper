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

## First start

### Windows (easiest)

You do not need to install Python, FFmpeg, or Panako yourself.

1. Get the project:
   - On GitHub, select **Code → Download ZIP**, then use **Extract All**. Do
     not run setup from inside the ZIP preview.
   - Or run `git clone https://github.com/PragmaticsGhost/SCRipper.git`.
2. Open the project folder (`SCRipper-main` from a ZIP download or `SCRipper`
   from Git), then open **mixid_app**.
3. Double-click **setup.bat**.

The script offers to install **Docker Desktop** if needed, starts Docker,
builds the app, waits until it is live, and opens
[http://localhost:8080](http://localhost:8080).

If Docker Desktop was just installed, Windows may require a sign-out or
restart. Do that, then double-click `setup.bat` again.

The first build normally takes about **10 minutes** because it compiles the
Panako fingerprinting engine. Lots of terminal output during this build is
normal. Later starts usually take only a few seconds. Setup is complete when
the terminal says:

```text
SCRipper Suite is running:  http://localhost:8080
```

The containers keep running after the setup window closes.

### Your first five minutes

- To test **SCRipper**, paste a public SoundCloud or YouTube track URL,
  select **Check URLs**, choose a destination folder, and select
  **Download**.
- To test **MixID**, first add or re-index a music folder. Once fingerprinting
  finishes, drop in a recorded mix and wait for its tracklist.
- Leave cookies alone at first. Public tracks work without them; the
  **Cookies** section is for private, age-restricted, or account-only tracks.

### Starting and stopping later

Double-click `setup.bat` whenever you want to start or update the app. It is
safe to run repeatedly. If the browser does not open automatically, visit
[http://localhost:8080](http://localhost:8080).

To stop the app, open PowerShell in `mixid_app` and run:

```powershell
docker compose stop
```

Your music, fingerprints, and past scans are preserved when the app is
stopped or rebuilt.

### If first start does not finish

- **Docker was just installed:** restart Windows, open Docker Desktop, wait
  until its engine is running, and run `setup.bat` again.
- **The containers started but no page appeared:** wait another minute, then
  open [http://localhost:8080](http://localhost:8080) yourself.
- **Port 8080 is already in use:** stop the other app using that port, then
  run setup again.
- **You need the error details:** run
  `docker compose logs --tail 100 scripper` from `mixid_app`.

### Manual setup (macOS, Linux, or Windows)

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) or
Docker Engine with Compose, then run:

```bash
git clone https://github.com/PragmaticsGhost/SCRipper.git
cd SCRipper/mixid_app
docker compose up -d --build
```

Open [http://localhost:8080](http://localhost:8080). The first build normally
takes about 10 minutes.

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

Downloads are 320kbps MP3 with title/artist/cover-art tags and run 3 at a
time. Completed files are promoted atomically: an existing track is never
overwritten, and an unexpected name collision gets a numbered filename.
A per-folder download archive also avoids fetching known URLs again.
A live console shows per-file progress; failures are reported in plain
language (deleted track, region-locked, rate-limited, needs cookies, etc.).

### Files panel — getting music in and out

You never need to touch the container or project folders directly:

- **Play** any track in the browser.
- **⬇ Download** a single file, or **⬇ Download folder (.zip)** — straight
  to your browser's Downloads folder.
- **⬆ Add tracks…** — upload your own audio files into a folder so they can
  be indexed and played. Existing filenames are preserved rather than
  overwritten; conflicts are reported in the UI.

### Cookies (optional — for private / Go+ tracks)

Without cookies, only publicly available tracks can be downloaded. To use
your own account session, use the built-in login browser — no extensions,
no manual exports:

1. In the 🍪 Cookies section, click **Open login browser** — the app starts
   a real Firefox (in a throwaway container) on demand and opens it in a new
   tab (it starts on soundcloud.com).
2. Log in normally. Your password goes directly to soundcloud.com — the app
   never sees it. Visit youtube.com too if you want YouTube cookies.
3. Back in the app, click **Capture cookies**. The login browser is
   **shut down automatically** once cookies are captured.

The login browser only runs while you're using it — no 24/7 ~500MB
container. You can also close it manually with the **Close login browser**
button. Cookies stay on your machine in `mixid_app/cookies/` (gitignored)
and can be removed anytime with one click. Re-capture when downloads start
failing — sessions expire.

Tips:
- If you sign in to SoundCloud with Google SSO, Google can be picky about
  unfamiliar browsers — the direct email login is smoother.
- The very first "Open login browser" may take a bit longer while Docker
  pulls the Firefox image; after that it starts in a couple of seconds.

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
2. Watch it work. The mix waveform fills in behind a moving scan head, the
   progress line reports live detail such as
   `Scanning 22:20 / 51:51 · 13 tracks found · 12s`, and each match streams
   into a console below it (`♪ 4:15 — Luckey`) as it is recognised. A brief
   **Building tracklist** stage runs once scanning finishes.
3. Read the tracklist.

A 50-minute mix typically finishes in well under a minute against a
library of a few hundred tracks.

Each identified row shows:

- the **timestamp** where that track starts in your mix,
- the **track title** from your library,
- its musical **key** (for example `Dbm`), analysed once and cached,
- its **BPM**. When you played the track pitched, this reads
  `138 → 140 BPM · +1.4%` — the track's own BPM, the BPM it actually
  played at in your mix, and the shift you used.

Stretches that matched nothing appear as `— track not identified —`.

A track played only briefly — or tucked under a tight blend — is sometimes
recognised in just one short window. These are shown with a **weak** tag
(and marked `(weak)` in the copied tracklist) so you know they rest on
thinner evidence than the rest. Two kinds of thin match are dropped as
artifacts rather than shown: near-silent noise, and a re-match of a track
that played moments earlier (usually its audio bleeding through a blend —
a vocal riding the next track's instrumental — not a real second play).

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

## Stopping and managing jobs

Indexing, mix scans, and downloads can all be stopped while they run.

- Every progress bar has a **Stop** button — **Stop indexing**, **Stop
  scanning**, or **Stop downloading**. Stopping ends the underlying
  fingerprinting or download process itself, not just the progress display.
- An **Active jobs** panel appears in the MixID Library card whenever work is
  queued or running. It lists each job with its type, status, progress, and
  age, and offers a **Stop** button per job plus **Stop all**. Use it when a
  job looks stuck, or to clear work left behind by a tab you closed earlier.
- Starting a new mix scan automatically cancels any earlier scan still
  running or queued, so an abandoned scan never blocks a new one.

What happens to partial work:

- **Scan stopped** — the uploaded mix is discarded and no tracklist is
  produced. Only a scan that runs to completion saves a tracklist.
- **Indexing stopped** — tracks already fingerprinted are kept. Run the index
  again to finish the remainder.
- **Downloads stopped** — finished files are kept, and the results panel
  reports how many completed.

---

## Everyday troubleshooting

- **"The fingerprint library has changed … re-index"** — MixID pauses
  identification when an indexed file was edited, moved, or deleted outside
  the app, because the fingerprint database no longer matches your files. Run
  an index job on any library folder; it rebuilds safely and scanning works
  again.
- **A track was skipped as a duplicate** — indexing fingerprints each new
  track against the library first. Edits and bootlegs that share long
  stretches of audio with something you already own can be caught this way.
  The index result names both files so you can judge whether it was a real
  duplicate.
- **A scan produced no tracklist** — it was stopped, or superseded by a newer
  scan.
- **Nothing was identified** — confirm the Library panel reports
  fingerprinted tracks, then try lowering **Min matched segments per track**
  to `1` for a more sensitive (and noisier) match.
- **A key or BPM looks wrong** — both are detected automatically and cached.
  A `TKEY` or `TBPM` tag on the file (for example written by Rekordbox) always
  wins over detection, so tagging a stubborn track corrects it permanently.

---

## Security

- Both the app (`8080`) and the login browser (`5800`) bind to `127.0.0.1`
  only — not reachable from other machines.
- The app rejects requests with a non-localhost `Host` header (blocks
  DNS-rebinding from sites you visit) and rejects browser requests that
  declare a different `Origin`, `Referer`, or fetch site.
- The container drops to an unprivileged user for all work (yt-dlp, ffmpeg,
  Panako).
- Captured cookies are written owner-only (`0600`) and gitignored — treat
  `cookies/cookies.txt` as a password.
- **Docker isolation:** the media-processing app has no Docker socket or
  Docker CLI. A separate, minimal `browser-controller` service owns the
  socket and exposes only fixed start/stop/status operations for the login
  browser; it accepts no Docker arguments from the web app. It labels the
  container it creates and refuses to stop or replace a same-named container
  without that ownership label. The controller is still a host-trusted
  component because it owns the socket, so it has no published port and
  should remain on the private Compose network.

If your machine has **other local users** you don't fully trust, set
`LOGIN_BROWSER_PASSWORD` in `.env` so they can't drive the login browser
(which holds your streaming session).

Do **not** put this behind a public reverse proxy or expose the ports — it
has no user authentication and downloads with your session.

---

## Operations and development

The container serves Flask through Gunicorn with one process and a threaded
worker. The single process is intentional because job queues and retained job
state are in memory; the threads allow health checks and API requests to remain
responsive while work is running.

For implementation-level documentation, see the complete session
[engineering changelog](CHANGELOG.md) and the current-state
[engineering guide](ENGINEERING_GUIDE.md).

- `GET /api/health` is a lightweight liveness check.
- `GET /api/ready` checks worker threads, writable storage, and Panako.
- Compose and the Docker image use the liveness endpoint for health checks.

Run the same quality gate used for a release from PowerShell:

```powershell
.\scripts\ci.ps1
```

Or from a POSIX shell:

```bash
sh scripts/ci.sh
```

The gate validates Compose configuration, checks both Dockerfiles, builds and
runs the dedicated test image (Ruff plus the regression and native-audio smoke
tests), reports dependency freshness, and then builds the deployable images. The deployable image is
multi-stage: compilers, Git, headers, and the full JDK stay in the native build
stage and are not shipped in production.

### Keeping downloads working

Site extractors go stale quickly: YouTube changes its player and an old
`yt-dlp` starts failing with "page needs to be reloaded" errors or HTTP 403 on
download, while SoundCloud keeps working. The quality gate therefore runs a
dependency freshness report:

```bash
docker run --rm scripper-suite-test python3 scripts/check_updates.py
```

It compares every pin in `requirements.lock` against PyPI and prints two kinds
of finding:

- **Updates available** — a newer release exists and this image can install it.
  Regenerate `requirements.lock` and rebuild.
- **Blocked by Python version** — a newer release exists but needs a newer
  interpreter than the image ships, so pip silently keeps the old pin. This is
  the failure mode that once left `yt-dlp` ten months out of date on Debian
  bullseye (Python 3.9) and broke YouTube downloads. Fixing it means raising
  the base image, not just editing the pin.

The check is informational and never fails the gate; pass `--strict` to make
it exit non-zero when updates are pending. The runtime currently targets
Debian bookworm (Python 3.11).

The application is organized around small service modules: configuration and
application state, bounded job queues, metadata storage, downloads, route
registration, atomic JSON persistence, runtime cleanup, and worker lifecycle.
`app.py` remains the orchestration and request-handler layer.

## Notes

- The fingerprint database and scan history live in a Docker volume
  (`panako-db`) and survive container rebuilds. `docker volume rm` it to
  start fresh.
- The fingerprint manifest records file size and modification time. If an
  indexed file is changed or deleted outside the app, identification is
  paused until the next index job safely rebuilds the Panako database. The
  first index after upgrading an older path-only manifest also rebuilds it.
  An unreadable manifest is preserved with an `.invalid-*` suffix and forces
  a full-library rebuild rather than being mistaken for an empty library.
- Panako indexing and identification share one serialized worker because the
  fingerprint store is single-writer. Downloads and metadata analysis use
  separate workers, and identification processes have a duration-based
  end-to-end deadline so either kind of work cannot wedge the other
  indefinitely. Job queues and request collections are bounded; excess work
  receives a busy or validation response instead of growing memory without
  limit.
- BPM, key, and duration analysis is cached by library-relative path plus file
  size and modification time. Duplicate filenames in different folders no
  longer collide, and replacing a file automatically invalidates its analysis.
  Identification reads cached metadata only and warms missing entries in the
  background, keeping expensive analysis off the Panako worker's critical path.
- Container base images, native source revisions, and Python packages are
  pinned to immutable versions and hashes for reproducible builds. The Gradle
  wrapper distribution is checksum-verified as well. Upgrade these pins
  deliberately and run the quality gate before rebuilding.
- History, waveform, and metadata JSON files are replaced atomically. Corrupt
  state is quarantined with an `.invalid-*` suffix instead of being silently
  overwritten. Startup cleanup removes stale upload/download scratch data and
  bounds the waveform cache without following paths outside the configured
  scratch roots.
- Tabs, drop zones, folder rows, media controls, and waveforms support keyboard
  navigation and accessible labels. Motion-heavy visualizers and animations
  are disabled when the browser requests reduced motion.
- Finished job details are retained for up to 24 hours, capped at the 100 most
  recent jobs; active jobs are never pruned.
- A rotating debug log is written to `mixid_app/logs/scripper.log`
  (gitignored).
- Respect the terms of service of the platforms you download from and the
  rights of the artists whose music you use.
