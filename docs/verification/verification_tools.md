# 검증 결과: Tool 확인 `F3001` ~ `F3004`

- 실행일: 2026-11-18
- 확인 항목과 Tool 설계 확인 결과를 기록합니다.
- Tool 명세: [tool_spec.md](../tool_spec.md)

## 1. 실행 방법

```bash
# 실제 외부 API를 호출하는 확인: 30개 항목
python backend/check_tools.py

# 외부 API 없이 모의 응답으로 실행하는 단위 테스트: 22개
python -m unittest discover -s backend/tests -v
```

| 실행 | 결과 |
|---|---|
| `backend/check_tools.py` | **30/30 PASS**, 4번 연달아 실행해 모두 통과 (회당 8.3~8.7초, 품목 목록 캐시가 없는 첫 실행 10.3초) |
| `unittest` | **22/22 OK** (0.011초) |

모든 키는 `.env`의 정식 키(`ECOS_OPEN_API`, `LAW_API_OC`, `KEP_CO_API`)로 실행했습니다. 처음 확인할 때는 ECOS 정식 키가 없어 공개 키 `sample`을 썼고, 그때는 첫 실행에 약 4분 25초가 걸렸습니다 (호출당 10행 한도로 품목 목록 1,577행을 158번에 나눠 받음). 정식 키는 1,000행씩 받아 2번이면 됩니다.

## 2. 확인 항목

| # | 확인 항목 | 확인 방법 (`check_tools.py` 항목) | 결과 |
|---|---|---|---|
| 1 | 등록한 Tool의 이름, 설명, 입력 스키마 목록을 출력하여 명세와 일치하는지 | 1절 Tool 목록 출력 ↔ [tool_spec.md](../tool_spec.md) 3절 대조. `catalog()`가 `name`, `description`, `parameters`만 주는지는 단위 시험 `test_catalog_exposes_only_name_description_parameters` | PASS |
| 2 | Tool마다 정상 호출과 실패 호출(잘못된 입력, API 키 없음 등)이 예외 없이 실패 응답 형식으로 반환되는지 | 4개 Tool 모두 정상, 빈 입력, 형식 틀림, 키 없음 실행. 한전은 틀린 키도 실행. 예외, 시간 초과, 해석 불가 응답은 단위 시험 `test_run_never_raises`, `test_timeout_is_upstream_error_without_key`, `test_non_json_response` | PASS |
| 3 | 후보 목록 Tool에 기준 날짜를 주지 않았을 때 정의한 기본값이 적용되는지 | `search_material_item(keyword="열연강판")` → `requested_as_of: null`, `as_of: "202609"`, `note: "기준월을 지정하지 않아 최신 집계월(202609)을 기본값으로…"` | PASS |
| 4 | 요청한 기준과 실제로 조회한 기준이 다른 경우(미래 날짜) 그 사실이 응답에 밝혀지는지 | `as_of="202612"` → `as_of: "202609"`, `estimated: true`, `note: "…아직 집계되지 않아…이 값은 추정입니다."` 상세, 한전 Tool도 같은 형식으로 확인 | PASS |
| 5 | 후보 목록 Tool이 반환한 식별자를 상세정보 Tool에 넘겨 조회가 이어지는지 | `search_material_item`의 `candidates[0].item_code`(307125AA)를 그대로 `get_material_price_index`에 넣음 → 130.58 → 150.55, `change_rate_pct: 15.29` | PASS |
| 6 | 판단에 필요한 속성이 비어 있는 항목이 임의의 값으로 채워지지 않고 판단하지 않은 채 남는지 | 실제: `base_month="190001"`(집계 전) → `NO_INDEX_VALUE`. 한전 `200001` → `NO_INDEX_VALUE`. 모의: ECOS `DATA_VALUE: ""`, 한전 `bill: ""` → `NO_INDEX_VALUE` (`test_empty_value_is_not_filled`, `test_empty_bill_is_not_filled`) | PASS |
| 7 | 보조 정보 검색 Tool에서 결과가 없는 경우의 응답 형식이 정의대로 나오는지 | `search_law_article("상생협력법", "우주정거장")` → `{"ok": false, "error": {"code": "NO_DATA", "message": "대ㆍ중소기업 상생협력 촉진에 관한 법률에서 '우주정거장'가 들어간 조문을 찾지 못했습니다."}}` | PASS |

