"""Chatbot demo: trợ lý du lịch Huế.

    streamlit run demo/app.py

Cần: PostgreSQL đang chạy (docker compose up -d db), đã chạy pipeline và
`python -m pipeline embed`. Tuỳ chọn, trong biến môi trường hoặc file .env:
OPENAI_API_KEY để bật agent LLM và nhận diện ảnh; GOOGLE_MAPS_API_KEY cho thời
gian đi có giao thông.
"""

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # các file cấu hình dùng đường dẫn tương đối từ gốc repo

import pandas as pd  # noqa: E402
import pydeck as pdk  # noqa: E402
import streamlit as st  # noqa: E402

from agent.agent import run_agent  # noqa: E402
from agent.llm import LLMClient, load_env_value, load_openai_key  # noqa: E402
from agent.nlu import detect_intent, fold, match_landmarks, parse_date, parse_trip  # noqa: E402
from agent.tools import (  # noqa: E402
    TIER_LABEL, AgentContext, extractive_answer, run_place_facts, run_plan, run_search_knowledge, run_weather,
)
from maps.client import GoogleMapsClient, decode_polyline  # noqa: E402
from maps.osrm import OsrmClient  # noqa: E402
from pipeline import db  # noqa: E402
from planner.places import load_places_by_qid  # noqa: E402
from recognition.landmark import recognize  # noqa: E402

HOTELS = {"Trung tâm Huế (bờ nam sông Hương)": (16.4637, 107.5909)}
MODE_LABEL = {"motorbike": "xe máy", "car": "ô tô", "walking": "đi bộ"}
WEEKDAY_VI = ["Thứ hai", "Thứ ba", "Thứ tư", "Thứ năm", "Thứ sáu", "Thứ bảy", "Chủ nhật"]
DAY_COLORS = [[230, 57, 70], [29, 111, 186], [42, 157, 143], [233, 150, 38], [120, 70, 170]]
EXAMPLES = [
    "Chùa Thiên Mụ được xây dựng năm nào?",
    "lang tu duc co gi dep",
    "Đại Nội mở cửa mấy giờ, giá vé bao nhiêu?",
    "Lăng Khải Định mở cửa mấy giờ?",
    "Thời tiết ngày mai ở Huế thế nào?",
    "Lập lịch 1 ngày ngày mai: Đại Nội, chùa Thiên Mụ, lăng Tự Đức, lăng Khải Định, bảo tàng cổ vật",
    "Lập lịch 1 ngày 27/10/2024: Đại Nội, chùa Thiên Mụ, lăng Tự Đức, lăng Khải Định, chợ Đông Ba, bảo tàng cổ vật",
]

st.set_page_config(page_title="Trợ lý du lịch Huế", page_icon=":material/temple_buddhist:", layout="wide")


# ---------------------------------------------------------------------------
# Tài nguyên dùng chung
# ---------------------------------------------------------------------------


@st.cache_resource
def get_conn():
    return db.connect()


@st.cache_resource(show_spinner="Đang tải model bge-m3 và kho tri thức (lần đầu ~15 giây)...")
def get_retriever():
    from rag.hybrid import HybridRetriever
    from rag.lexical import BM25Retriever, load_corpus

    try:
        return HybridRetriever.from_connection(get_conn()), None
    except Exception as exc:  # thiếu sentence-transformers hoặc model
        return (BM25Retriever(load_corpus(get_conn()), analyzer="multi"),
                f"Không tải được bge-m3 ({exc}); đang chỉ dùng BM25.")


def get_llm() -> LLMClient | None:
    if "_llm" not in st.session_state:
        key = load_openai_key()
        st.session_state["_llm"] = LLMClient(key) if key else None
    return st.session_state["_llm"]


def get_google_client() -> GoogleMapsClient:
    key = st.session_state.get("maps_key") or load_env_value("GOOGLE_MAPS_API_KEY") or ""
    client = st.session_state.get("_google_client")
    if client is None or client.api_key != key:
        client = GoogleMapsClient(api_key=key)
        st.session_state["_google_client"] = client
    return client


