from utils.models import State , TestPlan, TestPlans


from dotenv import load_dotenv
import os

load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
from langchain_openrouter import ChatOpenRouter

model = ChatOpenRouter(
    model="google/gemini-2.5-flash",
    temperature=0,
    max_tokens=4000,
)

def call_llm_1(state: State):
    """First LLM call to generate initial joke"""

    test_generator = model.with_structured_output(TestPlans)
    msg = test_generator.invoke(
        f"Generate a list of 3 test plans for the following operation: {state['operation']}. Each test plan should include a name, description, category (happy_path, negative, or boundary), HTTP method, endpoint path, request body (if applicable), expected status code, and expected response."
    )
    return {"plans": msg.test_plans}