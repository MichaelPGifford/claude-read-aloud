---
description: Read Claude's last reply aloud
---

Run exactly this one bash command, then reply with "🔊 reading aloud" —
plus, if the command printed a line starting with "note:", relay that line too:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/speak.py" --detach --project "$(pwd)"
```

If `python3` is not found, retry the same command with `python`.
