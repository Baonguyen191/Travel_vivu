-- lat_grid/lon_grid ở weather_cache và climate_normals được khai báo REAL
-- (float32) từ 002_core.sql. So sánh trực tiếp với literal float8 trong SQL
-- (vd. WHERE lat_grid = 16.4) không bao giờ khớp vì Postgres không ép ngầm
-- float8 -> float4 khi so sánh bằng, chỉ có thể ép float4 -> float8 — nghĩa
-- là phía literal luôn thắng và phép so sánh luôn sai lệch bit. Đổi sang
-- DOUBLE PRECISION để khớp đúng giá trị Python float đã dùng để ghi lưới.
ALTER TABLE weather_cache
  ALTER COLUMN lat_grid TYPE DOUBLE PRECISION,
  ALTER COLUMN lon_grid TYPE DOUBLE PRECISION;

ALTER TABLE climate_normals
  ALTER COLUMN lat_grid TYPE DOUBLE PRECISION,
  ALTER COLUMN lon_grid TYPE DOUBLE PRECISION;
