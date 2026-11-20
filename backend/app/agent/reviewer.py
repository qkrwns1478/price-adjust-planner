"""로깅 Agent의 실행 평가 (F3020)

판정: 요청 1건(run)의 저장된 실행 로그를 규칙으로 대조하고 횟수를 센다. LLM을 쓰지 않는다.
진단: 경고가 하나라도 나온 실행에만 LLM이 실행 기록을 읽고 원인을 세 문장 이내로 적는다.
     진단은 판정과 따로 저장하며 경고 수에 섞지 않는다.

검사 결과 status
    ok            근거 이벤트가 있고 이상이 없다
    warn          이상 징후가 있다 (items에 근거 이벤트의 seq)
    undetermined  근거가 되는 이벤트가 없거나 서로 맞지 않아 판정할 수 없다

실행 판정 verdict
    anomaly       warn이 하나라도 있다
    undetermined  warn은 없고 undetermined가 하나라도 있다
    normal        모든 검사가 ok
"""

from __future__ import annotations

import json
import re
from typing import Callable, Literal

from pydantic import BaseModel, Field

from .config import max_replan

CHECKS = {
    "repeated_call": "같은 Tool, 같은 입력의 반복 호출",
    "excessive_replan": "재계획 과도 반복",
    "unplanned_call": "계획에 없던 Tool 호출",
    "fixed_arg_changed": "Planner가 고정한 인자 변경",
}
# Worker가 작업을 마칠 때마다 하는 수행 결과 검증 (F3021). 재계획 횟수는 요청이 끝나야 셀 수 있어 빼고 본다
ROUND_CHECKS = ("repeated_call", "unplanned_call", "fixed_arg_changed")

VERDICT_LABEL = {
    "normal": "이상 없음",
    "anomaly": "이상",
    "undetermined": "판정 불가",
    "failed": "실패",
}


# ── 판정 (규칙) ──────────────────────────────────────────


def _of(events: list[dict], type_: str, node: str | None = None) -> list[dict]:
    return [
        e for e in events if e["type"] == type_ and (node is None or e["node"] == node)
    ]


def _result(check: str, status: str, message: str, items: list | None = None) -> dict:
    return {
        "check": check,
        "label": CHECKS[check],
        "status": status,
        "message": message,
        "items": items or [],
    }


def _complete(events: list[dict]) -> str | None:
    """실행 로그가 처음부터 끝까지 빠짐없이 남았는지 본다. 문제가 있으면 사유를 반환한다."""
    if not events:
        return "실행 로그가 없습니다."
    seqs = [e["seq"] for e in events]
    if events[0]["type"] != "run_start":
        return "요청 시작 이벤트가 없습니다."
    if events[-1]["type"] != "run_end":
        return "요청 종료 이벤트가 없습니다."
    if seqs != list(range(1, len(events) + 1)):
        return "이벤트 순서에 빠진 번호가 있습니다."
    return None


def check_repeated_call(events: list[dict]) -> dict:
    requests = _of(events, "tool_request")
    if len(_of(events, "node_start", "execute")) != len(
        _of(events, "node_end", "execute")
    ):
        return _result(
            "repeated_call", "undetermined", "실행 단계의 종료 이벤트가 없습니다."
        )
    expected = sum(
        e["data"].get("called", 0) for e in _of(events, "node_end", "execute")
    )
    if expected != len(requests):
        return _result(
            "repeated_call",
            "undetermined",
            f"실행 단계가 기록한 호출 수({expected})와 Tool 호출 이벤트 수({len(requests)})가 다릅니다.",
        )
    groups: dict[str, list[dict]] = {}
    for e in requests:
        key = json.dumps(
            [e["data"]["tool"], e["data"].get("args", {})],
            ensure_ascii=False,
            sort_keys=True,
        )
        groups.setdefault(key, []).append(e)
    items = [
        {
            "tool": g[0]["data"]["tool"],
            "args": g[0]["data"].get("args", {}),
            "count": len(g),
            "seq": [e["seq"] for e in g],
        }
        for g in groups.values()
        if len(g) > 1
    ]
    if items:
        text = ", ".join(f"{i['tool']} {i['count']}회" for i in items)
        return _result(
            "repeated_call", "warn", f"같은 입력으로 다시 호출했습니다: {text}", items
        )
    return _result(
        "repeated_call", "ok", f"Tool 호출 {len(requests)}건, 같은 입력의 반복 없음"
    )


