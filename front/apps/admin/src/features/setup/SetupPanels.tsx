import { tileLabel } from '@/features/cctv/cctv';
import { useCctvStore } from '@/features/cctv/cctv.store';
import { CctvDemo } from '@/features/cctv/CctvDemo';
import { CctvWall, type WallTile } from '@/features/cctv/CctvWall';
import { DIMMED, TILE_FRAME, tileZoom } from '@/features/cctv/tileStyle';
import { useCctvPreview } from '@/features/cctv/useCctvPreview';
import {
  CAMERA_HEIGHT_HELP,
  CHECKS,
  HEAD_ERR_MAX_CM,
  MAX_CAMERA_HEIGHT_M,
  PAIR_MISMATCH,
  RESULT,
  type HeightFieldView,
  type SetupTone,
  type Stage,
  type Step1CoverageView,
} from '@/features/setup/setup';
import { DEFAULT_CCTV_PREVIEW_URL } from '@/lib/parseEnv';
import { Badge } from '@simai/ui/components/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@simai/ui/components/card';
import { cn } from '@simai/ui/lib/utils';
import { useCallback, type ReactNode } from 'react';

// 세팅 화면 패널: 1단계 CCTV 4분할(기존 디코딩 재사용)·카메라 연결/방향 안내·카메라 높이 입력, 이후 단계 요약.
// 색은 위험 등급색(sev-critical·sev-high·sev-caution)과 겹치지 않는 토큰만 쓴다.

const DOT: Record<SetupTone, string> = {
  ok: 'bg-ok',
  active: 'bg-primary',
  delay: 'bg-delay',
  idle: 'bg-faint',
};

const TONE_BADGE: Record<SetupTone, string> = {
  ok: 'bg-ok/15 text-ok',
  active: 'bg-primary/15 text-point-soft',
  delay: 'bg-delay/15 text-delay',
  idle: 'bg-surface-3 text-muted-foreground',
};

const CAPTION =
  'absolute flex items-center gap-2 rounded-md bg-card/80 px-2 py-1 text-xs backdrop-blur-[4px]';

function readPreviewUrls() {
  const raw = import.meta.env as Record<string, unknown>;
  const localUrl =
    typeof raw.VITE_CCTV_PREVIEW_URL === 'string' && raw.VITE_CCTV_PREVIEW_URL.trim() !== ''
      ? raw.VITE_CCTV_PREVIEW_URL.trim()
      : DEFAULT_CCTV_PREVIEW_URL;
  const stompUrl =
    typeof raw.VITE_STOMP_URL === 'string' && raw.VITE_STOMP_URL.trim() !== ''
      ? raw.VITE_STOMP_URL.trim()
      : 'ws://localhost:8080/ws';
  return { localUrl, stompUrl };
}

function Dot({ tone }: { tone: SetupTone }) {
  return <i aria-hidden className={cn('size-2 flex-none rounded-full', DOT[tone])} />;
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle>
          <h2>{title}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">{children}</CardContent>
    </Card>
  );
}

function Rows({ children }: { children: ReactNode }) {
  return <ul className="divide-y divide-border">{children}</ul>;
}

function Row({ children }: { children: ReactNode }) {
  return <li className="flex items-center gap-2.5 px-0.5 py-2 text-sm">{children}</li>;
}

interface Stat {
  label: string;
  value: string | null;
  ok?: boolean;
}

