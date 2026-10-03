# PERF_BASELINE (C0) — 2026-09-29

Environment: sandbox, sqlite file /tmp/baseline.db.

| Metric | Before | After (C1–C3) |
|---|---|---|
| Startup to /health/ready | 2576 ms | 2557 ms (neutral) |
| /api/artifacts | 1.6–6.3 ms (cold first) | 1.5–2.4 ms |
| /api/deals (auth) | ~5.0 ms | ~0.6 ms (warm pool) |
| JS bundle | 423272 raw / 115397 gzip | ok (<=150 KB gzip) |
| DB size after start | 163840 B + 53592 WAL | — |

Notes: GZip middleware active; Cache-Control: html → no-cache, /assets/* → immutable;
SQLite pool 3+2, PRAGMA temp_store=MEMORY / cache_size=8MiB; RSS baseline 102.1 MB.
