import type { ZoneEditing } from '@/features/zones/useZoneEditing';
import {
  ARMED_DELETE_CLASS,
  BUTTON_CLASS,
  DANGER_BUTTON_CLASS,
  PRIMARY_BUTTON_CLASS,
} from '@/features/zones/zonePanelStyle';
import { Button } from '@simai/ui/components/button';

/** 하단 버튼: 기존 구역은 저장·되돌리기·삭제(두 번 눌러 확인), 새 구역은 추가. */
export function ZoneActions({ edit }: { edit: ZoneEditing }) {
  if (!edit.zone) {
    return (
      <Button
        disabled={!edit.canAdd}
        onClick={() => void edit.add()}
        className={`${PRIMARY_BUTTON_CLASS} w-full`}
      >
        구역 추가
      </Button>
    );
  }
  const deleteLabel = edit.armed ? '삭제 확인' : '삭제';
  return (
    <div className="grid grid-cols-[1.4fr_1fr_1fr] gap-1.5">
      <Button
        disabled={!edit.canSave}
        onClick={() => void edit.save()}
        className={PRIMARY_BUTTON_CLASS}
      >
        저장
      </Button>
      <Button
        variant="secondary"
        disabled={!edit.dirty || edit.busy}
        onClick={edit.restore}
        className={BUTTON_CLASS}
      >
        되돌리기
      </Button>
      <Button
        variant="secondary"
        disabled={edit.busy}
        aria-label={deleteLabel}
        onClick={() => void edit.remove()}
        className={edit.armed ? ARMED_DELETE_CLASS : DANGER_BUTTON_CLASS}
      >
        {deleteLabel}
      </Button>
    </div>
  );
}
