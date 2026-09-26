-- Ghi lại model đã sinh embedding của mỗi chunk. Không có cột này thì đổi
-- EMBEDDING_MODEL rồi chạy `embed` (không --force) sẽ để lại vector của hai
-- model lẫn trong cùng bảng, và độ tương đồng giữa chúng vô nghĩa. Retriever
-- chỉ tìm trên các chunk có embedding_model trùng với model đang dùng cho
-- query; `embed` sinh lại mọi chunk có embedding_model khác model hiện tại.
--
-- Các embedding có sẵn trước migration này không rõ model, nên để NULL và
-- sẽ được sinh lại ở lượt `embed` kế tiếp.
ALTER TABLE place_chunks ADD COLUMN embedding_model TEXT;
