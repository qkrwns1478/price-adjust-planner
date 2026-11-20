// 계획 관리 화면 (F3015, F3016, F3018): 목록, 상세, 수정, 삭제

import { api } from "./api.js";
import { savedText } from "./chat.js";
import { PLAN_STATUS, dateTime, won } from "./labels.js";
import { escapeHtml } from "./markdown.js";
import { confirmDialog, toast } from "./ui.js";

const $ = (id) => document.getElementById(id);

let plans = [];
let current = null;

// 수정할 수 있는 핵심 값. [이름, 표시, 입력 형식]
const FIELDS = [
  ["meeting_date", "협의 날짜", "date"],
  ["start_time", "시작", "time"],
  ["end_time", "종료", "time"],
  ["status", "상태", "status"],
  ["client", "위탁기업", "text"],
  ["product", "납품 품목", "text"],
  ["item", "원재료", "text"],
  ["unit_price_won", "조정 전 단가(원)", "int"],
  ["increase_won", "개당 요청 금액(원)", "int"],
  ["new_price_won", "조정 후 단가(원)", "int"],
  ["memo", "메모", "textarea"],
];

function input([name, , type], value) {
  const v = escapeHtml(value ?? "");
  if (type === "status") {
    return `<select name="${name}">${Object.entries(PLAN_STATUS)
      .map(
        ([k, label]) =>
          `<option value="${k}"${k === value ? " selected" : ""}>${label}</option>`,
      )
      .join("")}</select>`;
  }
  if (type === "textarea")
    return `<textarea name="${name}" rows="3">${v}</textarea>`;
  if (type === "int")
    return `<input name="${name}" type="number" min="0" step="1" value="${v}" />`;
  const required = [
    "meeting_date",
    "start_time",
    "end_time",
    "client",
    "item",
  ].includes(name);
  return `<input name="${name}" type="${type}" value="${v}"${required ? " required" : ""} />`;
}

function renderList() {
  const body = $("plan-rows");
  body.replaceChildren();
  $("plan-empty").hidden = plans.length > 0;
  for (const p of plans) {
    const tr = document.createElement("tr");
    tr.dataset.id = p.id;
    tr.classList.toggle("is-selected", current?.id === p.id);
    tr.innerHTML = `
      <td>${escapeHtml(p.meeting_date)} ${escapeHtml(p.start_time)}–${escapeHtml(p.end_time)}</td>
      <td>${escapeHtml(p.client)}</td>
      <td>${escapeHtml(p.item)}</td>
      <td class="num">${p.increase_won != null ? won(p.increase_won) : "-"}</td>
      <td><span class="status is-${p.status}">${PLAN_STATUS[p.status]}</span></td>`;
    tr.addEventListener("click", () => open(p.id));
    body.append(tr);
  }
}

function renderDetail(notice = "") {
  const box = $("plan-detail");
  if (!current) {
    box.replaceChildren();
    return;
  }
  const p = current;
  const evidence = [
    p.index_name &&
      `${p.index_name} ${p.base_month ?? ""} ${p.base_value ?? ""} → ${p.current_month ?? ""} ${p.current_value ?? ""}`,
    p.change_rate_pct != null &&
      `변동률 ${p.change_rate_pct}%${p.threshold_pct != null ? ` (약정 ${p.threshold_pct}%)` : ""}`,
    ...(p.law_refs || []),
  ].filter(Boolean);
  box.innerHTML = `
    <form id="plan-form" class="plan-form">
      <h2>계획 #${p.id}</h2>
      <div class="fields">
        ${FIELDS.map(
          (f) =>
            `<label class="${f[2] === "textarea" ? "wide" : ""}">${f[1]}${input(f, p[f[0]])}</label>`,
        ).join("")}
      </div>
      ${evidence.length ? `<ul class="evidence">${evidence.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>` : ""}
      <p class="stamp">생성 ${dateTime(p.created_at)}, 수정 ${dateTime(p.updated_at)}</p>
      <div class="form-actions">
        <button type="submit">수정 저장</button>
        <button type="button" id="plan-delete" class="danger">삭제</button>
      </div>
      <div class="form-result">${notice}</div>
    </form>`;
  $("plan-form").addEventListener("submit", save);
  $("plan-delete").addEventListener("click", remove);
}

function changes(form) {
  const out = {};
  for (const [name, , type] of FIELDS) {
    const raw = form.elements.namedItem(name).value.trim();
    let v = raw === "" ? null : raw;
    if (type === "int" && v !== null) v = Number(v);
    if (v !== (current[name] ?? null)) out[name] = v;
  }
  return out;
}

async function save(ev) {
  ev.preventDefault();
  const body = changes(ev.target);
  if (!Object.keys(body).length) return toast("바뀐 값이 없습니다.");
  try {
    const saved = await api.updatePlan(current.id, body);
    current = saved.plan;
    await load();
    renderDetail(savedText(saved, true));
  } catch (e) {
    $("plan-form").querySelector(".form-result").innerHTML =
      `<p class="callout is-fail">${escapeHtml(e.message)}</p>`;
  }
}

async function remove() {
  if (!(await confirmDialog(`계획 #${current.id}을 삭제할까요?`))) return;
  try {
    await api.deletePlan(current.id);
    toast(`계획 #${current.id}을 삭제했습니다.`);
    current = null;
    await load();
    renderDetail();
  } catch (e) {
    toast(e.message);
  }
}

async function open(id) {
  try {
    current = await api.plan(id);
    renderList();
    renderDetail();
  } catch (e) {
    toast(e.message);
  }
}

async function load() {
  plans = (await api.plans()).plans;
  if (current && !plans.some((p) => p.id === current.id)) current = null;
  renderList();
}

export async function showPlans() {
  try {
    await load();
    renderDetail();
  } catch (e) {
    toast(e.message);
  }
}
