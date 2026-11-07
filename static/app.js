"use strict";

const tg = window.Telegram?.WebApp;
const screen = document.querySelector("#screen");
const nav = document.querySelector("#nav-band");
const notice = document.querySelector("#notice");
const tabs = [...document.querySelectorAll("[data-view]")];
const viewNames = {points: "Точки", dishes: "Блюда", hours: "Приём файлов", employees: "Сотрудники", usage: "Статистика"};
const permissionLabels = {
  point_addresses: "Редактировать адреса точек",
  invoice_exclusions: "Выбирать точки для печати накладных",
  dishes: "Переносить блюда между разделами",
  sections: "Создавать разделы плана",
  hours: "Менять часы приёма файлов"
};
const filters = {points: {query: "", invoice: ""}, dishes: {query: "", section: ""}};
const byName = new Intl.Collator("ru", {sensitivity: "base", numeric: true});
let state;
let view = "points";
let editing = false;
let busy = false;
let usageDays = "7";

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
  state = null;
  showNotice();
  editing = false;
  setNavigation(false);
  const panel = el("section", "access");
  panel.append(el("h1", "", title), el("p", "", message));
  if (retry) panel.append(button("Попробовать снова", "button", () => load()));
  screen.replaceChildren(panel);
  screen.setAttribute("aria-busy", "false");
}

function can(key) {
  return state?.owner === true || state?.permissions?.includes(key) === true;
}

function canOpen(name) {
  if (name === "points") return can("point_addresses") || can("invoice_exclusions");
  if (name === "dishes") return can("dishes") || can("sections");
  if (name === "employees" || name === "usage") return state?.owner === true;
  return name === "hours" && can("hours");
}

function viewTitle(name) {
  return name === "dishes" && !can("dishes") ? "Разделы" : viewNames[name];
}

