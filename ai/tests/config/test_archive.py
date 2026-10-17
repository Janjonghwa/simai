"""검증 묶음 보관과 재시작 복구 (명세 3.2 재시작 복구, 오프라인 중 묶음 유지, 규칙 6 덮어쓰지 않음)."""

import pytest

from simai_ai.config import BundleArchive, BundleRejected, ConfigSync, ConfigSyncState, RejectReason
from simai_ai.risk import default_registry


def restore(kit, archive, version):
    return archive.restore(version, registry=default_registry(), calibration_known=kit.known, clock=kit.clock)


def test_saved_bundle_restores_offline(kit, tmp_path):
    archive = BundleArchive(tmp_path)
    sync = ConfigSync(kit.verifier(), kit.clock, source_id="tier0-01", store=archive)
    sync.on_connected()
    sync.receive(kit.publish("cfg-12"))
    kit.clock.advance(10)
    applied_at = sync.begin_tick().applied_at

    kit.fetcher.blobs.clear()  # EC2·객체 저장소에 닿지 못한다
    bundle = restore(kit, archive, "cfg-12")
    assert bundle.bundle_hash == sync.active.bundle.bundle_hash
    assert bundle.zones == sync.active.zones
    assert bundle.rule_versions == sync.active.bundle.rule_versions

    restarted = ConfigSync(
        kit.verifier(), kit.clock, source_id="tier0-01", restored=bundle, restored_applied_at=applied_at
    )
    assert restarted.state is ConfigSyncState.UNVERIFIED
    assert restarted.begin_tick().config_version == "cfg-12"
    assert restarted.version_at(applied_at) == "cfg-12"


def test_save_never_overwrites(kit, tmp_path):
    archive = BundleArchive(tmp_path)
    bundle = kit.verifier().verify(kit.publish("cfg-12"))
    path = archive.save(bundle)
    notice = (path / "notice.json").read_bytes()
    assert archive.save(bundle) == path
    assert (path / "notice.json").read_bytes() == notice
    assert [n["payload"]["config_version"] for n in archive.notices()] == ["cfg-12"]
    assert not list((tmp_path / "staging").iterdir())


def test_corrupted_archive_is_rejected(kit, tmp_path):
    archive = BundleArchive(tmp_path)
    bundle = kit.verifier().verify(kit.publish("cfg-12"))
    path = archive.save(bundle)
    blob = path / "zones.json"
    blob.write_bytes(blob.read_bytes() + b" ")
    with pytest.raises(BundleRejected) as info:
        restore(kit, archive, "cfg-12")
    assert info.value.reason is RejectReason.HASH_MISMATCH


def test_missing_version_raises_key_error(kit, tmp_path):
    with pytest.raises(KeyError):
        restore(kit, BundleArchive(tmp_path), "cfg-404")


def test_store_failure_does_not_block_switch(kit):
    class BrokenStore:
        def save(self, bundle):
            raise OSError("볼륨 없음")

    sync = ConfigSync(kit.verifier(), kit.clock, source_id="tier0-01", store=BrokenStore())
    sync.on_connected()
    assert sync.receive(kit.publish("cfg-12")).accepted
    assert sync.begin_tick().config_version == "cfg-12"


def test_restore_by_hash_when_same_version_saved_twice(kit, tmp_path):
    """재시작 사이에 같은 버전의 다른 묶음이 둘 다 보관됐으면 해시 없이는 고르지 않는다."""
    archive = BundleArchive(tmp_path)
    first = kit.verifier().verify(kit.publish("cfg-12"))
    archive.save(first)
    second = kit.verifier().verify(kit.publish("cfg-12", zones=[]))
    archive.save(second)
    with pytest.raises(KeyError):
        restore(kit, archive, "cfg-12")
    got = archive.restore(
        "cfg-12",
        registry=default_registry(),
        calibration_known=kit.known,
        clock=kit.clock,
        bundle_hash=second.bundle_hash,
    )
    assert got.bundle_hash == second.bundle_hash and got.zones == ()


def test_broken_notice_folder_is_skipped(kit, tmp_path):
    archive = BundleArchive(tmp_path)
    archive.save(kit.verifier().verify(kit.publish("cfg-12")))
    broken = tmp_path / "bundles" / "broken"
    broken.mkdir()
    (broken / "notice.json").write_text("{not json", encoding="utf-8")
    assert [n["payload"]["config_version"] for n in archive.notices()] == ["cfg-12"]
    assert restore(kit, archive, "cfg-12").config_version == "cfg-12"
