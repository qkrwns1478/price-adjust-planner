"""최종 응답 생성 (F3011)

숫자와 조문은 Tool 결과와 판단 결과에서만 가져와 규칙으로 조립한다. LLM은 Tool이 필요 없는 대화에만 사용한다.
응답은 화면에서 그대로 사용할 수 있는 구조(dict)와, 그 구조로 만든 Markdown 문장(text)을 함께 포함한다.
"""

from __future__ import annotations

from typing import Callable

from ..tools.base import ErrorCode
from .config import DISCLAIMER, LAW_KEYWORD, chat_model
from .judge import unverifiable
from .planner import STRATEGY_LABEL
from .state import CONDITION_LABEL, INTENT_LABEL, grouped

Chatter = Callable[[str, list[dict]], str]

FATAL_NOTICE = {
    ErrorCode.INVALID_KEY: "외부 API 인증 설정을 확인해야 합니다. 관리자에게 API 키 설정을 확인해 달라고 요청해 주세요.",
    ErrorCode.UPSTREAM_ERROR: "외부 API가 응답하지 않습니다. 잠시 후 다시 시도해 주세요.",
}


def make_llm_chatter() -> Chatter:
    llm = chat_model(temperature=0.3)
    system = (
        "너는 중소 제조 수탁기업의 납품대금 조정 상담 Agent 다. Tool을 부르지 않는 인사, 사용법, 제도 설명에만 답한다. "
        "숫자나 법 조문 번호를 지어내지 말고, 구체적인 판단이 필요하면 납품단가, 원재료와 비중, 약정 비율, "
        "기준 시점을 알려 달라고 안내한다. 세 문장 이내로 답한다."
    )

    def chat(message: str, history: list[dict]) -> str:
        context = "\n".join(
            f"- 사용자: {h['user']} / 응답: {h['summary']}" for h in history
        )
        msgs = [
            ("system", system + (f"\n직전 대화 요약\n{context}" if context else "")),
            ("human", message),
        ]
        return llm.invoke(msgs).content

    return chat


# ── 근거 Tool 결과 요약 ────────────────────────────────────


def _summarize(r: dict) -> str:
    if not r["ok"]:
        code = r["error"]["code"]
        return FATAL_NOTICE.get(code, r["error"]["message"]) + f" ({code})"
    d, tool = r["data"], r["tool"]
    if tool == "search_material_item":
        c = (
            ", ".join(f"{x['item_code']} {x['item_name']}" for x in d["candidates"])
            or "없음"
        )
        sup = f", 보충: {d['supplement']['tool']}" if d.get("supplement") else ""
        return f"후보 {len(d['candidates'])}개: {c}{sup}"
    if tool == "get_material_price_index":
        return (
            f"{d['item_name']} {d['base']['month']} {d['base']['value']:g} → "
            f"{d['current']['month']} {d['current']['value']:g} ({d['change_rate_pct']:+.2f}%)"
        )
    if tool == "get_industrial_power_price":
        return (
            f"{' '.join(filter(None, [d['region'], d.get('city')]))} 산업용 {d['base']['month']} {d['base']['unit_cost_won_per_kwh']:g} → "
            f"{d['current']['month']} {d['current']['unit_cost_won_per_kwh']:g}원/kWh ({d['change_rate_pct']:+.2f}%)"
        )
    if tool == "search_law_article":
        return f"{d['law']['abbr'] or d['law']['name']} " + ", ".join(
            f"{a['article_no']}({a['title']})" for a in d["articles"]
        )
    return "성공"


def evidence(step_results: dict, calls: list[dict]) -> list[dict]:
    cached = {c["step"]: c["cached"] for c in calls}
    return [
        {
            "step": sid,
            "tool": r["tool"],
            "args": r["args"],
            "ok": r["ok"],
            "code": None if r["ok"] else r["error"]["code"],
            "reused": cached.get(sid, False),
            "attempts": r.get("attempts", 1),  # Worker가 인자를 고쳐 다시 부른 횟수 포함 (F3021)
            "summary": _summarize(r),
        }
        for sid, r in step_results.items()
    ]


