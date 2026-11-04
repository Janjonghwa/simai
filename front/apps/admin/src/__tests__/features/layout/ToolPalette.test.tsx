import { ToolPalette } from '@/features/layout/spatialHost/ToolPalette';
import { setSetupWorkUnitId, useSetupFlowStore } from '@/features/setup/setup.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { updateZoneDraft, useZoneEditStore } from '@/store/zoneEdit.store';
import { startZoneEdit, stopZoneEdit, useZoneEditModeStore } from '@/store/zoneEditMode.store';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const navigate = vi.hoisted(() => vi.fn());
vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => navigate,
}));
// 맵 재생성 도구의 서버 연결은 이 시험의 관심 밖이다(자체 시험이 있다).
vi.mock('@/features/layout/useSceneRegenerate', () => ({
  useSceneRegenerate: () => ({
    current: null,
    isInProgress: false,
    isSubmitting: false,
    isActivating: false,
    activatedSceneVersion: null,
    requestError: null,
    regenerate: vi.fn(),
    activateCandidate: vi.fn(),
  }),
}));

const onResetView = vi.fn();
const onRetry3d = vi.fn();

function renderPalette(
  props: Partial<React.ComponentProps<typeof ToolPalette>> = {},
  role: Role = 'ADMIN',
) {
  useSessionStore.setState({ session: { accessToken: 't', user: { id: role, role } } });
  return render(
    <ToolPalette
      view="3d"
      onResetView={onResetView}
      onRetry3d={onRetry3d}
      currentSceneVersion={null}
      {...props}
    />,
  );
}

const button = (name: string) => screen.queryByRole('button', { name });
const editButton = () => screen.getByRole('button', { name: '관리자 편집 모드' });

/** 저장하지 않은 구역 초안 하나를 만든다. */
const addDraft = () =>
  act(() => updateZoneDraft('z-1', () => ({ patch: { name: '임시 초안' }, box: null })));

beforeEach(() => {
  navigate.mockReset();
  onResetView.mockReset();
  onRetry3d.mockReset();
});
afterEach(() => {
  cleanup();
  stopZoneEdit();
  useZoneEditStore.getState().clear();
  useSetupFlowStore.setState({ zonesCommitted: false });
  setSetupWorkUnitId(null);
  useSessionStore.setState({ session: undefined });
});

describe('ToolPalette 틀', () => {
  it('도구 모음 하나가 위쪽 왼쪽 기본 자리에 있다', () => {
    renderPalette();
    const bar = screen.getByRole('group', { name: '도구' });
    expect(bar.className).toContain('top-[72px]');
    expect(bar.className).toContain('left-3.5');
    expect(bar.getAttribute('style')).toBeNull();
  });

  it('손잡이를 누르면 도구를 접고 다시 누르면 펼친다', () => {
    renderPalette();
    const handle = screen.getByRole('button', { name: '도구 접기' });
    expect(handle.getAttribute('aria-expanded')).toBe('true');
    // 손잡이는 도구 모양 하나뿐이다(점 여섯 개 없음).
    expect(handle.querySelectorAll('svg')).toHaveLength(1);
    expect(handle.querySelector('svg.lucide-wrench')).not.toBeNull();
    expect(button('시점 초기화')).toBeTruthy();

    fireEvent.click(handle);
    const folded = screen.getByRole('button', { name: '도구 펼치기' });
    expect(folded.getAttribute('aria-expanded')).toBe('false');
    expect(button('시점 초기화')).toBeNull();
    expect(button('관리자 편집 모드')).toBeNull();

    fireEvent.click(folded);
    expect(button('시점 초기화')).toBeTruthy();
  });

  it('3D 에서는 시점 초기화, 2D 로 내려오면 3D 다시 시도를 누른다', () => {
    const { rerender } = renderPalette();
    fireEvent.click(screen.getByRole('button', { name: '시점 초기화' }));
    expect(onResetView).toHaveBeenCalledOnce();
    expect(button('3D 다시 시도')).toBeNull();

    rerender(
      <ToolPalette
        view="2d"
        onResetView={onResetView}
        onRetry3d={onRetry3d}
        currentSceneVersion={null}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '3D 다시 시도' }));
    expect(onRetry3d).toHaveBeenCalledOnce();
    expect(button('시점 초기화')).toBeNull();
  });
});

