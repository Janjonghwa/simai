# HMR 다시점 집계와 Tier 0 root 정렬 (S15P31A101-87, EXP-02 E-GATE-1 1단계)

카메라별 Fast SAM 3D Body 결과를 사람(World ID)별 대표 포즈 하나로 묶고, 위치는 Tier 0 `pelvis_xyz` 에 고정하는 라이브러리와,
그것을 평가하는 E-GATE-1 경로(입력 준비 → GPU Job → 노트북 평가)다. 이번 단계는 GPU 없이 노트북에서 끝나는 부분이고,
E-GATE-1 판정은 GPU Run 뒤에 한다. -227 hmr_refine 워커(이연주)가 집계·프롬프트 함수를 그대로 부른다.

## 관련 명세

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 5.2 | root 위치 정본은 Tier 0, HMR 이 덮지 않음. 같은 카메라 두 검출을 한 World ID 에 묶지 않음 | 연결은 카메라당 하나, 위치는 `pelvis_xyz` 그대로 |
| 5.6 | mask 프롬프트(YOLO26-seg), keypoint 프롬프트는 몸통·고관절만. 내장 다시점 융합을 먼저 시험, 안 되면 카메라당 1개 클러스터링 → 대표 포즈의 translation 만 다시점 재투영으로 보정, 최종 root 는 5.2 | '내장 다시점 융합', '연결', '대표 포즈', '프롬프트' |
| 15.1 G5 | 한 카메라 제외 재투영 p95 8 px, 발-지지면 5 cm | E-GATE-1 지표(`docs/experiments/EXP-02_design.md` 'E-GATE-1') |
| 20.3·20.4 | 다인·교차·가림·재등장, Local Track ID 를 정체성 정본으로 쓰지 않음, 지지면 모르면 Z=0 가정 금지, 아바타 구동은 root·접지 Tier 0 | 상황 표지, 재등장 반례, 접지 null 유지 |
| 20.6 | E-GATE-1: 실패 사례 포함 정량 평가 | 이 문서의 경로 |

AGENTS 규칙 1·2·7: 결과 관절은 모두 inferred, 위험 판정에 쓰지 않음, Tier 0 pelvis·접지를 모르면 비운다. 얼굴 재식별 없음.

## 내장 다시점 융합(5.6 "먼저 시험")

Fast-SAM-3D-Body @d72aa36 의 다시점 융합은 `mocap/core/multiview_mhr2smpl.py`(`MultiViewFusionRunner`)와 `run_multiview_publisher.py` 다.
코드를 읽어(노트북, 선행 PoC 사본) 우리 경로에 쓸 수 없다고 판단했다. GPU 시험은 하지 않았다(아래 이유가 출력 형식이라 실행 결과와 무관).

- 출력이 SMPL(몸 자세 21관절 축각·체형 10·루트 상대 24관절)이다. 아바타 스트림·glTF 는 MHR 127관절 순서(-88·-249 계약)라 바로 쓸 수 없다.
- 카메라마다 가장 큰 bbox 한 사람만 넣는다(`pick_main_bbox`). 다인 교실에 맞지 않고, World ID 와의 연결이 없다.
- 카메라 외부 파라미터를 쓰지 않고 각 시점 메시를 중심화해 학습 MLP 에 넣는다. 위치를 내지 않는다(root 는 어차피 Tier 0).
- 별도 학습 가중치(`best_model.pth`·`sample_idx.npy`)와 SMPL 모델 파일·MHR→SMPL 대응표가 필요하다. 묶음 `exp02` 가중치에 없다.

그래서 5.6 의 대안(카메라당 1개 클러스터링 → 대표 포즈, translation 만 다시점 보정, root 는 Tier 0)을 만들었다.

## 코드

