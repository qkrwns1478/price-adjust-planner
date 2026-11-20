"""Planner·Worker 구조 확인 (F3021): 실제 LLM(OpenAI)과 외부 API를 호출한다.

    python backend/check_worker.py

- 정상 경로, 치명적 실패, 결과 없음 재시도, 계획 변경 요청은 실제 요청으로 확인한다.
- Planner 초안의 검색어가 ECOS 품목명과 다른 상황은 LLM 초안에 따라 나오지 않을 수 있으므로,
  실제 LLM 초안을 받은 뒤 품목 검색어만 사용자가 말한 이름으로 맞춰 재현한다.
- 고정 인자 변경은 Worker가 코드로 막으므로, 로깅 Agent 검증 경로는 고정 인자 보호를 끈 Worker에
  고정 인자를 바꾸는 판단을 넣어 재현한다.

확인용 로그는 data/logs/worker.db, 요약은 data/logs/worker_check.json에 저장한다 (형상관리 제외).
"""

from __future__ import annotations

import json
import os
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
DB = OUT / "worker.db"
DB.unlink(missing_ok=True)

from app.agent import PlannerAgent  # noqa: E402
from app.agent.logging_agent import LoggingAgent  # noqa: E402
from app.agent.planner import DraftStep, PlanDraft, make_llm_selector  # noqa: E402
from app.agent.worker import (
    ArgChange,
    Worker,
    WorkerDecision,
    make_llm_decider,
)  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from app.tools import call_tool  # noqa: E402
from check_agent import REPRESENTATIVE  # noqa: E402

PP = (
    "B사 납품 케이스는 폴리프로필렌이 원가의 30%야. 약정서에는 5% 이상 변동하면 분기마다 조정한다고 돼 있어. "
    "기준 시점은 작년 12월이야. 이번 분기에 요청할 수 있어?"
)
PP_INDEX = PP.replace(
    "이번 분기에", "약정서의 기준 지표도 폴리프로필렌이야. 이번 분기에"
)

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []
summary: dict = {}


def check(title: str, good: bool, detail: str = "") -> bool:
    status = PASS if good else FAIL
    results.append((status, title))
    print(f"[{status}] {title}" + (f"\n       → {detail}" if detail else ""))
    return good


def section(t: str):
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


store = RunLogStore(DB)


def wired(**kw) -> tuple[PlannerAgent, LoggingAgent]:
    bus = EventBus()
    logger = LoggingAgent(store, diagnose_async=False)
    logger.start(bus)
    agent = PlannerAgent(
        bus=bus,
        verifier=logger.verification,
        review_source=lambda s, r: store.get_review(r),
        **kw,
    )
    return agent, logger


def run(agent: PlannerAgent, message: str):
    sid = agent.new_session()
    r = agent.run(sid, message)
    events = store.get_run(r["run_id"])
    return r, agent.state(sid), events


def of(events, type_, **match):
    return [
        e
        for e in events
        if e["type"] == type_
        and all(e["data"].get(k) == v or e.get(k) == v for k, v in match.items())
    ]


def worker_llm(events) -> list[dict]:
    return [e["data"] for e in of(events, "decision", kind="worker", by="llm")]


def worker_rule(events) -> list[dict]:
    return [e["data"] for e in of(events, "decision", kind="worker", by="rule")]


def messages(events, kind) -> list[dict]:
    return [e["data"] for e in of(events, "agent_message", kind=kind)]


def name_keyword_selector():
    """실제 LLM 초안을 받고 품목 검색어만 사용자가 말한 원재료 이름으로 맞춘다."""
    real = make_llm_selector()

    def select(intent, conditions, message):
        draft = real(intent, conditions, message)
        return PlanDraft(
            steps=[
                (
                    DraftStep(**{**s.model_dump(), "keyword": s.item or s.keyword})
                    if s.tool == "search_material_item"
                    else s
                )
                for s in draft.steps
            ],
            reason=draft.reason,
        )

    return select


def keyword_selector(keyword: str):
    real = make_llm_selector()

    def select(intent, conditions, message):
        draft = real(intent, conditions, message)
        return PlanDraft(
            steps=[
                (
                    DraftStep(**{**s.model_dump(), "keyword": keyword})
                    if s.tool == "search_material_item"
                    else s
                )
                for s in draft.steps
            ],
            reason=draft.reason,
        )

    return select


