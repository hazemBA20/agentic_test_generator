"""Run the complete OpenAPI test-generation workflow."""
import argparse
from pathlib import Path

from workflow.graph import compile_workflow


ROOT = Path(__file__).resolve().parent.parent
HELPERS = ROOT / "src" / "helpers"


def main(args):
    workflow = compile_workflow()
    state = workflow.invoke(
        {
            "spec_path": args.spec,
            "operation_index": args.index,
            "run_all": args.all,
            "plans_path": args.out,
            "tests_path": args.tests,
            "results_path": args.results,
            "review_log_path": args.review_log,
            "run_tests": args.run_tests or args.review,
            "review": args.review,
            "reviewed": False,
        }
    )
    print(f"Generated test suite: {state['tests_path']}")
    if args.run_tests or args.review:
        passed = sum(result["passed"] for result in state.get("results") or [])
        print(f"Final execution: {passed}/{len(state.get('results') or [])} passed")
    if args.review:
        print(f"Reviewer patches: {state.get('patched_count', 0)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate, execute, and optionally review OpenAPI API tests.")
    parser.add_argument("--spec", default=str(ROOT / "spec.json"), help="Path to OpenAPI spec")
    parser.add_argument("--index", type=int, default=0, help="Operation index (ignored with --all)")
    parser.add_argument("--all", action="store_true", help="Generate tests for every operation")
    parser.add_argument("--out", default=str(HELPERS / "test_plans.json"), help="Plan JSON output path")
    parser.add_argument("--tests", default=str(HELPERS / "test.py"), help="Generated pytest output path")
    parser.add_argument("--results", default=str(HELPERS / "test_results.json"), help="Execution result JSON path")
    parser.add_argument("--review-log", default=str(HELPERS / "rewrite_log.json"), help="Reviewer audit-log path")
    parser.add_argument("--run-tests", action="store_true", help="Execute generated plans against API_BASE_URL")
    parser.add_argument("--review", action="store_true", help="Run one LLM rewrite pass after execution failures")
    main(parser.parse_args())
