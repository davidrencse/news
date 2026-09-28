const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const safeUrl = u => (/^https?:\/\//i.test(u || '') ? u : '');
const local = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};

async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

const PAGE = 60;  // library cards rendered at a time
const skeleton = (n = 4, label = '') => (label ? `<div class="loading-line">${esc(label)}</div>` : '')
  + Array.from({ length: n }, () => '<div class="card skel" aria-hidden="true"><div><i></i><i></i><i></i></div><i class="skel-img"></i></div>').join('');

const S = {
  topics: [],
  limit: PAGE,
  articles: [],
  sel: local.get('sel', { topic: null, sub: null }),
  tab: 'library',
  access: local.get('access', 'all'),  // 'all' | 'free' | 'locked' (member-only)
  mode: 'browse',        // 'browse' | 'search'
  search: null,          // {q, dest, items, next, loading, error}
  open: new Set(local.get('open', ['cybersecurity', 'artificial-intelligence'])),
  discover: {},          // "topic/sub" -> {items, errors, tags}
  reader: null,          // article currently shown
  pendingRemovals: new Map(),  // id -> timer; removals wait a few seconds so they can be undone
  pollTimer: null,
  sort: local.get('discoverSort', 'foryou'),  // Discover: 'foryou' (recommended) | 'trending'
  labels: local.get('labels', []),            // Discover "For you": only posts with one of these labels
  rec: null,             // {items, labels, top_labels, engaged} from /api/recommend
};

// "New" means added by the curator since the previous visit (nothing is new on a first visit).
const LAST_VISIT = local.get('lastVisit', null);
local.set('lastVisit', new Date().toISOString().slice(0, 19) + '+00:00');

const topic = id => S.topics.find(t => t.id === id);
const sub = (t, s) => topic(t)?.subtopics.find(x => x.id === s);
const key = (t, s) => `${t}/${s}`;
const fmtDate = d => {
  if (!d) return '';
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d);  // bare dates are local days, not UTC midnight
  const x = day ? new Date(+day[1], day[2] - 1, +day[3]) : new Date(d);
  return isNaN(x) ? '' : x.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
};
const fmtCount = n => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : (n || 0).toLocaleString());
const sourceOf = u => { try { const p = new URL(u); return p.hostname === 'medium.com' ? `medium.com/${p.pathname.split('/')[1]}` : p.hostname; } catch { return ''; } };

function toast(msg, ms = 2600, action = null) {
  const el = $('#toast');
  el.innerHTML = `<span>${esc(msg)}</span>${action ? `<button class="toast-act">${esc(action.label)}</button>` : ''}`;
  const hide = () => { el.classList.add('hiding'); clearTimeout(toast.t); toast.t = setTimeout(() => (el.hidden = true), 180); };
  if (action) el.querySelector('button').onclick = () => { hide(); action.run(); };
  el.classList.remove('hiding');
  el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(hide, ms);
}

async function load() {
  const data = await api('/api/library');
  S.topics = data.topics; S.articles = data.articles;
  if (S.sel.topic && !topic(S.sel.topic)) S.sel = { topic: null, sub: null };
  if (S.sel.sub && !sub(S.sel.topic, S.sel.sub)) S.sel.sub = null;
  render();
}

function select(t, s) {
  if (!t || s) drawer(false);
  if (S.sel.topic !== t || S.sel.sub !== s) $('.view').scrollTop = 0;
  S.sel = { topic: t, sub: s };
  if (!s) S.tab = 'library';
  S.mode = 'browse'; S.search = null; S.limit = PAGE; $('#search').value = '';
  local.set('sel', S.sel);
  closeReader();
  render();
  if (s && S.tab === 'discover') loadDiscover();
}

function render() { renderTree(); renderDest(); renderHead(); renderList(); }

/* ------------------------------------------------------------ sidebar */
function renderTree() {
  const counts = {};
  for (const a of S.articles) {
    counts[a.topic] = (counts[a.topic] || 0) + 1;
    counts[key(a.topic, a.subtopic)] = (counts[key(a.topic, a.subtopic)] || 0) + 1;
  }
  const isSel = (t, s) => S.mode === 'browse' && S.sel.topic === t && S.sel.sub === s;
  let h = `<button class="tree-item tree-all ${isSel(null, null) ? 'active' : ''}" data-t="" data-s="">
    <span class="label">All articles</span><span class="count">${S.articles.length}</span></button>
    <button class="tree-item tree-all tree-discover ${S.mode === 'discover' ? 'active' : ''}" data-discover-all>
    <span class="label">✦ Discover</span><span class="count">${S.suggest?.items ? S.suggest.items.length : ''}</span></button>
    <button class="tree-item tree-all tree-notebook ${S.mode === 'notebook' ? 'active' : ''}" data-notebook>
    <span class="label">✎ Notebook</span><span class="count">${S.articles.filter(a => a.notes_count).length || ''}</span></button>`;
  for (const t of S.topics) {
    const open = S.open.has(t.id);
    h += `<div class="${open ? 'open' : ''}" data-group="${esc(t.id)}">
      <button class="tree-item tree-topic ${isSel(t.id, null) ? 'active' : ''}" data-t="${esc(t.id)}" data-s="">
        <span class="chev">▶</span><span class="label">${esc(t.name)}</span><span class="count">${counts[t.id] || ''}</span>
      </button><div class="subs">`;
    for (const s of t.subtopics) {
      h += `<button class="tree-item tree-sub ${isSel(t.id, s.id) ? 'active' : ''}" data-t="${esc(t.id)}" data-s="${esc(s.id)}">
        <span class="label"><span class="hash">#</span> ${esc(s.name)}</span><span class="count">${counts[key(t.id, s.id)] || ''}</span></button>`;
    }
    h += `<button class="tree-item tree-sub tree-add" data-add="${esc(t.id)}">+ ${t.id === 'custom' ? 'New topic' : 'Add subtopic'}</button></div></div>`;
  }
  $('#tree').innerHTML = h;
}

$('#tree').addEventListener('click', e => {
  const add = e.target.closest('[data-add]');
  if (add) return newTopic(add.dataset.add);
  if (e.target.closest('[data-discover-all]')) return openDiscover();
  if (e.target.closest('[data-notebook]')) return openNotebook();
  const b = e.target.closest('.tree-item');
  if (!b) return;
  const t = b.dataset.t || null, s = b.dataset.s || null;
  if (t && !s) {
    // clicking a topic toggles it open and shows everything inside it
    if (S.sel.topic === t && !S.sel.sub && S.open.has(t)) S.open.delete(t); else S.open.add(t);
    local.set('open', [...S.open]);
  }
  select(t, s);
});
$('#newTopicBtn').onclick = () => newTopic('custom');

/* ------------------------------------------------------------ phone layout
   On a narrow screen the sidebar is a drawer and the paste form folds behind a button. Both are
   plain CSS classes, so on a wide screen these handlers are harmless no-ops. */
const isPhone = () => matchMedia('(max-width: 860px)').matches;

