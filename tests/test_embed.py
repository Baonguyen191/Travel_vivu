import pytest

from pipeline.embed import (
    EMBEDDING_DIM,
    DeterministicEmbedder,
    check_dimension,
    chunk_embedding_text,
    get_embedder,
    run_embed_chunks,
    to_vector_literal,
)


def test_deterministic_embedder_dimension_and_normalization():
    embedder = DeterministicEmbedder(dim=1024)
    assert embedder.dimension == 1024

    vectors = embedder.embed_texts(["Chùa Thiên Mụ ở Huế", "Đại Nội Huế"])

    assert len(vectors) == 2
    for vec in vectors:
        assert len(vec) == 1024
        assert abs(sum(v * v for v in vec) - 1.0) < 1e-4


def test_deterministic_embedder_reproducibility():
    embedder = DeterministicEmbedder(dim=128)
    assert embedder.embed_texts(["Kinh thành Huế"]) == embedder.embed_texts(["Kinh thành Huế"])


def test_get_embedder_reads_env(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "deterministic")
    embedder = get_embedder()
    assert isinstance(embedder, DeterministicEmbedder)
    assert embedder.model_name == "deterministic"


def test_check_dimension_rejects_mismatch():
    check_dimension(DeterministicEmbedder(dim=EMBEDDING_DIM))
    with pytest.raises(ValueError, match="VECTOR"):
        check_dimension(DeterministicEmbedder(dim=768))


def test_chunk_embedding_text_prefixes_place_name():
    assert chunk_embedding_text("Lăng Tự Đức", "Lăng được xây năm 1864.") == (
        "Lăng Tự Đức: Lăng được xây năm 1864."
    )


def test_to_vector_literal_keeps_precision():
    assert to_vector_literal([0.5, -1e-7, 0.0123456789]) == "[0.5,-1e-07,0.012345679]"


def _insert_chunk(conn, name="Chùa Thiên Mụ", content="Chùa Thiên Mụ là ngôi chùa cổ.") -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO places (name, category) VALUES (%s, 'chua') RETURNING id", (name,))
        place_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO place_chunks (place_id, content, content_hash)"
            " VALUES (%s, %s, 'h1') RETURNING id",
            (place_id, content),
        )
        return cur.fetchone()[0]


class _OtherModel(DeterministicEmbedder):
    @property
    def model_name(self) -> str:
        return "other-model"


@pytest.mark.integration
def test_run_embed_chunks_writes_vector_and_model(db_conn):
    chunk_id = _insert_chunk(db_conn)
    embedder = DeterministicEmbedder()

    assert run_embed_chunks(db_conn, embedder=embedder) == 1

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT embedding IS NOT NULL, embedding_model FROM place_chunks WHERE id = %s",
            (chunk_id,),
        )
        assert cur.fetchone() == (True, "deterministic")

    # Lượt sau cùng model: không còn gì để làm; --force: làm lại tất cả.
    assert run_embed_chunks(db_conn, embedder=embedder) == 0
    assert run_embed_chunks(db_conn, embedder=embedder, force=True) == 1


@pytest.mark.integration
def test_run_embed_chunks_reembeds_when_model_changes(db_conn):
    chunk_id = _insert_chunk(db_conn)
    run_embed_chunks(db_conn, embedder=DeterministicEmbedder())

    assert run_embed_chunks(db_conn, embedder=_OtherModel()) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT embedding_model FROM place_chunks WHERE id = %s", (chunk_id,))
        assert cur.fetchone()[0] == "other-model"


@pytest.mark.integration
def test_run_embed_chunks_rejects_wrong_dimension_before_writing(db_conn):
    _insert_chunk(db_conn)

    with pytest.raises(ValueError):
        run_embed_chunks(db_conn, embedder=DeterministicEmbedder(dim=768))

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(embedding) FROM place_chunks")
        assert cur.fetchone()[0] == 0
