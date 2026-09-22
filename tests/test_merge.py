from pipeline.config import CityConfig
from pipeline.models import PlaceRecord
from pipeline.normalize.merge import (
    haversine_m, merge_places, normalize_name, within_core,
)

CFG = CityConfig("Huế", (16.335, 107.435, 16.605, 107.725), (16.4698, 107.5796), 15.0)


def test_normalize_name_strips_accents_and_prefix():
    assert normalize_name("Chùa Thiên Mụ") == "thien mu"
    assert normalize_name("Lăng Tự Đức") == "tu duc"
    assert normalize_name("Đại Nội") == "dai noi"


def test_normalize_name_handles_d_with_stroke():
    assert "đ" not in normalize_name("Đền Huyền Trân")
    assert normalize_name("Đền Huyền Trân") == "huyen tran"


def test_haversine_known_distance():
    d = haversine_m(16.4698, 107.5796, 16.4698, 107.5896)
    assert 1000 < d < 1120


def test_within_core_rejects_far_place():
    near = PlaceRecord("A", {"osm": "node/1"}, 16.47, 107.58)
    far = PlaceRecord("B", {"osm": "node/2"}, 16.20, 107.20)
    assert within_core(near, CFG) is True
    assert within_core(far, CFG) is False


def test_merge_combines_matching_pair():
    wd = PlaceRecord("Chùa Thiên Mụ", {"wikidata": "Q1"}, 16.4539, 107.5453,
                     wikidata_classes=["Q16970"])
    osm = PlaceRecord("Chùa Thiên Mụ", {"osm": "way/9"}, 16.4540, 107.5454,
                      opening_hours_raw="Mo-Su 07:00-17:00", website="http://x.vn")
    merged, review = merge_places([wd], [osm])
    assert review == []
    [place] = merged
    assert place.external_ids == {"wikidata": "Q1", "osm": "way/9"}
    assert place.opening_hours_raw == "Mo-Su 07:00-17:00"
    assert place.website == "http://x.vn"
    assert place.wikidata_classes == ["Q16970"]


def test_close_but_different_name_produces_no_review():
    wd = PlaceRecord("Chùa Thiên Mụ", {"wikidata": "Q1"}, 16.4539, 107.5453)
    osm = PlaceRecord("Quán cà phê Thiên Mụ View", {"osm": "node/9"}, 16.4540, 107.5454)
    merged, review = merge_places([wd], [osm])
    assert len(merged) == 2
    assert review == []


def test_same_name_far_apart_goes_to_review():
    wd = PlaceRecord("Chợ Đông Ba", {"wikidata": "Q1"}, 16.4700, 107.5800)
    osm = PlaceRecord("Chợ Đông Ba", {"osm": "node/9"}, 16.5200, 107.6200)
    merged, review = merge_places([wd], [osm])
    assert len(merged) == 2
    assert review[0]["reason"] == "trung_ten_nhung_xa"


def test_merge_picks_nearest_same_name_candidate_regardless_of_input_order():
    wd = PlaceRecord("Lăng Tự Đức", {"wikidata": "Q1"}, 16.4325, 107.5660)
    close = PlaceRecord("Lăng Tự Đức", {"osm": "relation/close"}, 16.4329409, 107.5655529)
    far = PlaceRecord("Lăng Tự Đức", {"osm": "node/far"}, 16.4331813, 107.5645857)

    for osm_records in ([close, far], [far, close]):
        merged, review = merge_places([wd], osm_records)
        [wikidata_place] = [p for p in merged if "wikidata" in p.external_ids]
        assert wikidata_place.external_ids["osm"] == "relation/close"
        assert len(review) == 1
        assert review[0]["osm_id"] == "node/far"
        assert review[0]["reason"] == "trung_ten_nhung_xa"


def test_merge_with_three_same_name_candidates_merges_nearest_reviews_rest():
    wd = PlaceRecord("Lăng Khải Định", {"wikidata": "Q1"}, 16.4325, 107.5660)
    near = PlaceRecord("Lăng Khải Định", {"osm": "node/near"}, 16.4326, 107.5661)
    mid = PlaceRecord("Lăng Khải Định", {"osm": "node/mid"}, 16.4330, 107.5670)
    far = PlaceRecord("Lăng Khải Định", {"osm": "node/far"}, 16.4400, 107.5800)

    merged, review = merge_places([wd], [near, mid, far])
    [wikidata_place] = [p for p in merged if "wikidata" in p.external_ids]
    assert wikidata_place.external_ids["osm"] == "node/near"
    assert {row["osm_id"] for row in review} == {"node/mid", "node/far"}
    assert all(row["reason"] == "trung_ten_nhung_xa" for row in review)


def test_merge_tags_union_with_osm_precedence_on_conflict():
    wd = PlaceRecord("Chùa Thiên Mụ", {"wikidata": "Q1"}, 16.4539, 107.5453,
                     tags={"vi_title": "Chùa Thiên Mụ", "wikidata": "Q1"})
    osm = PlaceRecord("Chùa Thiên Mụ", {"osm": "way/9"}, 16.4540, 107.5454,
                      tags={"wikidata": "Q1-from-osm", "historic": "temple"})
    merged, review = merge_places([wd], [osm])
    [place] = merged
    assert place.tags["vi_title"] == "Chùa Thiên Mụ"
    assert place.tags["historic"] == "temple"
    assert place.tags["wikidata"] == "Q1-from-osm"