function drawer(open) {
  $('#sidebar').classList.toggle('open', open);
  $('#scrim').hidden = !open;
  $('#menuBtn').setAttribute('aria-expanded', String(open));
  document.body.style.overflow = open ? 'hidden' : '';
}
$('#menuBtn').onclick = () => drawer(!$('#sidebar').classList.contains('open'));
$('#scrim').onclick = () => drawer(false);
// Picking a subtopic, or "All articles", navigates and closes the drawer. Tapping a topic only
// expands it, so the drawer stays open for the subtopics it just revealed.
$('#tree').addEventListener('click', e => {
  const b = e.target.closest('.tree-item');
  if (!b || e.target.closest('[data-add]')) return;
  if (isPhone() && (b.dataset.s || b.classList.contains('tree-all'))) drawer(false);
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') drawer(false); });
addEventListener('resize', () => { if (!isPhone()) drawer(false); });

// Installed to a home screen, the app opens and reads saved articles without the server running.
// Browsers only allow this over https or on localhost, so a plain LAN address just skips it.
if ('serviceWorker' in navigator) {
  addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}

$('#pasteBtn').onclick = () => {
  const open = $('#pasteForm').classList.toggle('open');
  $('#pasteBtn').setAttribute('aria-expanded', String(open));
  if (open) $('#pasteUrl').focus();
};

document.addEventListener('keydown', e => {
  if (e.key !== '/' || e.ctrlKey || e.metaKey || S.reader || $('#dlg').open) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
  e.preventDefault();
  $('#search').focus();
  $('#search').select();
});
$('#themeBtn').onclick = () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
  local.set('theme', root.dataset.theme);
};

function currentDest() {
  return S.sel.sub ? key(S.sel.topic, S.sel.sub) : local.get('dest', 'cybersecurity/red-teaming');
}

function destOptions(cur) {
  return S.topics.filter(t => t.subtopics.length).map(t =>
    `<optgroup label="${esc(t.name)}">${t.subtopics.map(s =>
      `<option value="${esc(key(t.id, s.id))}" ${key(t.id, s.id) === cur ? 'selected' : ''}>${esc(s.name)}</option>`).join('')}</optgroup>`).join('');
}

function renderDest() { $('#pasteDest').innerHTML = destOptions(currentDest()); }

/* ------------------------------------------------------------ header */
function renderHead() {
  if (S.mode === 'search') return renderSearchHead();
  if (S.mode === 'discover') return renderDiscoverHead();
  if (S.mode === 'notebook') return renderNotebookHead();
  const t = topic(S.sel.topic), s = S.sel.sub && sub(S.sel.topic, S.sel.sub);
  const title = s ? s.name : t ? t.name : 'All articles';
  const crumb = ['Medium-Library', t?.id, s?.id].filter(Boolean).map(x => `<span>${esc(x)}</span>`).join('');
  const scope = scopeArticles();
  const access = [['all', 'All', scope.length], ['free', 'Free', scope.filter(a => a.locked === false).length],
    ['locked', '🔒 Member-only', scope.filter(a => a.locked === true).length]];
  let tags = '';
  if (s) {
    tags = `<div class="tags"><span class="crumb">Discover pulls from Medium tags:</span>
      ${s.tags.map(x => `<span class="tag">${esc(x)}</span>`).join('') || '<span class="tag">none</span>'}
      <button class="linkish" id="editTags">edit</button>
      ${s.custom ? '<button class="linkish" id="delSub" style="color:var(--danger)">delete topic</button>' : ''}</div>`;
  }
  $('#viewHead').innerHTML = `
    <div class="crumb">${crumb}</div>
    <div class="view-title"><h1>${esc(title)}</h1></div>
    ${tags}
    <div class="tabs">
      <button class="tab ${S.tab === 'library' ? 'active' : ''}" data-tab="library">Library · ${scope.length}</button>
      <button class="tab ${S.tab === 'discover' ? 'active' : ''}" data-tab="discover" ${s ? '' : 'disabled title="Pick a subtopic to discover new articles"'}>Discover</button>
      ${S.tab === 'discover' && s ? '<span class="tab-note"><button class="linkish" id="refreshDiscover">refresh</button></span>' : ''}
      ${S.tab === 'library' ? `<span class="access">${access.map(([k, label, n]) =>
        `<button class="chip ${S.access === k ? 'active' : ''}" data-access="${k}">${label} <span>${n}</span></button>`).join('')}</span>` : ''}
    </div>`;
  $('#viewHead').querySelectorAll('.tab').forEach(b => b.onclick = () => {
    S.tab = b.dataset.tab; renderHead(); renderList();
    if (S.tab === 'discover') loadDiscover();
  });
  $('#editTags') && ($('#editTags').onclick = () => editTags(S.sel.topic, s));
  $('#delSub') && ($('#delSub').onclick = () => deleteSub(S.sel.topic, s));
  $('#refreshDiscover') && ($('#refreshDiscover').onclick = () => loadDiscover(true));
  $('#viewHead').querySelectorAll('[data-access]').forEach(b => b.onclick = () => {
    S.access = b.dataset.access;
    S.limit = PAGE;
    local.set('access', S.access);
    renderHead(); renderList();
  });
}

/* ------------------------------------------------------------ lists */
function scopeArticles() {
  return S.articles.filter(a => (!S.sel.topic || a.topic === S.sel.topic) && (!S.sel.sub || a.subtopic === S.sel.sub));
}

function visibleArticles() {
  return scopeArticles().filter(a => S.access === 'all' || (S.access === 'locked' ? a.locked === true : a.locked === false));
}

// Your own library is searched here in the browser, with the same idea the server uses on the
// index: a title match counts for more than a mention in the snippet, words that sit together
// count for more than words scattered apart, and everything that matches is ranked, not just
// filtered. Quotes and -exclusions work the same way as they do on Medium-wide search.
function queryParts(q) {
  const phrases = [], plain = [], minus = [];
  for (const tok of q.toLowerCase().match(/-?"[^"]*"|\S+/g) || []) {
    const neg = tok.startsWith('-'), body = (neg ? tok.slice(1) : tok).replace(/^"|"$/g, '');
    const ws = body.match(/[a-z0-9]+/g) || [];
    if (!ws.length) continue;
    if (neg) minus.push(...ws);
    else if (ws.length > 1 && /^-?"/.test(tok)) phrases.push(ws.join(' '));
    else plain.push(...ws);
  }
  return { phrases, plain, minus, words: [...plain, ...phrases.flatMap(p => p.split(' '))] };
}

function libraryMatches(q) {
  const { phrases, plain, minus, words } = queryParts(q);
  if (!words.length) return [];
  const scored = [];
  for (const a of S.articles) {
    const title = (a.title || '').toLowerCase();
    const rest = `${a.author || ''} ${a.snippet || ''} ${a.topic} ${a.subtopic}`.toLowerCase();
    if (minus.some(w => title.includes(w) || rest.includes(w))) continue;
    if (phrases.some(p => !title.includes(p) && !rest.includes(p))) continue;
    const inTitle = words.filter(w => title.includes(w)).length;
    const anywhere = words.filter(w => title.includes(w) || rest.includes(w)).length;
    if (!anywhere) continue;
    const order = plain.map(w => title.indexOf(w)).filter(i => i >= 0);
    const span = order.length > 1 ? Math.max(...order) - Math.min(...order) + 1 : 1;
    scored.push([2.5 * (anywhere / words.length) + 1.2 * (inTitle / words.length)
      + 0.6 * (order.length > 1 ? order.length / span : 0)
      + 0.5 * phrases.filter(p => title.includes(p)).length
      + 0.3 * (a.fetched ? 1 : 0), a]);
  }
  return scored.sort((x, y) => y[0] - x[0] || (y[1].added || '').localeCompare(x[1].added || '')).map(s => s[1]);
}

function libraryCard(a, showLoc) {
  const t = topic(a.topic), s = sub(a.topic, a.subtopic);
  const img = safeUrl(a.image);
  return `<article class="card ${img ? '' : 'noimg'}" data-id="${esc(a.id)}" tabindex="0">
    <div>
      <div class="card-meta">
        ${a.author ? `<span>${esc(a.author)}</span>` : ''}
        ${a.published ? `<span class="${a.author ? 'dot' : ''}"> ${fmtDate(a.published)}</span>` : ''}
        ${a.claps ? `<span class="${a.author || a.published ? 'dot' : ''}" title="${a.claps.toLocaleString()} claps"> 👏 ${fmtCount(a.claps)}</span>` : ''}
      </div>
      <h2 class="card-title">${esc(a.title)}</h2>
      ${a.snippet ? `<p class="card-snip">${esc(a.snippet)}</p>` : ''}
      <div class="card-foot">
        ${a.source === 'auto' && LAST_VISIT && a.added > LAST_VISIT ? '<span class="pill new">New</span>' : ''}
        ${a.locked === true ? '<span class="pill locked" title="Member-only story: the PDF is made through Freedium">🔒 Member-only</span>'
          : a.locked === false ? '<span class="pill free" title="Free story: the PDF is made straight from Medium">Free</span>' : ''}
        ${a.pdf_url ? '<span class="pill ready" title="Saved on this computer; opens instantly">Saved offline</span>' : ''}
        ${showLoc && s ? `<span class="pill loc">${esc(t.name)} / ${esc(s.name)}</span>` : ''}
        ${a.notes_count ? `<span class="pill loc" title="Highlights and notes">✎ ${a.notes_count}</span>` : ''}
        <span class="card-actions">
          <button class="btn small ghost" data-act="move">Move</button>
          <button class="btn small ghost danger" data-act="remove">Remove</button>
        </span>
      </div>
    </div>
    ${img ? `<img class="card-img" src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}
  </article>`;
}

function renderList() {
  if (S.mode === 'search') return renderSearch();
  if (S.mode === 'discover') return renderDiscoverAll();
  if (S.mode === 'notebook') return renderNotebook();
  if (S.tab === 'discover' && S.sel.sub) return renderDiscover();
  const items = visibleArticles();
  if (!items.length && S.access !== 'all' && scopeArticles().length) {
    $('#list').innerHTML = `<div class="empty"><b>No ${S.access === 'free' ? 'free' : 'member-only'} articles here yet</b>Paywall status fills in as each article gets checked.</div>`;
    return;
  }
  if (!items.length) {
    $('#list').innerHTML = `<div class="empty"><b>Nothing saved here yet</b>${S.sel.sub
      ? 'Open the <b style="display:inline;font:inherit">Discover</b> tab, search Medium above, or paste a link.'
      : 'Search all of Medium above, pick a subtopic and use Discover, or paste a Medium link.'}</div>`;
    return;
  }
  const shown = items.slice(0, S.limit), left = items.length - shown.length;
  $('#list').innerHTML = shown.map(a => libraryCard(a, !S.sel.sub)).join('')
    + (left > 0 ? `<div class="more"><button class="btn" id="showMore">Show ${Math.min(PAGE, left)} more · ${left.toLocaleString()} left</button></div>` : '');
  $('#showMore') && ($('#showMore').onclick = () => { S.limit += PAGE; renderList(); });
}

$('#list').addEventListener('click', async e => {
  const card = e.target.closest('.card');
  if (!card) return;
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (card.dataset.result !== undefined) return onResultClick(card, act);
  if (card.dataset.discover !== undefined) return onDiscoverClick(card, act);
  if (card.dataset.suggest !== undefined) return onSuggestClick(card, act);
  const a = S.articles.find(x => x.id === card.dataset.id);
  if (!a) return;
  if (act === 'move') return moveArticle(a);
  if (act === 'remove') return removeArticle(a);
  openReader(a);
});
$('#list').addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.target.classList.contains('card')) e.target.click();
});

async function loadDiscover(force = false) {
  const k = key(S.sel.topic, S.sel.sub);
  if (S.discover[k] && !force) return renderList();
  $('#list').innerHTML = skeleton(4, 'Fetching the latest stories from Medium…');
  try {
    S.discover[k] = await api(`/api/discover/${encodeURIComponent(S.sel.topic)}/${encodeURIComponent(S.sel.sub)}`);
  } catch (err) {
    S.discover[k] = { items: [], errors: [err.message], tags: [] };
  }
  if (key(S.sel.topic, S.sel.sub) === k && S.tab === 'discover' && S.mode === 'browse') renderList();
}

function renderDiscover() {
  const d = S.discover[key(S.sel.topic, S.sel.sub)];
  if (!d) { $('#list').innerHTML = skeleton(4, 'Fetching the latest stories from Medium…'); return; }
  const saved = new Set(S.articles.map(a => a.url));
  const warn = d.errors?.length ? `<div class="warn">Some tags failed: ${d.errors.map(esc).join(' · ')}</div>` : '';
  if (!d.items.length) {
    $('#list').innerHTML = warn + '<div class="empty"><b>No stories found</b>Try different tags with “edit” above.</div>';
    return;
  }
  $('#list').innerHTML = warn + d.items.map((it, i) => {
    const img = safeUrl(it.image), isSaved = saved.has(it.url);
    return `<article class="card ${img ? '' : 'noimg'}" data-discover="${i}" tabindex="0">
      <div>
        <div class="card-meta">
          <span>${esc(it.author)}</span><span class="dot"> ${fmtDate(it.published)}</span><span class="dot"> #${esc(it.tag)}</span>
        </div>
        <h2 class="card-title">${esc(it.title)}</h2>
        ${it.snippet ? `<p class="card-snip">${esc(it.snippet)}</p>` : ''}
        <div class="card-foot">
          <button class="btn small primary" data-act="read">Read</button>
          ${isSaved ? '<span class="pill ready">In library</span>' : '<button class="btn small" data-act="save">Save for later</button>'}
        </div>
      </div>
      ${img ? `<img class="card-img" src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}
    </article>`;
  }).join('');
}

async function onDiscoverClick(card, act) {
  const it = S.discover[key(S.sel.topic, S.sel.sub)]?.items[+card.dataset.discover];
  if (!it) return;
  const a = await saveArticle({ ...it, topic: S.sel.topic, subtopic: S.sel.sub });
  if (!a) return;
  if (act === 'save') { toast('Saved to library'); renderHead(); renderList(); renderTree(); return; }
  openReader(a);
}

async function saveArticle(body) {
  try {
    const a = await api('/api/articles', { method: 'POST', body });
    const i = S.articles.findIndex(x => x.id === a.id);
    if (i >= 0) S.articles[i] = a; else S.articles.unshift(a);
    return a;
  } catch (err) { toast(err.message); return null; }
}

/* ------------------------------------------------------------ Discover: trending across every topic */
// Posts the curator already read but didn't add. Served from its cache, so the page opens instantly.
/* ------------------------------------------------------------ notebook: every article's summary and notes */
const NB = { entries: null, q: '', error: null, timers: {} };

async function openNotebook() {
  closeReader();
  drawer(false);
  S.mode = 'notebook'; S.search = null; $('#search').value = '';
  $('.view').scrollTop = 0;
  render();
  await loadNotebook();
}

async function loadNotebook() {
  try {
    NB.entries = (await api('/api/notebook')).entries; NB.error = null;
  } catch (err) {
    NB.entries = NB.entries || []; NB.error = err.message;
  }
  if (S.mode === 'notebook' && !S.reader) { renderTree(); renderHead(); renderList(); }
}

