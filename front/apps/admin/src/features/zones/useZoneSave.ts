import type { ViewerEditBox } from '@/features/viewer3d';
import { createZone, deleteZone, requestZones, updateZone, ZONES_KEY } from '@/features/zones/api';
import type { Zone, ZoneBody, ZoneItem, ZonesResponse } from '@/features/zones/types';
import { bodyOf, type ZoneForm } from '@/features/zones/zoneForm';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import {
  fieldOf,
  HTTP_CONFLICT,
  HTTP_FORBIDDEN,
  needsRefetch,
  outcomeUnknown,
} from '@/lib/writeFailure';
import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query';

/** 저장·만들기·보관 mutation 키. */
const SAVE_MUTATION_KEY = [...ZONES_KEY, 'save'] as const;
const CREATE_MUTATION_KEY = [...ZONES_KEY, 'create'] as const;
const ARCHIVE_MUTATION_KEY = [...ZONES_KEY, 'archive'] as const;

const idOf = (item: unknown) => fieldOf(item, 'zone_id');

/** 받은 목록에서 그 구역의 지금 버전. 없거나 읽을 수 없으면 undefined 다. */
function versionIn(list: ZonesResponse, zoneId: string): number | undefined {
  const version = fieldOf(
    list.find((item) => idOf(item) === zoneId),
    'zone_version',
  );
  return typeof version === 'number' ? version : undefined;
}

type SaveBlockedReason = 'CHANGED' | 'UNCHECKED';

/**
 * 저장 직전 확인에서 멈춘 저장(서버에 보내지 않았다). CHANGED 는 그 사이 다른 관리자가 새 버전을 저장한 것이고(받은 목록은
 * 이미 화면에 넣었다), UNCHECKED 는 지금 버전을 확인하지 못한 것이다.
 */
class ZoneSaveBlockedError extends Error {
  readonly reason: SaveBlockedReason;

  constructor(reason: SaveBlockedReason, options?: { cause?: unknown }) {
    super(`구역 저장을 보내지 않았다 (${reason})`, options);
    this.name = 'ZoneSaveBlockedError';
    this.reason = reason;
  }
}

/** 저장 응답(새 버전)을 구역 목록에 넣는다. 같은 ID 는 바꾸고, 없으면 끝에 더한다. */
function putZone(queryClient: QueryClient, zone: ZoneItem) {
  queryClient.setQueryData<ZonesResponse>(ZONES_KEY, (prev) => {
    if (!prev) return prev;
    const found = prev.some((item) => idOf(item) === zone.zone_id);
    return found
      ? prev.map((item) => (idOf(item) === zone.zone_id ? zone : item))
      : [...prev, zone];
  });
}

/** 보관(삭제)한 구역을 목록에서 뺀다. */
function removeZone(queryClient: QueryClient, zoneId: string) {
  queryClient.setQueryData<ZonesResponse>(ZONES_KEY, (prev) =>
    prev ? prev.filter((item) => idOf(item) !== zoneId) : prev,
  );
}

/** 서버가 정한 오류 이름(ProblemDetail code): 바닥 다각형·높이 범위가 계약 밖. */
const INVALID_POLYGON_CODE = 'INVALID_POLYGON';
const INVALID_Z_RANGE_CODE = 'INVALID_Z_RANGE';

const CHANGED_TEXT =
  '그 사이 다른 버전이 저장되어 최신 구역을 다시 받았습니다. 확인한 뒤 다시 저장하세요.';
const OUTCOME_UNKNOWN_TEXT =
  '저장됐는지 확인하지 못해 최신 구역을 다시 받았습니다. 확인한 뒤 다시 시도하세요.';
const FALLBACK_TEXT = '저장하지 못했습니다. 다시 시도하세요.';

/** 저장 직전 확인에서 멈춘 이유별 안내. */
const BLOCKED_TEXT: Record<SaveBlockedReason, string> = {
  CHANGED: CHANGED_TEXT,
  UNCHECKED: '최신 구역을 확인하지 못해 저장하지 않았습니다. 다시 시도하세요.',
};

/** 서버가 오류 상태로 답했을 때의 안내(상태별). */
const STATUS_TEXT = new Map<number, string>([
  [HTTP_CONFLICT, CHANGED_TEXT],
  [HTTP_FORBIDDEN, '구역을 저장할 권한이 없습니다.'],
]);

