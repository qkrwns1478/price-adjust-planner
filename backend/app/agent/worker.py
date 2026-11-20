"""Worker: Planner의 작업 목록을 Tool 호출로 수행한다 (F3021)

- 평소에는 LLM 없이 작업 목록대로 Tool을 부른다 (executor.prepare / invoke).
- Tool이 인자를 고치면 결과가 달라질 수 있는 실패(BAD_ARGUMENT, NOT_FOUND, NO_DATA)를 돌려주고,
  Planner가 고정하지 않은 인자가 남아 있을 때만 LLM이 재시도, 건너뜀, 계획 변경 요청 중 하나를 고른다.
- LLM에 묻지 않는 실패
    치명적 실패(INVALID_KEY, UPSTREAM_ERROR)     다시 불러도 결과가 같다 → Planner가 멈추고 안내한다
    필수 인자 누락(BAD_ARGUMENT)                 사용자에게 되물을 일이다
    인자와 무관한 실패(NO_INDEX_VALUE, SKIPPED)   Planner의 재계획(다른 후보)이 맡는다
    바꿀 인자가 없음, 재시도 상한 도달, LLM 판단 꺼짐
- 재시도는 같은 Tool로만, 상한(WORKER_MAX_RETRY) 안에서 한다. Planner가 고정한 인자(fixed, args_from)는
  LLM이 바꾸라고 해도 바꾸지 않는다.
- 스스로 해결하지 못한 실패는 사유와 함께 수행 보고(report.requests)로 Planner에 넘긴다.

수행 보고 형식 (Worker → Planner)
    {"round": 0,
     "tasks": [{"step", "tool", "status": ok|failed|skipped|dropped, "attempts", "code"}],
     "requests": [{"step", "tool", "code", "reason", "by": llm|rule, "tried": [인자, ...]}]}
"""

from __future__ import annotations

import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from ..tools import call_tool, get_tool
from ..tools.base import ErrorCode
from .config import worker_llm, worker_max_retry
from .executor import SKIPPED, Emit, ToolCaller, _no_emit, invoke, prepare, skip

# ── LLM 판단 ────────────────────────────────────────────


class ArgChange(BaseModel):
    name: str = Field(description="고칠 인자 이름. 바꿀 수 있는 인자 중 하나")
    value: str = Field(description="새 값")


class WorkerDecision(BaseModel):
    action: Literal["retry", "skip", "replan"] = Field(
        description=(
            "retry: 바꿀 수 있는 인자를 고쳐 같은 Tool을 다시 부른다 / "
            "skip: 이 작업 없이도 답할 수 있어 건너뛴다 / "
            "replan: 인자를 고쳐도 해결할 수 없어 Planner에게 계획 변경을 요청한다"
        )
    )
    args: list[ArgChange] = Field(
        default_factory=list,
        description="retry 일 때 고칠 인자. 다른 action이면 빈 목록",
    )
    reason: str = Field(description="그렇게 판단한 이유 한 문장")


# decide(task, args, error, adjustable, tried) -> WorkerDecision
Decider = Callable[[dict, dict, dict, list[str], list[dict]], WorkerDecision]

WORKER_PROMPT = """너는 납품대금 조정 상담 Agent의 Worker 다. Planner가 준 작업을 Tool로 수행하다가 실패했다.
실패 결과를 보고 retry, skip, replan 중 하나를 고른다.
- retry는 바꿀 수 있는 인자만 고쳐 같은 Tool을 다시 부른다. 이미 시도한 인자와 같은 값은 쓰지 않는다.
- 바꿀 수 있는 인자: {adjustable}. 나머지 인자는 Planner가 사용자 조건으로 고정했으므로 바꾸지 않는다.
- 원재료 품목 검색이 결과 없음이면, 한국은행 생산자물가지수의 품목명 체계(예: 열연강판, 동1차제품, 알루미늄1차제품)에 맞는
  더 일반적인 품목명이나 상위 분류명으로 고친다. 사용자가 말한 원재료와 다른 재료로 바꾸지 않는다.
- 법령 조문 검색이 결과 없음이면, 조문에 실제로 쓰일 법령 용어로 검색어를 줄이거나 바꾼다.
- 고칠 방법이 없으면 replan, 이 작업 없이도 답할 수 있으면 skip을 고른다.

Tool 명세
{spec}

작업: {task}
이번 인자: {args}
실패: {error}
이미 시도한 인자: {tried}"""


