from pydantic import BaseModel, Field
from typing_extensions import TypedDict
from typing import Any, Literal


class TestPlan(BaseModel): 
    
    name: str = Field(..., description="Short snake_case test function name, e.g. 'test_mission_add_missing_expert_code'")
    description: str = Field(..., description="What this test verifies")
    category: Literal["happy_path", "negative", "boundary"] = Field(..., description="Type of test case")

    method: str = Field(..., description="HTTP method, e.g. 'POST'")
    path: str = Field(..., description="Endpoint path, e.g. '/experts/copilot/mission/add'")
    request_body: dict[str, Any] | None = Field(None, description="Request payload to send")

    expected_status_code: int = Field(..., description="Expected HTTP status code")
    expected_response: dict[str, Any] | None = Field(None, description="Expected response body or key fields to assert on")


    

class TestPlans(BaseModel):
    test_plans: list[TestPlan] = Field(
        None, description="A list of test plans generated for the given operation."
    )


class State(TypedDict):
    operation: str
    plans: TestPlans | None