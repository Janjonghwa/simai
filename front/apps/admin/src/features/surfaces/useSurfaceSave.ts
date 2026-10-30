import {
  createStaticObject,
  deleteStaticObject,
  requestStaticObjects,
  STATIC_OBJECTS_KEY,
  updateStaticObject,
} from '@/features/surfaces/api';
import type {
  StaticObjectBody,
  StaticObjectItem,
  StaticObjectsResponse,
  WorkSurface,
} from '@/features/surfaces/types';
import { api } from '@/lib/api';
import { ApiError } from '@/lib/http';
import {
  fieldOf,
  HTTP_CONFLICT,
  HTTP_FORBIDDEN,
  HTTP_NOT_FOUND,
  needsRefetch,
  outcomeUnknown,
} from '@/lib/writeFailure';
import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query';

/** 저장·만들기·보관 mutation 키. */
const SAVE_MUTATION_KEY = [...STATIC_OBJECTS_KEY, 'save'] as const;
const CREATE_MUTATION_KEY = [...STATIC_OBJECTS_KEY, 'create'] as const;
const ARCHIVE_MUTATION_KEY = [...STATIC_OBJECTS_KEY, 'archive'] as const;

const idOf = (item: unknown) => fieldOf(item, 'object_id');

/** 받은 목록에서 그 작업면의 지금 버전. 없거나 읽을 수 없으면 undefined 다. */
function versionIn(list: StaticObjectsResponse, objectId: string): number | undefined {
  const version = fieldOf(
    list.find((item) => idOf(item) === objectId),
    'object_version',
  );
  return typeof version === 'number' ? version : undefined;
}

/** 저장 응답(새 버전)을 목록에 넣는다. 같은 ID 는 바꾸고, 없으면 끝에 더한다. */
function putStaticObject(queryClient: QueryClient, item: StaticObjectItem) {
  queryClient.setQueryData<StaticObjectsResponse>(STATIC_OBJECTS_KEY, (prev) => {
    if (!prev) return prev;
    const found = prev.some((entry) => idOf(entry) === item.object_id);
    return found
      ? prev.map((entry) => (idOf(entry) === item.object_id ? item : entry))
      : [...prev, item];
  });
}

/** 보관(삭제)한 작업면을 목록에서 뺀다. */
function removeStaticObject(queryClient: QueryClient, objectId: string) {
  queryClient.setQueryData<StaticObjectsResponse>(STATIC_OBJECTS_KEY, (prev) =>
    prev ? prev.filter((entry) => idOf(entry) !== objectId) : prev,
  );
}

type SaveBlockedReason = 'CHANGED' | 'UNCHECKED';

/**
 * 저장 직전 확인에서 멈춘 저장(서버에 보내지 않았다). CHANGED 는 그 사이 다른 관리자가 새 버전을 저장한 것이고(받은 목록은
 * 이미 화면에 넣었다), UNCHECKED 는 지금 버전을 확인하지 못한 것이다.
 */
class SurfaceSaveBlockedError extends Error {
  readonly reason: SaveBlockedReason;

  constructor(reason: SaveBlockedReason, options?: { cause?: unknown }) {
    super(`작업면 저장을 보내지 않았다 (${reason})`, options);
    this.name = 'SurfaceSaveBlockedError';
    this.reason = reason;
  }
}

/** 서버가 정한 오류 이름(ProblemDetail code): 작업면 값이 계약 범위 밖. */
const INVALID_STATIC_OBJECT_CODE = 'INVALID_STATIC_OBJECT';

const CHANGED_TEXT =
  '그 사이 다른 버전이 저장되어 최신 작업면을 다시 받았습니다. 확인한 뒤 다시 저장하세요.';
const OUTCOME_UNKNOWN_TEXT =
  '저장됐는지 확인하지 못해 최신 작업면을 다시 받았습니다. 확인한 뒤 다시 시도하세요.';
const FALLBACK_TEXT = '저장하지 못했습니다. 다시 시도하세요.';

/** 저장 직전 확인에서 멈춘 이유별 안내. */
const BLOCKED_TEXT: Record<SaveBlockedReason, string> = {
  CHANGED: CHANGED_TEXT,
  UNCHECKED: '최신 작업면을 확인하지 못해 저장하지 않았습니다. 다시 시도하세요.',
};