describe('ToolPalette 이름표', () => {
  it('늦게 뜨는 브라우저 기본 title 대신 빨리 뜨는 이름표를 쓴다', () => {
    renderPalette();
    for (const name of ['도구 접기', '시점 초기화', '관리자 편집 모드', '맵 재생성']) {
      expect(screen.getByRole('button', { name }).getAttribute('title')).toBeNull();
    }
  });
});

describe('ToolPalette 끌기', () => {
  it('손잡이를 끌면 도구 모음이 옮겨지고 접히지 않으며, 끌지 않고 누르면 접는다', () => {
    renderPalette();
    const bar = screen.getByRole('group', { name: '도구' });
    const parent = bar.parentElement!;
    // jsdom 은 배치를 하지 않아 크기·자리가 0 이다. 부모와 도구 모음의 크기·자리를 속성으로 심는다.
    Object.defineProperties(parent, {
      clientWidth: { get: () => 1000, configurable: true },
      clientHeight: { get: () => 700, configurable: true },
    });
    Object.defineProperties(bar, {
      offsetParent: { get: () => parent, configurable: true },
      offsetLeft: { get: () => 14, configurable: true },
      offsetTop: { get: () => 72, configurable: true },
      offsetWidth: { get: () => 200, configurable: true },
      offsetHeight: { get: () => 36, configurable: true },
    });
    const handle = screen.getByRole('button', { name: '도구 접기' });

    fireEvent.pointerDown(handle, { button: 0, clientX: 20, clientY: 80, pointerId: 1 });
    fireEvent.pointerMove(handle, { buttons: 1, clientX: 220, clientY: 80, pointerId: 1 });
    fireEvent.pointerUp(handle, { pointerId: 1 });
    // 마우스로 놓은 뒤 따라 나오는 클릭(detail 1)은 삼켜져 접히지 않는다.
    fireEvent.click(handle, { detail: 1 });

    expect(bar.style.left).toBe('214px');
    expect(handle.getAttribute('aria-expanded')).toBe('true');

    fireEvent.click(handle);
    expect(handle.getAttribute('aria-expanded')).toBe('false');
  });
});

describe('ToolPalette 역할별 도구', () => {
  it('관리자는 편집 모드와 맵 재생성을 쓴다', () => {
    renderPalette();
    expect(button('관리자 편집 모드')).toBeTruthy();
    expect(button('맵 재생성')).toBeTruthy();
  });

  it.each(['VIEWER', 'FOREMAN'] as const)('%s 는 시점 초기화만 쓴다', (role) => {
    renderPalette({}, role);
    expect(button('시점 초기화')).toBeTruthy();
    expect(button('관리자 편집 모드')).toBeNull();
    expect(button('맵 재생성')).toBeNull();
  });

  it('관리자가 아니면 맵 재생성 도구를 아예 달지 않는다', () => {
    renderPalette({}, 'VIEWER');
    expect(button('맵 재생성')).toBeNull();
    // 숨긴 요소는 접근성 이름이 비므로 이름(aria-label)으로 직접 찾는다.
    expect(screen.queryByLabelText('맵 재생성')).toBeNull();
  });
});

describe('ToolPalette 맵 재생성 도구 숨기기', () => {
  // 숨긴 요소는 접근성 이름이 비므로 이름(aria-label)으로 직접 찾는다.
  const hiddenTool = () => screen.getByLabelText('맵 재생성');

  it('도구 모음을 접어도 떼지 않고 숨기기만 하며, 펼치면 다시 보인다', () => {
    renderPalette();
    expect(hiddenTool().hasAttribute('hidden')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: '도구 접기' }));
    expect(button('맵 재생성')).toBeNull();
    expect(hiddenTool().hasAttribute('hidden')).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: '도구 펼치기' }));
    expect(button('맵 재생성')).toBeTruthy();
  });

  it('편집 중에도 떼지 않고 숨기기만 하며, 끄면 다시 보인다', () => {
    renderPalette();
    act(() => startZoneEdit());
    expect(button('맵 재생성')).toBeNull();
    expect(hiddenTool().hasAttribute('hidden')).toBe(true);

    act(() => stopZoneEdit());
    expect(button('맵 재생성')).toBeTruthy();
  });
});

