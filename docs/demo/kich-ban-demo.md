# Kịch bản demo: Trợ lý du lịch Huế

Thời lượng: khoảng 12 phút. Người trình bày điều khiển chatbot Streamlit trên
máy của mình và chiếu màn hình.

Mục tiêu: cho hội đồng thấy (1) LLM chỉ làm phần ngôn ngữ, mọi thông tin đúng/sai
đến từ công cụ và CSDL, nên câu trả lời có nguồn và không bịa; (2) đóng góp chính
là **lập lịch thích ứng thời tiết**, có số đo trên thời tiết thật chứ không chỉ
chạy được.

## 1. Chuẩn bị (làm trước buổi demo ít nhất 30 phút)

Chạy trong PowerShell, tại thư mục gốc repo, đã kích hoạt `.venv`:

```powershell
docker compose up -d db                     # PostgreSQL + PostGIS + pgvector
pip install -e ".[embed,demo,llm]"          # nếu máy mới
python -m pipeline download-model           # bge-m3 đã có trong cache thì chỉ mất ~10 giây
docker compose --profile routing up -d osrm # thời gian đi theo đường thật (dựng một lần: python scripts/setup_osrm.py)
streamlit run demo/app.py
```

LLM mặc định chạy local qua Ollama: trong `.env` ở gốc repo (demo tự đọc) có
`OPENAI_BASE_URL=http://localhost:11434/v1` và `OPENAI_MODEL=qwen3.5:9b`. Mở Ollama trước khi demo
và hỏi thử một câu để nạp model vào GPU (lần đầu vài chục giây). Dùng OpenAI thì bỏ
`OPENAI_BASE_URL` và đặt `OPENAI_API_KEY`.

Tải sẵn ảnh cho cảnh 5 (ảnh Wikimedia Commons, CC BY 4.0, tác giả Chainwit.), lưu
thành `truong-tien.jpg` ở máy, không cần đưa vào repo:
https://upload.wikimedia.org/wikipedia/commons/9/9f/2024-07_C%E1%BA%A7u_Tr%C6%B0%E1%BB%9Dng_Ti%E1%BB%81n_-_Truong_Tien_Bridge_-_Hu%E1%BA%BF_-_img_09.jpg
Nếu có ảnh tự chụp ở Huế thì dùng ảnh đó sẽ thuyết phục hơn.

Kiểm tra trước khi lên:

- [ ] Thanh bên: "Bộ xử lý ngôn ngữ" đang ở **LLM**, dưới có dòng `Model qwen3.5:9b (local)`.
- [ ] Thanh bên: "Nguồn thời gian đi" đang ở **OSRM**, có dòng "OSRM: đường thật, miễn phí".
- [ ] Trang không báo lỗi PostgreSQL; hỏi thử một câu (lần đầu ~15 giây để nạp bge-m3).
- [ ] Bấm lần lượt các câu hỏi mẫu ở thanh bên, câu nào cũng ra kết quả.
- [ ] Ollama đang chạy (`ollama ps` thấy `qwen3.5:9b`). Máy có mạng: cần cho Open-Meteo (LLM và OSRM chạy trên máy).
- [ ] Bấm "Xoá hội thoại" trước khi bắt đầu.

Thanh bên để mặc định: xe máy, 4 điểm/ngày, bật so sánh với lịch không tính thời tiết.

## 2. Các cảnh

Mỗi câu trả lời có mục **"Công cụ đã gọi"**. Mở mục này ở cảnh đầu để hội đồng
thấy LLM gọi công cụ nào với tham số gì; những cảnh sau chỉ mở khi được hỏi.

### Cảnh 1 — Hỏi đáp có nguồn (1,5 phút)

Gõ: **Chùa Thiên Mụ được xây dựng năm nào?**

Kết quả mong đợi: "khởi lập năm 1601 (Tân Sửu), dưới thời chúa Nguyễn Hoàng" kèm
link Wikipedia. Công cụ đã gọi: `search_knowledge`.

Nói:
- LLM không trả lời từ trí nhớ: nó gọi `search_knowledge` (RAG hybrid bge-m3 +
  BM25) rồi chỉ diễn đạt lại đoạn trả về, kèm nguồn.
- Mở "Các đoạn tri thức đã truy hồi" để cho thấy đoạn gốc.

Gõ tiếp: **Kể cho mình về tháp Eiffel**

Kết quả mong đợi: bot nói không tìm thấy trong kho tri thức (chỉ hỗ trợ Huế),
không kể từ trí nhớ.

### Cảnh 2 — Thông tin hay thay đổi: đọc từ CSDL, không đoán (1 phút)

