import type { SpatialDataStore } from '@/features/viewer3d/data/spatialData.store';
import {
  applyEditOperation,
  applyEditOperations,
  boxJson,
  fieldOperation,
  isValidBox,
  isValidShape,
  objectFieldOperations,
  sameBox,
  WORK_SURFACE_OBB_POLICY,
  ZONE_AABB_POLICY,
  type EditFace,
  type EditField,
  type EditOperation,
  type EditOutcome,
  type EditPolicy,
  type ObjectEditField,
  type WorldBox,
  type WorldObb,
} from '@/features/viewer3d/editing/editGeometry';
import type {
  ViewerEditCancelReason,
  ViewerEditCommandResult,
  ViewerEditPreview,
  ViewerEditProvenance,
  ViewerEditRejectReason,
  ViewerEditResult,
  ViewerObjectEditOrigin,
  ViewerObjectEditPreview,
  ViewerObjectEditProvenance,
  ViewerObjectEditResult,
} from '@/features/viewer3d/editing/editTypes';
import {
  geometryProblem,
  obbWorkSurface,
  sameObb,
  surfaceIdProblem,
  workSurfaceItem,
  workSurfaceJson,
  workSurfaceObb,
  type WorkSurfaceGeometry,
} from '@/features/viewer3d/surfaces/workSurfaceGeometry';
import { parseTarget, sameTarget, targetKey } from '@/features/viewer3d/target';
import type { ViewerMode, ViewerTarget } from '@/features/viewer3d/types';
import type { ViewerStore } from '@/features/viewer3d/viewer3d.store';
import { createStore, type StoreApi } from 'zustand/vanilla';

// 편집 세션 하나의 수명: 시작(원본 사본·출처 기록) → 미리보기(초안만 바꿈) → 취소(초안 폐기) 또는 완료(World 결과 반환).
// 원본 ZoneSnapshot·선택·위험 이벤트·적용 상태는 읽기만 한다. 저장·ACK 요청은 하지 않는다.
// 원본 구역이 바뀌거나 사라지고, 입력 Source·Scene 버전이 바뀌면 초안을 버리고 STALE_SOURCE 로 끝낸다.
// 작업면 OBB(-188)도 같은 Store·같은 엔진(applyEditOperation)·같은 수명 규칙을 쓴다. 구역 세션(session)과
// 작업면 세션(objectSession)은 동시에 하나만 있고, 결과는 모양이 달라 lastResult·lastObjectResult 로 따로 알린다.

/** 마우스로 잡는 손잡이. move 는 X·Y 이동, 나머지는 그 면의 경계다. */
export type EditHandle = 'move' | EditFace;

/** 작업면 OBB 손잡이. move X·Y 이동, x±·y± 지역 축 면, z+ 상판, yaw 수평 회전. */
export const OBJECT_EDIT_HANDLES = ['move', 'x-', 'x+', 'y-', 'y+', 'z+', 'yaw'] as const;
export type ObjectEditHandle = (typeof OBJECT_EDIT_HANDLES)[number];

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

/** 작업면 OBB 편집 세션(-188). 원본(base)은 바뀌지 않는다. */
export interface ObjectEditSession {
  target: ViewerTarget;
  mode: ViewerMode;
  policy: EditPolicy;
  base: WorldObb;
  draft: WorldObb;
  settled: WorldObb;
  provenance: ViewerObjectEditProvenance;
  /** REGISTERED 받은 작업면 편집 · NEW 3D 에서 새로 만듦(임시로 선택 대상에 올렸다가 끝나면 내린다). */
  origin: ViewerObjectEditOrigin;
  /** 받은 원본 항목(얕은 사본). 결과 항목(afterItem)에 다른 키를 그대로 돌려준다. NEW 는 빈 객체. */
  source: Readonly<Record<string, unknown>>;
  dragging: ObjectEditHandle | null;
  refused: { reason: 'NOT_EDITABLE' | 'INVALID_GEOMETRY'; detail: string } | null;
}

