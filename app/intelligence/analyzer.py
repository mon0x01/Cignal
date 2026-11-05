"""CPU-friendly text triage and report similarity helpers.

The included classifier is intentionally conservative. Its output is a triage hint,
not an emergency determination; an analyst must verify every incident.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

from rapidfuzz.fuzz import token_set_ratio

from app.core.config import settings

TRAINING = [
    ("flood", "flood water rising river overflow inundated street heavy rain जलभराव बाढ़ पानी भर गया પૂર વરસાદ પાણી ભરાયું"),
    ("flood", "bridge submerged homes underwater flash flood drainage overflow बाढ़ का पानी घर में ઘરમાં પાણી ભરાયું"),
    ("earthquake", "earthquake tremor shaking building collapse aftershock ભૂકંપ ધરતીકંપ भूकंप तेज झटके"),
    ("earthquake", "ground shaking walls cracked seismic activity tremor aftershock जमीन हिली धरતી ધ્રૂજી"),
    ("fire", "fire flames smoke burning building wildfire forest आग लगी धुआँ આગ ધુમાડો"),
    ("fire", "industrial blaze electrical short circuit forest fire smoke column आग धधक रही છે આગ લાગી"),
    ("cyclone", "cyclone hurricane typhoon storm strong wind coastal surge ચક્રવાત વાવાઝોડું चक्रवात तेज हवा"),
    ("cyclone", "severe storm wind gusts sea surge coastal warning તોફાન समुद्री तूफान तेज हवाएं"),
    ("landslide", "landslide mudslide rockfall slope collapse debris mountain road भूस्खलन ભૂસ્ખલન પહાડ ધસ્યો"),
    ("landslide", "hill collapsed debris blocked road slope failure मिट्टी धस गई પર્વત ધસી પડ્યો"),
    ("other", "power outage road blocked medical help missing people shelter request बिजली नहीं सड़क बंद મદદ જોઈએ"),
    ("other", "water shortage evacuation assistance relief supplies medical emergency સહાય આશ્રય દવા"),
    ("flood", "river overflow submerged houses streets under water flash flooding जलमग्न मकान बाढ़ का पानी नदी उफान पर"),
    ("flood", "heavy rainfall waterlogging drainage failure flooded underpass सड़क पर पानी भर गया પૂરનું પાણી"),
    ("flood", "dam water release village inundated people evacuated પાણી ગામમાં ઘૂસ્યું નદી છલકાઈ"),
    ("flood", "coastal inundation storm surge tidal flooding low lying neighbourhoods समुद्र का पानी बस्ती में ઘૂસ્યું"),
    ("flood", "પાણીનું સ્તર વધી રહ્યું છે નદી કાંઠે પાણી ઘરોમાં ભરાયું ભારે વરસાદ"),
    ("flood", "બ્રિજ પાણીમાં ડૂબ્યો રસ્તા પર પૂરનું પાણી ભરાયું"),
    ("earthquake", "strong earthquake felt buildings shaking cracked walls tremors भूकंप के झटके मकान हिल रहे हैं"),
    ("earthquake", "aftershock seismic event ground shaking damaged structures ભૂકંપના આંચકા દીવાલોમાં તિરાડ"),
    ("earthquake", "જોરદાર ભૂકંપથી મકાનો ધ્રૂજ્યા અને રસ્તામાં તિરાડ પડી"),
    ("earthquake", "earthquake damaged building debris falling people felt tremor धरती कांपी"),
    ("earthquake", "ભૂકંપના કારણે ઇમારતને નુકસાન અને જમીનમાં તિરાડો"),
    ("earthquake", "भूमि में कंपन मकान की दीवारों में दरार भूकंप के बाद झटके"),
    ("fire", "warehouse fire thick black smoke flames spreading firefighters आग की लपटें गोदाम में धुआँ"),
    ("fire", "forest wildfire burning trees smoke plume grass fire જંગલમાં આગ ધુમાડો"),
    ("fire", "electrical transformer fire sparks burning cable industrial blaze આગ લાગી વીજ તાર સળગ્યો"),
    ("fire", "residential building on fire flames from upper floors ધૂમાડો આગ ઇમારતમાં લાગી"),
    ("fire", "ઘરમાં આગ લાગી ધુમાડો ફેલાયો ફાયર બ્રિગેડ બોલાવો"),
    ("fire", "आग लगने से दुकान जल रही है धुआं फैल रहा है"),
    ("cyclone", "cyclonic winds coastal storm landfall evacuation warning hurricane gusts चक्रवात तटीय इलाकों में तेज हवा"),
    ("cyclone", "typhoon severe wind coastal flooding storm surge વાવાઝોડાની ચેતવણી દરિયાકાંઠે ભારે પવન"),
    ("cyclone", "વાવાઝોડાથી વૃક્ષો ધરાશાયી દરિયાઈ મોજાં ઊંચાં"),
    ("cyclone", "strong winds damaged roofs coastal evacuation cyclone warning समुद्री तूफान"),
    ("cyclone", "ચક્રવાતના કારણે ભારે પવન અને મકાનોની છતને નુકસાન"),
    ("cyclone", "तेज तूफानी हवा समुद्र में ऊंची लहरें तटीय चेतावनी"),
    ("landslide", "mud and rocks fell across mountain road slope collapsed debris blocked highway पहाड़ से मलबा गिरा"),
    ("landslide", "landslide buried houses hill collapse falling rocks ભૂસ્ખલન રસ્તો બંધ"),
    ("landslide", "પર્વતનો ભાગ ધસી પડ્યો રસ્તા પર કાટમાળ અને પથ્થરો પડ્યા"),
    ("landslide", "heavy rain triggered landslide soil sliding mountain village ભूस्खलन मिट्टी खिसकी"),
    ("landslide", "ભારે વરસાદ પછી ડુંગર ધસી પડ્યો મકાનોને જોખમ"),
    ("landslide", "पहाड़ी का हिस्सा गिरा सड़क मलबे से बंद भूस्खलन"),
    ("other", "road traffic blocked bridge closed medical assistance needed shelter supplies missing persons રસ્તો બંધ સહાય જોઈએ"),
    ("other", "power outage drinking water shortage request for medicine relief camp પરિવારોને મદદ અને પાણી જોઈએ"),
    ("other", "people need food shelter drinking water and medical supplies રાહત સામગ્રીની જરૂર"),
    ("other", "સહાય કેન્દ્રમાં દવા અને પીવાના પાણીની જરૂર છે"),
    ("other", "बिजली गुल है दवा और पीने के पानी की जरूरत राहत सहायता"),
    ("other", "bridge inspection traffic diversion emergency shelter information required"),
]

URGENT_TERMS = {
    "critical": ["trapped", "collapsed", "missing", "life threatening", "casualt", "rescue", "urgent", "evacuate", "मलबे", "फंसे", "बचाओ", "घायल", "તાત્કાલિક", "ફસાયેલા", "બચાવો", "ઇજા"],
    "high": ["rising fast", "major damage", "large fire", "severe", "washed away", "injured", "भारी", "तेज", "नુકસાન", "ભારે વરસાદ", "ગંભીર"],
    "moderate": ["blocked", "disrupted", "warning", "smoke", "shaking", "जलभराव", "ચેતવણી", "પાણી ભરાયું"],
}

CITY_COORDS = {
    "ahmedabad": (23.0225, 72.5714, "Ahmedabad, Gujarat"),
    "અમદાવાદ": (23.0225, 72.5714, "Ahmedabad, Gujarat"),
    "अहमदाबाद": (23.0225, 72.5714, "Ahmedabad, Gujarat"),
    "surat": (21.1702, 72.8311, "Surat, Gujarat"),
    "સુરત": (21.1702, 72.8311, "Surat, Gujarat"),
    "vadodara": (22.3072, 73.1812, "Vadodara, Gujarat"),
    "વડોદરા": (22.3072, 73.1812, "Vadodara, Gujarat"),
    "rajkot": (22.3039, 70.8022, "Rajkot, Gujarat"),
    "bhuj": (23.2419, 69.6669, "Bhuj, Gujarat"),
    "mumbai": (19.0760, 72.8777, "Mumbai, Maharashtra"),
    "मुंबई": (19.0760, 72.8777, "Mumbai, Maharashtra"),
    "delhi": (28.6139, 77.2090, "Delhi"),
    "दिल्ली": (28.6139, 77.2090, "Delhi"),
    "chennai": (13.0827, 80.2707, "Chennai, Tamil Nadu"),
    "kolkata": (22.5726, 88.3639, "Kolkata, West Bengal"),
    "bengaluru": (12.9716, 77.5946, "Bengaluru, Karnataka"),
    "pune": (18.5204, 73.8567, "Pune, Maharashtra"),
    "hyderabad": (17.3850, 78.4867, "Hyderabad, Telangana"),
    "गांधीनगर": (23.2156, 72.6369, "Gandhinagar, Gujarat"),
    "gandhinagar": (23.2156, 72.6369, "Gandhinagar, Gujarat"),
    "jamnagar": (22.4707, 70.0577, "Jamnagar, Gujarat"),
    "ભાવનગર": (21.7645, 72.1519, "Bhavnagar, Gujarat"),
    "bhavnagar": (21.7645, 72.1519, "Bhavnagar, Gujarat"),
    "indore": (22.7196, 75.8577, "Indore, Madhya Pradesh"),
    "lucknow": (26.8467, 80.9462, "Lucknow, Uttar Pradesh"),
    "guwahati": (26.1445, 91.7362, "Guwahati, Assam"),
    "srinagar": (34.0837, 74.7973, "Srinagar, Jammu and Kashmir"),
    "jaipur": (26.9124, 75.7873, "Jaipur, Rajasthan"),
}


@lru_cache(maxsize=1)
def _classifier():
    if not settings.ai_enable_sklearn:
        return None
    try:
        if settings.ai_text_model_path.is_file():
            import joblib
            # The artifact path is administrator-configured and mounted from local model storage.
            bundle = joblib.load(settings.ai_text_model_path)  # nosec B301
            pipeline = bundle.get("pipeline") if isinstance(bundle, dict) else None
            categories = bundle.get("categories") if isinstance(bundle, dict) else None
            known_categories = {"flood", "earthquake", "fire", "cyclone", "landslide", "other"}
            if (
                isinstance(bundle, dict)
                and bundle.get("model_name") == "cignal-disaster-text-classifier"
                and isinstance(categories, list)
                and len(categories) >= 2
                and set(categories).issubset(known_categories)
                and pipeline is not None
                and callable(getattr(pipeline, "predict_proba", None))
                and callable(getattr(pipeline, "predict", None))
            ):
                return {
                    "kind": "operator_trained_artifact",
                    "model": pipeline,
                    "version": str(bundle.get("model_version", "custom")),
                }
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        texts = [text for _label, text in TRAINING]
        labels = [label for label, _text in TRAINING]
        vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(2, 5), min_df=1, max_features=12000)
        matrix = vectorizer.fit_transform(texts)
        model = LogisticRegression(max_iter=500, class_weight="balanced", random_state=42)
        model.fit(matrix, labels)
        return {"kind": "builtin_baseline", "vectorizer": vectorizer, "model": model, "version": "builtin-v1"}
    except Exception:
        return None

@lru_cache(maxsize=1)
def _semantic_model():
    if not settings.ai_enable_pretrained_nlp:
        return None
    try:
        from sentence_transformers import SentenceTransformer
        # Never trigger a surprise network download from a live API/worker
        # process. Operators may pre-stage the model in the local Hugging Face
        # cache or point ai_nlp_model_id at a local model directory.
        model = SentenceTransformer(
            settings.ai_nlp_model_id,
            device="cpu",
            local_files_only=True,
        )
        examples = [sample for _label, sample in TRAINING]
        embeddings = model.encode(examples, normalize_embeddings=True)
        return model, embeddings
    except Exception:
        return None


def classify_category(text: str) -> tuple[str, float]:
    normalized = text.casefold()
    semantic = _semantic_model()
    if semantic:
        try:
            import numpy as np
            model, reference = semantic
            sample = model.encode([normalized], normalize_embeddings=True)[0]
            labels = [label for label, _sample in TRAINING]
            scores = {}
            for label in sorted(set(labels)):
                indices = [index for index, item in enumerate(labels) if item == label]
                scores[label] = float(np.mean(reference[indices] @ sample))
            best = max(scores, key=scores.get)
            confidence = max(0.0, min(0.99, (scores[best] + 1.0) / 2.0))
            return best, confidence
        except Exception:
            pass
    model = _classifier()
    if model:
        try:
            classifier = model["model"]
            if model["kind"] == "operator_trained_artifact":
                probabilities = classifier.predict_proba([normalized])[0]
            else:
                probabilities = classifier.predict_proba(model["vectorizer"].transform([normalized]))[0]
            index = int(probabilities.argmax())
            return str(classifier.classes_[index]), float(probabilities[index])
        except Exception:
            pass
    rule_terms = {
        "flood": ["flood", "water rising", "inundat", "बाढ़", "पूर"],
        "earthquake": ["earthquake", "tremor", "shaking", "भूकंप", "ભૂકંપ"],
        "fire": ["fire", "flame", "smoke", "आग", "આગ"],
        "cyclone": ["cyclone", "hurricane", "storm", "ચક્રવાત", "तूफान"],
        "landslide": ["landslide", "mudslide", "भूस्खलन", "ભૂસ્ખલન"],
    }
    for category, terms in rule_terms.items():
        if any(term in normalized for term in terms):
            return category, 0.55
    return "other", 0.35


def classify_urgency(text: str) -> tuple[str, float]:
    normalized = text.casefold()
    for severity in ("critical", "high", "moderate"):
        if any(term in normalized for term in URGENT_TERMS[severity]):
            return severity, {"critical": 0.84, "high": 0.72, "moderate": 0.58}[severity]
    return "low", 0.45


def extract_location(text: str) -> tuple[float | None, float | None, str | None]:
    coordinate_match = re.search(r"(?<!\d)(-?\d{1,2}\.\d{3,})\s*[,;/ ]\s*(-?\d{1,3}\.\d{3,})(?!\d)", text)
    if coordinate_match:
        lat, lon = float(coordinate_match.group(1)), float(coordinate_match.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon, f"Coordinates {lat:.4f}, {lon:.4f}"
    lowered = text.lower()
    for city, (lat, lon, label) in CITY_COORDS.items():
        if city in lowered:
            return lat, lon, label
    return None, None, None


def extract_text_signals(text: str) -> dict:
    normalized = text.casefold()
    category_terms = {
        "flood": ["flood", "waterlogging", "inundat", "बाढ़", "પૂર", "પાણી ભરાયું"],
        "earthquake": ["earthquake", "tremor", "seismic", "भूकंप", "ભૂકંપ"],
        "fire": ["fire", "flames", "smoke", "आग", "આગ"],
        "cyclone": ["cyclone", "hurricane", "typhoon", "વાવાઝોડું", "चक्रवात"],
        "landslide": ["landslide", "mudslide", "भूस्खलन", "ભૂસ્ખલન"],
    }
    matched_categories = {
        category: [term for term in terms if term in normalized]
        for category, terms in category_terms.items()
    }
    matched_categories = {key: value for key, value in matched_categories.items() if value}
    matched_urgency = {}
    for severity, terms in URGENT_TERMS.items():
        matches = [term for term in terms if term in normalized]
        if matches:
            matched_urgency[severity] = matches
    return {"category_terms": matched_categories, "urgency_terms": matched_urgency}


def analyze_report(title: str, description: str, declared_language: str = "en") -> dict:
    combined = f"{title}. {description}"
    category, cat_confidence = classify_category(combined)
    urgency, urgency_confidence = classify_urgency(combined)
    lat, lon, location = extract_location(combined)
    signals = extract_text_signals(combined)
    return {
        "category": category,
        "urgency": urgency,
        "confidence": round(cat_confidence, 3),
        "urgency_confidence": round(urgency_confidence, 3),
        "latitude": lat,
        "longitude": lon,
        "location_text": location,
        "language": declared_language,
        "signals": signals,
        "summary": f"Triage hint: {category} / {urgency}. Human review required.",
        "method": "sentence-transformer-semantic-similarity" if _semantic_model() else (
            "operator-trained-text-model" if (_classifier() or {}).get("kind") == "operator_trained_artifact" else
            "tfidf-character-ngram-logistic-regression" if _classifier() else "keyword-fallback"
        ),
        "model_version": (_classifier() or {}).get("version", "keyword-fallback"),
        "confidence_semantics": "uncalibrated model score; not a probability or verification",
    }


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def similarity_score(first: str, second: str) -> float:
    return float(token_set_ratio(first, second))
