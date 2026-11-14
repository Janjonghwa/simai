import { requestReuseCheck, SETUP_REUSE_CHECK_KEY } from '@/features/setup/api';
import type { ReuseCheck } from '@/features/setup/types';
import { api } from '@/lib/api';
import { cameraLabel } from '@/lib/cameraNames';
import { useQuery } from '@tanstack/react-query';

// 1단계 「이전 맵 재사용」을 고를 수 있는지(S15P31A101-689). 서버(GET /api/setup-sessions/reuse-check)가 세팅 시작과 같은 판단으로
// 이전 맵(현재 관제 맵) 유무·World 정합·직전 보정 대비 카메라 변화를 본다. 카메라가 달라졌으면 재사용을 끄고 이유를 두 줄(무엇이/어떻게)로
// 보인다. 카메라 변화를 알 근거가 없으면 막지 않고 「시작한 뒤 확인」 한 줄만 붙인다(워커가 마지막으로 확인하고, 거부되면 새 맵으로
// 넘어가지 않고 멈춘다).

/** 1단계에 머무는 동안 다시 받는 주기. 카메라가 그 사이 움직이면(관제 회전 감시) 선택이 바뀐다. */
const REUSE_CHECK_POLL_MS = 10_000;

const NEW_MAP_HOW = '새 맵 만들기를 골라 주세요';

/** 무엇이 / 어떻게 두 줄. */
export interface ReuseBlocked {
  what: string;
  how: string;
}

export interface ReuseChoiceState {
  /** 첫 응답을 기다리는 중이다(그동안은 고를 수 없다). */
  checking: boolean;
  /** 재사용을 고를 수 없는 이유. 고를 수 있으면 null. */
  blocked: ReuseBlocked | null;
  /** 고를 수 있을 때 덧붙이는 한 줄(카메라 변화를 아직 모름). 없으면 null. */
  note: string | null;
  /** 직전 보정과 달라진 카메라(카메라 배지 「직전 보정과 달라짐」 근거, S15P31A101-691). 모르면 빈 배열. */
  changedCameras: readonly string[];
}

const NONE: readonly string[] = [];

const CHECK_FAILED: ReuseBlocked = {
  what: '이전 맵을 확인하지 못했어요',
  how: '잠시 뒤 다시 열거나 새 맵 만들기를 골라 주세요',
};

/** 달라진 카메라 이름(카메라 2·카메라 3). 현장 카메라 목록에 없는 ID 는 원문 그대로 둔다(번호를 지어내지 않는다). */
export function changedCamerasLabel(
  cameraIds: readonly string[],
  changed: readonly string[],
): string {
  return changed.map((id) => (cameraIds.includes(id) ? cameraLabel(cameraIds, id) : id)).join('·');
}

/** 서버 판정을 화면 문구로 바꾼다. */
export function reuseChoiceOf(
  check: ReuseCheck,
  cameraIds: readonly string[],
): Omit<ReuseChoiceState, 'checking' | 'changedCameras'> {
  if (check.available) {
    return {
      blocked: null,
      note: check.camera_status === 'UNKNOWN' ? '카메라가 그대로인지는 시작한 뒤 확인해요' : null,
    };
  }
  switch (check.blocked_reason) {
    case 'NO_SCENE':
      return { blocked: { what: '이전 맵이 없어요', how: NEW_MAP_HOW }, note: null };
    case 'SCENE_MISMATCH':
      return {
        blocked: { what: '이전 맵이 지금 카메라 보정과 맞지 않아요', how: NEW_MAP_HOW },
        note: null,
      };
    case 'CAMERA_CHANGED': {
      const names = changedCamerasLabel(cameraIds, check.changed_cameras);
      if (check.camera_source === 'VIEW_MATCH')
        // 1단계 헬퍼가 이전 맵을 만들 때의 카메라 사진과 지금 화면을 비교했다(S15P31A101-712)
        return {
          blocked: {
            what: names ? `카메라가 바뀐 것 같아요(${names})` : '카메라가 바뀐 것 같아요',
            how: `이전 맵을 만들 때와 화면이 달라요 · ${NEW_MAP_HOW}`,
          },
          note: null,
        };
      return {
        blocked: {
          what: names
            ? `${names}의 위치나 방향이 직전 세팅과 달라졌어요`
            : '카메라 배치가 직전 세팅과 달라졌어요',
          how: NEW_MAP_HOW,
        },
        note: null,
      };
    }
    default:
      return { blocked: CHECK_FAILED, note: null };
  }
}

/** 직전 보정과 달라진 카메라: 서버가 카메라 변화(CHANGED)라고 판정했을 때만(-540 관제 회전 감시·1단계 시점 확인(-712)·직전 재사용 거부). */
export function changedCamerasOf(check: ReuseCheck): readonly string[] {
  return check.camera_status === 'CHANGED' || check.blocked_reason === 'CAMERA_CHANGED'
    ? check.changed_cameras
    : NONE;
}

/** 켜져 있는 동안(1단계) 재사용 판정을 받아 둔다. */
export function useReuseChoice(enabled: boolean, cameraIds: readonly string[]): ReuseChoiceState {
  const query = useQuery({
    queryKey: SETUP_REUSE_CHECK_KEY,
    queryFn: ({ signal }) => requestReuseCheck(api, signal),
    enabled,
    staleTime: 0,
    refetchInterval: enabled ? REUSE_CHECK_POLL_MS : false,
  });
  if (query.data)
    return {
      checking: false,
      ...reuseChoiceOf(query.data, cameraIds),
      changedCameras: changedCamerasOf(query.data),
    };
  if (query.isError)
    return { checking: false, blocked: CHECK_FAILED, note: null, changedCameras: NONE };
  return { checking: enabled, blocked: null, note: null, changedCameras: NONE };
}
