"""Thin adapter between the web UI and the existing test-generation pipeline.

Every pipeline capability used here already exists in ``src/``; this module
only orchestrates it and adds the two pieces of new logic the UI needs that
the CLI does not have yet:

- ``scan_operation_fixtures`` — the free, deterministic pre-build scan that
  tells the user which fixtures an operation is likely to need (binary file
  fields, auth schemes, required fields worth pinning as fixture keys).
- ``missing_fixture_report`` — the precise post-build report over generated
  plans (undefined ``<FIXTURE:>`` refs, unresolvable ``<FILE:>`` sentinels,
  unset ``<ENV:>`` names), the shape sketched in ``FIXTURE_CHECK_PLAN.md``.

The heavy workflow import (``workflow.utils.nodes``, which constructs the LLM
clients at import time) is kept lazy: parsing, fixture editing, and the scans
must work without any provider key, exactly like the ``--list`` CLI mode.
"""
import json
import os
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dotenv import load_dotenv

from helpers.coverage import _body_schema, _deref, _required_fields, _required_params
from helpers.generate_test_file import generate
from helpers.parser import ingest_openapi_spec
from helpers.plan_validation import FILE_PATTERN, FIXTURE_PATTERN, _walk, validate_plans

load_dotenv(ROOT / ".env")
load_dotenv()

WORKSPACE = Path(__file__).resolve().parent / "workspace"
UPLOADS_DIR = WORKSPACE / "uploads"
DEFAULT_PLANS_PATH = WORKSPACE / "test_plans.json"
DEFAULT_TESTS_PATH = WORKSPACE / "test.py"
DEFAULT_RESULTS_PATH = WORKSPACE / "test_results.json"
DEFAULT_COVERAGE_PATH = WORKSPACE / "coverage_report.json"
DEFAULT_REVIEW_LOG_PATH = WORKSPACE / "rewrite_log.json"

FIXTURES_DIR = SRC / "helpers" / "fixture"
FIXTURE_DATA_PATH = FIXTURES_DIR / "test_data.json"

ENV_PATTERN = re.compile(r"^<ENV:([^>]+)>$")
ALLOWED_SPEC_SUFFIXES = {".json", ".yaml", ".yml"}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _content_type(operation: dict) -> str:
    """Mirror of nodes._content_type for display; kept local so the UI works
    without importing the LLM clients. The builder derives the real value."""
    content = (operation.get("requestBody") or {}).get("content") or {}
    if "multipart/form-data" in content:
        return "multipart/form-data"
    if "application/x-www-form-urlencoded" in content:
        return "application/x-www-form-urlencoded"
    return "application/json"


def summarize_operations(operations: list[dict]) -> list[dict]:
    """Compact view for the UI. ``index`` is the 0-based position the pipeline
    itself uses; the UI displays it 1-based like the CLI listing."""
    summaries = []
    for index, wrapper in enumerate(operations):
        op = wrapper.get("operation") or {}
        summaries.append(
            {
                "index": index,
                "method": wrapper.get("method"),
                "path": wrapper.get("path"),
                "summary": op.get("summary") or "(no summary)",
                "content_type": _content_type(op),
            }
        )
    return summaries


