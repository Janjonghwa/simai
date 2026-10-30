# 관측 픽셀 UV 텍스처 누적 (S15P31A101-89)

## 범위와 명세

기획서 §20.3 4번(2단계), §5.6·13.2(외형과 캡슐 폴백), §12.5의 `hmr_appearance_update`,
§11.5(외형 자산 보관), §3.2(Tier 0 우선)를 구현한다. `avatar_asset` 1.0.0의
`texture`, `surface_source.ratios/face_ratios`, `face_policy`, 버전 필드를 그대로 쓴다.
I1·계약·front·Tier 0 위치/관절/ID/판정은 바꾸지 않는다. 출력은 안전 판단 권한이 없는 Tier 1 외형이다.

**명세와 작업 지시의 차이:** §20.3은 UV 투영 도구로 PyTorch3D/nvdiffrast를 적었지만,
이번 Owner 지시서는 새 의존성 없이 torch/numpy 래스터화를 명시했다. 그 지시를 따랐다.
또한 지시서의 “EXCLUDED면 mask_uri 등을 비움”은 계약의 조건과 다르다. 정본 계약에 따라
EXCLUDED도 몸 텍스처가 있으면 출처 PNG를 보내고, 얼굴 관측 비율만 0으로 둔다.

## 코드와 데이터 흐름

```text
Tier 0 공유 snapshot + CalibrationProvider + HMR 집계 포즈
  → 기존 체형 고정 PersonRig의 LBS → World 메시 (Tier 0 pelvis에 정렬)
  → 렌즈 왜곡 제거 (색과 사람 마스크를 같은 K로 remap)
  → torch z-buffer (동시각 다른 사람 메시 포함)
  → UV별 가시성/시선각/마스크 검사 → RGB 가중 누적
  → 좁은 보간·얼굴 정책 → PNG 두 장 → AVATAR_ASSET 업로드
  → 새 avatar_asset 버전 알림 → 그 버전의 포즈
```

- `hmr/raster.py`: 원근 보정 깊이와 면 번호, canonical UV의 면·barycentric 대응.
- `hmr/appearance.py`: 사람 한 명의 ID 보호, 가중 누적, 출처 지도·얼굴 정책, 무손실 PNG.
- `hmr/appearance_stream.py`: 기존 루프·보정·입력 연결, 외형 버전 발행과 업로드 재시도.
- `hmr/appearance_report.py`: 선택한 진단 폴더에 버전별 PNG/메타데이터와 전후 비교 그림.
- `hmr/stream.py`, `services/avatar/`: 기본 꺼짐 설정, 자산 송신과 기존 포즈 연결.
- 시험: `tests/hmr/test_appearance.py`, `test_avatar_loop.py`, `tests/services/avatar/test_transport.py`.

MHR UV는 아래 원점이다. `gltf_export.py`와 동일하게 `v_gltf = 1 - v_mhr`를 한 번 적용하고
PNG의 위쪽 행을 v=0으로 둔다. UV 텍셀 중심은 `((x+.5)/N, (y+.5)/N)`, 카메라 픽셀 중심은
기존 OpenCV 보정의 정수 좌표다. 실제 내보낸 TEXCOORD_0와 합성 색 기울기로 방향을 검증한다.
자산 정점·UV 이음매의 대응은 MHR faces/texcoord_faces를 그대로 쓰며 임의 UV를 생성하지 않는다.

LBS는 기존 PersonRig와 부모 기준 관절 회전(모르는 손 관절은 bind)을 쓴다. 집계된 root 회전을 적용하고
관절 1을 Tier 0 pelvis에 맞춘다. pelvis가 없으면 메시 위치를 만들지 않는다. 지지면 Z=0이나
HMR translation으로 보충하지 않으며 뷰어 좌표 변환 `(x,z,-y)`를 여기에서 하지 않는다.

## 수치와 근거

