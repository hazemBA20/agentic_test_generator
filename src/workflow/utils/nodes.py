import json
import os

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openrouter import ChatOpenRouter

from src.workflow.utils.models import State, TestPlan, TestPlans ,Scenarios
from src.workflow.utils.prompts import SCENARIO_PLANNER_SYSTEM_PROMPT, SCENARIO_PLANNER_USER_PROMPT

load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

MODEL_NAME = "google/gemini-2.5-flash"

model = ChatOpenRouter(
    model=MODEL_NAME,
    temperature=0,
    max_tokens=8000,
)


def call_llm_1(state: State) -> dict:
    """Planner node: turn one or more OpenAPI operations into TestPlan lists."""
    test_generator = model.with_structured_output(Scenarios)
    operations = state["operations"]
    

    all_plans = []
    for operation in operations:
        op_payload = json.dumps(operation, ensure_ascii=False, default=str)
        user_content = SCENARIO_PLANNER_USER_PROMPT.format(operation=op_payload)
        try :
            msg = test_generator.invoke(
                [SystemMessage(content=SCENARIO_PLANNER_SYSTEM_PROMPT), HumanMessage(content=user_content)]
            )
        except Exception as e:
            print(f"Error invoking LLM for operation {operation.get('path', '')}: {e}")
            continue
        # plans = msg.test_plans
        print
        plans = msg.scenarios
        all_plans=plans

        # print(plans)
        # for plan in plans:
        #     plan.path = operation.get("path", plan.path)
        #     plan.method = operation.get("method", plan.method)
        #     all_plans.append(plan)

    return {"plans": all_plans}
