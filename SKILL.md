---
name: colorgrading-slog3
description: Chỉnh màu hàng loạt footage Sony S-Log3 sang Rec.709 (LUT Sony + tự cân màu + tự xoay clip quay dọc), xuất HEVC 10-bit kèm báo cáo kiểm tra. Dùng khi người dùng đưa một thư mục hoặc clip S-Log3 cần chỉnh màu.
argument-hint: "<thư mục hoặc clip S-Log3> [--rotate auto|none|ccw|cw] [--limit 1] [--check]"
disable-model-invocation: true
---

# Chỉnh màu S-Log3 → Rec.709

Mọi xử lý video chạy bằng code trong `slog3grade/` (FFmpeg + numpy, chạy offline). Việc của bạn chỉ là chạy đúng lệnh, chờ, rồi tóm tắt báo cáo. Không đọc hay sửa code của skill trừ khi người dùng yêu cầu.

File gốc chỉ được đọc. Kết quả ra `<thư mục gốc>_Rec709` (giữ cấu trúc thư mục con, chỉ chứa video). Báo cáo `report.html` + `colorgrade.json` + ảnh so sánh nằm ở thư mục tạm, đường dẫn in ở cuối lệnh.

## Lệnh

Luôn chạy qua script bọc (tự chọn Python của `.venv` và đặt đúng thư mục):

- Windows: `powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_SKILL_DIR}/scripts/run.ps1" <tham số>`
- macOS/Linux: `bash "${CLAUDE_SKILL_DIR}/scripts/run.sh" <tham số>`

| Mục đích | Tham số |
| --- | --- |
| Kiểm tra cài đặt (lần đầu, hoặc khi lỗi) | `--doctor` |
| Xem trước: profile, LUT, chiều xoay của từng clip, không xuất | `"<SRC>" --check` |
| Chạy thử 1 clip | `"<SRC>" --limit 1` |
| Chạy cả thư mục (hoặc 1 file clip) | `"<SRC>"` |
| Chạy lại clip đã có kết quả | thêm `--force` |
| Ép chiều xoay | `--rotate ccw` (↺), `--rotate cw` (↻), `--rotate none` |
| Chỉ áp LUT, không tự cân màu | `--no-auto` |
| Đồng bộ màu giữa các clip (cần color-matcher) | `--match first` hoặc `--match "<clip/ảnh mẫu>"` |
| Thư mục kết quả khác | `--out "<thư mục>"` |

`$ARGUMENTS` là những gì người dùng gõ sau `/colorgrading-slog3`. Nếu không có đường dẫn, hỏi lại đường dẫn trước khi chạy.

LUT mặc định: `luts/SonyLookProfiles_SLog3_SGamut3Cine/2_SGamut3CineSLog3_To_LC-709TypeA.cube` (có sẵn trong skill). Thư mục `luts/` còn LC-709, Cine+709, SLog2-709; dùng `--lut` để đổi.

## Quy trình

1. Lần đầu dùng trên một máy, hoặc khi lệnh báo thiếu LUT/FFmpeg/thư viện: chạy `--doctor`, báo người dùng dòng LỖI và cách sửa trong `README.md`. Không tự cài phần mềm.
2. Lô lớn (hơn khoảng 10 clip hoặc vài GB): chạy `--check` trước, báo số clip, số clip bị bỏ qua và chiều xoay dự kiến của từng thư mục. Clip 4K mất khoảng 20–45 giây mỗi clip trên GPU NVIDIA.
3. Chạy lệnh chính dưới nền nếu lâu; không cần theo dõi liên tục, đợi lệnh kết thúc.
4. Đọc `colorgrade.json` trong thư mục báo cáo (đường dẫn ở dòng "Báo cáo kiểm tra"). Trả lời ngắn gọn:
   - số clip xong / bỏ qua / lỗi, thời gian, thư mục kết quả;
   - chiều xoay đã áp cho từng thư mục (`rotate_votes`);
   - clip cần xem lại, gom theo lý do trong `flags`, kèm đường dẫn `report.html`.
5. Đề xuất bước tiếp nếu có: ví dụ thư mục "Không chắc chiều xoay" thì chạy lại thư mục đó với `--rotate ccw|cw --force` và `--out` trỏ đúng thư mục con của kết quả.

Chi tiết các tuỳ chọn, giới hạn tự cân màu, ý nghĩa từng cảnh báo và lỗi thường gặp: xem `reference.md` (chỉ đọc khi cần).
