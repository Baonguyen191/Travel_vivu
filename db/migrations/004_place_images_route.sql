-- Ghi lại phép ánh xạ nào tạo ra mỗi dòng place_images, để báo cáo (task 15)
-- tách được số ảnh theo từng nguồn: ảnh đại diện P18 của Wikidata, danh mục
-- Commons đã gắn với địa danh, hay tìm kiếm theo tên (nhiễu cao nhất).
ALTER TABLE place_images ADD COLUMN route TEXT
  CHECK (route IN ('p18', 'category', 'name_search'));
