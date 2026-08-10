---
description: Show read-aloud configuration (provider, voice, auto-read)
---

Run exactly this one bash command and show the user its output verbatim:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --status
```

Then, if the user wants changes, edit the config file the output names —
fields: `provider` (system / speechify / elevenlabs / openai / command),
`voice`, `speed`, `auto_read`. API keys belong in environment variables
(`SPEECHIFY_API_KEY`, `ELEVENLABS_API_KEY`, `OPENAI_API_KEY`), not the file.
