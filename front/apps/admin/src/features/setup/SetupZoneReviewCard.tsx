import { ALARM_BORDER } from '@/features/events/alarmTone';
import { SEVERITY_LABEL } from '@/features/events/constants';
import { useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import { cameraText } from '@/features/setup/cameraLabels';
import { hasChangeOpinion } from '@/features/setup/setupUx';
import { useSetupZoneReview } from '@/features/setup/useSetupZoneReview';
import { ZoneOpinionBlock } from '@/features/setup/ZoneOpinionView';
import type { Zone, ZoneHeightChangeStatus } from '@/features/zones/types';
import { useArmedDelete } from '@/features/zones/useArmedDelete';
import { zonesForList } from '@/features/zones/zoneListOrder';
import { zoneDisplayName } from '@/features/zones/zoneName';
import {
  ARMED_DELETE_CLASS,
  BUTTON_CLASS,
  DANGER_BUTTON_CLASS,
  PRIMARY_BUTTON_CLASS,
  WARNING_CLASS,
} from '@/features/zones/zonePanelStyle';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useMemo } from 'react';

// 이어 쓴 구역 확인 카드(S15P31A101-629, S15P31A101-630). 새 모델 + World 이어짐 세팅은 구역 단계에 들어오면 지금 관제 구역을 세션 구역 목록에
// 채우고 모두 '확인 필요'로 둔다(서버 POST .../zones/seed). 이전 관제 맵과 이번 세팅 맵의 상면 높이를 비교해 구역마다
// 변경 의심(CHANGE_SUSPECTED)·변화 없음(UNCHANGED)·모름(UNKNOWN)을 표시한다.
// 변경 의심 구역을 먼저 정렬·강조하고, 변화 없음은 '변화 없음 N개 모두 확인' 단추로 한 번에 확인을 권유하며, 모름·변경 의심은 개별 확인한다.
// 확인 필요가 남아 있으면 세팅을 완료할 수 없다(서버 409 SETUP_ZONES_UNCONFIRMED).
// 변경 의심 구역은 AI 의견(유지·수정·삭제 권고 한 줄)과 이전·이번 세팅 사진을 행 아래에 보인다(S15P31A101-631). 의견은 제안일 뿐
// 확인·수정·삭제는 관리자가 직접 누른다.

const ROW_BUTTON_CLASS = 'h-7 shrink-0 px-2 text-[12px]';

const HEIGHT_STATUS_RANK: Record<ZoneHeightChangeStatus, number> = {
  CHANGE_SUSPECTED: 0,
  UNKNOWN: 1,
  UNCHANGED: 2,
};

const HEIGHT_STATUS_LABEL: Record<ZoneHeightChangeStatus, string> = {
  CHANGE_SUSPECTED: '변경 의심',
  UNCHANGED: '변화 없음',
  UNKNOWN: '모름',
};

const HEIGHT_BADGE_CLASS: Record<ZoneHeightChangeStatus, string> = {
  CHANGE_SUSPECTED:
    'border-amber-500/50 bg-amber-500/15 font-medium text-amber-700 dark:text-amber-300',
  UNCHANGED: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  UNKNOWN: 'border-border bg-muted/50 text-muted-foreground',
};

function heightRank(zone: Zone): number {
  if (hasChangeOpinion(zone.aiOpinion)) return 0;
  const status = zone.heightChange?.status;
  return status ? HEIGHT_STATUS_RANK[status] : 1;
}

/** 확인 필요 구역을 먼저, 그 안에서는 변경 의심 → 모름 → 변화 없음 순, 같은 판정 안에서는 위험한 등급·이름순. */
export function reviewOrder(zones: readonly Zone[]): Zone[] {
  const ordered = zonesForList(zones);
  const sortByHeight = (list: Zone[]) => [...list].sort((a, b) => heightRank(a) - heightRank(b));
  return [
    ...sortByHeight(ordered.filter((z) => z.needsReview)),
    ...sortByHeight(ordered.filter((z) => !z.needsReview)),
  ];
}

