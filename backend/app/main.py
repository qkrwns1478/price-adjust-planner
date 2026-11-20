"""FastAPI 서버

    cd backend
    uvicorn app.main:app --reload

메인 Agent와 로깅 Agent는 EventBus로만 연결한다. 메인 Agent는 로깅 Agent를 알지 못한다 (F3012, F3013).
메인 Agent는 판정을 저장소에서 읽는 함수(review_source)만 받는다 (F3020).
Planner는 로깅 Agent의 수행 결과 검증을 읽는 함수(verifier)만 받는다 (F3021).
계획 저장소는 Agent와 별개로 API에서만 다룬다 (F3015, F3016).
프론트엔드(frontend/)는 API만 호출한다. 이 서버가 / 에서 함께 제공하지만, 다른 서버에서 열어도 된다 (F3018, NF3006).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from .agent import PlannerAgent  # noqa: E402
from .agent.logging_agent import LoggingAgent  # noqa: E402
from .api import chat, logs, plans, system  # noqa: E402
from .events import EventBus  # noqa: E402
from .storage import PlanStore, RunLogStore  # noqa: E402

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
# Windows는 레지스트리 설정에 따라 .js를 text/plain으로 보내 모듈 스크립트가 막힌다
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def create_app(
    log_store: RunLogStore | None = None,
    agent: PlannerAgent | None = None,
    plan_store: PlanStore | None = None,
) -> FastAPI:
    app = FastAPI(title="단가조정 플래너 API")
    bus = agent.bus if agent is not None else EventBus()
    store = log_store or RunLogStore()
    logger = LoggingAgent(store)
    logger.start(bus)
    agent = agent or PlannerAgent(bus=bus)
    if agent.review_source is None:
        agent.review_source = lambda session_id, run_id: store.get_review(run_id)
    if agent.verifier is None:
        agent.verifier = logger.verification

    app.state.bus = bus
    app.state.log_store = store
    app.state.logging_agent = logger
    app.state.agent = agent
    app.state.plan_store = plan_store or PlanStore(store.path)

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(chat.router)
    app.include_router(plans.router)
    app.include_router(logs.router)
    app.include_router(system.router)
    if FRONTEND_DIR.is_dir():
        app.mount("/", FrontendFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


class FrontendFiles(StaticFiles):
    """ES 모듈은 파일 단위로 캐시되어, 일부만 갱신된 화면이 섞일 수 있다. 매번 재검증하게 한다."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = create_app()
