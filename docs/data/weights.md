# 모델 가중치 — manifest

`deploy/weights.json`(simai-bootstrap 가중치 목록, S15P31A101-205)의 출처·라이선스·해시 근거다.
가중치 파일은 저장소에 넣지 않는다. 이 문서에는 출처·라이선스·경로·해시와 해시를 얻은 기록만 둔다(AGENTS.md 저장소 규칙, 명세 3.12).
목록을 바꿀 때는 덮어쓰지 않고 새 항목(새 이름·경로)을 더하고, 이 문서에 획득 기록을 함께 남긴다.

## yolo26x-pose (Tier 0 2D 검출)

| 항목 | 값 |
|---|---|
| 저장 경로 | `$SIMAI_ROOT/weights/yolo/yolo26x-pose.pt` |
| 원본 | https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26x-pose.pt |
| sha256 | `08ed9e01d22a6f248b04f2f9992016aca9a32250b9ab57057d886a09d026700d` |
| 라이선스 | AGPL-3.0 (Ultralytics) |
| 쓰는 곳 | Tier 0 이미지(`deploy/tier0`), TensorRT 엔진 캐시 키(가중치 해시 앞 12자) |

획득 기록:

- 2026-10-16 E-GATE-0 때 일본 Network Volume 에서 `ai/experiments/hmr/setup_runpod.sh weights` 가
  `ultralytics.utils.downloads.attempt_download_asset('yolo26x-pose.pt')` 로 받았다. ultralytics 8.4.x 의 이 함수는 저장소
  `ultralytics/assets` 의 릴리스 `v8.4.0`(함수 기본 인자, 노트북 설치본 8.4.95·8.4.178 에서 확인)에서 받으므로 원본은 위 URL 이다.
- 같은 스크립트가 받은 직후 `sha256sum` 으로 `weights/hmr_weights.sha256` 을 만들었고, 그 사본이 노트북
  `ai/experiments/hmr/runs/weights/hmr_weights.sha256`(커밋하지 않는 `runs/`, 파일 sha256 `34d136f28c3d973d…`)에 있다. 해당 줄:

  ```
  08ed9e01d22a6f248b04f2f9992016aca9a32250b9ab57057d886a09d026700d  /workspace/simai/weights/yolo/yolo26x-pose.pt
  ```

- 원본 URL 에서 다시 받아 해시를 대조하지는 않았다(대용량 다운로드를 하지 않는 노트북 작업). 첫 GPU 세션에서 `simai-bootstrap` 이
  원본에서 받아 이 값과 대조한다. 다르면 시작하지 않으므로(종료 코드 2) 그때 이 문서와 목록을 새 항목으로 고친다.

## 실험 이미지 가중치 (S15P31A101-207)

HMR 이미지(`deploy/exp02`, 묶음 `exp02`)와 MapAnything 이미지(`deploy/mapanything`, 묶음 `static`)가 시작할 때 `simai-bootstrap` 으로 받는다.
HF 원본은 모두 **커밋(revision) 고정**이다(`main` 이 바뀌어도 같은 파일). 2026-10-21 에 HF API(`/api/models/<저장소>`)로 본 각 저장소의 마지막 수정이
E-GATE-0(2026-10-16) 전이어서, E-GATE-0 때 받은 파일과 같은 revision 이다.

