"""S-Log3 -> Rec.709 batch colour grade. Source clips are only read; graded copies go to <SRC>_Rec709 with the
same sub-folder layout, plus a before/after JPG per clip in _compare/ and a colorgrade.json report.

Per clip:
  1. detect the picture profile: Sony XML metadata (sidecar C0001M01.XML or the XML inside the MP4), else
     ffprobe tags, else a luma-histogram guess. Clips that are not S-Log3 are skipped unless `assume` is set;
  2. pick the Sony .cube LUT for its gamut (S-Gamut3.Cine or S-Gamut3) from a LUT file or folder;
  3. per-clip auto balance (autograde.py) and optionally a shot-match LUT fitted with color-matcher;
  4. encode HEVC 10-bit Rec.709 with NVENC (libx265 fallback) in one pass.

Run:  python -m slog3grade SRC --lut LUT_FILE_OR_FOLDER [--check] [--limit 1] [--look LC-709TypeA]
                               [--assume cine|sgamut3] [--match first|PATH] [--strength 0.6] [--method mkl] [--force]
"""
import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np

from . import autograde
from .config import settings

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mxf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
MATCH_SIZE = 33          # grid of the generated shot-match LUT
SAMPLE_W = 480           # frames are analysed at this width
SAMPLE_POINTS = (0.2, 0.35, 0.5, 0.65, 0.8)
GAMUT_VI = {"cine": "S-Gamut3.Cine", "sgamut3": "S-Gamut3"}
# preferred Sony looks when a LUT folder holds several (substrings of the normalised LUT output name)
DEFAULT_LOOKS = ["lc709typea", "s709", "lc709", "cine709", "709"]
# ffprobe colour matrix -> swscale matrix name; untagged clips are treated as BT.709 (what Sony writes)
MATRIX = {"bt709": "bt709", "smpte170m": "bt601", "bt470bg": "bt601", "bt2020nc": "bt2020", "bt2020c": "bt2020"}
MIN_FREE_BYTES = 2 << 30
# landscape clips shot on a camera turned sideways (many Sony bodies write no orientation): turn them upright
TRANSPOSE = {"ccw": "transpose=cclock", "cw": "transpose=clock"}
STABLE_SECONDS = 120     # watch mode: a file untouched this long is fully copied
ROTATE_SAMPLE = 20      # clips per folder that CLIP looks at for rotate="auto"
ROTATE_MIN_CLIPS = 5     # fewer landscape clips than this in a folder: too little evidence, do not rotate
ROTATE_MIN_SHARE = 0.65   # ...or this share of per-clip votes for the winning direction
ROTATE_MIN_MARGIN = 0.004  # mean score lead of the winning direction (real folders measured 0.009-0.011)
ROTATE_VI = {"ccw": "xoay 90° ngược chiều kim đồng hồ", "cw": "xoay 90° theo chiều kim đồng hồ"}

STEPS = {"analyse": "Phân tích màu", "auto": "Tự cân màu", "match": "Đồng bộ màu", "encode": "Xuất video", "compare": "Ảnh so sánh",
         "done": "Xong"}


class GradeError(Exception):
    """Problem the user can fix (paths, LUTs, missing packages); the message is shown as-is."""


class Cancelled(Exception):
    pass


# ---------- probing & profile detection ----------

