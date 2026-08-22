#!/usr/bin/env python3
"""
RxLocal - database schema + seeding. Built to match the MCP server's contracts.

MongoDB Community Edition, self-hosted. Database `rxlocal`.
The MCP server connects with:  MONGODB_URI=mongodb://<host>:27017/rxlocal

USAGE
    python rxlocal_db.py seed              # wipe + seed everything
    python rxlocal_db.py check             # preflight, changes nothing
    python rxlocal_db.py show PT-0001      # exactly what get_patient_meds returns
    python rxlocal_db.py queue             # what the sweep / dashboard reads
    python rxlocal_db.py analytics         # what /api/analytics/* reads
    python rxlocal_db.py telegram          # rows still needing a chat id
    python rxlocal_db.py export FILE       # write simulated_db.json
    python rxlocal_db.py test              # self-test

INSTALL
    pip install pymongo python-dotenv
    export MONGODB_URI=mongodb://localhost:27017/rxlocal

SCHEMA - four collections, shaped by the three tools:

  patients        get_patient_meds -> patient{id, first_name, last_name, dob, status}
  prescriptions   get_patient_meds -> prescription{drug_name, condition,
                    dose_instructions, next_checkin_date,
                    adherence_checkin_cadence_days, status}
                  schedule_followup writes next_checkin_date + status
  interactions    log_outcome writes one doc per check-in
                  get_patient_meds -> last_interaction_summary{outcome, date}
  escalations     log_outcome writes one when a red flag fires
  memories        durable patient facts; TTL-expired and ranked with recent
                  interactions + escalations for the next cold agent run

  staff           on-call pharmacist Telegram target (not a tool contract,
                  but log_outcome needs somewhere to read the chat id from)

NO REAL PATIENT DATA. Names and DOBs are invented.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# =====================================================================
# CONFIG
# =====================================================================

# MONGODB_URI is what the MCP server uses, so honour that name first.
MONGO_URI = (os.getenv("MONGODB_URI")
             or os.getenv("RXLOCAL_MONGO_URI")
             or "mongodb://localhost:27017/rxlocal")
TIMEOUT_MS = int(os.getenv("RXLOCAL_MONGO_TIMEOUT_MS", "5000"))

# Session keys must match what OpenClaw cron uses:
#   --session-key agent:rxlocal:patient:pt-0001
SESSION_KEY_PREFIX = os.getenv("RXLOCAL_SESSION_PREFIX", "agent:rxlocal:patient:")


def session_key_for(patient_id: str) -> str:
    """Derived server-side from patient_id, lowercased. The model never
    supplies this - it fabricates garbage when asked to."""
    return f"{SESSION_KEY_PREFIX}{patient_id.lower()}"


def db_name_from_uri(uri: str) -> str:
    """mongodb://host:27017/rxlocal -> rxlocal"""
    tail = uri.split("://", 1)[-1]
    path = tail.split("/", 1)[1] if "/" in tail else ""
    name = path.split("?", 1)[0]
    return name or os.getenv("RXLOCAL_MONGO_DB", "rxlocal")


MONGO_DB = db_name_from_uri(MONGO_URI)


def redact(uri: str) -> str:
    if "@" not in uri:
        return uri
    scheme, _, rest = uri.partition("://")
    creds, _, host = rest.rpartition("@")
    user = creds.split(":", 1)[0] if creds else ""
    return f"{scheme}://{user}:***@{host}"


# =====================================================================
# CONNECTION
# =====================================================================

_client: MongoClient | None = None
_err: Exception | None = None


class ConnectionUnavailable(Exception):
    pass


def get_client() -> MongoClient:
    global _client, _err
    if _client is not None:
        return _client
    try:
        c = MongoClient(MONGO_URI, serverSelectionTimeoutMS=TIMEOUT_MS,
                        connectTimeoutMS=TIMEOUT_MS, appname="rxlocal-seed")
        c.admin.command("ping")
        _client, _err = c, None
        return _client
    except Exception as e:  # noqa: BLE001 - includes DNS/config errors
        _err = e
        raise ConnectionUnavailable(str(e)) from e


def get_db() -> Database:
    return get_client()[MONGO_DB]


def ping() -> bool:
    try:
        get_client()
        return True
    except (PyMongoError, ConnectionUnavailable):
        return False


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def midnight(d: dt.date) -> dt.datetime:
    """Dates are stored as BSON dates at UTC midnight, so date comparisons in
    the sweep query are exact rather than time-of-day dependent."""
    return dt.datetime.combine(d, dt.time.min, tzinfo=dt.timezone.utc)


def as_date(v) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str):
        return dt.date.fromisoformat(v[:10])
    return None


def clean(doc: dict | None) -> dict | None:
    """Strip ObjectId, ISO-format datetimes."""
    if doc is None:
        return None
    out = {}
    for k, v in doc.items():
        if k == "_id":
            continue
        out[k] = v.isoformat() if isinstance(v, dt.datetime) else v
    return out


def clean_all(docs) -> list[dict]:
    return [clean(d) for d in docs]


# =====================================================================
# SCHEMA
# =====================================================================

PATIENTS = "patients"
PRESCRIPTIONS = "prescriptions"
INTERACTIONS = "interactions"
ESCALATIONS = "escalations"
MEMORIES = "memories"
STAFF = "staff"

COLLECTIONS = (PATIENTS, PRESCRIPTIONS, INTERACTIONS, ESCALATIONS, MEMORIES, STAFF)

# Vocabularies. These are contracts with the MCP server - log_outcome and
# schedule_followup both branch on `outcome`, so keep them in sync.
OUTCOMES = ["adherent", "non_adherent", "escalated"]
PRESCRIPTION_STATUS = ["active", "paused", "completed"]
PATIENT_STATUS = ["active", "inactive"]
NON_ADHERENCE_REASONS = ["side_effects", "cost", "forgot", "ran_out",
                         "felt_better", "no_belief", "confused_instructions"]
RED_FLAG_TYPES = ["symptom", "adverse_reaction", "clinical_question", "emergency"]
SEVERITIES = ["low", "medium", "high", "critical"]
ESCALATION_STATUS = ["open", "acknowledged", "resolved"]

# Telegram chat ids are a manual pre-demo step. Seeded as placeholders so the
# `telegram` command can list exactly what still needs filling in.
CHAT_ID_PLACEHOLDER = "REPLACE_WITH_{}_CHAT_ID"

INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict]]] = {
    PATIENTS: [
        ([("patient_id", ASCENDING)], {"unique": True, "name": "patient_id_unique"}),
        ([("dob", ASCENDING)], {"name": "dob_verify"}),
        ([("telegram_chat_id", ASCENDING)], {"name": "telegram"}),
    ],
    PRESCRIPTIONS: [
        ([("prescription_id", ASCENDING)],
         {"unique": True, "name": "prescription_id_unique"}),
        ([("patient_id", ASCENDING)], {"name": "patient"}),
        # THE sweep index: "which check-ins are due?"
        ([("status", ASCENDING), ("next_checkin_date", ASCENDING)],
         {"name": "due_sweep"}),
    ],
    INTERACTIONS: [
        ([("patient_id", ASCENDING), ("created_at", DESCENDING)],
         {"name": "patient_recent"}),
        ([("session_key", ASCENDING), ("created_at", DESCENDING)],
         {"name": "session"}),
        ([("created_at", DESCENDING)], {"name": "recent"}),
        ([("outcome", ASCENDING), ("created_at", DESCENDING)],
         {"name": "outcome_trend"}),
    ],
    ESCALATIONS: [
        ([("status", ASCENDING), ("severity", ASCENDING)], {"name": "open_by_severity"}),
        ([("created_at", DESCENDING)], {"name": "recent"}),
        ([("patient_id", ASCENDING)], {"name": "patient"}),
    ],
    MEMORIES: [
        ([("memory_id", ASCENDING)],
         {"unique": True, "name": "memory_id_unique"}),
        ([("patient_id", ASCENDING), ("priority", DESCENDING),
          ("created_at", DESCENDING)], {"name": "patient_ranked"}),
        ([("patient_id", ASCENDING), ("memory_type", ASCENDING),
          ("normalized_fact", ASCENDING)],
         {"unique": True, "name": "patient_fact_unique"}),
        # Mongo deletes stale facts in the background. expireAfterSeconds=0
        # means expires_at itself is the expiry instant.
        ([("expires_at", ASCENDING)],
         {"expireAfterSeconds": 0, "name": "stale_memory_ttl"}),
    ],
    STAFF: [([("role", ASCENDING)], {"name": "role"})],
}

VALIDATORS = {
    PATIENTS: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["patient_id", "first_name", "last_name", "dob", "status"],
        "properties": {
            "patient_id": {"bsonType": "string", "pattern": "^PT-[0-9]{4}$"},
            "dob": {"bsonType": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
            "status": {"enum": PATIENT_STATUS},
        }}},
    PRESCRIPTIONS: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["prescription_id", "patient_id", "drug_name", "condition",
                     "dose_instructions", "adherence_checkin_cadence_days", "status"],
        "properties": {
            "prescription_id": {"bsonType": "string", "pattern": "^RX-[0-9]{4}$"},
            "patient_id": {"bsonType": "string", "pattern": "^PT-[0-9]{4}$"},
            "status": {"enum": PRESCRIPTION_STATUS},
            "adherence_checkin_cadence_days": {"bsonType": ["int", "double"],
                                               "minimum": 1},
            "next_checkin_date": {"bsonType": ["date", "null"]},
        }}},
    INTERACTIONS: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["interaction_id", "patient_id", "prescription_id",
                     "session_key", "outcome", "created_at"],
        "properties": {"outcome": {"enum": OUTCOMES}}}},
    ESCALATIONS: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["escalation_id", "patient_id", "severity", "status"],
        "properties": {
            "severity": {"enum": SEVERITIES},
            "red_flag_type": {"enum": RED_FLAG_TYPES},
            "status": {"enum": ESCALATION_STATUS},
        }}},
    MEMORIES: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["memory_id", "patient_id", "memory_type", "fact",
                     "normalized_fact", "priority", "created_at", "expires_at"],
        "properties": {
            "memory_id": {"bsonType": "string", "pattern": "^MEM-[0-9]{5}$"},
            "patient_id": {"bsonType": "string", "pattern": "^PT-[0-9]{4}$"},
            "memory_type": {"enum": ["barrier", "contact_preference",
                                      "treatment_history",
                                      "communication_preference", "other"]},
            "priority": {"bsonType": "int", "minimum": 1, "maximum": 5},
            "expires_at": {"bsonType": "date"},
        }}},
}


def ensure_collections(db: Database) -> None:
    existing = set(db.list_collection_names())
    for name in COLLECTIONS:
        if name not in existing:
            db.create_collection(name)


def ensure_validators(db: Database) -> None:
    """Warn-level on purpose: a schema complaint must never kill a live demo,
    but a malformed write still shows in the server log."""
    for name, validator in VALIDATORS.items():
        try:
            db.command({"collMod": name, "validator": validator,
                        "validationLevel": "moderate", "validationAction": "warn"})
        except Exception:  # noqa: BLE001
            pass


def ensure_indexes(db: Database) -> None:
    """Idempotent and tolerant of an older database. Mongo refuses an index
    whose key pattern already exists under another name, so drop and recreate
    rather than crash on boot."""
    for name, specs in INDEXES.items():
        col = db[name]
        existing = col.index_information()
        for keys, opts in specs:
            wanted = [list(k) for k in keys]
            for other, info in existing.items():
                if other in ("_id_", opts["name"]):
                    continue
                if [list(k) for k in info.get("key", [])] == wanted:
                    col.drop_index(other)
            try:
                col.create_index(keys, **opts)
            except DuplicateKeyError as e:
                # A unique index over pre-existing docs that violate it. Almost
                # always leftovers from an older schema - say so plainly rather
                # than dumping a driver traceback.
                raise SystemExit(
                    f"\nCannot build unique index {opts['name']} on "
                    f"{name}: existing documents violate it.\n"
                    f"  {e}\n"
                    f"This usually means stale data from an earlier schema.\n"
                    f"Fix with:  python rxlocal_db.py seed\n") from e
            except OperationFailure as e:
                if e.code not in (85, 86):
                    raise
                col.drop_index(opts["name"])
                col.create_index(keys, **opts)


def setup_schema(db: Database) -> None:
    ensure_collections(db)
    ensure_validators(db)
    ensure_indexes(db)


def counts(db: Database) -> dict[str, int]:
    return {n: db[n].count_documents({}) for n in COLLECTIONS}


# =====================================================================
# SYNTHETIC DATA
# =====================================================================

# The first three are the Telegram demo patients - team members stand in as
# these. Everyone else exists so the dashboard queue and analytics have body.
DEMO_PATIENT_COUNT = 3
PATIENT_COUNT = 16
HISTORY_DAYS = 90

