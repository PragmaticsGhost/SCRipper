import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import browser_controller
import fingerprint_manifest
from history_updates import apply_manual_title
from job_service import JobService
from job_state import prune_jobs
from metadata_store import MISSING, PersistentJsonCache
from process_utils import (
    ProcessDeadlineExceeded,
    audio_process_timeout,
    iter_chunks_with_deadline,
    iter_lines_with_deadline,
)
from request_security import is_cross_origin_browser_request
from request_validation import (
    RequestValidationError,
    validate_download_request,
    validate_urls,
)
from runtime_hygiene import cleanup_runtime_artifacts, prune_files
from safe_download import promote_unique, safe_mp3_name
from safe_upload import copy_exclusive
from state_storage import atomic_write_json, load_json
from track_identity import (
    analysis_cache_key,
    build_resolution_index,
    resolve_library_path,
    resolve_with_index,
)
from waveform import WaveformLimitError, aggregate_pcm, validate_waveform_request
from worker_runtime import WorkerRuntime

ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def test_recorded_file_becomes_stale_when_bytes_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp, "track.mp3")
            audio.write_bytes(b"original")
            manifest = fingerprint_manifest.empty_manifest()
            fingerprint_manifest.record(manifest, str(audio))
            self.assertEqual({str(audio)}, fingerprint_manifest.current_files(manifest))
            self.assertEqual(set(), fingerprint_manifest.stale_files(manifest))

            audio.write_bytes(b"replacement-data")
            self.assertEqual({str(audio)}, fingerprint_manifest.stale_files(manifest))
            self.assertEqual(set(), fingerprint_manifest.current_files(manifest))

    def test_legacy_path_only_manifest_requires_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp, "track.mp3")
            audio.write_bytes(b"audio")
            legacy = {"files": {str(audio): True}}
            self.assertEqual({str(audio)}, fingerprint_manifest.stale_files(legacy))

    def test_manifest_save_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "manifest.json")
            manifest = fingerprint_manifest.empty_manifest()
            fingerprint_manifest.save(str(path), manifest)
            self.assertEqual(manifest, fingerprint_manifest.load(str(path)))
            self.assertFalse(Path(str(path) + ".tmp").exists())

    def test_corrupt_manifest_requires_rebuild_instead_of_looking_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "manifest.json")
            path.write_text("{broken", encoding="utf-8")
            manifest = fingerprint_manifest.load(str(path))
            self.assertTrue(manifest["invalid"])
            self.assertEqual(
                {fingerprint_manifest.INVALID_MANIFEST_MARKER},
                fingerprint_manifest.stale_files(manifest),
            )


class StateStorageTests(unittest.TestCase):
    def test_atomic_json_round_trip_leaves_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            atomic_write_json(str(path), {"ok": True})
            self.assertEqual({"ok": True}, load_json(str(path), dict))
            self.assertFalse(any(p.suffix == ".tmp" for p in Path(tmp).iterdir()))

    def test_invalid_json_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "state.json")
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual({}, load_json(str(path), dict))
            self.assertFalse(path.exists())
            self.assertEqual(1, len(list(Path(tmp).glob("state.json.invalid-*"))))

    def test_runtime_cleanup_only_removes_owned_old_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            music = root.joinpath("music")
            uploads = root.joinpath("uploads")
            waveforms = root.joinpath("waveforms")
            music.mkdir()
            uploads.mkdir()
            waveforms.mkdir()
            scratch = music.joinpath("Set", ".scripper-download-old")
            scratch.mkdir(parents=True)
            scratch.joinpath("partial").write_bytes(b"x")
            keep = music.joinpath("Set", "real-track.mp3")
            keep.write_bytes(b"music")
            old_upload = uploads.joinpath("old.zip")
            old_upload.write_bytes(b"zip")
            old_waveform = waveforms.joinpath("old.json")
            old_waveform.write_text("[]", encoding="utf-8")
            old = 1000
            for path in (scratch, old_upload, old_waveform):
                os.utime(path, (old, old))
            removed = cleanup_runtime_artifacts(
                str(music),
                str(uploads),
                str(waveforms),
                now=old + 10_000_000,
            )
            self.assertEqual({"uploads": 1, "waveforms": 1, "downloads": 1}, removed)
            self.assertEqual(b"music", keep.read_bytes())

    def test_cache_pruning_keeps_newest_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(4):
                path = Path(tmp, f"{index}.json")
                path.write_text("[]", encoding="utf-8")
                os.utime(path, (index + 1, index + 1))
            self.assertEqual(2, prune_files(tmp, suffix=".json", max_entries=2))
            self.assertEqual({"2.json", "3.json"}, {p.name for p in Path(tmp).iterdir()})

    def test_metadata_cache_distinguishes_cached_none_from_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = PersistentJsonCache(str(Path(tmp, "metadata.json")))
            self.assertIs(MISSING, cache.get("track"))
            cache.set("track", None)
            self.assertIsNone(cache.get("track"))