추가로 확인한 것: 출력과 코드, 캐시 파일에 `.env`의 키 값이 들어 있지 않음을 문자열 대조로 확인했습니다. `NF3001` `NF3002`

## 3. 기획 검증: Tool 설계 확인

### 3.1 외부 API 호출 확인

| API | 호출 | 결과 |
|---|---|---|
| ECOS 품목 목록 `StatisticItemList/404Y014` | `sample` 키 → `.env`의 `ECOS_OPEN_API` | 둘 다 성공. 1,577행(연, 분기, 월 주기 포함), 월간 최신 집계월 202609. 정식 키는 호출당 1,000행 |
| ECOS 통계 조회 `StatisticSearch/404Y014/M` | `sample` 키 → `.env`의 `ECOS_OPEN_API` | 둘 다 성공. 열연강판 202512 = 130.58, 202609 = 150.55 (제안서 2.1절 값과 같음) |
| 한전 계약종별 전력사용량 | `.env`의 `KEP_CO_API` | 성공. 대구 산업용(`cntrCd=300`) 시군구 9곳, 최신 집계월 202609 (202610, 202611은 404) |
| 법제처 법령 검색, 본문 | `.env`의 `LAW_API_OC` | 1차: `type=JSON`은 **실패**("미신청된 목록/본문에 대한 접근입니다"), `type=XML`만 성공. JSON 형식 추가 신청 뒤 2차: `type=JSON` 성공. 상생협력법(MST 290193, 시행 2026-09-29) 본문 JSON 약 175KB, 조문단위 89개 |

### 3.2 실제 응답과 Tool 출력 구조 대조

| Tool 출력 필드 | 채우는 응답 값 | 들어 있는가 |
|---|---|---|
| `search_material_item.candidates[].item_code`, `item_name` | `ITEM_CODE`, `ITEM_NAME` (`CYCLE=M` 행) | ✔ |
| `… .first_month`, `latest_month` | `START_TIME`, `END_TIME` | ✔ |
| `get_material_price_index.base.value`, `current.value` | `DATA_VALUE` (문자열 → 실수) | ✔ |
| `… .unit` | `UNIT_NAME` ("2020=100") | ✔ |
| `get_industrial_power_price.*.unit_cost_won_per_kwh` | Σ`bill` ÷ Σ`powerUsage` | ✔ (`unitCost`도 있으나 반올림된 값이라 합계로 다시 계산) |
| `search_law_article.articles[].article_no` | `조문번호` + `조문가지번호` | ✔ |
| `… .title`, `excerpt` | `조문제목`, `조문내용`, `항내용`, `호내용`, `목내용` | ✔ |
| `… .link` | 응답의 `법령상세링크`는 **OC가 든 주소**라 쓰지 않음 → 법령명으로 공개 주소를 만듦 (`https://www.law.go.kr/법령/{법령명}/{조문}`) | ✔ (공개 주소 4종 200 응답 확인) |

### 3.3 값이 비거나 결과가 없을 때의 실제 응답 → 실패 코드 반영

| API | 실제 응답 | Tool 실패 코드 |
|---|---|---|
| ECOS | `{"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}` (없는 코드, 집계 전 달, 미래 달) | 품목 목록에 없으면 `NOT_FOUND`, 지수가 없으면 `NO_INDEX_VALUE`, 미래 달은 최신 집계월로 대신하고 `estimated` |
| ECOS | `{"RESULT": {"CODE": "INFO-100", …}}` (틀린 키) | `INVALID_KEY` |
| ECOS | `ERROR-301` "sample은 최대 10건 이내에서 호출이 가능합니다." | `sample` 키일 때는 10행씩 나눠 받도록 설계 변경 (아래 3.4) |
| 한전 | HTTP 404 `{"errCd": "404", "errMsg": "NotFound"}` (집계 없는 달, 없는 시도 코드) | 해당 달 없음 → 최신 집계월 탐색 / `NO_INDEX_VALUE` / `NO_DATA` |
| 한전 | HTTP 401 `{"errCd": "401", "errMsg": "InvalidApiKeyException"}` (틀린 키, 그리고 틀린 키 호출 직후 몇 초 동안은 맞는 키에도) | 1초 간격으로 2번 더 물어 3번 모두 401이면 `INVALID_KEY` |
| 법제처 | 검색 결과 0건: `{"LawSearch": {"totalCnt": "0", …}}` (`law` 키 없음) | `NOT_FOUND` |
| 법제처 | 결과 1건, 항 1개, 호 1개: 배열이 아니라 **객체**로 옴 | `_as_list()`로 맞춰 읽음 (단위 시험 `test_search_and_no_result`) |
| 법제처 | OC 빈 값: `{"result": "필수입력요소 검증에 실패하였습니다."}` | `INVALID_KEY` |
| 법제처 | 미신청 형식: HTML "미신청된 목록/본문에 대한 접근입니다." | `INVALID_KEY` (신청 항목과 서버 IP 확인 안내) |

