# Agentic Test Generator

Generate executable API tests from an OpenAPI/Swagger document or a Postman
collection using an LLM-powered [LangGraph](https://www.langchain.com/langgraph)
workflow.

The generator:

- discovers happy-path, negative, and boundary scenarios from the specification;
- builds concrete request payloads and response assertions for each scenario;
- renders one readable `pytest` function per scenario;
- optionally audits spec coverage, executes tests against a live API, and
  performs one constrained rewrite pass over failures.

Test generation itself is offline: the target API is contacted only with
`--run-tests`, `--review`, or when running the generated suite.

## How it works

```text
API specification
      │
      ▼
 ingest and resolve $refs
      │
      ▼
 plan scenarios ──► build test plans ──► render pytest
                                           │
                         ┌────────────────┼────────────────┐
                         ▼                ▼                ▼
                   coverage audit     run tests      review failures
```

The planner focuses on documented behavior — valid request shapes, required
fields and parameters, enums, conditionals, boundaries, and documented status
codes. The builder combines schema examples with configured fixtures to make
each scenario executable. Invalid plans are quarantined as explicit
`pytest.skip` entries instead of failing the whole suite, and a partial build
never overwrites a larger existing suite.

## Quick start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure model access

Create a `.env` file in the repository root (see [`.env.example`](.env.example)
for every variable):

```env
GEMINI_API_KEY=...
groq_key=...
OPENROUTER_API_KEY=...
```

Model clients live in [`src/workflow/utils/provider.py`](src/workflow/utils/provider.py):

| Agent | Provider | Override |
| --- | --- | --- |
| Scenario planner | Gemini | `GOOGLE_MODEL` |
| Test builder | Groq | `GROQ_MODEL` |
| Coverage auditor | Groq | `GROQ_MODEL` |
| Failure reviewer | OpenRouter | `REWRITE_MODEL` |

Only the keys for the providers you use are required.

### 3. Add an API source

Place your source at `spec.json` (or pass another path with `--spec`).
OpenAPI JSON/YAML, Swagger, and Postman Collection v2 documents are supported.
List the operations and their indices:

```bash
python src/main.py --list
```

### 4. Generate tests

```bash
python src/main.py --all
```

This writes:

- `src/helpers/test_plans.json` — structured, reviewable test plans;
- `src/helpers/test.py` — the executable pytest suite.

Generate a single operation with `python src/main.py --index 2`.

## Running against a live API

Set the target URL and API key:

```env
API_BASE_URL=https://api.example.com
DIGIEXPERT_API_KEY=...
```

> [!WARNING]
> `--run-tests` and `--review` send real HTTP requests. Tests for mutating
> endpoints (`POST`, `PUT`, `PATCH`, `DELETE`) can create, update, or delete
> data — always point at a safe test environment.

Run the generated suite directly:

```bash
python -m dotenv run -- python -m pytest src/helpers/test.py -q
```

Or let the workflow execute plans and report results:

```bash
python -m dotenv run -- python src/main.py --all --run-tests
```

After failures, run one constrained review and verification pass:

```bash
python -m dotenv run -- python src/main.py --all --review
```

Audit coverage and fill gaps in one pass:

```bash
python src/main.py --all --coverage
```

(`python -m dotenv run --` preloads `.env`; omit it if the variables are
already exported.)

## Web UI

A local frontend drives the same pipeline — spec upload, fixture editing,
operation selection, generation, live execution, and review:

```bash
pip install -r web/requirements.txt
python -m web.server
```

Then open `http://127.0.0.1:8000`.

## Fixtures

Fixtures keep generated tests tied to real, valid data without hard-coding
environment-specific values into prompts or test code.

### Values

Add reusable values to
[`src/helpers/fixture/test_data.json`](src/helpers/fixture/test_data.json):

```json
{
  "expert_code": "MY_EXPERT_CODE",
  "mission_reference": "008/26"
}
```

The builder references them as `<FIXTURE:expert_code>`. Values resolve at
test time inside request bodies, paths, query parameters, and headers.

### Files

Copy upload samples into [`src/helpers/fixture/`](src/helpers/fixture/) and
reference them as `<FILE:passport.pdf>`. Multipart uploads are opened and
closed automatically per request; if the exact filename is unavailable, the
runner falls back to a file with the same extension.

### Secrets

Use `<ENV:VARIABLE_NAME>` for secrets or values that must stay out of the
repository, then define the environment variable before running tests.

## Repository layout

```text
src/
├── main.py                         CLI entry point
├── workflow/
│   ├── graph.py                    LangGraph workflow
│   └── utils/
│       ├── models.py               Pydantic state and plan models
│       ├── nodes.py                Planner, builder, coverage, and retries
│       ├── prompts.py              LLM instructions
│       └── provider.py             Model clients
└── helpers/
    ├── parser.py                   OpenAPI/Postman ingestion
    ├── generate_test_file.py       Plans → pytest
    ├── execute_plans.py            Plans → live API results
    ├── rewrite_failed.py           Constrained failure repair
    ├── _test_support.py            Runtime resolution and assertions
    └── fixture/                    Files and test_data.json
web/                                Local frontend over the same pipeline
tests/                              Unit and integration tests
```

Generated artifacts (`test_plans.json`, `test.py`, run reports) and the
confidential API specification are gitignored. Generated `test.py` is output:
correct the spec, fixtures, or plans and regenerate rather than editing it
by hand.

## Observability

Each run emits JSON-lines lifecycle events (`ingest`, `plans_persisted`,
`execution_completed`, `review_completed`, `coverage_completed`) to stderr
under a unique `run_id`, separate from CLI output. Set `LOG_LEVEL` to adjust
verbosity or `LOG_FILE` to also write events to a file. Request payloads and
fixture values are never logged.

## Tuning

Request pacing for model calls lives in `src/workflow/utils/nodes.py`:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `BUILD_BATCH_SIZE` | 4 | Scenarios per builder call |
| `MAX_CONCURRENT_CALLS` | 3 | Simultaneous model requests |
| `MAX_CALLS_PER_SECOND` | 2 | Sustained request rate |
| `MAX_RETRIES` | 5 | Retries for rate limits, timeouts, and 503s |

Lower the batch size or rate if a provider returns frequent `429`s; only
`429`, rate-limit, timeout, and `503` errors are retried.

## Limitations

- Generated authentication is API-key (`X-API-KEY`) based; JWT flows are
  accepted in plans but not executed.
- Only JSON and `multipart/form-data` request bodies are fully supported.
- The reviewer performs a single constrained patch pass, not repeated repair.
- Live tests depend on environment-specific data and state that the schema
  alone cannot provide; unseedable scenarios (e.g. conflicts requiring
  pre-existing duplicates) should be skipped with a note.

## Development

Run the offline test suite from the repository root (no keys or live API
required):

```bash
pytest
```

## License

Internal project — license text to be added before any external distribution.
