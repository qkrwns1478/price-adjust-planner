# Planner·Worker 구조 전환 `F3021`

- 작업일: 2026-11-19
- 범위: `F3021` 확인 항목. 심화 기능 중 `F3022`, `F3023`은 적용하지 않기로 정함
- 결과: `unittest` 103/103 OK, `check_worker.py` 17/17 PASS (실제 LLM, 외부 API). 구조 변경 전후, Worker LLM 켬과 끔을 같은 6개 요청으로 비교했다

## 1. 설계 결정

| 결정 | 근거 | 구현 |
|---|---|---|
| 기존 그래프를 유지하고 plan 노드를 Planner, execute 노드를 Worker로 둔다 | 심화 "필수 기능으로 구현한 Agent 실행 흐름과 세 개의 Tool, 실행 기록 수집 구조를 유지한 상태에서" | `Worker` 클래스(worker.py)가 execute 노드를 맡는다. 노드 이름과 이벤트 형식은 그대로 두어 `F3020` 판정과 화면이 계속 동작한다 |
| Planner가 작업마다 고정 인자를 표시한다 | `F3021` "Planner가 사용자 조건으로 고정한 인자는 바꾸지 않아야 한다" | Tool 입력 규격에서 품목, 조문 검색의 `keyword`, `max_results`만 고칠 수 있다. 나머지와 `args_from`은 `fixed`. 약정서 지표 이름을 말했으면 검색어도 고정 |
| Worker LLM은 고칠 여지가 있는 실패에서만 부른다 | `F3021` "결과 없음, 대상 없음, 잘못된 입력을 반환했을 때만", 예시 "치명적 실패, 필수 인자 누락 … LLM에게 넘기지 않는다" | `BAD_ARGUMENT`, `NOT_FOUND`, `NO_DATA` 이고 고칠 인자가 남아 있을 때만. LLM에 묻지 않은 경우도 `decision(kind=worker, by=rule)`에 사유를 남긴다 |
| 고정 인자 보호는 Worker 코드가 한다 | 같은 항목 | LLM이 고정 인자를 바꾸라고 해도 반영하지 않고 `ignored`로 남긴다. 이미 시도한 인자면 다시 부르지 않고 Planner에 보고 |
| 재시도는 같은 작업 id로 한다 | `F3021` "재시도는 같은 Tool 로만 상한 횟수 안에서" | `attempt`를 붙여 부른다. 계획 외 호출, 같은 입력 반복 호출로 판정되지 않는다. 상한 `WORKER_MAX_RETRY` (기본 2) |
| 세 Agent 사이의 메시지를 이벤트로 남긴다 | `F3021` "정해진 형식으로 주고받아야", 4.6 "메시지가 실행 로그에 남는지" | `agent_message` 이벤트: `task_list`, `work_report`, `verification` |
| 수행 결과 검증은 Worker가 끝날 때마다 로깅 Agent가 한다 | `F3021` "수행 결과의 검증은 로깅 Agent가 담당하며, 이상으로 판단되면 Planner가 작업 목록을 다시 구성" | 로깅 Agent가 `execute` 종료 이벤트를 받으면 규칙으로 검증하고(`verify_round`), Planner는 `verifier` 함수로만 읽는다. 메인 Agent는 로깅 Agent를 알지 못한다 |
| 고정 인자 변경 검사를 더한다 | 검증할 대상이 Worker의 수행 결과 | `fixed_arg_changed`. 요청 종료 판정 `F3020`에도 넣어 진단까지 이어진다 |
| Planner는 Worker 요청을 규칙으로 처리한다 | `F3021` "규칙으로 판단할 수 있는 상황에서는 LLM을 호출하지 않는다" | 사용자가 말한 원재료 이름으로 다시 찾기, 쓰지 않은 법정 기준 검색어로 다시 찾기. 작업으로 바꿀 수 없으면 기준 지표 이름을 되묻는다 |
| `F3022`, `F3023`은 적용하지 않는다 | 심화 "아래 과제 중 선택하여 수행" | 판단 근거는 법제처 조문 원문과 한국은행 지수로 확보하므로 해설 문서 검색이 필요 없고, 협의 일정 겹침은 `F3015`에서 알리며 일정이 조정 요건 판단에 영향을 주지 않는다 |

