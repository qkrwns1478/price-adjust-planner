# 검증 결과: 로깅 Agent와 실행 로그 `F3012` ~ `F3014`

- 실행일: 2026-11-19
- 확인 항목을 기록합니다.
- 설계: [agent_design.md](../agent_design.md) 12절

## 1. 실행 방법

```bash
# 실제 LLM(OpenAI)과 외부 API를 호출하는 확인: 14개 항목
python backend/check_logging.py

# LLM과 외부 API 없이 실행하는 단위 테스트: 로깅 12개 + Agent 17개 + Tool 22개
python -m unittest discover -s backend/tests -v
```

| 실행 | 결과 |
|---|---|
| `backend/check_logging.py` | **14/14 PASS** (요청 3건, 모델 `gpt-6-luna`) |
| `unittest` | **51/51 OK** |

대표 요청 1건의 전체 실행 로그는 `data/logs/representative_log.json`에 남깁니다 (형상관리 제외).

## 2. 확인 항목

| # | 확인 항목 | 확인 방법, 실제 결과 | 결과 |
|---|---|---|---|
| 1 | 단계 진입, 종료와 Tool 호출 요청, 응답, 소요 시간이 이벤트로 발행되는지 | 대표 요청 이벤트 22건. `node_start`, `node_end`가 `analyze → plan → execute → judge → respond`로 짝을 이룸. `tool_request`, `tool_response` 4쌍, `elapsed_ms` 4 ~ 998ms (3절) | PASS |
| 2 | LLM 판단(의도 분류 등)과 이유가 이벤트에 남는지 | `decision[llm] intent = plan`, `decision[llm] plan = [search_material_item, get_material_price_index, search_law_article]`에 각각 이유. `analyze` 종료 이벤트에도 `intent`, `intent_reason` | PASS |
| 3 | 로깅 Agent가 수집, 저장 중 LLM을 호출하지 않는지 | 로깅 Agent가 이벤트를 처리하는 동안 `Completions.create`, `chat_model`을 호출하면 실패하도록 막음 → 차단된 호출 0회, 저장 실패 0건 | PASS |
| 4 | 로깅 Agent를 연결하지 않아도 메인 Agent가 정상 동작하는지 | `logger.stop()` 후 "안녕하세요, 어떻게 쓰는 거예요?" → `chat` 응답, 로그 없음. 단위 테스트: 구독자가 예외를 던져도 응답이 같음, 저장소가 실패해도 `result` 응답 | PASS |
| 5 | 세션 단위로 구분하여 저장하고, 서로 다른 세션이 섞이지 않는지 | 대표 요청 세션 22건, 재계획 요청 세션 39건. 각 세션의 모든 행이 자기 `session_id`. 저장한 행이 발행한 이벤트와 필드까지 같음 | PASS |
| 6 | 목록, 상세(실행 순서), 삭제가 동작하는지 | `GET /api/logs` 세션 2개, `GET /api/logs/{id}` `run_start … run_end` 22건 `id` 오름차순, `DELETE` → `deleted: 39`, 이후 상세 404, 다른 세션은 유지 | PASS |
| 7 | 실행 로그 수정 API가 없는지 | OpenAPI 경로: `GET /api/logs`, `GET`, `DELETE /api/logs/{session_id}` 뿐. `PUT`, `PATCH` → 405. DB의 `UPDATE`는 트리거가 거부 | PASS |

## 3. 대표 요청 실행 로그

요청: "A사 납품 프레임이 개당 12만 원이고, 열연강판이 35%야. 약정서에는 5% 이상 변동하면 분기마다 조정한다고 돼 있어. 기준 시점은 작년 12월이야. 이번 분기에 요청할 수 있어?"

```text
  1 run_start
  2 node_start    analyze
  3 decision               [llm] intent = "plan" / 납품 조건과 약정 요건을 제시하고 이번 분기에 조정을 요청할 수 있는지 물었습니다.
  4 node_end      analyze  2812ms {"intent": "plan"}
  5 node_start    plan
  6 decision               [llm] plan = ["search_material_item", "get_material_price_index", "search_law_article"] / 사용자는 분기 조정 요청 가능성을 물었으므로 열연강판 지수 변동률을 기준과 비교하고, 판단 근거 조문도 함께 확인해야 합니다.
  7 node_end      plan     2208ms {"round": 0, "strategy": "initial", "reused": []}
  8 node_start    execute
  9 tool_request  execute  s1 search_material_item {"keyword": "열연강판"}
 10 tool_response execute  s1 search_material_item ok=True  4ms
 11 tool_request  execute  s2 get_material_price_index {"base_month": "202512", "include_trend": true, "item_code": "307125AA"}
 12 tool_response execute  s2 get_material_price_index ok=True  953ms
 13 tool_request  execute  s3 search_law_article {"law_name": "상생협력법", "keyword": "주요 원재료 납품대금 연동"}
 14 tool_response execute  s3 search_law_article ok=True  998ms
 15 tool_request  execute  s4 search_law_article {"law_name": "상생협력법", "keyword": "주요 원재료"}
 16 tool_response execute  s4 search_law_article ok=True  383ms
 17 node_end      execute  2381ms {"called": 4, "reused": 0}
 18 node_start    judge
 19 node_end      judge    0ms {"replan": null}
 20 node_start    respond
 21 node_end      respond  0ms {"kind": "result"}
 22 run_end                {"ok": true, "kind": "result", "intent": "plan", "replan_count": 0, "stop_reason": null}
```

## 4. 재계획 요청의 판단 이벤트

요청: "C사 납품 브래킷은 열연강판이 원가의 30%고, 약정 비율은 10%야. 기준 시점은 올해 3월이야. 이번 달에 조정 요청할 수 있어?"

```text
  3 decision  [llm]  intent  = "plan"
  6 decision  [llm]  plan    = [search_material_item, get_material_price_index, search_law_article, search_law_article]
 19 decision  [rule] replan  = "recheck"  / 변동률이 약정 비율에 못 미쳐, 지금 요청하는 계획 대신 다음 지수 발표 때 다시 조회하는 계획으로 바꿉니다.
 26 decision  [rule] replan  = "alt_path" / 연동 요건을 충족하지 못해, 연동이 아닌 공급원가 변동에 따른 조정 신청 근거를 찾습니다.
 35 decision  [rule] stop               / 연동이 아닌 조정 신청 경로까지 확인해 탐색을 마쳤습니다.
```

목록 조회에서 이 세션은 `replans: 2`, `tool_calls: 5`로 나타납니다.
