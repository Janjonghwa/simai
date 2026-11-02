import type { WorkSurface } from '@/features/surfaces/types';
import { surfaceSaveErrorText, useSurfaceSave } from '@/features/surfaces/useSurfaceSave';
import type { ViewerEditBox } from '@/features/viewer3d';
import type { Zone } from '@/features/zones/types';
import { useArmedDelete } from '@/features/zones/useArmedDelete';
import { publishErrorText } from '@/features/zones/useConfigApply';
import { saveErrorText, useZoneSave } from '@/features/zones/useZoneSave';
import type { ZoneTarget } from '@/features/zones/useZoneTarget';
import {
  commitArchive,
  commitCreate,
  commitPublish,
  commitSave,
  type CommitOps,
  type CommitOutcome,
  type CommitStep,
} from '@/features/zones/zoneCommit';
import {
  editedBox,
  NEW_KEY,
  withoutSaved,
  withSurface,
  type TargetDraft,
} from '@/features/zones/zoneEditModel';
import { newZoneBodyOf, type ZoneForm } from '@/features/zones/zoneForm';
import { removeZoneDraft, resetZoneHistory, updateZoneDraft } from '@/store/zoneEdit.store';
import { useRef, useState } from 'react';

/** 저장 단계별 실패 문구. */
const ERROR_TEXT: Record<CommitStep, (error: unknown) => string> = {
  zone: saveErrorText,
  surface: surfaceSaveErrorText,
  publish: publishErrorText,
};

/** 결과를 화면 알림 문구로 바꾼다. 끝까지 갔으면 null. */
const noticeOf = ({ failed }: CommitOutcome): string | null =>
  failed ? ERROR_TEXT[failed.step](failed.error) : null;

/**
 * 한 번에 하나만 보낸다. isPending 은 다음 턴에 바뀌어 같은 턴에 두 번 눌리면 두 번 보내므로, 보내는 중이면 여기서 바로
 * 막는다(막힌 호출은 undefined).
 */
function useExclusiveRun() {
  const sending = useRef(false);
  const [busy, setBusy] = useState(false);
  const run = async <T>(job: () => Promise<T>): Promise<T | undefined> => {
    if (sending.current) return undefined;
    sending.current = true;
    setBusy(true);
    try {
      return await job();
    } finally {
      sending.current = false;
      setBusy(false);
    }
  };
  return { busy, run };
}

/** 구역 요청(구역·작업면·발행)을 한 묶음으로 보낸다. */
function useCommitOps(publish: () => Promise<unknown>): CommitOps {
  const zoneSave = useZoneSave();
  const surfaceSave = useSurfaceSave();
  return {
    saveZone: (input) => zoneSave.save.mutateAsync(input),
    createZone: (body) => zoneSave.create.mutateAsync(body),
    archiveZone: (id) => zoneSave.archive.mutateAsync(id),
    saveSurface: (input) => surfaceSave.save.mutateAsync(input),
    createSurface: (body) => surfaceSave.create.mutateAsync(body),
    archiveSurface: (id) => surfaceSave.archive.mutateAsync(id),
    publish,
  };
}

export interface ZoneCommitInput {
  canEdit: boolean;
  zone: Zone | null;
  zoneBox: ViewerEditBox;
  form: ZoneForm;
  draft: TargetDraft | undefined;
  linked: WorkSurface | null;
  zoneDirty: boolean;
  surfaceDirty: boolean;
  /** 저장·추가 조건(보내는 중이 아니라는 것만 뺀 나머지). */
  saveReady: boolean;
  addReady: boolean;
  /** 저장했지만 마지막 묶음에 실리지 않은 구역·작업면 수. */
  pendingCount: number;
  publish: () => Promise<unknown>;
  target: ZoneTarget;
}

/**
 * 구역 편집의 저장·추가·삭제·다시 보내기. 요청 순서는 zoneCommit 이 정하고, 이 훅은 한 번에 하나만 보내며 결과를 초안·알림에
 * 반영한다: 저장한 부분만 초안에서 빼고, 실패한 부분은 남겨 다시 저장할 수 있게 한다.
 */
