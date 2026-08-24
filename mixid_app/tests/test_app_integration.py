import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REQUIRED = ("flask", "yt_dlp", "mutagen")
if any(importlib.util.find_spec(name) is None for name in REQUIRED):
    raise unittest.SkipTest("container-only Flask dependencies are unavailable")


_ROOT = tempfile.TemporaryDirectory()
_BASE = Path(_ROOT.name)
for name in ("music", "uploads", "db", "logs", "browser-profile", "cookies"):
    _BASE.joinpath(name).mkdir()
os.environ["SCRIPPER_START_WORKERS"] = "0"
os.environ["SCRIPPER_MUSIC_ROOT"] = str(_BASE.joinpath("music"))
os.environ["SCRIPPER_UPLOAD_DIR"] = str(_BASE.joinpath("uploads"))
os.environ["SCRIPPER_DB_DIR"] = str(_BASE.joinpath("db"))
os.environ["SCRIPPER_LOG_DIR"] = str(_BASE.joinpath("logs"))
os.environ["SCRIPPER_BROWSER_PROFILE_DIR"] = str(_BASE.joinpath("browser-profile"))
os.environ["SCRIPPER_COOKIE_FILE"] = str(_BASE.joinpath("cookies", "cookies.txt"))

import app as application  # noqa: E402


def tearDownModule():
    _ROOT.cleanup()


class ApiValidationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = application.create_app({"TESTING": True}).test_client()

    def test_download_rejects_string_urls(self):
        response = self.client.post(
            "/api/scripper/download",
            json={
                "urls": "https://example.com/track",
                "folder": "Set",
                "index_after": False,
            },
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("list", response.get_json()["error"])

    def test_download_rejects_non_http_urls(self):
        response = self.client.post(
            "/api/scripper/download",
            json={
                "urls": ["file:///etc/passwd"],
                "folder": "Set",
                "index_after": False,
            },
        )
        self.assertEqual(400, response.status_code)

    def test_download_returns_429_when_queue_is_full(self):
        with mock.patch.object(application, "submit_job", return_value=None):
            response = self.client.post(
                "/api/scripper/download",
                json={
                    "urls": ["https://example.com/track"],
                    "folder": "Set",
                    "index_after": False,
                },
            )
        self.assertEqual(429, response.status_code)

    def test_index_requires_an_object_schema(self):
        response = self.client.post("/api/index", json=[])
        self.assertEqual(400, response.status_code)

    def test_invalid_manifest_blocks_identification(self):
        with mock.patch.object(
            application,
            "manifest_stale_paths",
            return_value={application.INVALID_MANIFEST_MARKER},
        ):
            response = self.client.post("/api/identify")
        self.assertEqual(409, response.status_code)
        self.assertIn("manifest", response.get_json()["error"].lower())

    def test_health_endpoint_is_lightweight(self):
        with mock.patch.object(application, "manifest_files") as manifest:
            response = self.client.get("/api/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual("ok", response.get_json()["status"])
        manifest.assert_not_called()

    def test_corrupt_history_is_quarantined(self):
        history = Path(application.HISTORY_DIR, "deadbeef.json")
        history.write_text("{broken", encoding="utf-8")
        response = self.client.get("/api/history/deadbeef")
        self.assertEqual(409, response.status_code)
        self.assertFalse(history.exists())
        self.assertTrue(list(Path(application.HISTORY_DIR).glob("deadbeef.json.invalid-*")))

    def test_route_contract_is_registered_once(self):
        flask_app = application.create_app({"TESTING": True})
        rules = [rule.rule for rule in flask_app.url_map.iter_rules()]
        self.assertEqual(1, rules.count("/api/scripper/download"))
        self.assertIn("/api/ready", rules)
        self.assertIs(application.APP_STATE, flask_app.extensions["scripper"])


class IdentificationIntegrationTests(unittest.TestCase):
    def test_worker_rechecks_manifest_before_audio_work(self):
        mix = _BASE.joinpath("uploads", "queued.mp3")
        mix.write_bytes(b"not-needed")
        with (
            mock.patch.object(
                application,
                "manifest_stale_paths",
                return_value={"changed"},
            ),
            mock.patch.object(application, "update_job") as update,
            mock.patch.object(application, "discard_mix") as discard,
            mock.patch.object(application, "audio_duration") as duration,
        ):
            application.do_identify("job", str(mix), 2)
        duration.assert_not_called()
        discard.assert_called_once_with("job", str(mix))
        self.assertEqual("error", update.call_args.kwargs["status"])

    def test_tracklist_collapse_never_starts_metadata_analysis(self):
        matches = [
            {
                "query_start": 0.0,
                "query_stop": 5.0,
                "match_path": "/music/Set/Track.mp3",
                "match_name": "Track.mp3",
                "match_stop": 5.0,
                "score": 10.0,
                "time_factor": 1.0,
            }
        ]
        with (
            mock.patch.object(
                application,
                "library_track_duration",
                return_value=None,
            ) as duration,
            mock.patch.object(
                application,
                "track_bpm",
                return_value=None,
            ) as bpm,
            mock.patch.object(
                application,
                "track_key",
                return_value=None,
            ) as key,
        ):
            result = application.collapse_matches(matches, 1, 5.0)
        self.assertEqual("Track", result[0]["title"])
        self.assertFalse(duration.call_args.kwargs["analyze"])
        self.assertFalse(bpm.call_args.kwargs["analyze"])
        self.assertFalse(key.call_args.kwargs["analyze"])

    @staticmethod
    def _match(start, score, name="Track.mp3"):
        return {
            "query_start": float(start),
            "query_stop": float(start) + 5.0,
            "match_path": f"/music/Set/{name}",
            "match_name": name,
            "match_stop": 5.0,
            "score": float(score),
            "time_factor": 1.0,
        }

    def _collapse(self, matches, min_segments):
        with (
            mock.patch.object(application, "library_track_duration", return_value=None),
            mock.patch.object(application, "track_bpm", return_value=None),
            mock.patch.object(application, "track_key", return_value=None),
        ):
            return application.collapse_matches(matches, min_segments, 60.0)

    def test_single_segment_above_noise_floor_is_kept_as_weak(self):
        # A modest score (like a real briefly-played track) is kept, not judged
        # by score alone — only flagged weak.
        floor = application.WEAK_MATCH_NOISE_FLOOR
        result = self._collapse([self._match(10.0, floor + 22)], min_segments=2)
        identified = [t for t in result if not t["unidentified"]]
        self.assertEqual(1, len(identified))
        self.assertTrue(identified[0]["weak"])
        self.assertEqual(1, identified[0]["segments"])

    def test_near_zero_single_segment_is_dropped_as_noise(self):
        floor = application.WEAK_MATCH_NOISE_FLOOR
        result = self._collapse([self._match(10.0, floor - 5)], min_segments=2)
        self.assertEqual([], [t for t in result if not t["unidentified"]])

    def test_recent_replay_single_segment_is_suppressed_as_bleed(self):
        # A confident track, a different track, then a lone re-match of the
        # first track seconds later: that last one is bleed-through, not a
        # genuine second play, so it is dropped (the FURTHA/PISTOL PACCIN case).
        gap = application.RECENT_REPLAY_S
        matches = [
            self._match(0.0, 300, "A.mp3"),
            self._match(5.0, 300, "A.mp3"),
            self._match(30.0, 200, "B.mp3"),
            self._match(10.0 + gap - 20, 200, "A.mp3"),
        ]
        titles = [
            t["title"] for t in self._collapse(matches, min_segments=2) if not t["unidentified"]
        ]
        self.assertEqual(1, titles.count("A"))  # only the confident first play
        self.assertIn("B", titles)  # unrelated weak match survives

    def test_multi_segment_track_is_not_weak_even_with_low_scores(self):
        matches = [self._match(0.0, 20), self._match(5.0, 25)]
        result = self._collapse(matches, min_segments=2)
        identified = [t for t in result if not t["unidentified"]]
        self.assertEqual(1, len(identified))
        self.assertFalse(identified[0]["weak"])
        self.assertEqual(2, identified[0]["segments"])
