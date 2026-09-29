# Travel Agent — hướng dẫn cho agent

Trợ lý du lịch Việt Nam: nhận diện địa danh từ ảnh, hỏi đáp thông tin địa điểm, lập lịch trình thích ứng thời tiết, chỉ đường. Đây là đồ án tốt nghiệp — mọi quyết định kỹ thuật phải phục vụ được một đóng góp đo lường được, không chỉ chạy demo.

Repo hiện chưa có code. Khi dựng những phần đầu tiên, theo đúng các quyết định dưới đây; nếu một quyết định không còn phù hợp, sửa file này cùng lúc với code.

## Phạm vi

MVP giới hạn **thành phố Huế**, 50–100 địa danh, 200–300 quán ăn. Làm sâu một khu vực, không mở rộng địa bàn. Khi một yêu cầu kéo phạm vi ra ngoài Huế, nói rõ điều đó trước khi làm.

Đóng góp chính của đồ án là **tối ưu lịch trình theo thời tiết** (hướng A). Các tính năng khác làm ở mức đủ dùng. Ưu tiên công sức theo thứ tự đó.

## Kiến trúc

LLM agent điều phối + tools. LLM (có khả năng đọc ảnh) hiểu yêu cầu, gọi tool, tổng hợp câu trả lời. Các tool: nhận diện địa danh, RAG tri thức, tìm địa điểm & review, thời tiết, lập lịch, chỉ đường.

| Thành phần | Lựa chọn |
|---|---|
| Backend | FastAPI (Python) |
| Điều phối agent | Tool-calling qua API tương thích OpenAI Chat Completions (`agent/llm.py`). Mặc định Ollama local `qwen3.5:9b` (`OPENAI_BASE_URL=http://localhost:11434/v1`, `OLLAMA_CONTEXT_LENGTH=8192`), dự phòng `qwen3.5:4b`; OpenAI `gpt-5.4-mini` khi bỏ `OPENAI_BASE_URL` và có key; Gemini qua endpoint tương thích OpenAI cho baseline. Đổi bằng `OPENAI_BASE_URL` / `OPENAI_MODEL`; không có key lẫn server local thì chạy chế độ luật (`agent/nlu.py`). Chọn model bằng số liệu: `python scripts/llm_smoke_test.py` |
| CSDL | PostgreSQL + PostGIS + pgvector |
| Cache | Redis (dựng khi có backend API) |
| Lưu file | `data/raw/` trên đĩa; chuyển sang MinIO khi có backend |
| Tối ưu lịch | Google OR-Tools |
| Thời tiết | Open-Meteo |
| Chỉ đường | OSRM tự host (mặc định, miễn phí, `maps/osrm.py`, dữ liệu OSM Việt Nam trong Docker volume `travel_osrm`); Google Routes (`maps/client.py`) khi cần giao thông và có billing; không có cả hai thì ước lượng. Chọn nguồn: `maps/routing.py`. Dẫn đường bằng deep link Maps URLs |

Các dịch vụ mặc định dùng API miễn phí: Wikidata, Wikipedia, Wikivoyage, OSM Overpass, Wikimedia Commons, Open-Meteo. Sử dụng **Google Maps API** cho riêng tính năng chỉ đường và ma trận giao thông/tắc đường real-time. Dùng **LLM qua API tương thích OpenAI** (mặc định Ollama local, miễn phí) cho agent (trích ràng buộc, diễn giải kết quả tool) và vision LLM ở tầng 3 nhận diện ảnh; mọi thông tin đúng/sai vẫn đến từ tool. Model phải có cả vision lẫn tools (qwen3.5 có; phần lớn vision model khác trên Ollama không nhận tools). Gọi Ollama `/v1` với `reasoning_effort="none"` để tắt thinking (`extra_body={"think": false}` không có tác dụng trên Ollama 0.17). Không dùng Google Places / Foursquare. Key đặt trong `.env` hoặc biến môi trường, không bao giờ commit.

Python 3.14 trên máy phát triển. Tránh Scrapy (chưa chắc có wheel); dùng `httpx`, `selectolax`, `trafilatura`, `psycopg` v3.

## Ranh giới giữa LLM và thuật toán

Đây là ranh giới quan trọng nhất của dự án. LLM làm phần ngôn ngữ; thuật toán làm phần đúng/sai.

