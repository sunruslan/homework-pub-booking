"""Ex8 — voice loop (reference solution).

Two modes:
  * text mode: stdin → manager → stdout. Free, no mic needed.
  * voice mode: mic → ElevenLabs Scribe STT → manager → ElevenLabs TTS → speakers.

Both modes write identical trace events so downstream grading
doesn't care which ran.

Voice mode degrades gracefully:
  - No ELEVENLABS_API_KEY     → text mode with warning
  - httpx / sounddevice missing → text mode with install hint
  - No mic / no playback       → attempted run; errors surface clearly
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import wave

from sovereign_agent.session.directory import Session
from sovereign_agent.session.state import now_utc

from starter.voice_pipeline.manager_persona import ManagerPersona

# Audio config — 16 kHz mono PCM (matches ElevenLabs pcm_16000 TTS output)
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM
MAX_UTTERANCE_S = 15.0  # cap per-turn recording
SILENCE_TIMEOUT_S = 2.0  # consecutive silence to end an utterance

ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# Default ElevenLabs voice (George). Override with ELEVENLABS_VOICE_ID in .env.
DEFAULT_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"


# ---------------------------------------------------------------------------
# Text mode — reference implementation (read this first)
# ---------------------------------------------------------------------------
async def run_text_mode(session: Session, persona: ManagerPersona, max_turns: int = 6) -> None:
    """Conversation via stdin/stdout. Same trace-event shape as voice mode."""
    print("Text mode. Type a message to Alasdair (pub manager); blank line to quit.")
    print(f"Session: {session.session_id}")
    print("-" * 60)

    for turn_idx in range(max_turns):
        try:
            user_text = input("you> ").strip()
        except EOFError:
            break
        if not user_text:
            break

        session.append_trace_event(
            {
                "event_type": "voice.utterance_in",
                "actor": "user",
                "timestamp": now_utc().isoformat(),
                "payload": {"text": user_text, "turn": turn_idx, "mode": "text"},
            }
        )

        manager_text = await persona.respond(user_text)
        print(f"alasdair> {manager_text}")

        session.append_trace_event(
            {
                "event_type": "voice.utterance_out",
                "actor": "manager",
                "timestamp": now_utc().isoformat(),
                "payload": {"text": manager_text, "turn": turn_idx, "mode": "text"},
            }
        )

    print("-" * 60)
    print(f"Conversation ended. Trace: {session.trace_path}")


# ---------------------------------------------------------------------------
# Voice mode — ElevenLabs Scribe STT + ElevenLabs TTS (REST via httpx)
# ---------------------------------------------------------------------------
async def run_voice_mode(session: Session, persona: ManagerPersona, max_turns: int = 6) -> None:
    """Voice mode. Mic capture → ElevenLabs STT → manager → ElevenLabs TTS."""

    elevenlabs_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()

    if not elevenlabs_key:
        print(
            "⚠  ELEVENLABS_API_KEY not set — falling back to text mode.\n"
            "   Add to .env and re-run for real voice.",
            file=sys.stderr,
        )
        await run_text_mode(session, persona, max_turns=max_turns)
        return

    try:
        import httpx  # type: ignore[import-not-found]
        import sounddevice as sd  # type: ignore[import-not-found]
    except ImportError as e:
        print(
            f"⚠  Missing voice dep: {e.name}. Run:\n"
            "     make setup-voice\n"
            "   or: uv sync --extra voice\n"
            "   Falling back to text mode.",
            file=sys.stderr,
        )
        await run_text_mode(session, persona, max_turns=max_turns)
        return

    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", DEFAULT_VOICE_ID).strip() or DEFAULT_VOICE_ID

    print(f"🎙️  Voice mode (ElevenLabs). Session: {session.session_id}")
    print(f"    Voice ID: {voice_id}")
    print(f"    Speak when prompted. Silence for {SILENCE_TIMEOUT_S}s ends a turn.")
    print(f"    Max utterance: {MAX_UTTERANCE_S}s. Say 'goodbye' to end.")
    print("-" * 60)

    for turn_idx in range(max_turns):
        print(f"\n[turn {turn_idx + 1}] 🎤 listening...")

        try:
            audio_bytes = _record_until_silence(sd, session, turn_idx)
        except Exception as e:  # noqa: BLE001
            print(f"✗ mic capture failed: {e}", file=sys.stderr)
            print(
                "   macOS? Check System Settings → Privacy & Security → Microphone\n"
                "   and grant your terminal app access, then restart the terminal.",
                file=sys.stderr,
            )
            return

        if not audio_bytes:
            print("   (silence detected; ending conversation)")
            break

        try:
            user_text = await _transcribe_elevenlabs(audio_bytes, elevenlabs_key)
        except Exception as e:  # noqa: BLE001
            print(f"✗ STT failed: {e}", file=sys.stderr)
            print(
                "   Check ELEVENLABS_API_KEY (make educator-diagnostics).\n"
                "   Free tier has monthly caps; 401/403 usually means bad or exhausted key.",
                file=sys.stderr,
            )
            return

        user_text = user_text.strip()
        if not user_text:
            print("   (no transcript; ending conversation)")
            break

        print(f"   you> {user_text}")
        session.append_trace_event(
            {
                "event_type": "voice.utterance_in",
                "actor": "user",
                "timestamp": now_utc().isoformat(),
                "payload": {"text": user_text, "turn": turn_idx, "mode": "voice"},
            }
        )

        if user_text.lower().strip(".!?") in ("goodbye", "bye", "cheerio"):
            break

        manager_text = await persona.respond(user_text)
        print(f"   alasdair> {manager_text}")

        session.append_trace_event(
            {
                "event_type": "voice.utterance_out",
                "actor": "manager",
                "timestamp": now_utc().isoformat(),
                "payload": {"text": manager_text, "turn": turn_idx, "mode": "voice"},
            }
        )

        try:
            await _speak_elevenlabs(manager_text, elevenlabs_key, voice_id, sd)
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠ TTS playback failed: {e} (continuing)", file=sys.stderr)

    print("-" * 60)
    print(f"Conversation ended. Trace: {session.trace_path}")


# ---------------------------------------------------------------------------
# Audio capture
# ---------------------------------------------------------------------------
def _record_until_silence(sd, session: Session, turn: int) -> bytes:
    """Record from the default mic until SILENCE_TIMEOUT_S of silence or
    MAX_UTTERANCE_S hit. Returns raw 16-bit PCM @ SAMPLE_RATE mono.
    """
    import numpy as np

    threshold = 500
    chunk_ms = 100
    chunk_samples = int(SAMPLE_RATE * chunk_ms / 1000)
    silence_chunks_needed = int(SILENCE_TIMEOUT_S * 1000 / chunk_ms)

    captured: list[bytes] = []
    silence_chunks = 0
    total_ms = 0
    speech_started = False

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16") as stream:
        while True:
            data, _overflow = stream.read(chunk_samples)
            if hasattr(data, "tobytes"):
                raw = data.tobytes()
            else:
                raw = bytes(data)
            captured.append(raw)
            total_ms += chunk_ms

            arr = np.frombuffer(raw, dtype=np.int16)
            if arr.size == 0:
                rms = 0
            else:
                rms = int(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))

            if rms >= threshold:
                speech_started = True
                silence_chunks = 0
            else:
                silence_chunks += 1

            if speech_started and silence_chunks >= silence_chunks_needed:
                break
            if total_ms >= MAX_UTTERANCE_S * 1000:
                break
            if not speech_started and total_ms >= 3000:
                return b""

    audio_bytes = b"".join(captured)

    wav_path = session.workspace_dir / f"turn_{turn}_input.wav"
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_bytes)

    return audio_bytes


def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """Wrap raw PCM in a WAV container for ElevenLabs STT upload."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# ElevenLabs Scribe STT (REST)
# ---------------------------------------------------------------------------
async def _transcribe_elevenlabs(audio_bytes: bytes, api_key: str) -> str:
    """Transcribe captured PCM via ElevenLabs Speech-to-Text (Scribe v2)."""
    import httpx

    wav_bytes = _pcm_to_wav_bytes(audio_bytes)
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.post(
            ELEVENLABS_STT_URL,
            headers={"xi-api-key": api_key},
            files={"file": ("utterance.wav", wav_bytes, "audio/wav")},
            data={"model_id": "scribe_v2", "language_code": "eng"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs STT {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        text = payload.get("text") if isinstance(payload, dict) else None
        return str(text or "").strip()


# ---------------------------------------------------------------------------
# ElevenLabs TTS + playback (REST)
# ---------------------------------------------------------------------------
async def _speak_elevenlabs(text: str, api_key: str, voice_id: str, sd) -> None:
    """Synthesise speech with ElevenLabs and play through the default output device."""
    import httpx
    import numpy as np

    url = ELEVENLABS_TTS_URL.format(voice_id=voice_id)
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.post(
            url,
            params={"output_format": "pcm_16000"},
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/pcm",
            },
            json={"text": text, "model_id": "eleven_multilingual_v2"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs TTS {resp.status_code}: {resp.text[:300]}")
        pcm_bytes = resp.content

    if not pcm_bytes:
        raise RuntimeError("ElevenLabs TTS returned empty audio")

    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    if samples.size == 0:
        raise RuntimeError("ElevenLabs TTS decoded to zero samples")
    sd.play(samples, samplerate=SAMPLE_RATE)
    sd.wait()


__all__ = ["run_text_mode", "run_voice_mode"]
