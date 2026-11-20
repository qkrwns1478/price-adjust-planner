"""작업 계획 수립과 재계획 (F3006, F3009)

첫 계획: LLM이 Tool 명세(catalog)의 설명을 보고 필요한 Tool과 순서를 고른다.
코드는 그 초안을 검증해 계획으로 확정한다.
    - 등록되지 않은 Tool, 이 의도에 필요 없는 Tool, 조건에 없는 항목, 비중 미달 항목의 단계는 제외한다.
    - 계산에 꼭 필요한데 빠진 단계(후보 조회 → 상세 조회, 근거 조문)는 추가한다.
    - 사용자 조건으로 정해지는 인자(기준 시점, 비교 월, 지역)와 앞 단계 결과의 연결(args_from)은 코드가 채운다.
    - 제외하거나 추가한 내용은 plan.corrections에 기록한다. LLM이 실패하면 같은 규칙으로 계획을 만든다.
재계획: 판단 결과로 방법을 선택할 수 있으므로 규칙으로 처리한다.

단계(step) 형식
    {
        "id": "s2", "tool": "get_material_price_index", "args": {...},
        "args_from": {"item_code": "s1.candidates[0].item_code"},   # 앞 단계 결과에서 가져올 입력
        "role": "index", "item": 0, "candidate": 0, "round": 0, "purpose": "..."
    }
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from pydantic import BaseModel, Field

from ..tools import catalog
from .config import LAW_KEYWORD, LAW_NAME, LEGAL_SHARE_PCT, chat_model
from .state import INTENT_LABEL

TOOL_INTENTS = ("plan", "fact", "eligibility")
ROLE_OF = {
    "search_material_item": "search",
    "get_material_price_index": "index",
    "get_industrial_power_price": "power",
    "search_law_article": "law",
}

STRATEGY_LABEL = {
    "next_candidate": "다른 후보 지표로 다시 계산",
    "recheck": "다음 지수 발표 때 다시 조회하는 계획으로 전환",
    "alt_path": "연동이 아닌 조정 신청 경로 탐색",
    "worker_request": "Worker가 해결하지 못한 실패를 반영해 작업 재구성",
    "verify_fix": "로깅 Agent 검증 결과를 반영해 작업 재구성",
}

# Worker가 고칠 수 있는 인자 (F3021). 나머지 인자와 args_from은 Planner가 고정한다.
# 사용자가 약정서의 지표 이름을 말했으면 그 검색어도 고정한다.
ADJUSTABLE = {
    "search_material_item": ("keyword", "max_results"),
    "search_law_article": ("keyword", "max_results"),
}


def needs_tools(intent: str) -> bool:
    """인사, 사용법, 계획 관리처럼 호출할 Tool이 없는 요청은 계획 단계를 건너뛴다."""
    return intent in TOOL_INTENTS


# ── LLM 계획 초안 ────────────────────────────────────────


class DraftStep(BaseModel):
    tool: str = Field(description="Tool 명세의 name")
    item: Optional[str] = Field(
        None,
        description="이 단계가 다루는 원재료, 에너지 이름(조건의 items[].name). 법령 검색이면 null",
    )
    keyword: Optional[str] = Field(
        None,
        description="search_material_item은 품목 검색어, search_law_article은 조문 검색어",
    )
    purpose: str = Field(description="이 단계가 필요한 이유 한 문장")


class PlanDraft(BaseModel):
    steps: list[DraftStep] = Field(description="실행 순서대로 나열한 단계")
    reason: str = Field(description="이 계획을 고른 이유 한 문장")


Selector = Callable[[str, dict, str], PlanDraft]

PLAN_PROMPT = """너는 납품대금 조정 상담 Agent의 계획 수립 단계다.
아래 Tool 명세의 description을 보고, 사용자 요청을 처리하는 데 필요한 Tool만 실행 순서대로 고른다.
- 앞 Tool의 결과가 다음 Tool의 입력이 되면 앞 Tool을 먼저 둔다.
- 필요 없는 Tool은 넣지 않는다. 조건에 없는 원재료를 지어내지 않는다.
- 의도별 목표: plan = 변동률로 약정 조정 요건을 판단하고 판단 근거 조문과 함께 답한다 /
  fact = 지수, 요금의 변동만 답한다 / eligibility = 비중을 법정 기준과 비교하고 근거 조문과 함께 답한다.

