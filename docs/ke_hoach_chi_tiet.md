# Kế hoạch chi tiết — Travel Agent Huế (bản nặng AI)

*Cập nhật 17/10/2026. Dựa trên `thiet_ke_lai_ai.md`, cộng thêm các quyết định và kết quả review dữ liệu ngày 16–17/10.*

---

## 1. Đồ án làm gì (đọc trong 1 phút)

Trợ lý du lịch Huế có 3 khả năng chính:

1. **Lập lịch tham quan có tính thời tiết.** Người dùng nói "tôi ở Huế 2 ngày, thích di tích, sợ mưa", hệ thống trả về lịch: mấy giờ đi đâu, tránh điểm ngoài trời lúc mưa.
2. **Nhận diện địa danh từ ảnh.** Gửi ảnh, hệ thống trả lời "đây là Lăng Khải Định", hoặc "không xác định" nếu ảnh không phải địa danh nào đã biết.
3. **Hỏi đáp về địa điểm.** Thông tin lấy từ Wikipedia/DB (RAG), không để AI tự bịa.

**Điểm mới so với thiết kế cũ:** trước đây việc xếp lịch do thuật toán OR-Tools làm, AI chỉ đọc hiểu câu hỏi. Bây giờ **một mô hình ngôn ngữ nhỏ (2–4 tỷ tham số) được huấn luyện để tự xếp lịch**. OR-Tools không mất đi mà đổi vai:

- làm **thầy**: giải hàng nghìn bài toán mẫu để mô hình học theo;
- làm **thước đo**: lời giải tốt nhất để so sánh;
- làm **lưới an toàn**: khi mô hình xếp lịch sai thì OR-Tools sửa hoặc thay.

**Câu đóng góp sẽ viết trong báo cáo** (điền số khi có kết quả):

> Huấn luyện LLM nhỏ (≤4B) lập lịch du lịch thích ứng thời tiết bằng học tăng cường với phần thưởng kiểm chứng bằng chương trình, chưng cất từ OR-Tools. Model đạt X% lịch khả thi (chưa huấn luyện: Y%), cách mục tiêu tối ưu của OR-Tools Z%. Kết hợp dự báo mưa đã hiệu chỉnh, khoảng cách tới oracle về số giờ ngoài trời khi mưa thu hẹp W%.

**Câu hỏi nghiên cứu:** mô hình nhỏ học được ràng buộc cứng (giờ mở cửa, thời gian di chuyển, an toàn khi bão) đến mức nào, và học tăng cường có tốt hơn chỉ học bắt chước không? Nếu không tốt hơn, đó vẫn là một kết quả có số liệu để báo cáo.

---

## 2. Các phần của hệ thống

| Phần | Nói đơn giản | Vì sao cần | Ưu tiên |
|---|---|---|---|
| **M1. Planner AI** | Mô hình học xếp lịch: trước học chép lời giải OR-Tools (SFT), sau đó tự thử và được chấm điểm (GRPO) | Đóng góp chính của đồ án | Bắt buộc (~40% công sức) |
| **M2. Hiệu chỉnh dự báo mưa** | Học từ lịch sử "dự báo nói gì, thực tế ra sao" để dự báo mưa ở Huế chuẩn hơn | Hiện lịch tránh được 14% giờ dầm mưa, nếu biết trước thời tiết thật thì tránh được 38%. Phần chênh là do dự báo sai | Nên làm (~15%) |
| **M3. Nhận diện ảnh** | Dạy mô hình ảnh phân biệt các địa danh Huế; biết nói "không biết"; dùng thêm GPS trong ảnh | Tính năng thứ hai, có số liệu riêng | Bắt buộc (~25%) |
| **Triển khai** | Nén mô hình (GGUF) để chạy trên laptop bằng Ollama, đo nén làm giảm chất lượng bao nhiêu | Demo chạy được trên máy 8 GB VRAM | Nên làm (~10%) |
| ~~M4. Fine-tune tìm kiếm~~ | ~~Dạy lại bộ tìm đoạn văn cho RAG~~ | **Đã cắt**: RAG hiện đạt Hit@5 = 0.97, gần như không còn chỗ cải thiện để đo | Bỏ |

**Luồng chạy khi người dùng hỏi lịch:**