| 항목 | 초기값 | 근거·의미 |
|---|---|---|
| 출력 | 1024×1024 RGB albedo, 단일 채널 uint8 출처 PNG | 요청된 기준 해상도. PNG는 무손실 |
| 카메라 시각 | World 시각 ±40ms, 묶음 내 최대 차이 40ms | 기존 공유 통로 100ms보다 좁혀 움직임에 따른 다른 시각 색 혼합을 줄임. 제품 합격 기준 아님 |
| 시선각 | 법선·시선 내적 ≥0.25 | 약 75.5°보다 비스듬한 표면 배제 |
| 시선 가중치 | 내적² | 정면 관측을 우선하고 측면의 부정확한 투영 영향 완화 |
| 해상도 가중치 | min(투영 삼각형 면적 / UV 텍셀 면적, 1) | 텍셀보다 작은 영상 정보를 과신하지 않음 |
| 마스크 경계 | 거리 <1px 배제, min(거리/4px, 1) | 가림 경계/분할 경계의 색 섞임 완화. bbox 경계도 같은 처리 |
| 이상 관측 | 기존 관측과 RGB 최대 채널 차이 >80/255 거절 | 강한 색 변화/잘못 붙은 사람을 보수적으로 배제. 첫 관측부터 적용하며 조명 변화는 잔여 위험 |
| 보간 | 관측에서 4방향 최대 2텍셀 확장 | 좁은 이음매만 메움. 보간 색·가중치를 관측 누적으로 되먹이지 않음 |
| 얼굴 흐림 | 15×15 Gaussian, sigma=3 | 관측된 얼굴 텍셀만 정규화 합성곱. 미관측 얼굴은 채우지 않음 |
| ID 재개 | 안정된 서로 다른 입력 30프레임 | 명세 초기값. 중복 시각은 안정 횟수에 포함하지 않음 |
| 누적 간격 | 회차 완료 뒤 최소 2000ms | 포즈 루프의 여유 확보. 느린 CPU에서도 매 프레임 누적으로 고착되지 않음 |
| 발행 | 새 관측이 있고, 관측 비율 +0.02 또는 마지막 발행 뒤 30000ms | 2%p 이상 개선 때 먼저 전송하고 색만 개선된 경우는 30초로 제한. 최초 관측은 바로 발행 |
| 래스터 임시 조각 | 262144 후보/묶음 | 큰 삼각형도 임시 텐서가 무제한 커지지 않게 함 |

가중치는 세 가중치의 곱이다. `T_new=(W_old*T_old+w*C_obs)/(W_old+w)`를 쓰고 보간은
W를 증가시키지 않는다. 카메라 순서는 ID 순으로 고정한다. 색·가중치 사본에서 계산하여
도중 admission 차단이면 그 회차 전체를 버린다. numpy/torch CPU와 CUDA가 같은 식을 쓴다.
CUDA 설정에서는 투영·법선·색 누적과 래스터화를 GPU에서 계산하고, 보정 remap·PNG는 CPU다.

z-buffer는 면 단위 최근접 깊이를 비교하며 타인의 메시를 같은 버퍼에 포함한다.
near plane을 가로지르는 메시가 있으면 그 카메라 관측을 통째로 제외한다(잘린 가림막 뒤 색을 채우지 않음).
아직 메시가 없는 사람이나 불확실 ID의 bbox는 보수적 가림 마스크로 뺀다.
다른 사람의 메시 대표 카메라도 시각 허용 범위를 통과해야 가림막으로 쓴다.
렌즈 왜곡은 저장소의 정방향/왕복 유효성 검사와 remap을 사용한다(NONE·PINHOLE·FISHEYE).
보정별 remap 캐시는 최대 4대이며 보정이 바뀌면 새 키를 쓴다.

얼굴 면은 MHR 정점·면 번호 JSON을 지정하거나, head/face/jaw/eye 이름 관절과 그 자식의
스킨 영향 ≥0.1 정점에 닿은 면으로 정한다. 자동 영역은 머리 전체를 포함하는 보수적 영역이다.
둘 다 알 수 없으면 기동을 실패시킨다. 얼굴을 모른 채 몸으로 취급해 보간하지 않는다.
정밀 얼굴 영역 파일은 `{asset_sha256, vertex_indices: [...], face_indices: [...]}`이며 자산 해시를 검사한다.
실자산의 얼굴 경계 확인은 9차 세션 필수 항목이다.

