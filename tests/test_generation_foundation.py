import importlib
import json
from pathlib import Path

import pytest

from src.helpers import _test_support
from src.helpers.generate_test_file import generate
from src.helpers.plan_validation import validate_plans
from src.helpers.rewrite_failed import _apply, NamedPatch
from src.workflow.utils.models import ScenarioSpec, State, TestPlan as PlanModel

execute_module = importlib.import_module("src.helpers.execute_plans")


def _plan(**overrides):
    plan = {
        "name": "test_get_customer",
        "description": "Gets one customer",
        "category": "happy_path",
        "method": "GET",
        "path": "/customers/{customerId}",
        "request_body": None,
        "path_params": {"customerId": "<FIXTURE:customer_id>"},
        "query_params": {},
        "headers": {},
        "expected_status_code": 200,
        "expected_response": {"id": "<NON_NULL>"},
        "requires_api_key": False,
        "requires_jwt": False,
        "missing_fixtures": [],
    }
    plan.update(overrides)
    return plan


def test_state_workflow_fields_are_optional():
    assert {"run_tests", "review", "tests_path", "results"} <= State.__optional_keys__
    assert {"build_failures", "review_errors"} <= State.__optional_keys__


def test_a_partial_build_never_overwrites_a_larger_suite_on_disk(tmp_path, monkeypatch):
    """A builder batch lost to quota must not shrink the persisted plan file."""
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    graph = importlib.import_module("workflow.graph")

    plans_path = tmp_path / "test_plans.json"
    plans_path.write_text(json.dumps([{"a": 1}, {"b": 2}, {"c": 3}]), encoding="utf-8")

    with pytest.raises(graph.PartialBuildError, match="Refusing to overwrite"):
        graph.persist_plans({
            "plans": [{"d": 4}],
            "build_failures": 1,
            "plans_path": str(plans_path),
        })

    # The previous suite survives and the partial build is quarantined next to it.
    assert json.loads(plans_path.read_text(encoding="utf-8")) == [{"a": 1}, {"b": 2}, {"c": 3}]
    partial = json.loads((tmp_path / "test_plans.partial.json").read_text(encoding="utf-8"))
    assert partial == [{"d": 4}]


def test_a_full_build_or_first_run_still_persists_normally(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    graph = importlib.import_module("workflow.graph")

    plans_path = tmp_path / "test_plans.json"
    plans_path.write_text(json.dumps([{"a": 1}]), encoding="utf-8")
    # Same number of plans (full rebuild) and failure-count zero both write fine.
    result = graph.persist_plans({
        "plans": [{"b": 2}], "build_failures": 1, "plans_path": str(plans_path),
    })
    assert json.loads(plans_path.read_text(encoding="utf-8")) == [{"b": 2}]
    assert result["plans_path"] == str(plans_path)

    first_run = tmp_path / "fresh.json"
    graph.persist_plans({"plans": [{"c": 3}], "build_failures": 1, "plans_path": str(first_run)})
    assert json.loads(first_run.read_text(encoding="utf-8")) == [{"c": 3}]
    assert not (tmp_path / "fresh.partial.json").exists()


def test_review_errors_are_distinguished_from_judgment_skips(monkeypatch):
    """An error-skip is not the reviewer deciding the failure is unfixable."""
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    graph = importlib.import_module("workflow.graph")

    state = graph.review_failures({
        "plans": [],
        "results": [],
        "review_log": [
            {"name": "x", "action": "skip", "source": "error", "reason": "LLM error (quota)"},
            {"name": "y", "action": "skip", "source": "llm", "reason": "auth out of scope"},
        ],
    })
    assert state["review_errors"] == 1
    assert state["reviewed"] is True
    assert state["review_pass"] == 1


def test_review_routing_ignores_skips_and_stops_without_patches(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    graph = importlib.import_module("workflow.graph")

    review_state = {"review": True, "reviewed": False}
    assert graph._after_execute({
        **review_state,
        "results": [{"passed": False, "skipped": True, "kind": "invalid_plan"}],
    }) == "end"
    assert graph._after_execute({
        **review_state,
        "results": [{"passed": False, "skipped": False, "kind": "status"}],
    }) == "review"
    assert graph._after_review({"patched_count": 0}) == "end"
    assert graph._after_review({"patched_count": 1}) == "persist"


def test_a_planner_failure_is_counted_as_a_build_failure(monkeypatch):
    """A lost planner call must not silently shrink the persisted suite either."""
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")

    async def failing_plan(operation):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(nodes, "_plan_operation", failing_plan)

    state = nodes.call_llm_1({"operations": [{"path": "/x", "method": "GET", "operation": {}}]})

    assert state["scenarios"] == [[]]
    assert state["build_failures"] == 1


def test_the_builder_accumulates_rather_than_overwrites_build_failures(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")

    scenario = ScenarioSpec(
        name="test_x", category="negative", description="d", target_status_code=400, focus="f"
    )
    monkeypatch.setattr(
        nodes, "build_plans_with_failures", lambda operations, scenarios: ([], 1)
    )

    state = nodes.call_llm_2({
        "operations": [{"path": "/x", "method": "GET", "operation": {}}],
        "scenarios": [[scenario]],
        "build_failures": 2,
    })
    assert state["build_failures"] == 3  # the planner's 2, plus this batch's 1

    # An empty scenario state keeps earlier losses instead of resetting to zero.
    early = nodes.call_llm_2({"operations": [], "scenarios": [[]], "build_failures": 2})
    assert early["build_failures"] == 2


def test_plan_contract_supports_all_request_locations_and_boundary_category():
    plan = PlanModel(**_plan(category="boundary"))

    assert plan.category == "boundary"
    assert plan.path_params == {"customerId": "<FIXTURE:customer_id>"}
    assert plan.expected_response == {"id": "<NON_NULL>"}


def test_preflight_accepts_known_fixture(tmp_path: Path):
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"customer_id": 42}), encoding="utf-8")

    validate_plans([_plan()], fixtures)


