"""로깅 Agent 이상 징후 판정, 진단 단위 테스트 (F3020). LLM과 외부 API를 호출하지 않는다.

python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import copy
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("PLANNER_DB_PATH", str(Path(_TMP.name) / "default.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.agent.logging_agent import LoggingAgent  # noqa: E402
from app.agent.reviewer import evaluate_run, limit_sentences  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from test_agent import FakeTools, analysis, representative  # noqa: E402
from test_logging import collect, make_agent  # noqa: E402


def renumber(events: list[dict], run_id: str = "r-x") -> list[dict]:
    out = copy.deepcopy(events)
    for i, e in enumerate(out, 1):
        e["seq"], e["run_id"] = i, run_id
    return out


def real_run(responses=None, tools=None) -> list[dict]:
    bus = EventBus()
    events = collect(bus)
    make_agent(responses or [representative()], tools, bus).run("s1", "요청")
    return events


def index_of(events, type_, **match) -> int:
    for i, e in enumerate(events):
        if e["type"] == type_ and all(
            e["data"].get(k) == v or e.get(k) == v for k, v in match.items()
        ):
            return i
    raise LookupError(type_)


def with_repeated_call(events: list[dict]) -> list[dict]:
    """s2 지수 조회 요청, 응답을 한 번 더 넣고 실행 단계의 호출 수를 맞춘다."""
    out = copy.deepcopy(events)
    i = index_of(out, "tool_response", step="s2")
    out[i + 1 : i + 1] = copy.deepcopy(out[i - 1 : i + 1])
    out[index_of(out, "node_end", node="execute")]["data"]["called"] += 1
    return renumber(out)


def with_unplanned_call(events: list[dict]) -> list[dict]:
    out = copy.deepcopy(events)
    i = index_of(out, "node_end", node="execute")
    out[i:i] = [
        {
            **out[i],
            "type": "tool_request",
            "node": "execute",
            "data": {
                "step": "s9",
                "round": 0,
                "tool": "get_industrial_power_price",
                "args": {"region": "대구"},
            },
        },
        {
            **out[i],
            "type": "tool_response",
            "node": "execute",
            "data": {
                "step": "s9",
                "round": 0,
                "tool": "get_industrial_power_price",
                "ok": True,
                "code": None,
                "elapsed_ms": 1,
                "result": {},
            },
        },
    ]
    out[i + 2]["data"]["called"] += 1
    return renumber(out)


class FakeDiagnoser:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def __call__(self, review, events):
        self.calls += 1
        if self.fail:
            raise RuntimeError("OPENAI_API_KEY=secret 응답 없음")
        return {
            "agent": "main",
            "stage": "execute",
            "text": "실행기가 같은 인자를 다시 호출했습니다. 캐시 조회 전에 호출했습니다. 캐시 키를 확인하세요. 네 번째 문장입니다.",
        }


class EvaluateTest(unittest.TestCase):
    """규칙 판정"""

    def test_normal_run(self):
        review = evaluate_run(real_run())
        self.assertEqual(review["verdict"], "normal")
        self.assertEqual(review["warnings"], 0)
        self.assertEqual({c["status"] for c in review["checks"]}, {"ok"})

    def test_chat_run_is_normal(self):
        review = evaluate_run(real_run([analysis("chat")]))
        self.assertEqual(review["verdict"], "normal")

    def test_repeated_call(self):
        review = evaluate_run(with_repeated_call(real_run()))
        self.assertEqual(review["verdict"], "anomaly")
        check = review["checks"][0]
        self.assertEqual((check["check"], check["status"]), ("repeated_call", "warn"))
        self.assertEqual(check["items"][0]["tool"], "get_material_price_index")
        self.assertEqual(check["items"][0]["count"], 2)
        self.assertEqual(review["warnings"], 1)

    def test_cache_reuse_is_not_repeated_call(self):
        tools = FakeTools(index={"307125AA": (130.0, 132.0)})
        events = real_run(tools=tools)
        reused = copy.deepcopy(next(e for e in events if e["type"] == "tool_request"))
        reused["type"] = "tool_reused"
        events.insert(index_of(events, "node_end", node="execute"), reused)
        self.assertEqual(evaluate_run(renumber(events))["checks"][0]["status"], "ok")

    def test_excessive_replan_at_limit(self):
        events = real_run(
            [
                representative(
                    items=[{"name": "철판", "kind": "material", "share_pct": 40}],
                    threshold_pct=30.0,
                )
            ],
            FakeTools(
                index={"A": (100.0, 101.0), "B": (100.0, 102.0)},
                candidates=[("A", "열연강판"), ("B", "중후판")],
            ),
        )
        review = evaluate_run(events)
        check = review["checks"][1]
        self.assertEqual(check["status"], "warn", check["message"])
        self.assertEqual(check["items"][0]["reason"], "limit")
        self.assertEqual(review["verdict"], "anomaly")

    def test_same_strategy_twice(self):
        events = real_run(tools=FakeTools(index={"307125AA": (130.0, 132.0)}))
        i = index_of(events, "decision", kind="replan")
        extra = copy.deepcopy(events[i])
        events = events[: i + 1] + [extra] + events[i + 1 :]
        events[-1] = copy.deepcopy(events[-1])
        events[-1]["data"]["replan_count"] += 1
        check = evaluate_run(renumber(events))["checks"][1]
        self.assertEqual(check["status"], "warn")
        self.assertIn("same_strategy", [it["reason"] for it in check["items"]])

    def test_unplanned_call(self):
        review = evaluate_run(with_unplanned_call(real_run()))
        check = review["checks"][2]
        self.assertEqual(check["status"], "warn")
        self.assertEqual(check["items"][0]["step"], "s9")
        self.assertEqual(review["checks"][0]["status"], "ok")

    def test_undetermined_without_evidence(self):
        events = real_run()
        self.assertEqual(evaluate_run([])["verdict"], "undetermined")
        self.assertEqual(
            evaluate_run(events[:-1])["verdict"], "undetermined"
        )  # 종료 이벤트 없음
        gap = events[:5] + events[6:]
        self.assertEqual(evaluate_run(gap)["verdict"], "undetermined")  # 빠진 번호
        no_plan = renumber(
            [e for e in events if not (e["type"] == "node_end" and e["node"] == "plan")]
        )
        review = evaluate_run(no_plan)
        self.assertEqual(review["checks"][2]["status"], "undetermined")
        self.assertEqual(review["verdict"], "undetermined")
        lost = renumber(
            [
                e
                for e in events
                if not (e["type"] == "tool_request" and e["data"]["step"] == "s3")
            ]
        )
        self.assertEqual(evaluate_run(lost)["checks"][0]["status"], "undetermined")

    def test_warn_wins_over_undetermined(self):
        events = with_repeated_call(real_run())
        events = renumber(
            [e for e in events if not (e["type"] == "node_end" and e["node"] == "plan")]
        )
        self.assertEqual(evaluate_run(events)["verdict"], "anomaly")

    def test_limit_sentences(self):
        text = "변동률은 8.99%입니다. 둘째 문장. 셋째 문장! 넷째 문장."
        self.assertEqual(
            limit_sentences(text), "변동률은 8.99%입니다. 둘째 문장. 셋째 문장!"
        )


class ReviewAgentTest(unittest.TestCase):
    """로깅 Agent의 판정 저장, 진단 조건"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = RunLogStore(Path(self._dir.name) / "planner.db")
        self.bus = EventBus()

    def tearDown(self):
        self._dir.cleanup()

    def logger(self, diagnoser) -> LoggingAgent:
        agent = LoggingAgent(self.store, diagnoser=diagnoser, diagnose_async=False)
        agent.start(self.bus)
        return agent

    def publish(self, events):
        for e in events:
            self.bus.publish({**e, "session_id": "s-x"})

    def test_normal_run_does_not_call_llm(self):
        diagnoser = FakeDiagnoser()
        self.logger(diagnoser)
        response = make_agent([representative()], bus=self.bus).run("s1", "요청")
        reviews = self.store.list_reviews("s1")
        self.assertEqual(reviews[0]["verdict"], "normal")
        self.assertIsNone(reviews[0]["diagnosis"])
        self.assertEqual(diagnoser.calls, 0)
        self.assertIsNone(response["review"])  # review_source가 없으면 참조하지 않는다

    def test_anomaly_gets_separate_diagnosis(self):
        diagnoser = FakeDiagnoser()
        self.logger(diagnoser)
        self.publish(with_repeated_call(real_run()))
        review = self.store.get_review("r-x")
        self.assertEqual(diagnoser.calls, 1)
        self.assertEqual(review["verdict"], "anomaly")
        self.assertEqual(review["warnings"], 1)  # 진단이 경고 수에 섞이지 않는다
        self.assertEqual(review["diagnosis"]["status"], "ok")
        self.assertEqual(review["diagnosis"]["agent"], "main")
        self.assertEqual(review["diagnosis"]["text"].count("."), 3)  # 세 문장 이내

    def test_undetermined_does_not_call_llm(self):
        diagnoser = FakeDiagnoser()
        logger = self.logger(diagnoser)
        events = real_run()
        self.publish(renumber(events[:-1]))
        logger.review("s-x", "r-x")
        self.assertEqual(self.store.get_review("r-x")["verdict"], "undetermined")
        self.assertEqual(diagnoser.calls, 0)

    def test_diagnosis_failure_keeps_review_and_log(self):
        self.logger(FakeDiagnoser(fail=True))
        events = with_repeated_call(real_run())
        self.publish(events)
        review = self.store.get_review("r-x")
        self.assertEqual(review["verdict"], "anomaly")
        self.assertEqual(review["diagnosis"]["status"], "failed")
        self.assertIsNone(review["diagnosis"]["text"])  # 오류 원문을 남기지 않는다
        self.assertEqual(len(self.store.get_run("r-x")), len(events))

    def test_async_diagnosis(self):
        diagnoser = FakeDiagnoser()
        logger = LoggingAgent(self.store, diagnoser=diagnoser)
        logger.start(self.bus)
        self.publish(with_unplanned_call(real_run()))
        logger.wait(5)
        self.assertEqual(self.store.get_review("r-x")["diagnosis"]["status"], "ok")

    def test_review_is_read_only(self):
        self.logger(FakeDiagnoser())
        self.publish(with_repeated_call(real_run()))
        db = sqlite3.connect(self.store.path)
        try:
            for table in ("run_reviews", "run_diagnoses"):
                with self.assertRaises(sqlite3.DatabaseError):
                    db.execute(f"UPDATE {table} SET run_id = 'x'")
        finally:
            db.close()


class MainAgentReferenceTest(unittest.TestCase):
    """판정을 메인 Agent가 참조하고, API로도 조회한다."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.store = RunLogStore(Path(self._dir.name) / "planner.db")

    def tearDown(self):
        self._dir.cleanup()

    def test_agent_response_carries_review(self):
        agent = make_agent([representative()])
        app = create_app(self.store, agent)
        app.state.logging_agent._async = False
        response = agent.run("s1", "요청")
        self.assertEqual(response["review"]["verdict"], "normal")
        self.assertEqual(len(response["review"]["checks"]), 4)

        client = TestClient(app)
        detail = client.get("/api/logs/s1").json()
        self.assertEqual(detail["reviews"][0]["run_id"], response["review"]["run_id"])
        self.assertEqual(client.get("/api/logs").json()["sessions"][0]["anomalies"], 0)
        client.delete("/api/logs/s1")
        self.assertEqual(self.store.list_reviews("s1"), [])

    def test_agent_without_logging_agent(self):
        agent = make_agent([representative()])
        agent.review_source = lambda s, r: self.store.get_review(r)
        response = agent.run("s1", "요청")
        self.assertEqual(response["kind"], "result")
        self.assertIsNone(response["review"])


if __name__ == "__main__":
    unittest.main()
