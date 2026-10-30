import type { ZoneEditing } from '@/features/zones/useZoneEditing';
import { NEW_KEY } from '@/features/zones/zoneEditModel';
import { PICK_CLASS } from '@/features/zones/zonePanelStyle';

/** 드롭다운 항목 뒤에 붙는 상태. 저장 전이 발행 전보다 먼저다. */
const CHANGED_SUFFIX = ' · 저장 전';
const PENDING_SUFFIX = ' · 발행 전';

function stateSuffix(edit: ZoneEditing, zoneId: string): string {
  if (edit.changedIds.has(zoneId)) return CHANGED_SUFFIX;
  if (edit.pendingIds.has(zoneId)) return PENDING_SUFFIX;
  return '';
}

/** 구역 드롭다운: 새 구역 추가(관리자만), 기존 구역은 `ID · 이름`. */
export function ZoneSelect({ edit }: { edit: ZoneEditing }) {
  const { zone, canEdit } = edit;
  return (
    <select
      aria-label="구역 선택"
      value={zone ? zone.zoneId : NEW_KEY}
      onChange={(event) => edit.pick(event.target.value)}
      className={PICK_CLASS}
    >
      {canEdit && <option value={NEW_KEY}>새 구역 추가</option>}
      {!canEdit && !zone && (
        <option value={NEW_KEY} disabled>
          구역 선택
        </option>
      )}
      {edit.zones.map((z) => (
        <option key={z.zoneId} value={z.zoneId}>
          {z.zoneId} · {z.name}
          {stateSuffix(edit, z.zoneId)}
        </option>
      ))}
    </select>
  );
}
