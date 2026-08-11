from langgraph.graph import START, END, StateGraph

from src.helpers.parser import ingest_openapi_spec
from src.helpers.pretty_prints import pretty_print_test_plans
from src.run_graph import build_operation_payload
from src.workflow.utils.models import State
from src.workflow.utils.nodes import call_llm_1


def compile_workflow():
    """Assemble the planner workflow; no side effects on import."""
    graph_builder = StateGraph(State)
    graph_builder.add_node("planner", call_llm_1)
    graph_builder.add_edge(START, "planner")
    graph_builder.add_edge("planner", END)
    return graph_builder.compile()


if __name__ == "__main__":
    operations = ingest_openapi_spec("spec.json")
    operation = build_operation_payload(operations[0])

    Workflow = compile_workflow()
    state = Workflow.invoke({"operations": [operation]})
    pretty_print_test_plans(state["plans"])