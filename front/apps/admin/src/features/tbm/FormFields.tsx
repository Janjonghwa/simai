import { FIELD_MARK } from '@/features/tbm/constants';
import type { TbmForm, TbmFormValue } from '@/features/tbm/tbmForm';
import { cn } from '@simai/ui/lib/utils';
import { Check } from 'lucide-react';
import {
  createContext,
  useContext,
  useLayoutEffect,
  useRef,
  type ReactNode,
  type SyntheticEvent,
} from 'react';

// 회의록 서식의 칸. 서식 한 벌이 값(form)과 고칠 수 있는지(editable)를 한 번 주면 칸마다 이름(name)만 넘긴다.

interface SheetState {
  form: TbmForm;
  editable: boolean;
  onSave?: (key: string, value: TbmFormValue) => void;
}

const SheetContext = createContext<SheetState>({ form: {}, editable: false });

export function SheetProvider({
  form,
  editable,
  onSave,
  children,
}: SheetState & { children: ReactNode }) {
  return (
    <SheetContext.Provider value={{ form, editable, onSave }}>{children}</SheetContext.Provider>
  );
}

const text = (value: TbmFormValue | undefined) => (value == null ? '' : String(value));

/** 고칠 수 있는 칸. 글자는 React 가 아니라 직접 넣어 입력 중인 커서와 한글 조합을 건드리지 않는다. */
function Fx({
  value,
  editable,
  onSave,
  inline,
  placeholder,
  className,
}: {
  value: string;
  editable: boolean;
  onSave: (value: string) => void;
  inline: boolean;
  placeholder?: string;
  className?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const shown = useRef<string | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && shown.current !== value && document.activeElement !== el) {
      el.innerText = value;
      shown.current = value;
    }
  });
  const keep = (e: SyntheticEvent<HTMLSpanElement>) => {
    const next = e.currentTarget.innerText.replace(/\n+$/, '');
    shown.current = next;
    if (next !== value) onSave(next);
  };
  return (
    <span
      ref={ref}
      data-ph={placeholder}
      {...FIELD_MARK}
      contentEditable={editable}
      suppressContentEditableWarning
      role={editable ? 'textbox' : undefined}
      aria-multiline={editable && !inline ? true : undefined}
      onInput={keep}
      onBlur={keep}
      className={cn(
        'wrap-anywhere whitespace-pre-wrap outline-none empty:before:text-faint empty:before:content-[attr(data-ph)] print:empty:before:text-neutral-500',
        inline
          ? 'inline-block min-w-20 border-b border-current align-bottom'
          : 'block min-h-[1.5em]',
        editable &&
          'cursor-text bg-primary/10 ring-1 ring-primary/40 ring-inset focus:bg-card focus:ring-2 focus:ring-ring',
        className,
      )}
    />
  );
}

/** 서식의 글자 칸 하나(name 은 form 의 키). */
export function TextCell({
  name,
  inline = false,
  placeholder,
  className,
}: {
  name: string;
  inline?: boolean;
  placeholder?: string;
  className?: string;
}) {
  const { form, editable, onSave } = useContext(SheetContext);
  return (
    <Fx
      value={text(form[name])}
      editable={editable}
      onSave={(value) => onSave?.(name, value)}
      inline={inline}
      placeholder={placeholder}
      className={className}
    />
  );
}

/** 서식의 예·아니오 칸 하나. 편집 중에만 눌러 바꾸고, 같은 쪽을 다시 누르면 비운다. */
export function YesNoCell({
  name,
  boxFirst = false,
  sep,
}: {
  name: string;
  boxFirst?: boolean;
  sep: string;
}) {
  const { form, editable, onSave } = useContext(SheetContext);
  const value = form[name] ?? null;
  const option = (choice: boolean, label: string) => {
    const on = value === choice;
    const box = (
      <span
        aria-hidden
        className="inline-flex size-[11px] items-center justify-center border border-current"
      >
        {on && <Check className="size-2.5" strokeWidth={3} />}
      </span>
    );
    return (
      <button
        type="button"
        disabled={!editable}
        aria-pressed={on}
        onClick={() => onSave?.(name, on ? null : choice)}
        className={cn(
          'inline-flex items-center gap-[3px] rounded-[3px] px-0.5 disabled:cursor-default',
          editable && 'hover:bg-primary/15',
        )}
      >
        {boxFirst && box}
        {label}
        {!boxFirst && box}
      </button>
    );
  };
  return (
    <span className="whitespace-nowrap">
      {option(true, '예')}
      {sep}
      {option(false, '아니오')}
    </span>
  );
}
