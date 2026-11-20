"""Agent 상태와 조건 모델 (F3005, F3010)

그래프의 상태는 체크포인터에 저장되므로 모두 JSON으로 바꿀 수 있는 값(dict, list, str, 숫자)만 사용한다.
"""

from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict

from pydantic import BaseModel, Field

from ..tools.base import YM_PATTERN

Intent = Literal["plan", "fact", "eligibility", "manage", "chat"]

INTENT_LABEL = {
    "plan": "조정 계획 수립",
    "fact": "사실 조회",
    "eligibility": "대상 여부 판정",
    "manage": "계획 확정, 관리",
    "chat": "그 밖의 대화",
}


# ── 조건 (F3005) ──────────────────────────────────────────


class CostItem(BaseModel):
    """연동 판단의 대상이 되는 원재료 또는 에너지경비 하나를 나타낸다."""

    name: str = Field(
        description="사용자가 말한 원재료, 에너지 이름 그대로. 예: '열연강판', '철판', '전기료'"
    )
    kind: Literal["material", "energy"] = Field(
        description="원재료면 material, 전기료, 전력, 가스비 같은 에너지 요금이면 energy"
    )
    share_pct: Optional[float] = Field(
        None,
        description="이 비용이 납품대금에서 차지하는 비율(%). 말하지 않았으면 null",
    )


class ConditionUpdate(BaseModel):
    """이번 메시지에 나온 조건만 채운다. 언급하지 않은 값은 null로 둔다."""

    # 기준 조건
    client: Optional[str] = Field(
        None, description="위탁기업(협의 상대) 이름. 예: 'A사'"
    )
    product: Optional[str] = Field(None, description="납품하는 품목. 예: '프레임'")
    unit_price_won: Optional[int] = Field(
        None, description="현재 개당 납품단가(원). '12만 원'이면 120000"
    )
    items: Optional[list[CostItem]] = Field(
        None, description="이번 메시지에 나온 원재료, 에너지경비. 없으면 null"
    )
    base_month: Optional[str] = Field(
        None,
        pattern=YM_PATTERN,
        description="약정서의 기준 시점(YYYYMM). '작년 12월'은 오늘 날짜로 환산",
    )
    current_month: Optional[str] = Field(
        None,
        pattern=YM_PATTERN,
        description="비교할 달(YYYYMM)을 사용자가 콕 집어 말했을 때만. '이번 분기'는 null",
    )
    timing: Optional[str] = Field(
        None, description="조정을 요청하려는 시점 표현 그대로. 예: '이번 분기'"
    )
    # 선호 조건
    index_name: Optional[str] = Field(
        None, description="약정서에 적힌 기준 지표 이름을 말했을 때만"
    )
    region: Optional[str] = Field(None, description="사업장 시도. 예: '대구'")
    city: Optional[str] = Field(None, description="사업장 시군구. 예: '달서구'")
    # 제약 조건
    threshold_pct: Optional[float] = Field(
        None, description="약정서의 조정 요건 변동률(%). '5% 이상 변동하면'이면 5"
    )
    cycle: Optional[str] = Field(
        None, description="약정서의 조정 주기. 예: '분기', '월', '반기'"
    )


class Analysis(BaseModel):
    """의도 분석 결과를 나타낸다. LLM의 구조화된 출력으로 채운다."""

    intent: Intent = Field(
        description=(
            "plan: 조정 요청 가능 여부, 요청 금액, 협의 계획을 묻는다 / "
            "fact: 지수나 전기요금이 얼마나 변했는지 사실만 묻는다 / "
            "eligibility: 비중을 말하며 연동 대상인지만 묻는다 / "
            "manage: 계획 저장, 조회, 수정, 삭제 / "
            "chat: 인사, 사용법, 제도 설명 등 Tool이 필요 없는 대화"
        )
    )
    intent_reason: str = Field(description="그 의도로 분류한 이유 한 문장")
    update: ConditionUpdate
    items_mode: Literal["keep", "replace", "add"] = Field(
        description=(
            "직전 조건의 원재료 목록을 어떻게 바꾸는지. 원재료를 말하지 않았으면 keep, "
            "'~로 바꿔서', '~만'이면 replace, '~도 같이'면 add"
        )
    )


CONDITION_GROUPS: dict[str, list[str]] = {
    "기준": [
        "client",
        "product",
        "unit_price_won",
        "items",
        "base_month",
        "current_month",
        "timing",
    ],
    "선호": ["index_name", "region", "city"],
    "제약": ["threshold_pct", "cycle"],
}

