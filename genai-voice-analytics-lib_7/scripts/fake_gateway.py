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
import re
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


CANNED_SUMMARIES = {
    "bullet_points": (
        "- Agent contacted the customer about an outstanding balance\n"
        "- Outstanding amount stated as 52,300 rupees\n"
        "- Customer acknowledged and asked the agent to continue"
    ),
    "key_insights": (
        "Customer engaged without objection. Outstanding balance of 52,300 "
        "rupees was disclosed. No payment commitment was recorded."
    ),
    "short_summary": (
        "An HDFC agent called the customer regarding an outstanding balance of "
        "52,300 rupees. The customer acknowledged the call and invited the "
        "agent to continue. No promise to pay was made."
    ),
}


def _system_prompt(request: dict) -> str:
    for message in request.get("messages", []):
        if message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


CANNED_EXTRACTION = {
    "matches": [
        {
            "keyword": "topics",
            "found": True,
            "count": 3,
            "extracted_values": ["outstanding amount", "credit limit", "imperia program"],
            "context": ["...your outstanding amount is 52,300..."],
        },
        {
            "keyword": "agent_name",
            "found": True,
            "count": 1,
            "extracted_values": ["Priya"],
            "context": ["...I am Priya calling from HDFC Bank..."],
        },
    ]
}

CANNED_KPIS = {
    "kpis": [
        {"section": "Compliance", "kpi_code": "recording_consent",
         "kpi_name": "Recording consent captured", "score": 10,
         "raw_score": "Yes", "rationale": "The agent stated the call was recorded.",
         "applicable": True, "observable": True, "attempted": True,
         "evidence": [{"turn_index": 1, "speaker": "Agent",
                       "quote": "This call is being recorded."}]},
        {"section": "Compliance", "kpi_code": "rpc_verified",
         "kpi_name": "Right party contact verified", "score": 0,
         "raw_score": "No", "rationale": "Identity was never confirmed.",
         "negative_impact": "Regulatory breach risk",
         "applicable": True, "observable": True, "attempted": False,
         "evidence": [{"turn_index": 2, "speaker": "Agent",
                       "quote": "I am calling about your outstanding amount."}]},
        {"section": "Empathy and tone", "kpi_code": "polite_greeting",
         "kpi_name": "Polite greeting", "score": 9,
         "rationale": "Warm opening.", "applicable": True,
         "observable": True, "attempted": True,
         "evidence": [{"turn_index": 1, "speaker": "Agent", "quote": "Namaste."}]},
        {"section": "Empathy and tone", "kpi_code": "no_evidence_demo",
         "kpi_name": "Scored without proof", "score": 7,
         "rationale": "Felt about right.", "applicable": True,
         "observable": True, "attempted": True, "evidence": []},
    ],
    "risk_summary": {"compliance_violation": True, "manual_review_required": True,
                     "risk_reason": {"compliance_violation": "Identity not verified"}},
}


#: "KPI Code :", "KPI CODE:", "kpi_code :" -- any spacing, either separator.
_KPI_MARKER = re.compile(r"kpi[ _]*code\s*:", re.IGNORECASE)


def _canned_reply_for(request: dict) -> str:
    """Answer according to what the system prompt asked for.

    Matched on distinctive phrases from each prompt rather than the word
    "summary" -- the bullet-point and key-insight prompts never use it.

    The reply shape has to match the request: prose for text summaries
    (response_format is None) and JSON for transcription and audio summaries,
    or the library's parsing will reject it.
    """
    prompt = _system_prompt(request).lower()

    # Audio summarization: one JSON object holding every requested key.
    if "generate a summary strictly in json" in prompt:
        requested = [name for name in CANNED_SUMMARIES if name in prompt]
        keys = requested or ["short_summary"]
        return json.dumps({k: CANNED_SUMMARIES[k] for k in keys}, ensure_ascii=False)

    # Text summarization: a single prose string in the requested style.
    if "bullet point" in prompt:
        return CANNED_SUMMARIES["bullet_points"]
    if "critical insights" in prompt:
        return CANNED_SUMMARIES["key_insights"]
    if "executive summary" in prompt:
        return CANNED_SUMMARIES["short_summary"]

    # Translation: echo something plausible rather than a JSON blob.
    if "translate the provided banking transcript" in prompt:
        return "\n".join(
            f"[{seg['timestamp']}] {seg['translated_text']}"
            for seg in CANNED_TRANSCRIPT["segments"]
        )

    # Keyword extraction: the prompt names the JSON shape it wants back. The
    # reply carries every keyword the request asked for, so `extract --topics`
    # and `--agent-name` each get an answer.
    if "extract specific business keywords" in prompt:
        wanted = [
            match for match in CANNED_EXTRACTION["matches"]
            if match["keyword"] in prompt
        ]
        return json.dumps({"matches": wanted or CANNED_EXTRACTION["matches"]},
                          ensure_ascii=False)

    # KPI scoring: the prompt carries KPI blocks. Matched loosely on purpose.
    # The real template emits "KPI Code : <code>", but a hand-written prompt for
    # local testing may well say "KPI CODE:<code>" or "kpi_code:". Being strict
    # here means the stub silently answers with a transcript instead, and the
    # error surfaces three steps later as "the model returned no KPI scores" --
    # which points at the model rather than at a missing space.
    if _KPI_MARKER.search(prompt):
        return json.dumps(CANNED_KPIS, ensure_ascii=False)

    # Default: transcription.
    return json.dumps(CANNED_TRANSCRIPT, ensure_ascii=False)


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

        content = _canned_reply_for(request)
        if Handler.garbage:
            content = "Sure! Here is what you asked for:\n" + content

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
