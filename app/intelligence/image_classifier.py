"""Optional CPU MobileNet adapter; never treats an ImageNet label as incident verification."""
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load_model():
    from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small
    weights = MobileNet_V3_Small_Weights.DEFAULT
    model = mobilenet_v3_small(weights=weights)
    model.eval()
    return model, weights


def classify_image(path: str | Path) -> dict:
    try:
        import torch
        from PIL import Image
        model, weights = _load_model()
        image = Image.open(path).convert("RGB")
        tensor = weights.transforms()(image).unsqueeze(0)
        with torch.inference_mode():
            probabilities = model(tensor).softmax(dim=1)[0]
        values, indices = probabilities.topk(3)
        return {
            "available": True,
            "model": "torchvision-mobilenet-v3-small-imagenet",
            "labels": [{"label": weights.meta["categories"][int(i)], "confidence": round(float(v), 3)} for v, i in zip(values, indices, strict=True)],
            "warning": "Generic visual context only. Not trained to determine disaster damage or verify incidents.",
        }
    except Exception as exc:
        return {"available": False, "reason": f"Optional vision extra unavailable: {type(exc).__name__}"}
