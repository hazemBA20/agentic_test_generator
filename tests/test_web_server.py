"""Endpoint tests for the web server: TestClient, no LLM, no network.

The real fixture store under src/helpers/fixture/ is monkeypatched away so
these tests cannot touch it.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import pipeline, server


@pytest.fixture
def scratch_fixtures(monkeypatch, tmp_path):
    data_path = tmp_path / "test_data.json"
    data_path.write_text('{"existing_key": "v1"}\n', encoding="utf-8")
    fixtures_dir = tmp_path / "fixture"
    fixtures_dir.mkdir()
    (fixtures_dir / "sample.pdf").write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(server, "FIXTURE_DATA_PATH", data_path)
    monkeypatch.setattr(server, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(pipeline, "clear_pipeline_caches", lambda: None)
    return {"data_path": data_path, "fixtures_dir": fixtures_dir}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    plans = tmp_path / "test_plans.json"
    tests = tmp_path / "test.py"
    results = tmp_path / "test_results.json"
    coverage = tmp_path / "coverage_report.json"
    review_log = tmp_path / "rewrite_log.json"
    monkeypatch.setattr(server, "DEFAULT_PLANS_PATH", plans)
    monkeypatch.setattr(server, "DEFAULT_TESTS_PATH", tests)
    monkeypatch.setattr(server, "DEFAULT_RESULTS_PATH", results)
    monkeypatch.setattr(server, "DEFAULT_COVERAGE_PATH", coverage)
    monkeypatch.setattr(server, "DEFAULT_REVIEW_LOG_PATH", review_log)
    return {"plans": plans, "tests": tests, "results": results, "coverage": coverage, "review_log": review_log}


@pytest.fixture
def client(scratch_fixtures, workspace, tmp_path):
    server.session = pipeline.Session(uploads_dir=tmp_path / "uploads")
    server.jobs = pipeline.JobRunner()
    with TestClient(server.app) as test_client:
        yield test_client


SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "demo", "version": "1.0"},
    "paths": {
        "/pets": {
            "get": {"summary": "List pets"},
            "post": {
                "summary": "Create a pet",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    }
                },
            },
        }
    },
}


def test_health(client):
    body = client.get("/api/health").json()
    assert set(body) >= {
        "providers", "providers_ready", "api_base_url_set",
        "fixture_data_keys", "fixture_files", "spec_loaded", "operation_count",
    }
    assert body["fixture_data_keys"] == 1
    assert body["fixture_files"] == ["sample.pdf"]
    # Provider flags are booleans — the endpoint must never leak values.
    assert all(isinstance(flag, bool) for flag in body["providers"].values())


def test_upload_and_operations_flow(client):
    assert client.get("/api/operations").status_code == 409

    response = client.post(
        "/api/spec", files={"spec": ("spec.json", __import__("json").dumps(SPEC).encode(), "application/json")}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["spec_name"] == "spec.json"
    assert [op["method"] for op in body["operations"]] == ["GET", "POST"]

    listed = client.get("/api/operations").json()
    assert listed["spec_name"] == "spec.json"
    assert len(listed["operations"]) == 2


def test_upload_rejects_bad_files_and_keeps_session(client):
    client.post("/api/spec", files={"spec": ("spec.json", __import__("json").dumps(SPEC).encode(), "application/json")})
    assert client.post("/api/spec", files={"spec": ("notes.txt", b"hi", "text/plain")}).status_code == 400
    assert client.post("/api/spec", files={"spec": ("broken.json", b"not json", "application/json")}).status_code == 400
    # The good spec from the first call is still the loaded one.
    assert len(client.get("/api/operations").json()["operations"]) == 2


def test_fixture_needs_endpoint(client):
    client.post("/api/spec", files={"spec": ("spec.json", __import__("json").dumps(SPEC).encode(), "application/json")})
    assert client.get("/api/fixture-needs", params={"operation_index": 9}).status_code == 404
    body = client.get("/api/fixture-needs", params={"operation_index": 1}).json()
    assert body["operation"]["method"] == "POST"
    assert {"field": "name", "in": "body", "covered": None} in body["suggested_keys"]


def test_fixture_crud_endpoints(client, scratch_fixtures):
    body = client.put("/api/fixtures/data", json={"updates": {"another": 7}}).json()
    assert body["data"]["another"] == 7
    assert body["data"]["existing_key"] == "v1"

    assert client.put("/api/fixtures/data", json={"updates": {}}).status_code == 400

    body = client.delete("/api/fixtures/data/another").json()
    assert "another" not in body["data"]
    assert client.delete("/api/fixtures/data/another").status_code == 404

    files = client.post(
        "/api/fixtures/files", files={"file": ("sample.csv", b"a,b\n1,2\n", "text/csv")}
    ).json()
    assert files["saved"] == "sample.csv"
    assert "sample.csv" in files["files"]

    listing = client.get("/api/fixtures").json()
    assert listing["data"] == {"existing_key": "v1"}
    assert sorted(listing["files"]) == ["sample.csv", "sample.pdf"]


def test_fixture_uploads_tracked_per_session(client):
    first = client.post(
        "/api/fixtures/files", files={"file": ("a.jpg", b"1", "image/jpeg")}
    ).json()
    assert first["saved"] == "a.jpg"
    assert first["session_uploads"] == ["a.jpg"]

    second = client.post(
        "/api/fixtures/files", files={"file": ("b.jpg", b"2", "image/jpeg")}
    ).json()
    assert second["session_uploads"] == ["b.jpg", "a.jpg"]

    assert client.get("/api/fixtures").json()["session_uploads"] == ["b.jpg", "a.jpg"]


def test_target_override_allows_run_without_env(client, monkeypatch):
    import time

    monkeypatch.delenv("API_BASE_URL", raising=False)
    assert client.post("/api/jobs/run").status_code == 400

    assert client.put("/api/target", json={"base_url": "not a url"}).status_code == 400

    body = client.put(
        "/api/target", json={"base_url": "https://ui.example.com/api/"}
    ).json()
    assert body == {
        "base_url": "https://ui.example.com/api",
        "base_url_source": "session",
    }

    health = client.get("/api/health").json()
    assert health["base_url"] == "https://ui.example.com/api"
    assert health["base_url_source"] == "session"
    assert health["api_base_url_set"] is True

    calls = {}

    def fake_run(**kwargs):
        calls.update(kwargs)
        return {"summary": {"total": 0, "passed": 0, "skipped": 0, "failed": 0}, "results": []}

    monkeypatch.setattr(pipeline, "run_generated_tests", fake_run)
    assert client.post("/api/jobs/run").status_code == 200
    deadline = time.time() + 5
    while client.get("/api/jobs/current").json()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert client.get("/api/jobs/current").json()["state"] == "done"
    assert calls["base_url"] == "https://ui.example.com/api"

    client.put("/api/target", json={"base_url": None})
    # Clearing drops back to env, which is unset here — run is refused again.
    assert client.get("/api/health").json()["base_url"] is None
    assert client.get("/api/health").json()["base_url_source"] == "none"
    assert client.post("/api/jobs/run").status_code == 400


def test_artifact_endpoints_404_before_generation(client, workspace):
    assert client.get("/api/artifacts/plans").status_code == 404
    assert client.get("/api/artifacts/tests").status_code == 404
    assert client.get("/api/artifacts/results").status_code == 404


def test_artifact_endpoints_serve_files(client, workspace):
    workspace["plans"].write_text("[]", encoding="utf-8", newline="")
    workspace["tests"].write_text("# generated\n", encoding="utf-8", newline="")
    assert client.get("/api/artifacts/plans").text == "[]"
    assert client.get("/api/artifacts/tests").text == "# generated\n"
    download = client.get("/api/artifacts/tests", params={"download": True})
    assert download.headers["content-disposition"] == 'attachment; filename="test.py"'


def test_run_requires_base_url_and_plans(client, workspace, monkeypatch):
    monkeypatch.delenv("API_BASE_URL", raising=False)
    response = client.post("/api/jobs/run")
    assert response.status_code == 400
    assert "API_BASE_URL" in response.json()["detail"]


def test_generate_requires_spec(client):
    assert client.post("/api/jobs/generate", json={"operation_index": 0}).status_code == 409


def test_full_requires_spec_scope_and_target(client, monkeypatch):
    assert client.post("/api/jobs/full", json={}).status_code == 409

    client.post(
        "/api/spec", files={"spec": ("spec.json", __import__("json").dumps(SPEC).encode(), "application/json")}
    )
    assert client.post("/api/jobs/full", json={"scope": "bogus"}).status_code == 400
    assert client.post("/api/jobs/full", json={"scope": "operation"}).status_code == 400
    assert client.post(
        "/api/jobs/full", json={"scope": "operation", "operation_index": 9}
    ).status_code == 404

    monkeypatch.delenv("API_BASE_URL", raising=False)
    assert client.post(
        "/api/jobs/full", json={"scope": "all", "run_tests": True}
    ).status_code == 400


def test_full_provider_prereqs(client, monkeypatch):
    client.post(
        "/api/spec", files={"spec": ("spec.json", __import__("json").dumps(SPEC).encode(), "application/json")}
    )
    monkeypatch.setattr(
        pipeline,
        "health",
        lambda *args, **kwargs: {"providers": {"GEMINI_API_KEY": False, "OPENROUTER_API_KEY": False}},
    )
    assert client.post(
        "/api/jobs/full", json={"scope": "all", "coverage": True}
    ).status_code == 400
    monkeypatch.setenv("API_BASE_URL", "https://target.example.com")
    assert client.post(
        "/api/jobs/full", json={"scope": "all", "run_tests": True, "review": True}
    ).status_code == 400


def test_full_runs_and_reports_progress(client, monkeypatch):
    import threading
    import time

    client.post(
        "/api/spec", files={"spec": ("spec.json", __import__("json").dumps(SPEC).encode(), "application/json")}
    )
    release = threading.Event()
    calls = {}

    def fake_full(session, **kwargs):
        calls.update(kwargs)
        kwargs["progress"]({"stage": "builder", "label": "Building test plans"})
        release.wait(5)
        return {
            "scope": {"run_all": False, "operation": {"method": "GET", "path": "/pets"}, "operation_count": 2},
            "options": {"coverage": False, "run_tests": False, "review": False},
            "plans": [],
            "results": [],
        }

    monkeypatch.setattr(pipeline, "run_full_pipeline", fake_full)
    response = client.post(
        "/api/jobs/full", json={"scope": "operation", "operation_index": 0}
    )
    assert response.status_code == 200
    assert response.json()["kind"] == "full"
    deadline = time.time() + 5
    seen = None
    while time.time() < deadline:
        seen = client.get("/api/jobs/current").json()
        if seen["progress"]:
            break
        time.sleep(0.02)
    assert seen["progress"]["stage"] == "builder"
    assert calls["run_all"] is False
    assert calls["operation_index"] == 0
    release.set()
    deadline = time.time() + 5
    while client.get("/api/jobs/current").json()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    done = client.get("/api/jobs/current").json()
    assert done["state"] == "done"
    # The snapshot must stay JSON-serializable for the API.
    assert done["result"]["scope"]["operation_count"] == 2


def test_coverage_and_review_log_artifacts(client, workspace):
    assert client.get("/api/artifacts/coverage").status_code == 404
    assert client.get("/api/artifacts/review-log").status_code == 404
    workspace["coverage"].write_text("[]", encoding="utf-8", newline="")
    workspace["review_log"].write_text("[]", encoding="utf-8", newline="")
    assert client.get("/api/artifacts/coverage").text == "[]"
    assert client.get("/api/artifacts/review-log").text == "[]"
    download = client.get("/api/artifacts/review-log", params={"download": True})
    assert download.headers["content-disposition"] == 'attachment; filename="rewrite_log.json"'


def test_index_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Agentic Test Generator" in response.text
    assert 'id="job-progress"' in response.text


def test_job_progress_visible_while_running(client, monkeypatch):
    import threading
    import time

    client.post(
        "/api/spec",
        files={"spec": ("spec.json", __import__("json").dumps(SPEC).encode(), "application/json")},
    )
    release = threading.Event()
    entered = threading.Event()

    def fake_generate(session, operation_index, progress=None):
        entered.set()
        if progress is not None:
            progress({"stage": "building", "label": "Building test plans", "done": 1, "total": 3})
        release.wait(5)
        return {
            "plans": [],
            "planner_failures": 0,
            "builder_failures": 0,
            "issues_by_plan": [],
            "fixture_report": {},
            "operation": {"method": "GET", "path": "/pets"},
        }

    monkeypatch.setattr(pipeline, "generate_for_operation", fake_generate)
    assert client.post("/api/jobs/generate", json={"operation_index": 0}).status_code == 200
    assert entered.wait(5)
    deadline = time.time() + 5
    seen = None
    while time.time() < deadline:
        seen = client.get("/api/jobs/current").json()
        if seen["progress"]:
            break
        time.sleep(0.02)
    assert seen["progress"]["stage"] == "building"
    assert seen["progress"]["done"] == 1
    assert seen["progress"]["total"] == 3
    release.set()
    deadline = time.time() + 5
    while client.get("/api/jobs/current").json()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert client.get("/api/jobs/current").json()["state"] == "done"
