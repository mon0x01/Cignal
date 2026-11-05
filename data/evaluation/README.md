# Event-pair evaluation data

Use one JSON object per labelled report pair. Required fields are `split_group`,
`text_a`, `text_b`, and `same_event` (`1` for same real-world event, `0` otherwise).
Optional fields include `disaster_type_a/b`, `latitude_a/b`, `longitude_a/b`,
`observed_at_a/b` (ISO 8601), and `language_a/b`.

`split_group` must identify a disaster/event/campaign grouping that must never be
split between training and holdout. It is not a pair ID. The training script uses
GroupShuffleSplit and fails closed if it cannot produce both labels in both sides.

Do not use the synthetic fixture in `tests/fixtures` to claim real-world model
quality. Hold out entire incidents, geographic regions or time periods. Keep a
separate final test set untouched until model selection is finished. Record label
provenance and reviewer identity in your dataset governance process; avoid
including personally identifying citizen data in training exports.