# ── 1. 정해진 형식과 정상 경로 ──────────────────────────────
section("1. 작업 목록과 수행 결과의 형식, 정상 경로의 Worker LLM 호출 수")
agent, _ = wired()
r, st, ev = run(agent, REPRESENTATIVE)
tasks = messages(ev, "task_list")
reports = messages(ev, "work_report")
verifs = messages(ev, "verification")
task_ok = bool(tasks) and all(
    {"id", "tool", "args", "fixed", "purpose"} <= set(t)
    for m in tasks
    for t in m["body"]["tasks"]
)
report_ok = bool(reports) and all(
    {"round", "tasks", "requests"} <= set(m["body"])
    and all(
        {"step", "tool", "status", "attempts", "code"} <= set(t)
        for t in m["body"]["tasks"]
    )
    for m in reports
)
check(
    "Planner 작업 목록(task_list)이 정해진 형식으로 Worker에게 간다",
    task_ok
    and all((m["sender"], m["receiver"]) == ("planner", "worker") for m in tasks),
    json.dumps(tasks[0]["body"]["tasks"][1], ensure_ascii=False) if tasks else "",
)
check(
    "Worker 수행 결과(work_report)가 정해진 형식으로 Planner에게 간다",
    report_ok
    and all((m["sender"], m["receiver"]) == ("worker", "planner") for m in reports),
    json.dumps(reports[0]["body"], ensure_ascii=False)[:300] if reports else "",
)
check(
    "로깅 Agent의 수행 결과 검증(verification)이 Planner에게 간다",
    bool(verifs)
    and verifs[0]["sender"] == "logging"
    and verifs[0]["body"]["verdict"] == "normal",
    (
        json.dumps(
            [(c["check"], c["status"]) for c in verifs[0]["body"]["checks"]],
            ensure_ascii=False,
        )
        if verifs
        else ""
    ),
)
check(
    "정상 경로에서 Worker의 LLM 호출이 0회",
    r["kind"] == "result" and not worker_llm(ev) and not worker_rule(ev),
    f"추천 {[x['item'] for x in r['recommendations']]}, 재계획 {st['replan_count']}회",
)
fixed = {t["tool"]: t["fixed"] for t in tasks[0]["body"]["tasks"]} if tasks else {}
check(
    "Planner가 사용자 조건으로 채운 인자를 고정 인자로 표시한다",
    "base_month" in fixed.get("get_material_price_index", [])
    and "item_code" in fixed.get("get_material_price_index", [])
    and "keyword" not in fixed.get("search_material_item", ["keyword"]),
    json.dumps(fixed, ensure_ascii=False),
)
summary["normal"] = {"run_id": r["run_id"], "tasks": tasks, "reports": reports}

# ── 2. 치명적 실패, 필수 인자 누락 ─────────────────────────────
section("2. 치명적 실패와 필수 인자 누락에서는 Worker가 LLM을 호출하지 않는다")
with mock.patch.dict(os.environ, {"ECOS_OPEN_API": ""}):
    agent, _ = wired()
    r, st, ev = run(agent, REPRESENTATIVE)
rule = worker_rule(ev)
check(
    "API 키 누락(INVALID_KEY): Worker LLM 0회, 규칙 판단만 남는다",
    not worker_llm(ev)
    and any(d["code"] == "INVALID_KEY" for d in rule)
    and r["fatal"] == ["INVALID_KEY"],
    "; ".join(f"{d['step']} {d['code']}: {d['reason']}" for d in rule),
)

decider_calls = []


def counting(decide):
    def wrap(*a):
        decider_calls.append(a[0]["id"])
        return decide(*a)

    return wrap


worker = Worker(call_tool, decider=counting(make_llm_decider()))
events: list[dict] = []
plan = {
    "steps": [
        {
            "id": "s1",
            "tool": "search_law_article",
            "args": {"law_name": "상생협력법", "keyword": ""},
            "fixed": ["law_name"],
            "role": "law",
            "round": 0,
            "purpose": "필수 인자 누락 재현",
        }
    ],
    "rounds": [{"round": 0}],
}
_, _, calls, report = worker.run(
    plan, {}, {}, [], lambda t, **d: events.append({"type": t, "data": d})
)
check(
    "필수 인자 누락(BAD_ARGUMENT, keyword 빈 값): Worker LLM 0회",
    not decider_calls and calls[0]["code"] == "BAD_ARGUMENT",
    next((e["data"]["reason"] for e in events if e["type"] == "decision"), ""),
)

# ── 3. 결과 없음 → Worker LLM이 인자를 고쳐 같은 Tool을 다시 부른다 ──────
section("3. 결과 없음(NO_DATA)에서 Worker가 LLM 판단으로 인자를 고쳐 다시 부른다")
agent, _ = wired(selector=name_keyword_selector())
r, st, ev = run(agent, PP)
llm = worker_llm(ev)
retries = of(ev, "tool_request", tool="search_material_item")
first, again = (
    (retries[0]["data"]["args"], retries[1]["data"]["args"])
    if len(retries) > 1
    else ({}, {})
)
check(
    "Worker LLM이 retry를 고르고 같은 Tool(search_material_item)을 다른 검색어로 다시 부른다",
    bool(llm)
    and llm[0]["choice"] == "retry"
    and len(retries) >= 2
    and retries[1]["data"].get("attempt") == 1,
    f"{first.get('keyword')} → {again.get('keyword')}; 판단 이유: {llm[0]['reason'] if llm else '-'}",
)
check(
    "고정 인자는 바뀌지 않고 고칠 수 있는 인자만 바뀐다",
    bool(again)
    and {k for k in again if again[k] != first.get(k)}
    <= {"keyword", "as_of", "max_results"},
    f"첫 호출 {first} / 재시도 {again}",
)
check(
    "다시 확보한 품목으로 판단까지 이어진다 (재계획 없이)",
    st["replan_count"] == 0 and bool(r["criteria"] and r["criteria"][0]["index"]),
    f"{r['criteria'][0]['index']['label'] if r['criteria'] and r['criteria'][0]['index'] else '-'}, 재계획 {st['replan_count']}회",
)
summary["retry"] = {"run_id": r["run_id"], "decisions": llm, "text": r["text"]}

