import { type MockStateStorage, readMockState, writeMockState } from '@/mocks/mockPersist';
import { problem } from '@/mocks/problem';
import { http, HttpResponse } from 'msw/http';

// GPU 켜기·끄기·설정 mock(S15P31A101-702). 서버(domain/gpu)와 같은 모양으로 답한다. 켜면 켜는 중 → 가중치 → 엔진 → 준비 완료를
// 몇 초씩 지나 연결된다(실제는 5~7분). 끄면 꺼짐. 관제·세팅 막힘은 흉내 내지 않는다.
// S15P31A101-841: AWS·RunPod 둘 다 준비된 것으로 두고 공급자별 재고 표(?provider=)를 주며 켜기의 provider 를 받는다. 기본 공급자는 AWS.

export const GPU_MOCK_STEP_MS = 4_000;
const STEP_MS = GPU_MOCK_STEP_MS;
const STATE_KEY = 'gpu';

type MockProvider = 'RUNPOD' | 'AWS';

interface MockLaunch {
  launch_id: string;
  provider: MockProvider;
  gpu_type: string;
  region: string;
  price: number;
  requestedAt: number;
}

export interface GpuMockOptions {
  /** 처음부터 준비 완료(연결됨)로 시작한다. */
  startReady?: boolean;
  /** 늘 준비 완료로 두고 끄기를 거절한다. 연습 빌드(-806)는 실제 GPU 가 없으니 GPU 단계에서 막히지 않게 켠다(저장소는 보지 않는다). */
  alwaysOn?: boolean;
  /** 켜기·끄기를 남길 곳(연습 빌드의 localStorage). 남긴 값이 있으면 startReady 보다 먼저 따른다. */
  storage?: MockStateStorage | null;
  now?: () => number;
}

// 남긴 값: 끈 상태는 { on: false }, 켠 상태는 { on: true, launch }. 모양이 다르면 남기지 않은 것으로 본다.
// launch.provider 는 S15P31A101-841 에 더했다. 그 전에 남긴 값(provider 없음)은 RunPod 으로 읽는다(그때는 RunPod 만 있었다).
function readSavedLaunch(storage: MockStateStorage | null): MockLaunch | null | undefined {
  const saved = readMockState(storage, STATE_KEY);
  if (typeof saved !== 'object' || saved === null || !('on' in saved)) return undefined;
  if (saved.on === false) return null;
  if (saved.on !== true || !('launch' in saved)) return undefined;
  const l = saved.launch as Partial<MockLaunch> | null;
  if (
    typeof l?.launch_id !== 'string' ||
    typeof l.gpu_type !== 'string' ||
    typeof l.region !== 'string' ||
    typeof l.price !== 'number' ||
    typeof l.requestedAt !== 'number'
  ) {
    return undefined;
  }
  return {
    launch_id: l.launch_id,
    provider: l.provider === 'AWS' ? 'AWS' : 'RUNPOD',
    gpu_type: l.gpu_type,
    region: l.region,
    price: l.price,
    requestedAt: l.requestedAt,
  };
}

