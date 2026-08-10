---
description: Install the free Kokoro neural voice (one-time ~340MB download; big upgrade on Linux)
---

Run this bash command with a **10-minute timeout** and relay its progress
lines to the user as they appear (it creates a private virtualenv, installs
kokoro-onnx, and downloads the voice model — a few minutes on first run):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --setup kokoro
```

When it finishes successfully, demo the new voice:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --detach --text "Kokoro is installed. This is your new reading voice."
```

Then tell the user setup is done and that `voice` in the config file the
output named can be any of the 54 Kokoro voices (am_michael, af_heart,
bf_emma, …). If either step fails, show the error and do not guess at fixes.
