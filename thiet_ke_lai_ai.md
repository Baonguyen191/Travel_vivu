# Thiết kế lại đồ án Travel Agent Huế theo hướng nặng AI

*Phiên bản 1 · 16/10/2026 · Máy: RTX 4060 Laptop 8 GB VRAM, 16 GB RAM, Windows*

---

## 0. Tóm tắt thay đổi

| | Thiết kế cũ | Thiết kế mới |
|---|---|---|
| Ai lập lịch | OR-Tools VRPTW; LLM chỉ trích JSON và diễn giải | **LLM 2–4B được huấn luyện (SFT → GRPO) tự lập lịch**. OR-Tools trở thành **thầy** (sinh dữ liệu), **cận trên** (để so sánh) và **phương án dự phòng** |
| Thời tiết | Dùng thẳng dự báo Open-Meteo | **Model hiệu chỉnh dự báo** học từ dự báo lịch sử so với thời tiết thực |
| Nhận diện ảnh | GPS → (tầng 2 chưa làm) → VLM zero-shot | **Fine-tune SigLIP/CLIP bằng metric learning**, phát hiện ảnh ngoài phân phối (OOD), **kết hợp GPS theo xác suất** |
| RAG | bge-m3 + BM25 + RRF, chưa huấn luyện | **Fine-tune retriever (hard negatives) và reranker** — phần có thể cắt |
| Triển khai | Ollama, model có sẵn | Gộp LoRA → **GGUF lượng tử hóa** → Ollama; đo mức suy giảm chất lượng theo từng mức lượng tử |

**Câu đóng góp (dự kiến):**
> Huấn luyện một LLM nhỏ (≤4B) lập lịch du lịch thích ứng thời tiết bằng học tăng cường với phần thưởng kiểm chứng được bằng chương trình (bộ kiểm tra khả thi + thời tiết thực), chưng cất từ bộ giải OR-Tools. Model đạt X% lịch khả thi (LLM chưa huấn luyện: Y%) và cách mục tiêu tối ưu của OR-Tools Z%. Kết hợp thêm dự báo mưa đã hiệu chỉnh, khoảng cách tới oracle về số giờ ngoài trời khi mưa thu hẹp W%.

**Vì sao vẫn bảo vệ được trước hội đồng:**
- Đây không phải "dùng AI thay thuật toán cho vui". Câu hỏi nghiên cứu cụ thể là: *một LLM nhỏ học được ràng buộc cứng (khung giờ, thời gian di chuyển, an toàn khi bão) đến mức nào, và RL với phần thưởng kiểm chứng có vượt SFT thuần không?*
- Khác với các nghiên cứu planner bằng RL đã có, vốn dùng câu hỏi tổng hợp và phần thưởng do LLM chấm: ở đây phần thưởng là chương trình kiểm tra và thời tiết là dữ liệu thật.
- OR-Tools vẫn được giữ, nên demo luôn chạy được. Nếu RL không vượt SFT thì đó vẫn là một kết quả âm có số liệu.

---

## 1. Nguyên tắc thiết kế

1. **Mỗi module học được đều có đủ bốn phần:** dữ liệu thật hoặc sinh từ dữ liệu thật, một baseline không học, chỉ số đo, và một dòng trong bảng ablation.
2. **Tập test luôn là dữ liệu thật.** Văn bản yêu cầu dùng để *huấn luyện* được phép sinh tự động (mẫu câu hoặc LLM diễn đạt lại) và phải ghi rõ trong báo cáo. *Tập test* gồm yêu cầu thật và ảnh tự chụp.
3. **Đúng/sai vẫn do chương trình quyết định.** LLM được phép *đề xuất* lịch, nhưng lịch chỉ được trả cho người dùng sau khi qua `planner/feasibility.py`. Giờ mở cửa và giá vé vẫn lấy từ DB. Nguyên tắc ranh giới trong CLAUDE.md đổi từ "thuật toán lập lịch" thành "**model đề xuất – bộ kiểm tra chấp nhận – OR-Tools sửa/dự phòng**".
4. **Train trên Kaggle/Colab, chạy suy luận trên máy local.** Laptop 8 GB chỉ dùng cho suy luận, fine-tune vision và LoRA model nhỏ.

