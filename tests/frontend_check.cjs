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
    return { ok: true, json: async () => ({
      profile: { name: 'Helm', tagline: 'Tag', purpose: 'Purpose', phase: 'Build', tech_stack: [],
        policies: {}, environment: [], runbook: [], links: [] },
      repos: [{ full_name: 'org/repo', role: 'Primary' }], milestones: [], milestone_progress: 0,
    }) };
  }
  if (url === '/api/overview') return { ok: true, json: async () => ({
    main_ci: { conclusion: 'success', age: '1h ago', html_url: 'javascript:alert(1)' },
    open_prs: { ai: 1, human: 2 }, awaiting_approval: 1,
    ai_changes_this_week: { opened: 1, merged: 0 }, open_issues: 1, copilot: null,
    needs_attention: [{ title: 'Review needed', detail: 'Waiting on bob', url: 'https://github.com/org/repo/pull/4' }], errors: {},
  }) };
  if (url.startsWith('/api/work/issues')) return { ok: true, json: async () => ({
    issues: [{ number: 8, repo: 'org/repo', title: '<img src=x onerror=alert(1)> Work item',
      state: 'open', column: 'todo', url: 'javascript:alert(1)', created_at: '2026-11-14T00:00:00Z',
      assignees: ['bob'], labels: ['status:todo'], linked_pr: 4 }], errors: {},
  }) };
  if (url.startsWith('/api/activity')) return { ok: true, json: async () => ({
    events: [{ kind: 'copilot_failure', actor: 'AI DevOps Copilot', actor_type: 'ai', is_ai: true,
      repo: 'org/repo', created_at: '2026-11-14', message: 'Failure summary', url: 'javascript:alert(1)' }],
    next_before: null,
  }) };
  if (url.startsWith('/api/audit')) return { ok: true, json: async () => ({ entries: [] }) };
  if (url === '/api/status') return { ok: true, json: async () => ({ last_synced: 0, syncing: false }) };
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
  window.location.hash = '#/home';
  await tick(); await tick();
  assert.ok(window.document.querySelector('.overview-grid').textContent.includes('Open issues'));
  assert.equal(window.document.querySelectorAll('a[href^="javascript:"]').length, 0);
  window.location.hash = '#/work';
  await tick(); await tick();
  assert.ok(window.document.querySelector('.work-board').textContent.includes('To do'));
  assert.equal(window.document.querySelectorAll('img').length, 0, 'issue title is rendered as text');
  assert.equal(window.document.querySelectorAll('a[href^="javascript:"]').length, 0, 'issue URL is rejected');
  const assigneeSelect = window.document.querySelector('select[aria-label="Assignee"]');
  assigneeSelect.value = 'bob'; assigneeSelect.dispatchEvent(new window.Event('change', { bubbles: true }));
  await tick(); await tick();
  assert.ok(requests.some((url) => url.includes('assignee=bob')), 'assignee filter reloads issues');
  window.location.hash = '#/activity';
  await tick(); await tick();
  assert.ok(window.document.querySelector('.activity-feed').textContent.includes('Failure summary'));
  window.location.hash = '#/audit';
  await tick(); await tick();
  assert.ok(window.document.querySelector('table'));
  window.location.hash = '#/project';
  await tick(); await tick();
  assert.ok(window.document.querySelector('h1').textContent.includes('Project'));
  window.location.hash = '#/team';
  await tick(); await tick();
  assert.ok(window.document.querySelector('.member'));
  role = 'viewer';
  window.location.hash = '#/home';
  await tick();
  assert.equal([...window.document.querySelectorAll('.nav-link')].some((link) => link.textContent === 'Audit'), false,
    'audit navigation is hidden from viewer role');
}

run().then(() => dom.window.close()).catch((error) => { console.error(error); process.exitCode = 1; });