| 이름(`weights.json`) | 묶음 | 저장 경로(`$SIMAI_ROOT/`) | 원본(HF 저장소 @revision 앞 7자) | sha256 | 라이선스 | 해시 근거 |
|---|---|---|---|---|---|---|
| yolo26x-pose | tier0·exp02 | `weights/yolo/yolo26x-pose.pt` | 위 절 | 위 절 | AGPL-3.0 | 위 절 |
| sam-3d-body-dinov3 | exp02 | `weights/sam-3d-body-dinov3/model.ckpt` | `facebook/sam-3d-body-dinov3` @11aaa34 (승인제) | `b5a2f9d305dd02626b967aa2e86021fba07065df66ce7a7e00ffb9664f150abf` | SAM License (Meta) | E-GATE-0 `hmr_weights.sha256`(아래) |
| sam-3d-body-dinov3-mhr | exp02 | `weights/sam-3d-body-dinov3/assets/mhr_model.pt` | 같은 저장소 `assets/mhr_model.pt` (696,110,248 B) | `352e271a6c42729c68554ceaea0c955e866970160c31e35506d782dc0f7377bc` | SAM License (Meta) | HF API LFS oid(승인 계정, 2026-10-21, 아래) |
| moge-2-vits-normal | exp02 | `weights/moge/moge-2-vits-normal/model.pt` | `Ruicheng/moge-2-vits-normal` @26b477f | `79a16621928c2bf0ed04659218c55c01075e950507f40bb3332fb4c873d3e1dc` | MIT | HF API LFS oid |
| moge-2-vitl-normal | exp02 | `weights/moge/moge-2-vitl-normal/model.pt` | `Ruicheng/moge-2-vitl-normal` @cb0e8bb | `280741fd09bc3f403ccff9967784c2a391b52d2c0742ae3efdb21d9f90cc1a01` | MIT | HF API LFS oid |
| map-anything-apache-model | static | `weights/map-anything-apache/model.safetensors` | `facebook/map-anything-apache` @00f9c24 | `fa06c0fdccefc5048e072c85935d5789b1e36b307f3859033c17f9dcb9fd5201` | Apache-2.0 | HF API LFS oid = E-GATE-0 C2 `weights_hash` |
| map-anything-apache-config | static | `weights/map-anything-apache/config.json` | 같은 저장소 | `65701d09d99ed37a21d295f0d138978b3d584ab3bccdbcb4a2853da212b676c5` | Apache-2.0 | 고정 revision 에서 받아 sha256 (5,776 B) |
| map-anything-model | static | `weights/map-anything/model.safetensors` | `facebook/map-anything` @a1d87e9 | `981f060c64664dff3272b5f5a823d350abe71a2f144444db4cfc325f3ed5a3a0` | CC-BY-NC-4.0 (비상업, 연구 비교용) | HF API LFS oid = E-GATE-0 C1 `weights_hash` |
| map-anything-config | static | `weights/map-anything/config.json` | 같은 저장소 | `65701d09…676c5` (apache 와 같은 파일) | CC-BY-NC-4.0 | 고정 revision 에서 받아 sha256 |

획득 기록:

- **sam-3d-body-dinov3 `model.ckpt`**: 위 yolo 절과 같은 E-GATE-0 해시 파일(노트북 사본 `ai/experiments/hmr/runs/weights/hmr_weights.sha256`)의 줄
  `b5a2f9d3…0abf  /workspace/simai/weights/sam-3d-body-dinov3/model.ckpt`. 그때 `snapshot_download("facebook/sam-3d-body-dinov3")` 가 받은 `main` 이고, 저장소 마지막 수정은 2025-12-10 이라 지금 revision 11aaa34 와 같다.
- **`assets/mhr_model.pt`**: E-GATE-0 해시 파일은 폴더 최상위 `*.ckpt`·`*.pt` 만 해시해 이 파일이 없다. 저장소가 승인제라 토큰 없는 HF API·HEAD 요청은 401 이고 LFS 해시를 주지 않는다.
  2026-10-21 승인 계정 토큰으로 아래 명령을 돌려 채웠다(크기 696,110,248 B 일치). 같은 응답의 `model.ckpt` oid 는 위 E-GATE-0 값 `b5a2f9d3…0abf` 와 같았다.
  이미지 `jonghwa1492/tier0:*-3926a8772649` 안의 `/opt/simai/deploy/weights.json` 은 이 값을 채우기 전 판이라 비어 있다. 그 이미지로 exp02 를 돌릴 때는
  저장소 사본의 목록을 `SIMAI_WEIGHTS_MANIFEST=<사본>/deploy/weights.json` 로 넘긴다(다음 이미지 빌드부터는 이미지 안 목록에 들어 있다). 명령(토큰 값은 출력하지 않는다):

  ```sh
  curl -s -H "Authorization: Bearer $HF_TOKEN"     "https://huggingface.co/api/models/facebook/sam-3d-body-dinov3/tree/11aaa346c7204874a1cbafe3d39a979080b2c55a/assets"     | python -c "import sys,json; [print(x['path'], x['size'], x['lfs']['oid']) for x in json.load(sys.stdin) if x.get('lfs')]"
  ```

  나온 oid(= 파일 sha256)를 `weights.json` 의 `sam-3d-body-dinov3-mhr.sha256` 에 넣고 이 표의 근거 칸을 "HF API LFS oid(승인 계정, 날짜)"로 고친다.
  `model.ckpt` 의 oid 가 위 E-GATE-0 값과 같은지도 같은 명령(`tree/<revision>`)으로 대조한다.
