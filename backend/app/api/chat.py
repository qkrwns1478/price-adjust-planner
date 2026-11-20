"""대화 요청 API (F3017, F3019)

- POST /api/chat         요청 1건을 끝까지 실행하고 응답을 한 번에 돌려준다.
- POST /api/chat/stream  실행 이벤트를 발생 순서대로 흘려보낸 뒤 마지막 줄에 응답을 싣는다 (NDJSON).

화면은 stream의 이벤트로 처리 중인 단계와 Tool 호출을 실시간으로 그린다.
이벤트는 EventBus를 구독해서 받으므로 메인 Agent와 로깅 Agent는 이 API를 알지 못한다.
같은 세션의 요청은 대화 맥락이 섞이지 않도록 한 번에 하나씩 실행한다.
"""

from __future__ import annotations

import json
import queue
import threading
from collections import defaultdict
from typing import Any, Iterator, Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent import PlannerAgent
from .system import missing_keys

router = APIRouter(prefix="/api/chat", tags=["대화"])

_session_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


class ChatRequest(BaseModel):
    session_id: Optional[str] = Field(
        None, description="이어 갈 대화 세션. 비우면 새 세션을 만든다"
    )
    message: str = Field(min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    """메인 Agent의 최종 응답 (F3011). kind에 따라 채워지는 값이 다르다."""

    session_id: str
    run_id: Optional[str] = None
    kind: Literal["result", "clarify", "chat", "notice", "error"]
    text: str = Field(description="Markdown 응답")
    summary: Optional[str] = None
    intent: Optional[str] = None
    intent_label: Optional[str] = None
    intent_reason: Optional[str] = None
    conditions: dict[str, Any] = Field(
        default_factory=dict, description="기준, 선호, 제약 조건"
    )
    changed: list[str] = Field(
        default_factory=list, description="이번 요청에서 바뀐 조건"
    )
    questions: list[str] = Field(default_factory=list, description="되물을 질문")
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    unverified: list[dict[str, Any]] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(default_factory=list)
    aux: list[dict[str, Any]] = Field(default_factory=list, description="근거 조문")
    evidence: list[dict[str, Any]] = Field(
        default_factory=list, description="근거 Tool 결과"
    )
    plan: Optional[dict[str, Any]] = Field(None, description="Tool 호출 계획과 라운드")
    replans: list[dict[str, Any]] = Field(default_factory=list)
    stop_reason: Optional[str] = None
    fatal: list[str] = Field(
        default_factory=list, description="다시 호출해도 같은 실패 코드"
    )
    plan_drafts: list[dict[str, Any]] = Field(
        default_factory=list, description="저장할 계획 초안 (F3015)"
    )
    disclaimer: Optional[str] = None
    review: Optional[dict[str, Any]] = Field(None, description="이상 징후 판정 (F3020)")
    missing_keys: list[str] = Field(
        default_factory=list, description="설정되지 않은 API 키 환경변수 이름"
    )


ERROR_TEXT = "요청을 처리하는 중 문제가 생겼습니다. 잠시 후 다시 시도해 주세요."


def _agent(request: Request) -> PlannerAgent:
    return request.app.state.agent


def _run(agent: PlannerAgent, session_id: str, message: str) -> dict:
    """응답 dict를 돌려준다. 실행이 실패해도 예외 대신 오류 응답을 만든다 (NF3002, NF3003)."""
    with _session_locks[session_id]:
        try:
            out = agent.run(session_id, message)
        except Exception:  # noqa: BLE001 (오류 상세는 화면에 싣지 않는다)
            out = {
                "session_id": session_id,
                "kind": "error",
                "text": ERROR_TEXT,
                "summary": ERROR_TEXT,
            }
    return ChatResponse(**out, missing_keys=missing_keys()).model_dump()


@router.post("", response_model=ChatResponse, summary="대화 요청")
def chat(body: ChatRequest, request: Request) -> dict:
    agent = _agent(request)
    return _run(agent, body.session_id or agent.new_session(), body.message)


@router.post(
    "/stream",
    summary="대화 요청 (실행 과정 스트림)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"application/x-ndjson": {}},
            "description": (
                '한 줄에 JSON 하나. {"type": "session", "session_id"} → '
                '{"type": "event", "event": 실행 이벤트} 반복 → {"type": "response", "response": ChatResponse}'
            ),
        }
    },
)
def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    agent = _agent(request)
    session_id = body.session_id or agent.new_session()
    lines: queue.Queue[dict | None] = queue.Queue()

    def on_event(event: dict) -> None:
        if event["session_id"] == session_id:
            lines.put({"type": "event", "event": event})

    def work() -> None:
        unsubscribe = agent.bus.subscribe(on_event)
        try:
            response = _run(agent, session_id, body.message)
        finally:
            unsubscribe()
        lines.put({"type": "response", "response": response})
        lines.put(None)

    def stream() -> Iterator[str]:
        yield _line({"type": "session", "session_id": session_id})
        threading.Thread(target=work, daemon=True).start()
        while (item := lines.get()) is not None:
            yield _line(item)

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _line(item: dict) -> str:
    return json.dumps(item, ensure_ascii=False, default=str) + "\n"
