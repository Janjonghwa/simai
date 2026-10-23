import { z } from 'zod';

// 시스템 지표 REST(GET /api/metrics, 명세 12.6 관리자, BE S15P31A101-70). 응답 형식 계약이 아직 없어
// 실시간 health 에 없는 구간 지연(촬영 → 현장 소리·관제 화면·반장 PWA)만 담는다고 가정한다.
// contract 표시가 있는 응답만 받는다. 실제 형식이 정해지면 이 파일을 바꾼다.
// 구간 이름은 명세 6.6 을 따른다: capture_to_site_alert(촬영 → 노트북 실제 소리·화면),
// capture_to_remote_delivery(촬영 → 관제 화면 / 반장 PWA, 목적지마다 따로 잰다).
// 구간이나 값 하나가 빠져도 응답 전체를 버리지 않고 그 칸만 비운다.

const percentiles = z.object({ p50: z.number().nullish(), p95: z.number().nullish() });

export const metricsResponseSchema = z.object({
  contract: z.literal('DEMO_UNCONFIRMED'),
  as_of: z.number(),
  stages: z.object({
    capture_to_site_alert_ms: percentiles.nullish(),
    capture_to_remote_delivery_ms: z
      .object({ viewer: percentiles.nullish(), pwa: percentiles.nullish() })
      .nullish(),
  }),
});

export type MetricsResponse = z.infer<typeof metricsResponseSchema>;
