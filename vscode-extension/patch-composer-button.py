#!/usr/bin/env python3
"""OPTIONAL, EXPERIMENTAL: put a Read-aloud button inside Claude Code's chat
input, beside the mic — and capture right-click selections for "Read aloud".

This EDITS TWO FILES of the installed Claude Code VS Code extension. Originals
are backed up beside each file (*.cra-orig) and `--revert` restores them
exactly. Every Claude Code update replaces both files and removes the button;
the companion extension re-applies this automatically when its
`claudeReadAloud.composerButton` setting is on.

    patch-composer-button.py            apply
    patch-composer-button.py --status   check
    patch-composer-button.py --revert   restore originals

Why two edits (a webview is sandboxed):
  1. webview/index.js  — appended script inserts the button next to the mic and
     posts right-click selections to the companion extension's local server.
     Appended, never spliced: an upstream reshuffle can stop it matching, but
     it cannot corrupt 4.8MB of minified code.
  2. extension.js      — the Content-Security-Policy gains one directive,
     `connect-src http://127.0.0.1:48777`. Without it the button renders but
     every click is refused: the CSP is `default-src 'none'` with no
     connect-src at all. This is a real (if small) widening of that sandbox —
     one local port — and is the entire reason this ships opt-in.

The mic is found at runtime by aria-label ("Voice dictation"), messages by
data-testid="assistant-message" — never by CSS-module class names, which are
hashes that change every rebuild.
"""
import argparse
import os
import pathlib
import re
import shutil
import sys

PORT = 48777
SERVER = f"http://127.0.0.1:{PORT}"
MARKER = "/* claude-read-aloud composer v1 */"
FOREIGN = "/* claude-tts-button"        # a different local patch of the same files
BACKUP_SUFFIX = ".cra-orig"

CSP_FIND = "h=`worker-src ${e.cspSource}`"
CSP_REPLACE = "h=`worker-src ${e.cspSource}; connect-src " + SERVER + "`"

INJECTION = MARKER + """
(function () {
  var SERVER = '""" + SERVER + """';
  var ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none"' +
    ' stroke="currentColor" stroke-width="2" stroke-linecap="round"' +
    ' stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5z"/>' +
    '<path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a9 9 0 0 1 0 14"/></svg>';

  function findMics() {
    // Claude Code has more than one composer (the main chat box and the
    // floating "Ask Claude to edit…" editor box) — decorate every mic.
    return document.querySelectorAll(
      'button[aria-label*="Voice dictation" i],' +
      'button[aria-label*="dictation" i],' +
      'button[aria-label*="Microphone" i],' +
      'button[aria-label*="record" i]');
  }

  function selectedText() {
    try { return String(window.getSelection() || '').trim(); } catch (e) { return ''; }
  }

  function lastReplyText() {
    var nodes = document.querySelectorAll('[data-testid="assistant-message"]');
    if (!nodes.length) return '';
    return (nodes[nodes.length - 1].innerText || '').trim();
  }

  // Right-click: stash the selection so the extension's context-menu item
  // ("Read aloud") can ask its server to speak it. Fire-and-forget.
  document.addEventListener('contextmenu', function () {
    var s = selectedText();
    if (s) fetch(SERVER + '/selection', { method: 'POST', mode: 'cors', body: s })
      .catch(function () {});
  }, true);

  function ensure() {
    findMics().forEach(ensureOne);
  }

  function ensureOne(mic) {
    if (!mic || !mic.parentElement) return;
    var wrap = mic.parentElement;
    if (wrap.querySelector('.cra-btn')) return;

    // The mic wrapper is absolutely positioned with no layout of its own, so a
    // second child stacks ABOVE the mic instead of sitting beside it.
    if (getComputedStyle(wrap).display !== 'flex') {
      wrap.style.display = 'flex';
      wrap.style.alignItems = 'center';
    }

    var b = document.createElement('button');
    b.type = 'button';
    // Inherit the mic's own classes so it matches whatever the theme does.
    b.className = mic.className + ' cra-btn';
    b.setAttribute('aria-label', "Read aloud (selection, or Claude's last reply)");
    b.title = "Read aloud — highlighted text if any, else Claude's last reply";
    b.innerHTML = ICON;
    b.dataset.on = '0';

    // The composer focuses its textbox on mousedown; swallow it so clicking
    // this button neither steals the caret NOR collapses the selection.
    b.addEventListener('mousedown', function (e) {
      e.preventDefault();
      e.stopPropagation();
    });
    b.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      var on = b.dataset.on !== '1';
      b.dataset.on = on ? '1' : '0';
      b.style.opacity = on ? '0.55' : '';
      var done = function () { b.dataset.on = '0'; b.style.opacity = ''; };
      var fail = function () {
        done();
        b.title = 'Read-aloud server is not running — is the ' +
          'claude-read-aloud-button extension installed and its ' +
          'composerButton setting on?';
      };
      if (!on) { fetch(SERVER + '/stop', { mode: 'cors' }).catch(fail); return; }
      var text = selectedText() || lastReplyText();
      var req = text
        ? fetch(SERVER + '/speak', { method: 'POST', mode: 'cors', body: text })
        : fetch(SERVER + '/speak', { mode: 'cors' });   // transcript fallback
      req.catch(fail);
      setTimeout(done, 1000 * 600);
    });

    wrap.insertBefore(b, mic);
  }

  // React re-renders the composer constantly, so re-add on every mutation.
  function start() {
    try { new MutationObserver(ensure).observe(document.body, { childList: true, subtree: true }); } catch (e) {}
    ensure();
    setInterval(ensure, 2000);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
"""