def get_osrm_client() -> OsrmClient:
    if "_osrm_client" not in st.session_state:
        st.session_state["_osrm_client"] = OsrmClient(load_env_value("OSRM_URL"))
    return st.session_state["_osrm_client"]


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

llm = get_llm()
with st.sidebar:
    st.header("Cài đặt demo")
    brain = st.segmented_control(
        "Bộ xử lý ngôn ngữ", ["LLM", "Luật"], default="LLM" if llm else "Luật",
        help="LLM: OpenAI gọi công cụ (tool-calling). Luật: nhận ý định bằng từ khoá, không cần API.")
    if brain == "LLM" and llm is None:
        st.warning("Chưa có OPENAI_API_KEY (biến môi trường hoặc .env); dùng chế độ luật.")
    use_llm = brain == "LLM" and llm is not None
    if llm:
        st.caption(f"Model `{llm.model}` · {llm.usage.calls} lần gọi · "
                   f"{llm.usage.input_tokens + llm.usage.output_tokens:,} token trong phiên")

    hotel_name = st.selectbox("Nơi ở", list(HOTELS))
    hotel = HOTELS[hotel_name]
    default_mode = st.selectbox("Phương tiện mặc định", list(MODE_LABEL), format_func=MODE_LABEL.get)
    max_places = st.slider("Số điểm tối đa mỗi ngày", 2, 6, 4)
    compare = st.toggle("So sánh với lịch không tính thời tiết", value=True)
    st.caption("Ngày đã qua: lập lịch trên dự báo phát hành trước 1 ngày, chấm bằng thời tiết thật (ERA5).")

    st.subheader("Chỉ đường")
    osrm = get_osrm_client()
    routing = st.segmented_control(
        "Nguồn thời gian đi", ["OSRM", "Google", "Ước lượng"], default="OSRM" if osrm.is_enabled else "Ước lượng",
        help="OSRM: đường thật từ OpenStreetMap, miễn phí, không có giao thông. Google: có giao thông, tính phí.")
    maps_client = None
    if routing == "OSRM":
        if osrm.is_enabled:
            maps_client = osrm
            st.success("OSRM: đường thật, miễn phí (không tính giao thông)")
        else:
            st.warning(osrm.disabled_note)
    elif routing == "Google":
        st.text_input("Google API key (chỉ giữ trong phiên này)", type="password", key="maps_key")
        google = get_google_client()
        if google.is_enabled:
            maps_client = google
            st.info("Google Routes (giao thông 8h, 12h, 17h). Lỗi billing hay quyền thì tự dùng ước lượng.")
            if google.billed_elements:
                st.caption(f"Đã tính phí {google.billed_elements} phần tử ma trận trong phiên.")
        else:
            st.warning("Chưa có GOOGLE_MAPS_API_KEY; thời gian đi là ước lượng.")
    else:
        st.info("Thời gian đi ước lượng từ khoảng cách (chim bay × 1,3).")
    with_routes = st.toggle(
        "Vẽ tuyến đường thật trên bản đồ" + (" (tính phí)" if routing == "Google" else ""),
        value=routing == "OSRM", disabled=maps_client is None)

    st.subheader("Câu hỏi mẫu")
    for example in EXAMPLES:
        if st.button(example, width="stretch"):
            st.session_state["pending"] = example
    if st.button("Xoá hội thoại", icon=":material/delete:"):
        st.session_state["messages"] = []


def new_context() -> AgentContext:
    return AgentContext(conn=get_conn(), retriever=get_retriever()[0], hotel=hotel, today=date.today(),
                        max_places_per_day=max_places, default_mode=default_mode, compare_baseline=compare,
                        maps_client=maps_client, with_routes=with_routes and maps_client is not None)


# ---------------------------------------------------------------------------
# Hiển thị
# ---------------------------------------------------------------------------


