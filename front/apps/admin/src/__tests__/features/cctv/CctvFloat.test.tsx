import { CctvFloat } from '@/features/cctv/CctvFloat';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import { useRealtimeStore } from '@/store/realtime.store';
import { useSessionStore } from '@/store/session.store';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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

describe('떠 있는 CCTV', () => {
  it('카메라와 4분할을 바꾸고 닫았다 열어도 선택을 기억한다', () => {
    const close = vi.fn();
    const view = render(<CctvFloat selectedKey={null} onClose={close} />);
    fireEvent.click(screen.getByRole('button', { name: 'cam_B' }));
    expect(screen.getByLabelText('cam_B 영상')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '4분할' }));
    expect(screen.getByLabelText('cam_A 영상')).toBeTruthy();
    expect(screen.getByLabelText('cam_B 영상')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '떠 있는 CCTV 닫기' }));
    expect(close).toHaveBeenCalledOnce();
    view.unmount();
    expect(stopped).toHaveBeenCalledOnce();
    render(<CctvFloat selectedKey={null} onClose={close} />);
    expect(screen.getByRole('button', { name: '4분할' }).getAttribute('aria-pressed')).toBe('true');
  });

  it('저장한 위치와 크기를 복원하고 접으면 연결을 정리한다', () => {
    localStorage.setItem(
      'simai.cctv.float',
      JSON.stringify({ spot: { left: 120, offset: 140 }, width: 520, height: 400 }),
    );
    render(<CctvFloat selectedKey={null} onClose={vi.fn()} />);
    const box = screen.getByLabelText('떠 있는 CCTV');
    expect(box.style.left).toBe('120px');
    expect(box.style.top).toBe('140px');
    expect(box.style.width).toBe('520px');
    fireEvent.click(screen.getByRole('button', { name: 'CCTV 접기' }));
    expect(stopped).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'CCTV 펼치기' }));
    expect(screen.getByLabelText('cam_A 영상')).toBeTruthy();
  });

  it('선택한 사람의 카메라를 연다', () => {
    useRealtimeStore.setState({
      world: {
        payload: { tracks: [{ world_id: 'p1', cameras: ['cam_B'] }] } as never,
        receivedAt: Date.now(),
      },
    });
    render(<CctvFloat selectedKey="person:p1" onClose={vi.fn()} />);
    expect(screen.getByLabelText('cam_B 영상')).toBeTruthy();
  });

  it('3D 카메라 마커 클릭 요청(requestedCamera)으로 열리거나 이미 열린 창의 카메라를 바꾸고 4분할 버튼도 유지한다', () => {
    const view = render(
      <CctvFloat
        selectedKey={null}
        requestedCamera={{ cameraId: 'cam_B', seq: 1 }}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('cam_B 영상')).toBeTruthy();
    expect(screen.queryByLabelText('cam_A 영상')).toBeNull();

    // 4분할로 전환했다가 다시 카메라 마커 클릭 요청이 오면 해당 카메라 단일 보기로 전환한다
    fireEvent.click(screen.getByRole('button', { name: '4분할' }));
    expect(screen.getByLabelText('cam_A 영상')).toBeTruthy();
    expect(screen.getByLabelText('cam_B 영상')).toBeTruthy();

    view.rerender(
      <CctvFloat
        selectedKey={null}
        requestedCamera={{ cameraId: 'cam_A', seq: 2 }}
        onClose={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('cam_A 영상')).toBeTruthy();
    expect(screen.queryByLabelText('cam_B 영상')).toBeNull();
  });
});
