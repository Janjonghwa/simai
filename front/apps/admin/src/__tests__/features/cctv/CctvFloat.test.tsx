import { CctvFloat } from '@/features/cctv/CctvFloat';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { useCameraOrderStore } from '@/lib/cameraNames';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { CCTV_BOX_SETTLE_MS, useCctvFloatBoxStore } from '@/store/cctvFloatBox.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/env', () => ({ env: { VITE_CCTV_PREVIEW_URL: '', VITE_STOMP_URL: '' } }));
vi.mock('@/lib/api', () => ({ api: vi.fn() }));
vi.mock('@/features/cctv/useCctvPreview', () => ({ useCctvPreview: vi.fn() }));
const stopped = vi.fn();

beforeEach(() => {
  localStorage.clear();
  useSessionStore.setState({ session: { accessToken: 'token', user: { id: 'a', role: 'ADMIN' } } });
  useCctvStore.getState().setLink('open');
  useCctvStore.getState().hello(['cam_A', 'cam_B']);
  vi.mocked(useCctvPreview).mockImplementation(() => {
    useEffect(() => stopped, []);
    return { attach: vi.fn(), resume: vi.fn() };
  });
});
afterEach(() => {
  cleanup();
  stopped.mockClear();
  useCctvStore.getState().reset();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  useSessionStore.setState({ session: undefined });
});

// mock 바퀴(40초)의 0초라 카메라 4대가 정상이다.
const T = 1_760_342_400_000;
/** 지금 막 받은 health(카메라 4대, Tier 0 이어짐)와 world 를 넣는다. */
function seedHealth() {
  const now = Date.now();
  useRealtimeStore.setState({
    phase: 'OPEN',
    openedAt: now,
    health: {
      payload: { ...viewerHealthAt(T).payload, as_of: now, tier0_link: 'CONNECTED' },
      receivedAt: now,
      clockOffsetMs: 0,
    },
    world: { payload: { tracks: [] } as never, receivedAt: now },
  });
}