export function useZoneCommit(input: ZoneCommitInput) {
  const { canEdit, zone, zoneBox, form, draft, linked, target } = input;
  const ops = useCommitOps(input.publish);
  const { busy, run } = useExclusiveRun();
  const { armedKey, arm, disarm } = useArmedDelete();
  const [notice, setNotice] = useState<string | null>(null);
  // 마지막 발행이 실패했는지(원인은 notice 에도 문구로 남는다). 다시 보내 성공하면 지운다.
  const [publishFailed, setPublishFailed] = useState(false);

  /** 결과를 화면에 반영한다: 알림·발행 실패 표시. 어디까지 저장했는지는 호출한 쪽이 초안에 반영한다. */
  const report = (outcome: CommitOutcome) => {
    setPublishFailed(outcome.failed?.step === 'publish');
    setNotice(noticeOf(outcome));
  };

  const save = async () => {
    if (!zone || !input.saveReady || busy) return;
    const at = zone.zoneId;
    const outcome = await run(() =>
      commitSave(ops, {
        zone,
        form,
        box: editedBox(zone, draft),
        zoneChanged: input.zoneDirty,
        linked,
        surface: draft?.surface,
        surfaceChanged: input.surfaceDirty,
      }),
    );
    if (!outcome) return;
    updateZoneDraft(at, (prev) => withoutSaved(prev, outcome.saved));
    // 저장한 값이 새 기준이라 그 전으로는 되돌리지 않는다(아무것도 저장하지 못했으면 기록을 둔다).
    if (outcome.saved.zone || outcome.saved.surface) resetZoneHistory(at);
    if (outcome.saved.surface) target.setSurfaceMode(false);
    report(outcome);
  };

  const add = async () => {
    if (zone || !input.addReady || busy) return;
    const outcome = await run(() =>
      commitCreate(ops, { body: newZoneBodyOf(form, zoneBox), surface: draft?.surface }),
    );
    if (!outcome) return;
    const created = outcome.zone;
    if (created) {
      // 만든 구역을 고르고 새 구역 양식은 처음 값으로 돌린다. 작업면이 저장되지 못했으면 그 초안은 새 구역으로 옮긴다.
      removeZoneDraft(NEW_KEY);
      const unsaved = outcome.saved.surface ? undefined : draft?.surface;
      if (unsaved) updateZoneDraft(created.zone_id, () => withSurface(undefined, unsaved));
      target.setSurfaceMode(false);
      target.openZone(created.zone_id);
    }
    report(outcome);
  };

  // 삭제: 처음 누르면 확인 단계(ARM_MS), 한 번 더 누르면 보관한다(이은 작업면도 함께).
  const remove = async () => {
    if (!zone || !canEdit || busy) return;
    if (armedKey !== zone.zoneId) {
      arm(zone.zoneId);
      return;
    }
    disarm();
    const at = zone.zoneId;
    const outcome = await run(() => commitArchive(ops, { zoneId: at, linked }));
    if (!outcome) return;
    if (outcome.saved.zone) {
      removeZoneDraft(at);
      target.setSurfaceMode(false);
      target.openNew();
    }
    report(outcome);
  };

  const republish = async () => {
    const outcome = await run(() => commitPublish(ops));
    if (outcome) report(outcome);
  };

  return {
    busy,
    notice,
    armed: zone !== null && armedKey === zone.zoneId,
    canSave: input.saveReady && !busy,
    canAdd: input.addReady && !busy,
    // 저장했지만 현장 판정으로 보내지 못한 경우: 방금 발행이 실패했거나, 마지막 묶음에 실리지 않은 저장이 남아 있다.
    showRetry: canEdit && !busy && (publishFailed || input.pendingCount > 0),
    disarm,
    clearNotice: () => setNotice(null),
    save,
    add,
    remove,
    republish,
  };
}
