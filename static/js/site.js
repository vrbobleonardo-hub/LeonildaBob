const header = document.querySelector("[data-header]");
const menuToggle = document.querySelector("[data-menu-toggle]");
const siteNav = document.querySelector("[data-site-nav]");
const mobileQuery = window.matchMedia("(max-width: 980px)");
const ORIGIN_KEYS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "gclid", "fbclid"];
const ORIGIN_FIELD_LIMITS = {
  landing_path: 300,
  referrer: 500,
  visitor_id: 80,
  session_id: 80,
  utm_source: 120,
  utm_medium: 120,
  utm_campaign: 180,
  utm_content: 180,
  utm_term: 180,
  gclid: 240,
  fbclid: 240,
};
const CONSENT_KEY = "bob_consent_v1";
const ORIGIN_KEY = "bob_origin_v2";
const ORIGIN_TTL_MS = 30 * 24 * 60 * 60 * 1000;
const CONSENT_TTL_MS = 365 * 24 * 60 * 60 * 1000;

function safeGet(storage, key) {
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(storage, key, value) {
  try {
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function safeRemove(storage, key) {
  try {
    storage.removeItem(key);
  } catch {
    // Storage may be disabled by the browser.
  }
}

function safeJson(value, fallback = {}) {
  try {
    return value ? JSON.parse(value) : fallback;
  } catch {
    return fallback;
  }
}

function createId(prefix) {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
  return `${prefix}_${random}`;
}

function setStoredConsent(key, value) {
  safeSet(localStorage, key, JSON.stringify({ value, saved_at: Date.now() }));
}

function storedConsent(key) {
  const raw = safeGet(localStorage, key);
  if (raw === "accepted" || raw === "rejected") {
    setStoredConsent(key, raw);
    return raw;
  }
  const parsed = safeJson(raw, null);
  if (!parsed?.saved_at || Date.now() - parsed.saved_at > CONSENT_TTL_MS) {
    safeRemove(localStorage, key);
    return null;
  }
  return ["accepted", "rejected"].includes(parsed.value) ? parsed.value : null;
}

function privacySignalEnabled() {
  return navigator.globalPrivacyControl === true || navigator.doNotTrack === "1";
}

function analyticsAllowed() {
  return !privacySignalEnabled() && storedConsent(CONSENT_KEY) === "accepted";
}

function persistConsentCookie(value) {
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `bob_analytics_consent=${value}; Path=/; Max-Age=31536000; SameSite=Lax${secure}`;
}

function readOrigin() {
  if (!analyticsAllowed()) return {};
  const params = new URLSearchParams(window.location.search);
  const stored = safeJson(safeGet(localStorage, ORIGIN_KEY));
  const now = Date.now();
  const origin = stored.saved_at && now - stored.saved_at <= ORIGIN_TTL_MS ? { ...stored } : {};

  if (!origin.visitor_id) origin.visitor_id = createId("visitante");
  let sessionId = safeGet(sessionStorage, "bob_session_id");
  if (!sessionId) {
    sessionId = createId("sessao");
    safeSet(sessionStorage, "bob_session_id", sessionId);
  }
  origin.session_id = sessionId;
  if (!origin.landing_path) origin.landing_path = window.location.pathname;

  if (document.referrer) {
    try {
      const referrer = new URL(document.referrer);
      if (referrer.origin !== window.location.origin && !origin.referrer) {
        origin.referrer = `${referrer.origin}${referrer.pathname}`.slice(0, ORIGIN_FIELD_LIMITS.referrer);
      }
    } catch {
      // Ignore malformed browser referrers.
    }
  }

  let campaignChanged = false;
  ORIGIN_KEYS.forEach((key) => {
    const value = params.get(key);
    if (value) {
      const normalized = key.startsWith("utm_") ? value.trim().toLowerCase() : value.trim();
      origin[key] = normalized.slice(0, ORIGIN_FIELD_LIMITS[key]);
      campaignChanged = true;
    }
  });
  if (campaignChanged || !origin.saved_at) origin.saved_at = now;
  safeSet(localStorage, ORIGIN_KEY, JSON.stringify(origin));
  return origin;
}

function updateHeader() {
  header?.classList.toggle("is-solid", window.scrollY > 24);
}

function setMenu(open, { moveFocus = false } = {}) {
  if (!menuToggle || !siteNav) return;
  const mobile = mobileQuery.matches;
  const isOpen = mobile && open;
  document.body.classList.toggle("menu-open", isOpen);
  menuToggle.setAttribute("aria-expanded", String(isOpen));
  menuToggle.querySelector("[data-menu-label]").textContent = isOpen ? "Fechar menu" : "Menu";
  siteNav.toggleAttribute("aria-hidden", mobile && !isOpen);
  siteNav.inert = mobile && !isOpen;
  if (isOpen && moveFocus) siteNav.querySelector("a")?.focus();
}

window.addEventListener("scroll", updateHeader, { passive: true });
updateHeader();
setMenu(false);
menuToggle?.addEventListener("click", () => {
  setMenu(menuToggle.getAttribute("aria-expanded") !== "true", { moveFocus: true });
});
siteNav?.addEventListener("click", (event) => {
  if (event.target.closest("a")) setMenu(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Tab" && menuToggle?.getAttribute("aria-expanded") === "true" && siteNav) {
    const links = [...siteNav.querySelectorAll("a")];
    if (links.length) {
      const first = links[0];
      const last = links[links.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }
  if (event.key === "Escape" && menuToggle?.getAttribute("aria-expanded") === "true") {
    setMenu(false);
    menuToggle.focus();
  }
});
if (mobileQuery.addEventListener) {
  mobileQuery.addEventListener("change", () => setMenu(false));
} else {
  mobileQuery.addListener?.(() => setMenu(false));
}

const progressBar = document.querySelector("[data-scroll-progress]");
const contrastToggle = document.querySelector("[data-contrast-toggle]");
const fontToggle = document.querySelector("[data-font-toggle]");
const stickyCta = document.querySelector("[data-mobile-sticky-cta]");
const ACCESSIBILITY_KEY = "bob_accessibility_v1";

function readAccessibilityPrefs() {
  return safeJson(safeGet(localStorage, ACCESSIBILITY_KEY), {});
}

function saveAccessibilityPrefs(prefs) {
  safeSet(localStorage, ACCESSIBILITY_KEY, JSON.stringify(prefs));
}

function applyAccessibilityPrefs(prefs) {
  const highContrast = prefs.contrast === true;
  const largeFont = prefs.font === true;
  document.body.classList.toggle("high-contrast", highContrast);
  document.body.classList.toggle("font-large", largeFont);
  contrastToggle?.setAttribute("aria-pressed", String(highContrast));
  fontToggle?.setAttribute("aria-pressed", String(largeFont));
}

let accessibilityPrefs = readAccessibilityPrefs();
applyAccessibilityPrefs(accessibilityPrefs);

contrastToggle?.addEventListener("click", () => {
  accessibilityPrefs = { ...accessibilityPrefs, contrast: accessibilityPrefs.contrast !== true };
  applyAccessibilityPrefs(accessibilityPrefs);
  saveAccessibilityPrefs(accessibilityPrefs);
});

fontToggle?.addEventListener("click", () => {
  accessibilityPrefs = { ...accessibilityPrefs, font: accessibilityPrefs.font !== true };
  applyAccessibilityPrefs(accessibilityPrefs);
  saveAccessibilityPrefs(accessibilityPrefs);
});

function updateScrollAffordances() {
  if (progressBar) {
    const scrollable = document.documentElement.scrollHeight - window.innerHeight;
    const progress = scrollable > 0 ? Math.min(1, Math.max(0, window.scrollY / scrollable)) : 0;
    progressBar.style.transform = `scaleX(${progress})`;
  }
  if (stickyCta) {
    const hero = document.querySelector(".bpc-hero");
    const threshold = hero ? hero.offsetHeight * 0.72 : 360;
    stickyCta.classList.toggle("is-visible", window.scrollY > threshold);
  }
}

window.addEventListener("scroll", updateScrollAffordances, { passive: true });
window.addEventListener("resize", updateScrollAffordances);
updateScrollAffordances();

function initReveal() {
  const revealItems = [...document.querySelectorAll("[data-reveal]")];
  if (!revealItems.length || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    revealItems.forEach((item) => item.classList.add("is-visible"));
    return;
  }
  document.body.classList.add("reveal-ready");
  if (!("IntersectionObserver" in window)) {
    revealItems.forEach((item) => item.classList.add("is-visible"));
    return;
  }
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add("is-visible");
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.16, rootMargin: "0px 0px -8% 0px" });
  revealItems.forEach((item) => observer.observe(item));
}

initReveal();

document.querySelectorAll(".faq-section, .bpc-faq-list").forEach((group) => {
  const items = [...group.children].filter((item) => item.tagName === "DETAILS");
  items.forEach((item) => {
    item.addEventListener("toggle", () => {
      if (!item.open) return;
      items.forEach((other) => {
        if (other !== item) other.open = false;
      });
    });
  });
});

let origin = readOrigin();

function attributionPayload() {
  return Object.fromEntries(
    Object.entries(ORIGIN_FIELD_LIMITS)
      .filter(([key]) => origin[key])
      .map(([key, limit]) => [key, String(origin[key]).slice(0, limit)]),
  );
}

function trackingPayload(path = window.location.pathname) {
  return {
    path,
    ...attributionPayload(),
  };
}

function track(path = window.location.pathname) {
  if (!analyticsAllowed() || document.body.classList.contains("page-admin") || document.body.classList.contains("page-admin-login")) return;
  const body = JSON.stringify(trackingPayload(path));
  if (navigator.sendBeacon?.("/api/track", new Blob([body], { type: "application/json" }))) {
    return;
  }
  fetch("/api/track", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    keepalive: true,
  }).catch(() => {});
}

function conversionPath(base) {
  return document.body.classList.contains("page-landing-bpc") ? `${base}/bpc` : `${base}/institucional`;
}

document.addEventListener("click", (event) => {
  const link = event.target.closest('a[href^="https://wa.me/"]');
  if (!link) return;
  track(conversionPath("/conversion/whatsapp"));
});

function loadMap() {
  const frame = document.querySelector("[data-consent-map]");
  const placeholder = document.querySelector("[data-map-placeholder]");
  if (!frame || !frame.dataset.mapSrc) return;
  frame.src = frame.dataset.mapSrc;
  frame.hidden = false;
  if (placeholder) placeholder.hidden = true;
}

function unloadMap() {
  const frame = document.querySelector("[data-consent-map]");
  const placeholder = document.querySelector("[data-map-placeholder]");
  if (frame) {
    frame.removeAttribute("src");
    frame.hidden = true;
  }
  if (placeholder) placeholder.hidden = false;
}

function initConsent() {
  const banner = document.querySelector("[data-consent-banner]");
  const manageButton = document.querySelector("[data-consent-manage]");
  let current;
  if (privacySignalEnabled()) {
    setStoredConsent(CONSENT_KEY, "rejected");
    persistConsentCookie("rejected");
    safeRemove(localStorage, ORIGIN_KEY);
    safeRemove(sessionStorage, "bob_session_id");
    if (banner) banner.hidden = true;
    if (manageButton) manageButton.hidden = true;
    current = "rejected";
  } else {
    current = storedConsent(CONSENT_KEY);
  }
  if (banner && !current) banner.hidden = false;
  if (current === "accepted") {
    persistConsentCookie("accepted");
  }

  banner?.querySelector("[data-consent-accept]")?.addEventListener("click", () => {
    if (privacySignalEnabled()) {
      setStoredConsent(CONSENT_KEY, "rejected");
      persistConsentCookie("rejected");
      banner.hidden = true;
      return;
    }
    const firstAcceptance = !analyticsAllowed();
    setStoredConsent(CONSENT_KEY, "accepted");
    persistConsentCookie("accepted");
    banner.hidden = true;
    origin = readOrigin();
    if (firstAcceptance) track();
  });
  banner?.querySelector("[data-consent-reject]")?.addEventListener("click", () => {
    setStoredConsent(CONSENT_KEY, "rejected");
    safeRemove(localStorage, ORIGIN_KEY);
    safeRemove(sessionStorage, "bob_session_id");
    setStoredConsent("bob_map_consent", "rejected");
    persistConsentCookie("rejected");
    origin = {};
    unloadMap();
    banner.hidden = true;
  });
  document.querySelector("[data-load-map]")?.addEventListener("click", () => {
    setStoredConsent("bob_map_consent", "accepted");
    loadMap();
  });
  manageButton?.addEventListener("click", () => {
    if (!banner || privacySignalEnabled()) return;
    banner.hidden = false;
    banner.querySelector("h2")?.focus();
  });
  if (storedConsent("bob_map_consent") === "accepted") loadMap();
}

function formatApiError(result) {
  if (Array.isArray(result?.errors) && result.errors.length) {
    return result.errors.map((item) => `${item.field}: ${item.message}`).join(" ");
  }
  if (typeof result?.detail === "string") return result.detail;
  return "Não foi possível enviar agora. Revise os dados e tente novamente.";
}

document.querySelectorAll("[data-lead-form]").forEach((form) => {
  const status = form.querySelector("[data-form-status]");
  const submit = form.querySelector('button[type="submit"]');
  const submitLabel = submit?.textContent || "Enviar";
  const source = form.querySelector('input[name="source_path"]');
  const started = form.querySelector("[data-form-started]");
  const requestKey = form.querySelector("[data-request-key]");
  if (source) source.value = window.location.pathname;
  if (started) started.value = String(Math.floor(Date.now() / 1000));
  if (requestKey && !requestKey.value) requestKey.value = createId("lead");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const data = new FormData(form);
    const payload = {
      kind: data.get("kind") || "geral",
      area: data.get("area") || null,
      name: String(data.get("name") || "").trim(),
      phone: String(data.get("phone") || "").trim(),
      email: String(data.get("email") || "").trim() || null,
      message: String(data.get("message") || "").trim(),
      consent: data.get("consent") === "on",
      website: String(data.get("website") || ""),
      form_started_at: Number(data.get("form_started_at")) || null,
      source_path: data.get("source_path") || window.location.pathname,
      ...(analyticsAllowed() ? attributionPayload() : {}),
    };

    status.textContent = "Enviando...";
    status.className = "form-status";
    if (submit) {
      submit.style.minWidth = `${submit.offsetWidth}px`;
      submit.textContent = "Enviando...";
      submit.disabled = true;
    }
    form.setAttribute("aria-busy", "true");
    try {
      const response = await fetch("/api/leads", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": requestKey?.value || createId("lead"),
        },
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok || !result.ok) throw new Error(formatApiError(result));
      status.textContent = result.message || "Contato recebido.";
      status.classList.add("success");
      form.reset();
      if (source) source.value = window.location.pathname;
      if (started) started.value = String(Math.floor(Date.now() / 1000));
      if (requestKey) requestKey.value = createId("lead");
      track(conversionPath("/conversion/lead"));
    } catch (error) {
      status.textContent = error.message || "Erro ao enviar. Confira os dados e tente novamente.";
      status.classList.add("error");
      status.focus();
    } finally {
      form.removeAttribute("aria-busy");
      if (submit) {
        submit.textContent = submitLabel;
        submit.disabled = false;
        submit.style.removeProperty("min-width");
      }
    }
  });
});

