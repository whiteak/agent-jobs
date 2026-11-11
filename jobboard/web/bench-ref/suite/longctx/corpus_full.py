
# ===== file: serve/__init__.py =====
"""Basalt serve: the OpenAI- and Anthropic-compatible server in front of the engine."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ===== file: serve/api/__init__.py =====
"""The HTTP API surfaces: OpenAI, Anthropic, Responses and the JSON response formats."""

# ===== file: serve/api/anthropic.py =====
"""Anthropic messages events and their non-streaming collection."""
from __future__ import annotations

import json
import uuid

from serve.frontend import Event
from serve.prompt import tokens_known
from serve.service import Service


def anthropic_events(svc: Service, req: dict, ids, thinking, tools, max_new, cancel, force=None):
    mid = "msg_" + uuid.uuid4().hex[:24]

    def start():
        return "message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": svc.model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": len(ids), "output_tokens": 0}}}

    # the start carries the prompt's token count: when the engine tokenizes, it opens once the engine has counted
    started = tokens_known(ids)
    if started:
        yield start()
    index, open_kind, used_tool = -1, None, False

    def close():
        return ("content_block_stop", {"type": "content_block_stop", "index": index})

    streamed, finished = set(), set()              # calls sent piece by piece; those whose final tool_call came
    for kind, x in svc.run(ids, thinking, tools, max_new, req, cancel, force=force):
        if not started and (kind != "ping" or tokens_known(ids)):
            started = True
            yield start()
        if kind == "ping":
            yield None
            continue
        if kind == "event":
            ev: Event = x
            if ev.kind == "tool_args":
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "input_json_delta", "partial_json": ev.text}}
                continue
            if ev.kind == "tool_call" and ev.call.id in streamed:
                finished.add(ev.call.id)
                continue
            want = {"reasoning": "thinking", "content": "text", "tool_call": "tool_use", "tool_start": "tool_use"}[ev.kind]
            if ev.kind not in ("tool_call", "tool_start") and not ev.text:
                continue
            if ev.kind == "tool_start":
                streamed.add(ev.call.id)
                used_tool = True
            if open_kind != want or want == "tool_use":
                if open_kind is not None:
                    yield close()
                index += 1
                open_kind = want
                block = {"thinking": {"type": "thinking", "thinking": "", "signature": ""},
                         "text": {"type": "text", "text": ""},
                         "tool_use": {"type": "tool_use", "id": ev.call.id if ev.call else "", "name":
                                      ev.call.name if ev.call else "", "input": {}}}[want]
                yield "content_block_start", {"type": "content_block_start", "index": index, "content_block": block}
            if want == "thinking":
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "thinking_delta", "thinking": ev.text}}
            elif want == "text":
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "text_delta", "text": ev.text}}
            elif ev.kind == "tool_start":
                pass                                # its input follows as tool_args pieces
            else:
                used_tool = True
                yield "content_block_delta", {"type": "content_block_delta", "index": index, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(ev.call.arguments, ensure_ascii=False)}}
        else:
            if open_kind is not None:
                yield close()
            stop = "stop_sequence" if x.get("stop_sequence") is not None else \
                "tool_use" if used_tool and streamed <= finished and x["finish"] == "stop" else \
                {"stop": "end_turn", "length": "max_tokens", "cancel": "end_turn"}[x["finish"]]
            # input_tokens excludes the cached part, which is cache_read_input_tokens
            reused = min(x.get("reused") or 0, len(ids))
            yield "message_delta", {"type": "message_delta", "delta": {"stop_reason": stop,
                                                                       "stop_sequence": x.get("stop_sequence")},
                                    "usage": {"input_tokens": len(ids) - reused, "cache_read_input_tokens": reused,
                                              "output_tokens": x["completion_tokens"]}}
            yield "message_stop", {"type": "message_stop"}


def anthropic_collect(events) -> dict:
    msg, blocks = None, []
    for item in events:
        if item is None:                           # a heartbeat
            continue
        name, e = item
        if name == "message_start":
            msg = e["message"]
        elif name == "content_block_start":
            blocks.append(dict(e["content_block"]))
        elif name == "content_block_delta":
            d, b = e["delta"], blocks[-1]
            if d["type"] == "text_delta":
                b["text"] += d["text"]
            elif d["type"] == "thinking_delta":
                b["thinking"] += d["thinking"]
            else:                                  # input_json_delta pieces: parsed when complete
                b["_json"] = b.get("_json", "") + d["partial_json"]
        elif name == "content_block_stop" and blocks and "_json" in blocks[-1]:
            b = blocks[-1]
            try:
                b["input"] = json.loads(b.pop("_json") or "{}")
            except ValueError:                     # a call the output ended inside: it has no input to give
                blocks.pop()
        elif name == "message_delta":
            msg["stop_reason"] = e["delta"]["stop_reason"]
            msg["stop_sequence"] = e["delta"].get("stop_sequence")
            msg["usage"].update(e["usage"])
    msg["content"] = blocks
    return msg

# ===== file: serve/api/grammar.py =====
"""The `grammar` key of an engine request: what the engine builds its constraint from.

Sent only for a request with tools or a JSON response format, and only when the server option is on or the request
sets strict: true on the schema or on a tool function. A JSON format with tools offered (tool_choice auto) holds the
answer to tool calls or the JSON value; a forced call keeps the tools grammar alone."""
from __future__ import annotations

import json

from serve.frontend import tool_choice_of

REQUEST_KEY = "_grammar"        # in the request dict handed to the engine


def _strict(value) -> bool:
    """True when a tool list (any API shape, namespaces included) or a schema spec has strict: true."""
    if isinstance(value, dict):
        return value.get("strict") is True or _strict(value.get("function")) or _strict(value.get("tools"))
    if isinstance(value, list):
        return any(_strict(v) for v in value)
    return False


def _as_function(tool: dict) -> dict:
    """A tool in the OpenAI function-object shape."""
    return tool if tool.get("type") == "function" and isinstance(tool.get("function"), dict) \
        else {"type": "function", "function": tool}


def grammar_spec(enabled: bool, tools, tool_choice, raw_tools, response_format) -> dict | None:
    """The `grammar` value for a request, or None. `tools`: the tools offered (None when there are none or
    tool_choice is none); `raw_tools`: the request's tools as received, for strict; `response_format`: the chat
    path's shape ({"type": "json_schema", "json_schema": {...}} or {"type": "json_object"}), None for text.
    A JSON format with tools under tool_choice auto: the answer is tool calls or the JSON value."""
    answer = None
    kind = response_format.get("type") if isinstance(response_format, dict) else None
    if kind == "json_schema":
        spec = response_format.get("json_schema") or {}
        if enabled or spec.get("strict") is True:
            answer = {"kind": "json_schema", "schema": spec["schema"]}
    elif kind == "json_object" and enabled:
        answer = {"kind": "json_object"}
    if not tools:
        return answer
    calls = [_as_function(t) for t in tools]
    if answer and tool_choice_of(tool_choice)[0] == "auto":
        return dict(answer, tools=calls)
    if enabled or _strict(raw_tools):
        return {"kind": "tools", "tools": calls, "tool_choice": "auto" if tool_choice is None else tool_choice}
    return None


def unenforced(enabled: bool, response_format) -> bool:
    """True when response_format asks for JSON output but the request would get no grammar key."""
    kind = response_format.get("type") if isinstance(response_format, dict) else None
    return kind in ("json_schema", "json_object") and grammar_spec(enabled, None, None, None, response_format) is None


def engine_key(spec: dict | None) -> str:
    """The engine protocol's ` grammar=<JSON>`: compact, ASCII, no space (the line is split at spaces)."""
    if not spec:
        return ""
    text = json.dumps(spec, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return " grammar=" + text.replace(" ", "\\u0020")


# ===== file: serve/api/logit_bias.py =====
"""OpenAI `logit_bias`: a {token id: bias} map added to the logits before sampling, checked before any streaming."""
from __future__ import annotations

import math
import re

# the engine-side key of a request; the handler stores the checked map there, so only Chat Completions sets it
REQUEST_KEY = "_logit_bias"


def normalize(value, vocab_size: int | None = None) -> dict[int, float]:
    """The request's `logit_bias` as {token id: bias}; ValueError naming the problem otherwise.  Biases are -100..100
    (-100 bans the token); an empty or absent map is no bias."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("logit_bias: expected an object mapping token ids to biases")
    out: dict[int, float] = {}
    for key, bias in value.items():
        if isinstance(key, bool) or not isinstance(key, (str, int)) or not re.fullmatch(r"[0-9]+", str(key)):
            raise ValueError(f"logit_bias: {key!r} is not a token id (a non-negative whole number)")
        token = int(key)
        if token > 2**31 - 1 or (vocab_size is not None and token >= vocab_size):
            raise ValueError(f"logit_bias: token id {token} is outside the model's vocabulary")
        if token in out:
            raise ValueError(f"logit_bias: token id {token} appears twice")
        if isinstance(bias, bool) or not isinstance(bias, (int, float)) or not -100 <= bias <= 100 \
                or not math.isfinite(bias):
            raise ValueError(f"logit_bias: the bias of token {token} must be a number from -100 to 100")
        out[token] = float(bias)
    if vocab_size is not None and sum(b == -100 for b in out.values()) >= vocab_size:
        raise ValueError("logit_bias: it bans every token")
    return out


def engine_key(bias: dict[int, float] | None) -> str:
    """The engine protocol's ` logit_bias=id:value,...` (sorted by id), or nothing.  Only a map with integer ids, as
    `normalize` makes it, is sent: a request body (whose JSON keys are strings) cannot carry one."""
    if not bias or not all(isinstance(i, int) and isinstance(b, float) for i, b in bias.items()):
        return ""
    return " logit_bias=" + ",".join(f"{i}:{b:g}" for i, b in sorted(bias.items()))


def checked(req: dict, engine) -> dict[int, float]:
    """The request's bias, refused when this engine cannot apply it.  The engine reports its vocabulary size as
    INFO logit_bias=N (absent from a started engine: no support).  An engine that has not started (lazy load) or has
    no `info` (the mock) is not judged."""
    info = getattr(engine, "info", None)
    started = isinstance(info, dict) and "context" in info
    vocab = info.get("logit_bias") if started else None
    known = isinstance(vocab, int) and vocab > 1
    bias = normalize(req.get("logit_bias"), vocab if known else None)
    if bias and started and not known:
        raise ValueError("logit_bias: this engine build cannot apply token biases")
    if bias and getattr(engine, "batch", 0):
        raise ValueError("logit_bias: not supported with parallel slots (--parallel)")
    return bias

# ===== file: serve/api/openai.py =====
"""OpenAI chat completion chunks, and their non-streaming collection."""
from __future__ import annotations

import json
import time
import uuid

from serve.frontend import Event
from serve.service import Service


def openai_chunks(svc: Service, req: dict, ids, thinking, tools, max_new, cancel, force=None):
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    model = svc.model

    def chunk(delta, finish=None):
        return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant", "content": ""})
    calls = 0
    streamed = {}                                  # tool call id -> index, for calls sent piece by piece
    finished = set()                               # ... and the ones whose final tool_call came
    for kind, x in svc.run(ids, thinking, tools, max_new, req, cancel, force=force):
        if kind == "ping":
            yield None
        elif kind == "event":
            ev: Event = x
            if ev.kind == "reasoning" and ev.text:
                yield chunk({"reasoning_content": ev.text})
            elif ev.kind == "content" and ev.text:
                yield chunk({"content": ev.text})
            elif ev.kind == "tool_start":
                streamed[ev.call.id] = calls
                calls += 1
                yield chunk({"tool_calls": [{"index": streamed[ev.call.id], "id": ev.call.id, "type": "function",
                                             "function": {"name": ev.call.name, "arguments": ""}}]})
            elif ev.kind == "tool_args":
                yield chunk({"tool_calls": [{"index": streamed[ev.call.id], "function": {"arguments": ev.text}}]})
            elif ev.kind == "tool_call" and ev.call.id in streamed:
                finished.add(ev.call.id)
            elif ev.kind == "tool_call":
                yield chunk({"tool_calls": [{"index": calls, "id": ev.call.id, "type": "function",
                                             "function": {"name": ev.call.name,
                                                          "arguments": json.dumps(ev.call.arguments, ensure_ascii=False)}}]})
                calls += 1
        else:
            # a call whose final tool_call never came was cut off: not "tool_calls"
            whole = calls and streamed.keys() <= finished
            finish = "tool_calls" if whole and x["finish"] == "stop" else {"cancel": "stop"}.get(x["finish"], x["finish"])
            last = chunk({}, finish)
            last["usage"] = {"prompt_tokens": len(ids), "completion_tokens": x["completion_tokens"],
                             "total_tokens": len(ids) + x["completion_tokens"],
                             # prompt tokens served from the conversation cache
                             "prompt_tokens_details": {"cached_tokens": min(x.get("reused") or 0, len(ids))}}
            if x.get("timings"):
                last["timings"] = x["timings"]          # llama.cpp's field: the speed its clients show
            yield last


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def openai_collect(chunks) -> dict:
    content, reasoning, by_index, last = [], [], {}, None
    for c in chunks:
        if c is None:                              # a heartbeat
            continue
        d = c["choices"][0]["delta"]
        content.append(d.get("content") or "")
        reasoning.append(d.get("reasoning_content") or "")
        for tc in d.get("tool_calls") or []:       # streamed calls arrive in pieces: merge them by index
            cur = by_index.setdefault(tc.get("index", len(by_index)), {"id": None, "type": "function",
                                                                        "function": {"name": "", "arguments": ""}})
            cur["id"] = tc.get("id") or cur["id"]
            fn = tc.get("function") or {}
            cur["function"]["name"] += fn.get("name") or ""
            cur["function"]["arguments"] += fn.get("arguments") or ""
        last = c
    calls = [by_index[i] for i in sorted(by_index)]
    if last["choices"][0]["finish_reason"] != "tool_calls":
        calls = [c for c in calls if _is_json(c["function"]["arguments"])]   # a call the output ended inside
    msg = {"role": "assistant", "content": "".join(content) or None}
    if "".join(reasoning):
        msg["reasoning_content"] = "".join(reasoning)
    if calls:
        msg["tool_calls"] = calls
    out = {"id": last["id"], "object": "chat.completion", "created": last["created"], "model": last["model"],
           "choices": [{"index": 0, "message": msg, "finish_reason": last["choices"][0]["finish_reason"]}],
           "usage": last["usage"]}
    if last.get("timings"):
        out["timings"] = last["timings"]
    return out

# ===== file: serve/api/responses.py =====
"""OpenAI's Responses API (POST /v1/responses), stateless, on the chat path.

A request becomes the chat path's template messages and tools; Service.run's events come back as Responses items and
typed SSE events.
  * Stateless: the client resends the whole conversation in `input` (`store: false`).
  * Leading `instructions`/`developer` messages merge into one system message so the cache prefix is stable.
  * Function, `namespace` and `custom` tools are supported; hosted tools are dropped.
  * Thinking returns as `reasoning_text` (no summaries) and, on request, as an opaque `encrypted_content` (base64,
    not encrypted); either form sent back is restored into the prompt.
  * Event order: response.created, response.in_progress, output_item.added, deltas, done events,
    output_item.done, then response.completed / incomplete / failed.

Based on a contribution by CC-David-CC (item/event assembler, namespace tools, tool-result ordering).
"""
from __future__ import annotations

import base64
import copy
import json
import time
import uuid

from serve.frontend import IMAGE_PARTS, Event, _late_system_to_user, _parts_of, effort_kwargs, stable_tool_order

ENCRYPTED_PREFIX = "basalt.r1:"                   # our own reasoning replay strings; others' are ignored
HOSTED_TOOLS = ("web_search", "web_search_preview", "file_search", "computer_use_preview", "computer_use",
                "code_interpreter", "image_generation", "local_shell", "mcp", "tool_search")


class ResponsesError(ValueError):
    """A request error in the Responses format: {"error": {"message", "type", "param", "code"}}."""

    def __init__(self, message, param=None, code=None, status=400, kind="invalid_request_error"):
        super().__init__(message)
        self.param, self.code, self.status, self.kind = param, code, status, kind

    def body(self):
        return error_body(str(self), self.kind, self.param, self.code)


def error_body(message, kind="invalid_request_error", param=None, code=None) -> dict:
    return {"error": {"message": message, "type": kind, "param": param, "code": code}}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# ------------------------------------------------------------------------------------------------ request -> chat
def _content(content, param):
    """A message's (or a tool output's) content -> the template's form: a string, or text and image parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ResponsesError("expected a string or an array of content parts", param)
    parts = []
    for j, part in enumerate(content):
        if isinstance(part, str):
            parts.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            raise ResponsesError("expected a content part object", f"{param}[{j}]")
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text", "summary_text", "reasoning_text"):
            parts.append({"type": "text", "text": part.get("text") or ""})
        elif kind == "refusal":
            parts.append({"type": "text", "text": part.get("refusal") or ""})
        elif kind in IMAGE_PARTS:
            if not part.get("image_url"):
                print("[server] error: an image part has no image_url", flush=True)
                raise ResponsesError("only images given as image_url (a data: or http(s) URL) are supported; "
                                     "this server keeps no files", f"{param}[{j}]", "unsupported_parameter")
            parts.append({"type": "input_image", "image_url": part["image_url"]})
        else:
            raise ResponsesError(f"content parts of type {kind!r} are not supported (text and images are)",
                                 f"{param}[{j}].type", "unsupported_parameter")
    return _parts_of(parts)


def _reasoning_text(item, param) -> str:
    """The thinking of a reasoning item: reasoning_text content, else our own encrypted_content."""
    texts = [p.get("text") or "" for p in item.get("content") or [] if isinstance(p, dict)
             and p.get("type") in ("reasoning_text", "text")]
    if texts:
        return "".join(texts)
    enc = item.get("encrypted_content")
    if isinstance(enc, str) and enc.startswith(ENCRYPTED_PREFIX):
        try:
            return json.loads(base64.b64decode(enc[len(ENCRYPTED_PREFIX):], validate=True).decode("utf-8"))["text"]
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ResponsesError("this reasoning item's encrypted_content is damaged", param + ".encrypted_content",
                                 "invalid_encrypted_content") from None
    return ""


def _arguments(raw, param) -> dict:
    """A function_call's arguments as the mapping the template renders; non-object arguments are kept as text."""
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if not isinstance(raw, str):
        raise ResponsesError("function_call arguments must be a JSON string", param)
    try:
        value = json.loads(raw)
    except ValueError:
        return {"arguments": raw}
    return value if isinstance(value, dict) else {"arguments": value}


def _tool_output(item, param):
    out = item.get("output")
    if isinstance(out, dict):
        if out.get("type") in IMAGE_PARTS:
            return _content([out], param + ".output")
        return json.dumps(out, ensure_ascii=False)   # some clients send a JSON result as an object
    return out if isinstance(out, str) else _content(out, param + ".output")


def input_messages(req: dict) -> list[dict]:
    """`instructions` + `input` -> the chat template's messages."""
    messages = []
    instructions = req.get("instructions")
    if instructions not in (None, ""):
        messages.append({"role": "system", "content": _content(instructions, "instructions")
                         if not isinstance(instructions, str) else instructions})
    items = req.get("input")
    if items is None:
        raise ResponsesError("input is required: a string or an array of input items", "input", "missing_required_parameter")
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    if not isinstance(items, list):
        raise ResponsesError("input must be a string or an array of input items", "input")
    thinking = []                                    # reasoning waiting for the assistant turn it belongs to
    open_turn = None                                 # the assistant message the next function_call joins

    def turn(content=""):
        nonlocal open_turn
        open_turn = {"role": "assistant", "content": content}
        if thinking:
            open_turn["reasoning_content"] = "".join(thinking)
            thinking.clear()
        messages.append(open_turn)
        return open_turn

    for i, item in enumerate(items):
        param = f"input[{i}]"
        if not isinstance(item, dict):
            raise ResponsesError("expected an input item object", param)
        kind = item.get("type") or ("message" if "role" in item else None)
        if kind == "message":
            role = item.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                raise ResponsesError(f"unknown message role {role!r}", param + ".role")
            content = _content(item.get("content"), param + ".content")
            if role == "assistant":
                turn(content)
            else:
                open_turn = None
                thinking.clear()
                messages.append({"role": "system" if role == "developer" else role, "content": content})
        elif kind == "reasoning":
            text = _reasoning_text(item, param)
            if text:
                if open_turn is not None and (open_turn.get("tool_calls") or open_turn["content"]):
                    open_turn = None                 # thinking starts a new model turn
                thinking.append(text)
        elif kind in ("function_call", "custom_tool_call"):
            name = item.get("name")
            if not isinstance(name, str) or not name:
                raise ResponsesError("a function_call needs a name", param + ".name")
            if item.get("namespace"):
                name = f"{item['namespace']}.{name}"
            if kind == "function_call":
                args = _arguments(item.get("arguments"), param + ".arguments")
            else:
                args = {"input": item.get("input") if isinstance(item.get("input"), str) else ""}
            target = open_turn if open_turn is not None and not thinking else turn()
            target.setdefault("tool_calls", []).append({"function": {"name": name, "arguments": args},
                                                        "_call_id": item.get("call_id")})
        elif kind in ("function_call_output", "custom_tool_call_output"):
            messages.append({"role": "tool", "content": _tool_output(item, param), "_call_id": item.get("call_id")})
            open_turn = None
            thinking.clear()
        elif kind == "item_reference":
            raise ResponsesError("item references need stored responses, and this server keeps none: send the items "
                                 "themselves", param, "unsupported_parameter")
        else:
            raise ResponsesError(f"input items of type {kind!r} are not supported", param + ".type",
                                 "unsupported_parameter")
    _order_tool_results(messages)
    # leading system/developer messages merge into one system message; later ones become user messages
    lead = 0
    while lead < len(messages) and messages[lead]["role"] == "system" and isinstance(messages[lead]["content"], str):
        lead += 1
    if lead > 1:
        messages[:lead] = [{"role": "system", "content": "\n\n".join(m["content"] for m in messages[:lead])}]
    return _late_system_to_user(messages)


def _order_tool_results(messages):
    """Reorder each run of tool messages to match the preceding assistant turn's calls, then drop the private ids."""
    i = 0
    while i < len(messages):
        if messages[i]["role"] != "tool":
            i += 1
            continue
        j = i
        while j < len(messages) and messages[j]["role"] == "tool":
            j += 1
        prev = messages[i - 1] if i else {}
        order = [c.get("_call_id") for c in prev.get("tool_calls") or []]
        rank = {cid: k for k, cid in enumerate(order) if cid}
        messages[i:j] = sorted(messages[i:j], key=lambda m: rank.get(m.get("_call_id"), len(order)))
        i = j
    for m in messages:
        m.pop("_call_id", None)
        for c in m.get("tool_calls") or []:
            c.pop("_call_id", None)


def request_tools(req: dict):
    """-> (template tools or None, {template name: (namespace, name, kind)}, names of hosted tools left out)."""
    tools, names, skipped = [], {}, []
    given = req.get("tools") or []
    if not isinstance(given, list):
        raise ResponsesError("tools must be an array", "tools")

    def add(tool, param, namespace=None, ns_description=""):
        kind = tool.get("type")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ResponsesError("a tool needs a name", param + ".name")
        flat = f"{namespace}.{name}" if namespace else name
        description = tool.get("description") or ""
        if ns_description:
            description = ns_description + ("\n\n" + description if description else "")
        if kind == "function":
            params = tool.get("parameters")
            if params is not None and not isinstance(params, dict):
                raise ResponsesError(f'tool "{name}": "parameters" must be an object (its JSON schema)',
                                     param + ".parameters")
            params = params or {"type": "object", "properties": {}}
        else:                                        # custom: free-form text, maybe in a grammar the model reads
            fmt = tool.get("format") or {}
            if isinstance(fmt, dict) and fmt.get("type") == "grammar" and fmt.get("definition"):
                description += f"\n\nThe input must follow this {fmt.get('syntax', '')} grammar:\n{fmt['definition']}"
            params = {"type": "object", "properties": {"input": {"type": "string", "description": "the raw input"}},
                      "required": ["input"]}
        tools.append({"name": flat, "description": description, "parameters": params})
        names[flat] = (namespace, name, kind)

    for i, tool in enumerate(given):
        param = f"tools[{i}]"
        if not isinstance(tool, dict):
            raise ResponsesError("expected a tool object", param)
        kind = tool.get("type")
        if kind in ("function", "custom"):
            add(tool, param)
        elif kind == "namespace":
            for j, member in enumerate(tool.get("tools") or []):
                if not isinstance(member, dict) or member.get("type", "function") not in ("function", "custom"):
                    raise ResponsesError("a namespace holds function tools", f"{param}.tools[{j}]")
                add({"type": "function", **member}, f"{param}.tools[{j}]", tool.get("name"),
                    tool.get("description") or "")
        elif kind in HOSTED_TOOLS or isinstance(kind, str):
            skipped.append(kind)                     # the model cannot run OpenAI's hosted tools: left out
        else:
            raise ResponsesError("a tool needs a type", param + ".type")
    choice = req.get("tool_choice")
    if choice == "none":
        return None, names, skipped
    return (stable_tool_order(tools, lambda t: t["name"]) if tools else None), names, skipped


def text_format(req: dict):
    """`text.format` -> the chat path's response_format (None: plain text)."""
    text = req.get("text")
    if text is None:
        return None
    if not isinstance(text, dict):
        raise ResponsesError("text must be an object", "text")
    fmt = text.get("format")
    if fmt is None:
        return None
    if not isinstance(fmt, dict):
        raise ResponsesError("text.format must be an object", "text.format")
    kind = fmt.get("type")
    if kind in (None, "text"):
        return None
    if kind == "json_object":
        return {"type": "json_object"}
    if kind == "json_schema":
        spec = {k: fmt[k] for k in ("name", "schema", "strict", "description") if k in fmt}
        return {"type": "json_schema", "json_schema": spec}
    raise ResponsesError("text.format.type must be text, json_object or json_schema", "text.format.type")


def check_request(req: dict) -> None:
    """What this stateless server cannot do, refused before anything runs."""
    if req.get("previous_response_id"):
        raise ResponsesError("this server keeps no responses (stateless): send the whole conversation in input "
                             "instead of previous_response_id", "previous_response_id", "unsupported_parameter")
    if req.get("conversation"):
        raise ResponsesError("this server keeps no conversations (stateless): send the whole conversation in input",
                             "conversation", "unsupported_parameter")
    if req.get("background"):
        raise ResponsesError("background responses are not supported: use stream or wait for the answer",
                             "background", "unsupported_parameter")
    if req.get("prompt"):
        raise ResponsesError("stored prompt templates are not supported: send instructions and input",
                             "prompt", "unsupported_parameter")
    n = req.get("max_output_tokens")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
        raise ResponsesError("max_output_tokens must be a positive whole number", "max_output_tokens")
    reasoning = req.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise ResponsesError("reasoning must be an object", "reasoning")


def template_kwargs(req: dict) -> dict:
    effort = (req.get("reasoning") or {}).get("effort")
    try:
        return effort_kwargs(effort)
    except ValueError as e:
        raise ResponsesError(str(e), "reasoning.effort") from None


# ------------------------------------------------------------------------------------------------ output
class Assembler:
    """Builds one response's output items and typed events from Service.run's events."""

    def __init__(self, req: dict, model: str, prompt_tokens: int, names: dict, encrypted: bool):
        self.names, self.encrypted = names, encrypted
        self.prompt_tokens = prompt_tokens
        self.seq = 0
        self.item = None                             # the open output item
        reasoning = req.get("reasoning") if isinstance(req.get("reasoning"), dict) else {}
        self.response = {
            "id": new_id("resp"), "object": "response", "created_at": int(time.time()), "status": "in_progress",
            "background": False, "error": None, "incomplete_details": None,
            "instructions": req.get("instructions"), "max_output_tokens": req.get("max_output_tokens"),
            "model": model, "output": [], "parallel_tool_calls": req.get("parallel_tool_calls", True),
            "previous_response_id": None,
            "reasoning": {"effort": reasoning.get("effort"), "summary": reasoning.get("summary")},
            "store": False, "temperature": req.get("temperature"),
            "text": req.get("text") if isinstance(req.get("text"), dict) else {"format": {"type": "text"}},
            "tool_choice": req.get("tool_choice", "auto"), "tools": req.get("tools") or [],
            "top_p": req.get("top_p"), "truncation": "disabled", "usage": None, "user": None,
            "metadata": req.get("metadata") or {}}

    # --- events
    def event(self, kind, **data) -> dict:
        e = {"type": kind, "sequence_number": self.seq, **data}
        self.seq += 1
        return e

    def snapshot(self) -> dict:
        return copy.deepcopy(self.response)

    def start(self) -> list[dict]:
        return [self.event("response.created", response=self.snapshot()),
                self.event("response.in_progress", response=self.snapshot())]

    def in_progress(self) -> dict:
        """A keep-alive event; SSE comments would not reset a client's idle timer."""
        return self.event("response.in_progress", response=self.snapshot())

    def _open(self, item) -> list[dict]:
        self.response["output"].append(item)
        self.item = item
        out = [self.event("response.output_item.added", output_index=self.index, item=copy.deepcopy(item))]
        if item["type"] == "message":
            part = {"type": "output_text", "text": "", "annotations": [], "logprobs": []}
            item["content"].append(part)
            out.append(self.event("response.content_part.added", item_id=item["id"], output_index=self.index,
                                  content_index=0, part=copy.deepcopy(part)))
        return out

    @property
    def index(self) -> int:
        return len(self.response["output"]) - 1

    def close(self, status="completed") -> list[dict]:
        item, self.item = self.item, None
        if item is None:
            return []
        at, out = self.response["output"].index(item), []
        item["status"] = status
        if item["type"] == "message":
            part = item["content"][0]
            out += [self.event("response.output_text.done", item_id=item["id"], output_index=at, content_index=0,
                               text=part["text"], logprobs=[]),
                    self.event("response.content_part.done", item_id=item["id"], output_index=at, content_index=0,
                               part=copy.deepcopy(part))]
        elif item["type"] == "reasoning":
            text = item["content"][0]["text"]
            out.append(self.event("response.reasoning_text.done", item_id=item["id"], output_index=at,
                                  content_index=0, text=text))
            if self.encrypted:
                item["encrypted_content"] = ENCRYPTED_PREFIX + base64.b64encode(
                    json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")).decode("ascii")
        elif item["type"] == "function_call":
            out.append(self.event("response.function_call_arguments.done", item_id=item["id"], output_index=at,
                                  name=item["name"], arguments=item["arguments"]))
        elif item["type"] == "custom_tool_call":
            out.append(self.event("response.custom_tool_call_input.done", item_id=item["id"], output_index=at,
                                  input=item["input"]))
        out.append(self.event("response.output_item.done", output_index=at, item=copy.deepcopy(item)))
        return out

    def feed(self, ev: Event) -> list[dict]:
        out = []
        kind = ev.kind
        if kind == "reasoning":
            if not ev.text:
                return out
            if self.item is None or self.item["type"] != "reasoning":
                out += self.close()
                out += self._open({"id": new_id("rs"), "type": "reasoning", "status": "in_progress", "summary": [],
                                   "content": [{"type": "reasoning_text", "text": ""}]})
            self.item["content"][0]["text"] += ev.text
            out.append(self.event("response.reasoning_text.delta", item_id=self.item["id"], output_index=self.index,
                                  content_index=0, delta=ev.text))
        elif kind == "content":
            if not ev.text:
                return out
            if self.item is None or self.item["type"] != "message":
                out += self.close()
                out += self._open({"id": new_id("msg"), "type": "message", "status": "in_progress",
                                   "role": "assistant", "content": []})
            self.item["content"][0]["text"] += ev.text
            out.append(self.event("response.output_text.delta", item_id=self.item["id"], output_index=self.index,
                                  content_index=0, delta=ev.text, logprobs=[]))
        elif kind == "tool_start":
            out += self.close()
            out += self._open(self._call_item(ev))
        elif kind == "tool_args":
            item = self.item
            if item is None or item.get("call_id") != ev.call.id:
                return out
            if item["type"] == "function_call":         # a custom tool's input is sent whole, at its end
                item["arguments"] += ev.text
                out.append(self.event("response.function_call_arguments.delta", item_id=item["id"],
                                      output_index=self.index, delta=ev.text))
        elif kind == "tool_call":
            item = self.item
            if item is None or item.get("call_id") != ev.call.id:     # a call that was not announced while written
                out += self.close()
                out += self._open(self._call_item(ev))
                item = self.item
                if item["type"] == "function_call":
                    item["arguments"] = json.dumps(ev.call.arguments, ensure_ascii=False)
                    out.append(self.event("response.function_call_arguments.delta", item_id=item["id"],
                                          output_index=self.index, delta=item["arguments"]))
            if item["type"] == "custom_tool_call":
                value = ev.call.arguments.get("input") if isinstance(ev.call.arguments, dict) else None
                item["input"] = value if isinstance(value, str) else json.dumps(ev.call.arguments, ensure_ascii=False)
                out.append(self.event("response.custom_tool_call_input.delta", item_id=item["id"],
                                      output_index=self.index, delta=item["input"]))
            out += self.close()
        return out

    def _call_item(self, ev: Event) -> dict:
        namespace, name, kind = self.names.get(ev.call.name, (None, ev.call.name, "function"))
        if kind == "custom":
            item = {"id": new_id("ctc"), "type": "custom_tool_call", "status": "in_progress", "call_id": ev.call.id,
                    "name": name, "input": ""}
        else:
            item = {"id": new_id("fc"), "type": "function_call", "status": "in_progress", "call_id": ev.call.id,
                    "name": name, "arguments": ""}
        if namespace:
            item["namespace"] = namespace
        return item

    def finish(self, done: dict) -> list[dict]:
        """Close open items, then response.completed (or .incomplete on length)."""
        length = done.get("finish") == "length"
        # an open call was cut off: incomplete whatever the finish
        unfinished = self.item is not None and self.item["type"] in ("function_call", "custom_tool_call")
        out = self.close("incomplete" if length or unfinished else "completed")
        n = done.get("completion_tokens", 0)
        prompt = done.get("prompt_tokens", self.prompt_tokens)
        self.response["usage"] = {
            "input_tokens": prompt, "input_tokens_details": {"cached_tokens": min(done.get("reused") or 0, prompt)},
            "output_tokens": n, "output_tokens_details": {"reasoning_tokens": done.get("reasoning_tokens", 0)},
            "total_tokens": prompt + n}
        if length:
            self.response["status"] = "incomplete"
            self.response["incomplete_details"] = {"reason": "max_output_tokens"}
            return out + [self.event("response.incomplete", response=self.snapshot())]
        self.response["status"] = "completed"
        self.response["completed_at"] = int(time.time())
        return out + [self.event("response.completed", response=self.snapshot())]

    def failed(self, message: str, code: str = "server_error") -> dict:
        """response.failed: an error after the stream started (the HTTP status is already sent)."""
        if self.item is not None:
            self.item["status"] = "incomplete"
            self.item = None
        self.response["status"] = "failed"
        self.response["error"] = {"code": code, "message": message}
        return self.event("response.failed", response=self.snapshot())


def collect(events) -> dict:
    """The non-streaming answer: the final response object."""
    last = None
    for e in events:
        if e is not None:
            last = e
    return last["response"]

# ===== file: serve/api/structured.py =====
"""JSON response formats: the request is checked here; the engine enforces the schema with a grammar."""
import json


def check_format(response_format) -> bool:
    """True for a JSON response format (json_object or json_schema), False for plain text. ValueError for a request
    the server cannot take."""
    if response_format is None:
        return False
    if not isinstance(response_format, dict):
        raise ValueError("response_format must be an object")
    kind = response_format.get("type")
    if kind == "text":
        return False
    if kind == "json_object":
        return True
    if kind != "json_schema":
        raise ValueError("response_format.type must be text, json_object or json_schema")
    spec = response_format.get("json_schema")
    if not isinstance(spec, dict) or not isinstance(spec.get("schema"), dict):
        raise ValueError("response_format.json_schema needs a schema object")
    if not isinstance(spec.get("name"), str) or not spec["name"]:
        raise ValueError("response_format.json_schema needs a name")
    if "strict" in spec and not isinstance(spec["strict"], bool):
        raise ValueError("response_format.json_schema.strict must be boolean")
    if spec["schema"].get("type") != "object":
        raise ValueError("response_format schema must have type object at its root")

    def check_refs(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("$ref", "$dynamicRef") and isinstance(value, str) and not value.startswith("#"):
                    raise ValueError("response_format supports only local schema references (#...)")
                check_refs(value)
        elif isinstance(node, list):
            for value in node:
                check_refs(value)
    check_refs(spec["schema"])
    return True


def anthropic_format(req: dict):
    """The chat path's response_format for an Anthropic request's `output_config.format` (or the older top-level
    `output_format`), or None for plain text. ValueError for a request the server cannot take."""
    out = req.get("output_config")
    fmt = out.get("format") if isinstance(out, dict) else None
    where = "output_config.format"
    if fmt is None and req.get("output_format") is not None:
        fmt, where = req["output_format"], "output_format"
    if fmt is None:
        return None
    if not isinstance(fmt, dict) or fmt.get("type") != "json_schema":
        raise ValueError(f"{where}.type must be json_schema")
    response_format = {"type": "json_schema", "json_schema": {"name": "output", "schema": fmt.get("schema")}}
    try:
        check_format(response_format)
    except ValueError as e:
        raise ValueError(str(e).replace("response_format.json_schema", where).replace("response_format", where)) from None
    return response_format

# ===== file: serve/config.py =====
"""Server settings: the command-line parser, the --config file and engine flag checks."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from serve import ROOT
from serve.engine.flags import flag_index
from serve.engine.image_source import parse_url_host
from serve.engine.vision import VISION_MODES, VISION_USAGE, vision_mode
from serve.util.console import tagged


MIN_SLOT_CONTEXT = 256          # the least context a slot is given when --max-context is shared
CONFIG_VERSION = 2                          # the config file's "version"; a file without one is this format's first
PARALLEL_MAX = 8                            # the engine's batch slots
CONFIG_PATHS = ("model", "tokenizer", "image-dir")       # relative to the config file
# a model entry's own, and what it may also inherit from the top-level "engine"; other server options are the server's
ENTRY_OPTIONS = frozenset({"model", "alias"})
MODEL_OPTIONS = frozenset({
    "gpu", "vision", "parallel", "slot-full-context", "idle-sleep", "idle-unload", "min-free-vram-mib", "fit-max-tokens",
    "grammar", "reasoning-budget-tokens", "thinking-loop-tokens", "answer-room", "temperature", "top-p", "top-k", "min-p",
    "presence-penalty", "frequency-penalty", "repetition-penalty", "penalty-last-n", "seed"})
GRAMMAR_DEFAULT = True                      # the `grammar` option when neither the file nor the command line sets it


def engine_flags_of(engine_bin: str) -> set[str] | None:
    """The long flags the engine binary accepts; None when it cannot be read."""
    try:
        with open(engine_bin, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return {m.decode() for m in re.findall(rb"(?<![\x21-\x7e])--[a-z0-9][a-z0-9-]*(?![\x21-\x7e])", data)}


def server_options(ap: argparse.ArgumentParser) -> dict:
    """{name: action} of the server's own options (names without the dashes), apart from --config and --help."""
    return {o[2:]: act for act in ap._actions for o in act.option_strings
            if o.startswith("--") and o[2:] not in ("help", "config")}


def grammar_words(known: set[str] | None) -> list[str]:
    """The engine words that make grammar enforcement available: `--grammar on` whenever the engine binary has the
    flag (a request's grammar key enforces; the server option only decides which requests carry one), so an engine
    without it still starts."""
    return ["--grammar", "on"] if known is not None and "--grammar" in known else []


def split_engine_extras(extras: list[str]) -> list[str]:
    """The unknown command-line words, which go to the engine as typed: it parses its own flags and their values.
    A `--flag=VALUE` word is split in two, as the engine has no `=` form. ValueError for a leading word that is not
    a flag."""
    if extras and not (extras[0].startswith("--") and len(extras[0]) > 2):
        raise ValueError(f"unexpected {extras[0]!r}")
    out: list[str] = []
    for word in extras:
        flag, eq, value = word.partition("=")
        if eq and word.startswith("--") and len(flag) > 2:
            out += [flag, value]
        else:
            out.append(word)
    return out


def engine_words(key: str, value) -> list[str]:
    """The engine words for one config entry: a switch is true (the flag) or false (nothing), a string or number is
    one value, and a list gives one word per item (a flag that takes several values). ValueError otherwise."""
    flag = "--" + str(key).strip().lower().replace("_", "-").removeprefix("--")
    if isinstance(value, bool):
        return [flag] if value else []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (str, int, float)) or str(item).strip() == "":
            raise ValueError(f"engine.{key}: expected a string or number, a list of them for several values, "
                             f"or true/false for a switch, not {value!r}")
    if not items:
        raise ValueError(f"engine.{key}: an empty list gives the flag no value")
    return [flag, *(str(item) for item in items)]


@dataclass
class ModelEntry:
    """One entry of "models": its server options as command-line words, the engine words of the top-level "engine" (taken
    where the model has the flag) and its own (a flag it lacks stops the start)."""
    path: str
    argv: list[str] = field(default_factory=list)
    shared_words: list[str] = field(default_factory=list)
    own_words: list[str] = field(default_factory=list)


@dataclass
class ConfigFile:
    server_argv: list[str]
    models: list[ModelEntry]


def option_words(where: str, key, value, act, here: Path) -> list[str]:
    """The command-line words of one server option from a config file, so every command-line check applies."""
    name = config_name(key)
    flag = "--" + name
    if name == "vision":                               # a mode; booleans are refused
        try:
            return ["--vision", vision_mode(value)]
        except ValueError as e:
            raise ValueError(f"{where}.{key}: {e}") from None
    if act.nargs == 0:                                 # a switch
        if not isinstance(value, bool):
            raise ValueError(f"{where}.{key}: true or false")
        return [flag] if value else ([f"--no-{name}"] if isinstance(act, argparse.BooleanOptionalAction) else [])
    items = value if isinstance(value, list) else [value]
    if isinstance(value, list) and not isinstance(act, argparse._AppendAction):
        raise ValueError(f"{where}.{key}: one value, not a list")
    words: list[str] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ValueError(f"{where}.{key}: expected a string or number, not {item!r}")
        item = str(item)
        if name in CONFIG_PATHS and item:
            item = str((here / os.path.expanduser(item)))
        words += [flag, item]
    return words


def config_name(key) -> str:
    return str(key).strip().lower().replace("_", "-").removeprefix("--")


def read_config(path: str) -> dict:
    """The file's object, of this format and version; ValueError for anything else."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise ValueError(f"cannot read: {e.strerror or e}") from None
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON: {e}") from None
    if not isinstance(raw, dict):
        raise ValueError('expected {"version": ' + str(CONFIG_VERSION) + ', "server": {...}, "engine": {...}, "models": [...]}')
    version = raw.get("version", CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError('"version" must be a whole number')
    if version > CONFIG_VERSION:
        raise ValueError(f"this is a v{version} config; this Basalt reads v{CONFIG_VERSION}")
    if version < CONFIG_VERSION or "models" not in raw or "v" in raw:
        old = Path(path).resolve()
        new = old.with_name(f"{old.stem}.v{CONFIG_VERSION}{old.suffix}")
        raise ValueError(f"Basalt requires v{CONFIG_VERSION} config, please upgrade: "
                         f"python3 {ROOT / 'tools/migrations/config_v1_to_v2.py'} {old} -o {new}")
    extra = set(raw) - {"version", "server", "engine", "models"}
    if extra:
        raise ValueError(f"unexpected key {sorted(extra)[0]!r}")
    return raw


def load_config_file(path: str, ap: argparse.ArgumentParser) -> ConfigFile:
    """A --config JSON file {"version": 2, "server": {...}, "engine": {...}, "models": [{...}]}. `server` holds what the
    whole server shares; `engine` what applies to every model that takes it; each entry of `models` names one model, and
    its settings win over `engine`'s. Engine flags map to values (true: switch, false: off, a list: several values). Paths
    are relative to the file; ValueError for anything else."""
    raw = read_config(path)
    server, shared, entries = raw.get("server", {}), raw.get("engine", {}), raw["models"]
    for k, v in (("server", server), ("engine", shared)):
        if not isinstance(v, dict):
            raise ValueError(f'"{k}" must be an object')
    if not isinstance(entries, list) or not entries or not all(isinstance(e, dict) for e in entries):
        raise ValueError('"models" must be a list of at least one {"model": FILE, ...}')
    for section in (server, shared, *entries):
        for key in section:
            if re.sub(r"[^a-z]", "", str(key).lower()).startswith("apikey"):
                raise ValueError("api-key can't be set in a config file; pass --api-key on the command line")
    here = Path(path).resolve().parent
    known = server_options(ap)
    server_argv: list[str] = []
    for key, value in server.items():
        name = config_name(key)
        if name == "engine-bin":
            raise ValueError("server.engine-bin: engine-bin is set by basalt serve")
        if name in ENTRY_OPTIONS or name in MODEL_OPTIONS:
            raise ValueError(f'server.{key}: a model setting, set it under "engine" or in a model entry')
        if name not in known:
            raise ValueError(f'server.{key}: not a server option (engine flags go under "engine")')
        server_argv += option_words("server", key, value, known[name], here)
    shared_argv: list[str] = []
    shared_words: list[str] = []
    for key, value in shared.items():
        name = config_name(key)
        if name in ENTRY_OPTIONS:
            raise ValueError(f"engine.{key}: set it in a model entry")
        if name in MODEL_OPTIONS:
            shared_argv += option_words("engine", key, value, known[name], here)
        elif name in known:
            raise ValueError(f'engine.{key}: a server option, set it under "server"')
        else:
            shared_words += engine_words(key, value)
    models: list[ModelEntry] = []
    for i, entry in enumerate(entries):
        where = f"models[{i}]"
        if not isinstance(entry.get("model"), str) or not entry["model"].strip():
            raise ValueError(f'{where}: "model" names the model file')
        out = ModelEntry(str(here / os.path.expanduser(entry["model"])), list(shared_argv), list(shared_words))
        for key, value in entry.items():
            name = config_name(key)
            if name in ENTRY_OPTIONS or name in MODEL_OPTIONS:
                out.argv += option_words(where, key, value, known[name], here)
            elif name in known:
                raise ValueError(f'{where}.{key}: a server option, set it under "server"')
            else:
                out.own_words += engine_words(key, value)
        models.append(out)
    return ConfigFile(server_argv, models)


def model_settings(ap: argparse.ArgumentParser, cli: list[str], refused=None) -> list[tuple]:
    """parse_settings() for each model the server runs: the --config file's entries, else the command line's one.
    `refused(path, group)` says a model has no such engine flag; a top-level setting it refuses is left out of that model's
    words and named in `a.skipped`. With --config the command line's settings apply to every entry."""
    a_cli = ap.parse_known_args(cli)[0]
    if not a_cli.config:
        return [parse_settings(ap, cli)]
    given = [f for f, v in (("--model", a_cli.model), ("--alias", a_cli.alias)) if v]
    if given:
        verb = "is" if len(given) == 1 else "are"
        raise ValueError(f"{' and '.join(given)} {verb} not supported when using a config file")
    cfg = load_config_file(a_cli.config, ap)
    out = []
    for entry in cfg.models:
        kept, skipped = [], []
        for group in engine_groups(entry.shared_words):
            if refused is not None and refused(entry.path, group):
                skipped.append(group[0])
            else:
                kept += group
        settings = parse_settings(ap, cli, cfg.server_argv, entry.argv, kept + entry.own_words)
        settings[0].skipped = skipped
        out.append(settings)
    return out


def parse_settings(ap: argparse.ArgumentParser, cli: list[str], server_argv=(), model_argv=(), words=()):
    """(args, argv, file_words, cli_words): parsed options, their source words and the engine words from each.
    The engine reads its flags in order and a later one wins, so file words then command-line words keep
    command line > config file > defaults; SystemExit(2) for a bad value, ValueError for a stray word or a clash."""
    a_cli, extras = ap.parse_known_args(cli)
    cli_words = split_engine_extras(extras)
    file_argv, file_words = [*server_argv, *model_argv], list(words)
    a = ap.parse_known_args(file_argv + cli)[0]
    a.skipped = []
    a.sampling_launch = {k: getattr(a, k) for k in SAMPLING_KEYS if getattr(a, k) is not None}
    a.sampling_cli = {k for k in a.sampling_launch if getattr(a_cli, k) is not None}
    if a.vision == "":                                     # a bare --vision
        ap.error(VISION_USAGE)
    for act in ap._actions:                                # a repeatable command-line option replaces the file's
        if isinstance(act, argparse._AppendAction) and getattr(a_cli, act.dest) != act.default:
            setattr(a, act.dest, getattr(a_cli, act.dest))
    if a.parallel and a.parallel > 1:
        for flag in ("--batch", "--slots"):
            if flag in file_words or flag in cli_words:
                raise ValueError(f"parallel and engine {flag} both set: use parallel")
        (cli_words if a_cli.parallel else file_words).extend(["--batch", str(a.parallel)])
        if not a.slot_full_context:
            # --max-context is shared by the slots, as in llama.cpp: each holds an equal part (the solo path, used while
            # a request is alone, has the same length, so a request never outgrows its slot). Without the flag the
            # engine's default is shared, once the server has asked the engine for it (split_default).
            src = cli_words if "--max-context" in cli_words or "--max-context" not in file_words else file_words
            at = flag_index(src, "--max-context")
            if at is not None and at + 1 >= len(src):
                raise ValueError("--max-context needs a value")
            if at is None:
                a.split_default = True
            else:
                ctx = int(src[at + 1])
                src[at + 1] = str(slot_share(ctx, a.parallel))
                a.shared_context = ctx                     # the total, for the startup line
    return a, file_argv + cli, file_words, cli_words


def check_engine_flags(words: list[str], known: set[str] | None, where: str) -> None:
    """ValueError naming the first flag (a word starting with `--`) the engine rejects; value words are not names
    (unchecked when the binary is unreadable)."""
    for word in words:
        if word == "--gpu":
            raise ValueError(f"{where}: gpu is a server setting (--gpu, or \"gpu\" in the server section)")
        if known is not None and word.startswith("--") and word not in known:
            raise ValueError(f"{where}: {word} is not an engine flag")


def engine_groups(words: list[str]) -> list[list[str]]:
    """`words` as [flag, value, ...] groups, each starting at a word that begins with `--`."""
    groups: list[list[str]] = []
    for word in words:
        if word.startswith("--") or not groups:
            groups.append([word])
        else:
            groups[-1].append(word)
    return groups


# flag -> the console's name; a -mib or -gib flag's unit follows its value
ENGINE_NAMES = {"max-context": "ctx", "pool-workers": "pool", "conversation-cache-disk-mib": "disk-cache",
                "conversation-cache-mib": "ram-cache", "vram-reserve-mib": "reserve"}
UNIT_SUFFIXES = {"-mib": "MiB", "-gib": "GiB"}


def engine_item(group: list[str]) -> str:
    """One engine flag with its values as `name=value`; a switch is its bare name."""
    flag, values = group[0][2:], group[1:]
    unit = next((u for suffix, u in UNIT_SUFFIXES.items() if flag.endswith(suffix) and len(values) == 1), "")
    name = ENGINE_NAMES.get(flag, flag[:-4] if unit else flag)
    return f"{name}={','.join(values)}{unit}" if values else name


def start_items(file_words: list[str], cli_words: list[str], shared: tuple[int, int] | None = None,
                grammar: tuple[bool, str] | None = None, debug: bool = False) -> list[str]:
    """The engine words from --config and the command line as key=value items for a model's startup line, a flag given
    twice shown once with the value that wins. `shared` is (total, slots) when --max-context was divided among the slots;
    `grammar` is (on, origin), None when left at its default. The conversation folder shows only with --debug."""
    groups = engine_groups(file_words) + engine_groups(cli_words)
    last = {g[0]: i for i, g in enumerate(groups)}
    items = []
    for i, g in enumerate(groups):
        if last[g[0]] != i or (g[0] == "--conversation-cache-dir" and not debug):
            continue
        item = engine_item(g)
        if shared and g[0] == "--max-context":
            item += f" ({shared[0]}/{shared[1]} slots)"
        items.append(item)
    if grammar is not None:
        items.append(f"grammar={'on' if grammar[0] else 'off'}")
    return items


def slot_share(total: int, parallel: int) -> int:
    """Each of `parallel` slots' part of a shared --max-context."""
    return max(MIN_SLOT_CONTEXT, total // parallel)


def with_spec(items: list[str], spec) -> list[str]:
    """A model's items with `spec=N` the window the engine will run (its --gpu-plan answer), not the flag as given."""
    return items if not isinstance(spec, int) else [x for x in items if not x.startswith("spec=")] + [f"spec={spec}"]


def shared_line(shared: list[str], sleeps: list) -> tuple[str, bool]:
    """The items every model shares, with the idle sleep when all of several models sleep after the same time; and
    whether it took the sleep (the models then do not say it each)."""
    s = {x or 0 for x in sleeps}
    sleep = len(sleeps) > 1 and len(s) == 1 and next(iter(s)) > 0
    return " ".join(shared + ([f"sleep={next(iter(s)):g}s"] if sleep else [])), sleep


def model_line(name: str, width: int, items: list[str]) -> str:
    """A model's startup line: its name padded to the column of the longest name, then its items."""
    return f"[server] {name:<{width}} {' '.join(items)}".rstrip()


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _whole(v):
    return _num(v) and v == int(v)


# the engine's penalty window holds at most this many tokens (src/program/serve/state.hpp, kPenaltyWindowCap)
PENALTY_WINDOW_MAX = 4096
# sampling key -> (converter, test, what is expected); the model file, the launch options and every request share it
SAMPLING_KEYS = {
    "temperature": (float, lambda v: _num(v) and v >= 0, "a number >= 0 (0 = greedy)"),
    "top_p": (float, lambda v: _num(v) and 0 < v <= 1, "0 < top_p <= 1"),
    "min_p": (float, lambda v: _num(v) and 0 <= v <= 1, "0 <= min_p <= 1"),
    "top_k": (int, lambda v: _whole(v) and v >= -1, "an integer >= 0 (0 or -1 = off: every token)"),
    "presence_penalty": (float, _num, "a number"),
    "frequency_penalty": (float, _num, "a number"),
    "repetition_penalty": (float, lambda v: _num(v) and v > 0, "a number > 0 (1 = off)"),
    "penalty_last_n": (int, lambda v: _whole(v) and 0 <= v <= PENALTY_WINDOW_MAX,
                       f"an integer 0..{PENALTY_WINDOW_MAX} (the engine's penalty window)"),
    "seed": (int, lambda v: _whole(v) and -2 ** 63 <= v < 2 ** 64, "an integer"),
}


def checked_sampling(req: dict) -> None:
    """A request's sampling fields against SAMPLING_KEYS (the model file's rules): ValueError naming the first one out
    of range, so the request gets a 400 before streaming; a field left out or null takes the defaults.  A value is
    never changed: what passes reaches the engine as sent."""
    for key, (_, ok, expected) in SAMPLING_KEYS.items():
        value = req.get(key)
        if value is not None and not ok(value):
            raise ValueError(f"{key}: expected {expected}, not {value!r}")
    rd = req.get("relaxed_drafting")
    if rd is not None and rd is not False and not (_num(rd) and 0 <= rd <= 1):
        raise ValueError(f"relaxed_drafting: expected false or a number 0..1, not {rd!r}")


def sampling_defaults_from_config(cfg: dict) -> dict:
    """The model file's sampling defaults (its GGUF metadata's general.sampling.* keys, serve/engine/modelfile.py):
    defaults for fields a request omits (temperature, top_p, top_k, min_p,
    presence/repetition/frequency penalties, penalty_last_n, seed). Request fields win; a bad value refuses to
    start; unknown keys are named and ignored."""
    out = {}
    for key, value in (cfg.get("sampling") or {}).items():
        if value is None or (key == "top_k" and value == 0):   # 0: no top-k cut
            continue
        if key in SAMPLING_KEYS:
            convert, ok, expected = SAMPLING_KEYS[key]
            if not ok(value):
                raise SystemExit(f"[server] error: sampling.{key}={value!r}: expected {expected}")
            out[key] = convert(value)
        elif key == "experimental_speed_projection":
            if not isinstance(value, bool):
                raise SystemExit(f"[server] error: sampling.{key}={value!r}: expected true or "
                                 "false (the default for requests that leave it out, when the engine has the vector)")
            out[key] = value
        else:
            print(f"[server] warning: sampling.{key} is not a known key, ignored", flush=True)
    return out


def sampling_option(key: str):
    """argparse type for the launch option of one sampling key: the model file's check on the typed value."""
    convert, ok, expected = SAMPLING_KEYS[key]

    def parse(text: str):
        try:
            value = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected {expected}, not {text!r}") from None
        if not ok(value):
            raise argparse.ArgumentTypeError(f"expected {expected}, not {text!r}")
        return convert(value)
    return parse


SAMPLING_NAMES = {"temperature": "temp", "presence_penalty": "pres", "frequency_penalty": "freq",
                  "repetition_penalty": "rep"}      # the console's short names
SAMPLING_OFF = {"min_p": 0, "presence_penalty": 0, "frequency_penalty": 0, "repetition_penalty": 1, "top_k": -1}


def sampling_items(effective: dict) -> list[str]:
    """The effective sampling defaults as items, leaving out those that change nothing."""
    return [f"{SAMPLING_NAMES.get(k, k)}={v}" for k, v in effective.items()
            if not (k in SAMPLING_OFF and v == SAMPLING_OFF[k]) and not (k == "top_k" and v == 0)]


def vision_setting(text: str) -> str:
    """argparse type: one of the --vision modes."""
    try:
        return vision_mode(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def image_url_host(text: str) -> str:
    """argparse type: HOST or HOST:PORT an image URL from another machine may name."""
    try:
        parse_url_host(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return text.strip()


def parallel_count(text: str) -> int:
    """argparse type: a whole number of request slots, 1..PARALLEL_MAX."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number 1..{PARALLEL_MAX}, not {text!r}") from None
    if not 1 <= n <= PARALLEL_MAX:
        raise argparse.ArgumentTypeError(f"expected 1..{PARALLEL_MAX}, not {n}")
    return n


def budget_count(text: str) -> int:
    """argparse type: a token count, a whole number >= 0 (0: none)."""
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number >= 0, not {text!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"expected a whole number >= 0, not {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("--mock-engine", action="store_true",
                    help="run a scripted engine instead of the model, for clients and tests (no --model needed)")
    ap.add_argument("--model", help="the .basalt model file: the engine, tokenizer, sampling and vision come from it")
    ap.add_argument("--engine-bin", help=argparse.SUPPRESS)      # set by `basalt serve`
    ap.add_argument("--alias", help="the model name the API reports (default: the name stored in the model file)")
    ap.add_argument("--parallel", type=parallel_count, metavar="N",
                    help=f"requests served at once, 1..{PARALLEL_MAX} (default 1: one at a time); more requests wait "
                         "their turn. --max-context is shared: each slot holds that divided by N (so 4 slots of 262144 "
                         "need --max-context 1048576, or --slot-full-context). The slots' KV cache comes out of the "
                         "expert cache")
    ap.add_argument("--slot-full-context", action="store_true",
                    help="with --parallel, give every slot the whole --max-context instead of an equal part: N slots "
                         "cost N times the KV cache")
    ap.add_argument("--config", metavar="FILE",
                    help="a JSON file {\"server\": {...}, \"engine\": {...}, \"models\": [{\"model\": FILE, ...}]}: settings "
                         "named without the dashes. \"server\" holds what the server shares, \"engine\" what applies to "
                         "every model that takes it, a model entry that model's own; a list gives a flag several "
                         "values; the command line wins over it, for every model")
    ap.add_argument("--vision", nargs="?", const="", default="off", type=vision_setting, metavar="MODE|GPU",
                    help="image input: off (default), dynamic (inside the engine: it borrows expert-cache slots "
                         "on the main GPU while it reads an image, no standing VRAM), cpu (no VRAM, slow) or a "
                         "GPU number, as nvidia-smi numbers them: the encoder sidecar, always on that card")
    ap.add_argument("--image-url-host", action="append", default=[], metavar="HOST[:PORT]", type=image_url_host,
                    help="let requests from other machines name http(s) image URLs on exactly this host (repeatable; "
                         "without a port, 80 and 443). A name must resolve to public addresses; an IP address is "
                         "taken as written. Requests from this machine need no flag")
    ap.add_argument("--image-dir", action="append", default=[], metavar="DIR",
                    help="let requests from other machines name image files under this directory (repeatable). "
                         "Requests from this machine need no flag")
    ap.add_argument("--image-remote-only", action="store_true",
                    help="treat requests from this machine as remote too, so they get only data: URLs and what "
                         "--image-url-host and --image-dir allow. For a server behind anything on this machine that "
                         "forwards other people's requests")
    ap.add_argument("--host", default="127.0.0.1",
                    help="the address to listen on: 127.0.0.1 = this PC only (the default), 0.0.0.0 = also other "
                         "devices on your network (give --api-key)")
    ap.add_argument("--script", action="append",
                    help="the mock engine's answer (default: a short greeting); given more than once, requests get "
                         "them in turn and the last one repeats")
    ap.add_argument("--port", type=int, default=8080, help="the TCP port to listen on (default 8080)")
    ap.add_argument("--gpu", help="the GPUs to use, as nvidia-smi numbers them: exactly these, the first is the main "
                                  "card and the engine gives the rest their roles (\"0,1\"); auto = every Blackwell "
                                  "card, the fastest as main (default: GPU 0 alone)")
    ap.add_argument("--tokenizer", default=str(ROOT / "pack/full/tokenizer"),
                    help="tokenizer directory for the mock engine (a byte tokenizer if absent)")
    ap.add_argument("--open", action="store_true", help="open the local page in the browser once the model is ready")
    ap.add_argument("--fit-max-tokens", action="store_true",
                    help="clamp max_tokens to the remaining context instead of rejecting the request with 400")
    ap.add_argument("--grammar", action=argparse.BooleanOptionalAction, default=GRAMMAR_DEFAULT,
                    help="hold every tool call and JSON response format to a grammar in the engine (on by default; a request "
                         "with strict: true gets it either way); --no-grammar turns it off")
    ap.add_argument("--api-key", default="",
                    help="require this key on /v1/* (Authorization: Bearer ... or x-api-key); command line only, "
                         "not allowed in a --config file")
    ap.add_argument("--allowed-host", action="append", default=[], metavar="NAME",
                    help="also answer to this Host name (repeatable; \"*\" turns the check off)")
    ap.add_argument("--cors-origin", action="append", default=[], metavar="ORIGIN",
                    help="let web pages from this origin use /v1/* (repeatable)")
    ap.add_argument("--max-payload-mib", type=int, default=256, metavar="MIB",
                    help="the largest request body the server reads, in MiB; a bigger one gets a 413 (default 256)")
    ap.add_argument("--debug", action="store_true",
                    help="log one line per request and the raw model text after each reply")
    ap.add_argument("--request-lines", action="store_true",
                    help="echo the engine's per-request summary on stdout, for a supervisor that only sees this output")
    ap.add_argument("--lazy", action="store_true", help="start the API unloaded; load on first request; a sidecar image encoder is dropped, --vision dynamic stays")
    ap.add_argument("--idle-unload", type=float, default=None, metavar="SECONDS",
                    help="unload the model after this many seconds without requests; the next request loads it again")
    ap.add_argument("--idle-sleep", type=float, default=None, metavar="SECONDS",
                    help="put the model to sleep after this many seconds without requests: its VRAM is freed, its RAM "
                         "copy, conversations and learned expert placement stay, and the next request wakes it in a few "
                         "seconds (Linux; the driver suspends the engine's GPU state)")
    for key, text in (
            ("temperature", "0 = greedy; higher is more random"),
            ("top-p", "nucleus cutoff, 0 < value <= 1 (1 = off)"),
            ("top-k", "keep the K likeliest tokens (0 = off: every token)"),
            ("min-p", "drop tokens less likely than this times the likeliest, 0..1 (0 = off)"),
            ("presence-penalty", "flat penalty on a token seen in the window"),
            ("frequency-penalty", "penalty growing with how often a token was seen"),
            ("repetition-penalty", "multiplicative penalty on seen tokens, > 0 (1 = off)"),
            ("penalty-last-n", "the window the penalties look back over, in tokens (at most 4096)"),
            ("seed", "fix the random draw, an integer")):
        ap.add_argument(f"--{key}", type=sampling_option(key.replace("-", "_")), metavar="N", default=None,
                        help=f"default {key.replace('-', '_')} for requests that leave it out, over the model file's: {text}")
    ap.add_argument("--min-free-vram-mib", type=int, default=None,
                    help="load an unloaded model only when this much VRAM is free, else answer 503")
    ap.add_argument("--reasoning-budget-tokens", type=budget_count, default=None, metavar="N",
                    help="default thinking budget for requests that leave reasoning_budget_tokens out (0 = none): at "
                         "the budget the thinking is closed and the answer starts, so set it below the request's "
                         "max_tokens")
    ap.add_argument("--thinking-loop-tokens", type=budget_count, default=256, metavar="N",
                    help="end the thinking and let the model answer when it repeats one token this many times in a "
                         "row inside the thinking (0 = never)")
    ap.add_argument("--answer-room", action=argparse.BooleanOptionalAction, default=True,
                    help="reserve space for closing the thinking and the answer when the output cap is at least 1024 tokens; "
                         "an explicit request budget of 0 opts out")
    return ap

# ===== file: serve/engine/__init__.py =====
"""The engine side: the engine process and its mock, load narration, the image encoder process and the model file."""

# ===== file: serve/engine/errors.py =====
"""Errors raised for a request by the engine process and the server."""
from __future__ import annotations



class EngineDied(RuntimeError):
    """The engine process ended mid-request."""


class EngineStartError(EngineDied):
    """The engine ended before READY; the message is its error lines, else its last log line."""

    def __init__(self, message: str, log: str | None = None):
        super().__init__(message)
        self.log = log


class EngineStarting(RuntimeError):
    """The engine has not said READY yet, so there is no context size to plan with (a 503)."""


class ModelBusy(RuntimeError):
    """A model control was refused because requests are active or queued."""


class EngineSilent(EngineDied):
    """The engine was silent too long (or ignored a STOP) and the server ended it; the next request restarts it."""


class EngineError(ValueError):
    """The engine failed a request for a reason of its own (a CUDA error, a timed-out device wait): a 500, not the
    request's fault. A ValueError, so every path that ends a request on the engine's ERR line still does."""


class ClientGone(Exception):
    """The client left while its request waited for its model's turn."""


class EngineStuck(RuntimeError):
    """The engine survived QUIT, terminate and kill."""


class GpuBusy(RuntimeError):
    """The model is unloaded and free VRAM is below min_free_vram_mib, so the engine is not started."""

# ===== file: serve/engine/flags.py =====
"""Reading engine flags from a list of command-line words."""
from __future__ import annotations


def flag_index(words: list[str], flag: str) -> int | None:
    """The index of the last `flag` in `words` (the engine's parser lets a later one win), or None."""
    return next((i for i in range(len(words) - 1, -1, -1) if words[i] == flag), None)


def flag_value(words: list[str], flag: str) -> str | None:
    """The word after the last `flag`; None when absent or when no word follows."""
    i = flag_index(words, flag)
    return words[i + 1] if i is not None and i + 1 < len(words) else None

# ===== file: serve/engine/image_source.py =====
"""Where a request's images may come from, and the bounded read of each one.

A request from this machine may name any path or URL. Any other request gets `data:` URLs plus the hosts and
directories the operator allowed. `ImageSources()` is that remote policy with nothing allowed, so a caller that
passes no policy reads no file and fetches no URL. Which requests count as local: security.remote_reason.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import http.client
import ipaddress
import ntpath
import os
import queue
import re
import socket
import ssl
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from serve.security import is_ip

IMAGE_MAX_BYTES = 32 << 20                      # one image's bytes, from any source
IMAGE_READ_S = 60.0                             # one source's whole read, name lookup and connect included
USER_AGENT = "local-server"
USAGE = "an image must be a data: URL, an http(s) URL or a local file path"
POLICY = "image source refused by server policy"
DATA_WORKS = "A data: URL always works."
WHY_REMOTE = {"peer": "this client is not on the server's machine",
              "no-peer": "the client's address is unknown",
              "forwarded": "the request came through a proxy",
              "headers": "the request's headers are malformed",
              "host": "the request's Host is not a loopback name",
              "origin": "the request came from a web page",
              "remote-only": "the server treats every client as remote (--image-remote-only)"}
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]+://")     # two letters at least: `c://x` is a drive
_ENTRY = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9._-]+)(?::([0-9]{1,5}))?")
_UNSAFE_URL = re.compile(r"[^\x21-\x7e]|\\")
_WS = {ord(c): None for c in " \t\r\n\f\v"}
# not `ipaddress`'s is_global and friends: their answers differ between Python releases
_V4_NOT_PUBLIC = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.31.196.0/24", "192.52.193.0/24", "192.88.99.0/24", "192.168.0.0/16", "192.175.48.0/24",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"))
_V6_UNICAST = ipaddress.ip_network("2000::/3")          # everything else is special: mapped, link- and site-local, NAT64...
_V6_NOT_PUBLIC = tuple(ipaddress.ip_network(n) for n in (
    "2001::/23", "2001:db8::/32", "2002::/16", "2620:4f:8000::/48", "3fff::/20"))   # IETF, docs, 6to4, AS112, docs
_NT_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                *(f"{d}{n}" for d in ("COM", "LPT") for n in (*"0123456789", "\u00b9", "\u00b2", "\u00b3"))}


@dataclass(frozen=True)
class ImageSources:
    """What one request may name. `url_hosts`: (host, port or None for the scheme's default). `dirs`: real paths.
    `remote_only`: no request counts as local. `local`, `why` and `peer` come from the request's connection."""
    url_hosts: tuple[tuple[str, int | None], ...] = ()
    dirs: tuple[str, ...] = ()
    remote_only: bool = False
    local: bool = False
    why: str = "no-peer"
    peer: str = ""


class ImageRefused(ValueError):
    """A refused or failed source: the text is for the client, `log_line` for the operator."""

    def __init__(self, message: str, log_line: str):
        super().__init__(message)
        self.log_line = log_line


def shown(text) -> str:
    """Client-named text as one safe, short line: no line ends or terminal escapes get through."""
    return ascii(str(text)[:200])


def parse_url_host(text: str) -> tuple[str, int | None]:
    """A --image-url-host entry, HOST or HOST:PORT ([ADDR] for IPv6), as (host, port). No wildcards. ValueError."""
    m = _ENTRY.fullmatch(text.strip().lower())
    port = int(m.group(2)) if m and m.group(2) else None
    host = m.group(1) if m else ""
    if not host or host[0] == "." or host[-1] == "." or ".." in host or (port is not None and not 0 < port < 65536):
        raise ValueError(f"expected HOST or HOST:PORT, not {text!r}")
    if host.startswith("["):
        try:
            host = str(ipaddress.IPv6Address(host[1:-1]))
        except ValueError:
            raise ValueError(f"expected HOST or HOST:PORT, not {text!r}") from None
    return _canonical_host(host), port


def image_sources_of(hosts=(), dirs=(), remote_only: bool = False) -> ImageSources:
    """The operator's settings: --image-url-host, --image-dir, --image-remote-only. ValueError on a bad entry."""
    out = []
    for d in dirs:
        real = os.path.realpath(os.path.expanduser(d))
        if not os.path.isdir(real):
            raise ValueError(f"--image-dir {d}: not a directory")
        out.append(real)
    return ImageSources(url_hosts=tuple(dict.fromkeys(parse_url_host(h) for h in hosts)), dirs=tuple(dict.fromkeys(out)),
                        remote_only=remote_only)


def _canonical_host(host: str) -> str:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host


def is_public_address(text: str) -> bool:
    """An address on the public internet, by this module's own tables, so the answer is the same on every Python."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.version == 4:
        return not any(ip in net for net in _V4_NOT_PUBLIC)
    return not ip.scope_id and ip in _V6_UNICAST and not any(ip in net for net in _V6_NOT_PUBLIC)


def _refused(allowed: ImageSources, what: str, detail: str = "") -> ImageRefused:
    why = WHY_REMOTE.get(allowed.why, WHY_REMOTE["no-peer"])
    return ImageRefused(f"{POLICY}: {why}, {what}. {DATA_WORKS}",
                        f"{POLICY}: peer={shown(allowed.peer)} why={allowed.why} {detail}".rstrip())


def _failed(allowed: ImageSources, message: str, detail: str = "") -> ImageRefused:
    return ImageRefused(message, f"{message}: peer={shown(allowed.peer)} {detail}".rstrip())


def _local_failed(allowed: ImageSources, message: str, source: str, reason: str = "") -> ImageRefused:
    """A local client's specific error: it names the source and the reason. The log, which other clients may read, does
    not."""
    said = f"{message} ({reason})" if reason else message
    return ImageRefused(f"{said}: {shown(source)}", f"{message}: peer={shown(allowed.peer)}")


def _url_host(source: str) -> str:
    """The host of a URL, for the log: never its credentials, path or query."""
    try:
        return shown(urllib.parse.urlsplit(source).hostname or "")
    except ValueError:
        return "''"


# ------------------------------------------------------------------------------------------------ data: URLs
def _read_data(source: str) -> bytes:
    head, comma, payload = source.partition(",")
    if not comma:
        raise ValueError("a data: URL needs a comma before its payload")
    if ";base64" not in head.lower():
        raise ValueError("a data: URL must be base64")
    payload = payload.translate(_WS)                    # line-wrapped base64 is common
    if not payload:
        raise ValueError("a data: URL's payload is empty")
    if len(payload) > (IMAGE_MAX_BYTES + 2) // 3 * 4:   # checked before anything is decoded
        raise ValueError(f"the image is larger than {IMAGE_MAX_BYTES >> 20} MiB")
    try:
        return base64.b64decode(payload, validate=True)
    except ValueError:
        raise ValueError("a data: URL's payload is not valid base64") from None


# ------------------------------------------------------------------------------------------------ files
def file_url_path(source: str, windows: bool = os.name == "nt", relative: bool = False, decode: bool = True) -> str:
    """The path of a file: URL. `file://C:/x`, which harnesses build by joining strings, reads as the drive path.
    Anything else after `file://` is a host, a ValueError, unless `relative` reads it as a relative path."""
    rest = source[len("file:"):]
    if rest.startswith("//"):
        rest = rest[2:]
        drive = re.match(r"[A-Za-z]:[\\/]", rest)
        if rest[:10].lower() == "localhost/":
            rest = rest[9:]
        elif not rest.startswith("/") and not (windows and drive) and not relative:
            raise ValueError("a file: URL may not name another host")
    path = urllib.parse.unquote(rest) if decode else rest
    if windows and re.match(r"/[A-Za-z]:[\\/]", path):
        path = path[1:]
    return path


def windows_path_refused(path: str) -> bool:
    """Forms a remote request may not name on Windows: anything but a plain `C:\\...` path. UNC and device paths,
    drive-relative paths, alternate data streams, reserved device names and names Windows would silently rewrite."""
    if not re.match(r"[A-Za-z]:[\\/]", path) or ntpath.splitdrive(path)[0][1:] != ":":
        return True
    for part in re.split(r"[\\/]", path[3:]):
        if part in ("", ".", ".."):
            continue
        if re.search(r'[:*?"<>|\x00-\x1f]', part) or part != part.rstrip(". "):
            return True
        if part.split(".")[0].rstrip(" ").upper() in _NT_RESERVED:
            return True
    return False


def _read_handle(fd: int) -> bytes:
    """The bytes of an open regular file, at most the cap; the handle is closed. OverflowError past the cap, OSError
    for anything but a regular file."""
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        if info.st_size > IMAGE_MAX_BYTES:
            raise OverflowError
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        data = f.read(IMAGE_MAX_BYTES + 1)              # also for a file that grew, or that reports no size
    if len(data) > IMAGE_MAX_BYTES:
        raise OverflowError
    return data


def _open_flags() -> int:
    # a FIFO's open would block without O_NONBLOCK; regular files ignore it
    return (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0))


def _read_local_file(source: str, allowed: ImageSources) -> bytes:
    path = source
    if source[:6].lower() == "file:/":
        # percent-decoded, as a URL is; a client that joined "file://" and a path means a `%` in a name literally
        path = file_url_path(source, relative=True)
        if "%" in source:
            literal = file_url_path(source, relative=True, decode=False)
            path = path if os.path.exists(path) or not os.path.exists(literal) else literal
    if not path:
        raise ValueError(USAGE)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):     # before an open, which can have side effects on a device
            raise _local_failed(allowed, "the image path is not a regular file", path)
        return _read_handle(os.open(path, _open_flags()))
    except FileNotFoundError:
        raise _local_failed(allowed, "the image file does not exist", path) from None
    except OverflowError:
        raise _local_failed(allowed, f"the image is larger than {IMAGE_MAX_BYTES >> 20} MiB", path) from None
    except OSError as e:
        raise _local_failed(allowed, "the image file could not be read", path, e.strerror or "error") from None


def _within(path: str, root: str) -> bool:
    path, root = os.path.normcase(path), os.path.normcase(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _open_under(root: str, real: str) -> int:
    """Open `real`, a path with no symlinks below `root`, so that a component swapped for a symlink after the check
    is refused. Fd-relative where the platform has it. Elsewhere the opened file must be the one the path still
    names, with the path still resolving under `root`."""
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"):
        parts = [p for p in os.path.relpath(real, root).split(os.sep) if p != "."]
        # O_PATH: a directory that may be traversed but not listed is fine
        through = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_CLOEXEC
        walk = through | os.O_NOFOLLOW
        at = os.open(root, through)
        try:
            for part in parts[:-1]:
                below = os.open(part, walk, dir_fd=at)
                os.close(at)
                at = below
            return os.open(parts[-1], _open_flags() | os.O_NOFOLLOW, dir_fd=at)
        finally:
            os.close(at)
    fd = os.open(real, _open_flags())
    try:
        if os.path.samestat(os.fstat(fd), os.stat(real)) and _within(os.path.realpath(real), root):
            return fd
    except OSError:
        pass
    os.close(fd)
    raise PermissionError(13, "the path changed while it was opened")


def _read_allowed_file(source: str, allowed: ImageSources, windows: bool = os.name == "nt") -> bytes:
    """A file a remote request names: it must resolve under an --image-dir. Every refusal reads the same, whether the
    file exists or not, and nothing outside the directories is opened. The check is on the resolved path: the path as
    named may reach the directory through a symlink."""
    refused = _refused(allowed, "and the path is not a file under the directories allowed with --image-dir"
                       if allowed.dirs else "so a file path needs --image-dir on the server", "kind=path")
    if not allowed.dirs:
        raise refused
    try:
        path = file_url_path(source, windows) if source[:6].lower() == "file:/" else source
        if "\x00" in path or not os.path.isabs(path) or (windows and windows_path_refused(path)):
            raise refused
        real = os.path.realpath(path)
        root = next((r for r in allowed.dirs if _within(real, r)), None)
        if root is None or real == root:
            raise refused
        return _read_handle(_open_under(root, real))
    except OverflowError:
        raise _failed(allowed, f"the image is larger than {IMAGE_MAX_BYTES >> 20} MiB", "kind=path") from None
    except (OSError, ValueError) as e:
        if e is refused:
            raise
        raise refused from None


# ------------------------------------------------------------------------------------------------ URLs
class _Watch:
    """The connections of one read. `close` ends them, also from another thread, so a read blocked on one returns.
    A duplicate of each socket does it: wrapping a socket in TLS takes the original's descriptor away."""

    def __init__(self):
        self.lock, self.socks, self.closed = threading.Lock(), [], False

    def add(self, sock: socket.socket) -> None:
        dup = sock.dup()
        with self.lock:
            if not self.closed:
                self.socks.append(dup)
                return
        self._end(dup)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            socks, self.socks = self.socks, []
        for sock in socks:
            self._end(sock)

    @staticmethod
    def _end(sock: socket.socket) -> None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        sock.close()


class _Watched:
    """A urllib HTTP handler whose connections join a _Watch from the moment they exist."""
    watch: _Watch

    def do_open(self, http_class, req, **kw):
        def make(host, **args):
            conn = http_class(host, **args)
            create = conn._create_connection

            def watched(*a, **k):
                sock = create(*a, **k)
                self.watch.add(sock)
                return sock
            conn._create_connection = watched
            return conn
        return super().do_open(make, req, **kw)


class _WatchedHTTP(_Watched, urllib.request.HTTPHandler):
    pass


class _WatchedHTTPS(_Watched, urllib.request.HTTPSHandler):
    pass


def _read_response(r) -> bytes:
    """The response's bytes up to the cap. At the deadline _bounded ends the connection, which ends this loop."""
    parts, total = [], 0
    while True:
        piece = r.read1(1 << 16)
        if not piece:
            if getattr(r, "length", None):              # read1 does not report a body cut short
                raise http.client.IncompleteRead(b"", r.length)
            return b"".join(parts)
        total += len(piece)
        if total > IMAGE_MAX_BYTES:
            raise OverflowError
        parts.append(piece)


def _read_local_url(source: str, allowed: ImageSources, watch: _Watch) -> bytes:
    req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    handlers = _WatchedHTTP(), _WatchedHTTPS()
    for handler in handlers:
        handler.watch = watch
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=IMAGE_READ_S) as r:
            return _read_response(r)
    except OverflowError:
        raise _local_failed(allowed, f"the image is larger than {IMAGE_MAX_BYTES >> 20} MiB", source) from None
    except urllib.error.HTTPError as e:
        e.close()
        raise _local_failed(allowed, f"the image URL answered HTTP {e.code}", source) from None
    except (OSError, http.client.HTTPException, ValueError) as e:
        reason = getattr(e, "reason", None) or e
        raise _local_failed(allowed, "the image URL could not be read", source, shown(reason)) from None


def _resolve(host: str, port: int) -> list[str]:
    """The name's addresses, each once, in the resolver's order."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _connect(ip: str, port: int, timeout: float) -> socket.socket:
    """A TCP connection to exactly this address; no name lookup happens here."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((ip, port))
        return sock
    except BaseException:
        sock.close()
        raise


def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class _PinnedConnection(http.client.HTTPConnection):
    """Connects to the addresses that were checked, all of them within the timeout. The Host header and the TLS name
    and certificate check stay on the original host."""

    def __init__(self, host: str, port: int, addresses: list[str], tls: ssl.SSLContext | None, timeout: float,
                 watch: _Watch):
        super().__init__(host, port=port, timeout=timeout)
        self.addresses, self.tls, self.watch = addresses, tls, watch

    def connect(self):
        last = OSError("no address")
        deadline = time.monotonic() + self.timeout
        for ip in self.addresses:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("the connect timed out")
            try:
                sock = _connect(ip, self.port, left)
                break
            except OSError as e:
                last = e
        else:
            raise last
        try:
            self.watch.add(sock)
            self.sock = self.tls.wrap_socket(sock, server_hostname=self.host) if self.tls else sock
        except BaseException:
            sock.close()
            raise


class _PinnedHandler(urllib.request.AbstractHTTPHandler):
    def __init__(self, host: str, port: int, addresses: list[str], watch: _Watch):
        super().__init__()
        self.target = (host, port, addresses, watch)

    def _pinned(self, req, tls):
        host, port, addresses, watch = self.target
        return self.do_open(lambda _, timeout: _PinnedConnection(host, port, addresses, tls, timeout, watch), req)

    def http_open(self, req):
        return self._pinned(req, None)

    def https_open(self, req):
        return self._pinned(req, _tls_context())

    http_request = urllib.request.AbstractHTTPHandler.do_request_
    https_request = urllib.request.AbstractHTTPHandler.do_request_


def _read_allowed_url(source: str, allowed: ImageSources, watch: _Watch) -> bytes:
    """A URL a remote request names: exactly an allowed host and port. A name must resolve to public addresses only,
    and the connection goes to those; an IP literal the operator named is taken as written. No proxy, no redirect."""
    refused = _refused(allowed, "and the URL's host and port are not among those allowed with --image-url-host"
                       if allowed.url_hosts else "so an http(s) URL needs --image-url-host HOST on the server",
                       f"kind=url host={_url_host(source)}")
    if _UNSAFE_URL.search(source):
        raise refused
    try:
        parts = urllib.parse.urlsplit(source)
        host, port = _canonical_host((parts.hostname or "").lower()), parts.port
    except ValueError:
        raise refused from None
    if parts.username is not None or parts.password is not None or not host:
        raise refused
    default = 443 if parts.scheme == "https" else 80
    if not any(h == host and (p or default) == (port or default) for h, p in allowed.url_hosts):
        raise refused
    port = port or default
    fetch_failed = _failed(allowed, "the image URL could not be fetched", f"host={shown(host)}")
    if is_ip(host):
        addresses = [host]
    else:
        try:
            addresses = _resolve(host, port)
        except (OSError, UnicodeError) as e:
            fetch_failed.log_line += f" reason={shown(e)}"
            raise fetch_failed from None
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise ImageRefused(f"{POLICY}: the allowed host {shown(host)} resolves to a non-public address; name the "
                               f"address itself in --image-url-host to allow it. {DATA_WORKS}",
                               f"{POLICY}: peer={shown(allowed.peer)} host={shown(host)} non-public")
    opener = urllib.request.OpenerDirector()            # no proxy, redirect, ftp or file handler
    for handler in (_PinnedHandler(host, port, addresses, watch), urllib.request.HTTPDefaultErrorHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=IMAGE_READ_S) as r:
            return _read_response(r)
    except OverflowError:
        raise _failed(allowed, f"the image is larger than {IMAGE_MAX_BYTES >> 20} MiB", f"host={shown(host)}") from None
    except urllib.error.HTTPError as e:                 # with no redirect handler, every 3xx lands here too
        e.close()
        if 300 <= e.code < 400:
            raise ImageRefused(f"{POLICY}: the image URL redirected, and redirects are not followed for this client; "
                               f"name the final URL's host with --image-url-host on the server. {DATA_WORKS}",
                               f"{POLICY}: peer={shown(allowed.peer)} redirect={e.code} "
                               f"host={shown(host)}") from None
        raise _failed(allowed, f"the image URL answered HTTP {e.code}", f"host={shown(host)}") from None
    except (OSError, http.client.HTTPException, ValueError) as e:
        fetch_failed.log_line += f" reason={shown(e)}"
        raise fetch_failed from None


# ------------------------------------------------------------------------------------------------ the one entry
def _bounded(read, allowed: ImageSources, source: str) -> bytes:
    """`read(watch)` with a deadline, whatever blocks it. A slow host's connection is ended at the deadline, so its
    thread ends too. A name lookup or a dead network mount cannot be ended: that thread stays until the system gives
    up, as a daemon, and the request goes on."""
    done: queue.Queue = queue.Queue(1)
    watch = _Watch()

    def run():
        try:
            done.put((read(watch), None))
        except BaseException as e:  # noqa: BLE001 - raised again on the request's thread
            done.put((None, e))
        finally:
            watch.close()

    threading.Thread(target=run, daemon=True, name="image-read").start()
    try:
        data, error = done.get(timeout=IMAGE_READ_S)
    except queue.Empty:
        watch.close()
        message = f"the image source did not answer within {IMAGE_READ_S:.0f} s"
        raise ImageRefused(message if not allowed.local else f"{message}: {shown(source)}",
                           f"{message}: peer={shown(allowed.peer)}") from None
    if error is not None:
        raise error
    return data


def load_image(source, allowed: ImageSources | None = None) -> bytes:
    """One request image's bytes, under the policy; no policy is the remote one with nothing allowed."""
    allowed = allowed or ImageSources()
    if not isinstance(source, str) or not source:
        raise ValueError(USAGE)
    low = source[:32].lower()
    if low.startswith("data:"):
        return _read_data(source)
    if low.startswith(("http://", "https://")):
        fetch = _read_local_url if allowed.local else _read_allowed_url
        return _bounded(lambda watch: fetch(source, allowed, watch), allowed, source)
    if _SCHEME.match(low) and not low.startswith("file://"):
        raise ValueError(USAGE)
    read = _read_local_file if allowed.local else _read_allowed_file
    return _bounded(lambda watch: read(source, allowed), allowed, source)

# ===== file: serve/engine/lifeline.py =====
"""Start the engine so that it ends with the server, however the server ends.

The server's own path is graceful (QUIT, wait, terminate, kill). This is the guarantee behind it for the cases where
none of that runs: SIGKILL, a crash, a closed console, also while the model is still loading.

* Linux: the child gets PR_SET_PDEATHSIG = SIGKILL. The kernel ties the signal to the thread that forked the child,
  not to the process, so every start is made by one long-lived anchor thread; a request thread that starts an engine
  and then finishes would otherwise take the engine down with it.
* Windows: the child joins a job object that kills its members when the last handle closes; the only handle is ours.
* Everywhere: the child gets its own process group, so a terminal Ctrl+C reaches the server only, which then ends
  the engine the clean way.
"""
from __future__ import annotations

import concurrent.futures
import os
import queue
import signal
import subprocess
import sys
import threading

WINDOWS = os.name == "nt"
LINUX = sys.platform.startswith("linux")
NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)

PR_SET_PDEATHSIG = 1
_prctl = None
if LINUX:
    try:
        import ctypes
        _prctl = ctypes.CDLL(None, use_errno=True).prctl
        _prctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong)
    except (OSError, AttributeError):
        _prctl = None


class _Anchor:
    """A thread that lives as long as the process and makes every start, so the death signal's parent is it."""

    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, name="engine-anchor", daemon=True).start()

    def _run(self) -> None:
        while True:
            fn, future = self._jobs.get()
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                future.set_exception(exc)

    def call(self, fn):
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((fn, future))
        return future.result()


_anchor: _Anchor | None = None
_anchor_lock = threading.Lock()


def _the_anchor() -> _Anchor:
    global _anchor
    with _anchor_lock:
        if _anchor is None:
            _anchor = _Anchor()
        return _anchor


def _child_setup(parent: int, then=None):
    """The child's preexec function: its own process group, and (Linux) SIGKILL when the starting thread ends."""
    def run() -> None:
        os.setpgid(0, 0)
        if _prctl is not None:
            _prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
            if os.getppid() != parent:                  # the parent died between fork and prctl
                os.kill(os.getpid(), signal.SIGKILL)
        if then is not None:
            then()
    return run


_job = None
_job_lock = threading.Lock()


def _contain(child: subprocess.Popen) -> bool:
    """Windows: put the child in the kill-on-close job. False, harmlessly, when that is not possible."""
    global _job
    import ctypes
    from ctypes import wintypes

    class Limits(ctypes.Structure):             # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD), ("IoCounters", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        with _job_lock:
            if _job is None:
                job = k32.CreateJobObjectW(None, None)  # no security attributes: the handle is not inherited
                info = Limits()
                info.LimitFlags = 0x2000                # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                    return False
                _job = job
        return bool(k32.AssignProcessToJobObject(_job, int(child._handle)))
    except (AttributeError, OSError, ValueError):
        return False


def popen(args, **kw) -> subprocess.Popen:
    """``subprocess.Popen(args, **kw)`` for a process that must not outlive the server."""
    if WINDOWS:
        kw["creationflags"] = kw.get("creationflags", 0) | NEW_PROCESS_GROUP
        child = subprocess.Popen(args, **kw)
        _contain(child)
        return child
    kw["preexec_fn"] = _child_setup(os.getpid(), kw.get("preexec_fn"))
    return _the_anchor().call(lambda: subprocess.Popen(args, **kw))

# ===== file: serve/engine/mock.py =====
"""The Engine protocol and MockEngine, a scripted engine that needs no GPU."""
from __future__ import annotations

import threading
import time
from typing import Iterator, Protocol

from serve.engine.slots import AdmissionQueue
from serve.util.text import IM_END


class Engine(Protocol):
    max_context: int
    def generate(self, ids: list[int], max_new: int, sampling: dict, cancel: threading.Event) -> Iterator[int]: ...


class MockEngine:
    """Replays scripted completions as token ids plus the end-of-turn token; a list gives one script per request, the last repeating."""

    def __init__(self, tokenizer, script: str | list[str], max_context: int = 32768, delay_s: float = 0.0,
                 batch: int = 0, route=None):
        self.tok, self.max_context, self.delay = tokenizer, max_context, delay_s
        # batch >= 2: that many requests generate at once, the rest wait their turn (first come, first served)
        self.batch = batch if batch >= 2 else 0
        self.cv, self.admission, self.busy = threading.Condition(), AdmissionQueue(), 0
        self.route = route                              # prompt ids -> index of the script to answer with
        self.peak = 0                                   # most requests generating at once
        self.requests: list[tuple[list[int], dict]] = []    # (prompt, sampling) of each request, in arrival order
        end = tokenizer.encode(IM_END, parse_special=True)
        self.scripts = [tokenizer.encode(x, parse_special=True) + end for x in ([script] if isinstance(script, str)
                                                                                 else script)]
        self.script, self.turns = self.scripts[0], 0
        self.last_prompt: list[int] = []

    def queue_depth(self) -> int:
        with self.cv:
            return len(self.admission)

    def generate(self, ids, max_new, sampling, cancel, embeddings=None):
        if not self.batch:
            yield from self._run(ids, max_new, sampling, cancel, embeddings)
            return
        with self.cv:
            ticket = self.admission.join()
            try:
                while not (self.busy < self.batch and self.admission.may_go(ticket)):
                    self.cv.wait(timeout=0.05)
                    if cancel.is_set():
                        return
                self.busy += 1
                self.peak = max(self.peak, self.busy)
            finally:
                self.admission.leave(ticket)
                self.cv.notify_all()
        try:
            yield from self._run(ids, max_new, sampling, cancel, embeddings)
        finally:
            with self.cv:
                self.busy -= 1
                self.cv.notify_all()

    def _run(self, ids, max_new, sampling, cancel, embeddings=None):
        self.requests.append((list(ids), dict(sampling or {})))
        self.last_prompt = list(ids)
        self.last_embeddings = embeddings
        script = self.script
        if self.route:
            script = self.scripts[self.route(list(ids))]
        elif len(self.scripts) > 1:
            script = self.scripts[min(self.turns, len(self.scripts) - 1)]
            self.turns += 1
        self.script = script
        for t in script[:max_new]:
            if cancel.is_set():
                return
            if self.delay:
                time.sleep(self.delay)
            yield t

# ===== file: serve/engine/modelfile.py =====
"""Engine settings, tokenizer and vision inputs for a .basalt model file, unpacked once into the cache directory."""
from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from model_file import ModelFile  # noqa: E402
from serve.models import dialect_for  # noqa: E402
from serve.engine.flags import flag_value  # noqa: E402

VISION_EXE = "basalt-vision"
# llama.cpp's sampling keys in the GGUF metadata -> the request field each one defaults
GGUF_SAMPLING = {"general.sampling.temp": "temperature", "general.sampling.top_p": "top_p",
                 "general.sampling.top_k": "top_k", "general.sampling.min_p": "min_p",
                 "general.sampling.penalty_repeat": "repetition_penalty",
                 "general.sampling.penalty_last_n": "penalty_last_n"}


def cache_root() -> Path:
    return Path.home() / ".cache" / "basalt"


def cache_dir(mf: ModelFile) -> Path:
    """One cache directory per model, named by the meta section's sha256 prefix."""
    return cache_root() / mf.sections["meta"][2].hex()[:16]


def with_conversation_dir(words: list[str], cdir: Path) -> list[str]:
    """`words` plus the default conversation directory (inside the model cache folder `cdir`) when the disk budget
    is above zero and no directory is named."""
    budget = flag_value(words, "--conversation-cache-disk-mib")
    if budget and budget.isdigit() and int(budget) > 0 and not flag_value(words, "--conversation-cache-dir"):
        return words + ["--conversation-cache-dir", str(cdir / "conversations")]
    return words


def _short(v):
    """A GGUF float32 as the shortest decimal that is the same float32 (0.949999988079071 -> 0.95)."""
    if not isinstance(v, float):
        return v
    bits = struct.pack("<f", v)
    for digits in range(1, 10):
        s = float(f"{v:.{digits}g}")
        if struct.pack("<f", s) == bits:
            return s
    return v


def sampling_defaults(meta: dict) -> dict:
    """The model's sampling defaults: the general.sampling.* keys of meta.gguf_kv, by request field."""
    kv = meta.get("gguf_kv") or {}
    return {field: _short(kv[key]) for key, field in GGUF_SAMPLING.items() if kv.get(key) is not None}


def write_section(mf: ModelFile, name: str, dest: Path) -> None:
    """Copy a section to `dest` via a temporary name so a partial file is never used."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    off, size = mf.section(name)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".")
    try:
        with os.fdopen(fd, "wb") as out, open(mf.path, "rb") as src:
            src.seek(off)
            left = size
            while left > 0:
                b = src.read(min(left, 64 << 20))
                if not b:
                    raise IOError(f"{mf.path}: short read in {name}")
                out.write(b)
                left -= len(b)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def extract_tokenizer(mf: ModelFile) -> Path:
    """The `tokenizer/*` sections as a directory, reused when present."""
    out = cache_dir(mf) / "tokenizer"
    if (out / "vocab.json").exists():
        return out
    names = [n for n in mf.names() if n.startswith("tokenizer/")]
    if not any(n == "tokenizer/vocab.json" for n in names):
        raise ValueError(f"{mf.path}: no tokenizer sections")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=out.parent, prefix="tokenizer."))
    try:
        for n in names:
            (tmp / n.split("/", 1)[1]).write_bytes(mf.read(n))
        try:
            os.rename(tmp, out)
        except OSError:                          # another process finished first
            if not (out / "vocab.json").exists():
                raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def vision_config(mf: ModelFile, engine_bin: str, sidecar: bool = True) -> dict:
    """The vision sidecar's config (the projector alone without `sidecar`); ValueError names what is missing."""
    engine = Path(engine_bin).resolve()
    side = engine.parent / (VISION_EXE + engine.suffix)   # with the engine's suffix: basalt-vision.exe on Windows
    if "vision/mmproj" not in mf.sections:
        raise ValueError("this model file has no vision projector (vision/mmproj); drop --vision")
    if sidecar and not side.is_file():
        raise ValueError(f"{side} not found (build the vision sidecar), or drop --vision")
    if "vision/text_vocab" not in mf.sections:
        raise ValueError("this model file has no vision/text_vocab section; drop --vision")
    vdir = cache_dir(mf) / "vision"
    mmproj, vocab = vdir / "mmproj.gguf", vdir / "text_vocab.gguf"
    for name, dest in (("vision/mmproj", mmproj), ("vision/text_vocab", vocab)):
        if not dest.exists() or dest.stat().st_size != mf.section(name)[1]:
            write_section(mf, name, dest)
    return {"vision_bin": str(side), "mmproj": str(mmproj), "model": str(vocab)}


@dataclass(frozen=True)
class ModelSpec:
    """One model the server runs. `engine_words` are its engine flags as given. `vision`: a GPU number as a string, "cpu"
    or "dynamic". `gpu`: as --gpu."""
    path: str
    alias: str | None = None
    gpu: object = None
    vision: bool | str = False
    engine_words: tuple[str, ...] = ()


def model_config(spec: ModelSpec, engine_bin: str) -> dict:
    """The engine settings a model implies; its `engine_words` follow its arguments as given (the engine's parser lets
    a later flag win), its `vision` adds the sidecar or, "dynamic", the in-engine encoder (ValueError if it cannot be
    set up)."""
    path, vision = spec.path, spec.vision
    mf = ModelFile(path)
    meta = json.loads(mf.read("meta"))
    path = os.path.abspath(path)
    cdir = cache_dir(mf)
    words = with_conversation_dir(list(spec.engine_words), cdir)
    args = ["--model", path]   # settings come from the engine defaults and the server
    args += words
    cdir.mkdir(parents=True, exist_ok=True)
    cfg = {"engine_bin": os.path.abspath(engine_bin), "args": args, "model_name": spec.alias or meta.get("name") or "basalt",
           "sampling": sampling_defaults(meta),
           "arch": (meta.get("gguf_kv") or {}).get("general.architecture"), "log": str(cdir / "engine.log"),
           "tokenizer": str(extract_tokenizer(mf))}
    if vision and dialect_for(cfg["arch"]).in_engine_vision:
        if vision != "dynamic":
            raise ValueError(f"this model's images are encoded in the engine: --vision dynamic, not {vision}")
        if "vision/mmproj" not in mf.sections:
            raise ValueError("this model file has no vision projector (vision/mmproj); drop --vision")
        args.append("--vision")
        cfg["vision"] = {"mmproj": "", "mode": "dynamic", "gpu": True}
        return cfg
    v = vision_config(mf, engine_bin, sidecar=vision != "dynamic") if vision else None
    if v:
        v["mode"] = vision
        v["gpu"] = vision != "cpu"
        cfg["vision"] = v
        if vision == "dynamic":     # the engine encodes: it reads the projector itself
            args += ["--vision-mmproj", v["mmproj"]]
        if "--vision" not in args:   # image embeddings need --vision
            args.append("--vision")
    return cfg

# ===== file: serve/engine/process.py =====
"""ProcessEngine: one resident `basalt --serve` child process, driven over stdin and stdout.

Protocol (one line each):
  * start: stdout `INFO k=v ...` lines, then `READY <ctx> [stop]` (stop: STOP is honoured mid-request).
  * solo: `GEN <max_new> [k=v ...] <ids>` (GENI with embeddings) -> `T <id>`, `PP <read> <total> ...`,
    `RESUME <n>`, then `DONE ...`; `STOP` ends it early. `ERR ...` is a failure.
  * solo, text (INFO text_prompt=1): `GENT <max_new> [k=v ...] <nbytes> [plain=a:b,...]`, a newline, then nbytes of
    raw UTF-8: the engine tokenizes (control literals on; `plain` byte spans are text) and answers like a GEN, with
    `PT <n> <max_new>` before the first PP line. `clip=1` cuts max_new to the room the context leaves after the prompt.
    The slot and batch forms are SGENT and BGENT; the server uses only GENT.
  * batch slots: `BGEN <slot> ...` -> `BADM <slot> <continues>`, `BT <slot> <id>`, `BDONE <slot> ...`;
    `BSTOP <slot>`; `BYIELD <slot>` makes a long prompt read give way (answered by `YIELDED <slot> <n>`).
  * solo in a slot (INFO solo_any_slot=1): `SGEN <slot> <max_new> [k=v ...] <ids>` (SGENI with embeddings) is a GEN on
    that slot's own state, answered like a GEN.
  * `VRAM [mib]` -> `VRAM k=v ...`; `QUIT` ends the engine.
  * `ENC <image> <out> [<image> <out> ...]` (--vision-mmproj): one `ENC OK <n> <nx> <ny> <ms>` per image, or `ENC ERR`.
  * Requests share one pipe, serialized by the service FIFO (batch: by the `ctl` lock).
"""
from __future__ import annotations

import io
import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from serve.api.grammar import REQUEST_KEY as GRAMMAR_KEY, engine_key as grammar_key
from serve.api.logit_bias import REQUEST_KEY as BIAS_KEY, engine_key as bias_key
from serve.engine import lifeline, suspend
from serve.engine.flags import flag_value
from serve.engine.errors import EngineDied, EngineError, EngineSilent, EngineStartError, EngineStuck
from serve.engine.slots import AdmissionQueue
from serve.prompt import PromptText
from serve.util.console import tagged
from serve.engine.progress import narrate_start, server_line, start_errors, start_failure_hint, start_last_line


# A running request whose engine prints nothing for this long is out of step: the engine is ended and the request
# fails. ProcessEngine.silence_s overrides it (0: wait forever).
ENGINE_SILENCE_S = 300.0
# While a prompt is read the allowance grows: before the first PP line by the chunk's tokens at PP_FLOOR_TOK_S,
# afterwards a chunk may take PP_SLACK x the previous one's time.
PP_CHUNK_MAX = 32768
SLOT_MARGIN = 8       # tokens the engine keeps free in a slot's context beyond prompt and answer
PP_FLOOR_TOK_S = 50.0
PP_SLACK = 3.0
# The read of the engine's READY line has a timeout: a blocking read that never returns would hold the request turn
# for good (the engine sits idle, so no watchdog fires). Generous: a cold model load is not a hang. A process that
# says nothing for this long is ended, and the next request starts it again.
ENGINE_READY_S = 900.0
QUIT_S = 20.0            # an engine's time to end after QUIT
QUIT_DISK_S = 300.0      # ... with a disk tier: it writes the conversations it holds first
ENGINE_REQUEST = re.compile(
    # "+ 12288 of 98179 read": a request cancelled during its prompt read
    r"prompt (?P<prompt>\d+) tokens = (?P<reused>\d+) reused \+ \d+(?: of \d+)? read in (?P<read>[\d.]+) ms "
    r"\((?P<pp>[\d.]+) tok/s\), "
    r"(?P<gen>\d+) generated in (?P<gen_ms>[\d.]+) ms \((?P<tg>[\d.]+) tok/s\)")
_echoing: set[str] = set()      # logs already followed (restart() re-runs __init__)


def echo_requests(log_path: str, offset: int) -> None:
    """--request-lines: print one stdout line per finished request, parsed from the engine's log summary."""
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            m = ENGINE_REQUEST.search(line)
            if m:
                read_ms, gen_ms = float(m["read"]), float(m["gen_ms"])
                print("[server] request prompt %s cached %s output %s prompt_read %.0f ms total %.0f ms prefill %s "
                      "tok/s decode %s tok/s" % (m["prompt"], m["reused"], m["gen"], read_ms, read_ms + gen_ms, m["pp"],
                                                 m["tg"]), flush=True)


def engine_line(line: str) -> str:
    """Drop the `\r` a text-mode stream puts before a line's final `\n`; the line keeps its `\n`."""
    return line[:-2] + "\n" if line.endswith("\r\n") else line


AUTO_FLAGS = "auto flags: "
AUTO_NAMES = {"peer-exclusive": "peer-excl", "pool-workers": "pool"}   # the console's short names


def relay_auto_flags(line: str) -> bool:
    """Print the engine's auto-picked flags as a server line; False when the line is not that line."""
    if not line.startswith(AUTO_FLAGS):
        return False
    pairs = []
    for item in line.rstrip("\n")[len(AUTO_FLAGS):].split(", "):
        name, _, value = item.partition(" ")
        pairs.append(f"{AUTO_NAMES.get(name, name)}={value.replace(' ', '')}" if value else name)
    server_line(tagged("auto", " ".join(pairs)))
    return True


# First words of the stdout lines the server reads (or knowingly ignores); any other line is the engine talking to
# its log, such as a device-side trap message.
PROTOCOL_LINES = (AUTO_FLAGS, "INFO ", "READY", "T ", "PP ", "PL ", "PT ", "RESUME ", "REUSED ", "DONE", "ERR", "BADM ",
                  "BT ", "BDONE ", "YIELDED ", "VRAM ", "ENC ")


def log_unparsed(line: str, log) -> None:
    """Write an engine stdout line the server does not parse to the engine log, as it came."""
    if line.startswith(PROTOCOL_LINES) or not line.strip() or not hasattr(log, "write"):
        return
    try:
        log.write(line if line.endswith("\n") else line + "\n")
        log.flush()
    except (OSError, ValueError):                       # the log is gone (the engine is being replaced)
        pass


def frame_pipes(proc, stderr_pipe: bool) -> None:
    """Frame a child's pipes on `\n` alone, both directions: the text-mode default for a pipe writes `\r\n` on
    Windows, and ends a line at a `\r` that belongs to the line rather than ending it.  Only Popen's own text
    wrappers carry that default; a stream the caller made is left as it is."""
    if isinstance(proc.stdin, io.TextIOWrapper):
        proc.stdin.reconfigure(newline="\n", write_through=True, line_buffering=True)
    if isinstance(proc.stdout, io.TextIOWrapper):
        proc.stdout.reconfigure(newline="\n")
    if stderr_pipe and isinstance(proc.stderr, io.TextIOWrapper):
        proc.stderr.reconfigure(newline="\n")


def pump_lines(stream, sink) -> None:
    """Pass each line of a child's pipe to `sink` until it closes."""
    try:
        for line in stream:
            sink(engine_line(line))
    except (OSError, ValueError):
        pass


REQUEST_LINES = False    # --request-lines: echo the engine's per-request summary on stdout
PENALTY_LAST_N_DEFAULT = 64  # the penalty window of a request that sets a penalty but no penalty_last_n


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
# Engine lines for an unrecoverable verifier: the process may stay alive and print DONE, but must take no more requests.
FATAL_PREFIXES = ("ERR verify: timed out at layer ", "ERR verify batch: timed out at layer ",
                  "ERR verify: an earlier window never finished", "ERR verify batch: an earlier window never finished")
# Engine ERR texts that are the request's fault (a 400); any other ERR of a request is an engine failure (a 500).
REQUEST_ERRS = ("prompt (", "bad request:", "sampling ", "logit_bias:", "grammar:", "ppl:",
                "a token id is outside the vocabulary", "this engine was started without --vision",
                "the prompt cannot end in an image")


def request_err(err: str, shown: str | None = None) -> ValueError:
    """The exception for a request's ERR text, raised with `shown` when given: ValueError when the request caused it,
    else EngineError."""
    return (ValueError if err.startswith(REQUEST_ERRS) else EngineError)(err if shown is None else shown)


class ProcessEngine:
    """The resident engine process. Per-request sampling goes as `k=v` keys between max_new and the ids;
    an absent or zero temperature is greedy and not forwarded."""
    silence_s = ENGINE_SILENCE_S         # kept across restart()
    silent_note = None                   # why the server ended a silent engine
    last_err = None                      # an ERR that was the last stdout line
    slot_drafts = {}                     # batch slot -> (accepted, offered, time) of its last BDONE, counts None when absent
    batch = 0                            # --batch: the engine's batch slots (0: one request at a time)
    takes_text = True                    # generate() accepts a PromptText (sent as GENT when the engine can tokenize)
    stderr_sink = None                   # callable(line): engine stderr when there is no log file
    quit_s = QUIT_S

    # `last`: figures of the last DONE line; per thread with batch slots, since requests run concurrently.
    @property
    def last(self):
        if self.batch:
            tl = self.__dict__.setdefault("_tl", threading.local())
            if not hasattr(tl, "last"):
                tl.last = {}
            return tl.last
        return self.__dict__.get("_last", {})

    @last.setter
    def last(self, value):
        self.__dict__["_last"] = value
        if self.batch:
            tl = self.__dict__.setdefault("_tl", threading.local())
            tl.last = value

    # Windows of the request in progress on this thread: (windows, tokens in them), counted from the batch slot's
    # `BT` lines.
    def window_stats(self) -> tuple[int, int]:
        tl = self.__dict__.setdefault("_wtl", threading.local())
        return getattr(tl, "windows", 0), getattr(tl, "tokens", 0)

    def _count_window(self, tokens: int) -> None:
        tl = self.__dict__.setdefault("_wtl", threading.local())
        tl.windows, tl.tokens = getattr(tl, "windows", 0) + 1, getattr(tl, "tokens", 0) + tokens

    def _reset_windows(self) -> None:
        tl = self.__dict__.setdefault("_wtl", threading.local())
        tl.windows = tl.tokens = 0

    # `read_ms`: prompt-read time of the request in progress, summed over its engine runs (per thread with batch slots).
    @property
    def read_ms(self) -> float:
        if self.batch:
            return getattr(self.__dict__.setdefault("_tl", threading.local()), "read_ms", 0.0)
        return self.__dict__.get("_read_ms", 0.0)

    @read_ms.setter
    def read_ms(self, value: float):
        self.__dict__["_read_ms"] = value
        if self.batch:
            self.__dict__.setdefault("_tl", threading.local()).read_ms = value

    def __init__(self, engine_bin: str, args: list[str], cwd: str | None = None, log: str | None = None,
                 env: dict | None = None, lazy: bool = False):
        self.spawn = (engine_bin, list(args), cwd, log, env)   # to start it again
        self.model_path = flag_value(args, "--model") or ""
        self.quit_s = QUIT_DISK_S if (flag_value(args, "--conversation-cache-disk-mib") or "0") != "0" else QUIT_S
        self.log_path = log
        self.proc, self.pump, self.log = None, None, None
        if "slot_cv" not in self.__dict__:               # restart() wakes waiters even when the start failed
            self.slot_cv = threading.Condition()
        if "sleep_lock" not in self.__dict__:            # sleep, wake and close never overlap, across restarts too
            self.sleep_lock = threading.RLock()
        self.ended, self.unloaded = True, True
        if "loading" not in self.__dict__:               # restart() holds these across its __init__ calls
            self.loading, self.load_status = False, {}
        self.asleep = False              # its GPU state suspended by the driver (sleep/wake)
        self.restore_s = 0.0             # the driver's part of the last wake
        self.max_context = int(flag_value(args, "--max-context") or 0)   # the engine's own at READY
        self.can_stop = False            # READY <ctx> stop
        self.last = {}
        self.slot_drafts = {}
        self.info = {}                   # INFO key=value facts
        self.prefill_tok_s_mean = None
        self.progress = None             # (read, total) prompt tokens, from PP lines
        self.silent_note = None
        self.last_err = None
        try:                            # a release's BUILD.json gives its version
            self.info["version"] = json.loads((Path(engine_bin).parent / "BUILD.json").read_text()).get("version")
        except (OSError, ValueError):
            self.info["version"] = None
        if lazy:
            return
        self.unloaded = False            # `ended` stays True until READY
        self.log = open(log, "a", encoding="utf-8") if log else subprocess.DEVNULL
        loading = threading.Event()                     # set once READY: stops the narrator
        log_start = os.path.getsize(log) if log else 0  # where this start's log lines begin
        self.log_start = log_start
        if log:
            visible = (env or os.environ).get("CUDA_VISIBLE_DEVICES", "")
            gpus = [int(x) for x in visible.split(",") if x.strip().isdigit()]
            narrator = threading.Thread(target=narrate_start, args=(log, os.path.getsize(log), loading),
                                        kwargs={"gpus": gpus, "on_progress": self._note_load}, daemon=True)
            narrator.start()
            # one follower per log, across restarts
            if REQUEST_LINES and os.path.abspath(log) not in _echoing:
                _echoing.add(os.path.abspath(log))
                threading.Thread(target=echo_requests, args=(log, os.path.getsize(log)), daemon=True).start()
        sink = None if log else ProcessEngine.stderr_sink
        self.max_context = 0                            # before the new process is visible: never its predecessor's
        try:
            self.proc = lifeline.popen([engine_bin, "--serve", *args], cwd=cwd, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE if sink else self.log,
                                       text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        except OSError as e:                            # missing or not executable: nothing started, so this is a start error
            loading.set()                               # the narrator stops and the log handle goes back
            if log:
                self.log.close()
            raise EngineStartError(f"the engine could not be started: {e}", log) from e
        frame_pipes(self.proc, sink is not None)
        if sink:
            threading.Thread(target=pump_lines, args=(self.proc.stderr, sink), daemon=True).start()
        # the lines come from a thread up to READY, so the read can time out and the rest of the stream stays for _pump
        ready_q: queue.Queue = queue.Queue()
        threading.Thread(target=self._ready_pump, args=(self.proc, ready_q, self.log), daemon=True).start()
        ready_deadline = time.monotonic() + ENGINE_READY_S
        timed_out = False
        while True:
            left = ready_deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            try:
                line = ready_q.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                break
            if relay_auto_flags(line):
                continue
            if line.startswith("INFO "):
                for kv in line.split()[1:]:
                    k, _, v = kv.partition("=")
                    self.info[k] = int(v) if v.lstrip("-").isdigit() else v
            if line.startswith("READY"):
                f = line.split()
                self.max_context = int(f[1])
                self.can_stop = "stop" in f[2:]
                break
        loading.set()
        if log:
            narrator.join(2)                            # its lines precede what follows READY
        if self.max_context <= 0:
            if timed_out:                               # still loading, or hung: end it so the turn is not held
                try:
                    self.proc.kill()
                except OSError:
                    pass
            try:                                        # close its pipes and our log handle
                self.proc.wait(timeout=5)
                self.proc.stdin.close()
                self.proc.stdout.close()
                if log:
                    self.log.close()
            except (OSError, subprocess.TimeoutExpired):
                pass
            if timed_out:
                reason = f"the engine did not report READY within {ENGINE_READY_S:.0f} s"
            elif errors := start_errors(log, log_start):   # the engine's own reason
                reason = "; ".join(dict.fromkeys(errors))
            else:
                reason = start_last_line(log, log_start) or "the engine exited before it was ready"
            raise EngineStartError(reason + start_failure_hint(log, log_start), log)
        self.known_ctx = self.max_context   # survives a failed restart
        # an engine's own version (INFO engine=) beats the BUILD.json fallback
        if self.info.get("engine"):
            self.info["version"] = str(self.info["engine"])
        self.ended = False                              # READY: alive from here
        # stdout is pumped on a thread so requests can wait with a timeout
        self.lines: queue.Queue = queue.Queue()
        # the engine reports the slots it actually runs (INFO batch_slots=N), possibly fewer than asked
        asked = next((int(v) for v in (flag_value(args, k) for k in ("--batch", "--slots")) if v), 0)
        self.batch = int(self.info.get("batch_slots") or 0)
        if asked and self.batch != asked:
            print(f"[server] warning: {asked} parallel requests asked, the engine runs {self.batch or 'one'}", flush=True)
        self.slot_order = list(range(self.batch))
        self.slot_q = [queue.Queue() for _ in range(self.batch)]
        self.slot_busy = [False] * self.batch
        # tokens each slot holds (for prefix reuse by a conversation's next turn) and when it was last used
        self.slot_held: list[list[int]] = [[] for _ in range(self.batch)]
        self.slot_used = [0.0] * self.batch
        # admission state outlives engine processes: restart() re-runs this while requests wait on it
        if "ctl" not in self.__dict__:
            self.waiting = 0                            # requests waiting for ctl
            self.wait_lens: list[list[int]] = []        # their prompt lengths
            self.ctl_epoch = 0                          # times ctl was taken
            self.ctl = threading.Lock()                 # one admission or solo request at a time
            self.admission = AdmissionQueue()           # requests waiting for a free slot
        self.gen = self.__dict__.get("gen", 0) + 1      # which engine process this is
        self._yielded = None                            # (slot, tokens read) of a read that gave way
        self._ctl_erred = False                         # the control lines ended on an ERR
        self.wlock = threading.Lock()                   # serializes stdin writes
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()

    def _note_load(self, status: dict) -> None:
        self.load_status = status

    @staticmethod
    def _ready_pump(proc, out: queue.Queue, log=None) -> None:
        """The engine's first lines up to READY, for a read that can time out; None when its output closed first."""
        done = False
        try:
            for line in proc.stdout:
                line = engine_line(line)
                log_unparsed(line, log)
                out.put(line)
                if line.startswith("READY"):
                    done = True
                    return
        except (OSError, ValueError):                   # the pipe closed under us (the engine was killed)
            pass
        finally:
            if not done:
                out.put(None)

    def _pump(self):
        proc, lines, log = self.proc, self.lines, self.log   # a restart replaces all three
        slot_q = self.slot_q
        line = None
        for line in proc.stdout:
            line = engine_line(line)
            log_unparsed(line, log)
            if line.startswith(AUTO_FLAGS):             # printed once, before READY; never a reply line
                continue
            if line.startswith(FATAL_PREFIXES):         # before batch routing
                if self.proc is proc:
                    self.silent_note = "unrecoverable verification failure: " + line[4:].strip()
                    self.ended = True
                try:
                    proc.terminate()
                except OSError:
                    pass
                break                                   # publish EOF, never the trailing DONE
            if line.startswith(("BT ", "BDONE ")) and slot_q:   # a batch slot's own lines (BT <slot> <id> [<id> ..])
                try:
                    slot_q[int(line.split()[1])].put(line)
                    continue
                except (IndexError, ValueError):
                    pass
            lines.put(line)
        if self.proc is proc:                           # a killed engine's pump must not mark its successor
            self.ended = True                           # output closed: it is gone
            if line and line.startswith("ERR"):         # why it exited, when no request read it
                self.last_err = line[4:].strip()
        lines.put(None)
        for q in slot_q:
            q.put(None)

    def death_note(self) -> str:
        """Why the engine most likely ended, from the end of its log: its own watchdog, else RAM."""
        if getattr(self, "silent_note", None):          # the server ended it
            return self.silent_note
        tail = ""
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                tail = f.read().decode("utf-8", "replace")
        except (OSError, TypeError):
            pass
        for line in reversed(tail.splitlines()):
            if "issue #29" in line:
                return "the engine's watchdog stopped it: " + line.strip()
        proc = self.proc                                 # None between restart's close and start
        rc = proc.poll() if proc is not None else None
        if rc is not None and rc >= 0 and self.last_err:  # its last stdout words
            return f"the engine exited (code {rc}) after it reported: {self.last_err}"
        last = next((x.strip() for x in reversed(tail.splitlines()) if x.strip().startswith(("basalt", "ERR"))), "")
        if rc is not None and rc >= 0 and last:          # it ended by itself
            return f"the engine exited (code {rc}), last log line: {last}"
        return "probably out of RAM (sudo dmesg | grep -i 'out of memory')"

    def alive(self) -> bool:
        proc = self.proc                                 # read once: restart()/close() may clear it
        return proc is not None and not getattr(self, "ended", False) and proc.poll() is None

    def exit_code(self):
        if self.proc is None:
            return None
        try:
            return self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return None

    def unload(self):
        """Release GPU/RAM between requests, retaining the spawn config for automatic reloading."""
        self.close()
        self.unloaded = True

    RESTART_RETRY_S = 15.0   # between restart() tries, for VRAM to be released

    def restart(self, tries: int = 3):
        """Start the engine again after it died; a start that exits before READY is retried."""
        info = dict(self.info)
        self.starting = True                     # prepare() answers 503 meanwhile
        self.loading, self.load_status = True, {}
        try:
            self.close()
            for i in range(tries):
                # not alive until READY, so no request sees a half-started engine; __init__ clears `ended`
                self.ended = True
                try:
                    self.__init__(*self.spawn)
                    break
                except EngineStartError:
                    if self.proc is None:               # the program could not be started: a retry cannot help
                        raise
                    try:
                        self.proc.wait(timeout=60)
                    except (subprocess.TimeoutExpired, OSError):
                        pass
                    if i == tries - 1:
                        raise
                    print(f"[server] warning: the engine did not start (try {i + 1} of {tries}), retrying in "
                          f"{self.RESTART_RETRY_S:g} s", flush=True)
                    time.sleep(self.RESTART_RETRY_S)
        finally:
            self.starting = False
            self.loading = False
            with self.slot_cv:                   # wake the requests that waited through the restart
                self.slot_cv.notify_all()
        self.info = {**info, **self.info}

    def _parse_done(self, line):
        f = line.split()
        self.read_ms = getattr(self, "read_ms", 0.0) + float(f[3])
        self.last = {"generated": int(f[1]), "prompt_tokens": int(f[2]), "prompt_ms": float(f[3]),
                     "decode_ms": float(f[4]), "finish": f[5]}
        if len(f) >= 9:                                   # conversation cache fields
            self.last.update(drafts_accepted=int(f[6]), drafts_offered=int(f[7]), reused=int(f[8]))
        if len(f) >= 11:                                  # decode hit rate
            self.last.update(hits=int(f[9]), lookups=int(f[10]))
        if len(f) >= 13:                                  # decode lookups read from the expert file
            self.last.update(file_reads=int(f[12]))
        if len(f) >= 15:                                  # prompt tokens actually read
            self.last.update(prompt_read=int(f[14]))
        if len(f) >= 16:                                  # experts read over PCIe
            self.last.update(offloaded=int(f[15]))

    def vram(self, reserve_mib: int | None, timeout: float = 120.0) -> dict:
        """Resize the expert cache to leave `reserve_mib` free (None: the starting reserve); returns the engine's
        figures. Caller holds the service FIFO. ValueError carries the engine's refusal."""
        return self._vram_command("VRAM" + ("" if reserve_mib is None else f" {int(reserve_mib)}"), timeout)

    def can_sleep(self) -> bool:
        return suspend.available()

    def sleep(self, ram_check=None, timeout: float = 120.0) -> dict:
        """SLEEP: the engine gives its expert cache's VRAM back (its experts stay in RAM), then the driver moves the
        rest of its GPU state to host memory and releases the VRAM. `ram_check()`, asked once the cache is down, says
        why the rest would not fit in RAM (None: it fits). Caller holds the service FIFO and the control lines.
        ValueError: not asleep (the engine is as it was)."""
        with self.sleep_lock:
            out = self._vram_command("SLEEP", timeout)
            why = ram_check() if ram_check is not None else None
            if why is None:
                why = suspend.suspend(self.proc.pid)
            if why is not None:
                self._vram_command("WAKE", timeout)
                raise ValueError(why)
            self.asleep = True
            return out

    def wake(self, timeout: float = 120.0) -> dict:
        """The driver puts the GPU state back, then WAKE refills the expert cache from RAM. EngineDied when the GPU
        state cannot come back (the engine is ended so the next request starts it again)."""
        with self.sleep_lock:
            t = time.time()
            why = suspend.resume(self.proc.pid)
            self.restore_s = time.time() - t
            if why is None:
                self.asleep = False
                try:
                    return self._vram_command("WAKE", timeout)
                except ValueError as e:                 # its caches could not come back: start it again
                    why = str(e)
            self.asleep = False
            self._silent(why)
            raise EngineDied(why)

    def eos_ids(self) -> set[int]:
        """The ids the engine ends a reply on (INFO eos_ids=a,b)."""
        return {int(i) for i in str(self.info.get("eos_ids") or "").split(",") if i}

    def _vram_command(self, command: str, timeout: float) -> dict:
        """Send a VRAM, SLEEP or WAKE command and read its VRAM line."""
        if not self.alive():
            raise EngineDied("the engine is not running")
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        what = command.split()[0]
        deadline = time.time() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                raise EngineDied(f"the engine did not answer the {what} command") from None
            if line is None:
                raise EngineDied("the engine ended")
            line = line.strip()
            if line.startswith("ERR"):
                raise ValueError(line[4:].strip() or f"the engine refused the {what} command")
            if line.startswith("VRAM "):
                out = {}
                for kv in line.split()[1:]:
                    k, _, v = kv.partition("=")
                    out[k] = int(v) if v.lstrip("-").isdigit() else v
                self.info.update({k: out[k] for k in ("expert_slots", "vram_free_mib") if k in out})
                self.info["vram"] = out
                return out
            if time.time() > deadline:
                raise EngineDied(f"the engine did not answer the {what} command")

    ENC_TIMEOUT_S = 300.0

    def encode_images(self, jobs: list, timeout: float | None = None) -> list[str]:
        """ENC: the engine's in-process image encoder reads each (image file, output file) of `jobs` and answers one
        `OK <n> <nx> <ny> <ms>` line per image. Caller holds the service FIFO; with batch slots the control lines are
        taken here too (a decoding slot pauses for the encode). ValueError carries the engine's refusal."""
        if not self.alive():
            raise EngineDied("the engine is not running")
        timeout = self.ENC_TIMEOUT_S if timeout is None else timeout
        have_ctl = False
        if self.batch:
            turn = self._take_control(threading.Event(), 1)
            try:
                while True:
                    next(turn)
            except StopIteration:
                have_ctl = True
        try:
            self._send("ENC " + " ".join(f"{img} {out}" for img, out in jobs))
            deadline = time.time() + timeout
            got: list[str] = []
            while len(got) < len(jobs):
                try:
                    line = self.lines.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    raise EngineDied("the engine did not answer the image command") from None
                if line is None:
                    raise EngineDied("the engine ended")
                line = line.strip()
                if line.startswith("ENC ERR"):
                    raise ValueError("the image could not be read: " + (line[8:].strip() or "the engine refused it"))
                if line.startswith("ENC OK "):
                    got.append(line[4:])
                elif time.time() > deadline:
                    raise EngineDied("the engine did not answer the image command")
            return got
        finally:
            if have_ctl:
                self.ctl.release()

    @staticmethod
    def sampling_keys(sampling: dict) -> str:
        """The GEN line's sampling keys, each value as sent (the handler refused what is out of range,
        config.checked_sampling); a field left out is not sent.  A temperature of 0 is greedy, as an absent one;
        penalties that change nothing (repetition 1, frequency and presence 0) are not sent."""
        keys = ""
        t = sampling.get("temperature")
        if _number(t) and float(t) > 0.0:
            keys += f" temperature={float(t)!r}"
        tp = sampling.get("top_p")
        if _number(tp):
            keys += f" top_p={float(tp)!r}"
        tk = sampling.get("top_k")
        if _number(tk) and tk == int(tk):
            keys += f" top_k={int(tk)}"
        mp = sampling.get("min_p")
        if _number(mp):
            keys += f" min_p={float(mp)!r}"
        rp = sampling.get("repetition_penalty")
        rp_on = _number(rp) and float(rp) != 1.0
        pf = sampling.get("frequency_penalty")
        pf_on = _number(pf) and float(pf) != 0.0
        pp = sampling.get("presence_penalty")
        pp_on = _number(pp) and float(pp) != 0.0
        if rp_on:
            keys += f" penalty_repeat={float(rp)!r}"
        if pf_on:
            keys += f" penalty_freq={float(pf)!r}"
        if pp_on:
            keys += f" penalty_present={float(pp)!r}"
        if rp_on or pf_on or pp_on:
            # the window the penalties count over: the request's, else llama.cpp's default
            pln = sampling.get("penalty_last_n")
            keys += f" penalty_last_n={int(pln) if _number(pln) else PENALTY_LAST_N_DEFAULT}"
        seed = sampling.get("seed")
        if _number(seed) and seed == int(seed):
            keys += f" seed={int(seed)}"
        # relaxed_drafting: this request's --relaxed-drafting threshold, 0 or false for exact sampling
        rd = sampling.get("relaxed_drafting")
        if rd is False:
            keys += " relax=0"
        elif _number(rd):
            keys += f" relax={float(rd)!r}"
        return keys + ProcessEngine.projection_key(sampling) + bias_key(sampling.get(BIAS_KEY)) \
            + grammar_key(sampling.get(GRAMMAR_KEY))

    @staticmethod
    def projection_key(sampling: dict) -> str:
        """`cvec=0|1` key for the speed-projection control vector (ignored by engines without one)."""
        on = sampling.get("experimental_speed_projection")
        return f" cvec={int(on)}" if isinstance(on, bool) else ""

    def _send(self, text: str):
        try:
            with self.wlock:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
        except (OSError, AttributeError):                # the pipe is gone
            raise EngineDied(f"the engine stopped unexpectedly (exit code {self.exit_code()})") from None

    def _control(self, cancel, on_token, stop_when=None, idle=None):
        """Read the control lines of a GEN/BGEN, yielding None as heartbeat and False per token (on_token gets the id).
        Ends at DONE (solo) or BADM; `stop_when()` true sends STOP once. `idle()` runs whenever no line came for
        0.5 s: a prompt read beside the slots' windows sends no progress line."""
        stopped = False
        beat = time.monotonic()
        while True:
            try:
                # polls the cancel event every 0.5 s; the HTTP heartbeat stays at 10 s
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                if cancel.is_set() and not stopped:
                    self._send("STOP")
                    stopped = True
                if idle is not None:
                    idle()
                if time.monotonic() - beat >= 10.0:
                    beat = time.monotonic()
                    yield None
                continue
            beat = time.monotonic()
            if line is None:
                raise EngineDied(f"the engine stopped unexpectedly (exit code {self.exit_code()})")
            if line.startswith("T "):
                on_token(int(line[2:]))
                if not stopped and (cancel.is_set() or (stop_when is not None and stop_when())):
                    self._send("STOP")
                    stopped = True
                yield False                              # a token (not a heartbeat)
            elif line.startswith("PP "):
                f = line.split()
                if len(f) >= 3 and f[1].isdigit() and f[2].isdigit():
                    self.progress = (int(f[1]), int(f[2]))
                    self.prefill_tok_s_mean = float(f[4]) if len(f) >= 5 else None
                if not stopped and cancel.is_set():
                    # stop during the prompt read, not after the first token
                    self._send("STOP")
                    stopped = True
                yield None
            elif line.startswith("PL "):
                self._note_live(line)
                if not stopped and cancel.is_set():
                    self._send("STOP")
                    stopped = True
                yield None
            elif line.startswith("DONE"):
                self._parse_done(line)
                self._last_done = line
            elif line.startswith("BADM "):
                f = line.split()
                # BADM <slot> 1: continues, the admission's token already sent; 2: continues, no token yet (the slot's
                # first window makes it); 0: done
                self._ctl_result = ("badm", len(f) >= 3 and f[2] in ("1", "2"), len(f) >= 3 and f[2] == "2")
                return
            elif line.startswith("YIELDED "):            # <slot> <tokens read> after BYIELD
                f = line.split()
                if len(f) >= 3 and f[1].lstrip("-").isdigit() and f[2].isdigit():
                    self._yielded = (int(f[1]), int(f[2]))
            elif line.startswith("ERR"):
                self._ctl_erred = True                   # the engine refused it and is idle: no DONE follows
                raise request_err(line[4:].strip())
            if line.startswith("DONE") and self._ctl_mode == "solo":
                self._ctl_result = ("done", None)
                return

    def _drain_control(self, until: str, timeout: float = 300.0, born: int | None = None):
        """Read control lines up to `until` (DONE or BADM) so the next request sees no leftovers; None if the engine
        ended or `born` (the starting engine generation) is gone."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if born is not None and (self.gen != born or not self.alive()):
                return None
            try:
                line = self.lines.get(timeout=max(0.1, min(1.0, end - time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                return None
            if line.startswith("DONE"):
                self._parse_done(line)
            if line.startswith(until) or line.startswith("ERR"):
                return line
        return None

    def _release_slot_when_done(self, slot: int, stream: list[int] | None = None, wait_s: float = 0.0):
        """BSTOP a slot whose consumer left and free it at BDONE; `stream` (prompt + tokens so far) becomes what the
        slot holds. Waits up to `wait_s` for the BDONE and returns its fields (None when it has not come: the
        rest is freed in the background)."""
        if slot >= len(self.slot_q):                    # the engine restarted with fewer slots
            return None
        try:
            self._send(f"BSTOP {slot}")
        except EngineDied:
            pass
        q, busy, held = self.slot_q[slot], self.slot_busy, self.slot_held   # restart() replaces them
        tail = list(stream or [])

        def drain(end):             # "done" (fields) | "gone" (engine ended) | "wait" (not yet)
            while time.monotonic() < end:
                try:
                    line = q.get(timeout=max(0.01, min(5.0, end - time.monotonic())))
                except queue.Empty:
                    continue
                if line is None:
                    tail.clear()
                    return "gone", None
                if line.startswith("BT "):
                    try:
                        tail.extend(int(x) for x in line.split()[2:])
                    except ValueError:
                        tail.clear()
                if line.startswith("BDONE "):
                    f = line.split()
                    self._note_bdone(slot, f)
                    return "done", f
            return "wait", None

        def free():
            held[slot] = tail[:-1] if stream and tail else []
            with self.slot_cv:
                busy[slot] = False
                self.slot_cv.notify_all()

        state, fields = drain(time.monotonic() + wait_s) if wait_s > 0 else ("wait", None)
        if state != "wait":
            free()
            return fields

        def wait():
            drain(time.monotonic() + 600.0)
            free()
        threading.Thread(target=wait, daemon=True).start()
        return None

    def _note_bdone(self, slot: int, f: list[str]):
        """Keep a slot's draft counts from its BDONE line (optional trailing accepted, offered; None when absent)."""
        known = len(f) >= 7 and f[5].isdigit() and f[6].isdigit()
        self.slot_drafts[slot] = (int(f[5]) if known else None, int(f[6]) if known else None, time.time())

    def _bdone_last(self, f: list[str]):
        """This thread's `last` after a BDONE: finish, decode ms and the request's own draft counts (optional trailing
        accepted, offered; without them they are unknown, never an earlier request's)."""
        if len(f) < 5 or not isinstance(self.last, dict):
            return
        prev = {k: v for k, v in self.last.items() if k not in ("drafts_accepted", "drafts_offered")}
        counts = {"drafts_accepted": int(f[5]), "drafts_offered": int(f[6])} \
            if len(f) >= 7 and f[5].isdigit() and f[6].isdigit() else {}
        self.last = {**prev, **counts, "finish": f[3], "decode_ms": float(f[4]),
                     "generated": int(f[2]) if f[2].isdigit() else self.last.get("generated")}

    def _carry_drafts(self, carry: list[int], final: bool):
        """Draft counts of a request that ran in several engine runs (solo, then a slot, ...): the run just read adds
        to `carry`; at the request's last run `carry` joins its counts."""
        last = self.last
        if not isinstance(last, dict) or last.get("drafts_offered") is None or last.get("drafts_accepted") is None:
            if final:
                carry[:] = [0, 0]
            return
        if final:
            last["drafts_accepted"] += carry[0]
            last["drafts_offered"] += carry[1]
            carry[:] = [0, 0]
        else:
            carry[0] += last["drafts_accepted"]
            carry[1] += last["drafts_offered"]

    def _note_live(self, line: str):
        """A `PL <position> <total> <ms> <tok/s>` line: the display's position and speed inside a chunk."""
        f = line.split()
        if len(f) >= 5 and f[1].isdigit() and f[2].isdigit():
            self.progress = (int(f[1]), int(f[2]))
            try:
                self.prefill_tok_s_mean = float(f[4])
            except ValueError:
                pass

    def _note_first_reused(self, first: list, last0):
        """Keep the reused-prefix count of the request's first engine run: later runs of the same request (solo, then
        a slot; a read that gave way) find its own prompt and tokens in the slot, which is not an earlier cache."""
        last = self.last
        if not first and isinstance(last, dict) and last is not last0 and last.get("reused") is not None:
            first.append(last["reused"])

    def _carry_run(self, carry: list[int], first: list, last0, final: bool):
        """After an engine run: carry the draft counts and, at the request's last run, report the first run's reuse
        and the prompt-read time of all its runs (a later run only re-reads the token the slot lacks)."""
        self._note_first_reused(first, last0)
        last = self.last
        if final and first and isinstance(last, dict) and last is not last0:
            last["reused"] = first[0]
            if self.read_ms > 0:
                last["prompt_ms"] = self.read_ms
            first.clear()
        self._carry_drafts(carry, final)

    BDONE_WAIT_S = 2.0   # a leaving consumer waits this long for its slot's BDONE (the request's draft counts)

    YIELDS_MAX = 2   # times one prompt read may give way to a shorter waiting one

    def _take_control(self, cancel, plen: int, after_epoch: int | None = None):
        """Wait for the control lines, yielding None heartbeats; False when cancelled. `after_epoch`: let requests
        already waiting go first."""
        entry = [plen]
        with self.slot_cv:
            self.waiting += 1
            self.wait_lens.append(entry)
        beat = time.monotonic()
        try:
            while True:
                if not self.alive() and not getattr(self, "starting", False):
                    # engine gone and not restarting: end now rather than wait for lines nobody will answer
                    raise EngineDied("the engine stopped while this request waited; it was not sent")
                with self.slot_cv:
                    turn = after_epoch is None or self.ctl_epoch > after_epoch or self.waiting <= 1
                if turn and self.ctl.acquire(timeout=0.5):
                    if self.alive():
                        break
                    self.ctl.release()                  # died or restarting while acquiring: not this request's engine
                    turn = False
                if not turn:
                    with self.slot_cv:
                        self.slot_cv.wait(timeout=0.5)
                if cancel.is_set():
                    return False
                if time.monotonic() - beat >= 10.0:     # heartbeat every 10 s
                    beat = time.monotonic()
                    yield None
        finally:
            with self.slot_cv:
                self.waiting = max(0, self.waiting - 1)
                if entry in self.wait_lens:
                    self.wait_lens.remove(entry)
        with self.slot_cv:
            self.ctl_epoch += 1
            self.slot_cv.notify_all()
        return True

    SOLO_AGAIN_MAX = 2      # times a request alone in a slot returns to the solo path
    SOLO_AGAIN_MIN_LEFT = 32  # ... and only with this many tokens still allowed

    def _may_go_solo(self, left: int, times: int, embeddings) -> bool:
        """Whether a request alone in a slot should go back to the solo path (needs INFO slot_cache=1)."""
        if (embeddings or times >= self.SOLO_AGAIN_MAX or left < self.SOLO_AGAIN_MIN_LEFT or
                not (self.info or {}).get("slot_cache") or
                ((self.info or {}).get("slot0_solo") and not (self.info or {}).get("solo_any_slot"))):
            return False
        with self.slot_cv:
            return sum(1 for b in self.slot_busy if b) == 1 and self.waiting == 0

    def _shorter_waiting(self, plen: int, free: int = 1) -> bool:
        """A waiting prompt under half this length is worth giving way to, when `free` slots are free (the one it
        will run in, and the one the read waits in if it holds none yet)."""
        with self.slot_cv:
            return (sum(1 for b in self.slot_busy if not b) >= free and
                    any(e[0] * 2 <= plen for e in self.wait_lens))

    def generate_batched(self, ids, max_new, sampling, cancel, embeddings=None):
        """--batch: run solo (GEN) while alone, move to a batch slot (BGEN) when others arrive, and return to solo
        when left alone. Prompt reads and admissions take the control lines one at a time; a long read may give way
        to a much shorter waiting one (BYIELD). An early-leaving consumer is STOPped/BSTOPped and drained."""
        if not self.alive():
            raise EngineDied("the engine is unavailable; this request was not sent")
        self.progress = None
        self._reset_windows()
        keys = self.sampling_keys(sampling or {})
        out: list[int] = []
        pending: list[int] = []
        prompt, left = list(ids), int(max_new)

        def gen_keys() -> str:      # a run that continues after tokens were generated tells the grammar where the prompt ends
            return keys + (f" gprompt={len(ids)}" if out and " grammar=" in keys else "")
        ok = yield from self._take_control(cancel, len(prompt))
        if not ok:
            return
        holding, born = True, self.gen                  # born: engine generation holding the control lines
        slot, gen0, reserved = None, None, None
        solo_slot, run0 = None, 0
        phase = "none"            # solo -> admit -> slot; "none" once the engine is done with it
        stop_sent = False
        yields, solo_again = 0, 0
        carry = [0, 0]            # draft counts (accepted, offered) of the runs before the current one
        first, last0 = [], self.last   # reused prefix of the first run; `last` before this request's first DONE
        self.read_ms = 0.0             # the prompt-read time of every run this request makes
        try:
            while True:   # loops when a request alone in a slot returns to solo
                if not holding:
                    ok = yield from self._take_control(cancel, len(prompt))
                    if not ok:
                        return
                    holding, born = True, self.gen
                solo_slot = None
                with self.slot_cv:
                    # With slot 0 as the solo session, a solo run on another slot's state is only possible when the
                    # engine runs it there (solo_any_slot); else every conversation stays in its own slot (a solo run
                    # would copy its conversation into slot 0 over the one that slot holds) and a lone request is a
                    # batch of one.
                    any_slot = bool(self.info.get("solo_any_slot"))
                    alone = not any(self.slot_busy) and self.waiting == 0 and (any_slot or not self.info.get("slot0_solo"))
                    if alone and left > 1 and any_slot:
                        solo_slot = self.pick_slot(prompt)       # the slot holding the longest start, else a free one
                        if solo_slot is None:
                            alone = False
                        else:
                            self.slot_busy[solo_slot] = True
                            reserved = solo_slot
                if alone and left > 1:
                    if solo_slot is not None:
                        head = f"SGENI {solo_slot} {left}{gen_keys()} {embeddings}" if embeddings else \
                            f"SGEN {solo_slot} {left}{gen_keys()}"
                    else:
                        head = f"GENI {left}{gen_keys()} {embeddings}" if embeddings else f"GEN {left}{gen_keys()}"
                    self._send(f"{head} {','.join(str(int(t)) for t in prompt)}")
                    run0, yield_sent = len(out), False
                    if solo_slot is not None:
                        self.slot_held[solo_slot] = []          # the run overwrites what the slot held
                    phase = "solo"
                    self._ctl_mode, self._ctl_result, self._yielded = "solo", None, None
                    self._ctl_erred = False
                    def others():       # a request waiting moves this one into a slot, if one can ever hold it
                        with self.slot_cv:
                            return self.waiting > 0
                    for x in self._control(cancel, pending.append, stop_when=others):
                        while pending:
                            t = pending.pop(0)
                            out.append(t)
                            yield t
                        if x is None:                       # heartbeat
                            if (not yield_sent and not out and not embeddings and yields < self.YIELDS_MAX and
                                    self._shorter_waiting(len(prompt), 2 if solo_slot is None else 1)):
                                if solo_slot is None:
                                    with self.slot_cv:      # slot the partial read will wait in
                                        reserved = self.pick_slot(prompt)
                                        if reserved is not None:
                                            self.slot_busy[reserved] = True
                                if reserved is not None:    # (a solo run in a slot parks its read in that slot)
                                    self._send(f"BYIELD {reserved}")
                                    yield_sent = True
                            yield None
                    phase = "none"                          # DONE was read
                    self._note_first_reused(first, last0)
                    while pending:
                        t = pending.pop(0)
                        out.append(t)
                        yield t
                    if self._yielded is not None and reserved is not None and self._yielded[0] == reserved:
                        slot, reserved = reserved, None     # gave way: the read continues in that slot
                    elif reserved is not None:              # did not give way: release the slot
                        self._solo_kept(solo_slot, prompt, out[run0:])
                        with self.slot_cv:
                            self.slot_busy[reserved] = False
                            self.slot_cv.notify_all()
                        reserved = None
                    if slot is None:
                        finish = (self.last or {}).get("finish") if isinstance(self.last, dict) else None
                        left = int(max_new) - len(out)
                        if cancel.is_set() or left <= 0 or finish in ("stop", "length") or (out and out[-1] in self.eos_ids()):
                            self._carry_run(carry, first, last0, True)
                            return
                        self._carry_run(carry, first, last0, False)
                        prompt = list(ids) + out            # continues in a batch slot
                while True:
                    if slot is None:
                        # prefer the slot holding this prompt's start, else the longest unused
                        with self.slot_cv:
                            ticket = self.admission.join()
                            try:
                                while True:
                                    slot = self.pick_slot(prompt)
                                    if slot is not None and self.admission.may_go(ticket, self._resumes(slot, prompt)):
                                        self.slot_busy[slot] = True
                                        break
                                    slot = None
                                    if holding:
                                        # never wait for a slot holding the control lines: a read that gave way
                                        # waits in its slot for them, and only its end frees that slot
                                        self.ctl.release()
                                        holding = False
                                        self.slot_cv.notify_all()
                                    self.slot_cv.wait(timeout=10.0)
                                    if cancel.is_set():
                                        return
                            finally:
                                self.admission.leave(ticket)
                                self.slot_cv.notify_all()
                    if not holding:                         # waited for the slot: take the control lines again
                        ok = yield from self._take_control(cancel, len(prompt))
                        if not ok:
                            return
                        holding, born = True, self.gen
                    if self._yielded is not None:           # gave way: waiting requests go first
                        self.slot_held[slot] = list(prompt[:self._yielded[1]])
                        self._yielded = None
                        yields += 1
                        self.ctl.release()
                        holding = False
                        with self.slot_cv:
                            epoch = self.ctl_epoch
                        ok = yield from self._take_control(cancel, len(prompt), after_epoch=epoch)
                        if not ok:
                            return
                        holding, born = True, self.gen
                    left = max(1, min(left, self.max_context - len(prompt) - SLOT_MARGIN))   # the slot's room
                    while not self.slot_q[slot].empty():
                        self.slot_q[slot].get_nowait()
                    head = f"BGENI {slot} {left}{gen_keys()} {embeddings}" if embeddings else f"BGEN {slot} {left}{gen_keys()}"
                    self._send(f"{head} {','.join(str(int(t)) for t in prompt)}")
                    self.slot_held[slot] = []               # admission overwrites the slot
                    phase = "admit"
                    self._ctl_mode, self._ctl_result, self._yielded = "batch", None, None
                    self._ctl_erred = False
                    asked = False

                    def ask_yield():    # a shorter request waits: the read gives way at its next chunk boundary
                        nonlocal asked
                        if (not asked and not embeddings and yields < self.YIELDS_MAX and
                                self._shorter_waiting(len(prompt))):
                            self._send(f"BYIELD {slot}")
                            asked = True
                    for x in self._control(cancel, pending.append, idle=ask_yield):
                        while pending:
                            t = pending.pop(0)
                            out.append(t)
                            yield t
                        if x is None:
                            ask_yield()
                            yield None
                    self._note_first_reused(first, last0)
                    cont = bool(self._ctl_result and self._ctl_result[1])
                    phase = "slot" if cont else "none"
                    while pending:
                        t = pending.pop(0)
                        out.append(t)
                        yield t
                    if not cont and self._yielded is not None and self._yielded[0] == slot and not cancel.is_set():
                        continue                            # gave way: retry after the shorter request
                    break
                self.ctl.release()
                holding = False
                if not cont:
                    return
                notok = bool(self._ctl_result and len(self._ctl_result) > 2 and self._ctl_result[2])
                gen0 = len(out) if notok else len(out) - 1  # the admission's token, if any; the slot feeds it first
                going_solo = False
                while True:
                    try:
                        line = self.slot_q[slot].get(timeout=10.0)
                    except queue.Empty:
                        if cancel.is_set() and not stop_sent:
                            self._send(f"BSTOP {slot}")
                            stop_sent = True
                        yield None
                        continue
                    if line is None:
                        phase = "none"
                        raise EngineDied(f"the engine stopped unexpectedly (exit code {self.exit_code()})")
                    if line.startswith("BT "):
                        self._count_window(len(line.split()) - 2)
                        for t in (int(x) for x in line.split()[2:]):    # a window's tokens for this slot, in order
                            out.append(t)
                            if cancel.is_set():
                                if not stop_sent:
                                    self._send(f"BSTOP {slot}")
                                    stop_sent = True
                                continue
                            yield t
                        if not going_solo and not stop_sent and not cancel.is_set() and \
                                self._may_go_solo(int(max_new) - len(out), solo_again, embeddings):
                            self._send(f"BSTOP {slot}")         # alone now: continue solo
                            going_solo = True
                    elif line.startswith("BDONE "):
                        phase = "none"
                        f = line.split()
                        self._note_bdone(slot, f)
                        self._bdone_last(f)
                        # the slot holds the prompt and every token fed (all but the last)
                        self.slot_held[slot] = list(prompt) + out[gen0:-1] if len(out) > gen0 else []
                        again = (going_solo and f[3:4] == ["cancel"] and not cancel.is_set() and len(out) < int(max_new)
                                 and not (out and out[-1] in self.eos_ids()))
                        self._carry_run(carry, first, last0, not again)
                        if again:
                            # continue on the solo path
                            self.slot_used[slot] = time.time()
                            with self.slot_cv:
                                self.slot_busy[slot] = False
                                self.slot_cv.notify_all()
                            slot, gen0 = None, None
                            prompt, left = list(ids) + out, int(max_new) - len(out)
                            solo_again += 1
                            break
                        return
        finally:
            # consumer left early or errored: keep engine and server in step
            try:
                if phase in ("solo", "admit") and (self.gen != born or not self.alive()):
                    phase = "none"                      # engine gone: nothing to drain
                if phase in ("solo", "admit") and self._ctl_erred:
                    phase = "none"                      # it ended on the engine's ERR: no DONE to wait for
                drained = phase in ("solo", "admit")    # this run's DONE is read below
                if phase == "solo":
                    self._send("STOP")
                    self._drain_control("DONE", born=born)
                elif phase == "admit":
                    line = self._drain_control("BADM", born=born)
                    if line and line.startswith("BADM ") and line.split()[2:3] in (["1"], ["2"]):
                        phase = "slot"
                if drained and phase != "slot":
                    self._carry_run(carry, first, last0, True)     # a request that ran in earlier runs too
            except EngineDied:
                pass
            if holding:
                self.ctl.release()
            if reserved is not None:
                if reserved == solo_slot:
                    self._solo_kept(solo_slot, prompt, out[run0:])
                with self.slot_cv:
                    self.slot_busy[reserved] = False
                    self.slot_cv.notify_all()
            if slot is not None:
                self.slot_used[slot] = time.time()
                if phase == "slot":
                    self.slot_held[slot] = []
                    stream = list(prompt) + out[gen0:] if gen0 is not None and len(out) > gen0 else None
                    f = self._release_slot_when_done(slot, stream, wait_s=self.BDONE_WAIT_S)
                    if f is not None:
                        self._bdone_last(f)
                        self._carry_run(carry, first, last0, True)
                    elif isinstance(self.last, dict):
                        # the BDONE is still to come: the counts of the DONE before it are not this request's
                        for k in ("drafts_accepted", "drafts_offered"):
                            self.last.pop(k, None)
                        carry[:] = [0, 0]
                else:
                    with self.slot_cv:
                        self.slot_busy[slot] = False
                        self.slot_cv.notify_all()

    def _solo_kept(self, slot, prompt: list[int], gen: list[int]) -> None:
        """A solo run in `slot` ended: the engine keeps its prompt and every token it fed (all but the last) there."""
        if slot is None:
            return
        self.slot_held[slot] = list(prompt) + gen[:-1] if gen else []
        self.slot_used[slot] = time.time()

    def _resumes(self, slot: int, prompt: list[int]) -> bool:
        """Whether `slot` holds the start of `prompt`."""
        h = self.slot_held[slot]
        return bool(h) and len(h) < len(prompt) and prompt[:len(h)] == h

    def queue_depth(self) -> int:
        """Requests waiting for the engine or a free slot."""
        with self.slot_cv:
            return self.waiting + len(self.admission)

    def pick_slot(self, prompt: list[int]) -> int | None:
        """A free slot for `prompt` (caller holds slot_cv): longest held prefix, else an empty one, then the least
        recently used; None when none is free."""
        free = [b for b in self.slot_order if not self.slot_busy[b]]
        if not free:
            return None
        def held_prefix(b):
            h = self.slot_held[b]
            return len(h) if h and len(h) < len(prompt) and prompt[:len(h)] == h else 0
        best = max(free, key=held_prefix)
        if held_prefix(best) > 0:
            return best
        return min(free, key=lambda b: (bool(self.slot_held[b]), self.slot_used[b]))

    def generate(self, ids, max_new, sampling, cancel, embeddings=None):
        """Yield token ids, and None as a 10 s heartbeat while the engine is quiet. An early-leaving consumer or
        `cancel` makes the engine STOP."""
        if not self.alive():
            raise EngineDied("the engine is unavailable; this request was not sent")
        text = ids if isinstance(ids, PromptText) else None
        if text is not None and (getattr(self, "batch", 0) or embeddings or not self.info.get("text_prompt")):
            ids, max_new = text.resolve(int(max_new), self.max_context)    # this engine takes ids
            text = None
        if getattr(self, "batch", 0):
            yield from self.generate_batched(ids, max_new, sampling, cancel, embeddings)
            return
        self.progress = None
        self.prefill_tok_s_mean = None
        head = f"GENI {int(max_new)}{self.sampling_keys(sampling or {})} {embeddings}" if embeddings else \
            f"GEN {int(max_new)}{self.sampling_keys(sampling or {})}"
        try:
            if text is not None:
                body = text.text
                spans = text.byte_spans()
                plain = " plain=" + ",".join(f"{a}:{b}" for a, b in spans) if spans else ""
                clip = " clip=1" if text.clip else ""
                self.proc.stdin.write(f"GENT {int(max_new)}{self.sampling_keys(sampling or {})}{clip} "
                                      f"{len(body.encode('utf-8'))}{plain}\n{body}")
            else:
                self.proc.stdin.write(f"{head} {','.join(str(int(t)) for t in ids)}\n")
            self.proc.stdin.flush()
        except OSError:                                  # the pipe is gone: the engine died
            raise EngineDied(f"the engine stopped unexpectedly (exit code {self.exit_code()})") from None
        done = False
        # silence allowance: silence_s plus the first chunk's read time; each PP line resets it to its chunk's time
        silence = float(self.silence_s or 0)
        allow = silence + min(len(ids), PP_CHUNK_MAX) / PP_FLOOR_TOK_S if silence > 0 else 0.0
        heard, read_to = time.monotonic(), 0
        beat = heard
        try:
            while True:
                wait = 10.0
                if allow > 0:
                    wait = min(wait, allow - (time.monotonic() - heard))
                    if wait <= 0:
                        done = True                       # nothing is listening: no STOP, no drain
                        raise self._silent(f"the engine said nothing for {time.monotonic() - heard:.0f} s during "
                                           "the request")
                try:
                    line = self.lines.get(timeout=min(wait, 0.5))     # polls the cancel event
                except queue.Empty:
                    if cancel.is_set():
                        return
                    if allow > 0 and time.monotonic() - heard >= allow:
                        continue                          # enforce the expired deadline before another heartbeat
                    if time.monotonic() - beat >= 10.0:
                        beat = time.monotonic()
                        yield None                        # 10 s heartbeat
                    continue
                if line is None:
                    done = True
                    raise EngineDied(f"the engine stopped unexpectedly (exit code {self.exit_code()})")
                heard = time.monotonic()                  # any line counts as output
                beat = heard
                if line.startswith("T "):
                    allow = silence
                    if cancel.is_set():
                        return
                    yield int(line[2:])
                elif line.startswith("PT ") and text is not None:   # the engine tokenized the text
                    f = line.split()
                    if len(f) >= 3 and f[1].isdigit() and f[2].lstrip("-").isdigit():
                        text.set_counted(int(f[1]), int(f[2]))
                        allow = silence + min(int(f[1]), PP_CHUNK_MAX) / PP_FLOOR_TOK_S if silence > 0 else 0.0
                    yield None
                elif line.startswith("PP "):
                    f = line.split()
                    if len(f) >= 3 and f[1].isdigit() and f[2].isdigit():
                        self.progress = (int(f[1]), int(f[2]))
                        self.prefill_tok_s_mean = float(f[4]) if len(f) >= 5 else None
                        rate, chunk = self.prefill_tok_s_mean or 0.0, int(f[1]) - read_to
                        read_to = int(f[1])
                        if silence > 0 and rate > 0 and chunk > 0:   # the next chunk, as long as this one
                            allow = max(silence, PP_SLACK * chunk / rate)
                    if cancel.is_set():
                        return
                    yield None                            # PP lines reset the 10 s wait, so heartbeat here
                elif line.startswith("PL "):              # live progress inside a chunk: only the display moves
                    self._note_live(line)
                    if cancel.is_set():
                        return
                    yield None
                elif line.startswith("RESUME "):          # reused tokens: the first chunk starts after them
                    try:
                        read_to = int(line.split()[1])
                    except (IndexError, ValueError):
                        pass
                elif line.startswith("DONE"):
                    self._parse_done(line)
                    done = True
                    return
                elif line.startswith("ERR"):
                    done = True
                    err = line[4:].strip()
                    raise request_err(err, text.explain(err) if text is not None else None)
        finally:
            if not done:                                  # consumer left early: STOP and drain to DONE
                if self.can_stop:
                    try:
                        self.proc.stdin.write("STOP\n")
                        self.proc.stdin.flush()
                    except OSError:
                        pass
                # never wait untimed: this holds the request FIFO. With STOP the whole drain gets the current
                # allowance; without it the engine runs to max_new, so each line only has to come in time.
                heard = time.monotonic()
                while True:
                    left = allow - (time.monotonic() - heard) if allow > 0 else None
                    try:
                        if left is not None and left <= 0:
                            raise queue.Empty
                        line = self.lines.get(timeout=left)
                    except queue.Empty:
                        raise self._silent("the engine did not finish the request after it was stopped (STOP) "
                                           f"within {allow:.0f} s") from None
                    if line is None or line.startswith("ERR"):
                        break
                    if line.startswith("DONE"):
                        self._parse_done(line)
                        break
                    if not self.can_stop:
                        heard = time.monotonic()

    def _silent(self, what: str) -> EngineSilent:
        """Kill an engine that lost step with the server so the next request restarts it."""
        self.silent_note = "the engine and the server lost step (engine_silence_s, 0 = wait forever)"
        self.ended = True                               # the next request restarts it
        proc = self.proc
        try:
            proc.kill()
            proc.wait(timeout=20)                       # restart()/close() handles one still exiting
        except (OSError, AttributeError, subprocess.TimeoutExpired):
            pass
        return EngineSilent(f"{what}; the server ended the engine")

    def close(self):
        """End the engine: QUIT, then terminate, then kill, 20 s each; EngineStuck if it still runs."""
        if self.proc is None:
            return
        with self.sleep_lock:                        # a sleep in progress ends first
            if self.asleep:                          # its GPU state back first: QUIT runs CUDA calls
                self.asleep = False
                suspend.resume(self.proc.pid)
        try:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write("QUIT\n")
                    self.proc.stdin.flush()
                    self.proc.stdin.close()  # the engine must see EOF before shutdown
                    self.proc.wait(timeout=self.quit_s)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    self.proc.terminate()
                    self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                raise EngineStuck("the engine is still releasing GPU/RAM; retry unloading after it exits") from None
        except OSError:
            pass
        finally:
            if self.proc.poll() is not None:
                if self.pump is not None:
                    self.pump.join(timeout=2)
                try:
                    self.proc.stdin.close()             # buffered data may flush after exit
                except OSError:
                    pass
                self.proc.stdout.close()
                if self.log not in (None, subprocess.DEVNULL):
                    self.log.close()
                self.proc = None
                self.ended = True
                self.progress, self.last = None, {}

# ===== file: serve/engine/progress.py =====
"""Engine start narration from its log: load bars, warnings, errors and start-failure hints."""
from __future__ import annotations

import os
import re
import sys
import threading
import time

from serve.util.console import tagged
from serve.util.loglevel import ERR, WARN, engine_line


DRAFT_HEAD_FAIL = "the draft head does not fit"
DRAFT_HEAD_HINT = "free VRAM, or run without the drafter (--spec 0)"


def start_failure_hint(log: str | None, offset: int) -> str:
    """Advice when the start failed because the MTP draft head did not fit the free VRAM; "" for other failures."""
    if not log:
        return ""
    try:
        with open(log, "rb") as f:
            f.seek(offset)
            text = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    if DRAFT_HEAD_FAIL not in text:
        return ""
    said = [e.text for x in text.splitlines()
            if (e := engine_line(x.strip())) is not None and e.tag == "basalt mtp"
            and ("draft head over" in e.text or "hint:" in e.text)]
    return ". mtp: " + DRAFT_HEAD_FAIL + ". " + (" ".join(said) if said else "Hint: " + DRAFT_HEAD_HINT)


def start_last_line(log: str | None, offset: int) -> str:
    """The last meaningful log line of this start, without its prefix; "" when none."""
    if not log:
        return ""
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            start = max(offset, f.tell() - 64 * 1024)   # skip earlier starts' lines
            f.seek(start)
            text = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    lines = text.splitlines()[1 if start > offset else 0:]   # not a line cut in half
    for x in reversed(lines):
        x = x.strip()
        e = engine_line(x) if x else None
        if x and (e is None or e.level != WARN):
            return x if e is None else e.text
    return ""


class LoadBars:
    """One progress bar per `basalt load:` target against its planned size (a byte counter before the plan).
    `copy` targets are counted apart from the total. finish() collapses the bars into one result line. Off a
    terminal the rows print as text once per heartbeat, only while their bytes move.

    A row's MB/s comes from the times its byte counts were read: over the latest >= WINDOW s while it moves,
    over the engine's last phase for it once it is complete, and none while it stands still unfinished."""

    WIDTH = 24
    TABLE = "ngram"                                    # the n-gram table locked into RAM: its own row, outside the total
    STILL = 1.0                                        # s without new bytes: the stretch of movement ended
    WINDOW = 1.0                                       # s of the latest bytes a moving row's rate covers
    MIN_SPAN = 0.5                                     # s: a shorter span gives no rate

    def __init__(self, out=None, gpus=None):
        self.out = out or sys.stdout
        self.lock = threading.RLock()                  # the narrator and a thread printing a server line share the screen
        self.gpus = list(gpus or [])                   # CUDA ordinal -> nvidia-smi number
        self.tty = self.out.isatty()
        self.total = 0                                 # announced total bytes (0: none)
        self.targets: dict[tuple[str, bool], dict] = {}   # (target, copy) -> {plan, done, last, seen, start, ...}
        self._beat_seen: dict[tuple[str, bool], int] = {}  # bytes each row showed at the previous busy heartbeat
        self.started = 0                               # targets begun
        self.t0 = None
        self.drawn = 0                                 # lines of bars on screen
        self.shown: list[str] = []                     # the rows on screen
        self.kv: list[tuple[str, int, int, str, int]] = []  # (target, bytes, tokens, kind, max bytes): allocated only

    def name(self, target: str) -> str:
        if target == "ram":
            return "system ram"
        if target.startswith("cuda") and target[4:].isdigit() and int(target[4:]) < len(self.gpus):
            return f"cuda{self.gpus[int(target[4:])]}"
        return target

    def key(self, target: str) -> str:
        """The target's name as a key in a console line (no spaces)."""
        return "ram" if target == "ram" else self.name(target)

    def _target(self, target: str, copy: bool) -> dict:
        # moves: (time, bytes) read while moving, the first one >= WINDOW before the last; phase: the first bytes
        # of the engine's current phase for the target; moved: when its bytes last changed
        return self.targets.setdefault((target, copy), {"plan": 0, "done": 0, "last": 0, "seen": 0, "start": None,
                                                        "moves": [], "phase": None, "moved": None})

    def _sample(self, t: dict, before: int, now: float) -> None:
        """Record the target's bytes as read at `now`; `before` is what it held until this line."""
        have = t["done"] + t["last"]
        if have == before:
            return
        moves = t["moves"]
        if t["phase"] is None:                         # the phase's first bytes: its rate counts from here
            t["phase"] = (now, have)
            moves.clear()
        elif now - t["moved"] > self.STILL:            # moving again after standing still: a fresh window
            moves.clear()
        moves.append((now, have))
        while len(moves) > 2 and moves[1][0] <= now - self.WINDOW:
            moves.pop(0)
        t["moved"] = now

    def _rate(self, t: dict, have: int, now: float) -> str:
        if t["moved"] is None:
            return ""
        if t["plan"] > 0 and have >= t["plan"] and t["phase"] is not None:   # complete: its last phase's rate
            since, base = t["phase"]
        elif now - t["moved"] <= self.STILL:           # moving: the latest window
            since, base = t["moves"][0]
        else:
            return ""
        span = t["moved"] - since
        if span < self.MIN_SPAN or have <= base:
            return ""
        return f"  {(have - base) / 1e6 / span:.0f} MB/s"

    def done(self) -> int:
        return sum(t["done"] + t["last"] for (tg, c), t in self.targets.items() if not c and tg != self.TABLE)

    @staticmethod
    def _bar(done: int, total: int, width: int) -> str:
        f = min(1.0, done / total) if total > 0 else 0.0
        n = int(f * width)
        return "=" * n + (">" if n < width else "") + " " * (width - n - 1)

    def _rows(self, bar: int = WIDTH) -> list[tuple[tuple[str, bool], str]]:
        """(key, row) per started target, oldest start first, with bars `bar` wide."""
        rows = []
        started = [kv for kv in self.targets.items() if kv[1]["start"] is not None]
        now = time.monotonic()
        for (target, copy), t in sorted(started, key=lambda kv: kv[1]["start"]):
            have = t["done"] + t["last"]
            if t["plan"] <= 0 and not have:
                continue                               # planned to hold nothing
            rate = self._rate(t, have, now)
            if target == self.TABLE:
                rows.append(((target, copy), "[server] loading the n-gram table into system ram",
                             have, t["plan"], rate))
                continue
            verb = "copying experts to" if copy else "loading weights into"
            rows.append(((target, copy), f"[server] {verb} {self.name(target)}", have, t["plan"], rate))
        pad = max((len(r[1]) for r in rows), default=0)
        out = []
        for key, label, have, plan, rate in rows:
            if plan <= 0:                              # no plan yet: a count, not a bar
                out.append((key, f"{label.ljust(pad)}  {have / 2**30:5.1f} GiB{rate}"))
            else:
                plan = max(plan, have)
                out.append((key, f"{label.ljust(pad)} [{self._bar(have, plan, bar)}] "
                                 f"{have / 2**30:5.1f}/{plan / 2**30:.1f} GiB{rate}"))
        return out

    def progress(self) -> dict:
        """The load so far: {"percent": int | None, "gib": {target: GiB}} over the weight targets (copies apart)."""
        with self.lock:
            want = self.total or sum(t["plan"] for (tg, c), t in self.targets.items() if not c and tg != self.TABLE)
            have = {self.key(tg): round((t["done"] + t["last"]) / 2**30, 1)
                    for (tg, c), t in self.targets.items() if not c and tg != self.TABLE and t["start"] is not None}
            pct = min(99, int(100 * self.done() / want)) if want > 0 else None
            return {"percent": pct, "gib": have}

    def lines(self) -> list[str]:
        return [text for _, text in self._rows()]

    def moved_lines(self) -> list[str]:
        """Rows for the heartbeat off a terminal: only the targets whose bytes moved since the
        previous call; the first call shows every row."""
        with self.lock:
            rows = self._rows()
            have = {(tg, c): t["done"] + t["last"] for (tg, c), t in self.targets.items()}
            previous = self._beat_seen
            self._beat_seen = have
            return [text for key, text in rows if previous.get(key) != have[key]]

    def _fitted(self) -> list[str]:
        """The rows within the terminal width: shorter bars first, then without the rate, then cut. A wrapped row
        would take two lines and the erase, which counts rows, would leave its first half behind."""
        rows = self.lines()
        try:
            width = os.get_terminal_size(self.out.fileno()).columns - 1
        except (AttributeError, OSError, ValueError):
            width = 0
        if width <= 0 or all(len(r) <= width for r in rows):   # no size known, or it fits
            return rows
        bar = max(8, self.WIDTH - (max(map(len, rows)) - width))
        rows = [text for _, text in self._rows(bar)]
        return [r if len(r) <= width else re.sub(r"  \d+ MB/s$", "", r)[:width] for r in rows]

    def refresh(self) -> None:
        """Redraw the bars when time alone changed them (a rate that ended)."""
        with self.lock:
            if self.tty and self.drawn and self._fitted() != self.shown:
                self._erase()
                self._draw()

    def _erase(self):
        if self.drawn:
            self.out.write("\x1b[1A\x1b[2K" * self.drawn)
            self.drawn = 0

    def _draw(self):
        if self.tty:
            rows = self._fitted()
            for r in rows:
                self.out.write("\x1b[2K" + r + "\n")
            self.drawn = len(rows)
            self.shown = rows
        self.out.flush()

    def say(self, text: str) -> None:
        """A line printed above the bars."""
        with self.lock:
            self._erase()
            self.out.write(text + "\n")
            self._draw()

    def set_kv(self, target: str, nbytes: int, tokens: int, kind: str = "", max_bytes: int = 0) -> None:
        self.kv.append((target, nbytes, tokens, kind, max(max_bytes, nbytes)))

    def kv_lines(self) -> list[str]:
        """One line per device: all the session state allocated there (solo, batch slots, draft K/V).  An elastic
        state reads `backed now/most it can take`."""
        out = []
        for target in dict.fromkeys(x[0] for x in self.kv):
            mine = [x for x in self.kv if x[0] == target]
            total = sum(x[1] for x in mine)
            peak = sum(x[4] for x in mine)
            solo = [x[2] for x in mine if x[3] == ""]
            slots = [x[2] for x in mine if x[3] == "slot"]
            draft = sum(x[1] for x in mine if x[3] == "draft")
            name = self.key(target)
            if peak > total:
                tags = ["elastic"]
                if slots:
                    tags.append((f"solo={solo[0]} " if solo else "") + f"slots={len(slots)}")
                out.append(tagged("kv", f"{total / 2**30:.1f}/{peak / 2**30:.1f}GiB {name} {' '.join(tags)}"))
                continue
            head = f"{total / 2**30:.1f}GiB {name}"
            if not slots and not draft:
                out.append(tagged("kv", head + "".join(f" tokens={tok}" for tok in solo)))
                continue
            parts = [f"solo={tok}" for tok in solo]
            for tok in dict.fromkeys(slots):           # slots of one size read as `slots=2x262144`
                parts.append(f"slots={slots.count(tok)}x{tok}")
            if draft:
                parts.append(f"draft={draft / 2**30:.1f}GiB")
            out.append(tagged("kv", head + " " + " ".join(parts)))
        return out

    def set_total(self, total: int) -> None:
        """The bytes the whole start places."""
        self.total = total

    def set_plan(self, target: str, plan: int, copy: bool = False) -> None:
        if self.t0 is None:
            self.t0 = time.time()
        with self.lock:
            self._target(target, copy)["plan"] = plan
            self._erase()
            self._draw()

    def update(self, target: str, done: int, total: int, copy: bool = False) -> None:
        if self.t0 is None:
            self.t0 = time.time()
        with self.lock:
            t = self._target(target, copy)
            before = t["done"] + t["last"]
            if t["start"] is None:
                t["start"] = self.started
                self.started += 1
            done = min(done, total)
            if t["seen"] != total or done < t["last"]:     # new phase: the last is complete
                t["done"] += t["last"] if t["seen"] else 0
                t["last"] = 0
                t["seen"] = total
                t["phase"] = None
            t["last"] = done
            if target == self.TABLE:
                t["plan"] = total
            self._sample(t, before, time.monotonic())
            self._erase()
            self._draw()

    def finish(self) -> None:
        """The engine is up: replace the bars with the result line."""
        with self.lock:
            self._erase()
            done = self.done()
            want = self.total or sum(t["plan"] for (tg, c), t in self.targets.items() if not c and tg != self.TABLE)
            if done > 0 and done >= want:                  # a failed start is reported elsewhere
                def part(copy):
                    return {tg: t["done"] + t["last"] for (tg, c), t in self.targets.items()
                            if c == copy and tg != self.TABLE and t["done"] + t["last"]}
                parts = " ".join(f"{self.key(tg)}={b / 2**30:.1f}GiB" for tg, b in part(False).items())
                extra = "".join(f" {self.key(tg)}-copy={b / 2**30:.1f}GiB" for tg, b in part(True).items())
                self.out.write(tagged("loaded", f"{done / 2**30:.1f}GiB {time.time() - self.t0:.1f}s  {parts}{extra}") + "\n")
            for line in self.kv_lines():
                self.out.write(line + "\n")
            self.kv.clear()
            self.targets.clear()
            self.out.flush()


_active: "LoadBars | None" = None                     # the bars on screen during a start


def server_line(text: str) -> None:
    """Print a `[server]` line; while load bars are on screen it goes above them, so they are redrawn in place."""
    bars = _active
    if bars is not None:
        bars.say(text)
    else:
        print(text, flush=True)


def load_progress(line: str):
    """(target, done, total, copy) from an engine `basalt load: [copy ]<target> <done> <total>` line, else None."""
    if not line.startswith("basalt load: "):
        return None
    parts = line.split()[2:]
    copy = parts[:1] == ["copy"]
    if copy:
        parts = parts[1:]
    if len(parts) != 3 or parts[0] in ("total", "plan", "kv"):
        return None
    try:
        return parts[0], int(parts[1]), int(parts[2]), copy
    except ValueError:
        return None


def load_plan(line: str):
    """(target, bytes, copy) from an engine `basalt load: plan [copy ]<target> <bytes>` line, else None."""
    parts = line.split()
    if parts[:3] != ["basalt", "load:", "plan"]:
        return None
    parts = parts[3:]
    copy = parts[:1] == ["copy"]
    if copy:
        parts = parts[1:]
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1]), copy


def load_kv(line: str):
    """(target, bytes, tokens, kind, max bytes) from an engine
    `basalt load: kv [slot|draft ]<target> <bytes> <tokens> [<max_bytes>]` line (kind "" for the solo session; the max
    is the bytes when the line has none), else None."""
    parts = line.split()
    if parts[:3] != ["basalt", "load:", "kv"]:
        return None
    rest = parts[3:]
    kind = rest.pop(0) if rest and rest[0] in ("slot", "draft") else ""
    if len(rest) in (3, 4) and all(x.isdigit() for x in rest[1:]):
        return rest[0], int(rest[1]), int(rest[2]), kind, int(rest[3]) if len(rest) == 4 else int(rest[1])
    return None


def load_total(line: str):
    """The bytes of the whole start from an engine `basalt load: total <bytes>` line, else None."""
    parts = line.split()
    if len(parts) == 4 and parts[:3] == ["basalt", "load:", "total"] and parts[3].isdigit():
        return int(parts[3])
    return None


def engine_warning(line: str):
    """The text of an engine warning line, else None."""
    e = engine_line(line)
    return e.text.strip() if e is not None and e.level == WARN and e.text.strip() else None


def engine_ram_floor(line: str):
    """The text after `RAM floor ` of an engine `basalt generate: RAM floor ...` line, else None."""
    e = engine_line(line)
    if e is not None and e.tag == "basalt generate" and e.text.startswith("RAM floor ") and e.text[10:].strip():
        return e.text[10:].strip()
    return None


def engine_error(line: str):
    """The text of an engine error line, else None."""
    e = engine_line(line)
    return e.text.strip() if e is not None and e.level == ERR and e.text.strip() else None


def start_errors(log: str | None, offset: int) -> list[str]:
    """The engine's error lines this start wrote to its log."""
    if not log:
        return []
    try:
        with open(log, "rb") as f:
            f.seek(offset)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, ValueError):
        return []
    return [e for x in lines if (e := engine_error(x.strip())) is not None]


def narrate_start(log_path: str, offset: int, done: threading.Event, heartbeat=20.0, out=None, gpus=None,
                  on_progress=None) -> None:
    """Show load progress and warnings from the engine log, with a heartbeat while idle. `on_progress(dict)` gets
    LoadBars.progress() after each batch of log lines."""
    global _active
    bars = _active = LoadBars(out, gpus)
    t0 = last = time.time()
    beat = t0                                # last busy heartbeat
    pos = offset
    while True:
        stop = done.wait(0.05)               # often: a row's rate is timed by when its lines are read
        try:
            with open(log_path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            chunk = b""
        if chunk.count(b"\n"):
            cut = chunk.rfind(b"\n") + 1
            pos += cut
            for line in chunk[:cut].decode("utf-8", "replace").splitlines():
                if (n := load_total(line)) is not None:
                    bars.set_total(n)
                    last = time.time()
                elif (kv := load_kv(line)) is not None:
                    bars.set_kv(*kv)
                elif (pl := load_plan(line)) is not None:
                    bars.set_plan(*pl)
                    last = time.time()
                elif (p := load_progress(line)) is not None:
                    bars.update(*p)
                    last = time.time()
                elif (w := engine_warning(line)) is not None:
                    bars.say(f"[server] warning: {w}")
                elif (r := engine_ram_floor(line)) is not None:
                    bars.say(tagged("ram", f"floor {r}"))
        if on_progress is not None and chunk.count(b"\n"):
            on_progress(bars.progress())
        bars.refresh()
        if stop:
            bars.finish()
            if _active is bars:
                _active = None
            return
        if not bars.tty and time.time() - beat >= heartbeat:     # busy heartbeat: print only rows that moved
            beat = time.time()
            for r in bars.moved_lines():
                bars.say(r)                                        # prints nothing when no bytes moved
        if time.time() - last > heartbeat:
            last = time.time()
            bars.say(tagged("starting", f"{time.time() - t0:.0f}s"))

# ===== file: serve/engine/slots.py =====
"""First-come admission to the batch slots, with bounded overtaking."""
from __future__ import annotations

import itertools

OVERTAKE_MAX = 4        # times a waiting request may be passed by later ones before it must go next


class Waiter:
    __slots__ = ("ticket", "skipped")

    def __init__(self, ticket: int):
        self.ticket, self.skipped = ticket, 0


class AdmissionQueue:
    """Requests waiting for a free slot, oldest first. The oldest takes the next free slot; a later one may go
    first only when `hit` says its prompt resumes a slot's held prefix, and not past the oldest's overtake budget.
    All calls are made under the caller's lock."""

    def __init__(self, overtake_max: int = OVERTAKE_MAX):
        self.overtake_max, self.waiters, self._tickets = overtake_max, [], itertools.count()

    def join(self) -> Waiter:
        w = Waiter(next(self._tickets))
        self.waiters.append(w)
        return w

    def leave(self, w: Waiter) -> None:
        if w in self.waiters:
            self.waiters.remove(w)

    def may_go(self, w: Waiter, hit: bool = False) -> bool:
        """Whether `w` may take a free slot now; a granted overtake is charged to the oldest waiter."""
        head = self.waiters[0] if self.waiters else w
        if w is head:
            return True
        if hit and head.skipped < self.overtake_max:
            head.skipped += 1
            return True
        return False

    def __len__(self) -> int:
        return len(self.waiters)

# ===== file: serve/engine/suspend.py =====
"""Suspending a running engine's GPU state with the driver's process checkpoint.

The CUDA driver can lock a process, move the contents of its GPU memory to host memory and release the VRAM, then put
them back at the same addresses (cuCheckpointProcessLock / Checkpoint / Restore / Unlock). A sleeping engine keeps its
process, its RAM copy of the experts, its conversations and its learned placement, and holds no VRAM. The driver offers
this on Linux only; elsewhere `available()` is false and the server unloads instead.
"""
from __future__ import annotations

import ctypes
import sys
import threading

_lock = threading.Lock()
_cu = None
_tried = False


def _driver():
    """The CUDA driver with the checkpoint entry points, initialized once; None when absent."""
    global _cu, _tried
    with _lock:
        if _tried:
            return _cu
        _tried = True
        if not sys.platform.startswith("linux"):
            return None
        try:
            cu = ctypes.CDLL("libcuda.so.1")
            for name in ("cuCheckpointProcessLock", "cuCheckpointProcessCheckpoint", "cuCheckpointProcessRestore",
                         "cuCheckpointProcessUnlock"):
                fn = getattr(cu, name)
                fn.argtypes = [ctypes.c_int, ctypes.c_void_p]
                fn.restype = ctypes.c_int
            cu.cuInit.argtypes = [ctypes.c_uint]
            cu.cuInit.restype = ctypes.c_int
            if cu.cuInit(0) != 0:
                return None
        except (OSError, AttributeError):
            return None
        _cu = cu
        return _cu


def available() -> bool:
    return _driver() is not None


def suspend(pid: int) -> str | None:
    """Lock `pid` and move its GPU state to host memory; None on success, else why not (the process runs on)."""
    cu = _driver()
    if cu is None:
        return "the driver cannot suspend a process here"
    rc = cu.cuCheckpointProcessLock(int(pid), None)
    if rc != 0:
        return f"the driver could not lock the engine (CUresult {rc})"
    rc = cu.cuCheckpointProcessCheckpoint(int(pid), None)
    if rc != 0:
        cu.cuCheckpointProcessUnlock(int(pid), None)
        return f"the driver could not save the engine's GPU state (CUresult {rc})"
    return None


def resume(pid: int) -> str | None:
    """Put `pid`'s GPU state back and unlock it; None on success, else why not."""
    cu = _driver()
    if cu is None:
        return "the driver cannot resume a process here"
    rc = cu.cuCheckpointProcessRestore(int(pid), None)
    if rc != 0:
        return f"the driver could not restore the engine's GPU state (CUresult {rc})"
    rc = cu.cuCheckpointProcessUnlock(int(pid), None)
    if rc != 0:
        return f"the driver could not unlock the engine (CUresult {rc})"
    return None

# ===== file: serve/engine/vision.py =====
"""The image encoder process, and the GPU helpers the engine and encoder environments are built from."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
import queue
from pathlib import Path

from serve.engine import lifeline
from serve.engine.image_source import ImageSources, load_image
from serve.engine.process import frame_pipes
from serve.util.console import tagged

# Reads of the encoder's pipe have a timeout: a read that never returns would hold the request turn for good.
# Generous: a cold model load or a slow encode is not a hang. A silent encoder is killed and the next request
# starts a fresh one.
VISION_READY_S = 300.0
VISION_ENCODE_S = 300.0
VISION_CACHE_BYTES = 1 << 30                    # room for about 64 images
IMAGE_MAX_PIXELS = 64 << 20                     # matches the decoder's kMaxImagePixels
IMAGE_REQUEST_BYTES = 1 << 30                   # one request's images, as the encoder will get them
IMAGE_WAITING_BYTES = 2 << 30                   # all requests' read images waiting for their turn to encode
# what Pillow decodes for a client: formats whose header gives the size that decoding will produce, and whose cost is
# in proportion. Out: EPS (runs Ghostscript), ICO (the size checked is not the size decoded), JPEG 2000 (seconds of
# work that stall the server).
PILLOW_FORMATS = ("JPEG", "PNG", "BMP", "GIF", "WEBP", "TIFF", "AVIF", "HEIF", "TGA", "PPM", "QOI")
_decodes = threading.Semaphore(4)               # reads run outside the request turn: this bounds Pillow's memory


def model_plan(engine_bin: str, path: str, value, words: list[str]) -> dict:
    """The engine's choice for the gpu setting (`basalt --gpu-plan`) for a model with its flags: the cards, main first, as
    "devices" (their CUDA_VISIBLE_DEVICES spelling) and "cards", plus the lines to print once. The engine parses the flags
    by the model's own rules first and loads nothing. ValueError with the engine's reason."""
    text = gpu_text(value)
    cmd = [engine_bin, "--model", path, *words, "--gpu-plan"] + (["--gpu", text] if text else [])
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f"{engine_bin} could not check this model's flags") from None
    errors = [ln.split("error: ", 1)[1].strip() for ln in run.stderr.splitlines() if "error: " in ln]
    try:
        plan = json.loads(run.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        plan = {}
    if plan.get("error"):
        raise ValueError(plan["error"])
    if run.returncode != 0 or not plan:
        raise ValueError("; ".join(errors) or f"{engine_bin} could not check this model's flags (exit code {run.returncode})")
    return plan


def flag_refused(engine_bin: str, path: str, group: list[str]) -> bool:
    """Whether the model in the file `path` has no such flag; `group` is the flag and its values."""
    try:
        run = subprocess.run([engine_bin, "--model", path, *group, "--gpu-plan"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return f"unknown argument: {group[0]}" in run.stderr


class Vision:
    """The resident `basalt-vision` encoder: `ENC <image> <out>` lines in, embeddings out, cached by image hash."""

    def __init__(self, cfg: dict, log=None, env: dict | None = None, start: bool = True):
        """`start` false: down until the service restarts it, as an unloaded encoder is."""
        args = [cfg["vision_bin"], "--mmproj", cfg["mmproj"], "--model", cfg["model"]] + (["--gpu"] if cfg.get("gpu") else [])
        self.dir = Path(tempfile.mkdtemp(prefix="basalt-vision-"))
        self.spawn = (args, log, env)                   # to start again
        self.stopped = False
        self.ready_s = 0.0                              # last start's time to READY
        self.proc = None
        if start:
            self._start()
        else:
            self.stopped = True
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[Path, int, int]] = {}  # LRU cache: file, image tokens, bytes
        self.cache_bytes = 0
        self.pinned: dict[str, int] = {}                # image -> requests using it

    def _start(self):
        args, log, env = self.spawn
        t0 = time.time()
        self.proc = lifeline.popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log or subprocess.DEVNULL,
                                   text=True, encoding="utf-8", bufsize=1, env=env)
        frame_pipes(self.proc, False)
        try:
            line = self._readline(VISION_READY_S, "the vision encoder did not start")
        except RuntimeError:                            # silent: it was ended, and is not left half-started
            self.stopped = True
            raise
        if not line.startswith("READY"):
            raise RuntimeError("the vision encoder did not start: " + line.strip())
        self.ready_s = time.time() - t0
        self.stopped = False

    def _readline(self, timeout: float, what: str) -> str:
        """One line from the encoder, waiting at most `timeout` s. On a timeout the encoder is killed, so the next
        request starts a fresh one, and the read raises RuntimeError instead of holding the request turn."""
        proc = self.proc
        got: queue.Queue = queue.Queue()

        def read():
            try:
                got.put(proc.stdout.readline())
            except (OSError, ValueError):               # killed under the read
                got.put("")

        threading.Thread(target=read, daemon=True).start()
        try:
            return got.get(timeout=timeout)
        except queue.Empty:
            try:
                proc.kill()
            except OSError:
                pass
            raise RuntimeError(f"{what}: it said nothing for {timeout:.0f} s") from None

    def alive(self) -> bool:
        return not self.stopped and self.proc.poll() is None

    def unload(self):
        """Stop the encoder; encoded images stay cached on disk."""
        self.close()
        self.stopped = True

    def restart(self):
        """Start the encoder again, keeping the cache."""
        try:
            if self.proc is not None:
                self.proc.kill()
        except OSError:
            pass
        self._start()

    def read(self, sources: list, allowed: ImageSources | None = None) -> list[bytes]:
        """The request's images, read under its policy and normalized, each source once. Called outside the request
        turn, so a slow source holds up only its own request."""
        seen: dict[str, bytes] = {}
        out, total = [], 0
        for source in sources:
            if not isinstance(source, str) or source not in seen:
                data = self.normalize(load_image(source, allowed))
                total += len(data)
                if total > IMAGE_REQUEST_BYTES:
                    raise ValueError(f"the request's images are larger than {IMAGE_REQUEST_BYTES >> 20} MiB")
                seen[source] = data
            out.append(seen[source])
        return out

    @staticmethod
    def _exif_rotated(data: bytes) -> bool:
        """A JPEG whose EXIF Orientation says it is stored turned (phones do this for portrait photos): the decoder
        ignores the tag. False without Pillow, or when the tag is missing, 1 or unreadable."""
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(data), formats=["JPEG"]) as im:
                return im.getexif().get(0x0112, 1) not in (None, 0, 1)
        except Exception:  # noqa: BLE001 - any failure leaves the image as it was sent
            return False

    @staticmethod
    def normalize(data: bytes) -> bytes:
        """Formats the decoder reads pass through; others are converted to PNG. A JPEG with an EXIF Orientation
        other than 1 is turned upright first (needs Pillow). An image past IMAGE_MAX_PIXELS is refused before its
        pixels are loaded; the decoder enforces the same budget for the formats it reads itself."""
        jpeg = data[:3] == b"\xff\xd8\xff"
        if (jpeg and not Vision._exif_rotated(data)) or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"BM" or \
                data[:6] in (b"GIF87a", b"GIF89a"):
            return data
        with _decodes:
            return Vision._converted(data)

    @staticmethod
    def _converted(data: bytes) -> bytes:
        """The image through Pillow, as a PNG."""
        try:
            import io
            from PIL import Image, UnidentifiedImageError
        except ImportError:
            raise ValueError("this image format needs Pillow (python -m pip install pillow); JPEG, PNG, BMP and "
                             "GIF work without it") from None
        try:
            Image.init()                                 # plugin sets differ between builds
            im = Image.open(io.BytesIO(data), formats=[f for f in PILLOW_FORMATS if f in Image.OPEN])
            width, height = im.size
            if width * height > IMAGE_MAX_PIXELS:
                raise ValueError(f"the image is {width}x{height} pixels; the limit is "
                                 f"{IMAGE_MAX_PIXELS >> 20} megapixels")
            im.load()
            from PIL import ImageOps
            im = ImageOps.exif_transpose(im)             # the camera's orientation tag, applied
        except ValueError:
            raise                                        # the pixel budget's own message
        except Image.DecompressionBombError:
            raise ValueError(f"the image is larger than {IMAGE_MAX_PIXELS >> 20} megapixels") from None
        except UnidentifiedImageError:
            raise ValueError("the image could not be read: not a format this server decodes") from None
        except Exception as e:
            raise ValueError(f"the image could not be read ({e})") from None
        if im.mode in ("RGBA", "LA", "P") and "transparency" in im.info or im.mode in ("RGBA", "LA"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))   # transparency becomes white
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()

    def encode(self, images: list[bytes]) -> tuple[Path, list[int]]:
        """Encode a request's images (from read) into one embeddings file; return it and each image's token count."""
        pins = []
        try:
            encoded = self._encode_pinned(images, pins)
            combined = self.dir / f"req-{uuid.uuid4().hex[:12]}.sve"
            try:
                with open(combined, "wb") as f:
                    for path, _, _ in encoded:
                        f.write(path.read_bytes())
            except BaseException:                       # no partial file is left behind
                combined.unlink(missing_ok=True)
                raise
            return combined, [n for _, n, _ in encoded]
        finally:
            with self.lock:
                for k in pins:
                    self.pinned[k] -= 1
                    if not self.pinned[k]:
                        del self.pinned[k]
                self._evict()

    def _pin(self, key: str, pins: list[str]) -> None:
        self.pinned[key] = self.pinned.get(key, 0) + 1
        pins.append(key)
        if key in self.cache:
            self.cache[key] = self.cache.pop(key)

    def _encode_pinned(self, images: list[bytes], pins: list[str]) -> list[tuple[Path, int, int]]:
        return [self._encode_one(data, pins) for data in images]

    def _encode_one(self, data: bytes, pins: list[str]) -> tuple[Path, int, int]:
        key = hashlib.sha256(data).hexdigest()[:32]
        with self.lock:
            self._pin(key, pins)
            if key in self.cache:
                return self.cache[key]
            img, out = self.dir / f"{key}.img", self.dir / f"{key}.sve"
            img.write_bytes(data)
            try:
                line = self._enc(img, out)
            finally:                                                   # also when the pipe is gone
                img.unlink(missing_ok=True)
            if not line.startswith("OK"):
                raise ValueError("the image could not be read: " + (line[4:] if line.startswith("ERR") else
                                                                     "the vision encoder stopped"))
            return self._remember(key, out, int(line.split()[1]))

    def _enc(self, img: Path, out: Path) -> str:
        """One ENC round trip with the sidecar: its `OK <n> ...` or `ERR ...` line."""
        self.proc.stdin.write(f"ENC {img} {out}\n")
        self.proc.stdin.flush()
        try:
            return self._readline(VISION_ENCODE_S, "the image could not be read").strip()
        except RuntimeError as e:                       # silent: the encoder was ended
            self.stopped = True                         # alive() is False, so the next request starts it again
            raise ValueError(str(e)) from None

    def _remember(self, key: str, out: Path, n: int) -> tuple[Path, int, int]:
        size = out.stat().st_size
        self.cache[key] = (out, n, size)
        self.cache_bytes += size
        self._evict()
        return self.cache[key]

    def _evict(self) -> None:
        for k in [k for k in self.cache if k not in self.pinned]:
            if self.cache_bytes <= VISION_CACHE_BYTES:
                break
            path, _, size = self.cache.pop(k)
            path.unlink(missing_ok=True)
            self.cache_bytes -= size

    def close(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.write("QUIT\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


class EngineVision(Vision):
    """The encoder inside the engine process (`--vision dynamic`): `ENC` lines go over the engine's own pipe, the engine
    borrows expert-cache slots for the encode and gives them back. Same cache, same files as the sidecar's."""
    in_engine = True

    def __init__(self, cfg: dict):
        self.dir = Path(tempfile.mkdtemp(prefix="basalt-vision-"))
        self.engine = None                              # set by attach() once the engine exists
        self.stopped = False
        self.ready_s = 0.0
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[Path, int, int]] = {}
        self.cache_bytes = 0
        self.pinned: dict[str, int] = {}

    def attach(self, engine) -> None:
        self.engine = engine

    def alive(self) -> bool:                            # the engine's life is the engine's business
        return True

    def unload(self):
        """The engine unloads itself; encoded images stay cached on disk."""

    def restart(self):
        pass

    def close(self):
        pass

    def _encode_pinned(self, images: list[bytes], pins: list[str]) -> list[tuple[Path, int, int]]:
        """Every image not cached yet goes to the engine in one ENC, so it borrows its slots once."""
        keys, datas = [], {}
        for data in images:
            key = hashlib.sha256(data).hexdigest()[:32]
            keys.append(key)
            datas[key] = data
        with self.lock:
            for k in keys:
                self._pin(k, pins)
            todo = [k for k in dict.fromkeys(keys) if k not in self.cache]
            jobs = [(self.dir / f"{k}.img", self.dir / f"{k}.sve") for k in todo]
            try:
                for (img, _), k in zip(jobs, todo):
                    img.write_bytes(datas[k])
                if jobs:
                    lines = self.engine.encode_images(jobs)
                    for k, (_, out), line in zip(todo, jobs, lines):
                        self._remember(k, out, int(line.split()[1]))
            finally:
                for img, _ in jobs:
                    img.unlink(missing_ok=True)
            return [self.cache[k] for k in keys]


def gpu_text(value) -> str:
    """The gpu setting as the engine reads it: "" (unset), "auto" or "A,B,..."; a config may give a number or a list."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(x).strip() for x in value)
    return str(value).strip()


def check_gpu(value) -> None:
    """ValueError unless the gpu setting reads as auto or distinct GPU numbers."""
    text = gpu_text(value)
    if text in ("", "auto"):
        return
    items = [x.strip() for x in text.split(",")]
    if not all(x.isdigit() for x in items) or len(set(int(x) for x in items)) != len(items):
        raise ValueError(text)


def gpu_list(cfg: dict) -> list:
    """The cards the engine runs on, main first, as CUDA_VISIBLE_DEVICES names them (nvidia-smi numbers, or a UUID
    where the environment gave one); [] when none was chosen (the mock engine)."""
    g = gpu_text(cfg.get("gpu"))
    if g in ("", "auto"):
        return []
    return [int(x) if x.isdigit() else x for x in (w.strip() for w in g.split(",")) if x]


def gpus_in_play(cfg: dict, args: list[str] | None = None) -> list[int]:
    """The nvidia-smi numbers of the GPUs the engine uses, its main card first; [0] if none."""
    smi = [i for i in cfg.get("gpu_smi") or [] if isinstance(i, int) and i >= 0]
    if smi:
        return smi
    return [i for i in gpu_list(cfg) if isinstance(i, int)] or [0]


def engine_args(cfg: dict) -> list[str]:
    """The engine arguments the model file carries."""
    return list(cfg["args"])


def child_env(cfg: dict) -> dict:
    """The engine environment: only the --gpu cards are visible."""
    env = dict(os.environ)
    if gpu_list(cfg):                              # number cards as nvidia-smi does
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"      # CUDA's default order differs
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_list(cfg))
    return env


VISION_MODES = ("off", "dynamic", "cpu")
VISION_USAGE = "vision: use one of " + ", ".join(VISION_MODES) + " or a GPU number, as nvidia-smi numbers them"


def vision_mode(value) -> str:
    """The --vision setting: one of VISION_MODES (None: "off") or a GPU number as a string (the sidecar's own
    card); booleans and anything else are a ValueError."""
    if value is None:
        return "off"
    text = str(value).strip().lower()
    if text in VISION_MODES:
        return text
    if text.isascii() and text.isdigit():                 # odd Unicode digits are the error, not int()'s guess
        return str(int(text))
    raise ValueError(f"{VISION_USAGE} (got {value!r})")


def vision_card(mode) -> int | None:
    """The GPU number a vision mode names (the sidecar's own card); None for off, dynamic and cpu."""
    text = str(mode).strip()
    return int(text) if text.isascii() and text.isdigit() else None


def vision_env(env: dict, mode) -> dict:
    """The encoder process's environment: a GPU-numbered vision mode runs the sidecar on that card alone; the
    other modes inherit the engine's environment."""
    card = vision_card(mode)
    if card is None:
        return env
    env = dict(env)
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["CUDA_VISIBLE_DEVICES"] = str(card)
    return env


def gpu_card_name(index: int) -> str | None:
    """A card's name from one nvidia-smi query; None if unavailable."""
    try:
        out = subprocess.run(["nvidia-smi", "-i", str(index), "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5)
        name = out.stdout.strip().splitlines()[0].strip() if out.returncode == 0 and out.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return None
    return name.removeprefix("NVIDIA GeForce ").removeprefix("NVIDIA ") or None


def vision_item(vision: dict | None, cfg: dict) -> str | None:
    """The startup item for image input: where the encoder runs; None when it is off."""
    if not vision:
        return None
    gpu = vision_card(vision.get("mode") or "cpu")
    if gpu is not None:
        return f"vision=gpu{gpu}"
    if vision.get("mode") == "dynamic":
        listed = gpu_list(cfg)
        return f"vision=gpu{listed[0] if listed else 0}"
    return "vision=cpu"
# ===== file: serve/frontend.py =====
"""Text-only layer between API clients and the engine (no model, no GPU).

  * Normalizes OpenAI and Anthropic requests into the chat template's message form.
  * Renders the model's Jinja chat template as Hugging Face does.
  * Parses streamed output into reasoning, content and `<tool_call><function=NAME><parameter=P>...` tool calls,
    never emitting a partial tag.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import jinja2
from jinja2.sandbox import ImmutableSandboxedEnvironment


# ------------------------------------------------------------------------------------------------ template
class TemplateRequestError(jinja2.exceptions.TemplateError, ValueError):
    """The template refused the messages; a ValueError so the client gets a 400 with its message."""


class ChatTemplate:
    """The model's chat template, rendered with the Jinja settings of transformers' apply_chat_template."""

    def __init__(self, path: str | Path):
        def raise_exception(message):
            raise TemplateRequestError(message)

        def tojson(x, ensure_ascii=False, indent=None, separators=None, sort_keys=False):
            return json.dumps(x, ensure_ascii=ensure_ascii, indent=indent, separators=separators, sort_keys=sort_keys)

        env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True, extensions=["jinja2.ext.loopcontrols"])
        env.filters["tojson"] = tojson
        env.globals["raise_exception"] = raise_exception
        self.source = Path(path).read_text(encoding="utf-8")
        self.template = env.from_string(self.source)

    def render(self, messages: list[dict], tools: list[dict] | None = None, add_generation_prompt: bool = True,
               **kwargs) -> str:
        return self.template.render(messages=messages, tools=tools, add_generation_prompt=add_generation_prompt,
                                    **kwargs)


# ------------------------------------------------------------------------------------------------ requests
def _text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") in
                   ("text", "input_text", None))


IMAGE_PARTS = ("image_url", "input_image", "image")

# client spellings of a thinking level -> low, medium, high, max or none; a dialect says what its template takes for each
EFFORT = {"none": None, "off": None, "minimal": None, "disabled": None, "false": None,
          "low": "low", "medium": "medium", "high": "high", "xhigh": "max", "max": "max", "maximum": "max"}


def effort_kwargs(value) -> dict:
    """A client's reasoning effort as template kwargs; unknown values are a 400."""
    if value is None or value == "":
        return {}
    if value is False:
        return {"enable_thinking": False}
    key = str(value).strip().lower()
    if key.isdigit() and 1 <= int(key) <= 100:   # the dialect takes a number or refuses it
        return {"reasoning_effort": int(key)}
    if key not in EFFORT:
        raise ValueError(f"unknown reasoning effort {value!r}: use none, low, medium, high or max")
    level = EFFORT[key]
    return {"enable_thinking": False} if level is None else {"reasoning_effort": level}


def budget_effort(tokens) -> dict:
    """Anthropic's thinking budget (budget_tokens) -> a level: under 2K low, under 8K medium, else high."""
    try:
        n = int(tokens)
    except (TypeError, ValueError):
        return {}
    return {"reasoning_effort": "low" if n < 2048 else "medium" if n < 8192 else "high"}


def _has_image(content) -> bool:
    return isinstance(content, list) and any(isinstance(p, dict) and p.get("type") in IMAGE_PARTS for p in content)


def _image_source(part: dict) -> str:
    """An image part's source as a data: URL, http(s) URL or local path (OpenAI, Responses and Anthropic shapes)."""
    if part.get("type") == "image":
        src = part.get("source") if isinstance(part.get("source"), dict) else {}
        if src.get("type") == "base64":
            return f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
        return src.get("url") or src.get("path") or ""
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    return url or ""


def _parts_of(content):
    """The one form of message and tool result content in every API: a string without images, else its list of text
    and image items."""
    if isinstance(content, dict):
        content = [content]
    if not _has_image(content):
        return _text_of(content)
    items = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in IMAGE_PARTS:
            items.append({"type": "image", "source": _image_source(part)})
        elif part.get("type") in ("text", "input_text", None) and "text" in part:
            items.append({"type": "text", "text": part.get("text", "")})
    return items


def images_of(messages: list[dict]) -> list[str]:
    """The image sources of the conversation in prompt order."""
    return [item["source"] for m in messages if isinstance(m.get("content"), list)
            for item in m["content"] if item.get("type") == "image"]


# Literal think and vision tags in message text must stay plain text, but the tokenizer would turn them into
# special tokens. Before rendering they are swapped for these private-use marks and encoded as ordinary text.
THINK_TAGS = {"<think>": "\U000F0E01", "</think>": "\U000F0E02"}
# vision markers are special only where the template writes them for an image item
VISION_TAGS = {"<|vision_start|>": "\U000F0E03", "<|image_pad|>": "\U000F0E04", "<|vision_end|>": "\U000F0E05",
               "<|video_pad|>": "\U000F0E06"}
# The tool-call tags are special tokens the tokenizer matches in any text; a quoted one would hand the model the real
# marker. An assistant message keeps its own, since a client replaying raw call text sends the tokens the model wrote.
TOOL_LITERAL_TAGS = {"</tool_call>": "\U000F0E07", "<tool_call>": "\U000F0E08", "<tool_response>": "\U000F0E09",
                     "</tool_response>": "\U000F0E0A"}
LITERAL_TAGS = {**THINK_TAGS, **VISION_TAGS, **TOOL_LITERAL_TAGS}
THINK_MARKS = {v: k for k, v in LITERAL_TAGS.items()}
CONTROL_MARK0 = 0xF0E10      # control-token marks start here


def literal_tags(controls) -> dict[str, str]:
    """LITERAL_TAGS plus a mark for each control token's text in `controls`, longest first like the tokenizer."""
    tags = {**LITERAL_TAGS, **{c: chr(CONTROL_MARK0 + k) for k, c in enumerate(c for c in controls
                                                                                if c not in LITERAL_TAGS)}}
    return dict(sorted(tags.items(), key=lambda t: -len(t[0])))


def _mark(text: str, tags: dict[str, str] = LITERAL_TAGS) -> str:
    for tag, mark in tags.items():
        text = text.replace(tag, mark)
    return text


def _mark_deep(v, tags: dict[str, str] = LITERAL_TAGS):
    if isinstance(v, str):
        return _mark(v, tags)
    if isinstance(v, dict):
        return {k: _mark_deep(x, tags) for k, x in v.items()}
    if isinstance(v, list):
        return [_mark_deep(x, tags) for x in v]
    return v


def _has_tag(v, tags: dict[str, str] = LITERAL_TAGS) -> bool:
    if isinstance(v, str):
        return any(tag in v for tag in tags)
    if isinstance(v, dict):
        return any(_has_tag(x, tags) for x in v.values())
    if isinstance(v, list):
        return any(_has_tag(x, tags) for x in v)
    return False


_TOOL_RESULT_OPEN, _TOOL_RESULT_CLOSE = "<tool_response>", "</tool_response>"


def _keep_tool_result_wrap(content, tags: dict[str, str]):
    """A user message whose whole text is <tool_response>...</tool_response> (a client that formats a tool result the way
    the template does) keeps that outer pair as the markers, since the template tells such a turn from a question by
    exactly that pair; only the text inside is marked. None: not such a message."""
    o, c = _TOOL_RESULT_OPEN, _TOOL_RESULT_CLOSE

    def wrapped(text: str) -> bool:
        body = text.strip()
        return len(body) >= len(o) + len(c) and body.startswith(o) and body.endswith(c)

    def wrap(text: str, first: bool, last: bool) -> str:
        i = text.index(o) + len(o) if first else 0
        j = text.rindex(c) if last else len(text)
        head = _mark(text[:i - len(o)], tags) + o if first else ""
        tail = c + _mark(text[j + len(c):], tags) if last else ""
        return head + _mark(text[i:j], tags) + tail

    if isinstance(content, str):
        return wrap(content, True, True) if wrapped(content) else None
    if not (isinstance(content, list) and content and all(
            isinstance(x, dict) and (isinstance(x.get("text"), str) or x.get("type") == "image") for x in content)):
        return None
    texts = [x.get("text", "") for x in content]
    if not wrapped("".join(texts)) or not (texts[0].lstrip().startswith(o) and texts[-1].rstrip().endswith(c)):
        return None          # the pair is split inside a part: mark it all
    if len(content) == 1:
        return [{**content[0], "text": wrap(texts[0], True, True)}]
    return ([{**content[0], "text": wrap(texts[0], True, False)}] + [_mark_deep(x, tags) for x in content[1:-1]] +
            [{**content[-1], "text": wrap(texts[-1], False, True)}])


def mark_think_literals(messages: list[dict], tools: list[dict] | None, tags: dict[str, str] = LITERAL_TAGS):
    """(messages, tools, changed) with literal tags in their text swapped for `tags`' marks; unchanged objects come
    back when there are none. An assistant message opening with a whole <think>...</think> block keeps it."""
    if not _has_tag(messages, tags) and not _has_tag(tools, tags):
        return messages, tools, False
    out = []
    own = {t: v for t, v in tags.items() if t not in TOOL_LITERAL_TAGS}     # what an assistant message keeps as written
    for m in messages:
        m = dict(m)
        content = m.get("content")
        mine = own if m.get("role") == "assistant" else tags
        for k, v in m.items():
            if k != "role":
                m[k] = _mark_deep(v, mine)
        if m.get("role") == "user":
            kept = _keep_tool_result_wrap(content, tags)
            if kept is not None:
                m["content"] = kept
        if m.get("role") == "assistant" and isinstance(content, str) and content.lstrip().startswith("<think>") \
                and "</think>" in content:
            i, j = content.index("<think>") + len("<think>"), content.index("</think>")
            m["content"] = (content[:i] + _mark(content[i:j], own) + "</think>" +
                            _mark(content[j + len("</think>"):], own))
        out.append(m)
    return out, _mark_deep(tools, tags), True


_THINK_MARK_RE = re.compile("|".join(THINK_MARKS))


def unmark_think_literals(prompt: str, tags: dict[str, str] = LITERAL_TAGS) -> tuple[str, list[tuple[int, int]]]:
    """The prompt with marks restored to tag text, and those tags' (start, end) spans to encode as plain text."""
    marks = THINK_MARKS if tags is LITERAL_TAGS else {v: k for k, v in tags.items()}
    mark_re = _THINK_MARK_RE if tags is LITERAL_TAGS else re.compile("|".join(map(re.escape, marks)))
    out, spans, pos, n = [], [], 0, 0
    for m in mark_re.finditer(prompt):
        out.append(prompt[pos:m.start()])
        n += m.start() - pos
        tag = marks[m.group(0)]
        out.append(tag)
        spans.append((n, n + len(tag)))
        n += len(tag)
        pos = m.end()
    out.append(prompt[pos:])
    return "".join(out), spans


def _late_system_to_user(messages: list[dict]) -> list[dict]:
    """Turn system messages after the first into user messages in place; the template allows system only first,
    and merging would change the prompt start and lose the cache."""
    return [dict(m, role="user") if m.get("role") == "system" and i > 0 else m for i, m in enumerate(messages)]


def _object_list(value, name: str) -> list[dict]:
    """A list of objects from `value` (a double-encoded JSON string is decoded); None is empty, anything else a
    ValueError naming `name`."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError(f"{name} must be a list of objects (a string was sent that is not JSON)") from None
    if not isinstance(value, list) or not all(isinstance(m, dict) for m in value):
        raise ValueError(f"{name} must be a list of objects")
    return value


_TOOL_ORDERS: "OrderedDict[tuple, None]" = OrderedDict()   # orders of tool names seen, least recently used first
_TOOL_ORDERS_LOCK = threading.Lock()
_TOOL_ORDERS_MAX = 64


def stable_tool_order(tools: list[dict], name_of) -> list[dict]:
    """Put `tools` in the order their names were first seen, so a client that lists the same tools in another order
    keeps the prompt prefix (the template renders tools before the system prompt, so one swap re-reads the whole
    conversation). The closest remembered order that shares at least half the names decides: its tools keep its
    order and tools it lacks follow in the order sent, which moves the prefix back the least. A tool set with no such
    relative, a single tool, or duplicate names is left as sent. Based on Strata 1a62f6d."""
    if len(tools) < 2:
        return tools
    names = [name_of(t) for t in tools]
    have = set(names)
    if len(have) != len(names):
        return tools
    with _TOOL_ORDERS_LOCK:
        best, shared = None, 0
        for order in reversed(_TOOL_ORDERS):         # most recent first, so ties go to the newest
            n = len(have.intersection(order))
            if n > shared:
                best, shared = order, n
        if best is None or shared * 2 < len(names):
            result = tuple(names)
        else:
            known = set(best)
            result = tuple([n for n in best if n in have] + [n for n in names if n not in known])
        _TOOL_ORDERS.pop(result, None)
        _TOOL_ORDERS[result] = None
        while len(_TOOL_ORDERS) > _TOOL_ORDERS_MAX:
            _TOOL_ORDERS.popitem(last=False)
    if list(result) == names:
        return tools
    pos = {n: k for k, n in enumerate(result)}
    return sorted(tools, key=lambda t: pos[name_of(t)])


def _tool_list(value, wrapper: str | None) -> list[dict]:
    """A request's tools as named objects, in the OpenAI shape (`wrapper` "function") or Anthropic's (None);
    anything else is a ValueError, no value is no tools. A tool's schema ("parameters" / "input_schema") is an object
    when it is there: a string or a list would otherwise fail on the model's first call of that tool."""
    if not value:
        return []
    shape = ('{"type": "function", "function": {"name": ..., "parameters": {...}}}' if wrapper else
             '{"name": ..., "input_schema": {...}}')
    try:
        tools = _object_list(value, "tools")
    except ValueError:
        raise ValueError(f"tools must be a list of tool objects, each {shape}") from None
    key = "parameters" if wrapper else "input_schema"
    for i, t in enumerate(tools):
        fn = t.get(wrapper, t) if wrapper and t.get("type") == wrapper else t
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"]:
            raise ValueError(f"tools[{i}] has no name: each tool must be {shape}")
        schema = fn.get(key)
        if schema is not None and not isinstance(schema, dict):
            raise ValueError(f'tools[{i}] ({fn["name"]}): "{key}" must be an object (the JSON schema of its '
                             f"parameters), not {type(schema).__name__}")
    return stable_tool_order(tools, lambda t: (t.get(wrapper, t) if wrapper and t.get("type") == wrapper else t)["name"])


def _order_chat_tool_results(messages: list[dict]) -> None:
    """Restore a complete, unambiguous run of tool results to its assistant's call order."""
    i = 0
    while i < len(messages):
        if messages[i]["role"] != "tool":
            i += 1
            continue
        j = i + 1
        while j < len(messages) and messages[j]["role"] == "tool":
            j += 1
        prev = messages[i - 1] if i else {}
        calls = [c.get("_call_id") for c in prev.get("tool_calls") or []] if prev.get("role") == "assistant" else []
        results = [m.get("_call_id") for m in messages[i:j]]
        if len(calls) == len(results) and all(isinstance(cid, str) and cid for cid in calls + results):
            if len(set(calls)) == len(calls) and len(set(results)) == len(results) and set(calls) == set(results):
                rank = {cid: n for n, cid in enumerate(calls)}
                messages[i:j] = sorted(messages[i:j], key=lambda m: rank[m["_call_id"]])
        i = j
    for message in messages:
        message.pop("_call_id", None)
        for call in message.get("tool_calls") or []:
            call.pop("_call_id", None)


def openai_to_messages(req: dict) -> tuple[list[dict], list[dict] | None, dict]:
    """OpenAI Chat Completions -> (template messages, template tools, template kwargs)."""
    messages = []
    for m in _object_list(req.get("messages"), "messages"):
        role = m.get("role")
        if role == "developer":
            role = "system"
        out = {"role": role, "content": _parts_of(m.get("content"))}
        if role == "tool":
            out["_call_id"] = m.get("tool_call_id")
        if m.get("reasoning_content"):
            out["reasoning_content"] = m["reasoning_content"]
        if m.get("tool_calls"):
            calls = []
            for c in _object_list(m["tool_calls"], "tool_calls"):
                fn = c.get("function", c)
                if not isinstance(fn, dict):
                    raise ValueError("tool_calls must be a list of objects (each with a \"function\" object)")
                args = fn.get("arguments")
                if isinstance(args, str):               # the template needs a mapping
                    args = json.loads(args) if args.strip() else {}
                calls.append({"function": {"name": fn.get("name"), "arguments": args or {}}, "_call_id": c.get("id")})
            out["tool_calls"] = calls
        messages.append(out)
    _order_chat_tool_results(messages)
    tools = [t.get("function", t) if t.get("type") == "function" else t
             for t in _tool_list(req.get("tools"), "function")] or None
    kwargs = {}
    # "reasoning_effort", or Responses-style "reasoning": {"effort": ...}
    reasoning = req.get("reasoning") if isinstance(req.get("reasoning"), dict) else {}
    kwargs.update(effort_kwargs(req.get("reasoning_effort") or reasoning.get("effort")))
    # chat_template_kwargs: enable_thinking / reasoning_effort
    for k, v in (req.get("chat_template_kwargs") or {}).items():
        if k == "enable_thinking" and not v:
            kwargs = {"enable_thinking": False}
        elif k == "reasoning_effort" and "enable_thinking" not in kwargs:
            kwargs.update(effort_kwargs(v))
    return _late_system_to_user(messages), tools, kwargs


BILLING_HEADER = "x-anthropic-billing-header:"


def pin_billing_stamp(system: str) -> str:
    """Pin Claude Code's per-request billing stamp to f's. The header at the start of its system prompt carries a
    `cch=` value that changes on every request and a `cc_version` 4th part that changes on every session; left alone
    they change the prompt ahead of the cached prefix. Only a header at the very start of the system text is
    touched, and only inside its first 160 characters."""
    if not system.startswith(BILLING_HEADER):
        return system
    s = list(system)
    cch = system.find("cch=", len(BILLING_HEADER))
    if 0 <= cch <= 160:
        v, end = cch + 4, system.find(";", cch + 4)
        if end > v and end - v <= 16:
            s[v:end] = "f" * (end - v)
    cv = system.find("cc_version=")
    if 0 <= cv <= 160:
        v, end = cv + len("cc_version="), system.find(";", cv)
        if v < end and end - v <= 64:
            parts = system[v:end].split(".")
            if len(parts) > 3:
                tail = v + len(".".join(parts[:3])) + 1
                s[tail:end] = "f" * (end - tail)
    return "".join(s)


def anthropic_to_messages(req: dict, think_unasked: bool = True) -> tuple[list[dict], list[dict] | None, dict]:
    """Anthropic Messages -> (template messages, tools, kwargs). `think_unasked`: a request that asks nothing about
    thinking gets the template default; False renders it without thinking."""
    messages = []
    system = req.get("system")
    if system:
        system = _parts_of(system)
        messages.append({"role": "system", "content": pin_billing_stamp(system) if isinstance(system, str) else system})
    for m in _object_list(req.get("messages"), "messages"):
        content = m.get("content")
        if isinstance(content, str):
            messages.append({"role": m["role"], "content": content})
            continue
        parts, reasoning, calls = [], [], []
        for block in content or []:
            kind = block.get("type")
            if kind in ("text", "input_text", None) or kind in IMAGE_PARTS:
                parts.append(block)
            elif kind == "thinking":
                reasoning.append(block.get("thinking", ""))
            elif kind == "tool_use":
                calls.append({"function": {"name": block.get("name"), "arguments": block.get("input") or {}}})
            elif kind == "tool_result":
                messages.append({"role": "tool", "content": _parts_of(block.get("content"))})
        if parts or calls or reasoning:
            out = {"role": m["role"], "content": _parts_of(parts)}
            if reasoning:
                out["reasoning_content"] = "".join(reasoning)
            if calls:
                out["tool_calls"] = calls
            messages.append(out)
    tools = [{"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})}
             for t in _tool_list(req.get("tools"), None)] or None
    kwargs = {}
    # "thinking": {"type": "disabled" | "enabled", "budget_tokens"} and "output_config": {"effort"}
    thinking = req.get("thinking")
    effort = (req.get("output_config") or {}).get("effort") if isinstance(req.get("output_config"), dict) else None
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        kwargs["enable_thinking"] = False
    elif effort:
        kwargs.update(effort_kwargs(effort))
    elif isinstance(thinking, dict) and thinking.get("budget_tokens"):
        kwargs.update(budget_effort(thinking["budget_tokens"]))
    elif thinking is None and not (req.get("reasoning_budget_tokens") or req.get("thinking_budget_tokens")) and not think_unasked:
        # opt-in thinking: short helper calls that ask for none would otherwise spend their tokens thinking
        kwargs["enable_thinking"] = False
    return _late_system_to_user(messages), tools, kwargs


# ------------------------------------------------------------------------------------------------ output parser
@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:24])


@dataclass
class Event:
    kind: str                     # "reasoning" | "content" | "tool_call"
    text: str = ""
    call: ToolCall | None = None


THINK_END = "</think>"
CALL_START = "<tool_call>"
CALL_END = "</tool_call>"


PARAM_END = "</parameter>"
FUNC_END = "</function>"
PARAM_START = "<parameter="
FUNC_START = "<function="
_QUOTED_OPEN = re.compile(r"<function=[^<>\s]+>\s*(?:<parameter=|</function>)")


def param_end(text: str, final: bool = False, name: str | None = None) -> int:
    """Find a structural parameter close, keeping quoted calls and a reopened parameter inside its value.
    -1: none yet; -2: candidate awaiting more output. `final` falls back to the plain structural close."""
    at = text.find(PARAM_END)
    fallback = -1
    while at >= 0:
        after = text[at + len(PARAM_END):].lstrip()
        candidate = -1
        if after.startswith(PARAM_START):
            gt = after.find(">")
            if gt < 0:
                candidate = at if final else -2
            elif name is None or after[len(PARAM_START):gt].strip() != name:
                candidate = at
        elif after.startswith(FUNC_END):
            candidate = at
        elif not after or PARAM_START.startswith(after) or FUNC_END.startswith(after):
            candidate = at if final else -2
        if candidate != -1:
            if len(_QUOTED_OPEN.findall(text, 0, at)) <= text.count(FUNC_END, 0, at):
                return candidate
            if fallback < 0:
                fallback = at
        at = text.find(PARAM_END, at + 1)
    # An unclosed quoted function must not hide the enclosing call at end of output.
    return fallback if final else (-2 if fallback >= 0 else -1)


def call_end(text: str, final: bool = False) -> int:
    """Where a call body ends: the `</tool_call>` after its own `</function>` (found via param_end); -1: incomplete.
    A body not in the call format ends at the first `</tool_call>`."""
    s = text.lstrip()
    pos = len(text) - len(s)
    if not s.startswith("<function="):
        return text.find(CALL_END) if not "<function=".startswith(s) else -1
    gt = text.find(">", pos)
    if gt < 0:
        return -1
    pos = gt + 1
    while True:
        rest = text[pos:]
        s = rest.lstrip()
        pos += len(rest) - len(s)
        if s.startswith("<parameter="):
            gt = text.find(">", pos)
            if gt < 0:
                return -1
            end = param_end(text[gt + 1:], final=final, name=text[pos:gt].split("=", 1)[1].strip())
            if end < 0:
                return -1
            pos = gt + 1 + end + len(PARAM_END)
        elif s.startswith(FUNC_END):
            rest = text[pos + len(FUNC_END):]
            s = rest.lstrip()
            if s.startswith(CALL_END):
                return pos + len(FUNC_END) + len(rest) - len(s)
            return -1 if CALL_END.startswith(s) else text.find(CALL_END, pos)
        elif not s or "<parameter=".startswith(s) or FUNC_END.startswith(s):
            return -1
        else:
            return text.find(CALL_END, pos)


def tool_choice_of(tool_choice) -> tuple[str, str | None]:
    """A client's `tool_choice` in any API's shape -> ("auto" | "none" | "required" | "named", name).
    OpenAI: "auto" / "none" / "required", {"type": "function", "function": {"name": N}} or the flat
    {"type": "function", "name": N} (Responses, with an optional "namespace"); Anthropic: {"type": "auto" | "any" |
    "none"} and {"type": "tool", "name": N}.  A value it does not know is a ValueError."""
    if tool_choice is None:
        return "auto", None
    if isinstance(tool_choice, str):
        if tool_choice in ("auto", "none", "required"):
            return tool_choice, None
    elif isinstance(tool_choice, dict):
        kind = tool_choice.get("type")
        if kind in ("auto", "none"):
            return kind, None
        if kind in ("any", "required"):
            return "required", None
        if kind in ("function", "tool", "custom"):
            fn = tool_choice.get("function")
            name = (fn.get("name") if isinstance(fn, dict) else None) or tool_choice.get("name")
            if isinstance(name, str) and name:
                space = tool_choice.get("namespace")
                return "named", f"{space}.{name}" if isinstance(space, str) and space else name
            raise ValueError("tool_choice: a named choice needs the tool's name")
    raise ValueError(f"tool_choice: unsupported value {json.dumps(tool_choice)[:120]}; use auto, none, required "
                     "(Anthropic: any) or a named tool")


def forced_call(tool_choice, tools: list[dict] | None, opening=None) -> tuple[list[dict] | None, str | None]:
    """`tool_choice` -> (the tools to offer, the text that opens the call the reply must make or None).
    "none" offers no tools.  There is no grammar: the server writes the opening itself, so the model can only go on
    with a call.  "required" / Anthropic "any": any tool (the only one is named); a named tool: that one.  A forced
    choice with no tools, or naming a tool the request lacks, is a ValueError."""
    kind, name = tool_choice_of(tool_choice)
    if kind == "none":
        return None, None
    if kind in ("auto", "none"):
        return tools, None
    names = [t.get("name") for t in tools or []]
    if kind == "required":
        if not names:
            raise ValueError("tool_choice: required needs at least one tool in the request")
        if len(names) == 1:
            name = names[0]
    elif name not in names:
        raise ValueError(f"tool_choice: the request has no tool named {name!r}")
    if opening is not None:
        return tools, opening(name)
    return tools, CALL_START + (f"\n<function={name}>\n" if name else "\n<function=")


def _text_kinds(schema) -> tuple[bool, bool]:
    """(text, null) for a parameter schema: whether the grammar lets the model write it as raw text (type "string",
    a type list naming "string", or an anyOf/oneOf/one-branch allOf of plain strings and null), and whether null is
    one of its types."""
    if not isinstance(schema, dict):
        return False, False
    t = schema.get("type")
    if isinstance(t, str):
        return t == "string", t == "null"
    if isinstance(t, list):
        return "string" in t, "null" in t
    if t is not None or any(k in schema for k in ("enum", "const", "properties", "items")):
        return False, False
    union = [schema[k] for k in ("anyOf", "oneOf", "allOf") if isinstance(schema.get(k), list)]
    if len(union) != 1 or not union[0] or ("allOf" in schema and len(union[0]) != 1):
        return False, False
    text = null = False
    for b in union[0]:
        bt = b.get("type") if isinstance(b, dict) else None
        types = bt if isinstance(bt, list) else [bt]
        if (not types or any(x not in ("string", "null") for x in types)
                or any(k in b for k in ("enum", "const", "anyOf", "oneOf", "allOf", "properties", "items", "$ref"))):
            return False, False
        text = text or "string" in types
        null = null or "null" in types
    return text, null


def _decode_param(value: str, schema) -> object:
    """A parameter's raw text as its declared value: kept as text when the schema allows a string (`null` is None only
    where null is a type), else JSON-decoded when it parses (text when it does not)."""
    text, null = _text_kinds(schema)
    if text:
        return None if null and value.strip() == "null" else value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _deref(root: dict, schema):
    """Follows local $ref links ("#/a/b") to the schema they name; None if one does not resolve."""
    for _ in range(32):
        ref = schema.get("$ref") if isinstance(schema, dict) else None
        if not isinstance(ref, str):
            return schema
        if not ref.startswith("#/"):
            return None
        cur = root
        for part in ref[2:].split("/"):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                return None
        schema = cur
    return schema


def _union_variants(root: dict, schema, out: list, depth: int = 0) -> None:
    """The object variants of a oneOf/anyOf union, nested unions flattened."""
    schema = _deref(root, schema)
    if not isinstance(schema, dict) or depth > 8:
        return
    unions = [schema[k] for k in ("oneOf", "anyOf") if isinstance(schema.get(k), list)]
    for alts in unions:
        for alt in alts:
            _union_variants(root, alt, out, depth + 1)
    if not unions and isinstance(schema.get("properties"), dict):
        out.append(schema)


def _tool_properties(tool: dict | None) -> dict:
    """A tool's parameter schemas by name. Parameters sent as a union ({"properties": {}, "oneOf": [...]}) take the
    properties of every variant, string enums united, as the tool-call grammar does."""
    params = (tool or {}).get("parameters") or {}
    props = params.get("properties") if isinstance(params, dict) else None
    if props or not isinstance(params, dict):
        return props or {}
    variants = []
    for k in ("oneOf", "anyOf"):
        for alt in params.get(k) if isinstance(params.get(k), list) else []:
            _union_variants(params, alt, variants)
    merged = {}
    for v in variants:
        for name, prop in v["properties"].items():
            value = _deref(params, prop)
            value = value if value is not None else {}
            cur = merged.get(name)
            if cur is None:
                merged[name] = value
            elif cur == value:
                continue
            elif all(isinstance(s, dict) and isinstance(s.get("enum"), list)
                     and all(isinstance(e, str) for e in s["enum"]) for s in (cur, value)):
                merged[name] = dict(cur, enum=cur["enum"] + [e for e in value["enum"] if e not in cur["enum"]])
            elif isinstance(cur, dict) and list(cur) == ["anyOf"]:
                merged[name] = {"anyOf": cur["anyOf"] + ([value] if value not in cur["anyOf"] else [])}
            else:
                merged[name] = {"anyOf": [cur, value]}
    return merged


def parse_tool_call(body: str, schema: dict | None = None) -> ToolCall:
    """`<function=NAME>...</function>` -> ToolCall. Values are JSON-decoded when the schema says non-string
    (without a schema: when they parse as JSON objects, arrays, numbers or booleans)."""
    body = body.strip()
    if not body.startswith("<function=") or ">" not in body:
        raise ValueError("malformed tool call: " + body[:80])
    name = body[len("<function="):body.index(">")]
    rest = body[body.index(">") + 1:]
    props = _tool_properties(schema)
    args = {}
    while "<parameter=" in rest:
        rest = rest[rest.index("<parameter=") + len("<parameter="):]
        pname = rest[:rest.index(">")]
        rest = rest[rest.index(">") + 1:]
        end = param_end(rest, final=True, name=pname.strip())
        value = rest[:end] if end >= 0 else rest
        rest = rest[end + len(PARAM_END):] if end >= 0 else ""
        if value.startswith("\n"):
            value = value[1:]
        if value.endswith("\n"):
            value = value[:-1]
        args[pname] = _decode_param(value, props.get(pname))
    return ToolCall(name=name, arguments=args)


class OutputParser:
    """Incremental parser of model text: feed deltas, get events; a split tag is held until complete."""

    def __init__(self, thinking: bool = True, tools: list[dict] | None = None, stream_tools: bool = False):
        self.state = "reasoning" if thinking else "content"
        self.buf = ""
        self.lead = False
        self.rtail = ""              # last reasoning character sent
        self.schemas = {t.get("name"): t for t in tools or []}
        # stream_tools: also report a call while it is written ("tool_start", "tool_args" pieces) before "tool_call"
        self.stream_tools = stream_tools
        self.fence, self.line, self.ticks = "", "", 0     # code fence, current line and backticks of the answer so far
        self._reset_scan()

    def _track(self, text: str) -> str:
        """Follow the answer text that has gone out: the open code fence (``` or ~~~), the current line, and the
        backticks of the current paragraph. Returns the text."""
        parts = text.split("\n")
        for k, part in enumerate(parts):
            if k < len(parts) - 1:
                line, self.line = self.line + part, ""
                s = line.lstrip()
                if self.fence:
                    if s.startswith(self.fence * 3):
                        self.fence = ""
                elif s[:3] in ("```", "~~~") and (s[0] * 3) not in s[3:]:
                    self.fence = s[0]
                elif not s:
                    self.ticks = 0
                else:
                    self.ticks += line.count("`")
            else:
                self.line += part
        return text

    def _in_code(self) -> bool:
        """The text so far leaves the next character inside a code fence or inline code."""
        return bool(self.fence) or (self.ticks + self.line.count("`")) % 2 == 1

    def _reset_scan(self):
        self.sp = 0                  # consumed part of self.buf (the call body)
        self.ss = "name"             # name -> between -> str|raw -> ... -> done
        self.scall = None            # the ToolCall being streamed
        self.sfirst = True
        self.sval_started = False
        self.sdeclared = {}
        self.spname = ""
        self.svstart = 0

    def _scan(self, final: bool = False) -> list[Event]:
        """Advance the streaming view of the call body."""
        out = []

        def args(s):
            if s:
                out.append(Event("tool_args", s, call=self.scall))
        while True:
            rest = self.buf[self.sp:]
            if self.ss == "name":
                a = rest.find("<function=")
                b = rest.find(">", a + 10) if a >= 0 else -1
                if b < 0:
                    return out
                name = rest[a + 10:b]
                self.scall = ToolCall(name=name, arguments={})
                self.sdeclared = _tool_properties(self.schemas.get(name))
                out.append(Event("tool_start", call=self.scall))
                args("{")
                self.sp += b + 1
                self.ss = "between"
            elif self.ss == "between":
                stripped = rest.lstrip()
                self.sp += len(rest) - len(stripped)
                if stripped.startswith("<parameter="):
                    b = stripped.find(">")
                    if b < 0:
                        return out
                    pname = stripped[11:b]
                    args(("" if self.sfirst else ",") + json.dumps(pname) + ":")
                    self.sfirst = False
                    self.sp += b + 1
                    self.spname = pname
                    text, null = _text_kinds(self.sdeclared.get(pname))
                    if text and not null:
                        args('"')
                        self.ss, self.sval_started = "str", False
                    else:
                        self.ss = "raw"
                elif stripped.startswith("</function>"):
                    args("}")
                    self.sp += len("</function>")
                    self.ss = "done"
                else:
                    return out          # a tag still arriving: wait
            elif self.ss == "str":
                if not self.sval_started:
                    if not rest:
                        return out
                    if rest[0] == "\n":          # the leading newline is not part of the value
                        self.sp += 1
                        rest = rest[1:]
                    self.sval_started = True
                    self.svstart = self.sp
                end = param_end(self.buf[self.svstart:], final=final, name=self.spname) if PARAM_END in rest else -1
                end = end - (self.sp - self.svstart) if end >= 0 else end
                if end >= 0:
                    value = rest[:end]
                    if value.endswith("\n"):
                        value = value[:-1]
                    args(json.dumps(value)[1:-1] + '"')
                    self.sp += end + len(PARAM_END)
                    self.ss = "between"
                    continue
                # an undecided </parameter> is held from its start
                safe = rest.find(PARAM_END) if end == -2 else len(rest) - self._hold(rest, (PARAM_END,))
                if safe > 0 and rest[safe - 1] == "\n":   # maybe the newline before </parameter>
                    safe -= 1
                if safe > 0:
                    args(json.dumps(rest[:safe])[1:-1])
                    self.sp += safe
                return out
            elif self.ss == "raw":
                end = param_end(rest, final=final, name=self.spname)
                if end < 0:
                    return out
                value = rest[:end]
                if value.startswith("\n"):
                    value = value[1:]
                if value.endswith("\n"):
                    value = value[:-1]
                args(json.dumps(_decode_param(value, self.sdeclared.get(self.spname)), ensure_ascii=False))
                self.sp += end + len(PARAM_END)
                self.ss = "between"
            else:
                return out

    def _close_scan(self) -> list[Event]:
        """Close the JSON of a streamed call that ended without a clean </function>."""
        out = []
        if self.scall is None or self.ss == "done":
            return out
        tail = ""
        if self.ss == "str":
            tail += '"'
        elif self.ss == "raw":
            tail += json.dumps(self.buf[self.sp:].strip("\n"))
        tail += "}"
        out.append(Event("tool_args", tail, call=self.scall))
        self.ss = "done"
        return out

    def _reasoning(self, text: str) -> Event:
        """A reasoning event for text going out; remembers its last character for the quote check."""
        self.rtail = text[-1:] or self.rtail
        return Event("reasoning", text)

    def _quoted_think_end(self, i: int) -> bool | None:
        """Whether the `</think>` at self.buf[i] is the model quoting the tag while it thinks: written right after a
        quote or backtick and followed by anything but a line break. None while the character after it has not
        arrived."""
        before = self.buf[i - 1] if i else self.rtail
        if before not in ('"', "'", "`"):
            return False
        after = self.buf[i + len(THINK_END):]
        if not after:
            return None
        return after[0] not in "\r\n"

    def _hold(self, text: str, tags: tuple[str, ...]) -> int:
        """Length of the longest suffix of `text` that is a proper prefix of one of `tags`."""
        best = 0
        for tag in tags:
            for n in range(1, len(tag)):
                if text.endswith(tag[:n]):
                    best = max(best, n)
        return best

    def feed(self, delta: str, *, final: bool = False) -> list[Event]:
        self.buf += delta
        out: list[Event] = []
        while True:
            if self.state == "reasoning":
                i = self.buf.find(THINK_END)
                if i < 0:
                    keep = self._hold(self.buf, (THINK_END,))
                    if len(self.buf) > keep:
                        out.append(self._reasoning(self.buf[:len(self.buf) - keep]))
                        self.buf = self.buf[len(self.buf) - keep:]
                    return out
                quoted = self._quoted_think_end(i)
                if quoted is None:                   # held like a partial tag until the character after it is here
                    if i:
                        out.append(self._reasoning(self.buf[:i]))
                        self.buf = self.buf[i:]
                    return out
                if quoted:                           # a quoted tag is reasoning text
                    out.append(self._reasoning(self.buf[:i + len(THINK_END)]))
                    self.buf = self.buf[i + len(THINK_END):]
                    continue
                if i:
                    out.append(self._reasoning(self.buf[:i]))
                self.buf = self.buf[i + len(THINK_END):]
                self.state, self.lead = "content", True
                self.fence, self.line, self.ticks = "", "", 0     # the answer's own text starts here
            elif self.state == "content":
                if self.lead:                                   # newlines after </think> or a call
                    stripped = self.buf.lstrip("\n")
                    if not stripped:
                        self.buf = ""
                        return out
                    self.buf, self.lead = stripped, False
                i = self.buf.find(CALL_START)
                if i < 0:
                    # hold a partial tag and the newlines before it: a following tool call drops them
                    j = len(self.buf) - self._hold(self.buf, (CALL_START,))
                    while j > 0 and self.buf[j - 1] == "\n":
                        j -= 1
                    if j > 0:
                        out.append(Event("content", self._track(self.buf[:j])))
                        self.buf = self.buf[j:]
                    return out
                # an opener inside a code fence or inline code is a quoted example: text, decided by the text before it
                snap = (self.fence, self.line, self.ticks)
                self._track(self.buf[:i])
                in_code = self._in_code()
                self.fence, self.line, self.ticks = snap
                if in_code:
                    out.append(Event("content", self._track(self.buf[:i + len(CALL_START)])))
                    self.buf = self.buf[i + len(CALL_START):]
                    continue
                if i and self.buf[:i].strip():
                    out.append(Event("content", self._track(self.buf[:i].rstrip("\n"))))
                self.buf = self.buf[i + len(CALL_START):]
                self.state = "call"
            else:
                i = call_end(self.buf, final=final)
                if self.stream_tools:
                    if i >= 0:
                        whole, self.buf = self.buf, self.buf[:i]     # scan only the body
                        out += self._scan(final=final)
                        out += self._close_scan()
                        self.buf = whole
                    else:
                        out += self._scan(final=final)
                if i < 0:
                    return out
                body = self.buf[:i]
                self.buf = self.buf[i + len(CALL_END):]
                name = body.strip()[len("<function="):].split(">", 1)[0]
                call = parse_tool_call(body, self.schemas.get(name))
                if self.scall is not None:
                    call.id = self.scall.id
                out.append(Event("tool_call", call=call))
                self._reset_scan()
                self.state, self.lead = "content", True

    def finish(self) -> list[Event]:
        """End of generation: flush what is held (an unterminated call becomes content; an announced one is left
        unfinished with no "tool_call")."""
        out = self.feed("", final=True)
        if self.state == "call" and self.stream_tools and self.scall is not None:
            if self.ss == "done":               # only </tool_call> is missing
                out.append(Event("tool_call", call=self.scall))
            self.buf = ""
            self._reset_scan()
            return out
        if self.state == "reasoning" and self.buf == THINK_END:     # the output ends on a held tag: the thinking's end
            self.buf, self.state = "", "content"
        if self.buf:
            kind = {"reasoning": "reasoning", "content": "content"}.get(self.state, "content")
            text = self.buf if self.state != "call" else CALL_START + self.buf
            out.append(Event(kind, text))
            self.buf = ""
        return out

# ===== file: serve/handler.py =====
"""The HTTP server and its request handler: routes, authentication, CORS and streaming."""
from __future__ import annotations

import json
import os
import select
import socket
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from serve.api import responses as responses_api
from serve.api.responses import error_body as responses_error_body
from serve import ROOT
from serve.api.anthropic import anthropic_collect, anthropic_events
from serve.engine.errors import ClientGone, EngineDied, EngineError, EngineStarting, EngineStuck, GpuBusy, ModelBusy
from serve.api.grammar import REQUEST_KEY as GRAMMAR_KEY
from serve.api.logit_bias import REQUEST_KEY as BIAS_KEY, checked as checked_bias
from serve.config import checked_sampling
from serve.frontend import anthropic_to_messages, forced_call, openai_to_messages
from serve.api.openai import openai_chunks, openai_collect
from serve.api.responses import ResponsesError
from serve.prompt import primed
from serve.security import host_allowed, host_names_for, key_matches, origin_allowed, peer_of, remote_reason
from serve.router import Router
from serve.service import LOG_FIRST_LINES, LOG_MAX_LINES, QUEUE_BEAT_S, Service, _debug_req
from serve.api.structured import anthropic_format, check_format
from serve.util.text import stop_strings


CONTROL_BODY_MAX = 64 << 10                 # the unused body of /load and /unload
TURN_POLL_S = 0.5                           # how soon a request waiting for its turn notices its client left


class BadBody(Exception):
    """A request body that cannot be read (malformed or oversized): the status and the sentence."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _size_text(n: int) -> str:
    return f"{n >> 20} MiB" if n >= 1 << 20 else f"{n >> 10} KiB"


def make_handler(target: Service | Router):
    router = target if isinstance(target, Router) else Router([target])
    svc = router.front                                      # the settings every model shares

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"                       # SSE ends by closing the connection

        watch_done = None                                   # stops this request's disconnect watcher
        cancel = None                                       # set when the client left
        turn = None                                         # this request's router turn
        sse_open = False                                    # the stream's headers went out

        def log_message(self, fmt, *args):
            pass

        def parse_request(self):
            """Without an API key, check the Host header (DNS-rebinding protection); a key makes the check moot."""
            if not super().parse_request():
                return False
            host = self.headers.get("Host")
            if svc.api_key or host_allowed(host, svc.host_names, "*" in svc.allowed_hosts):
                return True
            print(f"[server] error: refused Host {host!r} from {self.client_address[0]} "
                  f"(--api-key KEY, or --allowed-host)",
                  flush=True)
            self._json(403, {"error": {"type": "forbidden", "message":
                             f"Host {host!r} is not allowed (DNS rebinding protection). Reaching the server under this "
                             f"name on purpose? Start the server with --allowed-host NAME, "
                             f"or with --api-key KEY, which turns this check off"}})
            return False

        def _chunked(self) -> bool:
            """The body is sent as Transfer-Encoding: chunked (it wins over a Content-Length, RFC 9112)."""
            te = self.headers.get("Transfer-Encoding", "") or ""
            return te.split(",")[-1].strip().lower() == "chunked"

        def _read_chunked(self, limit: int, keep: bool = True) -> bytes:
            """Decode a chunked body: at most `limit` bytes (413 beyond it; the size is checked before a chunk is
            read, so a huge announced chunk is never allocated), 400 for a malformed one. keep=False drops it."""
            parts, total = [], 0
            while True:
                line = self.rfile.readline(1025)
                if not line.endswith(b"\n"):
                    raise BadBody(400, "malformed chunked request body")
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise BadBody(400, "malformed chunk size in the request body") from None
                if size < 0:
                    raise BadBody(400, "malformed chunk size in the request body")
                if size == 0:
                    for _ in range(64):                       # the trailers, up to the blank line
                        if self.rfile.readline(8193).strip() == b"":
                            break
                    return b"".join(parts)
                total += size
                if total > limit:
                    self.close_connection = True
                    raise BadBody(413, f"the request body is larger than {_size_text(limit)}")
                left = size
                while left > 0:
                    piece = self.rfile.read(min(left, 1 << 20))
                    if not piece:
                        raise BadBody(400, "the chunked request body ended early")
                    left -= len(piece)
                    if keep:
                        parts.append(piece)
                if self.rfile.read(2) != b"\r\n":
                    raise BadBody(400, "malformed chunked request body")

        def _body(self) -> bytes:
            """The request body, chunked or by Content-Length, at most svc.max_payload bytes."""
            limit = svc.max_payload
            if self._chunked():
                return self._read_chunked(limit)
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                raise BadBody(400, "invalid Content-Length") from None
            if length < 0:
                raise BadBody(400, "invalid Content-Length")
            if length > limit:
                self.close_connection = True
                raise BadBody(413, f"the request body is larger than {_size_text(limit)}")
            return self.rfile.read(length)

        def _foreign_page(self) -> bool:
            """Without an API key, refuse a /v1 POST from another site's page: its Origin must be allowed (no Origin
            is fine) and, with one, the body must be JSON."""
            origin = self.headers.get("Origin")
            if svc.api_key or not origin:
                return False
            if not origin_allowed(origin, self.headers.get("Host"), svc.host_names,
                                  [*svc.trusted_origins, *svc.cors_origins]):
                print(f"[server] error: refused a request from web page {origin!r} "
                      f"(--api-key KEY, --allowed-host, --cors-origin)", flush=True)
                self._json(403, {"error": {"type": "forbidden", "message":
                                 f"web pages of {origin} may not use this server without an API key; start the server with --api-key KEY, "
                                 f"or add the page's host with --allowed-host (or its origin with --cors-origin)"}})
                return True
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": {"message": "send application/json"}})
                return True
            return False

        def _image_access(self):
            """What this request may name as an image, from its own connection."""
            why = "remote-only" if svc.image_sources.remote_only else remote_reason(self.client_address, self.headers)
            return replace(svc.image_sources, local=not why, why=why, peer=peer_of(self.client_address))

        def _client_cancel(self) -> threading.Event:
            """The event set when this request's client hangs up, watched from the first call."""
            if self.cancel is None:
                self.cancel = threading.Event()
                self._watch_client(self.cancel)
            return self.cancel

        def _take_turn(self, stream: bool = False) -> None:
            """Wait for this request's model to be loaded and its turn to come, which may take a swap. A stream gets
            its headers and keep-alives while it waits; a client that leaves gives its place up (ClientGone)."""
            self.turn = router.join(self.svc)
            if router.wait(self.turn, 0):
                return
            cancel, beat = self._client_cancel(), time.monotonic()
            while not router.wait(self.turn, TURN_POLL_S):
                if cancel.is_set():
                    raise ClientGone
                if stream and time.monotonic() - beat >= QUEUE_BEAT_S:
                    beat = time.monotonic()
                    try:
                        self._sse()
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    except OSError:
                        raise ClientGone from None

        def _watch_client(self, cancel: threading.Event) -> None:
            """Cancel the request when its client hangs up, polling every 0.5 s: a readable socket with nothing to
            read is EOF (the request is fully read, so data is not a hang-up)."""
            done = self.watch_done = threading.Event()
            sock = self.connection

            def watch():
                while not done.wait(0.5) and not cancel.is_set():
                    try:
                        readable, _, _ = select.select([sock], [], [], 0)
                        gone = bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""
                    except (ConnectionError, TimeoutError):
                        gone = True
                    except (OSError, ValueError):            # socket closed: the request has ended
                        return
                    if gone:
                        cancel.set()
                        return

            threading.Thread(target=watch, daemon=True, name="client-watch").start()

        def _cors(self):
            """CORS headers, only for /v1/* and a listed origin."""
            if not svc.cors_origins or not self.path.split("?")[0].startswith("/v1/"):
                return
            origin = (self.headers.get("Origin") or "").rstrip("/")
            if "*" in svc.cors_origins:
                allow = "*"
            elif origin and origin in svc.cors_origins:
                allow = origin
                self.send_header("Vary", "Origin")
            else:
                return
            self.send_header("Access-Control-Allow-Origin", allow)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            # echo the requested headers: "*" does not cover Authorization
            asked = self.headers.get("Access-Control-Request-Headers")
            self.send_header("Access-Control-Allow-Headers",
                             asked or "Authorization, Content-Type, x-api-key, anthropic-version, anthropic-beta")
            self.send_header("Access-Control-Max-Age", "600")

        def do_OPTIONS(self):
            # preflights carry no credentials, so no API key check
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _stream_error(self, obj) -> None:
            """An error after the stream's headers went out (a request that waited for a swap), in the API's own form."""
            path = self.path.split("?")[0].rstrip("/")
            err = obj.get("error") if isinstance(obj.get("error"), dict) else {"message": str(obj)}
            if path == "/v1/messages":
                data = b"event: error\ndata: " + json.dumps({"type": "error", "error": err}, ensure_ascii=False).encode() + b"\n\n"
            elif path == "/v1/responses":
                data = b"event: error\ndata: " + json.dumps({"type": "error", "code": err.get("code"), "message": err.get("message")},
                                                            ensure_ascii=False).encode() + b"\n\n"
            else:
                data = b"data: " + json.dumps({"error": err}, ensure_ascii=False).encode() + b"\n\ndata: [DONE]\n\n"
            self.wfile.write(data)

        def _json(self, code, obj):
            if self.sse_open:
                return self._stream_error(obj)
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not svc.api_key:
                return True
            auth = self.headers.get("Authorization", "")
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else self.headers.get("x-api-key", "")
            if key_matches(given, svc.api_key):
                return True
            self._json(401, {"error": {"type": "authentication_error", "message": "missing or wrong API key"}})
            return False

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path.startswith("/fonts/"):
                name = path[len("/fonts/"):]
                f = ROOT / "serve" / "web" / "fonts" / name
                if "/" in name or "\\" in name or not name.endswith(".woff2") or not f.is_file():
                    self._json(404, {"error": {"message": "not found"}})
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/web/"):
                name = path[len("/web/"):]
                types = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                         ".svg": "image/svg+xml"}
                f = ROOT / "serve" / "web" / name
                ext = os.path.splitext(name)[1]
                if "/" in name or "\\" in name or ext not in types or not f.is_file():
                    self._json(404, {"error": {"message": "not found"}})
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", types[ext])
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/metrics":
                if self._authorized():
                    name = parse_qs(urlsplit(self.path).query).get("model", [None])[0]
                    shown = router.shown() if name is None else router.service_for(name)
                    if shown is None:
                        self._json(404, {"error": {"message": "model not found"}})
                    else:
                        self._json(200, router.metrics(shown))
            elif path == "/api/logs":
                if self._authorized():
                    self._logs()
            elif path == "":
                body = (ROOT / "serve" / "web" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path in ("/health", "/api/health"):
                shown = router.shown()
                load = shown.load_progress()
                self._json(200, {"status": "ok", "max_context": shown.engine.max_context, "model": shown.model,
                                 "images": shown.vision is not None, "api_key": bool(svc.api_key),
                                 "loaded": shown.loaded(), "loading": shown.loading, "service": "basalt",
                                 "percent": load.get("percent") if load else None, "swap": router.swap_status()})
            elif path == "/status":
                if not self._authorized():                  # it shows the last answer's tail
                    return
                shown = router.shown()
                with shown.status_lock:
                    s = dict(shown.status)
                now = time.time()
                if s.get("busy"):
                    s["elapsed_s"] = round(now - s["started"], 1)
                    if s.get("first_token"):
                        s["tokens_per_s"] = round(shown._tok_s(), 1)
                        s["tokens_per_s_mean"] = round(shown._tok_s_mean(), 1)
                for k in ("started", "first_token"):
                    s.pop(k, None)
                self._json(200, s)
            elif path in ("/v1/models", "/models"):
                if self._authorized():
                    self._json(200, {"object": "list", "data": router.models()})
            elif path == "/props":
                if self._authorized():
                    self._props()
            elif path == "/slots":
                if self._authorized():
                    shown = router.shown()
                    loaded = not hasattr(shown.engine, "alive") or shown.engine.alive()
                    self._json(200, shown.slot_table()[0] if loaded else [])
            elif path == "/v1/status":
                if self._authorized():
                    self._json(200, {**router.shown().v1_status(), "models": router.models(), "swap": router.swap_status()})
            else:
                self._json(404, {"error": {"message": "not found"}})

        def do_POST(self):
            if not self._authorized():
                return
            path = self.path.split("?")[0].rstrip("/")   # clients post /v1/messages?beta=true
            if path.startswith("/v1/") and self._foreign_page():
                return
            if path in ("/unload", "/load") and not self._control_body():
                return
            # own page and JSON only: a form POST from any site must not control the model
            if path in ("/unload", "/load") and not self._own_page("the model can be loaded or unloaded"):
                return
            if path == "/unload":                            # unload between requests
                try:
                    r = router.shown().unload()
                except EngineStuck as e:
                    self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                    return
                self._json(409 if r == "busy" else 200, {"status": r})
                return
            if path == "/load":                              # load ahead of a request
                self.svc = router.shown()
                try:
                    self._take_turn()
                    self.svc.load()
                    self._json(200, {"status": "loaded"})
                except (GpuBusy, EngineDied) as e:
                    self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                except ClientGone:
                    pass
                finally:
                    self._end_request()
                return
            try:
                req = json.loads(self._body() or b"{}")
                if not isinstance(req, dict):
                    raise ValueError("send a JSON object")
                if path.startswith("/v1/responses/"):        # nothing is stored
                    self._json(404, responses_error_body("this server keeps no responses (stateless); send the "
                                                         "whole conversation to POST /v1/responses", code="not_found"))
                    return
                if path in ("/v1/load", "/v1/unload"):
                    if not self._own_page("the model can be loaded or unloaded"):
                        return
                    target = router.shown() if req.get("model") is None else router.service_for(req["model"])
                    if target is None:
                        self._json(404, {"error": {"message": "model not found"}})
                        return
                    self.svc = target
                    if path == "/v1/unload":
                        result = target.unload()
                        if result == "busy":
                            raise ModelBusy("a request is running or queued")
                    elif target.loading:                 # a load in progress: wait for it like any request
                        self._take_turn()
                        target.load()
                        result = "loaded"
                    else:
                        self._take_turn()                # another model's turn: this load waits for it
                        if not target.fifo.acquire(blocking=False):
                            raise ModelBusy("a request is running or queued")
                        try:
                            with target.status_lock:
                                if target.status.get("busy") or target.status.get("queued"):
                                    raise ModelBusy("a request is running or queued")
                            target.ensure_loaded()
                            target.last_request_at = time.time()
                        finally:
                            target.fifo.release()
                        result = "loaded"
                    self._json(200, {"status": result, **target.v1_status()})
                    return
                if path == "/v1/vram":                       # resize the VRAM reserve
                    if not self._own_page("the VRAM reserve can be changed"):
                        return
                    r = req.get("reserve_mib")
                    if r is not None and (isinstance(r, bool) or not isinstance(r, int) or r < 0):
                        raise ValueError("reserve_mib: a whole number of MiB (0 or more), or null for the start's")
                    try:
                        self._json(200, router.vram(r))
                    except EngineDied as e:
                        self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                    return
                requested = req.get("model")
                self.svc = router.service_for(requested)
                if self.svc is None:
                    self._unknown_model(path, requested)
                    return
                self.svc.request_ctx.path = path
                self.svc.request_ctx.model = self.svc.model  # always the served name, never the requested one
                if path == "/v1/responses":
                    self._responses(req)
                elif path == "/v1/chat/completions":
                    self._openai(req)
                elif path == "/v1/messages":
                    self._anthropic(req)
                elif path == "/v1/messages/count_tokens":
                    self._count_tokens(req)
                else:
                    self._json(404, {"error": {"message": "not found"}})
            except EngineError as e:                         # the engine failed: not the request's fault
                if path == "/v1/responses":
                    self._json(500, responses_error_body(str(e), "server_error", code="server_error"))
                else:
                    self._json(500, {"error": {"type": "server_error", "message": str(e)}})
            except BadBody as e:
                if path == "/v1/responses":
                    self._json(e.status, responses_error_body(str(e)))
                else:
                    self._json(e.status, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ValueError as e:
                if path == "/v1/responses":
                    self._json(400, responses_error_body(str(e)))
                else:
                    self._json(400, {"error": {"type": "invalid_request_error", "message": str(e)}})
            except ModelBusy as e:
                self._json(409, {"error": {"type": "model_busy", "message": str(e)}})
            except (GpuBusy, EngineStarting) as e:
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})
            except EngineDied as e:                          # before the answer started
                self._json(503, {"error": {"type": "server_error", "message": f"{e}; the next request restarts it"}})
            except EngineStuck as e:                         # the engine could not be ended
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})
            except ClientGone:                               # it left while queued
                pass
            finally:
                self._end_request()

        def _end_request(self) -> None:
            if self.turn is not None:
                router.leave(self.turn)
                self.turn = None
            if self.watch_done is not None:
                self.watch_done.set()

        def _props(self):
            name = parse_qs(urlsplit(self.path).query).get("model", [None])[0]
            svc = router.shown() if name is None else router.service_for(name)
            if svc is None:
                self._json(404, {"error": {"message": "model not found"}})
                return
            try:
                if not router.multi:
                    svc.wait_load()                          # a load in progress is waited for, not refused
            except (GpuBusy, EngineDied) as e:
                self._json(503, {"error": {"type": "server_error", "message": str(e)}})
                return
            if not svc.loaded() and not getattr(svc.engine, "unloaded", False):
                self._json(503, {"error": {"message": "the engine is not running"}})
                return
            names = {"repetition_penalty": "repeat_penalty", "penalty_last_n": "repeat_last_n"}
            params = {names.get(k, k): v for k, v in svc.sampling_defaults.items()
                      if k in ("temperature", "top_p", "top_k", "min_p", "seed", "repetition_penalty",
                               "presence_penalty", "frequency_penalty", "penalty_last_n")}
            params["n_predict"] = -1
            props = {"default_generation_settings": {"n_ctx": svc.engine.max_context, "params": params},
                     "total_slots": svc.slot_stats()["total"], "model_alias": svc.model, "chat_template": svc.template.source,
                     "modalities": {"vision": svc.vision is not None}, "models_autoload": hasattr(svc.engine, "restart"),
                     "is_sleeping": not svc.loaded()}
            if getattr(svc.engine, "model_path", None):
                props["model_path"] = svc.engine.model_path
            version = getattr(svc.engine, "info", {}).get("version")
            if version:
                props["build_info"] = "basalt " + str(version)
            self._json(200, props)

        def _control_body(self) -> bool:
            """Read the unused body before replying so the client sees no TCP reset."""
            chunked = self._chunked()
            length = 0
            if not chunked:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._json(400, {"error": {"message": "invalid Content-Length"}})
                    return False
                if not 0 <= length <= CONTROL_BODY_MAX:
                    self._json(413, {"error": {"message": "control request body is limited to 64 KiB"}})
                    return False
            timeout = self.connection.gettimeout()
            try:
                self.connection.settimeout(2.0)
                if chunked:
                    self._read_chunked(CONTROL_BODY_MAX, keep=False)
                    complete = True
                else:
                    complete = len(self.rfile.read(length)) == length
            except BadBody as e:
                self._json(e.status, {"error": {"message": f"control request: {e}"}})
                return False
            except OSError:
                complete = False
            finally:
                self.connection.settimeout(timeout)
            if not complete:
                self._json(400, {"error": {"message": "incomplete control request body"}})
            return complete

        def _own_page(self, what) -> bool:
            """Require a JSON body and no foreign Origin, so another site's page cannot control the model."""
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": {"message": "send application/json"}})
                return False
            # Origin must be this server's address or a trusted origin; proxy-added headers prove nothing
            origin = (self.headers.get("Origin") or "").rstrip("/")
            if origin and origin.split("://", 1)[-1] != self.headers.get("Host", "") and \
                    origin not in svc.trusted_origins:
                self._json(403, {"error": {"message": f"{what} only from the server's own page"}})
                return False
            return True

        def _logs(self):
            """GET /api/logs?after=<id>&limit=<n>: the newest lines after that id, and the newest id."""
            query = parse_qs(urlsplit(self.path).query)

            def number(name, default):
                try:
                    return max(0, int(query.get(name, [default])[0]))
                except ValueError:
                    return default
            after = number("after", None) if "after" in query else None
            limit = min(number("limit", LOG_FIRST_LINES), LOG_MAX_LINES) if after is None else \
                min(number("limit", LOG_MAX_LINES), LOG_MAX_LINES)
            lines, last = svc.logs.since(after or 0, limit)
            self._json(200, {"lines": lines, "last": last})

        def _sse(self):
            if self.sse_open:
                return
            self.sse_open = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")   # proxies pass each event at once
            self._cors()
            self.end_headers()

        def _unknown_model(self, path, name):
            """404 for a missing or unserved model, in the shape of the API called."""
            anthropic = path.startswith("/v1/messages")
            if name is None:
                text = "model: required" if anthropic else "model is required"
            else:
                text = f"model: {name}" if anthropic else f"The model '{name}' does not exist"
            if anthropic:
                self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": text}})
            else:
                self._json(404, responses_error_body(text, "invalid_request_error", "model", "model_not_found"))

        def _openai(self, req):
            messages, tools, kw = openai_to_messages(req)
            tools, force = forced_call(req.get("tool_choice"), tools, self.svc.dialect.forced_opening)   # a bad choice is a 400 before streaming
            check_format(req.get("response_format"))
            self.svc.require_grammar(req.get("response_format"))
            checked_sampling(req)                             # a sampling value out of range is a 400 likewise
            self._take_turn(bool(req.get("stream")))
            self.svc.load()
            req[BIAS_KEY] = checked_bias(req, self.svc.engine)     # a bad logit_bias is a 400 before streaming
            req[GRAMMAR_KEY] = self.svc.grammar_for(tools, req.get("tool_choice"), req.get("tools"),
                                               req.get("response_format"))
            max_new = int(req.get("max_completion_tokens") or req.get("max_tokens") or 0)   # 0/-1: the rest of the context
            self.svc.reasoning_budget(req)                         # a bad value is a 400 before streaming
            stop_strings(req)                                 # likewise a bad stop
            ids, thinking, max_new = self.svc.prepare(messages, tools, kw, max_new, force=force,
                                                 image_access=self._image_access())
            _debug_req("openai", req, messages, tools, max_new, thinking, len(ids))
            cancel = self._client_cancel()
            chunks = primed(openai_chunks(self.svc, req, ids, thinking, tools, max_new, cancel, force=force), ids)
            if not req.get("stream"):
                return self._json(200, openai_collect(chunks))
            self._sse()
            try:
                for c in chunks:
                    if c is None:
                        self.wfile.write(b": keep-alive\n\n")      # SSE comment
                    else:
                        self.wfile.write(b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
            except OSError:
                cancel.set()                                 # client left: stop the engine
                chunks.close()
            except EngineDied as e:                          # mid-stream
                err = {"error": {"type": "server_error", "message": f"{e}; the next request restarts it"}}
                self.wfile.write(b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")
            except ValueError as e:                          # engine ERR after the stream started: no 400 now
                err = {"error": {"type": "server_error", "message": str(e)}}
                self.wfile.write(b"data: " + json.dumps(err).encode() + b"\n\ndata: [DONE]\n\n")

        def _responses(self, req):
            """POST /v1/responses (serve/api/responses.py); after the stream starts, errors are response.failed."""
            try:
                ids, thinking, tools, max_new, asm, req, force = self._responses_prepare(req)
            except ResponsesError as e:
                return self._json(e.status, e.body())
            except ModelBusy as e:
                return self._json(409, responses_error_body(str(e), "server_error", code="model_busy"))
            except (GpuBusy, EngineStarting, EngineStuck, EngineDied) as e:
                return self._json(503, responses_error_body(str(e), "server_error", code="server_error"))
            cancel = self._client_cancel()

            def events():
                yield from asm.start()
                done = None
                for kind, x in self.svc.run(ids, thinking, tools, max_new, req, cancel, force=force):
                    if kind == "ping":
                        yield None
                    elif kind == "event":
                        yield from asm.feed(x)
                    elif kind == "done":
                        done = x
                if done is not None and done["finish"] != "cancel" and not cancel.is_set():
                    yield from asm.finish(done)
            try:
                items = primed(events(), ids)
            except EngineError:
                raise
            except ValueError as e:                          # too long for the context: found when the engine counted
                err = ResponsesError(str(e), "input", "context_length_exceeded" if "context" in str(e) else None)
                return self._json(err.status, err.body())
            if not req.get("stream"):
                try:
                    result = responses_api.collect(items)
                except EngineDied as e:
                    return self._json(503, responses_error_body(f"{e}; the next request restarts it", "server_error",
                                                                code="server_error"))
                except EngineError as e:                     # the engine failed
                    return self._json(500, responses_error_body(str(e), "server_error", code="server_error"))
                except ValueError as e:                      # the engine refused the request
                    err = ResponsesError(str(e), "input", "context_length_exceeded" if "context" in str(e) else None)
                    return self._json(err.status, err.body())
                return self._json(200, result)
            self._sse()
            last = time.monotonic()

            def send(e):
                self.wfile.write(f"event: {e['type']}\n".encode() + b"data: " +
                                 json.dumps(e, ensure_ascii=False).encode() + b"\n\n")

            try:
                for e in items:
                    if e is None:
                        # clients' idle timers count events, not SSE comments: send in_progress every 15 s
                        if time.monotonic() - last >= 15:
                            send(asm.in_progress())
                            last = time.monotonic()
                        else:
                            self.wfile.write(b": keep-alive\n\n")
                    else:
                        send(e)
                        last = time.monotonic()
                    self.wfile.flush()
            except OSError:
                cancel.set()                                 # client left: stop the engine
                items.close()
            except EngineDied as e:                          # mid-stream: response.failed
                self._responses_failed(asm, f"{e}; the next request restarts it", "server_error", send)
            except ValueError as e:                          # engine ERR after the stream started
                self._responses_failed(asm, str(e), "server_error", send)

        def _responses_failed(self, asm, message, code, send):
            e = asm.failed(message, code)
            try:
                send(e)
                self.wfile.flush()
            except OSError:
                pass

        def _responses_prepare(self, req):
            responses_api.check_request(req)
            messages = responses_api.input_messages(req)
            tools, names, skipped = responses_api.request_tools(req)
            try:
                tools, force = forced_call(req.get("tool_choice"), tools, self.svc.dialect.forced_opening)
            except ValueError as e:
                raise ResponsesError(str(e), "tool_choice") from None
            kw = responses_api.template_kwargs(req)
            try:
                check_format(responses_api.text_format(req))
            except ValueError as e:
                raise ResponsesError(str(e).replace("response_format", "text.format"), "text.format") from None
            try:
                self.svc.require_grammar(responses_api.text_format(req))
            except ValueError as e:
                raise ResponsesError(str(e).replace("response_format", "text.format"), "text.format") from None
            req[GRAMMAR_KEY] = self.svc.grammar_for(tools, req.get("tool_choice"), req.get("tools"),
                                               responses_api.text_format(req))
            noted = self.svc.__dict__.setdefault("hosted_tools_noted", set())   # once per tool
            if set(skipped) - noted:
                print(f"[server] warning: /v1/responses ignores hosted tools {', '.join(sorted(set(skipped) - noted))}", flush=True)
                noted.update(skipped)
            try:
                self.svc.reasoning_budget(req)                    # a bad value is a 400 before streaming
            except ValueError as e:
                raise ResponsesError(str(e), "reasoning_budget_tokens") from None
            try:
                checked_sampling(req)
            except ValueError as e:
                raise ResponsesError(str(e), str(e).split(":")[0]) from None
            try:
                stop_strings(req)
            except ValueError as e:
                raise ResponsesError(str(e), "stop") from None
            self._take_turn(bool(req.get("stream")))
            self.svc.load()
            try:
                ids, thinking, max_new = self.svc.prepare(messages, tools, kw, req.get("max_output_tokens") or 0, force=force,
                                                     image_access=self._image_access())
            except ResponsesError:
                raise
            except ValueError as e:                          # too long for the context, or an image without vision
                raise ResponsesError(str(e), "input", "context_length_exceeded" if "context" in str(e) else None) \
                    from None
            _debug_req("responses", req, messages, tools, max_new, thinking, len(ids))
            include = req.get("include") if isinstance(req.get("include"), list) else []
            asm = responses_api.Assembler(req, self.svc.model, len(ids), names,
                                          "reasoning.encrypted_content" in include)
            return ids, thinking, tools, max_new, asm, req, force

        def _count_tokens(self, req):
            """Anthropic's token count: the rendered, tokenized prompt, without running the model."""
            messages, tools, kw = anthropic_to_messages(req, self.svc.anthropic_think_unasked)
            tools, _ = forced_call(req.get("tool_choice"), tools, self.svc.dialect.forced_opening)   # same 400 as the request itself ("none": no tools)
            stop_strings(req)                                 # same 400 as the request itself
            self._json(200, {"input_tokens": len(self.svc.encode_prompt(messages, tools, kw))})

        def _anthropic(self, req):
            messages, tools, kw = anthropic_to_messages(req, self.svc.anthropic_think_unasked)
            tools, force = forced_call(req.get("tool_choice"), tools, self.svc.dialect.forced_opening)   # "any" / {"type": "tool", "name": N}
            max_new = int(req.get("max_tokens") or 0)                  # 0/-1: the rest of the context
            response_format = anthropic_format(req)            # structured outputs
            try:
                self.svc.require_grammar(response_format)
            except ValueError as e:
                raise ValueError(str(e).replace("response_format json_schema and json_object", "output_config.format json_schema")) from None
            req[GRAMMAR_KEY] = self.svc.grammar_for(tools, req.get("tool_choice"), req.get("tools"), response_format)
            self.svc.reasoning_budget(req)                         # a bad value is a 400 before streaming
            checked_sampling(req)                             # likewise a sampling value out of range
            stop_strings(req)                                 # likewise a bad stop
            self._take_turn(bool(req.get("stream")))
            self.svc.load()
            ids, thinking, max_new = self.svc.prepare(messages, tools, kw, max_new, force=force,
                                                 image_access=self._image_access())
            _debug_req("anthropic", req, messages, tools, max_new, thinking, len(ids))
            cancel = self._client_cancel()
            events = primed(anthropic_events(self.svc, req, ids, thinking, tools, max_new, cancel, force=force), ids)
            if not req.get("stream"):
                return self._json(200, anthropic_collect(events))
            self._sse()
            try:
                for item in events:
                    if item is None:
                        self.wfile.write(b": keep-alive\n\n")
                    else:
                        name, e = item
                        self.wfile.write(f"event: {name}\n".encode() + b"data: " +
                                         json.dumps(e, ensure_ascii=False).encode() + b"\n\n")
                    self.wfile.flush()
            except OSError:
                cancel.set()
                events.close()
            except EngineDied as e:                          # mid-stream: an Anthropic error event
                err = {"type": "error", "error": {"type": "api_error", "message": f"{e}; the next request restarts it"}}
                self.wfile.write(b"event: error\ndata: " + json.dumps(err).encode() + b"\n\n")
            except ValueError as e:                          # engine ERR after the stream started
                err = {"type": "error", "error": {"type": "api_error", "message": str(e)}}
                self.wfile.write(b"event: error\ndata: " + json.dumps(err).encode() + b"\n\n")

    return Handler


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    request_queue_size = 256      # socketserver's backlog of 5 resets some clients of a burst of simultaneous connections

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):   # a hang-up needs no stack trace
            super().handle_error(request, client_address)


def serve(target: Service | Router, host="127.0.0.1", port=8080) -> ThreadingHTTPServer:
    router = target if isinstance(target, Router) else Router([target])
    svc = router.front
    svc.host_names = host_names_for(host, svc.allowed_hosts, svc.trusted_origins)
    router.start_telemetry()
    httpd = Server((host, port), make_handler(router))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd

# ===== file: serve/models/__init__.py =====
"""Per-model chat dialects: how a model's prompt is rendered, its output parsed and its turn ended.

The server picks one from the model file's architecture; a file of an unknown architecture is refused.
"""
from __future__ import annotations


class Dialect:
    name = ""
    stop_texts: tuple[str, ...] = ()        # texts that end the model's turn
    reasoning_close = ""                    # closes thinking the model left open, or that reached its budget or repeats one token
    forced_call_ends_reply = False          # a named tool_choice ends the reply after its one call
    nothink_lead = ""                       # text between a no-thinking prompt and a forced call's opening
    image_pad = "<|image_pad|>"             # the prompt token that stands for one row of an image's embeddings
    image_start = "<|vision_start|>"        # the token an image's pad run follows ("" : every pad is an image row)
    in_engine_vision = False                # the image encoder always lives in the engine (only --vision dynamic)
    effort_turn = ""                        # a trailing turn that states the reasoning effort ("{}" for its text)

    def effort_level(self, level):
        """The frontend's reasoning level (low, medium, high, max or a number) as this model's template takes it."""
        if isinstance(level, int):
            raise ValueError(f"unknown reasoning effort {level!r}: use none, low, medium, high or max")
        return "xhigh" if level in ("high", "max") else level

    def template(self, path):
        """The prompt renderer: an object with `render(messages, tools=, add_generation_prompt=, **kwargs)` and
        `source`. `path` is the model's exported template file, which a dialect may ignore."""
        raise NotImplementedError

    def parser(self, thinking: bool, tools: list[dict] | None, stream_tools: bool = True):
        """An incremental output parser: `feed(text)` and `finish()` return events; `state` and `buf` tell the
        server where it is."""
        raise NotImplementedError

    def literal_tags(self, controls) -> dict[str, str]:
        """Texts that stay plain text inside a message, with the private-use mark standing for each while the
        prompt is rendered."""
        raise NotImplementedError

    def forced_opening(self, name: str | None) -> str:
        """The text that opens a call to tool `name` (None: any tool), for a forced tool choice."""
        raise NotImplementedError


def dialect_for(arch: str | None) -> Dialect:
    """The dialect of a model file's architecture; None (no file: the mock engine) is the plain chat-template one."""
    if arch == "deepseek41":
        from serve.models.deepseek41 import DeepSeek41
        return DeepSeek41()
    if arch in ("qwen4exp", None):
        from serve.models.qwen4exp import Qwen4Exp
        return Qwen4Exp()
    raise ValueError(f"no dialect for architecture {arch!r}")

# ===== file: serve/models/deepseek41.py =====
"""The DeepSeek-V4.1 dialect: prompts from DeepSeek's own encoder, `<think>` reasoning and DSML tool calls.

The call format follows the encoder (serve/models/deepseek41_encoding.py) and llama.cpp's parser
(common/parsers/deepseek.cpp, MIT): a reply is optional reasoning ending in `</think>` (or in the start of a call
block), the answer, then `<｜DSML｜ calls>` holding `<｜DSML｜ invoke name="N">` elements whose
`<｜DSML｜ parameter name="P" string="true|false">` values are text or JSON.
"""
from __future__ import annotations

import json
import re

from serve import frontend
from serve.frontend import Event, ToolCall
from serve.models import Dialect
from serve.models import deepseek41_encoding as enc

EOS = enc.eos_token
THINK_END = enc.thinking_end_token
CALLS_START = f"<{enc.dsml_token}{enc.tool_calls_block_name}>"
CALLS_END = f"</{enc.dsml_token}{enc.tool_calls_block_name}>"
INVOKE_START = f"<{enc.dsml_token}{enc.tool_call_tag_name}"
INVOKE_END = f"</{enc.dsml_token}{enc.tool_call_tag_name}>"
PARAM_START = f"<{enc.dsml_token}{enc.tool_parameter_tag_name}"
PARAM_END = f"</{enc.dsml_token}{enc.tool_parameter_tag_name}>"
CALLS_GAP = "\n\n"                      # between the answer and the call block
INVOKE_HEAD = re.compile(r'^' + re.escape(INVOKE_START) + r' name="([^"]*)">')
PARAM_HEAD = re.compile(r'^' + re.escape(PARAM_START) + r' name="([^"]*)" string="(true|false)">')
DSML_MARK = "\U000F2000"                # stands for the DSML marker while a message is rendered

# the frontend's levels -> the encoder's scale (low, high, max or 1-100); medium is its default
EFFORTS = {"low": "low", "medium": None, "high": "high", "max": "max"}


class DeepseekTemplate:
    """Renders messages with DeepSeek's encoder, taking the same keyword arguments as the Jinja template."""

    source = "deepseek41 encoder (serve/models/deepseek41_encoding.py)"

    def render(self, messages, tools=None, add_generation_prompt=True, enable_thinking=True, reasoning_effort=None,
               **_):
        msgs = [dict(m) for m in messages]
        if tools:
            wrapped = [{"type": "function", "function": t} for t in tools]
            if msgs and msgs[0].get("role") == "system":
                msgs[0]["tools"] = wrapped
            else:
                msgs.insert(0, {"role": "system", "content": "", "tools": wrapped})
        mode = "thinking" if enable_thinking is not False else "chat"
        effort = EFFORTS.get(reasoning_effort, reasoning_effort)
        text = enc.encode_messages(msgs, thinking_mode=mode, reasoning_effort=effort)
        if not add_generation_prompt and msgs and msgs[-1].get("role") in ("user", "tool", "system"):
            head = enc.ASSISTANT_SP_TOKEN
            cut = text.rfind(head)
            if cut >= 0 and text[cut + len(head):] in (enc.thinking_start_token, THINK_END):
                text = text[:cut]
        return text


def _hold(text: str, tags: tuple[str, ...]) -> int:
    """Length of the longest suffix of `text` that is a proper prefix of one of `tags`."""
    best = 0
    for tag in tags:
        for n in range(len(tag) - 1, best, -1):
            if text.endswith(tag[:n]):
                best = n
                break
    return best


def _value(text: str, is_string: bool):
    if is_string:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


class DeepseekParser:
    """Incremental parser of the model's text. `state` is "reasoning", "content" or "call"; text is held back
    while it may still be the start of a marker."""

    def __init__(self, thinking: bool = True, tools: list[dict] | None = None, stream_tools: bool = True):
        self.state = "reasoning" if thinking else "content"
        self.buf = ""
        self.stream_tools = stream_tools
        self.sub = "between"            # inside a call block: between | invoke | value
        self.call: ToolCall | None = None
        self.args: dict = {}
        self.first = True               # no argument written yet
        self.pname, self.pstring, self.pvalue = "", True, ""
        self.calls_seen = False

    # ------------------------------------------------------------------ helpers
    def _args(self, out: list, piece: str) -> None:
        if piece and self.stream_tools:
            out.append(Event("tool_args", piece, call=self.call))

    def _end_call(self, out: list) -> None:
        self._args(out, "}")
        out.append(Event("tool_call", call=ToolCall(name=self.call.name, arguments=self.args, id=self.call.id)))
        self.call = None

    # ------------------------------------------------------------------ text outside the call block
    def _reasoning(self, out: list) -> bool:
        """Advance reasoning; True when the state changed."""
        end, calls = self.buf.find(THINK_END), self.buf.find(CALLS_START)
        if end >= 0 and (calls < 0 or end < calls):
            if end:
                out.append(Event("reasoning", self.buf[:end]))
            self.buf, self.state = self.buf[end + len(THINK_END):], "content"
            return True
        if calls >= 0:                  # the thinking ran straight into the calls
            text = self.buf[:calls].rstrip()
            if text:
                out.append(Event("reasoning", text))
            self.buf, self.state = self.buf[calls + len(CALLS_START):], "call"
            return True
        keep = _hold(self.buf, (THINK_END, CALLS_GAP + CALLS_START, CALLS_START))
        if len(self.buf) > keep:
            out.append(Event("reasoning", self.buf[:len(self.buf) - keep]))
            self.buf = self.buf[len(self.buf) - keep:]
        return False

    def _content(self, out: list) -> bool:
        calls = self.buf.find(CALLS_START)
        if calls >= 0:
            text = self.buf[:calls].rstrip("\n")
            if text:
                out.append(Event("content", text))
            self.buf, self.state = self.buf[calls + len(CALLS_START):], "call"
            return True
        keep = _hold(self.buf, (CALLS_GAP + CALLS_START, CALLS_START))
        if len(self.buf) > keep:
            out.append(Event("content", self.buf[:len(self.buf) - keep]))
            self.buf = self.buf[len(self.buf) - keep:]
        return False

    # ------------------------------------------------------------------ the call block
    def _call(self, out: list) -> bool:
        """Advance inside the call block; True when more may follow in the buffer."""
        if self.sub == "between":
            s = self.buf.lstrip()
            if s.startswith(CALLS_END):
                self.buf, self.state, self.calls_seen = s[len(CALLS_END):], "content", True
                return True
            m = INVOKE_HEAD.match(s)
            if m:
                self.call = ToolCall(name=m.group(1), arguments={})
                self.args, self.first, self.sub = {}, True, "invoke"
                self.buf = s[m.end():]
                if self.stream_tools:
                    out.append(Event("tool_start", call=self.call))
                self._args(out, "{")
                return True
            if s and not (CALLS_END.startswith(s) or INVOKE_START.startswith(s) or
                          (s.startswith(INVOKE_START) and ">" not in s)):
                self.buf, self.state = CALLS_START + self.buf, "content"     # not a call block: it is text
                self.sub, self.calls_seen = "between", True
                out.append(Event("content", self.buf))
                self.buf = ""
            return False
        if self.sub == "invoke":
            s = self.buf.lstrip()
            if s.startswith(INVOKE_END):
                self.buf, self.sub = s[len(INVOKE_END):], "between"
                self._end_call(out)
                return True
            m = PARAM_HEAD.match(s)
            if m:
                self.pname, self.pstring, self.pvalue = m.group(1), m.group(2) == "true", ""
                self._args(out, ("" if self.first else ",") + json.dumps(m.group(1), ensure_ascii=False) + ":")
                self.first = False
                if self.pstring:
                    self._args(out, '"')
                self.buf, self.sub = s[m.end():], "value"
                return True
            return False                # a tag still arriving
        end = self.buf.find(PARAM_END)  # a value
        if end >= 0:
            text = self.buf[:end]
            self.pvalue += text
            self.args[self.pname] = _value(self.pvalue, self.pstring)
            if self.pstring:
                self._args(out, json.dumps(text, ensure_ascii=False)[1:-1] + '"')
            else:
                self._args(out, json.dumps(self.args[self.pname], ensure_ascii=False))
            self.buf, self.sub = self.buf[end + len(PARAM_END):], "invoke"
            return True
        keep = _hold(self.buf, (PARAM_END,))
        text, self.buf = self.buf[:len(self.buf) - keep], self.buf[len(self.buf) - keep:]
        self.pvalue += text
        if self.pstring:
            self._args(out, json.dumps(text, ensure_ascii=False)[1:-1])
        return False

    # ------------------------------------------------------------------ interface
    def feed(self, delta: str) -> list[Event]:
        self.buf += delta
        out: list[Event] = []
        while True:
            if self.state == "reasoning":
                more = self._reasoning(out)
            elif self.state == "content":
                more = self._content(out)
            else:
                more = self._call(out)
            if not more:
                return out

    def finish(self) -> list[Event]:
        """End of generation: flush what is held. A call cut off mid-way is closed without a "tool_call"; text
        that was only the start of a marker is text."""
        out: list[Event] = []
        if self.state == "call":
            if self.call is not None:
                if self.sub == "value":
                    self.pvalue += self.buf
                    if self.pstring:
                        self._args(out, json.dumps(self.buf, ensure_ascii=False)[1:-1] + '"')
                    else:
                        self._args(out, json.dumps(self.pvalue))
                self._args(out, "}")
                self.call = None
            elif self.buf.strip() and not self.calls_seen:
                out.append(Event("content", CALLS_START + self.buf))
        elif self.buf:
            out.append(Event("reasoning" if self.state == "reasoning" else "content", self.buf))
        self.buf = ""
        return out


class DeepSeek41(Dialect):
    def effort_level(self, level):
        return level

    name = "deepseek41"
    in_engine_vision = True
    image_pad = enc.IMAGE_PLACEHOLDER
    image_start = ""
    stop_texts = (EOS,)
    reasoning_close = "\n" + THINK_END
    forced_call_ends_reply = True
    nothink_lead = CALLS_GAP

    def template(self, path):
        return DeepseekTemplate()

    def parser(self, thinking, tools, stream_tools=True):
        return DeepseekParser(thinking=thinking, tools=tools, stream_tools=stream_tools)

    def literal_tags(self, controls):
        tags = {**frontend.literal_tags(controls), enc.dsml_token: DSML_MARK}
        return dict(sorted(tags.items(), key=lambda t: -len(t[0])))

    def forced_opening(self, name):
        head = f"{CALLS_START}\n{INVOKE_START} name=\""
        return head + (f'{name}">\n' if name else "")

# ===== file: serve/models/deepseek41_encoding.py =====
# Vendored from deepseek-ai/DeepSeek-V4.1-Flash-1017 encoding/encoding.py (MIT, Copyright (c) 2023 DeepSeek;
# the license text is in THIRD_PARTY_LICENSES). Pruned: the JSON case loader and its driver are removed.
"""
DeepSeek-V4.1 Text and Vision Encoding

A fully self-contained implementation for encoding/decoding DeepSeek-V4.1 chat
messages with tool calling, thinking mode, quick instruction tasks, and image
content blocks. No dependency on encoding_dsv4.

V4.1 changes relative to V4:

1. DSML tag names: tool calls are wrapped in "<｜DSML｜ calls>" blocks with
   "<｜DSML｜ invoke>" / "<｜DSML｜ parameter>" tags (leading-space tag names).
2. Numeric reasoning effort: "Reasoning Effort: {budget} (range 1-100, ...)".
   Accepts an int in [1, 100] or one of "low"/"high"/"max"
   (mapped to 50/75/100). Defaults to "high". Only rendered in thinking mode.
3. Mid-conversation system messages are supported via the "<｜System｜>" token.
   A mid-conversation system message behaves like a user message for the purpose
   of appending the assistant generation header.
"""

from typing import Any, Dict, List, Union, Optional, Tuple
import copy
import json
import re

# ============================================================
# Special Tokens
# ============================================================

bos_token: str = "<｜begin▁of▁sentence｜>"
eos_token: str = "<｜end▁of▁sentence｜>"
thinking_start_token: str = "<think>"
thinking_end_token: str = "</think>"
dsml_token: str = "｜DSML｜"

USER_SP_TOKEN = "<｜User｜>"
ASSISTANT_SP_TOKEN = "<｜Assistant｜>"
LATEST_REMINDER_SP_TOKEN = "<｜latest_reminder｜>"

IMAGE_PLACEHOLDER = "<｜deepseek_image｜>"
IMAGE_TAG_PATTERN = re.compile(r"<image>(.*?)</image>", re.DOTALL)

# Task special tokens for internal classification tasks
DS_TASK_SP_TOKENS = {
    "action": "<｜action｜>",
    "query": "<｜query｜>",
    "authority": "<｜authority｜>",
    "domain": "<｜domain｜>",
    "title": "<｜title｜>",
    "read_url": "<｜read_url｜>",
}
VALID_TASKS = set(DS_TASK_SP_TOKENS.keys())

# ============================================================
# Templates
# ============================================================

system_msg_template: str = "{content}"
user_msg_template: str = "{content}"
latest_reminder_msg_template: str = "{content}"
assistant_msg_template: str = "{reasoning}{content}{tool_calls}" + eos_token
assistant_msg_wo_eos_template: str = "{reasoning}{content}{tool_calls}"
thinking_template: str = "{reasoning_content}"

response_format_template: str = (
    "## Response Format:\n\nYou MUST strictly adhere to the following schema to reply:\n{schema}"
)

tool_output_template: str = (
    "<tool_result>{content}</tool_result>"
)

# ============================================================
# Utility Functions
# ============================================================

def to_json(value: Any) -> str:
    """Serialize a value to JSON string."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except:
        return json.dumps(value, ensure_ascii=True)


def tools_from_openai_format(tools):
    """Extract function definitions with namespace-qualified names."""
    functions = []
    for tool in tools:
        function = dict(tool["function"])
        if tool.get("namespace") is not None:
            function["namespace"] = tool["namespace"]
        function["name"] = _tool_name_for_encoding(function)
        namespace = function.pop("namespace", None)
        if isinstance(namespace, dict) and namespace.get("description"):
            function["description"] = (
                namespace["description"] + "\n" + (function.get("description") or "")
            )
        functions.append(function)
    return functions


def _split_tool_name(name: str, namespace: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Split a qualified name and validate any explicit namespace."""
    prefix, separator, bare_name = name.partition("::")
    if separator:
        assert namespace in (None, prefix), (
            f"Conflicting tool namespaces: {namespace} != {prefix}"
        )
        namespace, name = prefix, bare_name
    assert "::" not in name, f"Tool name must not contain '::': {name}"
    assert namespace is None or "::" not in namespace, (
        f"Tool namespace must not contain '::': {namespace}"
    )
    return namespace, name


def _tool_name_for_encoding(tool: Dict[str, Any]) -> str:
    namespace = tool.get("namespace")
    if isinstance(namespace, dict):
        namespace = namespace["name"]
    namespace, name = _split_tool_name(tool["name"], namespace)
    return name if namespace is None else f"{namespace}::{name}"


def tool_calls_from_openai_format(tool_calls):
    """Convert OpenAI-format tool calls to internal format."""
    calls = []
    for tool_call in tool_calls:
        function = tool_call["function"]
        namespace, name = _split_tool_name(
            function["name"], tool_call.get("namespace") or function.get("namespace")
        )
        call = {"name": name, "arguments": function["arguments"]}
        if namespace is not None:
            call["namespace"] = namespace
        calls.append(call)
    return calls


def tool_calls_to_openai_format(tool_calls):
    """Convert internal tool calls to OpenAI format."""
    calls = []
    for tool_call in tool_calls:
        call = {
            "type": "function",
            "function": {
                "name": tool_call["name"],
                "arguments": tool_call["arguments"],
            }
        }
        if tool_call.get("namespace") is not None:
            call["namespace"] = tool_call["namespace"]
        calls.append(call)
    return calls


def decode_dsml_to_arguments(tool_name: str, tool_args: Dict[str, Tuple[str, str]]) -> Dict[str, str]:
    """
    Decode DSML parameters back to a tool call dict.

    Args:
        tool_name: Name of the tool.
        tool_args: Dict mapping param_name -> (value, is_string_flag).

    Returns:
        Dict with "name", "arguments" (JSON string), and optional "namespace".
    """
    def _decode_value(key: str, value: str, string: str):
        if string == "true":
            value = to_json(value)
        return f"{to_json(key)}: {value}"

    tool_args_json = "{" + ", ".join([_decode_value(k, v, string=is_str) for k, (v, is_str) in tool_args.items()]) + "}"
    namespace, name = _split_tool_name(tool_name)
    tool_call = dict(name=name, arguments=tool_args_json)
    if namespace is not None:
        tool_call["namespace"] = namespace
    return tool_call


# ============================================================
# Preprocessing
# ============================================================

def merge_tool_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Merge tool messages into the preceding user message using content_blocks format.

    DeepSeek-V4.1 does not have a standalone "tool" role; instead, tool results
    are encoded as <tool_result> blocks within user messages.
    """
    merged: List[Dict[str, Any]] = []

    for msg in messages:
        msg = copy.deepcopy(msg)
        role = msg.get("role")

        if role == "tool":
            # Convert tool message to a user message with tool_result block
            content = msg.get("content", "")
            if msg.get("content_blocks") and not isinstance(msg.get("content"), str):
                content = msg["content_blocks"]
            tool_block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content,
            }
            # Merge into previous message if it's already a user (merged tool)
            if merged and merged[-1].get("role") == "user" and "content_blocks" in merged[-1]:
                merged[-1]["content_blocks"].append(tool_block)
            else:
                merged.append({
                    "role": "user",
                    "content_blocks": [tool_block],
                })
        elif role == "user":
            content_blocks = msg.get("content_blocks")
            if content_blocks is None:
                content = msg.get("content", "")
                content_blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            if merged and merged[-1].get("role") == "user" and "content_blocks" in merged[-1] and merged[-1].get("task") is None:
                merged[-1]["content_blocks"].extend(content_blocks)
            else:
                # Preserve structured content and all message-level metadata.
                new_msg = msg
                new_msg["content_blocks"] = content_blocks
                merged.append(new_msg)
        else:
            merged.append(msg)

    return merged


def sort_tool_results_by_call_order(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort tool_result blocks within user messages by the order of tool_calls
    in the preceding assistant message.
    """
    last_tool_call_order: Dict[str, int] = {}

    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            last_tool_call_order = {}
            for idx, tc in enumerate(msg["tool_calls"]):
                tc_id = tc.get("id") or tc.get("function", {}).get("id", "")
                if tc_id:
                    last_tool_call_order[tc_id] = idx

        elif role == "user" and msg.get("content_blocks"):
            tool_blocks = [b for b in msg["content_blocks"] if b.get("type") == "tool_result"]
            if len(tool_blocks) > 1 and last_tool_call_order:
                sorted_blocks = sorted(
                    tool_blocks,
                    key=lambda b: last_tool_call_order.get(b.get("tool_use_id", ""), 0)
                )
                sorted_idx = 0
                new_blocks = []
                for block in msg["content_blocks"]:
                    if block.get("type") == "tool_result":
                        new_blocks.append(sorted_blocks[sorted_idx])
                        sorted_idx += 1
                    else:
                        new_blocks.append(block)
                msg["content_blocks"] = new_blocks

    return messages


# ============================================================
# Vision Message Preprocessing
# ============================================================

def parse_tagged_text(text: str) -> Union[str, List[Dict[str, Any]]]:
    """Convert ``<image>path</image>`` text into standard content blocks."""
    matches = list(IMAGE_TAG_PATTERN.finditer(text))
    remaining = IMAGE_TAG_PATTERN.sub("", text)
    if "<image>" in remaining or "</image>" in remaining:
        raise ValueError("Malformed <image>path</image> tag")
    if not matches:
        return text

    blocks: List[Dict[str, Any]] = []
    cursor = 0
    for match in matches:
        if match.start() > cursor:
            blocks.append({"type": "text", "text": text[cursor:match.start()]})
        path = match.group(1)
        if not path:
            raise ValueError("Image path must not be empty")
        blocks.append({
            "type": "image_url",
            "image_url": {"url": path},
        })
        cursor = match.end()
    if cursor < len(text):
        blocks.append({"type": "text", "text": text[cursor:]})
    return blocks


def _is_image_block(block: Dict[str, Any]) -> bool:
    """Return whether a content block is an OpenAI/Anthropic/internal image."""
    return isinstance(block, dict) and block.get("type") in ("image", "image_url")


def _extract_image(block: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a supported image block into an internal image record."""
    record: Dict[str, Any] = {"type": "image"}
    if block.get("type") == "image_url":
        image_url = block.get("image_url")
        if isinstance(image_url, str):
            record["url"] = image_url
        else:
            record["url"] = (image_url or {}).get("url", "")
    else:
        for key in ("source", "url", "data"):
            if key in block:
                record[key] = block[key]
    if not any(record.get(key) for key in ("source", "url", "data")):
        raise ValueError("Image block does not contain a valid source")
    return record


def _process_image_blocks(
    blocks: List[Any], image_placeholder: str = IMAGE_PLACEHOLDER
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """Replace image blocks and collect their records in one ordered traversal."""
    new_blocks: List[Any] = []
    images: List[Dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            new_blocks.append(block)
            continue
        if _is_image_block(block):
            new_blocks.append({"type": "text", "text": image_placeholder})
            images.append(_extract_image(block))
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            block = copy.copy(block)
            block["content"], nested_images = _process_image_blocks(
                block["content"], image_placeholder)
            new_blocks.append(block)
            images.extend(nested_images)
        elif block.get("type") == "text":
            text = block.get("text") or ""
            if IMAGE_PLACEHOLDER in text:
                raise ValueError(
                    f"Text block contains image placeholder '{IMAGE_PLACEHOLDER}': "
                    f"'{text[:100]}'. Images should be separate content blocks."
                )
            new_blocks.append(block)
        else:
            new_blocks.append(block)
    return new_blocks, images


def _validate_no_image_sp_tokens(msg: Dict[str, Any]) -> None:
    """Reject user-supplied image placeholder tokens in textual fields."""
    content = msg.get("content")
    if isinstance(content, str) and IMAGE_PLACEHOLDER in content:
        raise ValueError(
            f"Message content contains image special token '{IMAGE_PLACEHOLDER}'. "
            "Images should be provided as image content blocks."
        )
    reasoning_content = msg.get("reasoning_content")
    if isinstance(reasoning_content, str) and IMAGE_PLACEHOLDER in reasoning_content:
        raise ValueError(
            f"reasoning_content contains image special token '{IMAGE_PLACEHOLDER}'"
        )


def process_image_messages(
    messages: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Normalize image blocks and return their records in prompt order."""
    processed: List[Dict[str, Any]] = []
    images: List[Dict[str, Any]] = []
    for msg in messages:
        msg = copy.deepcopy(msg)
        _validate_no_image_sp_tokens(msg)

        if isinstance(msg.get("content"), list) and "content_blocks" not in msg:
            msg["content_blocks"] = msg.pop("content")

        if msg.get("content_blocks"):
            msg["content_blocks"], message_images = _process_image_blocks(
                msg["content_blocks"])
            images.extend(message_images)
            if not isinstance(msg.get("content"), str):
                texts = [
                    block.get("text", "")
                    for block in msg["content_blocks"]
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                msg["content"] = "\n\n".join(texts)

        processed.append(msg)
    return processed, images


def _read_until_stop(index: int, text: str, stop: List[str]) -> Tuple[int, str, Optional[str]]:
    """
    Read text from index until one of the stop strings is found.

    Returns:
        Tuple of (new_index, content_before_stop, matched_stop_string_or_None).
    """
    min_pos = len(text)
    matched_stop = None

    for s in stop:
        pos = text.find(s, index)
        if pos != -1 and pos < min_pos:
            min_pos = pos
            matched_stop = s

    if matched_stop:
        content = text[index:min_pos]
        return min_pos + len(matched_stop), content, matched_stop
    else:
        content = text[index:]
        return len(text), content, None

# ============================================================
# V4.1 Special Tokens and DSML Tag Names
# ============================================================

SYSTEM_SP_TOKEN = "<｜System｜>"

tool_calls_block_name: str = " calls"
tool_call_tag_name: str = " invoke"
tool_parameter_tag_name: str = " parameter"

tool_call_template: str = (
    "<{dsml_token}{tool_call_tag_name} name=\"{name}\">\n{arguments}\n</{dsml_token}{tool_call_tag_name}>"
)
tool_calls_template = (
    "<{dsml_token}{tc_block_name}>\n{tool_calls}\n</{dsml_token}{tc_block_name}>"
)

# ============================================================
# Reasoning Effort (numeric budget)
# ============================================================

REASONING_EFFORT_TEMPLATE = (
    "Reasoning Effort: {budget} "
    "(range 1-100, the higher the value, the more thorough the reasoning)\n\n"
)

REASONING_EFFORT_MAPPINGS: Dict[str, int] = {
    "low": 50,
    "high": 75,
    "max": 100,
}
DEFAULT_REASONING_EFFORT = "high"


def render_reasoning_effort(
    index: int,
    thinking_mode: str,
    effort: Union[str, int, None],
) -> str:
    """Render the V4.1 numeric reasoning effort prefix (thinking mode, index 0 only)."""
    if effort is None:
        effort = DEFAULT_REASONING_EFFORT
    assert (
        type(effort) is int and 1 <= effort <= 100
    ) or effort in REASONING_EFFORT_MAPPINGS, (
        "Invalid reasoning effort for deepseek_v41: "
        f"{effort}, should be int within [1,100] or {list(REASONING_EFFORT_MAPPINGS)}"
    )
    if type(effort) is str:
        effort = REASONING_EFFORT_MAPPINGS[effort]
    if index == 0 and thinking_mode == "thinking":
        return REASONING_EFFORT_TEMPLATE.format(budget=effort)
    return ""


# ============================================================
# Tools rendering
# ============================================================

TOOLS_TEMPLATE = """## Tools

You have access to a set of tools to help answer the user's question. You can invoke tools by writing a "<{dsml_token}{tc_block_name}>" block like the following:

<{dsml_token}{tc_block_name}>
<{dsml_token}{tool_call_tag_name} name="$TOOL_NAME">
<{dsml_token}{tool_parameter_tag_name} name="$PARAMETER_NAME" string="true|false">$PARAMETER_VALUE</{dsml_token}{tool_parameter_tag_name}>
...
</{dsml_token}{tool_call_tag_name}>
<{dsml_token}{tool_call_tag_name} name="$TOOL_NAME2">
...
</{dsml_token}{tool_call_tag_name}>
</{dsml_token}{tc_block_name}>

String parameters should be specified as is and set `string="true"`. For all other types (numbers, booleans, arrays, objects), pass the value in JSON format and set `string="false"`.

If thinking_mode is enabled (triggered by {thinking_start_token}), you MUST output your complete reasoning inside {thinking_start_token}...{thinking_end_token} BEFORE any tool calls or final response.

Otherwise, output directly after {thinking_end_token} with tool calls or final response.

### Available Tool Schemas

{tool_schemas}

You MUST strictly follow the above defined tool name and parameter schemas to invoke tool calls.
"""


def render_tools(tools: List[Dict[str, Union[str, Dict[str, Any]]]]) -> str:
    """Render tool schemas into the V4.1 system prompt format."""
    tools_json = [to_json(t) for t in tools]

    return TOOLS_TEMPLATE.format(
        tool_schemas="\n".join(tools_json),
        dsml_token=dsml_token,
        tc_block_name=tool_calls_block_name,
        tool_call_tag_name=tool_call_tag_name,
        tool_parameter_tag_name=tool_parameter_tag_name,
        thinking_start_token=thinking_start_token,
        thinking_end_token=thinking_end_token,
    )


def encode_arguments_to_dsml(tool_call: Dict[str, Any]) -> str:
    """Encode tool call arguments into V4.1 DSML parameter format."""
    p_dsml_template = (
        '<{dsml_token}{tool_parameter_tag_name} name="{key}" string="{is_str}">'
        '{value}</{dsml_token}{tool_parameter_tag_name}>'
    )
    P_dsml_strs = []

    arguments = tool_call["arguments"]
    if not isinstance(arguments, dict):
        # Tolerate JSON strings, including double-encoded ones.
        for _ in range(2):
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    break
            else:
                break
        if not isinstance(arguments, dict):
            arguments = {"arguments": tool_call["arguments"]}

    for k, v in arguments.items():
        P_dsml_strs.append(p_dsml_template.format(
            dsml_token=dsml_token,
            tool_parameter_tag_name=tool_parameter_tag_name,
            key=k,
            is_str="true" if isinstance(v, str) else "false",
            value=v if isinstance(v, str) else to_json(v),
        ))

    return "\n".join(P_dsml_strs)


# ============================================================
# Message Rendering
# ============================================================

def find_last_user_index(messages: List[Dict[str, Any]]) -> int:
    """
    Find the index of the last user message.

    V4.1 supports mid-conversation system messages, which count as user
    messages for the purposes of the assistant generation header.
    User messages containing only tool results do not start a new turn.
    """
    last_user_index = -1
    for idx in range(len(messages) - 1, -1, -1):
        role = messages[idx].get("role")
        if role == "user" or (role == "system" and idx > 0):
            content_blocks = messages[idx].get("content_blocks")
            if content_blocks and not any(
                block.get("type") in ("text", "image", "image_url")
                for block in content_blocks
            ):
                continue
            last_user_index = idx
            break
    return last_user_index


def render_message(
    index: int,
    messages: List[Dict[str, Any]],
    thinking_mode: str,
    drop_thinking: bool = True,
    reasoning_effort: Union[str, int, None] = None,
) -> str:
    """
    Render a single message at the given index into its V4.1 encoded string form.
    """
    assert 0 <= index < len(messages)
    assert thinking_mode in ["chat", "thinking"], f"Invalid thinking_mode `{thinking_mode}`"

    msg = messages[index]
    last_user_idx = find_last_user_index(messages)

    role = msg.get("role")
    content = msg.get("content")
    tools = msg.get("tools")
    response_format = msg.get("response_format")
    tool_calls = msg.get("tool_calls")
    reasoning_content = msg.get("reasoning_content")
    wo_eos = msg.get("wo_eos", False)

    if tools:
        tools = tools_from_openai_format(tools)
    if tool_calls:
        tool_calls = tool_calls_from_openai_format(tool_calls)

    # Reasoning effort prefix (thinking mode, index 0 only)
    reasoning_effort_prompt = render_reasoning_effort(index, thinking_mode, reasoning_effort)
    # System token leads the conversation when there is a reasoning effort prompt
    # or the first message is a system message.
    prompt = SYSTEM_SP_TOKEN if index == 0 and (reasoning_effort_prompt or role == "system") else ""
    prompt += reasoning_effort_prompt

    if role == "system":
        if index > 0:
            # Mid-conversation system message
            prompt += SYSTEM_SP_TOKEN
        prompt += system_msg_template.format(content=content or "")
        if tools:
            prompt += "\n\n" + render_tools(tools)
        if response_format:
            prompt += "\n\n" + response_format_template.format(schema=to_json(response_format))

    elif role == "user":
        prompt += USER_SP_TOKEN

        # Handle content blocks (tool results mixed with text)
        content_blocks = msg.get("content_blocks")
        if content_blocks:
            parts = []
            for block in content_blocks:
                block_type = block.get("type")
                if block_type == "text":
                    parts.append(block.get("text", ""))
                elif block_type == "tool_result":
                    tool_content = block.get("content", "")
                    if isinstance(tool_content, list):
                        text_parts = []
                        for b in tool_content:
                            if b.get("type") == "text":
                                text_parts.append(b.get("text", ""))
                            else:
                                text_parts.append(f"[Unsupported {b.get('type')}]")
                        tool_content = "\n\n".join(text_parts)
                    parts.append(tool_output_template.format(content=tool_content))
                else:
                    parts.append(f"[Unsupported {block_type}]")
            prompt += "\n\n".join(parts)
        else:
            prompt += content or ""

    elif role == "latest_reminder":
        prompt += LATEST_REMINDER_SP_TOKEN + latest_reminder_msg_template.format(content=content)

    elif role == "tool":
        raise NotImplementedError("deepseek_v41 merges tool messages into user; please preprocess with merge_tool_messages()")

    elif role == "assistant":
        thinking_part = ""
        tc_content = ""

        if tool_calls:
            tc_list = [
                tool_call_template.format(
                    dsml_token=dsml_token,
                    tool_call_tag_name=tool_call_tag_name,
                    name=_tool_name_for_encoding(tc),
                    arguments=encode_arguments_to_dsml(tc)
                )
                for tc in tool_calls
            ]
            tc_content += '\n\n' + tool_calls_template.format(
                dsml_token=dsml_token,
                tool_calls="\n".join(tc_list),
                tc_block_name=tool_calls_block_name,
            )

        summary_content = content or ""
        rc = reasoning_content or ""

        # Check if previous message has a task - if so, this is a task output (no thinking)
        prev_has_task = index - 1 >= 0 and messages[index - 1].get("task") is not None

        if thinking_mode == "thinking" and not prev_has_task:
            if not drop_thinking or index > last_user_idx:
                thinking_part = thinking_template.format(reasoning_content=rc) + thinking_end_token
            else:
                thinking_part = ""

        if wo_eos:
            prompt += assistant_msg_wo_eos_template.format(
                reasoning=thinking_part,
                content=summary_content,
                tool_calls=tc_content,
            )
        else:
            prompt += assistant_msg_template.format(
                reasoning=thinking_part,
                content=summary_content,
                tool_calls=tc_content,
            )
    else:
        raise NotImplementedError(f"Unknown role: {role}")

    # Append transition tokens based on what follows
    if index + 1 < len(messages) and messages[index + 1].get("role") not in ["assistant", "latest_reminder"]:
        return prompt

    task = messages[index].get("task")
    if task is not None:
        # Task special token for internal classification tasks
        assert task in VALID_TASKS, f"Invalid task: '{task}'. Valid tasks are: {list(VALID_TASKS)}"
        task_sp_token = DS_TASK_SP_TOKENS[task]

        if task != "action":
            # Non-action tasks: append task sp token directly after the message
            prompt += task_sp_token
        else:
            # Action task: append Assistant + thinking token + action sp token
            prompt += ASSISTANT_SP_TOKEN
            prompt += thinking_end_token if thinking_mode != "thinking" else thinking_start_token
            prompt += task_sp_token

    elif role == "user" or (role == "system" and index > 0):
        # Normal generation: append Assistant + thinking token
        # (mid-conversation system messages also trigger the assistant header)
        prompt += ASSISTANT_SP_TOKEN
        if not drop_thinking and thinking_mode == "thinking":
            prompt += thinking_start_token
        elif drop_thinking and thinking_mode == "thinking" and index >= last_user_idx:
            prompt += thinking_start_token
        else:
            prompt += thinking_end_token

    return prompt


# ============================================================
# Main Encoding Function
# ============================================================

def _drop_thinking_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Drop reasoning_content and non-essential messages before the last user message.
    Same as V4, but uses the V4.1 last-user definition (mid systems count).
    """
    last_user_idx = find_last_user_index(messages)
    result = []
    keep_roles = {"user", "system", "tool", "latest_reminder", "direct_search_results"}

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        if role in keep_roles or idx >= last_user_idx:
            result.append(msg)
        elif role == "assistant":
            msg = copy.copy(msg)
            msg.pop("reasoning_content", None)
            result.append(msg)

    return result


def _encode_messages_text(
    messages: List[Dict[str, Any]],
    thinking_mode: str,
    context: Optional[List[Dict[str, Any]]] = None,
    drop_thinking: bool = True,
    add_default_bos_token: bool = True,
    reasoning_effort: Union[str, int, None] = None,
) -> str:
    """Encode preprocessed (text-only) messages into the V4.1 prompt format."""
    context = context if context else []

    full_messages = context + messages

    prompt = bos_token if add_default_bos_token and len(context) == 0 else ""

    # Resolve drop_thinking: if any message has tools defined, don't drop thinking
    effective_drop_thinking = drop_thinking
    if any(m.get("tools") for m in full_messages):
        effective_drop_thinking = False

    if thinking_mode == "thinking" and effective_drop_thinking:
        full_messages = _drop_thinking_messages(full_messages)
        num_to_render = len(full_messages) - len(_drop_thinking_messages(context))
        context_len = len(full_messages) - num_to_render
    else:
        num_to_render = len(messages)
        context_len = len(context)

    for idx in range(num_to_render):
        prompt += render_message(
            idx + context_len,
            full_messages,
            thinking_mode=thinking_mode,
            drop_thinking=effective_drop_thinking,
            reasoning_effort=reasoning_effort,
        )

    return prompt


def encode_messages(
    messages: List[Dict[str, Any]],
    thinking_mode: str,
    context: Optional[List[Dict[str, Any]]] = None,
    drop_thinking: bool = True,
    add_default_bos_token: bool = True,
    reasoning_effort: Union[str, int, None] = None,
    return_multi_modal_data: bool = False,
) -> Any:
    """Encode text or multimodal messages into the DeepSeek-V4.1 prompt format.

    Text-only calls return the prompt string. When return_multi_modal_data is
    true, the result is ``(prompt, media_data)``.
    """
    # Validate before merging can discard message-level textual fields.
    for msg in (context or []) + messages:
        _validate_no_image_sp_tokens(msg)

    # Collect images only after tool results are in their final prompt order.
    context = merge_tool_messages(context or [])
    messages = merge_tool_messages(messages)
    messages = sort_tool_results_by_call_order(context + messages)[len(context):]
    processed_context, _ = process_image_messages(context) if context else ([], [])
    processed_messages, images = process_image_messages(messages)
    prompt = _encode_messages_text(
        processed_messages,
        thinking_mode=thinking_mode,
        context=processed_context if processed_context else None,
        drop_thinking=drop_thinking,
        add_default_bos_token=add_default_bos_token,
        reasoning_effort=reasoning_effort,
    )
    if return_multi_modal_data:
        return prompt, {"images": images}
    return prompt


# ============================================================
# Parsing (Decoding model output)
# ============================================================

def parse_tool_calls(index: int, text: str) -> Tuple[int, Optional[str], List[Dict[str, str]]]:
    """
    Parse V4.1 DSML tool calls from text starting at the given index.

    Returns:
        Tuple of (new_index, last_stop_token, list_of_tool_call_dicts).
    """
    tool_calls: List[Dict[str, Any]] = []
    stop_token = None
    tool_calls_end_token = f"</{dsml_token}{tool_calls_block_name}>"
    tool_call_start_token = f"<{dsml_token}{tool_call_tag_name}"
    tool_call_end_token = f"</{dsml_token}{tool_call_tag_name}"
    tool_parameter_start_token = f"<{dsml_token}{tool_parameter_tag_name}"
    tool_parameter_end_token = f"/{dsml_token}{tool_parameter_tag_name}"

    while index < len(text):
        index, _, stop_token = _read_until_stop(index, text, [tool_call_start_token, tool_calls_end_token])
        if _ != ">\n":
            raise ValueError(f"Tool call format error: expected '>\\n' but got '{_}'")

        if stop_token == tool_calls_end_token:
            break

        if stop_token is None:
            raise ValueError("Missing special token in tool calls")

        index, tool_name_content, stop_token = _read_until_stop(index, text, [tool_parameter_start_token, tool_call_end_token])

        p_tool_name = re.findall(r'^\s*name="(.*?)">\n$', tool_name_content, flags=re.DOTALL)
        if len(p_tool_name) != 1:
            raise ValueError(f"Tool name format error: '{tool_name_content}'")
        tool_name = p_tool_name[0]

        tool_args: Dict[str, Tuple[str, str]] = {}
        while stop_token == tool_parameter_start_token:
            index, param_content, stop_token = _read_until_stop(index, text, [tool_parameter_end_token])

            param_kv = re.findall(r'^ name="(.*?)" string="(true|false)">(.*?)<$', param_content, flags=re.DOTALL)
            if len(param_kv) != 1:
                raise ValueError(f"Parameter format error: '{param_content}'")
            param_name, string, param_value = param_kv[0]

            if param_name in tool_args:
                raise ValueError(f"Duplicate parameter name: '{param_name}'")
            tool_args[param_name] = (param_value, string)

            index, content, stop_token = _read_until_stop(index, text, [tool_parameter_start_token, tool_call_end_token])
            if content != ">\n":
                raise ValueError(f"Parameter format error: expected '>\\n' but got '{content}'")

        tool_call = decode_dsml_to_arguments(tool_name=tool_name, tool_args=tool_args)
        tool_calls.append(tool_call)

    return index, stop_token, tool_calls


def parse_message_from_completion_text(text: str, thinking_mode: str) -> Dict[str, Any]:
    """
    Parse a model completion text into a structured assistant message (V4.1 format).

    Returns:
        Dict with keys: "role", "content", "reasoning_content", "tool_calls".
        tool_calls are in OpenAI format.
    """
    summary_content, reasoning_content, tool_calls = "", "", []
    index, stop_token = 0, None
    tool_calls_start_token = f"\n\n<{dsml_token}{tool_calls_block_name}"

    is_thinking = thinking_mode == "thinking"
    is_tool_calling = False

    if is_thinking:
        index, content_delta, stop_token = _read_until_stop(index, text, [thinking_end_token, tool_calls_start_token])
        reasoning_content = content_delta
        assert stop_token == thinking_end_token, "Invalid thinking format: missing </think>"

    index, content_delta, stop_token = _read_until_stop(index, text, [eos_token, tool_calls_start_token])
    summary_content = content_delta
    if stop_token == tool_calls_start_token:
        is_tool_calling = True
    else:
        assert stop_token == eos_token, "Invalid format: missing EOS token"

    if is_tool_calling:
        index, stop_token, tool_calls = parse_tool_calls(index, text)

        index, tool_ends_text, stop_token = _read_until_stop(index, text, [eos_token])
        assert not tool_ends_text, "Unexpected content after tool calls"

    assert len(text) == index and stop_token in [eos_token, None], "Unexpected content at end"

    for sp_token in [bos_token, eos_token, thinking_start_token, thinking_end_token, dsml_token]:
        assert sp_token not in summary_content and sp_token not in reasoning_content, \
            f"Unexpected special token '{sp_token}' in content"

    return {
        "role": "assistant",
        "content": summary_content,
        "reasoning_content": reasoning_content,
        "tool_calls": tool_calls_to_openai_format(tool_calls)
    }

# ===== file: serve/models/qwen4exp.py =====
"""The Qwen dialect: the model's Jinja chat template and `<tool_call><function=...>` calls."""
from __future__ import annotations

from serve import frontend
from serve.models import Dialect
from serve.util.text import IM_END


class Qwen4Exp(Dialect):
    name = "qwen4exp"
    stop_texts = (IM_END, "<|endoftext|>")
    reasoning_close = "\n</think>\n\n"
    effort_turn = "<|im_start|>system\n{}<|im_end|>\n"

    def template(self, path):
        return frontend.ChatTemplate(path)

    def parser(self, thinking, tools, stream_tools=True):
        return frontend.OutputParser(thinking=thinking, tools=tools, stream_tools=stream_tools)

    def literal_tags(self, controls):
        return frontend.literal_tags(controls)

    def forced_opening(self, name):
        return frontend.CALL_START + (f"\n<function={name}>\n" if name else "\n<function=")

# ===== file: serve/prompt.py =====
"""A prompt sent to the engine as text: the engine tokenizes it (GENT) instead of the server.

`PromptText` stands where the id list used to: `len()` is the token count once the engine has said `PT` (before that
an estimate, good for a status line only), `ids()` tokenizes in Python for the paths that need the ids (a second
reply segment, engines that take ids, batch slots), and `+` gives the id list those paths extend.
"""
from __future__ import annotations

import re

CTX_SLACK = 8               # the engine rejects prompt + max_new + 8 > context


def room_error(n: int, ctx: int) -> ValueError:
    return ValueError("request exceeds the context window: "
                      f"prompt ({n} tokens) leaves no room to answer in the context "
                      f"({ctx}); requests are never truncated")


def overflow_error(n: int, max_new: int, ctx: int) -> ValueError:
    room = ctx - CTX_SLACK - n
    return ValueError("request exceeds the context window: "
                      f"prompt ({n} tokens) + max tokens ({max_new}) exceeds the context "
                      f"({ctx}); requests are never truncated. Send a smaller "
                      f"max_tokens (at most {max(0, room)} here), or start the server with "
                      "--fit-max-tokens (or set \"fit-max-tokens\": true in the \"server\" section of "
                      "the --config file) to shorten it to the room left")


_OVERFLOW = re.compile(r"prompt \((\d+) tokens\) \+ max_new \((\d+)\) exceeds the context \((\d+)\)")
_NO_ROOM = re.compile(r"prompt \((\d+) tokens\) leaves no room to answer in the context \((\d+)\)")


class PromptText:
    def __init__(self, tokenizer, text: str, plain=(), tail: str = "", clip: bool = False):
        self.tok = tokenizer
        self.text = text            # what the engine tokenizes: the rendered prompt, then `tail`
        self.body = text[:len(text) - len(tail)] if tail else text
        self.tail = tail            # the forced opening of a call, encoded on its own like the engine does
        self.plain = list(plain)    # character spans of `body` where special-token text stays text
        self.clip = clip            # max_new is a cap: cut to the room the context leaves
        self.n: int | None = None   # prompt tokens, from the engine's PT line
        self.max_new: int | None = None   # the answer cap the engine settled on
        self._ids: list[int] | None = None

    def ids(self) -> list[int]:
        if self._ids is None:
            ids = self.tok.encode(self.body, parse_special=True, plain=self.plain) if self.plain \
                else self.tok.encode(self.body, parse_special=True)
            if self.tail:
                ids = ids + self.tok.encode(self.tail, parse_special=True)
            self._ids = ids
            self.n = len(ids)
        return self._ids

    def __len__(self) -> int:
        if self.n is not None:
            return self.n
        return max(1, len(self.text) // 4)     # not yet counted: a display estimate

    def __add__(self, other):
        return self.ids() + list(other)

    def byte_spans(self) -> list[tuple[int, int]]:
        """The plain spans as byte offsets into the UTF-8 text."""
        return [(len(self.body[:a].encode("utf-8")), len(self.body[:b].encode("utf-8"))) for a, b in self.plain]

    def set_counted(self, n: int, max_new: int) -> None:
        self.n, self.max_new = n, max_new

    def resolve(self, max_new: int, ctx: int) -> tuple[list[int], int]:
        """(ids, max_new) for an engine that takes ids: the answer cap the engine would have settled on."""
        ids = self.ids()
        room = ctx - CTX_SLACK - len(ids)
        if self.clip:
            if room < 1:
                raise room_error(len(ids), ctx)
            max_new = min(max_new, room)
        elif max_new > room:
            raise overflow_error(len(ids), max_new, ctx)
        self.max_new = max_new
        return ids, max_new

    def explain(self, err: str) -> str:
        """The engine's ERR text in the server's words, as the id path would have raised it."""
        m = _OVERFLOW.search(err)
        if m:
            self.n = int(m.group(1))
            return str(overflow_error(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        m = _NO_ROOM.search(err)
        if m:
            self.n = int(m.group(1))
            return str(room_error(int(m.group(1)), int(m.group(2))))
        return err


def tokens_known(prompt) -> bool:
    """False for a text prompt the engine has not counted yet."""
    return not isinstance(prompt, PromptText) or prompt.n is not None


def primed(events, prompt):
    """`events` (a generator) advanced until the engine has counted a text prompt, so an error the count finds (a prompt
    over the context) is raised before a stream's headers go out, as it was when the server counted the tokens. The
    items read on the way are replayed first. A prompt that is not text needs no wait."""
    if not isinstance(prompt, PromptText) or tokens_known(prompt):
        return events
    held, later = [], None
    try:
        while not tokens_known(prompt):
            held.append(next(events))
    except StopIteration:
        pass
    except ValueError:
        events.close()
        raise
    except BaseException as e:      # a dead engine and the like reach the stream as they always did
        later = e

    def replay():
        try:
            yield from held
            if later is not None:
                raise later
            yield from events
        finally:
            events.close()
    return replay()

# ===== file: serve/router.py =====
"""Router: one Service per configured model, one model loaded at a time, requests picking theirs by name.

A swap ends one engine process and starts the next. A request holds a turn from before its model is used until its reply
ends. Turns follow one rule, so mixed traffic cannot starve or thrash:
  * A request for the loaded model goes at once while nothing waits. Once a request for another model waits, later
    arrivals queue behind it, whatever their model.
  * The queue's head decides the next model. Its swap starts when every running request has finished.
  * A loaded model takes all the requests queued for it at that moment, in one batch.
  * A failed load fails that batch with the load's error and the queue moves on; the next request for it tries again.
"""
from __future__ import annotations

import collections
import threading
import time

from serve.engine.errors import EngineDied, EngineStartError, EngineStuck, GpuBusy

WAITING, GRANTED, FAILED, LEFT = "waiting", "granted", "failed", "left"


class Turn:
    """A request's place: waiting in the queue, then granted until it leaves. A preload holds nothing once granted."""
    __slots__ = ("svc", "state", "error", "holds", "at")

    def __init__(self, svc, holds: bool = True):
        self.svc, self.holds, self.state, self.error, self.at = svc, holds, WAITING, None, time.time()


def load_failure(name: str, e: Exception) -> Exception:
    """The error a waiting request gets for a failed load: the kind the API maps to its status, naming the model."""
    text = f"{name} did not load: {e}"
    if isinstance(e, EngineStartError):
        return EngineStartError(text, e.log)
    return (type(e) if isinstance(e, (GpuBusy, EngineStuck)) else EngineDied)(text)


class Router:
    def __init__(self, services: list):
        self.services = list(services)
        self.front = self.services[0]                   # holds the settings every model shares: key, hosts, image sources
        self.current = self.front if len(self.services) == 1 else None
        self.cv = threading.Condition()
        self.queue: collections.deque[Turn] = collections.deque()
        self.running = 0                                # granted requests that have not left
        self.swap = None                                # the swap under way: {"from", "to", "phase", "since"}
        self.swaps = 0                                  # swaps done since start
        self.errors: dict[int, str] = {}                # id(service) -> its last load's failure, until one succeeds
        self.stopping = False
        self.worker = None

    @property
    def multi(self) -> bool:
        return len(self.services) > 1

    def service_for(self, name) -> object | None:
        """The service answering to `name`: a listed name or a model file's name."""
        if isinstance(name, str):
            for svc in self.services:
                if name in svc.served_names():
                    return svc
        return None

    def shown(self):
        """The service the status routes describe: the swap's target, else the loaded model, else the first."""
        with self.cv:
            target = self.swap["svc"] if self.swap else self.current
        return target or self.front

    def models(self) -> list[dict]:
        """What /v1/models lists: every configured model, loaded or not."""
        out = []
        for svc in self.services:
            model = svc.model_entry()
            why = self.errors.get(id(svc))
            if why and model["status"]["value"] == "unloaded":
                model["status"] = {"value": "failed", "error": why}
            out.append(model)
        return out

    def swap_status(self) -> dict | None:
        """The swap under way, for /metrics and /health."""
        with self.cv:
            s = self.swap
            if not s:
                return None
            return {"from": s["from"], "to": s["svc"].model, "phase": s["phase"],
                    "elapsed_s": round(time.time() - s["since"], 1), "waiting": len(self.queue)}

    def metrics(self, svc) -> dict:
        """GET /metrics for `svc`, with the requests waiting for any model's turn and every model's row."""
        m = svc.metrics()
        now = time.time()
        with self.cv:
            waiting = [{"api": None, "model": t.svc.model, "prompt_tokens": None, "waited_s": round(now - t.at, 1)}
                       for t in self.queue if t.holds]
        m["live"]["queued"] += len(waiting)
        m["live"]["queue"] = m["live"]["queue"] + waiting
        return {**m, "models": self.models(), "swap": self.swap_status()}

    def join(self, svc) -> Turn:
        """Take a turn for `svc`: granted at once when its model is loaded and nothing waits, else queued."""
        turn = Turn(svc)
        with self.cv:
            if self.stopping:
                turn.state, turn.error = FAILED, EngineDied("the server is stopping")
            elif self.current is svc and not self.swap and not self.queue:
                turn.state = GRANTED
                self.running += 1
            else:
                self.queue.append(turn)
                self._start_worker()
                self.cv.notify_all()
        return turn

    def preload(self, svc) -> None:
        """Load `svc` through the queue, with no request behind it."""
        with self.cv:
            self.queue.append(Turn(svc, holds=False))
            self._start_worker()
            self.cv.notify_all()

    def wait(self, turn: Turn, timeout: float | None) -> bool:
        """Whether `turn` is granted after up to `timeout` seconds; raises the load's error when it failed."""
        with self.cv:
            self.cv.wait_for(lambda: turn.state != WAITING, timeout)
            if turn.state == FAILED:
                raise turn.error
            return turn.state == GRANTED

    def leave(self, turn: Turn) -> None:
        """The request ended, or left while queued."""
        with self.cv:
            if turn.state == GRANTED and turn.holds:
                self.running -= 1
            elif turn.state == WAITING:
                self.queue.remove(turn)
            turn.state = LEFT
            self.cv.notify_all()

    def stop(self) -> None:
        """Shutdown: the waiting requests get an error and no swap starts."""
        with self.cv:
            self.stopping = True
            for turn in self.queue:
                turn.state, turn.error = FAILED, EngineDied("the server is stopping")
            self.queue.clear()
            self.cv.notify_all()
        for svc in self.services:
            svc.stop()

    def vram(self, reserve_mib):
        """POST /v1/vram: the reserve is the machine's, so a model loaded later applies it too."""
        active = self.shown()
        for svc in self.services:
            if svc is not active:
                svc.vram_reserve = reserve_mib
        return active.vram(reserve_mib)

    def start_telemetry(self) -> None:
        """One hardware sampler over the cards of every model; the speeds are the shown model's."""
        if getattr(self.front, "telemetry", None) is None:
            from serve.util.telemetry import Telemetry
            cards = list(dict.fromkeys(i for svc in self.services for i in (getattr(svc, "gpu_indices", None) or [0])))
            sampler = Telemetry(extra=lambda: self.shown().telemetry_extra(), gpu_indices=cards)
            for svc in self.services:
                svc.telemetry = sampler

    def _start_worker(self) -> None:
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, daemon=True, name="router")
            self.worker.start()

    def _grant_batch(self, svc) -> None:
        """Every queued request for `svc` goes now. Caller holds the lock."""
        for turn in [t for t in self.queue if t.svc is svc]:
            self.queue.remove(turn)
            turn.state = GRANTED
            self.running += bool(turn.holds)
        self.cv.notify_all()

    def _fail_batch(self, svc, error: Exception) -> None:
        for turn in [t for t in self.queue if t.svc is svc]:
            self.queue.remove(turn)
            turn.state, turn.error = FAILED, error
        self.cv.notify_all()

    def _run(self) -> None:
        while True:
            with self.cv:
                while True:
                    if self.stopping:
                        return
                    head = self.queue[0].svc if self.queue else None
                    if head is not None and head is self.current:
                        self._grant_batch(head)
                    elif head is not None and self.running == 0:
                        break
                    else:
                        self.cv.wait()
                self.swap = {"svc": head, "from": self.current.model if self.current else None,
                             "phase": "unloading" if self.current else "loading", "since": time.time()}
                old, waiting = self.current, len(self.queue)
            failure = self._swap(old, head, waiting)
            with self.cv:
                if failure is None:
                    self.current = head
                    self.swaps += 1
                    self.errors.pop(id(head), None)
                    self._grant_batch(head)
                else:
                    self.errors[id(head)] = str(failure)
                    self._fail_batch(head, load_failure(head.model, failure))
                self.swap = None
                self.cv.notify_all()

    def _swap(self, old, new, waiting: int) -> Exception | None:
        """End `old`, start `new`; the failure, or None."""
        t = time.time()
        new.load_kind, new.loading = "weights", True    # the status routes show the load from now on
        if hasattr(new.engine, "load_status"):
            new.engine.load_status = {}
        print(f"[server] {f'swap {old.model} ->' if old else 'loading'} {new.model} ({waiting} waiting)", flush=True)
        try:
            if old is not None:
                old.swap_out()                          # at its QUIT the engine spills its conversations to disk
                with self.cv:
                    self.current = None
                    self.swap["phase"] = "loading"
            new.load()
        except Exception as e:  # noqa: BLE001 - any failure ends in the batch's error, and the server stays up for the others
            print(f"[server] error: {new.model} did not load: {e}", flush=True)
            return e
        finally:
            new.loading = False
        print(f"[server] swap: {new.model} ready in {time.time() - t:.1f} s", flush=True)
        return None

# ===== file: serve/security.py =====
"""Host and origin checks, the allowed names, and the exposure test for a server without an API key."""
from __future__ import annotations

import hmac
import ipaddress
import socket


LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


# proxies announce themselves with these; ordinary clients send none of them
FORWARDING_HEADERS = frozenset((
    "forwarded", "via", "x-real-ip", "x-client-ip", "client-ip", "true-client-ip", "x-cluster-client-ip",
    "cf-connecting-ip", "fastly-client-ip", "x-originating-ip", "x-remote-ip", "x-remote-addr", "x-host",
    "x-proxyuser-ip", "wl-proxy-client-ip", "proxy-connection"))
FORWARDING_PREFIXES = ("x-forwarded-", "x-original-", "x-envoy-", "x-amzn-", "x-azure-", "x-appengine-", "cf-",
                       "tailscale-")


def exposed_without_key(host: str, api_key: str) -> bool:
    """True when the server listens beyond this PC without an API key."""
    return host not in LOOPBACK_NAMES and not api_key


def api_key_of(value) -> str:
    """The key as a client can send it. An HTTP header loses the spaces and line ends around its value, so a key kept
    with them would match no request. A value that is only such characters raises ValueError: it never means "no key"."""
    key = "" if value is None else str(value)
    if key and not key.strip():
        raise ValueError("an API key was given but it is empty")
    return key.strip()


def key_matches(given: str, key: str) -> bool:
    """`given` is a header's value as http.server read it, each byte one character. A key with characters outside
    ASCII arrives as UTF-8 from most clients and as Latin-1 from some; the right key passes in both forms.
    Constant-time."""
    raw = given.encode("latin-1", "replace")
    ok = hmac.compare_digest(raw, key.encode())
    try:
        ok |= hmac.compare_digest(raw, key.encode("latin-1"))
    except UnicodeEncodeError:
        pass
    return ok


def lan_addresses() -> list[str]:
    """This PC's non-loopback, non-link-local IPv4 addresses."""
    import socket
    first, ips = None, set()
    try:                                                # default-route address; UDP connect sends nothing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            first = s.getsockname()[0]
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ok = lambda ip: ip and not ip.startswith(("127.", "169.254.", "0."))
    return ([first] if ok(first) else []) + sorted(ip for ip in ips if ok(ip) and ip != first)


def host_name(value) -> str:
    """The lowercase name of a Host header or origin host[:port] ("[::1]:8095" -> "::1"); "" when malformed."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        name, sep, rest = v[1:].partition("]")
        return name if sep and (not rest or (rest[:1] == ":" and rest[1:].isdigit())) else ""
    if v.count(":") == 1:
        v, port = v.split(":")
        if not port.isdigit():
            return ""
    elif ":" in v:                                       # a bare IPv6 address
        return v
    v = v.rstrip(".")
    return v if v and all(c.isalnum() or c in "-._" for c in v) else ""


def is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _name_in(name: str, names) -> bool:
    """`name` is one of `names`, or below an entry that starts with a dot (".example.com")."""
    return name in names or any(n.startswith(".") and (name.endswith(n) or name == n[1:]) for n in names)


def allowed_hosts_of(value) -> list[str]:
    """The --allowed-host names: host names, "*" or ".example.com" (it and below). Scheme, port and path are
    dropped; a bad entry raises ValueError."""
    items = [] if value in (None, "") else [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise ValueError("allowed_hosts: expected a host name or a list of them")
    out = []
    for raw in items:
        x = raw.strip().lower()
        if x == "*":
            out.append(x)
            continue
        x = x.split("://", 1)[-1].split("/", 1)[0]
        dot = x.startswith(".")
        name = host_name(x[1:] if dot else x)
        if not name:
            raise ValueError(f"allowed_hosts: {raw!r} is not a host name")
        out.append("." + name if dot else name)
    return list(dict.fromkeys(out))


def host_names_for(bind_host: str, allowed_hosts=(), trusted_origins=()) -> set[str]:
    """Every non-IP name this server answers to: localhost, the bind address, allowed hosts, trusted origins' hosts
    and, when listening beyond this PC, its name and LAN addresses (the Origin check trusts no IP)."""
    names = set(LOOPBACK_NAMES)
    bind = host_name(bind_host)
    if bind and bind not in ("0.0.0.0", "::"):
        names.add(bind)
    if bind not in LOOPBACK_NAMES:
        try:
            pc = socket.gethostname().lower()
            names.update((pc, pc + ".local"))
        except OSError:
            pass
        names.update(("host.docker.internal", *lan_addresses()))
    names.update(x for x in allowed_hosts if x != "*")
    names.update(filter(None, (host_name(o.split("://", 1)[-1]) for o in trusted_origins)))
    return names


def host_allowed(host, names, any_host=False) -> bool:
    """DNS-rebinding check: only known names pass, since a rebinding page carries its own site's name in Host.
    IPs, "*.localhost" and a missing Host (browsers always send one) pass too."""
    if any_host or not (host or "").strip():
        return True
    name = host_name(host)
    return bool(name) and (is_ip(name) or name == "localhost" or name.endswith(".localhost")
                           or _name_in(name, names))


def origin_allowed(origin: str, host, names, origins=()) -> bool:
    """Whether an Origin may use the model without an API key: this server's own page, a known name on any port,
    or a listed origin. Unlike the Host check no IP passes on trust, and "null" fails (any site can send it);
    non-http(s) schemes pass since no web site can send them."""
    origin = (origin or "").strip().rstrip("/")
    if origin in origins or "*" in origins:             # "*": every page
        return True
    scheme, sep, rest = origin.lower().partition("://")
    if not sep or not scheme:
        return False
    if scheme not in ("http", "https"):
        return True
    if host and rest == host.strip().lower():
        return True
    name = host_name(rest)
    return bool(name) and (name in LOOPBACK_NAMES or name.endswith(".localhost") or _name_in(name, names))


def peer_of(client_address) -> str:
    """The socket peer's address as text; "" when there is none."""
    try:
        return str(client_address[0])
    except (TypeError, IndexError, KeyError):
        return ""


def loopback_peer(address) -> bool:
    """127.0.0.0/8, ::1 or an IPv4-mapped ::ffff:127.x; anything else, this machine's LAN address too, is not."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback


def _loopback_host(host) -> bool:
    """A Host value only a client of this machine sends. Any other name fails, which also covers a rebinding page."""
    name = host_name(host)
    return name == "localhost" or loopback_peer(name) or name in ("0.0.0.0", "::")


def remote_reason(client_address, headers) -> str:
    """"" when a request is provably from this machine, else why not. Only the socket peer can make a request local;
    headers can only make it remote: a proxy's forwarding headers, a Host that is not a loopback name, any Origin or a
    Fetch Metadata value other than `none`, which a desktop app's main process sends. No web page counts as local, the
    server's own neither. Unparsable headers count too: the parser drops whatever follows a malformed line, which can
    hide a proxy's own."""
    if not peer_of(client_address):
        return "no-peer"
    if not loopback_peer(peer_of(client_address)):
        return "peer"
    if headers is None or headers.defects or headers.get_payload():
        return "headers"
    names = [k.lower().replace("_", "-") for k in headers.keys()]
    if any(n in FORWARDING_HEADERS or n.startswith(FORWARDING_PREFIXES) for n in names):
        return "forwarded"
    hosts = headers.get_all("Host") or []
    if len(hosts) != 1 or not _loopback_host(hosts[0]):
        return "host"
    if headers.get_all("Origin") or any(v.strip().lower() != "none" for v in headers.get_all("Sec-Fetch-Site") or []):
        return "origin"
    return ""


def origins_of(value, key: str, wildcard: bool) -> list[str]:
    """An origin or list of origins (scheme://host[:port], no path); "*" only when `wildcard`; a bad entry raises."""
    if value is None or value == "" or value == []:
        return []
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise SystemExit(f"[server] error: {key}: expected an origin or a list of origins")
    out = []
    for x in items:
        x = x.strip().rstrip("/")
        if x == "*" and wildcard:
            out.append(x)
            continue
        scheme, sep, rest = x.partition("://")
        if scheme not in ("http", "https") or not sep or not rest or "/" in rest or "*" in rest:
            raise SystemExit(f"[server] error: {key}: {x!r} is not an origin like https://chat.example.com"
                             + ("" if wildcard else " (no wildcards)"))
        out.append(x)
    return out

# ===== file: serve/server.py =====
"""OpenAI, Anthropic and Responses endpoints over engines that map token ids to tokens, one model loaded at a time.

    basalt serve --model FILE.basalt [--alias NAME] [--host H] [--port P] [--gpu 0,1] [--api-key KEY] ...
    python -m serve.server --model FILE.basalt --engine-bin bin/basalt ...
    python -m serve.server --config config.json          (each entry of its "models" is one model)
    python -m serve.server --mock-engine --port 8080

The engine boundary is `Engine.generate(prompt_ids, max_new, sampling, cancel)` -> token ids. ProcessEngine keeps
one `basalt --serve` process resident; MockEngine is a scripted stand-in for tests. Prompts that do not fit the
context are rejected with a 400, never truncated.
"""
from __future__ import annotations

import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # also runnable as a script
from serve import ROOT, service  # noqa: E402
from serve.config import (build_parser, check_engine_flags,  # noqa: E402
                          engine_flags_of, grammar_words, model_line, model_settings, sampling_defaults_from_config,
                          sampling_items, shared_line, slot_share, start_items, with_spec)
from serve.engine import process as engine_process  # noqa: E402
from serve.engine.errors import EngineStartError  # noqa: E402
from serve.engine.image_source import image_sources_of  # noqa: E402
from serve.engine.flags import flag_index, flag_value  # noqa: E402
from serve.engine.mock import MockEngine  # noqa: E402
from serve.engine.process import ProcessEngine  # noqa: E402
from serve.engine.vision import (EngineVision, Vision, check_gpu, child_env, engine_args, flag_refused, gpu_card_name,  # noqa: E402
                                 gpu_text, gpus_in_play, model_plan, vision_card, vision_env, vision_item)
from serve.models import dialect_for  # noqa: E402
from serve.handler import Server, serve  # noqa: E402
from serve.security import (LOOPBACK_NAMES, allowed_hosts_of, api_key_of, exposed_without_key,  # noqa: E402
                            lan_addresses, origins_of)
from serve.router import Router  # noqa: E402
from serve.service import Service  # noqa: E402
from serve.util.logbuf import ColorStream, LogBuffer, TeeStream, tail_file  # noqa: E402
from serve.util.console import LEGEND, tagged  # noqa: E402
from serve.util.text import ByteTokenizer  # noqa: E402


SWAP_START_TRIES = 2      # a swap's load: a second try for VRAM still being released, not a third for clients to wait on


def fail(msg: str) -> None:
    """Print a startup error and exit with status 2."""
    print(f"[server] error: {msg}", file=sys.stderr)
    raise SystemExit(2)


def warn_tight_ram(arena_mib) -> None:
    """Warn at start when less than ~6 GB of RAM is left beside the expert arena (INFO arena_mib)."""
    if not isinstance(arena_mib, int) or arena_mib <= 0:
        return
    try:
        import psutil
        total = psutil.virtual_memory().total
    except Exception:  # noqa: BLE001 - psutil is optional here
        return
    left = total / 2**30 - arena_mib / 1024
    if left < 6:
        print(tagged("warning", f"low RAM: {left:.1f} of {total / 2**30:.0f} GB left after experts"), flush=True)


def grammar_entry(on: bool, cli: list[str], used: list[str]) -> tuple[bool, str] | None:
    """(on, origin) for the startup line when the command line or the config file set grammar; None for the default."""
    names = ("--grammar", "--no-grammar")
    origin = "cli" if any(w in names for w in cli) else "config" if any(w in names for w in used) else ""
    return (on, origin) if origin else None


def open_when_ready(svc, url, now=False, opener=None):
    """Open the page once, after the first weights load; at once when no load is under way (mock, lazy)."""
    if opener is None:
        import webbrowser
        opener = webbrowser.open
    if now:
        opener(url)
        return
    done, previous = [], svc.on_loaded

    def loaded():
        if previous is not None:
            previous()
        if not done:
            done.append(1)
            opener(url)
    svc.on_loaded = loaded


def check_model(a, file_engine: list[str], cli_engine: list[str], used_argv: list[str], known) -> dict:
    """What one configured model's file and flags imply, by the engine's own rules and without loading anything; fail()
    for what stops the start."""
    cfg = {}                                            # what the model file implies
    lines = cfg["start_lines"] = []                     # printed with the model's other lines once all are checked
    try:
        check_gpu(a.gpu)
    except ValueError:
        fail(f"--gpu {gpu_text(a.gpu)}: expected numbers like 0 or 0,1, or auto")
    if not a.mock_engine and not (a.model and a.engine_bin):
        fail("the server needs --model and the engine binary: run it as basalt serve --config FILE (or --mock-engine)")
    if (file_engine or cli_engine) and not a.model:
        fail("engine flags need --model")
    pack = bool(a.model) and not (a.mock_engine and a.config)   # the scripted engine of a config file reads no model files
    if pack:
        if not a.engine_bin:
            fail("--model needs --engine-bin BINARY")
        from serve.engine import modelfile
        try:
            check_engine_flags(file_engine, known, f"--config {a.config}")
            check_engine_flags(cli_engine, known, "command line")
        except ValueError as e:
            fail(str(e))
        mode = a.vision
        spec = modelfile.ModelSpec(a.model, alias=a.alias, gpu=a.gpu,
                                   vision=False if a.mock_engine or mode == "off" else mode,
                                   engine_words=tuple(file_engine + cli_engine))
        try:
            cfg.update(modelfile.model_config(spec, a.engine_bin))
        except (OSError, ValueError, KeyError) as e:
            fail(f"{a.model}: {e}")
        if not a.mock_engine:
            # the engine's own choice and its own flag rules, printed once here; the engine then sees only these cards,
            # main first
            try:
                plan = model_plan(a.engine_bin, a.model, spec.gpu, list(spec.engine_words))
            except ValueError as e:
                fail(f"{a.model}: {e}")
            card = vision_card(mode)
            if card is not None:                       # a numbered sidecar card: the engine's sm_120 rule is the
                if gpu_card_name(card) is None:        # engine's; the sidecar is its own build and says whether
                    fail(f"vision: GPU {card} is not found by nvidia-smi")   # the sidecar checks the card itself
            for note in plan.get("notes") or []:
                lines.append(f"[server] {note}")
            if plan.get("warning"):
                lines.append(f"[server] warning: {plan['warning']}")
            cfg["gpu"] = plan["devices"]
            cfg["spec"] = plan.get("spec")
            if getattr(a, "split_default", False) and plan.get("max_context"):
                # the engine's default context, shared by the slots
                a.shared_context = int(plan["max_context"])
                part = ["--max-context", str(slot_share(a.shared_context, a.parallel))]
                cfg["args"] += part
                cli_engine += part
            cfg["gpu_smi"] = [c.get("smi", -1) for c in plan.get("cards") or []]
            cfg["args"] += ["--gpu", ",".join(str(i) for i in range(len(plan.get("cards") or [])))]
    for flag in a.skipped:
        lines.append(tagged("engine", f"{flag[2:]} from the top level: this model has no such flag, skipped"))
    cfg["items"] = start_items(file_engine, cli_engine,
                               (a.shared_context, a.parallel) if getattr(a, "shared_context", None) else None,
                               grammar_entry(a.grammar, sys.argv[1:], used_argv), a.debug) if pack and not a.mock_engine else []
    cfg["items"] = with_spec(cfg["items"], cfg.get("spec"))
    if a.alias and not pack:
        cfg["model_name"] = a.alias
    if not cfg.get("model_name") and a.model:
        cfg["model_name"] = Path(a.model).name
    return cfg


def drop_sidecar_vision(cfg: dict) -> None:
    """Only a sidecar encoder holds VRAM before the first request; `dynamic` encodes inside the engine, which the
    request loads first anyway."""
    if not cfg.get("vision") or cfg["vision"].get("mode") == "dynamic":
        return
    print(tagged("warning", "lazy loading drops a sidecar image encoder, vision off; --vision dynamic stays on"), flush=True)
    while (at := flag_index(cfg["args"], "--vision-mmproj")) is not None:
        del cfg["args"][at:at + 2]
    del cfg["vision"]


def build_model(a, cfg: dict, logs, multi: bool, known) -> Service:
    """The Service of one checked model, unloaded. With several models a numbered vision sidecar waits for its model's
    turn too."""
    tokenizer = cfg.get("tokenizer") or a.tokenizer
    tok = ByteTokenizer()
    tpath = Path(tokenizer)
    if not a.mock_engine and not (tpath / "vocab.json").exists():
        fail(f"tokenizer missing: {tpath / 'vocab.json'}")
    if (tpath / "vocab.json").exists():
        import tokenizer as ST
        vocab = json.loads((tpath / "vocab.json").read_text(encoding="utf-8"))
        tokens = [None] * len(vocab)
        for t, i in vocab.items():
            tokens[i] = t
        merges = (tpath / "merges.txt").read_text(encoding="utf-8").split("\n")
        types = json.loads((tpath / "token_type.json").read_text())
        pre = json.loads((tpath / "tokenizer.json").read_text(encoding="utf-8")).get("pre", "qwen35") \
            if (tpath / "tokenizer.json").exists() else "qwen35"
        tok = ST.Tokenizer(tokens, merges, types, pre=pre)
    has_grammar = False
    if not a.mock_engine:
        vision = None
        env = child_env(cfg)
        sampling_defaults = {**sampling_defaults_from_config(cfg), **a.sampling_launch}
        if a.lazy and not multi:
            drop_sidecar_vision(cfg)
        shown = [x for x in cfg["items"] if x not in cfg.get("shared_items", ())]
        shown += sampling_items(sampling_defaults) + [x for x in [vision_item(cfg.get("vision"), cfg)] if x]
        print(model_line(cfg["model_name"], cfg.get("name_width", 0), shown), flush=True)
        for line in cfg["start_lines"]:
            print(line, flush=True)
        if cfg.get("vision"):
            mode = cfg["vision"].get("mode")
            if mode == "dynamic":
                vision = EngineVision(cfg["vision"])
            else:
                vision = Vision(cfg["vision"], log=open(cfg["log"], "a", encoding="utf-8") if cfg.get("log") else None,
                                env=vision_env(env, mode), start=not multi)
        if a.lazy and not multi:
            print(tagged("model", "unloaded, the first request loads it"), flush=True)
        if cfg.get("log"):                              # follow the engine's log file
            threading.Thread(target=tail_file, args=(logs, cfg["log"], "eng"), daemon=True).start()
        else:
            ProcessEngine.stderr_sink = lambda line: logs.add("eng", line)
        has_grammar = known is not None and "--grammar" in known
        if a.grammar and not has_grammar:
            print(tagged("warning", "grammar is on but this engine has no --grammar; requests go without one"), flush=True)
        gwords = grammar_words(known)
        cdir = flag_value(engine_args(cfg), "--conversation-cache-dir")
        cmib = flag_value(engine_args(cfg), "--conversation-cache-disk-mib") or "0"
        if cdir and cmib != "0" and a.debug:
            print(tagged("disk", f"conversations={cdir} disk-mib={cmib}"), flush=True)
        # always built unloaded: the load itself is the lazy path's, started by the caller instead of by a request
        engine = ProcessEngine(cfg["engine_bin"], engine_args(cfg) + gwords, log=cfg.get("log"), env=env, lazy=True)
        if isinstance(vision, EngineVision):
            vision.attach(engine)
    else:
        engine, vision, sampling_defaults = MockEngine(tok, a.script or [
            "Thinking about it.</think>\n\nHello from the mock engine."], batch=a.parallel or 0), None, dict(a.sampling_launch)
    # the model's exported template, else the bundled one
    dialect = dialect_for(cfg.get("arch"))
    tpl = tpath / "chat_template.jinja"
    svc = Service(engine, tok, dialect.template(tpl if tpl.exists() else ROOT / "serve/chat_template.jinja"),
                  model_name=cfg.get("model_name", "basalt"), vision=vision,
                  sampling_defaults=sampling_defaults, fit_max_tokens=a.fit_max_tokens, grammar=a.grammar,
                  grammar_engine=bool(a.mock_engine or has_grammar), dialect=dialect)
    svc.logs = logs
    svc.idle_unload_s = a.idle_unload or 0
    svc.idle_sleep_s = a.idle_sleep or 0
    svc.min_free_vram_mib = a.min_free_vram_mib or 0
    svc.reasoning_budget_tokens = a.reasoning_budget_tokens or 0
    svc.answer_room = a.answer_room
    svc.thinking_loop_tokens = a.thinking_loop_tokens
    svc.gpu_indices = gpus_in_play(cfg, engine_args(cfg) if not a.mock_engine else [])   # the cards /metrics reads
    svc.gpu_index = svc.gpu_indices[0]
    if multi:
        svc.start_tries = SWAP_START_TRIES
    return svc


def check_models(services: list[Service]) -> None:
    """Refuse model names that answer for two entries, and one conversation folder shared by two: each model's chats keep
    their own budget."""
    seen: dict[str, Service] = {}
    folders: dict[str, str] = {}
    for svc in services:
        for name in svc.served_names():
            if seen.setdefault(name, svc) is not svc:
                fail(f"--config: {name!r} names two models: give each its own alias")
        args = getattr(svc.engine, "spawn", (None, []))[1]
        where = flag_value(args, "--conversation-cache-dir")
        if where and (flag_value(args, "--conversation-cache-disk-mib") or "0") != "0" and \
                folders.setdefault(where, svc.model) != svc.model:
            fail(f"--config: {svc.model} and {folders[where]} share the conversation folder {where}: give each its own")


def main() -> int:
    ap = build_parser()
    engine_bin = ap.parse_known_args(sys.argv[1:])[0].engine_bin
    try:
        settings = model_settings(ap, sys.argv[1:], (lambda path, group: flag_refused(engine_bin, path, group))
                                  if engine_bin else None)
    except ValueError as e:
        fail(f"--config: {e}")
    a = settings[0][0]                                  # the options every model shares read the same in each
    multi = len(settings) > 1
    service.DEBUG, engine_process.REQUEST_LINES = a.debug, a.request_lines
    logs = LogBuffer()                                  # this process's and the engine's output
    sys.stdout, sys.stderr = (TeeStream(ColorStream.wrap(sys.stdout), logs, "srv"),
                              TeeStream(ColorStream.wrap(sys.stderr), logs, "srv"))
    print(tagged("legend", LEGEND), flush=True)          # first line, so the key precedes every value
    known = engine_flags_of(a.engine_bin) if a.engine_bin else None
    cfgs = [check_model(m, file_engine, cli_engine, used_argv, known)
            for m, used_argv, file_engine, cli_engine in settings]
    if (a.image_url_host or a.image_dir) and not a.mock_engine and all(m[0].vision == "off" for m in settings):
        fail("--image-url-host and --image-dir need --vision (image input is off)")
    try:
        image_sources = image_sources_of(a.image_url_host, a.image_dir, a.image_remote_only)
    except ValueError as e:
        fail(str(e))
    try:                                                # fail fast if the port is taken
        Server((a.host, a.port), BaseHTTPRequestHandler).server_close()
    except OSError:
        fail(f"port {a.port} is in use (--port N)")
    width = max(len(c.get("model_name") or "") for c in cfgs) + 1
    shared = [x for x in cfgs[0].get("items", []) if all(x in c.get("items", []) for c in cfgs[1:])] if multi else []
    line, shared_sleep = shared_line(shared, [m[0].idle_sleep for m in settings])
    if line:
        print(tagged("engine", line), flush=True)
    for c in cfgs:
        c["shared_items"], c["name_width"] = shared, width
    services = [build_model(m[0], cfg, logs, multi, known) for m, cfg in zip(settings, cfgs)]
    check_models(services)
    router = Router(services)
    svc = router.front
    svc.image_sources = image_sources
    if image_sources.url_hosts or image_sources.dirs or image_sources.remote_only:
        print(tagged("images", f"from other machines{' and this one' if image_sources.remote_only else ''}: "
                               f"url-hosts={','.join(a.image_url_host) or '-'} "
                               f"dirs={','.join(image_sources.dirs) or '-'}"), flush=True)
    used_argv = settings[0][1]
    if any(x == "--api-key" and i + 1 < len(used_argv) and not used_argv[i + 1].strip() or x.strip() == "--api-key="
           for i, x in enumerate(used_argv)):
        # an empty key would silently disable authentication
        print("[server] error: --api-key is empty (give a key, or leave it out)", file=sys.stderr)
        return 2
    if a.max_payload_mib < 1:
        fail(f"--max-payload-mib {a.max_payload_mib} must be at least 1")
    svc.max_payload = a.max_payload_mib << 20
    try:
        svc.api_key = api_key_of(a.api_key)
    except ValueError as e:
        print(f"[server] error: {e}: give a key, or leave --api-key out", file=sys.stderr)
        return 2
    try:
        svc.cors_origins = origins_of(a.cors_origin, "--cors-origin", wildcard=True)
        svc.allowed_hosts = allowed_hosts_of(a.allowed_host)
    except (ValueError, SystemExit) as e:
        fail(str(e).removeprefix("[server] error: "))
    if svc.allowed_hosts:
        print(tagged("warning", "Host check off, any name is accepted (--allowed-host *)") if "*" in svc.allowed_hosts
              else tagged("hosts", ",".join(svc.allowed_hosts)), flush=True)
    if svc.cors_origins:
        print(tagged("cors", ",".join(svc.cors_origins)), flush=True)
        if "*" in svc.cors_origins and not svc.api_key:
            print(tagged("warning", "any web page can use the model (--api-key KEY)"), flush=True)
    httpd = serve(router, host=a.host, port=a.port)
    here = "127.0.0.1" if a.host in ("0.0.0.0", "", "::") else a.host

    def announcer(s: Service, m):
        first_load = [True]

        def announce():
            """After every weights load: the slot table once, then the weights line (no URL)."""
            engine = s.engine
            if first_load[0]:
                first_load[0] = False
                if not a.mock_engine:
                    warn_tight_ram(engine.info.get("arena_mib"))
                if getattr(engine, "batch", 0):
                    print(tagged("slots", f"par={engine.batch} ctx/slot={engine.max_context}"
                                 + (" (slot-full-context)" if m.slot_full_context else "") + ", others queue"), flush=True)
            print(tagged("weights", f"loaded{f' {s.model}' if multi else ''}, ctx={engine.max_context} "
                                    f"images={'on' if s.vision else 'off'}"), flush=True)
        return announce

    for s, m in zip(services, (x[0] for x in settings)):
        s.start_idle_unload()
        s.start_idle_sleep(announce=not shared_sleep, name=s.model if multi else None)
        s.on_loaded = announcer(s, m)

    # the one place the addresses and the APIs are printed: the server listens before the model loads
    print(tagged("ready", f"http://{here}:{a.port}/v1  openai+anthropic  stats http://{here}:{a.port}/"
                 + (" api-key=on" if svc.api_key else "")), flush=True)
    if a.mock_engine:
        for s in services:
            s.on_loaded()
    if a.host not in LOOPBACK_NAMES:
        # reachable from other devices: print the addresses
        ips = lan_addresses()
        for ip in ips:
            print(tagged("network", f"http://{ip}:{a.port}/v1"), flush=True)
        if not ips:
            print(tagged("network", f"port={a.port}"), flush=True)
        if exposed_without_key(a.host, svc.api_key):
            print(tagged("warning", "no api key, open to the network (--api-key KEY)"), flush=True)
    if not a.mock_engine and not a.lazy:
        if multi:
            router.preload(svc)                         # the first entry; a failed load leaves the others servable
        else:
            svc.start_load()                            # the page and the API are up; requests wait for the load
    if a.open:
        open_when_ready(svc, f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}/",
                        now=bool(a.mock_engine or a.lazy))
    # SIGTERM takes Ctrl+C's path (QUIT to the engine) so a container stop does not orphan it
    def on_sigterm(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # one stop: a later TERM must not cut the engine's QUIT short
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, on_sigterm)
    except (ValueError, OSError, AttributeError):         # not the main thread
        pass
    rc = 0
    try:
        while True:
            time.sleep(1)                               # a timed wait keeps Ctrl+C deliverable
            if svc.boot_error is not None:              # the start-up load failed: requests waiting on it get the error
                time.sleep(2)
                rc = 1
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        if not rc:
            print("[server] stopping (Ctrl+C again to force)", flush=True)
        closers = [router.stop, httpd.shutdown]
        for s in services:
            closers += [getattr(s.engine, "close", None), s.vision.close if s.vision else None]
        for close in filter(None, closers):
            try:
                close()
            except KeyboardInterrupt:                   # second Ctrl+C: do not wait for the engine
                for s in services:
                    if getattr(s.engine, "proc", None):
                        s.engine.proc.kill()
        print("[server] stopped", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

# ===== file: serve/service.py =====
"""Service: the engine behind a lock, with request bookkeeping and the metrics the stats page reads."""
from __future__ import annotations

import collections
import contextlib
import ctypes
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator

from serve.engine.errors import EngineDied, EngineSilent, EngineStartError, EngineStarting, EngineStuck, GpuBusy, ModelBusy
from serve.engine.mock import Engine
from serve.frontend import ChatTemplate, Event, images_of, mark_think_literals, unmark_think_literals
from serve.util.logbuf import LogBuffer
from serve.security import LOOPBACK_NAMES
from serve.models import Dialect, dialect_for
from serve.prompt import CTX_SLACK, PromptText, overflow_error, room_error
from serve.util.text import Detokenizer, StopMatcher, control_literals, stop_strings
from serve.engine.image_source import USAGE as IMAGE_USAGE, ImageRefused, ImageSources
from serve.engine.vision import IMAGE_WAITING_BYTES, Vision
from serve.api.grammar import grammar_spec, unenforced
from serve.config import GRAMMAR_DEFAULT


def available_ram_mib() -> int | None:
    """MemAvailable from /proc/meminfo, in MiB; None where there is no such file."""
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) >> 10
    except (OSError, ValueError, IndexError):
        pass
    return None


# trailing effort turn (effort_end): sentences for the non-default efforts
EFFORT_TEXT = {"low": "Reasoning effort is set to low. Keep your thinking brief and focused, moving directly to the "
                      "conclusion without unnecessary elaboration.",
               "medium": "Reasoning effort is set to medium. Think as much as the task needs, without unnecessary "
                         "elaboration."}
# the Qwen dialect's closer of thinking; other dialects carry their own
REASONING_CLOSE = "\n</think>\n\n"
# Reserve output tokens for closing the thinking and the answer above this cap.
ANSWER_ROOM_MIN_CAP = 1024
ANSWER_RESERVE_MIN = 512
QUEUE_BEAT_S = 10.0         # a request waiting for the single turn sends a keep-alive this often
KEEPALIVE_S = 10.0          # a stream quiet this long (a held-back tool argument) gets a keep-alive
# live tok/s is a windowed rate: a mean since the first token spikes at the start of an answer
RATE_WINDOW_S = 2.0
RATE_SAMPLES = 1024         # (time, generated) samples kept per request: the window at up to ~500 tok/s, one per token
LOG_FIRST_LINES = 300        # lines /api/logs returns to a client with none
LOG_MAX_LINES = 500          # ... and its maximum per answer
REQUESTS_SHOWN = 12        # finished requests /metrics lists
# the fastest whole request counts only when big enough that fixed costs do not decide it
BEST_MIN_OUT = 256          # output tokens, for the best decode rate
BEST_MIN_READ = 4096        # newly read prompt tokens, for the best prefill rate
RATE_MIN_SPAN_S = 0.25      # below this span there is no rate yet: use the mean with the span floored
# diagnostics, off unless the server flag sets them
DEBUG = False            # --debug: one line per request plus the raw model text


class Service:
    loading = booting = False                      # a load or wake runs; the start-up load runs
    load_kind = "weights"                               # what the running load is: "weights" or "wake"
    on_loaded = None                                    # called after each weights load or wake this service ran
    boot_error = None                                   # the start-up load's failure, for the requests queued behind it
    start_tries = None                                  # tries of a load other than the start-up's (None: the engine's)

    def model_entry(self) -> dict:
        """This model's row in /v1/models."""
        value = "loading" if self.loading else "loaded" if self.loaded() else "unloaded"
        return {"id": self.model, "object": "model", "status": {"value": value},
                "asleep": getattr(self.engine, "asleep", False) is True,
                "meta": {"n_ctx": self.engine.max_context},
                "architecture": {"input_modalities": ["text", "image"] if self.vision is not None else ["text"],
                                 "output_modalities": ["text"]}}

    def served_names(self) -> list[str]:
        """The model names a request may use: the one /v1/models lists, and the model file's own name."""
        names = [self.model]
        path = getattr(self.engine, "model_path", None)
        if isinstance(path, str) and path:
            names.append(path.replace("\\", "/").rsplit("/", 1)[-1])
        return names

    def __init__(self, engine: Engine, tokenizer, template: ChatTemplate, model_name: str = "basalt",
                 vision: Vision | None = None, sampling_defaults: dict | None = None,
                 fit_max_tokens: bool = False, grammar: bool = GRAMMAR_DEFAULT, grammar_engine: bool = True,
                 dialect: Dialect | None = None):
        self.engine, self.tok, self.template, self.model, self.vision = engine, tokenizer, template, model_name, vision
        self.dialect = dialect or dialect_for(None)      # how this model's prompts and replies are written
        self.literals = self.dialect.literal_tags(control_literals(tokenizer))   # control texts that stay text inside a message
        self.fit_max_tokens = fit_max_tokens          # clamp the output cap instead of a 400
        self.grammar = grammar                        # the `grammar` option: every tool or JSON request gets one
        self.grammar_engine = grammar_engine          # the engine understands the request key
        self.sampling_defaults = dict(sampling_defaults or {})   # the model file's sampling defaults
        self.fifo = threading.Lock()
        self.stopping = threading.Event()               # set at shutdown: nothing starts the engine again
        self.embeddings = threading.local()           # this request's image embeddings file
        self.api_key = ""                              # when set, /v1/* requires it
        self.image_sources = ImageSources()             # --image-url-host and --image-dir; remote, as is any default
        self.images_lock = threading.Lock()
        self.images_waiting = 0                         # bytes of read images waiting for their turn to encode
        # put a non-default reasoning effort in a system turn before the answer so the cached prefix survives
        self.effort_end = False
        self.logs = LogBuffer()                         # server and engine output
        # origins allowed to call /v1/* (CORS; "*" = any); empty: no CORS headers
        self.cors_origins: list[str] = []
        self.max_payload = 256 << 20                     # the most a JSON request body may hold, in bytes
        # origins treated as the server's own page for the load/unload/VRAM controls (no wildcard)
        self.trusted_origins: list[str] = []
        # DNS-rebinding guard: extra Host names ("*" = any) and all names answered, set by serve()
        self.allowed_hosts: list[str] = []
        self.host_names: set[str] = set(LOOPBACK_NAMES)
        self.status = {"busy": False, "queued": 0}      # what the model is doing now
        self.rate = collections.deque(maxlen=RATE_SAMPLES)   # (time, generated) samples
        # parallel mode: each running request's own status and rate window; self.status shows the newest
        self.live_reqs: dict[int, tuple[dict, collections.deque]] = {}
        self.request_ctx = threading.local()            # .path, .model: the API path and model name of this request
        self.slot_last: dict[int, dict] = {}            # per slot: context held and draft counts of its last request
        self.waiting: dict[int, dict] = {}              # single stream: requests waiting for the turn
        self.best: dict[str, dict] = {}                 # fastest whole request since start: "decode", "prefill"
        self.history = collections.deque(maxlen=500)    # finished requests, newest last
        self.requests_done = 0                           # finished requests since start
        self.last_timings = None                         # last request's timings
        self.last_request_at = None                      # last request start or finish
        self.started_at = time.time()
        self.status_lock = threading.Lock()
        self.live_lock = threading.Lock()               # one combined live line per LIVE_EVERY_S, whichever request is first
        self.live_at = 0.0
        # GPU sharing, all off by default: idle unload seconds, min free VRAM to load, a command run before loading
        self.idle_unload_s = 0
        self.idle_sleep_s = 0
        self.min_free_vram_mib = 0
        self.before_load = None
        self.vram_reserve = None                         # last POST /v1/vram reserve (None: the start's)
        self.reasoning_budget_tokens = 0                 # default thinking budget (0: none)
        self.answer_room = True                          # reserve output space when thinking
        self.thinking_loop_tokens = 256                  # one token repeated this often in the thinking ends it (0: off)
        self.anthropic_think_unasked = True               # "anthropic_thinking": "on_request" -> False
        self.stop_ids = {t for s in self.dialect.stop_texts for t in tokenizer.encode(s, parse_special=True)}

    def loaded(self) -> bool:
        return not hasattr(self.engine, "alive") or self.engine.alive()

    def reasoning_budget(self, req, *, use_default: bool = True) -> int | None:
        """The request's thinking budget in tokens, or None (0 or less: none); ValueError for a non-integer."""
        req = req if isinstance(req, dict) else {}
        value = req.get("reasoning_budget_tokens")
        if value is None:                               # llama.cpp's name for the same budget
            value = req.get("thinking_budget_tokens")
        if value is None:                               # Anthropic's, which also names a level by its size
            thinking = req.get("thinking")
            value = thinking.get("budget_tokens") if isinstance(thinking, dict) else None
        if value is None:
            value = self.reasoning_budget_tokens if use_default else None
        if value is None:
            return None
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"reasoning_budget_tokens: expected a whole number (0 = none), not {value!r}")
        return value if value > 0 else None

    def answer_budget(self, req, max_new: int, budget: int | None) -> int | None:
        """Limit thinking to leave answer space; an explicit non-positive request budget opts out."""
        if not self.answer_room or max_new < ANSWER_ROOM_MIN_CAP:
            return budget
        request_budget = self.reasoning_budget(req, use_default=False)
        # None also means an absent field: distinguish it from an explicit opt-out.
        req = req if isinstance(req, dict) else {}
        thinking = req.get("thinking")
        explicit = any(req.get(k) is not None for k in ("reasoning_budget_tokens", "thinking_budget_tokens")) \
            or isinstance(thinking, dict) and thinking.get("budget_tokens") is not None
        if explicit and request_budget is None:
            return budget
        room = max_new - min(max(ANSWER_RESERVE_MIN, max_new // 4), max_new // 2)
        if budget is None or budget > room:
            print(f"[server] thinking budget {room} tokens, output cap {max_new} (--no-answer-room disables)",
                  flush=True)
            return room
        return budget

    def _vision_down(self) -> bool:
        return self.vision is not None and hasattr(self.vision, "alive") and not self.vision.alive()

    def free_vram_mib(self) -> int | None:
        """Free VRAM on the first GPU from NVML; None when unreadable (nothing is then refused)."""
        try:
            from serve.util.telemetry import _Nvml
            nv = _Nvml(int(getattr(self, "gpu_index", 0) or 0))
            if not nv.ok():
                return None
            m = nv.Mem()
            if nv.lib.nvmlDeviceGetMemoryInfo(nv.dev, ctypes.byref(m)) != 0:
                return None
            return int(m.free >> 20)
        except Exception:
            return None

    def stop(self):
        """Shutdown has begun: a request still in flight no longer restarts the engine."""
        self.stopping.set()

    def load_progress(self) -> dict | None:
        """The running load: {"kind": "weights" | "wake", and for weights "percent", "gib"} from the engine's log, else None."""
        if not self.loading:
            return None
        return {"kind": self.load_kind, **((getattr(self.engine, "load_status", None) or {}) if self.load_kind == "weights" else {})}

    def start_load(self) -> None:
        """Load the model in the background at start. The HTTP server is already up and requests wait on the same
        fifo as for any load; a failed start is kept for them and ends the server."""
        def run():
            self.booting = True
            try:
                self.load()
            except (EngineDied, GpuBusy):
                pass                                    # kept in boot_error by ensure_loaded
            finally:
                self.booting = False
                self.loading = False
        self.load_kind, self.loading = "weights", True      # from the first poll, not from when the thread gets the fifo
        threading.Thread(target=run, daemon=True).start()

    def ensure_loaded(self):
        """Start the engine if it is not running, after the before_load hook and VRAM check. Caller holds fifo."""
        if self.boot_error is not None:
            raise EngineStartError(str(self.boot_error), self.boot_error.log)
        if self.loaded() and not self._vision_down() and getattr(self.engine, "asleep", False) is not True:
            return
        self.load_kind = "wake" if getattr(self.engine, "asleep", False) is True else "weights"
        self.loading = True
        try:
            self._ensure_loaded()
            if self.on_loaded is not None:
                self.on_loaded()
        except (EngineDied, GpuBusy) as e:
            if self.booting:                            # the requests queued behind it get this, not a second try
                self.boot_error = e if isinstance(e, EngineStartError) else EngineStartError(str(e))
            raise
        finally:
            self.loading = False

    def _ensure_loaded(self):
        if self.stopping.is_set():
            raise EngineDied("the server is stopping")
        if getattr(self.engine, "asleep", False) is True:
            self._wake()
        if self.loaded() and not self._vision_down():
            return
        if self.before_load:
            cmd = self.before_load
            print(f"[server] before_load: {cmd if isinstance(cmd, str) else ' '.join(map(str, cmd))}", flush=True)
            try:
                subprocess.run(cmd, shell=isinstance(cmd, str), timeout=120, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"[server] warning: before_load command failed ({e}), loading anyway", flush=True)
        if self.min_free_vram_mib:
            free = self.free_vram_mib()
            deadline = time.time() + 15                 # memory just released elsewhere may take a moment
            while free is not None and free < self.min_free_vram_mib and time.time() < deadline:
                time.sleep(0.5)
                free = self.free_vram_mib()
            if free is not None and free < self.min_free_vram_mib:
                raise GpuBusy(f"the GPU is in use by another program: {free} MiB of VRAM free, the model needs "
                              f"{self.min_free_vram_mib} (min_free_vram_mib) - it stays unloaded until that is free")
        if self._vision_down():                         # first: a GPU encoder takes its VRAM before the engine sizes its cache
            print("[server] restarting the vision encoder", flush=True)
            self.vision.restart()
        if self.loaded():
            return
        if self.stopping.is_set():                      # shutdown began while the hook or the VRAM wait ran
            raise EngineDied("the server is stopping")
        again = bool(getattr(self.engine, "known_ctx", 0))   # a first load says nothing: the load bars do
        if not again:
            pass
        elif getattr(self.engine, "unloaded", False):
            print("[server] loading the model again", flush=True)
        else:
            code = self.engine.exit_code() if hasattr(self.engine, "exit_code") else None
            print(f"[server] warning: the engine stopped (exit code {code}), restarting it", flush=True)
        try:
            tries = 1 if self.booting else self.start_tries   # the start-up load tries once
            self.engine.restart(tries=tries) if tries else self.engine.restart()
        except EngineStartError as e:                   # a line, not a traceback
            print(f"[server] error: {e}", flush=True)
            if e.log:
                print(f"[server] engine log: {e.log}", flush=True)
            raise
        if again:
            print("[server] engine running again", flush=True)
        if self.vram_reserve is not None and hasattr(self.engine, "vram"):   # reapply the last reserve
            try:
                self.engine.vram(self.vram_reserve)
            except (ValueError, EngineDied) as e:
                print(f"[server] warning: VRAM reserve of {self.vram_reserve} MiB not applied ({e})", flush=True)

    vram_wait_s = 300.0                                  # POST /v1/vram wait for a running request

    def vram(self, reserve_mib: int | None) -> dict:
        """POST /v1/vram: keep `reserve_mib` of VRAM free (None: the starting reserve), applied between requests.
        Needs --vram-elastic; an unloaded engine applies it on load."""
        if not hasattr(self.engine, "vram"):
            raise ValueError("this engine cannot resize its VRAM use")
        if not self.fifo.acquire(timeout=self.vram_wait_s):
            raise ModelBusy("a request is still running; try again when it has finished")
        # parallel requests do not hold the fifo, so take the control lines too: no resize while a slot decodes
        ctl = getattr(self.engine, "ctl", None) if getattr(self.engine, "batch", 0) else None
        if ctl is not None and not ctl.acquire(timeout=self.vram_wait_s):
            self.fifo.release()
            raise ModelBusy("a request is still running; try again when it has finished")
        try:
            if not self.loaded():
                self.vram_reserve = reserve_mib
                return {"status": "not loaded", "reserve_mib": reserve_mib,
                        "note": "applied when the model loads"}
            out = self.engine.vram(reserve_mib)
            self.vram_reserve = reserve_mib
            cache = f"expert cache {out['expert_cache_mib']} of {out.get('expert_cache_full_mib')} MiB " \
                if out.get("expert_cache_mib") is not None else ""
            print(f"[server] vram: {out.get('vram_free_mib')} MiB free, {cache}({out.get('expert_slots')} experts)", flush=True)
            return {"status": "ok", **out}
        finally:
            if ctl is not None:
                ctl.release()
            self.fifo.release()

    def _say_died(self, e: Exception) -> None:
        """Print the server-window line for an engine that ended mid-request."""
        note = self.engine.death_note() if hasattr(self.engine, "death_note") else ""
        log = getattr(self.engine, "log_path", None)
        why = f"{e}: {note}" if note else str(e)
        print(f"[server] warning: {why} (the next request restarts it{', log ' + log if log else ''})", flush=True)

    def require_grammar(self, response_format) -> None:
        """A JSON response format the engine would not enforce is refused rather than silently served as free text."""
        if unenforced(self.grammar, response_format):
            raise ValueError("response_format json_schema and json_object (structured output) need grammar enabled "
                             "on this server; start it without --no-grammar, or send strict: true")

    def grammar_for(self, tools, tool_choice, raw_tools, response_format) -> dict | None:
        """The engine request's `grammar` value (api.grammar.grammar_spec); None when the engine cannot take it."""
        if not self.grammar_engine:
            return None
        return grammar_spec(self.grammar, tools, tool_choice, raw_tools, response_format)

    def load(self):
        """Start the engine if unloaded (POST /load and every generation request); may raise GpuBusy."""
        # keeps the idle thread from unloading before this request starts
        self.last_request_at = time.time()
        if self.loaded() and not self._vision_down() and getattr(self.engine, "asleep", False) is not True:
            return
        with self.fifo:
            self.ensure_loaded()

    def wait_load(self):
        """Hold the caller until a running load or wake ends, on the same fifo as a request; raises the load's error."""
        if self.loading:
            self.load()

    def swap_out(self):
        """End the engine for another model's turn: no request runs (the router drained them). A sleeping engine gets
        its GPU state back for its exit, without refilling its caches."""
        with self.fifo:
            if hasattr(self.engine, "unload"):
                self.engine.unload()
            if self.vision is not None and hasattr(self.vision, "unload"):
                self.vision.unload()

    def _wake(self):
        """Wake a sleeping engine (caller holds fifo); a failed wake ends it and the next start loads it again."""
        t = time.time()
        try:
            self.engine.wake()
        except (ValueError, EngineDied) as e:
            print(f"[server] warning: waking the model failed ({e}); loading it again", flush=True)
            return
        print(f"[server] model awake in {time.time() - t:.1f} s (driver restore {self.engine.restore_s:.1f} s)",
              flush=True)

    sleep_refused_at = None                              # the request an idle sleep was last refused after
    SLEEP_RAM_MARGIN_MIB = 8192                          # RAM left free once the GPU state is in host memory

    def sleep(self, idle_for: float | None = None) -> str:
        """Put the engine to sleep between requests: its VRAM released, its process and RAM state kept, the next
        request wakes it. "asleep", "not loaded", "busy", "no ram" or "unsupported"."""
        if not hasattr(self.engine, "sleep") or not self.engine.can_sleep():
            return "unsupported"
        if self.stopping.is_set():                      # shutdown: the engine is being ended, not put to sleep
            return "busy"
        if not self.fifo.acquire(blocking=False):
            return "busy"
        ctl = getattr(self.engine, "ctl", None) if getattr(self.engine, "batch", 0) else None
        if ctl is not None and not ctl.acquire(blocking=False):
            self.fifo.release()
            return "busy"
        try:
            if not self.engine.alive():
                return "not loaded"
            if getattr(self.engine, "asleep", False) is True:
                return "asleep"
            with self.status_lock:
                if self.status.get("busy") or self.status.get("queued"):
                    return "busy"
            if idle_for is not None and time.time() - (self.last_request_at or self.started_at) < idle_for:
                return "busy"
            # an idle sleep the engine refused is tried again after the next request, not on every round
            if idle_for is not None and self.sleep_refused_at == (self.last_request_at or self.started_at):
                return "unsupported"
            t = time.time()
            try:
                self.engine.sleep(ram_check=self._sleep_fits)
            except ValueError as e:
                if idle_for is not None:
                    self.sleep_refused_at = self.last_request_at or self.started_at
                print(f"[server] warning: the model stays awake ({e})", flush=True)
                return "no ram" if "RAM" in str(e) else "unsupported"
            print(f"[server] model asleep{f' after {idle_for:.0f} s idle' if idle_for else ''} in {time.time() - t:.1f} s: "
                  "its VRAM is free, the next request wakes it", flush=True)
            return "asleep"
        finally:
            if ctl is not None:
                ctl.release()
            self.fifo.release()

    def _sleep_fits(self) -> str | None:
        """Whether the GPU memory the engine still holds fits in RAM with the margin left; None if so, else why not."""
        used = self.used_vram_mib()
        avail = available_ram_mib()
        if used is None or avail is None:
            return None if avail is not None else "RAM unreadable"
        if avail - used < self.SLEEP_RAM_MARGIN_MIB:
            return f"RAM: {avail} MiB free, {used} MiB of GPU state to hold and {self.SLEEP_RAM_MARGIN_MIB} MiB to keep"
        return None

    def used_vram_mib(self) -> int | None:
        """VRAM in use on the engine's GPUs from NVML (every process on them); None when unreadable."""
        try:
            from serve.util.telemetry import _Nvml
            total = 0
            for i in getattr(self, "gpu_indices", None) or [int(getattr(self, "gpu_index", 0) or 0)]:
                nv = _Nvml(int(i))
                if not nv.ok():
                    return None
                m = nv.Mem()
                if nv.lib.nvmlDeviceGetMemoryInfo(nv.dev, ctypes.byref(m)) != 0:
                    return None
                total += int(m.used >> 20)
            return total
        except Exception:
            return None

    def start_idle_sleep(self, announce: bool = True, name: str | None = None):
        """Put the model to sleep after idle_sleep_s; `announce` prints the line, naming `name` when given."""
        if not self.idle_sleep_s or not hasattr(self.engine, "sleep"):
            return
        if not self.engine.can_sleep():
            print("[server] warning: idle sleep is not available here (the driver cannot suspend a process); "
                  "use idle_unload", flush=True)
            return
        if announce:
            print(f"[server] {name or 'model'} sleeps after {self.idle_sleep_s:g} s idle", flush=True)

        def loop():
            while True:
                time.sleep(max(1.0, min(30.0, self.idle_sleep_s / 4)))
                try:
                    self.sleep(idle_for=self.idle_sleep_s)
                except EngineDied as e:                 # the next request starts it again
                    print(f"[server] warning: idle sleep failed ({e})", flush=True)
        threading.Thread(target=loop, daemon=True).start()

    def unload(self, idle_for: float | None = None) -> str:
        """Stop the engine between requests: "unloaded", "not loaded", "busy" or "unsupported"."""
        if not hasattr(self.engine, "unload"):
            return "unsupported"
        if not self.fifo.acquire(blocking=False):
            return "busy"
        try:
            if not self.engine.alive():
                return "not loaded"
            with self.status_lock:
                if self.status.get("busy") or self.status.get("queued"):
                    return "busy"
            if idle_for is not None and time.time() - (self.last_request_at or self.started_at) < idle_for:
                return "busy"
            if getattr(self.engine, "asleep", False) is True:
                self._wake()
            self.engine.unload()
            if self.vision is not None and hasattr(self.vision, "unload"):
                self.vision.unload()
            print(f"[server] model unloaded{f' after {idle_for:.0f} s idle' if idle_for else ''}, the next request loads it", flush=True)
            return "unloaded"
        finally:
            self.fifo.release()

    def start_idle_unload(self):
        if not self.idle_unload_s or not hasattr(self.engine, "unload"):
            return
        print(f"[server] model unloads after {self.idle_unload_s} s idle", flush=True)

        def loop():
            while True:
                time.sleep(max(1.0, min(30.0, self.idle_unload_s / 4)))
                try:
                    self.unload(idle_for=self.idle_unload_s)
                except EngineStuck as e:                # retried next turn
                    print(f"[server] warning: idle unload failed ({e})", flush=True)
        threading.Thread(target=loop, daemon=True).start()

    def telemetry_extra(self) -> dict:
        """This model's speeds, for the hardware sampler behind GET /metrics."""
        return {"tok_s": self._tok_s(), "prefill_tok_s_mean": self._prefill_tok_s_mean(),
                "slot_tok_s": [r["tok_s"] for r in self.slot_table()[0]]}

    def _tok_s(self):
        """tok/s over the last RATE_WINDOW_S (summed over parallel requests); 0.0 when idle."""
        with self.status_lock:
            many = [(dict(st), list(rt)) for st, rt in self.live_reqs.values()]
        if many:
            return sum(self._rate_of(st, rt) for st, rt in many)
        with self.status_lock:
            s = dict(self.status)
            rate = list(self.rate)
        return self._rate_of(s, rate)

    @staticmethod
    def _rate_of(s, rate):
        if not s.get("busy") or not s.get("first_token"):
            return 0.0
        now = time.time()
        newest = rate[-1] if rate else None
        oldest = next(((t, g) for t, g in rate if now - t <= RATE_WINDOW_S), None)
        if newest and oldest and newest[0] - oldest[0] >= RATE_MIN_SPAN_S:
            return max(0.0, (newest[1] - oldest[1]) / (newest[0] - oldest[0]))
        return s["generated"] / max(RATE_MIN_SPAN_S, now - s["first_token"])

    def _tok_s_mean(self):
        """Mean tok/s since the first token."""
        with self.status_lock:
            many = [dict(st) for st, _ in self.live_reqs.values()] or [dict(self.status)]
        return sum(s["generated"] / max(1e-6, time.time() - s["first_token"]) for s in many
                   if s.get("busy") and s.get("first_token"))

    def _prefill_tok_s_mean(self):
        """Engine-reported mean over newly read tokens, excluding the cached prefix."""
        with self.status_lock:
            reading = self.status.get("busy") and self.status.get("first_token") is None
        return getattr(self.engine, "prefill_tok_s_mean", None) if reading else 0.0

    def slot_stats(self) -> dict:
        """Request slots: how many exist, how many requests run in them, and how many wait for one."""
        total = int(getattr(self.engine, "batch", 0) or 0)
        with self.status_lock:
            running = len(self.live_reqs) if total else int(bool(self.status.get("busy")))
            queued = int(self.status.get("queued") or 0)
        depth = getattr(self.engine, "queue_depth", None)
        waiting = depth() if total and depth else 0
        return {"total": max(1, total), "active": max(0, min(max(1, total), running - waiting)),
                "queue_depth": queued + waiting}

    def slot_table(self) -> tuple[list[dict], list[dict]]:
        """Per-slot rows and the waiting requests. The oldest running requests take the slots, each keeping its
        slot until it ends; the rest of the live requests wait for admission."""
        base = self.slot_stats()
        total, active = base["total"], base["active"]
        now = time.time()
        with self.status_lock:
            if self.live_reqs:
                live = [st for st, _ in self.live_reqs.values()]
                rates = {id(st): list(rt) for st, rt in self.live_reqs.values()}
            else:
                live = [self.status] if self.status.get("busy") else []
                rates = {id(self.status): list(self.rate)}
            live.sort(key=lambda st: st.get("started") or 0)
            running, waiting = live[:active], live[active:]
            taken = {st["slot"] for st in running if st.get("slot") is not None and st["slot"] < total}
            for st in running:
                if st.get("slot") is None or st["slot"] >= total:
                    st["slot"] = next((i for i in range(total) if i not in taken), None)
                    taken.add(st["slot"])
            by_slot = {st["slot"]: (dict(st), rates.get(id(st), [])) for st in running}
            queue = [{"api": st.get("api"), "model": st.get("model"), "prompt_tokens": st.get("prompt_tokens"),
                      "waited_s": round(now - (st.get("started") or now), 1)}
                     for st in waiting + sorted(self.waiting.values(), key=lambda w: w["started"])]
            last = {k: dict(v) for k, v in self.slot_last.items()}
        reading = [i for i, (st, _) in by_slot.items() if st.get("first_token") is None]
        progress = getattr(self.engine, "progress", None)
        rows = []
        for i in range(total):
            held = dict(last.get(i, {}))
            bd = (getattr(self.engine, "slot_drafts", None) or {}).get(i)
            if bd and bd[2] >= held.get("at", 0):      # a batch slot's own counts, when newer than a solo request's
                held.update(accepted=bd[0], offered=bd[1])
            row = {"id": i, "n_ctx": self.engine.max_context, "is_processing": i in by_slot, "state": "idle",
                   "api": None, "model": None, "tok_s": 0.0, "generated": 0, "max_tokens": None, "prompt_tokens": 0,
                   "prefill_done": 0, "reused": None, "ttft_s": None, "draft_len": None,
                   "ctx_used": held.get("ctx", 0), "drafts_offered": held.get("offered"),
                   "drafts_accepted": held.get("accepted")}       # draft counts are the slot's last finished request's
            if i in by_slot:
                st, rate = by_slot[i]
                prompt, gen = int(st.get("prompt_tokens") or 0), int(st.get("generated") or 0)
                decoding = st.get("first_token") is not None
                done = prompt if decoding else (min(progress[0], prompt) if progress and reading == [i] else None)
                windows = st.get("windows") or 0
                row.update(state="decoding" if decoding else "prefill", api=st.get("api"), model=st.get("model"),
                           tok_s=round(self._rate_of(st, rate), 1), generated=gen, max_tokens=st.get("max_tokens"),
                           prompt_tokens=prompt, prefill_done=done, ctx_used=prompt + gen, reused=st.get("reused"),
                           ttft_s=round(st["first_token"] - st["started"], 2) if decoding and st.get("started")
                           else None,
                           draft_len=round(st.get("window_tokens", 0) / windows, 2) if windows else None)
            rows.append(row)
        return rows, queue

    def metrics(self) -> dict:
        """GET /metrics: engine facts, live state, recent requests, hardware and speed history."""
        with self.status_lock:
            s = dict(self.status)
            if self.live_reqs:                          # parallel: the newest request
                s = {**s, **dict(list(self.live_reqs.values())[-1][0]), "queued": s.get("queued", 0)}
            # parallel: generating while any slot decodes, though the newest request may still wait or read
            decoding = any(st.get("first_token") is not None for st, _ in self.live_reqs.values())
            hist = list(self.history)
        busy = bool(s.get("busy"))
        if self.loading:
            state = "loading"
        elif busy and s.get("first_token") is None and not decoding:
            state = "reading"
        elif busy:
            state = "generating"
        elif not self.loaded():
            state = "unloaded"
        elif getattr(self.engine, "asleep", False) is True:
            state = "asleep"
        else:
            state = "idle"
        slots = self.slot_stats()
        live = {"state": state, "load": self.load_progress(), "queued": slots["queue_depth"], "slots": slots, "queue": self.slot_table()[1],
                "prompt_tokens": s.get("prompt_tokens") if busy else None,
                "generated": s.get("generated") if busy else None,
                "elapsed_s": round(time.time() - s["started"], 1) if busy and s.get("started") else None,
                "tok_s": round(self._tok_s(), 1) if state == "generating" else None,
                "prefill_tok_s_mean": getattr(self.engine, "prefill_tok_s_mean", None) if busy else None}
        info = dict(getattr(self.engine, "info", {}) or {})
        spill = spill_of(info)
        engine = {"model": self.model, "max_context": self.engine.max_context, **info, "spill": spill}
        tel = self.telemetry.snapshot() if getattr(self, "telemetry", None) else \
            {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}
        with self.status_lock:
            best = {k: dict(v) for k, v in self.best.items()}
        return {"engine": engine, "live": live, "requests": hist[::-1][:REQUESTS_SHOWN], "best": best,
                "hardware": tel["now"], "hardware_static": tel["static"], "history": tel["history"]}

    def _note_best(self, r: dict) -> None:
        """Keep finished request `r` as the best decode or prefill when it is the fastest big enough one.
        Call with status_lock held."""
        if (r["output_tokens"] or 0) >= BEST_MIN_OUT and r["decode_tok_s"] and \
                r["decode_tok_s"] > self.best.get("decode", {}).get("tok_s", 0):
            self.best["decode"] = {"tok_s": r["decode_tok_s"], "id": r["id"], "time": r["time"],
                                   "output_tokens": r["output_tokens"]}
        read = (r["prompt_tokens"] or 0) - (r["reused"] or 0)
        if read >= BEST_MIN_READ and r["prompt_ms"]:
            rate = round(read / (r["prompt_ms"] / 1000), 1)
            if rate > self.best.get("prefill", {}).get("tok_s", 0):
                self.best["prefill"] = {"tok_s": rate, "id": r["id"], "time": r["time"], "read_tokens": read}

    def v1_status(self) -> dict:
        """GET /v1/status: model, context, APIs, activity and last timings, for clients that poll status."""
        with self.status_lock:
            s = dict(self.status)
            last_t, last_at = (dict(self.last_timings) if self.last_timings else None), self.last_request_at
        tel = self.telemetry.snapshot() if getattr(self, "telemetry", None) else {"now": {}, "static": {}}
        gpus, gpus_static = (tel.get("now") or {}).get("gpus") or [], (tel.get("static") or {}).get("gpus") or []
        hw, name = (gpus[0] if gpus else {}), (gpus_static[0].get("name") if gpus_static else None)
        ram = tel.get("now") or {}

        def scaled(v, unit, digits=0):
            return round(v / unit, digits) if isinstance(v, (int, float)) else None

        busy, ctx = bool(s.get("busy")), self.engine.max_context
        images = self.vision is not None
        return {
            "service": "basalt", "model": self.model,
            "loaded": self.loaded(), "loading": self.loading, "load": self.load_progress(), "asleep": getattr(self.engine, "asleep", False) is True,
            "auto_load": hasattr(self.engine, "restart"),
            "structured_output": {"formats": ["json_object", "json_schema"], "method": "grammar",
                                  "constrained_decoding": bool(self.grammar_engine), "stream_buffered": False},
            "engine": (getattr(self.engine, "info", {}) or {}).get("version"),
            "started": int(self.started_at), "uptime_s": int(time.time() - self.started_at),
            "cache_max_tokens": ctx,
            "context": {"native": ctx, "max_positions": ctx},
            "concurrency": {"serving": max(1, int(getattr(self.engine, "batch", 0) or 0)),
                            "requested": max(1, int(getattr(self.engine, "batch", 0) or 0))},
            "dialects": ["/v1/chat/completions", "/v1/messages", "/v1/responses"],
            "vision": {"enabled": images, "available": images, "error": None},
            "activity": {"requests": self.requests_done + int(busy), "in_flight": int(busy) + int(s.get("queued") or 0),
                         "last_request_at": int(last_at) if last_at else None},
            "last_timings": last_t,
            "vram": dict((getattr(self.engine, "info", {}) or {}).get("vram") or {},
                         elastic=bool((getattr(self.engine, "info", {}) or {}).get("vram_elastic"))),
            "machine": {
                "at": int(time.time()),
                "gpu": {"name": name, "used_mib": scaled(hw.get("mem_used"), 2 ** 20),
                        "total_mib": scaled(hw.get("mem_total"), 2 ** 20), "util_pct": hw.get("util"),
                        "temp_c": hw.get("temp"), "power_w": hw.get("power")} if name else None,
                "ram": {"used_gib": scaled(ram.get("ram_used"), 2 ** 30, 1),
                        "total_gib": scaled(ram.get("ram_total"), 2 ** 30, 1)} if ram.get("ram_total") else None}}

    def render_prompt(self, messages, tools, kwargs) -> str:
        """Render the template. With `effort_end`, a non-default effort is rendered as the default up to the answer
        (same prefix, cache kept) and follows as a short system turn right before it."""
        if kwargs.get("reasoning_effort") not in (None, ""):
            kwargs = dict(kwargs, reasoning_effort=self.dialect.effort_level(kwargs["reasoning_effort"]))
        effort = kwargs.get("reasoning_effort")
        off = kwargs.get("enable_thinking") is False
        if not self.effort_end or not self.dialect.effort_turn or (not off and effort in (None, "", "xhigh", "high")):
            return self.template.render(messages, tools=tools, **kwargs)
        base = {k: v for k, v in kwargs.items() if k not in ("reasoning_effort", "enable_thinking")}
        history = self.template.render(messages, tools=tools, add_generation_prompt=False, **base)
        full = self.template.render(messages, tools=tools, add_generation_prompt=True, **kwargs)
        mine = self.template.render(messages, tools=tools, add_generation_prompt=False, **kwargs)
        if not full.startswith(mine):                  # a template that cannot be split: render as asked
            return full
        tail = "" if off else self.dialect.effort_turn.format(EFFORT_TEXT.get(effort, EFFORT_TEXT["medium"]))
        return history + tail + full[len(mine):]

    def render_text(self, messages, tools, kwargs) -> tuple[str, list]:
        """The rendered prompt and the character spans in it where special-token text stays plain text: think tags
        and control-token text inside messages are text; only the template's own are special."""
        marked, marked_tools, changed = mark_think_literals(messages, tools, self.literals)
        prompt = self.render_prompt(marked, marked_tools, kwargs)
        if not changed:
            return prompt, []
        return unmark_think_literals(prompt, self.literals)

    def encode_prompt(self, messages, tools, kwargs) -> list[int]:
        """The rendered, tokenized prompt (see render_text)."""
        prompt, plain = self.render_text(messages, tools, kwargs)
        if not plain:
            return self.tok.encode(prompt, parse_special=True)
        return self.tok.encode(prompt, parse_special=True, plain=plain)

    def prepare(self, messages, tools, kwargs, max_new=None, force=None, image_access=None):
        """Return (ids, thinking, max_new); an unset or non-positive max_new means the rest of the context.
        `force` (frontend.forced_call): the opening of the call the reply must make.  Without thinking it ends the
        prompt; with thinking, run() writes it once the thinking is over.  `image_access`: what this request's
        connection may name as an image; without one, the remote policy."""
        self.embeddings.path = None
        try:
            return self._prepare(messages, tools, kwargs, max_new, force, image_access or self.image_sources)
        except BaseException:
            if self.embeddings.path:
                Path(self.embeddings.path).unlink(missing_ok=True)
                self.embeddings.path = None
            raise

    def _prepare_text(self, messages, tools, kwargs, max_new, force):
        """The text path: the engine tokenizes, so the server sends the rendered prompt as it is and settles the
        answer cap with the engine (clip: cut to the room the context leaves)."""
        text, plain = self.render_text(messages, tools, kwargs)
        tail = force if force and kwargs.get("enable_thinking", True) is False else ""
        ctx = self.engine.max_context
        if ctx <= 0:
            if getattr(self.engine, "starting", False):
                raise EngineStarting("the engine is starting (a minute or two); try again shortly")
            ctx = getattr(self.engine, "known_ctx", 0)
            if ctx <= 0:
                raise EngineStarting("the engine is starting (a minute or two); try again shortly")
        unset = max_new is None or max_new <= 0
        if unset:
            max_new = ctx - CTX_SLACK
        prompt = PromptText(self.tok, text + tail, plain, tail, clip=unset or self.fit_max_tokens)
        prompt.max_new = max_new
        return prompt, kwargs.get("enable_thinking", True) is not False, max_new

    def _check_images(self, messages, images) -> None:
        """Refuse, before rendering, a request whose images cannot reach the prompt."""
        if self.vision is None:
            _refuse_images("this server was started without image input (--vision), so it cannot read images")
        if images_of(m for m in messages if m.get("role") == "system"):
            _refuse_images("a system prompt cannot hold an image")
        if not all(images):
            _refuse_images(IMAGE_USAGE)

    def _prepare(self, messages, tools, kwargs, max_new, force, image_access):
        images = images_of(messages)
        if images:
            self._check_images(messages, images)
        elif getattr(self.engine, "takes_text", False) and not getattr(self.engine, "batch", 0):
            return self._prepare_text(messages, tools, kwargs, max_new, force)
        ids = self.encode_prompt(messages, tools, kwargs)
        if force and kwargs.get("enable_thinking", True) is False:
            ids = ids + self.tok.encode(self.dialect.nothink_lead + force, parse_special=True)
        if images:
            pad = self.tok.encode(self.dialect.image_pad, parse_special=True)[0]
            start = self.tok.encode(self.dialect.image_start, parse_special=True)[0] if self.dialect.image_start else None
            try:                                        # before the fifo: a slow source must not hold the turn
                images = self.vision.read(images, image_access)
            except ImageRefused as e:
                print(f"[server] error: {e.log_line}", flush=True)
                raise
            except ValueError as e:
                print(f"[server] error: {e}", flush=True)
                raise
            held = sum(len(i) for i in {id(i): i for i in images}.values())
            with self.images_lock:
                if self.images_waiting + held > IMAGE_WAITING_BYTES:
                    raise ModelBusy("too many images are waiting to be encoded; try again shortly")
                self.images_waiting += held
            try:
                # encode under the fifo: the encoder and a running engine must not use the GPU together
                with self.fifo:
                    if getattr(self.vision, "in_engine", False):   # the encoder lives in the engine: it must be running
                        self.ensure_loaded()
                    try:
                        self.embeddings.path, counts = self.vision.encode(images)
                    except ValueError as e:             # a refused image: the client gets the 400, the log says why
                        print(f"[server] error: {e}", flush=True)
                        raise
            finally:
                with self.images_lock:
                    self.images_waiting -= held
            # expand each image's pad to one per image token; a pad not after <|vision_start|> is message text
            literal = self.tok.encode(self.dialect.image_pad, parse_special=False)
            out, k = [], 0
            for j, t in enumerate(ids):
                if t == pad and (start is None or (j > 0 and ids[j - 1] == start)):
                    # more markers than images
                    if k == len(counts):
                        _refuse_images("the prompt and its images do not match")
                    out += [pad] * counts[k]
                    k += 1
                elif t == pad:
                    out += literal
                else:
                    out.append(t)
            if k != len(counts):
                _refuse_images("the prompt and its images do not match")
            ids = out
        ctx = self.engine.max_context
        if ctx <= 0:
            if getattr(self.engine, "starting", False):   # starting, not an oversized prompt
                raise EngineStarting("the engine is starting (a minute or two); try again shortly")
            # a failed restart leaves max_context 0: use the last known context so run() can restart the engine
            ctx = getattr(self.engine, "known_ctx", 0)
            if ctx <= 0:
                raise EngineStarting("the engine is starting (a minute or two); try again shortly")
        room = ctx - CTX_SLACK - len(ids)
        if max_new is None or max_new <= 0 or (self.fit_max_tokens and room < 1):
            if room < 1:
                raise room_error(len(ids), ctx)
            max_new = room
        elif max_new > room:
            if not self.fit_max_tokens:
                raise overflow_error(len(ids), max_new, ctx)
            max_new = max(1, room)          # fit_max_tokens: shorten rather than 400
        return ids, kwargs.get("enable_thinking", True) is not False, max_new

    def _note(self, n, evs, st=None, rate=None):
        with self.status_lock:
            s = self.status if st is None else st
            s["generated"] = n
            windows = getattr(self.engine, "window_stats", None)
            if windows is not None:
                s["windows"], s["window_tokens"] = windows()
            if s.get("first_token") is None:
                s["first_token"] = time.time()
            (self.rate if rate is None else rate).append((time.time(), n))   # live rate window
            for ev in evs:
                if ev.kind == "reasoning":
                    s["phase"] = "thinking"
                elif ev.kind == "content":
                    s["phase"] = "answering"
                elif ev.kind == "tool_start":
                    s["phase"], s["tool"] = f"writing a tool call: {ev.call.name}", ev.call.name
                elif ev.kind == "tool_call":
                    s["phase"] = "tool call complete"
                s["tail"] = ((s.get("tail") or "") + (ev.text or ""))[-600:]

    def _slot_of(self, st) -> int | None:
        """The slot a running request holds, as the slot table shows it (the lowest free one, kept to its end).
        Call with status_lock held."""
        total = max(1, int(getattr(self.engine, "batch", 0) or 0))
        if st.get("slot") is None or st["slot"] >= total:
            taken = {o["slot"] for o, _ in self.live_reqs.values()
                     if o is not st and o.get("slot") is not None and o["slot"] < total}
            st["slot"] = next((i for i in range(total) if i not in taken), None)
        return st["slot"]

    def live_line(self, now: float) -> str | None:
        """One line for every request that is generating; None when none is."""
        with self.status_lock:
            reqs = [st for st, _ in self.live_reqs.values()] or ([self.status] if self.status.get("busy") else [])
            rows = []
            for st in sorted(reqs, key=lambda x: x.get("started") or 0):
                if not st.get("busy") or st.get("first_token") is None:
                    continue
                slot = self._slot_of(st)
                rows.append((-1 if slot is None else slot, dict(st)))
        if not rows:
            return None
        parts, total = [], 0.0
        for slot, st in sorted(rows, key=lambda r: r[0]):
            seen = now - st["first_token"]
            rate = st["generated"] / seen if seen >= LIVE_MIN_S else None   # too soon after the first token for a rate
            total += rate or 0.0
            parts.append(f"{slot_tag(None if slot < 0 else slot)} tg={st['generated']}/{st.get('max_tokens')}"
                         + (f" {rate:.1f}t/s" if rate is not None else ""))
        return "[server] live | " + " | ".join(parts) + f" | total {total:.0f}t/s"

    def _progress(self, last_print, every=1.0, st=None):
        """Print progress while a request runs: its prompt read, then the shared live line."""
        now = time.time()
        with self.status_lock:
            s = dict(self.status if st is None else st)
        if now - last_print < (READ_PRINT_EVERY if s.get("first_token") is None else every):
            return last_print
        if s.get("first_token") is None:
            pr = getattr(self.engine, "progress", None)   # (position, prompt tokens); a reused prefix counts as read
            done = f"{pr[0]}/{pr[1]}" if pr and pr[1] else f"{s.get('prompt_tokens', 0)}"   # as read
            # the engine's average over the tokens it read for this request (a reused prefix is not in it)
            pp = getattr(self.engine, "prefill_tok_s_mean", None) if pr and pr[1] else None
            speed = f" {pp:.0f}t/s" if pp and pp > 0 else ""
            with self.status_lock:
                slot = self._slot_of(st if st is not None else self.status)
            print(f"[server] {slot_tag(slot)} read  pp={done}{speed} {now - s.get('started', now):.0f}s", flush=True)
        else:
            with self.live_lock:
                due = now - self.live_at >= LIVE_EVERY_S
                if due:
                    self.live_at = now
            line = self.live_line(now) if due else None
            if line:
                print(line, flush=True)
        return now

    def run(self, ids, thinking, tools, max_new, sampling, cancel, force=None) -> Iterator[tuple[str, object]]:
        """Yield ("event", Event) as text arrives, ("ping", None) as heartbeat, then ("done", {...}).
        `force`: the opening of the call the reply must make (see prepare)."""
        budget = self.reasoning_budget(sampling) if thinking else None
        budget_req = sampling
        budget_ready = False
        if self.sampling_defaults:     # the request's own fields win
            req_values = {k: v for k, v in (sampling or {}).items() if v is not None}
            sampling = {**self.sampling_defaults, **req_values}
        parser = self.dialect.parser(thinking, tools, stream_tools=True)
        detok, n, finish = Detokenizer(self.tok), 0, "length"
        thinking_n = 0                                 # tokens written while thinking
        stop_list = stop_strings(sampling)
        stops = StopMatcher(stop_list) if stop_list else None

        def cut(evs):
            """The events with the answer text cut at a stop string."""
            if stops is None:
                return evs
            out = []
            for ev in evs:
                if stops.hit is not None:
                    break                               # nothing after the stop string
                if ev.kind == "content":
                    text = stops.push(ev.text or "")
                    if text:
                        out.append(Event("content", text))
                    continue
                held = stops.flush()                    # a stop string does not span a tool call or thinking
                if held:
                    out.append(Event("content", held))
                out.append(ev)
            return out
        one_call = self.dialect.forced_call_ends_reply and bool(force) and force.rstrip().endswith(">")     # a named choice: the reply is that one call
        opening = []                                    # without thinking the prompt ends with the forced opening:
        if force and not thinking:                      # the parser reads it as if the model had written it
            opening, force = parser.feed(self.dialect.nothink_lead + force), None
        tail = ""                                       # the last characters written (the newlines before a call)
        reused0 = None                                  # reused prefix of the request's first engine run
        timings, before = None, None                    # this request's timings; the engine's `last` before it
        raw_ids = []                                    # every generated id (--debug)
        emb = getattr(self.embeddings, "path", None)
        # identity token: only a DONE line replaces engine.last, so a request without one records no stale figures
        engine_last0 = getattr(self.engine, "last", None)
        path = getattr(self.request_ctx, "path", None)   # the API path serving this request
        # parallel requests keep their own status and rate window; otherwise self.status / self.rate
        par = bool(getattr(self.engine, "batch", 0))
        st = {} if par else self.status
        rate = collections.deque(maxlen=RATE_SAMPLES) if par else self.rate
        wait_key = object()                             # this request's entry in self.waiting
        with self.status_lock:
            self.status["queued"] += 1
            if not par:
                self.waiting[id(wait_key)] = {"api": "anthropic" if path == "/v1/messages" else "openai",
                                              "model": getattr(self.request_ctx, "model", None) or self.model,
                                              "prompt_tokens": len(ids), "started": time.time()}
        try:
            # batch slots run requests concurrently, so no fifo
            turn = contextlib.ExitStack()
            if not getattr(self.engine, "batch", 0):
                # A request waiting for the single turn sends a keep-alive now and then, so a client with a stream idle
                # timeout does not abort a healthy queued request; one that left while queued gives its place up.
                got = False
                try:
                    while not got:
                        got = self.fifo.acquire(timeout=QUEUE_BEAT_S)
                        if got:
                            turn.callback(self.fifo.release)
                        elif cancel.is_set():
                            break
                        else:
                            yield "ping", None
                except BaseException:                    # the consumer closed us at a ping: no longer queued
                    with self.status_lock:
                        self.status["queued"] -= 1
                        self.waiting.pop(id(wait_key), None)
                    raise
                if not got:                              # cancelled while queued
                    with self.status_lock:
                        self.status["queued"] -= 1
                        self.waiting.pop(id(wait_key), None)
                    yield "done", {"finish": "cancel", "completion_tokens": 0, "reused": 0, "timings": None,
                                   "reasoning_tokens": 0}
                    return
            with turn:
                try:
                    with self.status_lock:
                        self.status["queued"] -= 1
                        self.waiting.pop(id(wait_key), None)
                    # restart a dead or unloaded engine; parallel requests serialize this start on the fifo
                    with (self.fifo if par else contextlib.nullcontext()):
                        self.ensure_loaded()
                    with self.status_lock:
                        st.update(busy=True, phase="reading the prompt", prompt_tokens=len(ids),
                                  generated=0, started=time.time(), first_token=None, tool=None, tail="",
                                  max_tokens=max_new, slot=None, reused=None,
                                  api="anthropic" if path == "/v1/messages" else "openai",
                                  model=getattr(self.request_ctx, "model", None) or self.model)
                        if par:
                            self.live_reqs[id(st)] = (st, rate)
                            self.status.update(st)
                        self.last_request_at = time.time()
                        rate.clear()                    # drop the previous request's samples
                    before = getattr(self.engine, "last", None)
                    last_print = time.time()
                    prompt, thought = ids, 0            # reasoning tokens so far
                    closed_early = False                # the thinking was already closed once
                    run_id, run_n = None, 0             # the token the thinking is repeating, and how often in a row
                    for ev in opening:
                        yield "event", ev
                    while True:
                        if isinstance(ids, PromptText) and ids.max_new:
                            max_new = ids.max_new       # settled by the engine at PT (a clipped cap)
                        gen = self.engine.generate(prompt, max_new - n, sampling, cancel, embeddings=emb) if emb \
                            else self.engine.generate(prompt, max_new - n, sampling, cancel)
                        seg, wrap, leaving = [], False, False   # this pass's tokens; budget reached; consumer left
                        looped = False                  # the wrap is for a repeated token, not the budget
                        ended_thinking = False          # the turn ended inside the thinking
                        opens = False                   # the thinking is over: write the forced call's opening
                        try:
                            # a held-back tool argument can leave the stream silent for long: ping after a quiet spell
                            last_out = time.monotonic()
                            for t in gen:
                                # The engine settles a text prompt's output cap before its first token.
                                if isinstance(ids, PromptText) and ids.max_new:
                                    max_new = ids.max_new
                                if thinking and not budget_ready and (not isinstance(ids, PromptText) or ids.n is not None):
                                    budget = self.answer_budget(budget_req, max_new, budget)
                                    budget_ready = True
                                if t is None:               # heartbeat
                                    if isinstance(ids, PromptText) and ids.n is not None and st.get("prompt_tokens") != ids.n:
                                        with self.status_lock:  # the engine has counted the prompt
                                            st.update(prompt_tokens=ids.n, max_tokens=ids.max_new or max_new)
                                    last_print = self._progress(last_print, st=st)
                                    last_out = time.monotonic()
                                    yield "ping", None
                                    continue
                                n += 1
                                if t in self.stop_ids:
                                    if (parser.state == "reasoning" and not closed_early and not parser.buf
                                            and not detok.pending()):
                                        n -= 1              # the end token is not part of the reply
                                        ended_thinking = True
                                        break
                                    finish = "stop"
                                    raw_ids.append(t)
                                    break
                                raw_ids.append(t)
                                seg.append(t)
                                if parser.state == "reasoning":
                                    run_n = run_n + 1 if t == run_id else 1
                                    run_id = t
                                else:
                                    run_n = 0
                                thinking_n += parser.state == "reasoning"
                                piece = detok.push(t)
                                tail = (tail + piece)[-2:]
                                evs = cut(parser.feed(piece))
                                self._note(n, evs, st, rate)
                                if st.get("reused") is None:    # the prefix reuse, once the engine reports it
                                    run_last = getattr(self.engine, "last", None)
                                    if isinstance(run_last, dict) and run_last is not engine_last0 \
                                            and run_last.get("reused") is not None:
                                        with self.status_lock:
                                            st["reused"] = min(int(run_last["reused"]), len(ids))
                                last_print = self._progress(last_print, st=st)
                                for ev in evs:
                                    yield "event", ev
                                if evs:
                                    last_out = time.monotonic()
                                elif time.monotonic() - last_out >= KEEPALIVE_S:
                                    last_out = time.monotonic()
                                    yield "ping", None
                                if stops is not None and stops.hit is not None:
                                    finish = "stop"         # gen.close() below STOPs the engine
                                    break
                                if (self.thinking_loop_tokens and run_n >= self.thinking_loop_tokens
                                        and parser.state == "reasoning" and not parser.buf and not detok.pending()):
                                    wrap = looped = True
                                    break
                                if one_call and any(ev.kind == "tool_call" for ev in evs):
                                    finish = "stop"         # no second call after a forced one
                                    break
                                if budget and parser.state == "reasoning":
                                    thought += 1
                                    # only at a clean point: no held tag or split character
                                    if thought >= budget and not parser.buf and not detok.pending():
                                        wrap = True
                                        break
                                if force:
                                    if any(ev.kind in ("tool_start", "tool_call") for ev in evs):
                                        force = None    # the model is writing a call of its own
                                    elif parser.state == "content" and not parser.buf and not detok.pending():
                                        opens = True    # at a clean point right after the thinking
                                        break
                        except EngineDied as e:
                            finish = "error"
                            self._say_died(e)
                            raise
                        except ValueError as e:             # the engine's ERR line
                            finish = "error"
                            print(f"[server] error: engine reported {e}", flush=True)
                            raise
                        except GeneratorExit:               # the client went away
                            leaving = True                  # an unacknowledged STOP below must not replace this
                            raise
                        finally:
                            try:
                                gen.close()             # STOP and drain to this request's DONE under the fifo
                            except EngineSilent as e:   # the STOP was never acknowledged
                                finish = "error"
                                self._say_died(e)
                                if not leaving and not cancel.is_set():
                                    raise
                        run_last = getattr(self.engine, "last", None)
                        if reused0 is None and isinstance(run_last, dict) and run_last is not engine_last0 \
                                and run_last.get("reused") is not None:
                            reused0 = min(int(run_last["reused"]), len(ids))    # later passes extend this reply
                        if not (wrap or opens or ended_thinking) or cancel.is_set():
                            break
                        # budget reached or turn ended in thinking: close it and continue; the next prompt extends this
                        # one, so the engine keeps its prefix
                        if ended_thinking:
                            closed_early = True
                            extra = self.tok.encode(self.dialect.reasoning_close + (force or ""), parse_special=True)
                            force = None
                            if max_new - n - len(extra) < 1:
                                finish = "stop"
                                break
                            print("[server] the model ended while thinking, closing it for an answer", flush=True)
                        elif wrap:
                            budget = None
                            extra = self.tok.encode(self.dialect.reasoning_close + (force or ""), parse_special=True)
                            force = None
                            if max_new - n - len(extra) < 1:
                                break                   # no room to answer: "length"
                            if looped:
                                print(f"[server] thinking repeated one token {run_n} times, ending it", flush=True)
                            else:
                                print(f"[server] thinking budget reached ({thought} tokens), wrapping up", flush=True)
                        else:
                            # the thinking closed itself: the call opens after the blank line the template puts
                            # before a call
                            text = "\n" * (2 - (len(tail) - len(tail.rstrip("\n")))) + force
                            force = None
                            extra = self.tok.encode(text, parse_special=True)
                            if max_new - n - len(extra) < 1:
                                break                   # no room for the call: "length"
                        for t in extra:
                            n += 1
                            raw_ids.append(t)
                            thinking_n += parser.state == "reasoning"
                            evs = cut(parser.feed(detok.push(t)))
                            self._note(n, evs, st, rate)
                            for ev in evs:
                                yield "event", ev
                        if stops is not None and stops.hit is not None:
                            finish = "stop"
                            break
                        prompt = prompt + seg + extra
                    if cancel.is_set():
                        finish = "cancel"
                except GeneratorExit:                  # client disconnected
                    finish = "disconnect"
                    raise
                finally:
                    # settle status and history under the fifo, before the next request sets its own
                    with self.status_lock:
                        if st.get("busy"):
                            # same object as before: no DONE arrived
                            last = dict(getattr(self.engine, "last", {}) or {}) \
                                if getattr(self.engine, "last", None) is not engine_last0 else {}
                            if reused0 is not None and last.get("reused") is not None:
                                last["reused"] = reused0
                            started = st.get("started", time.time())
                            hit_rate = round(last["hits"] / last["lookups"], 3) if last.get("lookups") else None
                            # experts read over PCIe or computed elsewhere are not in the hit rate's lookups
                            routed = (last.get("lookups") or 0) + (last.get("offloaded") or 0)
                            pcie_share = round(last["offloaded"] / routed, 3) \
                                if last.get("offloaded") is not None and routed else None
                            ssd_share = round(last["file_reads"] / routed, 3) if last.get("file_reads") and routed else None
                            seen = prompt_tokens_seen(len(ids), last)   # less than len(ids) when cancelled mid-read
                            self.requests_done += 1
                            if st.get("slot") is not None:
                                self.slot_last[st["slot"]] = {"ctx": seen + n, "offered": last.get("drafts_offered"),
                                                              "accepted": last.get("drafts_accepted"), "at": time.time()}
                            ft = st.get("first_token")
                            ttft = ft - st["started"] if ft and st.get("started") else None
                            windows, window_tokens = st.get("windows") or 0, st.get("window_tokens") or 0
                            dl = window_tokens / windows if windows else None
                            self.history.append({
                                "id": f"r_{self.requests_done:04d}", "path": path, "slot": self._slot_of(st),
                                "time": started, "duration_s": round(time.time() - started, 1), "finish": finish,
                                "prompt_tokens": seen, "reused": last.get("reused"), "output_tokens": n,
                                "prompt_ms": last.get("prompt_ms"),
                                "decode_tok_s": round(last["generated"] / (last["decode_ms"] / 1000), 1)
                                if n and last.get("generated") and last.get("decode_ms") else None,
                                "hit_rate": hit_rate, "pcie_share": pcie_share, "ssd_share": ssd_share,
                                "drafts_offered": last.get("drafts_offered"),
                                "drafts_accepted": last.get("drafts_accepted"),
                                "ttft_s": round(ttft, 2) if ttft is not None else None,
                                "draft_len": round(dl, 2) if dl is not None else None})
                            self._note_best(self.history[-1])
                            fresh = getattr(self.engine, "last", None)
                            if fresh is not None and fresh is not before:      # a DONE line arrived for this request
                                timings = request_timings(seen, n, last)
                                self.last_timings = dict(timings, at=int(time.time())) if timings else None
                            self.last_request_at = time.time()
                            now = time.time()
                            rate = n / max(1e-6, now - ft) if ft else 0.0
                            print(done_line(self._slot_of(st), seen, n, finish, cancel.is_set(), timings, ttft, rate,
                                            dl, hit_rate, pcie_share, ssd_share), flush=True)
                            if finish == "length" and parser.state == "reasoning":
                                print("[server] warning: max tokens reached while thinking, no answer "
                                      "(set reasoning_budget_tokens, or start with --reasoning-budget-tokens)",
                                      flush=True)
                            if DEBUG and raw_ids:
                                print(f"[server] raw: {self.tok.decode(raw_ids)!r}", flush=True)
                        st["busy"] = False
                        st.pop("tail", None)                     # drop the tail once done
                        st.pop("tool", None)
                        if par:
                            self.live_reqs.pop(id(st), None)
                            if self.live_reqs:              # the newest request still running
                                self.status.update(list(self.live_reqs.values())[-1][0])
                            else:
                                self.status.update(busy=False)
                                self.status.pop("tail", None)
                                self.status.pop("tool", None)
        finally:
            if emb:
                Path(emb).unlink(missing_ok=True)
        for ev in cut(parser.finish()):
            yield "event", ev
        if stops is not None and stops.hit is None and stops.held:
            yield "event", Event("content", stops.flush())     # the held tail was not a stop string
        done = {"finish": finish, "completion_tokens": n, "reused": (timings or {}).get("cache_n", 0),
                "timings": timings, "reasoning_tokens": thinking_n}
        if stops is not None and stops.hit is not None:
            done["stop_sequence"] = stops.hit
        yield "done", done


def prompt_tokens_seen(prompt_tokens: int, last: dict) -> int:
    """Prompt tokens a request got through: all, unless the DONE line shows a cancel mid-read (reused + read)."""
    read = last.get("prompt_read")
    if read is None or last.get("finish") != "cancel":
        return prompt_tokens
    return min(prompt_tokens, int(last.get("reused") or 0) + int(read))


READ_PRINT_EVERY = 5.0      # seconds between the prompt-reading lines
LIVE_EVERY_S = 2.0          # seconds between the live lines
LIVE_MIN_S = 0.5            # seconds of generation before the live line shows a slot's rate
MIN_RATE_TOKENS = 16        # a reply of fewer tokens shows no wall-clock rate
MIN_PP_RATE_TOKENS = 512    # a prompt read of fewer newly read tokens shows its time, not a rate


def slot_tag(slot: int | None) -> str:
    """A slot as the console names it: s1, s2, ..."""
    return "s?" if slot is None else f"s{slot + 1}"


def spill_of(info: dict) -> dict | None:
    """The experts only the file holds, from the engine's INFO words: {experts, total, bytes}, None when none are."""
    words = {k: info.pop(f"spill_{k}", None) for k in ("experts", "total", "bytes")}
    return words if isinstance(words["experts"], int) and words["experts"] > 0 else None


def done_line(slot: int | None, prompt_total: int, generated: int, finish: str, cancelled: bool,
              timings: dict | None, ttft: float | None, wall_rate: float, dl: float | None = None,
              hit: float | None = None, pcie: float | None = None, ssd: float | None = None) -> str:
    """The request's closing log line: slot, prompt (total, cached, read time, read rate), reply (count, rate,
    drafts accepted, tokens per window), time to the first token, end, expert cache hit and PCIe share.
    `timings` is request_timings(); None when the engine gave no clock."""
    t = timings or {}
    pp = f"pp={prompt_total}/{t.get('cache_n') or 0}c"
    if t.get("prompt_ms") is not None:
        pp += f" {t['prompt_ms'] / 1000:.1f}s"
        if (t.get("prompt_n") or 0) >= MIN_PP_RATE_TOKENS and t.get("prompt_per_second"):
            pp += f" {t['prompt_per_second']:.0f}t/s"
    rate = t.get("predicted_per_second") or (wall_rate if generated >= MIN_RATE_TOKENS else 0.0)
    tg = f"tg={generated}" + (f" {rate:.1f}t/s" if generated and rate else "")
    if t.get("draft_n"):
        tg += f" acc={t['draft_n_accepted'] / t['draft_n'] * 100:.0f}%"
        if dl is not None:
            tg += f" dl={dl:.1f}"
    groups = [pp, tg]
    if ttft is not None:
        groups.append(f"ttft={ttft:.1f}s")
    groups.append("end=" + ("cancelled" if cancelled or finish == "cancel" else finish))
    if hit is not None:
        groups.append(f"hit={hit * 100:.1f}%")
        if pcie is not None:
            groups.append(f"pcie={pcie * 100:.1f}%")
        if ssd:
            groups.append(f"ssd={ssd * 100:.1f}%")
    return f"[server] {slot_tag(slot)} done  " + "  ".join(groups)


def request_timings(prompt_tokens: int, generated: int, last: dict) -> dict | None:
    """A request's `timings` in llama.cpp's names from the engine's DONE figures; None without an engine clock."""
    if last.get("prompt_ms") is None:
        return None
    cache_n = int(last.get("reused") or 0)
    prompt_n, prompt_ms, decode_ms = max(0, prompt_tokens - cache_n), float(last["prompt_ms"]), float(last.get("decode_ms") or 0)
    decoded = int(last.get("generated") or generated)          # the engine's count gives the rate
    return {"cache_n": cache_n, "prompt_n": prompt_n, "prompt_ms": round(prompt_ms, 1),
            "prompt_per_token_ms": round(prompt_ms / prompt_n, 3) if prompt_n else None,
            "prompt_per_second": round(prompt_n / (prompt_ms / 1000), 1) if prompt_n and prompt_ms > 0 else None,
            "predicted_n": generated, "predicted_ms": round(decode_ms, 1),
            "predicted_per_token_ms": round(decode_ms / decoded, 3) if decoded else None,
            "predicted_per_second": round(decoded / (decode_ms / 1000), 1) if decoded and decode_ms > 0 else None,
            # draft counts only when reported
            **({"draft_n": int(last["drafts_offered"]), "draft_n_accepted": int(last["drafts_accepted"])}
               if last.get("drafts_offered") is not None else {})}


def _refuse_images(why: str):
    """A request refused for its images: the client gets the 400, the log says why."""
    print(f"[server] error: {why}", flush=True)
    raise ValueError(why)


def _debug_req(api, req, messages, tools, max_new, thinking, prompt_tokens):
    """--debug: one compact line per request."""
    if not DEBUG:
        return
    last = messages[-1] if messages else {}
    body = last.get("content")
    if isinstance(body, list):
        body = " ".join(p.get("text", "") for p in body if isinstance(p, dict))
    preview = (str(body or "")[:80]).replace("\n", " ")
    print(f"[server] req {api}: msgs={len(messages)} tools={len(tools or [])} "
          f"max_tokens_raw={req.get('max_tokens')!r}/{req.get('max_completion_tokens')!r} "
          f"max_new={max_new} thinking={thinking} stream={bool(req.get('stream'))} "
          f"prompt_tokens={prompt_tokens} last={last.get('role')!r}:{preview!r}", flush=True)

# ===== file: serve/util/__init__.py =====
"""Helpers with no server state: text and tokenizer helpers, the log buffer, hardware telemetry."""

# ===== file: serve/util/console.py =====
"""The server's console lines in the compact key=value style: `[server] <label>  <key=value ...>`."""
from __future__ import annotations

LABEL_WIDTH = 8

LEGEND = ("pp=prompt read  tg=generation  c=cached  acc=drafts accepted  dl=tokens per window  "
          "hit=expert cache  pcie=routed experts over PCIe")


def tagged(label: str, text: str) -> str:
    """A startup line: the label padded to a column, then the text."""
    return f"[server] {label:<{LABEL_WIDTH}} {text}"


# ===== file: serve/util/logbuf.py =====
"""Recent server and engine output for GET /api/logs: LogBuffer, TeeStream and tail_file."""
from __future__ import annotations

import collections
import os
import re
import threading
import time

from serve.util.loglevel import ERR, WARN, LevelReader

CAPACITY = 2000
MAX_LINE = 2000
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
SERVER_PREFIX = "[server] "


class LogBuffer:
    """A thread-safe ring of the last CAPACITY lines with rising ids."""

    def __init__(self, capacity: int = CAPACITY):
        self.lock = threading.Lock()
        self.lines: collections.deque = collections.deque(maxlen=capacity)
        self.last = 0
        self.readers: dict[str, LevelReader] = {}      # per source: a traceback spans lines

    def add(self, src: str, text: str) -> None:
        """Add each non-empty line of `text` (ANSI codes removed, long lines cut) from source `src`. A line's level is
        read from its marker (serve/util/loglevel.py); a `[server] ` prefix is dropped after that."""
        now = round(time.time(), 3)
        for raw in ANSI.sub("", text or "").splitlines():
            msg = raw.rstrip()[:MAX_LINE]
            if not msg.strip():
                continue
            with self.lock:
                lvl = self.readers.setdefault(src, LevelReader()).read(msg)
                self.last += 1
                self.lines.append({"id": self.last, "t": now, "src": src, "lvl": lvl,
                                   "msg": msg.removeprefix(SERVER_PREFIX)})

    def since(self, after_id: int = 0, limit: int = 500) -> tuple[list[dict], int]:
        """Return (the newest `limit` lines above `after_id`, oldest first; the newest id)."""
        with self.lock:
            fresh = [dict(x) for x in self.lines if x["id"] > after_id]
            last = self.last
        return (fresh[-limit:] if limit > 0 else []), last


class TeeStream:
    """Wraps sys.stdout/stderr: passes everything through and adds each complete line to the buffer as `src`."""

    def __init__(self, stream, buffer: LogBuffer, src: str):
        self._stream, self._buffer, self._src = stream, buffer, src
        self._partial = ""
        self._lock = threading.Lock()

    def write(self, text):
        n = self._stream.write(text)
        if isinstance(text, str):
            with self._lock:
                self._partial += text
                *done, self._partial = self._partial.split("\n")
            for line in done:
                self._buffer.add(self._src, line)
        return n

    def __getattr__(self, name):
        return getattr(self._stream, name)


class ColorStream:
    """Wraps a terminal stream: error lines are red and warning lines yellow, by the markers of serve/util/loglevel.py.
    Off a terminal, or with NO_COLOR set, the stream is returned unwrapped. Text that moves the cursor (escape
    codes, carriage returns) is passed through at once so in-place redraws keep working."""

    RED, YELLOW, RESET = "\x1b[31m", "\x1b[33m", "\x1b[0m"

    @classmethod
    def wrap(cls, stream):
        try:
            tty = stream.isatty()
        except (AttributeError, ValueError):
            tty = False
        return cls(stream) if tty and not os.environ.get("NO_COLOR") else stream

    def __init__(self, stream):
        self._stream = stream
        self._partial = ""
        self._lock = threading.Lock()
        self._reader = LevelReader()

    def paint(self, line: str) -> str:
        lvl = self._reader.read(ANSI.sub("", line))
        color = self.RED if lvl == ERR else self.YELLOW if lvl == WARN else ""
        return color + line + self.RESET if color else line

    def write(self, text):
        if not isinstance(text, str):
            return self._stream.write(text)
        with self._lock:
            *done, rest = (self._partial + text).split("\n")
            self._partial = ""
            if "\x1b" in rest or "\r" in rest:        # a redraw in progress: no waiting for its newline
                done.append(rest)
                out = "\n".join(self.paint(x) if i < len(done) - 1 else x for i, x in enumerate(done))
            else:
                self._partial = rest
                out = "".join(self.paint(x) + "\n" for x in done)
            if out:
                self._stream.write(out)
        return len(text)

    def flush(self):
        with self._lock:
            if self._partial:
                self._stream.write(self._partial)
                self._partial = ""
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def tail_file(buffer: LogBuffer, path: str, src: str = "eng", poll_s: float = 0.25) -> None:
    """Follow a file from its end, adding complete lines to the buffer; waits for a missing file and rereads a
    shrunk one from the start."""
    try:
        pos = os.path.getsize(path)
    except OSError:
        pos = 0
    partial = b""
    while True:
        try:
            size = os.path.getsize(path)
            if size < pos:
                pos, partial = 0, b""
            if size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read(min(size - pos, 1 << 20))
                pos += len(data)
                *done, partial = (partial + data).split(b"\n")
                for line in done:
                    buffer.add(src, line.decode("utf-8", "replace"))
                continue
        except OSError:
            pass
        time.sleep(poll_s)

# ===== file: serve/util/loglevel.py =====
"""The level of a log line, read from the marker its writer put on it. No message text is searched for words.

Markers, one per kind of writer:
  engine   basalt <area>: <text>            info
           basalt <area>: warning: <text>   warning
           basalt <area>: error: <text>     error      (older engines: "basalt warning: <text>", "basalt error: <text>")
  server   [server] <text>                  info
           [server] warning: <text>         warning    (a startup block line reads `warning  <text>`)
           [server] error: <text>           error
  python   a "Traceback (most recent call last):" block, and the exception line that ends it   error
  other    anything else (driver and library output, engine tables)                             info
"""
from __future__ import annotations

import re
from typing import NamedTuple

INFO, WARN, ERR = "info", "warn", "err"
_NAMES = {"warning": WARN, "error": ERR}
ENGINE = re.compile(r"(?P<tag>basalt(?:-[a-z]+)?(?: [\w-]+)*):(?: (?P<lvl>warning|error):)? (?P<text>.*)", re.S)
LEGACY = re.compile(r"basalt (?P<lvl>warning|error): (?P<text>.*)", re.S)
SERVER = re.compile(r"\[server\] (?:(?P<lvl>warning|error)(?:: | {2,}))?")
TRACEBACK = "Traceback (most recent call last):"
CHAINED = ("During handling of the above exception", "The above exception was the direct cause")


class EngineLine(NamedTuple):
    tag: str        # "basalt generate"; "basalt" for a line of no area
    level: str      # INFO, WARN or ERR
    text: str       # the message, without the tag and the marker


def engine_line(line: str) -> EngineLine | None:
    """The parts of an engine log line, or None when the line is not one."""
    m = LEGACY.fullmatch(line)
    if m:
        return EngineLine("basalt", _NAMES[m["lvl"]], m["text"])
    m = ENGINE.fullmatch(line)
    if m:
        return EngineLine(m["tag"], _NAMES.get(m["lvl"], INFO), m["text"])
    return None


def server_level(line: str) -> str | None:
    """The level a `[server] ` line states, or None when the line is not a server line."""
    m = SERVER.match(line)
    return None if m is None else _NAMES.get(m["lvl"], INFO)


class LevelReader:
    """Reads the level of the lines of one stream, in order (a traceback spans lines)."""

    def __init__(self):
        self._in_traceback = False

    def read(self, line: str) -> str:
        if line.startswith(TRACEBACK):
            self._in_traceback = True
            return ERR
        if line.startswith(CHAINED):
            self._in_traceback = False
            return ERR
        if self._in_traceback:
            if line[:1] not in (" ", "\t") and line.strip():     # the exception line ends the block
                self._in_traceback = False
            return ERR
        e = engine_line(line)
        if e is not None:
            return e.level
        s = server_level(line)
        return INFO if s is None else s

# ===== file: serve/util/telemetry.py =====
"""Hardware readings for GET /metrics, sampled once a second on a background thread.

GPU readings come from NVML through ctypes; CPU, RAM and disk from psutil, falling back to /proc (no disk rate).
Unreadable values are None and never stop the server.
"""
from __future__ import annotations

import collections
import ctypes
import os
import platform
import sys
import threading
import time

HISTORY = 60


# ------------------------------------------------------------------------------------------------ NVML
class _Nvml:
    class Util(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    class Mem(ctypes.Structure):
        _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

    def __init__(self, index=0):
        self.lib = self.dev = None
        names = ["libnvidia-ml.so.1", "libnvidia-ml.so"]
        if sys.platform == "win32":
            # Windows ships NVML as nvml.dll (System32); the .so names never load there, which left
            # /metrics with an empty gpus list and the dashboard showing "No GPU readings".
            names = ["nvml.dll"]
            nvml_dll = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "nvml.dll")
            if os.path.exists(nvml_dll):
                names.insert(0, nvml_dll)
        for n in names:
            try:
                self.lib = ctypes.CDLL(n)
                break
            except OSError:
                continue
        if self.lib is None:
            return
        try:
            init = getattr(self.lib, "nvmlInit_v2", None) or self.lib.nvmlInit
            if init() != 0:
                self.lib = None
                return
            h = ctypes.c_void_p()
            get = getattr(self.lib, "nvmlDeviceGetHandleByIndex_v2", None) or self.lib.nvmlDeviceGetHandleByIndex
            if get(ctypes.c_uint(index), ctypes.byref(h)) != 0:
                self.lib = None
                return
            self.dev = h
        except (AttributeError, OSError):
            self.lib = None

    def ok(self):
        return self.lib is not None and self.dev is not None

    def _uint(self, fn, *args):
        v = ctypes.c_uint()
        try:
            return v.value if getattr(self.lib, fn)(self.dev, *args, ctypes.byref(v)) == 0 else None
        except (AttributeError, OSError):
            return None

    def name(self):
        buf = ctypes.create_string_buffer(96)
        try:
            if self.lib.nvmlDeviceGetName(self.dev, buf, ctypes.c_uint(96)) == 0:
                return buf.value.decode(errors="replace")
        except (AttributeError, OSError):
            pass
        return None

    def driver_version(self):
        buf = ctypes.create_string_buffer(96)
        try:
            if self.lib.nvmlSystemGetDriverVersion(buf, ctypes.c_uint(96)) == 0:
                return buf.value.decode(errors="replace")
        except (AttributeError, OSError):
            pass
        return None

    def cuda_version(self):
        """The newest CUDA the driver supports, as "13.3"."""
        v = ctypes.c_int()
        for fn in ("nvmlSystemGetCudaDriverVersion_v2", "nvmlSystemGetCudaDriverVersion"):
            try:
                if getattr(self.lib, fn)(ctypes.byref(v)) == 0:
                    return f"{v.value // 1000}.{v.value % 1000 // 10}"
            except (AttributeError, OSError):
                continue
        return None

    def read(self):
        out = {}
        u = self.Util()
        try:
            if self.lib.nvmlDeviceGetUtilizationRates(self.dev, ctypes.byref(u)) == 0:
                out["util"] = u.gpu
        except (AttributeError, OSError):
            pass
        m = self.Mem()
        try:
            if self.lib.nvmlDeviceGetMemoryInfo(self.dev, ctypes.byref(m)) == 0:
                out["mem_used"], out["mem_total"] = m.used, m.total
        except (AttributeError, OSError):
            pass
        out["temp"] = self._uint("nvmlDeviceGetTemperature", ctypes.c_uint(0))          # NVML_TEMPERATURE_GPU
        mw = self._uint("nvmlDeviceGetPowerUsage")
        out["power"] = mw / 1000.0 if mw is not None else None
        lim = self._uint("nvmlDeviceGetEnforcedPowerLimit")
        out["power_limit"] = lim / 1000.0 if lim is not None else None
        out["pcie_gen"] = self._uint("nvmlDeviceGetCurrPcieLinkGeneration")        # drops at idle
        out["pcie_gen_max"] = self._uint("nvmlDeviceGetMaxPcieLinkGeneration")
        out["pcie_width"] = self._uint("nvmlDeviceGetCurrPcieLinkWidth")
        rx = self._uint("nvmlDeviceGetPcieThroughput", ctypes.c_uint(1))                 # NVML_PCIE_UTIL_RX_BYTES, KB/s
        tx = self._uint("nvmlDeviceGetPcieThroughput", ctypes.c_uint(0))
        out["pcie_rx_mb"] = rx / 1024.0 if rx is not None else None
        out["pcie_tx_mb"] = tx / 1024.0 if tx is not None else None
        return out


def gpu_reader(index=0):
    """The card's readings, from NVML."""
    return _Nvml(index)


# ------------------------------------------------------------------------------------------------ CPU / RAM
def _cpu_name():
    if os.path.exists("/proc/cpuinfo"):
        for line in open("/proc/cpuinfo", encoding="utf-8", errors="replace"):
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or None


class _CpuRamFallback:
    """CPU load and RAM without psutil."""

    def __init__(self):
        self.prev = self._times()

    def _times(self):
        try:
            f = [int(x) for x in open("/proc/stat").readline().split()[1:]]
            return f[3] + f[4], sum(f)
        except (OSError, ValueError):
            return None

    def cpu(self):
        cur = self._times()
        prev, self.prev = self.prev, cur
        if not cur or not prev or cur[1] == prev[1]:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - (cur[0] - prev[0]) / (cur[1] - prev[1]))))

    @staticmethod
    def ram():
        try:
            info = dict(line.split(":", 1) for line in open("/proc/meminfo"))
            total = int(info["MemTotal"].split()[0]) * 1024
            avail = int(info["MemAvailable"].split()[0]) * 1024
            return total - avail, total
        except (OSError, KeyError, ValueError):
            return None, None


# ------------------------------------------------------------------------------------------------ the sampler
GPU_KEYS = ("util", "mem_used", "mem_total", "temp", "power", "power_limit", "pcie_gen", "pcie_gen_max", "pcie_width",
            "pcie_rx_mb", "pcie_tx_mb")
SERIES = ("tok_s", "prefill_tok_s_mean")      # series kept as history
GPU_SERIES = ("util", "power", "temp")         # per-card series kept as history


def _r2(v):
    return round(v, 2) if isinstance(v, float) else v


class Telemetry:
    def __init__(self, extra=None, gpu_indices=None, gpu_factory=gpu_reader):
        """`extra()` returns the server's own speeds, recorded each second. `gpu_indices`: the engine's cards
        (NVML numbering); `gpu_factory(index)` returns a reader with ok(), read(), name() and optionally
        driver_version(), cuda_version()."""
        self.extra = extra
        self.lock = threading.Lock()
        self.now: dict = {"gpus": [], "cpu": None, "ram_used": None, "ram_total": None, "disk_read_mb": None}
        self.hist = {k: collections.deque(maxlen=HISTORY) for k in SERIES}
        self.slot_hist = collections.deque(maxlen=HISTORY)     # per sample: each slot's tok/s
        self.gpus = [(i, g) for i, g in ((i, gpu_factory(i)) for i in dict.fromkeys(gpu_indices or [0])) if g.ok()]
        self.gpu_hist = {i: {k: collections.deque(maxlen=HISTORY) for k in GPU_SERIES} for i, _ in self.gpus}
        try:
            import psutil  # noqa: F401
            self.ps = sys.modules["psutil"]
        except ImportError:
            self.ps = None
        self.fallback = _CpuRamFallback()
        first = self.gpus[0][1] if self.gpus else None
        self.static = {
            "gpus": [{"index": i, "name": g.name()} for i, g in self.gpus],
            "cpu_name": _cpu_name(),
            "cores": (self.ps.cpu_count(logical=False) if self.ps else None) or None,
            "threads": os.cpu_count(),
            "driver": first.driver_version() if hasattr(first, "driver_version") else None,
            "cuda": first.cuda_version() if hasattr(first, "cuda_version") else None,
        }
        self._disk_prev = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _disk(self):
        """Disk read MB/s since the last call; None without psutil or on the first call."""
        if not self.ps:
            return None
        try:
            c = self.ps.disk_io_counters()
        except (OSError, RuntimeError):
            return None
        t = time.time()
        prev, self._disk_prev = self._disk_prev, (t, c.read_bytes)
        if prev is None or t <= prev[0]:
            return None
        return (c.read_bytes - prev[1]) / (t - prev[0]) / 2**20

    def sample(self):
        s = {"gpus": []}
        for i, g in self.gpus:
            try:
                r = g.read()
            except Exception:  # noqa: BLE001 - telemetry must never take the server down
                r = {}
            s["gpus"].append({"index": i, **{k: r.get(k) for k in GPU_KEYS}})
        s["cpu"] = s["ram_used"] = s["ram_total"] = None
        if self.ps:
            try:
                s["cpu"] = self.ps.cpu_percent(interval=None)
                vm = self.ps.virtual_memory()
                s["ram_used"], s["ram_total"] = vm.total - vm.available, vm.total
            except (OSError, RuntimeError):
                pass
        else:
            s["cpu"] = self.fallback.cpu()
            s["ram_used"], s["ram_total"] = self.fallback.ram()
        s["disk_read_mb"] = self._disk()
        if self.extra:
            try:
                s.update(self.extra())
            except Exception:  # noqa: BLE001
                pass
        return s

    def record(self, s: dict) -> None:
        """Make sample `s` the current reading and add it to the history."""
        with self.lock:
            self.now = s
            for k in SERIES:
                self.hist[k].append(_r2(s.get(k)))
            if "slot_tok_s" in s:
                self.slot_hist.append([_r2(v) for v in s.get("slot_tok_s") or []])
            for g in s["gpus"]:
                h = self.gpu_hist.get(g["index"])
                if h:
                    for k in GPU_SERIES:
                        h[k].append(_r2(g.get(k)))

    def _loop(self):
        while True:
            self.record(self.sample())
            time.sleep(1.0)

    def snapshot(self):
        with self.lock:
            now = {k: v for k, v in self.now.items() if k not in SERIES and k != "slot_tok_s"}
            hist = {k: list(v) for k, v in self.hist.items()}
            # one series per slot, oldest first; a sample from before a slot existed is None
            n = max((len(x) for x in self.slot_hist), default=0)
            hist["slot_tok_s"] = [[x[j] if j < len(x) else None for x in self.slot_hist] for j in range(n)]
            hist["gpus"] = [{"index": i, **{k: list(v) for k, v in h.items()}} for i, h in self.gpu_hist.items()]
            return {"now": now, "history": hist, "static": dict(self.static)}

# ===== file: serve/util/text.py =====
"""Tokenizer helpers: the byte tokenizer used without a model, streaming detokenizing and stop strings."""
from __future__ import annotations

import codecs


IM_END = "<|im_end|>"


class ByteTokenizer:
    """Tiny stand-in tokenizer for tests without the pack: one id per UTF-8 byte, specials as ids >= 256."""
    SPECIALS = ["<|im_start|>", "<|im_end|>", "<|endoftext|>", "<|vision_start|>", "<|image_pad|>", "<|vision_end|>"]

    ALWAYS = ()                                     # specials matched without parse_special

    @property
    def control_tokens(self):
        return [s for s in self.SPECIALS if s not in self.ALWAYS]

    def encode(self, text, parse_special=False, plain=()):
        out, i = [], 0
        while i < len(text):
            for k, s in enumerate(self.SPECIALS):
                if (parse_special or s in self.ALWAYS) and text.startswith(s, i) and not any(
                        a <= i < b for a, b in plain):
                    out.append(256 + k)
                    i += len(s)
                    break
            else:
                out.extend(text[i].encode("utf-8"))
                i += 1
        return out

    def decode(self, ids, errors="replace"):
        raw = bytearray()
        for t in ids:
            raw += self.SPECIALS[t - 256].encode() if t >= 256 else bytes([t])
        return raw.decode("utf-8", errors=errors)


def control_literals(tok) -> list[str]:
    """The tokenizer's control tokens: the special literals it reads as tokens only when asked to parse them."""
    named = getattr(tok, "control_tokens", None)
    if named is not None:
        return list(named)
    return [s for s in getattr(tok, "special_tokens", ()) if s and
            tok.encode(s, parse_special=False) != tok.encode(s, parse_special=True)]


class Detokenizer:
    """Constant-time incremental decode: token bytes go through an incremental UTF-8 decoder that holds a split
    multi-byte character. A tokenizer without `token_bytes` re-decodes instead."""

    def __init__(self, tok):
        self.tok, self.ids, self.sent = tok, [], 0
        self.inc = codecs.getincrementaldecoder("utf-8")(errors="replace") if hasattr(tok, "token_bytes") else None

    def pending(self) -> bool:
        """A character is split across the tokens so far: its first bytes are held."""
        if self.inc is not None:
            return bool(self.inc.getstate()[0])
        return self.tok.decode(self.ids).endswith("\ufffd")

    def push(self, t: int) -> str:
        if self.inc is not None:
            return self.inc.decode(self.tok.token_bytes(t))
        self.ids.append(t)
        text = self.tok.decode(self.ids)
        if text.endswith("�"):
            return ""
        delta, self.sent = text[self.sent:], len(text)
        return delta


OPENAI_MAX_STOP = 4                                  # OpenAI's `stop` limit


def stop_strings(req: dict) -> list[str]:
    """The request's stop strings (OpenAI `stop` or Anthropic `stop_sequences`), empties dropped; ValueError if bad."""
    req = req or {}
    if req.get("stop") is not None:
        stop = req["stop"]
        stop = [stop] if isinstance(stop, str) else stop
        if not isinstance(stop, list) or not all(isinstance(x, str) for x in stop):
            raise ValueError("stop must be a string or a list of strings")
        if len(stop) > OPENAI_MAX_STOP:
            raise ValueError(f"stop takes at most {OPENAI_MAX_STOP} strings")
    elif req.get("stop_sequences") is not None:
        stop = req["stop_sequences"]
        if not isinstance(stop, list) or not all(isinstance(x, str) for x in stop):
            raise ValueError("stop_sequences must be a list of strings")
    else:
        return []
    return [x for x in stop if x]


class StopMatcher:
    """Cuts text at the first stop string, holding back only a tail that could start one."""

    def __init__(self, stops: list[str]):
        self.stops, self.held, self.hit = stops, "", None

    def push(self, text: str) -> str:
        """Return the text safe to send; after a match self.hit is the stop string and nothing more comes."""
        if self.hit is not None:
            return ""
        buf = self.held + text
        first = None
        for s in self.stops:
            i = buf.find(s)
            if i >= 0 and (first is None or i < first[0]):
                first = (i, s)
        if first is not None:
            self.held, self.hit = "", first[1]
            return buf[:first[0]]
        keep = 0                                      # longest end of buf that starts a stop string
        for s in self.stops:
            for k in range(min(len(s) - 1, len(buf)), keep, -1):
                if buf.endswith(s[:k]):
                    keep = k
                    break
        self.held = buf[len(buf) - keep:] if keep else ""
        return buf[:len(buf) - keep]

    def flush(self) -> str:
        held, self.held = self.held, ""
        return held

# ===== file: tools/check_benchmarks.py =====
#!/usr/bin/env python3
"""Validate docs/community/benchmarks.json (stdlib only). Exits 1 with per-row messages.

--fix rewrites a valid-JSON file in the canonical format, then validates it.
"""
import json
import re
import sys

REQUIRED = {"submitter", "date", "commit", "preset", "model", "gpus", "ram_gib", "os",
            "quant", "kv", "prefill_16k_tps", "prefill_64k_tps", "decode_tps"}
OPTIONAL = {"notes", "cpu", "ram_gbps"}
GPU_KEYS = {"name", "vram_gib", "power_w", "pcie", "h2d_gbps"}
PCIE = re.compile(r"[1-9](\.0)? x(1|2|4|8|16|32)")
PLACEHOLDER = "YOUR_NAME"
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
SYSTEMS = ("Linux", "Windows", "WSL2")
MODELS = ("Qwen3.8-Flash-Next", "DeepSeek-V4.1-Flash")   # the engine's model names (model_name of each adapter)
KV_FORMATS = ("fp16", "int8", "q4_0", "k8v4")            # the --kv values the engine accepts


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def is_pos(v):
    return is_num(v) and v > 0


def is_text(v):
    return isinstance(v, str) and v.strip() != ""


def check_gpu(g, k, err):
    if not isinstance(g, dict):
        err(f"gpus[{k}] must be an object")
        return
    for key in sorted(set(g) - GPU_KEYS):
        err(f"gpus[{k}]: unknown key '{key}'")
    if not is_text(g.get("name")):
        err(f"gpus[{k}].name must be a non-empty string")
    for key in ("vram_gib", "power_w"):
        if not is_pos(g.get(key)):
            err(f"gpus[{k}].{key} must be a number > 0")
    if "pcie" in g and not (isinstance(g["pcie"], str) and PCIE.fullmatch(g["pcie"])):
        err(f"gpus[{k}].pcie must look like '5.0 x16'")
    if "h2d_gbps" in g and not is_pos(g["h2d_gbps"]):
        err(f"gpus[{k}].h2d_gbps must be a number > 0")


def check_row(r, err):
    if not isinstance(r, dict):
        err("row must be an object")
        return
    for key in sorted(REQUIRED - set(r)):
        err(f"missing required key '{key}'")
    if "link" in r:
        err("'link' is no longer a field; remove it")
    for key in sorted(set(r) - REQUIRED - OPTIONAL - {"link"}):
        err(f"unknown key '{key}'")
    if "submitter" in r:
        if not is_text(r["submitter"]):
            err("submitter must be a non-empty string")
        elif r["submitter"].strip().upper() == PLACEHOLDER:
            err(f"submitter is still the placeholder {PLACEHOLDER}; use the name you want shown")
        elif len(r["submitter"]) > 40:
            err("submitter must be at most 40 characters")
        elif CONTROL.search(r["submitter"]):
            err("submitter must not contain control characters")
        elif re.search(r"\s", r["submitter"]):
            err("submitter must be one name with no spaces")
    if "date" in r:
        d = r["date"]
        ok = isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)
        if ok:
            try:
                import datetime
                datetime.date.fromisoformat(d)
            except ValueError:
                ok = False
        if not ok:
            err("date must be a real date as YYYY-MM-DD")
    if "commit" in r and not (isinstance(r["commit"], str) and re.fullmatch(r"[0-9a-fA-F]{7,40}", r["commit"])):
        err("commit must be 7-40 hex characters")
    if "preset" in r and not is_text(r["preset"]):
        err("preset must be a non-empty string")
    if "gpus" in r:
        g = r["gpus"]
        if not isinstance(g, list) or not g:
            err("gpus must be a non-empty list")
        else:
            for k, x in enumerate(g):
                check_gpu(x, k, err)
    for key in ("ram_gib", "prefill_16k_tps", "prefill_64k_tps", "decode_tps"):
        if key in r and not is_pos(r[key]):
            err(f"{key} must be a number > 0")
    if "ram_gbps" in r and not is_pos(r["ram_gbps"]):
        err("ram_gbps must be a number > 0")
    if "cpu" in r and not (is_text(r["cpu"]) and len(r["cpu"]) <= 80 and not CONTROL.search(r["cpu"])):
        err("cpu must be a non-empty string of at most 80 characters, without control characters")
    if "os" in r and r["os"] not in SYSTEMS:
        err("os must be one of " + ", ".join(SYSTEMS))
    if "quant" in r and not is_text(r["quant"]):
        err("quant must be a non-empty string")
    if "model" in r and r["model"] not in MODELS:
        err("model must be one of " + ", ".join(MODELS))
    if "kv" in r and r["kv"] not in KV_FORMATS:
        err("kv must be one of " + ", ".join(KV_FORMATS))
    if "notes" in r and not (isinstance(r["notes"], str) and len(r["notes"]) <= 200):
        err("notes must be a string of at most 200 characters")


ROW_ORDER = ("submitter", "date", "commit", "preset", "model", "gpus", "ram_gib", "cpu", "ram_gbps", "os",
             "quant", "kv", "prefill_16k_tps", "prefill_64k_tps", "decode_tps", "notes")
GPU_ORDER = ("name", "vram_gib", "power_w", "pcie", "h2d_gbps")
TOP_ORDER = ("preset", "runs")
FIX_HINT = "python3 tools/check_benchmarks.py --fix docs/community/benchmarks.json"


def ordered(d, order):
    """Known keys in the fixed order, unknown keys after them in file order."""
    if not isinstance(d, dict):
        return d
    return {**{k: d[k] for k in order if k in d}, **{k: v for k, v in d.items() if k not in order}}


def canonical(data):
    """The one accepted file form: fixed key order, 2-space indent, no trailing newline."""
    data = ordered(data, TOP_ORDER)
    if isinstance(data.get("runs"), list) if isinstance(data, dict) else False:
        rows = []
        for r in data["runs"]:
            r = ordered(r, ROW_ORDER)
            if isinstance(r, dict) and isinstance(r.get("gpus"), list):
                r["gpus"] = [ordered(g, GPU_ORDER) for g in r["gpus"]]
            rows.append(r)
        data["runs"] = rows
    return json.dumps(data, indent=2, ensure_ascii=False)


def validate(data):
    problems = []
    if not isinstance(data, dict):
        problems.append("top level must be an object")
    else:
        if not is_text(data.get("preset")):
            problems.append("top-level 'preset' must be a non-empty string")
        if not isinstance(data.get("runs"), list):
            problems.append("top-level 'runs' must be a list")
        for key in sorted(set(data) - {"preset", "runs"}):
            problems.append(f"unknown top-level key '{key}'")
        for i, r in enumerate(data["runs"] if isinstance(data.get("runs"), list) else []):
            who = r.get("submitter") if isinstance(r, dict) else None
            label = f"runs[{i}]" + (f" ({who})" if isinstance(who, str) and who else "")
            check_row(r, lambda m, label=label: problems.append(f"{label}: {m}"))
    return problems


def main(argv):
    args = argv[1:]
    fix = "--fix" in args
    args = [a for a in args if a != "--fix"]
    path = args[0] if args else "docs/community/benchmarks.json"
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        data = json.loads(text)
    except (OSError, ValueError) as e:
        print(f"{path}: {e}")
        return 1
    problems = validate(data)
    want = canonical(data)
    if fix and text != want:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(want)
        print(f"{path}: rewritten in the canonical format")
        text = want
    elif text != want:
        problems.append(f"not in the canonical format; run: {FIX_HINT}")
    for p in problems:
        print(f"{path}: {p}")
    if problems:
        return 1
    print(f"{path}: ok, {len(data['runs'])} runs")
    return 0


sys.exit(main(sys.argv))

# ===== file: tools/check_isa_isolation.py =====
#!/usr/bin/env python3
"""Fails when AVX-512 code can run on a host that has no AVX-512.

Two things run without a CPU check: any object that is not a dedicated AVX-512 kernel file, and the
static initializers of every object (they run before main). So the check fails on an AVX-512
instruction (EVEX prefix) in either of those. A function that tests MSVC's `__isa_available` before
its vector loop is already guarded by the compiler and passes. Usage: check_isa_isolation.py BUILD_DIR [--list]
"""
import pathlib
import re
import shutil
import subprocess
import sys

# Objects built with AVX-512 flags on purpose; their kernels are reached through a run-time CPU check.
AVX512_OBJECTS = ("iq_avx512", "nvfp4_avx512", "ggml_dots_avx512", "gemm_cpu_avx512", "src/kernels/cpu/expert.cpp")
# EVEX-encoded instructions start with byte 0x62 in 64-bit mode, whatever registers they name.
# A long instruction wraps onto continuation lines that carry no mnemonic, so the mnemonic is required.
EVEX = re.compile(r"^\s*[0-9a-f]+:[\t ]+62 [0-9a-f ]*\t\s*[a-z]")
STATIC_INIT = re.compile(r"^\?\?__E|^_GLOBAL__sub_I_|^__cxx_global_var_init|^_GLOBAL__I_")


def disassembler():
    for name in ("llvm-objdump", "objdump"):
        path = shutil.which(name)
        if path:
            return path
    win = pathlib.Path("C:/Program Files/LLVM/bin/llvm-objdump.exe")
    return str(win) if win.exists() else sys.exit("no objdump found")


def scan(tool, obj):
    out = subprocess.run([tool, "-d", "-r", str(obj)], capture_output=True, text=True, check=False).stdout
    sym = None
    found = {}
    guarded = set()
    for line in out.splitlines():
        m = re.match(r"[0-9a-f]+ <(.+)>:", line)
        if m:
            sym = m.group(1)
        elif sym and "__isa_available" in line:
            guarded.add(sym)
        elif sym and EVEX.match(line):
            found.setdefault(sym, [0, line.strip()])[0] += 1
    return {k: v for k, v in found.items() if k not in guarded}


def main():
    build = pathlib.Path(sys.argv[1])
    listing = "--list" in sys.argv
    tool = disassembler()
    bad = 0
    for obj in sorted(list(build.rglob("*.o")) + list(build.rglob("*.obj"))):
        dedicated = any(k in obj.as_posix() for k in AVX512_OBJECTS)
        for sym, (n, first) in scan(tool, obj).items():
            wrong = not dedicated or bool(STATIC_INIT.match(sym))
            if wrong or listing:
                print(f"{'WRONG' if wrong else 'kernel'} {obj.name} {sym} ({n}) e.g. {first}")
            bad += wrong
    print(f"{bad} function(s) outside the dispatched kernels use AVX-512")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

# ===== file: tools/check_model_layering.py =====
#!/usr/bin/env python3
"""Layering of the models, what grep cannot see: CMake targets and the names and token ids shared code may not hold."""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODELS = ("qwen4exp", "deepseek41")
NAMES = re.compile(r"qwen4exp|deepseek41|Flash-Next|DeepSeek|Qwen3\.8|\b(248044|248045|248046|248056|248058|248059|248320)\b")
status = 0


def fail(title, items):
    global status
    if items:
        status = 1
        print("model layering: " + title + ":", file=sys.stderr)
        for i in items:
            print("  " + i, file=sys.stderr)


def statements(text, verb):
    for m in re.finditer(r"\b" + verb + r"\(([^)]*)\)", re.sub(r"#[^\n]*", "", text)):
        words = m.group(1).split()
        if words:
            yield words[0], words[1:]


files = [ROOT / "CMakeLists.txt"] + sorted(ROOT.glob("src/models/*/**/*.cmake")) + sorted(ROOT.glob("src/models/*/*.cmake"))
text = "\n".join(f.read_text(encoding="utf-8") for f in dict.fromkeys(files))


def owner(target):
    """The model a target belongs to; None when it is shared."""
    for m in MODELS:
        if target.startswith(("model_" + m, m)):
            return m
    return None


def is_test(target, sources):
    return any("tests/" in s for s in sources) or target.endswith(("_test", "_parity", "_bench")) or "${" in target


def own_tool(sources):
    """Every source in one model's folder: the target is that model's."""
    return bool(sources) and all(s.startswith("src/models/") for s in sources if s.startswith("src/"))


targets = {}
for verb in ("add_library", "add_executable"):
    for name, sources in statements(text, verb):
        targets[name] = sources

for name, sources in targets.items():
    if name == "models" or owner(name) or is_test(name, sources) or own_tool(sources):
        continue
    fail("shared target " + name + " builds a model's sources", [s for s in sources if s.startswith("src/models/")])

for name, words in statements(text, "target_link_libraries"):
    who = owner(name)
    if name not in targets or is_test(name, targets[name]) or name == "models" or name.startswith("basalt"):
        continue
    bad = [w for w in words if owner(w) and owner(w) != who]
    fail("target " + name + " links another model's library", bad)

tracked = subprocess.check_output(["git", "ls-files", "src", "include"], cwd=ROOT, text=True).split()
hits = []
for f in tracked:
    if f.startswith(("src/models/", "include/basalt/models/")) or not f.endswith((".cpp", ".hpp", ".cu", ".cuh", ".h", ".inc")):
        continue
    for n, line in enumerate((ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if NAMES.search(line):
            hits.append(f"{f}:{n}: {line.strip()[:100]}")
fail("shared code names a model or its token ids", hits)

# A model's widths in shared code: what a name check cannot see. Each allowed file says why.
WIDTHS = re.compile(r"\b(2560|10240|1382400|1,382,400)\b|\bFF = 640\b|\b48 ?[x*] ?512\b")
WIDTH_OK = {
    "include/basalt/program/options.hpp": "2560 MiB, a RAM floor",
    "src/program/cli/help.cpp": "2560 MiB, a RAM floor",
    "src/kernels/cuda/gu2.cuh": "a shared kernel's instance for this width; other widths take the other path",
    "src/prefill/moe_mmq.cu": "the Q2_0 blob layout of that format's instance",
    "include/basalt/prefill/moe_mmq.hpp": "the Q2_0 blob layout of that format's instance",
    "include/basalt/kernels/cpu/expert.hpp": "the capacity of the Q8_1 activation block, not a model width",
    "src/kernels/cpu/kq_avx2.cpp": "a loop bound in a comment",
}
hits = []
for f in tracked:
    if f.startswith(("src/models/", "include/basalt/models/", "include/basalt/ggml/", "src/kernels/cpu/ggml/")) or f in WIDTH_OK:
        continue
    if not f.endswith((".cpp", ".hpp", ".cu", ".cuh", ".h", ".inc")):
        continue
    for n, line in enumerate((ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if WIDTHS.search(line):
            hits.append(f"{f}:{n}: {line.strip()[:100]}")
fail("shared code holds a model's width", hits)

py = subprocess.check_output(["git", "ls-files", "serve"], cwd=ROOT, text=True).split()
hits = []
for f in py:
    if not f.endswith(".py") or f.startswith(("serve/models/", "serve/tests/")):
        continue
    for n, line in enumerate((ROOT / f).read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if re.search(r"qwen4exp|deepseek41|\b24804[456]\b", line):
            hits.append(f"{f}:{n}: {line.strip()[:100]}")
fail("the server names a model outside serve/models", hits)
sys.exit(status)

# ===== file: tools/convert/arch/__init__.py =====
"""Per-architecture conversion rules for tools/convert/convert.py, keyed by the GGUF's `general.architecture`.

An architecture says how its source tensors are stored in the pack (the float form the engine reads, tensors that get
a section of their own), which optional sections it carries, and the license, sampling defaults and extra `meta` keys.
Everything else (tokenizer, dense/gguf, experts, container) is shared.
"""
from __future__ import annotations

import re


class Arch:
    name = ""
    # float form the engine reads each dense tensor in (name without `blk.N.`); empty = tensors are stored as they are
    form_table: dict[str, str] = {}
    # tensors kept out of the dense pool (they are served from `gguf` or from a section of their own)
    not_in_dense: frozenset = frozenset()
    # tensor name -> section that holds it alone (2 MiB aligned); such tensors are left out of `gguf`
    own_sections: dict[str, str] = {}
    # drafter, hc8 and vision sections
    side_sections = True
    # only the vision projector section (vision/mmproj), written when --mmproj is given
    mmproj_only = False
    projector_type = ""
    # takes a DSpark drafter GGUF (--dspark) as the dspark/gguf section
    dspark_ok = False
    # the model's sampling defaults, for a GGUF that does not carry them (llama.cpp's general.sampling.* keys)
    sampling_kv: dict = {}

    def form_of(self, name: str):
        return self.form_table.get(re.sub(r"^blk\.\d+\.", "", name))

    def native(self, name: str, type_name: str) -> bool:
        """A quantized tensor the engine reads from the source bytes as they are."""
        return self.form_of(name) is None

    def license(self, kv: dict) -> dict:
        raise NotImplementedError

    def default_name(self, first_gguf, kv: dict, sections_from: bool = False) -> str:
        raise NotImplementedError

    def meta_extra(self, kv: dict, tensors: dict, own: dict) -> dict:
        """Keys added to `meta`: `tensors` are the `gguf` tensors' entries, `own` the own-section tensors' ones."""
        return {}

    def check(self, kv: dict, own: dict) -> None:
        """Raise ConvertError when the source does not fit this architecture."""


REGISTRY: dict[str, Arch] = {}


def register(arch: Arch, *names: str) -> None:
    for n in names or (arch.name,):
        REGISTRY[n] = arch


def for_architecture(name) -> Arch:
    """The rules for `general.architecture`; an architecture without its own entry is converted as Flash-Next."""
    from . import qwen4exp, deepseek41  # noqa: F401  (register themselves)
    return REGISTRY.get(name) or REGISTRY["qwen4exp"]

# ===== file: tools/convert/arch/deepseek41.py =====
"""DeepSeek-V4.1-Flash (GGUF architecture `deepseek41`).

Every tensor is stored as published: the float tensors in `dense`, the quantized non-expert tensors in `gguf`, the routed
experts in `experts`, and the Engram table (`per_layer_token_embd.weight`, rows of 256 IQ4_NL values) in a section of its
own so it can be read by row with O_DIRECT. There is no hc8 section. `--mmproj` adds the vision
projector GGUF as the `vision/mmproj` section, `--dspark` the DSpark drafter GGUF as `dspark/gguf` (meta.dspark).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import Arch, register

ENGRAM_TENSOR = "per_layer_token_embd.weight"
ENGRAM_SECTION = "engram"
ENGRAM_ROW_VALUES = 256
IQ4_NL = 20
IQ4_NL_BLOCK = (32, 18)       # values, bytes

# The LICENSE file of the official model repository, with LF line endings.
MIT = """MIT License

Copyright (c) 2023 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

P = "deepseek41."
# GGUF keys of the geometry, copied to meta.geometry under the key without the prefix
GEOMETRY_KEYS = (
    "block_count", "context_length", "embedding_length", "attention.head_count", "attention.head_count_kv",
    "attention.key_length", "attention.value_length", "attention.q_lora_rank", "attention.sliding_window",
    "attention.output_group_count", "attention.output_lora_rank", "rope.dimension_count", "rope.freq_base",
    "attention.compress_rope_freq_base", "rope.scaling.factor", "rope.scaling.original_context_length",
    "attention.layer_norm_rms_epsilon",
    "expert_count", "expert_used_count", "expert_shared_count", "expert_feed_forward_length", "expert_gating_func",
    "expert_weights_scale", "expert_weights_norm", "swiglu_clamp_exp", "swiglu_clamp_shexp",
    "attention.indexer.head_count", "attention.indexer.key_length", "attention.indexer.top_k",
    "attention.compress_ratios", "hyper_connection.count", "hyper_connection.sinkhorn_iterations",
    "hyper_connection.epsilon", "embedding_length_out", "hash_layer_count",
)
# the hash and layout constants of the Engram row lookup, copied to meta.engram (the token map stays in gguf_kv)
ENGRAM_KEYS = (
    "ple.layers", "ple.ngram_size", "ple.heads_per_ngram", "ple.layer_multipliers", "ple.head_offsets",
    "ple.head_vocab_sizes", "embedding_length_per_layer_input", "engram.vocab_size", "engram.pad_token_id",
    "engram.compressed_vocab_size",
)


class DeepSeek41(Arch):
    name = "deepseek41"
    not_in_dense = frozenset({ENGRAM_TENSOR})
    own_sections = {ENGRAM_TENSOR: ENGRAM_SECTION}
    side_sections = False
    mmproj_only = True
    dspark_ok = True
    projector_type = "deepseek41v"
    sampling_kv = {"general.sampling.temp": 1.0, "general.sampling.top_p": 1.0, "general.sampling.top_k": 0}

    def license(self, kv):
        lic = {"name": "MIT", "text": MIT}
        if kv.get("general.quantized_by"):
            lic["quantized_by"] = kv["general.quantized_by"]
        return lic

    def default_name(self, first_gguf, kv, sections_from=False):
        base = kv.get("general.name") or "DeepSeek-V4.1-Flash"
        stem = re.sub(r"-\d{5}-of-\d{5}\.gguf$", "", Path(first_gguf).name)
        tail = stem[len(base):].strip("-_ ") if stem.startswith(base) else ""
        return f"{base} {tail}" if tail else base

    def check(self, kv, own):
        from model_sections import ConvertError
        t = own.get(ENGRAM_TENSOR)
        if t is None:
            raise ConvertError(f"the model has no {ENGRAM_TENSOR} (the Engram table)")
        if t.type_id != IQ4_NL or len(t.shape) != 2 or int(t.shape[0]) != ENGRAM_ROW_VALUES:
            raise ConvertError(f"{ENGRAM_TENSOR} is {t.type_name} {list(t.shape)}; expected rows of "
                               f"{ENGRAM_ROW_VALUES} IQ4_NL values")
        want = kv.get(P + "embedding_length_per_layer_input")
        if want is not None and int(want) != ENGRAM_ROW_VALUES:
            raise ConvertError(f"embedding_length_per_layer_input is {want}, the Engram rows hold {ENGRAM_ROW_VALUES}")
        sizes = kv.get(P + "ple.head_vocab_sizes")
        if sizes and sum(int(x) for x in sizes) != int(t.shape[1]):
            raise ConvertError(f"the Engram heads hold {sum(int(x) for x in sizes)} rows, the table {int(t.shape[1])}")

    def meta_extra(self, kv, tensors, own):
        t = own[ENGRAM_TENSOR]
        vals, nbytes = IQ4_NL_BLOCK
        row_bytes = ENGRAM_ROW_VALUES // vals * nbytes
        return {
            "arch": self.name,
            "geometry": {k: kv[P + k] for k in GEOMETRY_KEYS if P + k in kv},
            "engram": {"section": ENGRAM_SECTION, "tensor": ENGRAM_TENSOR, "type": t.type_name, "type_id": t.type_id,
                       "rows": int(t.shape[1]), "row_values": ENGRAM_ROW_VALUES, "row_bytes": row_bytes,
                       "bytes": int(t.shape[1]) * row_bytes,
                       **{k: kv[P + k] for k in ENGRAM_KEYS if P + k in kv},
                       "token_map": "gguf_kv:" + P + "engram.token_map"},
        }


register(DeepSeek41())

# ===== file: tools/convert/arch/qwen4exp.py =====
"""Flash-Next (and any architecture without its own entry)."""
from __future__ import annotations

from pathlib import Path

from . import Arch, register

LICENSE_TEXT = (Path(__file__).resolve().parent.parent / "qwen-community-license-1.0.txt").read_text(encoding="utf-8")

# float form the engine reads each dense tensor in; other forms are converted and recorded in meta.conversions:
#   F32 -> BF16 when exactly BF16 (low 16 bits zero)              exact, by default
#   other F32, F16 or quantized -> BF16                           rounds (nearest-even): only with --compat-bf16
#   F32 -> F16 (ple_conv1d)                                       index kind 3, narrowed by the loader
#   F16 / BF16 -> F32                                             exact widening
# tensors not listed are written as stored (floats) or served from the `gguf` section (quantized)
FORM = {
    "ffn_gate_inp.weight": "BF16", "ffn_gate_inp_shexp.weight": "BF16",
    "hc_attn_down.weight": "BF16", "hc_attn_up.weight": "BF16", "hc_attn_inject.weight": "BF16",
    "hc_ffn_down.weight": "BF16", "hc_ffn_up.weight": "BF16", "hc_ffn_inject.weight": "BF16",
    "output_hc_down.weight": "BF16", "output_hc_up.weight": "BF16",
    "indexer.k_proj.weight": "BF16", "indexer.q_proj.weight": "BF16",
    "ssm_alpha.weight": "BF16", "ssm_beta.weight": "BF16",
    "ple_key.weight": "BF16", "ple_value.weight": "BF16", "ple_conv1d.weight": "F16",
    "attn_q_norm.weight": "F32", "attn_k_norm.weight": "F32", "hc_attn_norm.weight": "F32",
    "hc_ffn_norm.weight": "F32", "output_hc_norm.weight": "F32", "indexer.q_norm.weight": "F32",
    "indexer.k_norm.weight": "F32", "ple_norm_conv.weight": "F32", "ple_norm_key.weight": "F32",
    "ple_norm_query.weight": "F32", "ssm_a": "F32", "ssm_conv1d.weight": "F32", "ssm_dt.bias": "F32",
    "ssm_norm.weight": "F32",
}
# PLE key encodings the engine reads natively; other quantized keys need --compat-bf16
NATIVE_PLE_KEY = {"Q2_0", "Q8_0"}


class Qwen4Exp(Arch):
    name = "qwen4exp"
    form_table = FORM
    # the PLE table comes from the `gguf` section, never the dense pool
    not_in_dense = frozenset({"per_layer_token_embd.weight"})
    sampling_kv = {"general.sampling.temp": 1.0, "general.sampling.top_p": 0.95, "general.sampling.top_k": 20}

    def native(self, name, type_name):
        return self.form_of(name) is None or (name == "blk.1.ple_key.weight" and type_name in NATIVE_PLE_KEY)

    def license(self, kv):
        """The model's license, with its full text (the license requires it in every copy)."""
        lic = {"name": "Qwen Community License 1.0", "text": LICENSE_TEXT}
        if kv.get("general.quantized_by"):
            lic["quantized_by"] = kv["general.quantized_by"]
        return lic

    def default_name(self, first_gguf, kv, sections_from=False):
        if sections_from:
            return "Qwen3.8-Flash-Next " + Path(first_gguf).name.split("Next-")[-1].split("-0000")[0]
        return "Qwen3.8-Flash-Next IQ3_XXS"


register(Qwen4Exp())

# ===== file: tools/convert/checkpoint.py =====
"""A .basalt pack's language-model sections read straight from an NVFP4 safetensors checkpoint (NVIDIA Model
Optimizer's layout, as in nvidia/Qwen3.8-Flash-Next-NVFP4), for tools/convert/convert.py.

  experts / experts.layout  each routed expert's NVFP4 gate | up | down rows moved into Basalt's blocks (codes and
                            UE4M3 scales unchanged) with the FP32 tail {s_gate, s_up, s_down, 0} from `weight_scale_2`
  gguf, dense, dense.index  every other language-model tensor in the GGUF names, order and forms the engine reads:
                            BF16 kept, norms as F32 with the "+1" folded in, GDN value heads in the GGUF head order,
                            projections quantized once from BF16 (Q6_K, or Q8_0 where rows are not whole 256-value
                            blocks, or for the 8-bit variant)
  gguf_kv, tokenizer        the model metadata and tokenizer from config.json, generation_config.json, README.md and
                            the tokenizer files

The checkpoint's tensors are visited in file order, each file in header order, so the output order follows the
checkpoint. Needs numpy.
"""
from __future__ import annotations

import json
import pathlib
import re
import struct

import numpy as np

import model_sections as S

NVFP4 = S.NVFP4
UNUSED, NORMAL, CONTROL, USER_DEFINED = 5, 1, 3, 4
# The pre-tokenizer split of the qwen35 vocabulary as Hugging Face tokenizers writes it.
QWEN35_HF_SPLIT = (r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?[\p{L}\p{M}]+|\p{N}"
                   r"| ?[^\s\p{L}\p{M}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+")
VALUES_PER_JOB = 1 << 22
TYPE_ID = {"F32": 0, "F16": 1, "Q8_0": 8, "Q6_K": 14, "BF16": 30}


class Checkpoint:
    """The safetensors files of a checkpoint directory: name -> (path, absolute offset, dtype, shape, bytes)."""

    def __init__(self, d):
        self.dir = pathlib.Path(d)
        cfg = self.dir / "config.json"
        if not cfg.is_file():
            raise S.ConvertError(f"{cfg} not found")
        self.config = json.loads(cfg.read_text(encoding="utf-8"))
        self.text = self.config.get("text_config", self.config)
        self.files = sorted(self.dir.glob("*.safetensors"))
        self.shards = [p for p in self.files if re.fullmatch(r"model-\d+-of-\d+\.safetensors", p.name)]
        if not self.shards:
            raise S.ConvertError(f"{self.dir}: no model-NNNNN-of-NNNNN.safetensors shards")
        self.t, self.order = {}, []
        for p in self.files:
            head, base = S.read_safetensors_header(p)
            items = sorted(((k, v) for k, v in head.items() if k != "__metadata__"),
                           key=lambda kv: kv[1]["data_offsets"][0])
            for k, v in items:
                if k in self.t:
                    raise S.ConvertError(f"{p.name}: duplicate tensor {k}")
                a, b = v["data_offsets"]
                self.t[k] = (p, base + a, v["dtype"], list(v["shape"]), b - a)
                if p in self.shards:
                    self.order.append(k)
                elif not SKIP.match(k):
                    raise S.ConvertError(f"{p.name} is not a model-NNNNN-of-NNNNN shard and holds {k}, which would be "
                                         "dropped; only the n-gram table, drafter and vision tensors may live there")

    def raw(self, name) -> np.ndarray:
        p, off, _, _, n = self.t[name]
        return np.fromfile(p, dtype=np.uint8, count=n, offset=off)

    def values(self, name) -> np.ndarray:
        """A BF16, F32 or I64 tensor as float32 (int64 for I64), shaped."""
        p, off, dt, shape, n = self.t[name]
        raw = self.raw(name)
        if dt == "BF16":
            return S._bf16_to_f32(raw.view("<u2")).reshape(shape)
        if dt == "F32":
            return raw.view("<f4").reshape(shape)
        if dt == "I64":
            return raw.view("<i8").reshape(shape)
        raise S.ConvertError(f"{name} is {dt}")

    def need(self, name, dtype, shape=None):
        if name not in self.t:
            raise S.ConvertError(f"the checkpoint has no {name}")
        _, _, dt, sh, _ = self.t[name]
        if dt != dtype or (shape is not None and sh != list(shape)):
            raise S.ConvertError(f"{name} is {dt} {sh}, expected {dtype} {list(shape) if shape else ''}")


# ------------------------------------------------------------------ routed experts
def nvfp4_rows(codes: np.ndarray, scales: np.ndarray) -> np.ndarray:
    """Model Optimizer's NVFP4 rows -> Basalt's blocks. `codes` [rows, cols/2]: value 2i in the low nibble of byte i,
    2i+1 in the high one; `scales` [rows, cols/16] UE4M3. A Basalt block of 64 values is 4 scale bytes then 32 code
    bytes where sub-block s keeps value j in the low nibble of byte 8 s + j and value j + 8 in its high nibble."""
    rows, cols = codes.shape[0], codes.shape[1] * 2
    v = np.empty((rows, cols), np.uint8)
    v[:, 0::2] = codes & 15
    v[:, 1::2] = codes >> 4
    v = v.reshape(rows, cols // 64, 4, 16)
    out = np.empty((rows, cols // 64, 36), np.uint8)
    out[..., :4] = scales.reshape(rows, cols // 64, 4)
    out[..., 4:] = (v[..., :8] | (v[..., 8:] << 4)).reshape(rows, cols // 64, 32)
    return out.reshape(-1)


def _expert_job(args):
    """Blobs of experts [s, s + n) of one layer: [gate | up | down] blocks and the FP32 scale tail."""
    entries, n = args
    out = []
    for e in range(n):
        parts, tail = [], []
        for (wp, wo, wshape), (sp, so, sshape), (tp, to) in entries[e]:
            w = np.fromfile(wp, dtype=np.uint8, count=wshape[0] * wshape[1], offset=wo).reshape(wshape)
            sc = np.fromfile(sp, dtype=np.uint8, count=sshape[0] * sshape[1], offset=so).reshape(sshape)
            if (sc & 0x80).any() or (sc == 0x7F).any():
                return f"a UE4M3 scale with the sign bit set or NaN at {wp.name}+{wo}"
            parts.append(nvfp4_rows(w, sc))
            tail.append(np.fromfile(tp, dtype="<f4", count=1, offset=to)[0])
        t = np.array(tail + [0.0], dtype="<f4")
        if not (np.isfinite(t).all() and (t[:3] > 0).all()):
            return f"a weight_scale_2 that is not finite and positive: {t[:3].tolist()}"
        out.append(np.concatenate(parts).tobytes())
        out.append(t.tobytes())
    return b"".join(out)


bounded_imap = S.bounded_imap


class CheckpointExperts:
    """The `experts` section from the checkpoint's per-expert NVFP4 tensors (`input_scale` is not used)."""

    ROLES = ("gate_proj", "up_proj", "down_proj")

    def __init__(self, ck: Checkpoint):
        tc = ck.text
        self.ck = ck
        self.n_layers, self.n_expert = int(tc["num_hidden_layers"]), int(tc["num_experts"])
        h, ff = int(tc["hidden_size"]), int(tc["moe_intermediate_size"])
        shapes = {"gate_proj": (ff, h), "up_proj": (ff, h), "down_proj": (h, ff)}
        self.blob = 2 * ff * S.nvfp4_row_bytes(h) + h * S.nvfp4_row_bytes(ff) + S.NVFP4_TAIL
        known = {f"{r}.{k}" for r in self.ROLES for k in ("weight", "weight_scale", "weight_scale_2", "input_scale")}
        for k in ck.t:
            m = re.fullmatch(r"model\.language_model\.layers\.(\d+)\.mlp\.experts\.(\d+)\.(.+)", k)
            if m and (int(m.group(1)) >= self.n_layers or int(m.group(2)) >= self.n_expert or m.group(3) not in known):
                raise S.ConvertError(f"unexpected expert tensor {k} (the model has {self.n_layers} layers of "
                                     f"{self.n_expert} experts)")
        self.entries = []
        for l in range(self.n_layers):
            layer = []
            for e in range(self.n_expert):
                ex = []
                for r in self.ROLES:
                    p = f"model.language_model.layers.{l}.mlp.experts.{e}.{r}."
                    rows, cols = shapes[r]
                    ck.need(p + "weight", "U8", (rows, cols // 2))
                    ck.need(p + "weight_scale", "F8_E4M3", (rows, cols // 16))
                    ck.need(p + "weight_scale_2", "F32", ())
                    w, s, t = (ck.t[p + k] for k in ("weight", "weight_scale", "weight_scale_2"))
                    ex.append(((w[0], w[1], w[3]), (s[0], s[1], s[3]), (t[0], t[1])))
                layer.append(ex)
            self.entries.append(layer)
        self.layout = [(l, NVFP4, NVFP4, l * self.blob * self.n_expert, self.blob) for l in range(self.n_layers)]
        self.total = self.n_layers * self.n_expert * self.blob

    def layout_text(self) -> bytes:
        return S.nvfp4_layout_text(self.n_expert, self.total, self.layout)

    def jobs(self, group: int):
        for l in range(self.n_layers):
            for s in range(0, self.n_expert, group):
                n = min(group, self.n_expert - s)
                yield self.entries[l][s:s + n], n

    def chunks(self, pool, group: int = 32, depth: int = 32):
        for b in bounded_imap(pool, _expert_job, self.jobs(group), depth):
            if isinstance(b, str):
                raise S.ConvertError(b)
            yield b


# ------------------------------------------------------------------ every other language-model tensor
# How each checkpoint tensor becomes a GGUF tensor: (GGUF name, stored type, transform). "MAT" is a projection
# quantized from BF16 (Q6_K when its rows are whole 256-value blocks and Q6_K is asked for, else Q8_0).
#   id     the values as they are             norm   + 1 (the RMSNorm weights are stored as offsets from 1)
#   heads  rows in the GGUF value-head order  negexp -exp, in the GGUF value-head order (A from A_log)
#   conv   [C, 1, K] -> [C, K]                gconv  conv, value-channel rows in the GGUF order
#   vrows  rows after the q and k parts in the GGUF value-head order;  zrows  all rows in it;  vcols  columns in it
LAYER = {
    "attn_hyper_connection.block_inject_weight.weight": ("hc_attn_inject.weight", "BF16", "id"),
    "attn_hyper_connection.hc_norm.weight": ("hc_attn_norm.weight", "F32", "norm"),
    "attn_hyper_connection.input_mix_weight_down.weight": ("hc_attn_down.weight", "BF16", "id"),
    "attn_hyper_connection.input_mix_weight_up.weight": ("hc_attn_up.weight", "BF16", "id"),
    "mlp_hyper_connection.block_inject_weight.weight": ("hc_ffn_inject.weight", "BF16", "id"),
    "mlp_hyper_connection.hc_norm.weight": ("hc_ffn_norm.weight", "F32", "norm"),
    "mlp_hyper_connection.input_mix_weight_down.weight": ("hc_ffn_down.weight", "BF16", "id"),
    "mlp_hyper_connection.input_mix_weight_up.weight": ("hc_ffn_up.weight", "BF16", "id"),
    "linear_attn.A_log": ("ssm_a", "F32", "negexp"),
    "linear_attn.conv1d.weight": ("ssm_conv1d.weight", "F32", "gconv"),
    "linear_attn.dt_bias": ("ssm_dt.bias", "F32", "heads"),
    "linear_attn.in_proj_a.weight": ("ssm_alpha.weight", "BF16", "heads"),
    "linear_attn.in_proj_b.weight": ("ssm_beta.weight", "BF16", "heads"),
    "linear_attn.in_proj_qkv.weight": ("attn_qkv.weight", "MAT", "vrows"),
    "linear_attn.in_proj_z.weight": ("attn_gate.weight", "MAT", "zrows"),
    "linear_attn.norm.weight": ("ssm_norm.weight", "F32", "id"),
    "linear_attn.out_proj.weight": ("ssm_out.weight", "MAT", "vcols"),
    "mlp.gate.weight": ("ffn_gate_inp.weight", "BF16", "id"),
    "mlp.shared_expert.down_proj.weight": ("ffn_down_shexp.weight", "MAT", "id"),
    "mlp.shared_expert.gate_proj.weight": ("ffn_gate_shexp.weight", "MAT", "id"),
    "mlp.shared_expert.up_proj.weight": ("ffn_up_shexp.weight", "MAT", "id"),
    "mlp.shared_expert_gate.weight": ("ffn_gate_inp_shexp.weight", "BF16", "id"),
    "ple.conv1d.weight": ("ple_conv1d.weight", "F16", "conv"),
    "ple.key_proj.weight": ("ple_key.weight", "BF16", "id"),
    "ple.value_proj.weight": ("ple_value.weight", "BF16", "id"),
    "ple.norm_conv.weight": ("ple_norm_conv.weight", "F32", "norm"),
    "ple.norm_key.weight": ("ple_norm_key.weight", "F32", "norm"),
    "ple.norm_query.weight": ("ple_norm_query.weight", "F32", "norm"),
    "self_attn.indexer.k_layernorm.weight": ("indexer.k_norm.weight", "F32", "norm"),
    "self_attn.indexer.q_layernorm.weight": ("indexer.q_norm.weight", "F32", "norm"),
    "self_attn.k_norm.weight": ("attn_k_norm.weight", "F32", "norm"),
    "self_attn.q_norm.weight": ("attn_q_norm.weight", "F32", "norm"),
    "self_attn.k_proj.weight": ("attn_k.weight", "MAT", "id"),
    "self_attn.o_proj.weight": ("attn_output.weight", "MAT", "id"),
    "self_attn.q_proj.weight": ("attn_q.weight", "MAT", "id"),
    "self_attn.v_proj.weight": ("attn_v.weight", "MAT", "id"),
}
# the indexer's fused projection: its first rows are q (heads x head width), the rest k
INDEXER_QK = "self_attn.indexer.index_qk_proj.weight"
GLOBAL = {
    "lm_head.weight": ("output.weight", "Q8_0", "id"),
    "model.language_model.embed_tokens.weight": ("token_embd.weight", "Q8_0", "id"),
    "model.language_model.hyper_connection_mixer.hc_norm.weight": ("output_hc_norm.weight", "F32", "norm"),
    "model.language_model.hyper_connection_mixer.input_mix_weight_down.weight": ("output_hc_down.weight", "BF16", "id"),
    "model.language_model.hyper_connection_mixer.input_mix_weight_up.weight": ("output_hc_up.weight", "BF16", "id"),
}
# read for the metadata or written elsewhere (experts, the n-gram table); the drafter and vision come from the base pack
SKIP = re.compile(r"^(mtp\.|model\.visual\.|model\.language_model\.layers\.\d+\.(mlp\.experts\.|ple\.ple_embedding\.))")
METHODS = {
    "id": "as stored",
    "norm": "bf16 + 1 in f32 (exact)",
    "heads": "value-head rows in the GGUF order",
    "negexp": "-exp(A_log), computed in f64 and rounded once to f32, value heads in the GGUF order",
    "conv": "[C, 1, K] -> [C, K]",
    "gconv": "[C, 1, K] -> [C, K], value channels in the GGUF head order",
    "vrows": "rows after q and k in the GGUF value-head order",
    "zrows": "rows in the GGUF value-head order",
    "vcols": "columns in the GGUF value-head order",
    "split": "the fused indexer projection split into q and k rows",
}


class Tensor:
    """One GGUF tensor made from a checkpoint tensor: `ne` is the GGUF shape (fastest dimension first)."""

    def __init__(self, name, type_name, src, how, ne, rows=None):
        self.name, self.type_name, self.src, self.how, self.ne, self.rows = name, type_name, src, how, ne, rows
        self.type_id = TYPE_ID[type_name]
        self.shape = ne

    def expected_bytes(self) -> int:
        n = int(np.prod(self.ne))
        return {"F32": 4 * n, "F16": 2 * n, "BF16": 2 * n, "Q8_0": n // 32 * 34, "Q6_K": n // 256 * 210}[self.type_name]


def _index(how: str, n: int, first: int) -> np.ndarray | None:
    if how in ("heads", "negexp"):
        return S.gdn_block_source()
    if how in ("vrows", "gconv"):
        return S.gdn_index(n, first)
    if how in ("zrows", "vcols"):
        return S.gdn_index(n, 0)
    return None


class CheckpointDense:
    """Every language-model tensor but the routed experts and the n-gram table, in the checkpoint's order, with
    `bytes(name)` the stored bytes of a float tensor and `chunks(name, pool)` the streamed bytes of a quantized one.
    `where` and `bytes` are what model_sections.Dense reads."""

    def __init__(self, ck: Checkpoint, q6k: bool = True, require_complete: bool = True):
        tc = ck.text
        self.ck, self.q6k = ck, q6k
        if int(tc["linear_num_key_heads"]) != S.GDN_K_HEADS or int(tc["linear_num_value_heads"]) != S.GDN_V_HEADS \
                or int(tc["linear_key_head_dim"]) != S.GDN_HEAD or int(tc["linear_value_head_dim"]) != S.GDN_HEAD:
            raise S.ConvertError("the GDN head geometry differs from the GGUF head order this converter knows")
        self.qk_rows = int(tc["indexer_n_heads"]) * int(tc["indexer_head_dim"])
        self.tensors: dict[str, Tensor] = {}
        unknown = []
        for k in ck.order:
            _, _, dt, shape, _ = ck.t[k]
            if SKIP.match(k):
                continue
            m = re.fullmatch(r"model\.language_model\.layers\.(\d+)\.(.+)", k)
            if m and m.group(2) == INDEXER_QK:
                rows = shape[0] - self.qk_rows
                for nm, r in (("indexer.q_proj.weight", (0, self.qk_rows)), ("indexer.k_proj.weight", (self.qk_rows, shape[0]))):
                    self._add(f"blk.{m.group(1)}.{nm}", "BF16", k, "split", [shape[1], r[1] - r[0]], r)
                continue
            rule = LAYER.get(m.group(2)) if m else GLOBAL.get(k)
            if rule is None:
                unknown.append(k)
                continue
            if dt != "BF16":
                raise S.ConvertError(f"{k} is {dt}; the dense tensors are read from BF16")
            name = f"blk.{m.group(1)}.{rule[0]}" if m else rule[0]
            ty, how = rule[1], rule[2]
            if how in ("conv", "gconv"):
                if len(shape) != 3 or shape[1] != 1:
                    raise S.ConvertError(f"{k}: a conv weight of shape {shape}")
                ne = [shape[2], shape[0]]
            else:
                ne = list(reversed(shape))
            if ty == "MAT":
                ty = "Q6_K" if q6k and ne[0] % S.Q6K_BLOCK == 0 else "Q8_0"
            if ty in ("Q8_0", "Q6_K") and (len(ne) != 2 or ne[0] % 32):
                raise S.ConvertError(f"{k}: rows of {ne[0]} values cannot be {ty}")
            self._add(name, ty, k, how, ne)
        if unknown:
            raise S.ConvertError("checkpoint tensors with no GGUF counterpart: " + ", ".join(unknown[:8])
                                 + (f" ... and {len(unknown) - 8} more" if len(unknown) > 8 else ""))
        if require_complete:
            self._check_complete(tc)
        self.where = {n: (None, t, ck.t[t.src][0]) for n, t in self.tensors.items()}

    def _check_complete(self, tc):
        """Refuses a checkpoint that lacks a tensor the engine reads (each layer's by its type) or has a layer the
        config does not."""
        n_layers, kinds = int(tc["num_hidden_layers"]), tc["layer_types"]
        for name in self.tensors:
            m = re.match(r"blk\.(\d+)\.", name)
            if m and int(m.group(1)) >= n_layers:
                raise S.ConvertError(f"{self.tensors[name].src}: the config has {n_layers} layers")
        need = [rule[0] for rule in GLOBAL.values()]
        ple_layers = {int(m.group(1)) for name in self.tensors for m in [re.match(r"blk\.(\d+)\.ple_", name)] if m}
        for l in range(n_layers):
            if kinds[l] not in ("linear_attention", "full_attention"):
                raise S.ConvertError(f"config.json: layer {l} is {kinds[l]}")
            for key, rule in LAYER.items():
                part = key.split(".")[0]
                if part == "linear_attn" and kinds[l] != "linear_attention" \
                        or part == "self_attn" and kinds[l] != "full_attention" \
                        or part == "ple" and l not in ple_layers:
                    continue
                need.append(f"blk.{l}.{rule[0]}")
            if kinds[l] == "full_attention":
                need += [f"blk.{l}.indexer.q_proj.weight", f"blk.{l}.indexer.k_proj.weight"]
        missing = [n for n in need if n not in self.tensors]
        if missing:
            raise S.ConvertError(f"the checkpoint lacks {len(missing)} tensors the engine reads: " + ", ".join(missing[:8])
                                 + (f" ... and {len(missing) - 8} more" if len(missing) > 8 else ""))

    def _add(self, name, ty, src, how, ne, rows=None):
        if name in self.tensors:
            raise S.ConvertError(f"two checkpoint tensors map to {name}")
        self.tensors[name] = Tensor(name, ty, src, how, ne, rows)

    def values(self, name) -> np.ndarray:
        """A float tensor's values in the GGUF order, as float32 [ne1, ne0]."""
        t = self.tensors[name]
        x = self.ck.values(t.src)
        if t.how in ("conv", "gconv"):
            x = x[:, 0, :]
        if t.how == "split":
            x = x[t.rows[0]:t.rows[1]]
        if t.how == "norm":
            x = x + np.float32(1)
        if t.how == "negexp":
            x = (-np.exp(x.astype(np.float64))).astype(np.float32)
        idx = _index(t.how, x.shape[0], 2 * S.GDN_K_HEADS * S.GDN_HEAD)
        if idx is not None:
            x = x[idx]
        return np.ascontiguousarray(x, dtype=np.float32)

    def bytes(self, name) -> np.ndarray:
        t = self.tensors[name]
        if t.type_name not in ("F32", "F16", "BF16"):
            raise S.ConvertError(f"{name} is {t.type_name}; read it with chunks()")
        x = self.values(name)
        if not np.isfinite(x).all():
            raise S.ConvertError(f"{name} has a non-finite value")
        if t.type_name == "F32":
            out = x.astype("<f4")
        elif t.type_name == "F16":
            try:
                out = S.f16_values(x, name).astype("<f2")
            except ValueError as e:
                raise S.ConvertError(str(e)) from None
        else:
            u = x.view(np.uint32)
            if np.count_nonzero(u & 0xFFFF):
                raise S.ConvertError(f"{name}: a value that is not BF16")
            out = (u >> 16).astype("<u2")
        return np.frombuffer(out.tobytes(), dtype=np.uint8)

    def jobs(self, name):
        t = self.tensors[name]
        p, off, _, shape, _ = self.ck.t[t.src]
        ne0, ne1 = t.ne
        step = max(1, VALUES_PER_JOB // ne0)
        kind = {"vrows": "rows", "zrows": "rows", "vcols": "cols"}.get(t.how)
        for r in range(0, ne1, step):
            yield (str(p), off, ne0, ne1, r, min(step, ne1 - r), kind,
                   2 * S.GDN_K_HEADS * S.GDN_HEAD if t.how == "vrows" else 0, t.type_name)

    def chunks(self, name, pool, depth: int = 64):
        for b in bounded_imap(pool, _quant_job, self.jobs(name), depth):
            if isinstance(b, str):
                raise S.ConvertError(f"{name}: {b}")
            yield b


def _quant_job(args):
    """Rows [row0, row0 + rows) of a BF16 matrix [ne1, ne0] (rows or columns in the GGUF order) as Q6_K or Q8_0."""
    path, off, ne0, ne1, row0, rows, kind, first, ty = args
    mm = np.memmap(path, dtype="<u2", mode="r", offset=off, shape=(ne1, ne0))
    if kind == "rows":
        u = mm[S.gdn_index(ne1, first)[row0:row0 + rows]]
    elif kind == "cols":
        u = mm[row0:row0 + rows][:, S.gdn_index(ne0, 0)]
    else:
        u = mm[row0:row0 + rows]
    x = S._bf16_to_f32(np.ascontiguousarray(u)).reshape(-1)
    if not np.isfinite(x).all():
        return "a non-finite value"
    return S.q6_k(x) if ty == "Q6_K" else S.q8_0(x)


# ------------------------------------------------------------------ metadata and tokenizer
def front_matter(readme: pathlib.Path) -> dict:
    """The simple `key: value` / `key:\\n- item` YAML block at the top of a model card."""
    out, key = {}, None
    if not readme.is_file():
        return out
    lines = readme.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("- ") and key:
            out.setdefault(key, []).append(line[2:].strip())
        elif ":" in line:
            key, v = (s.strip() for s in line.split(":", 1))
            if v:
                out[key] = v
    return out


def f32(v) -> float:
    return float(np.float32(v))


def tokenizer_kv(ck: Checkpoint) -> dict:
    """The tokenizer as GGUF metadata (llama.cpp's gpt2 conversion): ids without a token become `[PADn]` (unused),
    added tokens are control tokens when marked special or written `<|...|>`, other added tokens user-defined."""
    d = ck.dir
    tj = json.loads((d / "tokenizer.json").read_text(encoding="utf-8"))
    tcfg = json.loads((d / "tokenizer_config.json").read_text(encoding="utf-8"))
    model = tj.get("model", {})
    if model.get("type") != "BPE":
        raise S.ConvertError(f"tokenizer.json: a {model.get('type')} model; only byte-level BPE is converted")
    pre = tj.get("pre_tokenizer") or {}
    splits = [p.get("pattern", {}).get("Regex") for p in pre.get("pretokenizers", []) if p.get("type") == "Split"]
    if splits != [QWEN35_HF_SPLIT]:
        raise S.ConvertError("tokenizer.json: the pre-tokenizer is not the qwen35 split")
    n_vocab = int(ck.text["vocab_size"])
    rev = {int(i): t for t, i in model["vocab"].items()}
    added = {int(a["id"]): a for a in tj.get("added_tokens", [])}
    for i, a in added.items():
        rev[i] = a["content"]
    if max(rev) >= n_vocab:
        raise S.ConvertError(f"tokenizer.json: id {max(rev)} is beyond the vocabulary of {n_vocab}")
    tokens, types = [], []
    for i in range(n_vocab):
        if i not in rev:
            tokens.append(f"[PAD{i}]")
            types.append(UNUSED)
            continue
        t = rev[i]
        tokens.append(t)
        if i in added:
            special = added[i].get("special") or (t.startswith("<|") and t.endswith("|>"))
            types.append(CONTROL if special else USER_DEFINED)
        else:
            types.append(NORMAL)
    merges = [m if isinstance(m, str) else " ".join(m) for m in model["merges"]]
    ids = {t: i for i, t in enumerate(tokens)}

    def token_id(key):
        v = tcfg.get(key)
        v = v.get("content") if isinstance(v, dict) else v
        return ids.get(v) if v else None

    gen = json.loads((d / "generation_config.json").read_text(encoding="utf-8")) if (d / "generation_config.json").is_file() else {}
    kv = {"tokenizer.ggml.model": "gpt2", "tokenizer.ggml.pre": "qwen35", "tokenizer.ggml.tokens": tokens,
          "tokenizer.ggml.token_type": types, "tokenizer.ggml.merges": merges}
    eos, pad = token_id("eos_token"), token_id("pad_token")
    bos = token_id("bos_token")
    if bos is None:
        bos = gen.get("bos_token_id", ck.text.get("bos_token_id"))
    for k, v in (("eos", eos), ("padding", pad), ("bos", bos)):
        if v is not None:
            kv[f"tokenizer.ggml.{k}_token_id"] = int(v)
    kv["tokenizer.ggml.add_bos_token"] = bool(tcfg.get("add_bos_token", False))
    tpl = d / "chat_template.jinja"
    template = tpl.read_text(encoding="utf-8") if tpl.is_file() else tcfg.get("chat_template")
    if template:
        kv["tokenizer.chat_template"] = template
    return kv


def model_kv(ck: Checkpoint, ple_dim: int, file_type: int, name: str | None) -> dict:
    """The GGUF metadata the engine reads, from config.json, generation_config.json and the model card."""
    c, tc = ck.config, ck.text
    if tc.get("model_type") != "qwen4_exp_text":
        raise S.ConvertError(f"config.json: model_type {tc.get('model_type')}, expected qwen4_exp_text")
    A = "qwen4exp."
    d = ck.dir
    gen = json.loads((d / "generation_config.json").read_text(encoding="utf-8")) if (d / "generation_config.json").is_file() else {}
    card = front_matter(d / "README.md")
    kinds = tc["layer_types"]
    rope = tc.get("rope_parameters", {})
    head = int(tc["head_dim"])
    ple_layers = sorted({int(m.group(1)) for k in ck.t for m in [re.match(r"model\.language_model\.layers\.(\d+)\.ple\.key_proj\.weight$", k)] if m})
    if len(ple_layers) != 1:
        raise S.ConvertError(f"the checkpoint has PLE weights in layers {ple_layers}")
    pe = f"model.language_model.layers.{ple_layers[0]}.ple.ple_embedding."
    kv = {"general.architecture": "qwen4exp", "general.type": "model"}
    if "top_k" in gen:
        kv["general.sampling.top_k"] = int(gen["top_k"])
    if "top_p" in gen:
        kv["general.sampling.top_p"] = f32(gen["top_p"])
    if "temperature" in gen:
        kv["general.sampling.temp"] = f32(gen["temperature"])
    if name:
        kv["general.name"] = name
    if "license" in card:
        kv["general.license"] = card["license"]
    if "license_name" in card:
        kv["general.license.name"] = card["license_name"]
    bases = card.get("base_model") or []
    bases = [bases] if isinstance(bases, str) else bases
    if bases:
        kv["general.base_model.count"] = len(bases)
        for i, b in enumerate(bases):
            org, _, repo = b.rpartition("/")
            kv[f"general.base_model.{i}.name"] = repo.replace("-", " ")
            if org:
                kv[f"general.base_model.{i}.organization"] = org
            kv[f"general.base_model.{i}.repo_url"] = f"https://huggingface.co/{b}"
    tags = list(card.get("tags") or [])
    if card.get("pipeline_tag") and card["pipeline_tag"] not in tags:
        tags.append(card["pipeline_tag"])
    if tags:
        kv["general.tags"] = tags
    kv.update({
        A + "block_count": int(tc["num_hidden_layers"]),
        A + "context_length": int(tc["max_position_embeddings"]),
        A + "embedding_length": int(tc["hidden_size"]),
        A + "attention.head_count": int(tc["num_attention_heads"]),
        A + "attention.head_count_kv": int(tc["num_key_value_heads"]),
        A + "rope.dimension_sections": (list(rope.get("mrope_section", [])) + [0, 0, 0, 0])[:4],
        A + "rope.freq_base": f32(rope.get("rope_theta", tc.get("rope_theta"))),
        A + "attention.layer_norm_rms_epsilon": f32(tc["rms_norm_eps"]),
        A + "expert_count": int(tc["num_experts"]),
        A + "expert_used_count": int(tc["num_experts_per_tok"]),
        A + "attention.key_length": head,
        A + "attention.value_length": head,
        "general.file_type": file_type,
        A + "expert_feed_forward_length": int(tc["moe_intermediate_size"]),
        A + "expert_shared_feed_forward_length": int(tc["shared_expert_intermediate_size"]),
        A + "ssm.conv_kernel": int(tc["linear_conv_kernel_dim"]),
        A + "ssm.state_size": int(tc["linear_key_head_dim"]),
        A + "ssm.group_count": int(tc["linear_num_key_heads"]),
        A + "ssm.time_step_rank": int(tc["linear_num_value_heads"]),
        A + "ssm.inner_size": int(tc["linear_num_value_heads"]) * int(tc["linear_value_head_dim"]),
        A + "attention.recurrent_layers": [k == "linear_attention" for k in kinds],
        A + "full_attention_interval": int(tc["full_attention_interval"]),
        A + "rope.dimension_count": int(head * float(rope.get("partial_rotary_factor", tc.get("partial_rotary_factor")))),
        A + "hyper_connection.count": int(tc["hc_count"]),
        A + "hyper_connection.low_rank": int(tc["hc_lowrank"]),
        A + "attention.indexer.head_count": int(tc["indexer_n_heads"]),
        A + "attention.indexer.key_length": int(tc["indexer_head_dim"]),
        A + "attention.indexer.top_k": int(tc["indexer_budget"]),
        A + "attention.compress_ratios": [int(tc["indexer_compress_ratio"]) if k == "full_attention" else 0 for k in kinds],
        A + "ple.layers": ple_layers,
        A + "ple.ngram_size": int(tc["ngram_size"]),
        A + "ple.heads_per_ngram": int(tc["heads_per_ngram"]),
        A + "ple.conv_kernel": int(tc["ple_conv_kernel_size"]),
        A + "ple.eos_token_id": int(tc["eos_token_id"]),
        A + "ple.image_token_id": int(c["image_token_id"]),
        A + "embedding_length_per_layer_input": ple_dim,
        A + "ple.layer_multipliers": [int(v) for v in ck.values(pe + "layer_multipliers")],
        A + "ple.head_offsets": [int(v) for v in ck.values(pe + "ngram_heads_offsets")],
        A + "ple.head_vocab_sizes": [int(v) for v in ck.values(pe + "ngram_heads_vocab_sizes")],
        "general.quantization_version": 2,
    })
    kv.update(tokenizer_kv(ck))
    return kv


def ple_table(ck: Checkpoint) -> pathlib.Path:
    """The checkpoint file that holds the FP8 n-gram table."""
    hits = sorted({ck.t[k][0] for k in ck.t if k.endswith("ngram_embedding.weight_scale")})
    if len(hits) != 1:
        raise S.ConvertError(f"{ck.dir}: {len(hits)} files hold an ngram_embedding.weight_scale")
    return hits[0]

# ===== file: tools/convert/convert.py =====
"""Build the single .basalt model file from the source GGUF shards.

    python3 tools/convert/convert.py --gguf MODEL-00001-of-0000N.gguf --mmproj MMPROJ.gguf --out MODEL.basalt \\
        [--mtp-source DIR] [--mtp-dense FILE] [--draft-vocab FILE] [--name NAME] [--compat-bf16] [--no-vision] [--force]
    python3 tools/convert/convert.py --gguf MODEL-00001-of-0000N.gguf --sections-from BASE.basalt --out MODEL.basalt [--name NAME] [--force]
    python3 tools/convert/convert.py --dspark FILE --into PACK.basalt --undo-dir DIR
    python3 tools/convert/convert.py --sections-from BASE.basalt --checkpoint DIR --out MODEL.basalt [--dense-q8-0] [--mtp-dense FILE] \\
        [--name NAME] [--workers N] [--force]

Inputs
  --gguf FILE        the model's GGUF: shard 1 of a split model (the others are found beside it) or --gguf per shard
  --sections-from BASE.basalt  with --gguf: the drafter, DSpark and vision sections are copied from BASE byte for byte
                     (BASE's vocabulary must be the GGUF's); replaces --mmproj, --mtp-*, --draft-vocab and --dspark. A
                     tensor with a section of its own (the Engram table) whose shard stops after its header is copied
                     from BASE's section of the same tensor
  --mmproj FILE      the vision projector GGUF (required unless --no-vision)
  --dspark FILE      deepseek41 only: the DSpark drafter GGUF, stored whole as the dspark/gguf section
  --mtp-source DIR   the draft layer's BF16 tensors from `mtp_fetch.py fetch --out DIR`; without it there is no drafter
  --mtp-dense FILE   a tuned `mtp/dense` section (raw bytes, same layout as the source; needs --mtp-source); the packs
                     with a self-distilled MTP layer carry one. Its size is checked and its SHA-256 goes to meta.mtp.
                     With --sections-from and --checkpoint it replaces the mtp/dense copied from BASE (same size required)
  --draft-vocab FILE the token subset the draft head may propose (int32 ids; needs --mtp-source)

From an NVFP4 checkpoint (--sections-from with --checkpoint)
  --checkpoint DIR   NVIDIA Model Optimizer's NVFP4 safetensors (config.json, the model-NNNNN-of-NNNNN shards, the FP8
                     n-gram table file, tokenizer files, generation_config.json, README.md). The routed experts keep
                     their E2M1 codes and UE4M3 scales, moved into Basalt's blocks with the FP32 tail {s_gate, s_up,
                     s_down, 0} from weight_scale_2; the projections are quantized once from BF16 to Q6_K (Q8_0 where
                     rows are not whole 256-value blocks, and for the output head and token embedding); the other
                     tensors keep BF16 or become F32/F16 as the engine reads them; the n-gram table is FP8 code x scale
                     as Q8_0. The GGUF metadata and the tokenizer come from the checkpoint's files.
  --dense-q8-0       Q8_0 projections instead of Q6_K (the 8-bit dense variant)
  The drafter and vision sections come from BASE byte for byte; BASE's vocabulary must be the checkpoint's.

Dense requantization
    python3 tools/convert/convert.py --requant-dense-q6k PACK.basalt [--checkpoint DIR] --out MODEL.basalt [--workers N] [--force]
  Every Q8_0 dense projection whose rows are whole 256-value blocks (GDN qkv/z/out, attention q/k/v/o, the shared
  experts' gate and up) is written as Q6_K; everything else is copied byte for byte. --checkpoint DIR quantizes from the
  BF16 shards (each tensor is checked against the Q8_0 copy in PACK); without it the Q8_0 values are requantized.

Adding the drafter to a deepseek41 pack that exists
  --dspark FILE --into PACK.basalt --undo-dir DIR
                     writes dspark/gguf and a new meta past the file's end and replaces the header in one write: no other
                     section moves or changes. DIR (new) keeps the old header and meta;
  --roll-back --into PACK.basalt --undo-dir DIR
                     puts them back and cuts the file to its old size.

Options
  --name NAME        the model's name, reported by the server
  --compat-bf16      convert small projections stored in another form to BF16 (round-to-nearest-even, recorded in
                     meta.conversions)
  --no-vision        leave out the vision projector
  --force            overwrite --out

Every mode writes a version 2 file; its `hc8` section is computed from the file's own hyper-connection matrices
(tools/convert/hc8.py). Sections are listed in include/basalt/core/model_format.hpp. Everything is streamed.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))   # for model_file, gguf_reader, tokenizer
import arch as A  # noqa: E402
import hc8 as H  # noqa: E402
import model_sections as S  # noqa: E402
from model_file import SECTION_ALIGN, TENSOR_ALIGN, ModelFile, ModelWriter, Stream, append_section, undo_append  # noqa: E402

FINGERPRINT_BYTES = 64 << 20

QWEN = A.for_architecture("qwen4exp")


def model_license(kv: dict) -> dict:
    """Flash-Next's license, with its full text (the license requires it in every copy)."""
    return QWEN.license(kv)


def die(msg: str):
    raise SystemExit(f"convert: {msg}")


# ------------------------------------------------------------------ helpers
def fingerprint(path: Path) -> str:
    """sha256 over the first and last 64 MiB and the size: fast, and enough to tell two files apart."""
    size = path.stat().st_size
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(FINGERPRINT_BYTES))
        f.seek(max(0, size - FINGERPRINT_BYTES))
        h.update(f.read(FINGERPRINT_BYTES))
    h.update(struct.pack("<Q", size))
    return h.hexdigest()


def source_record(path: Path) -> dict:
    return {"path": path.name, "size": path.stat().st_size, "fingerprint_sha256_first_last_64mib_and_size": fingerprint(path)}


def check_dspark(path: Path) -> None:
    """Stops on what the engine's drafter cannot read: another architecture, or dense tensors no decode kernel reads."""
    from gguf_reader import GGUFFile
    if not path.is_file():
        die(f"missing input: {path}")
    try:
        g = GGUFFile(path)
    except (ValueError, KeyError, struct.error) as e:
        die(f"{path}: {e}")
    if g.metadata.get("general.architecture") != "dflash":
        die(f"{path} is not a DSpark (dflash) drafter GGUF")
    for t in g.tensors:
        if t.name == "fc.weight" and t.type_name in ("BF16", "F16", "F32"):
            die(f"{path}: {t.name} is {t.type_name}, which no decode kernel reads; take the Q8_0 drafter")


def dspark_record(path: Path) -> dict:
    return {"section": "dspark/gguf", "bytes": path.stat().st_size, "architecture": "dflash"}


def add_dspark(a) -> None:
    """--dspark with --into: the drafter joins a pack that exists, whose other sections stay where and as they are."""
    pack = Path(a.into)
    if not pack.is_file():
        die(f"missing input: {pack}")
    if a.roll_back:
        undo_append(str(pack), a.undo_dir)
        print(f"done: {pack} is back to {pack.stat().st_size} bytes")
        return
    dspark = Path(a.dspark)
    check_dspark(dspark)
    m = ModelFile(str(pack))
    meta = json.loads(m.read("meta"))
    if meta.get("arch") != "deepseek41":
        die(f"{pack} is no deepseek41 pack")
    meta["dspark"] = dspark_record(dspark)
    meta["sources"].append(source_record(dspark))
    try:
        append_section(str(pack), "dspark/gguf", str(dspark), json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), a.undo_dir)
    except (ValueError, FileExistsError) as e:
        die(str(e))
    m = ModelFile(str(pack))
    if not m.verify("dspark/gguf") or not m.verify("meta"):
        die(f"{pack}: the new sections do not read back as written; --roll-back puts the file back")
    print(f"done: {pack} is {pack.stat().st_size} bytes, {len(m.names())} sections")


def json_safe(v):
    """GGUF metadata as JSON: non-finite floats become strings, which JSON cannot carry."""
    if isinstance(v, float) and not math.isfinite(v):
        return repr(v)
    if isinstance(v, list):
        return [json_safe(x) for x in v]
    return v


def load_vision_helper():
    import pack_vision
    return pack_vision.vision_sections


def export_tokenizer(first_gguf: Path, out_dir: Path) -> list[Path]:
    """The tokenizer files (tools/tokenizer.py) of the model's first shard, written under `out_dir`/tokenizer."""
    import tokenizer
    tokenizer.extract(first_gguf, out_dir)
    return sorted(p for p in (out_dir / "tokenizer").iterdir() if p.is_file())


def source_architecture(first_gguf: Path) -> str | None:
    """`general.architecture` from the first shard's header."""
    from gguf_reader import GGUFFile
    return GGUFFile(first_gguf).metadata.get("general.architecture")


def source_tensors(model: S.SourceModel, own_sections=()):
    """(kv, [(name, shard path, absolute offset, size, TensorInfo)]) over every shard, expert tensors and the tensors
    that get a section of their own (`own_sections`) excluded."""
    kv, tensors = {}, []
    for p, g in zip(model.paths, model.files):
        for k, v in g.metadata.items():
            kv.setdefault(k, json_safe(v))
    for name, (g, t, p) in model.where.items():
        if S.is_expert(name) or name in own_sections:
            continue
        a, n = g.data_start + t.offset, t.expected_bytes()
        tensors.append((name, p, a, n, t))
    order = {p: i for i, p in enumerate(model.paths)}
    tensors.sort(key=lambda x: (order[x[1]], x[2]))      # sequential reads per shard
    return kv, tensors


# ------------------------------------------------------------------ the conversion
class DryWriter:
    """ModelWriter's interface without writing: sections get the offsets and sizes a real build would give them."""

    def __init__(self):
        self.pos = 64 << 10
        self.entries: list[tuple[str, int, int, bytes]] = []

    def _start(self, name):
        if any(e[0] == name for e in self.entries):
            raise ValueError(f"duplicate section: {name}")
        self.pos = (self.pos + SECTION_ALIGN - 1) // SECTION_ALIGN * SECTION_ALIGN
        return self.pos

    def add_size(self, name, size):
        off = self._start(name)
        self.pos = off + size
        self.entries.append((name, off, size, b""))
        return size

    def add_bytes(self, name, data):
        self.add_size(name, len(data))

    def add_file(self, name, src, offset=0, size=None):
        self.add_size(name, os.path.getsize(src) - offset if size is None else size)

    def add_chunks(self, name, chunks):
        return self.add_size(name, sum(len(b) for b in chunks))

    def add_parts(self, name, parts, align=TENSOR_ALIGN):
        pos, offsets = 0, []
        for part in parts:
            pos += (-pos) % align
            offsets.append(pos)
            pos += len(part) if isinstance(part, (bytes, bytearray)) else part.size if isinstance(part, Stream) else part[2]
        self.add_size(name, pos)
        return offsets

    def close(self):
        pass


class Job:
    def __init__(self, writer: ModelWriter):
        self.w = writer
        self.rows: list[tuple[str, int]] = []
        self.t0 = time.time()
        self.dry = isinstance(writer, DryWriter)

    def _report(self, name, nbytes, secs):
        mbs = nbytes / (1 << 20) / secs if secs > 0 else float("inf")
        print(f"  {name:<44} {nbytes / (1 << 30):9.3f} GiB {mbs:9.0f} MB/s", flush=True)
        self.rows.append((name, nbytes))

    def bytes(self, name, data: bytes):
        t = time.time()
        self.w.add_bytes(name, data)
        self._report(name, len(data), time.time() - t)

    def file(self, name, src, offset=0, size=None):
        t = time.time()
        self.w.add_file(name, str(src), offset, size)
        self._report(name, self.w.entries[-1][2], time.time() - t)

    def chunks(self, name, chunks, size=None):
        t = time.time()
        n = self.w.add_size(name, size) if size is not None and self.dry else self.w.add_chunks(name, chunks)
        self._report(name, n, time.time() - t)

    def hc8(self):
        """The `hc8` section, from the `dense` and `mtp/dense` sections already written."""
        t = time.time()
        self.w.f.flush()
        at = {e[0]: (e[1], e[2]) for e in self.w.entries}
        with open(self.w.path, "rb") as f:
            def read(name, off, size):
                f.seek(at[name][0] + off)
                return f.read(size)
            whole = lambda n: read(n, 0, at[n][1]) if n in at else None
            try:
                data = H.section(read, whole("dense.index"), whole("mtp/dense.index"))
            except H.Hc8Error as e:
                raise S.ConvertError(f"hc8: {e}")
        self.w.add_bytes("hc8", data)
        self._report("hc8", len(data), time.time() - t)


def source_paths(given) -> list[Path]:
    paths = [Path(p) for p in given]
    for p in paths:
        if not p.is_file():
            die(f"missing input: {p}")
    return S.expand_shards(paths[0]) if len(paths) == 1 else paths


def convert(a) -> None:
    out = Path(a.out)
    partial = Path(str(out) + ".partial")
    if out.exists() and not a.force:
        die(f"{out} exists (use --force to overwrite)")
    base = None
    dry = getattr(a, "dry_run", False)
    try:
        paths = source_paths(a.gguf)
    except S.ConvertError as e:
        die(str(e))
    arch = A.for_architecture(source_architecture(paths[0]))
    if not arch.side_sections:
        given = [o for o, v in (("--mmproj", a.mmproj and not arch.mmproj_only), ("--mtp-source", a.mtp_source), ("--mtp-dense", a.mtp_dense),
                                ("--draft-vocab", a.draft_vocab))
                 if v]
        if given:
            die(f"{arch.name} packs have no MTP drafter or hc8 sections; drop {', '.join(given)}")
    dspark = Path(a.dspark) if getattr(a, "dspark", None) else None
    if dspark is not None:
        if not arch.dspark_ok:
            die(f"{arch.name} packs take no --dspark drafter")
        check_dspark(dspark)
    vision = arch.side_sections and not a.no_vision
    if arch.mmproj_only and a.mmproj and not a.no_vision:
        vision = True
        with open(a.mmproj, "rb") as f:
            head = f.read(1 << 20)
        if head[:4] != b"GGUF" or arch.projector_type.encode() not in head:
            die(f"{a.mmproj} is not a {arch.projector_type} projector GGUF")
    if a.sections_from:
        base = ModelFile(a.sections_from) if Path(a.sections_from).is_file() else die(f"missing input: {a.sections_from}")
        if a.mtp_source or a.mtp_dense or a.draft_vocab or a.mmproj or a.no_vision or getattr(a, "dspark", None):
            die("--sections-from supplies the drafter and vision sections; do not combine it with "
                "--mtp-source, --mtp-dense, --draft-vocab, --mmproj, --dspark or --no-vision")
        base_meta = json.loads(base.read("meta"))
    mmproj = Path(a.mmproj) if a.mmproj else None
    side = [Path(x) for x in (a.draft_vocab, a.mtp_dense) if x]
    if vision and base is None:
        side.append(mmproj if mmproj else Path("<--mmproj>"))
    for p in side:
        if not p.is_file():
            die(f"missing input: {p}")
    if a.draft_vocab and not a.mtp_source:
        die("--draft-vocab needs --mtp-source")
    if a.mtp_dense and not a.mtp_source:
        die("--mtp-dense needs --mtp-source")

    err = None
    try:
        print("reading the source GGUFs ...", flush=True)
        model = S.SourceModel(paths, frozenset(arch.own_sections) if base is not None else frozenset())
        kv, tensors = source_tensors(model, arch.own_sections)
        own = {n: model.where[n][1] for n in arch.own_sections if n in model.where}
        if not arch.side_sections:
            arch.check(kv, own)
        for k, v in arch.sampling_kv.items():
            kv.setdefault(k, v)
        dense = S.Dense(model, a.compat_bf16, arch)
        experts = S.Experts(model)
        mtp = S.Mtp(a.mtp_source, a.mtp_dense) if a.mtp_source else None
        n_vocab = len(kv.get("tokenizer.ggml.tokens") or []) or None
        draft_vocab = S.draft_vocab_bytes(a.draft_vocab, n_vocab) if a.draft_vocab else None
        if base is not None:
            if base_meta.get("gguf_kv", {}).get("tokenizer.ggml.tokens") != kv.get("tokenizer.ggml.tokens"):
                die(f"{a.sections_from}s vocabulary differs from the GGUFs; its drafter and vision sections would not fit")
    except S.ConvertError as e:
        err = str(e)
    if err is not None:
        # Drop every view of the source files so they are unmapped (and deletable) before the process exits.
        model = dense = experts = mtp = None
        gc.collect()
        die(err)

    vision_extra = []
    if vision and base is None and not arch.mmproj_only:
        fn = load_vision_helper()
        if fn is None:
            print("note: pack_vision.py (vision_sections) not found; only vision/mmproj is written")
        else:
            vision_extra = list(fn(str(mmproj), [str(p) for p in model.paths]))

    if out.parent and not out.parent.exists():
        die(f"{out.parent} does not exist")
    if partial.exists():
        partial.unlink()
    w = DryWriter() if dry else ModelWriter(str(partial))
    job = Job(w)
    ok = False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            print("planning sections:" if dry else "writing sections:", flush=True)
            for p in export_tokenizer(model.paths[0], Path(tmp)):
                job.file(f"tokenizer/{p.name}", p)
            job.chunks("dense", dense.chunks())
            job.bytes("dense.index", dense.index_text())
            job.chunks("experts", experts.chunks(), experts.total)
            job.bytes("experts.layout", experts.layout_text())
            t = time.time()
            offs = w.add_parts("gguf", [(src, off, size) for _, src, off, size, _ in tensors], TENSOR_ALIGN)
            job._report("gguf", w.entries[-1][2], time.time() - t)
            tensor_offsets = dict(zip((x[0] for x in tensors), offs))
            own_entries = {}
            copied = []
            for name, section in arch.own_sections.items():
                t = own[name]
                if name in model.short:   # its shard stops after the header: the same tensor's section in the base
                    size = t.expected_bytes()
                    bt = base_meta.get("tensors", {}).get(name, {})
                    if (bt.get("section"), bt.get("type_id"), bt.get("shape"), bt.get("bytes")) != \
                            (section, t.type_id, [int(d) for d in t.shape], size) or section not in base.sections:
                        die(f"{name}: its shard holds only the header, and {a.sections_from} has no section of that tensor")
                    off, _ = base.section(section)
                    job.file(section, a.sections_from, off, size)
                    if w.entries[-1][3] != base.sections[section][2]:
                        die(f"section {section}: the copy's SHA-256 differs from {a.sections_from}'s")
                    copied.append(section)
                else:
                    src, off, size = model.abs_range(name)
                    job.file(section, src, off, size)
                own_entries[name] = {"type": t.type_name, "type_id": t.type_id, "shape": [int(d) for d in t.shape],
                                     "bytes": size, "offset": 0, "section": section}
            if base is not None:
                for name in base.names():
                    if name != "vision/text_vocab" and name.startswith(("mtp/", "vision/", "dspark/")):
                        off, size = base.section(name)
                        job.file(name, a.sections_from, off, size)
                        if w.entries[-1][3] != base.sections[name][2]:
                            die(f"section {name}: the copys SHA-256 differs from {a.sections_from}s")
                        copied.append(name)
                if "vision/text_vocab" in base.sections:
                    import pack_vision
                    for p in model.paths:
                        data = pack_vision.vocab_only_gguf(str(p))
                        if data is not None:
                            job.bytes("vision/text_vocab", data)
                            break
                    else:
                        die("none of the source GGUF files carries the tokenizer metadata the vision sidecar needs")
            if mtp is not None:
                job.chunks("mtp/dense", mtp.dense_chunks())
                job.bytes("mtp/dense.index", mtp.dense_index())
                job.chunks("mtp/experts", mtp.expert_chunks())
                if draft_vocab is not None:
                    job.bytes("mtp/draft_vocab", draft_vocab)
            if arch.side_sections:
                job.hc8()
            written = {e[0] for e in w.entries}
            if vision and base is None:
                job.file("vision/mmproj", mmproj)
                for name, data, src, off, size in vision_extra:
                    if name in written or name == "vision/mmproj":
                        print(f"note: vision section {name} already written, skipped")
                        continue
                    if data is not None:
                        job.bytes(name, data)
                    else:
                        job.file(name, src, off, size)
            if dspark is not None:
                job.file("dspark/gguf", dspark)

            sources = [source_record(p) for p in model.paths + ([mmproj] if vision and base is None else []) + ([dspark] if dspark else [])]
            meta = {
                "format": "basalt", "version": 2, "name": a.name,
                "converter": {"tool": "tools/convert/convert.py"},
                "sources": sources,
                "gguf_kv": kv,
                "tensors": {**{n: {"type": t.type_name, "type_id": t.type_id, "shape": [int(d) for d in t.shape],
                                   "bytes": nb, "offset": tensor_offsets[n]}
                               for n, _, _, nb, t in tensors}, **own_entries},
                "conversions": {"compat_bf16": a.compat_bf16, "tensors": dense.records},
                "license": arch.license(kv),
                "mtp": base_meta.get("mtp") if base is not None else None if mtp is None else {"source_sha256": mtp.source_sha256(),
                                                  "expert_format": "q2_0", "draft_vocab": draft_vocab is not None,
                                                  "dense": ({"tuning": "self_distilled", "sha256": mtp.tuned_sha256}
                                                            if mtp.tuned else {"tuning": None})},
                "vision": base_meta.get("vision") if base is not None else None if not vision else {"mmproj": "vision/mmproj", "mmproj_bytes": mmproj.stat().st_size,
                                                    "extra": [v[0] for v in vision_extra]},
                **arch.meta_extra(kv, {n: t for n, _, _, _, t in tensors}, own),
                **({"dspark": dspark_record(dspark)} if dspark else {}),
                **({"dspark": base_meta["dspark"]} if base is not None and "dspark" in base_meta else {}),
            }
            if base is not None:
                meta["sections_from"] = {"path": Path(a.sections_from).name, "size": Path(a.sections_from).stat().st_size,
                                         "fingerprint_sha256_first_last_64mib_and_size": fingerprint(Path(a.sections_from)),
                                         "copied": copied, "regenerated": ["vision/text_vocab"]}
            job.bytes("meta", json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        w.close()
        ok = True
    except S.ConvertError as e:
        err = str(e)
    finally:
        if not ok and not dry:
            try:
                w.f.close()
            except OSError:
                pass
            partial.unlink(missing_ok=True)
    if err is not None:
        model = dense = experts = mtp = job = None
        gc.collect()
        die(err)
    if dry:
        end = w.pos
        print(f"dry run: {'section':<28} {'offset':>16} {'bytes':>16}")
        for name, off, size, _ in w.entries:
            print(f"         {name:<28} {off:>16} {size:>16}")
        print(f"dry run: {len(w.entries)} sections, {end} bytes ({end / (1 << 30):.3f} GiB) -> {out}")
        return
    os.replace(partial, out)
    total = out.stat().st_size
    print(f"done: {len(job.rows)} sections, {total / (1 << 30):.3f} GiB, {time.time() - job.t0:.1f} s -> {out}")


def warn_unofficial():
    msg = ("WARNING: packs converted yourself are significantly slower than the official .basalt files. "
           "Download them from https://huggingface.co/jesdga/Qwen3.8-Flash-Next-Basalt")
    if sys.stderr.isatty():
        msg = f"\033[1;31m{msg}\033[0m"
    print(msg, file=sys.stderr, flush=True)


def from_checkpoint(a) -> None:
    """The language model straight from an NVFP4 safetensors checkpoint (routed experts moved into Basalt's layout,
    every other tensor converted once from BF16, the n-gram table from FP8, metadata and tokenizer from the
    checkpoint's files); drafter and vision are copied from BASE byte for byte."""
    import multiprocessing
    import checkpoint as C
    out, src = Path(a.out), Path(a.sections_from)
    partial = Path(str(out) + ".partial")
    if out.exists() and not a.force:
        die(f"{out} exists (use --force to overwrite)")
    if not src.is_file():
        die(f"missing input: {src}")
    base = ModelFile(str(src))
    base_meta = json.loads(base.read("meta"))
    q6k = not a.dense_q8_0
    try:
        ck = C.Checkpoint(a.checkpoint)
        experts = C.CheckpointExperts(ck)
        dense_src = C.CheckpointDense(ck, q6k)
        ple_path = C.ple_table(ck)
        ple = S.PleFp8(str(ple_path))
        kv = C.model_kv(ck, S.PLE_DIM, 18 if q6k else 7, a.name)
        dense = S.Dense(dense_src, False)
    except S.ConvertError as e:
        die(str(e))
    old = base.read("experts.layout").decode("utf-8").splitlines()
    m = re.search(r"\(n_expert (\d+)", old[0])
    n_layers = sum(1 for x in old if x.strip() and not x.startswith("#"))
    if not m or int(m.group(1)) != experts.n_expert or n_layers != experts.n_layers:
        die(f"{src} has {n_layers} layers of {m.group(1) if m else '?'} experts, the checkpoint {experts.n_layers} "
            f"of {experts.n_expert}")
    if base_meta.get("gguf_kv", {}).get("tokenizer.ggml.tokens") != kv["tokenizer.ggml.tokens"]:
        die(f"{src}'s vocabulary differs from the checkpoint's; its drafter and vision sections would not fit")
    if partial.exists():
        partial.unlink()
    w = ModelWriter(str(partial))
    job = Job(w)
    ok = False
    try:
        with tempfile.TemporaryDirectory() as tmp, multiprocessing.Pool(a.workers) as pool:
            import tokenizer
            tokenizer.extract_metadata(kv, Path(tmp))
            print("writing sections:", flush=True)
            for p in sorted(q for q in (Path(tmp) / "tokenizer").iterdir() if q.is_file()):
                job.file(f"tokenizer/{p.name}", p)
            job.chunks("dense", dense.chunks())
            job.bytes("dense.index", dense.index_text())
            job.chunks("experts", experts.chunks(pool))
            job.bytes("experts.layout", experts.layout_text())
            t = time.time()
            parts = []
            for n, info in dense_src.tensors.items():
                if info.type_name in ("Q8_0", "Q6_K"):
                    parts.append(Stream(info.expected_bytes(), dense_src.chunks(n, pool)))
                else:
                    parts.append(dense_src.bytes(n).tobytes())
            parts.append(Stream(ple.nbytes, ple.chunks(pool)))
            offs = w.add_parts("gguf", parts, TENSOR_ALIGN)
            job._report("gguf", w.entries[-1][2], time.time() - t)
            names = list(dense_src.tensors) + ["per_layer_token_embd.weight"]
            tensor_offsets = dict(zip(names, offs))
            tuned_sha = None
            for name in base.names():
                if name.startswith(("mtp/", "vision/")):
                    off, size = base.section(name)
                    if name == "mtp/dense" and a.mtp_dense:
                        tuned = Path(a.mtp_dense)
                        if tuned.stat().st_size != size:
                            die(f"{tuned}: {tuned.stat().st_size} bytes, {src}'s mtp/dense is {size}")
                        h = hashlib.sha256()
                        with open(tuned, "rb") as f:
                            for b in iter(lambda: f.read(1 << 24), b""):
                                h.update(b)
                        tuned_sha = h.hexdigest()
                        job.file(name, tuned)
                        continue
                    job.file(name, src, off, size)
            job.hc8()
            for name, _, _, digest in w.entries:
                if name in base.sections and name.startswith(("mtp/", "vision/")) \
                        and not (name == "mtp/dense" and a.mtp_dense) \
                        and digest != base.sections[name][2]:
                    die(f"section {name}: the copy's SHA-256 differs from {src}'s")
            tens = {n: {"type": t.type_name, "type_id": t.type_id, "shape": [int(d) for d in t.ne],
                        "bytes": t.expected_bytes(), "offset": tensor_offsets[n]} for n, t in dense_src.tensors.items()}
            tens["per_layer_token_embd.weight"] = {"type": "Q8_0", "type_id": 8, "shape": [S.PLE_DIM, ple.rows],
                                                  "bytes": ple.nbytes, "offset": tensor_offsets["per_layer_token_embd.weight"]}
            ck_files = sorted(set(ck.shards) | {ple_path})
            meta = {
                "format": "basalt", "version": 2, "name": a.name or base_meta.get("name"),
                "converter": {"tool": "tools/convert/convert.py"},
                "sources": [{"path": p.name, "size": p.stat().st_size,
                             "fingerprint_sha256_first_last_64mib_and_size": fingerprint(p)} for p in ck_files],
                "gguf_kv": kv,
                "tensors": tens,
                "conversions": {"compat_bf16": False, "tensors": [{
                    "name": "per_layer_token_embd.weight", "src_type": "F8_E4M3", "dst_type": "Q8_0",
                    "method": "fp8 code x per-tensor BF16 scale, Q8_0 blocks of 32 (ggml reference rounding)",
                    "scale": ple.scale}]},
                "license": model_license(kv),
            }
            for k in ("mtp", "vision"):
                meta[k] = base_meta.get(k)
            if tuned_sha:
                meta["mtp"] = {**meta["mtp"], "dense": {"tuning": "self_distilled", "sha256": tuned_sha}}
            used = sorted({t.how for t in dense_src.tensors.values()})
            meta["checkpoint"] = {
                "dir": Path(a.checkpoint).name, "config_sha256": hashlib.sha256((ck.dir / "config.json").read_bytes()).hexdigest(),
                "experts": {"format": "nvfp4", "source": "weight (E2M1 pairs), weight_scale (UE4M3 per 16), "
                            "weight_scale_2 (FP32); input_scale unused", "repack": "codes and scales unchanged, "
                            "nibbles moved into 64-value blocks", "tail": "fp32 {s_gate, s_up, s_down, 0} per expert"},
                "dense": {"projections": "Q6_K where rows are whole 256-value blocks, else Q8_0" if q6k else "Q8_0",
                          "q6_k_method": "least-squares 16-value group scales, 8-bit group scales, fp16 super-scale",
                          "q8_0_method": "ggml reference rounding", "from": "BF16, quantized once",
                          "transforms": {k: C.METHODS[k] for k in used}},
                "sections_from": {"path": src.name, "size": src.stat().st_size,
                                  "fingerprint_sha256_first_last_64mib_and_size": fingerprint(src),
                                  "copied": [n for n in base.names() if n.startswith(("mtp/", "vision/"))]}}
            job.bytes("meta", json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        w.close()
        ok = True
    except S.ConvertError as e:
        die(str(e))
    finally:
        if not ok:
            try:
                w.f.close()
            except OSError:
                pass
            partial.unlink(missing_ok=True)
    os.replace(partial, out)
    print(f"done: {len(job.rows)} sections, {out.stat().st_size / (1 << 30):.3f} GiB, {time.time() - job.t0:.1f} s -> {out}")


def requant_dense(a) -> None:
    """BASE with its Q8_0 dense projections written as Q6_K; every other section and tensor is copied byte for byte."""
    import multiprocessing
    out, src = Path(a.out), Path(a.requant_dense_q6k)
    partial = Path(str(out) + ".partial")
    if out.exists() and not a.force:
        die(f"{out} exists (use --force to overwrite)")
    if not src.is_file():
        die(f"missing input: {src}")
    if a.checkpoint and not Path(a.checkpoint).is_dir():
        die(f"missing input: {a.checkpoint}")
    base = ModelFile(str(src))
    meta = json.loads(base.read("meta"))
    gguf_off, _ = base.section("gguf")
    tensors = meta["tensors"]
    try:
        plan = S.DenseQ6K(tensors, a.checkpoint)
    except S.ConvertError as e:
        die(str(e))
    if not plan.plan:
        die(f"{src} has no Q8_0 dense projection to requantize")
    order = sorted(tensors, key=lambda n: tensors[n]["offset"])
    if partial.exists():
        partial.unlink()
    w = ModelWriter(str(partial))
    job = Job(w)
    ok = False
    try:
        with multiprocessing.Pool(a.workers) as pool:
            for name in base.names():
                if name == "meta":
                    continue
                if name != "gguf":
                    off, size = base.section(name)
                    job.file(name, src, off, size)
                    continue
                t = time.time()
                parts, sizes = [], {}
                for n in order:
                    info = tensors[n]
                    if n in plan.plan:
                        sizes[n] = plan.nbytes(info)
                        parts.append(Stream(sizes[n], plan.chunks(n, info, str(src), gguf_off, pool)))
                    else:
                        sizes[n] = info["bytes"]
                        parts.append((str(src), gguf_off + info["offset"], info["bytes"]))
                offs = w.add_parts("gguf", parts, TENSOR_ALIGN)
                job._report("gguf", w.entries[-1][2], time.time() - t)
            new = {n: dict(tensors[n]) for n in tensors}
            for n, o in zip(order, offs):
                new[n]["offset"] = o
                if n in plan.plan:
                    new[n].update({"type": "Q6_K", "type_id": 14, "bytes": sizes[n]})
            meta["tensors"] = new
            meta["converter"] = {"tool": "tools/convert/convert.py"}
            meta["dense_requant"] = {
                "from": {"path": src.name, "size": src.stat().st_size,
                         "fingerprint_sha256_first_last_64mib_and_size": fingerprint(src)},
                "to": "Q6_K", "source": "checkpoint BF16" if a.checkpoint else "pack Q8_0 values (two roundings)",
                "tensors": sorted(plan.plan), "max_bf16_to_q8_0_steps": plan.worst,
                "method": "least-squares 16-value group scales, 8-bit group scales, fp16 super-scale"}
            job.bytes("meta", json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        w.close()
        ok = True
    except S.ConvertError as e:
        die(str(e))
    finally:
        if not ok:
            try:
                w.f.close()
            except OSError:
                pass
            partial.unlink(missing_ok=True)
    os.replace(partial, out)
    print(f"done: {len(job.rows)} sections, {len(plan.plan)} tensors to Q6_K, {out.stat().st_size / (1 << 30):.3f} GiB,"
          f" max BF16-to-Q8_0 distance {plan.worst:.3f} steps -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sections-from", metavar="PACK.basalt",
                    help="with --checkpoint or --gguf: the drafter and vision projector come from this .basalt; everything "
                         "else from the checkpoint or the GGUF")
    ap.add_argument("--gguf", action="append", metavar="FILE",
                    help="the model's GGUF: shard 1 of a split model (the others are found beside it), or repeat per shard")
    ap.add_argument("--mmproj", help="vision projector GGUF (required unless --no-vision)")
    ap.add_argument("--dspark", metavar="FILE", help="deepseek41: the DSpark drafter GGUF, stored whole as dspark/gguf")
    ap.add_argument("--into", metavar="PACK.basalt", help="with --dspark: add it to this pack, whose other sections stay as they are")
    ap.add_argument("--undo-dir", metavar="DIR", help="with --into: a new directory for what --roll-back restores")
    ap.add_argument("--roll-back", action="store_true", help="with --into and --undo-dir: restore the pack as it was")
    ap.add_argument("--out", help="the .basalt file to write")
    ap.add_argument("--mtp-source", metavar="DIR", help="the drafter's BF16 tensors written by tools/convert/mtp_fetch.py fetch")
    ap.add_argument("--mtp-dense", metavar="FILE", help="a tuned (self-distilled) mtp/dense section to embed (needs --mtp-source)")
    ap.add_argument("--draft-vocab", metavar="FILE", help="int32 token ids the draft head may propose (needs --mtp-source)")
    ap.add_argument("--name")
    ap.add_argument("--compat-bf16", action="store_true",
                    help="convert small projections stored in another form to BF16 (rounds)")
    ap.add_argument("--no-vision", action="store_true")
    ap.add_argument("--requant-dense-q6k", metavar="PACK.basalt",
                    help="copy PACK with its Q8_0 dense projections requantized to Q6_K")
    ap.add_argument("--checkpoint", metavar="DIR",
                    help="an NVFP4 safetensors checkpoint: with --sections-from, the language model is read from it; "
                         "with --requant-dense-q6k, the BF16 projections")
    ap.add_argument("--dense-q8-0", action="store_true",
                    help="with --checkpoint and --sections-from: Q8_0 projections instead of Q6_K")
    ap.add_argument("--workers", type=int, default=16, help="with --checkpoint: converter processes")
    ap.add_argument("--force", action="store_true", help="overwrite --out")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --gguf: print every section's offset and size and write nothing")
    a = ap.parse_args()
    if a.into or a.roll_back or a.undo_dir:
        if not a.into or not a.undo_dir or bool(a.dspark) == a.roll_back or a.out or a.gguf or a.sections_from or a.checkpoint or a.requant_dense_q6k:
            ap.error("--into takes --undo-dir and either --dspark or --roll-back, and nothing else")
        add_dspark(a)
        return
    if not a.out:
        ap.error("--out is required")
    if a.dspark and (a.checkpoint or a.requant_dense_q6k or a.sections_from):
        ap.error("--dspark goes with --gguf")
    if a.requant_dense_q6k:
        if a.sections_from or a.gguf or a.dense_q8_0:
            ap.error("--requant-dense-q6k takes only --checkpoint, --out, --workers and --force")
        requant_dense(a)
        return
    if a.sections_from and a.checkpoint:
        if a.gguf or a.mmproj or a.mtp_source or a.draft_vocab:
            ap.error("--checkpoint with --sections-from takes only --out, --name, --mtp-dense, --dense-q8-0, --workers and --force")
        if not Path(a.checkpoint).is_dir():
            ap.error(f"missing input: {a.checkpoint}")
        if a.mtp_dense and not Path(a.mtp_dense).is_file():
            ap.error(f"missing input: {a.mtp_dense}")
        from_checkpoint(a)
        return
    if a.dense_q8_0:
        ap.error("--dense-q8-0 needs --checkpoint and --sections-from")
    if not a.gguf:
        ap.error("--gguf is required")
    if not Path(a.gguf[0]).is_file():
        ap.error(f"missing input: {a.gguf[0]}")
    arch = A.for_architecture(source_architecture(Path(a.gguf[0])))
    if arch.side_sections:
        a.name = a.name or arch.default_name(a.gguf[0], {}, bool(a.sections_from))
        if not a.no_vision and not a.mmproj and not a.sections_from:
            ap.error("--mmproj is required unless --no-vision")
        warn_unofficial()
    else:
        from gguf_reader import GGUFFile
        a.name = a.name or arch.default_name(a.gguf[0], GGUFFile(Path(a.gguf[0])).metadata)
    convert(a)


if __name__ == "__main__":
    main()

# ===== file: tools/convert/hc8.py =====
"""The `hc8` section: the 8-bit copies of the hyper-connection matrices the engine's decode reads.

The layout is in include/basalt/core/model_file.hpp (Hc8Header, Hc8Entry).  Each copy is the matrix's int8 codes, then
its f16 scales: q = rint(w / s) clamped to +-127, s = f16(absmax / 127) per 32 consecutive values of a row, in fp32
arithmetic, as the engine's quantizer (hc8_quant_kernel) computes them.

    blob = hc8.section(read, dense_index_text, mtp_index_text)   # read(section, offset, size) -> bytes
"""
from __future__ import annotations

import re
import struct

import numpy as np

G8 = 32
HEADER = struct.Struct("<8sII")                 # magic, version, n_entries
ENTRY = struct.Struct("<128sIIIIQQ")             # name, rows, cols, srow, reserved, offset, bytes
MAGIC = b"BASALTH8"
VERSION = 1
DATA_ALIGN = 256
# the drafter's matrices: bf16 tensors of its hyper-connection blocks
MTP_HC = re.compile(r".*hyper_connection.*")


class Hc8Error(Exception):
    pass


def srow_of(cols: int) -> int:
    return ((cols // G8) + 7) & ~7


def quantize(w: np.ndarray) -> bytes:
    """One copy of the bf16 matrix `w` (uint16, rows x cols): the codes, then rows x srow f16 scales (zero padded)."""
    rows, cols = w.shape
    if cols % G8:
        raise Hc8Error(f"{cols} columns are not whole groups of {G8}")
    x = (w.astype(np.uint32) << 16).view(np.float32).reshape(rows, cols // G8, G8)
    amax = np.abs(x).max(axis=2)
    with np.errstate(over="ignore"):
        h = (amax / np.float32(127.0)).astype(np.float16)
    s = h.astype(np.float32)[:, :, None]
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.clip(np.rint(x / s), np.float32(-127.0), np.float32(127.0))
    q = np.where(s > 0, v, np.float32(0.0)).astype(np.int8)
    scales = np.zeros((rows, srow_of(cols)), dtype=np.uint16)
    scales[:, : cols // G8] = h.view(np.uint16)
    return q.tobytes() + scales.astype("<u2").tobytes()


def table(copies: list[tuple[str, int, int, bytes]]) -> bytes:
    """The section from (name, rows, cols, copy) in order."""
    at = (HEADER.size + len(copies) * ENTRY.size + DATA_ALIGN - 1) // DATA_ALIGN * DATA_ALIGN
    entries, data = [], []
    for name, rows, cols, blob in copies:
        nb = name.encode()
        if len(nb) >= 128:
            raise Hc8Error(f"name too long: {name}")
        srow = srow_of(cols)
        if len(blob) != rows * cols + rows * srow * 2:
            raise Hc8Error(f"wrong size for {name}")
        entries.append(ENTRY.pack(nb, rows, cols, srow, 0, at, len(blob)))
        data.append((at, blob))
        at = (at + len(blob) + DATA_ALIGN - 1) // DATA_ALIGN * DATA_ALIGN
    out = bytearray(HEADER.pack(MAGIC, VERSION, len(copies)) + b"".join(entries))
    for off, blob in data:
        out += b"\0" * (off - len(out))
        out += blob
    return bytes(out)


def read_table(blob: bytes) -> dict[str, tuple[int, int, int, bytes]]:
    """name -> (rows, cols, srow, copy)."""
    magic, version, n = HEADER.unpack_from(blob, 0)
    if magic != MAGIC or version != VERSION:
        raise Hc8Error("bad magic or version")
    out = {}
    for i in range(n):
        name, rows, cols, srow, _, off, nb = ENTRY.unpack_from(blob, HEADER.size + i * ENTRY.size)
        out[name.rstrip(b"\0").decode()] = (rows, cols, srow, blob[off:off + nb])
    return out


def _dense_rows(index_text: str) -> dict[str, list[str]]:
    rows = {}
    for line in index_text.splitlines():
        if line and not line.startswith("#"):
            f = line.split()
            rows[f[0]] = f
    return rows


def _main_names(rows: dict) -> list[str]:
    """The model's hyper-connection matrices in the engine's order: per layer attn then ffn (down, up, inject), then the
    output head's down and up."""
    layers = sorted({int(m.group(1)) for n in rows for m in [re.match(r"blk\.(\d+)\.hc_", n)] if m})
    names = [f"blk.{l}.hc_{p}_{w}.weight" for l in layers for p in ("attn", "ffn") for w in ("down", "up", "inject")]
    names += ["output_hc_down.weight", "output_hc_up.weight"]
    return [n for n in names if n in rows]


def _bf16(raw: bytes, kind: int, rows: int, cols: int, name: str) -> np.ndarray:
    if kind == 4:                                 # bf16 as stored
        a = np.frombuffer(raw, dtype="<u2")
    elif kind == 1:                               # bf16 promoted to f32: the high 16 bits
        a = (np.frombuffer(raw, dtype="<u4") >> 16).astype(np.uint16)
    else:
        raise Hc8Error(f"{name} is dense index kind {kind}, not bf16")
    if a.size != rows * cols:
        raise Hc8Error(f"{name}: {a.size} values, not {rows} x {cols}")
    return a.reshape(rows, cols)


def section(read, dense_index: bytes, mtp_index: bytes | None) -> bytes:
    """The `hc8` section of a pack: `read(section, offset, size)` reads the pack's `dense` and `mtp/dense` bytes."""
    copies = []
    rows = _dense_rows(dense_index.decode("utf-8"))
    for name in _main_names(rows):
        f = rows[name]
        kind, off, nb, ne0, ne1 = int(f[2]), int(f[3]), int(f[4]), int(f[7]), int(f[8])
        if f[1] != "0":
            raise Hc8Error(f"{name} is not in the dense section")
        w = _bf16(read("dense", off, nb), kind, ne1, ne0, name)
        copies.append((name, ne1, ne0, quantize(w)))
    for line in (mtp_index or b"").decode("utf-8").splitlines():
        f = line.split()
        if len(f) != 6 or f[1] != "bf16" or not MTP_HC.match(f[0]):
            continue
        r, c, off, nb = int(f[2]), int(f[3]), int(f[4]), int(f[5])
        if r * c * 2 != nb:
            continue
        w = _bf16(read("mtp/dense", off, nb), 4, r, c, f[0])
        copies.append(("mtp." + f[0], r, c, quantize(w)))
    return table(copies)

# ===== file: tools/convert/model_sections.py =====
"""Sections of a .basalt file built from the source weights, for tools/convert/convert.py.

  dense / dense.index       float tensors in the form the engine reads, and the table of every non-expert tensor
  experts / experts.layout  per layer, n_expert blobs of [gate | up | down] rows, and where each layer starts
  mtp/dense, mtp/dense.index, mtp/experts  the draft layer from the BF16 `mtp.*` tensors (tools/convert/mtp_fetch.py)

Sections are produced as byte-chunk iterators for ModelWriter.add_chunks. Needs numpy.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import struct
import sys
from collections import deque

import numpy as np
import os

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import ggufq  # noqa: E402
from arch import for_architecture  # noqa: E402
from gguf_reader import GGUFFile  # noqa: E402


class ConvertError(Exception):
    """An input that cannot be converted; the message says why."""


FLOAT = {"BF16", "F32", "F16"}
ROLES = ("gate", "up", "down")
ALIGN = 64                      # between the tensors of `dense`
EXPERT_RE = re.compile(r"^blk\.\d+\.ffn_(gate|up|down|gate_up)_exps\.(weight|scale|input_scale)$")
KIND = {"BF16": "4", "F16": "5", "F32": "2"}
QWEN = for_architecture("qwen4exp")


def form_of(name: str, arch=QWEN):
    return arch.form_of(name)


# ------------------------------------------------------------------ the source model
def expand_shards(first: pathlib.Path) -> list[pathlib.Path]:
    """`<name>-00001-of-0000N.gguf` -> all N shards (a missing one is an error); any other name -> itself."""
    m = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", first.name)
    if not m:
        return [first]
    total = int(m.group(2))
    paths = [first.with_name(first.name[:m.start()] + "-%05d-of-%05d.gguf" % (i, total)) for i in range(1, total + 1)]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise ConvertError("missing model shards: " + ", ".join(missing))
    return paths


def check_split(files) -> None:
    """The split keys as the engine checks them: shard 1 carries the metadata and every shard declares split.count / split.no consistently."""
    n = len(files)
    meta0 = files[0].metadata
    if n == 1:
        if int(meta0.get("split.count", 1)) > 1:
            raise ConvertError(f"{files[0].path.name} is shard 1 of {meta0['split.count']}, but its name has no "
                               "-00001-of-0000N.gguf to find the others by")
        return
    if "general.architecture" not in meta0:
        raise ConvertError(f"{files[0].path.name} has no general.architecture; the first shard of a split model "
                           "carries the metadata")
    total = meta0.get("split.tensors.count")
    for i, g in enumerate(files):
        md = g.metadata
        if md.get("split.count") != n or md.get("split.no") != i or \
                (total is not None and md.get("split.tensors.count") != total):
            raise ConvertError(f"{g.path.name} does not declare itself shard {i + 1} of {n} of this model "
                               "(split.count / split.no / split.tensors.count)")
    if total is not None and sum(len(g.tensors) for g in files) != total:
        raise ConvertError(f"the {n} shards hold {sum(len(g.tensors) for g in files)} tensors, but "
                           f"split.tensors.count is {total}")


class SourceModel:
    """All shards of one model: name -> (GGUFFile, TensorInfo, shard path), in file order."""

    def __init__(self, paths, described=frozenset()):
        """`described`: tensors whose shard may stop after its header; their names land in `short`."""
        self.paths = [pathlib.Path(p) for p in paths]
        self.files = [GGUFFile(p) for p in self.paths]
        self.sizes = [p.stat().st_size for p in self.paths]
        check_split(self.files)
        self._maps: dict[pathlib.Path, np.ndarray] = {}
        self.where: dict[str, tuple] = {}
        self.short: set[str] = set()
        for p, g, size in zip(self.paths, self.files, self.sizes):
            for t in g.tensors:
                n = t.expected_bytes()
                if n is not None and g.data_start + t.offset + n > size and t.name in described:
                    self.short.add(t.name)
                elif n is None or g.data_start + t.offset + n > size:
                    raise ConvertError(f"{p.name}: unsupported or truncated tensor {t.name}")
                if t.name in self.where:
                    raise ConvertError(f"{p.name}: duplicate tensor {t.name}")
                self.where[t.name] = (g, t, p)

    def bytes(self, name: str) -> np.ndarray:
        g, t, p = self.where[name]
        if p not in self._maps:
            self._maps[p] = np.memmap(p, dtype=np.uint8, mode="r")
        a = g.data_start + t.offset
        return self._maps[p][a:a + t.expected_bytes()]

    def abs_range(self, name: str):
        """(shard path, absolute offset, size) of a tensor's bytes."""
        g, t, p = self.where[name]
        return p, g.data_start + t.offset, t.expected_bytes()


def is_expert(name: str) -> bool:
    return bool(EXPERT_RE.match(name))


# ------------------------------------------------------------------ dense
def _dequantize(raw: np.ndarray, type_name: str) -> np.ndarray:
    """The tensor's values as float32 (floats exactly, quantized types through tools/ggufq.py)."""
    if type_name == "F32":
        return raw.view(np.float32)
    if type_name == "F16":
        return raw.view(np.float16).astype(np.float32)
    if type_name == "BF16":
        return (raw.view(np.uint16).astype(np.uint32) << 16).view(np.float32)
    return ggufq.dequantize(raw, ggufq.GGMLQuantizationType[type_name]).astype(np.float32).reshape(-1)


def bf16_bytes(raw: np.ndarray, type_name: str) -> bytes:
    """Round-to-nearest-even BF16 of the tensor's values (ggml's conversion, halfway cases included)."""
    values = ggufq.dequantize(raw, ggufq.GGMLQuantizationType[type_name])
    if not np.isfinite(values).all():
        raise ValueError("cannot convert non-finite weights to BF16")
    return ggufq.quantize(values, ggufq.GGMLQuantizationType.BF16).tobytes()


def f16_values(values: np.ndarray, name: str) -> np.ndarray:
    """values -> F16, nearest-even; refuses a value that is not finite or overflows F16."""
    if not np.isfinite(values).all():
        raise ValueError(f"{name}: cannot convert non-finite weights to F16")
    with np.errstate(over="ignore"):
        h = values.astype(np.float16)
    if not np.isfinite(h).all():
        raise ValueError(f"{name}: a value is beyond F16's range, which the engine reads it as")
    return h


def convert_tensor(name: str, type_name: str, raw: np.ndarray, compat_bf16: bool, arch=QWEN):
    """The dense form of one tensor: (index kind, stored bytes, destination bytes, record or None), or an error string.
        `record` describes a conversion for meta.conversions."""
    form = arch.form_of(name)
    if form is None or form == type_name:
        return KIND[type_name], raw.tobytes(), raw.nbytes, None
    values = _dequantize(raw, type_name)
    rec = {"src_type": type_name, "dst_type": form}
    if form == "BF16":
        u = values.view(np.uint32) if type_name == "F32" else None
        if u is not None and not np.count_nonzero(u & 0xFFFF):
            if not np.isfinite(values).all():
                return f"{name} is F32 with non-finite values; refused rather than converted"
            data = (u >> 16).astype("<u2").tobytes()       # exact BF16 values
            rec.update(method="f32->bf16: the values are BF16's (low 16 bits zero)", exact=True, max_abs_err=0.0)
            return "4", data, len(data), rec
        if not compat_bf16:
            what = "F32 with values that are not BF16" if type_name == "F32" else type_name
            return f"{name} is {what}, but the engine requires BF16; use --compat-bf16 (rounds to nearest-even)"
        data = bf16_bytes(raw, type_name)
        got = (np.frombuffer(data, dtype="<u2").astype(np.uint32) << 16).view(np.float32)
        rec.update(method="dequantize->bf16 round-to-nearest-even (--compat-bf16)" if type_name not in FLOAT else
                   "%s->bf16 round-to-nearest-even (--compat-bf16)" % type_name.lower())
        rec.update(exact=bool(np.array_equal(got, values)),
                   max_abs_err=float(np.max(np.abs(got.astype(np.float64) - values))) if values.size else 0.0)
        return "4", data, len(data), rec
    if form == "F16":
        if type_name != "F32" and not compat_bf16:
            return f"{name} is {type_name}, but the engine reads F16; use --compat-bf16 (rounds to nearest-even)"
        h = f16_values(values, name)
        err = float(np.max(np.abs(h.astype(np.float64) - values))) if values.size else 0.0
        rec.update(exact=bool(np.array_equal(h.astype(np.float32), values)), max_abs_err=err)
        if type_name == "F32":
            # stored as F32 (index kind 3); the loader narrows to F16
            rec.update(method="f32->f16 by the loader (index kind 3, round-to-nearest-even)")
            return "3", raw.tobytes(), raw.nbytes // 2, rec
        rec.update(method="%s->f16 round-to-nearest-even (--compat-bf16)" % type_name.lower())
        data = h.astype("<f2").tobytes()
        return "5", data, len(data), rec
    # form == "F32"
    if type_name not in ("F16", "BF16") and not compat_bf16:
        return f"{name} is {type_name}, but the engine reads F32; use --compat-bf16"
    data = values.astype("<f4").tobytes()
    rec.update(method="%s->f32 widening" % type_name.lower(), exact=True, max_abs_err=0.0)
    return "2", data, len(data), rec


class Dense:
    """The `dense` section: `plan` validates every tensor first, `chunks` streams the pool and fills `rows` and `records`,
        `index_text` is the `dense.index`."""

    def __init__(self, model: SourceModel, compat_bf16: bool = False, arch=QWEN):
        self.model = model
        self.arch = arch
        self.compat_bf16 = compat_bf16
        self.todo = []
        self.rows: list[list[str]] = []
        self.records: list[dict] = []
        self.pool_bytes = 0
        problems = []
        for name, (g, t, p) in model.where.items():
            if is_expert(name) or name in arch.not_in_dense:
                continue
            if len(t.shape) > 2:
                raise ConvertError(f"tensor {name} has {len(t.shape)} dimensions; the index holds two")
            form = arch.form_of(name)
            # quantized tensors are served from the source unless the pool needs them dequantized (--compat-bf16)
            native = t.type_name not in FLOAT and arch.native(name, t.type_name)
            if t.type_name not in FLOAT and not native and not compat_bf16:
                problems.append(f"{name} is {t.type_name}, but the engine requires {form}; use --compat-bf16")
            self.todo.append((name, t, p, native))
        if problems:
            more = f"\n... and {len(problems) - 8} more" if len(problems) > 8 else ""
            raise ConvertError("\n".join(problems[:8]) + more)

    def chunks(self):
        at = 0
        for name, t, p, native in self.todo:
            ne0 = int(t.shape[0])
            ne1 = int(t.shape[1]) if len(t.shape) > 1 else 0
            if native:
                self.rows.append([name, "0", "0", "0", "0", "0", "0", str(ne0), str(ne1), "8", "0", "32"] + ["0"] * 7)
                continue
            raw = self.model.bytes(name)
            try:
                got = convert_tensor(name, t.type_name, raw, self.compat_bf16, self.arch)
            except ValueError as e:
                got = str(e)
            if isinstance(got, str):
                raise ConvertError(got)
            kind, data, dst_bytes, rec = got
            if rec is not None:
                self.records.append({"name": name, "shard": p.name, **rec,
                                     "source_sha256": hashlib.sha256(raw.tobytes()).hexdigest()})
            self.rows.append([name, "0", kind, str(at), str(len(data)), "0", str(dst_bytes), str(ne0), str(ne1),
                              "0", "0", "1"] + ["0"] * 7)
            yield data
            pad = (-len(data)) % ALIGN
            if pad:
                yield b"\0" * pad
            at += len(data) + pad

    def index_text(self) -> bytes:
        at = 0
        for r in self.rows:
            r[5] = str(at)
            at += (int(r[6]) + ALIGN - 1) // ALIGN * ALIGN
        self.pool_bytes = at
        out = ["# dense index v3\n", "# align %d pool %d tensors %d\n" % (ALIGN, at, len(self.rows))]
        out += [" ".join(r) + "\n" for r in self.rows]
        return "".join(out).encode("utf-8")


# ------------------------------------------------------------------ experts
class Experts:
    """The `experts` section and its `experts.layout`; each role is resolved by name in the shard that holds it."""

    def __init__(self, model: SourceModel):
        T = {n: w[1] for n, w in model.where.items()}
        exps = [n for n in T if n.startswith("blk.") and n.endswith("_exps.weight")]
        if not exps:
            raise ConvertError("the model has no expert tensors (blk.N.ffn_{gate,up,down}_exps.weight)")
        self.model = model
        self.n_layers = 1 + max(int(n.split(".")[1]) for n in exps)
        # router rows = experts kept (a pruned model has fewer)
        self.n_expert = int(T["blk.0.ffn_gate_inp.weight"].shape[1])
        if any(int(T["blk.%d.ffn_gate_inp.weight" % l].shape[1]) != self.n_expert for l in range(self.n_layers)):
            raise ConvertError("the routers disagree on the expert count; a per-layer pruned model cannot be converted")
        self.layout = []
        offset = 0
        for l in range(self.n_layers):
            names = ["blk.%d.ffn_%s_exps.weight" % (l, r) for r in ROLES]
            if any(n not in T for n in names):
                raise ConvertError("layer %d: missing %s" % (l, ", ".join(n for n in names if n not in T)))
            ts = [T[n] for n in names]
            if any(t.expected_bytes() is None or len(t.shape) != 3 or int(t.shape[2]) != self.n_expert for t in ts):
                raise ConvertError("layer %d: an expert tensor is not [*, *, %d] of whole blocks" % (l, self.n_expert))
            per = [t.expected_bytes() // self.n_expert for t in ts]
            if per[0] != per[1] or ts[0].type_name != ts[1].type_name:
                raise ConvertError("layer %d: gate and up differ in type" % l)
            blob = sum(per)
            self.layout.append((l, ts[0].type_id, ts[2].type_id, offset, blob, names))
            offset += blob * self.n_expert
        self.total = offset

    def layout_text(self) -> bytes:
        head = ("# experts v5: layer gu_type d_type offset blob_bytes (n_expert %d, total %d)\n"
                % (self.n_expert, self.total))
        return (head + "".join("%d %d %d %d %d\n" % (l, gt, dt, off, blob)
                               for l, gt, dt, off, blob, _ in self.layout)).encode("utf-8")

    def chunks(self, group: int = 32):
        """The blobs layer by layer, `group` experts at a time: each expert is [gate | up | down] of its slices."""
        for l, gt, dt, off, blob, names in self.layout:
            arrays = [self.model.bytes(n).reshape(self.n_expert, -1) for n in names]
            for s in range(0, self.n_expert, group):
                yield np.concatenate([a[s:s + group] for a in arrays], axis=1).tobytes()


NVFP4 = 40
NVFP4_TAIL = 16          # per expert, after down: FP32 {s_gate, s_up, s_down, 0}


def nvfp4_row_bytes(n: int) -> int:
    """Bytes of one NVFP4 row of n values (blocks of 64 in 36 bytes)."""
    if n % 64:
        raise ConvertError(f"an NVFP4 row of {n} values is not whole blocks")
    return n // 64 * 36


def nvfp4_layout_text(n_expert: int, total: int, layout) -> bytes:
    """The v6 `experts.layout`: one `layer gu_type d_type offset blob_bytes` line per layer."""
    head = ("# experts v6: layer gu_type d_type offset blob_bytes (n_expert %d, total %d); "
            "an NVFP4 blob ends in FP32 {s_gate, s_up, s_down, 0}\n" % (n_expert, total))
    return (head + "".join("%d %d %d %d %d\n" % x for x in layout)).encode("utf-8")


# ------------------------------------------------------------------ the drafter
MTP_H, MTP_FF, MTP_NE = 2560, 640, 512
MTP_EXPERTS = ("mtp.layers.0.mlp.experts.gate_up_proj", "mtp.layers.0.mlp.experts.down_proj")
MTP_Q8 = {"fc_embedding.weight", "fc_hidden.weight", "self_attn.q_proj.weight", "self_attn.k_proj.weight",
          "self_attn.v_proj.weight", "self_attn.o_proj.weight", "self_attn.indexer.index_qk_proj.weight",
          "mlp.shared_expert.gate_proj.weight", "mlp.shared_expert.up_proj.weight",
          "mlp.shared_expert.down_proj.weight"}


def q2_0(x: np.ndarray) -> np.ndarray:
    """Float32 values in blocks of 64 -> Q2_0 blocks (fp16 d, 16 bytes of 2-bit codes, code = q + 1, four per byte).
        The block scale minimizes squared error over {-1, 0, 1, 2} x d (the ggml reference uses d = max|w| and never the +2 level)."""
    x = x.reshape(-1, 64).astype(np.float32)
    amax = np.abs(x).max(axis=1, keepdims=True)
    best_err = np.full((x.shape[0], 1), np.inf, dtype=np.float32)
    best_d = np.zeros_like(amax)
    for f in np.linspace(0.5, 1.0, 17, dtype=np.float32):      # scale search range: amax/2 .. amax
        d = amax * f
        inv = np.where(d > 0, 1.0 / np.where(d > 0, d, 1.0), 0.0)
        q = np.clip(np.rint(x * inv), -1, 2)
        err = ((q * d - x) ** 2).sum(axis=1, keepdims=True)
        better = err < best_err
        best_err = np.where(better, err, best_err)
        best_d = np.where(better, d, best_d)
    d16 = best_d.astype(np.float16)
    d = d16.astype(np.float32)                                   # quantize against the stored (fp16) scale
    inv = np.where(d > 0, 1.0 / np.where(d > 0, d, 1.0), 0.0)
    codes = (np.clip(np.rint(x * inv), -1, 2) + 1).astype(np.uint8)
    c = codes.reshape(-1, 16, 4)
    packed = (c[:, :, 0] | (c[:, :, 1] << 2) | (c[:, :, 2] << 4) | (c[:, :, 3] << 6)).astype(np.uint8)
    out = np.empty((x.shape[0], 18), dtype=np.uint8)
    out[:, :2] = d16.view(np.uint8).reshape(-1, 2)
    out[:, 2:] = packed
    return out.reshape(-1)


def q8_0(x: np.ndarray) -> bytes:
    """ggml's reference rounding over rows of a float32 array: blocks of 32, fp16 d = amax / 127."""
    b = x.reshape(-1, 32).astype(np.float32)
    d = np.abs(b).max(axis=1) / 127.0
    inv = np.where(d > 0, 1.0 / np.where(d > 0, d, 1.0), 0.0).astype(np.float32)
    v = b * inv[:, None]
    q = (np.sign(v) * np.floor(np.abs(v) + 0.5)).astype(np.int8)
    out = np.empty((b.shape[0], 34), dtype=np.uint8)
    out[:, :2] = d.astype(np.float16).view(np.uint8).reshape(-1, 2)
    out[:, 2:] = q.view(np.uint8)
    return out.tobytes()


def _bf16_to_f32(u16: np.ndarray) -> np.ndarray:
    return (u16.astype(np.uint32) << 16).view(np.float32)


def mtp_blob(gu: np.ndarray, dn: np.ndarray) -> bytes:
    """One expert in the engine's blob layout: gu is its (1280, 40, 18) Q2_0 rows (gate 0..639, up 640..1279), dn its
        (2560, 10, 18). Gate/up rows interleave (2r = gate r, 2r+1 = up r), then the down rows; codes of all rows in one
        plane, fp16 scales in another."""
    inter = np.empty_like(gu)
    inter[0::2] = gu[:MTP_FF]
    inter[1::2] = gu[MTP_FF:]
    out = b"".join([inter[:, :, 2:].tobytes(), dn[:, :, 2:].tobytes(), inter[:, :, :2].tobytes(),
                    dn[:, :, :2].tobytes()])
    assert len(out) == 3 * (MTP_H * MTP_FF * 18 // 64), len(out)
    return out


class Mtp:
    """The draft layer's sections, from a directory written by `mtp_fetch.py fetch`."""

    def __init__(self, src, tuned_dense=None):
        self.src = pathlib.Path(src)
        self.tuned = pathlib.Path(tuned_dense) if tuned_dense else None
        self.tuned_sha256 = None
        if self.tuned is not None:
            if not self.tuned.is_file():
                raise ConvertError(f"missing input: {self.tuned}")
            h = hashlib.sha256()
            with open(self.tuned, "rb") as f:
                for b in iter(lambda: f.read(1 << 24), b""):
                    h.update(b)
            self.tuned_sha256 = h.hexdigest()
        mf = self.src / "mtp-manifest.json"
        if not mf.is_file():
            raise ConvertError(f"{mf} not found (run tools/convert/mtp_fetch.py fetch --out {self.src})")
        self.manifest = {t["name"]: t for t in json.loads(mf.read_text())}
        for n in MTP_EXPERTS:
            if n not in self.manifest:
                raise ConvertError(f"{mf}: tensor {n} is missing")
        self.dense_lines: list[str] = []
        self.dense_bytes = 0

    def source_sha256(self) -> str:
        return hashlib.sha256(json.dumps({n: t["sha256"] for n, t in self.manifest.items()},
                                         sort_keys=True).encode()).hexdigest()

    def dense_chunks(self):
        """The dense section: the tuned file when given (its size must match the source layout), else the source."""
        if self.tuned is None:
            yield from self._source_dense_chunks()
            return
        for _ in self._source_dense_chunks():     # fills the index and the expected size
            pass
        size = self.tuned.stat().st_size
        if size != self.dense_bytes:
            raise ConvertError(f"{self.tuned}: {size} bytes, the source layout is {self.dense_bytes}")
        with open(self.tuned, "rb") as f:
            for b in iter(lambda: f.read(1 << 24), b""):
                yield b

    def _source_dense_chunks(self):
        """Every tensor but the experts, by name, each starting at a multiple of 256; fills `dense_lines`."""
        off = 0
        for name in sorted(self.manifest):
            if name in MTP_EXPERTS:
                continue
            t = self.manifest[name]
            shape = t["shape"]
            short = name[len("mtp."):] if name.startswith("mtp.") else name
            short = short[len("layers.0."):] if short.startswith("layers.0.") else short
            raw16 = np.fromfile(self.src / t["file"], dtype="<u2")
            if raw16.size != int(np.prod(shape)):
                raise ConvertError(f"{t['file']}: {raw16.size} values, expected shape {shape}")
            if len(shape) == 1:      # norm weights: apply the Gemma RMSNorm "+1"
                arr = _bf16_to_f32(raw16) + 1.0
                raw, kind, rows, cols = arr.astype(np.float32).tobytes(), "f32", 1, arr.size
            else:
                u16 = raw16.reshape(shape[0], -1)
                rows, cols = u16.shape
                if short in MTP_Q8:
                    raw, kind = q8_0(_bf16_to_f32(u16)), "q8_0"
                else:
                    raw, kind = u16.tobytes(), "bf16"
            pad = (-off) % 256
            if pad:
                yield b"\0" * pad
                off += pad
            yield raw
            self.dense_lines.append(f"{short} {kind} {rows} {cols} {off} {len(raw)}")
            off += len(raw)
        self.dense_bytes = off

    def dense_index(self) -> bytes:
        return ("\n".join(self.dense_lines) + "\n").encode("utf-8")

    def expert_chunks(self):
        """512 blobs, one per expert; each expert is quantized to Q2_0 on its own, so the memory stays small."""
        gu_t, dn_t = (self.manifest[n] for n in MTP_EXPERTS)
        if gu_t["shape"] != [MTP_NE, 2 * MTP_FF, MTP_H] or dn_t["shape"] != [MTP_NE, MTP_H, MTP_FF]:
            raise ConvertError(f"unexpected drafter expert shapes {gu_t['shape']} {dn_t['shape']}")
        gu = np.memmap(self.src / gu_t["file"], dtype="<u2", mode="r", shape=tuple(gu_t["shape"]))
        dn = np.memmap(self.src / dn_t["file"], dtype="<u2", mode="r", shape=tuple(dn_t["shape"]))
        for e in range(MTP_NE):
            g = q2_0(_bf16_to_f32(np.asarray(gu[e]))).reshape(2 * MTP_FF, MTP_H // 64, 18)
            d = q2_0(_bf16_to_f32(np.asarray(dn[e]))).reshape(MTP_H, MTP_FF // 64, 18)
            yield mtp_blob(g, d)


def draft_vocab_bytes(path, n_vocab: int | None = None) -> bytes:
    """A draft vocabulary file: int32 token ids, none outside the vocabulary."""
    data = pathlib.Path(path).read_bytes()
    if len(data) % 4:
        raise ConvertError(f"{path}: not a list of int32 ids ({len(data)} bytes)")
    ids = np.frombuffer(data, dtype="<i4")
    if ids.size == 0 or (ids < 0).any() or (n_vocab is not None and (ids >= n_vocab).any()):
        raise ConvertError(f"{path}: an id lies outside the vocabulary" if ids.size else f"{path}: empty")
    return data


# ------------------------------------------------------------------ the n-gram table from a per-tensor FP8 checkpoint
PLE_DIM = 160


def fp8_e4m3_table() -> np.ndarray:
    """All 256 E4M3 (finite-only, bias 7) codes as float32; the two NaN codes are NaN."""
    b = np.arange(256)
    sign = np.where(b >> 7, -1.0, 1.0)
    e, m = (b >> 3) & 15, b & 7
    v = np.where(e == 0, m / 8.0 * 2.0 ** -6, (1 + m / 8.0) * 2.0 ** (e.astype(np.float64) - 7))
    v = np.where((e == 15) & (m == 7), np.nan, v)
    return (sign * v).astype(np.float32)


def fp8_rows_to_q8_0(raw: np.ndarray, scale: float) -> bytes:
    """Rows of PLE_DIM FP8 codes times the tensor's scale -> Q8_0 rows (5 blocks of 34 bytes each)."""
    x = fp8_e4m3_table()[raw.reshape(-1)] * np.float32(scale)
    if not np.isfinite(x).all():
        raise ConvertError("the FP8 table holds a non-finite value")
    return q8_0(x)


def _ple_job(args):
    path, off, rows, scale = args
    raw = np.fromfile(path, dtype=np.uint8, count=rows * PLE_DIM, offset=off)
    return fp8_rows_to_q8_0(raw, scale)


def read_safetensors_header(path):
    """(header, offset of the data) of a safetensors file; refuses a file shorter than its header says."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        raw = f.read(8)
        if len(raw) < 8:
            raise ConvertError(f"{path}: too short for a safetensors header")
        n = struct.unpack("<Q", raw)[0]
        if 8 + n > size:
            raise ConvertError(f"{path}: truncated, the header alone ({n} bytes) is past the end of the file")
        head = json.loads(f.read(n))
    end = 0
    for k, v in head.items():
        if k == "__metadata__":
            continue
        a, b = v["data_offsets"]
        if not 0 <= a <= b:
            raise ConvertError(f"{path}: {k} has bad data offsets {a}..{b}")
        end = max(end, b)
    if 8 + n + end > size:
        raise ConvertError(f"{path}: truncated, its header covers {8 + n + end} bytes but the file has {size}")
    return head, 8 + n


def bounded_imap(pool, fn, jobs, depth: int):
    """pool.imap with at most `depth` results waiting, so a slow writer never piles them up in memory."""
    q = deque()
    for j in jobs:
        q.append(pool.apply_async(fn, (j,)))
        if len(q) >= depth:
            yield q.popleft().get()
    while q:
        yield q.popleft().get()


class PleFp8:
    """The PLE n-gram table of a checkpoint that stores it as FP8 shards (`...ngram_embedding.shard_K.weight`) with one
    BF16 `...ngram_embedding.weight_scale`, as Q8_0 rows: values are code x scale, re-blocked per 32 values."""

    def __init__(self, path, chunk_rows: int = 65536):
        self.path, self.chunk_rows = path, chunk_rows
        head, base = read_safetensors_header(path)
        scales = [k for k in head if k.endswith("ngram_embedding.weight_scale")]
        if len(scales) != 1:
            raise ConvertError(f"{path}: {len(scales)} ngram_embedding.weight_scale tensors")
        stem = scales[0][:-len("weight_scale")]
        sc = head[scales[0]]
        if sc["dtype"] != "BF16" or sc["shape"] != [1]:
            raise ConvertError(f"{path}: the PLE scale is not one BF16 value")
        with open(path, "rb") as f:
            f.seek(base + sc["data_offsets"][0])
            u = np.frombuffer(f.read(2), dtype="<u2").astype(np.uint32) << 16
        self.scale = float(u.view(np.float32)[0])
        shards = []
        while f"{stem}shard_{len(shards)}.weight" in head:
            t = head[f"{stem}shard_{len(shards)}.weight"]
            if t["dtype"] != "F8_E4M3" or len(t["shape"]) != 2 or t["shape"][1] != PLE_DIM:
                raise ConvertError(f"{path}: shard {len(shards)} is not FP8 rows of {PLE_DIM}")
            shards.append((base + t["data_offsets"][0], t["shape"][0]))
        if not shards:
            raise ConvertError(f"{path}: no PLE shards")
        self.shards = shards
        self.rows = sum(r for _, r in shards)
        self.row_bytes = PLE_DIM // 32 * 34
        self.nbytes = self.rows * self.row_bytes

    def jobs(self):
        for off, rows in self.shards:
            for r in range(0, rows, self.chunk_rows):
                n = min(self.chunk_rows, rows - r)
                yield (self.path, off + r * PLE_DIM, n, self.scale)

    def chunks(self, pool, depth: int = 16):
        """The Q8_0 rows in order, made on `pool` with at most `depth` chunks in flight."""
        yield from bounded_imap(pool, _ple_job, self.jobs(), depth)


# ------------------------------------------------------------------ Q6_K requantization of the dense projections
Q6K_BLOCK, Q6K_BYTES = 256, 210


def q6_k(x: np.ndarray) -> bytes:
    """Q6_K blocks of 256 over a float32 array (length a multiple of 256): per 16-value group the least-squares
    scale from the signed-max starting grid, 8-bit group scales against one fp16 super-scale, values re-rounded
    against the scales as stored. The layout is ggml block_q6_K."""
    nb = x.size // Q6K_BLOCK
    x = np.ascontiguousarray(x, dtype=np.float32).reshape(nb, 16, 16)
    with np.errstate(divide="ignore", invalid="ignore"):
        mx = np.take_along_axis(x, np.abs(x).argmax(-1)[..., None], -1)[..., 0]
        start = np.where(mx != 0, -32.0 / np.where(mx == 0, 1, mx), 0.0)
        l = np.clip(np.rint(x * start[..., None]), -32, 31)
        den, num = (l * l).sum(-1), (x * l).sum(-1)
        scale = np.where(den > 0, num / np.where(den == 0, 1, den), 0.0)
        smax = np.take_along_axis(scale, np.abs(scale).argmax(-1)[..., None], -1)[..., 0]
        d = smax / 127.0
        d16 = d.astype(np.float16)
        sc = np.clip(np.rint(scale / np.where(d == 0, 1, d)[..., None]), -128, 127)
        eff = d16.astype(np.float32)[..., None] * sc
        q = np.where(eff[..., None] != 0, np.rint(x / np.where(eff == 0, 1, eff)[..., None]), 0)
    q = np.clip(q, -32, 31).astype(np.int32) + 32
    q = q.reshape(nb, 2, 4, 32)
    ql = np.empty((nb, 2, 64), np.uint8)
    ql[..., :32] = (q[:, :, 0] & 15) | ((q[:, :, 2] & 15) << 4)
    ql[..., 32:] = (q[:, :, 1] & 15) | ((q[:, :, 3] & 15) << 4)
    qh = (q[:, :, 0] >> 4) | ((q[:, :, 1] >> 4) << 2) | ((q[:, :, 2] >> 4) << 4) | ((q[:, :, 3] >> 4) << 6)
    out = np.empty((nb, Q6K_BYTES), np.uint8)
    out[:, 0:128] = ql.reshape(nb, 128)
    out[:, 128:192] = qh.astype(np.uint8).reshape(nb, 64)
    out[:, 192:208] = sc.astype(np.int8).view(np.uint8).reshape(nb, 16)
    out[:, 208:210] = d16.view(np.uint8).reshape(nb, 2)
    return out.tobytes()


# Q8_0 dense tensors of a pack that move to Q6_K: GGUF name pattern -> checkpoint name under `layers.N.`.
Q6K_TARGETS = [
    (re.compile(r"blk\.(\d+)\.attn_qkv\.weight"), "linear_attn.in_proj_qkv"),
    (re.compile(r"blk\.(\d+)\.attn_gate\.weight"), "linear_attn.in_proj_z"),
    (re.compile(r"blk\.(\d+)\.ssm_out\.weight"), "linear_attn.out_proj"),
    (re.compile(r"blk\.(\d+)\.ffn_gate_shexp\.weight"), "mlp.shared_expert.gate_proj"),
    (re.compile(r"blk\.(\d+)\.ffn_up_shexp\.weight"), "mlp.shared_expert.up_proj"),
    (re.compile(r"blk\.(\d+)\.ffn_down_shexp\.weight"), "mlp.shared_expert.down_proj"),
    (re.compile(r"blk\.(\d+)\.attn_q\.weight"), "self_attn.q_proj"),
    (re.compile(r"blk\.(\d+)\.attn_k\.weight"), "self_attn.k_proj"),
    (re.compile(r"blk\.(\d+)\.attn_v\.weight"), "self_attn.v_proj"),
    (re.compile(r"blk\.(\d+)\.attn_output\.weight"), "self_attn.o_proj"),
]
Q6K_VALUES_PER_JOB = 1 << 22
# The GGUF lists the 48 value heads (128 wide) as (ratio r, key head k) = r * 16 + k, the checkpoint as k * 3 + r.
# The GGUF's GDN qkv rows after the q and k parts (4096), its z rows and its out-projection columns follow that order.
GDN_HEAD, GDN_V_HEADS, GDN_K_HEADS = 128, 48, 16
GDN_QK_ROWS = 4096
Q6K_LAYOUT = {"attn_qkv": "rows", "attn_gate": "rows", "ssm_out": "cols"}


def gdn_block_source() -> np.ndarray:
    j = np.arange(GDN_V_HEADS)
    return (j % GDN_K_HEADS) * (GDN_V_HEADS // GDN_K_HEADS) + j // GDN_K_HEADS


def gdn_index(n: int, first: int) -> np.ndarray:
    """Checkpoint index of each of the GGUF's n positions: identity below `first`, head-block reordered above."""
    idx = np.arange(n)
    blocks = (n - first) // GDN_HEAD
    idx[first:] = first + (gdn_block_source()[:blocks, None] * GDN_HEAD + np.arange(GDN_HEAD)).reshape(-1)
    return idx


def q6k_target(name: str, info: dict):
    """The checkpoint tensor name for a Q8_0 dense tensor that can move to Q6_K, else None (rows must be whole blocks)."""
    if info["type"] != "Q8_0" or info["shape"][0] % Q6K_BLOCK:
        return None
    for rx, hf in Q6K_TARGETS:
        m = rx.fullmatch(name)
        if m:
            return f"model.language_model.layers.{m.group(1)}.{hf}.weight"
    return None


def q8_0_values(raw: np.ndarray) -> np.ndarray:
    b = raw.reshape(-1, 34)
    return b[:, :2].copy().view(np.float16).astype(np.float32) * b[:, 2:].view(np.int8).astype(np.float32)


def _q6k_job(args):
    """One row range: its Q6_K bytes, and the largest distance between the Q8_0 values and the BF16 source in Q8_0 steps."""
    pack, off, ne0, row0, rows, st_path, st_off, layout = args
    raw = np.fromfile(pack, dtype=np.uint8, count=rows * ne0 // 32 * 34, offset=off + row0 * ne0 // 32 * 34)
    q8 = q8_0_values(raw).reshape(rows, ne0)
    if st_path is None:
        return q6_k(q8), 0.0
    if layout is None:
        u = np.fromfile(st_path, dtype="<u2", count=rows * ne0, offset=st_off + 2 * row0 * ne0)
    else:
        kind, ne1 = layout
        mm = np.memmap(st_path, dtype="<u2", mode="r", offset=st_off, shape=(ne1, ne0))
        if kind == "rows":
            u = mm[gdn_index(ne1, GDN_QK_ROWS if ne1 > GDN_V_HEADS * GDN_HEAD else 0)[row0:row0 + rows]]
        else:
            u = mm[row0:row0 + rows][:, gdn_index(ne0, 0)]
    src = _bf16_to_f32(np.ascontiguousarray(u)).reshape(rows, ne0)
    step = np.abs(src).reshape(-1, 32).max(axis=1) / 127.0
    err = np.abs(q8 - src).reshape(-1, 32).max(axis=1)
    return q6_k(src), float((err / np.maximum(step, 2e-5)).max())


class DenseQ6K:
    """Q6_K stand-ins for the Q8_0 dense projections of a pack, quantized from the checkpoint's BF16 tensors when
    `checkpoint` (a directory of safetensors shards) is given, else from the pack's own Q8_0 values. Each BF16
    tensor is checked against the pack's Q8_0 copy: a name or layout mismatch shows as a distance above one step."""

    def __init__(self, tensors: dict, checkpoint=None):
        hf = {}
        if checkpoint:
            for p in sorted(pathlib.Path(checkpoint).glob("model-0*.safetensors")):
                head, base = read_safetensors_header(p)
                for k, t in head.items():
                    if k != "__metadata__":
                        hf[k] = (str(p), base + t["data_offsets"][0], t["dtype"], t["shape"])
        self.plan, self.layout = {}, {}
        for n, info in tensors.items():
            want = q6k_target(n, info)
            if want is None:
                continue
            src = hf.get(want)
            if checkpoint and (src is None or src[2] != "BF16" or list(reversed(info["shape"])) != src[3]):
                raise ConvertError(f"{n}: no matching BF16 tensor {want} in {checkpoint}")
            self.plan[n] = src
            kind = Q6K_LAYOUT.get(n.split(".", 2)[2].rsplit(".weight", 1)[0])
            if kind:
                self.layout[n] = (kind, info["shape"][1])
        self.worst = 0.0

    @staticmethod
    def nbytes(info: dict) -> int:
        return info["shape"][0] // Q6K_BLOCK * Q6K_BYTES * info["shape"][1]

    def jobs(self, name, info, pack, gguf_off):
        ne0, ne1 = info["shape"]
        step = max(1, Q6K_VALUES_PER_JOB // ne0)
        src = self.plan[name]
        for r in range(0, ne1, step):
            yield (pack, gguf_off + info["offset"], ne0, r, min(step, ne1 - r),
                   src[0] if src else None, src[1] if src else 0, self.layout.get(name))

    def chunks(self, name, info, pack, gguf_off, pool):
        for blob, worst in pool.imap(_q6k_job, self.jobs(name, info, pack, gguf_off), chunksize=1):
            self.worst = max(self.worst, worst)
            if worst > 1.05:
                raise ConvertError(f"{name}: the BF16 source is {worst:.2f} Q8_0 steps from the pack's copy")
            yield blob

# ===== file: tools/convert/mtp_fetch.py =====
"""Fetch the MTP tensors from the BF16 checkpoint without downloading it.

The source GGUF has no MTP head; the BF16 checkpoint has 31 `mtp.*` tensors across 28 shards. Safetensors headers
list each tensor's byte range, so HTTP range requests read only the headers and those tensors.

    python tools/convert/mtp_fetch.py inventory --out DIR          # headers only
    python tools/convert/mtp_fetch.py fetch --out DIR [--only SUBSTR]  # the tensors, resumable
    python tools/convert/mtp_fetch.py verify --out DIR             # against SHA256 (exit 3: bad ones)

`fetch` writes one raw file per tensor plus `mtp-manifest.json` (dtype, shape, source shard, byte range, sha256).

A mirror that ignores Range answers 200 with the whole shard (possibly cut to length), which size and hash of the
download alone would not catch. A range read therefore needs a 206 with the asked Content-Range, and every tensor
of the pinned revision is checked against SHA256.
"""
import argparse
import hashlib
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request

# fixed checkpoint commit so every install reads the same tensors; --revision overrides it, and resolve_repo falls
# back to the current files with a message when the commit is gone
PINNED_REVISION = "de4b8e4d43b917e7706784d8bb445c9af86a3540"
REVISION = PINNED_REVISION
# HF_ENDPOINT may name a mirror; the SHA256 checks still apply
HF_ENDPOINT = (os.environ.get("HF_ENDPOINT") or "").strip().rstrip("/") or "https://huggingface.co"
REPO = HF_ENDPOINT + "/Qwen/Qwen3.8-Flash-Next/resolve/%s/" % REVISION
PINNED = HF_ENDPOINT + "/Qwen/Qwen3.8-Flash-Next/resolve/%s/" % PINNED_REVISION   # the revision SHA256 covers
DTYPE_BYTES = {"BF16": 2, "F16": 2, "F32": 4, "F8_E4M3": 1, "I64": 8, "I32": 4}
BAD = 3                                             # `verify`'s exit code: a tensor is missing or corrupt

# sha256 of every MTP tensor at PINNED_REVISION: catches a mirror that ignored Range and corrupted installs
SHA256 = {
    "mtp.fc_embedding.weight":
        "241d6e87ded317a61b1aeb3ca4e28c53f1caba09ad5be66092e91fe5e3e14751",
    "mtp.fc_hidden.weight":
        "97c8e427ad0177e2c713ed24a397d815b523eda15ef453f1ae6463f6c70a8f96",
    "mtp.hyper_connection_mixer.hc_norm.weight":
        "ce948a398e15aeaa403c424cda34e6eed296ab2570d591bb5ff3da642b255d64",
    "mtp.hyper_connection_mixer.input_mix_weight_down.weight":
        "c633d2841fda463e82045e0b93fb8cb8d511ec0c9eda616020f01aca18d71947",
    "mtp.hyper_connection_mixer.input_mix_weight_up.weight":
        "6565285f0ba2923703252763cfc11d8a9bda40ab42e7548e12464b04dbdbec13",
    "mtp.layers.0.attn_hyper_connection.block_inject_weight.weight":
        "c8afff64e7f54412d02120097c4255033c55245e1c1eed84cc630d428ccb864c",
    "mtp.layers.0.attn_hyper_connection.hc_norm.weight":
        "1fb619aeaaf637ccd70f46f1f8f8116e6a46448cb43026b246de8ee8286ed809",
    "mtp.layers.0.attn_hyper_connection.input_mix_weight_down.weight":
        "e80ac8bf3e270e9d5a949ec7c913329dc8de017d69851334820fae7b8c86a7f5",
    "mtp.layers.0.attn_hyper_connection.input_mix_weight_up.weight":
        "f80f1fd44e1183e7764ea496b11ff89ead9dd758f861d4e79a770bf54956b1a5",
    "mtp.layers.0.mlp.experts.down_proj":
        "539c46234f73bac70450f731f40e715f77ac32faf77def0f3f800e58aaabbbc0",
    "mtp.layers.0.mlp.experts.gate_up_proj":
        "839e98f9642ebc884b178abd2090b6dc8b0bcd81ed92d7f7a08314c6a68f96d5",
    "mtp.layers.0.mlp.gate.weight":
        "f0d35fa41b360a08bc0c5b1e5766aa42df960291716ee189208b5d2599462c20",
    "mtp.layers.0.mlp.shared_expert.down_proj.weight":
        "d2ced8c81367cc494599d1a8cdc279c3dd6579b628ded05acd7495e99cee01d8",
    "mtp.layers.0.mlp.shared_expert.gate_proj.weight":
        "f4a81109165853b3b64f36d809f98094b599763364070f5fa079200e1c386d4c",
    "mtp.layers.0.mlp.shared_expert.up_proj.weight":
        "172586861eea53efbff10afc43ded5b6e49c83879e9efc01e089c652bbb0a156",
    "mtp.layers.0.mlp.shared_expert_gate.weight":
        "acdd306d9996d356e4667a16ff63af6fef086fc7f570f1bc00bbe882a2a93f6e",
    "mtp.layers.0.mlp_hyper_connection.block_inject_weight.weight":
        "c5782dd92b479a1f8ef43d683fe9c917fd9e8fba81f23c1aae8817bfe4b476a6",
    "mtp.layers.0.mlp_hyper_connection.hc_norm.weight":
        "21a504876b5e7b788301557c68dfb757999430eba36c66ec8ab96a63604dbfd1",
    "mtp.layers.0.mlp_hyper_connection.input_mix_weight_down.weight":
        "b71240036dea954e034d5ae04e097bcd7ac51cb28a44e4c93d198af3447278a1",
    "mtp.layers.0.mlp_hyper_connection.input_mix_weight_up.weight":
        "40756866bf2a5e82c28e407f717b6fa07614d593f78e79c7ceb2cd4ffe6dda94",
    "mtp.layers.0.self_attn.indexer.index_qk_proj.weight":
        "2befafae6e944c1fc651fd73d4d527eb6f138a6c1679065b003128ee3150e859",
    "mtp.layers.0.self_attn.indexer.k_layernorm.weight":
        "2db621f6a08b4085756fc69d60a4a1bd1d7fbf5d406456d2477075842efaae54",
    "mtp.layers.0.self_attn.indexer.q_layernorm.weight":
        "afaf59f68e251939970ffdeacad0667dffc657a522bea62cdc44a52816fc49d6",
    "mtp.layers.0.self_attn.k_norm.weight":
        "8f924b0357cf936e14fc34518205d000e1423aa60626827df81b0804fae8cdfd",
    "mtp.layers.0.self_attn.k_proj.weight":
        "1a295dc2d6e633e808aeb19e00c97856a1817bbfcc83f5b610e2bfcacfcb8c38",
    "mtp.layers.0.self_attn.o_proj.weight":
        "9d2fcf0ac6ac19de87f638f710cfd1af6ad14141e75b7d3d5d29fbd06f374219",
    "mtp.layers.0.self_attn.q_norm.weight":
        "57957aa1e7c5a3ac628bff0ec5522ad55a39b8c0b8bd2d324ae640e3771617ef",
    "mtp.layers.0.self_attn.q_proj.weight":
        "5220aef3d288f40759f7356af9ef453e5ac0256a38749a9bb803a4e9db58150d",
    "mtp.layers.0.self_attn.v_proj.weight":
        "39cb12f5424e965fa71fb81db0ee230c701b7b1e0b72c36641fde7e0e41bff41",
    "mtp.pre_fc_norm_embedding.weight":
        "04c4a570850e06f2d8913da8220d54d4c7f87db6eb6d45480b938e8ba41d6a86",
    "mtp.pre_fc_norm_hidden.weight":
        "b933b44559879449b1baf2af8385c5c237347307e3fd452394870cb53e6eab6d",
}


def get(url, start=None, end=None, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mtp-fetch"})
            if start is not None:
                req.add_header("Range", "bytes=%d-%d" % (start, end))
            with urllib.request.urlopen(req, timeout=120) as r:
                if start is not None:
                    check_range(r.status, r.headers.get("Content-Range"), start, end)
                data = r.read()
            if start is not None and len(data) != end - start + 1:
                raise IOError("short range read: %d of %d" % (len(data), end - start + 1))
            return data
        except Exception as e:  # retried, then surfaced
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
            print("retry %s: %s" % (url, e), file=sys.stderr)


def check_range(status, content_range, start, end):
    """A range read is only what was asked for when the server says so: 206 and `bytes START-END/...`."""
    if status != 206:
        raise IOError("range request not honoured: HTTP %s instead of 206 (a mirror or proxy that ignores the Range "
                      "header sends the whole file)" % status)
    want = "bytes %d-%d/" % (start, end)
    if not (content_range or "").strip().startswith(want):
        raise IOError("range request answered with Content-Range %r, not %s..." % (content_range, want))


def pinned():
    """The tensors' hashes are the pinned revision's: they apply when this run reads that revision."""
    return REPO == PINNED


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 24), b""):
            h.update(block)
    return h.hexdigest()


def resolve_repo():
    """REPO, or the repository's current files when the pinned revision is gone from it (a 404 on its index)."""
    global REPO
    try:
        req = urllib.request.Request(REPO + "model.safetensors.index.json", method="HEAD",
                                     headers={"User-Agent": "mtp-fetch"})
        urllib.request.urlopen(req, timeout=120).close()
    except urllib.error.HTTPError as e:
        if e.code == 404 and "/resolve/main/" not in REPO:
            print("the checkpoint's pinned revision %s is gone: reading its current files (main)" % REVISION,
                  file=sys.stderr)
            REPO = REPO.replace("/resolve/%s/" % REVISION, "/resolve/main/")
    except OSError:
        pass                                        # get() retries and reports it
    return REPO


def shard_header(shard):
    url = REPO + shard
    n = struct.unpack("<Q", get(url, 0, 7))[0]
    header = json.loads(get(url, 8, 8 + n - 1))
    return 8 + n, header


def inventory(out):
    index = json.loads(get(REPO + "model.safetensors.index.json"))["weight_map"]
    mtp = {k: v for k, v in index.items() if k.startswith("mtp.")}
    rows, total = [], 0
    for shard in sorted(set(mtp.values())):
        base, header = shard_header(shard)
        for name, meta in header.items():
            if name in mtp and mtp[name] == shard:
                a, b = meta["data_offsets"]
                rows.append(dict(name=name, shard=shard, dtype=meta["dtype"], shape=meta["shape"],
                                 start=base + a, end=base + b - 1, bytes=b - a))
                total += b - a
    missing = sorted(set(mtp) - set(r["name"] for r in rows))
    if missing:
        sys.exit("tensors named in the index but absent from their shard headers: %s" % missing)
    rows.sort(key=lambda r: r["name"])
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "mtp-inventory.json"), "w", encoding="utf-8") as f:
        json.dump(dict(repo=REPO, total_bytes=total, tensors=rows), f, indent=1)
    lines = ["# MTP block in the BF16 checkpoint", "", "%d tensors, %.3f GB, in %d shards." % (len(rows), total / 1e9, len(set(r["shard"] for r in rows))), "",
             "| tensor | dtype | shape | MB |", "|---|---|---|---:|"]
    for r in rows:
        lines.append("| `%s` | %s | %s | %.1f |" % (r["name"], r["dtype"], "x".join(map(str, r["shape"])), r["bytes"] / 1e6))
    text = "\n".join(lines) + "\n"
    with open(os.path.join(out, "mtp-inventory.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return rows


def fetch(out, only):
    inv_path = os.path.join(out, "mtp-inventory.json")
    tdir = os.path.join(out, "tensors")
    inv = None
    if os.path.exists(inv_path):
        try:
            with open(inv_path, encoding="utf-8") as f:
                inv = json.load(f)
        except ValueError:
            inv = None
    # byte ranges belong to one revision: an inventory from another repository or revision is read again
    same = isinstance(inv, dict) and inv.get("repo") == REPO
    rows = inv["tensors"] if same else inventory(out)
    os.makedirs(tdir, exist_ok=True)
    manifest = []
    chunk = 64 << 20
    for r in rows:
        if only and only not in r["name"]:
            continue
        path = os.path.join(tdir, r["name"] + ".bin")
        want = SHA256.get(r["name"]) if pinned() else None
        if os.path.exists(path) and not same and (
                want is None or os.path.getsize(path) != r["bytes"] or sha256_of(path) != want):
            os.remove(path)
        for attempt in range(2):
            have = os.path.getsize(path) if os.path.exists(path) else 0
            if have > r["bytes"]:
                os.remove(path)
                have = 0
            with open(path, "ab") as f:
                pos = r["start"] + have
                while pos <= r["end"]:
                    end = min(pos + chunk - 1, r["end"])
                    f.write(get(REPO + r["shard"], pos, end))
                    pos = end + 1
                    print("%s %.0f%%" % (r["name"], 100 * (pos - r["start"]) / r["bytes"]), file=sys.stderr)
            if os.path.getsize(path) != r["bytes"]:
                sys.exit("%s: size %d != %d" % (path, os.path.getsize(path), r["bytes"]))
            digest = sha256_of(path)
            if want is None or digest == want:
                break
            os.remove(path)                         # wrong bytes: fetch the tensor again, once
            if attempt:
                sys.exit("%s: sha256 %s is not the checkpoint's %s - the download source sends wrong data (a mirror "
                         "that ignores Range requests?)" % (r["name"], digest, want))
            print("%s: wrong bytes (sha256 %s...), fetching it again" % (r["name"], digest[:12]), file=sys.stderr)
        manifest.append(dict(r, file=os.path.relpath(path, out), sha256=digest))
    with open(os.path.join(out, "mtp-manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1)


def verify(out):
    """Names of fetched tensors that are missing or wrong against SHA256 ([] if right or nothing to check).
    Verdicts are cached in tensors/verified.json by size and mtime."""
    tdir = os.path.join(out, "tensors")
    if not pinned() or not os.path.isdir(tdir):
        return []
    stamp_path = os.path.join(tdir, "verified.json")
    try:
        with open(stamp_path, encoding="utf-8") as f:
            stamps = json.load(f)
    except (OSError, ValueError):
        stamps = {}
    bad, good = [], {}
    for name, want in sorted(SHA256.items()):
        path = os.path.join(tdir, name + ".bin")
        if not os.path.exists(path):
            bad.append(name)
            continue
        st = os.stat(path)
        key = [st.st_size, st.st_mtime_ns, want]
        if stamps.get(name) != key and sha256_of(path) != want:
            bad.append(name)
            continue
        good[name] = key
    try:
        with open(stamp_path, "w", encoding="utf-8") as f:
            json.dump(good, f, indent=1)
    except OSError:
        pass
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["inventory", "fetch", "verify"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--only")
    ap.add_argument("--revision", help="read this revision of the checkpoint instead of the pinned one (e.g. main); "
                                       "its tensors are not hash-checked")
    a = ap.parse_args()
    if a.revision:
        global REVISION, REPO
        REVISION = a.revision
        REPO = REPO.replace("/resolve/%s/" % PINNED_REVISION, "/resolve/%s/" % a.revision)
    if a.cmd == "verify":                           # offline
        bad = verify(a.out)
        for name in bad:
            print("MTP tensor missing or corrupt: %s" % name, file=sys.stderr)
        sys.exit(BAD if bad else 0)
    resolve_repo()
    inventory(a.out) if a.cmd == "inventory" else fetch(a.out, a.only)


if __name__ == "__main__":
    main()

# ===== file: tools/convert/pack_vision.py =====
"""The sections the vision sidecar reads from a .basalt file:

    vision/mmproj       the projector GGUF, whole
    vision/text_vocab   a vocab-only GGUF of the text model (metadata without tensors or split keys), used for the
                        image-marker tokenizer

vision_sections(mmproj_path, native_paths) -> [(section_name, bytes_or_None, src_path_or_None, offset, size)]"""
from __future__ import annotations

import os
import struct

GGUF_MAGIC = b"GGUF"
ALIGNMENT = 32
# value type id -> fixed size; strings and arrays are walked
_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def _skip_value(buf: bytes, pos: int, vtype: int) -> int:
    if vtype in _FIXED:
        return pos + _FIXED[vtype]
    if vtype == 8:
        (n,) = struct.unpack_from("<Q", buf, pos)
        return pos + 8 + n
    if vtype == 9:
        etype, count = struct.unpack_from("<IQ", buf, pos)
        pos += 12
        if etype in _FIXED:
            return pos + _FIXED[etype] * count
        for _ in range(count):
            pos = _skip_value(buf, pos, etype)
        return pos
    raise ValueError(f"unknown GGUF value type {vtype}")


def _kv_entries(buf: bytes):
    """(key, raw bytes of the whole entry) for each metadata entry of a GGUF v3 header in `buf`."""
    if buf[:4] != GGUF_MAGIC:
        raise ValueError("not a GGUF file")
    version, _n_tensors, n_kv = struct.unpack_from("<IQQ", buf, 4)
    if version != 3:
        raise ValueError(f"GGUF v{version}: only v3 is handled")
    pos = 24
    for _ in range(n_kv):
        start = pos
        (klen,) = struct.unpack_from("<Q", buf, pos)
        key = buf[pos + 8:pos + 8 + klen].decode("utf-8", "replace")
        pos += 8 + klen
        (vtype,) = struct.unpack_from("<I", buf, pos)
        pos = _skip_value(buf, pos + 4, vtype)
        yield key, buf[start:pos]


def _read_header_prefix(path: str) -> bytes:
    """The start of a GGUF up to the end of its metadata; tensor info and data follow and are not read."""
    size = 8 << 20
    with open(path, "rb") as f:
        while True:
            f.seek(0)
            buf = f.read(size)
            try:
                list(_kv_entries(buf))
                return buf
            except struct.error:
                if len(buf) < size:
                    raise
                size *= 4


def vocab_only_gguf(path: str) -> bytes | None:
    """The GGUF at `path` with its metadata only (no tensors, no split.* keys), or None when it has no tokenizer."""
    buf = _read_header_prefix(path)
    kept = [raw for key, raw in _kv_entries(buf) if not key.startswith("split.")]
    if not any(k == "tokenizer.ggml.tokens" for k, _ in _kv_entries(buf)):
        return None
    head = GGUF_MAGIC + struct.pack("<IQQ", 3, 0, len(kept))
    body = head + b"".join(kept)
    return body + b"\0" * (-len(body) % ALIGNMENT)


def vision_sections(mmproj_path, native_paths):
    out = [("vision/mmproj", None, str(mmproj_path), 0, os.path.getsize(mmproj_path))]
    for p in native_paths:
        data = vocab_only_gguf(str(p))
        if data is not None:
            out.append(("vision/text_vocab", data, None, 0, len(data)))
            return out
    raise ValueError("none of the source GGUF files carries the tokenizer metadata the vision sidecar needs")

# ===== file: tools/gguf_reader.py =====
"""Minimal GGUF v3 header reader with no ggml or gguf-py dependency.

gguf-py cannot open these files: its quantization enum has no type 42 (`Q2_0`). This is the reference for
`src/artifact/`. It parses the header only; tensor data is never read.
"""
from __future__ import annotations

import dataclasses
import pathlib
import struct
from typing import Any

GGUF_MAGIC = 0x46554747  # "GGUF" little-endian

# ggml_type values; 42 = Q2_0, the routed-expert encoding, absent from gguf-py
GGML_TYPES: dict[int, str] = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 4: "Q4_2", 5: "Q4_3", 6: "Q5_0", 7: "Q5_1",
    8: "Q8_0", 9: "Q8_1", 10: "Q2_K", 11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K",
    15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S", 20: "IQ4_NL",
    21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16", 26: "I32", 27: "I64",
    28: "F64", 29: "IQ1_M", 30: "BF16", 31: "Q4_0_4_4", 32: "Q4_0_4_8", 33: "Q4_0_8_8",
    34: "TQ1_0", 35: "TQ2_0", 36: "IQ4_NL_4_4", 37: "IQ4_NL_4_8", 38: "IQ4_NL_8_8",
    39: "MXFP4", 40: "NVFP4", 41: "Q4_0_8_8", 42: "Q2_0",
}

# GGUF metadata value type ids
GGUF_META = {
    0: ("u8", 1), 1: ("i8", 1), 2: ("u16", 2), 3: ("i16", 2), 4: ("u32", 4), 5: ("i32", 4),
    6: ("f32", 4), 7: ("bool", 1), 8: ("string", None), 9: ("array", None),
    10: ("u64", 8), 11: ("i64", 8), 12: ("f64", 8),
}

# ggml block geometry per encoding: (block elements, bytes per block); makes tensor byte counts checkable
BLOCK_GEOMETRY: dict[str, tuple[int, int]] = {
    "F32": (1, 4), "F16": (1, 2), "BF16": (1, 2), "F64": (1, 8),
    "Q4_0": (32, 18), "Q4_1": (32, 20), "Q5_0": (32, 22), "Q5_1": (32, 24),
    "Q8_0": (32, 34), "Q8_1": (32, 36),
    "Q2_K": (256, 84), "Q3_K": (256, 110), "Q4_K": (256, 144), "Q5_K": (256, 176),
    "Q6_K": (256, 210), "Q8_K": (256, 292),
    "IQ2_XXS": (256, 66), "IQ2_XS": (256, 74), "IQ3_XXS": (256, 98), "IQ1_S": (256, 50),
    "IQ4_NL": (32, 18), "IQ3_S": (256, 110), "IQ2_S": (256, 82), "IQ4_XS": (256, 136),
    "IQ1_M": (256, 56), "Q2_0": (64, 18), "MXFP4": (32, 17), "NVFP4": (64, 36),
}


@dataclasses.dataclass
class TensorInfo:
    name: str
    shape: list[int]
    type_id: int
    type_name: str
    offset: int

    @property
    def elements(self) -> int:
        n = 1
        for d in self.shape:
            n *= d
        return n

    def expected_bytes(self) -> int | None:
        geom = BLOCK_GEOMETRY.get(self.type_name)
        if geom is None:
            return None
        block_elems, block_bytes = geom
        if self.elements % block_elems:
            return None
        return self.elements // block_elems * block_bytes


class GGUFFile:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.metadata: dict[str, Any] = {}
        self.tensors: list[TensorInfo] = []
        self.version = 0
        self.alignment = 32
        with self.path.open("rb") as fh:
            self._parse(fh)
        self.data_start = self._data_start

    # ------------------------------------------------------------------ internals
    def _parse(self, fh) -> None:
        magic = struct.unpack("<I", fh.read(4))[0]
        if magic != GGUF_MAGIC:
            raise ValueError(f"{self.path.name}: not a GGUF file (magic {magic:#x})")
        self.version, n_tensors, n_kv = struct.unpack("<IQQ", fh.read(20))
        if self.version != 3:
            raise ValueError(f"{self.path.name}: GGUF v{self.version}, this reader handles v3")
        for _ in range(n_kv):
            key = self._str(fh)
            self.metadata[key] = self._value(fh)
        for _ in range(n_tensors):
            name = self._str(fh)
            (n_dims,) = struct.unpack("<I", fh.read(4))
            shape = list(struct.unpack(f"<{n_dims}Q", fh.read(8 * n_dims)))
            type_id, offset = struct.unpack("<IQ", fh.read(12))
            self.tensors.append(TensorInfo(name, shape, type_id,
                                           GGML_TYPES.get(type_id, f"type{type_id}"), offset))
        align = self.metadata.get("general.alignment")
        if isinstance(align, int) and align:
            self.alignment = align
        pos = fh.tell()
        self._data_start = (pos + self.alignment - 1) // self.alignment * self.alignment

    def _str(self, fh) -> str:
        (n,) = struct.unpack("<Q", fh.read(8))
        return fh.read(n).decode("utf-8", "replace")

    def _value(self, fh):
        (t,) = struct.unpack("<I", fh.read(4))
        name, size = GGUF_META[t]
        if name == "string":
            return self._str(fh)
        if name == "array":
            (et,) = struct.unpack("<I", fh.read(4))
            (count,) = struct.unpack("<Q", fh.read(8))
            ename, esize = GGUF_META[et]
            if ename == "string":
                return [self._str(fh) for _ in range(count)]
            fmt = {"u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i",
                   "f32": "f", "bool": "?", "u64": "Q", "i64": "q", "f64": "d"}[ename]
            raw = fh.read(esize * count)
            return list(struct.unpack(f"<{count}{fmt}", raw))
        fmt = {"u8": "B", "i8": "b", "u16": "H", "i16": "h", "u32": "I", "i32": "i",
               "f32": "f", "bool": "?", "u64": "Q", "i64": "q", "f64": "d"}[name]
        return struct.unpack(f"<{fmt}", fh.read(size))[0]

    # ------------------------------------------------------------------ helpers
    def by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tensors:
            out[t.type_name] = out.get(t.type_name, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def find(self, needle: str) -> list[TensorInfo]:
        return [t for t in self.tensors if needle in t.name]

# ===== file: tools/gguf_writer.py =====
"""A minimal GGUF v3 writer including `Q2_0`, which gguf-py cannot write (no type 42).

Enough to build the tiny synthetic model; the only quantizer is Q2_0:
    block = 64 elements, fp16 scale, 16 bytes of 2-bit codes, LSB-first, 4 codes per byte
    d = amax, code = clamp(round(w/d) + 1, 0, 3), symbol = code - 1  ->  {-1, 0, +1, +2}

`python tools/gguf_writer.py` writes a synthetic file, reads it back with `gguf_reader.py` and checks it byte-exactly.
"""
from __future__ import annotations

import dataclasses
import pathlib
import struct
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gguf_reader import GGUF_MAGIC, GGUFFile  # noqa: E402

# writable ggml_type ids, matching GGML_TYPES in gguf_reader.py
TYPE_IDS = {"F32": 0, "F16": 1, "Q5_K": 13, "Q6_K": 14, "Q8_0": 8, "Q2_0": 42, "IQ2_XXS": 16, "IQ1_S": 19, "IQ4_NL": 20, "BF16": 30}
DEFAULT_ALIGNMENT = 32

_META_IDS = {"u8": 0, "i8": 1, "u16": 2, "i16": 3, "u32": 4, "i32": 5, "f32": 6,
             "bool": 7, "string": 8, "array": 9, "u64": 10, "i64": 11, "f64": 12}


# ------------------------------------------------------------------ Q2_0, per the Q2_0 contract
def quantize_q2_0(w: np.ndarray) -> bytes:
    """(n,) float32 -> concatenated 18-byte blocks. n must be a multiple of 64."""
    w = np.asarray(w, dtype=np.float32).reshape(-1)
    if w.size % 64:
        raise ValueError(f"Q2_0 needs a multiple of 64 elements, got {w.size}")
    blocks = w.reshape(-1, 64)
    out = bytearray()
    for b in blocks:
        amax = float(np.max(np.abs(b)))
        d = amax
        # d = amax (not amax/2): the grid is asymmetric, symbols are {-1,0,+1,+2}
        code = np.zeros(64, dtype=np.uint8) if d == 0 else \
            np.clip(np.rint(b / d).astype(np.int32) + 1, 0, 3).astype(np.uint8)
        packed = np.zeros(16, dtype=np.uint8)
        # code j -> byte j//4, bit (j%4)*2
        for j in range(64):
            packed[j // 4] |= np.uint8(code[j] << ((j % 4) * 2))
        out += struct.pack("<e", d)      # fp16 scale
        out += packed.tobytes()
    return bytes(out)


def dequantize_q2_0(raw: bytes) -> np.ndarray:
    """Inverse of the above, for the round-trip check. Mirrors `dequantize_row_q2_0`."""
    nblocks = len(raw) // 18
    out = np.empty(nblocks * 64, dtype=np.float32)
    for i in range(nblocks):
        d = struct.unpack_from("<e", raw, i * 18)[0]
        qs = raw[i * 18 + 2: i * 18 + 18]
        for j in range(64):
            c = (qs[j // 4] >> ((j % 4) * 2)) & 0x03
            out[i * 64 + j] = (c - 1) * d
    return out


# ------------------------------------------------------------------ writer
@dataclasses.dataclass
class _Tensor:
    name: str
    shape: list[int]          # GGUF order: dim 0 varies fastest
    type_name: str
    data: bytes


class GGUFWriter:
    def __init__(self, alignment: int = DEFAULT_ALIGNMENT):
        self.alignment = alignment
        self.metadata: dict[str, tuple[str, object]] = {}
        self.tensors: list[_Tensor] = []

    # ---- metadata
    def add(self, key: str, value, type_name: str | None = None) -> None:
        if type_name is None:
            type_name = {bool: "bool", int: "i64", float: "f64", str: "string"}.get(type(value))
        if type_name is None:
            if isinstance(value, list) and value:
                elem = {bool: "bool", int: "i32", float: "f32", str: "string"}.get(type(value[0]))
                if elem is None:
                    raise TypeError(f"unsupported array element {type(value[0])} for {key}")
                type_name = "array:" + elem
            else:
                raise TypeError(f"cannot infer a GGUF type for {key}")
        self.metadata[key] = (type_name, value)

    # ---- tensors
    def add_f32(self, name: str, arr: np.ndarray) -> None:
        a = np.ascontiguousarray(arr, dtype="<f4")
        self.tensors.append(_Tensor(name, list(a.shape), "F32", a.tobytes()))

    def add_raw(self, name: str, shape: list[int], type_name: str, data: bytes) -> None:
        """A tensor whose blocks are given as they are stored (shape in GGUF order)."""
        self.tensors.append(_Tensor(name, list(shape), type_name, bytes(data)))

    def add_q2_0(self, name: str, arr: np.ndarray, shape: list[int]) -> None:
        """`arr` is (rows, in_features) row-major; `shape` is the full GGUF shape, passed explicitly because a
        fused expert tensor is [in, out, experts] and cannot be inferred. Blocks run along dim 0 only.
        """
        a = np.asarray(arr, dtype=np.float32)
        in_features = shape[0]
        if a.ndim != 2 or a.shape[1] != in_features:
            raise ValueError(f"{name}: expected (rows, {in_features}), got {a.shape}")
        if a.shape[0] * in_features != int(np.prod(shape)):
            raise ValueError(f"{name}: {a.shape} does not cover shape {shape}")
        per_row = quantize_q2_0(a.reshape(-1))          # row-major: row 0 first
        self.tensors.append(_Tensor(name, list(shape), "Q2_0", per_row))

    # ---- serialisation
    def _kv_bytes(self, key: str, type_name: str, value) -> bytes:
        out = bytearray()
        out += struct.pack("<Q", len(key)) + key.encode("utf-8")
        if type_name.startswith("array:"):
            elem = type_name.split(":", 1)[1]
            out += struct.pack("<I", _META_IDS["array"])
            out += struct.pack("<I", _META_IDS[elem])
            out += struct.pack("<Q", len(value))
            for v in value:
                out += self._scalar(elem, v)
            return bytes(out)
        out += struct.pack("<I", _META_IDS[type_name])
        out += self._scalar(type_name, value)
        return bytes(out)

    @staticmethod
    def _scalar(type_name: str, v) -> bytes:
        if type_name == "string":
            b = v.encode("utf-8")
            return struct.pack("<Q", len(b)) + b
        fmt = {"u8": "<B", "i8": "<b", "u16": "<H", "i16": "<h", "u32": "<I", "i32": "<i",
               "f32": "<f", "bool": "<?", "u64": "<Q", "i64": "<q", "f64": "<d"}[type_name]
        return struct.pack(fmt, v)

    def write(self, path) -> pathlib.Path:
        path = pathlib.Path(path)
        body = bytearray()
        body += struct.pack("<I", GGUF_MAGIC)
        body += struct.pack("<I", 3)                       # GGUF v3
        body += struct.pack("<Q", len(self.tensors))
        body += struct.pack("<Q", len(self.metadata))
        for k, (t, v) in self.metadata.items():
            body += self._kv_bytes(k, t, v)

        # tensor directory: offsets are relative to the data section
        offset = 0
        infos = bytearray()
        for t in self.tensors:
            infos += struct.pack("<Q", len(t.name)) + t.name.encode("utf-8")
            infos += struct.pack("<I", len(t.shape))
            infos += struct.pack(f"<{len(t.shape)}Q", *t.shape)
            infos += struct.pack("<I", TYPE_IDS[t.type_name])
            infos += struct.pack("<Q", offset)
            offset += len(t.data)
            offset = (offset + self.alignment - 1) // self.alignment * self.alignment
        body += infos

        pad = (-len(body)) % self.alignment
        body += b"\0" * pad
        for t in self.tensors:
            body += t.data
            pad = (-len(t.data)) % self.alignment
            body += b"\0" * pad

        path.write_bytes(bytes(body))
        return path


# ------------------------------------------------------------------ self-check
def _selfcheck() -> int:
    rng = np.random.default_rng(7)
    w = GGUFWriter()
    w.add("general.architecture", "qwen4exp")
    w.add("qwen4exp.block_count", 8, "u32")
    w.add("qwen4exp.test.array", [1, 2, 3, 4], "array:i32")
    w.add("general.name", "tiny")

    A = rng.standard_normal((16, 64)).astype(np.float32)
    B = rng.standard_normal((4, 8)).astype(np.float32)
    w.add_q2_0("blk.0.ffn_gate_exps.weight", A, [64, 16])
    w.add_f32("blk.0.ssm_a", B)

    p = pathlib.Path(tempfile.mkdtemp(prefix="gguf-selfcheck-")) / "tiny-selftest.gguf"
    w.write(p)
    print(f"wrote {p.name}  ({p.stat().st_size} B)")

    g = GGUFFile(p)
    ok = True
    print(f"  version {g.version}  tensors {len(g.tensors)}  metadata {len(g.metadata)}")
    if g.metadata.get("general.architecture") != "qwen4exp": print("  FAIL arch"); ok = False
    if g.metadata.get("qwen4exp.block_count") != 8:         print("  FAIL u32"); ok = False
    if g.metadata.get("qwen4exp.test.array") != [1, 2, 3, 4]: print("  FAIL array"); ok = False

    by = {t.name: t for t in g.tensors}
    t = by.get("blk.0.ffn_gate_exps.weight")
    if t is None or t.type_name != "Q2_0" or t.shape != [64, 16]:
        print(f"  FAIL tensor dir: {t}"); ok = False
    else:
        with p.open("rb") as fh:
            fh.seek(g.data_start + t.offset)
            raw = fh.read(16 * 18)
        back = dequantize_q2_0(raw)
        err = np.abs(back - A.reshape(-1)).max()
        rel = float(np.abs(back - A.reshape(-1)).mean() / np.abs(A).mean())
        print(f"  Q2_0 round trip: max abs {err:.4f}  mean rel {rel*100:.2f}%  "
              f"(quantization error, not a write bug)")
        # re-quantizing the dequantized values must be a fixed point
        again = dequantize_q2_0(quantize_q2_0(back))
        if not np.array_equal(again, back):
            print("  FAIL Q2_0 is not a fixed point under requantization"); ok = False
        else:
            print("  Q2_0 fixed point: PASS (requantizing the dequantized values is exact)")
    print("SELF-CHECK " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selfcheck())

# ===== file: tools/ggufq.py =====
# SPDX-License-Identifier: MIT
# Copyright (c) 2023-2026 The ggml authors
# Copyright (c) 2023 Georgi Gerganov
#
# Dequantizers for the GGML block formats the converter and the fixtures read, plus the BF16 quantizer.
# Embedded from llama.cpp's gguf-py (gguf/quants.py and the block-size table of gguf/constants.py) at commit
# 3cf03257f219afbe7334045ff7c6a06ac68c627d, MIT; see THIRD_PARTY_LICENSES. The formats not used here (TQ*, MXFP4,
# NVFP4), the quantizers other than BF16 and the lazy-tensor machinery are left out; the code kept is unchanged.
from __future__ import annotations

from enum import IntEnum
from math import ceil, log2
from typing import Any, Callable, Sequence

import numpy as np
from numpy.typing import DTypeLike

QK_K = 256


class GGMLQuantizationType(IntEnum):
    F32 = 0
    F16 = 1
    Q4_0 = 2
    Q4_1 = 3
    Q5_0 = 6
    Q5_1 = 7
    Q8_0 = 8
    Q2_K = 10
    Q3_K = 11
    Q4_K = 12
    Q5_K = 13
    Q6_K = 14
    IQ2_XXS = 16
    IQ2_XS = 17
    IQ3_XXS = 18
    IQ1_S = 19
    IQ4_NL = 20
    IQ3_S = 21
    IQ2_S = 22
    IQ4_XS = 23
    IQ1_M = 29
    BF16 = 30


# (values per block, bytes per block)
GGML_QUANT_SIZES: dict[GGMLQuantizationType, tuple[int, int]] = {
    GGMLQuantizationType.F32:     (1, 4),
    GGMLQuantizationType.F16:     (1, 2),
    GGMLQuantizationType.Q4_0:    (32, 2 + 16),
    GGMLQuantizationType.Q4_1:    (32, 2 + 2 + 16),
    GGMLQuantizationType.Q5_0:    (32, 2 + 4 + 16),
    GGMLQuantizationType.Q5_1:    (32, 2 + 2 + 4 + 16),
    GGMLQuantizationType.Q8_0:    (32, 2 + 32),
    GGMLQuantizationType.Q2_K:    (256, 2 + 2 + QK_K // 16 + QK_K // 4),
    GGMLQuantizationType.Q3_K:    (256, 2 + QK_K // 4 + QK_K // 8 + 12),
    GGMLQuantizationType.Q4_K:    (256, 2 + 2 + QK_K // 2 + 12),
    GGMLQuantizationType.Q5_K:    (256, 2 + 2 + QK_K // 2 + QK_K // 8 + 12),
    GGMLQuantizationType.Q6_K:    (256, 2 + QK_K // 2 + QK_K // 4 + QK_K // 16),
    GGMLQuantizationType.IQ2_XXS: (256, 2 + QK_K // 4),
    GGMLQuantizationType.IQ2_XS:  (256, 2 + QK_K // 4 + QK_K // 32),
    GGMLQuantizationType.IQ3_XXS: (256, 2 + QK_K // 4 + QK_K // 8),
    GGMLQuantizationType.IQ1_S:   (256, 2 + QK_K // 8 + QK_K // 16),
    GGMLQuantizationType.IQ4_NL:  (32, 2 + 16),
    GGMLQuantizationType.IQ3_S:   (256, 2 + QK_K // 4 + QK_K // 8 + QK_K // 32 + 4),
    GGMLQuantizationType.IQ2_S:   (256, 2 + QK_K // 4 + QK_K // 16),
    GGMLQuantizationType.IQ4_XS:  (256, 2 + 2 + QK_K // 2 + QK_K // 64),
    GGMLQuantizationType.IQ1_M:   (256, QK_K // 8 + QK_K // 16 + QK_K // 32),
    GGMLQuantizationType.BF16:    (1, 2),
}


def quant_shape_to_byte_shape(shape: Sequence[int], quant_type: GGMLQuantizationType) -> tuple[int, ...]:
    block_size, type_size = GGML_QUANT_SIZES[quant_type]
    if shape[-1] % block_size != 0:
        raise ValueError(f"Quantized tensor row size ({shape[-1]}) is not a multiple of {quant_type.name} block size ({block_size})")
    return (*shape[:-1], shape[-1] // block_size * type_size)


def quant_shape_from_byte_shape(shape: Sequence[int], quant_type: GGMLQuantizationType) -> tuple[int, ...]:
    block_size, type_size = GGML_QUANT_SIZES[quant_type]
    if shape[-1] % type_size != 0:
        raise ValueError(f"Quantized tensor bytes per row ({shape[-1]}) is not a multiple of {quant_type.name} type size ({type_size})")
    return (*shape[:-1], shape[-1] // type_size * block_size)


# This is faster than np.vectorize and np.apply_along_axis because it works on more than one row at a time
def _apply_over_grouped_rows(func: Callable[[np.ndarray], np.ndarray], arr: np.ndarray, otype: DTypeLike, oshape: tuple[int, ...]) -> np.ndarray:
    rows = arr.reshape((-1, arr.shape[-1]))
    assert len(rows.shape)
    osize = 1
    for dim in oshape:
        osize *= dim
    out = np.empty(shape=osize, dtype=otype)
    # compute over groups of 16 rows (arbitrary, but seems good for performance)
    n_groups = (rows.shape[0] // 16) or 1
    np.concatenate([func(group).ravel() for group in np.array_split(rows, n_groups)], axis=0, out=out)
    return out.reshape(oshape)


class QuantError(Exception): ...


_type_traits: dict[GGMLQuantizationType, type[_Quant]] = {}


def quantize(data: np.ndarray, qtype: GGMLQuantizationType) -> np.ndarray:
    """Only BF16 (and the plain float types) can be quantized."""
    if qtype == GGMLQuantizationType.F32:
        return data.astype(np.float32, copy=False)
    elif qtype == GGMLQuantizationType.F16:
        return data.astype(np.float16, copy=False)
    elif qtype == GGMLQuantizationType.BF16:
        return BF16.quantize(data)
    raise NotImplementedError(f"Quantization for {qtype.name} is not implemented")


def dequantize(data: np.ndarray, qtype: GGMLQuantizationType) -> np.ndarray:
    if qtype == GGMLQuantizationType.F32:
        return data.view(np.float32)
    elif qtype == GGMLQuantizationType.F16:
        return data.view(np.float16).astype(np.float32)
    elif (q := _type_traits.get(qtype)) is not None:
        return q.dequantize(data)
    raise NotImplementedError(f"Dequantization for {qtype.name} is not implemented")


class _Quant:
    qtype: GGMLQuantizationType
    block_size: int
    type_size: int

    grid: np.ndarray[Any, np.dtype[np.float32]] | None = None
    grid_shape: tuple[int, int] = (0, 0)
    grid_map: tuple[int | float, ...] = ()
    grid_hex: bytes | None = None

    def __init_subclass__(cls, qtype: GGMLQuantizationType) -> None:
        cls.qtype = qtype
        cls.block_size, cls.type_size = GGML_QUANT_SIZES[qtype]
        assert qtype not in _type_traits
        _type_traits[qtype] = cls

    @classmethod
    def init_grid(cls):
        if cls.grid is not None or cls.grid_hex is None:
            return

        bits_per_elem = ceil(log2(len(cls.grid_map)))
        assert bits_per_elem != 0, cls.qtype.name
        elems_per_byte = 8 // bits_per_elem

        grid = np.frombuffer(cls.grid_hex, dtype=np.uint8)
        # decode hexadecimal chars from grid
        grid = grid.reshape((-1, 2))
        grid = (np.where(grid > 0x40, grid + 9, grid) & 0x0F) << np.array([4, 0], dtype=np.uint8).reshape((1, 2))
        grid = grid[..., 0] | grid[..., 1]
        # unpack the grid values
        grid = grid.reshape((-1, 1)) >> np.array([i for i in range(0, 8, 8 // elems_per_byte)], dtype=np.uint8).reshape((1, elems_per_byte))
        grid = (grid & ((1 << bits_per_elem) - 1)).reshape((-1, 1))
        grid_map = np.array(cls.grid_map, dtype=np.float32).reshape((1, -1))
        grid = np.take_along_axis(grid_map, grid, axis=-1)
        cls.grid = grid.reshape((1, 1, *cls.grid_shape))

    @classmethod
    def dequantize_rows(cls, rows: np.ndarray) -> np.ndarray:
        rows = rows.view(np.uint8)
        shape = rows.shape
        n_blocks = rows.size // cls.type_size
        blocks = rows.reshape((n_blocks, cls.type_size))
        blocks = cls.dequantize_blocks(blocks)
        assert blocks.dtype == np.float32
        assert blocks.shape[-1] == cls.block_size
        return blocks.reshape(quant_shape_from_byte_shape(shape, cls.qtype))

    @classmethod
    def quantize_rows(cls, rows: np.ndarray) -> np.ndarray:
        rows = rows.astype(np.float32, copy=False)
        shape = rows.shape
        n_blocks = rows.size // cls.block_size
        blocks = rows.reshape((n_blocks, cls.block_size))
        blocks = cls.quantize_blocks(blocks)
        assert blocks.dtype == np.uint8
        assert blocks.shape[-1] == cls.type_size
        return blocks.reshape(quant_shape_to_byte_shape(shape, cls.qtype))

    @classmethod
    def quantize(cls, tensor: np.ndarray) -> np.ndarray:
        if tensor.shape[-1] % cls.block_size != 0:
            raise QuantError(f"Can't quantize tensor with shape {tensor.shape} to {cls.qtype.name}")
        return _apply_over_grouped_rows(cls.quantize_rows, arr=tensor, otype=np.uint8,
                                        oshape=quant_shape_to_byte_shape(tensor.shape, cls.qtype))

    @classmethod
    def dequantize(cls, tensor: np.ndarray) -> np.ndarray:
        cls.init_grid()
        return _apply_over_grouped_rows(cls.dequantize_rows, arr=tensor, otype=np.float32,
                                        oshape=quant_shape_from_byte_shape(tensor.shape, cls.qtype))


class BF16(_Quant, qtype=GGMLQuantizationType.BF16):
    @classmethod
    # same as ggml_compute_fp32_to_bf16 in ggml-impl.h
    def quantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n = blocks.view(np.uint32)
        # force nan to quiet
        n = np.where((n & 0x7fffffff) > 0x7f800000, (n & np.uint32(0xffff0000)) | np.uint32(64 << 16), n)
        # round to nearest even
        n = (np.uint64(n) + (0x7fff + ((n >> 16) & 1))) >> 16
        return n.astype(np.uint16).view(np.uint8)

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        return (blocks.view(np.int16).astype(np.int32) << 16).view(np.float32)


class Q4_0(_Quant, qtype=GGMLQuantizationType.Q4_0):

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, qs = np.hsplit(blocks, [2])

        d = d.view(np.float16).astype(np.float32)

        qs = qs.reshape((n_blocks, -1, 1, cls.block_size // 2)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))
        qs = (qs & np.uint8(0x0F)).reshape((n_blocks, -1)).astype(np.int8) - np.int8(8)

        return (d * qs.astype(np.float32))


class Q4_1(_Quant, qtype=GGMLQuantizationType.Q4_1):

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        m, qs = np.hsplit(rest, [2])

        d = d.view(np.float16).astype(np.float32)
        m = m.view(np.float16).astype(np.float32)

        qs = qs.reshape((n_blocks, -1, 1, cls.block_size // 2)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))
        qs = (qs & np.uint8(0x0F)).reshape((n_blocks, -1)).astype(np.float32)

        return (d * qs) + m


class Q5_0(_Quant, qtype=GGMLQuantizationType.Q5_0):

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        qh, qs = np.hsplit(rest, [4])

        d = d.view(np.float16).astype(np.float32)
        qh = qh.view(np.uint32)

        qh = qh.reshape((n_blocks, 1)) >> np.array([i for i in range(32)], dtype=np.uint32).reshape((1, 32))
        ql = qs.reshape((n_blocks, -1, 1, cls.block_size // 2)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))
        qh = (qh & np.uint32(0x01)).astype(np.uint8)
        ql = (ql & np.uint8(0x0F)).reshape((n_blocks, -1))

        qs = (ql | (qh << np.uint8(4))).astype(np.int8) - np.int8(16)

        return (d * qs.astype(np.float32))


class Q5_1(_Quant, qtype=GGMLQuantizationType.Q5_1):

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        m, rest = np.hsplit(rest, [2])
        qh, qs = np.hsplit(rest, [4])

        d = d.view(np.float16).astype(np.float32)
        m = m.view(np.float16).astype(np.float32)
        qh = qh.view(np.uint32)

        qh = qh.reshape((n_blocks, 1)) >> np.array([i for i in range(32)], dtype=np.uint32).reshape((1, 32))
        ql = qs.reshape((n_blocks, -1, 1, cls.block_size // 2)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))
        qh = (qh & np.uint32(0x01)).astype(np.uint8)
        ql = (ql & np.uint8(0x0F)).reshape((n_blocks, -1))

        qs = (ql | (qh << np.uint8(4))).astype(np.float32)

        return (d * qs) + m


class Q8_0(_Quant, qtype=GGMLQuantizationType.Q8_0):

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        d, x = np.split(blocks, [2], axis=1)
        d = d.view(np.float16).astype(np.float32)
        x = x.view(np.int8).astype(np.float32)

        return (x * d)


class Q2_K(_Quant, qtype=GGMLQuantizationType.Q2_K):
    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        scales, rest = np.hsplit(blocks, [QK_K // 16])
        qs, rest = np.hsplit(rest, [QK_K // 4])
        d, dmin = np.hsplit(rest, [2])

        d = d.view(np.float16).astype(np.float32)
        dmin = dmin.view(np.float16).astype(np.float32)

        # (n_blocks, 16, 1)
        dl = (d * (scales & 0xF).astype(np.float32)).reshape((n_blocks, QK_K // 16, 1))
        ml = (dmin * (scales >> 4).astype(np.float32)).reshape((n_blocks, QK_K // 16, 1))

        shift = np.array([0, 2, 4, 6], dtype=np.uint8).reshape((1, 1, 4, 1))

        qs = (qs.reshape((n_blocks, -1, 1, 32)) >> shift) & np.uint8(3)

        qs = qs.reshape((n_blocks, QK_K // 16, 16)).astype(np.float32)

        qs = dl * qs - ml

        return qs.reshape((n_blocks, -1))


class Q3_K(_Quant, qtype=GGMLQuantizationType.Q3_K):
    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        hmask, rest = np.hsplit(blocks, [QK_K // 8])
        qs, rest = np.hsplit(rest, [QK_K // 4])
        scales, d = np.hsplit(rest, [12])

        d = d.view(np.float16).astype(np.float32)

        # The scales are packed at 6-bit each in this pattern:
        #  0: IIIIAAAA
        #  1: JJJJBBBB
        #  2: KKKKCCCC
        #  3: LLLLDDDD
        #  4: MMMMEEEE
        #  5: NNNNFFFF
        #  6: OOOOGGGG
        #  7: PPPPHHHH
        #  8: MMIIEEAA
        #  9: NNJJFFBB
        # 10: OOKKGGCC
        # 11: PPLLHHDD
        lscales, hscales = np.hsplit(scales, [8])
        lscales = lscales.reshape((n_blocks, 1, 8)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 2, 1))
        lscales = lscales.reshape((n_blocks, 16))
        hscales = hscales.reshape((n_blocks, 1, 4)) >> np.array([0, 2, 4, 6], dtype=np.uint8).reshape((1, 4, 1))
        hscales = hscales.reshape((n_blocks, 16))
        scales = (lscales & np.uint8(0x0F)) | ((hscales & np.uint8(0x03)) << np.uint8(4))
        scales = (scales.astype(np.int8) - np.int8(32)).astype(np.float32)

        dl = (d * scales).reshape((n_blocks, 16, 1))

        ql = qs.reshape((n_blocks, -1, 1, 32)) >> np.array([0, 2, 4, 6], dtype=np.uint8).reshape((1, 1, 4, 1))
        qh = hmask.reshape(n_blocks, -1, 1, 32) >> np.array([i for i in range(8)], dtype=np.uint8).reshape((1, 1, 8, 1))
        ql = ql.reshape((n_blocks, 16, QK_K // 16)) & np.uint8(3)
        qh = (qh.reshape((n_blocks, 16, QK_K // 16)) & np.uint8(1))
        qh = qh ^ np.uint8(1)  # strangely, the offset is zero when the bitmask is 1
        q = (ql.astype(np.int8) - (qh << np.uint8(2)).astype(np.int8)).astype(np.float32)

        return (dl * q).reshape((n_blocks, QK_K))


class Q4_K(_Quant, qtype=GGMLQuantizationType.Q4_K):
    K_SCALE_SIZE = 12

    @staticmethod
    def get_scale_min(scales: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        n_blocks = scales.shape[0]
        scales = scales.view(np.uint8)
        ### Unpacking the following: ###
        #  0 EEAAAAAA
        #  1 FFBBBBBB
        #  2 GGCCCCCC
        #  3 HHDDDDDD
        #  4 eeaaaaaa
        #  5 ffbbbbbb
        #  6 ggcccccc
        #  7 hhdddddd
        #  8 eeeeEEEE
        #  9 ffffFFFF
        # 10 ggggGGGG
        # 11 hhhhHHHH
        scales = scales.reshape((n_blocks, 3, 4))
        d, m, m_d = np.split(scales, 3, axis=-2)

        sc = np.concatenate([d & 0x3F, (m_d & 0x0F) | ((d >> 2) & 0x30)], axis=-1)
        min = np.concatenate([m & 0x3F, (m_d >> 4) | ((m >> 2) & 0x30)], axis=-1)

        return (sc.reshape((n_blocks, 8)), min.reshape((n_blocks, 8)))

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        dmin, rest = np.hsplit(rest, [2])
        scales, qs = np.hsplit(rest, [cls.K_SCALE_SIZE])

        d = d.view(np.float16).astype(np.float32)
        dmin = dmin.view(np.float16).astype(np.float32)

        sc, m = Q4_K.get_scale_min(scales)

        d = (d * sc.astype(np.float32)).reshape((n_blocks, -1, 1))
        dm = (dmin * m.astype(np.float32)).reshape((n_blocks, -1, 1))

        qs = qs.reshape((n_blocks, -1, 1, 32)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))
        qs = (qs & np.uint8(0x0F)).reshape((n_blocks, -1, 32)).astype(np.float32)

        return (d * qs - dm).reshape((n_blocks, QK_K))


class Q5_K(_Quant, qtype=GGMLQuantizationType.Q5_K):
    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        dmin, rest = np.hsplit(rest, [2])
        scales, rest = np.hsplit(rest, [Q4_K.K_SCALE_SIZE])
        qh, qs = np.hsplit(rest, [QK_K // 8])

        d = d.view(np.float16).astype(np.float32)
        dmin = dmin.view(np.float16).astype(np.float32)

        sc, m = Q4_K.get_scale_min(scales)

        d = (d * sc.astype(np.float32)).reshape((n_blocks, -1, 1))
        dm = (dmin * m.astype(np.float32)).reshape((n_blocks, -1, 1))

        ql = qs.reshape((n_blocks, -1, 1, 32)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))
        qh = qh.reshape((n_blocks, -1, 1, 32)) >> np.array([i for i in range(8)], dtype=np.uint8).reshape((1, 1, 8, 1))
        ql = (ql & np.uint8(0x0F)).reshape((n_blocks, -1, 32))
        qh = (qh & np.uint8(0x01)).reshape((n_blocks, -1, 32))
        q = (ql | (qh << np.uint8(4))).astype(np.float32)

        return (d * q - dm).reshape((n_blocks, QK_K))


class Q6_K(_Quant, qtype=GGMLQuantizationType.Q6_K):
    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        ql, rest = np.hsplit(blocks, [QK_K // 2])
        qh, rest = np.hsplit(rest, [QK_K // 4])
        scales, d = np.hsplit(rest, [QK_K // 16])

        scales = scales.view(np.int8).astype(np.float32)
        d = d.view(np.float16).astype(np.float32)
        d = (d * scales).reshape((n_blocks, QK_K // 16, 1))

        ql = ql.reshape((n_blocks, -1, 1, 64)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))
        ql = (ql & np.uint8(0x0F)).reshape((n_blocks, -1, 32))
        qh = qh.reshape((n_blocks, -1, 1, 32)) >> np.array([0, 2, 4, 6], dtype=np.uint8).reshape((1, 1, 4, 1))
        qh = (qh & np.uint8(0x03)).reshape((n_blocks, -1, 32))
        q = (ql | (qh << np.uint8(4))).astype(np.int8) - np.int8(32)
        q = q.reshape((n_blocks, QK_K // 16, -1)).astype(np.float32)

        return (d * q).reshape((n_blocks, QK_K))


class IQ2_XXS(_Quant, qtype=GGMLQuantizationType.IQ2_XXS):
    ksigns: bytes = (
        b"\x00\x81\x82\x03\x84\x05\x06\x87\x88\x09\x0a\x8b\x0c\x8d\x8e\x0f"
        b"\x90\x11\x12\x93\x14\x95\x96\x17\x18\x99\x9a\x1b\x9c\x1d\x1e\x9f"
        b"\xa0\x21\x22\xa3\x24\xa5\xa6\x27\x28\xa9\xaa\x2b\xac\x2d\x2e\xaf"
        b"\x30\xb1\xb2\x33\xb4\x35\x36\xb7\xb8\x39\x3a\xbb\x3c\xbd\xbe\x3f"
        b"\xc0\x41\x42\xc3\x44\xc5\xc6\x47\x48\xc9\xca\x4b\xcc\x4d\x4e\xcf"
        b"\x50\xd1\xd2\x53\xd4\x55\x56\xd7\xd8\x59\x5a\xdb\x5c\xdd\xde\x5f"
        b"\x60\xe1\xe2\x63\xe4\x65\x66\xe7\xe8\x69\x6a\xeb\x6c\xed\xee\x6f"
        b"\xf0\x71\x72\xf3\x74\xf5\xf6\x77\x78\xf9\xfa\x7b\xfc\x7d\x7e\xff"
    )

    # iq2xxs_grid, but with each byte of the original packed in 2 bits,
    # by mapping 0x08 to 0, 0x19 to 1, and 0x2b to 2.
    grid_shape = (256, 8)
    grid_map = (0x08, 0x19, 0x2b)
    grid_hex = (
        b"00000200050008000a00110014002000220028002a0041004400500058006100"
        b"6400800082008a00a20001010401100115014001840198010002020222028202"
        b"010404041004210424044004420448046004810484049004a404000502050805"
        b"200546056905800591050906100640068406a406000805080808140828084108"
        b"440850085208880804094009020a140a01100410101021104010601084109010"
        b"951000110811201150115a118011241245120014081420142514491480141815"
        b"6215001616160118041810184018811800190519a019511a002002200a204420"
        b"6120802082202921482100220222012404241024402456240025412564259026"
        b"082820289428442a014004401040184021402440404048405640604081408440"
        b"9040004120416141804185410142104248425642684200440844204480449944"
        b"124524450046014804481048404845480049584961498249454a904a00500850"
        b"1150195020508050885004514251a4519152905492540a550156545600581158"
        b"195864584059085a046010604060686000615561186260620064056410651265"
        b"84654268008002800a8041808280048118814081118201840484108415844084"
        b"608400854685948509864086608602880489118a0490109024904090a1901691"
        b"8091459200942294449451958198209902a050a085a009a100a218a450a804a9"
    )

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, qs = np.hsplit(blocks, [2])

        d = d.view(np.float16).astype(np.float32)

        qs = qs.view(np.uint32).reshape(n_blocks, -1, 2)

        db = d * (np.float32(0.5) + (qs[..., 1] >> 28).astype(np.float32)) * np.float32(0.25)
        db = db.reshape((n_blocks, -1, 1, 1))

        # get the sign indices and unpack the bits
        signs = qs[..., 1].reshape((n_blocks, -1, 1)) >> np.array([0, 7, 14, 21], dtype=np.uint32).reshape((1, 1, 4))
        ksigns = np.frombuffer(cls.ksigns, dtype=np.uint8).reshape((1, 1, 1, 128))
        signs = (signs & np.uint32(0x7F)).reshape((n_blocks, -1, 4, 1))
        signs = np.take_along_axis(ksigns, signs, axis=-1)
        signs = signs.reshape((n_blocks, -1, 4, 1)) >> np.array([i for i in range(8)], dtype=np.uint8).reshape((1, 1, 1, 8))
        signs = signs & np.uint8(0x01)
        signs = np.where(signs == 0, np.float32(1), np.float32(-1))
        signs = signs.reshape((n_blocks, -1, 4, 8))

        assert cls.grid is not None
        grid = np.take_along_axis(cls.grid, qs[..., 0].copy().view(np.uint8).reshape((n_blocks, -1, 1, 1)), axis=-2)
        grid = grid.reshape((n_blocks, -1, 4, 8))

        return (db * grid * signs).reshape((n_blocks, -1))


class IQ2_XS(_Quant, qtype=GGMLQuantizationType.IQ2_XS):
    # iq2xs_grid, but with each byte of the original packed in 2 bits,
    # by mapping 0x08 to 0, 0x19 to 1, and 0x2b to 2.
    grid_shape = (512, 8)
    grid_map = (0x08, 0x19, 0x2b)
    grid_hex = (
        b"00000200050008000a0011001400160019002000220025002800410044004600"
        b"49005000520055005800610064008000820085008800910094009900a0000101"
        b"04010601090110011201150118011a0121012401400142014501480151015401"
        b"6001680181018401900100020202050208021102140220024102440250025502"
        b"80028a0201040404060409041004120415041804210424044004420445044804"
        b"5104540456046004810484049004000502050505080511051405200541054405"
        b"500561058005010604061006260640064206840600080208050808080a081108"
        b"14082008250841084408500858088008a008aa08010904091009400981098909"
        b"000a200a280a960aa00a01100410061009101010121015101810211024104010"
        b"4210451048105110541060106a10811084109010001102110511081111111411"
        b"2011411144115011801194119611011204120612101240126012001402140514"
        b"0814111414142014411444144914501464148014011504151015401500161416"
        b"49160118041810181218401854188618001905196619511aa91a002002200520"
        b"08200a201120142020204120442050208020a020012104211021402148216521"
        b"002222228022a82201240424102429244024002541255225992501261a26a626"
        b"002808280a28202855288828a22868299029082a202a822a882a8a2a01400440"
        b"0640094010401240154018402140244040404240454048404a40514054406040"
        b"6540814084409040004102410541084111411441204141414441504180418541"
        b"a241014204421042124229424042004402440544084411441444194420444144"
        b"4444504480449444014504451045244540459a4500460a464446504601480448"
        b"1048404845485448624800491149444950496949044a00500250055008501150"
        b"145020502850415044505050805001510451105115514051425100524452aa52"
        b"0154045410542154405460548154a154005508558055885521566856a1560058"
        b"14584158505899581a5940594259855a0160046010604060546062608660a960"
        b"006124624a62926200641664106540654565a46501686a682569066a546a626a"
        b"00800280058008801180148020802a8041804480508080808280a880aa800181"
        b"0481068110814081518159810082208280828282a082a8820184048410841284"
        b"158440846084898400854485a58518866a860088088825885a8880888288a888"
        b"0689228a808a888a968aa88a0190049010904090569084900091229164915692"
        b"89920094059444945094589429959095929541965198a6984999159a609a00a0"
        b"02a008a00aa020a02aa0a0a051a159a1a6a100a202a208a22aa280a2a0a240a4"
        b"95a465a698a60aa820a822a828a8a0a8a8a804a984a986a928aa2aaa91aaaaaa"
    )

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        qs, scales = np.hsplit(rest, [2 * QK_K // 8])

        d = d.view(np.float16).astype(np.float32)
        qs = qs.view(np.uint16)

        scales = scales.reshape((n_blocks, -1, 1)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2))
        scales = (scales & 0x0F).reshape((n_blocks, -1))
        db = d * (np.float32(0.5) + scales) * np.float32(0.25)
        db = db.reshape((n_blocks, -1, 1, 1))

        # get the sign indices and unpack the bits
        signs = np.frombuffer(IQ2_XXS.ksigns, dtype=np.uint8).reshape(1, 1, 128)
        signs = np.take_along_axis(signs, (qs >> 9).reshape((n_blocks, -1, 1)), axis=-1)
        signs = signs.reshape((n_blocks, -1, 1)) >> np.array([i for i in range(8)], dtype=np.uint8).reshape((1, 1, 8))
        signs = signs & np.uint8(0x01)
        signs = np.where(signs == 0, np.float32(1), np.float32(-1))
        signs = signs.reshape((n_blocks, -1, 2, 8))

        assert cls.grid is not None
        grid = np.take_along_axis(cls.grid, (qs & np.uint16(511)).reshape((n_blocks, -1, 1, 1)), axis=-2)
        grid = grid.reshape((n_blocks, -1, 2, 8))

        return (db * grid * signs).reshape((n_blocks, -1))


class IQ2_S(_Quant, qtype=GGMLQuantizationType.IQ2_S):
    # iq2s_grid, but with each byte of the original packed in 2 bits,
    # by mapping 0x08 to 0, 0x19 to 1, and 0x2b to 2.
    grid_shape = (1024, 8)
    grid_map = (0x08, 0x19, 0x2b)
    grid_hex = (
        b"00000200050008000a0011001400160019002000220025002800410044004600"
        b"490050005200550058006100640066006900800082008500880091009400a000"
        b"a500aa0001010401060109011001120115011801210124014001420145014801"
        b"510154015601590160016501680181018401900192019501a101a40100020202"
        b"050208021102140220022a02410244024602490250025502800285028a029402"
        b"a202010404040604090410041204150418042104240426042904400442044504"
        b"48044a0451045404560459046004620465048104840486048904900495049804"
        b"a104a40400050205050508050a05110514051605190520052505280541054405"
        b"46054905500552055505580561056405800582058505880591059405a0050106"
        b"0406060609061006150640064506480651065406600681068406900600080208"
        b"050808081108140816081908200825082a084108440846084908500852085508"
        b"580861086408800885089408aa08010904091009120915091809210940094509"
        b"480951095409600981099009000a110a140a220a280a2a0a500a990a01100410"
        b"0610091010101210151018102110241026104010421045104810511054105610"
        b"59106010621065106810811084108610901095109810a110a410001102110511"
        b"08110a1111111411161119112011221125112811411144114611491150115211"
        b"5511581161116411801182118511881191119411011204120912101215122112"
        b"2412401245125112541281128412901200140214051408141114141416141914"
        b"2014251428144114441446144914501452145514581461146414801482148514"
        b"881491149414a014011504150615091510151215151518152115241540154215"
        b"4515481551155415601581158415901500160516081611161416201641164416"
        b"50168016aa160118041806180918101815181818211840184218451848185118"
        b"541860188118841800190219051908191119141920194119441950196919a219"
        b"041a101a401a561a00200220052008201120142016201920202025202a204120"
        b"4420502052205520642080208a209420aa200121042110211221152121214021"
        b"4221452151215421602181218421902100220a22222228222a22442250228822"
        b"8a22a82201240424062409241024152418242124242440244224452448245124"
        b"5424602481248424902400250525082511251425202541254425502566258025"
        b"0126042610264026592600280528112814284128442850288a28aa2801290429"
        b"102995290a2a222a642a882a8a2a014004400640094010401240154018401a40"
        b"21402440264040404240454048404a4051405440564059406040624065408140"
        b"8440904095409840a140a4400041024105410841114114411641194120412241"
        b"2541414144414641494150415241554158416141644180418241854188419141"
        b"9441a04101420442104212421542184224424042454248425142544260428142"
        b"844200440244054408440a441144144416441944204422442544284441444444"
        b"46444944504452445544584461446444804482448544884491449444a0440145"
        b"0445064509451045124515451845214524454045424545454845514554456045"
        b"6a4581458445904500460246054608461146144620464146444650468046a546"
        b"0148044809481048124815481848214824484048424845484848514854486048"
        b"84489048004902490549084911491449204941494449504980499649014a044a"
        b"104a404a00500250055008501150145016501950205022502550285041504450"
        b"4650495050505250555058506150645080508250855088509150945001510451"
        b"0651095110511251155118512151245140514251455148515151545160518151"
        b"8451905100520552085211521452205241524452505269528052015404540654"
        b"0954105412541554185421542454405442544554485451545454605481548454"
        b"9054005502550555085511551455205541554455505580550156045610562656"
        b"405600580258055808581158145820584158445850585a588058015904591059"
        b"4059005a195a855aa85a01600460066010601260156018602160246040604560"
        b"4860516054606060846090600061026105610861116114612061416144615061"
        b"806199610462106240625662a162006405640864116414642064416444645064"
        b"806401650465106540654a656865926500669466016804681068656898680069"
        b"2a69426aa16a0080028005800880118014801980208025804180448050805280"
        b"5580588061808080858091809480018104810981108112811581188121812481"
        b"408142814581488151815481818184819081a981008205820a82118214824182"
        b"4482508201840484068409841084128415841884218440844284458448845184"
        b"5484608481848484908400850285058508851185148520854185448550858085"
        b"8a85018604861086298640860088058811881488418844885088a28801890489"
        b"40896589228a588a5a8a828aa28a019004900990109012901590189024904090"
        b"4290459048905190549060908190849090900091059111911491419144915091"
        b"5a910192049210924092a6920094029405940894119414942094419444945094"
        b"8094969401950495109540959895a19500964696649601980498109826984098"
        b"a998009949995299909a00a005a00aa014a022a02aa041a044a050a0a2a0aaa0"
        b"40a165a102a20aa222a228a22aa282a288a28aa2a8a201a404a410a440a489a4"
        b"a4a400a519a551a60aa828a8a2a854a986a908aa0aaa20aa22aa28aa88aaaaaa"
    )

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        qs, rest = np.hsplit(rest, [QK_K // 8])
        signs, rest = np.hsplit(rest, [QK_K // 8])
        qh, scales = np.hsplit(rest, [QK_K // 32])

        d = d.view(np.float16).astype(np.float32)

        scales = scales.reshape((n_blocks, -1, 1)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2))
        scales = (scales & 0x0F).reshape((n_blocks, -1))
        db = d * (np.float32(0.5) + scales) * np.float32(0.25)
        db = db.reshape((n_blocks, -1, 1, 1))

        # unpack the sign bits
        signs = signs.reshape((n_blocks, -1, 1)) >> np.array([i for i in range(8)], dtype=np.uint8).reshape((1, 1, 8))
        signs = signs & np.uint8(0x01)
        signs = np.where(signs == 0, np.float32(1), np.float32(-1))
        signs = signs.reshape((n_blocks, -1, 2, 8))

        qh = qh.reshape((n_blocks, -1, 1)) >> np.array([0, 2, 4, 6], dtype=np.uint8).reshape((1, 1, 4))
        qs = qs.astype(np.uint16) | ((qh & 0x03).astype(np.uint16) << 8).reshape((n_blocks, -1))

        assert cls.grid is not None
        grid = np.take_along_axis(cls.grid, qs.reshape((n_blocks, -1, 1, 1)), axis=-2)
        grid = grid.reshape((n_blocks, -1, 2, 8))

        return (db * grid * signs).reshape((n_blocks, -1))


class IQ3_XXS(_Quant, qtype=GGMLQuantizationType.IQ3_XXS):
    grid_shape = (256, 4)
    grid_map = (0x04, 0x0c, 0x14, 0x1c, 0x24, 0x2c, 0x34, 0x3e)
    grid_hex = (
        b"0000020004001100130017002000220031004200730075000101030110011201"
        b"2101250130013201410154017001000202020402110220022202310233023702"
        b"5102570275020103070310031203250370031304370444045704730475040105"
        b"0705320552053506640610071407160743076107011003101010121021102310"
        b"3010321034104710501000110211111120112211011203121012121221123012"
        b"7212001302132013311346136613011405145014201524154615711505162217"
        b"4017002002201120132020202220262031204220012103210521102112212121"
        b"3021632167217021002202221122172220222222372240225522012310231423"
        b"7023742335245324032527254125742501270327162745270130103012302130"
        b"2330503065307230003102312031313144314631013203321032253252327232"
        b"1133333330344734723400350635223555351436363663363337603704401740"
        b"3540374053405740744120423742404260426642074345430444514464442545"
        b"4345704505471047124730471250415070500051065126515551145232527252"
        b"0253535310542354275472540255315550562457425724604460466064602161"
        b"6161176264623063366344640565526533660367216703700570077010703270"
        b"5270267140711272457252720073157333736073217441740075027524753076"
    )

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        qs, scales = np.hsplit(rest, [QK_K // 4])

        d = d.view(np.float16).astype(np.float32)
        scales = scales.view(np.uint32)

        db = d * (np.float32(0.5) + (scales >> 28).astype(np.float32)) * np.float32(0.5)
        db = db.reshape((n_blocks, -1, 1, 1))

        # get the sign indices and unpack the bits
        signs = scales.reshape((n_blocks, -1, 1)) >> np.array([0, 7, 14, 21], dtype=np.uint32).reshape((1, 1, 4))
        ksigns = np.frombuffer(IQ2_XXS.ksigns, dtype=np.uint8).reshape((1, 1, 1, 128))
        signs = (signs & np.uint32(0x7F)).reshape((n_blocks, -1, 4, 1))
        signs = np.take_along_axis(ksigns, signs, axis=-1)
        signs = signs.reshape((n_blocks, -1, 4, 1)) >> np.array([i for i in range(8)], dtype=np.uint8).reshape((1, 1, 1, 8))
        signs = signs & np.uint8(0x01)
        signs = np.where(signs == 0, np.float32(1), np.float32(-1))
        signs = signs.reshape((n_blocks, -1, 4, 8))

        assert cls.grid is not None
        grid = np.take_along_axis(cls.grid, qs.reshape((n_blocks, -1, 1, 1)), axis=-2)
        grid = grid.reshape((n_blocks, -1, 4, 8))

        return (db * grid * signs).reshape((n_blocks, -1))


class IQ3_S(_Quant, qtype=GGMLQuantizationType.IQ3_S):
    grid_shape = (512, 4)
    grid_map = (0x01, 0x03, 0x05, 0x07, 0x09, 0x0b, 0x0d, 0x0f)
    grid_hex = (
        b"0000010002000500070010001100120014001600200021002500330040004200"
        b"4500470051005300600062007100740077000001010102010401100111011501"
        b"2001230127013101350144016101650172010002010205020702100213021602"
        b"2102250230023402420245024702510253027002730203031103150320032203"
        b"3103330336034403500352036703710375030004130417042104240432044004"
        b"4304510470040205040520052205260533054105450547056605730506061106"
        b"1306310652067106000702070407200722072607330750075407001001100210"
        b"0410101011101310151017102010221031103410361054105610611072100011"
        b"0111031106111011141121113011331141115011521170117611001212121512"
        b"1712201224123212401243125512601272120113041307131013131321132713"
        b"3013341341136213701303140514121414143114331442144614501454140115"
        b"1015131521153015321551152016241627164416461601170317101712172117"
        b"3517411762177017002001200320052007201020122014201620212023202720"
        b"3020322041204320452050205220672070207320752000210221102113211721"
        b"2221252131213421422151210122042207222122232230223722412253225722"
        b"7122742200230223052311232223242331233323422350236623012407242024"
        b"2324322435244124722475240425112522253725402553257025002602260726"
        b"2126552661260527112726273027432750270230113013301530173022303130"
        b"3330353042304430473051306330713001310331053114312131233140316031"
        b"7231763100321232203232323432503201331033143321332333273330334133"
        b"4333473355337333033411341634223431345234603464340135103512352535"
        b"3235443556357335163641360137033720372237353700400440124020402440"
        b"2740324041405040704002410741114113412241304135414341514155410142"
        b"0342104215422142334240425742624270420443114313432043224331433543"
        b"0044024424443744404471440545074521456245134634466046104715473047"
        b"4347514702501050145022504050445047505250665074500151035105511251"
        b"2151325172510052115223523052365253520253075310532753445351536553"
        b"7353015404542054325446541255265551555355425602570457225711601360"
        b"1560316033606060006120612761646112623462426255626262706200631463"
        b"2163406325644364626400650365346560650566406611671367007004700770"
        b"2070227036704070547062700271117124714371457101720472107216722172"
        b"3072517202733273357353730174057413742074507422754275027631760077"
    )

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        qs, rest = np.hsplit(rest, [QK_K // 4])
        qh, rest = np.hsplit(rest, [QK_K // 32])
        signs, scales = np.hsplit(rest, [QK_K // 8])

        d = d.view(np.float16).astype(np.float32)

        scales = scales.reshape((n_blocks, -1, 1)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2))
        scales = (scales & 0x0F).reshape((n_blocks, -1))
        db = d * (1 + 2 * scales)
        db = db.reshape((n_blocks, -1, 1, 1))

        # unpack the sign bits
        signs = signs.reshape((n_blocks, -1, 1)) >> np.array([i for i in range(8)], dtype=np.uint8).reshape((1, 1, 8))
        signs = signs & np.uint8(0x01)
        signs = np.where(signs == 0, np.float32(1), np.float32(-1))
        signs = signs.reshape((n_blocks, -1, 4, 8))

        qh = qh.reshape((n_blocks, -1, 1)) >> np.array([i for i in range(8)], dtype=np.uint8)
        qh = (qh & 0x01).astype(np.uint16).reshape((n_blocks, -1))
        qs = qs.astype(np.uint16) | (qh << 8)

        assert cls.grid is not None
        grid = np.take_along_axis(cls.grid, qs.reshape((n_blocks, -1, 1, 1)), axis=-2)
        grid = grid.reshape((n_blocks, -1, 4, 8))

        return (db * grid * signs).reshape((n_blocks, -1))


class IQ1_S(_Quant, qtype=GGMLQuantizationType.IQ1_S):
    # iq1s_grid, with each byte packed into 2 bits
    # -1, 0, 1 <=> 0, 1, 2
    grid_shape = (2048, 8)
    grid_map = (-1, 0, 1)
    grid_hex = (
        b"00000200050008000a00110015002000220028002a0045005100540056006500"
        b"8000820088008a009500a000a200a800aa000401050111011401160119011a01"
        b"2501410146014901520155015a0161016401660168018501910194019601a501"
        b"0002020208020a0215022002220228022a024502510259026402690280028202"
        b"88028a02910295029902a002a202a802aa021104140416042504410449045504"
        b"5a046404650491049904a5040105040505050605150518051a05290540054505"
        b"4a0550055105540555055605590560056205650568056a058105910595059805"
        b"9a05a105a405a505a605a9051406190641064406500652065506580660066106"
        b"6606690685069106940699060008020808080a0815082008220828082a084508"
        b"5108560865088008820888088a089508a008a208a808aa080509110914091909"
        b"2409250941095009510955096109640969099109940996099909a509000a020a"
        b"080a0a0a150a200a220a280a2a0a450a510a590a610a650a800a820a850a880a"
        b"8a0a950aa00aa20aa80aaa0a1010111014101910241025104110441050105510"
        b"58106110641065106910911094109610a110a510011104110611091110111211"
        b"1511181121112411291145114a11501151115211541155115611591160116511"
        b"841192119511a111a41111121412161225124012461249125212551258125a12"
        b"641266128512911294129612a512011406140914141415141814191421142614"
        b"41144514461448144a1451145414551456145914621465146814841489149014"
        b"94149514981499149a14a114a414a514a914021505150a151115141515151615"
        b"191520152215251528152a154115441545154615511552155415551556155915"
        b"5a1561156415651566156915801582158415851588158a159015911594159515"
        b"961599159a15a015a215a51501160416051606161516161618161a1621162616"
        b"401642164416451648164a165116551656165816591661166416651668166916"
        b"6a1686168a1692169516a416a916111816182518411844184618491850185518"
        b"58185a1860186118641866186918851891189418a5181019121915191a192119"
        b"25194219441945194819511954195519561959195a19601965196a1989199119"
        b"921995199819a119a619a919091a161a241a261a441a461a491a501a521a551a"
        b"581a611a661a691a851a911a961a9a1a0020022008200a201520202022202520"
        b"28202a20452051205920612065208020822088208a209520a020a220a520a820"
        b"aa2005211121142119212521422144214921552158215a216121642165216621"
        b"8521902196219921a521012208220a22112215222022222228222a2245225122"
        b"562259226522812288228a2291229522a022a222a822aa220524142416241924"
        b"252444244524462449245224552458245a2466248524912494249924a124a524"
        b"0925152521252925402545254825512554255525592562256525682589259025"
        b"9425952598259a25a125a425a625a92505261026122619262526412649265526"
        b"6026612669268426862690269a260028022808280a2815282028222828282a28"
        b"45285128542865288028822888288a28a028a228a828aa280929112914291929"
        b"2529462949295229552961296429662969298529902996299929a429a529002a"
        b"022a082a0a2a202a222a282a2a2a452a512a562a592a652a802a822a882a8a2a"
        b"952aa02aa22aa82aaa2a054011401640254049405240554058405a4061406440"
        b"664094409940a140a6400041014104410641094112411541164118411a412141"
        b"26412941454148414a41514154415541564159415a41654168416a4181418441"
        b"8641904192419541a041a141a241054211421442164225424142524255425a42"
        b"6442694289429442a5420144154419442944454448444a445144544455445644"
        b"61446244654468446a44814486448944904492449544a044a144a94401450245"
        b"05450a4511451445154516451945204525452a45414544454545464549455045"
        b"5145544555455645584559456145644565456645694582458445854588459145"
        b"94459545964599459a45a545a845aa450146054609461446154618461a462146"
        b"2446294640464246454648465046514652465546564659466246654668468146"
        b"85468a4694469546a146a446a6460548114815481a4825484248494850485548"
        b"5848614864486648694885489148944896489948a5480149054906490a491049"
        b"144915491849214924492649404945494a495149524954495549564959496049"
        b"6249654966496a49864989499249954996499849a149a449a649a949164a444a"
        b"464a494a554a584a5a4a644a694a944aa54a0150045005500650095012501550"
        b"1a50215024502950405045504850515054505550565059506550685086508950"
        b"95509850a050a150a650a9500551085109510a51115114511551165118511951"
        b"20512551265128512a5141514451455146514951505151515251545155515651"
        b"585159515a51615164516551665169518251855191519451955196519951a051"
        b"a551aa5101520652125215521a5221522452425245524a525152545255525652"
        b"595262526552855290529252955299529a52a452045405541154145415541654"
        b"185419542154255428542a54415444544554465449544a545054515454545554"
        b"5654585459545a54615462546454655466546954805488548a54915494549554"
        b"96549954a154a454a554aa540155025504550555065509551055115512551455"
        b"1555165519551a55215524552555265529554055415542554455455546554855"
        b"4955505551555255545555555655585559555a55605561556455655566556855"
        b"69556a5581558455855589558a559055915594559555965598559955a155a455"
        b"a555a655a9550056015602560456065608560956115614561556185619562056"
        b"2156225624562556265628562956415645564656485649564a56505651565256"
        b"545655565656585659565a566156645665566956825685568656885689568a56"
        b"915695569a56a256a556a656a856a95604580558065809581058155818582158"
        b"2a58455848584a58515854585558565858585958605862586458655882588958"
        b"9058925895589858a158a9580159025905590a59115914591559165919592559"
        b"41594459455946594959505951595259545955595659585959595a5961596459"
        b"655966596959815985598959915994599559965998599959a559045a085a155a"
        b"1a5a205a255a265a295a455a485a495a515a555a565a585a595a625a655a685a"
        b"6a5a815a8a5a925a955a965a985a9a5aa15a0560146016601960256044605060"
        b"5560566058605a60616064606660696081609660a56001610461066109611261"
        b"15612161226126612961456149615161556156615961656166616a6184618a61"
        b"92619561a161a661a96111621662196240624162466255625662586260628562"
        b"91629662a56211641264156416641a6421642664296440644264456448644a64"
        b"516454645564566459645a646064626465648464856489649064926494649564"
        b"966498649a64a164a464a964056508650a651165156516651965446545654665"
        b"496550655165546555655665596561656465656566656965866589658a659165"
        b"9565966599659a65a265a565a665a86502660966156620662666286629664066"
        b"456648664a66516654665566566658665a666066656668668066826685668a66"
        b"9466966698669966a066a466a666aa661668196825684168526855685a686168"
        b"6968856891689868a66801690469106915692169246926692969406941694569"
        b"4669486951695469556956695969606965696a69826984698a699569a169a469"
        b"a569a969116a166a186a416a446a496a506a556a586a5a6a646a656a696a866a"
        b"946a986a9a6aa66a0080028008800a802080228028802a804580508051805480"
        b"5680598065808080828088808a809580a080a280a880aa800581118114811681"
        b"1981258141814481498150815281558156815881598164816681698185818981"
        b"948196819981a5810082028208820a8215822082228228822a82518254825982"
        b"65828082828288828a829582a082a282a882aa82148419844184448451845584"
        b"5a846184648469849484998401850985128515851a8526852985408541854585"
        b"4885518554855585568559855a856585668568856a8581858485868589859085"
        b"928595859885a68511861686198625864186448649864a865086558659865a86"
        b"618666866a86858691869a86a4860088028808880a8815882088228828882a88"
        b"41884588518854885988658869888088828888888a889588a088a288a888aa88"
        b"05890689118914891689258941894489468949895089528955895a8961896489"
        b"858996899989a589008a028a088a0a8a158a208a228a288a2a8a458a518a548a"
        b"568a808a828a888a8a8a958aa08aa28aa88aaa8a059011901690189019902590"
        b"419046904990559058905a9069906a9085909190949096909990a59001910491"
        b"069109911091159118911a912191249126912991409145915091519154915591"
        b"569159916291659184918691929195919891a191a491a691a991059211921492"
        b"19922592449246924992509252925592589266926992859294929692a9920194"
        b"04940694109415941894269440944a9451945494559456945894599460946194"
        b"62946594849486949294949495949894a194a9940095059508950a9510951195"
        b"14951595169519952195259529952a9541954495459546954995509551955295"
        b"549555955695589559955a956195649565956695699581958595889591959295"
        b"94959595969599959a95a095a295a595a895aa95019604961096159619962096"
        b"2696299645964896499651965296559656965996659668968296849689968a96"
        b"929694969596a496a696a9960598169819982598419846985098529855985698"
        b"5a98649865988598919896989998a59804990699099910991299159918991a99"
        b"209921992499269940994299459948994a995199549955995699599962996599"
        b"66996a99819984999099929995999a99a199a699059a159a259a449a469a499a"
        b"509a559a589a619a859a919a949a959a969a00a002a008a00aa015a020a022a0"
        b"28a02aa045a051a054a056a059a080a082a088a08aa095a0a0a0a2a0a8a0aaa0"
        b"05a109a111a114a116a119a11aa146a149a151a155a158a15aa161a164a185a1"
        b"90a192a196a199a102a208a20aa210a219a222a228a22aa245a251a256a259a2"
        b"65a280a282a288a28aa295a2a0a2a2a2a8a2aaa219a425a441a444a450a454a4"
        b"55a458a45aa461a465a466a468a469a485a406a509a510a512a515a518a526a5"
        b"29a542a545a551a554a555a556a559a565a56aa581a584a585a586a589a592a5"
        b"95a598a505a611a616a61aa621a625a644a646a64aa652a655a656a658a660a6"
        b"62a686a690a695a696a699a6a1a6a4a6a6a600a802a808a80aa820a822a828a8"
        b"2aa851a854a856a859a880a882a888a88aa895a8a0a8a2a8a8a8aaa805a914a9"
        b"19a921a925a941a950a955a95aa961a966a969a990a996a900aa02aa08aa0aaa"
        b"20aa22aa28aa2aaa51aa54aa56aa80aa82aa88aa8aaa95aaa0aaa2aaa8aaaaaa"
    )

    delta = np.float32(0.125)

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        qs, qh = np.hsplit(rest, [QK_K // 8])

        d = d.view(np.float16).astype(np.float32)
        qh = qh.view(np.uint16)

        dl = d * (2 * ((qh >> 12) & 7) + 1)
        dl = dl.reshape((n_blocks, -1, 1, 1))
        delta = np.where((qh & np.uint16(0x8000)) == 0, cls.delta, -cls.delta)
        delta = delta.reshape((n_blocks, -1, 1, 1))

        qh = qh.reshape((n_blocks, -1, 1)) >> np.array([0, 3, 6, 9], dtype=np.uint16).reshape((1, 1, 4))
        qs = qs.astype(np.uint16) | ((qh & 7) << 8).reshape((n_blocks, -1))

        assert cls.grid is not None
        grid = np.take_along_axis(cls.grid, qs.reshape((n_blocks, -1, 1, 1)), axis=-2)
        grid = grid.reshape((n_blocks, -1, 4, 8))

        return (dl * (grid + delta)).reshape((n_blocks, -1))


class IQ1_M(_Quant, qtype=GGMLQuantizationType.IQ1_M):
    grid_shape = IQ1_S.grid_shape
    grid_map = IQ1_S.grid_map
    grid_hex = IQ1_S.grid_hex

    delta = IQ1_S.delta

    # Okay *this* type is weird. It's the only one which stores the f16 scales in multiple parts.
    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        qs, rest = np.hsplit(blocks, [QK_K // 8])
        qh, scales = np.hsplit(rest, [QK_K // 16])

        # The f16 scale is packed across multiple bytes
        scales = scales.view(np.uint16)
        d = (scales.reshape((n_blocks, 4)) & np.uint16(0xF000)) >> np.array([12, 8, 4, 0], dtype=np.uint16).reshape((1, 4))
        d = d[..., 0] | d[..., 1] | d[..., 2] | d[..., 3]
        d = d.view(np.float16).astype(np.float32).reshape((n_blocks, 1))

        scales = scales.reshape(n_blocks, -1, 1) >> np.array([0, 3, 6, 9], dtype=np.uint16).reshape((1, 1, 4))
        scales = (scales & 0x07).reshape((n_blocks, -1))
        dl = d * (2 * scales + 1)
        dl = dl.reshape((n_blocks, -1, 2, 1, 1))

        qh = qh.reshape((n_blocks, -1, 1)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2))
        qs = qs.astype(np.uint16) | ((qh & 0x07).astype(np.uint16) << 8).reshape((n_blocks, -1))

        delta = np.where(qh & 0x08 == 0, cls.delta, -cls.delta)
        delta = delta.reshape((n_blocks, -1, 2, 2, 1))

        assert cls.grid is not None
        grid = np.take_along_axis(cls.grid, qs.reshape((n_blocks, -1, 1, 1)), axis=-2)
        grid = grid.reshape((n_blocks, -1, 2, 2, 8))

        return (dl * (grid + delta)).reshape((n_blocks, -1))


class IQ4_NL(_Quant, qtype=GGMLQuantizationType.IQ4_NL):
    kvalues = (-127, -104, -83, -65, -49, -35, -22, -10, 1, 13, 25, 38, 53, 69, 89, 113)

    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, qs = np.hsplit(blocks, [2])

        d = d.view(np.float16).astype(np.float32)

        qs = qs.reshape((n_blocks, -1, 1, cls.block_size // 2)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))

        qs = (qs & np.uint8(0x0F)).reshape((n_blocks, -1, 1))

        kvalues = np.array(cls.kvalues, dtype=np.int8).reshape(1, 1, 16)
        qs = np.take_along_axis(kvalues, qs, axis=-1).astype(np.float32).reshape((n_blocks, -1))

        return (d * qs)


class IQ4_XS(_Quant, qtype=GGMLQuantizationType.IQ4_XS):
    @classmethod
    def dequantize_blocks(cls, blocks: np.ndarray) -> np.ndarray:
        n_blocks = blocks.shape[0]

        d, rest = np.hsplit(blocks, [2])
        scales_h, rest = np.hsplit(rest, [2])
        scales_l, qs = np.hsplit(rest, [QK_K // 64])

        d = d.view(np.float16).astype(np.float32)
        scales_h = scales_h.view(np.uint16)

        scales_l = scales_l.reshape((n_blocks, -1, 1)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2))
        scales_h = scales_h.reshape((n_blocks, 1, -1)) >> np.array([2 * i for i in range(QK_K // 32)], dtype=np.uint16).reshape((1, -1, 1))
        scales_l = scales_l.reshape((n_blocks, -1)) & np.uint8(0x0F)
        scales_h = scales_h.reshape((n_blocks, -1)).astype(np.uint8) & np.uint8(0x03)

        scales = (scales_l | (scales_h << np.uint8(4))).astype(np.int8) - np.int8(32)
        dl = (d * scales.astype(np.float32)).reshape((n_blocks, -1, 1))

        qs = qs.reshape((n_blocks, -1, 1, 16)) >> np.array([0, 4], dtype=np.uint8).reshape((1, 1, 2, 1))
        qs = qs.reshape((n_blocks, -1, 32, 1)) & np.uint8(0x0F)

        kvalues = np.array(IQ4_NL.kvalues, dtype=np.int8).reshape((1, 1, 1, -1))
        qs = np.take_along_axis(kvalues, qs, axis=-1).astype(np.float32).reshape((n_blocks, -1, 32))

        return (dl * qs).reshape((n_blocks, -1))

# ===== file: tools/greedy_identity.py =====
"""Do two engine builds produce the same greedy tokens?

    python3 tools/greedy_identity.py REF_BIN NEW_BIN --model FILE.basalt [--fixtures DIR] [--tokens N] [-- ENGINE ARGS...]

Each binary runs as `--serve` with BASE plus any flags after `--` (which replace BASE values). The bench fixtures'
prompts (conv, struct, prose: comma-separated token ids per line) run at temperature 0 and every token is compared.
Adaptive swaps and the PCIe share are off so placement cannot drift; pin the main card's tier too (`-- --expert-cache
N`) when the builds' VRAM use differs, since auto-sized tiers change rounding. A second card's tier holds what fits:
check that its expert count ("peer GPU 1: N experts" in both logs) matches.

Prints "GREEDY IDENTICAL" (exit 0) or "GREEDY DIFFERS" with the first difference (exit 1); exit 2 when an engine fails.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PROMPTS = ("conv", "struct", "prose")
# flags after `--` replace these
BASE = ["--spec", "7", "--max-context", "8192"]
PINNED = ["--adapt-swaps", "0", "--peer-adapt-swaps", "0"]


def set_args(args: list[str], extra: list[str]) -> list[str]:
    """`args` with each flag of `extra` replacing the same flag (and its value) in `args`, or added."""
    out = list(args)
    i = 0
    while i < len(extra):
        flag = extra[i]
        has_value = i + 1 < len(extra) and not extra[i + 1].startswith("--")
        value = [extra[i + 1]] if has_value else []
        if flag in out:
            j = out.index(flag)
            old_has_value = j + 1 < len(out) and not out[j + 1].startswith("--")
            out[j:j + 1 + old_has_value] = [flag, *value]
        else:
            out += [flag, *value]
        i += 1 + has_value
    return out


def run(label: str, engine_bin: str, args: list[str], prompts: list[str], tokens: int, logs: Path) -> list[list[int]] | None:
    """One engine session over the prompts: the generated ids of each, or None when the engine failed."""
    err = open(logs / f"{label}.err", "w")
    proc = subprocess.Popen([engine_bin, "--serve", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                            text=True, bufsize=1)
    out: list[list[int]] = []
    try:
        for line in proc.stdout:
            if line.startswith("READY"):
                break
        else:
            return None
        for ids in prompts:
            proc.stdin.write(f"GEN {tokens} temperature=0 {ids}\n")
            proc.stdin.flush()
            got: list[int] = []
            for line in proc.stdout:
                if line.startswith("T "):
                    got.append(int(line.split()[1]))
                elif line.startswith("DONE"):
                    break
                elif line.startswith("ERR"):
                    print(f"{label}: {line.strip()}", file=sys.stderr)
                    return None
            else:
                return None
            out.append(got)
        proc.stdin.write("QUIT\n")
        proc.stdin.flush()
        proc.wait(timeout=120)
        return out
    except (BrokenPipeError, subprocess.TimeoutExpired):
        return None
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
        err.close()


def main() -> int:
    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        k = argv.index("--")
        argv, extra = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ref")
    ap.add_argument("new")
    ap.add_argument("--model", required=True)
    ap.add_argument("--fixtures", default=str(HERE.parent / "data" / "bench"))
    ap.add_argument("--tokens", type=int, default=300)
    a = ap.parse_args(argv)

    prompts = []
    for name in PROMPTS:
        path = Path(a.fixtures) / f"{name}.ids"
        if not path.is_file():
            print(f"greedy_identity: {path} is missing", file=sys.stderr)
            return 2
        prompts.append(path.read_text().strip())
    args = set_args(["--model", a.model, *BASE], PINNED + extra)

    logs = Path(tempfile.mkdtemp(prefix="greedy-identity-"))
    results = {}
    for label, engine_bin in (("ref", a.ref), ("new", a.new)):
        r = run(label, engine_bin, args, prompts, a.tokens, logs)
        if r is None:
            tail = (logs / f"{label}.err").read_text().strip().splitlines()[-5:]
            print(f"greedy_identity: the {label} engine failed (logs: {logs})", *tail, sep="\n  ", file=sys.stderr)
            return 2
        print(f"{label}: {sum(map(len, r))} tokens, {len(r)} requests")
        results[label] = r

    for name, ref, new in zip(PROMPTS, results["ref"], results["new"]):
        if ref != new:
            i = next((k for k, (x, y) in enumerate(zip(ref, new)) if x != y), min(len(ref), len(new)))
            print(f"GREEDY DIFFERS: {name}, token {i}: ref {ref[i:i + 5]} new {new[i:i + 5]} (logs: {logs})")
            return 1
    print("GREEDY IDENTICAL")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ===== file: tools/iq_fixture.py =====
"""Deterministic i-quant fixtures for tests/kernels/iq_parity.cpp.

For each of the ten types iq_parity tests, writes `<out>/<name>.bin` (int32 header: the KERNEL type id,
rows, cols - then the raw GGUF block bytes) and `<out>/<name>.f32` (rows*cols float32 reference values,
dequantized by the same library the comparison treats as ground truth):

    python tools/iq_fixture.py --out build/iq_fixture
    python tools/iq_fixture.py --out build/iq_fixture --seed 7 --rows 32 --cols 256

Deterministic: a seed and fixed dimensions drive every random choice. The i-quants are only dequantized (tools/ggufq.py), so
each block pairs a sane fp16 scale (at the layout's scale offsets) with seeded index and sub-scale bytes.

Needs numpy. Q2_0 (GGML type 42, not in ggufq) is
encoded by tools/gguf_writer.py. The kernel ids in the .bin headers match the repo's i-quant dispatch table and the
ggufq enum ids.
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

try:
    import ggufq
    from ggufq import dequantize as gguf_dequantize
    from gguf_writer import dequantize_q2_0, quantize_q2_0
except ImportError as e:  # say so plainly instead of a traceback
    sys.stderr.write(f"iq_fixture: missing dependency: {e}\n"
                     "numpy is required.\n")
    sys.exit(3)

QT = ggufq.GGMLQuantizationType
# name -> (block values, block bytes), mirroring ggufq's dequantize_blocks; scales sit at fixed offsets
# and the rest of a block is seeded random indices and sub-scales
LAYOUT = {   # equals ggufq's GGML_QUANT_SIZES and the kernels' sizes
    "IQ2_XXS": (256,  66),
    "IQ2_XS":  (256,  74),
    "IQ2_S":   (256,  82),
    "IQ3_XXS": (256,  98),
    "IQ3_S":   (256, 110),
    "IQ1_M":   (256,  56),
    "IQ4_NL":   (32,  18),
    "IQ4_XS":  (256, 136),
    "Q3_K":    (256, 110),
}
HALF_ONE = struct.pack("<e", 1.0)      # fp16 1.0 = 0x3c00


def repair_scales(name: str, raw: np.ndarray) -> None:
    """Overwrite every block's fp16 scale with a sane value, in place. IQ*_X* and IQ4_* keep it at offset 0; Q3_K
    after the 32-byte hmask, 64-byte qs and 12 scale bytes; IQ1_M splits it over the high nibbles of the four
    uint16s at bytes 48..56 (1.0 is nibbles 3, C, 0, 0 on bytes 49, 51, 54, 55)."""
    if name == "Q3_K":
        raw[:, 108:110] = np.frombuffer(HALF_ONE, dtype=np.uint8)
        return
    if name == "IQ1_M":
        raw[:, 48:56] &= np.uint8(0x0F)            # clear the four scale nibbles
        for byte, nib in ((49, 0x30), (51, 0xC0), (54, 0x00), (55, 0x00)):
            raw[:, byte] |= np.uint8(nib)
        return
    raw[:, 0:2] = np.frombuffer(HALF_ONE, dtype=np.uint8)
# name -> kernel type id for the .bin header (equal to the ggufq enum id)
KERNEL_IDS = {"IQ2_XXS": 16, "IQ2_XS": 17, "IQ2_S": 22, "IQ3_XXS": 18, "IQ3_S": 21,
              "IQ1_M": 29, "IQ4_NL": 20, "IQ4_XS": 23, "Q2_0": 42, "Q3_K": 11}


def build(name: str, rows: int, cols: int, seed: int):
    """The .bin body (header + raw blocks) and the .f32 reference for one type."""
    kernel_type = KERNEL_IDS[name]
    n = rows * cols
    rng = np.random.RandomState((seed, hash(name) & 0xFFFF) if False else (seed, KERNEL_IDS[name]))
    if name == "Q2_0":                                 # ggufq has no type 42
        w = rng.standard_normal(n).astype(np.float32)
        raw = np.frombuffer(quantize_q2_0(w), dtype=np.uint8)
        ref = dequantize_q2_0(raw.tobytes()).astype(np.float32)
        flat = raw
    else:
        block_values, block_bytes = LAYOUT[name]
        blocks = n // block_values
        raw = rng.randint(0, 256, (blocks, block_bytes), dtype=np.uint8)
        repair_scales(name, raw)                       # sane scales; indices stay random
        flat = raw.reshape(-1)
        ref = np.asarray(gguf_dequantize(flat, getattr(QT, name)), dtype=np.float32)
    if not np.isfinite(ref).all() or float(np.abs(ref).max()) > 1e4:
        raise RuntimeError(f"{name}: the dequantized reference is not finite/tame - the layout table "
                           f"(block bytes, scale offsets) does not match ggufq's dequantizer")
    header = struct.pack("<3i", kernel_type, rows, cols)
    return header + flat.tobytes(), ref


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="logs/iq_fixture", help="the fixture directory (created; may live in the "
                                                             "build directory - the source tree is not written)")
    ap.add_argument("--seed", type=int, default=7, help="the fixture RNG seed (deterministic generation)")
    ap.add_argument("--rows", type=int, default=32, help="weight rows (rows*cols stays a multiple of 256)")
    ap.add_argument("--cols", type=int, default=256, help="the reduction dimension, a multiple of 256")
    args = ap.parse_args()
    if args.cols % 256 or args.rows * args.cols % 256:
        ap.error("--cols must be a multiple of 256 and rows*cols a multiple of 256")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in KERNEL_IDS:
        blob, ref = build(name, args.rows, args.cols, args.seed)
        (out / f"{name}.bin").write_bytes(blob)
        (out / f"{name}.f32").write_bytes(np.ascontiguousarray(ref, dtype="<f4").tobytes())
        print(f"  {name:<8} type {KERNEL_IDS[name]:>2}  {args.rows} x {args.cols}  "
              f"{len(blob) - 12} block bytes, {ref.size} reference floats")
    print(f"iq_fixture: {len(KERNEL_IDS)} fixtures in {out} (seed {args.seed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ===== file: tools/migrations/basalt_v1_to_v2.py =====
"""Write a version 2 .basalt file from a version 1 file, without re-quantizing anything.

    python3 tools/migrations/basalt_v1_to_v2.py OLD.basalt NEW.basalt [--force]

Every model section is copied byte for byte (its SHA-256 checked against OLD's table); `profile` is dropped; `hc8` is
computed from the file's own hyper-connection matrices (tools/convert/hc8.py); `meta` loses `profile`, `hc8`,
`sampling` and the converter's git revision, and records version 2. Sampling defaults that only `meta.sampling` held
move to `gguf_kv` as llama.cpp's general.sampling.* keys. Needs numpy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "convert"))
import hc8 as H  # noqa: E402
from model_file import ModelFile, ModelWriter  # noqa: E402

DROPPED = ("profile", "hc8", "meta")
# meta.sampling key -> GGUF key
SAMPLING_KV = {"temperature": "general.sampling.temp", "top_p": "general.sampling.top_p",
               "top_k": "general.sampling.top_k", "min_p": "general.sampling.min_p",
               "repetition_penalty": "general.sampling.penalty_repeat",
               "penalty_last_n": "general.sampling.penalty_last_n"}


def die(msg: str):
    raise SystemExit(f"migration: {msg}")


def new_meta(meta: dict) -> dict:
    meta = dict(meta)
    kv = dict(meta.get("gguf_kv") or {})
    for key, value in (meta.pop("sampling", None) or {}).items():
        if key in SAMPLING_KV and value is not None:
            kv.setdefault(SAMPLING_KV[key], value)
    meta["gguf_kv"] = kv
    for key in ("profile", "hc8"):
        meta.pop(key, None)
    if isinstance(meta.get("converter"), dict):
        meta["converter"] = {k: v for k, v in meta["converter"].items() if k != "git"}
    meta["version"] = 2
    return meta


def migrate(src: Path, out: Path) -> None:
    try:
        old = ModelFile(str(src), versions=(1, 2))
    except ValueError as e:
        die(str(e))
    if old.version != 1:
        die(f"{src} is .basalt version {old.version}; this script reads version 1")
    meta = json.loads(old.read("meta"))
    partial = Path(str(out) + ".partial")
    partial.unlink(missing_ok=True)
    w = ModelWriter(str(partial))
    t0 = time.time()
    ok = False
    try:
        with open(src, "rb") as f:
            def read(name, off, size):
                f.seek(old.section(name)[0] + off)
                return f.read(size)
            for name in old.names():
                if name in DROPPED:
                    continue
                off, size = old.section(name)
                w.add_file(name, str(src), off, size)
                if w.entries[-1][3] != old.sections[name][2]:
                    die(f"section {name}: its bytes do not match the SHA-256 in {src}'s table")
                print(f"  {name:<44} {size / (1 << 30):9.3f} GiB", flush=True)
            whole = lambda n: read(n, 0, old.section(n)[1]) if n in old.sections else None
            try:
                table = H.section(read, whole("dense.index"), whole("mtp/dense.index"))
            except H.Hc8Error as e:
                die(f"hc8: {e}")
        w.add_bytes("hc8", table)
        print(f"  {'hc8':<44} {len(table) / (1 << 30):9.3f} GiB", flush=True)
        w.add_bytes("meta", json.dumps(new_meta(meta), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        w.close()
        ok = True
    finally:
        if not ok:
            w.f.close()
            partial.unlink(missing_ok=True)
    os.replace(partial, out)
    print(f"done: {len(w.entries)} sections, {out.stat().st_size / (1 << 30):.3f} GiB, {time.time() - t0:.1f} s -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old", help="the version 1 .basalt file (read only)")
    ap.add_argument("new", help="the version 2 .basalt file to write")
    ap.add_argument("--force", action="store_true", help="overwrite NEW")
    a = ap.parse_args()
    src, out = Path(a.old), Path(a.new)
    if not src.is_file():
        die(f"missing input: {src}")
    if out.exists() and not a.force:
        die(f"{out} exists (use --force to overwrite)")
    if out.exists() and out.resolve() == src.resolve():
        die("NEW must be another file than OLD")
    migrate(src, out)


if __name__ == "__main__":
    main()

# ===== file: tools/migrations/config_v1_to_v2.py =====
#!/usr/bin/env python3
"""Turn a single-model server config into the v2 shape with "models": prints it, or writes it to a new file.

    python3 tools/migrations/config_v1_to_v2.py basalt_config.json [-o NEW.json]

The input is only read. What says which model it is goes in the model entry; the rest goes in the top-level "engine", where
it applies to every model added later; what the whole server shares stays in "server".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VERSION = 2
# stays with the model: its identity, what it costs and how it answers
ENTRY_KEYS = {"model", "alias", "gpu", "vision", "parallel", "slot-full-context", "max-context", "kv",
              "temperature", "top-p", "top-k", "min-p", "presence-penalty", "frequency-penalty", "repetition-penalty",
              "penalty-last-n", "seed", "reasoning-budget-tokens", "thinking-loop-tokens", "answer-room", "grammar",
              "fit-max-tokens"}
# server options that apply per model and leave "server"
MODEL_OPTIONS = ENTRY_KEYS | {"idle-sleep", "idle-unload", "min-free-vram-mib"}


def name_of(key) -> str:
    return str(key).strip().lower().replace("_", "-").removeprefix("--")


def migrated(old) -> dict:
    """The v2 object for a v1 one; ValueError when `old` is not a v1 config that names its model."""
    if not isinstance(old, dict) or "models" in old:
        raise ValueError("not a v1 config: it already has \"models\"" if isinstance(old, dict) else "expected a JSON object")
    extra = set(old) - {"v", "server", "engine"}
    if extra:
        raise ValueError(f"not a v1 config: unexpected key {sorted(extra)[0]!r}")
    server, engine, entry, shared = {}, {}, {}, {}
    for key, value in old.get("server", {}).items():
        name = name_of(key)
        if name in ENTRY_KEYS:
            entry[name] = value
        elif name in MODEL_OPTIONS:
            shared[name] = value
        else:
            server[name] = value
    for key, value in old.get("engine", {}).items():
        name = name_of(key)
        (entry if name in ENTRY_KEYS else shared)[name] = value
    if "model" not in entry:
        raise ValueError("this config names no model (\"model\" in \"server\"): add it first")
    engine = shared
    out = {"version": VERSION, "models": [entry]}
    if engine:
        out["engine"] = engine
    if server:
        out["server"] = server
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="the old config file; only read")
    ap.add_argument("-o", "--output", help="write here instead of printing; the file must not exist")
    a = ap.parse_args(argv)
    try:
        with open(a.config, encoding="utf-8") as f:
            new = migrated(json.load(f))
    except (OSError, ValueError) as e:
        print(f"{a.config}: {e}", file=sys.stderr)
        return 2
    text = json.dumps(new, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if not a.output:
        sys.stdout.write(text)
        return 0
    try:
        with Path(a.output).open("x", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        print(f"{a.output}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ===== file: tools/model_file.py =====
"""Read and write the .basalt model file (the layout in include/basalt/core/model_format.hpp).

    w = ModelWriter(path)
    w.add_bytes("meta", json_bytes)
    w.add_file("experts", "/path/experts.bin")        # streamed and hashed while copied
    w.add_chunks("dense", chunk_iterator)             # streamed
    w.close()

    m = ModelFile(path)                               # version 2 only; versions=(1,) for the migration's input
    m.names(); m.section("meta") -> (offset, bytes); m.read("meta") -> bytes; m.verify("dense") -> bool

    append_section(path, "name", "/path/source", new_meta, undo_dir)   # no other section moves or changes
    undo_append(path, undo_dir)
"""
from __future__ import annotations

import hashlib
import os
import struct

MAGIC = b"BASALTMF"
VERSION = 2
MIGRATION = "tools/migrations/basalt_v1_to_v2.py"
SECTION_ALIGN = 2 << 20
HEADER_BYTES = 64 << 10
TENSOR_ALIGN = 4096                            # inside the `gguf` section
HEADER = struct.Struct("<8sIIQ40s")           # 64 bytes
ENTRY = struct.Struct("<64sQQ32s16s")         # 128 bytes
MAX_SECTIONS = (HEADER_BYTES - HEADER.size) // ENTRY.size
COPY_CHUNK = 64 << 20


def _align(n: int) -> int:
    return (n + SECTION_ALIGN - 1) // SECTION_ALIGN * SECTION_ALIGN


class Stream:
    """A part of `add_parts` produced on the fly: `size` bytes yielded by the iterable `chunks`."""

    def __init__(self, size: int, chunks):
        self.size, self.chunks = size, chunks


class ModelWriter:
    """Sections are appended in order; the header and table are written by close()."""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "wb")
        self.f.write(b"\0" * HEADER_BYTES)
        self.entries: list[tuple[str, int, int, bytes]] = []

    def _start(self, name: str) -> int:
        if len(name.encode()) >= 64:
            raise ValueError(f"section name too long: {name}")
        if any(e[0] == name for e in self.entries):
            raise ValueError(f"duplicate section: {name}")
        if len(self.entries) >= MAX_SECTIONS:
            raise ValueError("too many sections")
        off = _align(self.f.tell())
        self.f.write(b"\0" * (off - self.f.tell()))
        return off

    def add_bytes(self, name: str, data: bytes) -> None:
        off = self._start(name)
        self.f.write(data)
        self.entries.append((name, off, len(data), hashlib.sha256(data).digest()))

    def add_file(self, name: str, src: str, offset: int = 0, size: int | None = None) -> None:
        """Copy `size` bytes of `src` from `offset` (the whole rest of the file by default)."""
        off = self._start(name)
        h = hashlib.sha256()
        left = (os.path.getsize(src) - offset) if size is None else size
        n = 0
        with open(src, "rb") as s:
            s.seek(offset)
            while left > 0:
                b = s.read(min(COPY_CHUNK, left))
                if not b:
                    raise IOError(f"{src}: short read")
                h.update(b)
                self.f.write(b)
                left -= len(b)
                n += len(b)
        self.entries.append((name, off, n, h.digest()))

    def add_chunks(self, name: str, chunks) -> int:
        """One section from an iterator of byte chunks (streamed, hashed as written).  Returns its size."""
        off = self._start(name)
        h = hashlib.sha256()
        n = 0
        for b in chunks:
            h.update(b)
            self.f.write(b)
            n += len(b)
        self.entries.append((name, off, n, h.digest()))
        return n

    def add_parts(self, name: str, parts, align: int = TENSOR_ALIGN) -> list[int]:
        """One section of several parts, each at a multiple of `align` (zero padded). A part is bytes, a Stream or
        (src_path, offset, size); returns each part's offset. The hash covers the whole section."""
        off = self._start(name)
        h = hashlib.sha256()
        pos, offsets = 0, []

        def put(b: bytes) -> None:
            nonlocal pos
            h.update(b)
            self.f.write(b)
            pos += len(b)

        for part in parts:
            pad = (-pos) % align
            if pad:
                put(b"\0" * pad)
            offsets.append(pos)
            if isinstance(part, (bytes, bytearray)):
                put(bytes(part))
                continue
            if isinstance(part, Stream):
                n0 = pos
                for b in part.chunks:
                    put(b)
                if pos - n0 != part.size:
                    raise IOError(f"stream part: {pos - n0} bytes, expected {part.size}")
                continue
            src, o, size = part
            left = size
            with open(src, "rb") as s:
                s.seek(o)
                while left > 0:
                    b = s.read(min(COPY_CHUNK, left))
                    if not b:
                        raise IOError(f"{src}: short read")
                    put(b)
                    left -= len(b)
        self.entries.append((name, off, pos, h.digest()))
        return offsets

    def close(self) -> None:
        end = self.f.tell()
        self.f.seek(0)
        self.f.write(HEADER.pack(MAGIC, VERSION, len(self.entries), end, b"\0" * 40))
        for name, off, n, digest in self.entries:
            self.f.write(ENTRY.pack(name.encode(), off, n, digest, b"\0" * 16))
        self.f.close()


def _sync(f) -> None:
    f.flush()
    os.fsync(f.fileno())


def _write_zeros(f, n: int) -> None:
    while n > 0:
        k = min(COPY_CHUNK, n)
        f.write(b"\0" * k)
        n -= k


def append_section(path: str, name: str, src: str, new_meta: bytes, undo_dir: str) -> None:
    """Adds a section to a file without moving or changing any other: the section and `new_meta` go past the file's end,
    then one write replaces the header and table, then the old meta's bytes, now a gap, are zeroed. Until that one write the
    file reads as it did. `undo_dir` (new) keeps what undo_append restores."""
    m = ModelFile(path)
    if name in m.sections or len(name.encode()) >= 64:
        raise ValueError(f"{path}: cannot add a section named {name}")
    with open(path, "r+b") as f:
        head = f.read(HEADER_BYTES)
        magic, version, n, file_bytes, reserved = HEADER.unpack_from(head, 0)
        if n + 1 > MAX_SECTIONS:
            raise ValueError(f"{path}: the section table is full")
        if os.path.getsize(path) != file_bytes:
            raise ValueError(f"{path}: {os.path.getsize(path)} bytes, its table says {file_bytes}")
        if not m.verify("meta"):
            raise ValueError(f"{path}: its meta section does not match its hash")
        old_off, old_size = m.section("meta")
        os.makedirs(undo_dir)
        with open(os.path.join(undo_dir, "header.bin"), "wb") as u:
            u.write(head)
            _sync(u)
        with open(os.path.join(undo_dir, "meta.bin"), "wb") as u:
            u.write(m.read("meta"))
            _sync(u)

        off = _align(file_bytes)
        f.seek(file_bytes)
        _write_zeros(f, off - file_bytes)
        h = hashlib.sha256()
        size = 0
        with open(src, "rb") as s:
            while b := s.read(COPY_CHUNK):
                h.update(b)
                f.write(b)
                size += len(b)
        meta_off = _align(off + size)
        _write_zeros(f, meta_off - (off + size))
        f.write(new_meta)
        end = meta_off + len(new_meta)
        _sync(f)

        table = [(k, v[0], v[1], v[2]) for k, v in m.sections.items() if k != "meta"]
        table += [(name, off, size, h.digest()), ("meta", meta_off, len(new_meta), hashlib.sha256(new_meta).digest())]
        new_head = HEADER.pack(magic, version, len(table), end, reserved)
        new_head += b"".join(ENTRY.pack(k.encode(), o, b, d, b"\0" * 16) for k, o, b, d in table)
        f.seek(0)
        f.write(new_head + b"\0" * (HEADER_BYTES - len(new_head)))
        _sync(f)

        f.seek(old_off)
        _write_zeros(f, old_size)
        _sync(f)


def undo_append(path: str, undo_dir: str) -> None:
    """Restores the file append_section changed, bit for bit."""
    with open(os.path.join(undo_dir, "header.bin"), "rb") as u:
        head = u.read()
    with open(os.path.join(undo_dir, "meta.bin"), "rb") as u:
        old_meta = u.read()
    _, _, n, file_bytes, _ = HEADER.unpack_from(head, 0)
    old_off = None
    for i in range(n):
        name, off, _, _, _ = ENTRY.unpack_from(head, HEADER.size + i * ENTRY.size)
        if name.rstrip(b"\0") == b"meta":
            old_off = off
    if old_off is None or len(head) != HEADER_BYTES:
        raise ValueError(f"{undo_dir}: not what append_section kept")
    with open(path, "r+b") as f:
        f.seek(old_off)
        f.write(old_meta)
        _sync(f)
        f.seek(0)
        f.write(head)
        _sync(f)
        f.truncate(file_bytes)
        _sync(f)


class ModelFile:
    def __init__(self, path: str, versions: tuple[int, ...] = (VERSION,)):
        self.path = path
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES)
        magic, version, n, file_bytes, _ = HEADER.unpack_from(head, 0)
        if magic != MAGIC:
            raise ValueError(f"{path}: not a .basalt model file")
        if version not in versions:
            hint = f"; upgrade it with {MIGRATION}" if version < VERSION else ""
            raise ValueError(f"{path}: .basalt version {version}, this build reads version {VERSION}{hint}")
        self.version = version
        if os.path.getsize(path) < file_bytes:
            raise ValueError(f"{path}: truncated ({os.path.getsize(path)} of {file_bytes} bytes)")
        self.sections: dict[str, tuple[int, int, bytes]] = {}
        for i in range(n):
            name, off, size, digest, _ = ENTRY.unpack_from(head, HEADER.size + i * ENTRY.size)
            self.sections[name.rstrip(b"\0").decode()] = (off, size, digest)

    def names(self) -> list[str]:
        return list(self.sections)

    def section(self, name: str) -> tuple[int, int]:
        off, size, _ = self.sections[name]
        return off, size

    def read(self, name: str) -> bytes:
        off, size = self.section(name)
        with open(self.path, "rb") as f:
            f.seek(off)
            return f.read(size)

    def verify(self, name: str) -> bool:
        off, size, digest = self.sections[name]
        h = hashlib.sha256()
        with open(self.path, "rb") as f:
            f.seek(off)
            left = size
            while left > 0:
                b = f.read(min(COPY_CHUNK, left))
                if not b:
                    return False
                h.update(b)
                left -= len(b)
        return h.digest() == digest

# ===== file: tools/ppl_gate.py =====
#!/usr/bin/env python3
"""Perplexity and KL divergence through the decode path, against a llama.cpp base.

The base is `llama-perplexity --kl-divergence-base`'s file (wikitext-2, 512-token chunks, second half scored). The
engine runs as `--serve` with the model plus --ppl-base and --ppl-out; per chunk it reads the first half as a prompt
and teacher-forces the rest through verify windows of T tokens (see the engine's ppl_gate.hpp). Flags this tool does
not know are passed to the engine as they are; it sets only what the measurement needs (--spec 5,
no adaptive swaps, the first two visible GPUs when there are two: --gpu 0,1), and an engine flag given here replaces
that value.

  python3 tools/ppl_gate.py --model FILE.basalt --base base.kld --out DIR [--engine-bin BIN] [--chunks 20] [--t 3]
                            [--gpu 0 ...]
  python3 tools/ppl_gate.py --report DIR/rows.tsv          # summary of a finished run
  python3 tools/ppl_gate.py --diff A/rows.tsv B/rows.tsv   # row by row: identical rows, largest NLL/KLD change
"""
from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import time
from pathlib import Path

ENGINE_BIN = Path(__file__).resolve().parents[1] / "bin" / "basalt"


def read_base(path: str):
    with open(path, "rb") as f:
        if f.read(8) != b"_logits_":
            sys.exit(f"{path}: not a llama.cpp KL-divergence base")
        n_ctx, n_vocab, n_chunk = struct.unpack("<Iii", f.read(12))
        toks = struct.unpack(f"<{n_ctx * n_chunk}i", f.read(4 * n_ctx * n_chunk))
    return n_ctx, n_vocab, n_chunk, [toks[k * n_ctx:(k + 1) * n_ctx] for k in range(n_chunk)]


def summary(rows_path: str, extra: dict | None = None) -> str:
    nll, nllb, kld, same, dlp = [], [], [], 0, []
    chunks = set()
    for line in open(rows_path):
        f = line.split("\t")
        if len(f) < 10:
            continue
        chunks.add(int(f[0]))
        nll.append(float(f[3])); nllb.append(float(f[4])); kld.append(float(f[5]))
        same += int(f[6]); dlp.append(float(f[9]))
    n = len(nll)
    if n == 0:
        return "no rows"
    ks = sorted(kld)
    pct = lambda q: ks[min(n - 1, int(round(q * (n - 1))))]
    mn = sum(nll) / n
    se = math.sqrt(max(0.0, sum(v * v for v in nll) / n - mn * mn) / max(1, n - 1))
    out = [f"chunks {len(chunks)}  rows {n}",
           f"PPL {math.exp(mn):.4f} +/- {math.exp(mn) * se:.4f}   base PPL {math.exp(sum(nllb) / n):.4f}   "
           f"ln(PPL/base) {mn - sum(nllb) / n:.5f}",
           f"KLD mean {sum(kld) / n:.5f}  median {pct(0.5):.5f}  p99 {pct(0.99):.4f}  max {ks[-1]:.4f}",
           f"same top-1 {100.0 * same / n:.2f}%   mean |dlogp| of the base top-1 {sum(dlp) / n:.5f}"]
    if extra:
        out.append("  ".join(f"{k} {v}" for k, v in extra.items()))
    return "\n".join(out)


def diff(pa: str, pb: str) -> str:
    def load(p):
        return {(f[0], f[1]): f for f in (l.rstrip("\n").split("\t") for l in open(p)) if len(f) >= 10}
    a, b = load(pa), load(pb)
    keys = sorted(set(a) & set(b), key=lambda k: (int(k[0]), int(k[1])))
    if not keys:
        return "no common rows"
    same = sum(a[k][3:] == b[k][3:] for k in keys)
    top = sum(a[k][7] == b[k][7] for k in keys)
    dn = max(abs(float(a[k][3]) - float(b[k][3])) for k in keys)
    dk = max(abs(float(a[k][5]) - float(b[k][5])) for k in keys)
    first = next((k for k in keys if a[k][3:] != b[k][3:]), None)
    return (f"rows {len(keys)}: identical {same} ({100.0 * same / len(keys):.2f}%), same top-1 {top}, "
            f"max |dNLL| {dn:.6f}, max |dKLD| {dk:.6f}, first difference "
            f"{'none' if first is None else 'chunk %s pos %s' % first}")


def gpu_count() -> int:
    try:
        out = subprocess.run(["nvidia-smi", "--list-gpus"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in out.splitlines() if line.startswith("GPU "))


def set_args(args: list[str], extra: list[str]) -> list[str]:
    """`args` with each flag of `extra` replacing the same flag (and its value) in `args`, or added."""
    out = list(args)
    i = 0
    while i < len(extra):
        flag = extra[i]
        has_value = i + 1 < len(extra) and not extra[i + 1].startswith("--")
        value = [extra[i + 1]] if has_value else []
        if flag in out:
            j = out.index(flag)
            old_has_value = j + 1 < len(out) and not out[j + 1].startswith("--")
            out[j:j + 1 + old_has_value] = [flag, *value]
        else:
            out += [flag, *value]
        i += 1 + has_value
    return out


def run(a) -> int:
    n_ctx, n_vocab, n_chunk, chunks = read_base(a.base)
    first = n_ctx // 2
    args = ["--model", str(Path(a.model).resolve()), "--spec", "5",
            "--adapt-swaps", "0", "--peer-adapt-swaps", "0", "--max-context", "4096"]
    if gpu_count() > 1:
        args += ["--gpu", "0,1"]
    args = set_args(args, a.engine_args + ["--ppl-base", str(Path(a.base).resolve())])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = out / "rows.tsv"
    rows.unlink(missing_ok=True)
    args = set_args(args, ["--ppl-out", str(rows)])
    log = open(out / "engine.log", "w")
    log.write(f"ppl_gate: engine={a.engine_bin} T={a.t} chunks={a.chunks} args={' '.join(args)}\n")
    log.flush()
    t0 = time.time()
    eng = subprocess.Popen([a.engine_bin, "--serve", *args], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1)
    try:
        for line in eng.stdout:
            if line.startswith("READY"):
                break
        else:
            print("the engine exited before READY; see", out / "engine.log")
            return 1
        t_load = time.time() - t0
        hits = look = off = 0
        pp_ms = dec_ms = 0.0
        nc = min(a.chunks, n_chunk) if a.chunks > 0 else n_chunk
        t1 = time.time()
        for k in range(nc):
            ids = ",".join(str(t) for t in chunks[k][:first + 1])
            eng.stdin.write(f"GEN {n_ctx - 1 - first} ppl={k} ppl_t={a.t} {ids}\n")
            eng.stdin.flush()
            for line in eng.stdout:
                if line.startswith("ERR"):
                    print(f"chunk {k}: {line.strip()}")
                    return 1
                if line.startswith("DONE"):
                    f = line.split()
                    pp_ms += float(f[3]); dec_ms += float(f[4])
                    if f[8] != "0":
                        print(f"chunk {k}: {f[8]} prompt tokens reused from an earlier request")
                    if len(f) > 10:
                        hits += int(f[9]); look += int(f[10])
                    if len(f) > 15:
                        off += int(f[15])
                    break
            else:
                print(f"chunk {k}: the engine exited; see", out / "engine.log")
                return 1
            if a.progress and (k + 1) % a.progress == 0:
                print(f"  {k + 1}/{nc} chunks, {time.time() - t1:.0f} s", flush=True)
        t_run = time.time() - t1
        eng.stdin.write("QUIT\n")
        eng.stdin.flush()
    finally:
        try:
            eng.wait(timeout=60)
        except subprocess.TimeoutExpired:
            eng.kill()
    extra = {"T": a.t, "load s": f"{t_load:.0f}", "run s": f"{t_run:.1f}",
             "prompt ms/chunk": f"{pp_ms / nc:.0f}", "decode ms/chunk": f"{dec_ms / nc:.0f}",
             "decode routed experts": f"{look + off}: VRAM hits {100.0 * hits / max(1, look + off):.1f}%, "
                                      f"peer GPU/PCIe {100.0 * off / max(1, look + off):.1f}%, "
                                      f"CPU {100.0 * (look - hits) / max(1, look + off):.1f}%"}
    rep = summary(str(rows), extra)
    (out / "report.txt").write_text(f"engine {a.engine_bin}\nargs {' '.join(args)}\n{rep}\n")
    print(rep)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", help="the .basalt file")
    ap.add_argument("--engine-bin", default=str(ENGINE_BIN) if ENGINE_BIN.is_file() else None,
                    help="the engine binary (default: bin/basalt in the repo)")
    ap.add_argument("--base")
    ap.add_argument("--out")
    ap.add_argument("--chunks", type=int, default=0, help="the first N chunks (0: all)")
    ap.add_argument("--t", type=int, default=3, help="tokens per verify window")
    ap.add_argument("--progress", type=int, default=10, help="a progress line every N chunks (0: none)")
    ap.add_argument("--report", help="print the summary of an existing rows.tsv and exit")
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"), help="compare two rows.tsv files row by row and exit")
    a, extra = ap.parse_known_args()
    a.engine_args = extra
    if a.diff:
        print(diff(*a.diff))
        return 0
    if a.report:
        print(summary(a.report))
        return 0
    if not (a.model and a.base and a.out):
        ap.error("--model, --base and --out are required")
    if not a.engine_bin:
        ap.error("no engine binary: pass --engine-bin")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())

# ===== file: tools/serve_soak.py =====
#!/usr/bin/env python3
"""Concurrent-stream load for a running basalt server: a fixed set of streams on different topics (`topics`), or
random OpenAI/Anthropic streaming traffic with cancels, near-limit and over-long prompts and images (`soak`).

  serve_soak.py BASE_URL topics --secs 300 --streams 4
  serve_soak.py BASE_URL soak --secs 1800 --pgid PID --out soak.jsonl [--images]

Both print a time series of the total tokens/s and the failures; `soak` also samples /slots, GPU memory and the
resident memory of the server's process group, and flags requests that stop producing."""
import argparse, base64, http.client, json, os, random, struct, subprocess, sys, threading, time, urllib.parse, urllib.request, zlib

TOPICS = {
    "code": ["Write a complete Python module that implements an LRU cache, a rate limiter and a retry decorator, with docstrings and unit tests.",
             "Write a C++17 program with a thread pool, a lock-free queue and a small benchmark, explaining each part in comments.",
             "Implement a red-black tree in Rust with insertion, deletion and an iterator, and write tests."],
    "prose": ["Write a long, detailed essay about the history of the printing press and its effect on European society.",
              "Write a detailed explanation of how the immune system learns from vaccines, for an educated reader.",
              "Write a long narrative about a lighthouse keeper during a winter storm."],
    "json": ["Produce a JSON array of 60 realistic customer records with id, name, email, address (street, city, postcode), orders (list of items with sku, qty, price) and a notes field. Output JSON only.",
             "Produce a JSON document describing a fictional company's org chart with 40 employees, nested teams, roles, skills and projects. Output JSON only."],
    "math": ["Solve step by step: find all integer solutions of x^2 - 7y^2 = 1 with x below 10000, then prove the pattern, then compute the sum of the first twenty fundamental solutions.",
             "Derive the closed form of the sum of k^3 for k=1..n, then use induction to prove it, then evaluate several related sums step by step.",
             "Work through twelve different definite integrals step by step, with the substitutions explained."],
}
FILLER = ("The committee reviewed the quarterly figures and noted that several regional offices had exceeded their targets, "
          "while others reported delays caused by supply chain disruptions, staffing changes and unusually severe weather. ")


def http_json(base, path, body=None, timeout=30):
    u = urllib.parse.urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    c.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None,
              {"Content-Type": "application/json", "x-api-key": "k", "anthropic-version": "2023-06-01"})
    r = c.getresponse()
    data = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data


_SERVED = {}


def served_model(base):
    """The model name the server lists; requests must name it."""
    if base not in _SERVED:
        _SERVED[base] = http_json(base, "/v1/models")[1]["data"][0]["id"]
    return _SERVED[base]


def png(w=96, h=96, seed=0):
    rnd = random.Random(seed)
    rows = b"".join(b"\x00" + bytes(((x * 3 + seed * 40) & 255, (y * 3) & 255, rnd.randrange(256)) [i % 3] for x in range(w) for i in range(3)) for y in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def message(kind, text, image=None):
    if image is None:
        return {"role": "user", "content": text}
    b64 = base64.b64encode(image).decode()
    if kind == "openai":
        return {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                                            {"type": "text", "text": text}]}
    return {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                                        {"type": "text", "text": text}]}


class Req:
    def __init__(self, kind, tag):
        self.kind, self.tag = kind, tag
        self.t0 = time.time()
        self.status = None
        self.tokens = 0
        self.chunks = []        # arrival time of every text chunk
        self.last = self.t0
        self.err = None
        self.done = False
        self.cancelled = False
        self.expect = 200


def stream(base, req, text, max_tokens, cancel_after=None, image=None, stop=None):
    """Run one streaming request; fills `req` as it goes."""
    u = urllib.parse.urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=900)
    try:
        if req.kind == "openai":
            path = "/v1/chat/completions"
            body = {"model": served_model(base), "messages": [message("openai", text, image)], "max_tokens": max_tokens, "temperature": 0.7,
                    "stream": True, "stream_options": {"include_usage": True}, "reasoning_effort": "none"}
        else:
            path = "/v1/messages"
            body = {"model": served_model(base), "max_tokens": max_tokens, "temperature": 0.7, "stream": True,
                    "messages": [message("anthropic", text, image)]}
        conn.request("POST", path, json.dumps(body),
                     {"Content-Type": "application/json", "x-api-key": "k", "anthropic-version": "2023-06-01"})
        r = conn.getresponse()
        req.status = r.status
        if r.status != 200:
            req.err = r.read().decode()[:200]
            return
        usage = None
        for raw in r:
            if stop is not None and stop.is_set():
                req.cancelled = True
                return
            line = raw.decode().rstrip("\n")
            if not line.startswith("data:"):
                continue
            d = line[5:].strip()
            if d == "[DONE]":
                break
            try:
                j = json.loads(d)
            except ValueError:
                continue
            piece = None
            if req.kind == "openai":
                if j.get("error"):
                    req.err = str(j["error"])[:200]
                if j.get("usage"):
                    usage = j["usage"].get("completion_tokens")
                for c in j.get("choices", []):
                    piece = (c.get("delta") or {}).get("content") or (c.get("delta") or {}).get("reasoning_content")
            else:
                if j.get("type") == "error":
                    req.err = str(j)[:200]
                if j.get("type") == "content_block_delta":
                    dl = j.get("delta") or {}
                    piece = dl.get("text") or dl.get("thinking")
                if j.get("type") == "message_delta":
                    usage = (j.get("usage") or {}).get("output_tokens")
            if piece:
                req.last = time.time()
                req.chunks.append(req.last)
                if cancel_after and len(req.chunks) >= cancel_after:
                    req.cancelled = True
                    return
        req.tokens = usage if usage is not None else len(req.chunks)
    except Exception as e:  # noqa: BLE001
        req.err = f"{type(e).__name__}: {e}"
    finally:
        conn.close()
        req.done = True


def series(reqs, t0, t1, bin_s):
    """Total tokens/s per bin; each request's tokens are spread over its chunk arrivals."""
    nb = max(1, int((t1 - t0) / bin_s))
    acc = [0.0] * nb
    for q in reqs:
        if not q.chunks or not q.tokens:
            continue
        per = q.tokens / len(q.chunks)
        for t in q.chunks:
            b = int((t - t0) / bin_s)
            if 0 <= b < nb:
                acc[b] += per
    return [a / bin_s for a in acc]


def run_topics(a):
    names = list(TOPICS)
    reqs, lock, stop = [], threading.Lock(), threading.Event()
    t0 = time.time()

    def worker(i):
        topic = names[i % len(names)]
        k = 0
        while not stop.is_set():
            q = Req("openai" if (i + k) % 2 == 0 else "anthropic", f"{topic}{i}")
            with lock:
                reqs.append(q)
            stream(a.base, q, TOPICS[topic][k % len(TOPICS[topic])], a.max_tokens, stop=stop)
            k += 1
    ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(a.streams)]
    for t in ths:
        t.start()
    time.sleep(a.secs)
    stop.set()
    t1 = time.time()
    for t in ths:
        t.join(120)
    # the streams are cut at the deadline: count what they produced
    for q in reqs:
        if q.cancelled and not q.tokens:
            q.tokens = len(q.chunks)
    ser = series(reqs, t0, t1, a.bin)
    print("bins of", a.bin, "s, total tok/s:", [round(x) for x in ser])
    skip = int(60 / a.bin)   # the first minute holds the admissions
    steady = ser[skip:] or ser
    print(f"streams {a.streams}: total tok/s mean {sum(steady) / len(steady):.1f} (after the first minute), "
          f"min {min(steady):.0f} max {max(steady):.0f}")
    errs = [(q.tag, q.status, q.err) for q in reqs if q.err or (q.status not in (None, 200))]
    print("requests", len(reqs), "errors", errs[:10])
    return 0 if not errs else 1


def count_tokens(base, text):
    st, j = http_json(base, "/v1/messages/count_tokens", {"model": served_model(base), "messages": [{"role": "user", "content": text}]})
    return j.get("input_tokens") if st == 200 and isinstance(j, dict) else None


def long_text(base, target):
    """A prompt of about `target` tokens."""
    per = count_tokens(base, FILLER * 50) or 1500
    per = per / 50.0
    n = max(1, int(target / per))
    return FILLER * n


def sample_system(pgid):
    out = {}
    try:
        g = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True,
                           text=True, timeout=20).stdout.split()
        out["gpu_mib"] = [int(x) for x in g]
    except Exception:  # noqa: BLE001
        pass
    if pgid:
        try:
            r = subprocess.run(["ps", "-o", "rss=", "-g", str(pgid)], capture_output=True, text=True, timeout=20).stdout.split()
            out["rss_mib"] = round(sum(int(x) for x in r) / 1024)
        except Exception:  # noqa: BLE001
            pass
    try:
        with open("/proc/meminfo") as f:
            m = {l.split(":")[0]: int(l.split()[1]) for l in f}
        out["mem_avail_mib"] = m["MemAvailable"] // 1024
        out["swap_used_mib"] = (m["SwapTotal"] - m["SwapFree"]) // 1024
    except Exception:  # noqa: BLE001
        pass
    return out


def is_bad(q):
    """A finished request that did not end as planned (an expected 400 is fine, a cancelled stream is fine)."""
    if q.cancelled:
        return False
    return q.status != q.expect or (q.status == 200 and bool(q.err))


def run_soak(a):
    rnd = random.Random(a.seed)
    allp = [p for v in TOPICS.values() for p in v]
    reqs, lock, stop = [], threading.Lock(), threading.Event()
    t0 = time.time()
    log = open(a.out, "w") if a.out else None
    st, props = http_json(a.base, "/slots")
    n_slots = len(props) if isinstance(props, list) else 4
    slot_ctx = props[0].get("n_ctx") if isinstance(props, list) and props else None
    print("slots", n_slots, "per-slot context", slot_ctx, flush=True)
    ctx = slot_ctx or 65536
    img = png(seed=1)
    target = [rnd.randint(2, 8)]

    def launch(kind_pick):
        kind = rnd.choice(["openai", "anthropic"])
        q = Req(kind, kind_pick)
        text, mt, cancel, image = rnd.choice(allp), rnd.choice([64, 200, 600, 1500]), None, None
        if kind_pick == "cancel":
            cancel = rnd.randint(3, 120)
            mt = 1500
        elif kind_pick == "near":
            text = long_text(a.base, ctx - 1800) + "\n\nSummarize the passage above in two sentences."
            mt = 200
        elif kind_pick == "over":
            text = long_text(a.base, ctx + 4000) + "\n\nSummarize."
            q.expect = 400
        elif kind_pick == "image":
            image, text, mt = img, "Describe this image in a few sentences.", 200
        with lock:
            reqs.append(q)
        threading.Thread(target=stream, args=(a.base, q, text, mt, cancel, image, stop), daemon=True).start()

    def pick():
        r = rnd.random()
        if r < 0.10: return "cancel"
        if r < 0.13: return "near"
        if r < 0.17: return "over"
        if a.images and r < 0.22: return "image"
        return "chat"

    def monitor():
        while not stop.is_set():
            rec = {"t": round(time.time() - t0)}
            try:
                st_, sl = http_json(a.base, "/slots", timeout=20)
                rec["slots_busy"] = sum(1 for s in sl if s.get("is_processing")) if isinstance(sl, list) else None
            except Exception as e:  # noqa: BLE001
                rec["slots_err"] = str(e)[:80]
            rec.update(sample_system(a.pgid))
            with lock:
                live = [q for q in reqs if not q.done]
                rec["in_flight"] = len(live)
                rec["done"] = len(reqs) - len(live)
                rec["bad"] = sum(1 for q in reqs if q.done and is_bad(q))
                now = time.time()
                rec["stuck"] = [q.tag for q in live if now - q.last > a.stuck_s and q.chunks]
                rec["tokens"] = sum(q.tokens for q in reqs)
            if log:
                log.write(json.dumps(rec) + "\n")
                log.flush()
            print(json.dumps(rec), flush=True)
            time.sleep(a.sample_s)
    threading.Thread(target=monitor, daemon=True).start()
    nlong = [0, 0]
    while time.time() - t0 < a.secs:
        with lock:
            live = sum(1 for q in reqs if not q.done)
        if rnd.random() < 0.02:
            target[0] = rnd.randint(2, 8)
        if live < target[0]:
            k = pick()
            if k in ("near", "over") and nlong[k == "over"] >= (a.secs // 600 + 1) * 2:
                k = "chat"
            if k in ("near", "over"):
                nlong[k == "over"] += 1
            launch(k)
        time.sleep(rnd.uniform(0.2, 2.0))
    # drain: no new requests; the rest finish (or are cut after a grace period)
    grace = time.time() + 300
    while time.time() < grace and any(not q.done for q in reqs):
        time.sleep(1)
    stop.set()
    time.sleep(2)
    t1 = time.time()
    by = {}
    for q in reqs:
        key = (q.tag, q.status if not q.cancelled else "cancel", bool(q.err))
        by[key] = by.get(key, 0) + 1
    print("== requests by (kind, status, error):")
    for k, v in sorted(by.items(), key=str):
        print("  ", k, v)
    bad = [(q.tag, q.kind, q.status, q.err) for q in reqs if q.done and is_bad(q)]
    print("unexpected:", len(bad), bad[:10])
    ser = series(reqs, t0, t1, 60)
    print("total tok/s per minute:", [round(x) for x in ser])
    time.sleep(5)
    st, sl = http_json(a.base, "/slots")
    print("final /slots:", json.dumps(sl)[:600])
    print("final system:", sample_system(a.pgid))
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("mode", choices=["topics", "soak"])
    ap.add_argument("--secs", type=int, default=300)
    ap.add_argument("--streams", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=2000)
    ap.add_argument("--bin", type=int, default=30)
    ap.add_argument("--pgid", type=int, default=0, help="soak: process group of the server, for its resident memory")
    ap.add_argument("--out", default="", help="soak: a jsonl of the samples")
    ap.add_argument("--images", action="store_true")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--sample-s", type=int, default=15)
    ap.add_argument("--stuck-s", type=int, default=90, help="soak: seconds without a token before a stream counts as stuck")
    a = ap.parse_args()
    sys.exit(run_topics(a) if a.mode == "topics" else run_soak(a))


if __name__ == "__main__":
    main()

# ===== file: tools/tokenizer.py =====
"""The reference byte-level BPE tokenizer, read back from the model's `tokenizer/` directory.

The GGUF metadata carries the whole tokenizer (gpt2 model, tokens, merges, token types, `qwen35` pre-tokenizer,
chat template), so no weights are needed. Any byte maps to a printable character, so there is no UNK token, and
`decode(encode(s)) == s` is the check that catches byte-offset, merge-order and pre-tokenizer mistakes.
"""
from __future__ import annotations

import json
import pathlib
import sys

import regex

# ------------------------------------------------------------------ byte <-> unicode (GPT-2 byte-level BPE)
def bytes_to_unicode() -> dict[int, str]:
    """The GPT-2 byte encoder: bytes 33..126, 161..172, 174..255 map to themselves, the other 68 are shifted to
    256+n in order; a wrong order is invisible in the ids."""
    bs = (list(range(0x21, 0x7F)) + list(range(0xA1, 0xAD)) + list(range(0xAE, 0x100)))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, (chr(c) for c in cs)))


BYTE_TO_UNICODE = bytes_to_unicode()
UNICODE_TO_BYTE = {v: k for k, v in BYTE_TO_UNICODE.items()}

# The `qwen35` pre-tokenizer, transcribed from llama.cpp's LLAMA_VOCAB_PRE_TYPE_QWEN35. Unlike the QWEN3 pattern it
# keeps `\p{M}` in the letter class and out of the punctuation class; without that a combining mark is swallowed into
# the next punctuation run. Round trips cannot see the difference; only an oracle comparison can.
QWEN35_PATTERN = (
    r"(?:'[sS]|'[tT]|'[rR][eE]|'[vV][eE]|'[mM]|'[lL][lL]|'[dD])"
    r"|[^\r\n\p{L}\p{N}]?[\p{L}\p{M}]+"
    r"|\p{N}"
    r"| ?[^\s\p{L}\p{M}\p{N}]+[\r\n]*"
    r"|\s*[\r\n]+"
    r"|\s+(?!\S)"
    r"|\s+"
)

# The `joyai-llm` pre-tokenizer (llama.cpp LLAMA_VOCAB_PRE_TYPE_JOYAI_LLM): the regexes apply in turn, each one
# splitting the pieces the previous left; text a regex does not match stays a piece for the next.
JOYAI_PATTERNS = [
    r"\p{N}{1,3}",
    "[\u4e00-\u9fa5\u3040-\u309f\u30a0-\u30ff]+",
    r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~][A-Za-z]+|[^\r\n\p{L}\p{P}\p{S}]?[\p{L}\p{M}]+"
    r"| ?[\p{P}\p{S}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+",
]
PRE_PATTERNS = {"joyai-llm": JOYAI_PATTERNS}


class Tokenizer:
    def __init__(self, tokens: list[str], merges: list[str], token_types: list[int] | None = None,
                 pre: str = "qwen35", special_ids: dict[str, int] | None = None):
        self.tokens = tokens
        self.pre = pre
        self.token_types = token_types
        self.special_ids = special_ids or {}
        self.ids = {t: i for i, t in enumerate(tokens)}
        if len(self.ids) != len(tokens):
            raise ValueError("vocabulary has duplicate tokens: %d entries, %d unique"
                             % (len(tokens), len(self.ids)))
        # merges as (left, right) -> rank; a non-total order or an unknown token would silently change ids, so check
        self.ranks: dict[tuple[str, str], int] = {}
        for i, m in enumerate(merges):
            parts = m.split(" ")
            if len(parts) != 2:
                raise ValueError("merge %d is not a pair: %r" % (i, m))
            if parts[0] not in self.ids or parts[1] not in self.ids:
                raise ValueError("merge %d names a token outside the vocabulary: %r" % (i, m))
            self.ranks[(parts[0], parts[1])] = i
        self._res = [regex.compile(p) for p in PRE_PATTERNS.get(pre, [QWEN35_PATTERN])]

        # Literals matched directly instead of through BPE (GGUF token types):
        #   * type 4 (USER_DEFINED: `<think>`, `<tool_call>`, ...) always matches, with or without parse_special;
        #   * type 3 (CONTROL: `<|im_start|>`, `<|im_end|>`, ...) matches only when parse_special is set.
        # This mirrors llama.cpp; treating them alike changes chat prompts.
        self.special_tokens: dict[str, int] = {}
        if token_types:
            for i, ty in enumerate(token_types):
                if ty in (3, 4):
                    self.special_tokens[tokens[i]] = i
        always = [t for t, i in self.special_tokens.items() if token_types and token_types[i] == 4]
        # longest literal first so `<|im_end|>` cannot match a shorter prefix; regex.escape for metacharacters
        self._always_re = self._alt(always)
        self._special_re = self._alt(list(self.special_tokens))

    @staticmethod
    def _alt(literals: list[str]):
        if not literals:
            return None
        return regex.compile("|".join(regex.escape(s) for s in sorted(literals, key=len, reverse=True)))

    # -------------------------------------------------------------- constructors
    @classmethod
    def from_gguf(cls, path) -> "Tokenizer":
        return cls.from_metadata(_gguf_metadata(path))

    @classmethod
    def from_metadata(cls, md: dict) -> "Tokenizer":
        """From GGUF tokenizer metadata (`tokenizer.ggml.*`), as a GGUF file or a converter carries it."""
        need = ["tokenizer.ggml.tokens", "tokenizer.ggml.merges"]
        missing = [k for k in need if k not in md]
        if missing:
            raise ValueError("GGUF is missing %s; it does not carry a tokenizer" % ", ".join(missing))
        model = md.get("tokenizer.ggml.model")
        if model != "gpt2":
            raise ValueError("expected a byte-level BPE tokenizer (model 'gpt2'), got %r" % model)
        special = {k: int(md[k]) for k in md
                   if k.startswith("tokenizer.ggml.") and k.endswith("_token_id")}
        return cls(list(md["tokenizer.ggml.tokens"]), list(md["tokenizer.ggml.merges"]),
                   list(md.get("tokenizer.ggml.token_type") or []) or None,
                   md.get("tokenizer.ggml.pre", "qwen35"), special)

    # -------------------------------------------------------------- the algorithm
    def _bpe(self, word: str) -> list[str]:
        """Merge `word` (already byte-mapped) by lowest rank first, not left to right; long words go to _bpe_heap."""
        if len(word) > self.HEAP_MIN:
            return self._bpe_heap(word)
        ranks_get = self.ranks.get      # read once: the scan below works on a local, not on `self` per symbol pair
        parts = list(word)
        while len(parts) > 1:
            best, best_rank = None, None
            for i in range(len(parts) - 1):
                r = ranks_get((parts[i], parts[i + 1]))
                if r is not None and (best_rank is None or r < best_rank):
                    best, best_rank = i, r
            if best is None:
                break
            parts[best:best + 2] = [parts[best] + parts[best + 1]]
        return parts

    HEAP_MIN = 64       # longer words use the heap

    def _bpe_heap(self, word: str) -> list[str]:
        """_bpe's result in O(n log n): a linked list of symbols and a heap of ranked pairs keyed (rank, position);
        stale pairs are skipped."""
        import heapq
        parts = list(word)
        n = len(parts)
        nxt = list(range(1, n)) + [-1]
        prv = list(range(-1, n - 1))
        ranks = self.ranks
        heap = []
        for i in range(n - 1):
            r = ranks.get((parts[i], parts[i + 1]))
            if r is not None:
                heap.append((r, i, parts[i], parts[i + 1]))
        heapq.heapify(heap)
        while heap:
            r, i, left, right = heapq.heappop(heap)
            j = nxt[i]
            if parts[i] != left or j < 0 or parts[j] != right:
                continue                                # stale: a symbol has merged since
            parts[i] = left + right
            parts[j] = None                             # j merged into i
            k = nxt[j]
            nxt[i] = k
            if k >= 0:
                prv[k] = i
                r2 = ranks.get((parts[i], parts[k]))
                if r2 is not None:
                    heapq.heappush(heap, (r2, i, parts[i], parts[k]))
            p = prv[i]
            if p >= 0:
                r2 = ranks.get((parts[p], parts[i]))
                if r2 is not None:
                    heapq.heappush(heap, (r2, p, parts[p], parts[i]))
        return [s for s in parts if s is not None]

    PIECE_CACHE_MAX = 200_000    # pre-tokenizer pieces remembered; bounds the memo's memory

    def _split(self, text: str) -> list[str]:
        if len(self._res) == 1:
            return self._res[0].findall(text)
        pieces = [text]
        for rx in self._res:
            nxt = []
            for p in pieces:
                pos = 0
                for m in rx.finditer(p):
                    if m.start() > pos:
                        nxt.append(p[pos:m.start()])
                    if m.end() > m.start():
                        nxt.append(m.group(0))
                    pos = m.end()
                if pos < len(p):
                    nxt.append(p[pos:])
            pieces = nxt
        return pieces

    def _encode_plain(self, text: str) -> list[int]:
        out: list[int] = []
        # A piece's ids depend on the piece alone, so repeated pieces (most of a resent conversation) are looked up
        # instead of merged again. Same ids as running _bpe each time. Idea from Strata f346dc6 (MIT).
        cache = self.__dict__.setdefault("_piece_ids", {})
        for piece in self._split(text):
            got = cache.get(piece)
            if got is None:
                mapped = "".join(BYTE_TO_UNICODE[b] for b in piece.encode("utf-8"))
                got = []
                for tok in self._bpe(mapped):
                    i = self.ids.get(tok)
                    if i is None:
                        raise KeyError("BPE produced a token outside the vocabulary: %r" % tok)
                    got.append(i)
                if len(cache) < self.PIECE_CACHE_MAX:
                    cache[piece] = got
            out.extend(got)
        return out

    def _encode_matching(self, text: str, pat, plain=()) -> list[int]:
        """Encode `text`, emitting literal `pat` matches as single tokens and BPE-ing the rest. The split is on raw
        text; matches inside `plain` spans are left to the text around them."""
        if pat is None:
            return self._encode_plain(text)
        out: list[int] = []
        pos = 0
        for m in pat.finditer(text):
            if plain and any(a <= m.start() < b for a, b in plain):
                continue
            if m.start() > pos:
                out.extend(self._encode_plain(text[pos:m.start()]))
            out.append(self.special_tokens[m.group(0)])
            pos = m.end()
        if pos < len(text):
            out.extend(self._encode_plain(text[pos:]))
        return out

    def encode(self, text: str, parse_special: bool = False, plain=()) -> list[int]:
        """Tokenize `text`. `parse_special` governs only type-3 CONTROL literals; type-4 USER_DEFINED ones always match.
        `plain`: (start, end) spans that stay ordinary text even where they spell a literal."""
        return self._encode_matching(text, self._special_re if parse_special else self._always_re, plain)

    def token_bytes(self, i: int) -> bytes:
        """The raw bytes of one token (a multi-byte character can be split across tokens)."""
        cache = self.__dict__.setdefault("_bytes_cache", {})
        b = cache.get(i)
        if b is None:
            if i < 0 or i >= len(self.tokens):
                raise IndexError("token id %d is outside the vocabulary (%d)" % (i, len(self.tokens)))
            raw = bytearray()
            for ch in self.tokens[i]:
                v = UNICODE_TO_BYTE.get(ch)
                if v is None:
                    if self.special_tokens.get(self.tokens[i]) == i:    # a special's text is the literal itself
                        raw = bytearray(self.tokens[i].encode("utf-8"))
                        break
                    raise KeyError("token %d contains a character outside the byte alphabet: %r" % (i, ch))
                raw.append(v)
            b = cache[i] = bytes(raw)
        return b

    def decode(self, ids: list[int], errors: str = "replace") -> str:
        return b"".join(self.token_bytes(i) for i in ids).decode("utf-8", errors=errors)


# ------------------------------------------------------------------ the pack's tokenizer/ directory
def _gguf_metadata(path) -> dict:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from gguf_reader import GGUFFile
    return GGUFFile(pathlib.Path(path)).metadata


def extract(gguf_path, out_dir) -> dict:
    """Write the tokenizer into `<out_dir>/tokenizer/` so the engine never opens the weight shards for it."""
    return extract_metadata(_gguf_metadata(gguf_path), out_dir)


def extract_metadata(md: dict, out_dir) -> dict:
    """`extract` from GGUF tokenizer metadata held in a dict."""
    out = pathlib.Path(out_dir) / "tokenizer"
    out.mkdir(parents=True, exist_ok=True)
    tk = Tokenizer.from_metadata(md)
    cfg = {
        "model": "gpt2",
        "pre": tk.pre,
        "vocab_size": len(tk.tokens),
        "n_merges": len(tk.ranks),
        "special_ids": tk.special_ids,
        "add_bos_token": False,
        # shipped rather than re-derived by readers, so a port cannot lose the `\p{M}` classes
    }
    if tk.pre in PRE_PATTERNS:
        cfg["pre_patterns"] = PRE_PATTERNS[tk.pre]
        cfg["pre_pattern_source"] = "llama.cpp src/llama-vocab.cpp (LLAMA_VOCAB_PRE_TYPE_JOYAI_LLM), applied in order"
    else:
        cfg["pre_pattern"] = QWEN35_PATTERN
        cfg["pre_pattern_source"] = "llama.cpp src/llama-vocab.cpp (LLAMA_VOCAB_PRE_TYPE_QWEN35)"
    (out / "vocab.json").write_text(json.dumps(tk.ids, ensure_ascii=False), encoding="utf-8")
    (out / "merges.txt").write_text("\n".join("%s %s" % k for k, _ in
                                              sorted(tk.ranks.items(), key=lambda kv: kv[1])), encoding="utf-8",
                                    newline="\n")
    (out / "token_type.json").write_text(json.dumps(tk.token_types), encoding="utf-8")
    (out / "tokenizer.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8",
                                        newline="\n")
    # fine-tunes change the chat template, so the model's own is shipped
    tpl = md.get("tokenizer.chat_template")
    if tpl:
        (out / "chat_template.jinja").write_text(tpl, encoding="utf-8", newline="\n")
    return cfg


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--check", action="store_true", help="round-trip a corpus and report")
    args = ap.parse_args()
    cfg = extract(args.gguf, args.out)
    print("tokenizer/: vocab %d, merges %d, pre %s, specials %s"
          % (cfg["vocab_size"], cfg["n_merges"], cfg["pre"], cfg["special_ids"]))
    if args.check:
        tk = Tokenizer.from_gguf(args.gguf)
        corpus = ["", "Hello, world!", "  leading and trailing  ", "a\n\n\nb",
                  "def f(x):\n\treturn x  # comment\n", "\u4f60\u597d\uff0c\u4e16\u754c", "\u0645\u0631\u062d\u0628\u0627",
                  "\U0001f600\U0001f680\U0001f1fa\U0001f1f8", "e\u0301\u0301 combining", "\x00\x01\x7f control",
                  "\u00a0non-breaking\u00a0space", "1234567890", "MixedCASE_and-dashes", "\r\n\r\n", "\u2028\u2029",
                  "x" * 5000]
        bad = 0
        for s in corpus:
            ids = tk.encode(s)
            back = tk.decode(ids)
            if back != s:
                print("  *** ROUND TRIP FAILED *** %r -> %d ids -> %r" % (s[:40], len(ids), back[:40]))
                bad += 1
        print("round trip: %d strings, %d failed" % (len(corpus), bad))
        return 0 if bad == 0 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# ===== file: tools/zero_doorbell_check.py =====
#!/usr/bin/env python3
"""Batch windows on the zero-doorbell graph: a server with --parallel and --force-zero-doorbell (every decode window
on the graph a fully resident pack runs, whatever the residency) under concurrent streams, some cancelled mid-stream.
The output is not checked (the experts not in VRAM drop out); every request must end and the engine must not stop.

  zero_doorbell_check.py --engine-bin build/basalt --model PACK.basalt [--secs 120] [--streams 4] [server flags...]

Exit 0 when every request ended and the engine never stopped, 1 otherwise."""
import argparse, http.client, json, os, random, subprocess, sys, threading, time

PROMPTS = ["Write a short story about a lighthouse keeper.", "Explain how a hash map works, with a Python example.",
           "List ten facts about volcanoes, one per line.", "Describe the water cycle to a ten year old."]


def get(port, path, timeout=5):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def stream(port, model, rng, stats, lock, timeout=120):
    body = {"model": model, "stream": True, "max_tokens": rng.choice([32, 128, 400]),
            "temperature": rng.choice([0, 0.7]), "messages": [{"role": "user", "content": rng.choice(PROMPTS)}]}
    cancel_at = rng.choice([None, None, 4, 30])
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)   # a hung window ends the request here
    kind = "ok"
    try:
        c.request("POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"})
        r = c.getresponse()
        if r.status != 200:
            kind = f"HTTP {r.status}: {r.read()[:200]!r}"
        else:
            n = 0
            for line in r:
                if not line.startswith(b"data: ") or b"[DONE]" in line:
                    continue
                if b'"error"' in line:
                    kind = f"stream error: {line[:200]!r}"
                    break
                n += 1
                if cancel_at is not None and n >= cancel_at:
                    kind = "cancel"
                    break
    except Exception as e:   # a timeout is the hang this checks for
        kind = f"exception: {e!r}"
    finally:
        c.close()
    with lock:
        stats[kind if kind in ("ok", "cancel") else "fail"] += 1
        if kind not in ("ok", "cancel"):
            print(kind, flush=True)
    if kind not in ("ok", "cancel"):
        time.sleep(2)   # the server restarts the engine


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine-bin", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=18192)
    ap.add_argument("--secs", type=float, default=120)
    ap.add_argument("--streams", type=int, default=4)
    ap.add_argument("--log", default="zero_doorbell_check.server.log")
    a, extra = ap.parse_known_args()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, "-m", "serve.server", "--model", a.model, "--engine-bin", a.engine_bin, "--host", "127.0.0.1",
           "--port", str(a.port), "--parallel", str(a.streams), "--kv", "int8", *extra, "--force-zero-doorbell"]
    with open(a.log, "w") as log:
        srv = subprocess.Popen(cmd, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    try:
        model = None
        for _ in range(900):
            if srv.poll() is not None:
                print("the server ended before it was ready; see", a.log)
                return 1
            try:
                if get(a.port, "/health")[0] == 200:
                    model = json.loads(get(a.port, "/v1/models")[1])["data"][0]["id"]
                    break
            except OSError:
                pass
            time.sleep(2)
        if model is None:
            print("the server never became ready; see", a.log)
            return 1
        stats, lock = {"ok": 0, "cancel": 0, "fail": 0}, threading.Lock()
        stream(a.port, model, random.Random(0), stats, lock, timeout=1800)   # the engine loads on the first request
        deadline = time.time() + a.secs

        def loop(i):
            rng = random.Random(i + 1)
            while time.time() < deadline:
                stream(a.port, model, rng, stats, lock)

        ts = [threading.Thread(target=loop, args=(i,)) for i in range(a.streams)]
        for t in ts: t.start()
        for t in ts: t.join()
    finally:
        srv.terminate()
        try:
            srv.wait(30)
        except subprocess.TimeoutExpired:
            srv.kill()
    text = open(a.log, errors="replace").read()
    stops = sum(text.count(w) for w in ("stopped unexpectedly", "timed out", "no progress for"))
    print(f"requests: {stats['ok']} finished, {stats['cancel']} cancelled, {stats['fail']} failed; engine stops: {stops}")
    return 0 if stats["fail"] == 0 and stops == 0 and stats["ok"] + stats["cancel"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