PEOPLE = [
    ("Maria", "Delgado", "1971-03-04"), ("James", "Whitfield", "1958-11-22"),
    ("Aisha", "Okonkwo", "1966-07-09"), ("Robert", "Brennan", "1949-02-17"),
    ("Linda", "Castellanos", "1975-05-30"), ("Wei", "Zhang", "1962-09-12"),
    ("Carlos", "Ramirez", "1980-01-25"), ("Dorothy", "Sullivan", "1944-06-03"),
    ("Samuel", "Achebe", "1969-12-08"), ("Priya", "Nair", "1983-04-19"),
    ("Frank", "Kowalski", "1955-08-27"), ("Grace", "Tran", "1972-10-14"),
    ("Omar", "Haddad", "1967-03-21"), ("Betty", "Lindqvist", "1951-01-06"),
    ("Diego", "Ortega", "1978-11-02"), ("Fatima", "Rahman", "1964-05-16"),
]

# (drug_name, condition, dose_instructions, cadence_days)
REGIMENS = [
    ("Metformin 500mg", "Type 2 Diabetes", "Take one tablet twice daily with meals", 30),
    ("Metformin 1000mg", "Type 2 Diabetes", "Take one tablet twice daily with meals", 30),
    ("Lisinopril 10mg", "Hypertension", "Take one tablet once daily in the morning", 30),
    ("Amlodipine 5mg", "Hypertension", "Take one tablet once daily", 30),
    ("Atorvastatin 20mg", "High Cholesterol", "Take one tablet at bedtime", 45),
    ("Levothyroxine 50mcg", "Hypothyroidism",
     "Take one tablet once daily, 30 minutes before breakfast", 60),
    ("Warfarin 5mg", "Atrial Fibrillation",
     "Take one tablet once daily at the same time each day", 14),
    ("Albuterol inhaler", "Asthma", "Two puffs every 4 to 6 hours as needed", 30),
]

PHARMACISTS = ["PharmD Chen", "PharmD Alvarez", "PharmD Osei"]
CLINICS = [("Dr. Alan Reyes", "Northside Family Medicine"),
           ("Dr. Priya Menon", "Riverside Internal Medicine"),
           ("Dr. Susan Okafor", "Midtown Primary Care")]

# Where each active prescription's next_checkin_date lands relative to today,
# so the dashboard queue has every bucket populated rather than one.
CHECKIN_OFFSETS = [-9, -4, -2, -1,      # Overdue
                   0, 0, 0,             # Due Today
                   1, 2, 3, 5, 7,       # Upcoming
                   12, 18, 25, 30]      # Not due

RAW_TEXT = {
    "adherent": [
        "Yes, taking it every day with breakfast and dinner.",
        "Yep, no problems at all.",
        "Every morning, haven't missed one.",
    ],
    "non_adherent": [
        "I stopped about a week ago, I couldn't afford the copay.",
        "I keep forgetting the evening one.",
        "I ran out and haven't been back to the pharmacy.",
        "I felt fine so I stopped taking it.",
        "I don't think it's doing anything, honestly.",
    ],
    "escalated": [
        "It's been upsetting my stomach pretty badly.",
        "I've had a rash on my arms since I started it.",
        "I get dizzy about an hour after I take it.",
        "Should I stop taking it if I'm feeling worse?",
    ],
}

ESCALATION_TEXT = {
    "symptom": ("Patient reports stomach upset since starting medication", "high"),
    "adverse_reaction": ("Patient reports rash following medication start", "high"),
    "clinical_question": ("Patient asking whether to stop medication", "medium"),
    "emergency": ("Patient reports chest pain and shortness of breath", "critical"),
}


def build_patients(count: int = PATIENT_COUNT) -> list[dict]:
    out = []
    for i in range(count):
        first, last, dob = PEOPLE[i % len(PEOPLE)]
        pid = f"PT-{i + 1:04d}"
        # Only the demo patients get a Telegram placeholder. The rest are
        # dashboard population and are never cron-contacted.
        chat_id = (CHAT_ID_PLACEHOLDER.format(pid.replace("-", "_"))
                   if i < DEMO_PATIENT_COUNT else None)
        prescriber, clinic = CLINICS[i % len(CLINICS)]
        out.append({
            "patient_id": pid,
            "first_name": first,
            "last_name": last,
            "dob": dob,
            "status": "active" if i < count - 1 else "inactive",
            "phone": f"+1-555-{100 + i:04d}",
            "preferred_language": "en",
            "telegram_chat_id": chat_id,
            "prescriber": prescriber,
            "clinic": clinic,
            "pharmacist": PHARMACISTS[i % len(PHARMACISTS)],
            "created_at": midnight(dt.date.today() - dt.timedelta(days=200 + i)),
        })
    return out


def build_prescriptions(patients: list[dict], rng: random.Random,
                        today: dt.date) -> list[dict]:
    out: list[dict] = []
    n = 0
    for i, p in enumerate(patients):
        # Most patients have one prescription; a few have two.
        for _ in range(1 if i % 5 else 2):
            drug, condition, dose, cadence = REGIMENS[n % len(REGIMENS)]
            n += 1
            rx_id = f"RX-{n:04d}"

            if p["status"] == "inactive":
                status, next_checkin = "completed", None
            else:
                offset = CHECKIN_OFFSETS[(n - 1) % len(CHECKIN_OFFSETS)]
                status = "active"
                next_checkin = midnight(today + dt.timedelta(days=offset))

            out.append({
                "prescription_id": rx_id,
                "patient_id": p["patient_id"],
                "drug_name": drug,
                "condition": condition,
                "dose_instructions": dose,
                "adherence_checkin_cadence_days": cadence,
                "next_checkin_date": next_checkin,
                "status": status,
                "prescribed_on": midnight(today - dt.timedelta(
                    days=rng.randint(60, 400))),
                "last_refill_date": midnight(today - dt.timedelta(
                    days=rng.randint(3, 45))),
                "refills_remaining": rng.randint(0, 5),
            })
    return out


