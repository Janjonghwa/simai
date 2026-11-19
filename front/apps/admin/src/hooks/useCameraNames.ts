import { useCctvStore } from '@/features/cctv/cctv.store';
import { cameraLabel, cameraOrder, useCameraOrderStore } from '@/lib/cameraNames';
import { useRealtimeStore } from '@/store/realtime.store';

/** 목록 전체로 번호를 매긴다. 선택·장애·부분 클립 목록으로 카메라 번호를 다시 매기지 않는다. */
export function useCameraNames(fallbackIds: readonly string[] = []) {
  const ids = useCameraIds(fallbackIds);
  return (id: string) => cameraLabel(ids, id);
}

/** 번호를 매기는 카메라 순서(첫째가 「카메라 1」). 현장 배치 목록 → 중계 목록 → health → 보정 목록 순으로 기준을 고른다. */
export function useCameraIds(fallbackIds: readonly string[] = []): string[] {
  const configured = useCameraOrderStore((s) => s.ids);
  const calibration = useCameraOrderStore((s) => s.calibrationIds);
  const relay = useCctvStore((s) => s.cameras);
  // 연결이 끊겨도 마지막 목록은 번호 근거로 쓴다. 현재 상태 판정에는 쓰지 않는다.
  const health = useRealtimeStore((s) => s.health);
  const tier0 = health?.payload.tier0_health;
  const raw: unknown =
    tier0 && typeof tier0 === 'object' ? Reflect.get(tier0, 'cameras') : undefined;
  const healthIds = Array.isArray(raw)
    ? raw.flatMap((c: unknown) => {
        if (typeof c !== 'object' || c === null) return [];
        const id: unknown = Reflect.get(c, 'id');
        return typeof id === 'string' ? [id] : [];
      })
    : [];
  const base = configured.length
    ? configured
    : relay.length
      ? relay
      : healthIds.length
        ? healthIds
        : calibration;
  return cameraOrder(base, fallbackIds);
}
