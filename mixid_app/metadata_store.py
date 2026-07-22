"""Thread-safe persistent stores for duration, BPM, and musical-key metadata."""

import threading

from state_storage import atomic_write_json, load_json

MISSING = object()


class PersistentJsonCache:
    def __init__(self, path, logger=None):
        self.path = path
        self.logger = logger
        self._values = None
        self._lock = threading.Lock()

    def _load(self):
        if self._values is None:
            try:
                values = load_json(self.path, dict)
            except OSError:
                values = {}
                if self.logger:
                    self.logger.warning(
                        "could not load cache %s",
                        self.path,
                        exc_info=True,
                    )
            self._values = values if isinstance(values, dict) else {}
        return self._values

    def get(self, key):
        with self._lock:
            return self._load().get(key, MISSING)

    def set(self, key, value):
        with self._lock:
            values = self._load()
            values[key] = value
            try:
                atomic_write_json(self.path, values)
            except OSError:
                if self.logger:
                    self.logger.warning(
                        "could not persist cache %s",
                        self.path,
                        exc_info=True,
                    )


class MetadataStores:
    def __init__(self, db_dir, logger=None):
        import os

        self.duration = PersistentJsonCache(
            os.path.join(db_dir, "duration_cache_v1.json"),
            logger,
        )
        self.bpm = PersistentJsonCache(
            os.path.join(db_dir, "bpm_cache_v3.json"),
            logger,
        )
        self.key = PersistentJsonCache(
            os.path.join(db_dir, "key_cache_v3.json"),
            logger,
        )