def test_unknown_referenced_fixture_is_quarantined_without_transport(tmp_path: Path, monkeypatch):
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text("{}", encoding="utf-8")
    unknown_key = "definitely_missing_fixture_for_test"
    plan = _plan(path_params={"customerId": f"<FIXTURE:{unknown_key}>"})

    with pytest.warns(UserWarning, match=rf"undefined fixture\(s\): {unknown_key}"):
        issues = validate_plans([plan], fixtures)
    assert len(issues) == 1
    assert len(issues[0]) == 1

    plans_path = tmp_path / "plans.json"
    output_path = tmp_path / "test_generated.py"
    plans_path.write_text(json.dumps([plan]), encoding="utf-8")
    with pytest.warns(UserWarning, match=rf"undefined fixture\(s\): {unknown_key}"):
        generate(plans_path, output_path)

    generated = output_path.read_text(encoding="utf-8")
    # The rendered file must be importable Python — a template whitespace bug
    # once emitted body lines at column 0, which only surfaced at runtime.
    compile(generated, str(output_path), "exec")
    function_source = generated.split("def test_get_customer():", 1)[1]
    assert "pytest.skip(" in function_source
    assert unknown_key in function_source
    assert "send_request" not in function_source

    def unexpected_request(**kwargs):
        pytest.fail(f"transport must not be called for an invalid plan: {kwargs}")

    monkeypatch.setattr(execute_module, "send_request", unexpected_request)
    with pytest.warns(UserWarning, match=rf"undefined fixture\(s\): {unknown_key}"):
        results = execute_module.execute_plans([plan])

    assert len(results) == 1
    assert results[0]["name"] == "test_get_customer"
    assert results[0]["passed"] is False
    assert results[0]["skipped"] is True
    assert results[0]["kind"] == "invalid_plan"
    assert unknown_key in results[0]["error"]


def test_malformed_fixture_json_quarantines_referencing_plan(tmp_path: Path):
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text("{not-json", encoding="utf-8")

    with pytest.warns(UserWarning, match="could not read fixture data file"):
        issues = validate_plans([_plan()], fixtures)

    assert len(issues) == 1
    assert "cannot validate referenced fixture(s) customer_id" in issues[0][0]
    assert str(fixtures) in issues[0][0]


def test_preflight_warns_for_advisory_missing_fixture(tmp_path: Path):
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text("{}", encoding="utf-8")
    plan = _plan(
        path="/health",
        path_params={},
        missing_fixtures=["expert_with_active_mission"],
    )

    warning = rf"test_get_customer.*expert_with_active_mission.*{fixtures.name}"
    with pytest.warns(UserWarning, match=warning):
        validate_plans([plan], fixtures)


def test_response_matchers_support_presence_types_and_flexible_arrays():
    _test_support.assert_response(
        {
            "optional": None,
            "id": "abc-123",
            "items": [{"id": 1}, {"id": 2}],
        },
        {
            "optional": "<PRESENT>",
            "id": "<ANY_STRING>",
            "items": {"$array": {"min_items": 1, "contains": {"id": 2}}},
        },
    )

    with pytest.raises(AssertionError, match="non-null"):
        _test_support.assert_response({"id": None}, {"id": "<NON_NULL>"})


