/* Agentic Test Generator UI.
 * Flow: upload spec -> set fixtures -> pick operation -> generate (and optionally run).
 * Talks only to the local FastAPI backend in web/server.py. */
"use strict";

const state = {
  operations: [],
  selected: null,        // 0-based operation index
  health: null,
  fixtures: {},          // key -> value from the store
  fixtureFiles: [],
  dirty: {},             // key -> new value, awaiting save
  polling: null,         // interval id
  jobKind: null,
};

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return response.json();
}

async function apiText(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* non-JSON error body */ }
    throw new Error(detail);
  }
  return response.text();
}

function toast(message, isError = true) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 5000);
}

function methodBadge(method) {
  return `<span class="method ${esc(method).toLowerCase()}">${esc(method)}</span>`;
}

/* ---------- health ---------- */

async function refreshHealth() {
  try {
    state.health = await api("/api/health");
  } catch (error) {
    $("health").innerHTML = `<span class="chip bad">backend unreachable</span>`;
    return;
  }
  const chips = [];
  for (const [name, present] of Object.entries(state.health.providers)) {
    chips.push(`<span class="chip ${present ? "ok" : "bad"}" title="${present ? "configured" : "missing in .env"}">${esc(name)}</span>`);
  }
  chips.push(`<span class="chip ${state.health.api_base_url_set ? "ok" : "bad"}" title="${esc(state.health.base_url || "no target — set one in step 4 or add API_BASE_URL to .env")} (${esc(state.health.base_url_source || "none")})">API_BASE_URL</span>`);
  $("health").innerHTML = chips.join("");
  state.target = { url: state.health.base_url || null, source: state.health.base_url_source || "none" };
  renderTargetStatus();
  const hints = [];
  if (!state.health.providers.GEMINI_API_KEY) hints.push("coverage needs GEMINI_API_KEY");
  if (!state.target.url) hints.push("run/review need a target");
  if (!state.health.providers.OPENROUTER_API_KEY) hints.push("review needs OPENROUTER_API_KEY");
  $("options-hint").textContent = hints.length
    ? `Note: ${hints.join("; ")}.`
    : "All pipeline options available.";
  $("run-btn").disabled = !state.target.url;
}

function renderTargetStatus() {
  const el = $("target-status");
  if (!el) return;
  const target = state.target || { url: null, source: "none" };
  if (target.url) {
    el.innerHTML = `Target: <code>${esc(target.url)}</code> <span class="muted">(from ${target.source === "session" ? "this UI" : ".env"})</span>`;
  } else {
    el.textContent = "No target set — enter one above or add API_BASE_URL to .env.";
  }
}

async function saveTarget() {
  const raw = $("target-input").value;
  try {
    const body = await api("/api/target", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: raw }),
    });
    state.target = { url: body.base_url, source: body.base_url_source };
    toast(body.base_url ? "Target saved." : "Target cleared — using .env.", false);
    refreshHealth();
  } catch (error) {
    toast(`Invalid target: ${error.message}`);
  }
}

async function clearTarget() {
  $("target-input").value = "";
  await saveTarget();
}

/* ---------- step 1: upload ---------- */

async function uploadSpec(file) {
  const form = new FormData();
  form.append("spec", file);
  $("upload-result").classList.add("hidden");
  toast("Parsing spec…", false);
  try {
    const body = await api("/api/spec", { method: "POST", body: form });
    state.operations = body.operations;
    state.selected = null;
    toast(`Parsed ${body.spec_name}: ${body.operations.length} operation(s)`, false);
    renderUploadResult(body);
    renderOperations();
    renderNeedsOpSelect();
    refreshHealth();
  } catch (error) {
    toast(`Parse failed: ${error.message}`);
  }
}

function renderUploadResult(body) {
  const result = $("upload-result");
  result.classList.remove("hidden");
  result.innerHTML = `
    <p><strong>${esc(body.spec_name)}</strong> — ${body.operations.length} operation(s) found.</p>
    <p class="muted">Next: review fixtures (step 2), then pick an operation (step 3).</p>`;
}

function wireUpload() {
  const input = $("spec-input");
  input.addEventListener("change", () => { if (input.files[0]) uploadSpec(input.files[0]); });
  const drop = $("dropzone");
  for (const type of ["dragover", "dragenter"]) {
    drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.add("over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.remove("over"); });
  }
  drop.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) uploadSpec(file);
  });
}

