"""Runtime support for the generated test suite. Not a test file itself —
test.py imports these. Auth is lazy and cached: a JWT is only fetched the
first time a test that actually needs one runs, so a login outage doesn't
block tests (like /version) that don't require auth at all."""
import os
from pathlib import Path

import requests

BASE_URL = os.environ.get("API_BASE_URL", "https://test.expert.digiclaim.tn/api")
API_KEY = os.environ.get("DIGIEXPERT_API_KEY")
LOGIN_USERNAME = os.environ.get("DIGIEXPERT_USERNAME")
LOGIN_PASSWORD = os.environ.get("DIGIEXPERT_PASSWORD")

FIXTURES_DIR = Path(__file__).parent / "fixture"
GENERATED_SENTINEL = "<GENERATED>"
FILE_PREFIX = "<FILE:"
FILE_SUFFIX = ">"

_token_cache: dict[str, str] = {}


def _get_jwt() -> str:
    if "token" in _token_cache:
        return _token_cache["token"]
    if not (LOGIN_USERNAME and LOGIN_PASSWORD):
        raise RuntimeError("DIGIEXPERT_USERNAME / DIGIEXPERT_PASSWORD not set — needed to obtain a JWT")
    resp = requests.post(
        f"{BASE_URL}/core/external/login",
        headers={"X-API-KEY": API_KEY},
        json={"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json()["token"]["value"]
    _token_cache["token"] = token
    return token


def _is_file_sentinel(value) -> bool:
    return isinstance(value, str) and value.startswith(FILE_PREFIX) and value.endswith(FILE_SUFFIX)


def _resolve_fixture(sentinel: str) -> tuple[str, Path]:
    filename = sentinel[len(FILE_PREFIX):-len(FILE_SUFFIX)]
    path = FIXTURES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"Test plan references fixture '{filename}' which doesn't exist at "
            f"{path}. Add it under fixtures/."
        )
    return filename, path


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
            files.append((key, (filename, open(path, "rb"))))
    return fields, files


def send_request(method: str, path: str, request_body, content_type: str,
                  requires_api_key: bool, requires_jwt: bool):
    headers = {}
    if requires_api_key:
        if not API_KEY:
            raise RuntimeError("DIGIEXPERT_API_KEY not set")
        headers["X-API-KEY"] = API_KEY
    if requires_jwt:
        headers["Authorization"] = f"Bearer {_get_jwt()}"

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
        for _, (_, fh) in kwargs.get("files", []):
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