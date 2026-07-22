"""Strict request schemas and limits for API job submission."""

from urllib.parse import urlsplit

MAX_DOWNLOAD_URLS = 500
MAX_RESOLVE_URLS = 25
MAX_URL_LENGTH = 4096
MAX_FOLDER_LENGTH = 200
MAX_PATH_LENGTH = 4096


class RequestValidationError(ValueError):
    pass


def require_object(data):
    if not isinstance(data, dict):
        raise RequestValidationError("a JSON object is required")
    return data


def validate_path_request(data, field="path"):
    data = require_object(data)
    value = data.get(field, "")
    if not isinstance(value, str):
        raise RequestValidationError(f"{field} must be a string")
    if len(value) > MAX_PATH_LENGTH:
        raise RequestValidationError(f"{field} is too long")
    return value


def split_and_validate_urls(raw, maximum):
    if not isinstance(raw, str):
        raise RequestValidationError("input must be a string")
    candidates = [item.strip() for item in raw.replace(",", "\n").splitlines() if item.strip()]
    return validate_urls(candidates, maximum)


def validate_urls(values, maximum=MAX_DOWNLOAD_URLS):
    if not isinstance(values, list):
        raise RequestValidationError("urls must be a list")
    if not values:
        raise RequestValidationError("no URLs supplied")
    if len(values) > maximum:
        raise RequestValidationError(f"at most {maximum} URLs are allowed")
    clean = []
    seen = set()
    for value in values:
        if not isinstance(value, str):
            raise RequestValidationError("every URL must be a string")
        value = value.strip()
        if not value or len(value) > MAX_URL_LENGTH:
            raise RequestValidationError("URL is empty or too long")
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise RequestValidationError("only absolute HTTP/HTTPS URLs are allowed")
        if value not in seen:
            seen.add(value)
            clean.append(value)
    return clean


def validate_download_request(data):
    data = require_object(data)
    urls = validate_urls(data.get("urls"))
    folder = data.get("folder")
    if not isinstance(folder, str):
        raise RequestValidationError("folder must be a string")
    folder = folder.strip()
    if not folder or len(folder) > MAX_FOLDER_LENGTH:
        raise RequestValidationError("folder is empty or too long")
    if any(char in folder for char in '<>:"/\\|?*') or any(ord(char) < 32 for char in folder):
        raise RequestValidationError("folder contains unsupported characters")
    index_after = data.get("index_after", False)
    if not isinstance(index_after, bool):
        raise RequestValidationError("index_after must be a boolean")
    return urls, folder, index_after
