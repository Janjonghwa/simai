import type { SpatialDataStore } from '@/features/viewer3d/data/spatialData.store';
import {
  applyEditOperation,
  boxJson,
  isValidBox,
  sameBox,
  ZONE_AABB_POLICY,
  type EditFace,
  type EditOperation,
  type EditOutcome,
  type EditPolicy,
  type WorldBox,
} from '@/features/viewer3d/editing/editGeometry';
import type {
  ViewerEditCancelReason,
  ViewerEditCommandResult,
  ViewerEditPreview,
  ViewerEditProvenance,
  ViewerEditRejectReason,
  ViewerEditResult,
} from '@/features/viewer3d/editing/editTypes';
import { parseTarget, sameTarget, targetKey } from '@/features/viewer3d/target';
import type { ViewerMode, ViewerTarget } from '@/features/viewer3d/types';
import type { ViewerStore } from '@/features/viewer3d/viewer3d.store';
import { createStore, type StoreApi } from 'zustand/vanilla';

// 편집 세션 하나의 수명: 시작(원본 사본·출처 기록) → 미리보기(초안만 바꿈) → 취소(초안 폐기) 또는 완료(World 결과 반환).
// 원본 ZoneSnapshot·선택·위험 이벤트·적용 상태는 읽기만 한다. 저장·ACK 요청은 하지 않는다.
// 원본 구역이 바뀌거나 사라지고, 입력 Source·Scene 버전이 바뀌면 초안을 버리고 STALE_SOURCE 로 끝낸다.

/** 마우스로 잡는 손잡이. move 는 X·Y 이동, 나머지는 그 면의 경계다. */
export type EditHandle = 'move' | EditFace;

export interface EditSession {
  target: ViewerTarget;
  mode: ViewerMode;
  policy: EditPolicy;
  /** 시작할 때의 원본 사본. 세션 동안 바뀌지 않는다. */
  base: WorldBox;
  draft: WorldBox;
  /** 손잡이를 놓았을 때의 초안(끄는 동안은 그대로). 이름표·알림에 쓴다. */
  settled: WorldBox;
  provenance: ViewerEditProvenance;
  /** 지금 끌고 있는 손잡이. */
  dragging: EditHandle | null;
  /** 마지막으로 거부된 미리보기(초안은 그 직전 유효 값에 머문다). */
  refused: { reason: 'NOT_EDITABLE' | 'INVALID_GEOMETRY'; detail: string } | null;
}

export interface EditState {
  session: EditSession | null;
  /** 마지막 결과. 바뀔 때마다 새 객체라 구독자가 알린다. */
  lastResult: ViewerEditResult | null;
  begin(input: unknown): ViewerEditCommandResult;
  /** from(보통 끌기 시작 때 초안)에 조작을 적용해 초안으로 둔다. 거부되면 초안은 그대로다. */
  preview(from: WorldBox, operation: EditOperation): EditOutcome;
  setDragging(handle: EditHandle | null): void;
  /** 끌던 조작을 되돌린다(Esc). 초안을 끌기 전 값으로 둔다. */
  restoreDraft(draft: WorldBox): void;
  cancel(reason?: ViewerEditCancelReason): ViewerEditResult | null;
  finish(): ViewerEditResult | null;
}

export type EditStore = StoreApi<EditState>;

const copyBox = (box: WorldBox): WorldBox =>
  ({ min: [...box.min], max: [...box.max] }) as unknown as WorldBox;

export function previewOf(session: EditSession): ViewerEditPreview {
  return {
    target: session.target,
    mode: session.mode,
    before: boxJson(session.base),
    draft: boxJson(session.draft),
    provenance: session.provenance,
    saved: false,
  };
}

/**
 * 뷰어 인스턴스마다 하나. 뷰어 선택·모드·데이터를 읽어 세션을 지킨다.
 * connect() 가 돌려준 해제 함수를 뷰어가 내려갈 때 부른다(진행 중인 편집은 UNMOUNTED 로 취소).
 */
