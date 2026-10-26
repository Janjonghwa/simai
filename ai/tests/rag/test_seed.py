"""임베딩 파일·DB 시드 SQL (S15P31A101-27). 모델 없이 돈다(커밋된 float32 임베딩을 쓴다)."""

import math
import random
import struct
from pathlib import Path

import pytest

from simai_ai.rag import seed
from simai_ai.rag.embed import DIM

SEED_SQL = Path(__file__).resolve().parents[3] / "server/src/main/resources/db/migration/R__rag_clauses.sql"


def test_f32_text_round_trips_exactly():
    rng = random.Random(27)
    values = [struct.unpack("<f", struct.pack("<f", rng.uniform(-1, 1) * 10 ** rng.randint(-8, 0)))[0] for _ in range(5000)]
    values += [0.0, -0.0, 1.0, struct.unpack("<f", struct.pack("<f", 1e-38))[0]]
    for x in values:
        s = seed.f32_text(x)
        assert struct.unpack("<f", struct.pack("<f", float(s)))[0] == x


def test_pack_round_trips():
    v = [struct.unpack("<f", struct.pack("<f", x / 7))[0] for x in range(-5, 6)]
    assert list(seed.unpack(seed.pack(v))) == v


def test_committed_embeddings_cover_inputs_and_are_unit_vectors():
    vecs = seed.vectors()  # 조항·템플릿과 어긋나면 ValueError
    assert set(vecs["clauses"]) == {c.clause_id for c in seed.load_chunks()}
    assert set(vecs["templates"]) == {t["key"] for t in seed.load_templates()}
    for kind in vecs.values():
        for key, v in kind.items():
            assert len(v) == DIM, key
            assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-6, key


def test_stale_embeddings_are_rejected(monkeypatch):
    original = seed.load_templates()
    monkeypatch.setattr(seed, "load_templates", lambda: [*original[:-1], {**original[-1], "text": original[-1]["text"] + " 바뀜"}])
    with pytest.raises(ValueError, match="seed embed"):
        seed.vectors()


def test_template_keys():
    keys = [t["key"] for t in seed.load_templates()]
    assert len(keys) == len(set(keys))
    assert all(seed.TEMPLATE_KEY_RE.match(k) for k in keys)
    assert {f"R{i}" for i in range(1, 6)} <= {k.split(".")[0] for k in keys}


def test_render_is_stable_and_has_every_row():
    text = seed.render()
    assert text == seed.render()
    assert text.count("'산업안전보건기준규칙 제") == len(seed.load_chunks())
    assert "ON CONFLICT (clause_id) DO UPDATE" in text and "RAISE EXCEPTION" in text


def test_committed_seed_is_up_to_date():
    if not SEED_SQL.exists():
        pytest.skip("server/ 가 없는 설치(ai 만 설치한 환경)")
    assert seed.main(["sql", "--check", str(SEED_SQL)]) == 0, "seed sql --write 로 R__rag_clauses.sql 을 다시 만든다"
