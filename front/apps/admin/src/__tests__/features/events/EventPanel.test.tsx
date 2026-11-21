import { EventDetail } from '@/features/events/EventDetail';
import { EventPanel } from '@/features/events/EventPanel';
import { groupIncidents, mergeEvent, type EventEntry } from '@/features/events/events';
import { PANEL_DEFAULT_STYLE } from '@/features/events/panelBox';
import type { EventItem } from '@/features/events/types';
import { BUNDLES_KEY, ZONES_KEY } from '@/features/zones/api';
import { EMPTY_TRACK_LIFE, noteWorldFrame } from '@/lib/trackLife';
import { MOCK_ZONES } from '@/mocks/zones';
import { useCctvFloatBoxStore } from '@/store/cctvFloatBox.store';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import type { WorldStatePayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => vi.fn(),
}));
vi.mock('@/lib/api', () => ({ api: () => Promise.reject(new Error('no api in test')) }));

function item(change: Partial<EventItem> = {}): EventItem {
  return {
    event_id: 'e-1',
    event_sequence: 1,
    rule_id: 'R2',
    source: 'LIVE',
    lifecycle: 'RAISED',
    verification: 'VERIFIED',
    severity: 'CRITICAL',
    confidence: 'HIGH',
    needs_check: false,
    world_ids: ['w-0015'],
    zone_id: 'z-04',
    metrics: { edge_distance_m: 0.82, height_diff_m: 2.1 },
    rationale: null,
    proposed_severity: null,
    raised_at: T,
    updated_at: T,
    ...change,
  };
}

const entry = (change: Partial<EventItem> = {}): EventEntry => mergeEvent(undefined, item(change));

function panelProps(
  entries: EventEntry[],
  props: Partial<ComponentProps<typeof EventPanel>> = {},
): ComponentProps<typeof EventPanel> {
  return {
    entries,
    loaded: true,
    failed: false,
    selectedKey: null,
    onSelect: vi.fn(),
    openEventId: null,
    onOpen: vi.fn(),
    ...props,
  };
}

function renderPanel(
  entries: EventEntry[],
  props: Partial<ComponentProps<typeof EventPanel>> = {},
) {
  const queryClient = new QueryClient();
  queryClient.setQueryData(ZONES_KEY, MOCK_ZONES);
  const tree = (next: ComponentProps<typeof EventPanel>) => (
    <QueryClientProvider client={queryClient}>
      <EventPanel {...next} />
    </QueryClientProvider>
  );
  const view = render(tree(panelProps(entries, props)));
  return {
    rerender: (
      nextEntries: EventEntry[],
      nextProps: Partial<ComponentProps<typeof EventPanel>> = {},
    ) => view.rerender(tree(panelProps(nextEntries, nextProps))),
  };
}

const fold = () => screen.getByRole('button', { name: /이벤트 (접기|펼치기)/ });

