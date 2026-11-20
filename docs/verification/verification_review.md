# 검증 결과: 로깅 Agent 이상 징후 판단 `F3020`

- 실행일: 2026-11-19
- `F3020` 확인 항목을 기록합니다.
- 설계: [agent_design.md](../agent_design.md) 13절

## 1. 실행 방법

```bash
# 실제 LLM(OpenAI)과 외부 API를 호출하는 확인: 16개 항목
python backend/check_review.py

# LLM과 외부 API 없이 실행하는 단위 테스트: 판정 18개 + 로깅 12개 + Agent 17개 + Tool 22개
python -m unittest discover -s backend/tests -v
```

| 실행 | 결과 |
|---|---|
| `backend/check_review.py` | **16/16 PASS** (2회 실행, 모델 `gpt-6-luna`) |
| `backend/check_logging.py`, `backend/check_agent.py` | 14/14, 26/26 PASS (변경 후 회귀 확인) |
| `unittest` | **69/69 OK** |

판정과 진단은 `data/logs/reviews.json`에 남깁니다 (형상관리 제외).

## 2. 이상 실행을 만든 방법

| 이상 | 만든 방법 |
|---|---|
| 재계획 과도 반복 | 실제 요청 "D사 납품 커버는 철판이 40%야. 약정 비율 12%, …" → `next_candidate → recheck → alt_path` 3회로 상한 도달 |
| 같은 입력 반복 호출 | 메인 Agent는 캐시로 재호출하지 않는다. 실제 대표 요청 로그의 `s2 get_material_price_index` 요청, 응답을 한 번 더 넣고 `execute` 종료의 `called`를 맞춰 새 요청으로 발행 |
| 계획에 없던 호출 | 메인 Agent는 계획의 단계만 호출한다. 실제 대표 요청 로그에 `s9 get_industrial_power_price` 요청, 응답을 넣어 새 요청으로 발행 |
| 판정 불가 | 대표 요청 로그에서 `run_end`를 뺀 실행, `node_end(plan)`을 뺀 실행 |

## 3. `F3020` 확인 항목

| # | 확인 항목 | 실제 결과 | 결과 |
|---|---|---|---|
| 1 | 같은 Tool을 같은 입력으로 반복 호출한 실행을 식별하는지 | `repeated_call warn`: "같은 입력으로 다시 호출했습니다: get_material_price_index 2회", 근거 seq 11, 13 | PASS |
| 1 | 재계획을 과도하게 반복한 실행을 식별하는지 | `excessive_replan warn`: "재계획 3회로 상한 3회에 도달했습니다." | PASS |
| 1 | 계획에 없던 Tool을 호출한 실행을 식별하는지 | `unplanned_call warn`: "계획에 없던 호출: s9 get_industrial_power_price". 나머지 두 검사는 `ok` | PASS |
| 2 | 근거 이벤트가 없을 때 '이상 없음'이 아니라 '판정 불가'인지 | `run_end` 없음 → 세 검사 모두 `undetermined`("요청 종료 이벤트가 없습니다."). 계획 이벤트 없음 → `unplanned_call undetermined`("Tool을 호출했지만 계획 이벤트가 없습니다."), `verdict = undetermined` | PASS |
| 3 | 판단 결과를 메인 Agent가 참조할 수 있는지 | `PlannerAgent.run()` 응답의 `review.verdict = normal`, `review.run_id`가 저장된 판정과 같음. API 상세 `reviews[]`, 목록 `anomalies` | PASS |
| 4 | 이상 실행에만 진단이 붙고, 정상 실행에는 LLM이 호출되지 않는지 | 정상 1건, 판정 불가 2건: 진단 LLM 0회, `diagnosis = null`. 이상 3건: 진단 LLM 3회, 모두 `status = ok` | PASS |
| — | 진단을 판정과 구분해 남기고 경고 수에 섞지 않는지 | `run_diagnoses` 별도 테이블. `warnings = [1, 1, 1]`이 `warn` 항목 수와 같음 | PASS |
| — | 진단이 세 문장 이내인지 | 3, 2, 2문장 | PASS |
| — | 진단이 실패해도 판정과 실행 로그가 남는지 | 진단기가 예외를 던지게 함 → `verdict = anomaly`, `diagnosis.status = failed`, 이벤트 수 그대로 | PASS |
| — | 진단이 응답을 늦추지 않는지 | 재계획 요청 응답 7.6초, 응답 시점 `review.diagnosis = null`, 이후 진단 저장 | PASS |

## 4. LLM 진단

| 이상 | 진단 (`agent/stage`) |
|---|---|
| 재계획 과도 반복 | `main/plan` 문제는 메인 Agent의 재계획 진행입니다. next_candidate→recheck→alt_path 순서는 지켰고 recheck에 빈 단계도 적절하지만, 사용자가 약정한 12% 요건이 공개 지표에서 충족되지 않아 재계획 상한에 도달했으므로 조건을 조정할지 되묻거나 현재 기준으로 요청 가능 여부와 대체 조정 경로를 안내했어야 합니다. |
| 같은 입력 반복 호출 | `main/execute` 같은 단계 s2에서 get_material_price_index를 동일 인자(base_month 202512, include_trend true, item_code 307125AA)로 seq 11과 13에 두 번 요청했습니다. 실행 기록은 재사용(tool_reused) 대신 재호출한 것으로 나타나므로, 실행기가 캐시 결과를 사용해 중복 호출하지 않도록 고쳐야 합니다. |
| 계획에 없던 호출 | `main/execute` 문제는 메인 Agent의 실행 단계입니다. 계획에는 s1~s4만 기록되어 있는데, 실행에서 계획에 없는 s9 get_industrial_power_price를 호출했습니다. 실행기는 계획된 단계 ID의 Tool만 호출하도록 고치고, 이 호출은 계획에서 제거해야 합니다. |

첫 실행에서는 재계획 진단이 "31번 계획은 단계 없이 끝났는데도 재계획했다"며 `recheck`의 빈 단계를 원인으로 지목했습니다. `recheck`는 새 Tool 호출 없이 받은 추세로 추정하는 설계이므로, 진단 프롬프트에 재계획 방법의 순서와 `recheck`의 빈 단계가 정상임을 적었습니다. 두 번째 실행에서는 빈 단계를 적절하다고 보고, 원인을 약정 조건과 공개 지표의 차이로 진단했습니다.