class UploadTests(unittest.TestCase):
    def test_new_file_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "track.mp3")
            copied = copy_exclusive(io.BytesIO(b"audio"), str(target))
            self.assertTrue(copied)
            self.assertEqual(b"audio", target.read_bytes())

    def test_existing_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "track.mp3")
            target.write_bytes(b"keep-me")
            copied = copy_exclusive(io.BytesIO(b"replacement"), str(target))
            self.assertFalse(copied)
            self.assertEqual(b"keep-me", target.read_bytes())

    def test_failed_copy_removes_only_its_partial_file(self):
        class BrokenStream(io.BytesIO):
            def read(self, *_args, **_kwargs):
                raise OSError("read failed")

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "track.mp3")
            with self.assertRaises(OSError):
                copy_exclusive(BrokenStream(b"x"), str(target))
            self.assertFalse(target.exists())


class DownloadPromotionTests(unittest.TestCase):
    def test_existing_track_is_preserved_and_download_gets_numbered_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp, "Track.mp3")
            completed = Path(tmp, ".completed.mp3")
            existing.write_bytes(b"original")
            completed.write_bytes(b"download")
            promoted = promote_unique(str(completed), str(existing))
            self.assertEqual(b"original", existing.read_bytes())
            self.assertEqual(str(Path(tmp, "Track (2).mp3")), promoted)
            self.assertEqual(b"download", Path(promoted).read_bytes())
            self.assertFalse(completed.exists())

    def test_multiple_collisions_increment_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Track.mp3").write_bytes(b"one")
            Path(tmp, "Track (2).mp3").write_bytes(b"two")
            completed = Path(tmp, ".completed.mp3")
            completed.write_bytes(b"three")
            promoted = promote_unique(str(completed), str(Path(tmp, "Track.mp3")))
            self.assertEqual(str(Path(tmp, "Track (3).mp3")), promoted)

    def test_download_name_is_safe_for_windows_library_mounts(self):
        self.assertEqual("A_B_C.mp3", safe_mp3_name("A:B?C."))
        self.assertEqual("_CON.mp3", safe_mp3_name("CON"))


class BrowserControllerTests(unittest.TestCase):
    def test_stop_failure_is_reported_when_container_remains_running(self):
        with (
            mock.patch.object(browser_controller, "browser_status", side_effect=[True, True]),
            mock.patch.object(browser_controller, "_docker"),
        ):
            with self.assertRaises(browser_controller.ControllerError):
                browser_controller.stop_browser()

    def test_controller_operations_use_fixed_container_name(self):
        completed = mock.Mock(stdout="true|true\n", returncode=0)
        with mock.patch.object(
            browser_controller,
            "_docker",
            side_effect=[None, None, completed, completed],
        ) as docker:
            self.assertTrue(browser_controller.start_browser())
        run_args = next(call.args for call in docker.call_args_list if call.args[0] == "run")
        self.assertIn(browser_controller.BROWSER_NAME, run_args)
        self.assertNotIn("/var/run/docker.sock", " ".join(run_args))

    def test_foreign_container_is_never_removed_or_stopped(self):
        foreign = mock.Mock(stdout="<no value>|false\n", returncode=0)
        with mock.patch.object(
            browser_controller,
            "_docker",
            return_value=foreign,
        ) as docker:
            with self.assertRaises(browser_controller.ControllerError):
                browser_controller.start_browser()
        commands = [call.args[0] for call in docker.call_args_list]
        self.assertNotIn("rm", commands)
        self.assertNotIn("stop", commands)

    def test_stopped_managed_container_can_be_replaced(self):
        stopped = mock.Mock(stdout="true|false\n", returncode=0)
        running = mock.Mock(stdout="true|true\n", returncode=0)
        with mock.patch.object(
            browser_controller,
            "_docker",
            side_effect=[stopped, mock.Mock(), mock.Mock(), running],
        ) as docker:
            self.assertTrue(browser_controller.start_browser())
        self.assertEqual("rm", docker.call_args_list[1].args[0])


