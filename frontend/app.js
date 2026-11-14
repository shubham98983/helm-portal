const app = document.getElementById('app');
const pages = [
  ['home', 'Home'], ['work', 'Work'], ['pull-requests', 'Pull requests'], ['activity', 'Activity'],
  ['team', 'Team'], ['project', 'Project'], ['audit', 'Audit'],
];
const boardColumns = [
  ['draft', 'Draft'], ['ci', 'Checks'], ['awaiting', 'Awaiting review'], ['ready', 'Ready'], ['done', 'Done'],
];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  if (!response.ok) {
    let data = {};
    try { data = await response.json(); } catch {}
    throw new Error(data.detail || data.error || `Request failed (${response.status})`);
  }
  return response.json();
}

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

function safeGitHubLink(url, label) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || parsed.hostname !== 'github.com') return el('span', label);
    const link = el('a', label);
    link.href = parsed.href;
    link.rel = 'noopener noreferrer';
    return link;
  } catch {
    return el('span', label);
  }
}

function login(message = '') {
  app.replaceChildren();
  const wrap = el('main', undefined, 'login-wrap'), box = el('section', undefined, 'login');
  box.append(el('div', 'HELM / PROJECT COMMAND', 'brand'), el('h1', 'Welcome back'),
    el('p', 'Sign in to view the project workspace.', 'muted'));
  const form = document.createElement('form');
  form.id = 'login-form';
  for (const [name, label, type] of [['username', 'GitHub username', 'text'], ['password', 'Password', 'password']]) {
    const caption = el('label', label);
    caption.htmlFor = name;
    const input = document.createElement('input');
    input.id = name; input.name = name; input.type = type; input.required = true;
    form.append(caption, input);
  }
  const button = el('button', 'Sign in', 'primary');
  button.type = 'submit';
  form.append(button);
  if (message) form.append(el('p', message, 'error'));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await api('/api/login', { method: 'POST', body: JSON.stringify({ username: form.username.value, password: form.password.value }) });
      location.hash = '#/home';
      render();
    } catch (error) { login(error.message); }
  });
  box.append(form); wrap.append(box); app.append(wrap);
}

function card(title, body) {
  const section = el('section', undefined, 'card');
  section.append(el('h3', title));
  if (body) section.append(body);
  return section;
}

function list(items) {
  const ul = document.createElement('ul');
  (items || []).forEach((item) => ul.append(el('li', item)));
  return ul;
}

function memberNode(member) {
  const li = document.createElement('li'), box = el('article', undefined, 'card member');
  const head = el('div', undefined, 'member-head');
  head.append(el('div', member.name), el('span', member.role, 'tag'));
  box.append(head, el('p', member.title, 'muted'));
  if (member.responsibilities?.length) box.append(el('strong', 'Responsibilities'), list(member.responsibilities));
  const work = member.workload || {};
  box.append(el('p', `${work.reviews_done || 0} reviews done · ${work.reviews_pending || 0} pending`,'workload'));
  box.append(el('p', `${work.ai_prs_opened || 0} AI PRs opened · ${work.open_prs || 0} open PRs`, 'workload'));
  for (const agent of member.ai_agents || []) {
    const section = el('div', undefined, 'agent');
    section.append(el('strong', agent.name), el('p', `Supervised by ${member.name}`), el('span', 'May'));
    section.append(list(agent.may), el('span', 'May not'), list(agent.may_not));
    box.append(section);
  }
  li.append(box);
  if (member.children?.length) {
    const children = el('ul', undefined, 'tree');
    member.children.forEach((child) => children.append(memberNode(child)));
    li.append(children);
  }
  return li;
}

async function renderTeam(content) {
  const data = await api('/api/team'), tree = el('ul', undefined, 'tree root');
  (data.tree || []).forEach((member) => tree.append(memberNode(member)));
  if (!data.tree?.length) content.append(el('p', 'No team members are configured yet.', 'empty-state'));
  content.append(tree);
  const section = el('section', undefined, 'section');
  section.append(el('h2', 'AI contributors'));
  const grid = el('div', undefined, 'grid');
  (data.ai_agents || []).forEach((agent) => {
    const item = card(agent.name, el('p', agent.purpose));
    item.append(el('p', `Supervised by ${agent.supervisor_name}`, 'muted'), el('strong', 'May'), list(agent.may),
      el('strong', 'May not'), list(agent.may_not));
    grid.append(item);
  });
  if (!data.ai_agents?.length) grid.append(el('p', 'No AI contributors are configured.', 'empty-state'));
  section.append(grid); content.append(section);
}

