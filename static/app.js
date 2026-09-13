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

const S = {
  topics: [],
  articles: [],
  sel: local.get('sel', { topic: null, sub: null }),
  tab: 'library',
  mode: 'browse',        // 'browse' | 'search'
  search: null,          // {q, dest, items, next, loading, error}
  open: new Set(local.get('open', ['cybersecurity', 'artificial-intelligence'])),
  discover: {},          // "topic/sub" -> {items, errors, tags}
  reader: null,          // article currently shown
  pollTimer: null,
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

function toast(msg, ms = 2600) {
  const el = $('#toast');
  el.textContent = msg; el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (el.hidden = true), ms);
}

async function load() {
  const data = await api('/api/library');
  S.topics = data.topics; S.articles = data.articles;
  if (S.sel.topic && !topic(S.sel.topic)) S.sel = { topic: null, sub: null };
  if (S.sel.sub && !sub(S.sel.topic, S.sel.sub)) S.sel.sub = null;
  render();
}

function select(t, s) {
  S.sel = { topic: t, sub: s };
  if (!s) S.tab = 'library';
  S.mode = 'browse'; S.search = null; $('#search').value = '';
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
    <span class="label">All articles</span><span class="count">${S.articles.length}</span></button>`;
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
  const t = topic(S.sel.topic), s = S.sel.sub && sub(S.sel.topic, S.sel.sub);
  const title = s ? s.name : t ? t.name : 'All articles';
  const crumb = ['Medium-Library', t?.id, s?.id].filter(Boolean).map(x => `<span>${esc(x)}</span>`).join('');
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
      <button class="tab ${S.tab === 'library' ? 'active' : ''}" data-tab="library">Library · ${visibleArticles().length}</button>
      <button class="tab ${S.tab === 'discover' ? 'active' : ''}" data-tab="discover" ${s ? '' : 'disabled title="Pick a subtopic to discover new articles"'}>Discover</button>
      ${S.tab === 'discover' && s ? '<span class="tab-note"><button class="linkish" id="refreshDiscover">refresh</button></span>' : ''}
    </div>`;
  $('#viewHead').querySelectorAll('.tab').forEach(b => b.onclick = () => {
    S.tab = b.dataset.tab; renderHead(); renderList();
    if (S.tab === 'discover') loadDiscover();
  });
  $('#editTags') && ($('#editTags').onclick = () => editTags(S.sel.topic, s));
  $('#delSub') && ($('#delSub').onclick = () => deleteSub(S.sel.topic, s));
  $('#refreshDiscover') && ($('#refreshDiscover').onclick = () => loadDiscover(true));
}

/* ------------------------------------------------------------ lists */
function visibleArticles() {
  return S.articles.filter(a => (!S.sel.topic || a.topic === S.sel.topic) && (!S.sel.sub || a.subtopic === S.sel.sub));
}

function libraryMatches(q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  return S.articles.filter(a => {
    const hay = `${a.title} ${a.author} ${a.snippet} ${a.topic} ${a.subtopic}`.toLowerCase();
    return words.every(w => hay.includes(w));
  });
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
        ${a.pdf_url ? '<span class="pill ready">PDF saved</span>' : '<span class="pill online">Not downloaded</span>'}
        ${showLoc && s ? `<span class="pill loc">${esc(t.name)} / ${esc(s.name)}</span>` : ''}
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
  if (S.tab === 'discover' && S.sel.sub) return renderDiscover();
  const items = visibleArticles();
  if (!items.length) {
    $('#list').innerHTML = `<div class="empty"><b>Nothing saved here yet</b>${S.sel.sub
      ? 'Open the <b style="display:inline;font:inherit">Discover</b> tab, search Medium above, or paste a link.'
      : 'Search all of Medium above, pick a subtopic and use Discover, or paste a Medium link.'}</div>`;
    return;
  }
  $('#list').innerHTML = items.map(a => libraryCard(a, !S.sel.sub)).join('');
}

