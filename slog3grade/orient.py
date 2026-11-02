"""Guess whether a clip was shot with the camera turned sideways, using a local CLIP model (open_clip, runs offline,
no API). Many Sony bodies write no orientation metadata, so this replaces a person looking at thumbnails. Each sampled frame is scored as-is and turned 90° each way; CLIP rates how "upright" each looks.
"""
import io
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from .config import settings

UPRIGHT = ["a photo", "an upright photo of a scene", "a normal photo taken with the camera held straight"]
TILTED = ["a photo rotated 90 degrees", "a sideways photo", "a photo turned on its side", "an upside-down photo"]
CANDIDATES = {"none": 0, "ccw": 90, "cw": -90}   # PIL rotate() angles: positive = counter-clockwise
SAMPLE_POINTS = (0.25, 0.5, 0.75)
MIN_MARGIN = 0.01                                   # below this the guess is "unsure" and nothing is rotated


@lru_cache
def _clip():
    import open_clip
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k",
                                                                 device=device)
    return model, preprocess, open_clip.get_tokenizer("ViT-B-32"), device


def _frames(path: Path, duration: float) -> list[Image.Image]:
    s = settings()
    out = []
    for t in (SAMPLE_POINTS if duration > 1 else (0,)):
        raw = subprocess.run([s["ffmpeg"], "-hide_banner", "-loglevel", "error", "-ss", f"{duration * t:.2f}",
                              "-i", str(path), "-frames:v", "1", "-vf", "scale=336:336:force_original_aspect_ratio="
                              "decrease,format=rgb24", "-f", "image2pipe", "-vcodec", "png", "-"],
                             capture_output=True).stdout
        if raw:
            out.append(ImageOps.autocontrast(Image.open(io.BytesIO(raw)).convert("RGB"), cutoff=1))  # log is flat
    return out


def guess(path: Path, duration: float) -> dict:
    """{'rotate': 'none'|'ccw'|'cw', 'sure': bool, 'scores': {...}} for a clip as players display it."""
    import torch

    model, preprocess, tokenizer, device = _clip()
    frames = _frames(path, duration)
    if not frames:
        return {"rotate": "none", "sure": False, "scores": {}}
    with torch.no_grad():
        tf = model.encode_text(tokenizer(UPRIGHT + TILTED).to(device))
        tf = tf / tf.norm(dim=-1, keepdim=True)
        scores = {}
        for name, angle in CANDIDATES.items():
            imgs = torch.stack([preprocess(f.rotate(angle, expand=True)) for f in frames]).to(device)
            f = model.encode_image(imgs)
            f = f / f.norm(dim=-1, keepdim=True)
            sim = (f @ tf.T).float().cpu().numpy()
            scores[name] = float(np.mean(sim[:, :len(UPRIGHT)].mean(1) - sim[:, len(UPRIGHT):].mean(1)))
    best = max(scores, key=scores.get)
    second = max(v for k, v in scores.items() if k != best)
    sure = scores[best] - second >= MIN_MARGIN
    return {"rotate": best if sure else "none", "sure": sure, "scores": {k: round(v, 4) for k, v in scores.items()}}
