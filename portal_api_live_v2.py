"""MongoDB portal runtime with internal Telegram control messages hidden."""
from __future__ import annotations

from http.server import ThreadingHTTPServer

import portal_api


base_visible_messages = portal_api._visible_messages


def visible_portal_messages(messages):
    return [
        message for message in base_visible_messages(messages)
        if message["text"].strip().upper() not in {"NO_REPLY", "NO_RESPONSE"}
    ]


portal_api._visible_messages = visible_portal_messages


if __name__ == "__main__":
    portal_api.client.admin.command("ping")
    print(
        f"RxLocal portal API reading MongoDB '{portal_api.db_name}' "
        f"on http://127.0.0.1:{portal_api.PORT}"
    )
    ThreadingHTTPServer(("127.0.0.1", portal_api.PORT), portal_api.Handler).serve_forever()