Gõ: **Đại Nội mở cửa mấy giờ, giá vé bao nhiêu?**

Kết quả mong đợi: giờ mở cửa thứ hai–thứ bảy 07:30–17:30, chủ nhật 06:30–17:30;
giá vé 200.000 đ; kèm ngày cập nhật. Công cụ: `get_place_facts`.

Gõ: **Lăng Khải Định mở cửa mấy giờ?**

Kết quả mong đợi: "CSDL hiện chưa có giờ mở cửa", không đưa ra giờ nào.

Nói: giờ mở cửa và giá vé đổi thường xuyên, LLM rất dễ bịa (nó "biết" một con số
từ dữ liệu huấn luyện cũ). Prompt và công cụ buộc nó đọc CSDL; thiếu thì nói thiếu.

### Cảnh 3 — Lập lịch cho ngày mai, rồi hỏi nối tiếp (2 phút)

Gõ: **Mai mình muốn đi Đại Nội, chùa Thiên Mụ, lăng Tự Đức, lăng Khải Định và bảo tàng cổ vật**

Kết quả mong đợi:
- Công cụ: `plan_trip` với ngày mai, 5 địa điểm theo đúng thứ tự người dùng nêu.
- Hai tab "Thích ứng thời tiết" / "Không tính thời tiết": bảng giờ, di chuyển,
  ghi chú thời tiết, link Google Maps từng chặng, bản đồ lộ trình.
- Tối đa 4 điểm/ngày nên một điểm vào "Không xếp được" kèm lý do.

Gõ tiếp: **thêm lăng Minh Mạng vào được không?**

Kết quả mong đợi: bot gọi lại `plan_trip` với 6 điểm và giải thích điểm nào
không xếp vừa.

Nói:
- LLM chỉ trích ngày, số ngày, địa điểm, phương tiện (bước 1) và giải thích kết
  quả (bước 3). Chọn điểm, chọn giờ, sắp thứ tự là thuật toán VRP trên OR-Tools;
  LLM không được sửa lịch.
- Đây là ranh giới quan trọng nhất của đồ án: LLM tự viết lịch sẽ tạo lịch phi
  thực tế (đến khi đã đóng cửa, di chuyển không kịp).
- Nếu mai trời khô, hai lịch gần giống nhau: đúng, vì không có gì để tránh.

### Cảnh 4 — Đóng góp chính: ngày mưa thật trong quá khứ (3 phút)

Gõ (hoặc bấm câu mẫu cuối): **Lập lịch 1 ngày 27/10/2024: Đại Nội, chùa Thiên Mụ, lăng Tự Đức, lăng Khải Định, chợ Đông Ba, bảo tàng cổ vật**

Hệ thống nhận ra ngày đã qua: lập lịch trên dự báo **phát hành trước 1 ngày**
(người dùng khi đó thật sự thấy dự báo này), rồi chấm bằng **thời tiết đã xảy ra**
(ERA5).

Kết quả mong đợi (dữ liệu lưu trữ nên ổn định giữa các lần chạy):

| | Giờ ngoài trời khi mưa (thực tế) | Lượt đến lúc mưa lớn |
|---|---|---|
| Thích ứng thời tiết | 1,96 giờ | 1 |
| Không tính thời tiết | 4,65 giờ | 3 |

(Số với thời gian đi OSRM. Nếu thanh bên để "Ước lượng", lượt đến lúc mưa lớn của
lịch không tính thời tiết là 4.)

- Biểu đồ so dự báo với thực tế theo giờ.
- Lịch thích ứng đưa chợ Đông Ba và bảo tàng vào buổi sáng, dời Lăng Tự Đức và
  chùa Thiên Mụ sang cuối chiều. Hoàng thành bị bỏ dù ưu tiên cao nhất: tham quan
  mất 3 giờ, không có khoảng 3 giờ nào dự báo hết mưa lớn (mở "Không xếp được" và
  "Điều chỉnh theo thời tiết").
- Lịch không tính thời tiết đi các lăng ngoài trời từ 8h, giữa lúc mưa.

Nói:
- Đây là chỉ số CLAUDE.md đặt ra cho phần thời tiết, so với bộ lập lịch không có thời tiết.
- Trên toàn mùa mưa 2024–2025 (244 chuyến 1 ngày, thời gian đi OSRM): giảm 14% giờ
  ngoài trời khi mưa, khoảng tin cậy 95% không chứa 0; lượt đến lúc mưa lớn từ 14
  xuống 5; số điểm đi được không đổi; 100% lịch khả thi
  (data/qa/planner_experiment_2026-09-27_osrm.md).
