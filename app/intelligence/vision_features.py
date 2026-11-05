"""Deterministic CPU feature extraction for operator-trained image triage models."""
from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from app.intelligence.vision import MAX_IMAGE_PIXELS, ImageEvidenceError

FEATURE_VERSION = "hog-rgb-v1"
IMAGE_SIZE = 128
CELL_SIZE = 8
ORIENTATION_BINS = 9


def extract_visual_features(content: bytes) -> np.ndarray:
    """Return normalized HOG + RGB histograms; no pretrained weights/network access."""
    if not isinstance(content, bytes) or not content:
        raise ImageEvidenceError("Image is empty")
    try:
        with Image.open(BytesIO(content)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ImageEvidenceError("Image exceeds the pixel processing limit")
            image = ImageOps.exif_transpose(source).convert("RGB").resize(
                (IMAGE_SIZE, IMAGE_SIZE), Image.Resampling.LANCZOS
            )
    except ImageEvidenceError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageEvidenceError("Image content failed validation") from exc

    rgb = np.asarray(image, dtype=np.float32) / 255.0
    gray = rgb.mean(axis=2)
    grad_y, grad_x = np.gradient(gray)
    magnitude = np.hypot(grad_x, grad_y)
    angle = np.mod(np.degrees(np.arctan2(grad_y, grad_x)), 180.0)
    bin_width = 180.0 / ORIENTATION_BINS
    orientation = np.floor(angle / bin_width).astype(np.int32)
    orientation = np.clip(orientation, 0, ORIENTATION_BINS - 1)

    cells = IMAGE_SIZE // CELL_SIZE
    histograms = np.zeros((cells, cells, ORIENTATION_BINS), dtype=np.float32)
    for cell_y in range(cells):
        y0 = cell_y * CELL_SIZE
        for cell_x in range(cells):
            x0 = cell_x * CELL_SIZE
            cell_bins = orientation[y0:y0 + CELL_SIZE, x0:x0 + CELL_SIZE].ravel()
            cell_magnitude = magnitude[y0:y0 + CELL_SIZE, x0:x0 + CELL_SIZE].ravel()
            histograms[cell_y, cell_x] = np.bincount(
                cell_bins, weights=cell_magnitude, minlength=ORIENTATION_BINS
            )[:ORIENTATION_BINS]

    hog_blocks: list[np.ndarray] = []
    for cell_y in range(cells - 1):
        for cell_x in range(cells - 1):
            block = histograms[cell_y:cell_y + 2, cell_x:cell_x + 2].reshape(-1)
            normalized = block / np.sqrt(float(np.dot(block, block)) + 1e-6)
            normalized = np.minimum(normalized, 0.2)
            normalized = normalized / np.sqrt(float(np.dot(normalized, normalized)) + 1e-6)
            hog_blocks.append(normalized)

    color_features = []
    for channel in range(3):
        histogram, _ = np.histogram(rgb[:, :, channel], bins=8, range=(0.0, 1.0))
        normalized = histogram.astype(np.float32) / float(IMAGE_SIZE * IMAGE_SIZE)
        color_features.append(normalized)

    descriptor = np.concatenate([np.concatenate(hog_blocks), *color_features]).astype(np.float32)
    if not np.isfinite(descriptor).all():
        raise ImageEvidenceError("Image feature extraction produced non-finite values")
    return descriptor
