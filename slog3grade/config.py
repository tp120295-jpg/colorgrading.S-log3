"""Settings: config.json next to this package (copy config.example.json), or the file named by SLOG3GRADE_CONFIG.
Missing keys fall back to DEFAULTS, so an empty config works once the LUT path is given on the command line."""
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
BUNDLED_LUT = SKILL_DIR / "luts" / "SonyLookProfiles_SLog3_SGamut3Cine" / "2_SGamut3CineSLog3_To_LC-709TypeA.cube"
DEFAULTS = {
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "temp_root": str(Path(tempfile.gettempdir()) / "slog3grade"),
    "colorgrade": {"lut": str(BUNDLED_LUT), "look": "LC-709TypeA", "default_gamut": "cine", "auto": True, "rotate": "auto"},
}


@lru_cache
def settings() -> dict:
    f = Path(os.environ.get("SLOG3GRADE_CONFIG", SKILL_DIR / "config.json"))
    user = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    s = {**DEFAULTS, **{k: v for k, v in user.items() if k != "colorgrade" and v not in ("", None)}}
    s["colorgrade"] = {**DEFAULTS["colorgrade"], **{k: v for k, v in user.get("colorgrade", {}).items() if v not in ("", None)}}
    lut = Path(s["colorgrade"]["lut"])  # relative paths in config.json are relative to the skill folder
    s["colorgrade"]["lut"] = str(lut if lut.is_absolute() else SKILL_DIR / lut)
    s["config_file"] = str(f) if f.exists() else None
    Path(s["temp_root"]).mkdir(parents=True, exist_ok=True)
    return s
