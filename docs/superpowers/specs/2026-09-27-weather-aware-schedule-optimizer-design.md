# Thiết kế bộ lập lịch thích ứng thời tiết (`planner/`)

Đây là đóng góp chính của đồ án (CLAUDE.md, hướng A). Tài liệu mô tả đúng bản
cài đặt hiện tại; khi đổi thiết kế, sửa tài liệu cùng lúc với code.

## 1. Ranh giới LLM và thuật toán

- LLM: trích ràng buộc thành `UserConstraint` (bước 1), diễn giải `ScheduleResult`
  thành văn bản (bước 3).
- Thuật toán (bước 2): chọn điểm, chọn ngày, chọn giờ, sắp thứ tự. LLM không
  sửa kết quả này.

## 2. Thành phần

| File | Vai trò |
|---|---|
| `models.py` | Kiểu dữ liệu, giữ nguyên định dạng bảng `places` |
| `places.py` | Nạp `Place` từ DB (theo id hoặc QID Wikidata) |
| `rules.py` | Một định nghĩa duy nhất cho "mưa", "mưa lớn", "bão", "biển động", chi phí thời tiết, hệ số đi |
| `weather.py` | Nguồn thời tiết theo ô lưới 0.1°, phân tầng theo khoảng cách tới ngày đi |
| `matrix.py` | Ma trận thời gian đi: ước lượng (chim bay × 1.3) hoặc Google Routes theo mốc giờ |
| `routes.py` | Gắn tuyến Google (polyline) cho các chặng đã chốt, né vùng ngập khi mưa lớn |
| `service.py`, `__main__.py` | `plan_trip` cho agent; `python -m planner` để chạy thử |
| `solver.py` | VRPTW nhiều ngày trên OR-Tools |
| `feasibility.py` | Kiểm tra khả thi cho mọi lịch (của solver hay của LLM) |
| `evaluator.py` | Chấm lịch trên thời tiết quan trắc |
| `llm_baseline.py` | Prompt và đọc JSON cho baseline "LLM tự lập lịch" |
| `experiment.py` | Thực nghiệm trên thời tiết lịch sử, CLI |

## 3. Thời tiết

Tầng theo khoảng cách từ hôm nay tới ngày đi:

| Khoảng cách | Tầng | Solver dùng thế nào |
|---|---|---|
| 0–3 ngày | `hourly` | Chọn giờ: mỗi giờ một bản sao, ràng buộc cứng xét từng giờ |
| 4–10 ngày | `daily` | Chọn ngày: bản ghi gộp cả ngày (tổng mưa, gió lớn nhất) |
| > 10 ngày | `climate` | Không ràng buộc; ghi cảnh báo xu hướng từ `climate_normals` |
| không có dữ liệu | `none` | Không ràng buộc; ghi rõ lịch chưa tính thời tiết |

Dự báo đọc từ `weather_cache` nếu mới hơn 6 giờ, không thì gọi Open-Meteo.
Lỗi mạng không bao giờ được thay bằng thời tiết giả định.

Quy tắc (`rules.py`), dùng chung cho solver và evaluator:

- Mưa: ≥ 0.5 mm/h (theo ngày: ≥ 5 mm). Mưa lớn: ≥ 7.5 mm/h (theo ngày: ≥ 50 mm)
  hoặc mã WMO 65/82. Bão: gió ≥ 50 km/h hoặc mã 95/96/99. Biển động: gió ≥ 40 km/h
  (xấp xỉ khi chưa có dữ liệu sóng).
- Ràng buộc cứng: chỉ cấm khi hiện tượng nằm trong `unsafe_conditions` của điểm
  (`bao`, `mua_lon`, `song_lon`).
- Chi phí mềm (0..1) = (1 − indoor_ratio) × (s_rain·mưa + s_heat·nóng + s_wind·gió),
  với s_* là `weather_sensitivity` của điểm.

## 4. Mô hình tối ưu

- Mỗi ngày là một xe, xuất phát và kết thúc ở khách sạn, trong khung giờ của ngày
  (ngày đầu và ngày cuối cắt theo giờ đến/đi của người dùng).
- Mỗi điểm được nhân thành bản sao theo (ngày, khoảng mở cửa, khung giờ). Khung
  giờ đến bảo đảm tham quan xong trước giờ đóng cửa. Bản sao trùng giờ nguy hiểm
  không được tạo. Disjunction chọn nhiều nhất một bản sao cho mỗi điểm; bỏ điểm
  chịu phạt `visit_value × priority` (10 000).
- Chi phí cung vào bản sao = phút đi (Google có giao thông tại mốc giờ gần nhất,
  hoặc ước lượng) × hệ số thời tiết + phạt vùng ngập (30 phút, chỉ khi mưa lớn/bão
  và đoạn đi qua vùng trong `config/flood_zones.yml`) + `weather_weight` (120) ×
  chi phí thời tiết × số giờ tham quan.
- Dimension đếm số điểm mỗi ngày; dimension ngân sách vé, với ràng buộc tổng qua
  mọi ngày ≤ ngân sách.
- Tìm kiếm: PARALLEL_CHEAPEST_INSERTION + Guided Local Search, giới hạn thời gian.

Xấp xỉ đã biết: hệ số thời tiết cho thời gian đi lấy theo giờ của điểm đến (OR-Tools
không cho thời gian cung phụ thuộc giờ khởi hành); chi phí thời tiết của một bản
sao tính tại giữa khung giờ.

## 5. Đánh giá

Theo CLAUDE.md: "sinh lịch trên thời tiết lịch sử, đối chiếu thời tiết thực tế".

- Lập lịch trên dự báo phát hành trước `lead_days` ngày (Open-Meteo Previous
  Runs), chấm trên ERA5 (`models=era5`). Không dùng archive mặc định: với ngày
  gần, nó trả về đúng dự báo, làm phép đánh giá vòng lặp.
- Biến thể: `aware` (dự báo), `baseline` (tắt thời tiết, cùng solver), `oracle`
  (biết trước thời tiết thật, cận trên).
- Chỉ số: giờ ngoài trời khi mưa, số lượt đến điểm đang có hiện tượng nguy hiểm,
  số điểm đi được, phút di chuyển, tỷ lệ lịch khả thi. So cặp bằng bootstrap 95%.
- Baseline LLM: `llm-prompt` sinh prompt với cùng dữ liệu; `llm-score` chấm JSON
  LLM trả về bằng cùng bộ kiểm tra khả thi và cùng thời tiết quan trắc.

Chạy: `python -m planner.experiment run` (cấu hình `eval/planner_experiment.yml`).

## 6. Giới hạn dữ liệu hiện tại

- Chỉ 9/208 điểm tham quan có `opening_hours`; điểm chưa có giờ được coi là mở
  suốt ngày và lịch ghi chú "nên kiểm tra trước".
- Chỉ 1 điểm có giá vé; ngân sách tính các điểm chưa rõ giá là 0 đồng.
- Thời gian đi mặc định là ước lượng; Google Routes tính phí theo phần tử ma trận
  (n² × số mốc giờ × số ngày) nên không dùng cho thực nghiệm hàng loạt.
- Chưa có dữ liệu vùng ngập; phạt và né tuyến ngập chỉ hoạt động khi
  `config/flood_zones.yml` có vùng kèm nguồn.
