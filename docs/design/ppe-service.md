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
| 주기 | 카메라마다 같은 프레임은 한 번, 지난 처리 뒤 500 ms 안의 프레임은 건너뛴다. Tier 0 가 공유한 뒤 150 ms 를 넘긴 프레임은 버린다. 추론은 한 스레드·한 장씩. 상태는 500 ms 마다 보낸다 | 5.5: 카메라당 최대 2 Hz, 대기 1, 동시 추론 1, 대기 150 ms. 송신은 1~2 Hz |
| 접수 문 | `AdmissionGate`(Tier 0 표시 파일의 지연 신호, 표시 파일이 없으면 모름 → 멈춤). 막히면 새 추론만 멈추고 누적 상태는 계속 보낸다. Tier 0 가 정상 종료했으면 보내지 않는다 | 5.5 "Tier 0 지연을 모르거나 넘으면 새 추론을 멈추고 10초 안정 뒤 재개". 계속 보내면 근거 만료(10초)로 UNKNOWN 이 된다 |
| 출력 | outbox 폴더에 메시지 한 건 = 파일 한 개. Tier 0 가 꺼내 자기 I1 연결로 보낸다(대기 상한 32, 10초 넘은 파일은 버림, `source_id` 는 Tier 0 연결 값으로 맞춤) | PPE 가 같은 `source_id` 로 I1 을 따로 열면, EC2 의 `event_ack`·`event_close`·`event_escalate` 전달기가 gateway 가 맞는 연결 중 첫 번째로 보내므로 운영자 명령이 PPE 로 갈 수 있다. 다른 `source_id` 는 계약(tier0-01)을 바꿔야 한다. 빈 순번은 계약상 잃은 메시지다 |
| GPU | `cap_process_memory(SIMAI_PPE_GPU_MEMORY_FRACTION=0.05)`. bootstrap 은 Tier 0 + 분석 워커 + PPE ≤ 0.95 를 본다 | 3.2 표·49행. 초기값으로는 여유가 0 이라 G0 실측으로 다시 정한다 |
| 기동 | `SIMAI_SERVICES` 에 `ppe` 를 명시해야 켜진다. lobby 기본·preflight 묶음에는 넣지 않았다 | G9(동시 실행 p99) 판정 전이라 시연 기본값을 바꾸지 않는다 |

## 버린 대안

- PPE 가 I1 WebSocket 을 따로 연다: 위 EC2 전달기 문제. EC2 를 고치거나 계약을 바꿔야 한다.
- Tier 0 에 PPE 전용 공유 채널을 새로 만든다: 아바타 공유 파일에 필요한 것이 이미 다 있다. Tier 0 판정 경로에 출력을 하나 더 늘리지 않는다.

## 알려진 한계

- 트랙의 local → World 대응(`local_keys`)은 그 틱의 다시점 연관 결과다. 공유되는 관측·영상은 카메라별 최신 프레임이라 둘이 최대 100 ms 어긋날 수 있다(공유 조건). local ID 는 카메라 안에서 이어지는 번호라 대부분 같은 사람이지만, 계약 문구("같은 프레임의 Tier 0 대응만")를 엄밀히 맞추려면 Tier 0 가 대응을 만든 프레임 키도 공유해야 한다. 후속.
- 로컬 시험은 가짜 검출기로만 돈다(torch·ultralytics 없음). 실제 추론·GPU 상한·Tier 0 와 동시 실행은 이미지 재빌드 뒤 Pod 에서 확인한다(G9).
- 48GB급 미만 GPU 에서는 감시 프로세스가 Tier 0 단독으로 뜨므로 PPE 도 뜨지 않는다(기존 규칙).

## 코드·시험

- `ai/services/ppe/loop.py`·`__main__.py`, `ai/src/simai_ai/io/ppe_outbox.py`, `ai/services/tier0/service.py`(`_build_link`), `deploy/scripts/simai_bootstrap.py`
- `ai/tests/ppe/test_service_loop.py`, `ai/tests/io/test_ppe_outbox.py`, `ai/tests/deploy/test_simai_supervisor.py`(`test_ppe_*`)
