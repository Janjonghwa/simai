import { SIDE_COLUMN_CLASS } from '@/features/monitor/sideColumnStyle';
import { ZoneList } from '@/features/zones/ZoneList';

/** 관제 화면 왼쪽 칼럼: 도구 모음 아래에 위험 구역 목록을 두고, 그 아래에 이어서 다른 패널이 들어온다. */
export function MonitorSideColumn() {
  return (
    <div className={SIDE_COLUMN_CLASS}>
      <ZoneList />
    </div>
  );
}