```
Yêu cầu tiếng Việt
   │
   ▼
Planner AI sinh N lịch ──► Bộ kiểm tra khả thi (chương trình) ──► có lịch đạt? ── có ──► trả lịch tốt nhất
                                                                       │
                                                                     không
                                                                       ▼
                                                        OR-Tools sửa hoặc giải lại ──► trả lịch
Dự báo Open-Meteo ──► M2 hiệu chỉnh ──► xác suất mưa theo giờ ──► dùng cho cả Planner AI và OR-Tools
```

Nguyên tắc không đổi: **AI được đề xuất, chương trình quyết định đúng/sai.** Giờ mở cửa và giá vé luôn lấy từ DB.

---

## 3. Đã chốt những gì

| Quyết định | Lý do |
|---|---|
| **Viết lại code từ đầu** trên nhánh `fresh-start`; code nhánh `main` chỉ dùng để tham khảo | Bạn chọn ngày 16/10 |
| Giữ toàn bộ dữ liệu đã tải trong `data/raw/` và `data/staged/`, không tải lại | Tiết kiệm thời gian, tái lập được kết quả |
| **Cắt M4** ngay từ đầu | Không còn chỗ cải thiện để chứng minh |
| Demo bằng **Streamlit** một trang, không làm lại web React | Viết lại tốn thời gian; React chỉ làm nếu tuần 7 còn dư |
| M3 giới hạn **30–40 địa danh** có đủ ảnh | Hiện chỉ 47 địa danh có từ 5 ảnh trở lên |
| Tập test ảnh lấy từ **Flickr + Mapillary + ảnh nhờ người chụp** | Bạn không ở Huế |
| M2 chỉ dùng **LightGBM**; mô hình Transformer là phần thêm nếu dư thời gian | Rẻ, đủ mạnh |
| Train trên **Kaggle** (GPU miễn phí); laptop chỉ chạy suy luận, fine-tune ảnh và LoRA 2B | Laptop 8 GB VRAM không đủ cho GRPO 4B |

---

## 3a. Cách tổ chức: chia module, làm xong từng module rồi ghép nối

Dự án chia thành 9 module độc lập (core, data, weather, planner_core, planner_llm, recognition, rag, app, deploy),
mỗi module có giao diện `api.py` cố định, test, CLI và notebook đánh giá riêng; ghép nối ở module `app/` làm sau cùng.
Chi tiết giao diện, điều kiện hoàn thành và thứ tự: **`docs/modules.md`**.

## 3b. Quy định chung (áp dụng cho mọi phần)

**Quy định 1 — Mọi thứ cần kiểm định chất lượng phải có benchmark và notebook.**
Một phần chỉ được coi là xong khi có file `.ipynb` chạy được và in ra số liệu, để bạn tự kiểm tra. Notebook gồm:

- số liệu **trong lúc train/fine-tune**: loss, chỉ số trên dev theo từng epoch/step, vẽ thành đường cong (log ra file để notebook đọc lại);
- số liệu **cuối cùng trên tập test**, so với baseline, kèm khoảng tin cậy 95% khi so sánh;
- bảng lỗi mẫu (ví dụ 20 ca sai) để xem tận mắt.

| Phần | Notebook | Chỉ số bắt buộc |
|---|---|---|
| Bộ kiểm tra khả thi | `notebooks/eval_feasibility.ipynb` | Precision / recall / F1 khi phát hiện từng loại vi phạm, đo trên lịch đã gán nhãn tay và lịch LLM cũ |
| M1 Planner (SFT, GRPO) | `notebooks/train_planner_sft.ipynb`, `train_planner_grpo.ipynb`, `eval_planner.ipynb` | Loss/reward theo step; % JSON hợp lệ; % lịch khả thi; precision / recall / F1 bám ràng buộc người dùng; chênh lệch so với OR-Tools; giờ dầm mưa; phân loại vi phạm; độ trễ p50/p95; tỷ lệ lịch rỗng |
| M2 Dự báo mưa | `notebooks/train_weather.ipynb`, `eval_weather.ipynb` | Brier, AUC, precision / recall / F1 tại ngưỡng mưa, reliability diagram, theo số ngày dự báo trước; % khoảng cách tới oracle thu hẹp |
| M3 Nhận diện ảnh | `notebooks/train_vision.ipynb`, `eval_vision.ipynb` | Loss + Top-1 dev theo epoch; Top-1/Top-5; precision / recall / F1 từng lớp + macro; confusion matrix; AUROC, FPR@95 cho ảnh lạ; ablation ảnh / GPS / kết hợp |
| RAG | `notebooks/eval_rag.ipynb` | Hit@k, MRR, nDCG@5, Recall@k; **TTFT** (thời gian tới token đầu tiên), tổng độ trễ p50/p95; AnswerHit |
| Triển khai GGUF | `notebooks/eval_quantization.ipynb` | % lịch khả thi, F1 ràng buộc, TTFT, tokens/giây, VRAM theo từng mức nén |