class WindowsSetupTests(unittest.TestCase):
    def test_legacy_powershell_script_is_ascii_safe(self):
        data = ROOT.joinpath("setup.ps1").read_bytes()
        self.assertTrue(data.isascii())


class ComposeIsolationTests(unittest.TestCase):
    def test_main_app_has_no_docker_socket(self):
        compose = ROOT.joinpath("docker-compose.yml").read_text(encoding="utf-8")
        main_service, controller_service = compose.split("\n  browser-controller:", maxsplit=1)
        self.assertNotIn("/var/run/docker.sock", main_service)
        self.assertIn("/var/run/docker.sock", controller_service)
        self.assertNotIn("LOGIN_BROWSER_PASSWORD", main_service)


class BuildPinTests(unittest.TestCase):
    def test_base_images_and_source_revisions_are_immutable(self):
        dockerfile = ROOT.joinpath("Dockerfile").read_text(encoding="utf-8")
        controller = ROOT.joinpath("browser-controller.Dockerfile").read_text(encoding="utf-8")
        compose = ROOT.joinpath("docker-compose.yml").read_text(encoding="utf-8")

        self.assertRegex(dockerfile, r"ARG DEBIAN_IMAGE=debian:bookworm@sha256:[0-9a-f]{64}")
        self.assertEqual(2, dockerfile.count("FROM ${DEBIAN_IMAGE}"))
        self.assertRegex(controller, r"FROM docker:[^\s]+@sha256:[0-9a-f]{64}")
        self.assertRegex(compose, r"LOGIN_BROWSER_IMAGE=jlesage/firefox@sha256:[0-9a-f]{64}")
        for name in ("OPENLDAP_COMMIT", "JGABORATOR_COMMIT", "PANAKO_COMMIT"):
            self.assertRegex(dockerfile, rf"ARG {name}=[0-9a-f]{{40}}")
        self.assertRegex(dockerfile, r"ARG GRADLE_7_2_BIN_SHA256=[0-9a-f]{64}")
        self.assertIn("distributionSha256Sum", dockerfile)
        self.assertNotIn("git clone --depth 1", dockerfile)

    def test_runtime_stage_excludes_the_native_build_toolchain(self):
        dockerfile = ROOT.joinpath("Dockerfile").read_text(encoding="utf-8")
        runtime = dockerfile.split("FROM ${DEBIAN_IMAGE} AS runtime-base", 1)[1].split(
            "FROM runtime-base AS test", 1
        )[0]
        packages = runtime.split("RUN apt-get update", 1)[1].split("COPY --from", 1)[0]

        self.assertIn("default-jre-headless", packages)
        for build_dependency in (r"\bgit\b", r"\bgcc\b", r"\bg\+\+\b", "default-jdk"):
            self.assertNotRegex(packages, build_dependency)
        self.assertIn("cp -L /openldap/libraries/liblmdb/liblmdb.so", dockerfile)

    def test_runtime_uses_gunicorn_and_exposes_health_check(self):
        dockerfile = ROOT.joinpath("Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            'CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]',
            dockerfile,
        )
        self.assertIn("http://localhost:8080/api/health", dockerfile)

    def test_repeatable_quality_gate_scripts_cover_images_and_tests(self):
        for script_name in ("ci.ps1", "ci.sh"):
            script = ROOT.joinpath("scripts", script_name).read_text(encoding="utf-8")
            self.assertIn("docker compose config", script)
            self.assertIn("--target test", script)
            # Stale extractors break downloads silently; the gate must report them.
            self.assertIn("scripts/check_updates.py", script)
            self.assertIn("docker compose build", script)

    def test_python_runtime_dependencies_are_exactly_pinned(self):
        requirements = ROOT.joinpath("requirements.lock").read_text(encoding="utf-8")
        pins = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertTrue(pins)
        self.assertTrue(
            all(
                re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s=]+ --hash=sha256:[0-9a-f]{64}", pin)
                for pin in pins
            )
        )


class JobRetentionTests(unittest.TestCase):
    def test_pruning_keeps_active_and_newest_finished_jobs(self):
        jobs = {
            "active": {"status": "running", "updated_at": 1},
            "old": {"status": "done", "finished_at": 10},
            "new": {"status": "error", "finished_at": 20},
        }
        prune_jobs(jobs, now=25, ttl_seconds=100, max_finished=1)
        self.assertEqual({"active", "new"}, set(jobs))

    def test_pruning_expires_terminal_jobs_only(self):
        jobs = {
            "active": {"status": "queued", "updated_at": 1},
            "expired": {"status": "done", "finished_at": 1},
        }
        prune_jobs(jobs, now=100, ttl_seconds=10, max_finished=100)
        self.assertEqual({"active"}, set(jobs))