| 파일 | 내용 |
|---|---|
| `ai/src/simai_ai/hmr/multiview.py` | 연결(`associate`), 사람별 대표 포즈(`aggregate_person`), 한 시각 묶음(`aggregate_frame`), 오프라인 기록(`pose_frame_record`) |
| `ai/src/simai_ai/hmr/prompt.py` | 프롬프트 구성(`build_prompts`), 원본 → HMR 그림 좌표(`HmrImageMap`), keypoint 프롬프트 배열(`keypoint_prompt_array`) |
| `ai/experiments/hmr/egate1_frames.py` | 녹화 → 같은 시각 카메라별 그림(노트북 CPU, PyAV) |
| `ai/experiments/hmr/run_multiview_hmr.py` | GPU Job: 그림 → 카메라별 HMR 결과 묶음(통합 이미지 HMR 경로) |
| `ai/experiments/hmr/egate1_bundle.py` | 결과 묶음 형식(쓰기·읽기) |
| `ai/experiments/hmr/egate1_jobs.py` | GPU 큐 항목 생성기(독립, 4차 세션 큐에는 Lead 가 끼움) |
| `ai/experiments/hmr/egate1_eval.py` | 노트북 평가: 묶음 + world_tracks → 지표·`pose_frames.jsonl`·Run 기록 |
| 시험 | `ai/tests/hmr/test_multiview.py`·`test_prompt.py`(합성 장면 `multiview_scene.py`), `ai/tests/experiments/hmr/test_egate1*.py`(Panoptic 은 marker `dataset`) |

## -227 이 부르는 법

```python
from simai_ai.geometry.rig import CameraRig
from simai_ai.hmr.multiview import HmrCandidate, aggregate_frame, pose_frame_record
from simai_ai.hmr.pose import pose_from_hmr          # body 모드. full 모드면 pose_from_model_params (mhr-skinned-gltf.md)
from simai_ai.hmr.prompt import HmrImageMap, build_prompts, observations_from_world_tracks

rig = CameraRig.from_provider(calibration_provider)   # world_state 와 같은 calibration_version
# 카메라마다: Tier 0 관측 → 프롬프트 → HMR
obs = observations_from_world_tracks(doc, cam_id)      # doc = 같은 시각 world_tracks(=world_state 트랙 + 카메라별 bbox·keypoint)
prompts, skipped = build_prompts(obs, seg_instances, HmrImageMap(rig.cameras[cam_id], K_used))
#   ... process_one_image(bboxes=[p.bbox], masks=[p.mask]) — 마스크 있는 사람과 없는 사람은 따로 부른다
pose = pose_from_hmr(out["pred_global_rots"], rig_parents, hands_estimated=False)
cand = HmrCandidate(camera_id=cam_id, candidate_id=..., bbox=p.observation.bbox,           # 원본 픽셀
                    keypoints_cam=out["pred_keypoints_3d"][:70] + out["pred_cam_t"],         # 카메라 좌표 m
                    camera_from_rig=pose.camera_from_rig, joint_rotations=pose.joint_rotations,
                    score=p.observation.score, prompt_world_id=p.observation.world_id,
                    prompt_identity_revision=p.observation.identity_revision)
agg = aggregate_frame(all_candidates, doc, rig)       # 사람마다 PersonAggregate, 버린 후보는 agg.rejected
for person in agg.people:
    person.root_rotation, person.joint_rotations      # → avatar_pose_batch mhr_pose (위치 없음)
    person.identity_revision                          # 계산에 쓴 Tier 0 차수 그대로 포즈에 싣는다
```

가정(어기면 결과가 틀리거나 함수가 거절한다):

- `doc` 와 `rig` 의 `calibration_version` 이 같다(다르면 ValueError, 섞지 않음). doc 은 World(m, +Z) 좌표다.
- `keypoints_cam` 은 MHR70 순서이고 HMR 이 넣은 그림의 카메라 좌표다. 왜곡을 편 그림(K_used, `fs3db_common.prepare_image` alpha=1)은
  카메라 회전이 같으므로 보정 R 로 World 방향을 만든다. COCO17 에 대응하는 관절(`COCO17_TO_MHR70`)이 유한하지 않으면 그 후보는 버린다.
- `bbox` 는 원본 픽셀이다(Tier 0 per_camera bbox 와 비교). HMR 그림 좌표 bbox(`HmrPrompt.bbox`)를 넣지 않는다.
- 상태가 없다. 시간 평활화·체형 고정(30 프레임)·`appearance_version` 은 -227 몫이다. 같은 입력이면 결과가 같다.
- 위험 판정·I1 로 보내지 않는다. 결과는 아바타 스트림(관절 회전)과 오프라인 기록(`human_mesh_output.pose_frame`)에만 쓴다.