### 3.4 확인 결과에 따른 설계 변경

| 발견 | 변경 |
|---|---|
| 법령 API는 JSON 형식이 신청되어 있지 않고 XML만 열려 있었다 | 1차는 XML로 구현. JSON 형식을 추가 신청한 뒤 JSON으로 바꿈 (XML 파서 제거) |
| 법령 JSON은 항목 수에 따라 객체, 배열이 바뀌고, `항`이 `조문내용`보다 앞에 오기도 한다 | 단수, 복수를 `_as_list()`로 맞추고, 키 순서 대신 조문내용 → 항 → 호 → 목 순으로 본문을 모음 |
| `.env`의 ECOS 키 이름이 `ECOS_OPEN_API` | 코드, 문서, `.env.example`의 키 이름을 `ECOS_OPEN_API`로 맞춤 |
| 법령 API 응답의 상세 링크에 OC가 그대로 들어 있다 | 사용자에게는 OC가 없는 국가법령정보센터 공개 주소를 준다 |
| ECOS `sample` 키는 호출당 10행, 품목 목록은 1,577행 | 품목 목록을 하루 동안 메모리 + `data/ecos/` 파일에 캐시. 지수는 기준월, 현재월을 따로 1행씩 조회 |
| ECOS 품목명에 현장 용어(철판, 구리)가 없다 | 별칭표(`ALIASES`) 추가: 철판 → 강판, 후판 |
| ECOS에서 '전기'로 찾으면 전기장비 등 원재료가 아닌 품목이 나온다 | 에너지 검색어에는 `supplement`로 한전 Tool을 알려 다른 API로 보충 |
| 한전 최신 집계월은 호출 시점마다 다르다 (오늘 기준 202609) | 지난달부터 최대 6개월을 거슬러 올라가며 찾고, 요청한 달이 더 뒤면 `estimated` |
| 한전 시군구 `unitCost`는 반올림 값이고, 시도 전체 값이 없다 | Σ전기요금 ÷ Σ사용량으로 다시 계산하고 `basis`로 산정 기준을 밝힘 |
| 한전 시도 코드 29(광주), 46(전남)은 404 | 지원 지역 목록에서 제외 (확인된 15개 시도만) |
| 연달아 실행하면 한전 정상 호출이 가끔 `INVALID_KEY`로 실패 | 원인 확인: 틀린 키를 보낸 뒤 1~3초 안에 맞는 키로 부르면 401이 섞여 옴 (맞는 키 15번 중 2번). 한전은 401을 1초 간격으로 2번 더 확인하게 바꾸고 (`test_spurious_401_is_retried`, `test_persistent_401_is_invalid_key`), `check_tools.py`의 틀린 키 확인을 맨 뒤로 옮김. 그 뒤 4번 연속 30/30 PASS |
| 외부 API가 잠깐 응답하지 않을 수 있다 | `http_get`이 연결 오류, 시간 초과, 5xx만 한 번 더 부름 (`test_transient_failure_is_retried_once`) |
| 대구 산업용 단가가 202512 → 202609에 −11.05% (계절별 요금 차이) | Tool 정의서에 계절 요금 주의를 적고, 최종 응답 `F3011`에서 안내하도록 넘김 |

## 4. 실행 기록: `python backend/check_tools.py`

아래는 2026-11-18 실행 출력 그대로입니다 (`.env`의 정식 키, 4번 연속 실행 중 마지막 회).

