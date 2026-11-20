// 백엔드 API 호출.
// 백엔드가 제공하는 화면이면 같은 주소를 쓰고, Live Server처럼 다른 서버나 파일로 열면 백엔드 기본 주소를 쓴다.
// 다른 주소의 백엔드를 쓰려면 window.API_BASE를 지정한다.
const BACKEND = "http://127.0.0.1:8000";
let basePromise = null;

// 서버 주소 확인이 화면 초기화나 API 요청을 무기한 붙잡지 않도록 한다.
function probeStatus(url) {
  return fetch(url, { signal: AbortSignal.timeout(3000), cache: "no-store" });
}

function base() {
  if (window.API_BASE != null) return Promise.resolve(window.API_BASE);
  if (location.protocol === "file:") return Promise.resolve(BACKEND);
  // 화면을 연 서버에 API가 있는지 한 번만 확인한다
  basePromise ??= probeStatus("/api/status")
    .then((r) =>
      r.ok && (r.headers.get("content-type") || "").includes("application/json")
        ? ""
        : BACKEND,
    )
    .catch(() => BACKEND);
  return basePromise;
}

// Live Server처럼 백엔드가 아닌 서버에서 화면을 열었고 백엔드가 응답하면, 백엔드가 제공하는 같은 화면 주소를 돌려준다.
// Live Server는 백엔드가 실행 로그(data/planner.db)를 쓸 때마다 페이지를 새로고침해 진행 중인 대화를 지운다.
export async function backendPageUrl() {
  if (window.API_BASE != null || location.protocol === "file:") return null;
  if ((await base()) !== BACKEND) return null;
  try {
    const r = await probeStatus(BACKEND + "/api/status");
    return r.ok ? `${BACKEND}/${location.hash}` : null;
  } catch {
    return null;
  }
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// FastAPI 오류 응답에서 화면에 보여 줄 문장만 꺼낸다 (NF3002: 스택, 내부 값은 싣지 않는다)
function detailText(body, status) {
  const d = body?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) {
    return d.map((e) => (e.msg || "").replace(/^Value error, /, "")).join(" ");
  }
  return status >= 500
    ? "서버가 요청을 처리하지 못했습니다."
    : "요청을 처리하지 못했습니다.";
}

async function request(method, path, body) {
  let res;
  try {
    res = await fetch((await base()) + path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(
      0,
      "서버에 연결하지 못했습니다. 백엔드 서버가 실행 중인지 확인해 주세요.",
    );
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, detailText(data, res.status));
  return data;
}

export const api = {
  status: () => request("GET", "/api/status"),

  plans: () => request("GET", "/api/plans"),
  plan: (id) => request("GET", `/api/plans/${id}`),
  createPlan: (body) => request("POST", "/api/plans", body),
  updatePlan: (id, body) => request("PATCH", `/api/plans/${id}`, body),
  deletePlan: (id) => request("DELETE", `/api/plans/${id}`),

  logs: () => request("GET", "/api/logs"),
  log: (sessionId) => request("GET", `/api/logs/${sessionId}`),
  deleteLog: (sessionId) => request("DELETE", `/api/logs/${sessionId}`),

  // 실행 이벤트를 받는 대로 onLine에 넘기고, 마지막 응답을 돌려준다.
  async chatStream(body, onLine) {
    let res;
    try {
      res = await fetch((await base()) + "/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch {
      throw new ApiError(
        0,
        "서버에 연결하지 못했습니다. 백엔드 서버가 실행 중인지 확인해 주세요.",
      );
    }
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new ApiError(res.status, detailText(data, res.status));
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let response = null;
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        const item = JSON.parse(line);
        if (item.type === "response") response = item.response;
        onLine(item);
      }
      if (done) break;
    }
    if (!response)
      throw new ApiError(
        0,
        "응답을 끝까지 받지 못했습니다. 다시 시도해 주세요.",
      );
    return response;
  },
};
