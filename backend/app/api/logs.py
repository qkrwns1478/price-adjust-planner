"""실행 로그 조회 API (F3014, F3020)

목록, 상세(요청별 판정과 진단 포함), 삭제만 제공한다. 기록을 고칠 수 있으면 관찰의 의미가 사라지므로 수정 API는 두지 않는다.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..storage import RunLogStore

router = APIRouter(prefix="/api/logs", tags=["실행 로그"])


class SessionSummary(BaseModel):
    session_id: str
    started_at: str
    ended_at: str
    runs: int
    events: int
    tool_calls: int
    replans: int
    anomalies: int
    first_message: Optional[str] = None


class SessionList(BaseModel):
    sessions: list[SessionSummary]


class LogEvent(BaseModel):
    id: int
    session_id: str
    run_id: str
    seq: int
    type: str
    node: Optional[str] = None
    ts: str
    data: dict[str, Any]


class Check(BaseModel):
    check: str
    label: str
    status: Literal["ok", "warn", "undetermined"]
    message: str
    items: list[dict[str, Any]]


class Diagnosis(BaseModel):
    status: Literal["ok", "failed"]
    agent: Optional[str] = None
    stage: Optional[str] = None
    text: Optional[str] = None
    created_at: str


class Review(BaseModel):
    run_id: str
    verdict: Literal["normal", "anomaly", "undetermined", "failed"]
    warnings: int
    checks: list[Check]
    created_at: str
    diagnosis: Optional[Diagnosis] = None


class SessionLog(BaseModel):
    session_id: str
    events: list[LogEvent]
    reviews: list[Review]


class Deleted(BaseModel):
    session_id: str
    deleted: int


def _store(request: Request) -> RunLogStore:
    return request.app.state.log_store


@router.get("", response_model=SessionList, summary="실행 로그 목록 (세션 단위)")
def list_logs(request: Request) -> SessionList:
    return SessionList(sessions=_store(request).list_sessions())


@router.get(
    "/{session_id}",
    response_model=SessionLog,
    summary="세션 실행 로그 상세 (실행 순서)",
)
def get_log(session_id: str, request: Request) -> SessionLog:
    events = _store(request).get_session(session_id)
    if events is None:
        raise HTTPException(404, "실행 로그가 없습니다.")
    return SessionLog(
        session_id=session_id,
        events=events,
        reviews=_store(request).list_reviews(session_id),
    )


@router.delete("/{session_id}", response_model=Deleted, summary="세션 실행 로그 삭제")
def delete_log(session_id: str, request: Request) -> Deleted:
    deleted = _store(request).delete_session(session_id)
    if not deleted:
        raise HTTPException(404, "실행 로그가 없습니다.")
    return Deleted(session_id=session_id, deleted=deleted)
