import { MINIMAP_SIZE_KEY, PlanMinimap } from '@/features/plan/PlanMinimap';
import { TBM_WORKS_KEY } from '@/features/tbm/api';
import { todayIsoDate } from '@/features/tbm/taskInput';
import type { TbmWorkUnit } from '@/features/tbm/types';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 3D 관제 왼쪽 아래 2D 미니맵: 기본 크기는 칼럼 폭의 정사각형(--minimap-size)이고, 오른쪽 위 모서리 손잡이를 끌어
// 비율 고정으로 크기를 바꿀 수 있다(-829). 누르면 큰 창이 뜬다.
// 평면 그림(LivePlan)은 따로 시험하므로 자리표시자로 바꾼다.
vi.mock('@/features/plan/LivePlan', () => ({
  LivePlan: ({ fit, onSelect }: { fit?: boolean; onSelect?: unknown }) => (
    <div data-testid="live-plan" data-fit={String(Boolean(fit))} data-select={String(!!onSelect)} />
  ),
}));

const opener = () => screen.getByRole('button', { name: '2D 평면 크게 보기' });
const grip = () => screen.getByTestId('plan-minimap-grip');

/** works 가 있으면 오늘 작업 목록을 이미 받은 것으로 한다(대기 화면 여부를 캐시에서 읽는다). */
function renderMinimap(works?: TbmWorkUnit[]) {
  const client = new QueryClient();
  if (works) client.setQueryData(TBM_WORKS_KEY, works);
  render(
    <QueryClientProvider client={client}>
      <PlanMinimap selectedKey={null} onSelect={() => {}} />
    </QueryClientProvider>,
  );
  return screen.getByTestId('plan-minimap');
}

