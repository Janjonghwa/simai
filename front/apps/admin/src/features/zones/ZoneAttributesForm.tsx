import { JUDGE_TYPES, SEVERITIES, ZONE_TYPES } from '@/features/zones/types';
import {
  JUDGE_TYPE_LABEL,
  NAME_MAX,
  ZONE_TYPE_LABEL,
  type ZoneForm,
  type ZoneFormErrors,
} from '@/features/zones/zoneForm';
import { Input } from '@simai/ui/components/input';
import { useId, type ReactNode } from 'react';

const SELECT_CLASS =
  'h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2 text-xs outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50 dark:bg-input/30';

/**
 * 칸 이름과 오류. 오류는 입력 칸에 aria-describedby 로 잇는다. aria-invalid 는 입력 칸을 빨갛게 칠해
 * 위험 등급 색과 섞이므로 쓰지 않는다.
 */
function Field({
  label,
  error,
  className,
  children,
}: {
  label: string;
  error?: string;
  className?: string;
  children: (describedBy: string | undefined) => ReactNode;
}) {
  const errorId = useId();
  return (
    <label className={`flex min-w-0 flex-col gap-1 ${className ?? ''}`}>
      <span className="text-[11px] text-muted-foreground">{label}</span>
      {children(error ? errorId : undefined)}
      {error && (
        <span id={errorId} role="alert" className="text-[11px] font-medium text-foreground">
          {error}
        </span>
      )}
    </label>
  );
}

const zoneTypeText = (value: string) => ZONE_TYPE_LABEL[value] ?? value;

/** 구역 속성(명세 FR-15). 관리자만 고친다. 다른 역할은 값만 본다. 검사는 화면(ZonesPage)이 한 번 해 넘긴다. */
export function ZoneAttributesForm({
  form,
  errors,
  onChange,
  readOnly,
  disabled = false,
}: {
  form: ZoneForm;
  errors: ZoneFormErrors;
  onChange: (form: ZoneForm) => void;
  readOnly: boolean;
  disabled?: boolean;
}) {
  if (readOnly) {
    return (
      <dl
        aria-label="구역 속성"
        className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-[12px] bg-surface-2 px-2.5 py-2 text-xs"
      >
        <dt className="text-muted-foreground">판정 유형</dt>
        <dd>{JUDGE_TYPE_LABEL[form.judgeType]}</dd>
        <dt className="text-muted-foreground">유형</dt>
        <dd>{zoneTypeText(form.zoneType)}</dd>
        <dt className="text-muted-foreground">심각도</dt>
        <dd className="font-mono">{form.severity}</dd>
      </dl>
    );
  }

  const set = <K extends keyof ZoneForm>(key: K, value: ZoneForm[K]) =>
    onChange({ ...form, [key]: value });
  // 받은 유형이 입력 목록에 없으면(다른 곳에서 만든 구역) 그 값도 고를 수 있게 둔다. 비었으면(새 구역) 고르게 한다.
  const zoneTypes: string[] =
    form.zoneType !== '' && !(ZONE_TYPES as readonly string[]).includes(form.zoneType)
      ? [...ZONE_TYPES, form.zoneType]
      : [...ZONE_TYPES];

  return (
    <fieldset
      aria-label="구역 속성"
      disabled={disabled}
      className="grid grid-cols-2 gap-2 rounded-[12px] bg-surface-2 px-2.5 py-2"
    >
      <Field label="이름" error={errors.name} className="col-span-2">
        {(describedBy) => (
          <Input
            aria-label="이름"
            aria-describedby={describedBy}
            value={form.name}
            maxLength={NAME_MAX + 10}
            onChange={(e) => set('name', e.target.value)}
            className="h-8 text-xs"
          />
        )}
      </Field>
      <Field label="판정 유형" error={errors.judgeType} className="col-span-2">
        {(describedBy) => (
          <select
            aria-label="판정 유형"
            aria-describedby={describedBy}
            value={form.judgeType}
            onChange={(e) => set('judgeType', e.target.value as ZoneForm['judgeType'])}
            className={SELECT_CLASS}
          >
            {JUDGE_TYPES.map((type) => (
              <option key={type} value={type}>
                {JUDGE_TYPE_LABEL[type]}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="심각도">
        {() => (
          <select
            aria-label="심각도"
            value={form.severity}
            onChange={(e) => set('severity', e.target.value as ZoneForm['severity'])}
            className={SELECT_CLASS}
          >
            {SEVERITIES.map((severity) => (
              <option key={severity} value={severity}>
                {severity}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="유형" error={errors.zoneType}>
        {(describedBy) => (
          <select
            aria-label="유형"
            aria-describedby={describedBy}
            value={form.zoneType}
            onChange={(e) => set('zoneType', e.target.value)}
            className={SELECT_CLASS}
          >
            {form.zoneType === '' && (
              <option value="" disabled>
                선택
              </option>
            )}
            {zoneTypes.map((type) => (
              <option key={type} value={type}>
                {zoneTypeText(type)}
              </option>
            ))}
          </select>
        )}
      </Field>
    </fieldset>
  );
}
