import { requestSetupSession, setupSessionKey } from '@/features/setup/api';
import { useSetupFlowStore } from '@/features/setup/setup.store';
import type { SetupSessionLookup } from '@/features/tbm/workStage';
import { api } from '@/lib/api';
import { useQuery } from '@tanstack/react-query';

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