def law_articles(step_results: dict) -> list[dict]:
    out, seen = [], set()
    for r in step_results.values():
        if r["tool"] != "search_law_article" or not r["ok"]:
            continue
        law = r["data"]["law"]
        for a in r["data"]["articles"]:
            if (law["name"], a["article_no"]) in seen:
                continue
            seen.add((law["name"], a["article_no"]))
            out.append(
                {
                    "law": law["abbr"] or law["name"],
                    "article_no": a["article_no"],
                    "title": a["title"],
                    "excerpt": a["excerpt"],
                    "link": a["link"],
                    "keyword": r["args"]["keyword"],
                }
            )
    return out


# ── 저장용 계획 초안 (F3015) ─────────────────────────────


def plan_drafts(conditions: dict, judgments: list[dict], aux: list[dict]) -> list[dict]:
    """요건을 충족한 항목마다 저장할 계획의 핵심 값을 판단 결과에서 그대로 옮긴다.

    협의 날짜와 시각은 API로 확인할 수 없는 사용자의 일정이므로 채우지 않고 사용자가 정한다.
    """
    refs = list(
        dict.fromkeys(f"{a['law']} {a['article_no']}({a['title']})" for a in aux)
    )
    drafts = []
    for j in judgments:
        if j["status"] != "satisfied" or not j.get("chosen"):
            continue
        c, a = j["chosen"], j.get("amount", {})
        drafts.append(
            {
                "client": conditions.get("client"),
                "product": conditions.get("product"),
                "item": j["item_name"],
                "item_kind": j["kind"],
                "share_pct": j.get("share_pct"),
                "unit_price_won": a.get("unit_price_won"),
                "index_code": c.get("item_code"),
                "index_name": c.get("item_name") or c.get("label"),
                "base_month": c["base"]["month"],
                "base_value": c["base"]["value"],
                "current_month": c["current"]["month"],
                "current_value": c["current"]["value"],
                "change_rate_pct": c["rate"],
                "threshold_pct": conditions.get("threshold_pct"),
                "increase_ratio_pct": a.get("ratio_pct"),
                "increase_won": a.get("increase_won"),
                "new_price_won": a.get("new_price_won"),
                "law_refs": refs,
                "meeting_date": None,
                "start_time": None,
                "end_time": None,
            }
        )
    return drafts


# ── 응답 조립 ────────────────────────────────────────────


def _won(v: int) -> str:
    return f"{v:,}원"


def _check_text(c: dict) -> str:
    return f"{c['value']:g}{c['unit']} {c['op']} {c['threshold']:g}{c['unit']} {'충족' if c['passed'] else '미충족'}"


def build_response(state: dict, chatter: Chatter | None = None) -> dict:
    intent = state.get("intent", "chat")
    conditions = state.get("conditions") or {}
    base = {
        "intent": intent,
        "intent_label": INTENT_LABEL.get(intent, intent),
        "intent_reason": state.get("intent_reason"),
        "conditions": grouped(conditions),
        "changed": state.get("changed", []),
    }

    if state.get("error"):
        text = "요청을 분석하지 못했습니다. 잠시 후 다시 시도해 주세요."
        return base | {
            "kind": "notice",
            "text": text,
            "summary": text,
            "disclaimer": None,
        }

    if state.get("missing"):
        qs = [m["question"] for m in state["missing"]]
        text = "조정 여부를 계산하려면 몇 가지 정보가 더 필요합니다.\n\n" + "\n".join(
            f"{i}. {q}" for i, q in enumerate(qs, 1)
        )
        return base | {
            "kind": "clarify",
            "text": text,
            "questions": qs,
            "disclaimer": None,
            "summary": "되물음: " + ", ".join(m["label"] for m in state["missing"]),
        }

    if intent == "manage":
        text = (
            "계획은 조정 계획 결과에서 협의 날짜와 시각을 정해 저장할 수 있습니다. "
            "저장한 계획의 조회, 수정, 삭제는 계획 관리 화면에서 할 수 있습니다."
        )
        return base | {
            "kind": "notice",
            "text": text,
            "summary": "계획 관리 요청 (계획 관리 화면 안내)",
            "disclaimer": None,
        }

    if intent == "chat":
        try:
            text = (chatter or make_llm_chatter())(
                state["user_message"], state.get("history", [])
            )
        except (
            Exception
        ):  # noqa: BLE001 (LLM 오류 상세는 화면에 포함하지 않는다, NF3002)
            text = (
                "납품단가, 원재료와 그 비중, 약정서의 조정 비율과 기준 시점을 알려 주시면 "
                "조정 요청 가능 여부와 금액을 계산해 드립니다."
            )
        return base | {
            "kind": "chat",
            "text": f"{text}\n\n> {DISCLAIMER}",
            "summary": text[:80],
            "disclaimer": DISCLAIMER,
            "plan": None,
        }

    return base | _result(state)


