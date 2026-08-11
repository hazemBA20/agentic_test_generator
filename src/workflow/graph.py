from langchain.tools import tool
from langchain.chat_models import init_chat_model
from langchain.messages import AnyMessage
from langgraph.graph import START, StateGraph ,END
from typing_extensions import TypedDict, Annotated
import operator
from langchain.messages import SystemMessage
from utils.models import State, TestPlans
from utils.nodes import call_llm_1
from IPython.display import Image, display
import json
from src.helpers.pretty_prints import pretty_print_test_plans
operation = {
    "path": "/users/{id}",
    "method": "GET",
    "summary": "Get a user by ID",
    "description": "Returns a single user. Returns 404 if the user does not exist.",
    "parameters": [
        {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
    ],
    "responses": {
        "200": {"description": "User found", "content": {"application/json": {"example": {"id": 1, "name": "Alice", "email": "alice@example.com"}}}},
        "404": {"description": "User not found", "content": {"application/json": {"example": {"error": "USER_NOT_FOUND"}}}}
    }
}



graph_builder = StateGraph(State)


graph_builder.add_node("call_llm_1", call_llm_1)
graph_builder.add_edge(START, "call_llm_1")
graph_builder.add_edge("call_llm_1", END)
Workflow= graph_builder.compile()
display(Image(Workflow.get_graph().draw_mermaid_png()))

# Invoke
state = Workflow.invoke({"operation": json.dumps(operation)})

pretty_print_test_plans(state["plans"])