## 연결 (카메라당 최대 1개)

1. 후보마다 같은 카메라의 Tier 0 관측(`per_camera`)과 bbox IoU ≥ 0.5 인 World ID 만 짝 후보다. Tier 0 가 그 카메라에서 그 사람을
   보지 않았으면 붙이지 않는다(HMR 이 정체성을 정하지 않는다, 20.3).
2. Tier 0 pelvis 가 있으면 두 기하 게이트를 더 본다: 후보 엉덩이 중점의 투영이 pelvis 투영과 bbox 높이 × 0.5 안, 카메라 깊이 비가
   1/1.5 ~ 1.5 안. 앞 사람 bbox 로 뒤 사람을 복원한 경우(같은 시선 위 앞뒤 포함)를 막는다. 단안 깊이는 부정확해 넉넉한 비만 본다.
3. 프롬프트 World ID 가 있으면 그 ID 에만, 그 ID 가 이 문서에 없으면 버리고(재등장으로 새 ID), `identity_revision` 이 다르면 버린다(교정 뒤 낡은 결과).
4. IoU 큰 순서로 탐욕 배정: 한 후보는 한 ID, 한 ID 는 카메라마다 한 후보. 진 후보는 `CAMERA_SLOT_TAKEN`, 짝이 없으면 `NO_TRACK_MATCH`.

## 대표 포즈

- 후보마다 상대 관절 = R_wc · (관절 − 엉덩이 중점). 위치가 없다.
- Tier 0 pelvis 가 있으면 상대 관절 + pelvis 를 클러스터의 **다른** 카메라 Tier 0 2D keypoint(COCO17, 신뢰도 ≥ 0.3)에 재투영해
  카메라별 중앙값의 중앙값이 가장 작은 후보를 고른다(`CROSS_VIEW`). 몸 방향이 뒤집힌 카메라·잘못 복원한 카메라가 빠진다.
- 그것이 안 되면(pelvis null 이거나 다른 카메라 keypoint 없음) 3개 이상은 상대 관절 거리 합이 가장 작은 후보(`MEDOID`), 2개는 검출 점수(`SCORE`), 1개는 그대로(`SINGLE`).
- 관절 회전은 대표 후보 하나의 것을 그대로 쓴다. `root_rotation` = R_wc · camera_from_rig(`pose.world_root_rotation`).
- 진단 `hmr_root_offset_m`: 대표 상대 관절을 고정하고 위치 T 만 다시점 Tier 0 2D keypoint 로 선형 최소제곱(카메라 2대·점 6개 이상)한 뒤 |T − pelvis|.
  root 로 쓰지 않는다. pelvis 가 null 이면 비운다.

## 프롬프트

- bbox: 원본 bbox 의 변을 따라 찍은 점(변마다 9개)을 HMR 그림(K_used · 왜곡 제거 정규화)으로 옮긴 외접 사각형. 옮길 수 없으면 그 사람은 건너뛴다.
- 마스크: HMR 그림에서 YOLO26-seg 사람 인스턴스와 박스 IoU ≥ 0.5, 큰 순서로 한 사람 한 인스턴스. 없으면 bbox 만.
- keypoint: COCO 어깨 5·6, 엉덩이 11·12(MHR70 5·6·9·10), 신뢰도 ≥ 0.5, HMR 그림 bbox 안. 모델 프롬프트 모양은 [u, v, MHR70 번호], 빈 칸 [0, 0, −2].
- 공식 `process_one_image` 는 keypoint 프롬프트를 받지 않는다. GPU Job 의 `--prompt mask_kp` 는 body 모드 추론 뒤 모델의
  `run_keypoint_prompt` 를 공식 손목 프롬프트와 같은 방식(`_full_to_crop` → [0, 1], crop 밖은 빈 칸)으로 한 번 더 부른다. 모델 내부 호출이라 GPU 미검증이다.

## 결과 묶음 (`simai.egate1_hmr_bundle` 0.1.0)

