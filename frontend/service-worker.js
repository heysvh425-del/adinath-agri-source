// Bump this whenever app shell behavior changes so installed clients replace
// stale cached JavaScript and HTML assets on the next service worker update.
const VERSION = 'adinath-agri-v27-ca-accounting';
const SHELL_CACHE = `${VERSION}-shell`;
const IMAGE_CACHE = `${VERSION}-images`;
const APP_SHELL = ['/', '/static/css/style.css?v=premium-green-6', '/static/css/premium.css?v=ca-accounting-1', '/static/css/print.css?v=gst-billing-3', '/static/js/vendor/lucide-1.74.0.min.js', '/static/js/app.js?v=ca-accounting-1', '/static/images/dashboard-field.webp', '/manifest.webmanifest', '/favicon.ico', '/icons/adinath-agri-logo.svg', '/icons/icon-192.png', '/icons/icon-512.png', '/icons/icon-512-maskable.png', '/icons/apple-touch-icon.png', '/offline.html'];
const API_PREFIXES = ['/api', '/auth', '/dashboard', '/products', '/farmers', '/invoices', '/outstanding', '/reports', '/notifications', '/accounting', '/business-settings'];

self.addEventListener('install', event => event.waitUntil((async () => {
  const cache = await caches.open(SHELL_CACHE);
  await cache.addAll(APP_SHELL);
  await self.skipWaiting();
})()));
self.addEventListener('activate', event => event.waitUntil((async () => {
  const keys = await caches.keys();
  await Promise.all(keys.filter(key => key.startsWith('adinath-agri-') && ![SHELL_CACHE, IMAGE_CACHE].includes(key)).map(key => caches.delete(key)));
  await self.clients.claim();
})()));
self.addEventListener('message', event => { if (event.data?.type === 'SKIP_WAITING') self.skipWaiting(); });

function isApiRequest(url) { return API_PREFIXES.some(prefix => url.pathname === prefix || url.pathname.startsWith(`${prefix}/`)); }
async function trimImageCache() { const cache = await caches.open(IMAGE_CACHE); const keys = await cache.keys(); await Promise.all(keys.slice(0, Math.max(0, keys.length - 60)).map(key => cache.delete(key))); }
async function cacheFirstAsset(request, url) { const cache = await caches.open(SHELL_CACHE); const normalized = new Request(url.origin + url.pathname); const cached = await cache.match(request) || await cache.match(normalized); if (cached) return cached; const response = await fetch(request); if (response.ok) cache.put(normalized, response.clone()); return response; }
async function imageStrategy(request) { const cache = await caches.open(IMAGE_CACHE); const cached = await cache.match(request) || await caches.match(request); if (cached) return cached; try { const response = await fetch(request); if (response.ok) { cache.put(request, response.clone()); trimImageCache(); } return response; } catch (error) { return caches.match('/icons/icon-192.png'); } }

self.addEventListener('fetch', event => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || isApiRequest(url)) return;
  if (request.mode === 'navigate') { event.respondWith(fetch(request).then(response => { caches.open(SHELL_CACHE).then(cache => cache.put('/', response.clone())); return response; }).catch(async () => (await caches.match('/') || await caches.match('/offline.html')))); return; }
  if (request.destination === 'image') { event.respondWith(imageStrategy(request)); return; }
  if (url.pathname.startsWith('/static/') || ['/manifest.webmanifest', '/favicon.ico'].includes(url.pathname)) event.respondWith(cacheFirstAsset(request, url));
});