def make_llm_decider() -> Decider:
    from .config import chat_model

    structured = chat_model().with_structured_output(
        WorkerDecision, method="function_calling"
    )

    def decide(
        task: dict, args: dict, error: dict, adjustable: list[str], tried: list[dict]
    ) -> WorkerDecision:
        tool = get_tool(task["tool"])
        prompt = WORKER_PROMPT.format(
            adjustable=", ".join(adjustable),
            spec=json.dumps(tool.spec() if tool else {}, ensure_ascii=False),
            task=json.dumps(
                {"tool": task["tool"], "purpose": task.get("purpose")},
                ensure_ascii=False,
            ),
            args=json.dumps(args, ensure_ascii=False),
            error=json.dumps(error, ensure_ascii=False),
            tried=json.dumps(tried, ensure_ascii=False),
        )
        return structured.invoke([("system", prompt), ("human", "판단해 줘.")])

    return decide


# ── 규칙 ────────────────────────────────────────────────


def adjustable_args(step: dict) -> list[str]:
    """Planner가 고정하지 않아 Worker가 고칠 수 있는 인자를 반환한다."""
    tool = get_tool(step["tool"])
    props = tool.parameters.get("properties", {}) if tool else {}
    locked = set(step.get("fixed", [])) | set(step.get("args_from", {}))
    return [k for k in props if k not in locked]


def _missing_required(step: dict, args: dict) -> list[str]:
    tool = get_tool(step["tool"])
    required = tool.parameters.get("required", []) if tool else []
    return [k for k in required if args.get(k) in (None, "")]


def _coerce(step: dict, name: str, value: str) -> Any:
    tool = get_tool(step["tool"])
    kind = (tool.parameters.get("properties", {}).get(name, {}) if tool else {}).get(
        "type"
    )
    if kind == "integer":
        try:
            return int(value)
        except ValueError:
            return value
    return value


