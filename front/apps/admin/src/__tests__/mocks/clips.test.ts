import { clipUrlSchema, eventClipsSchema } from '@/features/clips/types';
import { createClipsMock } from '@/mocks/clips';
import type { RiskEventPayload } from '@simai/contracts';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

const T = 1_760_342_400_000;
/** 위험 구간 끝(발생 + 20초) + 업로드(4초) + 카메라 순서마다 1.5초. */
const ALL_UPLOADED = T + 20_000 + 4_000 + 3_000;

let t = T;
const events = new Map<string, RiskEventPayload>();

const event = (id: string, change: Partial<RiskEventPayload> = {}) =>
  ({
    event_id: id,
    event_sequence: 1,
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
    raised_at: T,
    updated_at: T,
    resume_count: 0,
    ...change,
  }) as RiskEventPayload;

const findEvent = (id: string) => events.get(id);
const mock = createClipsMock({ findEvent, now: () => t, video: async () => '/clip.webm' });
const server = setupServer(...mock.handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());
beforeEach(() => {
  t = ALL_UPLOADED;
});

const ADMIN = 'Bearer mock-token.admin';
const request = (path: string, init: RequestInit = {}, authorization: string | null = ADMIN) =>
  fetch(`http://api.test${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(authorization ? { Authorization: authorization } : {}),
    },
  });
const list = async (eventId: string) =>
  eventClipsSchema.parse(await (await request(`/api/events/${eventId}/clips`)).json()).clips;
const setHold = (clipId: string, hold: unknown) =>
  request(`/api/clips/${clipId}/retention`, {
    method: 'PUT',
    body: JSON.stringify({ retention_hold: hold }),
  });

// 저장소 루트 contracts/ 의 정본.
const SCHEMAS = import.meta.glob<{
  default: { properties: { payload: { properties: object; required: string[] } } };
}>('../../../../../../contracts/messages/media_clip.schema.json', { eager: true });

describe('클립 mock', () => {
  it('클립은 위험 구간이 끝나고 올라간 뒤에 생기고, cam2 는 앞이 잘리고 cam3 는 프레임이 빠진다', async () => {
    events.set('e-high', event('e-high'));
    t = T + 20_000 + 4_000 - 1;
    expect(await list('e-high')).toEqual([]);

    t = ALL_UPLOADED;
    const clips = await list('e-high');
    expect(clips.map((c) => c.camera_id)).toEqual(['cam1', 'cam2', 'cam3']);
    expect(clips.map((c) => [c.truncated_start, c.has_gaps])).toEqual([
      [false, false],
      [true, false],
      [false, true],
    ]);
    expect(clips[0]).toMatchObject({
      clip_start_capture_ms: T - 20_000,
      clip_end_capture_ms: T + 20_000,
      retention_hold: false,
      deleted_at_ms: null,
    });
  });

  it('클립 칸은 계약 media_clip 의 칸과 같고 필수 칸을 모두 싣는다(EC2 의 deleted_at_ms 만 더한다)', async () => {
    events.set('e-fields', event('e-fields'));
    const body = (await (await request('/api/events/e-fields/clips')).json()) as {
      clips: Record<string, unknown>[];
    };
    const schema = Object.values(SCHEMAS)[0]?.default.properties.payload;
    const fields = Object.keys(schema?.properties ?? {});
    const keys = Object.keys(body.clips[0] ?? {}).filter((key) => key !== 'deleted_at_ms');
    expect(fields.length).toBeGreaterThan(0);
    expect(keys.filter((key) => !fields.includes(key))).toEqual([]);
    expect((schema?.required ?? []).filter((key) => !keys.includes(key))).toEqual([]);
  });

  it('재개 구간은 따로 클립을 만든다', async () => {
    events.set('e-resume', event('e-resume'));
    await list('e-resume');
    events.set('e-resume', event('e-resume', { resume_count: 1, updated_at: T + 60_000 }));
    t = T + 60_000 + 27_000;
    const clips = await list('e-resume');
    expect(clips.map((c) => c.resume_count)).toEqual([0, 0, 0, 1, 1, 1]);
  });

  it('LIVE 의 CRITICAL·HIGH 가 아니면 클립이 없고, 없는 이벤트는 404 다', async () => {
    events.set('e-caution', event('e-caution', { severity: 'CAUTION' }));
    events.set('e-planned', event('e-planned', { source: 'PLANNED' }));
    expect(await list('e-caution')).toEqual([]);
    expect(await list('e-planned')).toEqual([]);
    expect((await request('/api/events/none/clips')).status).toBe(404);
  });

  it('재생 주소는 가정 형식으로 주고, 없는 클립은 404 다', async () => {
    events.set('e-url', event('e-url'));
    const [first] = await list('e-url');
    const body = clipUrlSchema.parse(await (await request(`/api/clips/${first?.clip_id}`)).json());
    expect(body.url).toBe('/clip.webm');
    expect(body.expires_at_ms).toBeGreaterThan(t);
    expect((await request('/api/clips/none')).status).toBe(404);
  });

  it('시험 영상을 만들지 못하면 재생 주소 대신 500 을 준다', async () => {
    const failing = createClipsMock({
      findEvent,
      now: () => t,
      video: () => Promise.reject(new Error('MediaRecorder 없음')),
    });
    server.use(...failing.handlers);
    events.set('e-nofilm', event('e-nofilm'));
    const [first] = await list('e-nofilm');
    expect((await request(`/api/clips/${first?.clip_id}`)).status).toBe(500);
  });

  it('HIGH 클립은 관리자가 보존 표시를 붙이고 뗀다', async () => {
    events.set('e-hold', event('e-hold'));
    const [first] = await list('e-hold');
    expect((await setHold(first!.clip_id, true)).status).toBe(200);
    expect((await list('e-hold'))[0]?.retention_hold).toBe(true);
    expect((await setHold(first!.clip_id, false)).status).toBe(200);
    expect((await list('e-hold'))[0]?.retention_hold).toBe(false);
    expect((await setHold(first!.clip_id, 'yes')).status).toBe(400);
  });

  it('CRITICAL 클립은 자동 보존이라 뗄 수 없다', async () => {
    events.set('e-critical', event('e-critical', { severity: 'CRITICAL', rule_id: 'R2' }));
    const [first] = await list('e-critical');
    expect(first?.retention_hold).toBe(true);
    expect((await setHold(first!.clip_id, false)).status).toBe(409);
  });

  it('클립은 관리자만 본다(로그인하지 않았으면 401, 다른 역할은 403)', async () => {
    events.set('e-auth', event('e-auth'));
    const [first] = await list('e-auth');
    for (const path of ['/api/events/e-auth/clips', `/api/clips/${first?.clip_id}`]) {
      expect((await request(path, {}, null)).status).toBe(401);
      expect((await request(path, {}, 'Bearer mock-token.foreman')).status).toBe(403);
      expect((await request(path, {}, 'Bearer mock-token.viewer')).status).toBe(403);
    }
    const put = await request(
      `/api/clips/${first?.clip_id}/retention`,
      { method: 'PUT', body: JSON.stringify({ retention_hold: true }) },
      'Bearer mock-token.viewer',
    );
    expect(put.status).toBe(403);
  });

  it('만료된 실서버 토큰(혼합 모드)은 401 이다', async () => {
    events.set('e-jwt', event('e-jwt'));
    // 서명은 보지 않으므로 payload(sub·role·exp)만 맞춘다.
    const jwt = (exp: number) =>
      `Bearer h.${btoa(JSON.stringify({ sub: 'u-1', role: 'ADMIN', exp })).replace(/=+$/, '')}.s`;
    const sec = Math.floor(t / 1000);
    expect((await request('/api/events/e-jwt/clips', {}, jwt(sec - 1))).status).toBe(401);
    expect((await request('/api/events/e-jwt/clips', {}, jwt(sec + 60))).status).toBe(200);
  });
});
