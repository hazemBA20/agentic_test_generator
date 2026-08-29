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

from _test_support import send_request, assert_response

ROOT = Path(__file__).resolve().parent
BODY_SNIPPET = 800


def execute_plan(plan: dict) -> dict:
    name = plan.get("name", "unnamed")
    expected_status = plan["expected_status_code"]
    try:
        resp = send_request(
            method=plan["method"],
            path=plan["path"],
            request_body=plan.get("request_body"),
            content_type=plan.get("content_type", "application/json"),
            requires_api_key=bool(plan.get("requires_api_key", True)),
            requires_jwt=bool(plan.get("requires_jwt", True)),
        )
    except Exception as e:
        return {
            "name": name,
            "passed": False,
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
            "kind": "status",
            "expected_status": expected_status,
            "status_code": resp.status_code,
            "body": body_json if body_json is not None else body_text[:BODY_SNIPPET],
        }

    expected_response = plan.get("expected_response")
    if expected_response:
        try:
            assert_response(body_json, expected_response, context=f"{name}: ")
        except AssertionError as e:
            return {
                "name": name,
                "passed": False,
                "kind": "body",
                "status_code": resp.status_code,
                "error": str(e),
                "body": body_json,
            }

    return {
        "name": name,
        "passed": True,
        "kind": "pass",
        "status_code": resp.status_code,
    }


def execute_plans(plans: list[dict]) -> list[dict]:
    return [execute_plan(plan) for plan in plans]


def main(plans_path: Path, out_path: Path | None):
    plans = json.loads(plans_path.read_text(encoding="utf-8"))
    if not plans:
        print(f"No plans in {plans_path}")
        return

    results = execute_plans(plans)
    passed = sum(1 for r in results if r["passed"])
    failed = [r for r in results if not r["passed"]]

    print(f"{passed}/{len(results)} passed")
    for r in failed:
        extra = r.get("error") or f"status {r.get('status_code')} (expected {r.get('expected_status')})"
        print(f"  FAIL [{r.get('kind')}] {r['name']}: {extra}")

    if out_path:
        out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {len(results)} results to {out_path}")


if __name__ == "__main__":
    plans_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test_plans.json"
    out_arg = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "test_results.json"
    if not plans_arg.exists():
        sys.exit(f"{plans_arg} not found. Generate plans with main.py first.")
    main(plans_arg, out_arg)
