import { PageTitle } from '@/features/layout/PageTitle';
import { RulesPanel } from '@/features/rules/RulesPanel';
import { SetupMap } from '@/features/setup/SetupMap';
import {
  CameraHeightPanel,
  CameraPanel,
  SetupCctvPanel,
  StagePanel,
} from '@/features/setup/SetupPanels';
import {
  ACTION_LABEL,
  deriveHeightFields,
  deriveStep1Coverage,
  MAP_TITLE,
  resolveHeightCameraIds,
  SECONDARY_ACTIONS,
  STEPS,
  validateHeightFields,
  type ActionKey,
  type CalibrationState,
  type SetupTone,
  type Stage,
} from '@/features/setup/setup';
import {
  useCameraHeightDefaults,
  useCoverageGuides,
  useCreateSetupSession,
} from '@/features/setup/useSetupStep1';
import { Alert } from '@simai/ui/components/alert';
import { Badge } from '@simai/ui/components/badge';
import { Button } from '@simai/ui/components/button';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useNavigate } from '@tanstack/react-router';
import { Check } from 'lucide-react';
import { useCallback, useMemo, useState } from 'react';

// 설치 · 보정(명세 8.1·13.4·18.1, S15P31A101-448):
// 1단계는 CCTV 4칸 실시간 미리보기, 카메라 각도 헬퍼(GET /api/cameras/coverage-guides 2~3초 폴링:
// 연결 상태 3종·6쌍 충분/약함/부족·회전 방향 안내 한 줄·영상 없음/계산 실패/STALE 표시, 막지 않음),
// 카메라별 실측 높이 입력(GET /api/setup-sessions/camera-height-defaults 기본 채움, 빈칸만 입력 필수,
// POST /api/setup-sessions 제출값이 다음 기본값)으로 구성한다. 걷기 보정과 정물 배치는 없다.

const STATE_TONE: Record<CalibrationState, string> = {
  UNCALIBRATED: 'bg-surface-3 text-muted-foreground',
  PROVISIONAL: 'bg-primary/15 text-point-soft',
  DRIFTED: 'bg-delay/15 text-delay',
  CALIBRATED: 'bg-ok/15 text-ok',
};

const TONE_BADGE: Record<SetupTone, string> = {
  ok: 'bg-ok/15 text-ok',
  active: 'bg-primary/15 text-point-soft',
  delay: 'bg-delay/15 text-delay',
  idle: 'bg-surface-3 text-muted-foreground',
};

const STAGE_CALIBRATION: readonly { state: CalibrationState; version: string }[] = [
  { state: 'UNCALIBRATED', version: '세팅 1단계 · 카메라 확인' },
  { state: 'PROVISIONAL', version: '후보 cal-8' },
  { state: 'CALIBRATED', version: 'cal-8' },
  { state: 'CALIBRATED', version: 'cal-8' },
];

