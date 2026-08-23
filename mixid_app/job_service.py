"""Thread-safe in-memory job registry and bounded application queues."""

import queue
import re
import threading
import time
import uuid

from job_state import TERMINAL_STATUSES, prune_jobs
from process_utils import terminate_process_tree


def sanitize_log(value):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))


class JobService:
    def __init__(
        self,
        logger,
        *,
        retention_seconds,
        max_finished,
        panako_capacity,
        download_capacity,
        metadata_capacity,
    ):
        self.logger = logger
        self.retention_seconds = retention_seconds
        self.max_finished = max_finished
        self.jobs = {}
        self.lock = threading.Lock()
        # Cooperative cancellation: an Event per job, plus the live
        # subprocesses a cancel should terminate immediately.
        self.cancels = {}
        self.processes = {}
        self.panako_queue = queue.Queue(maxsize=panako_capacity)
        self.download_queue = queue.Queue(maxsize=download_capacity)
        self.metadata_queue = queue.Queue(maxsize=metadata_capacity)

    def new(self, job_type, label):
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock:
            prune_jobs(self.jobs, now, self.retention_seconds, self.max_finished)
            self.jobs[job_id] = {
                "id": job_id,
                "type": job_type,
                "label": label,
                "status": "queued",
                "progress": None,
                "detail": "",
                "result": None,
                "error": None,
                "log": [],
                "active": {},
                "created_at": now,
                "updated_at": now,
                "finished_at": None,
            }
            self.cancels[job_id] = threading.Event()
        self.logger.info(
            "job %s queued: %s — %s",
            job_id,
            job_type,
            sanitize_log(label),
        )
        return job_id

    def update(self, job_id, **values):
        with self.lock:
            job = self.jobs[job_id]
            now = time.time()
            job.update(values)
            job["updated_at"] = now
            if job.get("status") in TERMINAL_STATUSES and not job.get("finished_at"):
                job["finished_at"] = now
                # Release cancellation bookkeeping for a finished job.
                self.cancels.pop(job_id, None)
                self.processes.pop(job_id, None)
            prune_jobs(self.jobs, now, self.retention_seconds, self.max_finished)

    def append_log(self, job_id, line):
        line = sanitize_log(line)
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            job = self.jobs.get(job_id)
            if job is not None:
                job["log"].append(f"[{stamp}] {line}")
                del job["log"][:-300]
        self.logger.info("job %s: %s", job_id, line)

    def set_active(self, job_id, key, value):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return
            if value is None:
                job["active"].pop(key, None)
            else:
                job["active"][key] = value

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            result = dict(job)
            result["log"] = list(job["log"])
            result["active"] = dict(job["active"])
            return result

    def is_cancelled(self, job_id):
        event = self.cancels.get(job_id)
        return bool(event and event.is_set())

    def active_ids_of_type(self, job_type):
        """IDs of queued/running jobs of a given type (newest last)."""
        with self.lock:
            return [
                jid
                for jid, job in self.jobs.items()
                if job.get("type") == job_type and job.get("status") not in TERMINAL_STATUSES
            ]

    def active_jobs(self):
        """Summaries of all queued/running jobs, oldest first, for the UI
        so a user can see and kill anything that hangs."""
        now = time.time()
        with self.lock:
            active = [
                {
                    "id": jid,
                    "type": job.get("type"),
                    "label": job.get("label"),
                    "status": job.get("status"),
                    "progress": job.get("progress"),
                    "detail": job.get("detail"),
                    "age": now - (job.get("created_at") or now),
                }
                for jid, job in self.jobs.items()
                if job.get("status") not in TERMINAL_STATUSES
            ]
        active.sort(key=lambda j: j["age"], reverse=True)
        return active

    def register_process(self, job_id, proc):
        """Track a live subprocess so a cancel can terminate it at once."""
        with self.lock:
            if job_id in self.cancels:
                self.processes.setdefault(job_id, set()).add(proc)
                already_cancelled = self.cancels[job_id].is_set()
            else:
                already_cancelled = False
        # If cancellation raced ahead of registration, kill it now.
        if already_cancelled:
            terminate_process_tree(proc)

    def clear_process(self, job_id, proc):
        with self.lock:
            procs = self.processes.get(job_id)
            if procs:
                procs.discard(proc)

    def request_cancel(self, job_id):
        """Signal a job to stop. Returns True if a cancel was accepted."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job.get("status") in TERMINAL_STATUSES:
                return False
            event = self.cancels.get(job_id)
            procs = list(self.processes.get(job_id, ()))
            was_queued = job.get("status") == "queued"
        if event:
            event.set()
        for proc in procs:
            terminate_process_tree(proc)
        if was_queued:
            # Never picked up by a worker — finalize immediately.
            self.update(job_id, status="cancelled", detail="", progress=None)
        return True

    def submit(self, job_type, label, job_queue, function, args):
        job_id = self.new(job_type, label)
        try:
            job_queue.put_nowait((job_id, function, args))
        except queue.Full:
            self.update(
                job_id,
                status="error",
                error="Job queue is full; try again later",
            )
            return None
        return job_id
