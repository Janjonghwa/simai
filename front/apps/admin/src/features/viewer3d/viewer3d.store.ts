import {
  DEFAULT_LAYERS,
  resolveLayers,
  sameLayers,
  type ResolvedLayers,
} from '@/features/viewer3d/layers';
import type { ViewerTheme } from '@/features/viewer3d/palette';
import { isViewerMode, parseTarget, sameTarget, targetKey } from '@/features/viewer3d/target';
import type {
  CommandResult,
  ViewerError,
  ViewerMode,
  ViewerPhase,
  ViewerSelectCancelReason,
  ViewerSelectOptions,
  ViewerSelectRequest,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d/types';
import { createStore, type StoreApi } from 'zustand/vanilla';

export interface FocusRequest {
  target: ViewerTarget;
}

/** 기다리는 선택의 최대 시간(ms). 이보다 길게 주면 이 값으로 자른다. */
export const MAX_SELECT_WAIT_MS = 30_000;

interface PendingSelect {
  target: ViewerTarget;
  focus: boolean;
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
  /** 장면 색표(화면 테마와 같다). */
  theme: ViewerTheme;
  /** 포인터가 1초 넘게 머문 대상 키. 그 대상의 이름표를 보인다(관제 데모와 같다). */
  hovered: string | null;
  /** 처음 시점으로 돌아가기 요청 수. 늘 때마다 카메라를 처음 자리로 옮긴다. */
  viewResets: number;
  /** 레이어별 표시·피킹(-234). 기본값은 모두 interactive(기존 동작). */
  layers: ResolvedLayers;
  /** 기다리는 선택 요청(-234). 하나뿐이다. */
  pendingSelect: PendingSelect | null;
  /** 기다리는 선택의 마지막 진행. 바뀔 때마다 새 객체라 구독자가 알린다. */
  selectRequest: ViewerSelectRequest | null;

  registerTarget(target: ViewerTarget): void;
  unregisterTarget(target: ViewerTarget): void;
  select(input: unknown): CommandResult;
  /** select 에 기다리기·초점을 더한 명령(-234). options 가 없으면 select 와 같다. */
  requestSelect(input: unknown, options?: ViewerSelectOptions): CommandResult;
  activate(input: unknown): CommandResult;
  clearSelection(): CommandResult;
  focusOn(input: unknown): CommandResult;
  setMode(input: unknown): CommandResult;
  setLayers(input: unknown): void;
  markReady(): void;
  markError(error: ViewerError): void;
  /** 뷰어가 내려갈 때 기다리던 선택을 UNMOUNTED 로 끝내고 타이머를 푼다. */
  dispose(): void;
  setTheme(theme: ViewerTheme): void;
  setHovered(key: string | null): void;
  resetView(): CommandResult;
}

export type ViewerStore = StoreApi<ViewerState>;

const OK: CommandResult = { ok: true };

export function toStatus(state: ViewerState): ViewerStatus {
  return { phase: state.phase, mode: state.mode, error: state.error };
}

/** 뷰어 인스턴스마다 하나 만든다. 30 FPS World 상태는 여기에 두지 않는다. */
export function createViewerStore(initialMode: ViewerMode = 'LIVE'): ViewerStore {
  return createStore<ViewerState>()((set, get) => {
    let pendingTimer: ReturnType<typeof setTimeout> | null = null;
    const stopTimer = () => {
      if (pendingTimer !== null) clearTimeout(pendingTimer);
      pendingTimer = null;
    };
    /** 기다리던 선택을 끝낸다. 상태 하나로 알린다. */
    const settle = (
      status: 'APPLIED' | 'EXPIRED' | 'CANCELLED',
      reason?: ViewerSelectCancelReason,
      patch: Partial<ViewerState> = {},
    ) => {
      const pending = get().pendingSelect;
      if (!pending) return;
      stopTimer();
      set({
        ...patch,
        pendingSelect: null,
        selectRequest: {
          target: pending.target,
          status,
          focus: pending.focus,
          ...(reason ? { reason } : {}),
        },
      });
    };
    /** 기다리던 대상이 지금 고를 수 있으면 고른다. */
    const applyNow = () => {
      const pending = get().pendingSelect;
      if (!pending || get().phase !== 'ready') return;
      if (!get().targets.has(targetKey(pending.target))) return;
      settle('APPLIED', undefined, {
        selected: pending.target,
        ...(pending.focus ? { focus: { target: pending.target } } : {}),
      });
    };
    // 등록 직후가 아니라 이번 반영이 끝난 뒤에 고른다. 새로 보인 레이어는 React StrictMode 에서 등록→해제→등록을
    // 한 번에 거치는데, 그 사이에 고르면 해제가 선택을 풀어 버린다. 끝난 뒤에도 등록돼 있을 때만 고른다.
    let applyQueued = false;
    const tryApply = () => {
      if (applyQueued || !get().pendingSelect) return;
      applyQueued = true;
      queueMicrotask(() => {
        applyQueued = false;
        applyNow();
      });
    };

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
      layers: DEFAULT_LAYERS,
      pendingSelect: null,
      selectRequest: null,
      theme: 'dark',
      hovered: null,
      viewResets: 0,

      registerTarget: (target) => {
        const key = targetKey(target);
        const targets = new Map(get().targets);
        targets.set(key, (targets.get(key) ?? 0) + 1);
        set({ targets });
        tryApply();
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
        // 3D 클릭·다른 선택이 기다리던 요청보다 나중이다. 같은 대상이면 그 요청이 이뤄진 것이다.
        if (get().pendingSelect) {
          if (sameTarget(get().pendingSelect!.target, resolved.target)) settle('APPLIED');
          else settle('CANCELLED', 'SELECTION_CHANGED');
        }
        if (!sameTarget(get().selected, resolved.target)) set({ selected: resolved.target });
        return OK;
      },

      requestSelect: (input, options) => {
        const waitMs = Math.min(Math.max(Number(options?.waitMs) || 0, 0), MAX_SELECT_WAIT_MS);
        const focus = options?.focus === true;
        const target = parseTarget(input);
        if (!target) return { ok: false, reason: 'INVALID_TARGET' };
        // 새 요청이 오면 기다리던 요청은 바뀐 것이다.
        settle('CANCELLED', 'REPLACED');
        const resolved = resolve(target);
        if ('target' in resolved) {
          const result = get().select(target);
          if (result.ok && focus) set({ focus: { target } });
          return result;
        }
        // 기다릴 수 있는 이유는 아직 준비 중이거나 아직 장면에 없을 때뿐이다(오류 상태는 기다리지 않는다).
        const reason = resolved.ok ? null : resolved.reason;
        const waitable =
          waitMs > 0 &&
          get().phase !== 'error' &&
          (reason === 'NOT_READY' || reason === 'NOT_FOUND');
        if (!waitable) return resolved;
        set({
          pendingSelect: { target, focus },
          selectRequest: { target, status: 'PENDING', focus },
        });
        pendingTimer = setTimeout(() => {
          pendingTimer = null;
          settle('EXPIRED');
        }, waitMs);
        return { ok: true, pending: true };
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
        settle('CANCELLED', 'SELECTION_CHANGED');
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

      setLayers: (input) => {
        const layers = resolveLayers(input);
        if (!sameLayers(layers, get().layers)) set({ layers });
      },

      markReady: () => {
        if (get().phase !== 'ready') set({ phase: 'ready', error: null });
        tryApply();
      },

      markError: (error) => {
        settle('CANCELLED', 'NOT_READY');
        set({ phase: 'error', error });
      },

      dispose: () => settle('CANCELLED', 'UNMOUNTED'),

      setTheme: (theme) => {
        if (get().theme !== theme) set({ theme });
      },

      setHovered: (key) => {
        if (get().hovered !== key) set({ hovered: key });
      },

      resetView: () => {
        if (get().phase !== 'ready') return { ok: false, reason: 'NOT_READY' };
        set({ viewResets: get().viewResets + 1 });
        return OK;
      },
    };
  });
}
