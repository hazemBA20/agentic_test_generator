import importlib
from pathlib import Path

import pytest

from src.helpers.coverage import audit_operation, plans_for_operation, required_body_fields
from src.workflow.utils.models import ScenarioSpec, TestPlan as PlanModel


def _operation(**overrides):
    """A POST with a $ref'd body: one required field, one enum, one required query param."""
    wrapper = {
        "path": "/missions/{missionId}/documents",
        "method": "POST",
        "operation": {
            "parameters": [
                {"name": "missionId", "in": "path", "required": True, "schema": {"type": "string"}},
                {"name": "notify", "in": "query", "required": True, "schema": {"type": "boolean"}},
                {"name": "X-API-KEY", "in": "header", "required": True, "schema": {"type": "string"}},
            ],
            "requestBody": {
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Document"}}
                }
            },
            "responses": {
                "200": {"description": "Created"},
                "400": {"description": "Bad request"},
                "403": {"description": "Forbidden"},
                "409": {"description": "Mission already closed"},
                "default": {"description": "Unexpected"},
            },
        },
        "definitions": {
            "#/components/schemas/Document": {
                "type": "object",
                "required": ["expertCode", "documentType"],
                "properties": {
                    "expertCode": {"type": "string"},
                    "documentType": {"$ref": "#/components/schemas/DocType"},
                },
            },
            "#/components/schemas/DocType": {
                "type": "string",
                "enum": ["EXPERTISE_REPORT", "INVOICE"],
            },
        },
    }
    wrapper.update(overrides)
    return wrapper


def _plan(**overrides):
    plan = {
        "name": "test_add_document_success",
        "category": "happy_path",
        "method": "POST",
        "path": "/missions/{missionId}/documents",
        "request_body": {"expertCode": "E1", "documentType": "INVOICE"},
        "path_params": {"missionId": "M1"},
        "query_params": {"notify": True},
        "headers": {},
        "expected_status_code": 200,
    }
    plan.update(overrides)
    return plan


def _kinds(gaps):
    return {gap["kind"] for gap in gaps}


def _by_kind(gaps, kind):
    return [gap for gap in gaps if gap["kind"] == kind]


def test_uncovered_required_field_and_enum_are_reported_with_fillable_scenarios():
    report, gaps = audit_operation(_operation(), [_plan()])

    assert report["checklist"]["required_fields"]["missing"] == ["expertCode", "documentType"]
    assert report["checklist"]["enums"]["missing"] == ["documentType"]
    assert report["checklist"]["required_params"]["missing"] == ["notify (query)"]
    assert report["checklist"]["happy_path"] is True

    # Every gap the checklist can name a condition for carries a buildable scenario.
    for kind in ("required_field", "enum", "required_param"):
        for gap in _by_kind(gaps, kind):
            scenario = ScenarioSpec(**gap["scenario"])
            assert scenario.name.startswith("test_")
            assert scenario.category in {"negative", "boundary"}
            assert scenario.target_status_code == 400  # the documented 4xx


def test_complete_suite_reports_no_checklist_gaps():
    plans = [
        _plan(),
        _plan(
            name="test_missing_expert_code",
            category="negative",
            request_body={"documentType": "INVOICE"},
            expected_status_code=400,
        ),
        _plan(
            name="test_missing_document_type",
            category="negative",
            request_body={"expertCode": "E1"},
            expected_status_code=400,
        ),
        _plan(
            name="test_invalid_document_type",
            category="negative",
            request_body={"expertCode": "E1", "documentType": "NOPE"},
            expected_status_code=400,
        ),
        _plan(
            name="test_missing_notify",
            category="negative",
            request_body={"expertCode": "E1", "documentType": "INVOICE"},
            query_params={},
            expected_status_code=400,
        ),
        _plan(name="test_mission_closed", category="negative", expected_status_code=409),
    ]

    report, gaps = audit_operation(_operation(), plans)

    checklist = report["checklist"]
    assert checklist["required_fields"]["missing"] == []
    assert checklist["enums"]["missing"] == []
    assert checklist["required_params"]["missing"] == []
    assert checklist["status_codes"]["missing"] == []
    assert gaps == []


def test_auth_statuses_path_params_and_auth_headers_are_not_gaps():
    report, gaps = audit_operation(_operation(), [_plan()])

    checklist = report["checklist"]
    # 403 is documented but the planner is told not to test auth, and 'default'
    # is not a concrete code — neither belongs on the checklist at all.
    assert checklist["status_codes"] == {"covered": [200], "missing": [400, 409]}
    # Path params and the runner-injected API key never need a missing-param negative.
    params = checklist["required_params"]["covered"] + checklist["required_params"]["missing"]
    assert params == ["notify (query)"]