initConsent();
if (analyticsAllowed()) track();

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function localDate(value) {
  const date = new Date(value || "");
  return Number.isNaN(date.getTime()) ? String(value || "") : new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short", timeZone: "America/Sao_Paulo" }).format(date);
}

function labelKind(value) {
  const labels = {
    bpc: "BPC/LOAS",
    trabalhista: "Trabalhista",
    instituto: "Instituto",
    geral: "Geral",
    previdenciario: "Previdenciário",
  };
  return labels[String(value || "").toLowerCase()] || value || "Geral";
}

function labelStatus(value) {
  const labels = {
    open: "Aberta",
    closed: "Encerrada",
    archived: "Arquivada",
    new: "Novo",
    contacted: "Contatado",
    qualified: "Qualificado",
  };
  return labels[String(value || "").toLowerCase()] || value || "Registrada";
}

document.querySelectorAll("[data-datetime]").forEach((element) => {
  element.textContent = localDate(element.getAttribute("datetime"));
});

function renderMedia(message) {
  if (!message.media_url || !String(message.media_url).startsWith("/api/admin/media?")) return "";
  const url = escapeHtml(message.media_url);
  const name = escapeHtml(message.media_name || "Arquivo");
  const mime = String(message.media_mime || "");
  if (mime.startsWith("image/")) return `<div class="chat-media"><a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${name}" loading="lazy"></a></div>`;
  if (mime.startsWith("audio/")) return `<div class="chat-media"><audio controls src="${url}"></audio><a href="${url}">${name}</a></div>`;
  if (mime.startsWith("video/")) return `<div class="chat-media"><video controls src="${url}"></video><a href="${url}">${name}</a></div>`;
  return `<div class="chat-media"><a href="${url}">${name}</a></div>`;
}