**Quy định 2 — Chốt mô hình với bạn trước khi dùng.**
Mọi mô hình (LLM, mô hình ảnh, embedding, reranker, cả mô hình ML cổ điển) chỉ là **đề xuất** cho tới khi bạn chốt. Khi tới bước cần mô hình, mình đưa 2–3 lựa chọn kèm so sánh (VRAM trên RTX 4060 8 GB, giấy phép, chất lượng dự kiến, tốc độ) và một khuyến nghị, rồi chờ bạn chọn.

| Thời điểm chốt | Cần chốt | Đề xuất hiện tại (chưa chốt) |
|---|---|---|
| Tuần 2 | LLM baseline chưa huấn luyện | `qwen3.5:4b`, `qwen3.5:9b` (Ollama), Gemini Flash |
| Tuần 2 | Mô hình hiệu chỉnh mưa | LightGBM |
| Tuần 3 | LLM để fine-tune planner | Qwen3.5-2B, Qwen3.5-4B |
| Tuần 3 | Mô hình sinh câu yêu cầu tổng hợp | Chưa đề xuất |
| Tuần 5 | Backbone nhận diện ảnh + hàm loss | SigLIP-2 hoặc CLIP ViT-B, ArcFace |
| Tuần 6 | LLM điều phối agent | `qwen3.5:9b` local |
| ✅ 18/10 | Retriever RAG | **Đã chốt: hybrid bge-m3 + BM25 (RRF).** Benchmark: `data/qa/rag/2026-10-18`, `notebooks/eval_rag.ipynb` |
| ✅ 18/10 | LLM sinh câu trả lời RAG | **Đã chốt: `qwen3.5:4b`** (TTFT p50 3.3 s, 36 token/s) |

---

## 4. Dữ liệu hiện có — đủ gì, thiếu gì

| Loại dữ liệu | Đang có | Đánh giá | Việc phải làm |
|---|---|---|---|
| Địa điểm | 315 mục Wikidata + 688 mục OSM (982 sau khi bỏ trùng) | Đủ số lượng | — |
| Nhãn tay cho địa điểm (tỷ lệ trong nhà, thời gian tham quan, điều kiện nguy hiểm) | **1 nhãn** | **Thiếu nghiêm trọng.** Mọi địa điểm đang dùng giá trị mặc định theo loại, trong khi phần thưởng huấn luyện và chỉ số "giờ dầm mưa" dựa trên chính các trường này | Gán tay 50 địa điểm chính (tuần 1) |
| Giờ mở cửa, giá vé có nguồn | 18 địa điểm (`config/place_facts.csv`) | Ít. 4 điểm chưa có giờ. Giờ hè/đông khác nhau 30 phút nhưng chưa được tính | Bổ sung giờ theo mùa vào DB |
| Thời tiết lịch sử | Dự báo trước 1 ngày + thời tiết thực (ERA5), Sep–Dec 2024 và 2025 | Đủ cho thực nghiệm cũ, **không đủ cho M2** | Tải dự báo trước 1–10 ngày, 01/2024 → 09/2026 |
| Thực nghiệm lập lịch cũ | 366 chuyến, 10 địa điểm cố định | Quá hẹp để train, nhưng tốt làm một phần tập test | Dùng lại làm test để so với số cũ |
| Baseline AI chưa huấn luyện | `qwen3.5:9b`: 51.6% lịch khả thi (1 ngày), 23.8% (2 ngày) | **Đã có**, dù thiết kế ghi "đang thiếu" | Chạy lại trên bộ test mới |
| Ảnh địa danh | Commons: 47 địa danh ≥5 ảnh. GLDv2: 224 ảnh, 7 địa danh Huế (cũng từ Commons) | Thiếu | Crawl Commons sâu hơn + Flickr + Mapillary (tuần 3) |
| Zalo Landmark dataset | Link tải đã chết, chưa tìm được bản sao | Không dựa vào | Script nhập đã sẵn (`scripts/zalo_landmark.py`) nếu tìm được file |
| Câu hỏi RAG | 34 câu, chưa câu nào được kiểm duyệt | Không dùng được trong báo cáo | Kiểm duyệt nếu còn thời gian |

