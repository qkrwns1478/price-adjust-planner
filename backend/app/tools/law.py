"""국가법령정보 공동활용 Open API Tool

- search_law_article (F3004 보조 정보 검색) 법령명 + 검색어 → 근거 조문 (조문 번호, 제목, 발췌, 링크)

JSON으로 받는다. 이 API의 JSON은 항목이 하나면 객체, 여럿이면 배열로 오므로 _as_list()로 맞춘다.
사용자에게 주는 링크는 OC(인증값)가 든 DRF 주소가 아니라 국가법령정보센터 공개 주소로 만든다.
"""

from __future__ import annotations

import os

from .base import ErrorCode, UpstreamError, fail, http_get, ok

SEARCH_URL = "https://www.law.go.kr/DRF/lawSearch.do"
SERVICE_URL = "https://www.law.go.kr/DRF/lawService.do"
PUBLIC_URL = "https://www.law.go.kr/법령"
KEY_NAME = "LAW_API_OC"
EXCERPT_LEN = 400

_law_body_cache: dict[str, list[dict]] = (
    {}
)  # MST → 조문 목록 (같은 프로세스 안에서 재사용)


class LawError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _norm(s: str | None) -> str:
    return "".join((s or "").split()).replace("·", "").replace("ㆍ", "")


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def _text(v) -> str:
    """문자열 또는 문자열 배열(여러 줄)을 하나의 문자열로 합친다."""
    if isinstance(v, list):
        return "\n".join(_text(x) for x in v if _text(x))
    return v.strip() if isinstance(v, str) else ""


def _get_json(url: str, params: dict) -> dict:
    res = http_get(url, params={**params, "type": "JSON"}, timeout=30)
    try:
        body = res.json()
    except ValueError:
        # 등록, 권한 문제는 HTML 안내 페이지로 반환된다
        text = res.text
        if "미신청" in text or "IP주소" in text or "도메인주소" in text:
            raise LawError(
                ErrorCode.INVALID_KEY,
                f"{KEY_NAME}에 이 API 사용 권한이 없습니다. open.law.go.kr에서 신청 항목과 서버 IP를 확인하세요.",
            ) from None
        raise LawError(
            ErrorCode.UPSTREAM_ERROR, "법령 API 응답을 해석하지 못했습니다."
        ) from None
    if not isinstance(body, dict):
        raise LawError(ErrorCode.UPSTREAM_ERROR, "법령 API 응답을 해석하지 못했습니다.")
    if "result" in body and "필수" in str(body.get("result")):  # OC 빈 값, 검증 실패
        raise LawError(ErrorCode.INVALID_KEY, f"{KEY_NAME}가 유효하지 않습니다.")
    return body


def _find_law(oc: str, law_name: str) -> dict:
    body = _get_json(SEARCH_URL, {"OC": oc, "target": "law", "query": law_name})
    laws = [
        {
            "name": (el.get("법령명한글") or "").strip(),
            "abbr": (el.get("법령약칭명") or "").strip() or None,
            "law_id": el.get("법령ID"),
            "mst": el.get("법령일련번호"),
            "kind": el.get("법령구분명"),
            "ministry": el.get("소관부처명"),
            "effective_date": el.get("시행일자"),
            "status": el.get("현행연혁코드"),
        }
        for el in _as_list(body.get("LawSearch", {}).get("law"))
        if isinstance(el, dict)
    ]
    if not laws:
        raise LawError(ErrorCode.NOT_FOUND, f"'{law_name}' 법령을 찾지 못했습니다.")
    q = _norm(law_name)
    # 우선순위: 법령명, 약칭이 정확히 같은 현행 법령, 그다음 현행 법령, 마지막으로 첫 결과
    laws.sort(
        key=lambda x: (
            q not in (_norm(x["name"]), _norm(x["abbr"])),
            x["status"] != "현행",
        )
    )
    return laws[0]


def _article_label(a: dict) -> str:
    no = str(a.get("조문번호") or "").strip()
    branch = str(a.get("조문가지번호") or "").strip()
    return f"제{no}조" + (f"의{branch}" if branch and branch != "0" else "")


