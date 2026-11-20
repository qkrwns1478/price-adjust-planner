"""메인 Agent 실행 흐름 (F3005 ~ F3011, F3021 Planner·Worker, LangGraph)

    analyze ─┬─(Tool 필요)→ plan → execute → judge ─┬─(재계획)→ plan
             └─(인사, 사용법, 되물음)────────────────┴─────────→ respond → END

    plan     Planner: 작업 목록을 만들고 고정 인자를 표시한다. 재계획에서 Worker 요청과 로깅 Agent 검증을 반영한다.
    execute  Worker: 작업 목록대로 Tool을 부르고, 고칠 여지가 있는 실패에서만 LLM으로 재시도를 판단한다.
    judge    제약 판단 뒤 Planner가 로깅 Agent의 수행 결과 검증을 읽고 재계획 여부를 정한다.
    세 Agent 사이의 메시지(작업 목록, 수행 보고, 검증)는 agent_message 이벤트로 남는다.

대화 맥락은 체크포인터에 세션 id(thread_id) 단위로 저장된다 (F3010).
단계 진입과 종료, Tool 호출, 실행 흐름을 바꾸는 판단은 EventBus로 발행한다 (F3012).
"""

from __future__ import annotations

import time
import uuid
from functools import partial
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from ..events import (
    Emitter,
    EventBus,
    EventType,
    bind_emitter,
    current_emitter,
    unbind_emitter,
)
from ..tools import call_tool
from ..tools.base import ErrorCode
from .analyzer import Analyzer, find_missing, make_llm_analyzer
from .config import HISTORY_TURNS, max_replan
from .executor import ToolCaller, by_tool
from .judge import judge
from .planner import (
    Selector,
    apply_replan,
    build_plan,
    choose_strategy,
    make_llm_selector,
    needs_tools,
    request_actions,
    suspect_steps,
)
from .responder import Chatter, build_response
from .state import AgentState, merge_conditions
from .worker import Decider, Worker

Verifier = Callable[[str, str], "dict | None"]  # (session_id, run_id) → 수행 결과 검증

_TURN_RESET: dict[str, Any] = {
    "intent": None,
    "intent_reason": None,
    "changed": [],
    "missing": [],
    "error": None,
    "plan": {},
    "results": {},
    "step_results": {},
    "calls": [],
    "cache": {},
    "judgments": [],
    "replan_count": 0,
    "replan": None,
    "stop_reason": None,
    "worker_requests": [],
    "handled_requests": [],
    "verified_steps": [],
    "planner_notes": [],
    "response": {},
}


# ── 이벤트 (F3012) ──────────────────────────────────────


def _message(node: str, sender: str, receiver: str, kind: str, body: Any) -> None:
    """Planner, Worker, 로깅 Agent 사이의 메시지를 발행한다 (F3021)."""
    current_emitter().emit(
        EventType.AGENT_MESSAGE,
        node,
        sender=sender,
        receiver=receiver,
        kind=kind,
        body=body,
    )


def _task_list(plan: dict) -> dict:
    """이번 라운드에 Planner가 Worker에게 넘기는 작업 목록을 만든다."""
    last = plan["rounds"][-1]
    steps = {s["id"]: s for s in plan["steps"]}
    keys = ("id", "tool", "args", "args_from", "fixed", "purpose", "no_retry")
    return {
        "round": last["round"],
        "strategy": last["strategy"],
        "tasks": [
            {k: steps[i][k] for k in keys if k in steps[i]} for i in last["steps"]
        ],
        "discarded": [s["id"] for s in plan["steps"] if s.get("discarded")],
    }


def _decision(by: str, kind: str, choice: Any, reason: str | None) -> None:
    """실행 흐름을 바꾸는 판단과 그 이유를 발행한다. by = llm | rule"""
    current_emitter().emit(
        EventType.DECISION, None, by=by, kind=kind, choice=choice, reason=reason
    )


