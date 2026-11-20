"""Tool 확인 (F3001 ~ F3004): 실제 외부 API를 호출한다.

    python backend/check_tools.py

LLM, Agent, FastAPI에 연결하지 않고 Tool만 단독으로 실행한다. 출력에 API 키를 포함하지 않는다.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT.parent / ".env")

from unittest import mock  # noqa: E402

from app.tools import call_tool, catalog, kepco  # noqa: E402
from app.tools.base import shift_month  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []


def show(r: dict, limit: int = 700) -> str:
    s = json.dumps(r["data"] if r["ok"] else r["error"], ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + " …"


def check(title: str, r: dict, expect, detail: bool = True):
    """expect에는 True(성공 기대), 실패 코드 문자열, 결과를 받아 bool을 돌려주는 함수 중 하나를 전달한다."""
    if callable(expect):
        good = bool(expect(r))
    elif expect is True:
        good = r["ok"]
    else:
        good = (not r["ok"]) and r["error"]["code"] == expect
    status = PASS if good else FAIL
    results.append((status, title))
    head = "ok   " if r["ok"] else f"fail {r['error']['code']}"
    print(f"[{status}] {title}\n       → {head}" + (f"  {show(r)}" if detail else ""))
    return r


@contextmanager
def without_env(name: str):
    saved = os.environ.pop(name, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[name] = saved


def section(t: str):
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


section("1. 등록한 Tool 목록 (Agent에 넘기는 명세)")
for c in catalog():
    print(
        f"[Tool] {c['name']}\n       {c['description']}\n       입력: "
        f"{json.dumps(c['parameters']['properties'], ensure_ascii=False)[:300]} …"
        f" / 필수 {c['parameters']['required']}"
    )
check("모르는 Tool 이름은 실패 응답", call_tool("no_such_tool", {}), "NOT_FOUND")
check(
    "모르는 인자는 BAD_ARGUMENT",
    call_tool("search_material_item", {"keyword": "열연", "foo": 1}),
    "BAD_ARGUMENT",
)

section("2. F3002 후보 조회 — search_material_item")
r = check(
    "정상: '열연강판' (기준월 생략 → 기본값=최신 집계월)",
    call_tool("search_material_item", {"keyword": "열연강판"}),
    lambda r: r["ok"] and r["data"]["requested_as_of"] is None and r["data"]["as_of"],
)
latest = r["data"]["as_of"] if r["ok"] else "202609"
item_code = r["data"]["candidates"][0]["item_code"] if r["ok"] else "307125AA"
future = shift_month(latest, 3)
check(
    f"미래 기준월 {future} → 최신 집계월로 대신 조회 + 추정 표시",
    call_tool("search_material_item", {"keyword": "열연강판", "as_of": future}),
    lambda r: r["ok"] and r["data"]["estimated"] and r["data"]["as_of"] == latest,
)
check(
    "별칭: '철판' → 강판, 후판 품목 후보",
    call_tool("search_material_item", {"keyword": "철판", "max_results": 3}),
    True,
)
check(
    "보충: '전기' → 한전 Tool로 보충 안내(supplement)",
    call_tool("search_material_item", {"keyword": "전기", "max_results": 2}),
    lambda r: r["ok"]
    and r["data"].get("supplement", {}).get("tool") == "get_industrial_power_price",
)
check(
    "빈 입력 → BAD_ARGUMENT",
    call_tool("search_material_item", {"keyword": ""}),
    "BAD_ARGUMENT",
)
check(
    "형식 틀림(as_of='2026-09') → BAD_ARGUMENT",
    call_tool("search_material_item", {"keyword": "열연강판", "as_of": "2026-09"}),
    "BAD_ARGUMENT",
)
check(
    "결과 없음 → NO_DATA",
    call_tool("search_material_item", {"keyword": "zzzzzz"}),
    "NO_DATA",
)
with without_env("ECOS_OPEN_API"):
    check(
        "키 없음 → INVALID_KEY",
        call_tool("search_material_item", {"keyword": "열연강판"}),
        "INVALID_KEY",
    )

section("3. F3003 상세정보 조회 — get_material_price_index")
check(
    f"연결: 후보 Tool의 item_code({item_code}) → 상세 조회 (현재월 생략 → 최신 집계월)",
    call_tool(
        "get_material_price_index", {"item_code": item_code, "base_month": "202512"}
    ),
    lambda r: r["ok"]
    and r["data"]["current"]["month"] == latest
    and r["data"]["change_rate_pct"] is not None,
)
check(
    f"미래 현재월 {future} → 최신 집계월로 계산 + 추정 표시",
    call_tool(
        "get_material_price_index",
        {
            "item_code": item_code,
            "base_month": "202512",
            "current_month": future,
            "include_trend": True,
        },
    ),
    lambda r: r["ok"] and r["data"]["estimated"] and len(r["data"]["trend"]) == 6,
)
check(
    "지수가 비어 있는 달(190001) → NO_INDEX_VALUE, 값을 채우지 않음",
    call_tool(
        "get_material_price_index", {"item_code": item_code, "base_month": "190001"}
    ),
    "NO_INDEX_VALUE",
)
check(
    "없는 품목 코드 → NOT_FOUND",
    call_tool(
        "get_material_price_index", {"item_code": "ZZZZ", "base_month": "202512"}
    ),
    "NOT_FOUND",
)
check(
    "기준월이 현재월보다 늦음 → BAD_ARGUMENT",
    call_tool(
        "get_material_price_index",
        {"item_code": item_code, "base_month": "202609", "current_month": "202512"},
    ),
    "BAD_ARGUMENT",
)
check(
    "필수 인자 누락 → BAD_ARGUMENT",
    call_tool("get_material_price_index", {"item_code": item_code}),
    "BAD_ARGUMENT",
)
with without_env("ECOS_OPEN_API"):
    check(
        "키 없음 → INVALID_KEY",
        call_tool(
            "get_material_price_index", {"item_code": item_code, "base_month": "202512"}
        ),
        "INVALID_KEY",
    )

section("3-1. F3003 (에너지경비 보충) — get_industrial_power_price")
r = check(
    "정상: 대구 산업용 (현재월 생략 → 최신 집계월)",
    call_tool("get_industrial_power_price", {"base_month": "202512", "region": "대구"}),
    lambda r: r["ok"] and r["data"]["requested_current_month"] is None,
)
check(
    "시군구 지정: 대구 달서구, 미래 현재월 → 추정 표시",
    call_tool(
        "get_industrial_power_price",
        {
            "base_month": "202512",
            "region": "대구",
            "city": "달서구",
            "current_month": "209912",
        },
    ),
    lambda r: r["ok"] and r["data"]["estimated"],
)
check(
    "없는 시군구 → NOT_FOUND",
    call_tool("get_industrial_power_price", {"base_month": "202512", "city": "강남구"}),
    "NOT_FOUND",
)
check(
    "집계가 없는 기준월(200001) → NO_INDEX_VALUE",
    call_tool("get_industrial_power_price", {"base_month": "200001"}),
    "NO_INDEX_VALUE",
)
check(
    "지원하지 않는 지역 → BAD_ARGUMENT",
    call_tool("get_industrial_power_price", {"base_month": "202512", "region": "평양"}),
    "BAD_ARGUMENT",
)
with without_env("KEP_CO_API"):
    check(
        "키 없음 → INVALID_KEY",
        call_tool("get_industrial_power_price", {"base_month": "202512"}),
        "INVALID_KEY",
    )

section("4. F3004 보조 정보 검색 — search_law_article")
check(
    "정상: 상생협력법 '주요 원재료'",
    call_tool(
        "search_law_article",
        {"law_name": "상생협력법", "keyword": "주요 원재료", "max_results": 2},
    ),
    lambda r: r["ok"]
    and all(
        a["link"].startswith("https://www.law.go.kr/") for a in r["data"]["articles"]
    ),
)
check(
    "정상: 상생협력법 '연동 조정'",
    call_tool(
        "search_law_article",
        {"law_name": "상생협력법", "keyword": "연동 조정", "max_results": 2},
    ),
    True,
)
check(
    "결과 없음 → NO_DATA (정의한 형식)",
    call_tool(
        "search_law_article", {"law_name": "상생협력법", "keyword": "우주정거장"}
    ),
    "NO_DATA",
)
check(
    "없는 법령 → NOT_FOUND",
    call_tool(
        "search_law_article", {"law_name": "존재하지않는법률zz", "keyword": "연동"}
    ),
    "NOT_FOUND",
)
check(
    "빈 입력 → BAD_ARGUMENT",
    call_tool("search_law_article", {"law_name": "", "keyword": "연동"}),
    "BAD_ARGUMENT",
)
with without_env("LAW_API_OC"):
    check(
        "키 없음 → INVALID_KEY",
        call_tool("search_law_article", {"law_name": "상생협력법", "keyword": "연동"}),
        "INVALID_KEY",
    )

section("5. 틀린 키 (한전) — 마지막에 실행한다")
# 한전 API는 잘못된 키로 호출한 직후 몇 초 동안 올바른 키에도 401을 반환할 수 있으므로, 다른 확인보다 뒤에 배치한다
with mock.patch.dict(os.environ, {"KEP_CO_API": "wrong-key"}):
    kepco._month_cache.clear()  # 앞서 저장한 응답이 재사용되지 않도록 비운다
    check(
        "틀린 키 → INVALID_KEY",
        call_tool("get_industrial_power_price", {"base_month": "202512"}),
        "INVALID_KEY",
    )
kepco._month_cache.clear()

section("요약")
n_pass = sum(1 for s, _ in results if s == PASS)
print(f"{n_pass}/{len(results)} PASS")
for s, t in results:
    if s == FAIL:
        print(f"  FAIL  {t}")
sys.exit(0 if n_pass == len(results) else 1)
