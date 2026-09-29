from datetime import date

from agent.nlu import (
    DEFAULT_TRIP, best_sentences, detect_intent, match_landmarks, parse_date, parse_days, parse_mode,
    parse_trip,
)

TODAY = date(2026, 9, 27)  # chủ nhật


def test_detect_intent():
    assert detect_intent("Lập lịch 2 ngày đi Đại Nội và lăng Tự Đức") == "plan"
    assert detect_intent("mai đi chơi đâu ở Huế") == "plan"
    assert detect_intent("Đại Nội mở cửa mấy giờ?") == "info"
    assert detect_intent("giá vé Hoàng thành bao nhiêu") == "info"
    assert detect_intent("Thời tiết ngày mai ở Huế thế nào?") == "weather"
    assert detect_intent("Chùa Thiên Mụ được xây năm nào?") == "ask"
    assert detect_intent("mo cua may gio") == "ask"  # không nêu địa danh thì không tra DB


def test_match_landmarks_without_diacritics_and_in_order():
    assert match_landmarks("đi lang tu duc roi dai noi") == ["Q7481171", "Q10769129"]
    assert match_landmarks("Chùa Thiên Mụ, Chợ Đông Ba") == ["Q975568", "Q97273263"]
    # "bảo tàng cổ vật" không bị nhận thêm là "Văn miếu" hay gì khác
    assert match_landmarks("Bảo tàng Cổ vật cung đình") == ["Q5929149"]
    assert match_landmarks("quán bún bò gần đây") == []


def test_parse_date():
    assert parse_date("hôm nay", TODAY) == TODAY
    assert parse_date("ngày mai đi đâu", TODAY) == date(2026, 9, 28)
    assert parse_date("ngày mốt", TODAY) == date(2026, 9, 29)
    assert parse_date("thứ bảy này", TODAY) == date(2026, 10, 3)
    assert parse_date("đi ngày 27/10/2025", TODAY) == date(2025, 10, 27)
    assert parse_date("ngày 5/10", TODAY) == date(2026, 10, 5)
    assert parse_date("đi Huế", TODAY) is None


def test_parse_days_and_mode():
    assert parse_days("lịch 2 ngày") == 2
    assert parse_days("hai ngày một đêm") == 2
    assert parse_days("đi ngày mốt") is None
    assert parse_days("30 ngày") == 7
    assert parse_mode("đi ô tô") == "car"
    assert parse_mode("di bo thoi") == "walking"
    assert parse_mode("đi Đại Nội") is None


def test_parse_trip_reports_assumptions():
    req = parse_trip("Lập lịch đi chơi Huế", TODAY)
    assert req.qids == DEFAULT_TRIP and req.days == 1 and req.start == date(2026, 9, 28)
    assert len(req.assumptions) == 3
    req = parse_trip("lịch 2 ngày từ 27/10/2025, Đại Nội, lăng Khải Định, đi ô tô", TODAY)
    assert (req.start, req.days, req.mode) == (date(2025, 10, 27), 2, "car")
    assert req.qids == ["Q10769129", "Q7818621"] and req.assumptions == []


def test_best_sentences_keeps_relevant_sentences():
    content = ("Chùa Thiên Mụ nằm trên đồi Hà Khê. Chùa được xây dựng năm 1601 dưới thời chúa Nguyễn Hoàng. "
               "Tháp Phước Duyên cao bảy tầng. Hằng năm có nhiều du khách ghé thăm nơi này.")
    answer = best_sentences("Chùa Thiên Mụ xây năm nào", content, limit=1, context="Chùa Thiên Mụ")
    assert answer == "Chùa được xây dựng năm 1601 dưới thời chúa Nguyễn Hoàng."
