"""백엔드 API 서버 단위 테스트 (F3017, F3019). LLM과 외부 API를 호출하지 않는다.

python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import json
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

from fastapi.testclient import TestClient  # noqa: E402

from app.api.system import KEYS  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from test_agent import FakeTools, analysis, representative  # noqa: E402
from test_logging import make_agent  # noqa: E402


class ServerTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._dir.cleanup()

    def client(self, responses, tools=None) -> TestClient:
        self.tools = tools or FakeTools()
        self.agent = make_agent(responses, self.tools)
        app = create_app(RunLogStore(Path(self._dir.name) / "planner.db"), self.agent)
        return TestClient(app)

    def stream(self, client: TestClient, body: dict) -> list[dict]:
        r = client.post("/api/chat/stream", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("application/x-ndjson"))
        return [json.loads(line) for line in r.text.splitlines() if line.strip()]

    # ── F3017: 경로 분리와 형식 ──

    def test_routes_are_separated(self):
        paths = set(self.client([]).get("/openapi.json").json()["paths"])
        for path in (
            "/api/chat",
            "/api/chat/stream",
            "/api/plans",
            "/api/plans/{plan_id}",
            "/api/logs",
            "/api/logs/{session_id}",
            "/api/status",
        ):
            self.assertIn(path, paths)
        # 실행 로그 수정 API는 두지 않는다 (F3014)
        spec = (
            self.client([])
            .get("/openapi.json")
            .json()["paths"]["/api/logs/{session_id}"]
        )
        self.assertNotIn("put", spec)
        self.assertNotIn("patch", spec)

    def test_chat_returns_response_and_keeps_session(self):
        client = self.client([representative(), analysis("chat")])
        first = client.post("/api/chat", json={"message": "대표 요청"}).json()
        self.assertEqual(first["kind"], "result")
        self.assertTrue(first["session_id"] and first["run_id"])
        self.assertTrue(first["plan_drafts"])
        self.assertTrue(first["evidence"])
        second = client.post(
            "/api/chat", json={"session_id": first["session_id"], "message": "고마워"}
        ).json()
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["kind"], "chat")
        # 같은 세션의 실행 로그에 두 요청이 남는다
        log = client.get(f"/api/logs/{first['session_id']}").json()
        self.assertEqual(len({e["run_id"] for e in log["events"]}), 2)

    def test_chat_rejects_empty_message(self):
        r = self.client([]).post("/api/chat", json={"message": ""})
        self.assertEqual(r.status_code, 422)

    # ── F3019: 실행 과정 스트림 ──

    def test_stream_sends_events_in_order_then_response(self):
        lines = self.stream(self.client([representative()]), {"message": "대표 요청"})
        self.assertEqual(lines[0]["type"], "session")
        self.assertEqual(lines[-1]["type"], "response")
        events = [line["event"] for line in lines[1:-1]]
        self.assertTrue(all(line["type"] == "event" for line in lines[1:-1]))
        self.assertEqual(events[0]["type"], "run_start")
        self.assertEqual(events[-1]["type"], "run_end")
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        nodes = [e["node"] for e in events if e["type"] == "node_start"]
        self.assertEqual(nodes, ["analyze", "plan", "execute", "judge", "respond"])
        self.assertTrue(any(e["type"] == "tool_request" for e in events))
        self.assertEqual(lines[-1]["response"]["session_id"], lines[0]["session_id"])

    def test_stream_marks_replan(self):
        tools = FakeTools(index={"307125AA": (130.0, 132.0)})
        lines = self.stream(
            self.client([representative()], tools), {"message": "재계획"}
        )
        events = [line["event"] for line in lines if line["type"] == "event"]
        replans = [
            e
            for e in events
            if e["type"] == "decision" and e["data"]["kind"] == "replan"
        ]
        self.assertTrue(replans)
        self.assertTrue(lines[-1]["response"]["replans"])
        # 재계획 결정 뒤에 다시 계획 단계가 시작된다
        first = events.index(replans[0])
        self.assertEqual(
            next(e["node"] for e in events[first:] if e["type"] == "node_start"), "plan"
        )

    def test_stream_reports_tool_failure(self):
        tools = FakeTools(fail_tools={"search_law_article": "INVALID_KEY"})
        lines = self.stream(self.client([representative()], tools), {"message": "실패"})
        failed = [
            line["event"]
            for line in lines
            if line["type"] == "event"
            and line["event"]["type"] == "tool_response"
            and not line["event"]["data"]["ok"]
        ]
        self.assertEqual([e["data"]["code"] for e in failed], ["INVALID_KEY"])
        response = lines[-1]["response"]
        self.assertIn("INVALID_KEY", response["fatal"])
        self.assertTrue(any(not e["ok"] for e in response["evidence"]))

    def test_key_failure_does_not_ask_to_change_conditions(self):
        tools = FakeTools(
            fail_tools={
                "search_material_item": "INVALID_KEY",
                "search_law_article": "INVALID_KEY",
            }
        )
        r = self.client([representative()], tools).post(
            "/api/chat", json={"message": "키 없음"}
        ).json()
        self.assertIn("INVALID_KEY", r["fatal"])
        self.assertNotIn("알려 주시면 다시 계산", r["text"])

    def test_agent_crash_returns_error_without_detail(self):
        client = self.client([representative()])
        with mock.patch.object(
            self.agent, "run", side_effect=RuntimeError("secret-key-12345 at http://x")
        ):
            r = client.post("/api/chat", json={"message": "대표 요청"})
            lines = self.stream(client, {"message": "대표 요청"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["kind"], "error")
        self.assertNotIn("secret", r.text)
        self.assertEqual(lines[-1]["response"]["kind"], "error")
        self.assertNotIn("secret", json.dumps(lines))

    # ── F3019: API 키 누락 안내 (NF3002) ──

    def test_status_lists_missing_keys_without_values(self):
        env = {name: "" for name in KEYS} | {"OPENAI_API_KEY": "sk-should-not-leak"}
        with mock.patch.dict(os.environ, env):
            client = self.client([analysis("chat")])
            status = client.get("/api/status")
            chat = client.post("/api/chat", json={"message": "안녕"}).json()
        body = status.json()
        self.assertFalse(body["ready"])
        configured = {k["env"]: k["configured"] for k in body["keys"]}
        self.assertTrue(configured["OPENAI_API_KEY"])
        self.assertFalse(configured["ECOS_OPEN_API"])
        self.assertNotIn("sk-should-not-leak", status.text)
        self.assertEqual(
            sorted(chat["missing_keys"]), sorted(n for n in KEYS if n != "OPENAI_API_KEY")
        )

    # ── F3018: 화면 제공 ──

    def test_frontend_is_served(self):
        client = self.client([])
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("단가조정 플래너", page.text)
        for path, mime in (
            ("/js/main.js", "text/javascript"),
            ("/js/trace.js", "text/javascript"),
            ("/css/app.css", "text/css"),
        ):
            r = client.get(path)
            self.assertEqual(r.status_code, 200, path)
            # 모듈 스크립트는 JavaScript MIME 형식이어야 브라우저가 실행한다
            self.assertTrue(r.headers["content-type"].startswith(mime), path)


if __name__ == "__main__":
    unittest.main()
