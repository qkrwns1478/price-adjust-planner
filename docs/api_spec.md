# API 명세서

- 작성일: 2026-11-19
- 구현: [backend/app/api/](../backend/app/api/)
- FastAPI Docs: `http://127.0.0.1:8000/docs`

| 분류 | 메서드, 경로 | 요청 | 응답 | 요구사항 |
|---|---|---|---|---|
| 대화 | `POST /api/chat` | `ChatRequest` | `ChatResponse` | `F3017` |
| | `POST /api/chat/stream` | `ChatRequest` | NDJSON 스트림 | `F3017`, `F3019` |
| 계획 관리 | `POST /api/plans` | `PlanCreate` | 201 `PlanSaved` | `F3015` |
| | `GET /api/plans` |  | `PlanList` | `F3015` |
| | `GET /api/plans/{plan_id}` |  | `Plan` | `F3015` |
| | `PATCH /api/plans/{plan_id}` | `PlanUpdate` | `PlanSaved` | `F3016` |
| | `DELETE /api/plans/{plan_id}` |  | `PlanDeleted` | `F3016` |
| 실행 로그 | `GET /api/logs` |  | `SessionList` | `F3014` |
| | `GET /api/logs/{session_id}` |  | `SessionLog` | `F3014`, `F3020` |
| | `DELETE /api/logs/{session_id}` |  | `Deleted` | `F3014` |
| 설정 상태 | `GET /api/status` |  | `Status` | `F3019`, `NF3002` |

`/`는 프론트엔드(`frontend/`)를 제공합니다. 실행 로그 수정 API(`PUT`, `PATCH`)는 두지 않습니다.

## 공통

| 상태 코드 | 경우 | 본문 |
|---|---|---|
| 200, 201 | 성공 | 각 응답 모델 |
| 404 | 없는 계획, 세션 | `{"detail": "계획이 없습니다."}` / `{"detail": "실행 로그가 없습니다."}` |
| 422 | 형식 오류, 종료 시각 ≤ 시작 시각, 필수 값을 비우는 수정 | `{"detail": "..."}` 또는 FastAPI 검증 오류 목록 |

Agent 실행 중 예외가 발생해도 대화 API는 200과 `kind: "error"` 응답을 돌려줍니다. 응답에는 예외 원문을 포함하지 않습니다.

## 1. 대화

### `ChatRequest`

| 필드 | 형식 | 설명 |
|---|---|---|
| `session_id` | string, 선택 | 이어 갈 대화 세션. 비우면 새 세션 |
| `message` | string, 1 ~ 2000자 | 사용자 요청 |

```json
{"session_id": null, "message": "A사 납품 프레임이 개당 12만 원이고, 열연강판이 35%야. …"}
```

### `ChatResponse`

| 필드 | 형식 | 설명 |
|---|---|---|
| `session_id`, `run_id` | string | 세션, 요청 1건 식별자 |
| `kind` | `result` / `clarify` / `chat` / `notice` / `error` | 판단 결과 / 되물음 / 대화 / 안내 / 오류 |
| `text` | string | Markdown 응답 (면책 안내 포함) |
| `summary` | string | 대화 맥락에 남기는 한 줄 요약 |
| `intent`, `intent_label`, `intent_reason` | string | LLM이 분류한 의도와 이유 |
| `conditions` | object | `기준`, `선호`, `제약` 조건 |
| `changed` | string[] | 이번 요청에서 바뀐 조건 |
| `questions` | string[] | 되물을 질문 |
| `recommendations` | object[] | 요건을 충족한 항목과 요청 금액 |
| `criteria` | object[] | 항목별 지표, 기준값 → 현재값, 판단, 역산 기준, 추정 |
| `unverified` | object[] | API로 확인할 수 없어 사용자가 확인할 사항 |
| `excluded` | object[] | 제외한 후보와 사유 |
| `aux` | object[] | 근거 조문 (법령, 조문 번호, 제목, 발췌, 링크) |
| `evidence` | object[] | 근거 Tool 결과 (Tool, 성공 여부, 실패 코드, 요약) |
| `plan` | object | Tool 호출 계획과 재계획 라운드 |
| `replans` | object[] | 재계획 방법과 이유 |
| `stop_reason` | string | 재계획을 멈춘 사유 |
| `fatal` | string[] | 다시 호출해도 같은 실패 코드 (`INVALID_KEY`, `UPSTREAM_ERROR`) |
| `plan_drafts` | object[] | 저장할 계획 초안. 협의 날짜, 시각은 비어 있음 |
| `disclaimer` | string | 면책 문구 |
| `review` | object | 이상 징후 판정 `F3020` |
| `missing_keys` | string[] | 설정되지 않은 API 키 환경변수 이름 |

```json
{"session_id": "a93f…", "run_id": "7f1c…", "kind": "result",
 "summary": "조정 계획 수립: 열연강판 충족(+15.29%), 요청 6,422원",
 "intent": "plan", "intent_reason": "납품대금 조정 요건을 제시하고 이번 분기에 요청할 수 있는지 물었습니다.",
 "recommendations": [{"item": "열연강판", "rate_pct": 15.29, "amount": {"increase_won": 6422, "new_price_won": 126422}}],
 "replans": [], "stop_reason": null, "fatal": [], "missing_keys": [], "…": "…"}
```

### `POST /api/chat/stream`

`application/x-ndjson`. 한 줄에 JSON 하나를 발생 순서대로 보냅니다.

