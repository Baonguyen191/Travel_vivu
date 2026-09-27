-- Ghi lại phép ánh xạ nào tạo ra mỗi dòng place_images, để báo cáo (task 15)
-- tách được số ảnh theo từng nguồn: ảnh đại diện P18 của Wikidata, danh mục
-- Commons đã gắn với địa danh, hay tìm kiếm theo tên (nhiễu cao nhất).
--
-- 'name_search' đã bị GỠ khỏi pipeline (task 13, ruling R31 trong ledger)
-- vì tỷ lệ khớp sai landmark quá cao cho tập ảnh tham chiếu nhận diện.
-- CHECK vẫn cho phép giá trị này để không phải viết migration đổi ràng buộc
-- nếu sau này có một import tuyển chọn tay (không tự động) muốn dùng lại
-- nhãn route này cho ảnh đã được người kiểm tra xác nhận đúng landmark.
ALTER TABLE place_images ADD COLUMN route TEXT
  CHECK (route IN ('p18', 'category', 'name_search'));
