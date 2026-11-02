"""Per-clip automatic balance after the Sony LUT ("Tự cân màu"): white balance, black/white points with a gentle
S-curve, light vibrance that spares skin, and a small skin-hue nudge. Everything is fitted once on sampled frames
(display-referred Rec.709 RGB in [0, 1]) and baked into one .cube, so the clip is still encoded in one pass and the
correction cannot flicker. Limits keep it natural, with only a light saturation lift.
"""
from pathlib import Path

import numpy as np

LUT_SIZE = 33
LUMA = np.array([0.2126, 0.7152, 0.0722])

# targets and safety limits (IRE / 100)
BLACK_TARGET, WHITE_TARGET = 0.03, 0.93
MAX_BLACK_DROP = 0.08          # never pull blacks down more than 8 IRE
MAX_CONTRAST_GAIN = 1.25
MAX_CLIPPED = 0.005            # at most 0.5% of pixels above 98 IRE after correction
WB_AMOUNT, WB_MAX = 0.7, 0.06  # correct 70% of the measured cast, at most ±6% per channel
WB_MIN_NEUTRAL = 0.05          # under 5% near-neutral pixels -> loose fallback below, else skip
WB_LOOSE_SAT, WB_LOOSE_AMOUNT, WB_LOOSE_MAX = 0.25, 0.5, 0.04
S_CURVE = 0.12                 # blend towards smoothstep: mid-tone contrast without crushing ends
VIBRANCE = 0.12                # extra saturation for dull colours (natural, light)
SATURATION = 1.04              # small global lift
SKIN_HUE, SKIN_WIDTH = 125.0, 16.0   # vectorscope angle (deg) of the skin line and the protected band
SKIN_SAT_MAX = 1.04            # skin gets at most +4% saturation
SKIN_SHIFT_MAX = 3.0           # degrees; the colour-only skin detector also catches wood and blond fur
SKIN_MIN = 0.02


def _ycc(rgb):
    y = rgb @ LUMA
    return y, (rgb[:, 2] - y) / 1.8556, (rgb[:, 0] - y) / 1.5748


def _rgb(y, cb, cr):
    r = y + 1.5748 * cr
    b = y + 1.8556 * cb
    g = (y - LUMA[0] * r - LUMA[2] * b) / LUMA[1]
    return np.stack([r, g, b], axis=-1)


def _skin_weight(cb, cr):
    ang = (np.degrees(np.arctan2(cr, cb)) + 360) % 360
    chroma = np.hypot(cb, cr)
    w = np.exp(-((ang - SKIN_HUE) / SKIN_WIDTH) ** 2) * np.clip((chroma - 0.01) / 0.02, 0, 1)
    return w, ang, chroma


def measure(rgb: np.ndarray) -> dict:
    """Scope numbers used in the report (and by the web page)."""
    rgb = rgb.reshape(-1, 3)
    y, cb, cr = _ycc(rgb)
    sat = (rgb.max(1) - rgb.min(1)) / np.maximum(rgb.max(1), 1e-6)
    neu = (sat < 0.12) & (y > 0.25) & (y < 0.9)
    m = rgb[neu].mean(0) if neu.mean() > 0.01 else rgb.mean(0)
    w, ang, chroma = _skin_weight(cb, cr)
    sk = (w > 0.5) & (y > 0.2) & (y < 0.85)
    p = np.percentile(y, [0.5, 50, 99.5])
    return {"black_ire": round(p[0] * 100, 1), "mid_ire": round(p[1] * 100, 1), "white_ire": round(p[2] * 100, 1),
            "clipped_pct": round(float((y > 0.98).mean() * 100), 2),
            "wb_rg": round(float(m[0] / m[1]), 3), "wb_bg": round(float(m[2] / m[1]), 3),
            "saturation": round(float(np.median(sat)), 3), "chroma_p90": round(float(np.percentile(chroma, 90)), 4),
            "skin_pct": round(float(sk.mean() * 100), 1),
            "skin_hue": round(float(np.median(ang[sk])), 1) if sk.any() else None}


