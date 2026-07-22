# Engineering changelog

## Scope and baseline

This document records the complete engineering change set made during the
code-review and remediation session. The comparison baseline is repository
commit `f35dee56d6cd9bb2a320f9818ef93f9854da9648` (`Ignore legacy loose
scripts`, 2026-07-19). The other side of the comparison is the current
workspace, including files that have not yet been committed.

The work was performed over several review passes (P1, P2, and P3). Because
those changes currently exist as one workspace diff rather than as separate
commits, the entries below are grouped by engineering subsystem instead of
claiming a commit-by-commit chronology that does not exist.

At a high level, the work changed the application from a mostly monolithic
Flask development-server deployment with unbounded in-process work and
unpinned builds into a bounded, testable, production-served application with
explicit persistence, security, worker, and container boundaries.

## Data-loss prevention and filesystem correctness

### Non-destructive track uploads

- Added `safe_upload.copy_exclusive()`.
- Track uploads now create destination files with `O_CREAT | O_EXCL`, so an
  existing library track cannot be silently truncated or overwritten.
- A failed streaming copy removes only the partial file created by that call.
  It never removes a pre-existing user file.
- `/api/upload-tracks` now limits one request to 500 files and reports
  `saved`, `rejected`, and `conflicts` separately.
- The frontend reports filename conflicts and does not imply that an existing
  file was replaced.

### Conflict-safe download publication

- Download work now happens in a per-track `.scripper-download-*` directory
  under the selected destination folder.
- yt-dlp writes an ID-based filename inside that private directory instead of
  writing a title-derived filename directly into the music library.
- ffmpeg conversion uses `-n` and refuses to replace an existing output.
- The converted file is tagged before it becomes visible at its final library
  name.
- Added Windows-safe MP3 filename handling, including control/forbidden
  character replacement, trailing-dot/space removal, length bounding, and
  reserved DOS device-name handling.
- Added numbered collision allocation (`Track.mp3`, `Track (2).mp3`, and so
  on).
- Final publication uses an atomic hard link on filesystems that support it.
  Docker Desktop bind mounts that reject hard links use an exclusive-create
  streaming fallback. Both paths preserve the no-overwrite guarantee; only
  the hard-link path also provides atomic visibility.
- Private download directories are always removed in a `finally` block.
- Startup cleanup removes abandoned `.scripper-download-*` directories older
  than one day, subject to root-containment and symlink checks.

### Safer temporary and retained mix handling

- Added `discard_mix()` so a failed identification removes its own upload
  without accidentally clearing a newer successful mix.
- Queue rejection removes the just-uploaded mix immediately.
- Identification failure, timeout, invalid duration, and stale-manifest paths
  now consistently discard the failed upload.
- Only the latest successful mix remains registered for slice playback.

## Fingerprint database consistency

### Versioned, content-aware manifest

- Replaced the original path-to-boolean manifest model with manifest version
  2 entries containing file size and nanosecond modification time.
- Added helpers for loading, saving, signing, recording, and classifying
  current/stale files in `fingerprint_manifest.py`.
- A modified, deleted, or replaced audio file is now detected even when its
  path is unchanged.
- Legacy path-only manifests are intentionally treated as stale because they
  cannot prove that the Panako database still represents current bytes.
- If a Panako LMDB exists without a corresponding manifest, the state is
  treated as invalid rather than as an empty, trustworthy library.
- Malformed or structurally invalid manifests are represented explicitly as
  invalid state instead of being silently interpreted as an empty index.
- Invalid manifests are preserved with an `.invalid-*` suffix before a
  rebuild.

### Safe full-library rebuilds

- Identification is blocked with HTTP 409 while the manifest is invalid,
  legacy, or stale.
- The identification worker rechecks manifest state after queueing and again
  after Panako processing, closing the time-of-check/time-of-use window.
- An index job rebuilds the complete dedicated Panako LMDB when consistency
  cannot be proven.
- LMDB deletion is guarded by real-path, parent-directory, and basename checks
  so only the expected `panako_db` directory can be reset.
