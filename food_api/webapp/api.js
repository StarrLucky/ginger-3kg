/* Обёртки над API. Ключ здесь не хранится: авторизация — HttpOnly-кука,
   которую JS не видит и которую поэтому невозможно украсть при XSS. */

// Приложение отдаётся с /app/, а API живёт этажом выше. Через туннель префикс
// другой (/food/app/), поэтому база вычисляется, а не зашивается.
export const API_BASE = new URL('..', document.baseURI).href;

/** FastAPI отдаёт detail строкой у HTTPException и СПИСКОМ объектов у 422.
    Без этого список превращался в «[object Object]» прямо на экране. */
function describe(detail, status) {
  if (typeof detail === 'string' && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    const first = detail[0];
    const field = Array.isArray(first?.loc) ? first.loc[first.loc.length - 1] : null;
    const msg = first?.msg || 'неверное значение';
    return field ? `Поле «${field}»: ${msg}` : msg;
  }
  return `HTTP ${status}`;
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(describe(detail, status));
    this.status = status;
    this.detail = detail;
  }
}

/* Что делать, когда сессия перестала приниматься. Ставится из app.js:
   иначе каждый вызывающий ловил бы 401 сам, и любой пропуск оставлял бы
   пользователя на мёртвом экране без формы входа. */
let onUnauthorized = () => {};
export const setUnauthorizedHandler = (fn) => {
  onUnauthorized = fn;
};

async function request(path, { method = 'GET', body, timeoutMs = 20000 } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let resp;
  try {
    resp = await fetch(new URL(path, API_BASE), {
      method,
      // Кука уходит только благодаря этому: fetch по умолчанию её не шлёт
      // для запросов, инициированных скриптом на некоторых конфигурациях.
      credentials: 'same-origin',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') throw new ApiError(0, 'Сервер не ответил вовремя');
    throw new ApiError(0, 'Нет связи с сервером');
  }
  clearTimeout(timer);

  if (resp.status === 204) return null;

  let data = null;
  try {
    data = await resp.json();
  } catch {
    // Тело не JSON — для 2xx это ошибка контракта, для ошибки просто нет деталей.
    if (resp.ok) throw new ApiError(resp.status, 'Сервер вернул не JSON');
  }

  if (!resp.ok) {
    // Вход и проверка статуса сами разбираются с 401 — для них это штатный ответ.
    if (resp.status === 401 && !path.startsWith('auth/')) onUnauthorized();
    throw new ApiError(resp.status, data?.detail);
  }
  return data;
}

export const login = (key) => request('auth/login', { method: 'POST', body: { key } });
export const logout = () => request('auth/logout', { method: 'POST' });
export const authStatus = () => request('auth/status');

export const day = (date) => request(date ? `day?date=${encodeURIComponent(date)}` : 'day');
export const saveLog = (draft) => request('logs', { method: 'POST', body: draft });

// Pi + туннель + модель дают 5-20 с; двадцатисекундного таймаута тут мало.
export const recognize = (payload) =>
  request('recognize', { method: 'POST', body: payload, timeoutMs: 60000 });
