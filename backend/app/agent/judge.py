"""제약 적합성 판단 (F3008)

모델 판단이 필요 없는 계산이므로 실행 흐름이 직접 수행한다.

    변동률(%)          = (현재 지수 ÷ 기준 지수 − 1) × 100              ← Tool이 계산한 change_rate_pct
    법정 비중 기준      = 비중 ≥ 10%                                    (상생협력법 제2조 12, 12의2)
    약정 조정 요건      = 변동률 ≥ 약정 비율
    조정 요청 금액(원)  = 현재 단가 × 비중 ÷ 100 × 변동률 ÷ 100
    요건 충족 지수(역산) = 기준 지수 × (1 + 약정 비율 ÷ 100)

외부 API로 확인할 수 없는 값(직전 조정일, 약정서의 지표, 산식)은 임의로 채우지 않고, 확인할 수 있는 값에서
역산한 기준으로 제시한다.
"""

from __future__ import annotations

import math
from datetime import date

from ..tools.base import ErrorCode, shift_month
from .config import LEGAL_SHARE_PCT

CYCLE_MONTHS = {
    "월": 1,
    "매월": 1,
    "분기": 3,
    "반기": 6,
    "연": 12,
    "년": 12,
    "매년": 12,
}


def _ym(ym: str) -> str:
    return f"{ym[:4]}년 {int(ym[4:])}월"


def _option(step: dict, result: dict) -> dict:
    """지수, 전기요금 조회 결과 하나를 판단에 사용할 값으로 요약한다."""
    opt = {
        "step": step["id"],
        "tool": step["tool"],
        "candidate": step.get("candidate"),
        "ok": result["ok"],
    }
    if not result["ok"]:
        return opt | {
            "code": result["error"]["code"],
            "message": result["error"]["message"],
            "label": (
                f"{step.get('candidate', 0) + 1}번째 후보"
                if step["role"] == "index"
                else "전기요금"
            ),
        }
    d = result["data"]
    if step["role"] == "power":
        return opt | {
            "label": f"{' '.join(filter(None, [d['region'], d.get('city')]))} 산업용 평균판매단가",
            "unit": "원/kWh",
            "base": {
                "month": d["base"]["month"],
                "value": d["base"]["unit_cost_won_per_kwh"],
            },
            "current": {
                "month": d["current"]["month"],
                "value": d["current"]["unit_cost_won_per_kwh"],
            },
            "rate": d["change_rate_pct"],
            "estimated": d.get("estimated", False),
            "note": d.get("note"),
            "basis": d.get("basis"),
        }
    return opt | {
        "label": f"{d['item_name']}({d['item_code']})",
        "item_code": d["item_code"],
        "item_name": d["item_name"],
        "unit": d.get("unit"),
        "base": d["base"],
        "current": d["current"],
        "rate": d["change_rate_pct"],
        "estimated": d.get("estimated", False),
        "note": d.get("note"),
        "trend": d.get("trend"),
    }


def _forecast(opt: dict, required: float) -> dict:
    """최근 추세가 이어진다고 가정한 참고용 도달 시점. 실제 발표 값이 아니므로 추정임을 명시한다."""
    trend = opt.get("trend") or []
    current = opt["current"]
    # 발표일은 API로 확인할 수 없으므로 날짜를 정하지 않고 '다음 달 지수가 집계된 뒤'로 표기한다
    out = {
        "recheck_month": shift_month(current["month"], 1),
        "recheck_after": f"{_ym(shift_month(current['month'], 1))} 지수가 집계된 뒤",
        "estimated": True,
    }
    if len(trend) < 2:
        return out | {"message": "추세 자료가 부족해 도달 시점을 추정하지 않습니다."}
    slope = (trend[-1]["value"] - trend[0]["value"]) / (len(trend) - 1)
    if slope <= 0:
        return out | {
            "slope": round(slope, 2),
            "message": f"최근 {len(trend)}개월 지수가 오르지 않아 도달 시점을 추정할 수 없습니다.",
        }
    months = math.ceil((required - current["value"]) / slope)
    return out | {
        "slope": round(slope, 2),
        "months": months,
        "reach_month": shift_month(current["month"], months),
        "message": (
            f"최근 {len(trend)}개월 평균 상승폭(월 {slope:.2f})이 이어지면 약 {months}개월 뒤"
            f"({_ym(shift_month(current['month'], months))}분)에 요건 지수에 닿을 것으로 추정합니다."
        ),
    }


