// 대화 화면 (F3018, F3019)

import { api } from "./api.js";
import { NODE, TOOL, TOOL_ERROR, won } from "./labels.js";
import { escapeHtml, renderMarkdown } from "./markdown.js";
import { RunTrace } from "./trace.js";
import { showSetup, toast } from "./ui.js";

const $ = (id) => document.getElementById(id);

let sessionId = null;
let busy = false;
let selected = null; // 실행 과정 패널에 보여 주는 메시지

function scrollToEnd() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

function select(li) {
  if (selected) selected.classList.remove("is-selected");
  selected = li;
  li.classList.add("is-selected");
  $("chat-trace").replaceChildren(li._trace.root);
}

function addUser(text) {
  const li = document.createElement("li");
  li.className = "msg msg-user";
  li.textContent = text;
  $("messages").append(li);
}

function addPending(trace) {
  const li = document.createElement("li");
  li.className = "msg msg-agent is-pending";
  li.innerHTML =
    '<p class="pending"><span class="spinner"></span><span class="pending-text">요청을 보내는 중</span></p>';
  li._trace = trace;
  li.addEventListener("click", (ev) => {
    if (!ev.target.closest("form, a, button, input, select")) select(li);
  });
  $("messages").append(li);
  select(li);
  return li;
}

function failureNotice(r) {
  let html = "";
  // LLM 키가 없으면 요청 분석부터 실패하므로 답변 안에서도 설정을 안내한다
  if (r.missing_keys?.includes("OPENAI_API_KEY") && !r.evidence?.length) {
    html += `<div class="callout is-warn"><strong>LLM API 키가 없어 요청을 분석하지 못했습니다</strong><ul><li><code>.env</code> 의 <code>OPENAI_API_KEY</code> 를 설정한 뒤 서버를 다시 시작해 주세요.</li></ul></div>`;
  }
  const failed = (r.evidence || []).filter((e) => !e.ok);
  if (!failed.length) return html;
  const rows = failed
    .map(
      (e) =>
        `<li>${escapeHtml(TOOL[e.tool] || e.tool)}: ${escapeHtml(TOOL_ERROR[e.code] || "호출 실패")} <code>${escapeHtml(e.code)}</code></li>`,
    )
    .join("");
  return (
    html +
    `<div class="callout is-fail"><strong>Tool 호출 ${failed.length}건 실패</strong><ul>${rows}</ul></div>`
  );
}

function draftCard(draft) {
  const form = document.createElement("form");
  form.className = "draft";
  const amount =
    draft.increase_won != null
      ? `개당 ${won(draft.increase_won)} 인상 (${won(draft.unit_price_won)} → ${won(draft.new_price_won)})`
      : draft.increase_ratio_pct != null
        ? `단가의 ${draft.increase_ratio_pct}% 인상`
        : "";
  form.innerHTML = `
    <div class="draft-head">
      <strong>${escapeHtml([draft.client, draft.product].filter(Boolean).join(", ") || "위탁기업 미정")}</strong>
      <span>${escapeHtml(draft.item)}</span>
      <span class="draft-amount">${escapeHtml(amount)}</span>
    </div>
    <div class="draft-fields">
      <label>협의 날짜<input type="date" name="meeting_date" required /></label>
      <label>시작<input type="time" name="start_time" required /></label>
      <label>종료<input type="time" name="end_time" required /></label>
      <label class="wide">위탁기업<input name="client" value="${escapeHtml(draft.client || "")}" required /></label>
      <button type="submit">계획 저장</button>
    </div>
    <div class="draft-result"></div>`;
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const saved = await api.createPlan({ ...draft, ...data });
      form.classList.add("is-saved");
      form.querySelector(".draft-result").innerHTML = savedText(saved);
      form.querySelectorAll("input").forEach((i) => (i.disabled = true));
      button.textContent = "저장됨";
    } catch (e) {
      button.disabled = false;
      form.querySelector(".draft-result").innerHTML =
        `<p class="callout is-fail">${escapeHtml(e.message)}</p>`;
    }
  });
  return form;
}

export function savedText(saved, edited = false) {
  const brief = (p) =>
    `<li>#${p.id} ${escapeHtml(p.client)}, ${escapeHtml(p.item)}, ${escapeHtml(p.meeting_date)} ${escapeHtml(p.start_time)}–${escapeHtml(p.end_time)}</li>`;
  let html = edited
    ? `<p class="callout is-ok">계획 #${saved.plan.id}을 수정했습니다.</p>`
    : `<p class="callout is-ok">계획 #${saved.plan.id}을 저장했습니다. <a href="#plans">계획에서 보기</a></p>`;
  if (saved.overlaps?.length) {
    html += `<div class="callout is-warn"><strong>같은 날짜에 협의 시간이 겹치는 계획</strong><ul>${saved.overlaps.map(brief).join("")}</ul></div>`;
  }
  if (saved.duplicates?.length) {
    html += `<div class="callout is-warn"><strong>같은 위탁기업, 품목, 원재료로 진행 중인 계획</strong><ul>${saved.duplicates.map(brief).join("")}</ul></div>`;
  }
  return html;
}

function renderAnswer(li, r, failed = false) {
  li.classList.remove("is-pending");
  li.classList.toggle("is-error", r.kind === "error" || failed);
  li.innerHTML =
    failureNotice(r) + `<div class="md">${renderMarkdown(r.text)}</div>`;
  if (r.plan_drafts?.length) {
    const box = document.createElement("div");
    box.className = "drafts";
    r.plan_drafts.forEach((d) => box.append(draftCard(d)));
    li.append(box);
  }
}

async function send(text) {
  busy = true;
  $("chat-send").disabled = true;
  addUser(text);
  const trace = new RunTrace(text);
  const li = addPending(trace);
  const status = li.querySelector(".pending-text");
  scrollToEnd();
  try {
    const r = await api.chatStream(
      { session_id: sessionId, message: text },
      (line) => {
        if (line.type === "session") sessionId = line.session_id;
        if (line.type !== "event") return;
        trace.add(line.event);
        if (line.event.type === "node_start")
          status.textContent = `${NODE[line.event.node] || line.event.node} 중`;
        if (line.event.type === "tool_request") {
          status.textContent = `${TOOL[line.event.data.tool] || line.event.data.tool} 호출 중`;
        }
      },
    );
    sessionId = r.session_id;
    if (r.kind === "error") trace.abort();
    trace.review(r.review);
    renderAnswer(li, r, trace.failed);
    showSetup(r.missing_keys);
  } catch (e) {
    trace.abort(e.message);
    renderAnswer(li, { kind: "error", text: e.message });
  } finally {
    busy = false;
    $("chat-send").disabled = false;
    scrollToEnd();
  }
}

export function initChat() {
  const input = $("chat-input");
  $("chat-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    send(text);
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
      ev.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  $("new-session").addEventListener("click", () => {
    if (busy) return toast("응답을 받은 뒤에 새 대화를 시작할 수 있습니다.");
    sessionId = null;
    selected = null;
    $("messages").replaceChildren();
    $("chat-trace").replaceChildren();
    input.focus();
  });
}
