import { STATIC_OBJECTS_KEY } from '@/features/surfaces/api';
import type { ViewerWorkSurfaces } from '@/features/surfaces/types';
import type { ViewerBoxEdit, ViewerBoxGhost, ViewerEditBox } from '@/features/viewer3d';
import type { Zone } from '@/features/zones/types';
import {
  sameBoxEdit,
  sameGhosts,
  withSurfaceBox,
  withZoneBox,
  type SurfaceDraft,
} from '@/features/zones/zoneEditModel';
import { useBoxEditStore } from '@/store/boxEdit.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { updateZoneDraft } from '@/store/zoneEdit.store';
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

/**
 * 뷰어가 올린 상자(끄는 동안 0.05 m 단위)를 초안에 넣는 처리기. 작업면 조절이면 작업면 형상으로, 아니면 구역 상자로 넣는다.
 * 처리기 모양은 하나로 두고 고른 대상은 ref 로 읽는다(끄는 동안 3D 가 낡은 처리기를 부르지 않게).
 */
export function useBoxChange(target: BoxTarget) {
  const latest = useRef(target);
  useEffect(() => {
    latest.current = target;
  });
  return useCallback((box: ViewerEditBox) => {
    const { key, zone, surface, surfMode, zoneBox } = latest.current;
    if (!surfMode) {
      updateZoneDraft(key, (prev) => withZoneBox(prev, zone, surface, zoneBox, box));
      return;
    }
    if (!surface) return;
    updateZoneDraft(key, (prev) => withSurfaceBox(prev, surface, box));
  }, []);
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
