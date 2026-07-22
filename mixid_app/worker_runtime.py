"""Cooperative lifecycle management for in-process background workers."""

import atexit
import threading


class WorkerRuntime:
    def __init__(self):
        self.stop_event = threading.Event()
        self._threads = {}
        self._lock = threading.Lock()
        atexit.register(self.stop)

    def start(self, name, target, *args):
        with self._lock:
            existing = self._threads.get(name)
            if existing and existing.is_alive():
                return existing
            if self.stop_event.is_set():
                self.stop_event = threading.Event()
            thread = threading.Thread(
                name=f"scripper-{name}",
                target=target,
                args=(*args, self.stop_event),
                daemon=True,
            )
            self._threads[name] = thread
            thread.start()
            return thread

    def stop(self, timeout=5):
        self.stop_event.set()
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, timeout))

    def health(self):
        with self._lock:
            return {name: thread.is_alive() for name, thread in self._threads.items()}
