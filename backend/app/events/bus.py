"""실행 이벤트 발행과 구독 (F3012)

- 발행하는 쪽(메인 Agent)은 누가 구독하는지 알지 못한다. 구독자가 없어도 발행은 그대로 끝난다.
- 구독자가 예외를 던져도 발행하는 쪽으로 전파하지 않는다. 로깅 Agent가 멈춰도 메인 Agent는 응답한다.

이벤트 형식
    {"session_id", "run_id", "seq", "type", "node", "ts", "data"}

    session_id  대화 세션 식별자 (모든 이벤트에 공통)
    run_id      세션 안에서 요청 1건을 구분하는 식별자
    seq         요청 안에서 발생한 순서 (1부터)
    type        EventType 중 하나
    node        이벤트가 발생한 노드 (analyze, plan, execute, judge, respond). 요청 단위 이벤트는 None
    ts          발생 시각 (ISO 8601, 밀리초)
    data        이벤트 종류별 값
"""

from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable

Event = dict[str, Any]
Handler = Callable[[Event], None]


class EventType:
    RUN_START = "run_start"  # 요청 수신 {message}
    RUN_END = "run_end"  # 응답 완료 {ok, kind, intent, replan_count, stop_reason, elapsed_ms}
    NODE_START = "node_start"  # 단계 진입
    NODE_END = "node_end"  # 단계 종료 {ok, elapsed_ms, ...단계별 결과 요약}
    TOOL_REQUEST = "tool_request"  # Tool 호출 요청 {step, round, tool, args, attempt(Worker 재시도일 때)}
    TOOL_RESPONSE = "tool_response"  # Tool 응답 {step, round, tool, ok, code, elapsed_ms, result}
    TOOL_REUSED = "tool_reused"  # 같은 Tool, 같은 인자의 결과를 다시 사용 {step, round, tool, args, ok, code}
    TOOL_SKIPPED = "tool_skipped"  # 앞 단계 실패로 호출하지 않음 {step, round, tool, args, reason}
    DECISION = "decision"  # 실행 흐름을 바꾸는 판단 {by: llm|rule, kind, choice, reason}
    # Planner, Worker, 로깅 Agent 사이의 메시지 {sender, receiver, kind: task_list|work_report|verification, body}
    AGENT_MESSAGE = "agent_message"

    ALL = frozenset(
        {
            AGENT_MESSAGE,
            RUN_START,
            RUN_END,
            NODE_START,
            NODE_END,
            TOOL_REQUEST,
            TOOL_RESPONSE,
            TOOL_REUSED,
            TOOL_SKIPPED,
            DECISION,
        }
    )


class EventBus:
    """동기 방식의 발행, 구독 통로"""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []
        self._lock = threading.Lock()

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        """구독을 등록하고, 구독을 해지하는 함수를 반환한다."""
        with self._lock:
            self._handlers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers:
                    self._handlers.remove(handler)

        return unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 (구독자의 실패는 발행하는 쪽에 영향을 주지 않는다)
                pass


class Emitter:
    """요청 1건의 이벤트에 세션 id, 요청 id, 순서를 붙여 발행한다."""

    def __init__(
        self, bus: EventBus | None, session_id: str, run_id: str | None = None
    ):
        self.bus = bus
        self.session_id = session_id
        self.run_id = run_id or uuid.uuid4().hex
        self._seq = 0
        self._lock = threading.Lock()

    def emit(self, type: str, node: str | None = None, **data: Any) -> None:
        if self.bus is None:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq
        self.bus.publish(
            {
                "session_id": self.session_id,
                "run_id": self.run_id,
                "seq": seq,
                "type": type,
                "node": node,
                "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "data": data,
            }
        )


_NULL = Emitter(None, "")
_current: ContextVar[Emitter] = ContextVar("emitter", default=_NULL)


def current_emitter() -> Emitter:
    """실행 중인 요청의 Emitter. 요청 밖에서 부르면 아무것도 발행하지 않는 Emitter를 돌려준다."""
    return _current.get()


def bind_emitter(emitter: Emitter):
    return _current.set(emitter)


def unbind_emitter(token) -> None:
    _current.reset(token)
