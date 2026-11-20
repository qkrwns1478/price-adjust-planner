// 화면에 쓰는 이름표. 백엔드의 코드 값을 사람이 읽는 말로 바꾼다.

export const NODE = {
  analyze: "요청 분석",
  plan: "Planner 계획 수립",
  execute: "Worker 작업 수행",
  judge: "제약 판단",
  respond: "응답 생성",
};

export const TOOL = {
  search_material_item: "원재료 품목 검색",
  get_material_price_index: "생산자물가지수 조회",
  get_industrial_power_price: "산업용 전기요금 조회",
  search_law_article: "법령 조문 검색",
};

export const STRATEGY = {
  initial: "최초 계획",
  next_candidate: "다른 후보 지표로 다시 계산",
  recheck: "다음 지수 발표 때 다시 조회",
  alt_path: "연동이 아닌 조정 신청 경로 탐색",
  worker_request: "Worker 요청 반영",
  verify_fix: "검증 결과 반영",
};

export const WORKER_ACTION = {
  retry: "인자를 고쳐 다시 호출",
  skip: "건너뜀",
  replan: "Planner에 계획 변경 요청",
  report: "Planner에 보고",
};

export const INTENT = {
  plan: "조정 계획 수립",
  fact: "사실 조회",
  eligibility: "대상 여부 판정",
  manage: "계획 관리",
  chat: "그 밖의 대화",
};

export const JUDGMENT = {
  satisfied: "충족",
  unsatisfied: "미충족",
  undetermined: "판단 불가",
  info: "조회",
};

// Tool 실패 코드 → 사용자 안내 (NF3002: 원문 오류 대신 코드별 안내만 보여 준다)
export const TOOL_ERROR = {
  INVALID_KEY: "API 키가 없거나 올바르지 않습니다",
  UPSTREAM_ERROR: "외부 API가 응답하지 않았습니다",
  NOT_FOUND: "찾는 대상이 없습니다",
  NO_DATA: "조회 결과가 없습니다",
  NO_INDEX_VALUE: "지수 값이 비어 판단에서 제외했습니다",
  BAD_ARGUMENT: "입력 값이 규격과 다릅니다",
  SKIPPED: "앞 단계 결과가 없어 호출하지 않았습니다",
};

export const PLAN_STATUS = {
  ready: "준비",
  requested: "요청함",
  agreed: "합의",
  rejected: "거절",
};

export const VERDICT = {
  normal: "정상",
  anomaly: "이상",
  undetermined: "판정 불가",
  failed: "실패",
};

export function won(v) {
  return v == null ? "-" : `${Number(v).toLocaleString("ko-KR")}원`;
}

export function ms(v) {
  if (v == null) return "";
  return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`;
}

export function dateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function argsText(args) {
  return Object.values(args || {})
    .filter(
      (v) =>
        v !== null && v !== undefined && v !== "" && typeof v !== "boolean",
    )
    .map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v)))
    .join(", ");
}
