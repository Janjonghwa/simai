import {
  FIELD_CLASS,
  FIELD_LABEL_CLASS,
  FIELD_NOTE_CLASS,
  NUMBER_INPUT_CLASS,
} from '@/features/zones/zonePanelStyle';
import { Input } from '@simai/ui/components/input';
import { useEffect, useRef, useState } from 'react';

/** 입력을 멈추면 이 시간(ms) 뒤, 또는 Enter·포커스를 잃을 때 반영한다. */
const COMMIT_DELAY_MS = 220;
/** 양의 소수만 받는다(부호·지수 없음). */
const HEIGHT_PATTERN = /^\d+(\.\d+)?$/;
/** 보이는 높이를 맞추는 배율(0.001 m)과 소수 자릿수. */
const HEIGHT_ROUND_SCALE = 1000;
const HEIGHT_DIGITS = 2;

/** 높이(m) 글자: 소수 둘째 자리까지. 작업면 카드가 같은 모양으로 보인다. */
export const formatMeters = (value: number) =>
  (Math.round(value * HEIGHT_ROUND_SCALE) / HEIGHT_ROUND_SCALE).toFixed(HEIGHT_DIGITS);

/** 작업면 상판 높이 칸. 크기·위치는 3D 손잡이로 고치고, 높이만 숫자로 고친다. */
export function SurfaceHeightField({
  value,
  bad,
  disabled,
  onCommit,
}: {
  value: number;
  bad: boolean;
  disabled: boolean;
  onCommit: (top: number) => void;
}) {
  const [raw, setRaw] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const commit = (text: string) => {
    clearTimeout(timer.current);
    if (HEIGHT_PATTERN.test(text.trim())) onCommit(Number(text));
  };

  return (
    <label className={FIELD_CLASS}>
      <span className={FIELD_LABEL_CLASS}>높이 (m)</span>
      <Input
        aria-label="높이(m)"
        inputMode="decimal"
        aria-invalid={bad || undefined}
        disabled={disabled}
        value={raw ?? formatMeters(value)}
        onFocus={() => setRaw(formatMeters(value))}
        onChange={(e) => {
          const text = e.target.value;
          setRaw(text);
          clearTimeout(timer.current);
          timer.current = setTimeout(() => commit(text), COMMIT_DELAY_MS);
        }}
        onBlur={() => {
          if (raw !== null) commit(raw);
          setRaw(null);
        }}
        onKeyDown={(e) => {
          if (e.key !== 'Enter') return;
          e.preventDefault();
          commit(e.currentTarget.value);
        }}
        className={NUMBER_INPUT_CLASS}
      />
      {bad && (
        <span role="alert" className={FIELD_NOTE_CLASS}>
          높이는 0보다 커야 합니다.
        </span>
      )}
    </label>
  );
}