async function renderProject(content) {
  const data = await api('/api/project'), project = data.profile;
  content.append(el('p', project.purpose, 'muted'));
  const progress = card(`${data.milestone_progress}% milestone progress`);
  const bar = el('div', undefined, 'progress'), fill = document.createElement('span');
  fill.style.width = `${data.milestone_progress}%`; bar.append(fill); progress.append(bar); content.append(progress);
  const grid = el('div', undefined, 'grid');
  grid.append(card('Profile', el('p', `${project.name} — ${project.tagline} (${project.phase})`)),
    card('Technology', list(project.tech_stack)));
  content.append(grid);
  const repos = el('div', undefined, 'grid');
  (data.repos || []).forEach((repo) => {
    const item = card(repo.full_name, el('p', repo.role, 'muted'));
    if (repo.metadata) item.append(el('p', `${repo.metadata.language || 'Repository'} · ${repo.metadata.default_branch || ''}`));
    if (repo.metadata_error) item.append(el('p', repo.metadata_error, 'muted'));
    repos.append(item);
  });
  if (!data.repos?.length) repos.append(el('p', 'No repositories are configured.', 'empty-state'));
  content.append(section('Repositories', repos));
  const env = el('div', undefined, 'grid');
  (project.environment || []).forEach((item) => {
    const box = card(item.name, el('p', item.notes)); box.append(el('p', item.url)); env.append(box);
  });
  content.append(section('Environments', env));
  const timeline = el('div', undefined, 'timeline');
  (data.milestones || []).forEach((item) => {
    const row = el('div', undefined, 'milestone');
    row.append(el('span', undefined, 'dot'), el('div', `${item.name}${item.due ? ` · ${item.due}` : ''}`), el('span', item.status, 'tag'));
    row.children[1].append(list(item.deliverables)); timeline.append(row);
  });
  if (!data.milestones?.length) timeline.append(el('p', 'No milestones are configured.', 'empty-state'));
  content.append(section('Milestones', timeline));
  for (const [title, items] of [
    ['Policies', Object.entries(project.policies || {}).map(([key, value]) => `${key.replaceAll('_', ' ')}: ${value}`)],
    ['Runbooks', (project.runbook || []).map((item) => `${item.title}: ${item.steps.join(' → ')}`)],
    ['Links', (project.links || []).map((item) => `${item.label}: ${item.url}`)],
  ]) content.append(section(title, list(items)));
}

function section(title, body) {
  const node = el('section', undefined, 'section');
  node.append(el('h2', title), body);
  return node;
}

function repoSelector(repos, current, onChange) {
  const select = document.createElement('select');
  select.setAttribute('aria-label', 'Repository');
  const all = document.createElement('option'); all.value = ''; all.textContent = 'All repositories'; select.append(all);
  repos.forEach((repo) => {
    const option = document.createElement('option'); option.value = repo.full_name; option.textContent = repo.full_name;
    select.append(option);
  });
  select.value = current;
  select.addEventListener('change', () => onChange(select.value));
  return select;
}

function approvalsText(approvals) {
  if (approvals.required === null) return 'Approvers not configured';
  const total = approvals.required.length;
  const count = approvals.approved_by.length;
  if (!total) return 'No approvals required';
  return `${count} of ${total} approved${approvals.missing.length ? `, waiting on ${approvals.missing.join(', ')}` : ''}`;
}

function prCard(pull) {
  const box = el('article', undefined, 'card pr-card');
  const title = el('h3'); title.append(safeGitHubLink(pull.url, `#${pull.number} ${pull.title}`));
  box.append(title, el('p', `${pull.repo} · ${pull.author}`, 'muted'), el('p', pull.branch, 'branch'));
  if (pull.is_ai) box.append(el('span', `AI · ${pull.ai_reasons.join(', ')}`, 'tag ai-badge'));
  const overall = pull.checks.overall;
  box.append(el('span', `${overall} · ${Object.entries(pull.checks.counts).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(', ') || 'no checks'}`, `check-chip ${overall}`));
  box.append(el('p', approvalsText(pull.approvals), 'approval-progress'));
  if (pull.approvals.stale_by.length) box.append(el('p', `Stale approval from ${pull.approvals.stale_by.join(', ')}`, 'warning'));
  if (pull.approvals.changes_requested_by.length) {
    box.append(el('p', `Changes requested by ${pull.approvals.changes_requested_by.join(', ')}`, 'warning'));
  }
  return box;
}