`<out>/hmr_frames.jsonl`(그림마다 한 줄: instant·camera_id·촬영 시각·frame_ref·image_size·K_used·undistorted·hmr_ms·사람 목록(원본 bbox·
점수·COCO17 keypoint·프롬프트)), `<out>/arrays/i<시각>_<카메라>.npz`(사람별 keypoints_cam (70, 3)·camera_from_rig·joint_rotations (127, 3, 모르면 NaN)·
vertices_cam f16·마스크 crop 비트), `<out>/faces.npy`(MHR 면, SAM License 라 팀 밖에 내지 않음), `<out>/metrics.json`. 사람이 찍힌 자료라 커밋하지 않는다.

## 평가 (`egate1_eval.py`)

묶음 그림과 world_tracks 의 카메라 관측은 frame_ref 가 같거나 촬영 시각 차 ≤ 20 ms 일 때만 잇는다(다른 시각 그림으로 채우지 않음).
지표 정의·이름은 `docs/experiments/EXP-02_design.md` 'E-GATE-1'. 요점:

- 한 카메라 제외: 사람이 보인 카메라 c 마다, c 를 뺀 Tier 0 2D 로 엉덩이를 삼각측량한 점을 기준점으로 쓴다(Tier 0 pelvis 는 c 를 썼으므로). c 를 뺀 집계의 대표 선택도 이 기준점을 pelvis 로 받는다(리뷰 권장 반영).
  c 를 뺀 후보로 다시 집계한 대표 포즈(`hmr_multiview`), 다른 카메라 후보 하나씩 기준점에 놓은 것(`hmr_single_root`), 자기 translation 그대로(`hmr_single_raw`, 기준선 '원본'),
  c 를 뺀 Tier 0 2D 삼각측량(`tier0_triangulated`, 기준선 '기본 골격')을 c 의 Tier 0 2D 에 비춘다. 관절은 c 에서 보였고 Tier 0 가 삼각측량할 수 있는 것(`common`)과
  Tier 0 가 못 한 것(`hmr_only`, 하체 채우기)을 따로 센다. 기준점을 만들 수 없으면(다른 카메라 2대 미만) 표본을 만들지 않고 이유를 남긴다.
- 실루엣 IoU: c 의 YOLO26-seg 마스크와, 같은 기준점에 놓은 대표 메시를 c 의 K_used 핀홀로 투영해 래스터한 것. numpy 래스터(화소 중심, 그림 넓이 25% 넘는 삼각형은 버림).
- 발-ground_anchor: 접지가 VALID 이고 `ground_anchor_xyz` 가 있을 때만 MHR70 발끝·발뒤꿈치 최저 Z − anchor Z. 기준선은 Tier 0 VALID 발목 Z(G5 정의).
- 상황 표지: crowded(다른 사람 있음), crossing(0.8 m 안), occluded(보인 카메라 < 전체), reappeared(빠졌다 다시). 연속성: 재등장·차수 변경·대표 카메라 바뀜·
  연속 시각 상대 관절 흔들림(재등장·차수 변경 사이는 재지 않음).
- 출력: `metrics.json`, `loo_rows.jsonl`, `pose_frames.jsonl`(계약 검사 통과분), `run_record.json`(EXP-02, E-GATE-1, gpu LOCAL, 계약 검사 뒤 씀). 표본이 없으면 FAILED.

## 노트북 검증 (경로 검증, 제품 성능 아님)

| 입력 | 결과 |
|---|---|
| 합성 장면(SIM, 카메라 4대·배럴 왜곡, 단안 오차 모형: 깊이 ±25%·체형 3%·관절 5 mm) | 다시점 한 카메라 제외 p50 < 원본 단안의 1/4, 실루엣 IoU 다시점 > 0.85 > 원본, 발 G5 안 100%, root 오차 다시점 < 2 cm < 원본. 체형 +10% 면 발 오차 7~11 cm 로 G5 밖(지표가 잡는다) |
| CMU Panoptic 171204_pose1(DATASET_GT 상한, HD 4대 hd_00_00·08·16·24, 30 시각 27 사람-시각, 후보 = 정답 + 체형·깊이 N(0, 5%) + 관절 2 cm, Tier 0 2D = 정답 왜곡 투영 + 1 px) | 한 카메라 제외 p50/p95: 다시점 15.2/27.1 px, 단안+기준점 18.2/32.2, 원본 단안 40.3/100.3, Tier 0 삼각측량 1.5/2.1. root 오차 p50 다시점 2.0 cm, 원본 10.3 cm. 실루엣·발은 정답이 없어 비었다(n=0) |