**Đã sửa (16/10):** công cụ gán nhãn cũ dùng mã Wikidata bịa (9/10 mã sai), nên nhãn gán xong sẽ bị bỏ qua mà không báo lỗi. Công cụ mới đọc 982 địa điểm thật. Chạy bằng:

```
python -m streamlit run scripts/labeler_app.py
```

---

## 5. Chi tiết từng phần

### M1 — Planner AI (phần chính)

**Đầu vào:** yêu cầu tiếng Việt; tối đa 30 địa điểm ứng viên kèm giờ mở cửa, thời gian tham quan, tỷ lệ trong nhà, điều kiện nguy hiểm; bảng thời gian đi giữa các điểm (OSRM); dự báo thời tiết theo giờ.
**Đầu ra:** lịch dạng JSON `[{day, place_id, start, end}]` và một câu giải thích.

**Các bước:**

1. **Bộ kiểm tra khả thi** (`planner/feasibility.py`). Kiểm tra một lịch có hợp lệ không: đến nơi khi đang mở cửa, kịp di chuyển giữa các điểm, nằm trong khung giờ đi chơi trong ngày, không trùng điểm, không đi điểm nguy hiểm khi bão/mưa lớn, không quá số điểm tối đa. Đây là "trọng tài" cho toàn bộ dự án, nên làm đầu tiên và có test đầy đủ.
2. **OR-Tools solver.** Giải bài toán tối ưu, cho ra lịch tốt nhất. Chỉ dùng làm thầy và lưới an toàn.
3. **Bộ sinh bài toán.** Tự tạo ~11.000 bài toán: chọn ngẫu nhiên 8–30 địa điểm Huế thật, một ngày thật trong 2024–2026 (kèm dự báo thật của ngày đó), số ngày đi, giờ xuất phát, sở thích. OR-Tools giải từng bài để tạo đáp án mẫu.
4. **Chia dữ liệu theo ngày.** Ngày dùng để test (ví dụ tháng 10–11/2025) không bao giờ xuất hiện trong dữ liệu train, để tránh "học tủ".
5. **SFT (học bắt chước).** Dạy Qwen3.5-2B (trên laptop) và Qwen3.5-4B (trên Kaggle) sinh đúng đáp án của OR-Tools. Dùng QLoRA để vừa bộ nhớ.
6. **GRPO (học tăng cường).** Mô hình tự sinh nhiều lịch cho cùng một bài, bộ kiểm tra chấm điểm, mô hình học từ lịch được điểm cao. Điểm thưởng gồm:
   - JSON hợp lệ (bắt buộc, sai là 0 điểm);
   - tỷ lệ ràng buộc đạt;
   - chất lượng lịch so với OR-Tools;
   - trừ điểm theo số giờ ngoài trời khi mưa;
   - trừ nặng nếu vi phạm an toàn;
   - **cộng điểm theo số điểm được thăm**, để chặn mẹo "lịch rỗng thì không bao giờ sai".
7. **Chạy thật có kiểm tra.** Sinh N lịch, chọn lịch hợp lệ tốt nhất; không có lịch nào hợp lệ thì OR-Tools sửa.

**So với:** chọn ngẫu nhiên; Qwen 4B/9B chưa huấn luyện; Gemini Flash; OR-Tools (mức trần).
**Đo bằng:** % JSON hợp lệ, % lịch khả thi, chênh lệch so với OR-Tools, số giờ dầm mưa (chấm bằng thời tiết thật ERA5), thời gian phản hồi.
**Kiểm tra tổng quát hóa:** nhiều ứng viên hơn lúc train (20 → 30), mùa chưa thấy, chuyến dài ngày hơn.
**Xong khi:** GRPO ≥ SFT > chưa huấn luyện về % khả thi, có khoảng tin cậy 95%.

### M2 — Hiệu chỉnh dự báo mưa

