import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { startZoneEdit } from '@/store/zoneEditMode.store';
import { useEffect } from 'react';

/**
 * 다른 화면(세팅·작업 관리)이 편집 모드로 열어 달라고 요청하면(`/?edit=true`) 관리자에게 편집 모드를 켜고, 요청을
 * 지운다(clear). 요청이 주소에 남지 않아 새로고침하면 다시 켜지지 않는다.
 */
export function useEditEntry(requested: boolean | undefined, clear: () => void) {
  const role = useCurrentRole();
  useEffect(() => {
    if (!requested) return;
    if (role === 'ADMIN') startZoneEdit();
    clear();
  }, [requested, role, clear]);
}
