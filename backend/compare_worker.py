"""Planner·Worker 구조 변경 전후 비교 (F3021): 실제 LLM(OpenAI)과 외부 API를 호출한다.

    python backend/compare_worker.py                       # 변경 후, Worker LLM 켬 / 끔
    python backend/compare_worker.py --root <변경 전 소스> --label before

같은 요청을 실행해 재계획 횟수, 요청당 LLM 호출 수, 응답 시간, 재계획 방법, 결과를 data/logs/compare_<label>.json에 남긴다.
LLM 호출 수는 ChatOpenAI의 실제 생성 호출(_generate)을 센다 (분석, 계획, Worker 판단, 대화 응답 모두 포함).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument(
    "--root", default=str(HERE.parent), help="비교할 소스의 저장소 루트"
)
parser.add_argument("--label", default=None)
parser.add_argument("--repeat", type=int, default=1)
args = parser.parse_args()

ROOT = Path(args.root).resolve()
sys.path.insert(0, str(ROOT / "backend"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

from dotenv import load_dotenv  # noqa: E402

load_dotenv(HERE.parent / ".env")

from langchain_openai import ChatOpenAI  # noqa: E402

from app.agent import PlannerAgent  # noqa: E402

REQUESTS = {
    "대표 (한 번에 충족)": (
        "A사 납품 프레임이 개당 12만 원이고, 열연강판이 35%야. 약정서에는 5% 이상 변동하면 분기마다 "
        "조정한다고 돼 있어. 기준 시점은 작년 12월이야. 이번 분기에 요청할 수 있어?"
    ),
    "재계획": "C사 납품 브래킷은 열연강판이 원가의 30%고, 약정 비율은 10%야. 기준 시점은 올해 3월이야. 이번 달에 조정 요청할 수 있어?",
    "상한 도달": "D사 납품 커버는 철판이 40%야. 약정 비율 12%, 기준 시점 올해 3월, 분기 조정이야. 요청 가능해?",
    "품목명 불일치 (폴리프로필렌)": (
        "B사 납품 케이스는 폴리프로필렌이 원가의 30%야. 약정서에는 5% 이상 변동하면 분기마다 조정한다고 돼 있어. "
        "기준 시점은 작년 12월이야. 이번 분기에 요청할 수 있어?"
    ),
    "품목명 불일치 (구리선)": (
        "F사 납품 하네스는 구리선이 원가의 25%야. 약정 비율 5%, 기준 시점 작년 12월, 분기 조정이야. 요청 가능해?"
    ),
    "Tool 불필요": "안녕, 이 서비스는 뭘 해 줘?",
}

COUNT = {"llm": 0}
_generate = ChatOpenAI._generate


def counted(self, *a, **kw):
    COUNT["llm"] += 1
    return _generate(self, *a, **kw)


ChatOpenAI._generate = counted


def measure(label: str) -> list[dict]:
    rows = []
    for name, message in REQUESTS.items():
        for _ in range(args.repeat):
            agent = PlannerAgent()
            sid = agent.new_session()
            COUNT["llm"] = 0
            started = time.perf_counter()
            r = agent.run(sid, message)
            elapsed = round((time.perf_counter() - started) * 1000)
            st = agent.state(sid)
            calls = st.get("calls", [])
            rows.append(
                {
                    "label": label,
                    "request": name,
                    "kind": r["kind"],
                    "replan_count": st.get("replan_count", 0),
                    "strategies": [
                        x["strategy"]
                        for x in (st.get("plan") or {}).get("rounds", [])[1:]
                    ],
                    "llm_calls": COUNT["llm"],
                    "tool_calls": sum(
                        1 for c in calls if not c["cached"] and c["code"] != "SKIPPED"
                    ),
                    "worker_retries": sum(1 for c in calls if c.get("attempt")),
                    "elapsed_ms": elapsed,
                    "result": r.get("summary"),
                }
            )
            row = rows[-1]
            print(
                f"{label:<12} {name:<24} 재계획 {row['replan_count']} {row['strategies']} "
                f"LLM {row['llm_calls']} Tool {row['tool_calls']} 재시도 {row['worker_retries']} "
                f"{elapsed / 1000:.1f}s | {row['result']}"
            )
    return rows


OUT = HERE.parent / "data" / "logs"
OUT.mkdir(parents=True, exist_ok=True)
if args.label:
    rows = measure(args.label)
    (OUT / f"compare_{args.label}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
else:
    for label, flag in (("after_off", "off"), ("after_on", "on")):
        os.environ["WORKER_LLM"] = flag
        rows = measure(label)
        (OUT / f"compare_{label}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