def probe(path: Path) -> dict:
    r = subprocess.run(
        [settings()["ffprobe"], "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,pix_fmt,width,height,color_space,color_transfer,color_primaries,color_range"
         ":stream_side_data=rotation:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        raise GradeError(f"Không đọc được file {path.name}: {r.stderr.strip()[-200:]}")
    d = json.loads(r.stdout)
    video = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise GradeError(f"{path.name} không có hình")
    audio = [s.get("codec_name", "") for s in d["streams"] if s.get("codec_type") == "audio"]
    rot = next((abs(int(x.get("rotation", 0))) for x in video.get("side_data_list", []) if "rotation" in x), 0)
    landscape = (video.get("width", 0) > video.get("height", 0)) != (rot % 180 == 90)  # as players show it
    return {**video, "audio": audio, "landscape": landscape,
            "duration": float(d.get("format", {}).get("duration") or 0)}


_XML_ITEM = re.compile(rb'name="Capture(GammaEquation|ColorPrimaries)"\s+value="([^"]*)"')


def _xml_values(data: bytes) -> dict:
    return {k.decode(): v.decode("ascii", "replace").lower() for k, v in _XML_ITEM.findall(data)}


def _embedded_xml(path: Path) -> bytes:
    """Sony writes its NonRealTimeMeta XML inside the MP4 (moov/meta or a uuid box): walk the top-level boxes."""
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        if path.suffix.lower() == ".mxf":  # MXF header metadata sits at the start
            f.seek(0)
            return f.read(8 << 20)
        pos = 0
        while pos + 8 <= size:
            f.seek(pos)
            h = f.read(16)
            n, typ = struct.unpack(">I4s", h[:8])
            hdr = 8
            if n == 1:
                n, hdr = struct.unpack(">Q", h[8:16])[0], 16
            elif n == 0:
                n = size - pos
            if n < hdr:
                break
            if typ in (b"moov", b"meta", b"uuid") and 16 < n <= 64 << 20:
                data = f.read(n - hdr) if hdr == 16 else h[8:] + f.read(n - 16)
                if b"CaptureGammaEquation" in data:
                    return data
            pos += n
    return b""


def _luma_guess(path: Path, info: dict) -> bool:
    """S-Log3 never reaches video black (code 95 of 1023 vs 64) and rarely white: a flat, lifted histogram."""
    s = settings()
    t = info["duration"] * 0.5 if info["duration"] > 1 else None
    seek = ["-ss", f"{t:.2f}"] if t else []
    raw = subprocess.run([s["ffmpeg"], "-hide_banner", "-loglevel", "error", *seek, "-i", str(path), "-frames:v", "1",
                          "-vf", f"scale={SAMPLE_W}:-2:flags=neighbor,format=yuv444p16le", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    if not raw:
        return False
    a = np.frombuffer(raw, dtype="<u2")
    y = a[: a.size // 3].astype(np.float64) / 65535.0  # first plane = Y code value / 1023 (range kept as-is)
    lo, hi = np.percentile(y, [0.5, 99.5])
    return bool(lo >= 0.08 and hi <= 0.86 and hi - lo < 0.66)


def detect_profile(path: Path, info: dict) -> dict:
    """{'gamma': 'slog3'|other, 'gamut': 'cine'|'sgamut3'|None, 'source': where it was read, 'raw': text}."""
    vals, source = {}, None
    for xml in sorted(path.parent.glob(path.stem + "M0*.XML")):  # Sony card sidecar, e.g. C0001M01.XML
        vals, source = _xml_values(xml.read_bytes()), "XML đi kèm"
        if vals:
            break
    if not vals and path.suffix.lower() in VIDEO_EXT:
        vals, source = _xml_values(_embedded_xml(path)), "metadata Sony"
    if vals:
        g, p = vals.get("GammaEquation", ""), vals.get("ColorPrimaries", "")
        raw = " / ".join(x for x in (g, p) if x)
        if "s-log3" in g or "slog3" in g:
            gamut = "cine" if "cine" in p or (not p and "cine" in g) else "sgamut3" if "gamut3" in p else None
            return {"gamma": "slog3", "gamut": gamut, "source": source, "raw": raw}
        return {"gamma": g or "unknown", "gamut": None, "source": source, "raw": raw}
    trc = info.get("color_transfer") or ""
    if trc in ("arib-std-b67", "smpte2084"):
        return {"gamma": "hlg" if trc == "arib-std-b67" else "pq", "gamut": None, "source": "ffprobe", "raw": trc}
    if path.suffix.lower() in VIDEO_EXT and _luma_guess(path, info):
        return {"gamma": "slog3", "gamut": None, "source": "đoán theo histogram", "raw": trc or "không có tag"}
    return {"gamma": "unknown", "gamut": None, "source": "không có metadata", "raw": trc or "không có tag"}


# ---------- LUTs ----------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def lut_info(p: Path) -> dict:
    """Parse Sony LUT names such as SLog3SGamut3.CineToLC-709TypeA, SLog3SGamut3ToCine+709, SGamut3CineSLog3_To_s709."""
    src, _, dst = _norm(p.stem).partition("to")
    gamut = "cine" if "sgamut3cine" in src else "sgamut3" if "sgamut3" in src else None
    return {"path": p, "name": p.name, "slog3": "slog3" in src, "gamut": gamut, "look": dst}


def check_lut(p: Path) -> None:
    try:
        head = p.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError as e:
        raise GradeError(f"Không mở được LUT {p}: {e}")
    if "LUT_3D_SIZE" not in head:
        raise GradeError(f"{p.name} không phải LUT 3D .cube (thiếu LUT_3D_SIZE)")


def load_luts(lut: Path) -> list[dict]:
    if not lut.exists():
        raise GradeError(f"Không thấy LUT: {lut}")
    if lut.is_file():
        check_lut(lut)
        return [{**lut_info(lut), "explicit": True}]
    luts = [lut_info(p) for p in sorted(lut.rglob("*.cube"))]
    luts = [x for x in luts if x["slog3"]]
    if not luts:
        raise GradeError(f"Thư mục {lut} không có LUT S-Log3 (.cube) nào")
    return luts


def pick_lut(luts: list[dict], gamut: str | None, look: str | None) -> tuple[dict, str | None]:
    """Best LUT for this gamut and look, plus a warning when the match is not exact."""
    if len(luts) == 1 and luts[0].get("explicit"):
        x = luts[0]
        warn = (f"LUT {x['name']} dành cho {GAMUT_VI[x['gamut']]} nhưng clip là {GAMUT_VI[gamut]}"
                if gamut and x["gamut"] and x["gamut"] != gamut else None)
        return x, warn
    looks = [_norm(look)] if look else DEFAULT_LOOKS

    def score(x):
        rank = next((i for i, k in enumerate(looks) if k in x["look"]), len(looks))
        return (x["gamut"] != gamut, rank, len(x["look"]))

    best = min(luts, key=score)
    warn = None
    if gamut and best["gamut"] != gamut:
        warn = f"Không có LUT cho {GAMUT_VI[gamut]}, tạm dùng {best['name']}"
    elif look and _norm(look) not in best["look"]:
        warn = f"Không có LUT kiểu {look}, dùng {best['name']}"
    return best, warn


# ---------- ffmpeg helpers ----------

def _to_rgb(info: dict) -> str:
    m = MATRIX.get(info.get("color_space") or "", "bt709")
    r = "pc" if info.get("color_range") == "pc" else "tv"
    return f"scale=in_color_matrix={m}:in_range={r},format=gbrp16le"


def _lut_chain(luts: list[Path]) -> list[str]:
    # lut3d paths are relative to the work dir, which avoids FFmpeg filter escaping of Windows paths
    return [f"lut3d=file={l.name}:interp=tetrahedral" for l in luts]


def grab_frames(path: Path, info: dict, luts: list[Path], workdir: Path) -> np.ndarray:
    """Sample frames (after `luts`) as one float RGB array in [0, 1], stacked vertically."""
    s = settings()
    times = [info["duration"] * t for t in SAMPLE_POINTS] if info["duration"] > 1 else [None]
    vf = ",".join([f"scale={SAMPLE_W}:-2", _to_rgb(info)] + _lut_chain(luts) + ["format=rgb48le"])
    frames = []
    for t in times:
        seek = ["-ss", f"{t:.2f}"] if t is not None else []
        raw = subprocess.run([s["ffmpeg"], "-hide_banner", "-loglevel", "error", *seek, "-i", str(path),
                              "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-"],
                             capture_output=True, check=True, cwd=workdir).stdout
        if raw:
            a = np.frombuffer(raw, dtype="<u2").astype(np.float64) / 65535.0
            frames.append(a.reshape(-1, SAMPLE_W, 3))
    if not frames:
        raise RuntimeError(f"Không lấy được khung hình mẫu từ {path.name}")
    return np.concatenate(frames, axis=0)


def fit_match_lut(src_rgb: np.ndarray, ref_rgb: np.ndarray, dst: Path, method: str, strength: float) -> None:
    """Fit color-matcher on the sampled frames and bake the colour transform into a .cube file."""
    try:
        from color_matcher import ColorMatcher  # GPL-3, installed separately (requirements-extra.txt)
    except ImportError as e:
        raise GradeError("Đồng bộ màu (--match) cần gói color-matcher: pip install color-matcher") from e

    n = MATCH_SIZE
    g = np.linspace(0.0, 1.0, n)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")          # red varies fastest in .cube order
    grid = np.stack([r, gg, b], axis=-1).reshape(-1, 1, 3)
    if method in ("mkl", "mvgd", "default"):
        cm = ColorMatcher(src=src_rgb, ref=ref_rgb, method="mkl")
        cm.transfer()
        out = (cm.transfer_mat @ (grid.reshape(-1, 3).T - cm.mu_r) + cm.mu_z).T
    else:  # hm / reinhard: append the grid to the frame so its statistics barely move, then read it back
        stacked = np.concatenate([src_rgb.reshape(-1, 1, 3), grid], axis=0)
        res = ColorMatcher(src=stacked, ref=ref_rgb.reshape(-1, 1, 3), method=method).transfer()
        out = res[-grid.shape[0]:].reshape(-1, 3)
    out = grid.reshape(-1, 3) + strength * (out - grid.reshape(-1, 3))
    out = np.clip(out, 0.0, 1.0)
    lines = [f"TITLE \"slog3grade shot match ({method}, {strength:g})\"", f"LUT_3D_SIZE {n}"]
    lines += [f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}" for v in out]
    dst.write_text("\n".join(lines) + "\n", encoding="ascii")


def _ffmpeg(cmd: list[str], duration: float, cwd: Path, on_pct=None, cancelled=None) -> None:
    """Run FFmpeg with -progress; raises RuntimeError with the stderr tail on failure, Cancelled on cancel."""
    cmd = [cmd[0], "-progress", "pipe:1", "-nostats", *cmd[1:]]
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, cwd=cwd, text=True, encoding="utf-8")
        for line in p.stdout:
            if cancelled and cancelled():
                p.kill()
                p.wait()
                raise Cancelled()
            if on_pct and duration and line.startswith("out_time_us="):
                try:
                    on_pct(min(99.0, int(line.split("=", 1)[1]) / 1e6 / duration * 100))
                except ValueError:
                    pass
        p.wait()
        if p.returncode:
            err.seek(0)
            raise RuntimeError(err.read().decode("utf-8", "replace").strip()[-600:] or f"FFmpeg lỗi {p.returncode}")


def _rotate(info: dict) -> list[str]:
    return [TRANSPOSE[info["rotate"]]] if info.get("rotate") else []


def encode(src: Path, dst: Path, info: dict, luts: list[Path], workdir: Path, on_pct=None, cancelled=None) -> str:
    """Grade `src` into `dst`; returns the encoder used."""
    s = settings()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".part" + dst.suffix)
    vf = ",".join(_rotate(info) + [_to_rgb(info)] + _lut_chain(luts) + [
        "scale=out_color_matrix=bt709:out_range=tv,format=p010le",
        "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"])
    tags = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
    # Sony records LPCM, which MP4 players/editors handle badly: those clips go to .mov with the audio copied
    head = [s["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?",
            "-vf", vf]
    tail = ["-tag:v", "hvc1", *tags, "-c:a", "copy", "-map_metadata", "0", "-movflags", "+faststart", str(tmp)]
    encoders = [("hevc_nvenc", ["-c:v", "hevc_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "16",
                                "-b:v", "0", "-profile:v", "main10"]),
                ("libx265", ["-c:v", "libx265", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p10le"])]
    try:
        for i, (name, codec) in enumerate(encoders):
            try:
                _ffmpeg(head + codec + tail, info["duration"], workdir, on_pct, cancelled)
                tmp.replace(dst)
                return name
            except RuntimeError:
                if i == len(encoders) - 1:
                    raise
    finally:
        tmp.unlink(missing_ok=True)


def make_compare(src: Path, dst: Path, info: dict, jpg: Path, sony: Path, workdir: Path) -> None:
    """Side-by-side JPG: original S-Log3 | Sony LUT only | final result."""
    s = settings()
    t = f"{info['duration'] * 0.4:.2f}" if info["duration"] > 1 else "0"
    fit = "scale='if(gt(iw,ih),960,-2)':'if(gt(iw,ih),-2,960)',setsar=1,format=yuv420p"
    split = ",".join(_rotate(info) + ["split"])
    lut_only = ",".join([_to_rgb(info)] + _lut_chain([sony]) + ["scale=out_color_matrix=bt709:out_range=tv"])
    jpg.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([s["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-ss", t, "-i", str(src), "-ss", t,
                    "-i", str(dst), "-filter_complex",
                    f"[0:v]{split}[s0][s1];[s0]{fit}[a];[s1]{lut_only},{fit}[b];[1:v]{fit}[c];[a][b][c]hstack=inputs=3",
                    "-frames:v", "1", "-q:v", "3", str(jpg)], check=True, capture_output=True, cwd=workdir)


# ---------- planning & batch ----------

def find_clips(src: Path, out: Path) -> list[Path]:
    def keep(p: Path) -> bool:
        parts = [x.upper() for x in p.parts]
        return (p.suffix.lower() in VIDEO_EXT and p.is_file() and out not in p.parents and not p.name.startswith(".")
                and ".PART." not in p.name.upper() and not (p.parent.name.upper() == "SUB" and "M4ROOT" in parts))
    return sorted(p for p in src.rglob("*") if keep(p))  # M4ROOT/SUB holds Sony's low-res proxies


def plan(src: str | Path, lut: str | Path | None = None, out: str | Path | None = None, look: str | None = None,
         assume: str | None = None, limit: int | None = None, rotate: str | None = None,
         only_new: bool = False) -> dict:
    """Detect every clip's profile and its LUT, without encoding anything."""
    cfg = settings().get("colorgrade", {})
    src = Path(src).expanduser().resolve()
    single = src if src.is_file() and src.suffix.lower() in VIDEO_EXT else None  # one clip: output next to its folder
    if single:
        src = src.parent
    elif not src.is_dir():
        raise GradeError(f"Không tìm thấy thư mục hoặc clip footage: {src}")
    lut = lut or cfg.get("lut")
    if not lut:
        raise GradeError("Chưa chọn LUT Sony. Nhập file/thư mục LUT hoặc đặt colorgrade.lut trong settings.json")
    luts = load_luts(Path(lut).expanduser())
    look = look or cfg.get("look")
    out = Path(out).expanduser().resolve() if out else src.with_name(src.name + "_Rec709")
    if out == src:
        raise GradeError("Thư mục kết quả không được trùng thư mục footage gốc")
    clips = [single] if single else find_clips(src, out)
    if not clips:
        raise GradeError(f"Không có clip video nào trong {src}")
    rotate = rotate or cfg.get("rotate")
    rows, eligible, cand = [], 0, {}
    for clip in clips:
        if only_new:  # watch mode: skip clips still being copied and clips already graded (they still vote)
            if time.time() - clip.stat().st_mtime < STABLE_SECONDS:
                continue
            base = (out / clip.relative_to(src)).with_suffix("")
            if any(base.with_suffix(e).exists() for e in (".mp4", ".mov")):
                if rotate == "auto":
                    info = probe(clip)
                    if info["landscape"]:
                        cand.setdefault(clip.parent, []).append((clip, info["duration"], {}))
                continue
        row = {"index": len(rows) + 1, "source": str(clip), "name": str(clip.relative_to(src)), "status": "QUEUED",
               "step": "", "pct": 0}
        try:
            info = probe(clip)
            prof = detect_profile(clip, info)
            # `assume` only fills in what metadata could not tell; it never overrides a clip tagged Rec.709/HLG
            if assume and (prof["gamma"] == "unknown" or prof["gamma"] == "slog3" and not prof["gamut"]):
                prof = {**prof, "gamma": "slog3", "gamut": prof["gamut"] or assume,
                        "source": prof["source"] + f", coi là {GAMUT_VI[assume]}"}
            row.update(profile=prof, info={k: info.get(k) for k in ("codec_name", "pix_fmt", "width", "height",
                                                                       "duration", "audio")})
            if prof["gamma"] != "slog3":
                row.update(status="SKIPPED", reason=f"Không phải S-Log3 ({prof['raw']}, {prof['source']})")
            else:
                gamut = prof["gamut"] or cfg.get("default_gamut", "cine")
                if not prof["gamut"]:
                    row["warn"] = f"Không rõ gamut, dùng {GAMUT_VI[gamut]} (mặc định)"
                    prof["gamut"] = gamut
                x, warn = pick_lut(luts, gamut, look)
                row.update(lut=str(x["path"]), lut_name=x["name"])
                if warn:
                    row["warn"] = "; ".join(filter(None, [row.get("warn"), warn]))
                ext = ".mov" if any(a.startswith("pcm") for a in info["audio"]) else ".mp4"
                row["output"] = str((out / clip.relative_to(src)).with_suffix(ext))
                if rotate in TRANSPOSE and info["landscape"]:  # clips already upright are left alone
                    row["rotate"] = rotate
                elif rotate == "auto" and info["landscape"]:
                    cand.setdefault(clip.parent, []).append((clip, info["duration"], row))
                eligible += 1
                if limit and eligible > limit:
                    continue
        except GradeError as e:
            row.update(status="FAILED", error=str(e))
        rows.append(row)
    votes = _vote_rotation(cand, src) if cand else {}
    if limit:
        rows = [r for r in rows if r["status"] == "QUEUED"]
    for i, r in enumerate(rows, 1):
        r["index"] = i
    return {"src": str(src), "out": str(out), "lut": str(lut), "look": look, "assume": assume, "rotate": rotate,
            "rotate_votes": votes, "clips": rows}


def _vote_rotation(cand: dict, src: Path) -> dict:
    """rotate="auto": CLIP guesses each landscape clip (orient.py), then every folder follows its majority, because
    one shoot keeps one camera orientation and a single guess is right only ~80% of the time (tested on 134
    clips: folder votes 75/97 and 32/37 were both correct). Under ROTATE_MIN_SHARE agreement nothing is rotated."""
    from collections import Counter

    try:
        from . import orient
    except ImportError:  # torch/open_clip not installed: no auto rotation
        for items in cand.values():
            for _, _, row in items:
                row["warn"] = "; ".join(filter(None, [row.get("warn"), "Chưa cài torch/open_clip nên không tự đoán "
                                                       "chiều xoay; dùng --rotate ccw|cw nếu cần"]))
        return {}

    out = {}
    for folder, items in cand.items():
        step = max(1, len(items) // ROTATE_SAMPLE)
        scores = [orient.guess(clip, dur)["scores"] for clip, dur, _ in items[::step][:ROTATE_SAMPLE]]
        scores = [s for s in scores if s]
        if not scores:
            continue
        votes = Counter(max(s, key=s.get) for s in scores)  # each clip's best guess, shown in the report
        # one clip's signal is weak (~0.01 apart, about as noisy), so decide on the folder's mean score
        mean = {k: float(np.mean([s[k] for s in scores])) for k in scores[0]}
        top = max(mean, key=mean.get)
        margin = mean[top] - max(v for k, v in mean.items() if k != top)
        share = votes[top] / len(scores)  # a clear majority of per-clip guesses also counts (14/20 on a real folder)
        sure = len(scores) >= ROTATE_MIN_CLIPS and margin > 0 and (margin >= ROTATE_MIN_MARGIN or share >= ROTATE_MIN_SHARE)
        decision = top if sure else "none"
        out[str(folder.relative_to(src)) or "."] = {"decision": decision, "sure": sure, "votes": dict(votes),
                                                     "margin": round(margin, 4), "clips_checked": len(scores)}
        for _, _, row in items:
            if decision in TRANSPOSE:
                row["rotate"], row["rotate_auto"] = decision, True
            elif not sure:
                why = (f"chỉ có {len(scores)} clip ngang, cần ≥ {ROTATE_MIN_CLIPS}" if len(scores) < ROTATE_MIN_CLIPS
                       else f"phiếu {dict(votes)}")
                row["warn"] = "; ".join(filter(None, [row.get("warn"),
                                                       f"Không chắc chiều xoay ({why}), giữ nguyên; chọn xoay tay nếu cần"]))
    return out


def _save_report(st: dict) -> None:
    # report + compare images live under temp_root, so the output folder holds only the graded clips
    d = Path(st["report_dir"])
    d.mkdir(parents=True, exist_ok=True)
    keep = {k: v for k, v in st.items() if k != "cancel"}
    (d / "colorgrade.json").write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")


def run(st: dict, match: str | None = None, method: str = "mkl", strength: float = 0.6, force: bool = False,
        auto: bool = True, auto_strength: float = 1.0, log=print) -> dict:
    """Grade every QUEUED clip of a plan() result in place; one failed clip does not stop the rest.
    Per clip: Sony LUT -> auto balance LUT (`auto`) -> shot-match LUT (`match`), all in one encode."""
    s = settings()
    run_id = st.get("grade_id") or datetime.now().strftime("GRADE_%Y%m%d_%H%M%S")
    st.update(status="RUNNING", match=match, method=method, strength=strength, auto=auto, auto_strength=auto_strength,
              started=datetime.now().isoformat(timespec="seconds"),
              report_dir=str(Path(s["temp_root"]) / "colorgrade" / run_id))
    src = Path(st["src"])
    todo = [c for c in st["clips"] if c["status"] == "QUEUED"]
    work = Path(tempfile.mkdtemp(prefix="slog3grade_", dir=s.get("temp_root")))
    names: dict[str, Path] = {}

    def staged(lut: str) -> Path:  # copy each LUT into the work dir once (short relative name for lut3d)
        if lut not in names:
            names[lut] = work / f"lut{len(names):02d}.cube"
            shutil.copy(lut, names[lut])
        return names[lut]

    def auto_lut(c: dict, clip: Path, info: dict, luts: list[Path]) -> Path:
        """Fit the clip's auto balance on frames after its Sony LUT; the parameters go into the report."""
        rgb = grab_frames(clip, info, luts, work)
        p = autograde.fit(rgb, auto_strength)
        dst = work / f"auto_{c['index']:03d}.cube"
        autograde.write_lut(p, dst)
        c["auto"] = {"params": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in p.items()},
                     "before": autograde.measure(rgb), "after": autograde.measure(autograde.apply(rgb, p))}
        return dst

    t0 = time.time()
    try:
        ref_rgb, ref_clip = None, None
        if match and todo:
            if match == "first":
                ref_clip = todo[0]
                ref = Path(ref_clip["source"])
                info = probe(ref)
                luts = [staged(ref_clip["lut"])]
                if auto:
                    luts.append(auto_lut(ref_clip, ref, info, luts))
                ref_rgb = grab_frames(ref, info, luts, work)
            else:
                ref = Path(match).expanduser()
                if not ref.exists():
                    raise GradeError(f"Không thấy clip/ảnh mẫu: {ref}")
                ref_rgb = grab_frames(ref, probe(ref), [], work)  # an external reference is already Rec.709
            st["match_ref"] = str(ref)
            log(f"Tham chiếu màu: {ref}")

        for c in todo:
            if st.get("cancel"):
                c.update(status="CANCELLED")
                continue
            clip, dst = Path(c["source"]), Path(c["output"])
            if dst.exists() and not force:
                c.update(status="SKIPPED", reason="Đã có bản chỉnh màu (bật Ghi đè để làm lại)", pct=100)
                log(f"[{c['index']}/{len(st['clips'])}] bỏ qua (đã có) {dst.name}")
                continue
            c.update(status="RUNNING", pct=0)
            tc = time.time()

            def step(k: str) -> None:
                c["step"], c["step_label"] = k, STEPS[k]

            try:
                free = shutil.disk_usage(next(p for p in [dst.parent, *dst.parents] if p.exists())).free
                if free < max(MIN_FREE_BYTES, clip.stat().st_size * 1.5):
                    raise GradeError(f"Ổ đĩa đích chỉ còn {free / 2**30:.1f} GB, không đủ chỗ cho {clip.name}")
                step("analyse")
                info = {**probe(clip), "rotate": c.get("rotate")}
                luts = [staged(c["lut"])]
                if auto:
                    step("auto")
                    luts.append(work / f"auto_{c['index']:03d}.cube" if c is ref_clip else
                                auto_lut(c, clip, info, luts))
                if ref_rgb is not None and c is not ref_clip:
                    step("match")
                    m = work / f"match_{c['index']:03d}.cube"
                    fit_match_lut(grab_frames(clip, info, luts, work), ref_rgb, m, method, strength)
                    luts.append(m)
                    c["matched"] = True
                step("encode")
                log(f"[{c['index']}/{len(st['clips'])}] {c['name']} · {GAMUT_VI.get(c['profile']['gamut'])} · "
                    f"{c['lut_name']}")
                c["encoder"] = encode(clip, dst, info, luts, work, lambda p: c.update(pct=round(p, 1)),
                                      lambda: st.get("cancel"))
                step("compare")
                jpg = Path(st["report_dir"]) / f"{c['index']:03d}_{clip.stem}.jpg"
                try:
                    make_compare(clip, dst, info, jpg, luts[0], work)
                    c["compare"] = str(jpg)
                except subprocess.CalledProcessError:
                    c["warn"] = "; ".join(filter(None, [c.get("warn"), "Không tạo được ảnh so sánh"]))
                step("done")
                c.update(status="COMPLETED", pct=100, seconds=round(time.time() - tc, 1),
                         size_mb=round(dst.stat().st_size / 2**20, 1), flags=review_flags(c))
            except Cancelled:
                c.update(status="CANCELLED", step="", step_label="")
            except GradeError as e:
                c.update(status="FAILED", error=str(e))
            except Exception as e:
                c.update(status="FAILED", error=f"Lỗi khi chỉnh màu {clip.name}: {str(e)[-400:]}",
                         trace=traceback.format_exc()[-1500:])
            if c["status"] == "FAILED":
                log(f"  LỖI: {c['error']}")
            _save_report(st)
    except GradeError as e:
        st["error"] = str(e)
    except Exception as e:
        st["error"] = f"Lỗi: {str(e)[-400:]}"
        st["trace"] = traceback.format_exc()[-1500:]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    done = [c for c in todo if c["status"] in ("COMPLETED", "SKIPPED")]
    st["status"] = ("CANCELLED" if st.get("cancel") else "FAILED" if st.get("error") or not done
                    else "COMPLETED" if len(done) == len(todo) else "PARTIAL") if todo else "COMPLETED"
    st["total_seconds"] = round(time.time() - t0, 1)
    st["review"] = sum(1 for c in st["clips"] if c.get("flags") or c["status"] == "FAILED")
    _save_report(st)
    write_html_report(st)
    return st


def review_flags(c: dict) -> list[str]:
    """Reasons a person should look at this clip; replaces reviewing every compare image by eye (or by Claude)."""
    flags = [c["warn"]] if c.get("warn") else []
    a = c.get("auto")
    if a:
        p, m = a["params"], a["after"]
        if p.get("wb_mode") == "skipped":
            flags.append("Không cân trắng được (không có vùng trắng/xám)")
        elif p.get("wb_mode") == "loose":
            flags.append("Cảnh ánh sáng ấm: cân trắng nhẹ theo sàn/tường, có thể còn hơi ấm")
        if m["black_ire"] > 15:
            flags.append(f"Cảnh sáng, đen chỉ xuống {m['black_ire']} IRE")
        if m["clipped_pct"] > 0.3:
            flags.append(f"Cháy sáng {m['clipped_pct']}%")
        if m.get("skin_hue") and m["skin_pct"] > 2 and not 115 <= m["skin_hue"] <= 135:
            flags.append(f"Màu da lệch ({m['skin_hue']}°, chuẩn ~125°)")
    return flags


def write_html_report(st: dict) -> Path:
    """One offline page in the report folder: summary, clips to review first, then every compare image."""
    from html import escape

    d = Path(st["report_dir"])
    clips = sorted(st["clips"], key=lambda c: (not (c.get("flags") or c["status"] == "FAILED"), c["index"]))

    def card(c: dict) -> str:
        img = f'<img src="{Path(c["compare"]).name}" loading="lazy">' if c.get("compare") else ""
        notes = "".join(f"<li>{escape(x)}</li>" for x in c.get("flags") or [])
        err = f'<p class="err">{escape(c["error"])}</p>' if c.get("error") else ""
        rot = f" · xoay {'↺' if c['rotate'] == 'ccw' else '↻'}" if c.get("rotate") else ""
        return (f'<section><h3>{escape(c["name"])} <small>{c["status"]}{rot}</small></h3>'
                f'{"<ul>" + notes + "</ul>" if notes else ""}{err}{img}</section>')

    n = {s: sum(c["status"] == s for c in st["clips"]) for s in ("COMPLETED", "SKIPPED", "FAILED", "CANCELLED")}
    votes = "".join(f"<li>{escape(k)}: {escape(str(v['decision']))} (phiếu {escape(str(v['votes']))})</li>"
                    for k, v in (st.get("rotate_votes") or {}).items())
    html = f"""<!doctype html><meta charset="utf-8"><title>Chỉnh màu {escape(st.get('grade_id', ''))}</title>
<style>body{{font:15px system-ui;margin:20px;max-width:1100px}}img{{width:100%;border-radius:6px}}
section{{border-top:1px solid #ddd;padding:10px 0}}small{{color:#777}}ul{{color:#b26a00}}.err{{color:#c62828}}</style>
<h1>Báo cáo chỉnh màu</h1><p>{escape(st['src'])} → {escape(st['out'])}<br>
Xong {n['COMPLETED']} · bỏ qua {n['SKIPPED']} · lỗi {n['FAILED']} · dừng {n['CANCELLED']} ·
<b>cần xem lại {st.get('review', 0)}</b> · tổng {round(st.get('total_seconds', 0) / 60)} phút</p>
{"<p>Chiều xoay tự đoán:</p><ul>" + votes + "</ul>" if votes else ""}
<p>Ảnh: trái log gốc · giữa chỉ LUT · phải kết quả. Clip cần xem lại được xếp lên đầu.</p>
{"".join(card(c) for c in clips)}"""
    f = d / "report.html"
    d.mkdir(parents=True, exist_ok=True)
    f.write_text(html, encoding="utf-8")
    st["report_html"] = str(f)
    return f


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="S-Log3 -> Rec.709 batch grade")
    ap.add_argument("src", type=Path, help="folder with S-Log3 clips (searched recursively, read-only)")
    ap.add_argument("--lut", help="Sony .cube LUT or a folder of them (default: settings.json colorgrade.lut)")
    ap.add_argument("--look", help="preferred LUT look when --lut is a folder, e.g. LC-709TypeA, LC-709, s709")
    ap.add_argument("--out", help="output folder (default: <src>_Rec709 next to src)")
    ap.add_argument("--assume", choices=["cine", "sgamut3"],
                    help="grade clips without readable metadata as S-Log3 with this gamut")
    ap.add_argument("--check", action="store_true", help="only list clips, detected profile and LUT")
    ap.add_argument("--match", help="'first' = match every clip to the first one, or a path to a graded "
                                    "reference clip/image; omit to skip shot matching")
    ap.add_argument("--method", default="mkl", choices=["mkl", "hm", "reinhard", "hm-mkl-hm"])
    ap.add_argument("--strength", type=float, default=0.6, help="0 = LUT only, 1 = full match")
    ap.add_argument("--limit", type=int, help="grade only the first N S-Log3 clips (test run)")
    ap.add_argument("--force", action="store_true", help="re-grade clips that already have an output")
    ap.add_argument("--rotate", choices=["auto", "none", "ccw", "cw"],
                    help="turn landscape clips upright (camera held sideways); default settings.json colorgrade.rotate")
    ap.add_argument("--no-auto", action="store_true", help="Sony LUT only, skip the per-clip auto balance")
    ap.add_argument("--auto-strength", type=float, default=1.0, help="auto balance amount, 0..1")
    ap.add_argument("--watch", type=float, metavar="MINUTES",
                    help="keep running: every MINUTES grade clips newly copied into SRC (Ctrl+C to stop)")
    a = ap.parse_args(argv)
    if a.watch:
        return watch(a)
    try:
        st = plan(a.src, a.lut, a.out, a.look, a.assume, a.limit, a.rotate)
    except GradeError as e:
        sys.exit(str(e))
    for c in st["clips"]:
        p = c.get("profile") or {}
        print(f"{c['index']:3d}. {c['name']}: {c['status']} · {p.get('raw', '')} ({p.get('source', '')})"
              f"{' -> ' + c['lut_name'] if c.get('lut_name') else ''}{' · ' + c['reason'] if c.get('reason') else ''}"
              f"{' · ' + c['warn'] if c.get('warn') else ''}{' · ' + c['error'] if c.get('error') else ''}", flush=True)
    if a.check:
        return
    run(st, a.match, a.method, a.strength, a.force, not a.no_auto, a.auto_strength, log=lambda m: print(m, flush=True))
    print(f"Báo cáo kiểm tra: {st.get('report_html', st['report_dir'])} · cần xem lại {st.get('review', 0)} clip", flush=True)
    n = sum(c["status"] == "COMPLETED" for c in st["clips"])
    print(f"{st['status']}: {n}/{len(st['clips'])} clip xong · {st['out']}" + (f" · {st['error']}" if st.get("error")
                                                                                else ""), flush=True)


def watch(a) -> None:
    """Inbox mode: copy footage into SRC and it is graded on the next pass; graded clips are never redone."""
    src = Path(a.src).expanduser().resolve()
    out = Path(a.out).expanduser().resolve() if a.out else src.with_name(src.name + "_Rec709")
    print(f"Đang theo dõi {src} (mỗi {a.watch:g} phút) → {out}. Ctrl+C để dừng.", flush=True)
    while True:
        try:
            new = [p for p in find_clips(src, out) if time.time() - p.stat().st_mtime >= STABLE_SECONDS
                   and not any((out / p.relative_to(src)).with_suffix(e).exists() for e in (".mp4", ".mov"))]
            if new:
                print(f"{datetime.now():%H:%M} có {len(new)} clip mới", flush=True)
                st = plan(src, a.lut, a.out, a.look, a.assume, None, a.rotate, only_new=True)
                if any(c["status"] == "QUEUED" for c in st["clips"]):
                    run(st, a.match, a.method, a.strength, False, not a.no_auto, a.auto_strength,
                        log=lambda m: print(m, flush=True))
                    print(f"Báo cáo: {st.get('report_html')} · cần xem lại {st.get('review', 0)} clip", flush=True)
        except GradeError as e:
            print(f"{datetime.now():%H:%M} {e}", flush=True)
        time.sleep(a.watch * 60)


if __name__ == "__main__":
    main()