describe('ToolPalette 관리자 편집 모드', () => {
  it('누르면 편집 모드가 켜지고 맵 재생성은 사라진다', () => {
    renderPalette();
    expect(editButton().getAttribute('aria-pressed')).toBe('false');

    fireEvent.click(editButton());
    expect(useZoneEditModeStore.getState().editing).toBe(true);
    expect(editButton().getAttribute('aria-pressed')).toBe('true');
    expect(button('맵 재생성')).toBeNull();
  });

  it('편집 모드가 켜지면 편집 버튼의 아이콘이 연필에서 X 로 바뀌고, 끄면 되돌아온다', () => {
    renderPalette();
    expect(editButton().querySelector('svg.lucide-square-pen')).not.toBeNull();
    expect(editButton().querySelector('svg.lucide-x')).toBeNull();

    fireEvent.click(editButton());
    expect(editButton().querySelector('svg.lucide-x')).not.toBeNull();
    expect(editButton().querySelector('svg.lucide-square-pen')).toBeNull();

    fireEvent.click(editButton());
    expect(editButton().querySelector('svg.lucide-square-pen')).not.toBeNull();
  });

  it('고치던 초안이 없으면 다시 눌러 바로 끈다', () => {
    renderPalette();
    fireEvent.click(editButton());
    fireEvent.click(editButton());
    expect(useZoneEditModeStore.getState().editing).toBe(false);
    expect(editButton().getAttribute('aria-pressed')).toBe('false');
    expect(button('수정 버리고 끄기')).toBeNull();
  });

  it('저장하지 않은 초안이 있으면 끄기 전에 확인한다: 계속 편집이면 그대로, 버리고 끄면 초안을 지운다', async () => {
    renderPalette();
    fireEvent.click(editButton());
    addDraft();

    fireEvent.click(editButton());
    expect(useZoneEditModeStore.getState().editing).toBe(true);
    expect(await screen.findByRole('button', { name: '수정 버리고 끄기' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '계속 편집' }));
    expect(useZoneEditModeStore.getState().editing).toBe(true);
    expect(Object.keys(useZoneEditStore.getState().drafts)).toEqual(['z-1']);

    fireEvent.click(editButton());
    fireEvent.click(await screen.findByRole('button', { name: '수정 버리고 끄기' }));
    expect(useZoneEditModeStore.getState().editing).toBe(false);
    expect(useZoneEditStore.getState().drafts).toEqual({});
  });

  it('물어보는 중에 편집 버튼을 한 번 더 누르면 묻는 창이 닫히고 편집은 그대로다', async () => {
    renderPalette();
    fireEvent.click(editButton());
    addDraft();
    fireEvent.click(editButton());
    await screen.findByRole('button', { name: '수정 버리고 끄기' });

    fireEvent.click(editButton());
    await waitFor(() => expect(button('수정 버리고 끄기')).toBeNull());
    expect(useZoneEditModeStore.getState().editing).toBe(true);
  });

  it('초안이 있어도 편집 모드가 꺼져 있으면 확인 없이 켠다', () => {
    renderPalette();
    addDraft();
    fireEvent.click(editButton());
    expect(useZoneEditModeStore.getState().editing).toBe(true);
    expect(button('수정 버리고 끄기')).toBeNull();
  });
});

describe('ToolPalette 작업 관리 이동', () => {
  it('편집 중이고 세팅에서 구역을 발행한 뒤에만 보인다', () => {
    renderPalette();
    expect(button('작업 관리(TBM)로 이동')).toBeNull();

    act(() => startZoneEdit());
    expect(button('작업 관리(TBM)로 이동')).toBeNull();

    act(() => useSetupFlowStore.setState({ zonesCommitted: true }));
    expect(button('작업 관리(TBM)로 이동')).toBeTruthy();

    act(() => stopZoneEdit());
    expect(button('작업 관리(TBM)로 이동')).toBeNull();
  });

  it('작업이 정해져 있으면 그 작업으로, 아니면 작업 관리 목록으로 간다', () => {
    renderPalette();
    act(() => {
      startZoneEdit();
      useSetupFlowStore.setState({ zonesCommitted: true });
    });
    fireEvent.click(screen.getByRole('button', { name: '작업 관리(TBM)로 이동' }));
    expect(navigate).toHaveBeenLastCalledWith({ to: '/work', search: {} });

    act(() => setSetupWorkUnitId(7));
    fireEvent.click(screen.getByRole('button', { name: '작업 관리(TBM)로 이동' }));
    expect(navigate).toHaveBeenLastCalledWith({ to: '/work', search: { workUnitId: '7' } });
  });
});
