import {
  cameraLabel,
  cameraNumber,
  cameraShortLabel,
  isNeighborPair,
  pairLabel,
} from '@/features/setup/cameraLabels';
import { describe, expect, it } from 'vitest';

const DB_IDS = ['cam1', 'cam2', 'cam3', 'cam4'];
const AI_IDS = ['cam_A', 'cam_B', 'cam_C', 'cam_D'];

describe('세팅 화면 카메라 이름', () => {
  it('id 종류와 상관없이 목록 순서로 이름을 만든다', () => {
    expect(cameraLabel(DB_IDS, 'cam3')).toBe('카메라 3');
    expect(cameraLabel(AI_IDS, 'cam_C')).toBe('카메라 3');
    expect(cameraShortLabel(AI_IDS, 'cam_D')).toBe('카메라 4');
    expect(pairLabel(AI_IDS, 'cam_B', 'cam_C')).toBe('카메라 2–카메라 3');
  });

  it('목록에 없는 id 는 번호 없이 표시하고 번호는 null 이다', () => {
    expect(cameraNumber(DB_IDS, 'cam9')).toBeNull();
    expect(cameraLabel(DB_IDS, 'cam9')).toBe('카메라');
    expect(cameraShortLabel(DB_IDS, 'cam9')).toBe('카메라');
    expect(pairLabel(DB_IDS, 'cam1', 'cam9')).toBe('카메라 1–카메라');
  });
});

describe('옆 카메라 쌍 판정', () => {
  it('모서리를 도는 순서의 이웃(1–2, 2–3, 3–4, 4–1)은 어느 쪽 순서로 물어도 참이다', () => {
    for (const [a, b] of [
      ['cam1', 'cam2'],
      ['cam2', 'cam3'],
      ['cam3', 'cam4'],
      ['cam4', 'cam1'],
    ] as const) {
      expect(isNeighborPair(DB_IDS, a, b)).toBe(true);
      expect(isNeighborPair(DB_IDS, b, a)).toBe(true);
    }
    expect(isNeighborPair(AI_IDS, 'cam_D', 'cam_A')).toBe(true);
  });

  it('마주 보는 쌍·같은 카메라·목록에 없는 id 는 거짓이다', () => {
    expect(isNeighborPair(DB_IDS, 'cam1', 'cam3')).toBe(false);
    expect(isNeighborPair(DB_IDS, 'cam4', 'cam2')).toBe(false);
    expect(isNeighborPair(DB_IDS, 'cam2', 'cam2')).toBe(false);
    expect(isNeighborPair(DB_IDS, 'cam1', 'cam9')).toBe(false);
  });
});