def _summary(node: str, state: AgentState, out: dict) -> dict:
    """단계 종료 이벤트에 싣는 결과 요약을 만든다."""
    if node == "analyze":
        return {
            "intent": out.get("intent"),
            "intent_reason": out.get("intent_reason"),
            "changed": out.get("changed", []),
            "missing": [m["field"] for m in out.get("missing", [])],
            "error": out.get("error"),
        }
    if node == "plan":
        plan = out["plan"]
        last = plan["rounds"][-1]
        steps = {s["id"]: s for s in plan["steps"]}
        return {
            "round": last["round"],
            "strategy": last["strategy"],
            "source": plan.get("source"),
            "steps": [
                {"id": i, "tool": steps[i]["tool"], "purpose": steps[i].get("purpose")}
                for i in last["steps"]
            ],
            "reused": last.get("reused", []),
            "corrections": plan.get("corrections", []),
            "skipped": plan.get("skipped", []),
        }
    if node == "execute":
        new = out["calls"][len(state.get("calls", [])) :]
        return {
            "called": sum(1 for c in new if not c["cached"] and c["code"] != "SKIPPED"),
            "retried": sum(1 for c in new if c.get("attempt")),
            "reused": sum(1 for c in new if c["cached"]),
            "skipped": sum(1 for c in new if c["code"] == "SKIPPED"),
            "failed": [
                {"step": c["step"], "tool": c["tool"], "code": c["code"]}
                for c in new
                if not c["ok"]
            ],
        }
    if node == "judge":
        return {
            "judgments": [
                {"item": j.get("item_name"), "status": j.get("status")}
                for j in out.get("judgments", [])
            ],
            "replan": (out.get("replan") or {}).get("strategy"),
            "stop_reason": out.get("stop_reason"),
        }
    if node == "respond":
        return {"kind": out["response"].get("kind")}
    return {}


