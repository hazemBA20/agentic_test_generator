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


def fixture_keys(path: Path = DEFAULT_FIXTURE_DATA) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return set(data)


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def plan_errors(plan: dict, available_fixtures: set[str]) -> list[str]:
    errors: list[str] = []
    name = plan.get("name", "unnamed")
    path = plan.get("path", "")
    path_params = plan.get("path_params") or {}
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
            elif item.startswith("<FIXTURE:"):
                errors.append(f"{name}: malformed fixture sentinel in {location}: {item!r}")
            elif item.startswith("<FILE:") and not FILE_PATTERN.fullmatch(item):
                errors.append(f"{name}: malformed file sentinel in {location}: {item!r}")

    unknown = sorted(referenced_fixtures - available_fixtures)
    if unknown:
        errors.append(
            f"{name}: undefined fixture(s): {', '.join(unknown)}; add them to {DEFAULT_FIXTURE_DATA}"
        )

    return errors


def validate_plans(plans: list[dict], fixture_data_path: Path = DEFAULT_FIXTURE_DATA) -> None:
    available = fixture_keys(fixture_data_path)
    errors: list[str] = []
    for plan in plans:
        errors.extend(plan_errors(plan, available))
        unresolved = sorted(set(plan.get("missing_fixtures") or []) - available)
        if unresolved:
            name = plan.get("name", "unnamed")
            warnings.warn(
                f"{name}: advisory missing fixture(s): {', '.join(unresolved)}; "
                f"not found in fixture file {fixture_data_path}; validation will continue",
                UserWarning,
                stacklevel=2,
            )
    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise ValueError(f"Generated plans failed preflight validation:\n{details}")
