"""Tool 등록부 (F3001)

Tool은 이름으로 찾는다. Agent에게는 catalog() 가 제공하는 이름, 설명, 입력 규격만 넘긴다.
새 Tool은 함수를 만든 뒤 아래 _TOOLS 목록에 Tool(...) 하나를 더하면 된다.
"""

from __future__ import annotations

from .base import YM_PATTERN, ErrorCode, Tool, ToolKind, fail
from .ecos import get_material_price_index, search_material_item
from .kepco import REGION_CODES, get_industrial_power_price
from .law import search_law_article

_YM = {"type": "string", "pattern": YM_PATTERN}

_TOOLS: list[Tool] = [
    Tool(
        name="search_material_item",
        kind="external",
        source="한국은행 ECOS 생산자물가지수(404Y014)",
        requirement="F3002",
        description=(
            "사용자가 말한 원재료 이름(예: 열연강판, 알루미늄, 철판)에 해당하는 한국은행 생산자물가지수 "
            "품목 코드를 모를 때 부른다. 변동률을 계산하기 전에 반드시 먼저 불러 item_code 후보를 얻는다. "
            "후보가 여럿이면 이름이 정확히 같은 품목이 앞에 온다. "
            "결과에 supplement가 있으면 그 Tool로 보충한다(전기료 등 에너지경비)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "원재료 이름. 예: '열연강판'",
                },
                "as_of": {
                    **_YM,
                    "description": "후보가 이 달의 지수를 갖는지 볼 기준월(YYYYMM). 생략하면 최신 집계월",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "돌려받을 후보 수. 기본 5",
                },
            },
            "required": ["keyword"],
        },
        func=search_material_item,
    ),
    Tool(
        name="get_material_price_index",
        kind="external",
        source="한국은행 ECOS 생산자물가지수(404Y014)",
        requirement="F3003",
        description=(
            "원재료 가격이 약정 기준 시점 대비 몇 % 변했는지 판단해야 할 때 부른다. "
            "search_material_item이 준 item_code와 약정서의 기준월을 넣으면 기준월, 현재월 지수와 "
            "변동률(change_rate_pct)을 준다. 현재월을 생략하면 최신 집계월을 쓴다. "
            "재계획에 쓸 최근 추세가 필요하면 include_trend=true."
        ),
        parameters={
            "type": "object",
            "properties": {
                "item_code": {
                    "type": "string",
                    "description": "ECOS 품목 코드. 예: '307125AA'",
                },
                "base_month": {
                    **_YM,
                    "description": "약정서의 기준 시점(YYYYMM). 예: '202512'",
                },
                "current_month": {
                    **_YM,
                    "description": "비교할 현재월(YYYYMM). 생략하면 최신 집계월",
                },
                "include_trend": {
                    "type": "boolean",
                    "description": "현재월까지 최근 6개월 지수를 함께 받을지",
                },
            },
            "required": ["item_code", "base_month"],
        },
        func=get_material_price_index,
    ),
    Tool(
        name="get_industrial_power_price",
        kind="external",
        source="한국전력 전력데이터개방포털 계약종별 전력사용량",
        requirement="F3003 (F3002 보충)",
        description=(
            "전기료(주요 에너지경비)가 올라 납품대금 조정이 가능한지 판단해야 할 때 부른다. "
            "지역 산업용 전기 평균판매단가(원/kWh)를 기준월과 현재월로 비교한 변동률을 준다. "
            "원재료 지수가 아니라 에너지경비가 대상일 때만 쓴다."
        ),
        parameters={
            "type": "object",
            "properties": {
                "base_month": {**_YM, "description": "약정서의 기준 시점(YYYYMM)"},
                "current_month": {
                    **_YM,
                    "description": "비교할 현재월(YYYYMM). 생략하면 최신 집계월",
                },
                "region": {
                    "type": "string",
                    "enum": list(REGION_CODES),
                    "description": "사업장이 있는 시도. 기본 '대구'",
                },
                "city": {
                    "type": "string",
                    "description": "시군구. 예: '달서구'. 생략하면 시도 전체",
                },
            },
            "required": ["base_month"],
        },
        func=get_industrial_power_price,
    ),
    Tool(
        name="search_law_article",
        kind="external",
        source="법제처 국가법령정보 공동활용 Open API",
        requirement="F3004",
        description=(
            "연동 대상 여부나 조정 협의의 판단 근거가 되는 법 조문을 사용자에게 함께 보여 줘야 할 때 부른다. "
            "법령명(약칭 가능, 예: '상생협력법')과 검색어(예: '주요 원재료', '납품대금 연동')를 넣으면 "
            "검색어가 모두 들어간 조문의 번호, 제목, 발췌, 링크를 준다. 조문을 지어내지 말고 이 Tool의 결과만 인용한다."
        ),
        parameters={
            "type": "object",
            "properties": {
                "law_name": {
                    "type": "string",
                    "description": "법령명 또는 약칭. 예: '상생협력법', '하도급법'",
                },
                "keyword": {
                    "type": "string",
                    "description": "조문에서 찾을 말. 공백으로 나누면 모두 포함한 조문만",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "돌려받을 조문 수. 기본 3",
                },
            },
            "required": ["law_name", "keyword"],
        },
        func=search_law_article,
    ),
    # 로컬 서버 API 툴은 kind="local" 로 여기에 추가한다.
]

TOOLS: dict[str, Tool] = {t.name: t for t in _TOOLS}
assert len(TOOLS) == len(_TOOLS), "Tool 이름이 겹칩니다."


def catalog(kind: ToolKind | None = None) -> list[dict]:
    """Agent에게 넘기는 Tool 명세 목록. 이름, 설명, 입력 규격만 포함한다."""
    return [t.spec() for t in TOOLS.values() if kind is None or t.kind == kind]


def get_tool(name: str) -> Tool | None:
    return TOOLS.get(name)


def call_tool(name: str, args: dict | None = None) -> dict:
    """이름으로 Tool을 호출한다. 등록되지 않은 이름도 실패 응답으로 반환한다."""
    tool = TOOLS.get(name)
    if tool is None:
        return fail(ErrorCode.NOT_FOUND, f"등록되지 않은 Tool입니다: {name}")
    if args is not None and not (
        isinstance(args, dict) and all(isinstance(k, str) for k in args)
    ):
        return fail(ErrorCode.BAD_ARGUMENT, "Tool 인자는 이름-값 객체여야 합니다.")
    return tool.run(**(args or {}))
