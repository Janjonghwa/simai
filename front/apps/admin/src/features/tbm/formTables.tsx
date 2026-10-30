import { ATTENDEES_PER_ROW, BAR_MARK, FORM_ROWS, NOTE_MARK } from '@/features/tbm/constants';
import { TextCell, YesNoCell } from '@/features/tbm/FormFields';
import { attendeeRows } from '@/features/tbm/tbmForm';
import { cn } from '@simai/ui/lib/utils';
import { Fragment } from 'react';

// Tool Box Meeting 회의록 서식의 표들. 화면은 토큰 색으로 그리고, 출력할 때만 흰 종이에 검은 선으로 찍는다.
// BAR_MARK · NOTE_MARK 는 paginate.ts 가 A4 쪽을 나눌 때 읽는 표시다(구역 제목 줄 · 안내 글).

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
const SIGN_TD = cn(TD, 'h-7');
const NOTE = 'text-[11.5px] font-normal text-muted-foreground print:text-neutral-600';
const GNOTE = cn(NOTE, 'mb-1 block');
const DOC = 'w-full table-fixed border-collapse break-keep';
/** 앞 표의 아래 선과 겹치게 붙는 표. */
const DOC_NEXT = cn(DOC, '-mt-px');

const INFO_COLS = ['w-[15%]', 'w-[35%]', 'w-[17%]', 'w-[33%]'];
const RISK_COLS = ['w-[6%]', 'w-[44%]', 'w-[6%]', 'w-[44%]'];
const FOCUS_COLS = ['w-[15%]', 'w-[10%]', 'w-[75%]'];
const CHECK_COLS = ['w-[6%]', 'w-[44%]', 'w-[18%]', 'w-[32%]'];

/** 서명 줄 한 칸 안의 사람 순번(0 ~ ATTENDEES_PER_ROW - 1). */
const SLOTS = Array.from({ length: ATTENDEES_PER_ROW }, (_, n) => n);

function Cols({ widths }: { widths: readonly string[] }) {
  return (
    <colgroup>
      {widths.map((width, n) => (
        <col key={n} className={width} />
      ))}
    </colgroup>
  );
}

/** 일시 · 작업명 · 작업내용 · 장소 · 위험성평가 여부. */
export function InfoTable() {
  return (
    <table className={DOC}>
      <Cols widths={INFO_COLS} />
      <tbody>
        <tr>
          <th className={TH}>TBM 일시</th>
          <td className={TD} colSpan={2}>
            <TextCell name="date" placeholder="20__ 년 __ 월 __ 일 __:__ ~ __:__" />
          </td>
          <td className={TD}>
            작업날짜와 동일함{' '}
            <span className="whitespace-nowrap">
              (<YesNoCell name="same" boxFirst sep=", " />)
            </span>
          </td>
        </tr>
        <tr>
          <th className={TH}>작 업 명</th>
          <td className={TD} colSpan={3}>
            <TextCell name="name" />
          </td>
        </tr>
        <tr>
          <th className={TH}>작업내용</th>
          <td className={TD} colSpan={3}>
            <TextCell name="content" />
          </td>
        </tr>
        <tr>
          <th className={TH}>TBM 장소</th>
          <td className={TD}>
            <TextCell name="place" />
          </td>
          <th className={TH}>위험성평가 실시여부</th>
          <td className={TD}>
            <YesNoCell name="riskDone" sep=" " />
          </td>
        </tr>
      </tbody>
    </table>
  );
}

/** 잠재위험요인과 대책. */
export function RiskTable() {
  return (
    <table className={DOC_NEXT}>
      <Cols widths={RISK_COLS} />
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
            <td className={TD}>
              <TextCell name={`r${n}`} />
            </td>
            <td className={NO}>{c}</td>
            <td className={TD}>
              <TextCell name={`m${n}`} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 중점위험 요인과 TBM 리더 확인. */
export function FocusTable() {
  return (
    <table className={DOC_NEXT}>
      <Cols widths={FOCUS_COLS} />
      <tbody>
        <tr>
          <th className={TH} rowSpan={2}>
            중점위험 요인
          </th>
          <th className={TH}>선정</th>
          <td className={TD}>
            <span className={GNOTE} {...NOTE_MARK}>
              ※ 잠재위험요인 ① ~ ③ 중 중요위험 1개를 선정하여 기재함
            </span>
            <TextCell name="focusRisk" />
          </td>
        </tr>
        <tr>
          <th className={TH}>대책</th>
          <td className={TD}>
            <TextCell name="focusMeasure" />
          </td>
        </tr>
        <tr>
          <th className={TH}>TBM 리더 확인</th>
          <td className={TD} colSpan={2}>
            • 소속 : <TextCell name="leadOrg" inline /> • 직책: <TextCell name="leadRole" inline />{' '}
            • 성명 <TextCell name="leadName" inline /> (서명)
          </td>
        </tr>
      </tbody>
    </table>
  );
}

/** 작업 전 안전조치 확인. */
export function CheckTable() {
  return (
    <table className={DOC_NEXT}>
      <Cols widths={CHECK_COLS} />
      <tbody>
        <tr>
          <td className={BAR} {...BAR_MARK} colSpan={4}>
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
            <td className={TD}>
              <TextCell name={`c${n}`} />
            </td>
            <td className={NO}>
              <YesNoCell name={`k${n}`} sep=", " />
            </td>
            <td className={TD}>
              <TextCell name={`f${n}`} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** 일일 안전점검 · 종료 미팅 칸과 참석자 확인 제목 줄. */
export function ReviewTable() {
  return (
    <table className={DOC_NEXT}>
      <tbody>
        <tr>
          <td className={BAR} {...BAR_MARK}>
            ■ 작업 전 일일 안전점검 시행 결과
          </td>
        </tr>
        <tr>
          <td className={AREA}>
            <span className={GNOTE} {...NOTE_MARK}>
              ※ 위험요인 중 조치가 되지 않은 사항, 작업자의 TBM내용 숙지 여부 중점체크
            </span>
            <TextCell name="daily" className="min-h-[52px]" />
          </td>
        </tr>
        <tr>
          <td className={BAR} {...BAR_MARK}>
            ■ 작업 후 종료 미팅(중점대책의 실효성)
          </td>
        </tr>
        <tr>
          <td className={AREA}>
            <TextCell name="closing" className="min-h-[52px]" />
          </td>
        </tr>
        <tr>
          <td className={BAR} {...BAR_MARK}>
            ■ 참석자 확인
            <small className={cn(NOTE, 'ml-2.5')}>
              ※ TBM에 참여하지 않은 작업자를 확인하여 미팅 참석 유도
            </small>
          </td>
        </tr>
      </tbody>
    </table>
  );
}

/** 참석자 이름 · 서명 칸. 한 줄에 ATTENDEES_PER_ROW 명씩 둔다. */
export function AttendeeTable({ people }: { people: number }) {
  return (
    <table className={DOC_NEXT}>
      <tbody>
        <tr>
          {SLOTS.map((slot) => (
            <Fragment key={slot}>
              <th className={TH}>이 름</th>
              <th className={TH}>서 명</th>
            </Fragment>
          ))}
        </tr>
        {Array.from({ length: attendeeRows(people) }, (_, row) => (
          <tr key={row}>
            {SLOTS.map((slot) => (
              <Fragment key={slot}>
                <td className={SIGN_TD}>
                  <TextCell name={`an${row * ATTENDEES_PER_ROW + slot}`} />
                </td>
                <td className={SIGN_TD}>
                  <TextCell name={`as${row * ATTENDEES_PER_ROW + slot}`} />
                </td>
              </Fragment>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
