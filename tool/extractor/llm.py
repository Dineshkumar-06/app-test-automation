"""Single choke point for all LLM calls in this project.

Per CLAUDE.md, GenAI is only used to read messy requirement-workbook prose
into structured rules and to phrase report text — never to decide pass/fail.
Every call in the codebase must go through call_llm() rather than importing
the OpenAI SDK directly, so swapping models or providers stays a one-line
change here.

Currently routed to NVIDIA's OpenAI-compatible API. NVIDIA_API_KEY must be
set in the environment (a local, gitignored .env is loaded automatically) —
never hardcode it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError

load_dotenv()

# Single place to change the model. meta/llama-3.3-70b-instruct was the
# intended default (strong instruction-follower, good at structured JSON
# output) but currently hangs on NVIDIA's free tier (tested 2026-07-23:
# both the OpenAI client and a raw curl to /v1/chat/completions timed out
# at 90s+ with no response, while this endpoint answers other models in
# ~15s). Falling back to 3.1 so the pipeline is actually runnable; swap
# back to 3.3 by editing this one line once NVIDIA's queue clears.
MODEL = "meta/llama-3.1-70b-instruct"

_client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.environ["NVIDIA_API_KEY"],
    # Free-tier NVIDIA endpoints occasionally hang rather than error (see
    # note above) — bound the wait so one stuck field can't stall a whole
    # batch run indefinitely.
    timeout=60.0,
)

# Transient errors worth retrying with backoff. The free tier drops
# connections and times out intermittently mid-batch; a couple of retries
# recover most of these without a full re-run. JSON-shape errors are NOT
# retried here — they have their own corrective retry in the extractor.
_TRANSIENT_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (3, 8)  # waited before attempts 2 and 3

# --------------------------------------------------------------------------
# Usage logging
# --------------------------------------------------------------------------
# Every call's token usage is appended to this JSONL log so we can audit how
# much each run costs. NVIDIA's free tier does not return a per-call dollar
# amount, so "cost" here is token counts + latency — the actionable proxy.
# If a paid tier / price-per-1M-tokens is ever configured, set USD_PER_1M_*
# below and est_usd populates automatically; left at 0.0 it stays 0.
USAGE_LOG_PATH = Path(os.environ.get("LLM_USAGE_LOG", "logs/llm_usage.jsonl"))
USD_PER_1M_PROMPT = 0.0
USD_PER_1M_COMPLETION = 0.0

_usage_lock = Lock()
_cumulative = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "est_usd": 0.0}


def _record_usage(*, label: str | None, usage, latency_s: float, ok: bool, error: str | None) -> None:
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    total_tokens = getattr(usage, "total_tokens", 0) or 0
    est_usd = (
        prompt_tokens / 1_000_000 * USD_PER_1M_PROMPT
        + completion_tokens / 1_000_000 * USD_PER_1M_COMPLETION
    )
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "label": label,
        "ok": ok,
        "error": error,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "latency_s": round(latency_s, 3),
        "est_usd": round(est_usd, 6),
    }
    with _usage_lock:
        _cumulative["calls"] += 1
        _cumulative["prompt_tokens"] += prompt_tokens
        _cumulative["completion_tokens"] += completion_tokens
        _cumulative["total_tokens"] += total_tokens
        _cumulative["est_usd"] += est_usd
        USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def usage_summary() -> dict:
    """Return cumulative usage for calls made in this process so far."""
    with _usage_lock:
        return dict(_cumulative)


def call_llm(system: str, user: str, *, temperature: float = 0, label: str | None = None) -> str:
    """Send a single system/user prompt pair and return the reply text.

    temperature=0 by default: rule extraction must be repeatable — the same
    Validations prose should produce the same rule on every run.

    `label` (e.g. a field's "sl:label") tags this call in the usage log so a
    run's token spend can be traced field by field. Every call — success or
    failure — is recorded to USAGE_LOG_PATH.
    """
    t0 = time.perf_counter()
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = _client.chat.completions.create(
                model=MODEL,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS[attempt])
                continue
            _record_usage(label=label, usage=None, latency_s=time.perf_counter() - t0, ok=False, error=str(exc))
            raise
        except Exception as exc:  # non-transient — don't retry
            _record_usage(label=label, usage=None, latency_s=time.perf_counter() - t0, ok=False, error=str(exc))
            raise
        _record_usage(
            label=label, usage=response.usage, latency_s=time.perf_counter() - t0, ok=True, error=None
        )
        return response.choices[0].message.content
    raise last_exc  # unreachable, but keeps type-checkers happy