출처 값은 0/1/2, 얼굴은 128/130뿐이다(129 없음). `ratios`의 분모는 UV가 있는 표면 텍셀,
`face_ratios`의 분모는 얼굴 텍셀이다. atlas 빈 공간은 비율에서 제외한다.
EXCLUDED는 얼굴 누적 자체를 막고 모두 128로 둔다. BLURRED도 관측 텍셀만 130이다.

## ID와 버전

불확실 ID에서는 누적·발행을 멈추고 확정 자산을 유지한다. revision 변경 시 미확정 PNG와
누적 버퍼는 폐기하고 새 체형/외형을 30개의 안정 프레임 뒤 시작한다. 같은 revision의 일시 불확실은
기존 확정 누적을 유지한 채 30프레임을 다시 기다린다. 분할된 새 ID는 별도 상태이며 상속하지 않는다.

처음에는 기존처럼 텍스처 없는 GLB 버전 0을 올린다. 이후 PNG 두 장을 모두 올린 뒤 새 버전을
발행한다. 체형 GLB는 기존 불변 URI를 참조하고 PNG만 `vN/albedo.png`, `vN/surface_source.png`로 올린다.
서버의 기존 `file_name`, `content_type=image/png`, `purpose=AVATAR_ASSET`을 쓴다.
같은 버전 업로드 재시도는 PNG 바이트·메타데이터를 고정한다. 취소한 외형 업로드의 버전 번호는
재사용하지 않고 `previous_appearance_version`은 마지막 확정 버전을 가리킨다.
업로드 실패여도 마지막 확정 자산의 포즈 전송은 계속 시도한다.

`AvatarLoop.reannounce_appearance(world_id, version)`은 확정 이력의 자산을 그대로 다시 알리고
후속 포즈가 그 버전을 가리키게 한다. 다음 신규 버전의 번호는 되돌리지 않는다. 자동 갱신이 켜져 있으면
다음 발행 조건에서 다시 새 외형이 나올 수 있다. 영구 되돌리기는 외형 설정을 끄고 서비스를 재기동한다.
이는 새 stream_epoch이며, 이전 버전 파일·DB 기록은 지우거나 덮어쓰지 않는다.

## 9차 GPU 세션 실행과 확인

코드·의존성 추가 설치 없이 기존 통합 이미지에서 avatar를 선택하고 다음 환경을 주입한다.
비밀값은 기존 주입 경로를 유지한다. 여기에는 실제 비밀값을 기록하지 않는다.

```sh
# 기본 auto(CUDA 면 켜짐). 강제로 켜려면 SIMAI_AVATAR_APPEARANCE=1
# device 기본은 CUDA 가 있으면 cuda
export SIMAI_AVATAR_TEXTURE_SIZE=1024
export SIMAI_AVATAR_FACE_POLICY=OBSERVED
export SIMAI_AVATAR_APPEARANCE_FREEZE_FRAMES=30
export SIMAI_AVATAR_TEXTURE_INTERVAL_MS=2000
export SIMAI_AVATAR_TEXTURE_PUBLISH_MS=30000
export SIMAI_AVATAR_TEXTURE_OUTPUT_DIR="$SIMAI_ROOT/runs/20261030-s9/appearance"
# 검증한 얼굴 번호 파일이 있으면 SIMAI_AVATAR_FACE_REGION으로 지정한다.
```

기본 `SIMAI_AVATAR_APPEARANCE=auto`(2026-10-30 사용자 결정): CUDA 가 있으면 켜고 device 도 cuda 로 둔다.
CUDA 가 없으면(노트북·CI) 끄고 종전 GLB·대표색 경로를 유지한다. CPU 누적은 1인당 3.5초라 기본으로 켜지 않는다.
auto 로 켜졌는데 얼굴 영역을 정하지 못하면 경고를 남기고 누적만 끈다(아바타 루프는 계속). `1` 은 CPU 에서도 강제로 켜고
얼굴 영역 오류에서 멈추며, `0` 은 끈다. GPU 상한은 기존 avatar 프로세스의
메모리 제한 안에 포함한다. 별도 모델·GPU 프로세스·I5 배치 작업을 띄우지 않는다.

