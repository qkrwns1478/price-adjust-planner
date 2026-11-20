"""한국은행 ECOS 생산자물가지수(기본분류, 404Y014) Tool

- search_material_item     (F3002 후보 조회)   원재료 이름 → 품목 코드 후보
- get_material_price_index (F3003 상세정보 조회) 품목 코드 → 기준월, 현재월 지수와 변동률

ECOS는 '이미 집계된 달'의 지수만 제공한다. 아직 집계되지 않은 달을 요청하면 가장 최근 집계월로
대신 조회하고, 추정이 섞였다는 사실(estimated, note)을 응답에 명시한다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .base import ErrorCode, UpstreamError, fail, http_get, ok, shift_month

BASE_URL = "https://ecos.bok.or.kr/api"
STAT_CODE = "404Y014"  # 생산자물가지수(기본분류)
CYCLE = "M"  # 월간
KEY_NAME = "ECOS_OPEN_API"
SAMPLE_KEY = "sample"  # 개발용 공개 키. 한 번에 10건까지만 조회된다

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "ecos"
CACHE_TTL_SEC = 24 * 60 * 60  # 품목 목록은 월 1회 바뀌므로 하루 동안 재사용한다

# 사용자가 흔히 쓰는 이름 → ECOS 품목명에 포함된 어휘
ALIASES: dict[str, list[str]] = {
    "철판": ["강판", "후판"],
    "강철판": ["강판", "후판"],
    "철강": ["강판", "강관", "형강", "봉강"],
    "철근": ["철근"],
    "구리": ["동"],
    "알미늄": ["알루미늄"],
    "플라스틱": ["플라스틱", "합성수지"],
}
# ECOS 품목 목록으로는 지역별 실제 전기요금을 확인할 수 없으므로 다른 API Tool로 보충한다
ENERGY_WORDS = ("전기", "전력", "전기료", "전기요금", "에너지")

_memory_catalog: dict[str, tuple[float, list[dict]]] = {}


# ── ECOS 호출 ──────────────────────────────────────────────


class EcosError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _call(service: str, key: str, start: int, end: int, *path: str) -> dict:
    url = "/".join([BASE_URL, service, key, "json", "kr", str(start), str(end), *path])
    res = http_get(url, params={})
    try:
        body = res.json()
    except ValueError:
        raise EcosError(
            ErrorCode.UPSTREAM_ERROR, "ECOS 응답을 해석하지 못했습니다."
        ) from None
    if service in body:
        return body[service]
    result = body.get("RESULT", {})
    code = result.get("CODE", "")
    if code == "INFO-100":
        raise EcosError(ErrorCode.INVALID_KEY, f"{KEY_NAME}가 유효하지 않습니다.")
    if code == "INFO-200":
        raise EcosError(ErrorCode.NO_DATA, "해당하는 데이터가 없습니다.")
    raise EcosError(
        ErrorCode.UPSTREAM_ERROR, f"ECOS 오류 응답입니다. ({code or '코드 없음'})"
    )


def _get_key() -> str | None:
    key = os.getenv(KEY_NAME, "").strip()
    return key or None


def _load_catalog(key: str) -> list[dict]:
    """월간(M) 품목 목록. 메모리 → 파일 캐시 → ECOS 순으로 찾는다."""
    now = time.time()
    hit = _memory_catalog.get(STAT_CODE)
    if hit and now - hit[0] < CACHE_TTL_SEC:
        return hit[1]

    cache_file = CACHE_DIR / f"{STAT_CODE}_items.json"
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if now - cached["fetched_at"] < CACHE_TTL_SEC and cached["rows"]:
            _memory_catalog[STAT_CODE] = (cached["fetched_at"], cached["rows"])
            return cached["rows"]
    except (OSError, ValueError, KeyError):
        pass

    page = 10 if key == SAMPLE_KEY else 1000
    first = _call("StatisticItemList", key, 1, page, STAT_CODE)
    total = int(first.get("list_total_count", 0))
    raw = list(first.get("row", []))
    for start in range(page + 1, total + 1, page):
        raw.extend(
            _call("StatisticItemList", key, start, start + page - 1, STAT_CODE).get(
                "row", []
            )
        )

    rows = [
        {
            "item_code": r.get("ITEM_CODE"),
            "item_name": r.get("ITEM_NAME"),
            "parent_name": r.get("P_ITEM_NAME"),
            "weight": _to_float(r.get("WEIGHT")),
            "first_month": r.get("START_TIME"),
            "latest_month": r.get("END_TIME"),
        }
        for r in raw
        if r.get("CYCLE") == CYCLE and r.get("ITEM_CODE")
    ]

    _memory_catalog[STAT_CODE] = (now, rows)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"fetched_at": now, "rows": rows}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass  # 캐시 저장 실패는 조회 결과에 영향을 주지 않는다
    return rows


def _fetch_values(key: str, item_code: str, start_ym: str, end_ym: str) -> list[dict]:
    """[start_ym, end_ym] 구간의 월별 지수를 조회한다. sample 키의 조회 한도는 최대 10개월이다."""
    try:
        body = _call(
            "StatisticSearch", key, 1, 10, STAT_CODE, CYCLE, start_ym, end_ym, item_code
        )
    except EcosError as e:
        if e.code == ErrorCode.NO_DATA:
            return []
        raise
    return [
        {
            "month": r.get("TIME"),
            "value": _to_float(r.get("DATA_VALUE")),
            "item_name": r.get("ITEM_NAME1"),
            "unit": r.get("UNIT_NAME"),
        }
        for r in body.get("row", [])
    ]


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None  # 비어 있는 값을 0으로 채우지 않는다


def _norm(s: str | None) -> str:
    return "".join((s or "").split()).replace("·", "").replace("ㆍ", "")


# ── F3002 후보 조회 ───────────────────────────────────────


def search_material_item(
    keyword: str, as_of: str | None = None, max_results: int = 5
) -> dict:
    keyword = keyword.strip()
    if not keyword:
        return fail(ErrorCode.BAD_ARGUMENT, "keyword는 필수입니다.")
    key = _get_key()
    if not key:
        return fail(ErrorCode.INVALID_KEY, f"{KEY_NAME}가 없습니다.")
    try:
        catalog = _load_catalog(key)
    except UpstreamError:
        return fail(ErrorCode.UPSTREAM_ERROR, "ECOS를 부르지 못했습니다.")
    except EcosError as e:
        return fail(e.code, e.message)

    nk = _norm(keyword)
    terms = [nk] + ALIASES.get(nk, [])
    matched = [r for r in catalog if any(t in _norm(r["item_name"]) for t in terms)]
    supplement = None
    if any(w in nk for w in ENERGY_WORDS):
        supplement = {
            "tool": "get_industrial_power_price",
            "reason": "ECOS 지수로는 지역별 실제 전기요금을 알 수 없어 한전 산업용 전기요금으로 보충합니다.",
        }
    if not matched:
        if supplement:  # ECOS에는 없지만 다른 API로 보충할 수 있는 후보
            return ok(
                {
                    "keyword": keyword,
                    "candidates": [],
                    "total_matches": 0,
                    "requested_as_of": as_of,
                    "as_of": None,
                    "estimated": False,
                    "note": "ECOS 생산자물가지수에서 일치하는 품목이 없습니다.",
                    "supplement": supplement,
                }
            )
        return fail(ErrorCode.NO_DATA, f"'{keyword}'와 일치하는 ECOS 품목이 없습니다.")

    # 이름이 정확히 같은 품목을 먼저, 그다음 가중치(거래 비중)가 큰 품목 순으로 정렬한다
    matched.sort(key=lambda r: (_norm(r["item_name"]) != nk, -(r["weight"] or 0)))

    latest = max(r["latest_month"] for r in matched)
    note = None
    estimated = False
    if as_of is None:
        used = latest
        note = f"기준월을 지정하지 않아 최신 집계월({used})을 기본값으로 사용했습니다."
    elif as_of > latest:
        used = latest
        estimated = True
        note = (
            f"요청한 기준월({as_of})은 아직 집계되지 않아 가장 최근 집계월({used})로 "
            f"대신 조회했습니다. 이 값은 추정입니다."
        )
    else:
        used = as_of

    candidates = [
        {
            **r,
            "has_as_of": r["first_month"] <= used <= r["latest_month"],
        }
        for r in matched[:max_results]
    ]
    data = {
        "keyword": keyword,
        "requested_as_of": as_of,
        "as_of": used,
        "estimated": estimated,
        "note": note,
        "total_matches": len(matched),
        "candidates": candidates,
    }
    if supplement:
        data["supplement"] = supplement
    return ok(data)


# ── F3003 상세정보 조회 ───────────────────────────────────

TREND_MONTHS = 6


def get_material_price_index(
    item_code: str,
    base_month: str,
    current_month: str | None = None,
    include_trend: bool = False,
) -> dict:
    item_code = item_code.strip()
    if not item_code:
        return fail(ErrorCode.BAD_ARGUMENT, "item_code는 필수입니다.")
    key = _get_key()
    if not key:
        return fail(ErrorCode.INVALID_KEY, f"{KEY_NAME}가 없습니다.")
    try:
        catalog = _load_catalog(key)
        item = next((r for r in catalog if r["item_code"] == item_code), None)
        if item is None:
            return fail(
                ErrorCode.NOT_FOUND,
                f"품목 코드 {item_code}가 ECOS 품목 목록에 없습니다.",
            )

        latest = item["latest_month"]
        estimated = False
        if current_month is None:
            used_current = latest
            note = f"현재월을 지정하지 않아 최신 집계월({latest})을 기본값으로 사용했습니다."
        elif current_month > latest:
            used_current = latest
            estimated = True
            note = (
                f"요청한 현재월({current_month})은 아직 집계되지 않아 가장 최근 집계월({latest})로 "
                f"대신 계산했습니다. 이 변동률은 추정입니다."
            )
        else:
            used_current = current_month
            note = None
        if base_month > used_current:
            return fail(
                ErrorCode.BAD_ARGUMENT,
                f"기준월({base_month})이 현재월({used_current})보다 늦습니다.",
            )

        base_rows = _fetch_values(key, item_code, base_month, base_month)
        cur_rows = _fetch_values(key, item_code, used_current, used_current)
        trend = []
        if include_trend:
            start = max(
                shift_month(used_current, -(TREND_MONTHS - 1)), item["first_month"]
            )
            trend = [
                {"month": r["month"], "value": r["value"]}
                for r in _fetch_values(key, item_code, start, used_current)
            ]
    except UpstreamError:
        return fail(ErrorCode.UPSTREAM_ERROR, "ECOS를 부르지 못했습니다.")
    except EcosError as e:
        return fail(e.code, e.message)

    base_val = base_rows[0]["value"] if base_rows else None
    cur_val = cur_rows[0]["value"] if cur_rows else None
    missing = [
        m for m, v in ((base_month, base_val), (used_current, cur_val)) if v is None
    ]
    if missing:  # 판단에 필요한 값이 없으면 임의로 채우지 않고 판단하지 않는다
        return fail(
            ErrorCode.NO_INDEX_VALUE,
            f"{item['item_name']}({item_code})의 {', '.join(missing)} 지수가 비어 있어 변동률을 계산할 수 없습니다.",
        )
    if base_val == 0:
        return fail(
            ErrorCode.NO_INDEX_VALUE, "기준월 지수가 0이라 변동률을 계산할 수 없습니다."
        )

    unit = cur_rows[0].get("unit") or base_rows[0].get("unit")
    data = {
        "item_code": item_code,
        "item_name": item["item_name"],
        "parent_name": item["parent_name"],
        "unit": unit,
        "weight": item["weight"],
        "base": {"month": base_month, "value": base_val},
        "current": {"month": used_current, "value": cur_val},
        "change_rate_pct": round((cur_val / base_val - 1) * 100, 2),
        "requested_current_month": current_month,
        "estimated": estimated,
        "note": note,
    }
    if include_trend:
        data["trend"] = trend
    return ok(data)
