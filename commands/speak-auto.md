---
description: Toggle reading every reply aloud automatically
argument-hint: on | off
---

Run exactly this one bash command and show the user its output:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --auto $ARGUMENTS
```

If the user gave no argument, ask whether they want auto-read `on` or `off`
instead of guessing. Warning to relay when turning it on: replies arrive
frequently — most people prefer on-demand `/speak` after trying auto for a day.