1. Tier 0 지연 감시가 허용하고 30샘플 체형 자산이 나온 뒤 외형 로그를 확인한다.
   `외형 누적 world_id=... device=cuda wall_ms=... cpu_ms=... gpu_ms=... accepted=... rejected=...`.
   cpu_ms는 process CPU 시간, wall_ms는 회차 전체 경과 시간, gpu_ms는 CUDA event로 잰
   준비/카메라 계산 구간들의 GPU 스트림 경과 시간(호스트 대기 포함, 순수 커널 합이 아님)이다.
   CPU에서는 gpu_ms=NA다. PNG/업로드 시간은 누적 시간 밖이므로 포즈 신선도/로그와 함께 본다.
2. `아바타 PNG 업로드 성공` 두 줄과 `외형 버전 확정 ... version=N observed=...`를 확인한다.
   객체 위치는 `s3://<bucket>/avatars/<world_id>/e<epoch>/vN/{albedo,surface_source}.png`다.
   GLB와 PNG URI가 서로 다르고 EXCLUDED에서도 몸 출처 PNG가 유지되는지 확인한다.
3. 로컬 진단 위치는 `$SIMAI_AVATAR_TEXTURE_OUTPUT_DIR/<world_id>/e<epoch>/vN/`의
   `albedo.png`, `surface_source.png`, `avatar_asset.json`이다. 진단 저장은 선택이며 실패해도 스트림은 계속된다.
   기존 파일의 내용이 다르면 덮어쓰지 않는다. 결과는 git에 넣지 않고 인스턴스를 지우기 전에 회수한다.
4. 한 사람이 앞/옆/뒤로 돌며 다른 카메라만 새 면을 보는 구간을 녹화한다. 처음 확정 버전과 나중 버전으로
   같은 atlas의 가려졌던 면이 새로 관측되는지, 얼굴 미관측이 남는지 확인한다. 다음 명령은
   두 albedo와 두 출처 지도를 같은 크기로 놓은 비교 PNG를 만든다(초록 관측·노랑 보간·회색 미관측·분홍 얼굴 테두리).

```sh
uv run --offline python -m simai_ai.hmr.appearance_report \
  "$SIMAI_AVATAR_TEXTURE_OUTPUT_DIR/<world_id>/e<epoch>/v1" \
  "$SIMAI_AVATAR_TEXTURE_OUTPUT_DIR/<world_id>/e<epoch>/v3" \
  --output "$SIMAI_ROOT/runs/20261030-s9/appearance-before-after.png"
```

5. 사람이 앞을 가리는 구간, ID 불확실/교정, 분할 ID, 감시 차단에서 색 혼합/누적 재개/새 버전/캡슐을 확인한다.
   전후 atlas 비교는 관측 증가 증거일 뿐이다. 같은 가린 카메라 시점의 3D 표시 품질은 별도 front 텍스처
   그리기 티켓이 연결된 뷰어에서 판정한다. CPU 합성 수치를 제품 성능으로 쓰지 않는다.
6. 실제 인원·카메라 수에서 Tier 0 지연과 avatar 포즈 500ms 신선도, 전체 VRAM, 누적/PNG/전송 비용을 측정한다.
   기준 미달이면 2단계는 HOLD하고 설정을 끈 1단계로 시연한다. 세션 실행자는 기존
   `contracts/experiments/run_record.schema.json` Run 기록을 남기고 종료 뒤 GPU를 Stop한다.

## 로컬 검증과 측정

합성 상자의 해석적 광선-평면 렌더러는 누적 래스터화와 독립이다. 2·4대 카메라의 알려진 RGB 기울기를
복원하고 관측 텍셀 평균 절대 오차 <2/255를 확인한다. 한 카메라에만 보이는 면, 미관측 바닥,
다른 메시 가림, 화면 밖·마스크, PINHOLE/FISHEYE, 이상 색 거절과 가중 평균, ID 보호,
세 얼굴 정책, 2텍셀 보간 상한, 버전/PNG 재시도, 계약 검증, 진단 파일·비교 그림을 검사한다.

