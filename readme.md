# SCRipper Suite

A private, localhost-only DJ toolbox with two parts:

- **SCRipper** downloads SoundCloud and YouTube tracks as 320kbps MP3s with
  metadata and cover art. It also lets you browse, play, upload, and export
  your music library.
- **MixID** fingerprints your library, scans a recorded DJ mix, and builds a
  timestamped tracklist with BPM, key, and tempo shift for each match.

Everything runs on your computer. MixID matches only against your own music
library; it is not a global music-recognition service, and your audio is not
uploaded anywhere.

> **Personal use only.** SCRipper uses your own streaming-service session when
> cookies are enabled. Keep the app on your own computer and do not expose it
> to the internet.

## First start on Windows

You do not need to install Python, FFmpeg, or Panako yourself. The setup script
runs the complete app in Docker.

### 1. Get the project

Choose either method:

- On GitHub, select **Code → Download ZIP**, then right-click the downloaded
  ZIP and choose **Extract All**. Do not run the setup script from inside the
  ZIP preview.
- Or clone it with Git:

  ```powershell
  git clone https://github.com/PragmaticsGhost/SCRipper.git
  ```

### 2. Run the setup script

Open the project folder (`SCRipper-main` from a ZIP download or `SCRipper`
from Git), then open **mixid_app** and double-click **setup.bat**.

The script will:

1. Offer to install Docker Desktop if it is missing.
2. Start Docker Desktop and wait for it to be ready.
3. Build and start SCRipper Suite.
4. Open [http://localhost:8080](http://localhost:8080) in your browser.

If Docker Desktop was just installed, Windows may ask you to sign out or
restart. Do that, then double-click `setup.bat` again.

The first build normally takes about **10 minutes** because Panako is compiled
from source. You will see a lot of build output in the terminal; that is
expected. Later starts usually take only a few seconds.

Setup is complete when the terminal says:

```text
SCRipper Suite is running:  http://localhost:8080
```

The containers keep running after you close the setup window.

### 3. Try the app

For a quick first test:

- In **SCRipper**, paste a public SoundCloud or YouTube track URL, select
  **Check URLs**, choose a destination folder, and select **Download**.
- In **MixID**, first add or re-index a folder from your music library. After
  fingerprinting finishes, drop in a recorded mix to identify it.

Cookies are optional. Public tracks work without them; use the app's
**Cookies** section only when a private, age-restricted, or account-only track
requires your logged-in session.

Long jobs can be stopped at any time. Each progress bar has a **Stop** button,
and an **Active jobs** panel lists everything queued or running so you can stop
an individual job or all of them. See the
[complete usage guide](mixid_app/README.md) for what happens to partial work.

## Starting and stopping later

To start or update the app, double-click `mixid_app\setup.bat` again. The
script is safe to run repeatedly.

If the browser does not open automatically, go to
[http://localhost:8080](http://localhost:8080).

To stop the app, open PowerShell in the `mixid_app` folder and run:

```powershell
docker compose stop
```

Your music, fingerprints, and past scan results are preserved when the app is
stopped or rebuilt.

## Where your music is stored

By default, the project folder containing `mixid_app` is mounted as the music
library. Downloads go into the destination folder you choose in SCRipper, and
the files remain directly accessible from Windows.

To use a different library location, create `mixid_app\.env` containing a
Windows path such as:

```dotenv
MUSIC_DIR=C:/Users/you/Music
```

Restart the app after changing `.env`.

## If the first start does not finish

- **Docker was just installed:** restart Windows, open Docker Desktop, wait
  until its engine is running, and run `setup.bat` again.
- **The containers started but no page appeared:** wait another minute and
  open [http://localhost:8080](http://localhost:8080) yourself.
- **Port 8080 is already in use:** stop the other app using that port, then
  run `setup.bat` again.
- **You need the error details:** open PowerShell in `mixid_app` and run:

  ```powershell
  docker compose logs --tail 100 scripper
  ```

## macOS and Linux

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) or
Docker Engine with Compose, then run:

```bash
git clone https://github.com/PragmaticsGhost/SCRipper.git
cd SCRipper/mixid_app
docker compose up -d --build
```

Open [http://localhost:8080](http://localhost:8080). The first build normally
takes about 10 minutes.

On Linux, if the app cannot write to your music folder, add your host user and
group IDs to `mixid_app/.env`:

```dotenv
PUID=1000
PGID=1000
```

Use `id -u` and `id -g` to find the correct values.

## More documentation

- [Complete usage guide](mixid_app/README.md)
- [Engineering and architecture guide](mixid_app/ENGINEERING_GUIDE.md)
- [Engineering changelog](mixid_app/CHANGELOG.md)

Respect the terms of service of the platforms you use and the rights of the
artists whose music you download.
