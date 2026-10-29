import { zoneOf } from '@/__tests__/features/zones/zoneFixture';
import { SpatialViewportHost } from '@/features/layout/SpatialViewportHost';
import type {
  Viewer3DHandle,
  Viewer3DProps,
  ViewerEditBox,
  ViewerEditField,
  ViewerStatus,
  ViewerTarget,
} from '@/features/viewer3d';
import { fieldOperation, isValidBox } from '@/features/viewer3d/editing/editGeometry';
import { worldPoint } from '@/features/viewer3d/worldCoordinates';
import type { ViewerZones, Zone } from '@/features/zones/types';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { parseZonesSearch } from '@/features/zones/zonesSearch';
import { ApiError } from '@/lib/http';
import { worldStateAt } from '@/mocks/realtimeScenario';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore, type Role } from '@/store/session.store';
import { useZoneDraftsStore } from '@/store/zoneDrafts.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useEffect, useImperativeHandle, useRef, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const DEFAULT_ZONES: Zone[] = [
  zoneOf({
    zoneId: 'z-02',
    name: '중앙 통로 상부 작업 공간',
    min: [2.2, 2.5, 0.6],
    max: [5.4, 5.1, 2.4],
  }),
  zoneOf(
    {
      zoneId: 'z-04',
      name: '북동 위험 구역',
      min: [5.8, 1.2, 0],
      max: [8.6, 3.8, 2.4],
    },
    { version: 3, severity: 'CRITICAL', zoneType: 'OPENING' },
  ),
];

const mockZonesState = vi.hoisted(() => ({
  zones: [] as Zone[],
  raw: undefined as ViewerZones | undefined,
  failed: false,
}));
// 시험마다 바꾼다. 기본은 서버가 없는 것처럼 실패한다.
const apiMock = vi.hoisted(() => ({
  fn: (path: string, options: unknown): Promise<unknown> =>
    Promise.reject(new Error(`no api in test: ${path} ${typeof options}`)),
}));
// 최근 구성 묶음 조회(GET /api/config-bundles)의 답. 시험마다 바꾼다.
const bundlesMock = vi.hoisted(() => ({ list: [] as unknown[] }));
const bundleOf = (
  config_version: string,
  issued_at: number,
  status: 'PENDING' | 'APPLIED' | 'REJECTED' | 'MISMATCH',
  active: string | null = config_version,
) => ({
  config_version,
  issued_at,
  status,
  applied_at: status === 'APPLIED' ? issued_at + 1 : null,
  last_ack:
    status === 'PENDING'
      ? null
      : {
          active_config_version: active,
          reject_reason: status === 'REJECTED' ? 'VALIDATION_FAILED' : null,
        },
});

vi.mock('@/features/events/useLiveEvents', () => ({
  useLiveEvents: () => ({ entries: [], loaded: true, failed: false }),
}));
vi.mock('@/features/zones/useZones', () => ({
  useZones: () => mockZonesState,
}));
// 조회(GET)는 시험마다 정한 묶음 목록과, 저장 전 확인용으로 화면의 구역 버전을 그대로 답한다. 쓰기는 apiMock 이 답한다.
vi.mock('@/lib/api', () => ({
  api: (path: string, options: unknown) => {
    if ((options as { method?: string }).method) return apiMock.fn(path, options);
    if (path === '/api/config-bundles') return Promise.resolve(bundlesMock.list);
    if (path === '/api/zones')
      return Promise.resolve(
        mockZonesState.zones.map((z) => ({ zone_id: z.zoneId, zone_version: z.version })),
      );
    return apiMock.fn(path, options);
  },
}));

const fake = vi.hoisted(() => ({
  selected: null as ViewerTarget | null,
  editing: null as { target: ViewerTarget; base: ViewerEditBox; draft: ViewerEditBox } | null,
  selectCalls: 0,
  focusCalls: 0,
  failAtUpdateStep: 0,
  updateStepCount: 0,
  overlay: null as Viewer3DProps['editDraftOverlay'] | null,
  onSelect: null as Viewer3DProps['onSelectionChange'] | null,
  onPreview: null as Viewer3DProps['onEditPreview'] | null,
  onResult: null as Viewer3DProps['onEditResult'] | null,
  onStatus: null as Viewer3DProps['onStatusChange'] | null,
}));

function applyField(
  draft: ViewerEditBox,
  field: ViewerEditField,
  value: number,
): ViewerEditBox | null {
  const wBox = {
    min: worldPoint(draft.min[0], draft.min[1], draft.min[2]),
    max: worldPoint(draft.max[0], draft.max[1], draft.max[2]),
  };
  const op = fieldOperation(wBox, field, value);
  if (!op) return null;
  const next: ViewerEditBox = {
    min: [...draft.min] as [number, number, number],
    max: [...draft.max] as [number, number, number],
  };
  if (op.type === 'translate') {
    next.min = [next.min[0] + op.delta[0], next.min[1] + op.delta[1], next.min[2] + op.delta[2]];
    next.max = [next.max[0] + op.delta[0], next.max[1] + op.delta[1], next.max[2] + op.delta[2]];
  } else if (op.type === 'face') {
    const axis = op.face[0] === 'x' ? 0 : op.face[0] === 'y' ? 1 : 2;
    if (op.face[1] === '+') next.max[axis] += op.delta;
    else next.min[axis] += op.delta;
  }
  return isValidBox({
    min: worldPoint(next.min[0], next.min[1], next.min[2]),
    max: worldPoint(next.max[0], next.max[1], next.max[2]),
  })
    ? next
    : null;
}

