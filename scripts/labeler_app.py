"""Ứng dụng Streamlit hỗ trợ gán nhãn thủ công & hoàn thiện dữ liệu cho Huế.

Chạy ứng dụng:
    python -m streamlit run scripts/labeler_app.py
"""

import csv
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.chdir(ROOT)

import pandas as pd
import streamlit as st

from label_source import GROUP_FACTS, GROUP_FOOD, GROUP_SIGHTS, load_places

OVERRIDES_PATH = ROOT / "config" / "overrides.csv"
FIELDNAMES = [
    "place_key",
    "indoor_ratio",
    "avg_visit_minutes",
    "best_time_of_day",
    "unsafe_conditions",
    "ticket_price_vnd",
    "dress_code",
    "note",
]

TIME_OF_DAY_OPTIONS = [
    "sang_som",
    "sang",
    "trua",
    "chieu",
    "chieu_muon",
    "hoang_hon",
    "toi",
    "bat_ky",
]

UNSAFE_CONDITION_OPTIONS = [
    "mua_lon",
    "bao",
    "nang_gat",
    "gio_lon",
    "song_lon",
]

GROUP_LABELS = {
    GROUP_FACTS: "Có nguồn giờ/giá (place_facts.csv)",
    GROUP_SIGHTS: "Tham quan",
    GROUP_FOOD: "Ăn uống",
}

st.set_page_config(
    page_title="Công cụ Gán nhãn Dữ liệu Huế",
    page_icon="🏷️",
    layout="wide",
)


