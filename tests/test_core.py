"""Tests for ledger_pipeline.core — parsers, domain logic, ingest, report."""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

# Make `src/` importable so tests can find ledger_pipeline regardless of CWD
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ledger_pipeline.core import (
    Ledger,
    effective_disposition,
    lead_bucket,
    normalize_phone,
    parse_date,
    parse_decimal,
    parse_timestamp,
    to_ist,
)


class TestNormalizePhone(unittest.TestCase):
    def test_plus91_with_spaces(self):
        self.assertEqual(normalize_phone("+91 98765 43210"), "+919876543210")

    def test_zero_prefix(self):
        self.assertEqual(normalize_phone("09876543210"), "+919876543210")

    def test_country_code_no_plus(self):
        self.assertEqual(normalize_phone("91-98765-43210"), "+919876543210")

    def test_raw_10_digits(self):
        self.assertEqual(normalize_phone("9876543210"), "+919876543210")

    def test_starts_with_6(self):
        self.assertEqual(normalize_phone("6123456789"), "+916123456789")

    def test_invalid_starts_with_5(self):
        self.assertIsNone(normalize_phone("5123456789"))

    def test_invalid_too_short(self):
        self.assertIsNone(normalize_phone("123456789"))

    def test_empty_returns_none(self):
        self.assertIsNone(normalize_phone(""))

    def test_none_input_returns_none(self):
        self.assertIsNone(normalize_phone(None))


class TestParseDate(unittest.TestCase):
    def test_iso_format(self):
        self.assertEqual(parse_date("2026-12-01"), "2026-12-01")

    def test_dd_mm_yyyy_slash(self):
        self.assertEqual(parse_date("01/12/2026"), "2026-12-01")

    def test_dd_mm_yyyy_dash(self):
        self.assertEqual(parse_date("01-12-2026"), "2026-12-01")

    def test_dd_mon_yyyy(self):
        self.assertEqual(parse_date("01-Dec-2026"), "2026-12-01")

    def test_dd_mon_yyyy_case_insensitive(self):
        self.assertEqual(parse_date("15-oct-2026"), "2026-10-15")

    def test_impossible_date_returns_none(self):
        self.assertIsNone(parse_date("31/02/2026"))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_date(""))

    def test_none_returns_none(self):
        self.assertIsNone(parse_date(None))


class TestParseDecimal(unittest.TestCase):
    def test_rupee_symbol(self):
        self.assertEqual(parse_decimal("₹12,345"), "12345.00")

    def test_rs_dot_prefix(self):
        self.assertEqual(parse_decimal("Rs. 99.9"), "99.90")

    def test_rs_prefix_no_dot(self):
        self.assertEqual(parse_decimal("Rs 100"), "100.00")

    def test_inr_prefix(self):
        self.assertEqual(parse_decimal("INR 50.555"), "50.56")

    def test_comma_thousands(self):
        self.assertEqual(parse_decimal("1,00,000.00"), "100000.00")

    def test_negative_returns_none(self):
        self.assertIsNone(parse_decimal("-1"))

    def test_zero_is_valid(self):
        self.assertEqual(parse_decimal("0"), "0.00")

    def test_empty_returns_none(self):
        self.assertIsNone(parse_decimal(""))