class DependencyFreshnessTests(unittest.TestCase):
    """The update checker exists because a pinned yt-dlp silently went ten
    months stale on an old Python and broke YouTube downloads."""

    def _module(self):
        import importlib.util

        path = ROOT.joinpath("scripts", "check_updates.py")
        spec = importlib.util.spec_from_file_location("check_updates", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_lock_parsing_ignores_hashes_and_comments(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp, "requirements.lock")
            lock.write_text(
                "\n".join(
                    [
                        "# comment",
                        "yt-dlp==2026.8.19 --hash=sha256:" + "a" * 64,
                        "",
                        "Flask==3.1.3 --hash=sha256:" + "b" * 64,
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                {"yt-dlp": "2026.8.19", "flask": "3.1.3"},
                module.parse_lock(str(lock)),
            )

    def test_version_ordering_detects_newer_releases(self):
        module = self._module()
        self.assertLess(module.parse_version("2025.10.14"), module.parse_version("2026.8.19"))
        self.assertLess(module.parse_version("3.4.9"), module.parse_version("3.5.1"))

    def test_python_requirement_gating(self):
        module = self._module()
        # The exact trap: a release needing a newer interpreter than the image.
        unreachable = ">=%d.%d" % (sys.version_info[0], sys.version_info[1] + 1)
        self.assertFalse(module.python_ok(unreachable))
        self.assertTrue(module.python_ok(">=3.8"))
        self.assertTrue(module.python_ok(None))


class JobCancellationTests(unittest.TestCase):
    def _service(self):
        return JobService(
            mock.Mock(),
            retention_seconds=3600,
            max_finished=100,
            panako_capacity=8,
            download_capacity=4,
            metadata_capacity=100,
        )

    def test_running_job_is_signalled_and_terminal_state_clears_state(self):
        service = self._service()
        jid = service.new("index", "folder")
        service.update(jid, status="running")
        self.assertFalse(service.is_cancelled(jid))
        self.assertTrue(service.request_cancel(jid))
        self.assertTrue(service.is_cancelled(jid))
        # A running job is not force-finalized; the worker observes the flag.
        self.assertEqual("running", service.get(jid)["status"])
        service.update(jid, status="cancelled")
        # Cancellation bookkeeping is released once the job is terminal.
        self.assertNotIn(jid, service.cancels)
        self.assertNotIn(jid, service.processes)

    def test_queued_job_cancels_immediately(self):
        service = self._service()
        jid = service.new("download", "urls")
        self.assertTrue(service.request_cancel(jid))
        self.assertEqual("cancelled", service.get(jid)["status"])

    def test_cancel_of_finished_job_is_rejected(self):
        service = self._service()
        jid = service.new("identify", "mix")
        service.update(jid, status="done")
        self.assertFalse(service.request_cancel(jid))

    def test_registered_process_is_terminated_on_cancel(self):
        service = self._service()
        jid = service.new("identify", "mix")
        service.update(jid, status="running")
        proc = mock.Mock()
        proc.poll.return_value = None
        service.register_process(jid, proc)
        with mock.patch("job_service.terminate_process_tree") as killer:
            service.request_cancel(jid)
            killer.assert_called_once_with(proc)

    def test_process_registered_after_cancel_is_killed_at_once(self):
        service = self._service()
        jid = service.new("index", "folder")
        service.update(jid, status="running")
        service.request_cancel(jid)
        proc = mock.Mock()
        with mock.patch("job_service.terminate_process_tree") as killer:
            service.register_process(jid, proc)
            killer.assert_called_once_with(proc)


class WorkerRuntimeTests(unittest.TestCase):
    def test_worker_starts_once_and_stops_cooperatively(self):
        runtime = WorkerRuntime()
        entered = threading.Event()

        def worker(stop_event):
            entered.set()
            stop_event.wait(5)

        first = runtime.start("test", worker)
        second = runtime.start("test", worker)
        self.assertIs(first, second)
        self.assertTrue(entered.wait(1))
        self.assertEqual({"test": True}, runtime.health())
        runtime.stop(timeout=1)
        self.assertEqual({"test": False}, runtime.health())


class ProcessDeadlineTests(unittest.TestCase):
    def test_audio_timeout_is_bounded(self):
        self.assertEqual(600, audio_process_timeout(None))
        self.assertEqual(600, audio_process_timeout(60))
        self.assertEqual(7200, audio_process_timeout(100_000))

    def test_streaming_process_is_terminated_at_deadline(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=(os.name != "nt"),
        )
        try:
            with self.assertRaises(ProcessDeadlineExceeded):
                list(iter_lines_with_deadline(proc, 0.2))
            self.assertIsNotNone(proc.poll())
        finally:
            proc.stdout.close()

    def test_binary_streaming_process_is_terminated_at_deadline(self):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys,time; sys.stdout.buffer.write(b'x'*10); "
                "sys.stdout.flush(); time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
        )
        try:
            with self.assertRaises(ProcessDeadlineExceeded):
                list(iter_chunks_with_deadline(proc, 0.2, chunk_size=4))
            self.assertIsNotNone(proc.poll())
        finally:
            proc.stdout.close()


class WaveformTests(unittest.TestCase):
    def test_streaming_aggregation_has_fixed_bucket_count(self):
        # Four little-endian signed 16-bit samples split across odd chunks.
        pcm = b"\x00\x00\xe8\x03\x18\xfc\xd0\x07"
        peaks = aggregate_pcm([pcm[:3], pcm[3:7], pcm[7:]], 2, 4)
        self.assertEqual(2, len(peaks))
        self.assertGreater(peaks[1], peaks[0])

    def test_waveform_duration_and_dimensions_are_bounded(self):
        self.assertEqual(8000, validate_waveform_request(1, 700, 8000, 3600))
        with self.assertRaises(WaveformLimitError):
            validate_waveform_request(3601, 700, 8000, 3600)


class TrackIdentityTests(unittest.TestCase):
    def test_full_path_resolves_duplicate_basenames_without_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "a", "track.mp3")
            second = Path(tmp, "b", "track.mp3")
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            paths = [str(first), str(second)]

            self.assertEqual(os.path.realpath(second), resolve_library_path(str(second), paths))
            self.assertIsNone(resolve_library_path("track.mp3", paths))

    def test_prebuilt_index_matches_direct_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp, "a", "track.mp3")
            second = Path(tmp, "b", "track.mp3")
            unique = Path(tmp, "a", "solo.mp3")
            first.parent.mkdir()
            second.parent.mkdir()
            for target in (first, second, unique):
                target.write_bytes(b"audio")
            paths = [str(first), str(second), str(unique)]
            index = build_resolution_index(paths)

            # Full paths resolve exactly; a unique basename still resolves;
            # an ambiguous basename stays unresolved rather than guessing.
            self.assertEqual(os.path.realpath(second), resolve_with_index(str(second), index))
            self.assertEqual(os.path.realpath(unique), resolve_with_index("solo.mp3", index))
            self.assertIsNone(resolve_with_index("track.mp3", index))
            self.assertIsNone(resolve_with_index("", index))

    def test_index_is_built_once_for_many_lookups(self):
        """Resolution must not re-walk the library per reference: that made
        one identification spend minutes in realpath() calls."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "track.mp3")
            target.write_bytes(b"audio")
            paths = [str(target)]
            index = build_resolution_index(paths)
            with mock.patch("track_identity.os.path.realpath", wraps=os.path.realpath) as spy:
                for _ in range(25):
                    resolve_with_index(str(target), index)
                # One realpath per lookup (the reference itself), never one
                # per library file.
                self.assertEqual(25, spy.call_count)

    def test_analysis_key_changes_with_path_and_file_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root.joinpath("a.mp3")
            second = root.joinpath("b.mp3")
            first.write_bytes(b"audio")
            second.write_bytes(b"audio")
            first_key = analysis_cache_key(str(first), str(root))
            self.assertNotEqual(first_key, analysis_cache_key(str(second), str(root)))
            first.write_bytes(b"replacement")
            self.assertNotEqual(first_key, analysis_cache_key(str(first), str(root)))


class RequestSecurityTests(unittest.TestCase):
    def test_same_origin_browser_request_is_allowed(self):
        self.assertFalse(
            is_cross_origin_browser_request(
                "http",
                "localhost:8080",
                origin="http://localhost:8080",
                sec_fetch_site="same-origin",
                referer="http://localhost:8080/",
            )
        )

    def test_cross_site_and_same_site_requests_are_rejected(self):
        self.assertTrue(
            is_cross_origin_browser_request("http", "localhost:8080", sec_fetch_site="cross-site")
        )
        self.assertTrue(
            is_cross_origin_browser_request("http", "localhost:8080", sec_fetch_site="same-site")
        )

    def test_mismatched_origin_or_referer_is_rejected(self):
        self.assertTrue(
            is_cross_origin_browser_request("http", "localhost:8080", origin="https://example.com")
        )
        self.assertTrue(
            is_cross_origin_browser_request(
                "http", "localhost:8080", referer="http://127.0.0.1:8080/"
            )
        )

    def test_local_cli_request_without_browser_headers_is_allowed(self):
        self.assertFalse(is_cross_origin_browser_request("http", "localhost:8080"))


class RequestValidationTests(unittest.TestCase):
    def test_download_urls_must_be_a_bounded_list(self):
        with self.assertRaises(RequestValidationError):
            validate_urls("https://example.com/track")
        with self.assertRaises(RequestValidationError):
            validate_urls(["https://example.com"] * 3, maximum=2)

    def test_download_request_deduplicates_and_validates_types(self):
        urls, folder, index_after = validate_download_request(
            {
                "urls": ["https://example.com/a", "https://example.com/a"],
                "folder": "Set",
                "index_after": True,
            }
        )
        self.assertEqual(["https://example.com/a"], urls)
        self.assertEqual("Set", folder)
        self.assertTrue(index_after)
        with self.assertRaises(RequestValidationError):
            validate_download_request(
                {
                    "urls": ["file:///etc/passwd"],
                    "folder": "Set",
                    "index_after": False,
                }
            )


class HistoryUpdateTests(unittest.TestCase):
    def test_start_zero_can_be_renamed(self):
        tracks = [{"start": 0, "unidentified": True}]
        apply_manual_title(tracks, 0, " Intro ")
        self.assertEqual("Intro", tracks[0]["manual_title"])

    def test_invalid_start_and_title_are_rejected(self):
        tracks = [{"start": 0, "unidentified": True}]
        with self.assertRaises(ValueError):
            apply_manual_title(tracks, False, "Intro")
        with self.assertRaises(ValueError):
            apply_manual_title(tracks, 0, {"bad": "type"})

    def test_missing_history_entry_is_reported(self):
        with self.assertRaises(LookupError):
            apply_manual_title([], 0, "Intro")


class FrontendSafetyTests(unittest.TestCase):
    def test_dynamic_select_options_use_dom_properties(self):
        html = ROOT.joinpath("static", "index.html").read_text(encoding="utf-8")
        self.assertNotIn('<option value="${esc(', html)
        self.assertIn("option.value = value", html)
        self.assertIn("option.textContent = label", html)

    def test_downloads_and_panako_use_separate_queues(self):
        source = ROOT.joinpath("app.py").read_text(encoding="utf-8")
        jobs = ROOT.joinpath("job_service.py").read_text(encoding="utf-8")
        self.assertIn("self.download_queue = queue.Queue(maxsize=", jobs)
        self.assertIn("self.panako_queue = queue.Queue(maxsize=", jobs)
        self.assertRegex(source, r"_download_job_queue,\s+do_download,")
        self.assertRegex(source, r"_panako_job_queue,\s+do_index,")

    def test_job_polling_is_serial_and_cancellable(self):
        html = ROOT.joinpath("static", "index.html").read_text(encoding="utf-8")
        self.assertNotIn("setInterval(async", html)
        self.assertIn("new AbortController()", html)
        self.assertIn("setTimeout(poll", html)

    def test_login_tab_is_reserved_before_async_startup(self):
        html = ROOT.joinpath("static", "index.html").read_text(encoding="utf-8")
        open_at = html.index('window.open("about:blank"')
        start_at = html.index('fetch("/api/login-browser/start"')
        navigate_at = html.index("browserTab.location.href = url")
        self.assertLess(open_at, start_at)
        self.assertLess(start_at, navigate_at)

    def test_primary_interactions_are_keyboard_and_screen_reader_accessible(self):
        html = ROOT.joinpath("static", "index.html").read_text(encoding="utf-8")
        self.assertIn('role="tablist"', html)
        self.assertIn('role="tabpanel"', html)
        self.assertIn('id="drop" role="button" tabindex="0"', html)
        self.assertIn('role="status" aria-live="polite"', html)
        self.assertIn('role="slider" tabindex="0"', html)
        self.assertIn('event.key === "ArrowRight"', html)
        self.assertIn("prefers-reduced-motion: reduce", html)


if __name__ == "__main__":
    unittest.main()
