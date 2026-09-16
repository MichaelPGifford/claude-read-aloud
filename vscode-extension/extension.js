const vscode = require('vscode');
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');

// The composer button injected into Claude Code's webview (an opt-in patch —
// see patch-composer-button.py) can only reach us over HTTP: a webview cannot
// spawn processes. This extension therefore hosts a tiny localhost server.
// The port is fixed because the patched CSP names it literally.
const PORT = 48777;
const MARKER = 'claude-read-aloud composer v2';
// The one place that says whether a reading is in progress. speak.py claims
// this file when a run starts and drops it when the audio ends — whichever
// entry point started it: this button, the hotkey, a slash command, the hook.
const PIDFILE = path.join(os.tmpdir(), 'claude-read-aloud.pid');

let playing = false;
let status;
let server = null;
let child = null;
let selection = { text: '', at: 0 };

// ---------------------------------------------------------------- speak plumbing

function findScript() {
  const configured = vscode.workspace.getConfiguration('claudeReadAloud').get('script');
  if (configured && fs.existsSync(configured)) return configured;
  // Auto-detect the installed plugin: walk ~/.claude/plugins a few levels deep
  // looking for directories whose plugin manifest names "read-aloud". Old
  // version directories linger in the cache after updates, so collect every
  // match and take the highest path — version dirs sort lexically.
  const roots = [path.join(os.homedir(), '.claude', 'plugins')];
  const matches = [];
  let visited = 0;
  while (roots.length && visited < 500) {
    const dir = roots.pop();
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
    for (const e of entries) {
      if (!e.isDirectory()) continue;
      visited += 1;
      const p = path.join(dir, e.name);
      const candidate = path.join(p, 'scripts', 'speak.py');
      const manifest = path.join(p, '.claude-plugin', 'plugin.json');
      if (fs.existsSync(candidate) && fs.existsSync(manifest)) {
        try {
          if (JSON.parse(fs.readFileSync(manifest, 'utf8')).name === 'read-aloud') {
            matches.push(candidate);
          }
        } catch { /* not ours */ }
      }
      roots.push(p);
    }
  }
  return matches.sort().pop() || null;
}

function pythonBin() {
  const configured = vscode.workspace.getConfiguration('claudeReadAloud').get('python');
  if (configured) return configured;
  return process.platform === 'win32' ? 'python' : 'python3';
}

function setPlaying(on) {
  playing = on;
  status.text = on ? '$(mute) Stop reading' : '$(unmute) Read aloud';
  status.command = on ? 'claudeReadAloud.stop' : 'claudeReadAloud.speak';
  status.backgroundColor = new vscode.ThemeColor(
    on ? 'statusBarItem.warningBackground' : 'statusBarItem.prominentBackground');
}

function run(args, { stdinText, onExit } = {}) {
  const script = findScript();
  if (!script) {
    vscode.window.showErrorMessage(
      'claude-read-aloud plugin not found. Install it in Claude Code first, ' +
      'or set claudeReadAloud.script to the path of speak.py.');
    setPlaying(false);
    return;
  }
  const p = spawn(pythonBin(), [script, ...args]);
  if (stdinText !== undefined) {
    p.stdin.write(stdinText);
    p.stdin.end();
  }
  let stderr = '';
  p.stderr.on('data', d => { stderr += d.toString(); });
  p.on('error', e => {
    vscode.window.showErrorMessage(`Read aloud failed to start: ${e.message}`);
    setPlaying(false);
  });
  p.on('exit', code => {
    if (code !== 0 && stderr.trim()) {
      vscode.window.showWarningMessage(`Read aloud: ${stderr.trim().split('\n')[0]}`);
    }
    if (onExit) onExit(code);
  });
  return p;
}

function isPlaying() {
  // Our own reader counts even before it has claimed the pidfile — the first
  // seconds of a run go on loading a voice, and a click lands in them.
  if (child && child.exitCode === null && !child.killed) return true;
  try {
    const pid = parseInt(fs.readFileSync(PIDFILE, 'utf8').trim(), 10);
    if (!pid) return false;
    process.kill(pid, 0);                 // signal 0 only asks: still alive?
    return true;
  } catch { return false; }               // no file, or it died mid-reading
}

