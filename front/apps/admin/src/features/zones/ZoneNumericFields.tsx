import type {
  ViewerEditCommandResult,
  ViewerEditField,
  ViewerEditPreview,
} from '@/features/viewer3d';
import {
  fieldErrorText,
  formatFieldDraft,
  formatFieldNumber,
  isIntermediateNumericInput,
  NUMERIC_DEBOUNCE_MS,
  parseFiniteNumber,
  readFieldValue,
  ZONE_FIELD_SPECS,
} from '@/features/zones/zoneEditor';
import { Input } from '@simai/ui/components/input';
import { useEffect, useRef, useState } from 'react';

/** 편집 세션마다 마운트하여 입력 문자열과 타이머가 다른 구역에 남지 않게 한다. */
export function ZoneNumericFields({
  preview,
  disabled,
  updateField,
  finish,
}: {
  preview: ViewerEditPreview;
  disabled: boolean;
  updateField: (field: ViewerEditField, value: number) => ViewerEditCommandResult;
  finish: () => unknown;
}) {
  const [raw, setRaw] = useState<Partial<Record<ViewerEditField, string>>>({});
  const [error, setError] = useState<string | null>(null);
  const sessions = useRef<
    Partial<Record<ViewerEditField, { raw: string; initial: number; dirty: boolean }>>
  >({});
  const timers = useRef<Partial<Record<ViewerEditField, ReturnType<typeof setTimeout>>>>({});
  const active = useRef(true);

  const clearTimer = (field: ViewerEditField) => {
    clearTimeout(timers.current[field]);
    delete timers.current[field];
  };
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      for (const timer of Object.values(timers.current)) clearTimeout(timer);
      timers.current = {};
    };
  }, []);

  const commit = (field: ViewerEditField, value: string) => {
    clearTimer(field);
    if (!active.current) return false;
    const number = parseFiniteNumber(value);
    if (number === null) {
      setError(fieldErrorText(field, 'NOT_NUMBER'));
      return false;
    }
    const result = updateField(field, number);
    setError(result.ok ? null : fieldErrorText(field, result.reason));
    return result.ok;
  };
  const resetRaw = (field: ViewerEditField) => {
    delete sessions.current[field];
    setRaw((prev) => {
      const next = { ...prev };
      delete next[field];
      return next;
    });
  };
  const values = { ...formatFieldDraft(preview.draft), ...raw };

  return (
    <>
      <div
        role="group"
        aria-label="World 수치 입력"
        className="grid grid-cols-2 gap-1.5 rounded-[12px] bg-surface-2 p-2"
      >
        {ZONE_FIELD_SPECS.map(({ field, label, hint }) => (
          <label key={field} className="flex flex-col gap-0.5 text-[11px]">
            <span className="font-medium text-foreground" title={hint}>
              {label}
            </span>
            <Input
              aria-label={label}
              value={values[field]}
              disabled={disabled}
              onFocus={() => {
                const initial = readFieldValue(preview.draft, field);
                sessions.current[field] = {
                  raw: formatFieldNumber(initial),
                  initial,
                  dirty: false,
                };
                setRaw((prev) => ({ ...prev, [field]: formatFieldNumber(initial) }));
              }}
              onChange={(event) => {
                clearTimer(field);
                const value = event.target.value;
                const initial =
                  sessions.current[field]?.initial ?? readFieldValue(preview.draft, field);
                sessions.current[field] = { raw: value, initial, dirty: true };
                setRaw((prev) => ({ ...prev, [field]: value }));
                setError(null);
                if (!isIntermediateNumericInput(value)) {
                  timers.current[field] = setTimeout(
                    () => commit(field, value),
                    NUMERIC_DEBOUNCE_MS,
                  );
                }
              }}
              onBlur={() => {
                clearTimer(field);
                const session = sessions.current[field];
                if (session?.dirty && !isIntermediateNumericInput(session.raw))
                  commit(field, session.raw);
                resetRaw(field);
              }}
              onKeyDown={(event) => {
                if (event.key !== 'Enter' && event.key !== 'Escape') return;
                event.preventDefault();
                event.stopPropagation();
                if (event.nativeEvent.isComposing || event.repeat) return;
                if (event.key === 'Enter') {
                  const value = sessions.current[field]?.raw ?? event.currentTarget.value;
                  if (commit(field, value)) finish();
                  return;
                }
                clearTimer(field);
                const initial =
                  sessions.current[field]?.initial ?? readFieldValue(preview.draft, field);
                const result = updateField(field, initial);
                if (!result.ok) {
                  setError(fieldErrorText(field, result.reason));
                  return;
                }
                sessions.current[field] = {
                  raw: formatFieldNumber(initial),
                  initial,
                  dirty: false,
                };
                setRaw((prev) => ({ ...prev, [field]: formatFieldNumber(initial) }));
                setError(null);
              }}
              className="h-7 px-2 font-mono text-xs"
            />
          </label>
        ))}
      </div>
      {error && (
        <p
          role="alert"
          className="rounded-[8px] bg-surface-3 px-2.5 py-1.5 text-xs font-medium text-foreground"
        >
          {error}
        </p>
      )}
    </>
  );
}