def build_history(patients: list[dict], prescriptions: list[dict],
                  rng: random.Random, today: dt.date) -> tuple[list[dict], list[dict]]:
    """Backfill ~90 days of interactions and escalations.

    Without this the dashboard's analytics views - adherence trend,
    non-adherence breakdown, escalation rate, call volume - are empty on stage,
    which is a worse look than having no charts at all.
    """
    interactions: list[dict] = []
    escalations: list[dict] = []
    by_patient = {p["patient_id"]: p for p in patients}
    i_n = e_n = 0

    for rx in prescriptions:
        cadence = rx["adherence_checkin_cadence_days"]
        # Walk backwards from today at roughly the check-in cadence.
        day = rng.randint(1, max(2, cadence // 2))
        while day < HISTORY_DAYS:
            when = today - dt.timedelta(days=day)
            day += max(3, int(cadence * rng.uniform(0.7, 1.3)))

            roll = rng.random()
            if roll < 0.62:
                outcome = "adherent"
            elif roll < 0.90:
                outcome = "non_adherent"
            else:
                outcome = "escalated"

            i_n += 1
            iid = f"INT-{i_n:05d}"
            pid = rx["patient_id"]
            doc = {
                "interaction_id": iid,
                "patient_id": pid,
                "prescription_id": rx["prescription_id"],
                "session_key": session_key_for(pid),
                "outcome": outcome,
                "raw_patient_text": rng.choice(RAW_TEXT[outcome]),
                "non_adherence_reason": (rng.choice(NON_ADHERENCE_REASONS)
                                         if outcome == "non_adherent" else None),
                "notes": None,
                "channel": "telegram",
                # Populated from `openclaw cron runs --json` once cron is live;
                # backfilled here so the analytics view is not empty.
                "duration_seconds": rng.randint(45, 240),
                "created_at": dt.datetime.combine(
                    when, dt.time(hour=rng.randint(9, 17),
                                  minute=rng.randint(0, 59)),
                    tzinfo=dt.timezone.utc),
            }

            if outcome == "escalated":
                flag = rng.choices(RED_FLAG_TYPES, weights=[50, 25, 20, 5])[0]
                reason, severity = ESCALATION_TEXT[flag]
                e_n += 1
                # Older escalations are mostly resolved; recent ones still open.
                age = (today - when).days
                status = ("resolved" if age > 14 else
                          "acknowledged" if age > 3 else "open")
                esc = {
                    "escalation_id": f"ESC-{e_n:04d}",
                    "interaction_id": iid,
                    "patient_id": pid,
                    "prescription_id": rx["prescription_id"],
                    "patient_name": f"{by_patient[pid]['first_name']} "
                                    f"{by_patient[pid]['last_name']}",
                    "drug_name": rx["drug_name"],
                    "red_flag_type": flag,
                    "severity": severity,
                    "reason": reason,
                    "raw_patient_text": doc["raw_patient_text"],
                    "status": status,
                    "notified": True,
                    "assigned_to": by_patient[pid]["pharmacist"],
                    "created_at": doc["created_at"],
                    "resolved_at": (doc["created_at"] + dt.timedelta(
                        hours=rng.randint(1, 48)) if status == "resolved" else None),
                }
                escalations.append(esc)
                doc["escalation_id"] = esc["escalation_id"]

            interactions.append(doc)

    interactions.sort(key=lambda d: d["created_at"])
    escalations.sort(key=lambda d: d["created_at"])
    return interactions, escalations


def build_staff() -> list[dict]:
    return [
        {"staff_id": "ST-0001", "name": "PharmD Chen", "role": "on_call_pharmacist",
         "telegram_chat_id": CHAT_ID_PLACEHOLDER.format("ONCALL"),
         "active": True, "shift": "day"},
        {"staff_id": "ST-0002", "name": "PharmD Alvarez", "role": "pharmacist",
         "telegram_chat_id": None, "active": True, "shift": "night"},
        {"staff_id": "ST-0003", "name": "PharmD Osei", "role": "pharmacist",
         "telegram_chat_id": None, "active": False, "shift": "day"},
    ]


def build_memories(today: dt.date) -> list[dict]:
    """Non-demo history that proves the collection and ranking are live.

    Demo patients PT-0001..3 intentionally start without memories so the
    before/after stage demonstration is visible.
    """
    created = midnight(today - dt.timedelta(days=12))
    return [{
        "memory_id": "MEM-00001",
        "patient_id": "PT-0004",
        "memory_type": "contact_preference",
        "fact": "morning calls before 10am are difficult",
        "normalized_fact": "morning calls before 10am are difficult",
        "priority": 4,
        "source_interaction_id": None,
        "created_at": created,
        "updated_at": created,
        "expires_at": midnight(today + dt.timedelta(days=168)),
    }]


def build_all(seed: int = 7, today: dt.date | None = None) -> dict[str, list[dict]]:
    """Deterministic given the same seed - identical data on every machine."""
    today = today or dt.date.today()
    rng = random.Random(seed)
    patients = build_patients()
    prescriptions = build_prescriptions(patients, rng, today)
    interactions, escalations = build_history(patients, prescriptions, rng, today)
    return {PATIENTS: patients, PRESCRIPTIONS: prescriptions,
            INTERACTIONS: interactions, ESCALATIONS: escalations,
            MEMORIES: build_memories(today), STAFF: build_staff()}


# =====================================================================
# READ HELPERS - the exact shapes the tools and dashboard need
# =====================================================================

def get_patient_context(patient_id: str, limit: int = 6) -> dict:
    """Rank durable facts, active escalations, and recent outcomes in Mongo."""
    current = now()
    pipeline = [
        {"$match": {"patient_id": patient_id}},
        {"$limit": 1},
        {"$lookup": {"from": MEMORIES, "let": {"pid": "$patient_id"},
         "pipeline": [
             {"$match": {"$expr": {"$and": [
                 {"$eq": ["$patient_id", "$$pid"]},
                 {"$gt": ["$expires_at", current]},
             ]}}},
             {"$project": {"_id": 0, "kind": {"$literal": "memory"},
                            "memory_id": 1, "memory_type": 1, "fact": 1,
                            "priority": 1, "created_at": 1,
                            "score": {"$add": [100, {"$multiply": [
                                "$priority", 10]}]}}},
         ], "as": "memory_items"}},
        {"$lookup": {"from": INTERACTIONS, "let": {"pid": "$patient_id"},
         "pipeline": [
             {"$match": {"$expr": {"$eq": ["$patient_id", "$$pid"]}}},
             {"$sort": {"created_at": -1}}, {"$limit": 3},
             {"$project": {"_id": 0, "kind": {"$literal": "interaction"},
                            "interaction_id": 1, "outcome": 1,
                            "non_adherence_reason": 1, "created_at": 1,
                            "score": {"$switch": {"branches": [
                                {"case": {"$eq": ["$outcome", "escalated"]},
                                 "then": 95},
                                {"case": {"$eq": ["$outcome", "non_adherent"]},
                                 "then": 70}], "default": 30}}}},
         ], "as": "interaction_items"}},
        {"$lookup": {"from": ESCALATIONS, "let": {"pid": "$patient_id"},
         "pipeline": [
             {"$match": {"$expr": {"$and": [
                 {"$eq": ["$patient_id", "$$pid"]},
                 {"$in": ["$status", ["open", "acknowledged"]]},
             ]}}},
             {"$project": {"_id": 0, "kind": {"$literal": "escalation"},
                            "escalation_id": 1, "severity": 1, "reason": 1,
                            "status": 1, "created_at": 1,
                            "score": {"$switch": {"branches": [
                                {"case": {"$eq": ["$severity", "critical"]},
                                 "then": 200},
                                {"case": {"$eq": ["$severity", "high"]},
                                 "then": 170},
                                {"case": {"$eq": ["$severity", "medium"]},
                                 "then": 140}], "default": 110}}}},
         ], "as": "escalation_items"}},
        {"$project": {"items": {"$concatArrays": [
            "$memory_items", "$escalation_items", "$interaction_items"]}}},
        {"$unwind": {"path": "$items", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"items.score": -1, "items.created_at": -1}},
        {"$limit": limit},
        {"$group": {"_id": None, "items": {"$push": "$items"}}},
    ]
    row = next(iter(get_db()[PATIENTS].aggregate(pipeline)), {"items": []})
    items = [clean(item) for item in row.get("items", []) if item]
    memory = next((item for item in items if item.get("kind") == "memory"), None)
    return {
        "available": True,
        "retrieval_engine": "mongodb_aggregation",
        "joined_collections": [MEMORIES, INTERACTIONS, ESCALATIONS],
        "ranking": "severity_then_priority_then_recency",
        "items": items,
        "behavior_directive": (
            "Acknowledge the most relevant durable fact before asking the "
            "generic adherence question; do not make the patient repeat it."
            if memory else None),
        "suggested_opening": (
            f"Last time you mentioned that {memory['fact']} Has that changed?"
            if memory else None),
    }

def get_patient_meds(patient_id: str) -> dict:
    """The exact payload get_patient_meds returns. Kept here so the seed data
    is verified against the real contract rather than an assumed one."""
    db = get_db()
    p = db[PATIENTS].find_one({"patient_id": patient_id})
    if not p:
        raise LookupError(f"no patient {patient_id}")
    rx = db[PRESCRIPTIONS].find_one(
        {"patient_id": patient_id, "status": "active"},
        sort=[("next_checkin_date", ASCENDING)])
    last = db[INTERACTIONS].find_one({"patient_id": patient_id},
                                     sort=[("created_at", DESCENDING)])
    return {
        "patient": {"id": p["patient_id"], "first_name": p["first_name"],
                    "last_name": p["last_name"], "dob": p["dob"],
                    "status": p["status"]},
        "prescription": ({"id": rx["prescription_id"],
                          "drug_name": rx["drug_name"],
                          "condition": rx["condition"],
                          "dose_instructions": rx["dose_instructions"],
                          "next_checkin_date": as_date(
                              rx["next_checkin_date"]).isoformat()
                          if rx.get("next_checkin_date") else None,
                          "adherence_checkin_cadence_days":
                              rx["adherence_checkin_cadence_days"],
                          "status": rx["status"]} if rx else None),
        "last_interaction_summary": ({"outcome": last["outcome"],
                                      "date": last["created_at"].date().isoformat()}
                                     if last else None),
        "retrieved_context": get_patient_context(patient_id),
    }


def due_now(at: dt.date | None = None) -> list[dict]:
    """What the trigger scripts sweep for: active prescriptions whose
    next_checkin_date has arrived. Hits the `due_sweep` index."""
    at = at or dt.date.today()
    return clean_all(get_db()[PRESCRIPTIONS].find({
        "status": "active",
        "next_checkin_date": {"$lte": midnight(at)},
    }).sort("next_checkin_date", ASCENDING))


def queue_buckets(at: dt.date | None = None) -> dict[str, list[dict]]:
    """What GET /api/queue renders."""
    at = at or dt.date.today()
    db = get_db()
    today_start, today_end = midnight(at), midnight(at + dt.timedelta(days=1))

    overdue = list(db[PRESCRIPTIONS].find(
        {"status": "active", "next_checkin_date": {"$lt": today_start}}))
    due_today = list(db[PRESCRIPTIONS].find(
        {"status": "active",
         "next_checkin_date": {"$gte": today_start, "$lt": today_end}}))
    upcoming = list(db[PRESCRIPTIONS].find(
        {"status": "active", "next_checkin_date": {"$gte": today_end}}))
    paused = list(db[PRESCRIPTIONS].find({"status": "paused"}))
    completed_today = list(db[INTERACTIONS].find(
        {"created_at": {"$gte": today_start}}))

    return {"overdue": clean_all(overdue), "due_today": clean_all(due_today),
            "upcoming": clean_all(upcoming), "paused": clean_all(paused),
            "completed_today": clean_all(completed_today)}


def analytics(at: dt.date | None = None) -> dict[str, Any]:
    """What GET /api/analytics/* renders."""
    at = at or dt.date.today()
    db = get_db()
    since = midnight(at - dt.timedelta(days=HISTORY_DAYS))

    by_outcome = {d["_id"]: d["n"] for d in db[INTERACTIONS].aggregate([
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {"_id": "$outcome", "n": {"$sum": 1}}}])}
    total = sum(by_outcome.values()) or 1

    reasons = {d["_id"]: d["n"] for d in db[INTERACTIONS].aggregate([
        {"$match": {"non_adherence_reason": {"$ne": None}}},
        {"$group": {"_id": "$non_adherence_reason", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}])}

    weekly = [{"week": d["_id"], "calls": d["n"], "adherent": d["adherent"],
               "adherence_rate": round(d["adherent"] / d["n"] * 100, 1)}
              for d in db[INTERACTIONS].aggregate([
                  {"$match": {"created_at": {"$gte": since}}},
                  {"$group": {
                      "_id": {"$dateToString": {"format": "%Y-W%V",
                                                "date": "$created_at"}},
                      "n": {"$sum": 1},
                      "adherent": {"$sum": {"$cond": [
                          {"$eq": ["$outcome", "adherent"]}, 1, 0]}}}},
                  {"$sort": {"_id": 1}}])]

    esc_by_severity = {d["_id"]: d["n"] for d in db[ESCALATIONS].aggregate([
        {"$group": {"_id": "$severity", "n": {"$sum": 1}}}])}
    esc_open = db[ESCALATIONS].count_documents({"status": "open"})

    return {
        "total_calls": total,
        "by_outcome": by_outcome,
        "adherence_rate": round(by_outcome.get("adherent", 0) / total * 100, 1),
        "escalation_rate": round(by_outcome.get("escalated", 0) / total * 100, 1),
        "non_adherence_reasons": reasons,
        "weekly": weekly,
        "escalations_by_severity": esc_by_severity,
        "escalations_open": esc_open,
    }


# =====================================================================
# SEED
# =====================================================================

def seed(seed_value: int = 7, reset: bool = True) -> dict:
    db = get_db()
    if reset:
        # Drop rather than delete_many: an earlier schema version leaves docs
        # of the wrong shape behind, and building a unique index over those
        # fails with a duplicate-key error on the missing field. Dropping also
        # clears stale indexes from that version.
        for name in set(db.list_collection_names()):
            db[name].drop()
    setup_schema(db)
    data = build_all(seed=seed_value)
    for name, docs in data.items():
        if docs:
            db[name].insert_many([dict(d) for d in docs])
    return counts(db)


def export(path: str, seed_value: int = 7) -> dict:
    """Export the safety fallback only; durable memory stays Mongo-only."""
    data = build_all(seed=seed_value)
    out = {name: [clean(d) for d in docs] for name, docs in data.items()
           if name != MEMORIES}
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    return {k: len(v) for k, v in out.items()}


# =====================================================================
# PREFLIGHT
# =====================================================================

def check() -> tuple[bool, list[str]]:
    if not ping():
        return False, [f"Mongo unreachable: {redact(MONGO_URI)}",
                       f"error: {_err}",
                       "  - is mongod running?  systemctl status mongod",
                       "  - is it listening?    ss -tlnp | grep 27017"]

    db = get_db()
    c = counts(db)
    msgs = [f"connected: {redact(MONGO_URI)}",
            f"database: {MONGO_DB}",
            f"collections: {c}"]
    ok = True

    if c[PATIENTS] == 0:
        return False, msgs + ["EMPTY - run: python rxlocal_db.py seed"]

    # 1. Referential integrity - every FK resolves.
    pids = set(db[PATIENTS].distinct("patient_id"))
    rxids = set(db[PRESCRIPTIONS].distinct("prescription_id"))
    orphan_rx = set(db[PRESCRIPTIONS].distinct("patient_id")) - pids
    orphan_int_p = set(db[INTERACTIONS].distinct("patient_id")) - pids
    orphan_int_r = set(db[INTERACTIONS].distinct("prescription_id")) - rxids
    orphan_esc = set(db[ESCALATIONS].distinct("patient_id")) - pids
    orphan_mem = set(db[MEMORIES].distinct("patient_id")) - pids
    orphans = orphan_rx | orphan_int_p | orphan_int_r | orphan_esc | orphan_mem
    if orphans:
        ok = False
        msgs.append(f"ORPHANED REFERENCES: {sorted(orphans)[:5]}")
    else:
        msgs.append("referential integrity: all patient_id / prescription_id resolve")

    # 2. Vocabularies match what the tools branch on.
    bad_outcome = set(db[INTERACTIONS].distinct("outcome")) - set(OUTCOMES)
    bad_status = set(db[PRESCRIPTIONS].distinct("status")) - set(PRESCRIPTION_STATUS)
    if bad_outcome or bad_status:
        ok = False
        msgs.append(f"UNKNOWN VOCABULARY outcome={bad_outcome} status={bad_status}")
    else:
        msgs.append(f"vocabularies valid: outcomes={sorted(set(db[INTERACTIONS]
                    .distinct('outcome')))}")

    # 3. session_key must match what cron passes, or the agent starts a new
    #    conversation every time instead of resuming one.
    bad_keys = [k for k in db[INTERACTIONS].distinct("session_key")
                if not k.startswith(SESSION_KEY_PREFIX) or k != k.lower()]
    if bad_keys:
        ok = False
        msgs.append(f"BAD SESSION KEYS (must be lowercase, "
                    f"{SESSION_KEY_PREFIX}*): {bad_keys[:3]}")
    else:
        msgs.append(f"session keys match cron format "
                    f"({session_key_for('PT-0001')})")

    # 4. Every escalated interaction has an escalation, and vice versa.
    esc_int = db[INTERACTIONS].count_documents({"outcome": "escalated"})
    esc_rows = db[ESCALATIONS].count_documents({})
    if esc_int != esc_rows:
        ok = False
        msgs.append(f"MISMATCH: {esc_int} escalated interactions vs "
                    f"{esc_rows} escalation docs")
    else:
        msgs.append(f"escalations paired: {esc_rows} interactions <-> docs")

    # 5. Paused prescriptions must have no next_checkin_date, or the sweep
    #    will re-contact a patient a human is already engaged with.
    bad_paused = db[PRESCRIPTIONS].count_documents(
        {"status": "paused", "next_checkin_date": {"$ne": None}})
    if bad_paused:
        ok = False
        msgs.append(f"{bad_paused} paused prescription(s) still have a "
                    f"next_checkin_date - the sweep would re-contact them")
    else:
        msgs.append("paused prescriptions have no next_checkin_date")

    # 6. The due sweep returns something.
    due = due_now()
    msgs.append(f"due now: {len(due)} prescription(s) "
                f"({[d['patient_id'] for d in due][:5]})")
    if not due:
        ok = False
        msgs.append("NOTHING DUE - the demo sweep will find no work")

    # 7. Dashboard buckets are all populated.
    q = queue_buckets()
    msgs.append("queue: " + "  ".join(f"{k}={len(v)}" for k, v in q.items()))
    if not q["overdue"] or not q["due_today"]:
        ok = False
        msgs.append("QUEUE BUCKETS EMPTY - dashboard will look broken")

    # 8. Telegram wiring - the manual pre-demo step.
    missing = list(db[PATIENTS].find(
        {"telegram_chat_id": {"$regex": "^REPLACE_WITH_"}}, {"patient_id": 1}))
    oncall = db[STAFF].find_one({"role": "on_call_pharmacist"})
    oncall_missing = (oncall or {}).get("telegram_chat_id", "").startswith(
        "REPLACE_WITH_")
    if missing or oncall_missing:
        msgs.append(f"TELEGRAM PENDING: {len(missing)} patient(s) "
                    f"{'+ on-call ' if oncall_missing else ''}still placeholder "
                    f"- run: python rxlocal_db.py telegram")

    # 9. Indexes.
    for name in (PATIENTS, PRESCRIPTIONS, INTERACTIONS, MEMORIES):
        idx = set(db[name].index_information())
        msgs.append(f"{name} indexes: {sorted(idx)}")
    if "due_sweep" not in db[PRESCRIPTIONS].index_information():
        ok = False
        msgs.append("MISSING due_sweep index - the sweep will collection-scan")
    if "stale_memory_ttl" not in db[MEMORIES].index_information():
        ok = False
        msgs.append("MISSING stale_memory_ttl index")

    # 10. get_patient_meds returns a complete payload for the demo patients.
    for pid in [f"PT-{i+1:04d}" for i in range(DEMO_PATIENT_COUNT)]:
        try:
            r = get_patient_meds(pid)
            if not r["prescription"]:
                ok = False
                msgs.append(f"{pid} has NO ACTIVE PRESCRIPTION - "
                            f"get_patient_meds returns null")
            else:
                msgs.append(f"{pid}: {r['patient']['first_name']} "
                            f"{r['patient']['last_name']} DOB {r['patient']['dob']} "
                            f"-> {r['prescription']['drug_name']} "
                            f"(next {r['prescription']['next_checkin_date']})")
        except LookupError as e:
            ok = False
            msgs.append(str(e))

    return ok, msgs


