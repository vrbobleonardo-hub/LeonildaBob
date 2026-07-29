import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";

// Render fornece PORT ao serviço; localmente mantemos 3333 como padrão.
const PORT = Number(process.env.WHATSAPP_QR_BRIDGE_PORT || process.env.PORT || 3333);
const HOST = process.env.WHATSAPP_QR_BRIDGE_HOST || "127.0.0.1";
const AUTH_DIR = path.resolve(process.env.WHATSAPP_QR_AUTH_DIR || ".whatsapp-qr-auth/default");
const INBOUND_URL = String(process.env.WHATSAPP_QR_INBOUND_URL || "").replace(/\/$/, "");
const BRIDGE_TOKEN = String(process.env.WHATSAPP_QR_BRIDGE_TOKEN || "");
const QR_EXPIRES_MS = 55_000;

const runtime = {
  socket: null,
  starting: null,
  reconnectTimer: null,
  status: "disconnected",
  qr: null,
  qrExpiresAt: null,
  phoneWaId: null,
  displayName: null,
  lastConnectedAt: null,
  lastDisconnectedAt: null,
  lastInboundAt: null,
  lastOutboundAt: null,
  lastError: null,
  updatedAt: new Date().toISOString(),
};

function now() {
  return new Date().toISOString();
}

function patch(values) {
  Object.assign(runtime, values, { updatedAt: now() });
}

function text(value) {
  return String(value ?? "").trim();
}

function normalizePhone(value) {
  const digits = text(value).replace(/\D/g, "");
  if (digits.length === 10 || digits.length === 11) return `55${digits}`;
  return digits;
}

function recipientJid(value) {
  const digits = normalizePhone(value);
  return /^55\d{10,11}$/.test(digits) ? `${digits}@s.whatsapp.net` : "";
}

function clientAllowed(req) {
  if (!BRIDGE_TOKEN) return true;
  return req.headers["x-qr-bridge-token"] === BRIDGE_TOKEN;
}

async function authExists() {
  try {
    await fs.access(path.join(AUTH_DIR, "creds.json"));
    return true;
  } catch {
    return false;
  }
}

async function importWhatsApp() {
  process.env.WS_NO_BUFFER_UTIL ||= "1";
  process.env.WS_NO_UTF_8_VALIDATE ||= "1";
  const baileys = await import("@whiskeysockets/baileys");
  const pinoModule = await import("pino");
  const loggerFactory = pinoModule.default || pinoModule;
  return {
    makeWASocket: baileys.default || baileys.makeWASocket,
    useMultiFileAuthState: baileys.useMultiFileAuthState,
    fetchLatestBaileysVersion: baileys.fetchLatestBaileysVersion,
    Browsers: baileys.Browsers || null,
    DisconnectReason: baileys.DisconnectReason || {},
    logger: loggerFactory({ level: "silent" }),
  };
}

function messageText(message) {
  const payload = message?.message?.ephemeralMessage?.message || message?.message || {};
  if (payload.conversation) return text(payload.conversation);
  if (payload.extendedTextMessage?.text) return text(payload.extendedTextMessage.text);
  if (payload.imageMessage) return text(payload.imageMessage.caption) || "[Imagem]";
  if (payload.videoMessage) return text(payload.videoMessage.caption) || "[Vídeo]";
  if (payload.audioMessage) return "[Áudio]";
  if (payload.documentMessage) return text(payload.documentMessage.fileName) || "[Documento]";
  return "";
}

async function postInbound(message) {
  if (message?.key?.fromMe || !INBOUND_URL) return;
  const jid = text(message?.key?.remoteJid);
  if (!jid || jid.endsWith("@g.us") || jid === "status@broadcast") return;
  const phone = normalizePhone(jid.split("@")[0].split(":")[0]);
  const body = messageText(message);
  if (!/^55\d{10,11}$/.test(phone) || !body) return;
  const response = await fetch(INBOUND_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(BRIDGE_TOKEN ? { "X-QR-Bridge-Token": BRIDGE_TOKEN } : {}),
    },
    body: JSON.stringify({
      phone,
      name: text(message?.pushName) || null,
      text: body,
      provider_message_id: text(message?.key?.id) || null,
      message_type: "text",
      raw: { source: "whatsapp_qr_bridge", remote_jid: jid },
    }),
  });
  if (!response.ok) throw new Error(`inbound_${response.status}`);
  patch({ lastInboundAt: now() });
}

function clearReconnect() {
  if (runtime.reconnectTimer) clearTimeout(runtime.reconnectTimer);
  runtime.reconnectTimer = null;
}

async function stopSocket(logout = false) {
  const socket = runtime.socket;
  if (!socket) return;
  try {
    if (logout) await socket.logout();
    else socket.end?.();
  } catch {
    socket.ws?.close?.();
  }
}

function scheduleReconnect() {
  if (runtime.reconnectTimer) return;
  runtime.reconnectTimer = setTimeout(() => {
    runtime.reconnectTimer = null;
    startSession().catch((error) => patch({ status: "failed", lastError: text(error?.message || error) }));
  }, 4_000);
}

