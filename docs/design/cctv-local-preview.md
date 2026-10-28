# 현장 노트북 CCTV 로컬 미리보기 (S15P31A101-310)

관제 앱 CCTV 화면(`/cctv`)에 카메라 4대 실시간 영상을 보인다. 노트북 영상 중계(`gateway/relay`)가 이미 받고 있는 H.264 를
디코딩·재인코딩 없이 같은 노트북의 루프백 WebSocket 으로 넘기고, 브라우저가 WebCodecs `VideoDecoder` 로 원본 해상도
(2304x1296)·25 fps 그대로 4분할에 그린다.

## 관련 명세

- 3.3 RTSP 접속 규칙, 16장 결정 "관제 화면 실시간 영상"(v1.3 수정 34): 현장 노트북 로컬 미리보기만 허용한다.
  원래 실시간 영상을 뺀 두 근거를 그대로 지킨다.
  1. **영상은 EC2 를 거치지 않는다.** 중계는 `127.0.0.1` 에서만 듣는다(`PREVIEW_HOST` 상수, 바꾸는 인자 없음,
     시험 `test_server_listens_on_loopback_only`). 관제 앱 주소 환경 변수도 루프백 주소만 받는다(`parseEnv`).
  2. **C110 RTSP 동시 접속 2세션.** 카메라 세션을 늘리지 않는다. 수신 루프가 Tier 0 로 넘기는 I0 기록을
     `PreviewSink` 가 그대로 나눠 받는다(원래 CameraSink 를 감쌈, 원래 전송을 먼저 한다).
- Lead 결정 2026-10-28(4차 GPU 세션 시연 리허설): 위 조건으로 로컬 미리보기를 허용한다.
- 미리보기 영상은 알림 채널이 아니고 위험 판정에 들어가지 않는다(AGENTS.md 규칙 1·5). 현장 노트북이 아닌 관제 화면은
  안내와 EC2 가 알린 카메라 상태(`viewer_health.tier0_health.cameras`)만 보인다.
- 요구사항 FR-133 비고를 같은 MR 에서 맞췄다.

## 코드·시험 위치

| 위치 | 내용 |
|---|---|
| `contracts/messages/cctv_preview.schema.json` | 텍스트 메시지 계약 1.0.0(묶음 v6.5.0). 예시 `contracts/examples/cctv_preview*.json`, 반례 `examples/invalid/cctv_preview.*.json` 5건 |
| `gateway/relay/src/simai_relay/preview.py` | `PreviewHub`(마지막 codec·소비자 목록), `PreviewConsumer`(소비자 큐·버림), `PreviewSink`, `PreviewServer`(websockets 동기 서버) |
| `gateway/relay/src/simai_relay/cli.py` | `--preview-port`(기본 꺼짐)·`--preview-origin`, 상태 로그 `preview`·`미리보기 상태` |
| `gateway/relay/tests/test_preview.py` | 계약, codec 먼저·키프레임부터, 늦은 소비자, 새 epoch, 느린 소비자 격리(큐·실제 소켓), 루프백 바인딩, Origin 거절, 소비자 상한, 꺼짐 기본값, `--send` 없는 CLI 끝까지 |
| `front/apps/admin/src/features/cctv/` | `types.ts`(계약과 같은 zod), `protocol.ts`(해석·Annex B), `previewSession.ts`(연결·카메라별 디코더), `webcodecs.ts`(브라우저 붙이기·캔버스), `cctv.store.ts`, `cctv.ts`(문구), `useCctvPreview.ts`, `CctvPage.tsx` |
| `front/apps/admin/src/__tests__/features/cctv/` | 계약 예시·반례를 zod 에 넣어 보기, 이진 메시지 해석, SPS·PPS 앞붙이기, 가짜 소켓·디코더로 연결 실패 안내·재연결·키프레임 대기·epoch·디코더 밀림·오류·상태 갱신 간격 |
| `front/apps/admin/src/lib/parseEnv.ts` | `VITE_CCTV_PREVIEW_URL`(기본 `ws://127.0.0.1:47032`, 루프백 ws(s) 만) |

## 연결 형식 (simai-cctv-preview/1)

- 텍스트: 접속 직후 `hello`(중계가 맡은 카메라 목록) 한 번 → 카메라별 마지막 `codec`(I0 `codec_info` 그대로 + WebCodecs
  `codec_string` = `avc1.` + SPS 의 profile_idc·constraint·level_idc). 카메라가 새 `stream_epoch` 를 시작하면 그 카메라의
  `codec` 을 다시 보낸다.
