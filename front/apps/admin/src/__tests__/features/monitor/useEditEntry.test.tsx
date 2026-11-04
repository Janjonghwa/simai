import { useEditEntry } from '@/features/monitor/useEditEntry';
import { useSessionStore, type Role } from '@/store/session.store';
import { stopZoneEdit, useZoneEditModeStore } from '@/store/zoneEditMode.store';
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

function signIn(role: Role) {
  useSessionStore.setState({ session: { accessToken: 't', user: { id: role, role } } });
}

beforeEach(() => signIn('ADMIN'));
afterEach(() => {
  cleanup();
  stopZoneEdit();
  useSessionStore.setState({ session: undefined });
});

describe('useEditEntry', () => {
  it('요청이 오면 관리자에게 편집 모드를 켜고 요청을 지운다', () => {
    const clear = vi.fn();
    renderHook(() => useEditEntry(true, clear));
    expect(useZoneEditModeStore.getState().editing).toBe(true);
    expect(clear).toHaveBeenCalledOnce();
  });

  it('요청이 없으면 아무것도 하지 않는다', () => {
    const clear = vi.fn();
    renderHook(() => useEditEntry(undefined, clear));
    expect(useZoneEditModeStore.getState().editing).toBe(false);
    expect(clear).not.toHaveBeenCalled();
  });

  it('관리자가 아니면 편집 모드를 켜지 않고 요청만 지운다', () => {
    signIn('VIEWER');
    const clear = vi.fn();
    renderHook(() => useEditEntry(true, clear));
    expect(useZoneEditModeStore.getState().editing).toBe(false);
    expect(clear).toHaveBeenCalledOnce();
  });

  it('요청을 지운 뒤 다시 그려도 한 번만 켠다', () => {
    const clear = vi.fn();
    const { rerender } = renderHook(({ requested }) => useEditEntry(requested, clear), {
      initialProps: { requested: true as boolean | undefined },
    });
    act(() => stopZoneEdit());
    rerender({ requested: undefined });
    expect(useZoneEditModeStore.getState().editing).toBe(false);
    expect(clear).toHaveBeenCalledOnce();
  });
});
