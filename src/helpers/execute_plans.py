"""Run test_plans.json against the live API and print pass/fail.

This is the first step toward an execute/rewrite loop: see what actually
failed, as structured results. It does not rewrite anything.

Usage (from this directory):
    python execute_plans.py
    python execute_plans.py test_plans.json
    python execute_plans.py test_plans.json results.json
"""
import json
import sys
from pathlib import Path

try:  # Supports both `python execute_plans.py` and package imports by LangGraph.
    from ._test_support import send_request, assert_response
    from .plan_validation import validate_plans
except ImportError:  # pragma: no cover - direct-script compatibility
    from _test_support import send_request, assert_response
    from plan_validation import validate_plans

ROOT = Path(__file__).resolve().parent
BODY_SNIPPET = 800


def execute_plan(plan: dict, issues: list[str] | None = None) -> dict:
    name = plan.get("name", "unnamed")
    if issues:
        return {
            "name": name,
            "passed": False,
            "skipped": True,
            "kind": "invalid_plan",
            "error": "Invalid plan: " + "; ".join(issues),
        }
    expected_status = plan["expected_status_code"]
    try:
        resp = send_request(
            method=plan["method"],
            path=plan["path"],
            request_body=plan.get("request_body"),
            content_type=plan.get("content_type", "application/json"),
            path_params=plan.get("path_params") or {},
            query_params=plan.get("query_params") or {},
            headers=plan.get("headers") or {},
            requires_api_key=plan.get("requires_api_key", False),
            requires_jwt=plan.get("requires_jwt", False),
        )
    except Exception as e:
        return {
            "name": name,
            "passed": False,
            "skipped": False,
            "kind": "error",
            "error": str(e),
        }

    body_text = resp.text
    try:
        body_json = resp.json()
    except Exception:
        body_json = None

    if resp.status_code != expected_status:
        return {
            "name": name,
            "passed": False,
            "skipped": False,
            "kind": "status",
            "expected_status": expected_status,
            "status_code": resp.status_code,
            "body": body_json if body_json is not None else body_text[:BODY_SNIPPET],
        }

    expected_response = plan.get("expected_response")
    if expected_response is not None:
        try:
            assert_response(body_json, expected_response, context=f"{name}: ")
        except AssertionError as e:
            return {
                "name": name,
                "passed": False,
                "skipped": False,
                "kind": "body",
                "status_code": resp.status_code,
                "error": str(e),
                "body": body_json,
            }

    return {
        "name": name,
        "passed": True,
        "skipped": False,
        "kind": "pass",
        "status_code": resp.status_code,
    }


def execute_plans(plans: list[dict]) -> list[dict]:
    issues_by_plan = validate_plans(plans)
    results = []
    for index, (plan, issues) in enumerate(zip(plans, issues_by_plan)):
        if not isinstance(plan, dict):
            results.append({
                "name": f"plan at index {index}",
                "passed": False,
                "skipped": True,
                "kind": "invalid_plan",
                "error": "Invalid plan: " + "; ".join(issues),
            })
        else:
            results.append(execute_plan(plan, issues))
    return results


def main(plans_path: Path, out_path: Path | None):
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    if not plans:
        print(f"No plans in {plans_path}")
        return

    results = execute_plans(plans)
    passed = sum(1 for result in results if result["passed"])
    skipped = [result for result in results if result.get("skipped")]
    failed = [result for result in results if not result["passed"] and not result.get("skipped")]

    print(f"{passed} passed, {len(skipped)} skipped, {len(failed)} failed")
    for result in skipped:
        print(f"  SKIP [{result.get('kind')}] {result['name']}: {result.get('error')}")
    for result in failed:
        extra = result.get("error") or f"status {result.get('status_code')} (expected {result.get('expected_status')})"
        print(f"  FAIL [{result.get('kind')}] {result['name']}: {extra}")

    if out_path:
        out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {len(results)} results to {out_path}")


if __name__ == "__main__":
    plans_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test_plans.json"
    out_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "test_results.json"
    if not plans_arg.exists():
        sys.exit(f"{plans_arg} not found. Generate plans with main.py first.")
    main(plans_arg, out_arg)
