import type { StageKey, StageState, WorkStageResult } from '@/features/tbm/workStage';
import { Button } from '@simai/ui/components/button';
import { cn } from '@simai/ui/lib/utils';

// 작업 하나의 단계 줄(한 줄): 세팅 → 구역 → TBM 보고서 → 시작 → 종료 → 마무리·서명.
// 단계를 누르면 그 단계 화면으로 간다(onStep). 오른쪽에는 지금 단계의 다음 행동 단추를 둔다. 작업 시작은 계획 중이면 늘 보인다
// (세팅·구역 전에도 시작은 막지 않고 확인 경고만 한다). 작업 종료는 진행 중 본문 카드에 있다.

const STATE_TEXT: Record<StageState, string> = {
  done: '끝남',
  todo: '아직',
  running: '진행 중',
  failed: '실패',
  unknown: '확인 불가',
};

const MARK_CLASS: Record<StageState, string> = {
  done: 'bg-ok/15 text-ok ring-ok/60',
  todo: 'text-muted-foreground ring-faint',
  running: 'text-point-soft ring-primary',
  failed: 'bg-destructive/15 text-destructive ring-destructive/60',
  unknown: 'bg-amber-500/15 text-amber-300 ring-amber-400/60',
};

/** 단계 줄 단추: 단계 칸과 같은 높이로 크게 둔다. */
const BAR_BUTTON = 'h-[38px] px-4 text-sm font-bold';

export interface WorkStageBarProps {
  result: WorkStageResult;
  status: 'PLANNED' | 'IN_PROGRESS' | 'DONE' | undefined;
  /** 세팅 세션이 이미 있다(진행 중·실패·끝남). */
  hasSetupSession: boolean;
  /** 작업 id 가 숫자가 아니면 세팅(구역 단계 포함)을 열 수 없다. */
  canOpenSetup: boolean;
  generating: boolean;
  pending: boolean;
  onStep: (key: StageKey) => void;
  /** 세팅 화면을 연다. 구역 단계도 세팅 화면의 마지막 단계다. */
  onOpenSetup: () => void;
  onGenerate: () => void;
  onStart: () => void;
  onSign: () => void;
  /** 본문 가운데에 같은 단추를 크게 띄웠으면 줄 안의 다음 행동 단추는 숨긴다. */
  hideNext?: boolean;
  /** 서명할 보고서가 있는지. 보고서 없이 끝난 작업은 서명할 수 없어 서명하기를 보이지 않는다. */
  canSign: boolean;
}

export function WorkStageBar(props: WorkStageBarProps) {
  const { result, status, pending } = props;
  const { current } = result;

  const next = (() => {
    switch (current) {
      case 'setup':
        return (
          <Button disabled={!props.canOpenSetup} onClick={props.onOpenSetup} className={BAR_BUTTON}>
            {props.hasSetupSession ? '세팅 이어가기' : '세팅 시작'}
          </Button>
        );
      case 'zones':
        return (
          <Button disabled={!props.canOpenSetup} onClick={props.onOpenSetup} className={BAR_BUTTON}>
            구역 설정
          </Button>
        );
      case 'tbm':
        return (
          <Button disabled={props.generating} onClick={props.onGenerate} className={BAR_BUTTON}>
            {props.generating ? 'TBM 보고서 만드는 중…' : 'TBM 보고서 만들기'}
          </Button>
        );
      case 'sign':
        return props.canSign ? (
          <Button onClick={props.onSign} className={BAR_BUTTON}>
            서명하기
          </Button>
        ) : null;
      default:
        return null;
    }
  })();

  return (
    <section
      aria-label="작업 단계"
      data-testid="work-stage-bar"
      className="flex flex-none items-center gap-2.5"
    >
      <ol className="grid min-w-0 flex-1 grid-cols-6 gap-1.5">
        {result.stages.map((s, i) => (
          <li
            key={s.key}
            data-stage={s.key}
            data-state={s.state}
            aria-current={s.key === current ? 'step' : undefined}
          >
            <button
              type="button"
              title={`${STATE_TEXT[s.state]} · ${s.detail}`}
              onClick={() => props.onStep(s.key)}
              className={cn(
                'flex h-[38px] w-full min-w-0 items-center gap-2 rounded-[10px] bg-card px-3 text-[13.5px] whitespace-nowrap text-muted-foreground ring-1 ring-border ring-inset hover:bg-surface-2 hover:text-foreground',
                s.state === 'done' && 'text-foreground',
                s.key === current && 'bg-surface-3 font-bold text-foreground ring-2 ring-primary',
              )}
            >
              <span
                aria-hidden
                className={cn(
                  'inline-flex size-5 flex-none items-center justify-center rounded-full font-mono text-[11.5px] ring-1 ring-inset',
                  MARK_CLASS[s.state],
                )}
              >
                {s.state === 'done' ? '✓' : i + 1}
              </span>
              <span className="min-w-0 truncate">{s.label}</span>
              <span className="sr-only">
                {STATE_TEXT[s.state]} · {s.detail}
              </span>
            </button>
          </li>
        ))}
      </ol>
      {!props.hideNext && next}
      {status === 'PLANNED' && (
        <Button
          variant={current === 'start' ? 'default' : 'secondary'}
          disabled={pending}
          onClick={props.onStart}
          className={cn(
            BAR_BUTTON,
            current !== 'start' && 'text-foreground ring-1 ring-faint ring-inset',
          )}
        >
          작업 시작
        </Button>
      )}
    </section>
  );
}