def _result(state: dict) -> dict:
    intent, conditions = state["intent"], state["conditions"]
    plan, step_results = state["plan"], state.get("step_results", {})
    judgments, calls = state.get("judgments", []), state.get("calls", [])

    fatal = sorted({c["code"] for c in calls if c["code"] in ErrorCode.FATAL})
    recs = [j for j in judgments if j["status"] == "satisfied"]
    excluded = [
        {"item": j["item_name"], "reason": r} for j in judgments for r in j["reasons"]
    ]
    criteria = [
        {
            "item": j["item_name"],
            "checks": j["checks"],
            "back_calc": j.get("back_calc"),
            "forecast": j.get("forecast"),
            "index": (
                {
                    k: j["chosen"].get(k)
                    for k in (
                        "label",
                        "base",
                        "current",
                        "rate",
                        "unit",
                        "note",
                        "estimated",
                    )
                }
                if j.get("chosen")
                else None
            ),
        }
        for j in judgments
    ]
    unver = unverifiable(intent, conditions, judgments)
    aux = law_articles(step_results)
    ev = evidence(step_results, calls)
    replans = plan["rounds"][1:]

    lines: list[str] = []
    if intent == "plan":
        lines.append("## 결론")
        for j in judgments:
            if j["status"] == "satisfied":
                a = j["amount"]
                money = (
                    f"개당 {_won(a['increase_won'])} 인상 요청 ({_won(a['unit_price_won'])} → {_won(a['new_price_won'])})"
                    if "increase_won" in a
                    else f"단가의 {a['ratio_pct']:g}% 인상 요청"
                )
                lines.append(
                    f"- **{j['item_name']}**: 조정 요청 요건을 충족합니다. {money}"
                )
            else:
                lines.append(
                    f"- **{j['item_name']}**: {'판단하지 못했습니다' if j['status'] == 'undetermined' else '조정 요청 요건을 충족하지 못합니다'}."
                )
        # 키, 외부 API 오류로 하나도 판단하지 못했으면 조건을 바꿔도 결과가 같으므로 조건 조정을 묻지 않는다
        if not recs and not (
            fatal and all(j["status"] == "undetermined" for j in judgments)
        ):
            lines += [
                "",
                "현재 조건으로는 연동에 따른 조정 요청 요건을 충족하는 항목이 없습니다. "
                "다른 원재료나 전기료 같은 에너지경비가 있는지, 약정서의 기준 시점과 조정 비율을 바꿔 볼 수 있는지 알려 주시면 다시 계산하겠습니다.",
            ]
    elif intent == "eligibility":
        lines.append("## 결론")
        for j in judgments:
            kind = "주요 에너지경비" if j["kind"] == "energy" else "주요 원재료"
            verdict = (
                f"{kind}에 해당해 납품대금 연동 대상이 될 수 있습니다"
                if j["status"] == "satisfied"
                else f"{kind}에 해당하지 않아 연동 대상이 아닙니다"
            )
            lines.append(f"- **{j['item_name']}** 비중 {j['share_pct']:g}%: {verdict}.")
    else:
        lines.append("## 조회 결과")
        for j in judgments:
            c = j.get("chosen")
            lines.append(
                f"- **{j['item_name']}**: "
                + (
                    f"{c['label']} {c['base']['month']} {c['base']['value']:g} → {c['current']['month']} "
                    f"{c['current']['value']:g}, 변동률 {c['rate']:+.2f}%"
                    if c
                    else "조회하지 못했습니다."
                )
            )

    alt = [a for a in aux if a["keyword"] == LAW_KEYWORD["alt_path"]]
    if alt and not recs:
        lines.append(
            f"- 대안 경로: 연동 요건과 별개로 {alt[0]['law']} {alt[0]['article_no']}({alt[0]['title']})를 근거로 "
            "위탁기업에 납품대금 조정을 신청할 수 있는지 검토해 보세요."
        )
    if fatal:
        lines += ["", "> " + " ".join(FATAL_NOTICE[c] for c in fatal)]

    if intent in ("plan", "eligibility"):
        lines += [
            "",
            "## 판단 기준",
            "| 항목 | 지표 | 기준 → 현재 | 판단 |",
            "|---|---|---|---|",
        ]
        for j, c in zip(judgments, criteria):
            idx = c["index"]
            span = (
                f"{idx['base']['month']} {idx['base']['value']:g} → {idx['current']['month']} {idx['current']['value']:g}"
                if idx
                else "-"
            )
            checks = (
                "<br>".join(f"{k['label']} {_check_text(k)}" for k in j["checks"])
                or "-"
            )
            lines.append(
                f"| {j['item_name']} | {idx['label'] if idx else '-'} | {span} | {checks} |"
            )
        for c in criteria:
            if c["back_calc"]:
                lines.append(f"- {c['item']} 역산 기준: {c['back_calc']['message']}")
            if c["forecast"]:
                f = c["forecast"]
                lines.append(
                    f"- {c['item']} 재조회: {f['recheck_after']}에 다시 조회합니다. {f['message']} (추정)"
                )
            if c["index"] and c["index"].get("note"):
                lines.append(f"- {c['item']}: {c['index']['note']}")
    notes = state.get("planner_notes", [])
    if notes:
        lines += ["", "## 다시 알려 주실 사항"] + [f"- {n}" for n in notes]
    if unver:
        lines += ["", "## API로 확인할 수 없어 직접 확인할 사항"] + [
            f"- {u['label']}: {u['criterion']}" for u in unver
        ]
    if replans:
        lines += ["", "## 재계획"]
        lines += [
            f"{r['round']}. {STRATEGY_LABEL[r['strategy']]}: {r['reason']}"
            for r in replans
        ]
        if state.get("stop_reason"):
            lines.append(f"- {state['stop_reason']}")
    if excluded:
        lines += ["", "## 제외한 후보"] + [
            f"- {e['item']}: {e['reason']}" for e in excluded
        ]
    if aux:
        lines += ["", "## 근거 조문"]
        for a in aux:
            first = a["excerpt"].split("\n")[0]
            lines.append(
                f"- [{a['law']} {a['article_no']}({a['title']})]({a['link']}): {first[:160]}"
            )
    lines += ["", "## 근거 Tool 결과"]
    lines += [
        f"- `{e['tool']}` {e['summary']}"
        + (" (재사용)" if e["reused"] else "")
        + (
            f" (Worker가 검색어를 고쳐 {e['attempts']}번째에 확보: {e['args'].get('keyword')})"
            if e["attempts"] > 1 and e["ok"]
            else ""
        )
        for e in ev
    ]
    lines += ["", f"> {DISCLAIMER}"]

    summary = "; ".join(
        f"{j['item_name']} {'충족' if j['status'] == 'satisfied' else j['status']}"
        + (f"({j['chosen']['rate']:+.2f}%)" if j.get("chosen") else "")
        + (
            f", 요청 {_won(j['amount']['increase_won'])}"
            if j.get("amount", {}).get("increase_won")
            else ""
        )
        for j in judgments
    )
    return {
        "kind": "result",
        "text": "\n".join(lines),
        "recommendations": [
            {
                "item": j["item_name"],
                "index": j["chosen"]["label"],
                "rate_pct": j["chosen"]["rate"],
                "threshold_pct": conditions.get("threshold_pct"),
                "amount": j["amount"],
            }
            for j in recs
        ],
        "criteria": criteria,
        "unverified": unver,
        "excluded": excluded,
        "aux": aux,
        "evidence": ev,
        "plan": {
            "steps": plan["steps"],
            "rounds": plan["rounds"],
            "skipped": plan.get("skipped", []),
        },
        "replans": replans,
        "stop_reason": state.get("stop_reason"),
        "planner_notes": notes,
        "fatal": fatal,
        "plan_drafts": (
            plan_drafts(conditions, judgments, aux) if intent == "plan" else []
        ),
        "disclaimer": DISCLAIMER,
        "summary": f"{INTENT_LABEL[intent]}: {summary or '판단 항목 없음'}",
    }


def condition_text(conditions: dict) -> str:
    """조건을 한 줄로 보여 준다. 확인 스크립트에서 사용한다."""
    parts = []
    for k, v in conditions.items():
        if v in (None, []):
            continue
        if k == "items":
            v = ", ".join(f"{i['name']}({i['kind']}, {i['share_pct']}%)" for i in v)
        parts.append(f"{CONDITION_LABEL[k]}={v}")
    return ", ".join(parts)
