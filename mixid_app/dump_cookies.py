#!/usr/bin/env python3
"""
Dump SoundCloud (and optionally YouTube) cookies from Chrome into
mixid_app/cookies/cookies.txt so the dockerized SCRipper downloader
can use them.

Run this ON THE HOST (not in the container) — Chrome's cookie store is
encrypted with your Windows user account and can only be read here:

    .venv/Scripts/python.exe mixid_app/dump_cookies.py
    .venv/Scripts/python.exe mixid_app/dump_cookies.py --youtube

Re-run whenever downloads start failing with auth errors (cookies expire).
The container picks up the new file immediately — no restart needed.
"""

import argparse
import os
import sys
from http.cookiejar import MozillaCookieJar

try:
    import browser_cookie3
except ImportError:
    print("browser_cookie3 not installed. Run: pip install browser_cookie3")
    sys.exit(1)

COOKIE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies")
COOKIE_FILE = os.path.join(COOKIE_DIR, "cookies.txt")


def main():
    parser = argparse.ArgumentParser(description="Dump browser cookies for the SCRipper container")
    parser.add_argument("--youtube", action="store_true", help="Also include youtube.com cookies")
    args = parser.parse_args()

    domains = ["soundcloud.com"]
    if args.youtube:
        domains.append("youtube.com")

    os.makedirs(COOKIE_DIR, exist_ok=True)
    cj = MozillaCookieJar(COOKIE_FILE)
    total = 0
    for domain in domains:
        try:
            for ck in browser_cookie3.chrome(domain_name=domain):
                cj.set_cookie(ck)
                total += 1
        except Exception as e:
            print(f"Warning: could not read {domain} cookies: {e}")
    cj.save(ignore_discard=True, ignore_expires=True)
    print(f"Wrote {total} cookies ({', '.join(domains)}) to {COOKIE_FILE}")
    print("The container will use them automatically on the next download.")


if __name__ == "__main__":
    main()
