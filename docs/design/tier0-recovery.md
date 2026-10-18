# Tier 0 재시작 복구 설계와 운영 절차

S15P31A101-51. 기획서 §3.2(재시작 복구·복구 순서·저널 계약), §3.10(복원 전 빈 현장 경보 목록 금지), §6.2, 부록 D-G3-09·11·13, §15.1 G3 기준이다.
코드는 `ai/src/simai_ai/recovery/`(checkpoint, projection, restore, checkpointer, health_gate)이고 시험은 `ai/tests/recovery/` 다.
배선(진입점)은 S15P31A101-171, 복원 Track 과 재관측 매칭·30초 규칙은 S15P31A101-54, Network Volume 강제 종료 실측은 S15P31A101-92 다.

## 구조

| 단계 | 내용 |
|---|---|
| 저널 | 전이 기록마다 `meta.state` 에 전이 뒤 `EventState` 전체를 남긴다(I1 로는 보내지 않음). 저널이 정본이다 |
| 투영 | `EventProjection` 이 fsync 끝난 기록을 offset 순서로 적용한다. 이벤트별 sequence 로 멱등이고, 해소 기록(tombstone)으로 해소된 이벤트의 과거 전이를 거른다 |
| 체크포인트 | `Checkpointer` 가 1초마다 투영으로 쓴다. 활성 이벤트, 해소 기록(최근 1024개 + 밀려난 것의 판정 시각 watermark), last_applied_journal_offset·msg_id, config_version, 마지막 촬영 시각, Track(꺼내기만) |
| 복구 | `recover`: 체크포인트 선택 → 그 offset 이후 저널 재적용 → 활성 이벤트를 UNVERIFIABLE 로 복원(저널에 기록) |
| Health | `RecoveryHealthGate`: 재시작 뒤 촬영 시각이 앞으로 간 새 프레임과 시계 차이 추정이 함께 확인될 때까지 UNSAFE_UNKNOWN |

체크포인트 형식(내부, `format_version` 2): `checkpoint-{세대}.json` = {format, format_version, sha256, body}. 형식 1(해소 기록 없음)은 읽지 않고 직전 세대·저널 전체로 물러선다.
저널 정리는 남은 온전한 체크포인트 세대의 가장 작은 offset 과 acked_offset 중 작은 값 이하만 한다(명세 경계보다 보수적, 직전 세대로 물러설 구간 보존).

### 해소 기록의 보장 경계

- 최근 1024개 해소 이벤트: sequence 로 정확히 거른다.
- 그보다 오래돼 밀려난 이벤트: 밀려난 해소 기록의 마지막 판정 시각 최댓값(watermark) 이하인 판정 시각을 가진, 활성도 tombstone 도 아닌 이벤트의 기록을 오래된 기록으로 보고 건너뛴다. Tier 0 판정 시계가 watermark 보다 더 뒤로 가지 않는다는 전제에 기댄다(1024개 해소가 쌓이는 시간보다 큰 시계 역행이면 새 이벤트를 잘못 거를 수 있다).

## 시작 거부와 조치 (운영 절차)

`recover` 는 활성 이벤트를 추측하지 않는다. 아래 경우 Tier 0 는 시작하지 않고 노트북에는 "관제 중단"이 표시된다. **어느 경우든 저널을 지워 정상 복구로 삼지 않는다.** 저널을 지우면 활성 이벤트와 EC2 로 보내지 못한 전이(outbox)를 잃는다.

공통 첫 조치: 같은 Network Volume 의 `journal/`(세그먼트, `acked_offset.json`, `evidence_index.json`), `checkpoints/` 를 통째로 보관 위치(날짜·gateway_id 를 붙인 디렉터리)에 복사해 원본을 보존하고, 오류 메시지·로그와 함께 Lead 에 알린다.

