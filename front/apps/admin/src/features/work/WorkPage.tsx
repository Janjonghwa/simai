import { WorkCalendar } from '@/features/work/calendar/WorkCalendar';
import { PastDayPage } from '@/features/work/past/PastDayPage';
import { dayKind, toDateKey } from '@/features/work/workDay';
import { WorkDayPage } from '@/features/work/WorkDayPage';
import { useEffect, useState } from 'react';

// 작업 관리: 첫 화면은 월 달력이고, 날짜를 누르면 지난 날은 다시 보기, 오늘·예정인 날은 작업 흐름이다.
// 달력 ↔ 날짜 화면을 오가도 이 컴포넌트가 계속 마운트돼 있어, 마지막에 연 날짜(달력이 그 달로 돌아온다)와 알림을 여기서 든다.

/** 보고서를 보낸 뒤 달력 머리줄에 보이는 알림 글과 보이는 시간(ms). */
const SENT_NOTICE = '보고서를 보냈습니다';
const NOTICE_MS = 2_400;

export function WorkPage({
  date,
  todayKey,
  onOpenDate,
}: {
  /** 열려 있는 날짜 키(YYYY-MM-DD). 없으면 달력. */
  date?: string;
  /** 오늘 날짜 키. 안 주면 지금 시각으로 만든다(시험에서 고정할 때 넘긴다). */
  todayKey?: string;
  /** 날짜를 열라는(날짜 키) 또는 달력으로 나가라는(null) 요청. 주소를 바꾸는 일은 부르는 쪽이 한다. */
  onOpenDate: (date: string | null) => void;
}) {
  const today = todayKey ?? toDateKey(new Date());
  const [lastDate, setLastDate] = useState<string | null>(date ?? null);
  const [notice, setNotice] = useState<string | null>(null);

  // 주소로 날짜가 바뀌어도(링크·뒤로 가기) 마지막에 연 날짜가 따라간다.
  if (date && date !== lastDate) setLastDate(date);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  if (!date) {
    return (
      <WorkCalendar
        todayKey={today}
        focusDate={lastDate ?? today}
        notice={notice}
        onPick={onOpenDate}
      />
    );
  }
  // key: 지난 날 A → B 로 바로 옮겨도(링크·뒤로 가기) 고른 작업·오른쪽 탭이 남지 않게 날짜마다 새로 연다.
  if (dayKind(date, today) === 'past') return <PastDayPage key={date} date={date} />;
  return (
    <WorkDayPage
      key={date}
      date={date}
      todayKey={today}
      onBack={() => onOpenDate(null)}
      onSubmitted={() => {
        setNotice(SENT_NOTICE);
        onOpenDate(null);
      }}
    />
  );
}