def _article_lines(a: dict) -> list[str]:
    """조문 → 항 → 호 → 목 순서대로 본문 줄을 모은다 (JSON 키의 순서에는 의존하지 않는다)"""
    lines = [_text(a.get("조문내용"))]
    for hang in _as_list(a.get("항")):
        lines.append(_text(hang.get("항내용")))
        for ho in _as_list(hang.get("호")):
            lines.append(_text(ho.get("호내용")))
            for mok in _as_list(ho.get("목")):
                lines.append(_text(mok.get("목내용")))
    return [ln for ln in lines if ln]


def _load_articles(oc: str, mst: str) -> list[dict]:
    if mst in _law_body_cache:
        return _law_body_cache[mst]
    body = _get_json(SERVICE_URL, {"OC": oc, "target": "law", "MST": mst})
    units = _as_list(body.get("법령", {}).get("조문", {}).get("조문단위"))
    articles = [
        {
            "article_no": _article_label(a),
            "title": _text(a.get("조문제목")) or None,
            "lines": _article_lines(a),
        }
        for a in units
        if isinstance(a, dict) and a.get("조문여부") == "조문"
    ]  # '전문'은 장, 절 제목
    if not articles:
        raise LawError(
            ErrorCode.UPSTREAM_ERROR, "법령 본문에서 조문을 읽지 못했습니다."
        )
    _law_body_cache[mst] = articles
    return articles


def _excerpt(lines: list[str], terms: list[str]) -> tuple[str, bool]:
    """검색어가 들어간 줄을 먼저 보여 준다. 길면 잘라 내고, 잘랐다는 사실을 명시한다."""
    hit = [ln for ln in lines if any(t in _norm(ln) for t in terms)]
    text = "\n".join(hit or lines)
    if len(text) > EXCERPT_LEN:
        return text[:EXCERPT_LEN].rstrip() + "…", True
    return text, False


def search_law_article(law_name: str, keyword: str, max_results: int = 3) -> dict:
    law_name, keyword = law_name.strip(), keyword.strip()
    if not law_name or not keyword:
        return fail(ErrorCode.BAD_ARGUMENT, "law_name과 keyword는 필수입니다.")
    oc = os.getenv(KEY_NAME, "").strip()
    if not oc:
        return fail(ErrorCode.INVALID_KEY, f"{KEY_NAME}가 없습니다.")
    try:
        law = _find_law(oc, law_name)
        articles = _load_articles(oc, law["mst"])
    except UpstreamError:
        return fail(ErrorCode.UPSTREAM_ERROR, "법령 API를 부르지 못했습니다.")
    except LawError as e:
        return fail(e.code, e.message)

    terms = [_norm(t) for t in keyword.split() if t.strip()]
    scored = []
    for a in articles:
        body = _norm(" ".join([a["title"] or "", *a["lines"]]))
        if all(t in body for t in terms):  # 검색어를 모두 포함한 조문만
            scored.append((sum(body.count(t) for t in terms), a))
    law_link = f"{PUBLIC_URL}/{''.join(law['name'].split())}"
    law_info = {
        k: law[k]
        for k in ("name", "abbr", "law_id", "kind", "ministry", "effective_date")
    }
    law_info["link"] = law_link

    if not scored:
        return fail(
            ErrorCode.NO_DATA,
            f"{law['name']}에서 '{keyword}'가 들어간 조문을 찾지 못했습니다.",
        )

    scored.sort(key=lambda x: -x[0])
    results = []
    for _, a in scored[:max_results]:
        excerpt, truncated = _excerpt(a["lines"], terms)
        results.append(
            {
                "article_no": a["article_no"],
                "title": a["title"],
                "excerpt": excerpt,
                "truncated": truncated,
                "link": f"{law_link}/{a['article_no']}",
            }
        )
    return ok(
        {
            "law": law_info,
            "keyword": keyword,
            "total_matches": len(scored),
            "articles": results,
        }
    )
