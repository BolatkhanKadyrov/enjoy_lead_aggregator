"use strict";

const tg = window.Telegram?.WebApp;
const screen = document.querySelector("#screen");
const nav = document.querySelector("#nav-band");
const notice = document.querySelector("#notice");
const tabs = [...document.querySelectorAll("[data-view]")];
const viewNames = {points: "Точки", dishes: "Блюда", hours: "Приём файлов"};
const filters = {points: {query: "", invoice: ""}, dishes: {query: "", section: ""}};
const byName = new Intl.Collator("ru", {sensitivity: "base", numeric: true});
let state;
let view = "points";
let editing = false;
let busy = false;

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function icon(kind) {
  const paths = {search: "M16 16l5 5 M18 10.5a7.5 7.5 0 1 1-15 0a7.5 7.5 0 0 1 15 0", next: "m9 5 7 7-7 7", back: "m15 5-7 7 7 7", plus: "M12 4v16M4 12h16"};
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", paths[kind]);
  svg.append(path);
  return svg;
}

function button(label, style, fn, symbol) {
  const b = el("button", style, label);
  b.type = "button";
  if (symbol) b.prepend(icon(symbol));
  if (fn) b.addEventListener("click", fn);
  return b;
}

function showNotice(message = "") {
  notice.className = "notice";
  notice.textContent = message;
  notice.hidden = !message;
}

function setAuthView(title, message, retry) {
  editing = false;
  setNavigation(false);
  const panel = el("section", "access");
  panel.append(el("h1", "", title), el("p", "", message));
  if (retry) panel.append(button("Попробовать снова", "button", () => load()));
  screen.replaceChildren(panel);
  screen.setAttribute("aria-busy", "false");
}

function setNavigation(visible) {
  nav.hidden = !visible;
  for (const tab of tabs) {
    if (tab.dataset.view === view) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  if (tg?.isVersionAtLeast?.("6.1")) {
    if (editing) tg.BackButton.show();
    else tg.BackButton.hide();
  }
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(endpoint, values) {
  const headers = {Authorization: "tma " + tg.initData};
  const request = {method: values === undefined ? "GET" : "POST", headers, cache: "no-store"};
  if (values !== undefined) {
    headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(values);
  }
  let response;
  try {
    response = await fetch("/api/" + endpoint, request);
  } catch {
    throw new ApiError("Не удалось связаться с сервером. Проверьте соединение и попробуйте ещё раз.", 0);
  }
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) {
    throw new ApiError(data?.error || "Сервер временно недоступен. Попробуйте ещё раз.", response.status);
  }
  return data;
}

function authError(error) {
  if (error.status === 401) {
    setAuthView("Откройте приложение заново", "Вернитесь в личный чат с ботом и нажмите «Настройки цеха».");
    return true;
  }
  if (error.status === 403) {
    setAuthView("Доступ для администраторов", "Для этого аккаунта управление справочниками недоступно.");
    return true;
  }
  return false;
}

async function load(message = "") {
  busy = true;
  editing = false;
  setNavigation(false);
  showNotice(message);
  screen.setAttribute("aria-busy", "true");
  screen.replaceChildren(el("h1", "", "Загружаем справочники…"));
  try {
    state = await api("state");
    showView();
  } catch (err) {
    if (!authError(err)) setAuthView("Не удалось открыть справочники", err.message, true);
  } finally {
    busy = false;
    screen.setAttribute("aria-busy", "false");
  }
}

function makeHeading(title, count) {
  const bar = el("div", "heading");
  const heading = el("h1", "", title);
  if (count !== undefined) heading.append(el("span", "count", count));
  bar.append(heading);
  return bar;
}

function showView() {
  editing = false;
  setNavigation(true);
  if (view === "hours") hoursForm();
  else listView();
}

function selectBox(label, items, selected) {
  const select = el("select");
  select.setAttribute("aria-label", label);
  for (const [value, text] of items) {
    const option = el("option", "", text);
    option.value = value;
    select.append(option);
  }
  select.value = selected;
  return select;
}

function listView() {
  const isPoint = view === "points";
  const saved = filters[view];
  const items = [...state[view]].sort((a, b) => byName.compare(a.name, b.name));
  const heading = makeHeading(viewNames[view], items.length);
  if (!isPoint) heading.append(button("Новый раздел", "button", sectionForm, "plus"));

  const controls = el("div", "filters");
  const searchLabel = el("label", "search");
  const search = el("input");
  search.type = "search";
  search.placeholder = isPoint ? "Поиск по названию или адресу" : "Поиск блюда";
  search.setAttribute("aria-label", search.placeholder);
  search.value = saved.query;
  searchLabel.append(icon("search"), search);
  const options = isPoint
    ? [["", "Все точки"], ["excluded", "Без накладной"], ["included", "С накладной"]]
    : [["", "Все разделы"], ...state.sections.map(section => [section, section])];
  const filterKey = isPoint ? "invoice" : "section";
  const filter = selectBox(isPoint ? "Печать накладных" : "Фильтр по разделу", options, saved[filterKey]);
  controls.append(searchLabel, filter);
  const listing = el("div", "listing");
  const count = heading.querySelector(".count");
  count.setAttribute("aria-live", "polite");

  function draw() {
    saved.query = search.value;
    saved[filterKey] = filter.value;
    const query = search.value.trim().toLocaleLowerCase("ru");
    const matched = items.filter(item => {
      const text = (item.name + " " + (isPoint ? item.address : item.section)).toLocaleLowerCase("ru");
      const group = isPoint ? (item.excluded ? "excluded" : "included") : item.section;
      return text.includes(query) && (!filter.value || group === filter.value);
    });
    count.textContent = matched.length;
    listing.replaceChildren();
    if (!matched.length) {
      const message = items.length
        ? "Совпадений нет. Измените поиск или фильтр."
        : (isPoint ? "Точки появятся после загрузки заявок в бот." : "Блюда появятся после загрузки заявок в бот.");
      listing.append(el("p", "empty", message));
    }
    for (const item of matched) {
      const row = button("", "row", () => isPoint ? pointForm(item) : dishForm(item));
      const text = el("span", "row-text");
      text.append(el("span", "row-name", item.name), el("span", "row-meta", isPoint ? (item.address || "Адрес не указан") : item.section));
      row.append(text);
      if (isPoint && item.excluded) row.append(el("span", "flag", "Без накладной"));
      row.append(icon("next"));
      listing.append(row);
    }
  }
  search.addEventListener("input", draw);
  filter.addEventListener("change", draw);
  draw();
  screen.replaceChildren(heading, controls, listing);
}

function field(label, control) {
  const wrapper = el("label", "field", label);
  control.setAttribute("aria-label", label);
  wrapper.append(control);
  return wrapper;
}

function leaveForm() {
  if (busy) return;
  showNotice();
  showView();
  const heading = screen.querySelector("h1");
  heading.tabIndex = -1;
  heading.focus({preventScroll: true});
  window.scrollTo(0, 0);
}

function makeForm(title, context, isDetail) {
  editing = isDetail;
  setNavigation(!isDetail);
  const page = el("div", "form-area");
  if (isDetail) {
    showNotice();
    page.append(button(viewNames[view], "back", leaveForm, "back"));
  }
  page.append(makeHeading(title));
  if (context) page.append(el("p", "context", context));
  const form = el("form", "form-panel");
  page.append(form);
  screen.replaceChildren(page);
  if (isDetail) {
    const heading = page.querySelector("h1");
    heading.tabIndex = -1;
    heading.focus({preventScroll: true});
    window.scrollTo(0, 0);
  }
  return form;
}

function submit(form, endpoint, payload, success, label = "Сохранить") {
  const alert = el("div", "feedback");
  alert.setAttribute("role", "alert");
  alert.hidden = true;
  const controls = el("div", "form-actions");
  const save = button(label, "button primary");
  save.type = "submit";
  controls.append(save);
  form.append(alert, controls);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    busy = true;
    alert.hidden = true;
    const values = payload();
    const fields = [...form.elements, ...tabs];
    fields.forEach(item => { item.disabled = true; });
    save.textContent = "Сохраняем…";
    try {
      await api(endpoint, values);
      await load(success);
    } catch (error) {
      if (authError(error)) return;
      alert.textContent = error.message;
      alert.hidden = false;
      if (error.status === 409) {
        const reload = button("Загрузить актуальные данные", "button", () => load());
        controls.replaceChildren(reload);
      }
    } finally {
      busy = false;
      fields.forEach(item => { item.disabled = false; });
      save.textContent = label;
    }
  });
}

