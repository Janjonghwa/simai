#!/usr/bin/env bash
# 이미지 점검 (GPU 없이, 노트북·CI·Pod 어디서나). 이미지를 레지스트리에 올리기 전에 돌린다.
#   bash deploy/scripts/check_image.sh simai/tier0:local
#   bash deploy/scripts/check_image.sh simai/exp02-fs3db:local      # 실험 이미지(S15P31A101-207): 이미지 ENV SIMAI_IMAGE_KIND 로 종류를 고른다
#   bash deploy/scripts/check_image.sh simai/risk-engine:local      # EC2 Risk Engine 컨테이너(S15P31A101-99). 저장소 최상위에서(계약 예시를 넣는다)
#   bash deploy/scripts/check_image.sh simai/unified:local          # 통합 이미지(S15P31A101-246): Tier 0 항목 전부 + vLLM·플러그인·HMR(잠금 밖 휠 없이) import,
#                                                                     EGL 렌더(버리는 컨테이너의 Mesa), 감시 프로세스 기동·종료 순서(가짜 프로세스). 저장소 최상위에서
#                                                                     MapAnything(S15P31A101-250): 추론 경로 import·DINOv2 허브 사본(망 없이 골격 생성)·장면 준비 모드
# 확인: 비밀·가중치·엔진·SSH 호스트 키가 이미지에 없음, 패키지 import, 진입점 도움말,
#       볼륨 없음·가중치 없음·해시 불일치·GPU 없음이 각각 명확한 코드와 메시지로 끝남, GPU 별 엔진 경로가 다름,
#       PUBLIC_KEY(RunPod)·SSH_PUBLIC_KEY(Vast.ai) 를 주면 키 인증만 받는 sshd 가 뜸, SIMAI_SSHD=off 면 안 뜸,
#       wstunnel 고정 판이 있고 경로 비밀값을 주면 sshd 뒤에 뜨며 비밀값이 명령줄·출력에 없음(S15P31A101-286),
#       SIMAI_TUNNEL_ALLOW 포트가 127.0.0.1 허용 대상으로 더해짐(S15P31A101-288),
#       기본 진입점(simai-bootstrap)은 GPU 가 없으면 코드 3, 실행 스크립트에 CR(Windows 줄바꿈)이 없음 (S15P31A101-205).
# 실험 이미지(exp02·mapanything)는 Tier 0 전용 항목(엔진·simai-tier0·분석 워커) 대신: 종류별 import·허브 코드 사본
#       (exp02: Detectron2 확장이 CPU 전용이고 LazyConfig 가 Fast 관절 메타데이터를 읽음, S15P31A101-238),
#       부트스트랩이 자기 가중치 묶음을 고르는지(해시 모르는 항목은 받기 전에 코드 2, 받기 실패는 코드 2), 실행 도우미의 체크포인트 없음 코드 2.
# EC2 Risk Engine 컨테이너(risk_engine, S15P31A101-99)는 GPU·SSH·가중치 항목 대신: 무거운 패키지 없음, 비루트 사용자, 공개 포트(EXPOSE) 없음,
#       헬스체크, 공통 테스트 벡터(D-G3-04)가 이미지 안 라이브러리로 모두 통과, 망 없는(--network none) 컨테이너를 띄워 healthy 가 된 뒤
#       계약 예시를 HTTP 로 보내 계약 예시 결과와 같은지(deploy/scripts/risk_plan_roundtrip.py).
# 컨테이너 볼륨은 이름 있는 Docker 볼륨을 만들어 쓰고 끝나면 지운다(호스트 경로 변환 문제를 피한다).
set -uo pipefail
export MSYS_NO_PATHCONV=1

IMG=${1:?사용법: check_image.sh <이미지>}
VOL=simai-check-$$
FAILS=0
pass(){ echo "PASS  $*"; }
fail(){ echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
# shellcheck disable=SC2317  # trap 으로 부른다
cleanup(){ docker volume rm -f "$VOL" >/dev/null 2>&1; docker rm -f "$VOL" >/dev/null 2>&1; }
trap cleanup EXIT
KIND=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$IMG" | sed -n 's/^SIMAI_IMAGE_KIND=//p')
KIND=${KIND:-tier0}
echo "이미지 종류: $KIND"
case "$KIND" in tier0|exp02|mapanything|risk_engine|unified) ;; *) echo "FAIL  알 수 없는 SIMAI_IMAGE_KIND=$KIND"; exit 1 ;; esac
# 통합 이미지(S15P31A101-246)는 Tier 0 이미지와 같은 Tier 0 층·진입점이라 Tier 0 항목을 모두 돌고, 워커 쪽 항목을 더한다
T0LIKE=0; case "$KIND" in tier0|unified) T0LIKE=1 ;; esac

# 1) 이미지 층 이력·환경 변수에 비밀이 없다
SECRET_RE='hf_[A-Za-z0-9]{20,}|HF_TOKEN=[^ ]|HUGGING_FACE_HUB_TOKEN=|RUNPOD_API_KEY=|SIMAI_TUNNEL_PREFIX=|PUBLIC_KEY=ssh-|BEGIN [A-Z ]*PRIVATE KEY'
if docker history --no-trunc --format '{{.CreatedBy}}' "$IMG" | grep -Eq "$SECRET_RE"; then
  fail "docker history 에 비밀로 보이는 값이 있다"
else
  pass "docker history 에 비밀 값 없음"
fi
if docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$IMG" | grep -Eiq '^(HF_TOKEN|HUGGING_FACE_HUB_TOKEN|RUNPOD_API_KEY|SIMAI_TUNNEL_PREFIX|PUBLIC_KEY|.*SECRET.*|.*PASSWORD.*)='; then
  fail "이미지 환경 변수에 비밀 이름이 있다"