- The rebuild manifest is written with pending candidates before work starts,
  ensuring an interrupted rebuild remains detectably incomplete.
- Successful stores add a current file signature; failed stores and duplicate
  detections remove the candidate from the manifest.
- `/api/library` now exposes the stale count, and the frontend warns when a
  rebuild is required.

### Duplicate filename correctness

- Panako monitor results retain the full matched path in addition to the
  basename.
- Match aggregation and duplicate detection use the full reference path.
- Basename fallback is allowed only when exactly one current library path has
  that basename. Ambiguous duplicate names are not guessed.
- Metadata cache keys now include library-relative path, file size, and
  nanosecond modification time, preventing collisions between same-named
  tracks in different folders and invalidating cache entries after file
  replacement.

## Work scheduling, resource bounds, and process lifecycle

### Separate bounded queues

- Replaced the single unbounded job queue with three bounded queues:
  - Panako queue: capacity 8.
  - Download queue: capacity 4.
  - Metadata queue: capacity 1000.
- Panako indexing and identification remain serialized because the fingerprint
  store is single-writer.
- Downloads no longer wait behind long fingerprint operations.
- Metadata analysis no longer runs synchronously on the Panako critical path.
- Full queues produce a terminal job error and an HTTP 429 response instead of
  allowing memory to grow without limit.
- Optional post-download indexing now reports a distinct error if the Panako
  queue is full.

### Job registry bounds

- Extracted thread-safe job management into `JobService`.
- Finished jobs expire after 24 hours and are capped at the newest 100.
- Active jobs are never removed by retention pruning.
- Per-job console output is capped at the newest 300 lines.
- Job reads return defensive copies of mutable log and activity data.
- Control characters are removed from user-derived log text to prevent forged
  multiline log entries.

### Cooperative worker lifecycle

- Added `WorkerRuntime` with named, start-once background threads and a shared
  stop event.
- Workers use timed queue reads so shutdown does not depend on injecting
  sentinel jobs.
- Shutdown sets the stop event and joins worker threads with a bounded wait.
- Shutdown is registered with `atexit` and with Gunicorn's `worker_exit` hook.
- Worker health is introspectable for readiness reporting.

### Enforceable subprocess deadlines

- Added process-wide deadline helpers for text-line and binary-chunk streams.
- Reader-to-consumer queues are bounded (256 lines and 16 binary chunks).
- Timeout or consumer failure terminates the entire subprocess process group,
  first with a graceful signal and then with a forced kill if necessary.
- Identification gets a duration-derived end-to-end budget: at least 10
  minutes, normally twice the audio duration, and at most two hours.
- Waveform generation has a separate 10-minute ceiling and shares the overall
  identification deadline.
- Panako progress parsing no longer allows a silent/stuck child process to
  block a worker indefinitely.

### Bounded waveform generation

- Replaced `subprocess.run(..., capture_output=True)` waveform decoding with
  streaming ffmpeg output.
- Audio is decoded to mono, 8 kHz, signed 16-bit PCM.
- PCM is reduced incrementally into a fixed number of RMS buckets; decoded
  audio is no longer held in one large byte string.
- Duration must be finite and positive and is capped at eight hours.
- Added a portable RMS fallback for environments where Python no longer ships
  `audioop`.
- Waveform cache entries are written safely, expire after 90 days, and are
  capped at the 5000 newest entries.

## Metadata and identification quality

### Persistent metadata stores

- Added independently locked persistent stores for duration, BPM, and key.
- The cache distinguishes a missing entry from a cached `None`, preventing
  repeated analysis of files for which a value cannot be determined.
- Cache writes now use atomic JSON replacement.
- Indexing queues background metadata warming for newly indexed and already
  known files.
- Identification reads cached metadata only while collapsing matches and then
  queues missing analysis in the background. This prevents Essentia/aubio work
  from blocking Panako.

### BPM processing

- Preserved the preference for an existing `TBPM` tag.
- Retained aubio beat-grid analysis and the fallback to `aubio tempo` when too
  few beats are available.
