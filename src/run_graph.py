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


def main(spec_path, index):
    operations = ingest_openapi_spec(spec_path)
    if not operations:
        print("No operations found in spec.")
        sys.exit(1)
    op = operations[index]

    workflow = build_graph()
    state = workflow.invoke({"operations": [op]})
    for plan in state["plans"]:
        print(f"[{plan.category}] {plan.name} -> {plan.method} {plan.path} (expect {plan.expected_status_code}) with {plan.request_body or 'no body'} -> expect {plan.expected_response or 'no response body'}  ")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate test plans from an OpenAPI spec.")
    parser.add_argument("--spec", default=str(ROOT / "spec.json"), help="Path to OpenAPI spec")
    parser.add_argument("--index", type=int, default=0, help="Index of the operation to plan")
    args = parser.parse_args()
    main(args.spec, args.index)