#!/usr/bin/env python3
"""A stand-in LLM gateway for local testing.

Answers the one endpoint this library calls, ``POST /v1/chat/completions``,
with a canned transcription response. That makes it possible to exercise the
whole path -- CLI parsing, settings, client construction, retries, JSON repair,
segment formatting, file output and exit codes -- with no VPN and no real
credentials.

Standard library only, so it runs with any Python 3.12 and no install.

    python scripts/fake_gateway.py &
    LLM_BASE_URL=http://127.0.0.1:8099/v1 \
    LLM_API_KEY=sk-fake \
    MODEL_NAME=gemini-2.5-flash-transcription \
      python -m voice_analytics transcribe --input call.wav

Options:
    --port N        listen port (default 8099)
    --fail-times N  return HTTP 503 for the first N requests, to watch the
                    retry-and-backoff logic actually work
    --rate-limit    always return HTTP 429, to check the exit code is 6
    --garbage       return prose instead of JSON, to check JSON repair
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

CANNED_TRANSCRIPT = {
    "primary_language": "Hindi",
    "segments": [
        {
            "timestamp": "00:02",
            "text": "[Agent]: Namaste, main HDFC Bank se bol raha hoon.",
            "translated_text": "Greetings, I am calling from HDFC Bank.",
        },
        {
            "timestamp": "00:09",
            "text": "[Customer]: Haan ji, boliye.",
            "translated_text": "[Customer]: Yes, please go ahead.",
        },
        {
            "timestamp": "00:15",
            "text": "[Agent]: Aapka outstanding amount 52,300 rupees hai.",
            "translated_text": "Your outstanding amount is 52,300 rupees.",
        },
    ],
}


class Handler(BaseHTTPRequestHandler):
    fail_times = 0
    rate_limit = False
    garbage = False
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
        if not self.path.endswith("/chat/completions"):
            self._send(404, {"error": {"message": f"No such path: {self.path}"}})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            request = {}

        Handler.seen += 1
        model = request.get("model", "unknown")
        has_audio = "input_audio" in raw.decode("utf-8", errors="ignore")
        print(
            f"  -> request #{Handler.seen}  model={model}  audio={has_audio}  "
            f"bytes={length:,}",
            file=sys.stderr,
            flush=True,
        )

        if Handler.rate_limit:
            self._send(429, {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}})
            return

        if Handler.seen <= Handler.fail_times:
            self._send(503, {"error": {"message": "Service temporarily unavailable"}})
            return

        content = (
            "Sure! Here is the transcript you asked for:\n"
            if Handler.garbage
            else ""
        ) + json.dumps(CANNED_TRANSCRIPT, ensure_ascii=False)

        self._send(
            200,
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    def do_GET(self) -> None:  # noqa: N802
        # Some clients probe /models on startup.
        self._send(200, {"object": "list", "data": [{"id": "gemini-2.5-flash-transcription"}]})

    def log_message(self, *_args) -> None:
        """Silence the default per-request access log; we print our own."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--fail-times", type=int, default=0)
    parser.add_argument("--rate-limit", action="store_true")
    parser.add_argument("--garbage", action="store_true")
    args = parser.parse_args()

    Handler.fail_times = args.fail_times
    Handler.rate_limit = args.rate_limit
    Handler.garbage = args.garbage

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    print(
        f"Fake LLM gateway on http://127.0.0.1:{args.port}/v1  "
        f"(fail_times={args.fail_times} rate_limit={args.rate_limit} "
        f"garbage={args.garbage})\nPress Ctrl+C to stop.",
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
