import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useSetupFlowStore } from '@/features/setup/setup.store';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { clearZoneDrafts } from '@/store/zoneEdit.store';
import { Button } from '@simai/ui/components/button';
import { useNavigate } from '@tanstack/react-router';
import { useState } from 'react';

function MonitorTbmNavigateButton() {
  const navigate = useNavigate();
  return (
    <Button size="sm" variant="secondary" onClick={() => void navigate({ to: '/tbm' })}>
      작업 관리(TBM)로 이동
    </Button>
  );
}

export function MonitorPage({ initialEdit = false }: { initialEdit?: boolean } = {}) {
  const role = useCurrentRole();
  const zonesCommitted = useSetupFlowStore((s) => s.zonesCommitted);
  const [editing, setEditing] = useState(initialEdit);
  const [prevInitial, setPrevInitial] = useState(initialEdit);
  if (prevInitial !== initialEdit) {
    setPrevInitial(initialEdit);
    if (initialEdit) setEditing(true);
  }
  const active = role === 'ADMIN' && editing;
  const cancel = () => {
    clearZoneDrafts();
    setEditing(false);
  };
  return (
    <>
      <h1 className="sr-only">실시간 관제</h1>
      {role === 'ADMIN' && (
        <div className="pointer-events-auto absolute top-[72px] left-[124px] z-[6] flex items-center gap-2">
          <Button
            size="sm"
            aria-pressed={active}
            onClick={active ? cancel : () => setEditing(true)}
          >
            {active ? '편집 취소' : '관리자 편집 모드'}
          </Button>
          {active && (
            <span
              role="status"
              className="rounded bg-card px-3 py-1 text-xs font-semibold text-foreground shadow-float"
            >
              편집 중
            </span>
          )}
          {active && zonesCommitted && <MonitorTbmNavigateButton />}
        </div>
      )}
      {active && <ZonesPage />}
    </>
  );
}
