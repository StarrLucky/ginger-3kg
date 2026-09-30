/* Проверки чистой логики фронтенда. Запускается из pytest: `node run.mjs`.
   Ненулевой код выхода = провал. */

import assert from 'node:assert/strict';
import { installDom } from './dom.mjs';

installDom();

const { renderDay, renderDraft } = await import('../render.js');
const apiModule = await import('../api.js');
const { ApiError, setUnauthorizedHandler } = apiModule;

let passed = 0;
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

const day = (extra = {}) => ({
  date: '2026-09-30',
  totals: { calories_kcal: 617, protein_g: 23, fat_total_g: 6, carbs_g: 125 },
  meals: [
    {
      meal_type: 'lunch',
      calories_kcal: 617,
      items: [{ log_id: 1, name: 'гречка', quantity: 180, unit: 'g', calories_kcal: 617 }],
    },
  ],
  activity: { burned_kcal: 0, items: [] },
  ...extra,
});

/* --- экран дня ------------------------------------------------------- */

test('итоги показаны, когда целей нет', () => {
  const root = renderDay(day());
  assert.ok(root.find('totals'), 'блок итогов отсутствует');
  assert.match(root.text, /617/);
});

test('пустой progress не съедает итоги', () => {
  // Задана одна лишь цель по весу -> сервер отдаёт progress: {}, а пустой
  // объект в JS истинный. Раньше день оставался вообще без чисел.
  const root = renderDay(day({ progress: {}, targets: { weight_kg: 80 } }));
  assert.ok(root.find('totals'), 'при пустом progress итоги обязаны остаться');
  assert.match(root.text, /617/);
});

test('частичная цель не прячет остальные макросы', () => {
  const root = renderDay(
    day({ progress: { protein_g: { consumed: 23, target: 150, used_percent: 15.3 } } }),
  );
  assert.ok(root.find('totals'), 'итоги нужны рядом с полосой');
  assert.ok(root.find('progress'), 'полоса по заданной цели нужна тоже');
  assert.match(root.text, /125/, 'углеводы должны остаться на экране');
});

test('перебор цели виден цветом, а полоса не вылезает', () => {
  const root = renderDay(
    day({ progress: { calories_kcal: { consumed: 3000, target: 2000, used_percent: 150 } } }),
  );
  const fill = root.find('fill');
  assert.equal(fill.style.width, '100%', 'полоса не должна вылезать за трек');
  assert.ok(fill.classList.contains('over'), 'перебор обязан быть отмечен');
});

/* --- удаление записи -------------------------------------------------- */

/** День, где один обед состоит из двух отдельных записей. */
const twoLogs = () => ({
  date: '2026-09-30',
  totals: { calories_kcal: 800, protein_g: 30, fat_total_g: 10, carbs_g: 140 },
  meals: [
    {
      meal_type: 'lunch',
      calories_kcal: 800,
      items: [
        { log_id: 1, name: 'гречка', quantity: 180, unit: 'g', calories_kcal: 617 },
        { log_id: 1, name: 'масло', quantity: 10, unit: 'g', calories_kcal: 75 },
        { log_id: 2, name: 'яблоко', quantity: 150, unit: 'g', calories_kcal: 108 },
      ],
    },
  ],
  activity: { burned_kcal: 0, items: [] },
});

test('позиции одной записи сгруппированы вместе', () => {
  // DELETE убирает запись целиком; без группировки кнопка стояла бы у
  // позиции, а уносила бы соседние — показанное не совпало бы с удаляемым.
  const root = renderDay(twoLogs());
  const groups = root.findAll('log-group');
  assert.equal(groups.length, 2, 'две записи — две группы');
  assert.equal(groups[0].findAll('item').length, 2, 'гречка и масло вместе');
  assert.equal(groups[1].findAll('item').length, 1);
});

test('удаление подтверждается в два шага', () => {
  let deleted = null;
  const root = renderDay(twoLogs(), { onDelete: (id) => { deleted = id; } });
  const actions = root.findAll('log-group')[1].find('log-actions');

  actions.children[0].fire('click'); // «Убрать»
  assert.equal(deleted, null, 'первое нажатие удалять не должно');

  const yes = actions.children.find((c) => c.textContent === 'Да');
  yes.fire('click');
  assert.equal(deleted, 2, 'удалиться должна именно вторая запись');
});

test('отмена возвращает кнопку в исходное состояние', () => {
  let deleted = null;
  const root = renderDay(twoLogs(), { onDelete: (id) => { deleted = id; } });
  const actions = root.find('log-actions');

  actions.children[0].fire('click');
  actions.children.find((c) => c.textContent === 'Отмена').fire('click');

  assert.equal(deleted, null);
  assert.equal(actions.children.length, 1, 'должна остаться одна кнопка');
  assert.equal(actions.children[0].textContent, 'Убрать');
});

test('подтверждение предупреждает, что уйдут все позиции записи', () => {
  const root = renderDay(twoLogs());
  const actions = root.findAll('log-group')[0].find('log-actions');
  actions.children[0].fire('click');
  assert.match(actions.find('confirm-label').textContent, /все 2 позиции/);
});

test('у записи из одной позиции текст короткий', () => {
  const root = renderDay(twoLogs());
  const actions = root.findAll('log-group')[1].find('log-actions');
  actions.children[0].fire('click');
  assert.equal(actions.find('confirm-label').textContent, 'Удалить?');
});

/* --- экран правки ---------------------------------------------------- */

const draft = (overrides = {}) => ({
  meal_type: 'lunch',
  items: [
    {
      name: 'гречка',
      quantity: 180,
      unit: 'g',
      calories_kcal: 617,
      protein_g: 23,
      fat_total_g: 6,
      carbs_g: 125,
      source_ref: 'USDA 170286',
      needs_manual: false,
      ...overrides,
    },
  ],
});

