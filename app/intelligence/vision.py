"""CPU-first image evidence analysis.

This module extracts reproducible image-quality, integrity and location metadata.
It does not infer that an image proves a disaster. Optional ImageNet labels are
exposed as generic visual context only, never as damage verification.
"""
from __future__ import annotations

import hashlib
import tempfile
from functools import lru_cache
from io import BytesIO
from typing import Any

import numpy as np
from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from app.core.config import settings

MAX_IMAGE_PIXELS = 40_000_000
ANALYSIS_MAX_EDGE = 320


class ImageEvidenceError(ValueError):
    """Raised when image bytes are invalid or exceed safe processing limits."""


def _rational(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        if isinstance(value, (tuple, list)) and len(value) == 2 and value[1]:
            return float(value[0]) / float(value[1])
        raise


def _dms_to_decimal(value: Any, reference: Any) -> float:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError("Invalid GPS coordinate")
    degrees, minutes, seconds = (_rational(part) for part in value)
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    ref = str(reference).upper()
    if ref in {"S", "W"}:
        decimal = -decimal
    elif ref not in {"N", "E"}:
        raise ValueError("Invalid GPS reference")
    return decimal


def gps_ifd_to_coordinates(gps_ifd: dict | None) -> dict | None:
    """Extract only latitude/longitude; never retain camera serials or raw EXIF."""
    if not isinstance(gps_ifd, dict):
        return None
    try:
        latitude = _dms_to_decimal(gps_ifd[2], gps_ifd[1])
        longitude = _dms_to_decimal(gps_ifd[4], gps_ifd[3])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {
        "latitude": round(latitude, 6),
        "longitude": round(longitude, 6),
        "source": "image_exif_gps",
        "confidence": 0.85,
    }


def extract_image_metadata(content: bytes) -> dict:
    """Validate image bytes and extract safe dimensions plus optional EXIF GPS."""
    if not isinstance(content, bytes) or not content:
        raise ImageEvidenceError("Image is empty")
    try:
        with Image.open(BytesIO(content)) as original:
            if original.width * original.height > MAX_IMAGE_PIXELS:
                raise ImageEvidenceError("Image exceeds the pixel processing limit")
            exif = original.getexif()
            gps_ifd = None
            try:
                gps_ifd = exif.get_ifd(34853)
            except (AttributeError, KeyError, TypeError, ValueError):
                gps_tag = next((tag for tag, name in ExifTags.TAGS.items() if name == "GPSInfo"), None)
                if gps_tag is not None:
                    gps_ifd = exif.get(gps_tag)
            oriented = ImageOps.exif_transpose(original)
            result = {
                "width": int(oriented.width),
                "height": int(oriented.height),
                "format": str(original.format or "unknown").upper(),
                "exif_stripped": True,
            }
            gps = gps_ifd_to_coordinates(gps_ifd)
            if gps:
                result["exif_location"] = gps
            return result
    except ImageEvidenceError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageEvidenceError("Image content failed validation") from exc


def _average_hash(image: Image.Image) -> str:
    pixels = np.asarray(image.convert("L").resize((8, 8), Image.Resampling.LANCZOS), dtype=np.float32)
    mean = float(pixels.mean())
    bits = pixels >= mean
    value = 0
    for bit in bits.flat:
        value = (value << 1) | int(bool(bit))
    return f"{value:016x}"


def hash_distance(first: str, second: str) -> int:
    """Hamming distance between 64-bit average perceptual hashes."""
    if len(first) != 16 or len(second) != 16:
        raise ValueError("Perceptual hashes must be 16 hexadecimal characters")
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError as exc:
        raise ValueError("Perceptual hashes must be hexadecimal") from exc


def _image_statistics(image: Image.Image) -> dict:
    working = image.copy()
    working.thumbnail((ANALYSIS_MAX_EDGE, ANALYSIS_MAX_EDGE), Image.Resampling.LANCZOS)
    gray = np.asarray(working.convert("L"), dtype=np.float32) / 255.0
    brightness = float(gray.mean())
    contrast = float(gray.std())
    histogram = np.histogram(gray, bins=32, range=(0.0, 1.0))[0].astype(np.float64)
    probabilities = histogram / max(float(histogram.sum()), 1.0)
    nonzero = probabilities[probabilities > 0]
    entropy = float(-(nonzero * np.log2(nonzero)).sum())

    if min(gray.shape) >= 3:
        laplacian = (
            -4.0 * gray[1:-1, 1:-1]
            + gray[:-2, 1:-1]
            + gray[2:, 1:-1]
            + gray[1:-1, :-2]
            + gray[1:-1, 2:]
        )
        sharpness = float(laplacian.var())
    else:
        sharpness = 0.0

    return {
        "brightness_mean": round(brightness, 4),
        "contrast_stddev": round(contrast, 4),
        "entropy_bits": round(entropy, 4),
        "laplacian_variance": round(sharpness, 6),
        "average_hash": _average_hash(working),
    }


def _optional_visual_context(content: bytes) -> dict:
    """Run the optional generic ImageNet adapter only when explicitly enabled."""
    if not settings.ai_enable_torchvision:
        return {"available": False, "reason": "disabled"}
    try:
        from app.intelligence.image_classifier import classify_image

        with tempfile.NamedTemporaryFile(suffix=".jpg") as temporary:
            temporary.write(content)
            temporary.flush()
            result = classify_image(temporary.name)
        # Explicitly downgrade this to scene context; ImageNet is not disaster-trained.
        result["interpretation"] = "generic_visual_context_only"
        result["disaster_damage_assessment"] = False
        return result
    except Exception as exc:  # optional dependency/model failures must not break report triage
        return {"available": False, "reason": type(exc).__name__}



@lru_cache(maxsize=1)
def _operator_vision_model():
    """Load only a Cignal image-model artifact from the trusted local model volume."""
    if not settings.ai_vision_model_path.is_file():
        return None
    try:
        import joblib
        # The artifact path is administrator-configured and mounted from local model storage.
        bundle = joblib.load(settings.ai_vision_model_path)  # nosec B301
        classifier = bundle.get("classifier") if isinstance(bundle, dict) else None
        categories = bundle.get("categories") if isinstance(bundle, dict) else None
        from app.intelligence.vision_features import FEATURE_VERSION
        from app.intelligence.vision_training import ALLOWED_CATEGORIES
        if (
            isinstance(bundle, dict)
            and bundle.get("model_name") == "cignal-disaster-image-classifier"
            and bundle.get("feature_version") == FEATURE_VERSION
            and isinstance(categories, list)
            and len(categories) >= 2
            and set(categories).issubset(ALLOWED_CATEGORIES)
            and classifier is not None
            and callable(getattr(classifier, "predict_proba", None))
        ):
            return {
                "classifier": classifier,
                "categories": categories,
                "version": str(bundle.get("model_version", "unknown")),
                "evaluation": bundle.get("evaluation", {}),
            }
    except Exception:
        # A missing, incompatible or unreadable optional model never blocks report intake.
        return None
    return None


def _operator_visual_classification(content: bytes) -> dict:
    model = _operator_vision_model()
    if model is None:
        return {"available": False, "reason": "no_operator_trained_model"}
    try:
        from app.intelligence.vision_features import extract_visual_features
        descriptor = extract_visual_features(content)
        probabilities = model["classifier"].predict_proba([descriptor])[0]
        classes = list(model["classifier"].classes_)
        ranked = sorted(
            ((str(label), float(score)) for label, score in zip(classes, probabilities, strict=True)),
            key=lambda item: item[1], reverse=True,
        )
        return {
            "available": True,
            "candidate_label": ranked[0][0],
            "candidate_score": round(ranked[0][1], 4),
            "candidate_scores": [
                {"label": label, "score": round(score, 4)} for label, score in ranked[:5]
            ],
            "model_version": model["version"],
            "holdout_accuracy": model["evaluation"].get("holdout_accuracy"),
            "holdout_macro_f1": model["evaluation"].get("holdout_macro_f1"),
            "score_semantics": "uncalibrated model score; not a probability of disaster or damage",
            "human_review_required": True,
            "verified": False,
        }
    except Exception as exc:
        return {"available": False, "reason": type(exc).__name__}

def analyze_image_bytes(content: bytes, *, stored_metadata: dict | None = None) -> dict:
    """Analyze one stored image with safe CPU-bounded deterministic features."""
    if not isinstance(content, bytes) or not content:
        raise ImageEvidenceError("Image is empty")
    digest = hashlib.sha256(content).hexdigest()
    try:
        with Image.open(BytesIO(content)) as source:
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise ImageEvidenceError("Image exceeds the pixel processing limit")
            original_format = str(source.format or "unknown").upper()
            image = ImageOps.exif_transpose(source).convert("RGB")
            width, height = image.size
            statistics = _image_statistics(image)
    except ImageEvidenceError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageEvidenceError("Stored image cannot be decoded") from exc

    flags: list[str] = []
    if width < 640 or height < 480:
        flags.append("low_resolution")
    if statistics["brightness_mean"] < 0.12:
        flags.append("underexposed")
    elif statistics["brightness_mean"] > 0.94:
        flags.append("overexposed")
    if statistics["contrast_stddev"] < 0.055:
        flags.append("low_contrast")
    if statistics["laplacian_variance"] < 0.0008:
        flags.append("possible_blur")

    gps = (stored_metadata or {}).get("exif_location")
    valid_gps = None
    if isinstance(gps, dict):
        try:
            lat = float(gps["latitude"])
            lon = float(gps["longitude"])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                valid_gps = {
                    "latitude": round(lat, 6),
                    "longitude": round(lon, 6),
                    "source": "image_exif_gps",
                    "confidence": 0.85,
                }
        except (KeyError, TypeError, ValueError):
            pass

    return {
        "available": True,
        "engine": "cignal-cpu-vision-v1",
        "sha256": digest,
        "width": width,
        "height": height,
        "format": original_format,
        "quality": statistics,
        "quality_flags": flags,
        "visual_classification": _operator_visual_classification(content),
        "exif_location": valid_gps,
        "visual_context": _optional_visual_context(content),
        "limitations": [
            "Image quality and generic visual context are not proof of disaster damage.",
            "No person identification or sensitive-attribute inference is performed.",
        ],
    }
