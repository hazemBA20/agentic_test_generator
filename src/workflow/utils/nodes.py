import asyncio
import json
import os
import random
import time

from dotenv import load_dotenv
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openrouter import ChatOpenRouter
from langchain_google_genai import ChatGoogleGenerativeAI

from workflow.utils.models import State, ScenarioSpec, Scenarios, TestPlan, TestPlans
from workflow.utils.prompts import (
    SCENARIO_PLANNER_SYSTEM_PROMPT,
    SCENARIO_PLANNER_USER_PROMPT,
    TEST_BUILDER_SYSTEM_PROMPT,
    TEST_BUILDER_USER_PROMPT,
)

load_dotenv()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

MODEL_NAME = "google/gemini-2.5-flash"

model = ChatOpenRouter(
    model=MODEL_NAME,
    temperature=0,
    max_tokens=8000,
)




GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_MODEL="gemini-3.1-flash-lite"
llm = ChatGoogleGenerativeAI(
    model=GOOGLE_MODEL,
     google_api_key=GEMINI_API_KEY,
    temperature=0.2,
    max_output_tokens=8000,
)





scenario_planner = model.with_structured_output(Scenarios)
test_builder = llm.with_structured_output(TestPlans)

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


def call_llm_1(state: State) -> dict:
    """Planner node: turn each OpenAPI operation into a list of test scenarios."""
    operations = state["operations"]
    scenarios_per_operation = []

    for operation in operations:
        op_payload = json.dumps(operation, ensure_ascii=False, default=str)
        user_content = SCENARIO_PLANNER_USER_PROMPT.format(operation=op_payload)
        try:
            msg = scenario_planner.invoke(
                [SystemMessage(content=SCENARIO_PLANNER_SYSTEM_PROMPT), HumanMessage(content=user_content)]
            )
            scenarios_per_operation.append(msg.scenarios)
        except Exception as e:
            print(f"Error invoking LLM for operation {operation.get('path', '')}: {e}")
            scenarios_per_operation.append([])

    return {"scenarios": scenarios_per_operation}


async def _build_batch(operation: dict, batch: list[ScenarioSpec]) -> list[TestPlan]:
    async def _invoke():
        op_payload = json.dumps(operation, ensure_ascii=False, default=str)
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

    return plans


async def _build_all(operations: list[dict], scenarios_per_operation: list[list[ScenarioSpec]]) -> list[TestPlan]:
    tasks = [
        _build_batch(operation, batch)
        for operation, scenarios in zip(operations, scenarios_per_operation)
        for batch in _chunked(scenarios, BUILD_BATCH_SIZE)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_plans = []
    for result in results:
        if isinstance(result, Exception):
            print(f"Dropping a batch after exhausting retries: {result}")
            continue
        all_plans.extend(result)
    return all_plans


def call_llm_2(state: State) -> dict:
    """Builder node: turn each planned scenario into a concrete, executable TestPlan."""
    operations = state["operations"]
    scenarios_per_operation = state.get("scenarios") or []
    if not any(scenarios_per_operation):
        return {"plans": []}

    all_plans = asyncio.run(_build_all(operations, scenarios_per_operation))
    return {"plans": all_plans}