- 이진: `u32 BE 헤더 길이 | I0 frame_header JSON | H.264 접근 단위(Annex B, 받은 그대로)`. 소비자마다 카메라별 다음
  키프레임부터 보낸다. 헤더 이름은 I0 와 같다(촬영 시각은 `estimated_capture_time_ms`).
- 봉투를 쓰지 않는다(I0 영상 기록을 그대로 싣는 영상 연결). 메시지는 닫혀 있어 RTSP 주소·계정이 실릴 자리가 없다.
  RTSP 계정은 입력(`RtspSource`) 안에만 있고 I0 기록에 없다.
- 브라우저는 `description`(avcC) 없이 Annex B 로 디코더를 설정하고, 키프레임에 SPS·PPS 가 없으면 `codec` 의 것을 앞에 붙인다
  (C110 이 IDR 마다 SPS·PPS 를 싣는지는 실제 카메라로 확인 필요, 어느 쪽이든 동작한다).

## 정한 수치와 근거 (명세에 없음)

| 값 | 정한 값 | 근거 |
|---|---|---|
| 기본 포트 | 47032 | 중계 상태 보고 UDP 47031 옆. 관제 앱 기본 주소와 같게(시험 `test_default_port_matches_front_default`) |
| 바인딩 | 127.0.0.1 고정 | 명세 근거 1. 0.0.0.0·다른 주소를 고를 수 없다 |
| Origin 허용 | `https://simai.live`, `http://localhost:5173`·`http://127.0.0.1:5173`(개발), `…:4173`(vite preview). `--preview-origin` 으로 바꿈 | WebSocket 은 CORS 를 받지 않아 노트북에서 연 아무 웹 페이지나 루프백 포트로 영상을 가져갈 수 있다. 브라우저가 붙이는 Origin 은 페이지가 바꿀 수 없으므로 목록 밖·Origin 없음은 403 |
| 소비자 수 | 4 | 관제 탭 1~2개면 충분. 넘치면 1013 으로 닫는다(노트북 CPU·메모리 보호) |
| 소비자 큐 | 50 프레임 · 16 MiB | 4대 합 100 fps 의 0.5 s. 2304x1296 키프레임 수백 KiB 여러 장이 들어간다 |
| 버림 | 넘치면 그 소비자의 그 카메라만 다음 키프레임까지 건너뜀, `dropped_overflow` 로 셈 | 중간 P 프레임부터 넣으면 화면이 깨진다. 다른 소비자·카메라·Tier 0 전송은 영향 없음 |
| 압축 | WebSocket permessage-deflate 끔 | 압축된 영상을 다시 deflate 하면 CPU 만 쓴다 |
| 브라우저 디코더 대기 상한 | `decodeQueueSize` 8 초과면 다음 키프레임까지 건너뜀 | 밀린 프레임을 쌓아 늦게 그리지 않는다 |
| 그리기 | 디코더 출력 중 가장 새 것 하나를 다음 `requestAnimationFrame` 에 그림 | 밀린 프레임은 닫고 버린다 |
| 화면 상태 갱신 | 카메라마다 0.25 s 에 한 번까지 | 프레임마다 React 상태를 바꾸지 않는다(front CONVENTION 「상태」) |
| 지연 표시 | 마지막 프레임 나이 > 1 s 면 주황(`text-delay`) | 키프레임 간격 2 s 보다 짧게. 노트북 중계와 브라우저가 같은 기계라 벽시계(`Date.now()`)와 `estimated_capture_time_ms` 를 바로 비교 |
| 재연결 | 끊기면 3 s 뒤 다시 | 중계를 나중에 켜도 화면을 다시 열지 않고 이어진다 |

수신 스레드 비용: 소비자가 없으면 아무것도 하지 않는다. 있으면 헤더 JSON 한 번 + 바이트 이어붙이기 한 번(접근 단위 크기)을 하고
소비자마다 짧은 잠금 안에서 큐에 넣는다. 보내기는 소비자마다 따로 도는 서버 스레드가 한다. 미리보기 쪽 예외는 수신·Tier 0 전송으로
올리지 않는다(`test_preview_failure_does_not_break_tier0_sink`).

## https 화면에서 ws://127.0.0.1 접속