class TestParseTimestamp(unittest.TestCase):
    def test_utc_z_suffix(self):
        dt = parse_timestamp("2026-10-03T10:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzname(), "UTC")

    def test_offset_preserved(self):
        dt = parse_timestamp("2026-10-03T10:00:00+05:30")
        self.assertIsNotNone(dt)
        # Stored in UTC
        self.assertEqual(dt.hour, 4)   # 10:00 IST → 04:30 UTC

    def test_naive_treated_as_ist(self):
        dt = parse_timestamp("2026-10-03 10:01:00")
        self.assertIsNotNone(dt)
        ist = to_ist(dt.isoformat())
        self.assertEqual(ist.strftime("%H:%M"), "10:01")

    def test_empty_returns_none(self):
        self.assertIsNone(parse_timestamp(""))


class TestEffectiveDisposition(unittest.TestCase):
    def _make_event(self, etype, code, ts, eid, ignored=0):
        return {
            "event_type":           etype,
            "disposition_code":     code,
            "occurred_at":          ts,
            "id":                   eid,
            "ignored_disposition":  ignored,
        }

    def test_override_beats_set(self):
        events = [
            self._make_event("disposition.set",      "renewed",       "2026-01-01T00:00:00+00:00", 1),
            self._make_event("disposition.override",  "not_interested", "2026-01-02T00:00:00+00:00", 2),
        ]
        self.assertEqual(effective_disposition(events), "not_interested")

    def test_latest_override_wins(self):
        events = [
            self._make_event("disposition.override", "wrong_number",  "2026-01-01T00:00:00+00:00", 1),
            self._make_event("disposition.override", "not_interested", "2026-01-02T00:00:00+00:00", 2),
        ]
        self.assertEqual(effective_disposition(events), "not_interested")

    def test_latest_set_when_no_override(self):
        events = [
            self._make_event("disposition.set", "callback_requested", "2026-01-01T00:00:00+00:00", 1),
            self._make_event("disposition.set", "renewed",            "2026-01-02T00:00:00+00:00", 2),
        ]
        self.assertEqual(effective_disposition(events), "renewed")

    def test_ignored_events_excluded(self):
        events = [
            self._make_event("disposition.override", "unknown_code", "2026-01-02T00:00:00+00:00", 2, ignored=1),
            self._make_event("disposition.set",       "renewed",     "2026-01-01T00:00:00+00:00", 1),
        ]
        self.assertEqual(effective_disposition(events), "renewed")

    def test_no_valid_events_returns_none(self):
        events = [
            self._make_event("disposition.set", None, "2026-01-01T00:00:00+00:00", 1),
        ]
        self.assertIsNone(effective_disposition(events))

    def test_empty_list_returns_none(self):
        self.assertIsNone(effective_disposition([]))

    def test_tie_broken_by_id(self):
        """When occurred_at is identical, highest id wins."""
        events = [
            self._make_event("disposition.set", "renewed",       "2026-01-01T00:00:00+00:00", 1),
            self._make_event("disposition.set", "not_interested", "2026-01-01T00:00:00+00:00", 2),
        ]
        self.assertEqual(effective_disposition(events), "not_interested")


class TestLeadBucket(unittest.TestCase):
    CATALOGUE = {
        "motor_renewal": {
            "renewed":       {"is_terminal": True,  "is_success": True},
            "not_interested": {"is_terminal": True, "is_success": False},
        }
    }

    def _call(self, disposition=None, telephony_failed=False):
        return {"workflow": "motor_renewal", "disposition": disposition, "telephony_failed": telephony_failed}

    def test_no_calls_is_yet_to_reach_out(self):
        self.assertEqual(lead_bucket([], self.CATALOGUE), "yet_to_reach_out")

    def test_terminal_disposition_is_completed(self):
        calls = [self._call(disposition="renewed")]
        self.assertEqual(lead_bucket(calls, self.CATALOGUE), "completed")

    def test_all_telephony_failed(self):
        calls = [self._call(telephony_failed=True), self._call(telephony_failed=True)]
        self.assertEqual(lead_bucket(calls, self.CATALOGUE), "in_progress_telephony_failed")

    def test_mixed_telephony_is_in_progress_dialed(self):
        calls = [self._call(telephony_failed=True), self._call(telephony_failed=False)]
        self.assertEqual(lead_bucket(calls, self.CATALOGUE), "in_progress_dialed")

    def test_completed_takes_precedence_over_telephony_failed(self):
        """A terminal disposition beats all-telephony-failed."""
        calls = [
            self._call(telephony_failed=True),
            self._call(disposition="renewed", telephony_failed=False),
        ]
        self.assertEqual(lead_bucket(calls, self.CATALOGUE), "completed")

    def test_no_disposition_no_failed_is_in_progress_dialed(self):
        calls = [self._call()]
        self.assertEqual(lead_bucket(calls, self.CATALOGUE), "in_progress_dialed")


def _schema_dir() -> Path:
    """Return the schemas/ directory at the project root (sibling of tests/)."""
    return Path(__file__).resolve().parent.parent / "schemas"


class TestLedgerIngest(unittest.TestCase):
    """Integration tests that use real schemas and a real (temp) SQLite database."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = Ledger(self.root / "test.db", self.root / "key")

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    # --- helpers ---

    def _make_drop(self, name: str, csv_name: str, csv_content: str, workflow: str = "policy_feedback") -> Path:
        drop = self.root / name
        csv_dir = drop / "leads" / workflow
        csv_dir.mkdir(parents=True)
        (csv_dir / csv_name).write_text(csv_content, encoding="utf-8")
        return drop

    def _ingest(self, drop: Path) -> dict:
        return self.ledger.ingest(drop, schema_dir=_schema_dir(),
                                  dispositions_path=_schema_dir() / "dispositions.csv")

    def _report(self, out: Path) -> dict:
        return self.ledger.report(out, dispositions_path=_schema_dir() / "dispositions.csv")

    # --- tests ---

    def test_basic_ingest_creates_lead(self):
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "Name,Contact,Order ID,Product,Order Date,Language\n"
            "Asha Rao,+91 98765 43210,ORD-1,Phone,01/10/2026,EN\n",
        )
        result = self._ingest(drop)
        self.assertEqual(result["status"], "ingested")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)

    def test_idempotency(self):
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "Name,Contact,Order ID\n"
            "Asha Rao,+91 98765 43210,ORD-1\n",
        )
        first  = self._ingest(drop)
        second = self._ingest(drop)
        self.assertEqual(first["status"],  "ingested")
        self.assertEqual(second["status"], "already_ingested")
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)

    def test_pii_is_encrypted(self):
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "Name,Contact,Order ID\n"
            "Asha Rao,+91 98765 43210,ORD-1\n",
        )
        self._ingest(drop)
        row = self.ledger.conn.execute("SELECT phone_enc, customer_name_enc FROM leads").fetchone()
        # Plaintext must NOT appear in the ciphertext blob
        self.assertNotIn(b"98765", bytes(row["phone_enc"]))
        self.assertNotIn(b"Asha",  bytes(row["customer_name_enc"]))

    def test_rejection_invalid_phone(self):
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "Name,Contact,Order ID\n"
            "Bad User,5123456789,ORD-X\n",
        )
        self._ingest(drop)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 0)
        reason = self.ledger.conn.execute("SELECT reason FROM rejections").fetchone()["reason"]
        self.assertEqual(reason, "invalid_phone")

    def test_rejection_missing_required_field(self):
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "Name,Contact,Order ID\n"
            ",+91 98765 43210,ORD-1\n",    # missing customer_name
        )
        self._ingest(drop)
        reason = self.ledger.conn.execute("SELECT reason FROM rejections").fetchone()["reason"]
        self.assertEqual(reason, "missing_required:customer_name")

    def test_blank_rows_not_counted(self):
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "Name,Contact,Order ID\n"
            "Asha Rao,+91 98765 43210,ORD-1\n"
            ",,\n"                         # blank row — skipped
            "Ravi Kumar,+91 87654 32109,ORD-2\n",
        )
        self._ingest(drop)
        total = self.ledger.conn.execute(
            "SELECT value FROM quality_counts WHERE name='lead_rows_total'"
        ).fetchone()["value"]
        self.assertEqual(total, 2)

    def test_duplicate_lead_merge(self):
        """Second row for same phone updates non-null attributes; batch_id stays from first."""
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "Name,Contact,Order ID,Product\n"
            "Asha Rao,+91 98765 43210,ORD-1,Phone\n"
            "Asha R Updated,+91 98765 43210,ORD-1,Tablet\n",  # same phone
        )
        self._ingest(drop)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)
        attrs = json.loads(
            self.ledger.conn.execute("SELECT attributes_json FROM leads").fetchone()["attributes_json"]
        )
        self.assertEqual(attrs.get("product"), "Tablet")  # last write wins

    def test_header_alias_matching(self):
        """Headers that use aliases (e.g. 'mobile' instead of 'phone') are accepted."""
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "customer name,mobile,order id\n"
            "Ravi Kumar,+91 87654 32109,ORD-2\n",
        )
        self._ingest(drop)
        self.assertEqual(self.ledger.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)

    def test_enum_default_applied(self):
        """preferred_language defaults to 'en' when missing."""
        drop = self._make_drop(
            "drop_1", "batch_001.csv",
            "Name,Contact,Order ID\n"
            "Asha Rao,+91 98765 43210,ORD-1\n",
        )
        self._ingest(drop)
        attrs = json.loads(
            self.ledger.conn.execute("SELECT attributes_json FROM leads").fetchone()["attributes_json"]
        )
        self.assertEqual(attrs.get("preferred_language"), "en")

    def test_event_links_to_lead(self):
        """Events whose phone matches a known lead are linked."""
        drop = self.root / "drop_1"
        leads_dir = drop / "leads" / "policy_feedback"
        leads_dir.mkdir(parents=True)
        (leads_dir / "batch_001.csv").write_text(
            "Name,Contact,Order ID\nAsha Rao,+91 98765 43210,ORD-1\n", encoding="utf-8"
        )
        events_dir = drop / "events"
        events_dir.mkdir()
        (events_dir / "events_001.jsonl").write_text(
            json.dumps({
                "event_id": "evt_0001",
                "event_type": "call.initiated",
                "workflow_id": "policy_feedback",
                "call_id": "call_001",
                "occurred_at": "2026-10-01T10:00:00Z",
                "phone": "91-98765-43210",
                "payload": {},
            }) + "\n",
            encoding="utf-8",
        )
        self._ingest(drop)
        event = self.ledger.conn.execute("SELECT lead_id FROM events WHERE event_id='evt_0001'").fetchone()
        self.assertIsNotNone(event["lead_id"])

    def test_orphan_event_later_linked(self):
        """An event arriving before its lead becomes linked in a later drop."""
        # Drop 1: only the event
        drop1 = self.root / "drop_1"
        events_dir = drop1 / "events"
        events_dir.mkdir(parents=True)
        (events_dir / "events_001.jsonl").write_text(
            json.dumps({
                "event_id": "evt_orphan",
                "event_type": "call.initiated",
                "workflow_id": "policy_feedback",
                "call_id": "call_X",
                "occurred_at": "2026-10-01T10:00:00Z",
                "phone": "+919876543210",
                "payload": {},
            }) + "\n",
            encoding="utf-8",
        )
        self._ingest(drop1)
        orphan = self.ledger.conn.execute(
            "SELECT lead_id FROM events WHERE event_id='evt_orphan'"
        ).fetchone()
        self.assertIsNone(orphan["lead_id"])

        # Drop 2: lead arrives
        drop2 = self._make_drop(
            "drop_2", "batch_002.csv",
            "Name,Contact,Order ID\nAsha Rao,+91 98765 43210,ORD-1\n",
        )
        self._ingest(drop2)
        linked = self.ledger.conn.execute(
            "SELECT lead_id FROM events WHERE event_id='evt_orphan'"
        ).fetchone()
        self.assertIsNotNone(linked["lead_id"])

    def test_duplicate_events_dropped(self):
        """The same event_id ingested twice is counted and dropped."""
        event_line = json.dumps({
            "event_id": "evt_dup",
            "event_type": "call.initiated",
            "workflow_id": "policy_feedback",
            "call_id": "call_001",
            "occurred_at": "2026-10-01T10:00:00Z",
            "phone": "+919876543210",
            "payload": {},
        }) + "\n"

        drop1 = self.root / "drop_1"
        (drop1 / "events").mkdir(parents=True)
        (drop1 / "events" / "e1.jsonl").write_text(event_line, encoding="utf-8")
        self._ingest(drop1)

        drop2 = self.root / "drop_2"
        (drop2 / "events").mkdir(parents=True)
        (drop2 / "events" / "e2.jsonl").write_text(event_line, encoding="utf-8")
        self._ingest(drop2)

        count = self.ledger.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_id='evt_dup'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_malformed_json_line_counted(self):
        drop = self.root / "drop_1"
        (drop / "events").mkdir(parents=True)
        (drop / "events" / "bad.jsonl").write_text("{not valid json\n", encoding="utf-8")
        self._ingest(drop)
        malformed = self.ledger.conn.execute(
            "SELECT value FROM quality_counts WHERE name='malformed_events'"
        ).fetchone()["value"]
        self.assertEqual(malformed, 1)


class TestLedgerReport(unittest.TestCase):
    """Tests for report() output correctness."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger = Ledger(self.root / "test.db", self.root / "key")

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def _build_simple_scenario(self):
        """One lead, one call.initiated + call.connected + disposition.set(renewed)."""
        drop = self.root / "drop_1"
        leads_dir = drop / "leads" / "motor_renewal"
        leads_dir.mkdir(parents=True)
        (leads_dir / "batch_001.csv").write_text(
            "customer name,mobile,policy no\nAsha Rao,+91 98765 43210,POL-001\n",
            encoding="utf-8",
        )
        events_dir = drop / "events"
        events_dir.mkdir()
        (events_dir / "e1.jsonl").write_text(
            "\n".join([
                json.dumps({"event_id": "e1", "event_type": "call.initiated",
                            "workflow_id": "motor_renewal", "call_id": "c1",
                            "occurred_at": "2026-10-01T05:00:00Z",
                            "phone": "+919876543210", "payload": {}}),
                json.dumps({"event_id": "e2", "event_type": "call.connected",
                            "workflow_id": "motor_renewal", "call_id": "c1",
                            "occurred_at": "2026-10-01T05:01:00Z",
                            "phone": "+919876543210", "payload": {}}),
                json.dumps({"event_id": "e3", "event_type": "disposition.set",
                            "workflow_id": "motor_renewal", "call_id": "c1",
                            "occurred_at": "2026-10-01T05:05:00Z",
                            "phone": "+919876543210",
                            "payload": {"disposition_code": "renewed"}}),
            ]) + "\n",
            encoding="utf-8",
        )
        self.ledger.ingest(drop, schema_dir=_schema_dir(),
                           dispositions_path=_schema_dir() / "dispositions.csv")

    def _report_dir(self) -> Path:
        out = self.root / "reports"
        self.ledger.report(out, dispositions_path=_schema_dir() / "dispositions.csv")
        return out

    def _read_csv(self, path: Path) -> list[dict]:
        with path.open(encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def test_batch_summary_columns_and_row(self):
        self._build_simple_scenario()
        out = self._report_dir()
        rows = self._read_csv(out / "batch_summary.csv")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["workflow_id"], "motor_renewal")
        self.assertEqual(row["batch_id"],    "batch_001")
        self.assertEqual(row["total_leads"], "1")
        self.assertEqual(row["completed"],   "1")

    def test_daily_overview_connectivity_rate_format(self):
        self._build_simple_scenario()
        out = self._report_dir()
        rows = self._read_csv(out / "daily_overview.csv")
        self.assertEqual(len(rows), 1)
        rate = rows[0]["connectivity_rate"]
        # Must have exactly 4 decimal places
        self.assertRegex(rate, r"^\d+\.\d{4}$")
        self.assertEqual(rate, "1.0000")

    def test_daily_overview_success_leads(self):
        self._build_simple_scenario()
        out = self._report_dir()
        rows = self._read_csv(out / "daily_overview.csv")
        # 'renewed' is is_success=true for motor_renewal
        self.assertEqual(rows[0]["success_leads"], "1")

    def test_lead_report_phone_masking(self):
        self._build_simple_scenario()
        out = self._report_dir()
        rows = self._read_csv(out / "lead_report.csv")
        self.assertEqual(rows[0]["phone_masked"], "+91******3210")

    def test_lead_report_bucket_completed(self):
        self._build_simple_scenario()
        out = self._report_dir()
        rows = self._read_csv(out / "lead_report.csv")
        self.assertEqual(rows[0]["bucket"],       "completed")
        self.assertEqual(rows[0]["ever_success"], "true")

    def test_lead_report_last_call_at_in_ist(self):
        """last_call_at should include +05:30 IST offset."""
        self._build_simple_scenario()
        out = self._report_dir()
        rows = self._read_csv(out / "lead_report.csv")
        last_call_at = rows[0]["last_call_at"]
        self.assertIn("+05:30", last_call_at)

    def test_rejections_csv_has_no_pii(self):
        """Rejections must only contain workflow, batch, row, reason — no names or phones."""
        drop = self.root / "drop_1"
        leads_dir = drop / "leads" / "policy_feedback"
        leads_dir.mkdir(parents=True)
        (leads_dir / "batch_001.csv").write_text(
            "Name,Contact,Order ID\nAsha Rao,5555555555,ORD-1\n",  # invalid phone
            encoding="utf-8",
        )
        self.ledger.ingest(drop, schema_dir=_schema_dir(),
                           dispositions_path=_schema_dir() / "dispositions.csv")
        out = self.root / "reports"
        self.ledger.report(out, dispositions_path=_schema_dir() / "dispositions.csv")
        content = (out / "rejections.csv").read_text(encoding="utf-8")  # no file handle to leave open
        self.assertNotIn("Asha", content)
        self.assertNotIn("5555", content)

    def test_data_quality_json_keys(self):
        self._build_simple_scenario()
        out = self._report_dir()
        quality = json.loads((out / "data_quality.json").read_text(encoding="utf-8"))
        expected_keys = {
            "lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
            "duplicate_lead_rows_merged", "field_warnings", "events_total",
            "malformed_events", "duplicate_events_dropped", "orphan_events",
            "unknown_disposition_events",
        }
        self.assertEqual(set(quality.keys()), expected_keys)

    def test_data_quality_counts_are_non_negative(self):
        self._build_simple_scenario()
        out = self._report_dir()
        quality = json.loads((out / "data_quality.json").read_text(encoding="utf-8"))
        for key, val in quality.items():
            if isinstance(val, int):
                self.assertGreaterEqual(val, 0, f"{key} should be non-negative")

    def test_disposition_override_links_via_call_id(self):
        """A disposition.override with no phone field should link to the lead via call_id."""
        drop = self.root / "drop_1"
        leads_dir = drop / "leads" / "motor_renewal"
        leads_dir.mkdir(parents=True)
        (leads_dir / "batch_001.csv").write_text(
            "customer name,mobile,policy no\nAsha Rao,+91 98765 43210,POL-001\n",
            encoding="utf-8",
        )
        events_dir = drop / "events"
        events_dir.mkdir()
        (events_dir / "e1.jsonl").write_text(
            "\n".join([
                json.dumps({"event_id": "e1", "event_type": "call.initiated",
                            "workflow_id": "motor_renewal", "call_id": "c1",
                            "occurred_at": "2026-10-01T05:00:00Z",
                            "phone": "+919876543210", "payload": {}}),
                json.dumps({"event_id": "e2", "event_type": "disposition.set",
                            "workflow_id": "motor_renewal", "call_id": "c1",
                            "occurred_at": "2026-10-01T05:05:00Z",
                            "phone": "+919876543210",
                            "payload": {"disposition_code": "callback_requested"}}),
                # Override has NO phone field — should link via call_id
                json.dumps({"event_id": "e3", "event_type": "disposition.override",
                            "workflow_id": "motor_renewal", "call_id": "c1",
                            "occurred_at": "2026-10-01T05:10:00Z",
                            "payload": {"disposition_code": "renewed",
                                        "overridden_by": "supervisor1", "reason": "confirmed"}}),
            ]) + "\n",
            encoding="utf-8",
        )
        self.ledger.ingest(drop, schema_dir=_schema_dir(),
                           dispositions_path=_schema_dir() / "dispositions.csv")
        override = self.ledger.conn.execute(
            "SELECT lead_id FROM events WHERE event_id='e3'"
        ).fetchone()
        self.assertIsNotNone(override["lead_id"])

        out = self.root / "reports"
        self.ledger.report(out, dispositions_path=_schema_dir() / "dispositions.csv")
        rows = self._read_csv(out / "lead_report.csv")
        # Override (renewed) should win over the set (callback_requested)
        self.assertEqual(rows[0]["current_disposition"], "renewed")
        self.assertEqual(rows[0]["ever_success"], "true")


if __name__ == "__main__":
    unittest.main()
