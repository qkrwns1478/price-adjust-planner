# 메인 Agent 실행 흐름 구현 `F3005` ~ `F3011`

- 작업일: 2026-11-18
- 범위: 메인 Agent 실행 흐름 구현, 도구 연결 확인, 확인 항목
- 결과: `unittest` 39/39 OK (Tool 22, Agent 17), `check_agent.py` 26/26 PASS (3회 연속)

## 1. 설계 결정

| 결정 | 근거 | 구현 |
|---|---|---|
| 그래프를 analyze → plan → execute → judge → respond로 구성하고, 재계획은 judge → plan으로 되돌린다 | 4.2 "분석 → 계획 → 실행 → 판단 → 응답 노드" | LangGraph `StateGraph`, 조건부 간선 2개 |
| 분석은 LLM 구조화 출력으로 하고, 말하지 않은 값은 `null`로 둔다 | ⑤ "뽑지 못한 조건은 임의로 채우지 않고 비워 둔다" | `Analysis`, `ConditionUpdate`. 계산의 출발점이 없으면 Tool을 부르기 전에 되묻는다(`find_missing`) |
| 첫 계획은 LLM이 Tool 명세를 보고 고르고, 코드가 검증, 보정한다 | 1.1 "상황에 따라 Tool을 선택", 2.3 "Agent는 설명만 보고 Tool을 고른다", 2.7 | `make_llm_selector()`가 `catalog()`로 `PlanDraft`를 받는다. `build_plan()`이 불필요한 단계는 빼고 필수 단계는 더하며, 사용자 조건 인자와 `args_from`을 채운다. 보정 내역은 `corrections` |
| 재계획 방법은 규칙으로 고른다 | 2.4 "충족하는 후보가 하나도 없을 때만 재계획, 상한 3회, 키 누락은 재계획하지 않음" | `next_candidate` → `recheck` → `alt_path`. `INVALID_KEY`, `UPSTREAM_ERROR`는 재계획하지 않는다 |
| 상세 조회는 첫 계획에서 `include_trend=true`로 한 번만 부른다 | ⑨ "이미 조회한 상세정보는 다시 부르지 않는다" | `recheck`는 새 호출 없이 받은 추세로 도달 시점을 추정한다. 같은 Tool, 인자는 캐시에서 재사용한다 |
| 계산과 응답 조립은 코드가 한다 | 2.3 "모델의 판단이 필요 없는 작업은 실행 흐름이 직접" | `judge()`가 변동률, 요청 금액, 요건 충족 지수(역산)를 계산하고, `build_response()`가 Tool 결과와 판단 결과만으로 응답을 만든다 |
| API로 확인할 수 없는 값은 조건 형태로 제시한다 | ⑧ "임의로 정하지 않고, 역산한 기준을 제시" | 직전 조정 시점, 약정서 지표 일치, 다음 지수 집계 시점, 사업장 실제 전기요금 (`unverifiable()`) |
| 대화 맥락은 조건과 요약만 남긴다 | ⑩ "변경된 조건만 갱신", "Tool의 원본 응답은 보관하지 않고 요약만" | `MemorySaver`(thread_id = 세션 id), `merge_conditions()`, 응답 후 `step_results`, `cache` 비움 |
| 키, 외부 API 오류는 일반 문구로 보여 준다 | `NF3002` | `FATAL_NOTICE`, 제외 사유에서도 Tool 메시지 원문을 싣지 않는다 |

## 2. 변경 파일

### 추가

| 파일 | 내용 |
|---|---|
| `backend/app/agent/config.py` | 모델 설정, `chat_model()`(추론 모델이면 `reasoning_effort="none"`), 재계획 상한, 법정 기준, 면책 문구 |
| `backend/app/agent/state.py` | `AgentState`, `CostItem`, `ConditionUpdate`, `Analysis`, `grouped()`, `merge_conditions()` |
| `backend/app/agent/analyzer.py` | LLM 분석기, 의도별 필수 조건 검사 |
| `backend/app/agent/planner.py` | `DraftStep`, `PlanDraft`, `make_llm_selector()`, `build_plan()`(초안 검증, 보정, 실패 시 규칙 계획), `choose_strategy()`, `apply_replan()` |
| `backend/app/agent/executor.py` | `run_pending()`(값 전달, 실패 후 진행, `SKIPPED`, 캐시), `by_tool()` |
| `backend/app/agent/judge.py` | `judge()`, `_forecast()`, `unverifiable()` |
| `backend/app/agent/responder.py` | `build_response()`, 대화 LLM, 근거 Tool 결과 요약 |
| `backend/app/agent/graph.py` | `build_graph()`, `PlannerAgent`(run, state, new_session) |
| `backend/tests/test_agent.py` | 단위 테스트 17개 (4절) |
| `backend/check_agent.py` | 실제 LLM, API 확인 26개 항목. 결과는 `data/agent/*.json` |

### 수정

| 파일 | 내용 |
|---|---|
| `.env.example` | `GMS_BASE_URL`, `LLM_MODEL`, `MAX_REPLAN` |
| `.venv` | `requirements.txt` 라이브러리 설치 (langgraph 1.2.10, langchain-openai 1.4.3 등) |

## 3. 대표 요청 결과

