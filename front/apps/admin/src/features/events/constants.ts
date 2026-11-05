import type { Severity } from '@/features/events/types';

// 이벤트 기능이 같이 쓰는 이름표와 한도. 값이 어디서 왔는지는 각 줄 주석에 적는다.

/** 등급 순서(클수록 위험). */
export const SEVERITY_RANK: Record<Severity, number> = {
  CRITICAL: 3,
  HIGH: 2,
  CAUTION: 1,
  INFO: 0,
};

/** 등급 이름(배지 글자). 색 없이도 읽히도록 기호와 함께 쓴다(SeverityBadge). */
export const SEVERITY_LABEL: Record<Severity, string> = {
  CRITICAL: '위험',
  HIGH: '경고',
  CAUTION: '주의',
  INFO: '안내',
};

/** 목록에 남기는 해소된 이벤트 수. 넘치면 오래된 것부터 버린다. */
export const MAX_RESOLVED = 50;

/** 한 묶음(사람)에 보이는 해소된 규칙 수. 같은 규칙에 되풀이해 걸려도 카드가 길어지지 않게. */
export const RESOLVED_PER_INCIDENT = 2;

/** 이벤트 패널의 「최근 해소」 칸에 남기는 해소된 묶음(사람·규칙·구역별) 수. */
export const RECENT_RESOLVED_INCIDENTS = 4;

/** 관제 화면이 ACK 를 보낼 때 쓰는 확인 경로(계약 event_ack 의 channel). */
export const CONTROL_ROOM_CHANNEL = 'CONTROL_ROOM';

/** 확인(ACK)이 들어온 곳의 이름. */
export const ACK_CHANNEL_LABEL: Record<string, string> = {
  [CONTROL_ROOM_CHANNEL]: '관제',
  FOREMAN_PWA: '반장 휴대폰',
  SITE_BUTTON: '현장 버튼',
};

/** 서버가 ProblemDetail 의 code 로 알리는 ACK 실패(명세 12.6). */
export const ACK_ERROR_CODE = {
  NOT_FOUND: 'EVENT_NOT_FOUND',
  RESOLVED: 'EVENT_RESOLVED',
  INVALID_SEQUENCE: 'INVALID_EVENT_SEQUENCE',
  INVALID_CHANNEL: 'INVALID_ACK_CHANNEL',
} as const;

/** 상향 의견 처리 상태 문구(명세 5.4, 12.3). */
export const ESCALATION_STATUS_LABEL = {
  approved: '승인',
  rejected: '거절',
  applied: '적용',
  already_applied: '이미 적용',
  event_closed: '이벤트 종료',
} as const;

/** 서버가 ProblemDetail 의 code 로 알리는 VLM 상향 의견 처리 실패(명세 12.6). */
export const ESCALATION_ERROR_CODE = {
  NOT_FOUND: 'EVENT_NOT_FOUND',
  ASSESSMENT_NOT_FOUND: 'ASSESSMENT_NOT_FOUND',
  RESOLVED: 'EVENT_RESOLVED',
  NOT_ACTIVE: 'NOT_ACTIVE',
  NOT_UPWARD: 'NOT_UPWARD',
  DUPLICATE: 'DUPLICATE',
  ALREADY_DECIDED: 'ALREADY_DECIDED',
  JOURNAL_UNAVAILABLE: 'JOURNAL_UNAVAILABLE',
  INVALID_DECISION: 'INVALID_DECISION',
  INVALID_ASSESSMENT: 'INVALID_ASSESSMENT_ID',
} as const;

/** 사고 구간 요약 진행 상태 문구(명세 5.4, 12.6, S15P31A101-368). */
export const SUMMARY_STATUS_LABEL = {
  QUEUED: '구간 요약 대기 중',
  RUNNING: '구간 요약 실행 중',
  SUCCEEDED: '요약 완료',
  FAILED: '요약 실패',
  TIMED_OUT: '시간 초과',
} as const;

/** 서버·워커가 알리는 사고 구간 요약 실패 코드(명세 12.6, S15P31A101-271, -369). */
export const SUMMARY_ERROR_CODE = {
  NOT_FOUND: 'EVENT_NOT_FOUND',
  CLIP_NOT_READY: 'CLIP_NOT_READY',
  EVENT_NOT_LIVE: 'EVENT_NOT_LIVE',
  CLIP_FETCH_FAILED: 'CLIP_FETCH_FAILED',
  NO_FACE_SAFE_FRAMES: 'NO_FACE_SAFE_FRAMES',
  VLM_ANSWER_INVALID: 'VLM_ANSWER_INVALID',
  VLM_INPUT_INVALID: 'VLM_INPUT_INVALID',
  VLM_TIMEOUT: 'VLM_TIMEOUT',
  VLM_UNAVAILABLE: 'VLM_UNAVAILABLE',
  VLM_REQUEST_REJECTED: 'VLM_REQUEST_REJECTED',
  VLM_CONFIG: 'VLM_CONFIG',
  CANCELLED: 'CANCELLED',
  TIMED_OUT: 'TIMED_OUT',
} as const;

/** 규칙 ID 의 화면 이름. */
export const RULE_NAME: Record<string, string> = {
  R1: '위험 구역 진입',
  R2: '개구부·가장자리 근접',
  R3: '고소 위치',
  R4: '낙상 의심',
  R5: '진입 예측',
  R6: '위험 구역 내 관측 상실',
  R7: '구역 감시 능력 저하',
  R9: '보호구 미착용',
};

/** 등급에 따라 이름이 달라지는 규칙. R1 주의는 구역 안이 아니라 근접 띠에 있다(-497). */
export const RULE_NAME_BY_SEVERITY: Record<string, Partial<Record<Severity, string>>> = {
  R1: { CAUTION: '위험 구역 근접' },
};

/** 지표 키의 화면 이름. 없는 키는 키에서 단위 꼬리표를 뗀 이름으로 보인다. */
export const METRIC_LABEL: Record<string, string> = {
  edge_distance_m: '가장자리',
  height_diff_m: '높이 차',
};

/** 확인 필요 이유(confidence_reasons)의 화면 문구. */
export const CONFIDENCE_REASON: Record<string, string> = {
  Z_UNKNOWN: '높이 모름',
  PREDICTED: '예측 위치',
  PROVISIONAL_CALIB: '보정 미확정',
  SINGLE_VIEW: '한 대만 관측',
  JOINT_UNCERTAIN: '관절 불확실',
  IDENTITY_UNCERTAIN: '신원 불확실',
  UNSPECIFIED: '이유 미상',
};

/** 경과 시간이 바뀌는 간격(ms). 초 단위로 보이므로 1초다. */
export const ELAPSED_TICK_MS = 1000;
