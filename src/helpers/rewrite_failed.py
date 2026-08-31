"""Patch failed test plans using the live-run results from execute_plans.py.

Only body/status failures are sent to the LLM. Auth/server errors are skipped.
method, path, auth flags, content_type, and expected_status_code are never changed.

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

SYSTEM_PROMPT = """You repair a single generated API test plan that failed against a live server.

The plan's intent must stay the same: same category, same condition under test, same expected_status_code.

Decide:
- skip: the failure is not something a request/assertion tweak can honestly fix (auth, server bug, unreachable scenario, spec vs server disagreement you should not hide).
- patch: adjust request_body and/or expected_response so the plan still tests the same condition, but matches real server behavior.

Rules:
- Prefer fixing expected_response when the status code was already correct (kind=body). Use the literal string "<GENERATED>" for volatile or free-text fields. Only assert exact values the server actually returned and that the scenario cares about.
- Prefer fixing request_body when the status code was wrong (kind=status) and a different payload would realize the same scenario (missing field, invalid enum, etc.).
- Do not change expected_status_code.
- Do not invent new scenarios.
- Keep file-upload values as "<FILE:sample.pdf>" / "<FILE:sample.jpg>" when a binary field is required.
- If you are not confident, skip.
"""

USER_PROMPT = """Plan:
{plan}

Failure:
{failure}

Return skip or a patch with the full new request_body and expected_response values to store on the plan.
"""


class PlanPatch(BaseModel):
    action: Literal["skip", "patch"] = Field(..., description="skip if this failure should not be auto-fixed")
    reason: str = Field(..., description="One sentence why you patched or skipped")
    request_body: dict[str, Any] | None = Field(
        None, description="Full replacement request body when action=patch; null if the operation has no body"
    )
    expected_response: dict[str, Any] | None = Field(
        None, description="Full replacement response assertions when action=patch; null for status-only"
    )


def _rewriter():
    return ChatOpenRouter(
        model=os.getenv("REWRITE_MODEL", "deepseek/deepseek-v4-flash"),
        temperature=0,
        max_tokens=4000,
        reasoning={"effort": "low"},
    ).with_structured_output(PlanPatch)


def _should_rewrite(result: dict) -> str | None:
    """Return a skip reason, or None if the LLM should run."""
    if result.get("passed"):
        return "already passed"
    kind = result.get("kind")
    if kind not in REWRITEABLE:
        return f"kind={kind} is not auto-fixed"
    status = result.get("status_code")
    if status in INFRA_STATUSES:
        return f"HTTP {status} looks like auth/server, not a bad assertion"
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


def _apply(plan: dict, patch: PlanPatch) -> None:
    plan["request_body"] = patch.request_body
    plan["expected_response"] = patch.expected_response


def rewrite_plans(plans: list[dict], results: list[dict]) -> tuple[list[dict], int]:
    if len(plans) != len(results):
        raise SystemExit(
            f"plans ({len(plans)}) and results ({len(results)}) length mismatch — re-run execute_plans.py"
        )

    llm = _rewriter()
    patched = 0
    for plan, result in zip(plans, results):
        if result.get("name") not in (None, plan.get("name")):
            print(f"  skip name mismatch: plan={plan.get('name')} result={result.get('name')}")
            continue

        skip_reason = _should_rewrite(result)
        if skip_reason:
            print(f"  skip {plan.get('name')}: {skip_reason}")
            continue

        failure = dict(result)
        failure["body"] = _clip_body(result.get("body"))

        try:
            patch = llm.invoke(
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(
                        content=USER_PROMPT.format(
                            plan=json.dumps(plan, ensure_ascii=False, default=str),
                            failure=json.dumps(failure, ensure_ascii=False, default=str),
                        )
                    ),
                ]
            )
        except Exception as e:
            print(f"  skip {plan.get('name')}: LLM error ({e})")
            continue

        if patch.action != "patch":
            print(f"  skip {plan.get('name')}: {patch.reason}")
            continue

        _apply(plan, patch)
        patched += 1
        print(f"  patch {plan.get('name')}: {patch.reason}")

    return plans, patched


def main(plans_path: Path, results_path: Path):
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    results = json.loads(results_path.read_text(encoding="utf-8"))
    print(f"Rewriting failures from {results_path}...")
    plans, patched = rewrite_plans(plans, results)
    plans_path.write_text(json.dumps(plans, indent=2, default=str), encoding="utf-8")
    print(f"Patched {patched} plan(s). Wrote {plans_path}")
    print("Re-run: python execute_plans.py")


if __name__ == "__main__":
    plans_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test_plans.json"
    results_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "test_results.json"
    if not plans_arg.exists():
        sys.exit(f"{plans_arg} not found")
    if not results_arg.exists():
        sys.exit(f"{results_arg} not found. Run python execute_plans.py first.")
    main(plans_arg, results_arg)
