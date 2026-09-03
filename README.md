# Agentic Test Generator

Generate executable API tests from an OpenAPI/Swagger document or a Postman
collection using an LLM-powered [LangGraph](https://www.langchain.com/langgraph)
workflow.

The generator:

- discovers happy-path, negative, and boundary scenarios;
- builds concrete request and response assertions;
- renders one readable `pytest` function per scenario;
- optionally audits coverage, executes tests against a live API, and performs
  one constrained rewrite pass for failures.

Normal generation is offline: the target API is contacted only with
`--run-tests`, `--review`, or when running the generated suite.

## Workflow

```text
API specification
      │
      ▼
 ingest and resolve $refs
      │
      ▼
 plan scenarios ──► build test plans ──► render pytest
                                           │
                         ┌─────────────────┼─────────────────┐
                         ▼                 ▼                 ▼
                    coverage audit     run tests        review failures
```

The planner focuses on documented behavior: valid request shapes, required
fields and parameters, enums, conditionals, boundaries, and documented status
codes. The builder uses schema examples and configured fixtures to make each
scenario executable.

## Quick start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure model access

Create a `.env` file in the repository root:

```env
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
```

Planner and builder models are configured in
[`src/workflow/utils/nodes.py`](src/workflow/utils/nodes.py).

### 3. Add an API source

Place your source at `spec.json` (or provide another path with `--spec`).
OpenAPI JSON/YAML, Swagger, and Postman Collection v2 documents are supported.

### 4. Generate tests

```bash
cd src
python main.py --all
```

This writes:

- `src/helpers/test_plans.json` — generated test plans;
- `src/helpers/test.py` — executable pytest tests.

Generate only one operation with `python main.py --index 2`.

## Run against an API

Set the target URL and API key:

```env
API_BASE_URL=https://api.example.com
DIGIEXPERT_API_KEY=...
```

Then run the generated tests:

```bash
pytest src/helpers/test.py
```

Or let the workflow execute plans directly:

```bash
cd src
python main.py --all --run-tests
```

After failures, run one constrained review and verification pass:

```bash
python main.py --all --review
```

Coverage auditing and gap filling are available with:

```bash
python main.py --all --coverage
```

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

The builder may reference them as `<FIXTURE:expert_code>`. Values are resolved
at test time inside request bodies, paths, query parameters, and headers.

### Photos and documents

Copy files into [`src/helpers/fixture/`](src/helpers/fixture/) and reference
them as `<FILE:passport.pdf>` or `<FILE:identity_card.jpg>`. Multipart uploads
are opened and closed automatically during each request. If the exact filename
is unavailable, the runner looks for a file with the same extension.

### Secrets and external values

Use `<ENV:VARIABLE_NAME>` for secrets or values that should remain outside the
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
tests/                              Unit and integration tests
```

Generated artifacts and the confidential API specification are gitignored.
Generated `test.py` should be regenerated rather than edited by hand.

## Tuning

Builder performance can be adjusted in `src/workflow/utils/nodes.py`:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `BUILD_BATCH_SIZE` | 4 | Scenarios per builder call |
| `MAX_CONCURRENT_CALLS` | 3 | Simultaneous model requests |
| `MAX_CALLS_PER_SECOND` | 2 | Sustained request rate |
| `MAX_RETRIES` | 5 | Retries for rate limits, timeouts, and 503s |

## Development

Run the test suite from the repository root:

```bash
pytest
```

## License

Add your project license here before publishing.
