# SKILL.Colorgrading.S-Log3

De-log và cân màu cho các Source quay S-log3.

Skill cho Claude Code: chỉnh màu hàng loạt footage **Sony S-Log3 → Rec.709** bằng một câu lệnh `/colorgrading-slog3 <thư mục>`.
Mọi xử lý video chạy bằng code trên máy (FFmpeg + numpy, chạy offline); Claude chỉ chạy lệnh và tóm tắt báo cáo.

- Tự nhận S-Log3 + S-Gamut3 / S-Gamut3.Cine từ metadata Sony, chọn đúng LUT.
- Tự cân màu từng clip: cân trắng, điểm đen/trắng, độ tươi nhẹ, giữ màu da.
- Tự xoay clip quay dọc bằng máy xoay ngang (theo từng thư mục).
- Xuất HEVC 10-bit Rec.709 (GPU NVIDIA nếu có), giữ tiếng và timecode; file gốc không bị sửa.
- Báo cáo `report.html`: ảnh so sánh 3 cột (log / chỉ LUT / kết quả), clip cần xem lại xếp lên đầu.

Không dùng Claude cũng chạy được: `scripts/run.ps1 "<thư mục>"` (Windows) hoặc `scripts/run.sh "<thư mục>"`.

## Cài đặt

Yêu cầu: Python 3.10+, [FFmpeg](https://ffmpeg.org/download.html) trong PATH (có `hevc_nvenc` thì nhanh hơn nhiều). LUT S-Log3 của Sony đã có sẵn trong `luts/`.

```bash
# 1. Đặt skill vào thư mục skill cá nhân của Claude Code (dùng được ở mọi dự án)
git clone <URL repo này> ~/.claude/skills/colorgrading-slog3
cd ~/.claude/skills/colorgrading-slog3

# 2. Môi trường Python riêng cho skill
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # Windows (macOS/Linux: .venv/bin/pip)
.venv/Scripts/pip install -r requirements-extra.txt    # tuỳ chọn: tự đoán chiều xoay + đồng bộ màu

# 3. Cấu hình (tuỳ chọn: mặc định đã dùng LUT LC-709 Type A có sẵn trong luts/)
copy config.example.json config.json

# 4. Kiểm tra
powershell -File scripts/run.ps1 --doctor
```

Trên Windows, `~/.claude/skills` là `C:\Users\<tên>\.claude\skills`.

**LUT đi kèm:** `luts/SonyLookProfiles_SLog3_SGamut3Cine` (4 file, dành cho S-Log3 / S-Gamut3.Cine). Mặc định dùng `2_SGamut3CineSLog3_To_LC-709TypeA.cube`; đổi bằng `--lut` hoặc `colorgrade.lut` trong `config.json` (đường dẫn tương đối tính từ thư mục skill). Footage S-Gamut3 (không Cine) cần thêm LUT tương ứng của Sony vào `luts/`.

## Dùng

Trong Claude Code: `/colorgrading-slog3 "D:\Footage\Shoot 01"`. Skill đặt `disable-model-invocation: true` nên chỉ chạy khi bạn gọi, không tốn ngữ cảnh khi không dùng. Muốn Claude tự nhận khi bạn nhắc tới chỉnh màu S-Log3 thì xoá dòng đó trong `SKILL.md`.

Dòng lệnh (xem thêm `--help` và `reference.md`):

```bash
scripts/run.ps1 "D:\Footage\Shoot 01" --check     # xem trước, không xuất
scripts/run.ps1 "D:\Footage\Shoot 01" --limit 1   # chạy thử 1 clip
scripts/run.ps1 "D:\Footage\Shoot 01"             # cả thư mục
scripts/run.ps1 "D:\Footage\Shoot 01" --rotate ccw --force   # ép xoay ↺, làm lại
scripts/run.ps1 "D:\Inbox S-Log3" --watch 5      # thư mục chờ: mỗi 5 phút làm clip mới
```

Kết quả: `<thư mục gốc>_Rec709`. Báo cáo và ảnh so sánh: thư mục tạm (`temp_root`), đường dẫn in ở cuối lệnh.

## Cấu trúc

```text
SKILL.md                 hướng dẫn cho Claude
reference.md             chi tiết tuỳ chọn, cảnh báo, lỗi thường gặp
config.example.json      mẫu cấu hình (chép thành config.json)
requirements*.txt        thư viện Python
scripts/run.ps1|run.sh   chạy bằng Python của .venv
luts/                    bộ LUT S-Log3 của Sony
slog3grade/              code: colorgrade (luồng chính), autograde (tự cân màu), orient (tự đoán xoay), config
```

## Giấy phép và thư viện bên thứ ba

- `color-matcher` (đồng bộ màu, tuỳ chọn) dùng giấy phép **GPL-3.0**; repo này không chứa code của nó, chỉ cài qua pip khi cần.
- `open-clip-torch`, `torch`, `numpy`, `pillow`: cài qua pip theo giấy phép của từng gói. Trọng số CLIP `ViT-B-32 laion2b_s34b_b79k` tự tải lần đầu.
- Bộ LUT trong `luts/` là LookProfile của Sony, bản quyền thuộc Sony.
- Mã nguồn của repo này (thư mục `slog3grade/`, `scripts/` và các file hướng dẫn): giấy phép **MIT**, © 2026 D.U.M.A Studio (xem file `LICENSE`). Bộ LUT trong `luts/` không thuộc giấy phép này.
