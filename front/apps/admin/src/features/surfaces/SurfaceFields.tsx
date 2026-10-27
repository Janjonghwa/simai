import { fieldValue, toDegrees, toRadians, type SurfaceField } from '@/features/surfaces/surfaces';
import type { SurfaceGeometry } from '@/features/surfaces/types';
import { Input } from '@simai/ui/components/input';
import { useId, useRef, useState, type ReactNode } from 'react';

/** 숫자 칸. 길이는 m, 회전은 화면에서만 도(°)로 보이고 받는다(명세 4.2). */
const FIELDS: readonly { field: SurfaceField; label: string }[] = [
  { field: 'centerX', label: '중심 X (m)' },
  { field: 'centerY', label: '중심 Y (m)' },
  { field: 'width', label: '폭 (m)' },
  { field: 'depth', label: '깊이 (m)' },
  { field: 'topHeight', label: '상판 높이 (m)' },
  { field: 'yaw', label: '회전 (°)' },
];

const shown = (geometry: SurfaceGeometry, field: SurfaceField) =>
  field === 'yaw' ? toDegrees(geometry.yawRad).toFixed(1) : fieldValue(geometry, field).toFixed(2);

/** 입력한 글자를 칸 값(m·rad)으로 읽는다. 숫자가 아니면 null. 회전은 -180°~180° 로 접는다. */
function parse(field: SurfaceField, text: string): number | null {
  const value = Number(text.trim());
  if (text.trim() === '' || !Number.isFinite(value)) return null;
  return field === 'yaw' ? toRadians(value) : value;
}

/**
 * 칸 이름과 오류. 오류는 label 밖에 두고 입력 칸에 aria-describedby 로만 잇는다(칸 이름에 오류 문구가 섞여 두 번 읽히지
 * 않게).
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
  children: (ids: { id: string; describedBy: string | undefined }) => ReactNode;
}) {
  const id = useId();
  const errorId = useId();
  return (
    <div className={`flex min-w-0 flex-col gap-1 ${className ?? ''}`}>
      <label htmlFor={id} className="text-[11px] text-muted-foreground">
        {label}
      </label>
      {children({ id, describedBy: error ? errorId : undefined })}
      {error && (
        <span id={errorId} role="alert" className="text-[11px] font-medium text-foreground">
          {error}
        </span>
      )}
    </div>
  );
}

/**
 * 작업면 이름·형상 칸. 칸마다 입력 중 글자를 따로 들고, 고칠 때마다 올바른 값이면 바로 반영한다(저장할 때 밀린 입력이 없다).
 * 반영할 수 없는 값은 칸을 떠나거나 Enter 를 누를 때 알린다. Escape 는 그 칸에 들어왔을 때 값으로 되돌린다.
 * 작업면을 바꾸면 key 로 새로 뜬다.
 */
export function SurfaceFields({
  name,
  geometry,
  readOnly,
  disabled,
  onName,
  onField,
}: {
  name: string;
  geometry: SurfaceGeometry;
  readOnly: boolean;
  disabled: boolean;
  onName: (value: string) => void;
  onField: (field: SurfaceField, value: number) => string | null;
}) {
  const [texts, setTexts] = useState<Partial<Record<SurfaceField, string>>>({});
  const [errors, setErrors] = useState<Partial<Record<SurfaceField, string>>>({});
  // 칸에 들어왔을 때 값(Escape 로 되돌릴 값).
  const initial = useRef<Partial<Record<SurfaceField, number>>>({});

  if (readOnly) {
    return (
      <dl
        aria-label="작업면 값"
        className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-[12px] bg-surface-2 px-2.5 py-2 text-xs"
      >
        <dt className="text-muted-foreground">이름</dt>
        <dd>{name || '—'}</dd>
        {FIELDS.map(({ field, label }) => (
          <div key={field} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="font-mono">{shown(geometry, field)}</dd>
          </div>
        ))}
      </dl>
    );
  }

  const setError = (field: SurfaceField, error: string | null) =>
    setErrors((prev) => {
      if (!error && !(field in prev)) return prev;
      const next = { ...prev };
      if (error) next[field] = error;
      else delete next[field];
      return next;
    });
  const forgetText = (field: SurfaceField) =>
    setTexts((prev) => {
      const next = { ...prev };
      delete next[field];
      return next;
    });
  /** 칸을 떠나거나 Enter: 반영할 수 없으면 알리고 입력은 둔다. 반영됐으면 초안 값을 다시 보인다. */
  const settle = (field: SurfaceField) => {
    const text = texts[field];
    if (text === undefined) return;
    const value = parse(field, text);
    const error = value === null ? '숫자를 입력하세요.' : onField(field, value);
    setError(field, error);
    if (!error) forgetText(field);
  };

  return (
    <div className="flex flex-col gap-2 rounded-[12px] bg-surface-2 p-2">
      <Field label="이름">
        {({ id, describedBy }) => (
          <Input
            id={id}
            value={name}
            disabled={disabled}
            maxLength={100}
            aria-describedby={describedBy}
            onChange={(event) => onName(event.target.value)}
            className="h-7 px-2 text-xs"
          />
        )}
      </Field>
      <div role="group" aria-label="작업면 형상" className="grid grid-cols-2 gap-1.5">
        {FIELDS.map(({ field, label }) => (
          <Field key={field} label={label} error={errors[field]}>
            {({ id, describedBy }) => (
              <Input
                id={id}
                inputMode="decimal"
                value={texts[field] ?? shown(geometry, field)}
                disabled={disabled}
                aria-describedby={describedBy}
                onFocus={() => {
                  initial.current[field] = fieldValue(geometry, field);
                }}
                onChange={(event) => {
                  const text = event.target.value;
                  setTexts((prev) => ({ ...prev, [field]: text }));
                  const value = parse(field, text);
                  // 쓰는 중(예: "0.")에 막히는 값은 칸을 떠날 때 알린다. 반영되면 알림을 지운다.
                  if (value !== null && onField(field, value) === null) setError(field, null);
                }}
                onBlur={() => settle(field)}
                onKeyDown={(event) => {
                  if (event.key !== 'Enter' && event.key !== 'Escape') return;
                  if (event.nativeEvent.isComposing) return;
                  event.preventDefault();
                  event.stopPropagation();
                  if (event.key === 'Enter') {
                    settle(field);
                    return;
                  }
                  const before = initial.current[field];
                  if (texts[field] !== undefined && before !== undefined) onField(field, before);
                  forgetText(field);
                  setError(field, null);
                }}
                className="h-7 px-2 font-mono text-xs"
              />
            )}
          </Field>
        ))}
      </div>
    </div>
  );
}
