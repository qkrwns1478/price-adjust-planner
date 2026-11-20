# 로깅 Agent와 실행 로그 구현 `F3012` ~ `F3014`

- 작업일: 2026-11-19
- 범위: 로깅 Agent와 실행 로그 구현, 확인 항목
- 결과: `unittest` 51/51 OK, `check_logging.py` 14/14 PASS

## 1. 설계 결정

| 결정 | 근거 | 구현 |
|---|---|---|
| 메인 Agent는 `EventBus`에 발행만 하고 구독자를 모른다 | ⑫ "이벤트 발행은 로깅 Agent의 구현에 의존하지 않아야 한다", 2.5 "로깅 Agent가 멈춰도 메인 Agent가 정상 응답" | `PlannerAgent(bus=)`는 버스만 받는다. 두 Agent는 `main.create_app()` 에서만 연결한다 |
| 구독자 예외는 버스에서 삼킨다 | ⑬ "저장에 실패해도 대화는 정상 동작" | `EventBus.publish()`가 핸들러마다 `try/except`. 로깅 Agent도 저장 실패 시 `failures`만 올린다 |
| 동기 발행, 즉시 저장 | ⑬ "받는 즉시 … 발생 순서대로 덧붙여 저장", "별도의 비동기 처리는 요구하지 않는다" | 발행 시점에 핸들러를 바로 호출하고, 저장소는 INSERT 1건으로 끝낸다 |
| 이벤트를 원본 그대로 저장 | ⑬ "LLM이 요약하거나 가공한 내용으로 원본 이벤트를 대신하지 않고" | `data`를 JSON 원문으로 저장한다. Tool 응답 이벤트에는 원본 응답(`result`)을 싣는다 |
| 세션 id와 별도로 요청 id(`run_id`), 순서(`seq`)를 둔다 | 2.5 "이벤트에 세션 식별자를 공통으로", ⑭ "실행 순서대로 조회" | 한 세션에 요청이 여러 건이어도 요청 단위로 나눠 볼 수 있다. `F3019` 화면, `F3020` 이상 징후 판정에서 요청 단위 집계에 쓴다 |
| 규칙 판단도 `decision`으로 발행한다 | ⑫ 는 LLM 판단만 요구. `F3019` "재계획이 발생한 지점을 구분하여 표시" | `by: llm / rule`로 구분한다. 재계획 지점을 로그만으로 찾을 수 있다 |
| 캐시 재사용, 건너뜀을 실제 호출과 다른 종류로 발행한다 | `F3020` "동일한 Tool이 같은 입력으로 반복 호출된 상황을 식별" | `tool_reused`, `tool_skipped`를 `tool_request`와 분리해, 재사용을 반복 호출로 세지 않게 한다 |
| 수정 불가를 API와 DB 양쪽에서 보장 | ⑭ "실행 로그의 내용은 수정할 수 없도록" | 수정 API 없음 + 저장소에 수정 메서드 없음 + `BEFORE UPDATE` 트리거 |
| Emitter를 `ContextVar`로 노드에 전달 | — | 그래프 상태(체크포인터에 저장됨)나 `configurable`에 직렬화할 수 없는 객체를 넣지 않기 위해서다. `PlannerAgent.run()`이 요청마다 바인딩하고, 요청 밖에서는 아무것도 발행하지 않는 Emitter를 돌려준다 |

## 2. 이벤트

| `type` | 발행 위치 | `data` |
|---|---|---|
| `run_start` / `run_end` | `PlannerAgent.run()` | 요청 메시지 / `ok`, `kind`, `intent`, `replan_count`, `stop_reason`, `elapsed_ms` |
| `node_start` / `node_end` | `_traced()`로 감싼 5개 노드 | — / `ok`, `elapsed_ms`, 노드별 요약 |
| `tool_request` / `tool_response` | `run_pending()` | `step`, `round`, `tool`, `args` / `ok`, `code`, `elapsed_ms`, `result` |
| `tool_reused`, `tool_skipped` | `run_pending()` | 재사용한 결과의 `ok`, `code` / 건너뛴 사유 |
| `decision` | analyze, plan, judge | `by`, `kind`(intent, clarify, plan, replan, stop), `choice`, `reason` |

`node_end` 요약: analyze는 `intent`, `intent_reason`, `changed`, `missing`, `error` / plan은 `round`, `strategy`, `source`, `steps`, `reused`, `corrections` / execute는 호출, 재사용, 건너뜀 수와 실패 목록 / judge는 항목별 `status`, `replan`, `stop_reason` / respond는 `kind`.

## 3. 변경 파일

### 추가

