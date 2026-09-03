import asyncio
import json
import os
import random
import re
import time
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from helpers.coverage import (
    audit_operation, plans_for_operation, required_body_fields, UNREACHABLE_STATUSES,
)
from workflow.utils.models import (
    State, ScenarioSpec, Scenarios, TestPlan, TestPlans, CoverageGaps,
)
from workflow.utils.prompts import (
    SCENARIO_PLANNER_SYSTEM_PROMPT,
    SCENARIO_PLANNER_USER_PROMPT,
    TEST_BUILDER_SYSTEM_PROMPT,
    TEST_BUILDER_USER_PROMPT,
    COVERAGE_AUDITOR_SYSTEM_PROMPT,
    COVERAGE_AUDITOR_USER_PROMPT,
)

load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")


FIXTURE_DATA_PATH = Path(__file__).resolve().parents[2] / "helpers" / "fixture" / "test_data.json"




GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Overridable so a quota-exhausted model can be swapped without a code edit,
# the same way rewrite_failed.py takes REWRITE_MODEL.
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-3.5-flash")
llm = ChatGoogleGenerativeAI(
    model=GOOGLE_MODEL,
     google_api_key=GEMINI_API_KEY,
    temperature=0.2,
    max_output_tokens=7900,
)


# Groq — fast OpenAI-compatible inference for open models. The .env key is the
# lowercase `groq_key`; fall back to the conventional GROQ_API_KEY too. Model is
# overridable like GOOGLE_MODEL; the default is the strongest general model this
# key currently serves (run `Groq().models.list()` to see the live lineup — it
# rotates, and there is no Llama 3.x on it right now).
GROQ_API_KEY = os.getenv("groq_key") or os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
groq_llm = ChatGroq(
    model=GROQ_MODEL,
    api_key=GROQ_API_KEY,
    temperature=0,
    max_tokens=8000,
)





scenario_planner = llm.with_structured_output(Scenarios)
test_builder =  groq_llm.with_structured_output(TestPlans)
# Auditing coverage is a judgment task like planning, not payload construction,
# so it shares the planner's model rather than the builder's.
coverage_auditor = llm.with_structured_output(CoverageGaps)

# --- tuning knobs for the builder node -----------------------------------
# Scenarios per LLM call. Small enough that output can't get truncated and
# the model isn't juggling too many focuses at once; large enough that you're
# not paying the full operation-schema context on every single scenario.
# Shrink this for operations with big/complex request schemas, raise it for
# tiny ones (e.g. /version has nothing to batch-size around).
BUILD_BATCH_SIZE = 4
MAX_CONCURRENT_CALLS = 3      # in-flight requests at once
MAX_CALLS_PER_SECOND = 2      # sustained request rate across ALL calls
MAX_RETRIES = 5


class _AsyncTokenBucket:
    """Caps calls to `rate` per `per` seconds, shared across every coroutine
    that awaits .acquire(). Smooths bursts instead of firing every batch at
    once, independent of whatever rate-limiting (if any) the model wrapper
    itself does."""

    def __init__(self, rate: float, per: float = 1.0):
        self.rate = rate
        self.per = per
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.rate, self._tokens + (now - self._last) * (self.rate / self.per))
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) * (self.per / self.rate))


_rate_limiter = _AsyncTokenBucket(rate=MAX_CALLS_PER_SECOND, per=1.0)
_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

_loop: asyncio.AbstractEventLoop | None = None


