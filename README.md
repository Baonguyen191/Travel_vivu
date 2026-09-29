# Travel Agent — tầng dữ liệu (Huế)

Pipeline nạp dữ liệu địa danh, quán ăn, khách sạn cho trợ lý du lịch Huế: thu
thập từ Wikidata, OpenStreetMap, Wikipedia, Wikimedia Commons và Open-Meteo,
chuẩn hoá thành các trường có cấu trúc (toạ độ, giờ mở cửa, độ nhạy thời
tiết...) và nạp vào PostgreSQL + PostGIS + pgvector. Phạm vi hiện tại: **chỉ
thành phố Huế**, xem `CLAUDE.md` để biết quyết định kiến trúc đầy đủ.

## Yêu cầu môi trường

- Docker Desktop (đang chạy) — chứa PostgreSQL 17 + PostGIS + pgvector.
- Python 3.14 trên Windows.
- Một virtualenv tại `.venv/` (project dùng `.venv/Scripts/python.exe`).

Cài dependency:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Nếu `psycopg[binary]` chưa có wheel sẵn cho bản Python bạn dùng (ví dụ
cp314 lúc còn mới), thử theo thứ tự:

1. `pip install "psycopg[c]"` — cần có `pg_config` (đi kèm PostgreSQL client)
   trong PATH.
2. Cài `psycopg` thuần (không extra) và trỏ `libpq` từ thư mục cài của Docker
   Desktop / PostgreSQL client vào PATH trước khi `pip install`.

Trong quá trình phát triển dự án này wheel `psycopg[binary]` cho Python 3.14
đã cài được trực tiếp qua `pip install -e ".[dev]"`, không cần các bước dự
phòng trên — ghi lại đây phòng khi môi trường khác gặp vướng.

## Thiết lập `CONTACT_EMAIL`

Mọi request ra ngoài (Wikidata, Wikipedia, Wikimedia Commons, Overpass,
Open-Meteo) đi qua `pipeline.http.Fetcher`, và `Fetcher` **raise lỗi ngay**
nếu biến môi trường `CONTACT_EMAIL` chưa được đặt — chính sách của Wikimedia
yêu cầu User-Agent phải có email liên hệ. Dự án **không tự nạp `.env`**, bạn
phải đặt biến môi trường thủ công trước khi chạy bất kỳ lệnh `ingest` hay
`all` nào:

```bash
# Git Bash / bash
export CONTACT_EMAIL=you@example.com
```

```powershell
# PowerShell
$env:CONTACT_EMAIL = "you@example.com"
```

Có thể sao `.env.example` thành `.env` để ghi nhớ giá trị (`DATABASE_URL`,
`TEST_DATABASE_URL`, `CONTACT_EMAIL`), nhưng vẫn phải tự export biến vào
shell trước khi chạy — file `.env` chỉ là ghi chú, không có cơ chế đọc tự
động.

Mọi lệnh CLI dưới đây phải chạy từ **thư mục gốc của repo** — config
(`config/*.yml`, `config/overrides.csv`) và dữ liệu (`data/...`) đều được
đọc/ghi theo đường dẫn tương đối với thư mục làm việc hiện tại, không phải
tương đối với vị trí file Python.

## Dựng database và chạy pipeline

```bash
docker compose up -d --build
python -m pipeline.cli all
```

Lệnh `all` chạy tuần tự theo `pipeline.cli.PIPELINE_ORDER`:

