import { markSetupZonesCommitted } from '@/features/setup/setup.store';
import { requestStaticObjects, STATIC_OBJECTS_KEY } from '@/features/surfaces/api';
import type { WorkSurface } from '@/features/surfaces/types';
import { commitZone, requestZones, ZONES_KEY } from '@/features/zones/api';
import {
  applyCommitted,
  invalidateCommitted,
  SURFACE_ID_FIELD,
  ZONE_ID_FIELD,
} from '@/features/zones/commitCache';
import { CommitBlockedError } from '@/features/zones/commitErrors';
import type { Zone, ZoneCommitRequest, ZoneCommitResult } from '@/features/zones/types';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import { fieldOf, needsRefetch } from '@/lib/writeFailure';
import {
  useMutation,
  useQueryClient,
  type QueryClient,
  type QueryKey,
} from '@tanstack/react-query';

/** 저장 mutation 키. */
const COMMIT_MUTATION_KEY = [...ZONES_KEY, 'commit'] as const;

/** 서버가 구성 묶음을 만들지 못해 구역·작업면 저장까지 모두 되돌린 오류 이름(ProblemDetail code). */
const BUNDLE_FAILURE_CODES: ReadonlySet<string> = new Set([
  'CURRENT_CALIBRATION_MISSING',
  'CALIBRATION_MISSING',
  'RULES_MISSING',
  'PUBLIC_URL_NOT_SET',
]);

/** 저장 직전에 서버의 지금 버전을 확인할 목록(구역·작업면). */
interface VersionedList {
  target: CommitBlockedError['target'];
  key: QueryKey;
  load: (signal?: AbortSignal) => Promise<unknown[]>;
  idField: string;
  versionField: string;
}

const ZONE_LIST: VersionedList = {
  target: 'zone',
  key: ZONES_KEY,
  load: (signal) => requestZones(api, signal),
  idField: ZONE_ID_FIELD,
  versionField: 'zone_version',
};

const SURFACE_LIST: VersionedList = {
  target: 'surface',
  key: STATIC_OBJECTS_KEY,
  load: (signal) => requestStaticObjects(api, signal),
  idField: SURFACE_ID_FIELD,
  versionField: 'object_version',
};

/**
 * 서버는 고치기 시작한 버전을 받지 않고 기록을 통째로 바꾼다. 그 사이 다른 관리자가 새 버전을 저장했으면 그 변경을
 * 덮지 않도록 보내기 전에 지금 버전을 다시 받아 보고 멈춘다(받은 목록은 화면에 들어간다). 같은 순간 부딪친 저장은 서버가 409 로 막는다.
 */
async function assertNotChanged(
  queryClient: QueryClient,
  list: VersionedList,
  id: string,
  version: number,
) {
  const latest = await queryClient
    .fetchQuery({
      queryKey: list.key,
      queryFn: ({ signal }) => list.load(signal),
      staleTime: 0,
    })
    .catch((cause: unknown) => {
      throw new CommitBlockedError(list.target, 'UNCHECKED', { cause });
    });
  const current = fieldOf(
    latest.find((item) => fieldOf(item, list.idField) === id),
    list.versionField,
  );
  if (typeof current === 'number' && current !== version) {
    throw new CommitBlockedError(list.target, 'CHANGED');
  }
}

export interface CommitInput {
  request: ZoneCommitRequest;
  /** 고치기 시작한 구역(새 구역이면 null)과 이은 작업면. 요청이 덮거나 보관하는 것만 버전을 맞춰 본다. */
  zone: Zone | null;
  linked: WorkSurface | null;
}

/** 요청이 덮거나 보관하는 구역·작업면의 버전을 서버의 지금 버전과 맞춰 본다(KEEP·CREATE 는 덮지 않는다). */
async function assertUnchanged(queryClient: QueryClient, { request, zone, linked }: CommitInput) {
  const zoneOp = request.zone.op;
  const checks: Promise<void>[] = [];
  if (zone && (zoneOp === 'UPDATE' || zoneOp === 'ARCHIVE')) {
    checks.push(assertNotChanged(queryClient, ZONE_LIST, zone.zoneId, zone.version));
  }
  if (linked && request.surface && request.surface.op !== 'CREATE') {
    checks.push(assertNotChanged(queryClient, SURFACE_LIST, linked.objectId, linked.version));
  }
  await Promise.all(checks);
}

export interface CommitDone {
  result: ZoneCommitResult;
  /** 묶음을 만들지 못해 저장만 한 경우의 오류. 묶음까지 만들었으면 null. */
  publishError: ApiError | null;
}

/**
 * 구성 묶음까지 한 번에 보낸다. 묶음을 만들 수 없는 현장(보정 문서 없음, 공개 주소 미설정)에서는 서버가 구역·작업면 저장도
 * 되돌리므로, 같은 본문을 묶음 없이(publish: false) 한 번 더 보내 저장만 한다.
 */
async function send(request: ZoneCommitRequest): Promise<CommitDone> {
  try {
    return { result: await commitZone(api, { ...request, publish: true }), publishError: null };
  } catch (error) {
    if (!(error instanceof ApiError) || !BUNDLE_FAILURE_CODES.has(error.code ?? '')) throw error;
    return { result: await commitZone(api, { ...request, publish: false }), publishError: error };
  }
}

/**
 * 구역·작업면 저장을 요청 하나로 보낸다(POST /api/zones/commit). 서버가 한 트랜잭션으로 처리해 실패하면 아무것도 바뀌지
 * 않는다. 낙관적 갱신을 하지 않고 응답(새 버전·묶음)으로 목록을 바꾼다(CONVENTION). 결과를 알 수 없는 실패(연결 끊김·시간
 * 초과·응답 오류·서버 오류)면 이미 저장됐을 수 있어 세 목록을 다시 받는다.
 */
export function useZoneCommitMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationKey: COMMIT_MUTATION_KEY,
    mutationFn: async (input: CommitInput): Promise<CommitDone> => {
      await assertUnchanged(queryClient, input);
      return send(input.request);
    },
    onSuccess: ({ result }, { request }) => {
      applyCommitted(queryClient, request, result);
      markSetupZonesCommitted();
    },
    onError: (error) => {
      if (needsRefetch(error)) invalidateCommitted(queryClient);
    },
  });
}
