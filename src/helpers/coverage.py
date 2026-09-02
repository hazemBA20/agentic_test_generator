"""Deterministic coverage audit for generated test plans.

Compares the plans produced for one operation against the coverage rules the
planner prompt asks for (a negative per required field, a negative per enum, a
scenario per documented status code) and reports what is missing. This half
never calls a model: everything here is derivable from the operation schema, so
it is free and reproducible. Gaps that need judgment to turn into a scenario —
naming the condition that produces a documented 409, for example — are reported
with ``scenario=None`` for the LLM half to propose.

Mirrors ``plan_validation.py``: pure functions, no I/O, no LLM.
"""
from typing import Any

# The planner is explicitly told not to generate auth scenarios (prompts.py) and
# the runner always attaches its configured key, so an uncovered 401/403 is a
# deliberate omission rather than a gap. 5xx is likewise not request-driven.
# Same set the rewriter refuses to auto-fix (rewrite_failed.py INFRA_STATUSES).
UNREACHABLE_STATUSES = {401, 403, 500, 502, 503}
AUTH_HEADERS = {"authorization", "x-api-key"}
MAX_DEREF_DEPTH = 12


def _deref(schema: Any, definitions: dict, _seen: frozenset = frozenset()) -> dict:
    """Follow a ``$ref`` against the operation's resolved definitions bag.

    ``extract_operation_with_refs`` (parser.py) collects every reachable local
    ref into ``definitions``, so resolution here is a dict lookup rather than a
    walk back into the source document. Cycles resolve to ``{}`` rather than
    recursing forever.
    """
    if not isinstance(schema, dict):
        return {}
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    if ref in _seen or len(_seen) >= MAX_DEREF_DEPTH:
        return {}
    return _deref(definitions.get(ref) or {}, definitions, _seen | {ref})


def _body_schema(operation: dict, definitions: dict) -> dict:
    """The request schema for whichever media type this operation actually uses."""
    content = (operation.get("requestBody") or {}).get("content") or {}
    for media_type in (
        "application/json",
        "multipart/form-data",
        "application/x-www-form-urlencoded",
    ):
        if media_type in content:
            return _deref((content[media_type] or {}).get("schema") or {}, definitions)
    for media in content.values():
        if isinstance(media, dict) and media.get("schema"):
            return _deref(media["schema"], definitions)
    return {}


def _composed(schema: dict, definitions: dict) -> list[dict]:
    """Flatten ``allOf`` into the parent schema.

    ``oneOf``/``anyOf`` are deliberately not flattened: their branches are
    alternative request shapes, and demanding that every branch's fields be
    present would report gaps that aren't real. Those are left to the LLM pass.
    """
    resolved = _deref(schema, definitions)
    parts = [resolved]
    for branch in resolved.get("allOf") or []:
        parts.extend(_composed(branch, definitions))
    return parts


def _required_fields(schema: dict, definitions: dict) -> list[str]:
    required: list[str] = []
    for part in _composed(schema, definitions):
        for name in part.get("required") or []:
            if isinstance(name, str) and name not in required:
                required.append(name)
    return required


def _enum_fields(schema: dict, definitions: dict) -> dict[str, list]:
    """Map top-level property name -> allowed values, for properties with an enum.

    Only top-level properties are checked. A nested enum needs a nested payload
    to exercise, which the structural "did any plan send an invalid value" match
    below cannot verify reliably.
    """
    enums: dict[str, list] = {}
    for part in _composed(schema, definitions):
        for name, prop in (part.get("properties") or {}).items():
            resolved = _deref(prop, definitions)
            values = resolved.get("enum")
            if isinstance(values, list) and values:
                enums.setdefault(str(name), values)
    return enums


