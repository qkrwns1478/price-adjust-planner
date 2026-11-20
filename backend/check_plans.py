"""계획 관리 확인 (F3015 ~ F3016): 실제 LLM(OpenAI)과 외부 API를 호출한다.

    python backend/check_plans.py

대표 요청으로 만든 계획 초안을 API로 저장하고 조회, 겹침 알림, 수정, 삭제를 확인한다.
확인용 DB는 data/logs/check_plans.db에 저장한다 (형상관리 제외). 출력에 API 키를 포함하지 않는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT.parent / ".env")

OUT = ROOT.parent / "data" / "logs"
OUT.mkdir(parents=True, exist_ok=True)
DB = OUT / "check_plans.db"
DB.unlink(missing_ok=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.agent import PlannerAgent  # noqa: E402
from app.events import EventBus  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import RunLogStore  # noqa: E402
from check_agent import REPLAN, REPRESENTATIVE  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []
CORE = (
    "client",
    "product",
    "item",
    "change_rate_pct",
    "increase_won",
    "meeting_date",
    "start_time",
    "end_time",
)


def check(title: str, good: bool, detail: str = "") -> bool:
    status = PASS if good else FAIL
    results.append((status, title))
    print(f"[{status}] {title}" + (f"\n       → {detail}" if detail else ""))
    return good


def section(t: str):
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


def brief(p: dict) -> str:
    return json.dumps(
        {k: p.get(k) for k in ("id",) + CORE + ("status", "created_at")},
        ensure_ascii=False,
    )


def main() -> int:
    agent = PlannerAgent(bus=EventBus())
    client = TestClient(create_app(RunLogStore(DB), agent))

    section("1. F3015 최종 계획 저장, 목록, 상세")
    sid = agent.new_session()
    r = agent.run(sid, REPRESENTATIVE)
    drafts = r.get("plan_drafts", [])
    print(f"  응답 요약: {r['summary']}")
    for d in drafts:
        print("  초안: " + json.dumps(d, ensure_ascii=False))
    if not check("요건을 충족한 결과에서 저장용 초안 생성", len(drafts) >= 1):
        return 1
    draft = drafts[0]
    rec = r["recommendations"][0]
    check(
        "초안의 계산 값이 응답의 판단 결과와 같고 협의 일정은 비어 있음",
        draft["change_rate_pct"] == rec["rate_pct"]
        and draft["increase_won"] == rec["amount"].get("increase_won")
        and draft["meeting_date"] is None,
        f"변동률 {draft['change_rate_pct']}%, 요청 {draft['increase_won']}원",
    )
    replan = agent.run(agent.new_session(), REPLAN)
    check(
        "요건을 충족하지 못한 결과에는 초안 없음",
        replan.get("plan_drafts") == [],
        replan["summary"],
    )

    first = client.post(
        "/api/plans",
        json=draft
        | {"meeting_date": "2026-11-25", "start_time": "14:00", "end_time": "15:00"},
    )
    saved = first.json()
    check("POST /api/plans 저장 (201)", first.status_code == 201, brief(saved["plan"]))
    plan_id = saved["plan"]["id"]
    listed = client.get("/api/plans").json()["plans"]
    detail = client.get(f"/api/plans/{plan_id}").json()
    check(
        "목록과 상세에 핵심 값과 생성 일시가 나타남",
        listed[0]["id"] == plan_id
        and all(listed[0][k] == detail[k] for k in CORE + ("created_at",))
        and bool(detail["created_at"]),
        brief(detail),
    )

    section("2. F3015 같은 날짜, 겹치는 시간")
    other = client.post(
        "/api/plans",
        json=draft
        | {
            "client": "B사",
            "meeting_date": "2026-11-25",
            "start_time": "14:30",
            "end_time": "16:00",
        },
    ).json()
    print(
        "  저장 결과: "
        + json.dumps(
            {k: other[k] for k in ("overlaps", "duplicates")}, ensure_ascii=False
        )
    )
    check(
        "저장은 되고, 겹치는 계획을 저장 결과와 함께 알림",
        other["plan"]["id"] != plan_id
        and [p["id"] for p in other["overlaps"]] == [plan_id],
    )
    later = client.post(
        "/api/plans",
        json=draft
        | {
            "client": "C사",
            "meeting_date": "2026-11-25",
            "start_time": "16:00",
            "end_time": "17:00",
        },
    ).json()
    check(
        "앞 계획이 끝나는 시각에 시작하는 계획은 겹치지 않음",
        later["overlaps"] == [],
        f"16:00 ~ 17:00, 계획 {len(client.get('/api/plans').json()['plans'])}건",
    )

    section("3. F3016 수정, 삭제")
    patched = client.patch(
        f"/api/plans/{other['plan']['id']}",
        json={
            "meeting_date": "2026-11-26",
            "start_time": "10:00",
            "end_time": "11:00",
            "status": "requested",
        },
    )
    p = patched.json()["plan"]
    row = next(
        x for x in client.get("/api/plans").json()["plans"] if x["id"] == p["id"]
    )
    check(
        "PATCH로 날짜, 시각, 상태 수정 후 목록에 바로 반영",
        patched.status_code == 200
        and (row["meeting_date"], row["start_time"], row["status"])
        == ("2026-11-26", "10:00", "requested")
        and patched.json()["overlaps"] == [],
        brief(row),
    )
    bad = client.patch(f"/api/plans/{p['id']}", json={"end_time": "09:00"})
    check(
        "종료가 시작보다 이른 수정은 거부 (422)", bad.status_code == 422, bad.text[:120]
    )
    deleted = client.delete(f"/api/plans/{plan_id}")
    remain = [x["id"] for x in client.get("/api/plans").json()["plans"]]
    check(
        "DELETE 후 목록에서 바로 사라짐",
        deleted.status_code == 200
        and plan_id not in remain
        and client.get(f"/api/plans/{plan_id}").status_code == 404,
        f"남은 계획 {remain}",
    )

    section("4. 계획 관리 요청 (대화)")
    m = agent.run(sid, "아까 그 계획 저장해 줘")
    check(
        "의도 manage로 분류하고 Tool 없이 계획 관리 화면을 안내",
        m["intent"] == "manage" and m["kind"] == "notice",
        f"{m['intent_reason']} / {m['text']}",
    )

    section("요약")
    n_pass = sum(1 for s, _ in results if s == PASS)
    print(f"{n_pass}/{len(results)} PASS")
    for s, t in results:
        if s == FAIL:
            print(f"  FAIL  {t}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
