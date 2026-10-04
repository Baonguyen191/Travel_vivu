from pipeline.models import PlaceRecord
from pipeline.normalize.place_facts import apply_place_facts, load_place_facts, parse_ticket

HEADER = "place_key,name,opening_hours,ticket_price_vnd,source_url,checked_on,note\n"


def test_parse_ticket_single_and_range():
    assert parse_ticket("150000") == {"vnd": 150000}
    assert parse_ticket("0-50.000") == {"vnd": 0, "vnd_max": 50000}


def test_load_place_facts_parses_hours_price_and_sources(tmp_path, capsys):
    path = tmp_path / "facts.csv"
    path.write_text(HEADER
                    + "wikidata:Q1,A,Mo-Su 06:30-17:30,0-50000,https://a https://b,2026-10-03,\n"
                    + "wikidata:Q2,B,,0,https://c,2026-10-03,chưa có giờ\n"
                    + "wikidata:Q3,C,mở cả ngày,abc,https://d,2026-10-03,\n", encoding="utf-8")
    facts = load_place_facts(str(path))

    a = facts["wikidata:Q1"]
    assert a["opening_hours_raw"] == "Mo-Su 06:30-17:30" and a["opening_hours"]["sun"] == [["06:30", "17:30"]]
    assert a["ticket_price"] == {"vnd": 0, "vnd_max": 50000, "source_url": ["https://a", "https://b"],
                                 "checked_on": "2026-10-03"}
    assert "opening_hours" not in facts["wikidata:Q2"]  # ô trống: giữ giá trị cũ, không đoán
    assert "wikidata:Q3" not in facts  # cả giờ lẫn giá đều không đọc được
    err = capsys.readouterr().err
    assert "mở cả ngày" in err and "abc" in err


def test_apply_place_facts_overrides_only_given_fields(tmp_path):
    path = tmp_path / "facts.csv"
    path.write_text(HEADER + "wikidata:Q1,A,,0,https://x,2026-10-03,\n", encoding="utf-8")
    place = PlaceRecord(name="A", external_ids={"wikidata": "Q1"}, lat=16.4, lon=107.5,
                        opening_hours_raw="Mo-Su 07:00-17:00")
    matched: set = set()
    out = apply_place_facts(place, load_place_facts(str(path)), matched)
    assert out.ticket_price["vnd"] == 0 and out.opening_hours_raw == "Mo-Su 07:00-17:00"
    assert matched == {"wikidata:Q1"}


def test_repo_place_facts_file_is_valid(capsys):
    facts = load_place_facts()  # config/place_facts.csv thật: mọi dòng phải đọc được, có nguồn
    assert len(facts) >= 10
    assert "Cảnh báo" not in capsys.readouterr().err