function FakeViewer({
  ref,
  onSelectionChange,
  onStatusChange,
  onEditPreview,
  onEditResult,
  editDraftOverlay,
}: Viewer3DProps) {
  useEffect(() => {
    fake.onSelect = onSelectionChange ?? null;
    fake.onPreview = onEditPreview ?? null;
    fake.onResult = onEditResult ?? null;
    fake.overlay = editDraftOverlay ?? null;
  }, [onSelectionChange, onEditPreview, onEditResult, editDraftOverlay]);

  useImperativeHandle(
    ref,
    () =>
      ({
        select: (target: ViewerTarget) => {
          fake.selectCalls += 1;
          const exists = mockZonesState.zones.some((z) => z.zoneId === target.id);
          if (!exists) return { ok: false, reason: 'NOT_FOUND' };
          fake.selected = target;
          fake.onSelect?.(target);
          return { ok: true };
        },
        focus: () => {
          fake.focusCalls += 1;
          return { ok: true };
        },
        clearSelection: () => {
          fake.selected = null;
          fake.onSelect?.(null);
          return { ok: true };
        },
        setMode: () => ({ ok: true }),
        beginEdit: (target: ViewerTarget) => {
          const zone = mockZonesState.zones.find((z) => z.zoneId === target.id);
          if (!zone) return { ok: false, reason: 'NOT_FOUND' };
          const base: ViewerEditBox = {
            min: [zone.min[0], zone.min[1], zone.min[2]],
            max: [zone.max[0], zone.max[1], zone.max[2]],
          };
          const draft: ViewerEditBox = { min: [...base.min], max: [...base.max] };
          fake.editing = { target, base, draft };
          fake.selected = target;
          fake.onSelect?.(target);
          fake.onPreview?.({
            target,
            mode: 'LIVE',
            before: base,
            draft,
            provenance: {
              sourceId: useRealtimeStore.getState().source?.id ?? 's-1',
              zoneContract: 'DEMO_UNCONFIRMED',
              zonesReceivedAt: 1000,
              sceneVersion: useRealtimeStore.getState().world?.payload.scene_version ?? 'scene-3',
              configVersion: null,
            },
            saved: false,
          });
          return { ok: true };
        },
        updateEditField: ({ field, value }) => {
          if (!fake.editing) return { ok: false, reason: 'NOT_EDITABLE' };
          fake.updateStepCount += 1;
          if (fake.failAtUpdateStep > 0 && fake.updateStepCount === fake.failAtUpdateStep) {
            return { ok: false, reason: 'INVALID_GEOMETRY' };
          }
          const next = applyField(fake.editing.draft, field, value);
          if (!next) return { ok: false, reason: 'INVALID_GEOMETRY' };
          fake.editing.draft = next;
          fake.onPreview?.({
            target: fake.editing.target,
            mode: 'LIVE',
            before: fake.editing.base,
            draft: next,
            provenance: {
              sourceId: useRealtimeStore.getState().source?.id ?? 's-1',
              zoneContract: 'DEMO_UNCONFIRMED',
              zonesReceivedAt: 1000,
              sceneVersion: useRealtimeStore.getState().world?.payload.scene_version ?? 'scene-3',
              configVersion: null,
            },
            saved: false,
          });
          return { ok: true };
        },
        finishEdit: () => {
          if (!fake.editing) return null;
          const cur = fake.editing;
          fake.editing = null;
          const res = {
            status: 'FINISHED' as const,
            target: cur.target,
            mode: 'LIVE' as const,
            before: cur.base,
            after: cur.draft,
            changed: JSON.stringify(cur.base) !== JSON.stringify(cur.draft),
            provenance: {
              sourceId: useRealtimeStore.getState().source?.id ?? 's-1',
              zoneContract: 'DEMO_UNCONFIRMED' as const,
              zonesReceivedAt: 1000,
              sceneVersion: useRealtimeStore.getState().world?.payload.scene_version ?? 'scene-3',
              configVersion: null,
            },
            saved: false as const,
          };
          fake.onResult?.(res);
          return res;
        },
        cancelEdit: () => {
          if (!fake.editing) return null;
          const cur = fake.editing;
          fake.editing = null;
          const res = {
            status: 'CANCELLED' as const,
            target: cur.target,
            mode: 'LIVE' as const,
            before: cur.base,
            reason: 'USER' as const,
            provenance: {
              sourceId: useRealtimeStore.getState().source?.id ?? 's-1',
              zoneContract: 'DEMO_UNCONFIRMED' as const,
              zonesReceivedAt: 1000,
              sceneVersion: useRealtimeStore.getState().world?.payload.scene_version ?? 'scene-3',
              configVersion: null,
            },
            saved: false as const,
          };
          fake.onResult?.(res);
          return res;
        },
      }) as Viewer3DHandle,
  );

  const reported = useRef(false);
  useEffect(() => {
    fake.onStatus = onStatusChange ?? null;
    if (!reported.current) {
      reported.current = true;
      onStatusChange?.({ phase: 'ready', mode: 'LIVE', error: null } as ViewerStatus);
    }
  }, [onStatusChange]);

  return <div data-testid="viewer3d" />;
}

const loadFake = () => Promise.resolve({ default: FakeViewer });

function signInAs(role: Role) {
  useSessionStore.setState({
    session: { accessToken: `token-${role}`, user: { id: role.toLowerCase(), role } },
  });
}