/** 서버가 오류 상태로 답했을 때의 안내(오류 이름별). 상태 안내가 없을 때만 쓴다. */
const CODE_TEXT = new Map<string, string>([
  [INVALID_POLYGON_CODE, '바닥 다각형이 꼬였거나 넓이가 없어 저장하지 못했습니다.'],
  [INVALID_Z_RANGE_CODE, '높이 범위가 맞지 않아 저장하지 못했습니다(최저 < 최고).'],
]);

export function saveErrorText(error: unknown): string {
  if (error instanceof ZoneSaveBlockedError) return BLOCKED_TEXT[error.reason];
  if (!(error instanceof ApiError)) return FALLBACK_TEXT;
  const known = STATUS_TEXT.get(error.status) ?? CODE_TEXT.get(error.code ?? '');
  if (known !== undefined) return known;
  return outcomeUnknown(error) ? OUTCOME_UNKNOWN_TEXT : FALLBACK_TEXT;
}

/**
 * 서버는 고치기 시작한 버전을 받지 않고 기록을 통째로 바꾼다. 그 사이 다른 관리자가 새 버전을 저장했으면 그 변경을
 * 덮지 않도록 보내기 전에 지금 버전을 다시 받아 보고 멈춘다(받은 목록은 화면에 들어간다). 같은 순간 부딪친 저장은 서버가 409 로 막는다.
 */
async function assertNotChanged(queryClient: QueryClient, zone: Zone) {
  const latest = await queryClient
    .fetchQuery({
      queryKey: ZONES_KEY,
      queryFn: ({ signal }) => requestZones(api, signal),
      staleTime: 0,
    })
    .catch((cause: unknown) => {
      throw new ZoneSaveBlockedError('UNCHECKED', { cause });
    });
  const current = versionIn(latest, zone.zoneId);
  if (current !== undefined && current !== zone.version) {
    throw new ZoneSaveBlockedError('CHANGED');
  }
}

export interface SaveZoneInput {
  zone: Zone;
  form: ZoneForm;
  /** 고친 형상. 형상을 고치지 않았으면 null 이다. */
  box: ViewerEditBox | null;
}

/**
 * 구역 저장(새 버전)과 새 구역 만들기. 낙관적 갱신을 하지 않고 응답(새 버전)으로 목록을 바꾼다(CONVENTION).
 * 저장은 현장에 바로 가지 않는다. 구성 묶음을 발행해야 Tier 0 가 받는다(명세 3.2).
 */
export function useZoneSave() {
  const queryClient = useQueryClient();
  // 진행 중인 목록 다시 받기(409 뒤 등)는 이 버전보다 옛 목록을 줄 수 있어 취소하고 응답을 넣은 뒤 다시 받는다.
  const applyWritten = async (zone: ZoneItem) => {
    const refetching = queryClient.isFetching({ queryKey: ZONES_KEY }) > 0;
    await queryClient.cancelQueries({ queryKey: ZONES_KEY });
    putZone(queryClient, zone);
    if (refetching) void queryClient.invalidateQueries({ queryKey: ZONES_KEY });
  };
  // 최신 구역을 다시 받는다(받은 형상이 다르면 화면이 고치던 초안을 버린다).
  const refetchAfter = (error: unknown) => {
    if (needsRefetch(error)) void queryClient.invalidateQueries({ queryKey: ZONES_KEY });
  };
  const save = useMutation({
    mutationKey: SAVE_MUTATION_KEY,
    mutationFn: async ({ zone, form, box }: SaveZoneInput) => {
      await assertNotChanged(queryClient, zone);
      return updateZone(api, zone.zoneId, bodyOf(zone, form, box));
    },
    onSuccess: applyWritten,
    onError: refetchAfter,
  });
  const create = useMutation({
    mutationKey: CREATE_MUTATION_KEY,
    mutationFn: (body: ZoneBody) => createZone(api, body),
    onSuccess: applyWritten,
    onError: refetchAfter,
  });
  // 구역 보관(DELETE). 응답이 오면 목록에서 뺀다(낙관적 갱신 아님). 이미 보관한 구역도 서버는 204 다.
  const archive = useMutation({
    mutationKey: ARCHIVE_MUTATION_KEY,
    mutationFn: async (zoneId: string) => {
      await deleteZone(api, zoneId);
      return zoneId;
    },
    onSuccess: (zoneId) => removeZone(queryClient, zoneId),
    onError: refetchAfter,
  });
  return { save, create, archive };
}
