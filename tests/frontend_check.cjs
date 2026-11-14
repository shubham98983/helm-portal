// Run with jsdom installed outside the project and NODE_PATH pointing at that install.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const root = path.resolve(__dirname, '..');
const dom = new JSDOM(fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8'), {
  url: 'http://helm.test/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
});
const { window } = dom;
const script = fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8');
let loggedIn = false;
window.fetch = async (url) => {
  if (url === '/api/login') {
    loggedIn = true;
    return { ok: true, json: async () => ({ ok: true }) };
  }
  if (url === '/api/me') {
    return loggedIn
      ? { ok: true, json: async () => ({ username: 'lead', name: 'Lead', role: 'lead' }) }
      : { ok: false, status: 401, json: async () => ({ error: 'Authentication required' }) };
  }
  if (url === '/api/team') {
    return {
      ok: true,
      json: async () => ({
        tree: [{
          github: 'lead', name: '<img src=x onerror=alert(1)>', role: 'lead', title: 'Lead',
          responsibilities: [], children: [], ai_agents: [],
        }],
        ai_agents: [],
      }),
    };
  }
  return { ok: true, json: async () => ({ ok: true }) };
};

async function tick() { await new Promise((resolve) => setTimeout(resolve, 10)); }
async function run() {
  window.eval(script);
  await tick();
  assert.ok(window.document.querySelector('#login-form'), 'unauthenticated users see login');
  window.document.querySelector('#username').value = 'lead';
  window.document.querySelector('#password').value = 'secret';
  window.document.querySelector('#login-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await tick();
  window.location.hash = '#/team';
  await tick();
  await tick();
  assert.match(window.document.querySelector('h1').textContent, /Team/);
  assert.equal(window.document.querySelectorAll('img').length, 0, 'config text is never parsed as HTML');
  assert.equal(window.document.querySelector('.member').textContent.includes('<img src=x onerror=alert(1)>'), true);
}

run().then(() => dom.window.close()).catch((error) => { console.error(error); process.exitCode = 1; });