class Session:
    """The single-user working state: the uploaded spec and its operations."""

    def __init__(self, uploads_dir: Path = UPLOADS_DIR):
        self._lock = threading.Lock()
        self.uploads_dir = Path(uploads_dir)
        self.spec_path: str | None = None
        self.spec_name: str | None = None
        self.operations: list[dict] = []
        # Fixture files uploaded through the UI in this session,
        # most-recent first. Frontend runs prefer these over stale files.
        self.fixture_uploads: list[str] = []
        # Target API override set in the UI. None means "read API_BASE_URL
        # from the environment", exactly like the terminal path.
        self.base_url: str | None = None

    def load_spec(self, filename: str, content: bytes) -> list[dict]:
        """Save an upload and parse it. Raises ValueError on a bad spec, in
        which case the previous spec stays loaded (a failed upload must not
        clobber a working session)."""
        safe_name = Path(filename.replace("\\", "/")).name
        if not safe_name:
            raise ValueError("The uploaded file has no usable name.")
        if Path(safe_name).suffix.lower() not in ALLOWED_SPEC_SUFFIXES:
            raise ValueError(
                f"Unsupported spec type {safe_name!r}: expected one of "
                f"{', '.join(sorted(ALLOWED_SPEC_SUFFIXES))}"
            )
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        target = self.uploads_dir / safe_name
        target.write_bytes(content)

        with self._lock:
            try:
                operations = ingest_openapi_spec(str(target))
            except ValueError:
                raise
            except Exception as exc:  # keep parser failures as 400s, not 500s
                raise ValueError(f"Could not parse {safe_name!r}: {exc}") from exc
            if not operations:
                raise ValueError(f"No HTTP operations found in {safe_name!r}.")
            self.spec_path = str(target)
            self.spec_name = safe_name
            self.operations = operations
        return operations

    def summarized_operations(self) -> list[dict]:
        with self._lock:
            return summarize_operations(self.operations)

    def operation(self, index: int) -> dict:
        with self._lock:
            if not self.operations:
                raise LookupError("No spec uploaded yet.")
            if index < 0 or index >= len(self.operations):
                raise IndexError(
                    f"Operation index {index} is outside 0..{len(self.operations) - 1}"
                )
            return self.operations[index]

    def note_fixture_upload(self, name: str) -> list[str]:
        """Remember a UI fixture upload, most-recent first (deduplicated)."""
        with self._lock:
            self.fixture_uploads = [n for n in self.fixture_uploads if n != name]
            self.fixture_uploads.insert(0, name)
            del self.fixture_uploads[20:]
            return list(self.fixture_uploads)

    def snapshot_uploads(self) -> list[str]:
        with self._lock:
            return list(self.fixture_uploads)

    def set_base_url(self, value: str | None) -> str | None:
        """Set (or clear, with None/"") the UI target override."""
        normalized = normalize_base_url(value)
        with self._lock:
            self.base_url = normalized
            return self.base_url

    def snapshot_base_url(self) -> str | None:
        with self._lock:
            return self.base_url


# ---------------------------------------------------------------------------
# Fixtures: the real store the pipeline and generated tests read at runtime
# ---------------------------------------------------------------------------

def clear_pipeline_caches() -> None:
    """Invalidate the lru_caches the pipeline holds over the fixture store.

    Without this the builder would keep offering the key list it read at
    first use, and fixture edits made in the UI would be invisible until a
    server restart (USAGE_GUIDE.md documents the same restart requirement
    for the CLI).
    """
    try:
        from workflow.utils import nodes

        nodes._available_fixture_keys.cache_clear()
    except Exception:
        # The workflow import constructs the LLM clients; without provider
        # keys there is no cache to clear yet — the first real import reads
        # the file fresh.
        pass
    try:
        from helpers import _test_support

        _test_support._data_fixtures.cache_clear()
    except Exception:
        pass


def load_fixture_data(path: Path = FIXTURE_DATA_PATH) -> dict:
    if not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def update_fixture_data(updates: dict, path: Path = FIXTURE_DATA_PATH) -> dict:
    """Merge new/changed keys into the store, preserving everything else."""
    if not isinstance(updates, dict) or not updates:
        raise ValueError("Provide at least one fixture key to set.")
    for key in updates:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Fixture keys must be non-empty strings.")
    data = load_fixture_data(path)
    data.update(updates)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    clear_pipeline_caches()
    return data


def delete_fixture_key(key: str, path: Path = FIXTURE_DATA_PATH) -> dict:
    data = load_fixture_data(path)
    if key not in data:
        raise KeyError(f"Fixture key {key!r} is not defined in {path}")
    del data[key]
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    clear_pipeline_caches()
    return data


def list_fixture_files(fixtures_dir: Path = FIXTURES_DIR) -> list[str]:
    directory = Path(fixtures_dir)
    if not directory.exists():
        return []
    reserved = {Path(FIXTURE_DATA_PATH).name}
    return sorted(
        p.name
        for p in directory.iterdir()
        if p.is_file() and p.name not in reserved and not p.name.startswith(".")
    )


