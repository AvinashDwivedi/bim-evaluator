const list = document.getElementById("caseList");
const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const reportLink = document.getElementById("reportLink");
const selectAll = document.getElementById("selectAll");
const clearSelection = document.getElementById("clearSelection");
const judgeMode = document.getElementById("judgeMode");
let cases = [];
let state = null;
let pollTimer = null;
let activeFilter = "all";
let selectedIndices = new Set();
let selectionInitialized = false;
let defaultTimeoutSeconds = 600;

const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const shortId = value => value ? `${value.slice(0, 8)}...` : "unknown";

function formatTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat(undefined, {dateStyle:"medium", timeStyle:"short"}).format(new Date(value));
}

function formatUsd(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const amount = Number(value);
  const digits = amount !== 0 && Math.abs(amount) < 0.01 ? 6 : 2;
  return new Intl.NumberFormat(undefined, {
    style:"currency", currency:"USD", minimumFractionDigits:2, maximumFractionDigits:digits
  }).format(amount);
}

function aggregateResultCosts(results) {
  const costs = (results || []).map(item => item.cost).filter(Boolean);
  const known = costs.filter(item => item.estimated_cost_usd !== null && item.estimated_cost_usd !== undefined);
  return {
    estimated_cost_usd: known.reduce((total, item) => total + Number(item.estimated_cost_usd), 0),
    status: costs.length === (results || []).length && costs.every(item => item.is_complete) ? "calculated" : "partial",
    known: known.length,
  };
}

function updateSelectionControls() {
  const count = selectedIndices.size;
  document.getElementById("selectionCount").textContent = `${count} question${count === 1 ? "" : "s"} selected`;
  selectAll.checked = cases.length > 0 && count === cases.length;
  selectAll.indeterminate = count > 0 && count < cases.length;
  const locked = state?.status === "running";
  selectAll.disabled = locked;
  clearSelection.disabled = locked || count === 0;
  judgeMode.disabled = locked;
  startButton.disabled = locked || count === 0;
}

function currentCaseIndices(values) {
  const available = new Set(cases.map((_, index) => index + 1));
  return (values || []).filter(index => available.has(index));
}

function renderEvidenceDetails(result) {
  if (!result) return "";
  const trace = result.investigation_trace || [];
  const limitations = result.limitations || [];
  const failures = result.failure_categories || [];
  if (!trace.length && !limitations.length && !failures.length) return "";
  const traceItems = trace.map(item => `<li>${escapeHtml(item)}</li>`).join("");
  const limitationItems = limitations.map(item => `<li>${escapeHtml(item)}</li>`).join("");
  const failureItems = failures.map(item => `<li>${escapeHtml(item)}</li>`).join("");
  return `<details class="case-trace"><summary>Investigation evidence (${trace.length} trace steps)</summary>
    ${failures.length ? `<h4>Failure categories</h4><ul>${failureItems}</ul>` : ""}
    ${limitations.length ? `<h4>Limitations</h4><ul>${limitationItems}</ul>` : ""}
    ${trace.length ? `<h4>Trace</h4><ol>${traceItems}</ol>` : ""}
  </details>`;
}

