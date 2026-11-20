import { useThemeStore } from '@/features/layout/theme.store';
import { SettingsPage } from '@/features/settings/SettingsPage';
import { alarmSound } from '@/lib/alarmSound';
import { useSessionStore, type Role } from '@/store/session.store';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const settingsRequests: string[] = [];
const server = setupServer(
  http.get('*/api/gpu/settings', () => {
    settingsRequests.push('get');
    return HttpResponse.json({
      settings: {
        version_id: 2,
        provider: 'RUNPOD',
        preferred_gpu_types: ['NVIDIA H100 80GB HBM3'],
        preferred_regions: ['AP-JP-1'],
        cloud_type: 'SECURE',
        interruptible: false,
        max_price_per_hour_usd: null,
        auto_stop_idle_minutes: 30,
        runpod_template_id: 'tpl-simai',
        created_at_ms: 1_793_000_000_000,
      },
      providers: [{ id: 'RUNPOD', configured: true }],
    });
  }),
);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
afterEach(() => {
  cleanup();
  server.resetHandlers();
  settingsRequests.length = 0;
  useSessionStore.getState().signOut();
  useThemeStore.setState({ theme: 'dark' });
  alarmSound.setMuted(false);
});

function signInAs(role: Role) {
  useSessionStore.getState().signIn({ accessToken: 't', user: { id: 'u-1', role } });
}

function renderPage() {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <SettingsPage />
    </QueryClientProvider>,
  );
}

const sections = () => screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);

describe('SettingsPage (S15P31A101-705, -824)', () => {
  it.each([
    ['ADMIN', ['화면', '경보 소리', '고급 설정 · GPU']],
    ['FOREMAN', ['화면', '경보 소리']],
    ['VIEWER', ['화면']],
  ] as const)('%s 는 쓸 수 있는 구역만 본다', async (role, expected) => {
    signInAs(role);
    renderPage();
    if (role === 'ADMIN') await screen.findByText(/RunPod · H100 SXM · 자동 정지 30분/);
    expect(sections()).toEqual(expected);
    // GPU 설정은 관리자만 서버에 묻는다
    expect(settingsRequests.length > 0).toBe(role === 'ADMIN');
  });

  it('화면 모드를 바꾸면 테마 저장소가 바뀐다(옛 계정 메뉴의 화이트·다크 모드)', () => {
    signInAs('VIEWER');
    renderPage();
    expect((screen.getByLabelText('다크 모드') as HTMLInputElement).checked).toBe(true);
    act(() => fireEvent.click(screen.getByLabelText('화이트 모드')));
    expect(useThemeStore.getState().theme).toBe('light');
    expect(document.documentElement.classList.contains('dark')).toBe(false);
    act(() => fireEvent.click(screen.getByLabelText('다크 모드')));
    expect(useThemeStore.getState().theme).toBe('dark');
  });

  it('경보 소리는 상단바 스피커 버튼과 같은 값을 쓰고, 실제 상태(차단됨·꺼짐·켜짐)와 소리 켜기 단추를 보인다', () => {
    const unlockSpy = vi.spyOn(alarmSound, 'unlock').mockResolvedValue();
    try {
      signInAs('FOREMAN');
      renderPage();
      const box = screen.getByLabelText('확인 대기 경보를 소리로 알리기') as HTMLInputElement;
      expect(box.checked).toBe(true);
      // AudioContext 가 아직 running 이 아니므로 차단됨 상태와 소리 켜기 단추를 보인다.
      expect(screen.getByText('경보음 차단됨')).toBeTruthy();
      const enableBtn = screen.getByRole('button', { name: '소리 켜기' });
      act(() => fireEvent.click(enableBtn));
      expect(unlockSpy).toHaveBeenCalledTimes(1);

      // 체크를 끄면 저장값이 muted=true 로 바뀌고 상태가 꺼짐으로 바뀐다.
      act(() => fireEvent.click(box));
      expect(alarmSound.getSnapshot().muted).toBe(true);
      expect(box.checked).toBe(false);
      expect(screen.getByText('경보음 꺼짐')).toBeTruthy();
      expect(screen.queryByRole('button', { name: '소리 켜기' })).toBeNull();

      // 상단바 소리 버튼(alarmSound.setMuted)에서 다시 켜면 설정 화면도 즉시 따라온다.
      act(() => alarmSound.setMuted(false));
      expect(box.checked).toBe(true);
      expect(screen.getByText('경보음 차단됨')).toBeTruthy();
    } finally {
      unlockSpy.mockRestore();
    }
  });

  it('경보 소리가 브라우저에서 깨어난 상태(ready=true)이면 경보음 켜짐을 보이고 소리 켜기 단추를 숨긴다', () => {
    const snapshotSpy = vi
      .spyOn(alarmSound, 'getSnapshot')
      .mockReturnValue({ ready: true, muted: false, want: null });
    try {
      signInAs('FOREMAN');
      renderPage();
      expect(screen.getByText('경보음 켜짐')).toBeTruthy();
      expect(screen.queryByRole('button', { name: '소리 켜기' })).toBeNull();
    } finally {
      snapshotSpy.mockRestore();
    }
  });

  it('GPU 구역은 기본으로 접힌 「고급 설정 · GPU」 안에 있고, 누르면 펼쳐진다(창이 아님)', async () => {
    signInAs('ADMIN');
    renderPage();
    await screen.findByText(/RunPod · H100 SXM · 자동 정지 30분/);
    expect(screen.queryByRole('dialog')).toBeNull();
    const details = screen.getByText('고급 설정 · GPU').closest('details') as HTMLDetailsElement;
    expect(details).toBeTruthy();
    expect(details.open).toBe(false);
    act(() => fireEvent.click(screen.getByText('고급 설정 · GPU')));
    expect(details.open).toBe(true);
    expect(screen.getByText('버전 2')).toBeTruthy();
  });
});