- **MoGe2·map-anything `model.*`**: 공개 저장소라 HF API `tree/main?recursive=true` 의 `lfs.oid`(LFS 객체 sha256)를 그대로 썼다(2026-10-21). map-anything 두 파일은
  E-GATE-0 Run 기록(`ai/experiments/static_scene/runs/C1_room4_bf16/metrics.json`·`C2_room4_apache_bf16/metrics.json` 의 `weights_hash`, 커밋하지 않는 `runs/`)과 같다.
  MoGe2 는 E-GATE-0 때 Fast 코드가 HF 캐시로 받았던 것이라 해시 기록이 없다. 이미지에서는 `fs3db_common.use_local_moge()` 가 이 확인된 파일을 쓴다.
  S15P31A101-238 부터 HMR 은 알려진 K(보정·데이터셋)를 먼저 쓰지만 MoGe2 두 항목은 묶음에 남긴다: K 를 모르는 입력(E-GATE-0 재현 14장, 라이브 서버)의 예비 경로다(`docs/experiments/EXP-02_design.md` "K 입력").
- **map-anything `config.json`**: LFS 가 아니어서 고정 revision 의 `resolve/<revision>/config.json` 을 받아(5,776 B) sha256 을 냈다. 두 저장소의 config 가 같은 파일이다.
- 큰 파일은 노트북에서 받지 않았다. 첫 실험 이미지 세션에서 `simai-bootstrap` 이 받아 대조한다. 다르면 시작하지 않으므로(종료 코드 2) 그때 이 문서와 목록을 새 항목으로 고친다.
- 이미지에 넣는 코드 사본(가중치 아님): Fast-SAM-3D-Body @d72aa36(MIT), DINOv3 허브 코드 @6876159(DINOv3 License, 2026-10-16 이전 마지막 커밋),
  map-anything @3d10cf7(Apache-2.0), DINOv2 허브 코드 @7764ea0(Apache-2.0, 같은 기준). 두 허브 코드는 `torch.hub` 가 GitHub `main` 을 그때그때 받던 것을
  커밋으로 고정한 것이다(`pretrained=False` 경로: 코드만 쓰고 가중치는 위 목록에서 읽는다).

## yolo26x-seg (보정 입력 사람 마스크, S15P31A101-102)

MapAnything 보정 입력(`simai_ai.calibration.world_frame.cli prepare`)에서 교실 사진의 사람 영역을 찾는 분할 모델이다.
S15P31A101-362부터 아바타 루프도 HMR·텍스처 마스크로 쓰며 `deploy/weights.json`의 `avatar`·`exp02` 묶음에 포함한다.

| 항목 | 값 |
|---|---|
| 노트북 위치 | 정하지 않음(커밋하지 않는 위치. `--segmenter <경로> --segmenter-sha256 <값>` 으로 넘긴다) |
| 원본 | https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26x-seg.pt |
| sha256 | `92b3de0065766a17180d6219858717dc9d03cdce8a3ca9576c97fd75aabb64f3` (142,129,861 B) |
| 라이선스 | AGPL-3.0 (Ultralytics) |
| 쓰는 곳 | `world_frame.prepare.UltralyticsPersonSegmenter`(person 클래스만, 가중치 해시 확인 뒤 사용, 코드가 내려받지 않음) |
| 아바타 루프 | `hmr.segmentation.YoloPersonSegmenter`, `SIMAI_AVATAR_SEG_WEIGHTS`(기본 `$SIMAI_ROOT/weights/yolo/yolo26x-seg.pt`) |

획득 기록: 2026-10-22 노트북에서 위 URL 을 `curl -L` 로 받아 `sha256sum` 으로 쟀다(ultralytics 8.4.178 이 받는 릴리스와 같은 `v8.4.0`).