function stopNow() {
  // Kill our own reader by handle, not through the pidfile: a reader still
  // starting up has not claimed the file yet, and a stop that only reads the
  // file would miss it and leave two voices running over each other.
  if (child && child.exitCode === null) { try { child.kill('SIGTERM'); } catch { /* gone */ } }
  child = null;
  try {
    const pid = parseInt(fs.readFileSync(PIDFILE, 'utf8').trim(), 10);
    if (pid) process.kill(pid, 'SIGTERM');
    fs.unlinkSync(PIDFILE);
  } catch { /* nothing was reading */ }
}

function projectArgs() {
  const ws = vscode.workspace.workspaceFolders;
  return ws && ws.length ? ['--project', ws[0].uri.fsPath] : [];
}

// Every reading goes through here: stop what is playing, then start. A new
// reading always REPLACES the old one — two readings at once are unlistenable,
// and asking for one is the plainest way a person says "stop that".
function speakWith(args, stdinText) {
  stopNow();
  setPlaying(true);
  status.text = '$(loading~spin) Reading…';
  let mine;
  mine = run(args, {
    stdinText,
    // Only the reading that is still current may reset the button: a reader we
    // just killed exits a moment later, and its exit is not the end of reading.
    onExit: () => { if (child === mine) { child = null; setPlaying(false); } },
  });
  child = mine;
}

function speakTranscript() {
  speakWith(projectArgs());
}

function speakText(text) {
  speakWith(['--stdin'], text);
}

function stop() {
  stopNow();
  setPlaying(false);
}

function runCapture(args) {
  return new Promise((resolve, reject) => {
    const script = findScript();
    if (!script) return reject(new Error('plugin not found'));
    const p = spawn(pythonBin(), [script, ...args]);
    let out = '', err = '';
    p.stdout.on('data', d => { out += d.toString(); });
    p.stderr.on('data', d => { err += d.toString(); });
    p.on('error', reject);
    p.on('exit', code => code === 0 ? resolve(out)
      : reject(new Error(err.trim().split('\n')[0] || `exit ${code}`)));
  });
}

async function pickVoice() {
  let data;
  try {
    data = JSON.parse(await runCapture(['--list-voices']));
  } catch (e) {
    vscode.window.showErrorMessage(`Couldn't list voices: ${e.message}`);
    return;
  }
  const items = data.voices.map(v => ({
    label: v.label || v.id,
    description: (v.label && v.label !== v.id) ? v.id : '',
    id: v.id,
  }));
  const pick = await vscode.window.showQuickPick(items, {
    placeHolder: `${data.provider} voices — picking one sets it and plays a sample`,
    matchOnDescription: true,
  });
  if (!pick) return;
  try {
    await runCapture(['--set-voice', pick.id]);
    speakText(`This is ${pick.label.split('(')[0].trim()}. ` +
              "I'll be reading Claude's replies from now on.");
  } catch (e) {
    vscode.window.showErrorMessage(`Couldn't set voice: ${e.message}`);
  }
}

function speakSelection() {
  // The selection lives inside Claude Code's webview, which this extension
  // cannot read. The injected composer script posts it to our server on every
  // right-click; this command just speaks what was cached.
  if (selection.text && Date.now() - selection.at < 60_000) {
    speakText(selection.text);
  } else {
    vscode.window.showInformationMessage(
      'Nothing to read — highlight some text in Claude Code first. ' +
      '(This needs the composer-button patch: run "Claude Read Aloud: ' +
      'Install in-chat button".)');
  }
}

// ------------------------------------------------------- localhost server :48777

function ensureServer() {
  if (server) return;
  server = http.createServer((req, res) => {
    const cors = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };
    if (req.method === 'OPTIONS') { res.writeHead(204, cors); res.end(); return; }

    const route = (req.url || '').split('?')[0];
    const body = [];
    let size = 0;
    req.on('data', c => { size += c.length; if (size <= 60_000) body.push(c); });
    req.on('end', () => {
      const text = Buffer.concat(body).toString('utf8').trim();
      if (route === '/status') {
        res.writeHead(200, { ...cors, 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ playing: isPlaying() }));
        return;
      }
      if (route === '/speak' && req.method === 'POST' && text) speakText(text);
      else if (route === '/speak') speakTranscript();
      else if (route === '/stop') stop();
      else if (route === '/selection' && req.method === 'POST') {
        selection = { text, at: Date.now() };
      } else if (route === '/speak-selection') {
        if (selection.text && Date.now() - selection.at < 60_000) speakText(selection.text);
        else { res.writeHead(410, cors); res.end(); return; }
      } else { res.writeHead(404, cors); res.end(); return; }
      res.writeHead(204, cors);
      res.end();
    });
  });
  server.on('error', e => {
    // A second VS Code window finds the port taken by the first. That window
    // hosts the reading for both, and every click carries its own text, so
    // standing down quietly costs nothing — a popup per window would not.
    if (e.code !== 'EADDRINUSE') {
      vscode.window.showWarningMessage(`Read aloud server: ${e.message}`);
    }
    server = null;
  });
  server.listen(PORT, '127.0.0.1');
}

