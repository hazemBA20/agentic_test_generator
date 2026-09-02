"""Deterministic preflight validation for generated test plans."""
import json
import re
import warnings
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_FIXTURE_DATA = ROOT / "fixture" / "test_data.json"
FIXTURE_PATTERN = re.compile(r"^<FIXTURE:([^>]+)>$")
FILE_PATTERN = re.compile(r"^<FILE:([^>]+)>$")
PATH_PARAMETER_PATTERN = re.compile(r"\{([^{}]+)\}")


def _load_fixture_keys(path: Path) -> tuple[set[str], str | None]:
    if not path.exists():
        return set(), f"fixture data file {path} does not exist"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return set(), f"could not read fixture data file {path}: {exc}"
    if not isinstance(data, dict):
        return set(), f"fixture data file {path} must contain a JSON object"
    return set(data), None


def fixture_keys(path: Path = DEFAULT_FIXTURE_DATA) -> set[str]:
    """Return available fixture keys without making fixture-file errors global."""
    available, issue = _load_fixture_keys(path)
    if issue:
        warnings.warn(issue, UserWarning, stacklevel=2)
    return available


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def plan_errors(
    plan: dict,
    available_fixtures: set[str],
    fixture_data_path: Path = DEFAULT_FIXTURE_DATA,
    fixture_load_issue: str | None = None,
) -> list[str]:
    errors: list[str] = []
    name = plan.get("name") or "unnamed"

    for field in ("name", "method", "path", "expected_status_code"):
        if field not in plan or plan[field] is None:
            errors.append(f"{name}: missing required plan field: {field}")

    path = plan.get("path", "")
    if not isinstance(path, str):
        errors.append(f"{name}: path must be a string")
        path = ""

    raw_path_params = plan.get("path_params")
    if raw_path_params is not None and not isinstance(raw_path_params, dict):
        errors.append(f"{name}: path_params must be an object")
        path_params = {}
    else:
        path_params = raw_path_params or {}

    required_path_params = set(PATH_PARAMETER_PATTERN.findall(path))
    supplied_path_params = set(path_params)

    missing_path_params = sorted(required_path_params - supplied_path_params)
    extra_path_params = sorted(supplied_path_params - required_path_params)
    if missing_path_params:
        errors.append(f"{name}: missing path parameter(s): {', '.join(missing_path_params)}")
    if extra_path_params:
        errors.append(f"{name}: unused path parameter(s): {', '.join(extra_path_params)}")

    inputs = {
        "request_body": plan.get("request_body"),
        "path_params": path_params,
        "query_params": plan.get("query_params") or {},
        "headers": plan.get("headers") or {},
    }
    referenced_fixtures: set[str] = set()
    for location, value in inputs.items():
        for item in _walk(value):
            if not isinstance(item, str):
                continue
            match = FIXTURE_PATTERN.fullmatch(item)
            if match:
                referenced_fixtures.add(match.group(1))
            elif item.startswith("<FIXTURE"):
                errors.append(f"{name}: malformed fixture sentinel in {location}: {item!r}")
            elif item.startswith("<FILE") and not FILE_PATTERN.fullmatch(item):
                errors.append(f"{name}: malformed file sentinel in {location}: {item!r}")

    if referenced_fixtures and fixture_load_issue:
        errors.append(
            f"{name}: cannot validate referenced fixture(s) "
            f"{', '.join(sorted(referenced_fixtures))}: {fixture_load_issue}"
        )
    else:
        unknown = sorted(referenced_fixtures - available_fixtures)
        if unknown:
            errors.append(
                f"{name}: undefined fixture(s): {', '.join(unknown)}; "
                f"add them to {fixture_data_path}"
            )

    return errors


def validate_plans(
    plans: list[dict],
    fixture_data_path: Path = DEFAULT_FIXTURE_DATA,
    *,
    strict: bool = False,
) -> list[list[str]]:
    """Validate plans and return fatal issues aligned with plan indexes.

    Normal mode warns instead of raising so callers can quarantine only the bad
    plans. ``missing_fixtures`` is advisory and never appears in the returned
    issue lists because plans carry concrete fallback request data.
    """
    available, fixture_load_issue = _load_fixture_keys(fixture_data_path)
    if fixture_load_issue:
        warnings.warn(fixture_load_issue, UserWarning, stacklevel=2)

    issues_by_plan: list[list[str]] = []
    for index, plan in enumerate(plans):
        if not isinstance(plan, dict):
            issues = [f"plan at index {index} must be a JSON object"]
            name = f"plan at index {index}"
        else:
            issues = plan_errors(
                plan,
                available,
                fixture_data_path=fixture_data_path,
                fixture_load_issue=fixture_load_issue,
            )
            name = plan.get("name") or "unnamed"

            missing_fixtures = plan.get("missing_fixtures") or []
            if not isinstance(missing_fixtures, list) or not all(
                isinstance(key, str) for key in missing_fixtures
            ):
                warnings.warn(
                    f"{name}: advisory missing_fixtures must be a list of strings; "
                    "validation will continue",
                    UserWarning,
                    stacklevel=2,
                )
            else:
                unresolved = sorted(set(missing_fixtures) - available)
                if unresolved:
                    warnings.warn(
                        f"{name}: advisory missing fixture(s): {', '.join(unresolved)}; "
                        f"not found in fixture file {fixture_data_path}; validation will continue",
                        UserWarning,
                        stacklevel=2,
                    )

        issues_by_plan.append(issues)
        if not strict:
            for issue in issues:
                warnings.warn(issue, UserWarning, stacklevel=2)

    fatal_issues = [issue for issues in issues_by_plan for issue in issues]
    if strict and fatal_issues:
        details = "\n".join(f"- {issue}" for issue in fatal_issues)
        raise ValueError(f"Generated plans failed preflight validation:\n{details}")
    return issues_by_plan
