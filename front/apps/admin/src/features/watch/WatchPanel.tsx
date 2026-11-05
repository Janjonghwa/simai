import { useCurrentRole } from '@/features/auth/useCurrentRole';
import { useSpatialViewport } from '@/features/layout/SpatialViewportHost';
import { targetOfKey } from '@/features/layout/spatialHost/selectionKey';
import { useWatchBindingMutations, useWatchSummary } from '@/features/watch/useWatch';
import { channelOf } from '@/features/watch/watchStatus';
import { ApiError } from '@/lib/http';
import { Button } from '@simai/ui/components/button';
import { useEffect, useState } from 'react';

const CHANNEL_TEXT = { BOUND: '연결됨', LOST: '워치 끊김', UNBOUND: '연결된 워치 없음' } as const;

function errorText(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError && error.serverMessage) return error.serverMessage;
  return '요청이 실패했습니다. 다시 시도하세요';
}

/**
 * 관제 화면 워치 패널(S15P31A101-342, 확장 부록 C.6, FR-117). 추적 인원 vs 활성 워치 수, 워치별 연결·배터리·배정,
 * 3D 에서 고른 사람의 채널 상태를 보인다. 관리자는 고른 사람에게 워치를 손으로 잇고 풀 수 있다(움직임 대조보다 앞선다).
 * 사람 이름은 다루지 않는다: 익명 World ID 와 워치 기기 ID 만 보인다.
 */
export function WatchPanel() {
  const role = useCurrentRole();
  const admin = role === 'ADMIN';
  const { rows, tracked, registered, connected, failed } = useWatchSummary();
  const { bind, release } = useWatchBindingMutations();
  const viewport = useSpatialViewport();
  const target = targetOfKey(viewport?.selectedKey ?? null);
  const selectedWorld = target?.type === 'person' ? target.id : null;
  const selectedRow = selectedWorld ? rows.find((r) => r.worldId === selectedWorld) : undefined;
  const [device, setDevice] = useState('');
  const resetBind = bind.reset;
  const resetRelease = release.reset;
  // 다른 사람을 고르면 고른 워치를 지우고(렌더 중 맞춤, MonitorPage 와 같은 방식) 이전 요청의 오류도 지운다
  // (오류가 다른 사람 화면에 붙어 보이지 않게)
  const [deviceFor, setDeviceFor] = useState(selectedWorld);
  if (deviceFor !== selectedWorld) {
    setDeviceFor(selectedWorld);
    setDevice('');
  }
  useEffect(() => {
    resetBind();
    resetRelease();
  }, [selectedWorld, resetBind, resetRelease]);
  // 기본은 그 사람의 지금 워치, 없으면 배정 없는 워치, 그것도 없으면 첫 워치. 고른 워치가 사라지면(폐기) 기본으로
  const chosen = rows.some((r) => r.deviceId === device)
    ? device
    : (selectedRow?.deviceId ?? rows.find((r) => !r.worldId)?.deviceId ?? rows[0]?.deviceId ?? '');
  const chosenRow = rows.find((r) => r.deviceId === chosen);
  const alreadyHere = chosenRow !== undefined && chosenRow.worldId === selectedWorld;
  const movingFrom = chosenRow?.worldId && !alreadyHere ? chosenRow.worldId : null;
  const pending = bind.isPending || release.isPending;
  const error = errorText(bind.error ?? release.error);

  return (
    <section
      aria-label="워치"
      className="pointer-events-auto absolute bottom-5 left-[124px] z-[6] w-72 rounded-lg bg-card/95 p-3 text-xs text-foreground shadow-float"
    >
      <h2 className="mb-2 flex items-baseline justify-between text-sm font-semibold">
        <span>워치</span>
        <span>
          추적 {tracked}명 · 활성 워치 {connected}대
          <span className="text-muted-foreground"> / 등록 {registered}</span>
        </span>
      </h2>
      {failed && <p className="mb-2 text-destructive">워치 배정을 불러오지 못했습니다</p>}
      {rows.length === 0 ? (
        <p className="text-muted-foreground">등록된 워치가 없습니다</p>
      ) : (
        <ul aria-label="워치 목록" className="mb-2 space-y-1">
          {rows.map((row) => (
            <li key={row.deviceId} className="flex items-center justify-between gap-2">
              <span>
                <span
                  className={row.connected ? 'text-emerald-500' : 'text-destructive'}
                  aria-hidden
                >
                  ●
                </span>{' '}
                {row.deviceId} {row.connected ? '연결' : '끊김'}
                {row.batteryPct !== null && (
                  <span className="text-muted-foreground"> {row.batteryPct}%</span>
                )}
                <span className="text-muted-foreground">
                  {' '}
                  → {row.worldId ?? '배정 없음'}
                  {row.method === 'MOTION_MATCH' && ' (자동)'}
                </span>
              </span>
              {admin && row.worldId && (
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={pending}
                  onClick={() => {
                    resetBind();
                    release.mutate(row.deviceId);
                  }}
                >
                  풀기
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {selectedWorld && (
        <div className="border-t border-border pt-2">
          <p>
            선택한 사람 <strong>{selectedWorld}</strong>: {CHANNEL_TEXT[channelOf(selectedRow)]}
            {selectedRow && ` (${selectedRow.deviceId})`}
          </p>
          {admin && rows.length > 0 && (
            <div className="mt-2 flex items-center gap-2">
              <select
                aria-label="이을 워치"
                className="flex-1 rounded border border-border bg-background px-2 py-1"
                value={chosen}
                onChange={(e) => setDevice(e.target.value)}
              >
                {rows.map((row) => (
                  <option key={row.deviceId} value={row.deviceId}>
                    {row.deviceId}
                    {row.worldId ? ` (지금 ${row.worldId})` : ''}
                  </option>
                ))}
              </select>
              <Button
                size="sm"
                disabled={pending || !chosen || alreadyHere}
                onClick={() => {
                  resetRelease();
                  bind.mutate({ deviceId: chosen, worldId: selectedWorld });
                }}
              >
                이 사람에게 잇기
              </Button>
            </div>
          )}
          {admin && movingFrom && (
            <p className="mt-1 text-amber-500">
              {chosen} 은 지금 {movingFrom} 에 이어져 있습니다. 이으면 이 사람에게 옮깁니다
            </p>
          )}
          {admin && selectedRow && !alreadyHere && (
            <p className="mt-1 text-amber-500">
              이 사람의 지금 워치({selectedRow.deviceId})는 풀립니다
            </p>
          )}
        </div>
      )}
      {!selectedWorld && admin && rows.length > 0 && (
        <p className="text-muted-foreground">3D 에서 사람을 고르면 워치를 이을 수 있습니다</p>
      )}
      {error && (
        <p role="alert" className="mt-2 text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}