function initAdminChat() {
  const app = document.querySelector("[data-admin-app]");
  if (!app) return;
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const list = app.querySelector("[data-conversation-list]");
  const messagesEl = app.querySelector("[data-chat-messages]");
  const headerEl = app.querySelector("[data-chat-header] > div:first-child");
  const controls = app.querySelector("[data-chat-controls]");
  const detailEl = app.querySelector("[data-chat-detail]");
  const form = app.querySelector("[data-chat-form]");
  const note = app.querySelector("[data-chat-note]");
  const search = app.querySelector("[data-conversation-search]");
  const more = app.querySelector("[data-conversation-more]");
  const chatCount = app.querySelector("[data-chat-count]");
  const filterButtons = [...app.querySelectorAll("[data-chat-filter]")];
  const fileInput = app.querySelector("[data-chat-file]");
  const fileLabel = app.querySelector("[data-file-label]");
  const maxUploadBytes = Number(app.dataset.maxUploadBytes) || 25 * 1024 * 1024;
  let activeId = null;
  let activeConversation = null;
  let activeFilter = "all";
  let requestSequence = 0;
  let listRequestSequence = 0;
  let offset = 0;
  let searchTimer = null;

  async function adminFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", csrf);
    const response = await fetch(url, { ...options, headers });
    if (response.status === 401) window.location.assign("/admin/login");
    return response;
  }

  const qrPanel = app.querySelector("[data-whatsapp-qr]");
  const qrStatus = app.querySelector("[data-qr-status]");
  const qrNumber = app.querySelector("[data-qr-number]");
  const qrConnected = app.querySelector("[data-qr-connected]");
  const qrDescription = app.querySelector("[data-qr-description]");
  const qrNote = app.querySelector("[data-qr-note]");
  const qrCodePanel = app.querySelector("[data-qr-code-panel]");
  const qrImage = app.querySelector("[data-qr-image]");

  function qrStatusLabel(session, enabled) {
    if (!enabled) return "Ative o provedor QR";
    const labels = {
      connected: "Conectado",
      connecting: "Conectando",
      qr: "Aguardando leitura do QR",
      disconnected: "Desconectado",
      failed: "Falha de conexão",
      unavailable: "Bridge indisponível",
    };
    return labels[session?.status] || "Aguardando conexão";
  }

  function renderQrSession(payload) {
    if (!qrPanel) return;
    const session = payload?.session || {};
    const enabled = payload?.enabled === true;
    const isConnected = session.status === "connected" && session.can_send;
    qrStatus.textContent = qrStatusLabel(session, enabled);
    qrNumber.textContent = session.phone_wa_id || "—";
    qrConnected.textContent = session.last_connected_at ? localDate(session.last_connected_at) : "—";
    qrDescription.textContent = enabled
      ? (isConnected ? "A sessão está pronta para enviar e receber mensagens neste inbox." : "Gere o QR e leia-o pelo WhatsApp do telefone que será usado no atendimento.")
      : "Para usar este canal, configure WHATSAPP_PROVIDER=qr, WHATSAPP_DRY_RUN=0 e reinicie o serviço.";
    qrCodePanel.hidden = !session.qr_data_url;
    if (session.qr_data_url) qrImage.src = session.qr_data_url;
    if (!session.qr_data_url) qrImage.removeAttribute("src");
    app.querySelector("[data-qr-connect]").disabled = !enabled || isConnected;
    app.querySelector("[data-qr-reset]").disabled = !enabled;
    if (session.last_error) qrNote.textContent = `Conexão: ${session.last_error}`;
    else if (session.qr_expires_at) qrNote.textContent = `O código expira em ${localDate(session.qr_expires_at)}.`;
    else qrNote.textContent = "";
  }

  async function refreshQrSession(action = "refresh") {
    if (!qrPanel) return;
    const method = action === "refresh" ? "GET" : "POST";
    const options = method === "GET" ? {} : {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    };
    try {
      const response = await adminFetch("/api/admin/whatsapp/qr/session", options);
      const payload = await response.json();
      if (!response.ok) throw new Error(formatApiError(payload));
      renderQrSession(payload);
    } catch (error) {
      qrNote.textContent = error.message || "Não foi possível consultar a conexão QR.";
    }
  }

  qrPanel?.querySelector("[data-qr-connect]")?.addEventListener("click", () => refreshQrSession("connect"));
  qrPanel?.querySelector("[data-qr-refresh]")?.addEventListener("click", () => refreshQrSession());
  qrPanel?.querySelector("[data-qr-reset]")?.addEventListener("click", () => refreshQrSession("disconnect"));
  refreshQrSession();
  window.setInterval(() => {
    if (!document.hidden) refreshQrSession();
  }, 3_000);

  function conversationButton(item) {
    const name = item.name || item.phone || "Contato";
    const initial = escapeHtml(String(name).trim().slice(0, 1) || "?");
    const status = item.status || "open";
    const kind = item.kind || "geral";
    return `<button type="button" data-conversation-id="${Number(item.id)}" data-status="${escapeHtml(status)}" data-kind="${escapeHtml(kind)}" data-bot="${item.bot_enabled ? "true" : "false"}" aria-pressed="false" class="chat-contact"><span class="chat-contact-avatar">${initial}</span><span class="chat-contact-main"><strong>${escapeHtml(name)}</strong><small>${escapeHtml(item.last_message_preview || "Sem mensagem registrada.")}</small></span><span class="chat-contact-meta"><small>${escapeHtml(labelKind(kind))}</small><small>${item.bot_enabled ? "Bot" : escapeHtml(labelStatus(status))}</small></span></button>`;
  }

  function applyConversationFilter() {
    const buttons = [...list.querySelectorAll("[data-conversation-id]")];
    let visible = 0;
    buttons.forEach((button) => {
      const matches = activeFilter === "all"
        || button.dataset.status === activeFilter
        || (activeFilter === "bot" && button.dataset.bot === "true");
      button.hidden = !matches;
      if (matches) visible += 1;
    });
    if (chatCount) chatCount.textContent = String(visible);
  }

  function renderConversationDetail() {
    if (!detailEl) return;
    if (!activeConversation) {
      detailEl.innerHTML = '<div class="chat-detail-empty"><span>Dados</span><strong>Nenhuma conversa selecionada.</strong><p>Ao selecionar um contato, os dados do atendimento aparecerão aqui.</p></div>';
      return;
    }
    detailEl.innerHTML = `
      <article class="chat-detail-card">
        <span class="chat-detail-status">${escapeHtml(labelStatus(activeConversation.status))}</span>
        <h3>${escapeHtml(activeConversation.name || "Contato sem nome")}</h3>
        <dl>
          <div><dt>WhatsApp</dt><dd>${escapeHtml(activeConversation.phone || "Não informado")}</dd></div>
          <div><dt>Área</dt><dd>${escapeHtml(labelKind(activeConversation.kind))}</dd></div>
          <div><dt>Automação</dt><dd>${activeConversation.bot_enabled ? "Ativa" : "Desativada"}</dd></div>
          <div><dt>Primeiro registro</dt><dd>${escapeHtml(localDate(activeConversation.created_at))}</dd></div>
          <div><dt>Última atividade</dt><dd>${escapeHtml(localDate(activeConversation.updated_at || activeConversation.last_message_at))}</dd></div>
          <div><dt>ID do contato</dt><dd>${escapeHtml(activeConversation.source_lead_id || activeConversation.id)}</dd></div>
        </dl>
      </article>
      <article class="chat-detail-card">
        <h3>Próxima ação</h3>
        <p>Verifique documentos, responda com clareza e mantenha o status da conversa atualizado.</p>
      </article>
    `;
  }

  async function loadConversationList({ reset = false } = {}) {
    const sequence = ++listRequestSequence;
    if (reset) {
      offset = 0;
      list.innerHTML = "";
    }
    const query = encodeURIComponent(search?.value.trim() || "");
    if (more) more.disabled = true;
    try {
      const response = await adminFetch(`/api/admin/conversations?limit=50&offset=${offset}&q=${query}`);
      const result = await response.json();
      if (sequence !== listRequestSequence) return;
      if (!response.ok) throw new Error(formatApiError(result));
      list.insertAdjacentHTML("beforeend", result.conversations.map(conversationButton).join(""));
      if (reset && !result.conversations.length) {
        list.innerHTML = '<div class="admin-empty">Nenhuma conversa encontrada.</div>';
      }
      applyConversationFilter();
      offset = result.next_offset;
      more.hidden = !result.has_more;
      if (activeId) {
        list.querySelectorAll("[data-conversation-id]").forEach((button) => {
          const selected = Number(button.dataset.conversationId) === activeId;
          button.classList.toggle("is-active", selected);
          button.setAttribute("aria-pressed", String(selected));
        });
      }
      if (!activeId && result.conversations.length) await loadMessages(result.conversations[0].id);
    } finally {
      if (more && sequence === listRequestSequence) more.disabled = false;
    }
  }

  function updateControls() {
    if (!activeConversation) {
      controls.hidden = true;
      return;
    }
    controls.hidden = false;
    const bot = controls.querySelector("[data-toggle-bot]");
    const close = controls.querySelector("[data-close-conversation]");
    bot.textContent = activeConversation.bot_enabled ? "Desativar automação" : "Ativar automação";
    close.textContent = activeConversation.status === "open" ? "Encerrar conversa" : "Reabrir conversa";
    renderConversationDetail();
  }

  async function loadMessages(id, { quiet = false } = {}) {
    const sequence = ++requestSequence;
    if (!quiet) messagesEl.innerHTML = '<div class="admin-empty">Carregando conversa...</div>';
    const response = await adminFetch(`/api/admin/conversations/${id}/messages`);
    const result = await response.json();
    if (sequence !== requestSequence) return;
    if (!response.ok || !result.ok) throw new Error(formatApiError(result));
    activeId = Number(id);
    activeConversation = result.conversation;
    list.querySelectorAll("[data-conversation-id]").forEach((button) => {
      const selected = Number(button.dataset.conversationId) === activeId;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    headerEl.innerHTML = `<strong>${escapeHtml(activeConversation.name || activeConversation.phone)}</strong><span>${escapeHtml(activeConversation.phone)} · ${escapeHtml(labelKind(activeConversation.kind))} · ${escapeHtml(labelStatus(activeConversation.status))}</span>`;
    updateControls();
    if (!result.messages.length) {
      messagesEl.innerHTML = '<div class="admin-empty">Nenhuma mensagem nessa conversa.</div>';
      return;
    }
    messagesEl.innerHTML = result.messages.map((message) => {
      const text = message.text ? `<p>${escapeHtml(message.text).replaceAll("\n", "<br>")}</p>` : "";
      const fallback = !text && !message.media_url ? `<p>${escapeHtml(message.media_name || message.message_type || "Mensagem")}</p>` : "";
      return `<article class="chat-bubble ${message.direction === "out" ? "is-out" : "is-in"}">${text || fallback}${renderMedia(message)}<time datetime="${escapeHtml(message.created_at || "")}">${escapeHtml(localDate(message.created_at))} · ${escapeHtml(message.status || "registrada")}</time></article>`;
    }).join("");
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-conversation-id]");
    if (!button) return;
    loadMessages(button.dataset.conversationId).catch((error) => {
      messagesEl.innerHTML = `<div class="admin-empty">${escapeHtml(error.message)}</div>`;
    });
  });

  more?.addEventListener("click", () => loadConversationList().catch((error) => { note.textContent = error.message; }));
  search?.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadConversationList({ reset: true }).catch((error) => { note.textContent = error.message; }), 250);
  });
  filterButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeFilter = button.dataset.chatFilter || "all";
      filterButtons.forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
      applyConversationFilter();
    });
  });

  app.querySelectorAll("[data-quick-reply]").forEach((button) => {
    button.addEventListener("click", () => {
      const textarea = form?.querySelector("textarea");
      if (!textarea) return;
      const value = button.dataset.quickReply || "";
      textarea.value = textarea.value.trim() ? `${textarea.value.trim()}\n\n${value}` : value;
      textarea.focus();
    });
  });

  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!activeId) {
      note.textContent = "Selecione uma conversa antes de enviar.";
      return;
    }
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    form.setAttribute("aria-busy", "true");
    note.textContent = "Enviando...";
    try {
      const response = await adminFetch(`/api/admin/conversations/${activeId}/messages`, { method: "POST", body: new FormData(form) });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(formatApiError(result));
      form.reset();
      fileLabel.textContent = "Anexar";
      note.textContent = result.status === "dry_run" ? "Registrado em modo teste." : "Mensagem enviada.";
      await loadMessages(activeId);
    } catch (error) {
      note.textContent = error.message || "Erro ao enviar.";
    } finally {
      form.removeAttribute("aria-busy");
      submit.disabled = false;
    }
  });

  fileInput?.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    if (!file) {
      fileLabel.textContent = "Anexar";
      return;
    }
    if (file.size > maxUploadBytes) {
      note.textContent = `O arquivo ultrapassa ${Math.floor(maxUploadBytes / 1024 / 1024)} MB.`;
      fileInput.value = "";
      fileLabel.textContent = "Anexar";
      return;
    }
    fileLabel.textContent = `${file.name} (${Math.ceil(file.size / 1024)} KB)`;
  });

  controls?.querySelector("[data-toggle-bot]")?.addEventListener("click", async () => {
    const response = await adminFetch(`/api/admin/conversations/${activeId}/controls`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bot_enabled: !activeConversation.bot_enabled }) });
    const result = await response.json();
    if (response.ok) {
      activeConversation = { ...activeConversation, ...result.conversation };
      updateControls();
    } else {
      note.textContent = formatApiError(result);
    }
  });
  controls?.querySelector("[data-close-conversation]")?.addEventListener("click", async () => {
    const status = activeConversation.status === "open" ? "closed" : "open";
    const response = await adminFetch(`/api/admin/conversations/${activeId}/controls`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) });
    const result = await response.json();
    if (response.ok) {
      activeConversation = { ...activeConversation, ...result.conversation };
      updateControls();
    } else {
      note.textContent = formatApiError(result);
    }
  });

  app.querySelectorAll("[data-lead-status]").forEach((select) => {
    select.dataset.previousStatus = select.value;
    select.addEventListener("change", async () => {
      const response = await adminFetch(`/api/admin/leads/${select.dataset.leadStatus}/status`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: select.value }) });
      if (response.ok) {
        select.dataset.previousStatus = select.value;
      } else {
        select.value = select.dataset.previousStatus;
        note.textContent = "Não foi possível atualizar o contato.";
      }
    });
  });
  app.querySelectorAll("[data-retry-outbox]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const response = await adminFetch(`/api/admin/outbox/${button.dataset.retryOutbox}/retry`, { method: "POST" });
        const result = await response.json();
        note.textContent = response.ok ? "Nova tentativa concluída." : formatApiError(result);
      } catch {
        note.textContent = "Não foi possível realizar a nova tentativa.";
      } finally {
        button.disabled = false;
      }
    });
  });

  const privacyForm = app.querySelector("[data-privacy-delete]");
  privacyForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const phone = new FormData(privacyForm).get("phone");
    const privacyNote = privacyForm.querySelector("[data-privacy-note]");
    if (!window.confirm(`Excluir permanentemente os dados vinculados a ${phone}?`)) return;
    const submit = privacyForm.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const response = await adminFetch("/api/admin/privacy/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ phone }) });
      const result = await response.json();
      privacyNote.textContent = response.ok ? `Exclusão concluída: ${result.deleted.leads} contato(s) e ${result.deleted.messages} mensagem(ns).` : formatApiError(result);
      if (response.ok) {
        privacyForm.reset();
        activeId = null;
        activeConversation = null;
        headerEl.innerHTML = "<strong>Selecione uma conversa</strong><span>As mensagens aparecerão aqui.</span>";
        messagesEl.innerHTML = '<div class="admin-empty">Nenhuma conversa selecionada.</div>';
        updateControls();
        renderConversationDetail();
        await loadConversationList({ reset: true });
      }
    } catch {
      privacyNote.textContent = "Não foi possível concluir a exclusão.";
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

  loadConversationList({ reset: true }).catch((error) => { note.textContent = error.message; });
  window.setInterval(() => {
    if (!document.hidden && activeId) loadMessages(activeId, { quiet: true }).catch(() => {});
  }, 15000);
  window.setInterval(() => {
    if (!document.hidden && !search?.value.trim()) loadConversationList({ reset: true }).catch(() => {});
  }, 30000);
}

initAdminChat();