| Bước | Việc làm |
|---|---|
| `migrate` | Áp các migration SQL còn thiếu vào schema |
| `boundary` | Lấy ranh giới hành chính Huế từ Overpass, ghi `data/generated/city_hue_boundary.wkt`, cập nhật `config/city_hue.yml` |
| `wikidata` | Truy vấn SPARQL Wikidata trong bbox Huế, lưu bản thô vào `data/raw/wikidata/` |
| `osm` | Truy vấn Overpass lấy địa danh/quán ăn/khách sạn trong bbox, lưu `data/raw/osm/` |
| `load` | Gộp Wikidata + OSM theo rule-based (category, opening_hours, mặc định thời tiết theo category, `config/overrides.csv`) — tức chạy đúng một lượt bước `normalize` — rồi upsert kết quả vào bảng `places` (và bảng liên kết) |
| `wikipedia` | Với mỗi place đã có `id` trong DB, lấy tóm tắt Wikipedia, cắt đoạn (chunk) cho RAG (`place_chunks`) |
| `commons` | Với mỗi place đã có `id`, lấy ảnh tham chiếu từ Wikimedia Commons (`place_images`) |
| `weather` | Nạp dự báo giờ (Open-Meteo forecast) và khí hậu trung bình nhiều năm (Open-Meteo archive) theo lưới toạ độ ~0.1 độ, độc lập với place nên chạy sau cùng — phần **forecast luôn gọi lại API** (xem ghi chú dưới), phần archive dùng cache như các nguồn khác |
| `qa` | In báo cáo chất lượng dữ liệu (coverage, ngưỡng PASS/FAIL) |

Thứ tự này bắt buộc vì `wikipedia`/`commons` cần `places.id` (chỉ có sau
`load`), còn `boundary`/`wikidata`/`osm` phải chạy trước `load` vì chúng là
nguyên liệu đầu vào. `normalize` **không** nằm trong `all`: bản thân `load`
đã chạy đúng một lượt normalize rồi upsert, nên đưa cả hai vào `PIPELINE_ORDER`
sẽ tính lại toàn bộ bước gộp/nhãn hai lần cho cùng một dữ liệu. `normalize`
vẫn còn là lệnh đứng riêng — dùng khi chỉ muốn xem báo cáo gộp/cặp nghi trùng
mà chưa ghi gì vào DB.

Mỗi lệnh **an toàn để chạy lại nhiều lần**: `Fetcher` cache bản thô theo hash
request trong `data/raw/`, migration chỉ áp phần chưa chạy, `load` dùng
upsert (`ON CONFLICT DO UPDATE`), `weather_cache` khoá chính theo
`(lat_grid, lon_grid, forecast_time)` nên ghi lại không nhân bản dòng.

**Ngoại lệ: phần forecast của `weather` không dùng cache.** URL forecast
không có tham số ngày (chỉ toạ độ + "16 ngày kể từ hôm nay"), nên nó
byte-identical giữa các lần gọi — nếu dùng cache như mọi nguồn khác, `Fetcher`
sẽ trả mãi bản thô của lần fetch đầu tiên trong khi `fetched_at` vẫn ghi
`now()`, khiến dữ liệu trông "vừa mới lấy" nhưng thật ra đã cũ. Vì vậy
`pipeline.ingest.weather.run()` luôn gọi lại API cho phần forecast (bỏ qua
cache) mỗi khi chạy, kể cả không có `--force` — không cần và không nên
truyền `--force` chỉ để "làm mới" forecast. Phần archive (khí hậu 1991–2020)
là dữ liệu bất biến nên vẫn dùng cache như bình thường; `--force` cho
`weather` chỉ có tác dụng lên phần archive.

### Chạy lại một bước riêng lẻ

```bash
python -m pipeline.cli migrate
python -m pipeline.cli ingest --source wikidata   # hoặc: boundary, osm, wikipedia, commons, weather
python -m pipeline.cli normalize
python -m pipeline.cli load
python -m pipeline.cli qa
```

`ingest` nhận cờ `--force` để bỏ qua cache và gọi lại API dù đã có bản thô
(dùng khi nghi bản thô hỏng hoặc dữ liệu nguồn đã đổi):

```bash
python -m pipeline.cli ingest --source osm --force
```

## Số liệu thật sau khi chạy

Số liệu dưới đây lấy từ CSDL Huế đã tích luỹ qua các lần chạy `ingest` +
`normalize` + `load` + `wikipedia`/`commons`/`weather` (task 1–15), rồi xác
nhận lại bằng hai lần chạy `python -m pipeline.cli all` đầy đủ trên chính
CSDL đó (không rebuild từ đầu — xem mục "Chạy lại từ đầu" bên dưới về vì sao
và cách làm khi thật sự cần). Lần chạy thứ hai (sau khi sửa để `all` không
còn gọi `normalize` hai lần, xem log bên dưới) là idempotent hoàn toàn — mọi
bảng giữ nguyên số dòng:

