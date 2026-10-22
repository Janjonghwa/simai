import { ackErrorMessage } from '@/features/events/ack';
import { requestAck } from '@/features/events/api';
import { applyEvents } from '@/features/events/eventsCache';
import { eventItemSchema } from '@/features/events/types';
import { api } from '@/lib/api';
import { useMutation, useMutationState, useQueryClient } from '@tanstack/react-query';

const ACK_MUTATION_KEY = ['events', 'ack'] as const;

interface AckInput {
  eventId: string;
  /** 확인 버튼을 누를 때 화면이 보던 상태. */
  eventSequence: number | undefined;
}

/**
 * 이벤트 하나의 경보 확인(ACK). 낙관적 갱신을 하지 않는다(CONVENTION). 응답이 갱신된 이벤트면 바로 반영하고,
 * 아니면 서버가 다시 보내는 /topic/events 로 그린다.
 * 같은 이벤트의 확인 버튼이 배너·카드·상세에 함께 있다. 어느 버튼으로 보냈든 보내는 중이면 모두 막는다.
 */
export function useAckEvent(eventId: string) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationKey: ACK_MUTATION_KEY,
    mutationFn: (input: AckInput) => requestAck(api, input.eventId, input.eventSequence),
    onSuccess: (data) => {
      const event = eventItemSchema.safeParse(data);
      if (event.success) applyEvents(queryClient, [event.data]);
    },
  });
  const sending = useMutationState({
    filters: {
      mutationKey: ACK_MUTATION_KEY,
      status: 'pending',
      predicate: (m) => (m.state.variables as AckInput | undefined)?.eventId === eventId,
    },
  });
  // 배너는 같은 버튼으로 다음 경보를 보인다. 이 이벤트로 보낸 요청의 실패만 보인다.
  const failed = mutation.isError && mutation.variables.eventId === eventId;
  return {
    ack: (eventSequence: number | undefined) => mutation.mutate({ eventId, eventSequence }),
    pending: sending.length > 0,
    error: failed ? ackErrorMessage(mutation.error) : undefined,
  };
}
