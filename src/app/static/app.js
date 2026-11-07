"use strict";

// Order intake frontend. Talks to the same REST API as any client, with a bearer token.
// Every value from the server is rendered with textContent, never parsed as HTML.
(() => {
  const FIELDS = [
    ["patient_first_name", "Patient first name"],
    ["patient_last_name", "Patient last name"],
    ["patient_date_of_birth", "Patient date of birth"],
    ["provider_name", "Ordering provider"],
    ["provider_npi", "Provider NPI"],
  ];
  const TOKEN_KEY = "order-intake-token";
  const OK_STATUSES = new Set(["ok", "valid", "ready", "found", "supported", "verified"]);
  const BAD_STATUSES = new Set(["invalid", "invalid_checksum", "failed", "extraction_failed"]);

  const byId = (id) => document.getElementById(id);
  let token = sessionStorage.getItem(TOKEN_KEY);

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  // Build an element from a tag, attributes and children (strings become text nodes).
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "className") node.className = value;
      else if (key === "onClick") node.addEventListener("click", value);
      else if (key === "onSubmit") node.addEventListener("submit", value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children) {
      if (child === null || child === undefined || child === false) continue;
      node.append(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
    }
    return node;
  }

  function describeError(status, data) {
    const code = data && typeof data.error === "string" ? data.error : null;
    const reference = data && data.request_id ? ` (reference ${data.request_id})` : "";
    if (status === 413 || code === "request_too_large") return "That file is too large." + reference;
    if (status === 429) return "Too many requests. Please wait a minute and try again." + reference;
    if (status === 503) return "The service is not ready yet (database or extraction unavailable)." + reference;
    if (code === "invalid_request" && Array.isArray(data.problems) && data.problems.length) {
      const fields = data.problems.map((p) => `${p.field || "request"}: ${p.problem || "invalid"}`);
      return "Please check: " + fields.join("; ") + reference;
    }
    if (code) return code.replace(/_/g, " ") + reference;
    if (data && typeof data.detail === "string") return data.detail + reference;
    return `Request failed (HTTP ${status})` + reference;
  }

  async function api(path, { method = "GET", json, form } = {}) {
    const headers = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    let body;
    if (json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    } else if (form !== undefined) {
      body = form; // FormData or URLSearchParams; the browser sets the content type
    }
    const response = await fetch(path, { method, headers, body });
    if (response.status === 204) return null;
    const type = response.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      if (response.status === 401 && token) signOutLocally("Your session has ended. Please sign in again.");
      throw new ApiError(response.status, data && data.error, describeError(response.status, data));
    }
    return data;
  }

  function showMessage(text, isError = false) {
    const box = byId("message");
    box.textContent = text;
    box.classList.toggle("error", isError);
    box.hidden = !text;
  }

  function badge(status) {
    const value = String(status || "unknown");
    const kind = OK_STATUSES.has(value) ? "ok" : BAD_STATUSES.has(value) ? "bad" : "";
    return el("span", { className: `badge ${kind}`.trim() }, value.replace(/_/g, " "));
  }

  function setBusy(form, busy) {
    for (const control of form.querySelectorAll("button, input")) control.disabled = busy;
  }

  // ----- Session -----

  function setToken(value) {
    token = value;
    if (value) sessionStorage.setItem(TOKEN_KEY, value);
    else sessionStorage.removeItem(TOKEN_KEY);
  }

  function signOutLocally(message) {
    setToken(null);
    byId("app-view").hidden = true;
    byId("session").hidden = true;
    byId("auth-view").hidden = false;
    byId("detail").hidden = true;
    byId("detail").replaceChildren();
    byId("orders-body").replaceChildren();
    if (message) showMessage(message, true);
  }

  async function enterApp() {
    const me = await api("/auth/me");
    byId("whoami").textContent = `Signed in as ${me.username}`;
    byId("auth-view").hidden = true;
    byId("session").hidden = false;
    byId("app-view").hidden = false;
    await loadOrders();
  }

  async function signIn(username, password) {
    const data = await api("/auth/token", {
      method: "POST",
      form: new URLSearchParams({ username, password }),
    });
    setToken(data.access_token);
    await enterApp();
  }

  async function onLogin(event) {
    event.preventDefault();
    const form = event.currentTarget;
    setBusy(form, true);
    try {
      await signIn(form.username.value.trim(), form.password.value);
      form.reset();
      showMessage("");
    } catch (error) {
      showMessage(error.status === 401 ? "Wrong username or password." : error.message, true);
    } finally {
      setBusy(form, false);
    }
  }

  async function onRegister(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const username = form.username.value.trim();
    const password = form.password.value;
    setBusy(form, true);
    try {
      await api("/auth/register", { method: "POST", json: { username, password } });
      await signIn(username, password);
      form.reset();
      showMessage(`Account created. Signed in as ${username}.`);
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      setBusy(form, false);
    }
  }

  async function onLogout() {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      // The local sign-out below still happens.
    }
    signOutLocally();
    showMessage("Signed out.");
  }

  // ----- Orders -----

  function formatPatient(order) {
    const name = [order.patient_first_name, order.patient_last_name].filter(Boolean).join(" ");
    return name || "(no name)";
  }

  async function loadOrders() {
    const orders = await api("/orders?limit=100&offset=0");
    const body = byId("orders-body");
    body.replaceChildren(
      ...orders.map((order) =>
        el(
          "tr",
          {},
          el("td", {}, formatPatient(order)),
          el("td", {}, order.patient_date_of_birth || "—"),
          el("td", {}, order.provider_name || "—"),
          el("td", {}, order.provider_npi || "—"),
          el("td", {}, badge(order.status)),
          el("td", { className: "muted" }, new Date(order.created_at).toLocaleString()),
          el("td", {}, el("button", { type: "button", className: "secondary", onClick: () => openOrder(order.id) }, "Open")),
        ),
      ),
    );
    byId("orders-empty").hidden = orders.length > 0;
    byId("orders-table").hidden = orders.length === 0;
  }

  async function openOrder(id) {
    try {
      renderOrder(await api(`/orders/${encodeURIComponent(id)}`));
    } catch (error) {
      showMessage(error.message, true);
    }
  }

  function fieldInputs(values) {
    return FIELDS.map(([name, label]) =>
      el(
        "label",
        {},
        label,
        el("input", {
          name,
          type: name === "patient_date_of_birth" ? "date" : "text",
          value: values[name] || "",
          inputmode: name === "provider_npi" ? "numeric" : undefined,
        }),
      ),
    );
  }

  function readChanges(form, original) {
    const changes = {};
    for (const [name] of FIELDS) {
      const value = form.elements[name].value.trim();
      const next = value === "" ? null : value;
      if (next !== (original[name] ?? null)) changes[name] = next;
    }
    return changes;
  }

  function renderOrder(order) {
    const detail = byId("detail");
    const rows = FIELDS.map(([name, label]) => {
      const report = (order.fields && order.fields[name]) || {};
      return el(
        "tr",
        {},
        el("th", {}, label),
        el("td", {}, order[name] ?? "—"),
        el("td", {}, badge(report.status)),
        el("td", {}, report.page ? `page ${report.page}` : "—"),
        el("td", { className: "muted" }, report.reason || (report.extracted_text ? `read as "${report.extracted_text}"` : "")),
      );
    });
    const warnings = Array.isArray(order.warnings) && order.warnings.length
      ? el("ul", { className: "warnings" }, ...order.warnings.map((w) => el("li", {}, w.message || `${w.field}: ${w.code}`)))
      : null;

    const editForm = el("form", { onSubmit: (event) => saveOrder(event, order) }, ...fieldInputs(order),
      el("div", { className: "actions" },
        el("button", { type: "submit" }, "Save changes"),
        el("button", { type: "button", className: "danger", onClick: () => deleteOrder(order) }, "Delete order"),
      ),
    );

    detail.replaceChildren(
      el("div", { className: "row" },
        el("h2", {}, `Order for ${formatPatient(order)}`),
        badge(order.status),
      ),
      el("p", { className: "hint" }, `Source: ${order.source} · created ${new Date(order.created_at).toLocaleString()} · id ${order.id}`),
      el("table", {},
        el("thead", {}, el("tr", {}, el("th", {}, "Field"), el("th", {}, "Value"), el("th", {}, "Check"), el("th", {}, "Found on"), el("th", {}, "Note"))),
        el("tbody", {}, ...rows),
      ),
      warnings,
      el("h2", {}, "Correct or complete the order"),
      editForm,
    );
    detail.hidden = false;
    detail.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderNewOrder() {
    const detail = byId("detail");
    const form = el("form", { onSubmit: createOrder }, ...fieldInputs({}),
      el("div", { className: "actions" }, el("button", { type: "submit" }, "Create order")),
    );
    detail.replaceChildren(
      el("h2", {}, "New order (manual entry)"),
      el("p", { className: "hint" }, "Leave a field empty if it is unknown; it stays missing rather than guessed."),
      form,
    );
    detail.hidden = false;
    detail.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function createOrder(event) {
    event.preventDefault();
    const form = event.currentTarget;
    setBusy(form, true);
    try {
      const order = await api("/orders", { method: "POST", json: readChanges(form, {}) });
      showMessage("Order created.");
      renderOrder(order);
      await loadOrders();
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      setBusy(form, false);
    }
  }

  async function saveOrder(event, order) {
    event.preventDefault();
    const form = event.currentTarget;
    const changes = readChanges(form, order);
    if (Object.keys(changes).length === 0) {
      showMessage("Nothing changed.");
      return;
    }
    setBusy(form, true);
    try {
      const updated = await api(`/orders/${encodeURIComponent(order.id)}`, { method: "PATCH", json: changes });
      showMessage("Order updated.");
      renderOrder(updated);
      await loadOrders();
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      setBusy(form, false);
    }
  }

  async function deleteOrder(order) {
    if (!window.confirm(`Delete the order for ${formatPatient(order)}? This cannot be undone.`)) return;
    try {
      await api(`/orders/${encodeURIComponent(order.id)}`, { method: "DELETE" });
      byId("detail").hidden = true;
      byId("detail").replaceChildren();
      showMessage("Order deleted.");
      await loadOrders();
    } catch (error) {
      showMessage(error.message, true);
    }
  }

  async function onUpload(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const file = form.file.files[0];
    if (!file) return;
    const data = new FormData();
    data.append("file", file);
    setBusy(form, true);
    showMessage(`Extracting ${file.name}…`);
    try {
      const order = await api("/orders/upload", { method: "POST", form: data });
      const note = order.status === "ready" ? "All fields passed their checks." : "Some fields need review; see the checks below.";
      showMessage(`Extracted ${file.name}. ${note}`);
      form.reset();
      renderOrder(order);
      await loadOrders();
    } catch (error) {
      showMessage(error.message, true);
    } finally {
      setBusy(form, false);
    }
  }

  // ----- Start -----

  document.addEventListener("DOMContentLoaded", async () => {
    byId("login-form").addEventListener("submit", onLogin);
    byId("register-form").addEventListener("submit", onRegister);
    byId("upload-form").addEventListener("submit", onUpload);
    byId("logout").addEventListener("click", onLogout);
    byId("refresh").addEventListener("click", () => loadOrders().catch((e) => showMessage(e.message, true)));
    byId("new-order").addEventListener("click", renderNewOrder);
    if (token) {
      try {
        await enterApp();
      } catch {
        signOutLocally();
      }
    }
  });
})();
