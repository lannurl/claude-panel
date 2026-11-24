// Снимает кадры офиса: безголовый Edge/Chrome открывает generator.html, забирает картинки в assets/office.
//   node tools/office3d/render.mjs                 — всё
//   node tools/office3d/render.mjs --only=igris    — один агент в preview/ (для проверки)
// Браузер запускается со своим временным профилем и закрывается в конце (только свой процесс).
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { tmpdir } from 'node:os';

const here = dirname(fileURLToPath(import.meta.url));
const args = Object.fromEntries(process.argv.slice(2).map((a) => a.replace(/^--/, '').split('=')));
const outDir = args.only ? join(here, 'preview') : resolve(here, '..', '..', 'assets', 'office');
mkdirSync(outDir, { recursive: true });
const BROWSERS = ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Google/Chrome/Application/chrome.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/usr/bin/chromium', '/usr/bin/google-chrome'];
const exe = BROWSERS.find((p) => existsSync(p));
if (!exe) { console.error('Нужен Edge или Chrome'); process.exit(1); }
const port = 9400 + Math.floor(Math.random() * 400);
const profile = join(tmpdir(), `office3d-${process.pid}`);
const q = new URLSearchParams(Object.entries(args).filter(([k]) => ['only', 'h', 'yaw', 'pitch'].includes(k))).toString();
const url = pathToFileURL(join(here, 'generator.html')).href + (q ? `?${q}` : '');
const br = spawn(exe, ['--headless=new', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--hide-scrollbars', '--allow-file-access-from-files',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, url], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let ws, id = 0; const pend = new Map();
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const evalJs = async (expression) => { const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); return r.result?.result?.value; };
async function finish(code) { try { await send('Browser.close'); } catch {} await sleep(800); try { br.kill(); } catch {} try { rmSync(profile, { recursive: true, force: true }); } catch {} process.exit(code); }
try {
  let page;
  for (let i = 0; i < 60 && !page; i++) { try { const t = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); page = t.find((x) => x.type === 'page' && x.url.startsWith('file:')); } catch {} if (!page) await sleep(500); }
  if (!page) throw new Error('браузер не открыл страницу');
  ws = new WebSocket(page.webSocketDebuggerUrl); await new Promise((r) => ws.addEventListener('open', r));
  ws.addEventListener('message', (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } });
  const t0 = Date.now(); let last = '';
  for (;;) {
    const st = await evalJs(`JSON.stringify({done: !!window.__result, err: window.__error || null, log: document.getElementById('log')?.textContent})`);
    const s = JSON.parse(st || '{}');
    if (s.err) throw new Error(s.err);
    if (s.log !== last) { console.log(`[${((Date.now() - t0) / 1000).toFixed(0)} с] ${s.log}`); last = s.log; }
    if (s.done) break;
    if (Date.now() - t0 > 20 * 60 * 1000) throw new Error('слишком долго');
    await sleep(1000);
  }
  const names = JSON.parse(await evalJs('JSON.stringify(Object.keys(window.__result.files))'));
  for (const n of names) {
    const data = await evalJs(`window.__result.files[${JSON.stringify(n)}]`);
    writeFileSync(join(outDir, n), Buffer.from(data.split(',')[1], 'base64'));
  }
  const meta = await evalJs('JSON.stringify(window.__result.meta)');
  writeFileSync(join(outDir, args.only ? 'preview.json' : 'office.json'), JSON.stringify(JSON.parse(meta)));
  console.log(`записано в ${outDir}: ${names.length} картинок`);
  await finish(0);
} catch (e) { console.error('ошибка:', e.message || e); await finish(1); }
