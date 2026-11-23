import { useCamSplitStore } from '@/features/cctv/camSplit.store';
import { groupIncidents, mergeEvent, type EventEntry } from '@/features/events/events';
import { IncidentCard } from '@/features/events/IncidentCard';
import type { EventItem } from '@/features/events/types';
import { ZONES_KEY } from '@/features/zones/api';
import { MOCK_ZONES } from '@/mocks/zones';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import type { WorldStatePayload } from '@simai/contracts';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const T = 1_760_342_400_000;

const navigate = vi.hoisted(() => vi.fn());
vi.mock('@tanstack/react-router', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@tanstack/react-router')>()),
  useNavigate: () => navigate,
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

function renderCard(
  entries: EventEntry[],
  props: Partial<React.ComponentProps<typeof IncidentCard>> = {},
) {
  const incident = groupIncidents(entries)[0]!;
  const onSelect = vi.fn();
  const onOpen = vi.fn();
  const client = new QueryClient();
  client.setQueryData(ZONES_KEY, MOCK_ZONES);
  render(
    <QueryClientProvider client={client}>
      <ul>
        <IncidentCard
          incident={incident}
          selected={false}
          onSelect={onSelect}
          onOpen={onOpen}
          {...props}
        />
      </ul>
    </QueryClientProvider>,
  );
  return { card: screen.getByRole('article'), onSelect, onOpen, incident };
}

/** 카드 머리를 올려 보면 나오는 설명(줄 단위). 한 줄 카드에서 뺀 정보가 여기에 있다(-798). */
const detail = (card: HTMLElement) =>
  (within(card).getAllByRole('button')[0]!.getAttribute('title') ?? '').split('\n');

const CLOCK = /^\d{2}:\d{2}:\d{2}$/;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T + 12_000);
  navigate.mockReset();
  useCamSplitStore.getState().close();
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u', role: 'ADMIN' } });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  useSessionStore.setState(useSessionStore.getInitialState(), true);
  useRealtimeStore.setState(useRealtimeStore.getInitialState(), true);
});

