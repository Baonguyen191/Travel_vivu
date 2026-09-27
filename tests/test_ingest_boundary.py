import functools
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from pipeline.config import CityConfig
from pipeline.http import Fetcher
from pipeline.ingest import boundary, overpass
from pipeline.ingest.boundary import rings_to_wkt


def _outer(points_lonlat):
    """Xây member 'outer' từ danh sách điểm (lon, lat)."""
    return {"role": "outer", "geometry": [{"lat": lat, "lon": lon} for lon, lat in points_lonlat]}


def test_rings_to_wkt_builds_closed_polygon():
    elements = [
        {
            "type": "relation",
            "tags": {"admin_level": "4", "name": "Thành phố Huế"},
            "members": [
                {"role": "outer", "geometry": [
                    {"lat": 16.4, "lon": 107.5},
                    {"lat": 16.5, "lon": 107.5},
                    {"lat": 16.5, "lon": 107.6},
                ]}
            ],
        }
    ]
    wkt = rings_to_wkt(elements)
    assert wkt.startswith("POLYGON((")
    assert wkt.count("107.5 16.4") == 2  # điểm đầu lặp lại ở cuối để khép vòng


def test_rings_to_wkt_prefers_smallest_admin_unit():
    elements = [
        {"type": "relation", "tags": {"admin_level": "4"},
         "members": [{"role": "outer", "geometry": [
             {"lat": 0, "lon": 0}, {"lat": 9, "lon": 0}, {"lat": 9, "lon": 9}]}]},
        {"type": "relation", "tags": {"admin_level": "6"},
         "members": [{"role": "outer", "geometry": [
             {"lat": 1, "lon": 1}, {"lat": 2, "lon": 1}, {"lat": 2, "lon": 2}]}]},
    ]
    assert "1 1" in rings_to_wkt(elements)
    assert "9 9" not in rings_to_wkt(elements)


def test_rings_to_wkt_chains_ways_supplied_out_of_order():
    # 3 cạnh của một tam giác, các way được liệt kê không theo thứ tự đường đi:
    # C nối vào cuối A, A là điểm bắt đầu, B nối vào cuối A-C.
    way_a = _outer([(0, 0), (4, 0)])
    way_b = _outer([(4, 0), (4, 4)])
    way_c = _outer([(4, 4), (0, 0)])
    elements = [
        {"type": "relation", "tags": {"admin_level": "4"},
         "members": [way_c, way_a, way_b]},
    ]
    assert rings_to_wkt(elements) == "POLYGON((4 4, 0 0, 4 0, 4 4))"


def test_rings_to_wkt_flips_way_stored_in_reverse_direction():
    # way_b đại diện đoạn (5,0)->(5,5) nhưng được lưu ngược: (5,5)->(5,0).
    way_a = _outer([(0, 0), (5, 0)])
    way_b = _outer([(5, 5), (5, 0)])
    elements = [
        {"type": "relation", "tags": {"admin_level": "4"},
         "members": [way_a, way_b]},
    ]
    assert rings_to_wkt(elements) == "POLYGON((0 0, 5 0, 5 5, 0 0))"


def test_rings_to_wkt_raises_when_a_way_does_not_connect():
    way_a = _outer([(0, 0), (1, 0)])
    way_b = _outer([(5, 5), (6, 6)])  # không chung điểm đầu/cuối với way_a
    elements = [
        {"type": "relation", "tags": {"admin_level": "4"},
         "members": [way_a, way_b]},
    ]
    with pytest.raises(ValueError, match="1"):
        rings_to_wkt(elements)


def test_run_falls_back_to_second_mirror_on_504(httpx_mock, monkeypatch, db_conn):
    # Trước fix này, boundary.py gọi thẳng OVERPASS_URL (hard-code, không
    # retry): một 504 duy nhất ném httpx.HTTPStatusError ra ngoài, làm chết
    # cả `all` ở bước 2 (boundary chạy trước wikidata/osm). Test này không
    # chạm mạng thật lẫn cache thật: raw_root riêng và ngắn (pytest tmp_path
    # lồng quá sâu, vượt giới hạn 260 ký tự đường dẫn của Windows — dùng
    # tempfile.mkdtemp() như các test ingest khác), mock đúng hai request
    # theo thứ tự mirror.
    raw_root = tempfile.mkdtemp(prefix="boundary_raw_")
    generated_wkt = Path(raw_root) / "city_hue_boundary.wkt"
    monkeypatch.setattr(overpass.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        boundary,
        "Fetcher",
        functools.partial(Fetcher, raw_root=raw_root, user_agent="test-ua"),
    )
    monkeypatch.setattr(boundary, "GENERATED_WKT_PATH", str(generated_wkt))
    # run() ghi cứng "config/city_hue.yml" — thay save_city bằng một hàm ghi
    # nhận để test không đè lên file config thật của repo.
    saved_calls = []
    monkeypatch.setattr(
        boundary, "save_city", lambda cfg_out, path: saved_calls.append((cfg_out, path))
    )

    httpx_mock.add_response(
        url=overpass.OVERPASS_ENDPOINTS[0], method="POST", status_code=504,
    )
    body = json.dumps({"elements": [
        {"type": "relation", "tags": {"admin_level": "4", "name": "Thành phố Huế"},
         "members": [{"role": "outer", "geometry": [
             {"lat": 16.4, "lon": 107.5}, {"lat": 16.5, "lon": 107.5},
             {"lat": 16.5, "lon": 107.6},
         ]}]},
    ]}).encode("utf-8")
    httpx_mock.add_response(
        url=overpass.OVERPASS_ENDPOINTS[1], method="POST", content=body,
    )

    cfg = CityConfig(
        name="Test City - không dùng cho pipeline thật",
        bbox=(1.0, 2.0, 3.0, 4.0),
        core_center=(2.0, 3.0),
        core_radius_km=1.0,
    )
    try:
        count = boundary.run(db_conn, cfg, force=False)
    finally:
        shutil.rmtree(raw_root, ignore_errors=True)

    assert count == 1
    assert saved_calls  # save_city được gọi đúng một lần sau khi ghép ranh giới
    requested_urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert requested_urls == [overpass.OVERPASS_ENDPOINTS[0], overpass.OVERPASS_ENDPOINTS[1]]