## Cosmos3-Nano (분석 워커 VLM Reasoner, 묶음 `vlm`, S15P31A101-235)

| 항목 | 값 |
|---|---|
| 저장 경로 | `$SIMAI_ROOT/weights/cosmos3-nano/<HF 저장소 안 경로>` (18개) |
| 원본 | HF `nvidia/Cosmos3-Nano`, revision `e59a53c25979a090fa8706c9acc0c254a6e89b92`(HF API 마지막 수정 2026-09-16) |
| 크기 | 받는 18개 약 31.5GB(저장소 전체 34.99GB 중 `vae/` 1.41GB·`sound_tokenizer/` 1.99GB·예시 영상 등은 받지 않음) |
| 라이선스 | OpenMDW-1.1 (NVIDIA). HF API `gated=false`(2026-10-22, NVIDIA 빠른 시작 문서는 승인제라고 적어 서로 다름. 받기가 401 이면 승인 계정 `HF_TOKEN`) |
| 쓰는 곳 | vLLM 0.21.0 + `vllm-cosmos3` Reasoner 서빙(`--hf-overrides '{"architectures":["Cosmos3ReasonerForConditionalGeneration"]}'`). 지금 이 묶음을 쓰는 이미지는 없다(2단계 통합 이미지) |

획득 기록(2026-10-22, 노트북):

- `https://huggingface.co/api/models/nvidia/Cosmos3-Nano`(sha·gated)와 `/tree/main?recursive=true`(파일 크기·LFS oid)를 받았다. 큰 파일 8개(`transformer/diffusion_pytorch_model-0000{1..7}-of-00007.safetensors`,
  `vision_encoder/model.safetensors`)의 sha256 은 LFS oid 다(받지 않음).
- 작은 파일 10개(`config.json`, `model.safetensors.index.json`, `generation_config.json`, `preprocessor_config.json`, `video_preprocessor_config.json`, `chat_template.json`, `tokenizer.json`,
  `tokenizer_config.json`, `vocab.json`, `merges.txt`)는 위 revision 의 `resolve/` 로 받아 `sha256sum` 으로 쟀다.
- 고른 근거(루트 index 가 vae·sound_tokenizer 를 가리키지 않음)와 확인 항목은 `docs/design/unified-env.md` "Cosmos3-Nano 가중치". 첫 GPU 세션에서 `simai-bootstrap` 이 받아 대조한다. 다르면 시작하지 않는다(종료 코드 2).

## BAAI/bge-m3 (RAG 조항·질문 임베딩, extra `rag`, S15P31A101-27)

| 항목 | 값 |
|---|---|
| 저장 경로 | 노트북 HF 캐시(`~/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots/<revision>/`). GPU Pod 에서 쓰지 않아 `deploy/weights.json` 에 넣지 않는다 |
| 원본 | HF `BAAI/bge-m3`, revision `5617a9f61b028005a4858fdac845db406aefb181`(main, HF API 마지막 수정 2024-07-03, 2026-10-23 조회). 코드 고정 위치 `ai/src/simai_ai/rag/embed.py` `REVISION` |
| 쓰는 파일 | 아래 6개(transformers `AutoModel`·`AutoTokenizer` 가 받는 것). `onnx/`·`colbert_linear.pt`·`sparse_linear.pt` 는 받지 않는다(dense 만 씀) |
| 라이선스 | MIT. 모델 저장소에는 LICENSE 파일이 없고(위 revision 의 `resolve/…/LICENSE` 404) 모델 카드 메타데이터가 `license: mit` 이다. 코드 저장소 FlagOpen/FlagEmbedding 의 LICENSE 도 MIT(GitHub API, 2026-10-23) |
| 쓰는 곳 | `simai_ai.rag.seed embed`(노트북 CPU fp32, 배치 1). 결과는 `ai/src/simai_ai/rag/data/embeddings.json` 에 커밋한다 |