export function createGpuMock(options: GpuMockOptions = {}) {
  const now = options.now ?? Date.now;
  const storage = options.storage ?? null;
  const saved = options.alwaysOn ? undefined : readSavedLaunch(storage);
  // 처음부터 켜진 GPU 는 기본 공급자 AWS 의 기본 종류(연습 빌드에서 패널이 AWS 탭으로 열린다, S15P31A101-841)
  let launch: MockLaunch | null =
    saved !== undefined
      ? saved
      : options.startReady || options.alwaysOn
        ? {
            launch_id: crypto.randomUUID(),
            provider: 'AWS',
            gpu_type: 'g7e.4xlarge',
            region: 'ap-northeast-2a',
            price: 4.9155,
            requestedAt: now() - STEP_MS * 3,
          }
        : null;
  const saveLaunch = () =>
    writeMockState(storage, STATE_KEY, launch ? { on: true, launch } : { on: false });
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
    const at = now();
    if (!launch) {
      return { session: null, server_time_ms: at, launch: null, power: 'OFF', stop_block: null };
    }
    const age = at - launch.requestedAt;
    const instanceId =
      launch.provider === 'AWS'
        ? `i-0${launch.launch_id.replace(/-/g, '').slice(0, 16)}`
        : `pod-${launch.launch_id.slice(0, 6)}`;
    const launchView = {
      launch_id: launch.launch_id,
      state: 'RUNNING',
      provider: launch.provider,
      gpu_type: launch.gpu_type,
      region: launch.region,
      cloud_type: settings.cloud_type,
      interruptible: settings.interruptible,
      price_per_hour_usd: launch.price,
      requested_by: 'admin1',
      requested_at_ms: launch.requestedAt,
      instance_id: instanceId,
      ended_at_ms: null,
      end_reason: null,
      failure_code: null,
    };
    if (age < STEP_MS) {
      return {
        session: null,
        server_time_ms: at,
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
        provider: launch.provider,
        instance_id: launchView.instance_id,
        gpu_type: launch.gpu_type,
        region: launch.region,
        price_per_hour_usd: launch.price,
        registered_at_ms: launch.requestedAt + STEP_MS,
        elapsed_ms: age - STEP_MS,
        tier0_link: stage === 'READY' ? 'CONNECTED' : 'DISCONNECTED',
      },
      server_time_ms: at,
      launch: launchView,
      power: stage === 'READY' ? 'CONNECTED' : 'PREPARING',
      stop_block: null,
    };
  };

  // 카탈로그 흉내(S15P31A101-705): 선호 GPU + 그 밖 몇 종. 서버처럼 추천 → VRAM 등급 → 선호 순서로 정렬해 둔다(등급 규칙은 서버 GpuTiers)
  const CATALOG = [
    { id: 'NVIDIA H200', name: 'H200 SXM', mem: 141, price: 3.99 },
    { id: 'NVIDIA H100 80GB HBM3', name: 'H100 SXM', mem: 80, price: 2.99 },
    { id: 'NVIDIA H100 NVL', name: 'H100 NVL', mem: 94, price: 2.79 },
    { id: 'NVIDIA H100 PCIe', name: 'H100 PCIe', mem: 80, price: 2.39 },
    { id: 'NVIDIA A100 80GB PCIe', name: 'A100 PCIe', mem: 80, price: 1.64 },
    { id: 'NVIDIA L40S', name: 'L40S', mem: 48, price: 0.86 },
    { id: 'NVIDIA GeForce RTX 4090', name: 'RTX 4090', mem: 24, price: 0.69 },
    { id: 'NVIDIA RTX A4000', name: 'RTX A4000', mem: 16, price: 0.25 },
  ];
  // AWS 서울 GPU 인스턴스 흉내(이름은 서버 AwsGpuProvider 꼴, 가격은 서버 예비 가격표). 실시간 재고를 몰라 UNKNOWN(「모름」).
  // 서버 허용 목록과 같이 g7e.4xlarge 하나만(사용자 결정 11-23, 예산 약 200달러)
  const AWS_CATALOG = [
    { id: 'g7e.4xlarge', name: 'g7e.4xlarge · NVIDIA RTX PRO 6000 ×1', mem: 96, price: 4.9155 },
  ];
  const AWS_ALLOWED = AWS_CATALOG.map((c) => c.id);
  // 설정 공급자가 아니면 그 공급자의 서버 기본 선호(서버 GpuProperties.Defaults, AWS 는 g7e.4xlarge 먼저)
  const prefsFor = (provider: MockProvider) =>
    provider === settings.provider
      ? {
          gpus:
            provider === 'AWS'
              ? settings.preferred_gpu_types.filter((id) => AWS_ALLOWED.includes(id)).length > 0
                ? settings.preferred_gpu_types.filter((id) => AWS_ALLOWED.includes(id))
                : AWS_ALLOWED
              : settings.preferred_gpu_types,
          regions: settings.preferred_regions,
          cap: settings.max_price_per_hour_usd,
          cloud: settings.cloud_type,
          interruptible: settings.interruptible,
        }
      : provider === 'AWS'
        ? {
            gpus: AWS_ALLOWED,
            regions: ['ap-northeast-2a', 'ap-northeast-2b', 'ap-northeast-2c', 'ap-northeast-2d'],
            cap: null,
            cloud: 'SECURE',
            interruptible: false,
          }
        : {
            gpus: ['NVIDIA H100 80GB HBM3', 'NVIDIA H100 NVL', 'NVIDIA H100 PCIe'],
            regions: ['AP-JP-1', 'OC-AU-1'],
            cap: null,
            cloud: 'SECURE',
            interruptible: false,
          };
  const tierOf = (mem: number) =>
    mem >= 74 ? 'RECOMMENDED' : mem >= 44 ? 'BELOW_RECOMMENDED' : mem >= 22 ? 'LIMITED' : 'RISKY';
  const recommendedOf = (name: string) => /(^|[^A-Za-z0-9])H(100|200)(?![0-9])/.test(name);
  const rows = (provider: MockProvider, gpus: string[]) => {
    const catalog = provider === 'AWS' ? AWS_CATALOG : CATALOG;
    const known = new Set(catalog.map((c) => c.id));
    const all = [
      ...catalog,
      ...gpus
        .filter((id) => !known.has(id))
        .map((id) => ({ id, name: id, mem: null as number | null, price: 1 })),
    ];
    const rank = (id: string) => {
      const i = gpus.indexOf(id);
      return i < 0 ? Number.MAX_SAFE_INTEGER : i;
    };
    const tierRank = (mem: number | null) =>
      mem === null
        ? 4
        : ['RECOMMENDED', 'BELOW_RECOMMENDED', 'LIMITED', 'RISKY'].indexOf(tierOf(mem));
    return all
      .map((c) => ({ ...c, rec: recommendedOf(c.name) && (c.mem === null || c.mem >= 74) }))
      .sort(
        (a, b) =>
          Number(b.rec) - Number(a.rec) ||
          tierRank(a.mem) - tierRank(b.mem) ||
          rank(a.id) - rank(b.id) ||
          (b.mem ?? -1) - (a.mem ?? -1),
      );
  };
  const stockOf = (provider: MockProvider, gi: number, ri: number) => {
    if (provider === 'AWS') return (gi + ri) % 4 === 3 ? 'NONE' : 'UNKNOWN'; // NONE = 설정 서브넷이 없는 가용 영역
    return gi === 1 && ri === 0 ? 'NONE' : (gi + ri) % 4 === 3 ? 'NONE' : 'HIGH';
  };
  const availableOf = (stock: string) =>
    stock === 'NONE' ? false : stock === 'UNKNOWN' ? null : true;

  const offers = (provider: MockProvider) => {
    const prefs = prefsFor(provider);
    const list = rows(provider, prefs.gpus);
    const cells = list.flatMap((g, gi) =>
      prefs.regions.map((r, ri) => {
        const stock = stockOf(provider, gi, ri);
        return {
          gpu_type: g.id,
          region: r,
          stock,
          available: availableOf(stock),
          price_per_hour_usd: g.price,
          within_cap: prefs.cap === null || g.price <= prefs.cap,
        };
      }),
    );
    // 서버 autoPick 과 같다: 추천 중 켤 수 있다고 확인된 칸 → 선호 GPU 순서로 재고가 있거나 모르는 칸
    const cellOk = (id: string, r: string, known: boolean) =>
      cells.some(
        (c) =>
          c.gpu_type === id &&
          c.region === r &&
          (known ? c.available === true : c.stock !== 'NONE') &&
          c.within_cap,
      );
    let pick: { gpu_type: string; region: string } | null = null;
    const passes: Array<[string[], boolean]> = [
      [list.filter((g) => g.rec).map((g) => g.id), true],
      [prefs.gpus, false],
    ];
    for (const [ids, known] of passes) {
      for (const id of ids) {
        const r = prefs.regions.find((region) => cellOk(id, region, known));
        if (r) {
          pick = { gpu_type: id, region: r };
          break;
        }
      }
      if (pick) break;
    }
    return {
      provider,
      cloud_type: prefs.cloud,
      interruptible: prefs.interruptible,
      max_price_per_hour_usd: prefs.cap,
      gpu_types: list.map((g) => {
        const rowCells = cells.filter((c) => c.gpu_type === g.id);
        return {
          gpu_type: g.id,
          name: g.name,
          memory_gb: g.mem,
          preferred: prefs.gpus.includes(g.id),
          recommended: g.rec,
          tier: g.mem === null ? null : tierOf(g.mem),
          available: rowCells.some((c) => c.available === true)
            ? true
            : rowCells.some((c) => c.available === null)
              ? null
              : false,
        };
      }),
      regions: prefs.regions.map((r) => ({ region: r, name: r, preferred: true })),
      cells,
      default: pick,
      fetched_at_ms: Date.now(),
    };
  };
  // 두 공급자 모두 준비됐다고 흉내 내므로 서버 규칙(AWS 준비 → AWS)대로 기본은 AWS
  const DEFAULT_PROVIDER: MockProvider = 'AWS';
  const providerOf = (value: unknown): MockProvider | null =>
    value === undefined || value === null || value === ''
      ? DEFAULT_PROVIDER
      : value === 'AWS' || value === 'RUNPOD'
        ? value
        : null;

  const settingsView = () => ({
    version_id: settingsVersion,
    ...settings,
    created_at_ms: settingsAt,
  });

  const handlers = [
    http.get('*/api/gpu/sessions/current', () => HttpResponse.json(status())),
    http.get('*/api/gpu/offers', ({ request }) => {
      const provider = providerOf(new URL(request.url).searchParams.get('provider'));
      if (!provider) return problem(400, 'INVALID_GPU_SETTINGS', 'GPU 설정 값이 올바르지 않습니다');
      return HttpResponse.json(offers(provider));
    }),
    http.post('*/api/gpu/launches', async ({ request }) => {
      const body = (await request.json().catch(() => ({}))) as {
        provider?: string;
        gpu_type?: string;
        region?: string;
      };
      const provider = providerOf(body.provider);
      if (!provider) return problem(400, 'INVALID_GPU_SETTINGS', 'GPU 설정 값이 올바르지 않습니다');
      if (launch) return problem(409, 'GPU_ALREADY_ON', 'GPU 가 이미 켜져 있거나 켜는 중입니다');
      const o = offers(provider);
      // 서버처럼 AWS 는 허용 목록 밖 종류를 켜지 않는다
      if (provider === 'AWS' && body.gpu_type && !AWS_ALLOWED.includes(body.gpu_type)) {
        return problem(400, 'INVALID_GPU_SETTINGS', 'GPU 설정 값이 올바르지 않습니다');
      }
      const pick =
        body.gpu_type && body.region ? { gpu_type: body.gpu_type, region: body.region } : o.default;
      if (!pick) return problem(409, 'GPU_OUT_OF_STOCK', '재고가 없습니다');
      const cell = o.cells.find((c) => c.gpu_type === pick.gpu_type && c.region === pick.region);
      launch = {
        launch_id: crypto.randomUUID(),
        provider,
        gpu_type: pick.gpu_type,
        region: pick.region,
        price: cell?.price_per_hour_usd ?? 2.99,
        requestedAt: now(),
      };
      saveLaunch();
      return HttpResponse.json(status().launch, { status: 201 });
    }),
    http.post('*/api/gpu/stop', () => {
      if (options.alwaysOn) {
        return problem(
          409,
          'MOCK_GPU_ALWAYS_ON',
          '연습용 목업이라 GPU 를 끌 수 없습니다(실제 GPU·서버와 연결되지 않습니다)',
        );
      }
      if (!launch) return problem(409, 'GPU_NOT_ON', '켜져 있는 GPU 가 없습니다');
      launch = null;
      saveLaunch();
      return new HttpResponse(null, { status: 204 });
    }),
    http.post('*/api/gpu/presence', () => new HttpResponse(null, { status: 204 })),
    http.get('*/api/gpu/settings', () =>
      HttpResponse.json({
        settings: settingsView(),
        providers: [
          { id: 'RUNPOD', configured: true, default: false },
          { id: 'AWS', configured: true, default: true },
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