function pointForm(point) {
  const form = makeForm(point.name, "", true);
  const address = el("textarea");
  address.maxLength = 500;
  address.value = point.address;
  address.placeholder = "Адрес для накладной";
  const print = el("input");
  print.type = "checkbox";
  print.checked = !point.excluded;
  const check = el("label", "check");
  check.append(print, "Печатать накладную для этой точки");
  form.append(field("Адрес", address), check);
  submit(form, "point", () => ({
    name: point.name, address: address.value.trim(), excluded: !print.checked,
    previous: {address: point.address, excluded: point.excluded}
  }), "Настройки точки сохранены.");
}

function dishForm(dish) {
  const form = makeForm(dish.name, "", true);
  const section = selectBox("Раздел плана", state.sections.map(name => [name, name]), dish.section);
  form.append(field("Раздел плана производства", section));
  submit(form, "dish", () => ({
    name: dish.name, section: section.value, previous: {section: dish.section}
  }), "Раздел блюда сохранён.");
}

function sectionForm() {
  const form = makeForm("Новый раздел", "", true);
  const name = el("input", "input");
  name.maxLength = 100;
  name.required = true;
  name.addEventListener("input", () => name.setCustomValidity(name.value.trim() ? "" : "Введите название раздела."));
  form.append(field("Название раздела", name));
  submit(form, "section", () => ({name: name.value.trim()}), "Раздел сохранён.", "Сохранить раздел");
}

function hoursForm() {
  const form = makeForm("Приём файлов", "Каждый день по времени Астаны. Бот проверяет время отправки Excel в Telegram.", false);
  const times = el("div", "times");
  const start = el("input", "input");
  const end = el("input", "input");
  start.type = end.type = "time";
  start.required = end.required = true;
  start.value = state.settings.start;
  end.value = state.settings.end;
  times.append(field("С", start), field("До", end));
  form.append(times);
  const previous = {start: start.value, end: end.value};
  submit(form, "settings", () => ({start: start.value, end: end.value, previous}), "Часы приёма сохранены.");
}

for (const tab of tabs) {
  tab.addEventListener("click", () => {
    if (busy) return;
    view = tab.dataset.view;
    showNotice();
    showView();
  });
}

function syncTheme() {
  document.documentElement.dataset.theme = tg?.colorScheme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

syncTheme();
tg?.onEvent?.("themeChanged", syncTheme);
if (tg?.isVersionAtLeast?.("6.1")) tg.BackButton.onClick(leaveForm);
tg?.ready?.();
if (tg?.initData) {
  tg.expand?.();
  load();
} else {
  setAuthView("Откройте приложение в Telegram", "Отправьте боту /admin в личном чате и нажмите «Настройки цеха». Эта кнопка доступна назначенным администраторам.");
}
