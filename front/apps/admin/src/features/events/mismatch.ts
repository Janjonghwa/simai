import type { Realtime } from '@/lib/stomp';
import type { ActionMismatchPayload } from '@simai/contracts';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';

/** 조치 불일치(조치 완료인데 이벤트가 관측상 아직 활성, 명세 8.4)의 Query 키. 데이터는 Mismatches. */
export const MISMATCH_KEY = ['events', 'mismatch'] as const;

/** 이벤트 하나의 조치 불일치. 조치 티켓이 완료됐는데 연결된 이벤트가 아직 활성이다(/topic/admin/events, 관리자 전용). */
export interface EventMismatch {
  ticketId: string;
  ticketStatus: 'DONE';
  /** 불일치를 판단한 이벤트 상태의 순번. */
  eventSequence: number;
  /** EC2 가 불일치를 알아낸 시각(ms). */
  detectedAt: number;
}

/** event_id → 그 이벤트의 불일치. 없는 이벤트는 불일치가 없는 것이다. */
export type Mismatches = Readonly<Record<string, EventMismatch>>;

/** 불일치가 없을 때 늘 같은 객체를 준다. 받는 화면이 다시 그리지 않는다. */
const NONE: Mismatches = Object.freeze({});

// 서버에 조회 API 가 없어 이 값은 STOMP 로 받은 것뿐이다. 화면이 없는 동안(다른 화면에 있을 때) 캐시가 gc 로 지워지면
// 켜 둔 불일치를 잃는다. 지워지지 않게 한다. 로그아웃 때 queryClient.clear() 가 비운다(bindSession).
const OPTIONS = { staleTime: Infinity, gcTime: Infinity } as const;

/** 이미 있는 것보다 오래된 상태(순번이 작거나, 같은 순번에서 더 일찍 알아낸 것)인지. */
const isOlder = (payload: ActionMismatchPayload, stored: EventMismatch) =>
  payload.event_sequence < stored.eventSequence ||
  (payload.event_sequence === stored.eventSequence && payload.detected_at < stored.detectedAt);

/**
 * action_mismatch 한 건을 캐시에 반영한다.
 * - cleared 가 true 면(이벤트 해소로 불일치가 풀림) 그 이벤트의 항목을 지운다.
 * - 이미 있는 것보다 오래된 메시지(순번·알아낸 시각)는 무시한다. 해소(cleared)도 마찬가지다: 더 새 상태의 불일치를 지우지 않는다.
 * - 이벤트가 이미 해소된 상태의 불일치(cleared 아님)도 그대로 담는다. 해소된 카드에서 숨기는 것은 보는 쪽이 정한다.
 * 바뀐 것이 없으면 캐시 값을 그대로 둔다(구독하는 화면이 다시 그리지 않는다).
 * STOMP 한 연결은 순서를 지켜 오므로 뒤바뀜은 드물다. 지운 뒤 도착한 이전 순번을 막는 표식은 두지 않는다.
 */
export function applyMismatch(queryClient: QueryClient, payload: ActionMismatchPayload) {
  const old = queryClient.getQueryData<Mismatches>(MISMATCH_KEY);
  const stored = old?.[payload.event_id];
  if (stored && isOlder(payload, stored)) return;
  if (payload.cleared) {
    if (!old || !stored) return;
    const rest = { ...old };
    delete rest[payload.event_id];
    queryClient.setQueryData<Mismatches>(MISMATCH_KEY, rest);
    return;
  }
  queryClient.setQueryData<Mismatches>(MISMATCH_KEY, {
    ...old,
    [payload.event_id]: {
      ticketId: payload.ticket_id,
      ticketStatus: payload.ticket_status,
      eventSequence: payload.event_sequence,
      detectedAt: payload.detected_at,
    },
  });
}

/**
 * 실시간 연결의 조치 불일치를 캐시에 잇는다(CONVENTION: 화면이 따로 쓰는 메시지는 Query 캐시를 갱신한다).
 * 관리자 연결만 /topic/admin/events 를 구독하므로 다른 역할에서는 아무것도 오지 않는다.
 * 끊긴 사이 놓친 것은 다시 받을 길이 없어(조회 API 없음) 캐시를 비우지 않는다. 해소된 이벤트의 불일치는 보는 쪽이 숨긴다.
 * 해제 함수를 돌려준다.
 */
export function bindMismatchCache(realtime: Pick<Realtime, 'on'>, queryClient: QueryClient) {
  queryClient.setQueryDefaults(MISMATCH_KEY, OPTIONS);
  return realtime.on('actionMismatch', (payload) => applyMismatch(queryClient, payload));
}

/**
 * 조치 불일치가 있는 이벤트(event_id → 불일치). 없으면 늘 같은 빈 객체다.
 * 불일치를 바꿀 때만 새 객체가 되므로 useQuery 의 구조 공유로 바뀔 때만 다시 그린다. 요청은 하지 않는다:
 * queryFn 은 지금 캐시를 그대로 돌려줘 다시 받기(invalidate)가 값을 지우지 않는다.
 */
export function useMismatches(): Mismatches {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: MISMATCH_KEY,
    queryFn: () => queryClient.getQueryData<Mismatches>(MISMATCH_KEY) ?? NONE,
    ...OPTIONS,
    // 요청이 없으니 실패할 일도 없다. 혹시 던져도 다시 시도하지 않는다.
    retry: false,
    refetchOnMount: false,
  });
  return data ?? NONE;
}
