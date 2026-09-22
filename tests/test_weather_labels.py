from pipeline.models import PlaceRecord
from pipeline.normalize.weather_labels import apply_labels, load_weather_defaults

DEFAULTS = {
    "bao_tang": {"indoor_ratio": 0.95, "avg_visit_minutes": 60,
                 "weather_sensitivity": {"rain": 0.1, "heat": 0.1, "wind": 0.0},
                 "best_time_of_day": ["bat_ky"], "unsafe_conditions": []},
    "lang_tam": {"indoor_ratio": 0.25, "avg_visit_minutes": 75,
                 "weather_sensitivity": {"rain": 0.8, "heat": 0.7, "wind": 0.2},
                 "best_time_of_day": ["sang_som"], "unsafe_conditions": ["mua_lon"]},
    "khac": {"indoor_ratio": 0.5, "avg_visit_minutes": 45,
             "weather_sensitivity": {"rain": 0.5, "heat": 0.5, "wind": 0.2},
             "best_time_of_day": ["bat_ky"], "unsafe_conditions": []},
}


def test_layer1_applies_category_defaults():
    place = PlaceRecord("Bảo tàng", {"osm": "node/1"}, 16.4, 107.5, category="bao_tang")
    out = apply_labels(place, DEFAULTS, {})
    assert out.indoor_ratio == 0.95
    assert out.avg_visit_minutes == 60
    assert out.unsafe_conditions == []
    assert out.label_source == "default"


def test_layer1_unknown_category_uses_khac():
    place = PlaceRecord("X", {"osm": "node/2"}, 16.4, 107.5, category="khong_co_that")
    assert apply_labels(place, DEFAULTS, {}).indoor_ratio == 0.5


def test_layer2_building_tag_raises_indoor_ratio():
    place = PlaceRecord("Lăng", {"osm": "node/3"}, 16.4, 107.5,
                        category="lang_tam", tags={"building": "yes"})
    assert apply_labels(place, DEFAULTS, {}).indoor_ratio == 0.9


def test_layer2_park_tag_lowers_indoor_ratio():
    place = PlaceRecord("Công viên", {"osm": "node/4"}, 16.4, 107.5,
                        category="bao_tang", tags={"leisure": "park"})
    assert apply_labels(place, DEFAULTS, {}).indoor_ratio == 0.05


def test_layer2_does_not_touch_unsafe_conditions():
    place = PlaceRecord("Lăng", {"osm": "node/5"}, 16.4, 107.5,
                        category="lang_tam", tags={"building": "yes"})
    assert apply_labels(place, DEFAULTS, {}).unsafe_conditions == ["mua_lon"]


def test_layer3_override_wins_and_marks_manual():
    place = PlaceRecord("Lăng Tự Đức", {"wikidata": "Q1"}, 16.4, 107.5,
                        category="lang_tam", tags={"building": "yes"})
    overrides = {"wikidata:Q1": {"indoor_ratio": 0.15, "avg_visit_minutes": 90,
                                 "unsafe_conditions": ["mua_lon", "bao"]}}
    out = apply_labels(place, DEFAULTS, overrides)
    assert out.indoor_ratio == 0.15
    assert out.avg_visit_minutes == 90
    assert out.unsafe_conditions == ["mua_lon", "bao"]
    assert out.label_source == "manual"


def test_defaults_file_covers_every_category_in_categories_yml():
    from pipeline.normalize.category import load_category_rules

    defaults = load_weather_defaults("config/weather_defaults.yml")
    rules = load_category_rules("config/categories.yml")
    used = {r["category"] for r in rules["osm"]} | set(rules["wikidata"].values())
    assert used | {"khac"} <= set(defaults)
