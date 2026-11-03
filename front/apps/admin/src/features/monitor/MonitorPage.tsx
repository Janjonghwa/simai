import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { ZonesPage } from '@/features/zones/ZonesPage';
import { clearZoneDrafts } from '@/store/zoneEdit.store';
import { Button } from '@simai/ui/components/button';
import { useState } from 'react';

export function MonitorPage({ initialEdit = false }: { initialEdit?: boolean } = {}) {
  const role = useCurrentRole();
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
        </div>
      )}
      {active && <ZonesPage />}
    </>
  );
}
