from pipeline.ingest.wikidata import parse_bindings


def test_parse_bindings_extracts_place():
    bindings = [
        {
            "item": {"value": "http://www.wikidata.org/entity/Q1023458"},
            "itemLabel": {"value": "Chùa Thiên Mụ"},
            "coord": {"value": "Point(107.5453 16.4539)"},
            "classes": {"value": "Q24398318|Q16970"},
            "image": {"value": "https://commons.wikimedia.org/wiki/Special:FilePath/a.jpg"},
            "commons": {"value": "Thien Mu Pagoda"},
            "viTitle": {"value": "Chùa Thiên Mụ"},
        }
    ]
    [place] = parse_bindings(bindings)
    assert place.external_ids == {"wikidata": "Q1023458"}
    assert place.name == "Chùa Thiên Mụ"
    assert (round(place.lat, 4), round(place.lon, 4)) == (16.4539, 107.5453)
    assert place.wikidata_classes == ["Q24398318", "Q16970"]
    assert place.tags["commons_category"] == "Thien Mu Pagoda"
    assert place.tags["vi_title"] == "Chùa Thiên Mụ"


def test_parse_bindings_skips_rows_without_coordinates():
    bindings = [
        {"item": {"value": "http://www.wikidata.org/entity/Q1"},
         "itemLabel": {"value": "Không tọa độ"}},
    ]
    assert parse_bindings(bindings) == []