export interface EditState {
  session: EditSession | null;
  /** 마지막 결과. 바뀔 때마다 새 객체라 구독자가 알린다. */
  lastResult: ViewerEditResult | null;
  /** 작업면 OBB 편집 세션. 구역 세션과 동시에 열리지 않는다. */
  objectSession: ObjectEditSession | null;
  /** 작업면 편집의 마지막 결과(시작 거부 포함). */
  lastObjectResult: ViewerObjectEditResult | null;
  begin(input: unknown): ViewerEditCommandResult;
  /** from(보통 끌기 시작 때 초안)에 조작을 적용해 초안으로 둔다. 거부되면 초안은 그대로다. */
  preview(from: WorldBox, operation: EditOperation): EditOutcome;
  setDragging(handle: EditHandle | null): void;
  /** 끌던 조작을 되돌린다(Esc). 초안을 끌기 전 값으로 둔다. */
  restoreDraft(draft: WorldBox): void;
  /**
   * 초안의 한 값을 World 절대값으로 고친다(숫자 입력). 끌기와 같은 엔진·정책으로 검사하고,
   * 거부되면 초안은 마지막 유효 값에 둔다. 끄는 중에는 받지 않는다.
   */
  updateField(field: EditField, value: number): ViewerEditCommandResult;
  cancel(reason?: ViewerEditCancelReason): ViewerEditResult | null;
  finish(): ViewerEditResult | null;
  /** from(끌기 시작 때 작업면 초안)에 조작을 적용한다. 거부되면 초안은 그대로다. */
  previewObject(from: WorldObb, operation: EditOperation): EditOutcome;
  setObjectDragging(handle: ObjectEditHandle | null): void;
  restoreObjectDraft(draft: WorldObb): void;
  /** 작업면 초안의 한 값을 절대값으로 고친다(숫자 입력). 끌기와 같은 엔진·정책으로 검사한다. */
  updateObjectField(field: ObjectEditField, value: number): ViewerEditCommandResult;
  cancelObject(reason?: ViewerEditCancelReason): ViewerObjectEditResult | null;
  finishObject(): ViewerObjectEditResult | null;
  /** 새 작업면을 3D 에서 만든다(-188). ID·처음 형상을 받아 NEW 편집 세션을 연다. 등록·저장은 하지 않는다. */
  createObject(input: unknown): ViewerEditCommandResult;
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

export function objectPreviewOf(session: ObjectEditSession): ViewerObjectEditPreview {
  return {
    target: session.target,
    mode: session.mode,
    origin: session.origin,
    before: workSurfaceJson(obbWorkSurface(session.base)),
    draft: workSurfaceJson(obbWorkSurface(session.draft)),
    provenance: session.provenance,
    saved: false,
  };
}

const copyObb = (obb: WorldObb): WorldObb =>
  ({ center: [...obb.center], size: [...obb.size], yaw: obb.yaw }) as unknown as WorldObb;

/** 새로 만든 작업면(NEW) 세션이 끝나면 임시로 올린 선택 대상을 내린다. */
function releaseCreated(viewer: ViewerStore, session: ObjectEditSession) {
  if (session.origin === 'NEW') viewer.getState().unregisterTarget(session.target);
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

    /** 작업면 세션을 끝낸다. 새로 만든 작업면(NEW)은 임시로 올린 선택 대상을 내린다. */
    const closeObject = (session: ObjectEditSession, result: ViewerObjectEditResult) => {
      set({ objectSession: null, lastObjectResult: result });
      releaseCreated(viewer, session);
    };

    const cancelObject = (reason: ViewerEditCancelReason = 'USER') => {
      const { objectSession } = get();
      if (!objectSession) return null;
      const result: ViewerObjectEditResult = {
        status: 'CANCELLED',
        target: objectSession.target,
        mode: objectSession.mode,
        origin: objectSession.origin,
        before: workSurfaceJson(obbWorkSurface(objectSession.base)),
        reason,
        provenance: objectSession.provenance,
        saved: false,
      };
      closeObject(objectSession, result);
      return result;
    };

    const refuseObject = (
      target: ViewerTarget,
      reason: ViewerEditRejectReason,
      detail: string,
    ): ViewerEditCommandResult => {
      set({ lastObjectResult: { status: 'REJECTED', target, reason, detail, saved: false } });
      return { ok: false, reason };
    };

    /** 엔진 결과를 작업면 초안에 반영한다. 거부되면 이유만 남기고 초안은 마지막 유효 값이다. */
    const settleObject = (session: ObjectEditSession, outcome: EditOutcome): EditOutcome => {
      if (!outcome.ok) {
        set({
          objectSession: {
            ...session,
            refused: { reason: outcome.reason, detail: outcome.detail },
          },
        });
        return outcome;
      }
      if (outcome.shape.kind !== 'obb') return outcome;
      const { center, size, yaw } = outcome.shape;
      const draft: WorldObb = { center, size, yaw };
      if (sameObb(draft, session.draft, 0) && session.refused === null) return outcome;
      set({
        objectSession: {
          ...session,
          draft,
          settled: session.dragging ? session.settled : draft,
          refused: null,
        },
      });
      return outcome;
    };

    /** 작업면 OBB 편집 시작(-188). 구역 시작과 같은 순서로 검사한다. */
    const beginObject = (target: ViewerTarget): ViewerEditCommandResult => {
      const { phase, mode } = viewer.getState();
      if (phase !== 'ready') return refuseObject(target, 'NOT_READY', '뷰어가 준비되지 않았다');
      if (mode === 'REPLAY') {
        return refuseObject(
          target,
          'NOT_EDITABLE',
          '기록 재생(REPLAY)은 과거 입력이라 편집하지 않는다',
        );
      }
      const state = data.getState();
      const surface = state.workSurfaces.surfaces.find((s) => s.surfaceId === target.id);
      if (state.workSurfaces.status !== 'READY' || !surface) {
        return refuseObject(target, 'NOT_FOUND', '장면에 그 작업면이 없다');
      }
      const base = workSurfaceObb(surface);
      if (!isValidShape({ kind: 'obb', ...base })) {
        return refuseObject(target, 'INVALID_GEOMETRY', '원본 형상이 유효하지 않다');
      }
      const current = get().objectSession;
      if (current && sameTarget(current.target, target)) return { ok: true };
      if (!viewer.getState().targets.has(targetKey(target))) {
        return refuseObject(target, 'NOT_FOUND', '장면에 그 작업면이 등록되지 않았다');
      }
      if (current) cancelObject('REPLACED');
      if (get().session) cancel('REPLACED');
      viewer.getState().select(target);
      set({
        objectSession: {
          target,
          mode,
          policy: WORK_SURFACE_OBB_POLICY,
          base,
          draft: copyObb(base),
          settled: copyObb(base),
          provenance: {
            sourceId: state.sourceId,
            geometryContract: state.workSurfaces.contract,
            surfacesReceivedAt: state.workSurfaces.receivedAt,
            geometryVersion: state.workSurfaces.geometryVersion,
            sceneVersion: state.world?.sceneVersion ?? null,
            configVersion: null,
          },
          origin: 'REGISTERED',
          source: surface.source,
          dragging: null,
          refused: null,
        },
      });
      return { ok: true };
    };

    /** 새 작업면 만들기(-188). 받은 ID·처음 형상을 검사하고 장면에 임시 선택 대상으로 올린다. */
    const createObject = (input: unknown): ViewerEditCommandResult => {
      const id: unknown =
        typeof input === 'object' && input !== null ? Reflect.get(input, 'id') : undefined;
      const geometry: unknown =
        typeof input === 'object' && input !== null ? Reflect.get(input, 'geometry') : undefined;
      const idProblem = surfaceIdProblem(id);
      if (idProblem) {
        set({
          lastObjectResult: {
            status: 'REJECTED',
            target: null,
            reason: 'INVALID_TARGET',
            detail: idProblem,
            saved: false,
          },
        });
        return { ok: false, reason: 'INVALID_TARGET' };
      }
      const target: ViewerTarget = { type: 'object', id: id as string };
      const { phase, mode } = viewer.getState();
      if (phase !== 'ready') return refuseObject(target, 'NOT_READY', '뷰어가 준비되지 않았다');
      if (mode === 'REPLAY') {
        return refuseObject(target, 'NOT_EDITABLE', '기록 재생(REPLAY)에서는 만들지 않는다');
      }
      const state = data.getState();
      if (
        state.workSurfaces.surfaces.some((s) => s.surfaceId === target.id) ||
        viewer.getState().targets.has(targetKey(target))
      ) {
        return refuseObject(target, 'INVALID_TARGET', '이미 장면에 있는 ID 다');
      }
      const problem = geometryProblem(geometry);
      if (problem) return refuseObject(target, 'INVALID_GEOMETRY', problem);
      const base = workSurfaceObb(geometry as WorkSurfaceGeometry);
      const current = get().objectSession;
      if (current) cancelObject('REPLACED');
      if (get().session) cancel('REPLACED');
      viewer.getState().registerTarget(target);
      viewer.getState().select(target);
      set({
        objectSession: {
          target,
          mode,
          policy: WORK_SURFACE_OBB_POLICY,
          base,
          draft: copyObb(base),
          settled: copyObb(base),
          provenance: {
            sourceId: state.sourceId,
            geometryContract: state.workSurfaces.contract,
            surfacesReceivedAt: state.workSurfaces.receivedAt,
            geometryVersion: state.workSurfaces.geometryVersion,
            sceneVersion: state.world?.sceneVersion ?? null,
            configVersion: null,
          },
          origin: 'NEW',
          source: {},
          dragging: null,
          refused: null,
        },
      });
      return { ok: true };
    };

    return {
      session: null,
      lastResult: null,
      objectSession: null,
      lastObjectResult: null,

      begin: (input) => {
        const target = parseTarget(input);
        if (!target) return refuse(null, 'INVALID_TARGET', '대상 모양이 틀렸다');
        // 사람은 관측 결과라 편집 대상이 아니다. 작업면(object)은 등록 OBB 편집(-188)으로 보낸다.
        if (target.type === 'object') return beginObject(target);
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
        if (get().objectSession) cancelObject('REPLACED');
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

      updateField: (field, value) => {
        const session = get().session;
        if (!session) return { ok: false, reason: 'NOT_EDITABLE' };
        if (session.dragging) return { ok: false, reason: 'NOT_READY' };
        const operation = fieldOperation(session.draft, field, value);
        if (!operation) {
          set({
            session: {
              ...session,
              refused: { reason: 'INVALID_GEOMETRY', detail: '숫자가 아니다' },
            },
          });
          return { ok: false, reason: 'INVALID_GEOMETRY' };
        }
        const outcome = get().preview(session.draft, operation);
        return outcome.ok ? { ok: true } : { ok: false, reason: outcome.reason };
      },

      cancel,

      finish: () => {
        const { session } = get();
        if (!session || session.dragging) return null;
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

      previewObject: (from, operation) => {
        const session = get().objectSession;
        if (!session) return { ok: false, reason: 'NOT_EDITABLE', detail: '편집 중이 아니다' };
        const outcome = applyEditOperation({ kind: 'obb', ...from }, operation, session.policy);
        return settleObject(session, outcome);
      },

      setObjectDragging: (handle) => {
        const session = get().objectSession;
        if (!session || session.dragging === handle) return;
        set({
          objectSession: {
            ...session,
            dragging: handle,
            settled: handle === null ? session.draft : session.settled,
          },
        });
      },

      restoreObjectDraft: (draft) => {
        const session = get().objectSession;
        if (!session) return;
        const restored = copyObb(draft);
        set({
          objectSession: {
            ...session,
            draft: restored,
            settled: restored,
            dragging: null,
            refused: null,
          },
        });
      },

      updateObjectField: (field, value) => {
        const session = get().objectSession;
        if (!session) return { ok: false, reason: 'NOT_EDITABLE' };
        if (session.dragging) return { ok: false, reason: 'NOT_READY' };
        const operations = objectFieldOperations(session.draft, field, value);
        if (!operations) {
          set({
            objectSession: {
              ...session,
              refused: { reason: 'INVALID_GEOMETRY', detail: '숫자가 아니거나 범위를 벗어났다' },
            },
          });
          return { ok: false, reason: 'INVALID_GEOMETRY' };
        }
        const outcome = settleObject(
          session,
          applyEditOperations({ kind: 'obb', ...session.draft }, operations, session.policy),
        );
        return outcome.ok ? { ok: true } : { ok: false, reason: outcome.reason };
      },

      cancelObject,

      finishObject: () => {
        const session = get().objectSession;
        if (!session || session.dragging) return null;
        if (!isValidShape({ kind: 'obb', ...session.draft })) {
          const result: ViewerObjectEditResult = {
            status: 'REJECTED',
            target: session.target,
            reason: 'INVALID_GEOMETRY',
            detail: '초안 형상이 유효하지 않다',
            saved: false,
          };
          closeObject(session, result);
          return result;
        }
        const result: ViewerObjectEditResult = {
          status: 'FINISHED',
          target: session.target,
          mode: session.mode,
          origin: session.origin,
          before: workSurfaceJson(obbWorkSurface(session.base)),
          after: workSurfaceJson(obbWorkSurface(session.draft)),
          afterItem: workSurfaceItem(
            session.target.id,
            obbWorkSurface(session.draft),
            session.source,
          ),
          changed: !sameObb(session.base, session.draft, 0),
          provenance: session.provenance,
          saved: false,
        };
        closeObject(session, result);
        return result;
      },

      createObject,
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
    const endObject = (reason: ViewerEditRejectReason, detail: string) => {
      const session = store.getState().objectSession;
      if (!session) return;
      const result: ViewerObjectEditResult = {
        status: 'REJECTED',
        target: session.target,
        reason,
        detail,
        saved: false,
      };
      store.setState({ objectSession: null, lastObjectResult: result });
      releaseCreated(viewer, session);
    };
    const offViewer = viewer.subscribe((state, prev) => {
      const object = store.getState().objectSession;
      if (object) {
        if (state.phase === 'error' && prev.phase !== 'error') {
          endObject('NOT_READY', '뷰어 렌더링 오류');
        } else if (state.mode !== prev.mode) {
          if (state.mode === 'REPLAY') endObject('NOT_EDITABLE', '기록 재생(REPLAY)으로 바뀌었다');
          else store.getState().cancelObject('MODE_CHANGED');
        } else if (state.selected !== prev.selected && !sameTarget(state.selected, object.target)) {
          store.getState().cancelObject('SELECTION_CHANGED');
        }
      }
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
      const object = store.getState().objectSession;
      if (object) {
        const scene = state.world?.sceneVersion ?? null;
        const began = object.provenance.sceneVersion;
        if (state.sourceId !== object.provenance.sourceId) {
          endObject('STALE_SOURCE', '입력 Source 가 바뀌었다');
        } else if (state.workSurfaces !== prev.workSurfaces) {
          const surface = state.workSurfaces.surfaces.find((s) => s.surfaceId === object.target.id);
          if (object.origin === 'NEW') {
            if (surface) endObject('STALE_SOURCE', '같은 ID 의 작업면이 들어왔다');
          } else if (!surface) endObject('STALE_SOURCE', '원본 작업면이 사라졌다');
          else if (!sameObb(workSurfaceObb(surface), object.base, 0)) {
            endObject('STALE_SOURCE', '원본 작업면 형상이 바뀌었다');
          }
        } else if (began !== null && scene !== null && scene !== began) {
          endObject('STALE_SOURCE', `Scene 버전이 바뀌었다(${began} → ${scene})`);
        }
      }
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
      store.getState().cancelObject('UNMOUNTED');
    };
  };

  return { store, connect };
}
