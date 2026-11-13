import { useCctvStore } from '@/features/cctv/cctv.store';
import { CctvPage } from '@/features/cctv/CctvPage';
import {
  IDENTITY_VIEW,
  panBy,
  panZoomTransform,
  visibleRect,
  wheelFactor,
  zoomAt,
} from '@/features/cctv/panZoom';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { DRAG_THRESHOLD_PX } from '@/hooks/useFloatSpot';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 현장 노트북 중계(WebSocket·WebCodecs)는 붙이지 않는다. 연결·카메라 단계는 cctv.store 에 직접 넣는다.
vi.mock('@/lib/env', () => ({
  env: { VITE_CCTV_PREVIEW_URL: 'ws://127.0.0.1:47032', VITE_STOMP_URL: 'wss://simai.live/ws' },
}));
const resume = vi.fn();
vi.mock('@/features/cctv/useCctvPreview', () => ({
  useCctvPreview: vi.fn(() => ({ attach: vi.fn(), resume })),
}));

// T 는 mock 바퀴(40초)의 0초라 카메라 4대가 정상이다. T + 20초부터 6초 동안 cam3 가 키프레임을 기다린다.
const T = 1_760_342_400_000;
const CAMS = ['cam1', 'cam2', 'cam3', 'cam4'];

const cameraName = (id: string) => `카메라 ${id.slice(-1)}`;

function signIn(role: Role) {
  useSessionStore.setState({ session: { accessToken: 'token', user: { id: role, role } } });
}

/** 지금 막 받은 health(카메라 4대)와 world 를 넣는다. 둘 다 최근이라 EC2 와 이어져 있고 World 도 들어온다. */
function seedRealtime({
  at = T,
  tier0 = 'CONNECTED' as 'CONNECTED' | 'DISCONNECTED',
  world = true,
} = {}) {
  const now = Date.now();
  useRealtimeStore.setState({
    phase: 'OPEN',
    openedAt: now,
    health: {
      payload: { ...viewerHealthAt(at).payload, as_of: now, tier0_link: tier0 },
      receivedAt: now,
      clockOffsetMs: 0,
    },
    world: world ? { payload: { tracks: [] } as never, receivedAt: now } : null,
  });
}

/** 중계가 열려 카메라 4대를 알렸고 cam1 만 영상을 그리는 중이다. */
function openRelay() {
  const store = useCctvStore.getState();
  store.setLink('open');
  store.hello(CAMS);
  store.codec('cam1', 2304, 1296);
  store.played('cam1', Date.now(), Date.now());
}

const badge = (id: string) =>
  ['실시간', '끊김', '정지']
    .map((state) => screen.queryByLabelText(`${cameraName(id)} 상태 ${state}`))
    .find(Boolean)!;

beforeEach(() => {
  useCctvStore.getState().reset();
  signIn('ADMIN');
});

afterEach(() => {
  cleanup();
  vi.mocked(useCctvPreview).mockClear();
  resume.mockClear();
  useCctvStore.getState().reset();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  useSessionStore.setState({ session: undefined });
});

