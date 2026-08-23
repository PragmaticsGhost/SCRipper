"""Flask route registration kept separate from application services."""

from flask import Blueprint

ROUTE_DEFINITIONS = (
    ("/", ("GET",), "index"),
    ("/api/job/<jid>", ("GET",), "api_job"),
    ("/api/job/<jid>/cancel", ("POST",), "api_cancel_job"),
    ("/api/jobs/active", ("GET",), "api_active_jobs"),
    ("/api/health", ("GET",), "api_health"),
    ("/api/ready", ("GET",), "api_ready"),
    ("/api/login-browser/start", ("POST",), "api_login_browser_start"),
    ("/api/login-browser/stop", ("POST",), "api_login_browser_stop"),
    ("/api/login-browser/status", ("GET",), "api_login_browser_status"),
    ("/api/cookies/capture", ("POST",), "api_cookies_capture"),
    ("/api/cookies", ("GET", "POST", "DELETE"), "api_cookies"),
    ("/api/folders", ("GET",), "api_folders"),
    ("/api/resolve-folder", ("POST",), "api_resolve_folder"),
    ("/api/index", ("POST",), "api_index"),
    ("/api/identify", ("POST",), "api_identify"),
    ("/api/mix-stream", ("GET",), "api_mix_stream"),
    ("/api/mix", ("DELETE",), "api_mix_delete"),
    ("/api/history", ("GET",), "api_history"),
    ("/api/history/<hid>", ("GET", "PATCH", "DELETE"), "api_history_one"),
    ("/api/library", ("GET",), "api_library"),
    ("/api/files", ("GET",), "api_files"),
    ("/api/download", ("GET",), "api_download"),
    ("/api/zip", ("GET",), "api_zip"),
    ("/api/upload-tracks", ("POST",), "api_upload_tracks"),
    ("/api/stream", ("GET",), "api_stream"),
    ("/api/waveform", ("GET",), "api_waveform"),
    ("/api/scripper/resolve", ("POST",), "api_scripper_resolve"),
    ("/api/scripper/download", ("POST",), "api_scripper_download"),
)


def create_blueprint(handlers):
    blueprint = Blueprint("scripper", __name__)
    blueprint.before_app_request(handlers._enforce_local_origin)
    for path, methods, name in ROUTE_DEFINITIONS:
        blueprint.add_url_rule(
            path,
            endpoint=name,
            view_func=getattr(handlers, name),
            methods=list(methods),
        )
    return blueprint
