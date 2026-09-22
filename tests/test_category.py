from pipeline.normalize.category import load_category_rules, map_category

RULES = {
    "wikidata": {"Q16970": "chua", "Q24398318": "chua", "Q33506": "bao_tang"},
    "osm": [
        {"match": {"amenity": "restaurant"}, "category": "nha_hang"},
        {"match": {"amenity": "cafe"}, "category": "quan_ca_phe"},
        {"match": {"tourism": "museum"}, "category": "bao_tang"},
        {"match": {"historic": "castle"}, "category": "di_tich"},
        {"match": {"historic": "*"}, "category": "di_tich"},
        {"match": {"leisure": "park"}, "category": "cong_vien"},
    ],
}


def test_wikidata_class_wins_over_osm_tag():
    assert map_category({"amenity": "restaurant"}, ["Q16970"], RULES) == "chua"


def test_osm_exact_tag():
    assert map_category({"amenity": "cafe"}, [], RULES) == "quan_ca_phe"


def test_osm_wildcard_tag():
    assert map_category({"historic": "memorial"}, [], RULES) == "di_tich"


def test_unknown_falls_back_to_khac():
    assert map_category({"shop": "bakery"}, ["Q999999"], RULES) == "khac"


def test_config_file_has_required_categories():
    rules = load_category_rules("config/categories.yml")
    categories = {r["category"] for r in rules["osm"]} | set(rules["wikidata"].values())
    assert {"di_tich", "chua", "bao_tang", "nha_hang", "quan_ca_phe"} <= categories
