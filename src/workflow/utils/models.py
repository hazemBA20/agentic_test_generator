from pydantic import BaseModel, Field
from typing_extensions import TypedDict
from typing import Any, Literal

class ScenarioSpec(BaseModel):
    name: str = Field(..., description="Short snake_case identifier, prefixed with test_, e.g. 'test_mission_add_missing_expert_code'")
    category: Literal["happy_path", "negative", "boundary"] = Field(..., description="Type of test case")
    description: str = Field(..., description="One sentence stating what input condition is tested and why")
    target_status_code: int = Field(..., description="Expected HTTP status code, must be one documented on the operation")
    focus: str = Field(..., description="The specific field, param, or condition under test, e.g. 'missing expertCode'")


class Scenarios(BaseModel):
    scenarios: list[ScenarioSpec] = Field(default_factory=list)


class TestPlan(BaseModel):

    name: str = Field(..., description="Short snake_case test function name, e.g. 'test_mission_add_missing_expert_code'")
    description: str = Field(..., description="What this test verifies")
    category: Literal["happy_path", "negative", "boundary"] = Field(..., description="Type of test case")
    method: str = Field(..., description="HTTP method, e.g. 'POST'")
    path: str = Field(..., description="Endpoint path, e.g. '/experts/copilot/mission/add'")
    request_body: dict[str, Any] | None = Field(None, description="Request payload to send")
    path_params: dict[str, Any] = Field(default_factory=dict, description="Values substituted into {path} placeholders")
    query_params: dict[str, Any] = Field(default_factory=dict, description="Query-string parameter values")
    headers: dict[str, Any] = Field(default_factory=dict, description="Operation-specific request headers; excludes X-API-KEY")

    expected_status_code: int = Field(..., description="Expected HTTP status code")
    expected_response: dict[str, Any] | None = Field(None, description="Expected response body or key fields to assert on")

    # The demo runner always attaches X-API-KEY.  Authentication is deliberately
    # kept out of generated plans so every plan has the same execution contract.
    content_type: Literal["application/json", "multipart/form-data"] = Field(
        "application/json", description="Content-Type to send the request_body as"
    )


class TestPlans(BaseModel):
    test_plans: list[TestPlan] = Field(
        default_factory=list, description="A list of test plans generated for the given operation."
    )


class State(TypedDict):
    spec_path: str | None
    operation_index: int | None
    run_all: bool | None
    plans_path: str | None
    tests_path: str | None
    results_path: str | None
    review_log_path: str | None
    run_tests: bool | None
    review: bool | None
    reviewed: bool | None
    review_pass: int | None
    operations: list[dict] | None
    scenarios: list[list[ScenarioSpec]] | None  # one sub-list per operation, aligned by index
    plans: list[TestPlan] | list[dict] | None
    results: list[dict] | None
    patched_count: int | None
    review_log: list[dict] | None
