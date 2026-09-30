/* Кэш оболочки, чтобы приложение открывалось мгновенно и без сети.
   Данные НЕ кэшируются: еда за сегодня из вчерашнего кэша хуже, чем ошибка. */

const VERSION = 'v1';
const CACHE = `shell-${VERSION}`;

// Пути относительные: приложение может жить и на /app/, и на /food/app/.
const SHELL = [
  './',
  './index.html',
  './styles.css',
  './app.js',
  './api.js',
  './photo.js',
  './render.js',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Область видимости воркера — каталог приложения, но перехватывает он все
// запросы страниц из этой области, включая /day и /recognize. Поэтому всё,
// что не лежит внутри scope, пропускаем в сеть нетронутым.
const scope = new URL(self.registration.scope);

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const inScope = url.origin === scope.origin && url.pathname.startsWith(scope.pathname);

  if (event.request.method !== 'GET' || !inScope) return;

  event.respondWith(
    caches.match(event.request).then(
      (hit) =>
        hit ||
        fetch(event.request).then((resp) => {
          // Кладём в кэш только успешные ответы своего происхождения:
          // подсунуть в оболочку 404 или ответ редиректа — хуже, чем не кэшировать.
          if (resp.ok && resp.type === 'basic') {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(event.request, copy));
          }
          return resp;
        }),
    ),
  );
});