else
  pass "이미지 환경 변수에 비밀 이름 없음"
fi

# 2) 파일 시스템에 가중치·엔진·비밀 파일·SSH 호스트 키가 없다
#    가중치 확장자는 256KB 넘는 것만 본다(패키지가 싣는 .pth 경로 설정 파일·onnx 시험용 작은 모델은 가중치가 아니다).
#    .pem·.key 는 개인 키가 든 것만 본다(certifi 인증서 묶음 같은 공개 인증서는 괜찮다).
#    compressed_tensors(vLLM 의존성)의 hadamards.safetensors 는 패키지에 든 하다마드 행렬 표(학습 가중치 아님, 통합 이미지)라 뺀다.
FOUND=$(docker run --rm --entrypoint /bin/sh "$IMG" -c '
  find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f -size +256k \( \
    -name "*.pt" -o -name "*.pth" -o -name "*.engine" -o -name "*.onnx" -o -name "*.safetensors" -o -name "*.ckpt" \
    -o -name "*.trt" -o -name "*.bin" -path "*weights*" \)     ! -path "*/site-packages/compressed_tensors/transform/utils/hadamards.safetensors" -print 2>/dev/null
  find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f \( \
    -name "hf.env" -o -name ".env" -o -name "*.env" -o -name "id_rsa*" -o -name "id_ed25519*" -o -name "authorized_keys" \
    -o -path "/etc/ssh/ssh_host_*" -o -path "*/.simai-private/*" \) -print 2>/dev/null
  find / -xdev \( -path /proc -o -path /sys -o -path /dev \) -prune -o -type f \( -name "*.pem" -o -name "*.key" \) \
    -exec grep -l "PRIVATE KEY" {} + 2>/dev/null
  [ -e /workspace ] && echo "/workspace (이미지 안에 볼륨 경로가 있다)"
  [ -d /opt/simai/ai/src ] && echo "/opt/simai/ai/src (소스 사본이 남았다)"
  true')
if [ -z "$FOUND" ]; then
  pass "가중치·엔진·비밀 파일·SSH 호스트 키 없음"
else
  fail "이미지에 있으면 안 되는 파일:"; while IFS= read -r line; do echo "      $line"; done <<<"$FOUND"
fi

# 3) import·버전·잠금
case "$KIND" in
  exp02) IMPORTS='
import importlib.metadata as m, os, pathlib, sys
import torch, torchvision, detectron2, detectron2._C, moge, ultralytics, pyrender, smplx, pytorch_lightning  # chumpy 0.70 은 Python 3.11 에서 import 가 깨진다(inspect.getargspec, 볼륨 venv 와 같음). 설치만 확인
assert m.version("chumpy") == "0.70"
from moge.model.v2 import MoGeModel
from detectron2.config import LazyConfig
assert not detectron2._C.has_cuda(), "Detectron2 CUDA 확장이 들어 있다(CUDA 확장 없이 빌드해야 한다)"
repo = os.environ["SIMAI_FS3DB_REPO"]
assert len(LazyConfig.load(repo + "/sam_3d_body/metadata/mhr70.py").pose_info["keypoint_info"]) == 70
for d in (repo + "/sam_3d_body", repo + "/checkpoints/sam-3d-body-dinov3/model_config.yaml",
          "/root/.cache/torch/hub/facebookresearch_dinov3_main/hubconf.py"):
    assert pathlib.Path(d).exists(), d
print(sys.version.split()[0], "torch", torch.__version__, "detectron2", m.version("detectron2"), "cpu-only",
      "ultralytics", m.version("ultralytics"), "numpy", m.version("numpy"))' ;;
  mapanything) IMPORTS='
import importlib.metadata as m, os, pathlib, sys
import torch, torchvision, mapanything, uniception, rerun
from mapanything.models import MapAnything
from simai_ai.scene.run_mapanything import resolve_local_hub_dir, use_local_dinov2_hub
for d in (os.environ["SIMAI_MAPANYTHING_REPO"] + "/mapanything", "/root/.cache/torch/hub/facebookresearch_dinov2_main/hubconf.py"):
    assert pathlib.Path(d).exists(), d
assert resolve_local_hub_dir("facebookresearch/dinov2") == pathlib.Path("/root/.cache/torch/hub/facebookresearch_dinov2_main")
use_local_dinov2_hub()
print(sys.version.split()[0], "torch", torch.__version__, "mapanything", m.version("mapanything"), "uniception", m.version("uniception"),
      "commit", os.environ["SIMAI_MAPANYTHING_COMMIT"][:7])' ;;
  unified) IMPORTS='
import importlib.metadata as m, importlib.util as u, os, pathlib, sys
import torch, vllm, flashinfer, transformers, tensorrt, ultralytics, pyrender, smplx, pytorch_lightning, timm, cv2
import modelopt.onnx.autocast, onnxruntime
from vllm import ModelRegistry
from vllm.plugins import load_general_plugins
load_general_plugins()
assert "Cosmos3ReasonerForConditionalGeneration" in ModelRegistry.get_supported_archs(), "vllm-cosmos3 플러그인 미등록"
# 잠금 밖 휠(Detectron2·MoGe·chumpy)은 통합 이미지에 넣지 않는다. CUDA 12 짐(onnxruntime-gpu·cupy)·GUI OpenCV 도 없다
extra = [n for n in ("detectron2", "moge", "chumpy", "cupy") if u.find_spec(n)]
for d in ("onnxruntime-gpu", "opencv-python", "cupy-cuda12x"):
    try:
        m.distribution(d)
        extra.append(d)
    except m.PackageNotFoundError:
        pass