async function renderBoard(content, me) {
  const project = await api('/api/project'), controls = el('div', undefined, 'filters');
  const selected = new URLSearchParams(location.hash.split('?')[1] || '').get('repo') || '';
  const aiOnly = new URLSearchParams(location.hash.split('?')[1] || '').get('ai') === '1';
  const refresh = async (repo = selected, ai = aiOnly) => {
    const query = new URLSearchParams(); if (repo) query.set('repo', repo); if (ai) query.set('ai', '1');
    const data = await api(`/api/prs/board?${query}`);
    const board = el('div', undefined, 'board');
    boardColumns.forEach(([key, label]) => {
      const column = el('section', undefined, 'board-column');
      column.append(el('h2', label), el('span', String(data.columns[key].length), 'column-count'));
      data.columns[key].forEach((pull) => column.append(prCard(pull)));
      if (!data.columns[key].length) column.append(el('p', 'No pull requests', 'muted'));
      board.append(column);
    });
    const old = content.querySelector('.board'); if (old) old.replaceWith(board); else content.append(board);
  };
  controls.append(repoSelector(project.repos || [], selected, (repo) => refresh(repo, aiOnly)));
  const label = el('label', 'AI only', 'toggle');
  const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = aiOnly;
  checkbox.addEventListener('change', () => refresh(selected, checkbox.checked)); label.prepend(checkbox); controls.append(label);
  if (me.role === 'lead') {
    const sync = el('button', 'Sync now', 'primary');
    sync.addEventListener('click', async () => {
      const repo = selected || project.repos?.[0]?.full_name;
      if (!repo) return;
      sync.disabled = true;
      try { await api('/api/sync', { method: 'POST', body: JSON.stringify({ repo }) }); await refresh(); }
      catch (error) { content.prepend(el('p', error.message, 'error-banner')); }
      finally { sync.disabled = false; }
    });
    controls.append(sync);
  }
  content.append(controls);
  await refresh();
}

const eventIcon = { pull_request: '↗', review: '◉', check: '✓', workflow: '⚙', commit: '⌘' };

function ageLabel(value) {
  if (!value) return 'Age unavailable';
  const days = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 86400000));
  return days ? `${days}d old` : 'Today';
}

async function renderWork(content) {
  const project = await api('/api/project');
  const controls = el('div', undefined, 'filters');
  const boardRoot = el('div', undefined, 'board work-board');
  const assignees = document.createElement('select'); assignees.setAttribute('aria-label', 'Assignee');
  const labelSelect = document.createElement('select'); labelSelect.setAttribute('aria-label', 'Label');
  let repo = '', assignee = '', labelValue = '';
  const refresh = async () => {
    const query = new URLSearchParams();
    if (repo) query.set('repo', repo);
    if (assignee) query.set('assignee', assignee);
    if (labelValue) query.set('label', labelValue);
    const data = await api(`/api/work/issues?${query}`);
    boardRoot.replaceChildren();
    const assigneeOptions = [...new Set(data.issues.flatMap((issue) => issue.assignees))].sort();
    const labelOptions = [...new Set(data.issues.flatMap((issue) => issue.labels))].sort();
    assignees.replaceChildren();
    [['', 'All assignees'], ...assigneeOptions.map((name) => [name, name])].forEach(([value, text]) => {
      const option = document.createElement('option'); option.value = value; option.textContent = text; assignees.append(option);
    });
    assignees.value = assignee;
    labelSelect.replaceChildren();
    [['', 'All labels'], ...labelOptions.map((name) => [name, name])].forEach(([value, text]) => {
      const option = document.createElement('option'); option.value = value; option.textContent = text; labelSelect.append(option);
    });
    labelSelect.value = labelValue;
    Object.entries(data.errors || {}).forEach(([name, message]) => content.prepend(el('p', `${name}: ${message}`, 'error-banner')));
    const columns = [['backlog', 'Backlog'], ['todo', 'To do'], ['in-progress', 'In progress'],
      ['review', 'Review'], ['done', 'Done']];
    columns.forEach(([key, title]) => {
      const sectionNode = el('section', undefined, 'board-column');
      const items = data.issues.filter((issue) => issue.column === key);
      sectionNode.append(el('h2', title), el('span', String(items.length), 'column-count'));
      if (!items.length) sectionNode.append(el('p', 'Nothing here yet', 'muted'));
      items.forEach((issue) => {
        const item = el('article', undefined, 'card issue-card');
        const heading = el('h3'); heading.append(safeGitHubLink(issue.url, `#${issue.number} ${issue.title}`));
        item.append(heading, el('p', issue.repo, 'muted'));
        const people = el('div', undefined, 'assignees');
        issue.assignees.forEach((name) => people.append(el('span', initials(name), 'avatar-initials')));
        if (!issue.assignees.length) people.append(el('span', 'Unassigned', 'muted'));
        item.append(people);
        const tags = el('div', undefined, 'issue-labels');
        issue.labels.forEach((value) => tags.append(el('span', value, 'tag')));
        item.append(tags, el('p', ageLabel(issue.created_at), 'muted'));
        if (issue.linked_pr) {
          const link = safeGitHubLink(`https://github.com/${issue.repo}/pull/${issue.linked_pr}`, `Linked PR #${issue.linked_pr}`);
          item.append(link);
        }
        sectionNode.append(item);
      });
      boardRoot.append(sectionNode);
    });
  };
  assignees.addEventListener('change', () => { assignee = assignees.value; refresh(); });
  labelSelect.addEventListener('change', () => { labelValue = labelSelect.value; refresh(); });
  controls.append(repoSelector(project.repos || [], repo, (value) => { repo = value; refresh(); }), assignees,
    labelSelect);
  content.append(controls, boardRoot);
  await refresh();
}

