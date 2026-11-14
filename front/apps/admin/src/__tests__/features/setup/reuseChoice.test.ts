import type { ReuseCheck } from '@/features/setup/types';
import { changedCamerasLabel, reuseChoiceOf } from '@/features/setup/useReuseScene';
import { describe, expect, it } from 'vitest';

// 1단계 「이전 맵 재사용」 판정 → 화면 문구(S15P31A101-689)

const CAMS = ['cam_A', 'cam_B', 'cam_C', 'cam_D'];
const check = (overrides: Partial<ReuseCheck>): ReuseCheck => ({
  available: true,
  blocked_reason: null,
  scene_version: 'scene-1',
  camera_status: 'NOT_DETECTED',
  changed_cameras: [],
  camera_source: null,
  ...overrides,
});

describe('reuseChoiceOf', () => {
  it('고를 수 있으면 막지 않고, 카메라 변화를 모를 때만 시작 뒤 확인 한 줄을 붙인다', () => {
    expect(reuseChoiceOf(check({}), CAMS)).toEqual({ blocked: null, note: null });
    expect(reuseChoiceOf(check({ camera_status: 'UNKNOWN' }), CAMS)).toEqual({
      blocked: null,
      note: '카메라가 그대로인지는 시작한 뒤 확인해요',
    });
  });

  it('이전 맵 없음·World 어긋남·카메라 변화는 무엇이/어떻게 두 줄로 막는다', () => {
    expect(
      reuseChoiceOf(check({ available: false, blocked_reason: 'NO_SCENE' }), CAMS).blocked,
    ).toEqual({ what: '이전 맵이 없어요', how: '새 맵 만들기를 골라 주세요' });
    expect(
      reuseChoiceOf(check({ available: false, blocked_reason: 'SCENE_MISMATCH' }), CAMS).blocked
        ?.what,
    ).toBe('이전 맵이 지금 카메라 보정과 맞지 않아요');
    expect(
      reuseChoiceOf(
        check({
          available: false,
          blocked_reason: 'CAMERA_CHANGED',
          camera_status: 'CHANGED',
          changed_cameras: ['cam_B', 'cam_D'],
        }),
        CAMS,
      ).blocked?.what,
    ).toBe('카메라 2·카메라 4의 위치나 방향이 직전 세팅과 달라졌어요');
    expect(
      reuseChoiceOf(
        check({ available: false, blocked_reason: 'CAMERA_CHANGED', camera_status: 'CHANGED' }),
        CAMS,
      ).blocked?.what,
    ).toBe('카메라 배치가 직전 세팅과 달라졌어요');
    // 모르는 사유는 확인하지 못한 것으로 막는다(서버도 시작을 거절한다)
    expect(
      reuseChoiceOf(check({ available: false, blocked_reason: 'SOMETHING' }), CAMS).blocked?.what,
    ).toBe('이전 맵을 확인하지 못했어요');
  });

  it('현장 목록에 없는 카메라 ID 는 번호를 지어내지 않고 원문 그대로 둔다', () => {
    expect(changedCamerasLabel(CAMS, ['cam_C', 'cam_X'])).toBe('카메라 3·cam_X');
  });
});
