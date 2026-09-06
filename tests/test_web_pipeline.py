"""Offline tests for the web adapter: no LLM, no network, no provider keys.

The real fixture store under src/helpers/fixture/ is never touched — every
test works against a scratch store in tmp_path.
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import pipeline


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    """A scratch fixture store and dir, with cache-clearing stubbed out."""
    data_path = tmp_path / "test_data.json"
    data_path.write_text('{"existing_key": "v1"}\n', encoding="utf-8")
    fixtures_dir = tmp_path / "fixture"
    fixtures_dir.mkdir()
    calls = []
    monkeypatch.setattr(pipeline, "clear_pipeline_caches", lambda: calls.append(True))
    return {"data_path": data_path, "fixtures_dir": fixtures_dir, "cache_clears": calls}


def _multipart_operation(**overrides):
    operation = {
        "path": "/uploads",
        "method": "POST",
        "operation": {
            "summary": "Upload a document",
            "requestBody": {
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["document"],
                            "properties": {
                                "document": {"type": "string", "format": "binary"},
                                "note": {"type": "string"},
                            },
                        }
                    }
                }
            },
        },
        "definitions": {},
        "security_schemes": {},
    }
    operation.update(overrides)
    return operation


def test_scan_detects_binary_file_need(scratch):
    report = pipeline.scan_operation_fixtures(
        _multipart_operation(), scratch["data_path"], scratch["fixtures_dir"]
    )
    assert report["file_needs"] == [
        {"field_path": "document", "media_type": "multipart/form-data"}
    ]
    suggested = {item["field"]: item for item in report["suggested_keys"]}
    # A binary upload is satisfied with a <FILE:...>, never with a
    # <FIXTURE:...> data value, so it must not be suggested as a key.
    assert "document" not in suggested
    # Optional fields are not suggestions.
    assert "note" not in suggested


def test_scan_suggests_non_file_required_fields_alongside_binary(scratch):
    """Regression: registrationDocument-style ops suggest only the text fields.

    A multipart op with a required binary field plus a required string field
    must report the file need and suggest only the string field as a key.
    """
    operation = _multipart_operation()
    schema = operation["operation"]["requestBody"]["content"]["multipart/form-data"]["schema"]
    schema["required"] = ["document", "requestId"]
    schema["properties"]["requestId"] = {"type": "string"}
    report = pipeline.scan_operation_fixtures(
        operation, scratch["data_path"], scratch["fixtures_dir"]
    )
    assert report["file_needs"] == [
        {"field_path": "document", "media_type": "multipart/form-data"}
    ]
    suggested = {item["field"]: item for item in report["suggested_keys"]}
    assert set(suggested) == {"requestId"}
    assert suggested["requestId"]["in"] == "body"


def test_scan_reports_auth_required_params_and_path_params(scratch):
    operation = {
        "path": "/customers/{customerId}",
        "method": "GET",
        "operation": {
            "security": [{"api_key": []}],
            "parameters": [
                {"name": "limit", "in": "query", "required": True, "schema": {"type": "integer"}}
            ],
        },
        "definitions": {},
        "security_schemes": {"api_key": {"type": "apiKey", "name": "X-API-KEY", "in": "header"}},
    }
    report = pipeline.scan_operation_fixtures(
        operation, scratch["data_path"], scratch["fixtures_dir"]
    )
    assert report["auth"] == [
        {"name": "X-API-KEY", "type": "apiKey", "in": "header", "description": None}
    ]
    fields = {(item["field"], item["in"]) for item in report["suggested_keys"]}
    assert ("customerId", "path") in fields
    assert ("limit", "query") in fields


def test_scan_matches_available_keys_case_insensitively(scratch):
    scratch["data_path"].write_text('{"customerid": "42"}\n', encoding="utf-8")
    operation = {
        "path": "/customers/{customerId}",
        "method": "GET",
        "operation": {},
        "definitions": {},
        "security_schemes": {},
    }
    report = pipeline.scan_operation_fixtures(
        operation, scratch["data_path"], scratch["fixtures_dir"]
    )
    covered = {item["field"]: item["covered"] for item in report["suggested_keys"]}
    assert covered["customerId"] == "customerid"
    assert report["available_keys"] == ["customerid"]


def test_update_and_delete_fixture_data_merge(scratch):
    data = pipeline.update_fixture_data({"new_key": {"a": 1}}, scratch["data_path"])
    assert data["existing_key"] == "v1"
    assert data["new_key"] == {"a": 1}
    on_disk = json.loads(scratch["data_path"].read_text(encoding="utf-8"))
    assert on_disk["new_key"] == {"a": 1}
    assert scratch["cache_clears"], "edits must invalidate the pipeline caches"

    with pytest.raises(KeyError):
        pipeline.delete_fixture_key("absent", scratch["data_path"])
    data = pipeline.delete_fixture_key("new_key", scratch["data_path"])
    assert "new_key" not in data


def test_update_fixture_data_rejects_empty_updates(scratch):
    with pytest.raises(ValueError):
        pipeline.update_fixture_data({}, scratch["data_path"])


def test_missing_fixture_report_buckets(scratch, monkeypatch):
    (scratch["fixtures_dir"] / "sample.pdf").write_bytes(b"%PDF-1.4 test")
    plans = [
        {
            "name": "t_undefined_ref",
            "request_body": {"code": "<FIXTURE:missing_code>"},
            "path_params": {},
            "query_params": {},
            "headers": {},
        },
        {
            "name": "t_advised_only",
            "request_body": None,
            "path_params": {},
            "query_params": {},
            "headers": {},
            "missing_fixtures": ["also_missing"],
        },
        {
            "name": "t_defined_ref",
            "request_body": None,
            "path_params": {"id": "<FIXTURE:existing_key>"},
            "query_params": {},
            "headers": {},
        },
        {
            "name": "t_file_ok",
            "request_body": {"doc": "<FILE:pdf>"},
            "path_params": {},
            "query_params": {},
            "headers": {},
        },
        {
            "name": "t_file_unavailable",
            "request_body": {"doc": "<FILE:report.xyz>"},
            "path_params": {},
            "query_params": {},
            "headers": {},
        },
        {
            "name": "t_env",
            "request_body": None,
            "path_params": {},
            "query_params": {},
            "headers": {"X-Token": "<ENV:WEB_TEST_UNSET_VAR>"},
        },
    ]
    monkeypatch.delenv("WEB_TEST_UNSET_VAR", raising=False)
    report = pipeline.missing_fixture_report(
        plans, scratch["data_path"], scratch["fixtures_dir"]
    )
    assert report["missing_data_keys"] == ["also_missing", "missing_code"]
    # sample.pdf is present, and the resolution semantics accept any local
    # file as a final fallback — so both sentinels are resolvable here.
    by_requested = {need["requested"]: need for need in report["file_needs"]}
    assert by_requested["pdf"]["available"] is True
    assert by_requested["report.xyz"]["available"] is True
    assert report["env_needs"] == ["WEB_TEST_UNSET_VAR"]


def test_file_need_available_follows_resolution_fallbacks(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    # Nothing on disk at all: nothing can satisfy a <FILE:> sentinel.
    assert pipeline._file_need_available("<FILE:report.xyz>", empty) is False

    only_txt = tmp_path / "some"
    only_txt.mkdir()
    (only_txt / "sample.txt").write_text("x", encoding="utf-8")
    # Any file works as the final fallback...
    assert pipeline._file_need_available("<FILE:report.xyz>", only_txt) is True
    # ...and the shorthand <FILE:pdf> means sample.pdf, which is absent here,
    # but the any-file fallback still resolves it.
    assert pipeline._file_need_available("<FILE:pdf>", only_txt) is True

    both = tmp_path / "both"
    both.mkdir()
    (both / "sample.pdf").write_bytes(b"%PDF")
    # Same-extension wins only as a fallback order; availability is what matters.
    assert pipeline._file_need_available("<FILE:report.xyz>", both) is True
    assert pipeline._file_need_available("<FILE:pdf>", both) is True


def test_session_load_spec_and_listing(tmp_path):
    spec = {
        "openapi": "3.0.0",
        "paths": {
            "/a": {"get": {"summary": "List a"}},
            "/b": {
                "post": {
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {
                                "schema": {"type": "object", "properties": {}}
                            }
                        }
                    }
                }
            },
        },
    }
    session = pipeline.Session(uploads_dir=tmp_path)
    operations = session.load_spec("spec.json", json.dumps(spec).encode("utf-8"))
    assert len(operations) == 2
    summaries = session.summarized_operations()
    assert summaries[0]["index"] == 0
    assert summaries[0]["method"] == "GET"
    assert summaries[1]["content_type"] == "multipart/form-data"

    with pytest.raises(IndexError):
        session.operation(5)

    # A bad upload keeps the previous spec loaded.
    with pytest.raises(ValueError):
        session.load_spec("bad.txt", b"{}")
    with pytest.raises(ValueError):
        session.load_spec("bad.json", b"this is not json/yaml")
    assert session.spec_name == "spec.json"
    assert len(session.operations) == 2


def test_job_runner_is_single_slot():
    import threading

    runner = pipeline.JobRunner()
    started = threading.Event()
    release = threading.Event()

    def held_job():
        started.set()
        release.wait(5)
        return {"ok": True}

    runner.submit("generate", held_job)
    assert started.wait(5), "the worker never picked up the first job"
    with pytest.raises(pipeline.JobInProgress):
        runner.submit("run", lambda: {})
    release.set()

    deadline = time.time() + 5
    while runner.snapshot()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    snapshot = runner.snapshot()
    assert snapshot["state"] == "done"
    assert snapshot["result"] == {"ok": True}


def test_job_runner_reports_errors():
    def boom():
        raise RuntimeError("the model is unreachable")

    runner = pipeline.JobRunner()
    runner.submit("run", boom)
    deadline = time.time() + 5
    while runner.snapshot()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    snapshot = runner.snapshot()
    assert snapshot["state"] == "error"
    assert snapshot["error"].startswith("RuntimeError:")


@pytest.fixture
def image_dir(monkeypatch, tmp_path):
    """A fixture store with several images, like a lived-in frontend store."""
    from helpers import _test_support

    directory = tmp_path / "fx"
    directory.mkdir()
    (directory / "sample.jpg").write_bytes(b"stale")
    (directory / "card-back.jpg").write_bytes(b"back")
    (directory / "my-card.jpg").write_bytes(b"mine")
    monkeypatch.setattr(_test_support, "FIXTURES_DIR", directory)
    assert _test_support.PREFERRED_FILES == ()
    return directory


def test_resolve_without_preference_is_unchanged(image_dir):
    from helpers import _test_support

    assert _test_support._resolve_fixture("<FILE:sample.jpg>")[1].name == "sample.jpg"
    # Missing name falls back to the alphabetically first same-extension file.
    assert _test_support._resolve_fixture("<FILE:scan.jpg>")[1].name == "card-back.jpg"


def test_resolve_prefers_session_upload(image_dir):
    from helpers import _test_support

    with _test_support.preferred_files(["my-card.jpg"]):
        # Generic shorthand goes to the session upload, not stale sample.jpg.
        assert _test_support._resolve_fixture("<FILE:jpg>")[1].name == "my-card.jpg"
        # A missing explicit name prefers the session upload over dir scan.
        assert _test_support._resolve_fixture("<FILE:scan.jpg>")[1].name == "my-card.jpg"
        # An explicit name that exists on disk still wins.
        assert _test_support._resolve_fixture("<FILE:sample.jpg>")[1].name == "sample.jpg"
    assert _test_support.PREFERRED_FILES == ()


def test_resolve_ignores_session_uploads_missing_from_disk(image_dir):
    from helpers import _test_support

    with _test_support.preferred_files(["deleted.jpg"]):
        assert _test_support._resolve_fixture("<FILE:scan.jpg>")[1].name == "card-back.jpg"


def test_session_upload_tracking_is_recency_ordered(tmp_path):
    session = pipeline.Session(uploads_dir=tmp_path)
    assert session.snapshot_uploads() == []
    session.note_fixture_upload("a.jpg")
    session.note_fixture_upload("b.jpg")
    session.note_fixture_upload("a.jpg")
    assert session.snapshot_uploads() == ["a.jpg", "b.jpg"]


def test_run_forwards_session_preference(tmp_path, monkeypatch):
    from helpers import _test_support
    from helpers import execute_plans as execute_module

    seen = {}

    def fake_execute(plans, progress_cb=None):
        seen["preferred"] = _test_support.PREFERRED_FILES
        return []

    monkeypatch.setattr(execute_module, "execute_plans", fake_execute)
    plans_path = tmp_path / "plans.json"
    plans_path.write_text('[{"name": "t"}]', encoding="utf-8")
    results_path = tmp_path / "results.json"
    out = pipeline.run_generated_tests(
        plans_path, results_path, preferred_files=["my-card.jpg"]
    )
    assert seen["preferred"] == ("my-card.jpg",)
    assert _test_support.PREFERRED_FILES == ()
    assert out["preferred_files"] == ["my-card.jpg"]
    assert out["summary"] == {"total": 0, "passed": 0, "skipped": 0, "failed": 0}


def test_normalize_base_url():
    assert pipeline.normalize_base_url(None) is None
    assert pipeline.normalize_base_url("   ") is None
    assert (
        pipeline.normalize_base_url("https://api.example.com/api/")
        == "https://api.example.com/api"
    )
    assert (
        pipeline.normalize_base_url("http://localhost:8000")
        == "http://localhost:8000"
    )
    with pytest.raises(ValueError):
        pipeline.normalize_base_url("not a url")
    with pytest.raises(ValueError):
        pipeline.normalize_base_url("ftp://host/api")


def test_resolve_base_url_prefers_session_then_env(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "https://env.example.com")
    assert pipeline.resolve_base_url("https://ui.example.com") == (
        "https://ui.example.com",
        "session",
    )
    assert pipeline.resolve_base_url() == ("https://env.example.com", "env")
    monkeypatch.delenv("API_BASE_URL")
    assert pipeline.resolve_base_url() == (None, "none")


def test_session_base_url_round_trip(tmp_path):
    session = pipeline.Session(uploads_dir=tmp_path)
    assert session.snapshot_base_url() is None
    assert session.set_base_url("https://ui.example.com/api/") == "https://ui.example.com/api"
    assert session.snapshot_base_url() == "https://ui.example.com/api"
    assert session.set_base_url("") is None
    with pytest.raises(ValueError):
        session.set_base_url("bogus")


def test_run_applies_base_url_override(tmp_path, monkeypatch):
    from helpers import _test_support
    from helpers import execute_plans as execute_module

    seen = {}

    def fake_execute(plans, progress_cb=None):
        seen["override"] = _test_support.BASE_URL_OVERRIDE
        return [{"name": "t", "passed": True, "skipped": False}]

    monkeypatch.setattr(execute_module, "execute_plans", fake_execute)
    plans_path = tmp_path / "plans.json"
    plans_path.write_text('[{"name": "t"}]', encoding="utf-8")
    out = pipeline.run_generated_tests(
        plans_path, tmp_path / "results.json", base_url="https://ui.example.com"
    )
    assert seen["override"] == "https://ui.example.com"
    assert _test_support.BASE_URL_OVERRIDE is None
    assert out["base_url"] == "https://ui.example.com"
    assert out["base_url_source"] == "session"


def test_job_runner_reports_progress():
    import threading

    runner = pipeline.JobRunner()
    assert runner.snapshot()["progress"] is None

    release = threading.Event()
    started = threading.Event()

    def held_job():
        started.set()
        release.wait(5)
        return {"ok": True}

    runner.submit("generate", held_job)
    assert started.wait(5)
    runner.report({"stage": "building", "label": "Building", "done": 1, "total": 2})
    snapshot = runner.snapshot()
    assert snapshot["progress"]["stage"] == "building"
    assert snapshot["progress"]["done"] == 1
    assert snapshot["progress"]["total"] == 2
    release.set()

    deadline = time.time() + 5
    while runner.snapshot()["state"] == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert runner.snapshot()["state"] == "done"
    # Reports with no running job are ignored, not crashes.
    runner.report({"stage": "planning"})


def test_generate_emits_stage_sequence(tmp_path, monkeypatch):
    from workflow.utils import nodes as nodes_module

    events = []

    def fake_build(ops, scenarios, progress_cb=None):
        if progress_cb is not None:
            progress_cb(1, 2)
        return [{"name": "t", "method": "GET", "path": "/p"}], 0

    monkeypatch.setattr(
        nodes_module, "plan_scenarios", lambda ops: ([["s1", "s2", "s3", "s4", "s5"]], 0)
    )
    monkeypatch.setattr(nodes_module, "build_plans_with_failures", fake_build)
    monkeypatch.setattr(
        pipeline, "generate", lambda plans_path, tests_path: Path(tests_path).write_text("# t\n")
    )
    session = pipeline.Session(uploads_dir=tmp_path)
    session.operations = [{"path": "/p", "method": "GET", "operation": {}}]

    out = pipeline.generate_for_operation(
        session,
        0,
        plans_path=tmp_path / "plans.json",
        tests_path=tmp_path / "test.py",
        progress=events.append,
    )
    stages = [event["stage"] for event in events]
    assert stages[0] == "planning"
    assert stages[-1] == "validating"
    assert "building" in stages
    assert "rendering" in stages
    building = [event for event in events if event["stage"] == "building"]
    assert building[0]["done"] == 0
    assert any(event["done"] == 1 and event["total"] == 2 for event in building)
    assert out["tests_generated"] is True


def test_execute_plans_reports_counts():
    from helpers.execute_plans import execute_plans

    seen = []
    results = execute_plans(
        ["not a dict", {"name": "x"}], progress_cb=lambda done, total: seen.append((done, total))
    )
    assert seen == [(1, 2), (2, 2)]
    assert len(results) == 2


def _full_session(tmp_path):
    session = pipeline.Session(uploads_dir=tmp_path)
    session.spec_path = str(tmp_path / "spec.json")
    session.operations = [
        {"path": "/pets", "method": "GET", "operation": {"summary": "List"}}
    ]
    return session


def test_full_pipeline_streams_and_assembles(tmp_path, monkeypatch):
    import workflow.graph as graph_module

    seen_states = []
    chunks = [
        {"ingest": {}},
        {"planner": {}},
        {"builder": {"build_failures": 0}},
        {"render": {}},
        {"execute": {}},
    ]

    class FakeWorkflow:
        def stream(self, state):
            seen_states.append(dict(state))
            yield from chunks

    monkeypatch.setattr(graph_module, "compile_workflow", lambda: FakeWorkflow())
    plans = [
        {
            "name": "t_full",
            "method": "GET",
            "path": "/pets",
            "expected_status_code": 200,
            "request_body": None,
            "path_params": {},
            "query_params": {},
            "headers": {},
        }
    ]
    plans_path = tmp_path / "plans.json"
    plans_path.write_text(json.dumps(plans), encoding="utf-8")
    coverage_path = tmp_path / "coverage.json"
    coverage_path.write_text(
        json.dumps([{"method": "GET", "path": "/pets", "gaps": [{"kind": "k", "detail": "d"}]}]),
        encoding="utf-8",
    )
    events = []
    out = pipeline.run_full_pipeline(
        _full_session(tmp_path),
        run_all=True,
        coverage=True,
        review=True,
        base_url="https://ui.example.com",
        preferred_files=["a.jpg"],
        progress=events.append,
        plans_path=plans_path,
        tests_path=tmp_path / "test.py",
        results_path=tmp_path / "results.json",
        review_log_path=tmp_path / "review.json",
        coverage_report_path=coverage_path,
    )
    state = seen_states[0]
    assert state["run_all"] is True
    assert state["coverage"] is True
    assert state["review"] is True
    assert state["run_tests"] is True  # review implies execution
    assert state["plans_path"] == str(plans_path)
    assert [event["stage"] for event in events] == [
        "ingest", "planner", "builder", "render", "execute",
    ]
    assert out["scope"]["run_all"] is True
    assert out["options"] == {"coverage": True, "run_tests": True, "review": True}
    assert [plan["name"] for plan in out["plans"]] == ["t_full"]
    assert out["coverage_gaps"] == 1
    assert out["base_url"] == "https://ui.example.com"
    assert out["preferred_files"] == ["a.jpg"]


def test_full_pipeline_validates_inputs(tmp_path, monkeypatch):
    empty = pipeline.Session(uploads_dir=tmp_path)
    with pytest.raises(LookupError):
        pipeline.run_full_pipeline(empty, run_all=True)
    session = _full_session(tmp_path)
    with pytest.raises(ValueError, match="Pick an operation"):
        pipeline.run_full_pipeline(session)
    with pytest.raises(IndexError):
        pipeline.run_full_pipeline(session, operation_index=9)
    monkeypatch.delenv("API_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="No target"):
        pipeline.run_full_pipeline(session, operation_index=0, run_tests=True)