assert not extra, extra
# 시연 HMR 경로(보정 K → sam-3d-body → 메시·관절)가 셋 없이 올라온다. 관절 정보는 mhr70 모듈을 그대로 읽는다
sys.path.insert(0, os.environ["SIMAI_FS3DB_REPO"])
from sam_3d_body import SAM3DBodyEstimator, load_sam_3d_body
from sam_3d_body.metadata.mhr70 import pose_info
from tools.build_detector import HumanDetector
assert len(pose_info["keypoint_info"]) == 70
assert pathlib.Path("/root/.cache/torch/hub/facebookresearch_dinov3_main/hubconf.py").is_file()
# MapAnything(S15P31A101-250): 잠금 밖 map-anything 휠 + 잠금 scene 묶음. 추론 경로(run_mapanything.py)가 쓰는 모듈과 소스 사본·DINOv2 허브 코드
import mapanything, uniception, rerun
from mapanything.models import MapAnything
from mapanything.utils.geometry import depthmap_to_world_frame
from mapanything.utils.image import load_images, preprocess_inputs
from mapanything.utils.viz import predictions_to_glb
assert m.version("mapanything") == "1.1.4" and m.version("uniception") == "0.1.7", (m.version("mapanything"), m.version("uniception"))
assert os.environ["SIMAI_MAPANYTHING_COMMIT"].startswith("3d10cf7"), os.environ["SIMAI_MAPANYTHING_COMMIT"]
for d in (os.environ["SIMAI_MAPANYTHING_REPO"] + "/mapanything/models/__init__.py", "/root/.cache/torch/hub/facebookresearch_dinov2_main/hubconf.py"):
    assert pathlib.Path(d).is_file(), d
assert mapanything.__file__.startswith(sys.prefix), mapanything.__file__  # 설치한 휠(사본은 run_mapanything.py 가 sys.path 앞에 넣을 때만)
extra = [p.name for p in pathlib.Path(os.environ["SIMAI_MAPANYTHING_REPO"]).iterdir() if p.name == "build" or p.name.endswith(".egg-info")]
assert not extra, extra  # 휠 빌드 찌꺼기가 사본에 남지 않는다
print(sys.version.split()[0], "torch", torch.__version__, "vllm", m.version("vllm"), "vllm-cosmos3", m.version("vllm-cosmos3"),
      "transformers", m.version("transformers"), "tensorrt", m.version("tensorrt-cu13"), "onnxruntime", m.version("onnxruntime"),
      "mapanything", m.version("mapanything"), "rerun-sdk", m.version("rerun-sdk"))' ;;
  risk_engine) IMPORTS='
import importlib.metadata as m, importlib.util as u, sys
import services.risk_engine.app, simai_ai.risk.plan, fastapi, uvicorn, jsonschema
heavy = [n for n in ("torch", "numpy", "cv2", "ultralytics", "tensorrt", "websockets", "services.tier0") if u.find_spec(n)]
assert not heavy, heavy
print(sys.version.split()[0], "simai-ai", m.version("simai-ai"), "fastapi", m.version("fastapi"), "uvicorn", m.version("uvicorn"),
      "jsonschema", m.version("jsonschema"))' ;;
esac
if [ "$KIND" != tier0 ]; then
  if OUT=$(docker run --rm --entrypoint python "$IMG" -c "$IMPORTS" 2>&1); then
    pass "import·코드 사본 ($KIND: $(tail -1 <<<"$OUT"))"
  else
    fail "import 실패 ($KIND): $(tail -5 <<<"$OUT")"
  fi