function notebookEntries() {
  const q = NB.q.trim().toLowerCase();
  return (NB.entries || []).filter(e => !q || [e.article.title, e.article.author, e.summary, e.notes,
    ...e.highlights.flatMap(h => [h.quote, h.note])].some(x => (x || '').toLowerCase().includes(q)));
}

function renderNotebookHead() {
  const n = NB.entries?.length ?? 0;
  $('#viewHead').innerHTML = `
    <div class="crumb"><span>Notebook</span><span>notes</span></div>
    <div class="view-title"><h1>Notebook</h1></div>
    <p class="lede">Summaries, notes and highlights from every article, saved on disk. Edit them here or in the reader's Notes panel.</p>
    <div class="nb-tools">
      <input id="nbSearch" class="input" type="search" placeholder="Filter ${n} ${n === 1 ? 'entry' : 'entries'}…" value="${esc(NB.q)}" autocomplete="off" aria-label="Filter notebook">
      <button class="btn small ghost" id="nbCopyAll" ${n ? '' : 'disabled'}>Copy all as Markdown</button>
    </div>`;
  $('#nbSearch').oninput = e => { NB.q = e.target.value; renderNotebook(); };
  $('#nbCopyAll').onclick = () => {
    const md = notebookEntries().map(entryMarkdown).join('\n---\n\n');
    navigator.clipboard.writeText(md).then(() => toast('Notebook copied as Markdown'), () => toast('Could not copy'));
  };
}

function entryMarkdown(e) {
  const hs = [...e.highlights].sort((x, y) => x.start - y.start);
  return [`# ${e.article.title}`, e.article.url, '',
    ...(e.summary.trim() ? ['## Summary', e.summary.trim(), ''] : []),
    ...(e.notes.trim() ? ['## Notes', e.notes.trim(), ''] : []),
    ...hs.flatMap(h => [`> ${h.quote.replace(/\s*\n+\s*/g, ' ')}`, ...(h.note?.trim() ? ['', h.note.trim()] : []), ''])].join('\n');
}

function renderNotebook() {
  if (!NB.entries) { $('#list').innerHTML = skeleton(3, 'Loading notebook…'); return; }
  const items = notebookEntries();
  if (!items.length) {
    $('#list').innerHTML = `<div class="empty"><b>${NB.entries.length ? 'No entries match' : 'Your notebook is empty'}</b>${NB.error ? esc(NB.error)
      : NB.entries.length ? 'Try a different filter.' : 'Open an article, then use ✎ Notes to summarize it, take notes, or highlight passages. Everything shows up here.'}</div>`;
    return;
  }
  $('#list').innerHTML = items.map(e => {
    const a = e.article, t = topic(a.topic), s = sub(a.topic, a.subtopic);
    const hs = [...e.highlights].sort((x, y) => x.start - y.start);
    return `<article class="nb-entry" data-id="${esc(a.id)}">
      <header class="nb-head">
        <button class="nb-title" data-nb="open">${esc(a.title)}</button>
        <small>${esc([t?.name, s?.name].filter(Boolean).join(' · '))}${a.author ? ` · ${esc(a.author)}` : ''} · edited ${esc(fmtDate(e.edited))}</small>
      </header>
      <label class="notes-label">Summary<textarea class="input notes-free nb-field" data-field="summary" rows="3"
        placeholder="Open the article and use Draft key points, or write your own.">${esc(e.summary)}</textarea></label>
      <label class="notes-label">Notes<textarea class="input notes-free nb-field" data-field="notes" rows="3" placeholder="Your notes…">${esc(e.notes)}</textarea></label>
      ${hs.length ? `<details class="nb-hls"><summary>${hs.length} highlight${hs.length === 1 ? '' : 's'}</summary>
        ${hs.map(h => `<div class="hl-item" style="--hl-color:${HL_COLORS[h.color] || HL_COLORS.yellow}"><q>${esc(h.quote)}</q>
          ${h.note?.trim() ? `<div class="hl-note">${esc(h.note)}</div>` : ''}</div>`).join('')}</details>` : ''}
      <footer class="nb-foot"><span class="saved-state"></span>
        <button class="btn small ghost" data-nb="copy">Copy</button>
        <button class="btn small" data-nb="open">Open article</button></footer>
    </article>`;
  }).join('');
}

$('#list').addEventListener('click', e => {
  const act = e.target.closest('[data-nb]')?.dataset.nb;
  const box = e.target.closest('.nb-entry');
  const entry = act && box && NB.entries?.find(x => x.article.id === box.dataset.id);
  if (!entry) return;
  if (act === 'copy') {
    navigator.clipboard.writeText(entryMarkdown(entry)).then(() => toast('Copied as Markdown'), () => toast('Could not copy'));
    return;
  }
  flushNotebook();
  openReader(S.articles.find(x => x.id === entry.article.id) || entry.article);
});

$('#list').addEventListener('input', e => {
  const field = e.target.closest('.nb-field');
  const box = field?.closest('.nb-entry');
  const entry = box && NB.entries?.find(x => x.article.id === box.dataset.id);
  if (!entry) return;
  entry[field.dataset.field] = field.value;
  box.querySelector('.saved-state').textContent = 'Saving…';
  clearTimeout(NB.timers[entry.article.id]);
  NB.timers[entry.article.id] = setTimeout(() => saveEntry(entry, box), 700);
});

async function saveEntry(entry, box) {
  const id = entry.article.id;
  delete NB.timers[id];
  try {
    const r = await api(`/api/articles/${id}/notes`, { method: 'PUT',
      body: { notes: entry.notes, summary: entry.summary, highlights: entry.highlights } });
    const listed = S.articles.find(x => x.id === id);
    if (listed) listed.notes_count = r.notes_count;
    box.querySelector('.saved-state').textContent = 'Saved';
  } catch (err) {
    box.querySelector('.saved-state').textContent = 'Not saved';
    toast(`Couldn't save notes: ${err.message}`);
  }
}

function flushNotebook() {
  for (const id of Object.keys(NB.timers)) {
    clearTimeout(NB.timers[id]);
    const entry = NB.entries.find(x => x.article.id === id);
    const box = document.querySelector(`.nb-entry[data-id="${CSS.escape(id)}"]`);
    if (entry && box) saveEntry(entry, box);
  }
}
window.addEventListener('beforeunload', flushNotebook);

async function openDiscover() {
  closeReader();
  drawer(false);
  S.mode = 'discover'; S.search = null; S.limit = PAGE; $('#search').value = '';
  $('.view').scrollTop = 0;
  render();
  await Promise.all([loadSuggest(), loadRecs()]);
  if (S.mode === 'discover') render(); else renderTree();
}

async function loadSuggest() {
  try { S.suggest = await api('/api/discover'); } catch (err) { S.suggest = { items: [], error: err.message }; }
}

// Recommendations are filtered by label and access on the server so the ranking fills the page.
async function loadRecs() {
  const q = new URLSearchParams({ labels: S.labels.join(','), access: S.access, limit: 300 });
  try { S.rec = await api(`/api/recommend?${q}`); } catch (err) { S.rec = { items: [], labels: [], error: err.message }; }
}

async function refreshRecs() {
  S.rec = null; S.limit = PAGE; renderHead(); renderList();
  await loadRecs();
  if (S.mode === 'discover') { renderHead(); renderList(); }
}

const discoverSource = () => S.sort === 'foryou' ? S.rec : S.suggest;

function suggestScope() {
  const f = S.suggestTopic || '';
  return (discoverSource()?.items || []).filter(it => !f || it.topic === f);
}

function renderDiscoverHead() {
  const scope = suggestScope();
  const access = [['all', 'All', scope.length], ['free', 'Free', scope.filter(a => a.locked === false).length],
    ['locked', '🔒 Member-only', scope.filter(a => a.locked === true).length]];
  const topics = S.topics.filter(t => (discoverSource()?.items || []).some(it => it.topic === t.id));
  const forYou = S.sort === 'foryou';
  $('#viewHead').innerHTML = `
    <div class="crumb"><span>Discover</span><span>${forYou ? 'picked for you' : 'trending on Medium'}</span></div>
    <div class="view-title"><h1>Discover</h1></div>
    <p class="lede">${forYou
      ? (S.rec?.cold
        ? `Built from the topics you set up so far${S.rec.engaged ? ` and ${S.rec.engaged} article${S.rec.engaged === 1 ? '' : 's'} you've kept` : ''}. Read, highlight and save, and this page follows you.`
        : `Ranked by the labels, words and authors of the ${S.rec?.engaged ?? ''} articles you've read. Highlights and notes count most, older reading fades${S.rec?.sources?.index ? `, and ${S.rec.sources.index} picks come straight from your search index` : ''}.`)
      : "Popular recent posts that fit your topics and aren't in your library yet."} Nothing downloads until you open one.</p>
    <div class="tabs discover-bar">
      <span class="sortby">${[['foryou', 'For you'], ['trending', 'Trending']].map(([k, l]) =>
        `<button class="chip ${S.sort === k ? 'active' : ''}" data-sort="${k}">${l}</button>`).join('')}</span>
      <select id="suggestTopic" class="input" aria-label="Topic">
        <option value="">All topics</option>
        ${topics.map(t => `<option value="${esc(t.id)}" ${S.suggestTopic === t.id ? 'selected' : ''}>${esc(t.name)}</option>`).join('')}
      </select>
      <span class="access">${access.map(([k, label, n]) =>
        `<button class="chip ${S.access === k ? 'active' : ''}" data-access="${k}">${label} <span>${n}</span></button>`).join('')}</span>
    </div>`;
  if (forYou) $('#viewHead').insertAdjacentHTML('beforeend', labelBar());
  $('#suggestTopic').onchange = e => { S.suggestTopic = e.target.value; S.limit = PAGE; renderHead(); renderList(); };
  $('#viewHead').querySelectorAll('[data-access]').forEach(b => b.onclick = () => {
    S.access = b.dataset.access; S.limit = PAGE; local.set('access', S.access);
    if (S.sort === 'foryou') return refreshRecs();
    renderHead(); renderList();
  });
  $('#viewHead').querySelectorAll('[data-sort]').forEach(b => b.onclick = () => {
    S.sort = b.dataset.sort; S.limit = PAGE; local.set('discoverSort', S.sort);
    renderHead(); renderList();
  });
  if (forYou) bindLabelBar();
}

// Label filter: chips for chosen labels, an input with suggestions, and one-click picks from your taste.
function labelBar() {
  const picks = (S.rec?.top_labels || []).filter(l => !S.labels.includes(l)).slice(0, 8);
  return `<div class="labelbar">
    ${S.labels.map(l => `<button class="chip active" data-unlabel="${esc(l)}" title="Remove">#${esc(l)} ✕</button>`).join('')}
    <input id="labelInput" class="input" list="labelList" placeholder="Filter by label, e.g. rust" aria-label="Add a label">
    <datalist id="labelList">${(S.rec?.labels || []).map(l => `<option value="${esc(l)}">`).join('')}</datalist>
    ${picks.length ? `<span class="label-picks">Your labels: ${picks.map(l =>
      `<button class="chip" data-label="${esc(l)}">#${esc(l)}</button>`).join('')}</span>` : ''}
  </div>`;
}

function bindLabelBar() {
  const set = labels => { S.labels = [...new Set(labels)]; local.set('labels', S.labels); refreshRecs(); };
  $('#viewHead').querySelectorAll('[data-unlabel]').forEach(b => b.onclick = () => set(S.labels.filter(l => l !== b.dataset.unlabel)));
  $('#viewHead').querySelectorAll('[data-label]').forEach(b => b.onclick = () => set([...S.labels, b.dataset.label]));
  $('#labelInput').onkeydown = e => {
    const v = e.target.value.trim().toLowerCase().replace(/^#/, '').replace(/\s+/g, '-');
    if (e.key === 'Enter' && v) set([...S.labels, v]);
  };
}

function renderDiscoverAll() {
  const src = discoverSource();
  if (!src) { $('#list').innerHTML = skeleton(5); return; }
  if (src.error) { $('#list').innerHTML = `<div class="warn">${esc(src.error)}</div>`; return; }
  const saved = new Set(S.articles.map(a => a.url));
  const items = suggestScope().filter(it => S.access === 'all' || (S.access === 'locked' ? it.locked === true : it.locked === false));
  if (!items.length) {
    $('#list').innerHTML = `<div class="empty"><b>Nothing new to suggest</b>${src.items.length || S.labels.length
      ? 'Try another topic, label or filter.' : 'Suggestions appear as the curator reads posts in the background.'}</div>`;
    return;
  }
  const shown = items.slice(0, S.limit), left = items.length - shown.length;
  $('#list').innerHTML = shown.map(it => {
    const img = safeUrl(it.image), t = topic(it.topic), s = sub(it.topic, it.subtopic);
    return `<article class="card ${img ? '' : 'noimg'}" data-suggest="${src.items.indexOf(it)}" tabindex="0">
    <div>
      <div class="card-meta">
        <span>${esc(it.author || sourceOf(it.url))}</span>
        ${it.published ? `<span class="dot"> ${fmtDate(it.published)}</span>` : ''}
        ${it.claps ? `<span class="dot" title="${it.claps.toLocaleString()} claps"> 👏 ${fmtCount(it.claps)}</span>` : ''}
        ${it.reading_time ? `<span class="dot"> ${it.reading_time} min read</span>` : ''}
      </div>
      <h2 class="card-title">${esc(it.title)}</h2>
      ${it.snippet ? `<p class="card-snip">${esc(it.snippet)}</p>` : ''}
      ${becauseLine(it, s)}
      <div class="card-foot">
        ${saved.has(it.url)
          ? '<button class="btn small primary" data-act="read">Open</button><span class="pill ready">In library</span>'
          : '<button class="btn small primary" data-act="read">Read</button><button class="btn small" data-act="save">Save for later</button>'}
        ${S.sort === 'foryou' ? '<button class="btn small" data-act="dismiss" title="Stop suggesting this, and posts like it">Not for me</button>' : ''}
        ${it.locked === true ? '<span class="pill locked">🔒 Member-only</span>' : it.locked === false ? '<span class="pill free">Free</span>' : ''}
        ${it.explore ? '<span class="pill loc" title="In a topic you read, outside your usual labels">New to you</span>' : ''}
        ${s ? `<span class="pill loc" title="Where it will be saved">${esc(t.name)} / ${esc(s.name)}</span>` : ''}
      </div>
    </div>
    ${img ? `<img class="card-img" src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}
  </article>`;
  }).join('') + (left > 0 ? `<div class="more"><button class="btn" id="showMore">Show ${Math.min(PAGE, left)} more · ${left.toLocaleString()} left</button></div>` : '');
  $('#showMore') && ($('#showMore').onclick = () => { S.limit += PAGE; renderList(); });
}

