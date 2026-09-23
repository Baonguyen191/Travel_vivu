import sys

from pipeline import cli, db
from pipeline.cli import INGEST_MODULES, PIPELINE_ORDER


def test_pipeline_order_loads_places_before_text_and_images():
    assert PIPELINE_ORDER.index("load") < PIPELINE_ORDER.index("wikipedia")
    assert PIPELINE_ORDER.index("load") < PIPELINE_ORDER.index("commons")


def test_pipeline_order_ingests_sources_before_load():
    for source in ("boundary", "wikidata", "osm"):
        assert PIPELINE_ORDER.index(source) < PIPELINE_ORDER.index("load")


def test_every_ingest_step_has_a_module():
    steps = [s for s in PIPELINE_ORDER if s not in {"migrate", "load", "qa"}]
    assert set(steps) <= set(INGEST_MODULES)


def test_pipeline_order_has_no_separate_normalize_step():
    # `load` đã tự chạy normalize rồi upsert; đưa cả hai vào `all` sẽ chạy
    # normalize hai lần. `normalize` vẫn là lệnh đứng riêng, chỉ không nằm
    # trong PIPELINE_ORDER.
    assert "normalize" not in PIPELINE_ORDER


def test_unknown_ingest_source_exits_without_connecting_to_db(monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise AssertionError("không được kết nối DB khi nguồn không hợp lệ")

    monkeypatch.setattr(db, "connect", _boom)
    monkeypatch.setattr(sys, "argv", ["pipeline", "ingest", "--source", "bogus"])

    assert cli.main() == 1

    out = capsys.readouterr().out
    assert "Nguồn không hợp lệ: 'bogus'" in out