// --------------------------------------------------- composer-button patch upkeep

function claudeCodeExtDir() {
  // Same override the patcher honors — lets tests (and unusual installs)
  // point both tools at one specific Claude Code extension directory.
  const override = process.env.CLAUDE_CODE_EXT_DIR;
  if (override && fs.existsSync(path.join(override, 'webview', 'index.js'))) {
    return override;
  }
  const bases = [
    path.join(os.homedir(), '.vscode', 'extensions'),
    path.join(os.homedir(), '.vscode-insiders', 'extensions'),
    path.join(os.homedir(), '.vscode-server', 'extensions'),
  ];
  const hits = [];
  for (const base of bases) {
    let entries;
    try { entries = fs.readdirSync(base); } catch { continue; }
    for (const e of entries) {
      if (e.startsWith('anthropic.claude-code-') &&
          fs.existsSync(path.join(base, e, 'webview', 'index.js'))) {
        hits.push(path.join(base, e));
      }
    }
  }
  return hits.sort().pop() || null;
}

function patchIsApplied() {
  const dir = claudeCodeExtDir();
  if (!dir) return true;   // nothing to patch; stay quiet
  try {
    return fs.readFileSync(path.join(dir, 'webview', 'index.js'), 'utf8').includes(MARKER);
  } catch { return true; }
}

function runPatcher(args, done) {
  const patcher = path.join(__dirname, 'patch-composer-button.py');
  const p = spawn(pythonBin(), [patcher, ...args]);
  let out = '';
  p.stdout.on('data', d => { out += d.toString(); });
  p.stderr.on('data', d => { out += d.toString(); });
  p.on('exit', code => done(code, out.trim()));
  p.on('error', e => done(1, e.message));
}

async function ensurePatched() {
  if (patchIsApplied()) return;
  runPatcher([], async (code, out) => {
    if (code !== 0) {
      vscode.window.showWarningMessage(`Composer button patch failed: ${out.split('\n').pop()}`);
      return;
    }
    // A Claude Code update replaced the patched files; we just re-applied.
    const pick = await vscode.window.showInformationMessage(
      'Read-aloud composer button re-applied after a Claude Code update. ' +
      'Reload to see it.', 'Reload Window');
    if (pick) vscode.commands.executeCommand('workbench.action.reloadWindow');
  });
}

async function installComposerButton(alreadyConsented) {
  if (!alreadyConsented) {
    const pick = await vscode.window.showWarningMessage(
      'This patches the installed Claude Code extension (two files, backed up, ' +
      'fully revertible) to add a Read-aloud button beside the mic and enable ' +
      'right-click "Read aloud". Updates remove it; this extension re-applies it. ' +
      'Proceed?', { modal: true }, 'Patch Claude Code');
    if (!pick) return;
  }
  await vscode.workspace.getConfiguration('claudeReadAloud')
    .update('composerButton', true, vscode.ConfigurationTarget.Global);
  ensureServer();
  runPatcher([], async (code, out) => {
    if (code !== 0) {
      vscode.window.showErrorMessage(`Patch failed: ${out.split('\n').pop()}`);
      return;
    }
    const r = await vscode.window.showInformationMessage(
      'Composer button installed. Reload to see it.', 'Reload Window');
    if (r) vscode.commands.executeCommand('workbench.action.reloadWindow');
  });
}

async function removeComposerButton() {
  await vscode.workspace.getConfiguration('claudeReadAloud')
    .update('composerButton', false, vscode.ConfigurationTarget.Global);
  runPatcher(['--revert'], async (code, out) => {
    const r = await vscode.window.showInformationMessage(
      code === 0 ? 'Composer button removed; originals restored. Reload to finish.'
                 : `Revert reported: ${out.split('\n').pop()}`, 'Reload Window');
    if (r) vscode.commands.executeCommand('workbench.action.reloadWindow');
  });
}

