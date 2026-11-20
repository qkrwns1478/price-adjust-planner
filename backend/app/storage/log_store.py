"""실행 로그 저장소 (F3013, F3014, F3020, SQLite)

- 이벤트 1건을 행 1개로, 받은 순서대로 덧붙인다. 원본 이벤트를 가공하지 않고 그대로 남긴다.
- 요청 1건의 판정(run_reviews)과 LLM 진단(run_diagnoses)은 실행 로그와 다른 테이블에 둔다.
  진단은 판정을 고치지 않고 따로 쌓인다.
- 제공하는 작업은 덧붙이기, 조회, 세션 삭제뿐이다. 수정 기능은 두지 않으며,
  데이터베이스에서도 UPDATE를 트리거로 막는다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

DEFAULT_DB = Path(__file__).resolve().parents[3] / "data" / "planner.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    run_id      TEXT    NOT NULL,
    seq         INTEGER NOT NULL,
    type        TEXT    NOT NULL,
    node        TEXT,
    ts          TEXT    NOT NULL,
    data        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_events_session ON run_events (session_id, id);
CREATE TRIGGER IF NOT EXISTS run_events_read_only
BEFORE UPDATE ON run_events
BEGIN
    SELECT RAISE(ABORT, 'run_events is append-only');
END;

CREATE TABLE IF NOT EXISTS run_reviews (
    run_id      TEXT    PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    verdict     TEXT    NOT NULL,
    warnings    INTEGER NOT NULL,
    checks      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_reviews_session ON run_reviews (session_id);
CREATE TRIGGER IF NOT EXISTS run_reviews_read_only
BEFORE UPDATE ON run_reviews
BEGIN
    SELECT RAISE(ABORT, 'run_reviews is append-only');
END;

CREATE TABLE IF NOT EXISTS run_diagnoses (
    run_id      TEXT    PRIMARY KEY,
    session_id  TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    agent       TEXT,
    stage       TEXT,
    text        TEXT,
    created_at  TEXT    NOT NULL
);
CREATE TRIGGER IF NOT EXISTS run_diagnoses_read_only
BEFORE UPDATE ON run_diagnoses
BEGIN
    SELECT RAISE(ABORT, 'run_diagnoses is append-only');
END;
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class RunLogStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.getenv("PLANNER_DB_PATH") or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """작업 1건마다 연결을 열고, 끝나면 커밋한 뒤 닫는다."""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def append(self, event: dict) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO run_events (session_id, run_id, seq, type, node, ts, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event["session_id"],
                    event["run_id"],
                    event["seq"],
                    event["type"],
                    event.get("node"),
                    event["ts"],
                    json.dumps(event.get("data", {}), ensure_ascii=False, default=str),
                ),
            )

    def list_sessions(self) -> list[dict]:
        """세션별 요약. 최근에 기록된 세션부터 반환한다."""
        with self._connect() as db:
            rows = db.execute("""
                SELECT session_id,
                       MIN(ts) AS started_at,
                       MAX(ts) AS ended_at,
                       COUNT(DISTINCT run_id) AS runs,
                       COUNT(*) AS events,
                       SUM(type = 'tool_request') AS tool_calls,
                       SUM(type = 'decision' AND json_extract(data, '$.kind') = 'replan') AS replans,
                       (SELECT json_extract(f.data, '$.message') FROM run_events f
                         WHERE f.session_id = e.session_id AND f.type = 'run_start'
                         ORDER BY f.id LIMIT 1) AS first_message,
                       (SELECT COUNT(*) FROM run_reviews r
                         WHERE r.session_id = e.session_id AND r.verdict = 'anomaly') AS anomalies,
                       MAX(id) AS last_id
                  FROM run_events e
                 GROUP BY session_id
                 ORDER BY last_id DESC
                """).fetchall()
        return [
            {k: row[k] for k in row.keys() if k != "last_id"}
            | {
                "tool_calls": row["tool_calls"] or 0,
                "replans": row["replans"] or 0,
                "anomalies": row["anomalies"] or 0,
            }
            for row in rows
        ]

    def get_session(self, session_id: str) -> list[dict] | None:
        """세션의 이벤트를 기록된 순서(실행 순서)대로 반환한다. 세션이 없으면 None을 반환한다."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, session_id, run_id, seq, type, node, ts, data"
                " FROM run_events WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        if not rows:
            return None
        return [dict(row) | {"data": json.loads(row["data"])} for row in rows]

    def get_run(self, run_id: str) -> list[dict]:
        """요청 1건의 이벤트를 기록된 순서대로 반환한다."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, session_id, run_id, seq, type, node, ts, data"
                " FROM run_events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [dict(row) | {"data": json.loads(row["data"])} for row in rows]

    def delete_session(self, session_id: str) -> int:
        """세션의 실행 로그와 판정, 진단을 삭제하고 삭제한 이벤트 수를 반환한다."""
        with self._connect() as db:
            db.execute("DELETE FROM run_reviews WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM run_diagnoses WHERE session_id = ?", (session_id,))
            return db.execute(
                "DELETE FROM run_events WHERE session_id = ?", (session_id,)
            ).rowcount

    # ── 판정, 진단 (F3020) ──

    def add_review(self, session_id: str, run_id: str, review: dict) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO run_reviews (run_id, session_id, verdict, warnings, checks, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    session_id,
                    review["verdict"],
                    review["warnings"],
                    json.dumps(review["checks"], ensure_ascii=False, default=str),
                    _now(),
                ),
            )

    def add_diagnosis(self, session_id: str, run_id: str, diagnosis: dict) -> None:
        """status = ok | failed. 실패하면 agent, stage, text를 비워 둔다."""
        with self._connect() as db:
            db.execute(
                "INSERT INTO run_diagnoses (run_id, session_id, status, agent, stage, text, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    session_id,
                    diagnosis["status"],
                    diagnosis.get("agent"),
                    diagnosis.get("stage"),
                    diagnosis.get("text"),
                    _now(),
                ),
            )

    _REVIEW_SQL = """
        SELECT r.run_id, r.session_id, r.verdict, r.warnings, r.checks, r.created_at,
               d.status AS d_status, d.agent AS d_agent, d.stage AS d_stage,
               d.text AS d_text, d.created_at AS d_created_at
          FROM run_reviews r LEFT JOIN run_diagnoses d ON d.run_id = r.run_id
    """

    @staticmethod
    def _review_row(row: sqlite3.Row) -> dict:
        diagnosis = None
        if row["d_status"] is not None:
            diagnosis = {
                "status": row["d_status"],
                "agent": row["d_agent"],
                "stage": row["d_stage"],
                "text": row["d_text"],
                "created_at": row["d_created_at"],
            }
        return {
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "verdict": row["verdict"],
            "warnings": row["warnings"],
            "checks": json.loads(row["checks"]),
            "created_at": row["created_at"],
            "diagnosis": diagnosis,
        }

    def get_review(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                self._REVIEW_SQL + " WHERE r.run_id = ?", (run_id,)
            ).fetchone()
        return self._review_row(row) if row else None

    def list_reviews(self, session_id: str) -> list[dict]:
        """세션의 판정을 요청 순서대로 반환한다."""
        with self._connect() as db:
            rows = db.execute(
                self._REVIEW_SQL + " WHERE r.session_id = ? ORDER BY r.rowid",
                (session_id,),
            ).fetchall()
        return [self._review_row(r) for r in rows]
