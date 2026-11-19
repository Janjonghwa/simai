import { SIDE_COLUMN_CLASS } from '@/features/monitor/sideColumnStyle';
import { ZoneList } from '@/features/zones/ZoneList';

/**
 * 관제 화면 왼쪽 칼럼: 위험 구역 목록. 사람을 고르면 열리던 「선택한 작업자」 패널(표시 이름 변경)은 뺐고(S15P31A101-798),
 * 사람을 고르면 떠 있는 CCTV 창이 그 사람을 가장 잘 보는 카메라로 열린다(SpatialViewportHost).
 */
export function MonitorSideColumn() {
  return (
    <div className={SIDE_COLUMN_CLASS}>
      <ZoneList />
    </div>
  );
}
