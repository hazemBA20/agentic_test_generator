import argparse
import json
import sys
from pathlib import Path

from langgraph.graph import START, END, StateGraph

from helpers.parser import ingest_openapi_spec
from helpers.pretty_prints import pretty_print_test_plans
from workflow.utils.models import State
from workflow.utils.nodes import call_llm_1, call_llm_2

ROOT = Path(__file__).resolve().parent.parent


def build_graph():
    graph_builder = StateGraph(State)
    graph_builder.add_node("planner", call_llm_1)
    graph_builder.add_node("builder", call_llm_2)
    graph_builder.add_edge(START, "planner")
    graph_builder.add_edge("planner", "builder")
    graph_builder.add_edge("builder", END)
    return graph_builder.compile()


def run_one(workflow, op):
    state = workflow.invoke({"operations": [op]})
    return state.get("plans") or []


def main(spec_path, index, run_all, out_path):
    operations = ingest_openapi_spec(spec_path)
    if not operations:
        print("No operations found in spec.")
        sys.exit(1)

    workflow = build_graph()

    if run_all:
        all_plans = []
        for i, op in enumerate(operations):
            print(f"[{i + 1}/{len(operations)}] {op['method']} {op['path']}")
            plans = run_one(workflow, op)
            all_plans.extend(plans)
    else:
        all_plans = run_one(workflow, operations[index])
        for plan in all_plans:
            print(f"[{plan.category}] {plan.name} -> {plan.method} {plan.path} (expect {plan.expected_status_code}) ")

    if out_path:
        Path(out_path).write_text(
            json.dumps([p.model_dump() for p in all_plans], indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nWrote {len(all_plans)} test plans to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate test plans from an OpenAPI spec.")
    parser.add_argument("--spec", default=str(ROOT / "spec.json"), help="Path to OpenAPI spec")
    parser.add_argument("--index", type=int, default=0, help="Index of the operation to plan (ignored with --all)")
    parser.add_argument("--all", action="store_true", help="Run every operation in the spec, not just --index")
    parser.add_argument("--out", default=None, help="Path to write the combined test_plans.json")
    args = parser.parse_args()
    main(args.spec, args.index, args.all, args.out)