| 파일 | 내용 |
|---|---|
| `backend/app/events/bus.py` | `EventBus`(subscribe가 해지 함수 반환, publish가 구독자 예외 차단), `Emitter`(session_id, run_id, seq, ts 부여, 스레드 잠금), `EventType`, `current_emitter()` |
| `backend/app/storage/log_store.py` | `RunLogStore`: `run_events` 테이블과 `(session_id, id)` 인덱스, 트리거. `append`, `list_sessions`(요청 수, 이벤트 수, Tool 호출 수, 재계획 수, 첫 메시지), `get_session`(id 순), `delete_session`(삭제 건수). 경로는 `PLANNER_DB_PATH`, 기본 `data/planner.db` |
| `backend/app/agent/logging_agent.py` | `LoggingAgent`: `start(bus)`, `stop()`, `handle(event)`. LLM 관련 모듈을 import 하지 않는다 |
| `backend/app/api/logs.py` | 목록, 상세, 삭제 라우터와 Pydantic 응답 모델. 없는 세션은 404 |
| `backend/app/main.py` | `create_app(log_store, agent)`. `F3017`에서 대화, 계획 라우터를 더한다 |
| `backend/tests/test_logging.py` | 단위 테스트 12개 (4절) |
| `backend/check_logging.py` | 실제 호출 확인 14개 항목. 로그는 `data/logs/check.db`, 대표 요청 로그는 `data/logs/representative_log.json` |

### 수정

| 파일 | 내용 |
|---|---|
| `backend/app/agent/graph.py` | `_traced()`, `_summary()`, `_decision()` 추가. 판단 노드를 `judge_node()`(발행) 와 `decide()`(기존 판단) 로 나눴다. 분석 단계에서 되물을 조건이 있으면 `clarify`를 발행한다. `PlannerAgent`에 `bus` 인자, `run()`에 요청 단위 이벤트 |
| `backend/app/agent/executor.py` | `run_pending(emit=)` 인자 추가. 기본값은 아무것도 하지 않는 함수라 기존 호출부와 테스트가 그대로 동작한다 |
| `.env.example` | `PLANNER_DB_PATH` |

## 4. 검증

### 단위 테스트 (`test_logging.py`, LLM, 외부 API 없음)

| 테스트 | 확인 내용 |
|---|---|
| `EventTest.test_stage_tool_and_decision_events` | 5개 노드의 진입, 종료 짝, Tool 요청과 응답의 step 일치, `elapsed_ms`, 앞 Tool 결과(`item_code`)가 다음 요청 인자에 남음, LLM 판단 2건과 이유 |
| `EventTest.test_replan_and_reuse_events` | 재계획 판단이 `rule`로 남고, 계획 round가 0부터 이어지며, 같은 Tool과 인자의 `tool_request`가 중복되지 않음 |
| `EventTest.test_clarify_decision_skips_tools` | 조건이 모자라면 `clarify` 발행, Tool 요청 없음, 노드는 analyze → respond |
| `EventTest.test_tool_failure_event` | 실패한 Tool 응답에 `ok=false`, `code` |
| `EventTest.test_publish_does_not_depend_on_subscribers` | 구독자가 없을 때와 예외를 던질 때의 응답이 같음 |
| `LoggingAgentTest` (5개) | 세션별 분리와 저장 순서, 저장한 행이 발행한 이벤트와 필드까지 같음, LLM 차단 상태에서 저장, 저장소 예외에도 응답, `stop()` 뒤 저장 안 함, UPDATE 거부 |
| `LogApiTest` (2개) | 목록 순서와 집계 값, 상세 id 순서, 삭제 후 404, OpenAPI에 `get`, `delete`만 존재, `PUT`, `PATCH` 405 |

### 실제 호출 (`check_logging.py`)

- 대표 요청: 이벤트 22건, Tool 호출 4건(4 ~ 998ms), LLM 판단 2건(intent, plan)
- 재계획 요청: 이벤트 39건, `replan = recheck → alt_path`, `stop` 사유까지 기록
- 로깅 Agent 처리 중 `Completions.create`, `chat_model`을 차단 → 차단된 호출 0회, 저장 실패 0건
- 로깅 Agent를 멈춘 뒤 인사 요청 → `chat` 응답, 로그 없음
- API 목록 2개 세션, 상세 22건 실행 순서, 삭제 39건, 수정 경로 없음

## 5. 발생한 문제

