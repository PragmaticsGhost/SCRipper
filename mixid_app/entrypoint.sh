#!/bin/sh
# Runs as root only to align ownership on the app's managed volumes, then
# drops privileges to the unprivileged `scripper` user for the app itself.
# This keeps yt-dlp / ffmpeg / Panako from running as root.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# Allow matching the host user's ids (useful on Linux bind mounts).
if [ "$PGID" != "1000" ]; then groupmod -o -g "$PGID" scripper 2>/dev/null || true; fi
if [ "$PUID" != "1000" ]; then usermod  -o -u "$PUID" scripper 2>/dev/null || true; fi

# Ensure the app-managed locations are writable by the runtime user.
# /music is intentionally left untouched — it is the user's own library
# (chowning a bind-mounted host dir would rewrite their file ownership).
for d in /home/scripper/.panako /uploads /cookies /logs; do
    mkdir -p "$d"
    chown -R scripper:scripper "$d" 2>/dev/null || true
done

exec gosu scripper "$@"
