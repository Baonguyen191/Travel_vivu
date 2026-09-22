from pipeline.load.upsert import upsert_places
from pipeline.models import PlaceRecord


def _place(**kw):
    base = dict(name="Chùa Thiên Mụ", external_ids={"wikidata": "Q1"},
                lat=16.4539, lon=107.5453, category="chua",
                indoor_ratio=0.5, avg_visit_minutes=45,
                weather_sensitivity={"rain": 0.6, "heat": 0.5, "wind": 0.2},
                best_time_of_day=["sang_som"], unsafe_conditions=["mua_lon"])
    base.update(kw)
    return PlaceRecord(**base)


def test_upsert_inserts_then_updates(db_conn):
    inserted, updated = upsert_places(db_conn, [_place()])
    assert (inserted, updated) == (1, 0)

    inserted, updated = upsert_places(db_conn, [_place(avg_visit_minutes=60)])
    assert (inserted, updated) == (0, 1)

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*), max(avg_visit_minutes) FROM places")
        assert cur.fetchone() == (1, 60)


def test_upsert_links_all_external_ids(db_conn):
    upsert_places(db_conn, [_place(external_ids={"wikidata": "Q1", "osm": "way/9"})])
    with db_conn.cursor() as cur:
        cur.execute("SELECT source, external_id FROM place_external_ids ORDER BY source")
        assert cur.fetchall() == [("osm", "way/9"), ("wikidata", "Q1")]


def test_upsert_matches_existing_place_via_any_external_id(db_conn):
    upsert_places(db_conn, [_place(external_ids={"osm": "way/9"})])
    upsert_places(db_conn, [_place(external_ids={"wikidata": "Q1", "osm": "way/9"})])
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM places")
        assert cur.fetchone()[0] == 1


def test_upsert_writes_geography_point(db_conn):
    upsert_places(db_conn, [_place()])
    with db_conn.cursor() as cur:
        cur.execute("SELECT ST_Y(location::geometry), ST_X(location::geometry) FROM places")
        lat, lon = cur.fetchone()
        assert round(lat, 4) == 16.4539
        assert round(lon, 4) == 107.5453
