"""Tool 호출 및 결과 통합 (F3007)

- 계획의 단계를 순서대로 호출하고, 결과는 단계 id와 Tool 이름을 키로 모아 둔다.
- args_from으로 앞 단계 결과의 값을 다음 단계 입력으로 넘긴다.
- 호출이 실패해도 사유를 남기고 다음 단계로 진행한다. 앞 단계가 실패해 입력을 만들 수 없으면 건너뛴다.
- 같은 Tool과 같은 인자는 캐시에서 재사용한다 (F3009 재계획).
- 호출 요청, 응답, 소요 시간, 재사용, 건너뜀을 이벤트로 발행한다 (F3012).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from ..tools import call_tool
from ..tools.base import fail

ToolCaller = Callable[[str, dict], dict]
Emit = Callable[..., None]  # emit(type, **data)


def _no_emit(type: str, **data: Any) -> None:
    pass

SKIPPED = "SKIPPED"  # 단계 상태 코드: 앞 단계가 실패해 입력을 만들지 못했다

_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve(ref: str, step_results: dict) -> tuple[bool, Any]:
    """'s1.candidates[0].item_code' 를 앞 단계 결과에서 찾는다."""
    step_id, _, path = ref.partition(".")
    result = step_results.get(step_id)
    if not result or not result.get("ok"):
        return False, f"{step_id} 단계가 성공하지 못했습니다."
    value: Any = result["data"]
    for name, index in _TOKEN.findall(path):
        try:
            value = value[int(index)] if index else value[name]
        except (KeyError, IndexError, TypeError):
            return False, f"{step_id} 단계 결과에 {path} 값이 없습니다."
    return True, value


def cache_key(tool: str, args: dict) -> str:
    return json.dumps([tool, args], ensure_ascii=False, sort_keys=True)


def prepare(step: dict, step_results: dict) -> tuple[dict, str | None]:
    """단계의 인자를 만든다. args_from을 앞 단계 결과로 채우지 못하면 (인자, 사유) 를 반환한다."""
    args = dict(step["args"])
    for name, ref in step.get("args_from", {}).items():
        found, value = resolve(ref, step_results)
        if not found:
            return args, value
        args[name] = value
    return args, None


def where(step: dict, attempt: int = 0) -> dict:
    out = {"step": step["id"], "round": step.get("round", 0), "tool": step["tool"]}
    return out | ({"attempt": attempt} if attempt else {})


def skip(step: dict, args: dict, problem: str, calls: list, emit: Emit) -> dict:
    """앞 단계가 실패해 호출하지 않은 단계를 기록한다."""
    emit("tool_skipped", **where(step), args=args, reason=problem)
    calls.append(
        {
            "step": step["id"],
            "tool": step["tool"],
            "args": args,
            "ok": False,
            "code": SKIPPED,
            "cached": False,
            "elapsed_ms": 0,
        }
    )
    return fail(SKIPPED, f"앞 단계 결과가 없어 호출하지 않았습니다: {problem}")


def invoke(
    step: dict,
    args: dict,
    cache: dict,
    calls: list,
    tool_caller: ToolCaller,
    emit: Emit,
    attempt: int = 0,
) -> dict:
    """Tool을 한 번 호출한다. 같은 Tool, 같은 인자는 캐시에서 다시 쓴다. cache, calls를 갱신한다."""
    at = where(step, attempt)
    key = cache_key(step["tool"], args)
    cached = key in cache
    if cached:
        result, elapsed = cache[key], 0
        code = None if result["ok"] else result["error"]["code"]
        emit("tool_reused", **at, args=args, ok=result["ok"], code=code)
    else:
        emit("tool_request", **at, args=args)
        started = time.perf_counter()
        result = tool_caller(step["tool"], args)
        elapsed = round((time.perf_counter() - started) * 1000)
        cache[key] = result
        code = None if result["ok"] else result["error"]["code"]
        emit(
            "tool_response",
            **at,
            ok=result["ok"],
            code=code,
            elapsed_ms=elapsed,
            result=result,
        )
    calls.append(
        {
            "step": step["id"],
            "tool": step["tool"],
            "args": args,
            "ok": result["ok"],
            "code": code,
            "cached": cached,
            "elapsed_ms": elapsed,
            **({"attempt": attempt} if attempt else {}),
        }
    )
    return result


def run_pending(
    plan: dict,
    step_results: dict,
    cache: dict,
    calls: list[dict],
    tool_caller: ToolCaller = call_tool,
    emit: Emit = _no_emit,
) -> tuple[dict, dict, list[dict]]:
    """아직 결과가 없는 단계만 한 번씩 호출한다. 갱신된 (step_results, cache, calls) 를 반환한다.

    실패를 고쳐 다시 부르는 판단은 Worker(worker.py)가 맡는다.
    """
    step_results, cache, calls = dict(step_results), dict(cache), list(calls)
    for step in plan["steps"]:
        if step["id"] in step_results or step.get("discarded"):
            continue
        args, problem = prepare(step, step_results)
        if problem:
            result = skip(step, args, problem, calls, emit)
        else:
            result = invoke(step, args, cache, calls, tool_caller, emit)
        step_results[step["id"]] = {**result, "tool": step["tool"], "args": args}
    return step_results, cache, calls


def by_tool(step_results: dict) -> dict[str, list[dict]]:
    """Tool 이름별로 결과를 모은다."""
    out: dict[str, list[dict]] = {}
    for step_id, r in step_results.items():
        out.setdefault(r["tool"], []).append(
            {"step": step_id, **{k: v for k, v in r.items() if k != "tool"}}
        )
    return out
