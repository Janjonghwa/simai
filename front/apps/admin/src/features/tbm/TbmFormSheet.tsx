import { attendeeRows, FORM_ROWS, type TbmForm, type TbmFormValue } from '@/features/tbm/tbm';
import { cn } from '@simai/ui/lib/utils';
import { Check } from 'lucide-react';
import { Fragment, useLayoutEffect, useRef, type SyntheticEvent } from 'react';

// Tool Box Meeting 회의록 공식 서식. 화면은 토큰 색으로 그리고, 출력할 때만 흰 종이에 검은 선으로 찍는다.

const CIRC = ['①', '②', '③'].slice(0, FORM_ROWS);

const CELL = 'border border-muted-foreground px-[7px] py-[5px] align-middle print:border-black';
const TH = cn(
  CELL,
  'bg-surface-2 text-center font-bold [print-color-adjust:exact] print:bg-neutral-100',
);
const TD = cn(CELL, 'text-left');
const NO = cn(CELL, 'text-center');
const BAR = cn(
  CELL,
  'bg-surface-3 text-left font-bold [print-color-adjust:exact] print:bg-neutral-200',
);
const AREA = cn(TD, 'h-16 align-top');
const NOTE = 'text-[11.5px] font-normal text-muted-foreground print:text-neutral-600';
const GNOTE = cn(NOTE, 'mb-1 block');
const DOC = 'w-full table-fixed border-collapse break-keep';

const text = (value: TbmFormValue) => (value == null ? '' : String(value));