- Retained normalization into the normal DJ tempo range and snapping of
  detector output to credible integer/half-BPM values.
- Moved BPM persistence from a basename-keyed cache to the signature-aware
  metadata store.

### Musical-key detection

- Added preference for an existing `TKEY` tag.
- Added Essentia `KeyExtractor` analysis with the EDM-oriented `edmm` profile.
- Analysis uses a bounded, intro-skipping mono window decoded by ffmpeg.
- Essentia sharp spellings are normalized to flat spellings commonly used in
  DJ tools, and minor keys receive an `m` suffix.
- Key values are cached persistently and included in tracklist results.
- The frontend renders key chips beside tempo information.

### Tracklist collapse behavior

- Match runs are grouped by full fingerprint reference instead of basename.
- Expected source-track continuation still suppresses false unidentified gaps
  during blends, loops, or effects.
- Tracklist generation now carries source BPM, played BPM, tempo percentage,
  musical key, match count, and unidentified state.
- Expensive metadata analysis is explicitly disabled during the synchronous
  collapse phase.

## API validation and request security

### Strict request schemas and limits

- Added shared validation for JSON-object requirements, paths, URL lists,
  download requests, and folder names.
- URL fields must be absolute HTTP or HTTPS URLs; `file:`, malformed, empty,
  overlong, and non-string inputs are rejected.
- URL inputs are deduplicated while preserving order.
- Direct resolve requests are capped at 25 URLs.
- Download and expanded-playlist requests are capped at 500 URLs.
- URL, path, and folder-string lengths are bounded.
- `index_after` must be a Boolean.
- Playlist extraction validates the shape returned by yt-dlp and handles
  oversized or invalid playlists without scheduling work.
- The Flask request body limit is explicitly set to 4 GiB.

### History mutation validation

- Manual title edits require a finite numeric start time and a string-or-null
  title.
- Boolean and non-finite numeric values are rejected.
- Only an unidentified entry at the requested timestamp can be renamed.
- Missing entries return 404 instead of silently reporting success.
- A timestamp of zero is handled correctly.

### Browser-origin enforcement

- Retained the localhost Host-header allowlist used to mitigate DNS rebinding.
- Added validation of `Origin`, `Referer`, and `Sec-Fetch-Site` when browsers
  provide them.
- Cross-origin and same-site-but-not-same-origin browser requests are rejected.
- Requests without browser provenance headers remain available to local CLI
  clients.

### Safer frontend DOM construction

- Replaced string-built `<option>` markup with DOM property assignment through
  `setSelectOptions()`.
- User/platform-derived option values and labels are assigned via `.value` and
  `.textContent`, removing an HTML-injection path.
- Dynamic status and result rendering continues to prefer text nodes or
  escaped text for untrusted values.

## Login-browser privilege isolation

### Fixed-purpose controller service

- Replaced the always-running Compose Firefox service with an on-demand,
  throwaway Firefox container.
- Added a minimal `browser-controller` service. It is the only component with
  the Docker socket.
- The main media application talks to the controller over a private HTTP API
  and never accepts or constructs arbitrary Docker arguments from a request.
- The controller exposes only health, status, start, and stop operations.
- Controller request bodies are capped at 1024 bytes.
- Docker operations are serialized with a lock.
- The managed browser has a fixed name, fixed localhost port, fixed profile
  volume, fixed start URL, and digest-pinned image.
- The controller labels the browser it creates and refuses to stop or replace
  a same-named container without that ownership label.
- The controller itself is read-only, uses a tmpfs for `/tmp`, has
  `no-new-privileges`, and publishes no host port.
- Controller shutdown attempts to stop the managed browser.

### Browser and cookie workflow

- Added API endpoints to start, stop, and query the login browser.
- The frontend reserves a blank browser tab synchronously before asynchronous
  startup, avoiding popup blockers after a slow first image pull.
- The UI polls the localhost Firefox port, navigates the reserved tab when
  ready, and exposes a manual close action.
