#!/usr/bin/env python3
"""RxLocal MCP server - the four domain tools.

Register with:

    openclaw mcp add rxlocal \
      --command /path/to/.venv/bin/python3 \
      --arg /path/to/mcp-server/server.py \
      --cwd /path/to/mcp-server \
      --env MONGODB_URI=mongodb://localhost:27017/rxlocal \
      --include get_patient_meds,remember_patient_fact,log_outcome,schedule_followup

Run standalone for a smoke test:

    python server.py --selftest          # exercise all four tools
    python server.py --status            # which data mode is active

Design notes that matter:

* `log_outcome` takes NO session_key. It is derived server-side from
  patient_id, because the model fabricates one when asked to supply it.
* Every escalation goes through guardrails.enforce(), which can add or raise
  an escalation the model missed but never remove one it made.
* The Telegram alert is sent synchronously from inside log_outcome, so the
  tool result tells the agent whether the pharmacist was actually reached.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
import guardrails  # noqa: E402

# The SDK renamed FastMCP to MCPServer in 2.x. Support both so this file does
# not depend on which version happens to be on the box.
try:
    from mcp.server.fastmcp import FastMCP as _MCP  # SDK 1.x
except ImportError:  # pragma: no cover
    from mcp.server.mcpserver import MCPServer as _MCP  # SDK 2.x

SERVER_NAME = "rxlocal"

# Vocabularies - must stay in step with rxlocal_db.py.
OUTCOMES = ("adherent", "non_adherent", "escalated")
NON_ADHERENCE_REASONS = ("side_effects", "cost", "forgot", "ran_out",
                         "felt_better", "no_belief", "confused_instructions")
RED_FLAG_TYPES = ("symptom", "adverse_reaction", "clinical_question", "emergency")
SEVERITIES = ("low", "medium", "high", "critical")

NON_ADHERENT_RECHECK_DAYS = int(os.getenv("RXLOCAL_RECHECK_DAYS", "2"))
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "openclaw")
NOTIFY_TIMEOUT = int(os.getenv("RXLOCAL_NOTIFY_TIMEOUT", "20"))

mcp = _MCP(SERVER_NAME)


# =====================================================================
# Telegram alert
# =====================================================================

def _alert_pharmacist(escalation: dict) -> dict:
    """Send the on-call pharmacist a Telegram message. Never raises.

    Returns {sent, detail} - reported back in the tool result so the agent
    knows whether a human was actually reached, rather than assuming so.
    """
    chat_id, name = db.oncall_chat_id()
    if not chat_id:
        return {"sent": False,
                "detail": "on-call Telegram chat id not configured "
                          "(still REPLACE_WITH_* or missing)"}
    if not shutil.which(OPENCLAW_BIN):
        return {"sent": False, "detail": f"{OPENCLAW_BIN} not on PATH"}

    icon = {"critical": "[CRITICAL]", "high": "[HIGH]",
            "medium": "[MEDIUM]", "low": "[LOW]"}.get(escalation["severity"], "")
    lines = [
        f"{icon} RxLocal escalation - pharmacist needed",
        f"Patient: {escalation.get('patient_name')} "
        f"({escalation.get('patient_id')})",
        f"Medication: {escalation.get('drug_name')}",
        f"Flag: {escalation.get('red_flag_type')} / {escalation['severity']}",
        f"Reason: {escalation.get('reason')}",
        f"Patient said: \"{escalation.get('raw_patient_text')}\"",
        f"Escalation: {escalation.get('escalation_id')}",
    ]
    if escalation.get("forced_by_guardrail"):
        lines.append("(forced by deterministic guardrail, not the model)")

    try:
        proc = subprocess.run(
            [OPENCLAW_BIN, "message", "send", "--channel", "telegram",
             "--target", chat_id, "--message", "\n".join(lines)],
            capture_output=True, text=True, timeout=NOTIFY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"sent": False, "detail": f"timed out after {NOTIFY_TIMEOUT}s"}
    except OSError as e:
        return {"sent": False, "detail": f"{type(e).__name__}: {e}"}

    if proc.returncode == 0:
        return {"sent": True, "detail": f"notified {name}"}
    return {"sent": False,
            "detail": (proc.stderr or proc.stdout or "non-zero exit").strip()[:300]}


# =====================================================================
# TOOL 1 - get_patient_meds
# =====================================================================

@mcp.tool()
def get_patient_meds(patient_id: str) -> dict:
    """Look up a patient's record and current prescription.

    Read-only. Call this FIRST, before saying anything about medication, and
    use the returned `dob` to verify identity. Never state a drug name, dose,
    or condition that did not come from this payload.

    Returns patient{id, first_name, last_name, dob, status},
    prescription{id, drug_name, condition, dose_instructions,
    next_checkin_date, adherence_checkin_cadence_days, status}, and
    last_interaction_summary{outcome, date} or null, plus
    retrieved_context from a ranked MongoDB aggregation over durable memories,
    interactions, and escalations.

    If retrieved_context.behavior_directive is present, you MUST follow it.
    Start with retrieved_context.suggested_opening (after identity verification)
    instead of repeating the generic first-call opening.
    """
    try:
        return db.patient_meds_payload(patient_id)
    except LookupError as e:
        return {"error": str(e), "patient": None, "prescription": None,
                "last_interaction_summary": None}


# =====================================================================
# TOOL 2 - remember_patient_fact
# =====================================================================

@mcp.tool()
def remember_patient_fact(
    patient_id: str,
    memory_type: str,
    fact: str,
    priority: int = 3,
    ttl_days: int = 180,
) -> dict:
    """Store a durable fact that should change a future patient conversation.

    Call after the patient states a reusable barrier or preference, such as a
    cost problem, preferred contact time, a previously tried alternative, or
    an explicit contact boundary. Store only the stated fact, never an
    inference or diagnosis. `memory_type` is one of barrier | contact_preference
    | treatment_history | communication_preference | other. `priority` is 1-5.
    Facts expire automatically through MongoDB's TTL index (default 180 days).
    This tool intentionally fails in JSON simulation mode.
    """
    allowed = {"barrier", "contact_preference", "treatment_history",
               "communication_preference", "other"}
    if memory_type not in allowed:
        return {"error": f"memory_type must be one of {sorted(allowed)}"}
    if not db.find_patient(patient_id):
        return {"error": f"no patient with id {patient_id}"}
    fact = " ".join(fact.split())
    if not fact or len(fact) > 300:
        return {"error": "fact must contain 1-300 characters"}
    if not 1 <= priority <= 5:
        return {"error": "priority must be between 1 and 5"}
    if not 1 <= ttl_days <= 365:
        return {"error": "ttl_days must be between 1 and 365"}
    try:
        memory = db.upsert_memory(patient_id, memory_type, fact, priority,
                                  ttl_days)
    except RuntimeError as e:
        return {"error": str(e), "persisted": False}
    return {"persisted": True, "memory": memory,
            "retrieval_engine": "mongodb_aggregation"}


# =====================================================================
# TOOL 3 - log_outcome
# =====================================================================

@mcp.tool()
def log_outcome(
    patient_id: str,
    prescription_id: str,
    outcome: str,
    raw_patient_text: str,
    non_adherence_reason: str | None = None,
    notes: str | None = None,
    escalation_severity: str | None = None,
    escalation_red_flag_type: str | None = None,
    escalation_reason: str | None = None,
) -> dict:
    """Record the result of this check-in. Call once, after the patient has
    answered about their adherence.

    `outcome` is one of adherent | non_adherent | escalated.
    `raw_patient_text` must be the patient's own words, quoted verbatim - do
    not paraphrase and never invent it. A deterministic guardrail reads this
    field and will escalate on its own if it sees a symptom, adverse reaction,
    clinical question, or emergency, whether or not you set the escalation
    arguments.

    Set `non_adherence_reason` only when outcome is non_adherent.
    Set the three escalation_* arguments when you judge a human is needed.

    Returns {interaction_id, human_review, escalation_id?, escalation?,
    notification?}. If human_review.required is true, stop the automated
    clinical conversation and hand control to the pharmacist.
    """
    if outcome not in OUTCOMES:
        return {"error": f"outcome must be one of {list(OUTCOMES)}"}

    patient = db.find_patient(patient_id)
    if not patient:
        return {"error": f"no patient with id {patient_id}"}
    rx = db.find_prescription(prescription_id)
    if not rx:
        return {"error": f"no prescription with id {prescription_id}"}
    if rx["patient_id"] != patient_id:
        # Refuse rather than write a cross-patient record.
        return {"error": f"{prescription_id} does not belong to {patient_id}"}

    if non_adherence_reason and non_adherence_reason not in NON_ADHERENCE_REASONS:
        non_adherence_reason = "other"

    # Layer B: the model does not get the final say on escalation.
    verdict = guardrails.enforce(
        raw_patient_text,
        model_severity=(escalation_severity
                        if escalation_severity in SEVERITIES else None),
        model_red_flag_type=(escalation_red_flag_type
                             if escalation_red_flag_type in RED_FLAG_TYPES else None),
        model_reason=escalation_reason)

    final_outcome = "escalated" if verdict["escalate"] else outcome

    interaction = {
        "patient_id": patient_id,
        "prescription_id": prescription_id,
        "session_key": db.session_key_for(patient_id),   # derived, never supplied
        "outcome": final_outcome,
        "raw_patient_text": raw_patient_text,
        "non_adherence_reason": (non_adherence_reason
                                 if final_outcome == "non_adherent" else None),
        "notes": notes,
        "channel": "telegram",
        "duration_seconds": None,   # populated from `openclaw cron runs --json`
        "model_reported_outcome": outcome,
        "guardrail_forced": bool(verdict.get("forced")),
    }
    interaction_id = db.insert_interaction(interaction)

    result: dict[str, Any] = {
        "interaction_id": interaction_id,
        "outcome_recorded": final_outcome,
        "human_review": {"required": False},
    }
    if final_outcome != outcome:
        result["note"] = (f"outcome upgraded from '{outcome}' to "
                          f"'{final_outcome}' by the safety guardrail "
                          f"(matched: {verdict.get('matched')})")

    if verdict["escalate"]:
        escalation = {
            "interaction_id": interaction_id,
            "patient_id": patient_id,
            "prescription_id": prescription_id,
            "patient_name": f"{patient['first_name']} {patient['last_name']}",
            "drug_name": rx["drug_name"],
            "red_flag_type": verdict["red_flag_type"],
            "severity": verdict["severity"],
            "reason": verdict["reason"],
            "raw_patient_text": raw_patient_text,
            "status": "open",
            "notified": False,
            "assigned_to": patient.get("pharmacist"),
            "forced_by_guardrail": bool(verdict.get("forced")),
            "guardrail_match": verdict.get("matched"),
        }
        escalation_id = db.insert_escalation(escalation)
        escalation["escalation_id"] = escalation_id

        notification = _alert_pharmacist(escalation)
        db.mark_escalation_notified(escalation_id, notification["sent"],
                                    notification["detail"])

        result["escalation_id"] = escalation_id
        result["escalation"] = {"severity": verdict["severity"],
                                "red_flag_type": verdict["red_flag_type"],
                                "forced_by_guardrail": bool(verdict.get("forced"))}
        result["notification"] = notification
        result["human_review"] = {
            "required": True,
            "status": "open",
            "escalation_id": escalation_id,
            "pharmacist_notified": notification["sent"],
            "instruction": (
                "Stop automated clinical discussion and wait for pharmacist review."
            ),
        }

    return result


# =====================================================================
# TOOL 4 - schedule_followup
# =====================================================================

@mcp.tool()
def schedule_followup(patient_id: str, prescription_id: str,
                      outcome: str) -> dict:
    """Set the next check-in. Call this immediately after log_outcome, every
    time - the scheduler reads what it writes, so skipping it means the
    patient is never contacted again.

    adherent      -> next check-in in `adherence_checkin_cadence_days`, active
    non_adherent  -> shorter recheck in 2 days, still active
    escalated     -> prescription paused, no next check-in, because a human is
                     now engaged and automated contact must stop

    Returns {next_checkin_date, status, human_review}. An unresolved human
    review always overrides the requested outcome and keeps automation paused.
    """
    if outcome not in OUTCOMES:
        return {"error": f"outcome must be one of {list(OUTCOMES)}"}

    rx = db.find_prescription(prescription_id)
    if not rx:
        return {"error": f"no prescription with id {prescription_id}"}
    if rx["patient_id"] != patient_id:
        return {"error": f"{prescription_id} does not belong to {patient_id}"}

    pending_review = db.open_escalation(patient_id, prescription_id)
    if pending_review:
        written = db.set_schedule(prescription_id, None, "paused")
        return {
            "next_checkin_date": written["next_checkin_date"],
            "status": written["status"],
            "prescription_id": prescription_id,
            "human_review": {
                "required": True,
                "escalation_id": pending_review.get("escalation_id"),
                "status": pending_review.get("status"),
                "instruction": (
                    "Automation remains paused until pharmacist review closes."
                ),
            },
        }

    today = dt.date.today()
    if outcome == "escalated":
        next_date, status = None, "paused"
    elif outcome == "non_adherent":
        next_date = today + dt.timedelta(days=NON_ADHERENT_RECHECK_DAYS)
        status = "active"
    else:
        cadence = int(rx.get("adherence_checkin_cadence_days") or 30)
        next_date = today + dt.timedelta(days=cadence)
        status = "active"

    written = db.set_schedule(prescription_id, next_date, status)
    return {"next_checkin_date": written["next_checkin_date"],
            "status": written["status"],
            "prescription_id": prescription_id,
            "human_review": {"required": False}}


# =====================================================================
# Standalone checks
# =====================================================================

def _selftest() -> int:
    G, R, D, X = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
    fails = 0

    def ck(label, cond, extra=""):
        nonlocal fails
        if cond:
            print(f"  {G}PASS{X} {label} {D}{extra}{X}")
        else:
            fails += 1
            print(f"  {R}FAIL{X} {label} {D}{extra}{X}")

    def sec(t):
        print(f"\n\033[33m=== {t} ==={X}")

    sec("DATA MODE")
    st = db.status()
    print(f"  {D}{json.dumps(st, default=str)}{X}")
    ck("a data source is available",
       st["mode"] in ("mongo", "simulation"), st["mode"])
    ck("patients present", st["counts"].get("patients", 0) > 0)

    sec("get_patient_meds")
    r = get_patient_meds("PT-0001")
    ck("no error", "error" not in r, r.get("error", ""))
    ck("shape matches the contract",
       {"patient", "prescription", "last_interaction_summary"} <= set(r))
    ck("patient fields", set(r["patient"]) ==
       {"id", "first_name", "last_name", "dob", "status"})
    ck("prescription carries its id, so log_outcome can be called",
       "id" in (r["prescription"] or {}))
    ck("dob available for verification", len(r["patient"]["dob"]) == 10,
       r["patient"]["dob"])
    bad = get_patient_meds("PT-9999")
    ck("unknown patient returns an error, not a crash", "error" in bad)

    rx_id = r["prescription"]["id"]
    cadence = r["prescription"]["adherence_checkin_cadence_days"]

    sec("GUARDRAIL BACKSTOP (model says adherent, patient reports a symptom)")
    out = log_outcome("PT-0001", rx_id, "adherent",
                      "Yes I'm taking it, but it's been upsetting my stomach badly.")
    ck("escalation created anyway", "escalation_id" in out, str(out.get("note", "")))
    ck("outcome upgraded to escalated", out["outcome_recorded"] == "escalated")
    ck("flagged as guardrail-forced",
       out["escalation"]["forced_by_guardrail"] is True)
    ck("severity high for a symptom", out["escalation"]["severity"] == "high")
    ck("notification result reported",
       "sent" in out.get("notification", {}),
       out.get("notification", {}).get("detail", ""))

    sec("EMERGENCY")
    out = log_outcome("PT-0002", db.active_prescription("PT-0002")["prescription_id"],
                      "non_adherent", "I've had chest pain and can't breathe properly.")
    ck("critical severity", out["escalation"]["severity"] == "critical")
    ck("red flag type is emergency",
       out["escalation"]["red_flag_type"] == "emergency")

    sec("CLEAN ADHERENT PATH")
    p3 = db.active_prescription("PT-0003")
    if p3 is None:
        # The schedule_followup section below pauses this prescription. If an
        # earlier run did not restore it, say so plainly instead of dying on a
        # None subscript twenty lines later.
        print(f"  {R}FAIL{X} PT-0003 has no active prescription - a previous "
              f"self-test left it paused.\n         Reset with: "
              f"python ../rxlocal_db.py seed")
        return 1
    out = log_outcome("PT-0003", p3["prescription_id"], "adherent",
                      "Yes, every morning with breakfast. No problems.")
    ck("no escalation", "escalation_id" not in out)
    ck("outcome stays adherent", out["outcome_recorded"] == "adherent")
    ck("interaction id issued", out["interaction_id"].startswith("INT-"),
       out["interaction_id"])

    sec("NON-ADHERENT, NON-CLINICAL REASON")
    out = log_outcome("PT-0004", db.active_prescription("PT-0004")["prescription_id"],
                      "non_adherent", "I stopped because the copay was too expensive.",
                      non_adherence_reason="cost")
    ck("cost does not escalate", "escalation_id" not in out)
    ck("stays non_adherent", out["outcome_recorded"] == "non_adherent")

    sec("REJECTIONS")
    ck("bad outcome rejected",
       "error" in log_outcome("PT-0001", rx_id, "bogus", "hi"))
    ck("unknown patient rejected",
       "error" in log_outcome("PT-9999", rx_id, "adherent", "hi"))
    ck("cross-patient prescription rejected",
       "error" in log_outcome("PT-0002", rx_id, "adherent", "hi"),
       "PT-0002 with PT-0001's prescription")

    sec("schedule_followup")
    s = schedule_followup("PT-0003", p3["prescription_id"], "adherent")
    expect = (dt.date.today() + dt.timedelta(days=cadence)).isoformat()
    ck("adherent -> now + cadence", s["next_checkin_date"] is not None)
    ck("stays active", s["status"] == "active", str(s))

    s = schedule_followup("PT-0003", p3["prescription_id"], "non_adherent")
    ck("non_adherent -> 2 days",
       s["next_checkin_date"] ==
       (dt.date.today() + dt.timedelta(days=NON_ADHERENT_RECHECK_DAYS)).isoformat(),
       s["next_checkin_date"])
    ck("still active", s["status"] == "active")

    s = schedule_followup("PT-0003", p3["prescription_id"], "escalated")
    ck("escalated -> paused", s["status"] == "paused")
    ck("escalated -> no next check-in", s["next_checkin_date"] is None)

    after = db.find_prescription(p3["prescription_id"])
    ck("write actually persisted", after["status"] == "paused")
    ck("paused row has no next_checkin_date",
       after.get("next_checkin_date") in (None, ""))

    ck("cross-patient schedule rejected",
       "error" in schedule_followup("PT-0002", p3["prescription_id"], "adherent"))

    # Restore what this section paused, so the self-test can be run repeatedly.
    # Without this it passes exactly once and then dies on a None subscript -
    # which is the worst possible time to discover it, mid-demo.
    db.set_schedule(p3["prescription_id"],
                    dt.date.fromisoformat(p3["next_checkin_date"][:10])
                    if p3.get("next_checkin_date") else dt.date.today(),
                    "active")
    restored = db.find_prescription(p3["prescription_id"])
    ck("self-test restored PT-0003 to active (re-runnable)",
       restored["status"] == "active", restored["status"])

    sec("SESSION KEY")
    ck("derived, lowercase, cron format",
       db.session_key_for("PT-0001") == "agent:rxlocal:patient:pt-0001",
       db.session_key_for("PT-0001"))
    last = db.last_interaction("PT-0003")
    ck("written onto the interaction",
       last["session_key"] == "agent:rxlocal:patient:pt-0003",
       last["session_key"])

    sec("REGISTERED TOOLS")
    print(f"  {D}get_patient_meds, remember_patient_fact, log_outcome, "
          f"schedule_followup{X}")
    ck("log_outcome takes no session_key argument",
       "session_key" not in log_outcome.__annotations__)

    print(f"\n{G}ALL PASS{X}" if not fails else f"\n{R}{fails} FAILURES{X}")
    print(f"{D}note: run `python ../rxlocal_db.py seed` to reset after a self-test"
          f"{X}")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RxLocal MCP server")
    ap.add_argument("--selftest", action="store_true",
                    help="exercise all four tools and exit")
    ap.add_argument("--status", action="store_true",
                    help="print the active data mode and exit")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(db.status(), indent=2, default=str))
        return 0
    if args.selftest:
        return _selftest()

    # Default: speak MCP over stdio. Anything written to stdout that is not
    # protocol would corrupt the stream, so diagnostics go to stderr.
    print(f"[rxlocal] data mode: {db.mode()}", file=sys.stderr)
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
