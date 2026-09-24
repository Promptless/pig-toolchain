// Cursor waits for stop hooks. Only bounded stdin and a detached spawn belong here.
'use strict';
const {spawn} = require('node:child_process');
const lifecycle = process.argv[2];
const finish = () => process.exit(0);
if (!['session_start', 'stop', 'subagent_stop', 'session_end'].includes(lifecycle)) finish();
if (process.argv[3] !== '--background') {
  const chunks = [];
  let size = 0;
  setTimeout(finish, 180).unref();
  process.stdin.on('error', finish);
  process.stdin.on('data', chunk => {
    size += chunk.length;
    if (size > 65536) finish();
    chunks.push(chunk);
  });
  process.stdin.on('end', () => {
    let payload;
    try {
      const input = JSON.parse(Buffer.concat(chunks));
      payload = JSON.stringify(Object.fromEntries(
        ['conversation_id', 'generation_id', 'transcript_path', 'agent_transcript_path',
          'parent_conversation_id', 'agent_id'].filter(key =>
            typeof input[key] === 'string' && input[key].length <= 4096).map(key => [key, input[key]])
      ));
    } catch { return finish(); }
    // Stay below Windows' command-line limit, even after base64 expansion.
    if (Buffer.byteLength(payload) > 16384) return finish();
    const child = spawn(process.execPath, [__filename, lifecycle, '--background',
      Buffer.from(payload).toString('base64')], {detached: true, stdio: 'ignore', windowsHide: true});
    child.on('error', finish);
    child.unref();
    finish();
  });
} else {
  // Interpreter discovery and all durable work happen after Cursor's hook has exited.
  const path = require('node:path');
  const fs = require('node:fs');
  const os = require('node:os');
  const candidates = process.platform === 'win32' ? [['py', '-3'], ['python'], ['python3']] : [['python3'], ['python']];
  const body = Buffer.from(process.argv[4] || '', 'base64');
  const root = path.dirname(__dirname);
  function recordStatus(status) {
    const directory = path.join(os.homedir(), '.promptless', 'instruction-hub');
    try {
      fs.mkdirSync(directory, {recursive: true, mode: 0o700});
      fs.writeFileSync(path.join(directory, 'cursor-launcher-status.json'),
        JSON.stringify({status, observed_at: new Date().toISOString()}), {mode: 0o600});
    } catch { /* Never affect the editor when local diagnostics cannot be saved. */ }
  }
  function runNext() {
    const command = candidates.shift();
    if (!command) return recordStatus('python_unavailable');
    const probe = spawn(command[0], [...command.slice(1), '-c',
      'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'], {stdio: 'ignore', windowsHide: true});
    const timer = setTimeout(() => probe.kill(), 2000);
    let settled = false;
    const probed = code => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code !== 0) return runNext();
      launch(command);
    };
    probe.on('error', () => probed(1));
    probe.on('exit', probed);
  }
  function launch(command) {
    const child = spawn(command[0], [...command.slice(1), path.join(__dirname, 'promptless-host-runtime'),
      'cursor-notify', '--lifecycle', lifecycle], {
      stdio: ['pipe', 'ignore', 'ignore'], windowsHide: true,
      env: {...process.env, CURSOR_PLUGIN_ROOT: root}
    });
    let timedOut = false;
    const watchdog = setTimeout(() => {
      timedOut = true;
      recordStatus('collector_timeout');
      child.kill('SIGKILL');
    }, 120000);
    child.on('exit', code => {
      clearTimeout(watchdog);
      if (!timedOut) recordStatus(code === 0 ? 'completed' : 'collector_failed');
    });
    child.on('error', () => { clearTimeout(watchdog); runNext(); });
    child.stdin.on('error', () => {});
    child.stdin.end(body);
  }
  runNext();
}