def test_generator_keeps_advisory_plan_and_empty_response_assertion(tmp_path: Path, monkeypatch):
    plans_path = tmp_path / "plans.json"
    output_path = tmp_path / "test_generated.py"
    plan = _plan(
        path="/missions",
        path_params={},
        request_body={"expertId": "fallback-expert"},
        expected_response={},
        missing_fixtures=["expert_with_active_mission"],
    )
    plans_path.write_text(json.dumps([plan]), encoding="utf-8")

    with pytest.warns(UserWarning, match="expert_with_active_mission"):
        generate(plans_path, output_path)

    generated = output_path.read_text(encoding="utf-8")
    compile(generated, str(output_path), "exec")
    assert "def test_get_customer():" in generated
    assert "'expertId': 'fallback-expert'" in generated
    assert "assert_response(resp.json(), {}" in generated
    assert "requires_api_key=False" in generated
    assert "pytest.skip(" not in generated

    captured = {}

    class Response:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(execute_module, "send_request", fake_request)
    with pytest.warns(UserWarning, match="expert_with_active_mission"):
        results = execute_module.execute_plans([plan])

    assert results[0]["passed"] is True
    assert results[0]["skipped"] is False
    assert captured["request_body"] == {"expertId": "fallback-expert"}


def test_extension_only_file_sentinel_selects_matching_fixture():
    filename, path = _test_support._resolve_fixture("<FILE:pdf>")

    assert filename == "sample.pdf"
    assert path.name == "sample.pdf"


def test_a_status_patch_with_a_response_sentinel_in_a_request_field_is_refused():
    """A live run saw <GENERATED> patched into request_body — the runner would
    have sent that literal string to the server."""
    plan = _plan()

    reason = _apply(
        plan,
        NamedPatch(
            name="test_get_customer",
            action="patch",
            reason="swap the plate",
            request_body={"registrationNumber": "<GENERATED>"},
        ),
        kind="status",
    )

    assert reason and "sentinel" in reason
    assert plan["request_body"] is None  # untouched

    # Nested occurrences are caught too, and legit request sentinels stay fine.
    plan2 = _plan()
    reason2 = _apply(
        plan2,
        NamedPatch(name="t", action="patch", reason="r",
                   request_body={"items": [{"plate": "<ANY_STRING>"}]}),
        kind="status",
    )
    assert reason2 and "sentinel" in reason2
    assert plan2["request_body"] is None

    plan3 = _plan()
    reason3 = _apply(
        plan3,
        NamedPatch(name="t", action="patch", reason="r",
                   request_body={"note": "<FIXTURE:expert_code>", "doc": "<FILE:pdf>"}),
        kind="status",
    )
    assert reason3 is None
    assert plan3["request_body"] == {"note": "<FIXTURE:expert_code>", "doc": "<FILE:pdf>"}


def test_public_request_does_not_require_credentials(monkeypatch):
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(_test_support.requests, "request", fake_request)
    monkeypatch.delenv("DIGIEXPERT_API_KEY", raising=False)
    monkeypatch.setenv("API_BASE_URL", "https://example.test/api")

    _test_support.send_request("GET", "/health", None, "application/json")

    assert captured["headers"] == {}


def test_request_without_a_base_url_is_refused(monkeypatch):
    """No silent fallback: an unset target must stop the suite, not pick one."""
    monkeypatch.delenv("API_BASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="API_BASE_URL"):
        _test_support.send_request("GET", "/health", None, "application/json")


def test_bearer_and_api_key_are_attached_per_plan(monkeypatch):
    """requires_jwt is honored now: bearer from env, key under the plan's header."""
    from src.helpers import auth as auth_module

    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(_test_support.requests, "request", fake_request)
    monkeypatch.setenv("API_BASE_URL", "https://example.test/api")
    monkeypatch.setenv("DIGIEXPERT_API_KEY", "company-api-key")
    monkeypatch.setenv("AUTH_TOKEN", "static-token")
    monkeypatch.delenv("API_JWT", raising=False)
    monkeypatch.delenv("DIGIEXPERT_JWT", raising=False)
    monkeypatch.setattr(auth_module, "_cached_bearer", None)

    _test_support.send_request(
        "GET",
        "/private",
        None,
        "application/json",
        requires_api_key=True,
        requires_jwt=True,
        api_key_header="X-Api-Key",
    )

    assert captured["headers"] == {
        "X-Api-Key": "company-api-key",
        "Authorization": "Bearer static-token",
    }


def test_protected_request_reports_missing_credential(monkeypatch):
    monkeypatch.delenv("DIGIEXPERT_API_KEY", raising=False)
    monkeypatch.setenv("API_BASE_URL", "https://example.test/api")

    with pytest.raises(RuntimeError, match="API-key-protected"):
        _test_support.send_request(
            "GET",
            "/private",
            None,
            "application/json",
            requires_api_key=True,
        )