async function offerComposerButton(context) {
  // The in-chat button is the best part of this extension, but it patches
  // Claude Code's files, so it must be consented — never silent. This offer
  // is that consent, surfaced on startup so nobody has to discover a command.
  const cfg = vscode.workspace.getConfiguration('claudeReadAloud');
  if (cfg.get('composerButton')) return;
  if (context.globalState.get('cra.dontAsk')) return;
  if (!claudeCodeExtDir() || patchIsApplied()) return;

  const pick = await vscode.window.showInformationMessage(
    'Read Aloud: add a speaker button inside Claude Code\'s chat box, beside ' +
    'the mic? This patches the Claude Code extension locally (backed up, fully ' +
    'revertible, re-applied after updates).',
    'Add the button', 'Don\'t ask again');
  if (pick === 'Add the button') {
    await cfg.update('composerButton', true, vscode.ConfigurationTarget.Global);
    ensureServer();
    installComposerButton(true);
  } else if (pick === 'Don\'t ask again') {
    await context.globalState.update('cra.dontAsk', true);
  }
  // Plain dismissal: offer again next window — the command palette always works.
}

// ------------------------------------------------- settings panel (sidebar view)

// Contributed INTO Claude Code's own sidebar container, so read-aloud settings
// live where the user already is. VS Code lets users drag the view elsewhere.
class ReadAloudViewProvider {
  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = panelHtml();
    view.webview.onDidReceiveMessage(m => this.onMessage(m));
  }

  async sendConfig() {
    try {
      const cfg = JSON.parse(await runCapture(['--get-config']));
      cfg.composer = !!vscode.workspace.getConfiguration('claudeReadAloud')
        .get('composerButton');
      this.view?.webview.postMessage({ type: 'config', cfg });
    } catch (e) {
      this.view?.webview.postMessage({ type: 'error', text: e.message });
    }
  }

  async onMessage(m) {
    const err = e => vscode.window.showErrorMessage(`Read aloud: ${e.message}`);
    switch (m.type) {
      case 'init': this.sendConfig(); break;
      case 'speak': speakTranscript(); break;
      case 'stop': stop(); break;
      case 'test': speakText('This is your current reading voice.'); break;
      case 'changeVoice': await pickVoice(); this.sendConfig(); break;
      case 'provider':
        await runCapture(['--set-provider', m.value]).catch(err);
        this.sendConfig();
        break;
      case 'speed':
        await runCapture(['--set-speed', String(m.value)]).catch(err);
        break;
      case 'auto':
        await runCapture(['--auto', m.value ? 'on' : 'off']).catch(err);
        break;
      case 'composer':
        if (m.value) installComposerButton(); else removeComposerButton();
        break;
      case 'kokoro':
        vscode.window.withProgress(
          { location: vscode.ProgressLocation.Notification,
            title: 'Installing Kokoro voice (~340MB, a few minutes)…' },
          () => runCapture(['--setup', 'kokoro']))
          .then(() => this.sendConfig(), err);
        break;
    }
  }
}