class Worker:
    """작업 목록 수행자. 이벤트는 emit으로만 발행하며 Planner, 로깅 Agent를 알지 못한다."""

    def __init__(
        self,
        tool_caller: ToolCaller = call_tool,
        decider: Decider | None = None,
        use_llm: bool | None = None,
        guard_fixed: bool = True,
    ):
        self.tool_caller = tool_caller
        self._decider = decider
        self._use_llm = use_llm
        # 고정 인자 보호. 끄는 것은 로깅 Agent 검증 경로를 확인할 때만 쓴다 (check_worker.py)
        self.guard_fixed = guard_fixed

    @property
    def use_llm(self) -> bool:
        return worker_llm() if self._use_llm is None else self._use_llm

    def decider(self) -> Decider:
        if self._decider is None:
            self._decider = make_llm_decider()
        return self._decider

    def _blocked(self, step: dict, args: dict, code: str, retries: int) -> str | None:
        """LLM에 묻지 않는 실패면 그 사유를 반환한다."""
        if code in ErrorCode.FATAL:
            return "다시 불러도 결과가 같은 치명적 실패라 LLM에 묻지 않습니다."
        if code not in ErrorCode.RETRYABLE_WITH_NEW_ARGS:
            return "인자를 고쳐도 달라지지 않는 실패라 LLM에 묻지 않습니다."
        if code == ErrorCode.BAD_ARGUMENT and _missing_required(step, args):
            return (
                "필수 인자가 비어 있어 사용자에게 되물을 일이라 LLM에 묻지 않습니다."
            )
        if step.get("no_retry"):
            return (
                "Planner가 다시 시도하지 말라고 표시한 작업이라 LLM에 묻지 않습니다."
            )
        if not adjustable_args(step):
            return "Planner가 인자를 모두 고정해 고칠 인자가 없으므로 LLM에 묻지 않습니다."
        if retries >= worker_max_retry():
            return f"재시도 상한({worker_max_retry()}회)에 도달했습니다."
        if not self.use_llm:
            return "Worker의 LLM 판단을 끈 상태입니다."
        return None

    def run(
        self,
        plan: dict,
        step_results: dict,
        cache: dict,
        calls: list[dict],
        emit: Emit = _no_emit,
    ) -> tuple[dict, dict, list[dict], dict]:
        """결과가 없는 작업만 수행한다. (step_results, cache, calls, 수행 보고) 를 반환한다."""
        step_results, cache, calls = dict(step_results), dict(cache), list(calls)
        rounds = [r["round"] for r in plan.get("rounds", [])]
        report: dict = {
            "round": rounds[-1] if rounds else 0,
            "tasks": [],
            "requests": [],
        }

        def decision(by: str, choice: str, reason: str, step: dict, **extra) -> None:
            emit(
                "decision",
                by=by,
                kind="worker",
                choice=choice,
                reason=reason,
                step=step["id"],
                tool=step["tool"],
                **extra,
            )

        for step in plan["steps"]:
            if step["id"] in step_results or step.get("discarded"):
                continue
            args, problem = prepare(step, step_results)
            task = {"step": step["id"], "tool": step["tool"], "attempts": 0}
            if problem:
                result = skip(step, args, problem, calls, emit)
                report["tasks"].append(task | {"status": "skipped", "code": SKIPPED})
                step_results[step["id"]] = {
                    **result,
                    "tool": step["tool"],
                    "args": args,
                }
                continue

            tried = [args]
            result = invoke(step, args, cache, calls, self.tool_caller, emit)
            status, request = "ok", None
            while not result["ok"]:
                code = result["error"]["code"]
                status = "failed"
                blocked = self._blocked(step, args, code, len(tried) - 1)
                if blocked:
                    if code in ErrorCode.RETRYABLE_WITH_NEW_ARGS:
                        request = {"by": "rule", "reason": blocked}
                        decision(
                            "rule",
                            "replan",
                            blocked + " Planner에 보고합니다.",
                            step,
                            code=code,
                        )
                    else:
                        decision("rule", "report", blocked, step, code=code)
                    break
                try:
                    d = self.decider()(
                        step, args, result["error"], adjustable_args(step), tried
                    )
                except (
                    Exception
                ):  # noqa: BLE001 (LLM이 실패하면 규칙으로 Planner에 보고한다, NF3003)
                    reason = "Worker LLM이 응답하지 않아 Planner에 보고합니다."
                    request = {"by": "rule", "reason": reason}
                    decision("rule", "replan", reason, step, code=code)
                    break
                changes = {c.name: _coerce(step, c.name, c.value) for c in d.args}
                allowed = set(adjustable_args(step))
                ignored = sorted(k for k in changes if k not in allowed)
                if self.guard_fixed:
                    changes = {k: v for k, v in changes.items() if k in allowed}
                decision(
                    "llm",
                    d.action,
                    d.reason,
                    step,
                    code=code,
                    args=changes if d.action == "retry" else None,
                    ignored=ignored if self.guard_fixed else [],
                )
                if d.action == "skip":
                    status = "dropped"
                    break
                if d.action == "replan":
                    request = {"by": "llm", "reason": d.reason}
                    break
                new_args = {**args, **changes}
                if not changes or new_args in tried:
                    reason = (
                        "고친 인자가 이미 시도한 인자와 같아 Planner에 보고합니다."
                    )
                    request = {"by": "rule", "reason": reason}
                    decision("rule", "replan", reason, step, code=code)
                    break
                args = new_args
                tried.append(args)
                result = invoke(
                    step,
                    args,
                    cache,
                    calls,
                    self.tool_caller,
                    emit,
                    attempt=len(tried) - 1,
                )
                status = "ok" if result["ok"] else "failed"

            code = None if result["ok"] else result["error"]["code"]
            report["tasks"].append(
                task | {"status": status, "attempts": len(tried), "code": code}
            )
            if request:
                report["requests"].append(
                    {
                        "step": step["id"],
                        "tool": step["tool"],
                        "code": code,
                        "tried": tried,
                        **request,
                    }
                )
            step_results[step["id"]] = {
                **result,
                "tool": step["tool"],
                "args": args,
                **({"attempts": len(tried)} if len(tried) > 1 else {}),
            }
        return step_results, cache, calls, report
