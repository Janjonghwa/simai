import { CONTROL_ROOM_CHANNEL } from '@/features/events/constants';
import { isActive } from '@/features/events/entries';
import { eventsPageSchema, type EventItem, type EventsPage } from '@/features/events/types';
import type { Http } from '@/lib/http';
import { z } from 'zod';

/** 쪽마다 받는 이벤트 수(명세 12.6 최대 100). */
export const EVENTS_PAGE_SIZE = 100;

/** 최대로 받는 쪽 수. 오래된 해소 이벤트가 아주 많아도 로그인마다 요청이 끝없이 늘지 않게 한다. */
export const EVENTS_MAX_PAGES = 5;

/** 다음 쪽이 더 있는지. totalElements 가 없으면 쪽이 꽉 찼는지로 본다. */
function hasNextPage(result: EventsPage, page: number): boolean {
  if (result.content.length === 0) return false;
  const size = result.size ?? EVENTS_PAGE_SIZE;
  if (result.totalElements === undefined) return result.content.length >= size;
  return ((result.number ?? page) + 1) * size < result.totalElements;
}

/**
 * 관제 화면이 처음(재연결 때도) 받는 이벤트 목록. 이후는 /topic/events 로 갱신한다.
 * 서버는 해소된 이벤트·감사 전용(AUDIT_ONLY)까지 최신순으로 줘서, 해소된 것이 많으면 아직 진행 중인 오래된 경보가
 * 첫 쪽 밖으로 밀린다. 진행 중 이벤트가 하나도 없는 쪽을 만나거나, 쪽이 끝나거나, EVENTS_MAX_PAGES 에 닿을 때까지 이어 받는다.
 * 다음 쪽은 앞 쪽의 내용을 보고 정하므로 차례로 받는다.
 */
export async function requestEvents(
  http: Http,
  signal?: AbortSignal,
): Promise<{ content: EventItem[] }> {
  const content: EventItem[] = [];
  for (let page = 0; page < EVENTS_MAX_PAGES; page++) {
    const result = await http(`/api/events?page=${page}&size=${EVENTS_PAGE_SIZE}`, {
      schema: eventsPageSchema,
      signal,
    });
    content.push(...result.content);
    if (!hasNextPage(result, page) || !result.content.some(isActive)) break;
  }
  return { content };
}

/**
 * 운영자 확인(ACK). 본문의 event_sequence 는 확인 버튼을 누를 때 화면이 보던 상태다(계약 event_ack, EC2 가 Tier 0 로 넘김).
 * channel 은 관제 화면이라 CONTROL_ROOM 이다. 서버는 갱신된 이벤트 전체(새 acks 포함)를 돌려주고,
 * 실패는 ProblemDetail 의 code(EVENT_NOT_FOUND·EVENT_RESOLVED·INVALID_EVENT_SEQUENCE·INVALID_ACK_CHANNEL)로 알린다.
 * 응답을 읽는 쪽이 이벤트 형식인지 따로 본다. 확인은 서버에 반영됐는데 응답 형식 탓에 실패로 보이지 않게 z.unknown 으로 받는다.
 */
export function requestAck(http: Http, eventId: string, eventSequence: number | undefined) {
  return http(`/api/events/${encodeURIComponent(eventId)}/ack`, {
    method: 'POST',
    body: {
      ...(eventSequence === undefined ? {} : { event_sequence: eventSequence }),
      channel: CONTROL_ROOM_CHANNEL,
    },
    schema: z.unknown(),
  });
}