def render_map(itineraries, places, hotel_pt, key: str):
    paths, stops = [], []
    for d, day in enumerate(itineraries):
        if not day.visits:
            continue
        color = DAY_COLORS[d % len(DAY_COLORS)]
        path = [hotel_pt]
        for v in day.visits:
            p = places[v.place_id]
            path += decode_polyline(v.route_polyline)[1:] if v.route_polyline else [(p.lat, p.lon)]
            stops.append({"lon": p.lon, "lat": p.lat, "color": color, "label": str(len(stops) + 1),
                          "name": f"{v.arrival_time:%d/%m %H:%M} {v.place_name}"})
        path += decode_polyline(day.return_route_polyline)[1:] if day.return_route_polyline else [hotel_pt]
        paths.append({"path": [[lon, lat] for lat, lon in path], "color": color})
    if not stops:
        return
    stops.append({"lon": hotel_pt[1], "lat": hotel_pt[0], "color": [40, 40, 40], "label": "KS", "name": "Nơi ở"})
    view = pdk.ViewState(latitude=sum(s["lat"] for s in stops) / len(stops),
                         longitude=sum(s["lon"] for s in stops) / len(stops), zoom=12)
    st.pydeck_chart(pdk.Deck(
        map_provider="carto", map_style=pdk.map_styles.CARTO_LIGHT, initial_view_state=view,
        tooltip={"text": "{name}"},
        layers=[
            pdk.Layer("PathLayer", paths, get_path="path", get_color="color", width_min_pixels=3),
            pdk.Layer("ScatterplotLayer", stops, get_position=["lon", "lat"], get_fill_color="color",
                      get_radius=120, pickable=True),
            pdk.Layer("TextLayer", stops, get_position=["lon", "lat"], get_text="label", get_size=14,
                      get_pixel_offset=[0, -18]),
        ]), key=key)


def render_schedule(result, places, hotel_pt, key: str):
    for day in result.itineraries:
        st.markdown(f"**{WEEKDAY_VI[day.date.weekday()]} {day.date:%d/%m/%Y}** · thời tiết:"
                    f" {TIER_LABEL.get(day.weather_tier, day.weather_tier)}")
        for note in day.notes:
            st.caption(f":material/info: {note}")
        if not day.visits:
            st.write("Không có điểm nào trong ngày này.")
            continue
        rows = [{
            "Giờ": f"{v.arrival_time:%H:%M}–{v.departure_time:%H:%M}",
            "Địa điểm": v.place_name,
            "Di chuyển": f"{v.travel_minutes_from_prev} phút · {v.travel_distance_km:.1f} km",
            "Trong nhà": f"{v.indoor_ratio:.0%}",
            "Ghi chú": " ".join(x for x in (v.weather_note, v.travel_advice, v.route_note) if x),
            "Chỉ đường": v.google_maps_url,
        } for v in day.visits]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"Chỉ đường": st.column_config.LinkColumn(display_text="Mở Google Maps")})
        for i, url in enumerate(day.navigation_urls, 1):
            suffix = f" (phần {i})" if len(day.navigation_urls) > 1 else ""
            st.markdown(f":material/navigation: [Dẫn đường cả ngày{suffix}]({url})")
    if result.weather_adaptations:
        with st.expander("Điều chỉnh theo thời tiết"):
            for a in result.weather_adaptations:
                st.write("• " + a)
    if result.unvisited:
        with st.expander(f"Không xếp được ({len(result.unvisited)} điểm)"):
            for pid, why in result.unvisited.items():
                st.write(f"• {places[pid].name}: {why}")
    render_map(result.itineraries, places, hotel_pt, key=key)


def render_weather(rows: list[dict] | None):
    if not rows:
        return
    frame = pd.DataFrame(rows).set_index("hour")
    frame.index = [f"{h:02d}h" for h in frame.index]
    frame = frame.rename(columns={"forecast_precip_mm": "Dự báo", "observed_precip_mm": "Thực tế (ERA5)"})
    columns = [c for c in ("Dự báo", "Thực tế (ERA5)") if c in frame]
    if columns:
        st.bar_chart(frame[columns], y_label="mm/giờ", height=220)