def _run(coro):
    """Run a coroutine on one event loop that lives as long as the process.

    ``asyncio.run`` would open and close a loop per call, but the model clients
    cache an async HTTP pool bound to the loop that first used them, and the
    module-level semaphore/rate-limiter bind the same way. A second
    ``asyncio.run`` in one process then dies with "Event loop is closed" — which
    is exactly what the coverage fill pass does, since it invokes the builder a
    second time after the builder node already ran.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop.run_until_complete(coro)


@lru_cache(maxsize=1)
def _available_fixture_keys() -> list[str]:
    if not FIXTURE_DATA_PATH.exists():
        return []
    data = json.loads(FIXTURE_DATA_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{FIXTURE_DATA_PATH} must contain a JSON object")
    return sorted(data)



def _builder_operation(operation: dict) -> dict:
    """Give the builder fixture names without exposing their configured values."""
    return {**operation, "available_fixture_keys": _available_fixture_keys()}


def _content_type(operation: dict) -> str:
    op = operation.get("operation", {})
    content = (op.get("requestBody") or {}).get("content", {})
    if "multipart/form-data" in content:
        return "multipart/form-data"
    if "application/x-www-form-urlencoded" in content:
        return "application/x-www-form-urlencoded"
    return "application/json"


def _is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "timeout" in text or "503" in text


async def _call_with_retry(coro_fn):
    for attempt in range(MAX_RETRIES):
        try:
            await _rate_limiter.acquire()
            return await coro_fn()
        except Exception as e:
            if attempt == MAX_RETRIES - 1 or not _is_retryable(e):
                raise
            backoff = min(30, 2 ** attempt) + random.uniform(0, 1)
            print(f"Retryable error ({e}); backing off {backoff:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})")
            await asyncio.sleep(backoff)


def _chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _plan_operation(operation: dict) -> list[ScenarioSpec]:
    """Plan one operation under the same throttle as the builder."""
    async def _invoke():
        op_payload = json.dumps(operation, ensure_ascii=False, default=str)
        user_content = SCENARIO_PLANNER_USER_PROMPT.format(operation=op_payload)
        return await scenario_planner.ainvoke(
            [SystemMessage(content=SCENARIO_PLANNER_SYSTEM_PROMPT), HumanMessage(content=user_content)]
        )

    async with _semaphore:
        msg = await _call_with_retry(_invoke)
    return msg.scenarios


async def _plan_all(operations: list[dict]) -> tuple[list[list[ScenarioSpec]], int]:
    results = await asyncio.gather(
        *(_plan_operation(operation) for operation in operations),
        return_exceptions=True,
    )
    scenarios_per_operation: list[list[ScenarioSpec]] = []
    failed = 0
    for operation, result in zip(operations, results):
        if isinstance(result, Exception):
            failed += 1
            print(
                f"Planning failed for {operation.get('method', '')} {operation.get('path', '')} "
                f"after retries: {result}"
            )
            scenarios_per_operation.append([])
            continue
        scenarios_per_operation.append(result)
    return scenarios_per_operation, failed


def plan_scenarios(operations: list[dict]) -> tuple[list[list[ScenarioSpec]], int]:
    """Turn operations into per-operation scenarios, and count planning losses."""
    return _run(_plan_all(operations))


def call_llm_1(state: State) -> dict:
    """Planner node: turn each OpenAPI operation into a list of test scenarios."""
    print("Invoking scenario planner LLM...")
    scenarios_per_operation, failed = plan_scenarios(state["operations"])
    if failed:
        print(
            f"WARNING: {failed} operation(s) produced no scenarios — this suite is incomplete."
        )
    return {"scenarios": scenarios_per_operation, "build_failures": failed}


def _hollow_reason(plan: TestPlan, required: list[str]) -> str | None:
    """Why a plan that sends no body at all cannot test what it claims to.

    A ``null`` request_body means the model omitted the field rather than choosing
    to send nothing — an explicit ``{}`` is how a deliberate empty-body test is
    written. Left in the suite, such a plan looks like a real failure: the server
    rejects on the *first* missing required field, not the one the test names, so
    a "missing expertCode" test reports "missionReference is required" and the
    reviewer burns a call working out that the plan, not the assertion, is wrong.
    """
    if plan.request_body is not None or not required:
        return None
    return (
        f"request_body is null but the operation requires {', '.join(required)} — "
        "the server would reject on the first missing field, not the one under test"
    )


async def _build_batch(operation: dict, batch: list[ScenarioSpec]) -> list[TestPlan]:
    async def _invoke():
        op_payload = json.dumps(_builder_operation(operation), ensure_ascii=False, default=str)
        scenarios_payload = json.dumps([s.model_dump() for s in batch], ensure_ascii=False)
        user_content = TEST_BUILDER_USER_PROMPT.format(operation=op_payload, scenarios=scenarios_payload)
        return await test_builder.ainvoke(
            [SystemMessage(content=TEST_BUILDER_SYSTEM_PROMPT), HumanMessage(content=user_content)]
        )

    async with _semaphore:
        msg = await _call_with_retry(_invoke)

    plans = msg.test_plans
    if len(plans) != len(batch):
        print(
            f"Warning: {operation.get('path', '')} batch returned {len(plans)} plans "
            f"for {len(batch)} scenarios sent — check for truncation or a dropped scenario."
        )

    # method/path are deterministic from the operation itself, so don't trust
    # the model to copy them correctly — backfill regardless of what it said.
    for plan in plans:
        plan.path = operation.get("path", plan.path)
        plan.method = operation.get("method", plan.method)
        plan.requires_api_key = True
        plan.requires_jwt = False
        plan.content_type = _content_type(operation)

    required = required_body_fields(operation)
    kept = []
    for plan in plans:
        reason = _hollow_reason(plan, required)
        if reason:
            print(f"Dropping {plan.name}: {reason}")
            continue
        kept.append(plan)

    return kept


async def _build_all(
    operations: list[dict], scenarios_per_operation: list[list[ScenarioSpec]]
) -> tuple[list[TestPlan], int]:
    tasks = [
        _build_batch(operation, batch)
        for operation, scenarios in zip(operations, scenarios_per_operation)
        for batch in _chunked(scenarios, BUILD_BATCH_SIZE)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_plans = []
    failed_batches = 0
    for result in results:
        if isinstance(result, Exception):
            failed_batches += 1
            print(f"Dropping a batch after exhausting retries: {result}")
            continue
        all_plans.extend(result)
    return all_plans, failed_batches


def build_plans_with_failures(
    operations: list[dict], scenarios_per_operation: list[list[ScenarioSpec]]
) -> tuple[list[TestPlan], int]:
    """Build plans, and report how many batches died on the way.

    A dropped batch is not a smaller suite by choice: it is scenarios the planner
    asked for that no longer exist. A caller about to overwrite a previous
    artifact has to know that before it writes, so the count is returned rather
    than only printed.
    """
    return _run(_build_all(operations, scenarios_per_operation))


def build_plans(
    operations: list[dict], scenarios_per_operation: list[list[ScenarioSpec]]
) -> list[TestPlan]:
    """Turn per-operation scenarios into plans, batched and rate-limited.

    Shared by the builder node and the coverage fill pass so both get the same
    batching, retries, and deterministic method/path/content_type backfill.
    """
    return build_plans_with_failures(operations, scenarios_per_operation)[0]


def call_llm_2(state: State) -> dict:
    """Builder node: turn each planned scenario into a concrete, executable TestPlan."""
    scenarios_per_operation = state.get("scenarios") or []
    if not any(scenarios_per_operation):
        print("No scenarios found in state; skipping test builder.")
        return {"plans": [], "build_failures": state.get("build_failures") or 0}
    print("Invoking test builder LLM...")

    planned = sum(len(scenarios) for scenarios in scenarios_per_operation)
    all_plans, failed_batches = build_plans_with_failures(
        state["operations"], scenarios_per_operation
    )
    if failed_batches:
        print(
            f"WARNING: {failed_batches} builder batch(es) failed. Built {len(all_plans)} "
            f"plan(s) from {planned} planned scenario(s) — this suite is incomplete."
        )

    # Accumulate rather than overwrite: planner losses already in state must
    # survive the builder's own count, or persist_plans loses its guarantee.
    return {
        "plans": all_plans,
        "build_failures": (state.get("build_failures") or 0) + failed_batches,
    }


# --- coverage agent -------------------------------------------------------

def _plan_digest(plan) -> dict:
    """What a plan actually sends, for judging coverage by behaviour not by name."""
    plan = plan.model_dump() if hasattr(plan, "model_dump") else dict(plan)
    return {
        "name": plan.get("name"),
        "category": plan.get("category"),
        "expected_status_code": plan.get("expected_status_code"),
        "request_body": plan.get("request_body"),
        "query_params": plan.get("query_params") or {},
        "headers": plan.get("headers") or {},
    }


async def _audit_batch(wrapper: dict, plans: list[dict], report: dict, unfilled: list[dict]):
    async def _invoke():
        user_content = COVERAGE_AUDITOR_USER_PROMPT.format(
            operation=json.dumps(wrapper, ensure_ascii=False, default=str),
            plans=json.dumps([_plan_digest(p) for p in plans], ensure_ascii=False, default=str),
            checklist=json.dumps(report["checklist"], ensure_ascii=False),
            unfilled=json.dumps(unfilled, ensure_ascii=False) if unfilled else "(none)",
        )
        return await coverage_auditor.ainvoke(
            [
                SystemMessage(content=COVERAGE_AUDITOR_SYSTEM_PROMPT),
                HumanMessage(content=user_content),
            ]
        )

    async with _semaphore:
        return await _call_with_retry(_invoke)


def _gap_status(gap) -> int | None:
    """The status code an LLM-reported gap is about, from its scenario or prose."""
    if gap.scenario is not None:
        return gap.scenario.target_status_code
    match = re.search(r"\b([1-5]\d\d)\b", gap.detail or "")
    return int(match.group(1)) if match else None


def _rejected_reason(gap, checklist: dict) -> str | None:
    """Why an LLM gap contradicts what the deterministic checklist already knows.

    The prompt asks the model not to report these, but a prompt is not an
    enforcement mechanism: the checklist has the facts, so it gets the last word.
    """
    if gap.kind == "status_code":
        status = _gap_status(gap)
        if status in UNREACHABLE_STATUSES:
            return f"{status} is excluded (auth/infra, not request-driven)"
        if status in (checklist.get("status_codes") or {}).get("covered", []):
            return f"{status} is already covered"
    if gap.kind == "happy_path" and checklist.get("happy_path"):
        return "the suite already has a happy path"
    return None


def _matching_unfilled(gap, candidates: list[dict]) -> dict | None:
    """The unfilled checklist gap an LLM gap is answering, if any.

    The auditor is asked to echo a handed-over gap's kind and detail unchanged,
    but a paraphrased detail shouldn't produce a duplicate entry — so status
    gaps match on the code they target and the happy-path gap matches on kind.
    """
    for existing in candidates:
        if existing.get("scenario") is not None or existing["kind"] != gap.kind:
            continue
        if gap.kind == "status_code":
            target = gap.scenario.target_status_code if gap.scenario else None
            if existing.get("status") == target or existing["detail"] == gap.detail:
                return existing
        elif gap.kind == "happy_path" or existing["detail"] == gap.detail:
            return existing
    return None


async def _audit_all(operations: list[dict], plans: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Deterministic checklist per operation, then one LLM pass per operation."""
    audits = []
    for wrapper in operations:
        report, gaps = audit_operation(wrapper, plans_for_operation(wrapper, plans))
        audits.append((wrapper, report, gaps))

    async def _for(wrapper, report, gaps):
        unfilled = [
            {"kind": gap["kind"], "detail": gap["detail"]}
            for gap in gaps
            if gap.get("scenario") is None
        ]
        return await _audit_batch(wrapper, plans_for_operation(wrapper, plans), report, unfilled)

    results = await asyncio.gather(
        *(_for(wrapper, report, gaps) for wrapper, report, gaps in audits),
        return_exceptions=True,
    )

    audited = []
    for (wrapper, report, gaps), result in zip(audits, results):
        if isinstance(result, Exception):
            print(f"Coverage audit failed for {wrapper.get('path', '')}: {result}")
            print("  Keeping the deterministic checklist result for this operation.")
        else:
            for gap in result.gaps:
                rejected = _rejected_reason(gap, report["checklist"])
                if rejected:
                    print(f"  Ignoring an LLM gap for {wrapper.get('path', '')}: {rejected}")
                    continue
                scenario = gap.scenario.model_dump() if gap.scenario else None
                # A gap the checklist found but couldn't name a condition for was
                # handed to the model; its answer completes that entry rather than
                # standing as a second gap for the same thing.
                answered = _matching_unfilled(gap, gaps)
                if answered is not None:
                    answered["scenario"] = scenario
                    answered["source"] = "checklist+llm"
                    continue
                gaps.append({
                    "kind": gap.kind,
                    "detail": gap.detail,
                    "scenario": scenario,
                    "source": "llm",
                })
        for gap in gaps:
            gap.setdefault("source", "checklist")
        report["gaps"] = gaps
        audited.append((report, gaps))
    return audited