function renderCases() {
  const byIndex = new Map((state?.results || []).map(result => [result.index, result]));
  const allIndices = cases.map((_, index) => index + 1);
  const runSelection = new Set(state?.selected_question_indices || allIndices);
  list.innerHTML = cases.map((item, offset) => {
    const index = offset + 1;
    const result = byIndex.get(index);
    const running = state?.status === "running" && state.current_index === index;
    const included = runSelection.has(index);
    const status = result ? (result.correct ? "correct" : "incorrect") : running ? "running" : included ? "pending" : "excluded";
    const verdict = result ? (result.correct ? "Correct" : "Incorrect") : running ? "Running" : included ? "Pending" : "Not selected";
    const actual = result?.actual_answer || (running ? "The BIM agent is investigating this case..." : included ? "No response recorded." : "Excluded from this run.");
    const reason = result?.reason ? `<div class="reason">${escapeHtml(result.reason)}</div>` : "";
    const evidenceDetails = renderEvidenceDetails(result);
    const questionCost = result?.cost;
    const costTitle = questionCost
      ? `BIM answer: ${formatUsd(questionCost.system_estimated_cost_usd)}; judge: ${formatUsd(questionCost.judge_estimated_cost_usd)}; ${questionCost.status}`
      : "";
    const cost = questionCost ? `<span class="case-cost" title="${escapeHtml(costTitle)}">${formatUsd(questionCost.estimated_cost_usd)}${questionCost.is_complete ? "" : " estimated*"}</span>` : "";
    return `<article class="case-card ${status}" data-status="${status}" id="case-${index}">
      <div class="case-select"><input class="question-selector" type="checkbox" data-index="${index}" ${selectedIndices.has(index) ? "checked" : ""} ${state?.status === "running" ? "disabled" : ""} aria-label="Select question ${index}"></div>
      <div class="case-number">${String(index).padStart(2,"0")}</div>
      <div><div class="question">${escapeHtml(item.question)}</div><div class="scope">${shortId(item.client_id)} / ${shortId(item.project_id)}</div></div>
      <div class="comparison"><div class="answer"><label>Expected answer</label>${escapeHtml(item.answer)}</div><div class="answer actual ${result ? "" : "empty"}"><label>Agent response</label>${escapeHtml(actual)}</div></div>
      <div class="verdict"><span class="verdict-badge">${verdict}</span>${result ? `<span class="elapsed">${Number(result.elapsed_seconds).toFixed(1)} sec</span>${cost}` : ""}</div>${reason}${evidenceDetails}</article>`;
  }).join("");
  document.querySelectorAll(".question-selector").forEach(input => input.addEventListener("change", event => {
    const index = Number(event.target.dataset.index);
    if (event.target.checked) selectedIndices.add(index); else selectedIndices.delete(index);
    updateSelectionControls();
  }));
  applyFilter();
}

function renderState(next) {
  state = next;
  const firstRender = !selectionInitialized;
  if (!selectionInitialized || state.status === "running") {
    const requested = state.selected_question_indices || cases.map((_, index) => index + 1);
    selectedIndices = new Set(currentCaseIndices(requested));
    selectionInitialized = true;
  }
  if (firstRender && state.options?.judge_mode) judgeMode.value = state.options.judge_mode;
  const results = state.results || [];
  const correct = results.filter(item => item.correct).length;
  const completed = results.length;
  const total = state.total || cases.length;
  const errors = results.filter(item => item.error).length;
  const evaluationCost = aggregateResultCosts(results);
  document.getElementById("total").textContent = total;
  document.getElementById("completed").textContent = completed;
  document.getElementById("correct").textContent = correct;
  document.getElementById("incorrect").textContent = completed - correct;
  document.getElementById("errors").textContent = errors;
  document.getElementById("evaluationCost").textContent = formatUsd(evaluationCost.estimated_cost_usd);
  document.getElementById("evaluationCostStatus").textContent = `${evaluationCost.known} priced question${evaluationCost.known === 1 ? "" : "s"}${evaluationCost.status === "partial" && results.length ? " · partial estimate" : ""}`;
  document.getElementById("progressLabel").textContent = `${total ? Math.round(completed / total * 100) : 0}% progress`;
  document.getElementById("progressBar").style.width = `${total ? completed / total * 100 : 0}%`;
  document.getElementById("caseCount").textContent = `${total} selected from ${state.dataset_total || cases.length} project-scoped cases - ${completed} with verdicts`;
  document.getElementById("timeoutValue").textContent = `${Math.round(defaultTimeoutSeconds / 60)} minutes`;

  const status = state.status || "idle";
  const pill = document.getElementById("statusPill");
  pill.className = `status-pill ${status}`;
  pill.textContent = status;
  const titles = {idle:"Ready to evaluate",running:`Evaluating question ${state.current_index || "-"} (${state.current_position || 1} of ${total})`,completed:"Evaluation completed",stopped:"Evaluation stopped",failed:"Evaluation interrupted"};
  document.getElementById("runTitle").textContent = titles[status] || status;
  const descriptions = {
    idle:"No saved run yet. Start when the BIM backend is available.",
    running:`Started ${formatTime(state.started_at)}. You may close this page; the server will continue the run.`,
    completed:`Finished ${formatTime(state.finished_at)}. This run has its own saved report.`,
    stopped:`Stopped ${formatTime(state.finished_at)} after ${completed} completed cases. The partial report was saved.`,
    failed:`${state.error || "The run could not continue."} Last update: ${formatTime(state.updated_at)}.`
  };
  document.getElementById("runStatus").textContent = descriptions[status];
  document.getElementById("statusIcon").textContent = ({idle:"o",running:"~",completed:"OK",stopped:"x",failed:"!"})[status];
  startButton.innerHTML = status === "completed" || status === "stopped" || status === "failed" ? "<span>&#8635;</span> Run again" : "<span>&#9654;</span> Start evaluation";
  stopButton.classList.toggle("hidden", status !== "running");
  reportLink.classList.toggle("hidden", !state.run_id || state.run_id === "imported-report");
  if (state.run_id && state.run_id !== "imported-report") reportLink.href = `/api/evaluation/reports/${encodeURIComponent(state.run_id)}`;
  renderCases();
  updateSelectionControls();
  if (status === "running") schedulePoll(); else clearTimeout(pollTimer);
}

