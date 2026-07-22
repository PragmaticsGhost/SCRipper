"""Explicit object graph attached to each Flask application instance."""

from dataclasses import dataclass
from typing import Any

from app_config import AppSettings


@dataclass(frozen=True)
class ApplicationState:
    settings: AppSettings
    jobs: Any
    metadata: Any
    workers: Any
