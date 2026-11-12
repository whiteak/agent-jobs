"""Offline voice for Agent Jobs: speech-to-text (faster-whisper via worker), text-to-speech (Piper),
spoken job summaries and the hands-free command parser.

Framework-agnostic: jobboard.py only needs to call
    transcribe_bytes(audio_bytes) -> str
    speak_to_file(text) -> Path   (WAV)
    spoken_summary(status, title, report_md) -> str
    parse_command(text) -> dict | None
Everything is local. Nothing here touches the GPU or the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

VOICE_DIR = Path(os.environ.get("AGENT_VOICE_DIR", Path.home() / "agent-sandbox" / "voice"))
VENV = next((VOICE_DIR / d for d in (".venv", "venv") if (VOICE_DIR / d / "bin" / "python").exists()), VOICE_DIR / ".venv")
VENV_PY = VENV / "bin" / "python"
WHISPER_MODEL = VOICE_DIR / "models" / "whisper-small.en"
PIPER_BIN = VENV / "bin" / "piper"
PIPER_VOICE = next(iter(sorted((VOICE_DIR / "models" / "piper").rglob("en_US-lessac-medium.onnx"))),
                   VOICE_DIR / "models" / "piper" / "en_US-lessac-medium.onnx")
CACHE_DIR = Path(tempfile.gettempdir()) / "agent-jobs-tts"
CACHE_MAX = 50
IDLE_SECS = 600
WORKER = Path(__file__).with_name("voice_worker.py")


class VoiceError(RuntimeError):
    """Raised with a message that is safe to show the user."""


def _offline_env() -> dict:
    env = dict(os.environ)
    env.update(CUDA_VISIBLE_DEVICES="", HF_HUB_OFFLINE="1", OMP_NUM_THREADS=env.get("OMP_NUM_THREADS", "4"))
    return env


# ---------------------------------------------------------------- speech to text
class WhisperWorker:
    """Long-lived child process running voice_worker.py in the voice venv.
    Started on first use, stopped after IDLE_SECS without requests (RAM is tight)."""

    def __init__(self, python=VENV_PY, model=WHISPER_MODEL, worker=WORKER, idle_secs=IDLE_SECS):
        self.python, self.model, self.worker, self.idle_secs = Path(python), Path(model), Path(worker), idle_secs
        self.proc: subprocess.Popen | None = None
        self.last_used = 0.0
        self.lock = threading.Lock()
        self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
        self._reaper.start()

    def _start(self):
        if not self.python.exists():
            raise VoiceError(f"Voice is not installed (missing {self.python}).")
        if not self.model.exists():
            raise VoiceError(f"Speech model is missing ({self.model}).")
        self.proc = subprocess.Popen(
            [str(self.python), "-I", str(self.worker), str(self.model)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=_offline_env(),
        )
        ready = self.proc.stdout.readline()
        if not ready or json.loads(ready).get("ready") is not True:
            self.stop()
            raise VoiceError("Speech model failed to load.")

    def transcribe_wav(self, wav_path: str | Path) -> str:
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            self.proc.stdin.write(json.dumps({"wav": str(wav_path)}) + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            self.last_used = time.monotonic()
        if not line:
            self.stop()
            raise VoiceError("Speech worker stopped unexpectedly.")
        reply = json.loads(line)
        if "error" in reply:
            raise VoiceError(f"Transcription failed: {reply['error']}")
        return reply.get("text", "").strip()

    def stop(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
                p.wait(timeout=5)
            except Exception:
                p.kill()

    def _reap_loop(self):
        while True:
            time.sleep(30)
            with self.lock:
                if self.proc and time.monotonic() - self.last_used > self.idle_secs:
                    self.stop()


_worker: WhisperWorker | None = None


def get_worker() -> WhisperWorker:
    global _worker
    if _worker is None:
        _worker = WhisperWorker()
    return _worker


def to_wav16k(src: Path, dst: Path) -> None:
    r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
                        "-ac", "1", "-ar", "16000", str(dst)], capture_output=True, timeout=60)
    if r.returncode != 0:
        raise VoiceError("Could not read the recording (ffmpeg failed).")


def transcribe_bytes(audio: bytes, worker: WhisperWorker | None = None, ffmpeg: str | None = "auto") -> str:
    """ffmpeg is optional: without it (Jason's PC has none) the raw recording goes straight to the
    worker, where faster-whisper decodes and resamples webm/ogg/wav itself with PyAV."""
    if not audio:
        raise VoiceError("Empty recording.")
    if len(audio) > 25 * 1024 * 1024:
        raise VoiceError("Recording too long.")
    if ffmpeg == "auto":
        ffmpeg = shutil.which("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="agent-voice-") as d:
        src, wav = Path(d) / "in.webm", Path(d) / "in.wav"
        src.write_bytes(audio)
        if ffmpeg:
            to_wav16k(src, wav)
        else:
            wav = src
        return (worker or get_worker()).transcribe_wav(wav)


# ---------------------------------------------------------------- text to speech
def _prune_cache(cache_dir: Path, keep: int) -> None:
    files = sorted(cache_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)


def speak_to_file(text: str, cache_dir: Path = CACHE_DIR, keep: int = CACHE_MAX,
                  piper=PIPER_BIN, voice=PIPER_VOICE) -> Path:
    text = clean_markdown(text)[:4000]
    if not text:
        raise VoiceError("Nothing to say.")
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / (hashlib.sha256(text.encode()).hexdigest()[:24] + ".wav")
    if out.exists():
        os.utime(out)
        return out
    if not Path(piper).exists() or not Path(voice).exists():
        raise VoiceError("Talk-back voice is not installed.")
    tmp = out.with_suffix(".part")
    r = subprocess.run([str(piper), "-m", str(voice), "-f", str(tmp)], input=text.encode(),
                       capture_output=True, timeout=120, env=_offline_env())
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise VoiceError("Talk-back failed (piper error).")
    tmp.rename(out)
    _prune_cache(cache_dir, keep)
    return out


# ---------------------------------------------------------------- summaries
_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), " "),          # code blocks
    (re.compile(r"`([^`]*)`"), r"\1"),              # inline code
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),     # images
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),  # links
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),   # headings
    (re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s*", re.M), ""),  # checkboxes
    (re.compile(r"^\s*([-*+]|\d+\.)\s+", re.M), ""),      # list markers
    (re.compile(r"^\s*>\s?", re.M), ""),            # quotes
    (re.compile(r"^\s*\|?[-:| ]{3,}\|?\s*$", re.M), " "),  # table rules
    (re.compile(r"[*_~|]+"), " "),                  # emphasis, table pipes
]


def clean_markdown(md: str) -> str:
    s = md or ""
    for pat, rep in _MD_PATTERNS:
        s = pat.sub(rep, s)
    lines = [ln.strip() for ln in s.splitlines()]
    s = " ".join(ln if ln[-1] in ".!?:;," else ln + "." for ln in lines if ln)
    return re.sub(r"\s+", " ", s).strip()


_STATUS_WORDS = {"finished": "Job finished", "done": "Job finished", "success": "Job finished",
                 "failed": "Job failed", "error": "Job failed", "stuck": "Job looks stuck",
                 "waiting": "Job needs you", "queued": "Job queued", "running": "Job running"}


def _summary_part(report_md: str) -> str:
    """Prefer a '## Summary' section, else the first prose of the report."""
    m = re.search(r"^#{1,6}\s*summary\s*$(.*?)(?=^#{1,6}\s|\Z)", report_md or "", re.I | re.M | re.S)
    body = m.group(1) if m else re.sub(r"^\s*#\s.*$", "", report_md or "", count=1, flags=re.M)
    return clean_markdown(body)


def spoken_summary(status: str, title: str = "", report_md: str = "", max_words: int = 40) -> str:
    head = _STATUS_WORDS.get((status or "").lower().strip(), f"Job {status}".strip())
    parts = [head + "."]
    if title:
        parts.append(clean_markdown(title).rstrip(".") + ".")
    sents = re.split(r"(?<=[.!?])\s+", _summary_part(report_md))
    parts += [s for s in sents if s][:2]
    words = " ".join(parts).split()
    out = " ".join(words[:max_words])
    if len(words) > max_words:
        out = out.rstrip(",;:") + "..."
    return out


def status_speech(job: dict | None) -> str:
    """Spoken status for the latest job. job keys (all optional): status, title, step, steps,
    elapsed_s, report (markdown). Status values: queued, running, finished, failed, stuck."""
    if not job:
        return "There are no jobs yet."
    st = (job.get("status") or "").lower()
    if st in ("finished", "done", "success", "failed", "error", "stuck"):
        return spoken_summary(st, job.get("title", ""), job.get("report", ""))
    bits = [_STATUS_WORDS.get(st, f"Job {st}") + "."]
    if job.get("title"):
        bits.append(clean_markdown(job["title"]).rstrip(".") + ".")
    if job.get("steps"):
        bits.append(f"Step {job.get('step') or 0} of {job['steps']}.")
    if job.get("elapsed_s") is not None:
        m = int(job["elapsed_s"]) // 60
        bits.append("Under a minute so far." if m < 1 else f"{m} minute{'s' if m != 1 else ''} so far.")
    return " ".join(bits)


# ---------------------------------------------------------------- hands-free commands
_WAKE = re.compile(r"^\s*(?:hey|hi|okay|ok)?[\s,.!]*(?:agents?|a gent|asians?|aging|agent's)\b[\s,.!:]*", re.I)
_CMDS = [
    ("new_job", re.compile(r"^(?:new|start|create)\s+(?:a\s+)?(?:new\s+)?job\b[\s,.:]*(?P<text>.*)$", re.I)),
    ("follow_up", re.compile(r"^follow[\s-]?up\b[\s,.:]*(?P<text>.*)$", re.I)),
    ("run", re.compile(r"^(?:run|start|go|send)(?:\s+(?:it|that|this))?(?:\s+now)?[.!]?$", re.I)),
    ("cancel", re.compile(r"^(?:cancel|never\s*mind|scrap)(?:\s+(?:it|that|this))?[.!]?$", re.I)),
    ("status", re.compile(r"^(?:status|what'?s\s+(?:the\s+)?status|how'?s\s+it\s+going|progress)\??[.!]?$", re.I)),
    ("read_report", re.compile(r"^(?:read\s+(?:the\s+)?(?:latest\s+)?report|read\s+it)[.!]?$", re.I)),
    ("stop", re.compile(r"^(?:stop|quiet|shut\s+up|be\s+quiet)(?:\s+talking)?[.!]?$", re.I)),
]


def parse_command(heard: str) -> dict | None:
    """Return None if the phrase doesn't start with the wake word.
    Otherwise {"cmd": name, "text": str} where name is one of the _CMDS names, "wake" (the
    wake word alone) or "unknown"."""
    m = _WAKE.match(heard or "")
    if not m:
        return None
    rest = heard[m.end():].strip()
    if not rest.strip(" ,.!?:;-"):
        # Just "agent" on its own: people pause after the wake word. The page answers "Yes?"
        # and takes the next phrase as the command (it re-adds the wake word itself).
        return {"cmd": "wake", "text": ""}
    for name, pat in _CMDS:
        cm = pat.match(rest)
        if cm:
            text = (cm.groupdict().get("text") or "").strip().rstrip(".")
            if name in ("new_job", "follow_up") and not text:
                return {"cmd": "unknown", "text": rest}
            return {"cmd": name, "text": text}
    return {"cmd": "unknown", "text": rest}