- **Lập lịch trình:** LLM chỉ trích xuất ràng buộc ra JSON (bước 1) và diễn giải kết quả thành văn bản (bước 3). Việc chọn và sắp xếp địa điểm do thuật toán làm (bước 2): gom cụm theo vị trí, rồi giải VRP có khung thời gian bằng OR-Tools. LLM tự viết lịch tạo ra lịch phi thực tế (di chuyển không kịp, đến khi đã đóng cửa) — đó chính là baseline mà đồ án so sánh, không phải cách triển khai.
- **Giờ mở cửa, giá vé, quy định trang phục:** đọc từ trường có cấu trúc trong DB, hiển thị kèm `updated_at`. Loại thông tin này hay thay đổi và LLM dễ bịa.
- **Thông tin mô tả, lịch sử:** qua RAG từ `place_chunks`, không để LLM trả lời từ trí nhớ.
- **Dự báo thời tiết:** đưa vào prompt dạng số liệu kèm thời điểm lấy dữ liệu; với ngày ở xa, yêu cầu LLM diễn đạt không chắc chắn.

## Nhận diện địa danh

Ba tầng, chạy từ rẻ đến đắt, chỉ lên tầng sau khi tầng trước không đạt ngưỡng tin cậy:

1. GPS trong EXIF → truy vấn PostGIS bán kính ~200m.
2. Embedding ảnh (CLIP/SigLIP) so với kho ảnh địa danh trong `place_images` (20–50 ảnh mỗi địa danh, nhiều góc chụp).
3. Vision LLM dự phòng — LLM đoán sai nhiều với địa danh ít nổi tiếng. Chỉ được chọn trong danh sách địa danh có trong DB, dưới ngưỡng tin cậy 0.5 thì trả "không xác định".

GPS là vị trí người chụp, không phải vật được chụp (ảnh Cầu Trường Tiền chụp từ bờ sông có GPS cách tượng đài Phan Bội Châu 72 m). Có vision LLM thì GPS chỉ thu hẹp ứng viên trong 800 m để vision chọn; không có thì lấy điểm gần nhất trong 200 m. Tầng 2 (embedding ảnh) chưa làm. Cài đặt: `recognition/landmark.py`.

## Thời tiết trong bộ tối ưu

Độ chi tiết theo khoảng cách tới ngày đi: 0–3 ngày dùng dự báo theo giờ và xếp lịch theo khung giờ; 4–10 ngày dùng dự báo theo ngày và chỉ quyết định ngoài trời / trong nhà; trên 10 ngày dùng dữ liệu khí hậu nhiều năm và chỉ cảnh báo xu hướng.

Hai loại ràng buộc:

- **Cứng:** cảnh báo bão loại bỏ đi thuyền, ra đảo, leo núi; mưa lớn loại điểm ngoài trời không an toàn. Dùng `unsafe_conditions` của place.
- **Mềm:** mỗi cặp (địa điểm, khung giờ) có điểm phù hợp thời tiết, tính từ `weather_sensitivity` và `indoor_ratio`, đưa vào hàm mục tiêu.

Cache dự báo theo lưới tọa độ làm tròn ~0.1 độ, không gọi API cho từng địa điểm.

## Tối ưu chỉ đường dựa trên thời tiết và độ tắc đường (Google Maps API)

Dùng Google Routes API (`computeRouteMatrix` cho ma trận, `computeRoutes` cho tuyến; Distance Matrix API cũ đã Legacy) để tính thời gian đi thực tế, kết hợp giao thông và thời tiết. Cài đặt ở `maps/client.py`, `maps/flood.py`, `planner/matrix.py`, `planner/routes.py`, `planner/service.py`:

1. **Dữ liệu giao thông thời gian thực (Real-time Traffic):**
   - Lấy `duration` (có giao thông) và `staticDuration` (không giao thông) tại các mốc `traffic_hours` (mặc định 8h, 12h, 17h) của từng ngày; solver dùng mốc gần giờ đến nhất. Routes API chỉ nhận giờ khởi hành trong tương lai.
   - Cập nhật ma trận thời gian di chuyển (Travel Time Matrix) làm đầu vào chính xác cho thuật toán tối ưu lịch trình (OR-Tools VRP) thay vì dùng khoảng cách địa lý đơn thuần.

