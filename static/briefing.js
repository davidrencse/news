/* On-demand briefing; only poll while its view is open. */
const briefingView = { job: null, days: 3, timer: null, requesting: false, error: '' };

function briefingDate(value) {
  return new Date(value).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

async function openBriefing() {
  drawer(false);
  closeReader();
  S.mode = 'briefing';
  S.search = null;
  $('#search').value = '';
  render();
  try {
    briefingView.job = await api('/api/briefing');
    briefingView.error = '';
    if (S.mode !== 'briefing') return;
    if (briefingView.job.state === 'idle') return generateBriefing();
    briefingView.days = briefingView.job.days || 3;
    if (S.mode === 'briefing') { renderBriefingHead(); renderBriefingList(); }
    if (briefingView.job.state === 'running') scheduleBriefingPoll();
  } catch (err) {
    briefingView.error = err.message;
    if (S.mode === 'briefing') { renderBriefingHead(); renderBriefingList(); }
  }
}

async function generateBriefing(force = false) {
  if (briefingView.requesting) return;
  briefingView.requesting = true;
  briefingView.error = '';
  renderBriefingHead();
  try {
    briefingView.job = await api('/api/briefing', { method: 'POST', body: { days: briefingView.days, force } });
    briefingView.days = briefingView.job.days;
    scheduleBriefingPoll();
  } catch (err) {
    briefingView.error = err.message;
  } finally {
    briefingView.requesting = false;
    if (S.mode === 'briefing') { renderBriefingHead(); renderBriefingList(); }
  }
}

async function saveBriefingPick(index, btn) {
  const item = briefingView.job?.articles?.[index];
  if (!item || btn.disabled) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const res = await api('/api/briefing/save', { method: 'POST', body: {
      url: item.resolved_url || item.url, title: item.title,
      source: item.source, published: item.published, snippet: item.reason } });
    btn.textContent = res.fetched ? 'Saved to library ✓' : 'Saved as link ✓';
    btn.classList.add('saved');
    toast(res.fetched ? 'Saved to your library under Morning Briefing.'
                      : 'Saved as a link; the full text could not be fetched.');
  } catch (err) {
    btn.disabled = false;
    btn.textContent = original;
    toast(err.message);
  }
}

function scheduleBriefingPoll() {
  clearTimeout(briefingView.timer);
  briefingView.timer = setTimeout(async () => {
    if (S.mode !== 'briefing') return;
    try {
      const previous = briefingView.job?.state;
      briefingView.job = await api('/api/briefing');
      briefingView.error = '';
      if (S.mode !== 'briefing') return;
      if (previous !== briefingView.job.state) renderBriefingHead();
      renderBriefingList();
      if (briefingView.job.state === 'running') scheduleBriefingPoll();
    } catch (err) {
      briefingView.error = err.message;
      if (S.mode === 'briefing') { renderBriefingHead(); renderBriefingList(); }
    }
  }, 1500);
}

function renderBriefingHead() {
  const busy = briefingView.requesting || briefingView.job?.state === 'running';
  $('#viewHead').innerHTML = `<div class="briefing-head">
    <div><h1>Morning briefing</h1><p>A wider look at the news. Five stories worth your time.</p></div>
    <form id="briefingForm" class="briefing-controls">
      <label for="briefingDays">Look back<select id="briefingDays" class="input" ${busy ? 'disabled' : ''}>
        ${[[1, '24 hours'], [3, '3 days'], [7, '7 days']].map(([n, label]) => `<option value="${n}" ${briefingView.days === n ? 'selected' : ''}>${label}</option>`).join('')}
      </select></label>
      <button class="btn primary" type="submit" ${busy ? 'disabled' : ''}>${busy ? 'Scanning news…' : 'Generate briefing'}</button>
    </form></div>`;
  $('#briefingDays').onchange = e => { briefingView.days = Number(e.target.value); };
  $('#briefingForm').onsubmit = e => { e.preventDefault(); generateBriefing(true); };
}