Panoptic 숫자는 오차 모형을 우리가 넣은 것이라 HMR 성능이 아니다. 경로(집계·기준점·지표)가 실제 다시점 기하에서 도는지만 본다(AGENTS 규칙 7).

## 실행 순서 (E-GATE-1 Run)

1. 노트북: `egate1_frames.py --session <녹화> --out <폴더> --step-ms 2000 --max-instants 60`(`uv run --extra io`).
2. Pod 에 그림 폴더·교실 내부 파라미터 기록·`yolo26x-seg.pt`(sha256 `92b3de00…`, `docs/data/weights.md`)를 올린다.
3. 노트북: `egate1_jobs.py ... -o exp-jobs-87.json` → `gpu_queue.py check` → Lead 가 4차 세션 큐에 끼운다. 항목 e1(mask)·e2(bbox, 원본 기준선)·e3(mask_kp, 미검증 변형).
4. 결과 묶음을 노트북으로 가져와 `egate1_eval.py --bundle … --world-tracks … --calibration …`. world_tracks 는 아래 '넘긴 일' 1·2 가 풀려야 녹화에서 나온다.

## 명세에 없어 정한 수치

| 값 | 근거 |
|---|---|
| bbox IoU ≥ 0.5 | 프롬프트 bbox 가 Tier 0 bbox 에서 나오므로 같은 사람이면 1 에 가깝다. 겹친 두 사람은 기하 게이트가 가른다 |
| pelvis 투영 게이트 bbox 높이 × 0.5 | 엉덩이 중점은 bbox 안 세로 중간쯤. 반 높이 넘게 벗어나면 다른 사람이다 |
| 깊이 비 1.5 | 단안 깊이는 체형 크기 사전에 기대 수십 % 틀릴 수 있다고 보고(실측 없음) 넉넉히 받되, 같은 시선 위 4 m 앞 사람과 6 m 뒤 사람(비 1.5)은 가른다. 시험: +40% 붙음, +60% 안 붙음. GPU Run 의 `NO_TRACK_MATCH` 비율로 조정 |
| keypoint 신뢰도 0.3(선택·위치 진단), 0.5(프롬프트) | 선택은 관절 수가 필요해 낮게, 프롬프트는 잘못된 점이 모델을 끌어 높게 |
| 위치 진단 카메라 2대·점 6개 | 위치 3자유도의 다시점 해 최소 |
| 잇기 20 ms | 25 fps 프레임 간격 40 ms 의 절반 |
| 교차 0.8 m | 두 사람 몸통 폭(약 0.4~0.5 m)을 합친 거리 근처. 초기값 |
| 래스터 삼각형 상한 그림 넓이 25% | MHR 삼각형은 수 화소. 카메라 바로 앞 퇴화 삼각형만 버린다 |
| 그림 간격 2 s·60 시각 | 3분 녹화 전체를 고르게. 예상 시간은 그림당 1.1 s 로 잡았다(H100 실측 HMR 1명 약 0.3 s·5명 약 0.4 s, EXP-02 6.4 에 검출·분할·펴기·쓰기를 넉넉히 더한 값), Job 하나 약 7 분 |

## 버린 대안

