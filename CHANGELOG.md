# Changelog

## 0.1.0

First release.

- `GraphClient` with `get`, `post`, `patch`, `delete`, pagination and read-only batches.
- `ReadRetryPolicy`, `ReadBudget` and `read_budget` for bounded, nestable read retries.
- Host pinning, fragment and dot-segment refusal, and no followed redirects.
- `graph_object_id` and `graph_object_segment` for validating directory object IDs.
- `on_failure` and `on_retry` callbacks with log-safe events.
