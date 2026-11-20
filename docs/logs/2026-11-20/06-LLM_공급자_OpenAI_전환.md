# LLM 공급자를 OpenAI로 전환

## 변경 내용

- `GMS_KEY`를 `OPENAI_API_KEY`로 바꾸고 `GMS_BASE_URL` 설정을 제거했다. 기본 주소는 `ChatOpenAI`의 OpenAI 공식 주소를 쓴다.
- 기본 모델 `gpt-6-luna`는 OpenAI 공식 모델이므로 유지했고, `reasoning_effort="none"` 자동 지정도 유지했다.
- 키 이름이 노출되는 시스템 키 목록, 프론트엔드 안내 문구, 확인 스크립트, 테스트를 새 이름에 맞췄다.

## 수정 파일

- `backend/app/agent/config.py`, `backend/app/agent/analyzer.py`, `backend/app/api/system.py`
- `backend/check_*.py`, `backend/capture_screens.py`, `backend/compare_worker.py`
- `backend/tests/test_review.py`, `backend/tests/test_server.py`
- `frontend/js/chat.js`, `.env.example`

## 확인

- 단위 테스트 106건 통과.
- 기존 `.env`의 `GMS_KEY`는 `OPENAI_API_KEY`로 이름을 바꾸고 OpenAI 키로 교체해야 한다.
