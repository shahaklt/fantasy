/* Gridiron service worker.
 *
 * One job: make the panel open instantly and survive a wifi hiccup, without
 * ever showing you a stale number. The shell (HTML, JS, CSS, icons) is cached
 * and refreshed on every successful load. Anything under /api or /ws is never
 * cached — a projection that quietly came from yesterday is worse than an
 * error, especially with a draft clock running.
 */
const VERSION = 'gridiron-shell-v1';
const SHELL = [
  '/',
  '/assets/styles.css',
  '/assets/app.js',
  '/assets/ui.js',
  '/assets/views1.js',
  '/assets/views2.js',
  '/assets/guide.js',
  '/assets/guide-data.js',
  '/assets/espn.js',
  '/assets/icon-192.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      // Individually, so one missing file cannot fail the whole install.
      .then((cache) => Promise.allSettled(SHELL.map((url) => cache.add(url))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

function isLive(url) {
  return url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/');
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || isLive(url)) return;   // straight to the network

  // Network-first: an updated app.js has to win over a cached one, or
  // update.sh would appear to do nothing on the phone.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok && response.type === 'basic') {
          const copy = response.clone();
          caches.open(VERSION).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request, { ignoreSearch: true })
        .then((hit) => hit || caches.match('/'))),
  );
});
