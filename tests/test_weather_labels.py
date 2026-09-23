from pipeline.models import PlaceRecord
from pipeline.normalize.weather_labels import (
    apply_labels, load_overrides, load_weather_defaults, warn_unmatched_overrides,
)

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


def test_layer2_building_tag_raises_indoor_ratio_when_category_already_indoor():
    place = PlaceRecord("Bảo tàng", {"osm": "node/3"}, 16.4, 107.5,
                        category="bao_tang", tags={"building": "yes"})
    assert apply_labels(place, DEFAULTS, {}).indoor_ratio == 0.9


def test_layer2_building_tag_skipped_for_outdoor_category():
    """building=yes trên OSM chỉ nghĩa là 'có công trình xây', không nghĩa là
    'trải nghiệm chủ yếu trong nhà'. Ngọ Môn, lăng tẩm... đều có building=yes
    trên một phần công trình nhưng vẫn là điểm tham quan ngoài trời — nếu
    layer 2 lật indoor_ratio lên 0.9 trong khi weather_sensitivity.rain vẫn
    là 0.8 (theo category lang_tam), hai trường mâu thuẫn nhau."""
    place = PlaceRecord("Lăng", {"osm": "node/3"}, 16.4, 107.5,
                        category="lang_tam", tags={"building": "yes"})
    out = apply_labels(place, DEFAULTS, {})
    assert out.indoor_ratio == 0.25
    assert out.weather_sensitivity["rain"] == 0.8


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


def test_khach_san_and_don_vi_hanh_chinh_have_no_avg_visit_minutes():
    """Không phải điểm dừng có lịch trình thật -> avg_visit_minutes phải là None
    (không phải 0), để bộ tối ưu lịch trình không coi đây là điểm dừng miễn phí
    thời gian."""
    defaults = load_weather_defaults("config/weather_defaults.yml")
    hotel = PlaceRecord("Khách sạn X", {"osm": "node/10"}, 16.4, 107.5, category="khach_san")
    ward = PlaceRecord("Phường X", {"osm": "node/11"}, 16.4, 107.5, category="don_vi_hanh_chinh")
    assert apply_labels(hotel, defaults, {}).avg_visit_minutes is None
    assert apply_labels(ward, defaults, {}).avg_visit_minutes is None


def test_khac_category_has_no_avg_visit_minutes():
    """'khac' gom cả bản ghi không phải điểm dừng thật: triều đại, sự kiện,
    ngai vàng, một vùng địa lý, ga xe lửa... avg_visit_minutes = 45 cho
    nhóm này khiến bộ lập lịch coi chúng như điểm dừng schedule được. Cùng
    tín hiệu None đã dùng cho khach_san/don_vi_hanh_chinh."""
    defaults = load_weather_defaults("config/weather_defaults.yml")
    place = PlaceRecord("Sự kiện gì đó", {"wikidata": "Q1"}, 16.4, 107.5, category="khac")
    assert apply_labels(place, defaults, {}).avg_visit_minutes is None


def test_layer3_override_matches_second_external_id():
    """place_key có thể khớp theo bất kỳ external id nào của bản ghi đã ghép,
    không chỉ id đầu tiên trong dict."""
    place = PlaceRecord("Lăng Tự Đức", {"osm": "node/77", "wikidata": "Q1"}, 16.4, 107.5,
                        category="lang_tam", tags={"building": "yes"})
    overrides = {"wikidata:Q1": {"indoor_ratio": 0.15}}
    out = apply_labels(place, DEFAULTS, overrides)
    assert out.indoor_ratio == 0.15
    assert out.label_source == "manual"


def test_layer3_override_list_values_are_not_shared_between_records():
    """Hai bản ghi cùng khớp một dòng override không được giữ chung list object —
    sửa list ở bản ghi này không được ảnh hưởng bản ghi kia."""
    place1 = PlaceRecord("A", {"wikidata": "Q1"}, 16.4, 107.5, category="lang_tam")
    place2 = PlaceRecord("B", {"wikidata": "Q1"}, 16.4, 107.5, category="lang_tam")
    overrides = {"wikidata:Q1": {"unsafe_conditions": ["mua_lon"]}}
    out1 = apply_labels(place1, DEFAULTS, overrides)
    out2 = apply_labels(place2, DEFAULTS, overrides)
    out1.unsafe_conditions.append("bao")
    assert out2.unsafe_conditions == ["mua_lon"]


