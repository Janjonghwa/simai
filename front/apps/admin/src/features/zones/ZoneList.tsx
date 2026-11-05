import { useCurrentRole } from '@/features/auth/useCurrentRole';
import {
  useSpatialViewport,
  type SpatialViewportValue,
} from '@/features/layout/SpatialViewportHost';
import { SIDE_PANEL_CLASS, SIDE_PANEL_TITLE_CLASS } from '@/features/monitor/sideColumnStyle';
import { UnpublishedRetry } from '@/features/zones/UnpublishedRetry';
import { useZoneRename } from '@/features/zones/useZoneRename';
import { zonesForList } from '@/features/zones/zoneListOrder';
import { ZoneListRow } from '@/features/zones/ZoneListRow';
import { NOTE_CLASS, WARNING_CLASS } from '@/features/zones/zonePanelStyle';
import { startZoneEdit } from '@/store/zoneEditMode.store';
import { useMemo } from 'react';

/**
 * 관제 화면 왼쪽의 위험 구역 목록. 모든 역할에게 보인다. 장면(SpatialViewportHost) 밖이면 그리지 않는다.
 */
export function ZoneList() {
  const viewport = useSpatialViewport();
  return viewport && <ZoneListPanel viewport={viewport} />;
}

/**
 * 관리자가 구역을 고르면 편집 모드를 켜고 그 구역을 열고, 그 밖의 역할은 3D·2D 에서 강조만 한다(같은 구역을 다시 누르면
 * 해제). 관리자는 연필로 이름을 그 자리에서 고친다.
 */
function ZoneListPanel({ viewport }: { viewport: SpatialViewportValue }) {
  const { zones, zonesFailed, selectedZoneId, selectTarget, clearSelection } = viewport;
  const isAdmin = useCurrentRole() === 'ADMIN';
  const rename = useZoneRename();
  const rows = useMemo(() => zonesForList(zones), [zones]);

  const open = (zoneId: string) => {
    // 이름을 저장하는 중에 칼럼이 사라지면 저장 결과 알림도 사라지므로, 저장이 끝난 뒤에 연다.
    if (rename.busy) return;
    if (isAdmin) {
      selectTarget({ type: 'zone', id: zoneId }, { focus: true });
      startZoneEdit();
    } else if (zoneId === selectedZoneId) {
      clearSelection();
    } else {
      selectTarget({ type: 'zone', id: zoneId });
    }
  };

  if (rows.length === 0 && !zonesFailed) return null;
  return (
    <section aria-label="위험 구역" className={SIDE_PANEL_CLASS}>
      <h2 className={SIDE_PANEL_TITLE_CLASS}>위험 구역</h2>

      {zonesFailed && (
        <p role="alert" className={NOTE_CLASS}>
          구역 목록을 불러오지 못했습니다.
        </p>
      )}

      {rename.showRetry && <UnpublishedRetry onRetry={() => void rename.republish()} />}

      {rename.notice && (
        <p role="status" className={WARNING_CLASS}>
          {rename.notice}
        </p>
      )}

      <ul className="flex min-h-0 flex-col gap-0.5 overflow-y-auto">
        {rows.map((zone) => (
          <ZoneListRow
            key={zone.zoneId}
            zone={zone}
            selected={zone.zoneId === selectedZoneId}
            canRename={isAdmin}
            busy={rename.busy}
            onOpen={() => open(zone.zoneId)}
            onRename={rename.rename}
            onClearNotice={rename.clearNotice}
          />
        ))}
      </ul>
    </section>
  );
}