def render_message(msg: dict, idx: int):
    with st.chat_message(msg["role"]):
        if msg.get("image"):
            st.image(msg["image"], width=320)
        st.markdown(msg["text"])
        calls = msg.get("tool_calls") or []
        if calls:
            with st.expander(f"Công cụ đã gọi ({len(calls)})", icon=":material/build:"):
                for c in calls:
                    st.markdown(f"`{c.name}` — `{c.arguments}`")
        art = msg.get("artifacts") or {}
        if art.get("chunks"):
            with st.expander("Các đoạn tri thức đã truy hồi"):
                for c in art["chunks"]:
                    st.markdown(f"**{c.place_name}** · [nguồn]({c.source_url})")
                    st.caption(c.content[:500] + ("..." if len(c.content) > 500 else ""))
        if art.get("plans"):
            if art.get("outcome"):
                cols = st.columns(len(art["outcome"]))
                for col, (name, m) in zip(cols, art["outcome"].items()):
                    col.metric(f"{name}: giờ ngoài trời khi mưa (thực tế)", f"{m['outdoor_rain_hours']:.2f} h")
                    col.metric(f"{name}: lượt đến lúc mưa lớn", m["hazard_visits"])
            render_weather(art.get("weather"))
            tabs = st.tabs(list(art["plans"]))
            for tab, (name, result) in zip(tabs, art["plans"].items()):
                with tab:
                    render_schedule(result, art["places"], art["hotel"], key=f"map-{idx}-{name}")
        elif art.get("weather"):
            render_weather(art["weather"])


# ---------------------------------------------------------------------------
# Chế độ luật (không LLM): dùng chung tool với agent
# ---------------------------------------------------------------------------


def rules_reply(question: str, ctx: AgentContext) -> str:
    intent = detect_intent(question)
    if intent == "ask":
        run_search_knowledge(ctx, question)
        if not ctx.artifacts.get("chunks"):
            return "Không tìm thấy thông tin phù hợp trong kho tri thức."
        answer, sources = extractive_answer(question, ctx.artifacts["chunks"])
        return (answer + "\n\nNguồn: " + ", ".join(f"[{i + 1}]({u})" for i, u in enumerate(sources))
                + "\n\n_Chế độ luật: trích nguyên văn đoạn liên quan nhất._")
    if intent == "info":
        names = [p.name for p in load_places_by_qid(ctx.conn, match_landmarks(question))]
        facts = run_place_facts(ctx, names)["places"]
        t = fold(question)
        lines = []
        for f in facts:
            parts = []
            if any(w in t for w in ("mo cua", "may gio", "dong cua")):
                parts.append(f"giờ mở cửa: {f['opening_hours'] or '**CSDL chưa có**'}")
            if any(w in t for w in ("gia ve", "ve vao", "bao nhieu tien")):
                vnd = f["ticket_price_vnd"]
                parts.append(f"giá vé: {vnd:,} đ".replace(",", ".") if vnd else "giá vé: **CSDL chưa có**")
            lines.append(f"**{f['place']}** — " + "; ".join(parts) + f" _(cập nhật {f['updated_at']})_")
        return "\n\n".join(lines) + "\n\n_Đọc từ CSDL; thiếu thì báo thiếu, không đoán._"
    if intent == "weather":
        day = parse_date(question, ctx.today) or ctx.today
        result = run_weather(ctx, day)
        rows = result["hours"]
        rainy = [r for r in rows if max(r.get("forecast_precip_mm", 0), r.get("observed_precip_mm", 0)) >= 0.5]
        text = f"Thời tiết Huế **{day:%d/%m/%Y}** — {result.get('tier') or result.get('kind')}."
        if rows:
            text += f" {len(rainy)}/{len(rows)} giờ (6h–20h) có mưa từ 0,5 mm/h."
        for note in result.get("notes", []):
            text += f"\n\n:material/info: {note}"
        return text
    req = parse_trip(question, ctx.today, ctx.default_mode)
    places = load_places_by_qid(ctx.conn, req.qids)
    summary = run_plan(ctx, req.start, req.days, places, req.mode)
    text = (f"Lịch **{req.days} ngày** từ **{req.start:%d/%m/%Y}**, {MODE_LABEL[req.mode]}, {len(places)} điểm."
            f" Thời gian đi: {summary['travel_time_source']}.")
    if req.assumptions:
        text += "\n\nMình đã giả định: " + "; ".join(req.assumptions) + "."
    return text


