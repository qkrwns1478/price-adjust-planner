# Tool 정의 및 구현 `F3001` ~ `F3004`

- 작업일: 2026-11-18
- 범위: Tool 정의 및 구현, Tool 설계 확인, 확인 항목
- 결과: `unittest` 22/22 OK, `check_tools.py` 30/30 PASS (4회 연속)

## 1. 설계 결정

| 결정 | 근거 | 구현 |
|---|---|---|
| Tool을 이름으로 등록하고 Agent에는 이름, 설명, 입력 규격만 넘긴다 | ① "레지스트리에 등록하고 이름, 설명, 입력 스키마만 넘긴다" | `registry.TOOLS`, `catalog()`, `call_tool()` |
| 외부 API 툴과 로컬 서버 API 툴을 구분한다 | ① | `Tool.kind` (`external` / `local`), `catalog(kind)` |
| 성공, 실패를 같은 모양으로 돌려주고 예외를 밖으로 던지지 않는다 | ①, `NF3003` | `ok()` / `fail()`, `Tool.validate()` 입력 검사, `Tool.run()` 안전망 |
| 실패 코드를 6종으로 정하고 재계획 여부를 미리 나눈다 | 2.3, 2.4 "키 누락은 재계획하지 않음" | `ErrorCode`, `FATAL`(`INVALID_KEY`, `UPSTREAM_ERROR`), `RETRYABLE_WITH_NEW_ARGS` |
| 후보 조회는 ECOS 품목 목록, 상세 조회는 ECOS 지수, 보조 정보는 법제처 조문으로 한다 | ② ~ ④ | `search_material_item`, `get_material_price_index`, `search_law_article` |
| 에너지경비 후보는 한전 API Tool로 보충한다 | ② "다른 API를 래핑한 Tool로 보충" | `get_industrial_power_price`, 후보 조회 결과의 `supplement` |
| 기준월을 생략하면 최신 집계월을 쓰고, 미래 달은 최신 집계월로 대신하며 추정임을 밝힌다 | ② "기본값 정의", "추정이 섞였다는 사실을 밝힌다" | `requested_as_of`, `as_of`, `estimated`, `note` |
| 판단에 필요한 값이 비면 채우지 않고 실패로 돌려준다 | ③ "임의로 채우지 않고 판단하지 않은 채 둔다" | `NO_INDEX_VALUE` |
| 법령 링크는 OC가 없는 공개 주소로 만든다 | `NF3001`, `NF3002` | `https://www.law.go.kr/법령/{법령명}/{조문}` |

## 2. 변경 파일

### 추가

| 파일 | 내용 |
|---|---|
| `backend/app/tools/base.py` | `ok`, `fail`, `ErrorCode`, `Tool`(validate, run, spec), `http_get()`(시간 초과, 일시 오류 1회 재시도), `shift_month()` |
| `backend/app/tools/registry.py` | Tool 4개 등록, `catalog()`, `get_tool()`, `call_tool()` |
| `backend/app/tools/ecos.py` | `search_material_item`(별칭, 품목 목록 하루 캐시), `get_material_price_index`(변동률, 추세) |
| `backend/app/tools/kepco.py` | `get_industrial_power_price`(Σ요금 ÷ Σ사용량, 최신 집계월 탐색, 401 재확인) |
| `backend/app/tools/law.py` | `search_law_article`(JSON, 단수, 복수 처리, 조문 발췌, 본문 캐시) |
| `backend/check_tools.py` | 실제 API 확인 30개 항목 |
| `backend/tests/test_tools.py` | 단위 테스트 22개 (4절) |
| `requirements.txt` | 필수 라이브러리 버전 고정 |

### 수정, 삭제

| 파일 | 내용 |
|---|---|
| `.env.example` | `ECOS_OPEN_API` 추가 |
| `tools.py` | 삭제. 내용을 `backend/app/tools/`로 옮김 |
| `.venv` | requests 2.34.2, python-dotenv 1.2.3 설치 |

## 3. 실제 호출 결과

