import { isViewerMode, parseTarget, sameTarget, targetKey } from '@/features/viewer3d/target';
import type {
  CommandResult,
  ViewerError,
  ViewerMode,
  ViewerPhase,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d/types';
import { createStore, type StoreApi } from 'zustand/vanilla';

export interface FocusRequest {
  target: ViewerTarget;
}

export interface ViewerState {
  phase: ViewerPhase;
  error: ViewerError | null;
  mode: ViewerMode;
  selected: ViewerTarget | null;
  /** 요청마다 새 객체라 같은 대상을 다시 요청해도 카메라가 다시 맞춘다. */
  focus: FocusRequest | null;
  /** 3D 더블클릭(활성화) 요청. 요청마다 새 객체로 알린다. */
  activated: FocusRequest | null;
  /** 장면에 올라온 선택 가능 대상의 키별 등록 수. 같은 키가 잠시 겹쳐도 하나가 빠질 때 지우지 않는다. */
  targets: ReadonlyMap<string, number>;

  registerTarget(target: ViewerTarget): void;
  unregisterTarget(target: ViewerTarget): void;
  select(input: unknown): CommandResult;
  activate(input: unknown): CommandResult;
  clearSelection(): CommandResult;
  focusOn(input: unknown): CommandResult;
  setMode(input: unknown): CommandResult;
  markReady(): void;
  markError(error: ViewerError): void;
}

export type ViewerStore = StoreApi<ViewerState>;

const OK: CommandResult = { ok: true };

export function toStatus(state: ViewerState): ViewerStatus {
  return { phase: state.phase, mode: state.mode, error: state.error };
}

/** 뷰어 인스턴스마다 하나 만든다. 30 FPS World 상태는 여기에 두지 않는다. */
export function createViewerStore(initialMode: ViewerMode = 'LIVE'): ViewerStore {
  return createStore<ViewerState>()((set, get) => {
    // 명령 대상이 지금 장면에 있는지 확인한다.
    const resolve = (input: unknown): { target: ViewerTarget } | CommandResult => {
      const target = parseTarget(input);
      if (!target) return { ok: false, reason: 'INVALID_TARGET' };
      if (get().phase !== 'ready') return { ok: false, reason: 'NOT_READY' };
      if (!get().targets.has(targetKey(target))) return { ok: false, reason: 'NOT_FOUND' };
      return { target };
    };

    return {
      phase: 'initializing',
      error: null,
      mode: isViewerMode(initialMode) ? initialMode : 'LIVE',
      selected: null,
      focus: null,
      activated: null,
      targets: new Map(),

      registerTarget: (target) => {
        const key = targetKey(target);
        const targets = new Map(get().targets);
        targets.set(key, (targets.get(key) ?? 0) + 1);
        set({ targets });
      },

      unregisterTarget: (target) => {
        const key = targetKey(target);
        const count = get().targets.get(key);
        if (count === undefined) return;
        const targets = new Map(get().targets);
        if (count > 1) {
          targets.set(key, count - 1);
          set({ targets });
          return;
        }
        targets.delete(key);
        // 장면에서 사라진 대상을 선택·초점으로 남겨 두지 않는다.
        const { selected, focus } = get();
        set({
          targets,
          selected: sameTarget(selected, target) ? null : selected,
          focus: focus && sameTarget(focus.target, target) ? null : focus,
        });
      },

      select: (input) => {
        const resolved = resolve(input);
        if (!('target' in resolved)) return resolved;
        if (!sameTarget(get().selected, resolved.target)) set({ selected: resolved.target });
        return OK;
      },

      activate: (input) => {
        const resolved = resolve(input);
        if (!('target' in resolved)) return resolved;
        set({
          selected: resolved.target,
          activated: { target: resolved.target },
        });
        return OK;
      },

      clearSelection: () => {
        if (get().selected !== null) set({ selected: null });
        return OK;
      },

      focusOn: (input) => {
        const resolved = resolve(input);
        if (!('target' in resolved)) return resolved;
        set({ focus: { target: resolved.target } });
        return OK;
      },

      setMode: (input) => {
        if (!isViewerMode(input)) return { ok: false, reason: 'INVALID_MODE' };
        if (get().mode !== input) set({ mode: input });
        return OK;
      },

      markReady: () => {
        if (get().phase !== 'ready') set({ phase: 'ready', error: null });
      },

      markError: (error) => set({ phase: 'error', error }),
    };
  });
}
