import { useSpatialViewport, useZoneEditor } from '@/features/layout/SpatialViewportHost';
import { ConfigApplyBar } from '@/features/surfaces/ConfigApplyBar';
import type { ViewerEditBox } from '@/features/viewer3d';
import { NewZoneForm } from '@/features/zones/NewZoneForm';
import type { ZoneBox } from '@/features/zones/types';
import { useConfigApply } from '@/features/zones/useConfigApply';
import { saveErrorText, useZoneSave } from '@/features/zones/useZoneSave';
import { ZoneAttributesForm } from '@/features/zones/ZoneAttributesForm';
import { changedFieldDeltas, formatMeters, sameEditBox } from '@/features/zones/zoneEditor';
import {
  formErrors,
  formOf,
  NEW_ZONE_FORM,
  newZoneBodyOf,
  newZoneBox,
  newZoneErrors,
  patchOf,
  sameForm,
  type ZoneForm,
  type ZonePatch,
} from '@/features/zones/zoneForm';
import { ZoneNumericFields } from '@/features/zones/ZoneNumericFields';
import { Button } from '@simai/ui/components/button';
import { Input } from '@simai/ui/components/input';
import { ChevronDown, ChevronUp, Play, RotateCcw, Save, SlidersHorizontal, X } from 'lucide-react';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

function boxOfZone(zone: ZoneBox): ViewerEditBox {
  return {
    min: [zone.min[0], zone.min[1], zone.min[2]],
    max: [zone.max[0], zone.max[1], zone.max[2]],
  };
}

/** 고친 칸. until 은 저장해서 생긴 버전이다. 그 버전이 목록에 들어오면 버린다(저장 직후 옛 값이 비치지 않게 한다). */
interface FormEdit {
  patch: ZonePatch;
  until?: number;
}

