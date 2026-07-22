"""Subprocess streaming with enforceable deadlines and process-tree cleanup."""

import os
import queue
import signal
import subprocess
import threading
import time


class ProcessDeadlineExceeded(TimeoutError):
    pass


def terminate_process_tree(proc, grace_seconds=3):
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt" and hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=grace_seconds)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name != "nt" and hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def iter_lines_with_deadline(proc, timeout_seconds):
    """Yield stdout lines while enforcing one deadline for the process."""
    lines = queue.Queue(maxsize=256)
    sentinel = object()
    stopped = threading.Event()

    def enqueue(item):
        while not stopped.is_set():
            try:
                lines.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def read_stdout():
        try:
            for line in proc.stdout:
                if stopped.is_set():
                    break
                enqueue(line)
        finally:
            enqueue(sentinel)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessDeadlineExceeded(
                    f"process exceeded {timeout_seconds:.0f} second deadline"
                )
            try:
                item = lines.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            if item is sentinel:
                break
            yield item
        remaining = max(0.01, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise ProcessDeadlineExceeded(
                f"process exceeded {timeout_seconds:.0f} second deadline"
            ) from exc
    except BaseException:
        terminate_process_tree(proc)
        raise
    finally:
        stopped.set()
        reader.join(timeout=1)


def iter_chunks_with_deadline(proc, timeout_seconds, chunk_size=64 * 1024):
    """Yield bounded binary stdout chunks with a process-wide deadline."""
    chunks = queue.Queue(maxsize=16)
    sentinel = object()
    stopped = threading.Event()

    def enqueue(item):
        while not stopped.is_set():
            try:
                chunks.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def read_stdout():
        try:
            while not stopped.is_set():
                chunk = proc.stdout.read(chunk_size)
                if not chunk:
                    break
                enqueue(chunk)
        finally:
            enqueue(sentinel)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessDeadlineExceeded(
                    f"process exceeded {timeout_seconds:.0f} second deadline"
                )
            try:
                item = chunks.get(timeout=min(0.5, remaining))
            except queue.Empty:
                continue
            if item is sentinel:
                break
            yield item
        remaining = max(0.01, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise ProcessDeadlineExceeded(
                f"process exceeded {timeout_seconds:.0f} second deadline"
            ) from exc
    except BaseException:
        terminate_process_tree(proc)
        raise
    finally:
        stopped.set()
        reader.join(timeout=1)


def audio_process_timeout(duration, minimum=600, multiplier=2.0, maximum=7200):
    if not duration or duration <= 0:
        return minimum
    return min(maximum, max(minimum, duration * multiplier))