/** 세팅 구역 단계 오른쪽 칸의 확인 카드. 세션 구역 목록(ZoneSourceContext)으로 그린 장면(SpatialViewportContext) 안에서만 그린다. */
export function SetupZoneReviewCard({
  sessionId,
  cameraIds = [],
}: {
  sessionId: string;
  cameraIds?: readonly string[];
}) {
  const viewport = useSpatialViewport();
  const review = useSetupZoneReview(sessionId);
  const { armedKey, arm, disarm } = useArmedDelete();
  const zones = viewport?.zones;
  const rows = useMemo(() => reviewOrder(zones ?? []), [zones]);
  if (!viewport || rows.length === 0) return null;
  const { selectedZoneId, selectTarget, clearSelection } = viewport;
  const hasHeightVerdicts = rows.some((z) => z.heightChange != null);
  const suspectedPending = rows.filter(
    (z) => z.needsReview && z.heightChange?.status === 'CHANGE_SUSPECTED',
  ).length;

  // 수정: 그 구역을 고르면 3D 가 초점을 옮기고 구역 편집 도구가 그 구역을 연다. 저장하면 확인된 것으로 바뀐다
  const edit = (zoneId: string) => selectTarget({ type: 'zone', id: zoneId }, { focus: true });
  // 삭제는 한 번 더 눌러 확인한다(구역 편집 도구의 삭제와 같다). 뺀 구역은 세팅 완료 때 보관된다
  const remove = (zoneId: string) => {
    if (armedKey !== zoneId) {
      arm(zoneId);
      return;
    }
    disarm();
    if (selectedZoneId === zoneId) clearSelection();
    review.remove(zoneId);
  };

  return (
    <Card size="sm" data-testid="setup-zone-review-card">
      <CardHeader>
        <CardTitle>
          <h3>이어 쓴 구역 확인</h3>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-[13px]">
        <p role="status" className="text-xs text-muted-foreground">
          {review.pending > 0
            ? `확인 필요 ${review.pending}개${suspectedPending > 0 ? ` (변경 의심 ${suspectedPending}개)` : ''} · 전체 ${rows.length}개. 지금 현장과 맞는지 구역마다 확인하거나 고치거나 지우세요.`
            : `전체 ${rows.length}개를 모두 확인했습니다.`}
        </p>
        {review.unchangedPending > 0 ? (
          <Button
            disabled={review.busy}
            onClick={review.confirmUnchanged}
            className={`${PRIMARY_BUTTON_CLASS} w-full`}
          >
            {`변화 없음 ${review.unchangedPending}개 모두 확인`}
          </Button>
        ) : (
          !hasHeightVerdicts &&
          review.pending > 0 && (
            <Button
              disabled={review.busy}
              onClick={review.confirmAll}
              className={`${PRIMARY_BUTTON_CLASS} w-full`}
            >
              {`남은 ${review.pending}개 모두 확인`}
            </Button>
          )
        )}
        {review.error && (
          <p role="alert" className={WARNING_CLASS}>
            {review.error}
          </p>
        )}
        <ul aria-label="이어 쓴 구역" className="flex flex-col gap-1">
          {rows.map((zone) => {
            const name = cameraText(cameraIds, zoneDisplayName(zone.name));
            const armed = armedKey === zone.zoneId;
            const heightStatus = zone.heightChange?.status;
            const aiChange = hasChangeOpinion(zone.aiOpinion);
            const suspected = heightStatus === 'CHANGE_SUSPECTED' || aiChange;
            return (
              <li
                key={zone.zoneId}
                data-needs-review={zone.needsReview ? 'true' : undefined}
                data-height-change={heightStatus}
                className={cn(
                  'flex flex-col gap-1 rounded-md border-l-[3px] py-1 pl-2',
                  ALARM_BORDER[zone.severity],
                  suspected && zone.needsReview && 'bg-amber-500/10 ring-1 ring-amber-500/40',
                  zone.zoneId === selectedZoneId && 'bg-foreground/10',
                )}
              >
                <div className="flex items-center gap-1.5">
                  <span className="sr-only">{`${SEVERITY_LABEL[zone.severity]} `}</span>
                  <span
                    className={cn(
                      'min-w-0 flex-1 truncate',
                      !zone.needsReview && 'text-muted-foreground',
                      suspected && zone.needsReview && 'font-medium',
                    )}
                  >
                    {name}
                  </span>
                  {heightStatus && (
                    <span
                      className={cn(
                        'shrink-0 rounded border px-1.5 py-0.5 text-[11px] leading-none',
                        aiChange
                          ? 'border-delay bg-delay/10 text-delay'
                          : HEIGHT_BADGE_CLASS[heightStatus],
                      )}
                    >
                      {heightStatus === 'UNCHANGED' && aiChange
                        ? '변화 감지'
                        : HEIGHT_STATUS_LABEL[heightStatus]}
                    </span>
                  )}
                  {zone.needsReview ? (
                    <Button
                      disabled={review.busy}
                      aria-label={`${name} 확인`}
                      onClick={() => review.confirm(zone.zoneId)}
                      className={cn(PRIMARY_BUTTON_CLASS, ROW_BUTTON_CLASS)}
                    >
                      확인
                    </Button>
                  ) : (
                    <span className="shrink-0 text-[11.5px] text-muted-foreground">확인됨</span>
                  )}
                  <Button
                    variant="secondary"
                    aria-label={`${name} 수정`}
                    onClick={() => edit(zone.zoneId)}
                    className={cn(BUTTON_CLASS, ROW_BUTTON_CLASS)}
                  >
                    수정
                  </Button>
                  <Button
                    variant="secondary"
                    disabled={review.busy}
                    aria-label={armed ? `${name} 삭제 확인` : `${name} 삭제`}
                    onClick={() => remove(zone.zoneId)}
                    className={cn(
                      armed ? ARMED_DELETE_CLASS : DANGER_BUTTON_CLASS,
                      ROW_BUTTON_CLASS,
                    )}
                  >
                    {armed ? '삭제 확인' : '삭제'}
                  </Button>
                </div>
                {zone.aiOpinion && (
                  <ZoneOpinionBlock
                    zoneName={name}
                    opinion={zone.aiOpinion}
                    cameraIds={cameraIds}
                  />
                )}
              </li>
            );
          })}
        </ul>
      </CardContent>
    </Card>
  );
}