describe('CctvPage 관리자 · 중계가 이어졌을 때', () => {
  beforeEach(() => {
    seedRealtime();
    openRelay();
  });

  it('카메라 4대를 4분할로 그리고 칸마다 이름과 상태를 보인다', () => {
    render(<CctvPage />);
    for (const id of CAMS) {
      expect(screen.getByRole('button', { name: `${cameraName(id)} 크게 보기` })).toBeTruthy();
      expect(screen.getByLabelText(`${cameraName(id)} 영상`).tagName).toBe('CANVAS');
      expect(screen.getByText(cameraName(id))).toBeTruthy();
    }
    // 이름 옆에 단계 글('표시 중')은 두지 않는다.
    expect(screen.queryByText('표시 중')).toBeNull();
    expect(screen.queryByRole('button', { name: '4분할로 돌아가기' })).toBeNull();
  });

  it('LIVE 점은 프레임이 들어오는 칸에만 있고 나머지는 상태 글만 보인다', () => {
    render(<CctvPage />);
    expect(badge('cam1').textContent).toBe('LIVE');
    expect(badge('cam1').querySelector('i')).not.toBeNull();
    // cam2 는 health 가 정상이지만 영상 프레임이 아직 오지 않는다.
    expect(badge('cam2').textContent).toBe('실시간');
    expect(badge('cam2').querySelector('i')).toBeNull();
  });

  it('키프레임을 기다리는 카메라는 끊김이다', () => {
    seedRealtime({ at: T + 20_000 });
    render(<CctvPage />);
    expect(badge('cam3').textContent).toBe('끊김');
    expect(badge('cam1').textContent).toBe('LIVE');
  });

  it('World 가 멈추면(정지) 배지를 두지 않는다(S15P31A101-665)', () => {
    seedRealtime({ world: false });
    render(<CctvPage />);
    expect(badge('cam1')).toBeUndefined();
    expect(screen.queryByText(/정지/)).toBeNull();
  });

  it('Tier 0 가 잰 카메라 지연을 알면 실시간 배지에 ms 로 함께 보이고, 기준을 넘으면 지연 색이다', () => {
    useRealtimeStore.setState({
      lagSamples: [{ at: T, lagMs: 150, cameras: { cam1: 180, cam2: 720 } }],
    });
    render(<CctvPage />);
    expect(badge('cam1').textContent).toBe('LIVE· 180 ms');
    expect(badge('cam2').textContent).toBe('실시간· 720 ms');
    expect(within(badge('cam2')).getByText('· 720 ms').className).toContain('text-delay');
    // 지연을 모르는 칸은 상태 글만 보인다.
    expect(badge('cam3').textContent).toBe('실시간');
  });

  it('프레임이 1초 넘게 끊기면 LIVE 점을 끈다', () => {
    const now = Date.now();
    useCctvStore.getState().played('cam1', now, now - 2_000);
    render(<CctvPage />);
    expect(badge('cam1').textContent).toBe('실시간');
  });

  it('촬영 시각이 2초 늦어도 방금 받은 프레임이면 LIVE 이고 칸에는 지연 초가 보이지 않는다', () => {
    const now = Date.now();
    useCctvStore.getState().played('cam1', now - 2_000, now);
    render(<CctvPage />);
    expect(badge('cam1').textContent).toBe('LIVE');
    expect(screen.queryByText(/^\d+\.\d초$/)).toBeNull();
  });

  it('프레임을 받은 뒤 0.9초면 LIVE 이고 1.3초면 실시간이다', () => {
    vi.useFakeTimers();
    try {
      const now = Date.now();
      useCctvStore.getState().played('cam1', now, now);
      render(<CctvPage />);
      act(() => {
        vi.advanceTimersByTime(900);
      });
      expect(badge('cam1').textContent).toBe('LIVE');
      act(() => {
        // 다시 재는 간격(0.25초)이 있어, 기준(1초)을 한 번 이상 지난 뒤 잰 값이 나오도록 넉넉히 넘긴다.
        vi.advanceTimersByTime(400);
      });
      expect(badge('cam1').textContent).toBe('실시간');
    } finally {
      vi.useRealTimers();
    }
  });

  it('칸을 누르면 그 칸을 크게 보고 같은 칸을 다시 누르면 4분할로 돌아간다', () => {
    render(<CctvPage />);
    const canvas = screen.getByLabelText('카메라 2 영상');
    fireEvent.click(screen.getByRole('button', { name: '카메라 2 크게 보기' }));

    expect(screen.getByRole('button', { name: '카메라 2 작게 보기' })).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /크게 보기$/ })).toHaveLength(3);
    // 캔버스는 다시 만들어지지 않는다(영상이 끊기지 않는다).
    expect(screen.getByLabelText('카메라 2 영상')).toBe(canvas);

    fireEvent.click(screen.getByRole('button', { name: '카메라 2 작게 보기' }));
    expect(screen.getAllByRole('button', { name: /크게 보기$/ })).toHaveLength(4);
    expect(screen.getByLabelText('카메라 2 영상')).toBe(canvas);
  });

  it('크게 본 화면에서도 상태와 4분할로 버튼이 있다', () => {
    render(<CctvPage />);
    fireEvent.click(screen.getByRole('button', { name: '카메라 1 크게 보기' }));
    expect(badge('cam1').textContent).toBe('LIVE');
    fireEvent.click(screen.getByRole('button', { name: '4분할로 돌아가기' }));
    expect(screen.getAllByRole('button', { name: /크게 보기$/ })).toHaveLength(4);
  });

  it('중계가 카메라를 아직 알리지 않았으면 연결 문구만 보인다', () => {
    act(() => useCctvStore.setState({ cameras: [], views: {} }));
    render(<CctvPage />);
    expect(screen.queryAllByRole('button', { name: /크게 보기$/ })).toHaveLength(0);
  });
});