/* ---------- step 2: fixtures ---------- */

async function loadFixtures() {
  const body = await api("/api/fixtures");
  state.fixtures = body.data;
  state.fixtureFiles = body.files;
  state.sessionUploads = body.session_uploads || [];
  state.dirty = {};
  renderFixtureTable();
  renderFixtureFiles();
}

function renderFixtureTable() {
  const tbody = $("fixture-rows");
  const keys = [...new Set([...Object.keys(state.fixtures), ...Object.keys(state.dirty)])].sort();
  tbody.innerHTML = keys.map((key) => {
    const value = key in state.dirty ? state.dirty[key] : state.fixtures[key];
    const valueText = typeof value === "string" ? value : JSON.stringify(value);
    const changed = key in state.dirty && JSON.stringify(state.dirty[key]) !== JSON.stringify(state.fixtures[key]);
    return `
      <tr data-key="${esc(key)}">
        <td class="key-cell">${esc(key)}${changed ? ' <span class="chip changed">edited</span>' : ""}</td>
        <td><input class="input cell" data-key="${esc(key)}" value="${esc(valueText)}"></td>
        <td><button class="button subtle danger" data-delete="${esc(key)}" type="button">✕</button></td>
      </tr>`;
  }).join("") || `<tr><td colspan="3" class="muted">No fixture keys defined yet.</td></tr>`;

  tbody.querySelectorAll("input.cell").forEach((input) => {
    input.addEventListener("change", () => {
      const key = input.dataset.key;
      const raw = input.value;
      let parsed = raw;
      try { parsed = JSON.parse(raw); } catch (_) { /* keep as string */ }
      state.dirty[key] = parsed;
      renderFixtureTable();
    });
  });
  tbody.querySelectorAll("button[data-delete]").forEach((button) => {
    button.addEventListener("click", () => deleteKey(button.dataset.delete));
  });
  updateSaveButton();
}

function updateSaveButton() {
  let button = $("save-keys");
  if (!button) {
    button = document.createElement("button");
    button.id = "save-keys";
    button.className = "button primary";
    button.type = "button";
    button.textContent = "Save changes";
    button.addEventListener("click", saveFixtureEdits);
    $("add-key").parentElement.appendChild(button);
  }
  button.disabled = Object.keys(state.dirty).length === 0;
}

async function saveFixtureEdits() {
  if (!Object.keys(state.dirty).length) return;
  try {
    const body = await api("/api/fixtures/data", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ updates: state.dirty }),
    });
    state.fixtures = body.data;
    state.dirty = {};
    toast("Fixture data saved.", false);
    renderFixtureTable();
    refreshSelectedNeeds();
    refreshHealth();
  } catch (error) {
    toast(`Saving fixtures failed: ${error.message}`);
  }
}

async function addKey() {
  const key = $("new-key").value.trim();
  const raw = $("new-value").value;
  if (!key) { toast("Give the fixture a key."); return; }
  let value = raw;
  try { value = JSON.parse(raw); } catch (_) { /* keep as string */ }
  state.dirty[key] = value;
  $("new-key").value = "";
  $("new-value").value = "";
  renderFixtureTable();
  await saveFixtureEdits();
}

async function deleteKey(key) {
  try {
    const body = await api(`/api/fixtures/data/${encodeURIComponent(key)}`, { method: "DELETE" });
    state.fixtures = body.data;
    delete state.dirty[key];
    toast(`Removed ${key}.`, false);
    renderFixtureTable();
    refreshSelectedNeeds();
  } catch (error) {
    toast(`Delete failed: ${error.message}`);
  }
}

function renderFixtureFiles() {
  const preferred = new Set(state.sessionUploads || []);
  $("fixture-files").innerHTML = state.fixtureFiles.map((name) =>
    `<li><code>${esc(name)}</code>${preferred.has(name) ? ` <span class="chip ok" title="Uploaded this session — used first at run time">this session</span>` : ""}</li>`).join("")
    || `<li class="muted">no files yet</li>`;
}