```text

── 1. 등록한 Tool 목록 (Agent에 넘기는 명세) ─────────────────────────────
[Tool] search_material_item
       사용자가 말한 원재료 이름(예: 열연강판, 알루미늄, 철판)에 해당하는 한국은행 생산자물가지수 품목 코드를 모를 때 부른다. 변동률을 계산하기 전에 반드시 먼저 불러 item_code 후보를 얻는다. 후보가 여럿이면 이름이 정확히 같은 품목이 앞에 온다. 결과에 supplement가 있으면 그 Tool로 보충한다(전기료 등 에너지경비).
       입력: {"keyword": {"type": "string", "description": "원재료 이름. 예: '열연강판'"}, "as_of": {"type": "string", "pattern": "\\d{4}(0[1-9]|1[0-2])", "description": "후보가 이 달의 지수를 갖는지 볼 기준월(YYYYMM). 생략하면 최신 집계월"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "description": "돌려받을 후보 수. 기본 5"}} … / 필수 ['keyword']
[Tool] get_material_price_index
       원재료 가격이 약정 기준 시점 대비 몇 % 변했는지 판단해야 할 때 부른다. search_material_item이 준 item_code와 약정서의 기준월을 넣으면 기준월, 현재월 지수와 변동률(change_rate_pct)을 준다. 현재월을 생략하면 최신 집계월을 쓴다. 재계획에 쓸 최근 추세가 필요하면 include_trend=true.
       입력: {"item_code": {"type": "string", "description": "ECOS 품목 코드. 예: '307125AA'"}, "base_month": {"type": "string", "pattern": "\\d{4}(0[1-9]|1[0-2])", "description": "약정서의 기준 시점(YYYYMM). 예: '202512'"}, "current_month": {"type": "string", "pattern": "\\d{4}(0[1-9]|1[0-2])", "description": "비교할 현재월(YYYYMM … / 필수 ['item_code', 'base_month']
[Tool] get_industrial_power_price
       전기료(주요 에너지경비)가 올라 납품대금 조정이 가능한지 판단해야 할 때 부른다. 지역 산업용 전기 평균판매단가(원/kWh)를 기준월과 현재월로 비교한 변동률을 준다. 원재료 지수가 아니라 에너지경비가 대상일 때만 쓴다.
       입력: {"base_month": {"type": "string", "pattern": "\\d{4}(0[1-9]|1[0-2])", "description": "약정서의 기준 시점(YYYYMM)"}, "current_month": {"type": "string", "pattern": "\\d{4}(0[1-9]|1[0-2])", "description": "비교할 현재월(YYYYMM). 생략하면 최신 집계월"}, "region": {"type": "string", "enum": ["서울", "부산", "대구", "인천", "대전", "울산" … / 필수 ['base_month']
[Tool] search_law_article
       연동 대상 여부나 조정 협의의 판단 근거가 되는 법 조문을 사용자에게 함께 보여 줘야 할 때 부른다. 법령명(약칭 가능, 예: '상생협력법')과 검색어(예: '주요 원재료', '납품대금 연동')를 넣으면 검색어가 모두 들어간 조문의 번호, 제목, 발췌, 링크를 준다. 조문을 지어내지 말고 이 Tool의 결과만 인용한다.
       입력: {"law_name": {"type": "string", "description": "법령명 또는 약칭. 예: '상생협력법', '하도급법'"}, "keyword": {"type": "string", "description": "조문에서 찾을 말. 공백으로 나누면 모두 포함한 조문만"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "description": "돌려받을 조문 수. 기본 3"}} … / 필수 ['law_name', 'keyword']
[PASS] 모르는 Tool 이름은 실패 응답
       → fail NOT_FOUND  {"code": "NOT_FOUND", "message": "등록되지 않은 Tool입니다: no_such_tool"}
[PASS] 모르는 인자는 BAD_ARGUMENT
       → fail BAD_ARGUMENT  {"code": "BAD_ARGUMENT", "message": "모르는 인자입니다: foo"}

── 2. F3002 후보 조회 — search_material_item ───────────────────────
[PASS] 정상: '열연강판' (기준월 생략 → 기본값=최신 집계월)
       → ok     {"keyword": "열연강판", "requested_as_of": null, "as_of": "202609", "estimated": false, "note": "기준월을 지정하지 않아 최신 집계월(202609)을 기본값으로 사용했습니다.", "total_matches": 1, "candidates": [{"item_code": "307125AA", "item_name": "열연강판", "parent_name": "열간압연강재", "weight": 3.6, "first_month": "196501", "latest_month": "202609", "has_as_of": true}]}
[PASS] 미래 기준월 202612 → 최신 집계월로 대신 조회 + 추정 표시
       → ok     {"keyword": "열연강판", "requested_as_of": "202612", "as_of": "202609", "estimated": true, "note": "요청한 기준월(202612)은 아직 집계되지 않아 가장 최근 집계월(202609)로 대신 조회했습니다. 이 값은 추정입니다.", "total_matches": 1, "candidates": [{"item_code": "307125AA", "item_name": "열연강판", "parent_name": "열간압연강재", "weight": 3.6, "first_month": "196501", "latest_month": "202609", "has_as_of": true}]}
[PASS] 별칭: '철판' → 강판, 후판 품목 후보
       → ok     {"keyword": "철판", "requested_as_of": null, "as_of": "202609", "estimated": false, "note": "기준월을 지정하지 않아 최신 집계월(202609)을 기본값으로 사용했습니다.", "total_matches": 2, "candidates": [{"item_code": "307125AA", "item_name": "열연강판", "parent_name": "열간압연강재", "weight": 3.6, "first_month": "196501", "latest_month": "202609", "has_as_of": true}, {"item_code": "307124AA", "item_name": "중후판(두께3mm이상)", "parent_name": "열간압연강재", "weight": 2.1, "first_month": "196501", "latest_month": "202609", "has_as_of": true}]}
[PASS] 보충: '전기' → 한전 Tool로 보충 안내(supplement)
       → ok     {"keyword": "전기", "requested_as_of": null, "as_of": "202609", "estimated": false, "note": "기준월을 지정하지 않아 최신 집계월(202609)을 기본값으로 사용했습니다.", "total_matches": 16, "candidates": [{"item_code": "3101AA", "item_name": "전기장비", "parent_name": "전기장비", "weight": 46.8, "first_month": "196501", "latest_month": "202609", "has_as_of": true}, {"item_code": "310AA", "item_name": "전기장비", "parent_name": "공산품", "weight": 46.8, "first_month": "196501", "latest_month": "202609", "has_as_of": true}], "supplement": {"tool": "get_industrial_power_price", "reason": "ECOS 지수로는 지역별 실제 전기요금을 알 수 없어 한전 산업용 전기요금으로 보충합니다."}}
[PASS] 빈 입력 → BAD_ARGUMENT
       → fail BAD_ARGUMENT  {"code": "BAD_ARGUMENT", "message": "keyword는 필수입니다."}
[PASS] 형식 틀림(as_of='2026-09') → BAD_ARGUMENT
       → fail BAD_ARGUMENT  {"code": "BAD_ARGUMENT", "message": "as_of의 형식이 맞지 않습니다. (후보가 이 달의 지수를 갖는지 볼 기준월(YYYYMM). 생략하면 최신 집계월)"}
[PASS] 결과 없음 → NO_DATA
       → fail NO_DATA  {"code": "NO_DATA", "message": "'zzzzzz'와 일치하는 ECOS 품목이 없습니다."}
[PASS] 키 없음 → INVALID_KEY
       → fail INVALID_KEY  {"code": "INVALID_KEY", "message": "ECOS_OPEN_API가 없습니다."}

── 3. F3003 상세정보 조회 — get_material_price_index ─────────────────
[PASS] 연결: 후보 Tool의 item_code(307125AA) → 상세 조회 (현재월 생략 → 최신 집계월)
       → ok     {"item_code": "307125AA", "item_name": "열연강판", "parent_name": "열간압연강재", "unit": "2020=100", "weight": 3.6, "base": {"month": "202512", "value": 130.58}, "current": {"month": "202609", "value": 150.55}, "change_rate_pct": 15.29, "requested_current_month": null, "estimated": false, "note": "현재월을 지정하지 않아 최신 집계월(202609)을 기본값으로 사용했습니다."}
[PASS] 미래 현재월 202612 → 최신 집계월로 계산 + 추정 표시
       → ok     {"item_code": "307125AA", "item_name": "열연강판", "parent_name": "열간압연강재", "unit": "2020=100", "weight": 3.6, "base": {"month": "202512", "value": 130.58}, "current": {"month": "202609", "value": 150.55}, "change_rate_pct": 15.29, "requested_current_month": "202612", "estimated": true, "note": "요청한 현재월(202612)은 아직 집계되지 않아 가장 최근 집계월(202609)로 대신 계산했습니다. 이 변동률은 추정입니다.", "trend": [{"month": "202604", "value": 142.5}, {"month": "202605", "value": 143.09}, {"month": "202606", "value": 148.29}, {"month": "202607", "value": 150.74}, {"month": "202608", "value": 151.34}, {"month": "202609", "value": 150.55}]}
[PASS] 지수가 비어 있는 달(190001) → NO_INDEX_VALUE, 값을 채우지 않음
       → fail NO_INDEX_VALUE  {"code": "NO_INDEX_VALUE", "message": "열연강판(307125AA)의 190001 지수가 비어 있어 변동률을 계산할 수 없습니다."}
[PASS] 없는 품목 코드 → NOT_FOUND
       → fail NOT_FOUND  {"code": "NOT_FOUND", "message": "품목 코드 ZZZZ가 ECOS 품목 목록에 없습니다."}
[PASS] 기준월이 현재월보다 늦음 → BAD_ARGUMENT
       → fail BAD_ARGUMENT  {"code": "BAD_ARGUMENT", "message": "기준월(202609)이 현재월(202512)보다 늦습니다."}
[PASS] 필수 인자 누락 → BAD_ARGUMENT
       → fail BAD_ARGUMENT  {"code": "BAD_ARGUMENT", "message": "base_month는 필수입니다."}
[PASS] 키 없음 → INVALID_KEY
       → fail INVALID_KEY  {"code": "INVALID_KEY", "message": "ECOS_OPEN_API가 없습니다."}

── 3-1. F3003 (에너지경비 보충) — get_industrial_power_price ──────────
[PASS] 정상: 대구 산업용 (현재월 생략 → 최신 집계월)
       → ok     {"region": "대구", "city": null, "contract_type": "산업용", "basis": "지역 산업용 고객 전체의 전기요금 합 ÷ 사용량 합 (개별 사업장 요금 아님)", "base": {"month": "202512", "unit_cost_won_per_kwh": 189.48, "bill_won": 106089627529, "usage_kwh": 559910670, "customers": 20249}, "current": {"month": "202609", "unit_cost_won_per_kwh": 168.55, "bill_won": 97360950353, "usage_kwh": 577648419, "customers": 20094}, "change_rate_pct": -11.05, "requested_current_month": null, "estimated": false, "note": "현재월을 지정하지 않아 최신 집계월(202609)을 기본값으로 사용했습니다."}
[PASS] 시군구 지정: 대구 달서구, 미래 현재월 → 추정 표시
       → ok     {"region": "대구", "city": "달서구", "contract_type": "산업용", "basis": "지역 산업용 고객 전체의 전기요금 합 ÷ 사용량 합 (개별 사업장 요금 아님)", "base": {"month": "202512", "unit_cost_won_per_kwh": 188.88, "bill_won": 29996626748, "usage_kwh": 158810458, "customers": 3732}, "current": {"month": "202609", "unit_cost_won_per_kwh": 168.35, "bill_won": 27094816542, "usage_kwh": 160943824, "customers": 3727}, "change_rate_pct": -10.87, "requested_current_month": "209912", "estimated": true, "note": "요청한 현재월(209912)은 아직 집계되지 않아 가장 최근 집계월(202609)로 대신 계산했습니다. 이 변동률은 추정입니다."}
[PASS] 없는 시군구 → NOT_FOUND
       → fail NOT_FOUND  {"code": "NOT_FOUND", "message": "대구에 '강남구' 집계가 없습니다. 가능한 값: 군위군, 남구, 달서구, 달성군, 동구, 북구, 서구, 수성구, 중구"}
[PASS] 집계가 없는 기준월(200001) → NO_INDEX_VALUE
       → fail NO_INDEX_VALUE  {"code": "NO_INDEX_VALUE", "message": "대구 산업용 전력의 200001 집계가 비어 있어 변동률을 계산할 수 없습니다."}
[PASS] 지원하지 않는 지역 → BAD_ARGUMENT
       → fail BAD_ARGUMENT  {"code": "BAD_ARGUMENT", "message": "region는 ['서울', '부산', '대구', '인천', '대전', '울산', '세종', '경기', '충북', '충남', '경북', '경남', '제주', '강원', '전북'] 중 하나여야 합니다."}
[PASS] 키 없음 → INVALID_KEY
       → fail INVALID_KEY  {"code": "INVALID_KEY", "message": "KEP_CO_API가 없습니다."}

── 4. F3004 보조 정보 검색 — search_law_article ──────────────────────
[PASS] 정상: 상생협력법 '주요 원재료'
       → ok     {"law": {"name": "대ㆍ중소기업 상생협력 촉진에 관한 법률", "abbr": "상생협력법", "law_id": "010165", "kind": "법률", "ministry": "중소벤처기업부", "effective_date": "20260929", "link": "https://www.law.go.kr/법령/대ㆍ중소기업상생협력촉진에관한법률"}, "keyword": "주요 원재료", "total_matches": 3, "articles": [{"article_no": "제2조", "title": "정의", "excerpt": "12. \"주요 원재료\"란 수탁ㆍ위탁거래에서 물품등의 제조등에 사용되는 원재료로서 그 비용이 납품대금의 100분의 10 이상인 원재료를 말한다.\n12의2. \"주요 에너지경비\"란 수탁ㆍ위탁거래에서 물품등의 제조등에 사용되는 에너지(「에너지법」 제2조제1호 및 제3호에 따른 에너지를 말한다)에 대한 요금으로서 그 비용이 납품대금의 100분의 10 이상인 에너지 요금을 말한다.\n13. \"납품대금 연동\"이란 주요 원재료의 가격 또는 주요 에너지경비가 위탁기업과 수탁기업이 100분의 10 이내의 범위에서 협의하여 정한 비율 이상 변동하는 경우 그 변동분에 연동하여 납품대금을 조정하는 것을 말한다.", "truncated": false, "link": "https://www.law.go.kr/법령/ …
[PASS] 정상: 상생협력법 '연동 조정'
       → ok     {"law": {"name": "대ㆍ중소기업 상생협력 촉진에 관한 법률", "abbr": "상생협력법", "law_id": "010165", "kind": "법률", "ministry": "중소벤처기업부", "effective_date": "20260929", "link": "https://www.law.go.kr/법령/대ㆍ중소기업상생협력촉진에관한법률"}, "keyword": "연동 조정", "total_matches": 5, "articles": [{"article_no": "제22조의4", "title": "납품대금 연동 확산 지원본부의 지정 등", "excerpt": "제22조의4(납품대금 연동 확산 지원본부의 지정 등)\n① 중소벤처기업부장관은 납품대금 연동의 확산을 지원하기 위하여 중소기업지원 관련 기관이나 단체를 납품대금 연동 확산 지원본부(이하 \"연동지원본부\"라 한다)로 지정할 수 있다.\n② 연동지원본부는 다음 각 호의 사업을 한다.\n2. 납품대금 연동의 도입 및 조정 실적 확인\n3. 납품대금 연동 관련 교육 및 컨설팅\n4. 그 밖에 납품대금 연동의 확산을 위하여 필요한 사항으로서 중소벤처기업부령으로 정하는 사항\n③ 중소벤처기업부장관은 연동지원본부가 제2항 각 호의 사업을 추진하는 데 필요한 지원을 할 수 있다.\n④ 중소벤처기업부장관은 연동지원본부가 다음 각 호의 어느 하나에 해당하면 지정을 취소하거나 6개 …
[PASS] 결과 없음 → NO_DATA (정의한 형식)
       → fail NO_DATA  {"code": "NO_DATA", "message": "대ㆍ중소기업 상생협력 촉진에 관한 법률에서 '우주정거장'가 들어간 조문을 찾지 못했습니다."}
[PASS] 없는 법령 → NOT_FOUND
       → fail NOT_FOUND  {"code": "NOT_FOUND", "message": "'존재하지않는법률zz' 법령을 찾지 못했습니다."}
[PASS] 빈 입력 → BAD_ARGUMENT
       → fail BAD_ARGUMENT  {"code": "BAD_ARGUMENT", "message": "law_name는 필수입니다."}
[PASS] 키 없음 → INVALID_KEY
       → fail INVALID_KEY  {"code": "INVALID_KEY", "message": "LAW_API_OC가 없습니다."}

── 5. 틀린 키 (한전) — 마지막에 실행한다 ────────────────────────────────────
[PASS] 틀린 키 → INVALID_KEY
       → fail INVALID_KEY  {"code": "INVALID_KEY", "message": "KEP_CO_API가 유효하지 않습니다."}

── 요약 ──────────────────────────────────────────────────────────
30/30 PASS
```