- Biến thể "oracle" (biết trước thời tiết thật) giảm 38%; khoảng 14% → 38% là phần
  mất do dự báo trước 1 ngày sai giờ mưa, không do thuật toán.
- Nếu được hỏi: với thời gian đi ước lượng từ khoảng cách, mức giảm là 38% (oracle
  59%). Con số phụ thuộc lịch baseline tình cờ rơi vào giờ mưa nhiều hay ít; OSRM
  sát thực tế hơn nên dùng làm số chính.

### Cảnh 5 — Nhận diện ảnh (1,5 phút)

Bấm biểu tượng đính kèm trong ô chat, chọn `truong-tien.jpg`, gửi không cần gõ chữ.

Kết quả mong đợi: "Đây có vẻ là **Cầu Trường Tiền** (GPS + vision LLM, độ tin cậy
~99%)" kèm lý do nhìn thấy trong ảnh, rồi phần giới thiệu ngắn có nguồn.

Nói:
- Ba tầng từ rẻ đến đắt: GPS trong ảnh → embedding ảnh (chưa làm) → vision LLM.
- Phát hiện khi thử: GPS là vị trí **người chụp**. Ảnh này chụp từ bờ sông, GPS
  cách tượng đài Phan Bội Châu 72 m; nếu tin GPS thì nhận sai. Vì vậy GPS chỉ
  thu hẹp ứng viên, vision LLM chọn trong số đó.
- Vision LLM chỉ được chọn tên có trong CSDL; không chắc (dưới 50%) thì nói không
  nhận ra. Thử trên 4 ảnh Commons: đúng 3, sai Lăng Tự Đức; cần bộ ảnh test tự chụp
  để đo Top-1/Top-5 như CLAUDE.md yêu cầu.

## 3. Nếu có sự cố

| Sự cố | Xử lý |
|---|---|
| Trang báo không kết nối PostgreSQL | `docker compose up -d db`, tải lại trang |
| LLM lỗi (Ollama chưa chạy, OpenAI hết hạn mức, mất mạng) | App tự trả lời bằng chế độ luật và ghi "LLM lỗi"; hoặc chuyển thanh bên sang **Luật**. Chế độ luật chạy được cảnh 1, 2, 4 bằng các câu hỏi mẫu, không nhận diện ảnh |
| Mất mạng hoàn toàn | Cảnh 1, 2 (chế độ luật) vẫn chạy. Cảnh 3–4 cần Open-Meteo: mở data/qa/planner_experiment_2026-09-27_osrm.md và trình bày số liệu |
| OSRM không chạy | Thanh bên báo; chọn "Ước lượng" hoặc chạy `docker compose --profile routing up -d osrm` |
| Câu hỏi đầu chậm | Model bge-m3 đang nạp (~15 giây) |
| LLM hiểu sai ý | Hỏi lại cụ thể hơn, hoặc bấm câu hỏi mẫu |
| Kết quả cảnh 4 khác bảng trên một chút | Solver giới hạn 3 giây, máy chậm có thể ra lời giải khác; chạy lại |

Chi phí: mỗi câu hỏi tốn vài nghìn token `gpt-5.4-mini`; cả buổi demo chỉ vài chục
câu. Thanh bên hiện số token đã dùng trong phiên.

## 4. Câu hỏi hội đồng có thể hỏi

- **LLM có tự bịa được không?** Prompt cấm trả lời từ trí nhớ, và mọi dữ kiện đều
  đến từ công cụ (xem "Công cụ đã gọi"). Chỉ số đo tỷ lệ bịa (có RAG so với không
  RAG) là việc tiếp theo trong kế hoạch đánh giá.
- **Sao không để LLM lập lịch luôn?** LLM tự lập lịch là baseline của đồ án: nó tạo
  lịch phi thực tế. Công cụ `llm-prompt`/`llm-score` trong `planner/experiment.py`
  dùng để đo điều đó.
- **Thời gian đi lấy ở đâu?** OSRM tự host trên bản đồ OpenStreetMap: quãng đường theo
  đường thật, thời gian lấy max(thời gian OSRM, quãng đường / 25 km/h) vì tốc độ mặc
  định của OSRM là đường thông thoáng. Không có giao thông thời gian thực; Google Routes
  có giao thông nhưng cần bật billing.
- **Giờ mở cửa thiếu nhiều?** Mới 9/208 điểm tham quan có; điểm thiếu coi là mở suốt
  ngày và lịch ghi "nên kiểm tra trước".
- **"Thời tiết thật" có độc lập với dự báo không?** Có: ERA5 là tái phân tích.
  Archive mặc định của Open-Meteo trả về đúng dự báo cho ngày gần, nên đã chỉ định
  `models=era5` để tránh đánh giá vòng lặp.
