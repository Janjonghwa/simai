import { EventDetail } from '@/features/events/EventDetail';
import { EventPanel } from '@/features/events/EventPanel';
import { groupIncidents, mergeEvent, type EventEntry } from '@/features/events/events';
import type { EventItem } from '@/features/events/types';
import { BUNDLES_KEY, ZONES_KEY } from '@/features/zones/api';
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
    onClearAll: vi.fn(),
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

  it('카드에 규칙·구역과 경과를 넘긴다', () => {
    renderPanel([entry()]);
    const card = screen.getByRole('article', { name: 'w-0015 이벤트' });
    expect(within(card).getByText('개구부·가장자리 근접 · 개구부 구역')).toBeTruthy();
    expect(card.textContent).toMatch(/초 전|분/);
  });
});

describe('EventPanel 머리줄과 두 칸', () => {
  const head = () => screen.getByTestId('event-panel-head').textContent;
  const section = (name: string) => screen.getByRole('region', { name });

  it('머리줄에 지금 추적 중인 작업자 수(놓친 트랙 제외)와 진행 중 사건 수를 보인다', () => {
    setTracks(['CONFIRMED', 'OCCLUDED', 'LOST']);
    renderPanel([
      entry({ event_id: 'a', world_ids: ['w-0'] }),
      entry({ event_id: 'b', world_ids: ['w-1'] }),
      resolved({ event_id: 'c', world_ids: ['w-2'] }),
    ]);
    // 사건은 진행 중인 사람 묶음만 센다(해소는 세지 않는다).
    expect(head()).toContain('작업자 2명 · 사건 2건');
  });

  it('world 를 아직 받지 못했으면 작업자 0명이다', () => {
    renderPanel([entry()]);
    expect(head()).toContain('작업자 0명 · 사건 1건');
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

  it('같은 사람·규칙·구역이 되풀이된 해소는 한 카드에 N회 발생으로 보인다', () => {
    renderPanel([
      resolved({ event_id: 'a', updated_at: T + 100, resolved_at: T + 100 }),
      resolved({ event_id: 'b', updated_at: T + 200, resolved_at: T + 200 }),
    ]);
    const cards = within(section('최근 해소')).getAllByRole('article');
    expect(cards).toHaveLength(1);
    expect(within(cards[0]!).getByText('2회 발생')).toBeTruthy();
  });

  it('선택한 사람의 카드는 두 칸 모두 눌린 상태이고, 해소 카드를 누르면 그 이벤트의 상세를 연다', () => {
    const onOpen = vi.fn();
    renderPanel(
      [
        entry({ event_id: 'live', world_ids: ['w-0015'] }),
        resolved({ event_id: 'old', rule_id: 'R1', severity: 'HIGH', world_ids: ['w-0015'] }),
      ],
      { selectedKey: 'person:w-0015', onOpen },
    );
    const old = within(section('최근 해소')).getByRole('article');
    fireEvent.click(within(old).getByRole('button', { pressed: true }));
    expect(onOpen).toHaveBeenCalledWith('old');
    expect(
      within(within(section('진행 중')).getByRole('article')).getByRole('button', {
        pressed: true,
      }),
    ).toBeTruthy();
  });
});

describe('EventPanel 모두 지우기', () => {
  const clearButton = () => screen.queryByRole('button', { name: '모두 지우기' });

  it('보이는 묶음이 있으면 머리줄에 버튼이 있고, 누르면 onClearAll 을 한 번 부른다', () => {
    const onClearAll = vi.fn();
    renderPanel([entry()], { onClearAll });
    const head = screen.getByTestId('event-panel-head');
    expect(within(head).getByRole('button', { name: '모두 지우기' })).toBeTruthy();

    fireEvent.click(clearButton()!);
    expect(onClearAll).toHaveBeenCalledTimes(1);
  });

  it('해소된 묶음만 남아 있어도 지울 수 있다', () => {
    renderPanel([entry({ lifecycle: 'RESOLVED', resolved_at: T + 1_000 })]);
    expect(clearButton()).toBeTruthy();
  });

  it('보이는 묶음이 없으면 버튼이 없다(접힌 머리줄에도, 목록을 받는 중에도)', () => {
    const view = renderPanel([]);
    expect(clearButton()).toBeNull();

    view.rerender([], { loaded: false, failed: false });
    expect(clearButton()).toBeNull();

    view.rerender([entry()]);
    expect(clearButton()).toBeTruthy();
  });

  it('접어 둔 패널에서도 버튼은 접기 버튼 옆에 남는다', () => {
    renderPanel([entry()]);
    fireEvent.click(fold());
    expect(fold().getAttribute('aria-expanded')).toBe('false');
    expect(clearButton()).toBeTruthy();
  });

  it('지워서 비면 저절로 접히고 버튼이 사라진다', () => {
    const view = renderPanel([entry()]);
    view.rerender([]);
    expect(fold().getAttribute('aria-expanded')).toBe('false');
    expect(clearButton()).toBeNull();
  });
});

describe('EventPanel 떠 있는 CCTV 창 비켜 서기', () => {
  // jsdom 은 배치를 하지 않는다. 관제 장면(1440×900)과 패널 폭(340)을 심으면 기본 자리는 left 1086, top 72, 아래 끝 886 이다.
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
    expect(panel().style.height).toBe('');

    cctvAt(1000, 500);
    expect(placed()).toEqual({ left: '1086px', top: '72px', height: '414px' });

    cctvAt(96, 500);
    expect(panel().style.height).toBe('');
  });

  it('창이 패널 위쪽을 가리면 패널이 창 아래에서 시작하고, 창이 닫히면 제자리다', () => {
    renderPanel([entry()]);
    cctvAt(1000, 80, 480, 300);
    expect(placed()).toEqual({ left: '1086px', top: '394px', height: '492px' });

    act(() => useCctvFloatBoxStore.setState({ box: null }));
    expect(panel().style.top).toBe('');
  });

  it('접은 패널은 비켜 서지 않는다', () => {
    renderPanel([entry()]);
    cctvAt(1000, 500);
    fireEvent.click(fold());
    expect(panel().style.height).toBe('');
    expect(panel().style.top).toBe('');
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
    expect(within(dialog).getByText(/개구부 구역 ·/)).toBeTruthy();
    expect(dialog.textContent).not.toMatch(/z-\d/);
    cleanup();

    const zoneDialog = renderDetail([entry({ world_ids: [], zone_id: 'z-04' })]);
    expect(within(zoneDialog).getByRole('heading', { name: '개구부 구역 사건' })).toBeTruthy();
    expect(zoneDialog.textContent).not.toMatch(/z-\d/);
    cleanup();

    const unknown = renderDetail([entry({ world_ids: [], zone_id: 'z-99' })]);
    expect(within(unknown).getByRole('heading', { name: '구역 사건' })).toBeTruthy();
    expect(unknown.textContent).not.toContain('z-99');
  });

  it('규칙 줄은 구역 이름을 앞에 두고, 이름을 모르면 칸을 빼며, 구역이 없으면 `구역 없음` 이다', () => {
    const row = (change: Partial<EventItem>) => {
      const dialog = renderDetail([entry(change)]);
      const text = within(within(dialog).getByRole('region', { name: '규칙과 근거' })).getByRole(
        'listitem',
      ).textContent!;
      cleanup();
      return text;
    };
    expect(row({ zone_id: 'z-04' })).toContain('개구부 구역 · ');
    expect(row({ zone_id: 'z-99' })).not.toMatch(/구역|z-99/);
    expect(row({ zone_id: undefined })).toContain('구역 없음 · ');
    // 사람 단위 규칙(R9)은 구역을 보지 않아 구역 칸이 없다(-519).
    const r9 = row({ rule_id: 'R9', severity: 'CAUTION', zone_id: null, metrics: {} });
    expect(r9).toContain('R9 작업 중 안전모 미착용');
    expect(r9).not.toMatch(/구역/);
  });

  it('안전모 상태를 모르는 R9 는 확인 필요 이유를 한국어로 보인다(PPE_UNKNOWN)', () => {
    const dialog = renderDetail([
      entry({
        rule_id: 'R9',
        severity: 'CAUTION',
        zone_id: null,
        metrics: {},
        lifecycle: 'UPDATED',
        needs_check: true,
        confidence_reasons: ['PPE_UNKNOWN'],
      }),
    ]);
    // 머리(첫 이유)와 규칙 줄(전체 이유)에 같은 문구가 보인다.
    expect(within(dialog).getAllByText('확인 필요 · 안전모 상태 모름').length).toBeGreaterThan(0);
    expect(dialog.textContent).not.toContain('PPE_UNKNOWN');
  });

  it('해소된 R9 는 해소 전이 묶음의 안전모 요구로 작업 종료 해소와 착용 해소를 가른다', () => {
    const bundle = (version: string, helmet: 'REQUIRED' | 'NOT_REQUIRED') => ({
      config_version: version,
      issued_at: T,
      status: 'APPLIED',
      applied_at: T,
      items: [],
      ppe_policy: { helmet, work_id: helmet === 'REQUIRED' ? 12 : null, effective_from_ms: T },
    });
    const bundles = [bundle('cfg-8', 'NOT_REQUIRED'), bundle('cfg-7', 'REQUIRED')];
    const resolvedR9 = (configVersion: string) =>
      entry({
        rule_id: 'R9',
        severity: 'CAUTION',
        zone_id: null,
        metrics: {},
        lifecycle: 'RESOLVED',
        resolved_at: T + 9_000,
        updated_at: T + 9_000,
        config_version: configVersion,
      });

    const ended = renderDetail([resolvedR9('cfg-8')], bundles);
    expect(within(ended).getByRole('region', { name: '규칙과 근거' }).textContent).toContain(
      '작업 종료로 해소',
    );
    expect(within(ended).getByRole('region', { name: '타임라인' }).textContent).toContain(
      '작업 중 안전모 미착용 작업 종료로 해소',
    );
    cleanup();

    const worn = renderDetail([resolvedR9('cfg-7')], bundles);
    expect(within(worn).getByRole('region', { name: '처리' }).textContent).toContain(
      '착용 확인으로 해소',
    );
    cleanup();

    // 묶음을 모르면(최근 목록 밖) 그냥 해소다.
    const unknown = renderDetail([resolvedR9('cfg-1')], bundles);
    const rules = within(unknown).getByRole('region', { name: '규칙과 근거' }).textContent!;
    expect(rules).toMatch(/해소 \d/);
    expect(rules).not.toMatch(/작업 종료로|착용 확인으로/);
  });

  it('복원 이벤트는 확인 필요와 첫 이유를 머리에, 전체 이유를 규칙 줄에 보인다', () => {
    const dialog = renderDetail([
      entry({
        verification: 'UNVERIFIABLE',
        needs_check: true,
        confidence_reasons: ['Z_UNKNOWN', 'PREDICTED'],
      }),
    ]);
    expect(within(dialog).getByText('확인 필요 · 높이 모름')).toBeTruthy();
    expect(within(dialog).getByText('확인 필요 · 높이 모름, 예측 위치')).toBeTruthy();
    expect(within(dialog).getByText('해소 확인 불가')).toBeTruthy();
  });

  it('미확인 경보는 작은 외곽선 표시만 깜빡이고 움직임 줄이기 설정이면 멈춘다', () => {
    const dialog = renderDetail([entry()]);
    const marker = within(dialog).getByText('미확인');
    const blink = marker.querySelector('[aria-hidden="true"]');
    expect(blink?.className).toContain('motion-safe:animate-alarm-blink');
    expect(blink?.className).toContain('border');
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
  });
});