**Ý tưởng:** dự báo Open-Meteo cho Huế có sai số lặp lại (ví dụ hay báo thiếu mưa chiều mùa mưa). Học bảng "dự báo nói X, thực tế là Y" từ 2–3 năm dữ liệu thì sửa được một phần sai số này.

**Các bước:**
1. Tải dự báo đã phát hành trước 1–10 ngày (Open-Meteo Previous Runs) và thời tiết thực (ERA5) cho các ô lưới Huế, 01/2024 → 09/2026.
2. Ghép thành bảng: mỗi dòng là một giờ ở một ô lưới, gồm các cột dự báo, số ngày dự báo trước, giờ, tháng → nhãn "có mưa thật không".
3. Train LightGBM ra xác suất mưa theo giờ.
4. Giữ riêng một giai đoạn để test (không train trên đó).
5. Đưa xác suất mưa mới vào planner, chạy lại thực nghiệm cũ.

**Đo bằng:** Brier score, AUC, biểu đồ độ tin cậy, so với dự báo gốc và trung bình khí hậu. Chỉ số cuối cùng: khoảng cách 14% → 38% được thu hẹp bao nhiêu.
**Lưu ý trong báo cáo:** ERA5 vừa là nhãn train vừa là thước chấm, và có độ phân giải thô (~25 km). Nếu tìm được số liệu trạm Huế/Phú Bài (NOAA ISD, Meteostat) thì dùng làm thước chấm phụ.
**Xong khi:** Brier tốt hơn dự báo gốc trên giai đoạn test.

### M3 — Nhận diện địa danh từ ảnh

**Dữ liệu:**

| Tập | Nguồn | Ghi chú |
|---|---|---|
| Train | Wikimedia Commons (crawl đệ quy theo category của từng địa danh); thêm Zalo nếu tìm được | Có giấy phép CC và tên tác giả |
| Dev | Commons, nhóm tác giả không có trong train | Dùng chọn ngưỡng |
| Test trong phân phối | Flickr (ảnh CC có vị trí), Mapillary, ảnh nhờ người chụp | Tác giả không trùng train; lọc ảnh gần trùng |
| Test ngoài phân phối (OOD) | Ảnh phố Huế không có địa danh (Mapillary), địa danh nơi khác trông giống (Tử Cấm Thành Bắc Kinh, cung điện Hàn Quốc… từ GLDv2), ảnh trong nhà | Kiểm tra khả năng nói "không biết" |

**Không crawl** Google Images, ảnh Google Maps, Facebook, Instagram, TripAdvisor, Foody: các trang này cấm scrape và ảnh không có giấy phép.

**Các bước:**
1. Chạy chế độ **chỉ đếm** trên các nguồn để biết mỗi địa danh có bao nhiêu ảnh, rồi chốt 30–40 địa danh.
2. Tải ảnh, lưu manifest (link, giấy phép, tác giả, tọa độ, mã hash).
3. Lọc ảnh gần trùng giữa các tập, chia train/test theo tác giả.
4. Fine-tune SigLIP-2 hoặc CLIP ViT-B với ArcFace để ảnh cùng địa danh có vector gần nhau. Vector 768 chiều lưu trong pgvector.
5. Phát hiện ảnh lạ: so 4 cách (MSP, energy, Mahalanobis, kNN), chọn ngưỡng trên tập dev.
6. Kết hợp GPS: xác suất cuối = xác suất từ ảnh × xác suất từ khoảng cách GPS. Lưu ý GPS là chỗ người đứng chụp, có thể cách địa danh vài chục mét.

**So với:** chỉ dùng GPS; CLIP chưa fine-tune; Qwen vision chưa fine-tune.
**Đo bằng:** Top-1/Top-5, AUROC và FPR@95 cho ảnh lạ, bảng so "chỉ ảnh / chỉ GPS / kết hợp".
**Xong khi:** Top-1 vượt CLIP chưa fine-tune, có ngưỡng "không xác định" chọn trên dev.

### Triển khai

1. Gộp LoRA vào mô hình, xuất GGUF ở 3 mức nén (Q4_K_M, Q5_K_M, Q8_0), chạy bằng Ollama.
2. Đo % lịch khả thi và độ trễ ở từng mức nén.
3. Agent hỏi đáp dùng `qwen3.5` local; planner đã huấn luyện chạy song song.
4. Kiểm tra sớm (tuần 5) xem llama.cpp đã hỗ trợ kiến trúc Qwen3.5 chưa. Nếu chưa, chạy planner bằng transformers thay cho Ollama.