2026-10-30 Windows 로컬 CPU, torch 2.11.0+cpu, torch 스레드 1, 상자 24정점/12면,
1024² atlas와 128² 합성 영상 4대, 연속 세 회차의 최종 구현 측정:

| 회차 | wall_ms | cpu_ms | gpu_ms |
|---|---:|---:|---|
| 1 | 3528.925 | 3468.750 | NA |
| 2 | 3769.263 | 3734.375 | NA |
| 3 | 3477.887 | 3281.250 | NA |

최초 UV 대응표 준비 897.626ms, 보간·두 PNG 생성 474.257ms였다. 관측 0.640631,
보간 0.007042, 미관측 0.352327, 얼굴 관측 0.953062였다. 이 측정은 합성 단위 경로의
노트북 CPU 비용이며 EXP Run/실자산 성능 측정이 아니다. 실제 MHR·1080p·CUDA·다인 지연을 추정하는 근거로 쓰지 않는다.
재현은 `tests.hmr.test_appearance`의 `accumulator(resolution=1024, seam=2)`,
카메라 (3,2,1), (-3,2,1), (-3,-2,1), (3,-2,1), t=1000/1001/1002와 동일 시각 view를 사용한다.

`ai/`에서 `UV_CACHE_DIR=.uv-cache`, `PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`을 주고 실행했다.
pytest 임시 폴더는 모두 worktree 안으로 지정했으며 종료 뒤 삭제했다.

- `uv run --offline pytest tests/hmr -q -p no:cacheprovider --basetemp=.pytest-tmp-89` → 78 passed, 3 skipped(실제 MHR 자산 미지정).
- `uv run --offline pytest tests/services/avatar -q -p no:cacheprovider --basetemp=.pytest-tmp-89-services` → 13 passed.
- 마지막 bbox 경계 처리 보완 후 `uv run --offline pytest tests/hmr/test_appearance.py -q -p no:cacheprovider --basetemp=.pytest-tmp-89-final` → 18 passed.
- `uv run --offline lint-imports` → 6 kept, 0 broken. `git diff --check` 통과.
- 리뷰 생략(사용자 지시). 전체 pytest·실제 GPU/HMR·EC2/S3 실행은 하지 않았다. 계약 예시는 변경하지 않았다.

## 버린 대안과 남은 위험

- PyTorch3D/nvdiffrast: 지시서의 새 의존성 금지, CUDA 빌드/이미지 변경 비용 때문에 제외.
  UV 텍스처·마스크를 GLB에 삽입하는 방식도 별도 texture 계약·front 티켓과 중복되어 제외했다.
- 생성형 얼굴/옷 복원·Re-ID·Tier 0 값 교정은 도입하지 않는다. 첫 관측이 잘못되면 색 거절이 그 오류를
  고착시킬 수 있다. 조명/자동 노출·움직이는 옷·MHR 포즈 오차·얇은 면·UV 중첩은 실제 자료 확인이 필요하다.
- bbox만 있을 때 사람 경계 밖 배경이 메시 안에 투영될 수 있다. 타인 bbox 제외는 안전 쪽으로 관측을 줄이지만
  가려진 사람이 역으로 앞 사람의 관측까지 막을 수 있다. 정밀 인물 마스크 연결은 상류 제공 시 사용한다.
- 자동 얼굴 영역은 머리 전체여서 얼굴 비율이 정밀 얼굴 면적 비율은 아니다. 검증된 MHR 얼굴 번호로 좁혀야 한다.
- CPU 1024 누적은 LIVE 포즈 간격에 비해 느리다. CUDA는 계산 경로만 준비했고 이 노트북에서는 실행하지 않았다.
  PNG·네트워크 업로드도 포즈를 지연시킬 수 있으며 기존 500ms 검사로 오래된 포즈는 버린다.
  이미 시작한 CUDA 조각은 중간 취소할 수 없지만 다음 래스터 묶음/카메라/발행 단계에서 감시를 다시 확인한다.
