/* Состояние и обработчики. Разметка — в render.js, сжатие снимка — в photo.js. */

import * as api from './api.js';
import { compress } from './photo.js';
import { renderDay, renderDraft } from './render.js';

const $ = (id) => document.getElementById(id);

const screens = {
  today: $('screen-today'),
  add: $('screen-add'),
  edit: $('screen-edit'),
  settings: $('screen-settings'),
};

/** Всё изменяемое состояние приложения — здесь, а не разбросано по DOM. */
const state = {
  photo: null, // base64 снимка, ждущего распознавания
  draft: null, // черновик с экрана правки
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

// Сессия могла умереть в любой момент: истёк срок, отозвали с другого
// устройства, браузер выбросил куку. Без этого пользователь оставался на
// рабочем экране с сообщением об ошибке и без способа войти заново.
api.setUnauthorizedHandler(() => {
  state.draft = null;
  showApp(false);
  say($('login-error'), 'Сессия истекла — войди заново', 'error');
});

function say(node, text, kind = 'hint') {
  node.textContent = text;
  node.className = kind;
  node.hidden = !text;
}

/* --- экран дня -------------------------------------------------------- */

function paintDay(day) {
  const body = $('today-body');
  body.replaceChildren(renderDay(day, { onDelete: removeLog }));
  body.className = '';
}

async function removeLog(logId) {
  try {
    // Ответ уже содержит пересчитанный день — второй запрос не нужен.
    paintDay((await api.deleteLog(logId)).day);
  } catch (err) {
    // 404 значит, что запись уже убрали с другого устройства: перерисовать
    // день правильнее, чем показывать ошибку про то, чего и так нет.
    if (err.status === 404) loadDay();
    else say($('today-body'), `Не удалось удалить: ${err.message}`, 'placeholder');
  }
}

async function loadDay() {
  try {
    paintDay(await api.day());
  } catch (err) {
    say($('today-body'), `Не удалось загрузить день: ${err.message}`, 'placeholder');
  }
}

/* --- добавление ------------------------------------------------------- */

function resetAdd() {
  state.photo = null;
  $('add-text').value = '';
  say($('photo-note'), '');
  say($('add-status'), '');
  say($('add-error'), '');
}

async function takePhoto(input) {
  const file = input.files?.[0];
  // input очищается сразу: иначе повторный выбор того же файла не даст события
  input.value = '';
  if (!file) return;

  say($('add-error'), '');
  say($('photo-note'), 'Сжимаю снимок…');
  try {
    state.photo = await compress(file);
    const kb = Math.round((state.photo.length * 3) / 4 / 1024);
    say($('photo-note'), `Снимок готов, ${kb} КБ. Можно добавить описание и распознать.`);
  } catch (err) {
    state.photo = null;
    say($('photo-note'), '');
    say($('add-error'), err.message, 'error');
  }
}

async function recognize() {
  const text = $('add-text').value.trim();
  if (!text && !state.photo) {
    say($('add-error'), 'Сначала сними еду или опиши её словами', 'error');
    return;
  }

  const button = $('btn-recognize');
  button.disabled = true;
  say($('add-error'), '');
  // Pi + туннель + модель дают 5-20 с; без явного индикатора это выглядит зависанием.
  say($('add-status'), 'Распознаю… это занимает 5-20 секунд');

  try {
    const body = await api.recognize({
      ...(text ? { text } : {}),
      ...(state.photo ? { image_b64: state.photo } : {}),
    });
    state.draft = body.draft;
    say($('add-status'), '');
    openDraft();
  } catch (err) {
    say($('add-status'), '');
    // 429 — это «подожди», а не «сломалось»: сообщение приходит с сервера
    // уже человеческим, показываем как есть.
    say($('add-error'), err.message, 'error');
  } finally {
    button.disabled = false;
  }
}

/* --- правка ----------------------------------------------------------- */

function openDraft() {
  const root = $('edit-body');
  root.replaceChildren(
    renderDraft(state.draft, {
      onChange: (opts = {}) => {
        if (opts.rerender) openDraft();
        validateDraft();
      },
    }),
  );
  validateDraft();
  show('edit');
}

/** Что мешает сохранить. Пустая строка — можно. */
function draftProblem() {
  const draft = state.draft;
  if (!draft || !draft.items.length) return 'Не осталось ни одной позиции';

  // §2.4: справочник промолчал, и сохранять нули под видом данных нельзя.
  // Отметка снимается сама, как только в позицию вписали калории.
  const empty = draft.items.filter((i) => i.needs_manual && !(Number(i.calories_kcal) > 0));
  if (empty.length) {
    return `Справочник не нашёл: ${empty.map((i) => i.name).join(', ')}. `
      + 'Впиши калории и макросы вручную.';
  }
  for (const item of draft.items) {
    if (!String(item.name).trim()) return 'У позиции пустое название';
    if (!(Number(item.quantity) > 0)) return `Укажи количество для «${item.name}»`;
    if (!String(item.unit).trim()) return `Укажи единицу для «${item.name}»`;
    // POST /logs требует все четыре макроса числами; отрицательные он тоже
    // не примет. Ловим здесь, пока понятно, какая строка виновата.
    for (const key of ['calories_kcal', 'protein_g', 'fat_total_g', 'carbs_g']) {
      const value = Number(item[key]);
      if (!Number.isFinite(value) || value < 0) {
        return `Проверь числа в позиции «${item.name}»`;
      }
    }
  }
  return '';
}

function validateDraft() {
  const problem = draftProblem();
  say($('edit-warning'), problem, 'error');
  $('btn-save').disabled = Boolean(problem);
}

async function save() {
  const problem = draftProblem();
  if (problem) return;

  const button = $('btn-save');
  button.disabled = true;
  try {
    const body = await api.saveLog(state.draft);
    // §2.5: ответ уже содержит пересчитанный день — второй запрос не нужен.
    paintDay(body.day);
    state.draft = null;
    resetAdd();
    show('today');
  } catch (err) {
    say($('edit-warning'), err.message, 'error');
    button.disabled = false;
  }
}

/* --- вход ------------------------------------------------------------- */

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  say($('login-error'), '');
  button.disabled = true;
  try {
    await api.login($('login-key').value);
    $('login-key').value = '';
    showApp(true);
    show('today');
    loadDay();
  } catch (err) {
    say($('login-error'), err.status === 401 ? 'Неверный ключ' : err.message, 'error');
  } finally {
    button.disabled = false;
  }
});

