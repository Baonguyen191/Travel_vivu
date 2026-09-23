from pipeline.cli import INGEST_MODULES, PIPELINE_ORDER


def test_pipeline_order_loads_places_before_text_and_images():
    assert PIPELINE_ORDER.index("load") < PIPELINE_ORDER.index("wikipedia")
    assert PIPELINE_ORDER.index("load") < PIPELINE_ORDER.index("commons")


def test_pipeline_order_ingests_sources_before_normalize():
    for source in ("boundary", "wikidata", "osm"):
        assert PIPELINE_ORDER.index(source) < PIPELINE_ORDER.index("normalize")


def test_every_ingest_step_has_a_module():
    steps = [s for s in PIPELINE_ORDER if s not in {"migrate", "normalize", "load", "qa"}]
    assert set(steps) <= set(INGEST_MODULES)
