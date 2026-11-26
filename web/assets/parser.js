const form = document.getElementById("search-form");
const searchButton = document.getElementById("search-button");
const progress = document.getElementById("search-progress");
const summary = document.getElementById("results-summary");
const notice = document.getElementById("notice");
const sourceTag = document.getElementById("results-source");
const emptyState = document.getElementById("empty-state");
const emptyMessage = document.getElementById("empty-message");
const resultTable = document.getElementById("results-table");
const results = document.getElementById("result-rows");
const sourceNames = { "2gis": "2ГИС", yandex: "Яндекс Карты" };
const contactHostAliases = {
  "vk.com": "vk.ru",
  "vk.ru": "vk.ru",
  "wa.me": "api.whatsapp.com",
  "whatsapp.com": "api.whatsapp.com",
  "t.me": "t.me",
  "telegram.me": "t.me",
};

let searching = false;
let pendingMarks = 0;
let requested = 0;

for (const button of form.querySelectorAll("[data-all]")) {
  button.addEventListener("click", () => {
    const field = form.elements.namedItem(button.dataset.all);
    field.value = "все";
    field.focus();
  });
}

function showNotice(message, kind = "error") {
  notice.textContent = message;
  notice.dataset.kind = kind;
  notice.hidden = !message;
}

function updateControls() {
  searchButton.disabled = searching || pendingMarks > 0;
  searchButton.textContent = searching ? "Идёт поиск…" : "Найти компании";
  progress.hidden = !searching;
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
      ...options,
    });
  } catch (error) {
    throw new Error(error.name === "TimeoutError" ? "Парсер не ответил вовремя." : "Связь с парсером прервалась.");
  }
  const data = await response.json().catch(() => null);
  if (response.status === 401) throw new Error("Обновите страницу и войдите снова.");
  if (response.status === 422) throw new Error("Проверьте параметры поиска.");
  if (!response.ok || !data) {
    throw new Error(data?.detail?.message || "Запрос к парсеру не выполнен.");
  }
  return data;
}

async function waitForResult(jobId) {
  const deadline = performance.now() + 285000;
  while (performance.now() < deadline) {
    const result = await api(`/v1/search/${encodeURIComponent(jobId)}`);
    if (result.status !== "running") return result;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error("Время ожидания истекло. Запустите поиск снова.");
}

function updateTableVisibility(message) {
  const hasRows = results.childElementCount > 0;
  resultTable.hidden = !hasRows;
  emptyState.hidden = hasRows;
  emptyMessage.textContent = message;
}

function addCell(row, value, label) {
  const cell = row.insertCell();
  cell.textContent = value || "—";
  if (label) cell.dataset.label = label;
  return cell;
}

function contactUrl(value, phones) {
  if (phones) {
    const number = value.replace(/[^+0-9]/g, "");
    return /^\+?[0-9]{5,18}$/.test(number) ? `tel:${number}` : null;
  }
  try {
    const url = new URL(value);
    if (["http:", "https:"].includes(url.protocol) && url.hostname && !url.username && !url.password) {
      return url.href;
    }
  } catch {
    return null;
  }
  return null;
}

function socialLabel(href) {
  const url = new URL(href);
  const host = url.hostname;
  const domain = Object.keys(contactHostAliases).find((key) => host === key || host.endsWith(`.${key}`));
  return domain ? `https://${contactHostAliases[domain]}/` : `${url.protocol}//${host}/`;
}

function addContacts(row, values, label, phones = false) {
  const cell = addCell(row, null, label);
  if (!values.length) return;
  cell.textContent = "";
  values.forEach((value, index) => {
    if (index) cell.append("; ");
    const href = contactUrl(value, phones);
    if (!href) {
      cell.append(value);
      return;
    }
    const link = document.createElement("a");
    link.href = href;
    link.textContent = phones ? value : socialLabel(href);
    if (!phones) {
      link.className = "social-link";
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    }
    cell.append(link);
  });
}

async function markCalled(company, row, checkbox, problem) {
  if (searching) {
    checkbox.checked = false;
    return;
  }
  checkbox.disabled = true;
  problem.hidden = true;
  pendingMarks += 1;
  updateControls();
  try {
    const result = await api(`/v1/companies/${encodeURIComponent(company.id)}/called`, { method: "PUT" });
    if (result.called !== true) throw new Error("Не удалось сохранить отметку.");
    row.remove();
    summary.textContent = `Показано ${results.childElementCount} из ${requested}`;
    updateTableVisibility("Все компании из этой выдачи отмечены.");
  } catch (error) {
    checkbox.checked = false;
    problem.textContent = error.message;
    problem.hidden = false;
  } finally {
    pendingMarks -= 1;
    checkbox.disabled = false;
    updateControls();
  }
}

function renderResults(result) {
  results.replaceChildren();
  for (const company of result.companies) {
    const row = results.insertRow();
    addCell(row, company.name);
    addCell(row, company.address, "Точный адрес");
    addContacts(row, company.phones, "Телефоны", true);
    addContacts(row, company.socials, "Соцсети");
    const cell = row.insertCell();
    const label = document.createElement("label");
    label.className = "mark";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", `Прозванивалась, ${company.name}`);
    label.append(checkbox, "Прозванивалась");
    const problem = document.createElement("small");
    problem.className = "mark-error";
    problem.setAttribute("role", "alert");
    problem.hidden = true;
    cell.append(label, problem);
    checkbox.addEventListener("change", () => markCalled(company, row, checkbox, problem));
  }

  requested = result.requested;
  summary.textContent = `Найдено ${result.returned} из ${requested}`;
  const issues = result.issues.map((issue) => {
    if (issue.source === "yandex" && issue.code === "access_restricted") {
      return "Карты Яндекса ограничили доступ с этого сервера.";
    }
    return issue.message;
  });
  if (issues.length) showNotice(issues.join(" "), result.returned ? "warning" : "error");
  updateTableVisibility(issues.length ? "Карточки недоступны по этому запросу." : "Непрозвоненные компании не найдены.");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (searching || pendingMarks) return;
  const fields = new FormData(form);
  const payload = {
    city: fields.get("city").trim(),
    company: fields.get("company").trim(),
    limit: Number(fields.get("limit")),
    source: fields.get("source"),
  };
  if (!payload.city || !payload.company || !Number.isInteger(payload.limit) || payload.limit < 1 || payload.limit > 100) {
    showNotice("Укажите город, компанию и количество от 1 до 100.");
    return;
  }

  searching = true;
  updateControls();
  showNotice("");
  let started = false;
  try {
    const job = await api("/v1/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    started = true;
    sourceTag.textContent = sourceNames[payload.source];
    sourceTag.hidden = false;
    results.replaceChildren();
    summary.textContent = "Идёт поиск";
    updateTableVisibility("Открываем карточки организаций…");
    renderResults(await waitForResult(job.job_id));
  } catch (error) {
    showNotice(error.message);
    if (started) {
      summary.textContent = "Поиск прерван";
      updateTableVisibility("Запустите поиск снова.");
    }
  } finally {
    searching = false;
    updateControls();
  }
});
