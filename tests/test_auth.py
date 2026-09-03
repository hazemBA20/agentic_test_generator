"""Auth resolution: spec security -> plan flags -> runtime credentials."""
import pytest

from src.helpers import _test_support
from src.helpers import auth
from src.helpers.auth_resolver import resolve_operation_auth
from src.helpers.rewrite_failed import NamedPatch, _apply


def _wrapper(security=None, schemes=None):
    operation = {}
    if security is not None:
        operation["security"] = security
    return {
        "path": "/x",
        "method": "GET",
        "operation": operation,
        "security_schemes": schemes or {},
    }


# --- resolver decision table ------------------------------------------------

def test_explicit_empty_security_is_public():
    assert resolve_operation_auth(_wrapper(security=[])) == {"kind": "none"}


def test_api_key_header_name_comes_from_the_scheme():
    wrapper = _wrapper(
        [{"apiKey": []}],
        {"apiKey": {"type": "apiKey", "in": "header", "name": "X-Custom-Key"}},
    )
    assert resolve_operation_auth(wrapper) == {"kind": "api_key", "header": "X-Custom-Key"}


def test_query_api_keys_are_unsupported():
    wrapper = _wrapper(
        [{"apiKey": []}],
        {"apiKey": {"type": "apiKey", "in": "query", "name": "key"}},
    )
    result = resolve_operation_auth(wrapper)
    assert result["kind"] == "unsupported"
    assert "query" in result["reason"]


def test_bearer_basic_and_oauth_schemes_resolve():
    cases = [
        ({"type": "http", "scheme": "bearer"}, "bearer"),
        ({"type": "http", "scheme": "basic"}, "basic"),
        ({"type": "oauth2", "flows": {}}, "bearer"),
        ({"type": "openIdConnect", "openIdConnectUrl": "x"}, "bearer"),
    ]
    for scheme, expected_kind in cases:
        wrapper = _wrapper([{"auth": []}], {"auth": scheme})
        result = resolve_operation_auth(wrapper)
        assert result["kind"] == expected_kind, scheme


def test_unknown_scheme_type_is_unsupported():
    wrapper = _wrapper([{"mtls": []}], {"mtls": {"type": "mutualTLS"}})
    assert resolve_operation_auth(wrapper)["kind"] == "unsupported"


def test_referenced_but_undefined_scheme_is_unsupported():
    result = resolve_operation_auth(_wrapper([{"ghost": []}]))
    assert result["kind"] == "unsupported"
    assert "ghost" in result["reason"]


def test_silent_spec_defaults_to_the_api_key():
    assert resolve_operation_auth(_wrapper()) == {"kind": "api_key", "header": "X-API-KEY"}


def test_silent_spec_prefers_a_declared_api_key_header():
    wrapper = _wrapper(None, {"apiKey": {"type": "apiKey", "in": "header", "name": "X-K"}})
    assert resolve_operation_auth(wrapper) == {"kind": "api_key", "header": "X-K"}


def test_alternatives_fall_through_to_a_satisfiable_one():
    wrapper = _wrapper(
        [{"ghost": []}, {"bearer": []}],
        {"bearer": {"type": "http", "scheme": "bearer"}},
    )
    assert resolve_operation_auth(wrapper) == {"kind": "bearer"}


def test_multi_credential_alternative_is_skipped_when_another_exists():
    wrapper = _wrapper(
        [{"a": [], "b": []}, {"bearer": []}],
        {
            "a": {"type": "http", "scheme": "bearer"},
            "b": {"type": "apiKey", "in": "header", "name": "X-K"},
            "bearer": {"type": "http", "scheme": "bearer"},
        },
    )
    assert resolve_operation_auth(wrapper) == {"kind": "bearer"}


# --- builder backfill -------------------------------------------------------

def test_backfill_sets_plan_auth_from_the_operation(monkeypatch):
    """nodes imports helpers.*, so the module is loaded with src on sys.path."""
    import importlib
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")
    models = importlib.import_module("workflow.utils.models")

    class _Message:
        test_plans = [
            models.TestPlan(name="test_x", description="d", category="negative",
                            method="GET", path="/x", expected_status_code=400)
        ]

    async def fake_retry(invoke_fn):
        return _Message()

    monkeypatch.setattr(nodes, "_call_with_retry", fake_retry)
    wrapper = _wrapper(
        [{"bearerAuth": []}],
        {"bearerAuth": {"type": "http", "scheme": "bearer"}},
    )
    scenario = models.ScenarioSpec(
        name="test_x", category="negative", description="d",
        target_status_code=400, focus="f",
    )

    plan = nodes.build_plans([wrapper], [[scenario]])[0]

    assert plan.requires_jwt is True
    assert plan.requires_api_key is False
    assert plan.api_key_header == "X-API-KEY"


