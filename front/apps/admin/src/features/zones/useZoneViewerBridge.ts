import { STATIC_OBJECTS_KEY } from '@/features/surfaces/api';
import type { ViewerWorkSurfaces } from '@/features/surfaces/types';
import type { ViewerBoxEdit, ViewerBoxGhost, ViewerEditBox } from '@/features/viewer3d';
import type { Zone } from '@/features/zones/types';
import {
  sameBox,
  sameBoxEdit,
  sameGhosts,
  surfaceBox,
  withSurfaceBox,
  withZoneBox,
  type SurfaceDraft,
} from '@/features/zones/zoneEditModel';
import { useBoxEditStore } from '@/store/boxEdit.store';
import { useRealtimeStore } from '@/store/realtime.store';
import {
  discardZoneRecord,
  lastZoneRecord,
  recordZoneDraft,
  updateZoneDraft,
  type HistoryStep,
} from '@/store/zoneEdit.store';
import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useRef } from 'react';

// 구역 편집 화면과 3D 뷰어 사이의 다리: 작업면을 3D 에 넣고, 3D 손잡이가 올린 상자를 초안에 넣고, 편집 상자를 3D 로 내린다.

/** 구역 편집 화면이 떠 있지 않을 때 3D 에 남기는 작업면(없음). */
const NO_SURFACES = { work_surfaces: [] };

/**
 * 작업면은 이 화면에서 3D 에 넣는다. 3D 가 새로 뜨면 다시 넣고, 화면을 떠나면 지운다. 들어오면 다른 관리자가 저장한
 * 새 버전도 보이게 다시 받는다(이미 받는 중이면 그 응답을 쓴다).
 */
export function useWorkSurfaceFeed(input: {
  active: boolean;
  raw: ViewerWorkSurfaces | undefined;
}) {
  const { active, raw } = input;
  const queryClient = useQueryClient();
  const source = useRealtimeStore((s) => s.source);
  useEffect(() => {
    if (!active || !source || !raw) return;
    source.pushWorkSurfaces({ ok: true, raw });
  }, [active, source, raw]);
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: STATIC_OBJECTS_KEY }, { cancelRefetch: false });
    return () => {
      useRealtimeStore.getState().source?.pushWorkSurfaces({ ok: true, raw: NO_SURFACES });
    };
  }, [queryClient]);
}

/** 고르고 있는 대상. 3D 에서 끌 때마다 이 값으로 초안을 만든다. */
interface BoxTarget {
  key: string;
  zone: Zone | null;
  surface: SurfaceDraft | null;
  surfMode: boolean;
  zoneBox: ViewerEditBox;
}

/** 끌기 하나(포인터를 누르고 있는 동안)의 대상, 시작·마지막 상자, 첫 변경 때 남긴 되돌리기 단계. */
interface Drag {
  key: string;
  start: ViewerEditBox;
  last: ViewerEditBox;
  step: HistoryStep | undefined;
}

/** 끝난 끌기가 남긴 게 없는지: 시작 상자로 돌아왔고, 끌기가 남긴 단계가 아직 마지막이다(끄는 중 다른 고침이 단계를 더하지 않았다). */
const leftNothing = ({ key, start, last, step }: Drag) =>
  sameBox(start, last) && lastZoneRecord(key) === step;

/**
 * 3D 에서 한 번 끄는 동안의 변경을 되돌리기 한 단계로 묶는다. 끌기의 첫 변경에서만 단계를 남기고, 포인터를 놓거나
 * 가로채이면 끝낸다. 끝났을 때 남긴 게 없으면(끄는 중 Esc) 그 단계를 버린다.
 * 돌려주는 함수에 이번 변경의 대상·시작 상자(변경 전)·새 상자를 넘긴다.
 */
function useDragRecord() {
  const drag = useRef<Drag | null>(null);
  useEffect(() => {
    const end = () => {
      const current = drag.current;
      drag.current = null;
      if (current && leftNothing(current)) discardZoneRecord(current.key);
    };
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    // 새 끌기는 반드시 포인터를 누르며 시작한다. 누를 때도 끝내면 놓음을 못 받은 끌기가 다음 끌기에 합쳐지지 않는다(캡처라 3D 처리기보다 먼저).
    window.addEventListener('pointerdown', end, true);
    return () => {
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      window.removeEventListener('pointerdown', end, true);
    };
  }, []);
  return useCallback((key: string, start: ViewerEditBox, box: ViewerEditBox) => {
    if (drag.current) {
      drag.current.last = box;
      return;
    }
    recordZoneDraft(key);
    drag.current = { key, start, last: box, step: lastZoneRecord(key) };
  }, []);
}

/**
 * 뷰어가 올린 상자(끄는 동안 0.05 m 단위)를 초안에 넣는 처리기. 작업면 조절이면 작업면 형상으로, 아니면 구역 상자로 넣는다.
 * 처리기 모양은 하나로 두고 고른 대상은 ref 로 읽는다(끄는 동안 3D 가 낡은 처리기를 부르지 않게).
 */
export function useBoxChange(target: BoxTarget) {
  const latest = useRef(target);
  useEffect(() => {
    latest.current = target;
  });
  const trackDrag = useDragRecord();
  return useCallback(
    (box: ViewerEditBox) => {
      const { key, zone, surface, surfMode, zoneBox } = latest.current;
      if (!surfMode) {
        trackDrag(key, zoneBox, box);
        updateZoneDraft(key, (prev) => withZoneBox(prev, zone, surface, zoneBox, box));
        return;
      }
      if (!surface) return;
      trackDrag(key, surfaceBox(surface.geometry), box);
      updateZoneDraft(key, (prev) => withSurfaceBox(prev, surface, box));
    },
    [trackDrag],
  );
}

/**
 * 편집 상자·윤곽·처리기를 boxEdit 저장소로 내린다(SpatialViewportHost 가 뷰어에 넘긴다). 값이 달라졌을 때만 저장소를
 * 바꾼다(새 객체가 와도 3D 를 다시 그리지 않는다). 화면이 내려가면 비운다.
 */
export function useBoxEditBridge(input: {
  boxEdit: ViewerBoxEdit | null;
  ghosts: readonly ViewerBoxGhost[];
  onChange: (box: ViewerEditBox) => void;
}) {
  const { boxEdit, ghosts, onChange } = input;
  useEffect(() => {
    const store = useBoxEditStore.getState();
    const next: Parameters<typeof store.set>[0] = {};
    if (!sameBoxEdit(store.boxEdit, boxEdit)) next.boxEdit = boxEdit;
    if (!sameGhosts(store.ghosts, ghosts)) next.ghosts = ghosts;
    if (store.onChange !== onChange) next.onChange = onChange;
    if (Object.keys(next).length > 0) store.set(next);
  }, [boxEdit, ghosts, onChange]);
  useEffect(() => () => useBoxEditStore.getState().clear(), []);
}
