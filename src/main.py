"""Run the complete OpenAPI test-generation workflow."""
import argparse
import sys
from pathlib import Path

from workflow.graph import PartialBuildError, compile_workflow


ROOT = Path(__file__).resolve().parent.parent
HELPERS = ROOT / "src" / "helpers"


def main(args) -> int:
    workflow = compile_workflow()
    try:
        state = workflow.invoke(
            {
                "spec_path": args.spec,
                "operation_index": args.index,
                "run_all": args.all,
                "plans_path": args.out,
                "tests_path": args.tests,
                "results_path": args.results,
                "review_log_path": args.review_log,
                "coverage_report_path": args.coverage_report,
                "run_tests": args.run_tests or args.review,
                "review": args.review,
                "reviewed": False,
                "review_pass": 0,
                "coverage": args.coverage,
                "coverage_done": False,
            }
        )
    except PartialBuildError as e:
        print(f"ERROR: {e}")
        return 2

    exit_code = 0
    print(f"Generated test suite: {state['tests_path']}")
    if state.get("build_failures"):
        print(
            f"WARNING: {state['build_failures']} builder batch(es) failed, so this suite is "
            "missing scenarios the planner asked for. Re-run to rebuild them."
        )
        exit_code = 2
    if args.coverage:
        gaps = sum(len(entry.get("gaps") or []) for entry in state.get("coverage_report") or [])
        print(
            f"Coverage: {gaps} gap(s) reported, {state.get('filled_count', 0)} filled. "
            f"Report: {state.get('coverage_report_path')}"
        )
    if args.run_tests or args.review:
        results = state.get("results") or []
        passed = sum(bool(result.get("passed")) for result in results)
        skipped = sum(bool(result.get("skipped")) for result in results)
        failed = len(results) - passed - skipped
        print(f"Final execution: {passed} passed, {skipped} skipped, {failed} failed")
    if args.review:
        print(f"Reviewer patches: {state.get('patched_count', 0)}")
        errors = state.get("review_errors") or 0
        if errors:
            print(
                f"WARNING: the reviewer never reached the model for {errors} failure(s) — "
                "they are logged as skips but were not reviewed. Check the error text in "
                f"{state.get('review_log_path')} before trusting this run."
            )
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate, execute, and optionally review OpenAPI API tests.")
    parser.add_argument("--spec", default=str(ROOT / "spec.json"), help="Path to OpenAPI spec")
    parser.add_argument("--index", type=int, default=0, help="Operation index (ignored with --all)")
    parser.add_argument("--all", action="store_true", help="Generate tests for every operation")
    parser.add_argument("--out", default=str(HELPERS / "test_plans.json"), help="Plan JSON output path")
    parser.add_argument("--tests", default=str(HELPERS / "test.py"), help="Generated pytest output path")
    parser.add_argument("--results", default=str(HELPERS / "test_results.json"), help="Execution result JSON path")
    parser.add_argument("--review-log", default=str(HELPERS / "rewrite_log.json"), help="Reviewer audit-log path")
    parser.add_argument("--coverage-report", default=str(HELPERS / "coverage_report.json"), help="Coverage report path")
    parser.add_argument("--run-tests", action="store_true", help="Execute generated plans against API_BASE_URL")
    parser.add_argument("--review", action="store_true", help="Run one LLM rewrite pass after execution failures")
    parser.add_argument("--coverage", action="store_true", help="Audit generated coverage and fill gaps in one pass")
    sys.exit(main(parser.parse_args()))