// jsdom 은 칸 크기를 계산하지 않아 800×450 으로 흉내 낸다. 칸 가운데는 (400, 225) 다.
const TILE_BOX = { left: 0, top: 0, width: 800, height: 450 };
const CENTER = { x: 0.5, y: 0.5 };
const at = (x: number, y: number) => ({ clientX: x, clientY: y, button: 0, buttons: 1 });

describe('CctvPage 관리자 · 확대·이동', () => {
  beforeEach(() => {
    seedRealtime();
    openRelay();
  });

  function sizedTile(id: string) {
    const tile = screen.getByRole('button', {
      name: new RegExp(`^${cameraName(id)} (크게|작게) 보기$`),
    });
    vi.spyOn(tile, 'getBoundingClientRect').mockReturnValue(TILE_BOX as DOMRect);
    return tile;
  }
  const canvasOf = (id: string) =>
    screen.getByLabelText<HTMLCanvasElement>(`${cameraName(id)} 영상`);
  const wheelUp = (tile: HTMLElement, deltaY = -500) =>
    fireEvent.wheel(tile, { deltaY, clientX: 400, clientY: 225 });
  const dragTo = (tile: HTMLElement, x: number, y: number) => {
    fireEvent.pointerDown(tile, at(400, 225));
    fireEvent.pointerMove(tile, at(x, y));
    fireEvent.pointerUp(tile, at(x, y));
  };
  const zoomedView = zoomAt(IDENTITY_VIEW, CENTER, wheelFactor(-500, 0));

  it('휠을 위로 굴리면 영상이 포인터 밑을 기준으로 확대되고 작은 지도가 뜬다', () => {
    render(<CctvPage />);
    const tile = sizedTile('cam2');
    expect(canvasOf('cam2').style.transform).toBe('');
    expect(screen.queryByRole('img', { name: '확대한 자리' })).toBeNull();

    wheelUp(tile);

    expect(canvasOf('cam2').style.transform).toBe(panZoomTransform(zoomedView));
    const area = screen.getByRole('img', { name: '확대한 자리' }).firstElementChild as HTMLElement;
    const rect = visibleRect(zoomedView);
    expect(parseFloat(area.style.left)).toBeCloseTo(rect.left * 100, 3);
    expect(parseFloat(area.style.width)).toBeCloseTo(rect.width * 100, 3);
    // 다른 칸은 그대로다.
    expect(canvasOf('cam1').style.transform).toBe('');
  });

  it('휠은 페이지 스크롤을 막고, 아래로 굴려 1배까지 줄이면 확대가 풀린다', () => {
    render(<CctvPage />);
    const tile = sizedTile('cam2');
    expect(wheelUp(tile)).toBe(false);
    fireEvent.wheel(tile, { deltaY: 5000, clientX: 400, clientY: 225 });
    expect(canvasOf('cam2').style.transform).toBe('');
    expect(screen.queryByRole('img', { name: '확대한 자리' })).toBeNull();
  });

  it('확대하기 전에는 끌어도 옮겨지지 않고 누르면 그대로 크게 보기로 바뀐다', () => {
    render(<CctvPage />);
    const tile = sizedTile('cam2');
    dragTo(tile, 700, 225);
    expect(canvasOf('cam2').style.transform).toBe('');
    fireEvent.click(tile, { detail: 1 });
    expect(screen.getByRole('button', { name: '카메라 2 작게 보기' })).toBeTruthy();
  });

  it(`확대 중 ${DRAG_THRESHOLD_PX}px 넘게 끌면 옮겨지고, 놓은 뒤 이어지는 클릭은 크게 보기를 바꾸지 않는다`, () => {
    render(<CctvPage />);
    const tile = sizedTile('cam2');
    wheelUp(tile);

    dragTo(tile, 500, 225);

    // 오른쪽으로 100px(칸 폭의 1/8) 끌면 그림이 그만큼 따라온다.
    expect(canvasOf('cam2').style.transform).toBe(panZoomTransform(panBy(zoomedView, 0.125, 0)));
    fireEvent.click(tile, { detail: 1 });
    expect(screen.getByRole('button', { name: '카메라 2 크게 보기' })).toBe(tile);
    // 삼킨 것은 그 한 번뿐이라 다음 클릭은 크게 보기로 바뀐다.
    fireEvent.click(tile, { detail: 1 });
    expect(screen.getByRole('button', { name: '카메라 2 작게 보기' })).toBe(tile);
  });

  it(`확대 중 ${DRAG_THRESHOLD_PX}px 이하로 움직인 누르기는 클릭이라 옮겨지지 않는다`, () => {
    render(<CctvPage />);
    const tile = sizedTile('cam2');
    wheelUp(tile);
    const before = canvasOf('cam2').style.transform;

    dragTo(tile, 400 + DRAG_THRESHOLD_PX, 225);
    fireEvent.click(tile, { detail: 1 });

    expect(canvasOf('cam2').style.transform).toBe(before);
    expect(screen.getByRole('button', { name: '카메라 2 작게 보기' })).toBe(tile);
  });

  it('확대한 채 크게 보기와 4분할을 오가도 같은 캔버스에 같은 자리가 남는다', () => {
    render(<CctvPage />);
    const tile = sizedTile('cam2');
    const canvas = canvasOf('cam2');
    wheelUp(tile);
    const zoomed = canvas.style.transform;

    fireEvent.click(tile);
    expect(screen.getByRole('button', { name: '카메라 2 작게 보기' })).toBe(tile);
    expect(canvasOf('cam2')).toBe(canvas);
    expect(canvas.style.transform).toBe(zoomed);

    fireEvent.click(tile);
    expect(canvas.style.transform).toBe(zoomed);
  });

  it('+ 는 확대하고 0 은 1배로 되돌린다. 브라우저 확대 키(Ctrl)는 건드리지 않는다', () => {
    render(<CctvPage />);
    const tile = sizedTile('cam2');
    fireEvent.keyDown(tile, { key: '+' });
    expect(canvasOf('cam2').style.transform).not.toBe('');

    expect(fireEvent.keyDown(tile, { key: '0', ctrlKey: true })).toBe(true);
    expect(canvasOf('cam2').style.transform).not.toBe('');

    expect(fireEvent.keyDown(tile, { key: '0' })).toBe(false);
    expect(canvasOf('cam2').style.transform).toBe('');
  });
});

