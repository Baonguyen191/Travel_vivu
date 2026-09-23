import functools
import json

from pipeline.config import CityConfig
from pipeline.http import Fetcher
from pipeline.ingest import osm, overpass
from pipeline.ingest.osm import parse_elements


def test_parse_elements_reads_node_and_way():
    elements = [
        {"type": "node", "id": 1, "lat": 16.47, "lon": 107.58,
         "tags": {"name": "Quán bún bò", "amenity": "restaurant",
                  "opening_hours": "Mo-Su 06:00-10:00", "website": "http://x.vn"}},
        {"type": "way", "id": 2, "center": {"lat": 16.46, "lon": 107.57},
         "tags": {"name": "Đại Nội", "historic": "castle"}},
    ]
    a, b = parse_elements(elements)
    assert a.external_ids == {"osm": "node/1"}
    assert a.opening_hours_raw == "Mo-Su 06:00-10:00"
    assert a.website == "http://x.vn"
    assert b.external_ids == {"osm": "way/2"}
    assert (b.lat, b.lon) == (16.46, 107.57)


def test_parse_elements_skips_unnamed_and_positionless():
    elements = [
        {"type": "node", "id": 3, "lat": 16.4, "lon": 107.5,
         "tags": {"amenity": "restaurant"}},
        {"type": "way", "id": 4, "tags": {"name": "Không tọa độ"}},
    ]
    assert parse_elements(elements) == []


def test_run_falls_back_to_next_endpoint_on_504(
    httpx_mock, monkeypatch, tmp_path, db_conn
):
    # Isole tất cả I/O của test khỏi dữ liệu thật: raw_root riêng (không phải
    # "data/raw" thật) và một bbox không trùng config/city_hue.yml, để nếu
    # dòng raw_documents còn sót lại trong DB dùng chung, nó không bao giờ
    # trùng cache key với truy vấn Overpass thật của pipeline.
    monkeypatch.setattr(overpass.time, "sleep", lambda *_: None)
    monkeypatch.setattr(osm, "STAGED_PATH", str(tmp_path / "osm.json"))
    monkeypatch.setattr(
        osm,
        "Fetcher",
        functools.partial(Fetcher, raw_root=str(tmp_path / "raw"), user_agent="test-ua"),
    )

    httpx_mock.add_response(
        url=osm.OVERPASS_ENDPOINTS[0], method="POST", status_code=504,
    )
    body = json.dumps(
        {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 16.47,
                    "lon": 107.58,
                    "tags": {"name": "Quán bún bò", "amenity": "restaurant"},
                }
            ]
        }
    ).encode("utf-8")
    httpx_mock.add_response(
        url=osm.OVERPASS_ENDPOINTS[1], method="POST", content=body,
    )

    cfg = CityConfig(
        name="Test City - không dùng cho pipeline thật",
        bbox=(1.0, 2.0, 3.0, 4.0),
        core_center=(2.0, 3.0),
        core_radius_km=1.0,
    )
    count = osm.run(db_conn, cfg)

    assert count == 1
    requested_urls = [str(r.url) for r in httpx_mock.get_requests()]
    assert requested_urls == [osm.OVERPASS_ENDPOINTS[0], osm.OVERPASS_ENDPOINTS[1]]

    staged = json.loads((tmp_path / "osm.json").read_text(encoding="utf-8"))
    assert staged[0]["name"] == "Quán bún bò"