function Stats({ items }: { items: Stat[] }) {
  return (
    <dl className="grid grid-cols-2 gap-2.5">
      {items.map((item) => (
        <div key={item.label} className="grid gap-0.5 rounded-lg bg-surface-2 px-3 py-2.5">
          <dt className="text-xs text-muted-foreground">{item.label}</dt>
          <dd className={cn('font-mono text-[17px] font-semibold', item.ok && 'text-ok')}>
            {item.value ?? (
              <>
                <span aria-hidden>—</span>
                <span className="sr-only">값 없음</span>
              </>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function LiveSetupTile({
  cameraId,
  attach,
  wall,
}: {
  cameraId: string;
  attach: (cameraId: string, canvas: HTMLCanvasElement | null) => void;
  wall: WallTile;
}) {
  const phase = useCctvStore((s) => s.views[cameraId]?.phase);
  const link = useCctvStore((s) => s.link);
  const playing = link === 'open' && phase === 'playing';
  const ref = useCallback(
    (canvas: HTMLCanvasElement | null) => attach(cameraId, canvas),
    [attach, cameraId],
  );
  return (
    <button
      type="button"
      onClick={wall.toggle}
      aria-label={`${cameraId} ${wall.focused ? '작게 보기' : '크게 보기'}`}
      className={cn(TILE_FRAME, 'bg-surface-2', tileZoom(wall.focused))}
    >
      <canvas
        ref={ref}
        aria-label={`${cameraId} 영상`}
        className={cn('size-full object-contain', !playing && DIMMED)}
      />
      <span className={cn(CAPTION, 'top-2 left-2')}>
        <span className="font-mono font-semibold">{cameraId}</span>
        <span>{tileLabel(phase, link)}</span>
      </span>
    </button>
  );
}

/** 1단계 실시간 CCTV 4칸(기존 CCTV WebCodecs 디코딩·모의 화면 재사용). */
export function SetupCctvPanel() {
  const { localUrl, stompUrl } = readPreviewUrls();
  const { attach } = useCctvPreview(localUrl, stompUrl);
  const link = useCctvStore((s) => s.link);
  const cameras = useCctvStore((s) => s.cameras);

  return (
    <Panel title="실시간 CCTV 4칸">
      <div className="min-h-[260px]">
        {link === 'open' && cameras.length > 0 ? (
          <CctvWall
            ids={cameras}
            tile={(id, wall) => <LiveSetupTile cameraId={id} attach={attach} wall={wall} />}
          />
        ) : (
          <div className="flex flex-col gap-2">
            <p className="text-xs text-muted-foreground">{tileLabel(undefined, link)}</p>
            <CctvDemo />
          </div>
        )}
      </div>
    </Panel>
  );
}

/** 1단계 카메라별 수신 상태·회전 방향 안내 및 6쌍 매칭 등급. */
export function CameraPanel({ coverage }: { coverage: Step1CoverageView }) {
  return (
    <Panel title="카메라 연결 · 방향 안내">
      <Rows>
        {coverage.cameras.map((cam) => (
          <Row key={cam.id}>
            <Dot tone={cam.tone} />
            <b className="font-mono">{cam.id}</b>
            <Badge
              className={cn(
                'h-auto rounded px-1.5 py-0.5 font-mono text-xs font-medium',
                TONE_BADGE[cam.tone],
              )}
            >
              {cam.statusLabel}
            </Badge>
            <span className="ml-auto text-right text-xs text-muted-foreground">
              {cam.guideText ?? '각도 양호'}
            </span>
          </Row>
        ))}
      </Rows>

      <div className="grid grid-cols-2 gap-2" role="list" aria-label="카메라 쌍 매칭 등급">
        {coverage.pairs.map((pair) => (
          <div
            key={pair.key}
            role="listitem"
            className="flex items-center justify-between gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5 text-xs"
          >
            <span className="font-mono font-medium">{pair.key}</span>
            <span className="flex items-center gap-1.5">
              {pair.inliers !== null && (
                <span className="font-mono text-muted-foreground">{pair.inliers}점</span>
              )}
              <Badge
                className={cn(
                  'h-auto rounded px-1.5 py-0.5 font-mono text-xs font-semibold',
                  TONE_BADGE[pair.tone],
                )}
              >
                {pair.gradeLabel}
              </Badge>
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

/** 1단계 카메라별 실측 높이(바닥~렌즈 중심, m) 입력 패널. */
export function CameraHeightPanel({
  fields,
  onChange,
  submitError,
}: {
  fields: readonly HeightFieldView[];
  onChange: (cameraId: string, value: string) => void;
  submitError: string | null;
}) {
  return (
    <Panel title="카메라 높이 입력 (바닥~렌즈 중심, m)">
      <p className="text-xs text-muted-foreground">{CAMERA_HEIGHT_HELP}</p>
      <div className="grid grid-cols-2 gap-2.5">
        {fields.map((field) => {
          const inputId = `camera-height-${field.cameraId}`;
          const needsInput = field.required || field.missing;
          return (
            <div
              key={field.cameraId}
              className="flex flex-col gap-1 rounded-lg bg-surface-2 px-3 py-2"
            >
              <div className="flex items-center justify-between gap-2 text-xs">
                <label htmlFor={inputId} className="font-mono font-semibold text-foreground">
                  {field.cameraId} 높이 (m)
                </label>
                <span
                  className={cn(
                    'text-[11px]',
                    field.missing || field.invalid
                      ? 'font-semibold text-delay'
                      : 'text-muted-foreground',
                  )}
                >
                  {field.missing
                    ? '입력 필수'
                    : field.invalid
                      ? `0~${MAX_CAMERA_HEIGHT_M}m 입력`
                      : field.required
                        ? '신규 입력'
                        : '지난 제출값'}
                </span>
              </div>
              <input
                id={inputId}
                type="number"
                inputMode="decimal"
                step="0.01"
                min="0.01"
                max={MAX_CAMERA_HEIGHT_M}
                required={needsInput}
                aria-invalid={field.missing || field.invalid}
                placeholder="예: 2.70"
                value={field.rawValue}
                onChange={(e) => onChange(field.cameraId, e.target.value)}
                className={cn(
                  'h-8 w-full rounded-md border bg-card px-2.5 font-mono text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  field.missing || field.invalid ? 'border-delay' : 'border-border',
                )}
              />
            </div>
          );
        })}
      </div>
      {submitError && (
        <p role="alert" className="text-xs font-medium text-delay">
          {submitError}
        </p>
      )}
    </Panel>
  );
}

function CheckPanel() {
  const maxErrorCm = Math.max(...CHECKS.map((c) => c.errorCm));
  return (
    <Panel title={`보정 검증 · 머리 높이 오차 ${HEAD_ERR_MAX_CM}cm 이하`}>
      <Rows>
        {CHECKS.map((check, n) => (
          <Row key={n}>
            <Dot tone="ok" />
            <span>{n + 1}번 지점</span>
            <span className="ml-auto font-mono">{`${check.errorCm}cm`}</span>
          </Row>
        ))}
      </Rows>
      <Stats
        items={[
          { label: '최대 오차', value: `${maxErrorCm}cm`, ok: true },
          { label: '카메라 쌍 불일치', value: PAIR_MISMATCH },
        ]}
      />
    </Panel>
  );
}

function ResultPanel({ cameraCount }: { cameraCount: number }) {
  const maxErrorCm = Math.max(...CHECKS.map((c) => c.errorCm));
  return (
    <Panel title="보정 결과">
      <Stats
        items={[
          { label: '재투영 중앙 · p95', value: RESULT.reprojection },
          { label: '머리 높이 오차', value: `최대 ${maxErrorCm}cm`, ok: true },
          { label: '바닥 평면 차이', value: RESULT.floorDiff },
          { label: '스케일 근거', value: `카메라 높이 ${cameraCount}대` },
          { label: '검증 지점', value: `${CHECKS.length} / ${CHECKS.length}` },
        ]}
      />
    </Panel>
  );
}

export function StagePanel({ stage, cameraCount }: { stage: Stage; cameraCount: number }) {
  if (stage === 1) return <CheckPanel />;
  return <ResultPanel cameraCount={cameraCount} />;
}