Tool 명세
{catalog}

의도: {intent} ({label})
조건: {conditions}"""


def make_llm_selector() -> Selector:
    structured = chat_model().with_structured_output(
        PlanDraft, method="function_calling"
    )

    def select(intent: str, conditions: dict, message: str) -> PlanDraft:
        system = PLAN_PROMPT.format(
            catalog=json.dumps(catalog(), ensure_ascii=False),
            intent=intent,
            label=INTENT_LABEL.get(intent, intent),
            conditions=json.dumps(conditions, ensure_ascii=False),
        )
        return structured.invoke([("system", system), ("human", message)])

    return select


# ── 계획 확정 ────────────────────────────────────────────


class _Builder:
    def __init__(self, plan: dict | None = None):
        self.plan = plan or {"steps": [], "rounds": [], "skipped": []}

    def add(
        self,
        tool: str,
        args: dict,
        role: str,
        purpose: str,
        round_no: int,
        args_from: dict | None = None,
        item: int | None = None,
        candidate: int | None = None,
        lock: tuple[str, ...] = (),
        no_retry: bool = False,
    ) -> str:
        sid = f"s{len(self.plan['steps']) + 1}"
        args = {k: v for k, v in args.items() if v is not None}
        adjustable = set(ADJUSTABLE.get(tool, ())) - set(lock)
        step = {
            "id": sid,
            "tool": tool,
            "args": args,
            "fixed": sorted(
                k for k in set(args) | set(args_from or {}) if k not in adjustable
            ),
            "role": role,
            "round": round_no,
            "purpose": purpose,
        }
        if args_from:
            step["args_from"] = args_from
        if no_retry:
            step["no_retry"] = True
        if item is not None:
            step["item"] = item
        if candidate is not None:
            step["candidate"] = candidate
        self.plan["steps"].append(step)
        return sid


def _norm(text: str | None) -> str:
    return re.sub(r"[\sㆍ·]", "", text or "")


def _match_item(name: str | None, items: list[dict], kind: str) -> int | None:
    """초안의 항목 이름을 조건의 원재료 순번으로 바꾼다. 같은 종류가 하나뿐이면 그 항목으로 간주한다."""
    same_kind = [i for i, it in enumerate(items) if it["kind"] == kind]
    n = _norm(name)
    if n:
        for i in same_kind:
            m = _norm(items[i]["name"])
            if n == m or n in m or m in n:
                return i
    return same_kind[0] if len(same_kind) == 1 else None


def build_plan(
    intent: str,
    conditions: dict,
    draft: list[dict] | None = None,
    reason: str | None = None,
) -> dict:
    """첫 계획. draft가 없으면 규칙만으로 만든다."""
    b = _Builder()
    plan = b.plan
    plan.update(
        {
            "source": "llm" if draft is not None else "rule",
            "draft": draft,
            "draft_reason": reason,
            "corrections": [],
        }
    )

    def fix(message: str):
        if draft is not None:
            plan["corrections"].append(message)

    items = conditions.get("items") or []
    base, current = conditions.get("base_month"), conditions.get("current_month")
    need_index = intent in ("plan", "fact")
    need_law = intent in ("plan", "eligibility")

    def eligible(i: int) -> bool:
        share = items[i].get("share_pct")
        return not (intent == "plan" and share is not None and share < LEGAL_SHARE_PCT)

    for i, item in enumerate(items):
        if need_index and not eligible(i):
            plan["skipped"].append(
                {
                    "item": i,
                    "tool": "지수 조회",
                    "reason": f"{item['name']} 비중 {item['share_pct']:g}%가 법정 기준 {LEGAL_SHARE_PCT:g}%에 못 미쳐 변동률을 계산하지 않습니다.",
                }
            )

    search_of: dict[int, str] = {}
    index_done: set[int] = set()
    power_done: set[int] = set()
    law_done: set[str] = set()

    def add_search(i: int, keyword: str | None = None, purpose: str | None = None):
        lock: tuple[str, ...] = ()
        if conditions.get("index_name") and len(items) == 1:
            # 약정서의 지표 이름은 사용자 조건이므로 그대로 쓰고 Worker도 바꾸지 못하게 고정한다
            keyword, lock = conditions["index_name"], ("keyword",)
        search_of[i] = b.add(
            "search_material_item",
            {"keyword": (keyword or "").strip() or items[i]["name"]},
            role="search",
            item=i,
            round_no=0,
            purpose=purpose or f"{items[i]['name']}의 ECOS 품목 코드 후보",
            lock=lock,
        )

    def add_index(i: int, purpose: str | None = None):
        if i not in search_of:
            fix(
                f"{items[i]['name']}: 상세 조회에 필요한 품목 코드를 얻도록 후보 조회를 앞에 더했습니다."
            )
            add_search(i)
        b.add(
            "get_material_price_index",
            {
                "base_month": base,
                "current_month": current,
                "include_trend": intent == "plan" or None,
            },
            args_from={"item_code": f"{search_of[i]}.candidates[0].item_code"},
            role="index",
            item=i,
            candidate=0,
            round_no=0,
            purpose=purpose or f"{items[i]['name']} 기준 시점 대비 변동률",
        )
        index_done.add(i)

    def add_power(i: int, purpose: str | None = None):
        b.add(
            "get_industrial_power_price",
            {
                "base_month": base,
                "current_month": current,
                "region": conditions.get("region"),
                "city": conditions.get("city"),
            },
            role="power",
            item=i,
            round_no=0,
            purpose=purpose or f"{items[i]['name']} 기준 시점 대비 변동률",
        )
        power_done.add(i)

    def add_law(keyword: str, purpose: str | None = None):
        if keyword in law_done:
            return
        b.add(
            "search_law_article",
            {"law_name": LAW_NAME, "keyword": keyword},
            role="law",
            round_no=0,
            purpose=purpose or f"{keyword}의 법정 기준 조문",
        )
        law_done.add(keyword)

    # 1) LLM 초안을 검증한다
    for d in draft or []:
        tool = d.get("tool")
        role = ROLE_OF.get(tool)
        if role is None:
            fix(f"{tool}: 이 요청에 쓸 수 없는 Tool이라 뺐습니다.")
            continue
        if role == "law":
            if not need_law:
                fix(
                    f"{tool}: {INTENT_LABEL[intent]}에는 근거 조문이 필요 없어 뺐습니다."
                )
                continue
            add_law(
                (d.get("keyword") or "").strip() or LAW_KEYWORD["material"],
                d.get("purpose"),
            )
            continue
        if not need_index:
            fix(
                f"{tool}: {INTENT_LABEL[intent]}에는 지수, 요금 조회가 필요 없어 뺐습니다."
            )
            continue
        i = _match_item(
            d.get("item") or d.get("keyword"),
            items,
            "energy" if role == "power" else "material",
        )
        if i is None:
            fix(
                f"{tool}: 조건에 없는 항목({d.get('item') or d.get('keyword')})이라 뺐습니다."
            )
            continue
        if not eligible(i):
            fix(f"{tool}: {items[i]['name']} 비중이 법정 기준에 못 미쳐 뺐습니다.")
            continue
        if role == "search" and i not in search_of:
            add_search(i, d.get("keyword"), d.get("purpose"))
        elif role == "index" and i not in index_done:
            add_index(i, d.get("purpose"))
        elif role == "power" and i not in power_done:
            add_power(i, d.get("purpose"))

    # 2) 빠진 필수 단계를 보충한다
    if need_index:
        for i, item in enumerate(items):
            if not eligible(i):
                continue
            if item["kind"] == "energy" and i not in power_done:
                fix(f"{item['name']}: 변동률 계산에 필요한 전기요금 조회를 더했습니다.")
                add_power(i)
            elif item["kind"] == "material" and i not in index_done:
                fix(f"{item['name']}: 변동률 계산에 필요한 상세 조회를 더했습니다.")
                add_index(i)
    if need_law:
        # 판단에 사용하는 법정 비중 기준(제2조 12, 12의2)의 근거 조문은 반드시 확보한다. LLM이 선택한 다른 검색은 유지한다
        for kind in ("material", "energy"):
            if (
                any(it["kind"] == kind for it in items)
                and LAW_KEYWORD[kind] not in law_done
            ):
                fix(
                    f"법정 비중 기준의 근거 조문을 얻도록 '{LAW_KEYWORD[kind]}' 조문 검색을 더했습니다."
                )
                add_law(LAW_KEYWORD[kind])

    plan["rounds"].append(
        {
            "round": 0,
            "strategy": "initial",
            "reason": reason or "첫 계획",
            "steps": [s["id"] for s in plan["steps"]],
        }
    )
    return plan


# ── 재계획 (F3009) ────────────────────────────────────────


def _alternatives(
    plan: dict, step_results: dict, judgments: list[dict]
) -> list[tuple[int, str, int]]:
    """(item, 검색 단계 id, 다음 후보 번호) 목록. 지정한 지표가 이름으로 정확히 맞으면 다른 후보를 사용하지 않는다."""
    out = []
    for j in judgments:
        if j["status"] not in ("unsatisfied", "undetermined") or j.get("share_failed"):
            continue
        i = j["item"]
        searches = [
            s
            for s in plan["steps"]
            if s["role"] == "search"
            and s.get("item") == i
            and not s.get("discarded")
            and step_results.get(s["id"], {}).get("ok")
        ]
        if not searches:
            continue
        search = searches[-1]  # Worker 요청으로 다시 찾았으면 마지막으로 성공한 검색
        candidates = step_results[search["id"]]["data"].get("candidates", [])
        index_steps = [
            s
            for s in plan["steps"]
            if s["role"] == "index"
            and s.get("item") == i
            and not s.get("discarded")
            and s.get("args_from", {}).get("item_code", "").startswith(search["id"] + ".")
        ]
        tried = [s["candidate"] for s in index_steps]
        first_ok = bool(index_steps) and step_results.get(index_steps[0]["id"], {}).get(
            "ok"
        )
        exact = bool(candidates) and _norm(candidates[0]["item_name"]) == _norm(
            search["args"]["keyword"]
        )
        if exact and first_ok:
            continue
        nxt = next(
            (
                k
                for k in range(len(candidates))
                if k not in tried and candidates[k].get("has_as_of")
            ),
            None,
        )
        if nxt is not None:
            out.append((i, search["id"], nxt))
    return out


def choose_strategy(state: dict) -> dict | None:
    """충족하는 후보가 하나도 없을 때 사용할 재계획 방법을 선택한다. 사용할 방법이 없으면 None을 반환한다."""
    plan, judgments = state["plan"], state.get("judgments", [])
    used = {r["strategy"] for r in plan["rounds"]}
    intent = state.get("intent")

    if intent == "plan" and _alternatives(
        plan, state.get("step_results", {}), judgments
    ):
        names = ", ".join(
            sorted({j["item_name"] for j in judgments if j["status"] != "satisfied"})
        )
        return {
            "strategy": "next_candidate",
            "reason": f"{names}: 첫 후보 지표로는 조정 요건을 충족하지 못해 다른 후보 지표로 계산합니다.",
        }
    if (
        intent == "plan"
        and "recheck" not in used
        and any(j.get("rate_failed") and j.get("trend") for j in judgments)
    ):
        return {
            "strategy": "recheck",
            "reason": "변동률이 약정 비율에 못 미쳐, 지금 요청하는 계획 대신 다음 지수 발표 때 다시 조회하는 계획으로 바꿉니다.",
        }
    if "alt_path" not in used and any(j["status"] == "unsatisfied" for j in judgments):
        return {
            "strategy": "alt_path",
            "reason": "연동 요건을 충족하지 못해, 연동이 아닌 공급원가 변동에 따른 조정 신청 근거를 찾습니다.",
        }
    return None


TOOL_LABEL = {
    "search_material_item": "원재료 품목 검색",
    "get_material_price_index": "생산자물가지수 조회",
    "get_industrial_power_price": "산업용 전기요금 조회",
    "search_law_article": "법령 조문 검색",
}


def request_actions(state: dict) -> tuple[list[dict], list[str], list[str]]:
    """Worker가 해결하지 못하고 올린 요청을 Planner가 규칙으로 처리한다 (F3021).

    (새로 더할 작업, 사용자에게 확인할 사항, 처리한 요청의 단계 id) 를 반환한다.
        - 원재료 품목 검색 실패: 아직 쓰지 않은 사용자의 원재료 이름으로 다시 찾는다.
          모두 썼으면 약정서의 기준 지표 이름을 되묻는다.
        - 근거 조문 검색 실패: 아직 쓰지 않은 법정 기준 검색어로 찾는다. 없으면 그 검색어를 뺀다.
        - 그 밖의 실패(지역, 기준월 등 사용자 조건으로 정한 인자): 조건을 확인하도록 되묻는다.
    """
    plan, conditions = state["plan"], state["conditions"]
    items = conditions.get("items") or []
    steps = {s["id"]: s for s in plan["steps"]}
    handled = set(state.get("handled_requests", []))
    actions: list[dict] = []
    notes: list[str] = []
    done: list[str] = []
    for r in state.get("worker_requests", []):
        if r["step"] in handled or r["step"] not in steps:
            continue
        step = steps[r["step"]]
        done.append(r["step"])
        tried = {
            _norm(a.get("keyword"))
            for a in r.get("tried", [])
            if a.get("keyword") is not None
        }
        if step["role"] == "search" and step.get("item") is not None:
            name = items[step["item"]]["name"]
            used = tried | {
                _norm(s["args"].get("keyword"))
                for s in plan["steps"]
                if s["role"] == "search" and s.get("item") == step["item"]
            }
            if not conditions.get("index_name") and _norm(name) not in used:
                actions.append({"kind": "search", "item": step["item"], "keyword": name})
            else:
                notes.append(
                    f"{name}: 한국은행 생산자물가지수에서 일치하는 품목을 찾지 못했습니다. "
                    "약정서에 적힌 기준 지표 이름을 알려 주시면 그 이름으로 다시 찾겠습니다."
                )
        elif step["role"] == "law":
            used = tried | {
                _norm(s["args"].get("keyword"))
                for s in plan["steps"]
                if s["role"] == "law"
            }
            kinds = {it["kind"] for it in items} or {"material"}
            spare = [
                LAW_KEYWORD[k]
                for k in ("material", "energy")
                if k in kinds and _norm(LAW_KEYWORD[k]) not in used
            ]
            if spare:
                actions.append({"kind": "law", "keyword": spare[0]})
        else:
            notes.append(
                f"{TOOL_LABEL.get(step['tool'], step['tool'])}: {r['reason']} "
                "지역, 기준 시점 같은 조건이 맞는지 확인해 주세요."
            )
    return actions, notes, done


def suspect_steps(plan: dict, verification: dict | None) -> list[str]:
    """로깅 Agent가 고정 인자 변경으로 지적한 단계와, 그 결과를 입력으로 사용한 단계를 반환한다 (F3021)."""
    if not verification or verification.get("verdict") != "anomaly":
        return []
    flagged = {
        i["step"]
        for c in verification.get("checks", [])
        if c["check"] == "fixed_arg_changed" and c["status"] == "warn"
        for i in c["items"]
    }
    ids = {s["id"] for s in plan["steps"]}
    out = [s for s in flagged if s in ids]
    for s in plan["steps"]:
        refs = {ref.split(".")[0] for ref in s.get("args_from", {}).values()}
        if refs & set(out) and s["id"] not in out:
            out.append(s["id"])
    return out


def apply_replan(state: dict, directive: dict) -> dict:
    """계획에 재계획 단계를 추가한다. 이미 완료된 단계는 유지해 결과를 재사용한다."""
    plan = {
        **state["plan"],
        "steps": list(state["plan"]["steps"]),
        "rounds": list(state["plan"]["rounds"]),
    }
    b = _Builder(plan)
    round_no = state.get("replan_count", 0) + 1
    before = len(plan["steps"])
    conditions = state["conditions"]

    if directive["strategy"] == "next_candidate":
        for i, sid, k in _alternatives(
            plan, state.get("step_results", {}), state.get("judgments", [])
        ):
            name = conditions["items"][i]["name"]
            b.add(
                "get_material_price_index",
                {
                    "base_month": conditions.get("base_month"),
                    "current_month": conditions.get("current_month"),
                    "include_trend": True,
                },
                args_from={"item_code": f"{sid}.candidates[{k}].item_code"},
                role="index",
                item=i,
                candidate=k,
                round_no=round_no,
                purpose=f"{name}의 {k + 1}번째 후보 지표 변동률",
            )
    elif directive["strategy"] == "alt_path":
        b.add(
            "search_law_article",
            {
                "law_name": LAW_NAME,
                "keyword": LAW_KEYWORD["alt_path"],
                "max_results": 1,
            },
            role="law",
            round_no=round_no,
            purpose="연동이 아닌 조정 신청 근거 조문",
        )
    elif directive["strategy"] == "worker_request":
        for a in directive.get("actions", []):
            if a["kind"] == "search":
                i, name = a["item"], conditions["items"][a["item"]]["name"]
                sid = b.add(
                    "search_material_item",
                    {"keyword": a["keyword"]},
                    role="search",
                    item=i,
                    round_no=round_no,
                    purpose=f"{name}의 ECOS 품목 코드 후보 (사용자가 말한 이름으로 다시 검색)",
                )
                if state.get("intent") in ("plan", "fact"):
                    b.add(
                        "get_material_price_index",
                        {
                            "base_month": conditions.get("base_month"),
                            "current_month": conditions.get("current_month"),
                            "include_trend": state.get("intent") == "plan" or None,
                        },
                        args_from={"item_code": f"{sid}.candidates[0].item_code"},
                        role="index",
                        item=i,
                        candidate=0,
                        round_no=round_no,
                        purpose=f"{name} 기준 시점 대비 변동률",
                    )
            elif a["kind"] == "law":
                b.add(
                    "search_law_article",
                    {"law_name": LAW_NAME, "keyword": a["keyword"]},
                    role="law",
                    round_no=round_no,
                    purpose=f"{a['keyword']}의 법정 기준 조문",
                )
    elif directive["strategy"] == "verify_fix":
        # 검증에서 지적된 작업은 Planner가 정한 인자 그대로 다시 수행한다. Worker는 이 작업을 고치지 않는다
        original = {s["id"]: s for s in plan["steps"]}
        plan["steps"] = [
            {**s, "discarded": True} if s["id"] in directive.get("steps", []) else s
            for s in plan["steps"]
        ]
        renamed: dict[str, str] = {}
        for old in directive.get("steps", []):
            s = original[old]
            args_from = {
                k: (renamed.get(v.split(".")[0], v.split(".")[0]) + v[v.index(".") :])
                for k, v in s.get("args_from", {}).items()
            }
            renamed[old] = b.add(
                s["tool"],
                s["args"],
                role=s["role"],
                purpose=s["purpose"] + " (검증 후 다시 수행)",
                round_no=round_no,
                args_from=args_from or None,
                item=s.get("item"),
                candidate=s.get("candidate"),
                lock=tuple(s.get("fixed", [])),
                no_retry=True,
            )
    # recheck는 Tool을 새로 호출하지 않는다. 이미 받은 추세(trend)로 도달 시점을 추정한다.

    new_ids = [s["id"] for s in plan["steps"][before:]]
    reused = [
        s["id"]
        for s in plan["steps"][:before]
        if s["id"] in state.get("step_results", {})
    ]
    plan["rounds"].append(
        {
            "round": round_no,
            "strategy": directive["strategy"],
            "reason": directive["reason"],
            "steps": new_ids,
            "reused": reused,
        }
    )
    return plan
