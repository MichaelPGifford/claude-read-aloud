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
MARKER = "/* claude-read-aloud composer v2 */"
# Any version of our own injection. The version in MARKER is what tells an
# upgrade that the button in the file is the OLD one and has to go: appending a
# second would leave two buttons, each with its own idea of what is playing.
MARKER_ANY = "/* claude-read-aloud composer"
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
  var IDLE = "Read aloud \u2014 highlighted text if any, else Claude's last reply";

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
    try {
      var sel = window.getSelection();
      if (!sel || sel.isCollapsed) return '';
      return String(sel).trim();
    } catch (e) { return ''; }
  }

  // The last reply in ONE pane: the newest message that is actually on screen
  // and has words in it. A reply still streaming is empty, and a pane scrolled
  // out of view is not what anyone means by "read this".
  function replyIn(root) {
    var nodes = root.querySelectorAll('[data-testid="assistant-message"]');
    for (var i = nodes.length - 1; i >= 0; i--) {
      if (!nodes[i].getClientRects().length) continue;
      var t = (nodes[i].innerText || '').trim();
      if (t) return t;
    }
    return '';
  }

  // …and the pane is THIS button's own: walk out from the button until an
  // ancestor holds a reply. One window holds more than one conversation, and a
  // document-wide query reads whichever message sits last in the DOM — which is
  // how a click in one place used to read something from another.
  function lastReplyText(btn) {
    for (var n = btn.parentElement; n && n !== document.body; n = n.parentElement) {
      var found = replyIn(n);
      if (found) return found;
    }
    return replyIn(document.body);
  }

  function paint(b, on) {
    b.dataset.on = on ? '1' : '0';
    b.style.opacity = on ? '0.55' : '';
    b.title = on ? 'Stop reading' : IDLE;
    b.setAttribute('aria-label', on ? 'Stop reading aloud' : IDLE);
  }

  function ask(route, body) {
    var opts = { mode: 'cors' };
    if (body !== undefined) { opts.method = 'POST'; opts.body = body; }
    return fetch(SERVER + route, opts);
  }

  function playing() {
    return ask('/status').then(function (r) { return r.json(); })
      .then(function (j) { return !!j.playing; });
  }

  // While a reading runs the button follows the server, not a timer. The old
  // button guessed with a three-minute timeout, so its idea of "reading" and
  // the machine's parted company, and the next click did the wrong one of the
  // two things it could do.
  function watch(b) {
    if (b.craWatch) return;
    b.craWatch = setInterval(function () {
      var done = function () { clearInterval(b.craWatch); b.craWatch = null; };
      if (!b.isConnected) { done(); return; }   // React replaced it mid-reading
      playing().then(function (on) {
        if (on) return;
        done(); paint(b, false);
      }).catch(function () { done(); paint(b, false); });
    }, 1000);
  }

  function unreachable(b) {
    paint(b, false);
    b.title = 'Read-aloud is not listening — is the claude-read-aloud-button ' +
      'extension installed, with its composerButton setting on?';
  }

  function speak(b, text) {
    paint(b, true);
    watch(b);
    ask('/speak', text).catch(function () { unreachable(b); });
  }

  function click(b) {
    var highlighted = selectedText();
    if (highlighted) {
      // A highlight always wins, and always interrupts: the server stops the
      // current reading before starting this one. Never a toggle — someone who
      // highlights a paragraph and presses play is asking for THAT, now.
      speak(b, highlighted);
      return;
    }
    playing().then(function (on) {
      if (on) { paint(b, false); ask('/stop').catch(function () {}); return; }
      var reply = lastReplyText(b);
      if (!reply) {
        // Nothing here to read. The old button fell back to "the newest
        // transcript on this machine", which on a busy one is another
        // project's session — a stranger's words out of nowhere.
        b.title = 'Nothing to read in this conversation yet.';
        return;
      }
      speak(b, reply);
    }).catch(function () { unreachable(b); });
  }

  // Right-click: stash the selection so the extension's context-menu item
  // ("Read aloud") can ask its server to speak it. Fire-and-forget.
  document.addEventListener('contextmenu', function () {
    var s = selectedText();
    if (s) ask('/selection', s).catch(function () {});
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
    b.innerHTML = ICON;
    paint(b, false);

    // The composer focuses its textbox on mousedown; swallow it so clicking
    // this button neither steals the caret NOR collapses the selection.
    b.addEventListener('mousedown', function (e) {
      e.preventDefault();
      e.stopPropagation();
    });
    b.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      click(b);
    });

    wrap.insertBefore(b, mic);
    // React rebuilds this button constantly, including in the middle of a
    // reading. Ask the machine what is true rather than starting from idle.
    playing().then(function (on) { if (on) { paint(b, true); watch(b); } })
      .catch(function () {});
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
    injected = ("yes" if MARKER in wv
                else "an older version — re-apply" if MARKER_ANY in wv else "NO")
    print(f"  button injected : {injected}")
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
        b = wv_path.with_suffix(wv_path.suffix + BACKUP_SUFFIX)
        if MARKER_ANY in wv:
            # Start from the pristine file rather than cutting the old block
            # out of 5MB of minified code by hand.
            if not b.exists():
                sys.exit("  webview : an older button is injected but its backup is "
                         "gone — reinstall Claude Code, then run this again.")
            wv = b.read_text(encoding="utf-8", errors="replace")
            print("  webview : older button removed")
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
