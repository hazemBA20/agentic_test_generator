"""Patch failed test plans using the live-run results from execute_plans.py.

Only body/status failures are sent to the LLM. Unexpected 401/403/5xx status
mismatches are skipped; a test that *expected* 401/403 and got it (kind=body)
can still get an assertion patch.

method, path, content_type, and expected_status_code are never changed.
kind=body patches only expected_response; kind=status patches exactly one of the
request input fields. Every skip/patch reason is written to rewrite_log.json.

Usage (from this directory, after execute_plans.py):
    python rewrite_failed.py
    python rewrite_failed.py test_plans.json test_results.json
"""
import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openrouter import ChatOpenRouter
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent.parent / ".env")
load_dotenv()

REWRITEABLE = {"body", "status"}
INFRA_STATUSES = {401, 403, 500, 502, 503}
REWRITE_BATCH_SIZE = 4

SYSTEM_PROMPT = """You repair generated API test plans that failed against a live server.

You receive a batch of failures. For each item, decide skip or patch. Return one
entry per failure, using the plan's name unchanged.

The plan's intent must stay the same: same category, same condition under test,
same expected_status_code.

Decide:
- skip: the failure is not something a request/assertion tweak can honestly fix
  (auth, server bug, unreachable scenario, spec vs server disagreement you should
  not hide). Prefer skip over making a test pass by copying a buggy server.
- patch: adjust ONE field so the plan still tests the same condition.

Rules:
- kind=body means the HTTP status already matched. Patch only expected_response.
  Use the literal string "<GENERATED>" for volatile or free-text fields. Only
  assert exact values the server actually returned AND that the scenario cares
  about. Leave request_body null.
- kind=status means the HTTP status was wrong. Patch exactly one of request_body,
  path_params, query_params, or headers so the request realizes the same scenario
  (missing field, invalid enum, etc.). Leave all other patch fields and
  expected_response null. Never include X-API-KEY in headers. Do not change
  expected_status_code.
- Do not invent new scenarios.
- Keep file-upload values as "<FILE:sample.pdf>" / "<FILE:sample.jpg>" when a
  binary field is required.
- If you are not confident, skip. Missing names are treated as skip.
"""

USER_PROMPT = """Failures to review (one object per plan). Produce exactly one
decision per name, same names, same order if possible:

{batch}
"""


class NamedPatch(BaseModel):
    name: str = Field(..., description="Matches the failing plan's name")
    action: Literal["skip", "patch"] = Field(..., description="skip if this failure should not be auto-fixed")
    reason: str = Field(..., description="One sentence why you patched or skipped")
    request_body: dict[str, Any] | None = Field(
        None, description="Replacement request body when kind=status and action=patch; else null"
    )
    path_params: dict[str, Any] | None = Field(
        None, description="Replacement path parameters when kind=status and action=patch; else null"
    )
    query_params: dict[str, Any] | None = Field(
        None, description="Replacement query parameters when kind=status and action=patch; else null"
    )
    headers: dict[str, Any] | None = Field(
        None, description="Replacement non-auth headers when kind=status and action=patch; else null"
    )
    expected_response: dict[str, Any] | None = Field(
        None, description="Replacement response assertions when kind=body and action=patch; else null"
    )


class PatchBatch(BaseModel):
    patches: list[NamedPatch] = Field(default_factory=list)


def _rewriter():
    return ChatOpenRouter(
        model=os.getenv("REWRITE_MODEL", "deepseek/deepseek-v4-flash"),
        temperature=0,
        max_tokens=8000,
        reasoning={"effort": "low"},
    ).with_structured_output(PatchBatch)


def _should_rewrite(result: dict) -> str | None:
    """Return a skip reason, or None if the LLM should see this failure."""
    if result.get("passed"):
        return "already passed"
    kind = result.get("kind")
    if kind not in REWRITEABLE:
        return f"kind={kind} is not auto-fixed"
    # kind=body already means status matched expected (see execute_plans.py),
    # so 401/403 body failures are intended auth tests, not infra skips.
    if kind == "status" and result.get("status_code") in INFRA_STATUSES:
        return (
            f"HTTP {result.get('status_code')} looks like auth/server, "
            "not a bad assertion"
        )
    return None


def _clip_body(body: Any) -> Any:
    if body is None:
        return None
    if isinstance(body, str):
        return body[:1500]
    dumped = json.dumps(body, ensure_ascii=False, default=str)
    if len(dumped) <= 4000:
        return body
    return dumped[:4000]


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _log(entries: list[dict], **entry) -> None:
    entries.append(entry)


def _apply(plan: dict, patch: NamedPatch, kind: str) -> str | None:
    """Apply the field this kind is allowed to change. Return skip reason or None."""
    if kind == "body":
        if patch.expected_response is None:
            return "body patch missing expected_response"
        plan["expected_response"] = patch.expected_response
        return None
    if kind == "status":
        replacements = {
            "request_body": patch.request_body,
            "path_params": patch.path_params,
            "query_params": patch.query_params,
            "headers": patch.headers,
        }
        supplied = [(field, value) for field, value in replacements.items() if value is not None]
        if len(supplied) != 1:
            return "status patch must supply exactly one request input field"
        field, value = supplied[0]
        if field == "headers" and any(str(key).lower() == "x-api-key" for key in value):
            return "status patch attempted to replace X-API-KEY"
        plan[field] = value
        return None
    return f"kind={kind} is not auto-fixed"


