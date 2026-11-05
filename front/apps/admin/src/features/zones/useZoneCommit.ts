import type { WorkSurface } from '@/features/surfaces/types';
import { commitErrorText } from '@/features/zones/commitErrors';
import type { Zone, ZoneCommitRequest } from '@/features/zones/types';
import { useArmedDelete } from '@/features/zones/useArmedDelete';
import { useCommitNotice } from '@/features/zones/useCommitNotice';
import { publishErrorText } from '@/features/zones/useConfigApply';
import { useExclusiveRun } from '@/features/zones/useExclusiveRun';
import { archiveErrorText, useSurfaceArchive } from '@/features/zones/useSurfaceArchive';
import { useZoneCommitMutation, type CommitDone } from '@/features/zones/useZoneCommitMutation';
import type { ZoneTarget } from '@/features/zones/useZoneTarget';
import { archiveRequestOf, createRequestOf, saveRequestOf } from '@/features/zones/zoneCommit';
import { NEW_KEY, type TargetDraft } from '@/features/zones/zoneEditModel';
import type { ZoneForm, ZoneShape } from '@/features/zones/zoneForm';
import { removeZoneDraft } from '@/store/zoneEdit.store';

export interface ZoneCommitInput {
  zone: Zone | null;
  /** 저장할 구역 모양. 기존 구역을 고치지 않았으면 null(받은 모양 그대로)이고, 새 구역은 늘 있다. */
  shape: ZoneShape | null;
  form: ZoneForm;
  draft: TargetDraft | undefined;
  linked: WorkSurface | null;
  /** 3D 에서 고른, 어느 구역에도 이어지지 않은 작업면. */
  orphan: WorkSurface | null;
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
 * 구역 편집의 저장·추가·삭제·다시 보내기와 구역 없는 작업면 삭제. 요청 본문은 zoneCommit 이 만들고 보내기는
 * useZoneCommitMutation 이 맡는다. 이 훅은 한 번에 하나만 보내며 결과를 초안·알림에 반영한다: 저장하면 그 구역의 초안을
 * 통째로 지우고, 실패하면 서버에 아무것도 저장되지 않았으니 초안을 그대로 남겨 다시 저장할 수 있게 한다.
 */
export function useZoneCommit(input: ZoneCommitInput) {
  const { zone, shape, form, draft, linked, orphan, target } = input;
  const commit = useZoneCommitMutation();
  const archive = useSurfaceArchive();
  const { busy, run } = useExclusiveRun();
  const { armedKey, arm, disarm } = useArmedDelete();
  // 결과를 화면에 반영한다: 알림·발행 실패 표시. 초안은 호출한 쪽이 정리한다.
  const {
    notice,
    publishFailed,
    show,
    publishAndShow: showPublish,
    clearNotice,
  } = useCommitNotice();

  /**
   * 요청 하나를 보낸다. 저장되면 알림을 정리하고 onSaved 를 부른다(묶음만 만들지 못했어도 저장은 된 것이다). 실패하면 알림만
   * 두고 초안은 건드리지 않는다. 이미 보내는 중이면 아무것도 하지 않는다.
   */
  const send = async (request: ZoneCommitRequest, onSaved: (done: CommitDone) => void) => {
    let done: CommitDone | undefined;
    try {
      done = await run(() => commit.mutateAsync({ request, zone, linked }));
    } catch (error) {
      show(commitErrorText(error), false);
      return;
    }
    if (!done) return;
    const { publishError } = done;
    show(publishError ? publishErrorText(publishError) : null, publishError !== null);
    onSaved(done);
  };

  const save = async () => {
    if (!zone || !input.saveReady || busy) return;
    const request = saveRequestOf({
      zone,
      form,
      shape,
      zoneChanged: input.zoneDirty,
      linked,
      surface: draft?.surface,
      surfaceChanged: input.surfaceDirty,
    });
    if (!request) return;
    // 저장한 값이 새 기준이라 그 구역의 초안과 되돌리기 기록을 모두 버린다.
    await send(request, () => removeZoneDraft(zone.zoneId));
  };

  const add = async () => {
    if (zone || !shape || !input.addReady || busy) return;
    const request = createRequestOf({ form, shape, surface: draft?.surface });
    await send(request, ({ result }) => {
      // 만든 구역을 고르고 새 구역 양식은 처음 값으로 돌린다.
      removeZoneDraft(NEW_KEY);
      target.openZone(result.zone_id);
    });
  };

  // 삭제: 처음 누르면 확인 단계(ARM_MS), 한 번 더 누르면 보관한다(이은 작업면도 서버가 함께 보관한다).
  const remove = async () => {
    if (!zone || busy) return;
    if (armedKey !== zone.zoneId) {
      arm(zone.zoneId);
      return;
    }
    disarm();
    await send(archiveRequestOf(zone.zoneId), () => {
      removeZoneDraft(zone.zoneId);
      target.openNew();
    });
  };

  const publishAndShow = () => showPublish(input.publish);

  // 어느 구역에도 이어지지 않은 작업면 삭제: 처음 누르면 확인 단계, 한 번 더 누르면 보관하고(DELETE /api/static-objects/{id})
  // 선택을 풀고 구성 묶음을 발행한다. 보관만으로는 다음 묶음에서 빠질 뿐이라 발행해야 현장에서도 사라진다.
  const removeOrphan = async () => {
    if (!orphan || busy) return;
    if (armedKey !== orphan.objectId) {
      arm(orphan.objectId);
      return;
    }
    disarm();
    await run(async () => {
      try {
        await archive.mutateAsync(orphan.objectId);
      } catch (error) {
        show(archiveErrorText(error), false);
        return;
      }
      target.openNew();
      await publishAndShow();
    });
  };

  // 저장·삭제는 했지만 묶음을 만들지 못한 구성을 다시 보낸다.
  const republish = () => run(publishAndShow);

  return {
    busy,
    notice,
    armed: zone !== null && armedKey === zone.zoneId,
    orphanArmed: orphan !== null && armedKey === orphan.objectId,
    canSave: input.saveReady && !busy,
    canAdd: input.addReady && !busy,
    // 저장했지만 현장 판정으로 보내지 못한 경우: 방금 발행이 실패했거나, 마지막 묶음에 실리지 않은 저장이 남아 있다.
    showRetry: !busy && (publishFailed || input.pendingCount > 0),
    disarm,
    clearNotice,
    save,
    add,
    remove,
    removeOrphan,
    republish,
  };
}