describe('떠 있는 CCTV', () => {
  it('도구 줄 없이 카메라 하나만 보이고 닫으면 연결을 정리한다', () => {
    const close = vi.fn();
    const view = render(<CctvFloat selectedKey={null} onClose={close} />);
    // 요청이 없으면 중계 목록의 첫 카메라 하나만 보인다. 4분할·카메라 버튼 줄은 없다.
    expect(screen.getByLabelText('카메라 1 영상')).toBeTruthy();
    expect(screen.queryByLabelText('카메라 2 영상')).toBeNull();
    for (const name of ['4분할', 'cam_A', 'cam_B']) {
      expect(screen.queryByRole('button', { name })).toBeNull();
    }
    fireEvent.click(screen.getByRole('button', { name: '떠 있는 CCTV 닫기' }));
    expect(close).toHaveBeenCalledOnce();
    view.unmount();
    expect(stopped).toHaveBeenCalledOnce();
  });

  it('고른 카메라가 없으면 중계 목록 순서가 아니라 번호 순서의 「카메라 1」을 보인다(-798)', () => {
    // 현장 배치 순서(세팅)가 중계 목록과 다르면 번호는 배치 순서를 따른다: cam_C 가 카메라 1 이다.
    useCameraOrderStore.getState().set(['cam_C', 'cam_B', 'cam_A']);
    useCctvStore.getState().hello(['cam_A', 'cam_B', 'cam_C']);
    try {
      render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
      expect(screen.getByLabelText('카메라 1 영상')).toBeTruthy();
      expect(screen.queryByLabelText('카메라 3 영상')).toBeNull();
    } finally {
      useCameraOrderStore.getState().reset();
    }
  });

  it('예전에 저장한 카메라·4분할 값은 쓰지 않는다', () => {
    localStorage.setItem('simai.cctv.float', JSON.stringify({ camera: 'cam_B', all: true }));
    render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    expect(screen.getByLabelText('카메라 1 영상')).toBeTruthy();
    expect(screen.queryByLabelText('카메라 2 영상')).toBeNull();
  });

  it('제목 줄·접기 없이 영상만 있고, 닫기는 영상 오른쪽 위 X 아이콘이다(-798)', () => {
    const close = vi.fn();
    render(<CctvFloat selectedKey={null} onClose={close} />);
    const box = screen.getByLabelText('떠 있는 CCTV');
    expect(box.textContent).not.toContain('CCTV ⋮⋮');
    for (const text of ['접기', '펼치기', '닫기', '전체 보기']) {
      expect(within(box).queryByText(text)).toBeNull();
    }
    expect(screen.queryByRole('button', { name: /CCTV (접기|펼치기)/ })).toBeNull();
    // 창 테두리(ring)·안쪽 여백 없이 둥근 모서리와 그림자만 둔다.
    expect(box.className).not.toMatch(/\bring-1\b/);
    expect(box.className).toContain('rounded-xl');
    expect(box.className).toContain('shadow-float');
    // 닫기 버튼은 글자 없이 아이콘이고, 이름은 그대로다.
    const closeButton = screen.getByRole('button', { name: '떠 있는 CCTV 닫기' });
    expect(closeButton.textContent).toBe('');
    expect(closeButton.querySelector('svg')).not.toBeNull();
    fireEvent.click(closeButton);
    expect(close).toHaveBeenCalledOnce();
  });

  it('저장한 위치와 영상 폭을 복원하고, 높이는 저장하지 않고 영상 비율을 따른다(-798)', () => {
    localStorage.setItem(
      'simai.cctv.float',
      JSON.stringify({ spot: { left: 120, offset: 140 }, videoWidth: 640 }),
    );
    render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    const box = screen.getByLabelText('떠 있는 CCTV');
    expect(box.style.left).toBe('120px');
    expect(box.style.top).toBe('140px');
    expect(box.style.width).toBe('640px');
    expect(box.style.height).toBe('');
    expect(box.style.resize).toBe('horizontal');
    // 영상 자리는 해상도를 모르면 16:9, 알면 그 비율이다.
    const frame = screen.getByLabelText('카메라 1 영상').closest<HTMLElement>('.relative.w-full');
    expect(frame?.style.aspectRatio).toBe('16 / 9');
    act(() => useCctvStore.getState().codec('cam_A', 1280, 960));
    expect(frame?.style.aspectRatio).toBe('1280 / 960');
  });

  it('예전 창 크기(width·height, 제목 줄 포함)는 읽지 않고 기본 폭으로 열며, 위치는 그대로 쓴다(-798)', () => {
    localStorage.setItem(
      'simai.cctv.float',
      JSON.stringify({ spot: { left: 120, offset: 140 }, width: 520, height: 400 }),
    );
    render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    const box = screen.getByLabelText('떠 있는 CCTV');
    expect(box.style.left).toBe('120px');
    expect(box.style.width).toBe('560px');
    expect(box.style.height).toBe('');
  });

  describe('영상을 끌어 창 옮기기(-798)', () => {
    // jsdom 은 배치를 하지 않아 창 자리와 장면 크기를 심는다(창 96,128 · 480×270, 장면 1440×900).
    let spies: { mockRestore: () => void }[] = [];
    beforeEach(() => {
      const scene = document.createElement('div');
      Object.defineProperties(scene, {
        clientWidth: { get: () => 1440 },
        clientHeight: { get: () => 900 },
      });
      spies = [
        vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockReturnValue(scene),
        vi.spyOn(HTMLElement.prototype, 'offsetLeft', 'get').mockReturnValue(96),
        vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockReturnValue(128),
        vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(480),
        vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(270),
      ];
    });
    afterEach(() => spies.forEach((spy) => spy.mockRestore()));
    const at = (x: number, y: number) => ({ clientX: x, clientY: y, button: 0, buttons: 1 });
    const drag = (element: Element, dx: number, dy: number) => {
      fireEvent.pointerDown(element, at(300, 200));
      fireEvent.pointerMove(element, at(300 + dx, 200 + dy));
      fireEvent.pointerUp(element, at(300 + dx, 200 + dy));
    };
    const surface = () => screen.getByRole('group', { name: 'CCTV 영상, 끌어서 창 옮기기' });

    it('영상 위에서 끌면 창이 따라 움직이고, 끌기 자리는 영상 전체이며 커서는 손 모양이다', () => {
      render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
      const box = screen.getByLabelText('떠 있는 CCTV');
      expect(surface().className).toContain('cursor-grab');
      expect(surface().contains(screen.getByLabelText('카메라 1 영상'))).toBe(true);
      drag(screen.getByLabelText('카메라 1 영상'), 100, 40);
      expect(box.style.left).toBe('196px');
      expect(box.style.top).toBe('168px');
    });

    it('휠로 확대한 동안의 끌기는 영상 안 옮기기라 창은 그대로다', () => {
      render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
      const box = screen.getByLabelText('떠 있는 CCTV');
      const canvas = screen.getByLabelText<HTMLCanvasElement>('카메라 1 영상');
      const tile = canvas.parentElement!;
      vi.spyOn(tile, 'getBoundingClientRect').mockReturnValue({
        left: 0,
        top: 0,
        width: 480,
        height: 270,
      } as DOMRect);
      fireEvent.wheel(tile, { deltaY: -500, clientX: 240, clientY: 135 });
      const zoomed = canvas.style.transform;
      expect(zoomed).not.toBe('');
      drag(canvas, 100, 40);
      expect(box.style.left).toBe('');
      expect(canvas.style.transform).not.toBe(zoomed);
    });

    it('오른쪽 위 버튼을 누른 채 움직여도 창이 끌리지 않고, 누르기는 그대로 듣는다', () => {
      const close = vi.fn();
      const full = vi.fn();
      render(<CctvFloat selectedKey={null} onClose={close} onFullView={full} />);
      const box = screen.getByLabelText('떠 있는 CCTV');
      for (const name of ['떠 있는 CCTV 닫기', '3D 전체 보기']) {
        const button = screen.getByRole('button', { name });
        expect(surface().contains(button)).toBe(false);
        drag(button, 100, 40);
        expect(box.style.left).toBe('');
      }
      fireEvent.click(screen.getByRole('button', { name: '3D 전체 보기' }), { detail: 1 });
      expect(full).toHaveBeenCalledOnce();
      fireEvent.click(screen.getByRole('button', { name: '떠 있는 CCTV 닫기' }), { detail: 1 });
      expect(close).toHaveBeenCalledOnce();
    });
  });

  it('판정 지연 배지는 닫기 버튼 자리(오른쪽 위)를 비우고 왼쪽 아래에 둔다(-798)', () => {
    seedHealth();
    useRealtimeStore.setState({ lagSamples: [{ at: T, lagMs: 150, cameras: { cam_A: 180 } }] });
    render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    const badge = screen.getByLabelText('카메라 1 판정 지연 180 ms');
    expect(badge.className).toContain('bottom-2 left-2');
    expect(badge.className).not.toContain('right-2');
  });

  it('중계 연결 상태는 영상 위에 겹쳐 보인다(-798)', () => {
    // 중계가 끊기면 카메라 목록은 Tier 0 health 에서 온다.
    seedHealth();
    render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    act(() => useCctvStore.getState().setLink('unavailable'));
    const surface = screen.getByRole('group', { name: 'CCTV 영상, 끌어서 창 옮기기' });
    expect(within(surface).getByRole('status').textContent).toBe('중계 끊김');
    expect(screen.getByLabelText('카메라 1 영상')).toBeTruthy();
  });

  it('원격 시청 멈춤 안내와 다시 보기 버튼도 영상 위에 겹치고, 다시 보기를 누르면 이어 본다(-798)', () => {
    const resume = vi.fn();
    vi.mocked(useCctvPreview).mockImplementation(() => ({ attach: vi.fn(), resume }));
    act(() => {
      useCctvStore.getState().setMode('remote');
      useCctvStore.getState().setPause('idle');
    });
    render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    const surface = screen.getByRole('group', { name: 'CCTV 영상, 끌어서 창 옮기기' });
    expect(within(surface).getByText('10분 동안 입력이 없어 원격 시청을 멈췄습니다')).toBeTruthy();
    fireEvent.click(within(surface).getByRole('button', { name: '다시 보기' }));
    expect(resume).toHaveBeenCalledOnce();
  });

  it('보일 카메라가 없으면 영상 자리에 안내를 겹쳐 보인다', () => {
    useSessionStore.setState({
      session: { accessToken: 'token', user: { id: 'v', role: 'VIEWER' } },
    });
    render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    const surface = screen.getByRole('group', { name: 'CCTV 영상, 끌어서 창 옮기기' });
    expect(within(surface).getByRole('status').textContent).toBe('카메라 연결 정보가 없습니다');
    expect(screen.getByRole('button', { name: '떠 있는 CCTV 닫기' })).toBeTruthy();
  });

  it('선택한 사람의 카메라를 연다', () => {
    useRealtimeStore.setState({
      world: {
        payload: { tracks: [{ world_id: 'p1', cameras: ['cam_B'] }] } as never,
        receivedAt: Date.now(),
      },
    });
    render(<CctvFloat selectedKey="person:p1" onClose={vi.fn()} />);
    expect(screen.getByLabelText('카메라 2 영상')).toBeTruthy();
  });

  it('사람을 고른 채 카메라 마커로 열면 마커의 카메라를 보이고, 그 뒤 사람 선택이 바뀌면 그 사람의 카메라로 바뀐다', () => {
    useRealtimeStore.setState({
      world: {
        payload: {
          tracks: [
            { world_id: 'p1', cameras: ['cam_B'] },
            { world_id: 'p2', cameras: ['cam_B'] },
          ],
        } as never,
        receivedAt: Date.now(),
      },
    });
    const request = { cameraId: 'cam_A', seq: 1 };
    const view = render(
      <CctvFloat selectedKey="person:p1" requestedCamera={request} onClose={vi.fn()} />,
    );
    expect(screen.getByLabelText('카메라 1 영상')).toBeTruthy();
    expect(screen.queryByLabelText('카메라 2 영상')).toBeNull();

    view.rerender(
      <CctvFloat selectedKey="person:p2" requestedCamera={request} onClose={vi.fn()} />,
    );
    expect(screen.getByLabelText('카메라 2 영상')).toBeTruthy();
    expect(screen.queryByLabelText('카메라 1 영상')).toBeNull();
  });

  it('고른 사람 ID 가 병합으로 다른 트랙의 별칭이 되면 그 생존 트랙의 카메라로 다시 고른다(-661)', () => {
    const at = Date.now();
    useRealtimeStore.setState({
      world: {
        payload: { tracks: [{ world_id: 'p2', cameras: ['cam_A'] }] } as never,
        receivedAt: at,
      },
    });
    render(<CctvFloat selectedKey="person:p2" onClose={vi.fn()} />);
    expect(screen.getByLabelText('카메라 1 영상')).toBeTruthy();
    act(() =>
      useRealtimeStore.setState({
        world: {
          payload: { tracks: [{ world_id: 'p1', aliases: ['p2'], cameras: ['cam_B'] }] } as never,
          receivedAt: at + 100,
        },
      }),
    );
    expect(screen.getByLabelText('카메라 2 영상')).toBeTruthy();
  });

  it('연 창의 자리를 이벤트 패널에 알리고, 창이 내려가면 비운다', () => {
    // jsdom 은 배치를 하지 않아 창 자리를 심는다(자리 값은 시험용이다. 실제 기본 자리는 왼쪽 칼럼 오른쪽, -798).
    const scene = document.createElement('div');
    const spies = [
      vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockReturnValue(scene),
      vi.spyOn(HTMLElement.prototype, 'offsetLeft', 'get').mockReturnValue(96),
      vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockReturnValue(128),
      vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(480),
      vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(360),
    ];
    vi.useFakeTimers();
    try {
      const view = render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
      act(() => vi.advanceTimersByTime(CCTV_BOX_SETTLE_MS));
      expect(useCctvFloatBoxStore.getState().box).toEqual({
        left: 96,
        top: 128,
        width: 480,
        height: 360,
      });
      view.unmount();
      expect(useCctvFloatBoxStore.getState().box).toBeNull();
    } finally {
      vi.useRealTimers();
      spies.forEach((spy) => spy.mockRestore());
    }
  });

  it('3D 카메라 마커 클릭 요청(requestedCamera)으로 열리거나 이미 열린 창의 카메라를 바꾼다', () => {
    const view = render(
      <CctvFloat
        selectedKey={null}
        requestedCamera={{ cameraId: 'cam_B', seq: 1 }}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('카메라 2 영상')).toBeTruthy();
    expect(screen.queryByLabelText('카메라 1 영상')).toBeNull();

    // 이미 열린 창에 다른 카메라 마커 클릭 요청이 오면 그 카메라 하나로 바뀐다
    view.rerender(
      <CctvFloat
        selectedKey={null}
        requestedCamera={{ cameraId: 'cam_A', seq: 2 }}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('카메라 1 영상')).toBeTruthy();
    expect(screen.queryByLabelText('카메라 2 영상')).toBeNull();
  });

  it('「3D 에서 이 방향으로 보기」는 넘겼을 때만 두고, 보이는 카메라로 부르며, 막히면 끄고 이유를 보인다(-626)', () => {
    const view = render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: '3D 에서 이 방향으로 보기' })).toBeNull();
    view.unmount();

    const onView = vi.fn();
    const block = vi.fn((id: string) => (id === 'cam_B' ? '보정 정보가 없습니다' : null));
    const opened = render(
      <CctvFloat
        selectedKey={null}
        requestedCamera={{ cameraId: 'cam_A', seq: 1 }}
        onClose={vi.fn()}
        onViewFromCamera={onView}
        viewFromCameraBlock={block}
      />,
    );
    const button = screen.getByRole('button', { name: '3D 에서 이 방향으로 보기' });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);
    expect(onView).toHaveBeenCalledExactlyOnceWith('cam_A');

    opened.rerender(
      <CctvFloat
        selectedKey={null}
        requestedCamera={{ cameraId: 'cam_B', seq: 2 }}
        onClose={vi.fn()}
        onViewFromCamera={onView}
        viewFromCameraBlock={block}
      />,
    );
    const blocked = screen.getByRole('button', { name: '3D 에서 이 방향으로 보기' });
    expect((blocked as HTMLButtonElement).disabled).toBe(true);
    const reasonId = blocked.getAttribute('aria-describedby');
    expect(reasonId && document.getElementById(reasonId)?.textContent).toBe('보정 정보가 없습니다');
    fireEvent.click(blocked);
    expect(onView).toHaveBeenCalledOnce();
  });
});