---

## 2. Kiến trúc mới

```
Ảnh ──► [M3] SigLIP fine-tune + OOD + kết hợp GPS ──► địa danh / "không xác định"
                                                        │
Câu hỏi ─► Agent (qwen3.5 local) ─► [M4] Retriever + reranker fine-tune ─► place_chunks
                │
Yêu cầu lịch ───┼──► [M1] LLM Planner (SFT→GRPO) ──► bộ kiểm tra khả thi ──► OK → trả lịch
                │            ▲                              │ không đạt
                │            │ dữ liệu thầy                 ▼
                │        OR-Tools VRPTW  ◄──────────── sửa / dự phòng
                │
Open-Meteo ──► [M2] Hiệu chỉnh dự báo mưa ──► P(mưa | giờ, điểm) ──► M1 + OR-Tools
```

| Module | Công sức | Mức ưu tiên |
|---|---|---|
| M1 LLM Planner (SFT → GRPO) | ~40% | **Bắt buộc — đóng góp chính** |
| M3 Nhận diện địa danh (metric learning + OOD + GPS) | ~25% | **Bắt buộc** |
| M2 Hiệu chỉnh dự báo mưa | ~15% | Nên làm (rẻ, tác động thẳng vào kết quả hiện có) |
| M4 Fine-tune retriever + reranker | ~10% | Cắt đầu tiên nếu trễ |
| Triển khai GGUF + đo độ trễ | ~10% | Nên làm |

---

## 3. Chi tiết từng module

### M1 — LLM Planner học từ OR-Tools, tối ưu bằng GRPO (đóng góp chính)

**Bài toán.**
- Đầu vào: yêu cầu tiếng Việt; danh sách ≤30 ứng viên kèm `opening_hours`, `avg_visit_minutes`, `indoor_ratio`, `unsafe_conditions`; ma trận thời gian đi (OSRM); dự báo thời tiết theo giờ.
- Đầu ra: lịch JSON `[{day, place_id, start, end}]` kèm một câu giải thích ngắn.

**Dữ liệu.**
- **Bộ sinh bài toán**: lấy tập con POI Huế thật, ngày đi thật trong 2–3 năm lấy từ dự báo lịch sử của Open-Meteo, và hồ sơ ràng buộc ngẫu nhiên (số ngày, ngân sách, nhịp đi, sở thích, giờ bắt đầu).
- OR-Tools giải từng bài toán để tạo **lời giải thầy**. Mục tiêu khoảng 8–15k bài toán train và 1k bài toán dev; phần này chạy trên CPU.
- Văn bản yêu cầu dùng để train: mẫu câu cộng diễn đạt lại (ghi rõ là dữ liệu tổng hợp).
- **Tập test thật**: khoảng 100–150 yêu cầu lập lịch thật (thu từ bạn bè, khảo sát, câu hỏi công khai trên diễn đàn), tự gán nhãn ràng buộc và có 2 người gán để tính Cohen's kappa.

**Huấn luyện.**
1. *SFT (chưng cất):* QLoRA trên Qwen3.5-2B (chạy được local) và Qwen3.5-4B (trên Kaggle/Colab). Dạy model sinh lời giải của OR-Tools.
2. *GRPO với phần thưởng kiểm chứng:*
   `r = 1[JSON hợp lệ] · ( a·tỷ_lệ_ràng_buộc_đạt + b·(mục_tiêu / mục_tiêu_OR-Tools) − c·giờ_ngoài_trời_khi_mưa − d·1[vi phạm an toàn] )`.
   Phần thưởng tính bằng `planner/feasibility.py` cộng thời tiết. Thời tiết lấy từ dự báo trong lúc train và từ ERA5 khi chấm cuối.
