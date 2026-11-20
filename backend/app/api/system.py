"""설정 상태 API (F3019, NF3002)

API 키가 설정되었는지만 알려 준다. 키 값은 응답에 싣지 않는다.
"""

from __future__ import annotations

import os

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..tools import ecos, kepco, law

router = APIRouter(prefix="/api/status", tags=["설정 상태"])

# 환경변수 이름 → 쓰이는 곳
KEYS: dict[str, str] = {
    "OPENAI_API_KEY": "LLM (요청 분석, 계획 수립)",
    ecos.KEY_NAME: "한국은행 ECOS (원재료 품목, 물가지수)",
    kepco.KEY_NAME: "한국전력 (산업용 전기요금)",
    law.KEY_NAME: "법제처 (법령 조문)",
}


class KeyStatus(BaseModel):
    env: str = Field(description="환경변수 이름")
    usage: str
    configured: bool


class Status(BaseModel):
    ready: bool = Field(description="모든 키가 설정되었는지")
    keys: list[KeyStatus]


def missing_keys() -> list[str]:
    return [name for name in KEYS if not os.getenv(name, "").strip()]


@router.get("", response_model=Status, summary="API 키 설정 상태")
def status() -> Status:
    missing = set(missing_keys())
    return Status(
        ready=not missing,
        keys=[
            KeyStatus(env=name, usage=usage, configured=name not in missing)
            for name, usage in KEYS.items()
        ],
    )