async function startSession({ force = false } = {}) {
  if (runtime.starting) return runtime.starting;
  if (!force && runtime.socket && runtime.status === "connected") return;
  runtime.starting = (async () => {
    if (force) {
      await fs.rm(AUTH_DIR, { recursive: true, force: true });
    }
    await fs.mkdir(AUTH_DIR, { recursive: true });
    const { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, Browsers, DisconnectReason, logger } = await importWhatsApp();
    const latest = await fetchLatestBaileysVersion().catch(() => ({}));
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    await stopSocket(false);
    patch({ status: "connecting", lastError: null });
    const browserConfig = Browsers?.macOS?.("Desktop") || Browsers?.ubuntu?.("Chrome") || ["Mac OS", "Desktop", "14.4.1"];
    const socket = makeWASocket({
      auth: state,
      browser: browserConfig,
      logger,
      version: latest.version,
      markOnlineOnConnect: false,
      printQRInTerminal: false,
    });
    runtime.socket = socket;
    socket.ev.on("creds.update", saveCreds);
    socket.ev.on("messages.upsert", (event) => {
      for (const message of event?.messages || []) {
        postInbound(message).catch((error) => patch({ lastError: text(error?.message || error).slice(0, 240) }));
      }
    });
    socket.ev.on("connection.update", (update) => {
      if (update?.qr) {
        patch({ status: "qr", qr: update.qr, qrExpiresAt: new Date(Date.now() + QR_EXPIRES_MS).toISOString(), lastError: null });
      }
      if (update?.connection === "open") {
        clearReconnect();
        const userId = text(socket?.user?.id).split(":")[0].split("@")[0];
        patch({ status: "connected", qr: null, qrExpiresAt: null, phoneWaId: normalizePhone(userId) || null, displayName: text(socket?.user?.name) || null, lastConnectedAt: now(), lastError: null });
      }
      if (update?.connection === "close") {
        const code = Number(update?.lastDisconnect?.error?.output?.statusCode || 0);
        const loggedOut = code === DisconnectReason.loggedOut;
        patch({ status: loggedOut ? "disconnected" : "failed", qr: null, qrExpiresAt: null, lastDisconnectedAt: now(), lastError: loggedOut ? null : text(update?.lastDisconnect?.error?.message || "connection_closed") });
        runtime.socket = null;
        if (!loggedOut) scheduleReconnect();
      }
    });
  })();
  try {
    await runtime.starting;
  } finally {
    runtime.starting = null;
  }
}

async function disconnectSession() {
  clearReconnect();
  await stopSocket(true);
  runtime.socket = null;
  await fs.rm(AUTH_DIR, { recursive: true, force: true });
  patch({ status: "disconnected", qr: null, qrExpiresAt: null, phoneWaId: null, displayName: null, lastDisconnectedAt: now(), lastError: null });
}

async function status() {
  const expired = runtime.status === "qr" && runtime.qrExpiresAt && Date.parse(runtime.qrExpiresAt) <= Date.now();
  if (expired) patch({ status: "disconnected", qr: null, qrExpiresAt: null, lastError: "qr_expired_generate_again" });
  let qrDataUrl = null;
  if (runtime.qr) {
    const qrcode = await import("qrcode");
    qrDataUrl = await qrcode.toDataURL(runtime.qr, { margin: 1, width: 280, color: { dark: "#10130f", light: "#ffffff" } });
  }
  return {
    enabled: true,
    session: {
      status: runtime.status,
      phone_wa_id: runtime.phoneWaId,
      display_name: runtime.displayName,
      qr_expires_at: runtime.qrExpiresAt,
      last_connected_at: runtime.lastConnectedAt,
      last_disconnected_at: runtime.lastDisconnectedAt,
      last_inbound_at: runtime.lastInboundAt,
      last_outbound_at: runtime.lastOutboundAt,
      last_error: runtime.lastError,
      runtime_connected: runtime.status === "connected" && Boolean(runtime.socket),
      auth_available: await authExists(),
      can_send: runtime.status === "connected" && Boolean(runtime.socket),
      needs_scan: runtime.status === "qr" || runtime.status === "disconnected",
      qr_data_url: qrDataUrl,
      updated_at: runtime.updatedAt,
    },
  };
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 100_000) {
        req.destroy();
        reject(new Error("body_too_large"));
      }
    });
    req.on("end", () => {
      try { resolve(raw ? JSON.parse(raw) : {}); } catch { reject(new Error("invalid_json")); }
    });
    req.on("error", reject);
  });
}

function json(res, code, payload) {
  res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(JSON.stringify(payload));
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url || "/", `http://${HOST}:${PORT}`);
    if (req.method === "GET" && url.pathname === "/healthz") {
      return json(res, 200, { ok: true, service: "whatsapp_qr_bridge" });
    }
    if (!clientAllowed(req)) return json(res, 401, { ok: false, error: "unauthorized" });
    if (req.method === "GET" && url.pathname === "/status") return json(res, 200, await status());
    if (req.method !== "POST") return json(res, 404, { ok: false, error: "not_found" });
    const body = await readBody(req);
    if (url.pathname === "/connect") {
      await startSession({ force: Boolean(body.force) });
      return json(res, 200, { ok: true, ...(await status()) });
    }
    if (url.pathname === "/disconnect") {
      await disconnectSession();
      return json(res, 200, { ok: true, ...(await status()) });
    }
    if (url.pathname === "/reset") {
      await disconnectSession();
      await startSession({ force: true });
      return json(res, 200, { ok: true, ...(await status()) });
    }
    if (url.pathname === "/send") {
      const jid = recipientJid(body.to);
      const bodyText = text(body.text);
      if (!jid || !bodyText) throw new Error("recipient_or_text_invalid");
      if (runtime.status !== "connected" || !runtime.socket) throw new Error("qr_session_not_connected");
      const sent = await runtime.socket.sendMessage(jid, { text: bodyText });
      patch({ lastOutboundAt: now() });
      return json(res, 200, { ok: true, sent: true, status: "sent", provider_message_id: text(sent?.key?.id) || null });
    }
    return json(res, 404, { ok: false, error: "not_found" });
  } catch (error) {
    return json(res, 500, { ok: false, error: text(error?.message || error) || "unexpected_error", ...(await status().catch(() => ({}))) });
  }
});

server.listen(PORT, HOST, () => console.log(`[WHATSAPP_QR_BRIDGE] listening on ${HOST}:${PORT}`));
