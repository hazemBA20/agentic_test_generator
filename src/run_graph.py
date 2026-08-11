import argparse
import json
import sys
from pathlib import Path

from langgraph.graph import START, END, StateGraph

from src.helpers.parser import ingest_openapi_spec
from src.helpers.pretty_prints import pretty_print_test_plans
from src.workflow.utils.models import State
from src.workflow.utils.nodes import call_llm_1

ROOT = Path(__file__).resolve().parent.parent


# def inline_refs(node, definitions):
#     """Return a plain-dict version of `node` with every JSON-local $ref
#     replaced by its (recursively resolved) definition."""
#     if isinstance(node, dict):
#         ref = node.get("$ref")
#         if isinstance(ref, str):
#             definition = definitions.get(ref)
#             if definition is not None:
#                 return inline_refs(definition, definitions)
#         return {k: inline_refs(v, definitions) for k, v in node.items()}
#     if isinstance(node, list):
#         return [inline_refs(item, definitions) for item in node]
#     return node


# def build_operation_payload(op):
#     """Combine a parser operation into one self-contained JSON document
#     that the planner LLM can reason over (refs inlined)."""
#     return {
#         key: inline_refs(value, op.get("definitions", {}))
#         for key, value in op.get("operation", {}).items()
#     } | {"path": op["path"], "method": op["method"]}


def build_graph():
    graph_builder = StateGraph(State)
    graph_builder.add_node("planner", call_llm_1)
    graph_builder.add_edge(START, "planner")
    graph_builder.add_edge("planner", END)
    return graph_builder.compile()


def main(spec_path, index):
    operations = ingest_openapi_spec(spec_path)
    if not operations:
        print("No operations found in spec.")
        sys.exit(1)
    op = operations[index]
    # payload = build_operation_payload(op)

    workflow = build_graph()
    state = workflow.invoke({"operations": [op]})
    # pretty_print_test_plans(state["plans"])
    for plan in state["plans"]:
        print(f"Scenario: {plan.description}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate test plans from an OpenAPI spec.")
    parser.add_argument("--spec", default=str(ROOT / "spec.json"), help="Path to OpenAPI spec")
    parser.add_argument("--index", type=int, default=0, help="Index of the operation to plan")
    args = parser.parse_args()
    main(args.spec, args.index)