def _traced(
    node: str, fn: Callable[[AgentState], dict]
) -> Callable[[AgentState], dict]:
    """노드 진입과 종료를 발행한다. 노드가 예외를 던지면 실패로 발행한 뒤 다시 던진다."""

    def run(state: AgentState) -> dict:
        emitter = current_emitter()
        emitter.emit(EventType.NODE_START, node)
        started = time.perf_counter()
        try:
            out = fn(state)
        except Exception as e:
            emitter.emit(
                EventType.NODE_END,
                node,
                ok=False,
                error=type(e).__name__,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            raise
        elapsed = round((time.perf_counter() - started) * 1000)
        try:
            summary = _summary(node, state, out)
        except Exception:  # noqa: BLE001 (요약 실패가 실행을 막지 않는다)
            summary = {}
        summary_ok = not summary.get("error")
        emitter.emit(
            EventType.NODE_END,
            node,
            **{**summary, "ok": summary_ok, "elapsed_ms": elapsed},
        )
        return out

    return run


def build_graph(
    analyzer: Analyzer | None = None,
    chatter: Chatter | None = None,
    tool_caller: ToolCaller = call_tool,
    checkpointer: Any = None,
    selector: Selector | None = None,
    worker: Worker | None = None,
    verifier: Verifier | None = None,
):
    holder: dict[str, Any] = {"analyzer": analyzer, "selector": selector}
    worker = worker or Worker(tool_caller)

    def read_verification(state: AgentState) -> dict | None:
        """로깅 Agent가 이번 라운드의 수행 결과를 검증한 결과를 반환한다. 결과가 없거나 이전 라운드의 것이면 None을 반환한다."""
        if verifier is None:
            return None
        emitter = current_emitter()
        try:
            v = verifier(emitter.session_id, emitter.run_id)
        except Exception:  # noqa: BLE001 (검증을 읽지 못해도 실행은 이어진다)
            return None
        if not v or v.get("round") != state["plan"]["rounds"][-1]["round"]:
            return None
        _message(
            "judge",
            "logging",
            "planner",
            "verification",
            {
                "round": v["round"],
                "verdict": v["verdict"],
                "warnings": v["warnings"],
                "checks": [
                    {k: c[k] for k in ("check", "status", "message", "items")}
                    for c in v["checks"]
                ],
            },
        )
        return v

    def get_selector() -> Selector:
        if holder["selector"] is None:
            holder["selector"] = make_llm_selector()
        return holder["selector"]

    def get_analyzer() -> Analyzer:
        if holder["analyzer"] is None:
            holder["analyzer"] = make_llm_analyzer()
        return holder["analyzer"]

    # ── 노드 ──

    def analyze(state: AgentState) -> dict:
        """F3005 요청을 분석하고 F3010 직전 조건을 병합한다."""
        out = dict(_TURN_RESET)
        previous = state.get("conditions") or {}
        try:
            result = get_analyzer()(
                state["user_message"], previous, state.get("history", [])
            )
        except (
            Exception
        ):  # noqa: BLE001 (키, 주소가 섞일 수 있어 원문을 포함하지 않는다, NF3002)
            _decision(
                "rule",
                "intent",
                "chat",
                "요청 분석 LLM이 응답하지 않아 안내 응답으로 바꿨습니다.",
            )
            return out | {
                "intent": "chat",
                "error": "ANALYZE_FAILED",
                "conditions": previous,
            }
        conditions, changed = merge_conditions(previous, result)
        _decision("llm", "intent", result.intent, result.intent_reason)
        missing = find_missing(result.intent, conditions)
        if missing and needs_tools(result.intent):
            _decision(
                "rule",
                "clarify",
                [m["field"] for m in missing],
                "계산의 기준이 되는 조건이 비어 있어 Tool을 호출하기 전에 되묻습니다.",
            )
        return out | {
            "intent": result.intent,
            "intent_reason": result.intent_reason,
            "conditions": conditions,
            "changed": changed,
            "missing": missing,
        }

    def plan(state: AgentState) -> dict:
        """Planner: F3006 계획 수립 / F3009 재계획. 작업 목록을 Worker에게 메시지로 넘긴다 (F3021)."""
        out = plan_step(state)
        _message("plan", "planner", "worker", "task_list", _task_list(out["plan"]))
        return out

    def plan_step(state: AgentState) -> dict:
        if state.get("replan"):
            plan = apply_replan(state, state["replan"])
            out = {
                "plan": plan,
                "replan_count": state.get("replan_count", 0) + 1,
                "replan": None,
            }
            dropped = {s["id"] for s in plan["steps"] if s.get("discarded")}
            if dropped & set(state.get("step_results", {})):
                # 검증에서 지적된 작업의 결과는 판단에 쓰지 않는다
                kept = {
                    k: v for k, v in state["step_results"].items() if k not in dropped
                }
                out |= {"step_results": kept, "results": by_tool(kept)}
            return out
        try:
            draft = get_selector()(
                state["intent"], state["conditions"], state["user_message"]
            )
            plan = build_plan(
                state["intent"],
                state["conditions"],
                [s.model_dump() for s in draft.steps],
                draft.reason,
            )
            _decision("llm", "plan", [s.tool for s in draft.steps], draft.reason)
        except (
            Exception
        ):  # noqa: BLE001 (LLM이 실패해도 규칙 계획으로 진행한다, NF3003)
            plan = build_plan(state["intent"], state["conditions"])
            plan["corrections"] = ["LLM 계획을 받지 못해 규칙으로 계획했습니다."]
            _decision(
                "rule",
                "plan",
                [s["tool"] for s in plan["steps"]],
                plan["corrections"][0],
            )
        return {"plan": plan}

    def execute(state: AgentState) -> dict:
        """Worker: F3007 Tool 호출 및 결과 통합. 수행 보고를 Planner에게 메시지로 넘긴다 (F3021)."""
        step_results, cache, calls, report = worker.run(
            state["plan"],
            state.get("step_results", {}),
            state.get("cache", {}),
            state.get("calls", []),
            partial(current_emitter().emit, node="execute"),
        )
        _message("execute", "worker", "planner", "work_report", report)
        return {
            "step_results": step_results,
            "cache": cache,
            "calls": calls,
            "results": by_tool(step_results),
            "worker_requests": state.get("worker_requests", []) + report["requests"],
        }

    def judge_node(state: AgentState) -> dict:
        """F3008 제약을 판단하고, 충족하는 후보가 없으면 F3009 재계획 여부를 결정한다."""
        out = decide(state)
        if out.get("replan"):
            _decision(
                "rule", "replan", out["replan"]["strategy"], out["replan"]["reason"]
            )
        elif out.get("stop_reason"):
            _decision("rule", "stop", None, out["stop_reason"])
        return out

    def decide(state: AgentState) -> dict:
        judgments = judge(
            state["intent"], state["conditions"], state["plan"], state["step_results"]
        )
        out: dict[str, Any] = {"judgments": judgments, "replan": None}
        limit = max_replan()
        fatal = any(c["code"] in ErrorCode.FATAL for c in state.get("calls", []))
        under_limit = state.get("replan_count", 0) < limit

        # F3021: 로깅 Agent가 수행 결과를 이상으로 검증하면 Planner가 작업 목록을 다시 구성한다
        verification = read_verification(state)
        suspects = [
            s
            for s in suspect_steps(state["plan"], verification)
            if s not in state.get("verified_steps", [])
        ]
        if suspects and under_limit:
            return out | {
                "verified_steps": state.get("verified_steps", []) + suspects,
                "replan": {
                    "strategy": "verify_fix",
                    "reason": (
                        f"로깅 Agent가 {', '.join(suspects)} 작업에서 고정 인자 변경을 찾아, "
                        "그 결과를 버리고 Planner가 정한 인자로 다시 수행합니다."
                    ),
                    "steps": suspects,
                },
            }

        # F3021: Worker가 스스로 해결하지 못하고 올린 요청을 재계획에 반영한다
        actions, notes, done = request_actions(state)
        out |= {
            "handled_requests": state.get("handled_requests", []) + done,
            "planner_notes": state.get("planner_notes", []) + notes,
        }
        if actions and under_limit and not fatal:
            return out | {
                "replan": {
                    "strategy": "worker_request",
                    "reason": (
                        f"Worker가 {', '.join(done)} 작업의 실패를 해결하지 못해, "
                        "사용자가 말한 이름과 법정 기준 검색어로 작업을 다시 구성합니다."
                    ),
                    "actions": actions,
                }
            }

        if state["intent"] not in ("plan", "eligibility") or not judgments:
            return out
        if any(j["status"] == "satisfied" for j in judgments):
            return out
        if fatal:
            return out | {
                "stop_reason": "다시 호출해도 결과가 같은 오류라 재계획하지 않았습니다."
            }
        if not under_limit:
            return out | {
                "stop_reason": f"재계획 상한({limit}회)에 도달해 탐색을 멈췄습니다."
            }
        directive = choose_strategy({**state, "judgments": judgments})
        if directive is None:
            used = {r["strategy"] for r in state["plan"]["rounds"]}
            return out | {
                "stop_reason": (
                    "연동이 아닌 조정 신청 경로까지 확인해 탐색을 마쳤습니다."
                    if "alt_path" in used
                    else "더 탐색할 수 있는 대안이 없어 멈췄습니다."
                )
            }
        return out | {"replan": directive}

    def respond(state: AgentState) -> dict:
        """F3011 최종 응답을 만들고 F3010 대화 맥락을 갱신한다. Tool 원본 응답은 저장하지 않는다."""
        response = build_response(state, chatter)
        history = (
            state.get("history", [])
            + [
                {
                    "user": state["user_message"],
                    "intent": state.get("intent"),
                    "summary": response["summary"],
                }
            ]
        )[-HISTORY_TURNS:]
        return {
            "response": response,
            "history": history,
            "results": {},
            "step_results": {},
            "cache": {},
            "judgments": [],
        }

    # ── 연결 ──

    g = StateGraph(AgentState)
    g.add_node("analyze", _traced("analyze", analyze))
    g.add_node("plan", _traced("plan", plan))
    g.add_node("execute", _traced("execute", execute))
    g.add_node("judge", _traced("judge", judge_node))
    g.add_node("respond", _traced("respond", respond))

    g.add_edge(START, "analyze")
    g.add_conditional_edges(
        "analyze",
        lambda s: (
            "plan"
            if needs_tools(s["intent"]) and not s.get("missing") and not s.get("error")
            else "respond"
        ),
        {"plan": "plan", "respond": "respond"},
    )
    g.add_edge("plan", "execute")
    g.add_edge("execute", "judge")
    g.add_conditional_edges(
        "judge",
        lambda s: "plan" if s.get("replan") else "respond",
        {"plan": "plan", "respond": "respond"},
    )
    g.add_edge("respond", END)
    return g.compile(
        checkpointer=checkpointer if checkpointer is not None else MemorySaver()
    )


class PlannerAgent:
    """세션 단위로 대화를 이어 가는 메인 Agent. 계획 수립(Planner)과 작업 수행(Worker)으로 나뉜다 (F3021).

    실행 이벤트는 bus로 발행만 한다. 누가 구독하는지(로깅 Agent 등)는 알지 못한다 (F3012).
    review_source(session_id, run_id) 가 주어지면 요청을 마친 뒤 그 요청의 판정을 읽어 응답의 review에 싣는다 (F3020).
    판정을 누가 만드는지는 알지 못하며, 판정이 없으면 None이다.
    """

    def __init__(
        self,
        analyzer: Analyzer | None = None,
        chatter: Chatter | None = None,
        selector: Selector | None = None,
        tool_caller: ToolCaller = call_tool,
        checkpointer: Any = None,
        bus: EventBus | None = None,
        review_source: Callable[[str, str], dict | None] | None = None,
        worker_decider: Decider | None = None,
        worker: Worker | None = None,
        verifier: Verifier | None = None,
    ):
        self.bus = bus if bus is not None else EventBus()
        self.review_source = review_source
        # 수행 결과 검증을 읽는 함수 (F3021). 누가 검증하는지(로깅 Agent)는 알지 못하며, 없으면 검증 없이 진행한다
        self.verifier = verifier
        self.worker = worker or Worker(tool_caller, decider=worker_decider)
        self.graph = build_graph(
            analyzer,
            chatter,
            tool_caller,
            checkpointer,
            selector,
            self.worker,
            lambda s, r: self.verifier(s, r) if self.verifier else None,
        )

    @staticmethod
    def new_session() -> str:
        return uuid.uuid4().hex

    def _config(self, session_id: str) -> dict:
        # 재계획 상한 3회면 노드는 많아야 1 + 4 × 3 + 1 회 실행된다. 여유를 두고 제한한다 (NF3004)
        return {
            "configurable": {"thread_id": session_id},
            "recursion_limit": 25 + 4 * max_replan(),
        }

    def run(self, session_id: str, message: str) -> dict:
        emitter = Emitter(self.bus, session_id)
        token = bind_emitter(emitter)
        started = time.perf_counter()
        emitter.emit(EventType.RUN_START, message=message)
        try:
            final = self.graph.invoke(
                {"session_id": session_id, "user_message": message},
                self._config(session_id),
            )
        except Exception as e:
            emitter.emit(
                EventType.RUN_END,
                ok=False,
                error=type(e).__name__,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            raise
        finally:
            unbind_emitter(token)
        response = final["response"]
        emitter.emit(
            EventType.RUN_END,
            ok=not final.get("error"),
            error=final.get("error"),
            kind=response.get("kind"),
            intent=final.get("intent"),
            replan_count=final.get("replan_count", 0),
            stop_reason=final.get("stop_reason"),
            replan_limit=max_replan(),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
        ids = {"session_id": session_id, "run_id": emitter.run_id}
        drafts = [d | ids for d in response.get("plan_drafts", [])]
        return {
            **response,
            **ids,
            "plan_drafts": drafts,
            "review": self.review(session_id, emitter.run_id),
        }

    def review(self, session_id: str, run_id: str) -> dict | None:
        """요청 1건의 판정을 반환한다 (F3020). 판정이 없거나 읽지 못하면 None을 반환한다."""
        if self.review_source is None:
            return None
        try:
            return self.review_source(session_id, run_id)
        except Exception:  # noqa: BLE001 (판정을 읽지 못해도 응답은 그대로 돌려준다)
            return None

    def state(self, session_id: str) -> dict:
        return dict(self.graph.get_state(self._config(session_id)).values)
