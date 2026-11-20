"""계획 관리 단위 테스트 (F3015, F3016). LLM과 외부 API를 호출하지 않는다.

python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("PLANNER_DB_PATH", str(Path(_TMP.name) / "default.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from test_agent import FakeTools, analysis, representative  # noqa: E402
from test_logging import make_agent  # noqa: E402


def body(**over) -> dict:
    return {
        "client": "A사",
        "product": "프레임",
        "item": "열연강판",
        "share_pct": 35,
        "unit_price_won": 120000,
        "change_rate_pct": 15.29,
        "threshold_pct": 5,
        "increase_won": 6422,
        "new_price_won": 126422,
        "law_refs": ["상생협력법 제16조의2(납품대금의 연동)"],
        "meeting_date": "2026-11-25",
        "start_time": "14:00",
        "end_time": "15:00",
    } | over


class PlanApiTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.agent = make_agent([representative()] * 3)
        self.app = create_app(
            RunLogStore(Path(self._dir.name) / "planner.db"), self.agent
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        self._dir.cleanup()

    def save(self, **over) -> dict:
        r = self.client.post("/api/plans", json=body(**over))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    # ── F3015 ──

    def test_save_and_read_core_values(self):
        saved = self.save()
        plan = saved["plan"]
        self.assertEqual(saved["overlaps"], [])
        self.assertEqual(saved["duplicates"], [])
        for key in (
            "client",
            "item",
            "meeting_date",
            "start_time",
            "end_time",
            "increase_won",
        ):
            self.assertEqual(plan[key], body()[key])
        self.assertEqual(plan["status"], "ready")
        self.assertTrue(plan["created_at"])

        listed = self.client.get("/api/plans").json()["plans"]
        self.assertEqual([p["id"] for p in listed], [plan["id"]])
        self.assertEqual(listed[0]["created_at"], plan["created_at"])
        detail = self.client.get(f"/api/plans/{plan['id']}").json()
        self.assertEqual(detail, plan)
        self.assertEqual(self.client.get("/api/plans/999").status_code, 404)

    def test_overlap_is_saved_and_reported(self):
        first = self.save()["plan"]
        second = self.save(client="B사", start_time="14:30", end_time="15:30")
        self.assertEqual([p["id"] for p in second["overlaps"]], [first["id"]])
        # 끝나는 시각에 시작하는 계획, 다른 날짜의 계획은 겹치지 않는다
        self.assertEqual(
            self.save(client="C사", start_time="15:30", end_time="16:00")["overlaps"],
            [],
        )
        self.assertEqual(
            self.save(client="D사", meeting_date="2026-11-26")["overlaps"], []
        )
        self.assertEqual(len(self.client.get("/api/plans").json()["plans"]), 4)

    def test_duplicate_is_saved_and_reported(self):
        first = self.save()["plan"]
        dup = self.save(meeting_date="2026-12-02")
        self.assertEqual([p["id"] for p in dup["duplicates"]], [first["id"]])
        self.assertEqual(
            self.save(item="형강", meeting_date="2026-12-03")["duplicates"], []
        )

    def test_list_is_ordered_by_meeting(self):
        self.save(meeting_date="2026-12-01")
        self.save(
            client="B사",
            meeting_date="2026-11-20",
            start_time="09:00",
            end_time="10:00",
        )
        dates = [
            p["meeting_date"] for p in self.client.get("/api/plans").json()["plans"]
        ]
        self.assertEqual(dates, ["2026-11-20", "2026-12-01"])

    def test_invalid_values_are_rejected(self):
        for over in (
            {"meeting_date": None},
            {"meeting_date": "2026-13-01"},
            {"start_time": "25:00"},
            {"start_time": "15:00", "end_time": "14:00"},
            {"client": ""},
        ):
            self.assertEqual(
                self.client.post("/api/plans", json=body(**over)).status_code, 422, over
            )

    # ── F3016 ──

    def test_update_core_values(self):
        plan = self.save()["plan"]
        r = self.client.patch(
            f"/api/plans/{plan['id']}",
            json={
                "meeting_date": "2026-12-01",
                "start_time": "10:00",
                "end_time": "11:00",
                "status": "requested",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        updated = r.json()["plan"]
        self.assertEqual(
            (updated["meeting_date"], updated["start_time"], updated["status"]),
            ("2026-12-01", "10:00", "requested"),
        )
        self.assertEqual(updated["client"], "A사")  # 보내지 않은 값은 그대로
        self.assertEqual(updated["created_at"], plan["created_at"])
        self.assertEqual(self.client.get("/api/plans").json()["plans"][0], updated)

    def test_update_reports_overlap(self):
        a = self.save()["plan"]
        b = self.save(client="B사", start_time="16:00", end_time="17:00")["plan"]
        r = self.client.patch(
            f"/api/plans/{b['id']}", json={"start_time": "14:30"}
        ).json()
        self.assertEqual([p["id"] for p in r["overlaps"]], [a["id"]])

    def test_update_rejects_invalid(self):
        plan = self.save()["plan"]
        url = f"/api/plans/{plan['id']}"
        self.assertEqual(
            self.client.patch(url, json={"end_time": "13:00"}).status_code, 422
        )
        self.assertEqual(
            self.client.patch(url, json={"meeting_date": None}).status_code, 422
        )
        self.assertEqual(
            self.client.patch(url, json={"status": "done"}).status_code, 422
        )
        self.assertEqual(
            self.client.patch("/api/plans/999", json={"memo": "x"}).status_code, 404
        )
        self.assertEqual(self.client.get(url).json()["end_time"], "15:00")

    def test_delete(self):
        a = self.save()["plan"]
        b = self.save(client="B사")["plan"]
        r = self.client.delete(f"/api/plans/{a['id']}")
        self.assertEqual(r.json(), {"id": a["id"], "deleted": True})
        self.assertEqual(
            [p["id"] for p in self.client.get("/api/plans").json()["plans"]], [b["id"]]
        )
        self.assertEqual(self.client.delete(f"/api/plans/{a['id']}").status_code, 404)


class PlanDraftTest(unittest.TestCase):
    """Agent 응답의 저장용 초안이 판단 결과의 값을 그대로 옮기고, 협의 일정은 비워 두는지 확인한다."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._dir.cleanup()

    def test_draft_from_result_can_be_saved(self):
        agent = make_agent([representative(product="프레임")])
        r = agent.run("s1", "대표 요청")
        self.assertEqual(len(r["plan_drafts"]), 1)
        draft = r["plan_drafts"][0]
        rec = r["recommendations"][0]
        self.assertEqual(
            (draft["client"], draft["product"], draft["item"]),
            ("A사", "프레임", "열연강판"),
        )
        self.assertEqual(draft["index_code"], "307125AA")
        self.assertEqual(draft["change_rate_pct"], rec["rate_pct"])
        self.assertEqual(draft["increase_won"], rec["amount"]["increase_won"])
        self.assertTrue(draft["law_refs"])
        self.assertIsNone(draft["meeting_date"])
        self.assertEqual((draft["session_id"], draft["run_id"]), ("s1", r["run_id"]))

        client = TestClient(
            create_app(RunLogStore(Path(self._dir.name) / "p.db"), agent)
        )
        saved = client.post(
            "/api/plans",
            json=draft
            | {
                "meeting_date": "2026-11-25",
                "start_time": "14:00",
                "end_time": "15:00",
            },
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["plan"]["run_id"], r["run_id"])

    def test_no_draft_without_satisfied_item(self):
        tools = FakeTools(index={"307125AA": (130.0, 132.0)})
        r = make_agent([representative()], tools).run("s1", "미충족")
        self.assertEqual(r["plan_drafts"], [])
        r = make_agent([analysis(intent="chat", items_mode="keep")]).run("s2", "안녕")
        self.assertEqual(r["plan_drafts"], [])

    def test_manage_intent_points_to_plan_screen(self):
        r = make_agent([analysis(intent="manage", items_mode="keep")]).run(
            "s1", "저장해 줘"
        )
        self.assertEqual(r["kind"], "notice")
        self.assertIn("계획 관리 화면", r["text"])


if __name__ == "__main__":
    unittest.main()