fi
if [ "$T0LIKE" = 1 ]; then
if OUT=$(docker run --rm --entrypoint python "$IMG" -c '
import importlib.metadata as m, sys
import simai_ai, services.tier0, simai_ai.detection.engine_cache, cv2, ultralytics, tensorrt, onnx, modelopt
import simai_ai.reid.collector, timm, yacs, ftfy, einops  # Re-ID(reid extra, S15P31A101-471). COPE 코드·가중치는 이미지 밖(Pod 에 올림)
print(sys.version.split()[0], "ultralytics", m.version("ultralytics"), "torch", m.version("torch"), "tensorrt", m.version("tensorrt-cu13"),
      "modelopt", m.version("nvidia-modelopt"), "onnx", m.version("onnx"))' 2>&1); then
  pass "import simai_ai·services.tier0·engine_cache·cv2·ultralytics·tensorrt·onnx·modelopt·reid ($OUT)"
else
  fail "import 실패: $OUT"
fi
if OUT=$(docker run --rm "$IMG" simai-wait-tier0 --no-tier0 --marker /nonexistent -- true 2>&1); then
  pass "simai-wait-tier0 (분석 워커 순서 도구)"
else
  fail "simai-wait-tier0: $OUT"
fi
if OUT=$(docker run --rm "$IMG" --help 2>&1) && grep -q "Tier 0" <<<"$OUT"; then
  pass "simai-tier0 --help"
else
  fail "simai-tier0 --help: $OUT"
fi

fi

# 4) 볼륨·가중치·해시·GPU 문제는 Tier 0 를 띄우지 않고 명확한 코드로 끝난다
expect(){  # 이름 기대코드 기대문구 docker-run-인자...
  local name=$1 code=$2 text=$3; shift 3
  local out rc
  out=$(docker run --rm "$@" 2>&1); rc=$?
  if [ "$rc" -eq "$code" ] && grep -q "$text" <<<"$out" && ! grep -q "아직 조립하지 않았다" <<<"$out"; then
    pass "$name → 종료 코드 $rc"
  else
    fail "$name: 종료 코드 $rc (기대 $code, 문구 '$text')"; tail -5 <<<"$out" | sed 's/^/      /'
  fi
}
# 가짜 nvidia-smi(H100 한 줄을 출력)로 GPU 단계를 넘겨 가중치 단계를 본다. HF 주소는 닫힌 포트라 실제로 받지 않는다(실험 이미지·통합 이미지 장면 모드)
FAKE_GPU=(-e "SIMAI_NVIDIA_SMI=echo NVIDIA H100 80GB HBM3, 9.0, 81559, 580.65.06" -e HF_ENDPOINT=http://127.0.0.1:9)
# 기본 진입점 simai-bootstrap(S15P31A101-205): 볼륨이 없으면 컨테이너 디스크를 쓰고, GPU 가 없으면 가중치를 받기 전에 코드 3
if [ "$T0LIKE" = 1 ]; then
expect "simai-bootstrap GPU 없음" 3 "GPU 없음" "$IMG"
# 아래는 simai-tier0(엔진 준비 진입점)를 직접 부른다: 부트스트랩 뒤 단계의 볼륨·가중치·해시·GPU 처리
T0=(--entrypoint /usr/local/bin/simai-tier0)
expect "볼륨 없음" 2 "Network Volume" "${T0[@]}" "$IMG"
docker volume create "$VOL" >/dev/null
docker run --rm -v "$VOL:/workspace" --entrypoint /bin/sh "$IMG" -c 'mkdir -p /workspace/simai/weights/yolo' >/dev/null
expect "가중치 없음" 2 "가중치 없음" -v "$VOL:/workspace" "${T0[@]}" "$IMG"
docker run --rm -v "$VOL:/workspace" --entrypoint /bin/sh "$IMG" \
  -c 'printf "not-real-weights" > /workspace/simai/weights/yolo/yolo26x-pose.pt' >/dev/null
expect "가중치 해시 불일치" 2 "해시 불일치" -v "$VOL:/workspace" \
  -e SIMAI_TIER0_WEIGHTS_SHA256=0000000000000000000000000000000000000000000000000000000000000000 "${T0[@]}" "$IMG"
expect "GPU 없음(가중치는 있음)" 3 "CUDA GPU" -v "$VOL:/workspace" "${T0[@]}" "$IMG"
elif [ "$KIND" = risk_engine ]; then
  UID_IN=$(docker run --rm --entrypoint id "$IMG" -u 2>&1)
  if [ "$UID_IN" != 0 ] && [ -n "$UID_IN" ]; then pass "비루트 사용자 (uid $UID_IN)"; else fail "루트로 돈다: uid $UID_IN"; fi
  PORTS=$(docker image inspect --format '{{json .Config.ExposedPorts}}' "$IMG")
  if [ "$PORTS" = null ] || [ "$PORTS" = "{}" ]; then pass "EXPOSE 없음(공개 포트 없음)"; else fail "EXPOSE: $PORTS"; fi
  if docker image inspect --format '{{json .Config.Healthcheck.Test}}' "$IMG" | grep -q healthz; then
    pass "헬스체크 /healthz"
  else
    fail "헬스체크가 없다"
  fi
  if OUT=$(docker run --rm "$IMG" python -m services.risk_engine --help 2>&1) && grep -q "plan/evaluate" <<<"$OUT"; then
    pass "python -m services.risk_engine --help"
  else
    fail "services.risk_engine --help: $(tail -3 <<<"$OUT")"
  fi
  # 공통 테스트 벡터(D-G3-04): 이미지 안의 Risk Engine 라이브러리로 저장소 벡터 전부
  if OUT=$(docker run --rm "$IMG" python -m simai_ai.risk.vectors /opt/simai/vectors 2>&1); then
    pass "공통 테스트 벡터 $(tail -1 <<<"$OUT")"
  else
    fail "공통 테스트 벡터: $(grep -E 'FAIL|통과' <<<"$OUT" | tail -5)"
  fi
  # 망 없이 띄워(밖으로 나갈 수 없음) healthy 가 된 뒤 컨테이너 안에서 계약 예시를 HTTP 로 왕복한다
  docker run -d --name "$VOL" --network none "$IMG" >/dev/null
  STATE=starting
  for _ in $(seq 1 60); do
    STATE=$(docker inspect --format '{{.State.Health.Status}}' "$VOL" 2>/dev/null || echo gone)
    if [ "$STATE" = healthy ] || [ "$STATE" = unhealthy ] || [ "$STATE" = gone ]; then break; fi
    sleep 1
  done
  if [ "$STATE" = healthy ]; then pass "망 없는 컨테이너 healthy"; else fail "컨테이너 상태 $STATE"; docker logs "$VOL" 2>&1 | tail -5 | sed 's/^/      /'; fi
  docker cp contracts/examples "$VOL:/tmp/examples" >/dev/null && docker cp deploy/scripts/risk_plan_roundtrip.py "$VOL:/tmp/" >/dev/null
  if OUT=$(docker exec "$VOL" python /tmp/risk_plan_roundtrip.py --url http://127.0.0.1:8080 --examples /tmp/examples 2>&1); then
    pass "계약 예시 HTTP 왕복 ($(tail -1 <<<"$OUT"))"
  else
    fail "계약 예시 HTTP 왕복:"; while IFS= read -r line; do echo "      $line"; done <<<"$(tail -12 <<<"$OUT")"
  fi
  docker rm -f "$VOL" >/dev/null 2>&1
else
  expect "simai-bootstrap check GPU 없음" 3 "GPU 없음" "$IMG" check
  expect "simai-bootstrap run 명령 없음" 64 "사용법" "$IMG" run
  if [ "$KIND" = exp02 ]; then
    # 해시를 모르는 항목(빈 sha256)이 있으면 아무것도 받기 전에 그 이름으로 멈추고, 다 채워졌으면 받기 실패(닫힌 포트)로 멈춘다
    if docker run --rm --entrypoint python "$IMG" -c 'import json,sys; d=json.load(open("/opt/simai/deploy/weights.json")); sys.exit(0 if any("exp02" in w["sets"] and not w["sha256"] for w in d["weights"]) else 1)'; then
      expect "exp02 묶음: 해시 모르는 항목은 받기 전에 코드 2" 2 "목록에 sha256 이 없어" "${FAKE_GPU[@]}" "$IMG" check
    else
      expect "exp02 묶음: 받기 실패는 코드 2" 2 "받기 실패" "${FAKE_GPU[@]}" -e SIMAI_ROOT=/tmp/simai "$IMG" check
    fi
    expect "simai-exp02 체크포인트 없음" 2 "체크포인트 없음" -e SIMAI_ROOT=/tmp/simai --entrypoint /usr/local/bin/simai-exp02 "$IMG" true
  else
    expect "static 묶음: map-anything 가중치 받기 실패는 코드 2" 2 "map-anything-apache-model 받기 실패" "${FAKE_GPU[@]}" "$IMG" check
  fi
fi
# 이미지에 들어간 실행 스크립트에 CR 이 없다(Windows checkout 의 'python3\r' 실패 재발 방지). Risk Engine 이미지에는 실행 스크립트가 없다
if [ "$KIND" = risk_engine ]; then :
elif CR=$(docker run --rm --entrypoint /bin/sh "$IMG" -c "grep -l \$(printf '\\r') /usr/local/bin/simai-* || true") && [ -z "$CR" ]; then
  pass "실행 스크립트 LF (simai-start·bootstrap·tier0·wait-tier0)"
else
  fail "CR 이 든 실행 스크립트: $CR"
fi

# 5) GPU 아키텍처마다 엔진 경로가 다르다 (같은 볼륨에서 A40 캐시를 H100 이 고르지 않는다)
if [ "$T0LIKE" = 1 ]; then
plan(){ docker run --rm -v "$VOL:/workspace" "$IMG" python -m simai_ai.detection.engine_cache plan \
  --weights /workspace/simai/weights/yolo/yolo26x-pose.pt --cache-root /workspace/simai/engines \
  --gpu-arch "$1" --tensorrt 11.4.0.106 --ultralytics 8.4.178 2>&1; }
P90=$(plan sm_90); P86=$(plan sm_86)
if [ "$P90" != "$P86" ] && grep -q "/sm_90/trt" <<<"$P90" && grep -q "/sm_86/trt" <<<"$P86"; then
  pass "엔진 경로 sm_90 ≠ sm_86 (${P90#*	})"
else
  fail "엔진 경로가 GPU 별로 나뉘지 않는다: $P90 / $P86"
fi
fi

# 5b) 통합 이미지(S15P31A101-246): EGL 렌더와 감시 프로세스 기동·종료 순서(가짜 Tier 0·vLLM·워커, GPU 없음)
if [ "$KIND" = unified ]; then
  hostpath(){ if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }  # Git Bash: docker.exe 는 윈도 경로를 받는다
  # GPU 가 없는 곳에는 NVIDIA EGL 이 없어, 버리는 컨테이너에만 Mesa 소프트웨어 EGL 을 깔아 pyrender EGL 경로가 실제로 그리는지 본다(이미지에는 넣지 않음)
  cat > "${TMPDIR:-/tmp}/simai-egl-$$.py" <<'PY'
import os, numpy as np, pyrender, trimesh
s = pyrender.Scene(bg_color=[0, 0, 0, 1], ambient_light=[1.0, 1.0, 1.0])
s.add(pyrender.Mesh.from_trimesh(trimesh.creation.box()))
s.add(pyrender.PerspectiveCamera(yfov=1.0), pose=np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 3], [0, 0, 0, 1]], dtype=float))
r = pyrender.OffscreenRenderer(64, 64)
c, d = r.render(s)
r.delete()
assert c[32, 32].any() and not c[0, 0].any() and d[32, 32] > 0, (c[32, 32], c[0, 0])
print("EGL", os.environ.get("PYOPENGL_PLATFORM"), c.shape)
PY
  docker create --name "$VOL" --entrypoint bash "$IMG" -c \
    'apt-get update -qq >/dev/null 2>&1 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends libegl-mesa0 libgl1-mesa-dri >/dev/null 2>&1 || exit 77; python /tmp/egl.py' >/dev/null
  docker cp "$(hostpath "${TMPDIR:-/tmp}/simai-egl-$$.py")" "$VOL:/tmp/egl.py" >/dev/null
  OUT=$(docker start -a "$VOL" 2>&1); RC=$?
  docker rm -f "$VOL" >/dev/null 2>&1; rm -f "${TMPDIR:-/tmp}/simai-egl-$$.py"
  if [ "$RC" -eq 0 ]; then
    pass "pyrender EGL 렌더(버리는 컨테이너의 Mesa EGL, $(tail -1 <<<"$OUT"))"
  elif [ "$RC" -eq 77 ]; then
    echo "SKIP  pyrender EGL 렌더: Mesa 를 받지 못함(망). GPU 에서는 세션 u1 이 본다"
  else
    fail "pyrender EGL 렌더: $(tail -5 <<<"$OUT")"
  fi
  # 감시 프로세스: 저장소의 가짜 프로세스(ai/tests/deploy/fake_service.py)로 Tier 0 → vLLM(/health) → 워커, docker stop 에 워커 → vLLM → Tier 0
  SUP=$(mktemp -d)
  cp ai/tests/deploy/fake_service.py "$SUP/"
  cat > "$SUP/run.sh" <<'RUN'