| 파일 | 크기(B) | sha256 |
|---|---|---|
| `pytorch_model.bin` | 2,271,145,830 | `b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38` (LFS oid 와 같음) |
| `tokenizer.json` | 17,098,108 | `21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08` |
| `sentencepiece.bpe.model` | 5,069,051 | `cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865` |
| `config.json` | 687 | `26159e7ad065073448460117eb24b7a4572f6f4e78eadff65dc0a11c052449fa` |
| `tokenizer_config.json` | 444 | `a62b2b6784f990259fddef5f16388693a8043be4f69179e6a5257eeb3f9abac4` |
| `special_tokens_map.json` | 964 | `8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835` |

획득 기록: 2026-10-23 노트북에서 transformers 5.9.0 이 위 revision 으로 받은 캐시 스냅숏의 파일을 `sha256sum` 으로 쟀다(받기 약 8 분, 약 5 MB/s).
`pytorch_model.bin` 의 값은 캐시 blob 이름(HF LFS oid)과 같다.

## T1-v4-ALL PPE 검출 (안전모·조끼 4클래스, 묶음 `ppe`, S15P31A101-491)

| 항목 | 값 |
|---|---|
| 저장 경로 | `$SIMAI_ROOT/weights/ppe/T1-v4-ALL-seed20261021-epoch08.pt` |
| 원본 | HF 비공개 저장소 `simai-s15p31a101/simai-ppe-helmet-vest`, revision `36e3ebb10d44c38b46ab430d769b8d4334bdb5f4`, 파일 `T1-v4-ALL-seed20261021-epoch08.pt` |
| sha256 | `e373a104805765417502e34d21586be2607126bbdd8499b935896ca333eb103c` |
| 크기 | 471,779,845 바이트 |
| model_version | `T1-v4-ALL-seed20261021-e08-e373a1048057` |
| 구조·추론 | YOLO26x detect, 클래스 0 HELMET·1 NO_HELMET·2 VEST·3 NO_VEST(불러올 때 명세 이름 `helmet_head`·`no_helmet_head`·`vest_torso`·`no_vest_torso` 로 바꿈), 전체 화면 640, FP32, conf 0.5, iou 0.7 |
| 라이선스 | AGPL-3.0 (Ultralytics YOLO26x 미세조정 파생). 학습 데이터는 팀 내부 녹화(팀원 촬영)라 비공개 저장소에서 팀 내부 용도로만 쓴다 |
| 접근 | 비공개. 읽기 권한 계정의 `HF_TOKEN` 필요. RunPod Secret `hf_token` 계정(`jonghwa2`)을 조직에 READ 로 넣었다(2026-11-05). 그 토큰은 Fine-grained 라 토큰에 조직 `simai-s15p31a101` 저장소 읽기 권한을 더해야 받는다(미완료) |
| 쓰는 곳 | PPE 프로세스(명세 5.5, `simai_ai.ppe.detector.PpeDetector`), 환경 변수 `SIMAI_PPE_WEIGHTS` |

획득 기록:

- 2026-11-04 H200 학습 서버에서 세 세션 전체 재학습(T1 corrected-v4, seed 20261021, epoch 8 고정) checkpoint
  `native-checkpoints/epoch-0008.pt` 를 해시했다(`checked_at_utc` 2026-11-04T23:47:53Z). 시드는 먼저 등록한 시드라는 사전 규칙으로
  정했고 점수로 고르지 않았다. 같은 조건의 seed 20261022(`80603b71…`)는 비교용으로만 보존한다.
- 2026-11-05 RunPod `simai-tier0` 템플릿(H100, torch 2.11.0+cu130, ultralytics 8.4.178)에서 기준 12장 추론이 학습 환경 예측과
  비트 단위로 같았다(RunPod 동일성 확인 PASS).
- 2026-11-05 위 파일을 HF 비공개 저장소에 올렸다. HF API LFS oid(Bearspur 계정, 2026-11-05)가 위 sha256·크기와 같음을 확인했다(private=true, main 최신 revision `36e3ebb10d44c38b46ab430d769b8d4334bdb5f4`).
- 이 checkpoint 는 학습에서 뺀 세션이 없어 자체 외부 평가 수치가 없다. 같은 학습 방식의 세션 제외 폴드 mAP50 79.6~93.6%(두 시드 평균)가
  일반화 추정이다. 정답은 사람 전수 검토가 아닌 v4 기계 보정 + 육안 교정이다.