def save_fixture_file(filename: str, content: bytes, fixtures_dir: Path = FIXTURES_DIR) -> str:
    safe_name = Path(filename.replace("\\", "/")).name
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("The uploaded file has no usable name.")
    if safe_name == Path(FIXTURE_DATA_PATH).name:
        raise ValueError(
            f"{safe_name!r} is the fixture data store — edit it as data keys instead."
        )
    directory = Path(fixtures_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / safe_name).write_bytes(content)
    clear_pipeline_caches()
    return safe_name


# ---------------------------------------------------------------------------
# Pre-build scan: deterministic fixture needs straight from the operation
# ---------------------------------------------------------------------------

def _binary_field_paths(schema: Any, definitions: dict, prefix: str = "") -> list[str]:
    """Dotted paths of properties declared as binary uploads (type string +
    format binary), including inside arrays and nested objects."""
    resolved = _deref(schema, definitions)
    if not isinstance(resolved, dict):
        return []
    found: list[str] = []
    for name, prop in (resolved.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        path = f"{prefix}.{name}" if prefix else str(name)
        spec = _deref(prop, definitions)
        if isinstance(spec, dict) and spec.get("format") == "binary":
            found.append(path)
        found.extend(_binary_field_paths(spec, definitions, path))
    if isinstance(resolved.get("items"), dict):
        found.extend(_binary_field_paths(resolved["items"], definitions, prefix + "[]"))
    return found


def _covered_key(field: str, available: dict[str, Any]) -> str | None:
    """The fixture key that already covers a field name, if any.

    Matching is deliberately conservative: exact or case-insensitive equal.
    A fuzzy 'looks similar' match would hide real gaps from the user.
    """
    if field in available:
        return field
    lowered = field.lower()
    for key in available:
        if key.lower() == lowered:
            return key
    return None


def scan_operation_fixtures(
    operation: dict,
    fixture_data_path: Path = FIXTURE_DATA_PATH,
    fixtures_dir: Path = FIXTURES_DIR,
) -> dict:
    """What the user will probably have to set before generating for this one
    operation. Everything here is derivable without an LLM call."""
    op = operation.get("operation") or {}
    definitions = operation.get("definitions") or {}
    available = load_fixture_data(fixture_data_path)
    files = list_fixture_files(fixtures_dir)

    media_types = sorted(((op.get("requestBody") or {}).get("content") or {}).keys())
    body_schema = _body_schema(op, definitions)
    binary_paths = _binary_field_paths(body_schema, definitions)
    # Top-level fields that are file uploads (e.g. `registrationDocument` with
    # `format: binary`). These are satisfied with a <FILE:...> upload, never
    # with a <FIXTURE:...> data value, so they must not be suggested as keys.
    binary_top = {path.split(".")[0].removesuffix("[]") for path in binary_paths}
    file_needs = [
        {"field_path": path, "media_type": media}
        for media in ("multipart/form-data", "application/x-www-form-urlencoded", "application/json")
        if media in media_types
        for path in binary_paths
    ]

    auth = []
    schemes = operation.get("security_schemes") or {}
    for requirement in op.get("security") or []:
        for scheme_name in requirement or []:
            scheme = schemes.get(scheme_name) or {}
            if scheme:
                auth.append(
                    {
                        "name": scheme.get("name") or scheme_name,
                        "type": scheme.get("type"),
                        "in": scheme.get("in"),
                        "description": scheme.get("description"),
                    }
                )

    suggestions = []
    for field in _required_fields(body_schema, definitions):
        if field in binary_top:
            continue
        suggestions.append({"field": field, "in": "body", "covered": _covered_key(field, available)})
    for param in _required_params(op, definitions):
        suggestions.append(
            {"field": param["name"], "in": param["in"], "covered": _covered_key(param["name"], available)}
        )
    path_params = operation.get("path") and re.findall(r"\{([^{}]+)\}", operation["path"])
    for name in path_params or []:
        suggestions.append({"field": name, "in": "path", "covered": _covered_key(name, available)})

    return {
        "request_media_types": media_types,
        "file_needs": file_needs,
        "auth": auth,
        "suggested_keys": suggestions,
        "available_keys": sorted(available),
        "available_files": files,
    }


# ---------------------------------------------------------------------------
# Post-build report: the precise fixture needs of generated plans
# ---------------------------------------------------------------------------

def _plan_inputs(plan: dict) -> dict:
    return {
        "request_body": plan.get("request_body"),
        "path_params": plan.get("path_params") or {},
        "query_params": plan.get("query_params") or {},
        "headers": plan.get("headers") or {},
    }


def _file_need_available(requested: str, fixtures_dir: Path) -> bool:
    """Whether <FILE:...> resolution would find a file under ``fixtures_dir``.

    Mirrors the semantics of _test_support._resolve_fixture (shorthand
    ``<FILE:pdf>`` → ``sample.pdf``, exact hit, same-extension fallback, then
    any file) against an arbitrary directory rather than the module-global
    store, so scans and tests can target a scratch directory.
    """
    name = requested[len("<FILE:"):-1]
    if re.fullmatch(r"[A-Za-z0-9]+", name):
        name = f"sample.{name.lower()}"
    if name in {"", ".", ".."} or Path(name).name != name:
        return False
    directory = Path(fixtures_dir)
    if not directory.is_dir():
        return False
    if (directory / name).is_file():
        return True
    suffix = Path(name).suffix.lower()
    if suffix and any(path.is_file() for path in directory.glob(f"*{suffix}")):
        return True
    return any(path.is_file() for path in directory.iterdir())


def missing_fixture_report(
    plans: list[dict],
    fixture_data_path: Path = FIXTURE_DATA_PATH,
    fixtures_dir: Path = FIXTURES_DIR,
) -> dict:
    """Buckets for the UI banner, in the shape sketched in FIXTURE_CHECK_PLAN.md."""
    available = set(load_fixture_data(fixture_data_path))
    undefined_refs: set[str] = set()
    advised: set[str] = set()
    file_sentinels: set[str] = set()
    env_names: set[str] = set()

    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        for value in _walk(_plan_inputs(plan)):
            if not isinstance(value, str):
                continue
            match = FIXTURE_PATTERN.fullmatch(value)
            if match:
                undefined_refs.add(match.group(1))
                continue
            file_match = FILE_PATTERN.fullmatch(value)
            if file_match:
                file_sentinels.add(value)
                continue
            env_match = ENV_PATTERN.fullmatch(value)
            if env_match:
                env_names.add(env_match.group(1))
        for key in plan.get("missing_fixtures") or []:
            if isinstance(key, str):
                advised.add(key)

    missing_data_keys = sorted((undefined_refs | advised) - available)
    file_needs = []
    for sentinel in sorted(file_sentinels):
        name = sentinel[len("<FILE:"):-1]
        file_needs.append(
            {
                "requested": name,
                "ext": Path(name).suffix.lstrip(".").lower(),
                "available": _file_need_available(sentinel, fixtures_dir),
            }
        )
    env_needs = sorted(name for name in env_names if name not in os.environ)
    return {
        "missing_data_keys": missing_data_keys,
        "file_needs": file_needs,
        "env_needs": env_needs,
    }


# ---------------------------------------------------------------------------
# Generation and execution
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Target base URL: UI override first, environment fallback
# ---------------------------------------------------------------------------

def normalize_base_url(value: str | None) -> str | None:
    """Validate a UI-provided target. Empty/None clears back to env fallback."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if not re.fullmatch(r"https?://[^/\s]+(?:/[^\s]*)?", text):
        raise ValueError(
            f"Invalid base URL {value!r}: expected e.g. https://test-api.example.com/api"
        )
    return text.rstrip("/")


def resolve_base_url(session_base_url: str | None = None) -> tuple[str | None, str]:
    """Effective target: the UI override, else live API_BASE_URL, else nothing.

    Returns (url, source) with source in {"session", "env", "none"}.
    """
    if session_base_url:
        return session_base_url, "session"
    env = os.environ.get("API_BASE_URL")
    if env:
        return env, "env"
    return None, "none"


# ---------------------------------------------------------------------------
# Full pipeline: the complete LangGraph workflow (plan → build → render →
# coverage/fill → execute → review → verify), streamed node-by-node for UI
# progress. Same graph as the CLI; artifacts go to the web workspace.
# ---------------------------------------------------------------------------

FULL_PIPELINE_STAGES = {
    "ingest": "Loading operations",
    "planner": "Planning scenarios",
    "builder": "Building test plans",
    "persist_plans": "Saving plans",
    "render": "Rendering pytest",
    "coverage": "Auditing coverage",
    "fill_gaps": "Filling coverage gaps",
    "execute": "Sending live requests",
    "review": "Reviewing failures",
}


def _read_json_list(path: Path) -> list:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return payload if isinstance(payload, list) else []


def run_full_pipeline(
    session: Session,
    operation_index: int | None = None,
    run_all: bool = False,
    coverage: bool = False,
    run_tests: bool = False,
    review: bool = False,
    base_url: str | None = None,
    preferred_files: list[str] | None = None,
    progress=None,
    plans_path: Path = DEFAULT_PLANS_PATH,
    tests_path: Path = DEFAULT_TESTS_PATH,
    results_path: Path = DEFAULT_RESULTS_PATH,
    review_log_path: Path = DEFAULT_REVIEW_LOG_PATH,
    coverage_report_path: Path = DEFAULT_COVERAGE_PATH,
) -> dict:
    """Run the whole CLI workflow for the uploaded spec into the web workspace.

    Mirrors ``src/main.py`` flags: single operation vs ``--all``, ``--coverage``,
    ``--run-tests`` (``--review`` implies it), one constrained ``--review`` pass.
    ``progress`` is an optional ``report(dict)`` sink; terminal callers omit it.
    """
    from workflow.graph import compile_workflow

    if not session.operations:
        raise LookupError("No spec uploaded yet — start with step 1.")
    if run_all:
        operation_summary = None
    else:
        if operation_index is None:
            raise ValueError("Pick an operation or choose all operations.")
        operation_summary = summarize_operations([session.operation(operation_index)])[0]

    run_tests = bool(run_tests or review)
    if run_tests:
        target, _ = resolve_base_url(base_url)
        if not target:
            raise ValueError("No target set — set one in step 4 or add API_BASE_URL to .env.")

    if not session.spec_path:
        raise LookupError("No spec uploaded yet — start with step 1.")

    state = {
        "spec_path": session.spec_path,
        "operation_index": operation_index or 0,
        "run_all": run_all,
        "plans_path": str(plans_path),
        "tests_path": str(tests_path),
        "results_path": str(results_path),
        "review_log_path": str(review_log_path),
        "coverage_report_path": str(coverage_report_path),
        "run_tests": run_tests,
        "review": review,
        "reviewed": False,
        "review_pass": 0,
        "coverage": coverage,
        "coverage_done": False,
    }

    def _emit(node: str) -> None:
        if progress is not None:
            progress({
                "stage": node,
                "label": FULL_PIPELINE_STAGES.get(node, node),
            })

    from helpers import _test_support

    workflow = compile_workflow()
    merged: dict = {}
    with _test_support.preferred_files(preferred_files), _test_support.base_url_override(base_url):
        for chunk in workflow.stream(state):
            for node, update in chunk.items():
                _emit(node)
                if isinstance(update, dict):
                    merged.update(update)

    plans = _read_json_list(plans_path)
    results = _read_json_list(results_path)
    coverage_report = _read_json_list(coverage_report_path)
    review_log = _read_json_list(review_log_path)
    gaps = sum(len(entry.get("gaps") or []) for entry in coverage_report if isinstance(entry, dict))
    passed = sum(bool(result.get("passed")) for result in results)
    skipped = sum(bool(result.get("skipped")) for result in results)

    return {
        "scope": {
            "run_all": run_all,
            "operation": operation_summary,
            "operation_count": len(session.summarized_operations()),
        },
        "options": {"coverage": coverage, "run_tests": run_tests, "review": review},
        "plans": plans,
        "builder_failures": merged.get("build_failures") or 0,
        "filled_count": merged.get("filled_count") or 0,
        "patched_count": merged.get("patched_count") or 0,
        "review_errors": merged.get("review_errors") or 0,
        "issues_by_plan": validate_plans(plans),
        "fixture_report": missing_fixture_report(plans),
        "results": results,
        "summary": {
            "total": len(results),
            "passed": passed,
            "skipped": skipped,
            "failed": len(results) - passed - skipped,
        },
        "coverage_gaps": gaps,
        "review_log": review_log,
        "plans_path": str(plans_path),
        "tests_path": str(tests_path) if Path(tests_path).exists() else None,
        "tests_generated": Path(tests_path).exists(),
        "results_path": str(results_path) if Path(results_path).exists() else None,
        "coverage_report_path": str(coverage_report_path) if Path(coverage_report_path).exists() else None,
        "review_log_path": str(review_log_path) if Path(review_log_path).exists() else None,
        "preferred_files": list(preferred_files or []),
        "base_url": resolve_base_url(base_url)[0],
    }


def _plan_dict(plan) -> dict:
    return plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)


def generate_for_operation(
    session: Session,
    operation_index: int,
    plans_path: Path = DEFAULT_PLANS_PATH,
    tests_path: Path = DEFAULT_TESTS_PATH,
    progress=None,
) -> dict:
    """One full pipeline run for a single operation, into the web workspace.

    Calls the planner and builder directly (not compile_workflow) so each
    regeneration overwrites only this run's artifacts and the CLI's files are
    never touched. ``progress`` is an optional ``report(dict)`` sink for the
    UI (stage/done/total); terminal callers omit it.
    """
    from workflow.utils import nodes

    def _emit(stage: str, label: str, done: int | None = None, total: int | None = None) -> None:
        if progress is not None:
            progress({"stage": stage, "label": label, "done": done, "total": total})

    operation = session.operation(operation_index)
    plans_path = Path(plans_path)
    tests_path = Path(tests_path)

    _emit("planning", "Planning scenarios")
    scenarios, planner_failures = nodes.plan_scenarios([operation])
    plans: list[dict] = []
    builder_failures = 0
    if any(scenarios):
        batch_size = nodes.BUILD_BATCH_SIZE
        total_batches = sum(
            (len(op_scenarios) + batch_size - 1) // batch_size
            for op_scenarios in scenarios
            if op_scenarios
        )
        _emit("building", "Building test plans", 0, total_batches)

        def _on_batch(done: int, total: int) -> None:
            _emit("building", "Building test plans", done, total)

        built, builder_failures = nodes.build_plans_with_failures(
            [operation], scenarios, progress_cb=_on_batch if progress is not None else None
        )
        plans = [_plan_dict(plan) for plan in built]

    tests_generated = False
    if plans:
        _emit("rendering", "Rendering pytest")
        plans_path.parent.mkdir(parents=True, exist_ok=True)
        plans_path.write_text(json.dumps(plans, indent=2), encoding="utf-8")
        generate(plans_path, tests_path)
        tests_generated = True

    _emit("validating", "Validating plans")
    issues_by_plan = validate_plans(plans)
    return {
        "operation": summarize_operations([operation])[0],
        "plans": plans,
        "planner_failures": planner_failures,
        "builder_failures": builder_failures,
        "issues_by_plan": issues_by_plan,
        "fixture_report": missing_fixture_report(plans),
        "plans_path": str(plans_path),
        "tests_path": str(tests_path) if tests_generated else None,
        "tests_generated": tests_generated,
    }


def run_generated_tests(
    plans_path: Path = DEFAULT_PLANS_PATH,
    results_path: Path = DEFAULT_RESULTS_PATH,
    preferred_files: list[str] | None = None,
    base_url: str | None = None,
    progress=None,
) -> dict:
    """Execute the workspace plans against the effective target.

    ``preferred_files`` (web-session uploads), ``base_url`` (UI target
    override) and ``progress`` (UI stage sink) are honored only here, inside
    their scopes: the terminal/pytest path never sets them and behaves
    exactly as before.
    """
    from helpers import _test_support
    from helpers.execute_plans import execute_plans

    plans_path = Path(plans_path)
    if not plans_path.exists():
        raise LookupError(f"No generated plans at {plans_path} — generate tests first.")
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    if not plans:
        raise LookupError(f"{plans_path} holds no plans — generate tests first.")

    def _on_plan(done: int, total: int) -> None:
        if progress is not None:
            progress({"stage": "running", "label": "Sending live requests", "done": done, "total": total})

    if progress is not None:
        progress({"stage": "running", "label": "Sending live requests", "done": 0, "total": len(plans)})
    with _test_support.preferred_files(preferred_files), _test_support.base_url_override(base_url):
        results = execute_plans(
            plans, progress_cb=_on_plan if progress is not None else None
        )
    results_path = Path(results_path)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    passed = sum(bool(result.get("passed")) for result in results)
    skipped = sum(bool(result.get("skipped")) for result in results)
    effective, source = resolve_base_url(base_url)
    return {
        "results": results,
        "summary": {"total": len(results), "passed": passed, "skipped": skipped, "failed": len(results) - passed - skipped},
        "results_path": str(results_path),
        "preferred_files": list(preferred_files or []),
        "base_url": effective,
        "base_url_source": source,
    }


# ---------------------------------------------------------------------------
# Background jobs: one worker thread, one job at a time
# ---------------------------------------------------------------------------

class JobInProgress(RuntimeError):
    """Raised when a second job is submitted while one is running."""


class JobRunner:
    """All pipeline work runs on one dedicated worker thread.

    The workflow's event loop is bound to the thread that first uses it
    (nodes._run), so every LLM call must happen on the same thread. One job
    at a time also matches the single-user, local nature of this tool.
    """

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
        self._lock = threading.Lock()
        self._job: dict | None = None

    def report(self, update: dict) -> None:
        """Record a progress update for the running job (best-effort UI feed).

        The update replaces the previous one wholesale so readers never see a
        torn dict. Calls with no running job are ignored.
        """
        with self._lock:
            if self._job is not None and self._job["state"] == "running":
                self._job["progress"] = {
                    "stage": update.get("stage"),
                    "label": update.get("label") or update.get("stage"),
                    "done": update.get("done"),
                    "total": update.get("total"),
                    "detail": update.get("detail"),
                    "at": time.time(),
                }

    def submit(self, kind: str, fn) -> dict:
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise JobInProgress(
                    f"A {self._job['kind']} job is already running — wait for it to finish."
                )
            job = {
                "kind": kind,
                "state": "running",
                "progress": None,
                "result": None,
                "error": None,
                "traceback": None,
                "started_at": time.time(),
                "finished_at": None,
            }
            self._job = job

        def _work():
            try:
                job["result"] = fn()
                job["state"] = "done"
            except Exception as exc:
                job["state"] = "error"
                job["error"] = f"{type(exc).__name__}: {exc}"
                job["traceback"] = "".join(traceback.format_exception(exc))
            job["finished_at"] = time.time()

        self._executor.submit(_work)
        return {"kind": kind, "state": "running"}

    def snapshot(self) -> dict:
        with self._lock:
            if self._job is None:
                return {"kind": None, "state": "idle", "progress": None}
            return dict(self._job)


def health(session: Session, fixture_data_path: Path = FIXTURE_DATA_PATH, fixtures_dir: Path = FIXTURES_DIR) -> dict:
    """Which prerequisites are in place — names only, never secret values."""
    has = {
        "GEMINI_API_KEY": bool(os.environ.get("GEMINI_API_KEY")),
        "EXPLABS_API_KEY": bool(os.environ.get("EXPLABS_API_KEY")),
        "GROQ_API_KEY": bool(os.environ.get("groq_key") or os.environ.get("GROQ_API_KEY")),
        "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
    }
    base_url, base_url_source = resolve_base_url(session.snapshot_base_url())
    return {
        "providers": has,
        "providers_ready": has["GEMINI_API_KEY"] and has["OPENROUTER_API_KEY"],
        "api_base_url_set": bool(base_url),
        "base_url": base_url,
        "base_url_source": base_url_source,
        "target_api_key_set": bool(os.environ.get("DIGIEXPERT_API_KEY")),
        "fixture_data_keys": len(load_fixture_data(fixture_data_path)),
        "fixture_files": list_fixture_files(fixtures_dir),
        "spec_loaded": session.spec_name,
        "operation_count": len(session.summarized_operations()),
    }