async function uploadFixtureFile(file) {
  const form = new FormData();
  form.append("file", file);
  try {
    const body = await api("/api/fixtures/files", { method: "POST", body: form });
    state.fixtureFiles = body.files;
    state.sessionUploads = body.session_uploads || [];
    renderFixtureFiles();
    toast(`Saved fixture file ${body.saved}.`, false);
    refreshSelectedNeeds();
  } catch (error) {
    toast(`Fixture upload failed: ${error.message}`);
  }
}

function wireFixtures() {
  $("add-key").addEventListener("click", addKey);
  $("needs-refresh").addEventListener("click", refreshSelectedNeeds);
  $("needs-op").addEventListener("change", (event) => {
    const index = event.target.value === "" ? null : Number(event.target.value);
    if (index !== null) selectOperation(index, { syncList: true });
  });
  $("fixture-file-input").addEventListener("change", (event) => {
    const file = event.target.files[0];
    if (file) uploadFixtureFile(file);
  });
}

/* ---------- fixture needs (pre-scan) ---------- */

async function refreshSelectedNeeds() {
  if (state.selected === null) return;
  await renderNeeds(state.selected);
}

async function renderNeeds(index) {
  const panel = $("needs-panel");
  panel.innerHTML = `<p class="muted">Scanning…</p>`;
  let needs;
  try {
    needs = await api(`/api/fixture-needs?operation_index=${index}`);
  } catch (error) {
    panel.innerHTML = `<p class="muted">Scan failed: ${esc(error.message)}</p>`;
    return;
  }
  const parts = [];

  if (needs.file_needs.length) {
    parts.push(`<h4>File uploads needed</h4><ul>${needs.file_needs.map((need) =>
      `<li><code>${esc(need.field_path)}</code> <span class="muted">(${esc(need.media_type)})</span> → provide a
       file in <code>src/helpers/fixture/</code> and the builder will use <code>&lt;FILE:...&gt;</code></li>`).join("")}</ul>`);
  }
  if (needs.auth.length) {
    parts.push(`<h4>Auth</h4><ul>${needs.auth.map((scheme) =>
      `<li>${esc(scheme.type)}${scheme.name ? ` — <code>${esc(scheme.name)}</code>` : ""}
       <span class="muted">the runner attaches the configured key automatically for plans that need it</span></li>`).join("")}</ul>`);
  }
  if (needs.suggested_keys.length) {
    parts.push(`<h4>Fields you may want as fixture keys</h4>
      <table class="table"><thead><tr><th>Field</th><th>In</th><th>Fixture key</th></tr></thead><tbody>
      ${needs.suggested_keys.map((item) => {
        const covered = item.covered
          ? `<span class="chip ok">${esc(item.covered)}</span>`
          : `<button class="button subtle" data-suggest="${esc(item.field)}" type="button">add ${esc(item.field.toLowerCase())}…</button>`;
        return `<tr><td><code>${esc(item.field)}</code></td><td>${esc(item.in)}</td><td>${covered}</td></tr>`;
      }).join("")}</tbody></table>`);
  }
  if (!parts.length) {
    parts.push(`<p class="muted">No obvious fixture needs detected for this operation — the builder can fill everything from the schema.</p>`);
  }
  panel.innerHTML = parts.join("");
  panel.querySelectorAll("button[data-suggest]").forEach((button) => {
    button.addEventListener("click", () => {
      $("new-key").value = button.dataset.suggest.toLowerCase();
      $("new-value").focus();
      $("new-key").scrollIntoView({ block: "center" });
    });
  });
}

/* ---------- step 3: operations ---------- */

function renderOperations() {
  const container = $("operations");
  if (!state.operations.length) {
    container.innerHTML = `<p class="muted">Upload a spec first.</p>`;
    return;
  }
  container.innerHTML = state.operations.map((op) => `
    <label class="operation ${state.selected === op.index ? "selected" : ""}" data-index="${op.index}">
      <input type="radio" name="operation" value="${op.index}" ${state.selected === op.index ? "checked" : ""}>
      ${methodBadge(op.method)}
      <code class="op-path">${esc(op.path)}</code>
      <span class="muted op-summary">${esc(op.summary)}</span>
      ${op.content_type !== "application/json" ? `<span class="chip">${esc(op.content_type)}</span>` : ""}
    </label>`).join("");
  container.querySelectorAll("label.operation").forEach((label) => {
    label.addEventListener("click", () => selectOperation(Number(label.dataset.index)));
  });
}

