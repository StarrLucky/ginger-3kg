/* Обёртки над API. Ключ здесь не хранится: авторизация — HttpOnly-кука,
   которую JS не видит и которую поэтому невозможно украсть при XSS. */

// Приложение отдаётся с /app/, а API живёт этажом выше. Через туннель префикс
// другой (/food/app/), поэтому база вычисляется, а не зашивается.
export const API_BASE = new URL('..', document.baseURI).href;

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

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

  if (!resp.ok) throw new ApiError(resp.status, data?.detail);
  return data;
}

export const login = (key) => request('auth/login', { method: 'POST', body: { key } });
export const logout = () => request('auth/logout', { method: 'POST' });
export const authStatus = () => request('auth/status');

export const day = (date) => request(date ? `day?date=${encodeURIComponent(date)}` : 'day');
export const saveLog = (draft) => request('logs', { method: 'POST', body: draft });
export const deleteLog = (logId) => request(`logs/${logId}`, { method: 'DELETE' });

// Pi + туннель + модель дают 5-20 с; двадцатисекундного таймаута тут мало.
export const recognize = (payload) =>
  request('recognize', { method: 'POST', body: payload, timeoutMs: 60000 });
