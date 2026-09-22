# Thiết kế: Pipeline dữ liệu Huế

Ngày: 2026-09-22
Trạng thái: đã duyệt, chờ lập implementation plan

## 1. Mục tiêu

Dựng toàn bộ tầng dữ liệu cho Travel Agent, phạm vi thành phố Huế, chạy được thật từ máy trống: `docker compose up -d` rồi `python -m pipeline.cli all` cho ra một PostgreSQL chứa khoảng 100 địa danh và 300–600 quán ăn, kèm báo cáo chất lượng dữ liệu.

Đây là giai đoạn tuần 1–3 trong lộ trình ở `CLAUDE.md`. Tầng dữ liệu này phục vụ đóng góp chính của đồ án — tối ưu lịch trình theo thời tiết — nên các trường mà bộ lập lịch đọc (`location`, `opening_hours`, `avg_visit_minutes`, `indoor_ratio`, `weather_sensitivity`, `unsafe_conditions`) được xử lý kỹ hơn phần còn lại.

## 2. Ràng buộc

- **Chỉ nguồn miễn phí, không API key**: Wikidata, Wikipedia, Wikivoyage, OpenStreetMap Overpass, Wikimedia Commons, Open-Meteo. Không Google Places, không Foursquare, không LLM API.
- Hệ quả: bước "LLM chuẩn hóa thành trường có cấu trúc" trong `CLAUDE.md` được thay bằng rule-based cộng gán nhãn tay. Khi có LLM API, bước LLM thêm vào như một lớp gán nhãn nữa và so được với nhãn tay.
- Môi trường: Windows, Docker 27.5.1, Docker Compose v2.32.4, Python 3.14.6.
- Python 3.14 còn mới nên tránh Scrapy. Dùng `httpx` cho HTTP, `selectolax` cho HTML, `trafilatura` cho trích nội dung chính, `psycopg` (v3) cho DB.

## 3. Kiến trúc

ETL tuần tự, ba tầng, mỗi tầng ghi ra trước khi tầng sau đọc:

```
mạng → data/raw/<source>/<date>/   (nguyên bản, không sửa)
     → normalize (thuần Python, không I/O mạng)
     → load (upsert vào PostgreSQL)
     → qa (báo cáo + mẫu kiểm duyệt)
```

Không dùng Prefect hay Airflow: pipeline chỉ chạy vài lần trong vòng đời đồ án, chi phí setup và debug trên Windows lớn hơn giá trị nhận được. Cấu trúc module giữ nguyên nếu sau này bọc bằng orchestrator.

Không dùng dbt: parse `opening_hours` của OSM và JSON lồng nhau bằng Python dễ hơn bằng SQL.

### Cấu trúc repo

```
docker-compose.yml
db/Dockerfile                 # postgis/postgis:17-3.5 + pgvector từ PGDG
db/migrations/001_*.sql       # chạy tuần tự
pipeline/
  ingest/    wikidata.py  wikipedia.py  osm.py  commons.py  weather.py
  normalize/ opening_hours.py  category.py  merge.py  weather_labels.py
  load/      upsert.py
  qa/        coverage.py
  cli.py
config/   city_hue.yml  categories.yml  weather_defaults.yml  overrides.csv
data/raw/  data/qa/
tests/
```

### Hạ tầng

Không image công khai nào có sẵn cả PostGIS lẫn pgvector. `db/Dockerfile` build từ `postgis/postgis:17-3.5` và cài `postgresql-17-pgvector` qua apt PGDG. Compose chỉ có một service `db`.

Redis và MinIO chưa dựng ở giai đoạn này: chưa có API để cache, raw lưu thẳng xuống `data/raw/`. Thêm khi dựng backend. Cập nhật lại `CLAUDE.md` cho khớp.

## 4. Schema

Giữ các bảng trong `CLAUDE.md`, thêm và sửa như sau.

**Bảng thêm**

| Bảng | Vai trò |
|---|---|
| `raw_documents(id, source, source_url, fetched_at, content_hash, storage_path, http_status)` | Truy vết nguồn. `content_hash` cho phép bỏ qua URL không đổi khi chạy lại |
| `place_external_ids(place_id, source, external_id)`, UNIQUE `(source, external_id)` | Khóa upsert idempotent. Một place có thể có cả `wikidata:Q123` và `osm:node/456` |
| `climate_normals(lat_grid, lon_grid, month, temp_avg, precip_mm_avg, rain_days, wind_avg)` | Dự báo trên 10 ngày dùng khí hậu nhiều năm, lấy một lần từ Open-Meteo Archive |