function applyFilter() {
  document.querySelectorAll(".case-card").forEach(card => {
    card.dataset.hidden = String(activeFilter !== "all" && card.dataset.status !== activeFilter);
  });
}

async function loadState() {
  const response = await fetch("/api/evaluation/state", {cache:"no-store"});
  if (!response.ok) throw new Error("Could not restore evaluation state.");
  renderState(await response.json());
  loadDailyCost().catch(() => {});
}

async function loadDailyCost() {
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  const end = new Date(start);
  end.setDate(end.getDate() + 1);
  const query = new URLSearchParams({start:start.toISOString(), end:end.toISOString()});
  const response = await fetch(`/api/evaluation/costs/daily?${query}`, {cache:"no-store"});
  if (!response.ok) throw new Error("Could not load today's evaluation cost.");
  const daily = await response.json();
  document.getElementById("dailyCost").textContent = formatUsd(daily.estimated_cost_usd ?? 0);
  document.getElementById("dailyCostStatus").textContent = `${daily.runs} run${daily.runs === 1 ? "" : "s"} · ${daily.questions} question${daily.questions === 1 ? "" : "s"}${daily.status === "partial" ? " · partial" : ""}`;
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => loadState().catch(showError), 1000);
}

function showError(error) {
  document.getElementById("runStatus").textContent = error.message;
  if (state?.status === "running") schedulePoll();
}

async function startRun() {
  startButton.disabled = true;
  const questionIndices = [...selectedIndices].sort((a, b) => a - b);
  const response = await fetch("/api/evaluation/run", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({question_indices:questionIndices, judge_mode:judgeMode.value})
  });
  if (!response.ok) {
    const value = await response.json();
    throw new Error(value.detail || "Could not start evaluation.");
  }
  renderState(await response.json());
}

async function stopRun() {
  stopButton.disabled = true;
  try {
    const response = await fetch("/api/evaluation/stop", {method:"POST"});
    if (!response.ok) {
      const value = await response.json();
      throw new Error(value.detail || "Could not stop evaluation.");
    }
    renderState(await response.json());
  } finally {
    stopButton.disabled = false;
  }
}

document.querySelectorAll(".filter").forEach(button => button.addEventListener("click", () => {
  document.querySelector(".filter.active")?.classList.remove("active");
  button.classList.add("active");
  activeFilter = button.dataset.filter;
  applyFilter();
}));
startButton.addEventListener("click", () => startRun().catch(showError));
stopButton.addEventListener("click", () => stopRun().catch(showError));
selectAll.addEventListener("change", () => {
  selectedIndices = selectAll.checked ? new Set(cases.map((_, index) => index + 1)) : new Set();
  renderCases();
  updateSelectionControls();
});
clearSelection.addEventListener("click", () => {
  selectedIndices.clear();
  renderCases();
  updateSelectionControls();
});

async function initialize() {
  const configResponse = await fetch("/api/config", {cache:"no-store"});
  if (configResponse.ok) {
    const config = await configResponse.json();
    defaultTimeoutSeconds = config.case_timeout_seconds || defaultTimeoutSeconds;
  }
  const response = await fetch("/api/evaluation/cases", {cache:"no-store"});
  if (!response.ok) throw new Error("Could not load evaluation cases.");
  cases = (await response.json()).cases;
  selectedIndices = new Set(cases.map((_, index) => index + 1));
  await loadState();
}

initialize().catch(showError);
