import json
import os

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openrouter import ChatOpenRouter

from src.workflow.utils.models import State, TestPlan, TestPlans
from src.workflow.utils.prompts import PLANNER_SYSTEM_PROMPT, PLANNER_USER_PROMPT

load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

MODEL_NAME = "google/gemini-2.5-flash"

model = ChatOpenRouter(
    model=MODEL_NAME,
    temperature=0,
    max_tokens=4000,
)


def call_llm_1(state: State) -> dict:
    """Planner node: turn one or more OpenAPI operations into TestPlan lists."""
    test_generator = model.with_structured_output(TestPlans)
    operations = state["operations"]

    all_plans = []
    for operation in operations:
        op_payload = json.dumps(operation, ensure_ascii=False, default=str)
        user_content = PLANNER_USER_PROMPT.format(operation=op_payload)

        msg = test_generator.invoke(
            [SystemMessage(content=PLANNER_SYSTEM_PROMPT), HumanMessage(content=user_content)]
        )
        plans = msg.test_plans
        for plan in plans:
            plan.path = operation.get("path", plan.path)
            plan.method = operation.get("method", plan.method)
            all_plans.append(plan)

    return {"plans": all_plans}