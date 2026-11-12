# S15P31A101-634 PPE 조용한 비활성 감지와 워치 PPE 표시

관련 명세: 5.5(PPE), 7.2(R9·R11), 7.3(알림 채널), 8.10, 10장, 11.3(지연 전달·감사 전용), 3.2(재동기화), 부록 C.6(워치), 부록 E 장면 3. 선행: -507(PPE 서비스), -531(ppe_view), -548·-549(R11·조끼), -556(시연 예외 활성화), -565(ppe_state I1), -587(PPE 기동 절차). Jira: <https://ssafy.atlassian.net/browse/S15P31A101-634>.

2026-11-12 개인 감사(dev d1e29fa7) PPE 재정리의 A1~A5·B1~B4 를 고친다. 안전 쪽 실패(UNKNOWN 은 새 발생·자동 해소 없음, PPE 장애가 Tier 0 를 멈추지 않음)는 그대로 둔다.

## 결정

- **B2 워치 해소 통지: I4 새 메시지 `watch_alert_clear` 1.0.0**(2026-11-12 사용자 결정). 운영 대책(해소 뒤 확인 단추로 지움)은 버렸다. 다시 착용해도 손목에 '안전모'·'조끼'가 남아 장면 3 을 보여 줄 수 없다.
- **A3 lobby 기본 서비스에 ppe 포함: 결정 대기(Lead, -507 결정 변경).** 이 이슈는 기본 목록을 바꾸지 않는다. 대신 아래 A1 안내가 PPE 를 띄우지 않은 상태도 '작업 중 PPE 상태 수신 없음'으로 화면에 드러내고, 시연 T-60 점검(18.2)에 ppe 기동·`ppe_state` 수신 확인을 넣도록 제안한다.

## 바꾼 것

### 계약 (v7.36.0, I1 변경 없음)
| 계약 | 내용 |
|---|---|
| I4 `watch_alert_clear` 1.0.0 신설 | EC2 → 워치. `event_id`·`reason: RESOLVED`·`sent_at`, 닫힌 객체, World ID 없음. ACK 없음. 예시·반례 3개 |
| I2 `ppe_view` 1.1.0 | 선택 `last_received_at_ms`(그 Tier 0 의 ppe_state 를 EC2 가 마지막으로 받은 시각, 사람 0명 메시지도 셈, 받은 적 없으면 null). 예시·반례 1개 |

배포 순서 제약 없음: 이전 워치 앱은 모르는 msg_type 을 무시하고(지금처럼 알림이 남음), 이전 관제 앱은 모르는 payload 최상위 필드를 무시한다.

### A. 조용한 비활성
| 위치 | 내용 |
|---|---|
| `ai/services/ppe/alive.py`(새), `__main__.py` | 주기를 **정상으로 끝낼 때만** 1초마다 생존 파일(`SIMAI_PPE_ALIVE`, 기본 `$SIMAI_ROOT/state/ppe.alive`)에 UTC ms 를 적는다(임시 파일 → `os.replace`). 매 주기 실패·멈춤이면 값이 낡는다 |
| `deploy/scripts/simai_bootstrap.py` | ppe 를 띄우기 전에 지난 파일을 지우고, 띄운 뒤 준비 시간(`SIMAI_PPE_ALIVE_GRACE_S`, 기본 120 s)이 지나면 파일이 없거나 `SIMAI_PPE_ALIVE_STALE_S`(기본 30 s)보다 오래되면 그룹 KILL → 기존 백오프로 다시 띄운다. Tier 0 는 그대로 |
| `server/.../ppe/PpeReceipts`(새), `PpeStateIngestor`, `PpeViewRepository`, `PpeViewRelay` | 계약 검사를 지난 ppe_state 마다 Tier 0 별 마지막 수신 시각을 메모리에 둔다(사람 0명 포함). 스냅샷에 `last_received_at_ms`, 처음 상태 REST 는 받은 적 있는 Tier 0 도 싣는다. `/topic/ppe` 판 1.1.0 |
| `front/.../ppe/ppeHealth.ts`·`usePpeReceipt.ts`(새), `monitor/PpeWorkNotice.tsx`(새), `AppLayout` | 진행 중 작업이 있으면 `GET /api/ppe/current` 를 5초마다 받아 `snapshot_at_ms − last_received_at_ms > 10 s`(둘 다 EC2 시계)면 'PPE 상태 수신 없음'. 작업 중 적용 묶음 요구가 REQUIRED·그 작업이 아니면 'PPE 요구 미적용', 작업이 없는데 REQUIRED 면 'PPE 요구 해제 대기'. 모두 15초 이어질 때만 보인다 |

### B. 워치
| 위치 | 내용 |
|---|---|
| `WatchAlertService.alert` | `delivery = AUDIT_ONLY` 인 이벤트는 보내지 않는다(B3). 해소된 LIVE 이벤트면 그 이벤트로 알림을 받았고 아직 안 지운 연결된 워치마다 `watch_alert_clear` 를 보내고 `watch_alert.cleared_at` 을 적는다(B2) |
| `WatchAlertService.resync`(새, `WatchConnected`·`WatchBindingChanged`(배정) 때) | 그사이 해소된 알림을 지우고(B2), 배정된 사람의 열린 실시간 LIVE 이벤트 중 최근 10분 안에 갱신된 것을 보낸다(B4). 이미 보낸 것은 기존 중복 방지로 다시 울리지 않는다 |
| V60 | `watch_alert.cleared_at`, 부분 색인 `watch_alert_uncleared_idx`. V59 는 -632(!582) 몫이다. Flyway outOfOrder 가 꺼져 있어 **!582 를 먼저 머지·배포**한다(이 MR 이 먼저면 머지 직전에 번호를 다시 고른다) |
| 워치 `AlertBoard.pending` | 정렬을 "기한 안(ACTIVE) 먼저 → 등급 → 최근" 으로 바꿔 화면(`current`)과 진동(`vibrating`)이 같은 알림을 가리킨다(B1) |
| 워치 `AlertBoard.clear`, `I4Codec`, `I4Service` | `watch_alert_clear` 를 읽어 확인 대기 알림을 빼고 진동·알림창을 정리한다. 확인한 알림은 '확인함' 표시용으로 남긴다. ACK 를 보내지 않는다 |