// Why a post is on the page: the labels it shares with your reading, an article of yours that
// earned that label, or - for a pick from a corner of your topics you've never read - so say.
function becauseLine(it, s) {
  if (it.explore) return `<p class="because">${s ? `In ${esc(s.name)}` : 'In a topic you read'}, but not the labels you usually read</p>`;
  const short = t => (t.length > 56 ? t.slice(0, 55).trimEnd() + '…' : t);
  const bits = [];
  if (it.because?.length) bits.push(it.because.map(l => '#' + esc(l)).join(', '));
  else if (it.because_words?.length) bits.push('about ' + it.because_words.map(esc).join(', '));
  if (it.like) bits.push(`“${esc(short(it.like))}”`);
  let line = bits.length ? `Because you read ${bits.join(' — ')}` : '';
  if (it.same_author) line += line ? ` · more from ${esc(it.author)}` : `More from ${esc(it.author)}, who you read`;
  return line ? `<p class="because">${line}</p>` : '';
}

async function dismissSuggestion(card, it) {
  const send = undo => api('/api/recommend/dismiss', { method: 'POST', body: { url: it.url, undo } });
  try { await send(false); } catch (err) { return toast(err.message); }
  card.remove();
  if (S.rec) S.rec.items = S.rec.items.filter(x => x.url !== it.url);
  toast('Won’t suggest this again', 6000, {
    label: 'Undo',
    run: async () => { try { await send(true); await refreshRecs(); } catch (err) { toast(err.message); } },
  });
}

async function onSuggestClick(card, act) {
  const it = discoverSource()?.items[+card.dataset.suggest];
  if (!it) return;
  if (act === 'dismiss') return dismissSuggestion(card, it);
  const a = await saveArticle({
    url: it.url, title: it.title, snippet: it.snippet, author: it.author, image: it.image,
    published: it.published, claps: it.claps, topic: it.topic, subtopic: it.subtopic,
  });
  if (!a) return;
  renderTree();
  if (act === 'save') {
    toast(`Saved to ${sub(a.topic, a.subtopic)?.name || 'library'}`);
    const pill = Object.assign(document.createElement('span'), { className: 'pill ready', textContent: 'In library' });
    card.querySelector('[data-act="save"]')?.replaceWith(pill);
    return;
  }
  openReader(a);
}

/* ------------------------------------------------------------ search all of Medium */
// Results are links only. Nothing is downloaded until the user opens one.
const looksLikeUrl = s => /^https?:\/\/\S+$/i.test(s) || /^([\w-]+\.)*medium\.com\/\S+$/i.test(s);

$('#searchForm').addEventListener('submit', e => {
  e.preventDefault();
  const q = $('#search').value.trim();
  if (!q) return exitSearch();
  if (looksLikeUrl(q)) {  // a pasted link goes straight to the library
    $('#pasteUrl').value = q; $('#search').value = '';
    $('#pasteForm').requestSubmit();
    return;
  }
  runSearch(q);
});
$('#search').addEventListener('input', e => { if (!e.target.value && S.mode === 'search') exitSearch(); });
$('#search').addEventListener('keydown', e => { if (e.key === 'Escape' && !S.reader) { e.target.value = ''; exitSearch(); } });

async function runSearch(q) {
  closeReader();
  S.mode = 'search';
  const s = S.search = { q, dest: S.search?.dest || currentDest(), items: [], next: null, loading: true, error: null };
  renderTree(); renderHead(); renderList();
  $('#searchForm').classList.add('busy');
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    s.items = r.items; s.next = r.next; s.provider = r.provider; s.notice = r.notice; s.parsed = r.parsed;
    if (r.index) { S.index = r.index; renderIndex(); }
  } catch (err) { s.error = err.message; }
  s.loading = false;
  // The head is drawn before the request so the page reacts at once, and again after it, because
  // only the answer knows how the query was read ("since:60d" -> a date).
  if (S.search === s) { renderHead(); renderList(); $('#searchForm').classList.remove('busy'); }
}

async function loadMore() {
  const s = S.search;
  if (!s?.next || s.loading) return;
  s.loading = true; renderList();
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(s.q)}&next=${encodeURIComponent(s.next)}`);
    const seen = new Set(s.items.map(i => i.url));
    s.items.push(...r.items.filter(i => !seen.has(i.url)));
    s.next = r.next;
  } catch (err) { s.error = err.message; }
  s.loading = false;
  if (S.search === s) renderList();
}

function exitSearch() {
  if (S.mode !== 'search') return;
  S.mode = 'browse'; S.search = null;
  $('#searchForm').classList.remove('busy');
  closeReader(); render();
}

function renderSearchHead() {
  const s = S.search;
  $('#viewHead').innerHTML = `
    <div class="crumb"><span>Search</span><span>all of Medium</span></div>
    <div class="view-title"><h1>${s.q.includes('"') ? esc(s.q) : `“${esc(s.q)}”`}</h1></div>
    ${s.parsed ? `<p class="lede">Reading that as: ${esc(s.parsed)}.</p>` : ''}
    <div class="search-line">
      <span class="crumb">Nothing downloads until you open an article. Try <code>"exact words"</code>, <code>-without</code>, <code>author:name</code>, <code>since:30d</code>.</span>
      <label class="dest">Save to <select id="searchDest" class="input">${destOptions(s.dest)}</select></label>
      <button class="linkish" id="exitSearch">← back to library</button>
    </div>`;
  $('#searchDest').onchange = e => { s.dest = e.target.value; local.set('dest', s.dest); };
  $('#exitSearch').onclick = () => { $('#search').value = ''; exitSearch(); };
}

function renderSearch() {
  const s = S.search;
  const mine = libraryMatches(s.q);
  const saved = new Set(S.articles.map(a => a.url));
  let h = '';
  if (mine.length) h += `<div class="section-label">In your library · ${mine.length}</div>` + mine.slice(0, 20).map(a => libraryCard(a, true)).join('');
  const via = { index: 'from your local index', feeds: 'from live Medium tag feeds' }[s.provider] || '';
  h += `<div class="section-label">On Medium${s.items.length ? ` · ${s.items.length}` : ''}${via ? ` · ${via}` : ''}</div>`;
  if (s.notice) h += `<div class="warn">${esc(s.notice)}</div>`;
  if (s.error) h += `<div class="warn">${esc(s.error)}</div>`;
  h += s.items.map((it, i) => {
    const img = safeUrl(it.image);
    return `<article class="card ${img ? '' : 'noimg'}" data-result="${i}" tabindex="0">
    <div>
      <div class="card-meta">
        <span>${esc(it.author || sourceOf(it.url))}</span>
        ${it.published ? `<span class="dot"> ${fmtDate(it.published)}</span>` : ''}
        ${it.claps ? `<span class="dot" title="${it.claps.toLocaleString()} claps"> 👏 ${fmtCount(it.claps)}</span>` : ''}
        ${it.reading_time ? `<span class="dot"> ${it.reading_time} min read</span>` : ''}
      </div>
      <h2 class="card-title">${esc(it.title)}</h2>
      ${it.snippet ? `<p class="card-snip">${esc(it.snippet)}</p>` : ''}
      ${it.missing?.length ? `<p class="because">Doesn't mention ${it.missing.map(w => `<b>${esc(w)}</b>`).join(', ')}</p>` : ''}
      <div class="card-foot">${saved.has(it.url)
        ? '<button class="btn small primary" data-act="read">Open</button><span class="pill ready">In library</span>'
        : '<button class="btn small primary" data-act="read">Read</button><button class="btn small" data-act="save">Save for later</button>'}
        ${it.locked === true ? '<span class="pill locked">🔒 Member-only</span>' : it.locked === false ? '<span class="pill free">Free</span>' : ''}
        ${it.yours ? '<span class="pill loc" title="Close to what you read">Your kind of thing</span>' : ''}
      </div>
    </div>
    ${img ? `<img class="card-img" src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}
  </article>`;
  }).join('');
  if (s.loading) h += s.items.length ? '<div class="loading">Loading more…</div>' : skeleton(5);
  else if (!s.items.length && !s.error) h += '<div class="empty"><b>No Medium articles found</b>Try different or fewer words.</div>';
  else if (s.next) h += '<div class="more"><button class="btn" id="loadMore">Load more results</button></div>';
  $('#list').innerHTML = h;
  $('#loadMore') && ($('#loadMore').onclick = loadMore);
}