# =====================================================================
# SELF-TEST
# =====================================================================

G, R, D, X = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def run_tests() -> int:
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

    sec("CONNECTION")
    ck("Mongo reachable", ping(), redact(MONGO_URI))
    if not ping():
        return 1
    ck("database name parsed from URI", MONGO_DB == "rxlocal", MONGO_DB)

    sec("SESSION KEYS")
    ck("matches the cron --session-key format",
       session_key_for("PT-0001") == "agent:rxlocal:patient:pt-0001",
       session_key_for("PT-0001"))
    ck("lowercased", session_key_for("PT-0001").islower())

    sec("GENERATION (pure, no Mongo)")
    data = build_all()
    ck("16 patients", len(data[PATIENTS]) == 16, str(len(data[PATIENTS])))
    ck("deterministic across runs",
       [d["interaction_id"] for d in build_all()[INTERACTIONS]] ==
       [d["interaction_id"] for d in data[INTERACTIONS]])
    ck("patient ids are PT-000N",
       all(p["patient_id"].startswith("PT-") for p in data[PATIENTS]))
    ck("prescription ids are RX-000N",
       all(r["prescription_id"].startswith("RX-") for r in data[PRESCRIPTIONS]))
    ck("only the demo patients get Telegram placeholders",
       sum(1 for p in data[PATIENTS] if p["telegram_chat_id"]) == DEMO_PATIENT_COUNT)
    ck("history spans ~90 days", len(data[INTERACTIONS]) > 50,
       f"{len(data[INTERACTIONS])} interactions")
    ck("outcomes are only what the tools branch on",
       {d["outcome"] for d in data[INTERACTIONS]} <= set(OUTCOMES))
    ck("non_adherence_reason set iff non_adherent",
       all((d["non_adherence_reason"] is not None)
           == (d["outcome"] == "non_adherent") for d in data[INTERACTIONS]))
    ck("every escalated interaction has an escalation doc",
       sum(1 for d in data[INTERACTIONS] if d["outcome"] == "escalated")
       == len(data[ESCALATIONS]))
    ck("inactive patient's prescription is completed with no next_checkin",
       all(r["next_checkin_date"] is None
           for r in data[PRESCRIPTIONS] if r["status"] == "completed"))

    sec("SEED")
    c = seed()
    ck("all collections populated", all(v > 0 for v in c.values()), str(c))
    db = get_db()
    ck("due_sweep index exists",
       "due_sweep" in db[PRESCRIPTIONS].index_information())
    ck("dates stored as BSON dates",
       isinstance(db[PRESCRIPTIONS].find_one(
           {"next_checkin_date": {"$ne": None}})["next_checkin_date"], dt.datetime))
    ck("reseed is idempotent", seed()[PATIENTS] == 16)

    sec("get_patient_meds CONTRACT")
    r = get_patient_meds("PT-0001")
    ck("returns medication data plus ranked retrieval context",
       set(r) == {"patient", "prescription", "last_interaction_summary",
                  "retrieved_context"})
    ck("patient has id/first_name/last_name/dob/status",
       set(r["patient"]) == {"id", "first_name", "last_name", "dob", "status"})
    ck("prescription has the documented fields",
       {"drug_name", "condition", "dose_instructions", "next_checkin_date",
        "adherence_checkin_cadence_days", "status"} <= set(r["prescription"]))
    ck("last_interaction_summary is {outcome, date}",
       set(r["last_interaction_summary"]) == {"outcome", "date"})
    ck("dob is a plain YYYY-MM-DD string for the verification step",
       len(r["patient"]["dob"]) == 10 and r["patient"]["dob"][4] == "-",
       r["patient"]["dob"])
    ck("the active prescription is the one returned",
       r["prescription"]["status"] == "active")
    ck("retrieval is a Mongo aggregation across three business collections",
       r["retrieved_context"]["retrieval_engine"] == "mongodb_aggregation" and
       set(r["retrieved_context"]["joined_collections"]) ==
       {MEMORIES, INTERACTIONS, ESCALATIONS})
    remembered = get_patient_meds("PT-0004")["retrieved_context"]
    ck("durable memory changes the suggested next opening",
       remembered["suggested_opening"] is not None and
       "before 10am" in remembered["suggested_opening"],
       str(remembered["suggested_opening"]))
    try:
        get_patient_meds("PT-9999")
        ck("unknown patient raises", False)
    except LookupError:
        ck("unknown patient raises", True)

    sec("DUE SWEEP")
    due = due_now()
    ck("sweep finds work", len(due) > 0, f"{len(due)} due")
    ck("sweep returns only active",
       all(d["status"] == "active" for d in due))
    ck("sweep excludes future check-ins",
       all(as_date(d["next_checkin_date"]) <= dt.date.today() for d in due))

    sec("DASHBOARD QUEUE")
    q = queue_buckets()
    for k, v in q.items():
        print(f"  {D}{k:<16} {len(v)}{X}")
    ck("overdue populated", len(q["overdue"]) > 0)
    ck("due_today populated", len(q["due_today"]) > 0)
    ck("upcoming populated", len(q["upcoming"]) > 0)

    sec("ANALYTICS")
    a = analytics()
    print(f"  {D}calls={a['total_calls']}  adherence={a['adherence_rate']}%  "
          f"escalation={a['escalation_rate']}%  open_escalations="
          f"{a['escalations_open']}{X}")
    print(f"  {D}reasons: {a['non_adherence_reasons']}{X}")
    ck("outcome breakdown covers all three", len(a["by_outcome"]) == 3,
       str(a["by_outcome"]))
    ck("weekly trend has multiple points", len(a["weekly"]) >= 8,
       f"{len(a['weekly'])} weeks")
    ck("non-adherence reasons populated", len(a["non_adherence_reasons"]) >= 4)
    ck("escalations by severity populated", len(a["escalations_by_severity"]) >= 2)

    sec("EXPORT (simulation parity)")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        n = export(tf.name)
    with open(tf.name) as f:
        blob = json.load(f)
    ck("JSON fallback deliberately excludes Mongo-only memories",
       set(n) == set(COLLECTIONS) - {MEMORIES} and MEMORIES not in blob,
       str(n))
    ck("exported patient count matches Mongo",
       len(blob[PATIENTS]) == db[PATIENTS].count_documents({}))
    ck("exported json is serialisable and dates are strings",
       isinstance(blob[PRESCRIPTIONS][0]["prescribed_on"], str))
    os.unlink(tf.name)

    sec("PREFLIGHT")
    ok, msgs = check()
    for m in msgs:
        print(f"  {D}{m}{X}")
    ck("preflight passes", ok)

    print(f"\n{G}ALL PASS{X}" if not fails else f"\n{R}{fails} FAILURES{X}")
    return 1 if fails else 0