3. *Suy luận có bộ kiểm tra:* sinh N lịch, chọn lịch khả thi tốt nhất. Nếu không có lịch nào đạt thì OR-Tools sửa (repair) hoặc thay hẳn.

**Baseline.** Ngẫu nhiên (trung bình nhiều thứ tự); qwen3.5:4b/9b zero-shot; Gemini Flash dùng prompt; OR-Tools (cận trên, đồng thời là thầy).

**Chỉ số.**
- % JSON hợp lệ, % lịch khả thi, khoảng cách tới mục tiêu của OR-Tools, số giờ ngoài trời khi mưa (đối chiếu ERA5), độ trễ p50/p95.
- **Khả năng tổng quát hóa** ra ngoài phân phối train: số ứng viên lớn hơn lúc train (20 → 30), mùa mưa chưa thấy (giữ lại tháng 10–11 của một năm làm test), số ngày đi dài hơn.

**Ablation.** zero-shot → SFT → SFT+GRPO → +best-of-N có kiểm tra → +OR-Tools sửa.

**Mở rộng nếu kịp:** *sở thích mơ hồ* ("đi chậm", "tránh đông người", "thích chụp ảnh lúc hoàng hôn"). Đây là chỗ LLM có lợi thế so với solver. Muốn đánh giá phải có người chấm (40–60 lịch, 2 người), không dùng LLM làm giám khảo.

**Phần cứng.** Theo tài liệu Unsloth, LoRA bf16 của Qwen3.5 cần khoảng 5 GB VRAM với bản 2B và khoảng 10 GB với bản 4B. Vậy SFT bản 2B làm được trên laptop; bản 4B phải dùng QLoRA hoặc Kaggle/Colab. GRPO phải sinh nhiều lần cho mỗi prompt nên tốn VRAM hơn hẳn, vì vậy nên chạy GRPO trên Kaggle (hướng dẫn cài Unsloth ưu tiên Linux). Nhớ giữ JSON đầu ra gọn để rollout ngắn.

**Rủi ro.**
- Model học thuộc lời giải thầy nhưng vẫn vi phạm khung giờ. Đây chính là lý do cần GRPO và bộ kiểm tra lúc suy luận.
- Phần thưởng bị lợi dụng (reward hacking), ví dụ lịch rỗng vẫn "khả thi". Cách chặn: thêm phần thưởng theo số điểm thăm và giới hạn dưới tỷ lệ phủ.

### M2 — Hiệu chỉnh dự báo mưa cho Huế

**Lý do.** Kết quả hiện có: hệ thống giảm 14% số giờ ngoài trời khi mưa, oracle giảm 38% (chuyến 1 ngày, dùng OSRM). Nếu oracle dùng cùng solver và chỉ khác ở chỗ biết trước thời tiết (cần kiểm tra lại trong `planner/experiment`), thì khoảng cách này hoàn toàn do sai số dự báo gây ra.

**Dữ liệu.** Open-Meteo Previous Runs (dự báo ở các độ xa 1–10 ngày), đối chiếu với ERA5 hoặc dữ liệu tái phân tích theo giờ cho các ô lưới Huế, 2–3 năm. Lưu ý ERA5 có độ phân giải thô (~0.25°) và có sai số riêng với mưa đối lưu, nên cần nêu trong phần hạn chế.

**Model.** LightGBM làm baseline mạnh, sau đó thử một temporal Transformer hoặc MLP nhỏ. Đầu ra là P(mưa > ngưỡng) theo giờ. Đặc trưng: các biến dự báo, độ xa của ngày đi, giờ trong ngày, tháng, ô lưới.

**Chỉ số.** Brier score, AUC, reliability diagram (biểu đồ độ tin cậy), so với dự báo thô và trung bình khí hậu. Chỉ số **cuối cùng** là phần trăm khoảng cách tới oracle được thu hẹp, đo bằng `python -m planner.experiment run`.

