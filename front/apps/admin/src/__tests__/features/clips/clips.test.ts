import { autoRetained, clipTags, groupClips, hasClips, isDeleted } from '@/features/clips/clips';
import { clipUrlSchema, eventClipsSchema, type Clip } from '@/features/clips/types';
import type { EventItem } from '@/features/events/types';
import { describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;

const clip = (change: Partial<Clip> = {}): Clip => ({
  clip_id: 'c-1',
  camera_id: 'cam1',
  resume_count: 0,
  clip_start_capture_ms: T - 20_000,
  clip_end_capture_ms: T + 20_000,
  truncated_start: false,
  truncated_end: false,
  has_gaps: false,
  retention_hold: false,
  deleted_at_ms: null,
  ...change,
});

const event = (change: Partial<EventItem> = {}): EventItem => ({
  event_id: 'e-1',
  rule_id: 'R1',
  source: 'LIVE',
  lifecycle: 'RAISED',
  verification: 'VERIFIED',
  severity: 'HIGH',
  confidence: 'HIGH',
  needs_check: false,
  world_ids: ['w-0012'],
  zone_id: 'z-02',
  metrics: {},
  rationale: null,
  proposed_severity: null,
  ...change,
});

// 저장소 루트 contracts/ 의 정본. 가정한 클립 칸 이름이 계약 media_clip 과 같은지 본다.
const SCHEMAS = import.meta.glob<{ default: { properties: { payload: { properties: object } } } }>(
  '../../../../../../../contracts/messages/media_clip.schema.json',
  { eager: true },
);

describe('클립 응답 가정 형식', () => {
  it('클립 칸 이름은 계약 media_clip 과 같다(EC2 가 더하는 deleted_at_ms 만 다르다)', () => {
    const schema = Object.values(SCHEMAS)[0]?.default;
    const contractFields = Object.keys(schema?.properties.payload.properties ?? {});
    expect(contractFields.length).toBeGreaterThan(0);
    const fields = Object.keys(clip()).filter((key) => key !== 'deleted_at_ms');
    expect(fields.filter((key) => !contractFields.includes(key))).toEqual([]);
  });

  it('목록은 contract 표시가 있어야 받고, 형식이 틀린 클립 하나는 빼고 나머지를 받는다', () => {
    const body = {
      contract: 'DEMO_UNCONFIRMED',
      event_id: 'e-1',
      clips: [clip(), { ...clip({ clip_id: 'c-2' }), camera_id: 3 }, 'x', clip({ clip_id: 'c-3' })],
    };
    expect(eventClipsSchema.parse(body).clips.map((c) => c.clip_id)).toEqual(['c-1', 'c-3']);
    expect(eventClipsSchema.safeParse({ ...body, contract: 'V1' }).success).toBe(false);
    expect(eventClipsSchema.safeParse({ event_id: 'e-1', clips: [] }).success).toBe(false);
  });

  it('지운 시각은 없어도 된다(null·생략)', () => {
    const withoutDeleted: Record<string, unknown> = { ...clip() };
    delete withoutDeleted.deleted_at_ms;
    const body = { contract: 'DEMO_UNCONFIRMED', event_id: 'e-1', clips: [withoutDeleted] };
    expect(eventClipsSchema.parse(body).clips).toHaveLength(1);
  });

  it('재생 주소는 contract 표시와 비어 있지 않은 주소가 있어야 받는다', () => {
    const body = { contract: 'DEMO_UNCONFIRMED', clip_id: 'c-1', url: '/c.mp4', expires_at_ms: T };
    expect(clipUrlSchema.parse(body).url).toBe('/c.mp4');
    expect(clipUrlSchema.safeParse({ ...body, url: '' }).success).toBe(false);
    expect(clipUrlSchema.safeParse({ ...body, contract: undefined }).success).toBe(false);
  });
});

describe('클립 표시 계산', () => {
  it('클립은 LIVE 의 CRITICAL·HIGH 이벤트에만 있다', () => {
    expect(hasClips(event({ severity: 'CRITICAL' }))).toBe(true);
    expect(hasClips(event({ severity: 'HIGH' }))).toBe(true);
    expect(hasClips(event({ severity: 'CAUTION' }))).toBe(false);
    expect(hasClips(event({ severity: 'INFO' }))).toBe(false);
    expect(hasClips(event({ source: 'PLANNED' }))).toBe(false);
    expect(hasClips(event({ source: 'REPLAY' }))).toBe(false);
  });

  it('CRITICAL 이벤트 클립만 자동 보존이다', () => {
    expect(autoRetained(event({ severity: 'CRITICAL' }))).toBe(true);
    expect(autoRetained(event({ severity: 'HIGH' }))).toBe(false);
  });

  it('위험 구간(재개 횟수)별로 묶고 묶음 안은 카메라 순이다', () => {
    const groups = groupClips([
      clip({ clip_id: 'a', camera_id: 'cam3', resume_count: 1 }),
      clip({ clip_id: 'b', camera_id: 'cam2' }),
      clip({ clip_id: 'c', camera_id: 'cam1', resume_count: 1 }),
      clip({ clip_id: 'd', camera_id: 'cam1' }),
    ]);
    expect(groups.map((g) => [g.resumeCount, g.clips.map((c) => c.clip_id)])).toEqual([
      [0, ['d', 'b']],
      [1, ['c', 'a']],
    ]);
    expect(groupClips([])).toEqual([]);
  });

  it('잘림·프레임 빠짐은 Tier 0 가 알린 것만 짧게 보인다', () => {
    expect(clipTags(clip())).toEqual([]);
    expect(clipTags(clip({ truncated_start: true, truncated_end: true, has_gaps: true }))).toEqual([
      '앞 잘림',
      '뒤 잘림',
      '프레임 빠짐',
    ]);
  });

  it('지운 시각이 있으면 지운 클립이다', () => {
    expect(isDeleted(clip())).toBe(false);
    expect(isDeleted(clip({ deleted_at_ms: undefined }))).toBe(false);
    expect(isDeleted(clip({ deleted_at_ms: T }))).toBe(true);
  });
});