2. **Tối ưu chỉ đường thích ứng Thời tiết (Weather-aware Routing):**
   - **Hệ số phạt thời gian di chuyển:** `planner.rules.travel_factor`: mưa ×1.2, mưa lớn ×1.4, bão ×1.8.
   - **Né tuyến rủi ro:** khi dự báo mưa lớn/bão, đoạn đi qua vùng trong `config/flood_zones.yml` bị phạt trong hàm mục tiêu, và `attach_routes` xin tuyến thay thế không qua vùng đó. **Chưa có dữ liệu vùng ngập:** OSM không có đường nào ở Huế gắn `flood_prone`; chỉ thêm vùng có nguồn kiểm chứng được.
   - **Gợi ý phương tiện:** chặng đi xe máy lúc dự báo mưa lớn/bão có `travel_advice` khuyên đi taxi/ô tô kèm link dẫn đường ô tô.

3. **Tích hợp thuật toán & Trải nghiệm người dùng:**
   - **Hàm mục tiêu tổng hợp:** $\text{Cost}(i, j) = \text{Google\_Traffic\_Duration}(i, j) \times \text{Weather\_Penalty} + \text{Risk\_Penalty}$.
   - **Deep link & Dẫn đường:** mỗi chặng có link `dir_action=navigate`; mỗi ngày có link đủ các điểm (chia link khi quá 9 waypoint). `attach_routes` điền encoded polyline để giao diện vẽ tuyến.
   - **Chi phí:** ma trận tính phí theo phần tử; `TRAFFIC_AWARE` là SKU Pro, `TWO_WHEELER` là SKU Enterprise nên xe máy mặc định dùng `DRIVE`. Mỗi client có trần `max_billable_elements`. Không lưu kết quả Google ra đĩa (điều khoản hạn chế lưu nội dung); thực nghiệm hàng loạt dùng ước lượng.

## Dữ liệu

Dataset và API miễn phí làm nền: Wikidata, Wikipedia/Wikivoyage, OpenStreetMap Overpass, Wikimedia Commons, Open-Meteo. Crawl trang di tích và sở du lịch để bổ sung giá vé, quy định trang phục. Foursquare và Google Places chỉ cân nhắc khi có key và khi OSM thiếu quá nhiều.

Quy trình: lưu bản thô vào `data/raw/` → trích nội dung chính (Trafilatura) → chuẩn hóa thành trường có cấu trúc → kiểm duyệt thủ công ngẫu nhiên một phần → nạp vào DB.

Bước chuẩn hóa hiện là **rule-based**, không phải LLM: parser `opening_hours` cho cú pháp OSM, bảng tra category, mặc định theo category cho nhóm trường thời tiết, cộng `config/overrides.csv` gán tay cho các địa danh chính. Khi có LLM API, thêm lớp LLM và so với nhãn tay đã có. Tuân thủ `robots.txt`, giới hạn 1–2 request/giây mỗi domain, User-Agent rõ ràng, cache tránh crawl lặp.

Tần suất cập nhật: thời tiết theo giờ, giờ mở cửa và giá vé hàng tháng, thông tin lịch sử gần như không đổi.

### Giới hạn pháp lý

- Không crawl Google Maps, TripAdvisor, Foody — vi phạm điều khoản sử dụng. Dùng API chính thức.
- Review lưu vào DB phải ẩn danh: bỏ tên và ảnh đại diện người viết (Nghị định 13/2023/NĐ-CP).
- Nội dung từ blog, báo du lịch chỉ tóm tắt, không lưu và đăng lại nguyên văn.
- Ghi `source_url` cho mọi bản ghi lấy từ ngoài; Wikipedia là CC BY-SA, Wikimedia Commons ghi nguồn theo giấy phép.

### Schema

Các bảng cốt lõi: `places`, `place_chunks` (RAG, `VECTOR(1024)`), `place_images` (nhận diện, `VECTOR(768)`), `reviews`, `weather_cache` (khóa chính `lat_grid, lon_grid, forecast_time`).

Các trường lái thuật toán, cần có cho mọi place: `location GEOGRAPHY(POINT)`, `opening_hours JSONB`, `avg_visit_minutes`, `indoor_ratio` (0 = ngoài trời, 1 = trong nhà), `weather_sensitivity JSONB` (`rain`, `heat`, `wind`), `best_time_of_day`, `unsafe_conditions`. Thiếu các trường này thì bộ lập lịch không chạy đúng — gán nhãn chúng là việc của giai đoạn dữ liệu, không để sau. Thiết kế chi tiết tầng dữ liệu: [docs/superpowers/specs/2026-09-22-data-pipeline-hue-design.md](docs/superpowers/specs/2026-09-22-data-pipeline-hue-design.md).

