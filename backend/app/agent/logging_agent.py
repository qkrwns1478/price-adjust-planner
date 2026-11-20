"""로깅 서브 Agent (F3013, F3020)

수집, 저장 (F3013)
    메인 Agent가 발행한 실행 이벤트를 구독해 받은 즉시 세션 단위 실행 로그에 덧붙인다.
    LLM을 호출하지 않으며, 이벤트를 요약하거나 고치지 않고 그대로 저장한다.
    저장에 실패해도 예외를 밖으로 내지 않으므로 대화는 정상 동작한다.

수행 결과 검증 (F3021)
    Worker가 작업을 마친 단계 종료(execute node_end) 이벤트를 받으면, 그때까지의 실행 로그로
    반복 호출, 계획 외 호출, 고정 인자 변경을 규칙으로 검증한다 (LLM 없음). Planner는 verification() 으로 읽는다.

이상 징후 판정, 진단 (F3020)
    요청 종료(run_end) 이벤트를 받으면 저장된 실행 로그로 그 요청을 규칙 판정한다 (LLM 없음).
    판정이 이상(anomaly)일 때만 LLM 진단을 맡기고, 진단은 판정과 따로 저장한다.
    진단은 별도 스레드에서 수행해 응답을 늦추지 않으며, 실패해도 판정과 실행 로그는 그대로 남는다.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from ..events import Event, EventBus, EventType
from ..storage import RunLogStore
from .reviewer import (
    Diagnoser,
    evaluate_run,
    limit_sentences,
    make_llm_diagnoser,
    verify_round,
)


class LoggingAgent:
    def __init__(
        self,
        store: RunLogStore,
        diagnoser: Diagnoser | None = None,
        diagnose_async: bool = True,
    ):
        self.store = store
        self.failures = 0  # 저장하지 못한 이벤트, 판정 수
        self.diagnoses = 0  # 진단을 맡긴 횟수 (LLM 호출 수)
        self._diagnoser = diagnoser
        self._async = diagnose_async
        self._pool: ThreadPoolExecutor | None = None
        self._pending: list[Future] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._verified: dict[str, dict] = {}  # run_id → 가장 최근 수행 결과 검증

    @property
    def running(self) -> bool:
        return self._unsubscribe is not None

    def start(self, bus: EventBus) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = bus.subscribe(self.handle)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def handle(self, event: Event) -> None:
        try:
            self.store.append(event)
        except Exception:  # noqa: BLE001 (저장 실패가 대화를 막지 않는다)
            self.failures += 1
        if event.get("type") == EventType.NODE_END and event.get("node") == "execute":
            self.verify(event["run_id"])
        elif event.get("type") == EventType.RUN_END:
            self._verified.pop(event["run_id"], None)
            self.review(event["session_id"], event["run_id"])

    # ── F3021 ──

    def verify(self, run_id: str) -> dict | None:
        """Worker가 작업을 마친 시점까지의 실행 로그로 수행 결과를 검증한다."""
        try:
            result = verify_round(self.store.get_run(run_id))
        except Exception:  # noqa: BLE001 (검증하지 못해도 대화는 이어진다)
            self.failures += 1
            return None
        self._verified[run_id] = result
        return result

    def verification(self, session_id: str, run_id: str) -> dict | None:
        """Planner가 읽는 가장 최근 검증 결과를 반환한다. 검증하지 못했으면 None을 반환한다."""
        return self._verified.get(run_id)

    # ── F3020 ──

    def review(self, session_id: str, run_id: str) -> dict | None:
        """저장된 실행 로그로 요청 1건을 판정해 저장한다. 이상이면 진단을 맡긴다."""
        try:
            events = self.store.get_run(run_id)
            review = evaluate_run(events)
            self.store.add_review(session_id, run_id, review)
        except Exception:  # noqa: BLE001
            self.failures += 1
            return None
        if review["verdict"] == "anomaly":
            self.diagnoses += 1
            if self._async:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="diagnose"
                    )
                self._pending.append(
                    self._pool.submit(
                        self._diagnose, session_id, run_id, review, events
                    )
                )
            else:
                self._diagnose(session_id, run_id, review, events)
        return review

    def _diagnose(
        self, session_id: str, run_id: str, review: dict, events: list[dict]
    ) -> None:
        try:
            if self._diagnoser is None:
                self._diagnoser = make_llm_diagnoser()
            result = self._diagnoser(review, events)
            diagnosis = {
                "status": "ok",
                "agent": result.get("agent"),
                "stage": result.get("stage"),
                "text": limit_sentences(result.get("text") or ""),
            }
        except (
            Exception
        ):  # noqa: BLE001 (오류 원문에 키, 주소가 섞일 수 있어 남기지 않는다, NF3002)
            diagnosis = {"status": "failed"}
        try:
            self.store.add_diagnosis(session_id, run_id, diagnosis)
        except Exception:  # noqa: BLE001
            self.failures += 1

    def wait(self, timeout: float | None = None) -> None:
        """맡긴 진단이 끝날 때까지 기다린다 (확인, 테스트용)."""
        pending, self._pending = self._pending, []
        for f in pending:
            f.result(timeout)