| 오류 | 원인 | 조치 |
|---|---|---|
| `LegacyJournalError` "meta.state 없는 이전 형식 저널" | S15P31A101-51 이전 코드가 쓴 저널. 상태를 되살릴 수 없다 | 배포 전 시험 데이터면 `journal/`·`checkpoints/` 디렉터리를 보관 위치로 옮기고 새로 시작한다. 운영 데이터면 옮기지 말고 보존한 채 Lead 에 알린다. 운영 배포 전이라 업그레이드 bridge 는 두지 않는다(Lead 결정) |
| `RecoveryError` "저널이 체크포인트 … 와 맞지 않는다" | 저널이 fsync 된 기록을 잃었거나(체크포인트가 앞섬) 다른 저널(같은 offset 의 msg_id 가 다름). 마운트 경로·볼륨 착오가 흔한 원인 | 볼륨·경로 설정을 먼저 확인한다. 같은 Tier 0 의 저널이 맞는데 기록이 없어졌으면 저장소 장애로 보고 보존 후 Lead 판단 |
| `RecoveryIncomplete` | 쓸 체크포인트가 없고(모두 손상·형식 불일치) 저널 앞부분이 정리돼 활성 이벤트를 다 알 수 없다 | 보존 후 Lead 판단. EC2 의 이벤트 기록(최종 상태)과 대조해 수동으로 판단한다 |
| `RecoveryError` "저널을 다시 적용하지 못했다" | 저널 기록의 meta.state 손상, payload 와 불일치, 다른 Tier 0·LIVE 아닌 기록 | 보존 후 Lead 판단(저널 손상) |

시작하지만 알려야 하는 것:

- `RecoveryResult.rejected` 가 비어 있지 않음: 버린 체크포인트(손상·부분 쓰기·형식 버전·다른 gateway·정리된 구간)와 이유. 로그·health 에 남기고, 파일은 다음 체크포인트 저장 때 정리된다(필요하면 그 전에 보관 위치로 복사).
- `RecoveryResult.source == JOURNAL`: 체크포인트 없이 저널 전체로 복구했다. config_version 을 체크포인트에서 얻지 못하므로 BundleArchive 복구 결과만으로 판정한다.
- `RecoveryResult.stats` 의 gaps·unknown_updates·duplicates·stale: 저널 이상. 로그로 알린다.

## 실행 중 경고

| 신호 | 의미 | 조치 |
|---|---|---|
| `Checkpointer.stalled()` (기본 5초 동안 성공 없음), `last_error` | 체크포인트를 못 쓴다(볼륨 용량·권한·fsync 지연). 이전 세대는 그대로라 판정은 계속되지만 재시작 때 재적용 구간이 길어지고 저널 정리가 멈춘다 | Network Volume 용량·쓰기 지연을 확인한다. 용량이 차면 저널 정리도 멈추므로(정리 하한이 오르지 않음) 볼륨을 늘린다. 저널·체크포인트를 지워 공간을 만들지 않는다 |
| `JournalWriter.stalled()`·`failed` | 저널 쓰기 정지·실패(eventlog) | eventlog 운영 절차를 따른다 |
| 저널 크기 | 전이 기록에 상태 전체를 함께 남겨 이전 형식보다 커졌다(RAISED 표본 약 1.7배, 확인 이력이 길면 더 큼). 세그먼트 64 MiB, 보관 24시간 | 볼륨 용량 감시에 저널 디렉터리를 포함한다 |

## 남은 일

- 배선(-171): 시작 때 `recover` → `Checkpointer(…, compact=outbox.compact).start()`, 판정 루프의 `update_inputs`, health 발행 경로의 `RecoveryHealthGate.apply`(카메라 시각 상태는 새로 만든다), 복원 전 active_site_alerts 송신 금지, 위 경고를 health·로그로 알림, checkpoint config_version 과 BundleArchive 복구 버전 비교.
- 명세: §3.2 는 "MVP 는 World Track 을 체크포인트하지 않는다"이다. 이번에는 Track 을 저장·꺼내기만 하고 판정·Manager 에 넣지 않는다. 저장 범위는 명세 정리 때 Lead 가 정한다. 위 시작 거부 절차는 §18 에 옮길 후보다.
