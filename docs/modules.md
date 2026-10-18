# Chia dự án thành module chức năng

*Cập nhật 18/10/2026. Đi kèm `docs/ke_hoach_chi_tiet.md` (lịch theo tuần).*

## Nguyên tắc

1. **Mỗi module là một thư mục Python độc lập** có `api.py` (giao diện công khai), test, CLI và notebook đánh giá riêng.
2. **Module chỉ gọi nhau qua `api.py` và kiểu dữ liệu chung trong `core/`.** Không import hàm nội bộ của module khác. Nhờ vậy làm và kiểm từng module riêng được, đổi cách cài đặt bên trong không làm vỡ module khác.
3. **Mỗi module chạy được một mình** bằng dữ liệu mẫu (fixture) hoặc bản giả (stub) của module nó phụ thuộc. Không cần dựng cả hệ thống mới kiểm được.
4. **Ghép nối làm sau cùng** (module `app/`), chỉ nối các `api.py` đã hoàn thành.

## Điều kiện "hoàn thành" chung cho mọi module

Một module chỉ được đánh dấu xong khi đủ cả 6 điều:

| # | Điều kiện |
|---|---|
| 1 | `api.py` đúng chữ ký trong bảng giao diện bên dưới (đổi chữ ký phải sửa tài liệu này cùng lúc) |
| 2 | Test chạy xanh, gồm cả test cho từng ca lỗi chính |
| 3 | Notebook `notebooks/eval_<module>.ipynb` chạy được, in chỉ số + baseline + CI 95% (quy định 1) |
| 4 | Mọi mô hình dùng trong module đã được bạn chốt (quy định 2) |
| 5 | Chạy một mình được bằng fixture/stub, không cần module khác đang chạy |
| 6 | Có CLI để chạy thử nhanh từ terminal |

---

## Danh sách module

```
                       core/  (kiểu dữ liệu chung)
                          ▲
   ┌──────────┬───────────┼────────────┬─────────────┬───────────┐
 data/     weather/    planner_core/  recognition/   rag/        (mỗi module chỉ phụ thuộc core/
   ▲          ▲          ▲      ▲          ▲           ▲          và api.py của module bên dưới nó)
   │          │          │      │          │           │
   └──────────┴──── planner_llm/ ┘          │           │
                          ▲                 │           │
                          └──────── app/ (agent + Streamlit) ── ghép nối cuối cùng
                                         ▲
                                     deploy/ (GGUF, đo độ trễ)
```

| Module | Thư mục | Làm gì | Phụ thuộc | Trạng thái |
|---|---|---|---|---|
| **Core** | `core/` | Kiểu dữ liệu dùng chung (Place, WeatherHour, Problem, Schedule…), cấu hình, đường dẫn | — | **✅ Hoàn thành 19/10** |
| **Data** | `data_pipeline/` | Nạp địa điểm, nhãn tay, giờ mở cửa theo mùa, giá vé; ma trận thời gian đi (OSRM) | core | Một phần (công cụ gán nhãn) |
| **Weather** | `weather/` | Dự báo theo giờ, dữ liệu lịch sử, **M2 hiệu chỉnh xác suất mưa** | core, data | Chưa làm |
| **Planner core** | `planner_core/` | **Bộ kiểm tra khả thi** + **OR-Tools solver** + chấm điểm lịch (giờ dầm mưa, vi phạm) | core, data, weather (qua giao diện) | Chưa làm |
| **Planner LLM** | `planner_llm/` | **M1**: sinh bài toán, dữ liệu thầy, SFT, GRPO, suy luận best-of-N + sửa bằng OR-Tools | core, planner_core | Chưa làm |
| **Recognition** | `recognition/` | **M3**: thu ảnh, fine-tune, phát hiện ảnh lạ, kết hợp GPS | core, data | Một phần (script Zalo) |
| **RAG** | `rag/` | Kho chunk Wikipedia, truy hồi hybrid, sinh câu trả lời | core, data | **✅ Hoàn thành 18/10** |
| **App** | `app/` | Agent điều phối gọi các module + giao diện Streamlit | api.py của mọi module | Làm cuối |
| **Deploy** | `deploy/` | Gộp LoRA, xuất GGUF, đo chất lượng/độ trễ theo mức nén | planner_llm | Làm cuối |

