#!/usr/bin/env python3
"""Tests that clinical context cannot be retrieved before DOB verification."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))

import server_guarded_v2 as guarded  # noqa: E402


def main() -> int:
    start = guarded.start_checkin("PT-0001")
    assert start["identity_verified"] is False
    serialized = str(start)
    for secret in ("dob", "1971", "Metformin", "Diabetes", "RX-000"):
        assert secret not in serialized

    denied = guarded.get_verified_patient_meds("not-a-token")
    assert denied["identity_verified"] is False
    assert "prescription" not in denied

    first = guarded.verify_identity(start["verification_id"], "1998-09-15")
    assert first == {
        "identity_verified": False,
        "verification_locked": False,
        "attempts_remaining": 1,
        "instruction": (
            "Do not reveal or hint at the stored DOB. "
            "Ask the patient to check the date and try once more."
        ),
    }
    second = guarded.verify_identity(start["verification_id"], "2002-01-22")
    assert second["verification_locked"] is True
    assert second["attempts_remaining"] == 0
    assert "1971" not in str(second)
    locked = guarded.verify_identity(start["verification_id"], "1971-03-04")
    assert locked["identity_verified"] is False

    valid_start = guarded.start_checkin("PT-0001")
    verified = guarded.verify_identity(valid_start["verification_id"], "4 Mar 1971")
    assert verified["identity_verified"] is True
    assert "1971" not in str(verified)
    payload = guarded.get_verified_patient_meds(verified["verified_session"])
    assert payload["identity_verified"] is True
    assert "dob" not in payload["patient"]
    assert payload["prescription"]["drug_name"]
    print("tokenized identity guardrails: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
