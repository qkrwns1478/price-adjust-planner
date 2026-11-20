"""메인 Agent 실행 흐름 확인 (F3005 ~ F3011): 실제 LLM(OpenAI)과 외부 API를 호출한다.

    python backend/check_agent.py            # 확인 항목 실행
    python backend/check_agent.py --graph    # 그래프 구조(Mermaid)만 출력

출력에 API 키를 포함하지 않는다. 대표 요청의 전체 상태는 data/agent/ 에 JSON으로 저장한다 (형상관리 제외).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT.parent / ".env")

from app.agent import PlannerAgent  # noqa: E402
from app.agent.config import max_replan  # noqa: E402
from app.agent.responder import condition_text  # noqa: E402
from app.tools import call_tool, catalog  # noqa: E402
from app.tools.base import ErrorCode, fail  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str]] = []
OUT = ROOT.parent / "data" / "agent"

REPRESENTATIVE = (
    "A사 납품 프레임이 개당 12만 원이고, 열연강판이 35%야. 약정서에는 5% 이상 변동하면 분기마다 "
    "조정한다고 돼 있어. 기준 시점은 작년 12월이야. 이번 분기에 요청할 수 있어?"
)
REPLAN = "C사 납품 브래킷은 열연강판이 원가의 30%고, 약정 비율은 10%야. 기준 시점은 올해 3월이야. 이번 달에 조정 요청할 수 있어?"
LIMIT = "D사 납품 커버는 철판이 40%야. 약정 비율 12%, 기준 시점 올해 3월, 분기 조정이야. 요청 가능해?"
FOLLOW_UP = "같은 조건에서 원재료만 알루미늄으로 바꿔서 계산해 줘."
EXCLUDED = "E사 납품 하우징은 철판이 40%야. 약정 비율 10%, 기준 시점 올해 3월, 분기 조정이야. 요청 가능해?"


def check(title: str, good: bool, detail: str = "") -> bool:
    status = PASS if good else FAIL
    results.append((status, title))
    print(f"[{status}] {title}" + (f"\n       → {detail}" if detail else ""))
    return good


def section(t: str):
    print(f"\n── {t} " + "─" * max(0, 60 - len(t)))


def run(
    agent: PlannerAgent, message: str, sid: str | None = None
) -> tuple[str, dict, dict, float]:
    sid = sid or agent.new_session()
    started = time.perf_counter()
    response = agent.run(sid, message)
    return sid, response, agent.state(sid), time.perf_counter() - started


def tool_counts(state: dict) -> dict:
    out: dict = {}
    for c in state.get("calls", []):
        key = (c["tool"], "재사용" if c["cached"] else "호출")
        out[key] = out.get(key, 0) + 1
    return out


def calls_text(state: dict) -> str:
    return (
        " / ".join(
            f"{c['step']} {c['tool']}{' (재사용)' if c['cached'] else ''}{' ' + c['code'] if c['code'] else ''}"
            for c in state.get("calls", [])
        )
        or "Tool 호출 없음"
    )


def main() -> int:
    agent = PlannerAgent()
    if "--graph" in sys.argv:
        print(agent.graph.get_graph().draw_mermaid())
        return 0
    OUT.mkdir(parents=True, exist_ok=True)

    section("1. F3005 조건 추출 (기준, 선호, 제약)")
    sid, r, st, sec = run(agent, REPRESENTATIVE)
    (OUT / "representative.json").write_text(
        json.dumps(
            {
                "request": REPRESENTATIVE,
                "response": r,
                "calls": st["calls"],
                "plan": st["plan"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    print(f"요청: {REPRESENTATIVE}\n의도: {r['intent']} ({r['intent_reason']})")
    for group, values in r["conditions"].items():
        print(f"  {group}: {json.dumps(values, ensure_ascii=False)}")
    c = st["conditions"]
    item = (c["items"] or [{}])[0]
    check(
        "기준, 제약 조건을 요청대로 추출",
        c["unit_price_won"] == 120000
        and item.get("name") == "열연강판"
        and item.get("share_pct") == 35
        and c["base_month"] == "202512"
        and c["threshold_pct"] == 5
        and c["cycle"] == "분기",
        condition_text(c),
    )
    check(
        "요청에 없던 조건은 비워 둠 (비교 월, 지표, 시도, 시군구)",
        all(c[k] is None for k in ("current_month", "index_name", "region", "city")),
        json.dumps(
            {k: c[k] for k in ("current_month", "index_name", "region", "city")},
            ensure_ascii=False,
        ),
    )
    check(f"대표 요청 처리 시간 (NF3005) {sec:.1f}초", sec < 30, calls_text(st))

    section("2. 그래프 구조")
    nodes = set(agent.graph.get_graph().nodes) - {"__start__", "__end__"}
    print(agent.graph.get_graph().draw_mermaid())
    check(
        "분석 → 계획 → 실행 → 판단 → 응답 노드",
        nodes == {"analyze", "plan", "execute", "judge", "respond"},
        ", ".join(sorted(nodes)),
    )

    section("3. F3006 Tool이 필요한 요청과 필요 없는 요청의 계획")
    p = st["plan"]
    print(
        f"  LLM 초안: {[(d['tool'], d['item'], d['keyword']) for d in p['draft'] or []]}"
    )
    print(f"  이유: {p['draft_reason']}")
    print(f"  보정: {p['corrections'] or '없음'}")
    names = {t["name"] for t in catalog()}
    check(
        "Tool 명세를 받은 LLM이 Tool과 순서를 고름",
        p["source"] == "llm"
        and bool(p["draft"])
        and all(d["tool"] in names for d in p["draft"]),
        " → ".join(d["tool"] for d in p["draft"] or []),
    )
    tool_steps = [s["tool"] for s in st["plan"]["steps"]]
    ordered = (
        "search_material_item" in tool_steps
        and "get_material_price_index" in tool_steps
        and tool_steps.index("search_material_item")
        < tool_steps.index("get_material_price_index")
    )
    check(
        "대표 요청: 후보 조회 → 상세 조회 순서와 근거 조문 검색이 계획에 들어 있음",
        ordered
        and "search_law_article" in tool_steps
        and set(tool_steps)
        <= {"search_material_item", "get_material_price_index", "search_law_article"},
        " → ".join(tool_steps),
    )
    for msg in ("안녕하세요", "이 서비스는 어떻게 쓰면 돼?"):
        _, r2, st2, _ = run(agent, msg)
        check(
            f"'{msg}': 계획 단계를 건너뜀",
            r2["intent"] == "chat" and not st2["plan"] and not st2["calls"],
            f"의도 {r2['intent']}, 계획 {st2['plan'] or '없음'}, 응답 {r2['text'][:60]}",
        )
    _, r2, st2, _ = run(
        agent, "철판값이 많이 올랐는데, D사한테 단가 올려 달라고 해도 돼?"
    )
    check(
        "조건이 모자란 요청: Tool을 부르기 전에 되물음",
        r2["kind"] == "clarify" and not st2["calls"],
        " / ".join(r2.get("questions", [])),
    )
    _, r2, st2, _ = run(agent, "알루미늄이 원가의 8%인데 연동 대상이야?")
    check(
        "대상 여부 판정: 지수 조회 없이 근거 조문만 계획",
        {s["tool"] for s in st2["plan"]["steps"]} == {"search_law_article"},
        f"초안 {[d['tool'] for d in st2['plan']['draft'] or []]}, 보정 {st2['plan']['corrections'] or '없음'}, {calls_text(st2)}",
    )

    section("4. F3007 앞 Tool 결과 → 다음 Tool 입력, 실패해도 진행")
    s1 = next(x for x in st["calls"] if x["tool"] == "search_material_item")
    s2 = next(x for x in st["calls"] if x["tool"] == "get_material_price_index")
    step2 = next(x for x in st["plan"]["steps"] if x["id"] == s2["step"])
    check(
        "상세 조회의 item_code는 후보 조회 결과에서 전달",
        step2["args_from"]["item_code"].startswith(s1["step"])
        and s2["args"]["item_code"] == "307125AA",
        f"{step2['args_from']} → {s2['args']}",
    )

    def broken(name: str, args: dict) -> dict:
        if name == "search_material_item":
            return fail(ErrorCode.UPSTREAM_ERROR, "일부러 실패시킨 호출입니다.")
        return call_tool(name, args)

    broken_agent = PlannerAgent(tool_caller=broken)
    _, r3, st3, _ = run(broken_agent, REPRESENTATIVE)
    codes = [(x["tool"], x["code"]) for x in st3["calls"]]
    check(
        "후보 조회를 실패시켜도 나머지 단계 진행, 실패 사유 기록",
        codes[0] == ("search_material_item", "UPSTREAM_ERROR")
        and codes[1][1] == "SKIPPED"
        and codes[2] == ("search_law_article", None)
        and r3["kind"] == "result",
        calls_text(st3),
    )
    check(
        "다시 호출해도 같은 실패(UPSTREAM_ERROR)는 재계획하지 않고 안내",
        st3["replan_count"] == 0 and "UPSTREAM_ERROR" in r3["fatal"],
        r3["stop_reason"],
    )

    section("5. F3008 제약 판단 계산과 역산 기준")
    crit = r["criteria"][0]
    rec = r["recommendations"][0]
    rate, base = crit["index"]["rate"], crit["index"]["base"]["value"]
    expect_amount = round(120000 * 0.35 * rate / 100)
    check(
        "변동률, 요청 금액이 응답의 판단 기준과 일치",
        rec["rate_pct"] == rate
        and rec["amount"]["increase_won"] == expect_amount
        and f"{expect_amount:,}원" in r["text"]
        and f"{rate:g}%" in r["text"],
        f"변동률 {rate}% ≥ 5%, 120,000 × 35% × {rate}% = {expect_amount:,}원",
    )
    check(
        "요건 충족 지수를 역산해 제시",
        crit["back_calc"]["required_value"] == round(base * 1.05, 2),
        crit["back_calc"]["message"],
    )
    check(
        "확인할 수 없는 직전 조정 시점은 지어내지 않고 조건으로 제시",
        any(
            u["label"] == "직전 조정 시점" and "이었다면" in u["criterion"]
            for u in r["unverified"]
        ),
        " / ".join(u["criterion"] for u in r["unverified"]),
    )

    section("6. F3009 재계획과 결과 재사용")
    _, r4, st4, _ = run(agent, REPLAN)
    (OUT / "replan.json").write_text(
        json.dumps(
            {
                "request": REPLAN,
                "response": r4,
                "calls": st4["calls"],
                "plan": st4["plan"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    counts = tool_counts(st4)
    for rd in st4["plan"]["rounds"]:
        print(
            f"  {rd['round']}회 {rd['strategy']}: {rd['reason']}, 새 단계 {rd['steps']}, 재사용 {rd.get('reused', [])}"
        )
    check(
        "충족하는 후보가 없어 재계획이 일어남",
        st4["replan_count"] >= 1,
        f"재계획 {st4['replan_count']}회, {r4['stop_reason']}",
    )
    check(
        "이미 확보한 후보 조회, 상세 조회는 다시 부르지 않음",
        counts.get(("search_material_item", "호출")) == 1
        and counts.get(("get_material_price_index", "호출")) == 1,
        calls_text(st4),
    )
    check(
        "재조회 계획에 추세 기반 도달 시점(추정) 제시",
        bool(r4["criteria"][0].get("forecast")),
        (r4["criteria"][0].get("forecast") or {}).get("message", ""),
    )

    _, r5, st5, _ = run(agent, LIMIT)
    (OUT / "replan_limit.json").write_text(
        json.dumps(
            {
                "request": LIMIT,
                "response": r5,
                "calls": st5["calls"],
                "plan": st5["plan"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    for rd in st5["plan"]["rounds"]:
        print(f"  {rd['round']}회 {rd['strategy']}: {rd['reason']}")
    check(
        f"재계획 상한({max_replan()}회)에서 멈추고 사유와 함께 응답",
        st5["replan_count"] == max_replan()
        and "상한" in (r5["stop_reason"] or "")
        and "알려 주시면" in r5["text"],
        f"{r5['stop_reason']}, {calls_text(st5)}",
    )

    section("7. F3010 후속 요청: 바뀐 조건만 갱신")
    before = agent.state(sid)["conditions"]
    _, r6, st6, _ = run(agent, FOLLOW_UP, sid)
    after = st6["conditions"]
    same = {k: before[k] == after[k] for k in before if k != "items"}
    print(f"  직전: {condition_text(before)}\n  이후: {condition_text(after)}")
    check(
        "원재료만 바뀌고 나머지 조건은 유지",
        r6["changed"] == ["items"]
        and all(same.values())
        and after["items"][0]["name"] == "알루미늄"
        and after["items"][0]["share_pct"] == 35,
        f"바뀐 조건 {r6['changed']}",
    )
    check(
        "대화 맥락에는 요약만 남고 Tool 원본 응답은 남지 않음",
        not st6["step_results"] and not st6["cache"] and len(st6["history"]) == 2,
        " / ".join(h["summary"] for h in st6["history"]),
    )

    section("8. F3011 응답 구성")
    check(
        "추천 항목, 보조 정보, 판단 기준, 근거 Tool 결과를 함께 제시",
        all(r[k] for k in ("recommendations", "aux", "criteria", "evidence"))
        and all(
            h in r["text"]
            for h in ("## 결론", "## 판단 기준", "## 근거 조문", "## 근거 Tool 결과")
        ),
        f"추천 {len(r['recommendations'])}, 조문 {len(r['aux'])}, 근거 {len(r['evidence'])}",
    )
    check(
        "판단 기준(법정 비중 10%)의 근거 조문이 보조 정보에 포함",
        any(a["article_no"] == "제2조" for a in r["aux"]),
        ", ".join(f"{a['article_no']}({a['title']})" for a in r["aux"]),
    )
    _, r7, _, _ = run(agent, EXCLUDED)
    check(
        "제외한 후보에 제외 사유",
        bool(r7["excluded"]) and all(e["reason"] for e in r7["excluded"]),
        " / ".join(f"{e['item']}: {e['reason']}" for e in r7["excluded"]),
    )
    check(
        "위험 도메인 면책 안내 표시",
        r["disclaimer"]
        and r["disclaimer"] in r["text"]
        and r5["disclaimer"] in r5["text"],
        r["disclaimer"][:40] + " …",
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