---

## 6. Lịch 8 tuần

"Code" là phần lập trình (mình làm cùng bạn). "Bạn" là việc ngoài code, chỉ bạn làm được.

### Tuần 1 · 19–25/10 · Nền móng

- **Code**
  - Khung repo mới, Docker Postgres + PostGIS + pgvector.
  - Pipeline nạp dữ liệu từ `data/raw/` vào DB, có giờ mở cửa theo mùa.
  - `planner/feasibility.py` + test cho từng loại vi phạm.
  - Tải dự báo lịch sử 1–10 ngày + ERA5 (chạy nền, có thể mất vài giờ).
- **Bạn**
  - Gán nhãn **50 địa điểm chính**, bắt đầu từ nhóm "Có nguồn giờ/giá".
  - Nhờ 1 người gán độc lập 20 điểm trong số đó (để tính độ đồng thuận).
  - Đăng ký tài khoản Kaggle và xác minh số điện thoại (bắt buộc để có GPU).
  - Đăng ký Flickr API key và Mapillary token, đặt vào `.env`.
- **Mốc:** test bộ kiểm tra chạy xanh; DB có ≥900 địa điểm; ≥50 địa điểm có nhãn tay.

### Tuần 2 · 26/10–1/11 · Solver, dữ liệu thầy, baseline

- **Code**
  - OR-Tools solver + bảng thời gian đi OSRM.
  - Bộ sinh bài toán; chia ngày train/dev/test.
  - Sinh 10.000 bài train + 1.000 bài dev (chạy nền trên CPU).
  - Chạy baseline: ngẫu nhiên, Qwen 4B/9B chưa huấn luyện.
  - M2: dựng bảng dữ liệu + LightGBM.
- **Bạn**
  - Làm form khảo sát thu **yêu cầu lập lịch thật** ("Bạn sẽ hỏi trợ lý thế nào khi định đi Huế?"), phát cho bạn bè, nhóm sinh viên, nhóm du lịch. Mục tiêu 150 câu trước tuần 6.
  - Cài Ollama và kéo `qwen3.5:4b`, `qwen3.5:9b`.
- **Mốc:** ≥5.000 lời giải thầy; bảng baseline mới; LightGBM có Brier tốt hơn dự báo gốc.

### Tuần 3 · 2–8/11 · SFT và thu ảnh

- **Code**
  - Định dạng prompt/JSON gọn; sinh câu yêu cầu tổng hợp từ mẫu câu (ghi rõ là tổng hợp).
  - SFT 2B trên laptop; notebook Kaggle cho SFT 4B.
  - `train/vision/collect.py`: đếm rồi tải ảnh Commons/Flickr/Mapillary; lọc trùng.
- **Bạn**
  - Chạy notebook SFT 4B trên Kaggle (mình chuẩn bị sẵn, bạn bấm chạy).
  - Xem bảng đếm ảnh, chốt 30–40 địa danh cho M3.
  - Đăng bài nhờ người ở Huế chụp ảnh địa danh (giữ GPS, đồng ý cho dùng ảnh).
- **Mốc:** SFT có % lịch khả thi cao hơn bản chưa huấn luyện; có bảng đếm ảnh.

### Tuần 4 · 9–15/11 · GRPO

- **Code**
  - Hàm phần thưởng dựa trên bộ kiểm tra; notebook GRPO trên Kaggle (2B trước, 4B nếu đủ quota).
  - Theo dõi điểm thưởng, độ dài đầu ra, tỷ lệ lịch rỗng.
  - Đưa xác suất mưa của M2 vào planner, chạy lại thực nghiệm cũ.
- **Bạn**
  - Chạy GRPO trên Kaggle (quota khoảng 30 giờ GPU/tuần; nên chạy qua đêm).
- **Mốc:** điểm thưởng tăng dần, không có mẹo lịch rỗng; GRPO ≥ SFT trên dev.

### Tuần 5 · 16–22/11 · Nhận diện ảnh và runtime planner

- **Code**
  - Fine-tune SigLIP/CLIP + ArcFace; 4 cách phát hiện ảnh lạ; chọn ngưỡng.
  - `serve/planner_runtime.py`: sinh N lịch → kiểm tra → OR-Tools sửa.
  - Thử xuất GGUF sớm để biết có vướng không.
