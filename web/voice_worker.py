"""Whisper worker for Agent Jobs voice. Runs under ~/agent-sandbox/voice/venv/bin/python.
Protocol: prints {"ready": true} once loaded, then for each stdin line {"wav": path}
prints {"text": ...} or {"error": ...}. CPU int8 only."""
import json
import os
import sys

# Biases whisper toward the words used with Agent Jobs ("readme" was heard as "writer Ed B" without it).
PROMPT = os.environ.get("AGENT_VOICE_PROMPT",
                        "Agent, new job: update the README. Agent, run it. Agent, status. Agent, follow up. "
                        "Android app, APK, Gradle, Kotlin, git commit, unit tests, Basalt, Qwen.")


def main():
    model_dir = sys.argv[1]
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=4)
    except Exception as e:  # report and exit
        print(json.dumps({"ready": False, "error": str(e)}), flush=True)
        return 1
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            segs, _ = model.transcribe(req["wav"], language="en", vad_filter=True, beam_size=5,
                                        initial_prompt=PROMPT or None)
            print(json.dumps({"text": " ".join(s.text.strip() for s in segs).strip()}), flush=True)
        except Exception as e:
            print(json.dumps({"error": str(e)[:300]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