def _required_params(operation: dict, definitions: dict) -> list[dict]:
    """Required query/header params worth a missing-param negative.

    Path parameters are excluded: the planner prompt notes that omitting a path
    segment doesn't reach the same operation. Auth headers are excluded because
    the runner injects them.
    """
    params = []
    for raw in operation.get("parameters") or []:
        param = _deref(raw, definitions) if isinstance(raw, dict) else {}
        if not param.get("required") or param.get("in") not in {"query", "header"}:
            continue
        name = param.get("name")
        if not isinstance(name, str):
            continue
        if param["in"] == "header" and name.lower() in AUTH_HEADERS:
            continue
        params.append({"name": name, "in": param["in"]})
    return params


def _documented_statuses(operation: dict) -> list[int]:
    """Numeric response codes a request-level scenario could plausibly target."""
    statuses = []
    for key in (operation.get("responses") or {}):
        text = str(key)
        if not text.isdigit() or len(text) != 3:
            continue  # 'default', '2XX' and friends aren't concrete targets
        code = int(text)
        if code not in UNREACHABLE_STATUSES:
            statuses.append(code)
    return sorted(statuses)


def _negatives(plans: list[dict]) -> list[dict]:
    return [plan for plan in plans if plan.get("category") in {"negative", "boundary"}]


def _body(plan: dict) -> dict:
    body = plan.get("request_body")
    return body if isinstance(body, dict) else {}


def _covers_missing_field(plans: list[dict], field: str, required: list[str]) -> bool:
    """A plan tests 'field is missing' when it's a negative that omits exactly it.

    Matched structurally rather than by name: an LLM-chosen test name is not
    evidence of what the request actually does.
    """
    for plan in _negatives(plans):
        body = _body(plan)
        if not body or field in body:
            continue
        # Omitting several required fields at once tests something vaguer than
        # "this one field is required", so don't count it as covering any of them.
        if [name for name in required if name not in body] == [field]:
            return True
    return False


def _covers_invalid_enum(plans: list[dict], field: str, allowed: list) -> bool:
    for plan in _negatives(plans):
        value = _body(plan).get(field)
        if value is None or isinstance(value, (dict, list)):
            continue
        if value not in allowed:
            return True
    return False


def _covers_missing_param(plans: list[dict], name: str, location: str) -> bool:
    key = "query_params" if location == "query" else "headers"
    for plan in _negatives(plans):
        supplied = plan.get(key)
        if isinstance(supplied, dict) and name not in supplied:
            return True
    return False


def _scenario(name: str, category: str, description: str, status: int, focus: str) -> dict:
    return {
        "name": name,
        "category": category,
        "description": description,
        "target_status_code": status,
        "focus": focus,
    }


def _snake(text: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in str(text))
    return "_".join(part for part in cleaned.lower().split("_") if part) or "field"


def _slug(operation_path: str, method: str) -> str:
    parts = [
        segment
        for segment in str(operation_path).split("/")
        if segment and not segment.startswith("{")
    ]
    return _snake(f"{method}_{'_'.join(parts[-2:])}")


def _negative_status(statuses: list[int]) -> int:
    """The documented 4xx a synthesized negative should target."""
    client_errors = [code for code in statuses if 400 <= code < 500]
    return client_errors[0] if client_errors else 400


