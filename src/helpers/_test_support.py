"""Runtime support for generated API tests.

Every request uses the X-API-KEY provided through the environment. Binary
sentinels resolve to local fixture files without baking API-specific filenames
into generated test plans.
"""
import mimetypes
import os
from pathlib import Path

import requests

BASE_URL = os.environ.get("API_BASE_URL", "https://test.patch.digiclaim.tn/api")
API_KEY = os.environ.get("DIGIEXPERT_API_KEY")

FIXTURES_DIR = Path(__file__).parent / "fixture"
GENERATED_SENTINEL = "<GENERATED>"
FILE_PREFIX = "<FILE:"
FILE_SUFFIX = ">"

def _is_file_sentinel(value) -> bool:
    return isinstance(value, str) and value.startswith(FILE_PREFIX) and value.endswith(FILE_SUFFIX)


def _resolve_fixture(sentinel: str) -> tuple[str, Path]:
    filename = sentinel[len(FILE_PREFIX):-len(FILE_SUFFIX)]
    requested = Path(filename)
    if requested.name != filename or filename in {"", ".", ".."}:
        raise ValueError(f"Unsafe fixture name {filename!r}")

    path = FIXTURES_DIR / requested.name
    if not path.exists():
        # Specs commonly name uploads differently. Reuse a representative local
        # sample with the requested extension (or any sample as a final fallback)
        # while preserving the requested filename in the multipart upload.
        suffix = requested.suffix.lower()
        candidates = sorted(FIXTURES_DIR.glob(f"*{suffix}")) if suffix else []
        candidates += sorted(p for p in FIXTURES_DIR.iterdir() if p.is_file() and p not in candidates)
        path = next(iter(candidates), None)
    if path is None or not path.is_file():
        raise FileNotFoundError(
            f"Test plan references fixture '{filename}', but no sample files exist under "
            f"{FIXTURES_DIR}. Add a representative file there."
        )
    return requested.name, path


def split_multipart(body: dict | None) -> tuple[dict, list]:
    """Split a request_body dict into (form_fields, files) for a multipart
    request. Any '<FILE:name>' sentinel — scalar or inside a list — becomes a
    real open file handle. Multiple files under the same key are sent as
    repeated parts under that same field name (standard convention for
    array-of-file fields, e.g. Spring's `MultipartFile[] data`; switch to
    '{key}[]' below if your backend expects PHP-style bracket naming instead)."""
    fields: dict = {}
    files: list = []
    for key, value in (body or {}).items():
        values = value if isinstance(value, list) else [value]
        if not any(_is_file_sentinel(v) for v in values):
            fields[key] = value
            continue
        for v in values:
            if not _is_file_sentinel(v):
                continue
            filename, path = _resolve_fixture(v)
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            files.append((key, (filename, open(path, "rb"), content_type)))
    return fields, files


def send_request(method: str, path: str, request_body, content_type: str):
    if not API_KEY:
        raise RuntimeError("DIGIEXPERT_API_KEY not set")
    headers = {"X-API-KEY": API_KEY}

    kwargs = {"method": method, "url": f"{BASE_URL}{path}", "headers": headers, "timeout": 15}
    if content_type == "multipart/form-data":
        fields, files = split_multipart(request_body)
        kwargs["data"] = fields
        kwargs["files"] = files
    else:
        kwargs["json"] = request_body

    try:
        return requests.request(**kwargs)
    finally:
        for _, (_, fh, _) in kwargs.get("files", []):
            fh.close()


def assert_response(actual, expected, context: str = ""):
    _assert_value(actual, expected, context)


def _assert_value(actual, expected, path: str):
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected an object, got {actual!r}"
        for key, exp_v in expected.items():
            assert key in actual, f"{path}missing key {key!r} in {actual}"
            _assert_value(actual[key], exp_v, f"{path}{key}.")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected a list, got {actual!r}"
        assert len(actual) == len(expected), (
            f"{path}: expected {len(expected)} item(s), got {len(actual)}: {actual!r}"
        )
        for i, (a, e) in enumerate(zip(actual, expected)):
            _assert_value(a, e, f"{path}[{i}].")
    elif expected == GENERATED_SENTINEL:
        # assert actual is not None or actual==NULL, f"{path}: expected a server-generated value, got None"
        return
    else:
        assert actual == expected, f"{path}: expected {expected!r}, got {actual!r}"
