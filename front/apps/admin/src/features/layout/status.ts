// contracts/common/defs.schema.json 의 system_health. 계약 타입이 생기면(S15P31A101-24) 그 타입으로 바꾼다.
export type SystemHealth = 'NOMINAL' | 'DEGRADED' | 'UNSAFE_UNKNOWN';

export interface StatusValues {
  health?: SystemHealth;
  dataAgeSec?: number;
  cameras?: { online: number; total: number };
  calibration?: string;
  config?: string;
  scene?: string;
  /** EC2 기준 시각(명세 3.8). */
  clock?: string;
}
