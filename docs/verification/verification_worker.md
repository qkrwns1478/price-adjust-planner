# 검증 결과: Planner·Worker 구조 전환 `F3021`

- 실행일: 2026-11-19
- `F3021` 확인 항목과 구조 변경 전후 비교를 기록합니다.
- 설계: [agent_design.md](../agent_design.md) 16절

## 1. 실행 방법

```bash
# 실제 LLM(OpenAI)과 외부 API를 호출하는 확인: 17개 항목
python backend/check_worker.py

# 구조 변경 전후 비교 (변경 후 Worker LLM 켬 / 끔)
python backend/compare_worker.py
python backend/compare_worker.py --root <변경 전 소스> --label before

# LLM과 외부 API 없이 실행하는 단위 테스트
python -m unittest discover -s backend/tests -v
```

| 실행 | 결과 |
|---|---|
| `backend/check_worker.py` | **17/17 PASS** (모델 `gpt-6-luna`) |
| `backend/check_agent.py`, `check_logging.py`, `check_review.py`, `check_plans.py` | 26/26, 14/14, 16/16, 11/11 PASS (변경 후 회귀 확인) |
| `unittest` | **103/103 OK** (Planner·Worker 13개 추가) |

확인 결과는 `data/logs/worker_check.json`, 비교 결과는 `data/logs/compare_*.json`에 남깁니다 (형상관리 제외).

## 2. 재현한 상황

| 상황 | 만든 방법 |
|---|---|
| 결과 없음 (`NO_DATA`) | 실제 요청 "B사 납품 케이스는 폴리프로필렌이 원가의 30%야 …". ECOS 생산자물가지수에는 `폴리프로필렌` 품목이 없다. Planner LLM이 초안에서 검색어를 미리 바꿀 수 있으므로, 실제 초안을 받은 뒤 품목 검색어만 사용자가 말한 이름으로 맞췄다 |
| 계획 변경 요청 | 같은 요청에서 초안 검색어를 `PP`로 두고 `WORKER_LLM=off`로 실행 |
| 치명적 실패 | `ECOS_OPEN_API`를 비우고 대표 요청 실행 |
| 필수 인자 누락 | `search_law_article`의 `keyword`를 빈 값으로 둔 작업을 Worker에 실행 |
| 로깅 Agent가 이상으로 검증 | Worker는 고정 인자를 코드로 막는다. 고정 인자 보호를 끈 Worker에 "약정서 지표 이름(고정 인자)을 합성수지로 바꾸라"는 판단을 넣고, 약정서 지표가 폴리프로필렌이라고 말한 요청을 실행 |

## 3. `F3021` 확인 항목