function withoutEdit(edits: Record<string, FormEdit>, zoneId: string) {
  if (!(zoneId in edits)) return edits;
  const next = { ...edits };
  delete next[zoneId];
  return next;
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
  const { selectedZoneId, zones, zonesRaw, zonesFailed, selectTarget } = viewport;
  const {
    canEditZones,
    editPreview,
    editSessionId,
    localDrafts,
    editNotice,
    setEditNotice,
    requestZoneEdit,
    finishZoneEdit,
    cancelZoneEdit,
    discardLocalDraft,
    discardAllLocalDrafts,
    updateZoneField,
  } = editor;

  const [pickerOpen, setPickerOpen] = useState(false);
  // 드롭다운은 패널(스크롤 영역) 밖으로 펼쳐지도록 화면 위에 띄우고, 목록 버튼 바로 아래에 둔다.
  const [pickerAnchor, setPickerAnchor] = useState<DOMRect | null>(null);
  // 구역 목록은 바깥을 누르면 닫는다(관제 데모와 같다). 목록 버튼은 바깥으로 치지 않는다(누르면 직접 토글한다).
  const pickerRef = useRef<HTMLDivElement>(null);
  const pickerButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!pickerOpen) return undefined;
    const close = (event: PointerEvent) => {
      const target = event.target as Node;
      if (pickerRef.current?.contains(target) || pickerButtonRef.current?.contains(target)) return;
      setPickerOpen(false);
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [pickerOpen]);
  // 열려 있는 동안 패널이 스크롤되거나 창 크기가 바뀌면 목록 버튼 아래로 다시 맞춘다.
  useLayoutEffect(() => {
    if (!pickerOpen) return undefined;
    const place = () => {
      const rect = pickerButtonRef.current?.getBoundingClientRect();
      if (!rect) return;
      setPickerAnchor((prev) =>
        prev && prev.bottom === rect.bottom && prev.left === rect.left && prev.width === rect.width
          ? prev
          : rect,
      );
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [pickerOpen]);
  const [query, setQuery] = useState('');
  const [numericOpen, setNumericOpen] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [discardAllConfirm, setDiscardAllConfirm] = useState(false);
  // 고치는 중인 속성(바꾼 칸만). 구역별로 두어 형상 편집으로 넘어갔다 와도 남는다. 나머지 칸은 늘 받은 최신 버전이라
  // 409 로 최신 버전을 다시 받으면 다른 사람이 바꾼 칸이 보이고, 다시 저장해도 덮지 않는다.
  const [edits, setEdits] = useState<Record<string, FormEdit>>({});
  const consumedSearchRef = useRef<string | null>(null);
  const { save, create } = useZoneSave();
  // 새 구역 양식. 상자를 손대기 전에는 구역 목록이 바뀌면 그 가운데로 따라간다.
  const [newForm, setNewForm] = useState(NEW_ZONE_FORM);
  const [newBoxEdit, setNewBoxEdit] = useState<ViewerEditBox | null>(null);
  const newBox = useMemo(() => newBoxEdit ?? newZoneBox(zones), [newBoxEdit, zones]);
  const newErrors = useMemo(() => newZoneErrors(newForm), [newForm]);
  const apply = useConfigApply(zones);

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

  const savedForm = useMemo(() => (selectedZone ? formOf(selectedZone) : null), [selectedZone]);
  const edit = selectedZone ? edits[selectedZone.zoneId] : undefined;
  const livePatch =
    edit && (edit.until === undefined || selectedZone!.version < edit.until) ? edit.patch : {};
  const form = savedForm ? { ...savedForm, ...livePatch } : null;
  const formDirty = form !== null && savedForm !== null && !sameForm(form, savedForm);
  const errors =
    form && savedForm && selectedZone
      ? formErrors(form, { saved: savedForm, hasHeight: selectedZone.zRange !== null })
      : {};
  const canSave =
    canEditZones &&
    (hasUnsavedChange || formDirty) &&
    form !== null &&
    Object.keys(errors).length === 0 &&
    !save.isPending;

  const setForm = (next: ZoneForm) => {
    if (selectedZone && savedForm)
      setEdits((prev) => ({ ...prev, [selectedZone.zoneId]: { patch: patchOf(savedForm, next) } }));
  };

  // isPending 은 다음 턴에 바뀌어, 같은 턴에 두 번 눌리면 두 번 보낸다. 보내는 중인 요청은 여기서 바로 막는다.
  const sending = useRef(new Set<'save' | 'create'>());
  const begin = (key: 'save' | 'create') => {
    if (sending.current.has(key)) return null;
    sending.current.add(key);
    return () => sending.current.delete(key);
  };

  // 형상을 고치는 중이면 먼저 편집을 끝내고 그 결과를 함께 저장한다. 속성만 고쳤으면 받은 형상을 그대로 보낸다.
  const handleSave = () => {
    if (!selectedZone || !form || !canSave || sending.current.has('save')) return;
    let box = keptDraftForSelected?.after ?? null;
    if (isEditingSelected) {
      const result = finishZoneEdit();
      if (result?.status !== 'FINISHED') return;
      box = result.changed ? result.after : null;
    }
    const zone = selectedZone;
    const done = begin('save');
    if (!done) return;
    save.mutate(
      { zone, form, box },
      {
        onSettled: done,
        onSuccess: (saved) => {
          setEdits((prev) => {
            const done = prev[zone.zoneId];
            return done ? { ...prev, [zone.zoneId]: { ...done, until: saved.zone_version } } : prev;
          });
          setEditNotice({
            tone: 'neutral',
            text: `저장했습니다 (${saved.zone_id} v${saved.zone_version}).`,
          });
        },
        onError: (error) => setEditNotice({ tone: 'warning', text: saveErrorText(error) }),
      },
    );
  };

  const handleRestore = () => {
    if (!selectedZone) return;
    if (keptDraftForSelected) discardLocalDraft(selectedZone.zoneId);
    setEdits((prev) => withoutEdit(prev, selectedZone.zoneId));
  };

  // 드롭다운의 「새 구역」: 고친 형상은 미리보기로 남기고 선택을 풀어 새 구역 양식을 보인다.
  const handlePickNew = () => {
    if (isEditingSelected) finishZoneEdit();
    viewport.clearSelection();
    setPickerOpen(false);
  };

  // 새 구역은 양식의 속성·상자로 만들고(v1) 바로 형상 편집을 시작한다. 양식은 처음 값으로 돌린다. 현장에는 발행해야 간다.
  const handleCreate = () => {
    if (Object.keys(newErrors).length > 0) return;
    const done = begin('create');
    if (!done) return;
    create.mutate(newZoneBodyOf(newForm, newBox), {
      onSettled: done,
      onSuccess: (zone) => {
        setNewForm(NEW_ZONE_FORM);
        setNewBoxEdit(null);
        requestZoneEdit(zone.zone_id, { focus: true });
      },
      onError: (error) => setEditNotice({ tone: 'warning', text: saveErrorText(error) }),
    });
  };

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

      {/* 구역 선택 드롭다운. 기본은 「새 구역」(양식), 구역을 고르면 그 구역이다(관제 데모와 같다). */}
      <Button
        ref={pickerButtonRef}
        variant="secondary"
        aria-label={`구역 목록: ${selectedZone ? `${selectedZone.zoneId} ${selectedZone.name}` : '새 구역'}`}
        aria-haspopup="dialog"
        aria-expanded={pickerOpen}
        onClick={() => setPickerOpen((prev) => !prev)}
        className="w-full justify-between"
      >
        <span className="min-w-0 truncate">
          {selectedZone ? (
            <>
              <span className="font-mono font-semibold">{selectedZone.zoneId}</span> ·{' '}
              {selectedZone.name}
            </>
          ) : (
            '새 구역'
          )}
        </span>
        <ChevronDown className="size-4 shrink-0" aria-hidden />
      </Button>

      <ConfigApplyBar
        apply={apply}
        unpublishedCount={apply.unpublished?.size ?? 0}
        canEdit={canEditZones}
        onError={(text) => setEditNotice({ tone: 'warning', text })}
      />

      {!selectedZone && canEditZones && (
        <NewZoneForm
          form={newForm}
          box={newBox}
          errors={newErrors}
          ready={Boolean(zonesRaw)}
          pending={create.isPending}
          summary={
            <CurrentBoxSummary
              box={newBox}
              before={newBox}
              compareOpen={false}
              onToggleCompare={() => undefined}
            />
          }
          onFormChange={setNewForm}
          onBoxChange={setNewBoxEdit}
          onAdd={handleCreate}
        />
      )}

      {/* 구역 목록 드롭다운: 검색과 목록. 바깥을 누르거나 Esc 로 닫는다(관제 데모와 같다). */}
      {pickerOpen &&
        createPortal(
          <div
            ref={pickerRef}
            role="region"
            aria-label="구역 목록 선택"
            style={
              pickerAnchor
                ? {
                    top: pickerAnchor.bottom + 6,
                    left: pickerAnchor.left,
                    width: pickerAnchor.width,
                  }
                : undefined
            }
            className="fixed z-[20] flex w-[330px] flex-col gap-1.5 rounded-[12px] bg-popover p-2 shadow-float ring-1 ring-border ring-inset"
          >
            <Input
              aria-label="구역 검색"
              placeholder="검색"
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
            <div className="flex max-h-[220px] flex-col gap-1 overflow-y-auto">
              {/* 관리자가 아니면 양식은 없고 선택만 푼다. */}
              <button
                type="button"
                aria-pressed={!selectedZone}
                onClick={handlePickNew}
                className={
                  'flex items-center rounded-[8px] px-2.5 py-1.5 text-left text-xs transition-colors ' +
                  (!selectedZone
                    ? 'bg-surface-3 font-semibold text-foreground ring-1 ring-primary'
                    : 'text-muted-foreground hover:bg-surface-3 hover:text-foreground')
                }
              >
                새 구역
              </button>
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
                      <span className="font-mono font-semibold text-foreground">{zone.zoneId}</span>{' '}
                      · {zone.name}
                    </span>
                    <span className="flex shrink-0 items-center gap-1">
                      <span className="font-mono text-[10px] text-muted-foreground">
                        v{zone.version}
                      </span>
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
                      {apply.unpublished?.has(zone.zoneId) && (
                        <span className="rounded bg-surface-3 px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          발행 전
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
          </div>,
          document.body,
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
      {selectedZone && !isEditingSelected && form && (
        <div className="flex flex-col gap-2">
          <CurrentBoxSummary
            box={keptDraftForSelected ? keptDraftForSelected.after : boxOfZone(selectedZone)}
            before={boxOfZone(selectedZone)}
            compareOpen={compareOpen}
            onToggleCompare={() => setCompareOpen((prev) => !prev)}
          />

          <ZoneAttributesForm
            form={form}
            errors={errors}
            onChange={setForm}
            readOnly={!canEditZones}
            disabled={save.isPending}
          />

          {/* 명세 14장: 구역 수정은 관리자만 한다. 다른 역할에는 편집 영역을 그리지 않는다. */}
          {canEditZones && (
            <div className="flex flex-col gap-1.5">
              {keptDraftForSelected ? (
                <Button size="sm" onClick={handleStartEdit} className="w-full">
                  <Play className="size-3.5" aria-hidden />
                  계속 편집
                </Button>
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
              <div className="grid grid-cols-2 gap-1.5">
                <Button size="sm" disabled={!canSave} onClick={handleSave} className="w-full">
                  <Save className="size-3.5" aria-hidden />
                  {save.isPending ? '저장 중…' : '저장'}
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={!keptDraftForSelected && !formDirty}
                  onClick={handleRestore}
                  className="w-full"
                >
                  <RotateCcw className="size-3.5" aria-hidden />
                  원본 복원
                </Button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 활성 3D 연속 편집 세션: 중심 노란 구 + 면/높이 7핸들 동시 조작 + 접이식 정밀 입력 + 저장 판단 */}
      {selectedZone && isEditingSelected && editPreview && (
        <div className="flex flex-col gap-2">
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
                정밀 입력 (m)
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
            <Button size="sm" disabled={!canSave} onClick={handleSave} className="w-full">
              <Save className="size-3.5" aria-hidden />
              {save.isPending ? '저장 중…' : '저장'}
            </Button>
          </div>
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