# =====================================================================
# CLI
# =====================================================================

def cmd_show(pid: str) -> int:
    print(json.dumps(get_patient_meds(pid), indent=2, default=str))
    return 0


def cmd_queue() -> int:
    q = queue_buckets()
    db = get_db()
    names = {p["patient_id"]: f"{p['first_name']} {p['last_name']}"
             for p in db[PATIENTS].find({}, {"patient_id": 1, "first_name": 1,
                                             "last_name": 1})}
    for bucket in ("overdue", "due_today", "upcoming", "paused"):
        rows = q[bucket]
        print(f"\n{bucket.upper()}  ({len(rows)})")
        for r in rows[:12]:
            nxt = (as_date(r["next_checkin_date"]).isoformat()
                   if r.get("next_checkin_date") else "-")
            print(f"  {r['patient_id']}  {names.get(r['patient_id'],''):<20} "
                  f"{r['drug_name']:<22} next {nxt}")
    print(f"\nCOMPLETED TODAY  ({len(q['completed_today'])})")
    return 0


def cmd_analytics() -> int:
    a = analytics()
    print(f"calls (90d):        {a['total_calls']}")
    print(f"adherence rate:     {a['adherence_rate']}%")
    print(f"escalation rate:    {a['escalation_rate']}%")
    print(f"open escalations:   {a['escalations_open']}")
    print(f"\nby outcome:         {a['by_outcome']}")
    print(f"by severity:        {a['escalations_by_severity']}")
    print("\nnon-adherence reasons:")
    for k, v in a["non_adherence_reasons"].items():
        print(f"  {k:<24} {v}")
    print("\nweekly:")
    for w in a["weekly"][-8:]:
        bar = "#" * int(w["adherence_rate"] / 4)
        print(f"  {w['week']}  calls={w['calls']:>3}  "
              f"adherence={w['adherence_rate']:>5}%  {bar}")
    return 0