function selectOperation(index, { syncList = false } = {}) {
  state.selected = index;
  const op = state.operations.find((o) => o.index === index);
  $("selected-label").innerHTML = op
    ? `selected: ${methodBadge(op.method)} <code>${esc(op.path)}</code>`
    : "no operation selected";
  if (syncList) renderOperations();
  else {
    document.querySelectorAll("label.operation").forEach((label) =>
      label.classList.toggle("selected", Number(label.dataset.index) === index));
    const radios = document.querySelectorAll("input[name=operation]");
    radios.forEach((radio) => { radio.checked = Number(radio.value) === index; });
  }
  const select = $("needs-op");
  if ([...select.options].some((option) => option.value === String(index))) select.value = String(index);
  renderNeeds(index);
}

function renderNeedsOpSelect() {
  const select = $("needs-op");
  select.innerHTML = `<option value="">— pick an operation —</option>` +
    state.operations.map((op) =>
      `<option value="${op.index}">${op.index + 1}. ${esc(op.method)} ${esc(op.path)}</option>`).join("");
  if (state.selected !== null) select.value = String(state.selected);
  updateScopeLabels();
}

function updateScopeLabels() {
  $("scope-all-count").textContent = state.operations.length;
}

/* ---------- step 4: generate / run ---------- */

async function startPipeline() {
  const scope = document.querySelector("input[name=scope]:checked").value;
  let operation_index = null;
  if (scope === "operation") {
    if (state.selected === null) { toast("Pick an operation in step 3 first."); return; }
    operation_index = state.selected;
  }
  const coverage = $("opt-coverage").checked;
  const review = $("opt-review").checked;
  const run_tests = $("opt-run").checked || review;
  if (run_tests && !(state.target && state.target.url)) {
    toast("Set a target in step 4 (or API_BASE_URL in .env) before running.");
    return;
  }
  await submitJob(`/api/jobs/full`, { scope, operation_index, coverage, run_tests, review });
}

async function startRun() {
  await submitJob(`/api/jobs/run`, {});
}

