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
| Điều phối agent | Tool-calling trực tiếp qua API nhà cung cấp LLM |
| CSDL | PostgreSQL + PostGIS + pgvector |
| Cache | Redis (dựng khi có backend API) |
| Lưu file | `data/raw/` trên đĩa; chuyển sang MinIO khi có backend |
| Tối ưu lịch | Google OR-Tools |
| Thời tiết | Open-Meteo |
| Chỉ đường | Nhúng Google Maps API (Routes / Distance Matrix API) kết hợp OSRM / Goong làm fallback; dẫn đường thực tế deep link sang app bản đồ |

Các dịch vụ mặc định dùng API miễn phí: Wikidata, Wikipedia, Wikivoyage, OSM Overpass, Wikimedia Commons, Open-Meteo. Sử dụng **Google Maps API** cho riêng tính năng chỉ đường và ma trận giao thông/tắc đường real-time. Không dùng Google Places / Foursquare / LLM API thương mại khác để tối ưu chi phí.

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
3. Vision LLM dự phòng — LLM đoán sai nhiều với địa danh ít nổi tiếng.

## Thời tiết trong bộ tối ưu

Độ chi tiết theo khoảng cách tới ngày đi: 0–3 ngày dùng dự báo theo giờ và xếp lịch theo khung giờ; 4–10 ngày dùng dự báo theo ngày và chỉ quyết định ngoài trời / trong nhà; trên 10 ngày dùng dữ liệu khí hậu nhiều năm và chỉ cảnh báo xu hướng.

Hai loại ràng buộc:

- **Cứng:** cảnh báo bão loại bỏ đi thuyền, ra đảo, leo núi; mưa lớn loại điểm ngoài trời không an toàn. Dùng `unsafe_conditions` của place.
- **Mềm:** mỗi cặp (địa điểm, khung giờ) có điểm phù hợp thời tiết, tính từ `weather_sensitivity` và `indoor_ratio`, đưa vào hàm mục tiêu.

Cache dự báo theo lưới tọa độ làm tròn ~0.1 độ, không gọi API cho từng địa điểm.

## Tối ưu chỉ đường dựa trên thời tiết và độ tắc đường (Google Maps API)

Nhúng Google Maps API (Routes API / Distance Matrix API) để tính toán tuyến đường và thời gian di chuyển thực tế, kết hợp giữa tình trạng giao thông thời gian thực và yếu tố thời tiết:

1. **Dữ liệu giao thông thời gian thực (Real-time Traffic):**
   - Trích xuất `duration_in_traffic` và mức độ ùn tắc từ Google Maps API theo đúng khung giờ dự kiến di chuyển trong ngày (bao gồm giờ cao điểm).
   - Cập nhật ma trận thời gian di chuyển (Travel Time Matrix) làm đầu vào chính xác cho thuật toán tối ưu lịch trình (OR-Tools VRP) thay vì dùng khoảng cách địa lý đơn thuần.

2. **Tối ưu chỉ đường thích ứng Thời tiết (Weather-aware Routing):**
   - **Hệ số phạt thời gian di chuyển (Travel Time Penalty Factor):** Khi Open-Meteo dự báo mưa lớn, ngập lụt, sương mù hoặc tầm nhìn kém, thời gian di chuyển thực tế được nhân thêm hệ số điều chỉnh (ví dụ: $1.2\times - 1.5\times$) để phản ánh tốc độ lưu thông giảm.
   - **Né tránh tuyến đường rủi ro (Safe Routing):** Tự động phát hiện và loại bỏ/giảm ưu tiên các tuyến đường dễ ngập lụt, đường ven sông Hương, hoặc ngõ ngách hẹp khi có cảnh báo mưa lớn/bão.
   - **Gợi ý phương tiện & lộ trình thay thế:** Đề xuất lộ trình an toàn hơn (ví dụ: chọn đường lớn ít ngập thay vì tuyến ngắn nhất qua ngõ) hoặc điều chỉnh khuyến nghị phương tiện di chuyển (ô tô/taxi thay cho xe máy).

3. **Tích hợp thuật toán & Trải nghiệm người dùng:**
   - **Hàm mục tiêu tổng hợp:** $\text{Cost}(i, j) = \text{Google\_Traffic\_Duration}(i, j) \times \text{Weather\_Penalty} + \text{Risk\_Penalty}$.
   - **Deep link & Dẫn đường:** Hiển thị trực quan tuyến đường trên bản đồ giao diện ứng dụng, đồng thời cung cấp deep link mở ứng dụng Google Maps để người dùng sử dụng tính năng dẫn đường turn-by-turn.

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
| Lập lịch | Tỷ lệ lịch khả thi (đúng giờ mở cửa, trong ngân sách, di chuyển kịp) | LLM tự lập lịch |
| Thời tiết | Sinh lịch trên thời tiết lịch sử, đối chiếu thời tiết thực tế: số giờ ngoài trời khi mưa, số hoạt động bị hủy do bão | Bộ lập lịch không có tính năng thời tiết |
| Chỉ đường & giao thông | Thời gian di chuyển thực tế, tỷ lệ né tránh thành công điểm tắc đường/ngập lụt | chỉ đường static (OSRM/Goong không tính thời tiết & tắc đường real-time) |
| Review | Precision / Recall / F1 theo khía cạnh | — |

Khi thêm một tính năng, thêm cả cách đo nó. Tính năng không đo được không phải đóng góp.

## Tiến độ

12 tuần: dữ liệu (1–3), nhận diện + RAG (4–5), lập lịch + thời tiết (6–8), chỉ đường (9), thực nghiệm và báo cáo (10–12). Đóng băng tính năng sau tuần 8; từ đó chỉ sửa lỗi, chạy thực nghiệm, viết báo cáo.