- Cookie capture copies Firefox SQLite, WAL, and shared-memory sidecars before
  reading, avoiding locks on the live profile.
- Only SoundCloud, YouTube, and Google-domain cookies are exported.
- Cookie files are created with owner-only mode `0600`.
- Successful capture automatically stops the login browser and reports stop
  failures separately from capture success.

## Persistent-state durability and cleanup

### Atomic JSON storage

- Added `atomic_write_json()` using a unique same-directory temporary file,
  file flush/fsync, `os.replace`, and directory fsync on POSIX.
- Temporary files are removed on failure.
- Added `load_json()` with independent default values.
- Malformed JSON is moved beside the original with an `.invalid-*` suffix for
  diagnosis instead of being overwritten.
- Scan history, waveform cache, and duration/BPM/key caches now use these
  helpers.
- A directly requested corrupt history record returns HTTP 409 after
  quarantine.

### Startup hygiene

- Added root-contained, non-symlink cleanup for application-owned artifacts.
- Direct upload scratch files older than one day are removed.
- Download scratch directories older than one day are removed recursively only
  when their names carry the application prefix and remain under the music
  root.
- Waveform cache cleanup applies both age and count limits.
- Cleanup results are logged when anything is removed.

## Application structure and server runtime

### Application factory and service object graph

- Added environment-backed, immutable `AppSettings`.
- Added `ApplicationState` to make the settings, jobs, metadata stores, and
  worker runtime discoverable through `Flask.extensions["scripper"]`.
- Added `create_app()` so isolated Flask applications can be built for tests.
- Route registration was moved from decorators in `app.py` to a declarative
  blueprint in `web_routes.py`.
- Repeated factory calls register exactly one copy of each route.
- The refactor extracted configuration, job state/service, metadata storage,
  download helpers, request security/validation, history mutation, runtime
  cleanup, atomic state storage, waveform aggregation, track identity,
  fingerprint-manifest logic, and worker lifecycle into focused modules.
- `app.py` remains the orchestration layer and still owns request handlers and
  domain workflows; this was a modularization step, not a complete rewrite.

### Production WSGI serving

- Replaced the Flask development server as the container command with
  Gunicorn.
- Gunicorn is configured for one process, the `gthread` worker class, and eight
  HTTP threads.
- One process is deliberate because jobs, locks, queues, and the current mix
  pointer remain in memory.
- Added a lightweight `/api/health` liveness endpoint with no storage or
  manifest work.
- Added `/api/ready`, which reports worker liveness, writable storage, Panako
  availability, queue depths, and HTTP 503 when not ready.
- Added matching image and Compose health checks.
- Windows setup now waits on `/api/health` instead of a stateful library route.

## Frontend reliability and accessibility

### Job polling

- Replaced overlapping `setInterval(async ...)` polling with one serialized
  request at a time.
- Added `AbortController` cancellation when a new job replaces an existing
  poll.
- Added bounded retries with exponential backoff for transient connection
  failures.
- Added explicit handling for expired/unknown jobs and HTTP errors.
- Completion and error cleanup now clear timers and UI state in one place.

### Keyboard and assistive-technology support

- Added a semantic tablist, tabs, tabpanels, `aria-controls`,
  `aria-selected`, roving `tabindex`, and Arrow/Home/End navigation.
- Inactive panels use `hidden` as well as visual state.
- Drop zones and expandable folder rows are keyboard operable with Enter and
  Space.
- Folder rows expose `aria-expanded`.
- Player and mix controls have accessible names.
- The waveform scrubber is exposed as a slider with live value attributes and
  supports Arrow, Home, and End keys.
- Status areas use `role=status`/`aria-live`; the download console uses
  `role=log`.
- Dynamic play, download, edit, delete, refresh, volume, and stop controls have
  contextual labels.
- History scan names are real buttons rather than clickable spans.
- Focus-visible outlines were added for interactive roles.

### Reduced motion and rendering behavior

