import { SheetProvider } from '@/features/tbm/FormFields';
import {
  AttendeeTable,
  CheckTable,
  FocusTable,
  InfoTable,
  ReviewTable,
  RiskTable,
} from '@/features/tbm/formTables';
import type { TbmForm, TbmFormValue } from '@/features/tbm/tbmForm';

// Tool Box Meeting 회의록 공식 서식 한 장(표는 formTables, 칸은 FormFields).
// 화면은 토큰 색으로 그리고, 출력할 때만 흰 종이에 검은 선으로 찍는다.

const SCREEN =
  'paper mx-auto box-border w-full max-w-[820px] bg-card px-7 pt-[26px] pb-[30px] text-[12.5px] leading-normal text-card-foreground shadow-float';
const PRINT =
  'print:m-0 print:max-w-none print:break-after-page print:bg-white print:p-0 print:text-[10.5pt] print:text-black print:shadow-none print:last:break-after-auto';

export function TbmFormSheet({
  form,
  editable,
  onSave,
  people,
}: {
  form: TbmForm;
  editable: boolean;
  onSave?: (key: string, value: TbmFormValue) => void;
  /** 참석자 서명 줄 수를 정한다. */
  people: number;
}) {
  return (
    <SheetProvider form={form} editable={editable} onSave={onSave}>
      {/* paper: 다크 모드에서도 흰 종이(화이트 모드 토큰)로 보인다(globals.css). */}
      <div className={`${SCREEN} ${PRINT}`}>
        <h3 className="mb-3.5 text-center text-xl leading-7 font-bold">Tool Box Meeting 회의록</h3>
        <InfoTable />
        <RiskTable />
        <FocusTable />
        <CheckTable />
        <ReviewTable />
        <AttendeeTable people={people} />
      </div>
    </SheetProvider>
  );
}
