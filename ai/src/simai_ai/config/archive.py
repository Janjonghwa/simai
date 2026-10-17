"""검증한 구성 묶음 보관소 (명세 3.2 재시작 복구, 6장 오프라인 중 묶음 유지, AGENTS 규칙 6).

Tier 0 가 EC2 와 끊긴 채 재시작해도 체크포인트의 config_version 으로 판정을 이어 가려면 묶음 내용이 Network Volume 에
있어야 한다. 검증을 통과한 묶음의 알림 원본과 내려받은 바이트를 그대로 저장하고, 복구할 때 같은 검증기(BundleVerifier)를
로컬 사본으로 다시 돌린다(보관 중 깨졌으면 HASH_MISMATCH 로 드러난다).

    <root>/bundles/<bundle_hash hex>/notice.json       받은 config_bundle 메시지
    <root>/bundles/<bundle_hash hex>/manifest.json     매니페스트 원본 바이트
    <root>/bundles/<bundle_hash hex>/<구성 요소>.json   zones·rules·calibration·operational_geometry 원본 바이트

- 덮어쓰지 않는다: 임시 폴더에 다 쓰고(fsync) 이름을 바꿔 한 번에 내놓는다. 이미 있으면 그대로 둔다.
- 저장은 I1 수신 쪽(ConfigSync.receive)에서 하고 판정 스레드에서 하지 않는다.
- 어떤 묶음이 언제 적용됐는지(config_version·applied_at)는 체크포인트(3.2)가 기록한다. 여기는 내용만 둔다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from simai_ai.config.bundle import COMPONENT_NAMES, ConfigBundle, thaw_json
from simai_ai.config.verify import BundleVerifier, CalibrationKnown, FetchError
from simai_ai.risk import Clock, RuleRegistry

log = logging.getLogger("simai_ai.config.archive")


def _hex(digest: str) -> str:
    return digest.removeprefix("sha256:")


def _write(path: Path, data: bytes) -> None:
    with open(path, "xb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


class _LocalFetcher:
    """알림의 uri → 보관한 파일."""

    def __init__(self, folder: Path, notice: Mapping[str, Any]) -> None:
        payload = notice["payload"]
        self._folder = folder
        self._by_uri = {payload["bundle_uri"]: "manifest"}
        for name in COMPONENT_NAMES:
            self._by_uri[payload["components"][name]["uri"]] = name

    def fetch(self, uri: str) -> bytes:
        name = self._by_uri.get(uri)
        if name is None:
            raise FetchError("보관한 묶음에 없는 주소")
        try:
            return (self._folder / f"{name}.json").read_bytes()
        except OSError as e:
            raise FetchError(f"보관 사본을 읽지 못했다 ({type(e).__name__})") from None


def _files(bundle: ConfigBundle) -> dict[str, bytes]:
    out = {"manifest": bundle.blobs[bundle.bundle_hash]}
    for name in COMPONENT_NAMES:
        out[name] = bundle.blobs[bundle.notice.components[name].sha256]
    return out


class BundleArchive:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)

    def save(self, bundle: ConfigBundle) -> Path:
        """묶음을 보관한다. 이미 있으면 그대로 두고 그 위치를 돌려준다."""
        dest = self._root / "bundles" / _hex(bundle.bundle_hash)
        if dest.exists():
            return dest
        staging = self._root / "staging" / uuid.uuid4().hex
        staging.mkdir(parents=True)
        try:
            notice = json.dumps(thaw_json(bundle.notice.message), ensure_ascii=False, sort_keys=True)
            _write(staging / "notice.json", notice.encode("utf-8"))
            for name, data in _files(bundle).items():
                _write(staging / f"{name}.json", data)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(staging, dest)  # 폴더 이름 바꾸기: 있으면 실패하고 기존 것을 덮지 않는다
            except OSError:
                if not dest.exists():
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return dest

    def notices(self) -> list[Mapping[str, Any]]:
        """보관한 알림 원본들(issued_at 순). 읽지 못하는 폴더는 건너뛰고 로그를 남긴다."""
        out = [notice for _, notice in self._folders()]
        return sorted(out, key=lambda m: m["payload"]["issued_at"])

    def _folders(self) -> list[tuple[Path, Mapping[str, Any]]]:
        out = []
        for folder in sorted((self._root / "bundles").glob("*")):
            path = folder / "notice.json"
            if not path.is_file():
                continue
            try:
                notice = json.loads(path.read_text(encoding="utf-8"))
                payload = notice["payload"]
                if not all(k in payload for k in ("config_version", "bundle_hash", "issued_at")):
                    raise KeyError("payload")
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("보관 폴더 %s 의 notice.json 을 읽지 못해 건너뛴다", folder.name)
                continue
            out.append((folder, notice))
        return out

    def restore(
        self,
        config_version: str,
        *,
        registry: RuleRegistry,
        calibration_known: CalibrationKnown,
        clock: Clock,
        bundle_hash: str | None = None,
    ) -> ConfigBundle:
        """보관한 묶음을 같은 검증으로 다시 만든다. 없으면 KeyError, 깨졌으면 BundleRejected.

        bundle_hash(체크포인트의 해시)를 주면 (config_version, bundle_hash) 로 찾는다. 재시작 사이에 같은 버전의 다른
        묶음이 둘 다 보관됐을 수 있어, 주지 않았는데 후보가 둘 이상이면 고르지 않고 KeyError 를 낸다.
        """
        candidates = [
            (folder, notice)
            for folder, notice in self._folders()
            if notice["payload"]["config_version"] == config_version
            and (bundle_hash is None or notice["payload"]["bundle_hash"] == bundle_hash)
        ]
        if len(candidates) > 1:
            raise KeyError(f"보관한 묶음에 {config_version} 이 여럿이다. bundle_hash 로 고른다")
        for folder, notice in candidates:
            verifier = BundleVerifier(_LocalFetcher(folder, notice), registry, calibration_known, clock)
            return verifier.verify(notice)
        raise KeyError(f"보관한 묶음에 {config_version} 이 없다")