function initials(name) {
  return String(name || '?').split(/[-_\s]+/).filter(Boolean).slice(0, 2).map((part) => part[0].toUpperCase()).join('');
}

async function renderHome(content) {
  const data = await api('/api/overview');
  const grid = el('div', undefined, 'overview-grid');
  const ci = data.main_ci;
  const tiles = [
    ['Main branch CI', ci ? `${ci.conclusion || ci.status} · ${ci.age || ''}` : 'No data', ci?.html_url],
    ['Open pull requests', `${data.open_prs.ai} AI · ${data.open_prs.human} human`, '#/pull-requests'],
    ['Awaiting approval', String(data.awaiting_approval), '#/pull-requests'],
    ['AI changes this week', `${data.ai_changes_this_week.opened} opened · ${data.ai_changes_this_week.merged} merged`, '#/audit'],
    ['Open issues', String(data.open_issues), '#/work'],
  ];
  if (data.copilot) {
    const quality = data.copilot.status === 'unavailable' ? data.copilot.label
      : `Score ${data.copilot.score} · coverage ${data.copilot.coverage} · ${Object.entries(data.copilot.findings || {}).map(([severity, count]) => `${severity}: ${count}`).join(', ') || 'no findings'} · trend ${(data.copilot.score_trend || []).join(' → ') || 'No data'}`;
    tiles.push(['Copilot quality', quality, '#/project']);
  }
  tiles.forEach(([title, value, url]) => {
    const item = card(title, el('p', value, 'tile-value'));
    if (url?.startsWith('https://github.com/')) item.append(safeGitHubLink(url, 'Open source'));
    else if (url?.startsWith('#/')) { const link = el('a', 'Open details'); link.href = url; item.append(link); }
    grid.append(item);
  });
  content.append(grid);
  const listRoot = el('div', undefined, 'attention-list');
  if (!data.needs_attention.length) listRoot.append(el('p', 'No items need attention right now.', 'empty-state'));
  data.needs_attention.forEach((item) => {
    const row = el('article', undefined, 'attention-item');
    row.append(el('strong', item.title), el('p', item.detail, 'muted'));
    if (item.url?.startsWith('#/')) { const a = el('a', 'Open page'); a.href = item.url; row.append(a); }
    else if (item.url) row.append(safeGitHubLink(item.url, 'Open on GitHub'));
    listRoot.append(row);
  });
  content.append(section('Needs attention', listRoot));
  Object.entries(data.errors || {}).forEach(([repo, message]) => content.prepend(el('p', `${repo}: ${message}`, 'error-banner')));
}