def fit(rgb: np.ndarray, strength: float = 1.0) -> dict:
    """Correction parameters for one clip from its sampled frames (after the Sony LUT)."""
    rgb = rgb.reshape(-1, 3)
    k = float(np.clip(strength, 0, 1))
    p = {"strength": k}
    # 1. white balance on near-neutral pixels, luma-preserving gains
    y = rgb @ LUMA
    sat = (rgb.max(1) - rgb.min(1)) / np.maximum(rgb.max(1), 1e-6)
    neu = (sat < 0.12) & (y > 0.25) & (y < 0.9)
    gains, amount, cap, p["wb_mode"] = np.ones(3), WB_AMOUNT, WB_MAX, "neutral"
    if neu.mean() < WB_MIN_NEUTRAL:
        # warm-lit scenes (e.g. a red object under yellow light) have almost no grey: fall back to the brighter,
        # mildly tinted surfaces (floor, walls) and correct more gently
        neu = (sat < WB_LOOSE_SAT) & (y > 0.4) & (y < 0.95)
        amount, cap, p["wb_mode"] = WB_LOOSE_AMOUNT, WB_LOOSE_MAX, "loose"
    if neu.mean() >= WB_MIN_NEUTRAL:
        m = rgb[neu].mean(0)
        gains = 1 + amount * k * (m[1] / m - 1)
        gains = np.clip(gains, 1 - cap, 1 + cap)
        gains /= gains @ LUMA
    else:
        p["wb_mode"] = "skipped"
    p["wb_gains"] = gains.round(4).tolist()
    p["wb_skipped"] = p["wb_mode"] == "skipped"
    # 2. black / white points (same affine map on every channel, so hue is kept)
    yw = (rgb * gains) @ LUMA
    lo, hi = np.percentile(yw, [0.5, 99.5])
    black = max(BLACK_TARGET, lo - MAX_BLACK_DROP)
    gain = np.clip((WHITE_TARGET - black) / max(hi - lo, 1e-3), 1.0, MAX_CONTRAST_GAIN)
    p.update(lo=float(lo), s_curve=S_CURVE * k, gain=float(1 + k * (gain - 1)), black=float(lo + k * (black - lo)))
    for _ in range(20):  # back off until highlights are not clipped
        if (apply(rgb, p) @ LUMA > 0.98).mean() <= MAX_CLIPPED or p["gain"] <= 1.0:
            break
        p["gain"] = max(1.0, p["gain"] - 0.02)
    # 3./4. vibrance and skin nudge
    p["vibrance"], p["saturation"] = VIBRANCE * k, 1 + (SATURATION - 1) * k
    yy, cb, cr = _ycc(apply(rgb, {**p, "vibrance": 0, "saturation": 1}))
    w, ang, _ = _skin_weight(cb, cr)
    sk = (w > 0.5) & (yy > 0.2) & (yy < 0.85)
    shift = 0.0
    if sk.mean() >= SKIN_MIN:
        shift = float(np.clip(SKIN_HUE - np.median(ang[sk]), -SKIN_SHIFT_MAX, SKIN_SHIFT_MAX)) * k
    p["skin_shift_deg"] = round(shift, 2)
    return p


def apply(rgb: np.ndarray, p: dict) -> np.ndarray:
    """Apply fitted parameters to Nx3 RGB in [0, 1]."""
    x = rgb.reshape(-1, 3) * np.asarray(p["wb_gains"])
    x = (x - p["lo"]) * p["gain"] + p["black"]
    x = np.clip(x, 0, 1)
    x = x + p["s_curve"] * (x * x * (3 - 2 * x) - x)
    y, cb, cr = _ycc(x)
    w, _, chroma = _skin_weight(cb, cr)
    f = p.get("saturation", 1.0) + p.get("vibrance", 0) * np.clip(1 - chroma / 0.2, 0, 1)
    f = f * (1 - w) + np.minimum(f, SKIN_SAT_MAX) * w
    cb, cr = cb * f, cr * f
    if p.get("skin_shift_deg"):
        a = np.radians(p["skin_shift_deg"]) * w
        cb, cr = cb * np.cos(a) - cr * np.sin(a), cb * np.sin(a) + cr * np.cos(a)
    return np.clip(_rgb(y, cb, cr), 0, 1)


def write_lut(p: dict, dst: Path) -> None:
    n = LUT_SIZE
    g = np.linspace(0.0, 1.0, n)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")          # red varies fastest in .cube order
    out = apply(np.stack([r, gg, b], axis=-1).reshape(-1, 3), p)
    lines = ["TITLE \"slog3grade auto balance\"", f"LUT_3D_SIZE {n}"] + [f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}" for v in out]
    dst.write_text("\n".join(lines) + "\n", encoding="ascii")
