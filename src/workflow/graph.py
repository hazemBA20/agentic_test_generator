"""The complete OpenAPI-to-reviewed-tests LangGraph workflow."""
import json
from pathlib import Path
from typing import Literal

from langgraph.graph import END, START, StateGraph

from helpers.execute_plans import execute_plans
from helpers.generate_test_file import generate
from helpers.parser import ingest_openapi_spec
from helpers.rewrite_failed import rewrite_plans
from workflow.utils.models import State
from workflow.utils.nodes import call_llm_1, call_llm_2, coverage_audit, fill_gaps


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HELPERS = ROOT / "src" / "helpers"


def _plan_dict(plan) -> dict:
    return plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)


class PartialBuildError(RuntimeError):
    """Raised when persisting a partial build would shrink an existing suite."""


def _existing_plan_count(plans_path: Path) -> int:
    """How many plans the file we are about to replace already holds."""
    try:
        existing = json.loads(plans_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return len(existing) if isinstance(existing, list) else 0


def ingest(state: State) -> dict:
    """Load the requested OpenAPI operation(s) into the graph state."""
    spec_path = state.get("spec_path") or str(ROOT / "spec.json")
    operations = ingest_openapi_spec(spec_path)
    if not operations:
        raise ValueError(f"No HTTP operations found in {spec_path}")
    if not state.get("run_all"):
        index = state.get("operation_index") or 0
        if index < 0 or index >= len(operations):
            raise IndexError(f"Operation index {index} is outside 0..{len(operations) - 1}")
        operations = [operations[index]]
    print(f"Ingested {len(operations)} operation(s) from {spec_path}")
    return {"operations": operations}


def persist_plans(state: State) -> dict:
    """Persist plans as JSON; this is the durable source for generated tests."""
    plans_path = Path(state.get("plans_path") or DEFAULT_HELPERS / "test_plans.json")
    plans_path.parent.mkdir(parents=True, exist_ok=True)
    plans = [_plan_dict(plan) for plan in state.get("plans") or []]
    payload = json.dumps(plans, indent=2)

    # A batch the builder lost to quota or a timeout takes its scenarios with it.
    # Writing what survived over a larger previous suite deletes working tests to
    # no benefit and exits 0, so the run looks fine while the suite silently
    # shrank. Quarantine the partial result and stop instead.
    existing_count = _existing_plan_count(plans_path)
    if state.get("build_failures") and existing_count > len(plans):
        partial_path = plans_path.with_name(f"{plans_path.stem}.partial.json")
        partial_path.write_text(payload, encoding="utf-8")
        raise PartialBuildError(
            f"The builder dropped {state['build_failures']} batch(es): only {len(plans)} "
            f"plan(s) survived, fewer than the {existing_count} already in {plans_path}. "
            f"Refusing to overwrite a larger suite with a partial one.\n"
            f"The partial result is in {partial_path}. {plans_path} and the rendered "
            f"suite are untouched — re-run once the model is reachable again."
        )

    plans_path.write_text(payload, encoding="utf-8")
    print(f"Wrote {len(plans)} test plan(s) to {plans_path}")
    return {"plans_path": str(plans_path)}


def render(state: State) -> dict:
    plans_path = Path(state.get("plans_path") or DEFAULT_HELPERS / "test_plans.json")
    tests_path = Path(state.get("tests_path") or DEFAULT_HELPERS / "test.py")
    tests_path.parent.mkdir(parents=True, exist_ok=True)
    generate(plans_path, tests_path)
    return {"tests_path": str(tests_path)}


def execute(state: State) -> dict:
    """Execute plans directly and retain concise JSON results for the reviewer."""
    results = execute_plans([_plan_dict(plan) for plan in state.get("plans") or []])
    results_path = Path(state.get("results_path") or DEFAULT_HELPERS / "test_results.json")
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    passed = sum(bool(result.get("passed")) for result in results)
    skipped = sum(bool(result.get("skipped")) for result in results)
    failed = len(results) - passed - skipped
    print(
        f"Execution complete: {passed} passed, {skipped} skipped, {failed} failed. "
        f"Results: {results_path}"
    )

    # The first execution supplies evidence to the reviewer. Executions after a
    # review pass verify every recorded patch in the same audit log.
    review_log = state.get("review_log") or []
    review_pass = state.get("review_pass") or 0
    if review_log and review_pass:
        results_by_name = {result.get("name"): result for result in results}
        patched_entries = [
            entry
            for entry in review_log
            if entry.get("review_pass") == review_pass and entry.get("action") == "patch"
        ]
        for entry in patched_entries:
            entry["verification"] = results_by_name.get(entry.get("name"))

        log_path = Path(state.get("review_log_path") or DEFAULT_HELPERS / "rewrite_log.json")
        log_path.write_text(json.dumps(review_log, indent=2, default=str), encoding="utf-8")
        verified = sum(bool((entry.get("verification") or {}).get("passed")) for entry in patched_entries)
        print(f"Reviewer pass {review_pass} verification: {verified}/{len(patched_entries)} patched test(s) passed.")

    return {"results": results, "results_path": str(results_path), "review_log": review_log}


def review_failures(state: State) -> dict:
    """Perform exactly one constrained plan-rewrite pass."""
    plans = [_plan_dict(plan) for plan in state.get("plans") or []]
    revised, patched_count, review_log = rewrite_plans(plans, state.get("results") or [])
    review_pass = (state.get("review_pass") or 0) + 1
    for entry in review_log:
        entry["review_pass"] = review_pass

    all_entries = [*(state.get("review_log") or []), *review_log]
    log_path = Path(state.get("review_log_path") or DEFAULT_HELPERS / "rewrite_log.json")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(all_entries, indent=2, default=str), encoding="utf-8")
    skipped = sum(entry.get("action") == "skip" for entry in review_log)
    # A skip because the model call failed is not the reviewer deciding a failure
    # is unfixable — it never saw the failure at all. Reporting both as "skip"
    # makes an unreachable model look like a clean bill of health. Counted over
    # the cumulative log so errors from earlier passes are not re-reported as
    # resolved by a later clean pass.
    errored = sum(entry.get("source") == "error" for entry in all_entries)
    summary = f"Reviewer pass {review_pass}: {patched_count} patch(es), {skipped} skip(s)"
    if errored:
        summary += f", of which {errored} are model errors rather than judgments"
    print(f"{summary}. Detailed log: {log_path}")
    return {
        "plans": revised,
        "patched_count": patched_count,
        "review_log": all_entries,
        "review_log_path": str(log_path),
        "review_pass": review_pass,
        "review_errors": errored,
        "reviewed": True,
    }