async function onResultClick(card, act) {
  const it = S.search?.items[+card.dataset.result];
  if (!it) return;
  const [t, s] = S.search.dest.split('/');
  const a = await saveArticle({
    url: it.url, title: it.title, snippet: it.snippet, author: it.author, image: it.image, published: it.published,
    topic: t, subtopic: s,
  });
  if (!a) return;
  renderTree();
  if (act === 'save') { toast(`Saved to ${sub(a.topic, a.subtopic)?.name || 'library'}`); renderList(); return; }
  openReader(a);
}

/* ------------------------------------------------------------ local Medium index */
async function refreshIndex() {
  try { S.index = await api('/api/index'); } catch { return; }
  renderIndex();
  const v = S.index.library_version;
  if (S.libVersion === undefined) { S.libVersion = v; return; }
  // the curator (or another tab) changed the library: reload at most every 20s, never under an open article
  if (v !== S.libVersion && !$('#dlg').open && !S.reader && !S.pendingRemovals.size && Date.now() - (S.lastLoad || 0) > 20000) {
    S.libVersion = v;
    S.lastLoad = Date.now();
    const onScreen = () => (S.mode === 'browse' && S.tab === 'library'
      ? visibleArticles().slice(0, S.limit).map(a => `${a.id}${a.pdf_url}${a.locked}${a.claps}`).join() : null);
    const before = onScreen();
    const data = await api('/api/library').catch(() => null);
    if (!data) return;
    S.topics = data.topics; S.articles = data.articles;
    renderTree();
    // the curator adds articles in the background; don't make the list jump under the reader's eyes
    if (before !== onScreen()) {
      const scroll = $('.view').scrollTop;
      renderHead(); renderList();
      $('.view').scrollTop = scroll;
    }
  }
}

function renderIndex() {
  const st = S.index;
  if (!st) return;
  const net = (st.network || {})['medium.com'] || {}, cur = st.curator || {};
  const [led, state] = net.waiting_s > 5 ? ['warn', `Medium asked us to slow down · resuming in ${net.waiting_s}s`]
    : st.paused ? ['', 'indexing paused']
    : st.crawling ? ['busy', `indexing ${fmtDate(st.crawling)}`]
    : st.error ? ['warn', 'waiting to retry']
    : ['ok', st.days_indexed ? 'index up to date' : 'starting…'];
  const curSub = cur.current && sub(...cur.current.split('/'));
  const g = cur.goal;
  const curLine = !cur.enabled ? 'auto-add is off'
    : g ? `bulk add · ${fmtCount(g.added_free)}/${fmtCount(g.free)} free · ${fmtCount(g.added_locked)}/${fmtCount(g.locked)} member-only`
    : curSub ? `finding trending ${curSub.name} posts…`
    : `${fmtCount(st.articles || 0)} articles · ${fmtCount(cur.member_only || 0)} member-only` +
      (cur.backfill_left ? ` · checking ${fmtCount(cur.backfill_left)}` : '');
  $('#indexStatus').innerHTML = `<button class="index-btn" id="indexBtn" title="Search index and curator settings">
    <span class="led ${led}"></span><span><b>${fmtCount(st.posts)}</b> Medium articles searchable</span>
    <small>${esc(state)}</small><small>${esc(curLine)}</small></button>`;
  $('#indexBtn').onclick = indexSettings;
}