const work = (status: TbmWorkUnit['status'], progress: TbmWorkUnit['progress']): TbmWorkUnit => ({
  work_id: '1',
  status,
  progress,
  date: todayIsoDate(),
  name: '작업',
  from_ms: 1,
  to_ms: 2,
  zone_id: null,
  place: null,
  planned_people: 1,
  lead: null,
  layout_id: null,
  work_type: null,
});

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('PlanMinimap', () => {
  it('지금 관제할 작업이 없어 대기 화면이 덮는 동안은 inert 라 Tab 으로도 눌리지 않는다', () => {
    expect(renderMinimap([work('DONE', 'ENDED')]).hasAttribute('inert')).toBe(true);
    cleanup();
    expect(renderMinimap([work('PLANNED', 'REPORT')]).hasAttribute('inert')).toBe(true);
  });

  it('작업 목록을 받기 전이거나 세팅을 마친 작업이 있으면 inert 가 아니다', () => {
    expect(renderMinimap().hasAttribute('inert')).toBe(false);
    cleanup();
    expect(renderMinimap([work('PLANNED', 'SETUP_DONE')]).hasAttribute('inert')).toBe(false);
  });

  it('왼쪽 아래에 기본 크기(--minimap-size)로 서고, 오른쪽 위 모서리 대각선 손잡이가 있다(-829)', () => {
    const minimap = renderMinimap();
    expect(minimap.className).toContain('bottom-3.5');
    expect(minimap.className).toContain('left-3.5');
    expect(minimap.className).toContain('size-(--minimap-size)');
    expect(minimap.getAttribute('style')).toBeNull();

    const handle = grip();
    expect(handle.className).toContain('cursor-nesw-resize');
    expect(handle.className).toContain('-top-1.5');
    expect(handle.className).toContain('-right-1.5');
  });

  it('누르면 큰 2D 창이 열리고 Esc 로 닫힌다', () => {
    renderMinimap();
    expect(screen.queryByTestId('plan-modal')).toBeNull();
    fireEvent.click(opener());
    expect(screen.getByTestId('plan-modal')).toBeTruthy();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('plan-modal')).toBeNull();
  });

  it('작은 칸은 구역·사람에 맞춰 비추고(fit) 고르지 않으며, 큰 창은 방 전체에서 고른다(-800)', () => {
    renderMinimap();
    const [small] = screen.getAllByTestId('live-plan');
    expect(small?.dataset.fit).toBe('true');
    expect(small?.dataset.select).toBe('false');
    fireEvent.click(opener());
    const big = screen.getAllByTestId('live-plan')[1];
    expect(big?.dataset.fit).toBe('false');
    expect(big?.dataset.select).toBe('true');
  });

  it('몸통을 끌어도 옮겨지지 않고, 클릭은 큰 창을 연다', () => {
    const minimap = renderMinimap();
    fireEvent.pointerDown(minimap, { clientX: 100, clientY: 100, button: 0, buttons: 1 });
    fireEvent.pointerMove(minimap, { clientX: 400, clientY: 250, button: 0, buttons: 1 });
    fireEvent.pointerUp(minimap, { clientX: 400, clientY: 250, button: 0 });
    expect(minimap.getAttribute('style')).toBeNull();

    fireEvent.click(opener(), { detail: 1 });
    expect(screen.getByTestId('plan-modal')).toBeTruthy();
  });

  describe('오른쪽 위 모서리 손잡이 크기 조절(-829)', () => {
    function setupScene(width = 1440, height = 900, initialSize = 260) {
      const scene = document.createElement('main');
      Object.defineProperties(scene, {
        clientWidth: { get: () => width },
        clientHeight: { get: () => height },
      });
      scene.getBoundingClientRect = () => ({ left: 0, top: 0, width, height }) as DOMRect;
      vi.spyOn(HTMLElement.prototype, 'offsetParent', 'get').mockReturnValue(scene);
      vi.spyOn(HTMLElement.prototype, 'offsetLeft', 'get').mockReturnValue(14);
      vi.spyOn(HTMLElement.prototype, 'offsetTop', 'get').mockReturnValue(
        height - 14 - initialSize,
      );
      vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(initialSize);
      vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(initialSize);
      return scene;
    }

    it('손잡이를 오른쪽 위로 끌면 정사각형 비율로 커지고, 최소(기본)·최대(짧은 변 절반)를 넘지 않으며 localStorage 에 기억한다', () => {
      const scene = setupScene(1440, 900, 260);
      const minimap = renderMinimap();
      const handle = grip();

      fireEvent.pointerDown(handle, { clientX: 274, clientY: 626, button: 0, buttons: 1 });
      fireEvent.pointerMove(handle, { clientX: 374, clientY: 526, button: 0, buttons: 1 });
      expect(minimap.style.width).toBe('360px');
      expect(minimap.style.height).toBe('360px');
      expect(scene.style.getPropertyValue('--side-column-bottom')).toBe('calc(14px + 360px + 8px)');

      // 짧은 변 절반(450px)을 넘겨 끌어도 450px 에서 멈춘다.
      fireEvent.pointerMove(handle, { clientX: 700, clientY: 200, button: 0, buttons: 1 });
      expect(minimap.style.width).toBe('450px');
      expect(minimap.style.height).toBe('450px');
      fireEvent.pointerUp(handle, { clientX: 700, clientY: 200, button: 0 });
      expect(localStorage.getItem(MINIMAP_SIZE_KEY)).toBe('450');

      // 손잡이를 눌러도 큰 2D 창이 열리지 않는다.
      fireEvent.click(handle);
      expect(screen.queryByTestId('plan-modal')).toBeNull();

      // 두 번 누르면 기본 크기로 돌아가고 저장 값도 지운다.
      fireEvent.doubleClick(handle);
      expect(minimap.style.width).toBe('');
      expect(minimap.style.height).toBe('');
      expect(localStorage.getItem(MINIMAP_SIZE_KEY)).toBeNull();
      expect(scene.style.getPropertyValue('--side-column-bottom')).toBe('');
    });

    it('손잡이를 기본 크기 이하로 줄이면 최소 크기(기본 크기)에서 멈춘다', () => {
      setupScene(1440, 900, 360);
      localStorage.setItem(MINIMAP_SIZE_KEY, '360');
      const minimap = renderMinimap();
      const handle = grip();

      fireEvent.pointerDown(handle, { clientX: 374, clientY: 526, button: 0, buttons: 1 });
      fireEvent.pointerMove(handle, { clientX: 100, clientY: 800, button: 0, buttons: 1 });
      expect(minimap.style.width).toBe('260px');
      expect(minimap.style.height).toBe('260px');
      fireEvent.pointerUp(handle, { clientX: 100, clientY: 800, button: 0 });
      expect(localStorage.getItem(MINIMAP_SIZE_KEY)).toBeNull();
    });

    it('저장한 크기를 다시 열 때 복원하고, 작은 화면에서는 그 화면 최대 크기에 맞춰 줄인다', () => {
      localStorage.setItem(MINIMAP_SIZE_KEY, '420');
      setupScene(1288, 674, 229);
      const minimap = renderMinimap();
      // 1288×674 의 짧은 변 절반은 337px 이다.
      expect(minimap.style.width).toBe('337px');
      expect(minimap.style.height).toBe('337px');
    });

    it('localStorage 읽기·쓰기가 실패해도 기본 크기로 정상 동작한다', () => {
      setupScene(1440, 900, 260);
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new Error('blocked');
      });
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
        throw new Error('quota');
      });
      const minimap = renderMinimap();
      expect(minimap.getAttribute('style')).toBeNull();

      const handle = grip();
      fireEvent.pointerDown(handle, { clientX: 274, clientY: 626, button: 0, buttons: 1 });
      fireEvent.pointerMove(handle, { clientX: 374, clientY: 526, button: 0, buttons: 1 });
      fireEvent.pointerUp(handle, { clientX: 374, clientY: 526, button: 0 });
      expect(minimap.style.width).toBe('360px');
      expect(minimap.style.height).toBe('360px');
    });
  });
});