describe('CctvPage 관리자 · 중계가 이어지지 않았을 때', () => {
  it('연결 상태 한 줄과 모의 화면을 보인다(모의 화면도 크게 볼 수 있다)', () => {
    seedRealtime();
    useCctvStore.getState().setLink('unavailable');
    render(<CctvPage />);

    expect(screen.getByRole('status').textContent).toBe('중계 끊김');
    expect(screen.getByRole('img', { name: '카메라 1 영상' })).toBeTruthy();
    expect(document.querySelector('canvas')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '카메라 3 크게 보기' }));
    expect(screen.getByRole('button', { name: '카메라 3 작게 보기' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '4분할로 돌아가기' })).toBeTruthy();
  });
});

describe('CctvPage 관리자 · 원격 시청(S15P31A101-400)', () => {
  it('입력이 없어 멈추면 안내와 다시 보기를 보이고 누르면 다시 붙는다', () => {
    seedRealtime();
    const store = useCctvStore.getState();
    store.setMode('remote');
    store.setPause('idle');
    store.setLink('stopped');
    render(<CctvPage />);
    expect(screen.getByLabelText('원격 시청 안내').textContent).toContain('10분 동안 입력이 없어');
    fireEvent.click(screen.getByRole('button', { name: '다시 보기' }));
    expect(resume).toHaveBeenCalledOnce();
  });

  it('동시 시청 상한·월 상한이면 그 안내를, 게이트웨이 대기면 다시 보기 없이 안내만 보인다', () => {
    seedRealtime();
    const store = useCctvStore.getState();
    store.setMode('remote');
    store.status({ kind: 'status', protocol: 'simai-cctv-preview/1', state: 'viewer_limit' });
    const { unmount } = render(<CctvPage />);
    expect(screen.getByLabelText('원격 시청 안내').textContent).toContain('동시 시청 인원');
    unmount();

    act(() =>
      store.status({ kind: 'status', protocol: 'simai-cctv-preview/1', state: 'monthly_quota' }),
    );
    render(<CctvPage />);
    expect(screen.getByLabelText('원격 시청 안내').textContent).toContain('송출 한도');
    cleanup();

    act(() =>
      store.status({ kind: 'status', protocol: 'simai-cctv-preview/1', state: 'waiting_source' }),
    );
    render(<CctvPage />);
    expect(screen.getByLabelText('원격 시청 안내').textContent).toContain(
      '서버에 연결되지 않았습니다',
    );
    expect(screen.queryByRole('button', { name: '다시 보기' })).toBeNull();
  });
});