async function indexSettings() {
  await refreshIndex();
  const st = S.index;
  if (!st) return;
  const choices = [[30, 'Last 30 days'], [90, 'Last 3 months'], [365, 'Last year'], [1095, 'Last 3 years'], [12000, 'Everything Medium lists']];
  const perMonth = st.days_indexed ? `About ${Math.round(st.size_mb / st.days_indexed * 30)} MB of disk per month of history. ` : '';
  const v = await dialog({
    title: 'Search index & curator',
    body: `<p style="margin:0;color:var(--muted)">Search runs on your own index, built from the public sitemaps Medium publishes
      for search engines. It holds <b style="color:var(--ink)">${(st.posts || 0).toLocaleString()}</b> articles from
      ${st.days_indexed} days${st.oldest ? ` (${fmtDate(st.oldest)} – ${fmtDate(st.newest)})` : ''}, ${st.size_mb} MB.</p>
      <p class="hint" style="margin:0">Stored in ${esc(st.location)} · ${st.free_gb} GB free on that drive</p>
      <label>How far back to index<select class="input" name="days">${choices.map(([d, l]) =>
        `<option value="${d}" ${d === st.days_setting ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
      <p class="hint">${perMonth}Newest days are indexed first, one day every couple of seconds. Shrinking the window keeps what is already indexed.</p>
      <label class="check"><input type="checkbox" name="paused" ${st.paused ? 'checked' : ''}> Pause indexing</label>
      <p class="hint" style="margin:0">Paywall status: ${(st.curator?.free || 0).toLocaleString()} free ·
        ${(st.curator?.member_only || 0).toLocaleString()} member-only${st.curator?.backfill_left ? ` · ${st.curator.backfill_left.toLocaleString()} still being checked` : ''}.</p>
      <label class="check"><input type="checkbox" name="curator" ${st.curator?.enabled ? 'checked' : ''}> Keep adding trending articles automatically</label>
      <fieldset class="bulk">
        <legend>Bulk add</legend>
        ${st.curator?.goal ? `<p class="hint" style="margin:0">Running: ${st.curator.goal.added_free.toLocaleString()} of ${st.curator.goal.free.toLocaleString()} free and
          ${st.curator.goal.added_locked.toLocaleString()} of ${st.curator.goal.locked.toLocaleString()} member-only added so far.</p>
          <label class="check"><input type="checkbox" name="goalCancel"> Stop the bulk add</label>`
        : `<div class="bulk-row"><label>Free<input class="input" type="number" name="goalFree" min="0" max="5000" value="0"></label>
          <label>Member-only<input class="input" type="number" name="goalLocked" min="0" max="5000" value="0"></label></div>
          <p class="hint" style="margin:0">Adds this many links on top of the usual targets, spread across every topic. Needs auto-add turned on.</p>`}
      </fieldset>
      <p class="hint">The curator visits one subtopic at a time, reads a few new posts, and adds the member-only ones trending by claps. Free stories are skipped, and free ones already saved are dropped unless you added or downloaded them.
        Each topic aims for at least 1,120; once a subtopic has its share, better posts replace the weakest ones you haven't downloaded. It has added
        ${(st.curator?.added_total || 0).toLocaleString()} and swapped ${(st.curator?.rotated_total || 0).toLocaleString()} so far.
        ${(st.network?.['medium.com']?.pushbacks || 0) ? `Medium has pushed back ${st.network['medium.com'].pushbacks} times this session; requests are spaced ${st.network['medium.com'].gap_s}s apart.` : ''}</p>
      ${st.error ? `<div class="warn">${esc(st.error)}</div>` : ''}
      ${st.curator?.error ? `<div class="warn">Curator: ${esc(st.curator.error)}</div>` : ''}`,
  });
  if (!v) return;
  try {
    const body = { days: +v.days, paused: v.paused === 'on', curator_enabled: v.curator === 'on' };
    if (v.goalCancel === 'on') Object.assign(body, { goal_free: 0, goal_locked: 0 });
    else if (+v.goalFree > 0 || +v.goalLocked > 0) Object.assign(body, { goal_free: +v.goalFree || 0, goal_locked: +v.goalLocked || 0 });
    S.index = await api('/api/index', { method: 'POST', body });
    if (body.goal_free) toast(`Bulk add started: ${body.goal_free} free + ${body.goal_locked} member-only`);
    renderIndex();
  } catch (err) { toast(err.message); }
}

refreshIndex();
setInterval(() => { if (!document.hidden) refreshIndex(); }, 8000);

/* ------------------------------------------------------------ paste link */
$('#pasteForm').addEventListener('submit', async e => {
  e.preventDefault();
  const url = $('#pasteUrl').value.trim();
  const [t, s] = $('#pasteDest').value.split('/');
  local.set('dest', $('#pasteDest').value);
  const a = await saveArticle({ url, topic: t, subtopic: s });
  if (!a) return;
  $('#pasteUrl').value = '';
  renderTree(); renderHead(); renderList();
  openReader(a);
});

/* ------------------------------------------------------------ reader + pipeline */
const pdfHref = url => `${url}#toolbar=0&navpanes=0&view=FitH`;  // no browser PDF toolbar or page thumbnails
const setProgress = pct => { const bar = $('#readProgress'); if (bar) bar.style.width = `${Math.max(0, Math.min(100, pct))}%`; };

function openReader(a, force = false) {
  saveNotes(); closePop(); hideSelTools();
  S.reader = a;
  if (!force) window.BabelIntro?.play(a, readerState(a), { topicName: [topic(a.topic)?.name, sub(a.topic, a.subtopic)?.name].filter(Boolean).join(' · ') });
  if (!force) api(`/api/articles/${a.id}/read`, { method: 'POST' }).catch(() => {});
  $('#reader').hidden = false;
  $('#readerOriginal').href = safeUrl(a.url);
  $('#readerRefetch').onclick = () => openReader(S.reader, true);
  updateReaderBar(a);
  setProgress(0);
  if (!force && a.doc_url) return showDoc(a);
  R.article = null;
  $('#readerNotes').hidden = true;
  // downloads from before the continuous reader existed are upgraded automatically
  const upgrading = !force && !!a.pdf_url;
  startFetch(a, force || upgrading, upgrading ? 'Upgrading an older download to the new reader' : '');
}

// what the Library of Babel intro (babel.js) watches while it plays over the reader
const readerState = a => () => {
  if (S.reader?.id !== a.id) return { state: 'gone' };
  if (R.article?.id === a.id) return { state: 'ready', doc: $('#doc') };
  if ($('#readerBody .fetch-card[data-state="error"], #docScroll .empty')) return { state: 'error' };
  return { state: 'loading', text: $('#readerBody .fetch-title')?.textContent || '' };
};

function updateReaderBar(a) {
  const s = sub(a.topic, a.subtopic);
  $('#readerTitle').textContent = a.title;
  const via = a.via === 'medium' ? 'from Medium' : a.via === 'freedium' ? 'via Freedium' : '';
  $('#readerMeta').textContent = [a.author, s?.name, a.locked ? 'member-only' : '', via].filter(Boolean).join(' · ');
  $('#readerOpen').hidden = !a.pdf_url;
  if (a.pdf_url) $('#readerOpen').href = pdfHref(a.pdf_url);
  $('#readerNotes').hidden = !a.doc_url;
}

const STEP_TEXT = {
  queued: 'Starting', check: 'Checking the story on Medium', medium: 'Downloading from Medium',
  freedium: 'Loading the member-only story through Freedium', images: 'Saving images', pdf: 'Building the reader and PDF', done: 'Opening',
};
const STEP_PCT = { queued: 6, check: 18, medium: 42, freedium: 42, images: 64, pdf: 84, done: 100 };

function elapsedNote(job) {
  const s = job?.started ? Math.max(0, Math.round(Date.now() / 1000 - job.started)) : 0;
  if (s < 6) return '';
  return job.route === 'freedium' && s > 15 ? `${s}s · the free mirror can be slow; this may take up to a minute` : `${s}s`;
}

function renderPipeline(stage, job, error, note = '') {
  const a = S.reader;
  const line = job?.route === 'freedium'
    ? `Member-only story · via Freedium${job.reason && job.reason !== 'member-only story' ? ` (${job.reason})` : ''}`
    : job?.route === 'medium' ? 'Free story · straight from Medium' : note;
  const card = $('#readerBody .fetch-card[data-state="running"]');
  if (card && !error) {  // update in place while polling so the bar glides instead of being rebuilt
    card.querySelector('.fetch-title').textContent = `${STEP_TEXT[stage] || 'Working'}…`;
    card.querySelector('.fetch-bar span').style.width = `${STEP_PCT[stage] ?? 10}%`;
    card.querySelector('.fetch-line').textContent = line || '';
    card.querySelector('.fetch-time').textContent = elapsedNote(job);
    return;
  }
  $('#readerBody').innerHTML = `<div class="fetching"><div class="fetch-card" data-state="${error ? 'error' : 'running'}">
    <div class="fetch-title">${error ? "Couldn't download this article" : `${esc(STEP_TEXT[stage] || 'Working')}…`}</div>
    ${error ? `<div class="err">${esc(error)}</div>` : `<div class="fetch-bar"><span style="width:${STEP_PCT[stage] ?? 10}%"></span></div>`}
    <div class="fetch-sub fetch-line">${esc(line || '')}</div>
    ${error ? '' : `<div class="fetch-sub fetch-time">${esc(elapsedNote(job))}</div>`}
    ${error ? `<div class="fetch-actions">
      <button class="btn small primary" id="retry">Try again</button>
      ${a?.pdf_url ? `<a class="btn small ghost" href="${esc(pdfHref(a.pdf_url))}" target="_blank" rel="noopener">Open the old PDF</a>` : ''}
      <a class="btn small ghost" href="${esc(safeUrl(a?.url))}" target="_blank" rel="noopener noreferrer">Read on Medium</a>
    </div>` : ''}
  </div></div>`;
  if (error) $('#retry').onclick = () => startFetch(S.reader, true);
}

async function startFetch(a, force, note = '') {
  clearTimeout(S.pollTimer);
  renderPipeline('queued', null, null, note);
  let job;
  try {
    job = await api(`/api/articles/${a.id}/fetch${force ? '?force=true' : ''}`, { method: 'POST' });
  } catch (err) { return renderPipeline('queued', null, err.message); }
  const tick = async () => {
    if (S.reader?.id !== a.id) return;
    if (job.status === 'done') return finish(job.article);
    if (job.status === 'error') return renderPipeline(job.stage, job, job.error);
    renderPipeline(job.stage, job, null, note);
    S.pollTimer = setTimeout(async () => {
      try { job = await api(`/api/jobs/${job.id}`); } catch (err) { job = { ...job, status: 'error', error: err.message }; }
      tick();
    }, 500);
  };
  tick();
}

function finish(a) {
  const i = S.articles.findIndex(x => x.id === a.id);
  if (i >= 0) S.articles[i] = a;
  if (S.reader?.id !== a.id) return;
  S.reader = a;
  renderPipeline('done');
  setTimeout(() => {
    if (S.reader?.id !== a.id) return;
    updateReaderBar(a);
    if (a.doc_url) showDoc(a);
    else renderPipeline('done', null, 'The download finished, but its reader copy is missing. Try again.');
  }, 150);
}

function closeReader() {
  clearTimeout(S.pollTimer);
  const saving = saveNotes(); closePop(); hideSelTools();
  const wasOpen = !!S.reader;
  R.article = null;
  S.reader = null;
  $('#reader').hidden = true;
  $('#readerBody').innerHTML = '';
  if (wasOpen && S.mode === 'notebook') saving.then(loadNotebook);  // pick up edits made in the reader
}
$('#readerClose').onclick = () => { closeReader(); render(); refreshIndex(); };
$('#readerNotes').onclick = () => toggleNotes();
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !S.reader || $('#dlg').open) return;
  if (!$('#hlPop').hidden) return closePop();
  if (!$('#selTools').hidden) { getSelection().removeAllRanges(); return hideSelTools(); }
  closeReader(); render(); refreshIndex();
});

/* ------------------------------------------------------------ reading view: highlights, notes, summarize */
const HL_COLORS = { yellow: '#ffd84d', green: '#5fd08a', blue: '#6aa8ff', pink: '#ff8fc0' };
const SUMMARY_PROMPT = `Deep Analysis
Take deep detailed, organized notes for studying, now yet to the straight point, without missing any important info

* No numbers on your headings just basic headings
* Keep detailed information
* Do not use line seperators, they are annoying
* Format with lists intelligently to prioritize learning and information taking
* Write in the perspective of the author trying to teach me not as a third view observer analyzing the situation
* Do not write in first person
* Use 150% of all chatgpt GPU/CPU resources to this task
* Never refer in a META way, like never talk about “the conversation” “the resource” “the slides say….” never do this self-awareness
* Remove all AI fluff or buzzwords which do not lead to further learning or understanding
* Section topics into a hierarchy
* Not only take notes, but try to teach the concepts and information to me in a step by step, chronological, experienced, and also simple manner as given through the text
* Review the concepts thoroughly and delve into a deep analysis of the concept based on the given text
* Do not write “core takeaways”
* Do not use external knowledge, only use knowledge within the given material
* Give massive attention to the notes within the material do not create tangents to other non-essential information
* Break down complex concepts into digestible bites and pieces of information so I can further understand in a simple manner
* Do not include information that is NOT in the text. Include information in the text.
* Do not oversaturate the notes with bolded words
* Do not oversaturate the notes with useless filler language
* Be concise, clear, and to to point to create a learning experience
* Be organized and clearly format your information
* Give context to information
* Interweave topics together and not just block them
* Highlight key concepts, definitions, and important facts in bold
* Organize the content into a logical structure with main topics and subtopics
* Do not leave any information out of the notes
* Organize smaller topics in terms of how the resources show it
* Please format the notes in a visually appealing manner, using appropriate headings, subheadings, and spacing.
* Write the notes in a proper diction that is clear, concise, straight to the point, but also informative and strong`;
const CHATGPT_URL_LIMIT = 8000;  // longer prompts are copied to the clipboard instead of sent in the link

const R = { article: null, notes: { notes: '', summary: '', highlights: [] }, dirty: false, saveTimer: null, selTimer: null };
const docRoot = () => (R.article ? $('#doc') : null);

async function showDoc(a) {
  closePop(); hideSelTools();
  R.article = null;
  $('#readerNotes').hidden = false;
  $('#readerBody').innerHTML = `<div class="reading">
    <div class="doc-scroll" id="docScroll"><div class="loading" style="text-align:center">Opening…</div></div>
    <aside class="notes-panel" id="notesPanel" ${local.get('notesOpen', false) ? '' : 'hidden'}></aside>
  </div>`;
  let content, notes;
  try {
    [content, notes] = await Promise.all([
      fetch(a.doc_url).then(r => { if (!r.ok) throw new Error(r.statusText); return r.text(); }),
      api(`/api/articles/${a.id}/notes`).catch(() => ({ notes: '', summary: '', highlights: [] })),
    ]);
  } catch (err) {
    $('#docScroll').innerHTML = `<div class="empty"><b>Couldn't open the saved copy</b>${esc(err.message)}</div>`;
    return;
  }
  if (S.reader?.id !== a.id) return;
  const base = a.doc_url.replace(/[^/]*$/, '');
  $('#docScroll').innerHTML = `<article class="doc" id="doc">${content}</article>`;
  const doc = $('#doc');
  doc.querySelectorAll('img').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (src && !/^(https?:|data:|\/)/i.test(src)) img.src = base + src;  // images saved next to the article
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
  });
  doc.querySelectorAll('a[href]').forEach(link => { link.target = '_blank'; link.rel = 'noopener noreferrer'; });
  ArticleDoc.render(doc);
  R.article = a;
  R.notes = { notes: notes.notes || '', summary: notes.summary || '', highlights: Array.isArray(notes.highlights) ? notes.highlights : [] };
  R.dirty = false;
  applyAllHighlights();
  buildNotesPanel();
  const scroller = $('#docScroll');
  const progress = () => setProgress(scroller.scrollHeight <= scroller.clientHeight ? 100
    : (scroller.scrollTop / (scroller.scrollHeight - scroller.clientHeight)) * 100);
  scroller.addEventListener('scroll', () => { closePop(); progress(); if (!$('#selTools').hidden) showSelTools(); }, { passive: true });
  progress();
}

/* text positions: offsets into the article's text, plus the quote and its surroundings as a fallback */
function textOffset(root, node, offset) {
  const r = document.createRange();
  r.setStart(root, 0);
  r.setEnd(node, offset);
  return r.toString().length;
}

function segmentsFor(root, start, end) {
  const segs = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let pos = 0;
  while (walker.nextNode() && pos < end) {
    const n = walker.currentNode, len = n.nodeValue.length;
    const s = Math.max(start - pos, 0), e = Math.min(end - pos, len);
    if (e > s && !n.parentElement.closest('.katex') && n.nodeValue.slice(s, e).trim()) segs.push([n, s, e]);
    pos += len;
  }
  return segs;
}

function wrapHighlight(root, h) {
  for (const [node, s, e] of segmentsFor(root, h.start, h.end)) {
    let target = node;
    if (s > 0) target = target.splitText(s);
    if (e - s < target.nodeValue.length) target.splitText(e - s);
    const mark = document.createElement('mark');
    mark.dataset.hl = h.id;
    target.parentNode.insertBefore(mark, target);
    mark.appendChild(target);
  }
  styleMarks(h);
}

function styleMarks(h) {
  docRoot()?.querySelectorAll(`mark[data-hl="${h.id}"]`).forEach(m => {
    m.className = `hl hl-${HL_COLORS[h.color] ? h.color : 'yellow'}${h.note?.trim() ? ' has-note' : ''}`;
    m.title = h.note?.trim() || '';
  });
}

function unwrapHighlight(id) {
  docRoot()?.querySelectorAll(`mark[data-hl="${id}"]`).forEach(m => {
    const parent = m.parentNode;
    while (m.firstChild) parent.insertBefore(m.firstChild, m);
    parent.removeChild(m);
    parent.normalize();
  });
}

