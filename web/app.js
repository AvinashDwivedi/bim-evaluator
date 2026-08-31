const conversation = document.getElementById("conversation");
const messages = document.getElementById("messages");
const welcomePanel = document.getElementById("welcomePanel");
const form = document.getElementById("chatForm");
const input = document.getElementById("questionInput");
const sendButton = document.getElementById("sendButton");
const sidebar = document.getElementById("sidebar");
const menuButton = document.getElementById("menuButton");
const sidebarCloseButton = document.getElementById("sidebarCloseButton");
const sidebarScrim = document.getElementById("sidebarScrim");
let busy = false;
let activeScope = null;

const pipelineWorkflow = document.getElementById("pipelineWorkflow") || document.getElementById("agentWorkflow");
const pipelineRows = new Map();

function resetPipelineWorkflow() {
  pipelineRows.clear();
  if (!pipelineWorkflow) return;
  pipelineWorkflow.innerHTML = '<li class="workflow-empty"><span>·</span><div><strong>Waiting for a question</strong><small>Pipeline stages will appear here</small></div></li>';
}

function showPipelineStage(event) {
  pipelineWorkflow.querySelector(".workflow-empty")?.remove();
  const row = document.createElement("li");
  row.className = "active";
  row.dataset.eventId = event.stage;
  const number = document.createElement("span");
  number.textContent = pipelineRows.size + 1;
  const content = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = event.agent || event.stage.replaceAll("_", " ");
  const detail = document.createElement("small");
  detail.textContent = event.tool
    ? `${event.stage.replaceAll("_", " ")}: ${event.tool.replaceAll("_", " ")}`
    : `${event.stage.replaceAll("_", " ")} · ${Number(event.elapsed_seconds).toFixed(1)}s`;
  content.append(name, detail);
  row.append(number, content);
  pipelineWorkflow.appendChild(row);
  pipelineRows.set(event.stage, row);
  row.classList.remove("active");
  row.classList.add("done");
  row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// Lifecycle-aware renderer: one row per invocation, updated as its tools run.
function renderWorkflowEvent(event) {
  if (!pipelineWorkflow) return;
  if (event.stage === "pipeline_start" || event.stage === "pipeline_end") return;
  const key = event.event_id || `${event.agent || "pipeline"}-${event.sequence || event.stage}`;
  let row = pipelineRows.get(key);
  if (!row) {
    pipelineWorkflow.querySelector(".workflow-empty")?.remove();
    row = document.createElement("li");
    row.className = "active";
    row.dataset.eventId = key;
    const number = document.createElement("span");
    number.textContent = event.sequence || pipelineRows.size + 1;
    const content = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = event.agent || "Pipeline";
    const detail = document.createElement("small");
    detail.textContent = "Starting…";
    content.append(name, detail);
    row.append(number, content);
    pipelineWorkflow.appendChild(row);
    pipelineRows.set(key, row);
  }
  const detail = row.querySelector("small");
  if (event.stage === "tool_start") {
    row.classList.add("tool-active");
    detail.textContent = `Using ${event.tool.replaceAll("_", " ")}…`;
  } else if (event.stage === "tool_end") {
    row.classList.remove("tool-active");
    detail.textContent = `Completed ${event.tool.replaceAll("_", " ")}`;
  } else if (event.stage === "llm_start") {
    detail.textContent = "Reasoning…";
  } else if (event.stage === "agent_end") {
    row.classList.remove("active", "tool-active");
    row.classList.add("done");
    detail.textContent = `Completed · ${Number(event.elapsed_seconds).toFixed(1)}s`;
  }
  row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function scrollToBottom() {
  requestAnimationFrame(() => { conversation.scrollTop = conversation.scrollHeight; });
}

function addUserMessage(text) {
  const fragment = document.getElementById("userMessageTemplate").content.cloneNode(true);
  fragment.querySelector(".user-message").textContent = text;
  messages.appendChild(fragment);
  scrollToBottom();
}

function icon(name) {
  const paths = {
    warning: '<path d="M12 3 2.5 20h19Z"/><path d="M12 9v4M12 17h.01"/>',
    evidence: '<path d="M6 3h9l3 3v15H6Z"/><path d="M14 3v4h4M9 12h6M9 16h4"/>',
    check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 2.5 2.5L16 9"/>'
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name]}</svg>`;
}

function formatUsd(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "Unavailable";
  const amount = Number(value);
  return new Intl.NumberFormat(undefined, {
    style:"currency", currency:"USD", minimumFractionDigits:2,
    maximumFractionDigits: amount !== 0 && Math.abs(amount) < 0.01 ? 6 : 2
  }).format(amount);
}

function addAssistantMessage(report) {
  const fragment = document.getElementById("assistantMessageTemplate").content.cloneNode(true);
  const article = fragment.querySelector(".assistant-row");
  const copy = fragment.querySelector(".answer-copy");
  const meta = fragment.querySelector(".answer-meta");
  const answer = report.answer || "No answer was returned.";
  copy.textContent = answer;

  if (report.verification_status === "verified") {
    const label = document.createElement("div");
    label.className = "verified-label";
    label.innerHTML = `${icon("check")} Independently verified`;
    copy.appendChild(label);
  }

  const detailedClaims = (report.claims || []).filter(
    claim => claim.details?.length && !claim.details.every(item => answer.includes(item))
  );
  detailedClaims.forEach(claim => {
    const card = document.createElement("details");
    card.className = "result-details";
    const summary = document.createElement("summary");
    const shown = claim.displayed_count ?? claim.details.length;
    const total = claim.total_count ?? shown;
    const unit = claim.unit || "results";
    summary.textContent = `View ${shown.toLocaleString()} of ${total.toLocaleString()} ${unit}`;
    const list = document.createElement("ol");
    claim.details.forEach(item => {
      const row = document.createElement("li");
      row.textContent = item;
      list.appendChild(row);
    });
    card.append(summary, list);
    if (total > shown) {
      const note = document.createElement("p");
      note.textContent = `${(total - shown).toLocaleString()} additional matching records are not displayed.`;
      card.appendChild(note);
    }
    meta.appendChild(card);
  });

  if (report.cost) {
    const card = document.createElement("div");
    card.className = "meta-card";
    const totalTokens = Number(report.cost.tokens?.total_tokens || 0).toLocaleString();
    const qualifier = report.cost.is_complete ? "Estimated API cost" : "Partial API cost estimate";
    card.innerHTML = `<div class="meta-title">${qualifier}</div><strong>${formatUsd(report.cost.estimated_cost_usd)}</strong> <small>· ${totalTokens} tokens · ${Number(report.cost.api_requests || 0)} API calls</small>`;
    meta.appendChild(card);
  }

  if (report.limitations?.length) {
    const card = document.createElement("div");
    card.className = "meta-card warning";
    card.innerHTML = `<div class="meta-title">${icon("warning")} Model limitations</div>`;
    const list = document.createElement("ul");
    report.limitations.forEach(item => {
      const li = document.createElement("li");
      li.textContent = item;
      list.appendChild(li);
    });
    card.appendChild(list);
    meta.appendChild(card);
  }

  if (report.investigation_trace?.length) {
    const card = document.createElement("details");
    card.className = "result-details investigation-trace";
    const summary = document.createElement("summary");
    summary.textContent = `View investigation trace (${report.investigation_trace.length} steps)`;
    const list = document.createElement("ol");
    report.investigation_trace.forEach(item => {
      const step = document.createElement("li");
      step.textContent = item;
      list.appendChild(step);
    });
    card.append(summary, list);
    meta.appendChild(card);
  }

  const evidenceIds = [...new Set((report.claims || []).flatMap(claim => claim.evidence_ids || []))];
  if (evidenceIds.length) {
    const card = document.createElement("div");
    card.className = "meta-card";
    card.innerHTML = `<div class="meta-title">${icon("evidence")} Evidence</div>`;
    const row = document.createElement("div");
    row.className = "evidence-row";
    evidenceIds.forEach(id => {
      const chip = document.createElement("span");
      chip.className = "evidence-chip";
      chip.textContent = id;
      row.appendChild(chip);
    });
    card.appendChild(row);
    meta.appendChild(card);
  }

  messages.appendChild(fragment);
  scrollToBottom();
  return article;
}

function addTypingMessage() {
  const fragment = document.getElementById("assistantMessageTemplate").content.cloneNode(true);
  const row = fragment.querySelector(".assistant-row");
  fragment.querySelector(".assistant-message").innerHTML = '<div class="typing"><i></i><i></i><i></i><span class="typing-status">Interpreting BIM question…</span></div>';
  messages.appendChild(fragment);
  scrollToBottom();
  return row;
}

function setBusy(value) {
  busy = value;
  input.disabled = value;
  sendButton.disabled = value;
}

async function askQuestion(question) {
  if (busy || !question.trim()) return;
  welcomePanel?.classList.add("hidden");
  addUserMessage(question.trim());
  input.value = "";
  input.style.height = "auto";
  setBusy(true);
  resetPipelineWorkflow();
  const typingRow = addTypingMessage();
  const status = typingRow.querySelector(".typing-status");

  try {
    status.textContent = "Sending question to the BIM server…";
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: question.trim(),
        client_id: activeScope?.client_id,
        project_id: activeScope?.project_id
      })
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.detail || "The BIM service could not answer this question.");
    }
    if (!response.body) throw new Error("The browser could not open the live workflow stream.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalReport = null;
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "pipeline_stage") {
          renderWorkflowEvent(event);
          status.textContent = event.agent
            ? `${event.agent}: ${event.tool?.replaceAll("_", " ") || event.stage.replaceAll("_", " ")}…`
            : `${event.stage.replaceAll("_", " ")}…`;
        } else if (event.type === "result") {
          finalReport = event.report;
        } else if (event.type === "error") {
          throw new Error(event.message);
        }
      }
      if (done) break;
    }
    if (!finalReport) throw new Error("The workflow ended without a final BIM answer.");
    typingRow.remove();
    addAssistantMessage(finalReport);
  } catch (error) {
    typingRow.remove();
    addAssistantMessage({
      answer: error.message,
      verification_status: "insufficient_evidence",
      limitations: ["The request did not complete. Check the server log for the failed stage."],
      claims: []
    });
  } finally {
    setBusy(false);
    input.focus();
  }
}

form.addEventListener("submit", event => {
  event.preventDefault();
  askQuestion(input.value);
});

window.addEventListener("error", event => {
  console.error("BIM web client error", event.error || event.message);
  const activeStatus = document.querySelector(".typing-status");
  if (activeStatus) activeStatus.textContent = `Browser error: ${event.message}`;
});

window.addEventListener("unhandledrejection", event => {
  console.error("BIM web client promise error", event.reason);
  const activeStatus = document.querySelector(".typing-status");
  if (activeStatus) activeStatus.textContent = `Request error: ${event.reason?.message || event.reason}`;
});

input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 130)}px`;
});

