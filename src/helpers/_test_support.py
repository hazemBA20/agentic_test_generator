"""Runtime support for generated API tests.

Generated company requests use the X-API-KEY provided through the environment.
Binary sentinels resolve to local fixture files without baking API-specific
filenames into generated test plans.
"""
import json
import mimetypes
import os
import re
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

# This module is imported before the workflow's own load_dotenv() runs, so it
# loads .env itself — otherwise a key set only in .env is invisible here.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
load_dotenv()

BASE_URL = os.environ.get("API_BASE_URL")

# Web-UI target override (frontend runs only). None by default, so the
# terminal/pytest path resolves exactly as it always has. The web pipeline
# sets this around a run via ``base_url_override``; nothing outside web/
# touches it.
BASE_URL_OVERRIDE: str | None = None


def _base_url() -> str:
    """Read the target at request time, so a late .env or shell export counts.

    Refusing to guess matters more here than for the key: silently falling back
    to a developer's remembered staging URL can point generated write requests
    at a real environment.
    """
    base = BASE_URL_OVERRIDE or os.environ.get("API_BASE_URL") or BASE_URL
    if not base:
        raise RuntimeError(
            "API_BASE_URL is not set — refusing to guess the target environment. "
            "Export it in your shell or add it to .env at the repo root."
        )
    return base


def _api_key() -> str | None:
    """Read the key at request time, not import time.

    Generated suites are also imported directly by pytest, where the key may be
    exported after this module is first loaded.
    """
    return os.environ.get("DIGIEXPERT_API_KEY")


FIXTURES_DIR = Path(__file__).parent / "fixture"
DATA_FIXTURES_PATH = FIXTURES_DIR / "test_data.json"
GENERATED_SENTINEL = "<GENERATED>"
PRESENT_SENTINELS = {GENERATED_SENTINEL, "<PRESENT>"}
NON_NULL_SENTINEL = "<NON_NULL>"
TYPE_SENTINELS = {
    "<ANY_STRING>": (str, "string"),
    "<ANY_INTEGER>": (int, "integer"),
    "<ANY_NUMBER>": ((int, float), "number"),
    "<ANY_BOOLEAN>": (bool, "boolean"),
    "<ANY_OBJECT>": (dict, "object"),
    "<ANY_ARRAY>": (list, "array"),
}
FILE_PREFIX = "<FILE:"
FILE_SUFFIX = ">"
DATA_PREFIX = "<FIXTURE:"
ENV_PREFIX = "<ENV:"

def _is_file_sentinel(value) -> bool:
    return isinstance(value, str) and value.startswith(FILE_PREFIX) and value.endswith(FILE_SUFFIX)


def _sentinel_key(value: str, prefix: str) -> str | None:
    if not isinstance(value, str) or not value.startswith(prefix) or not value.endswith(FILE_SUFFIX):
        return None
    key = value[len(prefix):-len(FILE_SUFFIX)]
    return key or None


