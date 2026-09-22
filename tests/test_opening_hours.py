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