def _invoke_batch(llm, items: list[dict]) -> dict[str, NamedPatch]:
    payload = []
    for item in items:
        failure = dict(item["result"])
        failure["body"] = _clip_body(item["result"].get("body"))
        payload.append({"plan": item["plan"], "failure": failure})

    batch = llm.invoke(
        [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=USER_PROMPT.format(
                batch=json.dumps(payload, ensure_ascii=False, default=str)
            )),
        ]
    )
    by_name: dict[str, NamedPatch] = {}
    for patch in batch.patches:
        by_name[patch.name] = patch
    return by_name


def _fetch_patches(llm, items: list[dict], log_entries: list[dict]) -> dict[str, NamedPatch]:
    """One batched call, then a repair call for names the model dropped."""
    sent = {item["plan"]["name"] for item in items}
    try:
        by_name = _invoke_batch(llm, items)
    except Exception as e:
        for item in items:
            _log(
                log_entries,
                name=item["plan"].get("name"),
                kind=item["result"].get("kind"),
                action="skip",
                source="error",
                reason=f"LLM error ({e})",
            )
        return {}

    missing = [item for item in items if item["plan"]["name"] not in by_name]
    if missing:
        try:
            by_name.update(_invoke_batch(llm, missing))
        except Exception as e:
            for item in missing:
                _log(
                    log_entries,
                    name=item["plan"].get("name"),
                    kind=item["result"].get("kind"),
                    action="skip",
                    source="error",
                    reason=f"LLM repair-call error ({e})",
                )

    for name in sent - by_name.keys():
        _log(
            log_entries,
            name=name,
            kind=next(i["result"].get("kind") for i in items if i["plan"]["name"] == name),
            action="skip",
            source="implicit",
            reason="name missing from model response",
        )
    return by_name


def rewrite_plans(plans: list[dict], results: list[dict]) -> tuple[list[dict], int, list[dict]]:
    log_entries: list[dict] = []
    candidates: list[dict] = []

    results_by_name: dict[str, dict] = {}
    for result in results:
        name = result.get("name")
        if not name:
            continue
        if name in results_by_name:
            _log(
                log_entries,
                name=name,
                kind=result.get("kind"),
                action="skip",
                source="deterministic",
                reason="duplicate result name; keeping the first",
            )
            print(f"  skip duplicate result name: {name}")
            continue
        results_by_name[name] = result

    seen_plans: set[str] = set()
    for plan in plans:
        name = plan.get("name")
        if not name:
            _log(
                log_entries,
                name=name,
                kind=None,
                action="skip",
                source="deterministic",
                reason="plan has no name",
            )
            continue
        if name in seen_plans:
            _log(
                log_entries,
                name=name,
                kind=None,
                action="skip",
                source="deterministic",
                reason="duplicate plan name",
            )
            print(f"  skip duplicate plan name: {name}")
            continue
        seen_plans.add(name)

        result = results_by_name.get(name)
        if result is None:
            _log(
                log_entries,
                name=name,
                kind=None,
                action="skip",
                source="deterministic",
                reason="no matching result",
            )
            print(f"  skip {name}: no matching result")
            continue

        skip_reason = _should_rewrite(result)
        if skip_reason:
            _log(
                log_entries,
                name=name,
                kind=result.get("kind"),
                action="skip",
                source="deterministic",
                reason=skip_reason,
            )
            print(f"  skip {name}: {skip_reason}")
            continue

        candidates.append({"plan": plan, "result": result})

    llm = _rewriter()
    patched = 0
    for batch in _chunked(candidates, REWRITE_BATCH_SIZE):
        by_name = _fetch_patches(llm, batch, log_entries)
        for item in batch:
            plan = item["plan"]
            result = item["result"]
            name = plan["name"]
            kind = result.get("kind")
            patch = by_name.get(name)
            if patch is None:
                print(f"  skip {name}: name missing from model response")
                continue
            if patch.action != "patch":
                _log(
                    log_entries,
                    name=name,
                    kind=kind,
                    action="skip",
                    source="llm",
                    reason=patch.reason,
                )
                print(f"  skip {name}: {patch.reason}")
                continue

            apply_skip = _apply(plan, patch, kind)
            if apply_skip:
                _log(
                    log_entries,
                    name=name,
                    kind=kind,
                    action="skip",
                    source="apply",
                    reason=apply_skip,
                )
                print(f"  skip {name}: {apply_skip}")
                continue

            if kind == "body":
                field = "expected_response"
            else:
                field = next(
                    name
                    for name in ("request_body", "path_params", "query_params", "headers")
                    if getattr(patch, name) is not None
                )
            _log(
                log_entries,
                name=name,
                kind=kind,
                action="patch",
                source="llm",
                field=field,
                reason=patch.reason,
            )
            patched += 1
            print(f"  patch {name} [{field}]: {patch.reason}")

    return plans, patched, log_entries


def main(plans_path: Path, results_path: Path, log_path: Path):
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    results = json.loads(results_path.read_text(encoding="utf-8"))
    print(f"Rewriting failures from {results_path}...")
    plans, patched, log_entries = rewrite_plans(plans, results)
    plans_path.write_text(json.dumps(plans, indent=2, default=str), encoding="utf-8")
    log_path.write_text(json.dumps(log_entries, indent=2, default=str), encoding="utf-8")
    print(f"Patched {patched} plan(s). Wrote {plans_path}")
    print(f"Review reasons in {log_path} before trusting a green re-run.")
    print("Re-run: python execute_plans.py")


if __name__ == "__main__":
    plans_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test_plans.json"
    results_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "test_results.json"
    log_arg = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "rewrite_log.json"
    if not plans_arg.exists():
        sys.exit(f"{plans_arg} not found")
    if not results_arg.exists():
        sys.exit(f"{results_arg} not found. Run python execute_plans.py first.")
    main(plans_arg, results_arg, log_arg)
