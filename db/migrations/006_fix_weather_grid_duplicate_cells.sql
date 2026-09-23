-- Migration 005 đổi lat_grid/lon_grid từ REAL sang DOUBLE PRECISION bằng
-- ALTER COLUMN ... TYPE, tức CAST ngầm float4 -> float8. Phép cast đó mở
-- rộng đúng giá trị NHỊ PHÂN đã lưu (vd. 16.3 dạng float32 mở rộng thành
-- 16.299999237060547 dạng float64), KHÔNG phải giá trị thập phân mà float32
-- đó đại diện. Các dòng cũ (trước migration) kẹt ở giá trị xấp xỉ này,
-- trong khi mọi lượt `weather` chạy sau migration ghi toạ độ Python float
-- sạch (16.3) qua grid_key() — mỗi ô lưới tồn tại hai bản, một xấp xỉ một
-- sạch, và một truy vấn join theo khoá đã làm tròn chỉ thấy được một nửa.
--
-- Fix: làm tròn lat_grid/lon_grid về đúng 1 chữ số thập phân trong cả hai
-- bảng — dữ liệu vẫn tốt, chỉ khoá bị sai. Trước khi làm tròn (phép làm
-- tròn có thể khiến hai dòng trùng khoá chính), xoá bớt dòng trùng:
--   * weather_cache: giữ dòng có fetched_at MỚI HƠN cho mỗi
--     (lat_grid làm tròn, lon_grid làm tròn, forecast_time).
--   * climate_normals: hai dòng trùng ô sau khi làm tròn có số liệu giống
--     hệt nhau (cùng nguồn Open-Meteo, cùng tháng) — giữ dòng nào cũng được.
--
-- An toàn khi chạy trên DB đã sạch (không có dòng trùng): DELETE không xoá
-- gì (không có cặp nào thoả JOIN), UPDATE làm tròn một giá trị đã tròn ra
-- chính nó — idempotent.

DELETE FROM weather_cache w
USING weather_cache w2
WHERE round(w.lat_grid::numeric, 1) = round(w2.lat_grid::numeric, 1)
  AND round(w.lon_grid::numeric, 1) = round(w2.lon_grid::numeric, 1)
  AND w.forecast_time = w2.forecast_time
  AND (
    w.fetched_at < w2.fetched_at
    OR (w.fetched_at = w2.fetched_at AND w.ctid < w2.ctid)
  );

UPDATE weather_cache
SET lat_grid = round(lat_grid::numeric, 1)::double precision,
    lon_grid = round(lon_grid::numeric, 1)::double precision;

DELETE FROM climate_normals c
USING climate_normals c2
WHERE round(c.lat_grid::numeric, 1) = round(c2.lat_grid::numeric, 1)
  AND round(c.lon_grid::numeric, 1) = round(c2.lon_grid::numeric, 1)
  AND c.month = c2.month
  AND c.ctid < c2.ctid;

UPDATE climate_normals
SET lat_grid = round(lat_grid::numeric, 1)::double precision,
    lon_grid = round(lon_grid::numeric, 1)::double precision;