- front 텍스처/출처 지도 렌더링은 별도 티켓이다. 실제 EC2/S3 업로드, 라이선스 MHR 얼굴 UV 방향,
  가린 시점의 3D 품질과 GPU 상한 통합은 9차 세션/Lead 확인으로 넘긴다.

## S15P31A101-361 — 관측 품질 가중과 카메라 색 맞춤

관련 명세는 §5.6·20.3(관측 외형)·3.2(Tier 0 우선)다. 기존 계약과 얼굴/출처/ID/버전 규칙은 그대로 따른다.
`appearance_quality.py`는 품질 계산과 카메라 RGB 상태를 맡고, `appearance.py`는 같은 시각의 카메라 관측을
먼저 모은 뒤 색 이득을 추정하고 누적한다. `appearance_stream.py`는 Tier 0의 COCO17 2D 관절과
대표 HMR의 World COCO17을 연결하고, 모든 사람의 누적기에 하나의 `CameraColors`를 공유한다.
공통 텍셀은 반드시 **같은 사람·같은 회차** 안에서만 비교한다. 다른 사람의 색을 서로 대응시키지 않는다.

### 기본값과 근거

모두 `AppearanceConfig` 필드이며 네 `*_enabled`의 기본값은 True다. 수치는 제품 합격 기준이 아닌
9차 세션 전 합성 검증용 초기값이다. 픽셀 문턱은 atlas 해상도가 아닌 **원본 영상 해상도의 이상적 K 픽셀**이다.
왜곡 제거 후에도 K와 영상 크기를 유지하며 축소하지 않는다. 강한 렌즈 왜곡의 원본 거리와는 차이가 날 수 있다.

| 장치 / 설정 | 기본값 | 식과 근거 |
|---|---|---|
| `silhouette_enabled`, `silhouette_erode_px`, `silhouette_fade_px` | True, 3px, 3px | z-buffer에서 자기 면 번호만 1로 만든 실루엣의 정확한 L2 거리 d. `clip((d-3)/3,0,1)`로 3px 이내를 버리고 3~6px를 서서히 받아들인다. 기존 bbox/마스크 4px 감쇠와 곱한다. 1~2px 포즈/래스터 경계 오차에 여유를 주며 얇은 부위의 관측 감소는 감수한다. 내부 삼각형 모서리는 경계가 아니다. 타인 가림/영상 모서리도 경계로 센다 |
| `color_enabled`, `color_min_quality`, `color_min_common` | True, 0.25, 64텍셀 | 시선각²×bbox 경계×실루엣×흐림×맞춤 가중이 0.25 이상인 공통 텍셀만 사용. 투영/UV 면적은 atlas 크기에 따라 작아지므로 색 추정의 품질 문턱에서는 제외하되 실제 누적에는 곱한다. 64개 미만이면 갱신하지 않는다 |
| `color_min_rgb`, `color_max_rgb` | 10, 245 | 두 카메라의 모든 RGB 채널이 이 범위일 때만 로그 비에 사용. 암부의 잡음 증폭과 포화 채널의 잘못된 비율을 피한다 |
| `color_ema`, `color_gain_min`, `color_gain_max` | 0.2, 0.7, 1.4 | 현재 고품질 텍셀이 가장 많은 카메라가 기준(동률 ID 순). `target=clip(g_ref×exp(median(log(C_ref)-log(C_cam))),0.7,1.4)`, `g=clip(0.8g+0.2target,0.7,1.4)`. 기준의 기존 이득은 유지해 기준 전환 시 색 기준의 연속성을 보존한다. 최다 관측 기준은 공통 표면과 연결 가능성을 높이고, 전체 평균보다 카메라별 비공통 표면 색에 덜 영향받는다. 한 카메라뿐이거나 공통 부족이면 기존 이득(초기 1)을 유지한다 |
| `blur_enabled`, `blur_history`, `blur_min_pixels`, `blur_floor` | True, 15표본, 64픽셀, 0.1 | 자기 실루엣∩사람 영역 내부의 회색조 Laplacian(3×3 이웃, ksize=1) 분산 V. 경계 1px를 더 제외해 배경 대비를 세지 않는다. `clip(V/median(직전 15표본),0.1,1)`. 첫 관측/기준 분산 0이면 1, 내부 픽셀 부족이면 0.1. 카메라·사람별 이력으로 다른 사람 옷의 무늬를 비교하지 않는다. 기본 2초 간격에서 최근 약 30초에 대응 |
| `fit_enabled`, `fit_min_confidence`, `fit_min_joints` | True, 0.3, 6관절 | `observed_coco17`으로 Tier 0 원본 픽셀을 왜곡 제거한 뒤 대표 HMR World COCO17의 `reprojection_px` 유효 오차 중앙값 e 사용. COCO17 이외 레이아웃은 비교하지 않는다 |
| `fit_full_px`, `fit_reject_px`, `fit_sparse_weight` | 8px, 40px, 0.5 | e≤8은 1, 8~40은 선형 감쇠, e≥40은 카메라 관측 전체 배제. 8px는 작은 키포인트 흔들림을 허용하고 40px는 명백한 어긋남을 배제하는 초기 문턱. 관절 6개 미만/미제공이면 오차를 단정하지 않고 0.5. 모르는 관절을 채우지 않는다 |