async function renderActivity(content) {
  const project = await api('/api/project'), filter = el('div', undefined, 'filters');
  let repo = '', actor = 'all', kind = '', before = '';
  const feed = el('div', undefined, 'timeline activity-feed');
  const load = el('button', 'Load more', 'secondary');
  const fetchEvents = async (reset) => {
    if (reset) { before = ''; feed.replaceChildren(); }
    const params = new URLSearchParams({ actor, limit: '30' });
    if (repo) params.set('repo', repo); if (kind) params.set('kind', kind); if (before) params.set('before', before);
    const data = await api(`/api/activity?${params}`);
    data.events.forEach((event) => {
      const row = el('article', undefined, 'event-row');
      row.append(el('span', eventIcon[event.kind] || '•', 'event-icon'));
      const detail = el('div', undefined, 'event-detail');
      const text = event.kind === 'pull_request' ? `${event.action} pull request #${event.number}: ${event.title}`
        : event.kind === 'review' ? `${event.state || 'Review'} on pull request #${event.number}`
          : event.kind === 'check' ? `${event.name || 'Check'}: ${event.conclusion || event.status}`
            : event.kind === 'workflow' ? `${event.name || 'Workflow'}: ${event.conclusion || event.status}`
              : event.message || 'Branch commit';
      detail.append(el('strong', text), el('p', `${event.actor || 'Unknown actor'} · ${event.repo} · ${event.created_at}`, 'muted'));
      if (event.actor_type === 'ai' || event.is_ai) detail.append(el('span', 'AI contributor', 'tag ai-badge'));
      if (event.url) detail.append(safeGitHubLink(event.url, 'View on GitHub'));
      row.append(detail); feed.append(row);
    });
    if (reset && !data.events.length) feed.append(el('p', 'No activity matches these filters yet.', 'empty-state'));
    before = data.next_before || '';
    load.hidden = !before;
  };
  filter.append(repoSelector(project.repos || [], repo, (value) => { repo = value; fetchEvents(true); }));
  const actorSelect = document.createElement('select'); actorSelect.setAttribute('aria-label', 'Actor');
  [['all', 'All actors'], ['ai', 'AI actors'], ['human', 'Human actors']].forEach(([value, text]) => {
    const option = document.createElement('option'); option.value = value; option.textContent = text; actorSelect.append(option);
  });
  actorSelect.addEventListener('change', () => { actor = actorSelect.value; fetchEvents(true); }); filter.append(actorSelect);
  const kindSelect = document.createElement('select'); kindSelect.setAttribute('aria-label', 'Event type');
  [['', 'All activity'], ['pull_request', 'Pull requests'], ['review', 'Reviews'], ['check', 'Checks'], ['workflow', 'Workflows'], ['commit', 'Commits']].forEach(([value, text]) => {
    const option = document.createElement('option'); option.value = value; option.textContent = text; kindSelect.append(option);
  });
  kindSelect.addEventListener('change', () => { kind = kindSelect.value; fetchEvents(true); }); filter.append(kindSelect);
  load.addEventListener('click', () => fetchEvents(false));
  content.append(filter, feed, load); await fetchEvents(true);
}

async function renderAudit(content) {
  const project = await api('/api/project'), controls = el('div', undefined, 'filters');
  let repo = '', verdict = '';
  const tableWrap = el('div', undefined, 'table-wrap');
  const table = document.createElement('table');
  const head = document.createElement('thead'), headerRow = document.createElement('tr');
  ['Pull request', 'Author', 'Merged', 'Verdict', 'Details'].forEach((title) => headerRow.append(el('th', title)));
  head.append(headerRow); table.append(head);
  const body = document.createElement('tbody'); table.append(body); tableWrap.append(table);
  const refresh = async () => {
    const params = new URLSearchParams(); if (repo) params.set('repo', repo); if (verdict) params.set('verdict', verdict);
    const data = await api(`/api/audit?${params}`); body.replaceChildren();
    data.entries.forEach((item) => {
      const row = document.createElement('tr'), pr = document.createElement('td');
      pr.append(safeGitHubLink(item.url, `#${item.number} ${item.title}`));
      row.append(pr, el('td', item.author), el('td', item.merged_at), el('td', item.verdict, `verdict ${item.verdict}`));
      const detailsCell = document.createElement('td'), details = document.createElement('details');
      details.append(el('summary', 'Review and audit details'), el('p', item.reasons.join('; ') || 'No violations'));
      details.append(el('p', `Required: ${(item.required || []).join(', ') || 'unknown'}`));
      item.reviews.forEach((review) => details.append(el('p', `${review.user?.login || 'Unknown'} · ${review.state} · ${review.submitted_at || 'timestamp unavailable'}`)));
      detailsCell.append(details); row.append(detailsCell); body.append(row);
    });
    if (!data.entries.length) {
      const row = document.createElement('tr'), cell = el('td', 'No audit entries yet.', 'empty-state');
      cell.colSpan = 5; row.append(cell); body.append(row);
    }
  };
  controls.append(repoSelector(project.repos || [], repo, (value) => { repo = value; refresh(); }));
  const verdictSelect = document.createElement('select'); verdictSelect.setAttribute('aria-label', 'Verdict');
  [['', 'All verdicts'], ['compliant', 'Compliant'], ['violation', 'Violation'], ['unknown', 'Unknown']].forEach(([value, label]) => {
    const option = document.createElement('option'); option.value = value; option.textContent = label; verdictSelect.append(option);
  });
  verdictSelect.addEventListener('change', () => { verdict = verdictSelect.value; refresh(); }); controls.append(verdictSelect);
  const csvLink = el('a', 'Download CSV', 'secondary'); csvLink.href = '/api/audit.csv';
  csvLink.addEventListener('click', (event) => { if (repo || verdict) { event.preventDefault(); location.href = `/api/audit.csv?${new URLSearchParams({ ...(repo ? { repo } : {}), ...(verdict ? { verdict } : {}) })}`; } });
  controls.append(csvLink); content.append(controls, tableWrap); await refresh();
}

