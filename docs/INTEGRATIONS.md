# External data integrations

## Connector design

Each connector implements `Connector.configured()` and `Connector.fetch()` and returns normalized `NormalizedSignal` values. The ingestion worker upserts by `(source_type, source_record_id)`, records provider/source URL and provenance, and queues each new report for text triage. Connectors are isolated: one provider failure is logged and does not prevent other sources from importing.

The UI's READY status means the local connector has enough configuration to attempt a request. It does not mean the provider was reachable during the last poll or that a provider record has been verified as an incident.

## Bundled sources

| Source | Capability | Setup / constraint |
|---|---|---|
| USGS | Public earthquake GeoJSON feed | No API key in the included public feed. Event magnitude/location is not an impact assessment. |
| GDACS | Public disaster alert RSS | Public upstream feed; network/provider availability may vary. |
| Open-Meteo | Current conditions and forecast context | No key for eligible free non-commercial use; attribution required. Its data is returned as weather context, not converted into verified incident reports. Review current terms before commercial use. |
| IMD | Configurable India Meteorological Department API | Set `IMD_API_BASE_URL` to the exact endpoint granted to your account and `IMD_API_TOKEN`. The adapter intentionally does not guess undocumented API paths or scrape a portal. Access may require registration/approval. |
| India government / NDMA / state | Configurable government JSON endpoint | Set `INDIA_GOV_API_BASE_URL` and optional `INDIA_GOV_API_TOKEN`. Supply only an official endpoint you are permitted to access; the adapter expects an `alerts` or `data` list of JSON objects. It does not invent an NDMA endpoint. |
| Bhuvan / Indian geospatial data | Configurable GeoJSON feature feed | Set `BHUVAN_GEOJSON_URL` and optional `BHUVAN_API_TOKEN` to an endpoint you are permitted to use. Feature geometry must be GeoJSON coordinates; preserve the source's metadata and license. No undocumented Bhuvan service is assumed. |
| NASA FIRMS | Satellite active-fire detections | Register for a free `MAP_KEY`, set `NASA_FIRMS_MAP_KEY`. Detections indicate satellite observations, not verified fire impact or response need. |
| Permitted RSS | Admin-configured authorized feeds | Set `AUTHORIZED_RSS_FEEDS` to comma-separated feed URLs that you are allowed to process. No source is enabled automatically. |
| Social API | Generic authorized JSON API adapter | Set `SOCIAL_API_BASE_URL` and `SOCIAL_API_TOKEN`. The configured provider must return a JSON object with `posts`; see `app/integrations/social_api.py`. No scraping, login automation or access-control bypass is performed. |

The Open-Meteo connector is also exposed by the `GET /api/weather/forecast?latitude=...&longitude=...` route. It is forecast context only. Historical datasets can be uploaded through the Data Sources page as UTF-8 CSV, with `title` and `description` required and optional coordinates, category, urgency, language, location and source URL columns. Imported records are marked `historical`.

## Provenance meanings

- `live`: upstream API/feed item fetched during ingestion.
- `historical`: imported from a historical CSV/dataset.
- `simulated`: seeded demonstration record; never a live observation.
- `submitted`: citizen-provided record.

Do not combine these into a single unlabeled source category. Preserve provider attribution and licensing at display/export time.

## Optional AI dependencies

The default model is a small character n-gram TF-IDF + Logistic Regression classifier trained at runtime from a compact, built-in English/Hindi/Gujarati seed set. It has no external model download and uses CPU. Its outputs/confidence are only heuristic triage aids and should not be interpreted as calibrated probabilities.

Optional extras:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-ml.txt
```

For the optional generic image model, install a matching CPU-only `torch`/`torchvision` pair using the [PyTorch CPU wheel index](https://download.pytorch.org/whl/cpu). Do not install CUDA packages on the integrated-graphics workstation.

Then set `AI_ENABLE_PRETRAINED_NLP=true` to enable the configured multilingual SentenceTransformer (default: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`). It downloads model weights at first load and uses CPU. Set `AI_ENABLE_TORCHVISION=true` to enable MobileNet V3 Small image labels. The generic ImageNet model provides broad visual context only; it is not a flood, damage, injury or disaster classifier. The worker stores that output as evidence metadata with a warning.

The first model load requires internet and disk space; plan cache location and memory use before enabling it on the 16 GB workstation. Keep both flags disabled until you want the optional model weights.


## Runtime source health

The Data Sources page shows runtime health from `/api/integrations/health`; configuration readiness is not presented as proof of reachability. Each configured connector run is persisted in `integration_runs`. See `INGESTION_OBSERVABILITY.md` for run states, sanitized error handling, rejection metrics and provider timestamp behavior.


## Source governance and disaster data acquisition

For the provider catalogue, NDMA SACHET CAP-XML ETag behavior, NASA EONET v3, source-level licence status and provenance snapshots in research datasets, see [Data Acquisition and Source Governance v1](DATA_ACQUISITION_V1.md).
