from pipeline.ingest.boundary import rings_to_wkt


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