def test_untargeted_status_is_reported_without_a_synthesized_scenario():
    _, gaps = audit_operation(_operation(), [_plan()])

    status_gap = next(gap for gap in _by_kind(gaps, "status_code") if "409" in gap["detail"])
    assert "Mission already closed" in status_gap["detail"]
    # Only the operation's prose says what triggers a 409, so the LLM proposes it.
    assert status_gap["scenario"] is None


def test_status_a_synthesized_negative_already_targets_is_not_a_second_gap():
    """The missing-field negatives above all target 400, so 400 needs no scenario of its own."""
    report, gaps = audit_operation(_operation(), [_plan()])

    # The report still states honestly that the *current* suite misses 400...
    assert report["checklist"]["status_codes"]["missing"] == [400, 409]
    # ...but only 409 is handed to the model, so it can't invent a near-duplicate
    # of the missing-field negatives that already cover 400.
    assert [gap["status"] for gap in _by_kind(gaps, "status_code")] == [409]


def test_missing_happy_path_is_reported():
    _, gaps = audit_operation(
        _operation(), [_plan(category="negative", expected_status_code=400)]
    )

    assert "happy_path" in _kinds(gaps)


def test_multiple_simultaneously_omitted_fields_do_not_count_as_coverage():
    """A plan that drops both required fields tests something vaguer than either one."""
    plans = [_plan(), _plan(name="test_empty", category="negative", request_body={}, expected_status_code=400)]

    report, _ = audit_operation(_operation(), plans)

    assert report["checklist"]["required_fields"]["missing"] == ["expertCode", "documentType"]


def test_plans_are_matched_to_their_operation_by_method_and_path():
    wrapper = _operation()
    other = _plan(name="test_other", path="/health", method="GET")

    assert plans_for_operation(wrapper, [_plan(), other]) == [_plan()]


def test_postman_style_operation_without_schemas_degrades_to_status_coverage():
    """Postman input has no `required`/`enum`, so only status coverage is checkable."""
    wrapper = {
        "path": "/widgets",
        "method": "POST",
        "definitions": {},
        "operation": {
            "requestBody": {
                "content": {"application/json": {"schema": {"type": "object", "properties": {"a": {"type": "string"}}}}}
            },
            "responses": {"200": {"description": "OK"}},
        },
    }

    report, gaps = audit_operation(wrapper, [_plan(path="/widgets", request_body={"a": "x"})])

    assert report["checklist"]["required_fields"] == {"covered": [], "missing": []}
    assert report["checklist"]["enums"] == {"covered": [], "missing": []}
    assert gaps == []


# --- graph wiring ---------------------------------------------------------

