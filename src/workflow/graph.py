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
from workflow.utils.nodes import call_llm_1, call_llm_2


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HELPERS = ROOT / "src" / "helpers"


def _plan_dict(plan) -> dict:
    return plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)


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
    plans_path.write_text(
        json.dumps([_plan_dict(plan) for plan in state.get("plans") or []], indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(state.get('plans') or [])} test plan(s) to {plans_path}")
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
    print(
        f"Reviewer pass {review_pass}: {patched_count} patch(es), {skipped} skip(s). "
        f"Detailed log: {log_path}"
    )
    return {
        "plans": revised,
        "patched_count": patched_count,
        "review_log": all_entries,
        "review_log_path": str(log_path),
        "review_pass": review_pass,
        "reviewed": True,
    }


def _after_render(state: State) -> Literal["execute", "end"]:
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
    graph.add_node("execute", execute)
    graph.add_node("review", review_failures)
    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "planner")
    graph.add_edge("planner", "builder")
    graph.add_edge("builder", "persist_plans")
    graph.add_edge("persist_plans", "render")
    graph.add_conditional_edges("render", _after_render, {"execute": "execute", "end": END})
    graph.add_conditional_edges("execute", _after_execute, {"review": "review", "end": END})
    graph.add_conditional_edges("review", _after_review, {"persist": "persist_plans", "end": END})
    return graph.compile()
