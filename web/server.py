"""Local FastAPI app: serves the web UI and a thin JSON API over the
existing test-generation pipeline. Run from the repository root with
``python -m web.server`` (or ``uvicorn web.server:app``).

No pipeline behavior lives here — see web/pipeline.py — and nothing outside
the web workspace is written except the fixture store, which the pipeline
itself documents as user-editable data.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from web import pipeline
from web.pipeline import (
    DEFAULT_COVERAGE_PATH,
    DEFAULT_PLANS_PATH,
    DEFAULT_RESULTS_PATH,
    DEFAULT_REVIEW_LOG_PATH,
    DEFAULT_TESTS_PATH,
    FIXTURE_DATA_PATH,
    FIXTURES_DIR,
    JobInProgress,
    JobRunner,
    Session,
)

app = FastAPI(title="Agentic Test Generator", docs_url="/api/docs")

STATIC_DIR = Path(__file__).resolve().parent / "static"
session = Session()
jobs = JobRunner()


class GenerateRequest(BaseModel):
    operation_index: int


class FullRequest(BaseModel):
    scope: str = "operation"  # "operation" | "all"
    operation_index: int | None = None
    coverage: bool = False
    run_tests: bool = False
    review: bool = False


class FixtureUpdate(BaseModel):
    updates: dict


class TargetUpdate(BaseModel):
    base_url: str | None = None


def _no_spec() -> HTTPException:
    return HTTPException(409, "No spec uploaded yet — start with step 1.")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def get_health() -> dict:
    return pipeline.health(session, FIXTURE_DATA_PATH, FIXTURES_DIR)


@app.post("/api/spec")
async def upload_spec(spec: UploadFile) -> dict:
    content = await spec.read()
    if not content:
        raise HTTPException(400, "The uploaded spec file is empty.")
    try:
        operations = session.load_spec(spec.filename or "spec.json", content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "spec_name": session.spec_name,
        "operations": pipeline.summarize_operations(operations),
    }


@app.get("/api/operations")
def list_operations() -> dict:
    operations = session.summarized_operations()
    if not operations:
        raise _no_spec()
    return {"spec_name": session.spec_name, "operations": operations}


@app.get("/api/fixtures")
def get_fixtures() -> dict:
    return {
        "data": pipeline.load_fixture_data(FIXTURE_DATA_PATH),
        "files": pipeline.list_fixture_files(FIXTURES_DIR),
        "session_uploads": session.snapshot_uploads(),
    }


@app.put("/api/target")
def put_target(update: TargetUpdate) -> dict:
    """Set (or clear, with null/"") the UI target override for this session."""
    try:
        url = session.set_base_url(update.base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _, source = pipeline.resolve_base_url(url)
    return {"base_url": url, "base_url_source": source}


@app.put("/api/fixtures/data")
def put_fixtures(update: FixtureUpdate) -> dict:
    try:
        return {"data": pipeline.update_fixture_data(update.updates, FIXTURE_DATA_PATH)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/fixtures/data/{key}")
def delete_fixture(key: str) -> dict:
    try:
        return {"data": pipeline.delete_fixture_key(key, FIXTURE_DATA_PATH)}
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from exc


@app.post("/api/fixtures/files")
async def upload_fixture_file(file: UploadFile) -> dict:
    content = await file.read()
    if not content:
        raise HTTPException(400, "The uploaded fixture file is empty.")
    try:
        name = pipeline.save_fixture_file(file.filename or "", content, FIXTURES_DIR)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "saved": name,
        "files": pipeline.list_fixture_files(FIXTURES_DIR),
        "session_uploads": session.note_fixture_upload(name),
    }


@app.get("/api/fixture-needs")
def fixture_needs(operation_index: int) -> dict:
    try:
        operation = session.operation(operation_index)
    except IndexError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LookupError as exc:
        raise _no_spec() from exc
    needs = pipeline.scan_operation_fixtures(operation, FIXTURE_DATA_PATH, FIXTURES_DIR)
    return {"operation": pipeline.summarize_operations([operation])[0], **needs}


@app.post("/api/jobs/generate")
def start_generate(request: GenerateRequest) -> dict:
    try:
        session.operation(request.operation_index)
    except IndexError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LookupError as exc:
        raise _no_spec() from exc
    try:
        return jobs.submit(
            "generate",
            lambda: pipeline.generate_for_operation(
                session, request.operation_index, progress=jobs.report
            ),
        )
    except JobInProgress as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/jobs/run")
def start_run() -> dict:
    session_base_url = session.snapshot_base_url()
    base_url, _ = pipeline.resolve_base_url(session_base_url)
    if not base_url:
        raise HTTPException(
            400,
            "No target set — enter one in step 4 (Target) or add API_BASE_URL to .env.",
        )
    try:
        return jobs.submit(
            "run",
            lambda: pipeline.run_generated_tests(
                preferred_files=session.snapshot_uploads(),
                base_url=session_base_url,
                progress=jobs.report,
            ),
        )
    except JobInProgress as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/jobs/full")
def start_full(request: FullRequest) -> dict:
    """Run the complete pipeline: scope + coverage/run/review options."""
    if request.scope not in ("operation", "all"):
        raise HTTPException(400, "scope must be 'operation' or 'all'.")
    if not session.summarized_operations():
        raise _no_spec()
    index = None
    if request.scope == "operation":
        if request.operation_index is None:
            raise HTTPException(400, "Pick an operation in step 3 or choose all operations.")
        try:
            session.operation(request.operation_index)
        except IndexError as exc:
            raise HTTPException(404, str(exc)) from exc
        index = request.operation_index
    if (request.run_tests or request.review) and not pipeline.resolve_base_url(
        session.snapshot_base_url()
    )[0]:
        raise HTTPException(
            400,
            "No target set — enter one in step 4 (Target) or add API_BASE_URL to .env.",
        )
    providers = pipeline.health(session, FIXTURE_DATA_PATH, FIXTURES_DIR)["providers"]
    if request.coverage and not providers["GEMINI_API_KEY"]:
        raise HTTPException(400, "Coverage audit needs GEMINI_API_KEY in .env.")
    if request.review and not providers["OPENROUTER_API_KEY"]:
        raise HTTPException(400, "Review needs OPENROUTER_API_KEY in .env.")
    try:
        return jobs.submit(
            "full",
            lambda: pipeline.run_full_pipeline(
                session,
                operation_index=index,
                run_all=request.scope == "all",
                coverage=request.coverage,
                run_tests=request.run_tests,
                review=request.review,
                base_url=session.snapshot_base_url(),
                preferred_files=session.snapshot_uploads(),
                progress=jobs.report,
            ),
        )
    except JobInProgress as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/jobs/current")
def current_job() -> dict:
    return jobs.snapshot()


@app.get("/api/artifacts/plans")
def get_plans(download: bool = False) -> Response:
    if not DEFAULT_PLANS_PATH.exists():
        raise HTTPException(404, "No plans generated yet.")
    as_attachment = {"content_disposition_type": "attachment", "filename": "test_plans.json"} if download else {}
    return FileResponse(DEFAULT_PLANS_PATH, media_type="application/json", **as_attachment)


@app.get("/api/artifacts/tests")
def get_tests(download: bool = False) -> Response:
    if not DEFAULT_TESTS_PATH.exists():
        raise HTTPException(404, "No test file generated yet.")
    as_attachment = {"content_disposition_type": "attachment", "filename": "test.py"} if download else {}
    return FileResponse(DEFAULT_TESTS_PATH, media_type="text/x-python", **as_attachment)


@app.get("/api/artifacts/results")
def get_results(download: bool = False) -> Response:
    if not DEFAULT_RESULTS_PATH.exists():
        raise HTTPException(404, "No execution results yet — run the tests first.")
    as_attachment = {"content_disposition_type": "attachment", "filename": "test_results.json"} if download else {}
    return FileResponse(DEFAULT_RESULTS_PATH, media_type="application/json", **as_attachment)


@app.get("/api/artifacts/pytest-source")
def pytest_source() -> PlainTextResponse:
    """Raw generated suite for the in-page viewer."""
    if not DEFAULT_TESTS_PATH.exists():
        raise HTTPException(404, "No test file generated yet.")
    return PlainTextResponse(DEFAULT_TESTS_PATH.read_text(encoding="utf-8"))


@app.get("/api/artifacts/coverage")
def get_coverage(download: bool = False) -> Response:
    if not DEFAULT_COVERAGE_PATH.exists():
        raise HTTPException(404, "No coverage report yet — run the pipeline with coverage.")
    as_attachment = {"content_disposition_type": "attachment", "filename": "coverage_report.json"} if download else {}
    return FileResponse(DEFAULT_COVERAGE_PATH, media_type="application/json", **as_attachment)


@app.get("/api/artifacts/review-log")
def get_review_log(download: bool = False) -> Response:
    if not DEFAULT_REVIEW_LOG_PATH.exists():
        raise HTTPException(404, "No review log yet — run the pipeline with review.")
    as_attachment = {"content_disposition_type": "attachment", "filename": "rewrite_log.json"} if download else {}
    return FileResponse(DEFAULT_REVIEW_LOG_PATH, media_type="application/json", **as_attachment)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":
    import uvicorn

    print("Serving the test-generation UI on http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)