test('очистка поля макроса даёт ноль, а не пустую строку', () => {
  // '' доезжала до POST /logs и получала 422 на обязательном float.
  const d = draft();
  const root = renderDraft(d, { onChange: () => {} });
  const input = root.findAll('macro')[1].children[1]; // белки
  input.value = '';
  input.fire('input');
  assert.equal(d.items[0].protein_g, 0);
  assert.equal(typeof d.items[0].protein_g, 'number');
});

test('подсветка needs_manual гаснет, когда вписали калории', () => {
  const d = draft({ needs_manual: true, calories_kcal: 0, source_ref: 'manual: не найдено' });
  const root = renderDraft(d, { onChange: () => {} });
  const item = root.find('draft-item');
  assert.ok(item.classList.contains('needs-manual'), 'пустая позиция должна быть подсвечена');

  const kcal = root.findAll('macro')[0].children[1];
  kcal.value = '250';
  kcal.fire('input');
  assert.ok(!item.classList.contains('needs-manual'), 'после ввода подсветка должна гаснуть');
});

test('название вставляется текстом, а не разметкой', () => {
  const d = draft({ name: '<img src=x onerror=alert(1)>' });
  const root = renderDraft(d, { onChange: () => {} });
  const name = root.find('draft-head').children[0];
  assert.equal(name.value, '<img src=x onerror=alert(1)>');
  assert.equal(name.children.length, 0, 'разметка не должна разбираться в узлы');
});

/* --- ошибки API ------------------------------------------------------ */

test('detail строкой показывается как есть', () => {
  assert.equal(new ApiError(401, 'Invalid API key').message, 'Invalid API key');
});

test('detail списком не превращается в [object Object]', () => {
  // FastAPI отдаёт 422 списком объектов; раньше это и видел пользователь.
  const err = new ApiError(422, [
    { type: 'float_parsing', loc: ['body', 'items', 0, 'protein_g'], msg: 'Input should be a valid number' },
  ]);
  assert.ok(!err.message.includes('[object'), err.message);
  assert.match(err.message, /protein_g/);
  assert.match(err.message, /valid number/);
});

test('без detail остаётся код', () => {
  assert.equal(new ApiError(502, undefined).message, 'HTTP 502');
});

/* --- потеря сессии --------------------------------------------------- */

/** Подменить fetch на ответ с заданным кодом. Возвращает список путей. */
function stubFetch(status, body = {}) {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    };
  };
  return calls;
}

async function expectThrows(fn) {
  try {
    await fn();
  } catch (err) {
    return err;
  }
  throw new Error('ожидалась ошибка, её не было');
}

test.async = [];
const testAsync = (name, fn) => test.async.push([name, fn]);

testAsync('401 на рабочем запросе возвращает на экран входа', async () => {
  // Без этого пользователь оставался на рабочем экране с текстом ошибки и
  // без формы входа: сессия истекла, а выйти и войти заново нечем.
  let called = 0;
  setUnauthorizedHandler(() => {
    called += 1;
  });
  stubFetch(401, { detail: 'Session expired or revoked' });

  await expectThrows(() => apiModule.day());
  assert.equal(called, 1, 'обработчик потери сессии не вызван');
});

testAsync('401 на самом входе обработчик не дёргает', async () => {
  // Для /auth/login и /auth/status 401 — штатный ответ, а не потеря сессии.
  let called = 0;
  setUnauthorizedHandler(() => {
    called += 1;
  });
  stubFetch(401, { detail: 'Invalid API key' });

  await expectThrows(() => apiModule.login('wrong'));
  await expectThrows(() => apiModule.authStatus());
  assert.equal(called, 0, 'экран входа не должен перерисовываться сам себя');
});

testAsync('успешный ответ обработчик не трогает', async () => {
  let called = 0;
  setUnauthorizedHandler(() => {
    called += 1;
  });
  stubFetch(200, { totals: {} });

  await apiModule.day();
  assert.equal(called, 0);
});

/* --- работа за префиксом (§2.6) --------------------------------------- */

/** Перечитать api.js с другим baseURI: модули кэшируются, поэтому ?v=. */
async function apiWithBase(baseURI, tag) {
  installDom(baseURI);
  return import(`../api.js?v=${tag}`);
}

testAsync('база API берётся этажом выше приложения', async () => {
  const mod = await apiWithBase('http://pi.local/app/', 'plain');
  assert.equal(mod.API_BASE, 'http://pi.local/');
});

testAsync('за туннелем с префиксом база сохраняет префикс', async () => {
  // Публичный доступ идёт как https://<домен>/food/* -> food-api:8000,
  // то есть приложение отдаётся с /food/app/, а API живёт на /food/.
  // Зашитый '/' здесь бил бы мимо роутера.
  const mod = await apiWithBase('https://example.org/food/app/', 'prefixed');
  assert.equal(mod.API_BASE, 'https://example.org/food/');
});

testAsync('запросы уходят по вычисленной базе, а не от корня', async () => {
  const mod = await apiWithBase('https://example.org/food/app/', 'calls');
  const calls = stubFetch(200, {});
  mod.setUnauthorizedHandler(() => {});
  await mod.day();
  assert.equal(calls[0], 'https://example.org/food/day');
});

/* --- прогон ---------------------------------------------------------- */

let failed = 0;
for (const [name, fn] of [...tests, ...test.async]) {
  try {
    await fn();
    passed += 1;
  } catch (err) {
    failed += 1;
    console.error(`FAIL  ${name}\n      ${err.message}`);
  }
}
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