**Sửa `places`**

- `opening_hours_raw TEXT` giữ nguyên chuỗi OSM, bên cạnh `opening_hours JSONB` đã parse.
- `label_source TEXT` nhận `default` hoặc `manual`, cho biết nhóm trường thời tiết đã kiểm duyệt tay hay chưa.

**Index**: GIST trên `places.location`; HNSW trên `place_chunks.embedding` và `place_images.embedding`; UNIQUE trên `place_external_ids(source, external_id)`.

**`reviews` để trống ở giai đoạn này.** Không có nguồn review miễn phí hợp pháp gắn được vào địa điểm Huế. Dataset review tiếng Việt trên Kaggle hoặc Hugging Face chỉ dùng để train model sentiment, không map được `place_id`, nên nạp riêng khi làm phần phân tích review.

## 5. Ingest

Mỗi module ghi `data/raw/` trước, parse sau. Chạy lại không gọi mạng khi `content_hash` trùng, trừ khi truyền `--force`.

| Nguồn | Lấy gì | Cách gọi | Kỳ vọng |
|---|---|---|---|
| OSM Overpass | Ranh giới hành chính Huế, lưu polygon vào `config/city_hue.yml` | Query relation `admin_level=6` | 1 polygon |
| Wikidata | Địa danh có tọa độ trong bbox, `wdt:P31/wdt:P279*` thuộc di tích, chùa, bảo tàng, cung điện; kèm P18, P373, sitelink vi/en | Một query SPARQL | 80–150 địa danh |
| Wikipedia, Wikivoyage | Toàn văn bài theo sitelink, `action=query&prop=extracts` | API vi.wikipedia và en.wikivoyage | Nguồn cho `place_chunks` |
| OSM Overpass | POI trong polygon: `tourism`, `historic`, `amenity=restaurant\|cafe\|fast_food`; kèm `opening_hours`, `cuisine`, `website`, `phone` | Query thứ hai | 300–600 quán ăn, 100+ điểm tham quan |
| Wikimedia Commons | Tối đa 30 ảnh mỗi địa danh từ P18 và category, kèm license và tác giả từ `imageinfo.extmetadata` | API | Kho ảnh cho nhận diện |
| Open-Meteo | Forecast theo giờ; Archive 1991–2020 tổng hợp thành `climate_normals` theo tháng | 2 endpoint | Lưới ~0.1 độ phủ Huế |

**Giới hạn tốc độ**: Overpass mỗi lần một request, chờ xong mới gọi tiếp. Wikimedia yêu cầu User-Agent có email liên hệ, đọc từ `.env` chứ không hardcode. Các domain khác giới hạn 1–2 request mỗi giây.

**Gộp trùng Wikidata × OSM**: ghép khi khoảng cách dưới 150m **và** tên chuẩn hóa khớp (bỏ dấu, bỏ tiền tố "chùa", "đền", "lăng", "miếu"). Khớp một điều kiện thì ghi ra `data/qa/merge_review.csv` để xem tay, không tự ghép. Khi hai nguồn lệch nhau: Wikidata thắng ở tên và mô tả, OSM thắng ở `opening_hours` và `website`.

**Chuẩn hóa thay cho LLM**: `opening_hours` parse bằng parser riêng xử lý tập con phổ biến của cú pháp OSM (`Mo-Su 07:00-17:00`, `24/7`, `off`, nhiều khoảng cách nhau bằng `;`). Chuỗi ngoài tập con giữ ở `opening_hours_raw`, `opening_hours` để `NULL` — không đoán. Category map bằng `config/categories.yml`. Giá vé không có nguồn miễn phí có cấu trúc, nhập tay vào `config/overrides.csv` cho khoảng 20 di tích chính, phần còn lại để trống.

## 6. Gán nhãn thời tiết

Ba lớp, lớp sau đè lớp trước.

**Lớp 1 — mặc định theo category** (`config/weather_defaults.yml`):

