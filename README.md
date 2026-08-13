# agentic_test_generator

Generate executable API tests from an OpenAPI spec using a small
LLM-driven workflow built on [LangGraph](https://www.langchain.com/langgraph).

The pipeline takes an OpenAPI 3 document, plans the distinct test scenarios
each operation needs (happy path, negative, boundary), turns those scenarios
into concrete request/response test cases, and renders them into a single
plain-pytest file — no parametrized wrappers, just one real test function per
scenario.

## How it works

The workflow is a two-node LangGraph state machine:

```
OpenAPI spec
    │  parser.py ingests + resolves $refs
    ▼
┌────────────┐   scenarios per operation   ┌────────────┐
│  planner   │ ───────────────────────────▶ │  builder   │
│ (Gemini)   │   (category / status /      │  (DeepSeek) │
│ call_llm_1 │    focus)                    │  call_llm_2 │
└────────────┘                              └────────────┘
                                                   │ TestPlan list
                                                   ▼
                                     generate_test_file.py ──▶ test.py
```

1. **Ingest** — `src/helpers/parser.py` loads the spec, resolves every `$ref`
   (including nested ones), and extracts each HTTP operation into a
   self-contained payload with its resolved definitions.
2. **Plan** — `call_llm_1` asks the planner model to list the distinct
   scenarios an operation needs, following explicit coverage rules: one happy
   path per valid request shape, one negative per required field and per enum,
   one per documented conditional branch, boundary cases for named edge cases.
   It only targets status codes actually documented on the operation.
3. **Build** — `call_llm_2` turns each scenario into a concrete `TestPlan`
   (request body, expected status code, response-body assertions). The builder
   runs in batches with bounded concurrency, a shared token-bucket rate
   limiter, and exponential-backoff retries (`src/workflow/utils/nodes.py`).
   Request `method`/`path`/auth/`content_type` are backfilled from the
   operation deterministically rather than trusted from the model.
4. **Render** — `src/helpers/generate_test_file.py` reads the plans JSON and
   writes `test.py`, one pytest function per plan, with safe unique identifiers
   and literal payloads. Generated output — don't hand-edit it; regenerate.

## Project structure

```
├── spec.json                  # OpenAPI spec to generate from (gitignored)
├── src/
│   ├── main.py                # CLI entry point: spec → test plans JSON
│   ├── run_graph.py           # minimal single-operation runner
│   ├── workflow/
│   │   ├── graph.py           # LangGraph wiring
│   │   └── utils/
│   │       ├── models.py      # Pydantic models: State, ScenarioSpec, TestPlan
│   │       ├── prompts.py     # planner + builder system prompts
│   │       ├── nodes.py       # planner/builder nodes, batching, retries
│   │       └── provider.py    # LLM client setup
│   └── helpers/
│       ├── parser.py          # OpenAPI ingestion + $ref resolution
│       ├── generate_test_file.py  # plans JSON → test.py renderer
│       ├── _test_support.py   # runtime support for generated tests
│       ├── conftest.py        # fail-fast env validation for pytest
│       ├── test_plans.json    # generated plans (gitignored)
│       ├── test.py            # generated pytest file (gitignored)
│       └── fixture/           # sample.pdf / sample.jpg for file uploads
```

## Setup

```bash
pip install langgraph langchain-core langchain-openrouter \
            langchain-google-genai pydantic python-dotenv jsonref \
            requests pytest
```

Create a `.env` (see `.gitignore`) with the LLM keys:

```
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
```

The planner and builder can use different models — both are configured at the
top of `src/workflow/utils/nodes.py`.

## Usage

Generate test plans for the first operation of `spec.json`:

```bash
cd src
python main.py                # uses spec.json, operation 0, writes test_plans.json
```

Generate plans for every operation:

```bash
python main.py --all --out src/helpers/test_plans.json
```

Pick a specific operation:

```bash
python main.py --index 2
```

Render the plans into a pytest file:

```bash
cd src/helpers
python generate_test_file.py  # reads test_plans.json → test.py
```

### Running the generated tests

The generated tests hit a live API, so you need credentials and the target
base URL. The default base URL and the env var names below are fixed in
`src/helpers/_test_support.py` and `src/helpers/conftest.py`:

```
API_BASE_URL=<your server base url>
DIGIEXPERT_API_KEY=...
DIGIEXPERT_USERNAME=...
DIGIEXPERT_PASSWORD=...
```

Then:

```bash
pytest src/helpers/test.py
```

Support details in `_test_support.py`:

- **Auth** — `X-API-KEY` is attached when a plan requires it; a JWT is
  fetched lazily (and cached) only when a test actually needs one, so tests
  that don't require auth don't depend on the login endpoint being up.
- **File uploads** — a plan referencing `"<FILE:name>"` resolves to a real
  file under `fixtures/` and is sent as multipart form data.
- **Response assertions** — `assert_response` walks the expected dict; the
  sentinel `<GENERATED>` asserts a key exists without pinning its value, so
  server-generated ids/references don't cause brittle failures.

## Configuration

`config.yaml` currently holds the API base URL used by the generator itself
(`http://localhost:8080`). Tuning knobs for the builder live in
`src/workflow/utils/nodes.py`:

| Constant              | Default | Purpose                                     |
| --------------------- | ------- | ------------------------------------------- |
| `BUILD_BATCH_SIZE`    | 4       | Scenarios per builder LLM call              |
| `MAX_CONCURRENT_CALLS`| 3       | In-flight model requests                    |
| `MAX_CALLS_PER_SECOND`| 2       | Sustained request rate across all calls     |
| `MAX_RETRIES`         | 5       | Retries per batch on 429/timeout/5xx        |

## Notes

- This repo is an experimental test/agent harness under `tests/agentic`. It
  is coupled to the API described by `spec.json` and its auth scheme
  (`X-API-KEY` + bearer JWT). The spec itself is confidential and gitignored.
- Known limitations and planned work are tracked in
  `NEXT_IMPROVEMENTS.md` (config management, logging, Jinja2 templating, unit
  tests).
