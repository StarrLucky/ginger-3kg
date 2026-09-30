/* Каркас: вход, переключение экранов, регистрация service worker.
   Сами экраны (Сегодня / Добавить / Правка) — следующим шагом. */

import * as api from './api.js';

const $ = (id) => document.getElementById(id);

const screens = {
  today: $('screen-today'),
  add: $('screen-add'),
  edit: $('screen-edit'),
  settings: $('screen-settings'),
};

export function show(name) {
  for (const [key, el] of Object.entries(screens)) el.hidden = key !== name;
  for (const tab of $('tabs').children) {
    tab.classList.toggle('active', tab.dataset.screen === name);
  }
}

function showApp(signedIn) {
  $('screen-login').hidden = signedIn;
  $('app').hidden = !signedIn;
}

// --- вход -------------------------------------------------------------

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  const error = $('login-error');
  error.hidden = true;
  button.disabled = true;
  try {
    await api.login($('login-key').value);
    $('login-key').value = '';
    showApp(true);
    show('today');
  } catch (err) {
    error.textContent = err.status === 401 ? 'Неверный ключ' : err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});

$('logout').addEventListener('click', async () => {
  try {
    await api.logout();
  } finally {
    // Даже если запрос не дошёл, на этом устройстве показывать данные дальше
    // неправильно: пользователь попросил выйти.
    showApp(false);
  }
});

for (const tab of $('tabs').children) {
  tab.addEventListener('click', () => show(tab.dataset.screen));
}

$('edit-back').addEventListener('click', () => show('add'));

// --- старт ------------------------------------------------------------

async function boot() {
  try {
    await api.authStatus();
    showApp(true);
    show('today');
  } catch (err) {
    if (err.status !== 401) {
      // Сервер недоступен — просить ключ бессмысленно, он не поможет.
      $('login-error').textContent = err.message;
      $('login-error').hidden = false;
    }
    showApp(false);
  }
}

if ('serviceWorker' in navigator) {
  // Регистрация не блокирует запуск: без воркера приложение работает,
  // просто открывается медленнее.
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}

boot();