## 2. 변경 파일

### 추가

| 파일 | 내용 |
|---|---|
| `backend/app/agent/worker.py` | `Worker`, `WorkerDecision`, `make_llm_decider()`, `adjustable_args()` |
| `backend/tests/test_worker.py` | 단위 테스트 13개: 고정 인자 표시, 정상 경로 LLM 0회, 재시도와 고정 인자 유지, 고정 인자 변경 무시, 치명적 실패와 필수 인자 누락, 재시도 상한, LLM 끔, 계획 변경 요청, 건너뜀, 검증과 재구성 |
| `backend/check_worker.py` | 실제 LLM, 외부 API로 `F3021` 확인 17개 |
| `backend/compare_worker.py` | 구조 변경 전후, Worker LLM 켬과 끔 비교 |

### 수정

| 파일 | 내용 |
|---|---|
| `backend/app/agent/executor.py` | `prepare`, `invoke`, `skip`으로 나눔. 버린 작업(`discarded`)은 호출하지 않음 |
| `backend/app/agent/planner.py` | `fixed`, `no_retry`, `request_actions()`, `suspect_steps()`, `worker_request`, `verify_fix` 재계획, 다시 찾은 검색으로 다음 후보 고르기 |
| `backend/app/agent/graph.py` | 작업 목록, 수행 보고, 검증 메시지 발행, 검증과 Worker 요청 반영, `PlannerAgent(worker_decider=, worker=, verifier=)` |
| `backend/app/agent/reviewer.py` | `fixed_arg_changed`, `verify_round()`, 진단 대상에 planner, worker 추가 |
| `backend/app/agent/logging_agent.py` | `verify()`, `verification()` |
| `backend/app/agent/judge.py` | 버린 작업의 결과를 판단에 쓰지 않음 |
| `backend/app/agent/responder.py` | 근거 Tool 결과에 재시도로 확보한 검색어, "다시 알려 주실 사항", `planner_notes` |
| `backend/app/agent/state.py`, `config.py`, `events/bus.py`, `main.py` | 상태 필드, `WORKER_LLM`, `WORKER_MAX_RETRY`, `agent_message`, `verifier` 연결 |
| `frontend/js/trace.js`, `labels.js` | 재시도 표시, Worker 판단, 로깅 Agent 검증 이상, Planner, Worker 단계 이름 |
| `backend/tests/test_review.py`, `backend/check_review.py` | 판정 검사 수 4개, 진단 대상 확인 기준 |
| `backend/app/tools/registry.py` | 로컬 서버 API 툴 안내 주석 정리 |

## 3. 확인 중 발견한 점

| 발견 | 처리 |
|---|---|
| 실제 ECOS에서 `폴리프로필렌`, `PP`, `구리선`, `알루미늄판`, `스테인리스`가 `NO_DATA`, 법령 검색어 `연동제`도 `NO_DATA` | 결과 없음 재현 요청으로 사용 |
| Planner LLM 초안이 사용자가 말한 이름을 그대로 검색어로 쓰면, Worker LLM을 끈 경우 Planner가 더할 작업이 없음 | 재계획하지 않고 기준 지표 이름을 되묻는다. 비교표에 그대로 기록 |
| 재계획 뒤 다음 후보를 고를 때 첫 검색 단계만 보던 규칙 | 같은 원재료의 마지막 성공 검색과 그 검색에 이어진 지수 조회만 보도록 고침 |
| 진단 프롬프트에 새 재계획 방법을 한 문장으로 이어 붙이자, 진단 LLM이 설계대로인 `next_candidate → recheck`를 순서 위반으로 지목 | 제약 미충족 재계획의 순서와 `F3021` 재계획을 두 문장으로 나눔. 2회 다시 실행해 두 번 모두 바르게 진단 |
| `check_review.py`가 진단 대상으로 이전 값만 허용 | planner, worker를 허용. 회귀 확인 `check_agent` 26/26, `check_logging` 14/14, `check_review` 16/16, `check_plans` 11/11 PASS |