- Added a `prefers-reduced-motion` CSS mode.
- JavaScript observes reduced-motion changes at runtime.
- The animated 3D Web Audio visualizer and requestAnimationFrame waveform
  loops are disabled when reduced motion is requested.
- Ordinary media time updates still maintain player state and accessibility
  values without animation.

### Expressive live player terrain

- Evolved the music player's live 3D spectrum from independent filled ridges
  into a connected, translucent spectral terrain with longitudinal mesh lines.
- Retained and strengthened perceptual logarithmic placement from 35 Hz to 16
  kHz, keeping bass on the left and treble on the right while allocating useful
  width to musically important low frequencies.
- Combined per-band RMS energy with local maxima so the surface has stable body
  without discarding narrow transients.
- Added independent temporal and spatial behavior for three frequency ranges:
  broad, slow-decaying bass; moderately articulated mids; and fast,
  needle-shaped highs.
- Added cyan/blue underlighting driven by sustained sub-180 Hz energy and bass
  attack flux.
- Added positive spectral-flux detection with short-lived per-frequency
  envelopes. The strongest local transient peaks produce white/pink luminous
  crests above the live ridge.
- Increased terrain depth/height and history lifetime while capping history at
  46 cross-sections.
- Refactored the animation loop to compute the analyser spectrum once per
  frame, capture copies into history, and reuse one color gradient across the
  frame to control rendering cost.

### Additional UI corrections

- The tracklist is height-bounded and scrollable so it does not collide with
  the fixed player/visualizer region.
- Recalled scans no longer render disabled slice-play controls for unavailable
  audio.
- Key chips, stale-index warnings, rebuild notices, upload conflicts, and
  post-download index-queue errors are surfaced in the UI.
- Thumbnail waveform loading remains queued to avoid a burst of synchronous
  work.

## Container and supply-chain engineering

### Multi-stage application image

- Replaced the single-stage image with a native `panako-builder`, a
  `runtime-base`, a test stage, and a final runtime stage.
- Git, GCC/G++, Make, libc headers, and the full JDK remain in the builder and
  are absent from production.
- Production contains only the JRE and required media/runtime packages.
- LMDB is copied with symlink dereferencing so `/lib/liblmdb.so` is a real,
  loadable file rather than a dangling builder-stage link.
- The final runtime image was measured at 371,189,077 bytes, down from
  863,958,025 bytes at the start of the P3 implementation (approximately 57%
  smaller).

### Reproducible inputs

- Pinned the Debian base by digest.
- Pinned OpenLDAP/LMDB, JGaborator, and Panako to full Git commit IDs.
- Added Gradle wrapper distribution checksum verification.
- Replaced unconstrained `pip install` inputs with an exact, hash-checked
  `requirements.lock`.
- Added a separate hash-checked Ruff development lock.
- Pinned the controller base image and Firefox image by digest.
- Added `.dockerignore` coverage for credentials, logs, Python caches, and
  irrelevant setup artifacts while retaining the fixtures needed by the test
  stage.

### Runtime identity and health

- The application image still starts as root only long enough to align managed
  volume ownership, then uses `gosu` to run the application, yt-dlp, ffmpeg,
  and Panako as the unprivileged `scripper` user.
- The runtime home is explicitly `/home/scripper`, matching the persisted
  Panako volume.
- The controller and application images each have independent health checks.

## Quality engineering and documentation

### Test suite

- Added 63 automated tests covering:
  - Manifest signatures, migration, invalid-state handling, and atomic save.
  - Atomic JSON state and corruption quarantine.
  - Root-contained runtime cleanup and cache bounds.
  - Exclusive uploads and collision-safe downloads.
  - Browser-controller ownership and lifecycle rules.
  - Compose socket isolation and immutable build inputs.
  - Job retention, bounded queues, and worker shutdown.
  - Subprocess deadlines and process termination.
  - Streaming waveform aggregation and limits.
  - Duplicate-filename path resolution and metadata cache keys.
  - Browser-origin enforcement and request schemas.
  - History mutation validation.
  - Frontend DOM safety, polling, popup handling, and accessibility contracts.
  - Flask integration behavior for queue pressure, health, corrupt history,
    route registration, and manifest race checks.
  - Native runtime smoke tests that load LMDB and run ffmpeg, ffprobe, and
    Panako against generated audio.