async function render() {
  let me;
  try { me = await api('/api/me'); } catch { login(); return; }
  const key = (location.hash.match(/^#\/([^/?]+)/) || [])[1] || 'home';
  const visiblePages = pages.filter(([id]) => id !== 'audit' || ['lead', 'reviewer'].includes(me.role));
  if (key === 'audit' && !visiblePages.some(([id]) => id === key)) { location.hash = '#/home'; return; }
  const shell = el('div', undefined, 'shell'), nav = el('aside', undefined, 'sidebar');
  const skip = el('a', 'Skip to content', 'skip-link'); skip.href = '#main-content'; nav.append(skip);
  nav.append(el('div', 'HELM', 'brand'));
  nav.setAttribute('aria-label', 'Main navigation');
  visiblePages.forEach(([id, title]) => {
    const link = el('a', title, `nav-link${id === key ? ' active' : ''}`); link.href = `#/${id}`; nav.append(link);
  });
  const signOut = el('button', 'Sign out', 'signout');
  signOut.addEventListener('click', async () => {
    await api('/api/logout', { method: 'POST', body: '{}' }); location.hash = '#/home'; login();
  });
  nav.append(signOut);
  const main = el('div', undefined, 'main'), top = el('header', undefined, 'topbar');
  top.append(el('strong', key === 'team' ? 'People & ownership' : key === 'project' ? 'Project profile' : pages.find((page) => page[0] === key)?.[1] || 'Helm'));
  const identity = el('div', undefined, 'identity'); identity.append(el('strong', me.name), el('span', me.role));
  const lastSynced = el('span', 'Last synced: loading', 'last-synced'); identity.append(lastSynced); top.append(identity);
  main.append(top);
  const content = el('main', undefined, 'content'); content.id = 'main-content'; content.setAttribute('tabindex', '-1');
  content.setAttribute('aria-busy', 'true');
  const pageHead = el('div', undefined, 'page-head');
  const heading = key === 'pull-requests' ? 'Pull requests' : key === 'audit' ? 'AI audit ledger' : pages.find((page) => page[0] === key)?.[1] || 'Home';
  const titleBlock = el('div'); titleBlock.append(el('h1', heading), el('p', 'A clear view of project delivery.', 'muted'));
  pageHead.append(titleBlock); content.append(pageHead);
  try {
    if (key === 'team') await renderTeam(content);
    else if (key === 'project') await renderProject(content);
    else if (key === 'pull-requests') await renderBoard(content, me);
    else if (key === 'activity') await renderActivity(content);
    else if (key === 'audit') await renderAudit(content);
    else if (key === 'home') await renderHome(content);
    else if (key === 'work') await renderWork(content);
    else content.append(el('div', `${pages.find((page) => page[0] === key)?.[1] || 'Page'} coming next`, 'placeholder'));
  } catch (error) { content.append(el('p', error.message, 'error-banner')); }
  content.setAttribute('aria-busy', 'false');
  main.append(content); shell.append(nav, main); app.replaceChildren(shell);
  api('/api/status').then((status) => {
    const date = status.last_synced ? new Date(status.last_synced * 1000).toLocaleString() : 'Not synced yet';
    lastSynced.textContent = `Last synced: ${date}${status.syncing ? ' · Sync in progress' : ''}`;
    const notices = el('div', undefined, 'global-notices');
    (status.notices || []).forEach((notice) => notices.append(el('p', notice, 'error-banner')));
    content.prepend(notices);
  }).catch(() => { lastSynced.textContent = 'Last synced: unavailable'; });
}

window.addEventListener('hashchange', render);
render();
