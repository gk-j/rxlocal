#!/usr/bin/env python3
"""Fill in the Telegram chat ids that the escalation path needs.

Everything else is built; the alert cannot send because the on-call
pharmacist's chat id in Mongo is still a REPLACE_WITH_* placeholder. This is
the only step that needs a human, because @BotFather cannot be automated.

    # 1. In Telegram, message @BotFather -> /newbot -> copy the token
    # 2. Message your new bot once from each account that will take part
    # 3. Read the chat ids straight off the API:
    python scripts/set_chat_ids.py --discover <BOT_TOKEN>

    # 4. Write them in:
    python scripts/set_chat_ids.py --oncall 123456789
    python scripts/set_chat_ids.py --patient PT-0001 --chat-id 987654321

    python scripts/set_chat_ids.py --show      # what is still missing
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "mcp-server"))

import db  # noqa: E402

PLACEHOLDER = "REPLACE_WITH"


def _require_mongo() -> None:
    db.status()
    if db.mode() != "mongo":
        sys.exit("MongoDB is not reachable. Start it, or set MONGODB_URI.")


def discover(token: str) -> int:
    """Ask Telegram who has messaged the bot. No local state needed."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            payload = json.load(r)
    except Exception as e:
        sys.exit(f"could not reach Telegram: {type(e).__name__}: {e}")
    if not payload.get("ok"):
        sys.exit(f"Telegram rejected the token: {payload}")

    seen: dict[str, str] = {}
    for upd in payload.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is None:
            continue
        who = " ".join(filter(None, [chat.get("first_name"),
                                     chat.get("last_name")])) \
              or chat.get("title") or chat.get("username") or "?"
        seen[str(chat["id"])] = who

    if not seen:
        print("No chats yet. Each person must send the bot one message first,")
        print("then re-run this. Telegram only reveals a chat id after contact.")
        return 1
    print("chat id            who")
    for cid, who in seen.items():
        print(f"  {cid:<18} {who}")
    return 0


def show() -> int:
    _require_mongo()
    c = db._db
    print("on-call pharmacist:")
    for s in c.staff.find({"role": "on_call_pharmacist"}):
        cid = s.get("telegram_chat_id")
        ok = cid and PLACEHOLDER not in str(cid)
        print(f"  {'OK ' if ok else 'MISSING'}  {s.get('name')}  {cid!r}")

    missing = list(c.patients.find(
        {"$or": [{"telegram_chat_id": None},
                 {"telegram_chat_id": {"$regex": PLACEHOLDER}}]},
        {"patient_id": 1, "first_name": 1, "last_name": 1,
         "telegram_chat_id": 1}))
    print(f"\npatients without a usable chat id: {len(missing)}")
    for p in missing[:10]:
        print(f"  {p['patient_id']}  {p['first_name']} {p['last_name']}")
    print("\nOnly the patients you actually demo need one.")
    return 0


def set_oncall(chat_id: str) -> int:
    _require_mongo()
    res = db._db.staff.update_one({"role": "on_call_pharmacist"},
                                  {"$set": {"telegram_chat_id": chat_id}})
    if not res.matched_count:
        sys.exit("no staff row with role on_call_pharmacist")
    print(f"on-call pharmacist chat id set to {chat_id}")
    print("verify with:  openclaw message send --channel telegram "
          f"--target {chat_id} --message test")
    return 0


def set_patient(patient_id: str, chat_id: str) -> int:
    _require_mongo()
    res = db._db.patients.update_one({"patient_id": patient_id},
                                     {"$set": {"telegram_chat_id": chat_id}})
    if not res.matched_count:
        sys.exit(f"no patient {patient_id}")
    print(f"{patient_id} chat id set to {chat_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Wire Telegram chat ids into the RxLocal database.")
    ap.add_argument("--discover", metavar="BOT_TOKEN",
                    help="list chat ids of everyone who messaged the bot")
    ap.add_argument("--show", action="store_true",
                    help="show which chat ids are still missing")
    ap.add_argument("--oncall", metavar="CHAT_ID",
                    help="set the on-call pharmacist's chat id")
    ap.add_argument("--patient", metavar="PT-000X")
    ap.add_argument("--chat-id", metavar="CHAT_ID")
    a = ap.parse_args()

    if a.discover:
        return discover(a.discover)
    if a.oncall:
        return set_oncall(a.oncall)
    if a.patient:
        if not a.chat_id:
            sys.exit("--patient needs --chat-id")
        return set_patient(a.patient, a.chat_id)
    return show()


if __name__ == "__main__":
    raise SystemExit(main())
