import {
  createStaticObject,
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
import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query';

const fieldOf = (item: unknown, key: string): unknown =>
  typeof item === 'object' && item !== null ? Reflect.get(item, key) : undefined;
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
export function putStaticObject(queryClient: QueryClient, item: StaticObjectItem) {
  queryClient.setQueryData<StaticObjectsResponse>(STATIC_OBJECTS_KEY, (prev) => {
    if (!prev) return prev;
    const found = prev.some((entry) => idOf(entry) === item.object_id);
    return found
      ? prev.map((entry) => (idOf(entry) === item.object_id ? item : entry))
      : [...prev, item];
  });
}

/**
 * 저장 직전 확인에서 멈춘 저장(서버에 보내지 않았다). CHANGED 는 그 사이 다른 관리자가 새 버전을 저장한 것이고(받은 목록은
 * 이미 화면에 넣었다), UNCHECKED 는 지금 버전을 확인하지 못한 것이다.
 */
export class SurfaceSaveBlockedError extends Error {
  readonly reason: 'CHANGED' | 'UNCHECKED';

  constructor(reason: 'CHANGED' | 'UNCHECKED', options?: { cause?: unknown }) {
    super(`작업면 저장을 보내지 않았다 (${reason})`, options);
    this.name = 'SurfaceSaveBlockedError';
    this.reason = reason;
  }
}

/** 응답을 받지 못했거나 읽지 못해 서버에 저장됐는지 모르는 실패. */
const outcomeUnknown = (error: unknown) => error instanceof ApiError && error.kind !== 'HTTP';

/** 목록을 다시 받아야 하는 실패: 다른 요청이 먼저 새 버전을 만듦(409), 없어짐(404), 저장됐는지 모름, 서버 오류. */
const needsRefetch = (error: unknown) =>
  error instanceof ApiError &&
  (error.status === 409 || error.status === 404 || error.status >= 500 || outcomeUnknown(error));

const CHANGED_TEXT =
  '그 사이 다른 버전이 저장되어 최신 작업면을 다시 받았습니다. 확인한 뒤 다시 저장하세요.';

export function surfaceSaveErrorText(error: unknown): string {
  if (error instanceof SurfaceSaveBlockedError)
    return error.reason === 'CHANGED'
      ? CHANGED_TEXT
      : '최신 작업면을 확인하지 못해 저장하지 않았습니다. 다시 시도하세요.';
  if (error instanceof ApiError && error.status === 409) return CHANGED_TEXT;
  if (error instanceof ApiError && error.status === 403) return '작업면을 저장할 권한이 없습니다.';
  if (error instanceof ApiError && error.status === 404)
    return '작업면을 찾지 못해 최신 목록을 다시 받았습니다.';
  if (error instanceof ApiError && error.code === 'INVALID_STATIC_OBJECT')
    return '작업면 값이 올바르지 않아 저장하지 못했습니다.';
  if (outcomeUnknown(error))
    return '저장됐는지 확인하지 못해 최신 작업면을 다시 받았습니다. 확인한 뒤 다시 시도하세요.';
  return '저장하지 못했습니다. 다시 시도하세요.';
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
    mutationKey: ['static-objects', 'save'],
    mutationFn: async ({ surface, body }: SaveSurfaceInput) => {
      // 서버는 고치기 시작한 버전을 받지 않고 기록을 통째로 바꾼다. 그 사이 다른 관리자가 새 버전을 저장했으면 그 변경을
      // 덮지 않도록 보내기 전에 지금 버전을 다시 받아 본다(받은 목록은 화면에 들어간다). 같은 순간 부딪친 저장은 서버가 409 로 막는다.
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
      return updateStaticObject(api, surface.objectId, body);
    },
    onSuccess: applyWritten,
    onError: refetchAfter,
  });
  const create = useMutation({
    mutationKey: ['static-objects', 'create'],
    mutationFn: (body: StaticObjectBody) => createStaticObject(api, body),
    onSuccess: applyWritten,
    onError: refetchAfter,
  });
  return { save, create };
}
