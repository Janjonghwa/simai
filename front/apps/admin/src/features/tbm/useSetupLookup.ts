import { requestSetupSession, setupSessionKey } from '@/features/setup/api';
import { useSetupFlowStore } from '@/features/setup/setup.store';
import type { SetupSessionLookup } from '@/features/tbm/workStage';
import { api } from '@/lib/api';
import { useQuery, type QueryClient } from '@tanstack/react-query';

/** 이 브라우저에서 그 세팅 세션의 구역을 방금 발행했는지. */
const committedHere = (
  state: { zonesCommitted: boolean; activeSessionId: string | null },
  setupSessionId: string | null,
) =>
  state.zonesCommitted &&
  state.activeSessionId !== null &&
  state.activeSessionId === setupSessionId;

/**
 * 작업에 이은 세팅 세션(GET /api/setup-sessions/{id})의 조회 결과와, 이 브라우저에서 그 세션의 구역을 방금 발행했는지.
 * 작업 단계 판정(deriveWorkStages)의 세팅·구역 입력이다. 세팅 세션이 없는 작업은 none, enabled 가 false 면 조회하지 않는다.
 */
export function useSetupLookup(setupSessionId: string | null, enabled: boolean) {
  const query = useQuery({
    queryKey: setupSessionKey(setupSessionId ?? ''),
    queryFn: ({ signal }) => requestSetupSession(api, setupSessionId!, signal),
    enabled: enabled && setupSessionId !== null,
    retry: false,
  });
  const zonesCommittedHere = useSetupFlowStore((s) => committedHere(s, setupSessionId));
  const setup: SetupSessionLookup =
    setupSessionId === null
      ? { kind: 'none' }
      : query.data
        ? { kind: 'ready', session: query.data }
        : query.isError
          ? { kind: 'error' }
          : { kind: 'loading' };
  return { setup, zonesCommittedHere };
}

/**
 * 훅을 쓸 수 없는 곳(여러 작업 중 하나를 누를 때)의 한 번 조회. 세팅 세션을 지금 받아 같은 모양으로 돌려준다.
 */
export async function fetchSetupLookup(
  client: QueryClient,
  setupSessionId: string | null,
): Promise<{ setup: SetupSessionLookup; zonesCommittedHere: boolean }> {
  const zonesCommittedHere = committedHere(useSetupFlowStore.getState(), setupSessionId);
  if (setupSessionId === null) return { setup: { kind: 'none' }, zonesCommittedHere };
  try {
    const session = await client.fetchQuery({
      queryKey: setupSessionKey(setupSessionId),
      queryFn: ({ signal }) => requestSetupSession(api, setupSessionId, signal),
      retry: false,
    });
    return { setup: { kind: 'ready', session }, zonesCommittedHere };
  } catch {
    return { setup: { kind: 'error' }, zonesCommittedHere };
  }
}