beforeEach(() => {
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
});
afterEach(() => {
  cleanup();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

const resolved = (change: Partial<EventItem> = {}) =>
  entry({ lifecycle: 'RESOLVED', resolved_at: T + 1_000, updated_at: T + 1_000, ...change });

/** 사람 트랙만 담은 world(추적 중 수 시험용). */
const setTracks = (lifecycles: string[]) =>
  useRealtimeStore.setState({
    world: {
      receivedAt: T,
      payload: {
        tracks: lifecycles.map((track_lifecycle, i) => ({
          world_id: `w-${i}`,
          track_lifecycle,
          cameras: [],
        })),
      } as unknown as WorldStatePayload,
    },
  });

describe('EventPanel 접기', () => {
  it('이벤트가 하나도 없으면 저절로 접히고, 이벤트가 생기면 펼쳐진다', () => {
    const view = renderPanel([]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('활성 이벤트 없음')).toBeNull();

    view.rerender([entry()]);
    expect(fold().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('article', { name: 'w-0015 이벤트' })).toBeTruthy();

    // 다시 0개가 되면 접힌다.
    view.rerender([]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');
  });

  it('진행 중 이벤트가 없어도 해소된 묶음이 남아 있으면 접지 않는다', () => {
    renderPanel([entry({ lifecycle: 'RESOLVED', resolved_at: T + 1_000 })]);
    expect(fold().getAttribute('aria-expanded')).toBe('true');
  });

  it('목록을 받는 중이거나 못 받았으면 접지 않는다(실패 문구를 가리지 않는다)', () => {
    renderPanel([], { loaded: false, failed: true });
    expect(fold().getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('alert').textContent).toBe('이벤트 목록을 받지 못했습니다');
  });

  it('사용자가 누른 접기·펼치기는 이벤트 수가 0 이 되거나 생기기 전까지 따른다', () => {
    const view = renderPanel([entry()]);
    fireEvent.click(fold());
    expect(fold().getAttribute('aria-expanded')).toBe('false');

    // 같은 상태에서 이벤트만 바뀌어도 사용자가 접은 채로 둔다.
    view.rerender([entry(), entry({ event_id: 'e-2', world_ids: ['w-0021'] })]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');

    fireEvent.click(fold());
    expect(fold().getAttribute('aria-expanded')).toBe('true');

    // 비었는데 사용자가 펼쳐 볼 수도 있다.
    view.rerender([]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(fold());
    expect(screen.getByText('활성 이벤트 없음')).toBeTruthy();
  });

  it('카드는 규칙 이름 한 줄이고 구역 이름·경과가 없다(-798)', () => {
    renderPanel([entry()]);
    const card = screen.getByRole('article', { name: 'w-0015 이벤트' });
    expect(within(card).getByText('추락 위험(가장자리)')).toBeTruthy();
    expect(card.textContent).not.toContain('개구부 구역');
    expect(card.textContent).not.toMatch(/\d+(초|분|시간|일)( \d+(초|분|시간))? 전/);
  });
});

describe('EventPanel 머리줄과 두 칸', () => {
  const head = () => screen.getByTestId('event-panel-head').textContent;
  const section = (name: string) => screen.getByRole('region', { name });
  const labels = (name: string) =>
    within(section(name))
      .getAllByRole('article')
      .map((a) => a.getAttribute('aria-label'));

  it('머리줄에 진행 중 카드 수를 보인다', () => {
    setTracks(['CONFIRMED', 'OCCLUDED', 'LOST', 'TENTATIVE']);
    renderPanel([
      entry({ event_id: 'a', world_ids: ['w-0'] }),
      entry({ event_id: 'b', world_ids: ['w-1'] }),
      resolved({ event_id: 'c', world_ids: ['w-2'] }),
    ]);
    expect(head()).toMatch(/이벤트진행 중 [12]건▾/g);
    for (const text of ['작업자', '해소 대기']) expect(head()).not.toContain(text);
    // 진행 중·최근 해소는 칸 제목으로 가른다.
    expect(labels('진행 중')).toEqual(['w-0 이벤트', 'w-1 이벤트']);
    expect(labels('최근 해소')).toEqual(['w-2 이벤트']);
  });

  it('대상 사람이 사라진 진행 중 카드는 위험 등급이라도 최근 해소 맨 앞으로 옮기고, 흐리게·점멸 없이 보인다(-798)', () => {
    setTracks(['CONFIRMED']);
    renderPanel([
      entry({ event_id: 'a', world_ids: ['w-0'] }),
      entry({ event_id: 'b', world_ids: ['w-9'], rule_id: 'R3', zone_id: 'z-05' }),
      resolved({ event_id: 'r', world_ids: ['w-0021'], rule_id: 'R1', severity: 'HIGH' }),
    ]);
    expect(labels('진행 중')).toEqual(['w-0 이벤트']);
    expect(labels('최근 해소')).toEqual(['w-9 이벤트', 'w-0021 이벤트']);
    const gone = screen.getByRole('article', { name: 'w-9 이벤트' });
    expect(gone.className).toContain('opacity-60');
    expect(gone.className).not.toContain('ring-sev-critical');
    expect(gone.querySelector('[class*="animate-alarm-blink"]')).toBeNull();
    // 화면만 옮긴다: 숨기기를 부르지 않고 미확인 상태는 카드 설명에 남는다.
    expect(within(gone).getByRole('button').getAttribute('title')).toContain('미확인');
    // 지금 보이는 사람의 미확인 카드는 그대로 점멸한다.
    const live = screen.getByRole('article', { name: 'w-0 이벤트' });
    expect(live.querySelector('[class*="animate-alarm-blink"]')).toBeTruthy();
    cleanup();

    // 사람이 다 나가면 진행 중 칸이 없고, 최근 해소에 남아 패널은 접히지 않는다.
    setTracks([]);
    renderPanel([entry({ event_id: 'a', world_ids: ['w-0'] })]);
    expect(screen.queryByRole('region', { name: '진행 중' })).toBeNull();
    expect(labels('최근 해소')).toEqual(['w-0 이벤트']);
    expect(fold().getAttribute('aria-expanded')).toBe('true');
  });

  it('사람이 사라진 카드와 해소된 카드를 모두 남긴다', () => {
    setTracks([]);
    const done = [1, 2, 3, 4, 5].map((n) =>
      resolved({ event_id: `r${n}`, world_ids: [`w-10${n}`], rule_id: `R${n}` }),
    );
    renderPanel([
      entry({ event_id: 'g1', world_ids: ['w-1'] }),
      entry({ event_id: 'g2', world_ids: ['w-2'], rule_id: 'R3', zone_id: 'z-05' }),
      ...done,
    ]);
    const shown = labels('최근 해소');
    expect(shown.slice(0, 2).sort()).toEqual(['w-1 이벤트', 'w-2 이벤트']);
    expect(shown).toHaveLength(2 + 5);
  });

  it('몇 초 살다 사라진 번호(유령 트랙)의 카드는 최근 해소가 아니라 접힌 칸에 두고, 펼치면 보인다(S15P31A101-785)', () => {
    // w-0 은 처음부터 있던 실제 사람, w-212 는 2초 살다 사라진 중복 트랙
    let trackLife = EMPTY_TRACK_LIFE;
    for (let i = 0; i <= 50; i += 1) {
      const tracks = [{ world_id: 'w-0', track_lifecycle: 'CONFIRMED' }];
      if (i >= 10 && i <= 30) tracks.push({ world_id: 'w-212', track_lifecycle: 'CONFIRMED' });
      trackLife = noteWorldFrame(trackLife, tracks as never, T + i * 100);
    }
    setTracks(['CONFIRMED']);
    useRealtimeStore.setState({ trackLife });
    renderPanel([
      entry({ event_id: 'a', world_ids: ['w-0'] }),
      entry({
        event_id: 'b',
        world_ids: ['w-212'],
        rule_id: 'R3',
        zone_id: 'z-05',
        severity: 'CAUTION',
        needs_check: true,
      }),
    ]);
    expect(labels('진행 중')).toEqual(['w-0 이벤트']);
    expect(screen.queryByRole('region', { name: '최근 해소' })).toBeNull();
    const folded = section('잠깐 잡힌 번호 · 안 보임');
    expect(within(folded).queryAllByRole('article')).toHaveLength(0);
    fireEvent.click(within(folded).getByRole('button', { name: /잠깐 잡힌 번호 · 안 보임 1건/ }));
    const ghost = within(folded).getByRole('article');
    expect(ghost.textContent).not.toContain('지금 안 보임');
    expect(ghost.className).toContain('opacity-60');
    expect(within(ghost).getByRole('button').getAttribute('title')).toContain('확인 필요');
  });

  it('world 를 아직 받지 못했으면(살아 있는 사람을 모름) 카드는 진행 중에 남는다', () => {
    renderPanel([entry()]);
    expect(labels('진행 중')).toEqual(['w-0015 이벤트']);
    expect(screen.queryByRole('region', { name: '최근 해소' })).toBeNull();
  });

  it('지금 실행이 없으면(작업 전) 최근 해소는 비운다(S15P31A101-731)', () => {
    setTracks(['CONFIRMED', 'CONFIRMED']);
    renderPanel(
      [
        entry({ event_id: 'a', world_ids: ['w-0'] }),
        resolved({ event_id: 'r', world_ids: ['w-0021'] }),
      ],
      { runStartedAt: null },
    );
    expect(labels('진행 중')).toEqual(['w-0 이벤트']);
    expect(head()).toContain('진행 중 1건');
    expect(screen.queryByRole('region', { name: '최근 해소' })).toBeNull();
  });

  it('지금 실행이 있으면 그 뒤에 난 해소만 최근 해소에 남는다', () => {
    renderPanel(
      [
        resolved({ event_id: 'before', world_ids: ['w-0021'], raised_at: T - 60_000 }),
        resolved({ event_id: 'after', world_ids: ['w-0022'], raised_at: T + 1_000 }),
      ],
      { runStartedAt: T },
    );
    expect(
      within(section('최근 해소'))
        .getAllByRole('article')
        .map((a) => a.getAttribute('aria-label')),
    ).toEqual(['w-0022 이벤트']);
  });

  it('진행 중 칸과 최근 해소 칸으로 나뉘고, 카드는 각자 자기 칸에만 있다', () => {
    renderPanel([
      entry({ world_ids: ['w-0015'] }),
      resolved({ event_id: 'r', world_ids: ['w-0021'] }),
    ]);
    expect(
      within(section('진행 중'))
        .getAllByRole('article')
        .map((a) => a.getAttribute('aria-label')),
    ).toEqual(['w-0015 이벤트']);
    expect(
      within(section('최근 해소'))
        .getAllByRole('article')
        .map((a) => a.getAttribute('aria-label')),
    ).toEqual(['w-0021 이벤트']);
  });

  it('비어 있는 칸은 그리지 않는다', () => {
    renderPanel([resolved()]);
    expect(screen.queryByRole('region', { name: '진행 중' })).toBeNull();
    expect(section('최근 해소')).toBeTruthy();
  });

  it('같은 사람의 해소 알림도 각각 표시한다', () => {
    renderPanel([
      resolved({ event_id: 'a', updated_at: T + 100, resolved_at: T + 100 }),
      resolved({ event_id: 'b', updated_at: T + 200, resolved_at: T + 200 }),
    ]);
    const cards = within(section('최근 해소')).getAllByRole('article');
    expect(cards).toHaveLength(2);
    expect(
      cards.every((card) => !within(card).getByRole('button').textContent?.includes('2회')),
    ).toBe(true);
  });

  it('선택한 해소 카드만 눌리고 다시 누르면 그 알림의 상세를 연다', () => {
    const onOpen = vi.fn();
    renderPanel(
      [
        entry({ event_id: 'live', world_ids: ['w-0015'] }),
        resolved({ event_id: 'old', rule_id: 'R1', severity: 'HIGH', world_ids: ['w-0015'] }),
      ],
      { selectedKey: 'person:w-0015', onOpen },
    );
    const old = within(section('최근 해소')).getByRole('article');
    fireEvent.click(within(old).getByRole('button'));
    fireEvent.click(within(old).getByRole('button'));
    expect(onOpen).toHaveBeenCalledWith('old');
    expect(
      within(within(section('진행 중')).getByRole('article')).getByRole('button', {
        pressed: false,
      }),
    ).toBeTruthy();
  });
});

// S15P31A101-595: 꺼진 관제에서 열린 채 남은 사건은 접힌 「지난 관제 · 확인 필요」 칸으로 내린다.
describe('EventPanel 지난 관제 칸', () => {
  const staleToggle = () => screen.getByRole('button', { name: /지난 관제 · 확인 필요/ });
  const old = (change: Partial<EventItem> = {}) =>
    entry({ held_by_tier0: false, raised_at: T - 40 * 3_600_000, ...change });

  it('지난 관제는 처음에 접힌 칸으로 이벤트 수만 보이고, 진행 중 칸에 들지 않는다', () => {
    renderPanel([
      entry({ event_id: 'now', world_ids: ['w-0015'], held_by_tier0: true }),
      old({ event_id: 'o1', world_ids: ['w-0003'] }),
      old({ event_id: 'o2', world_ids: ['w-0003'], rule_id: 'R1', severity: 'HIGH' }),
      old({ event_id: 'o3', world_ids: ['w-0007'] }),
    ]);
    expect(
      within(screen.getByRole('region', { name: '진행 중' }))
        .getAllByRole('article')
        .map((a) => a.getAttribute('aria-label')),
    ).toEqual(['w-0015 이벤트']);
    expect(staleToggle().textContent).toContain('지난 관제 · 확인 필요 3건');
    expect(staleToggle().getAttribute('aria-expanded')).toBe('false');
    const region = screen.getByRole('region', { name: '지난 관제 · 확인 필요' });
    expect(within(region).queryAllByRole('article')).toHaveLength(0);

    fireEvent.click(staleToggle());
    expect(
      within(region)
        .getAllByRole('article')
        .map((a) => a.getAttribute('aria-label'))
        .sort(),
    ).toEqual(['w-0003 이벤트', 'w-0003 이벤트', 'w-0007 이벤트']);
    // 지난 관제 카드는 경보처럼 점멸하지 않고, 칩 없이 올려 보면 지난 관제라고 나온다
    expect(region.querySelector('.animate-alarm-blink, [class*="animate-alarm-blink"]')).toBeNull();
    expect(region.querySelector('[data-chip]')).toBeNull();
    expect(
      within(screen.getByRole('article', { name: 'w-0007 이벤트' }))
        .getByRole('button')
        .getAttribute('title'),
    ).toContain('지난 관제');
  });

  it('지난 관제만 남으면 진행 중이 없다고 알리고 패널을 접는다', () => {
    renderPanel([old()]);
    expect(screen.queryByRole('region', { name: '진행 중' })).toBeNull();
    expect(fold().getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(fold());
    expect(screen.getByText('진행 중 이벤트 없음')).toBeTruthy();
    expect(staleToggle().textContent).toContain('1건');
  });

  it('지난 관제 카드를 두 번 누르면 그 이벤트의 상세를 연다', () => {
    const onOpen = vi.fn();
    const onSelect = vi.fn();
    renderPanel(
      [
        entry({ event_id: 'now', held_by_tier0: true }),
        old({ event_id: 'o1', world_ids: ['w-0003'] }),
      ],
      {
        onOpen,
        onSelect,
        selectedKey: 'person:w-0003',
      },
    );
    fireEvent.click(staleToggle());
    const card = screen.getByRole('article', { name: 'w-0003 이벤트' });
    fireEvent.click(within(card).getByRole('button'));
    fireEvent.click(within(card).getByRole('button'));
    expect(onOpen).toHaveBeenCalledWith('o1');
  });
});

describe('EventPanel 떠 있는 CCTV 창 비켜 서기', () => {
  // jsdom 은 배치를 하지 않는다. 관제 장면(1440×900)과 패널 폭(340)을 심으면 기본 자리는 left 1086, top 72, 높이 414(화면의 46%), 아래 끝 486 이다.
  const scene = document.createElement('div');
  Object.defineProperties(scene, {
    clientWidth: { get: () => 1440 },
    clientHeight: { get: () => 900 },
  });
  const spies: { mockRestore: () => void }[] = [];
  beforeEach(() => {
    spies.push(
      vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockReturnValue(scene),
      vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(340),
    );
  });
  afterEach(() => {
    spies.splice(0).forEach((spy) => spy.mockRestore());
    useCctvFloatBoxStore.setState({ box: null });
  });
  const panel = () => screen.getByRole('complementary', { name: '이벤트' });
  const placed = () => {
    const { left, top, height } = panel().style;
    return { left, top, height };
  };
  const cctvAt = (left: number, top: number, width = 480, height = 360) =>
    act(() => useCctvFloatBoxStore.setState({ box: { left, top, width, height } }));

  it('창이 패널 아래쪽을 가리면 패널 아래 끝이 창 위로 올라가고, 창을 치우면 다시 길어진다', () => {
    renderPanel([entry()]);
    expect(panel().style.top).toBe('');

    cctvAt(1000, 300);
    expect(placed()).toEqual({ left: '1086px', top: '72px', height: '214px' });

    cctvAt(96, 300);
    expect(panel().style.top).toBe('');
  });

  it('기본 높이(화면의 46%) 아래에 둔 창은 패널이 비켜 설 필요가 없다', () => {
    renderPanel([entry()]);
    // 패널 아래 끝은 486 이라 그 아래(500)에 선 창과 겹치지 않는다. 전에는 바닥까지 길어서 패널이 올라갔다.
    cctvAt(1000, 500);
    expect(panel().style.top).toBe('');
    expect(panel().style.left).toBe('');
  });

  it('창이 패널 위쪽을 가리면 패널이 창 아래에서 시작하고, 창이 닫히면 제자리다', () => {
    renderPanel([entry()]);
    cctvAt(1000, 80, 480, 200);
    expect(placed()).toEqual({ left: '1086px', top: '294px', height: '192px' });

    act(() => useCctvFloatBoxStore.setState({ box: null }));
    expect(panel().style.top).toBe('');
  });

  it('접은 패널은 비켜 서지 않는다', () => {
    renderPanel([entry()]);
    cctvAt(1000, 300);
    fireEvent.click(fold());
    expect(panel().style.height).toBe('');
    expect(panel().style.top).toBe('');
  });
});

describe('EventPanel 기본 높이', () => {
  const panel = () => screen.getByRole('complementary', { name: '이벤트' });

  it('옮기지 않은 펼친 패널은 화면 높이의 46%(최소 380px)이고, 접으면 머리줄 높이다', () => {
    renderPanel([entry()]);
    // 바닥까지 늘이던 bottom 은 더 쓰지 않고 높이를 비율로 준다.
    expect(panel().className).not.toContain('bottom-');
    expect(panel().style.height).toBe(PANEL_DEFAULT_STYLE.height);
    expect(panel().style.maxHeight).toBe(PANEL_DEFAULT_STYLE.maxHeight);

    fireEvent.click(screen.getByRole('button', { name: '이벤트 접기' }));
    expect(panel().style.height).toBe('');
  });
});

describe('EventDetail 확인 필요', () => {
  function renderDetail(entries: EventEntry[], bundles?: unknown[]) {
    const incident = groupIncidents(entries)[0]!;
    const client = new QueryClient();
    client.setQueryData(ZONES_KEY, MOCK_ZONES);
    if (bundles) client.setQueryData(BUNDLES_KEY, bundles);
    render(
      <QueryClientProvider client={client}>
        <EventDetail incident={incident} openEventId="e-1" onClose={vi.fn()} />
      </QueryClientProvider>,
    );
    return screen.getByRole('dialog', { name: '사건 상세' });
  }

  it('구역은 이름으로만 보이고(대상이 구역이면 제목도), 모르는 구역은 `구역`이다', () => {
    const dialog = renderDetail([entry()]);
    expect(within(dialog).getAllByText(/개구부 구역 ·/).length).toBeGreaterThan(0);
    expect(dialog.textContent).not.toMatch(/z-\d/);
    cleanup();

    const zoneDialog = renderDetail([entry({ world_ids: [], zone_id: 'z-04' })]);
    expect(
      within(zoneDialog).getAllByRole('heading', { name: '추락 위험(가장자리)' }),
    ).toBeTruthy();
    expect(zoneDialog.textContent).not.toMatch(/z-\d/);
    cleanup();

    const unknown = renderDetail([entry({ world_ids: [], zone_id: 'z-99' })]);
    expect(within(unknown).getAllByRole('heading', { name: '추락 위험(가장자리)' })).toBeTruthy();
    expect(unknown.textContent).not.toContain('z-99');
  });

  it('AI 설명과 이벤트 클립만 표시한다', () => {
    const dialog = renderDetail([entry({ rationale: '상황 설명' })]);
    expect(within(dialog).getByRole('region', { name: 'AI 설명' }).textContent).toContain(
      '상황 설명',
    );
    expect(within(dialog).getByText('이벤트 클립')).toBeTruthy();
    for (const name of ['규칙과 근거', '처리', '타임라인'])
      expect(within(dialog).queryByRole('region', { name })).toBeNull();
    cleanup();
    expect(
      within(renderDetail([entry({ rationale: null })])).queryByRole('region', { name: 'AI 설명' }),
    ).toBeNull();
  });

  it('주의는 안내로 표시하고 자료가 없으면 접힘 칸을 숨긴다', () => {
    const dialog = renderDetail([entry({ severity: 'CAUTION' })]);
    expect(dialog.textContent).toContain('안내');
    expect(within(dialog).queryByText('이벤트 클립')).toBeNull();
    cleanup();
    const high = renderDetail([entry({ severity: 'HIGH' })]);
    expect(high.textContent).toContain('안내');
    expect(within(high).getByText('이벤트 클립')).toBeTruthy();
  });

  it('미확인 경보는 작은 외곽선 표시만 깜빡이고 움직임 줄이기 설정이면 멈춘다', () => {
    const dialog = renderDetail([entry()]);
    expect(dialog.textContent).toContain('확인 대기');
    expect(within(dialog).queryByRole('button', { name: /확인$/ })).toBeNull();
  });

  it('확인한 사건에는 미확인 표시가 없다', () => {
    const dialog = renderDetail([
      entry({
        acks: [
          {
            ack_id: 'a-1',
            channel: 'CONTROL_ROOM',
            actor_role: 'ADMIN',
            actor_id: 'admin',
            acked_at: T + 1_000,
          },
        ],
        updated_at: T + 1_000,
      }),
    ]);
    expect(within(dialog).queryByText('미확인')).toBeNull();
    expect(dialog.textContent).toContain('확인됨');
  });
});
