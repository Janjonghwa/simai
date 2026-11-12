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
| 관측 고르기 | 영상과 `estimated_capture_time`·`frame_sequence` 가 같은 관측만 쓴다. `avatar_update_hold` 트랙은 `IDENTITY_UNCERTAIN` 으로 넘긴다 | 5.5 "같은 카메라·같은 프레임·같은 촬영 시각". 보류 트랙(분할 대기·교차 의심)은 ID 불확실과 같이 그 칸을 건너뛰고, 3초 넘게 이어지면 상태를 초기화한다(판정 모듈 규칙, S15P31A101-653) |
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

## 메타데이터 선별·단계별 진단 (S15P31A101-578)

- 관련 명세: 3.2 접수 규칙, 5.5의 카메라당 최대 2 Hz·동시 추론 1·150 ms 대기·근거 만료, 11장의 원본 비저장. 입력 NPZ와 I1 `ppe_state`의 형식·판정 수치는 바꾸지 않는다.
- `LatestChannelReader.supports_predicate`가 있는 입력은 같은 열린 NPZ에서 메타데이터를 먼저 읽는다. 모든 카메라가 이미 추론한 프레임이거나, 추론 시작 간격·같은 촬영 칸 제한·기존 `produced_at_ms` 기준 대기를 넘으면 영상 배열을 읽지 않고 `(doc, {})`를 돌려준다. 처리 가능한 카메라가 하나라도 있으면 기존처럼 전체 배열을 읽는다. 지원하지 않는 입력은 기존 호출을 유지한다.
- 파일 변경 여부만 캐시해 영구 배제하는 대안은 쓰지 않는다. 처리율로 보류된 같은 파일이 나중에 처리 가능해질 수 있으므로 매번 선별한다. 선별은 처리 기록을 소비하지 않는다. 이미지를 읽은 뒤와 관측 구성·gate 확인 뒤에 대기를 다시 확인하며, 카메라 순서·gate 재확인·상태 발행·근거 만료를 유지한다. PPE-04(-572)의 촬영 칸 진행/UTC 만료 분리와 별개다.
- 진단 시계는 주입 가능한 `ChannelTiming.clock`(`perf_counter`, 초 단위)이며 판정용 UTC/단조 시계와 분리된다. 단계별 최근 **256개**, 키 **128개**, 단계명 80자·카메라 ID 128자 상한이다. 키가 차면 추가 계측만 `overflow`로 센다. `count`는 프로세스 시작 후 누계, `window_samples`와 ms의 mean/p50/p95/p99/max는 최근 표본이다. 타이머는 실패한 호출의 경과 시간도 기록한다. 영상·검출·사람 ID·프레임 이력은 보관하지 않는다. 이 상한은 진단 비용을 제한하기 위한 구현값으로 판정 정책이 아니다.
- writer: `offer_to_write_start`는 daemon 대기, `write_prepare`는 참조·JSON 준비, `write_npz`는 파일 열기·NPZ 쓰기·닫기, `write_replace`는 `os.replace`, `write_total`은 전체 쓰기(정리 포함), `offer_to_write_complete`는 접수부터 정상 쓰기 반환까지다. offer 경로는 기존 비대기 lock 안에서 진단 시각 한 번만 저장하며 진단 lock·집계를 수행하지 않는다. daemon이 데이터 처리 시 최대 60초마다 `공유 채널 계측`을 로그로 보낸다. 게시 시각 필드를 새로 만들거나 옮기지 않는다.
- reader: `read_metadata`(NPZ 열기·메타데이터 해석), `read_select`(predicate), `read_images`, `read_total`(파일 열기·닫기 포함), `images_skipped`/`images_loaded`/`read_error`를 센다. loop의 `source_latest`가 호출 전체를 잰다.
- 카메라별: `camera_batch_wait`는 영상 읽기 반환부터 그 카메라 처리 순서까지의 누적 대기, `camera_observations`, `camera_gate`, `camera_inference`, `camera_association_ingest`, `camera_inferred`를 기록한다. `camera_source_age_at_check`/`camera_source_age_at_infer`는 기존 `produced_at_ms`와 UTC의 차이여서 시계 어긋남이면 음수일 수 있다. 추론 직전 age는 대기 초과로 거절된 시도도 포함하며, 추론 성공 횟수는 `camera_inferred`로 구분한다. GPU 타이머가 아닌 검출 호출의 wall 시간이다. 포함 관계인 구간은 합산하지 않고 백분위끼리 빼서 기여율로 해석하지 않는다.
- 선별 단계 거절은 `counts.metadata_duplicate`·`metadata_rate_skip`·`metadata_wait_exceeded`와 `metadata_rejected`(전체 배열 생략 호출 수), 카메라별 `metadata_skip_*`로 구별한다. 이미지 읽기 뒤 거절은 기존 `rate_skip`·`wait_exceeded`와 `camera_skip_*`다. 이 수치는 반복 폴링의 검사 횟수이므로 고유 프레임 폐기율이 아니다. `PpeLoop.diagnostics()`의 reader·loop 계측은 기존 PPE 60초 상태 로그에 함께 출력한다.
- 검증 위치: `ai/tests/ppe/test_service_metadata.py`, `ai/tests/io/test_channel_timing.py`, 기존 PPE 접수·누적·Risk Engine, `ai/tests/services/tier0/test_avatar_channel.py`, 아바타 루프 회귀. 실제 NPZ 배열 접근을 감시해 전체 선별 시 메타데이터만 읽는지, 같은 파일 재시도·새 epoch·혼합 카메라·영상 읽기 중 gate/150·151ms 전환·상태 만료를 확인한다. 원자 교체 테스트는 Windows 파일 삭제 제한 때문에 이미 열린 이전 파일을 메모리 핸들로 모사하며, OS별 동시 교체 성능 판정은 아니다.
- 잔여·별도 이슈: -579에서 게시 시각의 정확한 계약과 카메라 공정성을 검토한다. -580에서 VLM 명세·FP8·GPU 예산 불일치를 정리한다. 이번 로컬 회귀는 실제 H100 추론율 개선·공존 부하·G9 통과의 근거가 아니다. Pod를 켜거나 운영 GPU에서 시험하지 않는다.