set -e
cd /tmp/sup
printf 'fake-yolo' > t0.pt
printf 'fake-vlm' > vlm.bin
python - <<'PY'
import hashlib, json
h = lambda f: hashlib.sha256(open(f, "rb").read()).hexdigest()
json.dump({"weights": [
    {"name": "yolo", "sets": ["tier0"], "path": "weights/yolo/yolo26x-pose.pt", "source": {"url": "file:///tmp/sup/t0.pt"},
     "sha256": h("t0.pt"), "env": "SIMAI_TIER0_WEIGHTS"},
    {"name": "fakevlm", "sets": ["fakevlm"], "path": "weights/cosmos3-nano/model.bin", "source": {"url": "file:///tmp/sup/vlm.bin"},
     "sha256": h("vlm.bin")}]}, open("w.json", "w"))
PY
exec simai-bootstrap tier0 --config c.json
RUN
  docker create --name "$VOL" -e SIMAI_ROOT=/tmp/sup/simai -e "SIMAI_NVIDIA_SMI=echo NVIDIA H100 80GB HBM3, 9.0, 81559, 580.65.06" \
    -e SIMAI_WEIGHTS_MANIFEST=/tmp/sup/w.json -e SIMAI_WORKER_WEIGHTS_SET=fakevlm -e SIMAI_MIN_FREE_GB=0 -e SIMAI_SUPERVISE_TICK_S=0.2 \
    -e "SIMAI_TIER0_CMD=python /tmp/sup/fake_service.py tier0" -e "SIMAI_VLLM_CMD=python /tmp/sup/fake_service.py vllm" \
    -e "SIMAI_WORKER_CMD=python /tmp/sup/fake_service.py worker" -e FAKE_EVENTS=/tmp/sup/events.txt \
    -e HF_TOKEN=hf_checkimageFakeToken0000000000 "$IMG" bash /tmp/sup/run.sh >/dev/null
  docker cp "$(hostpath "$SUP")/." "$VOL:/tmp/sup" >/dev/null && docker start "$VOL" >/dev/null
  for _ in $(seq 1 120); do
    if docker exec "$VOL" cat /tmp/sup/events.txt 2>/dev/null | grep -q '^worker start'; then break; fi
    sleep 1
  done
  docker stop -t 60 "$VOL" >/dev/null
  CODE=$(docker inspect --format '{{.State.ExitCode}}' "$VOL")
  docker cp "$VOL:/tmp/sup/events.txt" "$(hostpath "$SUP")/events.txt" >/dev/null 2>&1
  LOGS=$(docker logs "$VOL" 2>&1)
  docker rm -f "$VOL" >/dev/null 2>&1
  STARTS=$(awk '$2 == "start" { printf "%s ", $1 }' "$SUP/events.txt" 2>/dev/null)
  TERMS=$(awk '$2 == "term" { printf "%s ", $1 }' "$SUP/events.txt" 2>/dev/null)
  TOKENS=$(awk '$2 == "start" && $3 != "clean"' "$SUP/events.txt" 2>/dev/null)
  if [ "$STARTS" = "tier0 vllm worker " ] && [ "$TERMS" = "worker vllm tier0 " ] && [ -z "$TOKENS" ] && [ "$CODE" = 0 ] \
     && grep -q '^\[worker\] worker says hello' <<<"$LOGS" && ! grep -q 'hf_checkimage' <<<"$LOGS"; then
    pass "감시 프로세스: 기동 ${STARTS}/ 종료 ${TERMS}/ 자식 토큰 없음, 종료 코드 0"
  else
    fail "감시 프로세스: 기동 '$STARTS' 종료 '$TERMS' 코드 $CODE 토큰 '$TOKENS'"; tail -8 <<<"$LOGS" | sed 's/^/      /'
  fi
  rm -rf "$SUP"

  # 5c) MapAnything(S15P31A101-250·-566): DINOv2 허브 코드 사본으로 망 없이(HTTP 504 상황 포함) source="local" 골격을 만든다
  if OUT=$(docker run --rm --network none --entrypoint python "$IMG" -c '
import urllib.error, urllib.request, torch
from simai_ai.scene.run_mapanything import resolve_local_hub_dir, use_local_dinov2_hub
assert resolve_local_hub_dir("facebookresearch/dinov2") is not None
def _boom(*a, **k):
    raise urllib.error.HTTPError("https://github.com/facebookresearch/dinov2", 504, "Gateway Time-out", {}, None)
urllib.request.urlopen = _boom
use_local_dinov2_hub()
m = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14", pretrained=False)
n = sum(p.numel() for p in m.parameters())
assert n > 2e7, n
print("dinov2_vits14", n)' 2>&1); then
    pass "DINOv2 허브 사본으로 망 없이 골격 생성 ($(tail -1 <<<"$OUT"))"
  else
    fail "DINOv2 허브 사본: $(tail -3 <<<"$OUT")"
  fi
  # 장면 준비 모드(simai-bootstrap scene): 명령 없음 64, LIVE 표시 파일이 새것이면 받기 전에 75, 묶음은 static(받기 실패 2),
  # 가짜 GPU(사용 중 0 MiB)·file:// 가중치로 명령 종료 코드 7 이 그대로·자식 토큰 없음, 사용 중 2048 MiB 면 75
  expect "scene 명령 없음" 64 "사용법" "$IMG" scene
  expect "scene LIVE 표시 파일 → 받기 전에 거부" 75 "LIVE 서비스가 떠 있다" -e SIMAI_ROOT=/tmp/s --entrypoint /bin/sh "$IMG"     -c 'mkdir -p /tmp/s/state && echo "{}" > /tmp/s/state/tier0.live && exec simai-bootstrap scene true'
  expect "scene 묶음 static: map-anything 받기 실패는 코드 2" 2 "map-anything-apache-model 받기 실패" "${FAKE_GPU[@]}" "$IMG" scene true
  SCN=$(mktemp -d)
  cp ai/tests/experiments/gpu_queue/fake_nvidia_smi.py "$SCN/"
  cat > "$SCN/run.sh" <<'RUN'
cd /tmp/scn
printf 'fake-map-anything' > ma.bin
echo 0 > used.txt
python - <<'PY'
import hashlib, json
json.dump({"weights": [{"name": "ma", "sets": ["static"], "path": "weights/map-anything/model.safetensors",
                        "source": {"url": "file:///tmp/scn/ma.bin"}, "sha256": hashlib.sha256(open("ma.bin", "rb").read()).hexdigest()}]},
          open("w.json", "w"))
PY
simai-bootstrap scene -- python -c 'import os, sys; sys.exit(9 if {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"} & set(os.environ) else 7)'
echo "scene-rc=$?"
echo 2048 > used.txt
simai-bootstrap scene true
echo "scene-busy=$?"
# 실행 중 Tier 0 시작(Codex 리뷰 R1): 장면 작업을 자식으로 띄운 뒤 Tier 0 시작 경로(시작 잠금 → 장면 대기)를 부르면 장면 그룹이 끝나고 75, Tier 0 는 진행
echo 0 > used.txt
simai-bootstrap scene -- python -c 'import os, time; open("/tmp/scn/child.pid", "w").write(str(os.getpid())); time.sleep(120)' &
SCENE_PID=$!
for _ in $(seq 1 150); do [ -s child.pid ] && break; sleep 0.2; done
python - <<'PY'
import importlib.machinery, importlib.util, os, pathlib
loader = importlib.machinery.SourceFileLoader("bs", "/usr/local/bin/simai-bootstrap")
bs = importlib.util.module_from_spec(importlib.util.spec_from_loader("bs", loader))
loader.exec_module(bs)
root = pathlib.Path("/tmp/scn/simai")
bs.claim_tier0(root)
bs.wait_scene_released(root)
try:
    os.kill(int(open("/tmp/scn/child.pid").read()), 0)
    print("preempt-child=alive")
except ProcessLookupError:
    print("preempt-child=gone")
PY
wait "$SCENE_PID"
echo "scene-preempt=$?"
# 감시 부모가 멈춤(SIGSTOP) → Tier 0 시작 경로가 시한(20초) 뒤 장면 그룹을 직접 회수, 감시 부모가 죽음(SIGKILL) → PDEATHSIG 로 장면 리더가 같이 끝남
reap_probe(){  # $1 = stop|kill
  rm -f child.pid
  simai-bootstrap scene -- python -c 'import os, time; open("/tmp/scn/child.pid", "w").write(str(os.getpid())); time.sleep(120)' &
  MON=$!
  for _ in $(seq 1 150); do [ -s child.pid ] && break; sleep 0.2; done
  if [ "$1" = stop ]; then kill -STOP "$MON"; else kill -KILL "$MON"; sleep 2; fi
  python - "$1" <<'PY'
import importlib.machinery, importlib.util, os, pathlib, sys, time
loader = importlib.machinery.SourceFileLoader("bs", "/usr/local/bin/simai-bootstrap")
bs = importlib.util.module_from_spec(importlib.util.spec_from_loader("bs", loader))
loader.exec_module(bs)
root = pathlib.Path("/tmp/scn/simai")
pid = int(open("/tmp/scn/child.pid").read())
if sys.argv[1] == "kill":  # PDEATHSIG: Tier 0 를 부르기 전에 이미 끝났어야 한다
    print("pdeath-child=" + ("alive" if bs._proc_alive(pid) else "gone"))
bs.claim_tier0(root)
bs.wait_scene_released(root)
bs.release_tier0_claim()
print(f"reap-{sys.argv[1]}-child=" + ("alive" if bs._proc_alive(pid) else "gone"))
PY
  kill -KILL "$MON" 2>/dev/null; wait "$MON" 2>/dev/null
}
reap_probe stop
reap_probe kill
RUN
  docker create --name "$VOL" -e SIMAI_ROOT=/tmp/scn/simai -e "SIMAI_NVIDIA_SMI=python /tmp/scn/fake_nvidia_smi.py /tmp/scn/used.txt"     -e SIMAI_WEIGHTS_MANIFEST=/tmp/scn/w.json -e SIMAI_MIN_FREE_GB=0 -e HF_TOKEN=hf_checkimageFakeToken0000000000     --entrypoint bash "$IMG" /tmp/scn/run.sh >/dev/null
  docker cp "$(hostpath "$SCN")/." "$VOL:/tmp/scn" >/dev/null
  OUT=$(docker start -a "$VOL" 2>&1)
  docker rm -f "$VOL" >/dev/null 2>&1; rm -rf "$SCN"
  if grep -q '^scene-rc=7$' <<<"$OUT" && grep -q '^scene-busy=75$' <<<"$OUT" && ! grep -q 'hf_checkimage' <<<"$OUT" \
     && grep -q '^preempt-child=gone$' <<<"$OUT" && grep -q '^scene-preempt=75$' <<<"$OUT" \
     && grep -q '^reap-stop-child=gone$' <<<"$OUT" && grep -q '^pdeath-child=gone$' <<<"$OUT" && grep -q '^reap-kill-child=gone$' <<<"$OUT"; then
    pass "장면 준비 모드: 명령 종료 코드 7 그대로, 자식 토큰 없음, GPU 사용 중 2048 MiB 면 75, 실행 중 Tier 0 시작 → 장면 그룹 종료·75·Tier 0 진행, 감시 부모 정지 → Tier 0 가 그룹 회수, 감시 부모 소멸 → PDEATHSIG"
  else
    fail "장면 준비 모드: $(grep -E 'scene-|preempt-|reap-|pdeath-|거부|오류' <<<"$OUT" | tail -4 | tr '
' ' ')"
  fi
fi

# 6) SSH: PUBLIC_KEY(RunPod)·SSH_PUBLIC_KEY(Vast.ai) 를 주면 키 인증만 받는 sshd 가 뜬다(호스트 키는 이때 만든다). Risk Engine 이미지는 sshd 가 없다
if [ "$KIND" != risk_engine ]; then
KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEZha2VLZXlGb3JDaGVja09ubHkwMDAwMDAwMDAwMDA check"
SSH_PROBE='pgrep -x sshd >/dev/null && echo running; sshd -T 2>/dev/null | grep -Ei "^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) "'
for VAR in PUBLIC_KEY SSH_PUBLIC_KEY; do
  OUT=$(docker run --rm -e "$VAR=$KEY" "$IMG" bash -c "$SSH_PROBE" 2>/dev/null)
  if grep -q running <<<"$OUT" && grep -qi "^passwordauthentication no" <<<"$OUT" \
     && grep -qi "^kbdinteractiveauthentication no" <<<"$OUT" && grep -qi "^permitrootlogin without-password" <<<"$OUT"; then
    pass "SSH($VAR): sshd 기동, 비밀번호·키보드 인증 끔"
  else
    fail "SSH($VAR) 설정: $(tr '\n' ' ' <<<"$OUT")"
  fi
done
# 엘리스 GPU VM: 호스트 sshd 를 쓰므로 SIMAI_SSHD=off 면 키가 있어도 컨테이너 sshd 를 띄우지 않는다
OUT=$(docker run --rm -e "PUBLIC_KEY=$KEY" -e SIMAI_SSHD=off "$IMG" bash -c 'pgrep -x sshd >/dev/null && echo running || echo none' 2>/dev/null)
if [ "$OUT" = none ]; then pass "SIMAI_SSHD=off: 컨테이너 sshd 없음"; else fail "SIMAI_SSHD=off 인데 sshd: $OUT"; fi
# 막힌 망 예비 SSH(S15P31A101-286): 고정 판 wstunnel 이 PATH 에 있고, 공개키·경로 비밀값을 주면 sshd 뒤에 뜨며 비밀값은 명령줄·출력·넘겨받은 명령에 없다
OUT=$(docker run --rm --entrypoint wstunnel "$IMG" --version 2>&1)
if grep -q '10\.7\.1' <<<"$OUT"; then pass "wstunnel 10.7.1"; else fail "wstunnel 판: $OUT"; fi
TP="check${RANDOM}Prefix0123456789abcdefghijklmn"
WST_PROBE='sleep 1; P=$(pgrep -x wstunnel) && echo running && tr "\0" " " </proc/$P/cmdline; echo; echo "child=${SIMAI_TUNNEL_PREFIX:-}"'
OUT=$(docker run --rm -e "PUBLIC_KEY=$KEY" -e "SIMAI_TUNNEL_PREFIX=$TP" "$IMG" bash -c "$WST_PROBE" 2>&1)
if grep -q '^running' <<<"$OUT" && grep -q 'ws://0.0.0.0:8080' <<<"$OUT" && ! grep -qF "$TP" <<<"$OUT"; then
  pass "wstunnel: sshd 뒤 기동, 비밀값은 명령줄·출력·자식 환경에 없음"
else
  fail "wstunnel 기동: $(tr '\n' ' ' <<<"${OUT//$TP/<비밀값>}")"
fi
# 막힌 망 I0 예비 경로(S15P31A101-288): SIMAI_TUNNEL_ALLOW 의 포트는 127.0.0.1 로만 더해진다
OUT=$(docker run --rm -e "PUBLIC_KEY=$KEY" -e "SIMAI_TUNNEL_PREFIX=$TP" -e SIMAI_TUNNEL_ALLOW=17000,17001 "$IMG" bash -c "$WST_PROBE" 2>&1)
if grep -q -- '--restrict-to 127.0.0.1:22 --restrict-to localhost:22 --restrict-to 127.0.0.1:17000 --restrict-to 127.0.0.1:17001 ws://0.0.0.0:8080' <<<"$OUT" \
   && ! grep -qF "$TP" <<<"$OUT"; then
  pass "wstunnel: SIMAI_TUNNEL_ALLOW 포트를 127.0.0.1 로만 더함"
else
  fail "wstunnel 허용 대상: $(tr '\n' ' ' <<<"${OUT//$TP/<비밀값>}")"
fi
fi

echo
if [ "$FAILS" -eq 0 ]; then echo "check_image: 모두 통과 ($IMG)"; else echo "check_image: 실패 $FAILS 건 ($IMG)"; fi
exit "$FAILS"