/** 서버가 오류 상태로 답했을 때의 안내(상태별). */
const STATUS_TEXT = new Map<number, string>([
  [HTTP_CONFLICT, CHANGED_TEXT],
  [HTTP_FORBIDDEN, '작업면을 저장할 권한이 없습니다.'],
  [HTTP_NOT_FOUND, '작업면을 찾지 못해 최신 목록을 다시 받았습니다.'],
]);

/** 서버가 오류 상태로 답했을 때의 안내(오류 이름별). 상태 안내가 없을 때만 쓴다. */
const CODE_TEXT = new Map<string, string>([
  [INVALID_STATIC_OBJECT_CODE, '작업면 값이 올바르지 않아 저장하지 못했습니다.'],
]);

export function surfaceSaveErrorText(error: unknown): string {
  if (error instanceof SurfaceSaveBlockedError) return BLOCKED_TEXT[error.reason];
  if (!(error instanceof ApiError)) return FALLBACK_TEXT;
  const known = STATUS_TEXT.get(error.status) ?? CODE_TEXT.get(error.code ?? '');
  if (known !== undefined) return known;
  return outcomeUnknown(error) ? OUTCOME_UNKNOWN_TEXT : FALLBACK_TEXT;
}

/**
 * 서버는 고치기 시작한 버전을 받지 않고 기록을 통째로 바꾼다. 그 사이 다른 관리자가 새 버전을 저장했으면 그 변경을
 * 덮지 않도록 보내기 전에 지금 버전을 다시 받아 보고 멈춘다(받은 목록은 화면에 들어간다). 같은 순간 부딪친 저장은 서버가 409 로 막는다.
 */
async function assertNotChanged(queryClient: QueryClient, surface: WorkSurface) {
  const latest = await queryClient
    .fetchQuery({
      queryKey: STATIC_OBJECTS_KEY,
      queryFn: ({ signal }) => requestStaticObjects(api, signal),
      staleTime: 0,
    })
    .catch((cause: unknown) => {
      throw new SurfaceSaveBlockedError('UNCHECKED', { cause });
    });
  const current = versionIn(latest, surface.objectId);
  if (current !== undefined && current !== surface.version) {
    throw new SurfaceSaveBlockedError('CHANGED');
  }
}

export interface SaveSurfaceInput {
  surface: WorkSurface;
  body: StaticObjectBody;
}

/**
 * 작업면 새 버전 저장과 새 작업면 만들기. 낙관적 갱신을 하지 않고 응답(새 버전)으로 목록을 바꾼다(CONVENTION).
 * 저장은 현장에 바로 가지 않는다. 구성 묶음을 발행해야 Tier 0 가 받는다(명세 3.2).
 */
export function useSurfaceSave() {
  const queryClient = useQueryClient();
  // 진행 중인 목록 다시 받기는 이 버전보다 옛 목록을 줄 수 있어 취소하고 응답을 넣은 뒤 다시 받는다.
  const applyWritten = async (item: StaticObjectItem) => {
    const refetching = queryClient.isFetching({ queryKey: STATIC_OBJECTS_KEY }) > 0;
    await queryClient.cancelQueries({ queryKey: STATIC_OBJECTS_KEY });
    putStaticObject(queryClient, item);
    if (refetching) void queryClient.invalidateQueries({ queryKey: STATIC_OBJECTS_KEY });
  };
  const refetchAfter = (error: unknown) => {
    if (needsRefetch(error)) void queryClient.invalidateQueries({ queryKey: STATIC_OBJECTS_KEY });
  };
  const save = useMutation({
    mutationKey: SAVE_MUTATION_KEY,
    mutationFn: async ({ surface, body }: SaveSurfaceInput) => {
      await assertNotChanged(queryClient, surface);
      return updateStaticObject(api, surface.objectId, body);
    },
    onSuccess: applyWritten,
    onError: refetchAfter,
  });
  const create = useMutation({
    mutationKey: CREATE_MUTATION_KEY,
    mutationFn: (body: StaticObjectBody) => createStaticObject(api, body),
    onSuccess: applyWritten,
    onError: refetchAfter,
  });
  // 작업면 보관(DELETE). 응답이 오면 목록에서 뺀다(낙관적 갱신 아님).
  const archive = useMutation({
    mutationKey: ARCHIVE_MUTATION_KEY,
    mutationFn: async (objectId: string) => {
      await deleteStaticObject(api, objectId);
      return objectId;
    },
    onSuccess: (objectId) => removeStaticObject(queryClient, objectId),
    onError: refetchAfter,
  });
  return { save, create, archive };
}
