import pytest

from pipeline.normalize.opening_hours import parse_opening_hours

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def test_24_7():
    assert parse_opening_hours("24/7") == {d: [["00:00", "24:00"]] for d in DAYS}


def test_simple_range():
    result = parse_opening_hours("Mo-Su 07:00-17:00")
    assert result == {d: [["07:00", "17:00"]] for d in DAYS}


def test_two_intervals_in_one_day():
    result = parse_opening_hours("Mo-Fr 08:00-11:30,13:30-17:00")
    assert result["mon"] == [["08:00", "11:30"], ["13:30", "17:00"]]
    assert result["sat"] == []


def test_off_rule_overrides_earlier_rule():
    result = parse_opening_hours("Tu-Su 08:00-17:00; Mo off")
    assert result["mon"] == []
    assert result["tue"] == [["08:00", "17:00"]]


def test_day_list():
    result = parse_opening_hours("Mo,We,Fr 09:00-12:00")
    assert result["wed"] == [["09:00", "12:00"]]
    assert result["tue"] == []


def test_wraparound_day_range():
    result = parse_opening_hours("Sa-Mo 10:00-12:00")
    assert result["sat"] and result["sun"] and result["mon"]
    assert result["tue"] == []


@pytest.mark.parametrize(
    "raw",
    ["sunrise-sunset", "Apr-Oct Mo-Su 07:00-18:00", "Mo-Su 07:00-17:00; PH off",
     "week 1-53/2 Mo 10:00-12:00", "linh tinh", "", None],
)
def test_unsupported_returns_none(raw):
    assert parse_opening_hours(raw) is None


def test_closed_every_day_is_not_none():
    result = parse_opening_hours("Mo-Su off")
    assert result == {d: [] for d in DAYS}


def test_separators_only_returns_none():
    assert parse_opening_hours(";;;") is None


def test_bare_time_rule_applies_to_all_days():
    result = parse_opening_hours("06:00-23:00")
    assert result == {d: [["06:00", "23:00"]] for d in DAYS}


def test_bare_time_rule_multi_interval():
    result = parse_opening_hours("06:30-22:00,17:00-21:00")
    assert result == {d: [["06:30", "22:00"], ["17:00", "21:00"]] for d in DAYS}


def test_bare_rule_00_to_24_still_valid():
    result = parse_opening_hours("00:00-24:00")
    assert result == {d: [["00:00", "24:00"]] for d in DAYS}


def test_bare_rule_garbage_first_token_returns_none():
    assert parse_opening_hours("0600-2300") is None


@pytest.mark.parametrize("raw", ["25:00-26:00", "Mo-Su 25:00-26:00"])
def test_hour_above_24_returns_none(raw):
    assert parse_opening_hours(raw) is None


def test_multi_clause_with_dayless_clause_returns_none():
    # Ý định mơ hồ khi chuỗi có nhiều quy tắc mà một quy tắc thiếu phần ngày:
    # không rõ "13:30-17:00" áp dụng cho Tu-Su (quên dấu phẩy) hay cả tuần.
    assert parse_opening_hours("Tu-Su 07:30-11:30; 13:30-17:00") is None


def test_multi_clause_all_with_day_parts_still_parses():
    result = parse_opening_hours("Mo-Fr 08:00-17:00; Sa 08:00-12:00")
    assert result["mon"] == [["08:00", "17:00"]]
    assert result["fri"] == [["08:00", "17:00"]]
    assert result["sat"] == [["08:00", "12:00"]]
    assert result["sun"] == []


def test_wraparound_time_range_splits_across_midnight():
    # 17:00-01:30 mỗi ngày trong tuần: đoạn đầu [17:00, 24:00] ghi vào chính
    # ngày đó, đoạn còn lại [00:00, 01:30] tràn sang ngày hôm sau — mỗi
    # ngày trong Mo-Su đều là "hôm sau" của một ngày khác cũng trong Mo-Su,
    # nên mọi ngày đều có cả hai đoạn.
    result = parse_opening_hours("Mo-Su 17:00-01:30")
    for day in DAYS:
        assert ["00:00", "01:30"] in result[day]
        assert ["17:00", "24:00"] in result[day]


def test_bare_wraparound_time_range_splits_across_midnight():
    result = parse_opening_hours("16:00-04:00")
    for day in DAYS:
        assert ["00:00", "04:00"] in result[day]
        assert ["16:00", "24:00"] in result[day]


def test_end_at_midnight_is_treated_as_24_00_not_a_wraparound():
    # "...-00:00" là cách viết tắt phổ biến cho đóng cửa lúc nửa đêm — không
    # được tách thành một đoạn tràn ["00:00","00:00"] rỗng ở ngày kế tiếp.
    result = parse_opening_hours("Mo-Su 18:00-00:00")
    for day in DAYS:
        assert result[day] == [["18:00", "24:00"]]


def test_zero_length_interval_returns_none():
    assert parse_opening_hours("Mo-Su 12:00-12:00") is None


def test_bare_zero_length_interval_returns_none():
    assert parse_opening_hours("10:00-10:00") is None