def cmd_telegram() -> int:
    """List every chat id still needing to be filled in before the demo."""
    db = get_db()
    pending = list(db[PATIENTS].find(
        {"telegram_chat_id": {"$regex": "^REPLACE_WITH_"}}))
    staff = list(db[STAFF].find({"telegram_chat_id": {"$regex": "^REPLACE_WITH_"}}))

    if not pending and not staff:
        print("All Telegram chat ids are set.")
        return 0

    print("Telegram chat ids still to fill in (manual pre-demo step):\n")
    for p in pending:
        print(f"  {p['patient_id']}  {p['first_name']} {p['last_name']:<12} "
              f"{p['telegram_chat_id']}")
    for s in staff:
        print(f"  {s['staff_id']}  {s['name']:<20} ({s['role']}) "
              f"{s['telegram_chat_id']}")

    print("\nSet them with:")
    for p in pending:
        print(f"  python rxlocal_db.py set-chat-id {p['patient_id']} <chat_id>")
    for s in staff:
        print(f"  python rxlocal_db.py set-chat-id {s['staff_id']} <chat_id>")
    print("\nVerify each first:")
    print("  openclaw message send --channel telegram --target <chat_id> "
          "--message 'test'")
    return 0


def cmd_set_chat_id(target_id: str, chat_id: str) -> int:
    db = get_db()
    coll, key = ((PATIENTS, "patient_id") if target_id.startswith("PT-")
                 else (STAFF, "staff_id"))
    res = db[coll].update_one({key: target_id},
                              {"$set": {"telegram_chat_id": chat_id}})
    if res.matched_count == 0:
        print(f"no {coll} row with {key}={target_id}", file=sys.stderr)
        return 1
    print(f"{target_id} -> {chat_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="RxLocal database schema and seeding",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    p_seed = sub.add_parser("seed", help="wipe and seed the database")
    p_seed.add_argument("--seed", type=int, default=7)
    p_seed.add_argument("--keep", action="store_true",
                        help="ensure schema and indexes without touching data")
    sub.add_parser("check", help="verify the database, change nothing")
    p_show = sub.add_parser("show", help="what get_patient_meds returns")
    p_show.add_argument("patient_id", nargs="?", default="PT-0001")
    sub.add_parser("queue", help="what the sweep and dashboard read")
    sub.add_parser("analytics", help="what /api/analytics/* reads")
    sub.add_parser("telegram", help="chat ids still needing to be set")
    p_set = sub.add_parser("set-chat-id", help="set a Telegram chat id")
    p_set.add_argument("target_id")
    p_set.add_argument("chat_id")
    p_exp = sub.add_parser("export", help="write simulated_db.json")
    p_exp.add_argument("path", nargs="?", default="data/simulated_db.json")
    sub.add_parser("test", help="run the self-test")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return 0
    if args.cmd == "test":
        return run_tests()

    if args.cmd == "export":
        os.makedirs(os.path.dirname(args.path) or ".", exist_ok=True)
        n = export(args.path)
        print(f"wrote {args.path}: {n}")
        return 0

    if args.cmd == "check":
        ok, msgs = check()
        for m in msgs:
            print("  " + m)
        print("OK" if ok else "PROBLEMS FOUND")
        return 0 if ok else 1

    if not ping():
        _, msgs = check()
        for m in msgs:
            print(m, file=sys.stderr)
        return 1

    if args.cmd == "show":
        return cmd_show(args.patient_id)
    if args.cmd == "queue":
        return cmd_queue()
    if args.cmd == "analytics":
        return cmd_analytics()
    if args.cmd == "telegram":
        return cmd_telegram()
    if args.cmd == "set-chat-id":
        return cmd_set_chat_id(args.target_id, args.chat_id)

    if args.cmd == "seed":
        print(f"target: {redact(MONGO_URI)}  db={MONGO_DB}")
        if args.keep:
            setup_schema(get_db())
            print("schema + indexes ensured, data untouched")
        else:
            print(f"seeded: {seed(seed_value=args.seed)}")
        ok, msgs = check()
        for m in msgs:
            print("  " + m)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
