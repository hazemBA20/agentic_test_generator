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
    name: str = Field(..., description="Short snake_case test function name")
    description: str = Field(..., description="What this test verifies")
    category: Literal["happy_path", "negative", "boundary"] = Field(..., description="Type of test case")
    method: str = Field(..., description="HTTP method, e.g. 'POST'")
    path: str = Field(..., description="Endpoint path, e.g. '/customers/{customerId}'")
    request_body: Any | None = Field(None, description="Request payload to send")
    path_params: dict[str, Any] = Field(default_factory=dict)
    query_params: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    expected_status_code: int = Field(..., description="Expected HTTP status code")
    expected_response: Any | None = Field(
        None, description="Expected response body or declarative response matcher"
    )
    missing_fixtures: list[str] = Field(
        default_factory=list,
        description="Suggested fixture keys required to make this plan executable",
    )

    # Backfilled deterministically from the operation rather than trusted to the LLM.
    requires_api_key: bool = False
    requires_jwt: bool = False
    content_type: str = Field("application/json", description="Request Content-Type")


class TestPlans(BaseModel):
    test_plans: list[TestPlan] = Field(
        default_factory=list, description="A list of test plans generated for the given operation."
    )


class CoverageGap(BaseModel):
    kind: Literal[
        "status_code", "required_field", "enum", "required_param",
        "happy_path", "conditional", "boundary",
    ] = Field(..., description="What kind of coverage is missing")
    detail: str = Field(..., description="One sentence naming what the suite does not cover")
    scenario: ScenarioSpec | None = Field(
        None, description="A scenario that would close this gap, or null if it cannot be tested"
    )


class CoverageGaps(BaseModel):
    gaps: list[CoverageGap] = Field(default_factory=list)


class State(TypedDict, total=False):
    operations: list[dict]
    scenarios: list[list[ScenarioSpec]]
    plans: list[TestPlan | dict[str, Any]]
    spec_path: str
    operation_index: int
    run_all: bool
    plans_path: str
    tests_path: str
    results_path: str
    review_log_path: str
    run_tests: bool
    review: bool
    reviewed: bool
    review_pass: int
    results: list[dict[str, Any]]
    review_log: list[dict[str, Any]]
    patched_count: int
    # Batches the builder lost to quota/timeouts. Non-zero means `plans` is
    # missing scenarios the planner asked for, so persisting it may destroy
    # working tests rather than replace them.
    build_failures: int
    # Failures the reviewer never actually saw because the model call errored.
    # Distinct from a reviewer judging a failure unfixable.
    review_errors: int
    coverage: bool
    coverage_done: bool
    coverage_report_path: str
    coverage_report: list[dict[str, Any]]
    coverage_gap_scenarios: list[list[ScenarioSpec]]
    filled_count: int
