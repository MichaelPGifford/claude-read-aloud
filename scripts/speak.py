#!/usr/bin/env python3
"""Read Claude's replies aloud. Python 3.9+, standard library only.

  speak.py                      speak the last reply (newest transcript)
  speak.py --project PATH       …scoped to one workspace's sessions
  speak.py --transcript FILE    …from one specific session transcript
  speak.py --text "…"           speak the given text
  speak.py --stdin              speak the text arriving on stdin
  speak.py --stop               stop playback
  speak.py --detach             re-launch detached, return immediately
  speak.py --hook               Stop-hook mode: exit fast unless auto_read is on
  speak.py --status             show current configuration
  speak.py --auto on|off        toggle read-every-reply
  speak.py --print              show what would be spoken, speak nothing

Design notes, learned the hard way before this was a plugin:

* CHUNKED, RAMPED PLAYBACK. Cloud TTS has per-request input limits, and
  synthesis time scales with input length — nothing plays until the first
  chunk exists, so the first chunk's size IS the time-to-first-sound
  (measured: 3.3s at 260 chars vs 10.7s at 1800, same voice). Chunks ramp
  ~260 → ~1000 → 1800 at sentence boundaries; chunk i+1 synthesises while
  chunk i plays, so later chunks cost no waiting and there are no gaps.
* THE ORCHESTRATOR IS THE PROCESS YOU STOP. This process stays alive for the
  whole playback, records its pid, and SIGTERM takes down the current player
  with it. Starting a new reading stops the old one first.
* SPEECHIFY'S WAV NEEDS ITS HEADER FIXED. It arrives with the RIFF/data sizes
  set to 0xFFFFFFFF (a streaming placeholder); some players then play the
  header bytes as audio — it sounds like a burst of static.
* HOOK MODE MUST BE HARMLESS. It reads the exact transcript path from stdin
  (never guesses), spawns itself detached, and exits 0 no matter what —
  a TTS failure must never break Claude's own flow.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.request
import wave

# --------------------------------------------------------------------------- paths

HOME = pathlib.Path.home()
PROJECTS = HOME / ".claude" / "projects"
PIDFILE = pathlib.Path(tempfile.gettempdir()) / "claude-read-aloud.pid"
PARTS = [pathlib.Path(tempfile.gettempdir()) / f"claude-read-aloud-{i}.wav"
         for i in (0, 1)]

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"


def config_path() -> pathlib.Path:
    if IS_WIN:
        base = pathlib.Path(os.environ.get("APPDATA", HOME))
    else:
        base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
    return base / "claude-read-aloud" / "config.json"


def data_dir() -> pathlib.Path:
    if IS_WIN:
        base = pathlib.Path(os.environ.get("LOCALAPPDATA", HOME))
    else:
        base = pathlib.Path(os.environ.get("XDG_DATA_HOME", HOME / ".local" / "share"))
    return base / "claude-read-aloud"


DEFAULTS = {
    "provider": "system",   # system | speechify | elevenlabs | openai | command
    "voice": "",            # provider-specific voice name/id; "" = default
    "speed": 1.0,
    "auto_read": False,     # Stop hook reads every reply when true
    "max_chars": 12000,     # hard cap; ~14 minutes of speech
    "chunk_chars": 1800,    # per-request size, under every provider's limit
    "first_chars": 260,     # first chunk is tiny: its size is the latency
    "model": "",            # openai only; default tts-1
    "command": "",          # custom engine: template with {text} and/or {out}
    "api_keys": {},         # optional; env vars take precedence
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(config_path().read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def api_key(cfg: dict, provider: str) -> str:
    key = os.environ.get(f"{provider.upper()}_API_KEY") \
        or cfg.get("api_keys", {}).get(provider, "")
    if not key:
        sys.exit(f"{provider} needs an API key: set {provider.upper()}_API_KEY "
                 f"or api_keys.{provider} in {config_path()}")
    return key

# ----------------------------------------------------------------- transcript → text


def newest_transcript(project: str | None = None) -> pathlib.Path | None:
    """Newest session transcript, scoped to one project when a path is given.

    Claude Code encodes a project path into its directory name by replacing
    every non-alphanumeric character with '-'. Unscoped "newest across all
    projects" reads whichever session wrote last — the wrong one whenever two
    sessions are running, which is why scoping exists.
    """
    files: list[pathlib.Path] = []
    if project:
        enc = "-" + re.sub(r"[^A-Za-z0-9]", "-", project.strip("/\\"))
        files = list((PROJECTS / enc).glob("*.jsonl"))
    if not files:
        files = list(PROJECTS.glob("*/*.jsonl"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def last_reply(path: pathlib.Path) -> str:
    """The final assistant text block in a session transcript."""
    text = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "assistant":
            continue
        blocks = e.get("message", {}).get("content", [])
        if not isinstance(blocks, list):
            continue
        joined = "".join(b.get("text", "") for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text")
        if joined.strip():
            text = joined
    return text


def spoken_form(md: str, max_chars: int) -> str:
    """Markdown reads terribly aloud — strip everything that is not prose."""
    s = re.sub(r"```.*?```", " (code omitted) ", md, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)      # keep link text, drop URL
    s = re.sub(r"^\s*\|.*\|\s*$", " ", s, flags=re.M)   # tables
    s = re.sub(r"^[ \t]*[-*+]\s+", "", s, flags=re.M)   # bullets
    s = re.sub(r"^#{1,6}\s*", "", s, flags=re.M)        # headings
    s = re.sub(r"[*_>#`]+", "", s)
    s = re.sub(r"https?://\S+", " link ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_chars:
        cut = s[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        s = (cut[:end + 1] if end > max_chars // 2 else cut) \
            + " … I'll stop there — the rest is on screen."
    return s

# ------------------------------------------------------------------------ chunking


def chunk_text(s: str, first: int, full: int) -> list[str]:
    """Split at sentence boundaries; sizes ramp first → 4×first → full."""
    def limits():
        yield min(first, full)
        yield min(4 * first, full)
        while True:
            yield full

    gen = limits()
    limit = next(gen)
    parts: list[str] = []
    cur = ""
    for sent in re.split(r"(?<=[.!?;:]) +", s):
        while len(sent) > limit:                # pathological unbroken run
            take = limit - len(cur) - 1 if cur else limit
            if take < 40:                       # no room left in this chunk
                parts.append(cur)
                cur = ""
                limit = next(gen)
                continue
            head, sent = sent[:take], sent[take:]
            cur = f"{cur} {head}".strip()
            parts.append(cur)
            cur = ""
            limit = next(gen)
        if cur and len(cur) + len(sent) + 1 > limit:
            parts.append(cur)
            cur = sent
            limit = next(gen)
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        parts.append(cur)
    return parts

# ------------------------------------------------------------------- audio plumbing


def fix_riff_sizes(b: bytes) -> bytes:
    """Repair streaming WAV headers whose size fields are 0xFFFFFFFF.

    Speechify (and some streaming encoders) emit RIFF/data chunk sizes as the
    placeholder 0xFFFFFFFF; players that trust the header then misread the
    file — heard as a burst of static. Sizes are recomputed from actual length.
    """
    if len(b) < 44 or b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return b
    out = bytearray(b)
    struct.pack_into("<I", out, 4, len(out) - 8)
    off = 12
    while off + 8 <= len(out):
        cid = bytes(out[off:off + 4])
        size = struct.unpack_from("<I", out, off + 4)[0]
        actual_rest = len(out) - off - 8
        if cid == b"data":
            if size == 0xFFFFFFFF or size > actual_rest:
                struct.pack_into("<I", out, off + 4, actual_rest)
            break
        if size == 0xFFFFFFFF or size > actual_rest:
            break
        off += 8 + size + (size & 1)
    return bytes(out)


def wrap_pcm(pcm: bytes, out: pathlib.Path, rate: int = 22050) -> None:
    """Wrap raw 16-bit mono PCM in a WAV container (ElevenLabs pcm_* output)."""
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def _which(*names: str) -> str | None:
    from shutil import which
    for n in names:
        if which(n):
            return n
    return None


def player_cmd(wav: pathlib.Path) -> list[str]:
    if IS_MAC:
        return ["afplay", str(wav)]
    if IS_WIN:
        path = str(wav).replace("'", "''")
        return ["powershell", "-NoProfile", "-Command",
                f"(New-Object Media.SoundPlayer '{path}').PlaySync()"]
    p = _which("aplay", "paplay", "ffplay")
    if not p:
        sys.exit("no audio player found (need aplay, paplay, or ffplay)")
    if p == "ffplay":
        return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(wav)]
    return [p, "-q", str(wav)] if p == "aplay" else [p, str(wav)]

# ---------------------------------------------------------------------- providers


def http_json(url: str, payload: dict, headers: dict) -> bytes:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def make_synth(cfg: dict):
    """Return synth(text, out_path) writing a playable WAV, or None when the
    provider speaks directly (system voices, self-playing custom commands)."""
    provider = cfg["provider"]

    if provider == "speechify":
        import base64
        key = api_key(cfg, "speechify")

        def synth(text: str, out: pathlib.Path) -> None:
            raw = http_json(
                "https://api.sws.speechify.com/v1/audio/speech",
                {"input": text, "voice_id": cfg["voice"] or "oliver",
                 "audio_format": "wav"},
                {"Authorization": f"Bearer {key}"})
            audio = base64.b64decode(json.loads(raw)["audio_data"])
            out.write_bytes(fix_riff_sizes(audio))
        return synth

    if provider == "elevenlabs":
        key = api_key(cfg, "elevenlabs")
        voice = cfg["voice"] or "21m00Tcm4TlvDq8ikWAM"      # Rachel (premade)

        def synth(text: str, out: pathlib.Path) -> None:
            pcm = http_json(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
                "?output_format=pcm_22050",
                {"text": text},
                {"xi-api-key": key})
            wrap_pcm(pcm, out)
        return synth

    if provider == "openai":
        key = api_key(cfg, "openai")

        def synth(text: str, out: pathlib.Path) -> None:
            audio = http_json(
                "https://api.openai.com/v1/audio/speech",
                {"model": cfg["model"] or "tts-1",
                 "voice": cfg["voice"] or "alloy",
                 "input": text, "response_format": "wav",
                 "speed": cfg["speed"] or 1.0},
                {"Authorization": f"Bearer {key}"})
            out.write_bytes(fix_riff_sizes(audio))
        return synth

    if provider == "kokoro":
        vp, model, voices_bin, runner = kokoro_paths()
        if not (vp.exists() and model.exists() and voices_bin.exists() and runner.exists()):
            sys.exit("kokoro is not set up — run /read-aloud:voice-setup "
                     "(or: speak.py --setup kokoro)")

        def synth(text: str, out: pathlib.Path) -> None:
            # One long-lived runner keeps the 300MB model loaded across chunks;
            # spawning per chunk would reload it every time.
            global _kokoro_runner
            if _kokoro_runner is None or _kokoro_runner.poll() is not None:
                _kokoro_runner = subprocess.Popen(
                    [str(vp), str(runner), str(model), str(voices_bin)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            req = {"text": text, "voice": cfg["voice"] or "am_michael",
                   "speed": float(cfg["speed"] or 1.0), "out": str(out)}
            _kokoro_runner.stdin.write(json.dumps(req) + "\n")
            _kokoro_runner.stdin.flush()
            line = _kokoro_runner.stdout.readline()
            if not line.startswith("ok"):
                raise RuntimeError(f"kokoro runner: {line.strip() or 'died'}")
        return synth

    if provider == "command":
        template = cfg["command"]
        if not template:
            sys.exit(f'provider "command" needs a command template in {config_path()}')
        if "{out}" not in template:
            return None                          # self-playing command

        def synth(text: str, out: pathlib.Path) -> None:
            cmd = [a.replace("{text}", text).replace("{out}", str(out))
                   for a in shlex.split(template)]
            subprocess.run(cmd, capture_output=True, check=True)
        return synth

    return None                                  # system voices speak directly


def speak_direct(text: str, cfg: dict) -> None:
    """System TTS (and self-playing custom commands): no files, near-zero latency."""
    speed = float(cfg["speed"] or 1.0)
    if cfg["provider"] == "command":
        cmd = [a.replace("{text}", text) for a in shlex.split(cfg["command"])]
    elif IS_MAC:
        cmd = ["say"]
        if cfg["voice"]:
            cmd += ["-v", cfg["voice"]]
        if abs(speed - 1.0) > 0.01:
            cmd += ["-r", str(int(190 * speed))]
        cmd.append(text)
    elif IS_WIN:
        esc = text.replace("'", "''")
        rate = max(-10, min(10, int((speed - 1.0) * 10)))
        sel = (f"$s.SelectVoice('{cfg['voice']}');" if cfg["voice"] else "")
        cmd = ["powershell", "-NoProfile", "-Command",
               "Add-Type -AssemblyName System.Speech;"
               "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
               f"{sel}$s.Rate = {rate};$s.Speak('{esc}')"]
    else:
        if not _which("spd-say"):
            sys.exit("no system voice found — install speech-dispatcher, "
                     "or pick a cloud provider (see --status)")
        cmd = ["spd-say", "-w"]
        if cfg["voice"]:
            cmd += ["-y", cfg["voice"]]
        if abs(speed - 1.0) > 0.01:
            cmd += ["-r", str(max(-100, min(100, int((speed - 1.0) * 100))))]
        cmd.append(text)

    stop()
    PIDFILE.write_text(str(os.getpid()))
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    signal.signal(signal.SIGTERM, lambda *_: (p.terminate(), os._exit(0)))
    p.wait()
    PIDFILE.unlink(missing_ok=True)

# ------------------------------------------------------------------ kokoro (local)

_kokoro_runner: subprocess.Popen | None = None

KOKORO_URLS = {
    "kokoro-v1.0.onnx":
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin":
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
        "model-files-v1.0/voices-v1.0.bin",
}

# Written to disk at setup; runs inside the plugin's own venv where
# kokoro-onnx and soundfile exist. The plugin itself stays stdlib-only.
KOKORO_RUNNER = '''import json, sys
from kokoro_onnx import Kokoro
import soundfile as sf
k = Kokoro(sys.argv[1], sys.argv[2])
for line in sys.stdin:
    try:
        q = json.loads(line)
        s, r = k.create(q["text"], voice=q["voice"], speed=q["speed"], lang="en-us")
        sf.write(q["out"], s, r)
        print("ok", flush=True)
    except Exception as e:
        print("err " + str(e).replace(chr(10), " "), flush=True)
'''


def kokoro_paths() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    d = data_dir()
    vp = d / "venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")
    return vp, d / "kokoro" / "kokoro-v1.0.onnx", d / "kokoro" / "voices-v1.0.bin", \
        d / "kokoro_runner.py"


def setup_kokoro() -> int:
    """One-time install of the free local neural voice (~340MB, no account)."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    vp, model, voices_bin, runner = kokoro_paths()

    if not vp.exists():
        print("creating a private virtualenv…", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(d / "venv")], check=True)
    print("installing kokoro-onnx (a few minutes on first run)…", flush=True)
    subprocess.run([str(vp), "-m", "pip", "install", "--quiet", "--upgrade",
                    "kokoro-onnx", "soundfile"], check=True)

    (d / "kokoro").mkdir(exist_ok=True)
    for name, min_mb in (("kokoro-v1.0.onnx", 200), ("voices-v1.0.bin", 10)):
        dest = d / "kokoro" / name
        if dest.exists() and dest.stat().st_size > min_mb * 1_000_000:
            print(f"{name}: already present", flush=True)
            continue
        print(f"downloading {name}…", flush=True)
        tmp = dest.with_suffix(".part")
        with urllib.request.urlopen(KOKORO_URLS[name], timeout=60) as r, \
                open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                got += len(buf)
                if total and got % (50 << 20) < (1 << 20):
                    print(f"  {got >> 20} / {total >> 20} MB", flush=True)
        tmp.rename(dest)
        print(f"  {name}: {dest.stat().st_size >> 20} MB", flush=True)

    runner.write_text(KOKORO_RUNNER, encoding="utf-8")

    cfg = load_config()
    if cfg["provider"] == "system":
        cfg["provider"] = "kokoro"
        if not str(cfg["voice"]).startswith(("af_", "am_", "bf_", "bm_")):
            cfg["voice"] = "am_michael"
        save_config(cfg)
        print("\nKokoro installed — provider set to kokoro (voice am_michael).")
    else:
        # Someone who deliberately configured a cloud voice keeps it.
        print(f"\nKokoro installed. Provider left as {cfg['provider']} — switch by "
              f"setting \"provider\": \"kokoro\" in {config_path()}")
    print("54 voices (am_michael, am_puck, af_heart, bf_emma, …) — set \"voice\" in the config.")
    return 0

