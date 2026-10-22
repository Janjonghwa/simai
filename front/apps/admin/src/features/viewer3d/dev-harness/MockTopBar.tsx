import type { SpatialDataStore } from '@/features/viewer3d/data/spatialData.store';
import type { ViewerStatus } from '@/features/viewer3d/types';
import { Button } from '@simai/ui/components/button';
import { Moon, Sun } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useStore } from 'zustand';

const NONE = '—';
/** 개발 화면 표시 기준(ms). 운영 임계값이 아니다(명세 수치 없음, FE HEALTH_TIMEOUT_MS 와도 별개). */
const DEV_STALE_MS = 1000;

function Meta({ label }: { label: string }) {
  return (
    <span>
      {label} <b className="smk-mono">{NONE}</b>
    </span>
  );
}

/**
 * 입력 연결과 World 수신 경과를 따로 보인다. 연결이 OPEN 이어도 World 가 멈췄으면(일시정지·지연) 최신이라고 보이지 않는다.
 * 3D ready(렌더러 상태)와도 다른 값이다.
 */
function DataAge({ store }: { store: SpatialDataStore }) {
  const connection = useStore(store, (s) => s.connection?.state ?? null);
  const receivedAt = useStore(store, (s) => s.world?.meta.receivedAt ?? null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, []);
  const age = receivedAt === null ? null : Math.max(0, now - receivedAt);
  const stale = age === null || age > DEV_STALE_MS;
  return (
    <span data-testid="data-age" data-stale={stale ? '' : undefined}>
      데이터{' '}
      <b className="smk-mono">
        {connection ?? NONE} · {age === null ? 'World 없음' : `${(age / 1000).toFixed(1)}s 전`}
        {stale && age !== null ? ' · 지연(개발 기준)' : ''}
      </b>
    </span>
  );
}

interface MockTopBarProps {
  status: ViewerStatus | null;
  light: boolean;
  onToggleTheme: () => void;
  store: SpatialDataStore;
}

/** 3D 위에 떠 있는 상단 바. 운영 상태 값은 연결하지 않아 비워 둔다. */
export function MockTopBar({ status, light, onToggleTheme, store }: MockTopBarProps) {
  return (
    <header className="smk-top">
      <strong className="smk-brand">
        Sim<span>AI</span>
      </strong>
      <span className="smk-site">교실 테스트베드 · 10×10m</span>
      <span className="smk-chip">개발 목업 · 예시 데이터</span>
      <div className="smk-meta">
        <DataAge store={store} />
        <Meta label="카메라" />
        <Meta label="보정" />
        <Meta label="구성" />
        <Meta label="장면" />
      </div>
      <span className="smk-status smk-mono" data-testid="viewer-status">
        3D {status ? `${status.phase} · ${status.mode}` : NONE}
      </span>
      <Button variant="secondary" size="sm" onClick={onToggleTheme}>
        {light ? <Moon /> : <Sun />}
        {light ? '다크' : '화이트'}
      </Button>
    </header>
  );
}
