#!/usr/bin/env python3
"""migratectl: the new server's half of a migration.

The old server starts it over SSH (`sudo -n migratectl`) and talks to it one line at a time: a JSON object in,
a JSON object out. Standard output carries nothing else, so anything a person should read goes to standard error.
mailctl and domainctl are only ever run as commands, never imported, so either may be missing.
"""

import json
import shutil
import socket
import sys

PROTOCOL_VERSION = 1
TOOLS = ("mailctl", "domainctl")


def hello(request):
    return {
        "ok": True,
        "version": PROTOCOL_VERSION,
        "server": socket.gethostname(),
        "tools": {name: shutil.which(name) is not None for name in TOOLS},
    }


HANDLERS = {"hello": hello}


def answer(request):
    """One request in, one answer out. It never raises: a bad request must not end the connection."""
    try:
        if not isinstance(request, dict):
            return {"ok": False, "error": "A request must be a JSON object, like {\"request\": \"hello\"}."}
        name = request.get("request")
        handler = HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            return {"ok": False, "error": f"I don't know the request {name!r}. Ask for \"hello\" to see what I can do."}
        return handler(request)
    except Exception as error:  # noqa: BLE001 - whatever went wrong, the old server needs an answer
        return {"ok": False, "error": f"That request failed: {error}"}


def main():
    # Bytes, so a line that is not UTF-8 is answered with an error instead of ending the process.
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # ValueError covers bad JSON and bad UTF-8; deep nesting recurses
            reply = {"ok": False, "error": "That line is not valid JSON. Send one JSON object per line."}
        else:
            reply = answer(request)
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
