import { demoZoneDrafts } from '@/features/demo/demoZoneDrafts';
import { draftCreateRequest, draftZoneFootprint } from '@/features/setup/draftZone';
import { SETUP_NEW_ZONE_PREFIX } from '@/features/setup/setupZonePlan';
import { describe, expect, it } from 'vitest';

describe('시연 계정 AI 구역 초안', () => {
  it('개구부 초안 하나를 끝난 분석으로 보인다', () => {
    const res = demoZoneDrafts('s-1');
    expect(res.status).toBe('SUCCEEDED');
    expect(res.setup_session_id).toBe('s-1');
    expect(res.drafts.map((d) => [d.recommended_name, d.candidate_kind, d.no_opinion])).toEqual([
      ['단상 개구부', 'OPENING', false],
    ]);
  });

  it('수락하면 지금 놓인 단상 개구부 자리(x 5.2~7.6, y 2.25~4.55, 높이 0~1.4)에 구역을 만든다', () => {
    const [draft] = demoZoneDrafts('s-1').drafts;
    const { zone } = draftCreateRequest(draft!);
    if (zone.op !== 'CREATE') throw new Error('구역 만들기 요청이 아니다');
    const xs = zone.body.polygon_xy.map((p) => p[0]);
    const ys = zone.body.polygon_xy.map((p) => p[1]);
    expect(zone.body.polygon_xy).toHaveLength(4);
    expect(Math.min(...xs)).toBeCloseTo(5.2);
    expect(Math.max(...xs)).toBeCloseTo(7.6);
    expect(Math.min(...ys)).toBeCloseTo(2.25);
    expect(Math.max(...ys)).toBeCloseTo(4.55);
    expect(zone.body.z_min_m).toBeCloseTo(0);
    expect(zone.body.z_max_m).toBeCloseTo(1.4);
    expect(zone.body.severity).toBe('CRITICAL');
  });

  it('맵에서 이미 들어온 같은 자리 구역과 겹치는 가짜 초안은 만들지 않고, 방금 수락한 새 구역은 그대로 둔다', () => {
    const [opening] = demoZoneDrafts('s-1').drafts;
    const openingSpot = draftZoneFootprint(opening!);
    const filtered = demoZoneDrafts('s-1', [
      {
        zoneId: 'z-carried-opening',
        polygon: openingSpot.polygon,
        zBounds: [openingSpot.zMin, openingSpot.zMax],
      },
      {
        zoneId: `${SETUP_NEW_ZONE_PREFIX}0`,
        polygon: openingSpot.polygon,
        zBounds: [openingSpot.zMin, openingSpot.zMax],
      },
      {
        zoneId: 'z-far',
        polygon: [
          [20, 20],
          [22, 20],
          [22, 22],
          [20, 22],
        ],
        zBounds: [0, 2],
      },
    ]);
    expect(filtered.drafts).toEqual([]);
  });
});