새 관측 RGB에는 이득을 곱하고 0~255로 제한한 **뒤** 기존 `outlier_rgb` 거절과 가중 평균을 적용한다.
최종 가중은 기존 시선각²×면적×bbox 경계에 실루엣·흐림·맞춤 가중을 곱한 값이다.
과거 누적 색을 재작성하지 않으므로 이득 수렴 뒤에도 초기 색의 영향은 새 관측에 따라 서서히 줄어든다.
색·가중치뿐 아니라 이득·선명도 이력도 회차 성공 뒤에만 반영한다. 지연 감시 차단 시 전부 버린다.
카메라 이득은 사람의 revision/삭제와 무관하게 유지하지만 보정 버전 변경/프로세스 재시작 때 초기화한다.
선명도 이력은 사람 revision 변경 시에도 초기화한다. 공유 이득은 같은 입력 카메라 보정 객체를 키로 쓴다.

9차 세션의 개별 A/B와 즉시 해제를 위해 다음 네 스위치만 환경에 추가한다(각각 `0`/`1`, 기본 `1`).
세부 문턱은 `AppearanceConfig`로만 조절한다. 기존 `SIMAI_AVATAR_APPEARANCE=auto`는 그대로이며 CPU에서 기본 누적은 꺼진다.

```text
SIMAI_AVATAR_TEXTURE_SILHOUETTE
SIMAI_AVATAR_TEXTURE_COLOR
SIMAI_AVATAR_TEXTURE_BLUR
SIMAI_AVATAR_TEXTURE_FIT
```

### 합성 검증과 CPU 비용

`tests/hmr/test_appearance_quality.py`에서 확대 메시+bbox만 있는 장면의 가중 배경색 오차가
장치 해제 대비 20% 미만으로 줄고 관측 텍셀은 절반 이상 유지되는지 확인한다. RGB 이음매 차이는 30회 누적 뒤
해제 대비 20% 미만, 보정된 카메라 RGB 차이는 처음의 2% 미만, 이득은 0.7~1.4 안으로 수렴한다.
또한 20% 이상 색 표본 오염의 강건성·기준 카메라 전환·최소 공통 수·범위 양끝을 확인한다.
합성 checkerboard를 Gaussian blur한 관측은 직전 선명 관측의 20% 미만 가중으로 기여한다.
50px 관절 오차는 제외되고 24px는 0.5, 3관절만 있으면 0.5다. 실제 pipeline 연결·환경별 독립 해제·
공유 이득·보정 초기화·admission 차단 시 상태 보존도 시험한다.

