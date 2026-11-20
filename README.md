# 단가조정 플래너 (Price Adjust Planner)

대구 지역 중소 제조 수탁기업을 대상으로 원자재, 에너지 가격이 오른 만큼 납품단가를 다시 정할 수 있는지 판단하고, 얼마를 언제 요청할지 계획해 주는 Agent 서비스입니다.

> ⚠️ 생성된 결과는 공개된 가격 지표와 법령을 바탕으로 계산한 참고 자료이며, 법률 자문이 아닙니다. 실제 조정 협의는 체결한 약정서의 내용을 기준으로 하며, 정확한 정보가 필요하면 중소벤처기업부 납품대금 연동지원본부나 법률 전문가와 상담하시기 바랍니다.

- [프로젝트 제안서](docs/project_proposal.md)
- [요구사항 정의서](docs/requirements.md)
- [Agent 설계 문서](docs/agent_design.md)
- [Tool 정의서](docs/tool_spec.md)
- [API 명세서](docs/api_spec.md)
- [검증 결과](docs/verification/verification.md)

## 기술 스택

| 구분 | 기술 |
|---|---|
| Back | ![Python](https://img.shields.io/badge/Python-3776AB?style=flat&logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat&logo=fastapi&logoColor=white) ![Uvicorn](https://img.shields.io/badge/Uvicorn-499848?style=flat) ![Pydantic](https://img.shields.io/badge/Pydantic-E92063?style=flat&logo=pydantic&logoColor=white) ![Requests](https://img.shields.io/badge/Requests-2D7DBA?style=flat) |
| Agent | ![LangChain](https://img.shields.io/badge/LangChain-1C3C3C?style=flat&logo=langchain&logoColor=white) ![LangGraph](https://img.shields.io/badge/LangGraph-1C3C3C?style=flat) ![OpenAI](https://img.shields.io/badge/OpenAI-412991?style=flat&logo=openai&logoColor=white) |
| Front | ![HTML5](https://img.shields.io/badge/HTML5-E34F26?style=flat&logo=html5&logoColor=white) ![CSS3](https://img.shields.io/badge/CSS3-1572B6?style=flat&logo=css3&logoColor=white) ![JavaScript](https://img.shields.io/badge/JavaScript-F7DF1E?style=flat&logo=javascript&logoColor=black) |
| DB | ![SQLite](https://img.shields.io/badge/SQLite-003B57?style=flat&logo=sqlite&logoColor=white) |

## 실행 방법

```bash
# 1. 가상환경과 라이브러리
python -m venv .venv
.venv\Scripts\activate            # macOS / Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2. API 키 설정
copy .env.example .env            # macOS / Linux: cp .env.example .env

# 3. 백엔드 서버 실행
cd backend
uvicorn app.main:app --reload     # http://127.0.0.1:8000/

# 4. 프론트엔드 서버 실행
cd ..
python serve_frontend.py 5500     # http://127.0.0.1:5500/
```

## API 키 발급

| 환경변수 | 발급처 | 절차 요약 |
|---|---|---|
| `ECOS_OPEN_API` | [한국은행 ECOS Open API](https://ecos.bok.or.kr/api/) | 회원가입 → 인증키 신청 |
| `LAW_API_OC` | [국가법령정보 공동활용](https://open.law.go.kr/) | 회원가입 → OPEN API 신청 → 법령 목록, 본문 항목 체크, 호출 서버 IP, 도메인 등록 및 OC 설정 |
| `KEP_CO_API` | [한국전력 전력데이터개방포털](https://bigdata.kepco.co.kr/) | 회원가입 → 오픈 API 키 발급 |
| `OPENAI_API_KEY` | [OpenAI Platform](https://platform.openai.com/api-keys) | LLM 호출용 |

## 사용한 외부 API의 출처 및 이용 조건

| API | 출처 | 쓰는 Tool | 이용 조건 | 호출 한도 |
|---|---|---|---|---|
| 생산자물가지수(기본분류, 404Y014) | 한국은행 경제통계시스템 ECOS | `search_material_item`, `get_material_price_index` | 인증키 신청 | 3분 내 300회 이상 호출 시 제한 |
| 법령 검색, 법령 본문 | 법제처 국가법령정보 공동활용 | `search_law_article` | OC 등록과 호출 서버 IP 및 도메인 등록 필요, 신청한 목록과 형식만 호출 가능 | 단시간 과도한 호출 시 제한 |
| 계약종별 전력사용량 | 한국전력 전력데이터개방포털 | `get_industrial_power_price` | 인증키 신청 | N/A |