def coverage_audit(state: State) -> dict:
    """Coverage node: report what the generated suite does not cover.

    Runs exactly once — ``coverage_done`` is set unconditionally so the second
    trip through ``render`` after a fill pass cannot re-enter this node.
    """
    print("Auditing generated coverage...")
    operations = state.get("operations") or []
    plans = [p.model_dump() if hasattr(p, "model_dump") else dict(p) for p in state.get("plans") or []]

    audited = _audit_all_sync(operations, plans)

    report = [entry for entry, _ in audited]
    gap_scenarios: list[list[ScenarioSpec]] = []
    for _, gaps in audited:
        scenarios = []
        for gap in gaps:
            if gap.get("scenario"):
                try:
                    scenarios.append(ScenarioSpec(**gap["scenario"]))
                except Exception as e:
                    print(f"  Discarding an unusable gap scenario: {e}")
                    gap["scenario"] = None
        gap_scenarios.append(scenarios)

    total_gaps = sum(len(gaps) for _, gaps in audited)
    fillable = sum(len(scenarios) for scenarios in gap_scenarios)
    print(
        f"Coverage audit: {total_gaps} gap(s) across {len(operations)} operation(s); "
        f"{fillable} with a scenario to fill."
    )
    for entry, gaps in audited:
        for gap in gaps:
            marker = "fill" if gap.get("scenario") else "report"
            print(f"  [{marker}] {entry['method']} {entry['path']} — {gap['kind']}: {gap['detail']}")

    return {
        "coverage_report": report,
        "coverage_gap_scenarios": gap_scenarios,
        "coverage_done": True,
    }


