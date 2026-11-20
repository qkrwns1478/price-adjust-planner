"""Agent 실행 설정. 값은 .env에서 읽고, 키는 코드에 작성하지 않는다 (NF3001)."""

from __future__ import annotations

import os

LLM_MODEL_DEFAULT = "gpt-6-luna"

LEGAL_SHARE_PCT = 10.0  # 상생협력법 제2조 12, 12의2: 납품대금의 100분의 10 이상
HISTORY_TURNS = 5  # 대화 맥락으로 유지할 직전 대화 수 (F3010)

LAW_NAME = "상생협력법"
LAW_KEYWORD = {
    "material": "주요 원재료",
    "energy": "에너지경비",
    "alt_path": "납품대금 조정",
}

DISCLAIMER = (
    "이 결과는 공개된 가격 지표와 법령을 바탕으로 계산한 참고 자료이며, 법률 자문이 아닙니다. "
    "실제 조정 협의는 체결한 약정서의 내용을 기준으로 하며, 필요하면 중소벤처기업부 납품대금 "
    "연동지원본부나 법률 전문가와 상담하시기 바랍니다."
)


def max_replan() -> int:
    """재계획 상한 (F3009, NF3004). 1 ~ 10으로 제한한다."""
    try:
        value = int(os.getenv("MAX_REPLAN", "3"))
    except ValueError:
        value = 3
    return min(max(value, 1), 10)


def worker_llm() -> bool:
    """Worker의 LLM 판단 사용 여부 (F3021). WORKER_LLM=off면 실패를 모두 Planner에 보고한다."""
    return os.getenv("WORKER_LLM", "on").strip().lower() not in ("off", "0", "false")


def worker_max_retry() -> int:
    """Worker가 같은 Tool로 다시 부르는 횟수 상한 (F3021). 0 ~ 5로 제한한다."""
    try:
        value = int(os.getenv("WORKER_MAX_RETRY", "2"))
    except ValueError:
        value = 2
    return min(max(value, 0), 5)


def llm_settings() -> dict:
    return {
        "api_key": os.getenv("OPENAI_API_KEY") or None,
        "model": os.getenv("LLM_MODEL") or LLM_MODEL_DEFAULT,
    }


_REASONING_MODELS = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def chat_model(temperature: float = 0):
    """OpenAI 채팅 모델.

    추론 모델(gpt-5, gpt-6 계열)은 chat/completions에서 함수 호출을 사용하려면 reasoning_effort를 'none'으로
    설정해야 한다. LLM_REASONING_EFFORT로 변경할 수 있으며, 추론 모델에는 temperature를 전달하지 않는다.
    """
    from langchain_openai import ChatOpenAI

    s = llm_settings()
    effort = os.getenv("LLM_REASONING_EFFORT")
    if effort is None and s["model"].startswith(_REASONING_MODELS):
        effort = "none"
    extra = {"reasoning_effort": effort} if effort else {"temperature": temperature}
    return ChatOpenAI(
        model=s["model"],
        api_key=s["api_key"],
        timeout=30,
        max_retries=1,
        **extra,
    )
