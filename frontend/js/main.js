// 화면 전환과 시작 시 설정 확인

import { api, backendPageUrl } from "./api.js";
import { initChat } from "./chat.js";
import { showLogs } from "./logs.js";
import { showPlans } from "./plans.js";
import { showSetup, toast } from "./ui.js";

const VIEWS = { intro: null, chat: null, plans: showPlans, logs: showLogs };

function route() {
  const tab = location.hash.slice(1) in VIEWS ? location.hash.slice(1) : "intro";
  for (const name of Object.keys(VIEWS)) {
    document.getElementById(`view-${name}`).hidden = name !== tab;
    document
      .querySelector(`[data-tab="${name}"]`)
      .setAttribute("aria-selected", String(name === tab));
  }
  VIEWS[tab]?.();
}

async function checkSetup() {
  try {
    const s = await api.status();
    const usage = Object.fromEntries(s.keys.map((k) => [k.env, k.usage]));
    showSetup(
      s.keys.filter((k) => !k.configured).map((k) => k.env),
      usage,
    );
  } catch (e) {
    toast(e.message);
  }
}

// 네트워크 응답을 기다리는 동안에도 화면과 탭을 사용할 수 있게 한다.
initChat();
window.addEventListener("hashchange", route);
route();

async function connectBackend() {
  const backendPage = await backendPageUrl();
  if (backendPage) {
    location.replace(backendPage);
    return;
  }
  checkSetup();
}

connectBackend();