| Bảng | Số dòng | Ghi chú |
|---|---|---|
| `places` | 906 | không đổi ở lần chạy `all` gần nhất (`load: thêm 0, cập nhật 903`) — 903 → 906 từng xảy ra ở một lần chạy trước đó do Overpass trả về khác đi vài phần tử qua mirror khác nhau giữa các lần truy vấn, không phải lỗi pipeline |
| `place_chunks` (RAG) | 1.574 | không đổi — wikipedia không ghi thêm chunk mới (đã đủ, idempotent) |
| `place_images` | 104 | không đổi — commons không ghi thêm ảnh mới (đã đủ, idempotent) |
| `weather_cache` | 6.144 | không đổi về số dòng ròng — mỗi ô lưới upsert lại 96 giờ dự báo, ghi đè cùng khoá chính |
| `climate_normals` | 192 | không đổi |
| `reviews` | 0 | chưa có nguồn ingest review (ngoài phạm vi 17 task hiện tại) |

Phân bố `places` theo `category`: `nha_hang` 305, `quan_ca_phe` 218,
`khach_san` 134, `di_tich` 86, `diem_tham_quan` 50, `khac` 41, `bao_tang` 17,
`lang_tam` 17, `cong_vien` 16, `chua` 13, `song` 9.

Log đầy đủ của lần chạy `all` gần nhất (2026-09-23, sau khi `normalize`
được bỏ khỏi `PIPELINE_ORDER` — chỉ còn in đúng một lượt "normalize: ..." vì
dòng đó là output của bước `load`, trên CSDL đã có sẵn dữ liệu, không phải
DB trống):

```
Đã chạy 0 migration: không có
boundary: 1 bản ghi
wikidata: 314 bản ghi
osm: đang thử endpoint https://overpass-api.de/api/interpreter (lượt 1/2)
osm: 687 bản ghi
normalize: bỏ qua 75 đơn vị hành chính
normalize: 903 địa điểm, 20 cặp chờ xem tay
load: thêm 0, cập nhật 903
wikipedia: 0 bản ghi
commons: 0 bản ghi
weather: 6336 bản ghi
# Báo cáo chất lượng dữ liệu — 2026-09-23
| Tọa độ hợp lệ trong lõi Huế | 100% | 98% | PASS |
| Category xác định được | 95% | 90% | PASS |
| Di tích, bảo tàng, lăng tẩm có opening_hours parse được | 8% | 5% | PASS |
| Place có liên kết Wikidata và có ít nhất một ảnh | 27% | 25% | PASS |
| Địa danh gán nhãn tay | 0 | 100 | FAIL |
| Địa danh có từ 5 ảnh trở lên | 6 | — | (thông tin) |
```

Lần chạy trước đó (còn `normalize` trong `PIPELINE_ORDER`) từng phải retry
Overpass qua 2 mirror trả `504` trước khi thành công ở mirror thứ ba — minh
hoạ đúng thực tế rate-limit mô tả ở mục dưới; lần chạy này mirror đầu tiên
đã thành công ngay. "Địa danh gán nhãn tay" FAIL ở 0/100 là kỳ vọng — chỉ
số này chỉ pass khi có người điền tay `config/overrides.csv` cho đủ 100
địa danh, việc đó chưa nằm trong phạm vi 17 task của kế hoạch dữ liệu.

## Chạy lại từ đầu (cold start) và giới hạn tốc độ API

Nếu bạn bắt đầu từ một CSDL trống (`docker compose down -v` rồi
`docker compose up -d --build`), `python -m pipeline.cli all` sẽ chạy toàn
bộ pipeline từ đầu. Vài điều cần biết:

- **Overpass (dùng cho `boundary` và `osm`) hay trả về `504` khi tải cao.**
  `pipeline.ingest.osm` và `pipeline.ingest.boundary` tự động thử lần lượt
  qua nhiều mirror công khai (overpass-api.de, overpass.kumi.systems,
  overpass.private.coffee...) và đợi giữa các lần thử — không cần can thiệp
  tay, chỉ cần kiên nhẫn (có thể mất vài phút nếu vài mirror đầu đều quá
  tải).
