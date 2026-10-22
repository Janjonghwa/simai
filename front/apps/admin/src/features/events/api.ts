import { eventsPageSchema } from '@/features/events/types';
import type { Http } from '@/lib/http';
import { z } from 'zod';

/** 관제 화면이 처음 받는 이벤트 수(명세 12.6 최대 100). 이후는 /topic/events 로 갱신한다. */
export const EVENTS_PAGE_SIZE = 100;

// http 를 인자로 받는다. 테스트가 env 없이 createHttp 로 부를 수 있게 한다.
export function requestEvents(http: Http, signal?: AbortSignal) {
  return http(`/api/events?page=0&size=${EVENTS_PAGE_SIZE}`, { schema: eventsPageSchema, signal });
}

/**
 * 운영자 확인(ACK). 본문의 event_sequence 는 확인 버튼을 누를 때 화면이 보던 상태다(계약 event_ack, EC2 가 Tier 0 로 넘김).
 * 응답 형식은 아직 정해지지 않아 받기만 한다. 화면은 서버가 다시 보내는 risk_event 로 그린다(낙관적 갱신 없음).
 */
export function requestAck(http: Http, eventId: string, eventSequence: number | undefined) {
  return http(`/api/events/${encodeURIComponent(eventId)}/ack`, {
    method: 'POST',
    body: eventSequence === undefined ? {} : { event_sequence: eventSequence },
    schema: z.unknown(),
  });
}
