"""Upgrade 6a: vision check engine (model call + robust verdict parsing)."""
from __future__ import annotations

import base64
import json
import re
import urllib.request
from typing import Callable, Optional

PROMPT = """You are checking a screenshot of an app against its spec.

Spec:
{spec}

Go through each requirement in the spec one by one and say whether it is clearly visible in the screenshot.
Then answer on the last line with JSON only, exactly like:
{{"pass": true, "issues": [], "confidence": 0.9}}
"pass" is true only if every requirement is visible. List each missing or wrong thing in "issues"."""


def build_request(png: bytes, spec: str, model: str = "local", max_tokens: int = 4096) -> dict:
    b64 = base64.b64encode(png).decode()
    return {"model": model, "temperature": 0.2, "max_tokens": max_tokens, "messages": [{
        "role": "user", "content": [
            {"type": "text", "text": PROMPT.format(spec=spec.strip())},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]}


def parse_verdict(text: str) -> Optional[dict]:
    """Find the last JSON object with a 'pass' key. None if there isn't one."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    cands = re.findall(r"\{[^{}]*\}", text)
    for c in reversed(cands):
        try:
            d = json.loads(c)
        except ValueError:
            try:
                d = json.loads(c.replace("True", "true").replace("False", "false").replace("'", '"'))
            except ValueError:
                continue
        if isinstance(d, dict) and isinstance(d.get("pass"), bool):
            issues = d.get("issues") or []
            if not isinstance(issues, list):
                issues = [str(issues)]
            conf = d.get("confidence")
            return {"pass": d["pass"], "issues": [str(i) for i in issues],
                    "confidence": float(conf) if isinstance(conf, (int, float)) else None}
    return None


def ask_once(png: bytes, spec: str, call: Callable[[dict], str], retries: int = 1) -> Optional[dict]:
    for _ in range(retries + 1):
        v = parse_verdict(call(build_request(png, spec)))
        if v is not None:
            return v
    return None


def check(png: bytes, spec: str, call: Callable[[dict], str], votes: int = 2) -> dict:
    """Ask `votes` times; pass only if all pass; disagreement or no JSON = inconclusive."""
    res = [ask_once(png, spec, call) for _ in range(votes)]
    if any(r is None for r in res):
        return {"verdict": "inconclusive", "issues": ["The model didn't give a clear answer."], "votes": res}
    passes = [r["pass"] for r in res]
    if all(passes):
        return {"verdict": "pass", "issues": [], "votes": res}
    if not any(passes):
        issues = []
        for r in res:
            for i in r["issues"]:
                if i not in issues:
                    issues.append(i)
        return {"verdict": "fail", "issues": issues, "votes": res}
    return {"verdict": "inconclusive", "issues": ["The two looks disagreed."], "votes": res}


def to_check_result(name: str, verdict: dict, mode: str = "advisory", screenshots=()) -> dict:
    ok = verdict["verdict"] == "pass"
    return {"name": name, "cmd": "vision", "type": "vision", "mode": mode, "ok": ok,
            "exit_code": 0 if ok else 1, "verdict": verdict["verdict"], "issues": verdict["issues"],
            "screenshots": list(screenshots), "passed": None, "failed": None, "skipped": None}


def http_call(url: str = "http://127.0.0.1:8080/v1/chat/completions", timeout: int = 600):
    def _call(body: dict) -> str:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read())
        return j["choices"][0]["message"].get("content") or ""
    return _call