- Split discoverable test entry points into storage/jobs, security/validation,
  media, frontend/build, integration, and native-audio categories while
  retaining shared regression cases in one module.

### Static quality and repeatable release gate

- Added Ruff configuration for Python 3.9, import sorting, syntax/error rules,
  and a 100-column format.
- Applied Ruff formatting/import normalization to the Python codebase.
- Added equivalent PowerShell and POSIX release-gate scripts.
- The gate validates Compose, runs Dockerfile build checks, builds the isolated
  test image, executes lint/format/tests inside that image, and builds both
  deployable images.
- The isolated test stage includes the source/configuration fixtures needed to
  test the actual copied artifact rather than relying on a bind-mounted
  workspace.

### Documentation and setup

- Expanded the README to document non-overwrite behavior, on-demand browser
  lifecycle, Docker trust boundaries, manifest rebuild semantics, queue and
  deadline behavior, metadata caches, reproducible builds, atomic state,
  accessibility, health endpoints, and the quality gate.
- Converted non-ASCII dash characters in the Windows setup script to ASCII to
  avoid legacy PowerShell parsing/encoding failures.
- Changed the Windows startup probe to the dedicated liveness endpoint.
- Extended `.gitignore` for Python bytecode and test caches.
- Reformatted `dump_cookies.py` to the project quality standard without
  changing its behavior.

## File-level inventory

### Existing files modified

- `.gitignore` — Python/test cache exclusions.
- `Dockerfile` — reproducible multi-stage builder/runtime/test image and
  Gunicorn command.
- `README.md` — updated user, security, operations, and development docs.
- `app.py` — orchestration, validation, consistency, queue, metadata,
  lifecycle, and endpoint changes.
- `docker-compose.yml` — controller boundary, on-demand browser, health, and
  immutable image configuration.
- `dump_cookies.py` — formatting/import cleanup.
- `setup.ps1` — ASCII compatibility and liveness probing.
- `static/index.html` — reliability, DOM safety, key display, browser
  lifecycle, accessibility, and reduced-motion changes.

### New application modules

- `app_config.py`
- `application_state.py`
- `download_service.py`
- `fingerprint_manifest.py`
- `history_updates.py`
- `job_service.py`
- `job_state.py`
- `metadata_store.py`
- `process_utils.py`
- `request_security.py`
- `request_validation.py`
- `runtime_hygiene.py`
- `safe_download.py`
- `safe_upload.py`
- `state_storage.py`
- `track_identity.py`
- `waveform.py`
- `web_routes.py`
- `worker_runtime.py`

### New deployment and quality assets

- `.dockerignore`
- `browser-controller.Dockerfile`
- `browser_controller.py`
- `gunicorn.conf.py`
- `pyproject.toml`
- `requirements.lock`
- `requirements-dev.lock`
- `scripts/ci.ps1`
- `scripts/ci.sh`

### New tests

- `tests/regression_cases.py`
- `tests/test_app_integration.py`
- `tests/test_audio_smoke.py`
- `tests/test_frontend_and_build.py`
- `tests/test_media_processing.py`
- `tests/test_security_and_validation.py`
- `tests/test_storage_and_jobs.py`

## Verification state at the end of the session

- Dockerfile build checks: passed for both images with no warnings.
- Ruff lint: passed.
- Ruff format check: passed.
- Unit/integration/native smoke tests: 62 passed.
- Compose configuration validation: passed.
- Application and controller image builds: passed.
- Production runtime audit: Git and GCC absent; Gunicorn 23.0.0 and LMDB
  loadable; Panako executable present.
- Live default-container smoke test: Gunicorn started with `gthread`, liveness
  returned HTTP 200, readiness returned HTTP 200, and all three application
  workers reported healthy.
- Frontend JavaScript parse check: passed.
