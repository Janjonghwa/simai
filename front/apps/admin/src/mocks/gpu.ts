import { problem } from '@/mocks/problem';
import { http, HttpResponse } from 'msw/http';

// GPU 켜기·끄기·설정 mock(S15P31A101-702). 서버(domain/gpu)와 같은 모양으로 답한다. 켜면 켜는 중 → 가중치 → 엔진 → 준비 완료를
// 몇 초씩 지나 연결된다(실제는 5~7분). 끄면 꺼짐. 관제·세팅 막힘은 흉내 내지 않는다.

const STEP_MS = 4_000;

interface MockLaunch {
  launch_id: string;
  gpu_type: string;
  region: string;
  price: number;
  requestedAt: number;
}

export function createGpuMock() {
  let launch: MockLaunch | null = null;
  let settingsVersion: number | null = null;
  let settings = {
    provider: 'RUNPOD' as 'RUNPOD' | 'AWS',
    preferred_gpu_types: ['NVIDIA H100 80GB HBM3', 'NVIDIA H100 NVL', 'NVIDIA H100 PCIe'],
    preferred_regions: ['AP-JP-1', 'OC-AU-1'],
    cloud_type: 'SECURE' as 'SECURE' | 'COMMUNITY',
    interruptible: false,
    max_price_per_hour_usd: null as number | null,
    auto_stop_idle_minutes: 30,
    runpod_template_id: null as string | null,
  };
  let settingsAt: number | null = null;

  const status = () => {
    const now = Date.now();
    if (!launch) {
      return { session: null, server_time_ms: now, launch: null, power: 'OFF', stop_block: null };
    }
    const age = now - launch.requestedAt;
    const launchView = {
      launch_id: launch.launch_id,
      state: 'RUNNING',
      provider: 'RUNPOD',
      gpu_type: launch.gpu_type,
      region: launch.region,
      cloud_type: settings.cloud_type,
      interruptible: settings.interruptible,
      price_per_hour_usd: launch.price,
      requested_by: 'admin1',
      requested_at_ms: launch.requestedAt,
      instance_id: `pod-${launch.launch_id.slice(0, 6)}`,
      ended_at_ms: null,
      end_reason: null,
      failure_code: null,
    };
    if (age < STEP_MS) {
      return {
        session: null,
        server_time_ms: now,
        launch: launchView,
        power: 'STARTING',
        stop_block: null,
      };
    }
    const stage = age < STEP_MS * 2 ? 'WEIGHTS' : age < STEP_MS * 3 ? 'ENGINE' : 'READY';
    return {
      session: {
        session_id: launch.launch_id,
        state: 'ACTIVE',
        stage,
        stage_reason_code: null,
        provider: 'RUNPOD',
        instance_id: launchView.instance_id,
        gpu_type: launch.gpu_type,
        region: launch.region,
        price_per_hour_usd: launch.price,
        registered_at_ms: launch.requestedAt + STEP_MS,
        elapsed_ms: age - STEP_MS,
        tier0_link: stage === 'READY' ? 'CONNECTED' : 'DISCONNECTED',
      },
      server_time_ms: now,
      launch: launchView,
      power: stage === 'READY' ? 'CONNECTED' : 'PREPARING',
      stop_block: null,
    };
  };

  const offers = () => ({
    provider: settings.provider,
    cloud_type: settings.cloud_type,
    interruptible: settings.interruptible,
    max_price_per_hour_usd: settings.max_price_per_hour_usd,
    gpu_types: settings.preferred_gpu_types.map((id, i) => ({
      gpu_type: id,
      name: id.replace('NVIDIA ', ''),
      memory_gb: i < 3 ? 80 : null,
    })),
    regions: settings.preferred_regions.map((r) => ({ region: r, name: r, preferred: true })),
    cells: settings.preferred_gpu_types.flatMap((g, gi) =>
      settings.preferred_regions.map((r, ri) => ({
        gpu_type: g,
        region: r,
        stock: gi === 0 && ri === 0 ? 'NONE' : gi + ri === 1 ? 'LOW' : 'HIGH',
        price_per_hour_usd: 2.99 - gi * 0.3,
        within_cap:
          settings.max_price_per_hour_usd === null ||
          2.99 - gi * 0.3 <= settings.max_price_per_hour_usd,
      })),
    ),
    default:
      settings.preferred_gpu_types.length > 0 && settings.preferred_regions.length > 1
        ? { gpu_type: settings.preferred_gpu_types[0], region: settings.preferred_regions[1] }
        : null,
    fetched_at_ms: Date.now(),
  });

  const settingsView = () => ({
    version_id: settingsVersion,
    ...settings,
    created_at_ms: settingsAt,
  });

  const handlers = [
    http.get('*/api/gpu/sessions/current', () => HttpResponse.json(status())),
    http.get('*/api/gpu/offers', () => HttpResponse.json(offers())),
    http.post('*/api/gpu/launches', async ({ request }) => {
      if (launch) return problem(409, 'GPU_ALREADY_ON', 'GPU 가 이미 켜져 있거나 켜는 중입니다');
      const body = (await request.json().catch(() => ({}))) as {
        gpu_type?: string;
        region?: string;
      };
      const o = offers();
      const pick =
        body.gpu_type && body.region ? { gpu_type: body.gpu_type, region: body.region } : o.default;
      if (!pick) return problem(409, 'GPU_OUT_OF_STOCK', '재고가 없습니다');
      const cell = o.cells.find((c) => c.gpu_type === pick.gpu_type && c.region === pick.region);
      launch = {
        launch_id: crypto.randomUUID(),
        gpu_type: pick.gpu_type,
        region: pick.region,
        price: cell?.price_per_hour_usd ?? 2.99,
        requestedAt: Date.now(),
      };
      return HttpResponse.json(status().launch, { status: 201 });
    }),
    http.post('*/api/gpu/stop', () => {
      if (!launch) return problem(409, 'GPU_NOT_ON', '켜져 있는 GPU 가 없습니다');
      launch = null;
      return new HttpResponse(null, { status: 204 });
    }),
    http.get('*/api/gpu/settings', () =>
      HttpResponse.json({
        settings: settingsView(),
        providers: [
          { id: 'RUNPOD', configured: true },
          { id: 'AWS', configured: false },
        ],
      }),
    ),
    http.post('*/api/gpu/settings', async ({ request }) => {
      settings = { ...settings, ...((await request.json()) as typeof settings) };
      settingsVersion = (settingsVersion ?? 0) + 1;
      settingsAt = Date.now();
      return HttpResponse.json(settingsView(), { status: 201 });
    }),
  ];
  return { handlers };
}
