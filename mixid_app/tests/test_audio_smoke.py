"""Container-only smoke tests for native media and fingerprinting tools."""

import ctypes
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("panako"),
    "container audio tools are unavailable",
)
class AudioRuntimeSmokeTests(unittest.TestCase):
    def test_native_lmdb_library_is_loadable(self):
        ctypes.CDLL("/lib/liblmdb.so")

    def test_ffmpeg_and_panako_process_synthetic_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp, "tone.wav")
            generated = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=30",
                    "-ar",
                    "44100",
                    str(audio),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(0, generated.returncode, generated.stderr)
            duration = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "quiet",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(audio),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertAlmostEqual(30, float(duration.stdout.strip()), places=1)
            panako = subprocess.run(
                ["panako", "same", str(audio), str(audio)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(
                0,
                panako.returncode,
                (panako.stderr or panako.stdout)[-1000:],
            )
