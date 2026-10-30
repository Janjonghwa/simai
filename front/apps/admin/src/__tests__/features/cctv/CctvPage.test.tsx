import { useCctvStore } from '@/features/cctv/cctv.store';
import { CctvPage } from '@/features/cctv/CctvPage';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { viewerHealthAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 현장 노트북 중계(WebSocket·WebCodecs)는 붙이지 않는다. 연결·카메라 단계는 cctv.store 에 직접 넣는다.
vi.mock('@/lib/env', () => ({ env: { VITE_CCTV_PREVIEW_URL: 'ws://127.0.0.1:47032' } }));
vi.mock('@/features/cctv/useCctvPreview', () => ({ useCctvPreview: vi.fn(() => vi.fn()) }));

// T 는 mock 바퀴(40초)의 0초라 카메라 4대가 정상이다. T + 20초부터 6초 동안 cam3 가 키프레임을 기다린다.
const T = 1_760_342_400_000;
const CAMS = ['cam1', 'cam2', 'cam3', 'cam4'];

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
  store.played('cam1', Date.now());
}

const badge = (id: string) =>
  ['실시간', '끊김', '정지']
    .map((state) => screen.queryByLabelText(`${id} 상태 ${state}`))
    .find(Boolean)!;

beforeEach(() => {
  useCctvStore.getState().reset();
  signIn('ADMIN');
});

afterEach(() => {
  cleanup();
  vi.mocked(useCctvPreview).mockClear();
  useCctvStore.getState().reset();
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
  useSessionStore.setState({ session: undefined });
});

describe('CctvPage 관리자 · 중계가 이어졌을 때', () => {
  beforeEach(() => {
    seedRealtime();
    openRelay();
  });

  it('카메라 4대를 4분할로 그리고 칸마다 상태를 보인다', () => {
    render(<CctvPage />);
    for (const id of CAMS) {
      expect(screen.getByRole('button', { name: `${id} 크게 보기` })).toBeTruthy();
      expect(screen.getByLabelText(`${id} 영상`).tagName).toBe('CANVAS');
    }
    expect(screen.queryByRole('button', { name: '4분할로' })).toBeNull();
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

  it('World 가 멈추면 정지이고 LIVE 점이 꺼진다', () => {
    seedRealtime({ world: false });
    render(<CctvPage />);
    expect(badge('cam1').textContent).toMatch(/^정지/);
    expect(badge('cam1').querySelector('i')).toBeNull();
  });

  it('프레임이 1초 넘게 끊기면 LIVE 점을 끈다', () => {
    useCctvStore.getState().played('cam1', Date.now() - 2_000);
    render(<CctvPage />);
    expect(badge('cam1').textContent).toBe('실시간');
  });

  it('칸을 누르면 그 칸을 크게 보고 같은 칸을 다시 누르면 4분할로 돌아간다', () => {
    render(<CctvPage />);
    const canvas = screen.getByLabelText('cam2 영상');
    fireEvent.click(screen.getByRole('button', { name: 'cam2 크게 보기' }));

    expect(screen.getByRole('button', { name: 'cam2 작게 보기' })).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /크게 보기$/ })).toHaveLength(3);
    // 캔버스는 다시 만들어지지 않는다(영상이 끊기지 않는다).
    expect(screen.getByLabelText('cam2 영상')).toBe(canvas);

    fireEvent.click(screen.getByRole('button', { name: 'cam2 작게 보기' }));
    expect(screen.getAllByRole('button', { name: /크게 보기$/ })).toHaveLength(4);
    expect(screen.getByLabelText('cam2 영상')).toBe(canvas);
  });

  it('크게 본 화면에서도 상태와 4분할로 버튼이 있다', () => {
    render(<CctvPage />);
    fireEvent.click(screen.getByRole('button', { name: 'cam1 크게 보기' }));
    expect(badge('cam1').textContent).toBe('LIVE');
    fireEvent.click(screen.getByRole('button', { name: '4분할로' }));
    expect(screen.getAllByRole('button', { name: /크게 보기$/ })).toHaveLength(4);
  });

  it('중계가 카메라를 아직 알리지 않았으면 연결 문구만 보인다', () => {
    act(() => useCctvStore.setState({ cameras: [], views: {} }));
    render(<CctvPage />);
    expect(screen.queryAllByRole('button', { name: /크게 보기$/ })).toHaveLength(0);
  });
});

describe('CctvPage 관리자 · 중계가 이어지지 않았을 때', () => {
  it('연결 상태 한 줄과 모의 화면을 보인다(모의 화면도 크게 볼 수 있다)', () => {
    seedRealtime();
    useCctvStore.getState().setLink('unavailable');
    render(<CctvPage />);

    expect(screen.getByRole('status').textContent).toBe('중계 끊김');
    expect(screen.getByRole('img', { name: 'cam1 카메라 영상' })).toBeTruthy();
    expect(screen.queryByLabelText('cam1 영상')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'cam3 크게 보기' }));
    expect(screen.getByRole('button', { name: 'cam3 작게 보기' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '4분할로' })).toBeTruthy();
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
      screen.getByRole('group', { name: `${id} 상태` }).lastElementChild?.textContent;
    expect(text('cam1')).toBe('실시간');
    expect(text('cam3')).toBe('끊김');
    expect(screen.getAllByRole('group')).toHaveLength(4);
  });

  it('World 가 멈추면 정지로 보인다', () => {
    seedRealtime({ world: false });
    render(<CctvPage />);
    const group = screen.getByRole('group', { name: 'cam1 상태' });
    expect(group.textContent).toMatch(/정지/);
  });

  it('Tier 0 상태를 받지 못하면 안내 한 줄만 보인다', () => {
    seedRealtime({ tier0: 'DISCONNECTED' });
    render(<CctvPage />);
    expect(screen.getByRole('status').textContent).toBe('카메라 상태를 받지 못했습니다');
    expect(screen.queryAllByRole('group')).toHaveLength(0);
  });
});
