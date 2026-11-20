// 실행 로그 화면 (F3014, F3019): 세션 목록, 실행 순서대로 상세, 삭제. 수정 기능은 두지 않는다.

import { api } from "./api.js";
import { dateTime } from "./labels.js";
import { escapeHtml } from "./markdown.js";
import { renderRuns } from "./trace.js";
import { confirmDialog, toast } from "./ui.js";

const $ = (id) => document.getElementById(id);

let currentId = null;

function renderList(sessions) {
  const list = $("session-list");
  list.replaceChildren();
  if (!sessions.length) {
    list.innerHTML = '<li class="empty">저장한 실행 로그가 없습니다.</li>';
    return;
  }
  for (const s of sessions) {
    const li = document.createElement("li");
    li.className =
      "session" + (s.session_id === currentId ? " is-selected" : "");
    const flags = [
      `요청 ${s.runs}`,
      `Tool ${s.tool_calls}`,
      s.replans ? `재계획 ${s.replans}` : "",
    ].filter(Boolean);
    li.innerHTML = `
      <p class="session-msg">${escapeHtml(s.first_message || s.session_id)}</p>
      <p class="session-meta">
        <span>${dateTime(s.started_at)}</span>
        <span>${flags.join(", ")}</span>
        ${s.anomalies ? `<span class="verdict is-anomaly">이상 ${s.anomalies}</span>` : ""}
      </p>`;
    li.addEventListener("click", () => open(s.session_id));
    list.append(li);
  }
}

async function open(sessionId) {
  try {
    const log = await api.log(sessionId);
    currentId = sessionId;
    const box = $("log-detail");
    box.innerHTML = `
      <div class="detail-head">
        <code>${escapeHtml(sessionId)}</code>
        <button type="button" id="log-delete" class="danger">삭제</button>
      </div>
      <div class="trace"></div>`;
    renderRuns(box.querySelector(".trace"), log.events, log.reviews);
    $("log-delete").addEventListener("click", () => remove(sessionId));
    document
      .querySelectorAll(".session")
      .forEach((n) => n.classList.remove("is-selected"));
    await showLogs(false);
  } catch (e) {
    toast(e.message);
  }
}

async function remove(sessionId) {
  if (!(await confirmDialog("이 세션의 실행 로그를 삭제할까요?"))) return;
  try {
    const r = await api.deleteLog(sessionId);
    toast(`이벤트 ${r.deleted}건을 삭제했습니다.`);
    currentId = null;
    $("log-detail").replaceChildren();
    await showLogs(false);
  } catch (e) {
    toast(e.message);
  }
}

export async function showLogs(resetDetail = true) {
  try {
    const { sessions } = await api.logs();
    if (
      resetDetail &&
      currentId &&
      !sessions.some((s) => s.session_id === currentId)
    ) {
      currentId = null;
      $("log-detail").replaceChildren();
    }
    renderList(sessions);
  } catch (e) {
    toast(e.message);
  }
}