**Phần cứng.** CPU, vài phút.

### M3 — Nhận diện địa danh: metric learning + OOD + kết hợp GPS

**Dữ liệu train.**
- Ảnh Wikimedia Commons theo từng địa danh Huế (ghi giấy phép).
- Các lớp thuộc Huế trong Zalo Landmark, nếu link tải còn sống.
- Mapillary.
- Mục tiêu 30–200 ảnh cho mỗi địa danh, nhiều góc chụp.

**Tập test (tự chụp, thật).**
- 300–500 ảnh trong phân phối, có giữ EXIF GPS.
- Khoảng 150 ảnh **ngoài phân phối**: phố Huế không phải địa danh, địa danh ở thành phố khác trông giống, ảnh trong nhà.
- Nếu bạn không ở Huế, cần lên kế hoạch thu ảnh sớm, hoặc nhờ người chụp hộ có giữ EXIF.

**Model.** Fine-tune SigLIP-2 / CLIP ViT-B (LoRA hoặc mở băng vài khối cuối) với ArcFace hoặc supervised contrastive loss. Embedding 768 chiều, khớp với `place_images VECTOR(768)`; truy vấn kNN bằng pgvector.

**Phát hiện OOD.** So sánh MSP, energy, Mahalanobis và khoảng cách kNN. Báo cáo AUROC và FPR@95TPR. Ngưỡng chọn trên tập dev, thay cho ngưỡng 0.5 cố định hiện nay.

**Kết hợp GPS.** `P(l | ảnh, GPS) ∝ P(ảnh | l) · P(GPS | l)`, trong đó prior khoảng cách được *học* từ khoảng cách người chụp – vật thể trong dữ liệu Commons/Mapillary có tọa độ. Lý do: GPS là vị trí người chụp, ví dụ ảnh cầu Trường Tiền có thể chụp từ xa 72 m.

**Baseline.** Chỉ dùng GPS gần nhất; CLIP zero-shot; qwen3.5 VLM zero-shot (tầng 3 hiện tại).

**Ablation.** Chỉ ảnh / chỉ GPS / kết hợp; có hoặc không có OOD.

**Phần cứng.** ViT-B fine-tune trên 8 GB được, với batch nhỏ và mixed precision.

### M4 — Fine-tune retriever và reranker (cắt được)

- Cặp (câu hỏi, đoạn) dùng để train: sinh từ `place_chunks` (dữ liệu tổng hợp, chỉ dùng để train). Hard negatives khai thác bằng BM25, **bỏ các ứng viên trùng với đoạn đúng**.
- Fine-tune bge-m3 phần dense với InfoNCE. Fine-tune một cross-encoder reranker (ví dụ họ bge-reranker).
- Test trên `eval/rag_queries.yml`, mở rộng thêm câu hỏi thật. Chỉ số giữ nguyên: Hit@k, MRR, nDCG@5.
- Baseline: pipeline lai hiện tại chưa fine-tune.

### Triển khai và đo lường

- Gộp LoRA → xuất GGUF ở Q4_K_M / Q5_K_M / Q8_0 → chạy bằng Ollama.
- Đo % lịch khả thi và độ trễ theo từng mức lượng tử, để thấy lượng tử hóa làm mất bao nhiêu chất lượng.
- Agent điều phối dùng qwen3.5 local như khuyến nghị trước. Planner là model đã fine-tune, chạy cạnh agent.

---

## 4. Bảng thực nghiệm chính trong báo cáo

| Bảng | Nội dung |
|---|---|
| T1 | Planner: ngẫu nhiên / zero-shot 4B / zero-shot 9B / Gemini Flash / SFT-2B / SFT-4B / GRPO-4B / GRPO + kiểm tra / OR-Tools |
| T2 | Ablation planner theo từng bước, kèm khoảng tin cậy bootstrap 95% |
| T3 | Tổng quát hóa: số ứng viên, mùa chưa thấy, số ngày |
| T4 | Hiệu chỉnh dự báo: Brier/AUC theo độ xa ngày đi, % khoảng cách tới oracle được thu hẹp |
| T5 | Nhận diện: Top-1/5, AUROC OOD, FPR@95, ablation GPS |
| T6 | Lượng tử hóa: chất lượng và độ trễ |
| T7 | Phân loại lỗi planner (≥100 ca): sai khung giờ, đi không kịp, vi phạm an toàn, sai JSON, bỏ sót ràng buộc người dùng |