$('#list').addEventListener('click', async e => {
  const card = e.target.closest('.card');
  if (!card) return;
  const act = e.target.closest('[data-act]')?.dataset.act;
  if (card.dataset.result !== undefined) return onResultClick(card, act);
  if (card.dataset.discover !== undefined) return onDiscoverClick(card, act);
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
  $('#list').innerHTML = '<div class="loading">Fetching the latest stories from Medium…</div>';
  try {
    S.discover[k] = await api(`/api/discover/${encodeURIComponent(S.sel.topic)}/${encodeURIComponent(S.sel.sub)}`);
  } catch (err) {
    S.discover[k] = { items: [], errors: [err.message], tags: [] };
  }
  if (key(S.sel.topic, S.sel.sub) === k && S.tab === 'discover' && S.mode === 'browse') renderList();
}

function renderDiscover() {
  const d = S.discover[key(S.sel.topic, S.sel.sub)];
  if (!d) { $('#list').innerHTML = '<div class="loading">Fetching the latest stories from Medium…</div>'; return; }
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
          <button class="btn small primary" data-act="read">Read as PDF</button>
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
  try {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    s.items = r.items; s.next = r.next; s.provider = r.provider; s.notice = r.notice;
    if (r.index) { S.index = r.index; renderIndex(); }
  } catch (err) { s.error = err.message; }
  s.loading = false;
  if (S.search === s) renderList();
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
  closeReader(); render();
}

function renderSearchHead() {
  const s = S.search;
  $('#viewHead').innerHTML = `
    <div class="crumb"><span>Search</span><span>all of Medium</span></div>
    <div class="view-title"><h1>“${esc(s.q)}”</h1></div>
    <div class="search-line">
      <span class="crumb">Nothing downloads until you open an article.</span>
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
  if (mine.length) h += `<div class="section-label">In your library · ${mine.length}</div>` + mine.map(a => libraryCard(a, true)).join('');
  const via = { index: 'from your local index', feeds: 'from live Medium tag feeds' }[s.provider] || '';
  h += `<div class="section-label">On Medium${s.items.length ? ` · ${s.items.length}` : ''}${via ? ` · ${via}` : ''}</div>`;
  if (s.notice) h += `<div class="warn">${esc(s.notice)}</div>`;
  if (s.error) h += `<div class="warn">${esc(s.error)}</div>`;
  h += s.items.map((it, i) => {
    const img = safeUrl(it.image);
    return `<article class="card ${img ? '' : 'noimg'}" data-result="${i}" tabindex="0">
    <div>
      <div class="card-meta"><span>${esc(it.author || sourceOf(it.url))}</span>${it.published ? `<span class="dot"> ${fmtDate(it.published)}</span>` : ''}</div>
      <h2 class="card-title">${esc(it.title)}</h2>
      ${it.snippet ? `<p class="card-snip">${esc(it.snippet)}</p>` : ''}
      <div class="card-foot">${saved.has(it.url)
        ? '<button class="btn small primary" data-act="read">Open</button><span class="pill ready">In library</span>'
        : '<button class="btn small primary" data-act="read">Read as PDF</button><button class="btn small" data-act="save">Save link</button>'}
      </div>
    </div>
    ${img ? `<img class="card-img" src="${esc(img)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ''}
  </article>`;
  }).join('');
  if (s.loading) h += '<div class="loading">Searching Medium…</div>';
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
  if (v !== S.libVersion && !$('#dlg').open) {  // the curator (or another tab) changed the library
    S.libVersion = v;
    const scroll = $('.view').scrollTop;
    await load();
    $('.view').scrollTop = scroll;
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
  const curLine = !cur.enabled ? 'auto-add is off'
    : curSub ? `finding trending ${curSub.name} posts…`
    : `${fmtCount(cur.auto_articles || 0)} curated · ${fmtCount(cur.added_total || 0)} added automatically`;
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
      <label class="check"><input type="checkbox" name="curator" ${st.curator?.enabled ? 'checked' : ''}> Keep adding trending articles automatically</label>
      <p class="hint">The curator visits one subtopic at a time, reads a few new posts, and adds the ones trending by claps.
        Once a subtopic has 12, better posts replace the weakest ones you haven't downloaded. It has added
        ${(st.curator?.added_total || 0).toLocaleString()} and swapped ${(st.curator?.rotated_total || 0).toLocaleString()} so far.
        ${(st.network?.['medium.com']?.pushbacks || 0) ? `Medium has pushed back ${st.network['medium.com'].pushbacks} times this session; requests are spaced ${st.network['medium.com'].gap_s}s apart.` : ''}</p>
      ${st.error ? `<div class="warn">${esc(st.error)}</div>` : ''}
      ${st.curator?.error ? `<div class="warn">Curator: ${esc(st.curator.error)}</div>` : ''}`,
  });
  if (!v) return;
  try {
    S.index = await api('/api/index', { method: 'POST', body: { days: +v.days, paused: v.paused === 'on', curator_enabled: v.curator === 'on' } });
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
const STAGES = ['link', 'freedium', 'download', 'pdf'];
const STAGE_INDEX = { queued: 1, freedium: 1, download: 2, pdf: 3, done: 4 };

function openReader(a, force = false) {
  S.reader = a;
  $('#reader').hidden = false;
  $('#readerOriginal').href = safeUrl(a.url);
  $('#readerRefetch').onclick = () => openReader(S.reader, true);
  updateReaderBar(a);
  if (a.pdf_url && !force) return showPdf(a);
  startFetch(a, force);
}

function updateReaderBar(a) {
  const t = topic(a.topic), s = sub(a.topic, a.subtopic);
  $('#readerTitle').textContent = a.title;
  $('#readerMeta').textContent = [a.author, t && s ? `${t.name} / ${s.name}` : ''].filter(Boolean).join(' · ');
  $('#readerOpen').hidden = !a.pdf_url;
  if (a.pdf_url) $('#readerOpen').href = a.pdf_url;
}

function showPdf(a) {
  $('#readerBody').innerHTML = `<iframe title="${esc(a.title)}" src="${esc(a.pdf_url)}#view=FitH"></iframe>`;
}

function renderPipeline(stage, job, error) {
  const idx = STAGE_INDEX[stage] ?? 1;
  const labels = ['Medium link', 'Freedium mirror', 'Download article', 'Save PDF'];
  $('#readerBody').innerHTML = `<div class="pipe">
    <h2>${error ? 'Could not fetch this article' : 'Preparing your PDF'}</h2>
    <p>${error ? '' : 'Routing the article through Freedium and printing a clean local copy.'}</p>
    <ol class="steps">${STAGES.map((s, i) => {
      const cls = i < idx ? 'done' : i === idx ? (error ? 'failed' : 'active') : '';
      return `<li class="${cls}">${labels[i]}</li>`;
    }).join('')}</ol>
    ${job?.freedium_url ? `<div class="via">${esc(job.freedium_url)}</div>` : ''}
    ${error ? `<div class="err">${esc(error)}</div><button class="btn primary" id="retry">Try again</button>` : ''}
  </div>`;
  if (error) $('#retry').onclick = () => startFetch(S.reader, true);
}

async function startFetch(a, force) {
  clearTimeout(S.pollTimer);
  renderPipeline('queued');
  let job;
  try {
    job = await api(`/api/articles/${a.id}/fetch${force ? '?force=true' : ''}`, { method: 'POST' });
  } catch (err) { return renderPipeline('queued', null, err.message); }
  const tick = async () => {
    if (S.reader?.id !== a.id) return;
    if (job.status === 'done') return finish(job.article);
    if (job.status === 'error') return renderPipeline(job.stage, job, job.error);
    renderPipeline(job.stage, job);
    S.pollTimer = setTimeout(async () => {
      try { job = await api(`/api/jobs/${job.id}`); } catch (err) { job = { ...job, status: 'error', error: err.message }; }
      tick();
    }, 700);
  };
  tick();
}

function finish(a) {
  const i = S.articles.findIndex(x => x.id === a.id);
  if (i >= 0) S.articles[i] = a;
  if (S.reader?.id !== a.id) return;
  S.reader = a;
  renderPipeline('done');
  setTimeout(() => { if (S.reader?.id === a.id) { updateReaderBar(a); showPdf(a); } }, 450);
  renderTree(); renderHead();
  if (S.tab === 'library' || S.mode === 'search') renderList();
}

function closeReader() {
  clearTimeout(S.pollTimer);
  S.reader = null;
  $('#reader').hidden = true;
  $('#readerBody').innerHTML = '';
}
$('#readerClose').onclick = () => { closeReader(); renderList(); };
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && S.reader && !$('#dlg').open) { closeReader(); renderList(); }
});

/* ------------------------------------------------------------ dialogs */
function dialog({ title, body, submit = 'Save', danger = false }) {
  return new Promise(resolve => {
    const dlg = $('#dlg'), form = $('#dlgForm');
    form.innerHTML = `<h3>${esc(title)}</h3>${body}
      <div class="row"><button class="btn ghost" value="cancel" formnovalidate>Cancel</button>
      <button class="btn ${danger ? 'danger' : 'primary'}" value="ok">${esc(submit)}</button></div>`;
    dlg.onclose = () => resolve(dlg.returnValue === 'ok' ? Object.fromEntries(new FormData(form)) : null);
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
  const ok = await dialog({
    title: 'Remove article?',
    body: `<p style="margin:0;color:var(--muted)">“${esc(a.title)}” is removed from your library${a.pdf_url ? ' and its PDF file is deleted' : ''}.</p>`,
    submit: 'Remove', danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/articles/${a.id}`, { method: 'DELETE' });
    S.articles = S.articles.filter(x => x.id !== a.id);
    render();
  } catch (err) { toast(err.message); }
}

load().catch(err => { $('#list').innerHTML = `<div class="empty"><b>Can't reach the server</b>${esc(err.message)}</div>`; });
