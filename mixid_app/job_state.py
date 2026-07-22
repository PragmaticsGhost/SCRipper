"""Pure helpers for bounded in-memory job state."""

import time

TERMINAL_STATUSES = frozenset({"done", "error", "cancelled"})


def prune_jobs(jobs, now=None, ttl_seconds=24 * 60 * 60, max_finished=100):
    """Remove expired jobs and retain only the newest terminal jobs."""
    now = time.time() if now is None else now
    finished = []
    for jid, job in list(jobs.items()):
        if job.get("status") not in TERMINAL_STATUSES:
            continue
        finished_at = job.get("finished_at") or job.get("updated_at") or 0
        if finished_at and now - finished_at > ttl_seconds:
            jobs.pop(jid, None)
        else:
            finished.append((finished_at, jid))

    overflow = len(finished) - max_finished
    if overflow > 0:
        for _finished_at, jid in sorted(finished)[:overflow]:
            jobs.pop(jid, None)