/** 구역을 고르지 않았을 때 보이는 새 구역 양식에서 유형을 고른다. */
function chooseNewZoneType(zoneType: string) {
  const form = screen.getByRole('region', { name: '새 구역' });
  act(() => {
    fireEvent.change(within(form).getByRole('combobox', { name: '유형' }), {
      target: { value: zoneType },
    });
  });
}
const addZoneButton = () => screen.getByRole('button', { name: '구역 추가' }) as HTMLButtonElement;
/** 구역 목록 드롭다운을 연다. */
function openZoneList() {
  act(() => screen.getByRole('button', { name: /^구역 목록/ }).click());
  return screen.getByRole('region', { name: '구역 목록 선택' });
}
/** 드롭다운의 「새 구역」을 골라 선택을 풀고 새 구역 양식을 연다. */
function pickNewZone() {
  const list = openZoneList();
  act(() => within(list).getByRole('button', { name: '새 구역' }).click());
}

function renderZones(ui: ReactNode = <ZonesPage />) {
  // 실패를 흉내 내는 시험이 재시도 지연을 기다리지 않게 한다.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SpatialViewportHost loadViewer={loadFake} zoneEditor>
        {ui}
      </SpatialViewportHost>
    </QueryClientProvider>,
  );
}

describe('ZonesPage 경량 플로팅 편집 도구', () => {
  beforeEach(() => {
    signInAs('ADMIN');
    useZoneDraftsStore.getState().clear();
    useRealtimeStore.setState(
      {
        ...useRealtimeStore.getInitialState(),
        source: { id: 's-1', pushZones: vi.fn() } as never,
        world: { payload: worldStateAt(Date.now()).payload, receivedAt: Date.now() },
      },
      true,
    );
    mockZonesState.zones = [...DEFAULT_ZONES];
    mockZonesState.raw = { contract: 'DEMO_UNCONFIRMED', zones: [] };
    mockZonesState.failed = false;
    fake.selected = null;
    fake.editing = null;
    fake.selectCalls = 0;
    fake.focusCalls = 0;
    fake.failAtUpdateStep = 0;
    fake.updateStepCount = 0;
    fake.overlay = null;
    apiMock.fn = () => Promise.reject(new Error('no api in test'));
    // 기본: 구역들(valid_from 1)보다 늦게 발행한 cfg-12 가 현장에 적용되어 있다.
    bundlesMock.list = [bundleOf('cfg-12', 100, 'APPLIED')];
  });
  afterEach(cleanup);

  it('구역 선택 전에는 최소 안내와 접이식 구역 목록 버튼만 보이고 3열 고정 패널이나 도구 탭을 두지 않는다', async () => {
    renderZones();
    await screen.findByTestId('viewer3d');
    // 드롭다운 기본은 「새 구역」이고, 모드·상태 칩은 두지 않는다.
    expect(screen.getByRole('button', { name: '구역 목록: 새 구역' })).toBeTruthy();
    expect(screen.queryByText('구역 편집 모드')).toBeNull();
    // 설명 문구는 두지 않는다.
    expect(screen.queryByText(/3D 구역을 클릭해 선택/)).toBeNull();
    expect(screen.queryByRole('region', { name: '구역 목록 선택' })).toBeNull();
    expect(screen.queryByRole('group', { name: 'World 수치 입력' })).toBeNull();

    const list = openZoneList();
    expect(within(list).getByRole('button', { name: '새 구역' }).getAttribute('aria-pressed')).toBe(
      'true',
    );
    const searchInput = screen.getByLabelText('구역 검색');
    fireEvent.change(searchInput, { target: { value: '북동' } });
    expect(within(list).getByRole('button', { name: /z-04/ })).toBeTruthy();
    expect(within(list).queryByRole('button', { name: /z-02/ })).toBeNull();
  });

  it('한 편집 세션에서 도구 탭 전환 없이 XY·폭/깊이·높이 수치를 누적하고, 숫자 칸 Esc 는 칸 값만 되돌린다', async () => {
    renderZones();
    await screen.findByTestId('viewer3d');

    act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
    act(() => screen.getByRole('button', { name: '구역 수정' }).click());
    act(() => screen.getByRole('button', { name: /정밀 입력/ }).click());

    const originX = screen.getByLabelText('시작 X (m)') as HTMLInputElement;
    const width = screen.getByLabelText('폭 X (m)') as HTMLInputElement;
    const zMax = screen.getByLabelText('상단 Z (m)') as HTMLInputElement;

    // 타이핑 후 디바운스만으로 로컬 3D 미리보기가 자동 갱신된다(U05).
    fireEvent.focus(originX);
    fireEvent.change(originX, { target: { value: '6.2' } });
    await new Promise((r) => setTimeout(r, 260));
    expect(fake.editing?.draft.min[0]).toBe(6.2);

    // X=5000대 및 음수 origin 도 임의 클램프 없이 허용된다(U06).
    fireEvent.focus(originX);
    fireEvent.change(originX, { target: { value: '5000' } });
    fireEvent.blur(originX);
    expect(fake.editing?.draft.min[0]).toBe(5000);
    fireEvent.focus(originX);
    fireEvent.change(originX, { target: { value: '-12.5' } });
    fireEvent.blur(originX);
    expect(fake.editing?.draft.min[0]).toBe(-12.5);
    fireEvent.focus(originX);
    fireEvent.change(originX, { target: { value: '6.2' } });
    fireEvent.blur(originX);

    fireEvent.focus(width);
    fireEvent.change(width, { target: { value: '3.5' } });
    fireEvent.blur(width);
    fireEvent.focus(zMax);
    fireEvent.change(zMax, { target: { value: '3.0' } });
    fireEvent.blur(zMax);

    expect(fake.editing?.draft.min[0]).toBeCloseTo(6.2, 9);
    expect(fake.editing?.draft.max[0]).toBeCloseTo(9.7, 9);
    expect(fake.editing?.draft.max[2]).toBe(3);

    // 잘못된 값(음수 폭·교차 높이·문자열)은 거부하고 마지막 유효 초안을 유지한다.
    fireEvent.change(width, { target: { value: '-1' } });
    fireEvent.keyDown(width, { key: 'Enter' });
    expect(screen.getByRole('alert').textContent).toContain('0보다 커야');
    expect(fake.editing?.draft.max[0]).toBeCloseTo(9.7, 9);

    fireEvent.change(zMax, { target: { value: 'abc' } });
    fireEvent.keyDown(zMax, { key: 'Enter' });
    expect(screen.getByRole('alert').textContent).toContain('유효한 숫자');

    // 숫자 칸 Esc 는 세션을 취소하지 않고 그 칸 입력만 현재 초안 값(3)으로 되돌린다.
    fireEvent.keyDown(zMax, { key: 'Escape' });
    expect(zMax.value).toBe('3');
    expect(fake.editing).not.toBeNull();
  });

  it('미리보기 유지는 결과를 사라지게 하지 않고 3D Ghost·계속 편집·원본 복원·저장을 표시한다', async () => {
    renderZones();
    await screen.findByTestId('viewer3d');

    act(() => fake.onSelect?.({ type: 'zone', id: 'z-02' }));
    act(() => screen.getByRole('button', { name: '구역 수정' }).click());
    act(() => screen.getByRole('button', { name: /정밀 입력/ }).click());
    const zMin = screen.getByLabelText('하단 Z (m)') as HTMLInputElement;
    fireEvent.change(zMin, { target: { value: '0.9' } });
    fireEvent.blur(zMin);

    act(() => screen.getByRole('button', { name: '미리보기 유지' }).click());
    expect(fake.overlay).toEqual({
      target: { type: 'zone', id: 'z-02' },
      box: { min: [2.2, 2.5, 0.9], max: [5.4, 5.1, 2.4] },
    });
    const saveBtn = screen.getByRole('button', { name: '저장' });
    expect((saveBtn as HTMLButtonElement).disabled).toBe(false);
    expect(document.body.textContent).not.toContain('서버 미연결');
    expect(document.body.textContent).not.toContain('초안 확정');
    expect(document.body.textContent).not.toContain('BE -120');

    // 계속 편집을 누르면 보관된 미리보기 값(zMin 0.9)에서 다시 편집을 이어 간다.
    act(() => screen.getByRole('button', { name: '계속 편집' }).click());
    expect(fake.editing?.draft).toEqual({ min: [2.2, 2.5, 0.9], max: [5.4, 5.1, 2.4] });

    act(() => screen.getByRole('button', { name: '미리보기 유지' }).click());
    act(() => screen.getByRole('button', { name: '원본 복원' }).click());
    expect(fake.overlay).toBeNull();
  });

  describe('저장', () => {
    const calls: { path: string; method?: string; body?: Record<string, unknown> }[] = [];
    const answer = (zone: Record<string, unknown>) => {
      apiMock.fn = (path, options) => {
        const { method, body } = options as { method?: string; body?: Record<string, unknown> };
        calls.push({ path: path as string, method, body });
        return Promise.resolve(zone);
      };
    };
    // 서버(BE -120)가 돌려주는 새 버전.
    const saved = (zone_id: string, zone_version: number) => ({
      zone_id,
      zone_version,
      name: '저장됨',
      zone_kind: 'OPENING',
      eval_type: 'VOLUME_3D',
      polygon_xy: [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1],
      ],
      z_min_m: 0,
      z_max_m: 1,
      severity: 'HIGH',
      requirements: null,
      has_guardrail: null,
      valid_from: 500,
      valid_to: null,
    });
    beforeEach(() => {
      calls.length = 0;
    });

    it('속성만 고치면 받은 형상과 고치기 시작한 버전을 함께 보내고 저장한 버전을 알린다', async () => {
      answer(saved('z-04', 4));
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));

      const save = screen.getByRole('button', { name: '저장' }) as HTMLButtonElement;
      expect(save.disabled).toBe(true);
      fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
        target: { value: '북동 개구부' },
      });
      fireEvent.change(screen.getByRole('combobox', { name: '심각도' }), {
        target: { value: 'HIGH' },
      });
      expect(save.disabled).toBe(false);
      act(() => save.click());

      expect(await screen.findByText('저장했습니다 (z-04 v4).')).toBeTruthy();
      expect(calls).toEqual([
        {
          path: '/api/zones/z-04',
          method: 'PUT',
          body: {
            name: '북동 개구부',
            zone_kind: 'OPENING',
            eval_type: 'VOLUME_3D',
            polygon_xy: DEFAULT_ZONES[1]!.polygon,
            z_min_m: 0,
            z_max_m: 2.4,
            severity: 'HIGH',
            has_guardrail: null,
            requirements: null,
          },
        },
      ]);
    });

    it('미리보기로 보관한 형상을 저장하면 그 형상을 보내고, 저장 뒤 보관 초안과 Ghost 를 정리한다', async () => {
      answer(saved('z-04', 4));
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      act(() => screen.getByRole('button', { name: '구역 수정' }).click());
      act(() => screen.getByRole('button', { name: /정밀 입력/ }).click());
      const originX = screen.getByLabelText('시작 X (m)') as HTMLInputElement;
      fireEvent.change(originX, { target: { value: '6.2' } });
      fireEvent.blur(originX);
      act(() => screen.getByRole('button', { name: '미리보기 유지' }).click());
      expect(fake.overlay).not.toBeNull();

      act(() => screen.getByRole('button', { name: '저장' }).click());
      expect(await screen.findByText('저장했습니다 (z-04 v4).')).toBeTruthy();
      const [[x0]] = calls[0]!.body!.polygon_xy as [number, number][];
      expect(x0).toBeCloseTo(6.2, 9);
      expect(useZoneDraftsStore.getState().drafts).toEqual({});
      expect(fake.overlay).toBeNull();
    });

    it('409 면 고친 칸은 남기고 고치지 않은 칸은 다시 받은 최신 값을 보이며, 다시 저장해도 그 값을 덮지 않는다', async () => {
      apiMock.fn = (path, options) => {
        const { method, body } = options as { method?: string; body?: Record<string, unknown> };
        calls.push({ path, method, body });
        return Promise.reject(new ApiError(409));
      };
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      const name = screen.getByRole('textbox', { name: '이름' }) as HTMLInputElement;
      fireEvent.change(name, { target: { value: '북동 개구부' } });
      act(() => screen.getByRole('button', { name: '저장' }).click());
      expect(await screen.findByText(/그 사이 다른 버전이 저장되어/)).toBeTruthy();

      // 다른 관리자가 심각도를 INFO 로 바꾼 v4 를 다시 받았다.
      mockZonesState.zones = [
        DEFAULT_ZONES[0]!,
        { ...DEFAULT_ZONES[1]!, version: 4, severity: 'INFO' },
      ];
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      expect(name.value).toBe('북동 개구부');
      expect((screen.getByRole('combobox', { name: '심각도' }) as HTMLSelectElement).value).toBe(
        'INFO',
      );

      answer(saved('z-04', 5));
      act(() => screen.getByRole('button', { name: '저장' }).click());
      expect(await screen.findByText('저장했습니다 (z-04 v5).')).toBeTruthy();
      expect(calls[1]!.body).toMatchObject({ name: '북동 개구부', severity: 'INFO' });
    });

    it.each([
      [new ApiError(403), '구역을 저장할 권한이 없습니다.'],
      [new ApiError(400), '저장하지 못했습니다. 다시 시도하세요.'],
      [new ApiError(0, { kind: 'NETWORK' }), /저장됐는지 확인하지 못해/],
    ])('저장이 %s 로 실패하면 이유를 알리고 고친 속성은 둔다', async (error, text) => {
      apiMock.fn = () => Promise.reject(error);
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      const name = screen.getByRole('textbox', { name: '이름' }) as HTMLInputElement;
      fireEvent.change(name, { target: { value: '북동 개구부' } });
      act(() => screen.getByRole('button', { name: '저장' }).click());
      expect(await screen.findByText(text)).toBeTruthy();
      expect(name.value).toBe('북동 개구부');
    });

    it('저장 중에는 버튼을 끄고 다시 눌러도 한 번만 보낸다', async () => {
      apiMock.fn = (path, options) => {
        calls.push({ path, method: (options as { method?: string }).method });
        return new Promise(() => {});
      };
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
        target: { value: '북동 개구부' },
      });
      const save = screen.getByRole('button', { name: '저장' }) as HTMLButtonElement;
      // isPending 이 바뀌기 전(같은 턴)에 두 번 누른다.
      act(() => {
        save.click();
        save.click();
      });
      await waitFor(() => expect(save.textContent).toContain('저장 중'));
      expect(save.disabled).toBe(true);
      await waitFor(() => expect(calls).toHaveLength(1));

      // 드롭다운에서 「새 구역」을 고르면 새 구역 양식이 보인다. 같은 틱에 두 번 눌러도 한 번만 만든다.
      pickNewZone();
      chooseNewZoneType('OPENING');
      act(() => {
        addZoneButton().click();
        addZoneButton().click();
      });
      await waitFor(() => expect(calls).toHaveLength(2));
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
      expect(calls.map((c) => c.method)).toEqual(['PUT', 'POST']);
    });

    it('직사각형이 아니고 높이가 없는 구역은 속성만 고쳐 저장해도 받은 다각형·높이·유형을 그대로 보내고, 입체로 바꾸지 못한다', async () => {
      const triangle = zoneOf(
        { zoneId: 'z-07', name: '삼각 바닥', min: [0, 0, 0], max: [4, 3, 0.05] },
        {
          judgeType: 'GROUND_FOOTPRINT',
          zoneType: 'CUSTOM_X',
          polygon: [
            [0, 0],
            [4, 0],
            [0, 3],
          ],
          zBounds: [null, null],
          zRange: null,
          shapeEditable: false,
        },
      );
      mockZonesState.zones = [...DEFAULT_ZONES, triangle];
      answer(saved('z-07', 2));
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-07' }));

      const kind = screen.getByRole('combobox', { name: '유형' }) as HTMLSelectElement;
      expect(kind.value).toBe('CUSTOM_X');
      fireEvent.change(screen.getByRole('combobox', { name: '판정 유형' }), {
        target: { value: 'VOLUME_3D' },
      });
      expect(screen.getByRole('alert').textContent).toBe(
        '높이가 없는 구역은 입체로 판정할 수 없습니다.',
      );
      expect((screen.getByRole('button', { name: '저장' }) as HTMLButtonElement).disabled).toBe(
        true,
      );
      fireEvent.change(screen.getByRole('combobox', { name: '판정 유형' }), {
        target: { value: 'GROUND_FOOTPRINT' },
      });

      fireEvent.change(screen.getByRole('textbox', { name: '이름' }), {
        target: { value: '삼각 바닥 구역' },
      });
      act(() => screen.getByRole('button', { name: '저장' }).click());
      expect(await screen.findByText('저장했습니다 (z-07 v2).')).toBeTruthy();
      expect(calls[0]!.body).toMatchObject({
        name: '삼각 바닥 구역',
        eval_type: 'GROUND_FOOTPRINT',
        zone_kind: 'CUSTOM_X',
        polygon_xy: [
          [0, 0],
          [4, 0],
          [0, 3],
        ],
        z_min_m: null,
        z_max_m: null,
      });
    });

    it('구역 목록을 받기 전에는 새 구역을 만들 수 없다', async () => {
      mockZonesState.raw = undefined;
      renderZones();
      await screen.findByTestId('viewer3d');
      chooseNewZoneType('OPENING');
      expect(addZoneButton().disabled).toBe(true);
    });

    it('형상을 고치는 중에 저장하면 편집을 끝내고 그 형상을 함께 보낸다', async () => {
      answer(saved('z-04', 4));
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      act(() => screen.getByRole('button', { name: '구역 수정' }).click());
      act(() => screen.getByRole('button', { name: /정밀 입력/ }).click());
      const originX = screen.getByLabelText('시작 X (m)') as HTMLInputElement;
      fireEvent.change(originX, { target: { value: '6.2' } });
      fireEvent.blur(originX);

      act(() => screen.getByRole('button', { name: '저장' }).click());
      expect(fake.editing).toBeNull();
      expect(await screen.findByText('저장했습니다 (z-04 v4).')).toBeTruthy();
      expect(useZoneDraftsStore.getState().drafts).toEqual({});
      expect(fake.overlay).toBeNull();
      const body = calls[0]!.body!;
      expect(body).toMatchObject({ z_min_m: 0, z_max_m: 2.4 });
      const [[x0, y0], [x1], [, y1]] = body.polygon_xy as number[][] as [number, number][];
      expect([x0, y0, y1]).toEqual([expect.closeTo(6.2, 9), 1.2, 3.8]);
      expect(x1).toBeCloseTo(9, 9);
    });

    it('저장하지 못하면 이유를 알리고 고친 속성은 그대로 둔다', async () => {
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      const name = screen.getByRole('textbox', { name: '이름' }) as HTMLInputElement;
      fireEvent.change(name, { target: { value: '북동 개구부' } });
      act(() => screen.getByRole('button', { name: '저장' }).click());

      expect(await screen.findByText('저장하지 못했습니다. 다시 시도하세요.')).toBeTruthy();
      expect(name.value).toBe('북동 개구부');
      act(() => screen.getByRole('button', { name: '원본 복원' }).click());
      expect(name.value).toBe('북동 위험 구역');
    });

    it('이름을 비우면 저장할 수 없다', async () => {
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      fireEvent.change(screen.getByRole('textbox', { name: '이름' }), { target: { value: ' ' } });
      expect(screen.getByRole('alert').textContent).toBe('이름을 입력하세요.');
      expect((screen.getByRole('button', { name: '저장' }) as HTMLButtonElement).disabled).toBe(
        true,
      );
    });

    it('구역을 고르지 않으면 새 구역 양식이 보이고, 유형을 고르면 그 속성으로 기존 구역들 가운데에 만들고 바로 형상 편집을 시작한다', async () => {
      const created = zoneOf({ zoneId: 'z-05', name: '새 구역', min: [4, 2, 0], max: [6, 4, 2] });
      apiMock.fn = (path, options) => {
        const { method, body } = options as { method?: string; body?: Record<string, unknown> };
        calls.push({ path: path as string, method, body });
        mockZonesState.zones = [...DEFAULT_ZONES, created];
        return Promise.resolve(saved('z-05', 1));
      };
      renderZones();
      await screen.findByTestId('viewer3d');
      // 유형은 비어 있어 고르기 전에는 만들 수 없다(개구부를 기본으로 두지 않는다).
      expect(addZoneButton().disabled).toBe(true);
      expect(screen.getByText('유형을 고르세요.')).toBeTruthy();
      chooseNewZoneType('EQUIPMENT_PARKING');
      expect(addZoneButton().disabled).toBe(false);
      act(() => addZoneButton().click());

      await waitFor(() => expect(fake.editing?.target).toEqual({ type: 'zone', id: 'z-05' }));
      expect(calls[0]).toMatchObject({ path: '/api/zones', method: 'POST' });
      expect(calls[0]!.body).toMatchObject({
        name: '새 구역',
        zone_kind: 'EQUIPMENT_PARKING',
        z_min_m: 0,
        z_max_m: 2,
      });
    });

    it('관리자가 아니면 속성을 읽기만 하고 저장·새 구역 버튼이 없다', async () => {
      signInAs('VIEWER');
      renderZones();
      await screen.findByTestId('viewer3d');
      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));

      expect(screen.getByLabelText('구역 속성').textContent).toContain('개구부');
      expect(screen.getByLabelText('구역 속성').textContent).toContain('CRITICAL');
      expect(screen.queryByRole('textbox', { name: '이름' })).toBeNull();
      expect(screen.queryByRole('button', { name: '저장' })).toBeNull();
      // 관리자가 아니어도 드롭다운 「새 구역」으로 선택을 풀 수 있다. 새 구역 양식은 없다.
      pickNewZone();
      expect(screen.getByRole('button', { name: '구역 목록: 새 구역' })).toBeTruthy();
      expect(screen.queryByLabelText('구역 속성')).toBeNull();
      expect(screen.queryByRole('region', { name: '새 구역' })).toBeNull();
      expect(screen.queryByRole('button', { name: '구역 추가' })).toBeNull();
    });
  });

  describe('현장 적용', () => {
    let published: string[];
    beforeEach(() => {
      published = [];
      // z-04 는 cfg-12(issued_at 100)보다 늦게(200) 저장해 발행 전이다.
      mockZonesState.zones = DEFAULT_ZONES.map((z) =>
        z.zoneId === 'z-04' ? { ...z, validFrom: 200 } : z,
      );
      apiMock.fn = (path, options) => {
        if (path === '/api/config-bundles' && (options as { method?: string }).method === 'POST') {
          published.push(path);
          return Promise.resolve(bundleOf('cfg-13', 300, 'PENDING'));
        }
        return Promise.reject(new Error(`no api in test: ${path}`));
      };
    });
    const status = () => screen.getByRole('group', { name: '현장 반영' }).textContent;

    it('현장에 반영하지 않은 저장 건수를 쉬운 말로 보이고(묶음 번호는 보이지 않는다) 목록에서 그 구역에 표시한다', async () => {
      renderZones();
      await screen.findByTestId('viewer3d');
      await waitFor(() => expect(status()).toContain('현장 미반영 1건'));
      expect(status()).not.toContain('cfg-12');

      act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
      const list = openZoneList();
      const row = within(list).getByRole('button', { name: /z-04/ });
      expect(row.textContent).toContain('v3');
      expect(row.textContent).toContain('발행 전');
      expect(within(list).getByRole('button', { name: /z-02/ }).textContent).not.toContain(
        '발행 전',
      );
    });

    it('현장에 반영하면 보내는 중으로 보이고 미반영 건수가 사라진다', async () => {
      renderZones();
      await screen.findByTestId('viewer3d');
      await waitFor(() => expect(status()).toContain('현장 미반영 1건'));

      act(() => screen.getByRole('button', { name: '현장에 반영' }).click());
      await waitFor(() => expect(status()).toContain('현장에 보내는 중'));
      expect(published).toEqual(['/api/config-bundles']);
      expect(status()).not.toContain('미반영');
      expect(
        (screen.getByRole('button', { name: '현장에 반영' }) as HTMLButtonElement).disabled,
      ).toBe(true);
    });

    it('현장에 반영하지 못하면 이유를 알린다', async () => {
      apiMock.fn = () => Promise.reject(new ApiError(403));
      renderZones();
      await screen.findByTestId('viewer3d');
      await waitFor(() => expect(status()).toContain('현장 미반영 1건'));
      act(() => screen.getByRole('button', { name: '현장에 반영' }).click());
      expect(await screen.findByText('현장에 반영할 권한이 없습니다.')).toBeTruthy();
    });

    it('Tier 0 가 묶음을 거부하면 현장이 거부함으로 보이고, 미반영 구역이 없어도 다시 반영할 수 있다', async () => {
      bundlesMock.list = [
        bundleOf('cfg-13', 300, 'REJECTED', 'cfg-12'),
        bundleOf('cfg-12', 100, 'APPLIED'),
      ];
      renderZones();
      await screen.findByTestId('viewer3d');
      await waitFor(() => expect(status()).toContain('현장이 거부함'));
      expect(status()).not.toContain('현장 반영됨');
      expect(status()).not.toContain('미반영');
      expect(
        (screen.getByRole('button', { name: '현장에 반영' }) as HTMLButtonElement).disabled,
      ).toBe(false);
    });

    it('관리자가 아니면 상태만 보고 반영하지 못한다', async () => {
      signInAs('FOREMAN');
      renderZones();
      await screen.findByTestId('viewer3d');
      await waitFor(() => expect(status()).toContain('현장 미반영 1건'));
      expect(screen.queryByRole('button', { name: '현장에 반영' })).toBeNull();
    });
  });

  it('초안 이어 편집 6단계 중 중간 단계가 실패하면 부분 적용을 취소하고 실패를 알린다(R03)', async () => {
    renderZones();
    await screen.findByTestId('viewer3d');

    act(() => fake.onSelect?.({ type: 'zone', id: 'z-02' }));
    act(() => screen.getByRole('button', { name: '구역 수정' }).click());
    act(() => screen.getByRole('button', { name: /정밀 입력/ }).click());
    const originX = screen.getByLabelText('시작 X (m)') as HTMLInputElement;
    fireEvent.change(originX, { target: { value: '3.0' } });
    fireEvent.blur(originX);
    act(() => screen.getByRole('button', { name: '미리보기 유지' }).click());

    fake.updateStepCount = 0;
    fake.failAtUpdateStep = 4;
    act(() => screen.getByRole('button', { name: '계속 편집' }).click());
    expect(fake.editing).toBeNull();
    expect(screen.getByRole('status').textContent).toContain('초안 좌표를 복원하지 못해');
  });

  it('2D 보기에서 구역 수정을 누르면 NOT_READY 경고 없이 3D 로 전환된 뒤 편집을 시작하고, 비관리자는 차단한다(R02, R05)', async () => {
    renderZones();
    await screen.findByTestId('viewer3d');

    act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
    // 3D 가 멈추면 2D 로 바뀐다(손으로 2D 로 바꾸는 버튼은 없다).
    act(() =>
      fake.onStatus?.({
        phase: 'error',
        mode: 'LIVE',
        error: { code: 'RENDER_FAILED', message: 'x' },
      } as ViewerStatus),
    );
    expect(screen.queryByTestId('viewer3d')).toBeNull();
    act(() => screen.getByRole('button', { name: '구역 수정' }).click());
    await screen.findByTestId('viewer3d');
    expect(fake.editing?.target).toEqual({ type: 'zone', id: 'z-04' });
    expect(screen.queryByRole('status')).toBeNull();

    act(() => signInAs('VIEWER'));
    expect(fake.editing).toBeNull();
    expect(screen.queryByRole('button', { name: '구역 수정' })).toBeNull();
    // 비관리자에게는 편집 영역도, 설명 문구도 그리지 않는다.
    expect(screen.queryByText(/관리자/)).toBeNull();
    expect(screen.queryByRole('button', { name: /계속 편집|미리보기 유지/ })).toBeNull();
  });

  it('Scene 버전 변경·원본 구역 형상 변경·Source(계정) 변경 시 오래된 미저장 미리보기를 즉시 폐기한다', async () => {
    const qc = new QueryClient();
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <SpatialViewportHost loadViewer={loadFake} zoneEditor>
          <ZonesPage />
        </SpatialViewportHost>
      </QueryClientProvider>,
    );
    await screen.findByTestId('viewer3d');

    act(() => fake.onSelect?.({ type: 'zone', id: 'z-04' }));
    act(() => screen.getByRole('button', { name: '구역 수정' }).click());
    const moved = {
      ...fake.editing!,
      draft: {
        ...fake.editing!.draft,
        min: [6.3, 1.2, 0] as [number, number, number],
        max: [9.1, 3.8, 2.4] as [number, number, number],
      },
    };
    fake.editing = moved;
    act(() => screen.getByRole('button', { name: '미리보기 유지' }).click());
    expect(fake.overlay).not.toBeNull();

    // Scene 버전 변경 → 미리보기 폐기
    act(() => {
      const baseWorld = worldStateAt(Date.now()).payload;
      useRealtimeStore.setState({
        world: {
          payload: { ...baseWorld, scene_version: 'scene-4' },
          receivedAt: Date.now(),
        },
      });
    });
    expect(fake.overlay).toBeNull();
    expect(screen.getByRole('status').textContent).toContain('Scene 버전이 바뀌어');

    // 다시 미리보기 생성 후 Source 변경(로그아웃/다른 계정) → 선택과 미리보기 모두 초기화
    act(() => screen.getByRole('button', { name: '구역 수정' }).click());
    fake.editing!.draft = { min: [6.3, 1.2, 0], max: [9.1, 3.8, 2.4] };
    act(() => screen.getByRole('button', { name: '미리보기 유지' }).click());
    expect(fake.overlay).not.toBeNull();
    act(() => {
      useRealtimeStore.setState({ source: { id: 's-2', pushZones: vi.fn() } as never });
    });
    rerender(
      <QueryClientProvider client={qc}>
        <SpatialViewportHost loadViewer={loadFake} zoneEditor>
          <ZonesPage />
        </SpatialViewportHost>
      </QueryClientProvider>,
    );
    expect(fake.overlay).toBeNull();
  });

  it('늦은 GET /api/zones 응답이 도착하기 전에는 재시도 횟수를 소진하지 않고 도착 즉시 ?zone=z-04&edit=1 을 선택·편집한다', async () => {
    mockZonesState.zones = [];
    mockZonesState.raw = undefined;
    const onConsumed = vi.fn();
    const qc = new QueryClient();
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <SpatialViewportHost loadViewer={loadFake} zoneEditor>
          <ZonesPage initialZoneId="z-04" autoStartEdit onSearchConsumed={onConsumed} />
        </SpatialViewportHost>
      </QueryClientProvider>,
    );
    await screen.findByTestId('viewer3d');
    expect(fake.selectCalls).toBe(0);

    mockZonesState.zones = [...DEFAULT_ZONES];
    mockZonesState.raw = { contract: 'DEMO_UNCONFIRMED', zones: [] };
    rerender(
      <QueryClientProvider client={qc}>
        <SpatialViewportHost loadViewer={loadFake} zoneEditor>
          <ZonesPage initialZoneId="z-04" autoStartEdit onSearchConsumed={onConsumed} />
        </SpatialViewportHost>
      </QueryClientProvider>,
    );
    expect(fake.selectCalls).toBe(1);
    expect(fake.focusCalls).toBe(1);
    expect(fake.editing?.target).toEqual({ type: 'zone', id: 'z-04' });
    expect(onConsumed).toHaveBeenCalledTimes(1);
  });

  it('parseZonesSearch 는 유효한 zone·edit 만 남기고 잘못된 값은 버린다', () => {
    expect(parseZonesSearch({ zone: ' z-04 ', edit: '1' })).toEqual({ zone: 'z-04', edit: '1' });
    expect(parseZonesSearch({ zone: 'bad id!', edit: '1' })).toEqual({});
    expect(parseZonesSearch({})).toEqual({});
  });
});
