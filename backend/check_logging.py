"""로깅 Agent와 실행 로그 확인 (F3012 ~ F3014): 실제 LLM(OpenAI)과 외부 API를 호출한다.

    python backend/check_logging.py

확인용 실행 로그는 data/logs/check.db에, 대표 요청 1건의 전체 실행 로그는 data/logs/representative_log.json에
저장한다 (형상관리 제외). 출력에 API 키를 포함하지 않는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT.parent / ".env")

OUT = ROOT.parent / "data" / "logs"
OUT.mkdir(parents=True, exist_ok=True)
DB = OUT / "check.db"
DB.unlink(missing_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import PlannerAgent  # noqa: E402
from app.agent.logging_agent import LoggingAgent  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from check_agent import REPLAN, REPRESENTATIVE  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []
NODES = {"analyze", "plan", "execute", "judge", "respond"}


def check(title: str, good: bool, detail: str = "") -> bool:
    status = PASS if good else FAIL
    results.append((status, title))
    print(f"[{status}] {title}" + (f"\n       → {detail}" if detail else ""))
    return good


def section(t: str):
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


def line(e: dict) -> str:
    d = e["data"]
    head = f"{e['seq']:>3} {e['type']:<13} {e['node'] or '':<8}"
    if e["type"] in ("tool_request", "tool_reused", "tool_skipped"):
        return f"{head} {d['step']} {d['tool']} {json.dumps(d['args'], ensure_ascii=False)}"
    if e["type"] == "tool_response":
        return f"{head} {d['step']} {d['tool']} ok={d['ok']} {d['code'] or ''} {d['elapsed_ms']}ms"
    if e["type"] == "decision":
        return f"{head} [{d['by']}] {d['kind']} = {json.dumps(d['choice'], ensure_ascii=False)} / {d['reason']}"
    if e["type"] == "node_end":
        extra = {k: v for k, v in d.items() if k in ("intent", "round", "strategy", "called", "reused", "replan", "kind")}
        return f"{head} {d['elapsed_ms']}ms {json.dumps(extra, ensure_ascii=False)}"
    return f"{head} {json.dumps(d, ensure_ascii=False)[:100]}"


def of(events: list[dict], type_: str, **match) -> list[dict]:
    return [
        e
        for e in events
        if e["type"] == type_ and all(e["data"].get(k) == v or e.get(k) == v for k, v in match.items())
    ]


def main() -> int:
    store = RunLogStore(DB)
    bus = EventBus()
    agent = PlannerAgent(bus=bus)
    app = create_app(store, agent)
    client = TestClient(app)
    logger: LoggingAgent = app.state.logging_agent

    published: list[dict] = []
    bus.subscribe(published.append)

    # 로깅 Agent가 이벤트를 처리하는 동안 LLM을 호출하면 바로 실패하도록 막는다
    llm_blocked = {"hit": 0}

    def guarded(event: dict) -> None:
        def deny(*a, **k):
            llm_blocked["hit"] += 1
            raise AssertionError("로깅 Agent가 LLM을 호출했습니다.")

        with mock.patch(
            "openai.resources.chat.completions.Completions.create", side_effect=deny
        ), mock.patch("app.agent.config.chat_model", side_effect=deny):
            original(event)

    logger.stop()
    original = logger.handle
    logger.handle = guarded  # type: ignore[method-assign]
    logger.start(bus)

    section("1. F3012 실행 이벤트 발행 (대표 요청 1건)")
    sid = agent.new_session()
    response = agent.run(sid, REPRESENTATIVE)
    events = [e for e in published if e["session_id"] == sid]
    for e in events:
        print("  " + line(e))
    starts = [e["node"] for e in of(events, "node_start")]
    ends = [e["node"] for e in of(events, "node_end")]
    check(
        "단계마다 진입, 종료 이벤트를 짝지어 발행",
        starts == ends and set(starts) == NODES,
        " → ".join(starts),
    )
    req, res = of(events, "tool_request"), of(events, "tool_response")
    check(
        "Tool 호출 요청, 응답, 소요 시간 발행",
        bool(req)
        and [e["data"]["step"] for e in req] == [e["data"]["step"] for e in res]
        and all(isinstance(e["data"]["elapsed_ms"], int) for e in res),
        ", ".join(f"{e['data']['tool']} {e['data']['elapsed_ms']}ms" for e in res),
    )
    analyze_end = of(events, "node_end", node="analyze")[0]["data"]
    llm = of(events, "decision", by="llm")
    check(
        "의도 분석 종료 이벤트에 분류한 의도와 이유",
        analyze_end["intent"] == response["intent"] and bool(analyze_end["intent_reason"]),
        f"{analyze_end['intent']}: {analyze_end['intent_reason']}",
    )
    check(
        "LLM 판단(의도 분류, 계획 선택)과 이유를 이벤트로 발행",
        [d["data"]["kind"] for d in llm] == ["intent", "plan"] and all(d["data"]["reason"] for d in llm),
        " / ".join(f"{d['data']['kind']}: {d['data']['reason']}" for d in llm),
    )
    check(
        "모든 이벤트에 같은 세션 id, 발생 순서",
        all(e["session_id"] == sid for e in events)
        and [e["seq"] for e in events] == list(range(1, len(events) + 1)),
        f"이벤트 {len(events)}건",
    )

    section("2. 재계획 요청의 판단 이벤트")
    sid2 = agent.new_session()
    agent.run(sid2, REPLAN)
    ev2 = [e for e in published if e["session_id"] == sid2]
    replans = of(ev2, "decision", kind="replan")
    for d in of(ev2, "decision"):
        print("  " + line(d))
    check(
        "재계획 판단과 사유를 이벤트로 발행",
        bool(replans) and all(r["data"]["reason"] for r in replans),
        " / ".join(r["data"]["choice"] for r in replans),
    )

    section("3. F3013 로깅 Agent 수집과 저장")
    stored = store.get_session(sid)
    same = [
        {k: e[k] for k in ("session_id", "run_id", "seq", "type", "node", "ts", "data")}
        for e in stored
    ] == json.loads(json.dumps(events, ensure_ascii=False, default=str))
    check("발행한 이벤트를 가공하지 않고 발생 순서대로 저장", same, f"저장 {len(stored)}건")
    check(
        "수집과 저장 중 LLM 호출 없음",
        llm_blocked["hit"] == 0 and logger.failures == 0,
        f"차단된 LLM 호출 {llm_blocked['hit']}회, 저장 실패 {logger.failures}건",
    )
    (OUT / "representative_log.json").write_text(
        json.dumps({"request": REPRESENTATIVE, "events": stored}, ensure_ascii=False, indent=2), "utf-8"
    )
    stored2 = store.get_session(sid2)
    check(
        "서로 다른 세션의 로그가 섞이지 않음",
        all(e["session_id"] == sid for e in stored) and all(e["session_id"] == sid2 for e in stored2),
        f"{sid[:8]} {len(stored)}건 / {sid2[:8]} {len(stored2)}건",
    )

    section("4. 로깅 Agent를 연결하지 않고 실행")
    logger.stop()
    sid3 = agent.new_session()
    r3 = agent.run(sid3, "안녕하세요, 어떻게 쓰는 거예요?")
    check(
        "메인 Agent 정상 응답, 로그는 남지 않음",
        r3["kind"] == "chat" and store.get_session(sid3) is None,
        r3["text"][:60],
    )
    logger.start(bus)

    section("5. F3014 실행 로그 조회, 삭제 API")
    listed = client.get("/api/logs").json()["sessions"]
    for s in listed:
        print(f"  {s['session_id'][:8]} 요청 {s['runs']}, 이벤트 {s['events']}, Tool {s['tool_calls']}, 재계획 {s['replans']} / {s['first_message'][:30]}")
    check(
        "GET /api/logs 세션 단위 목록",
        {s["session_id"] for s in listed} == {sid, sid2},
        f"{len(listed)}개 세션",
    )
    detail = client.get(f"/api/logs/{sid}").json()["events"]
    check(
        "GET /api/logs/{session_id} 실행 순서대로 상세",
        [e["id"] for e in detail] == sorted(e["id"] for e in detail)
        and detail[0]["type"] == "run_start"
        and detail[-1]["type"] == "run_end",
        f"{detail[0]['type']} … {detail[-1]['type']} ({len(detail)}건)",
    )
    deleted = client.delete(f"/api/logs/{sid2}")
    check(
        "DELETE /api/logs/{session_id} 삭제",
        deleted.status_code == 200
        and client.get(f"/api/logs/{sid2}").status_code == 404
        and store.get_session(sid) is not None,
        json.dumps(deleted.json(), ensure_ascii=False),
    )
    paths = app.openapi()["paths"]
    methods = sorted({f"{m.upper()} {p}" for p, ops in paths.items() if p.startswith("/api/logs") for m in ops})
    check(
        "실행 로그 수정 API 없음 (PUT, PATCH 405)",
        all(not m.startswith(("PUT", "PATCH", "POST")) for m in methods)
        and client.put(f"/api/logs/{sid}", json={}).status_code == 405
        and client.patch(f"/api/logs/{sid}", json={}).status_code == 405,
        ", ".join(methods),
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
