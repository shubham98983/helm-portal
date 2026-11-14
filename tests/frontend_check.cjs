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
let role = 'lead';
const requests = [];
window.fetch = async (url) => {
  requests.push(url);
  if (url === '/api/login') {
    loggedIn = true;
    return { ok: true, json: async () => ({ ok: true }) };
  }
  if (url === '/api/me') {
    return loggedIn
      ? { ok: true, json: async () => ({ username: role, name: 'Lead', role }) }
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
  if (url === '/api/project') {
    return { ok: true, json: async () => ({ repos: [{ full_name: 'org/repo' }] }) };
  }
  if (url.startsWith('/api/prs/board')) {
    const empty = { draft: [], ci: [], awaiting: [], ready: [], done: [] };
    empty.ready.push({
      number: 4, repo: 'org/repo', title: '<svg onload=alert(1)>Unsafe title', author: 'dev',
      branch: 'feature/x', is_ai: true, ai_reasons: ['branch'], is_bot: false,
      checks: { overall: 'passing', counts: { passing: 1 } },
      approvals: { required: [], approved_by: [], missing: [], stale_by: [], changes_requested_by: [] },
      url: 'javascript:alert(1)', updated_at: '2025-01-01',
    });
    return { ok: true, json: async () => ({ columns: empty }) };
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
  window.location.hash = '#/pull-requests';
  await tick(); await tick();
  assert.equal(window.document.querySelectorAll('.board-column').length, 5, 'all five board columns render');
  assert.equal(window.document.querySelectorAll('svg').length, 0, 'pull request titles are text');
  assert.equal(window.document.querySelectorAll('a[href^="javascript:"]').length, 0, 'unsafe URLs never become links');
  const aiToggle = window.document.querySelector('.toggle input');
  aiToggle.checked = true;
  aiToggle.dispatchEvent(new window.Event('change', { bubbles: true }));
  await tick(); await tick();
  assert.ok(requests.some((url) => url.includes('ai=1')), 'AI-only filter reloads the board');
  role = 'viewer';
  window.location.hash = '#/home';
  await tick();
  assert.equal([...window.document.querySelectorAll('.nav-link')].some((link) => link.textContent === 'Audit'), false,
    'audit navigation is hidden from viewer role');
}

run().then(() => dom.window.close()).catch((error) => { console.error(error); process.exitCode = 1; });