CONDITION_LABEL = {
    "client": "위탁기업",
    "product": "품목",
    "unit_price_won": "현재 단가(원)",
    "items": "원재료, 에너지경비",
    "base_month": "기준 시점",
    "current_month": "비교 월",
    "timing": "요청 시점",
    "index_name": "기준 지표",
    "region": "시도",
    "city": "시군구",
    "threshold_pct": "약정 변동률(%)",
    "cycle": "조정 주기",
}

EMPTY_CONDITIONS: dict[str, Any] = {k: None for k in ConditionUpdate.model_fields} | {
    "items": []
}


def grouped(conditions: dict) -> dict[str, dict]:
    """조건을 기준, 선호, 제약으로 나눠 보여 준다. 비어 있는 값은 None 그대로 둔다."""
    return {
        g: {k: conditions.get(k) for k in keys} for g, keys in CONDITION_GROUPS.items()
    }


GENERIC_ITEM_NAMES = {"원재료", "원자재", "재료", "자재", "에너지", "에너지경비"}


def merge_conditions(previous: dict, analysis: Analysis) -> tuple[dict, list[str]]:
    """직전 조건에 이번 메시지에서 나온 값만 덮어쓴다 (F3010). (새 조건, 바뀐 필드) 를 반환한다."""
    merged = {**EMPTY_CONDITIONS, **(previous or {})}
    changed: list[str] = []
    update = analysis.update.model_dump()
    new_items = update.pop("items")
    for key, value in update.items():
        if value is not None and value != merged.get(key):
            merged[key] = value
            changed.append(key)

    old_items = merged.get("items") or []
    if new_items and analysis.items_mode != "keep":
        if analysis.items_mode == "add":
            names = {i["name"] for i in old_items}
            items = old_items + [i for i in new_items if i["name"] not in names]
        else:
            items = new_items
            # "원재료 비중을 20%로"처럼 품목을 지칭하지 않는 일반 명사는 직전 품목명을 유지한다
            if len(old_items) == 1 and len(items) == 1 and items[0]["name"] in GENERIC_ITEM_NAMES:
                items = [{**old_items[0], **{k: v for k, v in items[0].items() if k != "name" and v is not None}}]
            # "같은 조건에서 원재료만 바꿔서": 하나를 하나로 바꾸고 비중을 언급하지 않았으면 직전 비중을 그대로 사용한다
            if (
                len(old_items) == 1
                and len(items) == 1
                and items[0]["share_pct"] is None
            ):
                items = [{**items[0], "share_pct": old_items[0]["share_pct"]}]
        if items != old_items:
            merged["items"] = items
            changed.append("items")
    elif new_items and not old_items:
        merged["items"] = new_items
        changed.append("items")
    return merged, changed


# ── 그래프 상태 ──────────────────────────────────────────


class AgentState(TypedDict, total=False):
    # 대화 맥락: 체크포인터에 저장되어 다음 요청으로 이어진다 (F3010)
    session_id: str
    conditions: dict  # 누적된 기준, 선호, 제약 조건
    history: list[dict]  # 직전 대화 요약 [{user, intent, summary}]

    # 이번 요청에서만 사용하는 값: analyze 노드가 매번 초기화한다
    user_message: str
    intent: str
    intent_reason: str
    changed: list[str]  # 이번 요청에서 바뀐 조건
    missing: list[dict]  # 계산에 꼭 필요한데 비어 있는 조건
    error: str | None  # 분석 실패 코드 (원문은 포함하지 않는다)
    plan: dict  # 현재 계획 {steps, rounds}
    results: dict  # Tool 이름 → 호출 결과 목록 (F3007)
    step_results: dict  # 단계 id → 호출 결과
    calls: list[dict]  # 실제 호출과 재사용 기록
    cache: dict  # (Tool, 인자) → 결과. 재계획에서 재사용한다 (F3009)
    judgments: list[dict]  # 원재료, 에너지경비별 판단 (F3008)
    replan_count: int
    replan: dict | None  # 판단 노드가 요청한 재계획 {strategy, reason}
    stop_reason: str | None  # 재계획을 멈춘 사유
    worker_requests: list[dict]  # Worker가 해결하지 못하고 Planner에 올린 요청 (F3021)
    handled_requests: list[str]  # Planner가 처리한 요청의 단계 id
    verified_steps: list[str]  # 로깅 Agent 검증으로 다시 수행한 단계 id
    planner_notes: list[str]  # Planner가 작업으로 해결하지 못해 사용자에게 확인할 사항
    response: dict  # 최종 응답 (F3011)