def _audit_all_sync(operations: list[dict], plans: list[dict]) -> list[tuple[dict, list[dict]]]:
    if not operations:
        return []
    return _run(_audit_all(operations, plans))


def _unique_name(name: str, used: set[str]) -> str:
    """Keep a filled plan under a distinct name.

    Duplicate plan names are treated as skips by the rewriter, so a gap plan
    that collides with an existing one gets suffixed rather than dropped.
    """
    candidate, n = name, 2
    while candidate in used:
        candidate = f"{name}_gap{n}"
        n += 1
    used.add(candidate)
    return candidate


def fill_gaps(state: State) -> dict:
    """Build plans for the coverage gaps and merge them into the suite."""
    gap_scenarios = state.get("coverage_gap_scenarios") or []
    if not any(gap_scenarios):
        return {"filled_count": 0}

    print(f"Filling {sum(len(s) for s in gap_scenarios)} coverage gap(s)...")
    new_plans, failed_batches = build_plans_with_failures(state["operations"], gap_scenarios)
    if failed_batches:
        print(
            f"WARNING: {failed_batches} fill batch(es) failed, so only "
            f"{len(new_plans)} of the {sum(len(s) for s in gap_scenarios)} gap scenario(s) "
            "became plans."
        )

    existing = list(state.get("plans") or [])
    used = {
        (plan.model_dump() if hasattr(plan, "model_dump") else plan).get("name")
        for plan in existing
    }
    used.discard(None)
    for plan in new_plans:
        plan.name = _unique_name(plan.name, used)

    print(f"Added {len(new_plans)} plan(s) from the coverage audit.")
    return {
        "plans": [*existing, *new_plans],
        "filled_count": len(new_plans),
        # A partially failed build is still partial no matter where it happened,
        # so persist_plans must know about fill losses before it overwrites.
        "build_failures": (state.get("build_failures") or 0) + failed_batches,
    }