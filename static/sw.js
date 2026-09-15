/* Service worker: makes the app open and read offline once it has been installed to a phone.

   - the interface (HTML, CSS, JS, KaTeX, highlight.js) is precached, so the app starts with no server
   - article files under /files/ are cached the first time they are read, so a story you have opened
     stays readable on the train
   - /api/library is cached after each successful load, so the library list still renders offline
   - every other /api/ call goes to the network only: searching, downloading and curating need it

   Served from / (see app.py) so its scope covers the whole app, not just /static/. */

const VERSION = 'v1';
const SHELL = `shell-${VERSION}`;
const FILES = `files-${VERSION}`;
const DATA = `data-${VERSION}`;

// app.py stamps its own assets with ?v=<mtime>, so what the page asks for never matches these bare
// URLs exactly. They are the offline fallback: see staticAsset() below.
const SHELL_URLS = [
  '/',
  '/static/style.css',
  '/static/article.css',
  '/static/app.js',
  '/static/article.js',
  '/static/manifest.webmanifest',
  '/static/apple-touch-icon.png',
  '/static/icon-192.png',
  '/static/vendor/katex/katex.min.css',
  '/static/vendor/katex/katex.min.js',
  '/static/vendor/katex/auto-render.min.js',
  '/static/vendor/hljs/highlight.min.js',
];

self.addEventListener('install', e => {
  // addAll fails the whole install if any one URL 404s, so each is added on its own
  e.waitUntil(caches.open(SHELL)
    .then(c => Promise.all(SHELL_URLS.map(u => c.add(u).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  const keep = new Set([SHELL, FILES, DATA]);
  e.waitUntil(caches.keys()
    .then(names => Promise.all(names.filter(n => !keep.has(n)).map(n => caches.delete(n))))
    .then(() => self.clients.claim()));
});

/* A partial response (206, from the browser's PDF viewer asking for a byte range) is a slice of a
   file, not the file. Storing one and handing it back later as the whole thing corrupts the PDF. */
const storable = res => res && res.status === 200 && res.type !== 'opaque';

async function put(cacheName, req, res) {
  if (storable(res)) (await caches.open(cacheName)).put(req, res.clone());
  return res;
}

async function networkFirst(req, cacheName) {
  try {
    return await put(cacheName, req, await fetch(req));
  } catch (err) {
    const hit = await caches.match(req);
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(req, cacheName) {
  const hit = await caches.match(req);
  if (hit) return hit;
  return put(cacheName, req, await fetch(req));
}

/* /static/… carries ?v=<mtime>, so a changed file is a different URL and can never be served stale.
   The flip side is that after an update the new URL is not in the cache: offline, fall back to any
   version of the same path. Older stamps of a file are dropped once the new one is stored. */
async function staticAsset(req) {
  const hit = await caches.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (storable(res)) {
      const cache = await caches.open(SHELL);
      await cache.put(req, res.clone());
      const path = new URL(req.url).pathname;
      for (const old of await cache.keys()) {
        if (new URL(old.url).pathname === path && old.url !== req.url) await cache.delete(old);
      }
    }
    return res;
  } catch (err) {
    const any = await caches.match(req, { ignoreSearch: true });
    if (any) return any;
    throw err;
  }
}

/* The PDF viewer asks for byte ranges. Those are never cached (see storable), so offline the best
   we can do is hand back the whole file — which a client asking for a range must accept. */
async function rangeRequest(req) {
  try {
    return await fetch(req);
  } catch (err) {
    const whole = await caches.match(new Request(req.url, { headers: {} }));
    if (whole) return whole;
    throw err;
  }
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // saving notes must always reach the server
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // article images hosted on Medium: leave alone

  if (req.headers.has('range')) {
    e.respondWith(rangeRequest(req));
    return;
  }
  if (req.mode === 'navigate') {
    e.respondWith(networkFirst(req, SHELL).catch(() => caches.match('/')));
    return;
  }
  if (url.pathname === '/api/library') {
    e.respondWith(networkFirst(req, DATA));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;           // search, jobs, index status: live only
  if (url.pathname.startsWith('/files/')) {
    e.respondWith(cacheFirst(req, FILES));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(staticAsset(req));
  }
});
