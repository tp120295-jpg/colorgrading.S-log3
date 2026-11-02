# Tham khảo chi tiết

## Từng clip được xử lý thế nào

1. **Nhận diện profile:** đọc XML của Sony (file `C0001M01.XML` đi kèm, hoặc XML nằm trong MP4) để lấy `CaptureGammaEquation` và `CaptureColorPrimaries`. Không có thì xem tag HLG/PQ của ffprobe; vẫn không có thì đoán theo histogram (S-Log3 không chạm đen video, ít chạm trắng). Clip không phải S-Log3 bị bỏ qua, trừ khi dùng `--assume cine|sgamut3`. Proxy trong `M4ROOT/SUB` luôn bị bỏ qua.
2. **Chọn LUT:** `--lut` (hoặc `colorgrade.lut` trong config) là một file `.cube` hoặc một thư mục. Thư mục thì chọn theo gamut (S-Gamut3.Cine / S-Gamut3) và kiểu màu (`--look`, mặc định ưu tiên LC-709 Type A).
3. **Xoay:** `--rotate auto` (mặc định) cho mô hình CLIP chấm từng clip đang nằm ngang ở 3 hướng, rồi mỗi thư mục theo kết quả chung của tối đa 20 clip. Chỉ xoay khi có ít nhất 5 clip ngang và một hướng thắng rõ (điểm trung bình chênh ≥ 0,004, hoặc ≥ 65% số phiếu). Clip đã dọc (kể cả nhờ tag xoay trong file) không bao giờ bị xoay.
4. **Tự cân màu** (`--no-auto` để tắt; `--auto-strength 0..1`): đo 5 khung hình sau LUT, rồi nướng thành một LUT riêng cho clip:
   - Cân trắng theo điểm gần trung tính (bão hoà < 0,12, sáng 25–90 IRE): sửa 70% độ lệch, tối đa ±6% mỗi kênh. Dưới 5% điểm trung tính thì dùng sàn/tường (bão hoà < 0,25): sửa 50%, tối đa ±4%.
   - Đen về ~3 IRE (hạ tối đa 8 IRE), trắng về ~93 IRE (giãn tối đa 1,25 lần, tự lùi nếu > 0,5% điểm cháy), đường cong S nhẹ.
   - Độ tươi +4% và vibrance nhẹ; vùng da tối đa +4%.
   - Màu da kéo về ~125° trên vectorscope, tối đa 3°.
5. **Đồng bộ màu** (`--match`, tuỳ chọn): color-matcher (MKL) tính phép biến đổi màu từ clip về clip mẫu, mức `--strength` (mặc định 0,6).
6. **Xuất một lần:** xoay → LUT Sony → LUT tự cân màu → LUT đồng bộ, HEVC 10-bit `hevc_nvenc` (dự phòng `libx265`), gắn tag Rec.709, tag `hvc1`, giữ tiếng và timecode. Clip có tiếng LPCM xuất `.mov`, còn lại `.mp4`.
7. **Ảnh so sánh 3 cột** (log gốc / chỉ LUT / kết quả) và `report.html`.

## Cảnh báo trong báo cáo (`flags`)

| Cảnh báo | Ý nghĩa | Làm gì |
| --- | --- | --- |
| Không chắc chiều xoay | Thư mục có dưới 5 clip ngang, hoặc không hướng nào thắng rõ | Xem ảnh so sánh; chạy lại với `--rotate ccw|cw --force` |
| Chưa cài torch/open_clip | Không có thư viện tự đoán xoay | Cài `requirements-extra.txt` hoặc dùng `--rotate` |
| Cảnh ánh sáng ấm | Ít vùng trắng/xám, cân trắng nhẹ theo sàn/tường | Thường chấp nhận được; muốn trắng hơn thì chỉnh tay |
| Không cân trắng được | Gần như không có vùng trung tính | Xem ảnh; chỉnh tay nếu cần |
| Cảnh sáng, đen chỉ xuống X IRE | Cảnh high-key, cố ý không kéo tối | Thường không cần làm gì |
| Cháy sáng X% | Hơn 0,3% điểm trên 98 IRE | Giảm `--auto-strength` |
| Màu da lệch | Góc màu da ngoài 115–135° | Bộ nhận da theo màu có thể bắt nhầm gỗ, lông vàng; xem ảnh |
| Không rõ gamut | Không có metadata, dùng `default_gamut` | Kiểm tra lại cài đặt máy quay |

## Lỗi thường gặp

| Lỗi | Cách sửa |
| --- | --- |
| `Không thấy LUT` | Kiểm tra `colorgrade.lut` trong `config.json` hoặc `--lut`; mặc định là LUT trong `luts/` |
| `ffmpeg` không tìm thấy | Cài FFmpeg, thêm vào PATH, hoặc ghi đường dẫn đầy đủ vào `config.json` |
| Encode rất chậm | Không có `hevc_nvenc` (không có GPU NVIDIA hoặc FFmpeg không hỗ trợ): đang chạy `libx265` trên CPU |
| `Ổ đĩa đích chỉ còn ... GB` | Giải phóng ổ hoặc dùng `--out` sang ổ khác |
| Clip bị `SKIPPED: Đã có bản chỉnh màu` | Thêm `--force` để làm lại |
| `Đồng bộ màu (--match) cần gói color-matcher` | `pip install color-matcher` (GPL-3) |

## Chế độ thư mục chờ

`--watch 5` theo dõi thư mục nguồn, mỗi 5 phút chỉ làm clip mới đã chép xong (không đổi trong 2 phút) và chưa có kết quả. Dừng bằng Ctrl+C.
