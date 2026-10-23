import { readMockUser } from '@/mocks/mockUser';
import { nextMsgId } from '@/mocks/realtimeScenario';
import type { StompBroker } from '@/mocks/stompBroker';
import type { RiskEventMessage } from '@simai/contracts';
import { http, HttpResponse } from 'msw/http';

// 서버 이벤트 API(S15P31A101-121) 대신 쓰는 mock. 시나리오 전이와 운영자 확인을 한 저장소에 모은다.
// 순번(event_sequence)은 이 저장소가 매긴다. 확인(ACK)도 전이라 순번을 올려 /topic/events 로 다시 보낸다.

/** 해소된 이벤트를 목록에 남기는 시간(ms). */
const KEEP_RESOLVED_MS = 120_000;

export interface EventsMock {
  /** 시나리오 전이를 저장소에 반영하고 보낼 메시지를 돌려준다. 앞선 확인 기록은 이어 간다. */
  record(message: RiskEventMessage): RiskEventMessage;
  /** GET /api/events, POST /api/events/:id/ack */
  handlers: ReturnType<typeof http.get>[];
  /** 저장소의 이벤트 하나(클립 mock 이 읽는다). */
  find(eventId: string): RiskEventMessage['payload'] | undefined;
  /** 다음 ACK 하나를 토큰 만료(401)로 거절한다. 갱신 후 다시 보내기를 30분 기다리지 않고 본다(개발 콘솔 simaiMock). */
  expireAuth(): void;
}

// 서버 보안 필터가 토큰을 거절할 때와 같은 본문(ProblemDetail + code).
const unauthorized = () =>
  HttpResponse.json(
    { status: 401, detail: '로그인이 필요합니다', code: 'UNAUTHORIZED' },
    { status: 401 },
  );

export function createEventsMock({
  broker,
  now = Date.now,
}: {
  broker: StompBroker;
  now?: () => number;
}): EventsMock {
  const events = new Map<string, RiskEventMessage>();
  const nextSequence = (eventId: string) => (events.get(eventId)?.payload.event_sequence ?? 0) + 1;

  function prune(t: number) {
    for (const [id, { payload }] of events) {
      if (payload.lifecycle === 'RESOLVED' && (payload.updated_at ?? 0) < t - KEEP_RESOLVED_MS) {
        events.delete(id);
      }
    }
  }

  function record(message: RiskEventMessage): RiskEventMessage {
    const id = message.payload.event_id;
    const acks = events.get(id)?.payload.acks;
    const stored: RiskEventMessage = {
      ...message,
      payload: { ...message.payload, event_sequence: nextSequence(id), ...(acks ? { acks } : {}) },
    };
    events.set(id, stored);
    prune(message.sent_at);
    return stored;
  }

  function ack(eventId: string, actor: { id: string; role: 'ADMIN' | 'FOREMAN' }, t: number) {
    const prev = events.get(eventId);
    if (!prev || prev.payload.lifecycle === 'RESOLVED') return undefined;
    const stored: RiskEventMessage = {
      ...prev,
      msg_id: nextMsgId(),
      sent_at: t,
      payload: {
        ...prev.payload,
        event_sequence: nextSequence(eventId),
        updated_at: t,
        acks: [
          ...(prev.payload.acks ?? []),
          {
            ack_id: nextMsgId(),
            channel: 'CONTROL_ROOM',
            actor_role: actor.role,
            actor_id: actor.id,
            acked_at: t,
          },
        ],
      },
    };
    events.set(eventId, stored);
    return stored;
  }

  let rejectNextAck = false;

  const handlers = [
    http.get('*/api/events', () => {
      const content = [...events.values()]
        .map((m) => m.payload)
        .sort((a, b) => (b.raised_at ?? 0) - (a.raised_at ?? 0));
      return HttpResponse.json({ content, totalElements: content.length, number: 0, size: 100 });
    }),
    http.post('*/api/events/:id/ack', ({ params, request }) => {
      // mock 로그인 토큰이나 실서버 토큰(혼합 모드)으로 누가 확인했는지 정한다. 만료된 실서버 토큰은 서버처럼 거절한다.
      const user = readMockUser(request.headers.get('Authorization'));
      const t = now();
      if (!user || (user.expiresAt !== undefined && user.expiresAt <= t)) return unauthorized();
      if (rejectNextAck) {
        rejectNextAck = false;
        return unauthorized();
      }
      if (user.role === 'VIEWER') {
        return HttpResponse.json({ message: '확인 권한이 없습니다' }, { status: 403 });
      }
      const stored = ack(String(params.id), { id: user.id, role: user.role }, t);
      if (!stored) {
        return HttpResponse.json({ message: '이미 해소된 이벤트입니다' }, { status: 409 });
      }
      broker.publish('/topic/events', stored);
      return HttpResponse.json(stored.payload);
    }),
  ];

  return {
    record,
    handlers,
    find: (eventId) => events.get(eventId)?.payload,
    expireAuth: () => {
      rejectNextAck = true;
    },
  };
}
