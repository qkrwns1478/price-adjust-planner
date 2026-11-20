"""한국전력 전력데이터개방포털 계약종별 전력사용량 Tool

- get_industrial_power_price (F3003 상세정보 조회, F3002 보충)
    지역의 산업용 전기 평균판매단가(원/kWh)를 기준월, 현재월로 비교한다.
    ECOS 원재료 품목으로는 확보할 수 없는 '주요 에너지경비(전기료)' 후보를 다른 API로 보충한다.

평균판매단가는 지역 전체(또는 시군구) 산업용 고객의 전기요금 합 ÷ 사용량 합으로 계산한다.
개별 공장의 실제 요금이 아니므로 응답에 그 사실(basis)을 함께 제공한다.
"""

from __future__ import annotations

import datetime as dt
import os
import time

from .base import ErrorCode, UpstreamError, fail, http_get, ok, shift_month

URL = "https://bigdata.kepco.co.kr/openapi/v1/powerUsage/contractType.do"
KEY_NAME = "KEP_CO_API"
INDUSTRIAL = "300"  # 계약종별 코드: 산업용
LATEST_LOOKBACK = 6  # 최신 집계월을 찾을 때 거슬러 올라가는 최대 개월 수
# 한전 API는 잘못된 키로 호출한 직후 몇 초 동안 올바른 키에도 401을 반환할 때가 있으므로, 401은 잠시 대기한 뒤 다시 요청한다
AUTH_RETRIES = 2
AUTH_RETRY_WAIT_SEC = 1.0

# 2026-11-18 실제 호출로 확인한 시도 코드 (광주, 전남은 코드 29, 46으로 호출해도 응답이 없어 제외했다)
REGION_CODES: dict[str, str] = {
    "서울": "11",
    "부산": "26",
    "대구": "27",
    "인천": "28",
    "대전": "30",
    "울산": "31",
    "세종": "36",
    "경기": "41",
    "충북": "43",
    "충남": "44",
    "경북": "47",
    "경남": "48",
    "제주": "50",
    "강원": "51",
    "전북": "52",
}

_month_cache: dict[tuple[str, str], list[dict] | None] = {}


class KepcoError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _fetch_month(key: str, metro: str, ym: str) -> list[dict] | None:
    """해당 월의 산업용 행 목록. 집계가 없으면 None을 반환한다."""
    ck = (metro, ym)
    if ck in _month_cache:
        return _month_cache[ck]
    params = {
        "apiKey": key,
        "year": ym[:4],
        "month": ym[4:],
        "metroCd": metro,
        "cntrCd": INDUSTRIAL,
        "returnType": "json",
    }
    for attempt in range(AUTH_RETRIES + 1):
        res = http_get(URL, params=params, timeout=20)
        try:
            body = res.json()
        except ValueError:
            raise KepcoError(
                ErrorCode.UPSTREAM_ERROR, "한전 API 응답을 해석하지 못했습니다."
            ) from None
        err = str(body.get("errCd", "")) if isinstance(body, dict) else ""
        if not (err == "401" or res.status_code == 401):
            break
        if attempt == AUTH_RETRIES:
            raise KepcoError(ErrorCode.INVALID_KEY, f"{KEY_NAME}가 유효하지 않습니다.")
        time.sleep(
            AUTH_RETRY_WAIT_SEC
        )  # 올바른 키에도 일시적으로 401을 반환하는 경우가 있다 (docs/verification/verification_tools.md 3.4)
    if err == "404" or res.status_code == 404:
        _month_cache[ck] = None
        return None
    if err or res.status_code >= 400:
        raise KepcoError(
            ErrorCode.UPSTREAM_ERROR,
            f"한전 API 오류 응답입니다. ({err or res.status_code})",
        )
    rows = body.get("data") or None
    _month_cache[ck] = rows
    return rows


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _summarize(rows: list[dict], city: str | None) -> dict | None:
    if city:
        rows = [r for r in rows if r.get("city") == city]
    bills = [_num(r.get("bill")) for r in rows]
    usages = [_num(r.get("powerUsage")) for r in rows]
    if not rows or None in bills or None in usages or sum(usages) == 0:
        return None  # 값이 비어 있으면 임의로 채우지 않는다
    custs = [_num(r.get("custCnt")) for r in rows]
    return {
        "unit_cost_won_per_kwh": round(sum(bills) / sum(usages), 2),
        "bill_won": int(sum(bills)),
        "usage_kwh": int(sum(usages)),
        "customers": int(sum(c for c in custs if c is not None)),
    }


