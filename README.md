# Interaction Ledger & Reporting Pipeline

A Python pipeline that ingests messy CSV lead files and JSONL call-event streams, stores everything safely in SQLite and produces five report files.

---

## What it does

1. **Reads customer lists (CSVs)** — validates phone numbers, dates and required fields. Bad rows go to `rejections.csv`. Duplicate customers are merged, not duplicated.
2. **Reads call events (JSONL)** — deduplicates events, links them to leads by phone. Events that arrive before their lead are stored as orphans and linked automatically on the next ingest.
3. **Protects PII** — phone numbers and customer names are Fernet-encrypted before hitting the database. Matching still works via an HMAC fingerprint — no plaintext ever stored.
4. **Is safe to re-run** — a SHA-256 fingerprint of each drop is stored. Re-ingesting the same drop does nothing.
5. **Produces 5 reports** — batch summary, daily overview, per-lead report, rejections and a data quality JSON.

---

## Folder structure

```
├── src/ledger_pipeline/
│   ├── core.py          ← all logic: parsers, Ledger class, ingest, report
│   ├── cli.py           ← CLI entry point (ingest / report commands)
│   ├── __init__.py
│   └── __main__.py
│
├── tests/
│   └── test_core.py     ← 65 unit + integration tests
│
├── schemas/
│   ├── motor_renewal.json
│   ├── policy_feedback.json
│   └── dispositions.csv
│
├── input/
│   └── generate_data.py ← synthetic messy data generator (stdlib only)
│
├── requirements.txt
└── .gitignore
```

---

## Quick start

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Generate test data
python input/generate_data.py --seed 42 --scale small --out data

# 4. Ingest both drops
python -m src.ledger_pipeline ingest data/drop_1
python -m src.ledger_pipeline ingest data/drop_2

# 5. Generate reports
python -m src.ledger_pipeline report --out output

# 6. Run all tests
python -m unittest discover -s tests -v
```

Optional flags:

```bash
# Custom DB and key path
python -m src.ledger_pipeline --db my.db --key-file my.key ingest data/drop_1

# Custom schemas location
python -m src.ledger_pipeline ingest data/drop_1 --schema-dir schemas --dispositions schemas/dispositions.csv
```

---

## Output files

| File | What's in it |
|---|---|
| `batch_summary.csv` | Lead counts per batch, broken down by status bucket |
| `daily_overview.csv` | Calls, connections, and success rate per day per workflow |
| `lead_report.csv` | One row per lead — masked phone, bucket, disposition, IST timestamps |
| `rejections.csv` | Rows that failed validation (no PII stored) |
| `data_quality.json` | Counters: total rows, rejections, duplicates, orphans, etc. |

---

## Database design

Five tables in SQLite (WAL mode):

| Table | Purpose |
|---|---|
| `leads` | One row per `(workflow_id, phone)`. PII encrypted. |
| `rejections` | Failed rows. No PII. |
| `events` | Append-only call event log. Never modified after insert. |
| `processed_drops` | Fingerprint of each ingested drop — powers idempotency. |
| `quality_counts` | Running counters accumulated across all drops. |

---

## Security note

`.ledger.key` and `ledger.db` are created on first run and listed in `.gitignore`.
**Never commit them.** Losing `.ledger.key` makes all encrypted PII unrecoverable.

---

## Results on `seed=42, scale=small`

```
lead_rows_total:            2057
lead_rows_rejected:          108
duplicate_lead_rows_merged:   57
field_warnings:              122
events_total:               8745
malformed_events:              8
duplicate_events_dropped:    237
orphan_events:               462
unknown_disposition_events:   19
```