- **혼합 콘텐츠:** W3C Secure Contexts 는 `127.0.0.1`·`localhost`(·`[::1]`)를 "잠재적으로 신뢰할 수 있는" 출처로 보고, Mixed
  Content 규칙은 그런 주소를 막지 않는다. Chrome·Edge(Chromium)·Firefox(84+)는 https 페이지의 `ws://127.0.0.1` 을 혼합 콘텐츠로
  막지 않는 것으로 알려져 있다. **Safari 는 확인 필요**(막는다는 보고가 있다). 현장 노트북은 Chrome·Edge 를 쓴다.
- **Local Network Access(Chrome):** 공개 출처(`https://simai.live`)가 루프백으로 요청하면 Chrome 이 "이 기기의 다른 앱·서비스에
  접근" 권한 창을 띄운다(Chrome 142 에서 fetch 등부터). Chrome 개발자 블로그(2025-06)는 당시 WebSocket 은 아직 대상이 아니라고
  했고, 벤더 문서(Visualware)는 Chrome 147 부터 WebSocket 도 권한 창이 뜬다고 한다. **실제 노트북 Chrome 판에서 확인 필요.**
  창이 뜨면 현장 노트북에서 한 번 "허용"을 누른다(관리 정책 `LocalNetworkAccessAllowedForUrls` 로 미리 허용할 수 있다). 거부하면
  화면은 "현장 노트북에서만 표시" 안내로 돌아간다.
- 둘 다 막히면 노트북에서 관제 앱을 로컬로 띄운다(`vite preview`, `http://localhost:4173`, Origin 허용 목록에 있음).
- 참고: <https://developer.chrome.com/blog/local-network-access>,
  <https://myconnectionserver.visualware.com/support/v11/userguide/chrome-lna-websocket>

## 버린 대안

- **MJPEG 재인코딩:** 노트북 CPU 로 4 × 2304x1296 × 25 fps 를 디코딩하고 JPEG 로 다시 굽는다. 중계 수신 루프와 CPU 를 다퉈 I0
  촬영 시각 추정·전송 타이밍이 나빠진다(4차 세션 수신 지연 중앙값 190~320 ms). 화질도 떨어진다.
- **EC2 경유(WebRTC·HLS 중계):** 명세 근거 1 위반(영상은 EC2 를 거치지 않음), 업로드 대역도 든다.
- **`cam_viewer.py` 재사용:** 카메라마다 RTSP 세션을 하나 더 연다(명세 근거 2 위반, C110 2세션 한도에서 여유 1세션을 다 쓴다).
- **avcC description 으로 설정:** Annex B → 길이 접두 변환을 브라우저에서 프레임마다 해야 한다. Annex B 그대로 넣는 쪽이 단순하다.
- **asyncio 서버:** 중계는 스레드 구조라 websockets 동기 서버(연결마다 스레드)가 맞다. 수신 스레드에서 이벤트 루프로 넘기는
  다리가 필요 없다.

## 잔여 위험

- 키프레임 간격 2 s(50장)라 접속 직후·새 epoch·건너뛰기 뒤 최대 2 s 빈 칸("키프레임 대기")이다. 마지막 키프레임을 보관해 먼저
  보내는 방법은 그 뒤 P 프레임도 모두 보관해야 해서 하지 않았다.
- 브라우저 하드웨어 디코더가 2304x1296 4스트림을 동시에 풀지 못하면(노트북 GPU·드라이버) 소프트웨어 디코딩으로 CPU 를 쓴다.
  밀리면 다음 키프레임까지 건너뛰어 끊겨 보인다. 실제 노트북에서 Lead 확인 필요.
- 실제 카메라로는 아직 시험하지 않았다(합성 스트림만). C110 의 SPS 바이트·IDR 안 SPS/PPS 여부, Chrome LNA 권한 창, 중계
  CPU 증가는 Lead 가 실제 카메라로 확인한다.
- 노트북 안 다른 프로세스는 Origin 을 꾸며 붙을 수 있다(같은 기계의 사용자 권한이면 `.env` 계정도 읽을 수 있으므로 위협 모델 밖).

## 넘긴 일

- 검출 상자·트랙 ID 겹쳐 그리기(Tier 0 관측을 카메라 화면 좌표로 받아야 함, 계약 없음).
- 원격 관제 화면 실시간 영상(명세상 제외 유지).
- 카메라 한 대 크게 보기·전체 화면.
