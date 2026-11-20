#!/usr/bin/env python3
"""simai-gpu-boot: GPU 를 켜면 사람 손 없이 관제 준비까지 가는 부팅 (S15P31A101-700, 설계 docs/design/S15P31A101-700-gpu-auto-boot.md).

    simai-gpu-boot auto          이미지 기본 CMD(simai-bootstrap auto)가 부팅 비밀값 환경 변수를 보면 이것을 exec 한다.
    simai-gpu-boot seed-reid     지금 Pod 의 $SIMAI_ROOT/reid(손으로 올린 COPE·CLIP)를 GPU 캐시에 올린다(첫 한 번, 노트북 업로드 대체)
    simai-gpu-boot --help        이 설명

auto 순서 (공급자 무관: RunPod 은 RUNPOD_*, AWS·그 밖은 SIMAI_* 이름. 모르는 값은 null):
  1) 비밀 값(부팅 비밀값·TLS 비밀 키·HF 토큰)을 환경에서 꺼내 이 프로세스에만 든다(자식 환경·명령줄·로그에 없음, 로그는 가린다).
     AWS(S15P31A101-703)는 SIMAI_SECRETS_SOURCE=aws-secretsmanager 면 인스턴스 역할(IMDSv2) 자격으로 Secrets Manager 의 JSON 비밀
     SIMAI_AWS_SECRET_ID 를 받아 채운다(표준 라이브러리 SigV4, aws cli·boto 없음). user-data·환경 변수에 원문이 없다.
  2) GPU(nvidia-smi)·공급자 사실·TLS 키(공급자 비밀 저장소의 고정 키, 노트북 고정 지문 그대로) → POST /api/gpu/sessions(gpu_session_register).
  3) 응답 토큰을 권한 600 파일(<SIMAI_ROOT>/../.simai-private/gpu-session/)에 쓰고 보고기를 띄운다: 단계가 바뀔 때와 report.interval_ms 마다
     gpu_session_report(WEIGHTS → ENGINE → TIER0 → VLLM → READY, 실패는 FAILED + reason_code). 409 GPU_SESSION_ENDED 면 서비스를 멈춘다.
  4) 시작 묶음: start.config.calibration 을 받아 sha256 확인 → calibration.json, start.laptop_token_sha256 → laptop_token.sha256, TLS → tls/.
     start.config 가 null(구성 묶음 없음 = 세팅부터)이면 Tier 0 를 띄울 보정이 없다: 가중치·엔진만 예열하고 FAILED NO_START_CONFIG 로 손 경로를 기다린다.
  5) GPU 캐시(S3, 워커 토큰 서명 /api/worker/gpu-cache/sign): TensorRT 엔진·Re-ID 는 받아서 Tier 0 전에 놓고, vLLM 컴파일 캐시는 뒤에서 받는다.
     없으면 이번에 만든 것을 READY 뒤에 올린다(같은 키는 덮어쓰지 않는다).
  6) simai-bootstrap preflight --sets tier0(Tier 0 가중치·GPU 예산·런타임) → simai-bootstrap lobby ... 를 자식으로 띄우고 단계를 본다.
     세팅용·VLM 가중치는 lobby 감시 프로세스가 Tier 0 준비 뒤 뒤에서 받는다(Tier 0 가중치 먼저, 엔진 빌드 바로).
  7) 스스로 멈출 때(SIGTERM) stage ENDED. 실패하면 컨테이너를 끝내지 않고 FAILED 를 계속 보고하며 기다린다(공급자가 컨테이너를 다시 띄우면
     새 등록이 진짜 세션을 밀어내므로). 이 인스턴스 세션이 끝났다는 기록(SIMAI_GPU_BOOT_STATE_DIR, 컨테이너 디스크)이 있으면 다시 등록하지 않는다.

종료 코드: 0 정상 정지(SIGTERM), 64 사용법·설정 오류(부팅 비밀값은 있는데 ID·EC2 주소가 없음 등). 그 밖의 실패는 끝내지 않고 기다린다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
import re
import shlex
import shutil
import signal
import ssl
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK, EXIT_USAGE = 0, 64
BOOT_SECRET_ENV = "SIMAI_GPU_BOOT_SECRET"  # 공급자 비밀 저장소(RunPod {{ RUNPOD_SECRET_simai_gpu_boot_secret }})
BOOT_ID_ENV = "SIMAI_GPU_BOOT_ID"  # 비밀 아님(admin gpu-boot register --id 값)
EC2_ENV = "SIMAI_EC2_BASE_URL"
PROVIDER_ENV = "SIMAI_GPU_PROVIDER"  # RUNPOD | AWS | MANUAL (없으면 RUNPOD_POD_ID 가 있으면 RUNPOD, 아니면 MANUAL)
TLS_CERT_ENV = "SIMAI_TIER0_TLS_CERT_PEM"  # Tier 0 TLS 인증서(공개, PEM 또는 PEM 의 base64 한 줄)
TLS_KEY_ENV = "SIMAI_TIER0_TLS_PRIVATE_KEY"  # Tier 0 TLS 비밀 키(공급자 비밀, PEM 또는 PEM 의 base64 한 줄)
PIN_ENV = "SIMAI_TIER0_PINNED_SPKI_SHA256"  # 선택: 노트북 고정 지문과 같은지 기동 전에 확인
SECRET_ENVS = (BOOT_SECRET_ENV, TLS_KEY_ENV)
SECRETS_SOURCE_ENV = "SIMAI_SECRETS_SOURCE"  # aws-secretsmanager(S15P31A101-703). 없으면 환경 변수(RunPod Secret 등)
AWS_SECRETS_SOURCE = "aws-secretsmanager"
AWS_SECRET_ID_ENV = "SIMAI_AWS_SECRET_ID"  # 비밀 아님(Secrets Manager 이름 또는 ARN). 값은 JSON {환경 변수 이름: 값}
# Secrets Manager JSON 에서 받는 이름. 인증서는 공개 값이지만 키와 한 묶음으로 둘 수 있게 받는다(환경에 이미 있으면 환경이 먼저)
AWS_SECRET_KEYS = (BOOT_SECRET_ENV, TLS_KEY_ENV, "HF_TOKEN")
AWS_PUBLIC_KEYS = ("SIMAI_TIER0_TLS_CERT_PEM",)
DEFAULT_IMDS = "http://169.254.169.254"
HF_ENVS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")
CONTROL_PORT, VIDEO_PORT = 17000, 17001  # RunPod 템플릿에 노출한 TCP(deploy/README "RunPod 템플릿")
DEFAULT_ADVERTISE_HOST = "127.0.0.1"  # SSAFY 망 기본 경로: 노트북이 EC2 SSH 로컬 포워드로 붙는다(deploy/README, 14차 start-lobby 와 같음)
SCHEMA_VERSION = "1.0.0"
REPORT_SCHEMA_VERSION = "1.3.0"  # gpu_session_report: 선택 host_cpu(S15P31A101-740, 1.1.0)·disabled_features(S15P31A101-747, 1.2.0)·disabled_notices(S15P31A101-756, 1.3.0) 추가
BENCH_BUDGET_S = 1.5  # 단일 스레드 기준 측정 시간 상한(합쳐 2 초 이내, 설계 docs/design/S15P31A101-740-gpu-host-cpu-check.md)
MAX_DISABLED = 20  # 계약 disabled_features 항목 수 상한
WARMUP_TIMEOUT_S = 120.0  # vLLM 더미 멀티모달 요청 1회의 응답 대기 상한(S15P31A101-747)
STAGES = ("WEIGHTS", "ENGINE", "TIER0", "VLLM", "READY", "FAILED", "ENDED")
# 자동 부팅 기본 서비스(80GB급 이상) = lobby 기본(simai_bootstrap.LOBBY_SERVICES) + ppe(S15P31A101-718). 작업 설정의 안전모·조끼 필수를
# 셀프서비스 Pod 에서도 감시한다. 손 lobby·preflight 기본은 그대로 둔다(ppe 명시 선택, -507). ppe 는 몫 0.05(Tier 0 0.20 + 분석 0.70 + 0.05 = 0.95)
BOOT_SERVICES = "tier0,vllm,worker,avatar,ppe"
# ppe 를 자동으로 더하는 VRAM 하한(MiB). 48GB급(약 46,000 MiB)은 0.95 를 다 쓰면 남는 여유가 2 GiB 남짓이라 lobby 기본(ppe 없음)을 쓴다.
# H100 80GB(81,559)·96GB급(RTX PRO 6000 등 약 97,000)은 켠다
PPE_MIN_VRAM_MB = 70_000
CACHE_PATH = "/api/worker/gpu-cache/sign"
REGISTER_PATH = "/api/gpu/sessions"
MAX_CALIBRATION_BYTES = 16 << 20
MAX_CACHE_BYTES = 4 << 30  # 서버 GpuCacheService.MAX_BYTES 와 같다
# Re-ID 기본 해시(ai/src/simai_ai/reid/embedder.py 의 DEFAULT_COPE_WEIGHTS_SHA256·DEFAULT_CLIP_VIT_B_16_SHA256 와 같아야 한다, 시험이 대조)
COPE_WEIGHTS_SHA256 = "3cf24e2e542b0465c9e0ad7a0804eb2ae80a16815b3441142a923c29b0e42e9b"
CLIP_SHA256 = "5806e77cd80f8b59890b7e101eabd078d9fb84e6937f9e85e4ecb61988df416f"
REID_FILES = ("weights/cope_msmt17.pth", "clip-cache/ViT-B-16.pt")  # 이것들과 cope/ 가 있어야 Re-ID 묶음으로 본다
REID_OPTIONAL = ("insightface",)
_SHA_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")

_secrets: set[str] = set()
_out = threading.Lock()


def redact(text: str) -> str:
    for s in sorted(_secrets, key=len, reverse=True):
        if s:
            text = text.replace(s, "***")
    text = re.sub(r"hf_[A-Za-z0-9]{20,}", "hf_***", text)
    # 서명 주소의 서명 값(쿼리)은 남기지 않는다
    return re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']*", r"\1?***", text)


def log(msg: str) -> None:
    with _out:
        print(f"simai-gpu-boot: {redact(msg)}", file=sys.stderr, flush=True)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class BootError(Exception):
    """기동 실패. reason 은 gpu_session_report reason_code(^[A-Z][A-Z0-9_]{0,63}$)"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------- 비밀 값


def take_secrets(env: dict[str, str]) -> dict[str, str | None]:
    """비밀 값을 환경에서 꺼낸다(이 뒤의 자식은 받지 않는다). HF 토큰은 가중치를 받는 자식(preflight·lobby)에만 다시 넘긴다."""
    taken: dict[str, str | None] = {}
    for k in (*SECRET_ENVS, *HF_ENVS):
        v = env.pop(k, None)
        taken[k] = v or None
        if v and k != "HUGGING_FACE_HUB_TOKEN":
            _secrets.add(v)
    return taken


# ---------------------------------------------------------------- AWS Secrets Manager (S15P31A101-703)