def find_extension() -> pathlib.Path:
    override = os.environ.get("CLAUDE_CODE_EXT_DIR")
    if override:
        p = pathlib.Path(override)
        if (p / "webview" / "index.js").exists():
            return p
        sys.exit(f"CLAUDE_CODE_EXT_DIR does not look like the extension: {p}")
    home = pathlib.Path.home()
    candidates: list[pathlib.Path] = []
    for base in (home / ".vscode" / "extensions",
                 home / ".vscode-insiders" / "extensions",
                 home / ".vscode-server" / "extensions"):
        candidates += sorted(base.glob("anthropic.claude-code-*"))
    candidates = [c for c in candidates if (c / "webview" / "index.js").exists()]
    if not candidates:
        sys.exit("No Claude Code VS Code extension found. Is it installed?")
    return candidates[-1]          # highest version wins


def backup(p: pathlib.Path) -> None:
    b = p.with_suffix(p.suffix + BACKUP_SUFFIX)
    if not b.exists():
        shutil.copy2(p, b)


def status(ext: pathlib.Path) -> int:
    wv = (ext / "webview/index.js").read_text(encoding="utf-8", errors="replace")
    ej = (ext / "extension.js").read_text(encoding="utf-8", errors="replace")
    print(f"extension : {ext}")
    print(f"  button injected : {'yes' if MARKER in wv else 'NO'}")
    print(f"  csp widened     : {'yes' if SERVER in ej else 'NO'}")
    if FOREIGN in wv or FOREIGN in ej:
        print("  WARNING: a different patch of these files is present "
              "(claude-tts-button). Revert it with its own tool before applying this one.")
        return 2
    return 0 if (MARKER in wv and SERVER in ej) else 1


def apply(ext: pathlib.Path) -> None:
    wv_path, ej_path = ext / "webview/index.js", ext / "extension.js"
    wv = wv_path.read_text(encoding="utf-8", errors="replace")
    ej = ej_path.read_text(encoding="utf-8", errors="replace")

    if FOREIGN in wv or FOREIGN in ej:
        sys.exit("A different patch of these files is present (claude-tts-button). "
                 "Revert it with its own tool first — two injections would fight.")

    if SERVER in ej:
        print("  csp     : already widened")
    elif CSP_FIND in ej:
        backup(ej_path)
        ej_path.write_text(ej.replace(CSP_FIND, CSP_REPLACE, 1), encoding="utf-8")
        print("  csp     : widened (connect-src added)")
    else:
        # Fall back to any worker-src template if the variable was renamed.
        m = re.search(r"`worker-src \$\{[A-Za-z_$][\w$]*\.cspSource\}`", ej)
        if not m:
            sys.exit("  csp     : anchor not found — Claude Code's CSP layout changed. "
                     "Not applying half a patch; please file an issue.")
        backup(ej_path)
        new = m.group(0)[:-1] + "; connect-src " + SERVER + "`"
        ej_path.write_text(ej.replace(m.group(0), new, 1), encoding="utf-8")
        print(f"  csp     : widened via fallback anchor")

    if MARKER in wv:
        print("  webview : already injected")
    else:
        backup(wv_path)
        wv_path.write_text(wv.rstrip() + "\n;" + INJECTION, encoding="utf-8")
        print("  webview : button injected")


def revert(ext: pathlib.Path) -> None:
    for rel in ("webview/index.js", "extension.js"):
        p = ext / rel
        b = p.with_suffix(p.suffix + BACKUP_SUFFIX)
        if b.exists():
            shutil.copy2(b, p)
            print(f"  restored {rel}")
        else:
            print(f"  no backup for {rel} (nothing to revert)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    ext = find_extension()
    if args.status:
        sys.exit(status(ext))
    if args.revert:
        print(f"reverting {ext.name}")
        revert(ext)
        print("Done. Reload VS Code.")
    else:
        print(f"patching {ext.name}")
        apply(ext)
        print("\nDone. Reload VS Code: Ctrl+Shift+P → Developer: Reload Window")


if __name__ == "__main__":
    main()