## Đánh giá

Mỗi tính năng phải có chỉ số và baseline, thiết kế trước khi code:

| Thành phần | Chỉ số | Baseline so sánh |
|---|---|---|
| Nhận diện địa danh | Top-1 / Top-5 trên tập ảnh test tự chụp | LLM thuần |
| Truy hồi RAG (hybrid: bge-m3 + BM25 giữ dấu/bỏ dấu/bigram, RRF; loại category không phải điểm đến) | Hit@k, MRR, nDCG@5, Precision@5, AnswerHit@5 trên `eval/rag_queries.yml` (`python -m rag.benchmark`) | Dense, BM25, random |
| Hỏi đáp | Tỷ lệ thông tin sai / bịa | Không có RAG |
| Lập lịch | Tỷ lệ lịch khả thi (đúng giờ mở cửa, trong ngân sách, di chuyển kịp), `planner/feasibility.py` | LLM tự lập lịch (`python -m planner.experiment llm-prompt` / `llm-score`) |
| Thời tiết | Sinh lịch trên dự báo lịch sử (Open-Meteo Previous Runs), đối chiếu thời tiết thực tế (ERA5): số giờ ngoài trời khi mưa, số hoạt động bị hủy do bão (`python -m planner.experiment run`) | Bộ lập lịch không có tính năng thời tiết; oracle biết trước thời tiết làm cận trên |
| Chỉ đường & giao thông | Thời gian di chuyển thực tế, tỷ lệ né tránh thành công điểm tắc đường/ngập lụt | chỉ đường static (OSRM/Goong không tính thời tiết & tắc đường real-time) |
| Review | Precision / Recall / F1 theo khía cạnh | — |

Khi thêm một tính năng, thêm cả cách đo nó. Tính năng không đo được không phải đóng góp.

## Tiến độ

12 tuần: dữ liệu (1–3), nhận diện + RAG (4–5), lập lịch + thời tiết (6–8), chỉ đường (9), thực nghiệm và báo cáo (10–12). Đóng băng tính năng sau tuần 8; từ đó chỉ sửa lỗi, chạy thực nghiệm, viết báo cáo.

- **Đã hoàn thành:**
  - Data Pipeline (Crawl, normalize, PostGIS/pgvector storage).
  - Hybrid RAG (BAAI/bge-m3 + BM25 multi/fold + RRF fusion + benchmark engine).
  - Bộ lập lịch thích ứng thời tiết (`planner/`, thiết kế: docs/superpowers/specs/2026-09-27-weather-aware-schedule-optimizer-design.md): VRPTW nhiều ngày, bản sao theo khung giờ, ràng buộc cứng theo `unsafe_conditions`, tầng thời tiết theo khoảng cách tới ngày đi, thực nghiệm trên thời tiết lịch sử (data/qa/planner_experiment_2026-09-27.md).
- **Baseline LLM tự lập lịch** (`python -m planner.experiment llm-collect` rồi `run --llm-dir`, lịch cache ở `eval/llm_schedules/`): `gpt-5.4-mini` đã chạy đủ 1 ngày (`none`, `medium`) và 2 ngày (`none`); 2 ngày `medium` mới 17/122 lịch (hết credit OpenAI). Kết quả: data/qa/planner_experiment_2026-09-29_osrm_llm_*.md. Baseline trên model local/Gemini chạy cùng lệnh với `--base-url`.
- **Còn thiếu:** giờ mở cửa (9/208 điểm tham quan) và giá vé (1 điểm) trong DB; kiểm định chuyến 2 ngày. Kết quả chính dùng thời gian đi OSRM (data/qa/planner_experiment_2026-09-27_osrm.md): 1 ngày giảm 14% giờ ngoài trời khi mưa (oracle 38%), 2 ngày giảm 11% (oracle 35%); với thời gian đi ước lượng là 38% (oracle 59%). Mức giảm nhạy với việc lịch baseline tình cờ trùng giờ mưa: nên thêm baseline lấy trung bình nhiều thứ tự ngẫu nhiên để số liệu ổn định hơn.