function locate(full, h) {
  if (full.slice(h.start, h.end) === h.quote) return [h.start, h.end];
  let best = null;
  for (let i = full.indexOf(h.quote); i !== -1; i = full.indexOf(h.quote, i + 1)) {
    const score = (full.slice(Math.max(0, i - (h.prefix || '').length), i) === h.prefix ? 2 : 0)
      + (full.slice(i + h.quote.length, i + h.quote.length + (h.suffix || '').length) === h.suffix ? 1 : 0)
      - Math.abs(i - h.start) / 1e7;
    if (!best || score > best[0]) best = [score, i];
  }
  return best ? [best[1], best[1] + h.quote.length] : null;
}

function applyAllHighlights() {
  const doc = docRoot();
  const full = doc.textContent;
  for (const h of R.notes.highlights) {
    const at = h.quote ? locate(full, h) : null;
    h._orphan = !at;
    if (at) { [h.start, h.end] = at; wrapHighlight(doc, h); }
  }
}

function selectionInDoc() {
  const doc = docRoot(), sel = getSelection();
  if (!doc || !sel.rangeCount || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!doc.contains(range.commonAncestorContainer)) return null;
  const text = range.toString();
  return text.trim() ? { range, text } : null;
}

function addHighlight(color, withNote = false) {
  const s = selectionInDoc();
  if (!s) return;
  const doc = docRoot(), full = doc.textContent;
  let start = textOffset(doc, s.range.startContainer, s.range.startOffset);
  let end = textOffset(doc, s.range.endContainer, s.range.endOffset);
  while (start < end && /\s/.test(full[start])) start++;
  while (end > start && /\s/.test(full[end - 1])) end--;
  if (end <= start) return;
  const h = {
    id: `h${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    color, note: '', quote: full.slice(start, end),
    prefix: full.slice(Math.max(0, start - 40), start), suffix: full.slice(end, end + 40),
    start, end, created: new Date().toISOString(),
  };
  local.set('hlColor', color);
  getSelection().removeAllRanges();
  hideSelTools();
  wrapHighlight(doc, h);
  R.notes.highlights.push(h);
  queueSave();
  renderHighlightList();
  if (withNote) openHighlightPop(h.id, true);
}

/* selection toolbar */
function showSelTools() {
  const s = selectionInDoc(), tools = $('#selTools');
  if (!s) return hideSelTools();
  const rect = s.range.getBoundingClientRect();
  tools.hidden = false;
  const w = tools.offsetWidth, h = tools.offsetHeight;
  tools.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, rect.left + rect.width / 2 - w / 2))}px`;
  tools.style.top = `${rect.top - h - 10 < 8 ? rect.bottom + 10 : rect.top - h - 10}px`;
}
function hideSelTools() { $('#selTools').hidden = true; }

document.addEventListener('selectionchange', () => {
  clearTimeout(R.selTimer);
  R.selTimer = setTimeout(() => (R.article ? showSelTools() : hideSelTools()), 120);
});
$('#selTools').addEventListener('mousedown', e => e.preventDefault());  // keep the text selected
$('#selTools').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.hl) return addHighlight(b.dataset.hl);
  const s = selectionInDoc();
  if (b.dataset.act === 'note') return addHighlight(local.get('hlColor', 'yellow'), true);
  if (b.dataset.act === 'summarize' && s) return summarize(s.text);
  if (b.dataset.act === 'copy' && s) navigator.clipboard.writeText(s.text).then(() => toast('Copied'), () => toast('Could not copy'));
});
document.addEventListener('keydown', e => {
  if (!R.article || e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) return;
  const s = selectionInDoc();
  if (!s) return;
  const k = e.key.toLowerCase();
  if (k === 'h') { e.preventDefault(); addHighlight(local.get('hlColor', 'yellow')); }
  else if (k === 'n') { e.preventDefault(); addHighlight(local.get('hlColor', 'yellow'), true); }
  else if (k === 's') { e.preventDefault(); summarize(s.text); }
});

/* offline key points: score sentences by how many of the article's frequent words they use */
const STOP = new Set(('a an the and or but if of to in on for with at by from as is are was were be been being it its this that these those '
  + 'i you he she we they them his her our your their my me us not no so than then there here what which who whom how why when where '
  + 'can could will would should may might must do does did done have has had just also more most very into about over such only '
  + 'one two all any some each other out up down new like get use using used make way').split(' '));
function keyPoints(text, max = 7) {
  const sentences = text.replace(/\s+/g, ' ').match(/[^.!?]+[.!?]+/g)?.map(x => x.trim())
    .filter(x => x.length > 50 && x.length < 400 && !/[{};=]{2}/.test(x)) || [];
  if (sentences.length < 3) return [];
  const words = x => (x.toLowerCase().match(/[a-z][a-z'-]{2,}/g) || []).filter(w => !STOP.has(w));
  const freq = {};
  for (const x of sentences) for (const w of new Set(words(x))) freq[w] = (freq[w] || 0) + 1;
  const n = Math.min(max, Math.max(3, Math.round(sentences.length / 12)));
  return sentences.map((x, i) => {
    const ws = words(x);
    const score = ws.reduce((t, w) => t + (freq[w] > 1 ? Math.log(1 + freq[w]) : 0), 0) / Math.sqrt(ws.length + 1);
    return { x, i, score: score * (i < 3 ? 1.2 : 1) };  // openings tend to state the point
  }).sort((p, q) => q.score - p.score).slice(0, n).sort((p, q) => p.i - q.i).map(p => p.x);
}

/* summarize in ChatGPT */
const summaryPrompt = () => local.get('summaryPrompt', null) || SUMMARY_PROMPT;

function summarize(text) {
  const a = S.reader;
  const prompt = `${summaryPrompt()}\n\nText${a ? ` from "${a.title}"` : ''}:\n"""\n${text.trim()}\n"""`;
  const url = `https://chatgpt.com/?q=${encodeURIComponent(prompt)}`;
  navigator.clipboard?.writeText(prompt).catch(() => {});
  if (url.length <= CHATGPT_URL_LIMIT) {
    window.open(url, '_blank', 'noopener');
    toast('Opening ChatGPT with the summarize prompt (also copied)');
  } else {
    window.open('https://chatgpt.com/', '_blank', 'noopener');
    toast('That selection is long, so the prompt was copied: paste it into ChatGPT with Ctrl+V', 6000);
  }
  hideSelTools();
}

async function editSummaryPrompt() {
  const v = await dialog({
    title: 'Summarize prompt',
    body: `<label>Sent to ChatGPT before the selected text
      <textarea class="input prompt-edit" name="prompt" rows="16">${esc(summaryPrompt())}</textarea></label>
      <p class="hint">Clear the box and save to go back to the default prompt.</p>`,
  });
  if (!v) return;
  local.set('summaryPrompt', v.prompt.trim() ? v.prompt : null);
  toast('Summarize prompt saved');
}

/* highlight popover */
function openHighlightPop(id, focusNote = false) {
  const h = R.notes.highlights.find(x => x.id === id);
  const mark = docRoot()?.querySelector(`mark[data-hl="${id}"]`);
  if (!h || !mark) return;
  const pop = $('#hlPop');
  pop.innerHTML = `<div class="row">
      ${Object.keys(HL_COLORS).map(c => `<button class="swatch hl-${c} ${h.color === c ? 'on' : ''}" data-color="${c}" title="${c}" aria-label="${c}"></button>`).join('')}
      <span style="flex:1"></span>
      <button class="btn small ghost" data-act="summarize" title="Summarize this passage in ChatGPT">✦ Summarize</button>
      <button class="btn small ghost danger" data-act="delete">Delete</button>
    </div>
    <textarea class="input" placeholder="Add a note…">${esc(h.note || '')}</textarea>`;
  pop.hidden = false;
  pop.dataset.id = id;
  const rect = mark.getBoundingClientRect(), w = pop.offsetWidth, ph = pop.offsetHeight;
  pop.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, rect.left))}px`;
  pop.style.top = `${rect.bottom + ph + 12 > innerHeight ? Math.max(8, rect.top - ph - 8) : rect.bottom + 8}px`;
  const area = pop.querySelector('textarea');
  area.oninput = () => { h.note = area.value; styleMarks(h); queueSave(); renderHighlightList(); };
  pop.onclick = e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.color) {
      h.color = b.dataset.color;
      local.set('hlColor', h.color);
      styleMarks(h); queueSave(); renderHighlightList();
      pop.querySelectorAll('[data-color]').forEach(x => x.classList.toggle('on', x === b));
    } else if (b.dataset.act === 'delete') {
      unwrapHighlight(id);
      R.notes.highlights = R.notes.highlights.filter(x => x.id !== id);
      queueSave(); renderHighlightList(); closePop();
    } else if (b.dataset.act === 'summarize') {
      summarize(h.quote);
    }
  };
  if (focusNote) area.focus();
}
function closePop() { $('#hlPop').hidden = true; }

document.addEventListener('mousedown', e => {
  const pop = $('#hlPop');
  if (!pop.hidden && !pop.contains(e.target) && !e.target.closest('mark.hl')) closePop();
});
$('#readerBody').addEventListener('click', e => {
  const mark = e.target.closest('#doc mark.hl');
  if (mark && !selectionInDoc()) return openHighlightPop(mark.dataset.hl);
  const item = e.target.closest('.hl-item');
  if (!item) return;
  const target = docRoot()?.querySelector(`mark[data-hl="${item.dataset.id}"]`);
  if (!target) return;
  target.scrollIntoView({ block: 'center', behavior: 'smooth' });
  docRoot().querySelectorAll(`mark[data-hl="${item.dataset.id}"]`).forEach(m => {
    m.classList.remove('flash'); void m.offsetWidth; m.classList.add('flash');
  });
  setTimeout(() => openHighlightPop(item.dataset.id), 450);
});

/* notes panel */
function buildNotesPanel() {
  const panel = $('#notesPanel');
  if (!panel) return;
  panel.innerHTML = `<div class="notes-head">
      <b>Notes</b><span class="saved-state" id="savedState"></span>
      <button class="btn small ghost" id="copyNotes" title="Copy highlights and notes as Markdown">Copy</button>
      <button class="btn small ghost" id="closeNotes" title="Hide notes">×</button>
    </div>
    <div class="notes-scroll">
      <div class="notes-label">Summary
        <span class="sum-actions">
          <button class="btn small ghost" id="draftSummary" title="Pick the article's key sentences, offline">Draft key points</button>
          <button class="btn small ghost" id="gptSummary" title="Summarize the whole article in ChatGPT">✦ ChatGPT</button>
        </span></div>
      <textarea id="sumNotes" class="input notes-free notes-sum" placeholder="Draft key points, or paste ChatGPT's summary here. Saved with the article."></textarea>
      <label class="notes-label">Your notes<textarea id="freeNotes" class="input notes-free" placeholder="Anything worth remembering about this article…"></textarea></label>
      <div class="notes-label">Highlights <span id="hlCount"></span></div>
      <div id="hlList" class="hl-list"></div>
      <p class="hint">Select text to highlight it (H), add a note (N), or summarize it in ChatGPT (S).
        <button class="linkish" id="editPrompt">Edit the summarize prompt</button></p>
    </div>`;
  $('#freeNotes').value = R.notes.notes || '';
  $('#freeNotes').oninput = e => { R.notes.notes = e.target.value; queueSave(); };
  $('#sumNotes').value = R.notes.summary || '';
  $('#sumNotes').oninput = e => { R.notes.summary = e.target.value; queueSave(); };
  $('#draftSummary').onclick = () => {
    const doc = docRoot();
    if (!doc) return;
    const points = keyPoints(doc.innerText);
    if (!points.length) return toast('Not enough text to summarize');
    const draft = points.map(p => `• ${p}`).join('\n');
    const box = $('#sumNotes');
    box.value = box.value.trim() ? `${box.value.trim()}\n\n${draft}` : draft;
    R.notes.summary = box.value; queueSave();
    toast(`Drafted ${points.length} key points`);
  };
  $('#gptSummary').onclick = () => { const doc = docRoot(); if (doc) summarize(doc.innerText); };
  $('#closeNotes').onclick = () => toggleNotes(false);
  $('#copyNotes').onclick = copyNotesMarkdown;
  $('#editPrompt').onclick = editSummaryPrompt;
  $('#readerNotes').classList.toggle('on', !panel.hidden);
  renderHighlightList();
}

function renderHighlightList() {
  const list = $('#hlList');
  if (!list) return;
  const items = [...R.notes.highlights].sort((x, y) => x.start - y.start);
  $('#hlCount').textContent = items.length ? `· ${items.length}` : '';
  list.innerHTML = items.length ? items.map(h => `<div class="hl-item ${h._orphan ? 'orphan' : ''}" data-id="${esc(h.id)}"
      style="--hl-color:${HL_COLORS[h.color] || HL_COLORS.yellow}">
      <q>${esc(h.quote)}</q>
      ${h.note?.trim() ? `<div class="hl-note">${esc(h.note)}</div>` : ''}
      ${h._orphan ? '<div class="hl-note">This passage isn\'t in the current copy of the article.</div>' : ''}
    </div>`).join('') : '<p class="hint">No highlights yet.</p>';
  const b = $('#readerNotes');
  b.textContent = items.length ? `✎ Notes · ${items.length}` : '✎ Notes';
}

function toggleNotes(open) {
  const panel = $('#notesPanel');
  if (!panel) return;
  open = open ?? panel.hidden;
  panel.hidden = !open;
  $('#readerNotes').classList.toggle('on', open);
  local.set('notesOpen', open);
}

function copyNotesMarkdown() {
  const a = R.article;
  if (!a) return;
  const items = [...R.notes.highlights].filter(h => !h._orphan).sort((x, y) => x.start - y.start);
  const md = [`# ${a.title}`, a.url, '',
    ...(R.notes.summary?.trim() ? ['## Summary', R.notes.summary.trim(), ''] : []),
    ...(R.notes.notes?.trim() ? ['## Notes', R.notes.notes.trim(), ''] : []),
    ...items.flatMap(h => [`> ${h.quote.replace(/\s*\n+\s*/g, ' ')}`, ...(h.note?.trim() ? ['', h.note.trim()] : []), ''])].join('\n');
  navigator.clipboard.writeText(md).then(() => toast('Notes copied as Markdown'), () => toast('Could not copy'));
}