document.getElementById("suggestions").addEventListener("click", event => {
  const button = event.target.closest("button[data-question]");
  if (button) askQuestion(button.dataset.question);
});

document.getElementById("newChatButton").addEventListener("click", () => {
  if (busy) return;
  messages.replaceChildren();
  welcomePanel.classList.remove("hidden");
  resetPipelineWorkflow();
  input.focus();
});

function setSidebarOpen(open) {
  sidebar.classList.toggle("open", open);
  menuButton.setAttribute("aria-expanded", String(open));
  menuButton.setAttribute("aria-label", open ? "Close project panel" : "Open project panel");
}

menuButton.addEventListener("click", () => setSidebarOpen(!sidebar.classList.contains("open")));
sidebarCloseButton.addEventListener("click", () => setSidebarOpen(false));
sidebarScrim.addEventListener("click", () => setSidebarOpen(false));
document.addEventListener("click", event => {
  if (window.innerWidth <= 880 && sidebar.classList.contains("open") && !sidebar.contains(event.target) && !event.target.closest("#menuButton")) {
    setSidebarOpen(false);
  }
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape") setSidebarOpen(false);
});
window.addEventListener("resize", () => {
  if (window.innerWidth > 880) setSidebarOpen(false);
});

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    const health = await response.json();
    if (!response.ok) throw new Error();
    document.getElementById("projectName").textContent = "Authorized BIM project";
    document.getElementById("connectionText").textContent = "Connected and authorized";
    document.getElementById("connectionDot").classList.add("online");
    document.getElementById("sourceCount").textContent = health.source_count ?? "—";
    document.getElementById("elementCount").textContent = Number(health.element_count || 0).toLocaleString();
    document.getElementById("contractVersion").textContent = `v${health.contract_version}`;
  } catch {
    document.getElementById("projectName").textContent = "BIM service unavailable";
    document.getElementById("connectionText").textContent = "Check server connection";
  }
}

async function loadConfig() {
  const response = await fetch("/api/config");
  if (!response.ok) throw new Error("Evaluator configuration is unavailable.");
  const config = await response.json();
  const select = document.getElementById("scopeSelect");
  (config.scopes || []).forEach((scope, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = `${scope.client_id.slice(0, 8)}… / ${scope.project_id.slice(0, 8)}…`;
    select.appendChild(option);
  });
  activeScope = config.scopes?.[0] || null;
  select.addEventListener("change", () => { activeScope = config.scopes[Number(select.value)]; });
}

Promise.all([loadConfig(), loadHealth()]).catch(error => {
  document.getElementById("connectionText").textContent = error.message;
});
input.focus();
