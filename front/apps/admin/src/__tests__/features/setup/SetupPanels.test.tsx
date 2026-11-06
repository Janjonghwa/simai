import { useCctvStore } from '@/features/cctv/cctv.store';
import { deriveStep1Coverage } from '@/features/setup/setup';
import { CameraMapPanel, CameraPanel } from '@/features/setup/SetupPanels';
import type { CoverageGuidesResponse } from '@/features/setup/types';
import { buildCoverageGuidePayload } from '@/mocks/setup';
import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/features/cctv/useCctvPreview', () => ({
  useCctvPreview: () => ({ attach: vi.fn() }),
}));

afterEach(() => {
  cleanup();
  useCctvStore.getState().reset();
});

/** 기본 카메라 4대(cam1~cam4)로 만든 1단계 카메라 목록. */
const defaultCoverage = () => deriveStep1Coverage(undefined);

describe('세팅 카메라 지도와 영상', () => {
  it.each([false, true])(
    '실시간 연결 %s 에서도 영상 4칸을 지도 양옆에 그 카메라 모서리 높이대로 놓는다',
    (live) => {
      if (live) {
        useCctvStore.getState().hello(['cam1', 'cam2', 'cam3', 'cam4']);
        useCctvStore.getState().setLink('open');
      }
      render(<CameraMapPanel coverage={defaultCoverage()} />);
      const canvasIds = (group: HTMLElement) =>
        within(group)
          .getAllByLabelText(/영상$/)
          .map((canvas) => canvas.getAttribute('aria-label'));
      // 위쪽 모서리(cam1·cam2) 영상이 위, 아래쪽 모서리(cam4·cam3) 영상이 아래다.
      expect(canvasIds(screen.getByRole('group', { name: '왼쪽 카메라 영상' }))).toEqual([
        'cam1 영상',
        'cam4 영상',
      ]);
      expect(canvasIds(screen.getByRole('group', { name: '오른쪽 카메라 영상' }))).toEqual([
        'cam2 영상',
        'cam3 영상',
      ]);
      expect(screen.getByRole('img', { name: /위에서 본 보정 화면/ })).toBeTruthy();
      expect(screen.getAllByRole('heading', { name: '카메라 시야 · 실시간 영상' })).toHaveLength(1);
    },
  );

  it('영상이 오지 않는 칸은 흐리게 두고 상태를 글로 보인다', () => {
    render(<CameraMapPanel coverage={defaultCoverage()} />);
    const tile = screen.getByLabelText('cam1 영상');
    expect(tile.className).toContain('opacity-45');
    expect(tile.parentElement!.textContent).toContain('cam1');
  });

  it('영상 칸은 누르는 단추가 아니다(크게 보기 없음)', () => {
    render(<CameraMapPanel coverage={defaultCoverage()} />);
    expect(screen.queryByRole('button', { name: /크게 보기|작게 보기/ })).toBeNull();
  });
});

const latest = buildCoverageGuidePayload({ connectivity: 'CONNECTED', guides: [] });
const fresh: CoverageGuidesResponse = {
  calibration_version: null,
  guides: [],
  site_id: 'classroom-1',
  status: 'OK',
  stale: false,
  received_at_ms: 100,
  age_ms: 0,
  latest,
};

describe('카메라 각도 표시', () => {
  it.each([
    { label: '확인 중', response: undefined, error: false, expected: '—' },
    { label: '계산 요청 실패', response: fresh, error: true, expected: '—' },
    {
      label: '결과 없음',
      response: { ...fresh, status: 'EMPTY' as const, latest: null },
      error: false,
      expected: '—',
    },
    {
      label: '응답 STALE',
      response: { ...fresh, status: 'STALE' as const },
      error: false,
      expected: '—',
    },
    { label: 'stale 플래그', response: { ...fresh, stale: true }, error: false, expected: '—' },
    {
      label: '계산 불가',
      response: { ...fresh, latest: { ...latest, connectivity: 'UNKNOWN' as const } },
      error: false,
      expected: '—',
    },
    ...(['STALE', 'NO_VIDEO', 'OK'] as const).map((status) => ({
      label: `카메라 ${status}`,
      response: {
        ...fresh,
        latest: buildCoverageGuidePayload({
          connectivity: 'CONNECTED',
          guides: [],
          cameraStatuses: { cam1: status },
        }),
      },
      error: false,
      expected: status === 'OK' ? '각도 양호' : '—',
    })),
    {
      label: 'OK + 회전 안내',
      response: {
        ...fresh,
        latest: buildCoverageGuidePayload({
          connectivity: 'DISCONNECTED',
          guides: [
            {
              camera_id: 'cam1',
              pair: ['cam1', 'cam2'],
              direction: 'RIGHT',
              magnitude: 'SMALL',
              reason_code: 'WEAK_PAIR_EDGE_CLUSTER',
            },
          ],
        }),
      },
      error: false,
      expected: 'cam1 카메라를 오른쪽으로 조금 돌려 주세요 (cam1–cam2)',
    },
    {
      label: 'CONNECTED + 선택 보강만',
      response: {
        ...fresh,
        latest: buildCoverageGuidePayload({
          guides: [
            {
              camera_id: 'cam1',
              pair: ['cam1', 'cam2'],
              direction: 'RIGHT',
              magnitude: 'SMALL',
              reason_code: 'OPTIONAL_IMPROVE',
            },
          ],
        }),
      },
      error: false,
      expected: '각도 양호',
    },
  ])('$label', ({ response, error, expected }) => {
    render(<CameraPanel coverage={deriveStep1Coverage(response, error)} />);
    const row = screen.getByText('cam1', { selector: 'b' }).closest('li')!;
    expect(within(row).getByText(expected)).toBeTruthy();
    if (expected !== '각도 양호') expect(within(row).queryByText('각도 양호')).toBeNull();
  });
});