- **Open-Meteo (endpoint archive, dùng cho khí hậu nhiều năm) hay trả về
  `429`** khi nạp đủ 16 ô lưới liên tiếp. Khi gặp lỗi này, chạy lại:

  ```bash
  python -m pipeline.cli ingest --source weather
  ```

  Bản thô đã tải thành công được cache trong `data/raw/open_meteo/`, nên
  chạy lại chỉ tốn công gọi API cho các ô lưới còn thiếu — không tải lại
  những gì đã có. Lặp lại cho tới khi log không còn dòng "bỏ qua ... —
  lỗi HTTP 429" cho ô lưới nào (đủ 16/16 ô).
- Toàn bộ pipeline chạy trên DB trống ước tính mất **trên 30 phút** (phụ
  thuộc số lần phải đợi rate-limit), vì vậy không nên chạy `docker compose
  down -v` chỉ để "test cho chắc" — nếu chỉ cần xác nhận wiring, chạy
  `python -m pipeline.cli all` trên DB đang có sẵn và kiểm tra các bước báo
  không có (hoặc rất ít) dòng mới, như log ở mục trên.

## Gán nhãn tay và `config/overrides.csv`

Nhiều trường lái thuật toán lập lịch (`indoor_ratio`, `avg_visit_minutes`,
`best_time_of_day`, `unsafe_conditions`, giá vé, quy định trang phục)
không có sẵn đầy đủ từ OSM/Wikidata, đặc biệt với di tích, bảo tàng, lăng
tẩm. Sửa tay bằng cách thêm/sửa dòng trong `config/overrides.csv`:

```csv
place_key,indoor_ratio,avg_visit_minutes,best_time_of_day,unsafe_conditions,ticket_price_vnd,dress_code,note
wikidata:Q10769129,0.2,180,sang_som|chieu_muon,mua_lon,200000,,Đại Nội đi bộ nhiều sân lộ thiên
```

`place_key` là định danh nguồn (`wikidata:<QID>` hoặc `osm:<type>/<id>`,
tuỳ place có liên kết nào). Sau khi sửa file, chạy lại:

```bash
python -m pipeline.cli load
```

`load` tự chạy lại `normalize` rồi upsert — override sẽ được áp lại cho các
place tương ứng mà không cần ingest lại từ nguồn ngoài.

## File sinh ra và vị trí

Toàn bộ thư mục `data/` bị `.gitignore` (sinh lại được bằng pipeline, không
commit):

- `data/raw/` — bản thô đã fetch (JSON Overpass/Wikidata, HTML Wikipedia,
  ảnh/metadata Commons, JSON Open-Meteo), theo thư mục con từng nguồn.
- `data/staged/` — kết quả trung gian trước khi chuẩn hoá.
- `data/qa/` — báo cáo chất lượng dữ liệu (`coverage_<ngày>.md`), file mẫu
  kiểm duyệt (`sample_<ngày>.csv`), danh sách cặp nghi trùng
  (`merge_review_<ngày>.csv` — ghi theo ngày để không đè lên một review tay
  đang làm dở của lần chạy trước).
- `data/generated/` — file sinh ra dùng lại cho lần chạy sau (ví dụ
  `city_hue_boundary.wkt`).

## Chatbot demo

```powershell
pip install -e ".[embed,demo,llm]"
streamlit run demo/app.py
```

Cần database đang chạy và đã `python -m pipeline embed`.

- LLM (agent tool-calling và nhận diện ảnh bằng vision LLM) qua API tương thích OpenAI. Mặc định
  Ollama local, miễn phí:
  ```powershell
  winget install Ollama.Ollama
  setx OLLAMA_CONTEXT_LENGTH 8192     # rồi khởi động lại Ollama
  ollama pull qwen3.5:9b              # thiếu VRAM thì qwen3.5:4b
  ```
  và trong `.env`: `OPENAI_BASE_URL=http://localhost:11434/v1`, `OPENAI_MODEL=qwen3.5:9b`. Dùng
  OpenAI thì bỏ `OPENAI_BASE_URL`, đặt `OPENAI_API_KEY`. Không cấu hình gì thì demo chạy chế độ
  luật (nhận ý định bằng từ khoá). So sánh model: `python scripts/llm_smoke_test.py --n 10` (model theo `OPENAI_MODEL`).