- **내장 다시점 융합**: 위 절.
- **카메라별 관절 회전 평균(관절마다 회전 평균)**: 카메라마다 다른 관절이 섞여 계층이 어긋날 수 있고 MHR 파라미터와 맞지 않는다. 대표 후보 하나를 고른다.
- **HMR translation 다시점 해를 root 로**: 5.2·20.4 위반(위치 정본은 Tier 0). 진단값으로만 둔다.
- **헝가리안 배정**: 교실 인원 규모에서 IoU 탐욕과 차이가 작고(자기 bbox 의 IoU 가 1 에 가깝다), 의존성(scipy)을 hmr 경로에 더하지 않는다.
- **마스크 없는 사람에게 bbox 사각형 마스크**: 지어낸 마스크다. 공식 API 제약은 호출을 둘로 나눠 푼다.
- **eval 이 Tier 0 pelvis 를 한 카메라 제외 기준점으로**: pelvis 는 제외한 카메라도 썼다. 나머지 카메라 2D 로 다시 삼각측량한다.
- **그림을 Pod 에서 디코딩**: 녹화 전체를 Pod 에 올려야 한다. 노트북에서 고른 그림만 올린다(GPU 최소 사용).

## 잔여 위험

- **체형 크기 오차가 발 오차로 바로 간다.** root 를 Tier 0 pelvis 에 두면 HMR 다리 길이 오차(체형 사전 오차)가 발 높이 오차가 된다(합성: 체형 +10% → 발 약 9 cm).
  5.6 은 translation 만 보정하라고 해 크기는 고치지 않았다. GPU Run 의 `foot_anchor_*` 로 보고, 크면 크기·위치 함께 맞추기(닮음 변환)를 명세 변경으로 올린다.
- 연결 게이트는 단안 깊이에 기댄다. 깊이 오차가 50% 를 넘는 사람(어린이·앉은 사람·가장자리)은 붙지 않을 수 있다(`NO_TRACK_MATCH` 비율로 본다).
- 대표 선택은 Tier 0 2D 에 기댄다. Tier 0 2D 가 같은 쪽으로 틀리면(좌우 뒤바뀜) 같이 틀린다.
- keypoint 프롬프트 주입(e3)은 모델 내부를 부른다. 묶음 프롬프트가 모두 빈 칸이면 재추론을 건너뛰고 그림 줄 `kp_prompt`·요약 `kp_prompt_skipped_all_dummy` 에 남긴다. 판이 바뀌면 깨지고, 이번에는 GPU 에서 돌려 보지 않았다.
- full 모드 `pred_global_rots` 는 손 IK 전 값이다(-88). 평가 Job 은 body 모드라 해당 없음. full 로 돌리면 `pose_from_model_params` 가 필요하고 추정기 출력에 `mhr_model_params` 를 더해야 한다.
- Panoptic·합성 결과는 우리가 넣은 오차 모형의 결과다. 실제 HMR 오차 분포는 GPU Run 뒤에 안다.

## 넘긴 일 / 판단 필요

1. **녹화 world_tracks 에 카메라별 관측이 없다.** Tier 0 파일 재생(`docs/design/tier0-recording-replay.md`)은 `world_state.jsonl` 만 쓰고
   `observation_batch` 를 쓰지 않는다. TIER0_LOG Provider 는 그것이 있어야 `per_camera`(bbox·keypoint)를 붙인다. 없으면 E-GATE-1 평가가 연결할 관측이 없다.
   재생이 `observation_batch` 도 쓰거나 2D 관측(Parquet) 리더가 생겨야 한다(Tier 0 쪽 티켓).
2. **교실 외부 파라미터 보정**(-214 계열)이 있어야 녹화를 재생해 world_state 가 나온다. 지금은 내부 파라미터만 있다.
3. `deploy/weights.json` 에 `yolo26x-seg` 를 `exp02`·`unified` 묶음으로 넣을지(-291 영역이라 고치지 않았다). 지금은 Pod 에 따로 올리고 sha256 을 Job 이 확인한다.
4. `human_mesh_output.pose_frame.native_pose.params` 모양: 이번 기록은 스트림 `mhr_pose` 와 같은 `{root_rotation, joint_rotations}` 를 싣고
   `format_version` 에 `mhr_pose@avatar_pose_batch-1.0.0` 이라 적었다. 스키마는 객체만 요구한다(계약 그대로). -227 이 기록 스키마 마이너로 고정할 때 맞춘다.
5. 명세 5.6 '다시점 집계'에 내장 융합을 쓰지 않은 이유(SMPL 출력·1인)와 대표 선택 규칙을, 15.1·20.6 에 체형 크기 → 발 오차 위험을 적을지(-247 명세 정리).
