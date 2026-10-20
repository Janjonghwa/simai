import type { ViewerStatus } from '@/features/viewer3d/types';
import { Button } from '@simai/ui/components/button';
import { Moon, Sun } from 'lucide-react';

const NONE = '—';

function Meta({ label }: { label: string }) {
  return (
    <span>
      {label} <b className="smk-mono">{NONE}</b>
    </span>
  );
}

interface MockTopBarProps {
  status: ViewerStatus | null;
  light: boolean;
  onToggleTheme: () => void;
}

/** 3D 위에 떠 있는 상단 바. 운영 상태 값은 연결하지 않아 비워 둔다. */
export function MockTopBar({ status, light, onToggleTheme }: MockTopBarProps) {
  return (
    <header className="smk-top">
      <strong className="smk-brand">
        Sim<span>AI</span>
      </strong>
      <span className="smk-site">교실 테스트베드 · 10×10m</span>
      <span className="smk-chip">개발 목업 · 예시 데이터</span>
      <div className="smk-meta">
        <Meta label="데이터" />
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