| Tool | 호출 | 결과 |
|---|---|---|
| `search_material_item` | 열연강판, 기준월 생략 / 202612 | 307125AA, 최신 집계월 202609 / 추정 표시 |
| `get_material_price_index` | 307125AA, 202512 기준 | 130.58 → 150.55, +15.29% |
| `get_industrial_power_price` | 대구 산업용, 202512 기준 | 189.48 → 168.55 원/kWh, −11.05% (계절별 요금 차이) |
| `search_law_article` | 상생협력법, "주요 원재료" | 제2조 12, 12의2, 13 발췌, 공개 링크 |

## 4. 검증

### 단위 테스트 (`test_tools.py`, 외부 API 없음)

| 테스트 | 확인 내용 |
|---|---|
| `RegistryTest` (6개) | 명세에 이름, 설명, 입력 규격만 있음, kind 구분, 예외, 잘못된 반환 차단, 타입, 범위 검사, 응답 모양 |
| `EcosTest` (7개) | 시간 초과 시 키 미노출, 일시 오류 재시도, `INFO-100` → `INVALID_KEY`, 빈 값 → `NO_INDEX_VALUE`, 변동률, 추정, 기본값, 결과 없음, JSON 아닌 응답 |
| `LawTest` (4개) | 미신청 HTML → `INVALID_KEY`, JSON 단수, 복수와 본문 순서, 검색 0건 → `NOT_FOUND`, OC 검증 실패 |
| `KepcoTest` (5개) | 일시적 401 재확인, 지속 401 → `INVALID_KEY`, 최근 집계 없음, 빈 요금 → `NO_INDEX_VALUE`, 단가 계산 |

### 실제 호출 (`check_tools.py`)

- 확인 항목 7개를 30개 검사로 나눠 확인
- 출력, 캐시 파일, 코드, 문서에 `.env` 키 값이 없음을 문자열 대조로 확인

## 5. 발생한 문제

| 문제 | 원인 | 해결 |
|---|---|---|
| 법령 API를 JSON으로 부르면 "미신청된 목록/본문" | JSON 형식 미신청 | 1차는 XML로 구현. 사용자가 JSON을 신청한 뒤 JSON으로 바꾸고 XML 파서 제거 |
| 법령 JSON에서 `law`, `항`, `호`가 객체로 오기도 함 | 항목이 하나면 배열이 아니라 객체 | `_as_list()`로 맞춤, 본문은 조문내용 → 항 → 호 → 목 순으로 모음 |
| ECOS `sample` 키로 첫 실행이 약 4분 25초 | 호출당 10행 한도, 품목 목록 1,577행 | 품목 목록 하루 캐시. 정식 키는 1,000행씩 받아 첫 실행 10.3초 |
| `.env`에 ECOS 키를 넣었는데 `INVALID_KEY` | 코드는 `ECOS_API_KEY`, `.env`는 `ECOS_OPEN_API` | 코드, 테스트, `.env.example`을 `ECOS_OPEN_API`로 맞춤 |
| 연달아 실행하면 한전 정상 호출이 가끔 `INVALID_KEY` | 틀린 키 호출 뒤 1 ~ 3초 동안 맞는 키에도 401 (15번 중 2번) | 401 은 1초 간격으로 2번 더 확인, 틀린 키 확인을 `check_tools.py` 맨 뒤로 옮김 |
| 한전 시도 코드 29(광주), 46(전남) 응답 없음 | 확인 불가 | 확인된 15개 시도만 지원 |
| ECOS에서 "전기"로 찾으면 전기장비 품목이 나옴 | 원재료 지수에 지역 전기요금 없음 | `supplement`로 한전 Tool 보충 안내 |
| `law.py` 수정 스크립트에서 `\n`이 실제 줄바꿈으로 들어가 `SyntaxError` | 수정 스크립트 문자열의 이스케이프 처리 | 해당 줄을 직접 수정 |

## 6. 남은 작업

- 세 API의 호출 한도는 공개 페이지에서 확인하지 못했다. 한도를 알게 되면 실행 설정에 반영한다.
- 한도 초과 응답(HTTP 429)을 흉내 낸 테스트가 없다.
- 로컬 서버 API 툴(`kind="local"`)은 `F3015`, `F3022`, `F3023`에서 추가한다.
