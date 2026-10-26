import { useSpatialViewport, useZoneEditor } from '@/features/layout/SpatialViewportHost';
import type { ViewerEditBox } from '@/features/viewer3d';
import type { ZoneBox } from '@/features/zones/types';
import {
  changedFieldDeltas,
  formatMeters,
  sameEditBox,
  SERVER_SAVE_DISABLED_REASON,
} from '@/features/zones/zoneEditor';
import { ZoneNumericFields } from '@/features/zones/ZoneNumericFields';
import { Button } from '@simai/ui/components/button';
import { Input } from '@simai/ui/components/input';
import {
  ChevronDown,
  ChevronUp,
  FolderSearch,
  Play,
  RotateCcw,
  Save,
  SlidersHorizontal,
  X,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

function boxOfZone(zone: ZoneBox): ViewerEditBox {
  return {
    min: [zone.min[0], zone.min[1], zone.min[2]],
    max: [zone.max[0], zone.max[1], zone.max[2]],
  };
}

function CurrentBoxSummary({
  box,
  before,
  compareOpen,
  onToggleCompare,
}: {
  box: ViewerEditBox;
  before: ViewerEditBox;
  compareOpen: boolean;
  onToggleCompare: () => void;
}) {
  const w = formatMeters(box.max[0] - box.min[0]);
  const d = formatMeters(box.max[1] - box.min[1]);
  const h = formatMeters(box.max[2] - box.min[2]);
  const deltas = useMemo(() => changedFieldDeltas(before, box), [before, box]);

  return (
    <div className="flex flex-col gap-1.5 rounded-[12px] bg-surface-2 px-2.5 py-2 text-xs">
      <div className="grid grid-cols-2 gap-x-3 gap-y-1">
        <div className="flex flex-col">
          <span className="text-[11px] text-muted-foreground">기준 위치 (시작 X, Y)</span>
          <span className="font-mono font-semibold text-foreground">
            {formatMeters(box.min[0])}, {formatMeters(box.min[1])} m
          </span>
        </div>
        <div className="flex flex-col">
          <span className="text-[11px] text-muted-foreground">수평 크기 (폭 × 깊이)</span>
          <span className="font-mono font-semibold text-foreground">
            {w} × {d} m
          </span>
        </div>
        <div className="flex flex-col">
          <span className="text-[11px] text-muted-foreground">높이 범위 (하단 ~ 상단 Z)</span>
          <span className="font-mono font-semibold text-foreground">
            {formatMeters(box.min[2])} ~ {formatMeters(box.max[2])} m
          </span>
        </div>
        <div className="flex flex-col">
          <span className="text-[11px] text-muted-foreground">수직 높이 (ΔZ)</span>
          <span className="font-mono font-semibold text-foreground">
            {h} m ({box.min[2] > 0 ? '공중' : '바닥'})
          </span>
        </div>
      </div>

      {deltas.length > 0 && (
        <div className="flex flex-col gap-1 border-t border-border/60 pt-1.5">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-foreground">
              변경 항목 ({deltas.length}건)
            </span>
            <button
              type="button"
              aria-expanded={compareOpen}
              onClick={onToggleCompare}
              className="text-[11px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
            >
              {compareOpen ? '원본 비교 접기' : '원본 비교 보기'}
            </button>
          </div>
          <div className="flex flex-wrap gap-1">
            {deltas.map((item) => (
              <span
                key={item.field}
                className="rounded-md bg-surface-3 px-1.5 py-0.5 font-mono text-[11px] text-foreground"
              >
                {item.shortLabel} {item.deltaText}{' '}
                <span className="text-muted-foreground">(이전 {item.beforeText})</span>
              </span>
            ))}
          </div>
          {compareOpen && (
            <div className="mt-0.5 rounded-[8px] bg-surface-3 px-2 py-1 font-mono text-[11px] text-muted-foreground">
              <div>
                원본: X {formatMeters(before.min[0])}–{formatMeters(before.max[0])} · Y{' '}
                {formatMeters(before.min[1])}–{formatMeters(before.max[1])} · Z{' '}
                {formatMeters(before.min[2])}–{formatMeters(before.max[2])} m
              </div>
              <div className="text-foreground">
                현재: X {formatMeters(box.min[0])}–{formatMeters(box.max[0])} · Y{' '}
                {formatMeters(box.min[1])}–{formatMeters(box.max[1])} · Z {formatMeters(box.min[2])}
                –{formatMeters(box.max[2])} m
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ZonesPage({
  initialZoneId = null,
  autoStartEdit = false,
  onSearchConsumed,
}: {
  initialZoneId?: string | null;
  autoStartEdit?: boolean;
  onSearchConsumed?: () => void;
} = {}) {
  const viewport = useSpatialViewport()!;
  const editor = useZoneEditor()!;
  const { selectedZoneId, zones, zonesFailed, selectTarget } = viewport;
  const {
    canEditZones,
    editPreview,
    editSessionId,
    localDrafts,
    editNotice,
    requestZoneEdit,
    finishZoneEdit,
    cancelZoneEdit,
    discardLocalDraft,
    discardAllLocalDrafts,
    updateZoneField,
  } = editor;

  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [numericOpen, setNumericOpen] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [discardAllConfirm, setDiscardAllConfirm] = useState(false);
  const consumedSearchRef = useRef<string | null>(null);

  const selectedZone = useMemo(
    () => (selectedZoneId ? (zones.find((z) => z.zoneId === selectedZoneId) ?? null) : null),
    [zones, selectedZoneId],
  );

  const isEditingSelected =
    editPreview !== null &&
    editPreview.target.type === 'zone' &&
    editPreview.target.id === selectedZoneId;

  const draftList = useMemo(() => Object.values(localDrafts), [localDrafts]);
  const keptDraftForSelected =
    !isEditingSelected && selectedZoneId ? (localDrafts[selectedZoneId] ?? null) : null;

  // ?zone=z-xx(&edit=1) 로 진입했을 때 구역 데이터가 도착하고 3D 가 준비되면 선택·초점(필요 시 바로 편집)을 건다.
  // 이미 관제 화면에서 같은 구역을 골라 둔 채 넘어온 경우에는 기존 카메라 시점을 그대로 유지한다(U03).
  // 재시도는 SpatialViewportHost 단일 루프가 맡으므로 여기서는 중복 타이머를 돌리지 않는다.
  useEffect(() => {
    if (!initialZoneId) return;
    const token = `${initialZoneId}:${autoStartEdit ? '1' : '0'}`;
    if (consumedSearchRef.current === token) return;
    if (zones.length > 0 && !zones.some((z) => z.zoneId === initialZoneId)) return;

    consumedSearchRef.current = token;
    const shouldFocus = selectedZoneId !== initialZoneId;
    if (autoStartEdit) {
      requestZoneEdit(initialZoneId, { focus: shouldFocus });
    } else {
      selectTarget({ type: 'zone', id: initialZoneId }, { focus: shouldFocus });
    }
    onSearchConsumed?.();
  }, [
    initialZoneId,
    autoStartEdit,
    zones,
    selectedZoneId,
    requestZoneEdit,
    selectTarget,
    onSearchConsumed,
  ]);

  const filteredZones = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return zones;
    return zones.filter(
      (z) => z.zoneId.toLowerCase().includes(q) || z.name.toLowerCase().includes(q),
    );
  }, [zones, query]);

  const handlePickZone = (zoneId: string) => {
    selectTarget({ type: 'zone', id: zoneId }, { focus: true });
    setPickerOpen(false);
  };

  const handleStartEdit = () => {
    if (!selectedZoneId) return;
    requestZoneEdit(selectedZoneId, {
      continueFrom: keptDraftForSelected?.after,
    });
  };

  const hasUnsavedChange = useMemo(() => {
    if (isEditingSelected && editPreview) {
      return !sameEditBox(editPreview.before, editPreview.draft);
    }
    if (keptDraftForSelected) {
      return keptDraftForSelected.changed;
    }
    return false;
  }, [isEditingSelected, editPreview, keptDraftForSelected]);

  return (
    <section
      aria-label="구역 편집 도구"
      onKeyDown={(event) => {
        if (discardAllConfirm) event.stopPropagation();
        if (event.key === 'Escape' && (discardAllConfirm || pickerOpen)) {
          event.preventDefault();
          event.stopPropagation();
          setDiscardAllConfirm(false);
          setPickerOpen(false);
        }
      }}
      className="pointer-events-auto absolute top-[124px] left-3.5 z-[5] flex max-h-[calc(100svh-228px)] w-[356px] flex-col gap-2.5 overflow-y-auto rounded-[16px] bg-card/94 p-3 break-keep shadow-float ring-1 ring-border backdrop-blur-[8px] ring-inset"
    >
      <h1 className="sr-only">구역 편집</h1>
      {discardAllConfirm && (
        <div
          role="alertdialog"
          aria-label="전체 미리보기 복원 확인"
          className="rounded-[12px] bg-surface-2 p-3"
        >
          <p className="text-xs">
            미저장 미리보기 {draftList.length}건과 진행 중인 수정을 버리고 원본으로 돌아갑니다.
          </p>
          <div className="mt-2 flex gap-2">
            <Button size="sm" autoFocus onClick={() => setDiscardAllConfirm(false)}>
              돌아가기
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => {
                setDiscardAllConfirm(false);
                discardAllLocalDrafts();
              }}
            >
              모두 원본 복원
            </Button>
          </div>
        </div>
      )}

      {/* 1단: 모드·상태 배지(좌) + 구역 목록 토글 버튼(우) */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="shrink-0 rounded-md bg-surface-3 px-2 py-0.5 font-semibold whitespace-nowrap text-point-soft">
            구역 편집 모드
          </span>
          <span className="shrink-0 rounded-md bg-surface-2 px-2 py-0.5 font-medium whitespace-nowrap text-muted-foreground">
            {!selectedZone
              ? draftList.length > 0
                ? `미저장 ${draftList.length}건`
                : '구역 미선택'
              : hasUnsavedChange
                ? '미저장 변경'
                : '변경 없음'}
          </span>
          {draftList.length > 0 && (
            <span className="text-muted-foreground">전체 미저장 {draftList.length}건</span>
          )}
        </div>
        <Button
          size="sm"
          variant="secondary"
          aria-expanded={pickerOpen}
          onClick={() => setPickerOpen((prev) => !prev)}
          className="shrink-0 whitespace-nowrap"
        >
          <FolderSearch className="size-3.5" aria-hidden />
          구역 목록 ({zones.length})
        </Button>
      </div>

      {/* 2단: 미선택 안내 또는 선택된 구역 식별 헤더 (전체 폭 사용으로 줄바꿈 깨짐 방지) */}
      {selectedZone ? (
        <div
          onDoubleClick={() => {
            if (!isEditingSelected && canEditZones) handleStartEdit();
          }}
          className="flex items-center gap-2 rounded-[10px] bg-surface-2 px-2.5 py-1.5"
        >
          <span className="shrink-0 rounded-md bg-surface-3 px-2 py-0.5 font-mono text-xs font-semibold whitespace-nowrap text-foreground">
            {isEditingSelected ? `구역 수정 중 · ${selectedZoneId}` : selectedZone.zoneId}
          </span>
          <span className="min-w-0 truncate text-xs font-medium text-foreground">
            {selectedZone.name}
          </span>
        </div>
      ) : (
        <p className="rounded-[10px] bg-surface-2 px-3 py-2 text-xs text-muted-foreground">
          3D 구역을 클릭해 선택 · 더블클릭해 바로 수정
        </p>
      )}

      {/* 접이식 구역 검색·선택 목록 */}
      {pickerOpen && (
        <div
          role="region"
          aria-label="구역 목록 선택"
          className="flex flex-col gap-1.5 rounded-[12px] bg-surface-2 p-2 ring-1 ring-border ring-inset"
        >
          <Input
            aria-label="구역 검색"
            placeholder="구역 ID 또는 이름 검색…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(event) => {
              if (event.key !== 'Escape') return;
              event.preventDefault();
              event.stopPropagation();
              setPickerOpen(false);
            }}
            className="h-8 text-xs"
          />
          {zonesFailed && (
            <p role="alert" className="text-xs text-muted-foreground">
              구역 목록을 불러오지 못했습니다. 자동으로 다시 시도합니다.
            </p>
          )}
          <div className="flex max-h-[168px] flex-col gap-1 overflow-y-auto">
            {filteredZones.map((zone) => {
              const active = zone.zoneId === selectedZoneId;
              const hasDraft = Boolean(localDrafts[zone.zoneId]);
              return (
                <button
                  key={zone.zoneId}
                  type="button"
                  aria-pressed={active}
                  onClick={() => handlePickZone(zone.zoneId)}
                  className={
                    'flex items-center justify-between rounded-[8px] px-2.5 py-1.5 text-left text-xs transition-colors ' +
                    (active
                      ? 'bg-surface-3 font-semibold text-foreground ring-1 ring-primary'
                      : 'text-muted-foreground hover:bg-surface-3 hover:text-foreground')
                  }
                >
                  <span className="truncate">
                    <span className="font-mono font-semibold text-foreground">{zone.zoneId}</span> ·{' '}
                    {zone.name}
                  </span>
                  <span className="flex shrink-0 items-center gap-1">
                    {zone.min[2] > 0 && (
                      <span className="rounded bg-surface-3 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                        공중 {formatMeters(zone.min[2])}m
                      </span>
                    )}
                    {hasDraft && (
                      <span className="rounded bg-point-soft/20 px-1.5 py-0.5 text-[10px] font-medium text-point-soft">
                        미저장 미리보기
                      </span>
                    )}
                  </span>
                </button>
              );
            })}
            {filteredZones.length === 0 && (
              <p className="py-2 text-center text-xs text-muted-foreground">
                조건에 맞는 구역이 없습니다.
              </p>
            )}
          </div>
        </div>
      )}

      {/* 보관 중인 미저장 미리보기(다중 구역) 바로가기 칩 */}
      {draftList.length > 0 && (
        <div className="flex flex-col gap-1.5 rounded-[10px] bg-surface-2 px-2.5 py-2 text-xs">
          <div className="flex items-center justify-between">
            <span className="text-[11px] font-medium text-muted-foreground">
              미저장 미리보기 보관 중 ({draftList.length}개 구역)
            </span>
            {draftList.length > 1 && (
              <button
                type="button"
                onClick={() => setDiscardAllConfirm(true)}
                className="text-[11px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
              >
                전체 복원
              </button>
            )}
          </div>
          <div className="flex flex-wrap gap-1">
            {draftList.map((d) => (
              <button
                key={d.zoneId}
                type="button"
                onClick={() => handlePickZone(d.zoneId)}
                className="rounded-md bg-surface-3 px-2 py-0.5 font-mono text-xs text-foreground transition-colors hover:bg-surface-3/80"
              >
                {d.zoneId}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 구역 선택 후 · 편집 시작 전 (또는 미저장 미리보기 확인 상태) */}
      {selectedZone && !isEditingSelected && (
        <div className="flex flex-col gap-2">
          <CurrentBoxSummary
            box={keptDraftForSelected ? keptDraftForSelected.after : boxOfZone(selectedZone)}
            before={boxOfZone(selectedZone)}
            compareOpen={compareOpen}
            onToggleCompare={() => setCompareOpen((prev) => !prev)}
          />

          {/* 명세 14장: 구역 수정은 관리자만 한다. 다른 역할에는 편집 영역을 그리지 않는다. */}
          {!canEditZones ? null : keptDraftForSelected ? (
            <>
              <div className="flex flex-col gap-1.5">
                <div className="grid grid-cols-2 gap-1.5">
                  <Button size="sm" onClick={handleStartEdit} className="w-full">
                    <Play className="size-3.5" aria-hidden />
                    계속 편집
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => discardLocalDraft(selectedZone.zoneId)}
                    className="w-full"
                  >
                    <RotateCcw className="size-3.5" aria-hidden />
                    원본 복원
                  </Button>
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled
                  aria-disabled="true"
                  className="w-full"
                >
                  <Save className="size-3.5" aria-hidden />
                  저장 (서버 미연결)
                </Button>
              </div>
              <p className="rounded-[10px] bg-surface-2 px-2.5 py-1.5 text-[11px] leading-4 text-muted-foreground">
                {SERVER_SAVE_DISABLED_REASON}
              </p>
            </>
          ) : (
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-muted-foreground">
                {selectedZone.min[2] > 0
                  ? `공중 구역 (하단 ${formatMeters(selectedZone.min[2])}m)`
                  : '바닥 구역 (하단 0.00m)'}
              </span>
              <Button size="sm" onClick={handleStartEdit}>
                구역 수정
              </Button>
            </div>
          )}
        </div>
      )}

      {/* 활성 3D 연속 편집 세션: 중심 노란 구 + 면/높이 7핸들 동시 조작 + 접이식 정밀 입력 + 저장 판단 */}
      {selectedZone && isEditingSelected && editPreview && (
        <div className="flex flex-col gap-2">
          <div className="grid grid-cols-3 gap-1 text-center text-[11px] text-muted-foreground">
            <span className="rounded-md bg-surface-2 px-1.5 py-1 whitespace-nowrap">
              중앙 구 · XY 이동
            </span>
            <span className="rounded-md bg-surface-2 px-1.5 py-1 whitespace-nowrap">
              옆면(4) · 폭/깊이
            </span>
            <span className="rounded-md bg-surface-2 px-1.5 py-1 whitespace-nowrap">
              원뿔(2) · 높이 Z
            </span>
          </div>

          <CurrentBoxSummary
            box={editPreview.draft}
            before={editPreview.before}
            compareOpen={compareOpen}
            onToggleCompare={() => setCompareOpen((prev) => !prev)}
          />

          {/* 필요한 경우만 펼치는 소형 World(m) 수치 속성 패널 */}
          <div className="flex flex-col gap-1.5">
            <Button
              size="sm"
              variant="ghost"
              aria-expanded={numericOpen}
              onClick={() => setNumericOpen((prev) => !prev)}
              className="justify-between px-2 text-xs"
            >
              <span className="flex items-center gap-1.5">
                <SlidersHorizontal className="size-3.5" aria-hidden />
                정밀 입력 (World, m)
              </span>
              {numericOpen ? (
                <ChevronUp className="size-3.5" aria-hidden />
              ) : (
                <ChevronDown className="size-3.5" aria-hidden />
              )}
            </Button>

            {numericOpen && (
              <ZoneNumericFields
                key={editSessionId}
                preview={editPreview}
                disabled={!canEditZones}
                updateField={updateZoneField}
                finish={finishZoneEdit}
              />
            )}
          </div>

          {/* 저장/취소/미리보기 유지 행동 영역 */}
          <div className="flex flex-col gap-1.5 pt-0.5">
            <div className="grid grid-cols-2 gap-1.5">
              <Button size="sm" onClick={() => finishZoneEdit()} className="w-full">
                미리보기 유지
              </Button>
              <Button
                size="sm"
                variant="secondary"
                onClick={() => cancelZoneEdit()}
                className="w-full"
              >
                <X className="size-3.5" aria-hidden />
                이번 수정 취소
              </Button>
            </div>
            <Button size="sm" variant="secondary" disabled aria-disabled="true" className="w-full">
              <Save className="size-3.5" aria-hidden />
              저장 (서버 미연결)
            </Button>
          </div>
          <p className="rounded-[10px] bg-surface-2 px-2.5 py-1.5 text-[11px] leading-4 text-muted-foreground">
            {SERVER_SAVE_DISABLED_REASON}
          </p>
        </div>
      )}

      {editNotice && (
        <p
          role="status"
          className={
            'rounded-[10px] px-2.5 py-1.5 text-xs ' +
            (editNotice.tone === 'warning'
              ? 'bg-surface-3 font-medium text-foreground ring-1 ring-border ring-inset'
              : 'bg-surface-2 text-muted-foreground')
          }
        >
          {editNotice.text}
        </p>
      )}
    </section>
  );
}