export function SetupPage({ workUnitId = 1 }: { workUnitId?: number } = {}) {
  const [stage, setStage] = useState<Stage>(0);
  const [heightEdits, setHeightEdits] = useState<Record<string, string>>({});
  const navigate = useNavigate();

  const step1Active = stage === 0;
  const coverageQuery = useCoverageGuides(step1Active);
  const heightDefaultsQuery = useCameraHeightDefaults();

  const go = useCallback((next: Stage) => {
    const clamped = Math.min(next, (STEPS.length - 1) as Stage) as Stage;
    setStage(clamped);
  }, []);

  const createSession = useCreateSetupSession(() => {
    setHeightEdits({});
    go(1);
  });

  const cameraIds = useMemo(
    () => resolveHeightCameraIds(heightDefaultsQuery.data, coverageQuery.data),
    [heightDefaultsQuery.data, coverageQuery.data],
  );

  const heightFields = useMemo(
    () => deriveHeightFields(cameraIds, heightDefaultsQuery.data?.camera_heights_m, heightEdits),
    [cameraIds, heightDefaultsQuery.data?.camera_heights_m, heightEdits],
  );

  const heightValidation = useMemo(() => validateHeightFields(heightFields), [heightFields]);

  const coverage = useMemo(
    () => deriveStep1Coverage(coverageQuery.data, coverageQuery.error, cameraIds),
    [coverageQuery.data, coverageQuery.error, cameraIds],
  );

  const onChangeHeight = useCallback((cameraId: string, value: string) => {
    setHeightEdits((prev) => ({ ...prev, [cameraId]: value }));
  }, []);

  const submitStep1 = useCallback(() => {
    if (!heightValidation.valid || createSession.pending) return;
    createSession.submit({
      work_unit_id: workUnitId,
      camera_heights_m: heightValidation.cameraHeightsM,
    });
  }, [heightValidation, createSession, workUnitId]);

  const stageActions: ActionKey[] =
    stage === 0 ? [] : stage === 1 ? ['confirm'] : ['restart', 'zones'];
  const stageMeta = STAGE_CALIBRATION[stage] ?? STAGE_CALIBRATION[0]!;

  const run: Record<ActionKey | 'rewalk', () => void> = {
    start: submitStep1,
    rewalk: () => go(1),
    confirm: () => go(3),
    restart: () => go(0),
    zones: () => void navigate({ to: '/', search: { edit: true } }),
  };

  return (
    <>
      <PageTitle>설치 · 보정</PageTitle>
      <div className="flex flex-wrap items-center gap-3">
        {stage === 0 ? (
          <>
            <Badge
              aria-label={`카메라 연결 상태 ${coverage.connectivity}`}
              className={cn(
                'h-auto rounded-md px-2.5 py-0.5 font-mono text-[13px] font-semibold',
                TONE_BADGE[coverage.connectivityTone],
              )}
            >
              {coverage.connectivity} · {coverage.connectivityLabel}
            </Badge>
            {coverage.stale && (
              <Badge
                aria-label="각도 안내 오래된 결과 STALE"
                className="h-auto rounded-md bg-delay/15 px-2.5 py-0.5 font-mono text-[13px] font-semibold text-delay"
              >
                STALE · 오래된 결과
              </Badge>
            )}
            {coverage.calculationFailed && (
              <Badge
                aria-label="각도 안내 계산 실패"
                className="h-auto rounded-md bg-surface-3 px-2.5 py-0.5 text-[13px] font-semibold text-muted-foreground"
              >
                계산 실패
              </Badge>
            )}
          </>
        ) : (
          <Badge
            className={cn(
              'h-auto rounded-md px-2.5 py-0.5 font-mono text-[13px] font-semibold',
              STATE_TONE[stageMeta.state],
            )}
          >
            {stageMeta.state}
          </Badge>
        )}
        <span className="font-mono text-muted-foreground">
          {createSession.session?.setup_session_id
            ? `세션 ${createSession.session.setup_session_id}`
            : stageMeta.version}
        </span>
        <div className="ml-auto flex flex-wrap gap-2">
          {stage === 0 ? (
            <Button
              size="lg"
              className="px-4"
              disabled={!heightValidation.valid || createSession.pending}
              onClick={run.start}
            >
              {coverage.submitLabel}
            </Button>
          ) : (
            stageActions.map((key) => {
              const secondary = SECONDARY_ACTIONS.has(key);
              return (
                <Button
                  key={key}
                  size="lg"
                  variant={secondary ? 'secondary' : 'default'}
                  className={cn('px-4', secondary && 'bg-surface-3 hover:bg-surface-3/70')}
                  onClick={run[key]}
                >
                  {ACTION_LABEL[key]}
                </Button>
              );
            })
          )}
        </div>
      </div>

      <ol aria-label="보정 단계" className="grid grid-cols-3 gap-2">
        {STEPS.map((label, k) => {
          const done = k < stage;
          const cur = k === stage;
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

      {stage === 0 && (
        <Alert
          role="status"
          aria-label="카메라 방향 안내"
          className={cn(
            'flex flex-wrap items-center justify-between gap-2 rounded-[10px] border-0 px-4 py-2.5 text-sm',
            coverage.stale || !coverage.canProceedCleanly
              ? 'bg-delay/15 text-delay'
              : 'bg-primary/15 text-point-soft',
          )}
        >
          <span>{coverage.directionLine}</span>
          {coverage.statusNotices.length > 0 && (
            <span className="font-mono text-xs">{coverage.statusNotices.join(' · ')}</span>
          )}
        </Alert>
      )}

      {stage === 0 ? (
        <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(320px,1fr)] items-start gap-3">
          <div className="flex flex-col gap-3">
            <SetupCctvPanel />
            <Card size="sm">
              <CardHeader>
                <CardTitle>
                  <h2>{MAP_TITLE[0]}</h2>
                </CardTitle>
              </CardHeader>
              <CardContent>
                <SetupMap stage={0} coverage={coverage} />
              </CardContent>
            </Card>
          </div>
          <div className="flex flex-col gap-3">
            <CameraPanel coverage={coverage} />
            <CameraHeightPanel
              fields={heightFields}
              onChange={onChangeHeight}
              submitError={createSession.error}
            />
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-[minmax(0,1.25fr)_minmax(320px,1fr)] items-start gap-3">
          <Card size="sm">
            <CardHeader>
              <CardTitle>
                <h2>{MAP_TITLE[stage]}</h2>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <SetupMap stage={stage} coverage={coverage} />
            </CardContent>
          </Card>
          <div className="flex flex-col gap-3">
            <CameraPanel coverage={coverage} />
            <StagePanel stage={stage} cameraCount={cameraIds.length} />
          </div>
        </div>
      )}

      <RulesPanel />
    </>
  );
}
