"""python -m slog3grade --doctor   checks the setup
   python -m slog3grade SRC ...   grades (see colorgrade.main / --help)"""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .colorgrade import main
from .config import settings


def doctor() -> int:
    """Print what is installed and what is missing; exit code 1 when grading cannot run."""
    s, ok = settings(), True

    def line(good: bool, text: str, required: bool = True) -> None:
        nonlocal ok
        ok &= good or not required
        print(("OK   " if good else ("LỖI " if required else "--   ")) + text)

    print(f"slog3grade {__version__} · Python {sys.version.split()[0]} · cấu hình: {s['config_file'] or 'mặc định'}")
    line(sys.version_info >= (3, 10), "Python 3.10 trở lên")
    for tool in ("ffmpeg", "ffprobe"):
        line(bool(shutil.which(s[tool])), f"{tool}: {shutil.which(s[tool]) or 'không tìm thấy, cài FFmpeg và thêm vào PATH'}")
    if shutil.which(s["ffmpeg"]):
        enc = subprocess.run([s["ffmpeg"], "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        line("hevc_nvenc" in enc, "hevc_nvenc (GPU NVIDIA); không có thì dùng libx265 trên CPU, chậm hơn", False)
        line("libx265" in enc, "libx265 (dự phòng CPU)", "hevc_nvenc" not in enc)
    line(importlib.util.find_spec("numpy") is not None, "numpy")
    line(all(importlib.util.find_spec(m) for m in ("torch", "open_clip", "PIL")),
         "torch + open_clip + pillow: tự đoán chiều xoay (tuỳ chọn, requirements-extra.txt)", False)
    line(importlib.util.find_spec("color_matcher") is not None,
         "color-matcher: đồng bộ màu --match (tuỳ chọn, GPL-3, requirements-extra.txt)", False)
    lut = s["colorgrade"].get("lut")
    line(bool(lut) and Path(lut).exists(), f"LUT Sony: {lut or 'chưa đặt colorgrade.lut trong config.json (hoặc truyền --lut)'}",
         False)
    print("Sẵn sàng." if ok else "Chưa sẵn sàng: sửa các dòng LỖI ở trên.")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--doctor" in sys.argv[1:]:
        sys.exit(doctor())
    main()
