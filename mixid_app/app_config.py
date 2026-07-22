"""Typed environment-backed configuration for the SCRipper application."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppSettings:
    music_root: str
    upload_dir: str
    db_dir: str
    log_dir: str
    cookie_file: str
    browser_profile_dir: str
    browser_controller_url: str
    start_workers: bool

    @classmethod
    def from_environment(cls):
        return cls(
            music_root=os.environ.get("SCRIPPER_MUSIC_ROOT", "/music"),
            upload_dir=os.environ.get("SCRIPPER_UPLOAD_DIR", "/uploads"),
            db_dir=os.environ.get(
                "SCRIPPER_DB_DIR",
                os.path.expanduser("~/.panako/dbs"),
            ),
            log_dir=os.environ.get("SCRIPPER_LOG_DIR", "/logs"),
            cookie_file=os.environ.get(
                "SCRIPPER_COOKIE_FILE",
                "/cookies/cookies.txt",
            ),
            browser_profile_dir=os.environ.get(
                "SCRIPPER_BROWSER_PROFILE_DIR",
                "/browser-profile",
            ),
            browser_controller_url=os.environ.get(
                "BROWSER_CONTROLLER_URL",
                "http://browser-controller:8090",
            ).rstrip("/"),
            start_workers=os.environ.get("SCRIPPER_START_WORKERS", "1") != "0",
        )