def _latest_month(key: str, metro: str) -> str | None:
    ym = shift_month(dt.date.today().strftime("%Y%m"), -1)
    for _ in range(LATEST_LOOKBACK):
        if _fetch_month(key, metro, ym):
            return ym
        ym = shift_month(ym, -1)
    return None


def get_industrial_power_price(
    base_month: str,
    current_month: str | None = None,
    region: str = "대구",
    city: str | None = None,
) -> dict:
    metro = REGION_CODES.get(region.strip())
    if metro is None:
        return fail(
            ErrorCode.BAD_ARGUMENT,
            f"region은 {', '.join(REGION_CODES)} 중 하나여야 합니다.",
        )
    key = os.getenv(KEY_NAME, "").strip()
    if not key:
        return fail(ErrorCode.INVALID_KEY, f"{KEY_NAME}가 없습니다.")
    city = city.strip() if city else None
    try:
        latest = _latest_month(key, metro)
        if latest is None:
            return fail(
                ErrorCode.NO_DATA,
                f"최근 {LATEST_LOOKBACK}개월 동안 {region} 산업용 전력 집계가 없습니다.",
            )
        estimated = False
        if current_month is None:
            used = latest
            note = f"현재월을 지정하지 않아 최신 집계월({latest})을 기본값으로 사용했습니다."
        elif current_month > latest:
            used = latest
            estimated = True
            note = (
                f"요청한 현재월({current_month})은 아직 집계되지 않아 가장 최근 집계월({latest})로 "
                f"대신 계산했습니다. 이 변동률은 추정입니다."
            )
        else:
            used, note = current_month, None
        if base_month > used:
            return fail(
                ErrorCode.BAD_ARGUMENT,
                f"기준월({base_month})이 현재월({used})보다 늦습니다.",
            )

        cur_rows = _fetch_month(key, metro, used) or []
        base_rows = _fetch_month(key, metro, base_month) or []
    except UpstreamError:
        return fail(ErrorCode.UPSTREAM_ERROR, "한전 API를 부르지 못했습니다.")
    except KepcoError as e:
        return fail(e.code, e.message)

    if city and not any(r.get("city") == city for r in cur_rows + base_rows):
        cities = sorted({r.get("city") for r in cur_rows + base_rows if r.get("city")})
        return fail(
            ErrorCode.NOT_FOUND,
            f"{region}에 '{city}' 집계가 없습니다. 가능한 값: {', '.join(cities)}",
        )

    base = _summarize(base_rows, city)
    cur = _summarize(cur_rows, city)
    missing = [m for m, v in ((base_month, base), (used, cur)) if v is None]
    if missing:
        return fail(
            ErrorCode.NO_INDEX_VALUE,
            f"{region}{' ' + city if city else ''} 산업용 전력의 {', '.join(missing)} 집계가 비어 있어 변동률을 계산할 수 없습니다.",
        )

    return ok(
        {
            "region": region,
            "city": city,
            "contract_type": "산업용",
            "basis": "지역 산업용 고객 전체의 전기요금 합 ÷ 사용량 합 (개별 사업장 요금 아님)",
            "base": {"month": base_month, **base},
            "current": {"month": used, **cur},
            "change_rate_pct": round(
                (cur["unit_cost_won_per_kwh"] / base["unit_cost_won_per_kwh"] - 1)
                * 100,
                2,
            ),
            "requested_current_month": current_month,
            "estimated": estimated,
            "note": note,
        }
    )