def test_load_overrides_reads_real_csv(tmp_path):
    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text(
        "place_key,indoor_ratio,avg_visit_minutes,best_time_of_day,unsafe_conditions,"
        "ticket_price_vnd,dress_code,note\n"
        "wikidata:Q1,0.2,180,sang_som|chieu_muon,mua_lon|bao,200000,,ghi chu\n",
        encoding="utf-8",
    )
    result = load_overrides(str(csv_path))
    assert result == {
        "wikidata:Q1": {
            "indoor_ratio": 0.2,
            "avg_visit_minutes": 180,
            "best_time_of_day": ["sang_som", "chieu_muon"],
            "unsafe_conditions": ["mua_lon", "bao"],
            "ticket_price": {"vnd": 200000},
        }
    }


def test_load_overrides_empty_cell_means_no_override(tmp_path):
    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text(
        "place_key,indoor_ratio,avg_visit_minutes,best_time_of_day,unsafe_conditions,"
        "ticket_price_vnd,dress_code,note\n"
        "wikidata:Q2,0.3,,,,,,\n",
        encoding="utf-8",
    )
    result = load_overrides(str(csv_path))
    assert result == {"wikidata:Q2": {"indoor_ratio": 0.3}}


def test_load_overrides_skips_malformed_value_but_keeps_rest_of_row(tmp_path, capsys):
    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text(
        "place_key,indoor_ratio,avg_visit_minutes,best_time_of_day,unsafe_conditions,"
        "ticket_price_vnd,dress_code,note\n"
        "wikidata:Q3,khong_phai_so,90,,,,,\n",
        encoding="utf-8",
    )
    result = load_overrides(str(csv_path))
    assert result == {"wikidata:Q3": {"avg_visit_minutes": 90}}
    warning = capsys.readouterr().err
    assert "wikidata:Q3" in warning
    assert "indoor_ratio" in warning
    assert "khong_phai_so" in warning


def test_apply_labels_records_matched_override_key():
    place = PlaceRecord("Lăng Tự Đức", {"wikidata": "Q1"}, 16.4, 107.5, category="lang_tam")
    overrides = {"wikidata:Q1": {"indoor_ratio": 0.15}}
    matched: set[str] = set()
    apply_labels(place, DEFAULTS, overrides, matched)
    assert matched == {"wikidata:Q1"}


def test_apply_labels_does_not_record_when_no_override_matches():
    place = PlaceRecord("X", {"wikidata": "Q99"}, 16.4, 107.5, category="lang_tam")
    overrides = {"wikidata:Q1": {"indoor_ratio": 0.15}}
    matched: set[str] = set()
    apply_labels(place, DEFAULTS, overrides, matched)
    assert matched == set()


def test_warn_unmatched_overrides_reports_key_that_matched_nothing(capsys):
    """Một QID gõ sai trong overrides.csv (ví dụ đã bị đổi/không tồn tại)
    khiến tầng 3 im lặng không bao giờ chạy — đây là lỗi thật đã xảy ra với
    dòng mẫu Q1140380. Cảnh báo phải nêu rõ khoá nào không khớp."""
    overrides = {"wikidata:Q1": {}, "wikidata:Q999999": {}}
    unmatched = warn_unmatched_overrides(overrides, {"wikidata:Q1"})
    assert unmatched == ["wikidata:Q999999"]
    assert "wikidata:Q999999" in capsys.readouterr().out


def test_warn_unmatched_overrides_silent_when_all_matched(capsys):
    unmatched = warn_unmatched_overrides({"wikidata:Q1": {}}, {"wikidata:Q1"})
    assert unmatched == []
    assert capsys.readouterr().out == ""


def test_load_overrides_skips_row_with_no_usable_fields(tmp_path, capsys):
    csv_path = tmp_path / "overrides.csv"
    csv_path.write_text(
        "place_key,indoor_ratio,avg_visit_minutes,best_time_of_day,unsafe_conditions,"
        "ticket_price_vnd,dress_code,note\n"
        "wikidata:Q4,khong_phai_so,,,,,,\n",
        encoding="utf-8",
    )
    result = load_overrides(str(csv_path))
    assert "wikidata:Q4" not in result
    warning = capsys.readouterr().err
    assert "wikidata:Q4" in warning
