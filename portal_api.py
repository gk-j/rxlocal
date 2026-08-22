"""MongoDB-backed read API for the local pharmacist portal.

The portal must never silently fall back to seeded frontend fixtures. Patient,
prescription, interaction, escalation, and chat-index data are read from the
local ``rxlocal`` MongoDB database. The Telegram transcript is exported from
the running RxLocal gateway, sanitized of internal scheduler prompts, cached in
MongoDB, and then returned from the MongoDB chat document.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from pymongo import MongoClient


MONGO_URI = os.getenv("RXLOCAL_MONGO_URI", "mongodb://127.0.0.1:27017/rxlocal")
LEGACY_API = os.getenv("RXLOCAL_TELEGRAM_API", "http://127.0.0.1:8787")
PORT = int(os.getenv("RXLOCAL_PORTAL_API_PORT", "8788"))

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, appname="rxlocal-portal")
db_name = MONGO_URI.rsplit("/", 1)[-1].split("?", 1)[0] or "rxlocal"
database = client[db_name]


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key != "_id"}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _usable_chat_id(value: Any) -> str | None:
    if value is None or str(value).startswith("REPLACE_WITH_"):
        return None
    return str(value)


def _telegram_payload(chat_id: str) -> dict[str, Any]:
    url = f"{LEGACY_API}/messages?{urllib.parse.urlencode({'chat_id': chat_id})}"
    with urllib.request.urlopen(url, timeout=35) as response:
        return json.load(response)


def _visible_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    visible: list[dict[str, str]] = []
    for message in messages:
        text = str(message.get("text") or "").strip()
        sender = message.get("from")
        if not text or sender not in ("bot", "customer"):
            continue
        if text.startswith("[SYSTEM: new proactive outreach"):
            continue
        normalized = {
            "from": str(sender),
            "text": text,
            "time": str(message.get("time") or ""),
        }
        if visible and visible[-1]["from"] == normalized["from"] and visible[-1]["text"] == text:
            continue
        visible.append(normalized)
    return visible


def sync_chat(chat_id: str) -> dict[str, Any]:
    existing = database.telegram_chats.find_one({"chat_id": chat_id})
    try:
        payload = _telegram_payload(chat_id)
        messages = _visible_messages(payload.get("messages") or [])
        now = datetime.now(timezone.utc)
        database.telegram_chats.update_one(
            {"chat_id": chat_id},
            {"$set": {
                "chat_id": chat_id,
                "session_key": payload.get("session_key"),
                "messages": messages,
                "message_count": len(messages),
                "synced_at": now,
                "sync_error": None,
            }},
            upsert=True,
        )
    except (OSError, ValueError, urllib.error.URLError) as error:
        if existing is None:
            raise RuntimeError(f"Telegram transcript unavailable: {error}") from error
        database.telegram_chats.update_one(
            {"chat_id": chat_id},
            {"$set": {"sync_error": str(error), "sync_attempted_at": datetime.now(timezone.utc)}},
        )
    return _clean(database.telegram_chats.find_one({"chat_id": chat_id}) or {})


def list_patients() -> dict[str, Any]:
    prescriptions: dict[str, list[dict[str, Any]]] = {}
    for prescription in database.prescriptions.find({"status": "active"}).sort("next_checkin_date", 1):
        prescriptions.setdefault(prescription["patient_id"], []).append(_clean(prescription))

    latest: dict[str, dict[str, Any]] = {}
    for interaction in database.interactions.find().sort("created_at", 1):
        latest[interaction["patient_id"]] = _clean(interaction)

    open_escalations: dict[str, int] = {}
    for escalation in database.escalations.find({"status": {"$in": ["open", "acknowledged"]}}):
        patient_id = escalation["patient_id"]
        open_escalations[patient_id] = open_escalations.get(patient_id, 0) + 1

    rows = []
    for patient in database.patients.find({"status": "active"}).sort("patient_id", 1):
        rows.append({
            "patient_id": patient["patient_id"],
            "first_name": patient["first_name"],
            "last_name": patient["last_name"],
            "status": patient["status"],
            "telegram_chat_id": _usable_chat_id(patient.get("telegram_chat_id")),
            "active_prescriptions": prescriptions.get(patient["patient_id"], []),
            "last_interaction": latest.get(patient["patient_id"]),
            "open_escalation_count": open_escalations.get(patient["patient_id"], 0),
        })
    return {"source": "mongodb", "patients": rows}


def list_chats() -> dict[str, Any]:
    rows = []
    for patient in database.patients.find({"status": "active"}).sort("patient_id", 1):
        chat_id = _usable_chat_id(patient.get("telegram_chat_id"))
        if not chat_id:
            continue
        transcript = sync_chat(chat_id)
        messages = transcript.get("messages") or []
        last_interaction = database.interactions.find_one(
            {"patient_id": patient["patient_id"]}, sort=[("created_at", -1)]
        )
        open_escalation = database.escalations.find_one({
            "patient_id": patient["patient_id"],
            "status": {"$in": ["open", "acknowledged"]},
        })
        last_text = str(messages[-1].get("text") or "").lower() if messages else ""
        verification_failed = messages and messages[-1].get("from") == "bot" and (
            "couldn't verify your identity" in last_text
            or "couldn’t verify your identity" in last_text
            or "limit of verification attempts" in last_text
        )
        if open_escalation:
            status = "Escalated"
        elif verification_failed:
            status = "Verification Failed"
        elif messages:
            status = "Conversation Active" if messages[-1]["from"] == "customer" else "Awaiting Reply"
        else:
            status = "Conversation Complete" if last_interaction else "Not Contacted"
        rows.append({
            "patient_id": patient["patient_id"],
            "chat_id": chat_id,
            "name": f"{patient['first_name']} {patient['last_name']}",
            "last_outcome": (last_interaction or {}).get("outcome"),
            "last_message_at": transcript.get("synced_at") or _clean((last_interaction or {}).get("created_at")),
            "message_count": len(messages),
            "status": status,
        })
    return {"source": "mongodb", "chats": _clean(rows)}


def list_escalations(status: str = "open") -> dict[str, Any]:
    query: dict[str, Any] = {}
    if status != "all":
        query["status"] = {"$in": ["open", "acknowledged"]}
    rows = [_clean(row) for row in database.escalations.find(query).sort("created_at", -1)]
    return {"source": "mongodb", "escalations": rows}


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(_clean(payload), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._reply(HTTPStatus.NO_CONTENT, {})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            client.admin.command("ping")
            if parsed.path == "/patients":
                payload = list_patients()
            elif parsed.path == "/chats":
                payload = list_chats()
            elif parsed.path == "/messages":
                chat_id = (query.get("chat_id") or [""])[0]
                if not chat_id:
                    self._reply(HTTPStatus.BAD_REQUEST, {"error": "chat_id is required"})
                    return
                payload = {"source": "mongodb", **sync_chat(chat_id)}
            elif parsed.path == "/escalations":
                payload = list_escalations((query.get("status") or ["open"])[0])
            elif parsed.path == "/health":
                payload = {
                    "ok": True,
                    "source": "mongodb",
                    "database": db_name,
                    "counts": {
                        name: database[name].count_documents({})
                        for name in ("patients", "prescriptions", "interactions", "escalations", "telegram_chats")
                    },
                    "time": datetime.now(timezone.utc),
                }
            else:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._reply(HTTPStatus.OK, payload)
        except Exception as error:  # keep failures explicit; never serve fixtures
            self._reply(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": str(error), "source": "mongodb", "fallback": False},
            )

    def log_message(self, format: str, *args: Any) -> None:
        print(f"portal-api {self.address_string()} {format % args}")


if __name__ == "__main__":
    client.admin.command("ping")
    print(f"RxLocal portal API reading MongoDB '{db_name}' on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