describe('IncidentCard', () => {
  it('카드는 큰 배지 · 사건(굵게)/사람 두 줄 · 오른쪽 HH:MM:SS 시각을 보인다', () => {
    const { card } = renderCard([entry()]);

    const badge = within(card).getByText('위험');
    expect(badge.className).toContain('h-7');
    // 위험 배지 기호는 ▲ 하나이고 화면 낭독기에 읽히지 않는다.
    expect(within(card).getByText('▲').getAttribute('aria-hidden')).not.toBeNull();
    expect(within(card).getByText('추락 위험(가장자리)')).toBeTruthy();
    expect(within(card).getByText('작업자 #0015')).toBeTruthy();
    const timeEl = [...card.querySelectorAll('span')].find((el) =>
      CLOCK.test(el.textContent ?? ''),
    );
    expect(timeEl).toBeTruthy();
    // 「미확인」 칩은 그리지 않는다. 미확인 경보의 점멸·테두리는 아래 시험이 따로 본다.
    expect(card.querySelector('[data-chip]')).toBeNull();
    expect(card.textContent).not.toContain('미확인');
    expect(card.textContent).not.toContain('초 전');
    expect(card.textContent).not.toContain('개구부 구역');
    expect(card.textContent).not.toMatch(/z-\d/);
    // 대상은 카드 이름에 남는다.
    expect(card.getAttribute('aria-label')).toBe('w-0015 이벤트');
  });

  it('뺀 정보(대상·처리 상태·발생 시각)는 머리를 올려 보면 나오는 설명에 있다', () => {
    const { card } = renderCard([entry()]);
    const lines = detail(card);
    expect(lines[0]).toBe('w-0015');
    // 사이드 카드에는 확인 개념이 없어 처리 상태 줄(미확인·확인 시각)이 없다(S15P31A101-838 후속)
    expect(lines[1]).toMatch(/^발생 \d{2}:\d{2}:\d{2}$/);
    expect(lines).not.toContain('미확인');
    expect(lines).toHaveLength(2);
  });

  it('지난 관제 카드는 흐리고 등급 테두리·점멸이 없으며, 설명에 지난 관제라고 적는다', () => {
    const { card } = renderCard([entry({ held_by_tier0: false, severity: 'HIGH' })]);

    expect(detail(card)).toContain('지난 관제');
    expect(card.querySelector('[data-chip]')).toBeNull();
    expect(card.className).toContain('opacity-75');
    expect(card.className).not.toContain('ring-sev-caution');
    expect(card.querySelector('.motion-safe\\:animate-alarm-blink')).toBeNull();
  });

  it('사라진 사람의 카드는 흐리고 등급 테두리·점멸이 없지만, 미확인 상태는 설명에 그대로 남는다', () => {
    const { card } = renderCard([entry()], { gone: true });

    expect(card.className).toContain('opacity-60');
    expect(card.className).not.toContain('ring-sev-critical');
    expect(card.textContent).not.toContain('지금 안 보임');
    const bar = card.querySelector<HTMLElement>('[aria-hidden="true"].w-1')!.className;
    expect(bar).toContain('bg-sev-critical');
    expect(bar).not.toContain('animate-alarm-blink');
    expect(detail(card)).not.toContain('미확인');
  });

  it('긴 사람 ID 는 카드에 끝번호로 줄이고 설명에 전체를 둔다', () => {
    const { card } = renderCard([entry({ world_ids: ['w-mw8y3hai-0722'] })]);

    expect(detail(card)[0]).toBe('w-mw8y3hai-0722');
    expect(card.textContent).not.toContain('mw8y3hai');
    expect(within(card).getByText('작업자 #0722')).toBeTruthy();
  });

  it('R1 주의 묶음은 규칙 이름을 구역 이름 없이 위험구역 진입으로 보인다', () => {
    const { card } = renderCard([entry({ rule_id: 'R1', severity: 'CAUTION', zone_id: 'z-04' })]);

    expect(within(card).getByText('위험구역 진입')).toBeTruthy();
    expect(card.textContent).not.toContain('개구부 구역');
    expect(card.textContent).not.toContain('위험 구역 근접');
  });

  it('묶인 카드 제목·배지는 가장 최근 경보로 보이고, 개구부 수치·나머지 구역은 카드에 없다', () => {
    const { card } = renderCard([
      entry({ event_id: 'e-1', rule_id: 'R2', severity: 'CRITICAL', raised_at: T }),
      entry({
        event_id: 'e-2',
        rule_id: 'R4',
        severity: 'CRITICAL',
        zone_id: 'z-02',
        raised_at: T + 5_000,
        updated_at: T + 5_000,
      }),
    ]);

    expect(card.textContent).not.toContain('0.82');
    expect(card.textContent).not.toContain('높이 차');
    expect(within(card).getByText('낙상 의심')).toBeTruthy();
    expect(within(card).queryByText('추락 위험(가장자리)')).toBeNull();
    expect(card.textContent).not.toContain('단상 구역');
    expect(card.textContent).not.toMatch(/z-\d/);
    expect(card.querySelectorAll('li')).toHaveLength(0);
  });

  it('구역이 대상인 카드는 대상 줄에 구역 이름을 보이고, 이름을 모르면 `구역`이다', () => {
    const { card } = renderCard([entry({ world_ids: [], zone_id: 'z-04' })]);
    expect(card.getAttribute('aria-label')).toBe('개구부 구역 이벤트');
    expect(detail(card)[0]).toBe('개구부 구역');
    expect(within(card).getByText('추락 위험(가장자리)')).toBeTruthy();
    expect(within(card).getByText('개구부 구역')).toBeTruthy();
    cleanup();

    const unknown = renderCard([entry({ world_ids: [], zone_id: 'z-99' })]).card;
    expect(detail(unknown)[0]).toBe('구역');
    expect(within(unknown).getByText('구역')).toBeTruthy();
    expect(unknown.textContent).not.toContain('z-99');
    expect(detail(unknown).join('\n')).not.toContain('z-99');
  });

  it('위험구역 접근(R5)도 진입 예상 줄 없이 한 줄이다', () => {
    const { card } = renderCard([
      entry({
        rule_id: 'R5',
        severity: 'CAUTION',
        metrics: { time_to_entry_s: 1.6 },
        zone_id: 'z-02',
      }),
    ]);

    expect(card.textContent).not.toContain('진입 예상');
    expect(detail(card).join('\n')).not.toContain('진입 예상');
    // 주의 등급은 확인할 경보가 없어 처리 상태가 없다.
    expect(detail(card)).toHaveLength(2);
  });

  it('안전모 미착용(R9)은 구역 없이 주의 배지와 규칙 이름으로 보이고 처리 상태가 없다', () => {
    // R9 는 사람 단위라 zone_id 가 없다(-519).
    const { card } = renderCard([
      entry({ rule_id: 'R9', severity: 'CAUTION', metrics: {}, zone_id: null }),
    ]);

    expect(within(card).getByText('주의')).toBeTruthy();
    expect(within(card).getByText('안전모 미착용')).toBeTruthy();
    expect(card.textContent).not.toMatch(/구역/);
    expect(detail(card)).not.toContain('미확인');
  });

  it('확인해도 사이드 카드 설명에 확인 표시가 없다(확인은 배너 전용)', () => {
    const acked = entry({
      acks: [
        {
          ack_id: 'a-1',
          channel: 'CONTROL_ROOM',
          actor_role: 'ADMIN',
          actor_id: 'admin',
          acked_at: T + 3_000,
        },
      ],
      updated_at: T + 3_000,
    });
    const { card } = renderCard([acked]);
    expect(detail(card).some((line) => line.startsWith('확인 '))).toBe(false);
    expect(detail(card)).not.toContain('미확인');
  });

  it('해소된 묶음은 설명에 해소 시각만 있고 발생 시각은 없으며, 카드는 흐려지고 「해소」 글 없이 발생 시각만 보인다(-856)', () => {
    const { card } = renderCard([
      entry({ lifecycle: 'RESOLVED', resolved_at: T + 5_000, updated_at: T + 5_000 }),
    ]);

    expect(detail(card)[1]).toMatch(/^해소 \d{2}:\d{2}:\d{2}$/);
    expect(card.textContent).not.toMatch(/해소/);
    expect(card.textContent).toMatch(/\d{2}:\d{2}:\d{2}/);
    expect(detail(card).some((line) => line.startsWith('발생'))).toBe(false);
    expect(card.textContent).not.toContain('초 전');
    expect(card.className).toContain('opacity-55');
    expect(card.className).not.toContain('ring-sev-critical');
  });

  it('두 번 이상 발생하면 카드와 설명에 횟수를 적는다', () => {
    const resolved = entry({
      lifecycle: 'RESOLVED',
      resolved_at: T + 5_000,
      updated_at: T + 5_000,
    });
    const twice = renderCard([resolved], { occurrences: 2 });
    expect(detail(twice.card)).toContain('2회 발생');
    expect(twice.card.textContent).toContain('2회');
    cleanup();

    const once = renderCard([resolved], { occurrences: 1 });
    expect(detail(once.card).join('\n')).not.toMatch(/회 발생/);
  });

  it('트랙 번호가 바뀌며 이어진 경보를 묶은 카드는 설명에 묶은 번호 수를 적는다(S15P31A101-731)', () => {
    const { card } = renderCard([entry()], { occurrences: 3, people: 3 });
    expect(detail(card)).toContain('3회 발생(트랙 번호 3개를 한 카드로 묶음)');
  });

  it('복원 이벤트의 확인 필요와 첫 이유는 설명에만 있다', () => {
    const { card } = renderCard([
      entry({
        verification: 'UNVERIFIABLE',
        needs_check: true,
        confidence_reasons: ['Z_UNKNOWN', 'PREDICTED'],
      }),
    ]);

    expect(detail(card)).toContain('확인 필요 · 높이 모름');
    expect(card.textContent).not.toContain('확인 필요');
    expect(card.textContent).not.toContain('높이 모름');
  });

  it('확인 필요는 운영자가 확인하기 전에만 설명에 있다', () => {
    const needsCheck = { verification: 'UNVERIFIABLE', needs_check: true } as const;
    const before = renderCard([entry(needsCheck)]).card;
    expect(detail(before).some((line) => line.startsWith('확인 필요'))).toBe(true);
    cleanup();

    const acked = renderCard([
      entry({
        ...needsCheck,
        acks: [
          {
            ack_id: 'a-1',
            channel: 'CONTROL_ROOM',
            actor_role: 'ADMIN',
            actor_id: 'admin',
            acked_at: T + 3_000,
          },
        ],
        updated_at: T + 3_000,
      }),
    ]).card;
    expect(detail(acked).some((line) => line.startsWith('확인 필요'))).toBe(false);
  });

  it('지연 알림·작업 외 꼬리표는 카드 줄에 없고 설명에 있다', () => {
    const { card } = renderCard(
      [entry({ delivery: 'DELAYED_ACTIVE', verification: 'UNVERIFIABLE', needs_check: true })],
      { outsideWork: 'OUTSIDE_ZONE' },
    );

    for (const tag of ['지연 알림', '작업 구역 밖', '확인 필요']) {
      expect(card.textContent).not.toContain(tag);
    }
    expect(detail(card)).toContain('지연 알림');
    expect(detail(card)).toContain('작업 구역 밖');
  });

  it('머리: 처음 누르면 선택, 이미 선택된 카드를 다시 누르면 상세를 연다', () => {
    const first = renderCard([entry()]);
    fireEvent.click(within(first.card).getByRole('button', { pressed: false }));
    expect(first.onSelect).toHaveBeenCalledWith(first.incident);
    expect(first.onOpen).not.toHaveBeenCalled();
    cleanup();

    const second = renderCard([entry()], { selected: true });
    fireEvent.click(within(second.card).getByRole('button', { pressed: true }));
    expect(second.onOpen).toHaveBeenCalledWith('e-1');
    expect(second.onSelect).not.toHaveBeenCalled();
  });

  it('선택되면 카드가 보이는 자리로 스크롤한다', () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    renderCard([entry()], { selected: true });
    expect(scroll).toHaveBeenCalledWith({ block: 'nearest' });
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  });

  it('위험 경보 카드도 확인 여부로 깜빡이지 않고(확인은 배너 전용) 진행 중 테두리만 있다', () => {
    const { card } = renderCard([entry()]);
    const blink = [...card.querySelectorAll<HTMLElement>('[aria-hidden="true"]')].filter((el) =>
      el.getAttribute('class')?.includes('animate-alarm-blink'),
    );
    expect(blink).toHaveLength(0);
    expect(card.className).toContain('ring-sev-critical');
  });

  it('등급마다 띠 색이 다르고 깜빡이지 않는다', () => {
    const barOf = (card: HTMLElement) =>
      card.querySelector<HTMLElement>('[aria-hidden="true"].w-1')!.className;

    const caution = renderCard([entry({ severity: 'CAUTION' })]);
    expect(barOf(caution.card)).toContain('bg-sev-caution');
    expect(barOf(caution.card)).not.toContain('animate-alarm-blink');
    cleanup();

    const info = renderCard([entry({ severity: 'INFO' })]);
    expect(barOf(info.card)).toContain('bg-faint');
  });

  it('HIGH 는 주의 노랑 띠·테두리이고 칩은 없다', () => {
    const { card } = renderCard([entry({ severity: 'HIGH' })]);
    const bar = card.querySelector<HTMLElement>('[aria-hidden="true"].w-1')!.className;
    expect(bar).toContain('bg-sev-caution');
    expect(bar).not.toContain('bg-sev-critical');
    expect(card.className).toContain('ring-sev-caution');
    expect(card.querySelector('[data-chip]')).toBeNull();
  });

  it('사람을 담은 카메라가 있어도 카드에는 「CCTV로 확인」 버튼이 없다', () => {
    useRealtimeStore.setState({
      world: {
        receivedAt: T,
        payload: {
          tracks: [{ world_id: 'w-0015', cameras: ['cam_B', 'cam_A'] }],
        } as unknown as WorldStatePayload,
      },
    });
    const { card } = renderCard([entry()]);

    expect(within(card).queryByRole('button', { name: /CCTV/ })).toBeNull();
    expect(within(card).getAllByRole('button')).toHaveLength(1);
    fireEvent.click(within(card).getByRole('button'));
    expect(navigate).not.toHaveBeenCalled();
    expect(useCamSplitStore.getState().split).toBeNull();
  });

  it('해석된 사람 표시 이름은 카드 대상 줄과 설명에 보인다(-658)', () => {
    useRealtimeStore.setState({
      health: {
        receivedAt: T,
        clockOffsetMs: 0,
        payload: {
          person_names: [
            { world_id: 'w-0012', display_name: '김철수', name_source: 'WATCH_DEVICE' },
            { world_id: 'w-0015', display_name: '김철수', name_source: 'WORLD_MANUAL' },
          ],
        } as never,
      },
    });
    const { card } = renderCard([entry({ world_ids: ['w-0015'] })]);
    expect(detail(card)[0]).toBe('김철수 (w-0015)');
    expect(card.getAttribute('aria-label')).toBe('김철수 (w-0015) 이벤트');
    expect(within(card).getByText('김철수 (0015)')).toBeTruthy();
  });

  it('워치 배정이 다른 World ID 로 옮겨 가도 앞선 경보 카드는 「작업자 #N」으로 바뀌지 않는다(-851)', () => {
    const health = (names: { world_id: string; display_name: string; name_source: string }[]) => ({
      receivedAt: T,
      clockOffsetMs: 0,
      payload: { as_of: T, person_names: names } as never,
    });
    useRealtimeStore
      .getState()
      .receiveHealth(
        health([{ world_id: 'w-0008', display_name: '추창만', name_source: 'WATCH_DEVICE' }]),
      );
    useRealtimeStore.getState().receiveHealth(
      health([
        { world_id: 'w-0008', display_name: '미확인 7', name_source: 'UNIDENTIFIED' },
        { world_id: 'w-0010', display_name: '추창만', name_source: 'WATCH_DEVICE' },
      ]),
    );
    const { card } = renderCard([entry({ world_ids: ['w-0008'] })]);
    expect(within(card).getByText('추창만')).toBeTruthy();
    expect(within(card).queryByText('작업자 #7')).toBeNull();
    expect(detail(card)[0]).toBe('추창만 (w-0008)');
  });

  it('사람 대상 UNVERIFIABLE 경보의 대상이 world_state 트랙에 없으면 「확인 불가 · 대상 시야 이탈」을 보인다(-811)', () => {
    useRealtimeStore.setState({
      world: {
        receivedAt: T,
        payload: {
          tracks: [
            { world_id: 'w-0015', track_lifecycle: 'LOST', cameras: [] },
            { world_id: 'w-0021', track_lifecycle: 'CONFIRMED', cameras: ['cam_A'] },
          ],
        } as unknown as WorldStatePayload,
      },
    });
    const { card } = renderCard(
      [entry({ rule_id: 'R4', verification: 'UNVERIFIABLE', world_ids: ['w-0015'] })],
      { gone: true },
    );

    // 카드는 한 줄이라 본문에는 없고, 마우스 올림 설명(title)에만 있다
    expect(within(card).queryByText('확인 불가 · 대상 시야 이탈')).toBeNull();
    expect(detail(card)).toContain('확인 불가 · 대상 시야 이탈');
    cleanup();

    // 대상이 트랙(또는 병합 aliases)에 있으면 기존 한 줄 카드와 문구를 유지한다.
    useRealtimeStore.setState({
      world: {
        receivedAt: T,
        payload: {
          tracks: [
            {
              world_id: 'w-0099',
              aliases: ['w-0015'],
              track_lifecycle: 'CONFIRMED',
              cameras: ['cam_A'],
            },
          ],
        } as unknown as WorldStatePayload,
      },
    });
    const present = renderCard([
      entry({ rule_id: 'R4', verification: 'UNVERIFIABLE', world_ids: ['w-0015'] }),
    ]).card;
    expect(within(present).queryByText('확인 불가 · 대상 시야 이탈')).toBeNull();
    expect(detail(present)).not.toContain('미확인');
    cleanup();

    // 구역 이벤트·VERIFIED 이벤트에는 적용하지 않는다.
    useRealtimeStore.setState({
      world: {
        receivedAt: T,
        payload: { tracks: [] } as unknown as WorldStatePayload,
      },
    });
    const zoneCard = renderCard([
      entry({ verification: 'UNVERIFIABLE', world_ids: [], zone_id: 'z-04' }),
    ]).card;
    expect(within(zoneCard).queryByText('확인 불가 · 대상 시야 이탈')).toBeNull();
    cleanup();

    const verifiedCard = renderCard([entry({ verification: 'VERIFIED', world_ids: ['w-0015'] })], {
      gone: true,
    }).card;
    expect(within(verifiedCard).queryByText('확인 불가 · 대상 시야 이탈')).toBeNull();
    expect(detail(verifiedCard)).not.toContain('미확인');
  });
});
