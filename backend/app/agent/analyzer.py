"""사용자 요청 분석 (F3005)

LLM은 의도 분류와 조건 추출에만 사용한다. 비어 있는 조건은 규칙으로 검사한다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Protocol

from .config import chat_model
from .state import CONDITION_LABEL, Analysis


class Analyzer(Protocol):
    def __call__(
        self, message: str, previous: dict, history: list[dict]
    ) -> Analysis: ...


SYSTEM_PROMPT = """너는 중소 제조 수탁기업의 납품대금 조정 상담 Agent의 요청 분석 단계다.
오늘 날짜는 {today} 이다. 상대적인 날짜(작년 12월, 올해 3월)는 이 날짜로 YYYYMM으로 환산한다.

규칙
- 이번 메시지에 실제로 나온 값만 update에 채운다. 말하지 않은 값은 반드시 null로 둔다.
- 추측하거나 일반적인 값(예: 약정 비율 5%, 기준 시점)을 지어내지 않는다.
- 직전 조건은 참고만 하고 update에 다시 적지 않는다. 바뀐 값만 적는다.
- '원재료 비중을 20%로'처럼 일반 명사(원재료, 원자재)로 말하면 items에 name을 그 말 그대로, share_pct를 20으로 적고 items_mode는 replace로 한다.
- '같은 조건에서', '다른 원재료로' 같은 후속 요청은 직전 조건을 유지한다는 뜻이다.
- 원재료와 에너지 요금(전기료, 전력, 가스비)을 구분해 kind를 정한다.
- '이번 분기', '이번 달' 같은 요청 시점은 timing에 그대로 적고 current_month는 null로 둔다.
- '올해 얼마나 올랐어'처럼 비교 시작 기간만 말하면 그 기간 바로 앞 달을 base_month로 둔다. (올해 → 작년 12월)

직전 조건(JSON)
{previous}

직전 대화 요약
{history}
"""


def make_llm_analyzer() -> Analyzer:
    """OpenAI로 구조화된 출력을 받는 분석기이다."""
    structured = chat_model().with_structured_output(
        Analysis, method="function_calling"
    )

    def analyze(message: str, previous: dict, history: list[dict]) -> Analysis:
        system = SYSTEM_PROMPT.format(
            today=date.today().isoformat(),
            previous=json.dumps(previous or {}, ensure_ascii=False),
            history="\n".join(
                f"- 사용자: {h['user']} / 응답: {h['summary']}" for h in history
            )
            or "(없음)",
        )
        return structured.invoke([("system", system), ("human", message)])

    return analyze


# ── 비어 있는 조건 (Tool을 호출하기 전에 되묻는다) ─────────────

_QUESTIONS = {
    "items": "어떤 원재료(또는 전기료 같은 에너지경비)의 가격 변동을 보시나요?",
    "share_pct": "{name} 비용이 납품단가에서 차지하는 비율은 대략 몇 %인가요?",
    "base_month": "약정서의 기준 시점은 언제인가요? (예: 2025년 12월)",
    "threshold_pct": "약정서에는 몇 % 이상 변동하면 조정하기로 되어 있나요?",
}

REQUIRED: dict[str, list[str]] = {
    "plan": ["items", "share_pct", "base_month", "threshold_pct"],
    "eligibility": ["items", "share_pct"],
    "fact": ["items", "base_month"],
}


def find_missing(intent: str, conditions: dict) -> list[dict]:
    """[{field, label, question}] 계산의 기준이 되는 값이 없으면 임의로 채우지 않고 되묻는다."""
    missing: list[dict] = []
    items = conditions.get("items") or []
    for field in REQUIRED.get(intent, []):
        if field == "items" and not items:
            missing.append(
                {
                    "field": "items",
                    "label": CONDITION_LABEL["items"],
                    "question": _QUESTIONS["items"],
                }
            )
        elif field == "share_pct":
            for item in items:
                if item.get("share_pct") is None:
                    missing.append(
                        {
                            "field": f"share_pct:{item['name']}",
                            "label": f"{item['name']} 비중",
                            "question": _QUESTIONS["share_pct"].format(
                                name=item["name"]
                            ),
                        }
                    )
        elif field in ("base_month", "threshold_pct") and conditions.get(field) is None:
            missing.append(
                {
                    "field": field,
                    "label": CONDITION_LABEL[field],
                    "question": _QUESTIONS[field],
                }
            )
    return missing
