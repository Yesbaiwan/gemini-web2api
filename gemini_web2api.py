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
}


# The (family, variant) each ticket key is expected to embed at ticket
# indexes 14/15. When a configured ticket carries anything else the upstream
# silently serves a different model (today: flash-thinking runs Flash-Lite
# Extended), so startup warns instead of mis-routing quietly. Tickets cannot
# be re-captured programmatically -- the owner refreshes them from a fresh
# browser session.
EXPECTED_TICKET_ROUTES = {
    "flash": (1, 1),
    "flash-thinking": (1, 2),
    "lite": (6, 1),
    "lite-thinking": (6, 2),
    "pro": (3, 1),
    "pro-thinking": (3, 2),
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


def ticket_for(model_name: str):
    """Return the upstream ticket header value for a model, or None.

    Tickets live in ``CONFIG["model_tickets"]`` because they are minted by a
    real browser session and expire: an absent or stale key degrades to the
    body fields rather than erroring, and ``gemini.check_routing`` warns when
    the upstream then answers with a different model.
    """
    from gemini_web2api.config import CONFIG
    cfg = MODELS.get(model_name) or {}
    key = cfg.get("ticket")
    if not key:
        return None
    return (CONFIG.get("model_tickets") or {}).get(key)


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


def looks_like_extension_refusal(text: str) -> bool:
    """True for any length of Gemini's Workspace/Gmail extension refusal.

    Verbose models pad the canned refusal ("I can't use Google Drive because
    required Gmail settings are off...") with prose, which escapes the anchored
    :data:`_UPSTREAM_ERROR_RE` and would be handed to the client as a finished
    reply. Deliberately NOT folded into looks_like_upstream_error(): that one
    also drives generic retries, and matching loose refusals there would retry
    genuine answers like "you can't use that tool here".
    """
    if not text or len(text) > 800:
        return False
    folded = _fold_diacritics(text).lower()
    return bool(re.search(
        r"can'?t use [a-z][a-z .'\-]{0,40}? because required .{0,60}?"
        r"(settings|permissions) are off",
        folded))


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
    ``looks_like_extension_refusal`` caps at 800 chars, so a verbose model that
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


def _is_mutating_call(name, arguments) -> bool:
    """True when a completed tool call changed the world earlier results describe.

    ``edit``/``write``/``apply_patch``-style names always mutate. Command
    runners (``bash``/``shell``/``exec``/``run``) are verification when their
    arguments hold a test command -- pytest/unittest/npm test output does not
    change files, so a green re-run must not wipe the edit's repeat-guard
    entry. Any other command (rm, sed -i, git commit, ...) still counts as
    mutating.
    """
    if not isinstance(name, str) or not _MUTATING_TOOL_RE.search(name):
        return False
    if isinstance(arguments, dict):
        blob = json.dumps(arguments, ensure_ascii=False)
    elif isinstance(arguments, str):
        blob = arguments
    else:
        blob = ""
    return not (blob and _TEST_CMD_RE.search(blob))


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
    r"\b(?:review|audit|inspect|examine|check|find|kiem\s+tra|soat|tim|xem\s+xet|doc|danh\s+gia|danhgia)\b",
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


def _is_source_review(folded: str) -> bool:
    """True when folded text asks for a review of source/code."""
    if _SOURCE_STRONG_VERB_RE.search(folded) and _SOURCE_NOUN_RE.search(folded):
        return True
    return bool(_SOURCE_REVIEW_VERB_RE.search(folded)
                and _SOURCE_CODE_NOUN_RE.search(folded))


def _is_file_review(folded: str, scannable: str) -> bool:
    """True when folded text aims a review verb at a named file.

    "Tiep tuc review server.py di." carries no source noun, so
    :func:`_is_source_review` misses it -- yet answering it from a glob
    listing alone is the same fabrication. A review verb plus a file-shaped
    token counts as a review of that file ("review file" with no file named
    stays silent; runtime names like ``Node.js`` never count as files).
    """
    if not _SOURCE_REVIEW_VERB_RE.search(folded):
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


def review_demanded(messages) -> bool:
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
            ext = token.rsplit(".", 1)[-1].lower() if "." in token else ""
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


def _created_files(scannable: str, folded: str) -> set:
    """File tokens exempt from the `uncovered` check (creation intent)."""
    if not _FILE_CREATE_RE.search(folded):
        return set()
    return {f.lower() for f in _files_in(scannable)}


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


def _is_change_request(folded: str, scannable: str, raw: str = "") -> bool:
    """True when folded text asks for a code change (fix/implement/update).

    ``folded``/``scannable`` come from :func:`_strip_code_spans`, which erases
    inline code, so ``raw`` (the untouched user text) is scanned separately:
    "fix the ``retry_delay`` handling" names its target in backticks and is
    a change request even though no prose noun or file token survives the
    strip.
    """
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
            if len(s) >= 25:
                out.append(s)
    rest = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    for m in re.finditer(r"`([^`\n]{25,})`", rest):
        out.append(m.group(1).strip())
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
    corpus = _norm_code("\n".join(parts))
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
import threading

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


def log(msg: str):
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


def load_cookie() -> tuple:
    """Load cookie from file with mtime-based caching."""
    global _cookie_cache, _cookie_last_mtime_check
    cookie_file = CONFIG.get("cookie_file")
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    cached_str, cached_sapisid, cached_mtime = _cookie_cache
    try:
        # Throttle mtime checks to avoid redundant I/O on filesystems with
        # coarse mtime granularity (e.g., FAT, some network mounts).
        now = time.time()
        if now - _cookie_last_mtime_check < _COOKIE_MTIME_MIN_INTERVAL:
            # Return cached value without hitting disk.
            if cached_str:
                return cached_str, cached_sapisid
        _cookie_last_mtime_check = now
        mtime = os.path.getmtime(cookie_file)
        if mtime == cached_mtime and cached_str:
            return cached_str, cached_sapisid
        # Serialize file reads and associated CONFIG updates to avoid
        # redundant I/O, partial reads, and interleaved config mutations
        # under concurrent requests.
        with _cookie_file_lock:
            # Re-check mtime after acquiring lock (another thread may have updated).
            mtime = os.path.getmtime(cookie_file)
            if mtime == cached_mtime and cached_str:
                return cached_str, cached_sapisid
            with open(cookie_file, "r") as f:
                content = f.read().strip()
            if content.startswith("{"):
                data = json.loads(content)
                cookie_str = data.get("cookie", "")
                sapisid = data.get("sapisid", "")
                # The bundled extension exports {cookie, sapisid, auth_user,
                # xsrf_token, gemini_bl} in one file. Ignore the extra fields and
                # StreamGenerate goes out without its `at` form field (HTTP 400)
                # and with whatever stale build label happens to be configured,
                # so sync them here: pointing cookie_file at the export must be
                # enough on its own.
                with _config_lock_ctx():
                    if data.get("xsrf_token"):
                        CONFIG["xsrf_token"] = data["xsrf_token"]
                    if data.get("auth_user") not in (None, ""):
                        CONFIG["auth_user"] = data["auth_user"]
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
                pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
                sapisid = pairs.get("SAPISID", "")
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


def _build_headers(ticket: str = None) -> dict:
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
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    if ticket:
        # The per-family ticket is what the upstream actually routes on; the
        # f.req [79]/[80] fields are advisory without it.
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


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
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


def check_routing(raw: str, model_id: int, extra_fields: dict = None, ticket: str = None) -> None:
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


def _status_error(status) -> GeminiError:
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
from gemini_web2api.gemini import load_cookie, make_sapisidhash, _get_ssl_ctx, log


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


def _resolve_public_ip(hostname: str) -> str | None:
    """Resolve hostname and return a single public IP, or None if blocked.

    This resolves DNS *once* and validates all addresses are global.
    The caller must then connect directly to this IP to prevent
    DNS-rebinding / TOCTOU attacks.
    """
    if not hostname:
        return None
    host = hostname.strip().rstrip(".").strip("[]")
    # Literal IP: check directly.
    try:
        return host if ipaddress.ip_address(host).is_global else None
    except ValueError:
        pass
    # Hostname: resolve and validate all addresses.
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return None
    if not infos:
        return None
    public_ip = None
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
            if not addr.is_global:
                return None  # Any non-global address blocks the whole host.
            public_ip = str(addr)  # Remember first global address.
        except ValueError:
            return None
    return public_ip


def _host_is_private(hostname: str) -> bool:
    """True when ``hostname`` resolves only to non-public IP addresses.

    Delegates to ``_resolve_public_ip`` for the actual check.
    """
    return _resolve_public_ip(hostname) is None


def fetch_image_bytes(url: str) -> bytes:
    """Fetch image from URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        log(f"Image fetch skipped for unsupported URL scheme: {parsed.scheme or 'none'}")
        return b""
    if _host_is_private(parsed.hostname or ""):
        log(f"Image fetch blocked for non-public target: {parsed.hostname or 'none'}")
        return b""
    # Resolve IP once and connect directly to prevent DNS-rebinding / TOCTOU.
    target_ip = _resolve_public_ip(parsed.hostname or "")
    if not target_ip:
        log(f"Image fetch blocked: could not resolve public IP for {parsed.hostname}")
        return b""
    try:
        # Reconstruct URL with resolved IP, keeping original Host header.
        # This avoids fragile string replacement on the full URL.
        netloc = f"{target_ip}:{parsed.port}" if parsed.port else target_ip
        reconstructed = parsed._replace(netloc=netloc).geturl()
        req = urllib.request.Request(
            reconstructed,
            headers={"User-Agent": "Mozilla/5.0", "Host": parsed.hostname},
            method="GET",
        )
        proxy = CONFIG.get("proxy")
        if proxy:
            # With proxy, we can't pin the IP; rely on proxy's DNS.
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
            )
            with opener.open(req, timeout=30) as resp:
                data = resp.read(_MAX_IMAGE_BYTES + 1)
        else:
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
            )
            with opener.open(req, timeout=30) as resp:
                data = resp.read(_MAX_IMAGE_BYTES + 1)
        if len(data) > _MAX_IMAGE_BYTES:
            log(f"Image fetch failed: body exceeds {_MAX_IMAGE_BYTES} bytes")
            return b""
        return data
    except Exception as e:
        log(f"Image fetch failed: {e}")
        return b""

# ==== ssrf5:begin (generated by flow_fix5_patch.py -- re-run to refresh, --revert to remove) ====
import http.client as _hc
import urllib.error as _uerr

# fetch_image_bytes used to validate only the FIRST url and then let urllib
# follow redirects on its own: a public host answering 302 -> http://127.0.0.1/...
# was fetched, and the body could then be uploaded to Gemini. Now every hop is
# validated, redirects are followed by hand, and the connection is pinned to
# the validated IP (SNI + certificate check still use the real host name).
_MAX_REDIRECTS = 5


def _resolve_public_ip(hostname):
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

from gemini_web2api.gemini import log, generate

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


def tool_kind(name) -> str:
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


# --------------------------------------------------------------------------
# Bằng chứng trong lượt hiện tại (kể từ message user cuối)
# --------------------------------------------------------------------------

def _answered_calls(messages):
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
_TEST_CMD_RE = re.compile(
    r"\b(unittest|pytest|npm\s+(?:run\s+)?test|yarn\s+test|make\s+test|"
    r"go\s+test|cargo\s+test)\b")
_SURVEY_CMD = ('git ls-files 2>/dev/null || find . -type f -not -path '
               '"./.git/*" -not -path "*/node_modules/*" | head -300')


_PATH_LINE_RE = re.compile(
    r'^\s*(?:[-*]\s+)?((?:[A-Za-z]:)?[/\\]?(?:[^\s:*?"<>|/\\]+[/\\])*'
    r'[^\s:*?"<>|/\\]+\.[A-Za-z0-9]{1,6})\s*$')
_READ_FAIL_RE = re.compile(
    r"^\s*(?:<?error|file not found|no such file|enoent|cannot (?:read|find|open)"
    r"|failed|not found)", re.IGNORECASE)


def _read_ok(e):
    """Tool result của một lệnh read có thật sự chứa nội dung không."""
    return bool(e["result"].strip()) and not _READ_FAIL_RE.match(e["result"])


def _listing_files(ev):
    """File paths từ kết quả survey/search. Giữ nguyên ổ đĩa (D:/...) và
    đường dẫn tuyệt đối: cắt mất chúng làm mọi lệnh read sau đó 'not found'."""
    seen, out = set(), []

    def add(f):
        f = f.replace("\\", "/")
        if f not in seen and f.lower() not in T._NON_FILE_NAMES:
            seen.add(f)
            out.append(f)

    for e in ev:
        if not (e["kind"] in ("survey", "search") or (
                e["kind"] == "exec"
                and _LIST_CMD_RE.search(_call_command(e["args"])))):
            continue
        matched = [m.group(1) for m in map(_PATH_LINE_RE.match,
                                           e["result"].splitlines()) if m]
        if matched:
            for f in matched:
                add(f)
        else:  # grep-style "path:line: text" output
            body = e["result"].replace("\\", "/")
            for f in sorted(T._files_in(body), key=body.find):
                add(f)
    return out


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


def _base_test_command(listing):
    """Lệnh test: config > autodetect theo listing > None (bỏ qua bước test)."""
    configured = _cfg("flow_test_command")
    if configured:
        return configured
    names = [f.replace("\\", "/").lower() for f in listing]
    if any(n.endswith("package.json") for n in names) or any(
            n.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")) for n in names):
        return "npm test"
    if any(n.endswith(".go") for n in names):
        return "go test ./..."
    if any(n.endswith(".rs") for n in names):
        return "cargo test"
    if any(n.endswith(".py") for n in names):
        return "python -m unittest discover -s tests"
    return None


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


def _base_keywords(text, limit=6):
    out = []
    for m in re.finditer(r"`([^`\n]{2,60})`", text or ""):
        k = re.sub(r"[()\s]", "", m.group(1))
        if k and k.lower() not in out:
            out.append(k.lower())
    folded = T._fold_diacritics(text or "")
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", folded):
        lw = w.lower()
        if lw not in _STOP and lw not in out:
            out.append(lw)
    return out[:limit]


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


def _act_ok(e):
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


def _clean_file_text(text):
    lines = text.replace("\r\n", "\n").split("\n")
    if T._already_numbered(lines):
        lines = [_LINENO_RE.sub("", ln, count=1) for ln in lines]
    return "\n".join(lines)


def _base_gate_act_calls(calls, messages):
    # Check edit calls against the file text the model actually read.
    # Returns (calls, problems). Line-number prefixes copied into
    # oldString/newString are stripped silently; a genuine mismatch is
    # reported (with the closest real line) so the caller can retry.
    texts = {}
    for e in _answered_calls(messages):
        if e["kind"] == "read" and _read_ok(e):
            p = _call_path(e["args"])
            if p:
                texts[p] = _clean_file_text(e["result"])
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
            out.append(tc)           # write/new file/unread file: nothing to check
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
            out.append("Allowed tools this turn: " + ", ".join(self.tool_names)
                       + ". Output ONLY tool_call block(s), no prose.")
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


def _base_is_code_question(text):
    scannable = T._strip_code_spans(text)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable.lower())).strip()
    if not _QUESTION_RE.search(folded):
        return False
    has_ident = bool(re.search(r"`[^`\n]{2,}`", text or ""))
    has_file = any(f for f in T._files_in(scannable)
                   if f.lower() not in T._NON_FILE_NAMES)
    return has_ident or has_file or bool(_PROJECT_RE.search(folded))


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
    ctx.scannable = T._strip_code_spans(ctx.request)
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


def clarification_text(messages):
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
                        "(no line-number prefixes)."
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

def _review_materials(messages):
    ev = _answered_calls(messages)
    files, seen = [], set()
    for e in ev:
        if e["kind"] == "read" and _read_ok(e):
            p = _call_path(e["args"])
            if p and p not in seen:
                seen.add(p)
                files.append((p, e["result"]))
    tests = [e for e in ev if e["kind"] == "exec"
             and _is_test_cmd(_call_command(e["args"]))]
    return files, tests


def _make_chunks(files, max_bytes, max_files):
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


def _verify_findings(text, chunk):
    """(kept, dropped): chỉ giữ finding có trích dẫn nằm thật trong chunk."""
    corpus = T._norm_code("\n".join(b for _p, b in chunk))
    kept, dropped = [], 0
    if re.fullmatch(r"\W*NONE\W*", (text or "").strip(), re.IGNORECASE):
        return kept, dropped
    for item in _split_findings(text):
        snippets = re.findall(r"`([^`\n]{8,})`", item)
        if snippets and all(T._norm_code(x) in corpus for x in snippets):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


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


def _map_reduce(messages, step, ctx, gen, client_gone=None):
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


def install():
    """Monkeypatch server.GeminiHandler. Idempotent."""
    from gemini_web2api import server
    for k, v in _DEFAULTS.items():
        CONFIG.setdefault(k, v)
    cls = server.GeminiHandler
    _fx_install(server)
    orig = cls._generate_with_tool_retry
    if getattr(orig, "_flow_patched", False):
        return

    @functools.wraps(orig)
    def wrapper(self, prompt, model_id, think_mode, file_refs, extra,
                tools_active, allowed_names, tool_schemas, required_tool,
                messages=None, ticket=None, tool_required=None,
                client_gone=None):
        def call(p, req=required_tool):
            return orig(self, p, model_id, think_mode, file_refs, extra,
                        tools_active, allowed_names, tool_schemas, req,
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
            return call((step_prompt or prompt) + "\n\n" + step.directive())
        if step.drive and _cfg("flow_drive"):
            return "", step.calls

        nudge, last = "", None
        for attempt in range(1 + max(0, int(_cfg("flow_max_retries")))):
            try:
                text, calls = call((step_prompt or prompt) + "\n\n"
                                   + step.directive() + nudge, True)
            except Exception as e:
                log(f"[flow] guide attempt {attempt + 1} failed: {e}")
                if step.calls:
                    return "", step.calls
                if attempt + 1 >= 1 + max(0, int(_cfg("flow_max_retries"))):
                    raise
                continue
            last = (text, calls)
            ok = [tc for tc in (calls or [])
                  if tool_kind((tc.get("function") or {}).get("name")) in step.kinds]
            if ok and step.name == "act":
                ok, problems = _gate_act_calls(ok, messages)
                if problems:
                    log("[flow] edit gate: " + " | ".join(problems))
                    if attempt < max(0, int(_cfg("flow_max_retries"))):
                        nudge = (" Your edit was rejected before sending: "
                                 + " | ".join(problems) + " Copy oldString "
                                 "EXACTLY from the file you read, without "
                                 "line-number prefixes.")
                        continue
            if ok:
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
    cls._generate_with_tool_retry = wrapper
    log("[flow] flow_patch installed")


# ==== fix_patch:begin (do fix_patch.py sinh ra; chạy lại script để cập nhật) ====
_FX_DEFAULTS = {
    "fix_enabled": True,
    "fix_debug_flow": True,
    "fix_create_flow": True,
    "fix_diff_review": True,
    "fix_read_docs_for_change": True,
    "fix_read_related_tests": True,
    "fix_max_test_attempts": 3,
    "fix_write_shrink_ratio": 0.5,
}
_DEFAULTS.update(_FX_DEFAULTS)


def _c(key):
    return CONFIG.get(key, _FX_DEFAULTS[key])


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


def pick_test_command(names, ev):
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


def _excerpt(text, limit=1800):
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


def local_imports(path, text, listing, scored=False):
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


def _read_texts(ev):
    return {_call_path(e["args"]): e["result"] for e in ev
            if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])}


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
    r"\b(?:implement|add|introduce|refactor|upgrade|migrate|rename|them|cap nhat)\b")
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
    r"|cap nhat)\b")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]*$")

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


def _existing(known, listing):
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


def _keywords(text, limit=6):
    """Từ khoá: identifier/backtick trước, từ thường (>=4 ký tự) sau."""
    strong, weak = [], []
    for m in re.finditer(r"`([^`\n]{2,60})`", text or ""):
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
    r"|hoat dong|la gi|tai sao|dinh nghia|tim hieu|mo ta|tom tat)\b")
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


def _analyze(messages):
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
            ctx.pattern = "CREATE"
            files = _named_files(ctx)
            ctx.target = files[0] if files else ""
            ctx.create_merge = bool(_MERGE_RE.search((ctx.folded or "").lower()))
            ctx.dir_cands = _dir_cands(ctx.last)
        elif not ctx.pattern:
            folded = (ctx.folded or "").lower()
            idents = [s for s in re.findall(r"`([^`\n]+)`", ctx.last or "")
                      if _IDENT_RE.match(s.strip())]
            if _CHG_V_RE.search(folded) and (idents or _named_files(ctx)):
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


def _change_tail(pat, ctx, ev, tm, names_all, total, extra=""):
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
                      "(no line-number prefixes)."
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


def _plan_change(ctx, ev, tm, pat="CHANGE", total=5):
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


def merge_script(srcs, out_path, ext):
    comment = "//" if ext in _SLASH_COMMENT_EXT else "#"
    return (_MERGE_TMPL.replace("@@SRC@@", json.dumps(list(srcs)))
            .replace("@@OUT@@", json.dumps(out_path))
            .replace("@@C@@", json.dumps(comment))
            .replace("@@PY@@", "True" if ext == ".py" else "False"))


def _merge_sources(ctx, listing):
    """(files, prefix): file cùng đuôi với target nằm trong thư mục user nêu."""
    target = _norm(getattr(ctx, "target", "") or "")
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    live = [f for f in _live(listing) if f.lower().endswith(ext)]
    tbase = posixpath.basename(target).lower()
    for cand in getattr(ctx, "dir_cands", []) or []:
        dl = _norm(cand).strip("/").split("/")[-1].lower()
        if not dl:
            continue
        groups = {}
        for f in live:
            segs = f.split("/")
            for i, s in enumerate(segs[:-1]):
                if s.lower() == dl:
                    groups.setdefault("/".join(segs[:i + 1]), []).append(f)
        if not groups:
            continue
        prefix = max(groups, key=lambda k: (
            sum(1 for f in groups[k] if posixpath.dirname(f) == k), len(k)))
        files = sorted({f for f in groups[prefix]
                        if not _is_test_path(f) and _base(f) != tbase
                        and _base(f) != _MERGE_SCRIPT})
        if len(files) >= 2:
            return files, prefix
    return [], ""


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


def _plan_create(ctx, ev, tm):
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


def _plan_debug(ctx, ev, tm):
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


def _plan_explain(ctx, ev, tm):
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

def _plan(messages, tm, ctx):
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


def plan_for(messages, tm, ctx=None):
    if not _c("fix_enabled"):
        return _base_plan_for(messages, tm, ctx)
    try:
        return _plan(messages, tm, ctx)
    except Exception as e:      # never let the planner break a request
        log(f"[fix] planner error, using the original planner: {e}")
        return _base_plan_for(messages, tm, ctx)


_CONTENT_KEYS = ("content", "contents", "text", "fileText", "file_text")


def _gate_act_calls(calls, messages):
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


def _fx_install(server):
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
_ff2_orig_gate_act_calls = _gate_act_calls
_ff2_orig_plan_create = _plan_create


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

def _merge_sources(ctx, listing):
    """``(files, prefix)`` to merge, or ``([], "")``.

    Candidate directories, in order: words the request marks as folders, EVERY
    word of the request that equals a directory in the listing, the target
    file's own stem, and finally the biggest Python package (a directory with
    ``__init__.py``). The old version needed the word "folder"/"thu muc".
    """
    target = _norm(getattr(ctx, "target", "") or "")
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    live = [f for f in _live(listing) if f.lower().endswith(ext)]
    tbase = posixpath.basename(target).lower()

    def usable(f):
        return (not _is_test_path(f) and _base(f) != tbase
                and _base(f) != _MERGE_SCRIPT)

    def group(dl):
        groups = {}
        for f in live:
            segs = f.split("/")
            for i, s in enumerate(segs[:-1]):
                if s.lower() == dl:
                    groups.setdefault("/".join(segs[:i + 1]), []).append(f)
        return groups

    def pick(groups):
        if not groups:
            return None
        prefix = max(groups, key=lambda k: (
            sum(1 for f in groups[k] if posixpath.dirname(f) == k), len(k)))
        files = sorted({f for f in groups[prefix] if usable(f)})
        return (files, prefix) if len(files) >= 2 else None

    cands = list(getattr(ctx, "dir_cands", []) or [])
    raw = getattr(ctx, "last", "") or ""
    for tok in re.findall(r"[A-Za-z0-9_.\-]+", raw):
        tok = tok.strip(".-")
        if len(tok) >= 3:
            cands.append(tok)
    stem_t = posixpath.splitext(posixpath.basename(target))[0]
    if stem_t:
        cands.append(stem_t)
    seen = set()
    for cand in cands:
        dl = _norm(cand).strip("/").split("/")[-1].lower()
        if not dl or dl in seen:
            continue
        seen.add(dl)
        hit = pick(group(dl))
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


def _plan_create(ctx, ev, tm):
    step = _ff2_orig_plan_create(ctx, ev, tm)
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

def _ff2_stub_problems(calls, messages):
    ctx = _analyze(messages)
    if (ctx is None or getattr(ctx, "pattern", None) != "CREATE"
            or not getattr(ctx, "create_merge", False)):
        return []
    total = 0
    for e in _answered_calls(messages):
        if e["kind"] != "read" or not _read_ok(e):
            continue
        p = _call_path(e["args"])
        if (p and not _is_doc(p) and not _is_test_path(p)
                and p.lower().endswith(_SRC_EXT)):
            total += len(_clean_file_text(e["result"]))
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


def _gate_act_calls(calls, messages):
    out, problems = _ff2_orig_gate_act_calls(calls, messages)
    try:
        problems = list(problems) + _ff2_stub_problems(out, messages)
    except Exception as e:
        log(f"[fix2] stub gate error: {e}")
    return out, problems
# ==== flowfix2:end ====


# ==== flowfix3:begin (generated by flow_fix3_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: every caller resolves these names at call time).
_ff3_prev_plan_create = _plan_create
_ff3_prev_install = install


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


def _plan_create(ctx, ev, tm):
    step = _ff3_prev_plan_create(ctx, ev, tm)
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
    total = 0
    for e in _answered_calls(messages):
        if e["kind"] != "read" or not _read_ok(e):
            continue
        p = _call_path(e["args"])
        if (p and not _is_doc(p) and not _is_test_path(p)
                and p.lower().endswith(_SRC_EXT)):
            total += len(_clean_file_text(e["result"]))
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


def install():
    _ff3_prev_install()
    from gemini_web2api import server
    cls = server.GeminiHandler
    cur = cls._generate_with_tool_retry
    if getattr(cur, "_flow3_patched", False):
        return

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
    cls._generate_with_tool_retry = wrapper3
    log("[fix3] flow_fix3 installed")
# ==== flowfix3:end ====


# ==== flowfix4:begin (generated by flow_fix4_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: every caller resolves these names at call time).
_ff4_prev_analyze = _analyze
_ff4_prev_plan_for = plan_for
_ff4_prev_map_reduce = _map_reduce
_ff4_prev_install = install


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


def _analyze(messages):
    ctx = _ff4_prev_analyze(messages)
    try:
        if (ctx is not None and _c("fix_enabled") and _c("fix_create_flow")
                and getattr(ctx, "pattern", None) in ("FILE_REVIEW", "GENERIC_REVIEW")
                and not _clarified(messages, ctx.idx)):
            folded = _ff4_scrub(ctx.folded or "")
            if (not _REVIEW_RE.search(folded.lower())
                    and _create_intent(folded, ctx.scannable, ctx.last)
                    and (_named_files(ctx)
                         or _MERGE_RE.search(folded.lower()))):
                files = _named_files(ctx)
                ctx.pattern = "CREATE"
                ctx.fix = False
                ctx.target = files[0] if files else ""
                ctx.create_merge = bool(_MERGE_RE.search(folded.lower()))
                ctx.dir_cands = _dir_cands(ctx.last)
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

_FF4_TRUNC_RE = re.compile(
    r"\[[^\]\n]*truncated[^\]\n]*?offset:?\s*(\d+)[^\]\n]*\]", re.IGNORECASE)
_FF4_MAX_CONT = 8


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
    """Truncations with no later *successful* read at or beyond their offset."""
    out = []
    for i, path, nxt in _ff4_truncations(ev):
        later = [_ff4_off(x["args"]) for x in ev[i + 1:]
                 if x["kind"] == "read" and _read_ok(x)
                 and T._path_seen(path, [_call_path(x["args"])])]
        if not any(o >= nxt for o in later):
            out.append((i, path, nxt))
    return out


def _ff4_continuations(ev, okey):
    """[(path, tool_call)] reading the rest of every truncated file."""
    if not okey:
        return []
    out, seen = [], set()
    for i, path, nxt in _ff4_unfinished(ev):
        if (path, nxt) in seen:
            continue
        seen.add((path, nxt))
        n_reads = sum(1 for x in ev
                      if x["kind"] == "read" and _call_path(x["args"]) == path)
        if n_reads > _FF4_MAX_CONT:
            continue
        args = dict(ev[i]["args"])
        args[okey] = nxt
        out.append((path, {
            "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": ev[i]["name"],
                         "arguments": json.dumps(args, ensure_ascii=False)}}))
    return out[:6]


def plan_for(messages, tm, ctx=None):
    step = _ff4_prev_plan_for(messages, tm, ctx)
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
            body = _FF4_TRUNC_RE.sub("", e["result"]).rstrip()
            if p in bodies:
                bodies[p] += "\n" + body
            else:
                bodies[p] = body
                order.append(p)
    tests = [e for e in ev if e["kind"] == "exec"
             and _is_test_cmd(_call_command(e["args"]))]
    return [(p, bodies[p]) for p in order], tests


def _map_reduce(messages, step, ctx, gen, client_gone=None):
    report = _ff4_prev_map_reduce(messages, step, ctx, gen, client_gone)
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


def install():
    _ff4_prev_install()
    _ff4_wrap_classifiers()
    _ff4_extend_canned()
    from gemini_web2api import server
    cls = server.GeminiHandler
    cur = cls._generate_with_tool_retry
    if getattr(cur, "_flow4_patched", False):
        return

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
    cls._generate_with_tool_retry = wrapper4
    log("[fix4] flow_fix4 installed")
# ==== flowfix4:end ====

# ==== flowfix5:begin (generated by flow_fix5_patch.py -- re-run to refresh, --revert to remove) ====
import hashlib as _ff5_hashlib
import threading as _ff5_threading
import time as _ff5_time
import ast as _ff5_ast

_ff5_prev_analyze = _analyze
_ff5_prev_install = install
_ff5_prev_plan_for = plan_for
_ff5_prev_map_reduce = _map_reduce
_ff5_prev_banner = getattr(T, "_task_pattern_banner_legacy",
                           T.task_pattern_banner)

_FF5_DEFAULTS = {
    "flow5_enabled": True,
    # No pattern matched -> ask the model to pick one from the catalog below.
    "flow5_classify": True,
    # Windows PowerShell 5.1 has no `&&`: rewrite it.
    "flow5_ps_fix": True,
    # Refuse wide destructive commands the user did not write themselves.
    "flow5_block_destructive": True,
    # "do not edit my files" is a hard constraint, not a hint.
    "flow5_respect_no_edit": True,
    # A write/edit that still fails the guards after the retries is dropped.
    "flow5_fail_closed_edits": True,
    # Targeted re-asks (with the specific error) before a guarded call is blocked.
    "flow5_guard_retries": 1,
    # Follow-ups (log / path / "continue") keep the earlier goal and its limits.
    "flow5_sticky": True,
    # PLAN pattern ("write a plan ...") is read-only.
    "flow5_plan": True,
    # Completion of the handoff (fix6): one context block per guided step,
    # scope-aware writes, a universal evidence gate and a real diff stage.
    "flow6_context": True,
    # Replace the full chat history with a minimal per-step prompt for guided
    # steps (finding 15). Off by default until measured against flash-lite.
    "flow6_lean_prompt": False,
    "flow6_metrics": True,
    "flow6_diff_step": True,
    "flow6_scope_guard": True,
    "flow6_universal_guard": True,
    "flow6_cross_module": True,
    "flow6_banner": True,
}


def _c5(key):
    return CONFIG.get(key, _FF5_DEFAULTS[key])


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
_FF5_HOWTO_RE = re.compile(
    r"\bhow\s+(?:can|could|do|should|would|to)\b|\bshould\s+(?:i|we)\b"
    r"|\bwhat(?:'s|\s+is)\s+the\s+(?:best\s+)?way\s+to\b"
    r"|\b(?:lam\s+sao|lam\s+the\s+nao|cach\s+nao|huong\s+dan|chi\s+(?:cach|minh\s+cach))\b"
    r"|\bnen\s+(?:sua|lam)\s+(?:nhu\s+the\s+nao|ra\s+sao)\b")
_FF5_NORUN_RE = re.compile(
    r"\b(?:do\s+not|don'?t|dont|never)\s+(?:run|execute)\s+(?:any\s+)?(?:commands?|shell|scripts?|anything)\b"
    r"|\b(?:khong|dung)\s+(?:duoc\s+)?chay\s+(?:bat\s+ky\s+)?(?:lenh|command|script|shell)\b")


def _ff5_fold(text):
    scannable = T._strip_code_spans(text or "")
    return re.sub(r"\s+", " ", T._fold_diacritics(scannable)).lower()


class _Ff5Perm:
    def __init__(self, edit, run, reason, restricted, scope=None):
        self.edit, self.run = edit, run
        self.reason, self.restricted = reason, restricted
        # None = no restriction; otherwise the paths this request may write.
        self.scope = scope

    def __repr__(self):
        return "Perm(edit=%s, run=%s, %s)" % (self.edit, self.run, self.reason)


def _ff5_permissions(messages, pattern=None, fix=False):
    """Permissions in force for the newest request.

    Sticky: a "do not edit / do not run" from ANY earlier user turn stays in
    force until a later turn grants it back. Guidance questions ("how can I
    fix ...?") withhold write access for that turn only. ``run`` is three
    levels: any / readonly / none -- "do not run commands" now really means
    no command at all, not merely read-only commands.
    """
    texts = [T._stringify_content(m.get("content", "")) for m in messages or []
             if isinstance(m, dict) and m.get("role") == "user"]
    edit_ok = run_ok = True
    why = []
    for t in texts:
        f = _ff5_fold(t)
        if _FF5_GRANT_RE.search(f):
            edit_ok = run_ok = True
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


def _ff5_scope(ctx, messages):
    """Files this request may write. ``None`` = undecided, do not tighten;
    ``[]`` = an empty but known scope, i.e. NO write is allowed (fail closed).

    Policy: a request that names files is limited to them. A request that names
    none (a plain "fix the bug") is limited to files actually read this turn.
    CREATE with neither a named file nor a resolved target gets ``[]`` -- an
    unnamed "create a script" must ask for a filename, never write freely.
    The proxy's own temporary merge helper is always allowed. REVERT/
    TOOL_DEMAND are unrestricted because their writes are derived from the
    conversation itself.
    """
    pattern = _ff5_pat(getattr(ctx, "pattern", None))
    if pattern in ("", "REVERT", "TOOL_DEMAND"):
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
            return named
        reads = [_call_path(e["args"]) for e in _answered_calls(messages)
                 if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])]
        return reads or None
    return []          # this pattern must not write at all


def _ff5_in_scope(path, scope):
    # The merge helper is proxy-managed: allowed before the empty-scope check.
    if _base(path) == _MERGE_SCRIPT:
        return True
    if not scope:
        return False
    return any(T._path_seen(path, [x]) for x in scope)


def _ff5_scope_problems(calls, messages, perm):
    scope = getattr(perm, "scope", None) if perm is not None else None
    # None = the scope could not be determined: do not tighten. An explicit
    # [] IS a restriction: every act call is out of scope (merge helper aside).
    if scope is None:
        return []
    problems = []
    shown = ", ".join(scope[:6]) or "(no write target)"
    for tc in calls or []:
        fn = (tc or {}).get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue
        path = _call_path(T._tool_arguments_dict(fn))
        if path and not _ff5_in_scope(path, scope):
            problems.append(
                f"{path}: outside this request's write scope ({shown}). "
                "Only edit the files the user named or the files read in this turn.")
    return problems


def _ff5_edit_forbidden(messages):
    return not _ff5_permissions(messages).edit


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
_FF5_TRUNC_RE = re.compile(r"(?i)(?:truncated|output\s+(?:was\s+)?cut|more\s+lines|\.\.\.\s*\[?\d+\s+(?:more|lines))")
_FF5_EXEC_FAIL_RE = re.compile(
    r"(?im)^(?:fatal:|error:|exception:|traceback \(most recent call last\)|"
    r"command not found|no such file)")


def _ff5_cmd_readonly(cmd):
    c = re.sub(r"\d?>&\d|\d?>\s*/dev/null|\d?>\s*nul\b", "", cmd or "", flags=re.I)
    if ">" in c:
        return False
    segs = [s for s in re.split(r"&&|\|\||;|\|", c) if s.strip()]
    if not segs:
        return False
    return all(_FF5_RO_SEG_RE.match(s) or _FF5_TEST_SEG_RE.match(s) for s in segs)


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

def _ff5_read_events(messages):
    return [e for e in _answered_calls(messages) if e["kind"] == "read"]


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


def _ff5_evidence_gate(ctx, messages, tm):
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
        detail = "; ".join(_ff5_status_detail(e) for e in partial[:3])
        if _ff5_vi(ctx):
            return None, ("Một lần đọc đã bị cắt và công cụ đọc không hỗ trợ "
                          "offset nên mình KHÔNG sửa gì (" + detail + "). Hãy "
                          "đọc lại file với giới hạn lớn hơn rồi yêu cầu lại.")
        return None, ("A read was truncated and the read tool exposes no offset "
                      "parameter, so I made NO edit (" + detail + "). Re-read the "
                      "file with a larger limit and ask again.")

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


def _build_step_prompt_core(ctx, step, messages, tm):
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


def _ff5_after_plan(step, messages, tm, ctx=None):
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


def plan_for(messages, tm, ctx=None):
    """Final planner: every pattern passes the universal transition guard."""
    step = _ff5_prev_plan_for(messages, tm, ctx)
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


def _ff5_py_defs(tree):
    """Top-level definitions of one module: name -> facts used by the pass."""
    out = {}
    for node in getattr(tree, "body", ()):
        if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
            args = node.args
            total = len(getattr(args, "posonlyargs", ())) + len(args.args)
            required = max(0, total - len(args.defaults))
            out[node.name] = {
                "kind": "function", "required": required, "total": total,
                "vararg": args.vararg is not None,
                "line": getattr(node, "lineno", 0),
            }
        elif isinstance(node, _ff5_ast.ClassDef):
            out[node.name] = {"kind": "class", "required": 0, "total": 0,
                              "vararg": True, "line": getattr(node, "lineno", 0)}
    return out


def _ff5_py_modules(messages):
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


def _ff5_arg_problem(node, info):
    if info.get("kind") != "function":
        return ""
    given = len(node.args)
    if any(isinstance(a, _ff5_ast.Starred) for a in node.args):
        return ""
    required, total = info.get("required", 0), info.get("total", 0)
    if given < required:
        return ("expects at least %d positional argument(s) but got %d"
                % (required, given))
    if given > total and not info.get("vararg"):
        return ("expects at most %d positional argument(s) but got %d"
                % (total, given))
    return ""


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
    report = _ff5_prev_map_reduce(messages, step, ctx, gen, client_gone)
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


def _ff5_own_result(step, ctx, messages, tm, perm):
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

def _ff5_spec_banner(messages):
    try:
        # Banner, planner and validator share ONE source of truth: the full
        # analysis (sticky context, permissions, create/merge, debug-fix),
        # not a bare detect_task_pattern + ad-hoc _Ctx fallback.
        ctx = _analyze(messages)
        if ctx is None or not getattr(ctx, "pattern", None):
            return _ff5_prev_banner(messages)
        pattern = _ff5_pat(ctx.pattern)
        fix = bool(getattr(ctx, "fix", False))
        perm = getattr(ctx, "perm", None) or \
            _ff5_permissions(messages, pattern, fix)
        if pattern not in _FF5_SPEC:
            return _ff5_prev_banner(messages)
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
        return _ff5_prev_banner(messages)


_FF5_REVERT_RE = re.compile(
    r"\b(?:revert|undo|rollback|roll\s+back|hoan\s+tac|khoi\s+phuc|huy|quay\s+lai|tra\s+ve)\b"
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
    r"|\bwithout\s+(?:editing|modifying|changing|touching|writing|altering)\b"
    r"|\bread[\s-]*only\b"
    r"|\b(?:khong|dung)\s+(?:duoc\s+)?(?:sua|chinh\s+sua|thay\s+doi|ghi|cap\s+nhat|dong\s+vao)"
    r"\s+(?:vao\s+)?(?:file|code|ma\s+nguon|source|bat\s+ky|gi|du\s+an)\b"
    r"|\bchi\s+(?:doc|giai\s+thich|huong\s+dan|goi\s+y)\b")

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


def _ff5_is_windows(messages):
    pat = re.compile(r"ParserError|PowerShell|not a valid statement separator|\bwin32\b|"
                     r"Platform:\s*windows|\b[A-Za-z]:[\\/]", re.I)
    for msg in messages or []:
        if isinstance(msg, dict):
            body = T._stringify_content(msg.get("content", ""))[:20000]
            if pat.search(body):
                return True
    return False


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


def _ff5_git_cmd(m):
    sub = m.group(1).lower()
    flags = []
    for tok in (m.group(2) or "").split():
        if tok.startswith("-"):
            flags.append(tok)
        else:
            break
    if not flags:
        flags = {"status": ["--short", "--branch"],
                 "log": ["--oneline", "-n", "20"]}.get(sub, [])
    return " ".join(["git", "--no-pager", sub] + flags)


_FF5_PLAN_RE = re.compile(
    r"\b(?:write|draft|make|give\s+me|create|lap|viet|soan)\s+(?:me\s+)?(?:an?\s+|the\s+)?"
    r"(?:implementation\s+|detailed\s+)?(?:plan|ke\s+hoach)\b|\bplan\s+(?:for|how|to)\b|\blap\s+ke\s+hoach\b")


def _ff5_detect(ctx):
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


_ff5_tls = _ff5_threading.local()
_ff5_cache = {}


def _ff5_classify_prompt(text):
    lines = ["Classify the user's request into exactly ONE label from the list, "
             "or NONE if no label fits.", "Labels:"]
    lines += [f"- {k}: {v}" for k, v in _FF5_CATALOG.items()]
    lines += ["- NONE: anything else (chat, a question unrelated to the project, ...)",
              "", "Request:", text.strip()[:600], "",
              "Answer with the label only, nothing else."]
    return T.defuse_extension_words("\n".join(lines))


def _ff5_classify(text):
    """Label from the catalog, or None (NONE / unknown / error -> model runs freely)."""
    text = (text or "").strip()
    if len(text) < 8:
        return None
    key = _ff5_hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    if key in _ff5_cache:
        return _ff5_cache[key]
    model = getattr(_ff5_tls, "model", None)
    if not model:
        return None
    label = None
    try:
        m_id, m_think, m_extra, m_ticket = model
        raw = generate(_ff5_classify_prompt(text), m_id, m_think, None, m_extra, m_ticket)
        if raw and not T.looks_like_canned_failure(raw):
            for w in re.findall(r"[A-Z][A-Z_]{3,}", raw.upper()):
                if w in _FF5_CATALOG:
                    label = w
                    break
                if w == "NONE":
                    break
        log(f"[fix5] model classified the request as {label or 'NONE'}")
    except Exception as e:
        log(f"[fix5] classify failed, model runs freely: {e}")
    if len(_ff5_cache) > 300:
        _ff5_cache.clear()
    _ff5_cache[key] = label
    return label


def _ff5_apply_label(ctx, label):
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
        prev = _ff5_prev_analyze(messages[:j + 1])
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


def _analyze(messages):
    ctx = _ff5_prev_analyze(messages)
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


def _ff5_plan_tests(ctx, messages, tm):
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


def _ff5_plan_command(ctx, messages, tm, cmd, label):
    ev = _answered_calls(messages)
    cmd = _ff5_fix_for(cmd, messages)
    runs = [e for e in ev if e["kind"] == "exec" and _call_command(e["args"]).strip() == cmd.strip()]
    if not runs:
        call = tm.make_call("exec", command=cmd, description=label) if tm.first("exec") else None
        if call:
            return _Ff5Step("exec", calls=[call])
        return _ff5_say(ctx, "Không có công cụ shell để chạy lệnh.", "No shell tool to run the command.")
    return _Ff5Step("say", text="`%s`\n%s" % (cmd, _ff5_block(runs[-1]["result"])))


def _ff5_plan(ctx, messages, tm):
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

def _ff5_read_guard(ctx, messages):
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


def _verify_findings(text, chunk):
    """(kept, dropped). A quote counts only inside the file the finding names,
    and (when the file shows line numbers) within 5 lines of the claimed one."""
    kept, dropped = [], 0
    if re.fullmatch(r"\W*NONE\W*", (text or "").strip(), re.IGNORECASE):
        return kept, dropped
    files = [(p, b) for p, b in chunk]
    for item in _split_findings(text):
        snippets = re.findall(r"`([^`\n]{8,})`", item)
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


def _ff5_act_needs_read_problems(calls, messages, ctx):
    """Read-before-edit: an act on an existing file needs a successful read.

    Only enforced for real code-change intent (CHANGE, or DEBUG with fix=True)
    -- exactly the scope of :func:`_ff5_read_guard`, which however only fires
    when a read *failed*; this closes the fail-open hole where the model emits
    an edit without ever reading the file.
    """
    pattern = getattr(ctx, "pattern", None)
    fix = bool(getattr(ctx, "fix", False))

    # Chỉ ép read-before-edit với intent sửa code thật sự
    if pattern not in ("CHANGE", "DEBUG"):
        return []
    if pattern == "DEBUG" and not fix:
        return []

    good_reads = _ff5_successful_read_paths(messages)
    problems = []

    for tc in calls or []:
        fn = (tc or {}).get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue

        args = T._tool_arguments_dict(fn)
        path = _call_path(args)
        if not path:
            continue

        # Helper merge script là ngoại lệ do proxy tự quản lý
        if _base(path) == _MERGE_SCRIPT:
            continue

        if not any(T._path_seen(path, [p]) for p in good_reads):
            problems.append(
                f"{path}: cannot edit/write before a successful read of this file"
            )

    return problems


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

    return False


def _ff5_postfilter(result, messages, perm=None, last=True, tm=None, ctx=None):
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
            return blocked(
                "Mình chưa sửa vì file chưa được đọc thành công:\n" + body,
                "I did not edit because the target file was not successfully "
                "read:\n" + body,
                "Read the target file successfully before editing it:\n" + body,
                retry=True)

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


def install():
    _ff5_prev_install()
    for k, v in _FF5_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    from gemini_web2api import server
    _ff5_wrap_change(server)
    _ff5_install_banner()
    cls = server.GeminiHandler
    cur = cls._generate_with_tool_retry
    if getattr(cur, "_flow5_patched", False):
        return

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
        try:
            retries = max(0, int(_c5("flow5_guard_retries")))
            for attempt in range(retries + 1):
                final, nudge = _ff5_postfilter(
                    result, messages, perm, last=(attempt >= retries), tm=tm,
                    ctx=ctx)
                if nudge is None:
                    return final
                _ff5_metric("guard_retries")
                log(f"[fix5] guard rejected the output, re-asking (attempt {attempt + 1}): {nudge[:160]}")
                result = cur(self, prompt + "\n\n[SYSTEM NOTE] " + nudge, model_id, think_mode, file_refs,
                             extra, tools_active, allowed_names, tool_schemas, required_tool,
                             messages=messages, ticket=ticket, tool_required=tool_required,
                             client_gone=client_gone)
            return result
        except Exception as e:
            log(f"[fix5] postfilter error: {e}")
            return result

    wrapper5._flow5_patched = True
    cls._generate_with_tool_retry = wrapper5
    log("[fix5] flow_fix5 installed")
# ==== flowfix5:end ====

# ==== flowfix6b:begin (generated by patch_flow_critical.py) ====
# Overrides (later definitions win: callers resolve these names at call time).
_ff6b_prev_postfilter = _ff5_postfilter
_ff6b_prev_cmd_readonly = _ff5_cmd_readonly
_ff6b_prev_plan = _ff5_plan


# 4. ctx None (tool_choice=required, analysis skipped) must not mean "no edit,
#    read-only": honour only the limits the user actually wrote.
def _ff5_postfilter(result, messages, perm=None, last=True, tm=None, ctx=None):
    if perm is None:
        perm = _ff5_permissions(messages, "TOOL_DEMAND")
    return _ff6b_prev_postfilter(result, messages, perm, last, tm, ctx)


# 3. tighter read-only check
_FF6B_FIND_RE = re.compile(
    r"\bfind\b[^|;&]*\s-(?:delete|exec|execdir|ok|okdir|fprint\w*|fls)\b", re.I)
_FF6B_BRANCH_RE = re.compile(
    r"\bgit\s+(?:--no-pager\s+)?branch\b[^|;&]*\s"
    r"(?:-d|-m|-c|-f|--delete|--move|--copy|--force)\b", re.I)
_FF6B_REMOTE_RE = re.compile(
    r"\bgit\s+(?:--no-pager\s+)?remote\s+(?:add|remove|rm|rename|set-url|"
    r"set-head|prune|update)\b", re.I)
_FF6B_OUTPUT_RE = re.compile(r"\bgit\b[^|;&]*--output\b", re.I)


def _ff5_cmd_readonly(cmd):
    c = cmd or ""
    if "`" in c or "$(" in c:
        return False
    if (_FF6B_FIND_RE.search(c) or _FF6B_BRANCH_RE.search(c)
            or _FF6B_REMOTE_RE.search(c) or _FF6B_OUTPUT_RE.search(c)):
        return False
    return _ff6b_prev_cmd_readonly(c)


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
    return _ff6b_prev_plan(ctx, messages, tm)


# 6a. classifier: never cache an upstream failure as "NONE"
def _ff5_classify(text):
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
def _ff5_py_defs(tree):
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


def _ff5_arg_problem(node, info):
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
_ff6c_prev_permissions = _ff5_permissions
_ff6c_prev_scope = _ff5_scope
_ff6c_prev_guard = _ff5_read_guard
_ff6c_prev_gate = _ff5_evidence_gate

_FF6C_NOTFOUND_RE = re.compile(
    r"(?i)no such file|not found|does not exist|enoent|cannot find|"
    r"cannot open|can'?t find")


# --------------------------------------------------------------------------
# 3. guidance only when the FIRST sentence is a how-to question
# --------------------------------------------------------------------------
_FF6C_Q_LEAD = re.compile(
    r"^(?:(?:ok|okay|so|hey|hi|and|also|then|vay|the|nhung|ma|oi)\b[\s,]*)?"
    r"(?:how\s+(?:can|could|do|does|should|would|to)\b|should\s+(?:i|we)\b|"
    r"what(?:'s|\s+is)\s+the\s+(?:best\s+)?way\s+to\b|lam\s+sao\b|"
    r"lam\s+the\s+nao\b|cach\s+nao\b|huong\s+dan\b|chi\s+(?:minh\s+)?cach\b|"
    r"nen\s+(?:sua|lam)\b)")
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


# --------------------------------------------------------------------------
# 2. pattern None: only the user's explicit limits apply
# --------------------------------------------------------------------------
def _ff5_permissions(messages, pattern=None, fix=False):
    return _ff6c_prev_permissions(messages, pattern or "TOOL_DEMAND", fix)


def _ff5_scope(ctx, messages):
    if not _ff5_pat(getattr(ctx, "pattern", None)):
        try:
            named = list(_named_files(ctx)) if ctx is not None else []
        except Exception:
            named = []
        reads = [_call_path(e["args"]) for e in _answered_calls(messages)
                 if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])]
        return (named + reads) or None
    return _ff6c_prev_scope(ctx, messages)


# --------------------------------------------------------------------------
# 1. new file vs existing file
# --------------------------------------------------------------------------
def _ff6c_is_write(args):
    return not any(isinstance(args.get(k), str) and args[k] for k in _OLD_KEYS)


def _ff6c_survey_listing(ev):
    sub = [e for e in ev if e["kind"] == "survey" or (
        e["kind"] == "exec" and _LIST_CMD_RE.search(_call_command(e["args"])))]
    return _listing_files(sub) if sub else []


def _ff6c_proven_new(path, messages):
    """True only with evidence that ``path`` does not exist yet."""
    ev = _answered_calls(messages)
    for e in ev:
        p = _call_path(e["args"])
        if (e["kind"] == "read" and p and T._path_seen(path, [p])
                and _ff5_status(e) in ("success", "partial")):
            return False
    for e in ev:
        p = _call_path(e["args"])
        if (e["kind"] == "read" and p and T._path_seen(path, [p])
                and _ff5_status(e) == "failed"
                and _FF6C_NOTFOUND_RE.search((e["result"] or "")[:300])):
            return True
    listing = _ff6c_survey_listing(ev)
    if (listing and path.lower().endswith(_SRC_EXT)
            and not any(T._path_seen(path, [f]) for f in listing)):
        return True
    return False


def _ff5_act_needs_read_problems(calls, messages, ctx):
    pattern = getattr(ctx, "pattern", None)
    fix = bool(getattr(ctx, "fix", False))
    if pattern not in ("CHANGE", "DEBUG", "CREATE"):
        return []
    if pattern == "DEBUG" and not fix:
        return []
    good = _ff5_successful_read_paths(messages)
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
        if any(T._path_seen(path, [p]) for p in good):
            continue
        is_write = _ff6c_is_write(args)
        if pattern == "CREATE":
            exists = bool(listing) and any(T._path_seen(path, [f]) for f in listing)
            if is_write and exists:
                problems.append(
                    f"{path}: this file already exists -- read it first, or "
                    "choose a different file name")
            elif not is_write:
                problems.append(
                    f"{path}: cannot edit this file before a successful read of it")
            continue
        if is_write and _ff6c_proven_new(path, messages):
            continue
        hint = (" If this is a NEW file, first prove it does not exist (a glob/ls "
                "listing, or a read that returns 'not found'); if it exists, read "
                "it first." if is_write else "")
        problems.append(
            f"{path}: cannot edit/write before a successful read of this file"
            + hint)
    return problems


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
                        and not any(T._path_seen(path, [f]) for f in keep_named)):
                    drop.add(msg.get("tool_call_id"))
        if not drop:
            return messages
        return [m for m in messages if not (
            isinstance(m, dict) and m.get("role") == "tool"
            and m.get("tool_call_id") in drop)]
    except Exception as e:
        log(f"[fix6c] read filter error: {e}")
        return messages


def _ff5_read_guard(ctx, messages):
    return _ff6c_prev_guard(ctx, _ff6c_filter(ctx, messages))


def _ff5_evidence_gate(ctx, messages, tm):
    return _ff6c_prev_gate(ctx, _ff6c_filter(ctx, messages), tm)
# ==== flowfix6c:end ====

# ==== flowfix8:begin (generated by flow_fix8_patch.py -- re-run to refresh, --revert to remove) ====
import threading as _ff8_threading

_FF8_DEFAULTS = {
    "flow8_strict_shell": True,        # finding 1
    "flow8_write_regate": True,        # finding 2
    "flow8_absence_proof": True,       # finding 3
    "flow8_independent_grants": True,  # finding 4
    "flow8_lean_source": True,         # finding 5 (only matters when flow6_lean_prompt is on)
    "flow8_lean_source_chars": 24000,
    "flow8_listing_cap": 100,          # a listing this long may have been cut by the client
    "flow8_bard_strict": True,         # finding 6
    "flow8_destructive_wide": True,    # finding 7
    "flow8_revert_needs_edit": True,   # finding 8
}
for _k8, _v8 in _FF8_DEFAULTS.items():
    CONFIG.setdefault(_k8, _v8)


def _c8(key):
    return CONFIG.get(key, _FF8_DEFAULTS[key])


_ff8_tls = _ff8_threading.local()

# --------------------------------------------------------------------------
# 1. Read-only command check: parse the WHOLE command, refuse what we cannot
#    reason about (newline, background &, backticks, $(), redirects, writing flags)
# --------------------------------------------------------------------------

_ff8_prev_cmd_readonly = _ff5_cmd_readonly

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
    if not _c8("flow8_strict_shell"):
        return _ff8_prev_cmd_readonly(cmd)
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


# --------------------------------------------------------------------------
# 4. Edit grant and run grant are independent
# --------------------------------------------------------------------------

_ff8_prev_permissions = _ff5_permissions
_FF8_RUN_GRANT_RE = re.compile(
    r"\b(?:you\s+(?:may|can)\s+(?:now\s+)?(?:run|execute)"
    r"|feel\s+free\s+to\s+(?:run|execute)"
    r"|go\s+ahead\s+and\s+(?:run|execute)"
    r"|(?:run|execute)\s+(?:it|them|the\s+(?:unit\s+)?(?:tests?|commands?))\s+(?:now|again)"
    r"|cho\s+phep\s+chay|chay\s+luon|cu\s+chay)\b")


def _ff5_permissions(messages, pattern=None, fix=False):
    """Like the original, but a grant of edit rights never lifts a ban on
    running commands (and the other way round). Bans win over grants made in
    the same message; a generic "go ahead" grants edit only (fail closed)."""
    if not _c8("flow8_independent_grants"):
        return _ff8_prev_permissions(messages, pattern, fix)
    texts = [T._stringify_content(m.get("content", "")) for m in messages or []
             if isinstance(m, dict) and m.get("role") == "user"]
    edit_ok = run_ok = True
    why = []
    for t in texts:
        f = _ff5_fold(t)
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


_ff8_prev_existing = _existing


def _existing(known, listing):
    if not _c8("flow8_absence_proof"):
        return _ff8_prev_existing(known, listing)
    msgs = getattr(_ff8_tls, "messages", None)
    if not msgs:
        return list(known)
    ev = _answered_calls(msgs)
    return [f for f in known
            if not f.lower().endswith(_SRC_EXT) or not _ff8_listing_absent(f, ev)]


_ff8_prev_plan = _plan


def _plan(messages, tm, ctx):
    _ff8_tls.messages = messages
    try:
        return _ff8_prev_plan(messages, tm, ctx)
    finally:
        _ff8_tls.messages = None


_ff8_prev_plan_create = _plan_create


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
                probs.extend(pr + ["%s: the file changed after your last read; "
                                   "re-read it before editing again" % path])
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


_ff8_prev_postfilter = _ff5_postfilter


def _ff5_postfilter(result, messages, perm=None, last=True, tm=None, ctx=None):
    result, nudge = _ff8_prev_postfilter(result, messages, perm, last, tm, ctx)
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
            body = _ff5_trunc_re().sub("", e.get("result") or "").rstrip()
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


def _ff8_render_view(v, kws, locs, budget):
    lines = v["text"].split("\n")
    n = len(lines)
    numbered = T._already_numbered(lines)
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


def _ff8_source_block(ctx, stage, messages):
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


_ff8_prev_core = _build_step_prompt_core


def _build_step_prompt_core(ctx, step, messages, tm):
    base = _ff8_prev_core(ctx, step, messages, tm)
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


_ff8_prev_install = install


def install():
    _ff8_prev_install()
    try:
        _ff8_patch_gemini()
    except Exception as e:
        log("[fix8] gemini patch failed: %s" % e)


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

_ff8_prev_analyze = _analyze


def _analyze(messages):
    ctx = _ff8_prev_analyze(messages)
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


_ff8_prev_own_result = _ff5_own_result


def _ff5_own_result(step, ctx, messages, tm, perm):
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
    return _ff8_prev_own_result(step, ctx, messages, tm, perm)
# ==== flowfix8:end ====

# ==== flowfix9:begin (generated by flow_fix9_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: callers resolve these names at call time).
_ff9_prev_permissions = _ff5_permissions
_ff9_prev_detect = _ff5_detect
_ff9_prev_apply_label = _ff5_apply_label


# 1. flowfix8 re-implemented _ff5_permissions and lost the rule flowfix6c added:
#    with no detected pattern only the limits the user wrote apply (edit allowed).
def _ff5_permissions(messages, pattern=None, fix=False):
    return _ff9_prev_permissions(messages, pattern or "TOOL_DEMAND", fix)


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


def _ff5_detect(ctx):
    det = _ff9_prev_detect(ctx)
    if det in _FF9_RUN_PATTERNS and _ff9_run_negated(ctx):
        log("[fix9] run request is negated by the user -> not a %s request" % det)
        ctx.cmd = ""
        ctx.git_cmd = ""
        return None
    return det


def _ff5_apply_label(ctx, label):
    if label in _FF9_RUN_PATTERNS and _ff9_run_negated(ctx):
        log("[fix9] classifier label %s ignored: the user negated running" % label)
        return
    return _ff9_prev_apply_label(ctx, label)

# 3. Edit gate: join continued reads, forget text that an edit made stale.
def _ff9_read_texts(messages):
    texts = {}
    for e in _answered_calls(messages):
        p = _call_path(e["args"])
        if not p:
            continue
        if e["kind"] == "act":
            if _act_ok(e):
                for k in [k for k in texts if T._path_seen(p, [k])]:
                    del texts[k]
            continue
        if e["kind"] != "read" or not _read_ok(e):
            continue
        body = _clean_file_text(
            _ff5_trunc_re().sub("", e["result"] or "").rstrip())
        if _ff4_off(e["args"]) > 0 and p in texts:
            texts[p] += "\n" + body
        else:
            texts[p] = body
    return texts


def _base_gate_act_calls(calls, messages):
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


_ff9_prev_read_guard = _ff5_read_guard
_ff9_prev_evidence_gate = _ff5_evidence_gate


def _ff5_read_guard(ctx, messages):
    return _ff9_prev_read_guard(ctx, _ff9_drop_superseded(messages))


def _ff5_evidence_gate(ctx, messages, tm):
    return _ff9_prev_evidence_gate(ctx, _ff9_drop_superseded(messages), tm)


# 5. A bare file name may live in a sub-folder: list the project before reading.
_ff9_prev_plan_change = _plan_change


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


def _plan_change(ctx, ev, tm, pat="CHANGE", total=5):
    try:
        if pat != "CREATE" and not _surveyed(ev) and _ff9_bare_unread(ctx, ev):
            step = _survey_step(pat, tm, ev, 1, total)
            if step is not None:
                return step
    except Exception as e:
        log(f"[fix9] bare-name survey error: {e}")
    return _ff9_prev_plan_change(ctx, ev, tm, pat=pat, total=total)


# 6. After an edit the read copy is stale: re-read before the next edit.
def _ff9_stale_edits(ev):
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


_ff9_prev_change_tail = _change_tail


def _change_tail(pat, ctx, ev, tm, names_all, total, extra=""):
    step = _ff9_prev_change_tail(pat, ctx, ev, tm, names_all, total, extra)
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


# 7. More truncation markers: "(Showing lines 1-2000 of N. Use offset=2001 ...)".
_ff9_trunc_orig = _FF4_TRUNC_RE
_FF9_TRUNC_PAREN = re.compile(
    r"\([^()\n]*?(?:showing\s+lines|truncated|more\s+lines)[^()\n]*?"
    r"\boffset\s*[=:]\s*(\d+)[^()\n]*\)", re.IGNORECASE)


class _Ff9Trunc:
    """Drop-in for the compiled regex: findall / search / sub only."""

    def __init__(self):
        self._rxs = (_ff9_trunc_orig, _FF9_TRUNC_PAREN)

    def findall(self, text):
        hits = []
        for rx in self._rxs:
            for m in rx.finditer(text or ""):
                hits.append((m.start(), m.group(1)))
        return [v for _s, v in sorted(hits)]

    def search(self, text):
        for rx in self._rxs:
            m = rx.search(text or "")
            if m:
                return m
        return None

    def sub(self, repl, text, count=0):
        for rx in self._rxs:
            text = rx.sub(repl, text, count=count)
        return text


_FF4_TRUNC_RE = _Ff9Trunc()
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

_FF10_DEFAULTS = {
    "flow_tiebreak": True,
    "flow_ask_on_ambiguity": True,
}


def _c10(key):
    return CONFIG.get(key, _FF10_DEFAULTS[key])


# Previous definitions, captured before this section overrides them. The file
# is a stack of patch layers: later definitions win, so every wrapper keeps a
# handle on the layer below it.
_ff10_orig_source_review = _is_source_review
_ff10_orig_file_review = _is_file_review
_ff10_prev_analyze = _analyze
_ff10_prev_clarification = clarification_text
_ff10_prev_spec_banner = _ff5_spec_banner
_ff10_prev_install = install


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


def _ff10_scrub(folded):
    """Apply the flowfix4 noise scrub when that layer is present."""
    scrub = globals().get("_ff4_scrub")
    return scrub(folded or "") if scrub else (folded or "")


def _ff10_is_source_review(folded):
    f = _ff10_scrub(folded)
    if not _ff10_orig_source_review(f):
        return False
    if _SOURCE_STRONG_VERB_RE.search(f):
        return True
    # "tim hieu source", "doc source roi giai thich", "find how ... works":
    # a weak review verb inside an explain question is not an audit request.
    if _FF10_EXPLAIN_RE.search(f) and not _FF10_DEFECT_RE.search(f):
        return False
    return True


def _ff10_is_file_review(folded, scannable):
    f = _ff10_scrub(folded)
    if not _ff10_orig_file_review(f, scannable):
        return False
    if _SOURCE_STRONG_VERB_RE.search(f):
        return True
    if _FF10_EXPLAIN_RE.search(f) and not _FF10_DEFECT_RE.search(f):
        return False
    return True


# detect_task_pattern()/review_demanded()/pending_tool_request() resolve these
# names from this module, so assigning them here changes the deterministic gate
# too. T is this same module (the flat bundle aliases every submodule).
_is_source_review = _ff10_is_source_review
_is_file_review = _ff10_is_file_review
T._is_source_review = _is_source_review
T._is_file_review = _is_file_review


def _ff10_last_user(messages):
    last = None
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            last = T._stringify_content(msg.get("content", ""))
    return last


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
    raw_file = _ff10_orig_file_review(scrubbed, scannable)
    raw_source = not raw_file and _ff10_orig_source_review(scrubbed)
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


def _ff10_classify(text, labels):
    text = (text or "").strip()
    labels = [x for x in labels if x]
    if len(text) < 8 or not labels:
        return None
    key = _ff10_hashlib.sha1(
        (text + "\x00" + "|".join(labels)).encode("utf-8", "ignore")).hexdigest()
    if key in _ff10_cache:
        return _ff10_cache[key]
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
        return _ff10_apply_pattern(ctx, messages, label)
    log("[flow10] tie-break unavailable -> keeping deterministic pattern %s"
        % (getattr(ctx, "pattern", None) or "NONE"))
    return ctx


def _analyze(messages):
    ctx = _ff10_prev_analyze(messages)
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
    if _c10("flow_ask_on_ambiguity") and messages:
        try:
            ctx = _analyze(messages)
            if ctx is not None and getattr(ctx, "ask_user", False):
                log("[flow10] ambiguous request -> asking the client to pick a pattern")
                return _ff10_clarify_text(getattr(ctx, "last", ""))
        except Exception as e:
            log("[flow10] clarification error: %s" % e)
    return _ff10_prev_clarification(messages)


def _ff5_spec_banner(messages):
    try:
        ctx = _analyze(messages)
        if ctx is not None and getattr(ctx, "free_run", False):
            return ""
    except Exception as e:
        log("[flow10] banner guard error: %s" % e)
    return _ff10_prev_spec_banner(messages)


def install():
    _ff10_prev_install()
    for k, v in _FF10_DEFAULTS.items():
        CONFIG.setdefault(k, v)


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
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from gemini_web2api.config import CONFIG
from gemini_web2api.models import MODELS, resolve_model, ticket_for
from gemini_web2api.gemini import generate, generate_stream, log, GeminiError, trace
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
    looks_like_extension_refusal,
    looks_like_canned_failure,
    pending_tool_request,
    SOURCE_REVIEW_OWED,
    is_read_retry_reason,
    is_review_test_reason,
    is_test_retry_reason,
    listed_turn_files,
    unread_listed_core_files,
    fresh_declared_unread,
    review_demanded,
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
        # The ticket is what upstream routes on; [79]/[80] alone are ignored.
        return echo, model_name, model_id, think_mode, extra, ticket_for(model_name)

    def do_POST(self):
        try:
            path = self._route_path()
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
                saw_call = True
                if all(((tc.get("function") or {}).get("name"),
                        _canonical_args((tc.get("function") or {}).get("arguments")))
                       in repeatable for tc in tool_calls):
                    # Every call repeats one whose result is already in the
                    # conversation: running it again cannot learn anything.
                    if repeats < 3 and attempt + 1 < attempts:
                        repeats += 1
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
from gemini_web2api.gemini import HAS_HTTPX, fetch_latest_bl, log
from gemini_web2api.server import GeminiHandler, ThreadedServer
from gemini_web2api import __version__

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(host) -> bool:
    """True when the bind address cannot be reached from another machine."""
    if not isinstance(host, str) or not host:
        return False
    value = host.strip().lower().strip("[]")
    return value in _LOOPBACK_HOSTS or value.startswith("127.")


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
    _guard_bind(host, args.allow_insecure)
    server = ThreadedServer((host, port), GeminiHandler)
    print(f"gemini-web2api v{__version__}")
    print(f"  Listening: http://{host}:{port}")
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  Models:    {', '.join(MODELS.keys())}")
    auth_enabled = bool(CONFIG.get('api_keys'))
    auth_msg = 'enabled' if auth_enabled else 'DISABLED (any client can call this)'
    if auth_enabled and not CONFIG.get('api_keys'):
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


if __name__ == "__main__":
    main()