- **Bạn**
  - Kiểm ảnh test: đúng địa danh không, có ảnh nào lẫn sang tập train không (xem nhanh danh sách mình xuất ra).
- **Mốc:** Top-1 vượt CLIP chưa fine-tune; planner chạy trọn luồng.

### Tuần 6 · 23–29/11 · Ghép hệ thống, đóng băng tính năng

- **Code**
  - Kết hợp GPS (prior học từ dữ liệu; không kịp thì dùng prior cố định).
  - GGUF 3 mức nén + đo độ trễ.
  - Agent tối giản + Streamlit demo.
  - **Từ cuối tuần này không thêm tính năng mới.**
- **Bạn**
  - Đóng khảo sát; lọc còn 100–150 yêu cầu dùng được.
  - Chạy thử demo, ghi lại chỗ khó dùng.
- **Mốc:** demo chạy trọn trên laptop: nhận yêu cầu → lịch đã qua kiểm tra; nhận ảnh → tên địa danh hoặc "không xác định".

### Tuần 7 · 30/11–6/12 · Chạy thực nghiệm

- **Code**
  - Chạy bảng T1–T7 kèm khoảng tin cậy 95% (bootstrap).
  - Phân loại ≥100 lỗi của planner.
- **Bạn**
  - Gán nhãn ràng buộc cho tập yêu cầu thật; người thứ hai gán lại một phần để tính Cohen's kappa.
  - Đọc qua 100 lỗi đã phân loại, sửa chỗ phân loại sai.
- **Mốc:** đủ số liệu cho mọi bảng.

### Tuần 8 · 7–13/12 · Báo cáo

- Viết báo cáo, làm slide, quay video demo dự phòng.
- Cập nhật `CLAUDE.md` theo kiến trúc mới.

---

## 7. Việc bạn cần làm — checklist

| Hạn | Việc |
|---|---|
| 25/10 | Gán nhãn 50 địa điểm + nhờ người gán lại 20 điểm |
| 25/10 | Kaggle (xác minh SĐT), Flickr API key, Mapillary token |
| 1/11 | Phát khảo sát yêu cầu lập lịch thật |
| 8/11 | Chốt 30–40 địa danh M3; đăng nhờ chụp ảnh ở Huế |
| 8/11 và 15/11 | Chạy notebook SFT / GRPO trên Kaggle |
| 29/11 | Đóng khảo sát, lọc 100–150 yêu cầu |
| 6/12 | Gán nhãn tập yêu cầu thật + người thứ hai gán (kappa) |
| Bất kỳ lúc nào | Hỏi tác giả các repo GitHub Zalo Landmark xem còn giữ file dataset không |

---

## 8. Khi bị trễ

**Cắt theo thứ tự:**
1. Làm đẹp Streamlit
2. Prior GPS học được → dùng prior cố định
3. GRPO bản 4B → chỉ chạy bản 2B
4. M2 → giữ dự báo gốc

**Không bao giờ cắt:** bộ kiểm tra khả thi, SFT + GRPO của planner, tập test thật, bảng ablation, khoảng tin cậy.

**Quy tắc:** nếu hết tuần 2 mà bộ kiểm tra + solver + bộ sinh bài toán chưa xong, cắt ngay mục 1 và 2 ở trên, không lùi mốc GRPO.

---

## 9. Các bảng kết quả trong báo cáo

| Bảng | Nội dung |
|---|---|
| T1 | So sánh planner: ngẫu nhiên / Qwen 4B, 9B chưa huấn luyện / Gemini Flash / SFT-2B / SFT-4B / GRPO / GRPO + kiểm tra / OR-Tools |
| T2 | Thêm từng bước một (chưa huấn luyện → SFT → GRPO → chọn N lịch → OR-Tools sửa), kèm khoảng tin cậy |
| T3 | Tổng quát hóa: nhiều ứng viên hơn, mùa chưa thấy, chuyến dài hơn |
| T4 | Dự báo mưa: Brier/AUC theo số ngày dự báo trước; khoảng cách tới oracle thu hẹp bao nhiêu |
| T5 | Nhận diện: Top-1/5, AUROC, FPR@95, chỉ ảnh / chỉ GPS / kết hợp |
| T6 | Nén mô hình: chất lượng và độ trễ theo mức nén |
| T7 | Phân loại lỗi planner: sai giờ mở cửa, đi không kịp, vi phạm an toàn, sai JSON, bỏ sót yêu cầu |

