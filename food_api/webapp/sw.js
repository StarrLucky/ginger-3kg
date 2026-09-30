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

  // stale-while-revalidate, а не cache-first.
  //
  // При cache-first обновление не доезжало бы до телефона никогда: браузер
  // переустанавливает воркер, только когда меняется сам sw.js, а VERSION
  // зашита в исходник, и ничто в Makefile или CI её не поднимает. Выкатил
  // исправление — устройство, которому оно нужнее всего, продолжает жить на
  // старой оболочке. Здесь кэш отдаётся мгновенно, а обновление подтягивается
  // в фоне и применяется со следующего открытия.
  event.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(event.request);
      const fresh = fetch(event.request)
        .then((resp) => {
          // Кладём только успешные ответы своего происхождения: подсунуть в
          // оболочку 404 или ответ редиректа — хуже, чем не кэшировать.
          if (resp.ok && resp.type === 'basic') cache.put(event.request, resp.clone());
          return resp;
        })
        .catch(() => null);

      if (cached) {
        event.waitUntil(fresh);
        return cached;
      }
      return (
        (await fresh) ||
        new Response('Нет связи и нет копии в кэше', { status: 504, statusText: 'Offline' })
      );
    }),
  );
});
