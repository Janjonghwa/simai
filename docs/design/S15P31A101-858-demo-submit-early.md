# 시연 계정은 종료 미팅을 기다리지 않고 보고서 제출 켜기 (S15P31A101-858)

-847(시연 계정 종료 미팅 가림 1.5초 고정)의 후속이다. 관련: 명세 13장(작업 관리 화면), 서버 제출 규칙(종료 미팅·AI 초안을 쓰는 중이면 409 `REPORT_NOT_READY`).
코드: `front/apps/admin/src/features/work/run/RunStage.tsx`(`EndedView`), `run/useRunActions.ts`(`useSendReport`), `run/run.ts`(`submitBlockReason`),
`features/work/useWorkReport.ts`(`SAVE_REPORT_FORM_KEY`). 시험: `src/__tests__/features/work/run/RunStage.test.tsx` 의 「시연 계정은 종료 미팅을 기다리지 않고」 묶음.

## 왜

운영 리허설에서 서버 종료 미팅(외부 LLM, 시간 제한 40 s)이 약 11초 걸렸고, 그동안 「보고서 제출」이 꺼져 있었다. 시연 계정 종이는
`demoReport` 가 미리 적어 둔 종료 미팅을 1.5초 뒤 보이므로 서버 문장을 기다릴 이유가 없다.

## 정한 규칙(A안)

1. 시연 계정은 1.5초 가림(`useDemoEndingGenerating`)이 끝나면 서버 종료 미팅(`wrap_up_status` GENERATING) 중이어도 「보고서 제출」을 켠다.
   AI 초안 작성 중(`status` GENERATING)·보고서 없음 등 다른 막는 이유는 그대로다(`submitBlockReason(…, { ignoreEnding: true })`).
2. 확인 창에서 「제출」을 눌렀을 때 서버가 종료 미팅을 쓰는 중이면 바로 보내지 않고 대기(pending, 단추·확인 창 「제출」 막힘)한다.
   `useWorkReport` 가 2초마다 다시 받아 종료 미팅이 끝난 것을 보면 그때 한 번 보낸다(409 를 받지 않는다). 기다리는 동안 「취소」하면 대기도 버린다.
3. 종료 미팅이 SUCCEEDED·FAILED·SKIPPED 어느 쪽으로 끝나도 지금 제출 규칙을 따른다: 남은 막는 이유가 없으면 보내고, 있으면(예: AI 초안 작성 중)
   보내지 않고 그 이유를 확인 창에 보인다(다시 「제출」을 누를 수 있다).
4. 기다림이 끝난 순간 진행 중인 양식 저장(`SAVE_REPORT_FORM_KEY`)이 있으면 그것도 끝난 뒤 보낸다. 시연 계정은 종료 미팅이 끝나 새 판이 오면
   종이(`useDemoReportSync`)가 미리 적어 둔 종료 미팅을 서버 양식에 저장하는데, 제출이 먼저 잠그면 제출본에 서버 문장이 남고 저장은 409 가 된다.
   같은 커밋에서 자식(종이) 효과가 먼저 저장을 시작하므로 `useIsMutating` 값에 더해 효과 안에서 `client.isMutating` 을 한 번 더 본다.
5. 시연 계정이 아니면 그대로다(서버 종료 미팅이 끝나야 켜지고, 확인하면 바로 보낸다).

## 버린 대안

- 클라이언트가 종료 미팅 중에도 바로 보내고 409 를 받으면 다시 보내기: 409 를 일부러 내는 흐름이고 다시 보내는 간격·횟수를 따로 정해야 한다.
- 대기 중 다시 받기 간격을 줄이기: 2초 주기로도 체감 지연이 작고, 다시 받기 정책(`useWorkReport`)을 바꾸면 영향이 넓다.

## 남은 위험

- 서버 종료 미팅이 끝난 뒤 제출까지 최대 다시 받기 한 주기(2초)가 더 걸린다.
- 대기 중 화면을 떠나면(날짜 이동·새로고침) 제출하지 않는다. 다시 들어와 다시 눌러야 한다.
