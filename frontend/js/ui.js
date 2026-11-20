// 공통 화면 요소: 알림, 확인 창, API 키 설정 안내

import { escapeHtml } from "./markdown.js";

let timer = null;

export function toast(text) {
  const box = document.getElementById("toast");
  box.textContent = text;
  box.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => (box.hidden = true), 3200);
}

// 브라우저 confirm 대신 쓰는 모달. 확인하면 true, 취소나 Esc면 false
export function confirmDialog(text, okLabel = "삭제") {
  const dialog = document.getElementById("confirm");
  document.getElementById("confirm-text").textContent = text;
  document.getElementById("confirm-ok").textContent = okLabel;
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true });
    dialog.returnValue = "cancel";
    dialog.showModal();
    dialog.querySelector(".ghost").focus();
  });
}

// 설정되지 않은 API 키를 환경변수 이름으로만 안내한다 (키 값은 받지도 보여 주지도 않는다, NF3002)
let usage = {};

export function showSetup(missing, keyUsage) {
  if (keyUsage) usage = keyUsage;
  const box = document.getElementById("setup");
  if (!missing?.length) {
    box.hidden = true;
    return;
  }
  const rows = missing
    .map(
      (k) =>
        `<li><code>${escapeHtml(k)}</code>${usage[k] ? ` ${escapeHtml(usage[k])}` : ""}</li>`,
    )
    .join("");
  box.innerHTML = `
    <strong>API 키가 설정되지 않았습니다</strong>
    <ul>${rows}</ul>
    <p>프로젝트 폴더의 <code>.env</code> 에 키를 입력한 뒤 서버를 다시 시작해 주세요.</p>`;
  box.hidden = false;
}
