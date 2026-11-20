"""조정 협의 계획 저장소 (F3015, F3016, SQLite)

- 계획 1건을 행 1개로 저장한다. 핵심 값은 위탁기업, 품목, 근거 원재료, 계산 결과(변동률, 요청 금액),
  협의 날짜와 시작, 종료 시각이다. 생성 일시와 수정 일시를 함께 남긴다.
- 같은 날짜에 협의 시간이 겹치는 계획이 있어도 저장과 수정을 막지 않고, 겹치는 계획을 결과와 함께 돌려준다.
- 같은 위탁기업, 품목, 원재료로 아직 끝나지 않은(준비, 요청함) 계획도 막지 않고 함께 알린다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .log_store import DEFAULT_DB

OPEN_STATUSES = ("ready", "requested")

# 저장, 수정할 수 있는 핵심 값. law_refs는 JSON 문자열로 저장한다.
FIELDS = (
    "client",
    "product",
    "item",
    "item_kind",
    "share_pct",
    "unit_price_won",
    "index_code",
    "index_name",
    "base_month",
    "base_value",
    "current_month",
    "current_value",
    "change_rate_pct",
    "threshold_pct",
    "increase_ratio_pct",
    "increase_won",
    "new_price_won",
    "law_refs",
    "meeting_date",
    "start_time",
    "end_time",
    "status",
    "memo",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    client              TEXT    NOT NULL,
    product             TEXT,
    item                TEXT    NOT NULL,
    item_kind           TEXT    NOT NULL,
    share_pct           REAL,
    unit_price_won      INTEGER,
    index_code          TEXT,
    index_name          TEXT,
    base_month          TEXT,
    base_value          REAL,
    current_month       TEXT,
    current_value       REAL,
    change_rate_pct     REAL,
    threshold_pct       REAL,
    increase_ratio_pct  REAL,
    increase_won        INTEGER,
    new_price_won       INTEGER,
    law_refs            TEXT    NOT NULL DEFAULT '[]',
    meeting_date        TEXT    NOT NULL,
    start_time          TEXT    NOT NULL,
    end_time            TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    memo                TEXT,
    session_id          TEXT,
    run_id              TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_date ON plans (meeting_date, start_time);
"""

_BRIEF = (
    "id",
    "client",
    "product",
    "item",
    "meeting_date",
    "start_time",
    "end_time",
    "status",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _row(row: sqlite3.Row) -> dict:
    return dict(row) | {"law_refs": json.loads(row["law_refs"])}


class PlanStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.getenv("PLANNER_DB_PATH") or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # ── 저장, 조회 (F3015) ──

    def create(
        self, plan: dict, session_id: str | None = None, run_id: str | None = None
    ) -> dict:
        """계획을 저장하고 {plan, overlaps, duplicates} 를 반환한다."""
        values = {k: plan.get(k) for k in FIELDS}
        values["law_refs"] = json.dumps(values["law_refs"] or [], ensure_ascii=False)
        now = _now()
        cols = list(FIELDS) + ["session_id", "run_id", "created_at", "updated_at"]
        with self._connect() as db:
            plan_id = db.execute(
                f"INSERT INTO plans ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [values[k] for k in FIELDS] + [session_id, run_id, now, now],
            ).lastrowid
        return self._with_conflicts(self.get(plan_id))

    def list(self) -> list[dict]:
        """협의 날짜, 시작 시각 순서로 반환한다."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM plans ORDER BY meeting_date, start_time, id"
            ).fetchall()
        return [_row(r) for r in rows]

    def get(self, plan_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return _row(row) if row else None

    # ── 수정, 삭제 (F3016) ──

    def update(self, plan_id: int, changes: dict) -> dict | None:
        """바꾼 값만 덮어쓰고 {plan, overlaps, duplicates} 를 반환한다. 계획이 없으면 None"""
        changes = {k: v for k, v in changes.items() if k in FIELDS}
        if "law_refs" in changes:
            changes["law_refs"] = json.dumps(
                changes["law_refs"] or [], ensure_ascii=False
            )
        with self._connect() as db:
            sets = ", ".join(f"{k} = ?" for k in changes) + (", " if changes else "")
            count = db.execute(
                f"UPDATE plans SET {sets}updated_at = ? WHERE id = ?",
                [*changes.values(), _now(), plan_id],
            ).rowcount
        if not count:
            return None
        return self._with_conflicts(self.get(plan_id))

    def delete(self, plan_id: int) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM plans WHERE id = ?", (plan_id,)).rowcount > 0

    # ── 겹침 ──

    def overlaps(self, plan: dict) -> list[dict]:
        """같은 날짜에 협의 시간이 겹치는 다른 계획. 한쪽이 끝나는 시각에 다른 쪽이 시작하면 겹치지 않는다."""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {', '.join(_BRIEF)} FROM plans"
                " WHERE id != ? AND meeting_date = ? AND start_time < ? AND ? < end_time"
                " ORDER BY start_time, id",
                (
                    plan["id"],
                    plan["meeting_date"],
                    plan["end_time"],
                    plan["start_time"],
                ),
            ).fetchall()
        return [dict(r) for r in rows]

    def duplicates(self, plan: dict) -> list[dict]:
        """같은 위탁기업, 품목, 원재료로 아직 진행 중인 다른 계획을 반환한다."""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {', '.join(_BRIEF)} FROM plans"
                " WHERE id != ? AND client = ? AND IFNULL(product, '') = IFNULL(?, '') AND item = ?"
                f" AND status IN ({', '.join('?' * len(OPEN_STATUSES))})"
                " ORDER BY meeting_date, start_time, id",
                (
                    plan["id"],
                    plan["client"],
                    plan["product"],
                    plan["item"],
                    *OPEN_STATUSES,
                ),
            ).fetchall()
        return [dict(r) for r in rows]

    def _with_conflicts(self, plan: dict) -> dict:
        return {
            "plan": plan,
            "overlaps": self.overlaps(plan),
            "duplicates": self.duplicates(plan),
        }
