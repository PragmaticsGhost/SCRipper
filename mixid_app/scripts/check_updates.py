"""Report pinned Python dependencies that have newer releases available.

Run inside the test image so the interpreter version matches production.

Two things are reported:

* **Update available** — a newer release exists and this image could install it.
* **Blocked by Python** — a newer release exists but requires a newer Python
  than the image ships, so pip silently keeps an older pin. This is the trap
  that left yt-dlp ten months stale on Debian bullseye (Python 3.9) and broke
  YouTube downloads, so it is called out loudly.

Informational by default: prints findings and exits 0 so an upstream release
never breaks the build. Pass --strict to exit 1 when updates are available.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 20
# Extractors for third-party sites rot fastest; surface them first.
PRIORITY = {"yt-dlp"}


def parse_lock(path):
    pins = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            spec = line.split("--hash", 1)[0].strip()
            if "==" not in spec:
                continue
            name, version = spec.split("==", 1)
            pins[name.strip().lower()] = version.strip()
    return pins


def parse_version(text):
    """Best-effort PEP 440-ish ordering without extra dependencies."""
    parts = []
    for chunk in str(text).replace("-", ".").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def python_ok(requires_python):
    """True when this interpreter satisfies a >=/> lower bound."""
    if not requires_python:
        return True
    current = sys.version_info[:2]
    for clause in requires_python.split(","):
        clause = clause.strip()
        if clause.startswith(">="):
            if current < parse_version(clause[2:])[:2]:
                return False
        elif clause.startswith(">"):
            if current <= parse_version(clause[1:])[:2]:
                return False
    return True


def fetch(name):
    url = f"https://pypi.org/pypi/{name}/json"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        payload = json.load(response)
    info = payload["info"]
    return info["version"], info.get("requires_python")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit 1 when updates exist")
    parser.add_argument(
        "--lock",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "requirements.lock"),
    )
    args = parser.parse_args()

    pins = parse_lock(args.lock)
    if not pins:
        print("check-updates: no pins found", file=sys.stderr)
        return 1

    runtime = ".".join(str(p) for p in sys.version_info[:3])
    print(f"check-updates: {len(pins)} pins, image Python {runtime}")

    updates, blocked, unreachable = [], [], []
    for name in sorted(pins):
        try:
            latest, requires_python = fetch(name)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            unreachable.append((name, str(exc)[:60]))
            continue
        if parse_version(latest) <= parse_version(pins[name]):
            continue
        if python_ok(requires_python):
            updates.append((name, pins[name], latest))
        else:
            blocked.append((name, pins[name], latest, requires_python))

    if blocked:
        print("\nBLOCKED BY PYTHON VERSION (pip cannot install the newer release):")
        for name, pinned, latest, requires in blocked:
            flag = "  <-- extractor package, likely breaking downloads" if name in PRIORITY else ""
            print(f"  {name}: pinned {pinned}, latest {latest} needs Python {requires}{flag}")
        print("  Fix: raise the base image Python, then regenerate requirements.lock.")

    if updates:
        print("\nUpdates available:")
        for name, pinned, latest in sorted(updates, key=lambda u: (u[0] not in PRIORITY, u[0])):
            flag = "  <-- extractor package, update regularly" if name in PRIORITY else ""
            print(f"  {name}: {pinned} -> {latest}{flag}")

    if unreachable:
        print(f"\nCould not check {len(unreachable)} package(s): "
              f"{', '.join(n for n, _ in unreachable)}")

    if not updates and not blocked:
        print("All pins are current.")

    if args.strict and (updates or blocked):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
