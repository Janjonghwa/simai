import { PageTitle } from '@/features/layout/PageTitle';
import { RulesPanel } from '@/features/rules/RulesPanel';
import { SetupMap } from '@/features/setup/SetupMap';
import { CameraPanel, StagePanel } from '@/features/setup/SetupPanels';
import {
  ACTION_LABEL,
  deriveSetup,
  initialSetup,
  isRunning,
  MAP_TITLE,
  SECONDARY_ACTIONS,
  setupReducer,
  STEPS,
  TICK_MS,
  type ActionKey,
  type CalibrationState,
  type Stage,
} from '@/features/setup/setupDemo';
import { Alert } from '@simai/ui/components/alert';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useNavigate } from '@tanstack/react-router';
import { Check } from 'lucide-react';
import { useEffect, useMemo, useReducer } from 'react';

// 설치 · 보정(명세 18.1~18.6). 실측 정물 → 보정 걷기 → 보정 검증 → 완료 순서로 보정 상태를 올린다.
// 보정 API 가 아직 없어 데모 각본(setupDemo)을 시간에 따라 진행한다. 단계는 눌러서 다시 할 수 있다.

const STATE_TONE: Record<CalibrationState, string> = {
  UNCALIBRATED: 'bg-sev-critical/15 text-sev-critical',
  PROVISIONAL: 'bg-sev-caution/15 text-sev-caution',
  DRIFTED: 'bg-sev-caution/15 text-sev-caution',
  CALIBRATED: 'bg-ok/15 text-ok',
};

export function SetupPage() {
  const [state, dispatch] = useReducer(setupReducer, initialSetup);
  const running = isRunning(state);
  const view = useMemo(() => deriveSetup(state), [state]);
  const navigate = useNavigate();

  // 걷기·검증 중에만 시간을 보낸다. 움직임 줄이기 설정과 상관없이 진행한다(점멸만 멈춘다).
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => dispatch({ type: 'tick', ms: TICK_MS }), TICK_MS);
    return () => window.clearInterval(id);
  }, [running]);

  const go = (stage: Stage) => dispatch({ type: 'go', stage });
  const run: Record<ActionKey, () => void> = {
    recheck: () => dispatch({ type: 'recheck' }),
    start: () => go(1),
    walkMore: () => dispatch({ type: 'walkMore' }),
    verify: () => go(2),
    rewalk: () => go(1),
    confirm: () => go(3),
    restart: () => go(0),
    zones: () => void navigate({ to: '/', search: { edit: true } }),
  };

  return (
    <>
      <PageTitle>설치 · 보정</PageTitle>
      <div className="flex flex-wrap items-center gap-3">
        <Badge
          className={cn(
            'h-auto rounded-md px-2.5 py-0.5 font-mono text-[13px] font-semibold',
            STATE_TONE[view.calibration],
          )}
        >
          {view.calibration}
        </Badge>
        <span className="font-mono text-muted-foreground">{view.version}</span>
        <div className="ml-auto flex flex-wrap gap-2">
          {view.actions.map((key) => {
            const secondary = SECONDARY_ACTIONS.has(key);
            return (
              <Button
                key={key}
                size="lg"
                variant={secondary ? 'secondary' : 'default'}
                // 모눈 바탕 위에서도 보이게 보조 버튼은 한 단계 진한 면을 쓴다.
                className={cn('px-4', secondary && 'bg-surface-3 hover:bg-surface-3/70')}
                onClick={run[key]}
              >
                {ACTION_LABEL[key]}
              </Button>
            );
          })}
        </div>
      </div>

      <ol aria-label="보정 단계" className="grid grid-cols-4 gap-2">
        {STEPS.map((label, k) => {
          const done = k < view.stage;
          const cur = k === view.stage;
          return (
            <li key={label}>
              <Button
                variant="ghost"
                aria-current={cur ? 'step' : undefined}
                onClick={() => go(k as Stage)}
                className={cn(
                  'h-auto w-full justify-start gap-2.5 rounded-lg bg-card px-3.5 py-2.5 text-left font-normal text-muted-foreground',
                  cur && 'font-semibold text-foreground ring-[1.5px] ring-primary ring-inset',
                )}
              >
                <span
                  className={cn(
                    'grid size-[22px] flex-none place-items-center rounded-full font-mono text-xs',
                    done || cur
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-surface-3 text-muted-foreground',
                  )}
                >
                  {done ? <Check aria-hidden className="size-3.5" /> : k + 1}
                </span>
                {label}
              </Button>
            </li>
          );
        })}
      </ol>

      {view.guide && (
        <Alert
          role="status"
          className={cn(
            'rounded-[10px] border-0 px-4 py-2.5 text-sm',
            view.guide.tone === 'warn'
              ? 'bg-sev-caution/15 text-sev-caution'
              : 'bg-primary/15 text-point-soft',
          )}
        >
          {view.guide.text}
        </Alert>
      )}

      <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(320px,1fr)] items-start gap-3">
        <Card size="sm">
          <CardHeader>
            <CardTitle>
              <h2>{MAP_TITLE[view.stage]}</h2>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <SetupMap view={view} />
          </CardContent>
        </Card>
        <div className="flex flex-col gap-3">
          <CameraPanel view={view} />
          <StagePanel view={view} />
        </div>
      </div>

      <RulesPanel />
    </>
  );
}
