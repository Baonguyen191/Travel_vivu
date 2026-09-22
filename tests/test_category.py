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


def test_rule_requires_all_keys_to_match():
    rules = {
        "wikidata": {},
        "osm": [
            {
                "match": {"amenity": "restaurant", "cuisine": "vietnamese"},
                "category": "nha_hang_viet",
            },
        ],
    }
    assert (
        map_category({"amenity": "restaurant", "cuisine": "vietnamese"}, [], rules)
        == "nha_hang_viet"
    )
    # Chỉ khớp một trong hai key thì không đủ (AND, không phải OR).
    assert map_category({"amenity": "restaurant", "cuisine": "italian"}, [], rules) == "khac"
    assert map_category({"amenity": "restaurant"}, [], rules) == "khac"
    assert map_category({"cuisine": "vietnamese"}, [], rules) == "khac"


def test_config_file_has_required_categories():
    rules = load_category_rules("config/categories.yml")
    categories = {r["category"] for r in rules["osm"]} | set(rules["wikidata"].values())
    assert {
        "di_tich",
        "chua",
        "bao_tang",
        "nha_hang",
        "quan_ca_phe",
        "khach_san",
        "don_vi_hanh_chinh",
    } <= categories


def test_config_file_wildcard_rules_are_not_shadowed_by_later_rules():
    """Với mỗi key được dùng làm wildcard ("*"), không rule nào sau đó trong file
    còn dùng cùng key đó — nếu có, rule sau sẽ không bao giờ được chạm tới vì
    wildcard đã khớp trước. Viết như một tính chất chung trên toàn bộ file, không
    hardcode tên key cụ thể, để vẫn đúng khi có thêm rule mới."""
    rules = load_category_rules("config/categories.yml")
    osm_rules = rules["osm"]
    for i, rule in enumerate(osm_rules):
        for key, expected in rule["match"].items():
            if expected != "*":
                continue
            for later_rule in osm_rules[i + 1 :]:
                assert key not in later_rule["match"], (
                    f"rule #{i} dùng wildcard cho key '{key}', nhưng rule sau đó "
                    f"({later_rule}) cũng match trên key '{key}' — sẽ không bao giờ "
                    "được chạm tới"
                )
