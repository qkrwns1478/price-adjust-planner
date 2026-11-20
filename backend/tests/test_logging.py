"""실행 이벤트, 로깅 Agent, 실행 로그 API 단위 테스트 (F3012 ~ F3014). LLM과 외부 API를 호출하지 않는다.

    python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = tempfile.TemporaryDirectory()
os.environ["PLANNER_DB_PATH"] = str(Path(_TMP.name) / "default.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import PlannerAgent  # noqa: E402
from app.agent.logging_agent import LoggingAgent  # noqa: E402
from app.events import EventBus, EventType  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from test_agent import FakeTools, analysis, representative, rule_selector  # noqa: E402

NODES = ["analyze", "plan", "execute", "judge", "respond"]


def make_agent(responses, tools=None, bus=None) -> PlannerAgent:
    queue = list(responses)
    return PlannerAgent(
        analyzer=lambda m, p, h: queue.pop(0),
        chatter=lambda m, h: "안녕하세요.",
        selector=rule_selector,
        tool_caller=tools or FakeTools(),
        bus=bus,
    )


def collect(bus: EventBus) -> list[dict]:
    events: list[dict] = []
    bus.subscribe(events.append)
    return events


def of(events, type_, **match):
    return [
        e
        for e in events
        if e["type"] == type_
        and all(e["data"].get(k) == v or e.get(k) == v for k, v in match.items())
    ]


class TempStoreMixin:
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = RunLogStore(Path(self._dir.name) / "planner.db")

    def tearDown(self):
        self._dir.cleanup()


class EventTest(unittest.TestCase):
    """F3012 실행 이벤트 발행"""

    def test_stage_tool_and_decision_events(self):
        bus = EventBus()
        events = collect(bus)
        agent = make_agent([representative()], bus=bus)
        agent.run("s1", "대표 요청")

        self.assertEqual(events[0]["type"], EventType.RUN_START)
        self.assertEqual(events[-1]["type"], EventType.RUN_END)
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(e["session_id"] == "s1" for e in events))
        self.assertEqual(len({e["run_id"] for e in events}), 1)

        starts = [e["node"] for e in of(events, EventType.NODE_START)]
        ends = [e["node"] for e in of(events, EventType.NODE_END)]
        self.assertEqual(starts, NODES)
        self.assertEqual(ends, NODES)
        self.assertTrue(all("elapsed_ms" in e["data"] for e in of(events, EventType.NODE_END)))

        requests = of(events, EventType.TOOL_REQUEST)
        responses = of(events, EventType.TOOL_RESPONSE)
        self.assertEqual(
            [e["data"]["tool"] for e in requests],
            ["search_material_item", "get_material_price_index", "search_law_article"],
        )
        self.assertEqual(
            [e["data"]["step"] for e in requests], [e["data"]["step"] for e in responses]
        )
        self.assertTrue(all(isinstance(e["data"]["elapsed_ms"], int) for e in responses))
        self.assertEqual(
            requests[1]["data"]["args"]["item_code"], "307125AA"
        )  # 앞 Tool 결과가 입력으로 이어진 사실이 남는다

        analyze_end = of(events, EventType.NODE_END, node="analyze")[0]["data"]
        self.assertEqual(analyze_end["intent"], "plan")
        self.assertEqual(analyze_end["intent_reason"], "테스트")
        decisions = of(events, EventType.DECISION, by="llm")
        self.assertEqual([d["data"]["kind"] for d in decisions], ["intent", "plan"])
        self.assertTrue(all(d["data"]["reason"] for d in decisions))

    def test_replan_and_reuse_events(self):
        bus = EventBus()
        events = collect(bus)
        tools = FakeTools(index={"307125AA": (130.0, 132.0)})
        make_agent([representative()], tools, bus).run("s1", "재계획 요청")

        replans = of(events, EventType.DECISION, kind="replan")
        self.assertTrue(replans)
        self.assertTrue(all(r["data"]["by"] == "rule" and r["data"]["reason"] for r in replans))
        plan_rounds = [e["data"]["round"] for e in of(events, EventType.NODE_END, node="plan")]
        self.assertEqual(plan_rounds, list(range(len(replans) + 1)))
        # 재계획에서 이미 호출한 Tool은 다시 요청하지 않는다
        keys = [
            (e["data"]["tool"], str(sorted(e["data"]["args"].items())))
            for e in of(events, EventType.TOOL_REQUEST)
        ]
        self.assertEqual(len(keys), len(set(keys)))

    def test_clarify_decision_skips_tools(self):
        bus = EventBus()
        events = collect(bus)
        make_agent(
            [analysis(items=[{"name": "철판", "kind": "material"}])], bus=bus
        ).run("s1", "철판 요청")
        self.assertEqual(of(events, EventType.DECISION, kind="clarify")[0]["data"]["by"], "rule")
        self.assertFalse(of(events, EventType.TOOL_REQUEST))
        self.assertEqual([e["node"] for e in of(events, EventType.NODE_START)], ["analyze", "respond"])

    def test_tool_failure_event(self):
        bus = EventBus()
        events = collect(bus)
        tools = FakeTools(fail_tools={"search_law_article": "UPSTREAM_ERROR"})
        make_agent([representative()], tools, bus).run("s1", "요청")
        failed = of(events, EventType.TOOL_RESPONSE, tool="search_law_article")[0]["data"]
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["code"], "UPSTREAM_ERROR")

    def test_publish_does_not_depend_on_subscribers(self):
        alone = make_agent([representative()]).run("s1", "요청")
        bus = EventBus()
        bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("구독자 오류")))
        broken = make_agent([representative()], bus=bus).run("s1", "요청")
        self.assertEqual(alone["text"], broken["text"])


class LoggingAgentTest(TempStoreMixin, unittest.TestCase):
    """F3013 로깅 Agent"""

    def test_stores_raw_events_by_session_in_order(self):
        bus = EventBus()
        published = collect(bus)
        LoggingAgent(self.store).start(bus)
        agent = make_agent([representative(), representative(), analysis("chat")], bus=bus)
        agent.run("a", "첫 요청")
        agent.run("b", "다른 세션")
        agent.run("a", "안녕")

        a = self.store.get_session("a")
        b = self.store.get_session("b")
        self.assertTrue(all(e["session_id"] == "a" for e in a))
        self.assertTrue(all(e["session_id"] == "b" for e in b))
        self.assertEqual(len({e["run_id"] for e in a}), 2)
        self.assertEqual(len(a) + len(b), len(published))
        expected = [e for e in published if e["session_id"] == "a"]
        self.assertEqual(
            [{k: e[k] for k in ("session_id", "run_id", "seq", "type", "node", "ts", "data")} for e in a],
            expected,
        )

    def test_analyze_failure_is_not_reviewed_as_normal(self):
        bus = EventBus()
        published = collect(bus)
        logger = LoggingAgent(self.store)
        logger.start(bus)

        def down(*_):
            raise RuntimeError("429")

        PlannerAgent(analyzer=down, selector=rule_selector, bus=bus).run("s1", "요청")
        node = next(e for e in published if e["type"] == EventType.NODE_END and e["node"] == "analyze")
        end = next(e for e in published if e["type"] == EventType.RUN_END)
        self.assertFalse(node["data"]["ok"])
        self.assertFalse(end["data"]["ok"])
        self.assertEqual(self.store.get_review(end["run_id"])["verdict"], "failed")

    def test_does_not_call_llm(self):
        bus = EventBus()
        LoggingAgent(self.store).start(bus)
        with mock.patch(
            "app.agent.config.chat_model", side_effect=AssertionError("LLM 호출")
        ), mock.patch(
            "langchain_openai.ChatOpenAI.__init__", side_effect=AssertionError("LLM 호출")
        ):
            make_agent([representative()], bus=bus).run("s1", "요청")
        self.assertTrue(self.store.get_session("s1"))
        source = Path(sys.modules[LoggingAgent.__module__].__file__).read_text("utf-8")
        self.assertNotIn("chat_model", source)

    def test_store_failure_does_not_break_chat(self):
        bus = EventBus()
        logger = LoggingAgent(self.store)
        logger.start(bus)
        with mock.patch.object(self.store, "append", side_effect=sqlite3.OperationalError("disk")):
            response = make_agent([representative()], bus=bus).run("s1", "요청")
        self.assertEqual(response["kind"], "result")
        self.assertGreater(logger.failures, 0)

    def test_stopped_logger_does_not_affect_agent(self):
        bus = EventBus()
        logger = LoggingAgent(self.store)
        logger.start(bus)
        agent = make_agent([representative(), representative()], bus=bus)
        agent.run("s1", "요청")
        logger.stop()
        response = agent.run("s2", "요청")
        self.assertEqual(response["kind"], "result")
        self.assertIsNone(self.store.get_session("s2"))

    def test_log_is_read_only(self):
        bus = EventBus()
        LoggingAgent(self.store).start(bus)
        make_agent([representative()], bus=bus).run("s1", "요청")
        self.assertFalse(hasattr(self.store, "update"))
        db = sqlite3.connect(self.store.path)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute("UPDATE run_events SET type = 'x'")
        finally:
            db.close()


class LogApiTest(TempStoreMixin, unittest.TestCase):
    """F3014 실행 로그 목록, 상세, 삭제"""

    def setUp(self):
        super().setUp()
        self.agent = make_agent([representative(), analysis("chat")])
        self.app = create_app(self.store, self.agent)
        self.client = TestClient(self.app)
        self.agent.run("a", "대표 요청")
        self.agent.run("b", "안녕")

    def test_list_detail_delete(self):
        sessions = self.client.get("/api/logs").json()["sessions"]
        self.assertEqual([s["session_id"] for s in sessions], ["b", "a"])
        a = next(s for s in sessions if s["session_id"] == "a")
        self.assertEqual((a["runs"], a["tool_calls"], a["first_message"]), (1, 3, "대표 요청"))

        detail = self.client.get("/api/logs/a").json()
        ids = [e["id"] for e in detail["events"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(detail["events"][0]["type"], "run_start")
        self.assertEqual(detail["events"][-1]["type"], "run_end")

        deleted = self.client.delete("/api/logs/a").json()
        self.assertEqual(deleted["deleted"], len(ids))
        self.assertEqual(self.client.get("/api/logs/a").status_code, 404)
        self.assertEqual(self.client.delete("/api/logs/a").status_code, 404)
        self.assertEqual([s["session_id"] for s in self.client.get("/api/logs").json()["sessions"]], ["b"])

    def test_no_update_api(self):
        paths = self.app.openapi()["paths"]
        methods = {m for p, ops in paths.items() if p.startswith("/api/logs") for m in ops}
        self.assertEqual(methods, {"get", "delete"})
        self.assertEqual(self.client.put("/api/logs/a", json={}).status_code, 405)
        self.assertEqual(self.client.patch("/api/logs/a", json={}).status_code, 405)


if __name__ == "__main__":
    unittest.main()