export function createEditStore(viewer: ViewerStore, data: SpatialDataStore) {
  const store = createStore<EditState>()((set, get) => {
    const reject = (
      target: ViewerTarget | null,
      reason: ViewerEditRejectReason,
      detail: string,
    ): ViewerEditResult => {
      const result: ViewerEditResult = { status: 'REJECTED', target, reason, detail, saved: false };
      set({ session: null, lastResult: result });
      return result;
    };

    const cancel = (reason: ViewerEditCancelReason = 'USER') => {
      const { session } = get();
      if (!session) return null;
      const result: ViewerEditResult = {
        status: 'CANCELLED',
        target: session.target,
        mode: session.mode,
        before: boxJson(session.base),
        reason,
        provenance: session.provenance,
        saved: false,
      };
      set({ session: null, lastResult: result });
      return result;
    };

    // 시작하지 못한 이유만 알린다. 진행 중인 다른 편집은 그대로 둔다.
    const refuse = (
      target: ViewerTarget | null,
      reason: ViewerEditRejectReason,
      detail: string,
    ): ViewerEditCommandResult => {
      set({ lastResult: { status: 'REJECTED', target, reason, detail, saved: false } });
      return { ok: false, reason };
    };

    return {
      session: null,
      lastResult: null,

      begin: (input) => {
        const target = parseTarget(input);
        if (!target) return refuse(null, 'INVALID_TARGET', '대상 모양이 틀렸다');
        // 사람은 관측 결과라 편집 대상이 아니다. 작업면(object)은 -188 도메인이 정책을 연결할 때 연다.
        if (target.type !== 'zone') {
          return refuse(target, 'NOT_EDITABLE', `${target.type} 는 아직 편집 대상이 아니다`);
        }
        const { phase, mode } = viewer.getState();
        if (phase !== 'ready') return refuse(target, 'NOT_READY', '뷰어가 준비되지 않았다');
        if (mode === 'REPLAY') {
          return refuse(
            target,
            'NOT_EDITABLE',
            '기록 재생(REPLAY)은 과거 입력이라 편집하지 않는다',
          );
        }
        const state = data.getState();
        const zone = state.zones.zones.find((z) => z.zoneId === target.id);
        if (state.zones.status !== 'READY' || !zone) {
          return refuse(target, 'NOT_FOUND', '장면에 그 구역이 없다');
        }
        const base = copyBox(zone);
        if (!isValidBox(base))
          return refuse(target, 'INVALID_GEOMETRY', '원본 형상이 유효하지 않다');
        const current = get().session;
        if (current && sameTarget(current.target, target)) return { ok: true };
        if (!viewer.getState().targets.has(targetKey(target))) {
          return refuse(target, 'NOT_FOUND', '장면에 그 구역이 등록되지 않았다');
        }
        if (current) cancel('REPLACED');
        // Gizmo 는 선택된 구역에만 붙는다. 편집 대상을 선택으로 맞춘다(선택 브리지는 뷰어 Store 하나).
        viewer.getState().select(target);
        set({
          session: {
            target,
            mode,
            policy: ZONE_AABB_POLICY,
            base,
            draft: copyBox(base),
            settled: copyBox(base),
            provenance: {
              sourceId: state.sourceId,
              zoneContract: state.zones.contract,
              zonesReceivedAt: state.zones.receivedAt,
              sceneVersion: state.world?.sceneVersion ?? null,
              configVersion: null,
            },
            dragging: null,
            refused: null,
          },
        });
        return { ok: true };
      },

      preview: (from, operation) => {
        const session = get().session;
        if (!session) return { ok: false, reason: 'NOT_EDITABLE', detail: '편집 중이 아니다' };
        const outcome = applyEditOperation({ kind: 'aabb', ...from }, operation, session.policy);
        if (!outcome.ok) {
          set({
            session: { ...session, refused: { reason: outcome.reason, detail: outcome.detail } },
          });
          return outcome;
        }
        if (outcome.shape.kind !== 'aabb') return outcome;
        const draft = { min: outcome.shape.min, max: outcome.shape.max };
        if (sameBox(draft, session.draft, 0) && session.refused === null) return outcome;
        set({
          session: {
            ...session,
            draft,
            settled: session.dragging ? session.settled : draft,
            refused: null,
          },
        });
        return outcome;
      },

      setDragging: (handle) => {
        const session = get().session;
        if (!session || session.dragging === handle) return;
        set({
          session: {
            ...session,
            dragging: handle,
            settled: handle === null ? session.draft : session.settled,
          },
        });
      },

      restoreDraft: (draft) => {
        const session = get().session;
        if (!session) return;
        const restored = copyBox(draft);
        set({
          session: {
            ...session,
            draft: restored,
            settled: restored,
            dragging: null,
            refused: null,
          },
        });
      },

      cancel,

      finish: () => {
        const { session } = get();
        if (!session) return null;
        if (!isValidBox(session.draft)) {
          return reject(session.target, 'INVALID_GEOMETRY', '초안 형상이 유효하지 않다');
        }
        const result: ViewerEditResult = {
          status: 'FINISHED',
          target: session.target,
          mode: session.mode,
          before: boxJson(session.base),
          after: boxJson(session.draft),
          changed: !sameBox(session.base, session.draft, 0),
          provenance: session.provenance,
          saved: false,
        };
        set({ session: null, lastResult: result });
        return result;
      },
    };
  });

  /** 원본·선택·모드를 지켜보다가 세션을 끝낸다. 해제 함수를 돌려준다. */
  const connect = () => {
    const end = (reason: ViewerEditRejectReason, detail: string) => {
      const session = store.getState().session;
      if (!session) return;
      const result: ViewerEditResult = {
        status: 'REJECTED',
        target: session.target,
        reason,
        detail,
        saved: false,
      };
      store.setState({ session: null, lastResult: result });
    };
    const offViewer = viewer.subscribe((state, prev) => {
      const session = store.getState().session;
      if (!session) return;
      if (state.phase === 'error' && prev.phase !== 'error')
        return end('NOT_READY', '뷰어 렌더링 오류');
      if (state.mode !== prev.mode) {
        if (state.mode === 'REPLAY') return end('NOT_EDITABLE', '기록 재생(REPLAY)으로 바뀌었다');
        store.getState().cancel('MODE_CHANGED');
        return;
      }
      if (state.selected !== prev.selected && !sameTarget(state.selected, session.target)) {
        store.getState().cancel('SELECTION_CHANGED');
      }
    });
    const offData = data.subscribe((state, prev) => {
      const session = store.getState().session;
      if (!session) return;
      if (state.sourceId !== session.provenance.sourceId)
        return end('STALE_SOURCE', '입력 Source 가 바뀌었다');
      if (state.zones !== prev.zones) {
        const zone = state.zones.zones.find((z) => z.zoneId === session.target.id);
        if (!zone) return end('STALE_SOURCE', '원본 구역이 사라졌다');
        if (!sameBox(zone, session.base, 0))
          return end('STALE_SOURCE', '원본 구역 형상이 바뀌었다');
      }
      const scene = state.world?.sceneVersion ?? null;
      const began = session.provenance.sceneVersion;
      if (began !== null && scene !== null && scene !== began) {
        end('STALE_SOURCE', `Scene 버전이 바뀌었다(${began} → ${scene})`);
      }
    });
    return () => {
      offViewer();
      offData();
      store.getState().cancel('UNMOUNTED');
    };
  };

  return { store, connect };
}
