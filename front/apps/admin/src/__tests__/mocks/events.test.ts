import { createEventsMock } from '@/mocks/events';
import { riskEventsBetween } from '@/mocks/realtimeScenario';
import type { StompBroker } from '@/mocks/stompBroker';
import { validateMessage, type RiskEventMessage } from '@simai/contracts';
import { setupServer } from 'msw/node';
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest';

const published: unknown[] = [];
const broker = {
  publish: (_: string, body: unknown) => published.push(body),
} as unknown as StompBroker;
const NOW = 1_760_342_450_000;
const mock = createEventsMock({ broker, now: () => NOW });
const server = setupServer(...mock.handlers);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
beforeEach(() => {
  published.length = 0;
});

const edge = riskEventsBetween(0, 40_000).filter((m) => m.payload.rule_id === 'R2');
const [raised, updated, resolved] = edge as [RiskEventMessage, RiskEventMessage, RiskEventMessage];

const ack = (eventId: string, account: string) =>
  fetch(`http://api.test/api/events/${eventId}/ack`, {
    method: 'POST',
    headers: { Authorization: `Bearer mock-token.${account}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_sequence: 1 }),
  });

describe('createEventsMock', () => {
  it('ACK 하면 확인 기록을 더하고 순번을 올려 /topic/events 로 보내며, 다음 전이에도 확인 기록이 이어진다', async () => {
    expect(mock.record(raised).payload.event_sequence).toBe(1);

    const response = await ack(raised.payload.event_id, 'admin');
    expect(response.status).toBe(200);
    const body = (await response.json()) as RiskEventMessage['payload'];
    expect(body.event_sequence).toBe(2);
    expect(body.acks).toMatchObject([{ actor_id: 'admin', actor_role: 'ADMIN', acked_at: NOW }]);
    expect(published).toHaveLength(1);
    const sent = published[0] as RiskEventMessage;
    expect(validateMessage('risk_event', sent).ok).toBe(true);

    const next = mock.record(updated);
    expect(next.payload.event_sequence).toBe(3);
    expect(next.payload.acks).toHaveLength(1);
    expect(validateMessage('risk_event', next).ok).toBe(true);
  });

  it('목록은 저장소의 이벤트를 최신 발생순으로 준다', async () => {
    const page = (await (await fetch('http://api.test/api/events?page=0&size=100')).json()) as {
      content: RiskEventMessage['payload'][];
    };
    expect(page.content.map((e) => e.event_id)).toContain(raised.payload.event_id);
  });

  it('열람 계정은 403, 해소된 이벤트는 409, 로그인 없으면 401 이다', async () => {
    expect((await ack(raised.payload.event_id, 'viewer')).status).toBe(403);
    mock.record(resolved);
    expect((await ack(raised.payload.event_id, 'admin')).status).toBe(409);
    expect(
      (await fetch(`http://api.test/api/events/${raised.payload.event_id}/ack`, { method: 'POST' }))
        .status,
    ).toBe(401);
    expect(published).toHaveLength(0);
  });

  it('실서버 토큰(혼합 모드)은 sub·role 로 확인을 남기고, 만료됐거나 expireAuth 뒤 한 번은 401 이다', async () => {
    const local = createEventsMock({ broker, now: () => NOW });
    server.use(...local.handlers);
    try {
      local.record(raised);
      const jwt = (claims: object) =>
        `h.${btoa(JSON.stringify(claims)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')}.s`;
      const ackWith = (token: string) =>
        fetch(`http://api.test/api/events/${raised.payload.event_id}/ack`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}` },
        });
      const expired = jwt({ sub: '42', role: 'ADMIN', exp: NOW / 1000 - 1 });
      const live = jwt({ sub: '42', role: 'FOREMAN', exp: NOW / 1000 + 1800 });

      const rejected = await ackWith(expired);
      expect(rejected.status).toBe(401);
      expect(await rejected.json()).toMatchObject({ code: 'UNAUTHORIZED' });

      local.expireAuth();
      expect((await ackWith(live)).status).toBe(401);
      const accepted = await ackWith(live);
      expect(accepted.status).toBe(200);
      expect(((await accepted.json()) as RiskEventMessage['payload']).acks).toEqual([
        expect.objectContaining({ actor_id: '42', actor_role: 'FOREMAN' }),
      ]);
    } finally {
      server.resetHandlers();
    }
  });
});