# ----------------------------------------------------------------------- playback


def stop() -> None:
    if PIDFILE.exists():
        try:
            os.kill(int(PIDFILE.read_text().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        PIDFILE.unlink(missing_ok=True)


def play_chunked(text: str, cfg: dict, synth) -> None:
    """Gapless chunked playback: synthesise chunk i+1 while chunk i plays."""
    stop()                                       # never overlap two readings
    PIDFILE.write_text(str(os.getpid()))

    state: dict = {"player": None}

    def on_term(_sig, _frm):
        p = state["player"]
        if p and p.poll() is None:
            p.terminate()
        if _kokoro_runner and _kokoro_runner.poll() is None:
            _kokoro_runner.terminate()
        PIDFILE.unlink(missing_ok=True)
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)

    chunks = chunk_text(text, int(cfg["first_chars"]), int(cfg["chunk_chars"]))
    synth(chunks[0], PARTS[0])
    for i in range(len(chunks)):
        cur = PARTS[i % 2]
        state["player"] = subprocess.Popen(
            player_cmd(cur), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        prefetch = None
        if i + 1 < len(chunks):
            prefetch = threading.Thread(
                target=synth, args=(chunks[i + 1], PARTS[(i + 1) % 2]), daemon=True)
            prefetch.start()
        state["player"].wait()
        if prefetch:
            prefetch.join()
    PIDFILE.unlink(missing_ok=True)

# --------------------------------------------------------------------------- modes


def detach(argv: list[str]) -> None:
    """Re-launch this script detached so the caller returns immediately."""
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "stdin": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.abspath(__file__), *argv], **kwargs)