@pytest.fixture
def graph(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    return importlib.import_module("workflow.graph")


def test_coverage_runs_once_then_render_falls_through(graph):
    assert graph._after_render({"coverage": True, "run_tests": True}) == "coverage"
    # After a fill pass, render is re-entered; coverage_done must not loop it back.
    assert graph._after_render({"coverage": True, "coverage_done": True, "run_tests": True}) == "execute"
    assert graph._after_render({"coverage": True, "coverage_done": True}) == "end"
    assert graph._after_render({"run_tests": True}) == "execute"


def test_coverage_routes_to_fill_only_when_a_gap_has_a_scenario(graph):
    scenario = ScenarioSpec(
        name="test_x", category="negative", description="d", target_status_code=400, focus="f"
    )
    assert graph._after_coverage({"coverage_gap_scenarios": [[scenario]]}) == "fill"
    # Gaps reported without scenarios are informational: nothing to rebuild.
    assert graph._after_coverage({"coverage_gap_scenarios": [[]], "run_tests": True}) == "execute"
    assert graph._after_coverage({"coverage_gap_scenarios": []}) == "end"


def test_fill_re_persists_only_when_plans_were_added(graph):
    assert graph._after_fill({"filled_count": 2}) == "persist"
    assert graph._after_fill({"filled_count": 0, "run_tests": True}) == "execute"
    assert graph._after_fill({"filled_count": 0}) == "end"


def test_fill_gaps_appends_plans_and_suffixes_colliding_names(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")

    existing = PlanModel(**_plan(description="d", category="happy_path"))
    built = [
        PlanModel(**_plan(name="test_add_document_success", description="d", category="negative")),
        PlanModel(**_plan(name="test_brand_new", description="d", category="negative")),
    ]
    monkeypatch.setattr(nodes, "build_plans_with_failures", lambda operations, scenarios: (built, 0))

    scenario = ScenarioSpec(
        name="test_x", category="negative", description="d", target_status_code=400, focus="f"
    )
    result = nodes.fill_gaps({
        "operations": [_operation()],
        "plans": [existing],
        "coverage_gap_scenarios": [[scenario]],
    })

    assert result["filled_count"] == 2
    names = [plan.name for plan in result["plans"]]
    assert names == ["test_add_document_success", "test_add_document_success_gap2", "test_brand_new"]


def test_fill_gaps_is_a_no_op_without_scenarios(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")

    def unexpected_build(*args, **kwargs):
        pytest.fail("the builder must not be called when there are no gap scenarios")

    monkeypatch.setattr(nodes, "build_plans_with_failures", unexpected_build)

    assert nodes.fill_gaps({"coverage_gap_scenarios": [[]]}) == {"filled_count": 0}


def test_a_failed_fill_batch_is_carried_forward_as_a_build_failure(monkeypatch):
    """A partial build is partial wherever it happened; persist_plans must hear about it."""
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")

    built = [PlanModel(**_plan(name="test_filled", description="d", category="negative"))]
    monkeypatch.setattr(nodes, "build_plans_with_failures", lambda operations, scenarios: (built, 1))

    scenario = ScenarioSpec(
        name="test_x", category="negative", description="d", target_status_code=400, focus="f"
    )
    result = nodes.fill_gaps({
        "operations": [_operation()],
        "plans": [],
        "coverage_gap_scenarios": [[scenario]],
        "build_failures": 2,
    })

    assert result["filled_count"] == 1
    assert result["build_failures"] == 3  # the builder's 2, plus this pass's 1


def test_coverage_audit_survives_an_llm_failure_and_keeps_the_checklist(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")

    async def failing_audit(*args, **kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(nodes, "_audit_batch", failing_audit)

    result = nodes.coverage_audit({"operations": [_operation()], "plans": [_plan()]})

    assert result["coverage_done"] is True
    gaps = result["coverage_report"][0]["gaps"]
    assert {"required_field", "enum", "required_param", "status_code"} <= _kinds(gaps)
    assert all(gap["source"] == "checklist" for gap in gaps)
    # The deterministic half still yields buildable scenarios without the model.
    assert any(scenarios for scenarios in result["coverage_gap_scenarios"])


def test_coverage_audit_merges_llm_gaps_and_marks_their_source(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")
    models = importlib.import_module("workflow.utils.models")

    async def fake_audit(wrapper, plans, report, unfilled):
        # The prose gap the deterministic checklist structurally cannot see.
        # Built from the module the node itself imported: pydantic rejects an
        # instance of the same class reached by a different import path.
        return models.CoverageGaps(gaps=[
            models.CoverageGap(
                kind="conditional",
                detail="documentType=INVOICE requires quoteId",
                scenario=models.ScenarioSpec(
                    name="test_invoice_without_quote_id",
                    category="negative",
                    description="Omit quoteId for an INVOICE document.",
                    target_status_code=400,
                    focus="INVOICE without quoteId",
                ),
            )
        ])

    monkeypatch.setattr(nodes, "_audit_batch", fake_audit)

    result = nodes.coverage_audit({"operations": [_operation()], "plans": [_plan()]})

    gaps = result["coverage_report"][0]["gaps"]
    conditional = _by_kind(gaps, "conditional")[0]
    assert conditional["source"] == "llm"
    assert any(gap["source"] == "checklist" for gap in gaps)
    names = [s.name for s in result["coverage_gap_scenarios"][0]]
    assert "test_invoice_without_quote_id" in names


def test_llm_answer_to_a_handed_over_gap_completes_it_instead_of_duplicating_it(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")
    models = importlib.import_module("workflow.utils.models")

    async def fake_audit(wrapper, plans, report, unfilled):
        # The checklist hands over the 409 with scenario=None; the model names the
        # condition. That is the same gap answered, not a new one.
        assert [gap["kind"] for gap in unfilled] == ["status_code"]
        return models.CoverageGaps(gaps=[
            models.CoverageGap(
                kind="status_code",
                detail="documented status 409 is never targeted (Mission already closed)",
                scenario=models.ScenarioSpec(
                    name="test_document_on_closed_mission",
                    category="negative",
                    description="Add a document to an already-closed mission.",
                    target_status_code=409,
                    focus="closed mission",
                ),
            )
        ])

    monkeypatch.setattr(nodes, "_audit_batch", fake_audit)

    result = nodes.coverage_audit({"operations": [_operation()], "plans": [_plan()]})

    status_gaps = _by_kind(result["coverage_report"][0]["gaps"], "status_code")
    assert len(status_gaps) == 1
    assert status_gaps[0]["source"] == "checklist+llm"
    assert status_gaps[0]["scenario"]["name"] == "test_document_on_closed_mission"


def test_llm_gaps_contradicting_the_checklist_are_ignored(monkeypatch):
    """A prompt is not enforcement: the checklist has the facts, so it wins."""
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")
    models = importlib.import_module("workflow.utils.models")

    async def fake_audit(wrapper, plans, report, unfilled):
        return models.CoverageGaps(gaps=[
            # 403 is documented but auth is deliberately out of scope.
            models.CoverageGap(kind="status_code", detail="403 Forbidden is not covered"),
            # 200 is already targeted by the happy path.
            models.CoverageGap(kind="status_code", detail="status 200 is not covered"),
            # The checklist already confirmed a happy path exists.
            models.CoverageGap(kind="happy_path", detail="no test sets convertToPdf=false"),
            # ...while a genuine prose gap still gets through.
            models.CoverageGap(kind="boundary", detail="empty data array is untested"),
        ])

    monkeypatch.setattr(nodes, "_audit_batch", fake_audit)

    result = nodes.coverage_audit({"operations": [_operation()], "plans": [_plan()]})
    gaps = result["coverage_report"][0]["gaps"]

    assert [gap["detail"] for gap in gaps if gap["source"] != "checklist"] == [
        "empty data array is untested"
    ]
    # The checklist's own 409 gap survives; the model's 403/200 claims do not.
    assert [gap["status"] for gap in _by_kind(gaps, "status_code")] == [409]
    assert not any(gap["source"] == "llm" for gap in _by_kind(gaps, "happy_path"))


def test_the_builder_can_run_twice_in_one_process(monkeypatch):
    """The fill pass calls build_plans after the builder node already did.

    asyncio.run() would close the loop the model clients bound their connection
    pool to, so the second call died with "Event loop is closed".
    """
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")

    async def fake_batch(operation, batch):
        # Touch both loop-bound primitives, as the real batch does.
        async with nodes._semaphore:
            await nodes._rate_limiter.acquire()
        return [PlanModel(**_plan(description="d"))]

    monkeypatch.setattr(nodes, "_build_batch", fake_batch)
    scenario = ScenarioSpec(
        name="test_x", category="negative", description="d", target_status_code=400, focus="f"
    )

    first = nodes.build_plans([_operation()], [[scenario]])
    second = nodes.build_plans([_operation()], [[scenario]])

    assert len(first) == 1 and len(second) == 1


# --- hollow-plan gate -----------------------------------------------------

def test_required_body_fields_resolves_through_a_ref():
    assert required_body_fields(_operation()) == ["expertCode", "documentType"]
    # An operation with no request body requires nothing, so nothing is hollow.
    assert required_body_fields({"operation": {"responses": {}}, "definitions": {}}) == []


def _built_names(monkeypatch, wrapper: dict, raw_plans: list[dict]) -> list[str]:
    """Run the real _build_batch over canned model output; return surviving names.

    Stubs the retry wrapper rather than the builder chain itself: the chain is a
    frozen pydantic Runnable, and this seam still exercises the backfill loop and
    the hollow-plan gate that follows it.
    """
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")

    class _Message:
        test_plans = [PlanModel(**plan) for plan in raw_plans]

    async def fake_call_with_retry(_invoke, *args, **kwargs):
        return _Message()

    monkeypatch.setattr(nodes, "_call_with_retry", fake_call_with_retry)
    scenario = ScenarioSpec(
        name="test_x", category="negative", description="d", target_status_code=400, focus="f"
    )
    return [plan.name for plan in nodes.build_plans([wrapper], [[scenario]])]


def test_a_plan_that_sends_no_body_is_dropped(monkeypatch):
    """A null body cannot test one missing field: the server rejects on the first."""
    hollow = _plan(name="test_missing_expert_code", category="negative", description="d")
    hollow.pop("request_body")  # the model omitted it -> None

    names = _built_names(monkeypatch, _operation(), [
        hollow,
        _plan(name="test_missing_expert_code_real", category="negative", description="d",
              request_body={"documentType": "INVOICE"}),
        _plan(name="test_empty_body", category="negative", description="d", request_body={}),
    ])

    assert "test_missing_expert_code" not in names      # null body -> dropped
    assert "test_missing_expert_code_real" in names     # omits exactly one field
    assert "test_empty_body" in names                   # explicit {} is deliberate


def test_a_null_body_is_kept_when_the_operation_requires_nothing(monkeypatch):
    bodyless = {"path": "/version", "method": "GET", "definitions": {},
                "operation": {"responses": {"200": {"description": "OK"}}}}
    plan = _plan(name="test_version", category="happy_path", description="d",
                 path="/version", method="GET", expected_status_code=200)
    plan.pop("request_body")

    assert _built_names(monkeypatch, bodyless, [plan]) == ["test_version"]
