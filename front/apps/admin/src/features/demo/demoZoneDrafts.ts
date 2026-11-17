import {
  setupZoneDraftsResponseSchema,
  type SetupZoneDraftsResponse,
} from '@/features/setup/types';
import { SURFACE_ZONE_MARGIN_M } from '@/features/zones/zoneShape';

// 시연 계정(features/demo/demoAccount)의 세팅 구역 단계 「AI 위험 구역 초안」. 서버 초안 대신 정해 둔 개구부 하나를 보인다.
// 자리는 시연 맵(assets 의 GLB)에 지금 놓인 「단상 개구부」 구역(바닥 x 5.2~7.6 m, y 2.25~4.55 m, 높이 0~1.4 m)이다.
// 초안을 수락하면 다각형을 SURFACE_ZONE_MARGIN_M 만큼 넓혀 구역을 만들므로(draftZone), 초안은 그만큼 안으로 줄여 둔다.

const OPENING = { minX: 5.2, maxX: 7.6, minY: 2.25, maxY: 4.55, zMin: 0, zMax: 1.4 } as const;
const INSET = SURFACE_ZONE_MARGIN_M;

/** 시연 계정이 보는 AI 구역 초안 응답(세팅 세션 하나). */
export function demoZoneDrafts(sessionId: string): SetupZoneDraftsResponse {
  const minX = OPENING.minX + INSET;
  const maxX = OPENING.maxX - INSET;
  const minY = OPENING.minY + INSET;
  const maxY = OPENING.maxY - INSET;
  return setupZoneDraftsResponseSchema.parse({
    setup_session_id: sessionId,
    site_id: 'demo',
    status: 'SUCCEEDED',
    drafts: [
      {
        candidate_id: 'demo-opening-stage',
        candidate_kind: 'OPENING',
        polygon_xy: [
          [minX, minY],
          [maxX, minY],
          [maxX, maxY],
          [minX, maxY],
        ],
        z_min_m: OPENING.zMin,
        z_max_m: OPENING.zMax,
        recommended_name: '단상 개구부',
        recommended_severity: 'CRITICAL',
        rationale: '단상 둘레에 난간 없이 트인 단차가 있어 발을 헛디디면 떨어질 수 있어요.',
        confidence: 0.92,
      },
    ],
  });
}