function setNavigation(visible) {
  nav.hidden = !visible;
  for (const tab of tabs) {
    tab.hidden = !canOpen(tab.dataset.view);
    tab.textContent = viewTitle(tab.dataset.view);
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
    setAuthView("Нет доступа к настройкам", "Попросите владельца назначить вам права в разделе «Сотрудники».", true);
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
    if (!canOpen(view)) view = tabs.find(tab => canOpen(tab.dataset.view))?.dataset.view;
    if (!view) {
      authError(new ApiError("", 403));
      return;
    }
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
  else if (view === "employees") employeesView();
  else if (view === "usage") usageView();
  else if (view === "dishes" && !can("dishes")) sectionsView();
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
  if (!isPoint && can("sections")) heading.append(button("Новый раздел", "button", sectionForm, "plus"));

  const controls = el("div", "filters");
  const searchLabel = el("label", "search");
  const search = el("input");
  search.type = "search";
  search.placeholder = !isPoint ? "Поиск блюда" : can("point_addresses") ? "Поиск по названию или адресу" : "Поиск по названию точки";
  search.setAttribute("aria-label", search.placeholder);
  search.value = saved.query;
  searchLabel.append(icon("search"), search);
  const options = isPoint
    ? [["", "Все точки"], ["excluded", "Без накладной"], ["included", "С накладной"]]
    : [["", "Все разделы"], ...state.sections.map(section => [section, section])];
  const filterKey = isPoint ? "invoice" : "section";
  const filter = selectBox(isPoint ? "Печать накладных" : "Фильтр по разделу", options, saved[filterKey]);
  controls.append(searchLabel);
  if (!isPoint || can("invoice_exclusions")) controls.append(filter);
  else filter.value = "";
  const listing = el("div", "listing");
  const count = heading.querySelector(".count");
  count.setAttribute("aria-live", "polite");

  function draw() {
    saved.query = search.value;
    saved[filterKey] = filter.value;
    const query = search.value.trim().toLocaleLowerCase("ru");
    const matched = items.filter(item => {
      const text = (item.name + " " + (isPoint ? (item.address || "") : item.section)).toLocaleLowerCase("ru");
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
      text.append(el("span", "row-name", item.name));
      if (!isPoint || can("point_addresses")) text.append(el("span", "row-meta", isPoint ? (item.address || "Адрес не указан") : item.section));
      row.append(text);
      if (isPoint && can("invoice_exclusions") && item.excluded) row.append(el("span", "flag", "Без накладной"));
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
    page.append(button(viewTitle(view), "back", leaveForm, "back"));
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
    const values = payload();
    if (values === null) {
      leaveForm();
      showNotice("Изменений нет.");
      return;
    }
    busy = true;
    alert.hidden = true;
    const fields = [...form.elements, ...tabs];
    fields.forEach(item => { item.disabled = true; });
    save.textContent = "Сохраняем…";
    try {
      await api(endpoint, values);
      await load(success);
    } catch (error) {
      if (error.status === 403) {
        await load("Ваши права доступа изменились.");
        return;
      }
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
  if (can("point_addresses")) form.append(field("Адрес", address));
  if (can("invoice_exclusions")) form.append(check);
  submit(form, "point", () => {
    const changes = {};
    const previous = {};
    if (can("point_addresses") && point.address !== address.value.trim()) {
      changes.address = address.value.trim();
      previous.address = point.address;
    }
    if (can("invoice_exclusions") && point.excluded !== !print.checked) {
      changes.excluded = !print.checked;
      previous.excluded = point.excluded;
    }
    return Object.keys(changes).length ? {name: point.name, ...changes, previous} : null;
  }, "Настройки точки сохранены.");
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

function sectionsView() {
  const names = [...state.sections].sort(byName.compare);
  const heading = makeHeading("Разделы", names.length);
  heading.append(button("Новый раздел", "button", sectionForm, "plus"));
  const listing = el("ul", "listing sections");
  for (const name of names) listing.append(el("li", "row", name));
  if (!names.length) listing.append(el("li", "empty", "Разделов пока нет."));
  screen.replaceChildren(heading, listing);
}

function employeesView() {
  const employees = [...state.employees].sort((a, b) => byName.compare(a.name, b.name));
  const heading = makeHeading("Сотрудники", employees.length);
  heading.append(button("Добавить сотрудника", "button", () => employeeForm(), "plus"));
  const listing = el("div", "listing");
  for (const employee of employees) {
    const row = button("", "row", () => employeeForm(employee));
    const text = el("span", "row-text");
    text.append(el("span", "row-name", employee.name), el("span", "row-meta", "Telegram ID " + employee.id));
    row.append(text, icon("next"));
    listing.append(row);
  }
  if (!employees.length) listing.append(el("p", "empty", "Сотрудников пока нет."));
  screen.replaceChildren(heading, listing);
}

function employeeForm(employee = null) {
  const form = makeForm(employee ? employee.name : "Новый сотрудник", "", true);
  const id = el("input", "input");
  id.inputMode = "numeric";
  id.pattern = "[1-9][0-9]{0,15}";
  id.title = "Введите числовой Telegram ID сотрудника";
  id.maxLength = 16;
  id.required = true;
  id.value = employee?.id || "";
  id.readOnly = !!employee;
  const name = el("input", "input");
  name.maxLength = 100;
  name.required = true;
  name.value = employee?.name || "";
  name.addEventListener("input", () => name.setCustomValidity(name.value.trim() ? "" : "Введите имя сотрудника."));
  const idField = field("Telegram ID", id);
  if (!employee) {
    const hint = el("span", "assist", "Сотрудник может узнать свой ID командой /id в личном чате с ботом.");
    hint.id = "employee-id-help";
    id.setAttribute("aria-describedby", hint.id);
    idField.append(hint);
  }
  form.append(idField, field("Имя сотрудника", name));

  const grants = el("fieldset", "grants");
  grants.append(el("legend", "", "Права на редактирование"));
  for (const [key, title] of Object.entries(permissionLabels)) {
    const label = el("label", "grant");
    const toggle = el("input", "switch");
    toggle.type = "checkbox";
    toggle.name = "permission";
    toggle.value = key;
    toggle.setAttribute("role", "switch");
    toggle.checked = employee?.permissions.includes(key) || false;
    label.append(el("span", "", title), toggle);
    grants.append(label);
  }
  form.append(grants);
  const previous = employee ? {name: employee.name, permissions: employee.permissions} : null;
  submit(form, "employee", () => ({
    id: id.value, name: name.value.trim(),
    permissions: [...grants.querySelectorAll("input:checked")].map(toggle => toggle.value),
    previous
  }), employee ? "Права сотрудника сохранены." : "Сотрудник добавлен.", employee ? "Сохранить" : "Добавить сотрудника");
  if (employee) {
    form.querySelector(".form-actions").append(button("Удалить сотрудника", "button danger", () => {
      const confirmation = makeForm("Удалить сотрудника?", employee.name + " потеряет назначенные права на редактирование.", true);
      submit(confirmation, "employee/remove", () => ({id: employee.id, previous}), "Сотрудник удалён.", "Да, удалить");
      const actions = confirmation.querySelector(".form-actions");
      actions.firstElementChild.classList.add("destructive");
      actions.prepend(button("Отмена", "button", () => employeeForm(employee)));
    }));
  }
}

const numberRu = new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 1});
const usageDate = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "Asia/Almaty", day: "2-digit", month: "2-digit", year: "numeric",
  hour: "2-digit", minute: "2-digit", hourCycle: "h23"
});
const usagePeriods = [["1", "Сегодня"], ["7", "7 дней"], ["30", "30 дней"]];

function fileSize(bytes) {
  if (bytes === null) return "Неизвестен";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let index = 0;
  let value = bytes;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return numberRu.format(value) + " " + units[index];
}

function knownVolume(user, largest = false) {
  if (!user.files && largest) return "—";
  if (user.files && user.unknown_sizes === user.files) return "Неизвестен";
  return (user.unknown_sizes ? "от " : "") + fileSize(largest ? user.max_bytes : user.total_bytes);
}

function usageFailure(target, error, retry) {
  if (!target.isConnected || authError(error)) return;
  const message = el("p", "feedback", error.message);
  message.setAttribute("role", "alert");
  target.replaceChildren(message, button("Повторить", "button", retry));
}

function usageMetric(label, value) {
  const cell = el("span", "usage-metric");
  cell.append(el("span", "usage-label", label), el("strong", "", value));
  return cell;
}

async function usageView() {
  const heading = makeHeading("Статистика");
  const period = selectBox("Период статистики", usagePeriods, usageDays);
  heading.append(period);
  const content = el("div", "usage-content");
  const loading = el("p", "assist", "Загружаем статистику…");
  loading.setAttribute("role", "status");
  content.append(loading);
  screen.replaceChildren(heading, content);
  period.addEventListener("change", () => {
    usageDays = period.value;
    usageView();
    screen.querySelector("select").focus({preventScroll: true});
  });
  try {
    const data = await api("usage?days=" + usageDays);
    if (!content.isConnected) return;
    const users = [...data.users].sort((a, b) => b.total_bytes - a.total_bytes ||
      (b.files + b.resumes) - (a.files + a.resumes) || byName.compare(a.name, b.name));
    const date = data.since.split("-").reverse().join(".");
    const dates = el("p", "assist", "В запросы входят отправленные файлы и команды /resume. Период с " + date + " по времени Астаны. Учёт ведётся с " +
      usageDate.format(data.started_at * 1000) + ".");
    const listing = el("div", "listing usage-list");
    if (!users.some(user => user.files || user.resumes)) {
      listing.append(el("p", "empty", "За этот период файлы и команды /resume ещё не поступали."));
    }
    for (const user of users) {
      const row = button("", "row usage-person", () => usageDetail(user));
      const person = el("span", "row-text");
      person.append(el("span", "row-name", user.name), el("span", "row-meta", "ID " + user.id));
      row.append(person, usageMetric("Запросы", numberRu.format(user.files + user.resumes)),
        usageMetric("Объём", knownVolume(user)), usageMetric("Макс. файл", knownVolume(user, true)), icon("next"));
      listing.append(row);
    }
    content.replaceChildren(dates, listing);
  } catch (error) {
    usageFailure(content, error, usageView);
  }
}

function usageDetail(user) {
  editing = true;
  setNavigation(false);
  const page = el("section", "usage-detail");
  const back = button("Статистика", "back", leaveForm, "back");
  const period = usagePeriods.find(([days]) => days === usageDays)[1];
  const heading = makeHeading(user.name);
  page.append(back, heading, el("p", "context", "ID " + user.id + " · " + period + " · Время Астаны"));
  const metrics = el("div", "usage-totals");
  metrics.append(usageMetric("Файлы", numberRu.format(user.files)), usageMetric("Команды /resume", numberRu.format(user.resumes)),
    usageMetric("Объём файлов", knownVolume(user)), usageMetric("Макс. файл", knownVolume(user, true)));
  page.append(metrics);
  const counts = ["Файлов принято " + numberRu.format(user.accepted), "отклонено " + numberRu.format(user.rejected)];
  if (user.pending) counts.push("в обработке " + numberRu.format(user.pending));
  page.append(el("p", "assist", counts.join(", ") + ". Чеков " + numberRu.format(user.receipts) +
    ", товарных строк " + numberRu.format(user.lines) + "."));
  if (user.unknown_sizes) page.append(el("p", "assist", "Файлов без данных о размере — " + numberRu.format(user.unknown_sizes) + "."));
  const listing = el("ol", "listing usage-history");
  listing.hidden = true;
  const actions = el("div", "usage-actions");
  page.append(el("h2", "", "История запросов"), listing, actions);
  screen.replaceChildren(page);
  const title = heading.querySelector("h1");
  title.tabIndex = -1;
  title.focus({preventScroll: true});
  window.scrollTo(0, 0);

  async function loadPage(before = null) {
    const loading = el("p", "assist", "Загружаем запросы…");
    loading.setAttribute("role", "status");
    actions.replaceChildren(loading);
    const query = new URLSearchParams({days: usageDays, user_id: user.id});
    if (before !== null) query.set("before", before);
    try {
      const data = await api("usage?" + query);
      if (!page.isConnected) return;
      const fragment = document.createDocumentFragment();
      for (const entry of data.requests) {
        const row = el("li", "usage-entry");
        const file = entry.kind === "file";
        const status = {pending: "В обработке", accepted: "Принят", rejected: "Отклонён"}[entry.status];
        const name = file ? "Файл" : "/resume";
        row.append(el("strong", "", name + " · " + status), el("time", "assist", usageDate.format(entry.sent_at * 1000)));
        if (file) {
          const facts = [entry.size_bytes === null ? "Размер неизвестен" : fileSize(entry.size_bytes)];
          if (entry.receipts !== null) facts.push("Чеков " + numberRu.format(entry.receipts));
          if (entry.lines !== null) facts.push("Строк " + numberRu.format(entry.lines));
          row.append(el("span", "row-meta", facts.join(" · ")));
        }
        fragment.append(row);
      }
      listing.append(fragment);
      listing.hidden = !listing.childElementCount;
      actions.replaceChildren();
      if (listing.hidden) actions.append(el("p", "assist", "За этот период запросов нет."));
      if (data.next_before !== null) {
        actions.append(button("Показать ещё", "button", () => loadPage(data.next_before)));
      }
      if (before !== null && data.requests.length) {
        const firstNew = listing.children[listing.children.length - data.requests.length];
        firstNew.tabIndex = -1;
        firstNew.focus({preventScroll: true});
      }
    } catch (error) {
      usageFailure(actions, error, () => loadPage(before));
    }
  }
  loadPage();
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
    if (busy || !canOpen(tab.dataset.view)) return;
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
  setAuthView("Откройте приложение в Telegram", "Отправьте боту /admin в личном чате и нажмите «Настройки цеха».");
}