def test_unsatisfiable_auth_drops_the_batch_before_the_model_is_called(monkeypatch):
    import importlib
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "src"))
    nodes = importlib.import_module("workflow.utils.nodes")
    models = importlib.import_module("workflow.utils.models")

    def unexpected(*args, **kwargs):
        pytest.fail("the builder must not be called for an unauthenticatable operation")

    monkeypatch.setattr(nodes, "_call_with_retry", unexpected)
    wrapper = _wrapper([{"ghost": []}])
    scenario = models.ScenarioSpec(
        name="test_x", category="negative", description="d",
        target_status_code=400, focus="f",
    )

    assert nodes.build_plans([wrapper], [[scenario]]) == []


# --- runtime attachment -----------------------------------------------------

def _send(monkeypatch, **kwargs):
    captured = {}

    def fake_request(**kw):
        captured.update(kw)
        return object()

    monkeypatch.setattr(_test_support.requests, "request", fake_request)
    monkeypatch.setenv("API_BASE_URL", "https://example.test/api")
    _test_support.send_request("GET", "/x", None, "application/json", **kwargs)
    return captured


def test_bearer_token_comes_from_a_static_env(monkeypatch):
    monkeypatch.setattr(auth, "_cached_bearer", None)
    monkeypatch.setenv("AUTH_TOKEN", "static-tok")
    captured = _send(monkeypatch, requires_jwt=True)
    assert captured["headers"]["Authorization"] == "Bearer static-tok"


def test_bearer_via_login_flow_runs_once_and_is_cached(monkeypatch):
    monkeypatch.setattr(auth, "_cached_bearer", None)
    for name in ("AUTH_TOKEN", "API_JWT", "DIGIEXPERT_JWT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_TOKEN_URL", "https://sso.example.test/login")
    monkeypatch.setenv("AUTH_USERNAME", "user")
    monkeypatch.setenv("AUTH_PASSWORD", "pass")

    calls = []

    class FakeLogin:
        status_code = 200

        def json(self):
            return {"access_token": "login-tok"}

    def fake_request(method=None, url=None, **kwargs):
        calls.append((method, url))
        return FakeLogin()

    monkeypatch.setattr(_test_support.requests, "request", fake_request)
    monkeypatch.setenv("API_BASE_URL", "https://example.test/api")

    _test_support.send_request("GET", "/x", None, "application/json", requires_jwt=True)
    _test_support.send_request("GET", "/x", None, "application/json", requires_jwt=True)

    assert calls[0] == ("POST", "https://sso.example.test/login")
    assert len(calls) == 3  # one login, two API calls — the token is cached


def test_login_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(auth, "_cached_bearer", None)
    for name in ("AUTH_TOKEN", "API_JWT", "DIGIEXPERT_JWT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AUTH_TOKEN_URL", "https://sso.example.test/login")
    monkeypatch.setenv("AUTH_USERNAME", "user")
    monkeypatch.setenv("AUTH_PASSWORD", "wrong")
    monkeypatch.setenv("API_BASE_URL", "https://example.test/api")

    class FakeReject:
        status_code = 401

    # auth.requests and _test_support.requests are the same module; this patch
    # must stay in force when bearer_token() fires, so do not reuse _send().
    monkeypatch.setattr(auth.requests, "request", lambda *a, **k: FakeReject())

    with pytest.raises(RuntimeError, match="Bearer login failed"):
        _test_support.send_request("GET", "/x", None, "application/json", requires_jwt=True)


def test_basic_auth_uses_http_auth(monkeypatch):
    monkeypatch.setenv("AUTH_BASIC_USERNAME", "user")
    monkeypatch.setenv("AUTH_BASIC_PASSWORD", "pass")
    captured = _send(monkeypatch, requires_basic=True)
    assert captured["auth"] == ("user", "pass")


def test_basic_auth_without_credentials_is_refused(monkeypatch):
    monkeypatch.delenv("AUTH_BASIC_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_BASIC_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="AUTH_BASIC_USERNAME"):
        _send(monkeypatch, requires_basic=True)


def test_missing_api_key_error_names_the_operation(monkeypatch):
    monkeypatch.delenv("DIGIEXPERT_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API-key-protected"):
        _send(monkeypatch, requires_api_key=True)


# --- reviewer guard ---------------------------------------------------------

def test_reviewer_cannot_patch_credential_headers():
    plan = {"name": "t", "headers": {}}
    reason = _apply(
        plan,
        NamedPatch(name="t", action="patch", reason="r",
                   headers={"Authorization": "Bearer x"}),
        kind="status",
    )
    assert reason and "credential" in reason
    assert plan["headers"] == {}