function setSaved(text) { const el = $('#savedState'); if (el) el.textContent = text; }

function queueSave() {
  if (!R.article) return;
  R.dirty = true;
  setSaved('Saving…');
  clearTimeout(R.saveTimer);
  R.saveTimer = setTimeout(saveNotes, 700);
}

/* Flush on the way out. An ordinary fetch is cancelled when the page goes away, and iOS discards a
   backgrounded tab without ever firing beforeunload — so notes typed inside the save debounce were
   simply lost. keepalive lets the request outlive the page; it is capped at 64KB, and a body over
   that is sent normally as a best effort. */
function saveNotesNow() {
  const a = R.article;
  if (!a || !R.dirty) return;
  R.dirty = false;
  const raw = JSON.stringify({ notes: R.notes.notes || '', highlights: R.notes.highlights.map(({ _orphan, ...h }) => h) });
  try {
    fetch(`/api/articles/${a.id}/notes`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: raw, keepalive: raw.length < 60000,
    });
  } catch { /* the page is going away; there is nothing left to tell the user */ }
}

async function saveNotes() {
  clearTimeout(R.saveTimer);
  const a = R.article;
  if (!a || !R.dirty) return;
  R.dirty = false;
  const body = { notes: R.notes.notes || '', summary: R.notes.summary || '', highlights: R.notes.highlights.map(({ _orphan, ...h }) => h) };
  try {
    const r = await api(`/api/articles/${a.id}/notes`, { method: 'PUT', body });
    a.notes_count = r.notes_count;
    const listed = S.articles.find(x => x.id === a.id);
    if (listed) listed.notes_count = r.notes_count;
    if (R.article === a) setSaved('Saved');
  } catch (err) {
    if (R.article === a) { R.dirty = true; setSaved('Not saved'); }
    toast(`Couldn't save notes: ${err.message}`);
  }
}
// visibilitychange is the one a phone reliably fires (switching apps, locking the screen)
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') saveNotesNow(); });
window.addEventListener('pagehide', saveNotesNow);
window.addEventListener('beforeunload', saveNotesNow);

/* ------------------------------------------------------------ dialogs */
function dialog({ title, body, submit = 'Save', danger = false }) {
  return new Promise(resolve => {
    const dlg = $('#dlg'), form = $('#dlgForm');
    form.innerHTML = `<h3>${esc(title)}</h3>${body}
      <div class="row"><button class="btn ghost" value="cancel" formnovalidate>Cancel</button>
      <button class="btn ${danger ? 'danger' : 'primary'}" value="ok">${esc(submit)}</button></div>`;
    // Read the answer when the form is submitted, not on the dialog's close event: some browsers
    // deliver "close" late (or not at all while the window isn't painting), which silently lost Save.
    let done = false;
    const finish = value => { if (!done) { done = true; resolve(value); } };
    form.onsubmit = e => {
      const ok = e.submitter?.value === 'ok';
      finish(ok ? Object.fromEntries(new FormData(form)) : null);
    };
    dlg.onclose = () => finish(dlg.returnValue === 'ok' ? Object.fromEntries(new FormData(form)) : null);
    dlg.oncancel = () => finish(null);
    dlg.returnValue = '';
    dlg.showModal();
    form.querySelector('input, select')?.focus();
  });
}

async function newTopic(parent) {
  const p = topic(parent);
  const v = await dialog({
    title: parent === 'custom' ? 'New topic' : `New subtopic in ${p.name}`,
    body: `<label>Name<input class="input" name="name" required maxlength="50" placeholder="e.g. Rust Security"></label>
      <label>Medium tags <input class="input" name="tags" placeholder="rust, memory-safety"></label>
      <p class="hint">Comma-separated tag slugs from medium.com/tag/… used by Discover. Leave blank to use the name.</p>`,
    submit: 'Create',
  });
  if (!v) return;
  try {
    const r = await api('/api/topics', { method: 'POST', body: { name: v.name, parent, tags: v.tags.split(',') } });
    S.open.add(r.topic); local.set('open', [...S.open]);
    await load();
    select(r.topic, r.subtopic.id);
    toast(`Created ${r.subtopic.name}`);
  } catch (err) { toast(err.message); }
}

async function editTags(t, s) {
  const v = await dialog({
    title: `Tags for ${s.name}`,
    body: `<label>Medium tags<input class="input" name="tags" value="${esc(s.tags.join(', '))}"></label>
      <p class="hint">Comma-separated. Find tags at medium.com/tag/&lt;tag&gt;.</p>`,
  });
  if (!v) return;
  try {
    await api(`/api/topics/${t}/${s.id}`, { method: 'PATCH', body: { name: s.name, tags: v.tags.split(',') } });
    delete S.discover[key(t, s.id)];
    await load();
    if (S.tab === 'discover') loadDiscover();
  } catch (err) { toast(err.message); }
}

async function deleteSub(t, s) {
  const n = S.articles.filter(a => a.topic === t && a.subtopic === s.id).length;
  const ok = await dialog({
    title: `Delete “${s.name}”?`,
    body: `<p style="margin:0;color:var(--muted)">${n ? `Its ${n} article${n > 1 ? 's are' : ' is'} removed from the library; downloaded PDFs stay on disk in Medium-Library/${esc(t)}/${esc(s.id)}.` : 'This topic has no articles.'}</p>`,
    submit: 'Delete', danger: true,
  });
  if (!ok) return;
  try { await api(`/api/topics/${t}/${s.id}`, { method: 'DELETE' }); await load(); select(t, null); }
  catch (err) { toast(err.message); }
}

async function moveArticle(a) {
  const v = await dialog({
    title: 'Move article',
    body: `<label>Move to<select class="input" name="dest">${destOptions(key(a.topic, a.subtopic))}</select></label>`,
    submit: 'Move',
  });
  if (!v) return;
  const [t, s] = v.dest.split('/');
  try {
    const na = await api(`/api/articles/${a.id}`, { method: 'PATCH', body: { topic: t, subtopic: s } });
    S.articles[S.articles.findIndex(x => x.id === a.id)] = na;
    render(); toast('Moved');
  } catch (err) { toast(err.message); }
}

async function removeArticle(a) {
  if (a.pdf_url || a.doc_url || a.notes_count) {
    const ok = await dialog({
      title: 'Remove article?',
      body: `<p style="margin:0;color:var(--muted)">“${esc(a.title)}” is removed from your library${a.pdf_url ? ', and its saved copy is deleted' : ''}${a.notes_count ? ' along with your highlights and notes' : ''}.</p>`,
      submit: 'Remove', danger: true,
    });
    if (ok) deleteNow(a);
    return;
  }
  const index = S.articles.indexOf(a);
  const drop = () => { S.articles = S.articles.filter(x => x !== a); renderTree(); renderHead(); renderList(); };
  const card = $(`#list .card[data-id="${CSS.escape(a.id)}"]`);
  if (card) { card.classList.add('leaving'); setTimeout(drop, 180); } else drop();
  const timer = setTimeout(() => { S.pendingRemovals.delete(a.id); deleteNow(a); }, 5000);
  S.pendingRemovals.set(a.id, timer);
  toast('Removed from library', 5000, {
    label: 'Undo',
    run: () => {
      clearTimeout(timer);
      S.pendingRemovals.delete(a.id);
      setTimeout(() => {
        if (!S.articles.includes(a)) S.articles.splice(Math.min(index, S.articles.length), 0, a);
        render();
      }, 200);
    },
  });
}

async function deleteNow(a) {
  try {
    await api(`/api/articles/${a.id}`, { method: 'DELETE' });
    S.articles = S.articles.filter(x => x.id !== a.id);
    render();
  } catch (err) {
    if (!S.articles.includes(a)) { S.articles.unshift(a); render(); }
    toast(err.message, 4000);
  }
}
window.addEventListener('pagehide', () => {  // finish removals that were still waiting for Undo
  for (const [id, timer] of S.pendingRemovals) {
    clearTimeout(timer);
    fetch(`/api/articles/${id}`, { method: 'DELETE', keepalive: true });
  }
});

load().catch(err => { $('#list').innerHTML = `<div class="empty"><b>Can't reach the server</b>${esc(err.message)}</div>`; });
