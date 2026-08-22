#!/usr/bin/env python3
"""Replay the 90-second demo against the real tools, with no LLM in the loop.

This is stage insurance. If Nemotron is slow, wobbling, or the box is busy,
this proves the pipeline itself - lookup, guardrail, escalation, audit write,
scheduler handoff - still does exactly what the pitch claims.

It is also the honest version of the demo beat: the "model" here deliberately
UNDER-escalates, classifying a reported side effect as a plain non_adherent.
The deterministic guardrail overrides it. That override is the thing worth
showing a judge.

    python scripts/demo_dry_run.py            # replay, then restore
    python scripts/demo_dry_run.py --keep     # leave the writes in Mongo
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "mcp-server"))

import db          # noqa: E402
import server      # noqa: E402

PATIENT = "PT-0001"
RX = "RX-0001"
PATIENT_LINE = "I stopped a week ago. It was upsetting my stomach."

B, G, Y, R, D, X = ("\033[1m", "\033[32m", "\033[33m", "\033[31m",
                    "\033[2m", "\033[0m")

PACE = float(os.getenv("RXLOCAL_DEMO_PACE", "0"))


def beat(label: str) -> None:
    print(f"\n{Y}{B}── {label} ──{X}")
    if PACE:
        time.sleep(PACE)


def say(who: str, text: str) -> None:
    colour = G if who == "AGENT" else ""
    print(f"  {colour}{B}{who}{X}  {text}")
    if PACE:
        time.sleep(PACE)


def note(text: str) -> None:
    print(f"  {D}// {text}{X}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="do not restore the prescription afterwards")
    args = ap.parse_args()

    st = db.status()
    print(f"{B}RxLocal demo dry run{X}  {D}data mode: {st.get('mode')}{X}")
    if st.get("mode") != "mongo":
        print(f"{R}WARNING: not on MongoDB. The privacy story assumes the "
              f"local instance is up.{X}")

    rx_before = db.find_prescription(RX) or {}
    _raw = rx_before.get("next_checkin_date")
    if isinstance(_raw, str):
        _raw = dt.date.fromisoformat(_raw[:10])
    elif isinstance(_raw, dt.datetime):
        _raw = _raw.date()
    before = (_raw, rx_before.get("status"))

    # ---------------------------------------------------------------
    beat("1. LOOKUP  (get_patient_meds)")
    payload = server.get_patient_meds(PATIENT)
    if payload.get("error"):
        print(f"{R}FAIL: {payload['error']}{X}")
        return 1
    pt, rx = payload["patient"], payload["prescription"]
    note(f"read from MongoDB on this box: {pt['first_name']} {pt['last_name']}, "
         f"DOB {pt['dob']}, {rx['drug_name']}")
    say("AGENT", "Hi, this is the CVS care line with a medication check-in. "
                 "To confirm I'm speaking with the right person, could you "
                 "tell me your date of birth?")
    say("PATIENT", "March 4th, 1971.")

    if pt["dob"] != "1971-03-04":
        print(f"{R}FAIL: DOB mismatch, seed data drifted "
              f"(got {pt['dob']}){X}")
        return 1
    note("DOB matches the record. Identity verified against Mongo, not memory.")

    say("AGENT", f"Thank you. Our records show you were prescribed "
                 f"{rx['drug_name']}. Have you been taking it as prescribed?")
    say("PATIENT", PATIENT_LINE)

    # ---------------------------------------------------------------
    beat("2. GUARDRAIL  (the model gets this wrong on purpose)")
    note("the agent classifies this as: non_adherent / side_effects")
    note("it passes NO escalation arguments - exactly the miss seen in testing")

    result = server.log_outcome(
        patient_id=PATIENT,
        prescription_id=RX,
        outcome="non_adherent",
        raw_patient_text=PATIENT_LINE,
        non_adherence_reason="side_effects",
        notes="Demo dry run - scripted replay, no model in the loop.",
    )
    if result.get("error"):
        print(f"{R}FAIL: {result['error']}{X}")
        return 1

    esc = result.get("escalation") or {}
    forced = esc.get("forced_by_guardrail")
    print(f"  {D}interaction {result['interaction_id']}{X}")
    if not esc:
        print(f"{R}FAIL: guardrail did not escalate a reported side effect{X}")
        return 1

    print(f"  {G}{B}GUARDRAIL OVERRODE THE MODEL{X}")
    stored = db.open_escalation(PATIENT) or {}
    print(f"    matched patient words : {B}{stored.get('guardrail_match')!r}{X}")
    print(f"    red flag              : {esc.get('red_flag_type')}")
    print(f"    severity              : {esc.get('severity')}")
    print(f"    forced by guardrail   : {forced}")
    print(f"    logged outcome        : {esc.get('outcome', 'escalated')}")
    if not forced:
        print(f"{R}FAIL: expected forced_by_guardrail = True{X}")
        return 1

    # ---------------------------------------------------------------
    beat("3. HUMAN HANDOFF  (pharmacist alert)")
    notif = result.get("notification") or {}
    if notif.get("sent"):
        print(f"  {G}Telegram alert delivered{X} {D}- {notif.get('detail')}{X}")
    else:
        print(f"  {Y}alert NOT delivered{X} {D}- {notif.get('detail')}{X}")
        print(f"  {D}run scripts/set_chat_ids.py once you have a chat id{X}")
    say("AGENT", "Thanks for telling me. That's a question for a pharmacist "
                 "rather than something I should answer, so I'm flagging it "
                 "to your care team now.")

    # ---------------------------------------------------------------
    beat("4. LOOP CLOSURE  (schedule_followup)")
    hr = result.get("human_review") or {}
    outcome_for_scheduler = "escalated" if esc else "non_adherent"
    note(f"passing the guardrail's outcome, not the model's: "
         f"{outcome_for_scheduler}")
    sched = server.schedule_followup(PATIENT, RX, outcome_for_scheduler)
    if sched.get("error"):
        print(f"{R}FAIL: {sched['error']}{X}")
        return 1
    print(f"    prescription status   : {B}{sched.get('status')}{X}")
    print(f"    next check-in         : {sched.get('next_checkin_date')}")
    if sched.get("status") != "paused":
        print(f"{R}FAIL: an escalated patient must be paused{X}")
        return 1
    if sched.get("next_checkin_date"):
        print(f"{R}FAIL: paused patient must have no next check-in{X}")
        return 1
    note("automated contact stops while a human is engaged. "
         "The scheduler reads this field, so the loop is genuinely closed.")

    # ---------------------------------------------------------------
    beat("5. AUDIT  (what a judge can ask to see)")
    last = db.last_interaction(PATIENT) or {}
    print(f"    session key           : {db.session_key_for(PATIENT)}")
    print(f"    stored patient words  : {last.get('raw_patient_text')!r}")
    print(f"    escalation id         : {result.get('escalation_id')}")
    print(f"    open escalation row   : "
          f"{stored.get('escalation_id') or stored.get('_id')} "
          f"/ status {stored.get('status')}")

    if not args.keep:
        db.set_schedule(RX, before[0], before[1])
        print(f"\n{D}restored {RX} to {before[1]} / "
              f"{before[0]}  (--keep to leave the writes){X}")
        print(f"{D}the interaction and escalation rows are left in place "
              f"on purpose - they are the audit trail{X}")

    print(f"\n{G}{B}DRY RUN PASSED{X} - lookup, guardrail override, "
          f"handoff, pause, audit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