def load_existing_overrides() -> dict[str, dict]:
    """Đọc file config/overrides.csv hiện tại."""
    if not OVERRIDES_PATH.exists():
        return {}
    overrides = {}
    with open(OVERRIDES_PATH, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            key = (row.get("place_key") or "").strip()
            if key:
                overrides[key] = row
    return overrides


def save_overrides(overrides: dict[str, dict]) -> None:
    """Ghi đè file config/overrides.csv."""
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OVERRIDES_PATH, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for key in sorted(overrides.keys()):
            writer.writerow(overrides[key])


@st.cache_data
def fetch_places() -> list[dict]:
    """Địa điểm thật từ data/staged/*.json; `key` là place_key của pipeline."""
    return [asdict(p) for p in load_places()]


def main():
    st.title("🏷️ Công cụ Gán nhãn & Hoàn thiện Dữ liệu Du lịch Huế")
    st.markdown(
        "Công cụ này giúp bạn dễ dàng gán nhãn thủ công cho tiêu chí chất lượng dữ liệu đồ án: "
        "`indoor_ratio`, `avg_visit_minutes`, `best_time_of_day`, `unsafe_conditions`, `ticket_price_vnd`, `dress_code`. "
        "Dữ liệu được ghi trực tiếp vào file chuẩn [`config/overrides.csv`](file:///c:/Users/baoba/OneDrive/M%C3%A1y%20t%C3%ADnh/Travel/config/overrides.csv)."
    )

    overrides = load_existing_overrides()
    places = fetch_places()
    known_keys = {p["key"] for p in places}
    orphan_keys = sorted(set(overrides) - known_keys)
    if orphan_keys:
        st.warning(f"Nhãn trong overrides.csv không khớp địa điểm nào trong data/staged: {', '.join(orphan_keys)}")

    col_stat1, col_stat2 = st.columns(2)
    col_stat1.metric("Địa điểm (data/staged)", f"{len(places)}")
    labeled = len(set(overrides) & known_keys)
    col_stat2.metric("Đã gán nhãn trong CSV", f"{labeled} / 100", delta=f"{labeled - 100} (Mục tiêu 100)")

    st.divider()

    # Bộ lọc
    col_f1, col_f2, col_f3 = st.columns([1, 1, 2])
    with col_f1:
        cat_filter = st.selectbox(
            "Nhóm:",
            ["Tất cả"] + list(GROUP_LABELS.keys()),
            index=1,
            format_func=lambda x: GROUP_LABELS.get(x, x)
        )
    with col_f2:
        status_filter = st.selectbox(
            "Trạng thái gán nhãn:",
            ["Chưa gán nhãn thủ công", "Đã gán nhãn trong CSV", "Tất cả"]
        )
    with col_f3:
        search_query = st.text_input("🔍 Tìm kiếm tên địa điểm:", placeholder="Nhập tên địa điểm...")

    filtered_places = []
    for p in places:
        pkey = p["key"]
        is_labeled = pkey in overrides

        if status_filter == "Chưa gán nhãn thủ công" and is_labeled:
            continue
        elif status_filter == "Đã gán nhãn trong CSV" and not is_labeled:
            continue

        if cat_filter != "Tất cả" and p["group"] != cat_filter:
            continue

        if search_query:
            q = search_query.lower()
            if q not in p["name"].lower() and q not in pkey.lower():
                continue

        filtered_places.append((p, pkey, is_labeled))

    st.write(f"Hiển thị **{len(filtered_places)}** địa điểm phù hợp:")

    if not filtered_places:
        st.info("Không tìm thấy địa điểm nào khớp với bộ lọc.")
        return

    col_left, col_right = st.columns([1, 1.3])

    with col_left:
        place_options = {
            f"{'✅ ' if is_lbl else '⏳ '}[{p['kind']}] {p['name']} ({pkey})": idx
            for idx, (p, pkey, is_lbl) in enumerate(filtered_places)
        }
        selected_label = st.radio(
            "Chọn địa điểm cần gán nhãn:",
            list(place_options.keys()),
            key="selected_place_radio"
        )
        selected_idx = place_options[selected_label]
        target_place, target_key, target_is_labeled = filtered_places[selected_idx]

        st.subheader(f"📌 {target_place['name']}")
        st.write(f"**Khóa nhận diện (`place_key`):** `{target_key}`")
        st.write(f"**Loại (thẻ nguồn):** `{target_place['kind']}` · {GROUP_LABELS[target_place['group']]}")
        if target_place["source_url"]:
            st.markdown(f"🔗 [Nguồn]({target_place['source_url']})")

        lat, lon = target_place.get("lat"), target_place.get("lon")
        if lat and lon:
            gmaps_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            gsearch_url = f"https://www.google.com/search?q={target_place['name']}+Huế"
            st.markdown(f"🔗 [Xem trên Google Maps]({gmaps_url}) | 🔍 [Tra cứu trên Google]({gsearch_url})")
            
            df_map = pd.DataFrame([{"lat": lat, "lon": lon}])
            st.map(df_map, zoom=14)

    with col_right:
        st.subheader("📝 Form Gán nhãn thủ công")
        
        existing_row = overrides.get(target_key, {})

        init_indoor = float(existing_row.get("indoor_ratio") or target_place.get("indoor_ratio") or 0.5)
        init_visit = int(existing_row.get("avg_visit_minutes") or target_place.get("avg_visit_minutes") or 60)
        
        raw_best_time = existing_row.get("best_time_of_day")
        if raw_best_time:
            init_best_time = [x.strip() for x in raw_best_time.split("|") if x.strip()]
        else:
            init_best_time = target_place.get("best_time_of_day") or ["sang_som", "chieu_muon"]

        raw_unsafe = existing_row.get("unsafe_conditions")
        if raw_unsafe:
            init_unsafe = [x.strip() for x in raw_unsafe.split("|") if x.strip()]
        else:
            init_unsafe = target_place.get("unsafe_conditions") or ["mua_lon"]

        init_price_raw = existing_row.get("ticket_price_vnd")
        if init_price_raw:
            init_price = int(init_price_raw)
        else:
            t_price_dict = target_place.get("ticket_price")
            init_price = int(t_price_dict.get("vnd", 0)) if isinstance(t_price_dict, dict) else 0

        init_dress = existing_row.get("dress_code") or target_place.get("dress_code") or ""
        init_note = existing_row.get("note") or ""

        with st.form(key=f"label_form_{target_key}"):
            st.write(f"**Gán nhãn cho:** `{target_key}`")
            
            indoor_ratio = st.slider(
                "Tỷ lệ trong nhà (indoor_ratio: 0.0 = ngoài trời hoàn toàn, 1.0 = trong nhà hoàn toàn):",
                min_value=0.0, max_value=1.0, value=min(max(init_indoor, 0.0), 1.0), step=0.05
            )

            avg_visit_minutes = st.number_input(
                "Thời gian tham quan trung bình (phút):",
                min_value=10, max_value=480, value=max(init_visit, 15), step=15
            )

            best_time_of_day = st.multiselect(
                "Khung giờ thích hợp nhất trong ngày (best_time_of_day):",
                options=TIME_OF_DAY_OPTIONS,
                default=[t for t in init_best_time if t in TIME_OF_DAY_OPTIONS] or ["sang_som"]
            )

            unsafe_conditions = st.multiselect(
                "Điều kiện thời tiết rủi ro / không an toàn (unsafe_conditions):",
                options=UNSAFE_CONDITION_OPTIONS,
                default=[u for u in init_unsafe if u in UNSAFE_CONDITION_OPTIONS] or ["mua_lon"]
            )

            ticket_price_vnd = st.number_input(
                "Giá vé (VNĐ) (0 nếu miễn phí/chưa rõ):",
                min_value=0, max_value=2000000, value=init_price, step=10000
            )

            dress_code = st.text_input(
                "Quy định trang phục (dress_code):",
                value=init_dress,
                placeholder="Ví dụ: lich_su (trang phục lịch sự khi vào đền chùa)"
            )

            note = st.text_input(
                "Ghi chú (note):",
                value=init_note,
                placeholder="Ví dụ: Đi bộ nhiều sân lộ thiên, cần mang ô/mũ"
            )

            col_sub1, col_sub2 = st.columns(2)
            with col_sub1:
                submitted = st.form_submit_button("💾 Lưu vào config/overrides.csv", use_container_width=True)
            with col_sub2:
                if target_is_labeled:
                    delete_submitted = st.form_submit_button("🗑️ Xóa khỏi overrides.csv", use_container_width=True)
                else:
                    delete_submitted = False

        if submitted:
            new_entry = {
                "place_key": target_key,
                "indoor_ratio": f"{indoor_ratio:.2f}".rstrip('0').rstrip('.'),
                "avg_visit_minutes": str(avg_visit_minutes),
                "best_time_of_day": "|".join(best_time_of_day),
                "unsafe_conditions": "|".join(unsafe_conditions),
                "ticket_price_vnd": str(ticket_price_vnd) if ticket_price_vnd > 0 else "",
                "dress_code": dress_code.strip(),
                "note": note.strip(),
            }
            overrides[target_key] = new_entry
            save_overrides(overrides)
            st.success(f"✅ Đã lưu nhãn thành công cho {target_place['name']} ({target_key})!")
            st.rerun()

        if delete_submitted:
            if target_key in overrides:
                del overrides[target_key]
                save_overrides(overrides)
                st.warning(f"❌ Đã xóa {target_place['name']} khỏi config/overrides.csv.")
                st.rerun()


if __name__ == "__main__":
    main()