def coverage(state: State) -> dict:
    """Audit the rendered suite against the spec and persist the report."""
    result = coverage_audit(state)
    report_path = Path(
        state.get("coverage_report_path") or DEFAULT_HELPERS / "coverage_report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(result.get("coverage_report") or [], indent=2, default=str), encoding="utf-8"
    )
    print(f"Coverage report: {report_path}")
    return {**result, "coverage_report_path": str(report_path)}


def _after_render(state: State) -> Literal["coverage", "execute", "end"]:
    if state.get("coverage") and not state.get("coverage_done"):
        return "coverage"
    return "execute" if state.get("run_tests") else "end"


def _after_coverage(state: State) -> Literal["fill", "execute", "end"]:
    has_fillable = any(
        scenarios for scenarios in state.get("coverage_gap_scenarios") or []
    )
    if has_fillable:
        return "fill"
    return "execute" if state.get("run_tests") else "end"


def _after_fill(state: State) -> Literal["persist", "execute", "end"]:
    if state.get("filled_count", 0) > 0:
        return "persist"
    return "execute" if state.get("run_tests") else "end"


def _after_execute(state: State) -> Literal["review", "end"]:
    has_failures = any(
        not result.get("passed") and not result.get("skipped")
        for result in state.get("results") or []
    )
    if state.get("review") and not state.get("reviewed") and has_failures:
        return "review"
    return "end"


def _after_review(state: State) -> Literal["persist", "end"]:
    return "persist" if state.get("patched_count", 0) > 0 else "end"


def compile_workflow():
    """Build the end-to-end workflow without performing I/O on import."""
    graph = StateGraph(State)
    graph.add_node("ingest", ingest)
    graph.add_node("planner", call_llm_1)
    graph.add_node("builder", call_llm_2)
    graph.add_node("persist_plans", persist_plans)
    graph.add_node("render", render)
    graph.add_node("coverage", coverage)
    graph.add_node("fill_gaps", fill_gaps)
    graph.add_node("execute", execute)
    graph.add_node("review", review_failures)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "planner")
    graph.add_edge("planner", "builder")
    graph.add_edge("builder", "persist_plans")
    graph.add_edge("persist_plans", "render")
    graph.add_conditional_edges(
        "render", _after_render,
        {"coverage": "coverage", "execute": "execute", "end": END},
    )
    graph.add_conditional_edges(
        "coverage", _after_coverage,
        {"fill": "fill_gaps", "execute": "execute", "end": END},
    )
    graph.add_conditional_edges(
        "fill_gaps", _after_fill,
        {"persist": "persist_plans", "execute": "execute", "end": END},
    )
    graph.add_conditional_edges("execute", _after_execute, {"review": "review", "end": END})
    graph.add_conditional_edges("review", _after_review, {"persist": "persist_plans", "end": END})
    return graph.compile()
