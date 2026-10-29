import type { ViewerEditBox, ViewerEditCommandResult, ViewerEditField } from '@/features/viewer3d';
import { ZoneAttributesForm } from '@/features/zones/ZoneAttributesForm';
import { writeFieldValue } from '@/features/zones/zoneEditor';
import type { ZoneForm, ZoneFormErrors } from '@/features/zones/zoneForm';
import { ZoneNumericFields } from '@/features/zones/ZoneNumericFields';
import { Button } from '@simai/ui/components/button';
import { ChevronDown, ChevronUp, Plus, SlidersHorizontal } from 'lucide-react';
import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';

/**
 * 구역을 고르지 않았을 때 늘 보이는 새 구역 양식(관제 데모와 같다). 속성과 상자(정밀 입력)를 먼저 정하고
 * 「구역 추가」로 만든다(v1). 만든 뒤에는 그 구역을 3D 에서 바로 고친다. 현장에는 발행해야 간다.
 */
export function NewZoneForm({
  form,
  box,
  errors,
  ready,
  pending,
  summary,
  onFormChange,
  onBoxChange,
  onAdd,
}: {
  form: ZoneForm;
  box: ViewerEditBox;
  errors: ZoneFormErrors;
  /** 구역 목록을 받았는지. 받기 전에는 만들지 않는다(겹침·이름을 볼 수 없다). */
  ready: boolean;
  pending: boolean;
  /** 상자 요약(구역 편집 화면과 같은 칸). */
  summary: ReactNode;
  onFormChange: (form: ZoneForm) => void;
  onBoxChange: (box: ViewerEditBox) => void;
  onAdd: () => void;
}) {
  const [numericOpen, setNumericOpen] = useState(false);
  const blocked = Object.keys(errors).length > 0;

  // 숫자 칸은 입력을 멈추면 바로 반영한다. 앞 칸이 반영된 뒤 다시 그리기 전에 다음 칸이 와도 최신 상자에 쓴다.
  const latest = useRef(box);
  useLayoutEffect(() => {
    latest.current = box;
  }, [box]);
  const updateField = (field: ViewerEditField, value: number): ViewerEditCommandResult => {
    const next = writeFieldValue(latest.current, field, value);
    if (!next) return { ok: false, reason: 'INVALID_GEOMETRY' };
    latest.current = next;
    onBoxChange(next);
    return { ok: true };
  };

  return (
    // 머리줄은 두지 않는다. 위 구역 선택 드롭다운이 「새 구역」을 보인다.
    <section aria-label="새 구역" className="flex flex-col gap-2">
      <ZoneAttributesForm
        form={form}
        errors={errors}
        onChange={onFormChange}
        readOnly={false}
        disabled={pending}
      />
      {summary}
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
        {/* 구역 편집과 같은 숫자 칸이다. 새 구역은 3D 편집 중이 아니라 Enter 로 끝낼 편집이 없다. */}
        {numericOpen && (
          <ZoneNumericFields
            preview={{ draft: box }}
            disabled={pending}
            updateField={updateField}
            finish={() => undefined}
          />
        )}
      </div>
      <Button size="sm" disabled={!ready || pending || blocked} onClick={onAdd} className="w-full">
        <Plus className="size-3.5" aria-hidden />
        {pending ? '추가 중…' : '구역 추가'}
      </Button>
    </section>
  );
}
