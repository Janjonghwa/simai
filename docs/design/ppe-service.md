# PPE 서비스 연결 (S15P31A101-507)

명세 5.5(PPE 워커)·3.2(GPU 몫·접수 규칙)·12.3(I1 `ppe_state`)를 따른다. 판정 로직(`simai_ai.ppe`)과 계약(`ppe_state` 1.0.0)은 S15P31A101-491 에서 들어왔다. 이 문서는 그것을 Tier 0 옆의 별도 프로세스로 돌리는 연결만 다룬다.

## 흐름

```
Tier 0 (SIMAI_AVATAR_SHARE=1)            PPE (python -m services.ppe)                Tier 0 (SIMAI_PPE_RELAY=1)
 _share_avatar → /dev/shm/simai-avatar-input.npz ──읽기──▶ PpeLoop.step
                                          검출 → 사람 연결 → 누적 → ppe_state
                                          └─ /dev/shm/simai-ppe-outbox/<epoch>-<seq>.json ──▶ PpeOutboxReader.take
                                                                                       → I1Publisher.add_source → EC2
```

## 정한 것과 근거

| 항목 | 값 | 근거 |
|---|---|---|
| 입력 | 아바타 입력 공유 파일(`simai.avatar-input@1`)을 읽기만 한다. Tier 0 코드는 고치지 않는다 | 이미 카메라별 원본 BGR 영상(I0 디코딩 그대로)·같은 프레임 키의 2D 관측·트랙의 `world_id`·`identity_revision`·`identity_confidence` 가 들어 있다. 읽는 쪽은 파일을 가져가지 않으므로 아바타와 같이 읽어도 된다 |
| 관측 고르기 | 영상과 `estimated_capture_time`·`frame_sequence` 가 같은 관측만 쓴다. `avatar_update_hold` 트랙은 `IDENTITY_UNCERTAIN` 으로 넘긴다 | 5.5 "같은 카메라·같은 프레임·같은 촬영 시각". 보류 트랙(분할 대기·교차 의심)은 상태를 초기화한다(판정 모듈 규칙) |
| 주기 | 카메라마다 추론 시작 간격을 단조 시계로 500 ms 이상 두고, 같은 촬영 시각 500 ms 칸은 한 번만 처리한다(-567). Tier 0 가 공유한 뒤 150 ms 를 넘긴 프레임은 버린다. 추론은 한 스레드·한 장씩. 상태는 500 ms 마다 보낸다 | 5.5: 카메라당 최대 2 Hz, 대기 1, 동시 추론 1, 대기 150 ms. 송신은 1~2 Hz |
| 접수 문 | `AdmissionGate`(Tier 0 표시 파일의 지연 신호, 표시 파일이 없으면 모름 → 멈춤). 막히면 새 추론만 멈추고 누적 상태는 계속 보낸다. Tier 0 가 정상 종료했으면 보내지 않는다 | 5.5 "Tier 0 지연을 모르거나 넘으면 새 추론을 멈추고 10초 안정 뒤 재개". 계속 보내면 근거 만료(10초)로 UNKNOWN 이 된다 |
| 출력 | outbox 폴더에 메시지 한 건 = 파일 한 개. Tier 0 가 꺼내 자기 I1 연결의 텔레메트리 대기열(`TELEMETRY_TYPES`, S15P31A101-565)로 보낸다(outbox 대기 상한 32, 10초 넘은 파일은 버림, `source_id` 는 Tier 0 연결 값으로 맞춤, I1 끊김·적체 시 `telemetry_limit` 상한에서 오래된 것부터 버림) | PPE 가 같은 `source_id` 로 I1 을 따로 열면, EC2 의 `event_ack`·`event_close`·`event_escalate` 전달기가 gateway 가 맞는 연결 중 첫 번째로 보내므로 운영자 명령이 PPE 로 갈 수 있다. 다른 `source_id` 는 계약(tier0-01)을 바꿔야 한다. 빈 순번은 계약상 잃은 메시지다 |
| GPU | `cap_process_memory(SIMAI_PPE_GPU_MEMORY_FRACTION=0.05)`. bootstrap 은 Tier 0 + 분석 워커 + PPE ≤ 0.95 를 본다 | 3.2 표·49행. 초기값으로는 여유가 0 이라 G0 실측으로 다시 정한다 |
| 기동 | `SIMAI_SERVICES` 에 `ppe` 를 명시해야 켜진다. lobby 기본·preflight 묶음에는 넣지 않았다 | G9(동시 실행 p99) 판정 전이라 시연 기본값을 바꾸지 않는다 |