def hook_mode() -> int:
    """Stop-hook entry: exit fast and NEVER fail — Claude's flow comes first."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not load_config().get("auto_read"):
            return 0
        path = event.get("transcript_path", "")
        if path and pathlib.Path(path).exists():
            detach(["--transcript", path])
    except Exception as e:                        # noqa: BLE001 — deliberate
        print(f"read-aloud hook: {e}", file=sys.stderr)
    return 0


# Kokoro's voice roster is fixed per model release, and the model cannot be
# asked for it cheaply — listing must not load 300MB. English voices first.
KOKORO_VOICES = [
    "am_michael", "am_puck", "af_heart", "af_bella", "af_nova", "af_sarah",
    "af_sky", "af_alloy", "af_aoede", "af_jessica", "af_kore", "af_nicole",
    "af_river", "am_adam", "am_echo", "am_eric", "am_fenrir",
    "am_liam", "am_onyx", "am_santa",
    "bf_emma", "bf_alice", "bf_isabella", "bf_lily",
    "bm_george", "bm_daniel", "bm_fable", "bm_lewis",
]

OPENAI_VOICES = ["alloy", "ash", "coral", "echo", "fable", "nova", "onyx",
                 "sage", "shimmer"]


def list_voices(cfg: dict) -> int:
    """Print {provider, voices:[{id,label}]} as JSON for pickers to consume."""
    provider = cfg["provider"]
    voices: list[dict] = []

    if provider == "kokoro":
        voices = [{"id": v, "label": v.split("_", 1)[1] + (
            " (British)" if v[0] == "b" else "")} for v in KOKORO_VOICES]
    elif provider == "openai":
        voices = [{"id": v, "label": v} for v in OPENAI_VOICES]
    elif provider == "speechify":
        key = api_key(cfg, "speechify")
        cursor, pages = "", 0
        while pages < 12:
            pages += 1
            url = ("https://api.sws.speechify.com/v1/voices?limit=100"
                   + (f"&cursor={cursor}" if cursor else ""))
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=60) as r:
                page = json.loads(r.read())
            for v in page.get("voices", []):
                if str(v.get("locale", "")).startswith("en"):
                    voices.append({"id": v["id"],
                                   "label": f"{v.get('display_name', v['id'])} "
                                            f"({v.get('locale', '')}, {v.get('gender', '?')})"})
            cursor = page.get("next_cursor") or ""
            if not page.get("has_more") or not cursor:
                break
    elif provider == "elevenlabs":
        key = api_key(cfg, "elevenlabs")
        req = urllib.request.Request("https://api.elevenlabs.io/v1/voices",
                                     headers={"xi-api-key": key})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        voices = [{"id": v["voice_id"], "label": v.get("name", v["voice_id"])}
                  for v in data.get("voices", [])]
    elif provider == "system":
        if IS_MAC:
            out = subprocess.run(["say", "-v", "?"], capture_output=True,
                                 text=True).stdout
            for line in out.splitlines():
                m = re.match(r"^(.*?)\s{2,}([a-z]{2}_\w+)", line)
                if m:
                    voices.append({"id": m.group(1).strip(),
                                   "label": f"{m.group(1).strip()} ({m.group(2)})"})
        elif IS_WIN:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -AssemblyName System.Speech;"
                 "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                 ".GetInstalledVoices()|ForEach-Object{$_.VoiceInfo.Name}"],
                capture_output=True, text=True).stdout
            voices = [{"id": n.strip(), "label": n.strip()}
                      for n in out.splitlines() if n.strip()]
        else:
            out = subprocess.run(["spd-say", "-L"], capture_output=True,
                                 text=True).stdout
            for line in out.splitlines()[1:]:
                # Rows are "NAME LANGUAGE VARIANT" but NAME can contain spaces
                # ("English (Great Britain)+Adam en-gb Adam"), so split from
                # the right. spd-say lists every variant in every language —
                # ~15,000 rows; English only, or the picker is unusable.
                parts = line.rsplit(None, 2)
                if len(parts) == 3 and parts[1].lower().startswith("en"):
                    voices.append({"id": parts[0].strip(),
                                   "label": parts[0].strip()})
    else:
        print(f"provider {provider} has no listable voices", file=sys.stderr)
        return 1

    print(json.dumps({"provider": provider, "voices": voices}, indent=1))
    return 0


def show_status(cfg: dict) -> None:
    keys = {p: ("env" if os.environ.get(f"{p.upper()}_API_KEY")
                else "config" if cfg.get("api_keys", {}).get(p) else "—")
            for p in ("speechify", "elevenlabs", "openai")}
    print(f"config    : {config_path()}")
    print(f"provider  : {cfg['provider']}"
          + (f"  (voice: {cfg['voice']})" if cfg["voice"] else "  (default voice)"))
    print(f"speed     : {cfg['speed']}")
    print(f"auto_read : {'on — every reply is spoken' if cfg['auto_read'] else 'off'}")
    print(f"api keys  : " + "  ".join(f"{p}:{s}" for p, s in keys.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--text")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--transcript")
    ap.add_argument("--project")
    ap.add_argument("--detach", action="store_true")
    ap.add_argument("--hook", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--setup", choices=["kokoro"],
                    help="one-time install of the free local neural voice")
    ap.add_argument("--voice",
                    help="use this voice for this run only (audition)")
    ap.add_argument("--list-voices", action="store_true", dest="list_voices",
                    help="print the current provider's voices as JSON")
    ap.add_argument("--set-voice", dest="set_voice",
                    help="save this voice to the config")
    ap.add_argument("--set-provider", dest="set_provider",
                    choices=["system", "kokoro", "speechify", "elevenlabs",
                             "openai", "command"])
    ap.add_argument("--set-speed", dest="set_speed", type=float)
    ap.add_argument("--get-config", action="store_true", dest="get_config",
                    help="print effective config as JSON (for UI panels)")
    ap.add_argument("--auto", choices=["on", "off"])
    ap.add_argument("--print", action="store_true", dest="print_only")
    args = ap.parse_args()

    if args.hook:
        return hook_mode()
    if args.stop:
        stop()
        return 0

    if args.setup:
        return setup_kokoro()

    cfg = load_config()
    if args.get_config:
        vp, model, vb, runner = kokoro_paths()
        print(json.dumps({
            "provider": cfg["provider"], "voice": cfg["voice"],
            "speed": cfg["speed"], "auto_read": bool(cfg["auto_read"]),
            "kokoro_ready": all(p.exists() for p in (vp, model, vb, runner)),
            "keys": {p: bool(os.environ.get(f"{p.upper()}_API_KEY")
                             or cfg.get("api_keys", {}).get(p))
                     for p in ("speechify", "elevenlabs", "openai")},
        }))
        return 0
    if args.set_provider:
        if args.set_provider != cfg["provider"]:
            # A voice id belongs to ONE provider — keeping the old one hands
            # e.g. Kokoro a Speechify id and every reading errors. Reset to
            # empty; each provider has a good built-in default.
            cfg["voice"] = ""
        cfg["provider"] = args.set_provider
        save_config(cfg)
        print(f"provider set to {args.set_provider} (voice: default)")
        return 0
    if args.set_speed is not None:
        cfg["speed"] = max(0.5, min(2.0, args.set_speed))
        save_config(cfg)
        print(f"speed set to {cfg['speed']}")
        return 0
    if args.list_voices:
        return list_voices(cfg)
    if args.set_voice:
        cfg["voice"] = args.set_voice
        save_config(cfg)
        print(f"voice set to {args.set_voice} ({cfg['provider']})")
        return 0
    if args.voice:
        cfg["voice"] = args.voice          # this run only; nothing saved
    if args.status:
        show_status(cfg)
        return 0
    if args.auto:
        cfg["auto_read"] = args.auto == "on"
        save_config(cfg)
        print(f"auto-read {'on — every reply will be spoken' if cfg['auto_read'] else 'off'}")
        return 0

    if args.detach:
        argv = [a for a in sys.argv[1:] if a != "--detach"]
        detach(argv)
        print("reading aloud…")
        # First impressions: the stock Linux voice is espeak, and it is rough.
        # Tell the user the good free voice is one command away — once there.
        if (not IS_MAC and not IS_WIN and cfg["provider"] == "system"
                and not kokoro_paths()[1].exists()):
            print("note: this is the basic Linux system voice — run "
                  "/read-aloud:voice-setup once for a much better free "
                  "neural voice (~340MB, local, no account).")
        return 0

    if args.stdin:
        raw = sys.stdin.read()
    elif args.text:
        raw = args.text
    else:
        t = (pathlib.Path(args.transcript) if args.transcript
             else newest_transcript(args.project))
        if not t or not t.exists():
            print("no transcript found", file=sys.stderr)
            return 1
        raw = last_reply(t)

    text = spoken_form(raw, int(cfg["max_chars"]))
    if not text:
        print("nothing to speak", file=sys.stderr)
        return 1
    if args.print_only:
        print(text)
        return 0

    synth = make_synth(cfg)
    if synth is None:
        speak_direct(text, cfg)
    else:
        play_chunked(text, cfg, synth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
