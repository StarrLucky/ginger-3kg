/* Разметка экранов. Отдельно от app.js, чтобы логику можно было читать,
   не продираясь через строки, а разметку — не через обработчики.

   Всё пользовательское вставляется через textContent, а не innerHTML:
   название еды приходит от модели, то есть в конечном счёте от того, что
   пользователь сфотографировал и написал. */

const MACROS = [
  ['calories_kcal', 'ккал', 0],
  ['protein_g', 'белки', 1],
  ['fat_total_g', 'жиры', 1],
  ['carbs_g', 'углеводы', 1],
];

const MEAL_NAMES = {
  breakfast: 'Завтрак',
  lunch: 'Обед',
  dinner: 'Ужин',
  snack: 'Перекус',
};

export const mealName = (key) => MEAL_NAMES[key] || key;

const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const num = (value, digits = 1) =>
  Number(value ?? 0).toFixed(digits).replace(/\.0$/, '');

/* --- экран дня -------------------------------------------------------- */

export function renderDay(day, { onDelete = () => {} } = {}) {
  const root = el('div', 'day');

  // Итоги — всегда. Раньше они подменялись полосами прогресса, и при цели,
  // заданной не по всем макросам, соответствующие цифры пропадали с экрана
  // совсем. Крайний случай: заданный один лишь weight_kg даёт progress: {} —
  // пустой объект, в JS истинный, — и день оставался вообще без чисел.
  root.append(renderTotals(day.totals));
  if (day.progress && Object.keys(day.progress).length) {
    root.append(renderProgress(day));
  }

  if (!day.meals.length) {
    root.append(el('p', 'placeholder', 'За сегодня ничего не записано.'));
    return root;
  }

  for (const meal of day.meals) root.append(renderMeal(meal, onDelete));
  return root;
}

function renderTotals(totals) {
  const box = el('div', 'totals');
  for (const [key, label, digits] of MACROS) {
    const cell = el('div', 'total');
    cell.append(el('b', null, num(totals[key], digits)), el('span', null, label));
    box.append(cell);
  }
  return box;
}

function renderProgress(day) {
  const box = el('div', 'progress');
  for (const [key, label, digits] of MACROS) {
    const p = day.progress[key];
    if (!p) continue;

    const row = el('div', 'progress-row');
    const head = el('div', 'progress-head');
    head.append(
      el('span', 'progress-label', label),
      el('span', 'progress-nums', `${num(p.consumed, digits)} / ${num(p.target, digits)}`),
    );

    const track = el('div', 'track');
    const fill = el('div', 'fill');
    // Полоса упирается в 100%, но перебор виден цветом: обрезать число молча —
    // значит скрыть ровно тот факт, ради которого на экран и смотрят.
    fill.style.width = `${Math.min(100, p.used_percent)}%`;
    if (p.used_percent > 100) fill.classList.add('over');
    track.append(fill);

    row.append(head, track);
    box.append(row);
  }

  if (day.remaining_with_activity !== undefined) {
    const burned = day.activity?.burned_kcal || 0;
    const note = burned
      ? `Осталось ${num(day.remaining_with_activity, 0)} ккал с учётом ${num(burned, 0)} потраченных`
      : `Осталось ${num(day.remaining_with_activity, 0)} ккал`;
    box.append(el('p', 'hint', note));
  }
  return box;
}

function renderMeal(meal, onDelete) {
  const box = el('section', 'meal');
  const head = el('div', 'meal-head');
  head.append(
    el('h2', null, mealName(meal.meal_type)),
    el('span', 'meal-kcal', `${num(meal.calories_kcal, 0)} ккал`),
  );
  box.append(head);

  for (const group of groupByLog(meal.items)) {
    box.append(renderLogGroup(group, onDelete));
  }
  return box;
}

/** Позиции одной записи — вместе.

    DELETE /logs/{id} убирает запись целиком, а один приём пищи может
    состоять из нескольких записей (обед записали дважды). Без группировки
    кнопка удаления стояла бы у позиции, а уносила бы соседние — то есть
    показанное не совпадало бы с удаляемым. */
function groupByLog(items) {
  const byLog = new Map();
  for (const item of items) {
    if (!byLog.has(item.log_id)) byLog.set(item.log_id, []);
    byLog.get(item.log_id).push(item);
  }
  return [...byLog.entries()].map(([logId, group]) => ({ logId, items: group }));
}

