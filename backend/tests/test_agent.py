"""메인 Agent 단위 테스트. LLM과 외부 API를 호출하지 않고 분석 결과와 Tool 응답을 모의한다.

    python -m unittest discover -s backend/tests -v

실제 LLM, API를 사용하는 확인은 backend/check_agent.py가 담당한다.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import PlannerAgent  # noqa: E402
from app.agent.analyzer import find_missing  # noqa: E402
from app.agent.executor import run_pending  # noqa: E402
from app.agent.planner import DraftStep, PlanDraft, build_plan  # noqa: E402
from app.agent.state import (
    Analysis,
    ConditionUpdate,
    CostItem,
    merge_conditions,
)  # noqa: E402
from app.tools.base import fail, ok  # noqa: E402

BASE = {
    "client": "A사",
    "unit_price_won": 120000,
    "base_month": "202512",
    "threshold_pct": 5.0,
    "cycle": "분기",
}


def analysis(intent="plan", items_mode="replace", items=None, **update) -> Analysis:
    return Analysis(
        intent=intent,
        intent_reason="테스트",
        items_mode=items_mode,
        update=ConditionUpdate(
            items=[CostItem(**i) for i in items] if items else None, **update
        ),
    )


def representative(**over) -> Analysis:
    fields = {**BASE, **over}
    items = fields.pop(
        "items", [{"name": "열연강판", "kind": "material", "share_pct": 35}]
    )
    return analysis(items=items, **fields)


class FakeTools:
    """Tool 이름별 응답을 반환하고 호출을 기록한다."""

    def __init__(
        self, index: dict | None = None, fail_tools: dict | None = None, candidates=None
    ):
        self.calls: list[tuple[str, dict]] = []
        self.index = index or {"307125AA": (130.58, 150.55)}
        self.fail_tools = fail_tools or {}
        self.candidates = candidates or [("307125AA", "열연강판")]

    def __call__(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        if name in self.fail_tools:
            return fail(self.fail_tools[name], "모의 실패")
        if name == "search_material_item":
            return ok(
                {
                    "keyword": args["keyword"],
                    "as_of": "202609",
                    "candidates": [
                        {"item_code": c, "item_name": n, "has_as_of": True}
                        for c, n in self.candidates
                    ],
                }
            )
        if name == "get_material_price_index":
            b, c = self.index[args["item_code"]]
            if b is None:
                return fail("NO_INDEX_VALUE", "기준월 지수가 비어 있습니다.")
            name_ = dict(self.candidates)[args["item_code"]]
            return ok(
                {
                    "item_code": args["item_code"],
                    "item_name": name_,
                    "unit": "2020=100",
                    "base": {"month": args["base_month"], "value": b},
                    "current": {"month": "202609", "value": c},
                    "change_rate_pct": round((c / b - 1) * 100, 2),
                    "estimated": False,
                    "note": None,
                    "trend": [
                        {"month": f"20260{m}", "value": c - (9 - m)}
                        for m in range(4, 10)
                    ],
                }
            )
        if name == "search_law_article":
            return ok(
                {
                    "law": {
                        "name": "대ㆍ중소기업 상생협력 촉진에 관한 법률",
                        "abbr": "상생협력법",
                    },
                    "articles": [
                        {
                            "article_no": (
                                "제2조"
                                if args["keyword"] != "납품대금 조정"
                                else "제22조의2"
                            ),
                            "title": "정의",
                            "excerpt": "주요 원재료",
                            "link": "https://www.law.go.kr/x",
                        }
                    ],
                }
            )
        return fail("NOT_FOUND", "모의에 없는 Tool")

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


def rule_selector(intent: str, conditions: dict, message: str) -> PlanDraft:
    """LLM 대신 규칙 계획을 초안으로 반환한다."""
    items = conditions.get("items") or []
    steps = [
        DraftStep(
            tool=s["tool"],
            item=items[s["item"]]["name"] if "item" in s else None,
            keyword=s["args"].get("keyword"),
            purpose=s["purpose"],
        )
        for s in build_plan(intent, conditions)["steps"]
    ]
    return PlanDraft(steps=steps, reason="규칙 초안")


def agent_with(
    responses: list[Analysis], tools: FakeTools, selector=rule_selector
) -> PlannerAgent:
    queue = list(responses)
    return PlannerAgent(
        analyzer=lambda m, p, h: queue.pop(0),
        chatter=lambda m, h: "안녕하세요.",
        selector=selector,
        tool_caller=tools,
    )


class ConditionTest(unittest.TestCase):
    def test_follow_up_updates_only_changed(self):
        first, _ = merge_conditions({}, representative())
        merged, changed = merge_conditions(
            first, analysis(items=[{"name": "알루미늄", "kind": "material"}])
        )
        self.assertEqual(changed, ["items"])
        self.assertEqual(
            merged["items"], [{"name": "알루미늄", "kind": "material", "share_pct": 35}]
        )
        self.assertEqual(
            {k: v for k, v in merged.items() if k != "items"},
            {k: v for k, v in first.items() if k != "items"},
        )

    def test_generic_item_name_keeps_previous_item(self):
        first, _ = merge_conditions({}, representative())
        merged, changed = merge_conditions(
            first, analysis(items=[{"name": "원재료", "kind": "material", "share_pct": 10}])
        )
        self.assertEqual(changed, ["items"])
        self.assertEqual(
            merged["items"], [{"name": first["items"][0]["name"], "kind": "material", "share_pct": 10}]
        )

    def test_missing_is_not_filled(self):
        conditions, _ = merge_conditions(
            {}, analysis(items=[{"name": "철판", "kind": "material"}], client="D사")
        )
        missing = [m["field"] for m in find_missing("plan", conditions)]
        self.assertEqual(missing, ["share_pct:철판", "base_month", "threshold_pct"])
        self.assertIsNone(conditions["threshold_pct"])


class PlanTest(unittest.TestCase):
    def test_plan_excludes_unneeded_tools(self):
        cond, _ = merge_conditions(
            {},
            representative(
                items=[
                    {"name": "열연강판", "kind": "material", "share_pct": 35},
                    {"name": "알루미늄", "kind": "material", "share_pct": 8},
                ]
            ),
        )
        plan = build_plan("plan", cond)
        self.assertEqual(
            [s["tool"] for s in plan["steps"]],
            ["search_material_item", "get_material_price_index", "search_law_article"],
        )
        self.assertEqual(plan["skipped"][0]["item"], 1)

    def test_llm_draft_is_validated(self):
        cond, _ = merge_conditions({}, representative(region="대구"))
        draft = [
            {
                "tool": "get_material_price_index",
                "item": "열연강판",
                "keyword": None,
                "purpose": "변동률",
            },
            {
                "tool": "get_industrial_power_price",
                "item": "전기료",
                "keyword": None,
                "purpose": "지어낸 항목",
            },
            {"tool": "no_such_tool", "item": None, "keyword": None, "purpose": "x"},
        ]
        plan = build_plan("plan", cond, draft, "초안")
        self.assertEqual(plan["source"], "llm")
        self.assertEqual(
            [s["tool"] for s in plan["steps"]],
            ["search_material_item", "get_material_price_index", "search_law_article"],
        )
        self.assertEqual(plan["steps"][1]["args"]["base_month"], "202512")
        self.assertEqual(
            len(plan["corrections"]), 4
        )  # 후보 조회 앞에 추가, 존재하지 않는 항목, 등록되지 않은 Tool, 근거 조문 추가

    def test_legal_basis_law_is_kept(self):
        cond, _ = merge_conditions({}, representative())
        draft = [
            {
                "tool": "search_material_item",
                "item": "열연강판",
                "keyword": "열연강판",
                "purpose": "x",
            },
            {
                "tool": "get_material_price_index",
                "item": "열연강판",
                "keyword": None,
                "purpose": "x",
            },
            {
                "tool": "search_law_article",
                "item": None,
                "keyword": "납품대금 연동",
                "purpose": "근거",
            },
        ]
        plan = build_plan("plan", cond, draft)
        self.assertEqual(
            [s["args"].get("keyword") for s in plan["steps"] if s["role"] == "law"],
            ["납품대금 연동", "주요 원재료"],
        )
        self.assertEqual(len(plan["corrections"]), 1)

    def test_eligibility_draft_drops_index(self):
        cond, _ = merge_conditions(
            {},
            analysis(
                intent="eligibility",
                items=[{"name": "알루미늄", "kind": "material", "share_pct": 8}],
            ),
        )
        draft = [
            {
                "tool": "search_material_item",
                "item": "알루미늄",
                "keyword": "알루미늄",
                "purpose": "x",
            },
            {
                "tool": "search_law_article",
                "item": None,
                "keyword": "주요 원재료",
                "purpose": "근거",
            },
        ]
        plan = build_plan("eligibility", cond, draft)
        self.assertEqual([s["tool"] for s in plan["steps"]], ["search_law_article"])

    def test_selector_failure_falls_back_to_rules(self):
        def broken(*_):
            raise RuntimeError("LLM 오류")

        tools = FakeTools()
        agent = agent_with([representative()], tools, selector=broken)
        r = agent.run("s", "대표")
        st = agent.state("s")
        self.assertEqual((st["plan"]["source"], r["kind"]), ("rule", "result"))
        self.assertTrue(st["plan"]["corrections"])

    def test_executor_passes_value_and_continues_after_failure(self):
        cond, _ = merge_conditions({}, representative())
        plan = build_plan("plan", cond)
        tools = FakeTools(fail_tools={"search_material_item": "NO_DATA"})
        results, cache, calls = run_pending(plan, {}, {}, [], tools)
        self.assertEqual([c["code"] for c in calls], ["NO_DATA", "SKIPPED", None])
        tools = FakeTools()
        results, cache, calls = run_pending(plan, {}, {}, [], tools)
        self.assertEqual(results["s2"]["args"]["item_code"], "307125AA")
        _, _, calls2 = run_pending(
            {"steps": plan["steps"] + [{**plan["steps"][0], "id": "s9"}]},
            results,
            cache,
            calls,
            tools,
        )
        self.assertTrue(calls2[-1]["cached"])
        self.assertEqual(tools.count("search_material_item"), 1)


class GraphTest(unittest.TestCase):
    def test_representative_flow(self):
        tools = FakeTools()
        agent = agent_with([representative()], tools)
        r = agent.run("s", "대표 요청")
        self.assertEqual(r["kind"], "result")
        rec = r["recommendations"][0]
        self.assertEqual(
            rec["amount"]["increase_won"], round(120000 * 0.35 * 15.29 / 100)
        )
        self.assertEqual(
            r["criteria"][0]["back_calc"]["required_value"], round(130.58 * 1.05, 2)
        )
        self.assertIn(r["disclaimer"], r["text"])
        self.assertEqual(agent.state("s")["step_results"], {})

    def test_chat_skips_plan(self):
        tools = FakeTools()
        r = agent_with([analysis(intent="chat", items_mode="keep")], tools).run(
            "s", "안녕"
        )
        self.assertEqual((r["kind"], tools.calls), ("chat", []))

    def test_clarify_before_tools(self):
        tools = FakeTools()
        r = agent_with(
            [analysis(items=[{"name": "철판", "kind": "material"}])], tools
        ).run("s", "철판 올랐어")
        self.assertEqual(r["kind"], "clarify")
        self.assertEqual(tools.calls, [])

    def test_replan_reuses_results(self):
        tools = FakeTools(index={"307125AA": (138.13, 150.55)})
        agent = agent_with([representative(threshold_pct=10.0)], tools)
        r = agent.run("s", "재계획")
        st = agent.state("s")
        self.assertEqual(
            [x["strategy"] for x in st["plan"]["rounds"]],
            ["initial", "recheck", "alt_path"],
        )
        self.assertEqual(
            (
                tools.count("search_material_item"),
                tools.count("get_material_price_index"),
            ),
            (1, 1),
        )
        self.assertTrue(r["criteria"][0]["forecast"])

    def test_replan_limit(self):
        tools = FakeTools(
            index={
                "307125AA": (138.13, 150.55),
                "307124AA": (135.35, 150.84),
                "307126AA": (140.0, 150.0),
            },
            candidates=[
                ("307125AA", "열연강판"),
                ("307124AA", "중후판"),
                ("307126AA", "냉연강판"),
            ],
        )
        with mock.patch.dict(os.environ, {"MAX_REPLAN": "2"}):
            agent = agent_with(
                [
                    representative(
                        threshold_pct=12.0,
                        items=[{"name": "철판", "kind": "material", "share_pct": 40}],
                    )
                ],
                tools,
            )
            r = agent.run("s", "상한")
            self.assertEqual(agent.state("s")["replan_count"], 2)
        self.assertIn("상한(2회)", r["stop_reason"])
        self.assertEqual(len(r["excluded"]), 3)

    def test_fatal_error_is_not_replanned(self):
        tools = FakeTools(fail_tools={"get_material_price_index": "INVALID_KEY"})
        agent = agent_with([representative()], tools)
        r = agent.run("s", "키 없음")
        self.assertEqual(agent.state("s")["replan_count"], 0)
        self.assertEqual(r["fatal"], ["INVALID_KEY"])
        self.assertNotIn("모의 실패", r["text"])

    def test_undetermined_tries_next_candidate(self):
        tools = FakeTools(
            index={"307125AA": (None, None), "307124AA": (130.0, 150.0)},
            candidates=[("307125AA", "열연강판"), ("307124AA", "중후판")],
        )
        r = agent_with([representative()], tools).run("s", "빈 값")
        self.assertEqual(r["recommendations"][0]["index"], "중후판(307124AA)")
        self.assertIn("NO_INDEX_VALUE", r["excluded"][0]["reason"])

    def test_analyzer_failure_does_not_crash(self):
        def boom(*_):
            raise RuntimeError("secret-key in url")

        r = PlannerAgent(
            analyzer=boom, selector=rule_selector, tool_caller=FakeTools()
        ).run("s", "아무 요청")
        self.assertEqual(r["kind"], "notice")
        self.assertNotIn("secret", r["text"])

    def test_follow_up_keeps_conditions(self):
        tools = FakeTools(
            index={"307125AA": (130.58, 150.55), "307222AA": (142.27, 167.05)},
            candidates=[("307125AA", "열연강판"), ("307222AA", "알루미늄1차제품")],
        )
        agent = agent_with(
            [
                representative(),
                analysis(items=[{"name": "알루미늄", "kind": "material"}]),
            ],
            tools,
        )
        agent.run("s", "대표")
        r = agent.run("s", "알루미늄으로")
        st = agent.state("s")
        self.assertEqual(r["changed"], ["items"])
        self.assertEqual(st["conditions"]["base_month"], "202512")
        self.assertEqual(len(st["history"]), 2)


if __name__ == "__main__":
    unittest.main()