## 정한 수치와 근거
| 값 | 근거 |
|---|---|
| 생존 파일 주기 1 s | 메인 루프 0.05 s 마다 쓰면 디스크를 두드린다. 감시 판정(30 s)보다 충분히 짧다 |
| 준비 시간 120 s | 가중치 불러오기·CUDA 초기화·TensorRT 없이 YOLO26x 첫 추론. 실측 전 보수값, 환경 변수로 바꾼다(미실측) |
| 무갱신 30 s | 상태 근거 만료 10 s 의 세 배. 접수 문 차단(Tier 0 지연)은 정상 주기라 값이 갱신돼 잘못 죽이지 않는다 |
| 화면 수신 끊김 10 s | PPE 게시 주기 0.5 s, 상태 근거 만료 10 s |
| 화면 안내 지속 15 s | 작업 시작·종료 묶음 적용(수 초)·5 s 조회 주기 한 번 늦음으로 깜박이지 않게 |
| 재발송 범위 10 분 | 격상 판정 10 분과 같다. 지난 관제에 열린 채 남은 이벤트를 새로 붙은 워치에 한꺼번에 울리지 않게 |

## 버린 대안
- **health 계약(I1)에 PPE 항목 추가**: I1 LIVE 계약이라 Lead 승인이 필요하다. EC2 가 받은 ppe_state 로 판단하면 I1 변경 없이 된다.
- **PPE 가 연속 실패 시 스스로 종료**: 한 주기에서 멈추는(행) 경우를 못 잡는다. 생존 파일 하나로 둘 다 잡는다.
- **워치 해소를 운영 대책으로**: 위 '결정'.
- **해소 통지를 watch_alert 에 칸으로 싣기**: watch_alert 는 진동 알림이라 받은 워치가 울린다. 별도 메시지가 의미가 분명하다.
- **화면 수신 판단을 STOMP `/topic/ppe` 로**: 사람이 없거나 바뀐 것이 없으면 보내지 않아 '받지 못함'과 '바뀐 것 없음'을 가를 수 없다.

## 잔여 위험
- 생존 파일 준비 시간 120 s 는 실측 전 값이다. 실제 GPU 에서 첫 정상 주기가 더 늦으면 PPE 가 반복 재시작된다(로그 `생존 파일이 … 없음`). G9 실측 때 확인한다.
- 마지막 수신 시각은 EC2 메모리에 있다. EC2 를 다시 띄운 직후 0.5 s 동안은 '받은 적 없음'이다(15 s 지속 조건으로 안내되지 않는다).
- 재동기화에서 해소가 감사 전용(AUDIT_ONLY)으로만 기록되면 `RiskEventsDelivered` 가 나지 않아 해소 통지가 다음 워치 재연결 때 간다.
- 워치가 끊긴 동안 해소된 알림의 통지는 다시 이어질 때 가므로 그사이 손목에는 남는다(끊김 중에는 어떤 메시지도 못 받는다).
- REPLAY 대체 때 관제 목록이 LIVE 아닌 이벤트를 버리는 문제(감사 D2)와 사람별 상태 글(-531)은 이 이슈 밖이다.

## 명세 반영 필요(명세 정리 이슈로 넘김)
- 부록 C.6: I4 메시지 목록에 `watch_alert_clear`(해소 통지, ACK 없음)와 워치 화면 우선순위(울리는 알림 먼저)를 더한다.
- 12장 I2 `ppe_view` 1.1.0 `last_received_at_ms`, 화면 안내 세 가지.
- 18.2 시연 T-60 점검표: ppe 기동, 관제 화면에 'PPE 상태 수신 없음'이 없을 것, 작업 시작 뒤 'PPE 요구 미적용'이 없을 것.

## 시험
- 계약: `uv run contracts/scripts/validate_examples.py` 통과.
- 서버: `WatchAlertIntegrationTest` 4건 추가·1건 갱신(해소 통지 한 번, 끊긴 동안 해소 → 재연결 때 통지, AUDIT_ONLY 미발송, 연결·배정 때 재발송·오래된 것 제외), `PpeViewIntegrationTest` 1건(사람 0명 수신 시각). 시험 도우미가 연결 직후 메시지를 버리지 않게 고쳤다.
- 워치: `AlertBoardTest` 4건 추가·1건 갱신(화면=진동, 기한 지난 '고소' 위에 '안전모', 지우기), `I4CodecTest` 1건.
- ai: `tests/services/ppe/test_alive.py` 4건, `test_simai_supervisor.py` 2건(멈춘 PPE 재시작, 준비 뒤 파일 없음). 기존 시험은 가짜 PPE 가 파일을 쓰지 않으므로 준비 시간을 크게 둔다.
- 프런트: `ppeHealth.test.ts`, `PpeWorkNotice.test.tsx`.