/** 고칠 수 있는 칸. 글자는 React 가 아니라 직접 넣어 입력 중인 커서와 한글 조합을 건드리지 않는다. */
function Fx({
  value,
  editable,
  onSave,
  inline = false,
  placeholder,
  className,
}: {
  value: string;
  editable: boolean;
  onSave: (value: string) => void;
  inline?: boolean;
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

/** 예·아니오 칸. 편집 중에만 눌러 바꾸고, 같은 쪽을 다시 누르면 비운다. */
function YesNo({
  value,
  editable,
  onSave,
  boxFirst = false,
  sep,
}: {
  value: TbmFormValue;
  editable: boolean;
  onSave: (value: boolean | null) => void;
  boxFirst?: boolean;
  sep: string;
}) {
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
        onClick={() => onSave(on ? null : choice)}
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

export function TbmFormSheet({
  form,
  editable,
  onSave,
  people,
  receiving = false,
}: {
  form: TbmForm;
  editable: boolean;
  onSave?: (key: string, value: TbmFormValue) => void;
  /** 참석자 서명 줄 수를 정한다. */
  people: number;
  /** 받는 중에는 빈 양식을 흐리게 보인다. */
  receiving?: boolean;
}) {
  const save = (key: string) => (value: TbmFormValue) => onSave?.(key, value);
  const fx = (
    key: string,
    opts?: { inline?: boolean; placeholder?: string; className?: string },
  ) => <Fx value={text(form[key] ?? null)} editable={editable} onSave={save(key)} {...opts} />;
  const yn = (key: string, boxFirst: boolean, sep: string) => (
    <YesNo
      value={form[key] ?? null}
      editable={editable}
      onSave={save(key)}
      boxFirst={boxFirst}
      sep={sep}
    />
  );
  const cols = (...widths: string[]) => (
    <colgroup>
      {widths.map((w, n) => (
        <col key={n} className={w} />
      ))}
    </colgroup>
  );
  const rows = attendeeRows(people);
  return (
    <div
      className={cn(
        // paper: 다크 모드에서도 흰 종이(화이트 모드 토큰)로 보인다(globals.css).
        'paper mx-auto box-border w-full max-w-[820px] bg-card px-7 pt-[26px] pb-[30px] text-[12.5px] leading-normal text-card-foreground shadow-float',
        'print:m-0 print:max-w-none print:break-after-page print:bg-white print:p-0 print:text-[10.5pt] print:text-black print:shadow-none print:last:break-after-auto',
        receiving && '[&_td]:text-faint',
      )}
    >
      <h3 className="mb-3.5 text-center text-xl leading-7 font-bold">Tool Box Meeting 회의록</h3>
      <table className={DOC}>
        {cols('w-[15%]', 'w-[35%]', 'w-[17%]', 'w-[33%]')}
        <tbody>
          <tr>
            <th className={TH}>TBM 일시</th>
            <td className={TD} colSpan={2}>
              {fx('date', { placeholder: '20__ 년 __ 월 __ 일 __:__ ~ __:__' })}
            </td>
            <td className={TD}>
              작업날짜와 동일함{' '}
              <span className="whitespace-nowrap">({yn('same', true, ', ')})</span>
            </td>
          </tr>
          <tr>
            <th className={TH}>작 업 명</th>
            <td className={TD} colSpan={3}>
              {fx('name')}
            </td>
          </tr>
          <tr>
            <th className={TH}>작업내용</th>
            <td className={TD} colSpan={3}>
              {fx('content')}
            </td>
          </tr>
          <tr>
            <th className={TH}>TBM 장소</th>
            <td className={TD}>{fx('place')}</td>
            <th className={TH}>위험성평가 실시여부</th>
            <td className={TD}>{yn('riskDone', false, ' ')}</td>
          </tr>
        </tbody>
      </table>
      <table className={cn(DOC, '-mt-px')}>
        {cols('w-[6%]', 'w-[44%]', 'w-[6%]', 'w-[44%]')}
        <tbody>
          <tr>
            <th className={TH} colSpan={2}>
              잠재위험요인
            </th>
            <th className={TH} colSpan={2}>
              대책 <small className={NOTE}>(※ 제거 → 대체 → 통제 순서 고려)</small>
            </th>
          </tr>
          {CIRC.map((c, n) => (
            <tr key={c}>
              <td className={NO}>{c}</td>
              <td className={TD}>{fx(`r${n}`)}</td>
              <td className={NO}>{c}</td>
              <td className={TD}>{fx(`m${n}`)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className={cn(DOC, '-mt-px')}>
        {cols('w-[15%]', 'w-[10%]', 'w-[75%]')}
        <tbody>
          <tr>
            <th className={TH} rowSpan={2}>
              중점위험 요인
            </th>
            <th className={TH}>선정</th>
            <td className={TD}>
              <span className={GNOTE}>※ 잠재위험요인 ① ~ ③ 중 중요위험 1개를 선정하여 기재함</span>
              {fx('focusRisk')}
            </td>
          </tr>
          <tr>
            <th className={TH}>대책</th>
            <td className={TD}>{fx('focusMeasure')}</td>
          </tr>
          <tr>
            <th className={TH}>TBM 리더 확인</th>
            <td className={TD} colSpan={2}>
              • 소속 : {fx('leadOrg', { inline: true })} • 직책: {fx('leadRole', { inline: true })}{' '}
              • 성명 {fx('leadName', { inline: true })} (서명)
            </td>
          </tr>
        </tbody>
      </table>
      <table className={cn(DOC, '-mt-px')}>
        {cols('w-[6%]', 'w-[44%]', 'w-[18%]', 'w-[32%]')}
        <tbody>
          <tr>
            <td className={BAR} colSpan={4}>
              ■ 작업 전 안전조치 확인
              <small className={cn(NOTE, 'ml-2.5')}>
                ※ 위 잠재위험요인(중점위험 포함) 안전조치 여부 재확인
              </small>
            </td>
          </tr>
          <tr>
            <th className={TH} colSpan={2}>
              잠재위험요소(중점위험 포함)
            </th>
            <th className={TH}>조 치 여 부</th>
            <th className={TH}>{"'아니오'인 경우 조치 내용"}</th>
          </tr>
          {CIRC.map((c, n) => (
            <tr key={c}>
              <td className={NO}>{c}</td>
              <td className={TD}>{fx(`c${n}`)}</td>
              <td className={NO}>{yn(`k${n}`, false, ', ')}</td>
              <td className={TD}>{fx(`f${n}`)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className={cn(DOC, '-mt-px')}>
        <tbody>
          <tr>
            <td className={BAR}>■ 작업 전 일일 안전점검 시행 결과</td>
          </tr>
          <tr>
            <td className={AREA}>
              <span className={GNOTE}>
                ※ 위험요인 중 조치가 되지 않은 사항, 작업자의 TBM내용 숙지 여부 중점체크
              </span>
              {fx('daily', { className: 'min-h-[52px]' })}
            </td>
          </tr>
          <tr>
            <td className={BAR}>■ 작업 후 종료 미팅(중점대책의 실효성)</td>
          </tr>
          <tr>
            <td className={AREA}>{fx('closing', { className: 'min-h-[52px]' })}</td>
          </tr>
          <tr>
            <td className={BAR}>
              ■ 참석자 확인
              <small className={cn(NOTE, 'ml-2.5')}>
                ※ TBM에 참여하지 않은 작업자를 확인하여 미팅 참석 유도
              </small>
            </td>
          </tr>
        </tbody>
      </table>
      <table className={cn(DOC, '-mt-px')}>
        <tbody>
          <tr>
            {[0, 1, 2].map((n) => (
              <Fragment key={n}>
                <th className={TH}>이 름</th>
                <th className={TH}>서 명</th>
              </Fragment>
            ))}
          </tr>
          {Array.from({ length: rows }, (_, r) => (
            <tr key={r}>
              {[0, 1, 2].map((c) => (
                <Fragment key={c}>
                  <td className={cn(TD, 'h-7')}>{fx(`an${r * 3 + c}`)}</td>
                  <td className={cn(TD, 'h-7')}>{fx(`as${r * 3 + c}`)}</td>
                </Fragment>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
