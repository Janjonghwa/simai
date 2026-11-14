import { GpuSettingsDialog, toBody, toForm } from '@/features/gpu/GpuSettingsDialog';
import type { GpuSettings } from '@/features/gpu/types';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw/http';
import { setupServer } from 'msw/node';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async () => {
  const { createHttp } = await import('@/lib/http');
  return { api: createHttp({ baseUrl: 'http://api.test' }) };
});

const settings: GpuSettings = {
  version_id: 3,
  provider: 'RUNPOD',
  preferred_gpu_types: ['NVIDIA H100 80GB HBM3', 'NVIDIA H100 PCIe'],
  preferred_regions: ['AP-JP-1', 'OC-AU-1'],
  cloud_type: 'SECURE',
  interruptible: false,
  max_price_per_hour_usd: null,
  auto_stop_idle_minutes: 30,
  runpod_template_id: 'tpl-simai',
  created_at_ms: 1_793_000_000_000,
};

const saved: unknown[] = [];
const server = setupServer(
  http.get('*/api/gpu/settings', () =>
    HttpResponse.json({
      settings,
      providers: [
        { id: 'RUNPOD', configured: true },
        { id: 'AWS', configured: false },
      ],
    }),
  ),
  http.post('*/api/gpu/settings', async ({ request }) => {
    const body = await request.json();
    saved.push(body);
    return HttpResponse.json({ ...settings, ...(body as object), version_id: 4 }, { status: 201 });
  }),
);
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
afterAll(() => server.close());
afterEach(() => {
  cleanup();
  server.resetHandlers();
  saved.length = 0;
});

describe('GpuSettingsDialog', () => {
  it('지금 설정을 채워 보이고, 고쳐 저장하면 새 버전 본문을 보낸 뒤 닫는다(비밀 값 칸은 없다)', async () => {
    const onClose = vi.fn();
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <GpuSettingsDialog onClose={onClose} />
      </QueryClientProvider>,
    );
    const gpus = (await screen.findByLabelText('선호 GPU (위가 먼저)')) as HTMLTextAreaElement;
    expect(gpus.value).toBe('NVIDIA H100 80GB HBM3\nNVIDIA H100 PCIe');
    expect(screen.getByText('버전 3')).toBeTruthy();
    expect(screen.getByRole('option', { name: 'AWS (설정되지 않음)' })).toBeTruthy();
    expect(screen.queryByLabelText(/API 키|비밀/)).toBeNull();

    act(() =>
      fireEvent.change(screen.getByLabelText('시간당 가격 상한 (USD)'), {
        target: { value: '3.5' },
      }),
    );
    act(() =>
      fireEvent.change(screen.getByLabelText('자동 정지 (유휴 분, 0 = 끔)'), {
        target: { value: '45' },
      }),
    );
    act(() => fireEvent.click(screen.getByLabelText('스팟(중단될 수 있음)')));
    act(() => fireEvent.click(screen.getByRole('button', { name: '저장' })));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(saved).toEqual([
      {
        provider: 'RUNPOD',
        preferred_gpu_types: ['NVIDIA H100 80GB HBM3', 'NVIDIA H100 PCIe'],
        preferred_regions: ['AP-JP-1', 'OC-AU-1'],
        cloud_type: 'SECURE',
        interruptible: true,
        max_price_per_hour_usd: 3.5,
        auto_stop_idle_minutes: 45,
        runpod_template_id: 'tpl-simai',
      },
    ]);
  });

  it('틀린 입력은 보내지 않고 까닭을 보인다', async () => {
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <GpuSettingsDialog onClose={() => undefined} />
      </QueryClientProvider>,
    );
    await screen.findByLabelText('선호 GPU (위가 먼저)');
    act(() =>
      fireEvent.change(screen.getByLabelText('선호 GPU (위가 먼저)'), { target: { value: '  ' } }),
    );
    act(() => fireEvent.click(screen.getByRole('button', { name: '저장' })));
    expect(screen.getByRole('alert').textContent).toBe('선호 GPU 를 하나 이상 적으세요.');
    expect(saved).toEqual([]);
  });

  it('toBody: 빈 가격은 상한 없음, 빈 템플릿은 null, 자동 정지 범위 검사', () => {
    const form = toForm(settings);
    expect(toBody({ ...form, maxPrice: '', templateId: ' ' })).toMatchObject({
      max_price_per_hour_usd: null,
      runpod_template_id: null,
    });
    expect(toBody({ ...form, autoStop: '-1' })).toBe('자동 정지는 0~1440 분입니다.');
    expect(toBody({ ...form, maxPrice: '0' })).toBe(
      '가격 상한은 0 보다 큰 숫자이거나 비워 둡니다.',
    );
  });
});
