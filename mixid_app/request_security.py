"""Same-origin checks for a localhost-only browser application."""

from urllib.parse import urlsplit


def is_cross_origin_browser_request(scheme, host, origin=None, sec_fetch_site=None, referer=None):
    """Reject browser requests that declare a different initiating origin.

    Requests without browser provenance headers remain available to local CLI
    clients. Host-header validation is handled separately by the application.
    """
    fetch_site = (sec_fetch_site or "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return True

    expected = ((scheme or "http").lower(), (host or "").lower())
    for candidate in (origin, referer):
        if not candidate:
            continue
        parsed = urlsplit(candidate)
        actual = (parsed.scheme.lower(), parsed.netloc.lower())
        if parsed.scheme not in {"http", "https"} or actual != expected:
            return True
    return False