- Thời gian đi và tuyến đường thật, miễn phí: dựng OSRM một lần rồi chạy service:
  ```powershell
  python scripts/setup_osrm.py                    # tải bản đồ OSM Việt Nam, xử lý ~20 phút, cần ~8 GB RAM Docker
  docker compose --profile routing up -d osrm     # http://localhost:5100
  ```
  Dữ liệu nằm trong Docker volume `travel_osrm`, không nằm trong repo (repo ở OneDrive).
- `GOOGLE_MAPS_API_KEY`: thời gian đi có giao thông từ Google Routes. Project phải bật Routes API
  và billing. Không có OSRM lẫn Google thì thời gian đi là ước lượng. Kịch bản trình bày:
[docs/demo/kich-ban-demo.md](docs/demo/kich-ban-demo.md).

## Chạy test

```bash
.venv/Scripts/python.exe -m pytest -q
```

Test dùng một **database riêng** (`travel_test`, cấu hình qua
`TEST_DATABASE_URL` trong `.env.example`) — không bao giờ chạm vào dữ liệu
dev/thật ở database `travel`. Database test được tạo và migrate tự động ở
lần chạy test đầu tiên (fixture `_test_database` trong `tests/conftest.py`).
Một số test cần Docker đang chạy (đánh dấu `integration` trong
`pyproject.toml`).

## Ghi chú lược đồ dữ liệu

- **`opening_hours` (JSONB trên `places`)**: mỗi ngày trong tuần ánh xạ tới
  một danh sách khoảng `["HH:MM", "HH:MM"]`. `"24:00"` nghĩa là **hết ngày /
  nửa đêm** — cùng quy ước OSM/Overpass đã dùng cho `24/7` — không phải
  `"00:00"` của ngày hôm sau. Một khoảng qua nửa đêm trong chuỗi OSM gốc (vd.
  `Mo-Su 17:00-01:30`) được `pipeline.normalize.opening_hours` TÁCH thành hai
  đoạn khi parse: `["17:00", "24:00"]` ghi vào ngày đó, `["00:00", "01:30"]`
  ghi vào ngày kế tiếp — nhờ vậy `end - start` trên một đoạn không bao giờ âm.
- **`climate_normals.rain_days`**: **tỷ lệ** (0.0–1.0) số ngày trong tháng có
  lượng mưa ≥ 1mm, tính trên 30 năm dữ liệu (1991–2020) — không phải số ngày
  tuyệt đối.
- **`climate_normals.precip_mm_avg`**: lượng mưa **trung bình một ngày**
  trong tháng (mm/ngày), không phải tổng lượng mưa cả tháng.

## Bản quyền / ghi nguồn dữ liệu

- Nội dung tóm tắt từ Wikipedia (`place_chunks`) theo giấy phép **CC
  BY-SA** — khi hiển thị lại cho người dùng cuối, phải ghi nguồn Wikipedia
  và điều khoản CC BY-SA.
- Ảnh từ Wikimedia Commons (`place_images`) ghi nguồn theo giấy phép cụ thể
  của từng ảnh (không phải tất cả đều CC BY-SA — Commons có nhiều giấy phép
  khác nhau theo file).
- Mọi bản ghi lấy từ ngoài đều có `source_url`.
- Review (khi có nguồn trong tương lai) phải được ẩn danh trước khi lưu vào
  DB — bỏ tên và ảnh đại diện người viết, theo Nghị định 13/2023/NĐ-CP.
- Không crawl Google Maps, TripAdvisor, Foody (vi phạm điều khoản dịch vụ).

Xem `CLAUDE.md` để biết đầy đủ các quyết định kiến trúc, ranh giới LLM/thuật
toán, và lộ trình 12 tuần của đồ án.