async function submitJob(url, body) {
  clearInterval(state.polling);
  setBusy(true);
  $("job-error").classList.add("hidden");
  try {
    const started = await api(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    state.jobKind = started.kind;
    state.jobStartedAt = Date.now();
    state.noteIndex = 0;
    state.noteStage = null;
    showProgress(started.kind);
    pollJob();
  } catch (error) {
    setBusy(false);
    toast(`Could not start job: ${error.message}`);
  }
}

const JOB_STAGES = {
  generate: ["planning", "building", "rendering", "validating"],
  run: ["running"],
  full: [], // dynamic: steps appear as graph nodes complete
};

const STAGE_LABELS = {
  planning: "Planning scenarios",
  building: "Building test plans",
  rendering: "Rendering pytest",
  validating: "Validating plans",
  running: "Sending live requests",
};

const FUN_NOTES = {
  planning: [
    "Reading your OpenAPI like a novel…",
    "Spotting happy paths, edge cases and traps…",
    "The planner is thinking in scenarios…",
  ],
  building: [
    "Turning scenarios into concrete requests…",
    "Wiring fixtures and assertions…",
    "Teaching pytest new tricks…",
  ],
  rendering: [
    "Writing clean test code…",
    "One readable function per scenario…",
  ],
  validating: [
    "Double-checking every plan…",
    "Quarantining the shaky ones…",
  ],
  running: [
    "Knocking on the live API…",
    "Real requests, real responses…",
  ],
};

function showProgress(kind) {
  const panel = $("job-progress");
  panel.classList.remove("hidden");
  panel.classList.remove("done", "failed");
  state.fullSteps = [];
  const stages = JOB_STAGES[kind] || [];
  $("progress-steps").innerHTML = stages.map((stage) =>
    `<li data-stage="${stage}"><span class="dot"></span>${esc(STAGE_LABELS[stage] || stage)}</li>`).join("");
  $("progress-fill").classList.remove("indeterminate");
  $("progress-fill").style.width = "0%";
  $("progress-label").textContent = "Warming up…";
  $("progress-elapsed").textContent = "";
  $("progress-note").textContent = "";
}

function renderProgress(job) {
  if (job.kind === "full") {
    renderFullProgress(job);
    return;
  }
  const stages = JOB_STAGES[job.kind] || [];
  const update = job.progress || {};
  const currentIndex = stages.indexOf(update.stage);
  document.querySelectorAll("#progress-steps li").forEach((li) => {
    const index = stages.indexOf(li.dataset.stage);
    li.classList.toggle("done", currentIndex >= 0 && index < currentIndex);
    li.classList.toggle("current", index === currentIndex);
  });
  const fill = $("progress-fill");
  if (typeof update.done === "number" && typeof update.total === "number" && update.total > 0) {
    fill.classList.remove("indeterminate");
    fill.style.width = `${Math.min(100, Math.round((update.done / update.total) * 100))}%`;
  } else {
    fill.classList.add("indeterminate");
  }
  $("progress-label").textContent =
    update.label || (currentIndex >= 0 ? STAGE_LABELS[update.stage] : "Warming up…");
  if (state.jobStartedAt) {
    $("progress-elapsed").textContent = `${Math.floor((Date.now() - state.jobStartedAt) / 1000)}s elapsed`;
  }
  const notes = FUN_NOTES[update.stage] || [];
  if (notes.length) {
    if (state.noteStage !== update.stage) {
      state.noteStage = update.stage;
      state.noteIndex = 0;
    } else {
      state.noteIndex += 1;
    }
    $("progress-note").textContent = notes[Math.floor(state.noteIndex / 4) % notes.length];
  }
}

const FULL_NOTE_POOL = {
  ingest: "planning", planner: "planning", builder: "building",
  persist_plans: "building", render: "rendering", coverage: "validating",
  fill_gaps: "building", execute: "running", review: "validating",
};

function renderFullProgress(job) {
  const update = job.progress || {};
  const stepsEl = $("progress-steps");
  if (update.stage && !state.fullSteps.some((step) => step.id === update.stage)) {
    state.fullSteps.push({ id: update.stage, label: update.label || update.stage });
    stepsEl.innerHTML = state.fullSteps.map((step) =>
      `<li data-stage="${esc(step.id)}"><span class="dot"></span>${esc(step.label)}</li>`).join("");
  }
  const items = [...stepsEl.children];
  items.forEach((li, index) => {
    li.classList.toggle("done", index < items.length - 1);
    li.classList.toggle("current", index === items.length - 1);
  });
  $("progress-fill").classList.add("indeterminate");
  $("progress-label").textContent = update.label || "Running pipeline…";
  if (state.jobStartedAt) {
    $("progress-elapsed").textContent = `${Math.floor((Date.now() - state.jobStartedAt) / 1000)}s elapsed`;
  }
  const pool = FULL_NOTE_POOL[update.stage];
  const notes = (pool && FUN_NOTES[pool]) || [];
  if (notes.length) {
    if (state.noteStage !== update.stage) {
      state.noteStage = update.stage;
      state.noteIndex = 0;
    } else {
      state.noteIndex += 1;
    }
    $("progress-note").textContent = notes[Math.floor(state.noteIndex / 4) % notes.length];
  }
}

function finishProgress(job, ok) {
  const panel = $("job-progress");
  panel.classList.add(ok ? "done" : "failed");
  document.querySelectorAll("#progress-steps li").forEach((li) =>
    li.classList.toggle("done", ok || li.classList.contains("done")));
  const fill = $("progress-fill");
  fill.classList.remove("indeterminate");
  if (ok) fill.style.width = "100%";
  const seconds = state.jobStartedAt
    ? `${((Date.now() - state.jobStartedAt) / 1000).toFixed(1)}s`
    : null;
  $("progress-label").textContent = ok
    ? `Done${seconds ? ` in ${seconds}` : ""}`
    : "Job failed";
  $("progress-elapsed").textContent = "";
  if (ok) $("progress-note").textContent = "";
}

function setBusy(busy) {
  $("job-spinner").classList.toggle("hidden", !busy);
  $("generate-btn").disabled = busy;
  $("run-btn").disabled = busy || !(state.target && state.target.url);
}

function pollJob() {
  clearInterval(state.polling);
  state.polling = setInterval(async () => {
    let job;
    try { job = await api("/api/jobs/current"); }
    catch (_) { return; /* transient — keep polling */ }
    if (job.state === "running") {
      renderProgress(job);
      return;
    }
    clearInterval(state.polling);
    setBusy(false);
    if (job.state === "error") {
      finishProgress(job, false);
      const el = $("job-error");
      el.textContent = `Job failed: ${job.error}`;
      el.classList.remove("hidden");
      return;
    }
    finishProgress(job, true);
    if (job.kind === "generate") renderGeneration(job.result);
    if (job.kind === "run") renderRun(job.result);
    if (job.kind === "full") renderFull(job.result);
  }, 1000);
}

function plansTableHTML(plans) {
  if (!plans.length) return "";
  return `
    <table class="table">
      <thead><tr><th>#</th><th>Test</th><th>Category</th><th>Request</th><th>Expect</th><th>Missing fixtures</th></tr></thead>
      <tbody>${plans.map((plan, index) => `
        <tr>
          <td>${index + 1}</td>
          <td><code>${esc(plan.name)}</code></td>
          <td><span class="chip cat-${esc(plan.category)}">${esc(plan.category)}</span></td>
          <td>${methodBadge(plan.method)} <code>${esc(plan.path)}</code></td>
          <td>${esc(plan.expected_status_code)}</td>
          <td>${(plan.missing_fixtures || []).map((key) => `<span class="chip warn">${esc(key)}</span>`).join(" ") || "—"}</td>
        </tr>`).join("")}</tbody>
    </table>`;
}

function fetchTestsSource() {
  apiText("/api/artifacts/pytest-source")
    .then((text) => { $("tests-source").textContent = text; })
    .catch(() => { $("tests-source").textContent = "(no test file was generated)"; });
}

function renderGeneration(result) {
  const plans = result.plans || [];
  $("plans-result").classList.remove("hidden");
  $("run-area").classList.remove("hidden");

  const warnings = [];
  if (result.planner_failures) warnings.push(`${result.planner_failures} planner call(s) failed — scenarios are missing from this run.`);
  if (result.builder_failures) warnings.push(`${result.builder_failures} builder batch(es) failed — re-generate to fill the gaps.`);
  if (!plans.length) warnings.push("The builder produced no plans for this operation.");

  const issues = (result.issues_by_plan || [])
    .flatMap((list, index) => (list || []).map((issue) => `${(plans[index] || {}).name || `plan ${index + 1}`}: ${issue}`));

  const report = result.fixture_report || {};
  const missing = report.missing_data_keys || [];
  const missingFiles = (report.file_needs || []).filter((need) => !need.available);
  const missingEnv = report.env_needs || [];

  renderFixtureBanner(missing, missingFiles, missingEnv);

  const table = plansTableHTML(plans);

  $("plans-result").innerHTML = `
    <h3 class="spaced">${plans.length} test plan(s) for ${methodBadge(result.operation.method)} <code>${esc(result.operation.path)}</code></h3>
    ${table}
    ${issues.length ? `<div class="banner warn"><strong>Validation warnings</strong><ul>${issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>` : ""}
    ${warnings.length ? `<div class="banner warn"><ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
    <div class="row spaced">
      <a class="button subtle" href="/api/artifacts/tests?download=true">Download test.py</a>
      <a class="button subtle" href="/api/artifacts/plans?download=true">Download test_plans.json</a>
      <a class="button subtle" href="/api/artifacts/tests" target="_blank">Open generated file</a>
    </div>
    <details class="spaced"><summary>Generated pytest source</summary>
      <pre class="source" id="tests-source">loading…</pre>
    </details>`;

  fetchTestsSource();
}

function reviewHTML(result) {
  if (!result.options || !result.options.review) return "";
  const log = result.review_log || [];
  const patches = log.filter((entry) => entry.action === "patch");
  const rows = log.map((entry) => {
    const verified = entry.verification && entry.verification.passed;
    const detail = `${entry.reason || ""}${entry.source ? ` [${entry.source}]` : ""}${verified ? " — verified passing" : ""}`;
    return `<tr>
      <td><code>${esc(entry.name)}</code></td>
      <td>${entry.action === "patch" ? `<span class="chip ok">patch</span>` : `<span class="chip warn">skip</span>`}</td>
      <td class="muted">${esc(detail.slice(0, 220))}</td>
    </tr>`;
  }).join("");
  return `
    <h3 class="spaced">Review: ${result.patched_count || 0} patched, ${log.length - patches.length} skipped</h3>
    ${rows ? `<table class="table"><thead><tr><th>Test</th><th>Action</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table>`
      : `<p class="muted">No failures needed review.</p>`}
    <div class="row">
      <a class="button subtle" href="/api/artifacts/review-log?download=true">Download rewrite_log.json</a>
    </div>`;
}

async function loadCoverage() {
  const el = $("coverage-result");
  let report;
  try {
    report = await api("/api/artifacts/coverage");
  } catch (_) {
    el.innerHTML = `<p class="muted">No coverage report available.</p>`;
    return;
  }
  const entries = (report || []).map((entry) => {
    const gaps = entry.gaps || [];
    const items = gaps.map((gap) =>
      `<li><strong>${esc(gap.kind)}</strong> — ${esc(gap.detail || "")} <span class="muted">[${esc(gap.source || "checklist")}]</span></li>`).join("");
    return `<div class="coverage-entry">
      <p>${methodBadge(entry.method || "?")} <code>${esc(entry.path || "?")}</code>
        <span class="muted">${gaps.length} gap(s)</span></p>
      ${items ? `<ul>${items}</ul>` : `<p class="muted">Fully covered.</p>`}
    </div>`;
  }).join("");
  el.innerHTML = entries || `<p class="muted">Empty coverage report.</p>`;
}

function renderFull(result) {
  const scope = result.scope || {};
  const opts = result.options || {};
  const plans = result.plans || [];
  $("plans-result").classList.remove("hidden");
  $("run-area").classList.remove("hidden");

  const scopeLabel = scope.run_all
    ? `all ${scope.operation_count} operation(s)`
    : (scope.operation ? `${methodBadge(scope.operation.method)} <code>${esc(scope.operation.path)}</code>` : "?");
  const optChips = [
    opts.coverage ? `<span class="chip">coverage</span>` : "",
    opts.run_tests ? `<span class="chip">run</span>` : "",
    opts.review ? `<span class="chip">review</span>` : "",
  ].join(" ");

  const warnings = [];
  if (result.builder_failures) warnings.push(`${result.builder_failures} builder batch(es) failed — re-run to rebuild the missing scenarios.`);
  if (result.review_errors) warnings.push(`${result.review_errors} failure(s) never reached the reviewer model — see the review log before trusting this run.`);
  if (!plans.length) warnings.push("The pipeline produced no plans.");

  const issues = (result.issues_by_plan || [])
    .flatMap((list, index) => (list || []).map((issue) => `${(plans[index] || {}).name || `plan ${index + 1}`}: ${issue}`));

  const report = result.fixture_report || {};
  renderFixtureBanner(
    report.missing_data_keys || [],
    (report.file_needs || []).filter((need) => !need.available),
    report.env_needs || [],
  );

  $("plans-result").innerHTML = `
    <h3 class="spaced">${plans.length} test plan(s) for ${scopeLabel} ${optChips}</h3>
    ${plansTableHTML(plans)}
    ${issues.length ? `<div class="banner warn"><strong>Validation warnings</strong><ul>${issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>` : ""}
    ${warnings.length ? `<div class="banner warn"><ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : ""}
    ${opts.coverage ? `<h3 class="spaced">Coverage: ${result.coverage_gaps || 0} gap(s), ${result.filled_count || 0} filled</h3>
      <div id="coverage-result"><p class="muted">loading…</p></div>
      <div class="row"><a class="button subtle" href="/api/artifacts/coverage?download=true">Download coverage_report.json</a></div>` : ""}
    ${reviewHTML(result)}
    <div class="row spaced">
      <a class="button subtle" href="/api/artifacts/tests?download=true">Download test.py</a>
      <a class="button subtle" href="/api/artifacts/plans?download=true">Download test_plans.json</a>
      ${opts.run_tests ? `<a class="button subtle" href="/api/artifacts/results?download=true">Download results</a>` : ""}
      <a class="button subtle" href="/api/artifacts/tests" target="_blank">Open generated file</a>
    </div>
    <details class="spaced"><summary>Generated pytest source</summary>
      <pre class="source" id="tests-source">loading…</pre>
    </details>`;

  fetchTestsSource();
  if (opts.coverage) loadCoverage();
  if (opts.run_tests && result.results) renderRun(result);
}

function renderFixtureBanner(missing, missingFiles, missingEnv) {
  const banner = $("fixture-banner");
  if (!missing.length && !missingFiles.length && !missingEnv.length) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  const rows = missing.map((key) => `
    <div class="row">
      <code>${esc(key)}</code>
      <input class="input" data-fixinput placeholder='value for ${esc(key)}'>
      <button class="button primary" data-fixregen="${esc(key)}" type="button">Set & regenerate</button>
    </div>`).join("");
  const fileNotes = missingFiles.map((need) =>
    `<div class="row"><span>No file can satisfy <code>&lt;FILE:${esc(need.requested)}&gt;</code> — upload one (step 2) with extension <code>.${esc(need.ext) || "any"}</code>.</span></div>`).join("");
  const envNotes = missingEnv.map((name) =>
    `<div class="row"><span>Environment variable <code>${esc(name)}</code> is not set — add it to <code>.env</code> and restart.</span></div>`).join("");
  banner.innerHTML = `<strong>Fixtures still needed</strong>${rows}${fileNotes}${envNotes}`;
  banner.classList.remove("hidden");
  banner.querySelectorAll("button[data-fixregen]").forEach((button) => {
    button.addEventListener("click", async () => {
      const key = button.dataset.fixregen;
      const input = button.closest(".row").querySelector("input[data-fixinput]");
      const raw = input ? input.value : "";
      if (!raw) { toast("Enter a value for the fixture first."); return; }
      let value = raw;
      try { value = JSON.parse(raw); } catch (_) { /* keep as string */ }
      try {
        await api("/api/fixtures/data", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ updates: { [key]: value } }),
        });
        await loadFixtures();
        await startPipeline();
      } catch (error) {
        toast(`Could not save fixture: ${error.message}`);
      }
    });
  });
}

