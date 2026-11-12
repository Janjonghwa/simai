import { cameraShortLabel } from '@/lib/cameraNames';
export { cameraLabel, cameraNumber, cameraShortLabel } from '@/lib/cameraNames';

/** '카메라 1–카메라 2'. */
export function pairLabel(cameraIds: readonly string[], a: string, b: string): string {
  return `${cameraShortLabel(cameraIds, a)}–${cameraShortLabel(cameraIds, b)}`;
}

/**
 * 보정이 쓰는 옆 카메라 쌍인지(목록 순서가 모서리를 도는 순서라 1–2, 2–3, 3–4, 4–1).
 * 마주 보는 쌍(1–3, 2–4)과 목록에 없는 id 는 아니다.
 */
export function isNeighborPair(cameraIds: readonly string[], a: string, b: string): boolean {
  const i = cameraIds.indexOf(a);
  const j = cameraIds.indexOf(b);
  if (i < 0 || j < 0 || i === j) return false;
  const gap = Math.abs(i - j);
  return gap === 1 || gap === cameraIds.length - 1;
}
