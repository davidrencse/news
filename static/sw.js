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

async function networkFirst(req, cacheName) {
  try {
    const res = await fetch(req);
    if (res.ok) (await caches.open(cacheName)).put(req, res.clone());
    return res;
  } catch (err) {
    const hit = await caches.match(req);
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(req, cacheName) {
  const hit = await caches.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res.ok) (await caches.open(cacheName)).put(req, res.clone());
  return res;
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // saving notes must always reach the server
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // article images hosted on Medium: leave alone

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
    // the server stamps ?v=<mtime> on its own assets, so a cached copy is only ever the right one
    e.respondWith(cacheFirst(req, SHELL));
  }
});
