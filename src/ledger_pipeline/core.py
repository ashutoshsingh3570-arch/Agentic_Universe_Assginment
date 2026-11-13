from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


# ---------------------------------------------------------------------------
# Field parsers
# ---------------------------------------------------------------------------

def normalize_phone(value: Any) -> str | None:
    """Strip non-digits, handle 91/0 prefix, return +91XXXXXXXXXX or None."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "6789":
        return None
    return "+91" + digits


def parse_date(value: Any) -> str | None:
    """Accept YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY. Return ISO-8601 or None."""
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_decimal(value: Any) -> str | None:
    """Strip ₹/Rs/INR prefix and comma separators, return 2dp string or None."""
    raw = str(value or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^(?:₹|Rs\.?|INR)\s*", "", raw, flags=re.IGNORECASE)
    raw = raw.replace(",", "").strip()
    try:
        number = Decimal(raw)
        if not number.is_finite() or number < 0:
            return None
        return str(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse UTC/offset/naive timestamp. Naive is treated as IST. Returns UTC datetime."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def to_ist(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(IST)


def normalize_header(value: str) -> str:
    """Lowercase + collapse underscores/spaces to a single space."""
    return re.sub(r"[_\s]+", " ", (value or "").strip().lower())


# ---------------------------------------------------------------------------
# Domain logic
# ---------------------------------------------------------------------------

def effective_disposition(call_events: list[Any]) -> str | None:
    """Return the winning disposition for a call.

    Latest disposition.override wins. Falls back to latest disposition.set.
    Ignored or incomplete events are excluded.
    """
    def valid(e: Any) -> bool:
        return bool(not e["ignored_disposition"] and e["disposition_code"] and e["occurred_at"])

    overrides = [e for e in call_events if e["event_type"] == "disposition.override" and valid(e)]
    sets = [e for e in call_events if e["event_type"] == "disposition.set" and valid(e)]
    candidates = overrides if overrides else sets
    if not candidates:
        return None
    return max(candidates, key=lambda e: (e["occurred_at"], e["id"]))["disposition_code"]


def lead_bucket(calls: list[dict], catalogue: dict) -> str:
    """Classify a lead. First matching rule wins:
      1. yet_to_reach_out       — no calls
      2. completed              — any call has a terminal disposition
      3. in_progress_telephony_failed — every call failed at network level
      4. in_progress_dialed     — everything else
    """
    if not calls:
        return "yet_to_reach_out"
    if any(
        c["disposition"]
        and catalogue.get(c["workflow"], {}).get(c["disposition"], {}).get("is_terminal")
        for c in calls
    ):
        return "completed"
    if all(c["telephony_failed"] for c in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class Ledger:
    """SQLite-backed interaction ledger.

    PII (phone, customer_name) is Fernet-encrypted before storage.
    Phone lookup uses an HMAC fingerprint so plaintext is never needed for matching.
    """

    def __init__(
        self,
        db_path: str | Path = "ledger.db",
        key_file: str | Path = ".ledger.key",
    ) -> None:
        self.db_path = str(db_path)
        self.key_file = Path(key_file)
        self.key_file.parent.mkdir(parents=True, exist_ok=True)

        if not self.key_file.exists():
            self.key_file.write_bytes(Fernet.generate_key())
            try:
                os.chmod(self.key_file, 0o600)
            except OSError:
                pass

        raw_key = self.key_file.read_bytes().strip()
        self.fernet = Fernet(raw_key)
        # Derive HMAC key from the encryption key — both travel together
        self.index_key = hashlib.sha256(raw_key + b"phone-index").digest()

        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_tables()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # --- Schema ---

    def _create_tables(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            id                  INTEGER PRIMARY KEY,
            workflow_id         TEXT    NOT NULL,
            phone_hash          TEXT    NOT NULL,
            phone_enc           BLOB    NOT NULL,
            customer_name_enc   BLOB    NOT NULL,
            external_ref        TEXT    NOT NULL,
            batch_id            TEXT    NOT NULL,
            attributes_json     TEXT    NOT NULL DEFAULT '{}',
            UNIQUE(workflow_id, phone_hash)
        );

        CREATE TABLE IF NOT EXISTS rejections (
            id          INTEGER PRIMARY KEY,
            workflow_id TEXT    NOT NULL,
            batch_id    TEXT    NOT NULL,
            row_number  INTEGER NOT NULL,
            reason      TEXT    NOT NULL
        );

        -- Append-only; rows are never updated or deleted
        CREATE TABLE IF NOT EXISTS events (
            id                  INTEGER PRIMARY KEY,
            event_id            TEXT    NOT NULL UNIQUE,
            workflow_id         TEXT    NOT NULL,
            event_type          TEXT    NOT NULL,
            call_id             TEXT,
            occurred_at         TEXT,
            phone_hash          TEXT,
            lead_id             INTEGER REFERENCES leads(id),
            payload_json        TEXT    NOT NULL DEFAULT '{}',
            disposition_code    TEXT,
            ignored_disposition INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_events_call  ON events(workflow_id, call_id);
        CREATE INDEX IF NOT EXISTS idx_events_phone ON events(workflow_id, phone_hash);
        CREATE INDEX IF NOT EXISTS idx_events_lead  ON events(lead_id);

        CREATE TABLE IF NOT EXISTS processed_drops (
            fingerprint  TEXT PRIMARY KEY,
            source_name  TEXT NOT NULL,
            processed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS quality_counts (
            name  TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        );
        """)
        self.conn.commit()

    # --- Crypto ---

    def _encrypt(self, value: str) -> bytes:
        return self.fernet.encrypt(value.encode("utf-8"))

    def _decrypt(self, value: bytes) -> str:
        return self.fernet.decrypt(value).decode("utf-8")

    def _phone_hash(self, canonical: str) -> str:
        return hmac.new(self.index_key, canonical.encode(), hashlib.sha256).hexdigest()

    # --- Counter ---

    def _count(self, name: str, amount: int = 1) -> None:
        if amount == 0:
            return
        self.conn.execute(
            "INSERT INTO quality_counts(name, value) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            (name, amount),
        )

    # --- Loaders ---

    @staticmethod
    def _load_schemas(schema_dir: Path) -> dict[str, dict]:
        schemas: dict[str, dict] = {}
        for path in sorted(schema_dir.glob("*.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            schemas[schema["workflow_id"]] = schema
        return schemas

    @staticmethod
    def _load_dispositions(path: Path) -> dict[str, dict[str, dict[str, bool]]]:
        result: dict[str, dict[str, dict[str, bool]]] = defaultdict(dict)
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                result[row["workflow_id"]][row["code"]] = {
                    "is_terminal": row["is_terminal"].strip().lower() == "true",
                    "is_success":  row["is_success"].strip().lower() == "true",
                }
        return result

    # --- Fingerprint (idempotency) ---

    def _fingerprint(self, drop: Path) -> str:
        """SHA-256 over all file paths + contents in the drop directory."""
        digest = hashlib.sha256()
        for path in sorted(p for p in drop.rglob("*") if p.is_file()):
            relative = path.relative_to(drop).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
        return digest.hexdigest()

    # --- Public: ingest ---

    def ingest(
        self,
        drop_path: str | Path,
        schema_dir: str | Path | None = None,
        dispositions_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Ingest one drop directory. Returns already_ingested if the drop was seen before."""
        drop = Path(drop_path).resolve()
        if not drop.is_dir():
            raise FileNotFoundError(f"Drop directory not found: {drop}")

        # Support passing the parent data dir; process each drop_* child in order
        child_drops = sorted(
            p for p in drop.iterdir()
            if p.is_dir() and ((p / "leads").exists() or (p / "events").exists())
        )
        if child_drops and not (drop / "leads").exists() and not (drop / "events").exists():
            results = [self.ingest(child, schema_dir, dispositions_path) for child in child_drops]
            return {"status": "ingested_drops", "drop": drop.name, "results": results}

        project_root = Path(__file__).resolve().parents[2]
        schema_dir = Path(schema_dir) if schema_dir else project_root / "schemas"
        dispositions_path = (
            Path(dispositions_path) if dispositions_path
            else project_root / "schemas" / "dispositions.csv"
        )

        schemas = self._load_schemas(schema_dir)
        dispositions = self._load_dispositions(dispositions_path)

        fingerprint = self._fingerprint(drop)
        if self.conn.execute(
            "SELECT 1 FROM processed_drops WHERE fingerprint = ?", (fingerprint,)
        ).fetchone():
            return {"status": "already_ingested", "drop": drop.name}

        before = {r["name"]: r["value"] for r in self.conn.execute("SELECT * FROM quality_counts")}

        with self.conn:
            leads_root = drop / "leads"
            if leads_root.exists():
                for workflow_dir in sorted(p for p in leads_root.iterdir() if p.is_dir()):
                    workflow = workflow_dir.name
                    if workflow not in schemas:
                        continue
                    for csv_path in sorted(workflow_dir.glob("*.csv")):
                        self._ingest_lead_file(csv_path, workflow, schemas[workflow])

            events_root = drop / "events"
            if events_root.exists():
                for event_path in sorted(events_root.glob("*.jsonl")):
                    self._ingest_event_file(event_path, dispositions)

            self._reconcile_events()

            self.conn.execute(
                "INSERT INTO processed_drops VALUES(?, ?, ?)",
                (fingerprint, drop.name, datetime.now(UTC).isoformat(timespec="seconds")),
            )

        after = {r["name"]: r["value"] for r in self.conn.execute("SELECT * FROM quality_counts")}
        return {
            "status": "ingested",
            "drop": drop.name,
            "new_counts": {
                k: after.get(k, 0) - before.get(k, 0)
                for k in sorted(set(before) | set(after))
            },
        }

    # --- Lead ingestion ---

    def _ingest_lead_file(self, path: Path, workflow: str, schema: dict) -> None:
        fields = schema["fields"]

        # Build normalised-header → canonical-field-name map
        aliases: dict[str, str] = {}
        for field in fields:
            for name in [field["name"], *field.get("aliases", [])]:
                aliases[normalize_header(name)] = field["name"]

        batch_id = path.stem

        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return
            mapped = {
                header: aliases.get(normalize_header(header))
                for header in reader.fieldnames
            }

            row_number = 0
            for raw_row in reader:
                if all(not str(v or "").strip() for v in raw_row.values()):
                    continue  # skip blank rows
                row_number += 1
                self._count("lead_rows_total")

                values: dict[str, Any] = {}
                warning_count = 0
                reject_reason: str | None = None

                for field in fields:
                    name = field["name"]
                    raw_value = ""
                    for header, target in mapped.items():
                        if target == name:
                            raw_value = str(raw_row.get(header) or "").strip()
                            break

                    if not raw_value:
                        values[name] = None
                        if field.get("required"):
                            reject_reason = f"missing_required:{name}"
                            break
                        continue

                    kind = field["type"]
                    parsed: Any
                    if kind == "phone":
                        parsed = normalize_phone(raw_value)
                        if parsed is None:
                            reject_reason = "invalid_phone"
                            break
                    elif kind == "date":
                        parsed = parse_date(raw_value)
                    elif kind == "decimal":
                        parsed = parse_decimal(raw_value)
                    elif kind == "enum":
                        allowed = {str(v).lower() for v in field.get("values", [])}
                        parsed = raw_value.lower() if raw_value.lower() in allowed else None
                    else:
                        parsed = raw_value

                    if parsed is None:
                        values[name] = None
                        warning_count += 1
                    else:
                        values[name] = parsed

                if reject_reason:
                    self.conn.execute(
                        "INSERT INTO rejections(workflow_id, batch_id, row_number, reason) VALUES(?, ?, ?, ?)",
                        (workflow, batch_id, row_number, reject_reason),
                    )
                    self._count("lead_rows_rejected")
                    self._count(f"rejections_by_reason:{reject_reason}")
                    continue

                phone = values.get("phone")
                if not phone:
                    self.conn.execute(
                        "INSERT INTO rejections(workflow_id, batch_id, row_number, reason) VALUES(?, ?, ?, ?)",
                        (workflow, batch_id, row_number, "missing_required:phone"),
                    )
                    self._count("lead_rows_rejected")
                    self._count("rejections_by_reason:missing_required:phone")
                    continue

                self._count("field_warnings", warning_count)

                phone_hash = self._phone_hash(phone)
                reference_field = next((f["name"] for f in fields if f.get("reference")), None)
                external_ref = str(values.get(reference_field) or "") if reference_field else ""
                pii_fields = {f["name"] for f in fields if f.get("pii")}
                customer_name = str(values.get("customer_name") or "")

                # Non-PII, non-reference fields stored as JSON attributes
                attributes = {
                    k: v for k, v in values.items()
                    if k not in pii_fields and k != reference_field
                }

                existing = self.conn.execute(
                    "SELECT id, customer_name_enc, attributes_json FROM leads "
                    "WHERE workflow_id = ? AND phone_hash = ?",
                    (workflow, phone_hash),
                ).fetchone()

                if existing:
                    # Merge: later non-null values overwrite earlier ones
                    old_attrs = json.loads(existing["attributes_json"])
                    for key, value in attributes.items():
                        if value is not None:
                            old_attrs[key] = value
                    new_name_enc = (
                        self._encrypt(customer_name)
                        if customer_name
                        else bytes(existing["customer_name_enc"])
                    )
                    self.conn.execute(
                        "UPDATE leads SET phone_enc = ?, customer_name_enc = ?, attributes_json = ? WHERE id = ?",
                        (self._encrypt(phone), new_name_enc, json.dumps(old_attrs, sort_keys=True), existing["id"]),
                    )
                    self._count("duplicate_lead_rows_merged")
                else:
                    # Apply schema defaults for still-null optional fields
                    defaults = {f["name"]: f["default"] for f in fields if f.get("default") is not None}
                    for key, default_value in defaults.items():
                        if attributes.get(key) is None:
                            attributes[key] = default_value

                    self.conn.execute(
                        "INSERT INTO leads "
                        "(workflow_id, phone_hash, phone_enc, customer_name_enc, external_ref, batch_id, attributes_json) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            workflow,
                            phone_hash,
                            self._encrypt(phone),
                            self._encrypt(customer_name),
                            external_ref,
                            batch_id,
                            json.dumps(attributes, sort_keys=True),
                        ),
                    )

    # --- Event ingestion ---

    def _ingest_event_file(self, path: Path, dispositions: dict) -> None:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                self._count("events_total")

                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    self._count("malformed_events")
                    continue
                if not isinstance(event, dict) or not event.get("event_id"):
                    self._count("malformed_events")
                    continue

                event_id = str(event["event_id"])

                if self.conn.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone():
                    self._count("duplicate_events_dropped")
                    continue

                workflow    = str(event.get("workflow_id") or "")
                event_type  = str(event.get("event_type") or "")
                call_id     = str(event.get("call_id") or "") or None
                occurred    = parse_timestamp(event.get("occurred_at"))
                occurred_at = iso_utc(occurred) if occurred else None
                raw_phone   = event.get("phone")
                phone       = normalize_phone(raw_phone) if raw_phone else None
                phone_hash  = self._phone_hash(phone) if phone else None
                payload     = event.get("payload") if isinstance(event.get("payload"), dict) else {}

                disposition_code: str | None = None
                ignored = 0
                if event_type in ("disposition.set", "disposition.override"):
                    code = str(payload.get("disposition_code") or "") or None
                    disposition_code = code
                    if not code or code not in dispositions.get(workflow, {}):
                        self._count("unknown_disposition_events")
                        ignored = 1

                lead_id: int | None = None
                if phone_hash:
                    row = self.conn.execute(
                        "SELECT id FROM leads WHERE workflow_id = ? AND phone_hash = ?",
                        (workflow, phone_hash),
                    ).fetchone()
                    if row:
                        lead_id = row["id"]

                self.conn.execute(
                    "INSERT INTO events "
                    "(event_id, workflow_id, event_type, call_id, occurred_at, "
                    " phone_hash, lead_id, payload_json, disposition_code, ignored_disposition) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id, workflow, event_type, call_id, occurred_at,
                        phone_hash, lead_id, json.dumps(payload, sort_keys=True),
                        disposition_code, ignored,
                    ),
                )

    # --- Reconciliation ---

    def _reconcile_events(self) -> None:
        """Link orphan events to leads after each ingestion.

        Pass 1: match by phone hash.
        Pass 2: match disposition.override events (no phone) via call_id.
        """
        self.conn.execute("""
            UPDATE events
               SET lead_id = (
                   SELECT leads.id FROM leads
                    WHERE leads.workflow_id = events.workflow_id
                      AND leads.phone_hash  = events.phone_hash
               )
             WHERE lead_id IS NULL AND phone_hash IS NOT NULL
        """)
        self.conn.execute("""
            UPDATE events
               SET lead_id = (
                   SELECT e2.lead_id FROM events e2
                    WHERE e2.workflow_id = events.workflow_id
                      AND e2.call_id    = events.call_id
                      AND e2.lead_id    IS NOT NULL
                    ORDER BY e2.id LIMIT 1
               )
             WHERE lead_id    IS NULL
               AND event_type = 'disposition.override'
               AND call_id    IS NOT NULL
        """)

    # --- Public: report ---

    def report(
        self,
        output_dir: str | Path,
        schema_dir: str | Path | None = None,
        dispositions_path: str | Path | None = None,
    ) -> dict[str, str]:
        """Write all five report files to output_dir. Returns a map of filename → path."""
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)

        project_root = Path(__file__).resolve().parents[2]
        dispositions_path = (
            Path(dispositions_path) if dispositions_path
            else project_root / "schemas" / "dispositions.csv"
        )
        catalogue = self._load_dispositions(dispositions_path)

        leads = self.conn.execute(
            "SELECT * FROM leads ORDER BY workflow_id, external_ref"
        ).fetchall()
        events = self.conn.execute(
            "SELECT * FROM events WHERE lead_id IS NOT NULL ORDER BY id"
        ).fetchall()

        # Group events by call
        events_by_call: dict[tuple, list] = defaultdict(list)
        for event in events:
            if event["call_id"]:
                events_by_call[(event["workflow_id"], event["call_id"], event["lead_id"])].append(event)

        # Build call objects; accumulate daily stats
        calls_by_lead: dict[int, list[dict]] = defaultdict(list)
        daily: dict[tuple, dict] = {}

        for (workflow, call_id, lead_id), call_events in events_by_call.items():
            initiated = [e for e in call_events if e["event_type"] == "call.initiated" and e["occurred_at"]]
            if not initiated:
                continue  # a call only counts when call.initiated is present

            start_ist   = to_ist(min(initiated, key=lambda e: e["id"])["occurred_at"])
            connected   = any(e["event_type"] == "call.connected" for e in call_events)
            tele_failed = any(e["event_type"] == "call.telephony_failed" for e in call_events)
            disposition = effective_disposition(call_events)

            calls_by_lead[lead_id].append({
                "workflow": workflow, "call_id": call_id, "lead_id": lead_id,
                "start": start_ist, "connected": connected,
                "telephony_failed": tele_failed, "disposition": disposition,
            })

            day = start_ist.date().isoformat()
            dk  = (workflow, day)
            if dk not in daily:
                daily[dk] = {"total_calls": 0, "connected_calls": 0, "success_leads": set()}
            daily[dk]["total_calls"]     += 1
            daily[dk]["connected_calls"] += int(connected)
            if disposition and catalogue.get(workflow, {}).get(disposition, {}).get("is_success"):
                daily[dk]["success_leads"].add(lead_id)

        # Per-lead aggregation
        batch_rows: list[tuple] = []
        lead_rows:  list[dict]  = []

        for lead in leads:
            lead_id    = lead["id"]
            lead_calls = sorted(calls_by_lead.get(lead_id, []), key=lambda c: c["start"])
            total      = len(lead_calls)
            connected  = sum(1 for c in lead_calls if c["connected"])
            bucket     = lead_bucket(lead_calls, catalogue)
            last_call  = lead_calls[-1] if lead_calls else None
            current_disp = next(
                (c["disposition"] for c in reversed(lead_calls) if c["disposition"]), ""
            )
            ever_success = any(
                c["disposition"]
                and catalogue.get(lead["workflow_id"], {}).get(c["disposition"], {}).get("is_success")
                for c in lead_calls
            )
            phone        = self._decrypt(lead["phone_enc"])
            phone_masked = "+91******" + phone[-4:]

            lead_rows.append({
                "workflow_id":         lead["workflow_id"],
                "external_ref":        lead["external_ref"],
                "batch_id":            lead["batch_id"],
                "phone_masked":        phone_masked,
                "bucket":              bucket,
                "total_calls":         total,
                "connected_calls":     connected,
                "last_call_at":        last_call["start"].isoformat(timespec="seconds") if last_call else "",
                "current_disposition": current_disp,
                "ever_success":        str(ever_success).lower(),
            })
            batch_rows.append((lead["workflow_id"], lead["batch_id"], bucket))

        # Batch summary
        batch_counts: dict[tuple, Counter] = defaultdict(Counter)
        for wf, batch_id, bucket in batch_rows:
            batch_counts[(wf, batch_id)]["total_leads"] += 1
            batch_counts[(wf, batch_id)][bucket]        += 1

        batch_output = [
            {
                "workflow_id":                  wf,
                "batch_id":                     batch_id,
                "total_leads":                  counts["total_leads"],
                "yet_to_reach_out":             counts["yet_to_reach_out"],
                "in_progress_telephony_failed": counts["in_progress_telephony_failed"],
                "in_progress_dialed":           counts["in_progress_dialed"],
                "completed":                    counts["completed"],
            }
            for (wf, batch_id), counts in sorted(batch_counts.items())
        ]

        daily_output = [
            {
                "workflow_id":       wf,
                "date":              day,
                "total_calls":       d["total_calls"],
                "connected_calls":   d["connected_calls"],
                "connectivity_rate": f"{d['connected_calls'] / d['total_calls']:.4f}",
                "success_leads":     len(d["success_leads"]),
            }
            for (wf, day), d in sorted(daily.items())
        ]

        rejection_rows = [
            dict(r) for r in self.conn.execute(
                "SELECT workflow_id, batch_id, row_number, reason "
                "FROM rejections ORDER BY workflow_id, batch_id, row_number"
            )
        ]

        self._write_csv(
            output / "batch_summary.csv",
            ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
             "in_progress_telephony_failed", "in_progress_dialed", "completed"],
            batch_output,
        )
        self._write_csv(
            output / "daily_overview.csv",
            ["workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"],
            daily_output,
        )
        self._write_csv(
            output / "lead_report.csv",
            ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
             "total_calls", "connected_calls", "last_call_at", "current_disposition", "ever_success"],
            lead_rows,
        )
        self._write_csv(
            output / "rejections.csv",
            ["workflow_id", "batch_id", "row_number", "reason"],
            rejection_rows,
        )

        qc = {r["name"]: r["value"] for r in self.conn.execute("SELECT * FROM quality_counts")}
        reasons = {
            name.split(":", 1)[1]: val
            for name, val in qc.items()
            if name.startswith("rejections_by_reason:")
        }
        quality = {
            "lead_rows_total":            qc.get("lead_rows_total", 0),
            "lead_rows_rejected":         qc.get("lead_rows_rejected", 0),
            "rejections_by_reason":       dict(sorted(reasons.items())),
            "duplicate_lead_rows_merged": qc.get("duplicate_lead_rows_merged", 0),
            "field_warnings":             qc.get("field_warnings", 0),
            "events_total":               qc.get("events_total", 0),
            "malformed_events":           qc.get("malformed_events", 0),
            "duplicate_events_dropped":   qc.get("duplicate_events_dropped", 0),
            "orphan_events":              self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE lead_id IS NULL"
            ).fetchone()[0],
            "unknown_disposition_events": qc.get("unknown_disposition_events", 0),
        }
        (output / "data_quality.json").write_text(
            json.dumps(quality, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        return {name: str(output / name) for name in [
            "batch_summary.csv", "daily_overview.csv", "lead_report.csv",
            "rejections.csv", "data_quality.json",
        ]}

    @staticmethod
    def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
