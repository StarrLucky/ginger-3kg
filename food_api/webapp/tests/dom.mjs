/* Минимальный DOM, чтобы проверять чистую логику разметки под Node.

   Не эмуляция браузера и не претендует на неё: вёрстка, камера и service
   worker так не проверяются. Цель узкая — поймать ветвления в render.js,
   которые иначе видны только на телефоне. */

class ClassList {
  constructor(node) {
    this.node = node;
    this.set = new Set();
  }
  add(...names) { for (const n of names) this.set.add(n); }
  toggle(name, on) { (on ? this.set.add(name) : this.set.delete(name)); }
  contains(name) { return this.set.has(name); }
}

class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.textContent = '';
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.classList = new ClassList(this);
    this.listeners = {};
  }
  set className(value) {
    this.classList.set = new Set(String(value).split(/\s+/).filter(Boolean));
  }
  get className() { return [...this.classList.set].join(' '); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  fire(type) { for (const fn of this.listeners[type] || []) fn({ target: this }); }

  /** Весь видимый текст поддерева — то, что пользователь реально прочтёт. */
  get text() {
    return [this.textContent, ...this.children.map((c) => c.text)].join(' ').trim();
  }
  find(className) {
    if (this.classList.contains(className)) return this;
    for (const child of this.children) {
      const hit = child.find(className);
      if (hit) return hit;
    }
    return null;
  }
  findAll(className, out = []) {
    if (this.classList.contains(className)) out.push(this);
    for (const child of this.children) child.findAll(className, out);
    return out;
  }
}

export function installDom(baseURI = 'http://localhost/app/') {
  globalThis.document = {
    baseURI,
    createElement: (tag) => new Node(tag),
  };
}