| 문제 | 원인 | 해결 |
|---|---|---|
| 테스트 종료 시 임시 DB 삭제에서 `PermissionError [WinError 32]` | `with sqlite3.connect() as db`는 커밋만 하고 연결을 닫지 않는다. Windows는 열린 파일을 지울 수 없다 | `_connect()`를 컨텍스트 관리자로 바꿔 작업 1건마다 커밋 후 `close()` 한다. 서버에서도 연결이 쌓이지 않는다 |
| 수정 API 부재 테스트에서 `/api/logs` 경로를 찾지 못함 | FastAPI 0.141 은 `include_router()` 한 라우터를 `_IncludedRouter`로 감싸 `app.routes`에 경로가 펼쳐지지 않는다 | `app.openapi()["paths"]`로 경로와 메서드를 검사한다 |
| 저장 원본 비교 테스트 실패 | 비교할 필드 목록에서 `session_id`를 빠뜨림 | 테스트 수정 |

## 6. 남은 작업

- ~~`check_agent.py`는 `F3012` ~ `F3014` 변경 직후에는 다시 실행하지 않았다.~~ 추가 작업 뒤 실행해 26/26 PASS를 확인했다.
- 대화 요청 API는 `F3017`에서 `main.py`에 추가한다. 그 전까지 실행 로그는 `PlannerAgent.run()`을 직접 호출해야 쌓인다.
- `F3019` 화면은 `run_id`로 요청을 묶고 `decision.kind = replan`으로 재계획 지점을 표시한다.

---

# 추가 작업: 이상 징후 판단 `F3020`

- 범위: `F3020` 확인 항목
- 결과: `unittest` 69/69 OK, `check_review.py` 16/16 PASS (2회), `check_logging.py` 14/14, `check_agent.py` 26/26 PASS

## 1. 설계 결정

| 결정 | 근거 | 구현 |
|---|---|---|
| 판정은 규칙으로만 한다 | ① "판정은 규칙으로 하고", "이벤트를 대조하고 횟수를 센다" | `reviewer.evaluate_run()`은 LLM을 import 하지 않는다 |
| 판정 근거는 저장된 실행 로그뿐이다 | ① "로깅 Agent가 수집한 실행 로그를 근거로" | `run_end`를 받으면 메모리의 이벤트가 아니라 `store.get_run(run_id)`로 다시 읽어 판정한다. 저장에 실패한 이벤트가 있으면 순서 번호가 비어 판정 불가가 된다 |
| 근거가 없거나 서로 맞지 않으면 판정 불가 | ① "근거가 되는 이벤트가 없으면 '이상 없음'이 아니라 '판정 불가'" | 로그 온전성(시작, 종료, 빠진 번호)과 검사별 근거(실행 단계의 호출 수와 요청 이벤트 수, 재계획 수, 계획 단계 목록)를 대조한다 |
| `warn`이 `undetermined`보다 우선한다 | — | 근거가 일부 없어도 확인된 이상은 이상으로 보고 진단한다 |
| 재계획 상한 도달을 이상으로 본다 | ① "재계획의 과도한 반복" | 상한은 무한 반복을 막는 안전장치이고, 도달은 공개 지표로 조건을 충족하지 못했다는 신호이다. 같은 방법을 연달아 쓴 경우도 이상으로 본다. 상한은 `run_end.replan_limit`로 로그에 남겨 판정 시점의 설정에 기대지 않는다 |
| 캐시 재사용은 반복 호출로 세지 않는다 | ① "동일한 Tool이 같은 입력으로 반복 호출" | `tool_request`만 센다. `F3012`에서 `tool_reused`를 분리해 둔 것을 그대로 쓴다 |
| 계획 외 호출은 (step id, Tool) 로 대조한다 | ① "계획에 없던 Tool 호출" | 재계획 round의 단계까지 모든 `node_end(plan).steps`를 모아 비교한다 |
| 진단은 이상 실행에만, 별도 스레드에서 | ① "이상으로 판정한 실행에만 LLM으로", `NF3005` | `ThreadPoolExecutor(max_workers=1)`. 응답은 진단을 기다리지 않는다 |
| 진단은 다른 테이블에 저장 | ① "진단은 판정과 구분하여 저장해 경고 수에 섞지 않는다", "진단이 실패해도 판정과 실행 로그는 그대로 남는다" | `run_reviews`, `run_diagnoses`를 분리하고 둘 다 UPDATE를 막는다. 실패는 `status = failed`만 남긴다 |
| 진단은 세 문장으로 자른다 | ① "원인을 세 문장 이내로" | 프롬프트로 요청하고, 저장 전에 `limit_sentences()`로 한 번 더 자른다 |
| 메인 Agent는 판정을 읽는 함수만 받는다 | ① "판단 결과는 메인 Agent가 참조할 수 있는 형식으로", ⑫ "로깅 Agent의 구현에 의존하지 않아야" | `PlannerAgent(review_source=)`. `create_app()`이 `store.get_review`를 넣는다. 로깅 Agent가 `run_end`를 동기로 처리하므로 `run()`이 끝나기 전에 판정이 있다 |

## 2. 변경 파일