function briefingStory(item, rank, compact = false) {
  const link = item.resolved_url || item.url;
  const related = item.related ? `<span class="briefing-related">+${item.related} related ${item.related === 1 ? 'story' : 'stories'}</span>` : '';
  return `<article class="briefing-story ${compact ? 'briefing-story-compact' : ''}">
    <span class="briefing-rank" aria-label="Pick ${rank}">${String(rank).padStart(2, '0')}</span>
    <div><p class="briefing-byline">${esc(item.category)} <span aria-hidden="true">·</span> ${esc(item.source)}
      <span aria-hidden="true">·</span> <time datetime="${esc(item.published)}">${esc(briefingDate(item.published))}</time>${related}</p>
      <h2><a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(item.title)}</a></h2>
      ${compact ? '' : `<p class="briefing-reason">${esc(item.reason)}</p>`}
      <div class="briefing-actions">
        <a class="briefing-read" href="${esc(link)}" target="_blank" rel="noopener noreferrer">Read story<span class="sr-only"> (opens in a new tab)</span></a>
        ${compact ? '' : `<button class="btn small ghost" data-save="${rank - 1}">Save to library</button>`}
      </div>
    </div></article>`;
}

function renderBriefingList() {
  const job = briefingView.job;
  if (briefingView.error) {
    $('#list').innerHTML = `<div class="briefing-sheet"><div class="empty" role="alert"><b>Could not reach the briefing</b><p>${esc(briefingView.error)}</p><button class="btn ghost" id="briefingReconnect">Reconnect</button></div></div>`;
    $('#briefingReconnect').onclick = openBriefing;
    return;
  }
  if (!job || job.state === 'idle') {
    $('#list').innerHTML = '<div class="briefing-sheet"><p role="status">Preparing your news scan…</p></div>';
    return;
  }
  const windowText = `${briefingDate(job.window_start)} – ${briefingDate(job.window_end)}`;
  const coverage = `${job.topics_scanned} of ${job.topics_total} topics checked · ${job.topics_succeeded} feeds returned`;
  if (job.state === 'running') {
    $('#list').innerHTML = `<section class="briefing-sheet briefing-progress" aria-label="Briefing progress">
      <h2>Finding the stories that matter</h2><p>Scanning tech, business, science, and world news. Then narrowing the best 10 articles to five reading picks.</p>
      <progress max="${job.topics_total}" value="${job.topics_scanned}" aria-label="Topics checked"></progress>
      <p role="status">${coverage}</p><p class="briefing-window">${esc(windowText)} · You can browse your library while this runs.</p>
      ${job.topics_failed ? `<p>${job.topics_failed} feeds unavailable so far. Other topics are still being checked.</p>` : ''}
    </section>`;
    return;
  }
  const articles = job.articles || [];
  const topics = (job.topics || []).map(x => `<li>${esc(x)}</li>`).join('');
  $('#list').innerHTML = `<section class="briefing-sheet">
    <div class="briefing-edition"><p>${esc(windowText)} <span>Local time</span></p>
      <p>${coverage} · ${job.candidate_count} unique candidates · ${job.shortlist.length} shortlisted · ${articles.length} picks</p></div>
    ${job.state !== 'complete' || articles.length < 5 ? `<p class="briefing-notice" role="status">${esc(job.message)}</p>` : ''}
    ${job.topics_failed ? `<p class="briefing-notice">Partial coverage: ${job.topics_failed} topic feeds could not be fetched. Generate again to retry.</p>` : ''}
    <div class="briefing-picks">${articles.map((item, i) => briefingStory(item, i + 1)).join('')}</div>
    ${job.shortlist.length ? `<details class="briefing-details"><summary>See the top ${job.shortlist.length} shortlist</summary><div>${job.shortlist.map((item, i) => briefingStory(item, i + 1, true)).join('')}</div></details>` : ''}
    <details class="briefing-details"><summary>Topics and selection method</summary>
      <p>Google News RSS search, in English. Headlines are ranked by publication time, topic relevance, and feed position, then balanced across publishers and categories. Similar headlines are reduced. These are reading recommendations, not AI-written summaries or a fact-check of the articles.</p>
      <ul class="briefing-topics">${topics}</ul>
      ${job.errors?.length ? `<p>Unavailable topics: ${job.errors.map(e => esc(e.topic)).join(', ')}.</p>` : ''}
    </details>
    <p class="briefing-footnote">Links open to the publisher (resolved from Google News where possible). Publisher access restrictions may apply. The time window is fixed when you generate; generate again for a fresh edition.</p>
  </section>`;
  $('#list').querySelectorAll('[data-save]').forEach(btn => {
    btn.onclick = () => saveBriefingPick(Number(btn.dataset.save), btn);
  });
}