## 버린 대안

- PPE 가 I1 WebSocket 을 따로 연다: 위 EC2 전달기 문제. EC2 를 고치거나 계약을 바꿔야 한다.
- Tier 0 에 PPE 전용 공유 채널을 새로 만든다: 아바타 공유 파일에 필요한 것이 이미 다 있다. Tier 0 판정 경로에 출력을 하나 더 늘리지 않는다.

## 알려진 한계

- 트랙의 local → World 대응(`local_keys`)은 그 틱의 다시점 연관 결과다. 공유되는 관측·영상은 카메라별 최신 프레임이라 둘이 최대 100 ms 어긋날 수 있다(공유 조건). local ID 는 카메라 안에서 이어지는 번호라 대부분 같은 사람이지만, 계약 문구("같은 프레임의 Tier 0 대응만")를 엄밀히 맞추려면 Tier 0 가 대응을 만든 프레임 키도 공유해야 한다. 후속.
- 로컬 시험은 가짜 검출기로만 돈다(torch·ultralytics 없음). 실제 추론·GPU 상한·Tier 0 와 동시 실행은 이미지 재빌드 뒤 Pod 에서 확인한다(G9).
- 48GB급 미만 GPU 에서는 감시 프로세스가 Tier 0 단독으로 뜨므로 PPE 도 뜨지 않는다(기존 규칙).

## 기본 5Hz 입력의 누적 창 보정 (S15P31A101-567)

- 문제: Tier 0 공유 입력이 200 ms 간격일 때 촬영 시각끼리 500 ms 이상 떨어져야 추론하게 하면 600 ms마다 선택된다. 고정 500 ms 칸에 빈칸이 생겨 명시적 미착용 검출이 이어져도 `NOT_WORN`의 6/6 조건을 만들지 못한다(명세 5.5).
- 수정: `PpeLoop`는 주입된 단조 시계로 카메라별 추론 시작 사이 500 ms를 지킨다. 별도로 `(camera_id, stream_epoch, capture_time // bin_ms)`가 같은 칸은 한 번만 고른다. 실행 진입점은 `time.monotonic_ns()`를 주입하고, UTC 시계는 대기·상태 송신·근거 만료에 계속 쓴다. 시험에서 단조 시계를 별도 주입하지 않으면 기존 주입 시계가 대역을 맡는다.
- 결과: 200 ms 촬영 입력에서 선택된 프레임의 촬영 간격이 400/600 ms로 바뀔 수 있어도, 실제 추론 시작은 500 ms 이상 떨어진다. 최신 입력을 보며 다음 촬영 칸을 채우므로 공유 주기를 올리거나 GPU 동시 추론을 늘리지 않는다.
- 유지: 상태기의 500 ms·6칸·착용 5/6·미착용 6/6·10초 만료·ID 초기화 수치, I1 스키마와 `policy_version`은 바꾸지 않는다. 빈칸·검출 없음은 `UNKNOWN`이다. 근거가 부족한 칸을 복사해 채우거나 6/6 조건을 낮추는 대안은 쓰지 않는다.
- 검증: `ai/tests/ppe/test_service_loop.py`에서 200/250/500 ms × 시작 위상 3개 × 카메라 1/4대, ±20 ms 촬영 시각 지터, 입력 누락·검출 없음, 같은 촬영 칸 중복, 단조 시계 처리율을 검사한다. 생산된 메시지를 실제 `PpeStateStore`·R9/R11·`LifecycleTracker`에 넣어 미착용 발생과 무근거 미발생을 REPLAY로 확인한다(실제 알림 없음).
- 범위 밖: `ppe_state` I1 송신 허용은 S15P31A101-565(MR !508)에서 별도로 수정한다. GPU 동시 부하·G9 정확도·EC2/화면 실연결은 이 로컬 회귀의 결과가 아니다. 부하·입력 누락으로 칸이 부족하면 여전히 `UNKNOWN`일 수 있다.