| 요청 | 계획, 호출 | 결과 |
|---|---|---|
| A사 프레임 12만 원, 열연강판 35%, 약정 5%, 기준 2025년 12월 | LLM 초안 3단계 + 보정 1단계(`주요 원재료` 조문) | 15.29% ≥ 5% 충족, 개당 6,422원 인상. 처리 8.4 ~ 9.5초 |
| C사 열연강판 30%, 약정 10%, 기준 2026년 3월 | 재계획 `recheck` → `alt_path` | 8.99% 미달, 약 1개월 뒤 도달 추정(추정 표시), 대안 경로 상생협력법 제22조의2. 후보, 상세 조회 각 1회 |
| D사 철판 40%, 약정 12% | 재계획 `next_candidate` → `recheck` → `alt_path` | 열연강판 8.99%, 중후판 11.44% 모두 미달, 상한 3회에서 멈추고 조건 조정을 되물음 |
| "같은 조건에서 원재료만 알루미늄으로" | 후속 요청 | `changed = ["items"]`, 비중 35% 와 나머지 조건 유지, 7,316원 |
| 인사, 사용법, 조건 부족, 대상 여부 판정 | 계획 생략 / 되물음 / 조문만 | Tool 호출 0건 / 질문 3개 / 8% < 10% 대상 아님 |

## 4. 검증

### 단위 테스트 (`test_agent.py`, LLM, 외부 API 없음)

| 테스트 | 확인 내용 |
|---|---|
| `ConditionTest` (2개) | 후속 요청에서 원재료만 바뀌고 비중을 이어 씀, 비어 있는 조건을 채우지 않고 되물을 목록에 올림 |
| `PlanTest` (6개) | 비중 미달 항목 제외, LLM 초안 검증(모르는 Tool, 지어낸 항목 제외, 빠진 후보 조회, 조문 보충, 고정 인자), 법정 기준 조문 보충, 대상 여부 판정에서 지수 조회 제외, 선택기 실패 시 규칙 계획, 값 전달, 실패 후 진행, 캐시 |
| `GraphTest` (9개) | 대표 흐름 금액, 역산 값, 인사 계획 생략, 되묻기, 재계획 재사용, 상한(`MAX_REPLAN=2`), 치명적 실패 재계획 안 함, 메시지 차단, 빈 값이면 다음 후보, 분석 실패 시 응답, 후속 요청 |

### 실제 호출 (`check_agent.py`)

- 확인 항목 11개와 "LLM이 Tool 명세로 Tool 선택"을 26개 검사로 나눠 확인
- 출력, `data/agent/*.json`, 코드, 문서에 `.env` 키 4종 값이 없음을 문자열 대조로 확인

## 5. 발생한 문제

| 문제 | 원인 | 해결 |
|---|---|---|
| 제안서 시나리오 2(형강 20%, 약정 8%)에서 재계획이 일어나지 않음 | 실제 변동률이 12.62% 로 요건 충족 | 재계획 사례를 열연강판 30%, 약정 10% 로 바꿈 |
| 첫 구현에서 계획을 규칙으로만 만들어 Tool 명세를 Agent가 읽지 않음 | 요구사항을 충분히 확인하지 않고 설계 | LLM 초안 + 코드 검증 구조로 변경 |
| `gpt-6-luna` 함수 호출이 400 오류 | chat/completions에서 추론 모델은 `reasoning_effort`가 `none` 이어야 함 | `chat_model()`이 추론 모델에 `reasoning_effort="none"`을 넣음 (`LLM_REASONING_EFFORT`로 변경 가능) |
| `bind_tools`로 고르게 하면 첫 Tool 하나만 고름 | 모델이 앞 Tool 결과를 기다리는 방식으로 응답 | Tool 명세를 넘기고 단계 목록을 구조화 출력(`PlanDraft`)으로 받음 |
| LLM이 고른 조문 검색어("납품대금 연동")로는 법정 비중 기준(제2조 12) 근거가 나오지 않음 | 판단에 쓴 기준과 LLM이 고른 근거가 다름 | 판단 기준의 근거 조문 검색은 검증 단계에서 반드시 더함 |
| 지수 발표일을 "11월 중순"으로 표시 | API로 확인하지 않은 날짜 | "2026년 10월 지수가 집계된 뒤"로 바꿈 |
| 모든 후보가 미달이면 첫 후보로 역산, 추정 | 대표 후보 선택 기준 누락 | 변동률이 가장 큰 후보로 계산 |
| `INVALID_KEY` 메시지 원문이 제외 사유에 실림 | 판단 단계에서 Tool 메시지를 그대로 사용 | 키, 외부 API 오류는 일반 문구로 바꿈 |
| "올해 얼마나 올랐어"에서 기준 시점을 뽑지 못함 | 기간 표현 환산 규칙 없음 | 분석 프롬프트에 "올해 → 작년 12월 대비" 규칙 추가 |
| 응답 문자열 수정 스크립트에서 `\n`이 실제 줄바꿈으로 바뀌어 `SyntaxError` | 셸 heredoc 안의 이스케이프 처리 | 해당 줄을 직접 수정. 이후 수정 스크립트는 파일로 작성해 실행 |

## 6. 남은 작업

- 계획 저장, 관리 의도(`manage`)는 준비 중 안내만 한다. `F3015`에서 구현한다.
- 실행 이벤트 발행 `F3012`은 이 작업 범위에 넣지 않았다.
- 제안서 3.5절의 재계획 예시(형강)는 실제 값과 맞지 않아 수정이 필요하다.
