/* Agent Jobs voice: hold-to-talk, talk-back and hands-free ("agent ...") mode.
 * Everything goes to the local jobboard server only. No browser SpeechRecognition (Chrome sends it to Google).
 *
 * Page hooks (data attributes):
 *   <textarea data-voice="new-job">                 new-job prompt box (gets a mic button)
 *   <textarea data-voice="follow-up" data-job="ID">  follow-up box(es) (get mic buttons)
 *   The form around each textarea is submitted for "run it" (its submit button is clicked).
 *   <div data-voice-read>report</div>               gets a "Read aloud" button
 * Server endpoints: POST /api/transcribe, POST /api/speak, GET /api/voice/status,
 *   GET /api/voice/report, POST /api/voice/command
 */
(function () {
  "use strict";
  if (window.AgentVoice) return;

  const store = {
    get(k, d) { try { const v = localStorage.getItem("voice." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("voice." + k, JSON.stringify(v)); } catch (e) {} },
    sget(k) { try { return sessionStorage.getItem("voice." + k); } catch (e) { return null; } },
    sset(k, v) { try { sessionStorage.setItem("voice." + k, v); } catch (e) {} },
  };

  /* ---------------- styles + control bar ---------------- */
  const css = `
  .av-mic{margin:4px 0 0 0;padding:6px 12px;border-radius:999px;border:1px solid var(--line,#888);background:var(--card,#f4f4f4);color:var(--fg,#222);font-weight:500;cursor:pointer;font:inherit;user-select:none;touch-action:none}
  .av-mic.rec{background:var(--bad,#d22);color:#fff;border-color:var(--bad,#d22)}
  .av-mic.busy{opacity:.6}
  .av-bar{position:fixed;right:12px;bottom:12px;z-index:9999;display:flex;gap:6px;align-items:center;background:var(--card,#fff);color:var(--fg,#222);border:1px solid var(--line,#ccc);border-radius:10px;padding:6px 8px;box-shadow:0 2px 8px rgba(0,0,0,.15);font:13px system-ui,sans-serif;max-width:calc(100vw - 24px);flex-wrap:wrap}
  .av-bar button{font:inherit;font-weight:500;padding:4px 8px;border-radius:6px;border:1px solid var(--line,#aaa);background:var(--bg,#f6f6f6);color:var(--fg,#222);cursor:pointer}
  .av-bar button.on{background:var(--ok,#2a7);color:#fff;border-color:var(--ok,#2a7)}
  .av-msg{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .av-dot{width:12px;height:12px;border-radius:50%;background:#bbb;display:inline-block}
  .av-dot.listen{background:#2a7}.av-dot.capture{background:#d22;width:16px;height:16px}
  @media (prefers-color-scheme: dark){.av-bar{background:var(--card,#222);color:var(--fg,#eee);border-color:var(--line,#444)}.av-bar button,.av-mic{background:var(--bg,#333);color:var(--fg,#eee);border-color:var(--line,#555)}}`;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  let bar, msgEl, dotEl, spkBtn, hfBtn, stopBtn;
  function buildBar() {
    bar = document.createElement("div");
    bar.className = "av-bar";
    bar.innerHTML = '<span class="av-dot"></span><button data-k="spk"></button><button data-k="hf"></button><button data-k="stop" hidden>Stop</button><span class="av-msg"></span>';
    document.body.appendChild(bar);
    [dotEl, spkBtn, hfBtn, stopBtn, msgEl] = bar.children;
    spkBtn.onclick = () => { setSpeaker(!store.get("speaker", true)); };
    hfBtn.onclick = () => { handsFree.active ? handsFree.stop() : handsFree.start(); };
    stopBtn.onclick = stopSpeaking;
    setSpeaker(store.get("speaker", true));
    hfBtn.textContent = "Hands-free: off";
  }
  function setSpeaker(on) {
    store.set("speaker", on);
    spkBtn.textContent = on ? "🔊 Talk-back on" : "🔇 Talk-back off";
    spkBtn.classList.toggle("on", on);
    if (!on) stopSpeaking();
  }
  let msgTimer;
  function say(text, ms) {
    msgEl.textContent = text;
    msgEl.title = text;
    clearTimeout(msgTimer);
    if (ms) msgTimer = setTimeout(() => { if (msgEl.textContent === text) msgEl.textContent = ""; }, ms);
  }

  /* ---------------- talk-back ---------------- */
  let audio = null, speaking = false;
  const speakQueue = [];
  async function speak(text, force) {
    if (!text || (!force && !store.get("speaker", true))) return;
    speakQueue.push(text);
    if (speaking) return;
    speaking = true;
    while (speakQueue.length) {
      const t = speakQueue.shift();
      try {
        const r = await fetch("/api/speak", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t }) });
        if (!r.ok) { say("Talk-back: " + ((await r.json().catch(() => ({}))).error || r.status), 6000); continue; }
        const url = URL.createObjectURL(await r.blob());
        audio = new Audio(url);
        stopBtn.hidden = false;
        handsFree.pause(true);
        await new Promise((res) => { audio.onended = audio.onerror = res; audio.play().catch(res); });
        URL.revokeObjectURL(url);
      } catch (e) { say("Talk-back failed", 6000); }
      audio = null;
    }
    speaking = false;
    stopBtn.hidden = true;
    setTimeout(() => handsFree.pause(false), 300); // don't hear our own tail
  }
  function stopSpeaking() {
    speakQueue.length = 0;
    if (audio) { audio.pause(); audio.onended && audio.onended(); }
  }

  /* ---------------- transcription ---------------- */
  async function transcribe(blob) {
    const r = await fetch("/api/transcribe", { method: "POST", headers: { "Content-Type": blob.type || "application/octet-stream" }, body: blob });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return (j.text || "").trim();
  }
  function micError(e) {
    if (e && (e.name === "NotAllowedError" || e.name === "SecurityError")) return "Microphone blocked. Allow it in the address bar.";
    if (e && e.name === "NotFoundError") return "No microphone found.";
    return "Mic error: " + (e && e.message || e);
  }
  function appendTo(ta, text) {
    if (!text) return;
    ta.value = ta.value && !/\s$/.test(ta.value) ? ta.value + " " + text : ta.value + text;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    ta.focus();
  }

  /* ---------------- hold-to-talk ---------------- */
  let rec = null;
  function addMic(ta) {
    if (ta.dataset.voiceMic) return;
    ta.dataset.voiceMic = "1";
    const b = document.createElement("button");
    b.type = "button";
    b.className = "av-mic";
    b.textContent = "🎤 Hold to talk";
    b.title = "Hold to record (mouse, touch or Space), release to transcribe";
    const start = async (ev) => {
      ev.preventDefault();
      if (rec || b.classList.contains("busy")) return;
      handsFree.pause(true);
      stopSpeaking();
      let stream;
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
      catch (e) { say(micError(e), 8000); handsFree.pause(false); return; }
      const chunks = [];
      const mr = new MediaRecorder(stream);
      rec = { mr, stream, b, released: false };
      mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      mr.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        rec = null;
        b.classList.remove("rec");
        b.classList.add("busy");
        b.textContent = "Transcribing...";
        try {
          const text = await transcribe(new Blob(chunks, { type: mr.mimeType || "audio/webm" }));
          text ? appendTo(ta, text) : say("Didn't catch anything", 4000);
        } catch (e) { say("Transcription: " + e.message, 8000); }
        b.classList.remove("busy");
        b.textContent = "🎤 Hold to talk";
        handsFree.pause(false);
      };
      mr.start();
      b.classList.add("rec");
      b.textContent = "Listening... release to stop";
      if (rec.released) stop();
    };
    const stop = () => { if (rec && rec.b === b && rec.mr.state === "recording") rec.mr.stop(); else if (rec) rec.released = true; };
    b.addEventListener("pointerdown", start);
    b.addEventListener("pointerup", stop);
    b.addEventListener("pointerleave", stop);
    b.addEventListener("pointercancel", stop);
    b.addEventListener("keydown", (e) => { if (e.code === "Space" && !e.repeat) start(e); });
    b.addEventListener("keyup", (e) => { if (e.code === "Space") { e.preventDefault(); stop(); } });
    ta.insertAdjacentElement("afterend", b);
  }
  function addReadAloud(el) {
    if (el.dataset.voiceReadBtn) return;
    el.dataset.voiceReadBtn = "1";
    const b = document.createElement("button");
    b.type = "button";
    b.className = "av-mic";
    b.textContent = "🔈 Read aloud";
    b.onclick = () => { speaking ? stopSpeaking() : speak(el.innerText, true); };
    el.insertAdjacentElement("beforebegin", b);
  }
  function scan() {
    document.querySelectorAll("textarea[data-voice]").forEach(addMic);
    document.querySelectorAll("[data-voice-read]").forEach(addReadAloud);
  }

  /* ---------------- hands-free ---------------- */
  function encodeWav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
    v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data");
    v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) { const s = Math.max(-1, Math.min(1, samples[i])); v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
    return new Blob([buf], { type: "audio/wav" });
  }

  const handsFree = {
    active: false, paused: false, ctx: null, stream: null, node: null, pending: null,
    async start() {
      try { this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); }
      catch (e) { say(micError(e), 8000); return; }
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      const src = this.ctx.createMediaStreamSource(this.stream);
      this.node = this.ctx.createScriptProcessor(2048, 1, 1);
      const rate = this.ctx.sampleRate, frameMs = 2048 / rate * 1000;
      let floor = 0.01, capturing = false, loudMs = 0, quietMs = 0, buf = [], pre = [];
      this.node.onaudioprocess = (e) => {
        const d = new Float32Array(e.inputBuffer.getChannelData(0));
        if (this.paused) { capturing = false; buf = []; pre = []; return; }
        let sum = 0; for (let i = 0; i < d.length; i++) sum += d[i] * d[i];
        const rms = Math.sqrt(sum / d.length), thr = Math.max(0.015, floor * 3);
        if (!capturing) {
          floor = rms < thr ? floor * 0.95 + rms * 0.05 : floor;
          pre.push(d); if (pre.length > 8) pre.shift();          // ~350 ms pre-roll
          loudMs = rms > thr ? loudMs + frameMs : 0;
          if (loudMs >= 120) { capturing = true; buf = pre.slice(); pre = []; quietMs = 0; dotEl.className = "av-dot capture"; }
        } else {
          buf.push(d);
          quietMs = rms > thr * 0.7 ? 0 : quietMs + frameMs;
          const lenMs = buf.length * frameMs;
          if (quietMs >= 800 || lenMs > 15000) {
            capturing = false; loudMs = 0; dotEl.className = "av-dot listen";
            const n = buf.reduce((a, b) => a + b.length, 0), all = new Float32Array(n);
            let o = 0; for (const b of buf) { all.set(b, o); o += b.length; }
            buf = [];
            if (lenMs - quietMs >= 300) this.phrase(encodeWav(all, rate));
          }
        }
      };
      src.connect(this.node);
      this.node.connect(this.ctx.destination); // required for onaudioprocess to fire (outputs silence)
      this.active = true;
      store.set("handsfree", true);
      hfBtn.textContent = "Hands-free: on"; hfBtn.classList.add("on");
      dotEl.className = "av-dot listen";
      say("Listening for 'agent'...");
    },
    stop() {
      this.active = false;
      store.set("handsfree", false);
      try { this.node && this.node.disconnect(); this.ctx && this.ctx.close(); } catch (e) {}
      this.stream && this.stream.getTracks().forEach((t) => t.stop());
      this.node = this.ctx = this.stream = null;
      this.pending = null;
      hfBtn.textContent = "Hands-free: off"; hfBtn.classList.remove("on");
      dotEl.className = "av-dot";
      say("", 1);
    },
    pause(p) { this.paused = p; },
    async phrase(wav) {
      let heard;
      try { heard = await transcribe(wav); } catch (e) { say("Transcription: " + e.message, 6000); return; }
      if (!heard) return;
      let cmd;
      try {
        const r = await fetch("/api/voice/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: heard }) });
        cmd = (await r.json()).command;
      } catch (e) { return; }
      if (!cmd) return; // no wake word: ignore silently
      say("Heard: " + heard, 8000);
      runCommand(cmd, heard);
    },
  };

  function box(kind) {
    const all = [...document.querySelectorAll(`textarea[data-voice="${kind}"]`)];
    return all.length ? all[all.length - 1] : null;
  }
  function submitBox(ta) {
    const form = ta.closest("form");
    if (!form) return false;
    const btn = form.querySelector('button[type="submit"],input[type="submit"],button:not([type])');
    btn ? btn.click() : form.requestSubmit ? form.requestSubmit() : form.submit();
    return true;
  }
  async function getText(url) {
    const r = await fetch(url);
    return (await r.json()).text || "";
  }

  async function runCommand(c, heard) {
    const p = handsFree.pending;
    switch (c.cmd) {
      case "new_job":
      case "follow_up": {
        const ta = box(c.cmd === "new_job" ? "new-job" : "follow-up");
        if (!ta) return speak(c.cmd === "new_job" ? "I can't find the new job box." : "There's no job to follow up.", true);
        ta.value = c.text;
        ta.dispatchEvent(new Event("input", { bubbles: true }));
        handsFree.pending = { ta };
        return speak((c.cmd === "new_job" ? "New job: " : "Follow up: ") + c.text + ". Say agent run it, or agent cancel.", true);
      }
      case "run":
        if (!p) return speak("There's nothing waiting to run.", true);
        handsFree.pending = null;
        return submitBox(p.ta) ? speak("Starting it now.", true) : speak("I couldn't press run.", true);
      case "cancel":
        if (p) { p.ta.value = ""; handsFree.pending = null; }
        return speak("Cancelled.", true);
      case "status": return speak(await getText("/api/voice/status"), true);
      case "read_report": return speak(await getText("/api/voice/report"), true);
      case "stop": return stopSpeaking();
      default: return speak("Sorry, I didn't get that.", true);
    }
  }

  /* ---------------- finish announcements ---------------- */
  const ANNOUNCE = ["finished", "failed", "stuck"];
  async function poll() {
    try {
      const r = await fetch("/api/voice/status");
      const j = await r.json();
      const key = j.job_id ? j.job_id + ":" + j.status : "";
      const last = store.sget("announced");
      if (last === null) { store.sset("announced", key); return; } // first load: don't replay old results
      if (key && key !== last) {
        store.sset("announced", key);
        if (ANNOUNCE.includes(j.status)) speak(j.text);
      }
    } catch (e) {}
  }

  /* ---------------- init ---------------- */
  function init() {
    buildBar();
    scan();
    new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
    poll();
    setInterval(poll, 5000);
    if (store.get("handsfree", false)) handsFree.start(); // survives page reloads
  }
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", init) : init();

  window.AgentVoice = { speak: (t) => speak(t, true), stopSpeaking, handsFree, scan };
})();
