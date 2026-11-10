"""gemini-web2api: Gemini Web to OpenAI API proxy."""
import sys

__version__ = "1.1.1"

# Dynamically register submodules into sys.modules pointing to this module so
# external callers and tests doing `from gemini_web2api.server import ...` or
# `mock.patch("gemini_web2api.server.generate")` keep working even though the
# whole implementation now lives in this single flat module.
_self = sys.modules[__name__]
sys.modules["gemini_web2api"] = _self
_submodules = [
    "config",
    "models",
    "multimodal",
    "gemini",
    "tools",
    "flow_patch",
    "server",
    "__main__",
]
for _sub in _submodules:
    sys.modules[f"gemini_web2api.{_sub}"] = _self
    if _sub != "__main__":
        setattr(_self, _sub, _self)

# ==============================================================================
# Section: config.py
# ==============================================================================

"""Configuration management."""
import json
import os
import threading

DEFAULT_CONFIG = {
    "port": 8081,
    # Bind to loopback by default: without API keys, anyone on the same network
    # could otherwise use your Google session. Set to "0.0.0.0" behind a
    # container / reverse proxy that provides its own access control.
    "host": "127.0.0.1",
    "tool_stream": False,
    # When tool_stream is on, also forward each streamed delta of a buffered
    # tool turn to the client (SSE ping / heartbeat) so agent clients see
    # per-part progress instead of 20-60 s of silence. Set false to keep the
    # old fully-buffered behaviour.
    "tool_delta_forward": True,
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    # Optional process-wide throttle in front of upstream model requests.
    # upstream_min_interval_sec spaces the *starts* of two generation calls
    # (retries included); 0 disables spacing. upstream_max_concurrency caps
    # simultaneous in-flight generation calls; 0 means unlimited. Both are
    # read at call time, so a config reload takes effect without a restart.
    "upstream_min_interval_sec": 0.0,
    "upstream_max_concurrency": 0,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    # Refresh gemini_bl from gemini.google.com on startup. Set to false to pin
    # the value above -- otherwise startup overwrites whatever you configured.
    "auto_update_bl": True,
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    # When False (default) unknown model names silently fall back to
    # default_model. When True they return 404 model_not_found.
    "strict_models": False,
    "log_requests": True,
    # Optional file sink for the same lines `log()` prints on stderr. Relative
    # paths resolve against the server's working directory.
    "log_file": None,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    # Per-model upstream tickets (X-Goog-Ext-525001261-Jspb). The browser mints
    # one per model family and the upstream routes BY TICKET: without it the
    # f.req [79]/[80] fields are ignored and the account default model answers
    # (observed: 3.1 Pro for every requested model). Tickets embed their
    # (family, variant) in plaintext and expire, so refresh a key by copying
    # the header value from a fresh browser StreamGenerate request (DevTools ->
    # Copy as cURL) for the matching family ("flash", "pro", "lite", ...).
    # gemini.check_routing logs a mismatch warning when one stops working.
    "model_tickets": {},
    # CORS origins allowed to read the response. Empty list means no CORS
    # headers are emitted at all, which is the safe default when api_keys is
    # also empty -- a malicious page could otherwise fetch the local server.
    # Set to ["*"] explicitly when you have api_keys configured and want to
    # allow any browser origin.
    "cors_origins": [],
    # Hard cap on a single request body. A large base64 image can otherwise
    # eat unbounded RAM while the server reads it.
    "max_body_bytes": 32 * 1024 * 1024,
    "temporary_chats": False,
    # Retry once when a response with tools offered looks like the model talked
    # about acting instead of acting (or came back as the upstream's canned
    # failure sentence). One extra upstream call at most, and off entirely if
    # the heuristic proves noisy.
    "tool_retry_on_miss": True,
    # Extra upstream attempts (beyond the first) when a turn is expected to end
    # in a tool call: tool_choice "required", or tools offered with
    # tool_retry_on_miss above. 0 restores the old single call, 2 or 3 helps a
    # model that keeps answering in prose instead of emitting the block -- each
    # extra attempt only ever costs a call that was already failing, never one
    # that succeeded.
    "tool_retry_attempts": 1,
    # When tool_choice is "required" and the model keeps re-emitting a tool
    # call whose result is already in the conversation (a duplicate loop),
    # True fails the request closed (GeminiError -> error status) instead of
    # silently returning prose: "required" is a contract, prose ends an agent
    # turn without a call. Set False for the old soft behaviour (a loop is
    # worse than one prose turn -- fall back to the plain-text answer).
    "required_tool_fail_on_duplicate": True,
    # Enable automatic Plan & TODO progress tracking and live todowrite
    # tool_call synchronization for agent clients (e.g. OpenCode).
    "flow16_plan_todo": True,
    "flow17_plan_todo_act": True,
    # Rewrite the bare word "workspace" to "project" in the prompt sent
    # upstream. The word routes a request to Gemini's Workspace extension,
    # which an anonymous / no-Gmail session answers with "I can't use
    # Workspace because required Gmail settings are off".
    "defuse_extension_words": True,
    # Upper bound for the assembled prompt (bytes) sent upstream. Agent
    # sessions (opencode/cursor) send tens of KB per file read; at 60000 a
    # single read of a 1000-line file filled the budget, the joiner kept only
    # that message's tail (dropping its "[Tool result ...]" label and the
    # pending question), and the model answered from memory. Live probes
    # against the real upstream accepted prompts well past 500 KB, so the
    # default is much larger; lower it only to shrink request payloads.
    "prompt_max_bytes": 262144,
    # Diagnostics: dump every raw upstream response to debug_raw_file so the
    # payload shape (thinking vs answer vs tool call) can be inspected. Off by
    # default -- raw responses can be large and carry conversation content.
    "debug_raw": False,
    "debug_raw_file": "gemini-raw.log",
    # Four-leg raw trace for deep investigation: CLIENT -> PROXY (do_POST body),
    # PROXY -> MODEL (built prompt), MODEL -> PROXY (raw upstream bytes) and
    # PROXY -> CLIENT (JSON/SSE frames), appended to trace_file. Gated
    # separately from debug_raw because it also captures client conversation
    # content on both ends.
    "debug_trace": False,
    "trace_file": "gemini-trace.log",
}

CONFIG = dict(DEFAULT_CONFIG)

# Global lock for CONFIG mutations across threads. Acquire before reading
# multiple interdependent fields or writing any field. Not needed for single
# reads under the GIL, but required when: (1) multiple fields must be read
# atomically, (2) a write depends on a prior read, or (3) code modifies
# CONFIG outside this module.
_config_lock = threading.Lock()


def _config_lock_ctx():
    """Context manager for CONFIG mutations (keeps call sites clean)."""
    return _config_lock


def load_config(path: str = None):
    """Load config from JSON file."""
    if path and os.path.exists(path):
        with open(path) as f:
            CONFIG.update(json.load(f))
    return CONFIG


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None


# ==============================================================================
# Section: models.py
# ==============================================================================

"""Model definitions and mapping from Gemini frontend StreamGenerate payloads."""
import json

# Model selection uses TWO fields in the f.req inner array (decoded from live
# browser captures, Sep 2026):
#   inner[79] = family: 1=flash, 3=pro, 4=auto, 5=dynamic-thinking, 6=flash-lite
#   inner[80] = variant: 1=standard, 2=extended/thinking, 3=enhanced
# E.g. 3.1 Pro=(3,1), Flash Extended=(1,2), Flash-Lite=(6,1).
#
# HOWEVER: the server only honors those fields when the request also carries
# the per-model ticket header X-Goog-Ext-525001261-Jspb (minted by the browser
# per model family; embeds family/variant in plaintext). Without the ticket the
# server ignores [79]/[80] entirely and answers with the account default model
# -- observed as 3.1 Pro, so every requested model silently became Pro. The
# ticket wins over the body fields when both are present, and it is what the
# echo check compares against (see gemini.check_routing).
#
# Note: no field selects the exact 3.x point version within a family; the
# server picks its current default (e.g. requesting "3.5-flash" yields 3.6
# Flash). The "3.x" in a model name is a label, not a routing hint.

# MODE_CATEGORY enum from 028-6eb337387583.js:
#   1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE
#   8=ARGON (captured 2026-11-07 from a paid account; the ticket still wins)

TICKET_HEADER = "X-Goog-Ext-525001261-Jspb"

# `variant` maps to inner[80]; `ticket` is the key into
# CONFIG["model_tickets"]. Models without a ticket have no live-verified
# ticket value, so they fall back to the body fields (and to the account
# default when the upstream declines those too).
MODELS = {
    "gemini-3.7-flash": {
        "mode": 1, "think": 4, "variant": 1, "ticket": "flash",
        "desc": "Latest all-around model (Gemini 3.7 Flash)",
    },
    "gemini-3.6-flash": {
        "mode": 1, "think": 4, "variant": 1, "ticket": "flash",
        "desc": "All-around model (Gemini 3.6 Flash)",
    },
    "gemini-3.5-flash": {
        "mode": 1, "think": 4, "variant": 1, "ticket": "flash",
        "desc": "Alias for gemini-3.6-flash (backend upgraded)",
    },
    "gemini-3.8-flash": {
        "mode": 1, "think": 4, "variant": 1, "ticket": "flash",
        "desc": "Latest Flash, low thinking (family 1 variant 1)",
    },
    "gemini-3.8-flash-low": {
        "mode": 1, "think": 4, "variant": 1, "ticket": "flash",
        "desc": "Flash low thinking (family 1 variant 1)",
    },
    "gemini-3.8-flash-medium": {
        "mode": 1, "think": 4, "variant": 4, "ticket": "flash-medium",
        "desc": "Flash medium thinking (family 1 variant 4)",
    },
    "gemini-3.8-flash-high": {
        "mode": 1, "think": 4, "variant": 2, "ticket": "flash-thinking",
        "desc": "Flash high thinking (family 1 variant 2)",
    },
    "gemini-3.5-flash-thinking": {
        # think=1 (Extended), not 0: live checks showed 1 produces the longer
        # thinking answer, 0 the shorter one.
        "mode": 2, "think": 1, "variant": 2, "ticket": "flash-thinking",
        "desc": "Deep thinking mode, longest output (~20k chars)",
    },
    "gemini-3.1-pro": {
        "mode": 3, "think": 4, "variant": 1, "ticket": "pro",
        "desc": "Pro model (requires cookie for real routing)",
    },
    "gemini-3.1-pro-low": {
        "mode": 3, "think": 4, "variant": 1, "ticket": "pro",
        "desc": "Pro low thinking (family 3 variant 1)",
    },
    "gemini-3.1-pro-medium": {
        "mode": 3, "think": 4, "variant": 4, "ticket": "pro-medium",
        "desc": "Pro medium thinking (family 3 variant 4)",
    },
    "gemini-3.1-pro-high": {
        "mode": 3, "think": 4, "variant": 2, "ticket": "pro-thinking",
        "desc": "Pro high thinking (family 3 variant 2)",
    },
    "gemini-4-argon": {
        "mode": 8, "think": 4, "variant": 1, "ticket": "argon",
        "desc": "Argon model, low thinking (family 8 variant 1)",
    },
    "gemini-4-argon-low": {
        "mode": 8, "think": 4, "variant": 1, "ticket": "argon",
        "desc": "Argon low thinking (family 8 variant 1)",
    },
    "gemini-4-argon-medium": {
        "mode": 8, "think": 4, "variant": 4, "ticket": "argon-medium",
        "desc": "Argon medium thinking (family 8 variant 4)",
    },
    "gemini-4-argon-high": {
        "mode": 8, "think": 4, "variant": 2, "ticket": "argon-high",
        "desc": "Argon high thinking (family 8 variant 2)",
    },
    "gemini-3.1-pro-enhanced": {
        # No ticket: a ticket wins over the body, which would throw away the
        # enhanced variant encoded in extra[80]=3.
        "mode": 3, "think": 4, "extra": {31: 2, 80: 3},
        "desc": "Pro with enhanced output (experimental)",
    },
    "gemini-auto": {
        # No ticket: the upstream picks the model for this family itself.
        "mode": 4, "think": 4, "variant": 1,
        "desc": "Auto model selection",
    },
    "gemini-3.5-flash-thinking-lite": {
        # The lite-thinking ticket routes to Flash-Lite Extended (6,2) even
        # though the body says family 5 -- the ticket wins.
        "mode": 5, "think": 1, "variant": 2, "ticket": "lite-thinking",
        "desc": "Dynamic thinking with adaptive depth",
    },
    "gemini-flash-lite": {
        "mode": 6, "think": 4, "variant": 1, "ticket": "lite",
        "desc": "Lightweight fast model",
    },
    "gemini-3.5-flash-lite": {
        # Explicit alias: clients (opencode configs) name the versioned id;
        # without it resolve_model fell back to flash and every lite request
        # silently ran on the wrong family.
        "mode": 6, "think": 4, "variant": 1, "ticket": "lite",
        "desc": "Alias for gemini-flash-lite (Gemini 3.5 Flash-Lite)",
    },
    "gemini-3.5-flash-lite-low": {
        "mode": 6, "think": 4, "variant": 1, "ticket": "lite",
        "desc": "Flash-Lite thinking low (family 6 variant 1)",
    },
    "gemini-3.5-flash-lite-medium": {
        "mode": 6, "think": 4, "variant": 4, "ticket": "lite-medium",
        "desc": "Flash-Lite thinking medium (family 6 variant 4)",
    },
    "gemini-3.5-flash-lite-high": {
        "mode": 6, "think": 4, "variant": 2, "ticket": "lite-thinking",
        "desc": "Flash-Lite thinking high (family 6 variant 2)",
    },
}


# The (family, variant) each ticket key is expected to embed at ticket
# indexes 14/15. When a configured ticket carries anything else the upstream
# silently serves a different model (today: flash-thinking runs Flash-Lite
# Extended), so startup warns instead of mis-routing quietly. Tickets cannot
# be re-captured programmatically -- the owner refreshes them from a fresh
# browser session.
EXPECTED_TICKET_ROUTES = {
    "flash": (1, 1),
    "flash-medium": (1, 4),
    "flash-thinking": (1, 2),
    "lite": (6, 1),
    "lite-medium": (6, 4),
    "lite-thinking": (6, 2),
    "pro": (3, 1),
    "pro-medium": (3, 4),
    "pro-thinking": (3, 2),
    "argon": (8, 1),
    "argon-medium": (8, 4),
    "argon-high": (8, 2),
}


def ticket_warnings() -> list:
    """Warning strings for configured tickets embedding an unexpected route.

    Each ``CONFIG["model_tickets"]`` value is parsed and its indexes 14/15
    compared against :data:`EXPECTED_TICKET_ROUTES`. Missing keys are skipped
    (no ticket degrades to the body fields, which ``check_routing`` covers);
    unparsable ones warn. Silent when everything matches.
    """
    from gemini_web2api.config import CONFIG
    warnings = []
    tickets = CONFIG.get("model_tickets") or {}
    for key, (want_fam, want_var) in EXPECTED_TICKET_ROUTES.items():
        raw = tickets.get(key)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            fam, var = parsed[14], parsed[15]
        except (json.JSONDecodeError, IndexError, TypeError):
            warnings.append(
                f"model ticket '{key}' could not be parsed -- refresh it from "
                f"a fresh browser capture")
            continue
        if fam != want_fam or var != want_var:
            warnings.append(
                f"model ticket '{key}' embeds (family={fam}, variant={var}), "
                f"expected (family={want_fam}, variant={want_var}) -- the "
                f"upstream serves a different model until this ticket is "
                f"refreshed from a fresh browser capture")
    return warnings


# Thinking level each ticket key represents. Used only for the fallback log so
# an operator can see which level was requested and which one actually routed.
_TICKET_KEY_LEVEL = {
    "flash": "low",
    "flash-medium": "medium",
    "flash-thinking": "high",
    "lite": "low",
    "lite-medium": "medium",
    "lite-thinking": "high",
    "pro": "low",
    "pro-medium": "medium",
    "pro-thinking": "high",
    "argon": "low",
    "argon-medium": "medium",
    "argon-high": "high",
}

# Account-safe ticket fallback. A free Google account usually only has a subset
# of the paid routes, so try the requested key first, then lower levels in the
# same family, then lower families (argon -> pro -> flash -> lite). Each chain
# is explicit (not computed) so it can be reviewed at a glance.
_TICKET_FALLBACKS = {
    "argon-high": (
        "argon-high", "argon-medium", "argon",
        "pro-thinking", "pro-medium", "pro",
        "flash-thinking", "flash-medium", "flash",
        "lite-thinking", "lite-medium", "lite",
    ),
    "argon-medium": (
        "argon-medium", "argon",
        "pro-medium", "pro",
        "flash-medium", "flash",
        "lite-medium", "lite",
    ),
    "argon": ("argon", "pro", "flash", "lite"),
    "pro-thinking": (
        "pro-thinking", "pro-medium", "pro",
        "flash-thinking", "flash-medium", "flash",
        "lite-thinking", "lite-medium", "lite",
    ),
    "pro-medium": (
        "pro-medium", "pro",
        "flash-medium", "flash",
        "lite-medium", "lite",
    ),
    "pro": ("pro", "flash", "lite"),
    "flash-thinking": (
        "flash-thinking", "flash-medium", "flash",
        "lite-thinking", "lite-medium", "lite",
    ),
    "flash-medium": ("flash-medium", "flash", "lite-medium", "lite"),
    "flash": ("flash", "lite"),
    "lite-thinking": ("lite-thinking", "lite-medium", "lite"),
    "lite-medium": ("lite-medium", "lite"),
    "lite": ("lite",),
}

# (model_name, requested_key, used_key) triples already logged, so one request
# burst reports each downgrade once instead of per turn.
_TICKET_FALLBACK_LOGGED = set()


def _resolve_ticket(model_name: str):
    """Return ``(requested_key, used_key, value)`` for a model.

    ``requested_key`` is the key named by :data:`MODELS`; ``used_key`` is the
    key whose ticket was actually selected (it differs when the account lacks
    the exact level/family). ``value`` is ``None`` when no key in the chain
    exists. This helper exists so the request log can show both keys instead
    of claiming the requested ticket was used after a fallback.
    """
    from gemini_web2api.config import CONFIG
    cfg = MODELS.get(model_name) or {}
    key = cfg.get("ticket")
    if not key:
        return None, None, None
    tickets = CONFIG.get("model_tickets") or {}
    for alt in _TICKET_FALLBACKS.get(key, (key,)):
        value = tickets.get(alt)
        if not value:
            continue
        if alt != key:
            marker = (model_name, key, alt)
            if marker not in _TICKET_FALLBACK_LOGGED:
                _TICKET_FALLBACK_LOGGED.add(marker)
                from gemini_web2api.gemini import log
                requested = _TICKET_KEY_LEVEL.get(key, key)
                used = _TICKET_KEY_LEVEL.get(alt, alt)
                log(f"[model] ticket fallback: {model_name} "
                    f"requested={requested} ({key}) -> "
                    f"used={used} ({alt})")
        return key, alt, value
    return key, None, None


def ticket_for(model_name: str):
    """Return the upstream ticket header value for a model, or None.

    Tickets live in ``CONFIG["model_tickets"]`` because they are minted by a
    real browser session and expire: an absent or stale key degrades to the
    body fields rather than erroring, and ``gemini.check_routing`` warns when
    the upstream then answers with a different model.

    When the exact ticket key is missing (common on a free account) the
    fallback chain for that level/family is used instead, so the request still
    routes to a real model rather than silently becoming the account default.
    """
    return _resolve_ticket(model_name)[2]


def resolve_model(model_name: str, default: str = "gemini-3.6-flash"):
    """Resolve model name to (name, mode_id, think_mode, error, extra_fields).

    Unknown model names fall back to default rather than erroring,
    since upstream clients may request arbitrary model identifiers.
    """
    think_override = None
    if default not in MODELS:
        default = "gemini-3.6-flash"
    if not isinstance(model_name, str) or not model_name:
        model_name = default
    if "@think=" in model_name:
        model_name, think_str = model_name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError:
            return None, None, None, f"Invalid think level: {think_str}", None
    cfg = MODELS.get(model_name)
    if not cfg:
        from gemini_web2api.gemini import log
        log(f"Unknown model '{model_name}', falling back to '{default}'")
        model_name = default
        cfg = MODELS[default]
    mode_id = cfg["mode"]
    think_mode = think_override if think_override is not None else cfg["think"]
    extra = dict(cfg.get("extra") or {})
    # inner[80] (variant) travels through extra_fields so a model can pin it
    # explicitly -- pro-enhanced already does; never overwrite that.
    if "variant" in cfg and 80 not in extra:
        extra[80] = cfg["variant"]
    return model_name, mode_id, think_mode, None, extra or None


# Thinking level selected by the OpenAI-style `reasoning_effort` request field.
# Any base id in ``_EFFORT_BASE_MODELS`` is re-routed to its -low/-medium/-high
# alias; an explicit level name always wins, and other models ignore effort.
_EFFORT_SUFFIX = {
    "minimal": "-low",
    "low": "-low",
    "medium": "-medium",
    "high": "-high",
}
# Base id -> prefix used to build the level-specific alias. Both lite ids map
# to the versioned 3.5 Flash-Lite names (the canonical entries).
_EFFORT_BASE_MODELS = {
    "gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
    "gemini-flash-lite": "gemini-3.5-flash-lite",
    "gemini-3.8-flash": "gemini-3.8-flash",
    "gemini-3.1-pro": "gemini-3.1-pro",
    "gemini-4-argon": "gemini-4-argon",
}


def _reasoning_effort(req):
    """Return a normalized effort from a request body, or None.

    Accepts the top-level ``reasoning_effort`` string, or the Responses-style
    ``reasoning.effort`` nested object. Unknown values are ignored.
    """
    if not isinstance(req, dict):
        return None
    effort = req.get("reasoning_effort")
    if not isinstance(effort, str):
        reasoning = req.get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if not isinstance(effort, str):
        return None
    effort = effort.strip().lower()
    return effort if effort in _EFFORT_SUFFIX else None


def _lite_effort_model(model_name, effort):
    """Map a base model id to its effort-specific alias.

    Kept under the historical name because callers/tests import it, but this
    now covers every base id that accepts ``reasoning_effort``.
    """
    prefix = _EFFORT_BASE_MODELS.get(model_name)
    suffix = _EFFORT_SUFFIX.get(effort)
    if prefix and suffix:
        return prefix + suffix
    return model_name


# ==============================================================================
# Section: tools.py
# ==============================================================================

"""Tool calling and multimodal message parsing."""
import ast
import json
import re
import uuid
import base64
import binascii
import unicodedata
from urllib.parse import unquote_to_bytes

# Fallback bound for the generated prompt when config carries no
# ``prompt_max_bytes``. Keeps large tool lists / long histories from being
# rejected by the upstream web endpoint. The live default lives in
# config.DEFAULT_CONFIG (well above this): 60000 was a single file read away
# from exhaustion in agent sessions, which cost the pending question.
PROMPT_MAX_BYTES = 60000


def _prompt_max_bytes() -> int:
    """Effective prompt budget in bytes: config override, else the fallback.

    Read from CONFIG at call time (not import time) so a config reload or a
    test patching ``CONFIG["prompt_max_bytes"]`` takes effect immediately.
    """
    from gemini_web2api.config import CONFIG
    try:
        value = int(CONFIG.get("prompt_max_bytes", PROMPT_MAX_BYTES))
    except (TypeError, ValueError):
        return PROMPT_MAX_BYTES
    return value if value > 0 else PROMPT_MAX_BYTES


def _build_tool_choice_instruction(tool_choice, tool_defs: list) -> str:
    """Build tool_choice constraint instruction.

    tool_choice values:
      - "none": do not call any tool
      - "auto": decide whether to call tools (default)
      - "required": must call at least one tool
      - {"type": "function", "function": {"name": "xxx"}}: must call specific tool
    """
    if tool_choice == "none":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if tool_choice == "required":
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    if isinstance(tool_choice, dict):
        # Chat Completions uses {"type":"function","function":{"name":"x"}};
        # the Responses API drops the nesting and uses {"type":"function","name":"x"}.
        fn = tool_choice.get("function")
        fn_name = fn.get("name", "") if isinstance(fn, dict) else ""
        if not fn_name:
            fn_name = tool_choice.get("name") or ""
        if fn_name:
            return f'\n\nIMPORTANT: You MUST call the tool "{fn_name}". Do not call other tools.'
    return ""


def _decode_data_url(url: str):
    match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", url, re.DOTALL)
    if not match:
        return None
    mime = match.group(1) or "image/png"
    is_base64 = bool(match.group(2))
    data = match.group(3)
    try:
        if is_base64:
            return base64.b64decode(data, validate=True), mime
        return unquote_to_bytes(data), mime
    except (ValueError, TypeError, binascii.Error):
        return None


def _image_from_url(url: str, mime: str = None):
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        return _decode_data_url(url)
    return url, mime or "image/png"


def _image_from_part(part: dict):
    part_type = part.get("type")
    if part_type == "image_url":
        image_url = part.get("image_url", {})
        if isinstance(image_url, dict):
            return _image_from_url(image_url.get("url"), image_url.get("mime_type"))
        return _image_from_url(image_url)
    if part_type in ("input_image", "image"):
        image_url = part.get("image_url") or part.get("url")
        if isinstance(image_url, dict):
            return _image_from_url(image_url.get("url"), image_url.get("mime_type"))
        if image_url:
            return _image_from_url(image_url, part.get("mime_type"))
        image_data = part.get("data") or part.get("base64")
        if isinstance(image_data, str):
            mime = part.get("mime_type") or part.get("media_type") or "image/png"
            if image_data.startswith("data:"):
                return _decode_data_url(image_data)
            try:
                return base64.b64decode(image_data, validate=True), mime
            except (ValueError, TypeError, binascii.Error):
                return None
    return None


def _stringify_content(content) -> str:
    """Flatten OpenAI message content (str or list of parts) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, dict):
                if c.get("type") in ("text", "input_text", "output_text"):
                    out.append(c.get("text", ""))
                elif isinstance(c.get("text"), str):
                    out.append(c["text"])
            elif isinstance(c, str):
                out.append(c)
        return "\n".join(p for p in out if p)
    return str(content)


def tool_names(tools) -> set:
    """Collect declared function names from an OpenAI tools list."""
    names = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function", tool) if tool.get("type") == "function" else tool
        if isinstance(fn, dict) and fn.get("name"):
            names.add(fn["name"])
    return names


def tool_parameters(tools) -> dict:
    """Map tool name -> set of top-level parameter names from a tools list.

    Used to recover a call whose model omitted ``name``: the parameter keys are
    matched against these sets, so a guess is only ever made when exactly one
    declared tool could have produced them.
    """
    schemas = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function", tool) if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters") or tool.get("parameters") or {}
        props = params.get("properties") if isinstance(params, dict) else None
        schemas[fn["name"]] = set(props) if isinstance(props, dict) else set()
    return schemas


def tool_required_params(tools) -> dict:
    """Map tool name -> set of required parameter names from a tools list.

    Complements :func:`tool_parameters`: a call can carry every declared
    property and still be unusable because the one required one is missing,
    and neither the client nor the model learns anything from the failure it
    produces.
    """
    required = {}
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function", tool) if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters") or tool.get("parameters") or {}
        names = params.get("required") if isinstance(params, dict) else None
        if isinstance(names, list):
            required[fn["name"]] = {n for n in names if isinstance(n, str)}
    return required


def missing_required_params(tool_calls, required) -> list:
    """``["tool.param", ...]`` for every required parameter a call left out.

    A call that parses but omits a required parameter reaches the client as a
    perfectly valid tool call, fails on the client's side, and the error the
    client sends back costs a round-trip the model spends guessing what was
    wrong. Reported per *tool.parameter* so the retry instruction can name the
    exact thing to add. Arguments that are not a JSON object are skipped: they
    cannot be checked, and they already carry their own "unusable arguments"
    report.
    """
    missing = []
    if not required:
        return missing
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name")
        params = required.get(name)
        if not params:
            continue
        try:
            args = json.loads(fn.get("arguments") or "{}", strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(args, dict):
            continue
        missing.extend(f"{name}.{p}" for p in sorted(params) if p not in args)
    return missing


# Sits between the dropped leading block and the kept tail so the model can see
# that history was removed rather than silently wondering what it missed.
_TRUNCATION_MARKER = "[...truncated...]"


def _utf8_safe_slice(data: bytes, start: int, end: int) -> bytes:
    """Slice bytes, snapping to valid UTF-8 character boundaries.

    Raw byte slicing cuts multi-byte characters in half; ``decode``
    with ``errors="ignore"`` then silently drops the fragments.
    Snapping to boundaries keeps whole characters intact.
    """
    if start >= end:
        return b""
    # Advance past any partial continuation byte at the start.
    while start < end and (data[start] & 0xC0) == 0x80:
        start += 1
    # Find the first byte of the character that ends at end-1.
    pos = end - 1
    while pos > start and (data[pos] & 0xC0) == 0x80:
        pos -= 1
    first = data[pos]
    if first & 0xE0 == 0xC0:
        char_len = 2
    elif first & 0xF0 == 0xE0:
        char_len = 3
    elif first & 0xF8 == 0xF0:
        char_len = 4
    else:
        char_len = 1
    # If the character extends past end, it is cut off: drop it.
    if pos + char_len > end:
        end = pos
    return data[start:end]


def _squeeze_part(part: str, cap: int) -> str:
    """Reduce one message to ~``cap`` bytes, keeping both its ends.

    A combined part starts with the ``[Assistant]:`` / ``[Tool result for
    read ...]`` labels that say *what* this blob is and ends with the newest
    lines of the file or a trailing question. Keeping only the tail (the old
    behaviour) handed the model an unattributed slab of code: it could not
    tell which file it came from, re-read files it had already read, and --
    once older messages were evicted too -- answered with no idea what the
    task was. Head keeps the labels, tail keeps the question/last lines, and
    the marker in between shows content was dropped.
    """
    raw = part.encode("utf-8")
    if len(raw) <= cap:
        return part
    marker = _TRUNCATION_MARKER.encode("utf-8")
    # Head 1/3 (labels + opening context), tail the rest (newest content).
    head_len = max(0, cap // 3 - len(marker) - 2)
    tail_len = max(0, cap - head_len - len(marker) - 2)
    if head_len <= 0 or tail_len <= 0:
        # Degenerate cap: fall back to keeping the tail end only, which at
        # least preserves a question asked at the end of the message.
        return _utf8_safe_slice(raw, len(raw) - cap, len(raw)).decode(
            "utf-8", errors="ignore")
    squeezed = (_utf8_safe_slice(raw, 0, head_len) + b"\n" + marker + b"\n"
                + _utf8_safe_slice(raw, len(raw) - tail_len, len(raw)))
    return squeezed.decode("utf-8", errors="ignore")


def _join_prompt_parts(parts: list, max_bytes: int, pinned_head: str = None,
                       pinned_first: str = None) -> str:
    """Join message parts into a prompt, dropping the *middle* when too long.

    Slicing the assembled prompt from the front (the obvious way) keeps the
    system instruction and the oldest history while throwing away the newest
    user message -- i.e. exactly the question that needs answering. Instead we
    keep the leading block (tool definitions / system instruction) and as many
    of the newest messages as fit, and mark the gap.

    ``pinned_head`` is reserved *before* anything else is allocated. Without it
    the newest messages (a large tool result, say) can fill the budget on their
    own, leaving nothing for the leading block -- and a prompt with no tool
    definitions makes the model answer in prose instead of calling a tool,
    which is indistinguishable from the model simply refusing to act.

    ``pinned_first`` is the first user message (the plan/task in agent
    sessions). It gets its own reservation so long middle histories cannot
    evict it: losing the plan while keeping tool definitions produces a model
    that calls tools correctly but for a forgotten task.
    """
    items = [p for p in parts if p]

    def nbytes(text: str) -> int:
        return len(text.encode("utf-8"))

    # Cap the pin so a pathological tool list cannot consume the budget on its
    # own; ~40% leaves the majority for the history and the pending question.
    # Squeezing (head+tail) keeps the header and the trailing constraint;
    # byte-slicing cut the constraint off first. Pins are only squeezed when
    # the prompt actually overflows: squeezing unconditionally would stamp a
    # "[...truncated...]" marker into a prompt that fits untouched.
    pinned = (pinned_head.strip() if pinned_head else "")
    first_pin = (pinned_first.strip() if pinned_first else "")
    first_idx = -1
    if first_pin:
        for i, p in enumerate(items):
            if p == pinned_first:
                first_idx = i
                break

    full = "\n\n".join(([pinned] if pinned else []) + items)
    if nbytes(full) <= max_bytes:
        return full

    if pinned:
        cap = max_bytes * 2 // 5
        if nbytes(pinned) > cap:
            pinned = _squeeze_part(pinned, cap).strip() or pinned
    if first_pin:
        fcap = max(1, max_bytes // 7)
        if nbytes(first_pin) > fcap:
            first_pin = _squeeze_part(first_pin, fcap).strip() or first_pin
        # Squeeze the original in place so the head loop below (which fills
        # from the start in order) can afford it inside its reservation.
        # Order is preserved: system stays before the first user message.
        if first_idx != -1:
            items[first_idx] = first_pin

    # Squeezing the pins alone may have been enough (a huge tool block with
    # a short history): re-check before dropping any middle.
    full = "\n\n".join(([pinned] if pinned else []) + items)
    if nbytes(full) <= max_bytes:
        return full

    from gemini_web2api.gemini import log
    log(f"Prompt truncated to {max_bytes} bytes")
    if pinned:
        log(f"Tool block pinned: {nbytes(pinned)} bytes "
            f"({100 * nbytes(pinned) // max_bytes}% of budget)")
    if first_pin:
        log(f"First user message pinned: {nbytes(first_pin)} bytes")

    # Two separators around the marker.
    budget = max_bytes - nbytes(_TRUNCATION_MARKER) - 4
    if pinned:
        budget -= nbytes(pinned) + 2
    if budget <= 0:
        # Degenerate budget (tiny max_bytes): give up on pinning rather than
        # drop the question entirely.
        pinned = ""
        budget = max_bytes - nbytes(_TRUNCATION_MARKER) - 4
    if budget <= 0:
        return full.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")

    # Reserve the first user message's share before the newest messages are
    # allocated: otherwise a giant tail part eats the budget and the plan --
    # the first thing the head loop would need -- never fits. When the first
    # message is also the newest (a short session), no reservation is needed:
    # the tail keeps it naturally.
    skip_first_in_tail = bool(first_pin) and first_idx != -1 \
        and first_idx != len(items) - 1
    first_cost = 0
    if skip_first_in_tail:
        first_cost = nbytes(first_pin) + 2
    tail_budget = budget - first_cost
    if tail_budget < 0:
        tail_budget = 0

    # 1. Keep the newest messages first: they carry the pending question and
    #    the latest tool results. No single message may claim more than half
    #    the budget: when one giant part (a 46 KB file read) was allowed to
    #    take everything, it evicted every older message including the user's
    #    original request, and only its tail -- stripped of the
    #    "[Tool result for read ...]" label at its head -- reached the model.
    #    The pinned first message is skipped here: it is allocated in step 2.
    tail = []
    taken = set()
    used = 0
    part_cap = max(1, budget // 2)
    for pos in range(len(items) - 1, -1, -1):
        if skip_first_in_tail and pos == first_idx:
            continue
        part = items[pos]
        if nbytes(part) > part_cap:
            part = _squeeze_part(part, part_cap)
        cost = nbytes(part) + (2 if tail else 0)
        if used + cost > tail_budget:
            break
        tail.append(part)
        taken.add(pos)
        used += cost
    tail.reverse()

    # 2. Spend whatever is left on the leading block (tool defs, system, ...).
    #    This only ever adds a prefix, so history in the middle stays dropped.
    #    The pinned first message is admitted at its natural position (after
    #    the system prompt, where it already sits); the reservation above
    #    guarantees room for its squeezed form.
    remaining = budget - used
    head = []
    head_used = 0
    for pos, part in enumerate(items):
        if skip_first_in_tail and pos == first_idx:
            part = first_pin
        elif pos in taken:
            break
        cost = nbytes(part) + (2 if head else 0)
        if head_used + cost > remaining:
            break
        head.append(part)
        head_used += cost

    blocks = ([pinned] if pinned else []) + head
    if blocks:
        return "\n\n".join(blocks + [_TRUNCATION_MARKER] + tail)
    return "\n\n".join([_TRUNCATION_MARKER] + tail)


def _compact_tool(t: dict, desc_max: int = 300) -> str:
    """One line per tool: name, parameter names, types and which are required.

    Used when the full JSON schema no longer fits. Dropping the schemas
    outright (the previous behaviour) leaves the model calling a tool whose
    parameters it can no longer see, which produces calls that fail
    validation -- so signatures are always kept and only prose is shortened.
    Nested object/array shapes are flagged so the model knows a parameter
    takes a structured value even though its inner schema was cut.
    """
    p = t.get("parameters") or {}
    props = p.get("properties") if isinstance(p, dict) else None
    props = props if isinstance(props, dict) else {}
    required = set(p.get("required", [])) if isinstance(p, dict) else set()

    def _kind(v) -> str:
        if not isinstance(v, dict):
            return "any"
        kind = v.get("type", "any")
        if kind in ("object", "array") and ("properties" in v or "items" in v):
            return f"{kind}(nested schema omitted)"
        return kind

    args = ", ".join(
        f'{k}{"" if k in required else "?"}: {_kind(v)}'
        for k, v in props.items()
    )
    return f'- {t["name"]}({args}): {(t.get("description") or "")[:desc_max]}'


# Appended after the last message whenever tools are offered. It has to be the
# final thing the model reads: in a long conversation the original instruction
# to *use* the tools sits far back, and the model reverts to answering from
# memory -- which looks exactly like a successful answer with no tool call.
_TOOL_REMINDER = ("\n\n[Reminder: you have working tools. To know what a file "
                  "contains or what a command produced, call the tool -- do not "
                  "answer from memory and do not claim to have read or run "
                  "anything you have not.]")

# Appended instead of _TOOL_REMINDER when the conversation already ends in a
# tool result. The model has just acted and tends to act again (read-read-read
# loops); this tells it the result above is usable and a repeated identical
# call is never the next step.
_TOOL_RESULT_REMINDER = ("\n\n[Reminder: the tool result above is already in "
                         "the conversation. If it is enough, answer now in "
                         "plain text; otherwise call the next tool. Do not "
                         "repeat a call whose result is already present.]")


def _example_call(tool_defs: list) -> str:
    """Render a copy-paste-shaped example call from the first declared tool.

    Placeholders (``func_name``/``param``) taught weak models to emit the
    placeholder itself as the call, so the example names a real tool and real
    parameter keys: one entry per ``required`` parameter (fallback: the first
    two declared properties; ``{}`` when the tool takes none), with values
    typed by the schema type.

    String values carry an ``EXAMPLE_`` prefix so a verbatim copy is
    detectable: :func:`parse_tool_calls` drops calls whose arguments are
    still the placeholder and tells the model to fill in real values.
    """
    first = (tool_defs or [None])[0] or {}
    name = first.get("name") if isinstance(first, dict) else None
    name = name or "tool_name"
    params = first.get("parameters") if isinstance(first, dict) else None
    props = params.get("properties") if isinstance(params, dict) else None
    props = props if isinstance(props, dict) else {}
    required = params.get("required") if isinstance(params, dict) else None
    if isinstance(required, list):
        keys = [k for k in required if k in props]
    else:
        keys = []
    if not keys:
        keys = list(props)[:2]
    example_args = {}
    for key in keys:
        spec = props.get(key) or {}
        kind = spec.get("type") if isinstance(spec, dict) else None
        if kind in ("integer", "number"):
            example_args[key] = 0
        elif kind == "boolean":
            example_args[key] = False
        elif kind == "array":
            example_args[key] = []
        elif kind == "object":
            example_args[key] = {}
        else:
            example_args[key] = f"EXAMPLE_{key}"
    return json.dumps({"name": name, "arguments": example_args},
                      ensure_ascii=False)


def _is_placeholder_args(args_json: str) -> bool:
    """True when parsed arguments still carry the ``EXAMPLE_`` placeholder."""
    try:
        args = json.loads(args_json, strict=False)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(args, dict):
        return False
    return any(isinstance(v, str) and v.startswith("EXAMPLE_")
               for v in args.values())


def _tool_use_block(tools_json: str, constraint: str, tool_defs: list = None) -> str:
    """The ``# Tool Use`` header the model must always be able to see.

    Pinned by ``_join_prompt_parts``: without it a large tool result can fill
    the prompt budget on its own and evict the tool definitions entirely, after
    which the model has no idea any tool exists.
    """
    return (
        "# Tool Use\n\n"
        "The tools below are connected to the user's machine and execute for "
        "real the moment you call them. You have full access to them.\n\n"
        "Call format (use this exact format):\n"
        '```tool_call\n'
        f'{_example_call(tool_defs)}\n'
        "```\n\n"
        "When calling tools:\n"
        "- Each turn do exactly ONE of: (A) you need to read or change "
        "something -> output ONLY tool_call block(s); (B) you already have "
        "everything -> answer as plain text with NO block. Never both, and "
        "never any prose after the block.\n"
        "- Before answering a request to review, audit, inspect or find bugs in "
        "source code, you MUST read the actual contents of the relevant files with "
        "a read/search tool first -- reading one file is not reviewing a codebase. "
        "Listing or globbing file names is not reading: "
        "if you have only listed or opened one file, read the other relevant ones before you "
        "answer. If you name files as part of the review, read every one of them -- "
        "a file you only mentioned is a file you have not reviewed.\n"
        "- Fixed flow for EVERY turn, in order, no skipping: "
        "(1) do you already have a [Tool result for ...] message proving "
        "everything you need? If YES -> answer as plain text. "
        "If NO -> output ONLY tool_call block(s) and stop. "
        "A guess, a generic checklist, or instructions telling the user to "
        "do it themselves are never an answer. "
        "(2) When the result arrives, go back to (1).\n"
        "- Output ONLY tool_call block(s): no prose, explanation or apology "
        "before or after them.\n"
        '- Every parameter belongs inside the "arguments" object, keyed exactly '
        'like the "parameters" properties below -- never beside "name".\n'
        "- Emit several blocks in one turn when the calls are independent.\n"
        "- Search results are leads, not evidence: after a glob/grep survey, "
        "read the matched files before answering or editing -- never quote "
        "or patch code you only saw in a snippet.\n"
        "- A limited read proves only the lines it shows: a 200-line head "
        "read says nothing about classes, functions or routes defined later "
        "in the same file. Before naming any symbol (class, function, "
        "endpoint, constant), grep for its name and read that section -- "
        "never invent plausible-looking names for code you have not opened.\n"
        "- When the task needs project-wide context (structure, conventions, "
        "architecture, where a feature lives) -- including how-to questions "
        "like where to put a new endpoint or which pattern to follow -- glob "
        "for README.md / AGENTS.md / agents.md first and read what exists "
        "before reading source -- they say where things live so the reads "
        "land in the right files. If the glob "
        "finds no docs, proceed with source as usual; narrow single-file "
        "tasks never need this step.\n"
        "- Before editing a symbol, check its other usages with a search "
        "tool; after editing, re-read the changed lines, then run the unit "
        "tests unless told not to -- an unverified fix is a claim, not "
        "evidence. If no test covers the changed behavior, add one first.\n"
        "- When reporting a bug, quote the exact lines verbatim: line "
        "numbers without quotes cannot be checked.\n"
        "- Never state that you have read a file, run a command or inspected "
        "the code unless a [Tool result for ...] message proves it. Knowing a "
        "file's contents or a command's output requires calling the tool.\n"
        "- Never claim the tools are unavailable, restricted, or that you lack "
        "permission: they are connected and will run.\n"
        "- Never write [Tool result for ...] or [Assistant]: yourself, and "
        "never invent a result: emit the block and stop.\n"
        "- When adding a new route, endpoint or feature, ADD A NEW BRANCH (e.g. `elif`) "        "instead of modifying existing ones to force them to match the new requirement. "        "Never replace dynamic variables (like `__version__`) with hardcoded strings "        "unless explicitly instructed to remove the dynamic behavior. Preserve existing "        "logic for existing paths.\n"        
"- Triple backticks inside a string argument (Markdown or code the user "
        "asked you to save) are literal text: keep all three of them. A fence "
        'inside the JSON never closes the "tool_call" block, so shortening '
        "them corrupts the file you were asked to write.\n\n"
        f"Available tools:\n{tools_json}"
        f"{constraint}"
    )


def messages_to_prompt(messages: list, tools: list = None, tool_choice=None) -> tuple:
    """Convert OpenAI messages to (prompt_str, images_list).

    Returns (prompt, images) where images is a list of (bytes, mime_type) tuples.
    """
    from gemini_web2api.gemini import log

    max_bytes = _prompt_max_bytes()

    parts = []
    images = []
    tool_block = None

    if tools and tool_choice != "none":
        tool_defs = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function", tool) if tool.get("type") == "function" else tool
            if not isinstance(fn, dict):
                continue
            name = fn.get("name", tool.get("name", ""))
            if not name:
                continue
            tool_defs.append({
                "name": name,
                "description": fn.get("description", tool.get("description", "")),
                "parameters": fn.get("parameters", tool.get("parameters", {})),
            })
        if tool_defs:
            constraint = _build_tool_choice_instruction(tool_choice, tool_defs)
            # Use compact JSON by default to save space
            tools_json = json.dumps(tool_defs, ensure_ascii=False)
            tool_bytes = len(tools_json.encode("utf-8"))
            # If still too large, trade description prose for space while
            # keeping every name, parameter, type and required flag: a model
            # that cannot see a parameter calls the tool with the wrong shape.
            if tool_bytes > max_bytes // 3:
                for desc_max in (300, 150, 80):
                    tools_json = "\n".join(_compact_tool(t, desc_max)
                                           for t in tool_defs)
                    tool_bytes = len(tools_json.encode("utf-8"))
                    if tool_bytes <= max_bytes // 3:
                        break
                log(f"Tool definitions compacted to signatures "
                    f"(descriptions cut to {desc_max} chars, {tool_bytes} bytes): "
                    "parameter names and types kept")
            else:
                log(f"Tool definitions: {len(tool_defs)} tools, {tool_bytes} bytes")
            tool_block = _tool_use_block(tools_json, constraint, tool_defs)

    # Map tool_call ids -> function names so tool results can be labelled correctly.
    id_to_name = {}
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tcid = tc.get("id")
                    fn = tc.get("function") or {}
                    if tcid and isinstance(fn, dict) and fn.get("name"):
                        id_to_name[tcid] = fn["name"]

    # Track which tool result messages have been combined with their assistant tool_call
    combined_tool_result_ids = set()
    
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        if role == "developer":
            role = "system"
        content = msg.get("content", "")
        if content is None:
            # An explicit ``content: null`` (legal JSON) must not reach the
            # f-strings below as the literal text "None".
            content = ""

        if isinstance(content, list):
            text_parts = []
            for c in content:
                if not isinstance(c, dict):
                    if isinstance(c, str):
                        text_parts.append(c)
                    continue
                if c.get("type") in ("text", "input_text", "output_text"):
                    text_parts.append(c.get("text", ""))
                    continue
                image = _image_from_part(c)
                if image:
                    images.append(image)
                    text_parts.append("[Image attached]")
                elif isinstance(c.get("text"), str):
                    text_parts.append(c["text"])
            content = " ".join(p for p in text_parts if p)

        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            if msg.get("tool_calls"):
                tc_strs = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function") or {}
                    name = json.dumps(fn.get("name", ""), ensure_ascii=False)
                    # An absent/empty "arguments" must still serialise as valid JSON.
                    args = fn.get("arguments") or "{}"
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False)
                    tc_strs.append(
                        f'```tool_call\n{{"id": {json.dumps(tc.get("id", ""))}, '
                        f'"name": {name}, "arguments": {args}}}\n```'
                    )
                # Combine with subsequent tool results to keep them together
                combined = f"[Assistant]: {content or ''}\n" + "\n".join(tc_strs)
                for j in range(i + 1, len(messages)):
                    next_msg = messages[j]
                    if not isinstance(next_msg, dict):
                        break
                    if next_msg.get("role") != "tool":
                        break
                    tcid = next_msg.get("tool_call_id", "")
                    combined_tool_result_ids.add(tcid)
                    next_content = next_msg.get("content", "")
                    if isinstance(next_content, list):
                        text_parts = []
                        for c in next_content:
                            if not isinstance(c, dict):
                                if isinstance(c, str):
                                    text_parts.append(c)
                                continue
                            if c.get("type") in ("text", "input_text", "output_text"):
                                text_parts.append(c.get("text", ""))
                                continue
                            image = _image_from_part(c)
                            if image:
                                images.append(image)
                                text_parts.append("[Image attached]")
                            elif isinstance(c.get("text"), str):
                                text_parts.append(c["text"])
                        next_content = " ".join(p for p in text_parts if p)
                    name = next_msg.get("name") or id_to_name.get(tcid) or "tool"
                    body = _stringify_content(next_content)
                    if _is_file_reader_tool(name):
                        path = _called_file_path(msg.get("tool_calls"), tcid)
                        combined += (f"\n[Tool result for {name} (id={tcid})]:\n"
                                     + _format_file_body(path, body))
                    else:
                        combined += f"\n[Tool result for {name} (id={tcid})]: {body}"
                parts.append(combined)
            else:
                parts.append(f"[Assistant]: {content}")
        elif role == "tool":
            tcid = msg.get("tool_call_id", "")
            # Skip if already combined with assistant tool_call
            if tcid in combined_tool_result_ids:
                continue
            name = msg.get("name") or id_to_name.get(tcid) or "tool"
            body = _stringify_content(content)
            if _is_file_reader_tool(name):
                # Standalone result (its assistant turn was evicted): no call
                # args left to recover the path from, so the label is generic.
                parts.append(f"[Tool result for {name} (id={tcid})]:\n"
                             + _format_file_body(None, body))
            else:
                parts.append(f"[Tool result for {name} (id={tcid})]: {body}")
        else:
            parts.append(_stringify_content(content))

    if tool_block:
        last = messages[-1] if messages else None
        last_role = last.get("role") if isinstance(last, dict) else None
        # The pattern banner goes before the reminder: the reminder has to
        # stay the final thing the model reads.
        banner = task_pattern_banner(messages)
        if banner:
            parts.append(banner)
        ledger = evidence_ledger(messages)
        if ledger:
            parts.append(ledger)
        # A turn that already ends in a tool result needs no push to act --
        # it needs permission to stop acting and answer.
        parts.append(_choose_reminder(messages, last_role))
    first_user_pin = None
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            first_user_pin = _stringify_content(msg.get("content", ""))
            if first_user_pin and first_user_pin.strip():
                break
            first_user_pin = None
    prompt = _join_prompt_parts(parts, max_bytes, pinned_head=tool_block,
                                pinned_first=first_user_pin)
    return defuse_extension_words(prompt), images


# Keys that describe the call itself rather than its parameters. Models emit
# flattened payloads (`{"name": "run_commands", "commands": [...]}`) as readily
# as the canonical nested one, so every other key counts as a parameter.
_CALL_META_KEYS = {"name", "id", "type", "description", "tool", "tool_name"}

# Aliases used for the nested parameter object.
_ARGUMENT_KEYS = ("arguments", "args", "input")

_NOTHING = object()


def extract_arguments(data: dict, props=None):
    """Pull a tool call's parameters out of a parsed payload.

    Handles the canonical ``{"name": ..., "arguments": {...}}`` shape and the
    variants models actually emit: ``args`` / ``input`` aliases, a
    ``parameters`` wrapper, and the flattened form where the parameters sit
    beside ``name``. Returns ``{}`` only when the payload carries none.

    ``props`` is the declared parameter set for the resolved tool (or None
    when unknown): a ``_CALL_META_KEYS`` entry that the schema declares --
    opencode's ``bash`` really takes a ``description`` -- is a parameter, not
    metadata, and survives. Without the schema every meta key is still
    dropped, so a bare ``name``/``id``/``type`` never leaks into arguments.

    A tool that genuinely declares a parameter named ``args``/``input`` (with
    a string value) is not a wrapper: the alias is only honoured when the
    schema does not declare it, or when the payload carries nothing else.
    """
    for key in _ARGUMENT_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict):
            if props is not None and key in props:
                siblings = set(data) - _CALL_META_KEYS - {key}
                if siblings:
                    continue
            return value
        if isinstance(value, str) and value.strip():
            if props is not None and key in props:
                continue
            return value
    if props is None:
        rest = {k: v for k, v in data.items() if k not in _CALL_META_KEYS}
    else:
        rest = {k: v for k, v in data.items()
                if k not in _CALL_META_KEYS or k in props}
    if len(rest) == 1:
        key, value = next(iter(rest.items()))
        if key in ("parameters", "params") and isinstance(value, dict):
            return value
    return rest or {}


def _arguments_json(args) -> tuple:
    """Serialise a tool call's arguments into a JSON object string.

    Returns ``(json_text, degradation)``; ``degradation`` is None when the
    arguments were usable. ``function.arguments`` has to parse as JSON -- and
    for every OpenAI-style client it has to be an object -- otherwise the
    client rejects the call with a bare "Invalid input" that tells the model
    nothing. A payload that cannot be salvaged therefore degrades to ``{}``,
    a well-formed call the model can retry after seeing the client's error.
    """
    if isinstance(args, dict):
        return json.dumps(args, ensure_ascii=False), None
    if isinstance(args, str):
        text = args.strip()
        if not text:
            return "{}", "empty string"
        parsed = _NOTHING
        strict_ok = True
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            strict_ok = False
            # Same leniency as _iter_tool_blocks: a string argument holding a
            # file body can contain a literal newline. It is re-serialised
            # below because the original text would reach the client with the
            # newline still unescaped, i.e. as invalid JSON.
            try:
                parsed = json.loads(text, strict=False)
            except (json.JSONDecodeError, ValueError):
                parsed = _NOTHING
        if isinstance(parsed, dict):
            if strict_ok:
                return text, None
            return json.dumps(parsed, ensure_ascii=False), None
        # Models occasionally hand back a Python literal instead of JSON.
        try:
            recovered = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            recovered = _NOTHING
        if isinstance(recovered, dict):
            try:
                return json.dumps(recovered, ensure_ascii=False), None
            except (TypeError, ValueError):
                # recovered contains non-JSON-serializable types (set, tuple,
                # bytes, etc.). Convert to a best-effort JSON-safe dict.
                def json_safe(obj):
                    if isinstance(obj, dict):
                        return {str(k): json_safe(v) for k, v in obj.items()}
                    if isinstance(obj, (list, tuple, set)):
                        return [json_safe(v) for v in obj]
                    if isinstance(obj, (str, int, float, bool)) or obj is None:
                        return obj
                    return str(obj)
                return json.dumps(json_safe(recovered), ensure_ascii=False), None
        return "{}", "not a JSON object"
    if args is None:
        return "{}", "missing"
    if isinstance(args, (list, int, float, bool)):
        return json.dumps(args, ensure_ascii=False), None
    return "{}", f"unsupported type {type(args).__name__}"


def _arguments_dict(data: dict, props=None) -> dict:
    """``extract_arguments`` for callers that need a JSON object, always."""
    args = extract_arguments(data, props)
    if isinstance(args, dict):
        return args
    text, _ = _arguments_json(args)
    try:
        parsed = json.loads(text, strict=False)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    return parsed if isinstance(parsed, dict) else {}


def _infer_tool_name(data: dict, tool_schemas) -> str:
    """Recover the tool name when the model leaves it out.

    The payload's parameter keys are matched against the declared schemas and a
    name is returned only when exactly one tool declares all of them -- an
    ambiguous shape is dropped rather than routed to the wrong tool.
    """
    if not tool_schemas:
        return None
    keys = set(_arguments_dict(data))
    if not keys:
        return None
    matches = [name for name, props in tool_schemas.items()
               if props and keys <= props]
    return matches[0] if len(matches) == 1 else None


# Conversation markers the model must never write itself. When the history
# format teaches them (``[Assistant]: ...`` / ``[Tool result for ...]``), a
# weak model copies the shape and fabricates results instead of calling.
_FABRICATED_CUT_RE = re.compile(
    r"^(?:\[Tool result for|\[System instruction\]:|\[Assistant\]:)",
    re.MULTILINE,
)


def _fence_spans(text: str) -> list:
    """``(start, end)`` spans of fenced code blocks in ``text``."""
    spans = []
    pos = 0
    while True:
        open_at = text.find("```", pos)
        if open_at == -1:
            return spans
        close_at = text.find("```", open_at + 3)
        if close_at == -1:
            spans.append((open_at, len(text)))
            return spans
        spans.append((open_at, close_at + 3))
        pos = close_at + 3


def _strip_fabricated(text: str, has_calls: bool) -> str:
    """Remove fabricated conversation markers from parsed model output.

    ``has_calls`` documents the companion rule enforced by the caller: when
    calls were accepted the tail after the last block is never appended to the
    clean text, so anything the model wrote after its block (including a
    self-written "result") cannot survive. What this function itself does,
    with or without calls:

    * a leading ``[Assistant]:`` prefix (position 0) is the history format
      leaking into the answer, not content -- strip it plus following space;
    * cut at the first line-start ``[Tool result for`` /
      ``[System instruction]:`` / ``[Assistant]:`` -- from there on the model
      is continuing the conversation by itself.

    Markers inside fenced code blocks are literal text the user asked about
    (e.g. quoting this proxy's own prompt format) and never trigger a cut.
    """
    if text.startswith("[Assistant]:"):
        text = text[len("[Assistant]:"):].lstrip()
    spans = _fence_spans(text)

    def _inside(pos: int) -> bool:
        return any(s <= pos < e for s, e in spans)

    for m in _FABRICATED_CUT_RE.finditer(text):
        if not _inside(m.start()):
            text = text[:m.start()]
            break
    return text


def _normalize_tool_name(name, allowed) -> str:
    """Resolve a model-written tool name against the declared ``allowed`` set.

    Models prefix (``functions.read``, ``default_api.read``) and vary case
    (``Read``); all of those used to be dropped as undeclared. Strip repeated
    ``functions.`` / ``default_api.`` prefixes, then adopt an allowed name
    only when exactly one candidate matches case-insensitively or by final
    dotted segment -- an ambiguous shape is still dropped rather than routed
    to the wrong tool. Returns None when nothing resolves.
    """
    if not isinstance(name, str) or not name:
        return None
    if allowed is None:
        stripped = name
        while True:
            lowered = stripped.lower()
            if lowered.startswith("functions.") or lowered.startswith("default_api."):
                stripped = stripped.split(".", 1)[1]
                if not stripped:
                    return None
            else:
                break
        return stripped or None
    if name in allowed:
        return name
    stripped = name
    while True:
        lowered = stripped.lower()
        if lowered.startswith("functions.") or lowered.startswith("default_api."):
            stripped = stripped.split(".", 1)[1]
            if not stripped:
                return None
        else:
            break
    if stripped in allowed:
        return stripped
    candidates = {a for a in allowed if a.lower() == stripped.lower()}
    if not candidates and "." in stripped:
        segment = stripped.split(".")[-1].lower()
        candidates = {a for a in allowed
                      if a.split(".")[-1].lower() == segment}
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _recover_python_payload(body):
    """Read a non-JSON fence body as a Python call or dict literal.

    Returns ``(data, reason)``: a dict payload and ``""`` on success, else
    ``(_NOTHING, reason)``. Only for fence kinds other than ``json`` -- a
    ``json`` fence that does not parse is prose, not a call attempt.
    ``read(path="x")`` yields ``{"name": "read", "arguments": {"path": "x"}}``;
    anything with positional arguments cannot be mapped to parameters and is
    dropped with its own reason.
    """
    if not isinstance(body, str):
        return _NOTHING, "body is not valid JSON"
    snippet = body.strip().strip("`").strip()
    if not snippet:
        return _NOTHING, "body is not valid JSON"
    try:
        expr = ast.parse(snippet, mode="eval").body
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        expr = None
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name):
            fname = func.id
        elif isinstance(func, ast.Attribute):
            fname = func.attr
        else:
            return _NOTHING, "body is not valid JSON"
        if expr.args:
            return _NOTHING, "python-style call with positional arguments"
        if any(kw.arg is None for kw in expr.keywords):
            return _NOTHING, "body is not valid JSON"
        try:
            kwargs = {kw.arg: ast.literal_eval(kw.value)
                      for kw in expr.keywords}
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return _NOTHING, "body is not valid JSON"
        return {"name": fname, "arguments": kwargs}, ""
    if snippet.startswith("{"):
        try:
            value = ast.literal_eval(snippet)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            return _NOTHING, "body is not valid JSON"
        if isinstance(value, dict):
            return value, ""
    return _NOTHING, "body is not valid JSON"
# for it routinely, but it is validated far more strictly below: an ordinary
# JSON example in prose must never be mistaken for a real call.
_FENCE_OPEN = re.compile(r'```(tool_call|function_call|tool_code|json)'
                         r'(?![A-Za-z0-9_])[ \t]*\n?')
_FENCE_CLOSE = re.compile(r'\s*```')


def _iter_tool_blocks(text: str):
    """Yield ``(start, end, data, kind, body)`` for each tool-call-shaped block.

    The payload is located with ``JSONDecoder.raw_decode`` starting right after
    the opening fence, so a ``` that appears *inside* a string argument (the
    normal shape of a ``write`` call whose content is Markdown or code)
    terminates neither the payload nor the block. A non-greedy regex instead
    stops at the very first fence, cuts the JSON in half, and the call is lost.

    ``data`` is ``_NOTHING`` for a block that declares a tool-call fence but
    whose body is not valid JSON; it is yielded anyway so the caller can log the
    drop rather than silently leaving it in the text. ``body`` is the raw
    payload text between the fences, for the AST recovery of Python-style
    calls (``read(path="x")``) that never were JSON in the first place.

    The decoder runs non-strict: ``write``/``edit`` payloads carry the body of
    a file, and a model that pastes a multi-line string writes the newline
    itself instead of escaping it. Strict JSON rejects that, so the whole call
    would be reported as "not valid JSON" -- the tool call silently becomes
    prose. ``json.dumps`` re-escapes the string on the way out, so what is
    handed to the client is well-formed again.
    """
    dec = json.JSONDecoder(strict=False)
    pos = 0
    while True:
        m = _FENCE_OPEN.search(text, pos)
        if not m:
            return
        kind = m.group(1)
        j = m.end()
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        try:
            data, end = dec.raw_decode(text, j)
        except ValueError:
            if kind == "json":
                # Not a call attempt, just JSON prose we cannot read: skip it
                # without a log line so ordinary answers stay quiet.
                pos = m.end()
                continue
            # Bound the reported body by the closing fence so the log shows what
            # the model actually wrote.
            close = text.find("```", m.end())
            span_end = close if close != -1 else m.end()
            body = text[j:close] if close != -1 else text[j:]
            yield m.start(), (close + 3 if close != -1 else m.end()), _NOTHING, kind, body
            pos = span_end if close != -1 else m.end()
            continue
        c = _FENCE_CLOSE.match(text, end)
        body = text[j:end]
        if c:
            end = c.end()
        yield m.start(), end, data, kind, body
        pos = end


def _is_declared_call(data, allowed_names, tool_schemas) -> bool:
    """True only for a ```json block that is unmistakably a declared tool call.

    Requires a name the client actually declared *and* a recognisable argument
    payload -- either one of the ``arguments``/``args``/``input`` aliases or
    flattened parameter keys that all belong to that tool's schema. Anything
    looser would swallow JSON examples the model quotes while explaining
    something to the user.
    """
    if not isinstance(data, dict) or not allowed_names:
        return False
    name = data.get("name")
    if name not in allowed_names:
        return False
    if any(k in data for k in _ARGUMENT_KEYS):
        return True
    keys = set(data) - _CALL_META_KEYS
    props = (tool_schemas or {}).get(name)
    return bool(keys) and bool(props) and keys <= props


def parse_tool_calls(text: str, allowed_names=None, tool_schemas=None) -> tuple:
    """Extract tool_call blocks. Returns (clean_text, tool_calls_list, dropped).

    ``allowed_names`` optionally restricts accepted function names.
    ``tool_schemas`` maps each declared name to its parameter names; when the
    model omits ``name`` a call is recovered only if its parameters point at
    exactly one declared tool, and a flattened call keeps a meta-looking key
    (``description``, say) only when the resolved tool's schema declares it.

    ``dropped`` is a list of human-readable reasons, one per block that looked
    like a call attempt but could not be used (undeclared name, unparsable
    body, ...). Blocks that fail are left in the text so that no content is
    silently lost; the caller retries with the reasons when nothing valid
    came out of the turn.
    """
    from gemini_web2api.gemini import log

    tool_calls = []
    dropped = []
    clean_parts = []
    last_end = 0
    for start, end, data, kind, body in _iter_tool_blocks(text):
        if kind == "json" and not _is_declared_call(data, allowed_names, tool_schemas):
            # Ordinary JSON in prose: leave it untouched and do not log it.
            continue
        if kind in ("tool_call", "function_call", "tool_code"):
            # A fence quoted mid-sentence ("e.g. ```tool_call ...") or pasted
            # from a file is an explanation, not a call. Only fences that
            # start their line (modulo whitespace) execute.
            line_start = text.rfind("\n", 0, start) + 1
            if text[line_start:start].strip():
                continue
        reason = ""
        if data is _NOTHING and kind != "json":
            # A fence that declares a call but holds no JSON: maybe a
            # Python-style call (``read(path="x")``) or a Python dict.
            data, reason = _recover_python_payload(body)
        parsed = None
        if isinstance(data, dict):
            name = data.get("name")
            if not name:
                name = _infer_tool_name(data, tool_schemas)
                if name:
                    log(f"Inferred missing tool name '{name}' from its parameters")
            if not name:
                reason = "no tool name"
            else:
                resolved = _normalize_tool_name(name, allowed_names)
                if allowed_names is not None and resolved is None:
                    valid = ", ".join(sorted(allowed_names))
                    reason = f"undeclared tool '{name}' (valid: {valid})"
                else:
                    if resolved is not None:
                        name = resolved
                    props = (tool_schemas or {}).get(name)
                    args, degraded = _arguments_json(
                        extract_arguments(data, props))
                    if degraded:
                        log(f"tool_call '{name}': arguments unusable ({degraded}), "
                            "sending an empty object so the client can report it")
                    if _is_placeholder_args(args):
                        reason = (f"tool_call '{name}' copied the EXAMPLE_ "
                                  "placeholder from the prompt -- fill in real values")
                    else:
                        parsed = {
                            "id": f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
        elif not reason:
            reason = ("body is not valid JSON" if data is _NOTHING
                      else "unrecognised payload")
        if parsed is None:
            dropped.append(reason or "unrecognised payload")
            log(f"Dropping tool_call block: {reason}: {text[start:end][:300]}")
            continue
        clean_parts.append(text[last_end:start])
        last_end = end
        tool_calls.append(parsed)
    if tool_calls:
        # The two-state rule: a turn either calls or answers. Anything after
        # the last block -- prose, and in particular a self-written
        # "[Tool result ...]" -- is never part of the answer.
        clean = "".join(clean_parts)
    else:
        clean_parts.append(text[last_end:])
        clean = "".join(clean_parts)
    clean = _strip_fabricated(clean, bool(tool_calls)).strip()
    return clean, tool_calls, dropped


def looks_like_tool_call(text: str) -> bool:
    """Whether ``text`` holds a block ``parse_tool_calls`` would actually emit.

    One response carries its answer and its tool call as separate texts, so a
    caller that picks between them by length can let a summary win over a call
    the model really made -- the client then receives prose claiming a tool
    was used and no call to run. This is the tie-breaker for that choice.

    Only the canonical shape counts. A fenced JSON example that merely happens
    to carry a top-level ``name`` is not a call, so preferring candidates that
    pass here can never swap the answer for a snippet quoted in prose: anything
    rejected simply stays out of the running and the caller falls back to
    comparing lengths as before.
    """
    for _start, _end, data, kind, _body in _iter_tool_blocks(text):
        if not isinstance(data, dict) or not data.get("name"):
            continue
        # ``{"name": ..., "arguments": ...}`` is a call under any fence; a
        # bare ``json`` fence needs the argument wrapper, because ``{"name":
        # "Alice"}`` in an example is exactly the prose this must not catch.
        if kind != "json" or any(k in data for k in _ARGUMENT_KEYS):
            return True
    return False


# Gemini's own generation step failing produces this canned sentence rather
# than an answer. It reaches the client as a normal 200, so the caller reads it
# as "the model replied" and stops -- when in fact nothing was generated and a
# single retry almost always succeeds.
#
# Both detectors below match against ``_fold_diacritics`` output, so their
# patterns are written in plain ASCII and recognise Vietnamese typed with or
# without accents.
_UPSTREAM_ERROR_RE = re.compile(
    r"^\s*(?:i encountered an error doing what you asked\.?\s*"
    r"could you try again\?*"
    r"|sorry, something went wrong\.?"
    r"|da co loi xay ra\.?"
    r"|i can'?t use [\w .'\-]+? because required [\w .'\-]+? (?:settings|permissions) are off\.?"
    r"(?:\s*turn on these settings to continue\.?)?)\s*$",
    re.IGNORECASE | re.DOTALL,
)

# Phrases that mean "I am about to act" or "I cannot reach that" --
# i.e. the model announced an action it never performed because it never
# emitted the tool call. Covers both read verbs (read/check/look/...) and
# implement verbs (write/create/update/run/...) in English and Vietnamese:
# without the latter an agent stops after one step ("I'll create the file"
# with no block reads as a finished answer).
_MISSED_TOOL_RE = re.compile(
    r"(khong\s+the\s+truy\s+cap|khong\s+co\s+quyen|khong\s+duoc\s+truy\s+cap"
    r"|khong\s+tim\s+thay|khong\s+the\s+doc"
    r"|cannot\s+access|can'?t\s+access"
    r"|don'?t\s+have\s+access|do\s+not\s+have\s+access"
    r"|no\s+(?:file\s+|tool\s+)?access"
    r"|not\s+(?:permitted|allowed)\s+to\s+read"
    r"|de\s+minh\s+(?:doc|kiem\s+tra|viet|tao|sua|chay|cap\s+nhat|thuc\s+hien|xem|mo)"
    r"|minh\s+se\s+(?:doc|viet|tao|sua|chay|cap\s+nhat|thuc\s+hien|kiem\s+tra)"
    r"|toi\s+se\s+(?:doc|viet|tao|sua|chay|cap\s+nhat|thuc\s+hien|kiem\s+tra)"
    r"|let'?s\s+(?:read|check|look|see|examine|inspect|write|create|update|edit|run|implement|fix|build)"
    r"|let\s+us\s+(?:read|check|look|see|examine|inspect|write|create|update|edit|run|implement|fix|build)"
    r"|let\s+me\s+(?:read|check|look|see|examine|inspect|write|create|update|edit|run|execute|implement|fix|build|apply|test)"
    r"|i(?:'ll|\s+will)\s+(?:read|check|look|see|examine|inspect|write|create|update|edit|modify|fix|run|execute|implement|build|apply|test|continue))",
    re.IGNORECASE,
)

# Longer guard for verbose models that explain before emitting tool_call.
# A long plan summary (>600 chars) followed by a promise to act is still a
# stalled turn, not a real answer.
_MAX_MISSED_TOOL_CHARS = 1200


def _fold_diacritics(text: str) -> str:
    """Strip accents so patterns match Vietnamese typed with or without them.

    ``đ``/``Đ`` have no canonical decomposition, so they are folded explicitly.
    """
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.replace("đ", "d").replace("Đ", "D")


def is_required_tool_choice(tool_choice) -> bool:
    """True when the client demands a tool call rather than allowing one.

    Chat Completions nests the target under ``function``; the Responses API
    drops that nesting and names it directly, so both shapes have to count --
    recognising only the nested one silently disabled the required-tool path
    for every Responses client.
    """
    if tool_choice == "required":
        return True
    if isinstance(tool_choice, dict):
        return bool(tool_choice.get("function") or tool_choice.get("name"))
    return False




def looks_like_upstream_error(text: str) -> bool:
    """True when the response is the upstream's canned failure, not an answer."""
    if not text:
        return False
    return bool(_UPSTREAM_ERROR_RE.match(_fold_diacritics(text)))


_CANNED_FAILURE_RES = (
    re.compile(r"sorry,?\s+something\s+went\s+wrong", re.IGNORECASE),
    re.compile(r"please\s+try\s+your\s+request\s+again", re.IGNORECASE),
    re.compile(r"\bi\s+seem\s+to\s+be\s+encountering\s+an\s+error\b", re.IGNORECASE),
    re.compile(r"i\s+can'?t\s+use\s+[a-z][a-z .'\-]{0,60}?\s+because\s+"
               r"required\s+.{0,80}?(settings|permissions)\s+are\s+off",
               re.IGNORECASE),
    re.compile(r"turn\s+on\s+these\s+settings\s+to\s+continue",
               re.IGNORECASE),
)


def looks_like_canned_failure(text: str) -> bool:
    """True for ANY upstream canned sentence, at any length.

    ``looks_like_upstream_error`` is anchored (whole-string) and
    a verbose model that
    pads "Sorry, something went wrong..." or the Workspace-settings refusal
    with prose escapes both and is handed to the client as a finished answer --
    observed live as turns that stall ~60 s and then "stop by themselves".
    These sentences are machine-generated upstream failures, never part of a
    genuine reply, so substring matching is safe here (the patterns are too
    specific to occur inside real content).
    """
    if not text or len(text) > 4000:
        return False
    folded = _fold_diacritics(text)
    return looks_like_upstream_error(text) or any(r.search(folded)
                                                  for r in _CANNED_FAILURE_RES)


def looks_like_missed_tool_call(text: str) -> bool:
    """True when a response *intended* to use a tool but never emitted a block.

    All three conditions must hold: the text announces or refuses an action,
    it is short enough to be a stalled first turn rather than a real answer,
    and it carries no tool fence (a fence means the model did try). Returning
    True costs one extra upstream call; returning True for a genuine answer
    would make every short reply do double duty, hence the length guard.
    """
    if not text or len(text) >= _MAX_MISSED_TOOL_CHARS:
        return False
    if "```tool_call" in text or "```function_call" in text:
        return False
    folded = _fold_diacritics(text)
    if folded.rstrip().endswith(":"):
        return True
    return bool(_MISSED_TOOL_RE.search(folded))


# Phrases where the model hands the work back to the user instead of acting:
# a how-to for code it already read. A verbatim bug quote (the prompt's own
# "quote the exact lines" rule) has proposed code too, so the handoff phrase
# is required -- a quote that explains a bug never tells the user to apply it.
_DESCRIBED_EDIT_RE = re.compile(
    r"\b(?:you\s+can|you\s+need\s+to|you\s+should|here\s+is\s+how\s+you\s+can|"
    r"follow\s+these\s+steps|update\s+\S+\s+as\s+follows|make\s+the\s+"
    r"following\s+changes?|to\s+do\s+this,?|run\s+\S+\s+to\s+verify|"
    r"since\s+tool\s+calls?\s+are\s+no\s+longer\s+available|"
    r"unable\s+to\s+execute\s+tool|i\s+would\s+be\s+happy\s+to\s+help\s+you\s+"
    r"(?:identify|implement|fix|update))\b",
    re.IGNORECASE,
)
_DIFF_MARKER_RE = re.compile(
    r"(?:^|\n)(?:\+\+\+|---|@@\s)", re.MULTILINE,
)


def looks_like_described_edit(text: str) -> bool:
    """True when the reply describes an edit instead of emitting the call.

    Both halves must hold: proposed code (a diff hunk or a fenced code block)
    AND a handoff phrase telling the user to apply it. Either half alone is
    legitimate -- a quoted bug snippet, or prose guidance with no code.
    """
    if not text or "```tool_call" in text or "```function_call" in text:
        return False
    has_code = bool(_DIFF_MARKER_RE.search(text)
                    or re.search(r"```(?:python|javascript|js|json|diff|bash|"
                                 r"typescript|ts)\b", text, re.IGNORECASE))
    if not has_code:
        return False
    return bool(_DESCRIBED_EDIT_RE.search(_fold_diacritics(text)))


# File-shaped tokens a sentence can point at: `README.md`, `server.py`,
# `src/server.py`, `gemini-web2api.log`. The optional ``(dir/)*`` prefix
# keeps project paths intact so callers can detect the project root; without
# it a "detect root via f.split('/')" block can never fire.
_FILE_REF_RE = re.compile(
    r"(?<![\w.-])(?:[\w.-]+/)*[\w][\w.-]*\.(?:"
    r"md|py|pyi|js|jsx|ts|tsx|json|jsonc|txt|ya?ml|toml|cfg|conf|ini|env|"
    r"java|kt|c|cc|cpp|h|hpp|go|rs|rb|php|html|css|scss|less|sql|sh|bash|ps1|"
    r"xml|csv|lock|log|rst|bat|cmd|vue|svelte|proto|gradle|properties"
    r")(?![\w-])",
    re.IGNORECASE,
)

# Bare `base.ext` tokens with these extensions are far more often member
# access (`self.cmd`, `process.env`, `math.log(`, `docs.rs`, `args.go`) than
# real files. They only count when path-like (`a/b.log`) or quoted/backticked.
_RISKY_BARE_EXTS = frozenset({"cmd", "env", "log", "rs", "go"})

# Names that end in a source extension but are runtimes, frameworks or
# libraries rather than files. `Node.js` and friends would otherwise look like
# an unread file for every turn that mentions them -- no tool call ever
# "opens" them, so the request would be retried until the conversation ends.
_NON_FILE_NAMES = frozenset({
    "node.js", "node.mjs", "node.cjs", "next.js", "nuxt.js", "vue.js",
    "react.js", "react-dom.js", "express.js", "jquery.js", "d3.js",
    "three.js", "ember.js", "backbone.js", "socket.js", "deno.js",
    "threading.lock",
})

def _canonical_args(arguments) -> str:
    """Normalise a call's arguments so repeats compare equal.

    Argument key order is a serialisation accident, not a different call:
    non-strict ``json.loads`` plus ``sort_keys`` dump. Anything that is not
    JSON falls back to the stripped raw text.
    """
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments, strict=False)
        except (json.JSONDecodeError, ValueError):
            return arguments.strip()
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    try:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(arguments)


_TODO_MUT_SKIP_RE = re.compile(
    r"(?i)^(?:todo_?(?:write|read|update|list|add)|write_?todos?|read_?todos?"
    r"|task_?(?:create|update|list|get))$"
)


def _is_mutating_call(name, arguments) -> bool:
    """True when a completed tool call changed the world earlier results describe.

    ``edit``/``write``/``apply_patch``-style names always mutate. Command
    runners (``bash``/``shell``/``exec``/``run``) are verification when their
    arguments hold a test command -- pytest/unittest/npm test output does not
    change files, so a green re-run must not wipe the edit's repeat-guard
    entry. Any other command (rm, sed -i, git commit, ...) still counts as
    mutating.
    """
    if not isinstance(name, str) or _TODO_MUT_SKIP_RE.match(name.strip()) or not _MUTATING_TOOL_RE.search(name):
        return False
    if isinstance(arguments, dict):
        blob = json.dumps(arguments, ensure_ascii=False)
    elif isinstance(arguments, str):
        blob = arguments
    else:
        blob = ""
    return not (blob and _TEST_CMD_RE.search(blob))


def _call_file_arg(raw_args):
    if isinstance(raw_args, dict):
        d = raw_args
    elif isinstance(raw_args, str) and raw_args.strip():
        try:
            d = json.loads(raw_args, strict=False)
        except Exception:
            d = {}
    else:
        d = {}
    if isinstance(d, dict):
        for k in ("path", "filePath", "file_path", "file", "filename", "file_name", "target", "target_file"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _repeatable_calls(messages) -> set:
    """``{(name, canonical_args)}`` this conversation already has results for.

    Every assistant ``tool_calls`` entry paired with a later ``role: "tool"``
    result carrying the same ``tool_call_id``. The retry loop uses this to
    recognise a model that keeps emitting a call it already got the answer
    to, instead of executing the same call again.

    A mutating tool (edit/write/...) changes the world the earlier
    results described: after one completes, every earlier entry is dropped.
    Re-running the same ``pytest`` or re-reading a just-edited file is then
    progress, not a loop. The mutating call itself stays in the set:
    re-emitting the exact edit that already applied is a wasted round-trip
    (its oldString no longer matches), while re-reads/re-runs are new work.
    """
    calls_by_id = {}
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            tcid = tc.get("id")
            if tcid and isinstance(fn, dict):
                calls_by_id[tcid] = (fn.get("name"),
                                     _canonical_args(fn.get("arguments")),
                                     fn.get("arguments"))
    done = set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            continue
        if msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id", "")
        key = calls_by_id.get(tcid)
        name = key[0] if key else (msg.get("name") or "")
        raw_args = key[2] if key else ""
        if key is not None:
            done.add(key[:2])
        if _is_mutating_call(name, raw_args):
            mut_path = _call_file_arg(raw_args) if _EDIT_TOOL_RE.search(name or "") else None
            if mut_path:
                kept = set()
                for d_name, d_cargs in done:
                    if _EDIT_TOOL_RE.search(d_name or ""):
                        kept.add((d_name, d_cargs))
                    elif re.search(r"^(?:read|view|cat|open)$", str(d_name or ""), re.I):
                        r_path = _call_file_arg(d_cargs)
                        if r_path and not _path_seen(r_path, [mut_path]):
                            kept.add((d_name, d_cargs))
                done = kept
            else:
                done.clear()
            # Keep the just-applied mutating call: repeating the exact same
            # edit is a loop, re-reading or re-testing after it is not.
            if key is not None:
                done.add(key[:2])
    return done


def _google_repeatable(contents) -> set:
    """Chat-path :func:`_repeatable_calls` for Google native ``contents``.

    Model parts carrying ``functionCall`` are paired with later
    ``functionResponse`` parts by name (falling back to oldest-first when the
    name matches nothing): each response completes one outstanding call, and
    completed calls are what the retry loop must not emit again. A completed
    mutating call clears earlier entries: state changed, so repeats are new
    work.
    """
    pending = []
    done = set()
    for content in contents or []:
        if not isinstance(content, dict):
            continue
        for part in content.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("functionCall"), dict):
                fc = part["functionCall"]
                pending.append((fc.get("name"),
                                _canonical_args(fc.get("args")),
                                fc.get("args")))
            elif isinstance(part.get("functionResponse"), dict):
                fr = part["functionResponse"]
                idx = next((i for i, (pending_name, _a, _r) in enumerate(pending)
                            if pending_name == fr.get("name")), None)
                if idx is None and pending:
                    idx = 0
                if idx is not None:
                    key = pending.pop(idx)
                    done.add(key[:2])
                    if _is_mutating_call(key[0], key[2]):
                        done.clear()
                        # Keep the mutating call itself out: re-emitting the
                        # exact edit is a loop, re-reading after it is not.
                        done.add(key[:2])
    return done


# Tool names that change state (files, processes, environment). A repeatable
# entry from before one of these completed is stale: the world changed.
_MUTATING_TOOL_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:write|edit|create|update|delete|remove|mkdir|rmdir|"
    r"bash|shell|exec|run|apply_patch|apply|patch|append|save|move|copy|"
    r"touch|chmod|truncate)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


# "dùng tool", "đọc file", "review source", "use the tool", ... -- matched
# against the folded text with whitespace runs collapsed to single spaces
# (never deleted: deleting them is what glued "because"+"tool" into a phrase
# containing "use"+"tool"). Every alternative starts at a word boundary with
# real whitespace between components, so intra-word lookalikes ("because
# tool", "demo file", "thread files", "review file") stay silent while the
# real demands still fire.
_TOOL_DEMAND_RE = re.compile(
    r"\b(?:su\s+|hay\s+|phai\s+|van\s+phai\s+|bat\s+buoc\s+|co\s+)?dung\s+"
    r"(?:la\s+)?(?:tool|tools|cong\s+cu|cac\s+cong\s+cu)"
    r"|\b(?:doc|kiem\s+tra|xem|mo)(?:\s+rai)?\s+"
    r"(?:file|files|tep|thu\s+muc|cac\s+file|cac\s+tep)"
    r"|\b(?:review|kiem\s+tra|doc)(?:\s+la)?\s+"
    r"(?:source|code|ma\s+nguon|cac\s+file)"
    r"|\b(?:use|call|run|invoke)\s+(?:the\s+)?(?:tool|tools)"
    r"|\b(?:read|open|view|check|inspect)\s+(?:the\s+)?(?:file|files)",
    re.IGNORECASE,
)

# A request to review/audit/inspect source code (matched against folded,
# whitespace-collapsed text, like _TOOL_DEMAND_RE). Kept separate because a
# listing-only turn (glob/list) must still count as owed here, while every
# other demand keeps the old "any call ends the check" rule.
#
# Verbs are split: strong verbs (review/audit/evaluate/...) combine with any
# noun, including plan/document/design/spec. Weak verbs (check/find/read/...)
# only combine with code nouns -- "read the plan then implement" is an
# implement turn, not a source review, and must not be forced to read modules.
#
# The broad noun set also covers the system under review when the user names
# it instead of the code ("review how this proxy loads the session cookie"):
# with a strong verb the request is unambiguously a codebase inspection, so
# proxy/server/service/... count. Weak verbs keep the narrow code nouns, so
# "check the server status" stays silent.
_SOURCE_REVIEW_VERB_RE = re.compile(
    r"\b(?:review|audit|inspect|examine|check|find|kiem\s+tra|soat|tim(?!\s+hieu\b)|xem\s+xet|doc|danh\s+gia|danhgia)\b",
    re.IGNORECASE,
)
_SOURCE_STRONG_VERB_RE = re.compile(
    r"\b(?:review|audit|inspect|examine|soat|danh\s+gia|danhgia|evaluate|assess)\b",
    re.IGNORECASE,
)
_SOURCE_CODE_NOUN_RE = re.compile(
    r"\b(?:source|code|codebase|ma\s+nguon|implementation|repository|repo)\b",
    re.IGNORECASE,
)
_SOURCE_NOUN_RE = re.compile(
    r"\b(?:source|code|codebase|ma\s+nguon|implementation|repository|repo|ke\s+hoach|kehoach|plan|document|tai\s+lieu|tai-lieu|thiet\s+ke|thietke|design|specification|spec|proxy|server|service|app|module|package|project)\b",
    re.IGNORECASE,
)


def _is_source_review_s1(folded: str) -> bool:
    """True when folded text asks for a review of source/code."""
    if _SOURCE_STRONG_VERB_RE.search(folded) and _SOURCE_NOUN_RE.search(folded):
        return True
    return bool(_SOURCE_REVIEW_VERB_RE.search(folded)
                and _SOURCE_CODE_NOUN_RE.search(folded))


_READ_THEN_CHANGE_RE = re.compile(
    r"\b(?:thuc\s+thi|trien\s+khai|implement|sua|cap\s+nhat|them|update|modify|edit|patch)\b",
    re.IGNORECASE,
)


def _is_file_review_s1(folded: str, scannable: str) -> bool:
    """True when folded text aims a review verb at a named file.

    "Tiep tuc review server.py di." carries no source noun, so
    :func:`_is_source_review` misses it -- yet answering it from a glob
    listing alone is the same fabrication. A review verb plus a file-shaped
    token counts as a review of that file ("review file" with no file named
    stays silent; runtime names like ``Node.js`` never count as files).
    """
    if not _SOURCE_REVIEW_VERB_RE.search(folded):
        return False
    # "Hãy đọc file plan.md và implement/cập nhật vào X.py" is a CHANGE turn, not a FILE_REVIEW
    if (not _SOURCE_STRONG_VERB_RE.search(folded)
            and re.search(r"\b(?:doc|read)\b", folded, re.IGNORECASE)
            and _READ_THEN_CHANGE_RE.search(folded)
            and not re.search(r"\b(?:review|audit|inspect|soat|danh\s+gia|kiem\s+tra\s+loi|tim\s+loi|tim\s+bug)\b", folded, re.IGNORECASE)):
        return False
    return any(f for f in _files_in(scannable)
               if f.lower() not in _NON_FILE_NAMES)


# Tool names that read content (not just list names). A grep/search with no
# path in its args still read something, so the name alone ends the check.
_READER_TOOL_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:read|cat|view|open|show|grep|ripgrep|search|head|tail|less|sed|fetch)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)

# A glob-only turn answered from filenames alone is the failure this guards:
# the model listed names but never opened one.
SOURCE_REVIEW_OWED = ("source review requested but no file content was read "
                      "(only listed)")


def is_read_retry_reason(reason) -> bool:
    """True when a retry reason means "no file content was read yet"."""
    return bool(reason) and ("never read" in reason
                             or reason == SOURCE_REVIEW_OWED)


def _last_user_index(messages) -> int:
    """Index of the last ``role == "user"`` dict, else -1."""
    idx = -1
    for i, msg in enumerate(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            idx = i
    return idx


def _turn_tool_calls(messages, start_idx) -> list:
    """Every ``tool_calls`` dict in messages after ``start_idx``."""
    calls = []
    for msg in (messages or [])[start_idx + 1:]:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                calls.append(tc)
    return calls


def _tool_arguments_dict(fn) -> dict:
    """Parse a ``tool_calls`` function payload's arguments to a dict or {}."""
    args = (fn or {}).get("arguments")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args, strict=False)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# Argument keys that carry a file path. Only these are scanned for evidence:
# scanning the whole serialised args is what counted `glob
# {"pattern": "**/hand3d.py"}` as "read hand3d.py" and `bash ls x.py` as a
# read. `pattern`/`command`/`query` deliberately excluded: a listing pattern
# or a shell one-liner is not content.
_PATH_ARG_KEYS = frozenset({
    "path", "filepath", "file_path", "file", "filename", "file_name",
    "target", "target_file", "dirpath", "directory", "folder",
})


def _is_reader_tool(name) -> bool:
    return isinstance(name, str) and bool(_READER_TOOL_RE.search(name))


# Tools that return whole-file content (not search snippets). Only these get
# line numbers + boundaries: numbering a grep hit would fabricate line
# numbers the file does not have.
_FILE_READER_TOOL_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:read|cat|view|open|show|head|tail|less)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def _is_file_reader_tool(name) -> bool:
    return isinstance(name, str) and bool(_FILE_READER_TOOL_RE.search(name))


def _format_file_body(path, body: str) -> str:
    """Number every line and wrap in boundaries so edits can anchor.

    A bare text blob gives the model no reference point: it cannot tell
    where one file ends and the next begins, nor which line a symbol sits
    on -- so patches land in the wrong method. Line numbers + explicit
    Start/End markers fix both.
    """
    label = path.strip() if isinstance(path, str) and path.strip() else "file"
    lines = body.split("\n")
    if _already_numbered(lines):
        # The client already numbered the lines: numbering again gives two
        # competing line-number columns and edits anchor on the wrong one.
        return f"--- Start {label} ---\n{body}\n--- End {label} ---"
    width = max(4, len(str(len(lines))))
    numbered = "\n".join(f"{i:>{width}} | {line}"
                         for i, line in enumerate(lines, 1))
    return f"--- Start {label} ---\n{numbered}\n--- End {label} ---"


def _called_file_path(tool_calls, tcid):
    """File path from the tool call with this id, else None.

    Reads ``path``-like argument keys only (never the whole serialised
    args): a ``glob {"pattern": "**/x.py"}`` is not a file that was read.
    """
    for tc in tool_calls or []:
        if not isinstance(tc, dict) or tc.get("id") != tcid:
            continue
        args = _tool_arguments_dict(tc.get("function") or {})
        for key in ("path", "filepath", "file_path", "file", "filename",
                    "file_name", "target", "target_file"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None
    return None


def _turn_result_ids(messages, start_idx) -> set:
    """``tool_call_id`` values with a ``role == "tool"`` result this turn."""
    ids = set()
    for msg in (messages or [])[start_idx + 1:]:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            tcid = msg.get("tool_call_id")
            if tcid:
                ids.add(tcid)
    return ids


def files_read_in_turn(messages) -> set:
    """Lowercased file names whose content this turn's tool calls opened.

    Only reader tools (``read``/``cat``/``view``/``grep``/...) count, only
    path-like argument keys are scanned, and a call without a paired tool
    result does not count: a ``glob`` pattern or an unanswered call is not
    evidence the model saw content.
    """
    start_idx = _last_user_index(messages)
    result_ids = _turn_result_ids(messages, start_idx)
    files = set()
    for tc in _turn_tool_calls(messages, start_idx):
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if not _is_reader_tool(name):
            continue
        if tc.get("id") and result_ids and tc.get("id") not in result_ids:
            continue
        args = _tool_arguments_dict(fn)
        if args:
            vals = [v for k, v in args.items()
                    if k in _PATH_ARG_KEYS and isinstance(v, str)]
            if vals:
                files |= {f.lower() for f in _files_in(*vals)}
                continue
            # Reader with no path key (grep query only): content was still
            # read, but no file can be named -- handled by _reads_this_turn.
            continue
        raw = fn.get("arguments")
        if isinstance(raw, str):
            found = {f.lower() for f in _files_in(raw)}
            if found:
                files |= found
    return files


def _reads_this_turn(messages) -> int:
    """Number of distinct content reads in this turn."""
    files = files_read_in_turn(messages)
    if files:
        return len(files)
    start_idx = _last_user_index(messages)
    result_ids = _turn_result_ids(messages, start_idx)
    for tc in _turn_tool_calls(messages, start_idx):
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if _is_reader_tool(name):
            if tc.get("id") and result_ids and tc.get("id") not in result_ids:
                continue
            return 1
    return 0


def _source_reads_this_turn(messages) -> int:
    """Number of distinct SOURCE CODE (.py) reads in this turn."""
    files = files_read_in_turn(messages)
    return len({f for f in files if f.endswith(".py")})


def review_demanded_s1(messages) -> bool:
    """True when the newest user turn asks for a source/code review."""
    last_user = None
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = _stringify_content(msg.get("content", ""))
    if not last_user:
        return False
    scannable = _strip_code_spans(last_user)
    folded = _fold_diacritics(scannable)
    folded = re.sub(r"\s+", " ", folded).strip()
    return bool(_is_source_review(folded)
                or _is_file_review(folded, scannable))


def declared_unread(messages) -> list:
    """Files the model named in this turn's prose but never read, else ``[]``.

    Ordered and unique: file-shaped tokens in assistant ``content`` since the
    last user turn (code spans blanked first, so pasted logs/code do not
    count; runtime names like ``Node.js`` never count), minus the files this
    turn's tool calls opened (compared case-insensitively).
    """
    start_idx = _last_user_index(messages)
    read_lower = files_read_in_turn(messages)
    seen = []
    for msg in (messages or [])[start_idx + 1:]:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for f in _files_in(_strip_code_spans(
                _stringify_content(msg.get("content", "")))):
            if f.lower() in _NON_FILE_NAMES or _path_seen(f, read_lower):
                continue
            if f not in seen:
                seen.append(f)
    return sorted(seen)


def fresh_declared_unread(text, messages) -> list:
    """Like :func:`declared_unread` but for one fresh reply string.

    The retry loop calls this on the reply it just received, before that
    reply is appended to ``messages``.
    """
    if not isinstance(text, str) or not text:
        return []
    read_lower = files_read_in_turn(messages)
    seen = []
    for f in _files_in(_strip_code_spans(text)):
        if f.lower() in _NON_FILE_NAMES or _path_seen(f, read_lower):
            continue
        if f not in seen:
            seen.append(f)
    return sorted(seen)


def listed_turn_files(messages) -> list:
    """File names from ``role == "tool"`` contents since the last user turn.

    Ordered and unique: the retry nudge names these so the model reads what
    it just listed instead of guessing. Bodies longer than ~2 KB (a just-read
    file's content) are skipped: scanning a full file body is what promoted
    incidental mentions (`config.json` quoted in code) into "listed files"
    and pushed ``required_reads`` to 2 on turns that listed nothing. Short
    listing outputs (glob/ls results) are unaffected.
    """
    start_idx = _last_user_index(messages)
    seen = []
    for msg in (messages or [])[start_idx + 1:]:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        body = _stringify_content(msg.get("content", ""))
        if len(body) > 2000:
            continue
        for f in _files_in(body):
            if f.lower() in _NON_FILE_NAMES:
                continue
            if f not in seen:
                seen.append(f)
    return seen


def _strip_fences(text: str) -> str:
    """Blank fenced code blocks (``` ... ```) but keep inline ``code`` spans."""
    if not isinstance(text, str) or not text:
        return ""
    blanked = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    return re.sub(r"```.*$", " ", blanked, flags=re.DOTALL)


def _strip_code_spans(text: str) -> str:
    """Remove fenced and inline code from ``text`` for demand/file scanning.

    A pasted log or code block can name files (``server.py``) and use tool
    words ("read the file" inside a comment) without the user asking for
    anything -- scanning the raw text turns every paste into a phantom "never
    read" retry. Paired fences are blanked; an unpaired opening fence blanks
    everything to the end (the paste was cut off, not prose).
    """
    if not isinstance(text, str) or not text:
        return ""
    blanked = _strip_fences(text)
    return re.sub(r"`[^`\n]+`", " ", blanked)


def _code_spans(text: str) -> list:
    """Inline ``code`` spans of ``text`` with fences blanked first.

    ``_strip_code_spans`` erases them, but an identifier the user wrapped in
    backticks ("fix the ``retry_delay`` handling") IS the target of the
    request -- losing it made every change that named its target in inline
    code look like vague prose.
    """
    if not isinstance(text, str) or not text:
        return []
    return re.findall(r"`([^`\n]+)`", _strip_fences(text))


def _files_in(*chunks) -> set:
    """File-shaped names mentioned anywhere in ``chunks`` (strings or JSON).

    Filters member access (`self.cmd`, `process.env`) and call expressions
    (`math.log(`): a token followed by ``(`` is code, not a file. Bare
    tokens with high-collision extensions (cmd/env/log/rs/go) need a path
    separator or quoting to count.
    """
    blob = " ".join(c for c in chunks if isinstance(c, str))
    out = set()
    for m in _FILE_REF_RE.finditer(blob):
        token = m.group(0)
        end = m.end()
        j = end
        while j < len(blob) and blob[j] in " \t":
            j += 1
        if j < len(blob) and blob[j] == "(":
            continue
        if "/" not in token and "\\" not in token:
            raw_ext = token.rsplit(".", 1)[-1] if "." in token else ""
            if raw_ext and raw_ext[0].isupper() and not raw_ext.isupper():
                continue
            ext = raw_ext.lower()
            if token.lower() in _NON_FILE_NAMES:
                continue
            if ext in _RISKY_BARE_EXTS:
                start = m.start()
                before = blob[start - 1] if start > 0 else ""
                after = blob[end] if end < len(blob) else ""
                if before not in ("`", "'", '"', "/", "\\") \
                        and after not in ("`", "'", '"'):
                    continue
        out.add(token)
    return out


def _conversation_called_files(messages) -> set:
    """Every file name a *reader* tool call in this conversation opened.

    Listing/writing tools do not count: a ``glob`` pattern that mentions a
    file is not evidence the model read it, and without this the `uncovered`
    check is disabled by the very discovery step it prescribes.
    """
    called = set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        for tc in (msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            if not _is_reader_tool(fn.get("name") or ""):
                continue
            args = _tool_arguments_dict(fn)
            if args:
                vals = [v for k, v in args.items()
                        if k in _PATH_ARG_KEYS and isinstance(v, str)]
                if vals:
                    called |= _files_in(*vals)
                    continue
            raw = fn.get("arguments")
            if isinstance(raw, str):
                called |= _files_in(raw)
            elif args:
                called |= _files_in(json.dumps(args, ensure_ascii=False))
    return called


# "create/write/generate/make a file ..." (EN + VI): files named in such a
# turn are to be *created*, not read. The `uncovered` branch must not demand
# reading a file that does not exist yet -- the retry nudge would otherwise
# tell the model to "read" it and the read fails.
_FILE_CREATE_RE = re.compile(
    r"\b(?:create|write|generate|make|add|tao|tao\s+moi|viet|tao\s+file|"
    r"tao\s+tep|ghi\s+file|thiet\s+ke\s+moi)\b",
    re.IGNORECASE,
)


_CREATE_CLAUSE_SPLIT_RE = re.compile(
    r"(?:\.(?=\s|$)|[!?;:\n]+|\b(?:sau\s+khi|sau\s+do|roi|tiep\s+theo|then|and\s+then|after\s+that)\b)",
    re.IGNORECASE,
)
_EXISTING_TARGET_PREP_RE = re.compile(
    r"\b(?:doc|read|open|xem|sua|edit|modify|update|refactor|patch|"
    r"implement\b[^.!?;:\n]{0,60}?\b(?:vao|into|in)|vao|into|in|from|tu|theo)\s+(?:file\s+|tep\s+)?$",
    re.IGNORECASE,
)
_PLAN_DOC_NAME_RE = re.compile(
    r"(?:^|[/\\_-])(?:plan|todo|checklist|roadmap|spec|design)\.(?:md|txt|rst)$",
    re.IGNORECASE,
)


def _created_files(scannable: str, folded: str) -> set:
    """File tokens exempt from the `uncovered` check (creation intent)."""
    if not _FILE_CREATE_RE.search(folded or ""):
        return set()
    out = set()
    clauses = _CREATE_CLAUSE_SPLIT_RE.split(scannable or "")
    for cl in clauses:
        cl_folded = _fold_diacritics(cl)
        if not _FILE_CREATE_RE.search(cl_folded):
            continue
        for f in _files_in(cl):
            fl = f.lower()
            if _PLAN_DOC_NAME_RE.search(fl):
                continue
            pos = cl_folded.lower().find(fl)
            if pos > 0 and _EXISTING_TARGET_PREP_RE.search(cl_folded[:pos]):
                continue
            out.add(fl)
    return out


# A request to CHANGE the code (fix/implement/update/...), matched against
# folded text like the review heuristics. This is the implement-side twin of
# `_is_source_review`: the failure it guards is the survey-stall, where the
# model globs/greps to locate the code and then answers from the snippets --
# describing the edit instead of reading the file and making it.
#
# A bare change verb is not enough ("double it" in a maths chat): it must aim
# at code -- a file token, a code/config noun, or an explicit act phrase
# ("make it so"). Creation verbs (create/write/generate) are excluded: a file
# that does not exist yet must not be demanded for reading.
_CHANGE_VERB_RE = re.compile(
    r"\b(?:fix|repair|implement|add|introduce|update|upgrade|change|modify|"
    r"refactor|remove|delete|drop|double|halve|increase|decrease|migrate|"
    r"sua|them|cap\s+nhat)\b",
    re.IGNORECASE,
)
_CHANGE_NOUN_RE = re.compile(
    r"\b(?:source|code\w*|implementation|repository|repo\w*|proxy|server|"
    r"service|app|module\w*|package|project|config\w*|setting\w*|option\w*|"
    r"endpoint\w*|rout\w*|test\w*|retr\w*|auth\w*|health\w*|bug\w*|"
    r"securit\w*|vulnerab\w*)\b",
    re.IGNORECASE,
)
_CHANGE_ACT_RE = re.compile(
    r"\b(?:make\s+it\s+so|make\s+it\s+work|get\s+it\s+done|do\s+it|"
    r"go\s+ahead|cu\s+lam|lam\s+di)\b",
    re.IGNORECASE,
)

# Tool names that act on the world (not just observe it). A change turn that
# emitted one of these has done the work; a turn with only glob/grep/read
# has surveyed but not acted.
_ACT_TOOL_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:edit|write|create|apply|patch|bash|shell|exec|run|"
    r"pytest|npm|make)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


# An inline-code target is a change target only when it reads like code
# (``retry_delay``, ``pkg/a.py``), not a whole sentence wrapped in backticks.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]*$")


def _looks_like_identifier(text: str) -> bool:
    return bool(_IDENT_RE.match((text or "").strip()))


def _is_change_request_s1(folded: str, scannable: str, raw: str = "") -> bool:
    """Core change-request heuristics from initial classification stage."""
    if _CHANGE_ACT_RE.search(folded):
        return True
    if not _CHANGE_VERB_RE.search(folded):
        return False
    if _CHANGE_NOUN_RE.search(folded):
        return True
    if any(f for f in _files_in(scannable)
           if f.lower() not in _NON_FILE_NAMES):
        return True
    return any(_looks_like_identifier(s) for s in _code_spans(raw))


def _turn_act_calls(messages, start_idx) -> bool:
    """True when this turn already emitted an acting (edit/write/run) call."""
    for tc in _turn_tool_calls(messages, start_idx):
        fn = tc.get("function") or {}
        if isinstance(fn, dict) and _ACT_TOOL_RE.search(fn.get("name") or ""):
            return True
    return False


# Edit tools vs command tools, split out of _ACT_TOOL_RE: after an implement
# the proxy must see a TEST RUN, not just any act -- a second edit is not
# verification. A command call only counts as a test run when its arguments
# look like one (unittest/pytest/npm test); `bash ls` never counts.
_EDIT_TOOL_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:edit|write|create|apply|patch)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
_RUN_TOOL_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:bash|shell|exec|run|pytest|npm|make)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
_TEST_CMD_RE = re.compile(
    r"\b(?:unittest|pytest|npm\s+test|make\s+test|go\s+test)\b",
    re.IGNORECASE,
)
_SOURCE_EDIT_EXT_RE = re.compile(
    r"\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|php|swift|kt|c|h|cpp)$",
    re.IGNORECASE,
)
# Explicit user opt-out ("do not run tests", "khong chay test", "skip tests").
# "Do not start the server" is NOT an opt-out: tests still run.
_NO_TEST_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|skip|khong\s+chay|dung\s+chay)\b.{0,24}\btests?\b"
    r"|\bkhong\s+can\s+(?:chay\s+)?test\b",
    re.IGNORECASE,
)


def _edit_indices(messages, start_idx=None) -> list:
    """Indices of assistant messages that edit a source file, in order.

    ``start_idx`` scopes the scan to evidence after that message index (the
    current request's user turn): an edit from an earlier request must not
    mark a new change request as implemented. ``None`` keeps the old
    whole-conversation behaviour (default -1 = everything after).
    """
    if start_idx is None:
        start_idx = -1
    out = []
    for i, msg in enumerate(messages or []):
        if i <= start_idx:
            continue
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if not isinstance(fn, dict):
                continue
            if not _EDIT_TOOL_RE.search(fn.get("name") or ""):
                continue
            args = _tool_arguments_dict(fn)
            for key in ("path", "filepath", "file_path", "file",
                        "filename", "file_name", "target", "target_file"):
                val = args.get(key)
                if isinstance(val, str) and _SOURCE_EDIT_EXT_RE.search(val.strip()):
                    out.append(i)
                    break
    return out


def _test_run_indices(messages, start_idx=None) -> list:
    """Indices of answered test-runner calls, in order.

    A call without a paired tool result proves nothing ran, so unanswered
    calls never count -- same rule as files_read_in_turn. ``start_idx``
    scopes the scan to evidence after that message index (the current
    request's user turn); ``None`` keeps the whole-conversation behaviour.
    """
    if start_idx is None:
        start_idx = -1
    answered = set()
    for msg in messages or []:
        if (isinstance(msg, dict) and msg.get("role") == "tool"
                and msg.get("tool_call_id")):
            answered.add(msg["tool_call_id"])
    out = []
    for i, msg in enumerate(messages or []):
        if i <= start_idx:
            continue
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            if tc.get("id") not in answered:
                continue
            fn = tc.get("function") or {}
            if not isinstance(fn, dict):
                continue
            if not _RUN_TOOL_RE.search(fn.get("name") or ""):
                continue
            args = _tool_arguments_dict(fn)
            blob = json.dumps(args, ensure_ascii=False) if args \
                else (fn.get("arguments") if isinstance(fn.get("arguments"), str) else "")
            if isinstance(blob, str) and _TEST_CMD_RE.search(blob):
                out.append(i)
                break
    return out


def implement_test_owed(messages):
    """Reason a change turn still owes a unit-test run, else None.

    Fires only on the full shape: the user asked for a code change, a
    source file was edited, and no test command ran after the last edit
    (a stale pre-edit run does not count). Silent when the user opted out
    ("do not run tests") or when the edit touched no source file. The
    evidence scan starts at the latest user message: an edit/test from an
    earlier request must not excuse the current one.
    """
    last_user = None
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = _stringify_content(msg.get("content", ""))
    if not last_user:
        return None
    scannable = _strip_code_spans(last_user)
    folded = _fold_diacritics(scannable)
    folded = re.sub(r"\s+", " ", folded).strip()
    if not _is_change_request(folded, scannable, last_user):
        return None
    if _NO_TEST_RE.search(folded):
        return None
    last_user_idx = _last_user_index(messages)
    edits = _edit_indices(messages, start_idx=last_user_idx)
    if not edits:
        return None
    runs = _test_run_indices(messages, start_idx=last_user_idx)
    if any(r > edits[-1] for r in runs):
        return None
    return ("change implemented but unit tests never ran: run the unit tests "
            "(python -m unittest discover -s tests); if no test covers the "
            "changed behavior, add one first, then run")


def is_test_retry_reason(reason) -> bool:
    """True when a retry reason means "implemented but never test-run"."""
    return bool(reason) and reason.startswith(
        "change implemented but unit tests never ran")


def is_review_test_reason(reason) -> bool:
    """True when a retry reason means "review started without test results"."""
    return bool(reason) and reason.startswith(
        "review requested but unit tests never ran")


def change_demanded(messages) -> bool:
    """True when the newest user turn asks for a code change."""
    last_user = None
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = _stringify_content(msg.get("content", ""))
    if not last_user:
        return False
    scannable = _strip_code_spans(last_user)
    folded = _fold_diacritics(scannable)
    folded = re.sub(r"\s+", " ", folded).strip()
    return _is_change_request(folded, scannable, last_user)


# ─── Task-pattern gate ────────────────────────────────────────────────────
# A vague request ("review source code") must not be answered like a narrow
# one ("what does X do"). The proxy classifies the newest user turn up front
# -- no extra upstream call, purely deterministic -- and each pattern carries
# a mandatory numbered flow the model must follow. Enforcement lives in
# pending_tool_request; the banner below announces the detected pattern so
# the model knows which flow binds it before it acts.

_TASK_PATTERN_GENERIC_REVIEW = "GENERIC_REVIEW"
_TASK_PATTERN_FILE_REVIEW = "FILE_REVIEW"
_TASK_PATTERN_CHANGE = "CHANGE"
_TASK_PATTERN_TOOL_DEMAND = "TOOL_DEMAND"


def _core_listed_files(messages) -> list:
    """Listed ``<root>/*.py`` source files of the main project, else ``[]``.

    Same project-root detection the required-reads check uses: count ``.py``
    files per first path component, take the busiest root. Bare names
    (``a.py`` with no directory) never form a core set.
    """
    from collections import Counter
    project_counts = Counter()
    for f in listed_turn_files(messages):
        if f.endswith(".py") and not f.startswith("__pycache__/"):
            parts = f.split("/")
            if len(parts) > 1:
                project_counts[parts[0]] += 1
    if not project_counts:
        return []
    main_project = project_counts.most_common(1)[0][0]
    return sorted(f for f in listed_turn_files(messages)
                  if f.startswith(main_project + "/") and f.endswith(".py"))


def unread_listed_core_files(messages) -> list:
    """Listed core source files with no read anywhere in the conversation."""
    core = _core_listed_files(messages)
    if not core:
        return []
    read_lower = files_read_in_turn(messages)
    called_lower = {f.lower() for f in _conversation_called_files(messages)}
    return sorted(f for f in core
                  if not _path_seen(f, read_lower)
                  and not _path_seen(f, called_lower))


def detect_task_pattern(messages):
    """Newest user turn's task pattern, else None.

    Priority: a review of named files is a FILE_REVIEW even when it also
    says "source"; a change request outranks a bare tool demand. Explain /
    where-is questions return None -- grep + a short read is enough there
    and a numbered flow would burn calls for nothing.
    """
    last_user = None
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user = _stringify_content(msg.get("content", ""))
    if not last_user:
        return None
    scannable = _strip_code_spans(last_user)
    folded = _fold_diacritics(scannable)
    folded = re.sub(r"\s+", " ", folded).strip()
    is_generic = _is_source_review(folded)
    is_file = _is_file_review(folded, scannable)
    if is_file:
        return _TASK_PATTERN_FILE_REVIEW
    if is_generic:
        return _TASK_PATTERN_GENERIC_REVIEW
    if _is_change_request(folded, scannable, last_user):
        return _TASK_PATTERN_CHANGE
    if _TOOL_DEMAND_RE.search(folded):
        return _TASK_PATTERN_TOOL_DEMAND
    return None


def _task_pattern_banner_legacy(messages) -> str:
    """Mandatory-flow banner for the detected task pattern, else ``""``."""
    pattern = detect_task_pattern(messages)
    if pattern == _TASK_PATTERN_GENERIC_REVIEW:
        core = _core_listed_files(messages)
        if core:
            names = ", ".join(core)
            return (
                "[Task pattern: GENERIC_REVIEW -- vague review request. "
                "Mandatory flow, no skipping: (1) run the unit tests "
                "(python -m unittest discover -s tests) so failures point "
                "at what to read closely; (2) glob to survey; "
                f"(3) read ALL {len(core)} listed source files ({names}) -- "
                "batch several read blocks in one turn; answering from one "
                "or two files is fabrication; "
                "(4) only then answer as plain text with verbatim quotes.]"
            )
        return (
            "[Task pattern: GENERIC_REVIEW -- vague review request. "
            "Mandatory flow, no skipping: (1) run the unit tests first so "
            "failures point at what to read closely; (2) glob to survey; "
            "(3) read every listed source file -- answering from filenames "
            "alone is fabrication; (4) only then answer as plain text.]"
        )
    if pattern == _TASK_PATTERN_FILE_REVIEW:
        return (
            "[Task pattern: FILE_REVIEW. Mandatory flow: "
            "(1) read every named file fully; "
            "(2) answer only about what you read, with verbatim quotes. "
            "A file you did not open is a file you must not discuss.]"
        )
    if pattern == _TASK_PATTERN_CHANGE:
        return (
            "[Task pattern: CHANGE. Mandatory flow: "
            "(1) survey with glob/grep; (2) read the target files; "
            "(3) act with edit/write/run -- never describe the edit "
            "instead of making it; "
            "(4) re-read the changed lines; "
            "(5) run the unit tests (python -m unittest discover -s tests) "
            "unless told not to -- an untested change is unfinished; "
            "(6) if no test covers the changed behavior, add one first, "
            "then run.]"
        )
    if pattern == _TASK_PATTERN_TOOL_DEMAND:
        return (
            "[Task pattern: TOOL_DEMAND. Emit the tool_call block(s) now; "
            "no prose, before or after.]"
        )
    return ""
_task_pattern_banner_orig = _task_pattern_banner_legacy


# One definition of the flow feeds the banner, the planner and the validator.
# ``flow_patch`` installs this hook; when it is absent the legacy banner above
# remains the single implementation, so importing tools alone is unchanged.
_TASK_BANNER_HOOK = None


def task_pattern_banner(messages) -> str:
    """Banner for the detected task pattern, or ``""`` when no flow applies."""
    hook = globals().get("_TASK_BANNER_HOOK")
    if hook is not None:
        try:
            text = hook(messages)
            if text is not None:
                return text
        except Exception:
            pass
    return _task_pattern_banner_legacy(messages)


def pending_tool_request(messages) -> str:
    """Why the newest user turn still owes the model a tool call, else ``None``.

    This is the check that catches a *fabricated* answer -- the failure mode
    where the user asks about ``README.md`` and the model replies as if it had
    opened it, without ever emitting a block. The reply contains no fence, no
    refusal and nothing grammatically wrong, so the response-side heuristic
    above never fires; the only thing provably false is that no tool call in
    the whole conversation has touched the file being discussed.

    Two independent shapes are reported: a file the newest user turn names
    that nothing has read, and a turn that demands tool use without one
    (covering requests with no file to name -- "review source code", say).

    Two guards keep this from burning a call on every finished answer:

    * a tool call made *this turn* ends the search. The text that follows is
      built on a real result, even when it names a file the call did not spell
      out, and replacing a completed summary with the retry's output is how a
      valid answer turns into a tool call nobody asked for;
    * names are compared case-insensitively, because ``readme.md`` typed by
      the user and ``README.md`` passed to ``read`` are the same file -- and
      on Windows they usually are literally.

    Opencode treats such a reply as the final answer and stops, so one turn of
    this ends the session with an answer invented from the filename alone.
    """
    if not isinstance(messages, list):
        return None

    last_user_idx = -1
    last_user = None
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_idx = i
            last_user = _stringify_content(msg.get("content", ""))

    if not last_user:
        return None

    if len(_turn_tool_calls(messages, last_user_idx)) >= _MAX_TURN_CALLS:
        # Safety valve: the gate must never trap an agent in an endless loop.
        return None

    # Pasted logs and code name files the user never asked about; scan what
    # remains after blanking them, for both the file check and the demand
    # match below.
    scannable = _strip_code_spans(last_user)
    folded = _fold_diacritics(scannable)
    folded = re.sub(r"\s+", " ", folded).strip()
    # A listing-only turn (glob/list) on a source-review request must stay
    # owed: filenames alone are not content, and answering from them is the
    # hallucinated review this check exists to prevent.
    is_generic_source_review = _is_source_review(folded)
    is_file_review = _is_file_review(folded, scannable)
    source_review = is_generic_source_review or is_file_review
    turn_has_call = any(isinstance(msg, dict) and msg.get("tool_calls")
                        for msg in messages[last_user_idx + 1:])
    required_reads = 1
    if (is_generic_source_review and len(listed_turn_files(messages)) >= 2):
        # A vague review is owed until EVERY listed core source file is
        # read -- not most of them. Reading all-but-one still leaves a
        # module the answer can hallucinate about, and the S3 live probe
        # showed one read is all a weak model volunteers on its own.
        core_files = _core_listed_files(messages)
        if core_files:
            required_reads = len(core_files)
        else:
            required_reads = 2
    reads = _reads_this_turn(messages)
    source_reads = _source_reads_this_turn(messages)
    called_lower = {f.lower() for f in _conversation_called_files(messages)}
    # An implement/change request is owed until the turn shows real contact
    # with the code: an acting call (edit/write/run), a path read, or a read
    # from an earlier turn. A glob/grep-only survey is not contact -- the
    # snippets are leads, not evidence -- so the early "a call ends the
    # search" return must not swallow it.
    change_owed = (
        _is_change_request(folded, scannable, last_user)
        and not _turn_act_calls(messages, last_user_idx)
        and not files_read_in_turn(messages)
        and not called_lower
    )
    # A vague review grounds itself in test results first: failing tests
    # point at the files worth reading closely, and a green suite bounds
    # the review so covered paths are not hallucinated about. Scoped to
    # generic reviews only -- demanding a full-suite run for "review this
    # one file" would burn 25s for nothing.
    review_test_owed = (
        is_generic_source_review
        and turn_has_call
        and not _test_run_indices(messages, start_idx=last_user_idx)
    )
    if turn_has_call and (reads >= required_reads or not source_review) \
            and not change_owed and not implement_test_owed(messages) \
            and not review_test_owed:
        return None

    created = _created_files(scannable, folded)
    uncovered = sorted(
        f for f in _files_in(scannable)
        if not _path_seen(f, called_lower) and f.lower() not in _NON_FILE_NAMES
        and f.lower() not in created
    )
    if uncovered:
        return "requested file(s) never read: " + ", ".join(uncovered)

    if change_owed:
        return "change requested but no file content was read or edited"

    if review_test_owed:
        return ("review requested but unit tests never ran: run the unit "
                "tests first (python -m unittest discover -s tests) so "
                "failures point at what to read closely")

    test_owed = implement_test_owed(messages)
    if test_owed:
        return test_owed

    # Only generic source reviews ("review source") enforce reading every
    # file the model names in its prose. Named-file reviews are handled by
    # the `uncovered` branch above (user-named files from the latest message).
    declared = declared_unread(messages)
    if is_generic_source_review and declared and turn_has_call:
        return "declared file(s) never read: " + ", ".join(declared)

    # For generic source reviews, require reading source code files (.py).
    # Use source_reads (only .py files) instead of total reads.
    effective_reads = source_reads if is_generic_source_review else reads
    if source_review and turn_has_call and effective_reads < required_reads:
        return SOURCE_REVIEW_OWED

    if _TOOL_DEMAND_RE.search(folded):
        return "user demanded tool use and none happened"
    return None


def build_response_format_instruction(response_format) -> str:
    """Build a prompt instruction from an OpenAI ``response_format`` value."""
    if not isinstance(response_format, dict):
        return ""
    rf_type = response_format.get("type")
    if rf_type == "json_object":
        return (
            "\n\nIMPORTANT: Respond with a single valid JSON object only. "
            "Do not include prose, explanations or Markdown code fences."
        )
    if rf_type == "json_schema":
        schema = response_format.get("json_schema", {})
        if isinstance(schema, dict) and "schema" in schema:
            schema = schema["schema"]
        return (
            "\n\nIMPORTANT: Respond with a single valid JSON value that strictly "
            "conforms to the following JSON Schema. Do not include prose, "
            "explanations or Markdown code fences.\n"
            f"JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
        )
    return ""


def strip_code_fence(text: str) -> str:
    """Remove a single wrapping Markdown code fence (e.g. ```json ... ```)."""
    if not text:
        return text
    stripped = text.strip()
    m = re.match(r'^```[a-zA-Z0-9_-]*[ \t]*\n?(.*?)\n?```$', stripped, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


# ─── Google Native API helpers ─────────────────────────────────────────────────


def build_tool_prompt(tool_defs: list) -> str:
    """Build natural tool-use prompt for Gemini Web that avoids prompt-injection detection."""
    tool_spec = json.dumps(tool_defs, indent=2, ensure_ascii=False)
    return (
        "# Tool Use\n\n"
        "You can call the following tools to help accomplish tasks. "
        "These tools connect to the user's local environment and will execute when called.\n\n"
        "Call format (use this exact format):\n"
        "```function_call\n"
        '{"name": "<tool_name>", "args": {<arguments>}}\n'
        "```\n\n"
        "When calling tools:\n"
        "- Each turn do exactly ONE of: (A) you need to read or change "
        "something -> output ONLY function_call block(s); (B) you already have "
        "everything -> answer as plain text with NO block. Never both, and "
        "never any prose after the block.\n"
        "- Before answering a request to review, audit, inspect or find bugs in "
        "source code, you MUST read the actual contents of the relevant files with "
        "a read/search tool first -- reading one file is not reviewing a codebase. "
        "Listing or globbing file names is not reading.\n"
        "- Output ONLY the function_call block(s), nothing else\n"
        "- You may call multiple tools with multiple blocks\n"
        "- Fixed flow for EVERY turn: do you already have a [Tool result "
        "for ...] proving everything you need? If YES -> answer as plain "
        "text. If NO -> output ONLY function_call block(s) and stop; a "
        "guess or instructions for the user are never an answer.\n"
        "- Every parameter belongs inside the \"args\" object, keyed exactly "
        "like the parameters properties -- never beside \"name\".\n"
        "- Never state that you have read a file, run a command or inspected "
        "the code unless a [Tool result for ...] message proves it.\n"
        "- Never claim the tools are unavailable or that you lack permission: "
        "they are connected and will run.\n"
        "- Never write [Tool result for ...] yourself, and never invent a "
        "result: emit the block and stop.\n"
        "- Search results are leads, not evidence: read matched files "
        "before answering or editing.\n"
        "- A limited read proves only the lines it shows: before naming any "
        "symbol (class, function, endpoint), grep for its name and read "
        "that section -- never invent names for code you have not opened.\n"
        "- When the task needs project-wide context (structure, conventions, "
        "architecture, where a feature lives -- including where to put a new "
        "endpoint or which pattern to follow), glob for README.md / AGENTS.md / "
        "agents.md first and read what exists before reading source. If the "
        "glob finds no docs, proceed with source as usual.\n"
        "- After editing, re-read the changed lines, then run the unit "
        "tests (python -m unittest discover -s tests) unless told not to. "
        "If no test covers the changed behavior, add one first, then run.\n"
        "- Triple backticks inside a string argument (Markdown or code the user "
        "asked you to save) are literal text: keep all three of them. A fence "
        "inside the JSON never closes the block.\n"
        "- After receiving a [Tool result for ...], use that data to answer the user\n\n"
        f"Available tools:\n{tool_spec}"
    )


def _google_tool_choice_instruction(req: dict) -> str:
    """Extract tool_choice constraint from Google API toolConfig."""
    tool_config = req.get("toolConfig", {})
    fc_config = tool_config.get("functionCallingConfig", {})
    mode = fc_config.get("mode", "AUTO")
    allowed = fc_config.get("allowedFunctionNames", [])

    if mode == "NONE":
        return "\n\nIMPORTANT: Do NOT call any tools. Respond with text only."
    if mode == "ANY":
        if allowed:
            names = ", ".join(f'"{n}"' for n in allowed)
            return f"\n\nIMPORTANT: You MUST call one of these tools: {names}. Do not respond with text only."
        return "\n\nIMPORTANT: You MUST call at least one tool. Do not respond with text only."
    return ""


def _google_pseudo_messages(req: dict) -> list:
    """Google contents as OpenAI-style messages for evidence helpers.

    # fix7: tool results stay in order (right after the content that carries
    # them). Pairing: a functionResponse answers the oldest unanswered call of
    # the same name, else the oldest unanswered call.
    """
    messages = []
    call_idx = 0
    orphan = 0
    pending = []  # [(id, name)] unanswered, oldest first
    for content in req.get("contents", []):
        role = content.get("role", "user")
        texts, calls, results = [], [], []
        for p in content.get("parts", []):
            if p.get("text"):
                texts.append(p["text"])
            elif p.get("functionCall"):
                fc = p["functionCall"]
                cid = f"g{call_idx}"
                call_idx += 1
                pending.append((cid, fc.get("name", "")))
                calls.append({"id": cid, "type": "function", "function": {
                    "name": fc.get("name", ""),
                    "arguments": json.dumps(fc.get("args", {}),
                                            ensure_ascii=False)}})
            elif p.get("functionResponse"):
                results.append(p["functionResponse"])
        text = "\n".join(t for t in texts if t)
        if role == "model":
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
        else:
            if text:
                messages.append({"role": "user", "content": text})
            if calls:
                messages.append({"role": "assistant", "content": "",
                                 "tool_calls": calls})
        for fr in results:
            idx = next((i for i, (_c, n) in enumerate(pending)
                        if n == fr.get("name")), 0 if pending else None)
            if idx is None:
                cid = f"g_orphan{orphan}"
                orphan += 1
            else:
                cid = pending.pop(idx)[0]
            messages.append({"role": "tool", "tool_call_id": cid,
                             "name": fr.get("name", ""),
                             "content": json.dumps(fr.get("response", {}),
                                                   ensure_ascii=False)})
    return messages


def google_contents_to_prompt(req: dict) -> tuple:
    """Convert Google API contents/tools/systemInstruction to (prompt_str, images_list).

    Returns (prompt, images) where images is a list of (bytes, mime_type) tuples.
    """
    parts = []
    images = []

    tool_config = req.get("toolConfig", {})
    fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")

    tools = req.get("tools")
    tool_defs = []
    if tools and fc_mode != "NONE":
        for tool_group in tools:
            for fn in tool_group.get("functionDeclarations", []):
                td = {"name": fn.get("name", ""), "description": fn.get("description", "")}
                params = fn.get("parameters") or fn.get("parametersJsonSchema")
                if params:
                    td["parameters"] = params
                tool_defs.append(td)

    sys_inst = req.get("systemInstruction")
    if sys_inst:
        sys_parts = sys_inst.get("parts", [])
        sys_text = " ".join(p.get("text", "") for p in sys_parts if p.get("text"))
        if sys_text:
            if tool_defs:
                constraint = _google_tool_choice_instruction(req)
                parts.append(sys_text + "\n\n" + build_tool_prompt(tool_defs) + constraint)
            else:
                parts.append(sys_text)
    elif tool_defs:
        constraint = _google_tool_choice_instruction(req)
        parts.append(build_tool_prompt(tool_defs) + constraint)

    for content in req.get("contents", []):
        role = content.get("role", "user")
        msg_parts = []
        for p in content.get("parts", []):
            if p.get("text"):
                msg_parts.append(p["text"])
            elif p.get("inlineData"):
                data = p["inlineData"]
                try:
                    images.append((
                        base64.b64decode(data["data"], validate=True),
                        data.get("mimeType", "image/png"),
                    ))
                    msg_parts.append("[Image attached]")
                except (KeyError, ValueError, TypeError, binascii.Error):
                    pass
            elif p.get("functionCall"):
                fc = p["functionCall"]
                msg_parts.append(
                    f'```function_call\n{json.dumps({"name": fc["name"], "args": fc.get("args", {})}, ensure_ascii=False)}\n```'
                )
            elif p.get("functionResponse"):
                fr = p["functionResponse"]
                msg_parts.append(
                    f'[Tool result for {fr.get("name", "")}]: {json.dumps(fr.get("response", {}), ensure_ascii=False)}'
                )
        text = "\n".join(msg_parts)
        if role == "model":
            parts.append(f"[Assistant]: {text}")
        else:
            parts.append(text)

    if tool_defs:
        banner = task_pattern_banner(_google_pseudo_messages(req))
        if banner:
            parts.append(banner)

    return defuse_extension_words("\n\n".join(p for p in parts if p)), images


# A ``function_call`` marker with no fence around it. Models drop the
# backticks often enough that ignoring these would lose real calls. The
# ``(?:^|\\n)`` is deliberate: with ``search(text, pos)`` a bare ``^`` only ever
# matches the very start of the string, so an already-scanned marker is never
# found again and the scan always moves forward.
_BARE_FUNCTION_CALL = re.compile(r'(?:^|\n)function_call\s*\n')


def _iter_bare_function_calls(text: str):
    """Yield ``(start, end, data)`` for each unfenced ``function_call`` marker.

    Same ``raw_decode`` technique as :func:`_iter_tool_blocks`: the payload
    starts right after the marker and ends when the JSON object does, so a
    nested ``args`` object neither truncates it nor confuses it. The regex this
    replaces stopped at the *first* ``}`` -- i.e. one level too early for any
    payload with nested arguments -- and, because the same pattern was used to
    blank the match out of the answer, left the trailing ``}`` behind while the
    call itself was lost.
    """
    dec = json.JSONDecoder(strict=False)
    pos = 0
    while True:
        m = _BARE_FUNCTION_CALL.search(text, pos)
        if not m:
            return
        j = m.end()
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        try:
            data, end = dec.raw_decode(text, j)
        except ValueError:
            # Unreadable payload: skip the marker only, leaving the text as
            # the model wrote it rather than eating a chunk of the answer.
            pos = m.end()
            continue
        if end <= pos:
            pos = m.end()
            continue
        yield m.start(), end, data
        pos = end


def parse_google_function_calls(text: str, allowed_names=None,
                                tool_schemas=None) -> tuple:
    """Extract function_call blocks from model output.

    Handles 3 formats:
    1. ```function_call\\n{...}\\n``` (standard)
    2. function_call\\n{...} (without backticks)
    3. Raw JSON with "name" + "args" keys

    Both the fenced and the unfenced payload are located with
    ``JSONDecoder.raw_decode`` rather than a regex. A non-greedy pattern stops
    at the first fence or brace that appears *inside* the payload -- ordinary
    for an ``args`` object with nested fields, or for a ``write`` call whose
    content is Markdown -- which cuts the JSON in half: ``json.loads`` then
    fails and the pattern used to blank the block removes that much text from
    the answer too. Blocks that still cannot be read are left in the text, so
    nothing the model wrote is silently dropped.

    ``allowed_names``/``tool_schemas`` mirror :func:`parse_tool_calls`:
    names are normalised (``functions.read``/case variants) and undeclared
    or placeholder-copied calls are skipped rather than executed.

    Returns (clean_text, [{"name": ..., "args": ...}])
    """
    from gemini_web2api.gemini import log

    function_calls = []

    def _accept(data) -> dict:
        if not (isinstance(data, dict) and data.get("name")):
            return None
        name = _normalize_tool_name(data.get("name"), allowed_names)
        if allowed_names is not None and name is None:
            log(f"Dropping function_call block: undeclared tool "
                f"'{data.get('name')}'")
            return None
        resolved = name or data.get("name")
        props = (tool_schemas or {}).get(resolved) if tool_schemas else None
        args = _arguments_dict(data, props)
        if isinstance(args, dict) and any(
                isinstance(v, str) and v.startswith("EXAMPLE_")
                for v in args.values()):
            log(f"Dropping function_call block: placeholder args copied "
                f"for '{resolved}'")
            return None
        return {"name": resolved, "args": args}
    function_calls = []

    # 1. Fenced blocks. ``json`` fences are skipped: without a declared-name
    #    check (this endpoint parses before it knows what the client sent) an
    #    ordinary JSON example in prose would be taken for a call.
    parts, last_end = [], 0
    for start, end, data, kind, _body in _iter_tool_blocks(text):
        if kind == "json" or not (isinstance(data, dict) and data.get("name")):
            continue
        if kind in ("function_call", "tool_call", "tool_code"):
            line_start = text.rfind("\n", 0, start) + 1
            if text[line_start:start].strip():
                continue
        accepted = _accept(data)
        if accepted is None:
            continue
        function_calls.append(accepted)
        parts.append(text[last_end:start])
        last_end = end
    # The tail stays scannable for stage 2 below (a bare marker may follow a
    # fenced block); it is dropped from the final text when calls exist.
    head, tail = "".join(parts), text[last_end:]
    clean = head + tail

    # 2. The same payload with a bare marker and no fences.
    before_bare = len(function_calls)
    parts, last_end = [], 0
    for start, end, data in _iter_bare_function_calls(clean):
        accepted = _accept(data)
        if accepted is None:
            continue
        function_calls.append(accepted)
        parts.append(clean[last_end:start])
        last_end = end
    if len(function_calls) > before_bare:
        clean = "".join(parts)
    elif before_bare:
        # Only fenced calls: a turn either calls or answers, so nothing after
        # the last block -- prose, or a self-written "[Tool result ...]" --
        # survives.
        clean = head
    else:
        parts.append(clean[last_end:])
        clean = "".join(parts)
    clean = _strip_fabricated(clean, bool(function_calls)).strip()

    # 3. The whole answer is one bare JSON payload.
    if not function_calls and clean.startswith("{"):
        try:
            data = json.loads(clean, strict=False)
        except (json.JSONDecodeError, ValueError):
            data = None
        accepted = _accept(data) if isinstance(data, dict) else None
        if accepted is not None:
            function_calls.append(accepted)
            clean = ""
    return clean, function_calls


# ---------------------------------------------------------------------------
# evidence patch (evidence_patch.py)
# Helpers that make a small model collect evidence before answering.
# ---------------------------------------------------------------------------

# After this many tool calls in one user turn the evidence gate stands down.
_MAX_TURN_CALLS = 30


def _path_seen(name, seen) -> bool:
    """True when ``name`` matches a path in ``seen`` modulo directory prefix.

    ``server.py`` in prose and ``/abs/pkg/server.py`` in a read call are the
    same file. Exact string comparison reported the file as never read and
    burned retries on a review that was already grounded.
    """
    n = str(name).replace("\\", "/").lower()
    for s in seen or ():
        t = str(s).replace("\\", "/").lower()
        if n == t or n.endswith("/" + t) or t.endswith("/" + n):
            return True
    return False


_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\s*(?:\||:|\t|\u2192)")


def _already_numbered(lines) -> bool:
    """True when most non-empty lines already start with a line number."""
    nonempty = [ln for ln in lines if ln.strip()]
    if len(nonempty) < 4:
        return False
    hits = sum(1 for ln in nonempty if _NUMBERED_LINE_RE.match(ln))
    return hits >= 3 and hits * 2 >= len(nonempty)


def _norm_code(text) -> str:
    """Lowercase, drop line-number prefixes, collapse whitespace."""
    lines = [_NUMBERED_LINE_RE.sub("", ln, count=1)
             for ln in str(text).split("\n")]
    return re.sub(r"\s+", " ", " ".join(lines)).strip().lower()


_SKIP_FENCE_LANGS = {"diff", "patch", "bash", "sh", "shell", "console",
                     "zsh", "powershell", "tool_call", "function_call"}


def _quoted_snippets(text) -> list:
    """Code the answer presents as quotes: fenced lines and inline spans."""
    out = []
    for m in re.finditer(r"```([^\n]*)\n(.*?)```", text, re.DOTALL):
        if m.group(1).strip().lower() in _SKIP_FENCE_LANGS:
            continue
        for ln in m.group(2).split("\n"):
            s = ln.strip()
            if len(s) >= 25 and not s.startswith(("#", "//")):
                out.append(s)
    rest = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    for m in re.finditer(r"`([^`\n]+)`", rest):
        s = m.group(1).strip()
        if (len(s) >= 25 and not s.startswith(("#", "//"))
                and "->" not in s and "=>" not in s and "\u2192" not in s):
            out.append(s)
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def unverified_quotes(text, messages) -> list:
    """Quoted code in ``text`` that appears in no tool result / user message.

    A small model that skimmed a file will happily "quote" lines that do not
    exist. Fires only when the evidence is real (corpus long enough), the
    answer quotes at least three snippets, and at least half of them are
    absent -- so a suggested-fix block next to genuine quotes stays quiet.
    Returns up to five offending snippets, else ``[]``.
    """
    if not isinstance(text, str) or not text:
        return []
    parts = []
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") in ("tool", "user"):
            parts.append(_stringify_content(msg.get("content", "")))
    raw_joined = "\n".join(parts)
    corpus = _norm_code(raw_joined)
    if CONFIG.get("defuse_extension_words", True):
        defused = _norm_code(defuse_extension_words(raw_joined))
        if defused and defused != corpus:
            corpus = corpus + " " + defused
    if len(corpus) < 200:
        return []
    snippets = _quoted_snippets(text)
    if len(snippets) < 3:
        return []
    missing = []
    for s in snippets:
        segs = [g.strip() for g in re.split(r"\.\.\.|\u2026", _norm_code(s))]
        segs = [g for g in segs if len(g) >= 12]
        if segs and not all(g in corpus for g in segs):
            missing.append(s)
    if len(missing) >= 3 and len(missing) * 2 >= len(snippets):
        return missing[:5]
    return []


def evidence_ledger(messages) -> str:
    """One-line ledger of what was actually read this turn, else ``""``."""
    if detect_task_pattern(messages) is None:
        return ""
    start = _last_user_index(messages)
    if not _turn_tool_calls(messages, start):
        return ""
    read = sorted(files_read_in_turn(messages))
    src_ext = (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cpp")
    listed = [f for f in listed_turn_files(messages)
              if f.lower() not in _NON_FILE_NAMES and not _path_seen(f, read)]
    listed.sort(key=lambda f: 0 if f.lower().endswith(src_ext) else 1)
    bits = []
    if read:
        bits.append("read: " + ", ".join(read[:12]))
    if listed:
        bits.append("listed but NOT read yet: " + ", ".join(listed[:12]))
    bits.append("unit tests: " + ("ran" if _test_run_indices(messages, start_idx=start)
                                  else "not run yet"))
    return ("[Evidence so far -- " + "; ".join(bits) +
            ". Make claims only about files you have read.]")


def _choose_reminder(messages, last_role) -> str:
    """Final reminder of the prompt.

    After a tool result the stock reminder says "if it is enough, answer
    now" -- exactly what a small model does after one read of a vague review.
    When the evidence gate says more is owed, say so instead.
    """
    if last_role != "tool":
        return _TOOL_REMINDER
    try:
        owed = pending_tool_request(messages)
    except Exception:
        owed = None
    if owed and detect_task_pattern(messages) is not None:
        return ("\n\n[Reminder: the tool result above is in the conversation, "
                "but the evidence is still incomplete -- " + owed + ". Call "
                "the next tool now; do not answer yet and do not repeat a "
                "call whose result is already present.]")
    return _TOOL_RESULT_REMINDER


# ---------------------------------------------------------------------------
# workspace guard (workspace_guard_patch.py)
# ---------------------------------------------------------------------------

# A bare "workspace" (not part of a path, file name or identifier) is what the
# upstream router reads as "use the Workspace extension".
# Extension trigger words: any of these appearing *as a bare word* (not in a
# path, file name or identifier) can route the request to one of Gemini's
# Workspace extensions, which an anonymous / no-Gmail session answers with the
# canned refusal instead of doing the work. Iteration order matters:
# "my drive" must be replaced before bare "drive".
_EXTENSION_WORDS = {
    "workspace": "project",
    # Two-word form before bare "drive": "my drive" -> "the files" (leaving
    # "my files" would read awkwardly next to real file lists).
    "my drive": "the files",
    "drive": "files",
    "docs": "documents",
    "slides": "presentations",
    "calendar": "schedule",
    "gmail": "email",
}


def _ext_word_re(word: str):
    """Matcher for one bare extension word/phrase (paths/identifiers excluded).

    Case-insensitive with a capture group over the whole phrase, so the
    replacement can restore capitalisation ("My Drive" -> "The files").
    """
    escaped = re.escape(word)
    return re.compile(rf"(?<![\w\\/.-])({escaped})(?![\w\\/@-]|\.\w)",
                      re.IGNORECASE)


# Dict insertion order is applied as-is -- "my drive" precedes "drive".
_EXT_WORD_RES = [(w, _ext_word_re(w)) for w in _EXTENSION_WORDS]


def defuse_extension_words(prompt: str) -> str:
    """Rewrite the bare word ``workspace`` to ``project`` in an outgoing prompt.

    Paths (``D:\\workspace\\x``), file names (``workspace.json``) and
    identifiers (``workspace_id``) are left alone, so tool arguments that
    refer to real files keep working. Disabled by
    ``CONFIG["defuse_extension_words"] = false``.
    """
    if not isinstance(prompt, str) or not prompt:
        return prompt
    from gemini_web2api.config import CONFIG
    if not CONFIG.get("defuse_extension_words", True):
        return prompt
    total = 0
    for word, ext_re in _EXT_WORD_RES:
        repl = _EXTENSION_WORDS[word]

        def _sub(m, r=repl):
            # Preserve a leading capital ("Workspace" -> "Project",
            # "My Drive" -> "The files"); lowercase otherwise.
            return r.capitalize() if m.group(1)[0].isupper() else r

        new, n = ext_re.subn(_sub, prompt)
        if n:
            prompt = new
            total += n
    if total:
        from gemini_web2api.gemini import log
        log(f"Prompt: rewrote {total} extension-trigger word(s) "
            "(avoids the Workspace extension route)")
    return prompt


# ==============================================================================
# Section: gemini.py
# ==============================================================================

"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
import uuid
import re
import urllib.request
import urllib.parse
import urllib.error
import ssl
import os
import hashlib
import tempfile
import threading
import contextlib

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG, _config_lock_ctx
from gemini_web2api.tools import looks_like_tool_call

_ssl_ctx = None
# Maximum bytes to accumulate in the stream line buffer before forcing a flush.
# Prevents unbounded memory growth if upstream sends a huge line without newlines.
_MAX_STREAM_LINE_BYTES = 10 * 1024 * 1024  # 10 MB
# A cache entry is a single immutable tuple, never a dict of separate keys:
# readers grab one reference (atomic under the GIL), so `str` from one file
# generation can never be paired with `sapisid`/`mtime` from another while a
# second thread is mid-update.
_cookie_cache = ("", None, 0)
_httpx_client = None
# Client construction is not idempotent. The server is threaded, so two
# requests racing on the `None` check would each build an httpx.Client and one
# of them would be dropped without ever being closed.
_httpx_lock = threading.Lock()

# Track config values used to create the httpx client so we can detect
# changes and rebuild the client when needed.
_httpx_client_config = {"proxy": None, "timeout": None}

# Lock for CONFIG mutations that touch multiple fields or depend on prior reads.
# Separate from _bl_lock to avoid cross-lock contention with BL auto-refresh.
_config_mutex = threading.Lock()

# Lock for cookie file reads to avoid redundant I/O under high concurrency.
_cookie_file_lock = threading.Lock()

# Minimum interval (seconds) between cookie file mtime checks to avoid
# redundant I/O on filesystems with coarse mtime granularity.
_COOKIE_MTIME_MIN_INTERVAL = 1.0
# Last time cookie mtime was checked.
_cookie_last_mtime_check = 0.0


def log_s1(msg: str):
    if CONFIG["log_requests"]:
        import sys
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
        # A file sink keeps a trail when stderr is swallowed by a launcher
        # (Start-Process, nohup, systemd without journal capture).
        path = CONFIG.get("log_file")
        if path:
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass


class GeminiError(Exception):
    """Upstream error carrying an optional HTTP status code."""

    def __init__(self, message: str, status: int = None):
        super().__init__(message)
        self.status = status


def trace(leg: str, data) -> None:
    """Append one leg of a request's journey to ``trace_file``.

    Four legs are traced when ``debug_trace`` is on: CLIENT -> PROXY (the raw
    do_POST body), PROXY -> MODEL (the prompt as assembled), MODEL -> PROXY
    (the raw upstream bytes) and PROXY -> CLIENT (JSON body or each SSE
    frame). debug_raw already covers the two model legs; this exists so a
    single file shows what the client sent *and* what it received, which is
    what distinguishes "model answered with nothing" from "proxy dropped it".
    Failures are silent: this is diagnostics and must never break a request.
    """
    if not CONFIG.get("debug_trace"):
        return
    path = CONFIG.get("trace_file") or "gemini-trace.log"
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    elif not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== [{time.strftime('%Y-%m-%d %H:%M:%S')}] {leg} "
                     f"({len(data)} chars) =====\n")
            fh.write(data)
            fh.write("\n")
    except OSError:
        pass


def _dump_raw(raw: str):
    """Write one unfiltered upstream response to ``debug_raw_file``.

    ``generate()`` only ever hands back the extracted text, so the shape of the
    raw payload (how many elements ``inner[4]`` holds, which one is thinking vs
    answer vs tool call) is invisible from the logs. Enabled by
    ``CONFIG["debug_raw"]``; failures are silent because this is diagnostics.
    """
    path = CONFIG.get("debug_raw_file") or "gemini-raw.log"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== [{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                     f"{len(raw)} bytes =====\n")
            fh.write(raw)
            fh.write("\n")
    except OSError:
        pass


def fetch_latest_bl():
    """Fetch the latest gemini_bl build label from gemini.google.com."""
    try:
        req = urllib.request.Request(
            "https://gemini.google.com/app",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        ctx = _get_ssl_ctx()
        proxy = CONFIG.get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=ctx),
            )
            with opener.open(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        else:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        # The label family is not stable: Google renamed
        # ``boq_assistant-bard-web-server_20260716.08_p0`` to
        # ``boq_gemini-web-uiserver_20261007.12_p0`` when the UI was rebuilt,
        # and a family-specific pattern then matches nothing -- auto-update
        # silently stops working and every request uses the stale default.
        # Match the shape (``boq_<family>_<date>.<build>_p<n>``) instead.
        m = re.search(r'(boq_[a-z0-9-]+_\d+\.\d+_p\d+)', html)
        if m:
            return m.group(1)
    except Exception as e:
        log(f"BL auto-update fetch failed: {e}")
    return None


# Refreshing the build label costs a network round-trip. Without coordination,
# every request that hits a 405 would fetch the same page simultaneously.
_bl_lock = threading.Lock()
_bl_last_attempt = 0.0
_BL_RETRY_INTERVAL = 60.0


def update_bl_if_needed() -> bool:
    """Fetch and update gemini_bl when a newer build label is available.

    Never blocks: if another thread is already fetching, or we fetched within
    the last ``_BL_RETRY_INTERVAL`` seconds, this returns False immediately and
    the caller reports its original error instead of queueing behind a fetch.
    """
    global _bl_last_attempt
    if not _bl_lock.acquire(blocking=False):
        return False
    try:
        if time.time() - _bl_last_attempt < _BL_RETRY_INTERVAL:
            return False
        # Stamp before fetching so a failing fetch cools down too.
        _bl_last_attempt = time.time()
        new_bl = fetch_latest_bl()
        if new_bl and new_bl != CONFIG["gemini_bl"]:
            log(f"BL auto-updated: {CONFIG['gemini_bl']} -> {new_bl}")
            with _config_mutex:
                CONFIG["gemini_bl"] = new_bl
            return True
        return False
    finally:
        _bl_lock.release()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


# One process-wide gate for every model generation request. State is guarded by
# _upstream_gate_lock; the semaphore is rebuilt only when the configured limit
# changes. Both knobs are read per call so tests and a future reload work.
_upstream_gate_lock = threading.Lock()
_upstream_last_start = 0.0
_upstream_sem = None
_upstream_sem_limit = 0


def _upstream_semaphore():
    """Return the semaphore for the current limit, or None when unlimited."""
    global _upstream_sem, _upstream_sem_limit
    limit = int(CONFIG.get("upstream_max_concurrency") or 0)
    if limit <= 0:
        return None
    with _upstream_gate_lock:
        if _upstream_sem is None or _upstream_sem_limit != limit:
            _upstream_sem = threading.BoundedSemaphore(limit)
            _upstream_sem_limit = limit
        return _upstream_sem


@contextlib.contextmanager
def _upstream_gate():
    """Hold one upstream slot and enforce the minimum start-to-start interval."""
    global _upstream_last_start
    sem = _upstream_semaphore()
    if sem is not None:
        sem.acquire()
    try:
        interval = float(CONFIG.get("upstream_min_interval_sec") or 0)
        if interval > 0:
            with _upstream_gate_lock:
                now = time.monotonic()
                wait = _upstream_last_start + interval - now
                if wait > 0:
                    time.sleep(wait)
                _upstream_last_start = time.monotonic()
        yield
    finally:
        if sem is not None:
            sem.release()


def _reset_upstream_gate():
    """Test helper: clear spacing history and the cached semaphore."""
    global _upstream_last_start, _upstream_sem, _upstream_sem_limit
    with _upstream_gate_lock:
        _upstream_last_start = 0.0
        _upstream_sem = None
        _upstream_sem_limit = 0


def _get_httpx_client():
    """Get or create the httpx client, rebuilding if proxy/timeout config changed.

    All reads/writes of shared state are inside the lock to prevent races.
    """
    global _httpx_client, _httpx_client_config
    if not HAS_HTTPX:
        return None
    with _httpx_lock:
        proxy = CONFIG.get("proxy")
        timeout = CONFIG.get("request_timeout_sec")
        need_rebuild = (
            _httpx_client is None
            or _httpx_client_config["proxy"] != proxy
            or _httpx_client_config["timeout"] != timeout
        )
        if need_rebuild:
            # Close old client if exists.
            if _httpx_client is not None:
                try:
                    _httpx_client.close()
                except Exception:
                    pass
            transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
            _httpx_client = httpx.Client(transport=transport, timeout=timeout, verify=True)
            _httpx_client_config["proxy"] = proxy
            _httpx_client_config["timeout"] = timeout
        return _httpx_client


def _extract_sapisid(cookie_str: str) -> str:
    if not cookie_str:
        return ""
    m = re.search(r"(?:^|;\s*)SAPISID=([^;\s]+)", cookie_str)
    return m.group(1) if m else ""


def _normalize_model_tickets(raw_tickets) -> dict:
    """Validate ``model_tickets`` before persisting or applying them.

    This fork stores the *full* ``X-Goog-Ext-525001261-Jspb`` header value
    (a JSON array) keyed by family (``flash``, ``pro``, ...), because the
    upstream routes on that header verbatim. The extension therefore sends
    the header it captured, not just the 16-hex ticket id embedded at index 4.
    Index 4 is optional metadata: most real captures carry ``null`` there, and
    routing reads the ``(family, variant)`` pair at indexes 14/15 instead.
    Invalid entries are dropped rather than written into config.
    """
    if not isinstance(raw_tickets, dict):
        return {}
    clean = {}
    for mk, mv in raw_tickets.items():
        if len(clean) >= 32:
            break
        if not isinstance(mk, str) or not isinstance(mv, str):
            continue
        key = mk.strip()
        value = mv.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}", key):
            continue
        if not value or len(value) > 8192 or any(c in value for c in "\r\n\x00"):
            continue
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if not isinstance(parsed, list) or len(parsed) < 16:
            continue
        fam, var = parsed[14], parsed[15]
        if (not isinstance(fam, int) or isinstance(fam, bool)
                or not isinstance(var, int) or isinstance(var, bool)):
            continue
        clean[key] = value
    return clean


def _normalize_metadata(data: dict) -> dict:
    meta = {}
    for k in ("xsrf_token", "gemini_bl"):
        val = data.get(k)
        if isinstance(val, str) and val.strip() and len(val) <= 4096 and not any(c in val for c in "\r\n\x00"):
            meta[k] = val.strip()
    au = data.get("auth_user")
    if isinstance(au, (int, str)) and not isinstance(au, bool) and str(au).strip().isdigit():
        meta["auth_user"] = str(au).strip()
    tickets = _normalize_model_tickets(data.get("model_tickets"))
    if tickets:
        meta["model_tickets"] = tickets
    return meta


def update_cookie_payload(data: dict) -> tuple:
    """Validate and apply a pushed cookie payload. Returns (ok, err_msg, status)."""
    global _cookie_cache
    if not isinstance(data, dict):
        return False, "invalid JSON object", 400
    raw_cookie = data.get("cookie")
    if not isinstance(raw_cookie, str) or not raw_cookie.strip():
        return False, "missing or invalid cookie field", 400
    cookie_str = raw_cookie.strip()
    if len(cookie_str) > 16384 or any(c in cookie_str for c in "\r\n\x00"):
        return False, "invalid characters or length in cookie field", 400
    sapisid = _extract_sapisid(cookie_str)
    meta = _normalize_metadata(data)

    with _cookie_file_lock:
        cached_str, cached_sapisid, _cached_mtime = _cookie_cache
        cf = CONFIG.get("cookie_file")
        existing = {}
        disk_cookie = ""
        disk_sapisid = None
        if cf and os.path.exists(cf):
            try:
                with open(cf, "r", encoding="utf-8-sig") as rf:
                    raw_existing = rf.read().strip()
                if raw_existing.startswith("{"):
                    loaded = json.loads(raw_existing)
                    if isinstance(loaded, dict):
                        existing = loaded
                        disk_cookie = existing.get("cookie", "") or ""
                        disk_sapisid = existing.get("sapisid") or _extract_sapisid(disk_cookie)
                else:
                    disk_cookie = raw_existing
                    disk_sapisid = _extract_sapisid(disk_cookie) or None
            except Exception as e:
                log(f"Cookie read error: {e}")
                return False, "failed to read cookie file", 500

        if cached_str:
            current_cookie, current_sapisid = cached_str, cached_sapisid
        else:
            current_cookie, current_sapisid = disk_cookie, (disk_sapisid or None)
        same_cookie = cookie_str == current_cookie and (sapisid or None) == current_sapisid

        cur_tickets = dict(CONFIG.get("model_tickets") or {})
        new_tickets = meta.get("model_tickets") or {}
        disk_tickets = _normalize_model_tickets(existing.get("model_tickets"))
        merged_tickets = dict(cur_tickets)
        merged_tickets.update(disk_tickets)
        merged_tickets.update(new_tickets)
        merged_tickets = _normalize_model_tickets(merged_tickets)
        tickets_changed = merged_tickets != cur_tickets
        scalar_changed = any(
            meta.get(k) != CONFIG.get(k) for k in meta if k != "model_tickets"
        )
        if same_cookie and not tickets_changed and not scalar_changed:
            # Refresh the cache from disk when it was cold; nothing to write.
            if cf and not cached_str and os.path.exists(cf):
                try:
                    _cookie_cache = (cookie_str, sapisid or None, os.path.getmtime(cf))
                except OSError:
                    pass
            return True, "", 200

        existing["cookie"] = cookie_str
        if sapisid:
            existing["sapisid"] = sapisid
        else:
            existing.pop("sapisid", None)
        if merged_tickets:
            existing["model_tickets"] = merged_tickets
        else:
            existing.pop("model_tickets", None)
        for k, v in meta.items():
            if k != "model_tickets":
                existing[k] = v

        mtime = 0
        if cf:
            tmp_path = None
            try:
                parent_dir = os.path.dirname(os.path.abspath(cf)) or "."
                fd, tmp_path = tempfile.mkstemp(dir=parent_dir, prefix=".cookie-", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as wf:
                    json.dump(existing, wf, ensure_ascii=False, indent=2)
                os.replace(tmp_path, cf)
                tmp_path = None
                mtime = os.path.getmtime(cf)
            except Exception as e:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                log(f"Cookie persist error: {e}")
                return False, "failed to persist cookie file", 500

        with _config_lock_ctx():
            for k, v in meta.items():
                if k != "model_tickets":
                    CONFIG[k] = v
            if merged_tickets:
                CONFIG["model_tickets"] = merged_tickets
            elif "model_tickets" in CONFIG:
                CONFIG.pop("model_tickets", None)
        _cookie_cache = (cookie_str, sapisid or None, mtime)
        log("Updated cookies via /v1/cookies")
        return True, "", 200


def load_cookie() -> tuple:
    """Load cookie from file with mtime-based caching."""
    global _cookie_cache, _cookie_last_mtime_check
    cookie_file = CONFIG.get("cookie_file")
    cached_str, cached_sapisid, cached_mtime = _cookie_cache
    if not cookie_file or not os.path.exists(cookie_file):
        # A pushed payload can live in memory even without a cookie_file.
        return cached_str, cached_sapisid
    try:
        # Throttle mtime checks to avoid redundant I/O on filesystems with
        # coarse mtime granularity (e.g., FAT, some network mounts).
        now = time.time()
        if now - _cookie_last_mtime_check < _COOKIE_MTIME_MIN_INTERVAL:
            # Return cached value without hitting disk.
            if cached_str:
                return cached_str, cached_sapisid
        # Serialize file reads and associated CONFIG updates to avoid
        # redundant I/O, partial reads, and interleaved config mutations
        # under concurrent requests.
        with _cookie_file_lock:
            cached_str, cached_sapisid, cached_mtime = _cookie_cache
            # Re-check mtime after acquiring lock (another thread may have updated).
            mtime = os.path.getmtime(cookie_file)
            if mtime == cached_mtime and cached_str:
                _cookie_last_mtime_check = now
                return cached_str, cached_sapisid
            _cookie_last_mtime_check = now
            with open(cookie_file, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
            if content.startswith("{"):
                data = json.loads(content)
                cookie_str = data.get("cookie", "")
                sapisid = data.get("sapisid", "") or _extract_sapisid(cookie_str)
                # The bundled extension exports {cookie, sapisid, auth_user,
                # xsrf_token, gemini_bl} in one file. Ignore the extra fields and
                # StreamGenerate goes out without its `at` form field (HTTP 400)
                # and with whatever stale build label happens to be configured,
                # so sync them here: pointing cookie_file at the export must be
                # enough on its own.
                meta = _normalize_metadata(data)
                with _config_lock_ctx():
                    if data.get("xsrf_token"):
                        CONFIG["xsrf_token"] = data["xsrf_token"]
                    if data.get("auth_user") not in (None, ""):
                        CONFIG["auth_user"] = data["auth_user"]
                    if "model_tickets" in meta:
                        cur_tickets = dict(CONFIG.get("model_tickets") or {})
                        cur_tickets.update(meta["model_tickets"])
                        CONFIG["model_tickets"] = cur_tickets
                    # The export's build label loses to a live one: the startup fetch
                    # and the 405 auto-refresh both write CONFIG first, and an
                    # operator-pinned label (anything but the built-in default) is
                    # deliberate. Only when auto-update is off and nothing was ever
                    # configured does the export fill the gap -- otherwise every
                    # cookie-file touch re-installs a stale label over a fresh one.
                    if data.get("gemini_bl"):
                        if (not CONFIG.get("auto_update_bl", True)
                                and CONFIG.get("gemini_bl") == DEFAULT_CONFIG.get("gemini_bl")):
                            CONFIG["gemini_bl"] = data["gemini_bl"]
            else:
                cookie_str = content
                sapisid = _extract_sapisid(cookie_str)
        # One atomic swap: a reader either sees the whole old entry or the
        # whole new one.
        _cookie_cache = (cookie_str, sapisid or None, mtime)
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        return cached_str, cached_sapisid


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix() -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    auth_user = CONFIG.get("auth_user")
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


_FF11_AUTH = {"ts": 0.0, "logged": False}


def _ff11_cookie_map(cookie_str):
    out = {}
    for part in re.split(r";\s*", cookie_str or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _build_headers(ticket: str = None) -> dict:
    """Build request headers for Gemini upstream (Authorization SAPISIDHASH is opt-in)."""
    account_prefix = _account_prefix()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(CONFIG["auth_user"])
    cookie_str, json_sapisid = load_cookie()
    cookie_str = (cookie_str or "").replace("\r", "").replace("\n", "").strip()
    cmap = _ff11_cookie_map(cookie_str)
    if cookie_str:
        headers["Cookie"] = cookie_str
        if not _FF11_AUTH["logged"]:
            _FF11_AUTH["logged"] = True
            log("[fix11] cookie names sent: " + ", ".join(sorted(cmap)) if cmap
                else "[fix11] cookie string has no name=value pairs")
            if "__Secure-1PSID" not in cmap and "__Secure-3PSID" not in cmap \
                    and "SID" not in cmap:
                log("[fix11] WARNING: no __Secure-1PSID/SID cookie -- the session "
                    "is not logged in, Google will answer 401/400")
            if "__Secure-1PSIDTS" not in cmap:
                log("[fix11] WARNING: no __Secure-1PSIDTS cookie (re-export cookies)")
    else:
        if not _FF11_AUTH["logged"]:
            _FF11_AUTH["logged"] = True
            log("[fix11] WARNING: no cookie loaded (cookie_file=%r exists=%s)" % (
                CONFIG.get("cookie_file"),
                bool(CONFIG.get("cookie_file")
                     and os.path.exists(CONFIG["cookie_file"]))))
    if CONFIG.get("send_sapisidhash", False):
        sapisid = (cmap.get("SAPISID") or cmap.get("__Secure-3PAPISID")
                   or json_sapisid)
        if sapisid:
            headers["Authorization"] = make_sapisidhash(sapisid)
    if ticket:
        from gemini_web2api.models import TICKET_HEADER
        headers[TICKET_HEADER] = ticket
    return headers


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None) -> str:
    # The `at` (xsrf) token lives in the cookie file and only reaches CONFIG
    # via load_cookie(). The body is built before any header is assembled, so
    # without this call the first attempt of a request goes out without `at`
    # and Gemini answers HTTP 400 -- and 400 is treated as non-retryable.
    load_cookie()
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    # One line per request naming what was actually asked for: when the answer
    # looks like another model, this plus check_routing is the whole trail.
    log(f"Upstream model family={model_id} variant={(extra_fields or {}).get(80)}")
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if CONFIG.get("xsrf_token"):
        params["at"] = CONFIG["xsrf_token"]
    if CONFIG.get("debug_raw"):
        # Mirror the response dump: the built prompt (with tool block and any
        # injected extra fields) is what the model actually sees, so a collapse
        # between client request and upstream text is visible right here.
        _dump_raw("REQUEST >>>\n" + json.dumps(inner, ensure_ascii=False)
                  + "\n<<< REQUEST")
    trace("PROXY -> MODEL", json.dumps(inner, ensure_ascii=False))
    return urllib.parse.urlencode(params)


def _get_url() -> str:
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix()
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


_MID_DRAFT_RESTART_RE = re.compile(
    r"(?m)^[^\n`]*`(?:[^\n`]*`[^\n`]*`)*[a-z0-9_./-]+"
    r"([A-Z\u00c0-\u1ef9][a-z\u00e0-\u1ef9]+\s+[^\n]{20,})$"
)


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    m = _MID_DRAFT_RESTART_RE.search(text)
    if m and "\n1. " in text[:m.start()] and "\n1. " in text[m.start(1):]:
        text = text[m.start(1):]
    return text.strip() if strip else text


_SCAFFOLD_FENCE = re.compile(r"```\w*\?code_")
_SCAFFOLD_LANGS = ("python", "javascript", "text")


def _holds_scaffold(buf: str) -> bool:
    """True while ``buf`` ends inside an open *upstream* scaffolding fence.

    Upstream interleaves helper blocks such as
    ```` ```python?code_reference&code_event_index=0 ... ``` ```` that
    ``clean_text`` strips as a unit, so those must stay buffered until they
    close. Ordinary code fences in the answer must not be buffered -- counting
    backticks instead would hold an entire legitimate code block back until its
    closing fence arrived, delivering it as one late lump.
    """
    if buf.count("```") % 2 == 0:
        return False
    open_at = buf.rfind("```")
    rest = buf[open_at + 3:]
    head, terminated, _ = rest.partition("\n")
    if _SCAFFOLD_FENCE.match(buf[open_at:]):
        return True                      # ```python?code_... arrived complete
    if terminated:
        return False                     # tag finished without ?code_ → normal fence
    if "?" in head:
        return True                      # tag still growing past '?'
    # Language tag not terminated yet: hold only while it can still grow into
    # one of the scaffolding tags (released as soon as a newline proves the
    # fence is an ordinary one).
    return any(lang.startswith(head) for lang in _SCAFFOLD_LANGS)


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


# Two payload shapes carry the same upstream rejection: the legacy prose form
# ``BardErrorInfo [1099]`` and the protobuf-JSON form the current build emits
# (``...application.BardErrorInfo",[1099]]``). The second one does not match
# ``BardErrorInfo\s*\[`` -- a quote and a comma sit between the name and the
# bracket -- so the rejection used to fall through as an empty reply and the
# real reason (often transient: the same request succeeds minutes later)
# never reached the log or the client.
# Match: BardErrorInfo followed by any punctuation/quotes, then optional
# whitespace/commas, then [CODE] or "CODE" or just CODE digits.
_BARD_ERROR_RE = re.compile(r'BardErrorInfo[^0-9]*[\[,]?\s*["\]]?\s*(\d+)')

# Codes Google is known to return, appended as a human-readable hint.
BARD_ERROR_HINTS = {
    1013: "temporary generation error",
    1037: "usage limit exceeded for the requested model",
    1050: "requested model is inconsistent with the conversation",
    1052: "requested model header is invalid or unavailable",
    1060: "Google temporarily blocked this IP address",
}


def bard_error_code(raw: str):
    """Return the BardErrorInfo code embedded in ``raw``, or None."""
    m = _BARD_ERROR_RE.search(raw)
    return int(m.group(1)) if m else None


def raise_bard_error(raw: str) -> None:
    """Raise a GeminiError when ``raw`` carries a BardErrorInfo rejection.

    Returns silently otherwise. GeminiError (status None) maps to 503 on the
    OpenAI endpoints: the upstream -- not the client's request -- refused.
    """
    code = bard_error_code(raw)
    if code is None:
        return
    hint = BARD_ERROR_HINTS.get(code)
    raise GeminiError(
        f"Gemini upstream rejected request: BardErrorInfo [{code}]"
        + (f" -- {hint}" if hint else "")
    )


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text.

    Ties break towards the candidate carrying a tool call, not towards the
    longer one. The answer and the call arrive as separate texts within the
    same payload; if a thinking or summary block happens to run longer, a
    longest-wins pick drops the call and the client is handed prose that only
    claims a tool was used. Nothing changes when no candidate looks like a
    call, which is the overwhelmingly common case.
    """
    raise_bard_error(raw)
    best_text = ""
    best_call = ""
    canned = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if looks_like_tool_call(t):
                if len(t) > len(best_call):
                    best_call = t
            elif looks_like_canned_failure(t):
                canned = t
            elif len(t) > len(best_text):
                best_text = t
    if canned:
        # Gemini can abort mid-answer: the payload keeps the partial text and
        # replaces the rest with a canned failure sentence. Longest-wins then
        # returned the cut-off sentence as a finished turn (live 11/5), so the
        # retry layer never learned the generation failed. Return the
        # placeholder instead; every caller already treats it as an error.
        return clean_text(canned)
    return clean_text(best_call or best_text)


def upstream_echo(raw: str):
    """Return the (label, family, variant) the upstream echoed, or None.

    The StreamGenerate response names the model that actually served the
    request; comparing it against what was requested is the only way to see a
    silent misroute (the request still answers 200 with fluent text).
    """
    for line in raw.split("\n"):
        if '"wrb.fr"' not in line or len(line) < 200:
            continue
        try:
            meta = json.loads(json.loads(line)[0][2])
        except (json.JSONDecodeError, IndexError, TypeError):
            continue
        if isinstance(meta, list) and len(meta) >= 60:
            return meta[42], meta[58], meta[59]
    return None


def check_routing_s1(raw: str, model_id: int, extra_fields: dict = None, ticket: str = None) -> None:
    """Log a warning when upstream served a different model than requested.

    A missing or expired model ticket is what causes this: the upstream then
    ignores [79]/[80] and answers with the account default (Pro). The answer
    itself is fine, so nothing else in the pipeline notices -- hence a log
    line rather than an error.
    """
    echo = upstream_echo(raw)
    if not echo:
        return
    _, fam, var = echo
    if ticket:
        # The ticket wins over the body fields, so expectations are read from
        # the (family, variant) embedded in the ticket actually sent.
        try:
            t = json.loads(ticket)
            want_fam, want_var = t[14], t[15]
        except (json.JSONDecodeError, IndexError, TypeError):
            want_fam, want_var = model_id, (extra_fields or {}).get(80)
    else:
        want_fam, want_var = model_id, (extra_fields or {}).get(80)
    if fam != want_fam or (want_var is not None and var != want_var):
        log(f"Routing mismatch: requested family={want_fam} variant={want_var} "
            f"but upstream served {echo[0]!r} (family={fam} variant={var}); "
            f"the model ticket in CONFIG['model_tickets'] may be missing or "
            f"expired -- refresh it from a fresh browser capture")
        # Generalize the low-fallback hint beyond Flash-Lite: any family that
        # requested medium/high but was served variant 1 is the same symptom.
        if fam == want_fam and want_var in (2, 4) and var == 1:
            family_names = {
                1: "Flash", 3: "Pro", 6: "Flash-Lite", 8: "Argon",
            }
            family = family_names.get(fam, f"family {fam}")
            log(f"Routing hint: upstream fell back to {family} low "
                f"({fam},{var}); anonymous sessions or exhausted quota "
                f"cannot use medium/high tickets")
        if ticket is None:
            try:
                from gemini_web2api.models import MODELS
                wants_ticket = any(cfg.get("mode") == model_id
                                   and cfg.get("ticket")
                                   for cfg in MODELS.values())
            except Exception:
                wants_ticket = False
            if wants_ticket:
                log("Routing mismatch while NO ticket was sent for a model "
                    "that defines one: the ticket key is missing from "
                    "CONFIG['model_tickets'] entirely -- add/refresh it (the "
                    "upstream serves the account default without it)")


def _status_error_s1(status) -> GeminiError:
    """Build the GeminiError for an upstream HTTP status.

    A 400 with no cookie configured is rarely the client's fault: Google
    rejects cookie-less (anonymous) sessions with 400, and reporting that
    verbatim tells the caller their request was malformed when the real fix
    is on this side.
    """
    msg = f"HTTP {status} from Gemini upstream"
    if status == 400 and not CONFIG.get("cookie_file"):
        msg += ("; no cookie_file is set and Google currently rejects"
                " anonymous sessions -- export cookies via the bundled"
                " extension and set cookie_file")
    return GeminiError(msg, status=status)


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, ticket: str = None) -> str:
    """Non-streaming generation with BL-aware retry."""
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields).encode()
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    last_err = None
    bl_refreshed = False
    for attempt in range(CONFIG["retry_attempts"]):
        _t0 = time.monotonic()
        url = _get_url()
        headers = _build_headers(ticket)
        try:
            with _upstream_gate():
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                if proxy:
                    opener = urllib.request.build_opener(
                        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                        urllib.request.HTTPSHandler(context=ctx)
                    )
                    with opener.open(req, timeout=CONFIG["request_timeout_sec"]) as resp:
                        raw = resp.read().decode("utf-8", errors="replace")
                else:
                    with urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"]) as resp:
                        raw = resp.read().decode("utf-8", errors="replace")
            if CONFIG.get("debug_raw"):
                _dump_raw(raw)
            trace("MODEL -> PROXY", raw)
            check_routing(raw, model_id, extra_fields, ticket)
            text = extract_response_text(raw)
            log(f"[timing] generate attempt {attempt + 1}: "
                f"{time.monotonic() - _t0:.1f}s, {len(text)} chars")
            return text
        except urllib.error.HTTPError as e:
            last_err = _status_error(e.code)
            # A stale BL build label manifests as 405/404: refresh and retry once.
            if e.code in (404, 405) and not bl_refreshed and update_bl_if_needed():
                bl_refreshed = True
                log("BL updated after upstream error, retrying")
                continue
            # Client errors will not succeed on retry.
            if e.code in (400, 401, 403, 404, 405):
                break
        except Exception as e:
            last_err = GeminiError(str(e))
        if attempt < CONFIG["retry_attempts"] - 1:
            log(f"Retry {attempt + 1}/{CONFIG['retry_attempts']}: {last_err}")
            time.sleep(CONFIG["retry_delay_sec"])
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None, ticket: str = None):
    """Streaming generation via httpx with BL-aware retry.

    Text is buffered only while an *upstream scaffolding* fence is open so that
    blocks (````python?code_...````) are always stripped whole, even when they
    straddle two network chunks. Ordinary code fences stream through
    immediately.
    """
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields, ticket)
        if text:
            yield text
        return

    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields)
    client = _get_httpx_client()

    last_err = None
    bl_refreshed = False
    for attempt in range(CONFIG["retry_attempts"]):
        _t0 = time.monotonic()
        _ttfb_logged = False
        url = _get_url()
        headers = _build_headers(ticket)
        emitted_raw_text = ""
        clean_buf = ""
        emitted_any = False
        raw_full = ""
        try:
            with _upstream_gate():
                with client.stream("POST", url, content=body, headers=headers) as resp:
                    resp.raise_for_status()
                    buf = ""
                    for chunk in resp.iter_text():
                        buf += chunk
                        raw_full += chunk
                        # Enforce max line buffer to prevent unbounded memory growth
                        # if upstream sends a massive line without newlines.
                        if len(buf) > _MAX_STREAM_LINE_BYTES:
                            log(f"Stream line buffer exceeded {_MAX_STREAM_LINE_BYTES} bytes, forcing flush")
                            # Process what we have as a pseudo-line.
                            texts = _extract_texts_from_line(buf)
                            for index, t in enumerate(texts):
                                if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                    continue
                                if index:
                                    block = clean_text(t, strip=False)
                                    if block:
                                        emitted_any = True
                                        yield block
                                    continue
                                if t.startswith(emitted_raw_text):
                                    clean_buf += t[len(emitted_raw_text):]
                                    emitted_raw_text = t
                                else:
                                    common_len = 0
                                    min_len = min(len(t), len(emitted_raw_text))
                                    while common_len < min_len and t[common_len] == emitted_raw_text[common_len]:
                                        common_len += 1
                                    if common_len > 0:
                                        clean_buf = t[common_len:]
                                        emitted_raw_text = t
                                    else:
                                        clean_buf = t
                                        emitted_raw_text = t
                            if _holds_scaffold(clean_buf):
                                # Hold incomplete scaffold blocks.
                                pass
                            else:
                                delta = clean_text(clean_buf, strip=False)
                                clean_buf = ""
                                if delta:
                                    emitted_any = True
                                    yield delta
                            buf = ""  # Force clear buffer after processing.
                        if "BardErrorInfo" in buf:
                            raise_bard_error(buf)
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            texts = _extract_texts_from_line(line)
                            for index, t in enumerate(texts):
                                if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                    continue
                                if index:
                                    # Later text in the *same* line is a separate
                                    # block (thinking summary), not a cumulative
                                    # echo of the earlier one: emit it immediately.
                                    # Keep the primary cumulative text tracking
                                    # intact so the next line still has a prefix
                                    # relation and does not log spurious warnings.
                                    block = clean_text(t, strip=False)
                                    if block:
                                        emitted_any = True
                                        yield block
                                    continue
                                if t.startswith(emitted_raw_text):
                                    clean_buf += t[len(emitted_raw_text):]
                                    emitted_raw_text = t
                                else:
                                    # First text of the line disagrees with everything
                                    # emitted so far. Upstream may have reset or corrected.
                                    # Try to find the longest common prefix to salvage
                                    # continuity; if none, reset tracking and continue
                                    # from the new text to avoid dropping data.
                                    common_len = 0
                                    min_len = min(len(t), len(emitted_raw_text))
                                    while common_len < min_len and t[common_len] == emitted_raw_text[common_len]:
                                        common_len += 1
                                    if common_len > 0:
                                        # Salvage the delta after the common prefix.
                                        clean_buf = t[common_len:]
                                        emitted_raw_text = t
                                        log(f"Stream prefix reset at {common_len} chars, continuing")
                                    else:
                                        # No common prefix: full reset. Emit the new text
                                        # from scratch rather than dropping it.
                                        clean_buf = t
                                        emitted_raw_text = t
                                        log(f"Stream full reset, emitting new segment: {t[:120]!r}")
                                if _holds_scaffold(clean_buf):
                                    # Inside an open upstream scaffolding block:
                                    # hold until it closes so clean_text can drop it
                                    # in one piece.
                                    continue
                                delta = clean_text(clean_buf, strip=False)
                                clean_buf = ""
                                if delta:
                                    if not _ttfb_logged:
                                        _ttfb_logged = True
                                        log(f"[timing] stream attempt "
                                            f"{attempt + 1}: first token in "
                                            f"{time.monotonic() - _t0:.1f}s")
                                    emitted_any = True
                                    yield delta
            if clean_buf:
                # If the stream ended inside an open scaffolding fence, handle it.
                if _holds_scaffold(clean_buf):
                    open_at = clean_buf.rfind("```")
                    # If we have an opening fence but no closing, emit content
                    # before the fence and log the truncation. This preserves
                    # legitimate text that appeared before the malformed block.
                    if open_at >= 0:
                        before_fence = clean_buf[:open_at]
                        log(f"Stream ended with unclosed scaffolding fence, emitting content before fence ({len(before_fence)} chars)")
                        clean_buf = before_fence
                    else:
                        clean_buf = ""
                delta = clean_text(clean_buf, strip=False)
                if delta:
                    yield delta
            if CONFIG.get("debug_raw") and raw_full:
                # Mirror generate(): the stream path also dumps the raw
                # upstream frames so prefix/delta handling can be audited.
                _dump_raw(raw_full)
            trace("MODEL -> PROXY (stream)", raw_full)
            check_routing(raw_full, model_id, extra_fields, ticket)
            log(f"[timing] stream attempt {attempt + 1}: "
                f"{time.monotonic() - _t0:.1f}s total, {len(raw_full)} raw bytes")
            return
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else None
            last_err = _status_error(status)
            if status in (404, 405) and not bl_refreshed and update_bl_if_needed():
                bl_refreshed = True
                log("BL updated after upstream error, retrying stream")
                continue
            if status in (400, 401, 403, 404, 405):
                raise last_err
        except Exception as e:
            last_err = GeminiError(str(e))
            # Never retry once partial content has been sent to the client.
            if emitted_any:
                raise last_err
        if attempt < CONFIG["retry_attempts"] - 1:
            log(f"Stream retry {attempt + 1}/{CONFIG['retry_attempts']}: {last_err}")
            time.sleep(CONFIG["retry_delay_sec"])
    if CONFIG.get("debug_raw") and raw_full:
        _dump_raw(raw_full)
    raise last_err


# ==============================================================================
# Section: multimodal.py
# ==============================================================================

"""Multimodal: Scotty resumable upload for Gemini image input."""
import json
import base64
import ipaddress
import socket
import urllib.request
import urllib.parse
import time
import ssl
import re
from urllib.parse import urlparse

from gemini_web2api.config import CONFIG, _config_lock_ctx
from gemini_web2api.gemini import load_cookie, make_sapisidhash, _get_ssl_ctx, log_s1 as log


def _get_page_tokens() -> dict:
    """Fetch WIZ_global_data tokens from Gemini page (Push-ID, X-Client-Pctx)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    try:
        req = urllib.request.Request("https://gemini.google.com/app", headers=headers)
        proxy = CONFIG.get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
            )
            resp = opener.open(req, timeout=30)
        else:
            resp = urllib.request.urlopen(req, context=_get_ssl_ctx(), timeout=30)
        html = resp.read().decode()
        tokens = {}
        for key, pattern in [
            ("push_id", r'"qKIAYe":"([^"]+)"'),
            ("pctx", r'"Ylro7b":"([^"]+)"'),
            ("at", r'"thykhd":"([^"]+)"'),
        ]:
            m = re.search(pattern, html)
            if m:
                tokens[key] = m.group(1)
        # The xsrf token ages out like everything else on the page: refresh
        # CONFIG from the same /app fetch the image path already does, so a
        # stale `at` form field stops failing StreamGenerate with HTTP 400.
        m = re.search(r'"SNlM0e":"([^"]+)"', html)
        if m:
            with _config_lock_ctx():
                CONFIG["xsrf_token"] = m.group(1)
            log("xsrf_token refreshed from the Gemini page")
        return tokens
    except Exception as e:
        log(f"Page token fetch failed: {e}")
        return {}


_page_tokens_cache = {"tokens": {}, "ts": 0}


def _cached_page_tokens() -> dict:
    now = time.time()
    if now - _page_tokens_cache["ts"] > 600:
        _page_tokens_cache["tokens"] = _get_page_tokens()
        _page_tokens_cache["ts"] = now
    return _page_tokens_cache["tokens"]


def detect_image_mime(image_bytes: bytes, fallback: str = "image/png") -> str:
    """Infer a common raster image MIME type from its file signature."""
    if not isinstance(image_bytes, bytes):
        return fallback
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp"
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        brand = image_bytes[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
    return fallback


def upload_image(image_bytes: bytes, filename: str = "image.png", mime_type: str = "image/png") -> str:
    """Upload image via Scotty resumable upload. Returns file reference path."""
    tokens = _cached_page_tokens()
    push_id = tokens.get("push_id", "feeds/mcudyrk2a4khkz")
    pctx = tokens.get("pctx", "CgcSBWjK7pYx")

    cookie_str, sapisid = load_cookie()
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    # Step 1: Initiate resumable upload
    start_headers = {
        "Push-ID": push_id,
        "X-Tenant-Id": "bard-storage",
        "X-Client-Pctx": pctx,
        "X-Goog-Upload-Header-Content-Length": str(len(image_bytes)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if cookie_str:
        start_headers["Cookie"] = cookie_str
    if sapisid:
        start_headers["Authorization"] = make_sapisidhash(sapisid)

    start_url = "https://content-push.googleapis.com/upload/"
    req = urllib.request.Request(start_url, data=b"", headers=start_headers, method="POST")

    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx)
        )
        resp = opener.open(req, timeout=30)
    else:
        resp = urllib.request.urlopen(req, context=ctx, timeout=30)

    upload_url = resp.headers.get("X-Goog-Upload-URL") or resp.headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError(f"No upload URL in response headers: {dict(resp.headers)}")

    log(f"Upload session started: {upload_url[:80]}...")

    # Step 2: Upload file data + finalize
    upload_headers = {
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
        "Content-Type": "application/octet-stream",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    req2 = urllib.request.Request(upload_url, data=image_bytes, headers=upload_headers, method="POST")
    if proxy:
        resp2 = opener.open(req2, timeout=60)
    else:
        resp2 = urllib.request.urlopen(req2, context=ctx, timeout=60)

    file_ref = resp2.read().decode().strip()
    if not file_ref or not file_ref.startswith("/"):
        raise RuntimeError(f"Invalid file reference: {file_ref[:100]}")

    log(f"Image uploaded: {filename} -> {file_ref[:50]}...")
    return file_ref


# Upper bound for a fetched image body. Without it one URL can eat unbounded
# RAM while the server reads it; the caller turns b"" into "image fetch
# failed", so oversize is a failed fetch, not a crash.
_MAX_IMAGE_BYTES = 16 * 1024 * 1024







# ==== ssrf5:begin (generated by flow_fix5_patch.py -- re-run to refresh, --revert to remove) ====
import http.client as _hc
import urllib.error as _uerr

# fetch_image_bytes used to validate only the FIRST url and then let urllib
# follow redirects on its own: a public host answering 302 -> http://127.0.0.1/...
# was fetched, and the body could then be uploaded to Gemini. Now every hop is
# validated, redirects are followed by hand, and the connection is pinned to
# the validated IP (SNI + certificate check still use the real host name).
_MAX_REDIRECTS = 5


def _resolve_public_ip_s1(hostname):
    """First public IP of ``hostname``, or None if ANY address is non-public."""
    if not hostname:
        return None
    host = hostname.strip().rstrip(".").strip("[]").split("%")[0]

    def public(addr):
        if addr.version == 6 and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return addr.is_global

    try:
        addr = ipaddress.ip_address(host)
        return str(addr) if public(addr) else None
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return None
    first = None
    for info in infos or []:
        try:
            addr = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return None
        if not public(addr):
            return None
        if first is None:
            first = str(addr)
    return first


class _PinnedHTTPConnection(_hc.HTTPConnection):
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pin_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pin_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(_hc.HTTPSConnection):
    def __init__(self, host, ip, port, timeout, ctx):
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._pin_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _fetch_once(parsed, ip, timeout=30):
    """One GET against the pinned IP. Returns (status, location, body)."""
    https = parsed.scheme == "https"
    port = parsed.port or (443 if https else 80)
    if https:
        conn = _PinnedHTTPSConnection(parsed.hostname, ip, port, timeout, _get_ssl_ctx())
    else:
        conn = _PinnedHTTPConnection(parsed.hostname, ip, port, timeout)
    path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    try:
        conn.request("GET", path, headers={"User-Agent": "Mozilla/5.0",
                                           "Accept": "image/*,*/*;q=0.5",
                                           "Accept-Encoding": "identity"})
        resp = conn.getresponse()
        body = resp.read(_MAX_IMAGE_BYTES + 1) if resp.status == 200 else b""
        return resp.status, resp.getheader("Location"), body
    finally:
        conn.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_once_proxy(url, timeout=30):
    """Through a proxy the IP cannot be pinned; redirects are still manual."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": CONFIG.get("proxy"), "https": CONFIG.get("proxy")}),
        urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
        _NoRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, None, resp.read(_MAX_IMAGE_BYTES + 1)
    except _uerr.HTTPError as e:
        if 300 <= e.code < 400:
            return e.code, e.headers.get("Location"), b""
        return e.code, None, b""


def _ssrf7_hop(url, parsed, ip):
    """One validated hop: ``(status, location, body)``.

    The single network seam of fetch_image_bytes, so redirect handling can be
    tested without sockets. The caller has already checked that ``ip`` is
    public; through a proxy the IP cannot be pinned and redirects stay manual.
    """
    if CONFIG.get("proxy"):
        return _fetch_once_proxy(url)
    return _fetch_once(parsed, ip)


def fetch_image_bytes(url):
    """Fetch an image from ``url``; b"" on any refusal or failure."""
    cur = url
    for _hop in range(_MAX_REDIRECTS + 1):
        parsed = urlparse(cur)
        if parsed.scheme not in ("http", "https"):
            log(f"Image fetch skipped for unsupported URL scheme: {parsed.scheme or 'none'}")
            return b""
        ip = _resolve_public_ip(parsed.hostname or "")
        if not ip:
            log(f"Image fetch blocked for non-public target: {parsed.hostname or 'none'}")
            return b""
        try:
            status, loc, data = _ssrf7_hop(cur, parsed, ip)
        except Exception as e:
            log(f"Image fetch failed: {e}")
            return b""
        if 300 <= status < 400 and loc:
            cur = urllib.parse.urljoin(cur, loc)
            continue
        if status != 200:
            log(f"Image fetch failed: HTTP {status}")
            return b""
        if len(data) > _MAX_IMAGE_BYTES:
            log(f"Image fetch failed: body exceeds {_MAX_IMAGE_BYTES} bytes")
            return b""
        if CONFIG.get("fetch_require_image_magic", True) and not detect_image_mime(data, ""):
            log("Image fetch refused: body is not a recognised image format")
            return b""
        return data
    log(f"Image fetch failed: more than {_MAX_REDIRECTS} redirects")
    return b""
# ==== ssrf5:end ====


# ==============================================================================
# Section: flow_patch.py
# ==============================================================================

"""flow_patch.py -- ép model yếu (gemini-3.5-flash-lite) đi đúng quy trình tool-call.

Ý tưởng: thay vì NHẮC model "hãy đọc file trước khi review", proxy tự giữ
state machine cho từng loại tác vụ (pattern). Mỗi lượt proxy tính ra "bước tiếp
theo" từ bằng chứng có trong hội thoại (client gửi lại toàn bộ messages nên
không cần lưu state ở server):

    GENERIC_REVIEW : survey(glob) -> tests -> read TẤT CẢ file lõi -> answer
    FILE_REVIEW    : read các file user nêu tên -> answer
    CHANGE         : survey -> read file đích -> act(edit/write) -> tests -> answer
    EXPLAIN        : grep/glob -> read file trúng -> answer
    TOOL_DEMAND    : bắt buộc gọi 1 tool -> answer

Với mỗi bước có 2 chế độ:
  * DRIVE (bước xác định được: glob, chạy test, read file đã biết tên):
    proxy TỰ SINH tool_call, không gọi upstream => 100% đúng format, nhanh hơn,
    rẻ hơn. Model chỉ còn việc ở bước "answer"/"act" -- nơi cần phán đoán.
  * GUIDE (bước cần model quyết định: viết edit, chọn grep pattern ...):
    gọi upstream với directive "STEP i/n" kèm template tool_call điền sẵn, bắt
    buộc có tool_call, lọc call sai loại, retry, cuối cùng fallback tự sinh
    (nếu bước xác định được) hoặc trả nguyên kết quả (không bao giờ kẹt).

Tuỳ chọn: hỏi lại client khi yêu cầu mơ hồ (flow_ask_when_unclear).

CÀI ĐẶT (2 dòng trong gemini_web2api/__main__.py, sau load_config + args):

    from gemini_web2api import flow_patch
    flow_patch.install()

Đặt file này cạnh server.py (gemini_web2api/flow_patch.py). Cấu hình trong
config.json (đều tuỳ chọn, xem _DEFAULTS bên dưới). Tắt nhanh:
    {"flow_enabled": false}
Chưa phủ: endpoint Google native (/v1beta) vì có vòng retry riêng.
"""
import difflib
import functools
import json
import os
import posixpath
import re
import uuid
from collections import Counter

from gemini_web2api import tools as T
from gemini_web2api.config import CONFIG
from concurrent.futures import ThreadPoolExecutor

from gemini_web2api.gemini import log_s1 as log, generate

# __all__ removed for unified export

_DEFAULTS = {
    "flow_enabled": True,
    # True: bước xác định được thì proxy tự sinh tool_call, khỏi gọi upstream.
    "flow_drive": True,
    "flow_explain": True,
    "flow_review_run_tests": True,
    # None = autodetect (package.json -> npm test, .py -> unittest, else skip tests)
    "flow_test_command": None,
    "flow_survey_pattern": "**/*.{py,js,jsx,mjs,cjs,ts,tsx,go,rs,java,kt,rb,php,cs,c,cc,cpp,h,hpp,swift}",
    "flow_read_batch": 6,
    "flow_max_review_files": 40,
    "flow_max_retries": 1,
    "flow_review_include_tests": False,
    # CHANGE: max edit/write calls per turn before the proxy stops the
    # edit-test loop and makes the model report honestly.
    "flow_max_acts": 6,
    "flow_answer_directive": True,
    # Review lớn: chia nội dung đã đọc thành nhóm nhỏ, mỗi nhóm một lượt gọi
    # model (chỉ để tìm lỗi), proxy kiểm chứng trích dẫn rồi tự gộp báo cáo.
    "flow_mapreduce": True,
    "flow_mapreduce_min_bytes": 30000,
    "flow_chunk_bytes": 30000,
    "flow_chunk_files": 4,
    "flow_map_workers": 2,
    # Model riêng cho bước map (tìm lỗi). None = dùng model của request.
    # Vd "gemini-3.5-flash" (cần ticket 'flash' trong model_tickets).
    "flow_map_model": None,
    # Opt-in: yêu cầu quá mơ hồ -> trả câu hỏi chọn pattern 1-4 cho client.
    "flow_ask_when_unclear": False,
}


def _cfg(key):
    return CONFIG.get(key, _DEFAULTS[key])


# --------------------------------------------------------------------------
# Phân loại tool theo "kind" từ tên (client nào cũng khác tên: read/Read/
# read_file/readFile, glob/list_files, bash/run_command ...).
# --------------------------------------------------------------------------

_META_SUBSTR = ("todo", "web", "task", "question", "skill", "agent", "notebook",
                "plan")
_KIND_WORDS = [
    ("act", {"edit", "write", "patch", "replace", "insert", "append", "create",
             "apply", "delete", "move", "rename", "mkdir", "editor", "save"}),
    ("exec", {"bash", "shell", "exec", "run", "command", "terminal", "cmd",
              "powershell"}),
    ("search", {"grep", "ripgrep", "rg", "search"}),
    ("survey", {"glob", "list", "ls", "find", "tree", "dir", "directory"}),
    ("read", {"read", "cat", "view", "open", "head", "tail", "show"}),
]
_KIND_SUBSTR = [("act", ("edit", "write", "patch", "replace")),
                ("exec", ("bash", "shell", "exec")),
                ("search", ("grep",)),
                ("survey", ("glob",)),
                ("read", ("read",))]


def _tokens(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def tool_kind_s1(name) -> str:
    """read | survey | search | act | exec | other"""
    low = str(name or "").lower()
    toks = set(_tokens(name))
    if any(m in low for m in _META_SUBSTR):
        return "other"
    for kind, words in _KIND_WORDS:
        if toks & words:
            return kind
    for kind, subs in _KIND_SUBSTR:
        if any(s in low for s in subs):
            return kind
    return "other"


_PATH_KEYS = ["filePath", "file_path", "path", "file", "filename", "file_name",
              "target_file", "target"]
_PATTERN_KEYS = ["pattern", "query", "glob", "regex", "search"]
_CMD_KEYS = ["command", "cmd", "script"]
_OFFSET_KEYS = ["offset", "start_line", "startLine", "line_offset", "start"]
_LIMIT_KEYS = ["limit", "count", "max_lines", "maxLines", "num_lines", "end_line"]


def _pick(props, keys):
    low = {str(p).lower(): p for p in (props or ())}
    for k in keys:
        if k.lower() in low:
            return low[k.lower()]
    return None


class ToolMap:
    """Các tool client khai báo, gom theo kind."""

    def __init__(self, allowed_names, schemas, required):
        self.schemas = schemas or {}
        self.required = required or {}
        self.by_kind = {}
        for n in sorted(allowed_names or [], key=lambda x: (len(x), x)):
            self.by_kind.setdefault(tool_kind(n), []).append(n)

    def first(self, kind):
        lst = self.by_kind.get(kind)
        return lst[0] if lst else None

    def names(self, kinds):
        out = []
        for k in sorted(kinds):
            out.extend(self.by_kind.get(k, []))
        return out

    # ------------------------------------------------------------------
    # Adapter (proposal 3): the client decides the schema; the proxy decides
    # the call. Model output is normalised to the client's real parameter
    # names, unknown keys are dropped and required parameters are checked
    # BEFORE the call reaches the client.
    # ------------------------------------------------------------------

    def adapt(self, call):
        """``(call, problems)`` -- normalise one tool_call to this client's schema."""
        if not isinstance(call, dict):
            return call, ["tool call is not an object"]
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        args = T._tool_arguments_dict(fn)
        if not isinstance(args, dict):
            return call, [f"{name or '?'}: arguments are not a JSON object"]
        props = self.schemas.get(name)
        props = set(props) if props else set()
        required = set(self.required.get(name, ()) or ())
        out = {}
        problems = []

        def pick_value(keys, allow_empty=False):
            for k in keys:
                v = args.get(k)
                if isinstance(v, str) and (v.strip() or allow_empty):
                    return v
                if isinstance(v, int) and not isinstance(v, bool):
                    return v
            return None

        def put(keys, value, fallback):
            if value is None:
                return
            target = _pick(props, keys)
            if target is None and not props:
                # No schema: keep the key the model actually used rather than
                # renaming it to a canonical name the client may not accept.
                target = next((k for k in keys if k in args), fallback)
            if target:
                out[target] = value

        kind = tool_kind(name)
        path = pick_value(_PATH_KEYS)
        command = pick_value(_CMD_KEYS)
        pattern = pick_value(_PATTERN_KEYS)
        old = pick_value(_OLD_KEYS, True)
        new = pick_value(_NEW_KEYS, True)
        content = pick_value(_CONTENT_KEYS, True)
        offset = pick_value(_OFFSET_KEYS)
        limit = pick_value(_LIMIT_KEYS)
        description = pick_value(["description"])

        if kind == "read":
            put(_PATH_KEYS, path, "filePath")
            put(_OFFSET_KEYS, offset, "offset")
            put(_LIMIT_KEYS, limit, "limit")
            if not path:
                problems.append(f"{name}: missing required parameter for the file path")
        elif kind in ("survey", "search"):
            put(_PATTERN_KEYS, pattern, "pattern")
            put(_PATH_KEYS, path, "path")
            if not pattern and not path:
                problems.append(f"{name}: missing required parameter pattern/path")
        elif kind == "exec":
            put(_CMD_KEYS, command, "command")
            put(["description"], description, "description")
            if not command:
                problems.append(f"{name}: missing required parameter for the command")
        elif kind == "act":
            put(_PATH_KEYS, path, "filePath")
            edit_like = bool(set(_tokens(name)) & {
                "edit", "write", "patch", "replace", "insert", "append",
                "apply", "save", "editor"})
            if props and not (_pick(props, _PATH_KEYS) or _pick(props, _OLD_KEYS)
                              or _pick(props, _CONTENT_KEYS)):
                edit_like = False  # patch-style tool: nothing per-file to validate
            # An edit uses old/new; a write uses content. Keep whichever the
            # client schema declares, and never mix both shapes silently. A
            # move/rename/delete/mkdir call is not an edit of file contents.
            if edit_like and old is not None and not (old == "" and content is not None):
                put(_OLD_KEYS, old, "oldString")
                put(_NEW_KEYS, new, "newString")
            elif edit_like:
                put(_CONTENT_KEYS, content, "content")
            if edit_like:
                if not path:
                    problems.append(f"{name}: missing required parameter for the file path")
                if old is None and content is None:
                    problems.append(
                        f"{name}: the edit has neither the text to replace "
                        "(oldString) nor the new file content (content)")

        # Preserve declared parameters the model already used correctly
        # (timeout, workdir, ...). Unknown keys are dropped when the schema is
        # known: the client would reject them anyway.
        for k, v in args.items():
            if props and k not in props:
                continue
            if k in out:
                continue
            if any(k in group for group in
                   (_PATH_KEYS, _CMD_KEYS, _PATTERN_KEYS, _OLD_KEYS,
                    _NEW_KEYS, _CONTENT_KEYS, _OFFSET_KEYS, _LIMIT_KEYS)):
                continue
            out[k] = v

        for req in sorted(required):
            if req in out:
                continue
            if str(req).lower() == "description":
                out[req] = description or "Run"
                continue
            problems.append(f"{name}: missing required parameter '{req}'")

        if not out and kind != "other":
            problems.append(f"{name}: no usable arguments after adapting")

        new_args = json.dumps(out, ensure_ascii=False)
        if new_args == (fn.get("arguments") or "") and not problems:
            return call, problems
        return {**call, "function": {**fn, "arguments": new_args}}, problems

    def make_call(self, kind, path=None, pattern=None, command=None,
                  description=None):
        """Tool_call (định dạng OpenAI) hoặc None nếu không sinh được."""
        name = self.first(kind)
        if not name:
            return None
        props = self.schemas.get(name) or set()
        args = {}
        if kind == "read":
            key = _pick(props, _PATH_KEYS) or (None if props else "filePath")
            if not key or not path:
                return None
            args[key] = path
        elif kind in ("survey", "search"):
            key = _pick(props, _PATTERN_KEYS)
            if key is None and not props:
                key = "pattern"
            if key and pattern:
                args[key] = pattern
            else:
                pkey = _pick(props, _PATH_KEYS)
                if kind == "survey" and pkey:
                    args[pkey] = "."
                else:
                    return None
        elif kind == "exec":
            key = _pick(props, _CMD_KEYS) or (None if props else "command")
            if not key or not command:
                return None
            args[key] = command
            dkey = _pick(props, ["description"])
            if dkey:
                args[dkey] = description or "Run command"
        else:
            return None
        for req in self.required.get(name, ()):
            if req in args:
                continue
            if str(req).lower() == "description":
                args[req] = description or "Run"
            else:
                return None
        return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)}}
_ToolMap_adapt_orig = ToolMap.adapt


# --------------------------------------------------------------------------
# Bằng chứng trong lượt hiện tại (kể từ message user cuối)
# --------------------------------------------------------------------------

def _answered_calls_s1(messages):
    """[{name, kind, args, result}] cho mọi call ĐÃ có tool result trong lượt."""
    start = T._last_user_index(messages)
    calls, out = {}, []
    for msg in messages[start + 1:]:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    fn = tc.get("function") or {}
                    calls[tc["id"]] = (fn.get("name") or "",
                                       T._tool_arguments_dict(fn))
        elif msg.get("role") == "tool":
            hit = calls.get(msg.get("tool_call_id"))
            if hit:
                out.append({"name": hit[0], "kind": tool_kind(hit[0]),
                            "args": hit[1],
                            "result": T._stringify_content(msg.get("content"))})
    return out


def _call_path(args):
    for k in _PATH_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\\", "/")
    return None


def _call_command(args):
    for k in _CMD_KEYS:
        v = args.get(k)
        if isinstance(v, str):
            return v
    return json.dumps(args, ensure_ascii=False)


_LIST_CMD_RE = re.compile(r"\b(ls|find|dir|tree|git\s+ls-files)\b")


_PATH_LINE_RE = re.compile(
    r'^\s*(?:[-*]\s+)?((?:[A-Za-z]:)?[/\\]?(?:[^\s:*?"<>|/\\]+[/\\])*'
    r'[^\s:*?"<>|/\\]+\.[A-Za-z0-9]{1,6})\s*$')
_READ_FAIL_RE = re.compile(
    r"^\s*(?:<?error|file not found|no such file|enoent|cannot (?:read|find|open)"
    r"|failed|not found)", re.IGNORECASE)


def _read_ok_s1(e):
    """Tool result của một lệnh read có thật sự chứa nội dung không."""
    return bool(e["result"].strip()) and not _READ_FAIL_RE.match(e["result"])




def _read_set(ev):
    return {_call_path(e["args"]).lower() for e in ev
            if e["kind"] == "read" and _call_path(e["args"])}


def _surveyed(ev):
    return any(e["kind"] in ("survey", "search") or (
        e["kind"] == "exec" and _LIST_CMD_RE.search(_call_command(e["args"])))
        for e in ev)


def _survey_count(ev):
    return sum(1 for e in ev if e["kind"] in ("survey", "search") or (
        e["kind"] == "exec" and _LIST_CMD_RE.search(_call_command(e["args"]))))


def _is_test_cmd(cmd):
    configured = (_cfg("flow_test_command") or "").strip()
    return bool(_TEST_CMD_RE.search(cmd) or (configured and configured in cmd))




def _tests_ran_after_act(ev):
    last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"),
                   default=-1)
    return any(i > last_act and e["kind"] == "exec"
               and _is_test_cmd(_call_command(e["args"]))
               for i, e in enumerate(ev))


_SRC_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
            ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift")
_IGNORE_SEG = {"__pycache__", "node_modules", ".git", "venv", ".venv", "env",
               "site-packages", "dist", "build", ".tox", ".mypy_cache",
               "vendor", ".idea"}
_TEST_SEG = {"tests", "test", "__tests__", "spec"}


def _core_sources(files, include_tests, limit):
    cand = []
    for f in files:
        p = f.replace("\\", "/")
        segs = p.split("/")
        if any(s in _IGNORE_SEG for s in segs[:-1]):
            continue
        if not p.lower().endswith(_SRC_EXT) or segs[-1] == "__init__.py":
            continue
        if not include_tests and (
                any(s in _TEST_SEG for s in segs[:-1])
                or segs[-1].startswith("test_")
                or segs[-1].endswith(("_test.py", ".test.js", ".test.ts",
                                      ".spec.js", ".spec.ts"))):
            continue
        if p not in cand:
            cand.append(p)
    if not cand:
        return []
    exts = Counter(os.path.splitext(p)[1].lower() for p in cand)
    top = exts.most_common(1)[0][1]
    keep = {e for e, n in exts.items() if n * 10 >= top * 3}
    core = [p for p in cand if os.path.splitext(p)[1].lower() in keep]
    entry = {"index", "main", "app", "server", "cli", "__main__", "mod", "lib"}
    low_dirs = {"scripts", "script", "utils", "util", "tools", "examples",
                "example", "docs", "bench", "benchmarks", "migrations", "types"}

    def key(item):
        i, p = item
        parts = p.lower().split("/")
        base = posixpath.splitext(parts[-1])[0]
        tier = 0 if base in entry else (2 if (set(parts[:-1]) & low_dirs
                                              or base in low_dirs) else 1)
        return (tier, i)

    return [p for _i, p in sorted(enumerate(core), key=key)][:limit]


_STOP = set(
    "the and for with this that these those please can you could would should "
    "how what where why when which does did from into about code file files "
    "project repo update change fix add make implement review check cho toi "
    "minh ban hay nay trong cua nhung cac mot nhu nao khong duoc va la co "
    "giup defined define defines used uses work works function class method".split())




def _rank(files, kws, hits=None):
    scored = []
    for i, f in enumerate(files):
        low = f.lower()
        s = 10 * sum(1 for k in kws if k in low) + (hits or {}).get(f, 0)
        if s > 0:
            scored.append((-s, i, f))
    return [f for _s, _i, f in sorted(scored)]


def _resolve(name, listing):
    """Tên file user nêu -> đường dẫn trong listing nếu khớp, else giữ nguyên."""
    n = name.replace("\\", "/")
    for f in listing:
        if T._path_seen(n, [f]):
            return f
    return n


# --------------------------------------------------------------------------
# Step
# --------------------------------------------------------------------------

# ---- flow_fix: act gating, test-command detection, grep ranking ----------
_ACT_FAIL_RE = re.compile(
    r"(?:^\s*<?error|\bfailed\b|old_?string.{0,40}not found|"
    r"could not find|cannot find|not found in|multiple matches|"
    r"found \d+ matches|must be different|no changes)", re.IGNORECASE)
_OLD_KEYS = ("oldString", "old_string", "oldText", "old_text", "old_str")
_NEW_KEYS = ("newString", "new_string", "newText", "new_text", "new_str")
_LINENO_RE = re.compile(r"^\s*\d+(?:: ?|\s*\| ?|\t|\u2192)")


def _act_ok_s1(e):
    # An edit/write whose result reports a failure must not count as done.
    return not _ACT_FAIL_RE.search((e.get("result") or "")[:400])


def _failed_act_hint(ev):
    for e in reversed(ev):
        if e["kind"] == "act":
            if _act_ok(e):
                return ""
            msg = " ".join((e.get("result") or "").split())[:300]
            return (" Your previous edit FAILED with: " + msg +
                    " -- re-copy the exact text from the file you read.")
    return ""


def _grep_hits(ev, files):
    # {file in `files`: number of grep hits} from this turn's search results.
    counts = {}
    for e in ev:
        if e["kind"] != "search":
            continue
        body = e["result"].replace("\\", "/")
        for hf in T._files_in(body):
            counts[hf] = counts.get(hf, 0) + body.count(hf)
    hits = {}
    for f in files:
        n = sum(c for hf, c in counts.items() if T._path_seen(hf, [f]))
        if n:
            hits[f] = n
    return hits


def _strip_linenos(text):
    # All-or-nothing: only strip when EVERY non-empty line carries a prefix.
    lines = text.split("\n")
    nonempty = [ln for ln in lines if ln.strip()]
    if nonempty and all(_LINENO_RE.match(ln) for ln in nonempty):
        return "\n".join(_LINENO_RE.sub("", ln, count=1) for ln in lines)
    return text


_READ_HEADER_RE = re.compile(
    r"^\s*(?:Read\s+file\s+[^\n]+?(?:,\s*lines\s+\d+\s*-\s*\d+)?|<file>)\s*$",
    re.IGNORECASE,
)
_READ_FOOTER_RE = re.compile(
    r"^\s*(?:</file>|\(\s*end\s+of\s+file\b[^()\n]*\))\s*$",
    re.IGNORECASE,
)


def _strip_read_envelope(text):
    if not isinstance(text, str) or not text:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and _READ_HEADER_RE.match(lines[0]):
        lines.pop(0)
    while lines and _READ_FOOTER_RE.match(lines[-1]):
        lines.pop()
    return "\n".join(lines)


def _clean_file_text(text):
    text = _strip_read_envelope(text or "")
    trunc = globals().get("_FF4_TRUNC_RE")
    if trunc is not None:
        text = trunc.sub("", text).rstrip()
    lines = text.replace("\r\n", "\n").split("\n")
    if T._already_numbered(lines):
        lines = [_LINENO_RE.sub("", ln, count=1) for ln in lines]
    return "\n".join(lines)




class Step:
    def __init__(self, pattern, name, index, total, kinds, instruction,
                 calls=None, drive=False, tool_names=()):
        self.pattern, self.name = pattern, name
        self.index, self.total = index, total
        self.kinds = set(kinds)
        self.instruction = instruction
        self.calls = calls or None
        self.drive = bool(drive and calls)
        self.tool_names = list(tool_names)

    def directive(self):
        head = f"[FLOW {self.pattern} step {self.index}/{self.total} -- {self.name.upper()}]"
        out = [head + " " + self.instruction]
        if self.tool_names:
            hint = " For edits, keep oldString short (1-5 unique anchor lines); do NOT copy large chunks." if self.name == "act" else ""
            out.append("Allowed tools this turn: " + ", ".join(self.tool_names)
                       + ". Output ONLY tool_call block(s), no prose." + hint)
        if self.calls:
            out.append("Emit exactly:")
            for c in self.calls:
                fn = c["function"]
                payload = {"name": fn["name"],
                           "arguments": json.loads(fn["arguments"])}
                out.append("```tool_call\n"
                           + json.dumps(payload, ensure_ascii=False) + "\n```")
        return "\n".join(out)


# --------------------------------------------------------------------------
# Phân loại pattern (+ hỏi lại client nếu mơ hồ)
# --------------------------------------------------------------------------

CLARIFY_MARK = "(flow:clarify)"
_CLARIFY_MAP = {"1": "GENERIC_REVIEW", "2": "CHANGE", "3": "EXPLAIN",
                "4": "TOOL_DEMAND"}
_VI_RE = re.compile("[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡ"
                    "ùúụủũưừứựửữỳýỵỷỹđ]")
_QUESTION_RE = re.compile(
    r"\b(how|where|why|what|which|explain|does|show me|giai thich|o dau|"
    r"nhu the nao|hoat dong|la gi|tai sao|dinh nghia)\b")
_PROJECT_RE = re.compile(
    r"\b(this|the|our)\s+(project|repo|repository|codebase|code|proxy|server|"
    r"module|function|class|file)\b|\b(du an|repo|codebase|ma nguon|trong code|"
    r"ham|class|module)\b")
_VAGUE_RE = re.compile(
    r"\b(improve|optimi[sz]e|clean|handle|help|toi uu|cai thien|xu ly|giup|"
    r"lam|do)\b")




class _Ctx:
    pass


def _base_analyze(messages):
    idx = T._last_user_index(messages)
    if idx < 0:
        return None
    ctx = _Ctx()
    ctx.idx = idx
    ctx.last = T._stringify_content(messages[idx].get("content", ""))
    ctx.request = ctx.last
    forced = _clarified(messages, idx)
    if forced:
        ctx.pattern, ctx.request = forced[0], forced[1] or ctx.last
    else:
        ctx.pattern = T.detect_task_pattern(messages)
        if not ctx.pattern and _cfg("flow_explain") and _is_code_question(ctx.last):
            ctx.pattern = "EXPLAIN"
    req_text = ctx.request or ""
    strip_fn = globals().get("_strip_pasted_content")
    if callable(strip_fn):
        stripped = strip_fn(req_text)
        if stripped.strip():
            req_text = stripped
    ctx.scannable = T._strip_code_spans(req_text)
    ctx.folded = re.sub(r"\s+", " ", T._fold_diacritics(ctx.scannable)).strip()
    return ctx


def _clarified(messages, idx):
    if idx < 1:
        return None
    prev = messages[idx - 1]
    if not (isinstance(prev, dict) and prev.get("role") == "assistant"
            and CLARIFY_MARK in T._stringify_content(prev.get("content", ""))):
        return None
    ans = T._fold_diacritics(
        T._stringify_content(messages[idx].get("content", ""))).lower().strip()
    pattern = None
    m = re.match(r"^\W*([1-4])\b", ans)
    if m:
        pattern = _CLARIFY_MAP[m.group(1)]
    elif re.search(r"review|audit|soat|danh gia", ans):
        pattern = "GENERIC_REVIEW"
    elif re.search(r"fix|implement|sua|them|code", ans):
        pattern = "CHANGE"
    elif re.search(r"explain|giai thich|tim hieu", ans):
        pattern = "EXPLAIN"
    if not pattern:
        return None
    original = None
    if idx >= 2 and isinstance(messages[idx - 2], dict) \
            and messages[idx - 2].get("role") == "user":
        original = T._stringify_content(messages[idx - 2].get("content", ""))
    return pattern, original


def clarification_text_s1(messages):
    """Câu hỏi chọn pattern nếu yêu cầu mơ hồ, else None (opt-in)."""
    if not _cfg("flow_ask_when_unclear") or not messages:
        return None
    last = messages[-1]
    if not (isinstance(last, dict) and last.get("role") == "user"):
        return None
    if any(isinstance(m, dict) and (m.get("tool_calls") or m.get("role") == "tool")
           for m in messages):
        return None
    ctx = _analyze(messages)
    if ctx is None or ctx.pattern:
        return None
    text = ctx.last.strip()
    folded = T._fold_diacritics(text.lower())
    if len(text) > 120 or T._files_in(text) or not _VAGUE_RE.search(folded):
        return None
    if _VI_RE.search(text.lower()):
        return ("Mình chưa rõ yêu cầu này thuộc dạng nào. Bạn chọn giúp:\n"
                "1. Review / audit source\n2. Implement / sửa code\n"
                "3. Giải thích / tìm hiểu code\n4. Chạy một lệnh / tool cụ thể\n"
                f"Trả lời 1-4 hoặc mô tả rõ hơn. {CLARIFY_MARK}")
    return ("I am not sure which kind of task this is. Please pick:\n"
            "1. Review / audit the source\n2. Implement / change code\n"
            "3. Explain / understand the code\n4. Run a specific command/tool\n"
            f"Reply 1-4 or describe it more precisely. {CLARIFY_MARK}")


# --------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------

def _answer_step(pattern, ev, total, index, skipped=None, final=False):
    if not ev or not _cfg("flow_answer_directive"):
        return None
    read = sorted({_call_path(e["args"]) for e in ev
                   if e["kind"] == "read" and _call_path(e["args"])
                   and _read_ok(e)})
    if pattern != "CHANGE" and not read:
        instr = ("NO file was successfully read in this turn (every read "
                 "failed or none was possible). Say that plainly and ask the "
                 "user for the correct project path. Do NOT describe or "
                 "evaluate any code: you have not seen any.")
    elif pattern == "CHANGE" and final:
        instr = ("Edit budget exhausted. Do NOT call any tool. Reply in plain "
                 "text: what was changed, whether the tests passed, and exactly "
                 "what still fails. Do not claim success you have not seen.")
    elif pattern == "CHANGE":
        instr = ("Evidence complete. If the last test run failed, fix the "
                 "cause with one more edit tool_call; otherwise reply in plain "
                 "text: what you changed and the test result. No other tool calls.")
    else:
        instr = ("Evidence collected (successfully read: "
                 + (", ".join(read[:30]) or "none") + "). Answer NOW in plain "
                 "text with NO tool_call block. Base every claim on those "
                 "files. Report format: each finding as `file:line -- verbatim "
                 "quote -- why it is a bug`. If you found none, write exactly "
                 "'no critical bug found in the files read' and list the files "
                 "read. A passing test suite does not prove absence of bugs. "
                 "Only claim a rule/contract is respected if you compared it "
                 "to the code you read; never cite a document you did not read.")
        if skipped:
            instr += (" NOT read (say your coverage is PARTIAL and name them): "
                      + ", ".join(skipped[:20]) + ".")
    st = Step(pattern, "answer", index, total, (), instr)
    st.skipped = list(skipped or [])
    return st


def _read_calls(tm, files):
    calls = [tm.make_call("read", path=f) for f in files]
    return calls if calls and all(calls) else None


def _base_plan_for(messages, tm, ctx=None):
    """Bước tiếp theo bắt buộc, hoặc None (model được trả lời tự do)."""
    ctx = ctx or _analyze(messages)
    if ctx is None or not ctx.pattern:
        return None
    p = ctx.pattern
    ev = _answered_calls(messages)
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    batch = max(1, int(_cfg("flow_read_batch")))
    test_cmd = _test_command(listing)
    named = [f for f in T._files_in(ctx.scannable)
             if f.lower() not in T._NON_FILE_NAMES
             and f.lower() not in T._created_files(ctx.scannable, ctx.folded)]
    test_cmd = _test_command(listing + named + [
        p for p in (_call_path(e["args"]) for e in ev) if p])

    def survey_step(index, total):
        retry = _survey_count(ev) >= 1  # first listing came back empty
        glob_call = tm.make_call("survey", pattern="**/*" if retry
                                 else _cfg("flow_survey_pattern"))
        exec_call = tm.make_call("exec", command=_SURVEY_CMD,
                                 description="List project files")
        call = (exec_call or glob_call) if retry else (glob_call or exec_call)
        if not call and not tm.names({"survey", "search", "exec"}):
            return None
        return Step(p, "survey", index, total, {"survey", "search", "exec"},
                    "List the project files before anything else.", [call] if call else None,
                    True, tm.names({"survey", "search", "exec"}))

    def tests_step(index, total):
        if not test_cmd:
            return None
        call = tm.make_call("exec", command=test_cmd, description="Run unit tests")
        if not call:
            return None
        return Step(p, "tests", index, total, {"exec"},
                    "Run the unit tests.", [call], True, tm.names({"exec"}))

    def read_step(files, index, total, drive=True):
        calls = _read_calls(tm, files)
        return Step(p, "read", index, total, {"read"},
                    "Read these files fully before answering: " + ", ".join(files) + ".",
                    calls, drive, tm.names({"read"}))

    if p == "GENERIC_REVIEW":
        if not listing and _survey_count(ev) < 2:
            s = survey_step(1, 4)
            if s:
                return s
        if (_cfg("flow_review_run_tests") and tm.first("exec")
                and not any(e["kind"] == "exec" and _is_test_cmd(_call_command(e["args"]))
                            for e in ev)):
            s = tests_step(2, 4)
            if s:
                return s
        core = _core_sources(listing, _cfg("flow_review_include_tests"),
                             int(_cfg("flow_max_review_files")))
        unread = [f for f in core if not T._path_seen(f, read_set)]
        if unread and tm.first("read"):
            return read_step(unread[:batch], 3, 4)
        skipped = [f for f in _core_sources(
            listing, _cfg("flow_review_include_tests"), 10 ** 6)
            if not T._path_seen(f, read_set)]
        if not read_set and tm.first("read"):
            # No recognised source files: read whatever the listing has
            # (README/AGENTS/config...) rather than answer from nothing.
            fallback = [f for f in listing if not any(
                s in _IGNORE_SEG for s in f.replace("\\", "/").split("/")[:-1])]
            if fallback:
                return read_step(fallback[:batch], 3, 4)
        return _answer_step(p, ev, 4, 4, skipped)

    if p == "FILE_REVIEW":
        files = [_resolve(f, listing) for f in named]
        unread = [f for f in files if not T._path_seen(f, read_set)]
        if unread and tm.first("read"):
            return read_step(unread[:batch], 1, 2)
        return _answer_step(p, ev, 2, 2)

    if p == "CHANGE":
        known = [_resolve(f, listing) for f in named]
        if not known and not _surveyed(ev):
            s = survey_step(1, 5)
            if s:
                return s
        if known:
            unread = [f for f in known if not T._path_seen(f, read_set)]
            if unread and tm.first("read"):
                return read_step(unread[:batch], 2, 5)
        elif not read_set and tm.first("read"):
            kws = _keywords(ctx.request)
            if (kws and tm.first("search")
                    and not any(e["kind"] == "search" for e in ev)):
                scall = tm.make_call("search", pattern="(?i)" + "|".join(
                    re.escape(k) for k in kws[:3]))
                if scall:
                    skinds = {"survey", "search", "exec"}
                    return Step(p, "search", 2, 5, skinds,
                                "Locate the code to change with grep first.",
                                [scall], True, tm.names(skinds))
            core = _core_sources(listing, False, 200)
            ranked = _rank(core, kws, _grep_hits(ev, core))[:2]
            if ranked:
                return read_step(ranked, 2, 5)
            return read_step(core[:2], 2, 5, drive=False) if core else Step(
                p, "read", 2, 5, {"read"},
                "Read the file(s) you are going to change.", None, False,
                tm.names({"read"}))
        acts = [e for e in ev if e["kind"] == "act"]
        if len(acts) >= int(_cfg("flow_max_acts")) and (
                not _act_ok(acts[-1]) or _tests_ran_after_act(ev)):
            return _answer_step(p, ev, 5, 5, final=True)
        if tm.first("act") and (not any(_act_ok(e) for e in acts)
                                or (acts and not _act_ok(acts[-1]))):
            return Step(p, "act", 3, 5, {"act"},
                        "Make the change now with ONE edit/write tool_call per "
                        "file, copying old text exactly from the file you read "
                        "(no line-number prefixes). "
                        "CRITICAL: Do NOT break or alter existing features/routes. "
                        "When adding a new endpoint, create a NEW branch (e.g. new `elif`) "
                        "instead of merging it into existing ones. Preserve dynamic "
                        "variables (like __version__) for existing paths." + _failed_act_hint(ev),
                        None, False, tm.names({"act"}))
        edited_src = any(e["kind"] == "act" and _act_ok(e)
                         and (_call_path(e["args"]) or "")
                         .lower().endswith(_SRC_EXT) for e in ev)
        if (edited_src and tm.first("exec") and not _tests_ran_after_act(ev)
                and not T._NO_TEST_RE.search(ctx.folded)):
            s = tests_step(4, 5)
            if s:
                return s
        return _answer_step(p, ev, 5, 5)

    if p == "EXPLAIN":
        kws = _keywords(ctx.request)
        if not _surveyed(ev):
            call = None
            if kws and tm.first("search"):
                call = tm.make_call("search", pattern="(?i)" + "|".join(
                    re.escape(k) for k in kws[:3]))
            call = call or tm.make_call("survey", pattern=_cfg("flow_survey_pattern"))
            kinds = {"survey", "search", "exec"}
            return Step(p, "search", 1, 3, kinds,
                        "Locate the relevant code with grep/glob first.",
                        [call] if call else None, True, tm.names(kinds))
        if not read_set and tm.first("read"):
            hits = _search_hits(ev)
            pool = list(hits) or listing
            pool = _core_sources(pool, True, 400) or pool
            ranked = _rank(pool, kws, hits)[:2] or pool[:1]
            if ranked:
                return read_step(ranked, 2, 3)
        return _answer_step(p, ev, 3, 3)

    if p == "TOOL_DEMAND":
        if not ev:
            kinds = {"read", "survey", "search", "act", "exec"}
            return Step(p, "call", 1, 1, kinds,
                        "The user asked you to use tools: call one now.",
                        None, False, tm.names(kinds))
        return None
    return None


# --------------------------------------------------------------------------
# Map-reduce review: model yếu chỉ nhìn 1-4 file mỗi lượt
# --------------------------------------------------------------------------



def _make_chunks_s1(files, max_bytes, max_files):
    """Nhóm file theo thư mục; file quá lớn được cắt theo dòng."""
    out, cur, size = [], [], 0
    for p, body in sorted(files, key=lambda x: x[0]):
        n = len(body.encode("utf-8", "ignore"))
        if n > max_bytes:
            if cur:
                out.append(cur)
                cur, size = [], 0
            piece, psize, part = [], 0, 1
            for ln in body.split("\n"):
                piece.append(ln)
                psize += len(ln.encode("utf-8", "ignore")) + 1
                if psize >= max_bytes:
                    out.append([(f"{p} (part {part})", "\n".join(piece))])
                    piece, psize, part = [], 0, part + 1
            if piece:
                out.append([(f"{p} (part {part})", "\n".join(piece))])
            continue
        if cur and (size + n > max_bytes or len(cur) >= max_files):
            out.append(cur)
            cur, size = [], 0
        cur.append((p, body))
        size += n
    if cur:
        out.append(cur)
    return out


def _map_prompt(request, chunk):
    parts = ["You are a meticulous code reviewer. User request: "
             + request.strip()[:300] + "\n\n"
             "Find CRITICAL bugs ONLY in the files below (crash, data loss or "
             "corruption, security hole, race condition, wrong logic). Rules:\n"
             "- Report only what is visible in the text below.\n"
             "- One finding per line, exactly: - path:line -- `exact quoted code "
             "line` -- why it is a bug\n"
             "- The quote MUST be copied verbatim from the file (without the "
             "line number).\n"
             "- If you find nothing critical, output exactly: NONE\n"
             "- No tool calls, no summary of what the code does, no praise.\n"]
    for p, body in chunk:
        parts.append(f"\n===== FILE: {p} =====\n{body}\n===== END {p} =====")
    return T.defuse_extension_words("".join(parts))


def _split_findings(text):
    items, cur = [], None
    for ln in (text or "").splitlines():
        if re.match(r"^\s*(?:[-*\u2022]|\d+[.)])\s+\S", ln):
            if cur:
                items.append(cur)
            cur = ln.strip()
        elif cur and ln.strip():
            cur += " " + ln.strip()
    if cur:
        items.append(cur)
    return items




def _test_summary(tests):
    if not tests:
        return []
    body = tests[-1]["result"]
    pat = re.compile(
        r"^(?:\S\s+|#\s*)?(?:tests?|suites?|pass(?:ed)?|fail(?:ed)?|cancelled|skipped|"
        r"todo|duration_ms)\s*[:=]?\s*\d+|^Ran \d+|^OK\b|^FAILED\b|"
        r"^(?:\S\s+)?\d+\s+(?:passed|failed|passing|failing)\b|"
        r"^Tests?:\s+\d+|^test result:", re.IGNORECASE)
    lines = [re.sub(r"^(?:\S\s+|#\s*)(?=[A-Za-z0-9])", "", ln.strip()) for ln in body.splitlines()
             if pat.search(ln.strip())]
    keep = [ln for ln in lines
            if not ln.lower().startswith(("duration", "suites"))]
    return keep[:6] or [body.strip()[-200:]]


def _map_reduce_s1(messages, step, ctx, gen, client_gone=None):
    files, tests = _review_materials(messages)
    total = sum(len(b.encode("utf-8", "ignore")) for _p, b in files)
    if not files or total < int(_cfg("flow_mapreduce_min_bytes")):
        return None
    chunks = _make_chunks(files, int(_cfg("flow_chunk_bytes")),
                          int(_cfg("flow_chunk_files")))
    log(f"[flow] map-reduce review: {len(files)} files, {total} bytes, "
        f"{len(chunks)} chunk(s)")

    def work(chunk):
        if client_gone is not None and client_gone.is_set():
            return None, "client disconnected"
        for attempt in range(2):
            try:
                text = gen(_map_prompt(ctx.request, chunk))
                if text and not T.looks_like_canned_failure(text):
                    return text, None
            except Exception as e:
                err = str(e)
                log(f"[flow] map chunk attempt {attempt + 1} failed: {e}")
        return None, "upstream failed"

    workers = max(1, int(_cfg("flow_map_workers")))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, chunks))

    findings, dropped, failed, leads, clean = [], 0, [], [], 0
    for chunk, (text, err) in zip(chunks, results):
        names = ", ".join(p for p, _b in chunk)
        if text is None:
            failed.append(names)
            continue
        kept, d = _verify_findings(text, chunk)
        findings.extend(kept)
        dropped += d
        is_none = bool(re.fullmatch(r"\W*NONE\W*", (text or "").strip(),
                                    re.IGNORECASE))
        if is_none:
            clean += 1
        elif not kept and not d:
            # Văn xuôi / định dạng lạ: không xác minh được -> KHÔNG tính là sạch.
            dropped += 1
            leads.append(" ".join(text.split())[:220])
        if d:
            kept_set = set(kept)
            leads.extend(x[:220] for x in _split_findings(text)
                         if x not in kept_set)
    vi = bool(_VI_RE.search(ctx.last.lower()))
    read = [p for p, _b in files]
    skipped = list(getattr(step, "skipped", []) or [])
    out = []
    summary = _test_summary(tests)
    if vi:
        out.append(f"**Kết quả review** ({len(files)} file, {len(chunks)} nhóm, "
                   "mỗi nhóm được soi riêng; trích dẫn đã đối chiếu với source)")
        if summary:
            out.append("Test: " + " | ".join(summary))
        if findings:
            out.append("**Phát hiện:**\n" + "\n".join(findings))
        else:
            out.append("Không tìm thấy critical bug nào có trích dẫn xác minh "
                       "được trong các file đã đọc.")
        if dropped:
            out.append(f"({dropped} nhận định bị loại vì trích dẫn không khớp source; "
                       "chỉ là nghi vấn chưa xác minh, cần tự kiểm tra:)\n"
                       + "\n".join(leads[:3]))
        out.append(f"{clean}/{len(chunks)} nhóm model trả về 'không có lỗi'; đó là ý kiến "
                   "của model nhỏ, không phải bằng chứng.")
        out.append("File đã đọc: " + ", ".join(read))
        if skipped or failed:
            out.append("**Chưa review đầy đủ (coverage một phần):** "
                       + ", ".join(skipped + failed))
        out.append("Lưu ý: kết quả test pass không chứng minh không có bug; đây "
                   "là review tự động bằng model nhỏ, nên kiểm tra lại các điểm quan trọng.")
    else:
        out.append(f"**Review result** ({len(files)} files, {len(chunks)} groups, "
                   "each reviewed separately; quotes verified against the source)")
        if summary:
            out.append("Tests: " + " | ".join(summary))
        if findings:
            out.append("**Findings:**\n" + "\n".join(findings))
        else:
            out.append("No critical bug with a verifiable quote was found in the "
                       "files read.")
        if dropped:
            out.append(f"({dropped} claim(s) dropped, quote not found in the source; "
                       "unverified leads, check manually:)\n" + "\n".join(leads[:3]))
        out.append(f"{clean}/{len(chunks)} groups returned 'no bug'; that is a small "
                   "model's opinion, not evidence.")
        out.append("Files read: " + ", ".join(read))
        if skipped or failed:
            out.append("**Partial coverage, not fully reviewed:** "
                       + ", ".join(skipped + failed))
        out.append("Note: passing tests do not prove the absence of bugs; this is "
                   "an automated small-model review, double-check important areas.")
    return "\n\n".join(out)


# --------------------------------------------------------------------------
# Hook vào GeminiHandler._generate_with_tool_retry
# --------------------------------------------------------------------------

def _map_model(model_id, think_mode, extra, ticket):
    """(model_id, think, extra, ticket) cho bước map; flow_map_model nếu có."""
    name = _cfg("flow_map_model")
    if not name:
        return model_id, think_mode, extra, ticket
    from gemini_web2api.models import resolve_model, ticket_for
    n, m_id, m_think, err, m_extra = resolve_model(
        name, default=CONFIG["default_model"])
    if err:
        log(f"[flow] flow_map_model '{name}' invalid ({err}); using request model")
        return model_id, think_mode, extra, ticket
    log(f"[flow] map step uses model '{n}'")
    return m_id, m_think, m_extra, ticket_for(n)


def _wrap_flow(orig):
    @functools.wraps(orig)
    def wrapper(self, prompt, model_id, think_mode, file_refs, extra,
                tools_active, allowed_names, tool_schemas, required_tool,
                messages=None, ticket=None, tool_required=None,
                client_gone=None):
        def call(p, req=required_tool, allowed_override=None):
            return orig(self, p, model_id, think_mode, file_refs, extra,
                        tools_active, allowed_override or allowed_names, tool_schemas, req,
                        messages=messages, ticket=ticket,
                        tool_required=tool_required, client_gone=client_gone)

        if not (_cfg("flow_enabled") and tools_active and not required_tool
                and messages and allowed_names):
            return call(prompt)
        try:
            tm = ToolMap(allowed_names, tool_schemas, tool_required)
            ask = clarification_text(messages)
            if ask:
                log("[flow] request unclear -> asking the client to pick a pattern")
                return ask, None
            ctx = _analyze(messages)
            last_role = messages[-1].get("role") if isinstance(messages[-1], dict) else None
            step = plan_for(messages, tm, ctx) if last_role in ("user", "tool") else None
        except Exception as e:  # never let the guard break a request
            log(f"[flow] planner error, falling back: {e}")
            return call(prompt)
        if step is None:
            return call(prompt)
        if getattr(step, "local_text", None) is not None:
            # A deterministic refusal/stop from a transition guard: return it
            # verbatim instead of asking the model again.
            return step.local_text, None
        log(f"[flow] pattern={step.pattern} step {step.index}/{step.total} "
            f"{step.name} mode={'drive' if step.drive and _cfg('flow_drive') else 'guide'}")

        # Finding 15: for guided steps, replace the full chat history with a
        # minimal per-step prompt (goal + permissions + selected evidence).
        # build_step_prompt returns None whenever the lean path does not apply
        # -> every call below falls back to the original `prompt` verbatim.
        lean = (_c5("flow6_lean_prompt") and ctx is not None
                and getattr(ctx, "pattern", None) in _FF5_SPEC
                and not _ff5_step_driven(step))
        step_prompt = build_step_prompt(ctx, step, messages, tm) if lean else None
        if step_prompt is not None:
            _ff5_metric("lean_prompt_used")
            _ff5_metric("lean_saved_chars", max(0, len(prompt) - len(step_prompt)))

        sys_note = prompt[prompt.rfind("\n\n[SYSTEM NOTE] "):] if "\n\n[SYSTEM NOTE] " in prompt else ""
        effective_prompt = (step_prompt + sys_note) if step_prompt else prompt

        if step.name == "answer":
            if (_cfg("flow_mapreduce")
                    and step.pattern in ("GENERIC_REVIEW", "FILE_REVIEW")):
                try:
                    m_id, m_think, m_extra, m_ticket = _map_model(
                        model_id, think_mode, extra, ticket)
                    report = _map_reduce(
                        messages, step, ctx,
                        lambda p: generate(p, m_id, m_think, None,
                                           m_extra, m_ticket), client_gone)
                except Exception as e:
                    log(f"[flow] map-reduce failed, using single answer: {e}")
                    report = None
                if report:
                    return report, None
            return call(effective_prompt + "\n\n" + step.directive())
        if step.drive and _cfg("flow_drive"):
            d_calls = list(step.calls or [])
            if "_ff17_attach_todowrite" in globals():
                d_calls = _ff17_attach_todowrite(ctx, messages, tm, step, d_calls)
            return "", d_calls

        nudge, last = "", None
        saved_todo_calls = []
        max_retries = max(0, int(_cfg("flow_max_retries"))) + (1 if step.name == "act" else 0)
        step_allowed = (
            {n for n in step.tool_names if not (step.name == "act" and "_ff16_is_todo_tool" in globals() and _ff16_is_todo_tool(n))}
            or set(step.tool_names)
        ) if step.tool_names else None
        for attempt in range(1 + max_retries):
            try:
                text, calls = call(
                    effective_prompt + "\n\n" + step.directive() + nudge,
                    True,
                    allowed_override=(step_allowed if attempt >= 1 else None),
                )
            except Exception as e:
                log(f"[flow] guide attempt {attempt + 1} failed: {e}")
                if step.calls:
                    return "", step.calls
                if attempt + 1 >= 1 + max_retries:
                    raise
                continue
            last = (text, calls)
            ok = [tc for tc in (calls or [])
                  if tool_kind((tc.get("function") or {}).get("name")) in step.kinds]
            if ok and step.name == "act":
                if not any(tool_kind((tc.get("function") or {}).get("name")) == "act" for tc in ok):
                    saved_todo_calls = [
                        tc for tc in ok
                        if "_ff16_is_todo_tool" in globals()
                        and _ff16_is_todo_tool((tc.get("function") or {}).get("name"))
                    ]
                    last = (text, None)
                    ok = []
                    nudge = (" You only called todowrite; you MUST emit an edit or write "
                             f"tool_call ({', '.join(sorted(step_allowed or step.tool_names))}) "
                             "to modify the target file in this step.")
                    continue
                ok, problems = _gate_act_calls(ok, messages)
                if problems:
                    log("[flow] edit gate: " + " | ".join(problems))
                    last = (text, None)
                    if attempt < max_retries:
                        if all("no-op edit" in p for p in problems):
                            nudge = (" Your edit was rejected before sending: "
                                     + " | ".join(problems)
                                     + " Do NOT repeat the already-applied block; emit an "
                                     "edit or write tool_call for the NEXT unimplemented "
                                     "requirement in the plan now.")
                        else:
                            nudge = (" Your edit was rejected before sending: "
                                     + " | ".join(problems) + " Copy oldString "
                                     "EXACTLY from the file you read, without "
                                     "line-number prefixes.")
                        continue
            if ok:
                if saved_todo_calls and not any(
                    "_ff16_is_todo_tool" in globals()
                    and _ff16_is_todo_tool((tc.get("function") or {}).get("name"))
                    for tc in ok
                ):
                    ok = saved_todo_calls + list(ok)
                elif "_ff17_attach_todowrite" in globals():
                    ok = _ff17_attach_todowrite(ctx, messages, tm, step, ok)
                return "", ok
            bad = ", ".join((tc.get("function") or {}).get("name", "?")
                            for tc in (calls or [])) or "none"
            nudge = (f" Your previous reply used: {bad}. That is not allowed at "
                     f"this step; use only: {', '.join(step.tool_names)}.")
        if step.calls:
            log("[flow] guide failed, synthesizing the step's call")
            return "", step.calls
        log("[flow] guide failed and nothing to synthesize: returning model output")
        return last if last else call(prompt)

    wrapper._flow_patched = True
    return wrapper


# ==== fix_patch:begin (do fix_patch.py sinh ra; chạy lại script để cập nhật) ====
_FLOW_DEFAULTS = {
    # Flow fix 1 defaults
    "fix_enabled": True,
    "fix_debug_flow": True,
    "fix_create_flow": True,
    "fix_diff_review": True,
    "fix_read_docs_for_change": True,
    "fix_read_related_tests": True,
    "fix_max_test_attempts": 3,
    "fix_write_shrink_ratio": 0.5,
    # Flow fix 5 defaults
    "flow5_enabled": True,
    "flow5_classify": True,
    "flow5_ps_fix": True,
    "flow5_block_destructive": True,
    "flow5_respect_no_edit": True,
    "flow5_fail_closed_edits": True,
    "flow5_guard_retries": 1,
    "flow5_sticky": True,
    "flow5_plan": True,
    "flow5_scope_check": True,
    "flow5_evidence_gate": True,
    "flow5_auto_advance": True,
    "flow5_canned_review": True,
    "flow5_step_driven": True,
    "flow5_lean_prompt": True,
    "flow5_lean_evidence": True,
    "flow5_lean_evidence_chars": 12000,
    "flow5_cross_module": True,
    # Flow fix 6 defaults
    "flow6_context": True,
    "flow6_lean_prompt": False,
    "flow6_metrics": True,
    "flow6_diff_step": True,
    "flow6_scope_guard": True,
    "flow6_universal_guard": True,
    "flow6_cross_module": True,
    "flow6_banner": True,
    # Flow fix 8 defaults
    "flow8_strict_shell": True,
    "flow8_write_regate": True,
    "flow8_absence_proof": True,
    "flow8_independent_grants": True,
    "flow8_lean_source": True,
    "flow8_lean_source_chars": 24000,
    "flow8_listing_cap": 100,
    "flow8_bard_strict": True,
    "flow8_destructive_wide": True,
    "flow8_revert_needs_edit": True,
    # Flow fix 10 defaults
    "flow_tiebreak": True,
    "flow_ask_on_ambiguity": True,
    # Flow fix 12 defaults
    "flow12_poll_max": 12,
    # Flow fix 14 defaults
    "flow14_compile": True,
    "flow14_edit_ambiguity": False,
    "flow14_explain_hops": True,
    "flow14_explain_max_reads": 4,
    # Flow fix 15 defaults
    "flow15_route": True,
    "flow15_keywords": True,
    "flow15_trunc_legacy": True,
    "flow15_runtime_strict": True,
    "flow15_server_hardening": True,
    # Flow fix 16 defaults
    "flow16_enabled": True,
    "flow16_plan_todo": True,
    "flow16_multifile_act": True,
    "flow16_review_quotes": True,
    "flow16_cross_module_constants": True,
    "flow16_explain_imports": True,
    "flow16_runtime_outer": True,
    "flow16_sse_cleanup": True,
    # Flow fix 17 defaults
    "flow17_enabled": True,
    "flow17_symbol_graph": True,
    "flow17_targeted_tests": True,
    "flow17_fuzzy_edit": True,
    "flow17_session_ledger": True,
    "flow17_session_cache_max": 128,
    "flow17_plan_todo_act": True,
    "flow17_act_context_pack": True,
    "flow17_plan_max_acts": 12,
    "flow17_post_test_max_acts": 4,
    "flow17_plan_compliance_audit": True,
    "flow17_static_pre_gate": True,
    # Flow fix 18 defaults
    "flow18_enabled": True,
    "flow18_max_repair_rounds": 4,
    "flow18_hard_max_acts": 24,
    "flow18_progress_bonus_rounds": 2,
    "flow18_honest_final": True,
    # Flow fix 19 defaults
    "flow19_enabled": True,
    "flow19_force_read_after": 3,
    "flow19_stop_after": 5,
    "flow19_anti_oscillation": True,
    "flow19_call_sig_check": True,
    "flow19_override_sigs": True,
}
_FX_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("fix_")}
_FF5_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("flow5_") or k.startswith("flow6_")}
_FF8_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("flow8_")}
_FF10_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k in ("flow_tiebreak", "flow_ask_on_ambiguity")}
_FF14_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("flow14_")}
_FF15_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("flow15_")}
_FF16_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("flow16_")}
_FF17_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("flow17_")}
_FF18_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("flow18_")}
_FF19_DEFAULTS = {k: v for k, v in _FLOW_DEFAULTS.items() if k.startswith("flow19_")}
_DEFAULTS.update(_FLOW_DEFAULTS)

# Import-time defaults applied to CONFIG in original sequential order (ff8 -> ff16 -> ff17 -> ff18 -> ff19)
for _sub in (_FF8_DEFAULTS, _FF16_DEFAULTS, _FF17_DEFAULTS, _FF18_DEFAULTS, _FF19_DEFAULTS):
    for _k, _v in _sub.items():
        CONFIG.setdefault(_k, _v)


def _flow_c(key, default=None):
    try:
        return CONFIG.get(key, _FLOW_DEFAULTS.get(key, default))
    except Exception:
        return _FLOW_DEFAULTS.get(key, default)


_c = _flow_c


def _norm(p):
    return str(p or "").replace("\\", "/")


def _base(p):
    return _norm(p).rsplit("/", 1)[-1].lower()


_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?)/|(?:^|/)test_[^/]*$|_test\.[a-z]+$"
    r"|\.(?:test|spec)\.[a-z]+$")
_DOC_NAMES = ("agents.md", "claude.md", "readme.md")


def _is_test_path(p):
    return bool(_TEST_PATH_RE.search(_norm(p).lower()))


def _is_doc(p):
    low = _norm(p).lower()
    return _base(p) in _DOC_NAMES or low.endswith((".md", ".rst", ".txt"))


def _live(paths):
    out = []
    for p in paths:
        n = _norm(p)
        if any(s in _IGNORE_SEG for s in n.split("/")[:-1]):
            continue
        out.append(n)
    return out


def _doc_files(listing):
    best = {}
    for f in _live(listing):
        b = _base(f)
        if b in _DOC_NAMES and (b not in best or len(f) < len(best[b])):
            best[b] = f
    guide = [best[n] for n in ("agents.md", "claude.md") if n in best]
    return guide or ([best["readme.md"]] if "readme.md" in best else [])


# --------------------------------------------------------------------------
# Lệnh test: marker file > đếm đuôi file; lệnh hỏng thì thử lệnh khác
# --------------------------------------------------------------------------

def test_candidates(names):
    """Danh sách lệnh test theo thứ tự ưu tiên ([] nếu không đoán được)."""
    names = _live(names)
    bases = {_base(n) for n in names}
    score = {"py": 0, "js": 0, "go": 0, "rs": 0}
    if "package.json" in bases:
        score["js"] += 100
    if bases & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
                "pytest.ini", "tox.ini", "conftest.py"}:
        score["py"] += 100
    if "go.mod" in bases:
        score["go"] += 100
    if "cargo.toml" in bases:
        score["rs"] += 100
    for n in names:
        low = n.lower()
        if low.endswith(".py"):
            score["py"] += 1
        elif low.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")):
            score["js"] += 1
        elif low.endswith(".go"):
            score["go"] += 1
        elif low.endswith(".rs"):
            score["rs"] += 1
    unittest_cmd = "python -m unittest discover -s tests"
    if (any("/" in n for n in names)
            and not any("/tests/" in "/" + n.lower() for n in names)):
        unittest_cmd = "python -m unittest discover"
    pytest_cmd = "python -m pytest -q"
    py_first = bool(bases & {"pytest.ini", "conftest.py", "tox.ini"})
    cmds = {
        "py": [pytest_cmd, unittest_cmd] if py_first else [unittest_cmd, pytest_cmd],
        "js": ["npm test"],
        "go": ["go test ./..."],
        "rs": ["cargo test"],
    }
    out = []
    configured = (_cfg("flow_test_command") or "").strip()
    if configured:
        out.append(configured)
    order = ["py", "js", "go", "rs"]
    for eco in sorted((e for e in order if score[e] > 0),
                      key=lambda e: (-score[e], order.index(e))):
        for cmd in cmds[eco]:
            if cmd not in out:
                out.append(cmd)
    return out


_BROKEN_RE = re.compile(
    r"ENOENT|Could not read package\.json|No module named|ModuleNotFoundError"
    r"|command not found|is not recognized as|No such file or directory"
    r"|cannot find the (?:path|file)|Missing script|no tests? (?:found|ran)"
    r"|Ran 0 tests|collected 0 items|usage: python|can't open file", re.I)
_RAN_RE = re.compile(
    r"\bRan\s+[1-9]\d*\s+tests?\b|\bcollected\s+[1-9]\d*\s+items?\b"
    r"|\b[1-9]\d*\s+(?:passed|failed)\b|(?m:^\W*tests\s+[1-9]\d*\b)", re.I)
_FAILED_RE = re.compile(
    r"(?m:^FAILED\b)|\bFAILED\s*\(|(?m:^FAIL:)|(?m:^ERROR:)|\b[1-9]\d*\s+failed\b"
    r"|\b[1-9]\d*\s+errors?\b|Traceback \(most recent call last\)|(?m:^not ok\b)"
    r"|Exited with code [1-9]|(?m:^\W*fail\s+[1-9])")


def test_outcome(result):
    """'pass' | 'fail' | 'broken' (lệnh không chạy được) | 'unknown'."""
    r = result or ""
    ran = bool(_RAN_RE.search(r))
    if not ran and _BROKEN_RE.search(r):
        return "broken"
    if _FAILED_RE.search(r):
        return "fail"
    if ran or re.search(r"(?m:^OK\b)|Exited with code 0", r):
        return "pass"
    return "unknown"


def _test_runs(ev):
    out = []
    for i, e in enumerate(ev):
        if e["kind"] != "exec":
            continue
        cmd = _call_command(e["args"])
        if _is_test_cmd(cmd):
            out.append((i, cmd, e["result"]))
    return out


def pick_test_command_s1(names, ev):
    """Lệnh test cần chạy tiếp, hoặc None khi không còn gì để chạy."""
    cands = test_candidates(names)
    if not cands:
        return None
    last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"), default=-1)
    runs = _test_runs(ev)
    after = [r for r in runs if r[0] > last_act]
    broken_cmds = {cmd.strip() for _i, cmd, res in runs
                   if test_outcome(res) == "broken"}
    if not after:
        for _i, cmd, res in reversed(runs):
            if test_outcome(res) in ("pass", "fail"):
                return cmd          # lệnh đã chứng tỏ chạy được: chạy lại sau khi sửa
        for c in cands:
            if c.strip() not in broken_cmds:
                return c
        return None
    if test_outcome(after[-1][2]) != "broken":
        return None
    if len(after) >= int(_c("fix_max_test_attempts")):
        return None
    tried = {cmd.strip() for _i, cmd, _r in after}
    for c in cands:
        if c.strip() not in tried:
            return c
    return None


def _excerpt_s1(text, limit=1800):
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if re.search(r"Traceback|^FAIL|^ERROR|AssertionError|Error:|\bfailed\b|not ok", ln):
            lines = lines[max(0, i - 2):]
            break
    return "\n".join(lines).strip()[:limit]


# --------------------------------------------------------------------------
# Vị trí lỗi trong output test / traceback người dùng dán
# --------------------------------------------------------------------------

_SKIP_PATH = re.compile(
    r"site-packages|dist-packages|node_modules|<frozen|/lib/python|python3\d*/lib/"
    r"|/unittest/|_pytest|pluggy|importlib", re.I)
_SRC_EXTS = r"(?:py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|rb|php)"


def failure_locations(text, listing=()):
    """[(path, line|None)] từ output test/traceback, chỉ file của project."""
    norm = (text or "").replace("\\", "/")
    found = []

    def add(path, line=None):
        path = (path or "").strip()
        if path and not _SKIP_PATH.search(_norm(path)):
            found.append((path, line))

    for m in re.finditer(r'File "([^"\n]+?)", line (\d+)', norm):
        add(m.group(1), int(m.group(2)))
    for m in re.finditer(
            r'(?m)^[ \t]*((?:[A-Za-z]:)?[^\s:"\'<>|()]+\.' + _SRC_EXTS + r'):(\d+)', norm):
        add(m.group(1), int(m.group(2)))
    for m in re.finditer(r'\b((?:[A-Za-z]:)?[^\s:"\'<>|()]+\.py)::', norm):
        add(m.group(1))
    for m in re.finditer(
            r'\(((?:[A-Za-z]:)?[^\s():"\'<>|]+\.(?:js|mjs|cjs|ts|tsx|jsx)):(\d+):\d+\)', norm):
        add(m.group(1), int(m.group(2)))
    if listing:
        for m in re.finditer(r'(?m)^(?:FAIL|ERROR): \S+ \(([\w.]+)\)', norm):
            parts = m.group(1).split(".")
            for k in range(len(parts), 0, -1):
                cand = "/".join(parts[:k]) + ".py"
                if any(T._path_seen(cand, [f]) for f in listing):
                    add(cand)
                    break
    return found


def _resolve_locations(locs, listing):
    out, seen = [], set()
    for path, _line in locs:
        if listing:
            hit = next((f for f in listing if T._path_seen(path, [f])), None)
        else:
            hit = _norm(path)
        if hit and hit.lower() not in seen:
            seen.add(hit.lower())
            out.append(hit)
    out.sort(key=lambda f: 0 if _is_test_path(f) else 1)
    return out


def local_imports_s1(path, text, listing, scored=False):
    """File của project mà `text` (nội dung `path`) import.

    scored=True trả [(file, số lần dùng)] giảm dần: module được dùng nhiều là
    module đáng đọc trước khi tìm nguyên nhân.
    """
    text = text or ""
    low = {_norm(f).lower(): f for f in listing}

    def find(rel):
        rel = rel.lower()
        for k, f in low.items():
            if k == rel or k.endswith("/" + rel):
                return f
        return None

    entries = []
    for m in re.finditer(r"(?m)^[ \t]*from\s+([\w.]+)\s+import\s+\(?([^\n#]+)", text):
        base = m.group(1)
        items = []
        for n in m.group(2).split(","):
            n = n.strip().strip("()").strip()
            if not n or n == "*":
                continue
            parts = n.split(" as ")
            items.append((parts[0].strip(), (parts[1] if len(parts) > 1 else parts[0]).strip()))
        entries.append((base, [a for _n, a in items]))
        for nm, al in items:
            if re.match(r"^\w+$", nm):
                entries.append((base + nm if base.endswith(".") else base + "." + nm, [al]))
    for m in re.finditer(r"(?m)^[ \t]*import\s+([\w.]+)(?:\s+as\s+(\w+))?", text):
        entries.append((m.group(1), [m.group(2) or m.group(1).split(".")[0]]))
    out, score = [], {}
    for mod, idents in entries:
        rel = "/".join(x for x in mod.lstrip(".").split(".") if x)
        if not rel:
            continue
        f = find(rel + ".py") or find(rel + "/__init__.py")
        if (not f or _base(f) == "__init__.py"
                or _norm(f).lower() == _norm(path).lower()):
            continue
        n = sum(len(re.findall(r"\b" + re.escape(i) + r"\b", text))
                for i in idents if re.match(r"^\w+$", i))
        score[f] = max(score.get(f, 0), n)
        if f not in out:
            out.append(f)
    d = posixpath.dirname(_norm(path))
    for m in re.finditer(r"""(?:from\s+|require\()\s*['"](\.{1,2}/[^'"]+)['"]""", text):
        rel = posixpath.normpath(posixpath.join(d, m.group(1)))
        for ext in ("", ".js", ".mjs", ".ts", ".tsx", ".jsx", "/index.js"):
            f = find(rel + ext)
            if f and f not in out:
                out.append(f)
                score[f] = 1
                break
    if scored:
        return sorted(((f, score.get(f, 1)) for f in out), key=lambda x: -x[1])
    return out




# --------------------------------------------------------------------------
# Nhận diện pattern (bổ sung cho T.detect_task_pattern)
# --------------------------------------------------------------------------

_FAIL_RE = re.compile(
    r"\b(?:fail|fails|failed|failing|failure|failures|broken|breaks|crash|crashes"
    r"|crashed|crashing|exception|traceback|regression|error|errors|loi|hong|sai"
    r"|that bai|khong chay|khong hoat dong|bi loi|panic|stack ?trace"
    r"|(?:does|do|is|are)(?:n'?t| not) work(?:ing)?|not working)\b")
_RT_RE = re.compile(
    r"\b(?:test|tests|testcase|testcases|pytest|unittest|npm|build|builds|compile"
    r"|compilation|ci|lint|run|runs|running|chay|lenh|command|startup|start|boot"
    r"|launch)\b")
_CAUSE_RE = re.compile(
    r"\b(?:why|cause|causes|root cause|investigate|investigation|debug|diagnose"
    r"|diagnosis|figure out|what(?:'s| is|s) wrong|reproduce|nguyen nhan|dieu tra"
    r"|tai sao|vi sao|tim hieu|tim ra|giai thich|xem thu|kiem tra|check|look into"
    r"|find out)\b")
_FIX_RE = re.compile(r"\b(?:fix|repair|resolve|patch|sua|sua loi|khac phuc)\b")
_WANT_CHANGE_RE = re.compile(
    r"\b(?:implement|add|introduce|refactor|upgrade|migrate|rename|them|cap nhat"
    r"|bo sung|cai dat|xay dung|trien khai|viet them|lam them|nang cap)\b")
_REVIEW_RE = re.compile(r"\b(?:review|audit|danh gia|soat)\b")
_TRACE_RE = re.compile(
    r"Traceback \(most recent call last\)|File \"[^\"\n]+\", line \d+"
    r"|(?m:^\s*(?:FAILED|ERROR)\s+\S+\.py)|AssertionError"
    r"|\bat\s+\S+\s+\([^)\n]*:\d+:\d+\)")
_DIFF_RE = re.compile(
    r"\b(?:git diff|diff|uncommitted|unstaged|staged|pull request|pr|commit"
    r"|commits|thay doi|changes|changeset|vua sua|vua thay doi|working tree)\b")
_REVIEWV_RE = re.compile(
    r"\b(?:review|audit|check|kiem tra|soat|danh gia|xem lai|look over|look at)\b")
_CHG_V_RE = re.compile(
    r"\b(?:fix|repair|implement|add|introduce|update|upgrade|change|modify|refactor"
    r"|remove|delete|drop|double|halve|increase|decrease|migrate|rename|sua|them"
    r"|cap nhat|bo sung|cai dat|chinh sua|xay dung|trien khai|viet them|lam them"
    r"|nang cap|va loi)\b")

# "tạo file / viết script / gộp file": tools._CHANGE_VERB_RE không biết các động từ này.
_CREATE_V_RE = re.compile(
    r"\b(?:create|creates|generate|scaffold|merge|combine|bundle|consolidate"
    r"|concat\w*|tao|viet|gop|ghep|gom|tong hop|them"
    r"|write\s+(?:a|an|the|new|me)|make\s+(?:a|an|new)|build\s+(?:a|an|new))\b")
_CREATE_O_RE = re.compile(
    r"\b(?:file|files|script|scripts|module|class|function|folder|directory|thu muc"
    r"|tep|ham|tool|cli|command|test|tests|component|endpoint|page|readme)\b")
_EXPLAIN_V_RE = re.compile(
    r"\b(?:explain|describe|summari[sz]e|what|how|why|giai thich|mo ta|tom tat"
    r"|la gi|nhu the nao|tai sao)\b")
_MERGE_RE = re.compile(
    r"\b(?:gop|ghep|gom|tong hop|merge|combine|bundle|consolidate|concat\w*"
    r"|single file|one file|into one|thanh (?:1|mot) file|thanh file duy nhat"
    r"|(?:1|mot) file duy nhat)\b")
_DIR_WORD = r"(?:folder|directory|dir|package|th[uư] m[uụ]c)"


def _create_intent(folded, scannable, raw=""):
    f = (folded or "").lower()
    if not _CREATE_V_RE.search(f) or _EXPLAIN_V_RE.search(f):
        return False
    return bool(_CREATE_O_RE.search(f) or T._files_in(scannable or ""))


def _change_demanded(messages):
    """tools.change_demanded + yêu cầu tạo/gộp file.

    Chỉ vá hàm này (server dùng nó để cấp thêm lượt retry khi model chỉ mô tả thay
    vì làm). KHÔNG vá tools._is_change_request: hàm đó còn dùng cho
    pending_tool_request, nơi "tạo file mới" có chủ đích không đòi phải đọc file.
    """
    if _FX_ORIG_CD(messages):
        return True
    last = None
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user":
            last = T._stringify_content(m.get("content", ""))
    if not last:
        return False
    scannable = T._strip_code_spans(last)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable)).strip()
    # Chỉ khi nêu tên file cụ thể: "tao file" trơn có thể là câu hỏi/trả lời ngắn.
    return (_create_intent(folded, scannable, last)
            and any(f.lower() not in T._NON_FILE_NAMES for f in T._files_in(scannable)))


def _dir_cands(raw):
    out = []
    for m in re.finditer(r"([\w./\\-]+)\s+" + _DIR_WORD + r"\b", raw or "", re.I):
        out.append(m.group(1))
    for m in re.finditer(_DIR_WORD + r"\s+[`'\"]?([\w./\\-]+)", raw or "", re.I):
        out.append(m.group(1))
    for m in re.finditer(r"([\w.-]+)[/\\](?:\*\*?)?(?=\s|$|[,.;)])", raw or ""):
        out.append(m.group(1))
    seen, res = set(), []
    for c in out:
        c = c.strip("/\\ ")
        if c and c.lower() not in seen:
            seen.add(c.lower())
            res.append(c)
    return res


def _named_files(ctx):
    files = [f for f in T._files_in(ctx.scannable)
             if f.lower() not in T._NON_FILE_NAMES]
    return sorted(files, key=lambda f: ctx.scannable.find(f))


def _existing_s1(known, listing):
    """Bỏ file mã nguồn không có trong listing (file sắp tạo): khỏi đọc file không tồn tại."""
    if not listing:
        return known
    return [f for f in known
            if not f.lower().endswith(_SRC_EXT)
            or any(T._path_seen(f, [x]) for x in listing)]


def _detect(ctx):
    """(pattern, fix) cho các pattern mới, hoặc None."""
    raw = ctx.last or ""
    folded = (ctx.folded or "").lower()
    named = _named_files(ctx)
    has_fail = bool(_FAIL_RE.search(folded))
    has_rt = bool(_RT_RE.search(folded))
    has_cause = bool(_CAUSE_RE.search(folded))
    has_fix = bool(_FIX_RE.search(folded))
    is_review = bool(_REVIEW_RE.search(folded))
    pasted = bool(_TRACE_RE.search(raw))
    wants_change = bool(_WANT_CHANGE_RE.search(folded)) and not has_fix
    if _c("fix_debug_flow") and not is_review:
        if pasted or (has_fail and has_rt and (has_cause or not named)
                      and not (wants_change and not has_cause)):
            return "DEBUG", has_fix
    if (_c("fix_diff_review") and not named and _REVIEWV_RE.search(folded)
            and _DIFF_RE.search(folded)):
        return "DIFF_REVIEW", False
    return None


_STOP2 = set(_STOP) | set(
    "giai thich source nay hoat dong nao the xem gium voi cho minh toi bang duoc "
    "dang chay lai con vao ra den neu khi cach chi tiet tong quan phan tich kiem "
    "them sua viet tao muon can hien tai nhu nao sao bao gom nguyen nhan dieu tra "
    "tinh nang chuc phai nen thu roi cung nua van ma thi dau kia hay thanh duy "
    "nhat noi dung folder thu muc gop tat ca".split())
_STOP2 -= {"flow"}


def _keywords_s1(text, limit=6):
    """Từ khoá: identifier/backtick trước, từ thường (>=4 ký tự) sau."""
    strong, weak = [], []
    for m in re.finditer(r"`([^`\n]+)`", text or ""):
        if not (2 <= len(m.group(1)) <= 60):
            continue
        k = re.sub(r"[()\s]", "", m.group(1)).lower()
        if k and k not in strong:
            strong.append(k)
    folded = T._fold_diacritics(re.sub(r"```.*?```", " ", text or "", flags=re.S))
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", folded):
        lw = w.lower()
        if lw in _STOP2 or lw in strong or lw in weak:
            continue
        if "_" in w or (any(c.isupper() for c in w[1:]) and any(c.islower() for c in w)):
            strong.append(lw)
        elif len(lw) >= 4:
            weak.append(lw)
    return (strong + weak)[:limit]


_FX_QUESTION_RE = re.compile(
    r"\b(how|where|why|what|which|explain|does|show me|giai thich|o dau|nhu the nao"
    r"|hoat dong|la gi|tai sao|dinh nghia|tim hieu|mo ta|tom tat"
    r"|dieu tra xem|phan tich xem|khao sat xem"
    r"|(?:kiem tra xem|xem)\b[^.!\n]*?\bco the\b[^.!\n]*?\bkhong)\b")
_FX_PROJECT_RE = re.compile(
    r"\b(this|the|our|nay|cua)\s+(project|repo|repository|codebase|code|proxy|server"
    r"|module|function|class|file|source|flow|pipeline|system|app)\b"
    r"|\b(du an|repo|codebase|ma nguon|source|source code|trong code|ham|class|module"
    r"|kien truc|architecture|luong|flow|pipeline)\b")


def _is_code_question(text):
    scannable = T._strip_code_spans(text)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable.lower())).strip()
    if not _FX_QUESTION_RE.search(folded):
        return False
    has_ident = bool(re.search(r"`[^`\n]{2,}`", text or ""))
    has_file = any(f for f in T._files_in(scannable)
                   if f.lower() not in T._NON_FILE_NAMES)
    return has_ident or has_file or bool(_FX_PROJECT_RE.search(folded))


def _apply_create_intent(ctx, folded: str) -> bool:
    """Helper to detect and apply CREATE/MERGE pattern to analysis context."""
    files = _named_files(ctx)
    created_set = T._created_files(ctx.scannable or "", folded or "")
    is_merge = bool(_MERGE_RE.search((folded or "").lower()))
    non_created = [f for f in files
                   if f.lower() not in created_set
                   and _base(f).lower() not in created_set]
    if is_merge or (created_set and (ctx.pattern is None or not non_created)):
        ctx.pattern = "CREATE"
        ctx.fix = False
        target_cands = [f for f in files
                        if f.lower() in created_set or _base(f).lower() in created_set]
        ctx.target = (target_cands[0] if target_cands else (files[0] if files else ""))
        ctx.create_merge = is_merge
        ctx.dir_cands = _dir_cands(ctx.last)
        return True
    return False


def _analyze_s1(messages):
    ctx = _base_analyze(messages)
    if ctx is None or not _c("fix_enabled"):
        return ctx
    ctx.fix = False
    ctx.create_merge = False
    ctx.target = ""
    ctx.dir_cands = []
    try:
        if _clarified(messages, ctx.idx):
            return ctx                      # người dùng đã chọn pattern thủ công
        det = _detect(ctx)
        if det:
            ctx.pattern, ctx.fix = det
        elif ctx.pattern in (None, "CHANGE") and _c("fix_create_flow") \
                and _create_intent(ctx.folded, ctx.scannable, ctx.last) \
                and (_named_files(ctx)
                     or _MERGE_RE.search((ctx.folded or "").lower())):
            _apply_create_intent(ctx, ctx.folded or "")
        elif not ctx.pattern:
            folded = (ctx.folded or "").lower()
            idents = [s for s in re.findall(r"`([^`\n]+)`", ctx.last or "")
                      if _IDENT_RE.match(s.strip())]
            if (_CHG_V_RE.search(folded) and (idents or _named_files(ctx))
                    and _is_change_request(ctx.folded, ctx.scannable, ctx.last)):
                ctx.pattern = "CHANGE"
    except Exception as e:
        log(f"[fix] analyze error: {e}")
    return ctx


# --------------------------------------------------------------------------
# Step builders
# --------------------------------------------------------------------------

def _custom_answer(pat, instruction, index, total):
    st = Step(pat, "answer", index, total, (), instruction)
    st.skipped = []
    return st


def _read_step(pat, tm, files, index, total, drive=True, instruction=None):
    calls = _read_calls(tm, files)
    return Step(pat, "read", index, total, {"read"},
                  instruction or ("Read these files fully before answering: "
                                  + ", ".join(files) + "."),
                  calls, drive, tm.names({"read"}))


def _survey_step(pat, tm, ev, index, total):
    n = _survey_count(ev)
    glob_call = (tm.make_call("survey", pattern=(_cfg("flow_survey_pattern")
                                                 if n == 0 else "**/*"))
                 if n < 2 else None)
    exec_call = tm.make_call("exec", command="git ls-files",
                             description="List project files")
    call = glob_call or exec_call
    kinds = {"survey", "search", "exec"}
    if not call and not tm.names(kinds):
        return None
    return Step(pat, "survey", index, total, kinds,
                  "List the project files before anything else.",
                  [call] if call else None, True, tm.names(kinds))


def _tests_step(pat, tm, cmd, index, total,
                instruction="Run the unit tests."):
    call = tm.make_call("exec", command=cmd, description="Run unit tests")
    if not call:
        return None
    return Step(pat, "tests", index, total, {"exec"}, instruction, [call],
                  True, tm.names({"exec"}))


def _code_reads(read_set):
    return [p for p in read_set if not _is_doc(p)]


def _matching_tests(read_set, listing):
    tests = [f for f in listing if _is_test_path(f)
             and f.lower().endswith(_SRC_EXT)]
    out = []
    for p in read_set:
        if _is_test_path(p) or _is_doc(p) or not p.endswith(_SRC_EXT):
            continue
        stem = posixpath.splitext(_base(p))[0]
        if len(stem) < 3 or stem == "__init__":
            continue
        for t in tests:
            if stem in _base(t) and t not in out:
                out.append(t)
    return [t for t in out if not T._path_seen(t, read_set)]


# --------------------------------------------------------------------------
# CHANGE / CREATE (không merge)
# --------------------------------------------------------------------------

_DO_NOT_DESCRIBE = (" Never answer with instructions for the user to do it "
                    "themselves: make the tool call.")
_CREATE_NOTE = ("\nThe file(s) to create do not exist yet: create each one with "
                "the write tool and the COMPLETE file content.")


def _change_tail_s1(pat, ctx, ev, tm, names_all, total, extra=""):
    acts = [e for e in ev if e["kind"] == "act"]
    max_acts = int(_cfg("flow_max_acts"))
    if len(acts) >= max_acts and (not _act_ok(acts[-1])
                                  or _tests_ran_after_act(ev)):
        st = _answer_step("CHANGE", ev, total, total, final=True)
        if st:
            st.pattern = pat
        return st
    ok_acts = [e for e in acts if _act_ok(e)]
    if tm.first("act") and (not ok_acts or (acts and not _act_ok(acts[-1]))):
        return Step(pat, "act", 3, total, {"act"},
                      "Make the change now with ONE edit/write tool_call per "
                      "file, copying old text exactly from the file you read "
                      "(no line-number prefixes). "
                      "CRITICAL: Do NOT break or alter existing features/routes. "
                      "When adding a new endpoint, create a NEW branch (e.g. new `elif`) "
                      "instead of merging it into existing ones. Preserve dynamic "
                      "variables (like __version__) for existing paths." + _DO_NOT_DESCRIBE
                      + _failed_act_hint(ev) + extra, None, False,
                      tm.names({"act"}))
    edited_src = any(e["kind"] == "act" and _act_ok(e)
                     and (_call_path(e["args"]) or "").lower().endswith(_SRC_EXT)
                     for e in ev)
    if edited_src and tm.first("exec") and not T._NO_TEST_RE.search(ctx.folded):
        cmd = pick_test_command(names_all, ev)
        if cmd:
            s = _tests_step(pat, tm, cmd, 4, total)
            if s:
                return s
        runs = _test_runs(ev)
        last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"), default=-1)
        after = [r for r in runs if r[0] > last_act]
        if (after and test_outcome(after[-1][2]) == "fail" and tm.first("act")
                and len(acts) < max_acts):
            return Step(pat, "act", 3, total, {"act"},
                          "The tests still FAIL after your edit. Fix the cause "
                          "with ONE more edit tool_call (copy old text exactly "
                          "from the file as it is NOW; re-read it first if you "
                          "are unsure). Failure:\n" + _excerpt(after[-1][2], 1500),
                          None, False, tm.names({"act"}))
    st = _answer_step("CHANGE", ev, total, total)
    if st:
        st.pattern = pat
    return st


def _plan_change_s1(ctx, ev, tm, pat="CHANGE", total=5):
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    batch = max(1, int(_cfg("flow_read_batch")))
    named = _named_files(ctx)
    known = _existing([_resolve(f, listing) for f in named], listing)
    paths = [x for x in (_call_path(e["args"]) for e in ev) if x]
    names_all = listing + named + paths
    can_read = bool(tm.first("read"))

    if (not known or pat == "CREATE") and not _surveyed(ev):
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    code_read = _code_reads(read_set)
    if known and pat != "CREATE":
        unread = [f for f in known if not T._path_seen(f, read_set)]
        if unread and can_read:
            return _read_step(pat, tm, unread[:batch], 2, total)
    elif not code_read and can_read:
        if _c("fix_read_docs_for_change") and listing:
            docs = [d for d in _doc_files(listing) if not T._path_seen(d, read_set)]
            if docs:
                return _read_step(
                    pat, tm, docs[:2], 2, total,
                    instruction="Read the project guidelines first (conventions, "
                                "where things live): " + ", ".join(docs[:2]) + ".")
        kws = _keywords(ctx.request)
        if (kws and tm.first("search")
                and not any(e["kind"] == "search" for e in ev)):
            scall = tm.make_call("search", pattern="(?i)" + "|".join(
                re.escape(k) for k in kws[:3]))
            if scall:
                skinds = {"survey", "search", "exec"}
                return Step(pat, "search", 2, total, skinds,
                              "Locate the code to change with grep first.",
                              [scall], True, tm.names(skinds))
        core = _core_sources(listing, False, 200)
        ranked = _rank(core, kws, _grep_hits(ev, core))[:2]
        if ranked:
            return _read_step(pat, tm, ranked, 2, total)
        if core:
            return _read_step(pat, tm, core[:2], 2, total, drive=False)
        if pat != "CREATE":
            return Step(pat, "read", 2, total, {"read"},
                          "Read the file(s) you are going to change.", None, False,
                          tm.names({"read"}))
    if can_read and listing and _c("fix_read_related_tests") and pat != "CREATE":
        tests = _matching_tests(read_set, listing)
        if tests:
            return _read_step(
                pat, tm, tests[:2], 2, total,
                instruction="Read the related test file(s) so the change can be "
                            "verified or covered by a test: " + ", ".join(tests[:2]) + ".")
    return _change_tail(pat, ctx, ev, tm, names_all, total,
                        extra=_CREATE_NOTE if pat == "CREATE" else "")


# --------------------------------------------------------------------------
# CREATE: gộp nhiều file thành một (proxy tự sinh script, model không phải chép source)
# --------------------------------------------------------------------------

_MERGE_TMPL = r'''# -*- coding: utf-8 -*-
# Temporary helper generated by the proxy; it deletes itself when done.
import io
import os
import re

SRC = @@SRC@@
OUT = @@OUT@@
C = @@C@@
IS_PY = @@PY@@


def stem(p):
    return os.path.splitext(os.path.basename(p))[0]


def read(p):
    with io.open(p, "r", encoding="utf-8") as fh:
        return fh.read()


names = {stem(p): p for p in SRC}
texts = {p: read(p) for p in SRC}


def deps(text):
    out = []
    for m in re.finditer(r"(?m)^[ \t]*from[ \t]+\.+(\w*)[ \t]+import[ \t]+([^\n#]+)", text):
        if m.group(1):
            out.append(m.group(1))
        else:
            for t in m.group(2).strip("() ").split(","):
                out.append(t.split(" as ")[0].strip())
    return [d for d in out if d in names]


order, seen = [], set()


def visit(p):
    if p in seen:
        return
    seen.add(p)
    for d in deps(texts[p]):
        visit(names[d])
    order.append(p)


rest = sorted(p for p in SRC if stem(p) not in ("__init__", "__main__"))
firsts = [p for p in SRC if stem(p) == "__init__"]
lasts = [p for p in SRC if stem(p) == "__main__"]
for p in firsts + rest + lasts:
    visit(p)

bar = C + " " + "=" * 60
parts = []
if IS_PY:
    parts.append("# -*- coding: utf-8 -*-\n\"\"\"Combined module generated from %d files.\"\"\"\n" % len(order))
for p in order:
    parts.append("\n" + bar + "\n" + C + " FILE: " + os.path.basename(p) + "\n" + bar
                 + "\n\n" + texts[p].rstrip() + "\n")
data = "".join(parts)
with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(data)
print("merged %d files -> %s (%d bytes)" % (len(order), OUT, len(data.encode("utf-8"))))
print("order: " + ", ".join(os.path.basename(p) for p in order))
try:
    os.remove(__file__)
except OSError:
    pass
'''

_SLASH_COMMENT_EXT = (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs",
                      ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".kt",
                      ".swift", ".php")
_MERGE_SCRIPT = "_merge_tmp.py"






def _write_tool(tm):
    for n in tm.by_kind.get("act", []):
        if re.search(r"write|create|save", n, re.I):
            return n
    return None


def _write_call(tm, path, content):
    name = _write_tool(tm)
    if not name:
        return None
    props = tm.schemas.get(name) or set()
    pk = _pick(props, _PATH_KEYS) or (None if props else "filePath")
    ck = _pick(props, ["content", "contents", "text", "file_text", "filetext",
                         "data"]) or (None if props else "content")
    if not pk or not ck:
        return None
    args = {pk: path, ck: content}
    for req in tm.required.get(name, ()):
        if req not in args:
            return None
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _plan_create_s1(ctx, ev, tm):
    pat, total = "CREATE", 5
    fallback = lambda: _plan_change(ctx, ev, tm, pat="CREATE")  # noqa: E731
    if not getattr(ctx, "create_merge", False) or not ctx.target:
        return fallback()
    if not _write_tool(tm) or not tm.first("exec"):
        return fallback()
    if not _surveyed(ev):
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    listing = _listing_files(ev)
    srcs, prefix = _merge_sources(ctx, listing)
    if not srcs:
        return fallback()
    target = _norm(ctx.target)
    root = posixpath.dirname(prefix)
    out_path = target if "/" in target else (posixpath.join(root, target) if root else target)
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    names = ", ".join(_base(f) for f in srcs)

    def run_events(word):
        return [e for e in ev if e["kind"] == "exec" and word in _call_command(e["args"])]

    wrote = [e for e in ev if e["kind"] == "act"
             and _MERGE_SCRIPT in (_call_path(e["args"]) or "")]
    if not wrote:
        call = _write_call(tm, _MERGE_SCRIPT, merge_script(srcs, out_path, ext))
        if not call:
            return fallback()
        return Step(pat, "merge", 2, total, {"act"},
                      "Create the helper script that merges " + names + ".",
                      [call], True, tm.names({"act"}))
    if not _act_ok(wrote[-1]):
        return _custom_answer(
            pat, "Writing the helper script failed: " + _excerpt(wrote[-1]["result"], 400)
                 + "\nReport this exact error to the user in their language. Do NOT "
                   "tell them to do the merge by hand.", 2, total)
    ran = run_events(_MERGE_SCRIPT)
    if not ran:
        call = tm.make_call("exec", command="python " + _MERGE_SCRIPT,
                            description="Merge the files")
        if not call:
            return fallback()
        return Step(pat, "merge", 3, total, {"exec"}, "Run the merge script.",
                      [call], True, tm.names({"exec"}))
    res = ran[-1]["result"] or ""
    if "merged" not in res.lower() or "traceback" in res.lower():
        return _custom_answer(
            pat, "The merge script FAILED. Report the exact error to the user in "
                 "their language and what you would change; do not claim the "
                 "file was created.\n" + _excerpt(res, 1500), 3, total)
    if ext == ".py":
        chk = run_events("py_compile")
        if not chk:
            call = tm.make_call("exec", command='python -m py_compile "' + out_path + '"',
                                description="Check syntax of the merged file")
            if call:
                return Step(pat, "verify", 4, total, {"exec"},
                              "Check that the merged file compiles.", [call], True,
                              tm.names({"exec"}))
        else:
            cres = chk[-1]["result"] or ""
            syntax = ("FAILED: " + _excerpt(cres, 400)) if re.search(
                r"Error|Traceback", cres) else "passed (python -m py_compile)"
            return _custom_answer(pat, _create_answer(out_path, srcs, names, syntax), 5, total)
    return _custom_answer(pat, _create_answer(out_path, srcs, names, "not checked"), 5, total)


def _create_answer(out_path, srcs, names, syntax):
    return ("DONE -- the file already exists. Reply in the user's language: `"
            + out_path + "` was created by merging " + str(len(srcs)) + " files ("
            + names + ") in dependency order, each under a '# FILE:' header. Syntax "
            "check: " + syntax + ". Be honest about the limit: the sources are "
            "concatenated verbatim, so intra-package relative imports (`from . "
            "import x`) are NOT rewritten and the merged module will not run "
            "standalone until imports/aliases are unified -- offer to do that as a "
            "follow-up. Do NOT give the user a script or steps to do it themselves: "
            "it is already done, and the temporary helper script deleted itself.")


# --------------------------------------------------------------------------
# DEBUG: reproduce -> read what the failure points at -> explain (or fix)
# --------------------------------------------------------------------------

def _debug_answer_text(read_files, excerpt):
    read = ", ".join(sorted(read_files)[:20]) or "none"
    return ("Evidence collected (successfully read: " + read + "). Answer NOW in "
            "plain text with NO tool_call, in the user's language. Structure: "
            "(1) which test/command failed and the exact error line; (2) ROOT "
            "CAUSE: quote the exact lines (`file:line -- verbatim code`) from the "
            "files you read and explain the mechanism step by step; (3) the "
            "minimal fix as a small diff -- say clearly it is NOT applied yet; "
            "(4) what you could not verify. Discuss only files you read; never "
            "guess a symbol you have not seen."
            + (("\nFailure output:\n" + excerpt) if excerpt else ""))


def _hop_targets(ev, listing, read_set, excerpt):
    """Module cần đọc tiếp: (1) module mà test import, (2) import của file đã đọc."""
    texts = _read_texts(ev)
    stage1 = []
    for p_, body in texts.items():
        if _is_test_path(p_):
            for f, _s in local_imports(p_, body, listing, scored=True):
                if (not _is_test_path(f) and f not in stage1
                        and not T._path_seen(f, read_set)):
                    stage1.append(f)
    if stage1:
        return stage1[:3]
    nontest = [p for p in texts if not _is_doc(p) and not _is_test_path(p)]
    if len(nontest) >= 5:
        return []
    cand = {}
    for p_ in nontest:
        for f, s in local_imports(p_, texts[p_], listing, scored=True):
            if _is_test_path(f) or T._path_seen(f, read_set):
                continue
            cand[f] = max(cand.get(f, 0), s)
    low = (excerpt or "").lower()
    ranked = sorted(cand, key=lambda f: (
        0 if posixpath.splitext(_base(f))[0] in low else 1, -cand[f]))
    return ranked[:2]


def _plan_debug_s1(ctx, ev, tm):
    pat, total = "DEBUG", 6
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    batch = max(1, int(_cfg("flow_read_batch")))
    paths = [x for x in (_call_path(e["args"]) for e in ev) if x]
    names_all = listing + paths
    can_read = bool(tm.first("read"))
    pasted = ctx.last if _TRACE_RE.search(ctx.last or "") else ""
    fix = bool(getattr(ctx, "fix", False))

    if not pasted and not listing and _survey_count(ev) < 2:
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    if fix and any(e["kind"] == "act" for e in ev):
        return _change_tail(pat, ctx, ev, tm, names_all, total)

    runs = _test_runs(ev)
    if not pasted and tm.first("exec"):
        cmd = pick_test_command(names_all, ev)
        if cmd:
            s = _tests_step(pat, tm, cmd, 2, total,
                            "Run the unit tests to reproduce the failure.")
            if s:
                return s
    outcome = test_outcome(runs[-1][2]) if runs else None
    result_text = (pasted + "\n" + (runs[-1][2] if runs else "")).strip()
    excerpt = _excerpt(result_text)

    if not pasted:
        if outcome is None:
            return _custom_answer(
                pat, "You could not run any test command here. Do NOT guess a "
                     "cause. Tell the user what you tried and ask them to paste "
                     "the failing output or name the test.", 2, total)
        if outcome == "broken":
            return _custom_answer(
                pat, "The test command itself failed to run (not a test "
                     "failure). Report the exact error and ask the user for the "
                     "correct test command. Do NOT guess a cause.\n" + excerpt,
                2, total)
        if outcome in ("pass", "unknown"):
            if fix:
                return _plan_change(ctx, ev, tm, pat="CHANGE")
            return _custom_answer(
                pat, "The test run showed NO failure (outcome: " + outcome + "). "
                     "Say plainly that you could not reproduce it, show the "
                     "summary line of the run, and ask for the exact failing "
                     "command or log. Do NOT invent a cause.\n" + excerpt,
                2, total)

    located = _resolve_locations(failure_locations(result_text, listing), listing)
    unread = [f for f in located if not T._path_seen(f, read_set)]
    if unread and can_read:
        return _read_step(pat, tm, unread[:batch], 3, total,
                          instruction="Read the file(s) the failure points at: "
                                      + ", ".join(unread[:batch]) + ".")
    if can_read and listing:
        hop = _hop_targets(ev, listing, read_set, excerpt)
        if hop:
            return _read_step(
                pat, tm, hop, 4, total,
                instruction="The failure runs through these project modules; "
                            "read them to find the root cause: " + ", ".join(hop) + ".")
    if fix:
        return _change_tail(pat, ctx, ev, tm, names_all, total,
                            extra="\nFailure to fix:\n" + excerpt)
    return _custom_answer(pat, _debug_answer_text(
        [p for p in read_set if not _is_doc(p)], excerpt), 5, total)


# --------------------------------------------------------------------------
# DIFF_REVIEW
# --------------------------------------------------------------------------

_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pyc", ".lock", ".log",
               ".zip", ".pdf", ".woff", ".woff2")


def _changed_files(status, diff):
    files = []
    for ln in (status or "").splitlines():
        m = re.match(r"^\s*([MADRCU?!]{1,2})\s+(.+?)\s*$", ln)
        if m and "D" not in m.group(1):
            p = m.group(2).split(" -> ")[-1].strip('"')
            if p not in files:
                files.append(p)
    for m in re.finditer(r"(?m)^\+\+\+ b/(.+)$", diff or ""):
        if m.group(1) not in files:
            files.append(m.group(1))
    return [f for f in _live(files)
            if not f.endswith("/") and not f.lower().endswith(_BINARY_EXT)]


def _plan_diff(ctx, ev, tm):
    pat, total = "DIFF_REVIEW", 4
    if not tm.first("exec"):
        return None

    def find(word):
        return next((e for e in ev if e["kind"] == "exec"
                     and "git" in _call_command(e["args"])
                     and word in _call_command(e["args"])), None)

    def git_step(cmd, index, why):
        call = tm.make_call("exec", command=cmd, description=why)
        if not call:
            return None
        return Step(pat, "git", index, total, {"exec"}, why + ".", [call],
                      True, tm.names({"exec"}))

    status_ev = find("status")
    if status_ev is None:
        return git_step("git status --short", 1, "List the changed files")
    diff_ev = find("diff")
    if diff_ev is None:
        return git_step("git --no-pager diff HEAD", 2, "Show the changes")
    if "not a git repository" in (status_ev["result"] + diff_ev["result"]).lower():
        return _custom_answer(pat, "This directory is not a git repository, so "
                                   "there is no diff to review. Say so and ask "
                                   "which files to review.", 3, total)
    changed = _changed_files(status_ev["result"], diff_ev["result"])
    read_set = _read_set(ev)
    unread = [f for f in changed if not T._path_seen(f, read_set)][:6]
    if unread and tm.first("read"):
        return _read_step(pat, tm, unread, 3, total,
                          instruction="Read the changed files for context: "
                                      + ", ".join(unread) + ".")
    if not changed and not (diff_ev["result"] or "").strip():
        return _custom_answer(pat, "There are no uncommitted changes. Say so in "
                                   "one sentence.", 4, total)
    return _custom_answer(
        pat, "Review ONLY the changes shown by the diff (the changed files you "
             "read are context). Report each problem as `file:line -- verbatim "
             "quote -- why it is a bug or risk`. If you find none, write exactly "
             "'no problem found in the diff'. Do not claim the tests pass: they "
             "were not run.", 4, total)


# --------------------------------------------------------------------------
# EXPLAIN
# --------------------------------------------------------------------------

def _explain_text(read_files):
    if not read_files:
        return ("NO file was successfully read. Say that plainly and ask the "
                "user for the project path; do NOT describe code you have not seen.")
    return ("Evidence collected (read: " + ", ".join(sorted(read_files)[:20]) +
            "). Answer NOW in plain text with NO tool_call, in the user's "
            "language. Start with a 2-3 sentence overview, then walk through the "
            "flow step by step, naming the REAL functions/classes and citing "
            "`file:line` with SHORT verbatim snippets from the files you read. "
            "Never invent symbols; list important files you did not read as 'not "
            "read'. This is an explanation, NOT a bug report.")


def _plan_explain_s1(ctx, ev, tm):
    pat, total = "EXPLAIN", 4
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    can_read = bool(tm.first("read"))
    named = _named_files(ctx)
    known = [_resolve(f, listing) for f in named]
    if known and can_read:
        unread = [f for f in known if not T._path_seen(f, read_set)]
        if unread:
            return _read_step(pat, tm, unread[:6], 1, total)
    code_read = _code_reads(read_set)
    kws = _keywords(ctx.request)
    if not _surveyed(ev):
        call = None
        if kws and tm.first("search"):
            call = tm.make_call("search", pattern="(?i)" + "|".join(
                re.escape(k) for k in kws[:3]))
        if call:
            kinds = {"survey", "search", "exec"}
            return Step(pat, "search", 1, total, kinds,
                          "Locate the relevant code with grep first.", [call],
                          True, tm.names(kinds))
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    hits = _search_hits(ev)
    if not code_read and not known and can_read:
        if hits:
            pool = list(hits)
            pool = _core_sources(pool, True, 400) or pool
            ranked = _rank(pool, kws, hits)[:2] or pool[:1]
            if ranked:
                return _read_step(pat, tm, ranked, 2, total)
        if not listing and _survey_count(ev) < 2:
            s = _survey_step(pat, tm, ev, 2, total)
            if s:
                return s
        docs = [d for d in _doc_files(listing) if not T._path_seen(d, read_set)]
        if docs:
            return _read_step(pat, tm, docs[:2], 2, total,
                              instruction="Read the project docs first: "
                                          + ", ".join(docs[:2]) + ".")
        entry = [f for f in _core_sources(listing, False, 200)
                 if not T._path_seen(f, read_set)][:3]
        if entry:
            return _read_step(pat, tm, entry, 3, total,
                              instruction="Read the main source files: "
                                          + ", ".join(entry) + ".")
    if not ev:
        return None
    return _custom_answer(pat, _explain_text(sorted(read_set)), 4, total)


# --------------------------------------------------------------------------
# Dispatcher + gate + install
# --------------------------------------------------------------------------

def _plan_s1(messages, tm, ctx):
    ctx = ctx or _analyze(messages)
    if ctx is None or not ctx.pattern:
        return None
    ev = _answered_calls(messages)
    pat = ctx.pattern
    if pat == "DEBUG":
        return _plan_debug(ctx, ev, tm)
    if pat == "DIFF_REVIEW":
        return _plan_diff(ctx, ev, tm)
    if pat == "CREATE":
        return _plan_create(ctx, ev, tm)
    if pat == "CHANGE":
        return _plan_change(ctx, ev, tm)
    if pat == "EXPLAIN":
        return _plan_explain(ctx, ev, tm)
    return _base_plan_for(messages, tm, ctx)


def plan_for_s1(messages, tm, ctx=None):
    if not _c("fix_enabled"):
        return _base_plan_for(messages, tm, ctx)
    try:
        return _plan(messages, tm, ctx)
    except Exception as e:      # never let the planner break a request
        log(f"[fix] planner error, using the original planner: {e}")
        return _base_plan_for(messages, tm, ctx)


_CONTENT_KEYS = ("content", "contents", "text", "fileText", "file_text")


def _gate_act_calls_s1(calls, messages):
    out, problems = _base_gate_act_calls(calls, messages)
    try:
        ratio = float(_c("fix_write_shrink_ratio"))
        texts = {}
        for e in _answered_calls(messages):
            if e["kind"] == "read" and _read_ok(e):
                p = _call_path(e["args"])
                if p:
                    texts[p] = _clean_file_text(e["result"])
        for tc in out:
            fn = tc.get("function") or {}
            if tool_kind(fn.get("name")) != "act":
                continue
            args = T._tool_arguments_dict(fn)
            if any(isinstance(args.get(k), str) and args[k] for k in _OLD_KEYS):
                continue                      # an edit: already checked
            new = next((args[k] for k in _CONTENT_KEYS
                        if isinstance(args.get(k), str)), None)
            path = _call_path(args)
            if new is None or not path:
                continue
            old = next((t for p, t in texts.items() if T._path_seen(path, [p])), None)
            if old is not None and len(old.strip()) >= 400 \
                    and len(new) < ratio * len(old):
                problems.append(
                    f"{path}: this write would replace a {len(old)}-char file "
                    f"you read with only {len(new)} chars -- use the edit tool "
                    "(oldString/newString) to change part of a file, or write "
                    "the COMPLETE new file")
    except Exception as e:
        log(f"[fix] write gate error: {e}")
    return out, problems


_SURVEY_CMD = "git ls-files"


def _test_command(listing):
    return (test_candidates(list(listing or [])) or [None])[0]


_FX_ORIG_CD = None


def _fx_install_s1(server):
    """Gọi từ install(): mở rộng tools.change_demanded (và bản import trong server)."""
    global _FX_ORIG_CD
    for k, v in _FX_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    if not CONFIG.get("fix_enabled", True):
        return
    cur = T.change_demanded
    if cur is _change_demanded:
        return
    _FX_ORIG_CD = cur
    T.change_demanded = _change_demanded
    if getattr(server, "change_demanded", None) is cur:
        server.change_demanded = _change_demanded
    log("[fix] change_demanded extended for create/merge requests")
# ==== fix_patch:end ====


# ==== flowfix2:begin (generated by flow_fix2_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: every caller resolves these names at call time).


# --------------------------------------------------------------------------
# 1+2. Listing: keep drive letters, read grep headers, drop truncated twins
# --------------------------------------------------------------------------

_FF2_GREP_HEAD_RE = re.compile(
    r'^\s*((?:[A-Za-z]:)?[^:*?"<>|\r\n]+?\.[A-Za-z0-9]{1,8})\s*:\s*$')
_FF2_GREP_INLINE_RE = re.compile(
    r'^\s*((?:[A-Za-z]:)?[^:*?"<>|\r\n]+?\.[A-Za-z0-9]{1,8}):\d+[:\-]')


def _listing_files(ev):
    """File paths from survey/search results (glob, ls, git ls-files, grep).

    Order of trust: whole-line paths (glob/ls) > grep header lines
    ``D:\\dir\\file.py:`` > ``path:line:text`` > scraping file-like tokens from
    the text. Scraping is the last resort because it cannot see the drive
    letter: ``D:/Project/x/README.md`` came out as ``Project/x/README.md``,
    which then won the "shortest path" tie-break and failed to read.
    """
    seen, out = set(), []

    def add(f):
        f = f.replace("\\", "/").strip()
        base = f.rsplit("/", 1)[-1].lower()
        if f and f not in seen and base not in T._NON_FILE_NAMES:
            seen.add(f)
            out.append(f)

    for e in ev:
        if not (e["kind"] in ("survey", "search") or (
                e["kind"] == "exec"
                and _LIST_CMD_RE.search(_call_command(e["args"])))):
            continue
        lines = e["result"].splitlines()
        matched = []
        for rx in (_PATH_LINE_RE, _FF2_GREP_HEAD_RE, _FF2_GREP_INLINE_RE):
            matched = [m.group(1) for m in map(rx.match, lines) if m]
            if matched:
                break
        if matched:
            for f in matched:
                add(f)
        else:
            body = e["result"].replace("\\", "/")
            for f in sorted(T._files_in(body), key=body.find):
                add(f)
    low = [f.lower() for f in out]
    # fix7: only drop a TRUNCATED twin (drive-less copy of a D:/... path); a
    # genuine relative file such as server.py next to pkg/server.py must stay.
    drive = re.compile(r"^[a-z]:/")
    return [f for f, l in zip(out, low)
            if not (not drive.match(l) and "/" in l
                    and any(drive.match(o) and o.endswith("/" + l)
                            for o in low))]


def _search_hits(ev):
    """``{path: occurrences}`` for real files named in search/grep results.

    Scraping ``_files_in`` over the whole result is wrong for grep output:
    the body embeds source snippets, so it invents filenames (``a.py``,
    ``pkg/a.py\\npkg/b.py``) and drops drive letters
    (``D:/Project/...`` -> ``Project/...``).  The planner then read paths
    that do not exist.  ``_listing_files`` reads the grep header lines
    instead, which keeps the full path; count occurrences in the body so
    the ranking still reflects how often a file matched.
    """
    hits = {}
    for e in ev:
        if e["kind"] != "search":
            continue
        body = e["result"].replace("\\", "/")
        for f in _listing_files([e]):
            hits[f] = hits.get(f, 0) + max(1, body.count(f))
    return hits


# --------------------------------------------------------------------------
# 1. Which files does "merge everything in X" mean?
# --------------------------------------------------------------------------



# --------------------------------------------------------------------------
# 3. merge_script: real single-file bundle for packages with relative imports
# --------------------------------------------------------------------------

_FF2_MERGE_TMPL = r'''# -*- coding: utf-8 -*-
# Temporary helper generated by the proxy; it deletes itself when done.
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC = @@SRC@@
OUT = @@OUT@@
C = @@C@@
IS_PY = @@PY@@

LOADER = r"""
import importlib.abc
import importlib.util
import linecache
import sys


class _BundleLoader(importlib.abc.Loader):
    def __init__(self, name):
        self._name = name

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        text, is_pkg, fname = _MODULES[self._name]
        module.__file__ = fname
        if is_pkg:
            module.__path__ = []
        linecache.cache[fname] = (len(text), None, text.splitlines(True), fname)
        exec(compile(text, fname, "exec", dont_inherit=True), module.__dict__)

    def get_source(self, fullname):
        return _MODULES[fullname][0]


class _BundleFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        entry = _MODULES.get(fullname)
        if entry is None:
            return None
        return importlib.util.spec_from_loader(
            fullname, _BundleLoader(fullname), is_package=entry[1])


if not any(isinstance(_f, _BundleFinder) for _f in sys.meta_path):
    sys.meta_path.insert(0, _BundleFinder())
"""


def stem(p):
    return os.path.splitext(os.path.basename(p))[0]


def read(p):
    with io.open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def write(path, data):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)


def plain(s):
    return str(s).encode("ascii", "replace").decode("ascii")


def smoke(path, name):
    # Run the bundle from a clean temp dir that has NO package folder next to
    # it: proves it is standalone, not just importable from the repo.
    tmp = tempfile.mkdtemp(prefix="bundle_smoke_")
    try:
        copy = os.path.join(tmp, name)
        shutil.copyfile(path, copy)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONPATH", None)
        probe = ("import importlib.util as u, sys\n"
                 "s = u.spec_from_file_location('_bundle', sys.argv[1])\n"
                 "m = u.module_from_spec(s)\n"
                 "s.loader.exec_module(m)\n"
                 "for n in m._MODULES:\n"
                 "    __import__(n)\n"
                 "print(len(m._MODULES))\n")
        runs = []
        for argv in ([sys.executable, "-c", probe, copy],
                     [sys.executable, copy, "--version"]):
            r = subprocess.run(argv, cwd=tmp, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=180)
            runs.append((r.returncode,
                         r.stdout.decode("utf-8", "replace").strip(),
                         r.stderr.decode("utf-8", "replace").strip()))
        (rc1, o1, e1), (rc2, o2, e2) = runs
        if rc1 != 0:
            return False, "importing the bundled modules failed:\n" + (e1 or o1)[-1500:]
        if "Traceback" in e2:
            return False, "running the bundle with --version failed:\n" + e2[-1500:]
        first = (o2 or e2).splitlines()
        return True, "all %s modules import in a clean dir; --version rc=%d %s" % (
            o1.splitlines()[-1] if o1 else "?", rc2, plain(first[0] if first else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


names = {stem(p): p for p in SRC}
texts = {p: read(p) for p in SRC}
inits = sorted((p for p in SRC if os.path.basename(p) == "__init__.py"), key=len)
rel_import = re.compile(r"(?m)^[ \t]*from[ \t]+\.")
use_bundle = IS_PY and bool(inits) and any(rel_import.search(t) for t in texts.values())


def build_bundle():
    root = os.path.dirname(inits[0])
    pkg = os.path.basename(root)
    mods = []
    for p in sorted(SRC):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        if rel.startswith(".."):
            mods.append((stem(p), False, texts[p], os.path.basename(p)))
            continue
        parts = rel[:-3].split("/")
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        mods.append((".".join([pkg] + parts), is_pkg, texts[p], pkg + "/" + rel))
    entry = pkg + ".__main__"
    has_entry = any(m[0] == entry for m in mods)
    body = ["#!/usr/bin/env python3",
            "# -*- coding: utf-8 -*-",
            "# Single-file bundle of the %s package (%d modules), generated by the proxy." % (pkg, len(mods)),
            "# Every module source is embedded below and served by an import hook, so",
            "# relative imports keep working. Edit the package and regenerate, not this file.",
            "_MODULES = {"]
    for name, is_pkg, text, fname in mods:
        body.append("    %r: (%r, %r, %r)," % (name, text, is_pkg, fname))
    body.append("}")
    body.append(LOADER)
    if has_entry:
        body.append("")
        body.append('if __name__ == "__main__":')
        body.append("    import importlib")
        body.append("    _entry = importlib.import_module(%r)" % entry)
        body.append('    _main = getattr(_entry, "main", None)')
        body.append("    if callable(_main):")
        body.append("        _main()")
    return "\n".join(body) + "\n", [m[0] for m in mods]


def deps(text):
    out = []
    for m in re.finditer(r"(?m)^[ \t]*from[ \t]+\.+(\w*)[ \t]+import[ \t]+([^\n#]+)", text):
        if m.group(1):
            out.append(m.group(1))
        else:
            for t in m.group(2).strip("() ").split(","):
                out.append(t.split(" as ")[0].strip())
    return [d for d in out if d in names]


order, seen = [], set()


def visit(p):
    if p in seen:
        return
    seen.add(p)
    for d in deps(texts[p]):
        visit(names[d])
    order.append(p)


try:
    out_dir = os.path.dirname(OUT)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    if use_bundle:
        data, mod_names = build_bundle()
        tmp_out = OUT + ".tmp"
        write(tmp_out, data)
        ok, msg = smoke(tmp_out, os.path.basename(OUT))
        if not ok:
            os.remove(tmp_out)
            print("SMOKE TEST FAILED -- the output file was NOT written")
            print(plain(msg))
            sys.exit(1)
        os.replace(tmp_out, OUT)
        print("merged %d files -> %s (%d bytes)" % (len(mod_names), OUT, len(data.encode("utf-8"))))
        print("mode: package bundle (every module embedded, import hook installed)")
        print("smoke: " + plain(msg))
        print("order: " + ", ".join(mod_names))
    else:
        rest = sorted(p for p in SRC if stem(p) not in ("__init__", "__main__"))
        firsts = [p for p in SRC if stem(p) == "__init__"]
        lasts = [p for p in SRC if stem(p) == "__main__"]
        for p in firsts + rest + lasts:
            visit(p)
        bar = C + " " + "=" * 60
        parts = []
        if IS_PY:
            parts.append("# -*- coding: utf-8 -*-\n\"\"\"Combined module generated from %d files.\"\"\"\n" % len(order))
        for p in order:
            parts.append("\n" + bar + "\n" + C + " FILE: " + os.path.basename(p) + "\n" + bar
                         + "\n\n" + texts[p].rstrip() + "\n")
        data = "".join(parts)
        write(OUT, data)
        print("merged %d files -> %s (%d bytes)" % (len(order), OUT, len(data.encode("utf-8"))))
        print("mode: plain concatenation")
        print("order: " + ", ".join(os.path.basename(p) for p in order))
finally:
    try:
        os.remove(__file__)
    except OSError:
        pass
'''


def merge_script(srcs, out_path, ext):
    comment = "//" if ext in _SLASH_COMMENT_EXT else "#"
    return (_FF2_MERGE_TMPL.replace("@@SRC@@", json.dumps(list(srcs)))
            .replace("@@OUT@@", json.dumps(out_path))
            .replace("@@C@@", json.dumps(comment))
            .replace("@@PY@@", "True" if ext == ".py" else "False"))


# --------------------------------------------------------------------------
# 5. Honest final answer once the bundle has been verified
# --------------------------------------------------------------------------

def _ff2_merge_output(ev):
    ran = [e for e in ev if e["kind"] == "exec"
           and _MERGE_SCRIPT in _call_command(e["args"])]
    return (ran[-1]["result"] if ran else "") or ""


def _ff2_bundle_answer(res):
    facts = " | ".join(ln.strip() for ln in res.splitlines()
                       if ln.strip())[:700]
    facts = facts.encode("ascii", "replace").decode("ascii")
    return ("DONE -- the file already exists and the helper VERIFIED it. Reply "
            "in the user's language, plainly: the package was merged into ONE "
            "standalone file. It embeds the source of every module and installs "
            "an import hook, so the relative imports keep working and the "
            "program behaves exactly as `python -m <package>` (same CLI "
            "options). It was tested in a clean temp directory with no package "
            "folder next to it: every embedded module imports and `--version` "
            "runs. Facts printed by the helper: " + facts + ". Not verified: a "
            "real request through Gemini. Mention the trade-off: sources are "
            "embedded as strings, so edit the package and regenerate rather than "
            "editing the bundle. Do NOT give the user a script or steps to do by "
            "hand; the temporary helper already deleted itself.")


def _plan_create_s2(ctx, ev, tm):
    step = _plan_create_s1(ctx, ev, tm)
    try:
        if (step is not None and step.name == "answer"
                and (step.instruction or "").startswith(
                    "DONE -- the file already exists")):
            res = _ff2_merge_output(ev)
            if "package bundle" in res:
                step.instruction = _ff2_bundle_answer(res)
    except Exception as e:
        log(f"[fix2] bundle answer error: {e}")
    return step


# --------------------------------------------------------------------------
# 4. Refuse a stub write for a "merge files" request
# --------------------------------------------------------------------------

def _calc_read_source_total(messages):
    total = 0
    for e in _answered_calls(messages):
        if e.get("kind") != "read" or not _read_ok(e):
            continue
        p = _call_path(e.get("args"))
        if (p and not _is_doc(p) and not _is_test_path(p)
                and p.lower().endswith(_SRC_EXT)):
            total += len(_clean_file_text(e.get("result", "")))
    return total


def _ff2_stub_problems(calls, messages):
    ctx = _analyze(messages)
    if (ctx is None or getattr(ctx, "pattern", None) != "CREATE"
            or not getattr(ctx, "create_merge", False)):
        return []
    total = _calc_read_source_total(messages)
    problems = []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue
        args = T._tool_arguments_dict(fn)
        if any(isinstance(args.get(k), str) and args[k] for k in _OLD_KEYS):
            continue
        new = next((args[k] for k in _CONTENT_KEYS
                    if isinstance(args.get(k), str)), None)
        path = _call_path(args)
        if new is None or not path or _MERGE_SCRIPT in path:
            continue
        if len(new) < max(400, total // 2):
            problems.append(
                f"{path}: this write holds only {len(new)} chars, but a merged "
                f"file must contain the COMPLETE code of every source file (the "
                f"files you read alone are {total} chars). Do not write a "
                "placeholder; read every file and include all of it, or use the "
                "bash tool to run a script that concatenates them")
    return problems


def _gate_act_calls_s2(calls, messages):
    out, problems = _gate_act_calls_s1(calls, messages)
    try:
        problems = list(problems) + _ff2_stub_problems(out, messages)
    except Exception as e:
        log(f"[fix2] stub gate error: {e}")
    return out, problems
# ==== flowfix2:end ====


# ==== flowfix3:begin (generated by flow_fix3_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: every caller resolves these names at call time).


def _ff3_key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# --------------------------------------------------------------------------
# 1. Which files does "merge everything in X" mean?  (no "folder" word needed)
# --------------------------------------------------------------------------

def _merge_sources(ctx, listing):
    target = _norm(getattr(ctx, "target", "") or "")
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    live = [f for f in _live(listing) if f.lower().endswith(ext)]
    tbase = posixpath.basename(target).lower()

    def usable(f):
        return (not _is_test_path(f) and _base(f) != tbase
                and _base(f) != _MERGE_SCRIPT)

    def group(match):
        groups = {}
        for f in live:
            segs = f.split("/")
            for i, s in enumerate(segs[:-1]):
                if match(s):
                    groups.setdefault("/".join(segs[:i + 1]), []).append(f)
        return groups

    def pick(groups):
        best = None
        for prefix, fl in groups.items():
            files = sorted({f for f in fl if usable(f)})
            direct = sum(1 for f in files if posixpath.dirname(f) == prefix)
            score = (direct, len(prefix))
            if len(files) >= 2 and (best is None or score > best[0]):
                best = (score, files, prefix)
        return (best[1], best[2]) if best else None

    cands = list(getattr(ctx, "dir_cands", []) or [])
    raw = getattr(ctx, "last", "") or ""
    for tok in re.findall(r"[A-Za-z0-9_.\-]+", raw):
        tok = tok.strip(".-")
        if len(tok) >= 3:
            cands.append(tok)
    stem_t = posixpath.splitext(posixpath.basename(target))[0]
    if stem_t:
        cands.append(stem_t)
    names, seen = [], set()
    for c in cands:
        dl = _norm(c).strip("/").split("/")[-1].lower()
        if dl and dl not in seen:
            seen.add(dl)
            names.append(dl)
    # pass 1: exact directory name; pass 2: ignore '-' / '_' / '.' differences
    for exact in (True, False):
        for dl in names:
            if exact:
                hit = pick(group(lambda s, d=dl: s.lower() == d))
            else:
                k = _ff3_key(dl)
                if len(k) < 4:
                    continue
                hit = pick(group(lambda s, k=k: _ff3_key(s) == k))
            if hit:
                return hit
    if ext == ".py":
        best = None
        pkgs = sorted({posixpath.dirname(f) for f in live
                       if _base(f) == "__init__.py" and not _is_test_path(f)},
                      key=len)
        for d in pkgs:
            files = sorted(f for f in live
                           if f.startswith(d + "/") and usable(f))
            if len(files) >= 2 and (best is None or len(files) > len(best[0])):
                best = (files, d)
        if best:
            return best
    return [], ""


# --------------------------------------------------------------------------
# 2. Honest answer when the verified merge helper never ran
# --------------------------------------------------------------------------

def _ff3_merge_output(ev):
    ran = [e for e in ev if e["kind"] == "exec"
           and _MERGE_SCRIPT in _call_command(e["args"])]
    return (ran[-1]["result"] if ran else "") or ""


def _ff3_why(ctx, ev, tm):
    listing = _listing_files(ev)
    srcs, _p = _merge_sources(ctx, listing)
    return ("target=%r write_tool=%r exec_tool=%r listing=%d merge_sources=%d"
            % (getattr(ctx, "target", ""), _write_tool(tm), tm.first("exec"),
               len(listing), len(srcs)))


def _ff3_honest(ctx, ev):
    wrote = []
    for e in ev:
        if e["kind"] != "act" or not _act_ok(e):
            continue
        a = e["args"]
        p = _call_path(a)
        c = next((a[k] for k in _CONTENT_KEYS if isinstance(a.get(k), str)), None)
        if p:
            wrote.append("%s (%s chars)" % (p, len(c) if c is not None else "?"))
    read_total = 0
    for e in ev:
        if e["kind"] == "read" and _read_ok(e):
            p = _call_path(e["args"]) or ""
            if p.lower().endswith(_SRC_EXT) and not _is_doc(p) and not _is_test_path(p):
                read_total += len(_clean_file_text(e["result"]))
    return ("HONESTY RULE -- the proxy's verified merge helper did NOT run, so the "
            "single-file merge is NOT done and NOT verified. Reply in the user's "
            "language, plainly: do NOT say the files were merged, do NOT say the "
            "program runs from one file. Facts: file(s) written by you: "
            + (", ".join(wrote) or "none") + "; source text you actually read: "
            + str(read_total) + " chars (not every file); the output was never "
            "compiled or run; the unit tests passing says nothing about it. State "
            "what is missing and offer to retry the merge.")


def _plan_create_s3(ctx, ev, tm):
    step = _plan_create_s2(ctx, ev, tm)
    try:
        if step is None or not getattr(ctx, "create_merge", False):
            return step
        if step.name not in ("search", "read", "act", "tests", "answer"):
            return step
        if _ff3_merge_output(ev):
            return step                     # helper ran: ff2 reports honestly
        log("[fix3] merge request is NOT on the verified helper path ("
            + _ff3_why(ctx, ev, tm) + "); step=" + step.name)
        if step.name in ("tests", "answer"):
            return _custom_answer("CREATE", _ff3_honest(ctx, ev), 5, 5)
    except Exception as e:
        log("[fix3] create guard error: %s" % e)
    return step


# --------------------------------------------------------------------------
# 3. Never let a stub write reach the client for a merge request
# --------------------------------------------------------------------------

def _ff3_stub_problems(calls, messages):
    ctx = _analyze(messages)
    if (ctx is None or getattr(ctx, "pattern", None) != "CREATE"
            or not getattr(ctx, "create_merge", False)):
        return []
    total = _calc_read_source_total(messages)
    problems = []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue
        args = T._tool_arguments_dict(fn)
        if any(isinstance(args.get(k), str) and args[k] for k in _OLD_KEYS):
            continue
        new = next((args[k] for k in _CONTENT_KEYS
                    if isinstance(args.get(k), str)), None)
        path = _call_path(args)
        if new is None or not path or _MERGE_SCRIPT in path:
            continue
        tiny = len(new) < max(400, total // 2)
        fake = "placeholder" in new.lower() and len(new) < 5000
        if tiny or fake:
            problems.append("%s: write of %d chars is a stub/placeholder, not a "
                            "merge (sources read: %d chars)" % (path, len(new), total))
    return problems


def _ff3_postfilter(result, messages, allowed_names, tool_schemas,
                    tool_required, tools_active):
    if not (tools_active and messages and isinstance(result, tuple)
            and len(result) == 2):
        return result
    text, calls = result
    if not calls:
        return result
    probs = _ff3_stub_problems(calls, messages)
    if not probs:
        return result
    log("[fix3] blocked a stub write for a merge request: " + " | ".join(probs))
    tm = ToolMap(allowed_names, tool_schemas, tool_required)
    ctx = _analyze(messages)
    ev = _answered_calls(messages)
    listing = _listing_files(ev)
    srcs, prefix = _merge_sources(ctx, listing)
    helper_written = any(e["kind"] == "act" and _MERGE_SCRIPT in (_call_path(e["args"]) or "")
                         for e in ev)
    if srcs and not helper_written and _write_tool(tm) and tm.first("exec"):
        target = _norm(ctx.target)
        root = posixpath.dirname(prefix)
        out_path = target if "/" in target else (
            posixpath.join(root, target) if root else target)
        ext = (posixpath.splitext(target)[1] or ".py").lower()
        call = _write_call(tm, _MERGE_SCRIPT, merge_script(srcs, out_path, ext))
        if call:
            log("[fix3] replaced the stub by the verified merge helper (%d files)"
                % len(srcs))
            return "", [call]
    why = _ff3_why(ctx, ev, tm)
    if _VI_RE.search((ctx.last or "").lower()):
        msg = ("Mình CHƯA gộp được file. Model chỉ sinh ra bản giữ chỗ "
               "(placeholder) nên proxy đã chặn, không ghi gì xuống đĩa. "
               "Lý do không chạy được bước gộp tự động: " + why +
               ". Hãy gửi lại kèm tên thư mục cần gộp, ví dụ: "
               "gộp thư mục gemini_web2api thành gemini_web2api.py.")
    else:
        msg = ("I did NOT merge the files. The model only produced a placeholder, "
               "so the proxy blocked it and nothing was written. Why the automatic "
               "merge could not run: " + why + ". Retry naming the folder to merge.")
    return msg, None


def _wrap_ff3(cur):
    @functools.wraps(cur)
    def wrapper3(self, prompt, model_id, think_mode, file_refs, extra,
                 tools_active, allowed_names, tool_schemas, required_tool,
                 messages=None, ticket=None, tool_required=None,
                 client_gone=None):
        result = cur(self, prompt, model_id, think_mode, file_refs, extra,
                     tools_active, allowed_names, tool_schemas, required_tool,
                     messages=messages, ticket=ticket,
                     tool_required=tool_required, client_gone=client_gone)
        try:
            return _ff3_postfilter(result, messages, allowed_names,
                                   tool_schemas, tool_required, tools_active)
        except Exception as e:
            log("[fix3] postfilter error: %s" % e)
            return result

    wrapper3._flow3_patched = True
    return wrapper3
# ==== flowfix3:end ====


# ==== flowfix4:begin (generated by flow_fix4_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: every caller resolves these names at call time).


# --------------------------------------------------------------------------
# 1. "doc lap" (độc lập) is not "doc" (đọc): stop mis-routing create/merge
#    requests to FILE_REVIEW
# --------------------------------------------------------------------------

_FF4_NOISE_RE = re.compile(r"\bdoc\s+(?:lap|quyen|dao|ac|hai)\b", re.IGNORECASE)


def _ff4_scrub(folded):
    return _FF4_NOISE_RE.sub(" ", folded or "")


def _ff4_wrap_classifiers():
    """Patch tools._is_file_review/_is_source_review (and server's copies)."""
    from gemini_web2api import server
    if getattr(T._is_file_review, "_ff4", False):
        return
    orig_file, orig_src = T._is_file_review, T._is_source_review

    def is_file_review(folded, scannable):
        folded = _ff4_scrub(folded)
        # A create/merge request with no explicit review word is not a review,
        # even if a weak verb ("kiem tra", "tim") and a file name are present.
        if _create_intent(folded, scannable) and not _REVIEW_RE.search(folded.lower()):
            return False
        return orig_file(folded, scannable)

    def is_source_review(folded):
        return orig_src(_ff4_scrub(folded))

    is_file_review._ff4 = True
    is_source_review._ff4 = True
    T._is_file_review = is_file_review
    T._is_source_review = is_source_review
    server._is_file_review = is_file_review
    server._is_source_review = is_source_review


def _analyze_s2(messages):
    ctx = _analyze_s1(messages)
    try:
        if (ctx is not None and _c("fix_enabled") and _c("fix_create_flow")
                and getattr(ctx, "pattern", None) in ("FILE_REVIEW", "GENERIC_REVIEW")
                and not _clarified(messages, ctx.idx)):
            folded = _ff4_scrub(ctx.folded or "")
            if (not _REVIEW_RE.search(folded.lower())
                    and _create_intent(folded, ctx.scannable, ctx.last)
                    and (_named_files(ctx)
                         or _MERGE_RE.search(folded.lower()))):
                _apply_create_intent(ctx, folded)
    except Exception as e:
        log(f"[fix4] analyze error: {e}")
    return ctx


# --------------------------------------------------------------------------
# 2. More canned upstream sentences
# --------------------------------------------------------------------------

_FF4_CANNED = (
    re.compile(r"\bi\s+seem\s+to\s+be\s+encountering\s+an\s+error\b", re.IGNORECASE),
    re.compile(r"\bhaving\s+a\s+hard\s+time\s+fulfilling\s+your\s+request\b",
               re.IGNORECASE),
    re.compile(r"\bi\s+encountered\s+an\s+error\s+doing\s+what\s+you\s+asked\b",
               re.IGNORECASE),
    re.compile(r"\bsorry,?\s+something\s+went\s+wrong\b", re.IGNORECASE),
)


def _ff4_extend_canned():
    cur = tuple(T._CANNED_FAILURE_RES)
    have = {r.pattern for r in cur}
    extra = tuple(r for r in _FF4_CANNED if r.pattern not in have)
    if extra:
        T._CANNED_FAILURE_RES = cur + extra


# --------------------------------------------------------------------------
# 3. Truncated reads: continue them, and never report them as fully read
# --------------------------------------------------------------------------

_FF4_TRUNC_BASE = re.compile(
    r"\[[^\]\n]*truncated[^\]\n]*?offset:?\s*(\d+)[^\]\n]*\]", re.IGNORECASE)
_FF9_TRUNC_PAREN = re.compile(
    r"\([^()\n]*?(?:showing\s+lines|truncated|more\s+lines)[^()\n]*?"
    r"\boffset\s*[=:]\s*(\d+)[^()\n]*\)", re.IGNORECASE)
_FF15_TRUNC_LEGACY = re.compile(
    r"\(\s*(?:file\s+has\s+more\s+lines|output\s+(?:truncated|capped))[^()\n]*?\b(?:beyond\s+)?line\s+(\d+)[^()\n]*\)", re.I)


class _Ff9Trunc:
    """Drop-in for the compiled regex: findall / search / sub only."""

    def __init__(self):
        self._rxs = (_FF4_TRUNC_BASE, _FF9_TRUNC_PAREN, _FF15_TRUNC_LEGACY)

    @staticmethod
    def _is_code_literal(text, m):
        s = m.group(0)
        if '"' in s or "\\n" in s:
            return True
        after = (text or "")[m.end():m.end() + 1]
        if after in ('"', "'"):
            return True
        return False

    def findall(self, text):
        hits = []
        t = text or ""
        for rx in self._rxs:
            for m in rx.finditer(t):
                if not self._is_code_literal(t, m):
                    hits.append((m.start(), m.group(1)))
        return [v for _s, v in sorted(hits)]

    def search(self, text):
        t = text or ""
        for rx in self._rxs:
            for m in rx.finditer(t):
                if not self._is_code_literal(t, m):
                    return m
        return None

    def sub(self, repl, text, count=0):
        t = text or ""
        for rx in self._rxs:
            replaced = [0]
            def _repl(m, _rx=rx):
                if count and replaced[0] >= count:
                    return m.group(0)
                if self._is_code_literal(t, m):
                    return m.group(0)
                replaced[0] += 1
                return repl(m) if callable(repl) else m.expand(repl)
            t = rx.sub(_repl, t)
        return t


_FF4_TRUNC_RE = _Ff9Trunc()
_FF4_MAX_CONT = 32


def _ff4_off(args):
    key = _pick(args or {}, _OFFSET_KEYS)
    try:
        return int((args or {}).get(key)) if key else 0
    except (TypeError, ValueError):
        return 0


def _ff4_offset_key(tm):
    name = tm.first("read")
    props = (tm.schemas.get(name) if name else None) or set()
    return _pick(props, _OFFSET_KEYS)


def _ff5_limit_key(tm):
    name = tm.first("read")
    props = (tm.schemas.get(name) if name else None) or set()
    return _pick(props, _LIMIT_KEYS)


def _ff5_enlarge_read_calls(partial, ev, tm):
    """Retry truncated reads with a larger ``limit`` when the tool has one.

    A client whose read tool exposes ``limit`` but no ``offset`` cannot be
    continued at an offset. Re-reading the same path with a limit four times
    the previous one (at least 4000) is the only way to get the rest of the
    file. A path already retried more than four times is left to the caller's
    refusal so a broken tool cannot loop forever.
    """
    lkey = _ff5_limit_key(tm)
    if not lkey:
        return []
    out = []
    for e in partial[:3]:
        path = _call_path(e.get("args") or {})
        if not path:
            continue
        n_reads = sum(1 for x in ev if x["kind"] == "read"
                      and T._path_seen(path, [_call_path(x["args"])]))
        if n_reads > 4:
            continue
        args = dict(e.get("args") or {})
        try:
            current = int(args.get(lkey)) if args.get(lkey) is not None else 0
        except (TypeError, ValueError):
            current = 0
        limit = max(current * 4, 4000)
        args[lkey] = limit
        log(f"[flow] read truncated; retrying with limit={limit} for {path}")
        out.append({"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                    "function": {"name": e.get("name") or "read",
                                 "arguments": json.dumps(args, ensure_ascii=False)}})
    return out


def _ff4_truncations(ev):
    """[(event_index, path, next_offset)] for reads whose output was cut off."""
    out = []
    for i, e in enumerate(ev):
        if e["kind"] != "read" or not _read_ok(e):
            continue
        hits = _FF4_TRUNC_RE.findall((e["result"] or "")[-400:])
        path = _call_path(e["args"])
        if hits and path:
            out.append((i, path, int(hits[-1])))
    return out


def _ff4_unfinished(ev):
    """Truncations with no later *successful* read at or beyond their offset.
    Also ignores bounded window re-reads (e.g. offset=30, limit=120) when the file
    was already read past `nxt` earlier in `ev`.
    """
    out = []
    for i, path, nxt in _ff4_truncations(ev):
        later = [_ff4_off(x["args"]) for x in ev[i + 1:]
                 if x["kind"] == "read" and _read_ok(x)
                 and T._path_seen(path, [_call_path(x["args"])])]
        if any(o >= nxt for o in later):
            continue
        # Check if this is a targeted window re-read of a file already read past `nxt`
        earlier_max = max(
            [_ff4_off(x["args"]) for x in ev[:i]
             if x["kind"] == "read" and _read_ok(x)
             and T._path_seen(path, [_call_path(x["args"])])] or [-1]
        )
        earlier_untouched = not any(
            x["kind"] == "act" and _act_ok(x)
            and T._path_seen(path, [_call_path(x["args"])])
            for x in ev[:i]
        )
        if earlier_max >= nxt and earlier_untouched:
            continue
        out.append((i, path, nxt))
    return out


def _ff4_continuations(ev, okey):
    """[(path, tool_call)] reading the rest of every truncated file."""
    if not okey:
        return []
    out, seen = [], set()
    deleted = _ff4_deleted(ev)
    for i, path, nxt in _ff4_unfinished(ev):
        if any(T._path_seen(path, [p]) for p in deleted):
            continue
        if (path, nxt) in seen:
            continue
        seen.add((path, nxt))
        max_cont = int(CONFIG.get("flow4_max_continuations", _FF4_MAX_CONT) or _FF4_MAX_CONT)
        n_reads = sum(1 for x in ev
                      if x["kind"] == "read"
                      and T._path_seen(path, [_call_path(x["args"])]))
        if n_reads > max_cont:
            continue
        args = dict(ev[i]["args"])
        # Drop small explicit window limits (<= 500 lines) when continuing via offset
        for lk in ("limit", "max_lines", "line_limit", "num_lines", "count", "n"):
            if lk in args and lk != okey:
                try:
                    if int(args[lk]) <= 500:
                        del args[lk]
                except Exception:
                    pass
        args[okey] = nxt
        out.append((path, {
            "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": ev[i]["name"],
                         "arguments": json.dumps(args, ensure_ascii=False)}}))
    return out[:6]


def plan_for_s2(messages, tm, ctx=None):
    step = plan_for_s1(messages, tm, ctx)
    try:
        if (step is not None and step.name == "answer"
                and step.pattern in ("GENERIC_REVIEW", "FILE_REVIEW", "EXPLAIN",
                                     "DEBUG", "DIFF_REVIEW")):
            pairs = _ff4_continuations(_answered_calls(messages),
                                       _ff4_offset_key(tm))
            if pairs:
                names = ", ".join(sorted({p for p, _c2 in pairs}))
                return Step(step.pattern, "read", step.index, step.total,
                            {"read"},
                            "A previous read was cut off (output truncated). "
                            "Read the rest of: " + names + ".",
                            [c for _p, c in pairs], True, tm.names({"read"}))
    except Exception as e:
        log(f"[fix4] continuation planner error: {e}")
    return step


def _review_materials(messages):
    """Like the original, but several reads of one file are concatenated."""
    ev = _answered_calls(messages)
    order, bodies = [], {}
    for e in ev:
        if e["kind"] == "read" and _read_ok(e):
            p = _call_path(e["args"])
            if not p:
                continue
            body = _strip_read_envelope(_FF4_TRUNC_RE.sub("", e["result"]).rstrip())
            if p in bodies:
                bodies[p] += "\n" + body
            else:
                bodies[p] = body
                order.append(p)
    tests = [e for e in ev if e["kind"] == "exec"
             and _is_test_cmd(_call_command(e["args"]))]
    return [(p, bodies[p]) for p in order], tests


def _map_reduce_s2(messages, step, ctx, gen, client_gone=None):
    report = _map_reduce_s1(messages, step, ctx, gen, client_gone)
    if not report:
        return report
    try:
        part = sorted({p for _i, p, _n in _ff4_unfinished(_answered_calls(messages))})
        if part:
            if _VI_RE.search((ctx.last or "").lower()):
                note = ("**Coverage một phần:** các file sau bị cắt khi đọc "
                        "(output truncated) và chưa đọc hết, phần cuối chưa được "
                        "review: ")
            else:
                note = ("**Partial coverage:** these files were cut off when read "
                        "(output truncated) and not read to the end: ")
            report += "\n\n" + note + ", ".join(part)
    except Exception as e:
        log(f"[fix4] coverage note error: {e}")
    return report


# --------------------------------------------------------------------------
# 4. Final CHANGE answer failed upstream: summarise from the tool results
# --------------------------------------------------------------------------

def _ff4_local_answer(exc, messages, tools_active):
    if not (tools_active and messages and "placeholder" in str(exc).lower()):
        return None
    last = messages[-1]
    if not (isinstance(last, dict) and last.get("role") == "tool"):
        return None
    ctx = _analyze(messages)
    if ctx is None or ctx.pattern != "CHANGE":
        return None
    ev = _answered_calls(messages)
    acts = [e for e in ev if e["kind"] == "act"]
    if not acts:
        return None
    done = [e for e in acts if _act_ok(e)]
    bad = [e for e in acts if not _act_ok(e)]
    last_act = max(i for i, e in enumerate(ev) if e["kind"] == "act")
    runs = [r for r in _test_runs(ev) if r[0] > last_act]
    outcome = test_outcome(runs[-1][2]) if runs else None
    summ = " | ".join(_test_summary([{"result": runs[-1][2]}])) if runs else ""
    files = sorted({_call_path(e["args"]) or "?" for e in done})
    vi = bool(_VI_RE.search((ctx.last or "").lower()))
    lines = []
    if vi:
        lines.append(("Đã áp dụng %d thay đổi: %s." % (len(done), ", ".join(files)))
                     if done else "Chưa có thay đổi nào được áp dụng thành công.")
        if bad:
            lines.append("Có %d lần sửa bị lỗi (lần cuối: %s)." % (
                len(bad), " ".join((bad[-1]["result"] or "").split())[:200]))
        if outcome == "pass":
            lines.append("Test sau lần sửa cuối: PASS -- " + summ)
        elif outcome == "fail":
            lines.append("Test sau lần sửa cuối: FAIL.\n" + _excerpt(runs[-1][2], 800))
        elif outcome == "broken":
            lines.append("Lệnh test không chạy được sau lần sửa cuối.")
        else:
            lines.append("Chưa có kết quả test hợp lệ sau lần sửa cuối.")
        lines.append("(Upstream trả về câu báo lỗi thay vì lời tổng kết, nên proxy "
                     "tự tóm tắt từ kết quả công cụ. Model chưa đánh giá lại nội "
                     "dung thay đổi; hãy xem lại diff.)")
    else:
        lines.append(("Applied %d change(s): %s." % (len(done), ", ".join(files)))
                     if done else "No change was applied successfully.")
        if bad:
            lines.append("%d edit(s) failed (last: %s)." % (
                len(bad), " ".join((bad[-1]["result"] or "").split())[:200]))
        if outcome == "pass":
            lines.append("Tests after the last edit: PASS -- " + summ)
        elif outcome == "fail":
            lines.append("Tests after the last edit: FAIL.\n" + _excerpt(runs[-1][2], 800))
        elif outcome == "broken":
            lines.append("The test command could not run after the last edit.")
        else:
            lines.append("No valid test result after the last edit.")
        lines.append("(Upstream returned an error sentence instead of a summary, so "
                     "the proxy summarised the tool results itself. The model did "
                     "not re-review the change; please check the diff.)")
    return "\n\n".join(lines)


def _wrap_ff4(cur):
    @functools.wraps(cur)
    def wrapper4(self, prompt, model_id, think_mode, file_refs, extra,
                 tools_active, allowed_names, tool_schemas, required_tool,
                 messages=None, ticket=None, tool_required=None,
                 client_gone=None):
        try:
            return cur(self, prompt, model_id, think_mode, file_refs, extra,
                       tools_active, allowed_names, tool_schemas, required_tool,
                       messages=messages, ticket=ticket,
                       tool_required=tool_required, client_gone=client_gone)
        except Exception as e:
            try:
                text = _ff4_local_answer(e, messages, tools_active)
            except Exception as inner:
                log(f"[fix4] local answer failed: {inner}")
                text = None
            if text is None:
                raise
            log("[fix4] answer step failed upstream; returning a local summary")
            return text, None

    wrapper4._flow4_patched = True
    return wrapper4
# ==== flowfix4:end ====

# ==== flowfix5:begin (generated by flow_fix5_patch.py -- re-run to refresh, --revert to remove) ====
import hashlib as _ff5_hashlib
import threading as _ff5_threading
import time as _ff5_time
import ast as _ff5_ast




_c5 = _flow_c


# One table drives the classifier catalog, the prompt banner, the step engine
# and the permission checks, so they cannot disagree (proposals 2 + 6).
# `edit` = may write files; `run` = "any" shell command, "readonly" (listing,
# git status/diff/log, test runners) or "none"; `steps` = canonical shared
# pipeline from which the planner, banner and validator are generated.
_FF5_SPEC = {
    "GENERIC_REVIEW": {"desc": "review / audit the whole project or its source code for bugs",
                       "steps": ("discover", "analyze", "verify", "read", "answer"),
                       "edit": False, "run": "readonly"},
    "FILE_REVIEW": {"desc": "",  # not offered to the classifier: the repo's own detector owns it
                    "steps": ("read", "analyze", "answer"),
                    "edit": False, "run": "readonly"},
    "CHANGE": {"desc": "modify, fix or implement something in EXISTING code",
               "steps": ("discover", "read", "edit", "diff", "verify", "answer"),
               "edit": True, "run": "any"},
    "CREATE": {"desc": "create a new file, script or module (or merge several files into one)",
               "steps": ("discover", "read", "edit", "verify", "answer"),
               "edit": True, "run": "any"},
    "EXPLAIN": {"desc": "explain how code works, where something lives, or how to do something (guidance only, no edits)",
                "steps": ("discover", "read", "answer"),
                "edit": False, "run": "readonly"},
    "DEBUG": {"desc": "investigate a failing test, an error message or a crash",
              "steps": ("discover", "read", "reproduce", "analyze", "answer"),
              "edit": False, "run": "readonly"},
    "DIFF_REVIEW": {"desc": "review the current uncommitted git changes",
                    "steps": ("diff", "read", "analyze", "answer"),
                    "edit": False, "run": "readonly"},
    "PLAN": {"desc": "write an implementation plan for a feature or change (no edits)",
             "steps": ("discover", "read", "analyze", "answer"),
             "edit": False, "run": "readonly"},
    "REVERT": {"desc": "undo / revert the changes made earlier in this conversation",
               "steps": ("diff", "edit", "verify", "answer"),
               "edit": True, "run": "any"},
    "RUN_TESTS": {"desc": "run the unit tests and report the result",
                  "steps": ("verify", "answer"),
                  "edit": False, "run": "readonly"},
    "RUN_COMMAND": {"desc": "run one specific shell command that the user wrote",
                    "steps": ("verify", "answer"),
                    "edit": False, "run": "any"},
    "GIT_INFO": {"desc": "show git status / log / diff / branch information",
                 "steps": ("diff", "answer"),
                 "edit": False, "run": "readonly"},
    "TOOL_DEMAND": {"desc": "", "steps": ("edit", "answer"),
                    "edit": True, "run": "any"},
}
_FF5_CATALOG = {k: v["desc"] for k, v in _FF5_SPEC.items() if v.get("desc")}
# Patterns planned entirely by this block (deterministic, no upstream call).
_FF5_OWN = ("REVERT", "RUN_TESTS", "RUN_COMMAND", "GIT_INFO")

# `DEBUG` only gets the edit stage when the user actually asked for a fix.
# The helper is used by the planner, banner and validator alike.
def _ff5_allowed_steps(pattern, fix=False):
    spec = _FF5_SPEC.get(pattern or "", {})
    steps = list(spec.get("steps", ()))
    if pattern == "DEBUG" and fix and "edit" not in steps:
        steps = steps[:-1] + ["edit", "diff", "verify", "answer"]
    if pattern != "DEBUG" or fix:
        return steps
    return [s for s in steps if s not in ("edit", "diff", "verify")]


def _ff5_pat(value):
    """Normalise a pattern label; every SPEC consumer compares the same form."""
    return str(value or "").strip().upper()


def _ff5_spec_edit(pattern, fix=False):
    # Fail closed: an unknown/empty pattern grants NO edit rights.
    spec = _FF5_SPEC.get(_ff5_pat(pattern), {})
    return bool(spec.get("edit", False) or (_ff5_pat(pattern) == "DEBUG" and fix))


def _ff5_spec_run(pattern):
    # Fail closed: an unknown/empty pattern is read-only, never "any".
    return _FF5_SPEC.get(_ff5_pat(pattern), {}).get("run", "readonly")

# --------------------------------------------------------------------------
# Proposal 1: intent is separate from permission. The verb "fix" never grants
# write access; the user's limits persist across turns until revoked.
# --------------------------------------------------------------------------

_FF5_GRANT_RE = re.compile(
    r"\b(?:go\s+ahead|apply\s+(?:it|the\s+(?:fix|changes?))|you\s+(?:may|can)\s+(?:edit|modify|change)"
    r"|feel\s+free\s+to\s+(?:edit|change|modify)|sua\s+luon|cu\s+sua|ap\s+dung(?:\s+luon)?|cho\s+phep\s+sua)\b")
_FF5_NORUN_RE = re.compile(
    r"\b(?:do\s+not|don'?t|dont|never)\s+(?:run|execute)\s+(?:any\s+)?(?:commands?|shell|scripts?|anything)\b"
    r"|\b(?:khong|dung)\s+(?:duoc\s+)?chay\s+(?:bat\s+ky\s+)?(?:lenh|command|script|shell)\b")


def _ff5_fold(text):
    raw = text or ""
    strip_fn = globals().get("_strip_pasted_content")
    if callable(strip_fn):
        stripped = strip_fn(raw)
        if stripped.strip():
            raw = stripped
    scannable = T._strip_code_spans(raw)
    return re.sub(r"\s+", " ", T._fold_diacritics(scannable)).lower()


class _Ff5Perm:
    def __init__(self, edit, run, reason, restricted, scope=None):
        self.edit, self.run = edit, run
        self.reason, self.restricted = reason, restricted
        # None = no restriction; otherwise the paths this request may write.
        self.scope = scope

    def __repr__(self):
        return "Perm(edit=%s, run=%s, %s)" % (self.edit, self.run, self.reason)


_FF6C_Q_LEAD = re.compile(
    r"^(?:(?:ok|okay|so|hey|hi|and|also|then|vay|the|nhung|ma|oi|hay|nho|ban|giup(?:\s+minh|\s+toi)?)\b[\s,]*)?"
    r"(?:how\s+(?:can|could|do|does|should|would|to)\b|should\s+(?:i|we)\b|"
    r"what(?:'s|\s+is)\s+the\s+(?:best\s+)?way\s+to\b|lam\s+sao\b|"
    r"lam\s+the\s+nao\b|cach\s+nao\b|huong\s+dan\b|chi\s+(?:minh\s+)?cach\b|"
    r"nen\s+(?:sua|lam)\b|"
    r"(?:dieu\s+tra|tim\s+hieu|phan\s+tich|khao\s+sat)\s+xem\b|"
    r"(?:kiem\s+tra\s+xem|xem(?:\s+thu)?)\b[^.!\n]*?\bco\s+the\b[^.!\n]*?\bkhong\b|"
    r"lieu\s+(?:co\s+the|source|code|file|ham|module)\b|"
    r"is\s+it\s+possible\s+to\b)")
_FF6C_IMPERATIVE = re.compile(
    r"^(?:(?:please|pls|hay|vui\s+long|then|and|also|va|roi)\b[\s,]*)*"
    r"(?:fix|implement|add|update|change|modify|refactor|create|write|make|"
    r"remove|delete|apply|sua|them|viet|tao|cap\s+nhat|thuc\s+hien|xoa|"
    r"ap\s+dung)\b")


class _Ff6cHowto:
    """Drop-in for the old regex object: only .search() is used."""

    def search(self, text):
        sents = [s.strip() for s in re.split(r"[.!?\n;]+", text or "") if s.strip()]
        if not sents or not _FF6C_Q_LEAD.match(sents[0]):
            return None
        if any(_FF6C_IMPERATIVE.match(s) for s in sents[1:]):
            return None
        return True


_FF5_HOWTO_RE = _Ff6cHowto()
_FF8_RUN_GRANT_RE = re.compile(
    r"\b(?:you\s+(?:may|can)\s+(?:now\s+)?(?:run|execute)"
    r"|feel\s+free\s+to\s+(?:run|execute)"
    r"|go\s+ahead\s+and\s+(?:run|execute)"
    r"|(?:run|execute)\s+(?:it|them|the\s+(?:unit\s+)?(?:tests?|commands?))\s+(?:now|again)"
    r"|cho\s+phep\s+chay|chay\s+luon|cu\s+chay)\b")


def _ff5_permissions(messages, pattern=None, fix=False):
    """Permissions in force for the newest request.

    Unified permissions logic combining independent grants, no-edit/no-run constraints,
    guidance-question detection, and pattern defaulting to TOOL_DEMAND.
    """
    pattern = pattern or "TOOL_DEMAND"
    texts = [T._stringify_content(m.get("content", "")) for m in messages or []
             if isinstance(m, dict) and m.get("role") == "user"]
    edit_ok = run_ok = True
    why = []
    independent = _flow_c("flow8_independent_grants", True)
    for t in texts:
        f = _ff5_fold(t)
        if not independent:
            if _FF5_GRANT_RE.search(f):
                edit_ok = run_ok = True
            if _FF5_NOEDIT_RE.search(f):
                edit_ok = False
            if _FF5_NORUN_RE.search(f):
                run_ok = False
        else:
            if _FF5_GRANT_RE.search(f):
                edit_ok = True
            if _FF8_RUN_GRANT_RE.search(f):
                run_ok = True
            if _FF5_NOEDIT_RE.search(f):
                edit_ok = False
            if _FF5_NORUN_RE.search(f):
                run_ok = False
    if not edit_ok:
        why.append("user forbade edits")
    if not run_ok:
        why.append("user forbade running commands")
    guidance = False
    if texts:
        f = _ff5_fold(texts[-1])
        guidance = bool(_FF5_HOWTO_RE.search(f)) and not _FF5_GRANT_RE.search(f)
        if guidance:
            why.append("guidance question")
    spec_edit = _ff5_spec_edit(pattern, fix)
    spec_run = _ff5_spec_run(pattern)
    can_edit = bool(spec_edit and edit_ok and not guidance)
    if not run_ok:
        run = "none"
    elif spec_run == "any" and not (guidance and pattern == "EXPLAIN"):
        run = "any"
    else:
        run = "readonly"
    restricted = (can_edit != spec_edit) or (run != spec_run)
    return _Ff5Perm(can_edit, run, "; ".join(why) or "default", restricted,
                    scope=None)




def _ff5_scope_s1(ctx, messages):
    """Files this request may write. ``None`` = undecided, do not tighten;
    ``[]`` = an empty but known scope, i.e. NO write is allowed (fail closed).
    Unified scope resolution handling unnamed requests, create targets, and read test files.
    """
    pattern = _ff5_pat(getattr(ctx, "pattern", None))
    if not pattern:
        try:
            named = list(_named_files(ctx)) if ctx is not None else []
        except Exception:
            named = []
        reads = [_call_path(e["args"]) for e in _answered_calls(messages)
                 if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])]
        return (named + reads) or None
    if pattern in ("REVERT", "TOOL_DEMAND"):
        return None
    named = [f for f in (_named_files(ctx) if ctx is not None else []) if f]
    if pattern == "CREATE":
        allowed = list(named)
        target = getattr(ctx, "target", "") or ""
        if target:
            allowed.append(target)
        return allowed          # [] = no write target yet: fail closed
    if pattern in ("CHANGE", "DEBUG"):
        if named:
            scope = list(named)
        else:
            reads = [_call_path(e["args"]) for e in _answered_calls(messages)
                     if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])]
            scope = reads or None
        if scope:
            extra = []
            for e in _answered_calls(messages):
                p = _call_path(e["args"]) if e["kind"] == "read" and _read_ok(e) else None
                if (p and _is_test_path(p) and not T._path_seen(p, list(scope))
                        and p not in extra):
                    extra.append(p)
            if extra:
                scope = list(scope) + extra
        return scope
    return []          # this pattern must not write at all










_FF5_RO_SEG_RE = re.compile(
    r"^\s*(?:git\s+(?:--no-pager\s+)?(?:status|diff|log|ls-files|show|branch|rev-parse|remote)\b|"
    r"ls\b|dir\b|cat\b|type\b|echo\b|find\b|grep\b|rg\b|head\b|tail\b|pwd\b|cd\b|wc\b|sort\b|"
    r"get-childitem\b|get-content\b|select-string\b|where\b|which\b|"
    r"python3?\s+-m\s+(?:py_compile|unittest|pytest)\b|pytest\b|npm\s+(?:run\s+)?test\b|"
    r"go\s+test\b|cargo\s+test\b)", re.I)


_FF5_TEST_SEG_RE = re.compile(
    r"^\s*(?:python3?\s+-m\s+(?:unittest|pytest)\b|pytest\b|npm\s+(?:run\s+)?test\b|node\s+\S*test\S*|"
    r"go\s+test\b|cargo\s+test\b|dotnet\s+test\b|mvn\s+test\b|gradle\s+test\b)", re.I)
_FF5_READFAIL_RE = re.compile(
    r"(?i)^\s*(?:error|cannot\s+(?:read|open|find)|no\s+such\s+file|file\s+not\s+found|"
    r"enoent|the\s+system\s+cannot|failed\s+to\s+read|does\s+not\s+exist|permission\s+denied)")
_FF5_NOTFOUND_RE = re.compile(
    r"(?i)no such file|not found|does not exist|enoent|cannot find|cannot open|can't find")
_FF5_TRUNC_RE = re.compile(r"(?i)(?:truncated|output\s+(?:was\s+)?cut|more\s+lines|\.\.\.\s*\[?\d+\s+(?:more|lines))")
_FF5_EXEC_FAIL_RE = re.compile(
    r"(?im)^(?:fatal:|error:|exception:|traceback \(most recent call last\)|"
    r"command not found|no such file)")


_FF8_SAFE_REDIR_RE = re.compile(
    r"\d?>&\d|\d?>>?\s*/dev/null(?![\w./-])|\d?>>?\s*nul(?![\w./-])", re.I)
_FF8_UNSAFE_RE = re.compile(
    r"[\r\n\x00\x0b\x0c\x85\u2028\u2029`]|\$\(|\$\{|<\(|>\(|(?<!&)&(?!&)")
_FF8_FIND_BAD_RE = re.compile(
    r"\s-(?:delete|exec|execdir|ok|okdir|fprint0?|fprintf|fls)\b")
_FF8_BRANCH_SAFE = {
    "-a", "-r", "-v", "-vv", "-l", "-i", "--list", "--all", "--remotes",
    "--verbose", "--show-current", "--contains", "--no-contains", "--merged",
    "--no-merged", "--points-at", "--sort", "--format", "--color", "--no-color",
    "--column", "--no-column", "--ignore-case"}
_FF8_BRANCH_LISTY = {"-l", "--list", "--contains", "--no-contains", "--merged",
                     "--no-merged", "--points-at", "--sort", "--format"}


def _ff8_git_safe(rest):
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        o = rest[i]
        if (o == "-c" or o.startswith(("--exec-path", "--config-env", "--git-dir",
                                       "--work-tree"))):
            return False
        i += 1
    if i >= len(rest):
        return True
    sub, args = rest[i], rest[i + 1:]
    if any(a.startswith(("--output", "--ext-diff", "--textconv")) for a in args):
        return False
    if sub == "branch":
        flags = [a.split("=", 1)[0] for a in args if a.startswith("-")]
        pos = [a for a in args if not a.startswith("-")]
        if any(f not in _FF8_BRANCH_SAFE for f in flags):
            return False
        if pos and not any(f in _FF8_BRANCH_LISTY for f in flags):
            return False
    elif sub == "remote":
        if args and not (all(a in ("-v", "--verbose") for a in args)
                         or args[0] in ("show", "get-url")):
            return False
    return True


def _ff8_seg_safe(seg):
    toks = seg.split()
    if not toks:
        return True
    cmd = toks[0].lower()
    rest = [t.lower() for t in toks[1:]]
    joined = " " + " ".join(rest)
    if cmd == "find" and _FF8_FIND_BAD_RE.search(joined):
        return False
    if cmd == "sort":
        for t in rest:
            if t.startswith("--output") or t.startswith("--compress-program"):
                return False
            if t.startswith("-") and not t.startswith("--") and "o" in t[1:]:
                return False
    if cmd in ("rg", "grep") and any(
            t.startswith(("--pre", "--hostname-bin")) for t in rest):
        return False
    if cmd == "git" and not _ff8_git_safe(rest):
        return False
    return True


def _ff5_cmd_readonly(cmd):
    try:
        configured = (_cfg("flow_test_command") or "").strip()
        if configured and (cmd or "").strip() == configured:
            return True
    except Exception:
        pass
    if not _flow_c("flow8_strict_shell", True):
        c = cmd or ""
        if "`" in c or "$(" in c:
            return False
        if (_FF6B_FIND_RE.search(c) or _FF6B_BRANCH_RE.search(c)
                or _FF6B_REMOTE_RE.search(c) or _FF6B_OUTPUT_RE.search(c)):
            return False
        c_clean = re.sub(r"\d?>&\d|\d?>\s*/dev/null|\d?>\s*nul\b", "", c, flags=re.I)
        if ">" in c_clean:
            return False
        segs = [s for s in re.split(r"&&|\|\||;|\|", c_clean) if s.strip()]
        if not segs:
            return False
        return all(_FF5_RO_SEG_RE.match(s) or _FF5_TEST_SEG_RE.match(s) for s in segs)
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    c = _FF8_SAFE_REDIR_RE.sub(" ", cmd)
    if _FF8_UNSAFE_RE.search(c) or ">" in c:
        return False
    segs = [s.strip() for s in re.split(r"&&|\|\||;|\|", c)]
    segs = [s for s in segs if s]
    if not segs:
        return False
    for s in segs:
        if not (_FF5_RO_SEG_RE.match(s) or _FF5_TEST_SEG_RE.match(s)):
            return False
        if not _ff8_seg_safe(s):
            return False
    return True


is_readonly_command = _ff5_cmd_readonly


def _ff5_status(e):
    """success | partial | failed | unknown -- only success/partial unlock the next step.

    ``unknown`` is evidence that exists but proves nothing (an empty read).
    Such a read is retried once by :func:`_ff5_evidence_gate`, then the proxy
    stops instead of letting the model guess.
    """
    res = (e.get("result") or "")
    k = e.get("kind")
    if k == "read":
        if not res.strip():
            return "unknown"
        if not _read_ok(e) or _FF5_READFAIL_RE.match(res):
            return "failed"
        trunc = globals().get("_FF4_TRUNC_RE") or _FF5_TRUNC_RE
        return "partial" if trunc.search(res[-400:]) else "success"
    if k == "act":
        if not res.strip():
            return "unknown"
        return "success" if _act_ok(e) else "failed"
    if k == "exec":
        if not res.strip():
            return "unknown"
        outcome = test_outcome(res)
        if outcome in ("fail", "broken"):
            return "failed"
        if outcome == "pass":
            return "success"
        if re.search(r"Exited with code [1-9]\b", res, re.I):
            return "failed"
        if _FF5_EXEC_FAIL_RE.search(res):
            return "failed"
        return "success"
    return "unknown"


def _ff4_deleted(ev):
    """Paths whose truncated read was followed by a 'not found' read.

    The file disappeared between the first (truncated) read and the
    continuation, so re-issuing the continuation would loop forever.
    """
    out = set()
    for i, path, _nxt in _ff4_truncations(ev):
        for e in ev[i + 1:]:
            if e["kind"] != "read" or not T._path_seen(
                    path, [_call_path(e["args"])]):
                continue
            res = e.get("result") or ""
            if (_ff5_status(e) == "failed" and _FF5_READFAIL_RE.match(res)
                    and _FF5_NOTFOUND_RE.search(res[:300])):
                out.add(path)
    return out


def _ff5_status_detail(e):
    """A short, specific reason for a failed/unknown call (used in nudges)."""
    res = " ".join((e.get("result") or "").split())
    path = _call_path(e.get("args") or {}) or "?"
    low = res.lower()
    if e.get("kind") == "read":
        if not res.strip():
            return f"{path}: the read tool returned no content"
        if _ff5_status(e) == "partial":
            return (f"{path}: the read was cut off and could not be continued "
                    "to the end")
        return f"{path}: read failed -- {res[:180]}"
    if e.get("kind") == "act":
        if not res.strip():
            return f"{path}: the edit tool returned no result, so the write is unverified"
        if "oldstring" in low or "old_string" in low or "old text" in low:
            return (f"{path}: oldString does not match the file you read -- copy the "
                    f"exact text, without line-number prefixes ({res[:160]})")
        if re.search(r"not found|no such file|does not exist", low):
            return f"{path}: wrong path or missing file -- {res[:160]}"
        if re.search(r"missing|required|parameter|argument", low):
            return f"{path}: a required parameter is missing -- {res[:160]}"
        return f"{path}: edit failed -- {res[:180]}"
    return f"{path}: command failed -- {res[:180]}"


# --------------------------------------------------------------------------
# Proposal 3: ToolMap adapter -- proxy-side parameter normalisation
# --------------------------------------------------------------------------

def _ff5_adapt_calls(calls, tm):
    """Normalise every call to the client's schema; return (calls, problems)."""
    out, problems = [], []
    for tc in calls or []:
        try:
            fixed, probs = tm.adapt(tc)
        except Exception as e:
            fixed, probs = tc, [f"could not adapt the tool call: {e}"]
        out.append(fixed)
        problems.extend(probs)
    return out, problems


# --------------------------------------------------------------------------
# Proposal 5: universal evidence gate. A step only advances on success or a
# completed continuation; failed/unknown reads never unlock an edit, and a
# truncated read is continued by the proxy before anything else happens.
# --------------------------------------------------------------------------



def _ff5_same_path(a, b):
    return bool(a and b and T._path_seen(a, [b]))


def _ff5_retry_call(e):
    fn = e.get("name") or "read"
    args = dict(e.get("args") or {})
    return {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
            "function": {"name": fn,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _ff5_local_step(ctx, text):
    """A planner step that must be returned verbatim, without another model call."""
    st = Step(getattr(ctx, "pattern", None) or "ANSWER", "answer", 1, 1, (), text)
    st.local_text = text
    st.skipped = []
    return st


def _ff5_evidence_gate_s1(ctx, messages, tm):
    """``(step, stop_text)``. A read/continuation step wins; ``stop_text`` is a
    deterministic refusal that must not fall through to an edit or an answer."""
    if ctx is None or not getattr(ctx, "pattern", None):
        return None, None
    ev = _answered_calls(messages)
    reads = [e for e in ev if e["kind"] == "read"]
    if not reads:
        return None, None
    pattern = ctx.pattern
    perm = getattr(ctx, "perm", None)
    edit_expected = bool(getattr(perm, "edit", False)
                         and _ff5_spec_edit(pattern, getattr(ctx, "fix", False)))

    # 1. truncated output: continue at the offset the tool asked for, before
    #    editing or answering. For a pattern that may edit this is mandatory.
    okey = _ff4_offset_key(tm) if "_ff4_offset_key" in globals() else None
    conts = _ff4_continuations(ev, okey) if okey else []
    if conts:
        names = ", ".join(sorted({p for p, _c in conts}))
        return Step(pattern, "read", 1, 1, {"read"},
                    "A previous read was cut off (output truncated). Read the "
                    "rest before doing anything else: " + names + ".",
                    [c for _p, c in conts], True, tm.names({"read"})), None

    # 2. "partial" reads that no later successful read has completed. A
    #    truncation that was continued to the end is finished and must not
    #    block the edit; a partial read the proxy cannot track (no path, or an
    #    unknown cut marker) still fails closed.
    finished = {i for i, _p, _n in _ff4_truncations(ev)}
    finished -= {i for i, _p, _n in _ff4_unfinished(ev)}
    partial = [e for i, e in enumerate(ev)
               if e["kind"] == "read"
               and _ff5_status(e) == "partial"
               and i not in finished]
    if partial and edit_expected:
        deleted = _ff4_deleted(ev)
        deleted_reads = []
        for e in partial:
            p = _call_path(e.get("args") or {})
            if not p or not any(T._path_seen(p, [d]) for d in deleted):
                continue
            later = [x for x in ev
                     if x["kind"] == "read"
                     and T._path_seen(p, [_call_path(x["args"])])
                     and _ff5_status(x) == "failed"
                     and _FF5_READFAIL_RE.match(x.get("result") or "")
                     and _FF5_NOTFOUND_RE.search((x.get("result") or "")[:300])]
            if later:
                deleted_reads.append(later[-1])
        if deleted_reads:
            detail = "; ".join(_ff5_status_detail(e) for e in deleted_reads[:3])
            if _ff5_vi(ctx):
                return None, ("File đã bị xóa trong lúc đọc nên mình KHÔNG sửa gì ("
                              + detail + "). Hãy kiểm tra lại đường dẫn hoặc mở lại "
                              "file rồi yêu cầu lại.")
            return None, ("The file was deleted while it was being read, so I made "
                          "NO edit (" + detail + "). Check the path or reopen the "
                          "file and ask again.")
        calls = _ff5_enlarge_read_calls(partial, ev, tm)
        if calls:
            paths = []
            for c in calls:
                try:
                    args = json.loads(c["function"]["arguments"])
                except (KeyError, TypeError, json.JSONDecodeError):
                    args = {}
                p = _call_path(args)
                if p:
                    paths.append(p)
            names = ", ".join(sorted(set(paths)))
            return Step(pattern, "read", 1, 1, {"read"},
                        "A previous read was cut off. Re-read it with a larger "
                        "limit before doing anything else: " + names + ".",
                        calls, True, tm.names({"read"})), None
        max_cont = int(CONFIG.get("flow4_max_continuations", _FF4_MAX_CONT) or _FF4_MAX_CONT)
        if okey and all(
            sum(1 for x in ev
                if x["kind"] == "read" and _read_ok(x)
                and T._path_seen(_call_path(e.get("args") or {}), [_call_path(x["args"])])) > max_cont
            for e in partial
        ):
            partial = []
    if partial and edit_expected:
        detail = "; ".join(_ff5_status_detail(e) for e in partial[:3])
        if _ff5_vi(ctx):
            return None, ("Một lần đọc đã bị cắt và công cụ đọc không hỗ trợ "
                          "offset lẫn limit nên mình KHÔNG sửa gì (" + detail +
                          "). Hãy đọc lại file với giới hạn lớn hơn rồi yêu cầu lại.")
        return None, ("A read was truncated and the read tool exposes no offset "
                      "or limit parameter, so I made NO edit (" + detail + "). "
                      "Re-read the file with a larger limit and ask again.")

    # 3. unknown (empty) read: retry once, then stop rather than guess.
    pending_unknown, exhausted = [], []
    for i, e in enumerate(reads):
        if _ff5_status(e) != "unknown":
            continue
        path = _call_path(e["args"])
        same = [x for x in reads if _ff5_same_path(path, _call_path(x["args"]))]
        if any(_ff5_status(x) in ("success", "partial") for x in same):
            continue
        # A path has one retry. Two (or more) empty reads mean the retry also
        # returned nothing: stop instead of looping forever.
        if sum(1 for x in same if _ff5_status(x) == "unknown") >= 2:
            if not any(_ff5_same_path(path, _call_path(x["args"]))
                       for x in exhausted):
                exhausted.append(e)
        elif not any(_ff5_same_path(path, _call_path(x["args"]))
                     for x in pending_unknown):
            pending_unknown.append(e)
    if pending_unknown:
        detail = "; ".join(_ff5_status_detail(e) for e in pending_unknown[:3])
        return Step(pattern, "read", 1, 1, {"read"},
                    "The read tool returned no content. Read the file again "
                    "before continuing (" + detail + ").",
                    [_ff5_retry_call(e) for e in pending_unknown], True,
                    tm.names({"read"})), None
    if exhausted and not any(_ff5_status(e) in ("success", "partial") for e in reads):
        detail = "; ".join(_ff5_status_detail(e) for e in exhausted[:3])
        if _ff5_vi(ctx):
            return None, ("Công cụ đọc trả về nội dung rỗng nhiều lần nên mình dừng "
                          "thay vì đoán (" + detail + "). Hãy kiểm tra đường dẫn.")
        return None, ("The read tool returned no content repeatedly, so I stopped "
                      "instead of guessing (" + detail + "). Check the path.")

    # 4. failed reads never unlock an edit; if nothing was read successfully,
    #    they do not unlock an answer either.
    failed = [e for e in reads if _ff5_status(e) == "failed"]
    good = [e for e in reads if _ff5_status(e) in ("success", "partial")]
    if edit_expected and failed:
        guard = _ff5_read_guard(ctx, messages)
        if guard:
            return None, guard
    if failed and not good and pattern not in ("CREATE", "TOOL_DEMAND"):
        detail = "; ".join(_ff5_status_detail(e) for e in failed[:3])
        if _ff5_vi(ctx):
            return None, ("Không đọc được file cần thiết nên mình không thể trả lời "
                          "dựa trên nội dung chưa thấy (" + detail + "). Hãy kiểm "
                          "tra đường dẫn rồi yêu cầu lại.")
        return None, ("I could not read the required file, so I will not answer "
                      "from unseen content (" + detail + "). Check the path and "
                      "ask again.")
    return None, None


# --------------------------------------------------------------------------
# Proposal 2: shared stages. The rich discover/read builders above stay the
# primitives; this table names the stage each step belongs to and inserts the
# missing "read the diff" stage before verify/answer.
# --------------------------------------------------------------------------

_FF5_STAGE_NAME = {
    "survey": "discover", "search": "discover", "read": "read",
    "act": "edit", "merge": "edit", "diff": "diff", "git": "diff",
    "tests": "verify", "verify": "verify", "answer": "answer",
}


def _ff5_stage(step):
    return _FF5_STAGE_NAME.get(getattr(step, "name", ""), "analyze")


def _ff5_diff_step(ctx, step, messages, tm):
    if not _c5("flow6_diff_step") or step is None:
        return None
    pattern = getattr(ctx, "pattern", None)
    if pattern not in ("CHANGE", "DEBUG") or (pattern == "DEBUG" and not ctx.fix):
        return None
    if step.name not in ("answer", "tests", "verify"):
        return None
    if not tm.first("exec"):
        return None
    ev = _answered_calls(messages)
    acts = [e for e in ev if e["kind"] == "act" and _act_ok(e)
            and _call_path(e["args"])]
    if not acts:
        return None
    last = max(i for i, e in enumerate(ev) if e["kind"] == "act")
    if any(e["kind"] == "exec"
           and re.search(r"\bgit\b[^\n]*\bdiff\b", _call_command(e["args"]))
           for e in ev[last + 1:]):
        return None
    paths = sorted({_call_path(e["args"]) for e in acts
                    if _base(_call_path(e["args"])) != _MERGE_SCRIPT})
    if not paths:
        return None
    quoted = " ".join('"%s"' % p.replace('"', "") for p in paths[:12])
    call = tm.make_call("exec", command="git --no-pager diff -- " + quoted,
                        description="Read the diff of the change")
    if not call:
        return None
    return Step(pattern, "diff", getattr(step, "index", 1),
                getattr(step, "total", 1), {"exec"},
                "Read the diff now. Check that only the intended lines changed; "
                "if the change is wrong, fix it with one edit call, otherwise "
                "continue to verification.", [call], True, tm.names({"exec"}))


def _ff5_context_suffix(ctx, step, perm, messages):
    """The smallest context a weak model needs for this one step (proposal 4)."""
    spec = _FF5_SPEC.get(getattr(ctx, "pattern", None) or "", {})
    goal = " ".join((getattr(ctx, "last", "") or "").split())[:180]
    ev = _answered_calls(messages)
    good = sorted({_call_path(e["args"]) for e in ev
                   if e["kind"] == "read" and _ff5_status(e) in ("success", "partial")
                   and _call_path(e["args"])})
    bad = sorted({_call_path(e["args"]) for e in ev
                  if e["kind"] == "read" and _ff5_status(e) == "failed"
                  and _call_path(e["args"])})
    bits = ["goal=" + (goal or "(none)"),
            "intent=" + (getattr(ctx, "pattern", None) or "?"),
            "edit=" + ("allowed" if getattr(perm, "edit", False) else "forbidden"),
            "commands=" + str(getattr(perm, "run", "readonly")),
            "next=" + getattr(step, "stage", _ff5_stage(step))]
    if good:
        bits.append("read=" + ", ".join(good[:6]))
    if bad:
        bits.append("failed_reads=" + ", ".join(bad[:4]))
    if _FF5_SPEC.get(ctx.pattern or "", {}).get("steps"):
        bits.append("pipeline=" + " -> ".join(_ff5_allowed_steps(
            ctx.pattern, getattr(ctx, "fix", False))))
    text = "[Context] " + " | ".join(bits)
    return text[:900]


# --------------------------------------------------------------------------
# Finding 15: a "just enough" prompt for weak models. For guided steps the
# full chat history is replaced by goal + permissions + stage-selected
# evidence + the step's own directive (with the allowed tools' schemas).
# Everything is fail-open: any problem -> None -> the caller keeps using the
# original full prompt.
# --------------------------------------------------------------------------

_FF5_LEAN_CAPS = {"discover": 4, "read": 6, "edit": 4, "diff": 900,
                  "verify": 1200, "answer": 6, "analyze": 6}


def _ff5_lean_line(text, cap):
    """One flattened evidence line, hard-capped with an explicit marker."""
    t = " ".join(str(text or "").split())
    if len(t) <= cap:
        return t
    return t[:cap].rstrip() + " …[truncated]"


def _ff5_exec_findings(ev):
    """Exec results after the last successful edit: verified one-line findings."""
    try:
        last_act = max(i for i, e in enumerate(ev) if e["kind"] == "act") + 1
    except ValueError:
        last_act = 0
    out = []
    for e in ev[last_act:]:
        if e.get("kind") != "exec" or _ff5_status(e) != "success":
            continue
        res = e.get("result") or ""
        lines = [ln.strip()[:140] for ln in res.splitlines()
                 if ln.strip() and not re.match(r"^[\s|+\-=_`~#*]*$", ln.strip())]
        for ln in lines[:2]:
            item = _ff5_lean_line(ln, 140)
            if item and item not in out:
                out.append(item)
        if len(out) >= 3:
            break
    return out[:3]


def select_evidence_for_step(stage, messages, *, budget=2400):
    """Trả về list[str]: các mẩu evidence đã cắt gọn, ưu tiên theo độ mới."""
    out = []
    try:
        ev = _answered_calls(messages)
        seen_paths, seen_bad = set(), set()

        def add(line):
            if line and line not in out and sum(map(len, out)) + len(line) <= budget:
                out.append(line)

        def path_of(e):
            return _call_path(e.get("args") or {})

        def hits(result):
            body = (result or "").replace("\\", "/")
            fs = sorted(T._files_in(body), key=body.find)
            return [f for f in fs
                    if f.lower() not in T._NON_FILE_NAMES][:5]

        if stage == "discover":
            n = 0
            for e in reversed(ev):
                if e["kind"] not in ("survey", "search", "exec"):
                    continue
                cmd = " ".join(_call_command(e["args"]).split())[:120]
                n += 1
                add(f"search: [{e['kind']}] {cmd} -> "
                    f"{len(hits(e['result']))} hit(s)")
                if n >= _FF5_LEAN_CAPS["discover"]:
                    break
        elif stage == "read":
            for e in reversed(ev):
                p = path_of(e)
                if e["kind"] == "read" and p:
                    st = _ff5_status(e)
                    low = p.lower()
                    if st in ("success", "partial") and low not in seen_paths:
                        seen_paths.add(low)
                        add(f"read: {p}")
                    elif st == "failed" and low not in seen_bad:
                        seen_bad.add(low)
                        add(f"failed_read: {p} (do not read it again)")
                elif e["kind"] in ("survey", "search"):
                    for f in hits(e["result"]):
                        if len(seen_paths) + len(out) >= _FF5_LEAN_CAPS["read"]:
                            break
                        add(f"unread candidate: {f}")
                if sum(map(len, out)) > budget * 0.7:
                    break
        elif stage == "edit":
            for e in reversed(ev):
                p = path_of(e)
                if e["kind"] == "act" and _ff5_status(e) == "failed":
                    add("edit_failed: "
                        + _ff5_lean_line(_ff5_status_detail(e), 220))
                elif e["kind"] == "read" and p:
                    st = _ff5_status(e)
                    low = p.lower()
                    if st in ("success", "partial") and low not in seen_paths:
                        seen_paths.add(low)
                        add(f"read: {p}")
                    elif st == "failed" and low not in seen_bad:
                        seen_bad.add(low)
                        add(f"failed_read: {p} (do not read it again)")
                elif e["kind"] == "act" and p and p.lower() not in seen_paths:
                    seen_paths.add(p.lower())
                    add(f"target: {p}")
                if len(out) >= _FF5_LEAN_CAPS["edit"]:
                    break
        elif stage == "diff":
            for e in reversed(ev):
                if e["kind"] != "exec":
                    continue
                if not re.search(r"\bgit\b[^\n]*\bdiff\b", _call_command(e["args"])):
                    continue
                if _ff5_status(e) != "success":
                    continue
                add(f"diff: {_ff5_lean_line(e['result'], _FF5_LEAN_CAPS['diff'])}")
                break
        else:  # verify / answer / analyze share the tail rules
            if stage == "verify":
                for e in reversed(ev):
                    if e["kind"] != "exec":
                        continue
                    if not (_is_test_cmd(_call_command(e["args"]))
                            or test_outcome(e["result"]) in ("pass", "fail")):
                        continue
                    head = "test_failed: " if _ff5_status(e) == "failed" else "test: "
                    add(head + _ff5_lean_line(e["result"],
                                              _FF5_LEAN_CAPS["verify"]))
                    break
            good = sorted({path_of(e) for e in ev
                           if e["kind"] == "read"
                           and _ff5_status(e) in ("success", "partial")
                           and path_of(e)})
            bad = sorted({path_of(e) for e in ev
                          if e["kind"] == "read" and _ff5_status(e) == "failed"
                          and path_of(e)})
            for p in good[:_FF5_LEAN_CAPS[stage if stage in _FF5_LEAN_CAPS
                                          else "answer"]]:
                add(f"read: {p}")
            for p in bad[:4]:
                add(f"failed_read: {p} (do not read it again)")
            if stage == "analyze":
                n = 0
                for e in reversed(ev):
                    if e["kind"] not in ("survey", "search"):
                        continue
                    n += 1
                    add(f"search: {_ff5_lean_line(_call_command(e['args']), 100)}"
                        f" -> {', '.join(hits(e['result'])[:3]) or 'no path'}")
                    if n >= 2:
                        break
            for f in _ff5_exec_findings(ev):
                add(f"verified: {f}")
    except Exception as e:
        # Propagate: build_step_prompt turns ANY failure into "use the full
        # prompt". Swallowing here would ship a lean prompt whose evidence is
        # silently incomplete.
        log(f"[fix6] lean evidence error (stage={stage}): {e}")
        raise
    return out


def _ff5_allowed_tool_defs(step, tm):
    """JSON schema blocks for step.tool_names ∩ tm -- the same ToolMap the
    planner used, so allowlist and schema can never disagree."""
    props = getattr(tm, "schemas", None) or {}
    req = getattr(tm, "required", None) or {}
    blocks = []
    for name in list(getattr(step, "tool_names", ()) or []):
        if name not in props:
            continue
        payload = {"name": name,
                   "parameters": sorted(props.get(name) or ())}
        r = sorted(req.get(name) or ())
        if r:
            payload["required"] = r
        blocks.append("```json\n"
                      + json.dumps(payload, ensure_ascii=False) + "\n```")
    return blocks


def _ff5_step_driven(step):
    """True when the proxy emits this step's call itself (no upstream call).

    A step that carries a ready tool_call is only "driven" while flow_drive is
    on; with it off the same step is guided, i.e. it DOES go upstream and so
    it is exactly the kind of step the lean prompt is for.
    """
    return bool(getattr(step, "drive", False) and _cfg("flow_drive"))


def _build_step_prompt_core_s1(ctx, step, messages, tm):
    """Dựng lean step prompt; trả None nếu step nằm ngoài phạm vi lean path."""
    if step is None or tm is None:
        return None
    if _ff5_step_driven(step) or getattr(step, "local_text", None) is not None:
        return None
    pattern = _ff5_pat(getattr(ctx, "pattern", None))
    if pattern not in _FF5_SPEC:
        return None
    perm = getattr(ctx, "perm", None)
    if perm is None:
        perm = _ff5_permissions(messages, pattern, getattr(ctx, "fix", False))
        if getattr(perm, "scope", "__missing__") == "__missing__":
            try:
                perm.scope = _ff5_scope(ctx, messages)
            except Exception:
                perm.scope = None
    stage = getattr(step, "stage", None) or _ff5_stage(step)
    goal = _ff5_lean_line(getattr(ctx, "last", "") or "", 180) or "(none)"
    scope = getattr(perm, "scope", None)
    scope_txt = ", ".join(str(s) for s in scope[:8]) if scope \
        else ("(none -- no writes allowed)" if scope == [] else "(unrestricted)")
    lines = ["Goal: " + goal,
             "Permissions: edit=" + ("allowed" if getattr(perm, "edit", False)
                                     else "forbidden")
             + " | commands=" + str(getattr(perm, "run", "readonly"))
             + " | scope=" + scope_txt]
    steps = _ff5_allowed_steps(pattern, getattr(ctx, "fix", False))
    if steps:
        lines.append("Pipeline: " + " -> ".join(steps))
    evidence = select_evidence_for_step(stage, messages)
    if evidence:
        lines.append("Evidence:")
        lines.extend(evidence)
    defs = _ff5_allowed_tool_defs(step, tm)
    if defs:
        lines.append("Allowed tool schemas:")
        lines.extend(defs)
    lines.append("")          # blank line before Step.directive()
    return "\n".join(lines)


def build_step_prompt(ctx, step, messages, tm):
    """Chuẩn hoá inputs rồi gọi _build_step_prompt_core; MỌI exception -> None.

    None means "not applicable": the caller must fall back to the full prompt.
    """
    try:
        if not _c5("flow6_lean_prompt"):
            return None
        if ctx is None or not getattr(ctx, "pattern", None):
            return None
        return _build_step_prompt_core(ctx, step, messages, tm)
    except Exception as e:
        log(f"[fix6] lean step prompt error, using the full prompt: {e}")
        return None


def _ff5_after_plan_s1(step, messages, tm, ctx=None):
    """Final transition guard shared by every pattern (proposal 5 + 2 + 6)."""
    if ctx is None:
        try:
            ctx = _analyze(messages)
        except Exception:
            ctx = None
    if not (ctx is not None and getattr(ctx, "pattern", None)):
        return step
    perm = getattr(ctx, "perm", None)
    if perm is None:
        try:
            perm = _ff5_permissions(messages, ctx.pattern, getattr(ctx, "fix", False))
            perm.scope = _ff5_scope(ctx, messages)
        except Exception:
            perm = None
    if _c5("flow6_universal_guard"):
        gate, stop = _ff5_evidence_gate(ctx, messages, tm)
        if stop is not None:
            log("[fix6] evidence gate stopped the transition: " + stop[:160])
            return _ff5_local_step(ctx, stop)
        if gate is not None:
            step = gate
    if step is not None and _c5("flow6_diff_step"):
        diff = _ff5_diff_step(ctx, step, messages, tm)
        if diff is not None:
            step = diff
    if step is not None:
        step.stage = _ff5_stage(step)
        # Validator generated from the same SPEC as the planner/banner.
        if step.name in ("act", "merge") and perm is not None and not perm.edit:
            msg = ("Bạn không cho phép sửa file nên mình không thực hiện chỉnh sửa nào."
                   if _ff5_vi(ctx) else
                   "Edits are not allowed here, so I made no edit.")
            return _ff5_local_step(ctx, msg)
        if step.name in ("tests", "verify", "diff", "git") and perm is not None \
                and perm.run == "none":
            msg = ("Bạn không cho phép chạy lệnh nên mình không chạy bước này."
                   if _ff5_vi(ctx) else
                   "Running commands is not allowed here, so this step was skipped.")
            return _ff5_local_step(ctx, msg)
        # Finding 15: when the lean prompt is on, build_step_prompt already
        # carries goal/permissions/evidence -- injecting the [Context] suffix
        # here would double it up inside step.directive().
        if _c5("flow6_context") and not getattr(step, "drive", False) \
                and not _c5("flow6_lean_prompt"):
            suffix = _ff5_context_suffix(ctx, step, perm, messages)
            if suffix and suffix not in (step.instruction or ""):
                step.instruction = (step.instruction or "") + "\n" + suffix
    return step


def _ff5_plan_after(step, messages, tm, ctx=None):
    try:
        return _ff5_after_plan(step, messages, tm, ctx)
    except Exception as e:
        log(f"[fix6] transition guard error: {e}")
        return step


def plan_for_s3(messages, tm, ctx=None):
    """Final planner: every pattern passes the universal transition guard."""
    step = plan_for_s2(messages, tm, ctx)
    return _ff5_plan_after(step, messages, tm, ctx)


# --------------------------------------------------------------------------
# Proposal 4 / metrics: tiny counters so the real-model measurement (flash-lite)
# has a place to land without changing behaviour when disabled.
# --------------------------------------------------------------------------

_FF5_METRICS = {}


def _ff5_metric(key, n=1):
    if not _c5("flow6_metrics"):
        return
    _FF5_METRICS[key] = _FF5_METRICS.get(key, 0) + n


# --------------------------------------------------------------------------
# Cross-module pass (supplement): check the files read this turn against each
# other. This validates structure only (names, attributes, arity); the quote
# verifier likewise confirms the *position* of a quote, never the conclusion
# drawn from it.
# --------------------------------------------------------------------------

_FF5_CROSS_NOTE = (
    "Static cross-module checks confirm names/arity only; they do not confirm "
    "that a finding or conclusion is correct.")


def _ff5_trunc_re():
    return globals().get("_FF4_TRUNC_RE") or _FF5_TRUNC_RE




def _ff5_py_modules_s1(messages):
    """{stem: [module]} for every Python file read successfully this turn."""
    mods = {}
    for e in _answered_calls(messages):
        if e["kind"] != "read" or not _read_ok(e):
            continue
        path = _call_path(e["args"])
        if not path or not path.lower().endswith(".py"):
            continue
        text = _ff5_trunc_re().sub("", e["result"] or "").rstrip()
        try:
            tree = _ff5_ast.parse(_clean_file_text(text))
        except (SyntaxError, ValueError):
            continue
        stem = _base(path)[:-3]
        mods.setdefault(stem, []).append({
            "path": path, "tree": tree, "defs": _ff5_py_defs(tree)})
    return mods


def _ff5_py_target(mods, dotted):
    stem = (dotted or "").split(".")[-1].lower()
    cands = mods.get(stem) or []
    if not cands:
        return None
    want = (dotted or "").replace(".", "/").lower()
    for m in cands:
        p = m["path"].replace("\\", "/").lower()
        if p.endswith("/" + want + ".py") or p == want + ".py":
            return m
    return cands[0]




def _ff5_cross_module(messages):
    """Static findings between the Python files read in this turn."""
    if not _c5("flow6_cross_module"):
        return []
    mods = _ff5_py_modules(messages)
    if len(mods) < 2 and not any(len(v) > 1 for v in mods.values()):
        return []
    out, seen = [], set()

    def add(text):
        if text not in seen:
            seen.add(text)
            out.append(text)

    for items in mods.values():
        for m in items:
            path = m["path"]
            defs = m["defs"]
            imported, imported_mods = {}, {}
            for node in _ff5_ast.walk(m["tree"]):
                if isinstance(node, _ff5_ast.ImportFrom):
                    if not node.module:
                        continue
                    target = _ff5_py_target(mods, node.module)
                    if target is None:
                        continue
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        if alias.name not in target["defs"]:
                            add("%s:%s: imports %r from %r, but %s does not "
                                "define it" % (path, getattr(node, "lineno", "?"),
                                               alias.name, node.module,
                                               target["path"]))
                            continue
                        imported[alias.asname or alias.name] = (
                            target, alias.name)
                elif isinstance(node, _ff5_ast.Import):
                    for alias in node.names:
                        base = alias.name.split(".")[0]
                        target = _ff5_py_target(mods, alias.name) \
                            or _ff5_py_target(mods, base)
                        if target is not None:
                            imported_mods[alias.asname or base] = target

            for node in _ff5_ast.walk(m["tree"]):
                if not isinstance(node, _ff5_ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, _ff5_ast.Name) and fn.id in imported:
                    target, name = imported[fn.id]
                    info = target["defs"].get(name)
                    if info:
                        problem = _ff5_arg_problem(node, info)
                        if problem:
                            add("%s:%s: %s() %s (defined in %s)" % (
                                path, getattr(node, "lineno", "?"), name,
                                problem, target["path"]))
                elif (isinstance(fn, _ff5_ast.Attribute)
                      and isinstance(fn.value, _ff5_ast.Name)
                      and fn.value.id in imported_mods):
                    target = imported_mods[fn.value.id]
                    info = target["defs"].get(fn.attr)
                    if info is None:
                        add("%s:%s: %s.%s is not defined in %s" % (
                            path, getattr(node, "lineno", "?"), fn.value.id,
                            fn.attr, target["path"]))
                    else:
                        problem = _ff5_arg_problem(node, info)
                        if problem:
                            add("%s:%s: %s.%s() %s (defined in %s)" % (
                                path, getattr(node, "lineno", "?"),
                                fn.value.id, fn.attr, problem, target["path"]))
    return out


# --------------------------------------------------------------------------
# Own-pattern results (REVERT / RUN_TESTS / RUN_COMMAND / GIT_INFO): adapt the
# proxy-generated call to the client schema and enforce the same permissions.
# --------------------------------------------------------------------------

def _map_reduce(messages, step, ctx, gen, client_gone=None):
    """Final map-reduce: append the cross-module pass to every review report."""
    report = _map_reduce_s2(messages, step, ctx, gen, client_gone)
    if not report or not _c5("flow6_cross_module"):
        return report
    try:
        findings = _ff5_cross_module(messages)
        if findings:
            report += ("\n\n**Cross-module checks (static, top-level only):**\n"
                       + "\n".join("- " + f for f in findings[:20]))
        report += "\n\n_" + _FF5_CROSS_NOTE + "_"
    except Exception as e:
        log(f"[fix6] cross-module pass error: {e}")
    return report


def _ff5_own_result_s1(step, ctx, messages, tm, perm):
    if step is None:
        return None
    if step.text is not None:
        return step.text, None
    calls = list(step.calls or [])
    if not calls:
        return None
    calls, probs = _ff5_adapt_calls(calls, tm)
    if probs:
        text = ("Không tạo được lời gọi công cụ hợp lệ: " + "; ".join(probs[:3])
                if _ff5_vi(ctx) else
                "Could not build a valid tool call: " + "; ".join(probs[:3]))
        return text, None
    if perm is not None:
        kinds = [tool_kind((tc.get("function") or {}).get("name")) for tc in calls]
        if not perm.edit and "act" in kinds:
            return _ff5_say(
                ctx, "Bạn không cho phép sửa file nên mình không thực hiện chỉnh sửa nào.",
                "Edits are not allowed here, so I made no edit.").text, None
        if perm.run == "none" and "exec" in kinds:
            return _ff5_say(
                ctx, "Bạn không cho phép chạy lệnh nên mình không chạy bước này.",
                "Running commands is not allowed here, so this step was skipped."
            ).text, None
        if perm.run == "readonly" and "exec" in kinds:
            for tc in calls:
                fn = tc.get("function") or {}
                if tool_kind(fn.get("name")) != "exec":
                    continue
                cmd = _call_command(T._tool_arguments_dict(fn))
                if not _ff5_cmd_readonly(cmd):
                    return _ff5_say(
                        ctx, "Yêu cầu này chỉ cho phép lệnh chỉ-đọc nên mình không chạy `%s`."
                             % cmd[:160],
                        "Only read-only commands are allowed here, so I did not run `%s`."
                        % cmd[:160]).text, None
        scope = _ff5_scope_problems(calls, messages, perm)
        if scope:
            body = "\n".join("- " + x for x in scope[:5])
            return _ff5_say(
                ctx, "Mình không ghi ra ngoài phạm vi được phép:\n" + body,
                "I did not write outside the allowed scope:\n" + body).text, None
    return "", calls


def _ff5_install_banner():
    """Banner, planner and validator now share the same SPEC (proposal 6)."""
    if not _c5("flow6_banner"):
        return
    if getattr(T, "_TASK_BANNER_HOOK", None) is not None:
        return
    T._TASK_BANNER_HOOK = _ff5_spec_banner



# --------------------------------------------------------------------------
# Proposal 6: one banner generated from the same SPEC as the planner.
# --------------------------------------------------------------------------

def _ff5_spec_banner_s1(messages):
    try:
        # Banner, planner and validator share ONE source of truth: the full
        # analysis (sticky context, permissions, create/merge, debug-fix),
        # not a bare detect_task_pattern + ad-hoc _Ctx fallback.
        ctx = _analyze(messages)
        if ctx is None or not getattr(ctx, "pattern", None):
            return _task_pattern_banner_orig(messages)
        pattern = _ff5_pat(ctx.pattern)
        fix = bool(getattr(ctx, "fix", False))
        perm = getattr(ctx, "perm", None) or \
            _ff5_permissions(messages, pattern, fix)
        if pattern not in _FF5_SPEC:
            return _task_pattern_banner_orig(messages)
        # Same downgrade the planner performs: a CHANGE/CREATE request without
        # write permission is guidance only, so the banner must not announce an
        # edit stage the validator will reject.
        if pattern in ("CHANGE", "CREATE") and not perm.edit:
            pattern = "EXPLAIN"
            fix = False
            perm = _ff5_permissions(messages, pattern, fix)
        steps = _ff5_allowed_steps(pattern, fix)
        if not perm.edit:
            steps = [s for s in steps if s not in ("edit", "diff")]
        if perm.run == "none":
            steps = [s for s in steps if s != "verify"]
        limits = []
        if not perm.edit:
            limits.append("no file edits")
        if perm.run == "none":
            limits.append("no commands")
        elif perm.run == "readonly":
            limits.append("read-only commands")
        tail = (" Limits: " + ", ".join(limits) + ".") if limits else ""
        return ("[Task pattern: %s. Mandatory flow: %s.%s]"
                % (pattern, " -> ".join(steps), tail))
    except Exception as e:
        log(f"[fix6] banner error, using the previous banner: {e}")
        return _task_pattern_banner_orig(messages)


_FF5_REVERT_RE = re.compile(
    r"\b(?:revert|undo|rollback|roll\s+back|hoan\s+tac|khoi\s+phuc|huy|quay\s+lai|tra\s+lai)\b"
    r"[^.\n]{0,80}?\b(?:thay\s+doi|changes?|session|modifications?|edits?|sua\s+doi|tat\s+ca|everything)\b")
_FF5_RUNV_RE = re.compile(r"\b(?:run|execute|exec|chay|thuc\s+thi)\b")
_FF5_SHOWV_RE = re.compile(r"\b(?:show|xem|cho|list|liet\s+ke|display)\b")
_FF5_TESTS_RE = re.compile(
    r"\b(?:run|execute|chay|thuc\s+thi)\b[^.\n]{0,25}?"
    r"\b(?:unit\s*tests?|tests?|test\s+suite|pytest|unittest)\b")
_FF5_GIT_RE = re.compile(
    r"\bgit\s+(?:--no-pager\s+)?(status|log|diff|branch|show)\b([^\n`\"';&|<>]*)", re.I)
_FF5_CMD_START_RE = re.compile(
    r"^(?:git|npm|npx|pip|pip3|python|python3|node|make|cargo|go|docker|ls|dir|cat|"
    r"pytest|uv|yarn|pnpm|dotnet|mvn|gradle)\b", re.I)

# The user forbids touching files. Object-aware on purpose: "don't change the
# API, just add logging" must NOT match.
_FF5_NOEDIT_RE = re.compile(
    r"\b(?:do\s+not|don'?t|dont|never)\s+(?:edit|modify|change|touch|write\s+to|alter|"
    r"overwrite|rewrite|update)\s+(?:(?:my|the|any|these|those|your|our)\s+)?"
    r"(?:files?|code|source|anything|repo|repository|project|codebase)\b"
    r"|\b(?:do\s+not|don'?t|dont|never)\s+edit(?:\s+yet)?(?:\s*$|\s*[.,;!?)]|\s+(?:anything|yet))\b"
    r"|\b(?:no\s+edits?|not\s+editing(?:\s+yet)?)\b"
    r"|\bwithout\s+(?:editing|modifying|changing|touching|writing|altering)\b"
    r"|\bread[\s-]*only\b"
    r"|\b(?:khong|dung|chua|khoan)\s+(?:duoc\s+|can\s+|voi\s+)?(?:sua|chinh\s+sua|thay\s+doi|ghi|cap\s+nhat|dong\s+vao)"
    r"\s+(?:vao\s+)?(?:file|code|ma\s+nguon|source|source\s+code|bat\s+ky|gi|du\s+an|repo|project)\b"
    r"|\b(?:chua|khoan|khong|dung)\s+(?:duoc\s+|can\s+|voi\s+)?(?:sua|chinh\s+sua)(?:\s+(?:code|source|file|gi|vội|voi|luon|ngay|nhe|nha))*(?:\s*$|\s*[.,;!?)]|\s+(?:ma|nhung|chi|truoc)\b)"
    r"|\bchi\s+(?:doc|giai\s+thich|huong\s+dan|goi\s+y|dieu\s+tra|phan\s+tich|kiem\s+tra|xem|danh\s+gia|khao\s+sat|review)\b")

_FF5_DESTRUCTIVE_RE = re.compile(
    r"\bgit\s+(?:clean\s+-\w*[fdx]|reset\s+--hard|checkout\s+(?:--\s+)?\.(?:\s|$)|"
    r"restore\s+(?:--\S+\s+)*\.(?:\s|$)|stash\s+(?:drop|clear))"
    r"|\brm\s+-\w*r\w*f|\bremove-item\b[^\n]*-recurse|\brmdir\s+/s|\bdel\s+/s", re.I)

_FF5_READONLY_CMD_RE = re.compile(
    r"^\s*(?:git\s+(?:--no-pager\s+)?(?:status|diff|log|ls-files|show|branch|rev-parse)\b|"
    r"ls\b|dir\b|cat\b|type\b|echo\b|find\b|grep\b|rg\b|head\b|tail\b|pwd\b|cd\b|"
    r"python3?\s+-m\s+(?:py_compile|unittest|pytest)\b|pytest\b|npm\s+(?:run\s+)?test\b|"
    r"go\s+test\b|cargo\s+test\b)", re.I)
_FF5_FAIL_RE = re.compile(
    r"(?i)\b(?:error|cannot|denied|not\s+exist|exception|failed|fatal)\b|Exited with code [1-9]")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

class _Ff5Step:
    """Planned step: either tool calls to emit, or a final local text."""

    def __init__(self, name, calls=None, text=None):
        self.name, self.calls, self.text = name, calls, text


def _ff5_vi(ctx):
    return bool(_VI_RE.search((getattr(ctx, "last", "") or "").lower()))


def _ff5_say(ctx, vi_text, en_text):
    return _Ff5Step("say", text=vi_text if _ff5_vi(ctx) else en_text)


def _ff5_last_user_text(messages):
    idx = T._last_user_index(messages)
    if idx < 0:
        return ""
    return T._stringify_content(messages[idx].get("content", ""))


def _ff5_user_explicit_command(messages):
    """The command the newest user message asked to run, verbatim, else "".

    Only a RUN_COMMAND / RUN_TESTS classification whose extracted command is
    literally present in the user's own text counts as explicit permission.
    A substring match alone is not: "Đừng chạy `git reset --hard`, chỉ giải
    thích thôi" mentions the destructive command precisely so it must NOT run
    -- the old substring check treated any mention as consent (fail-open).
    """
    try:
        ctx = _analyze(messages)
    except Exception:
        return ""
    if getattr(ctx, "pattern", None) not in ("RUN_COMMAND", "RUN_TESTS"):
        return ""
    cmd = str(getattr(ctx, "cmd", "") or "").strip()
    if not cmd:
        return ""
    low_user = re.sub(r"\s+", " ", _ff5_last_user_text(messages)).lower()
    if cmd.lower() in low_user:
        return cmd
    return ""


def _ff5_noedit_text(text):
    if not isinstance(text, str) or not text:
        return False
    scannable = T._strip_code_spans(text)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable)).lower()
    return bool(_FF5_NOEDIT_RE.search(folded))




def _ff5_split_and(cmd):
    parts, buf, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif cmd.startswith("&&", i):
            parts.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _ff5_ps_command(cmd):
    """`a && b && c` -> `a; if ($?) { b; if ($?) { c } }` (Windows PowerShell 5.1)."""
    parts = _ff5_split_and(cmd)
    if len(parts) < 2:
        return cmd
    out = parts[-1]
    for p in reversed(parts[:-1]):
        out = f"{p}; if ($?) {{ {out} }}"
    return out


def _ff5_fix_for(cmd, messages):
    if _c5("flow5_ps_fix") and "&&" in cmd and _ff5_is_windows(messages):
        return _ff5_ps_command(cmd)
    return cmd


def _ff5_sig(call):
    fn = call["function"]
    return (fn["name"], T._canonical_args(T._tool_arguments_dict(fn)))


def _ff5_mk(name, args):
    return {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _ff5_allowed(tm):
    return {n for names in tm.by_kind.values() for n in names}


def _ff5_cmd_failed(res):
    return bool(_FF5_FAIL_RE.search(res or ""))


# --------------------------------------------------------------------------
# pattern detection + model-assisted classification
# --------------------------------------------------------------------------

def _ff5_command_from(raw):
    for span in re.findall(r"`([^`\n]+)`", raw or ""):
        s = span.strip()
        if _FF5_CMD_START_RE.match(s) and (" " in s or s.lower() in ("ls", "dir", "pytest")):
            return s
    m = re.search(r"```(?:\w+)?\n([^`]+?)```", raw or "", re.S)
    if m:
        lines = m.group(1).strip().splitlines()
        if lines and _FF5_CMD_START_RE.match(lines[0].strip()):
            return lines[0].strip()
    return ""




_FF5_PLAN_RE = re.compile(
    r"\b(?:write|draft|make|give\s+me|create|lap|viet|soan)\s+(?:me\s+)?(?:an?\s+|the\s+)?"
    r"(?:implementation\s+|detailed\s+)?(?:plan|ke\s+hoach)\b|\bplan\s+(?:for|how|to)\b|\blap\s+ke\s+hoach\b")


def _ff5_detect_s1(ctx):
    """Pattern for the new request shapes, else None."""
    folded = (ctx.folded or "").lower()
    raw = ctx.last or ""
    prev = getattr(ctx, "pattern", None)
    if _FF5_REVERT_RE.search(folded):
        return "REVERT"
    if _c5("flow5_plan") and prev in (None, "EXPLAIN", "CHANGE", "CREATE") and _FF5_PLAN_RE.search(folded):
        # "plan" is intent=explain with a plan-shaped answer; it never writes.
        ctx.plan = True
        return "EXPLAIN"
    if prev in (None, "TOOL_DEMAND", "EXPLAIN"):
        m = _FF5_GIT_RE.search(raw)
        if m and (_FF5_RUNV_RE.search(folded) or _FF5_SHOWV_RE.search(folded)):
            ctx.git_cmd = _ff5_git_cmd(m)
            return "GIT_INFO"
        if _FF5_RUNV_RE.search(folded):
            cmd = _ff5_command_from(raw)
            if cmd:
                ctx.cmd = cmd
                return "RUN_COMMAND"
            if _FF5_TESTS_RE.search(folded):
                return "RUN_TESTS"
    return None


_FLOW_STATE_TLS = _ff5_threading.local()


def _flow_state():
    """Unified accessor for flow engine thread-local state."""
    return _FLOW_STATE_TLS


_ff5_tls = _FLOW_STATE_TLS
_ff5_cache = {}


def _ff5_classify_prompt(text):
    lines = ["Classify the user's request into exactly ONE label from the list, "
             "or NONE if no label fits.", "Labels:"]
    lines += [f"- {k}: {v}" for k, v in _FF5_CATALOG.items()]
    lines += ["- NONE: anything else (chat, a question unrelated to the project, ...)",
              "", "Request:", text.strip()[:600], "",
              "Answer with the label only, nothing else."]
    return T.defuse_extension_words("\n".join(lines))




def _ff5_apply_label_s1(ctx, label):
    if not label or label not in _FF5_CATALOG:
        return
    raw = ctx.last or ""
    if label == "PLAN":
        # PLAN is intent=explain with a plan-shaped answer; it never writes.
        ctx.plan = True
        label = "EXPLAIN"
    if label == "RUN_COMMAND":
        cmd = _ff5_command_from(raw)
        if not cmd:
            return
        ctx.cmd = cmd
    elif label == "GIT_INFO":
        m = _FF5_GIT_RE.search(raw)
        ctx.git_cmd = _ff5_git_cmd(m) if m else "git --no-pager status --short --branch"
    elif label == "CREATE":
        files = _named_files(ctx)
        ctx.target = files[0] if files else ""
        ctx.dir_cands = _dir_cands(raw)
    elif label == "DEBUG":
        ctx.fix = bool(_FIX_RE.search((ctx.folded or "").lower()))
    ctx.pattern = label


def _ff5_supplement(ctx):
    """True when the newest message only supplements the previous request
    (pasted log / traceback, a bare path, "continue")."""
    raw = (ctx.last or "").strip()
    if not raw:
        return False
    if _TRACE_RE.search(raw):
        return True
    f = _ff5_fold(raw)
    if re.match(r"^\W*(?:ok|okay|yes|continue|go\s+on|proceed|tiep\s+tuc|tiep|duoc)\W*$", f):
        return True
    return len(raw) < 300 and bool(_named_files(ctx)) and not re.search(r"\w{3,}\s+\w{3,}\s+\w{3,}\s+\w{3,}", f)


def _ff5_inherit(messages):
    idxs = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"]
    for j in reversed(idxs[:-1]):
        prev = _analyze_s2(messages[:j + 1])
        if prev is None:
            continue
        for k, v in (("git_cmd", ""), ("cmd", ""), ("fix", False), ("target", ""),
                     ("create_merge", False), ("dir_cands", [])):
            if not hasattr(prev, k):
                setattr(prev, k, v)
        if not getattr(prev, "pattern", None):
            try:
                det = _ff5_detect(prev)
            except Exception:
                det = None
            if det:
                prev.pattern = det
        if getattr(prev, "pattern", None):
            return prev
    return None


def _analyze_s3(messages):
    ctx = _analyze_s2(messages)
    if ctx is None or not _c5("flow5_enabled"):
        return ctx
    try:
        for k, v in (("git_cmd", ""), ("cmd", ""), ("fix", False), ("target", ""),
                     ("create_merge", False), ("dir_cands", []), ("no_edit", False)):
            if not hasattr(ctx, k):
                setattr(ctx, k, v)
        if _clarified(messages, ctx.idx):
            ctx.perm = _ff5_permissions(messages, ctx.pattern, ctx.fix)
            ctx.perm.scope = _ff5_scope(ctx, messages)
            return ctx
        det = _ff5_detect(ctx)
        if det:
            ctx.pattern = det
        elif ctx.pattern is None and _c5("flow5_sticky") and _ff5_supplement(ctx):
            prev = _ff5_inherit(messages)
            if prev is not None:
                log(f"[fix5] follow-up message: keeping the earlier goal ({prev.pattern})")
                ctx.pattern = prev.pattern
                ctx.request = (prev.request or "") + "\n" + (ctx.last or "")
                ctx.scannable = (getattr(prev, "scannable", "") or "") + "\n" + ctx.scannable
                ctx.folded = (getattr(prev, "folded", "") or "") + " " + ctx.folded
                for k in ("fix", "target", "create_merge", "dir_cands", "cmd", "git_cmd"):
                    setattr(ctx, k, getattr(prev, k, getattr(ctx, k)))
        if ctx.pattern is None and _c5("flow5_classify"):
            _ff5_apply_label(ctx, _ff5_classify(ctx.last))
        perm = _ff5_permissions(messages, ctx.pattern, ctx.fix)
        # Proposal 1: "how can I fix X?" / "do not edit" -> intent stays explain.
        if ctx.pattern in ("CHANGE", "CREATE") and not perm.edit and _c5("flow5_respect_no_edit"):
            log(f"[fix5] {ctx.pattern} -> EXPLAIN ({perm.reason})")
            ctx.pattern = "EXPLAIN"
            perm = _ff5_permissions(messages, ctx.pattern, ctx.fix)
        elif (ctx.pattern is None and not perm.edit and _c5("flow5_respect_no_edit")
              and ("user forbade edits" in (perm.reason or "") or "guidance question" in (perm.reason or ""))):
            f_low = (ctx.folded or "").lower()
            prev_chg = globals().get("_rf_prev_is_change_request")
            first_cl = globals().get("_ff_first_clause")
            exp_rx = globals().get("_EXPLAIN_LEAD_RE")
            if ((callable(prev_chg) and prev_chg(ctx.folded, ctx.scannable, ctx.last))
                    or _FF5_HOWTO_RE.search(f_low)
                    or (callable(first_cl) and exp_rx and exp_rx.match(first_cl(ctx.last or "")))):
                log(f"[fix5] NONE -> EXPLAIN ({perm.reason})")
                ctx.pattern = "EXPLAIN"
                perm = _ff5_permissions(messages, ctx.pattern, ctx.fix)
        if ctx.pattern == "DEBUG" and ctx.fix and not perm.edit:
            ctx.fix = False
            perm = _ff5_permissions(messages, ctx.pattern, False)
        ctx.perm = perm
        ctx.no_edit = not perm.edit
        ctx.intent = (ctx.pattern or "").lower()
        ctx.scope = _named_files(ctx)
        perm.scope = _ff5_scope(ctx, messages)
    except Exception as e:
        log(f"[fix5] analyze error: {e}")
    if not hasattr(ctx, "perm"):
        ctx.perm = _Ff5Perm(True, "any", "default", False)
        ctx.perm.scope = None
    return ctx


# --------------------------------------------------------------------------
# REVERT: undo exactly what this conversation changed, nothing else
# --------------------------------------------------------------------------

def _ff5_session_acts(hist):
    """Successful write/edit calls in ``hist`` (messages BEFORE the revert request)."""
    calls, out = {}, []
    for i, msg in enumerate(hist):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    fn = tc.get("function") or {}
                    calls[tc["id"]] = (i, fn.get("name") or "", T._tool_arguments_dict(fn))
        elif msg.get("role") == "tool":
            hit = calls.get(msg.get("tool_call_id"))
            if not hit or tool_kind(hit[1]) != "act":
                continue
            res = T._stringify_content(msg.get("content"))
            path = _call_path(hit[2])
            if not path or not _act_ok({"result": res}):
                continue
            op = "edit" if any(isinstance(hit[2].get(k), str) for k in _OLD_KEYS) else "write"
            out.append({"idx": hit[0], "name": hit[1], "path": path, "op": op,
                        "args": hit[2]})
    return out


def _ff5_session_exec_files(hist):
    """Files named in the command/output of non-read-only shell calls."""
    calls, out = {}, []
    for msg in hist:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    fn = tc.get("function") or {}
                    calls[tc["id"]] = (fn.get("name") or "", T._tool_arguments_dict(fn))
        elif msg.get("role") == "tool":
            hit = calls.get(msg.get("tool_call_id"))
            if not hit or tool_kind(hit[0]) != "exec":
                continue
            cmd = _call_command(hit[1])
            if _FF5_READONLY_CMD_RE.match(cmd):
                continue
            res = T._stringify_content(msg.get("content"))[:4000]
            for f in sorted(T._files_in(cmd, res)):
                if f.lower() not in T._NON_FILE_NAMES and f not in out:
                    out.append(f)
    return out


def _ff5_read_before(hist, path, idx):
    for msg in hist[:idx]:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                if tool_kind(fn.get("name")) == "read":
                    p = _call_path(T._tool_arguments_dict(fn))
                    if p and T._path_seen(path, [p]):
                        return True
    return False


def _ff5_parse_status(text):
    """{path: XY} from `git status --short`; None when it is not a usable git status."""
    low = (text or "").lower()
    if "not a git repository" in low or re.search(
            r"\bfatal:|is not recognized|command not found", low):
        return None
    out = {}
    for ln in (text or "").splitlines():
        m = re.match(r"^([ MADRCUT?!]{2}) (.+?)\s*$", ln.rstrip("\r"))
        if m:
            p = m.group(2).split(" -> ")[-1].strip().strip('"').replace("\\", "/")
            out[p] = m.group(1)
    return out


def _ff5_status_path(path, status):
    for p in status or {}:
        if not p.endswith("/") and T._path_seen(path, [p]):
            return p
    return None


def _ff5_inverse_edits(ops, allowed):
    out = []
    for o in reversed(ops):
        args = o["args"]
        okey = next((k for k in _OLD_KEYS if isinstance(args.get(k), str)), None)
        nkey = next((k for k in _NEW_KEYS if isinstance(args.get(k), str)), None)
        if not okey or not nkey or not args[nkey] or o["name"] not in allowed:
            return None            # a pure deletion cannot be inverted safely
        inv = dict(args)
        inv[okey], inv[nkey] = args[nkey], args[okey]
        out.append((o, inv))
    return out


def _ff5_revert_actions(sess, xfiles, status, hist, tm, win):
    """(actions, notes). Conservative: only undo what is provably this session's."""
    actions, notes, handled = [], [], set()
    allowed = _ff5_allowed(tm)
    by_path = {}
    for a in sess:
        by_path.setdefault(_norm(a["path"]).lower(), []).append(a)

    def add_edits(path, ops):
        inv = _ff5_inverse_edits(ops, allowed)
        if inv is None:
            notes.append(("manual_checkout", path))
            return
        for o, args in inv:
            call = _ff5_mk(o["name"], args)
            actions.append({"kind": "edit", "call": call, "sig": _ff5_sig(call),
                            "label": "undo edit in " + path})

    def add_delete(spath):
        q = spath.replace('"', "")
        cmd = ('Remove-Item -LiteralPath "%s" -Force' % q) if win else ('rm -f -- "%s"' % q)
        call = tm.make_call("exec", command=cmd, description="Delete a file created in this session")
        if call:
            actions.append({"kind": "delete", "call": call, "sig": _ff5_sig(call),
                            "label": "delete " + spath})

    for _key, ops in by_path.items():
        path = ops[0]["path"]
        if status is None:
            if all(o["op"] == "edit" for o in ops):
                add_edits(path, ops)
            else:
                notes.append(("nogit", path))
            continue
        spath = _ff5_status_path(path, status)
        if spath is None:
            notes.append(("clean", path))
            continue
        handled.add(spath)
        code = status.get(spath, "")
        if code == "??":
            if _ff5_read_before(hist, path, ops[0]["idx"]):
                notes.append(("manual_overwrite", spath))
            else:
                add_delete(spath)
        elif all(o["op"] == "edit" for o in ops):
            add_edits(spath, ops)
        else:
            notes.append(("manual_checkout", spath))
    for f in xfiles:
        spath = _ff5_status_path(f, status) if status else None
        if not spath or spath in handled:
            continue
        handled.add(spath)
        if status.get(spath) == "??":
            add_delete(spath)
        else:
            notes.append(("manual_exec", spath))
    return actions, notes


def _ff5_revert_report(ctx, actions, exe, notes, runs):
    vi = _ff5_vi(ctx)

    def t(a, b):
        return a if vi else b

    done, failed = [], []
    for a in actions:
        res = exe.get(a["sig"])
        if res is None:
            bad = True
        elif a["kind"] == "edit":
            bad = not _act_ok({"result": res})
        else:
            bad = _ff5_cmd_failed(res)
        (failed if bad else done).append((a, res))
    L = []
    if done:
        L.append(t("**Đã hoàn tác:**", "**Reverted:**"))
        L += ["- " + a["label"] for a, _r in done]
    if failed:
        L.append(t("**Không hoàn tác được:**", "**Could NOT be reverted:**"))
        L += ["- %s -- %s" % (a["label"], " ".join((r or "not run").split())[:200])
              for a, r in failed]
    man = []
    for n in notes:
        k, p = n[0], n[1]
        if k == "manual_checkout":
            man.append(t("- %s: file có trong git nhưng bị ghi đè/sửa theo cách không đảo ngược an toàn được; "
                         "chưa tự hoàn tác vì có thể mất thay đổi chưa commit từ trước. Nếu chắc chắn: "
                         "git checkout -- \"%s\"",
                         "- %s: tracked file overwritten in a way that cannot be inverted safely; not "
                         "reverted because that could drop earlier uncommitted work. If you are sure: "
                         "git checkout -- \"%s\"") % (p, p))
        elif k == "manual_overwrite":
            man.append(t("- %s: file chưa được git theo dõi nhưng đã tồn tại trước session; không xoá.",
                         "- %s: untracked file that already existed before this session; not deleted.") % p)
        elif k == "manual_exec":
            man.append(t("- %s: bị thay đổi bởi một lệnh shell trong session; không tự hoàn tác.",
                         "- %s: changed by a shell command in this session; not reverted automatically.") % p)
        elif k == "nogit":
            man.append(t("- %s: không có git nên không xác minh được trạng thái; không tự hoàn tác.",
                         "- %s: no git repository, state cannot be verified; not reverted automatically.") % p)
    if man:
        L.append(t("**Cần bạn tự quyết:**", "**Left for you to decide:**"))
        L += man
    if actions:
        after = [ln for ln in (runs[-1]["result"] or "").splitlines() if ln.strip()]
        L.append(t("**git status sau khi hoàn tác:**", "**git status afterwards:**"))
        L.append("```\n" + ("\n".join(after[:20]) or t("(sạch)", "(clean)")) + "\n```")
    if not done and not failed and not man:
        L.append(t("Không có thay đổi nào của session cần hoàn tác (mọi file đã khớp git).",
                   "Nothing from this session needed reverting."))
    L.append(t("Lưu ý: chỉ động tới file mà session này đã ghi/sửa (file tạo gián tiếp qua script chỉ "
               "được nhận ra khi tên nó xuất hiện trong lệnh hoặc output); công việc chưa commit khác "
               "của bạn được giữ nguyên.",
               "Note: only files this session wrote or edited were touched (files created indirectly "
               "by a script are recognised only when their name appears in the command or output); "
               "your other uncommitted work was left alone."))
    return "\n".join(L)


def _ff5_plan_revert(ctx, messages, tm):
    ev = _answered_calls(messages)
    start = T._last_user_index(messages)
    hist = messages[:max(start, 0)]
    sess = [a for a in _ff5_session_acts(hist) if _base(a["path"]) != _MERGE_SCRIPT]
    xfiles = _ff5_session_exec_files(hist)
    if not sess and not xfiles:
        return _ff5_say(ctx, "Không thấy thay đổi nào do session này thực hiện để hoàn tác.",
                        "I found no change made by this conversation that needs reverting.")
    if not tm.first("exec"):
        return _ff5_say(ctx, "Cần công cụ shell để kiểm tra git status nhưng không có.",
                        "I need a shell tool to inspect git status and none is available.")
    runs = [e for e in ev if e["kind"] == "exec"
            and re.search(r"\bgit\b[^\n]*\bstatus\b", _call_command(e["args"]))]
    if not runs:
        call = tm.make_call("exec", command="git status --short",
                            description="List uncommitted changes")
        if call:
            return _Ff5Step("status", calls=[call])
        return _ff5_say(ctx, "Không tạo được lệnh git status.", "Could not build git status.")
    status = _ff5_parse_status(runs[0]["result"])
    win = _ff5_is_windows(messages)
    actions, notes = _ff5_revert_actions(sess, xfiles, status, hist, tm, win)
    exe = {(e["name"], T._canonical_args(e["args"])): e["result"] for e in ev}
    for a in actions:
        if a["sig"] not in exe:
            return _Ff5Step("revert", calls=[a["call"]])
    if actions and len(runs) < 2:
        call = tm.make_call("exec", command="git status --short", description="Verify the result")
        if call:
            return _Ff5Step("verify", calls=[call])
    return _Ff5Step("say", text=_ff5_revert_report(ctx, actions, exe, notes, runs))


# --------------------------------------------------------------------------
# RUN_TESTS / RUN_COMMAND / GIT_INFO
# --------------------------------------------------------------------------

def _ff5_block(out, limit=4000):
    out = (out or "").strip()
    if len(out) > limit:
        out = "[...]\n" + out[-limit:]
    return "```\n" + (out or "(no output)") + "\n```"


def _ff5_plan_tests_s1(ctx, messages, tm):
    ev = _answered_calls(messages)
    if not tm.first("exec"):
        return _ff5_say(ctx, "Không có công cụ shell để chạy test.", "No shell tool to run tests.")
    listing = _listing_files(ev)
    if not listing and _survey_count(ev) < 1:
        s = _survey_step("RUN_TESTS", tm, ev, 1, 3)
        if s and s.calls:
            return _Ff5Step("survey", calls=s.calls)
    names_all = listing + [x for x in (_call_path(e["args"]) for e in ev) if x]
    cmd = pick_test_command(names_all, ev)
    if cmd:
        call = tm.make_call("exec", command=_ff5_fix_for(cmd, messages),
                            description="Run unit tests")
        if call:
            return _Ff5Step("tests", calls=[call])
    runs = _test_runs(ev)
    if not runs:
        return _ff5_say(ctx, "Không đoán được lệnh test của project. Hãy cho mình biết lệnh cần chạy.",
                        "I could not work out this project's test command. Tell me which one to run.")
    _i, c, res = runs[-1]
    outcome = test_outcome(res)
    summ = " | ".join(_test_summary([{"result": res}]))
    vi = _ff5_vi(ctx)
    head = {"pass": ("Test PASS", "Tests PASSED"), "fail": ("Test FAIL", "Tests FAILED"),
            "broken": ("Lệnh test không chạy được", "The test command itself could not run"),
            }.get(outcome, ("Không xác định được kết quả test", "Test outcome unclear"))
    body = _excerpt(res, 1500) if outcome in ("fail", "broken") else summ
    return _Ff5Step("say", text="%s (`%s`)\n%s" % (head[0] if vi else head[1], c,
                                                    _ff5_block(body, 1600)))


def _ff5_plan_command_s1(ctx, messages, tm, cmd, label):
    ev = _answered_calls(messages)
    cmd = _ff5_fix_for(cmd, messages)
    runs = [e for e in ev if e["kind"] == "exec" and _call_command(e["args"]).strip() == cmd.strip()]
    if not runs:
        call = tm.make_call("exec", command=cmd, description=label) if tm.first("exec") else None
        if call:
            return _Ff5Step("exec", calls=[call])
        return _ff5_say(ctx, "Không có công cụ shell để chạy lệnh.", "No shell tool to run the command.")
    return _Ff5Step("say", text="`%s`\n%s" % (cmd, _ff5_block(runs[-1]["result"])))


def _ff5_plan_s1(ctx, messages, tm):
    p = ctx.pattern
    if p == "REVERT":
        return _ff5_plan_revert(ctx, messages, tm)
    if p == "RUN_TESTS":
        return _ff5_plan_tests(ctx, messages, tm)
    if p == "RUN_COMMAND" and ctx.cmd:
        return _ff5_plan_command(ctx, messages, tm, ctx.cmd, "Run the requested command")
    if p == "GIT_INFO" and ctx.git_cmd:
        return _ff5_plan_command(ctx, messages, tm, ctx.git_cmd, "Show git information")
    return None


# --------------------------------------------------------------------------
# Findings 3 + 4: fail-closed act gate, read guard, per-file quote verifier
# --------------------------------------------------------------------------

def _ff5_read_guard_s1(ctx, messages):
    """Text when a failed read would otherwise lead on to an edit step, else None."""
    if getattr(ctx, "pattern", None) not in ("CHANGE", "DEBUG"):
        return None
    if ctx.pattern == "DEBUG" and not getattr(ctx, "fix", False):
        return None
    ev = _answered_calls(messages)
    if any(e["kind"] == "act" for e in ev):
        return None
    reads = [e for e in ev if e["kind"] == "read"]
    bad = [e for e in reads if _ff5_status(e) == "failed" and not _is_doc(_call_path(e["args"]) or "")]
    if not bad:
        return None
    good = [e for e in reads if _ff5_status(e) in ("success", "partial")
            and not _is_doc(_call_path(e["args"]) or "")]
    named = [f.lower() for f in _named_files(ctx)]
    bad_named = any(any(T._path_seen(n, [_call_path(e["args"]) or ""]) for n in named)
                    for e in bad)
    if good and not bad_named:
        return None
    detail = "; ".join("%s: %s" % (_call_path(e["args"]), " ".join((e["result"] or "").split())[:120])
                       for e in bad[:3])
    if _ff5_vi(ctx):
        return ("Không đọc được file cần sửa nên mình KHÔNG thực hiện chỉnh sửa nào (" + detail +
                "). Hãy kiểm tra lại đường dẫn rồi yêu cầu lại.")
    return ("I could not read the file to change, so I made NO edit (" + detail +
            "). Check the path and ask again.")


_FF5_PART_RE = re.compile(r"\s*\(part\s+\d+\)\s*$")


def _ff5_quote_ok(snip, cand, line):
    qn = T._norm_code(snip)
    if not qn:
        return False
    for label, body in cand:
        is_part = bool(_FF5_PART_RE.search(label))
        for i, ln in enumerate(body.split("\n")):
            if qn in T._norm_code(ln):
                if line is None or is_part:
                    return True
                mm = re.match(r"^\s*(\d+)\s*(?:\||:|\t|\u2192)", ln)
                actual = int(mm.group(1)) if mm else i + 1
                if abs(actual - line) <= 5:
                    return True
    return False


def _verify_findings_s1(text, chunk):
    """(kept, dropped). A quote counts only inside the file the finding names,
    and (when the file shows line numbers) within 5 lines of the claimed one."""
    kept, dropped = [], 0
    if re.fullmatch(r"\W*NONE\W*", (text or "").strip(), re.IGNORECASE):
        return kept, dropped
    files = [(p, b) for p, b in chunk]
    for item in _split_findings(text):
        snippets = [s for s in re.findall(r"`([^`\n]+)`", item) if len(s) >= 8]
        if not snippets:
            dropped += 1
            continue
        head = item.split("`", 1)[0]
        m = re.search(r"((?:[A-Za-z]:)?[^\s:`'\"<>|*?]+?\.[A-Za-z0-9]{1,8})"
                      r"(?:\s*\(part\s+\d+\))?\s*(?::\s*(\d+))?", head)
        line = int(m.group(2)) if m and m.group(2) else None
        if m:
            want = m.group(1).replace("\\", "/")
            cand = [(p, b) for p, b in files
                    if T._path_seen(want, [_FF5_PART_RE.sub("", p).replace("\\", "/")])]
            if not cand:
                dropped += 1          # names a file that is not in this chunk
                continue
        else:
            cand = files
        if all(_ff5_quote_ok(s, cand, line) for s in snippets):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


# --------------------------------------------------------------------------
# post-filter on whatever the planner/model produced
# --------------------------------------------------------------------------

def _ff5_successful_read_paths(messages):
    """Paths of every read that actually produced content (success/partial).

    ``failed`` and ``unknown`` (empty) reads are excluded: they prove nothing,
    so they must never unlock an edit -- the same rule :func:`_ff5_read_guard`
    applies to failed reads, extended to the case where NO read happened at
    all.
    """
    out = []
    for e in _answered_calls(messages):
        if e.get("kind") != "read":
            continue
        if _ff5_status(e) not in ("success", "partial"):
            continue
        p = _call_path(e.get("args") or {})
        if p:
            out.append(p)
    return out




def _ff5_has_fresh_evidence_after_last_act(messages, path):
    """True when re-editing ``path`` is grounded in post-edit evidence.

    After the last act on the file, either a successful/partial read of it or
    a fresh ``git diff`` must exist; otherwise the model would be writing from
    a copy that its own earlier edit already invalidated. No act yet -> True
    (the ordinary first-edit gates apply).
    """
    ev = _answered_calls(messages)
    last_act = -1

    for i, e in enumerate(ev):
        if e.get("kind") != "act":
            continue
        if not _act_ok(e):
            continue          # a failed edit left the file untouched
        p = _call_path(e.get("args") or {})
        if p and T._path_seen(path, [p]):
            last_act = i

    if last_act < 0:
        return True

    for i, e in enumerate(ev):
        if i <= last_act:
            continue

        kind = e.get("kind")
        if kind == "read":
            p = _call_path(e.get("args") or {})
            if (p and T._path_seen(path, [p])
                    and _ff5_status(e) in ("success", "partial")):
                return True

        if kind == "exec":
            cmd = _call_command(e.get("args") or {})
            if re.search(r"\bgit\s+(?:--no-pager\s+)?diff\b", cmd):
                return True

    # If `_ff9_read_texts` still tracks the updated content of `path` in-place
    # (from earlier edits or full-file writes, with no failed test run since `last_act`),
    # allow subsequent phase edits without forcing a redundant re-read.
    if "_ff9_read_texts" in globals():
        tracked = _ff9_read_texts(messages)
        tk = _ff17_find_key(tracked, path) if "_ff17_find_key" in globals() else None
        if tk is not None or any(T._path_seen(path, [k]) for k in tracked):
            tests_ran = _tests_ran_after_act(ev) if "_tests_ran_after_act" in globals() else False
            if not tests_ran or len(tracked.get(tk or path) or "") > 50000:
                return True

    return False


def _ff5_postfilter_s1(result, messages, perm=None, last=True, tm=None, ctx=None):
    """Returns (result, nudge). ``nudge`` is a corrective instruction naming the
    SPECIFIC problem when a guard rejected the output and a retry is still
    allowed (last=False); on the last attempt the guard blocks instead
    (fail closed) and ``nudge`` is None."""
    if not (isinstance(result, tuple) and len(result) == 2):
        return result, None
    text, calls = result
    if not calls:
        return result, None
    last_user = _ff5_last_user_text(messages)
    vi = bool(_VI_RE.search(last_user.lower()))
    if perm is None:
        perm = _ff5_permissions(messages)

    def blocked(vi_text, en_text, nudge=None, retry=False):
        if retry and not last and nudge:
            return None, nudge
        msg = vi_text if vi else en_text
        return (((text + "\n\n") if text else "") + msg, None), None

    # 0a. read-before-edit: no act may target a file never successfully read
    # (fail-open hole: _ff5_read_guard only fires when a read FAILED, and the
    # evidence gate returns early when there is no read at all).
    if ctx is not None and _c5("flow5_fail_closed_edits"):
        read_probs = _ff5_act_needs_read_problems(calls, messages, ctx)
        if read_probs:
            body = "\n".join("- " + x for x in read_probs[:5])
            log("[fix5] edit without a successful read: " + " | ".join(read_probs))
            needed_paths = []
            for tc in calls or []:
                fn = (tc or {}).get("function") or {}
                if tool_kind(fn.get("name")) == "act":
                    p = _call_path(T._tool_arguments_dict(fn))
                    if p and any(p in prob for prob in read_probs) and p not in needed_paths:
                        needed_paths.append(p)
            if needed_paths and tm is not None and tm.first("read"):
                read_calls = _read_calls(tm, needed_paths)
                if read_calls:
                    log("[fix5] converting needs-read edit into driven read step: " + ", ".join(needed_paths))
                    return ("", read_calls), None
            return blocked(
                "Mình chưa sửa vì file chưa được đọc thành công:\n" + body,
                "I did not edit because the target file was not successfully "
                "read:\n" + body,
                "Read the target file successfully before editing it:\n" + body,
                retry=False)

    # 0. adapt every call to the schema the client actually declared (proposal 3)
    if tm is not None:
        calls, probs = _ff5_adapt_calls(calls, tm)
        if probs:
            body = "\n".join("- " + x for x in probs[:5])
            log("[fix6] tool calls did not match the client schema: " + " | ".join(probs))
            return blocked(
                "Mình chưa gửi lời gọi công cụ vì nó không khớp schema của client "
                "(chưa có gì được thực thi):\n" + body,
                "I did not send the tool call because it does not match the "
                "client's schema (nothing ran):\n" + body,
                "Your tool call does not match the tool schema. Fix exactly these "
                "problems and call it again:\n" + body, retry=True)

    # 0b. scope: writes are limited to the files the user named/read (proposal 1)
    if _c5("flow6_scope_guard"):
        scope = _ff5_scope_problems(calls, messages, perm)
        if scope:
            body = "\n".join("- " + x for x in scope[:5])
            log("[fix6] out-of-scope write(s): " + " | ".join(scope))
            return blocked(
                "Mình chưa ghi vì (các) file đó nằm ngoài phạm vi yêu cầu này "
                "(chưa có gì được ghi):\n" + body,
                "I did not write because those files are outside this request's "
                "scope (nothing was written):\n" + body,
                "Your edit targets a file outside this request. Edit only the "
                "files the user named or the files read in this turn:\n" + body,
                retry=True)

    # 1. intent vs permission: the user (or a guidance question) forbade edits
    if _c5("flow5_respect_no_edit") and not perm.edit:
        kept = [tc for tc in calls
                if tool_kind((tc.get("function") or {}).get("name")) != "act"]
        if len(kept) != len(calls):
            log("[fix5] dropped file-writing tool call(s): %s" % perm.reason)
            if not kept:
                return blocked("Bạn không cho phép sửa file (%s) nên mình không thực hiện chỉnh sửa nào." % perm.reason,
                               "Edits are not allowed here (%s), so I made no edit." % perm.reason)[0], None
            calls = kept

    # 2. command policy: three permission levels, wide destructive commands,
    #    PowerShell syntax.
    low_user = last_user.lower()
    win = _c5("flow5_ps_fix") and _ff5_is_windows(messages)
    if perm.run == "none":
        kept = [tc for tc in calls
                if tool_kind((tc.get("function") or {}).get("name")) != "exec"]
        if len(kept) != len(calls):
            log("[fix6] dropped command(s): the user forbade running any command")
            if not kept:
                return blocked(
                    "Bạn không cho phép chạy lệnh nên mình không chạy gì cả (%s)."
                    % perm.reason,
                    "Running commands is not allowed here (%s), so I ran nothing."
                    % perm.reason,
                    "Do not run any command. Answer from the files you already "
                    "read, or say what information you still need.", retry=True)
            calls = kept
    new = []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "exec":
            new.append(tc)
            continue
        args = T._tool_arguments_dict(fn)
        ckey = next((k for k in _CMD_KEYS if isinstance(args.get(k), str)), None)
        if not ckey:
            new.append(tc)
            continue
        cmd = args[ckey]
        if perm.run == "readonly" and not _ff5_cmd_readonly(cmd):
            log("[fix5] blocked a non read-only command in read-only mode: " + cmd[:200])
            return blocked("Mình chặn lệnh `%s` vì yêu cầu này chỉ cho phép lệnh chỉ-đọc (%s)." % (cmd[:200], perm.reason),
                           "I blocked `%s`: only read-only commands are allowed here (%s)." % (cmd[:200], perm.reason),
                           "Your last command `%s` is not read-only. Use only read-only commands "
                           "(ls, git status/diff/log, cat, grep, test runners), or answer from what you have read." % cmd[:120],
                           retry=True)
        m = _FF5_DESTRUCTIVE_RE.search(cmd)
        if (m and _c5("flow5_block_destructive")
                and cmd.strip() != _ff5_user_explicit_command(messages)):
            log("[fix5] blocked a destructive command: " + cmd[:200])
            return blocked("Mình đã chặn lệnh `%s`: nó xoá/ghi đè MỌI thay đổi chưa commit trong repo, không "
                           "chỉ thay đổi của session này. Hãy tự chạy nếu bạn thật sự muốn." % cmd[:200],
                           "I blocked `%s`: it would wipe ALL uncommitted work in the repo, not only this "
                           "session's changes. Run it yourself if you really mean it." % cmd[:200],
                           "`%s` would wipe ALL uncommitted work. Undo only the specific files this session "
                           "changed (edit them back), never the whole tree." % cmd[:120], retry=True)
        if win and "&&" in cmd:
            fixed = _ff5_ps_command(cmd)
            if fixed != cmd:
                log("[fix5] rewrote && for PowerShell")
                args[ckey] = fixed
                tc = {**tc, "function": {**fn, "arguments": json.dumps(args, ensure_ascii=False)}}
        new.append(tc)
    calls = new

    # 3. fail closed: a write that still fails the guards is NOT handed to the client
    if _c5("flow5_fail_closed_edits"):
        touched = [p for p in (_call_path(e["args"]) for e in _answered_calls(messages)
                               if e["kind"] == "act" and _act_ok(e)) if p]
        probs, new = [], []
        for tc in calls:
            fn = tc.get("function") or {}
            if tool_kind(fn.get("name")) != "act":
                new.append(tc)
                continue
            p = _call_path(T._tool_arguments_dict(fn))
            if p and any(T._path_seen(p, [x]) for x in touched):
                # Earlier edit this turn: the read copy is stale. It may not
                # simply write again from that copy -- a fresh read of the
                # file or a fresh git diff after the last act is required.
                if _ff5_has_fresh_evidence_after_last_act(messages, p):
                    new.append(tc)
                    continue
                probs.append(
                    f"{p}: file was already edited; re-read it or show a fresh "
                    "diff before another edit")
                continue
            fixed, pr = _gate_act_calls([tc], messages)
            if pr:
                probs.extend(pr)
            else:
                new.append(fixed[0])
        if probs:
            log("[fix5] unsafe write(s): " + " | ".join(probs))
            body = "\n".join("- " + x for x in probs[:5])
            return blocked("Mình đã chặn thao tác ghi file vì không qua được kiểm tra an toàn (chưa có gì "
                           "được ghi):\n" + body, "I blocked the file write because it failed the safety "
                           "checks (nothing was written):\n" + body,
                           "Your file edit was rejected. Fix exactly these problems and call the edit again:\n"
                           + body, retry=True)
        calls = new
    return (text, calls), None


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------

def _ff5_wrap_change(server):
    if not getattr(T._is_change_request, "_ff5", False):
        orig = T._is_change_request

        def is_change_request(folded, scannable, raw=""):
            if _c5("flow5_respect_no_edit") and (
                    _FF5_NOEDIT_RE.search((folded or "").lower()) or _ff5_noedit_text(raw)):
                return False
            return orig(folded, scannable, raw)

        is_change_request._ff5 = True
        T._is_change_request = is_change_request
    cur = T.change_demanded
    if not getattr(cur, "_ff5", False):
        def change_demanded(messages):
            if _c5("flow5_respect_no_edit") and not _ff5_permissions(messages).edit:
                return False
            return cur(messages)

        change_demanded._ff5 = True
        T.change_demanded = change_demanded
        if getattr(server, "change_demanded", None) is cur:
            server.change_demanded = change_demanded


def _wrap_ff5(cur):
    @functools.wraps(cur)
    def wrapper5(self, prompt, model_id, think_mode, file_refs, extra,
                 tools_active, allowed_names, tool_schemas, required_tool,
                 messages=None, ticket=None, tool_required=None,
                 client_gone=None):
        def call():
            return cur(self, prompt, model_id, think_mode, file_refs, extra,
                       tools_active, allowed_names, tool_schemas, required_tool,
                       messages=messages, ticket=ticket,
                       tool_required=tool_required, client_gone=client_gone)

        if not (_c5("flow5_enabled") and tools_active and messages and allowed_names):
            return call()
        _ff5_tls.model = (model_id, think_mode, extra, ticket)
        tm, perm, ctx = None, None, None
        try:
            last = messages[-1] if isinstance(messages[-1], dict) else {}
            if not required_tool and last.get("role") in ("user", "tool"):
                tm = ToolMap(allowed_names, tool_schemas, tool_required)
                ctx = _analyze(messages)
                if ctx is not None:
                    perm = getattr(ctx, "perm", None)
                    guard = _ff5_read_guard(ctx, messages)
                    if guard:
                        log("[fix5] read failed before an edit: stopping instead of editing")
                        return guard, None
                    if getattr(ctx, "pattern", None) in _FF5_OWN:
                        step = _ff5_plan(ctx, messages, tm)
                        if step is not None:
                            log(f"[fix5] pattern={ctx.pattern} step={step.name} "
                                f"({'local answer' if step.text is not None else 'drive'})")
                            _ff5_metric("own_" + step.name)
                            final = _ff5_own_result(step, ctx, messages, tm, perm)
                            if final is not None:
                                return final
        except Exception as e:
            log(f"[fix5] planner error, falling back: {e}")
        result = call()
        rejected_result = None
        try:
            retries = max(0, int(_c5("flow5_guard_retries")))
            for attempt in range(retries + 1):
                final, nudge = _ff5_postfilter(
                    result, messages, perm, last=(attempt >= retries), tm=tm,
                    ctx=ctx)
                if nudge is None:
                    return final
                rejected_result = result
                _ff5_metric("guard_retries")
                log(f"[fix5] guard rejected the output, re-asking (attempt {attempt + 1}): {nudge[:160]}")
                result = cur(self, prompt + "\n\n[SYSTEM NOTE] " + nudge, model_id, think_mode, file_refs,
                             extra, tools_active, allowed_names, tool_schemas, required_tool,
                             messages=messages, ticket=ticket, tool_required=tool_required,
                             client_gone=client_gone)
            return result
        except Exception as e:
            log(f"[fix5] postfilter error: {e}")
            if rejected_result is not None:
                try:
                    blocked_final, _ = _ff5_postfilter(
                        rejected_result, messages, perm, last=True, tm=tm, ctx=ctx)
                    if blocked_final is not None:
                        return blocked_final
                except Exception:
                    pass
            return result

    wrapper5._flow5_patched = True
    return wrapper5
# ==== flowfix5:end ====

# ==== flowfix6b:begin (generated by patch_flow_critical.py) ====
# 4. default perm TOOL_DEMAND (unified into _ff5_postfilter)
# Overrides (later definitions win: callers resolve these names at call time).


# 3. tighter read-only check (unified into _ff5_cmd_readonly)
_FF6B_FIND_RE = re.compile(
    r"\bfind\b[^|;&]*\s-(?:delete|exec|execdir|ok|okdir|fprint\w*|fls)\b", re.I)
_FF6B_BRANCH_RE = re.compile(
    r"\bgit\s+(?:--no-pager\s+)?branch\b[^|;&]*\s"
    r"(?:-d|-m|-c|-f|--delete|--move|--copy|--force)\b", re.I)
_FF6B_REMOTE_RE = re.compile(
    r"\bgit\s+(?:--no-pager\s+)?remote\s+(?:add|remove|rm|rename|set-url|"
    r"set-head|prune|update)\b", re.I)
_FF6B_OUTPUT_RE = re.compile(r"\bgit\b[^|;&]*--output\b", re.I)


# 2. REVERT deletes files with exec: it must obey the same no-edit permission.
def _ff5_plan(ctx, messages, tm):
    if getattr(ctx, "pattern", None) == "REVERT":
        perm = getattr(ctx, "perm", None)
        if perm is not None and not perm.edit:
            return _ff5_say(
                ctx,
                "Mình không hoàn tác vì việc này cần sửa/xoá file mà hiện không "
                "được phép (%s). Nếu bạn muốn hoàn tác thật, hãy nói rõ, ví dụ: "
                "'cứ sửa luôn, hoàn tác các thay đổi'." % perm.reason,
                "I did not revert anything: reverting edits or deletes files and "
                "that is not allowed here (%s). If you really want it, say so "
                "explicitly, e.g. 'go ahead and revert the changes'." % perm.reason)
    return _ff5_plan_s1(ctx, messages, tm)


# 6a. classifier: never cache an upstream failure as "NONE"
def _ff5_classify_s1(text):
    text = (text or "").strip()
    if len(text) < 8:
        return None
    key = _ff5_hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    if key in _ff5_cache:
        return _ff5_cache[key]
    model = getattr(_ff5_tls, "model", None)
    if not model:
        return None
    label, answered = None, False
    try:
        m_id, m_think, m_extra, m_ticket = model
        raw = generate(_ff5_classify_prompt(text), m_id, m_think, None,
                       m_extra, m_ticket)
        if raw and not T.looks_like_canned_failure(raw):
            answered = True
            for w in re.findall(r"[A-Z][A-Z_]{3,}", raw.upper()):
                if w in _FF5_CATALOG:
                    label = w
                    break
                if w == "NONE":
                    break
        log(f"[fix6b] model classified the request as {label or 'NONE'}")
    except Exception as e:
        log(f"[fix6b] classify failed, model runs freely: {e}")
    if answered:
        if len(_ff5_cache) > 300:
            _ff5_cache.clear()
        _ff5_cache[key] = label
    return label


# 6b. cross-module: keyword args, **kwargs and decorators are not arity errors
def _ff5_py_defs_s1(tree):
    out = {}
    for node in getattr(tree, "body", ()):
        if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
            a = node.args
            pos = [x.arg for x in list(getattr(a, "posonlyargs", ())) + list(a.args)]
            out[node.name] = {
                "kind": "function",
                "required": max(0, len(pos) - len(a.defaults)),
                "total": len(pos),
                "vararg": a.vararg is not None,
                "varkw": a.kwarg is not None,
                "names": pos,
                "kwonly": [x.arg for x in a.kwonlyargs],
                "decorated": bool(node.decorator_list),
                "line": getattr(node, "lineno", 0),
            }
        elif isinstance(node, _ff5_ast.ClassDef):
            out[node.name] = {"kind": "class", "required": 0, "total": 0,
                              "vararg": True, "line": getattr(node, "lineno", 0)}
    return out


def _ff5_arg_problem_s1(node, info):
    if info.get("kind") != "function" or info.get("decorated"):
        return ""
    if any(isinstance(a, _ff5_ast.Starred) for a in node.args):
        return ""
    if any(k.arg is None for k in node.keywords):      # f(**kw)
        return ""
    given = len(node.args)
    kw = {k.arg for k in node.keywords}
    names = info.get("names") or []
    required, total = info.get("required", 0), info.get("total", 0)
    missing = [n for n in names[given:required] if n not in kw]
    if missing:
        return ("expects at least %d argument(s) but %s not supplied (got %d "
                "positional)" % (required, ", ".join(missing), given))
    if given > total and not info.get("vararg"):
        return ("expects at most %d positional argument(s) but got %d"
                % (total, given))
    return ""
# ==== flowfix6b:end ====

# ==== flowfix6c:begin (generated by patch_flow_design.py) ====

_FF6C_NOTFOUND_RE = re.compile(
    r"(?i)no such file|not found|does not exist|enoent|cannot find|"
    r"cannot open|can'?t find")


# 2 & 3. Guidance question & pattern None (unified into _ff5_permissions and _ff5_scope above)


# --------------------------------------------------------------------------
# 1. new file vs existing file
# --------------------------------------------------------------------------
def _ff6c_is_write(args):
    return not any(isinstance(args.get(k), str) and args[k] for k in _OLD_KEYS)


def _ff6c_survey_listing(ev):
    sub = [e for e in ev if e["kind"] == "survey" or (
        e["kind"] == "exec" and _LIST_CMD_RE.search(_call_command(e["args"])))]
    return _listing_files(sub) if sub else []






def _ff5_scope_problems(calls, messages, perm):
    scope = getattr(perm, "scope", None) if perm is not None else None
    if scope is None:
        return []
    problems = []
    shown = ", ".join(scope[:6]) or "(no write target)"
    for tc in calls or []:
        fn = (tc or {}).get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue
        args = T._tool_arguments_dict(fn)
        path = _call_path(args)
        if not path or _ff5_in_scope(path, scope):
            continue
        # a brand-new TEST file is allowed even if the user did not name it
        if (scope and _ff6c_is_write(args) and _is_test_path(path)
                and _ff6c_proven_new(path, messages)):
            continue
        problems.append(
            f"{path}: outside this request's write scope ({shown}). "
            "Only edit the files the user named or the files read in this turn.")
    return problems


def _ff6c_filter(ctx, messages):
    """Hide 'not found' reads of files that were never meant to exist yet.

    A failed read only matters when the user named an EXISTING file. A file the
    user asked to create, or one the model probed to check it is new, is not an
    error.
    """
    try:
        scannable = getattr(ctx, "scannable", None)
        if scannable is None:
            return messages
        created = T._created_files(scannable, ctx.folded)
        keep_named = [f for f in _named_files(ctx) if f.lower() not in created]
        truncated = {p for _i, p, _n in
                     _ff4_truncations(_answered_calls(messages))}
        start = T._last_user_index(messages)
        calls, drop = {}, set()
        for msg in messages[start + 1:]:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        fn = tc.get("function") or {}
                        calls[tc["id"]] = (fn.get("name") or "",
                                           T._tool_arguments_dict(fn))
            elif msg.get("role") == "tool":
                hit = calls.get(msg.get("tool_call_id"))
                if not hit or tool_kind(hit[0]) != "read":
                    continue
                path = _call_path(hit[1])
                body = T._stringify_content(msg.get("content"))
                if (path and _FF5_READFAIL_RE.match(body)
                        and _FF6C_NOTFOUND_RE.search(body[:300])
                        and not any(T._path_seen(path, [f]) for f in keep_named)
                        and not any(T._path_seen(path, [p]) for p in truncated)):
                    drop.add(msg.get("tool_call_id"))
        if not drop:
            return messages
        return [m for m in messages if not (
            isinstance(m, dict) and m.get("role") == "tool"
            and m.get("tool_call_id") in drop)]
    except Exception as e:
        log(f"[fix6c] read filter error: {e}")
        return messages


def _ff5_read_guard_s2(ctx, messages):
    return _ff5_read_guard_s1(ctx, _ff6c_filter(ctx, messages))


def _ff5_evidence_gate_s2(ctx, messages, tm):
    return _ff5_evidence_gate_s1(ctx, _ff6c_filter(ctx, messages), tm)
# ==== flowfix6c:end ====

# ==== flowfix8:begin (generated by flow_fix8_patch.py -- re-run to refresh, --revert to remove) ====
import threading as _ff8_threading



_c8 = _flow_c


_ff8_tls = _FLOW_STATE_TLS

# --------------------------------------------------------------------------
# 1. Read-only command check: parse the WHOLE command, refuse what we cannot
#    reason about (newline, background &, backticks, $(), redirects, writing flags)
# --------------------------------------------------------------------------


# (Shell safety check unified into _ff5_cmd_readonly above)


# 4. Edit grant and run grant are independent (unified into _ff5_permissions above)


# --------------------------------------------------------------------------
# 3. A listing that does not cover the tree proves nothing about absence
# --------------------------------------------------------------------------

_FF8_NOTFOUND_RE = re.compile(
    r"(?i)no such file|not found|enoent|does not exist|cannot find|could not find")
_FF8_TRUNC_RE = re.compile(
    r"(?i)truncat|\bmore\b.*\b(?:results?|files?|matches)\b|showing (?:the )?first"
    r"|limit(?:ed)?\b|\[\.\.\.|\.\.\.\s*\d+")
_FF8_FULL_GLOBS = {"**/*", "**", "*", "**/*.*", "./**/*"}
_FF8_FIND_ALL_RE = re.compile(r"^\s*find\s+\.\s+-type\s+f\s*$")


def _ff8_complete_listings(ev):
    """File lists from listings that PROVABLY cover the whole tree: an
    unrestricted glob (no extension filter, no sub-path), short enough not to
    have hit a client cap, no truncation marker. Anything else (the default
    extension-limited survey, git ls-files which omits untracked files, a
    capped or cut result) is not complete."""
    cap = int(_c8("flow8_listing_cap"))
    out = []
    for e in ev:
        res = e.get("result") or ""
        lines = [ln for ln in res.splitlines() if ln.strip()]
        if not lines or len(lines) >= cap:
            continue
        if _FF8_TRUNC_RE.search(" ".join(lines[-3:])):
            continue
        args = e.get("args") or {}
        ok = False
        if e.get("kind") == "survey":
            pk = _pick(args, _PATTERN_KEYS)
            pat = str(args.get(pk) or "").strip() if pk else ""
            sub = _call_path(args)
            ok = pat in _FF8_FULL_GLOBS and (not sub or sub.strip("./ ") == "")
        elif e.get("kind") == "exec":
            ok = bool(_FF8_FIND_ALL_RE.match(_call_command(args)))
        if ok:
            files = _listing_files([e])
            if files:
                out.append(files)
    return out


def _ff8_failed_notfound(path, ev):
    for e in ev:
        if e.get("kind") != "read" or _ff5_status(e) != "failed":
            continue
        p = _call_path(e.get("args") or {})
        if p and T._path_seen(path, [p]) and _FF8_NOTFOUND_RE.search(
                (e.get("result") or "")[:300]):
            return True
    return False


def _ff8_listing_absent(path, ev):
    n = _norm(path)
    segs = n.split("/")
    if _base(n).startswith(".") or any(
            s in _IGNORE_SEG or s.startswith(".") for s in segs[:-1]):
        return False          # globs usually skip hidden / ignored dirs
    for files in _ff8_complete_listings(ev):
        if not any(T._path_seen(n, [f]) for f in files):
            return True
    return False


def _ff8_proven_absent(path, ev):
    """True only with evidence: a read that said not-found, or a complete listing."""
    if not path:
        return False
    return _ff8_failed_notfound(path, ev) or _ff8_listing_absent(path, ev)




def _existing(known, listing):
    if not _c8("flow8_absence_proof"):
        return _existing_s1(known, listing)
    msgs = getattr(_ff8_tls, "messages", None)
    if not msgs:
        return list(known)
    ev = _answered_calls(msgs)
    return [f for f in known
            if not f.lower().endswith(_SRC_EXT) or not _ff8_listing_absent(f, ev)]




def _plan(messages, tm, ctx):
    _ff8_tls.messages = messages
    try:
        return _plan_s1(messages, tm, ctx)
    finally:
        _ff8_tls.messages = None


_ff8_prev_plan_create = _plan_create_s3


def _plan_create(ctx, ev, tm):
    """CREATE must not overwrite a file it never checked: before the guided
    write step, read the target. 'not found' is the proof that it is new."""
    step = _ff8_prev_plan_create(ctx, ev, tm)
    try:
        if (not _c8("flow8_absence_proof") or step is None or step.name != "act"
                or getattr(ctx, "create_merge", False)):
            return step
        target = _norm(getattr(ctx, "target", "") or "")
        if not target or not tm.first("read"):
            return step
        seen = [e for e in ev if e["kind"] == "read" and _call_path(e["args"])
                and T._path_seen(target, [_call_path(e["args"])])]
        if any(_ff5_status(e) in ("success", "partial") for e in seen):
            step.instruction = ((step.instruction or "") +
                                " NOTE: `%s` ALREADY EXISTS (you read it above). Do not "
                                "overwrite it blindly: use edit for a partial change, or "
                                "write the COMPLETE new content." % target)
            return step
        if seen or _ff8_proven_absent(target, ev):
            return step
        return _read_step(
            "CREATE", tm, [target], 2, 5,
            instruction="Check whether `%s` already exists before writing it: read it. "
                        "A 'not found' result means it is safe to create." % target)
    except Exception as e:
        log("[fix8] create probe error: %s" % e)
        return step


# --------------------------------------------------------------------------
# 2 + 3. Post-filter: re-gate writes that skipped the guards, and refuse to
#        overwrite an unverified existing file for CREATE
# --------------------------------------------------------------------------

def _ff8_write_check(result, messages, ctx):
    """(new_result, problems). Never raises."""
    text, calls = result
    ev = _answered_calls(messages)
    touched = [p for p in (_call_path(e["args"]) for e in ev
                           if e["kind"] == "act" and _act_ok(e)) if p]
    pattern = getattr(ctx, "pattern", None) if ctx is not None else None
    probs, new = [], []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            new.append(tc)
            continue
        args = T._tool_arguments_dict(fn)
        path = _call_path(args)
        if not path or _base(path) == _MERGE_SCRIPT:
            new.append(tc)
            continue
        # (a) a second write into a file this turn already edited used to skip
        #     _gate_act_calls entirely once ANY fresh evidence existed.
        if any(T._path_seen(path, [x]) for x in touched):
            fixed, pr = _gate_act_calls([tc], messages)
            if pr:
                tracked_now = _ff9_read_texts(messages) if "_ff9_read_texts" in globals() else {}
                tk = _ff17_find_key(tracked_now, path) if "_ff17_find_key" in globals() else None
                is_tracked = (tk is not None) or any(T._path_seen(path, [k]) for k in tracked_now)
                tests_ran = _tests_ran_after_act(ev) if "_tests_ran_after_act" in globals() else False
                stale_re_read = not is_tracked or (tests_ran and len(tracked_now.get(tk or path) or "") <= 50000)
                if stale_re_read:
                    probs.extend(pr + ["%s: the file changed after your last read; "
                                       "re-read it before editing again" % path])
                else:
                    probs.extend(pr)
                continue
            new.append(fixed[0])
            continue
        # (b) CREATE writing a whole file that was never checked for existence
        content_only = not any(isinstance(args.get(k), str) and args[k]
                               for k in _OLD_KEYS)
        if (pattern == "CREATE" and content_only and _c8("flow8_absence_proof")):
            good = any(e["kind"] == "read" and _ff5_status(e) in ("success", "partial")
                       and _call_path(e["args"]) and T._path_seen(path, [_call_path(e["args"])])
                       for e in ev)
            if not good and not _ff8_proven_absent(path, ev):
                probs.append("%s: the file may already exist and was never read -- read "
                             "it first ('not found' proves it is new) before writing it" % path)
                continue
        new.append(tc)
    return (text, new), probs




def _ff5_postfilter_s2(result, messages, perm=None, last=True, tm=None, ctx=None):
    result, nudge = _ff5_postfilter_s1(result, messages, perm, last, tm, ctx)
    if nudge is not None or not _c8("flow8_write_regate"):
        return result, nudge
    if not (isinstance(result, tuple) and len(result) == 2 and result[1]):
        return result, nudge
    try:
        if ctx is None:
            ctx = _analyze(messages)
        new_result, probs = _ff8_write_check(result, messages, ctx)
    except Exception as e:
        log("[fix8] write re-gate error: %s" % e)
        return result, nudge
    if not probs:
        return new_result, None
    log("[fix8] blocked write(s): " + " | ".join(probs))
    body = "\n".join("- " + x for x in probs[:5])
    if not last:
        return None, ("Your file write was rejected. Fix exactly these problems "
                      "(re-read the file first if it changed):\n" + body)
    vi = bool(_VI_RE.search(_ff5_last_user_text(messages).lower()))
    msg = (("Mình chặn thao tác ghi file vì chưa qua kiểm tra an toàn (chưa có gì được ghi):\n"
            if vi else
            "I blocked the file write because it failed the safety checks (nothing was written):\n")
           + body)
    text = result[0]
    return (((text + "\n\n") if text else "") + msg, None), None


# --------------------------------------------------------------------------
# 5. Lean prompt must keep the source that edits/quotes depend on
# --------------------------------------------------------------------------

_FF8_SRC_STAGES = ("edit", "answer", "analyze")


def _ff8_file_views(ev):
    """Latest view of every file read this turn (continuations are joined)."""
    views, order, last_act = {}, [], {}
    for i, e in enumerate(ev):
        p = _call_path(e.get("args") or {})
        if not p:
            continue
        key = _norm(p).lower()
        if e["kind"] == "act" and _ff5_status(e) == "success":
            last_act[key] = i
        elif e["kind"] == "read" and _ff5_status(e) in ("success", "partial"):
            body = _strip_read_envelope(_ff5_trunc_re().sub("", e.get("result") or "").rstrip())
            off = _ff4_off(e["args"]) if "_ff4_off" in globals() else 0
            v = views.get(key)
            if v is None or off <= 0:
                views[key] = {"path": _norm(p), "text": body, "off": max(off, 0), "idx": i}
            else:
                v["text"] += "\n" + body
                v["idx"] = i
            if key in order:
                order.remove(key)
            order.append(key)
    out = []
    for key in order:
        v = views[key]
        v["edited"] = last_act.get(key, -1) > v["idx"]
        out.append(v)
    return out


def _ff8_render_view_s1(v, kws, locs, budget):
    lines = v["text"].split("\n")
    n = len(lines)
    non_empty_lns = [ln for ln in lines if ln.strip() and not _READ_HEADER_RE.match(ln) and not _READ_FOOTER_RE.match(ln)]
    numbered = T._already_numbered(lines) or (
        bool(non_empty_lns) and all(re.match(r"^\s*\d+\s*(?:\||:|\t|\u2192)\s", ln) for ln in non_empty_lns)
    )
    start = v["off"] + 1 if v["off"] > 0 else 1
    nums = []
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*(\d+)\s*(?:\||:|\t|\u2192)", ln) if numbered else None
        nums.append(int(m.group(1)) if m else start + i)
    width = max(4, len(str(nums[-1] if nums else n)))

    def fmt(i):
        return lines[i] if numbered else "%*d | %s" % (width, nums[i], lines[i])

    head = "--- Start %s (%d lines%s) ---" % (
        v["path"], n,
        "; EDITED after this read: re-read before editing again" if v.get("edited") else "")
    tail = "--- End %s ---" % v["path"]
    full = "\n".join(fmt(i) for i in range(n))
    if len(head) + len(full) + len(tail) + 2 <= budget:
        return head + "\n" + full + "\n" + tail
    keep = set(range(min(30, n)))
    low = [ln.lower() for ln in lines]
    for i, ln in enumerate(low):
        if any(k in ln for k in kws):
            keep.update(range(max(0, i - 12), min(n, i + 13)))
    by_num = {num: i for i, num in enumerate(nums)}
    for num in locs:
        i = by_num.get(num)
        if i is not None:
            keep.update(range(max(0, i - 15), min(n, i + 16)))
    out, used, prev = [head], len(head) + len(tail) + 2, -1
    for i in sorted(keep):
        if prev >= 0 and i > prev + 1:
            gap = "   ... (%d lines omitted)" % (i - prev - 1)
            out.append(gap)
            used += len(gap) + 1
        row = fmt(i)
        if used + len(row) + 1 > budget:
            out.append("   ... [cut for budget]")
            prev = n
            break
        out.append(row)
        used += len(row) + 1
        prev = i
    if 0 <= prev < n - 1:
        out.append("   ... (%d lines omitted)" % (n - 1 - prev))
    out.append(tail)
    return "\n".join(out)


def _ff8_source_block_s1(ctx, stage, messages):
    ev = _answered_calls(messages)
    views = _ff8_file_views(ev)
    if not views:
        return ""
    budget = int(_c8("flow8_lean_source_chars"))
    perm = getattr(ctx, "perm", None)
    named = list(getattr(perm, "scope", None) or [])
    try:
        named += list(_named_files(ctx))
    except Exception:
        pass
    try:
        kws = [k for k in _keywords(getattr(ctx, "request", "") or "") if len(k) >= 3]
    except Exception:
        kws = []
    locs = []
    try:
        for r in reversed([x for x in ev if x["kind"] == "exec"]):
            if _ff5_status(r) == "failed":
                locs = [(p, ln) for p, ln in failure_locations(r["result"], []) if ln]
                break
    except Exception:
        locs = []

    def loc_lines(v):
        return [ln for p, ln in locs if T._path_seen(v["path"], [_norm(p)])]

    def tier(v):
        if any(T._path_seen(v["path"], [n]) for n in named):
            return 0
        if loc_lines(v):
            return 1
        return 3 if _is_doc(v["path"]) else 2

    ranked = sorted(views, key=lambda v: (tier(v), -v["idx"]))
    remaining, blocks, omitted = budget, [], []
    for v in ranked:
        if remaining < 600:
            omitted.append(v["path"])
            continue
        b = _ff8_render_view(v, kws, loc_lines(v), remaining)
        blocks.append(b)
        remaining -= len(b) + 1
    if stage == "edit":
        head = ("Source (code you read this turn). The left column is a LINE NUMBER, not "
                "part of the code: copy oldString exactly WITHOUT the 'NNNN | ' prefix.")
    else:
        head = ("Source (code you read this turn). Quote lines exactly and cite "
                "file:line; the left column is a line number, not code.")
    out = [head] + blocks
    if omitted:
        out.append("Not shown here (over the source budget; re-read if needed): "
                   + ", ".join(omitted[:12]))
    return "\n".join(out)




def _build_step_prompt_core(ctx, step, messages, tm):
    base = _build_step_prompt_core_s1(ctx, step, messages, tm)
    if base is None or not _c8("flow8_lean_source"):
        return base
    stage = getattr(step, "stage", None) or _ff5_stage(step)
    if stage not in _FF8_SRC_STAGES:
        return base
    try:
        block = _ff8_source_block(ctx, stage, messages)
    except Exception as e:
        log("[fix8] lean source block error: %s" % e)
        return base
    if not block:
        return base
    _ff5_metric("lean_source_chars", len(block))
    return base + block + "\n"


# --------------------------------------------------------------------------
# 6. gemini.bard_error_code matched the model's ANSWER, not just the upstream
#    rejection. The old regex was `BardErrorInfo[^0-9]*...(\d+)`: any reply that
#    merely mentions the name (a review of gemini.py, a quoted test, ...) followed
#    -- at any distance, newlines included -- by a digit raised GeminiError, on
#    both the buffered (extract_response_text) and the streaming path.
# --------------------------------------------------------------------------

# Real protobuf-JSON rejection: a plain quote straight after the name. A copy of
# that text inside an answer is JSON-escaped (\" ) and therefore never matches.
_FF8_BARD_PB_RE = re.compile(r'BardErrorInfo",\s*\[\s*(\d+)\s*[\],]')
# Legacy prose form; only trusted on a SHORT line that carries no answer text.
_FF8_BARD_PROSE_RE = re.compile(r"BardErrorInfo\s*\[\s*(\d+)\s*\]")


def _ff8_bard_error_code(raw):
    if not isinstance(raw, str) or "BardErrorInfo" not in raw:
        return None
    from gemini_web2api import gemini as _g
    for line in raw.split("\n"):
        if "BardErrorInfo" not in line:
            continue
        m = _FF8_BARD_PB_RE.search(line)
        if m:
            return int(m.group(1))
        if len(line) >= 200 or _g._extract_texts_from_line(line):
            continue          # an answer line (or a partial one): not a rejection
        m = _FF8_BARD_PROSE_RE.search(line)
        if m:
            return int(m.group(1))
    return None


def _ff8_patch_gemini():
    """Swap gemini.bard_error_code; raise_bard_error / extract_response_text /
    generate_stream all resolve it from the module globals at call time."""
    if not _c8("flow8_bard_strict"):
        return
    from gemini_web2api import gemini as _g
    if getattr(_g.bard_error_code, "_ff8", False):
        return
    _ff8_bard_error_code._ff8 = True
    _g.bard_error_code = _ff8_bard_error_code
    log("[fix8] gemini.bard_error_code: answer text no longer counts as a rejection")


def _setup_ff8():
    for k, v in _FF8_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    _ff8_patch_gemini()


# --------------------------------------------------------------------------
# 7. Destructive-command filter: keep the old patterns, add the forms that
#    slipped through (rm -fr / -r -f / --recursive, git clean --force, checkout -f,
#    push --force, branch -D, find -delete, del /s, git -C dir clean ...)
# --------------------------------------------------------------------------

_FF8_DESTRUCTIVE_RE = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+|-\S+\s+)*(?:"
    r"clean\b(?=[^\n;&|]*(?:\s-\w*[fdx]\b|\s--force\b))"
    r"|reset\b[^\n;&|]*--hard\b"
    r"|checkout\s+(?:-f|--force)\b"
    r"|checkout\s+(?:-\w+\s+)*(?:--\s+)?[.*](?:\s|$)"
    r"|restore\b[^\n;&|]*\s[.*](?:\s|$)"
    r"|stash\s+(?:drop|clear)\b"
    r"|push\b[^\n;&|]*(?:\s-f\b|\s--force\b|\s--force-with-lease\b)"
    r"|branch\s+(?:-\S+\s+)*(?-i:-D)\b"
    r")"
    r"|\brm\s+(?:-\S+\s+)*-(?:\w*[rR]\w*|-recursive)\b"
    r"|\bfind\b[^\n;&|]*\s-delete\b"
    r"|\b(?:del|erase)\s+(?:/\w+\s+)*/[sq]\b"
    r"|\b(?:rmdir|rd)\s+(?:/\w+\s+)*/s\b"
    r"|\bformat\s+[a-z]:|\bmkfs\b|\bshred\b|\bdd\s+[^\n]*\bof=",
    re.I)

if _c8("flow8_destructive_wide") and not getattr(_FF5_DESTRUCTIVE_RE, "_ff8", False):
    _ff8_old_destructive = _FF5_DESTRUCTIVE_RE
    _FF5_DESTRUCTIVE_RE = re.compile(
        _ff8_old_destructive.pattern + "|" + _FF8_DESTRUCTIVE_RE.pattern, re.I)


# --------------------------------------------------------------------------
# 8. REVERT must obey the edit permission. Its file deletions are shell calls
#    (rm / Remove-Item, kind=exec), so perm.run="any" let them through even when
#    the user said "do not edit" or only asked HOW to undo something.
# --------------------------------------------------------------------------



def _analyze_s4(messages):
    ctx = _analyze_s3(messages)
    try:
        if (ctx is not None and _c8("flow8_revert_needs_edit")
                and getattr(ctx, "pattern", None) == "REVERT"
                and getattr(ctx, "perm", None) is not None
                and not ctx.perm.edit):
            log("[fix8] REVERT without edit permission (%s) -> EXPLAIN" % ctx.perm.reason)
            ctx.pattern = "EXPLAIN"
            ctx.fix = False
            perm = _ff5_permissions(messages, "EXPLAIN", False)
            perm.scope = _ff5_scope(ctx, messages)
            ctx.perm = perm
            ctx.no_edit = True
            ctx.intent = "explain"
    except Exception as e:
        log("[fix8] revert permission error: %s" % e)
    return ctx




def _ff5_own_result_s2(step, ctx, messages, tm, perm):
    """Second line of defence: a REVERT step without edit rights may only run
    read-only commands (the git status probes)."""
    try:
        if (_c8("flow8_revert_needs_edit") and step is not None
                and getattr(ctx, "pattern", None) == "REVERT"
                and perm is not None and not perm.edit and step.calls):
            for tc in step.calls:
                fn = tc.get("function") or {}
                kind = tool_kind(fn.get("name"))
                cmd = _call_command(T._tool_arguments_dict(fn))
                if kind == "act" or (kind == "exec" and not _ff5_cmd_readonly(cmd)):
                    return _ff5_say(
                        ctx,
                        "Bạn không cho phép sửa file nên mình không hoàn tác hay xoá gì cả.",
                        "Edits are not allowed here, so I reverted and deleted nothing.").text, None
    except Exception as e:
        log("[fix8] own-result guard error: %s" % e)
    return _ff5_own_result_s1(step, ctx, messages, tm, perm)
# ==== flowfix8:end ====

# ==== flowfix9:begin (generated by flow_fix9_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: callers resolve these names at call time).


# 1. flowfix8 permissions rule (unified into _ff5_permissions above)


# 2. "do not run X" / "dung chay X" must not become a run-type pattern.
#    Note: folding turns both "dung" (do not) and "dung" (use) into "dung", so
#    that word only counts when it sits right before the run verb.
_FF9_RUN_PATTERNS = ("RUN_COMMAND", "RUN_TESTS", "GIT_INFO")
_FF9_NEG_RUN_RE = re.compile(
    r"\b(?:do\s+not|don'?t|dont|never|without|khong|chua|khoan|cam)\s+"
    r"(?:\w+\s+){0,2}?"
    r"(?:run|running|execute|executing|exec|chay|thuc\s+thi)\b"
    r"|\bdung\s+(?:co\s+|hay\s+)?(?:chay|thuc\s+thi)\b")


def _ff9_run_negated(ctx):
    folded = " ".join(str(getattr(ctx, "folded", "") or "").lower().split())
    return bool(_FF9_NEG_RUN_RE.search(folded))


def _ff5_detect_s2(ctx):
    det = _ff5_detect_s1(ctx)
    if det in _FF9_RUN_PATTERNS and _ff9_run_negated(ctx):
        log("[fix9] run request is negated by the user -> not a %s request" % det)
        ctx.cmd = ""
        ctx.git_cmd = ""
        return None
    return det


def _ff5_apply_label_s2(ctx, label):
    if label in _FF9_RUN_PATTERNS and _ff9_run_negated(ctx):
        log("[fix9] classifier label %s ignored: the user negated running" % label)
        return
    return _ff5_apply_label_s1(ctx, label)

# 3. Edit gate: join continued reads, forget text that an edit made stale.


def _base_gate_act_calls_s1(calls, messages):
    texts = _ff9_read_texts(messages)
    problems, out = [], []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            out.append(tc)
            continue
        args = T._tool_arguments_dict(fn)
        path = _call_path(args)
        okey = next((k for k in _OLD_KEYS
                     if isinstance(args.get(k), str) and args[k]), None)
        src_text = next((t for p, t in texts.items()
                         if path and T._path_seen(path, [p])), None)
        if not (okey and src_text is not None):
            out.append(tc)           # write / new file / unread or edited file
            continue
        old = args[okey].replace("\r\n", "\n")
        hay = src_text.replace("\r\n", "\n")
        if old in hay or old.strip("\n") in hay:
            out.append(tc)
            continue
        stripped = _strip_linenos(old)
        if stripped != old and (stripped in hay or stripped.strip("\n") in hay):
            args[okey] = stripped
            for nk in _NEW_KEYS:
                if isinstance(args.get(nk), str):
                    args[nk] = _strip_linenos(args[nk])
            log(f"[flow] edit gate: stripped line-number prefixes in {path}")
            out.append({**tc, "function": {
                **fn, "arguments": json.dumps(args, ensure_ascii=False)}})
            continue
        first = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
        cands = [ln.strip() for ln in hay.split("\n") if ln.strip()]
        near = difflib.get_close_matches(first, cands, n=1, cutoff=0.6)
        problems.append(f"{path}: oldString does not appear in the file as read"
                        + (f" (closest real line: {near[0]!r})" if near else ""))
        out.append(tc)
    return out, problems


# 4. A failed read that a later read of the same file superseded is not evidence
#    against editing that file.
def _ff9_drop_superseded(messages):
    try:
        start = T._last_user_index(messages)
        calls, events = {}, []
        for i, msg in enumerate(messages):
            if i <= start or not isinstance(msg, dict):
                continue
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        fn = tc.get("function") or {}
                        calls[tc["id"]] = (fn.get("name") or "",
                                           T._tool_arguments_dict(fn))
            elif msg.get("role") == "tool":
                hit = calls.get(msg.get("tool_call_id"))
                if hit and tool_kind(hit[0]) == "read" and _call_path(hit[1]):
                    ev = {"kind": "read", "args": hit[1],
                          "result": T._stringify_content(msg.get("content"))}
                    events.append((i, _call_path(hit[1]), _ff5_status(ev)))
        drop = set()
        for n, (i, p, st) in enumerate(events):
            if st == "failed" and any(
                    s in ("success", "partial") and _ff5_same_path(p, q)
                    for _j, q, s in events[n + 1:]):
                drop.add(i)
        if not drop:
            return messages
        return [m for i, m in enumerate(messages) if i not in drop]
    except Exception as e:
        log(f"[fix9] superseded-read filter error: {e}")
        return messages




def _ff5_read_guard(ctx, messages):
    return _ff5_read_guard_s2(ctx, _ff9_drop_superseded(messages))


def _ff5_evidence_gate_s3(ctx, messages, tm):
    return _ff5_evidence_gate_s2(ctx, _ff9_drop_superseded(messages), tm)


# 5. A bare file name may live in a sub-folder: list the project before reading.


def _ff9_bare_unread(ctx, ev):
    out = []
    for f in _named_files(ctx):
        n = _norm(f)
        if "/" in n or re.match(r"^[A-Za-z]:", n) or not n.lower().endswith(_SRC_EXT):
            continue
        done = any(e["kind"] == "read" and _ff5_status(e) in ("success", "partial")
                   and _call_path(e["args"]) and T._path_seen(n, [_call_path(e["args"])])
                   for e in ev)
        if not done:
            out.append(n)
    return out


def _plan_change_s2(ctx, ev, tm, pat="CHANGE", total=5):
    try:
        if pat != "CREATE" and not _surveyed(ev) and _ff9_bare_unread(ctx, ev):
            step = _survey_step(pat, tm, ev, 1, total)
            if step is not None:
                return step
    except Exception as e:
        log(f"[fix9] bare-name survey error: {e}")
    return _plan_change_s1(ctx, ev, tm, pat=pat, total=total)


# 6. After an edit the read copy is stale: re-read before the next edit.
def _ff9_stale_edits_s1(ev):
    last = {}
    for i, e in enumerate(ev):
        if e["kind"] == "act" and _act_ok(e):
            p = _call_path(e["args"])
            if p and _base(p) != _MERGE_SCRIPT:
                last[_norm(p)] = i
    stale = []
    for p, i in last.items():
        later = [x for x in ev[i + 1:] if x["kind"] == "read"
                 and _call_path(x["args"]) and T._path_seen(p, [_call_path(x["args"])])]
        if any(_ff5_status(x) in ("success", "partial") for x in later):
            continue
        if len(later) >= 2:          # re-read keeps failing: do not loop
            continue
        stale.append(p)
    return stale




def _change_tail_s2(pat, ctx, ev, tm, names_all, total, extra=""):
    step = _change_tail_s1(pat, ctx, ev, tm, names_all, total, extra)
    try:
        if step is not None and step.name == "act" and tm.first("read"):
            stale = [p for p in _ff9_stale_edits(ev)
                     if p.lower().endswith(_SRC_EXT)][:4]
            if stale:
                return _read_step(
                    pat, tm, stale, 2, total,
                    instruction="Your last edit changed these files, so your copy is "
                                "stale. Re-read them before the next edit: "
                                + ", ".join(stale) + ".")
    except Exception as e:
        log(f"[fix9] stale-edit planner error: {e}")
    return step


# 7. More truncation markers (unified into _Ff9Trunc above).
# ==== flowfix9:end ====

# ==== flowfix10:begin (ambiguity-aware pattern routing) ====
# Problem: detect_task_pattern() commits the first regex match. A weak review
# verb ("tim", "find", "doc", "check") plus a code noun ("source", "code") is
# enough to force the GENERIC_REVIEW flow even when those tokens sit in
# different clauses and the user only asked to understand the code:
#     "giai thich phan xu ly tim pattern flow ... source dang lam the nao"
# This layer keeps the regex as the cheap filter, demotes a weak review match
# inside an explain/understand question, and lets a model tie-break only the
# genuinely ambiguous cases. OTHER = no forced flow (free run); AMBIGUOUS =
# ask the client to choose through the existing CLARIFY_MARK path.
import hashlib as _ff10_hashlib



_c10 = _flow_c


# Previous definitions, captured before this section overrides them. The file
# is a stack of patch layers: later definitions win, so every wrapper keeps a
# handle on the layer below it.


_FF10_EXPLAIN_RE = re.compile(
    r"\b(?:giai\s+thich|tim\s+hieu|explain|describe|summari[sz]e|how|why|what|"
    r"where|which|hoat\s+dong|nhu\s+the\s+nao|ra\s+sao|la\s+gi|tai\s+sao|o\s+dau|"
    r"walk\s+me\s+through|tell\s+me)\b",
    re.IGNORECASE,
)
_FF10_DEFECT_RE = re.compile(
    r"\b(?:bug|bugs|error|errors|issue|issues|problem|problems|vulnerab\w*|"
    r"security|correctness|wrong|incorrect|insecure|unsafe|risk|risks|loi|"
    r"van\s+de|lo\s+hong|sai|hong|fail\w*|crash\w*|broken|leak\w*|"
    r"deadlock|overflow|regression\w*)\b",
    re.IGNORECASE,
)
_FF10_REVIEW_LABELS = ("GENERIC_REVIEW", "FILE_REVIEW")
_FF10_OTHER_LABELS = ("EXPLAIN", "CHANGE", "TOOL_DEMAND")


_ff10_scrub = _ff4_scrub


def _is_source_review(folded):
    f = _ff10_scrub(folded)
    if not _is_source_review_s1(f):
        return False
    if _SOURCE_STRONG_VERB_RE.search(f):
        return True
    # "tim hieu source", "doc source roi giai thich", "find how ... works":
    # a weak review verb inside an explain question is not an audit request.
    if _FF10_EXPLAIN_RE.search(f) and not _FF10_DEFECT_RE.search(f):
        return False
    return True


def _is_file_review(folded, scannable):
    f = _ff10_scrub(folded)
    if not _is_file_review_s1(f, scannable):
        return False
    if _SOURCE_STRONG_VERB_RE.search(f):
        return True
    if _FF10_EXPLAIN_RE.search(f) and not _FF10_DEFECT_RE.search(f):
        return False
    return True


# detect_task_pattern()/review_demanded()/pending_tool_request() resolve these
# names from this module, so assigning them here changes the deterministic gate
# too. T is this same module (the flat bundle aliases every submodule).
_ff10_is_source_review = _is_source_review
_ff10_is_file_review = _is_file_review
_ff10_orig_source_review = _is_source_review_s1
_ff10_orig_file_review = _is_file_review_s1
T._is_source_review = _is_source_review
T._is_file_review = _is_file_review


def _ff10_last_user(messages):
    return _ff5_last_user_text(messages) or None


def _ff10_message_text(messages):
    last = _ff10_last_user(messages)
    if not last:
        return "", "", ""
    scannable = T._strip_code_spans(last)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable)).strip()
    return last, scannable, folded


def _ff10_candidates(messages):
    """Weighted intent candidates for the newest user turn.

    Deterministic routing still owns the final answer; this list only tells the
    tie-breaker which labels are plausible. Review labels carry ``weak=True``
    when they matched without a strong verb -- those are the only labels the
    model is allowed to overrule.
    """
    last, scannable, folded = _ff10_message_text(messages)
    if not last:
        return []
    scrubbed = _ff10_scrub(folded)
    raw_file = _is_file_review_s1(scrubbed, scannable)
    raw_source = not raw_file and _is_source_review_s1(scrubbed)
    strong = bool(_SOURCE_STRONG_VERB_RE.search(folded))
    explain = bool(_FF10_EXPLAIN_RE.search(folded)) or bool(
        _is_code_question(last))
    change = _is_change_request(folded, scannable, last)
    out = []
    if raw_file:
        out.append({"label": "FILE_REVIEW", "score": 3 if strong else 1,
                    "weak": not strong})
    elif raw_source:
        out.append({"label": "GENERIC_REVIEW", "score": 3 if strong else 1,
                    "weak": not strong})
    if explain:
        out.append({"label": "EXPLAIN", "score": 2, "weak": False})
    if change:
        out.append({"label": "CHANGE", "score": 2, "weak": False})
    if _TOOL_DEMAND_RE.search(folded):
        out.append({"label": "TOOL_DEMAND", "score": 1, "weak": False})
    return out


def _ff10_ambiguous(cands):
    labels = {c["label"] for c in cands}
    if len(labels) < 2:
        return False
    weak_review = any(c["weak"] for c in cands
                      if c["label"] in _FF10_REVIEW_LABELS)
    if not weak_review:
        return False
    return bool(labels & set(_FF10_OTHER_LABELS))


_ff10_cache = {}


def _ff10_classify_prompt(text, labels):
    lines = [
        "Classify one coding-agent request. A deterministic classifier is "
        "unsure between these labels:",
    ]
    for k in labels:
        if k in _FF5_CATALOG:
            lines.append("- %s: %s" % (k, _FF5_CATALOG[k]))
    lines += [
        "- OTHER: none of these labels fits; let the agent use tools freely.",
        "- AMBIGUOUS: two labels genuinely fit and only the user can choose.",
        "",
        "Rules:",
        "- Choose EXPLAIN when the user asks to understand or explain how code works.",
        "- Choose GENERIC_REVIEW only when the user asks to audit or review for bugs.",
        "- Never infer a code change unless the user asked for one.",
        "",
        "Request:",
        (text or "").strip()[:600],
        "",
        "Answer with ONE label only, nothing else.",
    ]
    return T.defuse_extension_words("\n".join(lines))


def _ff10_classify_s1(text, labels):
    text = (text or "").strip()
    labels = [x for x in labels if x]
    if len(text) < 8 or not labels:
        return None
    key = _ff10_hashlib.sha1(
        (text + "\x00" + "|".join(labels)).encode("utf-8", "ignore")).hexdigest()
    if key in _ff10_cache:
        cached = _ff10_cache[key]
        if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == "_ERR_":
            if time.monotonic() < cached[1]:
                return None
            _ff10_cache.pop(key, None)
        else:
            return cached
    model = getattr(_ff5_tls, "model", None)
    if not model:
        return None
    label, answered = None, False
    try:
        m_id, m_think, m_extra, m_ticket = model
        raw = generate(_ff10_classify_prompt(text, labels), m_id, m_think,
                       None, m_extra, m_ticket)
        if raw and not T.looks_like_canned_failure(raw):
            answered = True
            for w in re.findall(r"[A-Z][A-Z_]{3,}", raw.upper()):
                if w in labels:
                    label = w
                    break
                if w in ("OTHER", "NONE"):
                    label = "OTHER"
                    break
                if w == "AMBIGUOUS":
                    label = "AMBIGUOUS"
                    break
        log("[flow10] model tie-break -> %s" % (label or "none"))
    except Exception as e:
        log("[flow10] model tie-break failed, keeping deterministic label: %s" % e)
        _ff10_cache[key] = ("_ERR_", time.monotonic() + 15.0)
    if answered:
        if len(_ff10_cache) > 300:
            _ff10_cache.clear()
        _ff10_cache[key] = label
    return label


def _ff10_apply_pattern(ctx, messages, label):
    if not label:
        return ctx
    if label in ("OTHER", "NONE"):
        ctx.pattern = None
        ctx.free_run = True
        ctx.ask_user = False
        perm = _ff5_permissions(messages, None, getattr(ctx, "fix", False))
        perm.scope = _ff5_scope(ctx, messages)
        ctx.perm = perm
        ctx.no_edit = not perm.edit
        ctx.intent = ""
        return ctx
    if label == "PLAN":
        ctx.plan = True
        label = "EXPLAIN"
    _ff5_apply_label(ctx, label)
    if ctx.pattern in ("CHANGE", "CREATE", "DEBUG"):
        ctx.fix = bool(_FIX_RE.search((ctx.folded or "").lower()))
    perm = _ff5_permissions(messages, ctx.pattern, getattr(ctx, "fix", False))
    if ctx.pattern in ("CHANGE", "CREATE") and not perm.edit:
        log("[flow10] %s -> EXPLAIN (%s)" % (ctx.pattern, perm.reason))
        ctx.pattern = "EXPLAIN"
        ctx.fix = False
        perm = _ff5_permissions(messages, ctx.pattern, False)
    if ctx.pattern == "DEBUG" and ctx.fix and not perm.edit:
        ctx.fix = False
        perm = _ff5_permissions(messages, ctx.pattern, False)
    perm.scope = _ff5_scope(ctx, messages)
    ctx.perm = perm
    ctx.no_edit = not perm.edit
    ctx.intent = (ctx.pattern or "").lower()
    ctx.free_run = False
    ctx.ask_user = False
    return ctx


def _ff10_tiebreak(ctx, messages, cands):
    if not _c10("flow_tiebreak") or not _c5("flow5_classify"):
        return ctx
    weak = next((c for c in cands if c["weak"]
                 and c["label"] in _FF10_REVIEW_LABELS), None)
    if weak is None:
        return ctx
    labels = [weak["label"]]
    for c in cands:
        if c["label"] in _FF10_OTHER_LABELS and c["label"] not in labels:
            labels.append(c["label"])
    labels = labels[:3]
    label = _ff10_classify(getattr(ctx, "last", ""), labels)
    if label == "AMBIGUOUS":
        if _c10("flow_ask_on_ambiguity"):
            ctx.ask_user = True
            log("[flow10] model says AMBIGUOUS -> asking the client")
        return ctx
    if label in ("OTHER", "NONE"):
        log("[flow10] model says OTHER -> free run (no forced flow)")
        return _ff10_apply_pattern(ctx, messages, "OTHER")
    if label:
        ctx.model_choice = label
        return _ff10_apply_pattern(ctx, messages, label)
    log("[flow10] tie-break unavailable -> keeping deterministic pattern %s"
        % (getattr(ctx, "pattern", None) or "NONE"))
    return ctx


def _analyze_s5(messages):
    ctx = _analyze_s4(messages)
    if ctx is None:
        return ctx
    try:
        cands = _ff10_candidates(messages)
        ctx.candidates = [c["label"] for c in cands]
        ctx.ambiguous = False
        if not _c10("flow_tiebreak") or not _ff10_ambiguous(cands):
            return ctx
        # High-confidence labels (CHANGE/TOOL_DEMAND/RUN_*/...) are not
        # reviewed by the model: overriding them could grant edit rights.
        if getattr(ctx, "pattern", None) not in (
                None, "GENERIC_REVIEW", "FILE_REVIEW", "EXPLAIN"):
            return ctx
        if getattr(ctx, "pattern", None) is None:
            # The earlier _analyze already ran the full classifier for this
            # shape; a second call would only add latency.
            return ctx
        if _clarified(messages, getattr(ctx, "idx", -1)):
            return ctx
        if _answered_calls(messages):
            return ctx
        ctx.ambiguous = True
        log("[flow10] candidates=" + ", ".join(
            "%s:%s" % (c["label"], c["score"]) for c in cands))
        return _ff10_tiebreak(ctx, messages, cands)
    except Exception as e:
        log("[flow10] tie-break error: %s" % e)
        return ctx


def _ff10_clarify_text(text):
    if _VI_RE.search((text or "").lower()):
        return ("Mình thấy yêu cầu này có thể hiểu theo nhiều hướng. Bạn chọn giúp:\n"
                "1. Review / audit source\n2. Implement / sửa code\n"
                "3. Giải thích / tìm hiểu code\n4. Chạy một lệnh / tool cụ thể\n"
                f"Trả lời 1-4 hoặc mô tả rõ hơn. {CLARIFY_MARK}")
    return ("I can read this request more than one way. Please pick:\n"
            "1. Review / audit the source\n2. Implement / change code\n"
            "3. Explain / understand the code\n4. Run a specific command/tool\n"
            f"Reply 1-4 or describe it more precisely. {CLARIFY_MARK}")


def clarification_text(messages):
    try:
        last = messages[-1] if messages else None
        if not (isinstance(last, dict) and last.get("role") == "user"):
            return None
    except Exception:
        return None
    if _c10("flow_ask_on_ambiguity") and messages:
        try:
            ctx = _analyze(messages)
            if ctx is not None and getattr(ctx, "ask_user", False):
                log("[flow10] ambiguous request -> asking the client to pick a pattern")
                return _ff10_clarify_text(getattr(ctx, "last", ""))
        except Exception as e:
            log("[flow10] clarification error: %s" % e)
    return clarification_text_s1(messages)


def _ff5_spec_banner(messages):
    try:
        ctx = _analyze(messages)
        if ctx is not None and getattr(ctx, "free_run", False):
            return ""
    except Exception as e:
        log("[flow10] banner guard error: %s" % e)
    return _ff5_spec_banner_s1(messages)


def _setup_ff10():
    for k, v in _FF10_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    try:
        from gemini_web2api import gemini, multimodal, server
        for target in (gemini, multimodal, server):
            if hasattr(target, "MODELS"):
                target.MODELS = MODELS
    except Exception as ex:
        log("[fix10] models sync warning: %s" % ex)


# ==== flowfix10:end ====


# ==============================================================================
# Section: server.py
# ==============================================================================

"""HTTP server: OpenAI-compatible API endpoints."""
import json
import time
import uuid
import re
import hmac
import ipaddress
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from gemini_web2api.config import CONFIG
from gemini_web2api.models import MODELS, resolve_model, ticket_for
from gemini_web2api.gemini import (
    generate,
    generate_stream,
    log_s1 as log,
    GeminiError,
    trace,
    update_cookie_payload,
)
from gemini_web2api.tools import (
    looks_like_tool_call,
    messages_to_prompt,
    parse_tool_calls,
    google_contents_to_prompt,
    parse_google_function_calls,
    _canonical_args,
    _google_repeatable,
    _google_pseudo_messages,
    unverified_quotes,
    _repeatable_calls,
    build_response_format_instruction,
    strip_code_fence,
    tool_names,
    tool_parameters,
    tool_required_params,
    missing_required_params,
    is_required_tool_choice,
    looks_like_missed_tool_call,
    looks_like_upstream_error,
    looks_like_canned_failure,
    pending_tool_request,
    SOURCE_REVIEW_OWED,
    is_read_retry_reason,
    is_review_test_reason,
    is_test_retry_reason,
    listed_turn_files,
    unread_listed_core_files,
    fresh_declared_unread,
    review_demanded_s1 as review_demanded,
    change_demanded,
    looks_like_described_edit,
    _conversation_called_files,
    _turn_act_calls,
    _is_source_review,
    _is_file_review,
    _strip_code_spans,
    _fold_diacritics,
    _stringify_content,
)
from gemini_web2api.multimodal import detect_image_mime, fetch_image_bytes, upload_image
from gemini_web2api import __version__


ERR_INVALID_REQUEST = "invalid_request_error"
ERR_RATE_LIMIT = "rate_limit_error"
ERR_API = "api_error"


def _base_is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return host in ("localhost", "127.0.0.1", "::1")


def _is_loopback(host) -> bool:
    """True when the address is a loopback address or cannot be reached from another machine."""
    if not _flow_c("flow15_server_hardening", True):
        return _base_is_loopback(host)
    if not isinstance(host, str):
        return False
    v = host.strip().strip("[]").split("%")[0].lower()
    if not v:
        return False
    if v == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(v)
    except (ValueError, TypeError):
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    return bool(addr.is_loopback or (mapped is not None and mapped.is_loopback))



# Gap between SSE keep-alive comments while a buffered turn still works
# upstream. Must stay well under every client's idle/chunk timeout (OpenCode
# defaults to 300 s).
SSE_PING_INTERVAL_SEC = 5

_MISSING = object()


def _complete_call_end(text: str):
    """Char index just past the LAST closed, parseable tool-call block, else None.

    Uses the same primitives as the real parser (_FENCE_OPEN/_FENCE_CLOSE +
    JSONDecoder.raw_decode with strict=False), so early-exit fires exactly
    when the buffered prefix already holds a call ``parse_tool_calls`` would
    accept -- never on a stray triple-backtick. Conservative by design:
    * a payload still streaming fails raw_decode -> wait for more deltas;
    * a non-JSON body (Python-style ``read(path="x")``) is NOT counted --
      AST recovery happens later in parse_tool_calls, so such turns simply
      read to completion instead of risking a cut-off call;
    * prose ```json examples are skipped and scanning continues -- the real
      call may follow them.
    """
    from gemini_web2api.tools import _ARGUMENT_KEYS, _FENCE_OPEN, _FENCE_CLOSE
    dec = json.JSONDecoder(strict=False)
    last_end = None
    pos = 0
    while True:
        m = _FENCE_OPEN.search(text, pos)
        if not m:
            return last_end
        j = m.end()
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        kind = m.group(1)
        try:
            data, end = dec.raw_decode(text, j)
        except ValueError:
            # Body not (yet) valid JSON: keep any earlier closed block and
            # wait for more deltas rather than cutting mid-payload.
            return last_end
        counted = False
        if isinstance(data, dict) and data.get("name"):
            if kind != "json" or any(k in data for k in _ARGUMENT_KEYS):
                counted = True
        if not counted:
            # Prose example or non-call payload: skip it, keep scanning.
            c = _FENCE_CLOSE.match(text, end)
            pos = c.end() if c else max(end, m.end())
            continue
        c = _FENCE_CLOSE.match(text, end)
        if not c:
            return last_end          # closing fence still streaming
        last_end = c.end()
        pos = last_end


def _streamed_generate(call_prompt, model_id, think_mode, file_refs,
                       extra, ticket, tools_active, attempt_no, on_delta=None):
    """generate_stream() collapsed to one string, with [timing] + early exit.

    Used by the tool-retry loops when CONFIG["tool_stream"] is on. Three jobs:
      * log first-delta timing so operator and client can tell "upstream slow"
        from "proxy hung";
      * stop reading upstream as soon as a *complete, parseable* tool_call
        fence has arrived (real parser primitives, not a backtick heuristic);
      * hand every delta to ``on_delta`` so the caller can keep the client's
        connection warm (heartbeat/ping) during a long buffered turn.

    CONTRACT (tests/test_tool_stream.py depends on it): the generator is
    iterated DIRECTLY in this thread and abandoned via close() at the
    early-exit point. A background pump thread would pre-pull the TAIL delta
    into a queue before the break lands and silently violate "never read the
    tail" -- that exact bug produced the failing assertion
    "'TAIL SHOULD NOT BE READ' unexpectedly found in seen".
    """
    chunks = []
    ttfb_done = False
    t0 = time.monotonic()
    gen = generate_stream(call_prompt, model_id, think_mode,
                          file_refs, extra, ticket)
    try:
        for delta in gen:
            if not ttfb_done and delta:
                ttfb_done = True
                log(f"[timing] tool attempt {attempt_no + 1}: first delta in "
                    f"{time.monotonic() - t0:.1f}s")
            chunks.append(delta)
            if on_delta is not None:
                try:
                    on_delta(delta)
                except Exception as e:
                    log(f"tool attempt {attempt_no + 1}: delta sink failed: {e}")
            if tools_active:
                raw_so_far = "".join(chunks)
                cut = _complete_call_end(raw_so_far)
                if cut is not None and looks_like_tool_call(raw_so_far[:cut]):
                    log(f"[timing] tool attempt {attempt_no + 1}: complete "
                        f"tool_call fence after {time.monotonic() - t0:.1f}s, "
                        "early-exit read")
                    break
    finally:
        close = getattr(gen, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
    return "".join(chunks)


class _Heartbeat:
    """Delta sink for a buffered tool turn: upstream activity == keep-alive.

    ``feed(delta)`` is called by ``_streamed_generate`` for every streamed
    chunk of a tool-retry attempt. It writes one SSE comment immediately --
    clients see per-part activity instead of 20-60 s of silence -- and resets
    the interval timer so idle pings only fire when nothing arrives. The gone
    flag is shared with the caller: a failed write means nobody listens and
    further upstream attempts must be skipped. Thread-safe via ``_lock``
    because both the request thread (feed) and this thread (run) write.
    """

    def __init__(self, handler, stop_event, gone_event):
        self._handler = handler
        self._stop = stop_event
        self._gone = gone_event
        self._wake = threading.Event()
        self._lock = threading.Lock()

    def _write_ping(self):
        with self._lock:
            if self._stop.is_set():
                return False
            try:
                self._handler._sse_write(b": ping\n\n")
                return True
            except (BrokenPipeError, ConnectionResetError):
                self._gone.set()
                return False
            except Exception:
                return False

    def feed(self, delta=None):
        if self._stop.is_set() or self._gone.is_set():
            return
        self._write_ping()
        self._wake.set()          # restart the idle countdown

    def run(self):
        interval = SSE_PING_INTERVAL_SEC
        while not self._stop.is_set():
            if self._wake.wait(interval):
                self._wake.clear()
                continue          # real activity just happened: re-arm quietly
            if self._stop.is_set():
                return
            if not self._write_ping():
                return


def _estimate_tokens(text: str) -> int:
    return len(text or "") // 4


def _usage(prompt: str, text: str) -> dict:
    p = _estimate_tokens(prompt)
    c = _estimate_tokens(text)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _normalize_stop(stop) -> list:
    if stop is None:
        return []
    if isinstance(stop, str):
        return [stop] if stop else []
    if isinstance(stop, list):
        return [s for s in stop if isinstance(s, str) and s]
    return []


def _apply_stop(text: str, stops: list) -> str:
    if not text or not stops:
        return text
    cut = None
    for s in stops:
        idx = text.find(s)
        if idx != -1 and (cut is None or idx < cut):
            cut = idx
    return text[:cut] if cut is not None else text


def _apply_max_tokens(text: str, max_tokens) -> tuple:
    """Return (text, truncated) after applying an estimated token budget."""
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        return text, False
    limit = max_tokens * 4
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _responses_text_format(text_field) -> dict:
    """Map a Responses API ``text.format`` object to a Chat ``response_format``.

    The field name differs (``text.format`` vs ``response_format``) and the
    JSON schema sits one level further up:
    ``{"type":"json_schema","schema":{...}}`` instead of
    ``{"type":"json_schema","json_schema":{"schema":{...}}}``.
    """
    if not isinstance(text_field, dict):
        return None
    fmt = text_field.get("format")
    if not isinstance(fmt, dict):
        return None
    if fmt.get("type") == "json_object":
        return {"type": "json_object"}
    if fmt.get("type") == "json_schema":
        if isinstance(fmt.get("json_schema"), dict):
            return {"type": "json_schema", "json_schema": fmt["json_schema"]}
        return {"type": "json_schema", "json_schema": {"schema": fmt.get("schema", {})}}
    return None


def _map_upstream_error(e) -> tuple:
    """Map an upstream exception to (status, message, type, code).

    503 rather than 502: the spec declares 400/401/403/404/429/500/503 for
    ``/chat/completions`` and 400/404/429/503 for ``/responses``. A 502 is
    documented only for the audio endpoints, and a strict client that treats
    undeclared statuses as a protocol error would reject it. Upstream Gemini
    failing is exactly the "service unavailable" case 503 describes.
    """
    status = getattr(e, "status", None)
    if status is None:
        status = getattr(e, "code", None)
    if status is None:
        status = getattr(getattr(e, "response", None), "status_code", None)
    if status == 429:
        return 429, f"upstream rate limited: {e}", ERR_RATE_LIMIT, "rate_limit_exceeded"
    if status:
        return 503, f"upstream error ({status}): {e}", ERR_API, None
    return 503, f"upstream error: {e}", ERR_API, None


def _map_google_error(e) -> tuple:
    """Map an upstream exception to (status, message, google_status)."""
    status, message, _type, _code = _map_upstream_error(e)
    gstatus = "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE"
    return status, message, gstatus


def _upload_images(images: list) -> list:
    """Upload images and return list of file references. Returns None if no images."""
    if not images:
        return None
    file_refs = []
    for item in images:
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        data, mime = item
        if isinstance(data, str):
            data = fetch_image_bytes(data)
            mime = mime or "image/png"
        if not data:
            raise RuntimeError("image fetch failed")
        mime = detect_image_mime(data, mime or "image/png")
        try:
            ref = upload_image(data, "image.png", mime or "image/png")
            file_refs.append(ref)
        except Exception as e:
            raise RuntimeError(f"image upload failed: {e}") from e
    return file_refs if file_refs else None


def _tool_retry_attempts(required_tool: bool, tools_active: bool,
                         deep_demand: bool = False) -> int:
    """Total upstream attempts for a turn that is expected to end in a call.

    One retry leaves a model that answers in prose instead of emitting the
    block failing on a meaningful share of turns (and every ``tool_choice:
    "required"`` failure is a 503 for the client), so the number of extra
    attempts is configurable -- ``tool_retry_attempts`` in the config. The
    default of 1 keeps the cost at "one extra call, and only when the first
    one already failed"; an operator running a forgetful model raises it
    instead of everyone paying for three calls on turns that succeeded.
    """
    if not (required_tool or (tools_active and CONFIG.get("tool_retry_on_miss"))):
        return 1
    try:
        extra = int(CONFIG.get("tool_retry_attempts", 1))
    except (TypeError, ValueError):
        extra = 1
    total = 1 + max(0, extra)
    if deep_demand and not required_tool:
        # A review/change turn is multi-step by nature (survey, then read,
        # then act): a weak model spends one attempt per step, so one attempt
        # always dies on a describe-instead-of-do reply. The extra attempt
        # costs nothing on turns that succeed first try.
        total += 1
    return total


def _google_declared(req) -> tuple:
    """``(allowed_names, schemas, required)`` from a Google native request.

    Mirrors :func:`tool_names` / :func:`tool_parameters` /
    :func:`tool_required_params` for ``functionDeclarations`` so the retry
    loop can normalise names, infer nothing silently, and demand required
    parameters -- the parity the OpenAI path already has.
    """
    allowed, schemas, required = set(), {}, {}
    for tool_group in req.get("tools") or []:
        if not isinstance(tool_group, dict):
            continue
        for fn in tool_group.get("functionDeclarations", []) or []:
            if not isinstance(fn, dict) or not fn.get("name"):
                continue
            name = fn["name"]
            allowed.add(name)
            params = fn.get("parameters") or fn.get("parametersJsonSchema") or {}
            props = params.get("properties") if isinstance(params, dict) else None
            schemas[name] = set(props) if isinstance(props, dict) else set()
            req_list = params.get("required") if isinstance(params, dict) else None
            if isinstance(req_list, list):
                required[name] = {n for n in req_list if isinstance(n, str)}
    return allowed or None, schemas or None, required or None


def _log_tool_trace(raw_text: str, tool_calls) -> None:
    """Log what the model emitted and what was parsed out of it.

    A tool call that reaches the client with missing or unusable arguments is
    indistinguishable, from the client's side, from a model that simply wrote
    ``{"arguments": {}}``. The raw block is the only place that shows whether
    the model or the parser is at fault, so log it whenever tools are involved.

    Argument values are truncated (per-call and per-value caps): a ``write``
    call carries a whole file body, and logging it verbatim to stderr or
    ``log_file`` under plain ``log_requests=true`` leaks file content into
    logs the README only warns about for the debug flags.
    """
    blocks = raw_text.count("```tool_call")
    log(f"upstream: {len(raw_text)} chars, {blocks} tool_call marker(s)")
    for m in re.finditer(r"```tool_call[ \t]*\n?(.*?)\n?```", raw_text, re.DOTALL):
        log(f"tool_call block: {m.group(1).strip()[:500]}")
    if blocks and not tool_calls:
        log("tool_call marker(s) present but no tool call was parsed")
    if tool_calls:
        redacted = []
        for tc in tool_calls:
            try:
                fn = (tc or {}).get("function") or {}
                args_text = fn.get("arguments") or "{}"
                args = json.loads(args_text) if isinstance(args_text, str) else args_text
                if isinstance(args, dict):
                    short = {}
                    for k, v in args.items():
                        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                        short[k] = (s[:200] + "…[truncated]"
                                    if len(s) > 200 else s)
                    redacted.append({"name": fn.get("name"), "arguments": short})
                else:
                    redacted.append({"name": fn.get("name"),
                                     "arguments": str(args)[:200]})
            except (json.JSONDecodeError, ValueError, TypeError):
                redacted.append({"name": (tc.get("function") or {}).get("name"),
                                 "arguments": "[unparsable]"})
            if len(json.dumps(redacted, ensure_ascii=False)) > 2000:
                redacted.append("[... further calls truncated ...]")
                break
        log(f"parsed tool_calls: {json.dumps(redacted, ensure_ascii=False)}")
    if not tool_calls:
        # With tools offered and nothing parsed, the raw text is the only place
        # that distinguishes "model answered in prose" from "model used a fence
        # we do not recognise" (```json / ```function_call / bare JSON) -- both
        # look identical in the marker count above.
        fences = sorted({(m.group(1) or "bare").lower()
                         for m in re.finditer(r"```([A-Za-z0-9_+-]*)", raw_text)})
        log(f"no tool_call parsed; fences seen: {', '.join(fences) or 'none'}")
        log(f"no tool_call parsed; text head: {raw_text[:400]!r}")


class GeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        client_ip = self.client_address[0] if self.client_address else "-"
        log(f"{client_ip} {fmt % args}")

    # ─── CORS / security helpers ──────────────────────────────────────────────

    def _cors_origin(self) -> str:
        """Return the Access-Control-Allow-Origin value, or '' to omit.

        When api_keys is empty the server is unauthenticated; sending
        ``*`` would let any malicious page read the response.  We only
        emit the header when the operator explicitly configured
        ``cors_origins``.
        """
        origins = CONFIG.get("cors_origins") or []
        if not origins:
            return ""
        if "*" in origins:
            return "*"
        # Echo back the requesting origin if it is in the whitelist.
        origin = self.headers.get("Origin", "")
        if origin in origins:
            return origin
        return ""

    def _send_cors_headers(self):
        """Send CORS headers if applicable."""
        if self._route_path() == "/v1/cookies":
            # The cookie-sync endpoint is called from the bundled extension.
            # Keep its CORS grant narrow instead of reusing the API wildcard.
            origin = self.headers.get("Origin", "")
            if origin.startswith("chrome-extension://"):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, X-Gemini-Cookie-Sync",
                )
            return
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type, x-api-key, x-goog-api-key",
            )

    def _check_host(self) -> bool:
        """Reject requests whose Host header is not a loopback / configured host.

        This blocks DNS-rebinding attacks where an attacker's domain resolves
        to 127.0.0.1 but the browser sends ``Host: attacker.com``.

        Three cases where the check is deliberately not restrictive:

        * ``api_keys`` configured -- the request already had to present a valid
          key, so a rebound hostname buys nothing (``do_GET`` never runs this
          check either, so only POST was ever gated);
        * a wildcard bind (``0.0.0.0`` / ``::``) with ``api_keys`` -- the
          operator chose to serve every interface AND configured auth, so LAN
          IPs and container hostnames must pass;
        * no ``Host`` header at all (non-HTTP/1.1 edge case).

        The hostname is parsed with ``urlsplit`` so ``[::1]:8081`` yields
        ``::1`` instead of the unbracketed ``[::1]`` that ``rsplit(":")``
        produced, and a port is never mistaken for part of the name.

        NOTE: A wildcard bind WITHOUT ``api_keys`` does NOT bypass the check.
        This prevents accidental exposure when binding 0.0.0.0 on a public
        interface without authentication.
        """
        if CONFIG.get("api_keys"):
            return True
        host = self.headers.get("Host", "")
        if not host:
            return True
        hostname = (urllib.parse.urlsplit("//" + host).hostname or "").lower()
        if not hostname:
            return True
        allowed = {"localhost", "127.0.0.1", "::1"}
        bind_host = (CONFIG.get("host") or "").lower().strip("[]")
        if bind_host:
            if bind_host in ("0.0.0.0", "::", "*"):
                # Wildcard bind ONLY bypasses check when api_keys is set.
                # Without auth, we still restrict to loopback to prevent
                # accidental public exposure.
                pass
            else:
                allowed.add(bind_host)
        return hostname in allowed

    def _check_origin(self) -> bool:
        """Reject cross-origin requests when no API key is configured.

        A malicious page can issue a ``text/plain`` POST (simple request, no
        preflight) to our endpoint.  Without an API key we cannot distinguish
        a legitimate local client from an attacker's page, so we reject any
        request that carries an ``Origin`` header not in the whitelist.
        """
        keys = CONFIG.get("api_keys") or []
        if keys:
            return True  # Authenticated: origin check is not needed
        origin = self.headers.get("Origin")
        if not origin:
            return True  # No Origin: likely a non-browser client
        origins = CONFIG.get("cors_origins") or []
        if "*" in origins:
            return True
        return origin in origins

    # ─── response helpers ─────────────────────────────────────────────────────

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        trace(f"PROXY -> CLIENT (HTTP {status})", body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_api_error(self, status, message, type_=ERR_API, code=None):
        """Send an OpenAI-shaped error body."""
        self.send_json(
            {"error": {"message": message, "type": type_, "param": None, "code": code}},
            status,
        )

    def send_google_error(self, status, message, gstatus="UNKNOWN"):
        """Send a Google Gemini-shaped error body."""
        self.send_json({"error": {"code": status, "message": message, "status": gstatus}}, status)

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._send_cors_headers()
        self.end_headers()

    def _sse_write(self, data: bytes):
        """Write raw bytes to the SSE stream.

        A keep-alive thread may be writing ``: ping`` comments concurrently
        (see ``_start_sse_with_keepalive``), so every SSE write goes through
        one lock -- an interleaved frame would corrupt the stream.
        """
        lock = getattr(self, "_sse_lock", None)
        if lock is None:
            self.wfile.write(data)
            self.wfile.flush()
            return
        with lock:
            self.wfile.write(data)
            self.wfile.flush()

    def _start_sse_with_keepalive(self):
        """Send the SSE response head *now* and keep the wire warm.

        A buffered turn (tools active, or response_format set) can spend
        minutes upstream -- a thinking model plus one heuristic retry -- and
        HTTP carries its status line with the first body byte. A client that
        waits for response headers would see nothing at all until the very
        end and abort on its own header timeout (observed with OpenCode:
        "Timed out waiting for response headers", every 5 minutes, forever,
        while upstream answers kept arriving at a dead socket).

        Sending the head immediately plus an SSE comment every
        ``SSE_PING_INTERVAL_SEC`` keeps every idle timeout quiet; comments
        dispatch no event, so conformant clients never see them.
        """
        self._sse_lock = threading.Lock()
        client_gone = threading.Event()
        self._start_sse()

        stop_ping = threading.Event()

        hb = _Heartbeat(self, stop_ping, client_gone)
        threading.Thread(target=hb.run, daemon=True).start()
        return stop_ping.set, client_gone, hb

    def _sse_chunk(self, cid, model, delta, finish_reason, include_usage=False):
        # ``logprobs`` is required (nullable) on the choice of a chat chunk and
        # ``delta`` must open with an empty ``content`` so clients that read
        # ``delta.content`` on the first chunk (NextChat among them) see a
        # string rather than an absent key.
        chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "logprobs": None,
                         "finish_reason": finish_reason}],
        }
        if include_usage:
            # stream_options.include_usage: every chunk carries a usage field,
            # null until the final usage chunk.
            chunk["usage"] = None
        frame = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        trace("PROXY -> CLIENT (SSE frame)", frame)
        self._sse_write(frame.encode())

    def _sse_usage(self, cid, model, prompt, text):
        chunk = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [],
            "usage": _usage(prompt, text),
        }
        frame = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        trace("PROXY -> CLIENT (SSE usage)", frame)
        self._sse_write(frame.encode())

    def _sse_event_error(self, status, message, type_=ERR_API, code=None):
        payload = {"error": {"message": message, "type": type_, "param": None, "code": code}}
        frame = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        trace("PROXY -> CLIENT (SSE error)", frame)
        self._sse_write(frame.encode())

    def _sse_done(self):
        trace("PROXY -> CLIENT (SSE done)", "data: [DONE]\n\n")
        self._sse_write(b"data: [DONE]\n\n")

# ─── request parsing / auth / routing ─────────────────────────────────────

    def _parse_body(self, body: bytes):
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return None

    def _read_request_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        if "chunked" in transfer_encoding.lower():
            chunks = []
            total = 0
            limit = CONFIG.get("max_body_bytes", 32 * 1024 * 1024)
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                size_text = size_line.split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    raise ValueError("invalid chunked request body")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > limit:
                    raise ValueError("request body too large")
                chunks.append(self.rfile.read(size))
                # Consume the trailing CRLF after each chunk. Some non-conformant
                # clients send only LF; read until we hit a newline to be robust.
                crlf = self.rfile.read(1)
                if crlf == b"\r":
                    self.rfile.read(1)  # consume LF
                elif crlf != b"\n":
                    # Unexpected byte; push back by treating as part of next read
                    # (unlikely in practice, but avoids desync)
                    pass
            return b"".join(chunks)

        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return b""
        try:
            length = int(raw_len)
        except (ValueError, TypeError):
            raise ValueError("invalid Content-Length header")
        if length < 0:
            raise ValueError("invalid Content-Length header")
        if length > CONFIG.get("max_body_bytes", 32 * 1024 * 1024):
            raise ValueError("request body too large")
        return self.rfile.read(length) if length else b""

    def _route_path(self) -> str:
        """Return the request path without query string / trailing slash."""
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    def _authorized(self):
        keys = CONFIG.get("api_keys") or []
        if not keys:
            return True
        provided = []
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided.append(auth[7:])
        for header in ("x-api-key", "x-goog-api-key"):
            value = self.headers.get(header)
            if value:
                provided.append(value)
        query = urllib.parse.urlparse(self.path).query
        for pair in query.split("&"):
            if pair.startswith("key="):
                provided.append(urllib.parse.unquote(pair[4:]))
        for value in provided:
            for key in keys:
                if isinstance(key, str) and hmac.compare_digest(value.encode(), key.encode()):
                    return True
        return False

    def _needs_auth(self, path: str) -> bool:
        return path not in ("/", "/health")

    def do_OPTIONS(self):
        path = self._route_path()
        if self._route_path() == "/v1/cookies":
            origin = self.headers.get("Origin", "")
            if not origin.startswith("chrome-extension://"):
                self.send_response(403)
                self.end_headers()
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-Gemini-Cookie-Sync",
            )
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()
            return
        requested = self.headers.get("Access-Control-Request-Headers")
        self.send_response(204)
        self._send_cors_headers()
        self.send_header(
            "Access-Control-Allow-Headers",
            requested or "Authorization, Content-Type, x-api-key, x-goog-api-key",
        )
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        try:
            path = self._route_path()
            if self._needs_auth(path) and not self._authorized():
                self.send_api_error(401, "invalid api key", ERR_INVALID_REQUEST, "invalid_api_key")
                return
            if path in ("/v1/models", "/models"):
                self.send_json({"object": "list", "data": [
                    {"id": n, "object": "model", "created": 1700000000,
                     "owned_by": "google", "description": c["desc"]}
                    for n, c in MODELS.items()
                ]})
            elif path.startswith("/v1/models/") or path.startswith("/models/"):
                model_id = path.rsplit("/", 1)[-1]
                cfg = MODELS.get(model_id)
                if cfg:
                    self.send_json({"id": model_id, "object": "model", "created": 1700000000,
                                    "owned_by": "google", "description": cfg["desc"]})
                else:
                    self.send_api_error(404, f"model '{model_id}' not found",
                                        ERR_INVALID_REQUEST, "model_not_found")
            elif path.startswith("/v1beta/models"):
                self.send_json({"models": [
                    {"name": f"models/{n}", "displayName": n, "description": c["desc"],
                     "supportedGenerationMethods": ["generateContent", "streamGenerateContent"]}
                    for n, c in MODELS.items()
                ]})
            elif re.match(r"^/(v1/)?responses/[^/]+$", path):
                # The spec declares GET /v1/responses/{id} (200/404) but this
                # server keeps no store, so every id is unknown. Deeper paths
                # (e.g. /input_items) fall through to the generic 404.
                self.send_api_error(404, f"response '{path.rsplit('/', 1)[-1]}' not found",
                                    ERR_INVALID_REQUEST, "response_not_found")
            elif path in ("/", "/health"):
                self.send_json({"status": "ok", "version": __version__,
                                "models": list(MODELS.keys())})

            else:
                self.send_api_error(404, f"not found: {path}", ERR_INVALID_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            # Without this an exception escapes and the socket is dropped with
            # no response at all, leaving the client waiting on a dead socket.
            log(f"GET error: {e}")
            try:
                self.send_api_error(500, str(e), ERR_API)
            except Exception:
                pass

    def _check_security(self) -> str:
        """Run Host / Origin / Content-Type checks. Returns '' or an error message."""
        if not self._check_host():
            return "forbidden host"
        if not self._check_origin():
            return "cross-origin request not allowed"
        return ""

    def _resolve_request_model(self, req):
        """Resolve the requested model. Returns a tuple or None (error sent)."""
        raw = req.get("model")
        base = raw.split("@think=")[0] if isinstance(raw, str) else raw
        if CONFIG.get("strict_models") and isinstance(base, str) and base and base not in MODELS:
            self.send_api_error(404, f"model '{raw}' not found",
                                ERR_INVALID_REQUEST, "model_not_found")
            return None
        model_name, model_id, think_mode, err, extra = resolve_model(
            raw or CONFIG["default_model"], default=CONFIG["default_model"])
        if err:
            self.send_api_error(400, err, ERR_INVALID_REQUEST)
            return None
        echo = raw if isinstance(raw, str) and raw else model_name
        effort = _reasoning_effort(req)
        alias = _lite_effort_model(model_name, effort)
        if alias != model_name:
            # Preserve an explicit @think= suffix while switching the level.
            think_suffix = ""
            if isinstance(raw, str) and "@think=" in raw:
                think_suffix = raw[raw.index("@think="):]
            model_name, model_id, think_mode, _, extra = resolve_model(
                alias + think_suffix, default=CONFIG["default_model"])
            from gemini_web2api.gemini import log
            log(f"[model] reasoning_effort={effort} -> {alias}")
        # The ticket is what upstream routes on; [79]/[80] alone are ignored.
        # Resolve it once so the log can name the requested and used keys.
        requested_key, used_key, ticket = _resolve_ticket(model_name)
        cfg = MODELS.get(model_name) or {}
        variant = (extra or {}).get(80, cfg.get("variant"))
        level = _TICKET_KEY_LEVEL.get(used_key or requested_key, "unknown")
        if requested_key and used_key and used_key != requested_key:
            ticket_desc = f"{requested_key}->{used_key}"
        else:
            ticket_desc = requested_key or "none"
        from gemini_web2api.gemini import log
        log(f"[model] requested={echo} effort={effort or 'default'} -> "
            f"{model_name} (family={model_id} variant={variant} "
            f"level={level} ticket={ticket_desc})")
        return echo, model_name, model_id, think_mode, extra, ticket

    def do_POST(self):
        try:
            path = self._route_path()
            if path == "/v1/cookies":
                # Cookie rotation arrives from the extension without an API
                # key, so this route does its own header/host/loopback checks
                # instead of the generic key + Origin policy below.
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    self.send_api_error(415, "Content-Type must be application/json", ERR_INVALID_REQUEST)
                    return
                body = self._read_request_body()
                trace(f"CLIENT -> PROXY ({path})", body)
                self._handle_update_cookies(body)
                return
            if self._needs_auth(path) and not self._authorized():
                self.send_api_error(401, "invalid api key", ERR_INVALID_REQUEST, "invalid_api_key")
                return
            # Security checks: Host, Origin, Content-Type
            security_err = self._check_security()
            if security_err:
                self.send_api_error(403, security_err, ERR_INVALID_REQUEST)
                return
            # Require application/json for POST requests (except OPTIONS)
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self.send_api_error(415, "Content-Type must be application/json", ERR_INVALID_REQUEST)
                return
            body = self._read_request_body()
            trace(f"CLIENT -> PROXY ({path})", body)
            if path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_chat(body)
            elif path in ("/v1/responses", "/responses"):
                self._handle_responses(body)
            elif ":streamGenerateContent" in path:
                self._handle_google_generate(body, stream=True)
            elif ":generateContent" in path:
                self._handle_google_generate(body, stream=False)
            else:
                self.send_api_error(404, f"not found: {path}", ERR_INVALID_REQUEST)
        except ValueError as e:
            # Body parsing errors: invalid Content-Length, too large, etc.
            if "too large" in str(e):
                self.send_api_error(413, str(e), ERR_INVALID_REQUEST)
            else:
                self.send_api_error(400, str(e), ERR_INVALID_REQUEST)
            return
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"POST error: {e}")
            try:
                self.send_api_error(500, str(e), ERR_API)
            except Exception:
                pass

    def _unsupported_method(self):
        """Answer PUT/PATCH/DELETE with a JSON error instead of 501 HTML.

        ``BaseHTTPRequestHandler`` replies to any method it has no ``do_*``
        for with a bare-text ``501``, which no OpenAI SDK can parse.
        """
        try:
            path = self._route_path()
            if self._needs_auth(path) and not self._authorized():
                self.send_api_error(401, "invalid api key", ERR_INVALID_REQUEST,
                                    "invalid_api_key")
                return
            # Drain the body before answering. Left unread it becomes the next
            # "request line", and the resulting connection reset can discard
            # the very response written here.
            try:
                self._read_request_body()
            except ValueError as e:
                if "too large" in str(e):
                    self.send_api_error(413, str(e), ERR_INVALID_REQUEST)
                    return
            # DELETE /v1/responses/{id} is a real spec endpoint (200 or 404),
            # and this server stores nothing, so every id is simply unknown.
            if self.command == "DELETE" and re.match(r"^/(v1/)?responses/[^/]+$", path):
                self.send_api_error(404, f"response '{path.rsplit('/', 1)[-1]}' not found",
                                    ERR_INVALID_REQUEST, "response_not_found")
                return
            self.send_api_error(405, f"{self.command} is not allowed for {path}",
                                ERR_INVALID_REQUEST, "method_not_allowed")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log(f"{self.command} error: {e}")
            try:
                self.send_api_error(500, str(e), ERR_API)
            except Exception:
                pass

    def do_PUT(self):
        self._unsupported_method()

    def do_PATCH(self):
        self._unsupported_method()

    def do_DELETE(self):
        self._unsupported_method()

    def _generate_with_tool_retry(self, prompt, model_id, think_mode, file_refs,
                                  extra, tools_active, allowed_names,
                                  tool_schemas, required_tool, messages=None,
                                  ticket=None, tool_required=None,
                                  client_gone=None):
        """Run ``generate()``, retrying while the reply did not use a tool.

        Five shapes all arrive as a normal HTTP 200 yet leave the client
        believing the turn is over:

        * an empty body -- a connection that died mid-generation leaves
          nothing to answer with, and 200-with-no-content reads as "done";
        * the upstream's canned failure sentence ("I encountered an error doing
          what you asked...") -- nothing was generated, a retry usually works,
          and passing it on reads as a finished answer;
        * the model announcing an action ("let me read...") without ever
          emitting the block;
        * the model *fabricating* an answer about a file no tool call has
          opened -- the reply is fluent and complete, so nothing in the text
          itself looks wrong (see ``pending_tool_request``);
        * ``tool_choice: "required"`` with no block at all -- or a block that
          parses but omits a required parameter, which fails on the client's
          side and costs a round-trip before anyone learns what was missing.

        The retry appends a hard instruction. The number of attempts comes from
        :func:`_tool_retry_attempts`. Raises once attempts are exhausted (or if
        ``generate`` itself fails on the final attempt) so the caller decides
        how the failure is reported -- both endpoints map it through
        ``_map_upstream_error``.
        """
        retry_on_miss = bool(CONFIG.get("tool_retry_on_miss"))
        # Generic source review ("review source" without a named file) --
        # computed from the last user message, same way as pending_tool_request.
        last_user = None
        if messages:
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    last_user = _stringify_content(msg.get("content", ""))
                    break
        is_generic_source_review = False
        if last_user:
            scannable = _strip_code_spans(last_user)
            folded = _fold_diacritics(scannable)
            folded = re.sub(r"\s+", " ", folded).strip()
            if _is_source_review(folded) and not _is_file_review(folded, scannable):
                is_generic_source_review = True

        # An implement/change turn whose reply names files it never read is
        # the same fabrication shape as an unread source review: the model
        # surveyed (glob/grep) and then described the edit from the snippets.
        is_change_turn = change_demanded(messages) if tools_active else False

        # Review/change turns are multi-step (survey, read, act): grant one
        # more attempt so a describe-instead-of-do reply can still be
        # converted into the call. Ordinary turns keep the old budget.
        attempts = _tool_retry_attempts(
            required_tool, tools_active,
            deep_demand=(is_generic_source_review or is_change_turn))
        # Computed once: it inspects the whole conversation, not the reply.
        owed = pending_tool_request(messages) if tools_active else None
        # Files the model just listed but never opened: the read-directive
        # nudge names these so the retry reads instead of guessing.
        files = listed_turn_files(messages) if tools_active else []
        # Calls this conversation already has results for: emitting one again
        # is a loop, not progress.
        repeatable = _repeatable_calls(messages) if tools_active else set()

        text, tool_calls, missing = "", None, []
        dropped = []
        nudge_extra = ""
        repeat_nudge = ""
        repeats = 0
        saw_call = False
        dup_fallback = False
        best_text, best_idx = "", -1
        # Canned-error placeholders seen this turn: a deterministic refusal
        # (expired ticket / extension-routed prompt) is retried at most twice;
        # more attempts only burn quota on the same answer.
        placeholder_hits = 0
        for attempt in range(attempts):
            if attempt > 0 and client_gone is not None and client_gone.is_set():
                # The client hung up while an earlier attempt ran (a keep-alive
                # write failed): another upstream call would bill the account
                # for an answer that reaches nobody.
                log("Tool retry skipped: client disconnected")
                break
            call_prompt = prompt
            if attempt > 0:
                # "tool_call block ONLY" is right when the client demands a
                # call, but on a heuristic retry it would rewrite a finished
                # summary into a call nobody asked for -- and an agent happily
                # runs that, then asks again. Conditional wording still pushes
                # an acting model to act while letting a genuine answer stand.
                # A source-review turn that only listed files gets a directive
                # instead: the permissive "otherwise give your final answer"
                # is what let the model answer from filenames alone.
                if not required_tool and (is_read_retry_reason(owed)
                                            or is_test_retry_reason(owed)
                                            or is_review_test_reason(owed)
                                            or is_generic_source_review
                                            or is_change_turn):
                    if is_test_retry_reason(owed):
                        # The code is edited but no test command ran after
                        # the edit: direct, not a suggestion. A stale
                        # pre-edit run does not count.
                        nudge = ("IMPORTANT: The change is implemented but "
                                 "untested. Run the unit tests NOW with a "
                                 "bash tool_call block (python -m unittest "
                                 "discover -s tests) -- no prose. If no test "
                                 "covers the changed behavior, add one first "
                                 "with an edit block, then run the tests.")
                    elif is_review_test_reason(owed):
                        # A vague review grounds itself in test results
                        # first: failures point at what to read closely.
                        nudge = ("IMPORTANT: Run the unit tests NOW with a "
                                 "bash tool_call block (python -m unittest "
                                 "discover -s tests) -- no prose. Read the "
                                 "source files only after you have the test "
                                 "results, starting with files any failure "
                                 "implicates.")
                    elif is_generic_source_review:
                        unread_core = unread_listed_core_files(messages)
                        if unread_core:
                            # Escalation over the directive nudge: the S3 live
                            # probe showed a weak model reading one file and
                            # then answering prose anyway, ignoring "read the
                            # others first". While listed core files stay
                            # unread, prose is forbidden entirely so the retry
                            # can only come back with read blocks; the answer
                            # is allowed on a later attempt once the evidence
                            # exists.
                            nudge = ("IMPORTANT: Respond with read tool_call "
                                     "blocks ONLY -- no prose, before or "
                                     "after. Read ALL of these files: "
                                     + ", ".join(unread_core[:12]) + ".")
                        else:
                            nudge = ("IMPORTANT: You have not read enough of the source yet -- "
                                     "listing file names, or opening a single file, is not a review. Call the read tool now "
                                     "on the other relevant module(s) and answer from what you actually read.")
                            if files:
                                nudge += " Read these first: " + ", ".join(files[:8]) + "."
                    else:
                        nudge = ("IMPORTANT: You have not read enough of the source yet -- "
                                 "listing file names, or opening a single file, is not a review. Call the read tool now "
                                 "on the other relevant module(s) and answer from what you actually read.")
                        if files:
                            nudge += " Read these first: " + ", ".join(files[:8]) + "."
                else:
                    nudge = (
                        "IMPORTANT: Respond with a tool_call block ONLY."
                        if required_tool else
                        "IMPORTANT: Do not describe a tool call -- make it. If "
                        "answering requires reading or changing something, call "
                        "the tool now; otherwise give your final answer."
                    )
                if missing:
                    # A model that omitted a parameter believes its call is
                    # complete, so it has to be told which fields were absent.
                    nudge += (f" The previous tool call was missing required "
                              f"parameter(s): {', '.join(missing)} -- include "
                              "every required parameter.")
                    missing = []
                if nudge_extra:
                    nudge += nudge_extra
                    nudge_extra = ""
                if repeat_nudge:
                    nudge += repeat_nudge
                    repeat_nudge = ""
                call_prompt = prompt + "\n\n" + nudge
            try:
                if CONFIG.get("tool_stream"):
                    # Buffered turn, live heartbeat: every upstream delta
                    # emits an SSE comment on the already-open stream, so the
                    # client sees per-part activity instead of 20-60 s of
                    # silence between attempts. The sink is set by the endpoint
                    # that opened the stream; absent -> old buffered behaviour.
                    hb = getattr(self, "_chat_hb", None)
                    sink = (hb.feed if (hb is not None
                                        and CONFIG.get("tool_delta_forward", True))
                            else None)
                    raw = _streamed_generate(call_prompt, model_id, think_mode,
                                             file_refs, extra, ticket,
                                             tools_active, attempt,
                                             on_delta=sink)
                else:
                    raw = generate(call_prompt, model_id, think_mode, file_refs, extra, ticket)
            except Exception as e:
                if attempt + 1 < attempts:
                    log(f"Tool retry after upstream error: {e}")
                    time.sleep(CONFIG["retry_delay_sec"])
                    continue
                if best_text and not required_tool:
                    log(f"Tool retry: final attempt failed ({e}); returning "
                        f"best text from attempt {best_idx + 1}")
                    return best_text, None
                raise
            text, tool_calls, dropped = raw, None, []
            if tools_active and text:
                text, tool_calls, dropped = parse_tool_calls(text, allowed_names, tool_schemas)
                _log_tool_trace(raw, tool_calls)
            if tool_calls:
                if all(((tc.get("function") or {}).get("name"),
                        _canonical_args((tc.get("function") or {}).get("arguments")))
                       in repeatable for tc in tool_calls):
                    # Every call repeats one whose result is already in the
                    # conversation: running it again cannot learn anything.
                    if repeats < 3 and attempt + 1 < attempts:
                        repeats += 1
                        if required_tool and CONFIG.get("required_tool_fail_on_duplicate", True):
                            repeat_nudge = (" The result of this exact tool call "
                                            "is already in the conversation -- "
                                            "do NOT call it again with the same "
                                            "arguments; emit a DIFFERENT tool_call "
                                            "block for the next step.")
                        else:
                            repeat_nudge = (" The result of this exact tool call "
                                            "is already in the conversation -- "
                                            "answer in plain text, do not call "
                                            "it again.")
                        log(f"Tool retry (attempt {attempt + 1}/{attempts}): "
                            "duplicate tool call already answered, nudging "
                            "the model to answer instead")
                        continue
                    log("Duplicate tool call repeated 3 times (or no attempts "
                        "left): a loop is worse than one prose turn, "
                        "returning the plain-text answer instead")
                    tool_calls = None
                    dup_fallback = True
                    break
                saw_call = True
                missing = missing_required_params(tool_calls, tool_required)
                if missing:
                    msg = ("model emitted invalid tool call; missing required "
                           "parameter(s): " + ", ".join(missing))
                    # Always logged: even when no attempt is left this is the
                    # only place that shows the model wrote a call the client
                    # cannot run.
                    log(msg)
                    if attempt + 1 < attempts:
                        log(f"Tool retry (attempt {attempt + 1}/{attempts}): "
                            "asking the model to fill them in")
                        nudge_extra = (
                            f" The previous tool call was missing required "
                            f"parameter(s): {', '.join(missing)}. Include "
                            "every required parameter.")
                        continue
                    # Retries exhausted and the call is still unrunnable:
                    # handing it to the client as a valid tool call costs a
                    # round-trip and the model never learns which field was
                    # absent. Fail closed -- the endpoint maps this to an
                    # error status instead of pretending the call is fine.
                    raise GeminiError(msg)
                break
            if (text or "").strip() and not looks_like_canned_failure(text):
                # A usable answer worth keeping if a later attempt fails: a
                # good first turn must not become a 503 because the retry
                # produced the placeholder or raised.
                best_text, best_idx = text, attempt
            if required_tool:
                continue
            if attempt + 1 >= attempts:
                break
            time.sleep(CONFIG["retry_delay_sec"])
            if not (text or "").strip():
                # An upstream that produced nothing (observed once as an empty
                # body after a dropped connection) is never a usable answer:
                # handing it to the client as 200 says "finished, nothing to
                # do" and Opencode stops the turn there.
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): upstream "
                    "returned an empty response")
                continue
            if looks_like_canned_failure(text):
                placeholder_hits += 1
                if placeholder_hits >= 2:
                    log("Tool retry: upstream returned the same canned "
                        "placeholder twice -- stopping early; if this "
                        "persists, refresh CONFIG['model_tickets'] from a "
                        "fresh browser capture and check the routing-mismatch "
                        "log lines above")
                    break
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): upstream "
                    "returned its canned error placeholder instead of a response")
                continue
            if (not required_tool and tools_active
                    and looks_like_canned_failure(text)):
                # A verbose model can pad the settings-are-off refusal with
                # prose, escaping the anchored placeholder regex; without this
                # branch it would reach the client as a finished answer.
                placeholder_hits += 1
                if placeholder_hits >= 2:
                    log("Tool retry: upstream returned the same extension "
                        "refusal twice -- stopping early; refresh "
                        "CONFIG['model_tickets'] if this persists")
                    break
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): upstream "
                    "returned a Workspace-extension refusal inside a longer "
                    "reply")
                continue
            if tools_active and dropped:
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): previous "
                    f"tool_call block(s) unusable: {'; '.join(dropped)}")
                nudge_extra = (" Your previous tool_call block(s) could not be "
                               "used: " + "; ".join(dropped) +
                               ". Valid tool names: "
                               + ", ".join(sorted(allowed_names or [])) + ".")
                continue
            fresh = (fresh_declared_unread(text, messages)
                     if tools_active else [])
            if tools_active and fresh and (is_generic_source_review
                                            or is_change_turn):
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): reply "
                    f"named unread file(s): {', '.join(fresh)}")
                nudge_extra = (" You named these file(s) as part of the review "
                               "but have not read them: " + ", ".join(fresh) +
                               ". Call the read tool on each of them now, then "
                               "answer from what you actually read.")
                continue
            if (retry_on_miss and tools_active and (text or "").strip()
                    and review_demanded(messages)):
                bad_quotes = unverified_quotes(text, messages)
                if bad_quotes:
                    log(f"Tool retry (attempt {attempt + 1}/{attempts}): "
                        f"{len(bad_quotes)} quoted snippet(s) not found in "
                        "any tool result")
                    nudge_extra = (" Some code you quoted does not appear in "
                                   "any tool result: "
                                   + " | ".join(q[:80] for q in bad_quotes[:3])
                                   + ". Do not quote from memory: call the "
                                   "read tool (or grep) on the relevant file "
                                   "and quote only lines that really exist, "
                                   "or drop the claim.")
                    continue
            if retry_on_miss and owed:
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): {owed}")
                continue
            if (retry_on_miss and tools_active and is_change_turn
                    and not _turn_act_calls(messages, -1)
                    and looks_like_described_edit(text)):
                # The model read the code and then wrote a how-to instead of
                # editing ("here is how you can...", "since tool calls are no
                # longer available..."). An edit already emitted anywhere in
                # the conversation means the work is done -- only a
                # describe-instead-of-do reply is retried, with a do-it nudge.
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): reply "
                    f"describes the change instead of making it")
                nudge_extra = (" You have already read the relevant code -- "
                               "make the change with the edit/write tool now "
                               "instead of describing it. Emit the tool_call "
                               "block(s) and stop.")
                continue
            if (retry_on_miss and tools_active and not saw_call
                    and (is_generic_source_review or is_change_turn)
                    and not _conversation_called_files(messages)):
                # A review/change request answered in prose with no tool call
                # ever emitted in the conversation: `owed` was computed before
                # the reply and only covers user-named files, so a vague
                # prompt ("review how this proxy loads the cookie") and a
                # survey-stall (glob/grep, then prose describing the edit)
                # both slip through. One retry with the read-directive nudge
                # above; a turn that already has evidence never reaches here.
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): "
                    f"review/change requested but no tool call was ever "
                    f"emitted")
                continue
            if retry_on_miss and tools_active and looks_like_missed_tool_call(text):
                log(f"Tool retry (attempt {attempt + 1}/{attempts}): response "
                    f"looks like a missed tool call ({len(text)} chars)")
                continue
            break

        # The canned sentence is an upstream failure, not something the model
        # said. Handing it to the client as 200 says "answered" when it was not.
        if not tool_calls and looks_like_canned_failure(text):
            if best_text and not required_tool:
                log(f"Tool retry: final answer is the upstream placeholder; "
                    f"returning best text from attempt {best_idx + 1}")
                return best_text, None
            raise GeminiError("upstream returned an error placeholder instead "
                              "of a response; please retry")
        if not tool_calls and not (text or "").strip():
            if best_text and not required_tool:
                log(f"Tool retry: final answer is empty; returning best text "
                    f"from attempt {best_idx + 1}")
                return best_text, None
        if required_tool and not tool_calls:
            # `tool_choice: required` is a contract: prose with no call ever
            # emitted is not a result. Raise so the endpoint reports 503
            # instead of a 200 that silently ends an agent turn. A duplicate
            # loop that fell back to prose breaks the contract too -- it
            # raises as well while `required_tool_fail_on_duplicate` is on
            # (the default); set that config to False to keep the old soft
            # behaviour ("a loop is worse than one prose turn").
            if dup_fallback and not CONFIG.get("required_tool_fail_on_duplicate", True):
                pass
            elif not saw_call or dup_fallback:
                raise GeminiError("model did not emit a usable required tool "
                                  "call; please retry")
        return text, tool_calls

    # ─── /v1/chat/completions ─────────────────────────────────────────────────

    # ─── /v1/cookies ──────────────────────────────────────────────────────────

    def _cookie_sync_allowed(self) -> bool:
        """Authorize a cookie rotation push from the bundled extension.

        The extension cannot send an API key, so the endpoint relies on what a
        browser CSRF / DNS-rebinding request cannot forge: the custom header,
        an extension Origin (when present), a loopback Host, and -- when no
        api_keys are configured -- a loopback client address. With api_keys,
        non-loopback clients must still authenticate normally.
        """
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith("chrome-extension://"):
            return False
        if self.headers.get("X-Gemini-Cookie-Sync") != "1":
            return False
        host = self.headers.get("Host", "")
        if host:
            hostname = (urllib.parse.urlsplit("//" + host).hostname or "").lower()
            if hostname and hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
        client_ip = self.client_address[0] if self.client_address else ""
        if not _is_loopback(client_ip):
            if not (CONFIG.get("api_keys") and self._authorized()):
                return False
        return True

    def _handle_update_cookies(self, body: bytes):
        if not self._cookie_sync_allowed():
            self.send_json({"error": {"message": "cookie sync forbidden"}}, 403)
            return
        req = self._parse_body(body)
        if req is None:
            self.send_json({"error": {"message": "invalid JSON"}}, 400)
            return
        ok, err_msg, status = update_cookie_payload(req)
        if not ok:
            self.send_json({"error": {"message": err_msg}}, status)
            return
        self.send_json({"status": "ok"})

    def _handle_chat(self, body: bytes):
        req = self._parse_body(body)
        if not isinstance(req, dict):
            self.send_api_error(400, "invalid JSON body", ERR_INVALID_REQUEST)
            return
        resolved = self._resolve_request_model(req)
        if resolved is None:
            return
        echo_model, model_name, model_id, think_mode, extra, ticket = resolved

        n = req.get("n", 1)
        if isinstance(n, int) and n > 1:
            self.send_api_error(400, "n>1 is not supported", ERR_INVALID_REQUEST, "unsupported_value")
            return

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        messages = req.get("messages", [])
        if not isinstance(messages, list):
            self.send_api_error(400, "messages must be a list", ERR_INVALID_REQUEST)
            return

        # Review behaviour ("read the file before reviewing it") lives in the
        # shared `_tool_use_block` appended by `messages_to_prompt` whenever
        # tools are active -- not as a per-request injected system message.
        # (A hard-coded injection here previously shipped another project's
        # filenames, replaced clients' system prompts, and crashed on
        # list-shaped `content`.)
        prompt, images = messages_to_prompt(messages, tools, tool_choice)

        rf_instruction = build_response_format_instruction(req.get("response_format"))
        if rf_instruction:
            prompt = prompt + rf_instruction
        if not prompt.strip():
            self.send_api_error(400, "empty prompt", ERR_INVALID_REQUEST)
            return

        stream = bool(req.get("stream", False))
        stop_strings = _normalize_stop(req.get("stop"))
        max_tokens = req.get("max_tokens")
        if max_tokens is None:
            max_tokens = req.get("max_completion_tokens")
        include_usage = bool((req.get("stream_options") or {}).get("include_usage"))
        cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
            self.send_api_error(503, f"upstream error: {e}", ERR_API)
            return

        allowed_names = tool_names(tools) or None
        tool_schemas = tool_parameters(tools) or None
        tool_required = tool_required_params(tools) or None

        # Pure streaming (no tools, no post-processing): stream tokens straight through.
        # When response_format is set the text still has to be defenced and
        # normalised, so it has to go through the buffered path below.
        if stream and not rf_instruction and (not tools or tool_choice == "none"):
            self._stream_chat(cid, echo_model, prompt, model_id, think_mode, file_refs,
                              extra, stop_strings, max_tokens, include_usage, ticket)
            return

        # Tool calls need the full text; retry once when the reply did not use
        # one (see _generate_with_tool_retry).
        required_tool = is_required_tool_choice(tool_choice)
        tools_active = bool(tools) and tool_choice != "none"

        # A streaming client gets its response head and the opening role chunk
        # immediately, then keep-alive comments while upstream works: a
        # buffered turn can take minutes (thinking model plus one tool retry),
        # and a client that has received no headers at all gives up on its own
        # header timeout mid-generation (see _start_sse_with_keepalive).
        stop_ping, client_gone = None, None
        if stream:
            stop_ping, client_gone, self._chat_hb = self._start_sse_with_keepalive()
            self._sse_chunk(cid, echo_model, {"role": "assistant", "content": ""},
                            None, include_usage)
        try:
            text, tool_calls = self._generate_with_tool_retry(
                prompt, model_id, think_mode, file_refs, extra,
                tools_active, allowed_names, tool_schemas, required_tool,
                messages=messages, ticket=ticket, tool_required=tool_required,
                client_gone=client_gone)
        except Exception as e:
            if stream:
                try:
                    self._sse_event_error(*_map_upstream_error(e))
                    self._sse_done()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_api_error(*_map_upstream_error(e))
            return
        finally:
            if stop_ping is not None:
                stop_ping()
            self._chat_hb = None

        if rf_instruction and text:
            text = strip_code_fence(text)
        text = _apply_stop(text, stop_strings)
        text, truncated = _apply_max_tokens(text, max_tokens)

        if not text and not tool_calls:
            if stream:
                self._sse_event_error(503, "empty response from upstream", ERR_API)
                self._sse_done()
                return
            self.send_api_error(503, "empty response from upstream", ERR_API)
            return

        # `refusal` and `logprobs` are required-nullable on the spec's
        # response message/choice; strict SDKs (Go, Rust, Java) reject an
        # absent key where Python's only checks for null.
        msg = {"role": "assistant", "content": text or None, "refusal": None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else ("length" if truncated else "stop")

        if stream:
            # The head and the role chunk were sent before upstream started.
            try:
                if text:
                    self._sse_chunk(cid, echo_model, {"content": text}, None, include_usage)
                for index, tc in enumerate(tool_calls or []):
                    self._sse_chunk(cid, echo_model,
                                    {"tool_calls": [{"index": index, **tc}]},
                                    None, include_usage)
                self._sse_chunk(cid, echo_model, {}, finish, include_usage)
                if include_usage:
                    self._sse_usage(cid, echo_model, prompt, text)
                self._sse_done()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        self.send_json({
            "id": cid, "object": "chat.completion", "created": int(time.time()),
            "model": echo_model,
            "choices": [{"index": 0, "message": msg, "logprobs": None,
                         "finish_reason": finish}],
            "usage": _usage(prompt, text),
        })

    @staticmethod
    def _iter_with_first(first, gen):
        yield first
        for delta in gen:
            yield delta

    def _stream_chat(self, cid, model, prompt, model_id, think_mode, file_refs,
                     extra, stop_strings, max_tokens, include_usage, ticket=None):
        # Head first, upstream second: the first upstream byte can take
        # minutes on a thinking model, and a client waiting for response
        # headers would abort before a single frame (see
        # _start_sse_with_keepalive).
        stop_ping, _client_gone, _late_hb = self._start_sse_with_keepalive()
        self._sse_chunk(cid, model, {"role": "assistant", "content": ""},
                        None, include_usage)
        try:
            gen = generate_stream(prompt, model_id, think_mode, file_refs, extra, ticket)
            first = next(gen, _MISSING)
        except Exception as e:
            stop_ping()
            try:
                self._sse_event_error(*_map_upstream_error(e))
                self._sse_done()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if first is _MISSING:
            try:
                self._sse_event_error(503, "empty response from upstream", ERR_API)
                self._sse_done()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        full_text = ""
        finish = "stop"
        hold = max((len(s) for s in stop_strings), default=0)
        buf = ""
        try:
            # The keep-alive stays on for the whole stream: a thinking model
            # can go silent between chunks for longer than a client's chunk
            # timeout, and a comment is ignored by every SSE parser.
            for raw in self._iter_with_first(first, gen):
                if not raw:
                    continue
                buf += raw
                hit = None
                for s in stop_strings:
                    idx = buf.find(s)
                    if idx != -1 and (hit is None or idx < hit):
                        hit = idx
                if hit is not None:
                    piece = buf[:hit]
                    if piece:
                        full_text += piece
                        self._sse_chunk(cid, model, {"content": piece}, None, include_usage)
                    buf = ""
                    break
                # `>` rather than `>=`: a reply that lands exactly on the budget
                # is complete, and only a truncated reply may claim `length`.
                if max_tokens and (len(full_text) + len(buf)) > max_tokens * 4:
                    allowed = max(0, max_tokens * 4 - len(full_text))
                    piece = buf[:allowed]
                    if piece:
                        full_text += piece
                        self._sse_chunk(cid, model, {"content": piece}, None, include_usage)
                    buf = ""
                    finish = "length"
                    break
                if len(buf) > hold:
                    piece = buf[:len(buf) - hold] if hold else buf
                    buf = buf[-hold:] if hold else ""
                    if piece:
                        full_text += piece
                        self._sse_chunk(cid, model, {"content": piece}, None, include_usage)
            else:
                if buf:
                    full_text += buf
                    self._sse_chunk(cid, model, {"content": buf}, None, include_usage)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            log(f"Stream error: {e}")
            try:
                self._sse_event_error(*_map_upstream_error(e))
                self._sse_done()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        finally:
            stop_ping()
            close = getattr(gen, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        self._sse_chunk(cid, model, {}, finish, include_usage)
        if include_usage:
            self._sse_usage(cid, model, prompt, full_text)
        self._sse_done()

# ─── /v1/responses (Codex CLI) ───────────────────────────────────────────

    def _normalize_responses_tools(self, tools):
        if not tools:
            return None
        normalized = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function":
                fn = tool.get("function", tool)
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not name:
                    log("Responses: ignoring function tool without a name")
                    continue
                normalized.append({"type": "function", "function": {
                    "name": name,
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }})
            else:
                log(f"Responses: ignoring unsupported tool type '{tool.get('type')}'")
        return normalized or None

    def _responses_messages(self, input_items, instructions):
        messages = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
            return messages
        if not isinstance(input_items, list):
            return messages
        for item in input_items:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "function_call_output":
                output = item.get("output", "")
                if isinstance(output, list):
                    output = "\n".join(
                        c.get("text", "") for c in output
                        if isinstance(c, dict) and c.get("type") in ("output_text", "text")
                    )
                messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                                 "content": output})
            elif itype == "function_call":
                call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                # Codex omits `arguments` on a call it wants replayed from context;
                # an empty string would serialise into the broken `"arguments": }`.
                call = {"id": call_id, "type": "function",
                        "function": {"name": item.get("name", ""),
                                     "arguments": item.get("arguments") or "{}"}}
                if messages and messages[-1].get("role") == "assistant" \
                        and messages[-1].get("tool_calls"):
                    messages[-1]["tool_calls"].append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif itype in ("input_text", "input_image", "image"):
                messages.append({"role": "user", "content": [item]})
            elif item.get("role") == "assistant":
                content_parts = item.get("content", [])
                text_acc, tc_list = "", []
                if isinstance(content_parts, list):
                    for c in content_parts:
                        if isinstance(c, dict):
                            if c.get("type") == "output_text":
                                text_acc += c.get("text", "")
                            elif c.get("type") == "function_call":
                                tc_list.append(c)
                elif isinstance(content_parts, str):
                    text_acc = content_parts
                message = {"role": "assistant", "content": text_acc or None}
                if tc_list:
                    message["tool_calls"] = [
                        {"id": tc.get("call_id", f"call_{i}"), "type": "function",
                         "function": {"name": tc.get("name", ""),
                                      "arguments": tc.get("arguments") or "{}"}}
                        for i, tc in enumerate(tc_list)
                    ]
                messages.append(message)
            else:
                role = item.get("role", "user")
                if role == "developer":
                    role = "system"
                messages.append({"role": role, "content": item.get("content", "")})
        return messages

    def _handle_responses(self, body: bytes):
        req = self._parse_body(body)
        if not isinstance(req, dict):
            self.send_api_error(400, "invalid JSON body", ERR_INVALID_REQUEST)
            return
        resolved = self._resolve_request_model(req)
        if resolved is None:
            return
        echo_model, model_name, model_id, think_mode, extra, ticket = resolved

        tools = self._normalize_responses_tools(req.get("tools"))
        messages = self._responses_messages(req.get("input", []), req.get("instructions"))
        # `null` is a legal JSON value here; the spec has no null variant, so
        # fall back to "auto" rather than echoing a null tool_choice back.
        tool_choice = req.get("tool_choice") or "auto"
        prompt, images = messages_to_prompt(messages, tools, tool_choice)

        rf = req.get("response_format")
        if rf is None:
            rf = _responses_text_format(req.get("text"))
        rf_instruction = build_response_format_instruction(rf)
        if rf_instruction:
            prompt = prompt + rf_instruction
        if not prompt.strip():
            self.send_api_error(400, "empty input", ERR_INVALID_REQUEST)
            return

        stop_strings = _normalize_stop(req.get("stop"))
        max_tokens = req.get("max_output_tokens")

        allowed_names = tool_names(tools) or None
        tool_schemas = tool_parameters(tools) or None
        tool_required = tool_required_params(tools) or None
        required_tool = is_required_tool_choice(tool_choice)
        tools_active = bool(tools) and tool_choice != "none"
        want_stream = bool(req.get("stream"))

        # Fields the spec marks required on every response object. Request
        # fields are echoed back unchanged where we honour them, otherwise the
        # spec's own default is used.
        rid = f"resp_{uuid.uuid4().hex[:16]}"
        created_at = int(time.time())
        instructions = req.get("instructions")
        tools_echo = req.get("tools")
        metadata = req.get("metadata")
        parallel = req.get("parallel_tool_calls")
        temperature = req.get("temperature")
        top_p = req.get("top_p")
        # Set before generate (the opening events carry it as null) and
        # reassigned once truncation is known; the closure reads it at call
        # time, so both see the right value.
        incomplete_details = None

        def _number(value, default):
            # `bool` is an ``int`` subclass: `temperature: true` must not be
            # echoed back as a number the schema rejects.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return default
            return value

        def response_fields(status, items, token_usage):
            return {
                "id": rid,
                "object": "response",
                "created_at": created_at,
                "status": status,
                "model": echo_model,
                "output": items,
                "error": None,
                "incomplete_details": incomplete_details,
                "instructions": instructions if isinstance(instructions, str) else None,
                "tools": tools_echo if isinstance(tools_echo, list) else [],
                "parallel_tool_calls": parallel if isinstance(parallel, bool) else True,
                "metadata": metadata if isinstance(metadata, dict) else None,
                "tool_choice": tool_choice,
                "temperature": _number(temperature, 1),
                "top_p": _number(top_p, 1),
                "access_programs": None,
                "usage": token_usage,
            }

        # A buffered turn can spend minutes upstream, and HTTP carries its
        # status line with the first body byte: without the head now, a
        # streaming client aborts on its own header timeout mid-generation.
        stop_ping, client_gone = None, None
        if want_stream:
            stop_ping, client_gone, _hb = self._start_sse_with_keepalive()
            sequence_number = 0

            def emit(event_type, **fields):
                nonlocal sequence_number
                sequence_number += 1
                event = {"type": event_type, "sequence_number": sequence_number,
                         **fields}
                frame = (f"event: {event_type}\n"
                         f"data: {json.dumps(event)}\n\n").encode()
                trace(f"PROXY -> CLIENT (SSE {event_type})", frame)
                self._sse_write(frame)

            emit("response.created", response=response_fields("in_progress", [], None))
            emit("response.in_progress", response=response_fields("in_progress", [], None))
        try:
            file_refs = _upload_images(images)
            text, tool_calls = self._generate_with_tool_retry(
                prompt, model_id, think_mode, file_refs, extra,
                tools_active, allowed_names, tool_schemas, required_tool,
                messages=messages, ticket=ticket, tool_required=tool_required,
                client_gone=client_gone)
        except Exception as e:
            if want_stream:
                try:
                    _status, message, _type, _code = _map_upstream_error(e)
                    failed = response_fields("failed", [], None)
                    failed["error"] = {"message": message}
                    emit("response.failed", response=failed)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stop_ping()
                return
            self.send_api_error(*_map_upstream_error(e))
            return
        if rf_instruction and text:
            text = strip_code_fence(text)
        text = _apply_stop(text, stop_strings)
        text, truncated = _apply_max_tokens(text, max_tokens)
        if not text and not tool_calls:
            if want_stream:
                # Headers are already on the wire: the failure arrives as an
                # event, not a status.
                try:
                    failed = response_fields("failed", [], None)
                    failed["error"] = {"message": "empty response from upstream"}
                    emit("response.failed", response=failed)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stop_ping()
                return
            self.send_api_error(503, "empty response from upstream", ERR_API)
            return

        # Responses reports a cut-off answer as incomplete rather than completed.
        final_status = "incomplete" if truncated else "completed"
        # `incomplete_details` is required on the object and null when the
        # answer is complete; it must not disappear from the payload.
        incomplete_details = {"reason": "max_output_tokens"} if truncated else None
        # A message that max_output_tokens cut off is itself incomplete --
        # leaving it "completed" while the response says "incomplete" is the
        # contradiction strict clients flag.
        message_status = "incomplete" if truncated else "completed"

        mid = f"msg_{uuid.uuid4().hex[:12]}"
        output = []
        if tool_calls:
            for tc in tool_calls:
                output.append({"type": "function_call", "id": tc["id"], "call_id": tc["id"],
                               "name": tc["function"]["name"],
                               "arguments": tc["function"]["arguments"], "status": "completed"})
        if text or not tool_calls:
            output.append({"type": "message", "id": mid, "role": "assistant",
                           "status": message_status,
                           "content": [{"type": "output_text", "text": text or "",
                                        "annotations": [], "logprobs": []}]})

        usage = {
            "input_tokens": len(prompt) // 4,
            "output_tokens": len(text or "") // 4,
            "total_tokens": (len(prompt) + len(text or "")) // 4,
            # Required breakdowns: their absence fails strict SDKs even though
            # the totals are all we can actually measure.
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        }

        # Fields the spec marks required on every response object were bound
        # before generate (above); `created_at`, the echoed request fields and
        # `response_fields` are all reused here.
        if want_stream:
            # The head plus response.created/in_progress went out before
            # upstream started; only the output items and the final event
            # remain, on the same sequence.
            try:
                for output_index, item in enumerate(output):
                    if item["type"] == "function_call":
                        pending_item = {
                            "type": "function_call", "id": item["id"], "call_id": item["call_id"],
                            "name": item["name"], "arguments": "", "status": "in_progress",
                        }
                        emit("response.output_item.added", output_index=output_index, item=pending_item)
                        emit("response.function_call_arguments.delta", item_id=item["id"],
                             output_index=output_index, delta=item["arguments"])
                        emit("response.function_call_arguments.done", item_id=item["id"],
                             output_index=output_index, arguments=item["arguments"])
                        emit("response.output_item.done", output_index=output_index, item=item)
                    elif item["type"] == "message":
                        pending_item = {
                            "type": "message", "id": item["id"], "role": "assistant",
                            "status": "in_progress", "content": [],
                        }
                        emit("response.output_item.added", output_index=output_index, item=pending_item)
                        for content_index, content_part in enumerate(item["content"]):
                            event_fields = {"item_id": item["id"], "output_index": output_index,
                                            "content_index": content_index}
                            emit("response.content_part.added", **event_fields, part={
                                "type": "output_text", "text": "", "annotations": [],
                                "logprobs": []})
                            # `logprobs` is required on both text events (an empty
                            # list: we never see upstream probabilities).
                            emit("response.output_text.delta", **event_fields,
                                 delta=content_part["text"], logprobs=[])
                            emit("response.output_text.done", **event_fields,
                                 text=content_part["text"], logprobs=[])
                            emit("response.content_part.done", **event_fields, part=content_part)
                        emit("response.output_item.done", output_index=output_index, item=item)
                final_event = "response.incomplete" if truncated else "response.completed"
                emit(final_event, response=response_fields(final_status, output, usage))
                self.wfile.flush()
            finally:
                stop_ping()
        else:
            self.send_json(response_fields(final_status, output, usage))

    # ─── /v1beta/models (Google Gemini CLI) ──────────────────────────────────

    def _handle_google_generate(self, body: bytes, stream: bool):
        req = self._parse_body(body)
        if not isinstance(req, dict):
            self.send_google_error(400, "invalid JSON", "INVALID_ARGUMENT")
            return
        m = re.match(r'/v1beta/models/([^:?]+)', self.path)
        model_name = m.group(1) if m else CONFIG["default_model"]
        model_name, model_id, think_mode, err, extra = resolve_model(
            model_name, default=CONFIG["default_model"])
        if err:
            self.send_google_error(400, err, "INVALID_ARGUMENT")
            return
        ticket = ticket_for(model_name)

        tool_config = req.get("toolConfig", {})
        fc_mode = tool_config.get("functionCallingConfig", {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"

        # The review nudge lives in the shared tool block (see _handle_chat);
        # never rewrite the client's own system instruction here.
        prompt, images = google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_google_error(400, "empty content", "INVALID_ARGUMENT")
            return

        try:
            file_refs = _upload_images(images)
        except RuntimeError as e:
            self.send_google_error(502, f"upstream error: {e}", "UNAVAILABLE")
            return
        log(f"Google API: model={model_name} stream={stream} tools={has_tools} prompt_len={len(prompt)}")

        if stream and not has_tools:
            try:
                gen = generate_stream(prompt, model_id, think_mode, file_refs, extra, ticket)
                first = next(gen, _MISSING)
            except Exception as e:
                self.send_google_error(*_map_google_error(e))
                return
            if first is _MISSING:
                self.send_google_error(502, "empty response from upstream", "UNAVAILABLE")
                return
            try:
                self._start_sse()
                full_text = ""
                for delta in self._iter_with_first(first, gen):
                    if not delta:
                        continue
                    full_text += delta
                    chunk_obj = {
                        "candidates": [{"content": {"parts": [{"text": delta}],
                                                    "role": "model"}, "index": 0}],
                        "modelVersion": model_name,
                    }
                    self.wfile.write(
                        f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                final_chunk = {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": {
                        "promptTokenCount": len(prompt) // 4,
                        "candidatesTokenCount": len(full_text) // 4,
                        "totalTokenCount": (len(prompt) + len(full_text)) // 4,
                    },
                    "modelVersion": model_name,
                }
                self.wfile.write(f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log(f"Google stream error: {e}")
                # The 200 is already sent, so the only way to tell the client
                # is a Google-shaped error event on the same stream.
                status, message, gstatus = _map_google_error(e)
                try:
                    err_obj = {"error": {"code": status, "message": message, "status": gstatus}}
                    self.wfile.write(f"data: {json.dumps(err_obj, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            finally:
                close = getattr(gen, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            return

        # The Google path used to call generate() once and hand back whatever
        # came out, so none of the "the model said it would act but did not"
        # protections the OpenAI endpoints have applied here: a client using
        # the native API was the one most likely to be left with prose.
        required_tool = fc_mode == "ANY"
        retry_on_miss = bool(CONFIG.get("tool_retry_on_miss"))
        attempts = _tool_retry_attempts(required_tool, has_tools)
        grepeat = _google_repeatable(req.get("contents")) if has_tools else set()
        g_owed = None
        if has_tools:
            try:
                g_owed = pending_tool_request(_google_pseudo_messages(req))
            except Exception as exc:
                log(f"Google evidence check failed: {exc}")
        # Declared names + schemas for parity with the OpenAI path, plus the
        # mode=ANY allowlist: a call outside `allowedFunctionNames` is not a
        # usable call and must be retried, not returned.
        g_allowed, g_schemas, g_required = None, None, None
        g_allowlist = None
        if has_tools:
            g_allowed, g_schemas, g_required = _google_declared(req)
            fc_config = (req.get("toolConfig", {}) or {}).get(
                "functionCallingConfig", {}) or {}
            allowed_list = fc_config.get("allowedFunctionNames")
            if fc_mode == "ANY" and isinstance(allowed_list, list) and allowed_list:
                g_allowlist = {n for n in allowed_list if isinstance(n, str)}
                g_allowed = ({n for n in (g_allowed or set()) if n in g_allowlist}
                             or g_allowlist)

        # Stream + tools buffers the whole turn (retries included) before the
        # first byte, so the head goes out before upstream starts -- same
        # header-timeout rationale as the OpenAI streaming paths -- and
        # retries are gated on the client still listening.
        stop_ping, client_gone, sse_started = None, None, False
        if stream and has_tools:
            stop_ping, client_gone, self._google_hb = \
                self._start_sse_with_keepalive()
            sse_started = True

        def _google_stream_fail(exc):
            """Report a buffered-turn failure on the already-open stream."""
            status, message, gstatus = _map_google_error(exc)
            try:
                err_obj = {"error": {"code": status, "message": message,
                                     "status": gstatus}}
                self._sse_write(
                    f"data: {json.dumps(err_obj, ensure_ascii=False)}\n\n".encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if stop_ping is not None:
                    stop_ping()
                self._google_hb = None

        def _google_fail(status, message, gstatus):
            if sse_started:
                _google_stream_fail(GeminiError(message, status=status))
            else:
                self.send_google_error(status, message, gstatus)

        text, clean_text, function_calls = "", "", []
        grepeat_nudge = ""
        grepeats = 0
        best_text, best_idx = "", -1
        for attempt in range(attempts):
            if attempt > 0 and client_gone is not None and client_gone.is_set():
                # Same rationale as the chat loop: no billing the account for
                # an answer that reaches nobody.
                log("Google tool retry skipped: client disconnected")
                break
            call_prompt = prompt
            if attempt > 0:
                nudge = (
                    "IMPORTANT: Respond with a function_call block ONLY."
                    if required_tool else
                    "IMPORTANT: Do not describe a function call -- make it. If "
                    "answering requires reading or changing something, call "
                    "the tool now; otherwise give your final answer."
                )
                if grepeat_nudge:
                    nudge += grepeat_nudge
                    grepeat_nudge = ""
                call_prompt = prompt + "\n\n" + nudge
            try:
                if CONFIG.get("tool_stream"):
                    # Same live-heartbeat contract as the OpenAI loop: deltas
                    # reset/emit SSE pings through the sink the endpoint set up
                    # (self._google_hb). Absent sink -> old buffered behaviour.
                    ghb = getattr(self, "_google_hb", None)
                    gsink = (ghb.feed if (ghb is not None
                                          and CONFIG.get("tool_delta_forward", True))
                             else None)
                    raw = _streamed_generate(call_prompt, model_id, think_mode,
                                             file_refs, extra, ticket,
                                             has_tools, attempt,
                                             on_delta=gsink)
                else:
                    raw = generate(call_prompt, model_id, think_mode, file_refs, extra, ticket)
            except Exception as e:
                if attempt + 1 < attempts:
                    log(f"Google tool retry after upstream error: {e}")
                    time.sleep(CONFIG["retry_delay_sec"])
                    continue
                if best_text and not required_tool:
                    log(f"Google tool retry: final attempt failed ({e}); "
                        f"returning best text from attempt {best_idx + 1}")
                    text, clean_text, function_calls = best_text, best_text, []
                    break
                if sse_started:
                    _google_stream_fail(e)
                    return
                raise
            text = raw
            clean_text, function_calls = "", []
            if has_tools and raw:
                clean_text, function_calls = parse_google_function_calls(
                    raw, allowed_names=g_allowed, tool_schemas=g_schemas)
                function_calls = [
                    fc for fc in function_calls
                    if g_allowlist is None or fc.get("name") in g_allowlist
                ]
            if function_calls:
                if g_required:
                    g_missing = missing_required_params(
                        [{"function": {
                            "name": fc.get("name"),
                            "arguments": json.dumps(fc.get("args") or {},
                                                   ensure_ascii=False)}}
                         for fc in function_calls], g_required)
                    if g_missing:
                        log(f"Google tool retry (attempt {attempt + 1}/{attempts}): "
                            f"missing required parameter(s): {', '.join(g_missing)}")
                        if attempt + 1 < attempts:
                            grepeat_nudge = (
                                " The previous function call was missing required "
                                f"parameter(s): {', '.join(g_missing)} -- include "
                                "every required parameter.")
                            continue
                if all((fc.get("name"), _canonical_args(fc.get("args")))
                       in grepeat for fc in function_calls):
                    # Already answered upstream: re-emitting the call loops.
                    if grepeats < 3 and attempt + 1 < attempts:
                        grepeats += 1
                        grepeat_nudge = (" The result of this exact function "
                                         "call is already in the conversation "
                                         "-- answer in plain text, do not call "
                                         "it again.")
                        log(f"Google tool retry (attempt {attempt + 1}/{attempts}): "
                            "duplicate function call already answered, nudging")
                        continue
                    log("Google duplicate function call repeated 3 times (or no "
                        "attempts left): returning the plain-text answer instead")
                    function_calls = []
                    text = clean_text or raw
                    break
                break
            if (raw or "").strip() and not looks_like_canned_failure(raw):
                best_text, best_idx = raw, attempt
            if attempt + 1 >= attempts:
                break
            if not (raw or "").strip():
                log(f"Google tool retry (attempt {attempt + 1}/{attempts}): "
                    "upstream returned an empty response")
                continue
            if looks_like_canned_failure(raw):
                log(f"Google tool retry (attempt {attempt + 1}/{attempts}): "
                    "upstream returned its canned error placeholder instead of "
                    "a response")
                continue
            if required_tool:
                continue
            if retry_on_miss and has_tools and g_owed:
                log(f"Google tool retry (attempt {attempt + 1}/{attempts}): "
                    f"{g_owed}")
                grepeat_nudge = (" You have not gathered enough evidence yet ("
                                 + g_owed + "). Call the read/search tools "
                                 "now instead of answering from memory.")
                continue
            if retry_on_miss and looks_like_missed_tool_call(raw):
                log(f"Google tool retry (attempt {attempt + 1}/{attempts}): "
                    f"response looks like a missed tool call ({len(raw)} chars)")
                continue
            break

        if not text:
            if best_text and not required_tool:
                log(f"Google tool retry: final answer is empty; returning best "
                    f"text from attempt {best_idx + 1}")
                text = best_text
            else:
                _google_fail(502, "empty response from upstream", "UNAVAILABLE")
                return

        # The canned sentence is an upstream failure, not something the model
        # said. Returning it as a candidate tells the client "answered".
        if looks_like_canned_failure(text):
            if best_text and not required_tool:
                log(f"Google tool retry: final answer is the upstream "
                    f"placeholder; returning best text from attempt {best_idx + 1}")
                text = best_text
            else:
                status, message, gstatus = _map_google_error(GeminiError(
                    "upstream returned an error placeholder instead of a response; "
                    "please retry"))
                _google_fail(status, message, gstatus)
                return

        if required_tool and not function_calls:
            # mode=ANY is a contract, like tool_choice=required: prose ends
            # an agent turn that asked for action. Report, don't answer.
            status, message, gstatus = _map_google_error(GeminiError(
                "model did not emit the required function call; please retry"))
            _google_fail(status, message, gstatus)
            return

        response_parts = []
        if has_tools and function_calls:
            if clean_text:
                response_parts.append({"text": clean_text})
            for fc in function_calls:
                response_parts.append({"functionCall": {"name": fc["name"], "args": fc["args"]}})
        else:
            response_parts.append({"text": text})

        candidate = {
            "content": {"parts": response_parts, "role": "model"},
            "finishReason": "STOP",
            "index": 0,
        }
        usage = {
            "promptTokenCount": len(prompt) // 4,
            "candidatesTokenCount": len(text) // 4,
            "totalTokenCount": (len(prompt) + len(text)) // 4,
        }
        response_obj = {
            "candidates": [candidate],
            "usageMetadata": usage,
            "modelVersion": model_name,
        }

        if stream:
            try:
                if not sse_started:
                    self._start_sse()
                self._sse_write(
                    f"data: {json.dumps(response_obj, ensure_ascii=False)}\n\n".encode())
            finally:
                if stop_ping is not None:
                    stop_ping()
                self._google_hb = None
        else:
            self.send_json(response_obj)
_GeminiHandler_gwtr_orig = GeminiHandler._generate_with_tool_retry
_GeminiHandler_read_request_body_orig = GeminiHandler._read_request_body
_GeminiHandler_authorized_orig = GeminiHandler._authorized
_GeminiHandler_start_sse_with_keepalive_orig = GeminiHandler._start_sse_with_keepalive
_GeminiHandler_stream_chat_orig = GeminiHandler._stream_chat
_GeminiHandler_handle_responses_orig = GeminiHandler._handle_responses


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

# ==============================================================================
# Section: __main__.py
# ==============================================================================

"""Entry point: python -m gemini_web2api"""
import argparse
import os
import sys
import uuid

from gemini_web2api.config import CONFIG, load_config, find_config, _config_lock_ctx
from gemini_web2api.models import MODELS, ticket_warnings
from gemini_web2api.gemini import HAS_HTTPX, fetch_latest_bl, log_s1 as log
from gemini_web2api.server import GeminiHandler, ThreadedServer
from gemini_web2api import __version__

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _guard_bind(host, allow_insecure: bool) -> None:
    """Refuse an exposed bind with no API keys unless explicitly opted in.

    Binding a routable interface with ``api_keys: []`` hands your Google
    session to anyone who can reach the port. With no keys configured and
    no ``--allow-insecure``, we generate a one-time random key at startup,
    print it in the banner, and require it for every request -- so the
    Dockerfile can omit ``--allow-insecure``.  Anyone really wanting to
    expose the port passes ``--allow-insecure`` intentionally.
    """
    if allow_insecure:
        return
    if _is_loopback(host):
        return
    if CONFIG.get("api_keys"):
        return

    # Generate a one-time key and require it; Docker users read it from
    # the startup log and use it with Authorization: Bearer <key>. Written to
    # stderr directly, not through log(): with log_requests=false the key
    # would otherwise be generated and required but never shown.
    key = uuid.uuid4().hex[:32] + uuid.uuid4().hex[:32]
    sys.stderr.write(f"Auto-generated API key (print this): {key}\n")
    sys.stderr.flush()
    # Also write to log file if configured, so Docker users can find it even
    # when stderr is swallowed by the launcher.
    log_path = CONFIG.get("log_file")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"Auto-generated API key (print this): {key}\n")
        except OSError:
            pass
    with _config_lock_ctx():
        CONFIG["api_keys"] = [key]


def _maybe_refresh_bl() -> None:
    """Refresh the Gemini build label; stale labels make every request fail.

    Skipped when the operator pinned ``gemini_bl`` via ``auto_update_bl=false``
    -- otherwise startup silently overwrites whatever they configured.
    """
    if not CONFIG.get("auto_update_bl", True):
        return
    fetcher = getattr(sys.modules.get("gemini_web2api.__main__"), "fetch_latest_bl", fetch_latest_bl)
    new_bl = fetcher()
    if new_bl:
        with _config_lock_ctx():
            CONFIG["gemini_bl"] = new_bl


def main():
    parser = argparse.ArgumentParser(description="Gemini Web to OpenAI API")
    parser.add_argument("--host", type=str, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--cookie-file", type=str, default=None)
    parser.add_argument("--proxy", type=str, default=None, help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument(
        "--allow-insecure",
        action="store_true",
        help="start even when binding a non-loopback host with no API keys",
    )
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    args = parser.parse_args()

    config_path = args.config or os.environ.get("GEMINI_WEB2API_CONFIG") or find_config()
    if config_path:
        load_config(config_path)

    if args.host:
        with _config_lock_ctx():
            CONFIG["host"] = args.host
    if args.port:
        with _config_lock_ctx():
            CONFIG["port"] = args.port
    if args.cookie_file:
        with _config_lock_ctx():
            CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        with _config_lock_ctx():
            CONFIG["proxy"] = args.proxy

    # Refresh the Gemini build label; stale labels make every request fail.
    _maybe_refresh_bl()
    from gemini_web2api import flow_patch
    flow_patch.install()

    port = CONFIG["port"]
    host = CONFIG["host"]
    had_configured_keys = bool(CONFIG.get("api_keys"))
    _guard_bind(host, args.allow_insecure)
    server = ThreadedServer((host, port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://{host}:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    auth_enabled = bool(CONFIG.get('api_keys'))
    auth_msg = 'enabled' if auth_enabled else 'DISABLED (any client can call this)'
    if auth_enabled and not had_configured_keys:
        # Auto-generated key case: show that auth is required
        auth_msg = 'enabled (auto-generated key required)'
    print(f"  Auth:      {auth_msg}")
    print(f"  Cookie:    {'yes' if CONFIG.get('cookie_file') else 'none (anonymous)'}")
    print(f"  Proxy:     {CONFIG.get('proxy') or 'system env'}")
    print(f"  Streaming: {'httpx (true streaming)' if HAS_HTTPX else 'urllib (buffered)'}")
    print(f"  BL:        {CONFIG['gemini_bl']}"
          f"{'' if CONFIG.get('auto_update_bl', True) else ' (pinned)'}")
    print(f"  Temporary: {'yes' if CONFIG.get('temporary_chats', False) else 'no'}")
    print()
    for warning in ticket_warnings():
        sys.stderr.write(f"WARNING: {warning}\n")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()


# ==== flowfix11:begin (401 cookie/auth fix; patch.py) ====


def _ff11_auth_recent():
    return time.time() - _FF11_AUTH["ts"] < 60


def _status_error(status):
    err = _status_error_s1(status)
    if status in (401, 403):
        _FF11_AUTH["ts"] = time.time()
        _FF11_AUTH["logged"] = False      # re-log cookie names on the next request
        log("[fix11] Google rejected the session (HTTP %s). Re-export cookies "
            "from a logged-in gemini.google.com tab (need __Secure-1PSID and "
            "__Secure-1PSIDTS) and check cookie_file path: %r"
            % (status, CONFIG.get("cookie_file")))
        err = GeminiError(
            "HTTP %s from Gemini upstream -- cookie rejected or expired; "
            "re-export cookies and check cookie_file" % status, status=status)
    return err


def _ff5_classify(text):
    if _ff11_auth_recent():
        return None            # auth is broken: do not burn 3 calls per request
    return _ff5_classify_s1(text)


def _ff10_classify(text, labels):
    if _ff11_auth_recent():
        return None
    return _ff10_classify_s1(text, labels)
# ==== flowfix11:end ====

# ==== flowfix12:begin (generated by flow_fix12_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: callers resolve these names at call time).

# --------------------------------------------------------------------------
# 1. ToolMap.adapt must not drop declared parameters it did not handle
# --------------------------------------------------------------------------
_FF12_GROUPS = (_PATH_KEYS, _CMD_KEYS, _PATTERN_KEYS, _OLD_KEYS, _NEW_KEYS,
                _CONTENT_KEYS, _OFFSET_KEYS, _LIMIT_KEYS)


def _ff12_group_of(key):
    low = str(key).lower()
    for i, group in enumerate(_FF12_GROUPS):
        if any(low == str(k).lower() for k in group):
            return i
    return None


def _ff12_adapt(self, call):
    fixed, problems = _ToolMap_adapt_orig(self, call)
    try:
        fn = (call or {}).get("function") or {}
        name = fn.get("name") or ""
        props = self.schemas.get(name)
        if not props:
            return fixed, problems
        args = T._tool_arguments_dict(fn)
        ffn = (fixed or {}).get("function") or fn
        out = T._tool_arguments_dict(ffn)
        if not isinstance(args, dict) or not isinstance(out, dict):
            return fixed, problems
        used = {_ff12_group_of(k) for k in out} - {None}
        restored = False
        for k, v in args.items():
            if k in out or k not in props:
                continue
            g = _ff12_group_of(k)
            if g is not None and g in used:
                # the client's own key for this group is set -- unless this is
                # a second, DIFFERENT declared parameter of the same family
                # (Grep: pattern + glob). A value already mirrored under
                # another key means adapt renamed it: skip that one.
                if any(v == ov for ov in out.values()):
                    continue
            out[k] = v
            if g is not None:
                used.add(g)
            restored = True
        if not restored:
            return fixed, problems
        kept = []
        for p in problems:
            if "no usable arguments" in p:
                continue
            if any(("'%s'" % r) in p for r in out):
                continue
            kept.append(p)
        new_call = {**fixed, "function": {
            **ffn, "arguments": json.dumps(out, ensure_ascii=False)}}
        return new_call, kept
    except Exception as e:
        log("[fix12] adapt restore error: %s" % e)
        return fixed, problems


if not getattr(ToolMap.adapt, "_ff12", False):
    _ff12_adapt._ff12 = True
    ToolMap.adapt = _ff12_adapt


# --------------------------------------------------------------------------
# 2. Never ask the client a clarification question in the middle of a flow
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 3. The operator's own flow_test_command is read-only by definition
#    (Unified into _ff5_cmd_readonly above)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 4. Windows detection from real evidence, not from a stray word
# --------------------------------------------------------------------------
_FF12_ENV_RE = re.compile(
    r"Platform:\s*(?:win32|windows)|\bOS:\s*Windows|Operating\s+System:\s*Windows"
    r"|OS\s+Version:\s*Windows|\b(?:on|is|platform:?)\s+win32\b|\bWindows\s+NT\b"
    r"|<shell>\s*(?:powershell|pwsh|cmd)"
    r"|(?:<cwd>|\bcwd\b\W{0,4}|working\s+directory\W{0,4}|\bworkdir\W{0,4})\s*[A-Za-z]:[\\/]",
    re.I)
_FF12_ENVCTX_RE = re.compile(r"<environment_context>(.*?)</environment_context>", re.S | re.I)
_FF12_PS_ERR_RE = re.compile(
    r"(?m)^\s*(?:ParserError\b|.*CategoryInfo\s*:\s*ParserError)"
    r"|token '&&' is not a valid statement separator", re.I)
_FF12_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _ff5_is_windows_s1(messages):
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        body = T._stringify_content(msg.get("content", ""))
        if role in ("system", "developer") and _FF12_ENV_RE.search(
                body[:20000] + "\n" + body[-20000:]):
            return True
        if role == "user":
            # Codex-style clients put the environment in a user message
            for blk in _FF12_ENVCTX_RE.findall(body[:20000]):
                if _FF12_ENV_RE.search(blk):
                    return True
        if role in ("tool", "user") and len(body) < 4000 \
                and _FF12_PS_ERR_RE.search(body):
            return True
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = T._tool_arguments_dict(fn)
            for k in list(_PATH_KEYS) + ["workdir", "cwd"]:
                v = args.get(k)
                if isinstance(v, str) and _FF12_DRIVE_RE.match(v.strip()):
                    return True
    return False


# --------------------------------------------------------------------------
# 5. An explanation question is not a request to run the command it mentions
#    ("explain what happens when I run `git reset --hard`")
# --------------------------------------------------------------------------
_FF12_RUN_PATTERNS = ("RUN_COMMAND", "RUN_TESTS", "GIT_INFO")
_FF12_EXPLAIN_CUE_RE = re.compile(
    r"\b(?:explain|describe|what\s+(?:happens|does|will|would|is|are)|why|how\s+does"
    r"|is\s+it\s+safe|giai\s+thich|mo\s+ta|dieu\s+gi\s+(?:se\s+)?xay\s+ra"
    r"|chuyen\s+gi\s+(?:se\s+)?xay\s+ra|tai\s+sao|vi\s+sao|nghia\s+la\s+gi"
    r"|se\s+(?:nhu\s+the\s+nao|ra\s+sao|lam\s+gi)|co\s+an\s+toan\s+khong)\b")
_FF12_COND_RE = re.compile(
    r"\b(?:when|whenever|if|after|before|once|while|as\s+soon\s+as|khi|neu|luc"
    r"|sau\s+khi|truoc\s+khi|moi\s+khi)\b")


def _ff12_explain_only_s1(ctx):
    """True when the message asks to understand something and every run verb
    in it sits in a conditional clause (when/if/khi/neu ...)."""
    folded = " ".join(str(getattr(ctx, "folded", "") or "").lower().split())
    if not folded or not _FF12_EXPLAIN_CUE_RE.search(folded):
        return False
    for m in _FF5_RUNV_RE.finditer(folded):
        head = re.split(r"[.;!?\n,]", folded[:m.start()])[-1]
        if not _FF12_COND_RE.search(head):
            return False          # an imperative "run ..." is in there
    return True




def _ff5_detect(ctx):
    det = _ff5_detect_s2(ctx)
    if det in _FF12_RUN_PATTERNS and _ff12_explain_only(ctx):
        log("[fix12] explanation question -> not a %s request" % det)
        ctx.cmd = ""
        ctx.git_cmd = ""
        return None
    return det


def _ff5_apply_label(ctx, label):
    if label in _FF12_RUN_PATTERNS and _ff12_explain_only(ctx):
        log("[fix12] classifier label %s ignored: explanation question" % label)
        return
    return _ff5_apply_label_s2(ctx, label)


# --------------------------------------------------------------------------
# 6. tool_choice=required only forces a tool call; it must not widen rights.
#    wrapper5 skips the whole analysis when required_tool is set, so a write
#    proposed during "review a.py" went through unchecked. This outer wrapper
#    runs the same analysis + read guard + post-filter for required calls.
# --------------------------------------------------------------------------
def _wrap_ff12(cur):
    @functools.wraps(cur)
    def wrapper12(self, prompt, model_id, think_mode, file_refs, extra,
                  tools_active, allowed_names, tool_schemas, required_tool,
                  messages=None, ticket=None, tool_required=None,
                  client_gone=None):
        def call(p=prompt):
            return cur(self, p, model_id, think_mode, file_refs, extra,
                       tools_active, allowed_names, tool_schemas, required_tool,
                       messages=messages, ticket=ticket,
                       tool_required=tool_required, client_gone=client_gone)

        if not (required_tool and _c5("flow5_enabled") and tools_active
                and messages and allowed_names):
            return call()
        _ff5_tls.model = (model_id, think_mode, extra, ticket)
        tm, perm, ctx = None, None, None
        try:
            last = messages[-1] if isinstance(messages[-1], dict) else {}
            if last.get("role") in ("user", "tool"):
                tm = ToolMap(allowed_names, tool_schemas, tool_required)
                ctx = _analyze(messages)
                if ctx is not None:
                    perm = getattr(ctx, "perm", None)
                    guard = _ff5_read_guard(ctx, messages)
                    if guard:
                        log("[fix12] required call: read failed before an edit -> stop")
                        return guard, None
        except Exception as e:
            log("[fix12] required-call analysis error: %s" % e)
        result = call()
        try:
            retries = max(0, int(_c5("flow5_guard_retries")))
            for attempt in range(retries + 1):
                final, nudge = _ff5_postfilter(
                    result, messages, perm, last=(attempt >= retries), tm=tm, ctx=ctx)
                if nudge is None:
                    return final
                log("[fix12] required call rejected, re-asking: %s" % nudge[:160])
                result = call(prompt + "\n\n[SYSTEM NOTE] " + nudge)
            return result
        except Exception as e:
            log("[fix12] required-call postfilter error: %s" % e)
            return result

    wrapper12._ff12_required = True
    return wrapper12


# --------------------------------------------------------------------------
# 7. Exact file identity for the read-before-edit and write-scope guards.
#    _path_seen() matches modulo directory prefix, so "backup/src/a.py" counted
#    as "src/a.py" both as evidence of a read and as permission to write.
# --------------------------------------------------------------------------
def _ff12_canon(p):
    s = str(p or "").strip().replace("\\", "/")
    s = re.sub(r"/{2,}", "/", s)
    while s.startswith("./"):
        s = s[2:]
    s = s.replace("/./", "/")
    if len(s) > 1:
        s = s.rstrip("/")
    return s.lower()


def _ff12_isabs(s):
    return s.startswith("/") or bool(re.match(r"^[a-z]:/", s))


def _ff12_same_file(a, b):
    """Same file: equal after canonicalisation, or one absolute and the other
    a relative path that is a tail of it. Two relative (or two absolute) paths
    that differ are different files."""
    x, y = _ff12_canon(a), _ff12_canon(b)
    if not x or not y:
        return False
    if x == y:
        return True
    xa, ya = _ff12_isabs(x), _ff12_isabs(y)
    if xa and not ya:
        return x.endswith("/" + y)
    if ya and not xa:
        return y.endswith("/" + x)
    return False


# Semantic aliases for path helpers (Phase 5.1)
canonical_path = _ff12_canon
path_base = _base
is_absolute_path = _ff12_isabs
is_same_path = _ff12_same_file
path_matches_suffix = _path_seen


def _ff12_scope_match(path, entry):
    """Scope entry written by the user: a bare name ("a.py") may live in any
    folder, a name with a folder is exact."""
    if _ff12_same_file(path, entry):
        return True
    e = _ff12_canon(entry)
    return bool(e) and "/" not in e and _ff12_canon(path).rsplit("/", 1)[-1] == e


def _ff5_in_scope(path, scope):
    if _base(path) == _MERGE_SCRIPT:
        return True
    if not scope:
        return False
    return any(_ff12_scope_match(path, x) for x in scope)


# --------------------------------------------------------------------------
# 8. "Not in a filtered listing" is not proof that a file does not exist.
#    Only a read that said not-found, or a COMPLETE listing, proves a new file
#    (flowfix8 applied this to CREATE only; CHANGE could overwrite tests).
# --------------------------------------------------------------------------
def _ff6c_proven_new(path, messages):
    ev = _answered_calls(messages)
    for e in ev:
        p = _call_path(e["args"])
        if (e["kind"] == "read" and p and _ff12_same_file(path, p)
                and _ff5_status(e) in ("success", "partial")):
            return False
    for e in ev:
        p = _call_path(e["args"])
        if (e["kind"] == "read" and p and _ff12_same_file(path, p)
                and _ff5_status(e) == "failed"
                and _FF6C_NOTFOUND_RE.search((e["result"] or "")[:300])):
            return True
    try:
        return bool(_ff8_listing_absent(path, ev))
    except Exception:
        return False


_CREATE_TEST_INTENT_RE = re.compile(
    r"\b(?:tao|viet|them|create|write|add)\s+(?:file\s+|tep\s+|unit\s+)?test\b",
    re.IGNORECASE,
)


def _ff12_user_creates(path, ctx, listing):
    """Fix 19: the user or plan said to create/write this file and no listing shows it."""
    try:
        if listing and any(_ff12_same_file(path, f) for f in listing):
            return False
        base = _base(path).lower()
        norm_p = _norm(path).lower()
        created = T._created_files(ctx.scannable, ctx.folded)
        if base in created or norm_p in created:
            return True
        msgs = getattr(_ff8_tls, "messages", None)
        if msgs is not None and "_ff16_plan_files" in globals():
            plan_files = _ff16_plan_files(ctx, msgs, listing)
            if any(_ff12_same_file(path, pf) for pf in plan_files):
                return True
        if _is_test_path(path) and _CREATE_TEST_INTENT_RE.search(ctx.folded or ""):
            return True
        return False
    except Exception:
        return False


def _ff5_act_needs_read_problems(calls, messages, ctx):
    pattern = getattr(ctx, "pattern", None)
    fix = bool(getattr(ctx, "fix", False))
    if pattern not in ("CHANGE", "DEBUG", "CREATE"):
        return []
    if pattern == "DEBUG" and not fix:
        return []
    tracked = _ff9_read_texts(messages) if "_ff9_read_texts" in globals() else {}
    good = list(set(_ff5_successful_read_paths(messages)).union(tracked.keys()))
    listing = _ff6c_survey_listing(_answered_calls(messages))
    problems = []
    for tc in calls or []:
        fn = (tc or {}).get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue
        args = T._tool_arguments_dict(fn)
        path = _call_path(args)
        if not path or _base(path) == _MERGE_SCRIPT:
            continue
        if any(_ff12_same_file(path, p) for p in good):
            continue
        is_write = _ff6c_is_write(args)
        if pattern == "CREATE":
            exists = bool(listing) and any(_ff12_same_file(path, f) for f in listing)
            if is_write and exists:
                problems.append(
                    f"{path}: this file already exists -- read it first, or "
                    "choose a different file name")
            elif not is_write:
                problems.append(
                    f"{path}: cannot edit this file before a successful read of it")
            continue
        if is_write and (_ff6c_proven_new(path, messages)
                         or _ff12_user_creates(path, ctx, listing)):
            continue
        hint = (" If this is a NEW file, first prove it does not exist (a read "
                "that returns 'not found', or an unfiltered file listing); if it "
                "exists, read it first." if is_write else "")
        problems.append(
            f"{path}: cannot edit/write before a successful read of this file"
            + hint)
    return problems


# --------------------------------------------------------------------------
# 9. A successful read of one file must not hide a failed read of another file
#    the user named (review a.py and b.py; b.py -> permission denied).
# --------------------------------------------------------------------------


def _ff5_evidence_gate(ctx, messages, tm):
    step, stop = _ff5_evidence_gate_s3(ctx, messages, tm)
    if step is not None or stop is not None:
        return step, stop
    try:
        pattern = getattr(ctx, "pattern", None) if ctx is not None else None
        if not pattern or pattern in (
                "CREATE", "TOOL_DEMAND", "REVERT", "RUN_COMMAND", "RUN_TESTS",
                "GIT_INFO"):
            return step, stop
        named = [f for f in _named_files(ctx) if f]
        if not named:
            return step, stop
        reads = [e for e in _answered_calls(messages)
                 if e["kind"] == "read" and _call_path(e["args"])]
        try:
            created = T._created_files(ctx.scannable, ctx.folded)
        except Exception:
            created = set()
        bad = []
        for n in named:
            mine = [e for e in reads if _ff12_scope_match(_call_path(e["args"]), n)]
            if not mine or any(_ff5_status(e) in ("success", "partial") for e in mine):
                continue
            failed = [e for e in mine if _ff5_status(e) == "failed"]
            if failed and n.lower() in created and all(
                    _FF6C_NOTFOUND_RE.search((e["result"] or "")[:300])
                    for e in failed):
                continue    # file the user asked to create: missing is expected
            if failed:
                bad.append(failed[-1])
        if not bad:
            return step, stop
        detail = "; ".join(_ff5_status_detail(e) for e in bad[:3])
        if _ff5_vi(ctx):
            return None, ("Không đọc được file bạn nêu nên mình không trả lời như "
                          "thể đã xem nó (" + detail + "). Hãy kiểm tra đường dẫn "
                          "hoặc quyền truy cập rồi yêu cầu lại.")
        return None, ("I could not read a file you named, so I will not answer as if "
                      "I had seen it (" + detail + "). Check the path or permissions "
                      "and ask again.")
    except Exception as e:
        log("[fix12] named-file read check error: %s" % e)
        return step, stop




# --------------------------------------------------------------------------
# 10. A command that is still running is not a finished command.
#     exec_command -> "Process running with session ID 1234": poll it
#     (write_stdin / BashOutput ...) until it exits instead of reporting now.
# --------------------------------------------------------------------------
_FF12_EXIT_RE = re.compile(
    r"(?i)process\s+exited\s+with\s+code\s+-?\d+|\bexit(?:ed)?\s+(?:with\s+)?code\s*[:=]?\s*-?\d+"
    r"|\bexit\s+status\s*[:=]?\s*-?\d+|\[exit\s+code:?\s*-?\d+\]"
    r"|<exit_?code>\s*-?\d+|<status>\s*(?:completed|failed|killed)\s*</status>")
_FF12_RUNNING_RE = re.compile(
    r"(?i)\b(?:still\s+running|process\s+running|running\s+with\s+session"
    r"|running\s+in\s+(?:the\s+)?background|background\s+(?:shell|process))"
    r"|<status>\s*running\s*</status>")
_FF12_SID_RE = re.compile(
    r"(?i)(?:running\s+with\s+session\s*(?:id)?|session[_\s]*id|shell[_\s]*id"
    r"|bash[_\s]*id|background\s+with\s+id)\s*[:=]?\s*([A-Za-z0-9_-]+)")
_FF12_POLL_NAME_RE = re.compile(
    r"(?i)write_?stdin|bash_?output|read_?output|process_?output|get_?output"
    r"|check_?output|shell_?output|poll")
_FF12_SID_KEYS = ["session_id", "sessionId", "shell_id", "bash_id", "process_id",
                  "processId", "id", "pid"]
_FF12_CHARS_KEYS = ["chars", "input", "text", "stdin", "data"]


def _ff12_pending_sid(text):
    t = text or ""
    runs = [m.start() for m in _FF12_RUNNING_RE.finditer(t)]
    if not runs:
        return None
    ids = list(_FF12_SID_RE.finditer(t))
    if not ids:
        return None
    exits = [m.start() for m in _FF12_EXIT_RE.finditer(t)]
    if exits and max(exits) > max(runs):
        return None
    return ids[0].group(1)


def _ff12_poll_sid_arg(args):
    for k in _FF12_SID_KEYS:
        if isinstance(args, dict) and args.get(k) not in (None, ""):
            return str(args[k])
    return None


def _ff12_is_poll_call(tc):
    fn = (tc or {}).get("function") or {}
    return bool(_FF12_POLL_NAME_RE.search(fn.get("name") or "")
                and _ff12_poll_sid_arg(T._tool_arguments_dict(fn)))


def _ff12_merge_polls(messages):
    """Copy of ``messages`` where each poll call/result is folded into the
    result of the exec call that started the session."""
    calls = {}
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    fn = tc.get("function") or {}
                    calls[tc["id"]] = (fn.get("name") or "", T._tool_arguments_dict(fn))
    new = list(messages)
    owner, drop_ids = {}, set()
    for j, m in enumerate(messages):
        if not (isinstance(m, dict) and m.get("role") == "tool"):
            continue
        hit = calls.get(m.get("tool_call_id"))
        if not hit:
            continue
        name, args = hit
        res = T._stringify_content(m.get("content"))
        if _FF12_POLL_NAME_RE.search(name):
            sid = _ff12_poll_sid_arg(args)
            if sid is not None and sid in owner:
                k = owner[sid]
                new[k] = {**new[k], "content":
                          T._stringify_content(new[k].get("content")) + "\n" + res}
                drop_ids.add(m.get("tool_call_id"))
                if _ff12_pending_sid(res) is None:
                    owner.pop(sid, None)
            continue
        if tool_kind(name) == "exec":
            sid = _ff12_pending_sid(res)
            if sid is not None:
                owner[sid] = j
    if not drop_ids:
        return messages
    out = []
    for m in new:
        if isinstance(m, dict) and m.get("role") == "tool" \
                and m.get("tool_call_id") in drop_ids:
            continue
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            keep = [tc for tc in m["tool_calls"]
                    if not (isinstance(tc, dict) and tc.get("id") in drop_ids)]
            if len(keep) != len(m["tool_calls"]):
                if not keep and not T._stringify_content(m.get("content")).strip():
                    continue
                m = {**m, "tool_calls": keep}
                if not keep:
                    m.pop("tool_calls")
        out.append(m)
    return out


def _ff12_make_poll(tm, name, sid):
    props = tm.schemas.get(name) or ()
    skey = _pick(props, _FF12_SID_KEYS) or (None if props else "session_id")
    if not skey:
        return None
    args = {skey: int(sid) if str(sid).isdigit() else sid}
    ckey = _pick(props, _FF12_CHARS_KEYS)
    if ckey:
        args[ckey] = ""
    ykey = _pick(props, ["yield_time_ms", "yieldTimeMs", "wait_ms", "timeout_ms"])
    if ykey:
        args[ykey] = 10000
    for req in tm.required.get(name, ()) or ():
        if req in args:
            continue
        if str(req).lower() in _FF12_CHARS_KEYS:
            args[req] = ""
        else:
            return None
    return {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _ff12_pending_step(ctx, raw, merged, tm, pending_text):
    """Poll step (or an honest "still running" answer) for a pending result."""
    sid = _ff12_pending_sid(pending_text)
    if sid is None:
        return None
    done = sum(1 for e in _answered_calls_s1(raw)
               if _FF12_POLL_NAME_RE.search(e["name"] or "")
               and _ff12_poll_sid_arg(e["args"]) == sid)
    names = [n for lst in tm.by_kind.values() for n in lst
             if _FF12_POLL_NAME_RE.search(n)]
    if done < int(CONFIG.get("flow12_poll_max", 12)):
        for n in sorted(names, key=lambda x: (len(x), x)):
            call = _ff12_make_poll(tm, n, sid)
            if call:
                return _Ff5Step("exec", calls=[call])
    vi = _ff5_vi(ctx)
    note = ("Lệnh VẪN ĐANG CHẠY (session %s), chưa có kết quả cuối; đây là output "
            "đến lúc này:" if vi else
            "The command is STILL RUNNING (session %s), so there is no final "
            "result yet; output so far:") % sid
    return _Ff5Step("say", text=note + "\n" + _ff5_block(pending_text, 1600))


def _ff12_last_pending(ev, pred):
    last = None
    for e in ev:
        if e["kind"] == "exec" and not _FF12_POLL_NAME_RE.search(e["name"] or "") \
                and pred(e):
            last = e
    return last




def _ff5_plan_command(ctx, messages, tm, cmd, label):
    try:
        merged = _ff12_merge_polls(messages)
        want = _ff5_fix_for(cmd, messages).strip()
        e = _ff12_last_pending(_answered_calls(merged),
                               lambda x: _call_command(x["args"]).strip() == want)
        if e is not None:
            step = _ff12_pending_step(ctx, messages, merged, tm, e["result"])
            if step is not None:
                return step
        messages = merged
    except Exception as ex:
        log("[fix12] poll planner error: %s" % ex)
    return _ff5_plan_command_s1(ctx, messages, tm, cmd, label)


def _ff5_plan_tests(ctx, messages, tm):
    try:
        merged = _ff12_merge_polls(messages)
        runs = _test_runs(_answered_calls(merged))
        if runs:
            step = _ff12_pending_step(ctx, messages, merged, tm, runs[-1][2])
            if step is not None:
                return step
        messages = merged
    except Exception as ex:
        log("[fix12] poll planner error: %s" % ex)
    return _ff5_plan_tests_s1(ctx, messages, tm)


def _ff5_own_result(step, ctx, messages, tm, perm):
    try:
        calls = list(getattr(step, "calls", None) or [])
        if (step is not None and step.text is None and calls
                and all(_ff12_is_poll_call(tc) for tc in calls)):
            # polling a command this request already started is not an "edit"
            if perm is not None and perm.run == "none":
                return _ff5_say(
                    ctx, "Bạn không cho phép chạy lệnh nên mình không chạy bước này.",
                    "Running commands is not allowed here, so this step was skipped."
                ).text, None
            return "", calls
    except Exception as ex:
        log("[fix12] poll own-result error: %s" % ex)
    return _ff5_own_result_s2(step, ctx, messages, tm, perm)


# --------------------------------------------------------------------------
# 11. Session / poll tools are neither edits nor commands.
#     write_stdin (Codex) was classified "act": in review / explain / debug it
#     was dropped as a file write ("Edits are not allowed here"), and in an
#     implement flow every poll counted as a NEW edit, so diff -> tests -> poll
#     -> diff -> tests ... looped forever. BashOutput / KillShell (Claude Code)
#     were classified "exec" with no command, so adapt() rejected them
#     ("missing required parameter for the command") and the whole reply was
#     replaced by a schema error.
# --------------------------------------------------------------------------
_FF12_SESSION_TOOL_RE = re.compile(
    r"(?i)write_?stdin|send_?stdin|bash_?output|kill_?shell|kill_?bash|stop_?shell"
    r"|read_?output|process_?output|get_?output|check_?output|shell_?output|\bpoll")
_FF12_STDIN_TOOL_RE = re.compile(r"(?i)write_?stdin|send_?stdin")


def tool_kind_s2(name):
    if _FF12_SESSION_TOOL_RE.search(str(name or "")):
        return "other"
    return tool_kind_s1(name)


def _ff12_stdin_payload(args):
    if not isinstance(args, dict):
        return ""
    for k in _FF12_CHARS_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip("\r\n \t\x03\x04"):
            return v
    return ""


def _ff12_stdin_problem(calls, perm):
    """Typing text into a running process is running a command: it must obey
    the same run permission as exec (an empty / Ctrl-C write is only a poll)."""
    run = getattr(perm, "run", "any") if perm is not None else "any"
    if run == "any":
        return ""
    for tc in calls or []:
        fn = (tc or {}).get("function") or {}
        if not _FF12_STDIN_TOOL_RE.search(fn.get("name") or ""):
            continue
        payload = _ff12_stdin_payload(T._tool_arguments_dict(fn))
        if not payload:
            continue
        try:
            ok = run != "none" and _ff5_cmd_readonly(payload.strip())
        except Exception:
            ok = False
        if not ok:
            return payload.strip()[:80]
    return ""




def _ff5_postfilter_s3(result, messages, perm=None, last=True, tm=None, ctx=None):
    # returns (final, nudge); final = (text, calls)
    try:
        calls = result[1] if isinstance(result, tuple) and len(result) == 2 else None
        bad = _ff12_stdin_problem(calls, perm)
        if bad:
            log("[fix12] stdin to a running process blocked: %r" % bad)
            return (_ff5_say(
                ctx,
                "Mình không gửi đầu vào này vào tiến trình đang chạy vì nó là một "
                "lệnh không chỉ-đọc / không được phép ở đây (%s)." % bad,
                "I did not send this input to the running process: it is a "
                "command that is not read-only / not allowed here (%s)." % bad
            ).text, None), None
    except Exception as ex:
        log("[fix12] stdin guard error: %s" % ex)
    return _ff5_postfilter_s2(result, messages, perm, last, tm, ctx)


# --------------------------------------------------------------------------
# 12. Merge poll results into the command that started the session EVERYWHERE
#     (implement verify step, review/debug test step, evidence checks), not
#     only inside the RUN_TESTS / RUN_COMMAND planners. Otherwise the tests
#     stay "Process running ..." forever and the planner re-runs them.
#     (_ff12_raw_answered_calls keeps the unmerged view for counting polls.)
# --------------------------------------------------------------------------
def _answered_calls(messages):
    try:
        return _answered_calls_s1(_ff12_merge_polls(messages))
    except Exception as ex:
        log("[fix12] poll merge error: %s" % ex)
        return _answered_calls_s1(messages)


# --------------------------------------------------------------------------
# 13. Failed reads / failed edits that the older detectors mistook for success.
#     Claude Code reports errors as "<tool_use_error>File does not exist.</...>",
#     "File does not exist. Current working directory: ...", "File content (N
#     tokens) exceeds maximum allowed tokens ...", "EISDIR ...". None of these
#     start with the words _READ_FAIL_RE / _FF5_READFAIL_RE expect, so the read
#     counted as SUCCESSFUL: the evidence gate, read-before-edit guard and the
#     review/explain "answer only from what was read" rule all passed on an
#     error string, and a whole-file write() over a file that was never seen
#     (too large to read) was allowed -> silent data loss. Likewise an edit
#     rejected with "File has not been read yet" / "modified since read" was
#     counted as applied, so diff -> tests ran against an unchanged file.
# --------------------------------------------------------------------------
_FF12_TOOLERR_RE = re.compile(r"(?i)^\s*<tool_use_error>")
_FF12_READFAIL_RE = re.compile(
    r"(?is)^\s*(?:<tool_use_error>\s*)?(?:error\b[^\n:]{0,40}:\s*)?(?:"
    r"(?:the\s+)?(?:file|path|directory|folder)\b[^\n]{0,200}?"
    r"(?:does\s+not\s+exist|doesn't\s+exist|not\s+found|cannot\s+be\s+found"
    r"|is\s+a\s+directory|is\s+not\s+a\s+file|permission\s+denied|access\s+denied)"
    r"|file\s+content\s*\([^)]{0,60}\)\s*exceeds\s+(?:the\s+)?maximum"
    r"|(?:file|content)\s+(?:is\s+)?too\s+(?:large|big)"
    r"|(?:eacces|eisdir|eperm)\b|is\s+a\s+directory|access\s+is\s+denied)")
_FF12_ACTFAIL_RE = re.compile(
    r"(?i)^\s*<tool_use_error>|has\s+not\s+been\s+read\s+yet"
    r"|(?:modified|changed)\s+since\s+(?:it\s+was\s+)?(?:last\s+)?read"
    r"|apply_patch\s+verification\s+failed")


def _ff12_read_failed_text(res):
    t = res or ""
    if _FF12_TOOLERR_RE.match(t):
        return True
    return len(t) < 1500 and bool(_FF12_READFAIL_RE.match(t))




def _read_ok(e):
    if not _read_ok_s1(e):
        return False
    return not _ff12_read_failed_text(e.get("result"))




def _act_ok(e):
    if _FF12_ACTFAIL_RE.search((e.get("result") or "")[:400]):
        return False
    return _act_ok_s1(e)


# Semantic aliases for tool result status (Phase 5.2)
read_event_status = _read_ok
act_event_is_success = _act_ok
exec_event_status = _ff5_status


# --------------------------------------------------------------------------
# 14. Poll a still-running test command in EVERY flow, not only RUN_TESTS /
#     RUN_COMMAND. Section 10 only wired the poll into those two planners;
#     in implement (CHANGE/CREATE), debug and review the flow went from
#     "tests" straight to "answer" with the session still "Process running ...",
#     so the answer was written without a test result (and could claim a pass).
#     Here: when the last test command of this turn is pending, the next step
#     is a proxy-generated poll (up to flow12_poll_max); if the budget is used
#     up the answer step is told the run is unfinished.
# --------------------------------------------------------------------------
def _ff12_flow_pending(messages):
    merged = _ff12_merge_polls(messages)
    last = None
    for e in _answered_calls_s1(merged):
        if e["kind"] == "exec" and not _FF12_POLL_NAME_RE.search(e["name"] or ""):
            last = e
    if last is None:
        return None, None
    cmd = _call_command(last["args"])
    if not _is_test_cmd(cmd):
        return None, None
    sid = _ff12_pending_sid(last["result"])
    return (sid, last["result"]) if sid is not None else (None, None)




def plan_for(messages, tm, ctx=None):
    step = plan_for_s3(messages, tm, ctx)
    try:
        if (step is None or getattr(step, "local_text", None) is not None
                or not messages or not isinstance(messages[-1], dict)
                or messages[-1].get("role") != "tool"):
            return step
        pat = getattr(step, "pattern", None) or getattr(ctx, "pattern", None)
        if pat in _FF5_OWN or tm is None:
            return step
        sid, _text = _ff12_flow_pending(messages)
        if sid is None:
            return step
        done = sum(1 for e in _answered_calls_s1(messages)
                   if _FF12_POLL_NAME_RE.search(e["name"] or "")
                   and _ff12_poll_sid_arg(e["args"]) == sid)
        if done < int(CONFIG.get("flow12_poll_max", 12)):
            names = [n for lst in tm.by_kind.values() for n in lst
                     if _FF12_POLL_NAME_RE.search(n)]
            for n in sorted(names, key=lambda x: (len(x), x)):
                call = _ff12_make_poll(tm, n, sid)
                if call:
                    poll = Step(pat or "CHANGE", "tests",
                                getattr(step, "index", 4), getattr(step, "total", 5),
                                {"exec", "other"},
                                "The test command is still running; poll it for the result.",
                                [call], True, [n])
                    poll.stage = getattr(step, "stage", None) or "verify"
                    log("[fix12] test session %s still running -> poll %d" % (sid, done + 1))
                    return poll
        if step.name == "answer":
            vi = _ff5_vi(ctx) if ctx is not None else False
            step.instruction = (step.instruction or "") + (
                "\nLƯU Ý: lệnh test VẪN ĐANG CHẠY (session %s), chưa có kết quả cuối. "
                "KHÔNG được nói test đã pass hay fail; nói rõ là chưa có kết quả." % sid
                if vi else
                "\nNOTE: the test command is STILL RUNNING (session %s) and has no final "
                "result. Do NOT say the tests passed or failed; say the result is not "
                "available yet." % sid)
    except Exception as ex:
        log("[fix12] flow poll error: %s" % ex)
    return step


# --------------------------------------------------------------------------
# 15. PowerShell rewrite ("a && b" -> "a; if ($?) { b }") only for PowerShell.
#     Section 4 treats every Windows hint as PowerShell, but the rewritten
#     command is a syntax error in cmd.exe and in Git Bash. Clients state their
#     shell (Codex <shell>, Cline/Roo "Default Shell:"): honour it. A real
#     PowerShell parse error in the history still forces the rewrite. Long
#     system prompts (Cline/Roo, > 20000 chars) put the environment block at the
#     END, so the tail is scanned as well.
# --------------------------------------------------------------------------
_FF12_SHELL_RE = re.compile(
    r"(?i)(?:<shell>\s*|default\s+shell\s*:\s*|current\s+shell\s*:\s*|\bshell\s*:\s*)([^\n<]{1,80})")
_FF12_PSSHELL_RE = re.compile(r"(?i)power\s*shell|pwsh")


def _ff12_env_bodies(messages):
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        body = T._stringify_content(msg.get("content", ""))
        if role in ("system", "developer"):
            yield body[:20000] + "\n" + body[-20000:]
        elif role == "user":
            blocks = _FF12_ENVCTX_RE.findall(body[:20000])
            if blocks:
                yield "\n".join(blocks)


def _ff12_shell_hint(messages):
    last = None
    for body in _ff12_env_bodies(messages):
        for mm in _FF12_SHELL_RE.finditer(body):
            last = mm.group(1).strip()
    return last


def _ff12_has_ps_error(messages):
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") in ("tool", "user"):
            body = T._stringify_content(msg.get("content", ""))
            if len(body) < 4000 and _FF12_PS_ERR_RE.search(body):
                return True
    return False




def _ff5_is_windows(messages):
    if not _ff5_is_windows_s1(messages):
        return False
    if _ff12_has_ps_error(messages):
        return True
    shell = _ff12_shell_hint(messages)
    if shell is not None and not _FF12_PSSHELL_RE.search(shell):
        return False
    return True
# ==== flowfix12:end ====



# ==== flowfix13:begin (generated by gemini_flow_patch.py -- re-run to refresh, --revert to remove) ====
import collections as _rf_collections
import threading as _rf_threading
import time as _rf_time

# --------------------------------------------------------------------------
# 1. Nhận diện yêu cầu sửa / implement code (EN + VI không dấu sau khi fold)
# --------------------------------------------------------------------------
_RF_EN_VERBS = (r"fix|repair|implement|add|introduce|update|upgrade|change|modify|refactor|"
                r"remove|delete|drop|double|halve|increase|decrease|migrate|extend|enhance|"
                r"patch|rewrite|tweak|adjust|improve|harden")
_RF_VI_VERBS = (r"bo\s+sung|cai\s+dat|chinh\s+sua|chinh|thay\s+doi|viet\s+them|lam\s+them|"
                r"xay\s+dung|trien\s+khai|toi\s+uu|nang\s+cap|gan\s+them|bo\s+tro|"
                r"va\s+(?:loi|bug|code|ham|flow)|sua\s+loi|khac\s+phuc|sua|them|cap\s+nhat")
_RF_NOUNS = (r"source|code\w*|implementation|repository|repo\w*|proxy|server|service|app|"
             r"module\w*|package|project|config\w*|setting\w*|option\w*|endpoint\w*|rout\w*|"
             r"test\w*|retr\w*|auth\w*|health\w*|bug\w*|securit\w*|vulnerab\w*|features?|"
             r"functions?|methods?|class(?:es)?|handler\w*|logic|flow\w*|script\w*|hooks?|"
             r"cach\w*|logg\w*|prompt\w*|parser|regex\w*|pipeline\w*|planner|guard\w*|"
             r"tinh\s+nang|chuc\s+nang|ham|lop|luong|loi|lenh|thu\s+vien|cau\s+hinh")
_RF_VERB_RE = re.compile(r"\b(?:%s|%s)\b" % (_RF_EN_VERBS, _RF_VI_VERBS), re.I)
_RF_NOUN_RE = re.compile(r"\b(?:%s)\b" % _RF_NOUNS, re.I)
# Câu hỏi / giải thích / review: giữ hành vi gốc, KHÔNG nới rộng.
_RF_ASK_RE = re.compile(
    r"\b(?:giai\s+thich|hoat\s+dong|the\s+nao|la\s+gi|tai\s+sao|vi\s+sao|how|what|why|"
    r"does|explain|review|audit|danh\s+gia|kiem\s+tra|check)\b|\?", re.I)

_EXPLAIN_LEAD_RE = re.compile(
    r"^(?:giai\s+thich(?:\s+(?:tai\s+sao|vi\s+sao|giup(?:\s+minh|\s+toi)?))?|tai\s+sao|vi\s+sao|explain(?:\s+why)?|why|"
    r"(?:dieu\s+tra|tim\s+hieu|phan\s+tich|khao\s+sat)(?:\s+xem)?|"
    r"(?:kiem\s+tra\s+xem|xem(?:\s+thu)?)\b[^.!\n]*?\bco\s+the\b[^.!\n]*?\bkhong|"
    r"lieu\s+(?:co\s+the|source|code|file|ham|module)|is\s+it\s+possible\s+to)\b",
    re.IGNORECASE)

_PASTE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_PASTE_PRICE_RE = re.compile(
    r"[¥$€£₫]|(?:\b(?:vnd|usd|jpy|eur|gbp)\b)|/\s*(?:month|thang|year|nam)\b",
    re.IGNORECASE)
_PASTE_UI_RE = re.compile(
    r"^\s*(?:cac\s+khoan\s+phi|ngay\s+bat\s+dau|dang\s+ky\s+tren|huy\s+bat\s+ky|"
    r"muc\s+phi\s+phai\s+thanh\s+toan|ban\s+dang\s+xoa|hom\s+nay|bao\s+gom\s+thue|"
    r"subtotal|total|pricing|billing)\b",
    re.IGNORECASE)
_PASTE_LOG_RE = re.compile(
    r"^\s*(?:(?:Traceback\s+\(most\s+recent\s+call\s+last\)|File\s+\"[^\"]+\",\s+line\s+\d+|"
    r"(?:\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2})\b|"
    r"\[?(?:DEBUG|INFO|WARNING|WARN|ERROR|FATAL|CRITICAL)\]?[:\s]|\bat\s+\S+\s+\([^)\n]*:\d+:\d+\)).*)",
    re.IGNORECASE)
_PASTE_SESSION_SPLIT_RE = re.compile(
    r"\n\s*(?:-{3,}\s*\n+\s*)?\*\*Session\s+ID:\*\*\s+\S+",
    re.IGNORECASE)
_PASTE_READ_DUMP_RE = re.compile(
    r"^\s*(?:Read\s+file\s+\S+.*,\s*lines\s+\d+\s*-\s*\d+|\d+:\s.*|<truncated\s+\d+\s+bytes>)$",
    re.IGNORECASE)


def _strip_pasted_content(text: str) -> str:
    """Remove URLs, price/currency lines, stack traces/logs, UI tables, and pasted session dumps from text."""
    if not isinstance(text, str) or not text:
        return ""
    m_sess = _PASTE_SESSION_SPLIT_RE.search(text)
    if m_sess and text[:m_sess.start()].strip():
        text = text[:m_sess.start()]
    text = _PASTE_URL_RE.sub(" ", text)
    lines = text.splitlines()
    clean_lines = []
    for line in lines:
        s = line.strip()
        if not s:
            clean_lines.append("")
            continue
        if (_PASTE_PRICE_RE.search(s) or _PASTE_UI_RE.search(s)
                or _PASTE_LOG_RE.search(s) or _PASTE_READ_DUMP_RE.match(s)):
            continue
        clean_lines.append(line)
    return "\n".join(clean_lines)


def _ff_first_clause(text: str) -> str:
    if not text:
        return ""
    clean = _strip_pasted_content(text)
    f = " ".join(T._fold_diacritics(clean).lower().split())
    parts = re.split(r"[.!?;:\n]+|,|\s+(?:roi|sau\s+do|then|and\s+then)\s+", f)
    for p in parts:
        c = re.sub(
            r"^\W*(?:(?:hay|vui\s+long|lam\s+on|giup(?:\s+(?:toi|minh|em))?|ban|nho|please|pls|"
            r"can\s+you|could\s+you|would\s+you|toi\s+muon|minh\s+muon|toi\s+can|minh\s+can|"
            r"cho\s+toi|cho\s+minh|hey|ok|oke)\s+)+", "", p.strip()).strip()
        if c:
            return c
    return ""




def _is_change_request(folded, scannable, raw=""):
    raw_text = raw or scannable or folded or ""
    clean_raw = _strip_pasted_content(raw_text)
    if clean_raw != raw_text and clean_raw.strip():
        scannable = T._strip_code_spans(clean_raw)
        folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable)).strip()
        raw = clean_raw

    f_low = (folded or "").lower()
    if _c5("flow5_respect_no_edit"):
        if _FF5_NOEDIT_RE.search(f_low) or _ff5_noedit_text(raw):
            return False
        if _FF5_HOWTO_RE.search(f_low) and not _FF5_GRANT_RE.search(f_low):
            return False

    c0 = _ff_first_clause(raw_text)
    if c0 and _EXPLAIN_LEAD_RE.match(c0) and not (_CHANGE_ACT_RE.search(c0) or _FF5_GRANT_RE.search(f_low)):
        return False

    if _is_change_request_s1(folded, scannable, raw):
        return True
    try:
        if _RF_ASK_RE.search(folded or ""):
            return False
        return bool(_RF_VERB_RE.search(folded or "") and _RF_NOUN_RE.search(folded or ""))
    except Exception:
        return False


T._is_change_request = _is_change_request



# --------------------------------------------------------------------------
# 4. _is_loopback nhận ::ffff:127.0.0.1 (unified into canonical _is_loopback)
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 3. Kiểm tra proxy bằng trạng thái đang chạy
# --------------------------------------------------------------------------
_RF_LOCK = _rf_threading.Lock()
_RF_LOGS = _rf_collections.deque(maxlen=400)
_RF_ECHOES = _rf_collections.deque(maxlen=20)



def log(msg):
    try:
        _RF_LOGS.append("[%s] %s" % (_rf_time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass
    return log_s1(msg)




def check_routing(raw, model_id, extra_fields=None, ticket=None):
    try:
        e = upstream_echo(raw)
        want = None
        if ticket:
            try:
                t = json.loads(ticket)
                want = (t[14], t[15])
            except Exception:
                want = None
        if want is None:
            want = (model_id, (extra_fields or {}).get(80))
        with _RF_LOCK:
            _RF_ECHOES.append({
                "t": _rf_time.time(), "want": want, "ticket": bool(ticket),
                "served": (e[0], e[1], e[2]) if e else None,
                "ok": bool(e) and e[1] == want[0]
                      and (want[1] is None or e[2] == want[1])})
    except Exception:
        pass
    return check_routing_s1(raw, model_id, extra_fields, ticket)


_RF_SUBJECT_RE = re.compile(r"\b(?:routing|route|fallback|model|proxy|server|log|logs)\b")
_RF_LIVE_RE = re.compile(
    r"\b(?:hien\s+tai|dang\s+chay|dang\s+dung|dang\s+route|dang\s+routing|currently|"
    r"right\s+now|running|live|xem\s+log|check\s+log|log\s+(?:hien|server|proxy))\b")
_RF_CHECK_RE = re.compile(
    r"\b(?:check|kiem\s+tra|xem|verify|confirm|status|trang\s+thai)\b|\bchua\b|\bkhong\b|\?")
_RF_NOT_RE = re.compile(
    r"\b(?:source|code|giai\s+thich|review|sua|fix|implement|them|bo\s+sung|viet|"
    r"refactor|patch|hoat\s+dong\s+the\s+nao)\b")


def _rf_runtime_question_s1(messages):
    last = None
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            last = _stringify_content(m.get("content", ""))
            break
        if m.get("role") == "tool":
            return False
    if not last or len(last) > 700:
        return False
    f = re.sub(r"\s+", " ", _fold_diacritics(last)).strip().lower()
    return bool(_RF_SUBJECT_RE.search(f) and _RF_LIVE_RE.search(f)
                and _RF_CHECK_RE.search(f) and not _RF_NOT_RE.search(f))


def _rf_ticket_route(ticket):
    if not ticket:
        return None
    try:
        t = json.loads(ticket)
        return (t[14], t[15])
    except Exception:
        return "unparsable"


def _rf_runtime_report(model_id, think_mode, extra, ticket):
    name = "(không rõ)"
    for n, c in MODELS.items():
        if c.get("mode") == model_id and c.get("variant") == (extra or {}).get(80):
            name = n
            break
    var = (extra or {}).get(80)
    route = _rf_ticket_route(ticket)
    exp = route if isinstance(route, tuple) else (model_id, var)
    out = ["Kiểm tra trạng thái proxy đang chạy (không đọc source):",
           "- Model của request này: %s (family=%s, variant=%s, think=%s)"
           % (name, model_id, var, think_mode),
           "- Ticket gửi lên upstream: " + (
               "KHÔNG CÓ -> upstream bỏ qua lựa chọn model và dùng model mặc định của tài khoản"
               if not ticket else "có, nhúng (family,variant)=%s" % (route,)),
           "- Kỳ vọng upstream phục vụ: %s" % (exp,)]
    with _RF_LOCK:
        hist = list(_RF_ECHOES)[-6:]
    if not hist and CONFIG.get("runtime_check_probe", True):
        try:
            generate("Reply with the single word: OK", model_id, think_mode, None, extra, ticket)
        except Exception as e:  # noqa: BLE001
            out.append("- Probe thất bại: %s" % str(e)[:200])
        with _RF_LOCK:
            hist = list(_RF_ECHOES)[-6:]
    if hist:
        out.append("- Upstream echo ở các request gần nhất (cũ -> mới):")
        for h in hist:
            out.append("    * %ds trước: muốn %s -> upstream phục vụ %s : %s" % (
                int(_rf_time.time() - h["t"]), h["want"], h["served"],
                "ĐÚNG" if h["ok"] else "LỆCH"))
    else:
        out.append("- Chưa nhận được echo nào từ upstream.")
    with _RF_LOCK:
        lines = [l for l in _RF_LOGS if re.search(
            r"Upstream model|Routing|\[model\]|reasoning_effort|placeholder|canned|"
            r"Tool retry|mismatch", l)][-10:]
    if lines:
        out.append("- Log gần nhất liên quan:")
        out += ["    " + l[:220] for l in lines]
    mine = [h for h in hist if h["served"]]
    if mine and all(h["ok"] for h in mine[-3:]):
        out.append("=> KẾT LUẬN: routing ĐÚNG ở các request gần nhất, không thấy fallback.")
    elif mine:
        s = mine[-1]["served"]
        out.append("=> KẾT LUẬN: có request bị LỆCH (upstream phục vụ family=%s variant=%s)%s"
                   % (s[1], s[2], "; (6,1) nghĩa là fallback về Flash-Lite low."
                      if (s[1], s[2]) == (6, 1) else "."))
    else:
        out.append("=> KẾT LUẬN: CHƯA XÁC MINH được từ upstream; đọc source không chứng minh được.")
    return "\n".join(out)




def _rf_gwtr(self, prompt, model_id, think_mode, file_refs, extra, tools_active,
             allowed_names, tool_schemas, required_tool, messages=None,
             ticket=None, tool_required=None, client_gone=None):
    try:
        if (CONFIG.get("runtime_check_local", True) and not required_tool
                and _rf_runtime_question(messages)):
            log("[flowfix13] runtime check answered from live state, no source read")
            return _rf_runtime_report(model_id, think_mode, extra, ticket), []
    except Exception as e:  # noqa: BLE001
        log("[flowfix13] runtime check failed, falling back: %s" % e)
    return _GeminiHandler_gwtr_orig(self, prompt, model_id, think_mode, file_refs, extra,
                         tools_active, allowed_names, tool_schemas, required_tool,
                         messages, ticket, tool_required, client_gone)


GeminiHandler._generate_with_tool_retry = _rf_gwtr
# ==== flowfix13:end ====


# ==== flowfix14:begin (generated by flow_fix14_patch.py -- re-run to refresh, --revert to remove) ====
# flowfix14: bug fixes + better implement / review / explain flows for weak models.
#   1. _ff5_git_cmd keeps the number after "-n" ("git log -n 20" was cut to "-n").
#   2. _ff5_scope: a test file that was READ this turn may be written (CHANGE/DEBUG),
#      so the "add a test" step is no longer blocked when the user named only src files.
#   3. EXPLAIN / DEBUG answers list the real-case paths (read_set is lower-cased).
#   4. Edit gate: whitespace-insensitive unique repair of oldString (+ re-indent of
#      newString); optional (flow14_edit_ambiguity) error when oldString matches several places.
#   5. IMPLEMENT: when no test command can verify the edit, the proxy runs a syntax check
#      (ast.parse) of the edited .py files; a failure sends the model back to "act".
#   6. EXPLAIN: follow local imports of the files already read (cap flow14_explain_max_reads).
#   7. review_demanded() is also true for code questions, so fabricated quotes in an
#      explanation are retried through unverified_quotes().
#   8. "explain how to run the tests" is an explanation, not a RUN_TESTS request.


_ff14_c = _flow_c


# --------------------------------------------------------------------------
# 1. git log -n 20
# --------------------------------------------------------------------------
def _ff5_git_cmd(m):
    sub = m.group(1).lower()
    toks = (m.group(2) or "").split()
    flags, i = [], 0
    while i < len(toks):
        tok = toks[i]
        if not tok.startswith("-"):
            break
        flags.append(tok)
        if (tok in ("-n", "--max-count") and i + 1 < len(toks)
                and re.fullmatch(r"\d{1,4}", toks[i + 1])):
            flags.append(toks[i + 1])
            i += 1
        i += 1
    if not flags:
        flags = {"status": ["--short", "--branch"],
                 "log": ["--oneline", "-n", "20"]}.get(sub, [])
    return " ".join(["git", "--no-pager", sub] + flags)


# 2. write scope: test files read this turn (unified into _ff5_scope above)


# --------------------------------------------------------------------------
# 3. real-case paths in EXPLAIN / DEBUG answers
# --------------------------------------------------------------------------
def _ff14_restore_case(step, ev):
    try:
        if step is None or getattr(step, "name", None) != "answer" or not step.instruction:
            return step
        instr = step.instruction
        seen = set()
        for e in ev:
            if e["kind"] != "read" or not _read_ok(e):
                continue
            p = _call_path(e["args"])
            if p and p != p.lower() and p not in seen:
                seen.add(p)
                instr = re.sub(re.escape(p.lower()), lambda _m, _p=p: _p, instr,
                               flags=re.IGNORECASE)
        step.instruction = instr
    except Exception as ex:
        log("[fix14] restore-case error: %s" % ex)
    return step




def _plan_debug(ctx, ev, tm):
    return _ff14_restore_case(_plan_debug_s1(ctx, ev, tm), ev)


# --------------------------------------------------------------------------
# 6. EXPLAIN follows the imports of what it already read
# --------------------------------------------------------------------------


def _ff14_explain_hop_s1(ctx, ev, tm, step):
    if (not _ff14_c("flow14_explain_hops") or step is None
            or getattr(step, "name", None) != "answer"
            or getattr(step, "local_text", None) is not None
            or not tm.first("read")):
        return step
    cap = int(_ff14_c("flow14_explain_max_reads"))
    read_set = _read_set(ev)
    code = [p for p in _code_reads(read_set) if not _is_test_path(p)]
    if not code or len(code) >= cap:
        return step
    targets = [t for t in _hop_targets(ev, _listing_files(ev), read_set, "")
               if not _is_test_path(t) and not _is_doc(t)]
    targets = targets[:max(0, min(2, cap - len(code)))]
    if not targets:
        return step
    log("[fix14] explain: following imports -> %s" % ", ".join(targets))
    return _read_step(
        "EXPLAIN", tm, targets, 3, 4,
        instruction="The files you read import these local modules. Read them too so "
                    "the explanation covers the real flow: " + ", ".join(targets) + ".")


def _plan_explain_s2(ctx, ev, tm):
    step = _plan_explain_s1(ctx, ev, tm)
    try:
        step = _ff14_explain_hop(ctx, ev, tm, step)
    except Exception as ex:
        log("[fix14] explain hop error: %s" % ex)
    return _ff14_restore_case(step, ev)


# --------------------------------------------------------------------------
# 4. edit gate: unique whitespace-insensitive repair + ambiguity check
# --------------------------------------------------------------------------
_FF14_REPLACE_ALL_KEYS = ("replaceAll", "replace_all", "replace_all_occurrences")


def _ff14_nline(s):
    return " ".join(s.split())


def _ff14_find_unique(old, hay):
    """Line-based, whitespace-insensitive match -> (actual_text | None, n_matches)."""
    ol = [_ff14_nline(x) for x in old.split("\n")]
    while ol and not ol[0]:
        ol.pop(0)
    while ol and not ol[-1]:
        ol.pop()
    if not ol:
        return None, 0
    raw = hay.split("\n")
    nl = [_ff14_nline(x) for x in raw]
    n, hits = len(ol), []
    first = ol[0]
    for i in range(len(nl) - n + 1):
        if nl[i] == first and nl[i:i + n] == ol:
            hits.append(i)
    if len(hits) == 1:
        i = hits[0]
        return "\n".join(raw[i:i + n]), 1
    return None, len(hits)


def _ff14_ws(ln):
    return ln[:len(ln) - len(ln.lstrip())]


def _ff14_reindent(new, old, actual):
    """Re-indent ``new`` from the model's indentation of ``old`` to the file's ``actual``."""
    ol = [x for x in old.split("\n")]
    while ol and not ol[0].strip():
        ol.pop(0)
    while ol and not ol[-1].strip():
        ol.pop()
    al = actual.split("\n")
    mapping = {}
    if len(ol) == len(al):
        for o, a_ in zip(ol, al):
            if o.strip() and a_.strip():
                mapping.setdefault(_ff14_ws(o), _ff14_ws(a_))
    if not mapping or all(k == v for k, v in mapping.items()):
        return new
    keys = sorted(mapping, key=len, reverse=True)
    out = []
    for ln in new.split("\n"):
        if not ln.strip():
            out.append(ln)
            continue
        ws = _ff14_ws(ln)
        k = next((k for k in keys if ws.startswith(k)), None)
        out.append(ln if k is None else mapping[k] + ln[len(k):])
    return "\n".join(out)


def _ff14_gate_prepass(calls, messages):
    texts = _ff9_read_texts(messages)
    out, problems = [], []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            out.append(tc)
            continue
        args = T._tool_arguments_dict(fn)
        path = _call_path(args)
        okey = next((k for k in _OLD_KEYS
                     if isinstance(args.get(k), str) and args[k]), None)
        src = next((t for p, t in texts.items()
                    if path and T._path_seen(path, [p])), None)
        if not (okey and src is not None):
            out.append(tc)
            continue
        hay = src.replace("\r\n", "\n")
        old = args[okey].replace("\r\n", "\n")
        exact = None
        for c in (old, _strip_linenos(old)):
            if c in hay:
                exact = c
                break
            if c.strip("\n") and c.strip("\n") in hay:
                exact = c.strip("\n")
                break
        if exact is not None:
            cnt = hay.count(exact)
            if cnt > 1 and _ff14_c("flow14_edit_ambiguity") and not any(args.get(k) in (True, 1, "true", "True")
                                   for k in _FF14_REPLACE_ALL_KEYS):
                problems.append(
                    "%s: oldString matches %d places in the file; include more "
                    "surrounding lines (the line before/after) so it matches exactly "
                    "ONE place, or set replaceAll" % (path, cnt))
            out.append(tc)
            continue
        stripped = _strip_linenos(old)
        actual, n = _ff14_find_unique(stripped, hay)
        if actual is None:
            if n > 1 and _ff14_c("flow14_edit_ambiguity"):
                problems.append(
                    "%s: oldString (ignoring whitespace) matches %d places; include more "
                    "surrounding lines so it matches exactly ONE place" % (path, n))
            out.append(tc)
            continue
        args[okey] = actual
        for nk in _NEW_KEYS:
            if isinstance(args.get(nk), str):
                new = args[nk].replace("\r\n", "\n")
                if stripped != old:
                    new = _strip_linenos(new)
                if old != old.strip("\n"):
                    new = new.strip("\n")
                args[nk] = _ff14_reindent(new, stripped, actual)
        log("[fix14] edit gate: repaired whitespace of oldString in %s" % path)
        out.append({**tc, "function": {
            **fn, "arguments": json.dumps(args, ensure_ascii=False)}})
    return out, problems


def _base_gate_act_calls_s2(calls, messages):
    extra = []
    try:
        calls, extra = _ff14_gate_prepass(calls, messages)
    except Exception as ex:
        log("[fix14] edit gate pre-pass error: %s" % ex)
    out, problems = _base_gate_act_calls_s1(calls, messages)
    return out, list(problems) + extra


# --------------------------------------------------------------------------
# 5. IMPLEMENT: syntax-check the edited python files before the real tests
# --------------------------------------------------------------------------
_FF14_MARK = "import ast,sys;[ast.parse"
_FF14_SYNTAX_RE = re.compile(r"\b(?:SyntaxError|IndentationError|TabError)\b")
_FF14_NOPY_RE = re.compile(
    r"(?i)not\s+found|not\s+recognized|no\s+such\s+file|cannot\s+find|is\s+not\s+recognized")


def _ff14_compile_cmd(ev, paths):
    win = any(re.match(r"^[A-Za-z]:[\\/]", str((e.get("args") or {}).get(k) or ""))
              for e in ev for k in ("filePath", "file_path", "path")
              if isinstance((e.get("args") or {}).get(k), str))
    py = "python" if win else "python3"
    quoted = " ".join('"%s"' % p.replace('"', "") for p in paths[:8])
    return (py + " -c \"import ast,sys;[ast.parse(open(p,encoding='utf-8').read(),p) "
            "for p in sys.argv[1:]]\" " + quoted)


def _ff14_compile_step(pat, ctx, ev, tm, total, nxt):
    """Syntax check for edited .py files. Only used when the normal flow would
    answer without any verification (no test command ran after the edit); a
    syntax error found here sends the model back to "act"."""
    if not _ff14_c("flow14_compile") or not tm.first("exec"):
        return None
    if T._NO_TEST_RE.search(getattr(ctx, "folded", "") or ""):
        return None
    acts = [(i, e) for i, e in enumerate(ev) if e["kind"] == "act"]
    if not acts or not _act_ok(acts[-1][1]) or len(acts) >= int(_cfg("flow_max_acts")):
        return None
    last = acts[-1][0]
    paths = sorted({_call_path(e["args"]) for _i, e in acts
                    if _act_ok(e) and _call_path(e["args"])
                    and _call_path(e["args"]).lower().endswith(".py")
                    and _base(_call_path(e["args"])) != _MERGE_SCRIPT})
    if not paths:
        return None
    runs = [e for i, e in enumerate(ev) if i > last and e["kind"] == "exec"
            and _FF14_MARK in _call_command(e["args"])]
    if runs:
        res = runs[-1].get("result") or ""
        if _FF14_SYNTAX_RE.search(res) and tm.first("act"):
            return Step(pat, "act", 3, total, {"act"},
                        "Your edit left a Python SYNTAX ERROR. Fix it with ONE more edit "
                        "tool_call (copy old text exactly from the file as it is NOW; "
                        "re-read it first if unsure). Error:\n" + _excerpt(res, 1200),
                        None, False, tm.names({"act"}))
        return None
    if (getattr(nxt, "name", None) == "answer" and not _tests_ran_after_act(ev)):
        call = tm.make_call("exec", command=_ff14_compile_cmd(ev, paths),
                            description="Check Python syntax of the edited files")
        if call:
            return Step(pat, "tests", 4, total, {"exec"},
                        "Check the syntax of the edited Python file(s).",
                        [call], True, tm.names({"exec"}))
    return None


def _change_tail_s3(pat, ctx, ev, tm, names_all, total, extra=""):
    st = _change_tail_s2(pat, ctx, ev, tm, names_all, total, extra)
    try:
        cs = _ff14_compile_step(pat, ctx, ev, tm, total, st)
        if cs is not None:
            return cs
    except Exception as ex:
        log("[fix14] compile step error: %s" % ex)
    return st


# --------------------------------------------------------------------------
# 7. quotes in explanations are verified too
# --------------------------------------------------------------------------


_FF14_REFACTOR_Q_RE = re.compile(
    r"\b(?:refactor\w*|restructur\w*|clean\s*up|tinh\s+gon|gon\s+hon|"
    r"tach\s+(?:module|file|nho|ham)|to\s+chuc\s+lai|cau\s+truc\s+lai|"
    r"viet\s+lai|toi\s+uu)\b",
    re.IGNORECASE,
)


def review_demanded(messages) -> bool:
    """True when the newest user turn asks for a source/code review or code question."""
    if review_demanded_s1(messages):
        return True
    try:
        last_user = None
        for msg in messages or []:
            if isinstance(msg, dict) and msg.get("role") == "user":
                last_user = _stringify_content(msg.get("content", ""))
        if not last_user or not _is_code_question(last_user):
            return False
        folded = T._fold_diacritics(T._strip_code_spans(last_user))
        if _FF14_REFACTOR_Q_RE.search(folded) and not _FF10_DEFECT_RE.search(folded):
            return False
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# 8. "explain how to run the tests" is a question, not a command
# --------------------------------------------------------------------------
_FF14_HOWTO_RE = re.compile(
    r"\b(?:how\s+(?:to|do|can|should|would)|lam\s+(?:sao|the\s+nao)|cach\s+(?:nao|de)"
    r"|huong\s+dan)\b")
_FF14_CONJ_RE = re.compile(r"\b(?:and|then|also|va|roi|sau\s+do)\b")


def _ff12_explain_only(ctx):
    if _ff12_explain_only_s1(ctx):
        return True
    folded = " ".join(str(getattr(ctx, "folded", "") or "").lower().split())
    if not folded or not _FF14_HOWTO_RE.search(folded):
        return False
    for m in _FF5_RUNV_RE.finditer(folded):
        head = re.split(r"[.;!?\n,]", folded[:m.start()])[-1]
        hows = list(_FF14_HOWTO_RE.finditer(head))
        if hows:
            if _FF14_CONJ_RE.search(head[hows[-1].end():]):
                return False
            continue
        if _FF12_COND_RE.search(head):
            continue
        return False
    return True


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
def _setup_ff14():
    for k, v in _FF14_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    try:
        for name in ("gemini_web2api.tools", "gemini_web2api.server"):
            mod = sys.modules.get(name)
            if mod is not None and hasattr(mod, "review_demanded"):
                mod.review_demanded = review_demanded
    except Exception as ex:
        log("[fix14] install sync error: %s" % ex)
# ==== flowfix14:end ====


# ==== flowfix15:begin (generated by flow_fix15_patch.py -- re-run to refresh, --revert to remove) ====
# flowfix15: bug fixes + better routing for implement / review / explain flows.
#   1. _keywords: Vietnamese words (folded to ASCII) no longer leak into grep patterns.
#   2. Truncated reads: "(File has more lines. Use 'offset' parameter to read beyond line N)"
#      is now continued with offset=N like the other truncation markers.
#   3. Routing (wrapper on _analyze, only upgrades pattern=None or mis-routed *_REVIEW):
#        - "doc README roi tom tat", "xem package.json" -> EXPLAIN (read + summarize,
#          not a bug report); "tim ham foo_bar trong source" -> EXPLAIN (locate), not
#          GENERIC_REVIEW; "tim loi bao mat trong project" -> GENERIC_REVIEW;
#          "du an nay lam gi", "huong dan cach chay" -> EXPLAIN;
#          "xoa ham.. / doi ten.. / cap nhat README / lam theo PLAN.md" -> CHANGE.
#   4. _rf_runtime_question needs a proxy-specific word (proxy / routing / fallback /
#      ticket / upstream / gemini / flash / lite): "kiem tra log server" is a project task.
#   5. _is_loopback: one consistent definition (ipaddress based, handles ::ffff:127.0.0.1,
#      rejects hostnames such as 127.evil.example).
#   6. Request body reader: bounded chunk-size lines / trailers, short reads are an error;
#      "Authorization: bearer <key>" is case-insensitive.
#   7. _resolve_public_ip also rejects NAT64 (64:ff9b::/96) and 6to4 addresses that embed a
#      non-public IPv4.
# Every part has a switch in CONFIG (flow15_*), default on.


_ff15_c = _flow_c


# --------------------------------------------------------------------------
# 1. _keywords: no Vietnamese words in grep patterns
# --------------------------------------------------------------------------
_FF15_VI_STOP = frozenset("""
cho toi minh ban hay giup lam sao the nao tai sao vi sao khong chua duoc nhung neu thi
cua trong ngoai voi hoac nen khi con cung rat qua hon nhat moi tat cac mot hai nhieu
phai can muon biet hieu giai thich tim kiem xem doc viet tao them sua xoa doi ten loi
ham bien tep thu muc du an ma nguon chuc nang tinh xu hoat dong cach nhu duong dan
nguoi dung nhap xac thuc mat khau tai khoan chay kiem tra giup do nay kia day dau gi
nhe nha oi vao ra len xuong sang tu den tren duoi sau truoc roi ngay lien tuc luon
van bang cua vua moi dang dung theo nhan bao cao thong tin dieu phan thuc hien khac
rieng chung ban dau cuoi cung giua noi dung chi tiet tung toan bo ca het gom dua ngoai
gia tri ket qua yeu cau cau hinh loai kieu danh sach so luong huong xu ly lap trinh
""".split())



def _keywords(text, limit=6):
    """Identifier/backtick tokens first, plain ASCII words (>=4 chars) after.

    Unlike the original this tokenizes the ORIGINAL text: any word with an accented
    (Vietnamese) letter is a natural-language word, never a code identifier."""
    if not _ff15_c("flow15_keywords"):
        return _keywords_s1(text, limit)
    strong, weak = [], []
    text = text or ""
    for m in re.finditer(r"`([^`\n]+)`", text):
        if not (2 <= len(m.group(1)) <= 60):
            continue
        k = re.sub(r"[()\s]", "", m.group(1)).lower()
        if k and k not in strong:
            strong.append(k)
    src = re.sub(r"```.*?```", " ", text, flags=re.S)
    for w in re.findall(r"[^\W\d]\w*", src):
        if not w.isascii() or len(w) < 3:
            continue
        lw = w.lower()
        if lw in _STOP2 or lw in _FF15_VI_STOP or lw in strong or lw in weak:
            continue
        if "_" in w or (any(c.isupper() for c in w[1:]) and any(c.islower() for c in w)):
            strong.append(lw)
        elif len(lw) >= 4:
            weak.append(lw)
    return (strong + weak)[:limit]


# --------------------------------------------------------------------------
# 2. legacy opencode truncation marker (unified into _Ff9Trunc)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 3. routing
# --------------------------------------------------------------------------
_FF15_LEAD_RE = re.compile(
    r"^\W*(?:(?:hay|vui\s+long|lam\s+on|giup(?:\s+(?:toi|minh|em))?|ban|nho|please|pls|"
    r"can\s+you|could\s+you|would\s+you|toi\s+muon|minh\s+muon|toi\s+can|minh\s+can|"
    r"cho\s+toi|cho\s+minh|hey|ok|oke)\s+)+")
_FF15_SPLIT_RE = re.compile(r"[.!?;:\n]+|,|\s+(?:roi|sau\s+do|then|and\s+then)\s+")
_FF15_CHG_RE = re.compile(
    r"(?:xoa|loai\s+bo|go\s+bo|doi\s+ten|thay\s+the|thay\s+doi|chuyen\s+(?:sang|thanh|qua)|"
    r"tach|gop|viet\s+lai|toi\s+uu|cai\s+thien|hoan\s+thien|hoan\s+thanh|lam\s+theo|"
    r"thuc\s+hien|ap\s+dung|cap\s+nhat|chinh\s+sua|sua\s+doi|bo\s+sung|viet\s+them|"
    r"lam\s+them|them|sua|delete|remove|drop|rename|replace|rewrite|optimi[sz]e|improve|"
    r"migrate|convert|extract|split|merge|move|follow|apply|update|complete|finish|"
    r"clean\s*up|refactor|implement|add|fix)\b")
_FF15_DOCWRITE_RE = re.compile(
    r"(?:viet|soan|tao|write|create|generate)\s+(?:lai\s+)?(?:file\s+)?"
    r"(?:readme|changelog|tai\s+lieu|docs?\b)")
_FF15_READ_RE = re.compile(
    r"(?:doc|xem|mo|open|read|cat|show|view|print|hien\s+thi|tom\s+tat|summari[sz]e)\b")
_FF15_LOCATE_RE = re.compile(r"(?:tim(?:\s+kiem)?|find|search|locate|grep|look\s+for)\b")
_FF15_WHERE_RE = re.compile(
    r"\b(?:o\s+dau|cho\s+nao|where\s+(?:is|are|does|do)|used\s+(?:in|by)|duoc\s+(?:dung|goi)\s+o)\b")
_FF15_DOCNAME_RE = re.compile(
    r"\b(?:readme|changelog|license|licence|contributing|makefile|dockerfile|procfile)(?:\.\w+)?\b")
_FF15_NOUN_RE = re.compile(
    r"\b(?:file|tep|ham|function|method|class|module|bien|variable|test|unit\s+test|script|"
    r"code|source|du\s+an|project|repo|repository|codebase|readme|tai\s+lieu|docs?|config|"
    r"cau\s+hinh|chuc\s+nang|tinh\s+nang|feature|endpoint|api|comment|docstring|import|"
    r"plan|todo)\b")
_FF15_PROJ_RE = re.compile(
    r"\b(?:project|du\s+an|repo|repository|codebase|source|ma\s+nguon|he\s+thong)\b")
_FF15_AUDIT_VERB_RE = re.compile(
    r"\b(?:tim|kiem\s+tra|soat|scan|check|find|audit|look\s+for|detect|phat\s+hien)\b")
_FF15_AUDIT_WORD_RE = re.compile(
    r"\b(?:loi|bug|bugs|bao\s+mat|lo\s+hong|vulnerab\w*|security|van\s+de|sai\s+sot|"
    r"issues?|problems?|errors?)\b")
_FF15_PURPOSE_RE = re.compile(
    r"(?:\b(?:du\s+an|project|repo|repository|codebase)\b.{0,20}\b(?:lam\s+gi|de\s+lam\s+gi|"
    r"do\s+what|used\s+for|about)\b)|(?:\bwhat\s+(?:does|is)\s+this\s+(?:project|repo|codebase)\b)")
_FF15_HOWRUN_RE = re.compile(
    r"\b(?:huong\s+dan|cach|how\s+(?:do\s+i|to|can\s+i))\s+"
    r"(?:chay|cai\s+dat|su\s+dung|build|deploy|run|install|use|start|set\s*up|setup)\b")
_FF15_DEFECT_FALLBACK = re.compile(
    r"\b(?:bug|bugs|loi|sai|hong|fail\w*|crash\w*|vulnerab\w*|bao\s+mat|security|"
    r"lo\s+hong|sai\s+sot|issue|issues|problem|problems|wrong|broken)\b")
_FF15_STRONG_FALLBACK = re.compile(r"\b(?:review|audit|danh\s+gia|ra\s+soat|phan\s+tich)\b")
_FF15_IDENT_RES = (
    re.compile(r"`[^`\n]{2,}`"),
    re.compile(r"\b[A-Za-z]+_[A-Za-z0-9_]+\b"),
    re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b"),
    re.compile(r"\b\w+\(\)"),
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}\s*[\[\.]"),
)


def _ff15_has_ident(raw):
    return any(rx.search(raw) for rx in _FF15_IDENT_RES)


def _ff15_clauses(folded):
    out = []
    for part in _FF15_SPLIT_RE.split(folded):
        part = _FF15_LEAD_RE.sub("", (part or "").strip()).strip()
        if part:
            out.append(part)
    return out


def _ff15_label(ctx, pat):
    """New pattern label for this request, or None to keep ``pat``."""
    raw = str(getattr(ctx, "scannable", "") or getattr(ctx, "last", "") or "")[:1500]
    clean_raw = _strip_pasted_content(raw)
    f = " ".join(T._fold_diacritics(clean_raw).lower().split())
    if len(f) < 4:
        return None
    clauses = _ff15_clauses(f)
    if not clauses:
        return None

    # Priority rule: first clause starts with explain/why/giai thich/tai sao/vi sao -> EXPLAIN
    c0 = clauses[0]
    if _EXPLAIN_LEAD_RE.match(c0) and not (_CHANGE_ACT_RE.search(c0) or _FF5_GRANT_RE.search(f)):
        return "EXPLAIN"

    review_pat = pat in ("GENERIC_REVIEW", "FILE_REVIEW")
    if pat and not review_pat:
        return None
    defect_rx = globals().get("_FF10_DEFECT_RE") or _FF15_DEFECT_FALLBACK
    strong_rx = globals().get("_SOURCE_STRONG_VERB_RE") or _FF15_STRONG_FALLBACK
    howto_rx = globals().get("_FF5_HOWTO_RE")
    howto = bool(howto_rx and howto_rx.search(f))
    try:
        has_file = bool(T._files_in(raw))
    except Exception:
        has_file = False
    has_doc = bool(_FF15_DOCNAME_RE.search(f))
    has_ident = _ff15_has_ident(raw)
    defect = bool(defect_rx.search(f))
    strong = bool(strong_rx.search(f))

    def starts(rx):
        return any(rx.match(c) for c in clauses)

    read = starts(_FF15_READ_RE) and (has_file or has_doc)
    locate = ((starts(_FF15_LOCATE_RE) or bool(_FF15_WHERE_RE.search(f)))
              and (has_ident or has_file) and not defect)
    if review_pat:
        # plain "read / find where X is" is an explanation, not a bug report
        if not strong and not defect and (read or locate):
            if "?" not in raw and (starts(_FF15_CHG_RE) or starts(_FF15_DOCWRITE_RE)
                                   or T._is_change_request(f, clean_raw, raw)):
                return "CHANGE"
            return "EXPLAIN"
        return None
    if howto:
        return "EXPLAIN" if (_FF15_PURPOSE_RE.search(f) or _FF15_HOWRUN_RE.search(f)) else None
    target = has_ident or has_file or has_doc or bool(_FF15_NOUN_RE.search(f))
    if "?" not in raw and target and (starts(_FF15_CHG_RE) or starts(_FF15_DOCWRITE_RE)):
        return "CHANGE"
    if (_FF15_AUDIT_VERB_RE.search(f) and _FF15_AUDIT_WORD_RE.search(f)
            and _FF15_PROJ_RE.search(f) and not has_file):
        return "GENERIC_REVIEW"
    if _FF15_PURPOSE_RE.search(f):
        return "EXPLAIN"
    if _FF15_HOWRUN_RE.search(f) and (_FF15_PROJ_RE.search(f) or has_doc or "nay" in f.split()
                                      or "this" in f.split()):
        return "EXPLAIN"
    if read or locate:
        return "EXPLAIN"
    return None




def _analyze_s6(messages):
    ctx = _analyze_s5(messages)
    if ctx is None or not _ff15_c("flow15_route"):
        return ctx
    try:
        if getattr(ctx, "ask_user", False) or getattr(ctx, "free_run", False) or getattr(ctx, "model_choice", None):
            return ctx
        if _clarified(messages, getattr(ctx, "idx", -1)):
            return ctx
        pat = getattr(ctx, "pattern", None)
        label = _ff15_label(ctx, pat)
        if label and label != pat:
            log("[fix15] route %s -> %s" % (pat or "NONE", label))
            return _ff10_apply_pattern(ctx, messages, label)
    except Exception as ex:
        log("[fix15] route error: %s" % ex)
    return ctx


# --------------------------------------------------------------------------
# 4. proxy-status questions need a proxy-specific word
# --------------------------------------------------------------------------
_FF15_PROXY_RE = re.compile(
    r"\b(?:proxy|routing|routed|route|routes|fallback|fall(?:ing)?\s+back|ticket|upstream|"
    r"gemini|flash|lite)\b")


def _rf_runtime_question(messages):
    if not _rf_runtime_question_s1(messages):
        return False
    if not _ff15_c("flow15_runtime_strict"):
        return True
    try:
        last = None
        for m in reversed(messages or []):
            if isinstance(m, dict) and m.get("role") == "user":
                last = _stringify_content(m.get("content", ""))
                break
        f = " ".join(T._fold_diacritics(last or "").lower().split())
        return bool(_FF15_PROXY_RE.search(f))
    except Exception:
        return True


# --------------------------------------------------------------------------
# 5. _is_loopback: one definition (unified into canonical _is_loopback)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 6. request body reader + Bearer parsing
# --------------------------------------------------------------------------
_FF15_LINE_MAX = 65536


def _ff15_read_request_body(self):
    if not _ff15_c("flow15_server_hardening"):
        return _GeminiHandler_read_request_body_orig(self)
    limit = CONFIG.get("max_body_bytes", 32 * 1024 * 1024)
    if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
        chunks, total = [], 0
        while True:
            size_line = self.rfile.readline(_FF15_LINE_MAX + 1)
            if not size_line:
                break
            if len(size_line) > _FF15_LINE_MAX:
                raise ValueError("invalid chunked request body")
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise ValueError("invalid chunked request body")
            if size < 0:
                raise ValueError("invalid chunked request body")
            if size == 0:
                for _ in range(100):
                    trailer = self.rfile.readline(_FF15_LINE_MAX + 1)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            total += size
            if total > limit:
                raise ValueError("request body too large")
            data = self.rfile.read(size)
            if len(data) != size:
                raise ValueError("truncated chunked request body")
            chunks.append(data)
            nxt = self.rfile.read(1)
            if nxt == b"\r":
                self.rfile.read(1)
        return b"".join(chunks)
    raw_len = self.headers.get("Content-Length")
    if raw_len is None:
        return b""
    try:
        length = int(raw_len)
    except (ValueError, TypeError):
        raise ValueError("invalid Content-Length header")
    if length < 0:
        raise ValueError("invalid Content-Length header")
    if length > limit:
        raise ValueError("request body too large")
    if not length:
        return b""
    data = self.rfile.read(length)
    if len(data) != length:
        raise ValueError("incomplete request body")
    return data


def _ff15_authorized(self):
    if not _ff15_c("flow15_server_hardening"):
        return _GeminiHandler_authorized_orig(self)
    keys = CONFIG.get("api_keys") or []
    if not keys:
        return True
    provided = []
    auth = (self.headers.get("Authorization", "") or "").strip()
    if auth[:7].lower() == "bearer ":
        provided.append(auth[7:].strip())
    for header in ("x-api-key", "x-goog-api-key"):
        value = self.headers.get(header)
        if value:
            provided.append(value.strip())
    for pair in urllib.parse.urlparse(self.path).query.split("&"):
        if pair.startswith("key="):
            provided.append(urllib.parse.unquote(pair[4:]))
    for value in provided:
        for key in keys:
            if isinstance(key, str) and hmac.compare_digest(value.encode(), key.encode()):
                return True
    return False


GeminiHandler._read_request_body = _ff15_read_request_body
GeminiHandler._authorized = _ff15_authorized


# --------------------------------------------------------------------------
# 7. SSRF: NAT64 / 6to4 addresses embedding a non-public IPv4
# --------------------------------------------------------------------------
_FF15_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _ff15_public(addr):
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr in _FF15_NAT64:
            addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
        elif addr.sixtofour is not None:
            addr = addr.sixtofour
    return addr.is_global


def _resolve_public_ip(hostname):
    """First public IP of ``hostname``, or None if ANY address is non-public."""
    if not _ff15_c("flow15_server_hardening"):
        return _resolve_public_ip_s1(hostname)
    if not hostname:
        return None
    host = hostname.strip().rstrip(".").strip("[]").split("%")[0]
    try:
        addr = ipaddress.ip_address(host)
        return str(addr) if _ff15_public(addr) else None
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return None
    first = None
    for info in infos or []:
        try:
            addr = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            return None
        if not _ff15_public(addr):
            return None
        if first is None:
            first = str(addr)
    return first


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
def _setup_ff15():
    for k, v in _FF15_DEFAULTS.items():
        CONFIG.setdefault(k, v)
# ==== flowfix15:end ====


# ==== flowfix16:begin (generated by flow_fix16_patch.py -- re-run to refresh, --revert to remove) ====
# flowfix16: critical bug fixes & small-model improvements for review, explain,
# plan-driven implementation, and automatic Plan/TODO tracking.


def _ff16_c(key, default=None):
    return _flow_c(key, _FF16_DEFAULTS.get(key, default))


# --------------------------------------------------------------------------
# 1 & 2 & 3. Plan-document / multi-turn plan extraction, multi-file act completion,
#            native [Plan & TODO Progress] ledger, and todo-tool passthrough
# --------------------------------------------------------------------------
_FF16_PLAN_DOC_RE = re.compile(
    r"(?:^|/)(?:.*[-_])?(?:plan|todo|tasks?|spec|design|roadmap|implementation|checklist)"
    r"(?:[-_.\w]*)?\.(?:md|txt|rst)$", re.I)
_FF16_PLAN_REF_RE = re.compile(
    r"\b(?:plan|ke\s+hoach|todo|checklist|spec|design|buoc|steps?)\b", re.I)
_FF16_TODO_TOOL_RE = re.compile(
    r"(?i)^(?:todo_?(?:write|read|update|list|add)|write_?todos?|read_?todos?"
    r"|task_?(?:create|update|list|get))$")
_FF16_STEP_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s*\[[ xX/\-]\]\s+|\d+[.)]\s+|(?:step|buoc)\s+\d+\s*[:.\-]\s+)(.+)$",
    re.I)


def _ff16_is_todo_tool(name) -> bool:
    return bool(_FF16_TODO_TOOL_RE.match(str(name or "").strip()))


class _Ff16KindStr(str):
    """`str` subclass so `tool_kind('todowrite') == 'other'` stays True while
    `_Ff16KindSet` can distinguish todo tools from unrelated `'other'` tools."""
    def __new__(cls, value, is_todo=False):
        obj = super().__new__(cls, value)
        obj.is_todo = bool(is_todo)
        return obj


class _Ff16KindSet(set):
    def __contains__(self, item):
        if super().__contains__(item):
            return True
        if getattr(item, "is_todo", False) and super().__contains__("todo"):
            return True
        return False


def _ff16_is_plan_doc(path) -> bool:
    return bool(_FF16_PLAN_DOC_RE.search(_norm(path or "")))


def _ff16_last_assistant_plan(messages) -> str:
    """Return the most recent assistant message before the last user turn if it
    looks like an implementation plan or checklist."""
    idx = T._last_user_index(messages)
    if idx <= 0:
        return ""
    for m in reversed(messages[:idx]):
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        txt = T._stringify_content(m.get("content", "")).strip()
        if not txt:
            continue
        if (re.search(r"(?m)^\s*(?:#{1,4}\s*.*(?:plan|ke\s+hoach|todo|steps?)|\d+[.)]\s+\S|[-*]\s*\[[ xX]\]\s+\S)", txt, re.I)
                or len([f for f in T._files_in(T._strip_code_spans(txt))
                        if f.lower() not in T._NON_FILE_NAMES]) >= 1):
            return txt
    return ""


def _ff16_read_plan_bodies_s1(messages) -> list:
    """[(path, text)] for any plan/doc/todo file read in the conversation."""
    out = []
    last_user = _ff5_last_user_text(messages) if messages else ""
    folded_user = _ff5_fold(last_user) if last_user else ""
    plan_ref = bool(_FF16_PLAN_REF_RE.search(folded_user))
    user_files = set(f.lower() for f in T._files_in(T._strip_code_spans(last_user or "")))
    for e in _answered_calls(messages):
        if e.get("kind") != "read" or not _read_ok(e):
            continue
        p = _call_path(e.get("args") or {})
        if not p:
            continue
        if _ff16_is_plan_doc(p) or (
                _is_doc(p) and (plan_ref or any(T._path_seen(p, [uf]) for uf in user_files))):
            out.append((p, _clean_file_text(e.get("result") or "")))
    return out


_FF16_SECTION_COMMENT_RE = re.compile(r"#\s*Section:\s*\S+", re.I)
_FF16_SINGLE_FILE_RULE_RE = re.compile(
    r"single[-\s]+file\s+architecture|khong\s+tach\s+them\s+file",
    re.I,
)
_FF16_CHECKBOX_LINE_RE = re.compile(r"^\s*[-*+]\s*\[[ xX/\-]\]\s+(.+)$")


def _ff16_plan_files(ctx, messages, listing=()) -> list:
    """Source files referenced by a read plan document or a prior assistant plan."""
    if not _ff16_c("flow16_plan_todo"):
        return []
    sources = []
    for _p, body in _ff16_read_plan_bodies(messages):
        sources.append(body)
    folded = str(getattr(ctx, "folded", "") or "")
    if _FF16_PLAN_REF_RE.search(folded):
        prev_plan = _ff16_last_assistant_plan(messages)
        if prev_plan:
            sources.append(prev_plan)
    if not sources:
        return []
    raw_blob = "\n".join(sources)
    folded_blob = T._fold_diacritics(raw_blob).lower()
    single_file_only = bool(_FF16_SINGLE_FILE_RULE_RE.search(folded_blob))
    # Strip `# Section: foo.py` headers and fenced code/JSON blocks before scanning for file names
    clean_blob = _FF16_SECTION_COMMENT_RE.sub(" ", T._strip_fences(raw_blob))
    out, seen = [], set()
    for f in sorted(T._files_in(clean_blob), key=clean_blob.find):
        if f.lower() in T._NON_FILE_NAMES or _ff16_is_plan_doc(f) or _is_doc(f):
            continue
        in_listing = bool(listing) and any(_ff12_same_file(f, lf) for lf in listing)
        if not in_listing:
            # Non-existing files mentioned in a plan must be code/test files (not example .json/.log data)
            if not f.lower().endswith(_SRC_EXT):
                continue
            if single_file_only and not _is_test_path(f):
                continue
        resolved = _resolve(f, listing) if listing else _norm(f)
        low = resolved.lower()
        if low not in seen:
            seen.add(low)
            out.append(resolved)
    return out


def _ff16_extract_todo_blocks(ctx, messages) -> list:
    """Return `[(step_title, step_full_text_with_subbullets)]` from user request or plan docs.
    When a blob contains markdown checkbox lines (`- [ ] ...`), only checkbox items (plus their
    indented sub-bullets) are extracted from that blob so numbered prose constraints in
    `## Constraints` or `## Verification` sections are never mistaken for TODO steps."""
    if not _ff16_c("flow16_plan_todo"):
        return []
    blobs = []
    last_raw = str(getattr(ctx, "request", "") or getattr(ctx, "last", "") or "")
    if last_raw:
        blobs.append(last_raw)
    for _p, body in _ff16_read_plan_bodies(messages):
        blobs.append(body)
    folded = str(getattr(ctx, "folded", "") or "")
    if _FF16_PLAN_REF_RE.search(folded):
        prev = _ff16_last_assistant_plan(messages)
        if prev:
            blobs.append(prev)
    items, seen = [], set()
    for blob in blobs:
        lines = blob.splitlines()
        has_checkbox = any(_FF16_CHECKBOX_LINE_RE.match(ln) for ln in lines)
        current_heading = ""
        i = 0
        while i < len(lines):
            ln = lines[i]
            if ln.lstrip().startswith("#"):
                current_heading = ln.strip()
                i += 1
                continue
            m = _FF16_CHECKBOX_LINE_RE.match(ln) if has_checkbox else _FF16_STEP_LINE_RE.match(ln)
            if not m:
                i += 1
                continue
            step_txt = " ".join(m.group(1).strip().split())[:140]
            base_indent = len(ln) - len(ln.lstrip())
            sub_lines = [current_heading, ln] if current_heading else [ln]
            j = i + 1
            while j < len(lines):
                nxt_ln = lines[j]
                if not nxt_ln.strip():
                    j += 1
                    continue
                nxt_indent = len(nxt_ln) - len(nxt_ln.lstrip())
                if _FF16_CHECKBOX_LINE_RE.match(nxt_ln) and nxt_indent <= base_indent:
                    break
                if not has_checkbox and _FF16_STEP_LINE_RE.match(nxt_ln) and nxt_indent <= base_indent:
                    break
                if nxt_ln.lstrip().startswith("#"):
                    break
                if nxt_indent > base_indent:
                    sub_lines.append(nxt_ln)
                    j += 1
                else:
                    break
            i = j
            key = step_txt.lower()
            if len(step_txt) >= 4 and key not in seen:
                seen.add(key)
                items.append((step_txt, "\n".join(sub_lines)))
            if len(items) >= 12:
                return items
        if not items and not has_checkbox and re.search(r"\b1[.)]\s+\S+.*\b2[.)]\s+\S+", blob):
            for part in re.split(r"(?=\b\d+[.)]\s+)", blob):
                m = re.match(r"^\s*\d+[.)]\s+(.+)$", part.strip())
                if m:
                    step_txt = " ".join(m.group(1).strip().split())[:140]
                    key = step_txt.lower()
                    if len(step_txt) >= 4 and key not in seen:
                        seen.add(key)
                        items.append((step_txt, part.strip()))
    return items[:12]


def _ff16_extract_todo_items(ctx, messages) -> list:
    """Extract ordered plan/TODO step titles."""
    return [title for title, _full in _ff16_extract_todo_blocks(ctx, messages)]


def _ff16_planned_code_targets(ctx, ev, messages=None) -> list:
    """All non-doc, non-test code files explicitly targeted by the user or plan."""
    listing = _listing_files(ev)
    named = [_resolve(f, listing) for f in _named_files(ctx)]
    if messages is not None:
        for pf in _ff16_plan_files(ctx, messages, listing):
            if not any(_ff12_same_file(pf, x) for x in named):
                named.append(pf)
    out = []
    for f in _existing(named, listing):
        if _is_doc(f) or _ff16_is_plan_doc(f) or _is_test_path(f):
            continue
        if f.lower().endswith(_SRC_EXT) and not any(_ff12_same_file(f, x) for x in out):
            out.append(f)
    return out


def _ff16_todo_ledger_s1(ctx, messages) -> str:
    """Build a compact `[Plan & TODO Progress]` tracker for small models."""
    if not _ff16_c("flow16_plan_todo") or ctx is None:
        return ""
    pat = getattr(ctx, "pattern", None)
    if pat not in ("CHANGE", "CREATE", "DEBUG", "GENERIC_REVIEW", "FILE_REVIEW", "EXPLAIN"):
        return ""
    ev = _answered_calls(messages)
    read_files = sorted({_call_path(e["args"]) for e in ev
                         if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])})
    edited_files = sorted({_call_path(e["args"]) for e in ev
                           if e["kind"] == "act" and _act_ok(e) and _call_path(e["args"])})
    tested = _tests_ran_after_act(ev) if edited_files else bool(_test_runs(ev))
    steps = _ff16_extract_todo_items(ctx, messages)
    targets = _ff16_planned_code_targets(ctx, ev, messages)
    if not steps and len(targets) < 2:
        return ""
    lines = ["[Plan & TODO Progress]"]
    if targets:
        t_parts = []
        for t in targets[:8]:
            done_edit = any(_ff12_same_file(t, p) for p in edited_files)
            done_read = any(_ff12_same_file(t, p) for p in read_files)
            if pat in ("CHANGE", "CREATE") or (pat == "DEBUG" and getattr(ctx, "fix", False)):
                state = "[x] edited" if done_edit else ("[r] read, pending edit" if done_read else "[ ] unread")
            else:
                state = "[x] read" if done_read else "[ ] unread"
            t_parts.append(f"{t} ({state})")
        lines.append("Target files: " + ", ".join(t_parts))
    if steps:
        s_parts = []
        for idx, st in enumerate(steps[:8], 1):
            st_files = [f for f in T._files_in(st) if f.lower() not in T._NON_FILE_NAMES]
            if st_files:
                if pat in ("CHANGE", "CREATE"):
                    done = all(any(_ff12_same_file(sf, ef) for ef in edited_files)
                               or (_is_test_path(sf) and tested)
                               for sf in st_files)
                else:
                    done = all(any(_ff12_same_file(sf, rf) for rf in read_files)
                               for sf in st_files)
            elif re.search(r"\b(?:test|unittest|pytest|verify|kiem\s+tra)\b", st, re.I):
                done = tested
            else:
                done = False
            mark = "[x]" if done else "[ ]"
            s_parts.append(f"{idx}. {mark} {st}")
        lines.append("Steps: " + " | ".join(s_parts))
    return "\n".join(lines)




def _ff5_scope(ctx, messages):
    scope = _ff5_scope_s1(ctx, messages)
    if not _ff16_c("flow16_plan_todo") or ctx is None:
        return scope
    try:
        pattern = _ff5_pat(getattr(ctx, "pattern", None))
        if pattern not in ("CHANGE", "DEBUG", "CREATE"):
            return scope
        ev = _answered_calls(messages)
        listing = _listing_files(ev)
        plan_targets = _ff16_plan_files(ctx, messages, listing)
        reads = [_call_path(e["args"]) for e in ev
                 if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])
                 and not _is_doc(_call_path(e["args"]))
                 and not _ff16_is_plan_doc(_call_path(e["args"]))]
        if scope:
            merged = list(scope)
            only_docs_named = all(_is_doc(x) or _ff16_is_plan_doc(x) for x in merged)
            for extra_path in plan_targets + (reads if only_docs_named else []):
                if extra_path and not any(_ff12_same_file(extra_path, s) for s in merged):
                    merged.append(extra_path)
            return merged if merged else None
        if plan_targets:
            return plan_targets
    except Exception as ex:
        log("[fix16] scope expansion error: %s" % ex)
    return scope




def _plan_change_s3(ctx, ev, tm, pat="CHANGE", total=5):
    """Ensure plan documents (`PLAN.md`/`TODO.md`) and multi-turn plans have their
    referenced source files surveyed and read before entering `act`."""
    if not _ff16_c("flow16_plan_todo"):
        return _plan_change_s2(ctx, ev, tm, pat=pat, total=total)
    try:
        msgs = getattr(_ff8_tls, "messages", None)
        listing = _listing_files(ev)
        read_set = _read_set(ev)
        batch = max(1, int(_cfg("flow_read_batch")))
        can_read = bool(tm.first("read"))
        named = _named_files(ctx)
        known = _existing([_resolve(f, listing) for f in named], listing)

        # 1. Read any explicitly named files (including PLAN.md / TODO.md) first.
        if known and pat != "CREATE":
            if not _surveyed(ev) and _ff9_bare_unread(ctx, ev):
                s = _survey_step(pat, tm, ev, 1, total)
                if s is not None:
                    return s
            unread_known = [f for f in known if not T._path_seen(f, read_set)]
            if unread_known and can_read:
                return _read_step(pat, tm, unread_known[:batch], 2, total)

        # 2. Extract target files from any read plan doc or prior assistant plan.
        plan_targets = _ff16_plan_files(ctx, msgs or [], listing) if msgs is not None else []
        if plan_targets and pat != "CREATE":
            bare_plan = [f for f in plan_targets
                         if "/" not in _norm(f) and _norm(f).lower().endswith(_SRC_EXT)
                         and not T._path_seen(f, read_set)]
            if bare_plan and not _surveyed(ev):
                s = _survey_step(pat, tm, ev, 1, total)
                if s is not None:
                    return s
            listing = _listing_files(ev)
            created_set = T._created_files(ctx.scannable or "", ctx.folded or "")
            resolved_plan = _existing([_resolve(f, listing) for f in plan_targets], listing)
            if listing:
                resolved_plan = [
                    f for f in resolved_plan
                    if any(_ff12_same_file(f, lf) for lf in listing)
                    and _norm(f).lower() not in created_set
                    and _base(f).lower() not in created_set
                ]
            unread_plan = [f for f in resolved_plan if not T._path_seen(f, read_set)]
            if unread_plan and can_read:
                return _read_step(
                    pat, tm, unread_plan[:batch], 2, total,
                    instruction="Read the target file(s) specified in the plan before editing: "
                                + ", ".join(unread_plan[:batch]) + ".")

        # 3. If `known` only contained doc/plan files (`PLAN.md`) and no code file has
        #    been read yet, do NOT skip surveying/reading the project's code!
        code_read = _code_reads(read_set)
        only_doc_known = bool(known) and all(_is_doc(f) or _ff16_is_plan_doc(f) for f in known)
        if only_doc_known and not code_read:
            if not _surveyed(ev):
                s = _survey_step(pat, tm, ev, 1, total)
                if s is not None:
                    return s
            plan_bodies = "\n".join(b for _p, b in _ff16_read_plan_bodies(msgs or []))
            kws = _keywords((ctx.request or "") + "\n" + plan_bodies)
            if (kws and tm.first("search")
                    and not any(e["kind"] == "search" for e in ev)):
                scall = tm.make_call("search", pattern="(?i)" + "|".join(
                    re.escape(k) for k in kws[:3]))
                if scall:
                    skinds = {"survey", "search", "exec"}
                    return Step(pat, "search", 2, total, skinds,
                                "Locate the code referenced by the plan with grep first.",
                                [scall], True, tm.names(skinds))
            core = _core_sources(listing, False, 200)
            ranked = _rank(core, kws, _grep_hits(ev, core))[:2] or core[:2]
            if ranked and can_read:
                return _read_step(pat, tm, ranked, 2, total)
    except Exception as ex:
        log("[fix16] plan_change error: %s" % ex)
    return _plan_change_s2(ctx, ev, tm, pat=pat, total=total)




def _change_tail_s4(pat, ctx, ev, tm, names_all, total, extra=""):
    """Keep multi-file plan implementations in `act` until every planned, read
    source file has been edited (up to flow_max_acts)."""
    if _ff16_c("flow16_multifile_act") and tm.first("act"):
        try:
            msgs = getattr(_ff8_tls, "messages", None)
            acts = [e for e in ev if e["kind"] == "act"]
            max_acts = int(_cfg("flow_max_acts"))
            ok_acts = [e for e in acts if _act_ok(e)]
            if ok_acts and len(acts) < max_acts and _act_ok(acts[-1]):
                read_set = _read_set(ev)
                edited_paths = [_call_path(e["args"]) for e in ok_acts if _call_path(e["args"])]
                planned = _ff16_planned_code_targets(ctx, ev, msgs)
                read_planned = [f for f in planned if T._path_seen(f, read_set)]
                remaining = [f for f in read_planned
                             if not any(_ff12_same_file(f, ep) for ep in edited_paths)]
                if len(read_planned) >= 2 and remaining:
                    rem_str = ", ".join(remaining[:4])
                    return Step(
                        pat, "act", 3, total, {"act"},
                        "Continue implementing the plan: you have edited "
                        + ", ".join(edited_paths) + ", but still need to edit: "
                        + rem_str + ". Emit the edit/write tool_call for "
                        + remaining[0] + " now (copying oldString exactly without "
                        "line-number prefixes)." + _DO_NOT_DESCRIBE + extra,
                        None, False, tm.names({"act"}))
        except Exception as ex:
            log("[fix16] multifile act check error: %s" % ex)
    return _change_tail_s3(pat, ctx, ev, tm, names_all, total, extra=extra)




def _ff5_after_plan_s2(step, messages, tm, ctx=None):
    step = _ff5_after_plan_s1(step, messages, tm, ctx)
    if step is None or not _ff16_c("flow16_plan_todo"):
        return step
    try:
        if ctx is None:
            ctx = _analyze(messages)
        if tm is not None and not getattr(step, "drive", False) and step.kinds:
            ev_now = _answered_calls(messages)
            just_did_todo_only = (
                getattr(step, "name", None) == "act"
                and bool(ev_now)
                and _ff16_is_todo_tool(ev_now[-1].get("name", ""))
            )
            todo_tools = [
                n for n in tm.by_kind.get("other", []) if _ff16_is_todo_tool(n)
            ]
            if todo_tools and not just_did_todo_only:
                step.kinds = _Ff16KindSet(set(step.kinds) | {"todo"})
                if getattr(step, "name", None) != "act":
                    for tn in todo_tools:
                        if tn not in step.tool_names:
                            step.tool_names.append(tn)
        if not getattr(step, "drive", False) and getattr(step, "local_text", None) is None:
            ledger = _ff16_todo_ledger(ctx, messages)
            if ledger and "[Plan & TODO Progress]" not in (step.instruction or ""):
                step.instruction = (step.instruction or "") + "\n" + ledger
    except Exception as ex:
        log("[fix16] after_plan todo error: %s" % ex)
    return step




def tool_kind(name):
    res = tool_kind_s2(name)
    if _ff16_c("flow16_plan_todo") and res == "other" and _ff16_is_todo_tool(name):
        return _Ff16KindStr("other", is_todo=True)
    return res


# --------------------------------------------------------------------------
# 4. Map-reduce review: number lines before chunking & fix _verify_findings
# --------------------------------------------------------------------------


def _ff16_number_body(body: str) -> str:
    lines = (body or "").split("\n")
    if T._already_numbered(lines):
        return body
    width = max(4, len(str(len(lines))))
    return "\n".join(f"{i:>{width}} | {ln}" for i, ln in enumerate(lines, 1))


def _make_chunks(files, max_bytes, max_files):
    if not _ff16_c("flow16_review_quotes"):
        return _make_chunks_s1(files, max_bytes, max_files)
    numbered_files = [(p, _ff16_number_body(body)) for p, body in files]
    return _make_chunks_s1(numbered_files, max_bytes, max_files)


_FF16_LOC_SPAN_RE = re.compile(
    r"^\s*(?:[A-Za-z]:)?[^\s:`'\"<>|*?]+?\.[A-Za-z0-9]{1,8}(?::\d+(?:-\d+)?)?\s*$")


def _verify_findings(text, chunk):
    """Verify map-reduce findings without dropping items that wrap `path:line`
    in backticks or mention symbols/fixes in backticks in the explanation."""
    if not _ff16_c("flow16_review_quotes"):
        return _verify_findings_s1(text, chunk)
    kept, dropped = [], 0
    if re.fullmatch(r"\W*NONE\W*", (text or "").strip(), re.IGNORECASE):
        return kept, dropped
    files = [(p, b) for p, b in chunk]
    for item in _split_findings(text):
        parts = re.split(r"\s+--\s+", item, maxsplit=2)
        if len(parts) >= 2:
            loc_part, quote_part = parts[0], parts[1]
        else:
            loc_part, quote_part = item.split("`", 1)[0], item
        loc_clean = loc_part.replace("`", " ")
        m = re.search(r"((?:[A-Za-z]:)?[^\s:`'\"<>|*?]+?\.[A-Za-z0-9]{1,8})"
                      r"(?:\s*\(part\s+\d+\))?\s*(?::\s*(\d+))?", loc_clean)
        line = int(m.group(2)) if m and m.group(2) else None
        if m:
            want = m.group(1).replace("\\", "/")
            cand = [(p, b) for p, b in files
                    if T._path_seen(want, [_FF5_PART_RE.sub("", p).replace("\\", "/")])]
            if not cand:
                dropped += 1
                continue
        else:
            cand = files
        quote_snips = [s for s in re.findall(r"`([^`\n]+)`", quote_part)
                       if len(s) >= 6 and not _FF16_LOC_SPAN_RE.match(s)]
        if not quote_snips:
            quote_snips = [s for s in re.findall(r"`([^`\n]+)`", item)
                           if len(s) >= 8 and not _FF16_LOC_SPAN_RE.match(s)]
        if not quote_snips:
            dropped += 1
            continue
        if len(parts) >= 2:
            ok = all(_ff5_quote_ok(s, cand, line) for s in quote_snips)
        else:
            ok = _ff5_quote_ok(quote_snips[0], cand, line)
        if ok:
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


# --------------------------------------------------------------------------
# 5. Cross-module static checker: join continued reads, record constants/vars/
#    re-exports/nested defs, and validate required kwonlyargs
# --------------------------------------------------------------------------


def _ff16_collect_defs(stmts, out):
    for node in stmts or ():
        if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
            a = node.args
            pos = [x.arg for x in list(getattr(a, "posonlyargs", ())) + list(a.args)]
            kwonly = [x.arg for x in a.kwonlyargs]
            kw_defaults = list(getattr(a, "kw_defaults", ()) or ())
            req_kwonly = [
                k for k, d in zip(kwonly, kw_defaults) if d is None
            ]
            out[node.name] = {
                "kind": "function",
                "required": max(0, len(pos) - len(a.defaults)),
                "total": len(pos),
                "vararg": a.vararg is not None,
                "varkw": a.kwarg is not None,
                "names": pos,
                "kwonly": kwonly,
                "req_kwonly": req_kwonly,
                "decorated": bool(node.decorator_list),
                "line": getattr(node, "lineno", 0),
            }
        elif isinstance(node, _ff5_ast.ClassDef):
            out[node.name] = {"kind": "class", "required": 0, "total": 0,
                              "vararg": True, "line": getattr(node, "lineno", 0)}
        elif isinstance(node, _ff5_ast.Assign):
            for t in node.targets:
                for n in _ff5_ast.walk(t):
                    if isinstance(n, _ff5_ast.Name):
                        out.setdefault(n.id, {"kind": "value", "line": getattr(node, "lineno", 0)})
        elif isinstance(node, (_ff5_ast.AnnAssign, _ff5_ast.AugAssign)):
            for n in _ff5_ast.walk(node.target):
                if isinstance(n, _ff5_ast.Name):
                    out.setdefault(n.id, {"kind": "value", "line": getattr(node, "lineno", 0)})
        elif isinstance(node, (_ff5_ast.Import, _ff5_ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    bound = alias.asname or alias.name.split(".")[0]
                    out.setdefault(bound, {"kind": "import", "line": getattr(node, "lineno", 0)})
        elif isinstance(node, (_ff5_ast.If, _ff5_ast.Try, _ff5_ast.With, _ff5_ast.AsyncWith)):
            for attr in ("body", "orelse", "finalbody"):
                _ff16_collect_defs(getattr(node, attr, None), out)
            for h in getattr(node, "handlers", ()) or ():
                _ff16_collect_defs(getattr(h, "body", None), out)


def _ff5_py_defs(tree):
    if not _ff16_c("flow16_cross_module_constants"):
        return _ff5_py_defs_s1(tree)
    out = {}
    _ff16_collect_defs(getattr(tree, "body", ()), out)
    out.setdefault("__all__", {"kind": "value", "line": 0})
    out.setdefault("__version__", {"kind": "value", "line": 0})
    return out


def _ff5_py_modules(messages):
    if not _ff16_c("flow16_cross_module_constants"):
        return _ff5_py_modules_s1(messages)
    mods = {}
    for path, body in _ff9_read_texts(messages).items():
        if not path or not path.lower().endswith(".py"):
            continue
        try:
            tree = _ff5_ast.parse(body)
        except (SyntaxError, ValueError):
            continue
        stem = _base(path)[:-3]
        mods.setdefault(stem, []).append({
            "path": path, "tree": tree, "defs": _ff5_py_defs(tree)})
    return mods


def _ff5_arg_problem(node, info):
    prob = _ff5_arg_problem_s1(node, info)
    if prob or not _ff16_c("flow16_cross_module_constants"):
        return prob
    if info.get("kind") != "function" or info.get("decorated"):
        return ""
    if any(k.arg is None for k in node.keywords):
        return ""
    kw = {k.arg for k in node.keywords}
    req_kw = [k for k in (info.get("req_kwonly") or ()) if k not in kw]
    if req_kw:
        return "missing required keyword-only argument(s): %s" % ", ".join(req_kw)
    return ""


# --------------------------------------------------------------------------
# 6. EXPLAIN: no pointless grep after reading a named code file; read code after
#    a named doc file; infer local import paths even when started from grep
# --------------------------------------------------------------------------
_SKIP_FENCE_LANGS.update({
    "text", "plaintext", "txt", "mermaid", "markdown", "md", "ascii", "diagram"
})
T._SKIP_FENCE_LANGS = _SKIP_FENCE_LANGS



_STDLIB_MODS = getattr(sys, "stdlib_module_names", frozenset()) | {
    "os", "sys", "re", "json", "time", "math", "uuid", "hmac", "hashlib",
    "socket", "select", "ssl", "urllib", "http", "html", "email", "logging",
    "threading", "multiprocessing", "subprocess", "shutil", "pathlib",
    "posixpath", "ntpath", "glob", "fnmatch", "io", "tempfile", "collections",
    "itertools", "functools", "operator", "typing", "dataclasses", "enum",
    "abc", "contextlib", "copy", "pprint", "traceback", "inspect", "ast",
    "dis", "unittest", "doctest", "argparse", "configparser", "csv", "sqlite3",
    "zipfile", "tarfile", "gzip", "bz2", "lzma", "base64", "binascii",
    "struct", "codecs", "unicodedata", "string", "textwrap", "difflib",
    "random", "secrets", "statistics", "decimal", "fractions", "numbers",
    "datetime", "calendar", "zoneinfo", "ipaddress", "signal", "errno",
    "ctypes", "platform", "sysconfig", "warnings", "weakref", "gc", "atexit",
    "queue", "asyncio", "concurrent", "pytest", "httpx", "requests", "flask",
    "fastapi", "pydantic", "numpy", "pandas", "scipy", "matplotlib", "yaml",
    "toml", "tomli", "dotenv", "aiohttp", "websockets", "werkzeug", "jinja2",
    "click", "rich", "tqdm", "PIL", "cv2", "torch", "tensorflow", "sklearn",
}


def local_imports(path, text, listing, scored=False):
    """Also infer sibling/package paths when `listing` came from `grep` (partial)
    rather than a full-repo `glob`."""
    res = local_imports_s1(path, text, listing, scored=scored)
    if res or not _ff16_c("flow16_explain_imports"):
        return res
    if len(listing or ()) >= 4:
        return res
    text = text or ""
    norm_p = _norm(path or "")
    cur_dir = posixpath.dirname(norm_p)
    pkg_root = norm_p.split("/", 1)[0] if ("/" in norm_p and not re.match(r"^[A-Za-z]:/", norm_p)) else ""
    self_stem = posixpath.splitext(_base(norm_p))[0]
    augmented = list(listing or [])
    for m in re.finditer(r"(?m)^[ \t]*from\s+([\w.]+)\s+import\s+\(?([^\n#]+)", text):
        base_mod = m.group(1)
        names = [n.strip().strip("()").split(" as ")[0].strip()
                 for n in m.group(2).split(",") if n.strip().strip("()")]
        stripped_base = base_mod.lstrip(".")
        top_pkg = stripped_base.split(".", 1)[0] if stripped_base else ""
        if top_pkg and (top_pkg in _STDLIB_MODS or top_pkg == self_stem):
            continue
        if not stripped_base:
            candidates = [base_mod + nm for nm in names if re.match(r"^\w+$", nm)]
        elif "." not in stripped_base and pkg_root and stripped_base == pkg_root:
            candidates = [f"{stripped_base}.{nm}" for nm in names if re.match(r"^\w+$", nm)]
        else:
            candidates = [base_mod]
        for mod in candidates:
            rel = "/".join(x for x in mod.lstrip(".").split(".") if x)
            if not rel:
                continue
            if mod.startswith(".") and cur_dir:
                cand_path = posixpath.normpath(posixpath.join(cur_dir, rel)) + ".py"
            else:
                cand_path = rel + ".py"
                if pkg_root and not cand_path.startswith(pkg_root + "/") and cur_dir:
                    sib = posixpath.join(cur_dir, rel + ".py")
                    if sib not in augmented:
                        augmented.append(sib)
            if cand_path not in augmented:
                augmented.append(cand_path)
    for m in re.finditer(r"(?m)^[ \t]*import\s+([\w.]+)", text):
        top_pkg = m.group(1).split(".", 1)[0]
        if top_pkg in _STDLIB_MODS or top_pkg == self_stem:
            continue
        rel = "/".join(x for x in m.group(1).split(".") if x)
        if rel and (rel + ".py") not in augmented:
            augmented.append(rel + ".py")
    if len(augmented) == len(listing or ()):
        return res
    return local_imports_s1(path, text, augmented, scored=scored)


def _plan_explain(ctx, ev, tm):
    if not _ff16_c("flow16_explain_imports"):
        return _plan_explain_s2(ctx, ev, tm)
    pat, total = "EXPLAIN", 4
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    can_read = bool(tm.first("read"))
    named = _named_files(ctx)
    known = [_resolve(f, listing) for f in named]
    if known and can_read:
        unread = [f for f in known if not T._path_seen(f, read_set)]
        if unread:
            return _read_step(pat, tm, unread[:6], 1, total)

    code_read = _code_reads(read_set)
    only_doc_known = bool(known) and all(_is_doc(f) or _ff16_is_plan_doc(f) for f in known)
    wants_code_architecture = bool(re.search(
        r"\b(?:source|code|codebase|ma\s+nguon|kien\s+truc|architecture|flow|luong|ham|function|class|module)\b",
        str(getattr(ctx, "folded", "") or ""), re.I))

    need_discovery = (not known) or (only_doc_known and not code_read and wants_code_architecture)
    kws = _keywords(ctx.request)
    if need_discovery and not _surveyed(ev):
        call = None
        if kws and tm.first("search"):
            call = tm.make_call("search", pattern="(?i)" + "|".join(
                re.escape(k) for k in kws[:3]))
        if call:
            kinds = {"survey", "search", "exec"}
            return Step(pat, "search", 1, total, kinds,
                        "Locate the relevant code with grep first.", [call],
                        True, tm.names(kinds))
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s

    hits = _search_hits(ev)
    if not code_read and need_discovery and can_read:
        if hits:
            pool = list(hits)
            pool = _core_sources(pool, True, 400) or pool
            ranked = _rank(pool, kws, hits)[:2] or pool[:1]
            if ranked:
                return _read_step(pat, tm, ranked, 2, total)
        if not listing and _survey_count(ev) < 2:
            s = _survey_step(pat, tm, ev, 2, total)
            if s:
                return s
        docs = [d for d in _doc_files(listing) if not T._path_seen(d, read_set)]
        if docs:
            return _read_step(pat, tm, docs[:2], 2, total,
                              instruction="Read the project docs first: "
                                          + ", ".join(docs[:2]) + ".")
        entry = [f for f in _core_sources(listing, False, 200)
                 if not T._path_seen(f, read_set)][:3]
        if entry:
            return _read_step(pat, tm, entry, 3, total,
                              instruction="Read the main source files: "
                                          + ", ".join(entry) + ".")
    if not ev:
        return None
    step = _custom_answer(pat, _explain_text(sorted(read_set)), 4, total)
    try:
        step = _ff14_explain_hop(ctx, ev, tm, step)
    except Exception as ex:
        log("[fix16] explain hop error: %s" % ex)
    return _ff14_restore_case(step, ev)


# --------------------------------------------------------------------------
# 7. flowfix13 runtime check (`_rf_gwtr`): keep it on the outside of install()
#    and prevent `_analyze` from firing upstream classify on runtime checks
# --------------------------------------------------------------------------


def _analyze(messages):
    if _ff16_c("flow16_runtime_outer") and CONFIG.get("runtime_check_local", True):
        try:
            if _rf_runtime_question(messages):
                idx = T._last_user_index(messages)
                ctx = _Ctx()
                ctx.idx = idx
                ctx.last = T._stringify_content(messages[idx].get("content", "")) if idx >= 0 else ""
                ctx.request = ctx.last
                ctx.scannable = T._strip_code_spans(ctx.request)
                ctx.folded = re.sub(r"\s+", " ", T._fold_diacritics(ctx.scannable)).strip()
                ctx.pattern = None
                ctx.free_run = True
                ctx.perm = _ff5_permissions(messages, None, False)
                return ctx
        except Exception:
            pass
    return _analyze_s6(messages)


# --------------------------------------------------------------------------
# 8. SSE heartbeat cleanup when stream is empty, non-blocking MSG_PEEK disconnect
#    detection, & `/v1/responses` delta heartbeat wiring
# --------------------------------------------------------------------------


class _Ff16ClientGone:
    """Wraps a `threading.Event` and also checks non-blocking `MSG_PEEK` on the
    client socket so an aborted SSE connection is detected immediately instead
    of waiting up to `SSE_PING_INTERVAL_SEC` (5s) for the next heartbeat write."""
    def __init__(self, base_event, sock):
        self._ev = base_event
        self._sock = sock

    def set(self):
        self._ev.set()

    def clear(self):
        self._ev.clear()

    def wait(self, timeout=None):
        return self._ev.wait(timeout)

    def is_set(self) -> bool:
        if self._ev.is_set():
            return True
        sock = self._sock
        if sock is None:
            return False
        try:
            if getattr(sock, "_closed", False) or sock.fileno() < 0:
                self._ev.set()
                return True
            flags = getattr(socket, "MSG_PEEK", 0) | getattr(socket, "MSG_DONTWAIT", 0)
            if getattr(socket, "MSG_DONTWAIT", 0):
                data = sock.recv(1, flags)
                if data == b"":
                    self._ev.set()
                    return True
        except BlockingIOError:
            return False
        except (ConnectionResetError, BrokenPipeError, OSError):
            self._ev.set()
            return True
        except Exception:
            return False
        return False


def _ff16_start_sse_with_keepalive(self):
    stop_ping, client_gone, hb = _GeminiHandler_start_sse_with_keepalive_orig(self)
    if not _ff16_c("flow16_sse_cleanup"):
        return stop_ping, client_gone, hb
    wrapped_gone = _Ff16ClientGone(client_gone, getattr(self, "connection", None))
    return stop_ping, wrapped_gone, hb


def _ff16_stream_chat(self, cid, model, prompt, model_id, think_mode, file_refs,
                      extra, stop_strings, max_tokens, include_usage, ticket=None):
    if not _ff16_c("flow16_sse_cleanup"):
        return _GeminiHandler_stream_chat_orig(
            self, cid, model, prompt, model_id, think_mode, file_refs,
            extra, stop_strings, max_tokens, include_usage, ticket=ticket)
    orig_start = self._start_sse_with_keepalive
    stops = []

    def tracked_start():
        stop_ping, client_gone, hb = orig_start()
        stops.append(stop_ping)
        return stop_ping, client_gone, hb

    self._start_sse_with_keepalive = tracked_start
    try:
        return _GeminiHandler_stream_chat_orig(
            self, cid, model, prompt, model_id, think_mode, file_refs,
            extra, stop_strings, max_tokens, include_usage, ticket=ticket)
    finally:
        self._start_sse_with_keepalive = orig_start
        for sp in stops:
            try:
                sp()
            except Exception:
                pass


def _ff16_handle_responses(self, body: bytes):
    if not _ff16_c("flow16_sse_cleanup"):
        return _GeminiHandler_handle_responses_orig(self, body)
    orig_start = self._start_sse_with_keepalive
    stops = []

    def tracked_start():
        stop_ping, client_gone, hb = orig_start()
        self._chat_hb = hb
        stops.append(stop_ping)
        return stop_ping, client_gone, hb

    self._start_sse_with_keepalive = tracked_start
    try:
        return _GeminiHandler_handle_responses_orig(self, body)
    finally:
        self._start_sse_with_keepalive = orig_start
        self._chat_hb = None
        for sp in stops:
            try:
                sp()
            except Exception:
                pass


GeminiHandler._start_sse_with_keepalive = _ff16_start_sse_with_keepalive
GeminiHandler._stream_chat = _ff16_stream_chat
GeminiHandler._handle_responses = _ff16_handle_responses




def _fx_install(server):
    """Keep `_fx_install` idempotent even after `_ff5_wrap_change` wraps
    `T.change_demanded` (prevents `_FX_ORIG_CD <-> ff5.change_demanded` cycle
    when `install()` is called more than once)."""
    cur = getattr(T, "change_demanded", None)
    if cur is _change_demanded or getattr(cur, "_ff5", False):
        for k, v in _FX_DEFAULTS.items():
            CONFIG.setdefault(k, v)
        return
    return _fx_install_s1(server)


def _wrap_ff16(cur):
    @functools.wraps(cur)
    def wrapper16(self, prompt, model_id, think_mode, file_refs, extra,
                  tools_active, allowed_names, tool_schemas, required_tool,
                  messages=None, ticket=None, tool_required=None,
                  client_gone=None):
        try:
            if (_ff16_c("flow16_runtime_outer")
                    and CONFIG.get("runtime_check_local", True)
                    and not required_tool
                    and _rf_runtime_question(messages)):
                log("[flowfix16] runtime check answered at outer wrapper")
                return _rf_runtime_report(model_id, think_mode, extra, ticket), []
        except Exception as ex:
            log("[flowfix16] runtime outer check error: %s" % ex)
        return cur(self, prompt, model_id, think_mode, file_refs, extra,
                   tools_active, allowed_names, tool_schemas, required_tool,
                   messages=messages, ticket=ticket,
                   tool_required=tool_required, client_gone=client_gone)

    wrapper16._ff16_patched = True
    return wrapper16
# ==== flowfix16:end ====


# ==== flowfix17:begin (generated by flow_fix17_patch.py -- re-run to refresh, --revert to remove) ====
# flowfix17: Phase 2 roadmap enhancements for small models:
#   1. Symbol-level AST call graph & smart import slicing (`flow17_symbol_graph`)
#   2. Targeted test selection for edited modules (`flow17_targeted_tests`)
#   3. Blank-line-tolerant fuzzy edit anchor & delta re-indentation (`flow17_fuzzy_edit`)
#   4. Session-persisted Plan & TODO ledger LRU cache (`flow17_session_ledger`)
import collections as _ff17_collections
import hashlib as _ff17_hashlib



def _ff17_c(key, default=None):
    if not _flow_c("flow17_enabled", True):
        return False
    return _flow_c(key, _FF17_DEFAULTS.get(key, default))


# --------------------------------------------------------------------------
# 1. Symbol-Level AST Call Graph & Smart Import Slicing (`flow17_symbol_graph`)
# --------------------------------------------------------------------------
def _ff17_strip_numbered_lines(text):
    """Return unnumbered source text and a list mapping 0-based index -> file line number."""
    cleaned = _strip_read_envelope(text or "")
    trunc = globals().get("_FF4_TRUNC_RE")
    if trunc is not None:
        cleaned = trunc.sub("", cleaned).rstrip()
    lines = cleaned.split("\n")
    non_empty_lns = [ln for ln in lines if ln.strip() and not _READ_HEADER_RE.match(ln) and not _READ_FOOTER_RE.match(ln)]
    numbered = T._already_numbered(lines) or (
        bool(non_empty_lns) and all(re.match(r"^\s*\d+\s*(?:\||:|\t|\u2192)\s", ln) for ln in non_empty_lns)
    )
    clean_lines = []
    line_nums = []
    for i, ln in enumerate(lines):
        if _READ_HEADER_RE.match(ln) or _READ_FOOTER_RE.match(ln):
            continue
        if numbered:
            m = re.match(r"^(\s*(\d+)\s*(?:\||:|\t|\u2192)\s?)(.*)$", ln)
            if m:
                line_nums.append(int(m.group(2)))
                clean_lines.append(m.group(3))
                continue
        line_nums.append(i + 1)
        clean_lines.append(ln)
    return "\n".join(clean_lines), line_nums


def _ff17_ast_symbol_lines(text, target_symbols):
    """Return 1-based file line numbers covering full AST definitions (functions,
    classes, assignments) matching `target_symbols` (case-insensitive substring or
    exact identifier) PLUS 1-hop intra-file helper functions called inside them."""
    if not text or not target_symbols:
        return []
    clean_src, line_nums = _ff17_strip_numbered_lines(text)
    try:
        tree = _ff5_ast.parse(clean_src)
    except Exception:
        return []

    targets_low = {str(s).lower() for s in target_symbols if s and len(str(s)) >= 3}
    if not targets_low:
        return []

    defs_by_name = {}
    for node in _ff5_ast.walk(tree):
        if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef, _ff5_ast.ClassDef)):
            start = getattr(node, "lineno", None)
            if getattr(node, "decorator_list", None):
                dec_lines = [getattr(d, "lineno", start) for d in node.decorator_list if getattr(d, "lineno", None)]
                if dec_lines:
                    start = min([start] + dec_lines)
            end = getattr(node, "end_lineno", start)
            if start and end:
                defs_by_name.setdefault(node.name.lower(), []).append((node, start, end))
        elif isinstance(node, _ff5_ast.Assign):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", start)
            if start and end:
                for t in node.targets:
                    if isinstance(t, _ff5_ast.Name):
                        defs_by_name.setdefault(t.id.lower(), []).append((node, start, end))

    matched_nodes = []
    for name_low, entries in defs_by_name.items():
        if any(t == name_low or (len(t) >= 4 and t in name_low) for t in targets_low):
            matched_nodes.extend(entries)

    # 1-hop intra-file call graph: include helper functions called inside matched nodes
    callees = set()
    for node, _s, _e in list(matched_nodes):
        for sub in _ff5_ast.walk(node):
            if isinstance(sub, _ff5_ast.Call):
                fn = sub.func
                if isinstance(fn, _ff5_ast.Name):
                    callees.add(fn.id.lower())
                elif isinstance(fn, _ff5_ast.Attribute):
                    callees.add(fn.attr.lower())
    for callee in callees:
        if callee in defs_by_name:
            matched_nodes.extend(defs_by_name[callee])

    keep_file_lines = set()
    n_lines = len(line_nums)
    for _node, start_1, end_1 in matched_nodes:
        for idx in range(max(0, start_1 - 1), min(n_lines, end_1)):
            keep_file_lines.add(line_nums[idx])
    return sorted(keep_file_lines)


def _ff17_imported_symbols_by_file(ev, listing=()):
    """Map resolved local file path -> set of symbols imported/referenced from it
    by other read files in `ev`."""
    texts = _read_texts(ev)
    if not texts:
        return {}
    all_known = list(listing or []) + list(texts.keys())
    sym_map = {}

    for src_path, raw_body in texts.items():
        clean_src, _ = _ff17_strip_numbered_lines(raw_body)
        hop_files = local_imports(src_path, clean_src, all_known)
        try:
            tree = _ff5_ast.parse(clean_src)
        except Exception:
            tree = None
        if tree is None:
            continue
        mod_aliases = {}
        for node in _ff5_ast.walk(tree):
            if isinstance(node, _ff5_ast.ImportFrom):
                mod = (node.module or "").strip()
                imported = [a.name for a in node.names if a.name and a.name != "*"]
                if not imported:
                    continue
                mod_tail = mod.rsplit(".", 1)[-1].lower() if mod else ""
                for hf in hop_files:
                    hf_stem = posixpath.splitext(_base(hf))[0].lower()
                    if not mod_tail or hf_stem == mod_tail or hf_stem in [x.lower() for x in imported]:
                        sym_map.setdefault(_norm(hf), set()).update(imported)
            elif isinstance(node, _ff5_ast.Import):
                for a in node.names:
                    tail = a.name.rsplit(".", 1)[-1]
                    alias = a.asname or tail
                    mod_aliases[alias] = tail.lower()
        if mod_aliases:
            for node in _ff5_ast.walk(tree):
                if isinstance(node, _ff5_ast.Attribute) and isinstance(node.value, _ff5_ast.Name):
                    mod_stem = mod_aliases.get(node.value.id)
                    if mod_stem:
                        for hf in hop_files:
                            if posixpath.splitext(_base(hf))[0].lower() == mod_stem:
                                sym_map.setdefault(_norm(hf), set()).add(node.attr)
    return sym_map


_ff17_tls = _FLOW_STATE_TLS


def _ff8_render_view(v, kws, locs, budget):
    if not _ff17_c("flow17_symbol_graph"):
        return _ff8_render_view_s1(v, kws, locs, budget)
    extra_syms = set(kws or ())
    sym_map = getattr(_ff17_tls, "sym_map", None) or {}
    vpath = _norm(v.get("path") or "")
    for p, syms in sym_map.items():
        if T._path_seen(vpath, [p]):
            extra_syms.update(syms)
    ast_lines = _ff17_ast_symbol_lines(v.get("text") or "", extra_syms)
    merged_locs = list(locs or [])
    if ast_lines:
        existing = set(merged_locs)
        for ln in ast_lines:
            if ln not in existing:
                merged_locs.append(ln)
                existing.add(ln)
    return _ff8_render_view_s1(v, kws, merged_locs, budget)




def _ff8_source_block(ctx, stage, messages):
    if not _ff17_c("flow17_symbol_graph"):
        return _ff8_source_block_s1(ctx, stage, messages)
    try:
        ev = _answered_calls(messages)
        _ff17_tls.sym_map = _ff17_imported_symbols_by_file(ev, _listing_files(ev))
    except Exception:
        _ff17_tls.sym_map = {}
    try:
        return _ff8_source_block_s1(ctx, stage, messages)
    finally:
        _ff17_tls.sym_map = {}




def _ff14_explain_hop(ctx, ev, tm, step):
    res = _ff14_explain_hop_s1(ctx, ev, tm, step)
    if (not _ff17_c("flow17_symbol_graph") or res is None
            or res is step or getattr(res, "name", None) != "read"):
        return res
    try:
        sym_map = _ff17_imported_symbols_by_file(ev, _listing_files(ev))
        all_syms = []
        for p, syms in sym_map.items():
            for s in sorted(syms):
                if s not in all_syms:
                    all_syms.append(s)
        if all_syms and "symbols:" not in (res.instruction or ""):
            res.instruction = (
                res.instruction.rstrip(".")
                + " (focus on imported symbols: " + ", ".join(all_syms[:8]) + ")."
            )
    except Exception:
        pass
    return res


# --------------------------------------------------------------------------
# 2. Targeted Test Selection (`flow17_targeted_tests`)
# --------------------------------------------------------------------------


def _ff17_targeted_test_commands(names, ev):
    """When specific source files were edited in `ev` (or read in DEBUG), and
    matching test files exist in `names` alongside other tests, build targeted
    test commands for those specific test files first."""
    names = _live(names or [])
    all_tests = [f for f in names if _is_test_path(f) and f.lower().endswith(_SRC_EXT)]
    if len(all_tests) < 2:
        return []

    edited_paths = [
        _call_path(e["args"]) for e in (ev or [])
        if e.get("kind") == "act" and _act_ok(e) and _call_path(e.get("args") or {})
    ]
    if not edited_paths:
        return []

    matched_tests = []
    explicit_test_edits = []
    for p in edited_paths:
        if _is_test_path(p) and p.lower().endswith(_SRC_EXT):
            np = _norm(p)
            if np not in explicit_test_edits:
                explicit_test_edits.append(np)
            continue
        if _is_doc(p):
            continue
        stem = posixpath.splitext(_base(p))[0].lower()
        if len(stem) < 3 or stem == "__init__":
            continue
        for t in all_tests:
            tb = posixpath.splitext(_base(t))[0].lower()
            if (tb in (f"test_{stem}", f"{stem}_test", f"{stem}.test", f"{stem}.spec")
                    or stem in tb):
                if t not in matched_tests:
                    matched_tests.append(t)

    if explicit_test_edits:
        for tp in reversed(explicit_test_edits):
            matched_tests = [m for m in matched_tests if not _ff12_same_file(tp, m)]
            matched_tests.insert(0, tp)

    if not matched_tests:
        return []
    if not explicit_test_edits and len(all_tests) >= 2 and len(matched_tests) >= len(all_tests):
        return []

    bases = {_base(n) for n in names}
    py_pytest = bool(bases & {"pytest.ini", "conftest.py", "tox.ini"})
    py_tests = [t for t in matched_tests if t.lower().endswith(".py")]
    out = []
    if py_tests:
        if py_pytest:
            out.append("python -m pytest -q " + " ".join(py_tests[:4]))
        else:
            if len(py_tests) == 1:
                t = _norm(py_tests[0])
                t_dir = posixpath.dirname(t)
                t_base = posixpath.basename(t)
                if t_dir:
                    out.append(f'python -m unittest discover -s {t_dir} -p "{t_base}"')
                else:
                    out.append(f'python -m unittest discover -p "{t_base}"')
            else:
                out.append("python -m unittest " + " ".join(_norm(t) for t in py_tests[:4]))
    return out


def _ff17_extract_verification_commands(messages, names=None, ev=None, require_existing: bool = True) -> list:
    """Extract verification/test commands from read plan documents or prompt messages,
    sorting targeted/specific test file commands BEFORE full-suite discover commands."""
    plan_bodies = [b for _p, b in _ff16_read_plan_bodies(messages or [])]
    texts_to_scan = list(plan_bodies)
    if messages:
        for m in messages:
            if m.get("role") == "user":
                c = m.get("content") or ""
                if isinstance(c, str):
                    texts_to_scan.append(c)

    seen = set()
    raw_cmds = []
    cmd_rx = re.compile(
        r"(?m)^\s*(?:#\s*\d+\.?\s*)?((?:python(?:\d+)?\s+-m\s+unittest|pytest|npm\s+test|cargo\s+test|go\s+test)[^\r\n`]+)"
    )
    for txt in texts_to_scan:
        for m in cmd_rx.finditer(txt):
            cmd = m.group(1).strip()
            cmd = re.sub(r"\s+#.*$", "", cmd).strip()
            cmd = cmd.rstrip("`").strip()
            if cmd and cmd not in seen and _is_test_cmd(cmd):
                seen.add(cmd)
                raw_cmds.append(cmd)

    def _cmd_weight(c):
        c_low = c.lower()
        if "discover" in c_low and "-p" not in c_low:
            return 2
        if c_low in ("python -m unittest", "pytest", "npm test", "cargo test", "go test ./..."):
            return 2
        return 0

    raw_cmds.sort(key=_cmd_weight)
    if not require_existing:
        return raw_cmds

    # Filter out commands referencing .py files that do not exist yet in tracked texts or on disk
    tracked = _ff9_read_texts(messages or [])
    if ev:
        for v in _ff8_file_views(ev):
            p = v.get("path") or ""
            if p and p not in tracked:
                tracked[p] = v.get("text") or ""

    filtered = []
    for cmd in raw_cmds:
        file_refs = re.findall(r'(?:^|\s)((?:[\w./\\-]+/)*[\w-]+\.py)\b', cmd)
        if file_refs:
            if all(
                (tracked.get(f) or tracked.get(f.replace("\\", "/")) or tracked.get(f.replace("/", "\\")) or "").strip()
                or os.path.exists(f) or os.path.exists(f.replace("\\", "/"))
                for f in file_refs
            ):
                filtered.append(cmd)
        else:
            filtered.append(cmd)
    return filtered


def pick_test_command(names, ev):
    if not _ff17_c("flow17_targeted_tests"):
        return pick_test_command_s1(names, ev)
    configured = (_cfg("flow_test_command") or "").strip()
    if configured:
        return pick_test_command_s1(names, ev)
    targeted = _ff17_targeted_test_commands(names, ev)
    msgs = getattr(_ff8_tls, "messages", None)
    v_cmds = _ff17_extract_verification_commands(msgs, names, ev)

    cands = []
    for c in targeted:
        c_s = c.strip()
        if c_s and c_s not in cands:
            cands.append(c_s)
    for c in v_cmds:
        c_s = c.strip()
        if c_s and c_s not in cands:
            cands.append(c_s)
    for c in test_candidates(names):
        c_s = c.strip()
        if c_s and c_s not in cands:
            cands.append(c_s)

    if not cands:
        return pick_test_command_s1(names, ev)

    last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"), default=-1)
    runs = _test_runs(ev)
    after = [r for r in runs if r[0] > last_act]
    broken_cmds = {cmd.strip() for _i, cmd, res in runs
                   if test_outcome(res) == "broken"}
    tracked = _ff9_read_texts(getattr(_ff8_tls, "messages", None) or [])
    if ev:
        for v in _ff8_file_views(ev):
            p = v.get("path") or ""
            if p and p not in tracked:
                tracked[p] = v.get("text") or ""

    def _cmd_target_exists(cmd_s):
        file_refs = re.findall(r'(?:^|\s)((?:[\w./\\-]+/)*[\w-]+\.py)\b', cmd_s)
        if file_refs:
            return all(
                (tracked.get(f) or tracked.get(f.replace("\\", "/")) or tracked.get(f.replace("/", "\\")) or "").strip()
                or os.path.exists(f) or os.path.exists(f.replace("\\", "/"))
                for f in file_refs
            )
        return True

    if not after:
        for c in cands:
            c_s = c.strip()
            if c_s not in broken_cmds and _cmd_target_exists(c_s):
                return c_s
        return pick_test_command_s1(names, ev)
    if test_outcome(after[-1][2]) != "broken":
        return None
    if len(after) >= int(_c("fix_max_test_attempts")):
        return None
    tried = {cmd.strip() for _i, cmd, _r in after}
    for c in cands:
        c_s = c.strip()
        if c_s not in tried and c_s not in broken_cmds and _cmd_target_exists(c_s):
            return c_s
    return None


# Semantic aliases for test command helpers (Phase 5.3)
candidate_test_commands = test_candidates
extract_verification_commands = _ff17_extract_verification_commands


def is_test_command(cmd: str) -> bool:
    """Return True if the given command is recognized as a test execution command."""
    if not cmd or not isinstance(cmd, str):
        return False
    return bool(_TEST_CMD_RE.search(cmd.strip()))


# --------------------------------------------------------------------------
# 3. Blank-Line-Tolerant Fuzzy Edit Anchor & Delta Re-Indentation (`flow17_fuzzy_edit`)
# --------------------------------------------------------------------------


def _ff17_find_unique(old, hay):
    """First try exact line-count whitespace-insensitive match (`_ff14_find_unique`).
    If that misses and `flow17_fuzzy_edit` is enabled, try matching the sequence of
    non-empty normalized lines across `hay` (tolerating added/omitted blank lines
    inside the block) when it matches a single unique span in `hay`."""
    actual, n = _ff14_find_unique_s1(old, hay)
    if actual is not None or n > 1 or not _ff17_c("flow17_fuzzy_edit"):
        return actual, n

    ol_nonempty = [_ff14_nline(x) for x in old.split("\n") if _ff14_nline(x)]
    if len(ol_nonempty) < 2:
        return None, 0

    raw = hay.split("\n")
    nonempty_positions = []
    nonempty_norm = []
    for idx, ln in enumerate(raw):
        nl = _ff14_nline(ln)
        if nl:
            nonempty_positions.append(idx)
            nonempty_norm.append(nl)

    m = len(ol_nonempty)
    hits = []
    first = ol_nonempty[0]
    for k in range(len(nonempty_norm) - m + 1):
        if nonempty_norm[k] == first and nonempty_norm[k:k + m] == ol_nonempty:
            start_line = nonempty_positions[k]
            end_line = nonempty_positions[k + m - 1]
            # Guard against matching across huge unrelated gaps (> 6 extra blank lines)
            if (end_line - start_line + 1) <= (m + 6):
                hits.append((start_line, end_line))

    if len(hits) == 1:
        s_idx, e_idx = hits[0]
        return "\n".join(raw[s_idx:e_idx + 1]), 1
    return None, len(hits)


def _ff17_reindent(new, old, actual):
    """Align non-empty lines between `old` and `actual` (works even when blank-line
    counts differ) and fall back to a uniform base-indentation delta for newly
    added indentation levels in `new`."""
    if not _ff17_c("flow17_fuzzy_edit"):
        return _ff14_reindent_s1(new, old, actual)

    ol_nonempty = [x for x in old.split("\n") if x.strip()]
    al_nonempty = [x for x in actual.split("\n") if x.strip()]
    if len(ol_nonempty) != len(al_nonempty) or not ol_nonempty:
        return _ff14_reindent_s1(new, old, actual)

    mapping = {}
    deltas = []
    for o, a_ in zip(ol_nonempty, al_nonempty):
        ows = _ff14_ws(o)
        aws = _ff14_ws(a_)
        mapping.setdefault(ows, aws)
        if ows.expandtabs(4).strip() == "" and aws.expandtabs(4).strip() == "":
            deltas.append(len(aws.expandtabs(4)) - len(ows.expandtabs(4)))

    if not mapping or all(k == v for k, v in mapping.items()):
        return new

    base_delta = deltas[0] if deltas else 0
    use_tabs = any("\t" in v for v in mapping.values())
    keys = sorted(mapping, key=len, reverse=True)
    out = []
    for ln in new.split("\n"):
        if not ln.strip():
            out.append(ln)
            continue
        ws = _ff14_ws(ln)
        if ws in mapping:
            out.append(mapping[ws] + ln[len(ws):])
            continue
        k = next((k for k in keys if k and ws.startswith(k)), None)
        if k is not None:
            out.append(mapping[k] + ws[len(k):] + ln[len(ws):])
        elif base_delta != 0 and not use_tabs:
            new_len = max(0, len(ws.expandtabs(4)) + base_delta)
            out.append((" " * new_len) + ln.lstrip())
        else:
            out.append(ln)
    return "\n".join(out)




# --------------------------------------------------------------------------
# 4. Session-Persisted Plan & TODO Ledger LRU Cache (`flow17_session_ledger`)
# --------------------------------------------------------------------------
_FF17_SESSION_LOCK = threading.Lock()
_FF17_SESSION_CACHE = _ff17_collections.OrderedDict()
_COMPACT_MARKER_RE = re.compile(
    r"(?i)\b(?:resuming\s+from\s+a?\s*compaction|context\s+compact(?:ed|ion)|"
    r"conversation\s+summary|truncated\s+conversation|earlier\s+messages\s+omitted)\b"
)


def _ff17_session_id_from_messages(messages):
    """Extract an explicit session_id/conversation_id if present, or derive a
    fingerprint when compaction/truncation markers are present."""
    if not messages:
        return None
    cfg_sid = CONFIG.get("flow17_session_id")
    if cfg_sid:
        return str(cfg_sid)
    first_user = None
    has_compact_marker = False
    for m in messages:
        if not isinstance(m, dict):
            continue
        for k in ("session_id", "conversation_id", "thread_id"):
            if m.get(k):
                return f"sid:{m[k]}"
        txt = T._stringify_content(m.get("content", ""))
        if first_user is None and m.get("role") == "user" and txt.strip():
            first_user = txt.strip()
        if _COMPACT_MARKER_RE.search(txt):
            has_compact_marker = True
    if has_compact_marker and first_user:
        cleaned = re.sub(r"(?i)#\s*resuming\s+from\s+a?\s*compaction\s*", "", first_user).strip()
        h = _ff17_hashlib.sha1(cleaned[:400].encode("utf-8", "ignore")).hexdigest()[:16]
        return f"compact:{h}"
    return None


def clear_session_ledger_cache():
    """Clear the in-memory session ledger cache (useful for tests or resets)."""
    with _FF17_SESSION_LOCK:
        _FF17_SESSION_CACHE.clear()




def _ff16_read_plan_bodies(messages):
    bodies = _ff16_read_plan_bodies_s1(messages)
    if not _ff17_c("flow17_session_ledger"):
        return bodies
    sid = _ff17_session_id_from_messages(messages)
    if not sid:
        return bodies
    max_entries = max(8, int(_ff17_c("flow17_session_cache_max", 128) or 128))
    with _FF17_SESSION_LOCK:
        entry = _FF17_SESSION_CACHE.get(sid)
        if entry is None:
            entry = {"plan_bodies": [], "edited_files": [], "read_files": []}
            _FF17_SESSION_CACHE[sid] = entry
        _FF17_SESSION_CACHE.move_to_end(sid)
        existing_paths = {_norm(p).lower() for p, _b in bodies}
        merged = list(bodies)
        for p, b in entry.get("plan_bodies", ()):
            if _norm(p).lower() not in existing_paths:
                merged.append((p, b))
                existing_paths.add(_norm(p).lower())
        entry["plan_bodies"] = list(merged)
        while len(_FF17_SESSION_CACHE) > max_entries:
            _FF17_SESSION_CACHE.popitem(last=False)
    return merged


def _ff17_sync_session_ev(messages, ev=None):
    """Merge and update `edited_files` and `read_files` in `_FF17_SESSION_CACHE`
    for `messages` when a session ID / compaction marker is active."""
    if ev is None:
        ev = _answered_calls(messages or [])
    cur_edited = sorted({_norm(_call_path(e["args"])) for e in ev
                         if e.get("kind") == "act" and _act_ok(e) and _call_path(e.get("args") or {})})
    cur_read = sorted({_norm(_call_path(e["args"])) for e in ev
                       if e.get("kind") == "read" and _read_ok(e) and _call_path(e.get("args") or {})})
    if not _ff17_c("flow17_session_ledger"):
        return cur_edited, cur_read
    sid = _ff17_session_id_from_messages(messages)
    if not sid:
        return cur_edited, cur_read

    max_entries = max(8, int(_ff17_c("flow17_session_cache_max", 128) or 128))
    with _FF17_SESSION_LOCK:
        entry = _FF17_SESSION_CACHE.get(sid)
        if entry is None:
            entry = {"plan_bodies": [], "edited_files": [], "read_files": []}
            _FF17_SESSION_CACHE[sid] = entry
        _FF17_SESSION_CACHE.move_to_end(sid)
        for p in cur_edited:
            if not any(_ff12_same_file(p, x) for x in entry["edited_files"]):
                entry["edited_files"].append(p)
        for p in cur_read:
            if not any(_ff12_same_file(p, x) for x in entry["read_files"]):
                entry["read_files"].append(p)
        while len(_FF17_SESSION_CACHE) > max_entries:
            _FF17_SESSION_CACHE.popitem(last=False)
        return list(entry["edited_files"]), list(entry["read_files"])






def _ff16_todo_ledger_s2(ctx, messages) -> str:
    if not _ff17_c("flow17_session_ledger"):
        return _ff16_todo_ledger_s1(ctx, messages)
    sid = _ff17_session_id_from_messages(messages)
    if not sid:
        return _ff16_todo_ledger_s1(ctx, messages)
    if not _ff16_c("flow16_plan_todo") or ctx is None:
        return ""
    pat = getattr(ctx, "pattern", None)
    if pat not in ("CHANGE", "CREATE", "DEBUG", "GENERIC_REVIEW", "FILE_REVIEW", "EXPLAIN"):
        return ""
    ev = _answered_calls(messages)
    edited_files, read_files = _ff17_sync_session_ev(messages, ev)
    tested = _tests_ran_after_act(ev) if edited_files else bool(_test_runs(ev))
    steps = _ff16_extract_todo_items(ctx, messages)
    targets = _ff16_planned_code_targets(ctx, ev, messages)
    if not steps and len(targets) < 2:
        return ""
    lines = ["[Plan & TODO Progress]"]
    if targets:
        t_parts = []
        for t in targets[:8]:
            done_edit = any(_ff12_same_file(t, p) for p in edited_files)
            done_read = any(_ff12_same_file(t, p) for p in read_files)
            if pat in ("CHANGE", "CREATE") or (pat == "DEBUG" and getattr(ctx, "fix", False)):
                state = "[x] edited" if done_edit else ("[r] read, pending edit" if done_read else "[ ] unread")
            else:
                state = "[x] read" if done_read else "[ ] unread"
            t_parts.append(f"{t} ({state})")
        lines.append("Target files: " + ", ".join(t_parts))
    if steps:
        s_parts = []
        for idx, st in enumerate(steps[:8], 1):
            st_files = [f for f in T._files_in(st) if f.lower() not in T._NON_FILE_NAMES]
            if st_files:
                if pat in ("CHANGE", "CREATE"):
                    done = all(any(_ff12_same_file(sf, ef) for ef in edited_files)
                               or (_is_test_path(sf) and tested)
                               for sf in st_files)
                else:
                    done = all(any(_ff12_same_file(sf, rf) for rf in read_files)
                               for sf in st_files)
            elif re.search(r"\b(?:test|unittest|pytest|verify|kiem\s+tra)\b", st, re.I):
                done = tested
            else:
                done = False
            mark = "[x]" if done else "[ ]"
            s_parts.append(f"{idx}. [x] {st}" if done else f"{idx}. [ ] {st}")
        lines.append("Steps: " + " | ".join(s_parts))
    return "\n".join(lines)




def _plan_change(ctx, ev, tm, pat="CHANGE", total=5):
    if _ff17_c("flow17_session_ledger"):
        msgs = getattr(_ff8_tls, "messages", None)
        sid = _ff17_session_id_from_messages(msgs) if msgs else None
        if sid:
            edited_files, read_files = _ff17_sync_session_ev(msgs, ev)
            present_reads = {_norm(_call_path(e["args"])).lower() for e in ev
                             if e.get("kind") == "read" and _call_path(e.get("args") or {})}
            synthetic_ev = list(ev)
            for rf in read_files:
                if _norm(rf).lower() not in present_reads:
                    # Only synthesize read markers for plan docs or already-edited files
                    # so compacted turns don't re-read PLAN.md or already-completed files
                    if _ff16_is_plan_doc(rf) or any(_ff12_same_file(rf, ef) for ef in edited_files):
                        synthetic_ev.append({
                            "kind": "read", "name": "read",
                            "args": {"filePath": rf}, "result": "# cached from session\n"
                        })
            ev = synthetic_ev
    return _plan_change_s3(ctx, ev, tm, pat=pat, total=total)


def _ff17_large_file_structure_index(messages):
    """Build a compact structural index (sections, classes, multi-layer definitions
    with exact 1-based line numbers) when a file read in `messages` exceeds 100 KB
    so prompt truncation never hides earlier sections of a large file."""
    ev = _answered_calls(messages or [])
    total_read_chars = sum(len(e.get("result") or "") for e in ev
                           if e.get("kind") == "read" and _read_ok(e))
    if total_read_chars < 100000:
        return ""
    views = _ff8_file_views(ev)
    blocks = []
    for v in views:
        text = v.get("text") or ""
        if len(text) < 80000:
            continue
        clean_src, line_nums = _ff17_strip_numbered_lines(text)
        src_lines = clean_src.split("\n")
        sections, classes = [], []
        defs = _ff17_collections.defaultdict(list)
        for idx, raw_ln in enumerate(src_lines):
            lno = line_nums[idx] if idx < len(line_nums) else (idx + 1)
            s = raw_ln.rstrip()
            if re.match(r"^#\s*(?:Section:|====\s*\w+)", s):
                sections.append(f"  {lno}: {s}")
            elif re.match(r"^class\s+\w+\b.*:", s):
                classes.append(f"  {lno}: {s}")
            else:
                m_def = re.match(r"^def\s+(\w+)\s*\(", s)
                if m_def:
                    defs[m_def.group(1)].append((lno, s))
        multi_defs = [
            f"  {name} (defined {len(locs)}x at lines "
            + ", ".join(str(ln) for ln, _ in locs)
            + f"): {locs[-1][0]}: `{locs[-1][1]}`"
            for name, locs in defs.items() if len(locs) > 1
        ]
        parts = [f"[Structure index for {v.get('path')} ({len(src_lines)} lines total -- earlier read chunks may be truncated above; cite exact line numbers below):]"]
        if sections:
            parts.append("Sections / Patch Layers:\n" + "\n".join(sections[:45]))
        if classes:
            parts.append("Classes:\n" + "\n".join(classes[:25]))
        if multi_defs:
            parts.append("Functions redefined/wrapped across layers:\n" + "\n".join(multi_defs[:45]))
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------
# 5. Multi-Phase Plan & TODO Completion Gate + Large-File Act Context Pack
#    + Line-Aware Read Reconstruction & Smart Edit Gate Repairs
# --------------------------------------------------------------------------
_FF17_PURE_TEST_TODO_RE = re.compile(
    r"\b(?:chay|run|execute|perform|verify|kiem\s+tra|nghiem\s+thu)\b.*"
    r"\b(?:unit\s+tests?|tests?|pytest|unittest|kiem\s+thu)\b"
    r"|\b(?:unit\s+tests?|pytest|unittest)\b.*\b(?:chay|run|execute|nghiem\s+thu)\b",
    re.IGNORECASE,
)
_FF17_WRITE_VERB_TODO_RE = re.compile(
    r"\b(?:tao|viet|them|sua|cap\s+nhat|trien\s+khai|tich\s+hop|"
    r"create|write|add|edit|modify|update|implement|integrate|refactor)\b",
    re.IGNORECASE,
)
_FF17_STOP_IDENTS = {
    "str", "int", "float", "bool", "list", "dict", "set", "tuple", "bytes",
    "None", "True", "False", "self", "cls", "elif", "else", "return", "pass",
    "GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH", "HTTP",
    "json", "time", "sys", "os", "re", "math", "uuid", "threading", "socket",
    "unittest", "mock", "pytest", "tests",
}


def _ff17_find_key(mapping: dict, path: str):
    """Find an existing key in `mapping` that refers to the same file as `path`."""
    if not path:
        return None
    if path in mapping:
        return path
    for k in mapping:
        if _ff12_same_file(path, k) or T._path_seen(path, [k]):
            return k
    return None


def _ff17_merge_read_chunk(cur_text, raw_result, args, edited_since_read=False) -> str:
    """Line-aware merge of a `read` tool output into `cur_text`.
    Prevents bounded window re-reads (e.g. `offset=1, limit=200` or `offset=120, limit=40`)
    from either wiping out the rest of a 20K-line file or appending duplicate lines at the
    tail of the file."""
    trunc_rx = _ff5_trunc_re()
    raw_no_trunc = trunc_rx.sub("", raw_result or "").rstrip() if trunc_rx.search(raw_result or "") else (raw_result or "")
    clean_body = _clean_file_text(raw_no_trunc)
    raw_env_lines = [ln for ln in _strip_read_envelope(raw_no_trunc).split("\n") if ln.strip() and not _READ_HEADER_RE.match(ln) and not _READ_FOOTER_RE.match(ln)]
    has_explicit_nums = bool(raw_env_lines) and (
        T._already_numbered(raw_env_lines)
        or all(re.match(r"^\s*\d+\s*(?:\||:|\t|\u2192)\s", ln) for ln in raw_env_lines)
    )
    clean_src, line_nums = _ff17_strip_numbered_lines(raw_no_trunc)
    if has_explicit_nums:
        clean_body = clean_src
    if not cur_text:
        return clean_body

    new_lines = clean_body.split("\n")
    cur_lines = cur_text.split("\n")
    off = _ff4_off(args) if "_ff4_off" in globals() else 0

    # Determine 1-based start line of this chunk
    if has_explicit_nums:
        start_ln = line_nums[0]
    elif off > 0:
        start_ln = off
    else:
        start_ln = 1

    # Case 1: Forward continuation at or past the end of `cur_lines`
    if off > 1 and (start_ln > len(cur_lines) or (not has_explicit_nums and off >= len(cur_lines))):
        return cur_text + "\n" + clean_body

    # Case 2: If the file was already edited in-place and this is a small window read
    # whose content is already present (or smaller than half the file), keep `cur_text` intact
    if edited_since_read and len(new_lines) < len(cur_lines) * 0.6:
        if clean_body in cur_text:
            return cur_text
        if has_explicit_nums and 1 <= start_ln <= len(cur_lines):
            end_idx = start_ln - 1 + len(new_lines)
            if end_idx <= len(cur_lines):
                merged = list(cur_lines)
                merged[start_ln - 1:end_idx] = new_lines
                return "\n".join(merged)
        return cur_text

    # Case 3: Splicing numbered or offset-based window into a larger `cur_lines`
    if 1 <= start_ln <= len(cur_lines):
        end_idx = start_ln - 1 + len(new_lines)
        if end_idx < len(cur_lines):
            merged = list(cur_lines)
            merged[start_ln - 1:end_idx] = new_lines
            return "\n".join(merged)
        elif start_ln > 1:
            merged = list(cur_lines[:start_ln - 1]) + new_lines
            return "\n".join(merged)

    return clean_body


def _ff9_read_texts(messages):
    """Line-aware file state tracker across chunked reads, window re-reads,
    surgical edits (`oldString -> newString`), and full-file `write` calls."""
    texts = {}
    edited_flags = set()
    ev = _answered_calls(messages) if isinstance(messages, list) and (
        not messages or "kind" not in (messages[0] if isinstance(messages[0], dict) else {})
    ) else (messages or [])
    for e in ev:
        p = _call_path(e.get("args") or {})
        if not p:
            continue
        k = _ff17_find_key(texts, p) or p
        if e.get("kind") == "act":
            if _act_ok(e):
                args_a = e.get("args") or {}
                okey_a = next((ok_k for ok_k in _OLD_KEYS
                               if isinstance(args_a.get(ok_k), str) and args_a[ok_k]), None)
                nkey_a = next((nk_k for nk_k in _NEW_KEYS
                               if isinstance(args_a.get(nk_k), str)), None)
                ckey_a = next((ck_k for ck_k in _CONTENT_KEYS
                               if isinstance(args_a.get(ck_k), str)), None)
                if not okey_a and ckey_a is not None:
                    # Full-file write tool call: we know the exact new file content
                    texts[k] = args_a[ckey_a].replace("\r\n", "\n")
                    edited_flags.add(k)
                    continue
                if k in texts and okey_a and nkey_a is not None:
                    old_s = _strip_linenos(args_a[okey_a].replace("\r\n", "\n"))
                    new_s = _strip_linenos(args_a[nkey_a].replace("\r\n", "\n"))
                    cur_t = texts[k].replace("\r\n", "\n")
                    if old_s in cur_t:
                        texts[k] = cur_t.replace(old_s, new_s, 1)
                        edited_flags.add(k)
                        continue
                    if old_s.strip("\n") and old_s.strip("\n") in cur_t:
                        texts[k] = cur_t.replace(old_s.strip("\n"), new_s.strip("\n"), 1)
                        edited_flags.add(k)
                        continue
                    actual_u, n_u = _ff14_find_unique(old_s, cur_t)
                    if actual_u is not None and n_u == 1:
                        reindented = _ff14_reindent(new_s, old_s, actual_u)
                        texts[k] = cur_t.replace(actual_u, reindented, 1)
                        edited_flags.add(k)
                        continue
                    del texts[k]
            continue
        if e.get("kind") != "read" or not _read_ok(e):
            continue
        texts[k] = _ff17_merge_read_chunk(
            texts.get(k), e.get("result") or "", e.get("args") or {},
            edited_since_read=(k in edited_flags),
        )
    return texts


def _read_texts(ev):
    return _ff9_read_texts(ev)




def _ff9_stale_edits(ev):
    """Do not force a full re-read of a file during multi-step `act` (before tests run)
    or on large (>50 KB) files when `_ff9_read_texts` already tracks the post-edit text."""
    raw_stale = _ff9_stale_edits_s1(ev)
    if not raw_stale:
        return raw_stale
    msgs = getattr(_ff8_tls, "messages", None)
    tracked = _ff9_read_texts(msgs if msgs is not None else ev)
    tests_ran = _tests_ran_after_act(ev)
    out = []
    for p in raw_stale:
        tk = _ff17_find_key(tracked, p)
        if tk is not None:
            if not tests_ran or len(tracked.get(tk) or "") > 50000:
                continue
        out.append(p)
    return out


def _ff17_repair_pure_anchor_edit(old: str, new: str, hay: str):
    """When an edit is a pure append (`new` starts with `old` + newline) or pure prepend
    (`new` ends with newline + `old`), and `old` consists of 1-5 top-level assignment lines
    (`<ident> = ...`) whose LHS identifiers all exist within a single 35-line region of `hay`,
    anchor the insertion on the actual line in `hay` so approximate literal values in `old`
    (like `_cookie_cache = ("", None, 0.0)` vs `0`) do not reject a valid insertion."""
    old_clean = old.strip("\n")
    new_clean = new.strip("\n")
    if not old_clean or not new_clean or old_clean == new_clean:
        return None
    is_append = new_clean.startswith(old_clean + "\n")
    is_prepend = (not is_append) and new_clean.endswith("\n" + old_clean)
    if not (is_append or is_prepend):
        return None

    old_lines = [ln.strip() for ln in old_clean.split("\n") if ln.strip() and not ln.strip().startswith("#")]
    if not (1 <= len(old_lines) <= 5):
        return None
    lhs_names = []
    for ln in old_lines:
        m = re.match(r"^([A-Za-z_]\w*)\s*=", ln)
        if not m:
            return None
        lhs_names.append(m.group(1))

    hay_lines = hay.split("\n")
    lhs_positions = {}
    for idx, hln in enumerate(hay_lines):
        m = re.match(r"^([A-Za-z_]\w*)\s*=", hln)
        if m and m.group(1) in lhs_names:
            lhs_positions.setdefault(m.group(1), []).append(idx)

    if any(name not in lhs_positions for name in lhs_names):
        return None
    # Every LHS identifier must have a unique top-level assignment in a compact cluster
    chosen = []
    for name in lhs_names:
        locs = lhs_positions[name]
        if len(locs) != 1:
            return None
        chosen.append(locs[0])
    if max(chosen) - min(chosen) > 35:
        return None

    if is_append:
        suffix = new_clean[len(old_clean):]
        anchor_line = hay_lines[max(chosen)]
        if hay.count(anchor_line) == 1:
            return anchor_line, anchor_line + suffix
    else:
        prefix = new_clean[:-len(old_clean)]
        anchor_line = hay_lines[min(chosen)]
        if hay.count(anchor_line) == 1:
            return anchor_line, prefix + anchor_line
    return None


def _ff17_find_defused_unique(old: str, hay: str):
    """If `defuse_extension_words` rewrote words (e.g. 'Workspace' -> 'Project') in the
    prompt's `[Tool result for read]`, match `defuse_extension_words(old)` against
    `defuse_extension_words(hay)` and return the original undefused span from `hay`."""
    try:
        defused_old = T.defuse_extension_words(old)
        defused_hay = T.defuse_extension_words(hay)
    except Exception:
        return None
    if defused_old == old and defused_hay == hay:
        return None
    ol = [_ff14_nline(x) for x in defused_old.split("\n")]
    while ol and not ol[0]:
        ol.pop(0)
    while ol and not ol[-1]:
        ol.pop()
    if not ol:
        return None
    raw_hay = hay.split("\n")
    def_lines = [_ff14_nline(x) for x in defused_hay.split("\n")]
    if len(raw_hay) != len(def_lines):
        return None
    n = len(ol)
    hits = [i for i in range(len(def_lines) - n + 1) if def_lines[i:i + n] == ol]
    if len(hits) == 1:
        i = hits[0]
        return "\n".join(raw_hay[i:i + n])
    return None


def _ff17_static_python_lint(path: str, old_v: str, new_v: str, hay: str) -> list:
    """Analyze new Python code in an edit/write call for common LLM bugs:
    1. UnboundLocalError: reading a variable before its first assignment in the function's own scope
       (especially module-level state modified without declaring `global <var>`).
    2. Re-entrant Lock Deadlock: calling another function while holding a non-reentrant `threading.Lock()`.
    Returns a list of error strings to reject the call at gate."""
    if not (path or "").lower().endswith(".py") or not new_v:
        return []
    problems = []

    import textwrap as _textwrap
    import collections as _collections

    try:
        new_tree = _ff5_ast.parse(_textwrap.dedent(new_v))
    except Exception:
        if hay and old_v and old_v in hay:
            try:
                new_tree = _ff5_ast.parse(hay.replace(old_v, new_v, 1))
            except Exception:
                return []
        else:
            return []

    # 1. Module globals collection
    module_globals = set()
    if hay:
        for m in re.finditer(r"(?m)^([A-Za-z_]\w*)\s*=", hay):
            g_name = m.group(1)
            if not g_name.startswith("__"):
                module_globals.add(g_name)
    for node in getattr(new_tree, "body", []):
        if isinstance(node, _ff5_ast.Assign):
            for t in node.targets:
                if isinstance(t, _ff5_ast.Name):
                    module_globals.add(t.id)

    class _ScopeVisitor(_ff5_ast.NodeVisitor):
        def __init__(self):
            self.params = set()
            self.globals_declared = set()
            self.nonlocals_declared = set()
            self.assigned_names = set()
            self.read_before_assign = set()

        # Do NOT enter nested function definitions, lambdas, comprehensions, or class definitions
        def visit_FunctionDef(self, node): pass
        def visit_AsyncFunctionDef(self, node): pass
        def visit_Lambda(self, node): pass
        def visit_ClassDef(self, node): pass

        def visit_ListComp(self, node):
            for gen in node.generators: self.visit(gen.iter)
        def visit_SetComp(self, node):
            for gen in node.generators: self.visit(gen.iter)
        def visit_DictComp(self, node):
            for gen in node.generators: self.visit(gen.iter)
        def visit_GeneratorExp(self, node):
            for gen in node.generators: self.visit(gen.iter)

        def visit_Global(self, node):
            self.globals_declared.update(node.names)
        def visit_Nonlocal(self, node):
            self.nonlocals_declared.update(node.names)

        def visit_Assign(self, node):
            self.visit(node.value)
            for t in node.targets: self._visit_target(t)

        def visit_AugAssign(self, node):
            if isinstance(node.target, _ff5_ast.Name):
                name = node.target.id
                if name not in self.assigned_names:
                    self.read_before_assign.add(name)
                self.assigned_names.add(name)
            else:
                self.visit(node.target)
            self.visit(node.value)

        def visit_AnnAssign(self, node):
            if node.value: self.visit(node.value)
            self._visit_target(node.target)

        def visit_For(self, node):
            self.visit(node.iter)
            self._visit_target(node.target)
            for stmt in node.body: self.visit(stmt)
            for stmt in node.orelse: self.visit(stmt)

        def visit_AsyncFor(self, node):
            self.visit(node.iter)
            self._visit_target(node.target)
            for stmt in node.body: self.visit(stmt)
            for stmt in node.orelse: self.visit(stmt)

        def visit_With(self, node):
            for item in node.items:
                self.visit(item.context_expr)
                if item.optional_vars: self._visit_target(item.optional_vars)
            for stmt in node.body: self.visit(stmt)

        def visit_AsyncWith(self, node):
            for item in node.items:
                self.visit(item.context_expr)
                if item.optional_vars: self._visit_target(item.optional_vars)
            for stmt in node.body: self.visit(stmt)

        def visit_ExceptHandler(self, node):
            if node.type: self.visit(node.type)
            if node.name: self.assigned_names.add(node.name)
            for stmt in node.body: self.visit(stmt)

        def visit_NamedExpr(self, node):
            self.visit(node.value)
            self._visit_target(node.target)

        def visit_Name(self, node):
            if isinstance(node.ctx, _ff5_ast.Load):
                if node.id not in self.assigned_names:
                    self.read_before_assign.add(node.id)
            elif isinstance(node.ctx, (_ff5_ast.Store, _ff5_ast.Del)):
                self.assigned_names.add(node.id)

        def _visit_target(self, target):
            if isinstance(target, _ff5_ast.Name):
                self.assigned_names.add(target.id)
            elif isinstance(target, (_ff5_ast.Tuple, _ff5_ast.List)):
                for elt in target.elts: self._visit_target(elt)
            elif isinstance(target, _ff5_ast.Starred):
                self._visit_target(target.value)
            else:
                self.visit(target)

    for node in _ff5_ast.walk(new_tree):
        if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
            fn_name = node.name
            if new_v and fn_name not in new_v:
                continue
            params = {a.arg for a in getattr(node.args, "posonlyargs", [])}
            params.update(a.arg for a in node.args.args)
            if node.args.vararg:
                params.add(node.args.vararg.arg)
            if node.args.kwarg:
                params.add(node.args.kwarg.arg)
            for a in node.args.kwonlyargs:
                params.add(a.arg)

            vis = _ScopeVisitor()
            vis.params = set(params)
            vis.assigned_names = set(params)
            for stmt in node.body:
                vis.visit(stmt)

            for v in sorted(vis.assigned_names):
                if v in vis.params or v in vis.globals_declared or v in vis.nonlocals_declared:
                    continue
                if v in vis.read_before_assign and (v in module_globals or v.startswith("_")):
                    problems.append(
                        f"{path}: Function '{fn_name}' assigns to module-level variable '{v}' "
                        f"without declaring 'global {v}', which will raise UnboundLocalError: "
                        f"'{v}' is read before its first assignment. Add 'global {v}' at the top of '{fn_name}'."
                    )

    # 2. Re-entrant Lock Deadlock check
    combined = (hay or "") + "\n" + new_v
    non_reentrant_locks = set()
    for m in re.finditer(r"(?m)^([A-Za-z_]\w*)\s*=\s*(?:threading\.)?Lock\(\)", combined):
        non_reentrant_locks.add(m.group(1))

    if non_reentrant_locks:
        func_calls_under_lock = _collections.defaultdict(set)
        for node in _ff5_ast.walk(new_tree):
            if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
                if new_v and node.name not in new_v:
                    continue
                for with_node in _ff5_ast.walk(node):
                    if isinstance(with_node, _ff5_ast.With):
                        for item in with_node.items:
                            lock_id = getattr(item.context_expr, "id", None)
                            if lock_id in non_reentrant_locks:
                                for inner in _ff5_ast.walk(with_node):
                                    if isinstance(inner, _ff5_ast.Call):
                                        if isinstance(inner.func, _ff5_ast.Name):
                                            func_calls_under_lock[node.name].add((inner.func.id, lock_id))

        for caller, called_pairs in func_calls_under_lock.items():
            for callee, lk in called_pairs:
                callee_body = _ff17_extract_func_body(callee, combined)
                if callee_body and (f"with {lk}:" in callee_body or f"{lk}.acquire()" in callee_body):
                    problems.append(
                        f"{path}: Potential deadlock detected in '{caller}': it holds '{lk}' "
                        f"and calls '{callee}', which also acquires non-reentrant lock '{lk}'. "
                        f"Change '{lk} = threading.Lock()' to 'threading.RLock()' or avoid nested lock acquisition."
                    )

    return problems




def _base_gate_act_calls(calls, messages):
    """Enhanced edit gate:
    1. Reject no-op edits (`oldString == newString`) immediately with guidance.
    2. Reject edits that introduce duplicate method definitions (`def <m>(self...)`)
       when the method already exists in the class, showing the existing method lines.
    3. Repair `defuse_extension_words` substitutions and pure-anchor append/prepend edits.
    4. Reject edits that introduce missing global (UnboundLocalError) or threading.Lock deadlock.
    5. When `oldString` does not match, include a verbatim multi-line snippet around the
       closest matching line in `hay` (or around the target section/anchor when `oldString`
       invented a non-existent `def <fn>(`)."""
    texts = _ff9_read_texts(messages)
    repaired_calls = []
    noop_problems = []
    dup_method_problems = []
    static_lint_problems = []
    nonexistent_fn_map = {}
    for tc in calls:
        fn = (tc or {}).get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            repaired_calls.append(tc)
            continue
        args = T._tool_arguments_dict(fn)
        path = _call_path(args)
        okey = next((k for k in _OLD_KEYS if isinstance(args.get(k), str) and args[k]), None)
        nkey = next((k for k in _NEW_KEYS if isinstance(args.get(k), str)), None)
        wkey = next((k for k in ("content", "contents", "text") if isinstance(args.get(k), str)), None) if not nkey else None

        if _ff17_c("flow17_static_pre_gate", True) and (path or "").lower().endswith(".py"):
            new_code = args.get(nkey) if nkey is not None else args.get(wkey)
            if isinstance(new_code, str) and new_code.strip():
                tk = _ff17_find_key(texts, path) if path else None
                src_t = texts.get(tk) if tk else None
                hay_t = src_t.replace("\r\n", "\n") if src_t is not None else ""
                lint_errs = _ff17_static_python_lint(path, args.get(okey, ""), new_code, hay_t)
                if lint_errs:
                    for le in lint_errs:
                        if le not in static_lint_problems:
                            static_lint_problems.append(le)
                    continue

        if okey and nkey is not None:
            old_v = _strip_linenos(args[okey].replace("\r\n", "\n"))
            new_v = _strip_linenos(args[nkey].replace("\r\n", "\n"))
            if old_v.strip() == new_v.strip():
                noop_problems.append(
                    f"{path}: newString is identical to oldString (no-op edit) -- "
                    "this change is already applied; emit an edit/write tool_call for the "
                    "NEXT unimplemented requirement in the plan instead"
                )
                continue
            tk = _ff17_find_key(texts, path) if path else None
            src_t = texts.get(tk) if tk else None
            if src_t is not None:
                hay = src_t.replace("\r\n", "\n")
                # Check for duplicate class method definitions (e.g. prepending a second
                # `def do_GET(self):` or `def do_OPTIONS(self):` onto `GeminiHandler`)
                if (path or "").lower().endswith(".py") and not re.search(r"(?m)^[ \t]*class\s+\w+", new_v):
                    dup_found = False
                    for m_meth in re.finditer(r"(?m)^([ \t]*)def\s+([A-Za-z_]\w*)\s*\(\s*self\b", new_v):
                        m_name = m_meth.group(2)
                        if m_name.startswith("__"):
                            continue
                        rx_m = re.compile(rf"(?m)^[ \t]*def\s+{re.escape(m_name)}\s*\(\s*self\b")
                        c_old = len(rx_m.findall(old_v))
                        c_new = len(rx_m.findall(new_v))
                        c_hay = len(rx_m.findall(hay))
                        if c_new > c_old and c_hay > c_old:
                            hay_lines = hay.split("\n")
                            hit_idx = next((i for i, hl in enumerate(hay_lines) if rx_m.match(hl)), None)
                            snippet_msg = ""
                            if hit_idx is not None:
                                s_i = max(0, hit_idx - 2)
                                e_i = min(len(hay_lines), hit_idx + 25)
                                snippet = "\n".join(hay_lines[s_i:e_i])
                                snippet_msg = (
                                    f" Here are the exact verbatim lines {s_i + 1}-{e_i} of `def {m_name}` "
                                    f"from {path} (copy oldString verbatim from this block):\n{snippet}"
                                )
                            dup_method_problems.append(
                                f"{path}: newString defines a duplicate `def {m_name}(self...)`, "
                                f"which already exists in {path}. Edit the existing `def {m_name}(self...)` "
                                f"in place instead of adding a second definition.{snippet_msg}"
                            )
                            dup_found = True
                            break
                    if dup_found:
                        continue

                if old_v not in hay and old_v.strip("\n") not in hay:
                    actual_u, _nu = _ff14_find_unique(old_v, hay)
                    if actual_u is None:
                        def_match = _ff17_find_defused_unique(old_v, hay)
                        if def_match is not None:
                            args[okey] = def_match
                            tc = {**tc, "function": {**fn, "arguments": json.dumps(args, ensure_ascii=False)}}
                        else:
                            anchor_fix = _ff17_repair_pure_anchor_edit(old_v, new_v, hay)
                            if anchor_fix is not None:
                                args[okey], args[nkey] = anchor_fix
                                log(f"[fix17] edit gate: anchored pure insertion in {path}")
                                tc = {**tc, "function": {**fn, "arguments": json.dumps(args, ensure_ascii=False)}}
                            else:
                                first_old_ln = next((ln.strip() for ln in old_v.split("\n") if ln.strip()), "")
                                m_new_fn = re.match(r"^(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", first_old_ln)
                                if m_new_fn:
                                    fn_cand = m_new_fn.group(1)
                                    if not re.search(rf"(?m)^[ \t]*(?:async\s+)?def\s+{re.escape(fn_cand)}\s*\(", hay):
                                        nonexistent_fn_map[path] = fn_cand
        repaired_calls.append(tc)

    early_problems = noop_problems + dup_method_problems + static_lint_problems
    if early_problems and not repaired_calls:
        return [], early_problems

    out, problems = _base_gate_act_calls_s2(repaired_calls, messages)
    problems = early_problems + list(problems)
    if static_lint_problems:
        bad_lint_paths = {p.split(":")[0].strip() for p in static_lint_problems}
        out = [
            tc for tc in out
            if _call_path(T._tool_arguments_dict((tc or {}).get("function") or {})) not in bad_lint_paths
        ]
    if not problems:
        return out, problems

    # Enrich `oldString does not appear` problems with verbatim surrounding lines from `texts`
    # and remove failed tool calls from `out` so a rejected edit is never handed to the client.
    enriched = []
    bad_paths = set()
    for pr in problems:
        m_close = re.match(r"^(.*?): oldString does not appear in the file as read(?: \(closest real line: '(.*)'\))?", pr)
        if m_close:
            p_err = m_close.group(1)
            bad_paths.add(p_err)
            near_ln = m_close.group(2)
            tk = _ff17_find_key(texts, p_err)
            if tk and texts.get(tk):
                hay_lines = texts[tk].replace("\r\n", "\n").split("\n")
                missing_fn = nonexistent_fn_map.get(p_err)
                if missing_fn:
                    lc_body = _ff17_extract_func_body("load_cookie", texts[tk])
                    if lc_body:
                        snippet = "\n".join(lc_body.split("\n")[:15])
                        s_i, e_i = 0, len(snippet.split("\n"))
                    else:
                        anchor_idx = next(
                            (i for i, hl in enumerate(hay_lines) if re.match(r"^#\s*Section:\s*gemini\.py", hl.strip(), re.I)), 0
                        )
                        s_i = max(0, anchor_idx)
                        e_i = min(len(hay_lines), anchor_idx + 10)
                        snippet = "\n".join(hay_lines[s_i:e_i])
                    pr = (
                        f"{p_err}: `def {missing_fn}(...)` does not exist in {p_err} yet -- "
                        f"do NOT invent `def {missing_fn}(): pass` as oldString. Copy an existing "
                        f"block from {p_err} (such as lines {s_i + 1}-{e_i} below) as oldString and "
                        f"include both that block and your new `def {missing_fn}(...)` in newString:\n{snippet}"
                    )
                elif near_ln:
                    hit_idx = next((i for i, hl in enumerate(hay_lines) if hl.strip() == near_ln.strip()), None)
                    if hit_idx is not None:
                        s_i = max(0, hit_idx - 3)
                        e_i = min(len(hay_lines), hit_idx + 8)
                        snippet = "\n".join(hay_lines[s_i:e_i])
                        pr = (
                            f"{pr}. Here are the exact verbatim lines {s_i + 1}-{e_i} from {p_err} "
                            f"(copy oldString verbatim from this block):\n{snippet}"
                        )
        enriched.append(pr)
    if bad_paths:
        out = [
            tc for tc in out
            if _call_path(T._tool_arguments_dict((tc or {}).get("function") or {})) not in bad_paths
        ]
    return out, enriched


def _ff17_orig_read_corpus(ev) -> str:
    """Concatenate the initial read outputs in `ev` (before any edits)."""
    parts = []
    seen_acts = set()
    for e in ev or []:
        p = _call_path(e.get("args") or {})
        if e.get("kind") == "act" and _act_ok(e) and p:
            seen_acts.add(_norm(p).lower())
        elif e.get("kind") == "read" and _read_ok(e) and p:
            if not _ff16_is_plan_doc(p) and not _is_doc(p) and _norm(p).lower() not in seen_acts:
                parts.append(_clean_file_text(e.get("result") or ""))
    return "\n".join(parts)


def _ff17_added_act_corpus(ok_acts, include_tests=False) -> str:
    """Concatenate `newString` / `content` payloads from successful acts.
    By default (`include_tests=False`), excludes test files (`tests/...`) so symbol
    names merely referenced inside a unit test file never masquerade as implemented
    in the production source module."""
    parts = []
    for e in ok_acts or []:
        args_a = e.get("args") or {}
        p = _call_path(args_a) or ""
        if not include_tests and p and _is_test_path(p):
            continue
        for nk in list(_NEW_KEYS) + list(_CONTENT_KEYS):
            if isinstance(args_a.get(nk), str) and args_a[nk]:
                parts.append(args_a[nk])
    return "\n".join(parts)


def _ff17_token_defined_in_corpus(token: str, kind: str, corpus: str) -> bool:
    """Check whether `token` is actually defined as a function, variable, config key,
    or route in `corpus` (ignoring mere substring mentions inside comments or string literals)."""
    if not token or not corpus:
        return False
    if kind == "fn":
        return bool(re.search(rf"(?m)^[ \t]*(?:async\s+)?def\s+{re.escape(token)}\s*\(", corpus))
    if kind == "var":
        return bool(re.search(rf"(?m)^[ \t]*{re.escape(token)}\s*(?::[^=\n]+)?=", corpus))
    if kind == "cfg":
        return bool(re.search(r"[\x22\x27]" + re.escape(token) + r"[\x22\x27]\s*:", corpus))
    if kind == "route":
        return token in corpus
    return token in corpus


def _ff17_extract_req_tokens(text: str, orig_corpus: str, include_tests: bool = False) -> list:
    """Extract backticked function names, config keys, variables, and API routes from `text`
    that are NOT already defined in `orig_corpus` (i.e. new symbols to implement)."""
    if not text:
        return []
    out = []
    for sp in re.findall(r"`([^`\n]+)`", text):
        sp_s = sp.strip()
        token, tkind = None, None
        m_fn = re.match(r"^(?:def\s+)?([A-Za-z_]\w{2,})\s*\(", sp_s)
        m_route = re.search(r"(/v\d+(?:beta)?/[A-Za-z0-9_/-]+)", sp_s)
        m_cfg = re.match(r"^[\"']([A-Za-z_]\w{2,})[\"']\s*:", sp_s)
        m_var = re.match(r"^(_[A-Za-z_]\w{2,})(?:\s*=.*)?$", sp_s)
        if m_fn:
            token, tkind = m_fn.group(1), "fn"
        elif m_route:
            token, tkind = m_route.group(1), "route"
        elif m_cfg:
            token, tkind = m_cfg.group(1), "cfg"
        elif m_var:
            token, tkind = m_var.group(1), "var"
        elif include_tests:
            m_test = re.match(r"^(?:def\s+)?(test_[A-Za-z0-9_]+)(?:\(|$)", sp_s)
            if m_test and not sp_s.endswith((".py", ".js", ".ts", ".spec", ".test")) and "/" not in sp_s and "\\" not in sp_s:
                token, tkind = m_test.group(1), "fn"
        if token and token.lower() in ("attempt", "cooldown_until", "retry", "reason", "true", "false", "null", "none"):
            token = None
        if (token and token not in _FF17_STOP_IDENTS
                and (include_tests or not token.startswith("test_"))
                and not _ff17_token_defined_in_corpus(token, tkind, orig_corpus or "")
                and token not in out):
            out.append(token)
    return out


def _ff17_extract_existing_fn_targets(text: str, orig_corpus: str) -> list:
    """Extract backticked function/method names (`foo(...)`) from `text` that DO already
    exist as `def foo(` in `orig_corpus` (representing existing functions to update)."""
    if not text or not orig_corpus:
        return []
    out = []
    for sp in re.findall(r"`([^`\n]+)`", text):
        sp_s = sp.strip()
        m_fn = re.match(r"^(?:def\s+)?([A-Za-z_]\w{2,})\s*\(", sp_s)
        if m_fn:
            fn_name = m_fn.group(1)
            if (fn_name not in _FF17_STOP_IDENTS
                    and not fn_name.startswith("test_")
                    and _ff17_token_defined_in_corpus(fn_name, "fn", orig_corpus)
                    and fn_name not in out):
                out.append(fn_name)
    return out


def _ff17_extract_func_body(fn_name: str, src_text: str) -> str:
    """Extract the lines of `def <fn_name>(...):` from `src_text`."""
    if not fn_name or not src_text:
        return ""
    lines = src_text.replace("\r\n", "\n").split("\n")
    rx = re.compile(rf"^([ \t]*)(?:async\s+)?def\s+{re.escape(fn_name)}\s*\(")
    for idx, ln in enumerate(lines):
        m = rx.match(ln)
        if not m:
            continue
        base_ind = len(m.group(1))
        end_idx = len(lines)
        for k in range(idx + 1, min(len(lines), idx + 260)):
            s_ln = lines[k]
            if not s_ln.strip() or s_ln.lstrip().startswith("#"):
                continue
            cur_ind = len(s_ln) - len(s_ln.lstrip())
            if cur_ind <= base_ind and re.match(r"^(?:def |async def |class |@[A-Za-z_]|[A-Za-z_]\w*\s*=|if\s+__name__)", s_ln.lstrip()):
                end_idx = k
                break
        return "\n".join(lines[idx:end_idx]).strip()
    return ""


def _ff17_was_func_modified(fn_name: str, orig_corpus: str, tracked_src: str, ok_acts: list) -> bool:
    """Return True if the existing function `fn_name` in `orig_corpus` was modified in `tracked_src`
    or by any non-test edit in `ok_acts`. Returns False if unchanged or undetermined."""
    orig_body = _ff17_extract_func_body(fn_name, orig_corpus)
    cur_body = _ff17_extract_func_body(fn_name, tracked_src)
    if orig_body and cur_body:
        if orig_body != cur_body:
            return True
        return False
    rx_def = re.compile(rf"(?m)^[ \t]*(?:async\s+)?def\s+{re.escape(fn_name)}\s*\(")
    for e in ok_acts or []:
        args_a = e.get("args") or {}
        p = _call_path(args_a) or ""
        if p and _is_test_path(p):
            continue
        old_s = ""
        new_s = ""
        for ok_k in _OLD_KEYS:
            if isinstance(args_a.get(ok_k), str) and args_a[ok_k]:
                old_s = _strip_linenos(args_a[ok_k].replace("\r\n", "\n"))
                break
        for nk_k in _NEW_KEYS:
            if isinstance(args_a.get(nk_k), str):
                new_s = _strip_linenos(args_a[nk_k].replace("\r\n", "\n"))
                break
        if not old_s or old_s.strip() == new_s.strip():
            continue
        if rx_def.search(new_s):
            old_fn = _ff17_extract_func_body(fn_name, old_s)
            new_fn = _ff17_extract_func_body(fn_name, new_s)
            if new_fn and new_fn != old_fn:
                return True
        # If the edit replaced multiple lines inside the function body
        if orig_body and old_s in orig_body and len([l for l in old_s.strip().splitlines() if l.strip()]) >= 2:
            return True
    return False


def _ff17_is_pure_test_todo(content: str, edited_paths: list) -> bool:
    """True when a todo item is purely about running tests (and any test file it
    names has already been created/edited)."""
    folded = T._fold_diacritics(content or "").lower()
    if not _FF17_PURE_TEST_TODO_RE.search(folded):
        return False
    td_files = [
        f for f in T._files_in(content or "")
        if f.lower() not in T._NON_FILE_NAMES and not _ff16_is_plan_doc(f) and not _is_doc(f)
    ]
    if any(not any(_ff12_same_file(tf, ep) for ep in edited_paths) for tf in td_files):
        return False
    if _FF17_WRITE_VERB_TODO_RE.search(folded) and not td_files:
        if not any(_is_test_path(ep) for ep in edited_paths):
            return False
    return True


def _ff17_audit_plan_compliance(messages, ev) -> dict:
    """Audit the codebase against the plan document in messages:
    1. Missing symbols/functions/routes/test functions specified in backticks in the plan.
    2. Return tuple arity check (e.g. `get_active_cookie_credentials` returning 3-tuple).
    3. Retry loop refresh check (ensuring payload/headers are rebuilt inside retry loops).
    Returns {"compliant": bool, "violations": list[str]}."""
    violations = []
    plan_bodies = [b for _p, b in _ff16_read_plan_bodies(messages or [])]
    if not plan_bodies:
        return {"compliant": True, "violations": []}
    plan_text = "\n".join(plan_bodies)

    texts = _ff9_read_texts(messages or [])
    if not texts:
        for v in _ff8_file_views(ev or []):
            clean_s, _ = _ff17_strip_numbered_lines(v.get("text") or "")
            if clean_s:
                texts[v.get("path") or ""] = clean_s

    # 1. Missing test_* functions mentioned in the plan
    for m in re.finditer(r"`(test_[A-Za-z0-9_]+)`", plan_text):
        t_fn = m.group(1)
        found = False
        for p, txt in texts.items():
            if _is_test_path(p) and re.search(rf"(?m)^[ \t]*def\s+{re.escape(t_fn)}\b", txt):
                found = True
                break
        if not found:
            violations.append(f"Missing required unit test `{t_fn}` in test files.")

    # 2. Return Tuple Arity Check
    for m in re.finditer(r"`([A-Za-z_]\w+)\(\)`[^\n]*?(?:tr[\u1ea3a]\s*v[\u1ec1e]|return)[^\n]*?(?:tuple\s*)?`?\(([A-Za-z0-9_,\s]+)\)`?", plan_text, re.I):
        fn_name = m.group(1)
        elts_str = m.group(2)
        expected_elts = [x.strip() for x in elts_str.split(",") if x.strip()]
        expected_arity = len(expected_elts)
        if expected_arity >= 2:
            for p, txt in texts.items():
                if not _is_test_path(p) and not _is_doc(p) and f"def {fn_name}" in txt:
                    try:
                        tree = _ff5_ast.parse(txt)
                        for node in _ff5_ast.walk(tree):
                            if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)) and node.name == fn_name:
                                for ret in _ff5_ast.walk(node):
                                    if isinstance(ret, _ff5_ast.Return) and isinstance(ret.value, _ff5_ast.Tuple):
                                        actual_arity = len(ret.value.elts)
                                        if actual_arity != expected_arity:
                                            violations.append(
                                                f"Function `{fn_name}` in {p} returns a {actual_arity}-tuple, "
                                                f"but the plan specifies a {expected_arity}-tuple `({', '.join(expected_elts)})`."
                                            )
                                            break
                    except Exception:
                        pass

    # 3. Retry loop refresh check
    if re.search(r"\b(?:retry|attempt|failover|cooldown)\b", plan_text, re.I):
        for p, txt in texts.items():
            if not _is_test_path(p) and not _is_doc(p) and ("def generate(" in txt or "def generate_stream(" in txt):
                try:
                    tree = _ff5_ast.parse(txt)
                    for node in _ff5_ast.walk(tree):
                        if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)) and node.name in ("generate", "generate_stream"):
                            for sub in node.body:
                                if isinstance(sub, _ff5_ast.For) and isinstance(sub.target, _ff5_ast.Name) and "attempt" in sub.target.id:
                                    loop_calls = {
                                        c.func.id for c in _ff5_ast.walk(sub)
                                        if isinstance(c, _ff5_ast.Call) and isinstance(c.func, _ff5_ast.Name)
                                    }
                                    if "_build_headers" not in loop_calls or "_build_payload" not in loop_calls:
                                        missing_calls = [f for f in ("_build_headers", "_build_payload") if f not in loop_calls]
                                        violations.append(
                                            f"In `{node.name}`, request {' and '.join(missing_calls)} are built outside the retry loop (`for attempt in range(...)`). "
                                            f"Move `_build_headers` and `_build_payload` inside the retry loop so rotated credentials take effect on retry."
                                        )
                except Exception:
                    pass

    return {"compliant": len(violations) == 0, "violations": violations}


def _ff17_evaluate_plan_steps(ctx, ev, messages, pending_calls=None, step_name=None) -> list:
    """Evaluate all checklist steps from `_ff16_extract_todo_blocks(ctx, messages)` against
    the current conversation evidence (plus optional `pending_calls` about to be returned).
    Returns a list of step dicts with `index`, `title`, `full`, `done`, `is_test_run`,
    `is_test_step`, `missing_tokens`, `unmodified_fns`, and `uncreated_files`."""
    if ctx is None:
        return []
    step_blocks = _ff16_extract_todo_blocks(ctx, messages or [])
    if not step_blocks:
        return []
    ev_list = list(ev or [])
    if pending_calls:
        for tc in pending_calls:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name") or ""
            if tool_kind(name) == "act":
                ev_list.append({
                    "kind": "act",
                    "name": name,
                    "args": T._tool_arguments_dict(fn),
                    "result": "Edit applied successfully.",
                })
    ok_acts = [e for e in ev_list if e.get("kind") == "act" and _act_ok(e)]
    edited_files = sorted({_call_path(e.get("args") or {}) for e in ok_acts if _call_path(e.get("args") or {})})
    orig_corpus = _ff17_orig_read_corpus(ev_list)
    src_added_corpus = _ff17_added_act_corpus(ok_acts, include_tests=False)
    all_added_corpus = _ff17_added_act_corpus(ok_acts, include_tests=True)

    tracked_map = _ff9_read_texts(ev_list)
    tracked_src = "\n".join(
        txt for p, txt in tracked_map.items()
        if txt and not _ff16_is_plan_doc(p) and not _is_doc(p) and not _is_test_path(p)
    )
    tested = (
        True if step_name == "tests"
        else (_tests_ran_after_act(ev_list) if edited_files else bool(_test_runs(ev_list)))
    )

    out = []
    for idx, (st_title, st_full) in enumerate(step_blocks[:12], 1):
        body_without_heading = "\n".join(
            ln for ln in st_full.splitlines() if not ln.lstrip().startswith("#")
        )
        st_files = [
            f for f in T._files_in(T._strip_fences(st_full))
            if f.lower() not in T._NON_FILE_NAMES and not _ff16_is_plan_doc(f) and not _is_doc(f)
            and f.lower().endswith(_SRC_EXT)
        ]
        is_test_step = any(_is_test_path(sf) for sf in st_files) or bool(re.search(r"\b(?:tests/|test_)\w+", st_title))
        st_reqs = _ff17_extract_req_tokens(body_without_heading, orig_corpus, include_tests=is_test_step)
        st_existing_fns = _ff17_extract_existing_fn_targets(st_title + "\n" + body_without_heading, orig_corpus)
        is_pure_test = (not is_test_step and not st_reqs and not st_existing_fns
                        and (_ff17_is_pure_test_todo(st_title, edited_files)
                             or bool(re.search(r"\b(?:test|unittest|pytest|verify|kiem\s+tra|nghiem\s+thu)\b", st_title, re.I))))
        target_corpus = all_added_corpus if is_test_step else src_added_corpus
        test_disk_corpus = "\n".join(tracked_map.get(f, "") for f in st_files) if is_test_step else ""

        def _is_defined(tok):
            for corp in (target_corpus, test_disk_corpus, tracked_src):
                if not corp:
                    continue
                if any(_ff17_token_defined_in_corpus(tok, kd, corp) for kd in ("fn", "var", "cfg", "route")):
                    return True
            return False

        missing_reqs = [
            r for r in st_reqs
            if not _is_defined(r)
        ]
        unmodified_fns = (
            [] if is_test_step else [
                fn for fn in st_existing_fns
                if not _ff17_was_func_modified(fn, orig_corpus, tracked_src, ok_acts)
            ]
        )
        uncreated_st_files = [
            f for f in st_files
            if not ((tracked_map.get(f) or tracked_map.get(f.replace("\\", "/")) or tracked_map.get(f.replace("/", "\\")) or "").strip())
        ]
        if is_test_step:
            done = len(uncreated_st_files) == 0 and len(missing_reqs) == 0
        elif st_reqs or st_existing_fns:
            done = len(missing_reqs) == 0 and len(unmodified_fns) == 0
        elif st_files:
            done = len(uncreated_st_files) == 0
        elif is_pure_test:
            done = bool(tested)
        else:
            done = False
        out.append({
            "index": idx,
            "title": st_title,
            "full": st_full,
            "done": done,
            "is_test_run": is_pure_test,
            "is_test_step": is_test_step,
            "missing_tokens": missing_reqs,
            "unmodified_fns": unmodified_fns,
            "uncreated_files": uncreated_st_files,
        })

    # D3: Append verification commands and compliance audit steps to eval_steps
    v_cmds = _ff17_extract_verification_commands(messages, None, ev, require_existing=False) if "_ff17_extract_verification_commands" in globals() else []
    if v_cmds:
        n_steps = len(out)
        runs = _test_runs(ev_list)
        last_act_idx = max((i for i, e in enumerate(ev_list) if e.get("kind") == "act"), default=-1)
        runs_after_act = [r for r in runs if r[0] > last_act_idx]
        has_failing_test = any(test_outcome(r[2]) in ("fail", "broken") for r in runs_after_act)
        tests_completed = bool(runs_after_act and not has_failing_test) if last_act_idx >= 0 else bool(runs and not any(test_outcome(r[2]) in ("fail", "broken") for r in runs))

        # N+1: Chạy verification commands
        v_title = "Chạy verification commands" + (f" ({'; '.join(v_cmds[:2])})" if v_cmds else "")
        v_step_idx = n_steps + 1
        out.append({
            "index": v_step_idx,
            "title": v_title,
            "full": v_title,
            "done": tests_completed,
            "is_test_run": True,
            "is_test_step": True,
            "missing_tokens": [],
            "unmodified_fns": [],
            "uncreated_files": [],
        })

        # N+2: Đối chiếu plan (compliance audit)
        all_prev_done = bool(tests_completed and all(s["done"] for s in out[:n_steps]))
        audit_res = (
            _ff17_audit_plan_compliance(messages, ev_list)
            if (all_prev_done and "_ff17_audit_plan_compliance" in globals())
            else {"compliant": False, "violations": []}
        )
        audit_done = bool(all_prev_done and audit_res.get("compliant"))
        out.append({
            "index": n_steps + 2,
            "title": "Đối chiếu plan (compliance audit)",
            "full": "Đối chiếu plan (compliance audit)",
            "done": audit_done,
            "is_test_run": True,
            "is_test_step": False,
            "missing_tokens": [],
            "unmodified_fns": [],
            "uncreated_files": [],
        })

    return out


def _ff17_remaining_plan_or_todo_work(ctx, ev, messages) -> dict:
    """Inspect active `todowrite` state, read plan documents, and user prompt to
    determine if `step 3/5 act` still has unfinished implementation items, symbols,
    existing functions to update, or test files to create before transitioning to `step 4/5 tests`."""
    ok_acts = [e for e in (ev or []) if e.get("kind") == "act" and _act_ok(e)]
    edited_paths = [_call_path(e.get("args") or {}) for e in ok_acts if _call_path(e.get("args") or {})]
    src_edited_paths = [p for p in edited_paths if not _is_test_path(p)]
    orig_corpus = _ff17_orig_read_corpus(ev)
    src_added_corpus = _ff17_added_act_corpus(ok_acts, include_tests=False)
    all_added_corpus = _ff17_added_act_corpus(ok_acts, include_tests=True)
    listing = _listing_files(ev or [])

    # 1. Check required symbols/routes/config keys and existing function updates from read plan documents
    plan_bodies = "\n".join(b for _p, b in _ff16_read_plan_bodies(messages or []))
    plan_req_tokens = _ff17_extract_req_tokens(plan_bodies, orig_corpus) if plan_bodies else []
    missing_plan_tokens = [tok for tok in plan_req_tokens if tok not in src_added_corpus]

    remaining_todos = []
    eval_steps = _ff17_evaluate_plan_steps(ctx, ev, messages) if plan_bodies else []
    for st_info in eval_steps:
        if st_info["is_test_run"] or st_info["done"]:
            continue
        for tok in st_info["missing_tokens"] + st_info["unmodified_fns"]:
            if not st_info["is_test_step"] and tok not in missing_plan_tokens:
                missing_plan_tokens.append(tok)
        if st_info["title"] not in remaining_todos:
            remaining_todos.append(st_info["title"])

    # 2. Check active todowrite items
    todo_calls = [
        e for e in (ev or [])
        if _ff16_is_todo_tool(e.get("name", ""))
        and isinstance((e.get("args") or {}).get("todos"), list)
    ]
    if todo_calls and not eval_steps:
        latest_todos = todo_calls[-1]["args"]["todos"]
        done_count = 0
        open_items = []
        for td in latest_todos:
            if not isinstance(td, dict):
                continue
            status = str(td.get("status") or "").strip().lower()
            content = str(td.get("content") or td.get("title") or td.get("text") or "").strip()
            if not content or status in ("cancelled", "canceled"):
                continue
            if _ff17_is_pure_test_todo(content, edited_paths):
                continue
            if status in ("completed", "done") and not missing_plan_tokens:
                done_count += 1
            else:
                open_items.append((content, status))
        src_ok_acts = [e for e in ok_acts if not _is_test_path(_call_path(e.get("args") or {}) or "")]
        consumed_open = max(0, len(src_ok_acts) - done_count) if not missing_plan_tokens else 0
        for idx, (item_txt, item_status) in enumerate(open_items):
            td_files = [
                f for f in T._files_in(item_txt)
                if f.lower() not in T._NON_FILE_NAMES and not _ff16_is_plan_doc(f) and not _is_doc(f)
            ]
            uncreated_td = [
                tf for tf in td_files
                if not any(_ff12_same_file(tf, ep) for ep in edited_paths)
            ]
            td_reqs = _ff17_extract_req_tokens(item_txt, orig_corpus)
            missing_td_reqs = [
                r for r in td_reqs
                if r not in (all_added_corpus if any(_is_test_path(tf) for tf in td_files) else src_added_corpus)
            ]
            if uncreated_td or missing_td_reqs:
                remaining_todos.append(item_txt)
            elif missing_plan_tokens and any(not _is_test_path(tf) for tf in td_files):
                remaining_todos.append(item_txt)
            elif td_reqs and not missing_td_reqs:
                continue
            elif item_status in ("in_progress", "pending") and missing_plan_tokens:
                remaining_todos.append(item_txt)
            elif idx >= consumed_open:
                remaining_todos.append(item_txt)

    # 3. Check uncreated files from plan document or user prompt (including test files)
    uncreated_files = []
    if messages is not None:
        for pf in _ff16_plan_files(ctx, messages, listing):
            if _is_doc(pf) or _ff16_is_plan_doc(pf):
                continue
            in_listing = bool(listing) and any(_ff12_same_file(pf, lf) for lf in listing)
            if (not in_listing or _is_test_path(pf)) and not any(_ff12_same_file(pf, ep) for ep in edited_paths):
                if pf not in uncreated_files:
                    uncreated_files.append(pf)
    for cf in sorted(T._created_files(getattr(ctx, "scannable", "") or "", getattr(ctx, "folded", "") or "")):
        if (cf.lower() not in T._NON_FILE_NAMES
                and not any(_ff12_same_file(cf, ep) for ep in edited_paths)
                and not any(_ff12_same_file(cf, uf) for uf in uncreated_files)):
            uncreated_files.append(cf)
    if (not uncreated_files
            and _CREATE_TEST_INTENT_RE.search(getattr(ctx, "folded", "") or "")
            and not any(_is_test_path(ep) for ep in edited_paths)):
        uncreated_files.append("tests/test_new_feature.py")

    # 4. Check plan compliance before completing all plan work
    #    Run audit when no remaining todos/tokens, even if uncreated_files are still present
    if _ff17_c("flow17_plan_compliance_audit", True) and plan_bodies:
        if not remaining_todos and not missing_plan_tokens:
            audit_res = _ff17_audit_plan_compliance(messages, ev)
            if not audit_res["compliant"]:
                for v in audit_res["violations"]:
                    if v not in missing_plan_tokens:
                        missing_plan_tokens.append(v)

    has_more = bool(remaining_todos or missing_plan_tokens or uncreated_files or not src_edited_paths)
    return {
        "has_more": has_more,
        "remaining_todos": remaining_todos,
        "missing_plan_tokens": missing_plan_tokens,
        "uncreated_files": uncreated_files,
    }




def _ff17_attach_audit_note_to_answer(step_ans, msgs, ev, tested_after=False):
    if not step_ans or not _ff17_c("flow17_plan_compliance_audit", True):
        return step_ans
    try:
        audit_res = _ff17_audit_plan_compliance(msgs, ev)
        test_status_str = " (đã verify bằng test)" if tested_after else " (chưa verify bằng test)"
        if not audit_res.get("compliant") and audit_res.get("violations"):
            v_str = "\n".join("- " + v for v in audit_res["violations"][:5])
            note = f"\n\n[Plan Compliance Audit{test_status_str}]: Phát hiện không tuân thủ plan:\n{v_str}"
            step_ans.instruction = (step_ans.instruction or "") + note
        elif audit_res.get("compliant") and (msgs and _ff16_read_plan_bodies(msgs)):
            step_ans.instruction = (step_ans.instruction or "") + f"\n\n[Plan Compliance Audit{test_status_str}]: Tuân thủ đầy đủ các yêu cầu trong plan."
    except Exception:
        pass
    return step_ans


def _change_tail_s5(pat, ctx, ev, tm, names_all, total, extra=""):
    if _ff17_c("flow17_plan_todo_act") and tm.first("act"):
        try:
            msgs = getattr(_ff8_tls, "messages", None)
            acts = [e for e in ev if e["kind"] == "act"]
            plan_max_acts = max(int(_cfg("flow_max_acts")), int(_ff17_c("flow17_plan_max_acts", 12) or 12))
            post_test_max_acts = int(_ff17_c("flow17_post_test_max_acts", 4) or 4)
            total_max_acts = plan_max_acts + post_test_max_acts
            has_plan_or_todos = bool(
                (msgs and _ff16_read_plan_bodies(msgs))
                or any(_ff16_is_todo_tool(e.get("name", "")) for e in ev)
            )

            runs = _test_runs(ev)
            last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"), default=-1)
            after = [r for r in runs if r[0] > last_act]
            first_test_idx = min((i for i, e in enumerate(ev) if e["kind"] == "exec" and _is_test_cmd(_call_command(e["args"]))), default=-1)
            post_test_acts = [e for i, e in enumerate(ev) if e["kind"] == "act" and first_test_idx != -1 and i > first_test_idx]

            # Case A: Tests ran after the last act (_tests_ran_after_act is True)
            if after:
                failed_test = next((r for r in reversed(after) if test_outcome(r[2]) in ("fail", "broken")), None)
                if failed_test is not None:
                    # Test failed: allow self-healing edit if budget allows
                    if len(post_test_acts) < post_test_max_acts and len(acts) < total_max_acts:
                        stale = [p for p in _ff9_stale_edits(ev) if p.lower().endswith(_SRC_EXT)][:4]
                        if stale and tm.first("read"):
                            return _read_step(
                                pat, tm, stale, 2, total,
                                instruction="Your last edit changed these files, so your copy is "
                                            "stale. Re-read them before the next edit: "
                                            + ", ".join(stale) + "."
                            )
                        failed_cmd, failed_res = failed_test[1], failed_test[2]
                        excerpt_txt = _excerpt(failed_res, 1800)
                        locs = failure_locations(failed_res)
                        loc_str = ""
                        if locs:
                            loc_items = [f"{p}:{ln}" for p, ln in locs[:3]]
                            loc_str = " Potential failure locations: " + ", ".join(loc_items) + "."
                        msg = (
                            f"The test command `{failed_cmd}` FAILED after your changes.{loc_str} "
                            f"Analyze the traceback and fix the cause with an edit/write tool_call "
                            f"(copy oldString verbatim from the file as it is now). "
                            f"Test failure excerpt:\n{excerpt_txt}"
                        )
                        return Step(
                            pat, "act", max(3, total - 2), total, {"act"},
                            msg + _DO_NOT_DESCRIBE + extra,
                            None, False, tm.names({"act"})
                        )
                    else:
                        st = _answer_step("CHANGE", ev, total, total, final=True)
                        if st:
                            st.pattern = pat
                            st = _ff17_attach_audit_note_to_answer(st, msgs, ev, tested_after=bool(after))
                        return st
                else:
                    # All tests in after passed! Check if further verification commands are pending
                    verification_cmds = _ff17_extract_verification_commands(msgs, names_all, ev)
                    targeted_cmds = _ff17_targeted_test_commands(names_all, ev)
                    wanted_cmds = []
                    for c in targeted_cmds + verification_cmds:
                        c_clean = c.strip()
                        if c_clean and c_clean not in wanted_cmds:
                            wanted_cmds.append(c_clean)
                    ran_cmds_after = {
                        _call_command(e["args"]).strip()
                        for i, e in enumerate(ev)
                        if i > last_act and e["kind"] == "exec" and _is_test_cmd(_call_command(e["args"]))
                    }
                    next_test = next((c for c in wanted_cmds if c not in ran_cmds_after), None)
                    if next_test and tm.first("exec"):
                        return _tests_step(pat, tm, next_test, 4, total)

                    # All tests ran and passed: check plan compliance before finishing
                    if _ff17_c("flow17_plan_compliance_audit", True) and has_plan_or_todos:
                        audit_res = _ff17_audit_plan_compliance(msgs, ev)
                        if not audit_res["compliant"] and (len(post_test_acts) < post_test_max_acts and len(acts) < total_max_acts):
                            viol_str = "; ".join(audit_res["violations"][:4])
                            msg = (
                                f"All unit tests passed, but Plan Compliance Audit found discrepancies: {viol_str}. "
                                f"Please fix these discrepancies using edit/write tool_calls to fully adhere to the Plan."
                            )
                            return Step(
                                pat, "act", max(3, total - 2), total, {"act"},
                                msg + _DO_NOT_DESCRIBE + extra,
                                None, False, tm.names({"act"})
                            )

                    # Before finishing: check if any plan files are still missing
                    rem_final = _ff17_remaining_plan_or_todo_work(ctx, ev, msgs)
                    if rem_final.get("uncreated_files") and len(acts) < total_max_acts and tm.first("act"):
                        uc_str = ", ".join(rem_final["uncreated_files"][:4])
                        msg = (
                            f"Tests passed, but these plan files are still missing: "
                            f"{uc_str}. Create them before finishing."
                        )
                        return Step(
                            pat, "act", max(3, total - 2), total, {"act"},
                            msg + _DO_NOT_DESCRIBE + extra,
                            None, False, tm.names({"act"})
                        )

                    st = _answer_step("CHANGE", ev, total, total)
                    if st:
                        st.pattern = pat
                        st = _ff17_attach_audit_note_to_answer(st, msgs, ev, tested_after=bool(after))
                    return st

            # Case B: Model just did an act after a prior test failed (re-run the failed test!)
            failed_prev = next((r for r in reversed(runs) if test_outcome(r[2]) in ("fail", "broken") and r[0] < last_act), None)
            if failed_prev is not None and not after and acts:
                cmd_to_run = failed_prev[1] or pick_test_command(names_all, ev)
                if cmd_to_run and tm.first("exec"):
                    return _tests_step(pat, tm, cmd_to_run, 4, total)

            # Case C: No tests have run yet (Initial implementation phase)
            if len(acts) < plan_max_acts and (has_plan_or_todos or any(_act_ok(e) for e in acts)):
                rem = _ff17_remaining_plan_or_todo_work(ctx, ev, msgs)
                if rem["has_more"] and (rem["missing_plan_tokens"] or rem["remaining_todos"] or rem["uncreated_files"]):
                    parts = [
                        "Implement the plan steps now using edit/write tool_calls "
                        "(do NOT call read/grep/shell -- the plan and target code windows are "
                        "already provided below)."
                    ]
                    if acts and not _act_ok(acts[-1]):
                        parts.append(_failed_act_hint(ev).strip())
                    if rem["remaining_todos"]:
                        parts.append(
                            f"Next pending TODO ({len(rem['remaining_todos'])} remaining): "
                            f"{rem['remaining_todos'][0]}."
                        )
                    if rem["missing_plan_tokens"]:
                        parts.append(
                            "Still missing required symbols/functions/routes in the source file from the plan: "
                            + ", ".join(f"`{t}`" for t in rem["missing_plan_tokens"][:8])
                            + ". Implement or update them in the source file now (you may combine related additions in ONE edit call)."
                        )
                        if rem["uncreated_files"]:
                            parts.append(
                                "After the source symbols/routes are implemented, create: "
                                + ", ".join(f"`{u}`" for u in rem["uncreated_files"][:3])
                                + "."
                            )
                    elif rem["uncreated_files"]:
                        parts.append(
                            "Create the required test/new file now using the write tool: "
                            + ", ".join(f"`{u}`" for u in rem["uncreated_files"][:3])
                            + "."
                        )
                    parts.append(
                        "Emit the edit/write tool_call now (copying oldString "
                        "verbatim from the Target Code Windows below without line-number prefixes)."
                    )
                    return Step(
                        pat, "act", 3, total, {"act"},
                        " ".join(parts) + _DO_NOT_DESCRIBE + extra,
                        None, False, tm.names({"act"})
                    )
                else:
                    # Guard: if there are still uncreated files from the plan,
                    # force act step instead of running tests on non-existent files
                    if rem.get("uncreated_files") and len(acts) < plan_max_acts and tm.first("act"):
                        uc_str = ", ".join(rem["uncreated_files"][:4])
                        msg = (
                            f"The following files from the plan have NOT been created yet: "
                            f"{uc_str}. Create them now using write/edit tool_calls."
                        )
                        return Step(
                            pat, "act", max(3, total - 2), total, {"act"},
                            msg + _DO_NOT_DESCRIBE + extra,
                            None, False, tm.names({"act"})
                        )
                    cmd = pick_test_command(names_all, ev)
                    if cmd and tm.first("exec"):
                        return _tests_step(pat, tm, cmd, 4, total)
        except Exception as ex:
            log("[fix17] plan/todo act continuation error: %s" % ex)
    res = _change_tail_s4(pat, ctx, ev, tm, names_all, total, extra=extra)
    if res and getattr(res, "name", None) == "answer":
        res = _ff17_attach_audit_note_to_answer(res, msgs, ev, tested_after=bool(after if "after" in locals() else False))
    return res




def _ff16_todo_ledger(ctx, messages) -> str:
    base_ledger = _ff16_todo_ledger_s2(ctx, messages)
    if not _ff16_c("flow16_plan_todo") or ctx is None:
        return base_ledger
    try:
        ev = _answered_calls(messages or [])
        ok_acts = [e for e in ev if e.get("kind") == "act" and _act_ok(e)]
        edited_files = sorted({_call_path(e["args"]) for e in ok_acts if _call_path(e.get("args") or {})})
        tested = _tests_ran_after_act(ev) if edited_files else bool(_test_runs(ev))

        has_plan_doc = bool(_ff16_read_plan_bodies(messages or []))
        eval_steps = _ff17_evaluate_plan_steps(ctx, ev, messages)
        todo_calls = [
            e for e in ev
            if _ff16_is_todo_tool(e.get("name", ""))
            and isinstance((e.get("args") or {}).get("todos"), list)
        ]

        # Prefer the plan document's concrete checklist steps when a plan doc was read
        if base_ledger and eval_steps and (has_plan_doc or not todo_calls):
            s_parts = [
                f"{s['index']}. {'[x]' if s['done'] else '[ ]'} {s['title']}"
                for s in eval_steps[:14]
            ]
            prefix_lines = [ln for ln in base_ledger.splitlines() if not ln.startswith("Steps: ")]
            prefix_lines.append("Steps: " + " | ".join(s_parts))
            return "\n".join(prefix_lines)

        if todo_calls:
            raw_todos = [
                td for td in todo_calls[-1]["args"]["todos"]
                if isinstance(td, dict) and str(td.get("content") or td.get("title") or "").strip()
            ]
            if raw_todos:
                s_parts = []
                rem_info = _ff17_remaining_plan_or_todo_work(ctx, ev, messages)
                rem_set = {x.lower() for x in rem_info["remaining_todos"]}
                for idx, td in enumerate(raw_todos[:12], 1):
                    txt = str(td.get("content") or td.get("title") or "").strip()
                    st_raw = str(td.get("status") or "").strip().lower()
                    if _ff17_is_pure_test_todo(txt, edited_files):
                        done = tested
                    elif txt.lower() in rem_set:
                        done = False
                    elif st_raw in ("completed", "done") and not rem_info["missing_plan_tokens"]:
                        done = True
                    else:
                        done = False
                    s_parts.append(f"{idx}. {'[x]' if done else '[ ]'} {txt}")
                lines = ["[Plan & TODO Progress]"]
                if s_parts:
                    lines.append("Steps: " + " | ".join(s_parts))
                return "\n".join(lines)
    except Exception as ex:
        log("[fix17] todo ledger refine error: %s" % ex)
    return base_ledger


_FF17_TODOWRITE_NAME_RE = re.compile(r"(?i)^(?:todo_?write|write_?todos?)$")


def _ff17_attach_todowrite(ctx, messages, tm, step, calls) -> list:
    """When `flow16_plan_todo` is enabled and the client provides `todowrite`,
    automatically synthesize and prepend a `todowrite` tool_call whenever a plan
    document's step statuses change so client UIs (like OpenCode) render live TODOs."""
    out_calls = list(calls or [])
    if not out_calls or not _ff16_c("flow16_plan_todo") or tm is None or ctx is None:
        return out_calls
    try:
        if any(_ff16_is_todo_tool((tc.get("function") or {}).get("name")) for tc in out_calls):
            return out_calls
        todo_tool_name = next(
            (n for n in tm.by_kind.get("other", []) if _FF17_TODOWRITE_NAME_RE.match(str(n or "").strip())),
            None,
        )
        if not todo_tool_name:
            return out_calls
        ev = _answered_calls(messages or [])
        has_plan_doc = bool(_ff16_read_plan_bodies(messages or []))
        has_prior_todo = any(_ff16_is_todo_tool(e.get("name", "")) for e in ev)
        if not (has_plan_doc or has_prior_todo):
            return out_calls
        step_name = getattr(step, "name", None)
        eval_steps = _ff17_evaluate_plan_steps(
            ctx, ev, messages, pending_calls=out_calls, step_name=step_name
        )
        if not eval_steps:
            return out_calls

        todos_payload = []
        in_prog_assigned = False
        for s in eval_steps[:14]:
            if s["done"]:
                st_str = "completed"
            elif not in_prog_assigned:
                st_str = "in_progress"
                in_prog_assigned = True
            else:
                st_str = "pending"
            todos_payload.append({
                "id": str(s["index"]),
                "content": s["title"],
                "status": st_str,
                "priority": "high" if s["index"] <= 3 else "medium",
            })

        # Deduplicate against the most recent todowrite call in `ev`
        todo_calls = [
            e for e in ev
            if _ff16_is_todo_tool(e.get("name", ""))
            and isinstance((e.get("args") or {}).get("todos"), list)
        ]
        if todo_calls:
            prev_todos = todo_calls[-1]["args"]["todos"]
            prev_sig = [
                (str(t.get("content") or "").strip(), str(t.get("status") or "").strip().lower())
                for t in prev_todos if isinstance(t, dict)
            ]
            new_sig = [(t["content"], t["status"]) for t in todos_payload]
            if prev_sig == new_sig:
                return out_calls

        tc_todo = {
            "id": "call_" + uuid.uuid4().hex[:8],
            "type": "function",
            "function": {
                "name": todo_tool_name,
                "arguments": json.dumps({"todos": todos_payload}, ensure_ascii=False),
            },
        }
        return [tc_todo] + out_calls
    except Exception as ex:
        log("[fix17] attach todowrite error: %s" % ex)
        return out_calls


def _ff17_extract_code_targets_from_text(text: str):
    """Extract candidate function/class/global names, routes, and `# Section:` names
    from `text` without polluting `cand_idents` with JSON field names or prose words."""
    idents, routes, sections = set(), set(), set()
    if not text:
        return idents, routes, sections
    for m_sec in re.finditer(r"#\s*Section:\s*([A-Za-z0-9_.-]+)", text, re.I):
        sections.add(m_sec.group(1).lower())
    for sp in re.findall(r"`([^`\n]+)`", text):
        sp_s = sp.strip()
        for m_rt in re.finditer(r"(/v\d+(?:beta)?/[A-Za-z0-9_/-]+)", sp_s):
            routes.add(m_rt.group(1))
        m_fn = re.match(r"^(?:def\s+|class\s+)?([A-Za-z_]\w{2,})\s*(?:\(|$)", sp_s)
        m_var = re.match(r"^([A-Za-z_]\w{2,})\s*=", sp_s)
        if m_fn:
            tok = m_fn.group(1)
            if tok not in _FF17_STOP_IDENTS:
                idents.add(tok)
        elif m_var:
            tok = m_var.group(1)
            if tok not in _FF17_STOP_IDENTS:
                idents.add(tok)
        else:
            for m_call in re.finditer(r"\b([A-Za-z_]\w{2,})\s*\(", sp_s):
                tok = m_call.group(1)
                if tok not in _FF17_STOP_IDENTS:
                    idents.add(tok)
    return idents, routes, sections


def _ff17_act_context_pack(ctx, messages) -> str:
    """When read tool results exceed 80 KB (so `_join_prompt_parts` truncates the
    earlier chunks of a large file and/or the plan document), or after a test failure,
    build a compact verbatim context pack containing:
      1. Any read plan document (`feature_implementation_plan.md`)
      2. Post-test failure excerpt and target code windows for traceback functions
      3. Unnumbered verbatim code windows from the current post-edit file text
         (`_ff9_read_texts(messages)`), prioritizing windows for PENDING (`[ ]`)
         plan steps so completed steps do not crowd out later targets like `do_GET`.
    """
    ev = _answered_calls(messages or [])
    total_read_chars = sum(
        len(e.get("result") or "") for e in ev
        if e.get("kind") == "read" and _read_ok(e)
    )

    runs = _test_runs(ev)
    last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"), default=-1)
    after = [r for r in runs if r[0] > last_act]
    last_test_res = after[-1][2] if after else (runs[-1][2] if runs else "")
    test_failed = test_outcome(last_test_res) in ("fail", "broken")

    if total_read_chars < 80000 and not test_failed:
        return ""

    out_blocks = []
    if test_failed and last_test_res:
        out_blocks.append(
            "[Post-Test Repair Context -- Recent Test Failure Excerpt & Locations:]\n"
            + _excerpt(last_test_res, 1800)
        )

    plan_bodies = _ff16_read_plan_bodies(messages or [])
    for p_path, p_body in plan_bodies[:2]:
        if p_body:
            out_blocks.append(
                f"[Plan Document ({p_path}) -- already read in full; DO NOT call read on {p_path} again:]\n"
                + p_body[:12000]
            )

    plan_text = "\n".join(b for _p, b in plan_bodies)
    req_text = (getattr(ctx, "request", "") or getattr(ctx, "last", "") or "") + "\n" + plan_text

    tb_funcs = set()
    tb_files = set()
    if test_failed and last_test_res:
        for m in re.finditer(r'File\s+["\x27]([^"\x27]+)["\x27],\s+line\s+(\d+),\s+in\s+([A-Za-z_]\w*)', last_test_res):
            f_p, f_ln, f_fn = m.group(1), int(m.group(2)), m.group(3)
            if not _SKIP_PATH.search(f_p) and not f_fn.startswith("<"):
                tb_funcs.add(f_fn)
                tb_files.add(f_p)
        for p, ln in failure_locations(last_test_res):
            tb_files.add(p)

    eval_steps = _ff17_evaluate_plan_steps(ctx, ev, messages) if plan_bodies else []
    pending_src_steps = [
        s for s in eval_steps
        if not s["done"] and not s["is_test_run"] and not s["is_test_step"]
    ]
    if pending_src_steps:
        pending_text = "\n".join(s["full"] for s in pending_src_steps)
        cand_idents, cand_routes, cand_sections = _ff17_extract_code_targets_from_text(pending_text)
        needs_new_defs = any(
            any(not tok.startswith("/v") for tok in s["missing_tokens"])
            for s in pending_src_steps
        )
        if not needs_new_defs and cand_idents:
            cand_sections = set()
    else:
        cand_idents, cand_routes, cand_sections = _ff17_extract_code_targets_from_text(req_text)
        for kw in _keywords(getattr(ctx, "request", "") or ""):
            if len(kw) >= 4 and kw not in _FF17_STOP_IDENTS:
                cand_idents.add(kw)

    if tb_funcs:
        cand_idents.update(tb_funcs)

    tracked_texts = _ff9_read_texts(messages or [])
    if not tracked_texts:
        for v in _ff8_file_views(ev):
            clean_s, _ = _ff17_strip_numbered_lines(v.get("text") or "")
            if clean_s:
                tracked_texts[v.get("path") or ""] = clean_s

    budget_remaining = 48000
    for fpath, full_src in tracked_texts.items():
        is_fpath_in_tb = any(_ff12_same_file(fpath, tf) for tf in tb_files)
        if not full_src or _ff16_is_plan_doc(fpath) or _is_doc(fpath):
            continue
        if len(full_src) < 60000 and not is_fpath_in_tb:
            continue
        clean_src, _ = _ff17_strip_numbered_lines(full_src)
        src_lines = clean_src.splitlines()
        windows = []

        if test_failed and is_fpath_in_tb and last_test_res:
            for p, ln in failure_locations(last_test_res):
                if _ff12_same_file(p, fpath):
                    s_ln = max(1, ln - 10)
                    e_ln = min(len(src_lines), ln + 15)
                    windows.append((s_ln, e_ln, f"failure_line_{ln}"))

        # 1. Include `# Section: <name>` headers referenced by active steps (+ 60 lines of module globals)
        if cand_sections:
            for idx_ln, ln_str in enumerate(src_lines, 1):
                m_sh = re.match(r"^#\s*Section:\s*([A-Za-z0-9_.-]+)", ln_str.strip(), re.I)
                if m_sh and m_sh.group(1).lower() in cand_sections:
                    windows.append((max(1, idx_ln - 1), min(len(src_lines), idx_ln + 60), f"Section:{m_sh.group(1)}"))

        try:
            tree = _ff5_ast.parse(clean_src)
        except Exception:
            tree = None

        if tree is not None:
            referenced_globals = set()

            def _collect_ast_windows(stmts):
                for node in stmts:
                    if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
                        if node.name in cand_idents:
                            s_ln = max(1, node.lineno - 3)
                            e_ln = min(len(src_lines), getattr(node, "end_lineno", node.lineno) + 3)
                            windows.append((s_ln, e_ln, node.name))
                            for sub in _ff5_ast.walk(node):
                                if isinstance(sub, _ff5_ast.Global):
                                    referenced_globals.update(sub.names)
                                elif isinstance(sub, _ff5_ast.Name) and sub.id.startswith("_"):
                                    referenced_globals.add(sub.id)
                    elif isinstance(node, _ff5_ast.ClassDef):
                        if node.name in cand_idents and (getattr(node, "end_lineno", node.lineno) - node.lineno) <= 120:
                            s_ln = max(1, node.lineno - 3)
                            e_ln = min(len(src_lines), getattr(node, "end_lineno", node.lineno) + 3)
                            windows.append((s_ln, e_ln, node.name))
                        _collect_ast_windows(node.body)
                    elif isinstance(node, _ff5_ast.Assign):
                        for t in node.targets:
                            if isinstance(t, _ff5_ast.Name) and t.id in cand_idents:
                                s_ln = max(1, node.lineno - 3)
                                e_ln = min(len(src_lines), getattr(node, "end_lineno", node.lineno) + 3)
                                windows.append((s_ln, e_ln, t.id))

            _collect_ast_windows(tree.body)
            # Second pass: include top-level module variables referenced inside matched functions
            if referenced_globals:
                for node in tree.body:
                    if isinstance(node, _ff5_ast.Assign):
                        for t in node.targets:
                            if isinstance(t, _ff5_ast.Name) and t.id in referenced_globals and not t.id.startswith("_ff"):
                                s_ln = max(1, node.lineno - 3)
                                e_ln = min(len(src_lines), getattr(node, "end_lineno", node.lineno) + 3)
                                windows.append((s_ln, e_ln, t.id))
        else:
            # Regex fallback when AST parsing fails (e.g. truncated file or syntax error)
            ident_pat = "|".join(re.escape(x) for x in sorted(cand_idents) if len(x) >= 3)
            if ident_pat:
                def_rx = re.compile(rf"^([ \t]*)(?:async\s+def|def|class)\s+({ident_pat})\b|^([ \t]*)({ident_pat})\s*=")
                for idx_ln, ln_str in enumerate(src_lines, 1):
                    m_d = def_rx.match(ln_str)
                    if not m_d:
                        continue
                    indent = len(m_d.group(1) or m_d.group(3) or "")
                    sym = m_d.group(2) or m_d.group(4)
                    end_ln = min(len(src_lines), idx_ln + 75)
                    for k_ln in range(idx_ln + 1, min(len(src_lines) + 1, idx_ln + 120)):
                        s_k = src_lines[k_ln - 1]
                        if s_k.strip() and not s_k.lstrip().startswith("#"):
                            cur_ind = len(s_k) - len(s_k.lstrip())
                            if cur_ind <= indent and re.match(r"^(?:def |async def |class |# ===|[A-Za-z_]\w*\s*=)", s_k.lstrip()):
                                end_ln = k_ln - 1
                                break
                    windows.append((max(1, idx_ln - 3), min(len(src_lines), end_ln + 2), sym))

        for idx_ln, ln_str in enumerate(src_lines, 1):
            if any(rt in ln_str for rt in cand_routes):
                windows.append((max(1, idx_ln - 8), min(len(src_lines), idx_ln + 15), "route"))

        if not windows:
            continue
        windows.sort()
        merged = []
        for s_ln, e_ln, wname in windows:
            if merged and s_ln <= merged[-1][1] + 5:
                prev_s, prev_e, prev_n = merged[-1]
                new_n = prev_n if wname in prev_n.split(",") else (prev_n + "," + wname)
                merged[-1] = (prev_s, max(prev_e, e_ln), new_n)
            else:
                merged.append((s_ln, e_ln, wname))

        file_excerpts = [
            f"[Target Code Windows from {fpath} (current post-edit state, NO line numbers -- DO NOT call read on {fpath}; copy `oldString` verbatim from these blocks):]"
        ]
        for s_ln, e_ln, wnames in merged:
            snippet = "\n".join(src_lines[s_ln - 1:e_ln])
            if len(snippet) > budget_remaining and len(file_excerpts) > 1:
                break
            header = f"--- {fpath} lines {s_ln}-{e_ln} ({wnames}) ---"
            file_excerpts.append(header + "\n" + snippet)
            budget_remaining -= len(snippet)
            if budget_remaining <= 0:
                break
        if len(file_excerpts) > 1:
            out_blocks.append("\n\n".join(file_excerpts))

    return "\n\n".join(out_blocks)




def _ff5_after_plan_s3(step, messages, tm, ctx=None):
    step = _ff5_after_plan_s2(step, messages, tm, ctx)
    try:
        if step is not None and getattr(step, "local_text", None) is None:
            if getattr(step, "name", None) == "answer":
                idx_block = _ff17_large_file_structure_index(messages)
                if idx_block and "[Structure index for " not in (step.instruction or ""):
                    step.instruction = (step.instruction or "") + "\n\n" + idx_block
            elif (getattr(step, "name", None) == "act"
                  and not getattr(step, "drive", False)
                  and _ff17_c("flow17_act_context_pack")):
                if ctx is None:
                    ctx = _analyze(messages)
                pack = _ff17_act_context_pack(ctx, messages)
                if pack and "[Target Code Windows from " not in (step.instruction or "") and "[Plan Document (" not in (step.instruction or ""):
                    step.instruction = (step.instruction or "") + "\n\n" + pack
    except Exception as e:
        log(f"[fix17] after_plan context/index error: {e}")
    return step


def _setup_ff17():
    for k, v in _FF17_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    log("[fix17] flow_fix17 installed")
# ==== flowfix17:end ====

# ==== flowfix18:start ====
# Flow Engine 18: Post-test repair budget & loop intelligence
#   1. Group consecutive successful acts into repair rounds (flow18_max_repair_rounds=4)
#   2. Failure fingerprinting (detect identical failure vs progress)
#   3. Progress bonus rounds (flow18_progress_bonus_rounds=2, hard cap flow18_hard_max_acts=24)
#   4. Diagnostic context & warning injection on repeated identical failure
#   5. Final answer honesty guard (flow18_honest_final=True)



def _ff18_c(key, default=None):
    if not _flow_c("flow18_enabled", True):
        return False
    return _flow_c(key, _FF18_DEFAULTS.get(key, default))


def _ff18_failed_test_names(res: str) -> list:
    """Extract sorted unique failed/errored test function names from test output."""
    if not res:
        return []
    names = set()
    for m in re.finditer(r"(?m)^(?:FAIL|ERROR):\s*([A-Za-z0-9_]+)", res):
        names.add(m.group(1))
    for m in re.finditer(r"(?m)^FAILED\s+\S*?([A-Za-z0-9_]+)(?:\s+-\s+.*)?$", res):
        names.add(m.group(1))
    return sorted(names)


def _ff18_fail_fingerprint_s1(res: str) -> str:
    """Normalize test traceback and compute a stable hash for regression/progress detection."""
    if not res:
        return ""
    failed_names = _ff18_failed_test_names(res)
    norm = res
    norm = re.sub(r"Ran \d+ tests in [\d.]+s", "", norm)
    norm = re.sub(r"line \d+", "line X", norm)
    norm = re.sub(r":\d+:", ":X:", norm)
    norm = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", norm)
    norm = re.sub(r'File "[^"\n]+"', 'File "<path>"', norm)
    norm = re.sub(r"File '[^'\n]+'", "File '<path>'", norm)
    norm = re.sub(r"(?i)(?:[a-z]:[\\/][^\s\"']*[\\/]temp[\\/]|/tmp/)tmp[a-z0-9_]+", "<tmp>", norm)
    lines = [
        ln.strip() for ln in norm.splitlines()
        if re.search(r"(?:FAIL|ERROR|FAILED|AssertionError|Exception|Error:)", ln)
    ]
    key_str = "|".join(failed_names) + "\n" + "\n".join(lines)
    return _ff17_hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]


def _ff18_repair_rounds(ev) -> int:
    """Count completed repair rounds after the first test failure.
    A repair round is a group of one or more successful act events between test executions.
    Parallel edits in the same turn belong to the same round."""
    first_failed_test_idx = -1
    for i, e in enumerate(ev):
        if e.get("kind") == "exec":
            cmd = _call_command(e.get("args") or {})
            if _is_test_cmd(cmd) and test_outcome(e.get("result") or "") in ("fail", "broken"):
                first_failed_test_idx = i
                break
    if first_failed_test_idx == -1:
        return 0

    rounds = 0
    cur_round_had_ok_act = False
    for i in range(first_failed_test_idx + 1, len(ev)):
        e = ev[i]
        k = e.get("kind")
        if k == "act":
            if _act_ok(e):
                cur_round_had_ok_act = True
        elif k == "exec":
            cmd = _call_command(e.get("args") or {})
            if _is_test_cmd(cmd):
                if cur_round_had_ok_act:
                    rounds += 1
                    cur_round_had_ok_act = False
    if cur_round_had_ok_act:
        rounds += 1
    return rounds


def _ff18_analyze_failure_progression_s1(ev) -> dict:
    """Analyze the history of test failures to determine progress, bonus rounds, and identical failure loops."""
    runs = _test_runs(ev)
    failed_runs = [
        r for r in runs
        if test_outcome(r[2]) in ("fail", "broken")
    ]
    if not failed_runs:
        return {
            "rounds_used": 0,
            "bonus_rounds": 0,
            "is_no_progress": False,
            "failed_names": [],
            "latest_fingerprint": "",
            "progress_count": 0,
        }

    rounds_used = _ff18_repair_rounds(ev)
    fps = [_ff18_fail_fingerprint(r[2]) for r in failed_runs]
    names_list = [_ff18_failed_test_names(r[2]) for r in failed_runs]

    progress_count = 0
    # Evaluate progress between consecutive failed runs that had act(s) between them
    for j in range(1, len(failed_runs)):
        prev_idx = failed_runs[j - 1][0]
        cur_idx = failed_runs[j][0]
        had_acts = any(ev[k].get("kind") == "act" for k in range(prev_idx + 1, cur_idx))
        if had_acts:
            prev_names = names_list[j - 1]
            cur_names = names_list[j]
            prev_fp = fps[j - 1]
            cur_fp = fps[j]
            # Progress if fewer tests fail or error signature changed
            if len(cur_names) < len(prev_names) or (cur_fp != prev_fp and test_outcome(failed_runs[j][2]) != "broken"):
                progress_count += 1

    bonus_cap = int(_ff18_c("flow18_progress_bonus_rounds", 2) or 2)
    bonus_rounds = min(bonus_cap, progress_count)

    is_no_progress = False
    if len(failed_runs) >= 2:
        last_prev_idx = failed_runs[-2][0]
        last_cur_idx = failed_runs[-1][0]
        had_acts = any(ev[k].get("kind") == "act" for k in range(last_prev_idx + 1, last_cur_idx))
        if had_acts and fps[-1] == fps[-2]:
            is_no_progress = True

    return {
        "rounds_used": rounds_used,
        "bonus_rounds": bonus_rounds,
        "is_no_progress": is_no_progress,
        "failed_names": names_list[-1],
        "latest_fingerprint": fps[-1],
        "progress_count": progress_count,
    }


def _ff18_extract_test_symbols(failed_res: str, messages: list, ev: list) -> set:
    """Extract project functions and variables called or asserted inside failing test methods."""
    failed_names = set(_ff18_failed_test_names(failed_res))
    if not failed_names:
        return set()

    locs = failure_locations(failed_res)
    test_files = [p for p, _ln in locs if p.lower().endswith(".py")]

    tracked = _ff9_read_texts(messages or [])
    if ev:
        for v in _ff8_file_views(ev):
            p = v.get("path") or ""
            if p and p not in tracked:
                tracked[p] = v.get("text") or ""

    test_sources = []
    for tf in test_files:
        src = (
            tracked.get(tf)
            or tracked.get(tf.replace("\\", "/"))
            or tracked.get(tf.replace("/", "\\"))
        )
        if not src and os.path.exists(tf):
            try:
                with open(tf, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except Exception:
                src = None
        if src:
            test_sources.append(src)

    if not test_sources:
        for p, src in tracked.items():
            if "test" in p.lower() and any(fn in src for fn in failed_names):
                test_sources.append(src)

    stop_symbols = {
        "self", "assertEqual", "assertNotEqual", "assertTrue", "assertFalse",
        "assertRaises", "assertIn", "assertNotIn", "assertIsNone", "assertIsNotNone",
        "assertAlmostEqual", "assertListEqual", "assertDictEqual", "assertSetEqual",
        "None", "True", "False", "len", "range", "str", "int", "dict", "list",
        "set", "tuple", "bool", "float", "print", "open", "unittest", "pytest",
        "patch", "MagicMock", "Mock", "setUp", "tearDown", "Exception", "args", "kwargs",
    }

    called_symbols = set()
    for src in test_sources:
        clean_src, _ = _ff17_strip_numbered_lines(src)
        try:
            tree = _ff5_ast.parse(clean_src)
        except Exception:
            tree = None
        if tree is not None:
            for node in _ff5_ast.walk(tree):
                if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
                    if node.name in failed_names:
                        for sub in _ff5_ast.walk(node):
                            if isinstance(sub, _ff5_ast.Name):
                                if sub.id not in stop_symbols and not sub.id.startswith("__"):
                                    called_symbols.add(sub.id)
                            elif isinstance(sub, _ff5_ast.Attribute):
                                if isinstance(sub.value, _ff5_ast.Name) and sub.value.id not in stop_symbols:
                                    called_symbols.add(sub.value.id)
                                if sub.attr not in stop_symbols and not sub.attr.startswith("__"):
                                    called_symbols.add(sub.attr)
        else:
            for fn in failed_names:
                m = re.search(rf"def\s+{re.escape(fn)}\s*\([^)]*\):(.*?)(?=\n\s*(?:def|class|\Z))", clean_src, re.S)
                if m:
                    body = m.group(1)
                    for sym in re.findall(r"\b[A-Za-z_]\w*\b", body):
                        if sym not in stop_symbols and not sym.startswith("__"):
                            called_symbols.add(sym)

    return called_symbols


def _ff18_diagnostic_context(called_symbols: set, messages: list, ev: list) -> str:
    """Generate verbatim code windows from project source files for symbols tested/called in failing tests."""
    if not called_symbols:
        return ""

    tracked = _ff9_read_texts(messages or [])
    if ev:
        for v in _ff8_file_views(ev):
            p = v.get("path") or ""
            if p and p not in tracked:
                tracked[p] = v.get("text") or ""

    out_blocks = []
    budget_remaining = 24000

    for fpath, full_src in tracked.items():
        if not full_src or _ff16_is_plan_doc(fpath) or _is_doc(fpath) or _is_test_path(fpath):
            continue
        clean_src, _ = _ff17_strip_numbered_lines(full_src)
        src_lines = clean_src.splitlines()

        try:
            tree = _ff5_ast.parse(clean_src)
        except Exception:
            tree = None

        matched_windows = []
        if tree is not None:
            for node in _ff5_ast.walk(tree):
                if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
                    if node.name in called_symbols:
                        s_ln = max(1, node.lineno - 2)
                        e_ln = min(len(src_lines), getattr(node, "end_lineno", node.lineno) + 2)
                        matched_windows.append((s_ln, e_ln, node.name))
                elif isinstance(node, _ff5_ast.ClassDef):
                    if node.name in called_symbols:
                        s_ln = max(1, node.lineno - 2)
                        e_ln = min(len(src_lines), getattr(node, "end_lineno", node.lineno) + 2)
                        matched_windows.append((s_ln, e_ln, node.name))
            for node in getattr(tree, "body", []):
                if isinstance(node, _ff5_ast.Assign):
                    for t in node.targets:
                        if isinstance(t, _ff5_ast.Name) and t.id in called_symbols:
                            s_ln = max(1, node.lineno - 2)
                            e_ln = min(len(src_lines), getattr(node, "end_lineno", node.lineno) + 2)
                            matched_windows.append((s_ln, e_ln, t.id))
        else:
            ident_pat = "|".join(re.escape(x) for x in sorted(called_symbols) if len(x) >= 3)
            if ident_pat:
                def_rx = re.compile(rf"^([ \t]*)(?:async\s+def|def)\s+({ident_pat})\b|^([ \t]*)({ident_pat})\s*=")
                for idx_ln, ln_str in enumerate(src_lines, 1):
                    m_d = def_rx.match(ln_str)
                    if m_d:
                        sym = m_d.group(2) or m_d.group(4)
                        s_ln = max(1, idx_ln - 2)
                        e_ln = min(len(src_lines), idx_ln + 40)
                        matched_windows.append((s_ln, e_ln, sym))

        if not matched_windows:
            continue

        matched_windows.sort()
        merged = []
        for s_ln, e_ln, wname in matched_windows:
            if merged and s_ln <= merged[-1][1] + 5:
                prev_s, prev_e, prev_n = merged[-1]
                new_n = prev_n if wname in prev_n.split(",") else (prev_n + "," + wname)
                merged[-1] = (prev_s, max(prev_e, e_ln), new_n)
            else:
                merged.append((s_ln, e_ln, wname))

        file_excerpts = [
            f"[Diagnostic Source Windows for Failing Test Symbols ({fpath}):]"
        ]
        for s_ln, e_ln, wnames in merged:
            snippet = "\n".join(src_lines[s_ln - 1:e_ln])
            if len(snippet) > budget_remaining and len(file_excerpts) > 1:
                break
            header = f"--- {fpath} lines {s_ln}-{e_ln} ({wnames}) ---"
            file_excerpts.append(header + "\n" + snippet)
            budget_remaining -= len(snippet)
            if budget_remaining <= 0:
                break
        if len(file_excerpts) > 1:
            out_blocks.append("\n\n".join(file_excerpts))

    return "\n\n".join(out_blocks)




def _change_tail_s6(pat, ctx, ev, tm, names_all, total, extra=""):
    if not _ff18_c("flow18_enabled", True):
        return _change_tail_s5(pat, ctx, ev, tm, names_all, total, extra=extra)

    try:
        msgs = getattr(_ff8_tls, "messages", None)
        acts = [e for e in ev if e.get("kind") == "act"]
        runs = _test_runs(ev)
        last_act = max((i for i, e in enumerate(ev) if e.get("kind") == "act"), default=-1)
        after = [r for r in runs if r[0] > last_act]

        if after and tm.first("act"):
            failed_test = next((r for r in reversed(after) if test_outcome(r[2]) in ("fail", "broken")), None)
            if failed_test is not None:
                progression = _ff18_analyze_failure_progression(ev)
                rounds_used = progression["rounds_used"]
                bonus_rounds = progression["bonus_rounds"]
                base_max_rounds = int(_ff18_c("flow18_max_repair_rounds", 4) or 4)
                effective_max_rounds = base_max_rounds + bonus_rounds
                hard_max_acts = int(_ff18_c("flow18_hard_max_acts", 24) or 24)

                log(
                    f"[fix18] round={rounds_used}/{effective_max_rounds} (bonus={bonus_rounds}) "
                    f"progress={'no' if progression['is_no_progress'] else 'yes'} "
                    f"fp={progression['latest_fingerprint']} acts={len(acts)}/{hard_max_acts}"
                )

                if rounds_used < effective_max_rounds and len(acts) < hard_max_acts:
                    stale = [p for p in _ff9_stale_edits(ev) if p.lower().endswith(_SRC_EXT)][:4]
                    if stale and tm.first("read"):
                        return _read_step(
                            pat, tm, stale, 2, total,
                            instruction="Your last edit changed these files, so your copy is "
                                        "stale. Re-read them before the next edit: "
                                        + ", ".join(stale) + "."
                        )

                    failed_cmd, failed_res = failed_test[1], failed_test[2]
                    excerpt_txt = _excerpt(failed_res, 1800)
                    locs = failure_locations(failed_res)
                    loc_str = ""
                    if locs:
                        loc_items = [f"{p}:{ln}" for p, ln in locs[:3]]
                        loc_str = " Potential failure locations: " + ", ".join(loc_items) + "."

                    msg = (
                        f"The test command `{failed_cmd}` FAILED after your changes.{loc_str} "
                        f"Analyze the traceback and fix the cause with an edit/write tool_call "
                        f"(copy oldString verbatim from the file as it is now). "
                        f"Test failure excerpt:\n{excerpt_txt}"
                    )
                    if progression["is_no_progress"]:
                        called_syms = _ff18_extract_test_symbols(failed_res, msgs, ev)
                        sym_str = ", ".join(f"`{s}`" for s in sorted(called_syms)[:8]) if called_syms else "the called functions"
                        fn_str = ", ".join(f"`{n}`" for n in progression["failed_names"][:5])
                        msg += (
                            f"\n\n[DIAGNOSTIC NOTICE - REPEATED FAILURE]\n"
                            f"WARNING: The test output is IDENTICAL to the previous run ({fn_str}). "
                            f"Your last edit had NO effect on the failure. Do NOT repeat the same edit. "
                            f"Check the implementation of {sym_str} in the source file vs how the test inspects them."
                        )
                    return Step(
                        pat, "act", max(3, total - 2), total, {"act"},
                        msg + _DO_NOT_DESCRIBE + extra,
                        None, False, tm.names({"act"})
                    )
                else:
                    st = _answer_step("CHANGE", ev, total, total, final=True)
                    if st:
                        st.pattern = pat
                        if "_ff17_attach_audit_note_to_answer" in globals():
                            st = _ff17_attach_audit_note_to_answer(st, msgs, ev, tested_after=True)
                        if _ff18_c("flow18_honest_final", True) and progression["failed_names"]:
                            fn_str = ", ".join(progression["failed_names"][:6])
                            st.instruction = (st.instruction or "") + (
                                f"\n\nCRITICAL: The following unit tests STILL FAIL: {fn_str}. "
                                f"You MUST NOT claim that the task is completed or that all tests pass. "
                                f"State clearly that the repair budget has been exhausted, list the failing tests ({fn_str}), "
                                f"and explain what was implemented versus what remains unresolved."
                            )
                    return st
    except Exception as ex:
        log(f"[fix18] change_tail repair loop error: {ex}")

    return _change_tail_s5(pat, ctx, ev, tm, names_all, total, extra=extra)




def _ff5_after_plan_s4(step, messages, tm, ctx=None):
    step = _ff5_after_plan_s3(step, messages, tm, ctx)
    if not _ff18_c("flow18_enabled", True):
        return step
    try:
        if (step is not None and getattr(step, "name", None) == "act"
                and getattr(step, "local_text", None) is None
                and not getattr(step, "drive", False)):
            ev = _answered_calls(messages or [])
            runs = _test_runs(ev)
            last_act = max((i for i, e in enumerate(ev) if e.get("kind") == "act"), default=-1)
            after = [r for r in runs if r[0] > last_act]
            last_test_res = after[-1][2] if after else (runs[-1][2] if runs else "")
            if test_outcome(last_test_res) in ("fail", "broken"):
                progression = _ff18_analyze_failure_progression(ev)
                if progression["is_no_progress"]:
                    called_syms = _ff18_extract_test_symbols(last_test_res, messages, ev)
                    diag_pack = _ff18_diagnostic_context(called_syms, messages, ev)
                    if diag_pack and "[Diagnostic Source Windows" not in (step.instruction or ""):
                        step.instruction = (step.instruction or "") + "\n\n" + diag_pack
    except Exception as e:
        log(f"[fix18] after_plan diagnostic context error: {e}")
    return step



_VI_FALSE_COMPLETION = re.compile(
    r"(?i)\b(?:đã\s+(?:được\s+)?hoàn\s+(?:tất|thành)|hoàn\s+thành\s+(?:100%|toàn\s+bộ|mọi\s+bước)"
    r"|triển\s+khai\s+thành\s+công|tất\s+cả\s+(?:các\s+)?test\s+(?:đều\s+)?pass)\b"
)
_EN_FALSE_COMPLETION = re.compile(
    r"(?i)\b(?:all\s+tests?\s+(?:have\s+)?pass(?:ed)?|successfully\s+(?:implemented|completed)"
    r"|implementation\s+is\s+complete|everything\s+is\s+(?:working|done|fixed))\b"
)
_HONEST_KEYWORDS = re.compile(
    r"(?i)\b(?:fail(?:ed|ing|ure|s)?|chưa\s+pass|chưa\s+hoàn\s+thành|lỗi|error"
    r"|AssertionError|tuy\s+nhiên|however|unresolved|budget\s+exhausted|còn\s+vướng)\b"
)


def _ff5_postfilter(result, messages, perm=None, last=True, tm=None, ctx=None):
    res, nudge = _ff5_postfilter_s3(result, messages, perm, last, tm, ctx)
    if nudge is not None or not _ff18_c("flow18_honest_final", True):
        return res, nudge
    if not (isinstance(res, tuple) and len(res) == 2):
        return res, nudge
    text, calls = res
    if calls or not text:
        return res, nudge

    try:
        ev = _answered_calls(messages or [])
        runs = _test_runs(ev)
        if not runs:
            return res, nudge
        last_test = runs[-1]
        if test_outcome(last_test[2]) not in ("fail", "broken"):
            return res, nudge

        failed_names = _ff18_failed_test_names(last_test[2])
        fn_str = ", ".join(failed_names[:6]) if failed_names else "unit tests"

        is_false_claim = (
            (_VI_FALSE_COMPLETION.search(text) or _EN_FALSE_COMPLETION.search(text))
            and not _HONEST_KEYWORDS.search(text)
        )
        if is_false_claim:
            log(f"[fix18] honest_final detected false completion claim while tests fail: {fn_str}")
            if not last:
                nudge_msg = (
                    f"CRITICAL REQUIREMENT: Unit tests ({fn_str}) still FAIL. "
                    f"Do NOT claim the task is completed or that all tests pass. "
                    f"Plainly report what was implemented, that tests still fail ({fn_str}), and why."
                )
                return None, nudge_msg
            else:
                last_user = _ff5_last_user_text(messages)
                vi = bool(_VI_RE.search(last_user.lower())) or bool(_VI_RE.search(text.lower()))
                if vi:
                    notice = f"⚠️ [Lưu ý: Quá trình tự sửa lỗi đã dừng và vẫn còn test chưa pass: {fn_str}]\n\n"
                else:
                    notice = f"⚠️ [Note: Self-healing budget reached and tests are still failing: {fn_str}]\n\n"
                return (notice + text, calls), None
    except Exception as ex:
        log(f"[fix18] honest_final postfilter error: {ex}")

    return res, nudge


def _setup_ff18():
    for k, v in _FF18_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    log("[fix18] flow_fix18 installed")
# ==== flowfix18:end ====

# ==== flowfix19:start ====
# Flow Engine 19: corrected post-test repair convergence.
#   1. _excerpt + fail-fingerprint keep swallowed handler errors
#      ("GET error: ... takes 1 positional argument but 2 were given") that
#      the old case-sensitive regex dropped -> diagnosis is no longer blind.
#   2. Anti-oscillation: returning to an already-seen fingerprint is NOT
#      progress and earns no bonus round.
#   3. force-read has an exit condition (one read per act) and
#      flow19_stop_after (5) > flow19_force_read_after (3).
#   4. NEW: AST call-signature check on production edits -- catches
#      self._needs_auth() missing the required `path` BEFORE the client runs it.
#   5. Override signatures resolved via ast (last definition wins), scoped to
#      non-test helper classes. No proxy-log scraping (tests run elsewhere).



def _ff19_c(key, default=None):
    if not _flow_c("flow19_enabled", True):
        return False
    return _flow_c(key, _FF19_DEFAULTS.get(key, default))


# --------------------------------------------------------------------------
# 1a. _excerpt: keep swallowed handler-error lines (case-insensitive)
# --------------------------------------------------------------------------
_FF19_EXC_BASE = re.compile(
    r"Traceback|^FAIL|^ERROR|AssertionError|Error:|\bfailed\b|not ok")
_FF19_EXC_EXTRA = re.compile(
    r"\b(?:GET|POST)\s+error\b|takes \d+ positional argument"
    r"|unexpected keyword argument|missing \d+ required", re.IGNORECASE)


def _excerpt(text, limit=1800):
    if not _ff19_c("flow19_enabled"):
        return _excerpt_s1(text, limit)
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if _FF19_EXC_BASE.search(ln) or _FF19_EXC_EXTRA.search(ln):
            lines = lines[max(0, i - 2):]
            break
    return "\n".join(lines).strip()[:limit]


# --------------------------------------------------------------------------
# 1b. fail fingerprint: include the swallowed-error lines too
# --------------------------------------------------------------------------


def _ff18_fail_fingerprint(res):
    if not _ff19_c("flow19_enabled"):
        return _ff18_fail_fingerprint_s1(res)
    if not res:
        return ""
    failed_names = _ff18_failed_test_names(res)
    norm = res
    norm = re.sub(r"Ran \d+ tests in [\d.]+s", "", norm)
    norm = re.sub(r"line \d+", "line X", norm)
    norm = re.sub(r":\d+:", ":X:", norm)
    norm = re.sub(r"0x[0-9a-fA-F]+", "0xADDR", norm)
    norm = re.sub(r'File "[^"\n]+"', 'File "<path>"', norm)
    norm = re.sub(r"File '[^'\n]+'", "File '<path>'", norm)
    norm = re.sub(r"(?i)(?:[a-z]:[\\/][^\s\"']*[\\/]temp[\\/]|/tmp/)tmp[a-z0-9_]+",
                  "<tmp>", norm)
    lines = [ln.strip() for ln in norm.splitlines()
             if re.search(r"(?:FAIL|ERROR|FAILED|AssertionError|Exception|Error:)", ln)
             or _FF19_EXC_EXTRA.search(ln)]
    key_str = "|".join(failed_names) + "\n" + "\n".join(lines)
    return _ff17_hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 2. anti-oscillation: recompute progress with a seen-fingerprint set
# --------------------------------------------------------------------------


def _ff18_analyze_failure_progression(ev):
    res = _ff18_analyze_failure_progression_s1(ev)
    if not _ff19_c("flow19_enabled") or not _ff19_c("flow19_anti_oscillation"):
        return res
    try:
        failed_runs = [r for r in _test_runs(ev)
                       if test_outcome(r[2]) in ("fail", "broken")]
        if len(failed_runs) < 2:
            return res
        fps = [_ff18_fail_fingerprint(r[2]) for r in failed_runs]
        seen = {fps[0]}
        progress = 0
        for j in range(1, len(failed_runs)):
            prev_idx, cur_idx = failed_runs[j - 1][0], failed_runs[j][0]
            had_acts = any(ev[k].get("kind") == "act"
                           for k in range(prev_idx + 1, cur_idx))
            # a fingerprint we have seen before (oscillation) is NOT progress
            if had_acts and fps[j] != fps[j - 1] and fps[j] not in seen:
                progress += 1
            seen.add(fps[j])
        bonus_cap = int(_ff18_c("flow18_progress_bonus_rounds", 2) or 2)
        res["progress_count"] = progress
        res["bonus_rounds"] = min(bonus_cap, progress)
    except Exception as e:
        log("[fix19] anti-oscillation error: %s" % e)
    return res


# --------------------------------------------------------------------------
# streak helpers (fp streak drives stop; name streak drives force-read)
# --------------------------------------------------------------------------
def _ff19_streak(ev, key_fn):
    failed = [r for r in _test_runs(ev)
              if test_outcome(r[2]) in ("fail", "broken")]
    if len(failed) < 2:
        return 0
    keys = [key_fn(r[2]) for r in failed]
    streak = 1
    for j in range(len(failed) - 1, 0, -1):
        prev_idx, cur_idx = failed[j - 1][0], failed[j][0]
        had_acts = any(e.get("kind") == "act"
                       for e in ev[prev_idx + 1:cur_idx])
        if had_acts and keys[j] == keys[j - 1] and keys[j]:
            streak += 1
        else:
            break
    return streak


def _ff19_fp_streak(ev):
    return _ff19_streak(ev, _ff18_fail_fingerprint)


def _ff19_names_streak(ev):
    return _ff19_streak(ev, lambda r: frozenset(_ff18_failed_test_names(r)))


# --------------------------------------------------------------------------
# tracked sources / noise filter (shared by read-targets + override sigs)
# --------------------------------------------------------------------------
def _ff19_tracked_sources(messages):
    try:
        tracked = dict(_ff9_read_texts(messages or []))
    except Exception:
        tracked = {}
    try:
        for v in _ff8_file_views(_answered_calls(messages or [])):
            p = v.get("path") or ""
            if p and p not in tracked:
                tracked[p] = v.get("text") or ""
    except Exception:
        pass
    return tracked


def _ff19_is_noise(p):
    try:
        return _is_test_path(p) or _is_doc(p) or _ff16_is_plan_doc(p)
    except Exception:
        return False


def _ff19_read_targets(ev, messages, failed_res):
    syms = set()
    try:
        syms |= _ff18_extract_test_symbols(failed_res, messages, ev)
    except Exception:
        pass
    try:
        syms |= _ff19_override_symbols(messages)
    except Exception:
        pass
    targets = []
    for fpath, src in _ff19_tracked_sources(messages).items():
        if _ff19_is_noise(fpath) or not src:
            continue
        for s in syms:
            if re.search(r"(?:def|class)\s+" + re.escape(s) + r"\b", src):
                targets.append(fpath)
                break
    return targets


# --------------------------------------------------------------------------
# 5. override symbols + signatures via ast (scoped, last-definition-wins)
# --------------------------------------------------------------------------
def _ff19_override_symbols(messages):
    out = set()
    if not _ff19_c("flow19_override_sigs"):
        return out
    for fpath, src in _ff19_tracked_sources(messages).items():
        if not _is_test_path(fpath):
            continue
        try:
            tree = _ff5_ast.parse(src)
        except Exception:
            continue
        for node in _ff5_ast.walk(tree):
            # helper/double classes only: skip the TestCase class itself
            if isinstance(node, _ff5_ast.ClassDef) and \
                    not node.name.lower().startswith("test"):
                for item in node.body:
                    if isinstance(item, (_ff5_ast.FunctionDef,
                                         _ff5_ast.AsyncFunctionDef)):
                        nm = item.name
                        if nm.startswith("test") or nm.startswith("__") or \
                                nm in ("setUp", "tearDown",
                                       "setUpClass", "tearDownClass"):
                            continue
                        out.add(nm)
            # handler.foo.return_value = ... -> mocked method `foo`
            if isinstance(node, _ff5_ast.Attribute) and \
                    node.attr in ("return_value", "side_effect") and \
                    isinstance(node.value, _ff5_ast.Attribute):
                out.add(node.value.attr)
    return out


def _ff19_format_sig(name, node):
    a = node.args
    parts = []
    n, ndef = len(a.args), len(a.defaults)
    for i, arg in enumerate(a.args):
        s = arg.arg
        di = i - (n - ndef)
        if 0 <= di < len(a.defaults):
            try:
                s += "=" + _ff5_ast.unparse(a.defaults[di])
            except Exception:
                s += "=?"
        parts.append(s)
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    for i, arg in enumerate(a.kwonlyargs):
        s = arg.arg
        if i < len(a.kw_defaults) and a.kw_defaults[i] is not None:
            try:
                s += "=" + _ff5_ast.unparse(a.kw_defaults[i])
            except Exception:
                s += "=?"
        parts.append(s)
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return "def %s(%s)" % (name, ", ".join(parts))


def _ff19_signature_windows(symbols, messages, limit=30):
    if not symbols:
        return ""
    lines = []
    for fpath, src in _ff19_tracked_sources(messages).items():
        if _ff19_is_noise(fpath):
            continue
        try:
            tree = _ff5_ast.parse(src)
        except Exception:
            continue
        # last definition wins (file has many flowfix re-definitions)
        defs = {}
        for node in _ff5_ast.walk(tree):
            if isinstance(node, (_ff5_ast.FunctionDef,
                                 _ff5_ast.AsyncFunctionDef)) \
                    and node.name in symbols:
                ln = getattr(node, "lineno", 0)
                if node.name not in defs or ln > defs[node.name][0]:
                    defs[node.name] = (ln, node)
        for name, (ln, node) in defs.items():
            lines.append("%s:%d: %s" % (fpath, ln, _ff19_format_sig(name, node)))
            if len(lines) >= limit:
                break
    return "\n".join(lines[:limit])


# --------------------------------------------------------------------------
# 3. _change_tail: force-read (with exit) + late stop
# --------------------------------------------------------------------------


def _change_tail(pat, ctx, ev, tm, names_all, total, extra=""):
    if not _ff19_c("flow19_enabled"):
        return _change_tail_s6(pat, ctx, ev, tm, names_all, total, extra=extra)
    try:
        msgs = getattr(_ff8_tls, "messages", None)
        runs = _test_runs(ev)
        last_act = max((i for i, e in enumerate(ev)
                        if e.get("kind") == "act"), default=-1)
        after = [r for r in runs if r[0] > last_act]
        if after and tm.first("act"):
            failed_test = next((r for r in reversed(after)
                                if test_outcome(r[2]) in ("fail", "broken")), None)
            if failed_test is not None:
                failed_res = failed_test[2]
                fp_streak = _ff19_fp_streak(ev)
                names_streak = _ff19_names_streak(ev)
                stop_after = int(_ff19_c("flow19_stop_after", 5))
                force_read_after = int(_ff19_c("flow19_force_read_after", 3))

                # late stop: identical fingerprint many rounds in a row
                if fp_streak >= stop_after:
                    st = _answer_step("CHANGE", ev, total, total, final=True)
                    if st:
                        st.pattern = pat
                        if "_ff17_attach_audit_note_to_answer" in globals():
                            st = _ff17_attach_audit_note_to_answer(
                                st, msgs, ev, tested_after=True)
                        fnames = _ff18_failed_test_names(failed_res)
                        fn_str = ", ".join(fnames[:6]) or "unit tests"
                        st.instruction = (st.instruction or "") + (
                            "\n\nCRITICAL: Repair STOPPED EARLY after %d "
                            "consecutive rounds with an IDENTICAL error "
                            "signature. Tests STILL FAIL: %s. Do NOT claim "
                            "success." % (fp_streak, fn_str))
                        return st

                # force-read once per act (exit condition: a read after the
                # last act lifts the force until the next act)
                read_after_act = any(e.get("kind") == "read"
                                     for e in ev[last_act + 1:])
                if names_streak >= force_read_after and not read_after_act \
                        and tm.first("read"):
                    targets = _ff19_read_targets(ev, msgs, failed_res)
                    if targets:
                        return _read_step(
                            pat, tm, targets[:4], 2, total,
                            instruction=(
                                "Repair has made NO progress on the SAME "
                                "failing test for %d rounds. STOP guessing "
                                "and READ the source of the code under test "
                                "BEFORE editing again: %s."
                                % (names_streak, ", ".join(targets[:4]))))
    except Exception as ex:
        log("[fix19] change_tail pre-pass error: %s" % ex)
    return _change_tail_s6(pat, ctx, ev, tm, names_all, total, extra=extra)


# --------------------------------------------------------------------------
# 4. AST call-signature check on production edits (the high-value one)
# --------------------------------------------------------------------------
def _ff19_check_self_calls(path, post_src):
    """Conservative self.<method>() arity check.

    Goals:
    - catch obvious new bugs like self._needs_auth() when the method is
      def _needs_auth(self, path)
    - do NOT reject valid dynamic calls such as:
        self.send_api_error(*_map_upstream_error(e))
        self.foo(**kwargs)
    - resolve methods by the enclosing class, not one global name table.
    """
    problems = []
    try:
        tree = _ff5_ast.parse(post_src)
    except Exception:
        return problems

    def method_info(fn):
        if getattr(fn, "decorator_list", None):
            return None
        a = fn.args
        pos = list(getattr(a, "posonlyargs", ())) + list(a.args)
        if not pos or pos[0].arg not in ("self", "cls"):
            return None

        total_with_receiver = len(pos)
        required_with_receiver = max(0, total_with_receiver - len(a.defaults))
        req = max(0, required_with_receiver - 1)
        mx = max(0, total_with_receiver - 1)
        return req, mx, a.vararg is not None

    class Visitor(_ff5_ast.NodeVisitor):
        def __init__(self):
            self.class_stack = []

        def visit_ClassDef(self, node):
            methods = {}
            for item in node.body:
                if isinstance(item, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
                    info = method_info(item)
                    if info is not None:
                        # Last definition wins inside this class.
                        methods[item.name] = info
            self.class_stack.append((node.name, methods))
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_Call(self, node):
            try:
                if self.class_stack:
                    fn = node.func
                    if (isinstance(fn, _ff5_ast.Attribute)
                            and isinstance(fn.value, _ff5_ast.Name)
                            and fn.value.id == "self"):
                        # Dynamic shapes: do not guess.
                        if node.keywords:
                            self.generic_visit(node)
                            return
                        if any(isinstance(arg, _ff5_ast.Starred) for arg in node.args):
                            self.generic_visit(node)
                            return

                        name = fn.attr
                        methods = self.class_stack[-1][1]
                        if name in methods:
                            req, mx, has_var = methods[name]
                            given = len(node.args)
                            if has_var:
                                if given < req:
                                    problems.append(
                                        "%s: self.%s() needs at least %d positional "
                                        "arg(s) but the new code passes %d"
                                        % (path, name, req, given))
                            elif given < req or given > mx:
                                problems.append(
                                    "%s: self.%s() expects %d-%d positional "
                                    "arg(s) but the new code passes %d"
                                    % (path, name, req, mx, given))
            except Exception:
                pass
            self.generic_visit(node)

    Visitor().visit(tree)
    return problems




def _gate_act_calls(calls, messages):
    out, problems = _gate_act_calls_s2(calls, messages)
    if not _ff19_c("flow19_enabled") or not _ff19_c("flow19_call_sig_check"):
        return out, problems
    try:
        texts = _ff9_read_texts(messages)
        for tc in out:
            fn = (tc or {}).get("function") or {}
            if tool_kind(fn.get("name")) != "act":
                continue
            args = T._tool_arguments_dict(fn)
            path = _call_path(args)
            if not path or not path.lower().endswith(".py") or _is_test_path(path):
                continue
            nkey = next((k for k in _NEW_KEYS if isinstance(args.get(k), str)), None)
            if not nkey:
                continue
            new_code = args[nkey]
            okey = next((k for k in _OLD_KEYS
                         if isinstance(args.get(k), str) and args[k]), None)
            old_code = args.get(okey) if okey else ""
            tk = _ff17_find_key(texts, path) if "_ff17_find_key" in globals() else None
            full = texts.get(tk or path) or ""
            if not full:
                continue
            post = full.replace(old_code, new_code, 1) if old_code else full
            problems.extend(_ff19_check_self_calls(path, post))
    except Exception as ex:
        log("[fix19] call-sig gate error: %s" % ex)
    return out, problems


# --------------------------------------------------------------------------
# enrich guided act step with ast override signatures while a test fails
# --------------------------------------------------------------------------


def _ff5_after_plan(step, messages, tm, ctx=None):
    step = _ff5_after_plan_s4(step, messages, tm, ctx)
    if not _ff19_c("flow19_enabled") or not _ff19_c("flow19_override_sigs"):
        return step
    try:
        if step is not None and getattr(step, "name", None) == "act" \
                and getattr(step, "local_text", None) is None \
                and not getattr(step, "drive", False):
            ev = _answered_calls(messages or [])
            runs = _test_runs(ev)
            last_act = max((i for i, e in enumerate(ev)
                            if e.get("kind") == "act"), default=-1)
            after = [r for r in runs if r[0] > last_act]
            last_res = after[-1][2] if after else (runs[-1][2] if runs else "")
            if test_outcome(last_res) in ("fail", "broken"):
                sigs = _ff19_signature_windows(
                    _ff19_override_symbols(messages), messages)
                if sigs and "[fix19 override signatures]" not in (step.instruction or ""):
                    step.instruction = (step.instruction or "") + (
                        "\n\n[fix19 override signatures -- your test doubles "
                        "MUST match these exactly:]\n" + sigs)
    except Exception as e:
        log("[fix19] after_plan override-sig error: %s" % e)
    return step


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------
def _setup_ff19():
    for k, v in _FF19_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    log("[fix19] flow_fix19 installed (excerpt/fingerprint fix + "
        "anti-oscillation + force-read + AST call-sig gate)")

def install():
    """Install flow engine middleware and sync defaults. Idempotent."""
    from gemini_web2api import server
    cls = server.GeminiHandler
    cur = cls._generate_with_tool_retry
    if getattr(cur, "_flow_installed", False):
        return

    # Setup defaults and submodules in original sequential order
    for k, v in _DEFAULTS.items():
        CONFIG.setdefault(k, v)
    _fx_install(server)

    for k, v in _FLOW_DEFAULTS.items():
        CONFIG.setdefault(k, v)

    for k, v in _FX_DEFAULTS.items():
        CONFIG.setdefault(k, v)

    for k, v in _FF5_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    _ff5_wrap_change(server)
    _ff5_install_banner()
    _ff4_wrap_classifiers()
    _ff4_extend_canned()

    _setup_ff8()
    _setup_ff10()

    _setup_ff14()
    _setup_ff15()

    for k, v in _FF16_DEFAULTS.items():
        CONFIG.setdefault(k, v)

    _setup_ff17()
    _setup_ff18()
    _setup_ff19()

    # Middleware chain (inside-out)
    cur = _wrap_flow(cur)
    log("[flow] flow_patch installed")

    cur = _wrap_ff3(cur)
    log("[fix3] flow_fix3 installed")

    cur = _wrap_ff4(cur)
    log("[fix4] flow_fix4 installed")

    cur = _wrap_ff5(cur)
    log("[fix5] flow_fix5 installed")

    cur = _wrap_ff12(cur)
    log("[fix12] required-tool guard installed")

    cur = _wrap_ff16(cur)
    log("[fix16] flow_fix16 installed")

    cur._flow_installed = True
    cls._generate_with_tool_retry = cur
# ==== flowfix19:end ====

# ==============================================================================
# Semantic Names & Legacy Aliases (Phase 7)
# ==============================================================================
postfilter_output = _ff5_postfilter
get_flow_permissions = _ff5_permissions
get_flow_scope = _ff5_scope
detect_flow_pattern = _ff5_detect
filter_after_plan = _ff5_after_plan
check_evidence_gate = _ff5_evidence_gate
find_text_key = _ff17_find_key
extract_read_texts = _ff9_read_texts
build_todo_ledger = _ff16_todo_ledger
pack_act_context = _ff17_act_context_pack
attach_todowrite = _ff17_attach_todowrite
audit_plan_compliance = _ff17_audit_plan_compliance
evaluate_plan_steps = _ff17_evaluate_plan_steps
extract_req_tokens = _ff17_extract_req_tokens
remaining_plan_or_todo_work = _ff17_remaining_plan_or_todo_work
strip_numbered_lines = _ff17_strip_numbered_lines
analyze_failure_progression = _ff18_analyze_failure_progression
check_self_calls = _ff19_check_self_calls
collect_continuations = _ff4_continuations
is_unfinished_thought = _ff4_unfinished
is_ambiguous_intent = _ff10_ambiguous
rank_intent_candidates = _ff10_candidates
extract_todo_items = _ff16_extract_todo_items
extract_plan_files = _ff16_plan_files
format_own_result = _ff5_own_result
format_spec_banner = _ff5_spec_banner
format_flow_status = _ff5_status
format_flow_status_detail = _ff5_status_detail
check_read_guard = _ff5_read_guard
create_diff_step = _ff5_diff_step
is_file_in_scope = _ff5_in_scope
validate_scope_problems = _ff5_scope_problems
check_act_needs_read_problems = _ff5_act_needs_read_problems
is_cross_module_action = _ff5_cross_module
get_flow_cache = _ff5_cache
patch_gemini_calls = _ff8_patch_gemini
is_proven_absent = _ff8_proven_absent
filter_survey_listing = _ff6c_filter
orig_file_review = _ff10_orig_file_review
get_intent_cache = _ff10_cache
adapt_flow_calls = _ff5_adapt_calls
install_task_pattern_banner = _ff5_install_banner
filter_after_plan_step = _ff5_plan_after

_LEGACY_ALIASES = {
    "_ff5_postfilter": postfilter_output,
    "_ff5_cmd_readonly": is_readonly_command,
    "_ff5_permissions": get_flow_permissions,
    "_ff5_scope": get_flow_scope,
    "_ff5_detect": detect_flow_pattern,
    "_ff5_after_plan": filter_after_plan,
    "_ff5_evidence_gate": check_evidence_gate,
    "_ff17_find_key": find_text_key,
    "_ff9_read_texts": extract_read_texts,
    "_ff16_todo_ledger": build_todo_ledger,
    "_ff17_act_context_pack": pack_act_context,
    "_ff17_attach_todowrite": attach_todowrite,
    "_ff17_audit_plan_compliance": audit_plan_compliance,
    "_ff17_evaluate_plan_steps": evaluate_plan_steps,
    "_ff17_extract_req_tokens": extract_req_tokens,
    "_ff17_extract_verification_commands": extract_verification_commands,
    "_ff17_remaining_plan_or_todo_work": remaining_plan_or_todo_work,
    "_ff17_strip_numbered_lines": strip_numbered_lines,
    "_ff18_analyze_failure_progression": analyze_failure_progression,
    "_ff19_check_self_calls": check_self_calls,
    "_ff4_continuations": collect_continuations,
    "_ff4_unfinished": is_unfinished_thought,
    "_ff10_ambiguous": is_ambiguous_intent,
    "_ff10_candidates": rank_intent_candidates,
    "_ff16_extract_todo_items": extract_todo_items,
    "_ff16_plan_files": extract_plan_files,
    "_ff5_own_result": format_own_result,
    "_ff5_spec_banner": format_spec_banner,
    "_ff5_status": format_flow_status,
    "_ff5_status_detail": format_flow_status_detail,
    "_ff5_read_guard": check_read_guard,
    "_ff5_diff_step": create_diff_step,
    "_ff5_in_scope": is_file_in_scope,
    "_ff5_scope_problems": validate_scope_problems,
    "_ff5_act_needs_read_problems": check_act_needs_read_problems,
    "_ff5_cross_module": is_cross_module_action,
    "_ff5_cache": get_flow_cache,
    "_ff8_patch_gemini": patch_gemini_calls,
    "_ff8_proven_absent": is_proven_absent,
    "_ff6c_filter": filter_survey_listing,
    "_ff10_orig_file_review": orig_file_review,
    "_ff10_cache": get_intent_cache,
    "_ff5_adapt_calls": adapt_flow_calls,
    "_ff5_install_banner": install_task_pattern_banner,
    "_ff5_plan_after": filter_after_plan_step,
}

if __name__ == "__main__":
    main()