import { CctvFigures, CctvMockWall } from '@/features/cctv/CctvDemo';
import { cctvPeople, type CctvProjector } from '@/features/cctv/cctvMockView';
import { formatTime } from '@/features/events/events';
import { replayErrorLine } from '@/features/history/replay';
import { allGap, isEmptyReplay, type ReplayIndex } from '@/features/history/replayIndex';
import { ReplayVideoWall } from '@/features/history/ReplayVideoWall';
import type { Span, VideoSegment } from '@/features/history/types';
import { useActiveSegments, useReplaySegments } from '@/features/history/useReplaySegments';
import { useReplayFrame, useReplayNoData, type WorkReplay } from '@/features/history/useWorkReplay';
import type { ReplaySpatialSource } from '@/features/viewer3d';
import { useCallback, useMemo, type ReactNode } from 'react';

function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center p-6 text-muted-foreground">
      {children}
    </div>
  );
}

/**
 * CCTV 4화면. 재생 시각을 덮는 상시 영상 조각이 있으면 그 영상을 카메라 칸마다 틀고, 없으면 목 화면이다.
 * 목 화면은 재생 시각에 보이는 World 기록의 사람을 그리고, 기록이 바뀔 때만 다시 그린다.
 * 데이터 없음이면 사람을 그리지 않고 흐리게 둔다(마지막 기록을 이어 그리지 않는다, FR-28). 기록이 없어도 기록 없는 구간
 * 밖이면 그 시각에 아무도 없던 것이라 빈 화면을 그대로 보인다.
 */
function ReplayCctv({
  replay,
  index,
  holdMs,
  segments,
}: {
  replay: ReplaySpatialSource | null;
  index: ReplayIndex | null;
  holdMs: number;
  /** 작업 구간과 겹치는 영상 조각 전부. 지금 시각을 덮는 것만 골라 튼다. */
  segments: readonly VideoSegment[];
}) {
  const frame = useReplayFrame(replay, index, holdMs);
  const noData = useReplayNoData(replay, index, holdMs);
  const active = useActiveSegments(replay, segments);
  const people = useMemo(() => (frame ? cctvPeople(frame.tracks) : []), [frame]);
  const figures = useCallback(
    (project: CctvProjector) => <CctvFigures project={project} people={people} />,
    [people],
  );
  if (active.length > 0) return <ReplayVideoWall segments={active} replay={replay} />;
  return (
    <CctvMockWall
      figures={figures}
      frozen={noData}
      badge={noData ? '데이터 없음' : frame ? formatTime(frame.t) : '작업자 없음'}
    />
  );
}

interface ReplayStageProps {
  session: WorkReplay;
  /** 작업 구간. */
  range: Span;
}

/** 화면 가운데 안내 한 줄. 보일 기록이 있으면 null. 기록이 하나도 없을 때 전부 기록 없는 구간이면 데이터 없음이고, 아니면 잡힌 사람이 없던 것이다. */
function stageNotice(session: WorkReplay, range: Span): string | null {
  if (session.failed) return replayErrorLine(session.error);
  if (session.loading || !session.replay) return '기록을 불러오는 중입니다…';
  if (!session.index || !isEmptyReplay(session.index)) return null;
  return allGap(session.index.gaps, range) ? '데이터 없음' : '잡힌 작업자가 없습니다.';
}

/**
 * 가운데 칸: CCTV 목 화면. 기록을 받는 중·실패·없음이면 안내만 보인다.
 * 영상 조각 목록은 기록을 받는 동안 같이 받아 둔다(기록이 준비된 뒤에 요청하면 영상이 그만큼 늦게 켜진다).
 */
export function ReplayStage({ session, range }: ReplayStageProps) {
  const { replay, index, holdMs } = session;
  const segments = useReplaySegments(range);
  const notice = stageNotice(session, range);
  if (notice !== null) return <Notice>{notice}</Notice>;
  return <ReplayCctv replay={replay} index={index} holdMs={holdMs} segments={segments} />;
}