---

## 5. Lộ trình (giả định còn ~8 tuần tính từ 19/10/2026)

| Tuần | Việc | Mốc kiểm tra |
|---|---|---|
| 1 (19–25/10) | Bộ sinh bài toán + dữ liệu thầy OR-Tools; chạy baseline LLM zero-shot (vốn đang thiếu); kéo dữ liệu dự báo lịch sử và ERA5; **bắt đầu thu ảnh test** | Có ≥5k bài toán; bảng baseline zero-shot |
| 2 | SFT-2B local, SFT-4B trên Kaggle; M2 với LightGBM | SFT vượt zero-shot về % khả thi |
| 3 | GRPO-4B; thu ảnh Commons/Mapillary | Đường cong phần thưởng tăng, không có reward hacking |
| 4 | M3 fine-tune + OOD; suy luận planner có kiểm tra + OR-Tools sửa | Top-1 vượt zero-shot |
| 5 | Kết hợp GPS; xuất GGUF; tích hợp vào agent; **đóng băng tính năng** | Demo end-to-end chạy local |
| 6 | M4 (nếu đúng tiến độ) hoặc chạy lại thí nghiệm GRPO | — |
| 7 | Chạy toàn bộ T1–T7, phân tích lỗi, gán nhãn kappa | Đủ số liệu |
| 8 | Viết báo cáo, slide | Nộp |

**Thứ tự cắt khi trễ:** M4 → phần mở rộng "sở thích mơ hồ" → prior GPS học được (thay bằng prior cố định) → M2 chỉ giữ LightGBM → GRPO chỉ chạy bản 2B.
**Không bao giờ cắt:** SFT và GRPO của planner, bộ kiểm tra khả thi, tập test thật, bảng ablation, khoảng tin cậy.

---

## 6. Những gì ghi được vào CV

- Fine-tune LLM (QLoRA, SFT) và **RL với phần thưởng kiểm chứng (GRPO)** cho bài toán lập kế hoạch có ràng buộc; chưng cất từ bộ giải tối ưu tổ hợp (OR-Tools).
- Metric learning cho nhận diện ảnh (SigLIP/CLIP + ArcFace), phát hiện OOD, kết hợp dữ liệu đa phương thức (ảnh + GPS) theo Bayes.
- Hiệu chỉnh dự báo xác suất (Brier, độ tin cậy) trên dữ liệu khí tượng thật.
- Fine-tune hybrid retriever/reranker với hard-negative mining (nếu làm M4).
- Triển khai LLM: lượng tử hóa GGUF, Ollama, đo độ trễ, agent tool-calling.
- Thiết kế đánh giá: tập test tự gán nhãn có kappa, ablation, bootstrap CI, phân loại lỗi.

---

## 7. Cần sửa trong CLAUDE.md khi bắt đầu

- Mục "Ranh giới giữa LLM và thuật toán": đổi thành *model đề xuất – bộ kiểm tra chấp nhận – OR-Tools sửa/dự phòng*. "LLM tự lập lịch" chỉ còn là baseline **zero-shot**; planner đã huấn luyện là hệ thống chính.
- Thêm các thư mục `train/planner/`, `train/vision/`, `train/weather/`, `eval/`, cùng quy ước lưu checkpoint và seed.
- Dòng cấu hình LLM: đổi sang Ollama `qwen3.5:9b`/`4b` (theo khuyến nghị ngày 29/09).
- Sửa câu "Repo hiện chưa có code" cho khớp với tiến độ.
