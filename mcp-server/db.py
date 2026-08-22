"""Data access for the RxLocal MCP server.

Two modes, one interface:

  MONGO       when MONGODB_URI is set and reachable
  SIMULATION  otherwise - reads/writes data/simulated_db.json

The fallback is deliberate: the MCP server must start and answer tool calls
even if Mongo is down, because a dead database during a live demo should
degrade to stale-but-working rather than to a crashed agent. Which mode is
active is reported by `mode()` and stamped on every write, so nobody has to
guess afterwards whether a demo actually hit the database.

Schema and seed data come from rxlocal_db.py. Do not redefine shapes here.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# Collections
PATIENTS = "patients"
PRESCRIPTIONS = "prescriptions"
INTERACTIONS = "interactions"
ESCALATIONS = "escalations"
MEMORIES = "memories"
STAFF = "staff"

MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("RXLOCAL_MONGO_URI", "")
TIMEOUT_MS = int(os.getenv("RXLOCAL_MONGO_TIMEOUT_MS", "5000"))

SIM_PATH = Path(os.getenv(
    "RXLOCAL_SIM_DB",
    Path(__file__).resolve().parent.parent / "data" / "simulated_db.json"))

# Must match the OpenClaw cron --session-key format exactly, or the agent
# starts a fresh conversation each run instead of resuming one.
SESSION_KEY_PREFIX = os.getenv("RXLOCAL_SESSION_PREFIX", "agent:rxlocal:patient:")

_lock = threading.Lock()
_mode: str | None = None
_db = None            # pymongo Database when in mongo mode
_sim: dict[str, list[dict]] | None = None
_connect_error: str | None = None


# ---------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------

def _try_mongo():
    """Return a pymongo Database, or None if unavailable for any reason."""
    global _connect_error
    # load_dotenv() above picks MONGODB_URI up from ../.env, so unsetting the
    # environment variable is NOT enough to reach simulation mode. This flag is
    # the only reliable way to exercise that path.
    if os.getenv("RXLOCAL_FORCE_SIM", "").lower() in ("1", "true", "yes"):
        _connect_error = "forced to simulation by RXLOCAL_FORCE_SIM"
        return None
    if not MONGODB_URI:
        _connect_error = "MONGODB_URI not set"
        return None
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=TIMEOUT_MS,
                             connectTimeoutMS=TIMEOUT_MS, appname="rxlocal-mcp")
        client.admin.command("ping")
        tail = MONGODB_URI.split("://", 1)[-1]
        path = tail.split("/", 1)[1] if "/" in tail else ""
        name = path.split("?", 1)[0] or os.getenv("RXLOCAL_MONGO_DB", "rxlocal")
        return client[name]
    except Exception as e:  # noqa: BLE001 - any failure means fall back
        _connect_error = f"{type(e).__name__}: {e}"
        return None


def _load_sim() -> dict[str, list[dict]]:
    if SIM_PATH.exists():
        with open(SIM_PATH) as f:
            return json.load(f)
    return {c: [] for c in (PATIENTS, PRESCRIPTIONS, INTERACTIONS,
                            ESCALATIONS, STAFF)}


def _save_sim() -> None:
    """Persist simulation writes so state advances across tool calls, the way
    it would in Mongo. Without this a demo silently loses every log_outcome."""
    if _sim is None:
        return
    try:
        SIM_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SIM_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(_sim, f, indent=2, default=str)
        tmp.replace(SIM_PATH)
    except OSError:
        pass  # a read-only workspace must not break the tool call


def _init() -> None:
    global _mode, _db, _sim
    if _mode is not None:
        return
    with _lock:
        if _mode is not None:
            return
        _db = _try_mongo()
        if _db is not None:
            _mode = "mongo"
        else:
            _sim = _load_sim()
            _mode = "simulation"


def mode() -> str:
    _init()
    return _mode or "simulation"


def status() -> dict[str, Any]:
    _init()
    info = {"mode": mode(), "uri_set": bool(MONGODB_URI),
            "sim_path": str(SIM_PATH), "error": _connect_error}
    if mode() == "mongo":
        info["counts"] = {c: _db[c].count_documents({})
                          for c in (PATIENTS, PRESCRIPTIONS, INTERACTIONS,
                                    ESCALATIONS, MEMORIES, STAFF)}
    else:
        info["counts"] = {c: len(_sim.get(c, [])) for c in _sim or {}}
    return info


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------

def session_key_for(patient_id: str) -> str:
    """Derived here, never taken from the model - asked to supply its own
    session key, the model fabricates one."""
    return f"{SESSION_KEY_PREFIX}{patient_id.lower()}"


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso_date(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, str):
        return v[:10]
    return None


def _strip(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    return {k: (v.isoformat() if isinstance(v, dt.datetime) else v)
            for k, v in doc.items() if k != "_id"}


def _next_id(collection: str, field: str, prefix: str, width: int) -> str:
    """Continue the sequence the seeder started (INT-00001, ESC-0001)."""
    _init()
    if _mode == "mongo":
        last = _db[collection].find_one({}, sort=[(field, -1)])
        current = last.get(field) if last else None
    else:
        vals = [d.get(field) for d in _sim.get(collection, []) if d.get(field)]
        current = max(vals) if vals else None
    n = 0
    if current:
        m = re.search(r"(\d+)$", str(current))
        if m:
            n = int(m.group(1))
    return f"{prefix}{n + 1:0{width}d}"


# ---------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------

def find_patient(patient_id: str) -> dict | None:
    _init()
    if _mode == "mongo":
        return _strip(_db[PATIENTS].find_one({"patient_id": patient_id}))
    return next((dict(p) for p in _sim[PATIENTS]
                 if p["patient_id"] == patient_id), None)


def find_prescription(prescription_id: str) -> dict | None:
    _init()
    if _mode == "mongo":
        return _strip(_db[PRESCRIPTIONS].find_one(
            {"prescription_id": prescription_id}))
    return next((dict(r) for r in _sim[PRESCRIPTIONS]
                 if r["prescription_id"] == prescription_id), None)


def active_prescription(patient_id: str) -> dict | None:
    """The prescription this check-in is about.

    A patient may hold more than one. get_patient_meds takes only a
    patient_id, so we pick the one due soonest and return its id in the
    payload - the agent then passes that id back to log_outcome and
    schedule_followup, which are both prescription-scoped.
    """
    _init()
    if _mode == "mongo":
        return _strip(_db[PRESCRIPTIONS].find_one(
            {"patient_id": patient_id, "status": "active"},
            sort=[("next_checkin_date", 1)]))
    rows = [dict(r) for r in _sim[PRESCRIPTIONS]
            if r["patient_id"] == patient_id and r["status"] == "active"]
    rows.sort(key=lambda r: r.get("next_checkin_date") or "9999-12-31")
    return rows[0] if rows else None


def active_prescriptions(patient_id: str) -> list[dict]:
    _init()
    if _mode == "mongo":
        return [_strip(r) for r in _db[PRESCRIPTIONS].find(
            {"patient_id": patient_id, "status": "active"})]
    return [dict(r) for r in _sim[PRESCRIPTIONS]
            if r["patient_id"] == patient_id and r["status"] == "active"]


def last_interaction(patient_id: str) -> dict | None:
    _init()
    if _mode == "mongo":
        return _strip(_db[INTERACTIONS].find_one(
            {"patient_id": patient_id}, sort=[("created_at", -1)]))
    rows = [dict(d) for d in _sim[INTERACTIONS] if d["patient_id"] == patient_id]
    rows.sort(key=lambda d: str(d.get("created_at", "")), reverse=True)
    return rows[0] if rows else None


def open_escalation(patient_id: str,
                    prescription_id: str | None = None) -> dict | None:
    """Highest-severity unresolved item awaiting pharmacist review."""
    _init()
    if _mode == "mongo":
        query: dict[str, Any] = {
            "patient_id": patient_id,
            "status": {"$in": ["open", "acknowledged"]},
        }
        if prescription_id:
            query["prescription_id"] = prescription_id
        rows = [_strip(row) for row in _db[ESCALATIONS].find(query)]
    else:
        rows = [dict(row) for row in _sim.get(ESCALATIONS, [])
                if row.get("patient_id") == patient_id
                and row.get("status") in ("open", "acknowledged")
                and (not prescription_id
                     or row.get("prescription_id") == prescription_id)]
    severity = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    rows.sort(key=lambda row: (severity.get(row.get("severity"), 0),
                               str(row.get("created_at", ""))), reverse=True)
    return rows[0] if rows else None


def patient_context(patient_id: str, limit: int = 6) -> dict:
    """Build ranked, cross-collection context for the next cold agent run.

    This capability is intentionally Mongo-only. The JSON fallback keeps the
    core safety tools available during an outage, but it cannot impersonate
    the aggregation that makes durable memory useful.
    """
    _init()
    if _mode != "mongo":
        return {
            "available": False,
            "retrieval_engine": "unavailable_in_simulation",
            "items": [],
            "behavior_directive": None,
            "suggested_opening": None,
        }

    current = now()
    pipeline = [
        {"$match": {"patient_id": patient_id}},
        {"$limit": 1},
        {"$lookup": {
            "from": MEMORIES,
            "let": {"pid": "$patient_id"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$patient_id", "$$pid"]},
                    {"$or": [
                        {"$eq": [{"$type": "$expires_at"}, "missing"]},
                        {"$eq": ["$expires_at", None]},
                        {"$gt": ["$expires_at", current]},
                    ]},
                ]}}},
                {"$project": {
                    "_id": 0, "kind": {"$literal": "memory"},
                    "memory_id": 1, "memory_type": 1, "fact": 1,
                    "priority": 1, "created_at": 1,
                    "score": {"$add": [100, {"$multiply": ["$priority", 10]}]},
                }},
            ],
            "as": "memory_items",
        }},
        {"$lookup": {
            "from": INTERACTIONS,
            "let": {"pid": "$patient_id"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$patient_id", "$$pid"]}}},
                {"$sort": {"created_at": -1}},
                {"$limit": 3},
                {"$project": {
                    "_id": 0, "kind": {"$literal": "interaction"},
                    "interaction_id": 1, "outcome": 1,
                    "non_adherence_reason": 1, "created_at": 1,
                    "score": {"$switch": {"branches": [
                        {"case": {"$eq": ["$outcome", "escalated"]}, "then": 95},
                        {"case": {"$eq": ["$outcome", "non_adherent"]}, "then": 70},
                    ], "default": 30}},
                }},
            ],
            "as": "interaction_items",
        }},
        {"$lookup": {
            "from": ESCALATIONS,
            "let": {"pid": "$patient_id"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$patient_id", "$$pid"]},
                    {"$in": ["$status", ["open", "acknowledged"]]},
                ]}}},
                {"$project": {
                    "_id": 0, "kind": {"$literal": "escalation"},
                    "escalation_id": 1, "severity": 1, "reason": 1,
                    "status": 1, "created_at": 1,
                    "score": {"$switch": {"branches": [
                        {"case": {"$eq": ["$severity", "critical"]}, "then": 200},
                        {"case": {"$eq": ["$severity", "high"]}, "then": 170},
                        {"case": {"$eq": ["$severity", "medium"]}, "then": 140},
                    ], "default": 110}},
                }},
            ],
            "as": "escalation_items",
        }},
        {"$project": {"items": {"$concatArrays": [
            "$memory_items", "$escalation_items", "$interaction_items",
        ]}}},
        {"$unwind": {"path": "$items", "preserveNullAndEmptyArrays": True}},
        {"$sort": {"items.score": -1, "items.created_at": -1}},
        {"$limit": limit},
        {"$group": {"_id": None, "items": {"$push": "$items"}}},
    ]
    row = next(iter(_db[PATIENTS].aggregate(pipeline)), {"items": []})
    items = [_strip(item) for item in row.get("items", []) if item]
    top_escalation = next((item for item in items
                           if item.get("kind") == "escalation"), None)
    top_memory = next((item for item in items if item.get("kind") == "memory"), None)
    if top_escalation:
        directive = (
            "Human review is active. Do not continue an automated adherence "
            "assessment or give clinical guidance; tell the patient a "
            "pharmacist is reviewing the concern.")
        opening = "A pharmacist is reviewing the concern from your last check-in."
    elif top_memory:
        directive = (
            "Acknowledge the most relevant durable fact before asking the "
            "generic adherence question; do not make the patient repeat it.")
        opening = (f"Last time you mentioned that "
                   f"{top_memory['fact'].rstrip('.').rstrip()}. Has that changed?")
    else:
        directive = opening = None
    return {
        "available": True,
        "retrieval_engine": "mongodb_aggregation",
        "joined_collections": [MEMORIES, INTERACTIONS, ESCALATIONS],
        "ranking": "severity_then_priority_then_recency",
        "items": items,
        "human_review": ({
            "required": True,
            "escalation_id": top_escalation.get("escalation_id"),
            "status": top_escalation.get("status"),
            "severity": top_escalation.get("severity"),
        } if top_escalation else {"required": False}),
        "behavior_directive": directive,
        "suggested_opening": opening,
    }


def oncall_chat_id() -> tuple[str | None, str]:
    """(chat_id, pharmacist_name). chat_id is None when unset or still a
    REPLACE_WITH_* placeholder, so the caller can say so instead of shelling
    out to Telegram with a bogus target."""
    _init()
    if _mode == "mongo":
        s = _db[STAFF].find_one({"role": "on_call_pharmacist", "active": True})
    else:
        s = next((x for x in _sim.get(STAFF, [])
                  if x.get("role") == "on_call_pharmacist" and x.get("active")), None)
    if not s:
        return None, "on-call pharmacist"
    chat = s.get("telegram_chat_id")
    if not chat or str(chat).startswith("REPLACE_WITH_"):
        return None, s.get("name", "on-call pharmacist")
    return str(chat), s.get("name", "on-call pharmacist")


def patient_chat_id(patient_id: str) -> str | None:
    p = find_patient(patient_id)
    chat = (p or {}).get("telegram_chat_id")
    if not chat or str(chat).startswith("REPLACE_WITH_"):
        return None
    return str(chat)


# ---------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------

def insert_interaction(doc: dict) -> str:
    _init()
    doc = dict(doc)
    doc.setdefault("interaction_id", _next_id(INTERACTIONS, "interaction_id",
                                              "INT-", 5))
    doc.setdefault("created_at", now())
    doc["source_mode"] = _mode          # so nobody has to guess afterwards
    if _mode == "mongo":
        _db[INTERACTIONS].insert_one(dict(doc))
    else:
        _sim[INTERACTIONS].append(_strip(doc))
        _save_sim()
    return doc["interaction_id"]


def upsert_memory(patient_id: str, memory_type: str, fact: str,
                  priority: int = 3, ttl_days: int = 180,
                  source_interaction_id: str | None = None) -> dict:
    """Persist one durable patient fact. Mongo is required by design."""
    _init()
    if _mode != "mongo":
        raise RuntimeError(
            "durable patient memory requires MongoDB; JSON simulation cannot write it")
    created = now()
    expires = created + dt.timedelta(days=ttl_days)
    key = {"patient_id": patient_id, "memory_type": memory_type,
           "normalized_fact": " ".join(fact.lower().split())}
    patch = {
        "patient_id": patient_id,
        "memory_type": memory_type,
        "fact": fact.strip(),
        "priority": priority,
        "source_interaction_id": source_interaction_id,
        "updated_at": created,
        "expires_at": expires,
    }
    result = _db[MEMORIES].find_one_and_update(
        key,
        {"$set": patch, "$setOnInsert": {
            "memory_id": _next_id(MEMORIES, "memory_id", "MEM-", 5),
            "created_at": created,
        }},
        upsert=True,
        return_document=True,
    )
    return _strip(result) or {}


def insert_escalation(doc: dict) -> str:
    _init()
    doc = dict(doc)
    doc.setdefault("escalation_id", _next_id(ESCALATIONS, "escalation_id",
                                             "ESC-", 4))
    doc.setdefault("created_at", now())
    doc.setdefault("status", "open")
    doc["source_mode"] = _mode
    if _mode == "mongo":
        _db[ESCALATIONS].insert_one(dict(doc))
    else:
        _sim[ESCALATIONS].append(_strip(doc))
        _save_sim()
    return doc["escalation_id"]


def mark_escalation_notified(escalation_id: str, notified: bool,
                             detail: str = "") -> None:
    _init()
    patch = {"notified": notified, "notify_detail": detail}
    if _mode == "mongo":
        _db[ESCALATIONS].update_one({"escalation_id": escalation_id},
                                    {"$set": patch})
    else:
        for e in _sim[ESCALATIONS]:
            if e.get("escalation_id") == escalation_id:
                e.update(patch)
        _save_sim()


def set_schedule(prescription_id: str, next_checkin_date: dt.date | None,
                 status: str) -> dict:
    """Write back what schedule_followup decided."""
    _init()
    value = (dt.datetime.combine(next_checkin_date, dt.time.min,
                                 tzinfo=dt.timezone.utc)
             if next_checkin_date else None)
    if _mode == "mongo":
        _db[PRESCRIPTIONS].update_one(
            {"prescription_id": prescription_id},
            {"$set": {"next_checkin_date": value, "status": status,
                      "schedule_updated_at": now()}})
    else:
        for r in _sim[PRESCRIPTIONS]:
            if r["prescription_id"] == prescription_id:
                r["next_checkin_date"] = (next_checkin_date.isoformat()
                                          if next_checkin_date else None)
                r["status"] = status
                r["schedule_updated_at"] = now().isoformat()
        _save_sim()
    return {"prescription_id": prescription_id,
            "next_checkin_date": (next_checkin_date.isoformat()
                                  if next_checkin_date else None),
            "status": status}


# ---------------------------------------------------------------------
# Payload shaping - the exact get_patient_meds contract
# ---------------------------------------------------------------------

def patient_meds_payload(patient_id: str) -> dict:
    p = find_patient(patient_id)
    if not p:
        raise LookupError(f"no patient with id {patient_id}")

    rx = active_prescription(patient_id)
    last = last_interaction(patient_id)
    others = [r["prescription_id"] for r in active_prescriptions(patient_id)
              if rx and r["prescription_id"] != rx["prescription_id"]]

    payload = {
        "patient": {
            "id": p["patient_id"],
            "first_name": p["first_name"],
            "last_name": p["last_name"],
            "dob": p["dob"],
            "status": p["status"],
        },
        "prescription": ({
            "id": rx["prescription_id"],
            "drug_name": rx["drug_name"],
            "condition": rx["condition"],
            "dose_instructions": rx["dose_instructions"],
            "next_checkin_date": _iso_date(rx.get("next_checkin_date")),
            "adherence_checkin_cadence_days":
                rx["adherence_checkin_cadence_days"],
            "status": rx["status"],
        } if rx else None),
        "last_interaction_summary": ({
            "outcome": last["outcome"],
            "date": _iso_date(last.get("created_at")),
        } if last else None),
        "retrieved_context": patient_context(patient_id),
    }
    if others:
        # Surfaced rather than hidden: the agent should not silently discuss
        # one drug while the patient is on two.
        payload["other_active_prescription_ids"] = others
    return payload