# ---------------------------------------------------------------------------
# Trang chính
# ---------------------------------------------------------------------------

st.title("Trợ lý du lịch Huế")
st.caption("Hỏi đáp về địa danh · Giờ mở cửa, giá vé · Thời tiết · Lập lịch thích ứng thời tiết · Nhận diện ảnh")

try:
    get_conn()
except Exception as exc:
    st.error(f"Không kết nối được PostgreSQL: {exc}. Chạy `docker compose up -d db` rồi tải lại trang.")
    st.stop()
_, retriever_warning = get_retriever()
if retriever_warning:
    st.warning(retriever_warning)

messages = st.session_state.setdefault("messages", [])
if not messages:
    st.info("Gõ câu hỏi, bấm câu hỏi mẫu ở thanh bên, hoặc đính kèm ảnh một địa danh để nhận diện.")
for i, msg in enumerate(messages):
    render_message(msg, i)

submitted = st.chat_input("Hỏi về Huế, hoặc đính kèm ảnh địa danh", accept_file=True,
                          file_type=["jpg", "jpeg", "png"], submit_mode="disable")
text = (submitted.text if submitted else None) or st.session_state.pop("pending", None) or ""
image = submitted.files[0].getvalue() if submitted and submitted.files else None

if text or image:
    messages.append({"role": "user", "text": text or "Đây là địa danh nào?", "image": image})
    render_message(messages[-1], len(messages) - 1)
    ctx = new_context()
    reply = {"role": "assistant", "text": "", "tool_calls": [], "artifacts": {}}
    with st.spinner("Đang xử lý..."):
        try:
            question = text
            if image is not None:
                found = recognize(ctx.conn, image, llm if use_llm else None)
                tier = {"gps": "GPS trong ảnh", "gps+vision": "GPS + vision LLM", "vision": "vision LLM",
                        "none": "không nhận diện được"}[found.tier]
                if found.place is None:
                    reply["text"] = f"Chưa nhận ra địa danh ({tier}). {found.detail}"
                    question = ""
                else:
                    conf = f", độ tin cậy {found.confidence:.0%}" if found.confidence is not None else ""
                    reply["text"] = f"Đây có vẻ là **{found.place.name}** ({tier}{conf}). {found.detail}\n\n"
                    question = text or f"Giới thiệu ngắn gọn về {found.place.name}"
            if question:
                history = [{"role": m["role"], "text": m["text"]} for m in messages[:-1] if m.get("text")]
                if use_llm:
                    try:
                        answer = run_agent(question, history, ctx, llm)
                        reply["text"] += answer.text
                        reply["tool_calls"] = answer.tool_calls
                    except Exception as exc:  # LLM lỗi (mạng, hết hạn mức): quay về chế độ luật
                        reply["text"] += (f"_LLM lỗi ({type(exc).__name__}), trả lời bằng chế độ luật._\n\n"
                                          + rules_reply(question, ctx))
                else:
                    reply["text"] += rules_reply(question, ctx)
            reply["artifacts"] = ctx.artifacts
        except Exception as exc:  # demo: báo lỗi trong khung chat
            reply["text"] += f"Có lỗi khi xử lý ({type(exc).__name__}: {exc})."
    messages.append(reply)
    render_message(reply, len(messages) - 1)