function panelHtml() {
  return `<!DOCTYPE html><html><body style="font-family:var(--vscode-font-family);
color:var(--vscode-foreground);padding:4px 2px">
<style>
  button{background:var(--vscode-button-secondaryBackground);
    color:var(--vscode-button-secondaryForeground);border:none;border-radius:3px;
    padding:4px 10px;cursor:pointer;margin:2px 2px 2px 0}
  button.primary{background:var(--vscode-button-background);
    color:var(--vscode-button-foreground)}
  select,input[type=range]{width:100%;margin:2px 0 8px}
  select{background:var(--vscode-dropdown-background);
    color:var(--vscode-dropdown-foreground);
    border:1px solid var(--vscode-dropdown-border);padding:3px}
  label{display:block;margin-top:8px;opacity:.85;font-size:.92em}
  .row{margin:6px 0}
  .voice{font-weight:600}
  .hint{opacity:.7;font-size:.85em;margin-top:2px}
</style>
<div class="row">
  <button class="primary" id="speak">🔊 Read last reply</button>
  <button id="stopb">Stop</button>
</div>
<label>Voice</label>
<div class="row"><span class="voice" id="voice">…</span></div>
<div class="row">
  <button id="change">Change voice…</button>
  <button id="test">Test</button>
</div>
<label for="provider">Provider</label>
<select id="provider">
  <option value="system">System (free, built in)</option>
  <option value="kokoro">Kokoro (free, neural)</option>
  <option value="speechify">Speechify (API key)</option>
  <option value="elevenlabs">ElevenLabs (API key)</option>
  <option value="openai">OpenAI (API key)</option>
</select>
<div class="hint" id="provHint"></div>
<label for="speed">Speed <span id="speedv"></span></label>
<input type="range" id="speed" min="0.7" max="1.5" step="0.05">
<div class="row"><label style="display:inline"><input type="checkbox" id="auto">
 Read every reply automatically</label></div>
<div class="row"><label style="display:inline"><input type="checkbox" id="composer">
 Speaker button inside the chat box</label></div>
<script>
const vs = acquireVsCodeApi();
const $ = id => document.getElementById(id);
$('speak').onclick = () => vs.postMessage({type:'speak'});
$('stopb').onclick = () => vs.postMessage({type:'stop'});
$('test').onclick = () => vs.postMessage({type:'test'});
$('change').onclick = () => vs.postMessage({type:'changeVoice'});
$('provider').onchange = e => vs.postMessage({type:'provider', value:e.target.value});
$('speed').oninput = e => { $('speedv').textContent = e.target.value + '×'; };
$('speed').onchange = e => vs.postMessage({type:'speed', value:parseFloat(e.target.value)});
$('auto').onchange = e => vs.postMessage({type:'auto', value:e.target.checked});
$('composer').onchange = e => vs.postMessage({type:'composer', value:e.target.checked});
window.addEventListener('message', ev => {
  const m = ev.data;
  if (m.type !== 'config') return;
  const c = m.cfg;
  $('voice').textContent = c.voice || '(provider default)';
  $('provider').value = c.provider;
  $('speed').value = c.speed || 1;
  $('speedv').textContent = (c.speed || 1) + '×';
  $('auto').checked = !!c.auto_read;
  $('composer').checked = !!c.composer;
  let hint = '';
  if (c.provider === 'kokoro' && !c.kokoro_ready) {
    hint = 'Kokoro is not installed yet — <a href="#" id="kok">install it now</a> (~340MB, free).';
  } else if (['speechify','elevenlabs','openai'].includes(c.provider) && !c.keys[c.provider]) {
    hint = 'No ' + c.provider + ' API key found — set ' + c.provider.toUpperCase() + '_API_KEY.';
  }
  $('provHint').innerHTML = hint;
  const k = document.getElementById('kok');
  if (k) k.onclick = () => vs.postMessage({type:'kokoro'});
});
vs.postMessage({type:'init'});
</script></body></html>`;
}

// ------------------------------------------------------------------------ lifecycle

function activate(context) {
  // Bottom-LEFT, highest priority: nearest the chat input, where eyes already are.
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100000);
  setPlaying(false);
  status.show();

  context.subscriptions.push(
    status,
    vscode.commands.registerCommand('claudeReadAloud.speak', speakTranscript),
    vscode.commands.registerCommand('claudeReadAloud.stop', stop),
    vscode.commands.registerCommand('claudeReadAloud.speakSelection', speakSelection),
    vscode.commands.registerCommand('claudeReadAloud.pickVoice', pickVoice),
    vscode.commands.registerCommand('claudeReadAloud.installComposerButton', installComposerButton),
    vscode.commands.registerCommand('claudeReadAloud.removeComposerButton', removeComposerButton),
  );

  const panelProvider = new ReadAloudViewProvider();
  for (const id of ['claudeReadAloud.panel', 'claudeReadAloud.panelSecondary',
                    'claudeReadAloud.panelSessions']) {
    context.subscriptions.push(
      vscode.window.registerWebviewViewProvider(id, panelProvider));
  }

  if (vscode.workspace.getConfiguration('claudeReadAloud').get('composerButton')) {
    ensureServer();
    ensurePatched();
  } else {
    offerComposerButton(context);
  }
}

function deactivate() {
  if (playing) stop();
  if (server) { server.close(); server = null; }
}

module.exports = { activate, deactivate };
