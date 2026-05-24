# Ex8 — Voice pipeline

**You are building:** a conversational interface to a Llama-3.3-70B pub manager
persona, with real voice (ElevenLabs STT + TTS) or text-only fallback.

**Spec:** see `ASSIGNMENT.md` §Ex8.

**Time estimate:** 3-6 hours (voice mode is the wildcard).

## Modes

- **Text mode** (`--text`, default): stdin/stdout. Zero extra setup. Full
  credit for this mode alone is 16/20.
- **Voice mode** (`--voice`): mic → ElevenLabs Scribe STT → manager → ElevenLabs TTS.
  Requires `ELEVENLABS_API_KEY` in `.env`. Full credit is 20/20.

## Files

| File | What it is | Your job |
|---|---|---|
| `manager_persona.py` | Llama-3.3-70B pub-manager persona | Write the system prompt; wire the LLM client |
| `voice_loop.py` | STT → LLM → TTS loop | Implement text mode fully, voice mode if you have keys |
| `requirements-voice.txt` | Optional voice dep pins | — |

## How to run

```
make setup-voice     # once: elevenlabs + sounddevice
make ex8-text        # text mode (free)
make ex8-voice       # voice mode; falls back to text if ELEVENLABS_API_KEY missing
```

## Grading shape

Four evaluation dimensions:

1. **Conversation length & coherence** — at least 3 turns, the manager stays
   in character. Scored by an LLM-as-judge.
2. **Trace correctness** — every utterance appears as `voice.utterance_in` or
   `voice.utterance_out` in `logs/trace.jsonl`.
3. **Graceful degradation** — `--voice` with no key falls back with a clear
   warning, never crashes.
4. **Voice mode works end-to-end** — BONUS. You can get full marks without
   this if voice setup is impossible on your machine (e.g. no microphone).