describe.each(['FOREMAN', 'VIEWER'] as const)('CctvPage %s', (role) => {
  beforeEach(() => signIn(role));

  it('영상 없이 카메라 상태만 보이고 중계에 붙지 않는다', () => {
    seedRealtime({ at: T + 20_000 });
    render(<CctvPage />);

    expect(vi.mocked(useCctvPreview)).not.toHaveBeenCalled();
    expect(document.querySelector('canvas')).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.queryAllByRole('button')).toHaveLength(0);

    const text = (id: string) =>
      screen.getByRole('group', { name: `${cameraName(id)} 상태` }).lastElementChild?.textContent;
    expect(text('cam1')).toBe('실시간');
    expect(text('cam3')).toBe('끊김');
    expect(screen.getAllByRole('group')).toHaveLength(4);
  });

  it('World 가 멈추면(정지) 상태 글을 두지 않는다', () => {
    seedRealtime({ world: false });
    render(<CctvPage />);
    const group = screen.getByRole('group', { name: '카메라 1 상태' });
    expect(group.textContent).toBe('카메라 1');
  });

  it('실시간 칸은 Tier 0 가 잰 카메라 지연(ms)을 함께 보인다', () => {
    seedRealtime({ at: T + 20_000 });
    useRealtimeStore.setState({
      lagSamples: [{ at: T, lagMs: 150, cameras: { cam1: 180, cam3: 400 } }],
    });
    render(<CctvPage />);
    const text = (id: string) =>
      screen.getByRole('group', { name: `${cameraName(id)} 상태` }).lastElementChild?.textContent;
    expect(text('cam1')).toBe('실시간 · 180 ms');
    // 끊김(cam3 키프레임 대기)에는 붙이지 않는다.
    expect(text('cam3')).toBe('끊김');
  });

  it('Tier 0 상태를 받지 못하면 안내 한 줄만 보인다', () => {
    seedRealtime({ tier0: 'DISCONNECTED' });
    render(<CctvPage />);
    expect(screen.getByRole('status').textContent).toBe('카메라 상태를 받지 못했습니다');
    expect(screen.queryAllByRole('group')).toHaveLength(0);
  });
});