function renderRun(result) {
  const summary = result.summary || {};
  const rows = (result.results || []).map((entry) => `
    <tr>
      <td><code>${esc(entry.name)}</code></td>
      <td>${entry.skipped ? `<span class="chip">skipped</span>` : entry.passed ? `<span class="chip ok">passed</span>` : `<span class="chip bad">failed</span>`}</td>
      <td>${esc(entry.status_code ?? "—")}</td>
      <td class="muted">${esc((entry.error || "").slice(0, 200))}</td>
    </tr>`).join("");
  $("run-result").innerHTML = `
    <p class="spaced"><strong>${summary.passed} passed</strong>, ${summary.skipped} skipped, ${summary.failed} failed
      ${result.base_url ? `via <code>${esc(result.base_url)}</code> <span class="muted">(${esc(result.base_url_source || "env")})</span> ` : ""}
      <a class="button subtle" href="/api/artifacts/results?download=true">Download results</a></p>
    <table class="table"><thead><tr><th>Test</th><th>Outcome</th><th>Status</th><th>Error</th></tr></thead><tbody>${rows}</tbody></table>`;
}

/* ---------- boot ---------- */

async function boot() {
  wireUpload();
  wireFixtures();
  $("generate-btn").addEventListener("click", startPipeline);
  $("run-btn").addEventListener("click", startRun);
  $("target-save").addEventListener("click", saveTarget);
  $("target-clear").addEventListener("click", clearTarget);
  $("target-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") saveTarget();
  });
  await refreshHealth();
  if (!$("target-input").value && state.target.url) {
    $("target-input").value = state.target.url;
  }
  try {
    const ops = await api("/api/operations");
    state.operations = ops.operations;
    state.selected = null;
    renderOperations();
    renderNeedsOpSelect();
    $("upload-result").classList.remove("hidden");
    $("upload-result").innerHTML = `<p><strong>${esc(ops.spec_name)}</strong> is loaded (${ops.operations.length} operation(s)) — upload a new file to replace it.</p>`;
  } catch (_) { /* no spec yet */ }
  try { await loadFixtures(); } catch (_) { /* fixture endpoints down */ }
}

boot();