def check_excessive_replan(events: list[dict], limit: int | None = None) -> dict:
    end = events[-1]["data"]
    limit = end.get("replan_limit") or limit or max_replan()
    replans = [e for e in _of(events, "decision") if e["data"].get("kind") == "replan"]
    if _of(events, "node_start", "plan") and not _of(events, "node_end", "judge"):
        return _result(
            "excessive_replan", "undetermined", "판단 단계의 종료 이벤트가 없습니다."
        )
    if end.get("replan_count") is None or end["replan_count"] != len(replans):
        return _result(
            "excessive_replan",
            "undetermined",
            f"응답의 재계획 횟수({end.get('replan_count')})와 재계획 판단 이벤트 수({len(replans)})가 다릅니다.",
        )
    items = []
    if len(replans) >= limit:
        items.append(
            {
                "reason": "limit",
                "count": len(replans),
                "limit": limit,
                "seq": [e["seq"] for e in replans],
            }
        )
    strategies = [e["data"].get("choice") for e in replans]
    for prev, cur in zip(replans, replans[1:]):
        if prev["data"].get("choice") == cur["data"].get("choice"):
            items.append(
                {
                    "reason": "same_strategy",
                    "strategy": cur["data"].get("choice"),
                    "seq": [prev["seq"], cur["seq"]],
                }
            )
    if items:
        parts = []
        if any(i["reason"] == "limit" for i in items):
            parts.append(f"재계획 {len(replans)}회로 상한 {limit}회에 도달했습니다")
        parts += [
            f"같은 방법({i['strategy']})을 연달아 다시 썼습니다"
            for i in items
            if i["reason"] == "same_strategy"
        ]
        return _result("excessive_replan", "warn", ", ".join(parts) + ".", items)
    return _result(
        "excessive_replan",
        "ok",
        f"재계획 {len(replans)}회 (상한 {limit}회)"
        + (f": {' → '.join(strategies)}" if strategies else ""),
    )


def check_unplanned_call(events: list[dict]) -> dict:
    requests = _of(events, "tool_request")
    plans = _of(events, "node_end", "plan")
    if not requests:
        return _result("unplanned_call", "ok", "Tool 호출 없음")
    if not plans or any("steps" not in e["data"] for e in plans):
        return _result(
            "unplanned_call",
            "undetermined",
            "Tool을 호출했지만 계획 이벤트가 없습니다.",
        )
    planned = {(s["id"], s["tool"]) for e in plans for s in e["data"]["steps"]}
    items = [
        {"step": e["data"]["step"], "tool": e["data"]["tool"], "seq": [e["seq"]]}
        for e in requests
        if (e["data"]["step"], e["data"]["tool"]) not in planned
    ]
    if items:
        text = ", ".join(f"{i['step']} {i['tool']}" for i in items)
        return _result("unplanned_call", "warn", f"계획에 없던 호출: {text}", items)
    return _result(
        "unplanned_call", "ok", f"Tool 호출 {len(requests)}건이 모두 계획의 단계와 일치"
    )