def sigv4_headers(method: str, host: str, path: str, query: str, headers: dict[str, str], payload: bytes, *, access_key: str,
                  secret_key: str, region: str, service: str, amz_date: str, session_token: str | None = None) -> dict[str, str]:
    """AWS 서명 버전 4 머리글(Authorization·X-Amz-Date·X-Amz-Security-Token). query 는 이미 정렬·인코딩된 문자열."""
    h = {k.lower(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    h["host"] = host
    h["x-amz-date"] = amz_date
    if session_token:
        h["x-amz-security-token"] = session_token
    signed = ";".join(sorted(h))
    canonical = "\n".join([method, path, query, "".join(f"{k}:{h[k]}\n" for k in sorted(h)), signed,
                           hashlib.sha256(payload).hexdigest()])
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (amz_date[:8], region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {"X-Amz-Date": amz_date,
           "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"}
    if session_token:
        out["X-Amz-Security-Token"] = session_token
    return out


class AwsTemporary(Exception):
    """아직 안 됨(막 켠 인스턴스에서 IMDS·역할 자격이 늦게 뜸, 망, 5xx): 다시 해 본다."""


def _plain(method: str, url: str, headers: dict[str, str] | None = None, body: bytes | None = None,
           timeout: float = 5.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except OSError as e:
        raise AwsTemporary(type(e).__name__) from e


def imds_credentials(base: str) -> dict[str, str]:
    """IMDSv2 로 인스턴스 역할 임시 자격과 리전을 받는다(키는 이 프로세스 메모리에만)."""
    status, tok = _plain("PUT", f"{base}/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "300"})
    if status != 200:
        raise AwsTemporary(f"IMDS 토큰 HTTP {status}")
    hdr = {"X-aws-ec2-metadata-token": tok.decode().strip()}
    status, role = _plain("GET", f"{base}/latest/meta-data/iam/security-credentials/", hdr)
    if status != 200 or not role.strip():
        raise AwsTemporary(f"인스턴스 역할 없음(HTTP {status}, 시작 템플릿의 IAM 인스턴스 프로파일 확인)")
    name = role.decode().split()[0]
    status, raw = _plain("GET", f"{base}/latest/meta-data/iam/security-credentials/{name}", hdr)
    try:
        doc = json.loads(raw) if status == 200 else {}
    except ValueError:
        doc = {}
    if not isinstance(doc, dict) or not doc.get("AccessKeyId") or not doc.get("SecretAccessKey"):
        raise AwsTemporary(f"역할 자격 HTTP {status}")
    status, region = _plain("GET", f"{base}/latest/meta-data/placement/region", hdr)
    return {"access_key": doc["AccessKeyId"], "secret_key": doc["SecretAccessKey"], "token": doc.get("Token") or "",
            "region": region.decode().strip() if status == 200 else ""}


def fetch_aws_secret(env: dict[str, str]) -> dict[str, str]:
    """Secrets Manager GetSecretValue(JSON 객체 비밀). 거절(4xx)은 BootError, 일시 실패는 AwsTemporary."""
    secret_id = (env.get(AWS_SECRET_ID_ENV) or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9/_+=.@:-]{1,2048}", secret_id):
        raise BootError("BAD_CONFIG", f"{AWS_SECRET_ID_ENV} 가 없거나 형식이 틀리다")
    creds = imds_credentials((env.get("SIMAI_AWS_IMDS_URL") or DEFAULT_IMDS).rstrip("/"))
    _secrets.update({creds["secret_key"], creds["token"]} - {""})
    region = (env.get("SIMAI_AWS_REGION") or creds["region"] or "").strip()
    if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
        raise BootError("BAD_CONFIG", "AWS 리전을 모른다(SIMAI_AWS_REGION)")
    endpoint = (env.get("SIMAI_AWS_SECRETS_ENDPOINT") or f"https://secretsmanager.{region}.amazonaws.com").rstrip("/")
    host = urllib.parse.urlsplit(endpoint).netloc
    body = json.dumps({"SecretId": secret_id}).encode()
    base = {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": "secretsmanager.GetSecretValue"}
    amz_date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    signed = sigv4_headers("POST", host, "/", "", base, body, access_key=creds["access_key"], secret_key=creds["secret_key"],
                           region=region, service="secretsmanager", amz_date=amz_date, session_token=creds["token"] or None)
    status, raw = _plain("POST", endpoint + "/", {**base, **signed}, body, timeout=15.0)
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    kind = str(doc.get("__type") or "").rsplit("#", 1)[-1]
    if status >= 500 or status == 429 or "Throttling" in kind:
        raise AwsTemporary(f"Secrets Manager HTTP {status}")
    if status != 200:
        raise BootError("SECRETS_REJECTED", f"Secrets Manager 거절 HTTP {status} {kind}".strip())
    try:
        values = json.loads(doc.get("SecretString") or "")
    except ValueError:
        values = None
    if not isinstance(values, dict):
        raise BootError("SECRETS_REJECTED", "비밀 값이 JSON 객체가 아니다({환경 변수 이름: 값})")
    return {k: v for k, v in values.items() if k in (*AWS_SECRET_KEYS, *AWS_PUBLIC_KEYS) and isinstance(v, str) and v}


def write_private(path: Path, text: str) -> Path:
    """권한 600 파일(폴더 700). 이미 있으면 내용만 바꾼다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def pem_text(raw: str | None, marker: str) -> str | None:
    """PEM 원문 또는 PEM 의 base64 한 줄(RunPod Secrets 는 여러 줄 값을 다루기 번거롭다). marker 가 없으면 None."""
    if not raw:
        return None
    raw = raw.strip()
    if "-----BEGIN" not in raw:
        try:
            raw = base64.b64decode("".join(raw.split()), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
    raw = raw.replace("\\n", "\n").replace("\r\n", "\n")
    return raw if marker in raw else None


# ---------------------------------------------------------------- simai-bootstrap 재사용(경로로 불러온다)


def load_bootstrap(env: dict[str, str]):
    """같은 폴더의 simai_bootstrap.py(저장소) 또는 이미지의 /usr/local/bin/simai-bootstrap. 저장 위치·SPKI 지문 계산을 같이 쓴다."""
    here = Path(__file__).resolve().parent
    candidates = [env.get("SIMAI_BOOTSTRAP_PY"), str(here / "simai_bootstrap.py"), "/usr/local/bin/simai-bootstrap"]
    for c in candidates:
        if c and Path(c).is_file():
            loader = importlib.machinery.SourceFileLoader("simai_bootstrap_for_gpu_boot", c)
            spec = importlib.util.spec_from_loader(loader.name, loader)
            assert spec is not None
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
    raise BootError("BOOTSTRAP_MISSING", "simai-bootstrap 을 찾지 못함")


# ---------------------------------------------------------------- GPU·공급자 사실


def query_gpu(smi: list[str], env: dict[str, str]) -> dict:
    """nvidia-smi 의 이름·compute_cap·메모리·개수. 최소 사양 판정은 preflight(simai-bootstrap)가 한다(여기서는 등록할 사실만)."""
    try:
        r = subprocess.run([*smi, "--query-gpu=name,compute_cap,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise BootError("GPU_NOT_FOUND", f"nvidia-smi 를 실행하지 못함 ({type(e).__name__})") from e
    rows = [[v.strip() for v in line.split(",")] for line in r.stdout.strip().splitlines() if line.strip()]
    if r.returncode != 0 or not rows or len(rows[0]) < 3 or not rows[0][0]:
        raise BootError("GPU_NOT_FOUND", f"nvidia-smi 종료 {r.returncode}")
    name, cc, mem = rows[0][:3]
    m = re.fullmatch(r"(\d+)\.(\d+)", cc)
    return {"name": name[:128], "arch": f"sm_{m.group(1)}{m.group(2)}" if m else None, "count": len(rows),
            "memory_total_mb": float(mem) if re.fullmatch(r"[0-9.]+", mem) else None}


def _first(env: dict[str, str], *names: str) -> str | None:
    for n in names:
        v = (env.get(n) or "").strip()
        if v:
            return v
    return None


def provider_of(env: dict[str, str]) -> str:
    p = (env.get(PROVIDER_ENV) or "").strip().upper()
    if p:
        if p not in ("RUNPOD", "AWS", "MANUAL"):
            raise BootError("BAD_CONFIG", f"{PROVIDER_ENV}={p!r} 는 RUNPOD·AWS·MANUAL 중 하나다")
        return p
    return "RUNPOD" if env.get("RUNPOD_POD_ID") else "MANUAL"


def public_port(env: dict[str, str], requested: int) -> int | None:
    """밖에서 붙는 포트: 사이트 무관 이름이 먼저(Tier 0 와 같은 순서), 없으면 RunPod 매핑. 모르면 None."""
    raw = _first(env, f"SIMAI_PUBLIC_PORT_{requested}", f"RUNPOD_TCP_PORT_{requested}")
    if raw is None:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise BootError("BAD_CONFIG", f"공개 포트 {requested} 매핑 값이 포트 번호가 아니다: {raw!r}")
    return int(raw)


def register_facts(env: dict[str, str], gpu: dict, control_port: int, video_port: int, spki: str | None) -> dict:
    """gpu_session_register payload. 모르는 값은 null(지어내지 않는다)."""
    provider = provider_of(env)
    instance_id = _first(env, "SIMAI_INSTANCE_ID", "RUNPOD_POD_ID")
    host = _first(env, "SIMAI_PUBLIC_HOST", "RUNPOD_PUBLIC_IP")
    if instance_id is None:
        raise BootError("BAD_CONFIG", "인스턴스 ID 를 모른다(SIMAI_INSTANCE_ID 또는 RUNPOD_POD_ID)")
    if host is None:
        raise BootError("BAD_CONFIG", "공개 주소를 모른다(SIMAI_PUBLIC_HOST 또는 RUNPOD_PUBLIC_IP)")
    ports = []
    for role, req in (("CONTROL", control_port), ("VIDEO", video_port), ("SSH", 22)):
        pub = public_port(env, req)
        if pub is None:
            if role == "SSH":
                continue  # SSH 는 열려 있을 때만 싣는다
            if provider == "RUNPOD":
                log(f"주의: RUNPOD_TCP_PORT_{req} 가 없다(템플릿에 TCP {req} 를 노출하지 않음?). 직접 포트로 기록한다")
            pub = req  # 매핑 없는 사이트(AWS·VM): 듣는 포트 그대로 밖에서 붙는다
        ports.append({"role": role, "public_port": pub, "requested_port": req})
    price_raw = _first(env, "SIMAI_PRICE_PER_HOUR_USD")
    try:
        price = float(price_raw) if price_raw is not None else None
    except ValueError:
        price = None
    digest = _first(env, "SIMAI_IMAGE_DIGEST")
    payload = {
        "provider": provider,
        "instance_id": instance_id,
        "gpu_type": gpu["name"],
        "gpu_count": gpu["count"],
        "region": _first(env, "SIMAI_REGION", "RUNPOD_DC_ID"),
        "public_host": host,
        "ports": ports,
        "image_digest": digest if digest and re.fullmatch(r"sha256:[0-9a-f]{64}", digest) else None,
        "price_per_hour_usd": price,
    }
    if spki:
        payload["tls_public_key_sha256"] = spki
    return payload


def envelope(msg_type: str, source_id: str, payload: dict) -> dict:
    version = REPORT_SCHEMA_VERSION if msg_type == "gpu_session_report" else SCHEMA_VERSION
    return {"schema_version": version, "msg_type": msg_type, "msg_id": str(uuid.uuid4()), "sent_at": now_ms(),
            "source_id": source_id, "payload": payload}


# ---------------------------------------------------------------- 호스트 CPU (S15P31A101-740)


def cpu_model(cpuinfo: str = "/proc/cpuinfo") -> str | None:
    """/proc/cpuinfo 의 첫 `model name`(리눅스). 없으면(비리눅스·읽기 실패) None"""
    try:
        for line in Path(cpuinfo).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name"):
                name = " ".join(line.partition(":")[2].split())
                return name[:200] or None
    except OSError:
        pass
    return None


def cpu_quota(cpu_max: str = "/sys/fs/cgroup/cpu.max", cfs_quota: str = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
              cfs_period: str = "/sys/fs/cgroup/cpu/cpu.cfs_period_us") -> float | None:
    """cgroup CPU 몫(코어 수 단위, 예 4.0). 제한이 없거나(max, -1) 모르면 None. cgroup v2 cpu.max 를 먼저, 없으면 v1"""
    try:
        quota, _, period = Path(cpu_max).read_text(encoding="utf-8").strip().partition(" ")
        if quota != "max" and float(period) > 0 and float(quota) > 0:
            return round(float(quota) / float(period), 3)
        return None
    except (OSError, ValueError):
        pass
    try:
        q, p = float(Path(cfs_quota).read_text(encoding="utf-8")), float(Path(cfs_period).read_text(encoding="utf-8"))
        return round(q / p, 3) if q > 0 and p > 0 else None
    except (OSError, ValueError):
        return None


def single_thread_score(budget_s: float = BENCH_BUDGET_S, clock=time.perf_counter) -> float | None:
    """짧은 단일 스레드 기준 측정: 점수 = 초당 반복. 한 반복은 Tier 0 교차 시점과 비슷한 일(numpy 6x4 소행렬 SVD·DLT 삼각측량 풀이 4번)
    + 순수 Python 루프(딕셔너리·부동소수 2,000 걸음)다. numpy 가 없으면 Python 루프만(점수 척도가 달라 기준과 비교할 수 없으니 None).
    budget_s 안에서 반복하고 최소 3번은 돈다. 실패하면 None(부팅은 계속)."""
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        rng = np.random.default_rng(7)
        rows = rng.standard_normal((4, 6, 4))  # 4 점 × (카메라 3대 × 2행) × 동차 4

        def one() -> None:
            for a in rows:
                _, _, vt = np.linalg.svd(a)
                x = vt[-1]
                x = x[:3] / x[3] if abs(x[3]) > 1e-12 else x[:3]
                np.linalg.lstsq(a[:, :3], -a[:, 3], rcond=None)
                float(x @ x)
            acc, table = 0.0, {}
            for i in range(2000):
                acc += (i * 0.5) % 7.0
                table[i & 63] = acc
            if acc < 0:  # 최적화 방지용 사용(항상 거짓)
                raise ValueError

        one()  # 예열(import·캐시), 점수에 넣지 않음
        n, t0 = 0, clock()
        while True:
            one()
            n += 1
            elapsed = clock() - t0
            if elapsed >= budget_s and n >= 3:
                break
            if elapsed >= budget_s * 3:  # 너무 느린 기계: 3 번을 못 채워도 멈춘다
                break
        return round(n / elapsed, 2) if elapsed > 0 else None
    except Exception:  # 측정은 부팅을 막지 않는다
        return None


def measure_host_cpu() -> dict:
    """gpu_session_report.host_cpu. 못 잰 칸은 null(지어내지 않음)"""
    try:
        logical = os.cpu_count()
    except Exception:
        logical = None
    return {"model": cpu_model(), "logical_cpus": logical, "cpu_quota": cpu_quota(), "single_thread_score": single_thread_score(),
            "measured_at": now_ms()}


# ---------------------------------------------------------------- HTTP


class Http:
    """EC2·서명 주소 요청. allow_http 는 시험(가짜 EC2·S3)만 쓴다."""

    def __init__(self, ca_file: str | None = None, allow_http: bool = False, timeout: float = 30.0) -> None:
        self.ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
        self.allow_http = allow_http
        self.timeout = timeout

    def _check(self, url: str) -> None:
        if not (url.startswith("https://") or (self.allow_http and url.startswith("http://"))):
            raise BootError("BAD_CONFIG", f"https 주소만 받는다: {url.split('?', 1)[0]}")

    def request(self, method: str, url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None,
                data_file: Path | None = None) -> tuple[int, bytes]:
        self._check(url)
        h = dict(headers or {})
        f = None
        data: object = body
        if data_file is not None:
            f = data_file.open("rb")
            data = f
            h["Content-Length"] = str(data_file.stat().st_size)
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() or b""
        finally:
            if f is not None:
                f.close()

    def json(self, method: str, url: str, doc: dict | None, bearer: str | None = None) -> tuple[int, dict | None]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        status, raw = self.request(method, url, body=json.dumps(doc).encode("utf-8") if doc is not None else None,
                                   headers=headers)
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return status, parsed if isinstance(parsed, dict) else None

    def download(self, url: str, dest: Path, max_bytes: int | None = None) -> str:
        """GET 을 파일로 받아 sha256 16진수를 돌려준다(헤더 없이, 서명 주소)."""
        self._check(url)
        h = hashlib.sha256()
        n = 0
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r, dest.open("wb") as out:
            while chunk := r.read(1 << 20):
                n += len(chunk)
                if max_bytes is not None and n > max_bytes:
                    raise BootError("DOWNLOAD_TOO_LARGE", f"{max_bytes} 바이트를 넘는다")
                h.update(chunk)
                out.write(chunk)
        return h.hexdigest()


# ---------------------------------------------------------------- 보고기


class Reporter:
    """gpu_session_report 를 단계가 바뀔 때와 interval 마다 보낸다(긴 단계 중에도 배경 스레드로). 409 GPU_SESSION_ENDED 면 ended 를 세운다."""

    def __init__(self, http: Http, ec2: str, boot_id: str, secret: str, session_id: str, interval_s: float) -> None:
        self.http, self.ec2, self.boot_id, self.secret, self.session_id = http, ec2, boot_id, secret, session_id
        self.interval_s = max(1.0, interval_s)
        self.state: dict = {"stage": "WEIGHTS", "reason_code": None, "detail": None, "progress": None}
        # 모든 단계 설명 뒤에 붙일 한 줄(VRAM 으로 서비스를 줄였다는 사실, S15P31A101-705). 보고 계약은 그대로(stage_detail 칸)
        self.note: str | None = None
        # 꺼진 기능(S15P31A101-747·-756): None = 아직 모름(보고에 싣지 않는다), {} = 꺼진 것 없음. 키는 같은 기능의 항목을 덮어쓴다.
        # 항목은 {level: warning|info, text: 쉬운 말+영향+할 일, detail: 원문(선택)}
        self.disabled: dict[str, dict] | None = None
        self._base_detail: str | None = None  # note 를 붙이기 전 단계 설명(add_note 가 다시 붙인다)
        self.ended = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._warned401 = False
        self.host_cpu: dict | None = None  # 호스트 CPU 측정 결과(S15P31A101-740). 재면 보고에 한 번 더 실린다
        self.sent: list[dict] = []  # 시험·로그용(비밀 값 없음)
        self._thread = threading.Thread(target=self._loop, name="gpu-report", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def set(self, stage: str, reason: str | None = None, detail: str | None = None, progress: float | None = None) -> None:
        assert stage in STAGES
        base = detail
        if self.note and stage != "ENDED":
            detail = f"{detail} · {self.note}" if detail else self.note
        with self._lock:
            self._base_detail = base
            changed = (stage, reason, detail) != (self.state["stage"], self.state["reason_code"], self.state["detail"])
            self.state = {"stage": stage, "reason_code": reason, "detail": detail, "progress": progress}
        if changed:
            log(f"단계 {stage}" + (f" ({reason})" if reason else "") + (f": {detail}" if detail else ""))
            self._wake.set()

    def add_note(self, text: str) -> None:
        """단계와 무관하게 알게 된 사실(캐시 경로 쓰기 거부 등, S15P31A101-710)을 note 에 더하고 지금 단계 설명에 바로 붙인다. 같은 말은 한 번만."""
        with self._lock:
            if self.note and text in self.note:
                return
            self.note = f"{self.note} · {text}" if self.note else text
            s, base = dict(self.state), self._base_detail
        if s["stage"] != "ENDED":
            self.set(s["stage"], s["reason_code"], base, s["progress"])

    def set_disabled(self, key: str, text: str | None = None, level: str = "warning", detail: str | None = None) -> None:
        """꺼진 기능 목록에 더한다(text 가 None 이면 목록만 연다 = 지금까지 꺼진 것 없음). 같은 key 는 항목을 덮어쓴다."""
        with self._lock:
            if self.disabled is None:
                self.disabled = {}
            item = {"level": level, "text": text, **({"detail": detail} if detail else {})}
            if text is None or self.disabled.get(key) == item:
                return
            self.disabled[key] = item
        log(f"꺼진 기능: {text}")
        self._wake.set()

    def replace_disabled(self, items: dict[str, dict]) -> None:
        """READY 뒤 주기 재계산(S15P31A101-756): 목록 전체를 지금 값으로 바꾼다(해소된 항목은 빠진다). 바뀐 때만 로그·즉시 보고."""
        with self._lock:
            if self.disabled == items:
                return
            before = set((self.disabled or {}).keys())
            self.disabled = {k: dict(v) for k, v in items.items()}
        for key in sorted(set(items) - before):
            log(f"꺼진 기능: {items[key]['text']}")
        for key in sorted(before - set(items)):
            log(f"꺼진 기능 해소: {key}")
        self._wake.set()

    def payload(self) -> dict:
        with self._lock:
            s = dict(self.state)
            disabled = None if self.disabled is None else list(self.disabled.items())
        p = {"session_id": self.session_id, "stage": s["stage"],
             "reason_code": s["reason_code"] if s["stage"] in ("FAILED", "ENDED") else None, "reported_at": now_ms()}
        if s["stage"] == "FAILED" and not p["reason_code"]:
            p["reason_code"] = "UNKNOWN"
        if s["detail"]:
            p["detail"] = redact(str(s["detail"]))[:500]
        if s["progress"] is not None:
            p["progress"] = min(1.0, max(0.0, float(s["progress"])))
        if disabled is not None:
            # 1.2.0 칸(경고 문구만, 옛 EC2·화면 호환)과 1.3.0 칸(경고·정보와 자세히 원문). 같은 목록에서 만든다
            p["disabled_features"] = [redact(v["text"])[:200] for _, v in disabled if v["level"] == "warning"][:MAX_DISABLED]
            p["disabled_notices"] = [{"key": k, "level": v["level"], "text": redact(v["text"])[:200],
                                      **({"detail": redact(v["detail"])[:300]} if v.get("detail") else {})}
                                     for k, v in disabled[:MAX_DISABLED]]
        if self.host_cpu is not None:
            p["host_cpu"] = self.host_cpu
        return p

    def set_host_cpu(self, doc: dict) -> None:
        """배경 측정이 끝나면 다음 보고(단계 변화·주기)부터 싣고 바로 한 번 보낸다"""
        self.host_cpu = doc
        self._wake.set()

    def send_once(self) -> int | None:
        p = self.payload()
        url = f"{self.ec2}{REGISTER_PATH}/{self.session_id}/reports"
        try:
            status, body = self.http.json("POST", url, envelope("gpu_session_report", self.boot_id, p), bearer=self.secret)
        except (OSError, BootError) as e:
            log(f"보고 실패(다음 주기에 다시): {type(e).__name__}")
            return None
        self.sent.append({"stage": p["stage"], "reason_code": p["reason_code"], "status": status})
        if status == 409 and (body or {}).get("code") == "GPU_SESSION_ENDED":
            if not self.ended.is_set():
                log("EC2 가 이 세션을 끝냈다(GPU_SESSION_ENDED: 새 등록·무응답·폐기·관리자 정지) → 서비스를 멈춘다")
            self.ended.set()
        elif status == 401 and not self._warned401:
            self._warned401 = True
            log("보고 401: 부팅 비밀값이 회전·폐기됐다. 관제가 이미 돌면 그대로 두고, 아니면 무응답으로 끝난다")
        elif status not in (204, 200, 401, 409):
            log(f"보고 응답 HTTP {status}")
        return status

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.ended.is_set():
                return
            self._wake.clear()
            self.send_once()
            self._wake.wait(self.interval_s)

    def stop(self, final_stage: str | None = None, reason: str | None = None, detail: str | None = None) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        if final_stage and not self.ended.is_set():
            self.set(final_stage, reason, detail)
            self.send_once()


# ---------------------------------------------------------------- GPU 캐시 (S3, 워커 토큰 서명)


def gpu_slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (s or "gpu")[:48].rstrip("-")


def dist_version(*names: str) -> str | None:
    for n in names:
        try:
            return importlib.metadata.version(n)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def _digest(doc: dict) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def trt_cache_key(gpu: dict, model_sha256: str, env: dict[str, str], versions: dict[str, str | None]) -> str | None:
    """TensorRT 엔진: GPU 종류(같은 sm 이라도 H100·H200 따로)·sm·TensorRT·ultralytics·torch·ModelOpt·가중치 해시·엔진 설정.
    하나라도 모르면 None(캐시를 쓰지 않는다). 엔진 자체는 engine_cache 메타데이터 키로 다시 확인된다."""
    if not gpu.get("arch") or not versions.get("tensorrt") or not versions.get("ultralytics"):
        return None
    doc = {"format": 1, "gpu": gpu["name"], "arch": gpu["arch"], "model_sha256": model_sha256,
           "mode": env.get("SIMAI_TIER0_MODE") or "e2e", "imgsz": env.get("SIMAI_TIER0_IMGSZ") or "1280",
           "max_batch": env.get("SIMAI_TIER0_MAX_BATCH") or "4", "camera": env.get("SIMAI_TIER0_CAMERA") or "2304x1296",
           "tensorrt": versions["tensorrt"], "ultralytics": versions["ultralytics"], "torch": versions.get("torch") or "",
           "modelopt": versions.get("modelopt") or ""}
    return f"{gpu_slug(gpu['name'])}/{gpu['arch']}/{_digest(doc)}"


def vllm_cache_key(gpu: dict, model_shas: list[str], env: dict[str, str], versions: dict[str, str | None]) -> str | None:
    """vLLM 컴파일 캐시(torch.compile·Triton·Inductor): GPU 종류·sm·vLLM·torch·Triton·모델 해시·서빙 인자. vLLM 은 안에서 자기 해시로 한 번 더 고른다."""
    if not gpu.get("arch") or not versions.get("vllm") or not versions.get("torch") or not model_shas:
        return None
    doc = {"format": 1, "gpu": gpu["name"], "arch": gpu["arch"], "vllm": versions["vllm"], "torch": versions["torch"],
           "triton": versions.get("triton") or "", "model": hashlib.sha256("".join(sorted(model_shas)).encode()).hexdigest(),
           "quantization": env.get("SIMAI_VLLM_QUANTIZATION", "fp8"),
           "max_model_len": env.get("SIMAI_WORKER_VLLM_MAX_MODEL_LEN") or "32768",
           "gpu_memory_utilization": env.get("SIMAI_WORKER_VLLM_GPU_MEMORY_UTILIZATION") or "0.30",
           "max_num_seqs": env.get("SIMAI_WORKER_VLLM_MAX_NUM_SEQS") or "4", "extra": env.get("SIMAI_VLLM_EXTRA_ARGS") or ""}
    return f"{gpu_slug(gpu['name'])}/{gpu['arch']}/{_digest(doc)}"


def reid_cache_key(env: dict[str, str]) -> str:
    """Re-ID 묶음: COPE 가중치·CLIP 해시(Tier 0 embedder 가 받을 때 다시 확인한다)."""
    w = (env.get("SIMAI_COPE_WEIGHTS_SHA256") or COPE_WEIGHTS_SHA256).lower()
    c = (env.get("SIMAI_COPE_CLIP_SHA256") or CLIP_SHA256).lower()
    return f"cope-{w[:16]}-clip-{c[:16]}"


def safe_extract(tar_path: Path, dest: Path) -> list[str]:
    """tar 를 dest 아래로만 푼다(절대 경로·..·링크·장치 거부, Python data 필터)."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:") as tf:
        names = tf.getnames()
        tf.extractall(dest, filter="data")
    return names


def make_tar(base: Path, members: list[str], out: Path) -> tuple[int, str]:
    with tarfile.open(out, "w:", format=tarfile.PAX_FORMAT) as tf:
        for m in members:
            tf.add(base / m, arcname=m, recursive=True)
    h = hashlib.sha256()
    with out.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return out.stat().st_size, h.hexdigest()


class GpuCache:
    """객체 키는 서버가 정한다(job-objects/gpu_cache/<kind>/<key>.tar, S15P31A101-710). 이쪽은 kind·cache_key 만 보내고 받은 서명 주소만 쓴다.
    받기: 서명 GET → sha256 확인(저장소가 기록한 해시를 모르면 쓰지 않음) → 안전하게 풀기.
    올리기: tar → sha256 → 서명 PUT(없을 때만, If-None-Match) → 412 면 이미 있음, 403 이면 캐시 경로 쓰기 거부(서버 키 권한, 보고에 남김).
    실패해도 기동은 계속한다(캐시는 시간만 줄인다)."""

    DENIED_NOTE = "캐시 경로 쓰기 거부(S3 403)"

    def __init__(self, http: Http, ec2: str, worker_token: str, tmp: Path,
                 notice: Callable[[str], None] | None = None) -> None:
        self.http, self.ec2, self.token, self.tmp = http, ec2, worker_token, tmp
        self.notice = notice  # 보고 설명에 남길 한 줄(Reporter.add_note)
        self.results: dict[str, str] = {}

    def _sign(self, kind: str, key: str, method: str, size: int | None = None, sha: str | None = None) -> dict | None:
        doc: dict = {"kind": kind, "cache_key": key, "method": method}
        if size is not None:
            doc |= {"size_bytes": size, "sha256": sha}
        try:
            status, body = self.http.json("POST", self.ec2 + CACHE_PATH, doc, bearer=self.token)
        except (OSError, BootError) as e:
            log(f"캐시 서명 실패 {kind}: {type(e).__name__}")
            return None
        if status != 200 or body is None:
            log(f"캐시 서명 응답 {kind}: HTTP {status}")
            return None
        return body

    def fetch(self, kind: str, key: str, dest: Path) -> str:
        sig = self._sign(kind, key, "GET")
        if sig is None:
            return self._done(kind, "error")
        if not sig.get("hit"):
            return self._done(kind, "miss")
        want = (sig.get("sha256") or "").lower()
        if not _SHA_RE.match(want):
            log(f"캐시 {kind}: 저장소가 기록한 해시를 몰라 쓰지 않는다")
            return self._done(kind, "error")
        self.tmp.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{kind}-", suffix=".tar", dir=self.tmp)
        os.close(fd)
        part = Path(name)
        try:
            got = self.http.download(sig["url"], part, max_bytes=MAX_CACHE_BYTES)
            if got != want:
                log(f"캐시 {kind}: 받은 내용 sha256 이 다르다 → 쓰지 않는다")
                return self._done(kind, "error")
            safe_extract(part, dest)
        except (OSError, BootError, tarfile.TarError, urllib.error.URLError) as e:
            log(f"캐시 {kind} 받기 실패: {type(e).__name__}: {e}")
            return self._done(kind, "error")
        finally:
            part.unlink(missing_ok=True)
        log(f"캐시 {kind} 적중: {sig.get('object_key')} ({sig.get('size_bytes')} bytes)")
        return self._done(kind, "hit")

    def store(self, kind: str, key: str, base: Path, members: list[str]) -> str:
        pre = self._sign(kind, key, "GET")  # 이미 있으면 묶음을 만들지 않는다(Re-ID 1 GB)
        if pre is not None and pre.get("hit"):
            return self._done(f"{kind}:store", "exists")
        self.tmp.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f"{kind}-up-", suffix=".tar", dir=self.tmp)
        os.close(fd)
        out = Path(name)
        try:
            size, sha = make_tar(base, members, out)
            if size > MAX_CACHE_BYTES:
                log(f"캐시 {kind}: {size} bytes 가 상한을 넘어 올리지 않는다")
                return self._done(f"{kind}:store", "skipped")
            sig = self._sign(kind, key, "PUT", size, sha)
            if sig is None:
                return self._done(f"{kind}:store", "error")
            if sig.get("hit"):
                return self._done(f"{kind}:store", "exists")
            status, _ = self.http.request("PUT", sig["url"], headers=dict(sig.get("headers") or {}), data_file=out)
        except (OSError, BootError, tarfile.TarError) as e:
            log(f"캐시 {kind} 올리기 실패: {type(e).__name__}: {e}")
            return self._done(f"{kind}:store", "error")
        finally:
            out.unlink(missing_ok=True)
        if status == 412:
            return self._done(f"{kind}:store", "exists")
        if status == 403:
            # 서버가 HEAD 403 을 없음으로 보고 서명은 줬지만 저장소가 쓰기를 거부했다(서버 키에 그 경로 PutObject 권한 없음). 캐시 없이 진행한다
            log(f"캐시 {kind} 올리기 실패: {self.DENIED_NOTE}: {sig.get('object_key')} → 캐시 없이 진행(서버 키 권한 확인 필요)")
            if self.notice is not None:
                self.notice(self.DENIED_NOTE)
            return self._done(f"{kind}:store", "denied")
        if status not in (200, 201, 204):
            log(f"캐시 {kind} 올리기 HTTP {status}")
            return self._done(f"{kind}:store", "error")
        log(f"캐시 {kind} 올림: {sig.get('object_key')} ({size} bytes)")
        return self._done(f"{kind}:store", "stored")

    def _done(self, what: str, result: str) -> str:
        self.results[what] = result
        return result


def engine_members(engines: Path, stem: str, arch: str, sha12: str, env: dict[str, str]) -> list[str]:
    """이번 설정의 엔진(+메타데이터 .json)을 engines 기준 상대 경로로. 여러 TensorRT 판이 있으면 가장 새것."""
    mode, imgsz = env.get("SIMAI_TIER0_MODE") or "e2e", env.get("SIMAI_TIER0_IMGSZ") or "1280"
    batch, cam = env.get("SIMAI_TIER0_MAX_BATCH") or "4", env.get("SIMAI_TIER0_CAMERA") or "2304x1296"
    pattern = f"{stem}/{arch}/trt*/{stem}-{sha12}_{mode}_i{imgsz}_b{batch}_c{cam}_fp16_ul*.engine"
    found = [p for p in engines.glob(pattern) if p.with_suffix(".json").is_file()]
    if not found:
        return []
    best = max(found, key=lambda p: p.stat().st_mtime)
    return [best.relative_to(engines).as_posix(), best.with_suffix(".json").relative_to(engines).as_posix()]


def reid_ready(reid: Path) -> bool:
    return (reid / "cope").is_dir() and all((reid / f).is_file() for f in REID_FILES)


def reid_members(reid: Path) -> list[str]:
    return ["cope", *REID_FILES, *[o for o in REID_OPTIONAL if (reid / o).exists()]]


def dir_members(base: Path) -> list[str]:
    return sorted(p.name for p in base.iterdir()) if base.is_dir() else []


# ---------------------------------------------------------------- 시작 묶음·lobby 인자·단계


def write_bundle(http: Http, bundle: Path, start: dict, cert_pem: str, key_pem: str) -> None:
    """calibration.json(sha256 확인)·laptop_token.sha256·tls/ 를 lobby --bundle-dir 형식으로."""
    cfg = start.get("config")
    if not cfg:
        raise BootError("NO_START_CONFIG", "구성 묶음이 없다(세팅부터): Tier 0 를 띄울 보정이 없다")
    cal = cfg["calibration"]
    m = _SHA_RE.match(str(cal.get("sha256") or "").lower())
    if m is None:
        raise BootError("CALIBRATION_FETCH_FAILED", "보정 문서 sha256 형식이 아니다")
    bundle.mkdir(parents=True, exist_ok=True)
    part = bundle / "calibration.json.part"
    try:
        got = http.download(cal["uri"], part, max_bytes=MAX_CALIBRATION_BYTES)
    except (OSError, urllib.error.URLError, BootError) as e:
        part.unlink(missing_ok=True)
        raise BootError("CALIBRATION_FETCH_FAILED", f"보정 문서를 받지 못함({type(e).__name__})") from e
    if got != m.group(1):
        part.unlink(missing_ok=True)
        raise BootError("CALIBRATION_HASH_MISMATCH", f"보정 {cal.get('version')} sha256 이 다르다")
    try:
        json.loads(part.read_text(encoding="utf-8"))
    except ValueError as e:
        part.unlink(missing_ok=True)
        raise BootError("CALIBRATION_FETCH_FAILED", "보정 문서가 JSON 이 아니다") from e
    os.replace(part, bundle / "calibration.json")
    hashes = [h for h in start.get("laptop_token_sha256") or [] if isinstance(h, str)]
    if not hashes:
        raise BootError("NO_LAPTOP_TOKEN", "폐기 안 된 현장 노트북 기기 토큰이 없다(admin device register): 영상 포트를 열 수 없다")
    (bundle / "laptop_token.sha256").write_text(",".join(hashes) + "\n", encoding="utf-8", newline="\n")
    tls = bundle / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    (tls / "tier0.crt").write_text(cert_pem, encoding="utf-8", newline="\n")
    write_private(tls / "tier0.key", key_pem)


def lobby_argv(bootstrap: list[str], *, work: Path, bundle: Path, start: dict, worker_id: str, gateway_id: str,
               public_host: str, control_port: int, video_port: int, services: str | None,
               worker_token_file: Path, tier0_token_file: Path) -> list[str]:
    """등록 응답 start 를 simai-bootstrap lobby 인자로. 토큰은 파일 경로로만 넘긴다(명령줄에 원문 없음)."""
    cams = [c for c in start.get("camera_ids") or [] if isinstance(c, str)]
    if not cams:
        raise BootError("NO_CAMERAS", "현장에 설치된 카메라가 없다(start.camera_ids 비어 있음)")
    heights = start.get("measured_heights_m") or {}
    argv = [*bootstrap, "lobby", "--work", str(work), "--bundle-dir", str(bundle), "--public-host", public_host,
            "--ec2-base-url", start["ec2_base_url"], "--worker-token-file", str(worker_token_file),
            "--tier0-token-file", str(tier0_token_file), "--worker-id", worker_id, "--gateway-id", gateway_id,
            "--camera-ids", ",".join(cams),
            # 높이를 모르는 카메라는 빼고 넘긴다(0 으로 채우지 않는다)
            "--measured-heights", ",".join(f"{c}={heights[c]}" for c in cams if c in heights),
            # 확정 Scene 이 없으면 Tier 0 기본 초기값(scene-0). I1 scene_reference 를 받으면 EC2 현재 Scene 을 따른다
            "--scene-version", start.get("scene_version") or "scene-0",
            "--config-version", start["config"]["config_version"],
            "--control-port", str(control_port), "--video-ports", ",".join(f"{c}:{video_port}" for c in cams)]
    if services:
        argv += ["--services", services]
    return argv


def read_marker(marker: Path) -> dict | None:
    try:
        doc = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def lobby_stage(marker_doc: dict | None, started_ms: int, want_vllm: bool, vllm_healthy: bool,
                vllm_timed_out: bool) -> tuple[str, str | None]:
    """lobby 를 띄운 뒤의 단계. ENGINE: 이번 Tier 0 의 표시 파일이 아직 없음(엔진 준비·적재), TIER0: 기동 중(running·reserve OK 전),
    VLLM: Tier 0 는 준비됐고 vLLM /health 대기, READY: 관제 준비 끝(vLLM 이 시한 안에 안 뜨면 READY + 설명)."""
    gp = (marker_doc or {}).get("gpu_priority") if isinstance(marker_doc, dict) else None
    written = gp.get("written_at") if isinstance(gp, dict) else None
    if not isinstance(written, int) or isinstance(written, bool) or written < started_ms:
        return "ENGINE", "TensorRT 엔진 준비·적재"
    if gp.get("phase") != "running" or (gp.get("reserve") or {}).get("state") != "OK":
        return "TIER0", f"Tier 0 기동 중(phase={gp.get('phase')})"
    if want_vllm and not vllm_healthy:
        if vllm_timed_out:
            return "READY", "vLLM 미준비(시한 초과): VLM 근거·보고서 없이 관제"
        return "VLLM", "VLM 가중치·vLLM 기동 대기"
    return "READY", None


def notice(text: str, *, level: str = "warning", detail: str | None = None) -> dict:
    """꺼진 기능 항목(S15P31A101-756): text 는 관리자가 읽는 쉬운 말 + 영향 + 할 일(200자 이내), detail 은 「자세히」에 펼칠 원문."""
    assert level in ("warning", "info") and 1 <= len(text) <= 200, text
    return {"level": level, "text": text, **({"detail": detail} if detail else {})}


# 항목 종류별 문구표(S15P31A101-756). 형식: 무엇이 꺼졌나 — 사람에게 어떤 영향 — 할 일. 원문(로그·예외 문구)은 detail 로만 보낸다.
REID_EFFECT = "사람 다시 알아보기(Re-ID) 꺼짐 — 가렸다 나타난 사람이 새 번호를 받을 수 있어요."
REID_NO_BUNDLE = notice(REID_EFFECT + " 관리자: seed-reid 로 Re-ID 묶음을 등록해야 해요")
REID_REGISTERED = notice(REID_EFFECT + " 다음 부팅부터 자동으로 켜집니다(Re-ID 묶음 등록됨). 지금 켜려면 Tier 0 재시작(restart-tier0)")
REID_OTHER = notice(REID_EFFECT + " 묶음은 있는데 켜지지 않았어요. 관리자: 자세히를 보고 Tier 0 로그를 확인하세요")
CACHE_TEXT = {
    ("trt-engine", "miss"): ("info", "TensorRT 엔진 캐시가 없어 이번 부팅은 엔진을 새로 만들었어요(준비가 오래 걸림). 할 일 없음 — 다음 부팅부터 캐시를 써요"),
    ("trt-engine", "error"): ("warning", "TensorRT 엔진 캐시를 받지 못해 엔진을 새로 만들었어요(준비가 오래 걸림). 관리자: GPU 캐시(S3) 연결·권한 확인"),
    ("vllm-compile", "miss"): ("info", "VLM 컴파일 캐시가 없어 AI 설명(VLM) 첫 요청이 느릴 수 있어요. 할 일 없음 — 부팅 뒤 캐시에 올라가요"),
    ("vllm-compile", "error"): ("warning", "VLM 컴파일 캐시를 받지 못해 AI 설명(VLM) 첫 요청이 느릴 수 있어요. 관리자: GPU 캐시(S3) 연결·권한 확인"),
}
SERVICE_TEXT = {
    "svc_ppe": "보호구(PPE) 감시 꺼짐 — 안전모·조끼 미착용 경고가 나가지 않아요. 관리자: 기동 서비스에 ppe 를 넣고 다시 켜세요",
    "svc_avatar": "아바타 꺼짐 — 3D 화면의 사람이 캡슐로만 보여요. 관리자: 기동 서비스에 avatar 를 넣고 다시 켜세요",
    "svc_worker": "분석 워커 꺼짐 — 현장 세팅·맵 만들기·AI 근거·아바타가 동작하지 않아요. 관리자: 기동 서비스에 worker 를 넣고 다시 켜세요",
    "svc_vllm": "AI 설명(VLM) 꺼짐 — 이벤트에 AI 근거·보고서가 붙지 않아요(경보는 그대로). GPU 메모리가 모자라거나 서비스 목록에 vllm 이 없어요",
}
INFO_CONFIG_TEXT = {
    "live_calibration": ("cfg_live_calibration", "사람 관절 자동 보정은 기본 꺼짐이에요(의도된 설정) — 설정 파일의 live_calibration 값은 쓰이지 않아요. 켜려면 SIMAI_TIER0_HUMAN_CALIBRATION=1"),
    "drift_watch": ("cfg_drift_watch", "보정 감시는 사람 관절 보정이 꺼져 있어 돌지 않아요(기본 설정, 의도된 설정). 켜려면 SIMAI_TIER0_HUMAN_CALIBRATION=1"),
}
VLM_WARMUP_FAILED_TEXT = "AI 설명(VLM) 예열 실패 — 첫 AI 근거가 늦거나 시간 초과일 수 있어요(경보는 그대로). 계속되면 관리자: 자세히를 보고 vLLM 로그 확인"
VLM_TIMEOUT_TEXT = "AI 설명(VLM) 아직 준비 안 됨 — AI 근거·보고서 없이 관제 중이에요(경보는 그대로). 잠시 뒤에도 같으면 관리자: vLLM 로그 확인"
TIER0_OVERRIDE_TEXT = "설정 파일 값이 환경 변수에 덮여 적용되지 않았어요. 관리자: 의도한 설정인지 확인하세요(자세히)"
TIER0_UNKNOWN_TEXT = "Tier 0 가 알린 꺼진 기능이 있어요. 관리자: 자세히를 확인하세요"


def cache_findings(results: dict[str, str], reid_is_ready: bool) -> dict[str, dict]:
    """GPU 캐시 받기 결과(GpuCache.results)와 Re-ID 묶음 유무 → 꺼진 기능·캐시 miss 항목(S15P31A101-747, 근거 있는 것만).
    Re-ID 는 묶음이 없으면 Tier 0 가 꺼진 채 돈다. 엔진·vLLM 캐시 miss 는 기능이 꺼진 것은 아니라 정보(miss)·경고(오류)로 싣는다."""
    out: dict[str, dict] = {}
    if not reid_is_ready:
        r = results.get("reid")
        out["reid"] = dict(REID_NO_BUNDLE, **({"detail": f"캐시 받기 결과: {r}"} if r not in ("miss", None) else {"detail": "캐시 받기 결과: miss"}))
    for kind, key in (("trt-engine", "cache_trt"), ("vllm-compile", "cache_vllm")):
        r = results.get(kind)
        if (kind, r) in CACHE_TEXT:
            level, text = CACHE_TEXT[(kind, r)]
            out[key] = notice(text, level=level, detail=f"캐시 받기 결과: {r}")
    return out


def service_findings(want: list[str], vllm_vram_ok: bool, disabled: set[str] | None = None) -> dict[str, dict]:
    """기동하지 않는 서비스(--services 에 없거나 VRAM 부족) → 꺼진 기능 항목."""
    out: dict[str, dict] = {}
    for svc, key in (("ppe", "svc_ppe"), ("avatar", "svc_avatar"), ("worker", "svc_worker")):
        if svc not in want:
            out[key] = notice(SERVICE_TEXT[key], detail=f"기동 서비스: {','.join(want) or '(없음)'}")
    if "vllm" not in want or not vllm_vram_ok:
        configured_off = "vllm" in (disabled or set())
        out["svc_vllm"] = notice("AI 설명(VLM)은 설정으로 꺼져 있어요(경보는 그대로)." if configured_off else SERVICE_TEXT["svc_vllm"], level="info" if configured_off else "warning", detail=f"기동 서비스: {','.join(want) or '(없음)'}, VRAM 충분: {vllm_vram_ok}")
    return out


def tier0_findings(marker_doc: dict | None, reid_on_disk: bool = False) -> dict[str, dict]:
    """Tier 0 표시 파일 status → 꺼진 기능 항목(S15P31A101-747·-756). status 에 칸이 없으면(옛 Tier 0·아직 기동 전) 아무것도 지어내지 않는다.
    disabled_features(설정 무효·Re-ID 꺼짐)는 경고, info_notes(의도된 기본 설정으로 무효가 된 설정)는 정보, process_split.disabled 는 경고.
    Re-ID 는 status.reid.state 가 active 면 항목이 없다(꺼짐이 해소됨). 묶음이 이 서버에 있는지(reid_on_disk)로 할 일 문구를 고른다."""
    status = (marker_doc or {}).get("status") if isinstance(marker_doc, dict) else None
    if not isinstance(status, dict):
        return {}
    out: dict[str, dict] = {}
    n = 0
    for t in status.get("disabled_features") or []:
        if not isinstance(t, str) or not t:
            continue
        if t.startswith("Re-ID"):
            reid = status.get("reid")
            if isinstance(reid, dict) and reid.get("state") == "active":
                continue
            if not reid_on_disk:
                base = REID_NO_BUNDLE
            elif "FileNotFoundError" in t or "찾을 수 없" in t:
                base = REID_REGISTERED  # 부팅 뒤 묶음이 올라왔다(seed-reid): 다음 부팅부터 자동, 지금은 Tier 0 재시작
            else:
                base = REID_OTHER
            out["reid"] = dict(base, detail=t[:300])
        elif "덮어씀" in t:
            out[f"tier0_{n}"] = notice(TIER0_OVERRIDE_TEXT, detail=t[:300])
            n += 1
        else:
            out[f"tier0_{n}"] = notice(TIER0_UNKNOWN_TEXT, detail=t[:300])
            n += 1
    for t in status.get("info_notes") or []:
        if not isinstance(t, str) or not t:
            continue
        which = "live_calibration" if t.startswith("live_calibration") else "drift_watch" if t.startswith("drift_watch") else None
        if which:
            key, text = INFO_CONFIG_TEXT[which]
            out[key] = notice(text, level="info", detail=t[:300])
    split = status.get("process_split")
    names = [n_ for n_ in (split or {}).get("disabled", []) if isinstance(n_, str)] if isinstance(split, dict) else []
    if names:
        out["process_split"] = notice("프로세스 분리 때문에 일부 곁가지 기능이 꺼졌어요. 관리자: 자세히를 보고 필요한 기능이면 분리를 끄세요",
                                      detail=f"꺼진 기능: {', '.join(names)}")
    return out


def compose_disabled(*, services: dict[str, dict], cache_results: dict[str, str], reid_on_disk: bool, marker_doc: dict | None,
                     vlm_warmup_failure: str | None, vlm_timed_out: bool, include_vllm_cache: bool) -> dict[str, dict]:
    """지금 꺼진 기능 목록 전체를 근거에서 다시 계산한다(S15P31A101-756: READY 뒤에도 주기로 불러 해소된 항목이 빠지게 한다).
    순서: 서비스 미기동 → 캐시(Re-ID 묶음·엔진·vLLM) → Tier 0 표시 파일(Re-ID 가 켜졌으면 Re-ID 항목을 지운다) → VLM 웜업·시한."""
    out = dict(services)
    cache = cache_findings({k: v for k, v in cache_results.items() if include_vllm_cache or k != "vllm-compile"}, reid_on_disk)
    status = (marker_doc or {}).get("status") if isinstance(marker_doc, dict) else None
    reid = status.get("reid") if isinstance(status, dict) else None
    if isinstance(reid, dict) and reid.get("state") == "active":
        cache.pop("reid", None)  # Tier 0 가 지금 켜져 있다고 말한다: 부팅 때의 묶음 없음 보고는 낡았다
    out.update(cache)
    out.update(tier0_findings(marker_doc, reid_on_disk))
    if vlm_warmup_failure:
        out["vlm_warmup"] = notice(VLM_WARMUP_FAILED_TEXT, detail=vlm_warmup_failure[:300])
    if vlm_timed_out:
        out["vlm_timeout"] = notice(VLM_TIMEOUT_TEXT)
    return out


def dummy_png(width: int = 336, height: int = 192) -> bytes:
    """VLM 웜업용 작은 합성 이미지(색 띠 PNG, 표준 라이브러리만). 사람·얼굴 없는 합성 그림이다."""
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = b"".join(bytes([0]) + bytes(v for x in range(width) for v in ((x * 6 // width) * 40, (y * 6 // height) * 40, 128))
                      for y in range(height))
    return (bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def warmup_vlm(env: dict[str, str], opener=urllib.request.urlopen, clock=time.monotonic) -> tuple[bool, float, str]:
    """vLLM 에 더미 멀티모달 요청 1회(실제 근거 요청과 같은 경로·형식: POST /v1/chat/completions, user 메시지 text 파트 → image_url
    data URL 파트, repetition_penalty 포함). 첫 요청이 Triton JIT 로 TIMED_OUT 이던 일(18차 감사)을 READY 전에 흡수한다.
    돌려주는 값: (성공, 걸린 초, 설명). 실패해도 예외를 내지 않는다(부팅은 계속)."""
    port = int(env.get("SIMAI_VLLM_PORT") or 8000)
    model = env.get("SIMAI_VLM_MODEL") or env.get("SIMAI_VLLM_SERVED_NAME") or "cosmos3-nano"
    url = f"data:image/png;base64,{base64.b64encode(dummy_png()).decode('ascii')}"
    body = {"model": model, "max_tokens": 16, "repetition_penalty": 1.05,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "이 그림에 무엇이 보이는지 한 문장으로 답하라."},
                                                      {"type": "image_url", "image_url": {"url": url}}]}]}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json"})
    t0 = clock()
    try:
        with opener(req, timeout=float(env.get("SIMAI_BOOT_VLM_WARMUP_TIMEOUT_S") or WARMUP_TIMEOUT_S)) as r:
            doc = json.loads(r.read())
        ok = bool(doc["choices"][0]["message"]["content"] is not None)
        return ok, clock() - t0, "응답 받음" if ok else "응답 내용 없음"
    except Exception as e:  # noqa: BLE001 - 웜업은 부팅을 막지 않는다
        return False, clock() - t0, f"{type(e).__name__}"


def _boot_services_base(env: dict[str, str], gpu: dict, min_worker_mb: float, min_tier0_mb: float) -> tuple[str | None, str | None]:
    """lobby 서비스를 VRAM(nvidia-smi memory.total, MiB)으로 정한다(S15P31A101-705, 명세 3.12 최소 사양).
    SIMAI_BOOT_SERVICES 가 있으면 그 값을 그대로 쓴다(명시값 우선). 돌려주는 것은 (services, 보고에 붙일 한 줄 또는 None).
    - PPE_MIN_VRAM_MB(70,000 MiB) 이상: BOOT_SERVICES(tier0,vllm,worker,avatar,ppe, -718)
    - 48GB급(min_worker_mb 45,000 ~ 70,000 MiB): lobby 기본(tier0,vllm,worker,avatar) + 'ppe 꺼짐: VRAM 부족'. VLM 몫은 19.1 미결이라 그대로 둔다
    - 24GB급(min_tier0_mb 22,500 ~ 45,000 MiB): tier0. 분석 워커가 없으니 세팅(site_setup)·맵 굽기·VLM·아바타가 안 된다
    - 그보다 작음: tier0 + 경고. preflight 가 Tier 0 최소 사양 미만으로 멈춰 GPU_TOO_SMALL 이 될 수 있다(개발·시험용)
    - VRAM 을 모름: lobby 기본(preflight 가 사양을 모르면 멈춘다)"""
    explicit = (env.get("SIMAI_BOOT_SERVICES") or "").strip()
    if explicit:
        return explicit, None
    mb = gpu.get("memory_total_mb")
    if not isinstance(mb, (int, float)) or isinstance(mb, bool):
        return None, None
    gib = mb / 1024
    if mb >= PPE_MIN_VRAM_MB:
        return BOOT_SERVICES, None
    if mb >= min_worker_mb:
        return None, f"VRAM {gib:.1f} GiB(48GB급): ppe 꺼짐: VRAM 부족(보호구 감시는 80GB급 이상에서 자동으로 켠다)"
    if mb >= min_tier0_mb:
        return "tier0", f"VRAM {gib:.1f} GiB(24GB급): Tier 0 관제만, 세팅·맵 굽기·VLM·아바타 꺼짐"
    return "tier0", f"VRAM {gib:.1f} GiB: Tier 0 최소 사양(24GB급) 미만, 개발·시험용(준비 실패 가능)"


def boot_services(env: dict[str, str], gpu: dict, min_worker_mb: float, min_tier0_mb: float) -> tuple[str | None, str | None]:
    """명시 목록 또는 VRAM 기본값에서 설정으로 제외한 서비스만 뺀다."""
    services, note = _boot_services_base(env, gpu, min_worker_mb, min_tier0_mb)
    disabled = {s.strip() for s in env.get("SIMAI_BOOT_DISABLE", "").split(",") if s.strip()}
    if not disabled:
        return services, note
    selected = services if services is not None else "tier0,vllm,worker,avatar"
    remaining = ",".join(s.strip() for s in selected.split(",") if s.strip() and s.strip() not in disabled)
    if not remaining:
        raise ValueError("SIMAI_BOOT_DISABLE 적용 후 기동 서비스가 없다")
    return remaining, note


def lobby_failure(code: int, stage: str) -> tuple[str, str]:
    if code == 5 and stage == "ENGINE":
        return "ENGINE_BUILD_FAILED", "TensorRT 엔진 빌드 실패(종료 코드 5)"
    if code == 2:
        return "TIER0_CONFIG_FAILED", "저장 위치·가중치·설정 오류(종료 코드 2)"
    if code == 3:
        return ("GPU_TOO_SMALL" if stage == "ENGINE" else "TIER0_EXITED"), "GPU·재시작 복구 문제(종료 코드 3)"
    if code == 64:
        return "LOBBY_USAGE", "lobby 인자 오류(종료 코드 64)"
    if code == 0:
        return "LOBBY_STOPPED", "lobby 가 정상 종료했다(Tier 0 정상 종료·손으로 멈춤)"
    return "TIER0_EXITED", f"lobby 가 끝났다(종료 코드 {code})"


# ---------------------------------------------------------------- 부팅


class Boot:
    def __init__(self, env: dict[str, str] | None = None, *, http: Http | None = None, smi: list[str] | None = None,
                 bootstrap_cmd: list[str] | None = None, state_dir: Path | None = None, poll_s: float = 2.0) -> None:
        self.env = dict(os.environ if env is None else env)
        self.secrets = take_secrets(self.env)
        if env is None:  # 실제 실행: 이 프로세스 환경에서도 지운다
            for k in (*SECRET_ENVS, *HF_ENVS):
                os.environ.pop(k, None)
        self.http = http or Http(ca_file=self.env.get("SIMAI_EC2_CA_FILE"))
        self.smi = smi or shlex.split(self.env.get("SIMAI_NVIDIA_SMI") or "nvidia-smi")
        self.bootstrap_cmd = bootstrap_cmd or shlex.split(self.env.get("SIMAI_BOOTSTRAP_CMD") or "simai-bootstrap")
        self.state_dir = state_dir or Path(self.env.get("SIMAI_GPU_BOOT_STATE_DIR") or "/var/tmp/simai-gpu-boot")
        self.poll_s = poll_s
        self.stop = threading.Event()
        self.reporter: Reporter | None = None
        self.cache: GpuCache | None = None
        self.child: subprocess.Popen | None = None
        self.bs = load_bootstrap(self.env)
        root, _ = self.bs.choose_root(self.env)
        self.root = Path(root)
        self.private = Path(self.env.get("SIMAI_GPU_BOOT_PRIVATE_DIR") or (self.root.parent / ".simai-private" / "gpu-session"))
        self.manifest = Path(self.env.get("SIMAI_WEIGHTS_MANIFEST") or Path(self.bs.DEFAULT_MANIFEST))
        self.cache_root = Path(self.env.get("SIMAI_CACHE_ROOT") or "/tmp/simai-cache")
        self.control_port = int(self.env.get("SIMAI_BOOT_CONTROL_PORT") or CONTROL_PORT)
        self.video_port = int(self.env.get("SIMAI_BOOT_VIDEO_PORT") or VIDEO_PORT)

    # -- 설정 확인
    def config(self) -> tuple[str, str, str]:
        secret = self.secrets.get(BOOT_SECRET_ENV)
        boot_id = (self.env.get(BOOT_ID_ENV) or "").strip()
        ec2 = (self.env.get(EC2_ENV) or "").strip().rstrip("/")
        missing = [n for n, v in ((BOOT_SECRET_ENV, secret), (BOOT_ID_ENV, boot_id), (EC2_ENV, ec2)) if not v]
        if missing:
            raise BootError("BAD_CONFIG", f"부팅 환경 변수가 없다: {', '.join(missing)}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", boot_id):
            raise BootError("BAD_CONFIG", f"{BOOT_ID_ENV} 형식 오류")
        return boot_id, secret, ec2  # type: ignore[return-value]

    def load_aws_secrets(self) -> None:
        """AWS: Secrets Manager 에서 비밀 값을 받아 비어 있는 것만 채운다. 막 켠 인스턴스는 역할 자격이 늦게 뜰 수 있어
        SIMAI_AWS_SECRETS_WAIT_S(기본 180 s) 동안 2 s 부터 두 배(30 s 상한)로 다시 한다. 거절(권한·이름)은 바로 멈춘다."""
        wait_s = float(self.env.get("SIMAI_AWS_SECRETS_WAIT_S") or 180)
        deadline, delay, attempt = time.monotonic() + wait_s, 2.0, 0
        while True:
            attempt += 1
            try:
                values = fetch_aws_secret(self.env)
                break
            except AwsTemporary as e:
                log(f"Secrets Manager 받기 실패({attempt}번째): {e}")
                if time.monotonic() + delay > deadline or self.stop.wait(delay):
                    raise BootError("SECRETS_UNAVAILABLE", f"{wait_s:.0f} s 안에 비밀 값을 받지 못했다") from e
                delay = min(delay * 2, 30.0)
        for k in AWS_SECRET_KEYS:
            if values.get(k) and not self.secrets.get(k):
                self.secrets[k] = values[k]
                _secrets.add(values[k])
        for k in AWS_PUBLIC_KEYS:
            if values.get(k) and not (self.env.get(k) or "").strip():
                self.env[k] = values[k]
        got = [k for k in AWS_SECRET_KEYS if values.get(k)]
        log(f"Secrets Manager 에서 비밀 값 {len(got)}개를 받았다(이름만: {', '.join(got) or '없음'})")

    def ended_marker(self) -> Path:
        return self.state_dir / "ended.json"

    def resolve_tls(self) -> tuple[str, str, str]:
        """(a) 공급자 비밀 저장소의 고정 키(노트북 고정 지문 그대로). 없으면 .simai-private/tls/ 파일(엘리스 블록 스토리지 등)."""
        files = self.root.parent / ".simai-private" / "tls"
        cert = pem_text(self.env.get(TLS_CERT_ENV), "BEGIN CERTIFICATE")
        key = pem_text(self.secrets.get(TLS_KEY_ENV), "PRIVATE KEY")
        if cert is None and (files / "tier0.crt").is_file():
            cert = (files / "tier0.crt").read_text(encoding="utf-8")
        if key is None and (files / "tier0.key").is_file():
            key = (files / "tier0.key").read_text(encoding="utf-8")
        if not cert or not key:
            raise BootError("TLS_KEY_MISSING", f"Tier 0 TLS 인증서·비밀 키가 없다({TLS_CERT_ENV}·{TLS_KEY_ENV} 또는 .simai-private/tls/)")
        if "PRIVATE KEY" in cert:
            raise BootError("TLS_KEY_MISSING", "인증서 값에 비밀 키가 섞여 있다")
        _secrets.add(key.strip())
        self.private.mkdir(parents=True, exist_ok=True)
        probe = self.private / "tier0-cert-probe.crt"
        probe.write_text(cert, encoding="utf-8", newline="\n")
        try:
            spki = self.bs.spki_sha256_from_pem_file(probe)
        except self.bs.BootstrapError as e:
            raise BootError("TLS_KEY_MISSING", f"인증서를 읽지 못함: {e}") from e
        finally:
            probe.unlink(missing_ok=True)
        pin = (self.env.get(PIN_ENV) or "").strip().lower()
        if pin:
            m = _SHA_RE.match(pin)
            if m is None or f"sha256:{m.group(1)}" != spki:
                raise BootError("TLS_PIN_MISMATCH", f"인증서 지문 {spki} 이 노트북 고정 지문({PIN_ENV})과 다르다")
        return cert, key, spki

    # -- 등록
    def register(self, boot_id: str, secret: str, ec2: str, payload: dict) -> dict:
        """EC2 가 잠깐 없어도(재배포 1~2분·망 끊김) 멈출 때까지 다시 한다(2초부터 두 배, 60초 상한). 4xx 는 설정 문제라 다시 하지 않는다."""
        delay, attempt = 2.0, 0
        while True:
            attempt += 1
            try:
                status, body = self.http.json("POST", ec2 + REGISTER_PATH, envelope("gpu_session_register", boot_id, payload),
                                              bearer=secret)
            except (OSError, BootError) as e:
                status, body = None, None
                log(f"등록 요청 실패({attempt}번째): {type(e).__name__}")
            if status == 201 and body and isinstance(body.get("payload"), dict):
                return body["payload"]
            if status in (400, 401, 403, 404):
                code = (body or {}).get("code")
                raise BootError("REGISTER_REJECTED", f"등록 거부 HTTP {status} {code or ''}".strip())
            if status is not None:
                log(f"등록 응답 HTTP {status}({attempt}번째)")
            if self.stop.wait(delay):
                raise BootError("REGISTER_FAILED", "등록 전에 멈춤")
            delay = min(delay * 2, 60.0)

    def child_env(self, extra: dict[str, str] | None = None, *, hf: bool = True) -> dict[str, str]:
        e = {k: v for k, v in self.env.items() if k not in (*SECRET_ENVS, *HF_ENVS)}
        if hf and self.secrets.get("HF_TOKEN"):
            e["HF_TOKEN"] = self.secrets["HF_TOKEN"]  # 가중치를 받는 simai-bootstrap 만(그 안에서 다시 꺼내 자식에 넘기지 않는다)
        e["SIMAI_ROOT"] = str(self.root)
        e.update(extra or {})
        return e

    # -- 캐시 키에 쓸 값
    def tier0_weight(self) -> dict:
        entries = self.bs.load_manifest(self.manifest, "tier0")
        return next((w for w in entries if w.get("env") == "SIMAI_TIER0_WEIGHTS"), entries[0])

    def vlm_shas(self) -> list[str]:
        try:
            return [w["sha256"] for w in self.bs.load_manifest(self.manifest, "vlm") if w.get("sha256")]
        except self.bs.BootstrapError:
            return []

    def versions(self) -> dict[str, str | None]:
        return {"tensorrt": dist_version("tensorrt-cu13-bindings", "tensorrt-cu13", "tensorrt"),
                "ultralytics": dist_version("ultralytics"), "torch": dist_version("torch"),
                "modelopt": dist_version("nvidia-modelopt"), "vllm": dist_version("vllm"), "triton": dist_version("triton")}

    # -- 본 흐름
    def run(self) -> int:
        if self.ended_marker().is_file():
            log(f"이 인스턴스의 GPU 세션은 이미 끝났다({self.ended_marker()}). 다시 등록하지 않고 기다린다(다시 하려면 그 파일을 지운다)")
            return self.idle()
        if (self.env.get(SECRETS_SOURCE_ENV) or "").strip().lower() == AWS_SECRETS_SOURCE:
            try:
                self.load_aws_secrets()
            except BootError as e:
                log(f"비밀 값 받기 실패 {e.reason}: {e.detail} → 등록하지 않고 기다린다(서버 자동 정지가 BOOT_TIMEOUT 으로 끈다)")
                return self.idle()
        try:
            boot_id, secret, ec2 = self.config()
        except BootError as e:
            log(f"{e.detail} (종료 코드 {EXIT_USAGE})")
            return EXIT_USAGE
        tls_error: BootError | None = None
        cert = key = spki = None
        try:
            gpu = query_gpu(self.smi, self.child_env(hf=False))
            try:
                cert, key, spki = self.resolve_tls()
            except BootError as e:
                tls_error = e
            payload = register_facts(self.env, gpu, self.control_port, self.video_port, spki)
            result = self.register(boot_id, secret, ec2, payload)
        except BootError as e:
            log(f"등록 전 실패 {e.reason}: {e.detail} → 등록하지 않고 기다린다(수동 경로 가능)")
            return self.idle()
        session_id = result["session_id"]
        worker_id, gateway_id = result["worker"]["worker_id"], result["tier0"]["gateway_id"]
        _secrets.update({result["worker"]["token"], result["tier0"]["token"]})
        worker_file = write_private(self.private / f"{worker_id}.token", result["worker"]["token"] + "\n")
        tier0_file = write_private(self.private / "tier0.token", result["tier0"]["token"] + "\n")
        (self.private / "session.json").write_text(json.dumps({  # 비밀 아님: 손 경로·seed-reid 가 읽는다
            "session_id": session_id, "worker_id": worker_id, "gateway_id": gateway_id, "ec2_base_url": ec2,
            "worker_token_file": str(worker_file), "tier0_token_file": str(tier0_file),
            "registered_at": result.get("registered_at")}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"GPU 세션 {session_id} 등록(워커 {worker_id}, gateway {gateway_id}, 밀어낸 세션 {len(result.get('superseded_session_ids') or [])}개). "
            f"토큰 파일 {self.private}")
        rep = self.reporter = Reporter(self.http, ec2, boot_id, secret, session_id, result["report"]["interval_ms"] / 1000)
        rep.start()
        threading.Thread(target=self.measure_cpu, args=(rep,), name="host-cpu", daemon=True).start()
        self.cache = GpuCache(self.http, ec2, result["worker"]["token"], self.root / "tmp" / "gpu-cache", notice=rep.add_note)
        try:
            if tls_error is not None:
                raise tls_error
            return self.prepare_and_launch(result, gpu, cert, key)  # type: ignore[arg-type]
        except BootError as e:
            rep.set("FAILED", e.reason, e.detail)
            log(f"기동 실패 {e.reason}: {e.detail} → FAILED 를 보고하며 기다린다(손 경로: 토큰 파일 {self.private})")
            return self.idle()

    def measure_cpu(self, rep: Reporter) -> None:
        """WEIGHTS 단계 동안 백그라운드로 호스트 CPU 를 재서 보고에 싣는다(부팅을 늦추지 않음, 실패해도 계속)"""
        try:
            doc = self.cpu_measure()
            rep.set_host_cpu(doc)
            score = doc.get("single_thread_score")
            if score is not None and score < 1000:
                warning = f"CPU 단일 스레드 점수 {score}가 초기 기준 1000 미만: 판정 지연 가능"
                log(f"경고: {warning}")
            log(f"호스트 CPU: {doc['model']} · 논리 {doc['logical_cpus']} · 몫 {doc['cpu_quota']} · 단일 스레드 점수 {doc['single_thread_score']}")
        except Exception as e:
            log(f"호스트 CPU 측정 실패(무시하고 계속): {type(e).__name__}")

    cpu_measure = staticmethod(measure_host_cpu)

    def prepare_and_launch(self, result: dict, gpu: dict, cert: str, key: str) -> int:
        rep, cache = self.reporter, self.cache
        assert rep is not None and cache is not None
        start = result["start"]
        env = self.env
        versions = self.versions()
        tier0_w = self.tier0_weight()
        trt_key = trt_cache_key(gpu, tier0_w["sha256"], env, versions)
        vllm_key = vllm_cache_key(gpu, self.vlm_shas(), env, versions)
        reid_dir, reid_key = self.root / "reid", reid_cache_key(env)
        engines = Path(env.get("SIMAI_ENGINE_CACHE") or self.root / "engines")
        vllm_dir = self.cache_root / "vllm"
        # VRAM 별 lobby 서비스(명시값 우선). 줄였으면 모든 단계 보고 설명에 남긴다(작은 GPU 에서 vLLM OOM 으로 「준비 실패」만 보이지 않게)
        services, note = boot_services(env, gpu, self.bs.MIN_WORKER_VRAM_MB, self.bs.MIN_VRAM_MB)
        if note:
            rep.note = note
            log(f"서비스 축소: {note} (--services {services or self.bs.LOBBY_SERVICES}, 바꾸려면 SIMAI_BOOT_SERVICES)")
        rep.set("WEIGHTS", detail="캐시·Tier 0 가중치·시작 묶음")

        # 뒤에서: vLLM 컴파일 캐시(vLLM 은 VLM 가중치 뒤에 뜨므로 Tier 0 를 막지 않는다). 다른 이름으로 풀고 아직 없을 때만 옮긴다
        def fetch_vllm() -> None:
            if vllm_key is None or vllm_dir.exists():
                return
            staging = self.cache_root / ".vllm-staging"
            shutil.rmtree(staging, ignore_errors=True)
            if cache.fetch("vllm-compile", vllm_key, staging) == "hit":
                try:
                    os.rename(staging, vllm_dir)
                except OSError:
                    log("vLLM 캐시: vLLM 이 먼저 폴더를 만들어 받은 캐시를 쓰지 않는다")
        threads = [threading.Thread(target=fetch_vllm, name="cache-vllm", daemon=True)]
        if not reid_ready(reid_dir):
            threads.append(threading.Thread(target=lambda: cache.fetch("reid", reid_key, reid_dir), name="cache-reid", daemon=True))
        if trt_key is not None:
            threads.append(threading.Thread(target=lambda: cache.fetch("trt-engine", trt_key, engines), name="cache-trt", daemon=True))
        else:
            log("TensorRT 엔진 캐시 키를 만들 수 없다(TensorRT·ultralytics 판을 모름) → 캐시 없이 빌드")
        for t in threads:
            t.start()

        # 시작 묶음(보정 sha256 확인·기기 토큰 지문·TLS). config 가 없으면 세팅 경로(아래)
        bundle = self.private / "lobby-bundle"
        no_config: BootError | None = None
        try:
            write_bundle(self.http, bundle, start, cert, key)
        except BootError as e:
            if e.reason != "NO_START_CONFIG":
                raise
            no_config = e

        # Tier 0 가중치·GPU 예산·런타임(세팅·VLM 가중치는 lobby 가 Tier 0 준비 뒤 뒤에서 받는다)
        pre = [*self.bootstrap_cmd, "preflight", "--sets", "tier0", *(["--services", services] if services else [])]
        code = subprocess.run(pre, env=self.child_env(), stdout=subprocess.DEVNULL, check=False).returncode
        if code != 0:
            reason = {2: "WEIGHTS_FAILED", 3: "GPU_TOO_SMALL"}.get(code, "PREFLIGHT_FAILED")
            raise BootError(reason, f"preflight(tier0) 종료 코드 {code}")
        for t in threads:
            if t.name != "cache-vllm":
                t.join(timeout=float(env.get("SIMAI_BOOT_CACHE_WAIT_S") or 600))
        # 여기부터 보고에 「꺼진 기능」 목록(비어 있으면 꺼진 것 없음)을 싣는다
        rep.replace_disabled(cache_findings({k: v for k, v in cache.results.items() if k != "vllm-compile"}, reid_ready(reid_dir)))
        if not reid_ready(reid_dir):
            log("Re-ID 묶음 없음(캐시 miss·실패) → 이번 관제는 Re-ID 꺼짐. 손으로 올린 뒤 'simai-gpu-boot seed-reid' 로 캐시에 올린다")

        if no_config is not None:
            return self.setup_path(no_config, tier0_w, engines, trt_key, gpu)

        extra = {"SIMAI_CACHE_ROOT": str(self.cache_root), "VLLM_CACHE_ROOT": str(vllm_dir / "vllm")}
        if reid_ready(reid_dir):
            extra.update({k: v for k, v in (("SIMAI_COPE_ROOT", str(reid_dir / "cope")),
                                            ("SIMAI_COPE_WEIGHTS", str(reid_dir / REID_FILES[0])),
                                            ("SIMAI_COPE_CLIP_PATH", str(reid_dir / REID_FILES[1]))) if not env.get(k)})
        work = self.root / "runs" / f"lobby-gpu-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        argv = lobby_argv(self.bootstrap_cmd, work=work, bundle=bundle, start=start, worker_id=result["worker"]["worker_id"],
                          gateway_id=result["tier0"]["gateway_id"],
                          public_host=(env.get("SIMAI_TIER0_ADVERTISE_HOST") or DEFAULT_ADVERTISE_HOST),
                          control_port=self.control_port, video_port=self.video_port, services=services,
                          worker_token_file=self.private / f"{result['worker']['worker_id']}.token",
                          tier0_token_file=self.private / "tier0.token")
        return self.supervise_lobby(argv, work, extra, services, engines, tier0_w, trt_key, vllm_key, vllm_dir, reid_dir, reid_key, gpu)

    def setup_path(self, why: BootError, tier0_w: dict, engines: Path, trt_key: str | None, gpu: dict) -> int:
        """구성 묶음이 없다: 보정 없이 Tier 0 를 띄울 수 없다. 엔진만 미리 만들어(캐시에도 올려) 손 세팅이 바로 이어 가게 하고 FAILED 로 기다린다."""
        rep, cache = self.reporter, self.cache
        assert rep is not None and cache is not None
        rep.set("ENGINE", detail="구성 묶음 없음: 엔진만 미리 준비")
        e = self.child_env()
        args = [sys.executable, "-m", "simai_ai.detection.engine_cache", "ensure", "--weights", str(self.root / tier0_w["path"]),
                "--cache-root", str(engines), "--expect-sha256", tier0_w["sha256"],
                "--mode", e.get("SIMAI_TIER0_MODE") or "e2e", "--imgsz", e.get("SIMAI_TIER0_IMGSZ") or "1280",
                "--max-batch", e.get("SIMAI_TIER0_MAX_BATCH") or "4", "--camera", e.get("SIMAI_TIER0_CAMERA") or "2304x1296"]
        code = subprocess.run(args, env=self.bs.with_image_pythonpath(e), stdout=subprocess.DEVNULL, check=False).returncode
        if code == 0:
            self.store_engine(engines, tier0_w, trt_key, gpu)
        raise BootError(why.reason, why.detail + " → 세팅은 손 경로(lobby-bundle 을 올리고 start-lobby, 토큰 파일 그대로)")

    def store_engine(self, engines: Path, tier0_w: dict, trt_key: str | None, gpu: dict) -> None:
        if trt_key is None or self.cache is None or self.cache.results.get("trt-engine") == "hit" or not gpu.get("arch"):
            return
        members = engine_members(engines, Path(tier0_w["path"]).stem, gpu["arch"], tier0_w["sha256"][:12], self.env)
        if members:
            self.cache.store("trt-engine", trt_key, engines, members)

    def supervise_lobby(self, argv: list[str], work: Path, extra: dict[str, str], services: str | None, engines: Path,
                        tier0_w: dict, trt_key: str | None, vllm_key: str | None, vllm_dir: Path, reid_dir: Path,
                        reid_key: str, gpu: dict) -> int:
        rep, cache = self.reporter, self.cache
        assert rep is not None and cache is not None
        if self.stop.is_set():  # 준비 중에 SIGTERM: lobby 를 띄우지 않고 ENDED
            return self.idle()
        want = [s.strip() for s in (services or self.bs.LOBBY_SERVICES).split(",") if s.strip()]
        want_vllm = "vllm" in want and (gpu.get("memory_total_mb") or 0) >= self.bs.MIN_WORKER_VRAM_MB
        services_off = service_findings(want, (gpu.get("memory_total_mb") or 0) >= self.bs.MIN_WORKER_VRAM_MB,
                                        {s.strip() for s in self.env.get("SIMAI_BOOT_DISABLE", "").split(",") if s.strip()})
        for key, item in services_off.items():
            rep.set_disabled(key, item["text"], item["level"], item.get("detail"))
        vllm_port = int(self.env.get("SIMAI_VLLM_PORT") or 8000)
        vllm_timeout = float(self.env.get("SIMAI_BOOT_VLLM_READY_TIMEOUT_S") or 1800)
        started = now_ms()
        log("lobby 기동: " + " ".join(shlex.quote(a) for a in argv))
        self.child = subprocess.Popen(argv, env=self.child_env(extra), start_new_session=os.name != "nt")
        rep.set("ENGINE", detail="TensorRT 엔진 준비·적재")
        stage, ready_at, uploaded = "ENGINE", None, set()
        warmed = False
        tier0_ready_at = None
        warmup_failure: str | None = None
        last_refresh = float("-inf")
        while not self.stop.is_set():
            if rep.ended.is_set():
                self.stop_child("세션 끝남")
                self.mark_ended()
                return self.idle()
            code = self.child.poll()
            if code is not None:
                reason, detail = lobby_failure(code, stage)
                raise BootError(reason, detail)
            healthy = want_vllm and self.bs.http_ok(f"http://127.0.0.1:{vllm_port}/health")
            timed_out = tier0_ready_at is not None and time.monotonic() - tier0_ready_at > vllm_timeout
            new, detail = lobby_stage(read_marker(work / "state" / "tier0.live"), started, want_vllm, healthy, timed_out)
            if new in ("VLLM", "READY") and tier0_ready_at is None:
                tier0_ready_at = time.monotonic()
            doc = read_marker(work / "state" / "tier0.live")
            has_status = isinstance(doc, dict) and isinstance(doc.get("status"), dict)
            # Tier 0 가 돌기 시작한 뒤 표시 파일 status 로 꺼진 기능을 확인하고, READY 뒤에도 보고 주기(report.interval_ms)마다 다시 읽어
            # 다시 계산한다(S15P31A101-756: seed-reid·restart-tier0 로 Re-ID 가 켜지면 항목이 빠진다). 표시 파일에 status 가 없으면(재시작 중 등) 앞 값을 둔다
            if new in ("VLLM", "READY") and has_status and time.monotonic() - last_refresh >= rep.interval_s:
                last_refresh = time.monotonic()
                rep.replace_disabled(compose_disabled(
                    services=services_off, cache_results=dict(cache.results), reid_on_disk=reid_ready(reid_dir), marker_doc=doc,
                    vlm_warmup_failure=warmup_failure, vlm_timed_out=new == "READY" and want_vllm and not healthy,
                    include_vllm_cache=warmed))
            if new == "READY" and healthy and not warmed:
                # READY 보고 전 vLLM 더미 멀티모달 요청 1회(JIT·첫 요청 지연을 근거 요청 전에 흡수). 실패해도 부팅은 계속한다
                warmed = True
                rep.set("VLLM", detail="VLM 웜업(더미 이미지 요청 1회)")
                ok, took_s, why = warmup_vlm(self.child_env(extra))
                log(f"VLM 웜업 {'성공' if ok else '실패'}: {took_s:.1f}초 ({why})")
                rep.add_note(f"VLM 웜업 {took_s:.1f}초" + ("" if ok else f" 실패({why})"))
                if not ok:
                    warmup_failure = f"VLM 웜업 실패({why})"
                last_refresh = float("-inf")  # 웜업 결과(실패·vLLM 캐시 miss)를 바로 목록에 반영한다
                continue  # 다음 주기에 READY 로 보고한다
            if new != stage or (new == "READY" and detail != rep.state.get("detail")):
                rep.set(new, detail=detail)
                stage = new
            # 캐시 올리기(뒤에서, 한 번씩): 엔진은 Tier 0 준비 뒤, vLLM 은 /health 뒤 잠시, Re-ID 는 손으로 둔 묶음이 캐시에 없을 때
            if stage in ("VLLM", "READY") and "trt" not in uploaded:
                uploaded.add("trt")
                threading.Thread(target=self.store_engine, args=(engines, tier0_w, trt_key, gpu), daemon=True).start()
                if reid_ready(reid_dir) and cache.results.get("reid") in ("miss", None):
                    threading.Thread(target=cache.store, args=("reid", reid_key, reid_dir, reid_members(reid_dir)),
                                     daemon=True).start()
            if healthy and ready_at is None:
                ready_at = time.monotonic()
            settle = float(self.env.get("SIMAI_BOOT_VLLM_CACHE_SETTLE_S") or 60)
            if (ready_at is not None and "vllm" not in uploaded and time.monotonic() - ready_at > settle
                    and vllm_key is not None and cache.results.get("vllm-compile") != "hit" and vllm_dir.is_dir()):
                uploaded.add("vllm")
                threading.Thread(target=cache.store, args=("vllm-compile", vllm_key, vllm_dir, dir_members(vllm_dir)),
                                 daemon=True).start()
            self.stop.wait(self.poll_s)
        self.stop_child("SIGTERM")
        rep.stop("ENDED", "PROVIDER_STOP", "인스턴스 정지(SIGTERM)")
        return EXIT_OK

    def stop_child(self, why: str) -> None:
        c = self.child
        if c is None or c.poll() is not None:
            return
        log(f"lobby 정지({why})")
        try:
            os.killpg(c.pid, signal.SIGTERM) if hasattr(os, "killpg") else c.terminate()
        except OSError:
            pass
        try:
            c.wait(timeout=float(self.env.get("SIMAI_STOP_GRACE_S") or 15) + 30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(c.pid, signal.SIGKILL) if hasattr(os, "killpg") else c.kill()
            except OSError:
                pass

    def mark_ended(self) -> None:
        """EC2 가 이 세션을 끝냈다(409: 새 등록에 밀림·무응답·폐기·관리자 정지). 컨테이너가 같은 디스크로 다시 떠도 다시 등록해 진짜 세션을
        밀어내지 않게 남긴다. 스스로 멈춘 경우(SIGTERM)는 남기지 않는다(사람이 다시 켜면 다시 등록해야 한다)."""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            sid = self.reporter.session_id if self.reporter else None
            self.ended_marker().write_text(json.dumps({"session_id": sid, "ended_at": now_ms()}) + "\n", encoding="utf-8")
        except OSError:
            pass

    def idle(self) -> int:
        """컨테이너를 끝내지 않고 기다린다(보고기가 있으면 마지막 단계를 계속 보고). SIGTERM 이면 ENDED 를 보내고 0."""
        while not self.stop.wait(self.poll_s):
            if self.reporter is not None and self.reporter.ended.is_set():
                self.mark_ended()
                self.reporter.stop()
                self.reporter = None
        self.stop_child("SIGTERM")
        if self.reporter is not None:
            self.reporter.stop("ENDED", "PROVIDER_STOP", "인스턴스 정지(SIGTERM)")
        return EXIT_OK


def seed_reid(argv: list[str]) -> int:
    """손으로 올린 $SIMAI_ROOT/reid 를 GPU 캐시에 올린다(이 Pod 의 세션 워커 토큰, gpu-session/session.json)."""
    env = dict(os.environ)
    bs = load_bootstrap(env)
    root = Path(bs.choose_root(env)[0])
    private = Path(env.get("SIMAI_GPU_BOOT_PRIVATE_DIR") or (root.parent / ".simai-private" / "gpu-session"))
    reid = Path(argv[argv.index("--dir") + 1]) if "--dir" in argv else root / "reid"
    try:
        info = json.loads((private / "session.json").read_text(encoding="utf-8"))
        token = Path(info["worker_token_file"]).read_text(encoding="utf-8").strip()
    except (OSError, ValueError, KeyError) as e:
        log(f"세션 정보·워커 토큰을 읽지 못함({type(e).__name__}): auto 로 등록한 Pod 에서만 쓴다 (종료 코드 {EXIT_USAGE})")
        return EXIT_USAGE
    _secrets.add(token)
    if not reid_ready(reid):
        log(f"{reid} 에 cope/ 와 {', '.join(REID_FILES)} 가 없다 (종료 코드 {EXIT_USAGE})")
        return EXIT_USAGE
    cache = GpuCache(Http(ca_file=env.get("SIMAI_EC2_CA_FILE")), info["ec2_base_url"], token, root / "tmp" / "gpu-cache")
    result = cache.store("reid", reid_cache_key(env), reid, reid_members(reid))
    log(f"Re-ID 캐시: {result}")
    return EXIT_OK if result in ("stored", "exists") else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return EXIT_OK if args else EXIT_USAGE
    if args[0] == "seed-reid":
        return seed_reid(args[1:])
    if args[0] != "auto":
        log(f"모르는 명령 {args[0]!r} (종료 코드 {EXIT_USAGE})")
        return EXIT_USAGE
    try:
        boot = Boot()
    except BootError as e:
        log(f"{e.detail} (종료 코드 {EXIT_USAGE})")
        return EXIT_USAGE
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: boot.stop.set())
    return boot.run()


if __name__ == "__main__":
    sys.exit(main())