## 카메라 사이 접수 상태 재확인 (S15P31A101-568, 감사 PPE-03)

- 문제: 한 step 시작에만 허용을 확인하면 첫 카메라 추론 중 Tier 0 부하 표시가 불허로 바뀌어도 나머지 카메라의 검출을 새로 시작한다. 명세 3.2·5.5의 새 추론 중지 규칙을 카메라별로 적용한다.
- 수정: 프레임 중복·처리율·대기 검사와 관측 구성 뒤, 검출 및 처리 기록 갱신 직전에 `AdmissionGate.check()`를 다시 호출한다. LIVE 부하 불허면 남은 묶음을 중단하고 기존 상태를 계속 발행한다. NOT_LIVE면 그 step의 송신도 중단한다. 마지막 카메라 추론 중 정상 종료하는 경우를 위해 송신 전에도 LIVE를 확인한다. 관측 구성·gate 재확인 뒤에도 프레임 대기 상한 150ms를 확인해 그 사이 오래된 프레임을 검출하지 않는다.
- 유지: 차단된 카메라는 프레임·촬영 칸·추론 시작 시각을 소비하지 않아 회복 후 다시 처리할 수 있다. 기존 gate의 기본 10초 안정 대기·LIVE 종료 유예, 카메라당 최대 2Hz, 근거 10초 만료, I1 계약·상태 임계값·GPU 몫은 변경하지 않는다.
- 검증: `ai/tests/ppe/test_service_admission.py`에서 4대 중 1대/3대 처리 후 부하·모름·NOT_LIVE 전환, 관측 구성 중 전환, 마지막 추론 중 정상 종료, 미처리 프레임 재시도, 보고 지속·근거 만료, gate 재확인 비용을 포함한 대기 150/151ms 경계를 확인한다. 실제 `AdmissionGate`에 합성 표시 파일을 넣어 초과 지연·모름·파일 없음·깨짐·오래됨을 감지하고 정상 복귀 후 9.999초 차단·10초 재개를 검사한다. GPU 없이 가짜 검출기와 주입 시계로 실행한다.
- 버린 대안: 카메라마다 다음 step까지 기다리거나 묶음 시작의 허용을 재사용하면 전환 이후 새 추론이 계속된다. gate가 닫혔다고 상태 송신까지 전부 막으면 기존 근거의 만료가 전달되지 않는다.
- 잔여 위험: 이미 시작한 GPU 검출을 선점·취소하지 않는다. 표시 파일의 생산·갱신 주기 및 검사 직후 신호가 바뀌는 경쟁 구간도 남는다. 실제 동시 실행 지연·검사 비용·메모리와 G9 정확도는 Pod 실측으로 별도 확인한다. 이번 수정·회귀에서는 Pod를 켜지 않는다.

## 코드·시험

- `ai/services/ppe/loop.py`·`__main__.py`, `ai/src/simai_ai/io/ppe_outbox.py`, `ai/services/tier0/service.py`(`_build_link`), `ai/services/tier0/i1_client.py`·`i1_publisher.py`, `deploy/scripts/simai_bootstrap.py`
- `ai/tests/ppe/test_service_loop.py`, `ai/tests/io/test_ppe_outbox.py`, `ai/tests/services/tier0/test_i1_units.py`, `ai/tests/deploy/test_simai_supervisor.py`(`test_ppe_*`)