---

## Giao diện công khai (`api.py`) của từng module

Kiểu dữ liệu (đã định nghĩa trong `core/types.py`; xem đủ trường bằng `python -m core schema <Kiểu>`):

| Kiểu | Trường chính |
|---|---|
| `Place` | `key` (`wikidata:Q…` / `osm:…`), `name`, `lat`, `lon`, `category`, `opening_hours` (theo mùa), `avg_visit_minutes`, `indoor_ratio`, `unsafe_conditions`, `ticket_price_vnd`, `label_source` |
| `WeatherHour` | `time`, `lat`, `lon`, `rain_prob` (đã hiệu chỉnh), `precip_mm`, `temp_c`, `wind_kmh`, `conditions` (`mua_lon`, `bao`…), `source`, `fetched_at` |
| `Problem` | `places: list[Place]`, `days`, `start_date`, `day_window`, `hotel`, `travel_matrix`, `weather: list[WeatherHour]`, `max_places_per_day`, `preferences` |
| `Visit` / `Schedule` | `Visit(day, place_key, start, end)`; `Schedule(visits, explanation, source)` với `source` ∈ {llm, ortools, repaired} |
| `FeasibilityReport` | `violations: tuple[Violation(kind, detail, visit_index)]`, `metrics`; `feasible` suy ra = không có vi phạm. 15 loại vi phạm trong `ViolationKind` |
| `TravelMatrix` / `Location` / `Preferences` | ma trận phút giữa `hotel` và các place; khách sạn; must_visit / avoid / interests / pace / budget_vnd |
| `Recognition` | `place_key | None`, `score`, `candidates`, `is_ood`, `used_gps` |
| `Passage` / `Answer` | `Passage(chunk_id, place_key, title, section, text, score)`; `Answer(text, citations, context, model, refused, timings, output_tokens)` — đã có trong `core/types.py` |

| Module | Hàm công khai |
|---|---|
| data | `load_places(categories=None) -> list[Place]` · `get_place(key) -> Place` · `travel_matrix(places, mode, depart_at) -> Matrix` |
| weather | `forecast(lat, lon, start, end, calibrated=True) -> list[WeatherHour]` · `history(lat, lon, start, end, lead_days) -> list[WeatherHour]` |
| planner_core | `check(schedule, problem) -> FeasibilityReport` · `solve(problem, time_limit_s) -> Schedule` · `score(schedule, problem, observed_weather) -> dict` |
| planner_llm | `propose(problem, n) -> list[Schedule]` · `plan(problem) -> Schedule` (best-of-N → check → repair) |
| recognition | `recognize(image_bytes, gps=None) -> Recognition` |
| rag | `retrieve(query, k=5, place_key=None) -> list[Passage]` · `answer(query, k=5, place_key=None) -> Answer` · lỗi LLM: `LLMUnavailableError` · `RAGService.warm_up_llm()` cho app gọi lúc khởi động |
| app | `chat(message, history, attachments) -> Iterator[Event]` (`tool_start`, `tool_end`, `final`) |

---

## Chi tiết từng module

### Core — ✅ hoàn thành 19/10/2026
| File | Nội dung |
|---|---|
| `core/types.py` | 15 kiểu: `OpeningRule`, `OpeningHours` (giờ theo mùa/thứ, `covers(ngày, giờ đến, giờ rời)`), `Place`, `Location`, `WeatherHour`, `TravelMatrix`, `Preferences`, `Problem`, `Visit`, `Schedule`, `Violation`/`ViolationKind`, `FeasibilityReport`, `Recognition`, `Passage`, `Answer`; từ vựng chung (`WEATHER_CONDITIONS`, `TIMES_OF_DAY`, `HOTEL_KEY`…) |
| `core/io.py` | Lưu/đọc JSON và JSONL dạng `{"schema", "version", "data"}`; đọc lại luôn kiểm hợp lệ, lỗi báo `SchemaError` (kèm số dòng với JSONL) |
| `core/config.py` | Đường dẫn dự án, múi giờ `VN_TZ` (+07:00 cố định), đọc `.env` |
| `core/fixtures.py` | Bài toán mẫu 5 địa điểm Huế + ma trận ước lượng + thời tiết giả (mưa 13–16h) để module khác test độc lập |
| `python -m core` | `schema [Kiểu]`, `example Kiểu`, `check file.json/.jsonl [--type]` |