def check_fixed_arg_changed(events: list[dict]) -> dict:
    """Worker가 Planner의 작업 목록(task_list)에서 고정한 인자를 바꿔 불렀는지 본다 (F3021).

    args에 값이 있는 고정 인자는 작업 목록의 값과, 앞 단계 결과로 채운 고정 인자(args_from)는
    그 작업의 첫 호출 값과 비교한다. 작업 목록에 없는 단계는 계획 외 호출 검사가 맡는다.
    """
    requests = _of(events, "tool_request")
    if not requests:
        return _result("fixed_arg_changed", "ok", "Tool 호출 없음")
    tasks = {
        t["id"]: t
        for e in _of(events, "agent_message")
        if e["data"].get("kind") == "task_list"
        for t in e["data"].get("body", {}).get("tasks", [])
    }
    if not tasks:
        return _result(
            "fixed_arg_changed",
            "undetermined",
            "Tool을 호출했지만 고정 인자를 담은 작업 목록 메시지가 없습니다.",
        )
    first: dict[str, dict] = {}
    items = []
    for e in requests:
        d = e["data"]
        task = tasks.get(d["step"])
        if task is None:
            continue
        args = d.get("args", {})
        base = first.setdefault(d["step"], args)
        changed = {}
        for k in task.get("fixed", []):
            planned = task["args"].get(k, base.get(k))
            if args.get(k) != planned:
                changed[k] = {"planned": planned, "called": args.get(k)}
        if changed:
            items.append(
                {"step": d["step"], "tool": d["tool"], "changed": changed, "seq": [e["seq"]]}
            )
    if items:
        text = ", ".join(f"{i['step']} {', '.join(i['changed'])}" for i in items)
        return _result(
            "fixed_arg_changed", "warn", f"고정 인자를 바꿔 호출했습니다: {text}", items
        )
    return _result(
        "fixed_arg_changed", "ok", f"Tool 호출 {len(requests)}건이 고정 인자를 지킴"
    )


def _verdict(checks: list[dict]) -> dict:
    statuses = {c["status"] for c in checks}
    verdict = (
        "anomaly"
        if "warn" in statuses
        else "undetermined" if "undetermined" in statuses else "normal"
    )
    return {
        "verdict": verdict,
        "warnings": sum(len(c["items"]) for c in checks if c["status"] == "warn"),
        "checks": checks,
    }


def verify_round(events: list[dict]) -> dict:
    """요청 진행 중, Worker가 작업을 마친 시점까지의 실행 로그로 수행 결과를 검증한다 (F3021). LLM을 쓰지 않는다."""
    events = sorted(events, key=lambda e: e["seq"])
    if not events or events[0]["type"] != "run_start":
        checks = [
            _result(c, "undetermined", "요청 시작 이벤트가 없습니다.")
            for c in ROUND_CHECKS
        ]
    else:
        checks = [
            check_repeated_call(events),
            check_unplanned_call(events),
            check_fixed_arg_changed(events),
        ]
    rounds = [e["data"].get("round") for e in _of(events, "node_end", "plan")]
    return _verdict(checks) | {"round": rounds[-1] if rounds else 0}


def evaluate_run(events: list[dict], limit: int | None = None) -> dict:
    """요청 1건의 실행 로그를 판정한다. LLM을 호출하지 않는다."""
    events = sorted(events, key=lambda e: e["seq"])
    problem = _complete(events)
    if problem:
        checks = [_result(c, "undetermined", problem) for c in CHECKS]
    else:
        checks = [
            check_repeated_call(events),
            check_excessive_replan(events, limit),
            check_unplanned_call(events),
            check_fixed_arg_changed(events),
        ]
    statuses = {c["status"] for c in checks}
    verdict = (
        "anomaly"
        if "warn" in statuses
        else "undetermined" if "undetermined" in statuses else "normal"
    )
    # 검사 항목은 Tool 호출 이력만 보므로, 요청 자체가 실패한 실행을 "정상"으로 내지 않는다
    if verdict == "normal" and events[-1]["data"].get("ok") is False:
        verdict = "failed"
    return {
        "verdict": verdict,
        "warnings": sum(len(c["items"]) for c in checks if c["status"] == "warn"),
        "checks": checks,
    }


# ── 진단 (LLM, 이상으로 판정한 실행에만) ─────────────────────

Diagnoser = Callable[[dict, list[dict]], dict]