```json
{"type": "session", "session_id": "a93f…"}
{"type": "event", "event": {"session_id": "a93f…", "run_id": "7f1c…", "seq": 1, "type": "run_start", "node": null, "ts": "…", "data": {"message": "…"}}}
{"type": "event", "event": {"seq": 2, "type": "node_start", "node": "analyze", …}}
…
{"type": "response", "response": ChatResponse}
```

`event`의 `type`은 `run_start`, `node_start`, `node_end`, `tool_request`, `tool_response`, `tool_reused`, `tool_skipped`, `decision`, `agent_message`, `run_end` 입니다. 필드는 [agent_design.md](agent_design.md) 12.1을 따릅니다. 재계획 지점은 `decision`의 `data.kind == "replan"` 입니다.

같은 세션의 요청은 한 번에 하나씩 실행합니다.

## 2. 계획 관리

### `PlanCreate`

| 필드 | 형식 | 필수 | 설명 |
|---|---|---|---|
| `client` | string | ✅ | 위탁기업 |
| `item` | string | ✅ | 연동 근거 원재료 또는 에너지경비 |
| `meeting_date` | `YYYY-MM-DD` | ✅ | 협의 날짜 |
| `start_time`, `end_time` | `HH:MM` | ✅ | 협의 시작, 종료 (종료 > 시작) |
| `product` | string | | 납품 품목 |
| `item_kind` | `material` / `energy` | | 기본 `material` |
| `share_pct` | 0 ~ 100 | | 납품대금 중 비중(%) |
| `unit_price_won` | int ≥ 0 | | 조정 전 단가 |
| `index_code`, `index_name` | string | | ECOS 품목 코드, 지표 이름 |
| `base_month`, `current_month` | `YYYYMM` | | 기준월, 현재월 |
| `base_value`, `current_value` | number | | 기준값, 현재값 |
| `change_rate_pct`, `threshold_pct` | number | | 변동률, 약정 비율 |
| `increase_ratio_pct`, `increase_won`, `new_price_won` | number | | 인상률, 개당 요청 금액, 조정 후 단가 |
| `law_refs` | string[] | | 근거 조문 |
| `status` | `ready` / `requested` / `agreed` / `rejected` | | 기본 `ready` |
| `memo` | string | | 메모 |
| `session_id`, `run_id` | string | | 계획을 만든 대화 |

대화 응답의 `plan_drafts[i]`에 협의 날짜와 시각을 더해 그대로 보냅니다.

### `PlanSaved` (저장, 수정 응답)

| 필드 | 설명 |
|---|---|
| `plan` | 저장한 `Plan` (`PlanCreate` 값 + `id`, `created_at`, `updated_at`) |
| `overlaps` | 같은 날짜에 협의 시간이 겹치는 계획 (`PlanBrief`). 한쪽이 끝나는 시각에 다른 쪽이 시작하면 겹치지 않음 |
| `duplicates` | 같은 위탁기업, 품목, 원재료로 진행 중(`ready`, `requested`)인 계획 |

겹치거나 중복이어도 저장, 수정은 막지 않습니다.

```json
{"plan": {"id": 2, "client": "A사", "item": "열연강판", "meeting_date": "2026-11-25",
          "start_time": "14:00", "end_time": "15:00", "increase_won": 6422,
          "created_at": "2026-11-19T15:17:02+09:00", "…": "…"},
 "overlaps": [{"id": 1, "client": "B사", "item": "철근및봉강", "meeting_date": "2026-11-25",
               "start_time": "14:30", "end_time": "15:30", "status": "ready"}],
 "duplicates": []}
```

### 그 밖의 응답

| 모델 | 형식 |
|---|---|
| `PlanList` | `{"plans": [Plan]}` 협의 날짜, 시각 순 |
| `PlanUpdate` | `PlanCreate`의 값 중 바꿀 것만. 보내지 않은 값은 유지 |
| `PlanDeleted` | `{"id": 2, "deleted": true}` |

## 3. 실행 로그

| 모델 | 형식 |
|---|---|
| `SessionList` | `{"sessions": [{session_id, started_at, ended_at, runs, events, tool_calls, replans, anomalies, first_message}]}` 최근 세션부터 |
| `SessionLog` | `{"session_id", "events": [LogEvent], "reviews": [Review]}` |
| `LogEvent` | `{id, session_id, run_id, seq, type, node, ts, data}` 실행 순서 |
| `Review` | `{run_id, verdict: normal / anomaly / undetermined, warnings, checks: [{check, label, status: ok / warn / undetermined, message, items}], created_at, diagnosis}` |
| `Diagnosis` | `{status: ok / failed, agent, stage, text, created_at}` 이상 판정 실행에만 존재 |
| `Deleted` | `{"session_id", "deleted": 삭제한 이벤트 수}` |

## 4. 설정 상태

```json
{"ready": false,
 "keys": [{"env": "OPENAI_API_KEY", "usage": "LLM (요청 분석, 계획 수립)", "configured": true},
          {"env": "ECOS_OPEN_API", "usage": "한국은행 ECOS (원재료 품목, 물가지수)", "configured": false},
          {"env": "KEP_CO_API", "usage": "한국전력 (산업용 전기요금)", "configured": false},
          {"env": "LAW_API_OC", "usage": "법제처 (법령 조문)", "configured": false}]}
```

키 값은 포함하지 않고 설정 여부만 알려 줍니다.