### 추가

| 파일 | 내용 |
|---|---|
| `backend/app/agent/reviewer.py` | `check_repeated_call()`, `check_excessive_replan()`, `check_unplanned_call()`, `evaluate_run()`, `Diagnosis`(agent, stage, text), `make_llm_diagnoser()`, `limit_sentences()` |
| `backend/tests/test_review.py` | 단위 테스트 18개 (3절) |
| `backend/check_review.py` | 실제 호출 확인 16개 항목. 로그는 `data/logs/review.db`, 결과는 `data/logs/reviews.json` |

### 수정

| 파일 | 내용 |
|---|---|
| `backend/app/agent/logging_agent.py` | `run_end`에서 `review()` 호출. `diagnoser`, `diagnose_async` 인자, `diagnoses` 카운터, `wait()` |
| `backend/app/storage/log_store.py` | `run_reviews`, `run_diagnoses` 테이블과 트리거. `get_run`, `add_review`, `add_diagnosis`, `get_review`, `list_reviews`. 세션 목록에 `anomalies`, 세션 삭제 시 판정, 진단도 삭제 |
| `backend/app/agent/graph.py` | `run_end`에 `replan_limit`. `PlannerAgent(review_source=)`, `review()`, 응답에 `review` |
| `backend/app/api/logs.py` | `Check`, `Diagnosis`, `Review` 모델. 상세 응답에 `reviews`, 목록에 `anomalies` |
| `backend/app/main.py` | `review_source` 연결 |

## 3. 검증

### 단위 테스트 (`test_review.py`, LLM, 외부 API 없음)

| 테스트 | 확인 내용 |
|---|---|
| `EvaluateTest` (10개) | 대표 요청, 인사 요청 이상 없음 / 반복 호출 식별과 경고 수 / `tool_reused`는 반복이 아님 / 상한 도달 / 같은 방법 연속 / 계획 외 호출 / 로그 없음, 종료 없음, 빠진 번호, 계획 이벤트 없음, 요청 이벤트 누락은 판정 불가 / `warn` 우선 / 소수점을 나누지 않는 문장 자르기 |
| `ReviewAgentTest` (6개) | 정상 실행은 진단기 0회 / 이상 실행은 진단 1회, 경고 수 그대로, 세 문장 / 판정 불가는 진단 안 함 / 진단 실패 시 판정, 로그 유지, 오류 원문 미저장 / 비동기 진단 / 판정, 진단 테이블 UPDATE 거부 |
| `MainAgentReferenceTest` (2개) | `create_app()` 연결 시 응답 `review`, API `reviews`, `anomalies`, 세션 삭제 시 판정 삭제 / 로깅 Agent가 없으면 `review = None` 이고 응답 정상 |

### 실제 호출 (`check_review.py`)

- 정상: 대표 요청 `normal`, 진단 LLM 0회, 응답 `review.verdict = normal`
- 재계획 과도 반복: 실제 요청으로 상한 3회 도달 → `anomaly`, 진단 `main/plan`
- 반복 호출, 계획 외 호출: 실제 로그에 호출을 끼워 넣어 발행 → 각각 `anomaly`, 진단 `main/execute`
- 판정 불가 2건: 진단 LLM 0회
- 진단 LLM 호출 3회 = 이상 실행 3건, 경고 수 `[1, 1, 1]`, 진단 3 / 2 / 2문장
- 진단 실패 시 `diagnosis.status = failed`, 판정과 로그 유지

## 4. 발생한 문제

| 문제 | 원인 | 해결 |
|---|---|---|
| 재계획 진단이 `recheck`의 빈 계획을 원인으로 지목 | 진단 LLM이 재계획 방법의 설계를 모름 | 프롬프트에 재계획 방법 순서, `recheck`는 새 Tool 호출이 없는 것이 정상임, 상한 도달은 조건 미충족으로도 생긴다는 점을 적음. 재실행에서 빈 단계를 정상으로 보고 원인을 약정 조건과 공개 지표의 차이로 진단 |
| 문장 자르기 첫 구현이 `8.99%`를 두 문장으로 나눔 | 문장 부호마다 나누는 정규식 | 문장 부호 뒤에 공백이 있을 때만 나눔 |

## 5. 남은 작업

- 메인 Agent는 판정을 응답에 싣기만 한다. 이상 판정에 따라 작업 목록을 다시 구성하는 일은 `F3021`(Planner, Worker 분리)에서 한다.
- 재계획 진단은 응답 본문을 보지 못한다. `respond` 종료 이벤트에 `kind`만 있어서, 실제로는 조건 조정을 되물었는데도 "되물었어야 한다"고 진단했다. `F3021`에서 응답 요약을 이벤트에 더할지 정한다.