2026-10-30 Windows 로컬 CPU(torch 2.11.0+cpu, OpenCV 5.0.0, torch/OpenCV 스레드 각각 1),
상자 24정점/12면, 1024² atlas, 128² 합성 영상 4대, 정확한 합성 COCO17을 사용했다.
네 장치 해제/기본 활성 누적기를 각각 한 회 워밍업하고 순서를 번갈아 세 번 쟀다. UV 준비·PNG·업로드는 제외한다.
해제 기준선은 **동일 구현에서 네 스위치를 끈 경우**이며 이전 커밋의 실행 시간과 직접 비교한 값이 아니다.

| 회차 | 모두 해제 wall / CPU ms | 기본 활성 wall / CPU ms |
|---|---:|---:|
| 1 | 3859.733 / 3781.250 | 4036.384 / 3984.375 |
| 2 | 3823.380 / 3781.250 | 4235.481 / 4171.875 |
| 3 | 3665.640 / 3656.250 | 4092.985 / 4015.625 |

1인당 wall 중앙값은 **3823.380 → 4092.985ms, +269.605ms(+7.05%)**다.
이 측정은 합성 단위 경로의 노트북 비용으로 EXP Run/제품 성능이 아니다. CUDA·1080p·실제 MHR·다인 비용을 예측하지 않는다.
재현 스크립트는 로컬 Owner 산출물 `.agy/benchmark.py`다. 4대 위치는 (2,3,1), (-2,3,1), (-2,-3,1), (2,-3,1)이며,
기존 합성 `cube`/`render`, `project_ideal`로 17관절을 만들고 회차별 시각을 1000~1003ms로 맞춘다.

### 버린 대안·9차 세션 확인·남은 위험

- 분할 모델 추가는 GPU와 Tier 0 출력 변경이 필요하므로 미뤘다. 이번에는 이미 계산한 z-buffer의 자기 면 번호만 사용한다.
- 관절 이동량/시간으로 흐림을 추정하는 대안은 불규칙한 샘플 간격·가림·추정 관절 흔들림 때문에 실제 영상 흐림과 달라질 수 있다.
  관측 영상에서 직접 계산하고 추가 HMR 추론이 없는 라플라시안 분산을 기본으로 선택했다. 둘을 중복 감쇠하지 않는다.
- 색은 채널 이득만 보정한다. 카메라별 gamma·반사·국소 조명·비공통 표면만 있는 상황은 해결하지 못한다.
  같은 시각 충분한 공통 표면이 없으면 이득은 갱신되지 않는다. UV의 이웃 텍셀이 같은 영상 픽셀을 반복할 수 있어
  64 공통 텍셀이 독립 영상 표본 64개를 의미하지는 않는다.
- 실루엣 침식은 작은 메시 오차만 완화한다. 메시가 크게 틀리거나 얇은 팔·손이면 배경 오염이 남거나 관측을 잃는다.
  관절 오차는 피부/옷 표면 정합의 보증이 아니다. 관절이 적은 카메라는 배제 대신 감쇠만 한다.
- 무늬 없는 옷의 분산 0에서는 흐림을 식별할 수 없다. 지속적인 흐림은 최근 중앙값에도 들어가 적응될 수 있고,
  첫 프레임이 흐리면 선명 기준을 알 수 없다. 얼굴·다른 미관측 영역을 새로 만들지 않는다.
- 9차 세션은 기존 `외형 누적 ... wall_ms/cpu_ms/gpu_ms/accepted/rejected`와 함께
  `외형 품질 world_id=... camera=... fit=... error_px=... joints=... blur=... sharpness=...`,
  `외형 색 이득 camera=... rgb=[...]`를 본다. 이득이 경계값에 붙는지, fit=0으로 제외된 시점과
  blur 감소가 실제 영상에 대응하는지, PNG의 경계 번짐/이음매가 줄었는지 네 스위치 A/B로 비교한다.
  GPU에는 카메라 관측별 텐서를 모아두는 추가 메모리와 CPU 거리/색 통계 왕복 비용이 있다.
  실제 MHR·CUDA VRAM·Tier 0 지연·0.5초 포즈 신선도 검증은 수행하지 않았으며 Lead/9차 세션으로 넘긴다.
