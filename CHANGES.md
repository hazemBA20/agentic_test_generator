# What changed on the `fix-known-issues` branch

This document summarizes the work done on the `fix-known-issues` branch: the
problems that were fixed, the cleanup, the live-API validation run, and what
is still worth doing next. All work is committed in small steps, one topic
per commit, and the full test suite passes (54 tests).

---

## 1. Security: leaked credentials removed

- **`src/helpers/Provider.py` contained a hardcoded NVIDIA API key and was
  tracked in git.** The file was also dead code (nothing imported it). It was
  deleted with `git rm` and is no longer part of the repository.
- A scratch `test.py` at the repo root contained a hardcoded AgentRouter key.
  It was deleted from the working tree (it was already gitignored).
- A scan of every tracked file confirmed no other file contains secrets.

> **Important:** deleting a file does not remove the key from git history.
> Both keys should be treated as leaked and **rotated** in their provider
> consoles.

## 2. Real bugs fixed

### `review_errors` was only counted for the current pass

`review_failures` in `src/workflow/graph.py` counted model-error skips only
from the current review pass, so errors from an earlier pass disappeared from
the count after any later clean pass. It now counts over the cumulative log,
and the previously failing unit test passes.

### A planner failure could silently destroy an existing suite

The planner node caught every exception and wrote an empty scenario list. If
the planner failed for an operation during `--all`, the run would overwrite a
larger existing `test_plans.json` with fewer plans — and exit 0, so the suite
silently shrank. This was exactly the scenario the partial-build quarantine
was built for, but only the *builder* reported into it.

Fix: the planner now runs through the same token bucket, concurrency limit,
and retry/backoff as the builder, runs operations concurrently, and counts
planning losses as `build_failures`. The builder accumulates that count
instead of overwriting it, so `persist_plans` can refuse to shrink a suite no
matter where the loss happened.

### The generated test file did not compile

Live testing exposed this one: the Jinja template's `-%}` trim markers ate
the newline *and* the leading indentation of each function body's first line,
so `resp = send_request(...)` was emitted at column 0 and the generated
`test.py` died with `IndentationError`. The template was rewritten without
the whitespace-eating markers, and the renderer tests now `compile()` their
output so broken generation fails in CI instead of at runtime.

### The rewriter could patch response sentinels into requests

During the live run, the reviewer "fixed" a failing test by putting
`<GENERATED>` into `request_body` — but `<GENERATED>` is response-assertion
vocabulary, and the runner would have sent that literal string to the server.
`rewrite_failed.py` now refuses any request-field patch containing a
response-assertion sentinel (`<GENERATED>`, `<PRESENT>`, `<NON_NULL>`,
`<ANY_*>`) and logs it as a skip. Legitimate request-side sentinels
(`<FIXTURE:>`, `<FILE:>`, `<ENV:>`) still pass.

## 3. Cleanup

- **One renderer instead of two.** The Jinja2 implementation (recommended by
  `TEMPLATING_EVALUATION.md`) is now the single `generate_test_file.py`; the
  older `string.Template` version and its duplicated tests are gone. The
  template file's `test_functinon.j2` typo is fixed (`test_function.j2`).
- **Dead code removed:** `run_graph.py` (legacy entry point), the empty
  `workflow/utils/tools.py` and `helpers/reviewer.py`, the unused DeepSeek /
  OpenRouter client and commented-out block in `nodes.py`.
- **Model clients centralized.** `src/workflow/utils/provider.py` now holds
  one factory per provider (Gemini for planner + coverage auditor, Groq for
  the builder, OpenRouter for the failure rewriter) with the existing
  `GOOGLE_MODEL` / `GROQ_MODEL` / `REWRITE_MODEL` env overrides.
- **`src/helpers/rewrite_failed.py` keeps its own client** because it is also
  run as a standalone script and cannot import the `workflow` package.

## 4. Project hygiene

- **`requirements.txt`** was a raw `pip freeze` with 155 pinned packages
  (jupyter, pandas, fastapi, black, …). It now lists the 12 direct
  dependencies, pinned; everything else installs transitively.
- **`pyproject.toml` and `pyrightconfig.json` are now tracked** (they were
  gitignored despite containing no secrets), and pyproject pins
  `testpaths = ["tests"]` so pytest never collects generated suites.
- **`.env.example`** documents every supported variable.
- **CI**: `.github/workflows/ci.yml` installs the requirements and runs
  `pytest` on every push/PR. The suite is fully offline — no keys, no target
  API, no model calls needed.
- **README** updated: coverage agent in the pipeline, provider/model setup,
  the `--list` flag, and a pointer to `.env.example`.

## 5. New behavior worth knowing

- **`API_BASE_URL` must be set.** The old hardcoded fallback to a staging URL
  is gone; without the variable the runner raises a clear error instead of
  silently pointing generated write requests at a remembered environment.
- **`python main.py --list`** prints the operation table with indices, so you
  can pick a `--index` without opening the spec.

---

## 6. Live validation against the real API

Operation index 2 (`POST /experts/copilot/mission/add`) was generated and
executed against `https://test.patch.digiclaim.tn/api`:

- Groq rate limits (429) hit during generation were absorbed by the
  retry/backoff logic — no lost batches.
- **First run: 13 of 16 tests passed.**
- The one genuine plan gap (`FOREIGN_INSURANCE` + `TIERS` missing a
  `registrationNumber`) was fixed by the rewriter; the re-run confirmed the
  payload became valid (its 400 turned into a 409).
- The three remaining failures all hit the same wall: **409
  `EXPERT_MISSION_ALREADY_EXISTS`**. Every happy-path variant uses the same
  expert fixture, and the environment allows only **one active mission per
  expert**, so whichever variant runs first creates the mission and the rest
  collide. No request tweak can honestly fix that — it needs test-state
  isolation (see below).

## 7. What is still worth doing

1. **Rotate the leaked keys** (NVIDIA, AgentRouter) — they remain in git
   history.
2. **Test-state isolation** so multiple happy-path variants can run in
   sequence: distinct valid expert codes per variant in `test_data.json`, or
   a teardown step that closes the created mission between tests.
3. The bigger feature ideas from the review remain open: chained/stateful
   tests (create → use returned id), login/token auth flows (requires_jwt is
   still accepted but ignored), schema-based response validation via
   `jsonschema`, an LLM response cache for cheaper prompt iteration, and a
   combined HTML/markdown run report.