```yaml
bao_tang:   { indoor_ratio: 0.95, avg_visit_minutes: 60,
              weather_sensitivity: { rain: 0.1, heat: 0.1, wind: 0.0 },
              best_time_of_day: [bat_ky], unsafe_conditions: [] }
lang_tam:   { indoor_ratio: 0.25, avg_visit_minutes: 75,
              weather_sensitivity: { rain: 0.8, heat: 0.7, wind: 0.2 },
              best_time_of_day: [sang_som, chieu_muon], unsafe_conditions: [mua_lon] }
di_thuyen_song_huong:
            { indoor_ratio: 0.4,  avg_visit_minutes: 90,
              weather_sensitivity: { rain: 0.7, heat: 0.4, wind: 0.9 },
              best_time_of_day: [hoang_hon], unsafe_conditions: [bao, mua_lon, gio_manh] }
```

**Lớp 2 — điều chỉnh theo tag OSM**: `building=yes` hoặc `indoor=yes` kéo `indoor_ratio` lên 0.9; `covered=yes` lên 0.6; `natural=*` hoặc `leisure=park` kéo xuống 0.05. Lớp này chỉ đụng `indoor_ratio`, giữ nguyên `unsafe_conditions`.

**Lớp 3 — `config/overrides.csv` gán tay**: khoảng 100 địa danh chính, xếp hạng theo việc có bài Wikipedia và số lượng tag OSM. Sửa CSV rồi chạy lại `load` là đủ, không phải sửa code. Mỗi dòng có cột `note` ghi lý do đè.

`label_source` ghi `manual` khi có dòng override, `default` khi không.

**Giá trị cho báo cáo**: lớp 1 và 2 là baseline rule-based, lớp 3 là nhãn người. So hai bên trên cùng 100 địa danh cho ra tỷ lệ rule đoán đúng `indoor_ratio` trong sai số ±0.2. Khi có LLM API, thêm lớp LLM và so với cùng tập nhãn người.

## 7. QA

`python -m pipeline.cli qa` sinh `data/qa/coverage_<date>.md`.

| Chỉ số | Ngưỡng fail |
|---|---|
| Place có tọa độ hợp lệ trong polygon Huế | < 98% |
| Di tích và bảo tàng có `opening_hours` parse được | < 60% |
| Place map được category, không rơi vào `khac` | < 90% |
| Địa danh có từ 5 ảnh Commons trở lên | < 70% |
| Địa danh chính có `label_source = manual` | < 100 địa danh |

Báo cáo in thêm số cặp còn chờ trong `merge_review.csv`, và xuất 20 bản ghi ngẫu nhiên ra `data/qa/sample_<date>.csv` để đối chiếu tay với nguồn. Đây là bước kiểm duyệt thủ công mà `CLAUDE.md` yêu cầu, và là số liệu đưa vào báo cáo đồ án.

## 8. Kiểm thử

Viết test trước phần code xử lý. Dùng pytest.

- Parser `opening_hours`: các chuỗi thật lấy từ OSM Huế, gồm cả chuỗi hỏng phải trả `None`.
- Chuẩn hóa tên và logic ghép trùng: hai điểm cách 100m cùng tên thì ghép; cách 100m khác tên thì vào `merge_review.csv`.
- Upsert idempotent: chạy `load` hai lần, số dòng không đổi.
- Gán nhãn thời tiết: lớp 2 và lớp 3 đè đúng thứ tự.
- Ingest: mock HTTP. Test không gọi mạng.

## 9. CLI

`python -m pipeline.cli <lệnh>`: `migrate`, `ingest --source <tên>`, `normalize`, `load`, `qa`, `all`.

`ingest` bỏ qua URL có `content_hash` trùng trừ khi có `--force`. `load` upsert theo `place_external_ids`. Mọi lệnh chạy lại được nhiều lần mà không sinh dữ liệu trùng.

## 10. Ngoài phạm vi

- Sinh embedding cho `place_chunks` và `place_images`: cần model local 1–2 GB, thuộc tính năng 1. Cột và index HNSW dựng sẵn, giá trị để `NULL`.
- Thu thập review gắn với `place_id`.
- Cron hoặc orchestrator cho cập nhật định kỳ. Chạy tay bằng CLI ở giai đoạn này.
- Redis, MinIO, backend API.
