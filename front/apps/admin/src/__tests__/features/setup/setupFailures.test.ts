import { failureGuide, SETUP_FAILURES } from '@/features/setup/setupFailures';
import { describe, expect, it } from 'vitest';

const LETTERS = ['cam_A', 'cam_B', 'cam_C', 'cam_D'];
const LEGACY_NUMBERS = ['cam1', 'cam2', 'cam3', 'cam4'];

describe('세팅 실패 안내표(SETUP_FAILURES)', () => {
  it('같은 상태로 다시 시도해 볼 만한 실패만 다시 시도를 허용한다', () => {
    const retry = Object.fromEntries(Object.entries(SETUP_FAILURES).map(([k, v]) => [k, v.retry]));
    expect(retry).toEqual({
      BACKGROUND_INCOMPLETE: true,
      FLOOR_FIT_FAILED: false,
      INSUFFICIENT_FEATURES: false,
      VALIDATION_REJECTED: false,
      TIMEOUT: true,
      CANCELLED_BY_ADMIN: true,
      LEASE_EXPIRED: true,
    });
  });

  it.each([
    [
      'BACKGROUND_INCOMPLETE',
      '사람 없는 배경을 다 모으지 못했어요',
      '카메라 앞에서 사람과 장비가 비킨 상태로 다시 시도하세요.',
    ],
    [
      'FLOOR_FIT_FAILED',
      '바닥을 찾지 못했어요',
      '카메라 연결 관리에서 카메라 높이를 확인하고, 바닥이 영상에 넓게 보이게 맞춘 뒤 다시 시작하세요.',
    ],
    [
      'TIMEOUT',
      '시간이 너무 오래 걸렸어요 (15분)',
      '잠시 뒤 다시 시도하세요. 계속되면 관리자에게 알려 주세요.',
    ],
    ['CANCELLED_BY_ADMIN', '세팅을 취소했어요', '다시 시작하려면 다시 시도하세요.'],
    [
      'LEASE_EXPIRED',
      '분석 서버와 연결이 끊겼어요',
      '분석 서버가 켜져 있는지 확인한 뒤 다시 시도하세요.',
    ],
  ])('%s 는 원인과 해결을 준다', (code, title, fix) => {
    const guide = failureGuide(code, null, LETTERS);
    expect(guide.title).toBe(title);
    expect(guide.fix).toBe(fix);
    expect(guide.retry).toBe(SETUP_FAILURES[code]!.retry);
  });

  it('표에 없는 코드와 코드가 없는 실패는 폴백(다시 시도 가능)이다', () => {
    for (const code of ['RECONSTRUCT_FAILED', 'constructor', null]) {
      expect(failureGuide(code, 'whatever', LETTERS)).toEqual({
        title: '서버에서 세팅을 끝내지 못했어요',
        fix: '잠시 뒤 다시 시도하세요. 계속되면 관리자에게 알려 주세요.',
        retry: true,
      });
    }
  });
});

describe('실패 메시지에서 카메라 쌍 뽑기', () => {
  it('INSUFFICIENT_FEATURES: 메시지의 카메라 쌍을 캠N–캠M 으로 보인다', () => {
    const guide = failureGuide(
      'INSUFFICIENT_FEATURES',
      'Insufficient raw feature matches on pair cam_B-cam_C: 7',
      LETTERS,
    );
    expect(guide.title).toBe('옆 카메라 카메라 2–카메라 3 사이에 같이 보이는 곳이 적어요');
    expect(guide.fix).toBe(
      '카메라 연결 관리로 돌아가 두 카메라를 서로 쪽으로 돌리거나, 겹치는 곳에 무늬 있는 물건을 두세요.',
    );
    expect(guide.retry).toBe(false);
  });

  it('VALIDATION_REJECTED: worsened_pairs 의 쌍을 모두 보이고 그 밖의 카메라 쌍 글자는 보지 않는다', () => {
    const guide = failureGuide(
      'VALIDATION_REJECTED',
      "pair cam_A-cam_B was ok; worsened_pairs=['cam_C-cam_D', 'cam_A-cam_B']",
      LETTERS,
    );
    expect(guide.title).toBe(
      '보정한 뒤 오히려 어긋난 카메라 쌍이 있어요 (카메라 3–카메라 4, 카메라 1–카메라 2)',
    );
    expect(guide.fix).toBe(
      '카메라와 거치대를 단단히 고정하고 위치를 다시 맞춘 뒤 처음부터 다시 하세요.',
    );
    expect(guide.retry).toBe(false);
  });

  it('레거시 숫자형 카메라 id(cam1-cam2 꼴)도 같은 방식으로 읽는다', () => {
    expect(
      failureGuide(
        'VALIDATION_REJECTED',
        "worsened_pairs=['cam1-cam2', 'cam3-cam4']",
        LEGACY_NUMBERS,
      ).title,
    ).toBe('보정한 뒤 오히려 어긋난 카메라 쌍이 있어요 (카메라 1–카메라 2, 카메라 3–카메라 4)');
    expect(
      failureGuide('INSUFFICIENT_FEATURES', 'matches on pair cam4-cam1: 3', LEGACY_NUMBERS).title,
    ).toBe('옆 카메라 카메라 4–카메라 1 사이에 같이 보이는 곳이 적어요');
  });

  it('카메라 id 에 -가 있어도 알려진 id 조합을 찾는다', () => {
    const ids = ['cam-1', 'cam-2', 'cam-3'];
    expect(failureGuide('INSUFFICIENT_FEATURES', 'matches on pair cam-2-cam-3: 7', ids).title).toBe(
      '옆 카메라 카메라 2–카메라 3 사이에 같이 보이는 곳이 적어요',
    );
  });

  it('id 가 다른 id 의 끝부분이어도 다른 카메라 쌍으로 읽지 않는다', () => {
    const ids = ['am1', 'cam1', 'cam2'];
    expect(failureGuide('INSUFFICIENT_FEATURES', 'matches on pair cam1-cam2: 7', ids).title).toBe(
      '옆 카메라 카메라 2–카메라 3 사이에 같이 보이는 곳이 적어요',
    );
  });

  it('카메라 id 목록에 없는 쌍이나 쌍이 없는 메시지는 쌍 없는 문구를 쓴다', () => {
    expect(
      failureGuide('INSUFFICIENT_FEATURES', 'matches on pair cam_X-cam_Y: 7', LETTERS).title,
    ).toBe('옆 카메라끼리 같이 보이는 곳이 적어요');
    expect(failureGuide('INSUFFICIENT_FEATURES', null, LETTERS).title).toBe(
      '옆 카메라끼리 같이 보이는 곳이 적어요',
    );
    expect(failureGuide('VALIDATION_REJECTED', 'rejected', LETTERS).title).toBe(
      '보정 결과가 검증을 통과하지 못했어요',
    );
    expect(failureGuide('VALIDATION_REJECTED', 'worsened_pairs=[]', LETTERS).title).toBe(
      '보정 결과가 검증을 통과하지 못했어요',
    );
  });

  it('쌍이 필요 없는 실패는 메시지에 쌍이 있어도 문구가 같다', () => {
    expect(failureGuide('TIMEOUT', 'stuck on pair cam_A-cam_B', LETTERS).title).toBe(
      '시간이 너무 오래 걸렸어요 (15분)',
    );
  });
});