- **Nguyên tắc:** kiểu chỉ kiểm **hình dạng** (khoảng giá trị, từ vựng, giờ đóng sau giờ mở, ma trận vuông đủ điểm…). Kiểm **ngữ nghĩa** (đến khi đóng cửa, đi không kịp, chồng giờ) để `planner_core.check()` làm, nên lịch sai của LLM vẫn biểu diễn được để chấm.
- **JSON dễ đọc cho LLM:** giờ `"HH:MM"`, ngày `"YYYY-MM-DD"`, thời điểm ISO có `+07:00`, tập hợp là danh sách đã sắp xếp.
- **Test:** `tests/test_core_types.py`, `tests/test_core_io.py` — mọi kiểu đi-về JSON không đổi, từng ca dữ liệu sai bị chặn.
- **Không có notebook:** module không có chất lượng cần đo (đúng quy định chung).

### Data
- **Làm:** nạp `data/staged/*` + `config/overrides.csv` + `config/place_facts.csv` → danh sách `Place`; giờ mở cửa theo mùa; ma trận thời gian đi OSRM (có cache), dự phòng ước lượng khoảng cách.
- **Chỉ số (notebook `eval_data.ipynb`):** độ phủ từng trường (% có giờ mở cửa, % có nhãn tay…), Cohen's kappa giữa 2 người gán nhãn, sai số ma trận ước lượng so với OSRM (MAE phút).
- **Việc của bạn:** gán nhãn tay 50 địa điểm + người thứ hai gán 20 điểm.

### Weather (gồm M2)
- **Làm:** client Open-Meteo (forecast, Previous Runs lead 1–10, ERA5), cache lưới 0.1°; dataset dự báo ↔ thực tế; LightGBM hiệu chỉnh P(mưa).
- **Chỉ số (`eval_weather.ipynb`):** Brier, AUC, precision/recall/F1 tại ngưỡng mưa, reliability diagram theo số ngày dự báo trước; so với dự báo gốc và trung bình khí hậu.
- **Chạy một mình:** dữ liệu cache trên đĩa, không cần planner.
- **Chốt mô hình:** LightGBM (đề xuất).

### Planner core
- **Làm:** `check()` cho từng loại vi phạm (giờ mở cửa theo mùa, đi không kịp, khung ngày, trùng điểm, điểm nguy hiểm khi bão/mưa lớn, quá số điểm); `solve()` bằng OR-Tools VRPTW với điểm thời tiết; `score()` chấm lịch trên thời tiết thật.
- **Chỉ số (`eval_planner_core.ipynb`):** precision/recall/F1 của bộ kiểm tra trên bộ lịch gán nhãn tay; % khả thi của solver (phải 100%); gap tối ưu theo time limit; thời gian giải p50/p95; thực nghiệm aware/baseline/oracle (giờ dầm mưa).
- **Chạy một mình:** dùng `weather` stub (WeatherHour từ file).

### Planner LLM (M1)
- **Làm:** bộ sinh bài toán + chia ngày train/dev/test; dữ liệu thầy; định dạng prompt/JSON; SFT; GRPO (reward từ `planner_core.check/score`); `plan()` best-of-N + sửa.
- **Chỉ số (`train_planner_sft.ipynb`, `train_planner_grpo.ipynb`, `eval_planner_llm.ipynb`):** loss/reward theo step; % JSON hợp lệ; % khả thi; F1 bám ràng buộc người dùng; gap tới OR-Tools; giờ dầm mưa; phân loại vi phạm; tỷ lệ lịch rỗng; độ trễ.
- **Chốt mô hình:** LLM để fine-tune (đề xuất Qwen3.5-2B/4B), LLM baseline.

### Recognition (M3)
- **Làm:** `collect` (đếm → tải Commons/Flickr/Mapillary, manifest, giấy phép), lọc trùng, chia theo tác giả; fine-tune; OOD; prior GPS; `recognize()`.
- **Chỉ số (`train_vision.ipynb`, `eval_vision.ipynb`):** loss/Top-1 dev theo epoch; Top-1/5; precision/recall/F1 từng lớp + macro; confusion matrix; AUROC, FPR@95; ablation ảnh/GPS/kết hợp.
- **Chốt mô hình:** backbone (đề xuất SigLIP-2 / CLIP ViT-B), loss.
- **Việc của bạn:** API key Flickr, token Mapillary; nhờ người chụp ảnh.

