---
description: Pick the reading voice — list, audition, set
argument-hint: [voice id to set directly]
---

If the user supplied an argument, treat it as a voice id: run step 3 with it.

1. List what's available:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --list-voices
```

Show the result as a short readable list (label and id). If it's long, show
the first ~20 and say how many more there are; offer to filter by name.

2. When the user wants to hear one, audition it WITHOUT saving:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --detach --voice VOICE_ID --text "This is how I'd sound reading your replies."
```

Repeat for as many voices as they like.

3. When they choose, save it and confirm in the new voice:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --set-voice VOICE_ID
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --detach --text "Voice saved. This is me from now on."
```

Voices belong to the current provider (see `/read-aloud:speak-status`). To
change provider, edit the config file it names — or `/read-aloud:voice-setup`
installs the free Kokoro voices.