| # | 확인 항목 | 실제 결과 | 결과 |
|---|---|---|---|
| 1 | Planner의 작업 목록과 Worker의 수행 결과가 정해진 형식으로 오가는지 | `agent_message` planner→worker `task_list` {round, strategy, tasks[id, tool, args, args_from, fixed, purpose]}, worker→planner `work_report` {round, tasks[step, tool, status, attempts, code], requests} | PASS |
| 2 | 로깅 Agent가 이상으로 판단했을 때 Planner가 작업 목록을 다시 구성하는지 | Worker 수행 직후 검증 `fixed_arg_changed warn`: "고정 인자를 바꿔 호출했습니다: s1 keyword" → Planner 재계획 `['initial', 'verify_fix']`, s1, s2를 버리고 Planner가 정한 검색어 `폴리프로필렌`으로 다시 수행 (`no_retry`) | PASS |
| 3 | Planner, Worker, 로깅 Agent 사이의 메시지가 실행 로그에 남는지 | 저장된 실행 로그에 `planner→worker task_list`, `worker→planner work_report`, `logging→planner verification`이 모두 있음 | PASS |
| 4 | 구조 변경 전후의 실행 흐름과 재계획 횟수를 비교한 결과가 있는지 | 4절 | PASS |
| 5 | 정상 경로에서 Worker의 LLM 호출이 0회인지 | 대표 요청: Worker 판단 이벤트 0건, 재계획 0회, 열연강판 충족 | PASS |
| 5 | 치명적 실패에서 LLM이 호출되지 않는지 | `INVALID_KEY`: `decision(kind=worker, by=rule)` "다시 불러도 결과가 같은 치명적 실패라 LLM에 묻지 않습니다." Worker LLM 0회 | PASS |
| 5 | 필수 인자 누락에서 LLM이 호출되지 않는지 | `BAD_ARGUMENT`: "필수 인자가 비어 있어 사용자에게 되물을 일이라 LLM에 묻지 않습니다. Planner에 보고합니다." Worker LLM 0회 | PASS |
| 6 | 결과 없음에서 Worker가 LLM 판단으로 인자를 고쳐 같은 Tool을 다시 부르는지 | `search_material_item` `폴리프로필렌` → `NO_DATA` → Worker LLM `retry` "폴리프로필렌이 품목명으로 검색되지 않아, 같은 재료인 합성수지라는 상위 분류명으로 재검색합니다." → 같은 작업 s1 `attempt=1` `합성수지` → 합성수지(305211AA) 로 판단, 재계획 0회 | PASS |
| 6 | 고정 인자는 바뀌지 않는지 | 첫 호출 `{keyword: 폴리프로필렌}`, 재시도 `{keyword: 합성수지}`. 바뀐 인자는 고칠 수 있는 인자(`keyword`)뿐 | PASS |
| 7 | Worker가 올린 계획 변경 요청이 Planner의 재계획에 반영되는지 | 요청 {step s1, code NO_DATA, tried [{keyword: PP}]} → 재계획 `['initial', 'worker_request']`, 사용자가 말한 `폴리프로필렌`으로 다시 검색. 그래도 없어 "약정서에 적힌 기준 지표 이름을 알려 주시면 그 이름으로 다시 찾겠습니다" 를 되물음 | PASS |
| — | 요청 종료 판정 `F3020`에도 남는지 | 2번 실행의 판정 `anomaly`, 진단 `worker/execute` "s1의 keyword는 Planner가 '폴리프로필렌'으로 고정했는데, NO_DATA 응답 뒤 Worker가 이를 '합성수지'로 바꿔 호출해 경고가 발생했습니다 …" | PASS |

다른 품목으로 바꿔 확보한 지수는 응답의 "API로 확인할 수 없어 직접 확인할 사항" 에 "약정서의 기준 지표가 ECOS 합성수지(305211AA)일 때만 이 변동률이 그대로 적용됩니다" 로 함께 표시됩니다. 사용자가 약정서 지표 이름을 말한 경우 Planner가 검색어를 고정하므로 Worker는 바꾸지 못합니다.

## 4. 구조 변경 전후 비교

같은 6개 요청을 세 조건에서 1회씩 실행했습니다. 변경 전은 구조 전환 직전 커밋(`507da24`)입니다. LLM 호출 수는 `ChatOpenAI`의 실제 생성 호출을 셌습니다.

| 요청 | 조건 | 재계획 | 재계획 방법 | LLM 호출 | Tool 호출 | Worker 재시도 | 응답 시간 | 결과 |
|---|---|---|---|---|---|---|---|---|
| 대표 (한 번에 충족) | 변경 전 | 0 | — | 2 | 4 | — | 7.8s | 열연강판 충족 +15.29% |
| | Worker LLM 끔 | 0 | — | 2 | 4 | 0 | 8.1s | 같음 |
| | Worker LLM 켬 | 0 | — | 2 | 4 | 0 | 11.5s | 같음 |
| 재계획 | 변경 전 | 2 | recheck, alt_path | 2 | 5 | — | 7.2s | 열연강판 미충족 +8.99% |
| | Worker LLM 끔 | 2 | recheck, alt_path | 2 | 5 | 0 | 7.2s | 같음 |
| | Worker LLM 켬 | 2 | recheck, alt_path | 2 | 5 | 0 | 7.5s | 같음 |
| 상한 도달 | 변경 전 | 3 | next_candidate, recheck, alt_path | 2 | 6 | — | 7.5s | 철판 미충족 +11.44% |
| | Worker LLM 끔 | 3 | 같음 | 2 | 6 | 0 | 7.7s | 같음 |
| | Worker LLM 켬 | 3 | 같음 | 2 | 6 | 0 | 7.3s | 같음 |
| 품목명 불일치 (폴리프로필렌) | 변경 전 | 0 | — | 2 | 3 | — | 6.1s | **판단 불가** |
| | Worker LLM 끔 | 0 | — | 2 | 3 | 0 | 5.7s | **판단 불가**, 기준 지표 이름을 되물음 |
| | Worker LLM 켬 | 0 | — | **3** | 5 | **1** | 8.7s | **충족** +22.15% (합성수지) |
| 품목명 불일치 (구리선) | 변경 전 | 0 | — | 2 | 3 | — | 5.1s | **판단 불가** |
| | Worker LLM 끔 | 0 | — | 2 | 3 | 0 | 5.8s | **판단 불가**, 기준 지표 이름을 되물음 |
| | Worker LLM 켬 | 0 | — | **3** | 5 | **1** | 8.0s | **충족** +11.10% |
| Tool 불필요 | 세 조건 | 0 | — | 2 | 0 | — | 3.2 ~ 3.5s | 대화 응답 |

