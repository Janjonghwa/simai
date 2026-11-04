import type { StageState, WorkStageResult } from '@/features/tbm/workStage';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';

// 작업 하나의 단계 표시줄: 세팅 → 구역 → TBM 보고서 → 시작 → 종료 → 마무리·서명.
// 지금 단계를 강조하고, 그 단계의 다음 행동 단추를 하나 둔다. 작업 시작·종료 단추는 상태가 맞을 때 늘 보인다
// (세팅·구역 전에도 시작은 막지 않고 확인 경고만 한다).

const STATE_TEXT: Record<StageState, string> = {
  done: '끝남',
  todo: '아직',
  running: '진행 중',
  failed: '실패',
  unknown: '확인 불가',
};

const STATE_CLASS: Record<StageState, string> = {
  done: 'bg-ok/15 text-ok ring-ok/40',
  todo: 'text-muted-foreground ring-faint',
  running: 'bg-primary/15 text-point-soft ring-primary/40',
  failed: 'bg-destructive/15 text-destructive ring-destructive/40',
  unknown: 'bg-amber-500/15 text-amber-300 ring-amber-400/40',
};

export interface WorkStageBarProps {
  result: WorkStageResult;
  status: 'PLANNED' | 'IN_PROGRESS' | 'DONE' | undefined;
  /** 세팅 세션이 이미 있다(진행 중·실패·끝남). */
  hasSetupSession: boolean;
  /** 작업 id 가 숫자가 아니면 세팅을 시작할 수 없다. */
  canOpenSetup: boolean;
  generating: boolean;
  pending: boolean;
  onOpenSetup: () => void;
  onOpenZones: () => void;
  onGenerate: () => void;
  onStart: () => void;
  onEnd: () => void;
  onSign: () => void;
}

export function WorkStageBar(props: WorkStageBarProps) {
  const { result, status, pending } = props;
  const { current } = result;

  const next = (() => {
    switch (current) {
      case 'setup':
        return (
          <Button size="sm" disabled={!props.canOpenSetup} onClick={props.onOpenSetup}>
            {props.hasSetupSession ? '세팅 이어가기' : '세팅 시작'}
          </Button>
        );
      case 'zones':
        return (
          <Button size="sm" onClick={props.onOpenZones}>
            구역 설정
          </Button>
        );
      case 'tbm':
        return (
          <Button size="sm" disabled={props.generating} onClick={props.onGenerate}>
            {props.generating ? 'TBM 보고서 만드는 중…' : 'TBM 보고서 만들기'}
          </Button>
        );
      case 'sign':
        return (
          <Button size="sm" onClick={props.onSign}>
            서명하기
          </Button>
        );
      default:
        return null;
    }
  })();

  return (
    <section aria-label="작업 단계" data-testid="work-stage-bar" className="flex flex-col gap-2">
      <ol className="flex flex-wrap items-stretch gap-1.5">
        {result.stages.map((s, i) => (
          <li
            key={s.key}
            data-stage={s.key}
            data-state={s.state}
            aria-current={s.key === current ? 'step' : undefined}
            className={cn(
              'flex min-w-[112px] flex-1 flex-col gap-0.5 rounded-lg px-3 py-2 text-xs ring-1 ring-inset',
              STATE_CLASS[s.state],
              s.key === current && 'font-bold outline-2 outline-primary',
            )}
          >
            <span className="text-[13px] font-semibold">
              {i + 1}. {s.label}
            </span>
            <span>
              {STATE_TEXT[s.state]} · {s.detail}
            </span>
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap items-center gap-2">
        {next}
        {status === 'PLANNED' && (
          <Button
            size="sm"
            variant={current === 'start' ? 'default' : 'secondary'}
            disabled={pending}
            onClick={props.onStart}
          >
            작업 시작
          </Button>
        )}
        {status === 'IN_PROGRESS' && (
          <Button
            size="sm"
            variant={current === 'end' ? 'default' : 'secondary'}
            disabled={pending}
            onClick={props.onEnd}
          >
            작업 종료
          </Button>
        )}
      </div>
    </section>
  );
}
