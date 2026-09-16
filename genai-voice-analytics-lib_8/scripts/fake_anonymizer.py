#!/usr/bin/env python3
"""A stand-in anonymization service for local testing.

Answers the one endpoint ``voice_analytics.anonymization`` calls, replacing a
few obvious patterns with redaction markers. Enough to prove the
transcribe -> anonymize -> translate ordering end to end without VPN access to
the real auxiliary service.

Standard library only, so it runs with any Python 3.12 and no install.

    python scripts/fake_anonymizer.py &

    LLM_BASE_URL=http://127.0.0.1:8099/v1 \
    LLM_API_KEY=sk-fake \
    MODEL_NAME=gemini-2.5-flash-transcription \
    ANONYMIZATION_URL=http://127.0.0.1:9001/anonymize \
      python -m voice_analytics transcribe \
          --input call.wav --anonymize --target-lang en

Options:
    --port N      listen port (default 9001)
    --fail-times N  return HTTP 503 for the first N requests, to watch retries
    --status N    always return this status, e.g. 400 to check it is not retried
    --empty       return a well-formed response with no anonymized text, to
                  confirm the library refuses to fall back to the original
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

#: Deliberately crude. A real service does proper entity recognition; this only
#: needs to prove that *something* was replaced and the ordering held.
REDACTIONS: list[tuple[str, str]] = [
    (r"\bHDFC Bank\b", "[BANK]"),
    (r"\b\d{1,3}(?:,\d{2,3})+\b", "[AMOUNT]"),        # 52,300
    (r"\b\d{10,}\b", "[ACCOUNT]"),                     # long digit runs
    (r"\b[6-9]\d{9}\b", "[PHONE]"),                    # Indian mobile numbers
    (r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", "[EMAIL]"),
]


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = re.sub(pattern, replacement, text)
    return text


class Handler(BaseHTTPRequestHandler):
    fail_times = 0
    status = 200
    empty = False
    seen = 0

    def _send(self, status: int, payload: dict | str) -> None:
        body = (
            payload if isinstance(payload, str) else json.dumps(payload)
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            request = {}

        Handler.seen += 1
        text = ""
        for message in request.get("messages", []):
            if isinstance(message, dict) and message.get("role") == "user":
                text = str(message.get("content") or "")
                break

        print(
            f"  -> request #{Handler.seen}  {len(text):,} chars in",
            file=sys.stderr,
            flush=True,
        )

        if Handler.status != 200:
            self._send(Handler.status, {"error": f"forced status {Handler.status}"})
            return

        if Handler.seen <= Handler.fail_times:
            self._send(503, {"error": "temporarily unavailable"})
            return

        if Handler.empty:
            # Well-formed, but carrying nothing. The library must refuse to
            # continue rather than fall back to the original text.
            self._send(200, {"messages": []})
            return

        clean = redact(text)
        print(
            f"     redacted {sum(1 for p, _ in REDACTIONS if re.search(p, text))} "
            f"pattern(s), {len(clean):,} chars out",
            file=sys.stderr,
            flush=True,
        )
        self._send(200, {"messages": [{"role": "user", "content": clean}]})

    def log_message(self, *_args) -> None:
        """Silence the default access log; we print our own."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--fail-times", type=int, default=0)
    parser.add_argument("--status", type=int, default=200)
    parser.add_argument("--empty", action="store_true")
    args = parser.parse_args()

    Handler.fail_times = args.fail_times
    Handler.status = args.status
    Handler.empty = args.empty

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(
        f"Fake anonymization service on http://127.0.0.1:{args.port}/anonymize  "
        f"(fail_times={args.fail_times} status={args.status} empty={args.empty})\n"
        "Press Ctrl+C to stop.",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