- **정상 경로와 제약 미충족 재계획은 세 조건이 같습니다.** 재계획 횟수, 방법, LLM 호출 수, Tool 호출 수가 모두 같습니다. Worker는 정상 경로에서 LLM을 부르지 않습니다. 대표 요청의 11.5s는 그 조건의 첫 실행이어서 걸린 시간이며, 같은 조건의 다른 요청은 변경 전과 0.5s 안쪽으로 차이가 납니다.
- **품목명이 ECOS와 다른 요청에서만 차이가 납니다.** 변경 전에는 품목 검색이 `NO_DATA`로 끝나 판단하지 못했습니다. Worker LLM을 켜면 Worker가 같은 작업 안에서 검색어를 고쳐 다시 부르므로, 재계획 없이 LLM 1회, Tool 2회, 약 2.6s를 더 써서 판단까지 이어집니다.
- **Worker LLM을 끄면 Planner가 요청을 처리합니다.** 이 두 요청에서는 Planner LLM 초안이 이미 사용자가 말한 이름으로 검색했으므로 Planner가 더할 작업이 없어, 재계획하지 않고 기준 지표 이름을 되물었습니다. 초안 검색어가 사용자가 말한 이름과 다르면 `worker_request` 재계획이 1회 일어납니다 (3절 7번).

## 5. 회귀 확인에서 발견한 점

| 발견 | 처리 |
|---|---|
| `check_review.py`의 진단 대상 검사가 이전 값(main, logging, tool)만 허용해 1건 FAIL | 진단이 planner, worker로 세분화되었으므로 확인 기준을 고침 → 16/16 PASS |
| 재계획 방법 설명에 `verify_fix`, `worker_request`를 한 문장으로 이어 붙이자, 재계획 상한 진단이 설계대로인 `next_candidate → recheck`를 "순서 위반"으로 지목 | 제약 미충족 재계획의 순서와 `F3021` 재계획을 두 문장으로 나누고, 순서대로 진행한 것이 정상임을 적음. 다시 2회 실행에서 두 번 모두 "순서대로 진행되어 정상, 조건 조정을 되물어야 함"으로 진단 |

## 6. 수정 내용

| 파일 | 내용 |
|---|---|
| [worker.py](../backend/app/agent/worker.py) | Worker 신설. 작업 수행, LLM 호출 조건, 고정 인자 보호, 재시도 상한, 수행 보고 |
| [executor.py](../backend/app/agent/executor.py) | Tool 한 번 호출(`invoke`)과 인자 준비(`prepare`)를 나눠 Worker가 재사용 |
| [planner.py](../backend/app/agent/planner.py) | 작업마다 고정 인자(`fixed`) 표시, `worker_request`, `verify_fix` 재계획 |
| [graph.py](../backend/app/agent/graph.py) | plan 노드가 작업 목록, execute 노드가 수행 보고, judge 노드가 검증을 메시지로 남김. `PlannerAgent(worker_decider=, worker=, verifier=)` |
| [reviewer.py](../backend/app/agent/reviewer.py), [logging_agent.py](../backend/app/agent/logging_agent.py) | `fixed_arg_changed` 검사, Worker 수행 직후 검증 `verify_round()`, `LoggingAgent.verification()`, 진단 대상(`agent`)에 planner, worker 추가 |
| [responder.py](../backend/app/agent/responder.py) | 다시 고쳐 확보한 근거 표시, Planner가 되물을 사항 |
| [trace.js](../frontend/js/trace.js) | Worker 재시도, Worker 판단, 로깅 Agent 검증 이상 표시 |