@lru_cache(maxsize=1)
def _data_fixtures() -> dict:
    if not DATA_FIXTURES_PATH.exists():
        return {}
    data = json.loads(DATA_FIXTURES_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{DATA_FIXTURES_PATH} must contain a JSON object")
    return data


def resolve_test_data(value):
    """Resolve exact <FIXTURE:key> and <ENV:NAME> values recursively.

    This works in request bodies as well as path, query, and header parameters.
    It intentionally does not substitute inside arbitrary strings, avoiding
    surprising changes to payload content.
    """
    if isinstance(value, dict):
        return {key: resolve_test_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_test_data(item) for item in value]
    if not isinstance(value, str):
        return value

    fixture_key = _sentinel_key(value, DATA_PREFIX)
    if fixture_key is not None:
        fixtures = _data_fixtures()
        if fixture_key not in fixtures:
            raise KeyError(
                f"Data fixture {fixture_key!r} is not defined in {DATA_FIXTURES_PATH}"
            )
        return resolve_test_data(fixtures[fixture_key])

    env_name = _sentinel_key(value, ENV_PREFIX)
    if env_name is not None:
        env_value = os.environ.get(env_name)
        if env_value is None:
            raise RuntimeError(f"Environment fixture {env_name!r} is not set")
        return env_value
    return value


def _resolve_fixture(sentinel: str) -> tuple[str, Path]:
    filename = sentinel[len(FILE_PREFIX):-len(FILE_SUFFIX)]
    # Tolerate the common model shorthand <FILE:pdf> as <FILE:sample.pdf>
    # rather than treating "pdf" as an extensionless filename and selecting an
    # unrelated fallback file.
    shorthand = bool(re.fullmatch(r"[A-Za-z0-9]+", filename))
    if shorthand:
        filename = f"sample.{filename.lower()}"
    requested = Path(filename)
    if requested.name != filename or filename in {"", ".", ".."}:
        raise ValueError(f"Unsafe fixture name {filename!r}")

    session_hits = _session_ranked(requested.suffix.lower())
    path = FIXTURES_DIR / requested.name
    # A generic shorthand ("give me a jpg") yields to a file uploaded in this
    # web session; an explicit name that exists on disk always wins, exactly
    # as before. With no session uploads configured this branch is identical
    # to the historical behavior.
    if path.exists() and not (shorthand and session_hits):
        return requested.name, path
    if shorthand and session_hits:
        return session_hits[0].name, session_hits[0]
    if not path.exists():
        # Specs commonly name uploads differently. Reuse a representative local
        # sample with the requested extension (or any sample as a final fallback)
        # while preserving the requested filename in the multipart upload.
        # Session uploads rank first within each tier.
        suffix = requested.suffix.lower()
        candidates = list(session_hits)
        if suffix:
            candidates += sorted(
                p for p in FIXTURES_DIR.glob(f"*{suffix}")
                if p not in candidates
            )
        candidates += sorted(
            p for p in FIXTURES_DIR.iterdir()
            if p.is_file() and p not in candidates
        )
        path = next(iter(candidates), None)
    if path is None or not path.is_file():
        raise FileNotFoundError(
            f"Test plan references fixture '{filename}', but no sample files exist under "
            f"{FIXTURES_DIR}. Add a representative file there."
        )
    return requested.name, path


# ---------------------------------------------------------------------------
# Web-session file preference (frontend runs only)
# ---------------------------------------------------------------------------
# Names of fixture files uploaded through the web UI in the current session,
# most-recent first. Empty by default, so the terminal/pytest path resolves
# exactly as it always has. The web pipeline sets this around a run via
# ``preferred_files``; nothing outside web/ touches it.
PREFERRED_FILES: tuple[str, ...] = ()


def _session_ranked(suffix: str) -> list[Path]:
    """Session uploads that exist on disk: same-extension first, then the rest.

    Recency order is preserved within each tier.
    """
    same: list[Path] = []
    other: list[Path] = []
    seen: set[str] = set()
    for name in PREFERRED_FILES:
        if name in seen:
            continue
        seen.add(name)
        candidate = FIXTURES_DIR / Path(name).name
        if not candidate.is_file():
            continue
        (same if candidate.suffix.lower() == suffix else other).append(candidate)
    return same + other


@contextmanager
def preferred_files(names: list[str] | tuple[str, ...] | None):
    """Temporarily prefer web-session uploads in ``_resolve_fixture``."""
    global PREFERRED_FILES
    previous = PREFERRED_FILES
    PREFERRED_FILES = tuple(names or ())
    try:
        yield
    finally:
        PREFERRED_FILES = previous


@contextmanager
def base_url_override(url: str | None):
    """Temporarily override the target base URL (web runs only)."""
    global BASE_URL_OVERRIDE
    previous = BASE_URL_OVERRIDE
    BASE_URL_OVERRIDE = url or None
    try:
        yield
    finally:
        BASE_URL_OVERRIDE = previous


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


def _form_parts(fields: dict) -> list:
    """Plain form fields as file-less multipart parts, one part per list element."""
    parts = []
    for name, value in fields.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item is None:
                continue
            text = item if isinstance(item, str) else json.dumps(item)
            parts.append((str(name), (None, text)))
    return parts


def _multipart_kwargs(fields: dict, files: list, headers: dict) -> dict:
    """Build request kwargs that stay ``multipart/form-data`` in every case.

    ``requests`` only emits a multipart Content-Type when ``files`` is non-empty;
    given an empty list it downgrades to urlencoded, or sends no Content-Type at
    all. A negative test that deliberately omits the file part would then be
    rejected on media type (415) instead of having its body validated — the
    request never reaches the check the test exists to make. Sending the plain
    fields as file-less parts keeps the encoding correct, and a body with no
    parts at all still gets an explicit boundary.
    """
    parts = [*files, *_form_parts(fields)]
    if parts:
        return {"files": parts}
    boundary = uuid.uuid4().hex
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    return {"data": f"--{boundary}--\r\n".encode()}


def _render_path(path: str, path_params: dict) -> str:
    """Substitute and URL-encode every OpenAPI `{parameter}` placeholder."""
    rendered = path
    for name, value in path_params.items():
        token = "{" + str(name) + "}"
        if token not in rendered:
            raise ValueError(f"Path parameter {name!r} is not used by {path!r}")
        if value is None:
            raise ValueError(f"Path parameter {name!r} has no value")
        rendered = rendered.replace(token, quote(str(value), safe=""))

    missing = re.findall(r"\{([^{}]+)\}", rendered)
    if missing:
        raise ValueError(f"Missing path parameter value(s) for {', '.join(missing)}")
    return rendered


def _request_headers(
    extra_headers: dict,
    requires_api_key: bool,
) -> dict[str, str]:
    """Merge operation headers with the company API key when required."""
    headers: dict[str, str] = {}
    if requires_api_key:
        api_key = _api_key()
        if not api_key:
            raise RuntimeError(
                "DIGIEXPERT_API_KEY not set for an API-key-protected operation. "
                "Export it in your shell or add it to .env at the repo root."
            )
        headers["X-API-KEY"] = api_key
    for name, value in extra_headers.items():
        if str(name).lower() in {"authorization", "x-api-key"}:
            continue
        if value is not None:
            headers[str(name)] = ",".join(map(str, value)) if isinstance(value, list) else str(value)
    return headers


def send_request(
    method: str,
    path: str,
    request_body,
    content_type: str,
    path_params: dict | None = None,
    query_params: dict | None = None,
    headers: dict | None = None,
    requires_api_key: bool = False,
    requires_jwt: bool = False,
):
    """Send a request; ``requires_jwt`` is retained but ignored by company policy."""
    request_body = resolve_test_data(request_body)
    path_params = resolve_test_data(path_params or {})
    query_params = resolve_test_data(query_params or {})
    headers = resolve_test_data(headers or {})
    rendered_path = _render_path(path, path_params)

    kwargs = {
        "method": method,
        "url": f"{_base_url()}{rendered_path}",
        "headers": _request_headers(headers, requires_api_key),
        "params": query_params or None,
        "timeout": 15,
    }
    files: list = []
    if content_type == "multipart/form-data":
        fields, files = split_multipart(request_body)
        kwargs.update(_multipart_kwargs(fields, files, kwargs["headers"]))
    elif content_type == "application/x-www-form-urlencoded":
        kwargs["data"] = request_body
    else:
        kwargs["json"] = request_body

    try:
        return requests.request(**kwargs)
    finally:
        for _, (_, handle, _) in files:
            handle.close()


def assert_response(actual, expected, context: str = ""):
    _assert_value(actual, expected, context)


def _assert_array_matcher(actual, options, path: str):
    assert isinstance(actual, list), f"{path}: expected an array, got {actual!r}"
    assert isinstance(options, dict), f"{path}: $array matcher must be an object"
    if "min_items" in options:
        assert len(actual) >= options["min_items"], (
            f"{path}: expected at least {options['min_items']} item(s), got {len(actual)}"
        )
    if "max_items" in options:
        assert len(actual) <= options["max_items"], (
            f"{path}: expected at most {options['max_items']} item(s), got {len(actual)}"
        )
    if "contains" in options:
        failures = []
        for index, item in enumerate(actual):
            try:
                _assert_value(item, options["contains"], f"{path}[{index}].")
                break
            except AssertionError as exc:
                failures.append(str(exc))
        else:
            detail = failures[0] if failures else "array is empty"
            raise AssertionError(f"{path}: no array item matched contains ({detail})")


def _assert_value(actual, expected, path: str):
    if isinstance(expected, str) and expected in PRESENT_SENTINELS:
        return
    if expected == NON_NULL_SENTINEL:
        assert actual is not None, f"{path}: expected a non-null value"
        return
    if isinstance(expected, str) and expected in TYPE_SENTINELS:
        expected_type, type_name = TYPE_SENTINELS[expected]
        matches = isinstance(actual, expected_type)
        if expected in {"<ANY_INTEGER>", "<ANY_NUMBER>"} and isinstance(actual, bool):
            matches = False
        assert matches, f"{path}: expected {type_name}, got {actual!r}"
        return
    if isinstance(expected, dict) and set(expected) == {"$array"}:
        _assert_array_matcher(actual, expected["$array"], path)
        return
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
    else:
        assert actual == expected, f"{path}: expected {expected!r}, got {actual!r}"