## 게시 완료 관측 시각·순서 회전 (S15P31A101-579)

- 명세 §3.2·§5.5의 150ms 대기·2Hz·동시 추론 1을 유지한다. 이번 이슈에서 150ms 시작 사건을 **NPZ `os.replace`가 정상 반환한 직후 writer가 관측한 UTC ms**로 정의한다. NPZ 준비/쓰기 전의 `produced_at_ms`는 그대로 두어 기존 아바타의 접수 후 나이 검사와 호환한다. 촬영 시각·프레임 키·PPE-04 만료 시계·I1 계약·판정 임계값은 변경하지 않는다.
- 로컬 IPC 추가 계약: NPZ `simai.avatar-input@1` metadata에 매 쓰기마다 새 UUID의 `publication_id`를 추가한다. `<NPZ 이름>.published.json`에는 `format=simai.avatar-publication@1`, 같은 `publication_id`, 위 정의의 `published_at_ms`를 쓴다. writer는 완성된 NPZ를 원자 교체한 뒤 시각을 관측하고, 완료 표식도 임시 파일→원자 교체한다. 단일 writer 전제다. 최신 영상 1개·표식 1개만 유지하며 영상 복사본을 추가 보관하지 않는다.
- PPE 진입점은 reader의 `require_publication=True`를 명시한다. 같은 열린 NPZ의 metadata를 읽은 뒤 표식의 format·세대·정수 시각을 검사하고 일치한 경우에만 메모리 문서에 `published_at_ms`를 추가한 뒤 predicate와 배열 읽기를 진행한다. 표식 누락·불일치·깨짐·쓰기 실패·경쟁 세대는 영상 없이 None으로 건너뛴다. 이전 파일을 열고 이전 표식을 읽은 경우는 그 이전 snapshot을 일관되게 처리하며, 새 snapshot에 이전/새 시각을 섞지 않는다. 2개 파일의 순간적인 일괄 교체를 가정하지 않는다. 기본 reader(`False`)는 아바타 호환을 위해 표식을 요구하거나 읽지 않으며 produced 기준의 기존 나이 검사와 가용성을 유지한다. 표식만 실패해도 아바타 입력까지 차단하는 설계는 기각했다.
- 표식 게시까지의 지연은 150ms에 포함되므로 PPE에는 보수적이다. NPZ 교체 직후 죽으면 PPE는 새 영상을 표식 없이 채택하지 않는다. PPE가 소비 가능한 새 snapshot은 표식까지 게시된 것뿐이다. 새 ID 없는 기존 NPZ는 legacy 입력으로 읽고 PPE는 기존 produced 시각으로 검사한다. 새 ID가 있는데 표식이 없으면 PPE는 legacy로 폴백하지 않는다. 미래/잘못된 게시 시각도 처리 기록을 소비하지 않고 거절한다. 배포는 reader/PPE와 writer를 같은 소스로 갱신한다. 파일 한 개만 수동 복사하면 새 프로토콜의 PPE 입력은 준비되지 않은 것으로 본다. 아바타 기본 reader에는 이 제약을 추가하지 않는다.
- 이 사건은 커널의 실제 rename 순간과 같다고 주장하지 않는다. NPZ 교체 뒤 시각을 관측하기 전에 writer가 오래 선점되면 그 구간은 게시 시각에 반영되지 않는다. 대신 PPE에는 완료 표식 전까지 새 세대가 공개되지 않는다. 시각 관측 **뒤**의 표식 지연은 대기에 포함된다. 호스트 시계 역전/미래 시각이면 PPE가 건너뛴다. 커널 교체 순간부터의 엄밀한 150ms나 촬영→추론 전체 지연을 이 계약으로 보장하지 않는다.
- reader는 원본 NPZ에 내장된 `published_at_ms`를 항상 제거한다. 검증된 표식으로만 새 시각을 주입하며, ID 없는 legacy 입력이 임의의 내장 필드로 기존 produced 기준의 대기를 우회할 수 없게 한다.
- PPE는 일치한 게시 시각을 우선해 선별 단계·카메라별 시작·관측/gate 후 150ms를 모두 재확인한다. 순서는 마지막으로 실제 검출을 시작한 카메라의 사전순 다음부터 회전한다. 전 카메라를 다 처리하면 다음에도 같은 순환이지만, 예산이 앞쪽 일부에만 허용되면 다음 묶음은 미처리 카메라부터 기회를 얻는다. 마지막 카메라가 사라져도 사전순 다음 카메라부터 이어 간다. 미처리 카메라의 프레임/칸/추론 시각은 소비하지 않는다.
- 버린 대안: 게시 뒤 Python dict만 수정하면 NPZ 바이트에 반영되지 않는다. 동일 파일을 게시 후 다시 고치면 원자 snapshot을 깨뜨린다. mtime은 offer/쓰기/복사·시계 의미가 모호해 게시 사건으로 쓰지 않는다. 새 시각만 세대 없이 sidecar에 쓰면 이전 NPZ에 새 시각이 붙을 수 있다. 단순 step마다 시작 순서를 한 칸씩 옮기면 실제 처리 수가 달라질 때 편향이 남아 마지막 실제 추론 위치를 사용한다.
- 진단: -578의 `camera_source_age_*`는 새 입력에서 게시 시각 기준이고 legacy에서는 produced 기준이다. `camera_first`와 기존 카메라별 inferred/skip·batch_wait를 함께 해석한다. `write_publication`은 시각 관측 후 완료 표식의 직렬화/쓰기/교체 비용이다. 포함 관계인 단계 백분위를 합산하거나 고유 프레임 폐기율로 해석하지 않는다.
- 보장 범위: 동일 조건에서 반복되는 앞 카메라 편향을 줄이는 처리 기회 회전이다. 카메라마다 검출 시간·도착·누락·gate가 다르면 동일 추론율이나 6칸 확보를 보장하지 않는다. 대기 150ms보다 읽기 자체가 오래 걸리거나 CPU/GPU가 막히면 모두 건너뛸 수 있다. 로컬 합성 시험은 실제 H100 전체 부하·G9/G6 통과의 증거가 아니다.

