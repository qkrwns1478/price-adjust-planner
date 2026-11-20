"""서비스 구성, 대표 시나리오, 비기능 요구사항 확인: 실제 LLM(OpenAI)과 외부 API를 호출한다.

    python backend/check_service.py

모든 요청은 화면과 같은 경로(POST /api/chat/stream)로 보낸다.
대표 시나리오별 응답과 실행 로그는 data/scenarios/ 에 JSON으로 저장한다 (형상관리 제외). 출력에 API 키를 포함하지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from dotenv import dotenv_values, load_dotenv  # noqa: E402

load_dotenv(PROJECT / ".env")

OUT = PROJECT / "data" / "scenarios"
OUT.mkdir(parents=True, exist_ok=True)
DB = OUT / "service.db"
DB.unlink(missing_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import PlannerAgent  # noqa: E402
from app.agent.config import max_replan  # noqa: E402
from app.api.system import KEYS  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from app.tools import call_tool  # noqa: E402
from app.tools.base import ErrorCode, fail  # noqa: E402
from check_agent import LIMIT, REPLAN, REPRESENTATIVE  # noqa: E402

# 대조할 비밀 값은 API 키 4종만이다 (모델 이름, 주소 같은 설정 값은 제외)
SECRETS = [
    v for k, v in dotenv_values(PROJECT / ".env").items() if k in KEYS and v and v != "sample"
]

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []
NODES = ["analyze", "plan", "execute", "judge", "respond"]
FRONTEND = PROJECT / "frontend"


def check(title: str, good: bool, detail: str = "") -> bool:
    status = PASS if good else FAIL
    results.append((status, title))
    print(f"[{status}] {title}" + (f"\n       → {detail}" if detail else ""))
    return good


def section(t: str):
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


def leaks(text: str) -> list[str]:
    """출력에 들어 있는 .env 값의 개수만 돌려준다 (값 자체는 싣지 않는다)."""
    return [f"키 {i + 1}" for i, s in enumerate(SECRETS) if s in text]


def stream(client: TestClient, message: str, session_id: str | None = None) -> dict:
    started = time.perf_counter()
    r = client.post(
        "/api/chat/stream", json={"session_id": session_id, "message": message}
    )
    elapsed = time.perf_counter() - started
    lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    events = [x["event"] for x in lines if x["type"] == "event"]
    response = next(x["response"] for x in lines if x["type"] == "response")
    return {
        "status": r.status_code,
        "content_type": r.headers.get("content-type", ""),
        "raw": r.text,
        "lines": lines,
        "events": events,
        "response": response,
        "elapsed": elapsed,
    }


def nodes(events: list[dict]) -> list[str]:
    return [e["node"] for e in events if e["type"] == "node_start"]


def tool_history(events: list[dict]) -> list[str]:
    out = []
    for e in events:
        d = e["data"]
        if e["type"] == "tool_response":
            out.append(
                f"[{d['round']}] {d['tool']} {'ok' if d['ok'] else d['code']} {d['elapsed_ms']}ms"
            )
        elif e["type"] == "tool_reused":
            out.append(f"[{d['round']}] {d['tool']} 재사용")
        elif e["type"] == "tool_skipped":
            out.append(f"[{d['round']}] {d['tool']} 건너뜀")
    return out


def replans(events: list[dict]) -> list[dict]:
    return [
        e for e in events if e["type"] == "decision" and e["data"]["kind"] == "replan"
    ]


def flow(events: list[dict]) -> str:
    """노드 진행에 재계획 지점을 끼워 넣은 한 줄 요약."""
    parts = []
    for e in events:
        if e["type"] == "node_start":
            parts.append(e["node"])
        elif e["type"] == "decision" and e["data"]["kind"] == "replan":
            parts.append(f"⟲재계획({e['data']['choice']})")
    return " → ".join(parts)


def save(name: str, request: str, s: dict, expected: str, actual: str) -> None:
    r = s["response"]
    (OUT / f"{name}.json").write_text(
        json.dumps(
            {
                "request": request,
                "expected": expected,
                "actual": actual,
                "elapsed_sec": round(s["elapsed"], 1),
                "response": r,
                "events": s["events"],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        "utf-8",
    )


def scenario(n: int, title: str, request: str, s: dict, expected: str) -> str:
    r = s["response"]
    actual = (
        f"kind={r['kind']}, 재계획 {len(r['replans'])}회"
        + (f", 멈춘 사유: {r['stop_reason']}" if r.get("stop_reason") else "")
        + (f", 재시도 무의미 실패 {r['fatal']}" if r.get("fatal") else "")
    )
    print(f"\n  ({n}) {title}\n  요청: {request}")
    print(f"  흐름: {flow(s['events'])}")
    print(f"  Tool: {' / '.join(tool_history(s['events'])) or '호출 없음'}")
    print(f"  결과: {actual}, {s['elapsed']:.1f}초")
    print(f"  요약: {r.get('summary') or r['text'][:80]}")
    save(f"{n}", request, s, expected, actual)
    return actual


def main() -> int:
    store = RunLogStore(DB)
    client = TestClient(create_app(store))

    def broken(name: str, args: dict) -> dict:
        if name == "search_law_article":
            return fail(ErrorCode.UPSTREAM_ERROR, "일부러 실패시킨 호출입니다.")
        return call_tool(name, args)

    broken_client = TestClient(
        create_app(RunLogStore(DB), PlannerAgent(tool_caller=broken))
    )

    # ── 4.7 대표 시나리오 (4.5, 4.8 확인에도 같은 실행을 쓴다) ──
    section("4.7 대표 시나리오 5건")
    rep = stream(client, REPRESENTATIVE)
    rp = stream(client, REPLAN)
    lim = stream(client, LIMIT)
    brk = stream(broken_client, REPRESENTATIVE)
    chat = stream(client, "이 서비스는 어떻게 쓰면 돼?")
    after = stream(client, "안녕하세요")  # 실패 요청 뒤에도 서버가 응답하는지 (NF3003)

    cases = [
        (
            "제약을 한 번에 충족",
            REPRESENTATIVE,
            rep,
            "열연강판 변동률이 약정 5% 이상이라 재계획 없이 요청 금액과 근거 조문 제시",
            lambda r, s: r["kind"] == "result"
            and not r["replans"]
            and bool(r["recommendations"]),
        ),
        (
            "재계획이 필요",
            REPLAN,
            rp,
            "8.99% < 10% 미달 → 재조회 계획(recheck), 대안 경로(alt_path)로 재계획. 확보한 지수는 다시 부르지 않음",
            lambda r, s: len(r["replans"]) >= 1
            and sum(
                1
                for e in s["events"]
                if e["type"] == "tool_request"
                and e["data"]["tool"] == "get_material_price_index"
            )
            == 1,
        ),
        (
            "재계획 상한에 도달",
            LIMIT,
            lim,
            f"두 후보 모두 12% 미달 → 재계획 {max_replan()}회에서 멈추고 사유와 함께 조건 조정을 되물음",
            lambda r, s: len(r["replans"]) == max_replan()
            and "상한" in (r["stop_reason"] or ""),
        ),
        (
            "Tool 하나가 실패",
            REPRESENTATIVE + " (search_law_article을 UPSTREAM_ERROR로 실패시킴)",
            brk,
            "근거 조문 검색만 실패하고 지수 계산과 요청 금액은 그대로 제시. 다시 불러도 같은 실패라 재계획하지 않음",
            lambda r, s: r["kind"] == "result"
            and bool(r["recommendations"])
            and "UPSTREAM_ERROR" in r["fatal"]
            and not r["replans"],
        ),
        (
            "Tool이 필요 없음",
            "이 서비스는 어떻게 쓰면 돼?",
            chat,
            "의도 chat, 계획과 Tool 호출 없이 사용법 안내",
            lambda r, s: r["kind"] == "chat"
            and nodes(s["events"]) == ["analyze", "respond"]
            and not tool_history(s["events"]),
        ),
    ]
    for n, (title, request, s, expected, ok) in enumerate(cases, 1):
        scenario(n, title, request, s, expected)
        check(f"시나리오 {n} {title}: 기대한 동작과 같음", ok(s["response"], s), expected)

    # ── 4.5 서비스 구성과 화면 (F3017 ~ F3019) ──
    section("4.5 F3017 API 경로와 형식")
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    groups = {
        "대화": ["/api/chat", "/api/chat/stream"],
        "계획 관리": ["/api/plans", "/api/plans/{plan_id}"],
        "실행 로그": ["/api/logs", "/api/logs/{session_id}"],
    }
    check(
        "대화, 계획 관리, 실행 로그 API가 서로 다른 경로",
        all(p in paths for ps in groups.values() for p in ps),
        " / ".join(f"{k}: {', '.join(v)}" for k, v in groups.items()),
    )
    schemas = set(spec["components"]["schemas"])
    need = {
        "ChatRequest",
        "ChatResponse",
        "PlanCreate",
        "PlanUpdate",
        "PlanSaved",
        "PlanList",
        "SessionList",
        "SessionLog",
    }
    check(
        "요청, 응답 형식을 Pydantic 모델로 정의 (OpenAPI 스키마)",
        need <= schemas,
        ", ".join(sorted(need)),
    )
    from app.api.chat import ChatResponse

    check(
        "실제 대화 응답이 정의한 형식(ChatResponse)과 일치",
        all(
            ChatResponse.model_validate(s["response"])
            for s in (rep, rp, lim, brk, chat)
        )
        and rep["content_type"].startswith("application/x-ndjson"),
        f"5건 검증, 스트림 형식 {rep['content_type']}",
    )
    draft = dict(rep["response"]["plan_drafts"][0])
    draft.update(meeting_date="2026-11-25", start_time="14:00", end_time="15:00")
    saved = client.post(
        "/api/plans",
        json=draft | {"session_id": rep["response"]["session_id"]},
    )
    other = client.post(
        "/api/plans",
        json=draft | {"client": "B사", "start_time": "14:30", "end_time": "16:00"},
    )
    log_list = client.get("/api/logs").json()
    check(
        "계획 저장(201, PlanSaved), 실행 로그 목록(SessionList) 응답 형식",
        saved.status_code == 201
        and set(saved.json()) == {"plan", "overlaps", "duplicates"}
        and [p["id"] for p in other.json()["overlaps"]] == [saved.json()["plan"]["id"]]
        and len(log_list["sessions"]) >= 5,
        f"계획 id {saved.json()['plan']['id']}, 겹침 {len(other.json()['overlaps'])}건, 로그 세션 {len(log_list['sessions'])}개",
    )

    section("4.5 F3018 화면이 API를 호출해 표시")
    page = client.get("/")
    api_js = (FRONTEND / "js" / "api.js").read_text("utf-8")
    calls = {
        "chat.js": ["api.chatStream"],
        "plans.js": ["api.plans", "api.updatePlan", "api.deletePlan"],
        "logs.js": ["api.logs", "api.log", "api.deleteLog"],
    }
    found = {
        f: [c for c in cs if c in (FRONTEND / "js" / f).read_text("utf-8")]
        for f, cs in calls.items()
    }
    check(
        "대화, 계획 관리, 실행 로그 화면이 모두 백엔드 API를 호출",
        page.status_code == 200
        and all(
            p in api_js
            for p in ("/api/chat/stream", "/api/plans", "/api/logs", "/api/status")
        )
        and all(len(found[f]) == len(cs) for f, cs in calls.items()),
        " / ".join(f"{f}: {', '.join(v)}" for f, v in found.items()),
    )
    check(
        "화면은 Python 코드 없이 HTML, CSS, JavaScript로만 구성",
        not list(FRONTEND.rglob("*.py"))
        and {p.suffix for p in FRONTEND.rglob("*.*")} <= {".html", ".css", ".js"},
        ", ".join(sorted({p.suffix for p in FRONTEND.rglob("*.*")})),
    )

    section("4.5 F3019 실행 과정과 상태 표시")
    ev = rep["events"]
    check(
        "실행 단계와 Tool 호출이 실행 순서대로 흘러옴 (seq 1..n, run_start → run_end)",
        [e["seq"] for e in ev] == list(range(1, len(ev) + 1))
        and ev[0]["type"] == "run_start"
        and ev[-1]["type"] == "run_end"
        and nodes(ev) == NODES,
        f"{flow(ev)} / Tool {len(tool_history(ev))}건",
    )
    marks = replans(rp["events"])
    first = rp["events"].index(marks[0]) if marks else -1
    trace_js = (FRONTEND / "js" / "trace.js").read_text("utf-8")
    check(
        "재계획 지점이 decision(replan) 이벤트로 구분되고, 화면이 따로 그림",
        bool(marks)
        and next(e["node"] for e in rp["events"][first:] if e["type"] == "node_start")
        == "plan"
        and 'el("li", "replan")' in trace_js,
        flow(rp["events"]),
    )
    chat_js = (FRONTEND / "js" / "chat.js").read_text("utf-8")
    check(
        "처리 중 상태: 진행 중인 단계와 Tool 이름을 표시",
        "pending-text" in chat_js
        and "호출 중" in chat_js
        and '"처리 중"' in trace_js,
        "node_start → '<단계> 중', tool_request → '<Tool> 호출 중'",
    )
    failed = [
        e
        for e in brk["events"]
        if e["type"] == "tool_response" and not e["data"]["ok"]
    ]
    check(
        "Tool 실패를 이벤트와 응답(fatal, evidence)으로 알림",
        failed
        and {e["data"]["code"] for e in failed} == {"UPSTREAM_ERROR"}
        and any(not x["ok"] for x in brk["response"]["evidence"])
        and "failureNotice" in chat_js,
        f"실패 이벤트 {len(failed)}건, {brk['response']['stop_reason']}",
    )
    blank = {name: "" for name in KEYS if name != "OPENAI_API_KEY"}
    with mock.patch.dict(os.environ, blank):
        status = client.get("/api/status").json()
        nokey = stream(client, REPRESENTATIVE)
    r = nokey["response"]
    check(
        "API 키 누락: 설정 상태와 응답에 빠진 키 이름을 싣고, 화면이 설정 안내를 표시",
        not status["ready"]
        and sorted(r["missing_keys"]) == sorted(blank)
        and "INVALID_KEY" in r["fatal"]
        and "알려 주시면 다시 계산" not in r["text"]
        and "showSetup(r.missing_keys)" in chat_js,
        f"missing_keys {r['missing_keys']}, {r['stop_reason']}",
    )
    save(
        "missing_key",
        REPRESENTATIVE + " (ECOS, 한전, 법제처 키를 비움)",
        nokey,
        "키 누락 안내",
        r["stop_reason"] or "",
    )

    # ── 4.8 비기능 요구사항 ──
    section("4.8 비기능 요구사항 (NF3001 ~ NF3007)")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT, capture_output=True, text=True, check=True
    ).stdout.split()
    leaked_files = [
        f
        for f in tracked
        if (PROJECT / f).is_file()
        and leaks((PROJECT / f).read_text("utf-8", errors="ignore"))
    ]
    check(
        "NF3001 형상관리 파일에 API 키 값이 없고 .env는 제외",
        not leaked_files and ".env" not in tracked and ".env.example" in tracked,
        f"파일 {len(tracked)}개 대조, 키 {len(SECRETS)}종, 걸린 파일 {leaked_files or '없음'}",
    )
    outputs = [rep, rp, lim, brk, chat, after, nokey]
    shown = "".join(s["raw"] for s in outputs)
    with mock.patch.object(
        client.app.state.agent,
        "run",
        side_effect=RuntimeError("Traceback secret http://internal"),
    ):
        crash = stream(client, REPRESENTATIVE)
    check(
        "NF3002 화면에 가는 응답에 키 값과 오류 상세(예외 원문)가 없음",
        not leaks(shown + crash["raw"])
        and "Traceback" not in crash["raw"]
        and crash["response"]["kind"] == "error",
        f"응답 {len(outputs) + 1}건 대조. 예외 시 응답: {crash['response']['text']}",
    )
    check(
        "NF3003 Tool 실패에도 응답하고, 다음 요청도 정상 처리",
        brk["status"] == 200
        and brk["response"]["kind"] == "result"
        and after["status"] == 200
        and after["response"]["kind"] == "chat",
        f"실패 요청 {brk['response']['kind']}, 다음 요청 {after['response']['kind']}",
    )
    check(
        f"NF3004 재계획 상한({max_replan()}회)에서 멈춤",
        len(lim["response"]["replans"]) == max_replan(),
        lim["response"]["stop_reason"] or "",
    )
    times = {
        "대표": rep["elapsed"],
        "재계획": rp["elapsed"],
        "상한": lim["elapsed"],
        "실패": brk["elapsed"],
        "Tool 없음": chat["elapsed"],
    }
    check(
        "NF3005 대표 요청 처리 시간 30초 미만",
        max(times.values()) < 30,
        ", ".join(f"{k} {v:.1f}초" for k, v in times.items()),
    )
    agent_files = sorted(p.name for p in (ROOT / "app" / "agent").glob("*.py"))
    tool_files = sorted(p.name for p in (ROOT / "app" / "tools").glob("*.py"))
    check(
        "NF3006 프론트엔드와 백엔드 분리, Agent와 Tool 코드 파일 분리",
        FRONTEND.is_dir()
        and not re.search(r"\.\./backend|import .*app\.", api_js)
        and {"graph.py", "planner.py", "worker.py", "logging_agent.py"}
        <= set(agent_files)
        and {"ecos.py", "kepco.py", "law.py", "registry.py"} <= set(tool_files),
        f"agent {len(agent_files)}개, tools {len(tool_files)}개 파일",
    )
    readme = (PROJECT / "README.md").read_text("utf-8")
    check(
        "NF3007 README에 외부 API 3종의 출처와 이용 조건, 호출 한도 기록",
        all(s in readme for s in ("ECOS", "법제처", "한국전력", "호출 한도", "이용 조건")),
        "README.md '사용한 외부 API의 출처 및 이용 조건'",
    )

    section("요약")
    n_pass = sum(1 for s, _ in results if s == PASS)
    print(f"{n_pass}/{len(results)} PASS")
    for s, t in results:
        if s == FAIL:
            print(f"  FAIL  {t}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
