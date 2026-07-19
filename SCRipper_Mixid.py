#!/usr/bin/env python3
"""
SCRipper_Mixid - Identify tracks in a recorded DJ mix by matching against
your local library, using Panako (time-stretch/pitch-shift resistant
audio fingerprinting) running in Docker.

Usage:
    python SCRipper_Mixid.py index "Trap Mix"        Index a library folder
    python SCRipper_Mixid.py identify mix.wav        Identify tracks in a mix
    python SCRipper_Mixid.py stats                   Show database stats
    python SCRipper_Mixid.py clear                   Delete the fingerprint DB

The fingerprint database lives at ~/.panako/docker on the host.
"""
import sys
import os
import re
import argparse
import subprocess
import shutil
from collections import defaultdict

DOCKER_IMAGE = "panako:2.1"
DB_DIR = os.path.expanduser("~/.panako/docker")
STRATEGY = "panako"  # time-stretch + pitch-shift resistant algorithm

def to_docker_path(host_path):
    """Docker volume mounts need the file's directory; we mount the parent
    and reference by basename."""
    host_path = os.path.abspath(host_path)
    return os.path.dirname(host_path), os.path.basename(host_path)

def run_panako(args, mount_dir=None, timeout=None):
    """Run a panako command inside the Docker container."""
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{DB_DIR}:/root/.panako/dbs",
    ]
    if mount_dir:
        cmd += ["-v", f"{mount_dir}:/root/audio"]
    cmd += [DOCKER_IMAGE, "panako"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result

def cmd_index(folder):
    """Fingerprint every audio file in a folder into the Panako DB."""
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        print(f"Not a folder: {folder}")
        return 1
    audio_files = sorted(
        f for f in os.listdir(folder)
        if f.lower().endswith((".mp3", ".m4a", ".wav", ".flac", ".ogg"))
    )
    if not audio_files:
        print(f"No audio files found in {folder}")
        return 1

    os.makedirs(DB_DIR, exist_ok=True)
    print(f"Indexing {len(audio_files)} files from {folder}...")
    failed = []
    for i, fname in enumerate(audio_files, 1):
        print(f"  [{i}/{len(audio_files)}] {fname} ... ", end="", flush=True)
        result = run_panako(
            ["store", f"STRATEGY={STRATEGY}", fname],
            mount_dir=folder, timeout=300,
        )
        if result.returncode == 0:
            print("ok")
        else:
            print("FAILED")
            failed.append(fname)
            if result.stderr.strip():
                print(f"      {result.stderr.strip().splitlines()[-1]}")

    print(f"\nIndexed {len(audio_files) - len(failed)}/{len(audio_files)} files.")
    if failed:
        print("Failed:")
        for f in failed:
            print(f"  - {f}")
    return 0

def parse_monitor_output(text):
    """Parse Panako monitor output lines into match dicts.

    Actual Panako monitor columns (semicolon separated):
    0 index; 1 total; 2 query path (with -<start>_<stop> segment suffix);
    3 query start (within segment); 4 query stop; 5 match path; 6 match id;
    7 match start; 8 match stop; 9 score; 10 time factor ("1.040 %");
    11 frequency factor; 12 seconds with match
    """
    matches = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 13:
            continue
        if parts[0].lower().startswith(("index", "#")):
            continue
        if parts[5].lower() in ("null", ""):
            continue
        try:
            # Segment offset within the mix, e.g. ...mp3-20.0_45.0
            seg_match = re.search(r"-([\d.]+)_([\d.]+)$", parts[2])
            seg_offset = float(seg_match.group(1)) if seg_match else 0.0
            m = {
                "query_start": seg_offset + float(parts[3]),
                "query_stop": seg_offset + float(parts[4]),
                "match_name": os.path.basename(parts[5]),
                "match_start": float(parts[7]),
                "score": float(parts[9]),
                "time_factor": float(parts[10].replace("%", "").strip()),
                "freq_factor": float(parts[11].replace("%", "").strip()),
            }
        except (ValueError, IndexError):
            continue
        matches.append(m)
    return matches

def fmt_ts(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def collapse_matches(matches, min_segments=2):
    """Collapse consecutive segment matches of the same track into
    one tracklist entry. Requires at least min_segments consecutive
    segment hits to accept a track (filters one-off false positives)."""
    if not matches:
        return []
    matches.sort(key=lambda m: m["query_start"])
    entries = []
    current = None
    for m in matches:
        if current and m["match_name"] == current["name"]:
            current["end"] = m["query_stop"]
            current["segments"] += 1
            current["score"] += m["score"]
            current["time_factors"].append(m["time_factor"])
        else:
            if current:
                entries.append(current)
            current = {
                "name": m["match_name"],
                "start": m["query_start"],
                "end": m["query_stop"],
                "segments": 1,
                "score": m["score"],
                "time_factors": [m["time_factor"]],
            }
    if current:
        entries.append(current)
    return [e for e in entries if e["segments"] >= min_segments]

def cmd_identify(mix_file, min_segments=2):
    """Identify tracks in a mix recording."""
    mix_file = os.path.abspath(mix_file)
    if not os.path.isfile(mix_file):
        print(f"File not found: {mix_file}")
        return 1
    mount_dir, basename = to_docker_path(mix_file)

    print(f"Analyzing mix: {basename}")
    print("This takes a while for long mixes (segment-by-segment matching)...\n")
    result = run_panako(
        ["monitor", f"STRATEGY={STRATEGY}", basename],
        mount_dir=mount_dir, timeout=3600,
    )
    if result.returncode != 0:
        print("Panako monitor failed:")
        print(result.stderr[-2000:] if result.stderr else result.stdout[-2000:])
        return 1

    matches = parse_monitor_output(result.stdout)
    entries = collapse_matches(matches, min_segments=min_segments)

    if not entries:
        print("No tracks identified. Raw panako output:")
        print(result.stdout[-3000:])
        return 0

    print("=" * 60)
    print("TRACKLIST")
    print("=" * 60)
    for e in entries:
        name = os.path.splitext(e["name"])[0]
        avg_tf = sum(e["time_factors"]) / len(e["time_factors"])
        tempo_pct = (avg_tf - 1.0) * 100 if avg_tf > 0 else 0.0
        tempo_note = f"  (tempo {tempo_pct:+.1f}%)" if abs(tempo_pct) > 0.5 else ""
        print(f"{fmt_ts(e['start'])} - {name}{tempo_note}")
    print("=" * 60)
    print(f"{len(entries)} tracks identified")
    return 0

def cmd_stats():
    result = run_panako(["stats", f"STRATEGY={STRATEGY}"], timeout=120)
    print(result.stdout or result.stderr)
    return result.returncode

def cmd_clear():
    if os.path.isdir(DB_DIR):
        confirm = input(f"Delete fingerprint DB at {DB_DIR}? (y/n): ").strip().lower()
        if confirm == "y":
            shutil.rmtree(DB_DIR)
            print("Deleted.")
        else:
            print("Cancelled.")
    else:
        print("No database found.")
    return 0

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="Fingerprint a library folder")
    p_index.add_argument("folder")

    p_id = sub.add_parser("identify", help="Identify tracks in a mix recording")
    p_id.add_argument("mix_file")
    p_id.add_argument("--min-segments", type=int, default=2,
                      help="Min consecutive matched segments to accept a track (default 2)")

    sub.add_parser("stats", help="Show fingerprint DB stats")
    sub.add_parser("clear", help="Delete the fingerprint DB")

    args = parser.parse_args()
    if args.command == "index":
        sys.exit(cmd_index(args.folder))
    elif args.command == "identify":
        sys.exit(cmd_identify(args.mix_file, args.min_segments))
    elif args.command == "stats":
        sys.exit(cmd_stats())
    elif args.command == "clear":
        sys.exit(cmd_clear())