def judge(intent: str, conditions: dict, plan: dict, step_results: dict) -> list[dict]:
    threshold = conditions.get("threshold_pct")
    price = conditions.get("unit_price_won")
    recheck = any(r["strategy"] == "recheck" for r in plan["rounds"])
    judgments = []

    for i, item in enumerate(conditions.get("items") or []):
        share = item.get("share_pct")
        j = {
            "item": i,
            "item_name": item["name"],
            "kind": item["kind"],
            "share_pct": share,
            "checks": [],
            "options": [],
            "reasons": [],
            "share_failed": False,
            "rate_failed": False,
        }
        basis = (
            "상생협력법 제2조 12의2"
            if item["kind"] == "energy"
            else "상생협력법 제2조 12"
        )
        if share is not None and intent in ("plan", "eligibility"):
            passed = share >= LEGAL_SHARE_PCT
            j["checks"].append(
                {
                    "label": "법정 비중 기준",
                    "value": share,
                    "op": "≥",
                    "threshold": LEGAL_SHARE_PCT,
                    "unit": "%",
                    "passed": passed,
                    "basis": basis,
                }
            )
            if not passed:
                j["share_failed"] = True
                j["reasons"].append(
                    f"비중 {share:g}%가 법정 기준 {LEGAL_SHARE_PCT:g}%에 못 미쳐 "
                    f"주요 {'에너지경비' if item['kind'] == 'energy' else '원재료'}가 아닙니다."
                )

        steps = [
            s
            for s in plan["steps"]
            if s.get("item") == i
            and s["role"] in ("index", "power")
            and not s.get("discarded")
        ]
        j["options"] = [
            _option(s, step_results[s["id"]]) for s in steps if s["id"] in step_results
        ]
        j["evidence"] = [
            s["id"]
            for s in plan["steps"]
            if s.get("item") == i and s["id"] in step_results and not s.get("discarded")
        ]
        ok_opts = [o for o in j["options"] if o["ok"]]

        for o in ok_opts:
            if threshold is not None:
                o["passed"] = o["rate"] >= threshold
                o["required_value"] = round(
                    o["base"]["value"] * (1 + threshold / 100), 2
                )
        for o in j["options"]:
            if not o["ok"]:
                # 키, 외부 API 오류는 원문 대신 일반 문구로 기록한다 (NF3002)
                message = (
                    "외부 API를 호출하지 못해 판단하지 않았습니다."
                    if o["code"] in ErrorCode.FATAL
                    else o["message"]
                )
                j["reasons"].append(f"{o['label']}: {message} ({o['code']})")

        if intent == "eligibility":
            j["status"] = "unsatisfied" if j["share_failed"] else "satisfied"
        elif j["share_failed"]:
            j["status"] = "unsatisfied"
        elif not ok_opts:
            j["status"] = "undetermined"
        elif threshold is None:
            j["status"] = "info"
            j["chosen"] = ok_opts[0]
        else:
            passing = [o for o in ok_opts if o["passed"]]
            chosen = passing[0] if passing else max(ok_opts, key=lambda o: o["rate"])
            j["chosen"] = chosen
            j["checks"].append(
                {
                    "label": "약정 조정 요건",
                    "value": chosen["rate"],
                    "op": "≥",
                    "threshold": threshold,
                    "unit": "%",
                    "passed": bool(passing),
                    "basis": "약정서",
                }
            )
            j["back_calc"] = {
                "required_value": chosen["required_value"],
                "unit": chosen.get("unit"),
                "message": (
                    f"기준 시점 {_ym(chosen['base']['month'])} 값 {chosen['base']['value']:g} × "
                    f"(1 + {threshold:g}%) = {chosen['required_value']:g} 이상이면 약정 조정 요건을 충족합니다."
                ),
            }
            for o in ok_opts:
                if not o["passed"]:
                    j["reasons"].append(
                        f"{o['label']}: 변동률 {o['rate']:+.2f}%가 약정 비율 {threshold:g}%에 못 미칩니다."
                    )
            if passing:
                j["status"] = "satisfied"
                ratio = share / 100 * chosen["rate"] / 100
                j["amount"] = {"ratio_pct": round(ratio * 100, 2)}
                if price:
                    add = round(price * ratio)
                    j["amount"] |= {
                        "unit_price_won": price,
                        "material_cost_won": round(price * share / 100),
                        "increase_won": add,
                        "new_price_won": price + add,
                    }
            else:
                j["status"] = "unsatisfied"
                j["rate_failed"] = True
                j["trend"] = bool(chosen.get("trend"))
                if chosen["rate"] <= -threshold:
                    j["reasons"].append(
                        "가격이 약정 비율 이상 내려가 위탁기업이 인하 조정을 요청할 수 있는 상황입니다."
                    )
                if recheck and chosen["tool"] == "get_material_price_index":
                    j["forecast"] = _forecast(chosen, chosen["required_value"])
        judgments.append(j)
    return judgments


def unverifiable(
    intent: str, conditions: dict, judgments: list[dict], today: date | None = None
) -> list[dict]:
    """외부 API로 확인할 수 없어 임의로 채우지 않은 값과, 그 대신 제시하는 역산 기준이다."""
    if intent != "plan":
        return []
    today = today or date.today()
    out = []
    cycle = conditions.get("cycle")
    months = CYCLE_MONTHS.get((cycle or "").strip())
    this_month = f"{today.year:04d}{today.month:02d}"
    if months:
        last = shift_month(this_month, -months)
        out.append(
            {
                "label": "직전 조정 시점",
                "criterion": f"조정 주기가 '{cycle}'이므로 직전 조정이 {_ym(last)} 이전이었다면 지금 요청할 수 있습니다.",
            }
        )
    else:
        out.append(
            {
                "label": "조정 주기",
                "criterion": "약정서의 조정 주기를 알려 주시면 직전 조정 시점 기준을 함께 계산합니다.",
            }
        )
    for j in judgments:
        chosen = j.get("chosen")
        if (
            chosen
            and chosen["tool"] == "get_material_price_index"
            and not conditions.get("index_name")
        ):
            out.append(
                {
                    "label": f"{j['item_name']} 기준 지표",
                    "criterion": f"약정서의 기준 지표가 ECOS {chosen['label']}일 때만 이 변동률이 그대로 적용됩니다.",
                }
            )
        if chosen and chosen["tool"] == "get_industrial_power_price":
            out.append(
                {
                    "label": "사업장 실제 전기요금",
                    "criterion": f"{chosen.get('basis')}. 계절별 요금 차이로 기준 월과 비교 월의 계절이 다르면 변동률이 달라질 수 있습니다.",
                }
            )
    return out