---

## 10. Rủi ro chính

| Rủi ro | Cách xử lý |
|---|---|
| Viết lại code mất hơn 2 tuần | Cắt theo mục 8; giữ mốc GRPO |
| Hết quota GPU Kaggle | Ưu tiên bản 2B, đầu ra JSON ngắn, chạy qua đêm |
| Nhãn địa điểm vẫn là mặc định | Gán tay ngay tuần 1; phần thưởng và chỉ số dựa vào đó |
| Mô hình học mẹo ăn điểm (lịch rỗng) | Thưởng theo số điểm thăm + mức sàn độ phủ; theo dõi tỷ lệ lịch rỗng |
| Không có ảnh tự chụp | Flickr + Mapillary + nhờ chụp; chia theo tác giả; ghi rõ trong phần hạn chế |
| llama.cpp chưa hỗ trợ Qwen3.5 | Thử từ tuần 5; dự phòng chạy bằng transformers |
| M2 học khớp với chính thước chấm ERA5 | Giữ giai đoạn test riêng; thêm dữ liệu trạm nếu có |

---

## 11. Giải thích thuật ngữ

| Thuật ngữ | Nghĩa |
|---|---|
| **OR-Tools / VRPTW** | Thư viện tối ưu của Google; VRPTW là bài toán xếp lộ trình có khung giờ — đúng dạng bài xếp lịch tham quan |
| **SFT** | Học có giám sát: cho mô hình xem câu hỏi + đáp án mẫu để học chép theo |
| **LoRA / QLoRA** | Chỉ huấn luyện một phần nhỏ thêm vào mô hình (QLoRA: nén mô hình gốc 4-bit) để vừa GPU nhỏ |
| **GRPO** | Học tăng cường: mô hình tự sinh nhiều đáp án, chương trình chấm điểm, mô hình nghiêng dần về đáp án điểm cao |
| **Reward hacking** | Mô hình tìm mẹo ăn điểm mà không làm đúng việc (ví dụ trả lịch rỗng để không bao giờ vi phạm) |
| **Chưng cất (distillation)** | Mô hình nhỏ học lại lời giải của một hệ thống mạnh hơn (ở đây là OR-Tools) |
| **Best-of-N** | Sinh N đáp án rồi chọn đáp án tốt nhất qua bộ kiểm tra |
| **Oracle** | Phiên bản "gian lận" biết trước thời tiết thật; cho biết mức tốt nhất có thể đạt |
| **ERA5** | Dữ liệu thời tiết quá khứ tái tạo của châu Âu, dùng làm "thời tiết thật" để chấm |
| **Previous Runs** | Kho dự báo cũ của Open-Meteo: dự báo đã phát hành X ngày trước |
| **Brier score** | Sai số của dự báo xác suất; càng nhỏ càng tốt |
| **Reliability diagram** | Biểu đồ kiểm tra "nói 70% mưa thì có mưa thật khoảng 70% số lần không" |
| **OOD** | Ảnh ngoài phân phối: không thuộc địa danh nào mô hình đã học |
| **AUROC / FPR@95** | Đo khả năng tách ảnh lạ khỏi ảnh quen; FPR@95 là tỷ lệ ảnh lạ bị nhận nhầm khi vẫn giữ được 95% ảnh quen |
| **Metric learning / ArcFace** | Dạy mô hình đặt ảnh cùng địa danh gần nhau, khác địa danh xa nhau trong không gian vector |
| **kNN / pgvector** | Tìm vector gần nhất; pgvector là phần mở rộng của Postgres để làm việc này |
| **GGUF / lượng tử hóa** | Định dạng mô hình nén (4–8 bit) để chạy nhanh trên máy yếu, đổi lại giảm chút chất lượng |
| **Ablation** | Bỏ hoặc thêm từng thành phần để xem mỗi phần đóng góp bao nhiêu |
| **Bootstrap CI 95%** | Lấy mẫu lại nhiều lần để ước lượng khoảng dao động của kết quả |
| **Cohen's kappa** | Độ đồng thuận giữa hai người gán nhãn, đã trừ phần trùng do may rủi |
