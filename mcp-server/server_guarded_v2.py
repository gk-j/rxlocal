#!/usr/bin/env python3
"""RxLocal MCP entrypoint with enforced, tokenized identity verification."""
from __future__ import annotations

import datetime as dt
import re
import secrets
import time

import server


DATE_FORMATS = (
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d %Y",
    "%b %d %Y", "%d %B %Y", "%d %b %Y",
)
MAX_ATTEMPTS = 2
SESSION_TTL_SECONDS = 30 * 60
_pending: dict[str, dict] = {}
_verified: dict[str, dict] = {}


def _parse_dob(value: str) -> dt.date | None:
    cleaned = " ".join(str(value or "").strip().replace(",", " ").split())
    cleaned = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", cleaned, flags=re.IGNORECASE)
    for date_format in DATE_FORMATS:
        try:
            return dt.datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    return None


def _prune() -> None:
    cutoff = time.monotonic() - SESSION_TTL_SECONDS
    for store in (_pending, _verified):
        for key in [key for key, value in store.items() if value["created"] < cutoff]:
            store.pop(key, None)


@server.mcp.tool()
def start_checkin(patient_id: str) -> dict:
    """Start identity verification without releasing DOB or clinical data.

    Call first for scheduler-provided ``patient_id``. The result contains only
    the first name needed for a minimal greeting and an opaque verification ID.
    """
    _prune()
    patient = server.db.find_patient(patient_id)
    if not patient:
        return {"error": "Unable to start this check-in.", "verification_required": True}
    verification_id = secrets.token_urlsafe(24)
    _pending[verification_id] = {
        "patient_id": patient_id,
        "attempts": 0,
        "locked": False,
        "created": time.monotonic(),
    }
    return {
        "verification_id": verification_id,
        "patient_first_name": patient["first_name"],
        "verification_required": True,
        "identity_verified": False,
        "instruction": "Ask the patient for their date of birth and nothing else.",
    }


@server.mcp.tool()
def verify_identity(verification_id: str, provided_dob: str) -> dict:
    """Compare patient-supplied DOB and return pass/fail without secret data.

    Two failed attempts lock the verification ID. The stored DOB, normalized
    input, partial-match details, and patient record are never returned.
    """
    _prune()
    state = _pending.get(verification_id)
    if not state or state["locked"]:
        return {
            "identity_verified": False,
            "verification_locked": True,
            "attempts_remaining": 0,
            "instruction": "Stop patient-specific discussion and end or offer human follow-up.",
        }

    patient = server.db.find_patient(state["patient_id"])
    entered = _parse_dob(provided_dob)
    stored = _parse_dob(str((patient or {}).get("dob") or ""))
    if patient and entered and stored and entered == stored:
        verified_session = secrets.token_urlsafe(32)
        _verified[verified_session] = {
            "patient_id": state["patient_id"],
            "created": time.monotonic(),
        }
        _pending.pop(verification_id, None)
        return {
            "identity_verified": True,
            "verification_locked": False,
            "attempts_remaining": MAX_ATTEMPTS - state["attempts"],
            "verified_session": verified_session,
            "instruction": "Identity confirmed. Retrieve the verified medication context.",
        }

    state["attempts"] += 1
    remaining = max(0, MAX_ATTEMPTS - state["attempts"])
    state["locked"] = remaining == 0
    return {
        "identity_verified": False,
        "verification_locked": state["locked"],
        "attempts_remaining": remaining,
        "instruction": (
            "Do not reveal or hint at the stored DOB. "
            + ("Ask the patient to check the date and try once more."
               if remaining else
               "Stop patient-specific discussion and end or offer human follow-up.")
        ),
    }


@server.mcp.tool()
def get_verified_patient_meds(verified_session: str) -> dict:
    """Return medication context only after deterministic identity verification."""
    _prune()
    state = _verified.get(verified_session)
    if not state:
        return {
            "error": "A valid verified session is required.",
            "identity_verified": False,
        }
    payload = server.db.patient_meds_payload(state["patient_id"])
    patient = dict(payload.get("patient") or {})
    patient.pop("dob", None)
    payload["patient"] = patient
    payload["identity_verified"] = True
    return payload


if __name__ == "__main__":
    raise SystemExit(server.main())
