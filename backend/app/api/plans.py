"""조정 협의 계획 관리 API (F3015, F3016)

저장, 목록, 상세, 수정, 삭제를 제공한다. 사용자가 이미 정한 값을 다루는 작업이므로 Tool로 두지 않는다.
같은 날짜에 협의 시간이 겹치는 계획이 있어도 저장, 수정은 막지 않고 겹치는 계획을 결과와 함께 돌려준다.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from ..storage import PlanStore
from ..tools.base import YM_PATTERN

router = APIRouter(prefix="/api/plans", tags=["계획 관리"])

DATE_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"
TIME_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"
Status = Literal["ready", "requested", "agreed", "rejected"]


class PlanValues(BaseModel):
    """계획의 핵심 값. 계산 값은 Agent 응답의 plan_drafts에서 그대로 가져온다."""

    client: str = Field(min_length=1, description="위탁기업(협의 상대)")
    product: Optional[str] = Field(None, description="납품 품목")
    item: str = Field(min_length=1, description="연동 근거 원재료 또는 에너지경비")
    item_kind: Literal["material", "energy"] = "material"
    share_pct: Optional[float] = Field(
        None, ge=0, le=100, description="납품대금에서 차지하는 비중(%)"
    )
    unit_price_won: Optional[int] = Field(
        None, ge=0, description="조정 전 개당 단가(원)"
    )
    index_code: Optional[str] = Field(
        None, description="ECOS 품목 코드. 전기요금이면 비워 둔다"
    )
    index_name: Optional[str] = Field(None, description="기준 지표 이름")
    base_month: Optional[str] = Field(None, pattern=YM_PATTERN)
    base_value: Optional[float] = None
    current_month: Optional[str] = Field(None, pattern=YM_PATTERN)
    current_value: Optional[float] = None
    change_rate_pct: Optional[float] = Field(None, description="계산한 변동률(%)")
    threshold_pct: Optional[float] = Field(None, description="약정 변동률(%)")
    increase_ratio_pct: Optional[float] = Field(None, description="단가 대비 인상률(%)")
    increase_won: Optional[int] = Field(None, description="개당 조정 요청 금액(원)")
    new_price_won: Optional[int] = Field(None, description="조정 후 단가(원)")
    law_refs: list[str] = Field(
        default_factory=list,
        description="근거 조문. 예: '상생협력법 제16조의2(납품대금의 연동)'",
    )
    meeting_date: str = Field(pattern=DATE_PATTERN, description="협의 날짜 YYYY-MM-DD")
    start_time: str = Field(pattern=TIME_PATTERN, description="협의 시작 HH:MM")
    end_time: str = Field(pattern=TIME_PATTERN, description="협의 종료 HH:MM")
    status: Status = Field(
        "ready",
        description="ready 준비 / requested 요청함 / agreed 합의 / rejected 거절",
    )
    memo: Optional[str] = None

    @model_validator(mode="after")
    def _time_order(self):
        if self.end_time <= self.start_time:
            raise ValueError("협의 종료 시각은 시작 시각보다 늦어야 합니다.")
        return self


class PlanCreate(PlanValues):
    session_id: Optional[str] = Field(None, description="계획을 만든 대화 세션")
    run_id: Optional[str] = Field(None, description="계획을 만든 요청")


class PlanUpdate(BaseModel):
    """바꿀 값만 보낸다. 보내지 않은 값은 그대로 둔다."""

    client: Optional[str] = Field(None, min_length=1)
    product: Optional[str] = None
    item: Optional[str] = Field(None, min_length=1)
    item_kind: Optional[Literal["material", "energy"]] = None
    share_pct: Optional[float] = Field(None, ge=0, le=100)
    unit_price_won: Optional[int] = Field(None, ge=0)
    index_code: Optional[str] = None
    index_name: Optional[str] = None
    base_month: Optional[str] = Field(None, pattern=YM_PATTERN)
    base_value: Optional[float] = None
    current_month: Optional[str] = Field(None, pattern=YM_PATTERN)
    current_value: Optional[float] = None
    change_rate_pct: Optional[float] = None
    threshold_pct: Optional[float] = None
    increase_ratio_pct: Optional[float] = None
    increase_won: Optional[int] = None
    new_price_won: Optional[int] = None
    law_refs: Optional[list[str]] = None
    meeting_date: Optional[str] = Field(None, pattern=DATE_PATTERN)
    start_time: Optional[str] = Field(None, pattern=TIME_PATTERN)
    end_time: Optional[str] = Field(None, pattern=TIME_PATTERN)
    status: Optional[Status] = None
    memo: Optional[str] = None


class Plan(PlanValues):
    id: int
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    created_at: str
    updated_at: str


class PlanBrief(BaseModel):
    id: int
    client: str
    product: Optional[str] = None
    item: str
    meeting_date: str
    start_time: str
    end_time: str
    status: Status


class PlanSaved(BaseModel):
    plan: Plan
    overlaps: list[PlanBrief] = Field(description="같은 날짜에 협의 시간이 겹치는 계획")
    duplicates: list[PlanBrief] = Field(
        description="같은 위탁기업, 품목, 원재료로 진행 중인 계획"
    )


class PlanList(BaseModel):
    plans: list[Plan]


class PlanDeleted(BaseModel):
    id: int
    deleted: bool


def _store(request: Request) -> PlanStore:
    return request.app.state.plan_store


@router.post("", response_model=PlanSaved, status_code=201, summary="계획 저장")
def create_plan(body: PlanCreate, request: Request) -> PlanSaved:
    values = body.model_dump(exclude={"session_id", "run_id"})
    return PlanSaved(**_store(request).create(values, body.session_id, body.run_id))


@router.get("", response_model=PlanList, summary="계획 목록 (협의 날짜, 시각 순)")
def list_plans(request: Request) -> PlanList:
    return PlanList(plans=_store(request).list())


@router.get("/{plan_id}", response_model=Plan, summary="계획 상세")
def get_plan(plan_id: int, request: Request) -> Plan:
    plan = _store(request).get(plan_id)
    if plan is None:
        raise HTTPException(404, "계획이 없습니다.")
    return Plan(**plan)


@router.patch("/{plan_id}", response_model=PlanSaved, summary="계획 수정")
def update_plan(plan_id: int, body: PlanUpdate, request: Request) -> PlanSaved:
    store = _store(request)
    current = store.get(plan_id)
    if current is None:
        raise HTTPException(404, "계획이 없습니다.")
    changes = body.model_dump(exclude_unset=True)
    for key in (
        "client",
        "item",
        "item_kind",
        "meeting_date",
        "start_time",
        "end_time",
        "status",
    ):
        if key in changes and changes[key] is None:
            raise HTTPException(422, f"{key} 는 비울 수 없습니다.")
    start = changes.get("start_time", current["start_time"])
    end = changes.get("end_time", current["end_time"])
    if end <= start:
        raise HTTPException(422, "협의 종료 시각은 시작 시각보다 늦어야 합니다.")
    saved = store.update(plan_id, changes)
    if saved is None:
        raise HTTPException(404, "계획이 없습니다.")
    return PlanSaved(**saved)


@router.delete("/{plan_id}", response_model=PlanDeleted, summary="계획 삭제")
def delete_plan(plan_id: int, request: Request) -> PlanDeleted:
    if not _store(request).delete(plan_id):
        raise HTTPException(404, "계획이 없습니다.")
    return PlanDeleted(id=plan_id, deleted=True)
