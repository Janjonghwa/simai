import { MOCK_ACCOUNTS } from '@/mocks/accounts';
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
}

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

  // mock 로그인 토큰(mock-token.{계정 id})으로 누가 확인했는지 정한다.
  const accountOf = (request: Request) => {
    const id = request.headers.get('Authorization')?.replace(/^Bearer mock-token\./, '');
    return MOCK_ACCOUNTS.find((account) => account.id === id);
  };

  const handlers = [
    http.get('*/api/events', () => {
      const content = [...events.values()]
        .map((m) => m.payload)
        .sort((a, b) => (b.raised_at ?? 0) - (a.raised_at ?? 0));
      return HttpResponse.json({ content, totalElements: content.length, number: 0, size: 100 });
    }),
    http.post('*/api/events/:id/ack', ({ params, request }) => {
      const account = accountOf(request);
      if (!account) return HttpResponse.json({ message: '로그인이 필요합니다' }, { status: 401 });
      if (account.role === 'VIEWER') {
        return HttpResponse.json({ message: '확인 권한이 없습니다' }, { status: 403 });
      }
      const stored = ack(String(params.id), { id: account.id, role: account.role }, now());
      if (!stored) {
        return HttpResponse.json({ message: '이미 해소된 이벤트입니다' }, { status: 409 });
      }
      broker.publish('/topic/events', stored);
      return HttpResponse.json(stored.payload);
    }),
  ];

  return { record, handlers };
}