$('logout').addEventListener('click', async () => {
  try {
    await api.logout();
  } finally {
    // Даже если запрос не дошёл, показывать данные дальше неправильно:
    // пользователь попросил выйти.
    state.draft = null;
    resetAdd();
    showApp(false);
  }
});

/* --- связывание ------------------------------------------------------- */

for (const tab of $('tabs').children) {
  tab.addEventListener('click', () => {
    show(tab.dataset.screen);
    if (tab.dataset.screen === 'today') loadDay();
  });
}

$('edit-back').addEventListener('click', () => show('add'));
$('btn-camera').addEventListener('click', () => $('pick-camera').click());
$('btn-library').addEventListener('click', () => $('pick-library').click());
$('pick-camera').addEventListener('change', (e) => takePhoto(e.target));
$('pick-library').addEventListener('change', (e) => takePhoto(e.target));
$('btn-recognize').addEventListener('click', recognize);
$('btn-save').addEventListener('click', save);

/* --- старт ------------------------------------------------------------ */

async function boot() {
  try {
    await api.authStatus();
    showApp(true);
    show('today');
    loadDay();
  } catch (err) {
    if (err.status !== 401) {
      // Сервер недоступен — просить ключ бессмысленно, он не поможет.
      say($('login-error'), err.message, 'error');
    }
    showApp(false);
  }
}

if ('serviceWorker' in navigator) {
  // Не блокирует запуск: без воркера приложение работает, просто открывается медленнее.
  navigator.serviceWorker.register('./sw.js').catch(() => {});
}

boot();
