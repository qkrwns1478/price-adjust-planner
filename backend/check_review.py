"""로깅 Agent 이상 징후 판단 확인 (F3020): 실제 LLM(OpenAI)과 외부 API를 호출한다.

    python backend/check_review.py

- 재계획 과도 반복: 실제 요청으로 재계획 상한에 도달시킨다.
- 같은 입력 반복 호출, 계획에 없던 호출: 메인 Agent는 캐시와 계획 검증 때문에 이런 호출을 하지 않으므로,
  실제 대표 요청의 실행 로그에 해당 호출을 끼워 넣은 실행을 만들어 로깅 Agent에 발행한다.

확인용 로그는 data/logs/review.db, 판정과 진단은 data/logs/reviews.json에 저장한다 (형상관리 제외).
"""

from __future__ import annotations

import copy
import json
import sys
import time
import uuid
from pathlib import Path

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
DB = OUT / "review.db"
DB.unlink(missing_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import PlannerAgent  # noqa: E402
from app.agent.config import max_replan  # noqa: E402
from app.agent.logging_agent import LoggingAgent  # noqa: E402
from app.agent.reviewer import make_llm_diagnoser  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from check_agent import LIMIT, REPRESENTATIVE  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def check(title: str, good: bool, detail: str = "") -> bool:
    status = PASS if good else FAIL
    results.append((status, title))
    print(f"[{status}] {title}" + (f"\n       → {detail}" if detail else ""))
    return good


def section(t: str):
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


def show(review: dict | None) -> None:
    if not review:
        print("  (판정 없음)")
        return
    print(f"  판정: {review['verdict']}, 경고 {review['warnings']}건")
    for c in review["checks"]:
        print(f"    {c['status']:<12} {c['label']}: {c['message']}")
    d = review.get("diagnosis")
    if d:
        print(f"  진단({d['status']}): [{d['agent']}/{d['stage']}] {d['text']}")


def index_of(events, type_, **match) -> int:
    for i, e in enumerate(events):
        if e["type"] == type_ and all(
            e["data"].get(k) == v or e.get(k) == v for k, v in match.items()
        ):
            return i
    raise LookupError(type_)


def as_new_run(events: list[dict]) -> list[dict]:
    """저장된 이벤트를 새 세션, 새 요청의 이벤트로 바꾸고 순서를 다시 매긴다."""
    sid, rid = uuid.uuid4().hex, uuid.uuid4().hex
    out = []
    for i, e in enumerate(copy.deepcopy(events), 1):
        out.append(
            {
                "session_id": sid,
                "run_id": rid,
                "seq": i,
                "type": e["type"],
                "node": e["node"],
                "ts": e["ts"],
                "data": e["data"],
            }
        )
    return out


def repeated(events: list[dict]) -> list[dict]:
    out = copy.deepcopy(events)
    i = index_of(out, "tool_response", step="s2")
    out[i + 1 : i + 1] = copy.deepcopy(out[i - 1 : i + 1])
    out[index_of(out, "node_end", node="execute")]["data"]["called"] += 1
    return as_new_run(out)


def unplanned(events: list[dict]) -> list[dict]:
    out = copy.deepcopy(events)
    i = index_of(out, "node_end", node="execute")
    base = {"node": "execute", "ts": out[i]["ts"]}
    out[i:i] = [
        {
            **base,
            "type": "tool_request",
            "data": {
                "step": "s9",
                "round": 0,
                "tool": "get_industrial_power_price",
                "args": {"region": "대구"},
            },
        },
        {
            **base,
            "type": "tool_response",
            "data": {
                "step": "s9",
                "round": 0,
                "tool": "get_industrial_power_price",
                "ok": True,
                "code": None,
                "elapsed_ms": 0,
                "result": {},
            },
        },
    ]
    out[i + 2]["data"]["called"] += 1
    return as_new_run(out)


def main() -> int:
    store = RunLogStore(DB)
    agent = PlannerAgent(bus=EventBus())
    app = create_app(store, agent)
    client = TestClient(app)
    logger: LoggingAgent = app.state.logging_agent

    llm = {"calls": 0}
    real = make_llm_diagnoser()

    def counted(review, events):
        llm["calls"] += 1
        return real(review, events)

    logger._diagnoser = counted
    saved: dict[str, dict] = {}

    def publish(events: list[dict]) -> dict | None:
        for e in events:
            agent.bus.publish(e)
        logger.wait(60)
        return store.get_review(events[0]["run_id"])

    section("1. 정상 실행: 판정만 하고 LLM을 호출하지 않음")
    sid = agent.new_session()
    response = agent.run(sid, REPRESENTATIVE)
    logger.wait(60)
    normal = store.list_reviews(sid)[0]
    show(normal)
    saved["normal"] = normal
    check(
        "대표 요청 판정 = 이상 없음 (모든 검사 ok)",
        normal["verdict"] == "normal"
        and all(c["status"] == "ok" for c in normal["checks"]),
    )
    check(
        "정상 실행에는 진단 LLM 호출 0회",
        llm["calls"] == 0 and normal["diagnosis"] is None,
        f"진단 LLM 호출 {llm['calls']}회",
    )
    check(
        "판정 결과를 메인 Agent가 응답에서 참조",
        (response.get("review") or {}).get("run_id") == normal["run_id"]
        and response["review"]["verdict"] == "normal",
        f"response.review.verdict = {(response.get('review') or {}).get('verdict')}",
    )
    base_events = store.get_run(normal["run_id"])

    section("2. 재계획 과도 반복 (실제 요청)")
    sid2 = agent.new_session()
    started = time.perf_counter()
    r2 = agent.run(sid2, LIMIT)
    elapsed = time.perf_counter() - started
    logger.wait(60)
    rv2 = store.list_reviews(sid2)[0]
    show(rv2)
    saved["excessive_replan"] = rv2
    c2 = rv2["checks"][1]
    check(
        f"재계획 {max_replan()}회 상한 도달을 이상으로 식별",
        rv2["verdict"] == "anomaly"
        and c2["status"] == "warn"
        and c2["items"][0]["reason"] == "limit",
        c2["message"],
    )
    check(
        "응답 시간에 진단이 섞이지 않음 (진단은 별도 스레드)",
        r2["review"]["diagnosis"] is None and rv2["diagnosis"] is not None,
        f"응답 {elapsed:.1f}초, 응답 시점 진단 = {r2['review']['diagnosis']}",
    )

    section("3. 같은 입력 반복 호출 (로그에 호출을 끼워 넣음)")
    rv3 = publish(repeated(base_events))
    show(rv3)
    saved["repeated_call"] = rv3
    c3 = rv3["checks"][0]
    check(
        "같은 Tool, 같은 입력 반복을 이상으로 식별",
        rv3["verdict"] == "anomaly"
        and c3["status"] == "warn"
        and c3["items"][0]["count"] == 2,
        c3["message"],
    )

    section("4. 계획에 없던 Tool 호출 (로그에 호출을 끼워 넣음)")
    rv4 = publish(unplanned(base_events))
    show(rv4)
    saved["unplanned_call"] = rv4
    c4 = rv4["checks"][2]
    check(
        "계획에 없던 호출을 이상으로 식별",
        rv4["verdict"] == "anomaly"
        and c4["status"] == "warn"
        and c4["items"][0]["step"] == "s9",
        c4["message"],
    )

    section("5. 근거 이벤트가 없는 실행")
    broken = as_new_run(base_events)[:-1]
    for e in broken:
        agent.bus.publish(e)
    before = llm["calls"]
    rv5 = logger.review(broken[0]["session_id"], broken[0]["run_id"])
    logger.wait(60)
    show(rv5)
    no_plan = as_new_run(
        [
            e
            for e in base_events
            if not (e["type"] == "node_end" and e["node"] == "plan")
        ]
    )
    rv5b = publish(no_plan)
    show(rv5b)
    saved["undetermined"] = rv5b
    check(
        "종료 이벤트가 없으면 '판정 불가'",
        rv5["verdict"] == "undetermined"
        and all(c["status"] == "undetermined" for c in rv5["checks"]),
        rv5["checks"][0]["message"],
    )
    check(
        "계획 이벤트가 없으면 계획 외 호출 검사는 '판정 불가', 이상 없음으로 두지 않음",
        rv5b["verdict"] == "undetermined"
        and rv5b["checks"][2]["status"] == "undetermined",
        rv5b["checks"][2]["message"],
    )
    check(
        "판정 불가에는 진단 LLM 호출 없음",
        llm["calls"] == before,
        f"진단 LLM 호출 {llm['calls'] - before}회",
    )

    section("6. 진단")
    anomalies = [rv2, rv3, rv4]
    check(
        "이상으로 판정한 실행에만 진단 (LLM 호출 = 이상 실행 수)",
        llm["calls"] == len(anomalies)
        and all(r["diagnosis"] and r["diagnosis"]["status"] == "ok" for r in anomalies),
        f"진단 LLM 호출 {llm['calls']}회 / 이상 {len(anomalies)}건",
    )
    sentences = [
        len([s for s in r["diagnosis"]["text"].replace("!", ".").split(". ") if s])
        for r in anomalies
    ]
    check(
        "진단은 어느 Agent 쪽 문제인지와 세 문장 이내의 원인",
        all(
            r["diagnosis"]["agent"] in ("planner", "worker", "main", "logging", "tool")
            and r["diagnosis"]["text"]
            for r in anomalies
        )
        and max(sentences) <= 3,
        " / ".join(
            f"{r['diagnosis']['agent']}:{n}문장" for r, n in zip(anomalies, sentences)
        ),
    )
    check(
        "진단이 판정의 경고 수에 섞이지 않음",
        [r["warnings"] for r in anomalies]
        == [
            sum(len(c["items"]) for c in r["checks"] if c["status"] == "warn")
            for r in anomalies
        ],
        f"경고 수 {[r['warnings'] for r in anomalies]}",
    )

    fail_logger = LoggingAgent(
        store,
        diagnoser=lambda r, e: (_ for _ in ()).throw(RuntimeError("진단 실패")),
        diagnose_async=False,
    )
    logger.stop()
    fail_logger.start(agent.bus)
    failing = repeated(base_events)
    rv6 = publish(failing)
    fail_logger.stop()
    logger.start(agent.bus)
    check(
        "진단이 실패해도 판정과 실행 로그는 그대로 남음",
        rv6["verdict"] == "anomaly"
        and rv6["diagnosis"]["status"] == "failed"
        and len(store.get_run(failing[0]["run_id"])) == len(failing),
        f"verdict = {rv6['verdict']}, diagnosis.status = {rv6['diagnosis']['status']}",
    )

    section("7. API")
    detail = client.get(f"/api/logs/{sid2}").json()
    listed = {s["session_id"]: s for s in client.get("/api/logs").json()["sessions"]}
    check(
        "GET /api/logs/{session_id} 에 요청별 판정과 진단",
        detail["reviews"][0]["verdict"] == "anomaly"
        and detail["reviews"][0]["diagnosis"]["status"] == "ok",
        detail["reviews"][0]["diagnosis"]["text"],
    )
    check(
        "GET /api/logs에 세션별 이상 실행 수",
        listed[sid]["anomalies"] == 0 and listed[sid2]["anomalies"] == 1,
    )

    (OUT / "reviews.json").write_text(
        json.dumps(saved, ensure_ascii=False, indent=2), "utf-8"
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