# ── 4. Worker의 계획 변경 요청 → Planner 재계획 ─────────────────
section("4. Worker가 올린 계획 변경 요청이 Planner의 재계획에 반영된다")
with mock.patch.dict(os.environ, {"WORKER_LLM": "off"}):
    agent, _ = wired(selector=keyword_selector("PP"))
    r, st, ev = run(agent, PP)
reqs = [x for m in messages(ev, "work_report") for x in m["body"]["requests"]]
rounds = [x["strategy"] for x in st["plan"]["rounds"]]
check(
    "Worker가 해결하지 못한 실패를 사유와 함께 Planner에 보고한다",
    bool(reqs) and reqs[0]["step"] == "s1" and reqs[0]["reason"],
    json.dumps(reqs[0], ensure_ascii=False) if reqs else "",
)
check(
    "Planner가 요청을 반영해 작업 목록을 다시 구성한다 (worker_request)",
    "worker_request" in rounds,
    f"재계획 {rounds}, 다시 찾은 검색어 {[e['data']['args']['keyword'] for e in of(ev, 'tool_request', tool='search_material_item')]}",
)
check(
    "작업으로 해결하지 못한 요청은 사용자에게 되묻는다",
    bool(r.get("planner_notes")),
    (r.get("planner_notes") or ["-"])[0],
)
summary["request"] = {
    "run_id": r["run_id"],
    "rounds": rounds,
    "notes": r.get("planner_notes"),
}

# ── 5. 로깅 Agent가 이상으로 검증 → Planner가 작업 목록을 다시 구성 ──────
section("5. 로깅 Agent가 이상으로 판단하면 Planner가 작업 목록을 다시 구성한다")


def faulty(task, args, error, adjustable, tried):
    """고정 인자인 약정서 지표 이름을 바꾸라고 하는 잘못된 판단을 만든다."""
    return WorkerDecision(
        action="retry",
        args=[ArgChange(name="keyword", value="합성수지")],
        reason="재현용 잘못된 판단",
    )


agent, _ = wired(worker=Worker(call_tool, decider=faulty, guard_fixed=False))
r, st, ev = run(agent, PP_INDEX)
verifs = messages(ev, "verification")
bad = next((v for v in verifs if v["body"]["verdict"] == "anomaly"), None)
rounds = [x["strategy"] for x in st["plan"]["rounds"]]
check(
    "로깅 Agent가 Worker 수행 직후 고정 인자 변경을 이상으로 검증한다",
    bad is not None
    and any(
        c["check"] == "fixed_arg_changed" and c["status"] == "warn"
        for c in bad["body"]["checks"]
    ),
    (
        next((c["message"] for c in bad["body"]["checks"] if c["status"] == "warn"), "")
        if bad
        else ""
    ),
)
check(
    "Planner가 지적된 작업의 결과를 버리고 정한 인자로 다시 수행한다 (verify_fix)",
    "verify_fix" in rounds and any(s.get("discarded") for s in st["plan"]["steps"]),
    f"재계획 {rounds}, 버린 작업 {[s['id'] for s in st['plan']['steps'] if s.get('discarded')]}",
)
review = r.get("review") or {}
check(
    "요청 종료 판정(F3020)에도 고정 인자 변경이 남고 진단이 붙는다",
    review.get("verdict") == "anomaly"
    and (store.get_review(r["run_id"]) or {}).get("diagnosis") is not None,
    json.dumps(
        (store.get_review(r["run_id"]) or {}).get("diagnosis"), ensure_ascii=False
    ),
)
summary["verify"] = {"run_id": r["run_id"], "rounds": rounds, "verification": bad}

# ── 6. 실행 로그에 남는 메시지 ─────────────────────────────
section("6. Planner, Worker, 로깅 Agent 사이의 메시지가 실행 로그에 남는다")
kinds = {
    (e["data"]["sender"], e["data"]["receiver"], e["data"]["kind"])
    for e in of(ev, "agent_message")
}
check(
    "저장된 실행 로그에 task_list, work_report, verification 메시지가 모두 있다",
    {
        ("planner", "worker", "task_list"),
        ("worker", "planner", "work_report"),
        ("logging", "planner", "verification"),
    }
    <= kinds,
    ", ".join(sorted(f"{a}→{b} {k}" for a, b, k in kinds)),
)

# ── 결과 ─────────────────────────────────────────────
section("결과")
passed = sum(1 for s, _ in results if s == PASS)
print(f"{passed}/{len(results)} PASS")
(OUT / "worker_check.json").write_text(
    json.dumps({"results": results, **summary}, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
sys.exit(0 if passed == len(results) else 1)
