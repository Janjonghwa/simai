import { confirmationText } from '@/features/events/format';
import type { EventItem } from '@/features/events/types';
import { Badge } from '@simai/ui/components/badge';

// 사건 상세 머리의 꼬리표. 이벤트 카드의 처리 상태 칩(미확인·확인 시각·해소 시각)은 한 줄 카드에서 뺐다(S15P31A101-798, 글은 카드를
// 올려 보면 나오는 설명에 있다).

/**
 * 사건 상세의 확인 필요 꼬리표. 첫 이유까지 글자로 붙인다. 한 줄 카드(S15P31A101-798)는 꼬리표 없이 같은 글을 올려 보면 나오는 설명에 둔다.
 */
export function ConfirmBadge({ event }: { event: EventItem }) {
  return <Badge variant="outline">{confirmationText(event)}</Badge>;
}

/** 작업 외 진입의 이유: 발생 시각에 진행 중인 작업이 없음(NO_WORK), 작업 중이었지만 작업 구역 밖(OUTSIDE_ZONE). */
export type OutsideWorkKind = 'NO_WORK' | 'OUTSIDE_ZONE';

/** 작업 외 진입 꼬리표 문구와 올려 보면 나오는 설명(S15P31A101-731: 「작업 외 진입」 한 가지로는 뜻을 알 수 없었다). */
export const OUTSIDE_WORK_TEXT: Record<OutsideWorkKind, { label: string; title: string }> = {
  NO_WORK: {
    label: '작업 시간 외',
    title: '진행 중인 작업이 없을 때 난 이벤트입니다(작업 시작 전·종료 뒤·실행 사이).',
  },
  OUTSIDE_ZONE: {
    label: '작업 구역 밖',
    title: '작업 중이었지만 그 작업의 구역 밖에서 난 이벤트입니다.',
  },
};

/** 지연 알림 꼬리표(심각도와 함께 보여야 하는 것, 명세 7.3). 색은 지연(채도를 낮춘 주황) 몫이다. 사건 상세 머리에 둔다. */
export function DelayedBadge() {
  return (
    <Badge variant="outline" className="border-delay text-delay">
      지연 알림
    </Badge>
  );
}