## 코드·시험

- `ai/services/ppe/loop.py`·`__main__.py`, `ai/src/simai_ai/io/ppe_outbox.py`, `ai/services/tier0/service.py`(`_build_link`), `ai/services/tier0/i1_client.py`·`i1_publisher.py`, `deploy/scripts/simai_bootstrap.py`
- `ai/tests/ppe/test_service_loop.py`, `ai/tests/io/test_ppe_outbox.py`, `ai/tests/services/tier0/test_i1_units.py`, `ai/tests/deploy/test_simai_supervisor.py`(`test_ppe_*`)

## 지연 입력의 촬영 칸과 만료 시계 분리 (S15P31A101-572, 감사 PPE-04)

- 문제: 공유 뒤 대기는 150ms 안이고 실제 `AdmissionGate`도 허용하는 촬영→공유 지연 540ms에서, 처리 UTC로 먼저 닫힌 칸에 입력이 도착한다. 5Hz·시작 위상 100ms 반례는 28회 추론 중 27회가 `LATE_FRAME`으로 버려져 명시적 미착용이 `UNKNOWN`에 머문다. 관련 명세는 §5.5의 촬영 시각 500ms 누적과 마지막 유효 근거 + 10초 만료다.
- 수정: `PpeJudge`는 거부·오래된 epoch·닫힌 칸 검사를 통과한 프레임의 최대 촬영 시각을 보관한다. `PpeLoop`는 이를 `evaluate(now_ms, capture_time_ms=...)`에 넘겨 칸을 진행하고, 현재 UTC `now_ms`는 근거 만료와 트랙 보존 기간 정리에 계속 쓴다. 닫힌 칸은 촬영 시각이 뒤로 가도 다시 열지 않는다. 기존 동기·오프라인 호출은 키워드를 생략해 같은 시각으로 두 기능을 평가할 수 있다.
- 유지: 500ms 칸·100ms 닫힘 유예·6칸 창·착용 5/6·미착용 6/6·실제 10초 만료, 카메라별 최대 2Hz·공유 후 대기 150ms·ID 초기화·GPU 몫·I1 계약은 같다. 판정 수치·사유 코드가 아닌 서비스의 시각 전달을 고치므로 `policy_version`도 유지한다. 촬영 시각·근거를 처리 시각으로 다시 쓰지 않는다.
- 검증: `ai/tests/ppe/test_capture_watermark.py`는 실제 `AdmissionGate`에 합성 부하를 넣어 촬영→공유 지연 20/540/600ms × 시작 위상 3개 × 카메라 1/4대, 실제 저장소·R9/R11 REPLAY 생명주기, 검출 없음, 입력 중단·gate 차단 중 WORN/NOT_WORN의 만료 10,000/10,001ms 및 트랙 정리, 시각 역전과 카메라 충돌을 확인한다. 모델·영상·시계는 합성이고 실제 경보는 보내지 않는다.
- 버린 대안: 처리 UTC에서 임의의 고정 지연을 빼거나 닫힘 유예를 늘리면 부하가 달라졌을 때 같은 문제가 생긴다. 촬영 시각을 만료에도 사용하면 입력이 멈출 때 오래된 상태가 남는다. 빈칸 복사나 6/6 완화는 근거를 만들어 내므로 쓰지 않는다.
- 잔여 위험: 마지막 열린 촬영 칸은 후속 촬영 진행이 있어야 닫힌다. 입력이 멈추면 그 칸을 추측해서 채우지 않고 기존 근거를 실제 10초에 만료한다. 닫힌 칸의 순서 역전 입력은 계속 버린다. 실제 GPU 처리율·연속 6칸 확보·카메라 ID 대응·G9/G6 및 EC2/워치 연결은 별도 검증 대상이다. H200 단독 결과도 H100 동시 실행 성능을 대신하지 않는다.