function renderLogGroup(group, onDelete) {
  const box = el('div', 'log-group');
  box.dataset.logId = group.logId;

  for (const item of group.items) {
    const row = el('div', 'item');
    const main = el('div', 'item-main');
    main.append(
      el('span', 'item-name', item.name),
      el('span', 'item-qty', `${num(item.quantity)} ${item.unit}`),
    );
    row.append(main, el('span', 'item-kcal', `${num(item.calories_kcal, 0)} ккал`));
    box.append(row);
  }

  const actions = el('div', 'log-actions');

  // Удаление необратимо, поэтому в два шага. Не confirm(): в приложении с
  // экрана «Домой» системный диалог показывает домен и выглядит чужеродно.
  const idle = () => {
    const button = el('button', 'ghost', 'Убрать');
    button.type = 'button';
    button.addEventListener('click', () => actions.replaceChildren(...armed()));
    return [button];
  };

  const armed = () => {
    const what =
      group.items.length > 1 ? `Удалить все ${group.items.length} позиции?` : 'Удалить?';
    const label = el('span', 'confirm-label', what);

    const yes = el('button', 'danger', 'Да');
    yes.type = 'button';
    yes.addEventListener('click', () => onDelete(group.logId));

    const no = el('button', 'ghost', 'Отмена');
    no.type = 'button';
    no.addEventListener('click', () => actions.replaceChildren(...idle()));

    return [label, no, yes];
  };

  actions.replaceChildren(...idle());
  box.append(actions);
  return box;
}

/* --- экран правки ----------------------------------------------------- */

export function renderDraft(draft, { onChange }) {
  const root = el('div', 'draft');

  const meta = el('div', 'pad draft-meta');
  const mealSelect = el('select');
  for (const [value, label] of Object.entries(MEAL_NAMES)) {
    const option = el('option', null, label);
    option.value = value;
    if (value === draft.meal_type) option.selected = true;
    mealSelect.append(option);
  }
  mealSelect.addEventListener('change', () => {
    draft.meal_type = mealSelect.value;
  });
  const label = el('label', 'field');
  label.append(el('span', null, 'Приём пищи'), mealSelect);
  meta.append(label);
  root.append(meta);

  draft.items.forEach((item, index) => {
    root.append(renderDraftItem(item, index, draft, onChange));
  });
  return root;
}

function renderDraftItem(item, index, draft, onChange) {
  const box = el('div', 'draft-item');

  // Подсветка держится, только пока чисел нет: иначе она превращается в
  // украшение, которое пользователь научается игнорировать.
  const unresolved = () => item.needs_manual && !(Number(item.calories_kcal) > 0);
  const repaint = () => box.classList.toggle('needs-manual', unresolved());
  repaint();

  const name = el('input');
  name.value = item.name;
  name.setAttribute('aria-label', 'Название');
  name.addEventListener('input', () => {
    item.name = name.value;
    onChange();
  });

  const grams = el('input');
  grams.type = 'number';
  grams.inputMode = 'decimal';
  grams.min = '0';
  grams.step = '1';
  grams.value = item.quantity;
  grams.setAttribute('aria-label', 'Количество');
  grams.addEventListener('input', () => {
    item.quantity = grams.value === '' ? '' : Number(grams.value);
    onChange();
  });

  const unit = el('input');
  unit.value = item.unit;
  unit.setAttribute('aria-label', 'Единица');
  unit.addEventListener('input', () => {
    item.unit = unit.value;
    onChange();
  });

  const qtyRow = el('div', 'qty-row');
  qtyRow.append(grams, unit);

  const macros = el('div', 'macro-row');
  for (const [key, label, digits] of MACROS) {
    const field = el('label', 'macro');
    const input = el('input');
    input.type = 'number';
    input.inputMode = 'decimal';
    input.min = '0';
    input.step = digits ? '0.1' : '1';
    input.value = item[key] ?? 0;
    input.addEventListener('input', () => {
      // Пустое поле — это ноль, а не пустая строка: '' доезжала до POST /logs
      // и получала 422 на обязательном float, который пользователю нечем
      // истолковать.
      item[key] = Number(input.value) || 0;
      repaint();
      onChange();
    });
    field.append(el('span', null, label), input);
    macros.append(field);
  }

  const source = el('p', 'source', item.source_ref || '');
  if (item.needs_manual) {
    source.textContent = `${item.source_ref} — впиши числа вручную`;
  }

  const remove = el('button', 'ghost remove', 'Убрать');
  remove.type = 'button';
  remove.addEventListener('click', () => {
    draft.items.splice(index, 1);
    onChange({ rerender: true });
  });

  const head = el('div', 'draft-head');
  head.append(name, remove);
  box.append(head, qtyRow, macros, source);
  return box;
}
