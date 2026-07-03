const header = document.querySelector("[data-header]");
const menuToggle = document.querySelector("[data-menu-toggle]");
const sourceInputs = document.querySelectorAll('input[name="source_path"]');
const ORIGIN_KEYS = [
  "utm_source",
  "utm_medium",
  "utm_campaign",
  "utm_content",
  "utm_term",
  "gclid",
  "fbclid",
];

function id(prefix) {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

function readOrigin() {
  const params = new URLSearchParams(window.location.search);
  const stored = JSON.parse(localStorage.getItem("bob_origin") || "{}");
  const origin = { ...stored };

  if (!origin.visitor_id) {
    origin.visitor_id = id("visitante");
    localStorage.setItem("bob_visitor_id", origin.visitor_id);
  }
  origin.visitor_id = localStorage.getItem("bob_visitor_id") || origin.visitor_id;

  if (!sessionStorage.getItem("bob_session_id")) {
    sessionStorage.setItem("bob_session_id", id("sessao"));
  }
  origin.session_id = sessionStorage.getItem("bob_session_id");

  if (!origin.landing_path) {
    origin.landing_path = window.location.pathname + window.location.search;
  }
  origin.referrer = document.referrer || origin.referrer || null;

  ORIGIN_KEYS.forEach((key) => {
    const value = params.get(key);
    if (value) origin[key] = value.slice(0, 240);
  });

  localStorage.setItem("bob_origin", JSON.stringify(origin));
  return origin;
}

const origin = readOrigin();

function updateHeader() {
  if (!header) return;
  header.classList.toggle("is-solid", window.scrollY > 24);
}

window.addEventListener("scroll", updateHeader, { passive: true });
updateHeader();

if (menuToggle) {
  menuToggle.addEventListener("click", () => {
    const isOpen = document.body.classList.toggle("menu-open");
    menuToggle.setAttribute("aria-expanded", String(isOpen));
  });
}

sourceInputs.forEach((input) => {
  input.value = window.location.pathname;
});

document.querySelectorAll("[data-lead-form]").forEach((form) => {
  const status = form.querySelector("[data-form-status]");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector('button[type="submit"]');
    const data = new FormData(form);
    const payload = {
      kind: data.get("kind") || "geral",
      name: String(data.get("name") || "").trim(),
      phone: String(data.get("phone") || "").trim(),
      email: String(data.get("email") || "").trim() || null,
      message: String(data.get("message") || "").trim() || null,
      consent: data.get("consent") === "on",
      source_path: data.get("source_path") || window.location.pathname,
      ...origin,
    };

    status.textContent = "Enviando...";
    status.className = "form-status";
    submit.disabled = true;

    try {
      const response = await fetch("/api/leads", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) {
        throw new Error(result.detail || "Não foi possível enviar agora.");
      }
      status.textContent = result.message || "Contato recebido.";
      status.classList.add("success");
      form.reset();
      sourceInputs.forEach((input) => {
        input.value = window.location.pathname;
      });
    } catch (error) {
      status.textContent = error.message || "Erro ao enviar. Confira os dados e tente novamente.";
      status.classList.add("error");
    } finally {
      submit.disabled = false;
    }
  });
});

window.addEventListener("load", () => {
  if (document.body.classList.contains("page-admin") || document.body.classList.contains("page-admin-login")) {
    return;
  }
  fetch("/api/track", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path: window.location.pathname,
      referrer: document.referrer || null,
      ...origin,
    }),
    keepalive: true,
  }).catch(() => {});
});

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderMedia(message) {
  if (!message.media_url) return "";
  const url = escapeHtml(message.media_url);
  const name = escapeHtml(message.media_name || "Arquivo");
  const mime = String(message.media_mime || "");
  if (mime.startsWith("image/")) {
    return `<div class="chat-media"><a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${name}"></a></div>`;
  }
  if (mime.startsWith("audio/")) {
    return `<div class="chat-media"><audio controls src="${url}"></audio><a href="${url}" target="_blank" rel="noopener">${name}</a></div>`;
  }
  if (mime.startsWith("video/")) {
    return `<div class="chat-media"><video controls src="${url}"></video><a href="${url}" target="_blank" rel="noopener">${name}</a></div>`;
  }
  return `<div class="chat-media"><a href="${url}" target="_blank" rel="noopener">${name}</a></div>`;
}

function initAdminChat() {
  const app = document.querySelector("[data-admin-app]");
  if (!app) return;
  const list = app.querySelector("[data-conversation-list]");
  const messagesEl = app.querySelector("[data-chat-messages]");
  const headerEl = app.querySelector("[data-chat-header]");
  const form = app.querySelector("[data-chat-form]");
  const note = app.querySelector("[data-chat-note]");
  let activeId = null;

  async function loadMessages(id) {
    activeId = id;
    list?.querySelectorAll("[data-conversation-id]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.conversationId === String(id));
    });
    messagesEl.innerHTML = '<div class="admin-empty">Carregando conversa...</div>';
    const response = await fetch(`/api/admin/conversations/${id}/messages`);
    const result = await response.json();
    if (!response.ok || !result.ok) {
      messagesEl.innerHTML = '<div class="admin-empty">Não foi possível carregar a conversa.</div>';
      return;
    }
    const c = result.conversation;
    headerEl.innerHTML = `<strong>${escapeHtml(c.name || c.phone)}</strong><span>${escapeHtml(c.phone)}</span>`;
    if (!result.messages.length) {
      messagesEl.innerHTML = '<div class="admin-empty">Nenhuma mensagem nessa conversa.</div>';
      return;
    }
    messagesEl.innerHTML = result.messages
      .map((message) => {
        const text = message.text ? `<p>${escapeHtml(message.text).replaceAll("\n", "<br>")}</p>` : "";
        const fallback = !text && !message.media_url ? `<p>${escapeHtml(message.media_name || message.message_type || "Mensagem")}</p>` : "";
        return `
          <article class="chat-bubble ${message.direction === "out" ? "is-out" : "is-in"}">
            ${text || fallback}
            ${renderMedia(message)}
            <time>${escapeHtml(message.created_at || "")} · ${escapeHtml(message.status || "registrada")}</time>
          </article>
        `;
      })
      .join("");
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  list?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-conversation-id]");
    if (!button) return;
    loadMessages(button.dataset.conversationId).catch(() => {
      messagesEl.innerHTML = '<div class="admin-empty">Erro ao abrir a conversa.</div>';
    });
  });

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!activeId) {
      note.textContent = "Selecione uma conversa antes de enviar.";
      return;
    }
    const submit = form.querySelector('button[type="submit"]');
    const payload = new FormData(form);
    submit.disabled = true;
    note.textContent = "Enviando...";
    try {
      const response = await fetch(`/api/admin/conversations/${activeId}/messages`, {
        method: "POST",
        body: payload,
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "Não foi possível enviar.");
      form.reset();
      note.textContent = result.status === "dry_run" ? "Registrado em modo teste." : "Mensagem enviada.";
      await loadMessages(activeId);
    } catch (error) {
      note.textContent = error.message || "Erro ao enviar.";
    } finally {
      submit.disabled = false;
    }
  });

  app.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(button.dataset.copy || "");
        button.textContent = "Copiado";
      } catch {
        button.textContent = "Copie manualmente";
      }
    });
  });

  const first = list?.querySelector("[data-conversation-id]");
  if (first) {
    loadMessages(first.dataset.conversationId).catch(() => {});
  }
}

initAdminChat();