def audit_operation(wrapper: dict, plans: list[dict]) -> tuple[dict, list[dict]]:
    """Audit one operation's plans. Returns (report, gaps).

    Each gap is ``{kind, detail, scenario}``. ``scenario`` is a ScenarioSpec-shaped
    dict when this module can synthesize one, or ``None`` when naming the
    triggering condition needs judgment the schema doesn't supply.
    """
    operation = wrapper.get("operation") or {}
    definitions = wrapper.get("definitions") or {}
    path = wrapper.get("path", "")
    method = wrapper.get("method", "")
    slug = _slug(path, method)

    schema = _body_schema(operation, definitions)
    required = _required_fields(schema, definitions)
    enums = _enum_fields(schema, definitions)
    params = _required_params(operation, definitions)
    statuses = _documented_statuses(operation)
    negative_status = _negative_status(statuses)

    gaps: list[dict] = []
    covered_fields, missing_fields = [], []
    for field in required:
        if _covers_missing_field(plans, field, required):
            covered_fields.append(field)
            continue
        missing_fields.append(field)
        gaps.append({
            "kind": "required_field",
            "detail": f"required field {field!r} has no missing-field negative",
            "scenario": _scenario(
                f"test_{slug}_missing_{_snake(field)}",
                "negative",
                f"Omit the required field {field} and expect the request to be rejected.",
                negative_status,
                f"missing {field}",
            ),
        })

    covered_enums, missing_enums = [], []
    for field, allowed in enums.items():
        if _covers_invalid_enum(plans, field, allowed):
            covered_enums.append(field)
            continue
        missing_enums.append(field)
        gaps.append({
            "kind": "enum",
            "detail": f"enum field {field!r} has no invalid-value negative",
            "scenario": _scenario(
                f"test_{slug}_invalid_{_snake(field)}",
                "negative",
                f"Send a value outside the documented enum for {field} and expect rejection.",
                negative_status,
                f"{field} invalid enum value",
            ),
        })

    covered_params, missing_params = [], []
    for param in params:
        label = f"{param['name']} ({param['in']})"
        if _covers_missing_param(plans, param["name"], param["in"]):
            covered_params.append(label)
            continue
        missing_params.append(label)
        gaps.append({
            "kind": "required_param",
            "detail": f"required {param['in']} parameter {param['name']!r} has no missing-param negative",
            "scenario": _scenario(
                f"test_{slug}_missing_{_snake(param['name'])}_{param['in']}",
                "negative",
                f"Omit the required {param['in']} parameter {param['name']} and expect rejection.",
                negative_status,
                f"missing {param['name']} {param['in']} parameter",
            ),
        })

    targeted = {plan.get("expected_status_code") for plan in plans}
    covered_statuses = [code for code in statuses if code in targeted]
    missing_statuses = [code for code in statuses if code not in targeted]

    # Every negative synthesized above targets the documented 4xx, so that code
    # will be covered once the gaps are filled. Reporting it as a status gap too
    # would send the LLM off to invent a second, near-duplicate scenario for it.
    will_target = {gap["scenario"]["target_status_code"] for gap in gaps}
    for code in missing_statuses:
        if code in will_target:
            continue
        response = (operation.get("responses") or {}).get(str(code)) or {}
        described = response.get("description") or ""
        # No scenario: only the operation's own prose says what triggers this code.
        gaps.append({
            "kind": "status_code",
            "status": code,
            "detail": f"documented status {code} is never targeted"
                      + (f" ({described})" if described else ""),
            "scenario": None,
        })

    has_happy_path = any(
        plan.get("category") == "happy_path"
        and isinstance(plan.get("expected_status_code"), int)
        and 200 <= plan["expected_status_code"] < 300
        for plan in plans
    )
    if not has_happy_path:
        gaps.append({
            "kind": "happy_path",
            "detail": "no happy-path plan with a 2xx expected status",
            "scenario": None,
        })

    report = {
        "path": path,
        "method": method,
        "plan_count": len(plans),
        "checklist": {
            "status_codes": {"covered": covered_statuses, "missing": missing_statuses},
            "required_fields": {"covered": covered_fields, "missing": missing_fields},
            "enums": {"covered": covered_enums, "missing": missing_enums},
            "required_params": {"covered": covered_params, "missing": missing_params},
            "happy_path": has_happy_path,
        },
    }
    return report, gaps


def plans_for_operation(wrapper: dict, plans: list[dict]) -> list[dict]:
    """Plans belonging to one operation, matched on the backfilled method/path."""
    path, method = wrapper.get("path"), wrapper.get("method")
    return [
        plan
        for plan in plans
        if isinstance(plan, dict) and plan.get("path") == path and plan.get("method") == method
    ]