class Diagnosis(BaseModel):
    agent: Literal["main", "planner", "worker", "logging", "tool"] = Field(
        description=(
            "문제가 있는 쪽. planner: 계획 수립, 재계획 / worker: 작업 수행, 재시도 / "
            "main: 그 밖의 메인 Agent 단계(분석, 판단, 응답) / logging: 로깅 Agent(기록 누락, 판정 규칙) / tool: 외부 API Tool"
        )
    )
    stage: Literal[
        "analyze", "plan", "execute", "judge", "respond", "tool", "logging"
    ] = Field(description="문제가 생긴 단계")
    text: str = Field(
        description="원인과 고칠 곳을 세 문장 이내로. 실행 기록에 있는 사실만 근거로 쓴다"
    )


DIAGNOSE_PROMPT = """너는 납품대금 조정 상담 서비스의 로깅 Agent 다.
메인 Agent는 analyze → plan(Planner) → execute(Worker) → judge → respond 순서로 실행하고, judge에서 조건을 충족하지 못하면 plan으로 돌아가 재계획한다.
Planner는 작업 목록(agent_message task_list)에 사용자 조건으로 정한 고정 인자(fixed)를 표시한다.
Worker는 같은 Tool, 같은 인자의 결과를 캐시에서 다시 쓰며(tool_reused), 작업 목록의 단계(step id)대로만 Tool을 부른다.
Worker는 인자를 고치면 결과가 달라질 수 있는 실패에서만 LLM 판단(decision kind=worker)으로 고정되지 않은 인자를 고쳐 같은 단계를 다시 부를 수 있다(attempt).
제약을 충족하지 못했을 때의 재계획은 next_candidate(다른 후보 지표) → recheck(새 Tool 호출 없이 이미 받은 추세로 도달 시점 추정, 단계가 비어 있는 것이 정상) → alt_path(다른 협의 경로 조문 검색) 순서로 하나씩 쓰며, 이 순서대로 진행한 것은 정상이다.
verify_fix(로깅 Agent 검증에서 지적된 작업을 정한 인자로 다시 수행)와 worker_request(Worker가 해결하지 못한 실패 반영)는 그 순서와 별개로, 해당 상황이 생긴 라운드에서 먼저 쓴다.
재계획 상한 도달은 사용자의 약정 조건이 공개 지표로 충족되지 않을 때도 일어나므로, 그런 경우는 조건 조정을 되묻는 것이 맞는지도 함께 본다.

아래 규칙 판정에서 경고가 나왔다. 실행 기록을 읽고 어느 쪽의 문제인지, 무엇을 고치면 되는지 세 문장 이내로 진단한다.
판정을 다시 하지 말고, 기록에 없는 사실을 지어내지 않는다.

규칙 판정
{checks}

실행 기록 (seq, type, node, data)
{events}"""


def _brief(e: dict) -> str:
    data = dict(e["data"])
    if e["type"] == "tool_response":
        data.pop("result", None)
    text = json.dumps(data, ensure_ascii=False)
    return f"{e['seq']} {e['type']} {e['node'] or '-'} {text[:300]}"


def limit_sentences(text: str, n: int = 3) -> str:
    """문장 부호 뒤 공백으로 나눈다. 8.99% 같은 소수점은 나누지 않는다."""
    sentences = [s for s in re.split(r"(?<=[.!?。])\s+", text.strip()) if s]
    return " ".join(sentences[:n])


def make_llm_diagnoser() -> Diagnoser:
    from .config import chat_model

    structured = chat_model().with_structured_output(
        Diagnosis, method="function_calling"
    )

    def diagnose(review: dict, events: list[dict]) -> dict:
        warned = [c for c in review["checks"] if c["status"] == "warn"]
        prompt = DIAGNOSE_PROMPT.format(
            checks=json.dumps(warned, ensure_ascii=False),
            events="\n".join(_brief(e) for e in events),
        )
        result: Diagnosis = structured.invoke(
            [("system", prompt), ("human", "진단해 줘.")]
        )
        return {"agent": result.agent, "stage": result.stage, "text": result.text}

    return diagnose
