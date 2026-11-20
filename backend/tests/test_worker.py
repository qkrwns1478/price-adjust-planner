"""Planner·Worker 구조 단위 테스트 (F3021). LLM과 외부 API를 호출하지 않는다.

    python -m unittest discover -s backend/tests -v

실제 LLM, API를 사용하는 확인은 backend/check_worker.py가 담당한다.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("PLANNER_DB_PATH", str(Path(_TMP.name) / "default.db"))

from app.agent import PlannerAgent  # noqa: E402
from app.agent.logging_agent import LoggingAgent  # noqa: E402
from app.agent.planner import DraftStep, PlanDraft, build_plan  # noqa: E402
from app.agent.reviewer import verify_round  # noqa: E402
from app.agent.state import merge_conditions  # noqa: E402
from app.agent.worker import ArgChange, WorkerDecision, adjustable_args  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from app.tools.base import fail  # noqa: E402
from test_agent import FakeTools, representative, rule_selector  # noqa: E402
from test_logging import collect, of  # noqa: E402

PP = [{"name": "폴리프로필렌", "kind": "material", "share_pct": 30}]


class NamedTools(FakeTools):
    """search_material_item이 known에 있는 검색어만 찾고, 나머지는 NO_DATA로 응답한다."""

    def __init__(self, known=("합성수지",), **kw):
        super().__init__(
            index={"301AA": (110.0, 125.0)}, candidates=[("301AA", "합성수지")], **kw
        )
        self.known = set(known)

    def __call__(self, name, args):
        if name == "search_material_item" and args["keyword"] not in self.known:
            self.calls.append((name, args))
            return fail(
                "NO_DATA", f"'{args['keyword']}'와 일치하는 ECOS 품목이 없습니다."
            )
        return super().__call__(name, args)


class FakeDecider:
    """Worker LLM 대신 정해 둔 판단을 차례로 돌려준다."""

    def __init__(self, *decisions: WorkerDecision):
        self.decisions = list(decisions)
        self.calls: list[dict] = []

    def __call__(self, task, args, error, adjustable, tried):
        self.calls.append(
            {"task": task["id"], "args": args, "adjustable": adjustable, "tried": tried}
        )
        if not self.decisions:
            return WorkerDecision(action="replan", reason="더 고칠 방법이 없음")
        return self.decisions.pop(0)


def retry(reason="상위 품목명으로 다시 검색", **args) -> WorkerDecision:
    return WorkerDecision(
        action="retry",
        args=[ArgChange(name=k, value=str(v)) for k, v in args.items()],
        reason=reason,
    )


def keyword_selector(keyword):
    """LLM 계획 초안이 품목 검색어를 keyword로 고른 것처럼 만든다."""

    def select(intent, conditions, message):
        draft = rule_selector(intent, conditions, message)
        steps = [
            (
                DraftStep(**{**s.model_dump(), "keyword": keyword})
                if s.tool == "search_material_item"
                else s
            )
            for s in draft.steps
        ]
        return PlanDraft(steps=steps, reason=draft.reason)

    return select


def make(
    tools,
    decider=None,
    bus=None,
    selector=rule_selector,
    responses=None,
    **kw,
) -> PlannerAgent:
    queue = list(responses or [representative(items=PP)])
    return PlannerAgent(
        analyzer=lambda m, p, h: queue.pop(0),
        chatter=lambda m, h: "안녕하세요.",
        selector=selector,
        tool_caller=tools,
        bus=bus,
        worker_decider=decider,
        **kw,
    )


def messages(events, kind):
    return [e["data"] for e in of(events, "agent_message", kind=kind)]


class FixedArgsTest(unittest.TestCase):
    def test_planner_marks_fixed_args(self):
        cond, _ = merge_conditions({}, representative())
        steps = {s["tool"]: s for s in build_plan("plan", cond)["steps"]}
        self.assertEqual(
            adjustable_args(steps["search_material_item"]),
            ["keyword", "as_of", "max_results"],
        )
        self.assertEqual(
            steps["get_material_price_index"]["fixed"],
            ["base_month", "include_trend", "item_code"],
        )
        self.assertEqual(
            adjustable_args(steps["get_material_price_index"]), ["current_month"]
        )
        self.assertEqual(
            adjustable_args(steps["search_law_article"]), ["keyword", "max_results"]
        )

    def test_index_name_locks_keyword(self):
        cond, _ = merge_conditions({}, representative(index_name="열연강판"))
        search = build_plan("plan", cond)["steps"][0]
        self.assertIn("keyword", search["fixed"])
        self.assertNotIn("keyword", adjustable_args(search))


class WorkerTest(unittest.TestCase):
    def test_messages_and_no_llm_on_normal_path(self):
        bus = EventBus()
        events = collect(bus)
        decider = FakeDecider()
        r = make(FakeTools(), decider, bus, responses=[representative()]).run(
            "s", "대표"
        )
        self.assertEqual(r["kind"], "result")
        self.assertEqual(decider.calls, [])
        tasks = messages(events, "task_list")[0]
        self.assertEqual((tasks["sender"], tasks["receiver"]), ("planner", "worker"))
        self.assertEqual([t["id"] for t in tasks["body"]["tasks"]], ["s1", "s2", "s3"])
        self.assertIn("fixed", tasks["body"]["tasks"][1])
        report = messages(events, "work_report")[0]
        self.assertEqual((report["sender"], report["receiver"]), ("worker", "planner"))
        self.assertEqual({t["status"] for t in report["body"]["tasks"]}, {"ok"})
        self.assertEqual(report["body"]["requests"], [])

    def test_retry_fixes_keyword_and_keeps_fixed_args(self):
        bus = EventBus()
        events = collect(bus)
        tools = NamedTools()
        # LLM이 고정 인자(as_of가 아닌 base_month)까지 바꾸라고 해도 Worker는 고정 인자를 지킨다
        decider = FakeDecider(retry(keyword="합성수지"))
        agent = make(tools, decider, bus)
        r = agent.run("s", "폴리프로필렌")
        self.assertEqual(len(decider.calls), 1)
        self.assertEqual(
            decider.calls[0]["adjustable"], ["keyword", "as_of", "max_results"]
        )
        searches = [a["keyword"] for n, a in tools.calls if n == "search_material_item"]
        self.assertEqual(searches, ["폴리프로필렌", "합성수지"])
        self.assertEqual(r["recommendations"][0]["index"], "합성수지(301AA)")
        self.assertEqual(agent.state("s")["replan_count"], 0)
        retried = of(events, "tool_request", attempt=1)
        self.assertEqual(retried[0]["data"]["step"], "s1")
        llm = of(events, "decision", kind="worker", by="llm")
        self.assertEqual(llm[0]["data"]["choice"], "retry")
        self.assertIn("합성수지", r["text"])

    def test_guard_ignores_fixed_arg_change(self):
        tools = FakeTools()
        bad = WorkerDecision(
            action="retry",
            args=[ArgChange(name="base_month", value="202601")],
            reason="기준월 변경",
        )
        tools.fail_tools = {"search_law_article": "NO_DATA"}
        decider = FakeDecider(bad)
        bus = EventBus()
        events = collect(bus)
        make(tools, decider, bus, responses=[representative()]).run("s", "x")
        d = of(events, "decision", kind="worker", by="llm")[0]["data"]
        self.assertEqual((d["args"], d["ignored"]), ({}, ["base_month"]))
        # 고칠 인자가 없으므로 다시 부르지 않고 Planner에 보고한다
        self.assertTrue(
            of(events, "decision", kind="worker", by="rule", choice="replan")
        )
        self.assertFalse(of(events, "tool_request", attempt=1))

    def test_fatal_and_missing_required_do_not_call_llm(self):
        decider = FakeDecider(retry(keyword="x"))
        tools = FakeTools(fail_tools={"search_material_item": "INVALID_KEY"})
        r = make(tools, decider, responses=[representative()]).run("s", "키 없음")
        self.assertEqual(decider.calls, [])
        self.assertEqual(r["fatal"], ["INVALID_KEY"])

        tools = FakeTools(fail_tools={"search_law_article": "BAD_ARGUMENT"})
        agent = make(tools, decider, responses=[representative()])
        with mock.patch("app.agent.worker._missing_required", return_value=["keyword"]):
            agent.run("s2", "필수 인자 누락")
        self.assertEqual(decider.calls, [])

    def test_retry_limit(self):
        decider = FakeDecider(*(retry(keyword=f"후보{i}") for i in range(5)))
        tools = NamedTools(known=())
        with mock.patch.dict(os.environ, {"WORKER_MAX_RETRY": "2"}):
            make(tools, decider).run("s", "x")
        first = [c for c in decider.calls if c["task"] == "s1"]
        self.assertEqual(len(first), 2)  # 원래 호출 1 + 재시도 2 → 판단은 2번

    def test_worker_llm_off_reports_to_planner(self):
        bus = EventBus()
        events = collect(bus)
        decider = FakeDecider(retry(keyword="합성수지"))
        tools = NamedTools()
        with mock.patch.dict(os.environ, {"WORKER_LLM": "off"}):
            agent = make(tools, decider, bus, selector=keyword_selector("PP"))
            r = agent.run("s", "PP")
        self.assertEqual(decider.calls, [])
        report = messages(events, "work_report")[0]["body"]
        self.assertEqual(report["requests"][0]["by"], "rule")
        # Planner는 사용자가 말한 이름(폴리프로필렌)으로 다시 찾는 작업을 더한다
        rounds = agent.state("s")["plan"]["rounds"]
        self.assertEqual(rounds[1]["strategy"], "worker_request")
        searches = [a["keyword"] for n, a in tools.calls if n == "search_material_item"]
        self.assertEqual(searches, ["PP", "폴리프로필렌"])
        self.assertTrue(
            r["planner_notes"]
        )  # 그래도 찾지 못해 기준 지표 이름을 되묻는다

    def test_worker_replan_request_reaches_planner(self):
        decider = FakeDecider(
            WorkerDecision(action="replan", reason="검색어를 고쳐도 찾을 수 없음")
        )
        tools = NamedTools(known=("폴리프로필렌", "합성수지"))
        agent = make(tools, decider, selector=keyword_selector("PP수지"))
        r = agent.run("s", "PP")
        st = agent.state("s")
        self.assertEqual(st["worker_requests"][0]["by"], "llm")
        self.assertEqual(st["plan"]["rounds"][1]["strategy"], "worker_request")
        self.assertEqual(r["recommendations"][0]["item"], "폴리프로필렌")

    def test_skip_drops_task(self):
        decider = FakeDecider(
            WorkerDecision(action="skip", reason="조문 없이도 답할 수 있음")
        )
        tools = FakeTools(fail_tools={"search_law_article": "NO_DATA"})
        bus = EventBus()
        events = collect(bus)
        make(tools, decider, bus, responses=[representative()]).run("s", "x")
        report = messages(events, "work_report")[0]["body"]
        self.assertIn("dropped", {t["status"] for t in report["tasks"]})
        self.assertEqual(report["requests"], [])


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunLogStore(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.tmp.cleanup()

    def wired(self, tools, decider, **kw):
        bus = EventBus()
        events = collect(bus)
        logger = LoggingAgent(
            self.store,
            diagnoser=lambda r, e: {"agent": "worker", "stage": "execute", "text": "x"},
            diagnose_async=False,
        )
        logger.start(bus)
        agent = make(tools, decider, bus, verifier=logger.verification, **kw)
        return agent, events

    def test_normal_run_is_verified(self):
        agent, events = self.wired(
            FakeTools(), FakeDecider(), responses=[representative()]
        )
        agent.run("s", "대표")
        v = messages(events, "verification")[0]
        self.assertEqual((v["sender"], v["receiver"]), ("logging", "planner"))
        self.assertEqual(v["body"]["verdict"], "normal")

    def test_fixed_change_is_caught_and_replanned(self):
        # 고정 인자 보호를 끈 Worker가 LLM 말대로 약정서 지표 이름(고정 인자)을 바꿔 다시 부른 상황
        from app.agent.worker import Worker

        tools = NamedTools(known=("합성수지",))
        decider = FakeDecider(retry(keyword="합성수지"))
        bad = Worker(tools, decider=decider, guard_fixed=False)
        agent, events = self.wired(
            tools,
            None,
            worker=bad,
            responses=[representative(items=PP, index_name="폴리프로필렌")],
        )
        r = agent.run("s", "약정서 지표는 폴리프로필렌")
        st = agent.state("s")
        v = messages(events, "verification")[0]["body"]
        self.assertEqual(v["verdict"], "anomaly")
        fixed = next(c for c in v["checks"] if c["check"] == "fixed_arg_changed")
        self.assertEqual(fixed["items"][0]["step"], "s1")
        self.assertEqual(st["plan"]["rounds"][1]["strategy"], "verify_fix")
        self.assertTrue(st["plan"]["steps"][0]["discarded"])
        # 다시 수행한 작업은 Planner가 정한 검색어 그대로이며 Worker가 고치지 않는다
        redo = [s for s in st["plan"]["steps"] if s["round"] == 1]
        self.assertEqual(redo[0]["args"]["keyword"], "폴리프로필렌")
        self.assertTrue(redo[0]["no_retry"])
        # 기준 지표를 찾지 못했으므로 합성수지 결과로 추천하지 않는다
        self.assertEqual(r["recommendations"], [])
        # 요청 종료 판정(F3020)에도 고정 인자 변경이 남는다
        review = self.store.get_review(r["run_id"])
        self.assertEqual(review["verdict"], "anomaly")

    def test_verify_round_without_task_list_is_undetermined(self):
        events = [
            {"seq": 1, "type": "run_start", "node": None, "data": {}},
            {
                "seq": 2,
                "type": "node_end",
                "node": "plan",
                "data": {"round": 0, "steps": [{"id": "s1", "tool": "t"}]},
            },
            {"seq": 3, "type": "node_start", "node": "execute", "data": {}},
            {
                "seq": 4,
                "type": "tool_request",
                "node": "execute",
                "data": {"step": "s1", "tool": "t", "args": {}},
            },
            {"seq": 5, "type": "node_end", "node": "execute", "data": {"called": 1}},
        ]
        v = verify_round(events)
        self.assertEqual(v["verdict"], "undetermined")


if __name__ == "__main__":
    unittest.main()
