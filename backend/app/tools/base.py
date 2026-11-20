"""Tool 공통 약속 (F3001)

- 성공과 실패 모두 같은 형식으로 반환한다: ok(data) / fail(code, message)
- Tool은 예외를 밖으로 던지지 않는다. Tool.run()이 마지막 안전망이다.
- 오류 메시지에 API 키나 키가 든 주소를 포함하지 않는다 (NF3001, NF3002).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

import requests

# ── 응답 형식 ──────────────────────────────────────────────


def ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def fail(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


class ErrorCode:
    """모든 Tool이 공유하는 실패 코드. 의미가 겹치는 코드는 새로 만들지 않는다."""

    BAD_ARGUMENT = (
        "BAD_ARGUMENT"  # 입력이 규격과 다르다 (빈 값, 형식 틀림, 모르는 인자)
    )
    INVALID_KEY = (
        "INVALID_KEY"  # 키가 없거나 틀렸다, 또는 키에 해당 API 사용 권한이 없다
    )
    NOT_FOUND = "NOT_FOUND"  # 찾는 대상이 없다 (없는 품목 코드, 없는 법령명)
    NO_DATA = "NO_DATA"  # 조회는 됐지만 결과가 비었다 (검색 결과 없음 포함)
    NO_INDEX_VALUE = (
        "NO_INDEX_VALUE"  # 판단에 필요한 지수 값이 비어 있다 → 판단하지 않고 제외
    )
    UPSTREAM_ERROR = "UPSTREAM_ERROR"  # 외부 API가 응답하지 않았다 (시간 초과, 서버 오류, 해석 불가)

    # 같은 입력으로 다시 호출해도 결과가 같은 실패이므로, Agent가 재계획하지 않고 바로 안내한다
    FATAL = frozenset({INVALID_KEY, UPSTREAM_ERROR})
    # 인자를 고치면 결과가 달라질 수 있는 실패
    RETRYABLE_WITH_NEW_ARGS = frozenset({BAD_ARGUMENT, NOT_FOUND, NO_DATA})


# ── Tool 정의 ─────────────────────────────────────────────

ToolKind = Literal["external", "local"]

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}


@dataclass(frozen=True)
class Tool:
    name: str  # 이름. Agent가 이 이름으로 호출한다
    description: str  # 사용 시점에 대한 설명 (모델이 읽는다)
    parameters: dict  # 입력 규격 (JSON Schema, type=object)
    func: Callable[..., dict]  # 실제 작업을 수행하는 함수
    kind: ToolKind = "external"  # external: 외부 API 툴 / local: 로컬 서버 API 툴
    source: str = ""  # 출처 표기 (외부 API 이름)
    requirement: str = ""  # 대응 요구사항 번호

    def validate(self, kwargs: dict) -> dict | None:
        """입력 규격과 다르면 실패 응답을, 맞으면 None을 반환한다."""
        props: dict = self.parameters.get("properties", {})
        unknown = sorted(set(kwargs) - set(props))
        if unknown:
            return fail(
                ErrorCode.BAD_ARGUMENT, f"모르는 인자입니다: {', '.join(unknown)}"
            )
        for req in self.parameters.get("required", []):
            if kwargs.get(req) in (None, ""):
                return fail(ErrorCode.BAD_ARGUMENT, f"{req}는 필수입니다.")
        for key, value in kwargs.items():
            if value is None:
                continue
            spec = props[key]
            expected = _JSON_TYPES.get(spec.get("type", ""))
            # bool은 int의 하위 타입이라 따로 막는다
            if expected and (
                not isinstance(value, expected)
                or (isinstance(value, bool) and spec.get("type") != "boolean")
            ):
                return fail(
                    ErrorCode.BAD_ARGUMENT, f"{key}의 형식이 {spec['type']}가 아닙니다."
                )
            if "enum" in spec and value not in spec["enum"]:
                return fail(
                    ErrorCode.BAD_ARGUMENT,
                    f"{key}는 {spec['enum']} 중 하나여야 합니다.",
                )
            if (
                "pattern" in spec
                and isinstance(value, str)
                and not re.fullmatch(spec["pattern"], value)
            ):
                return fail(
                    ErrorCode.BAD_ARGUMENT,
                    f"{key}의 형식이 맞지 않습니다. ({spec.get('description', '')})",
                )
            if "minimum" in spec and value < spec["minimum"]:
                return fail(
                    ErrorCode.BAD_ARGUMENT,
                    f"{key}는 {spec['minimum']} 이상이어야 합니다.",
                )
            if "maximum" in spec and value > spec["maximum"]:
                return fail(
                    ErrorCode.BAD_ARGUMENT,
                    f"{key}는 {spec['maximum']} 이하여야 합니다.",
                )
        return None

    def run(self, **kwargs: Any) -> dict:
        """Agent가 Tool을 호출하는 유일한 진입점. 어떤 경우에도 예외를 던지지 않는다."""
        invalid = self.validate(kwargs)
        if invalid:
            return invalid
        try:
            result = self.func(**kwargs)
        except (
            Exception
        ):  # noqa: BLE001 (원문에 키가 섞일 수 있어 내용은 포함하지 않는다)
            return fail(ErrorCode.UPSTREAM_ERROR, "Tool 실행 중 오류입니다.")
        if not (isinstance(result, dict) and isinstance(result.get("ok"), bool)):
            return fail(ErrorCode.UPSTREAM_ERROR, "Tool 응답 형식이 약속과 다릅니다.")
        return result

    def spec(self) -> dict:
        """Agent에게는 이름, 설명, 입력 규격만 넘긴다 (F3001)."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


# ── 외부 API 호출 공통 ────────────────────────────────────

DEFAULT_TIMEOUT = 15


class UpstreamError(Exception):
    """외부 API 호출 실패. 메시지에는 키가 든 주소를 싣지 않는다."""


RETRIES = 1  # 일시적인 실패(연결 오류, 시간 초과, 5xx)만 한 번 더 호출한다


def http_get(
    url: str, params: dict, timeout: int = DEFAULT_TIMEOUT
) -> requests.Response:
    for attempt in range(RETRIES + 1):
        last = attempt == RETRIES
        try:
            res = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:  # 오류 원문에는 키가 든 주소가 섞인다
            if last:
                raise UpstreamError(type(exc).__name__) from None
            continue
        if res.status_code >= 500 and not last:
            continue
        res.encoding = "utf-8"
        return res
    raise UpstreamError("unreachable")  # pragma: no cover


YM_PATTERN = r"\d{4}(0[1-9]|1[0-2])"


def shift_month(ym: str, months: int) -> str:
    """'202609' 에 months 개월을 더한다 (음수 가능)."""
    y, m = int(ym[:4]), int(ym[4:])
    total = y * 12 + (m - 1) + months
    return f"{total // 12:04d}{total % 12 + 1:02d}"