### RAG — ✅ hoàn thành 18/10/2026
| Điều kiện | Trạng thái |
|---|---|
| 1. `api.py` | `rag/api.py`: `retrieve`, `answer` trả `core.types.Passage`/`Answer`; lọc theo địa danh (`place_key`); `LLMUnavailableError`; `set_service()` để thay service |
| 2. Test | `tests/test_rag_*.py`, `tests/test_core_types.py` (43 test toàn repo, xanh) |
| 3. Notebook | `notebooks/eval_rag.ipynb` — 188 câu đã duyệt: hybrid bge-m3 MRR@10 0.934 [0.905, 0.961]; qwen3.5:4b TTFT p50 3.2 s, phủ đáp án 0.90 |
| 4. Chốt mô hình | hybrid bge-m3 + BM25, qwen3.5:4b (18/10) |
| 5. Chạy một mình | chỉ cần `data/rag/chunks.jsonl` + Ollama |
| 6. CLI | `python -m rag build | search | ask` |

- `Chunk` là kiểu nội bộ của kho; ra ngoài module luôn là `Passage`.
- Hiệu năng (đã khởi động sẵn): truy hồi ~100 ms, TTFT ~3 s. Dựng service lần đầu ~30 s (import torch/transformers ~15–25 s, nạp bge-m3); venv đặt ngoài OneDrive ở `C:/dev/venvs/travel` (gói: `requirements-lock.txt`).
- Ngoài phạm vi RAG: giá vé, giờ mở cửa (Wikipedia không có) — `app/` phải chuyển các câu này sang module data.
- Hạn chế ghi trong báo cáo: bộ câu hỏi do AI soạn (có lợi cho BM25); qwen3.5:9b không vừa trọn 8 GB VRAM.

### App (ghép nối — làm cuối)
- **Làm:** agent tool-calling chỉ gọi `api.py` các module; Streamlit một trang (chat, lịch trình, gửi ảnh).
- **Chỉ số (`eval_app.ipynb`):** tỷ lệ chọn đúng tool trên bộ câu hỏi hỗn hợp (precision/recall/F1 theo tool); độ trễ đầu-cuối p50/p95 từng luồng (hỏi đáp, lập lịch, nhận diện ảnh); tỷ lệ lỗi.
- **Chốt mô hình:** LLM điều phối (đề xuất qwen3.5:9b hoặc 4b).

### Deploy (làm cuối)
- **Làm:** gộp LoRA, xuất GGUF 3 mức nén, chạy bằng Ollama.
- **Chỉ số (`eval_quantization.ipynb`):** % khả thi, F1 ràng buộc, TTFT, tokens/giây, VRAM theo từng mức nén.

---

## Thứ tự làm

| Thứ tự | Module | Lý do đứng ở vị trí này |
|---|---|---|
| 1 | Core | Mọi module dùng kiểu chung; làm trước để không phải sửa lại |
| 2 | RAG (đóng nốt `api.py`) | Gần xong, chỉ cần khớp với kiểu chung — đóng sớm cho gọn |
| 3 | Data | Planner, recognition, weather đều cần `Place` |
| 4 | Planner core | Trọng tài cho M1; phải xong trước khi sinh dữ liệu thầy |
| 5 | Weather (M2) | Chạy nền được song song với 4 (tải dữ liệu lâu) |
| 6 | Planner LLM (M1) | Đóng góp chính; cần 3, 4 xong |
| 7 | Recognition (M3) | Độc lập với planner; thu ảnh bắt đầu sớm song song từ bước 3 |
| 8 | App | Ghép nối khi các `api.py` đã xong |
| 9 | Deploy | Sau khi có model M1 cuối cùng |

Ghép nối không phải chờ tất cả: khi Core, Data, Planner core, RAG xong có thể dựng `app/` bản đầu dùng OR-Tools làm planner, rồi thay bằng `planner_llm.plan()` khi M1 xong — chỉ đổi một dòng vì cùng giao diện.
