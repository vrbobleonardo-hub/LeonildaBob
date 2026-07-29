import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";

const PORT = Number(process.env.WHATSAPP_QR_BRIDGE_PORT || 3333);
const HOST = process.env.WHATSAPP_QR_BRIDGE_HOST || "127.0.0.1";
const BRIDGE_TOKEN = process.env.WHATSAPP_QR_BRIDGE_TOKEN || "";
const INBOUND_URL = process.env.WHATSAPP_QR_INBOUND_URL || `http://127.0.0.1:${process.env.PORT || 8000}/api/webhooks/whatsapp/qr`;
const AUTH_DIR = path.resolve(process.env.WHATSAPP_QR_AUTH_DIR || ".whatsapp-qr-auth/default");
const QR_EXPIRES_MS = 55_000;
const MEDIA_MAX_BYTES = Number(process.env.WHATSAPP_QR_MEDIA_MAX_BYTES || 15 * 1024 * 1024);
const SYNC_RECENT_HISTORY = process.env.WHATSAPP_QR_SYNC_RECENT_HISTORY !== "0";
const HISTORY_LOOKBACK_MS = Number(process.env.WHATSAPP_QR_HISTORY_LOOKBACK_MS || 36 * 60 * 60 * 1000);

const runtime = {
  socket: null,
  downloadMediaMessage: null,
  baileysLogger: null,
  starting: null,
  reconnectTimer: null,
  reconnectAttempts: 0,
  status: "disconnected",
  qr: null,
  qrUpdatedAt: null,
  qrExpiresAt: null,
  phoneWaId: null,
  displayName: null,
  lastConnectedAt: null,
  lastDisconnectedAt: null,
  lastUpsertAt: null,
  lastHistoryAt: null,
  lastInboundAt: null,
  lastOutboundAt: null,
  lastSelfMessageAt: null,
  lastError: null,
  events: [],
  startedAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
};

const lidToPhoneJid = new Map();
const phoneToLidJid = new Map();
const pendingPlaceholderTimers = new Map();
let fatalRecovering = false;

function nowIso() {
  return new Date().toISOString();
}

function addMsIso(ms) {
  return new Date(Date.now() + ms).toISOString();
}

function cleanText(value) {
  return String(value ?? "").trim();
}

function logEvent(event, details = {}) {
  const item = { at: nowIso(), event, details };
  runtime.events = [...runtime.events.slice(-17), item];
  console.log(`[WA_QR_BRIDGE] ${event} ${JSON.stringify(details)}`);
}

function normalizePhone(value) {
  const digits = cleanText(value).replace(/\D/g, "");
  if (!digits) return "";
  if (digits.startsWith("55") && (digits.length === 12 || digits.length === 13)) return digits;
  if (digits.length === 10 || digits.length === 11) return `55${digits}`;
  return digits;
}

function jidUser(value) {
  return cleanText(value).split("@")[0].split(":")[0].split("_")[0];
}

function isLidJid(value) {
  return cleanText(value).toLowerCase().endsWith("@lid");
}

function isPhoneJid(value) {
  const jid = cleanText(value).toLowerCase();
  return jid.endsWith("@s.whatsapp.net") || jid.endsWith("@c.us");
}

function normalizePhoneJid(value) {
  const raw = cleanText(value).toLowerCase();
  if (isPhoneJid(raw)) {
    const phone = normalizePhone(jidUser(raw));
    return phone ? `${phone}@s.whatsapp.net` : "";
  }
  const phone = normalizePhone(raw);
  return phone ? `${phone}@s.whatsapp.net` : "";
}

function normalizeLidJid(value) {
  const raw = cleanText(value).toLowerCase();
  if (isLidJid(raw)) return `${jidUser(raw)}@lid`;
  return "";
}

function phoneFromJid(value) {
  if (!isPhoneJid(value)) return "";
  return normalizePhone(jidUser(value));
}

function rememberLidMapping(lidValue, phoneValue, source = "unknown") {
  const lidJid = normalizeLidJid(lidValue);
  const phoneJid = normalizePhoneJid(phoneValue);
  if (!lidJid || !phoneJid) return false;
  const previous = lidToPhoneJid.get(lidJid);
  lidToPhoneJid.set(lidJid, phoneJid);
  phoneToLidJid.set(phoneJid, lidJid);
  if (previous !== phoneJid) {
    logEvent("lid_phone_mapped", {
      source,
      lid_tail: jidUser(lidJid).slice(-4),
      phone_tail: phoneFromJid(phoneJid).slice(-4),
    });
  }
  return true;
}

function rememberContactIdentity(contact, source = "contact") {
  if (!contact || typeof contact !== "object") return;
  const id = cleanText(contact.id);
  const lid = cleanText(contact.lid || contact.lidJid);
  const jid = cleanText(contact.jid);
  if (isPhoneJid(id) && lid) rememberLidMapping(lid, id, source);
  if (isLidJid(id) && jid) rememberLidMapping(id, jid, source);
  if (lid && jid) rememberLidMapping(lid, jid, source);
}

function normalizeRecipientJid(value) {
  const raw = cleanText(value);
  if (!raw) return "";
  if (raw.includes("@")) {
    const jid = raw.toLowerCase();
    if (
      jid.endsWith("@s.whatsapp.net") ||
      jid.endsWith("@lid") ||
      jid.endsWith("@c.us") ||
      jid.endsWith("@g.us")
    ) {
      return jid;
    }
  }
  const phone = normalizePhone(raw);
  return phone ? `${phone}@s.whatsapp.net` : "";
}

function resolveMessageIdentity(message) {
  const remoteJid = cleanText(message?.key?.remoteJid).toLowerCase();
  const senderPn = normalizePhoneJid(message?.key?.senderPn);
  const participantPn = normalizePhoneJid(message?.key?.participantPn);
  const participant = normalizePhoneJid(message?.key?.participant);
  const senderLid = normalizeLidJid(message?.key?.senderLid);
  const participantLid = normalizeLidJid(message?.key?.participantLid);
  const remoteLid = normalizeLidJid(remoteJid);
  const lidJid = remoteLid || senderLid || participantLid || "";
  const directPhoneJid = normalizePhoneJid(remoteJid) || senderPn || participantPn || participant || "";

  if (lidJid && directPhoneJid) {
    rememberLidMapping(lidJid, directPhoneJid, "message_key");
  }

  const resolvedPhoneJid = directPhoneJid || (lidJid ? lidToPhoneJid.get(lidJid) || "" : "");
  const phone = phoneFromJid(resolvedPhoneJid);
  return {
    remoteJid,
    lidJid,
    phoneJid: resolvedPhoneJid || "",
    phone,
    contactIdentifier: phone || (lidJid ? `lid:${jidUser(lidJid)}` : normalizePhone(jidUser(remoteJid))),
    identifierKind: phone ? "phone" : lidJid ? "lid_unresolved" : "unknown",
    senderPn,
    participantPn,
    senderLid,
    participantLid,
  };
}

function patchRuntime(patch) {
  Object.assign(runtime, patch, { updatedAt: nowIso() });
}

function messageTimestampMs(message) {
  const raw = message?.messageTimestamp;
  if (!raw) return 0;
  const value =
    typeof raw === "number"
      ? raw
      : typeof raw === "bigint"
        ? Number(raw)
        : typeof raw?.toNumber === "function"
          ? raw.toNumber()
          : Number(raw);
  if (!Number.isFinite(value) || value <= 0) return 0;
  return value > 10_000_000_000 ? value : value * 1000;
}

function isRecentEnough(message) {
  const timestamp = messageTimestampMs(message);
  if (!timestamp) return true;
  return Date.now() - timestamp <= HISTORY_LOOKBACK_MS;
}

async function recoverAfterFatal(error, source = "runtime") {
  if (fatalRecovering) return;
  fatalRecovering = true;
  const reason = cleanText(error?.message || error) || "fatal_bridge_error";
  logEvent("fatal_recover", { source, error: reason.slice(0, 240) });
  patchRuntime({ status: "failed", lastError: reason.slice(0, 240), lastDisconnectedAt: nowIso() });
  try {
    await closeSocket(false);
  } catch {
    // best effort
  }
  runtime.socket = null;
  fatalRecovering = false;
  scheduleReconnect();
}

process.on("uncaughtException", (error) => {
  void recoverAfterFatal(error, "uncaught_exception");
});

process.on("unhandledRejection", (error) => {
  void recoverAfterFatal(error, "unhandled_rejection");
});

function clearReconnectTimer() {
  if (runtime.reconnectTimer) {
    clearTimeout(runtime.reconnectTimer);
    runtime.reconnectTimer = null;
  }
}

async function closeSocket(logout = false) {
  const socket = runtime.socket;
  if (!socket) return;
  logEvent("socket_close", { logout });
  try {
    if (logout && typeof socket.logout === "function") {
      await socket.logout();
      return;
    }
    if (typeof socket.end === "function") socket.end();
  } catch {
    try {
      socket?.ws?.close?.();
    } catch {
      // best effort
    }
  }
}

async function authCredentialsExist() {
  try {
    await fs.access(path.join(AUTH_DIR, "creds.json"));
    return true;
  } catch {
    return false;
  }
}

async function importBaileys() {
  process.env.WS_NO_BUFFER_UTIL = process.env.WS_NO_BUFFER_UTIL || "1";
  process.env.WS_NO_UTF_8_VALIDATE = process.env.WS_NO_UTF_8_VALIDATE || "1";
  const baileys = await import("@whiskeysockets/baileys");
  const pinoModule = await import("pino");
  const makeWASocket = baileys.default || baileys.makeWASocket;
  const loggerFactory = pinoModule.default || pinoModule;
  return {
    makeWASocket,
    useMultiFileAuthState: baileys.useMultiFileAuthState,
    fetchLatestBaileysVersion: baileys.fetchLatestBaileysVersion,
    DisconnectReason: baileys.DisconnectReason || {},
    downloadMediaMessage: baileys.downloadMediaMessage,
    logger: loggerFactory({ level: "silent" }),
  };
}

function getMessagePayload(message) {
  return (
    message?.message?.ephemeralMessage?.message ||
    message?.message?.viewOnceMessage?.message ||
    message?.message?.documentWithCaptionMessage?.message ||
    message?.message ||
    {}
  );
}

function pendingPlaceholderKey(message) {
  const remoteJid = cleanText(message?.key?.remoteJid);
  const id = cleanText(message?.key?.id);
  if (!remoteJid || !id) return "";
  return `${remoteJid}:${id}`;
}

function clearPendingPlaceholder(message) {
  const key = pendingPlaceholderKey(message);
  if (!key) return;
  const timer = pendingPlaceholderTimers.get(key);
  if (!timer) return;
  clearTimeout(timer);
  pendingPlaceholderTimers.delete(key);
  logEvent("placeholder_resolved", {
    remote_tail: jidUser(message?.key?.remoteJid).slice(-4),
    id_tail: cleanText(message?.key?.id).slice(-6),
  });
}

function extractInboundText(message) {
  const payload = getMessagePayload(message);
  const payloadKeys = Object.keys(payload || {});

  if (payload.conversation) return { type: "text", content: cleanText(payload.conversation) };
  if (payload.extendedTextMessage?.text) {
    return { type: "text", content: cleanText(payload.extendedTextMessage.text) };
  }
  if (payload.imageMessage) return { type: "image", content: cleanText(payload.imageMessage.caption) || "[Imagem]" };
  if (payload.videoMessage) return { type: "video", content: cleanText(payload.videoMessage.caption) || "[Video]" };
  if (payload.audioMessage) {
    return {
      type: "audio",
      content: "[Audio]",
      media: {
        kind: "audio",
        mime_type: cleanText(payload.audioMessage.mimetype) || "audio/ogg",
        seconds: payload.audioMessage.seconds || null,
        ptt: Boolean(payload.audioMessage.ptt),
        file_length: cleanText(payload.audioMessage.fileLength) || null,
      },
    };
  }
  if (payload.documentMessage) {
    return { type: "document", content: cleanText(payload.documentMessage.fileName) || "[Documento]" };
  }
  if (payload.buttonsResponseMessage) {
    return {
      type: "interactive",
      content:
        cleanText(payload.buttonsResponseMessage.selectedDisplayText) ||
        cleanText(payload.buttonsResponseMessage.selectedButtonId) ||
        "[Botao]",
    };
  }
  if (payload.listResponseMessage) {
    return {
      type: "interactive",
      content:
        cleanText(payload.listResponseMessage.title) ||
        cleanText(payload.listResponseMessage.singleSelectReply?.selectedRowId) ||
        "[Lista]",
    };
  }
  if (payload.placeholderMessage) {
    return {
      type: "unavailable",
      content: "Mensagem recebida, mas não foi possível exibir o conteúdo neste painel. Confira no WhatsApp conectado.",
      unavailable_reason: "placeholder_message",
      payload_keys: payloadKeys,
      placeholder_keys: Object.keys(payload.placeholderMessage || {}),
    };
  }
  const technicalKeys = new Set([
    "messageContextInfo",
    "protocolMessage",
    "reactionMessage",
    "senderKeyDistributionMessage",
    "pollUpdateMessage",
    "keepInChatMessage",
    "editedMessage",
  ]);
  const hasOnlyTechnicalKeys = payloadKeys.length > 0 && payloadKeys.every((key) => technicalKeys.has(key));
  if (payloadKeys.length === 0 || hasOnlyTechnicalKeys) {
    return { type: "ignored", content: "", ignore_reason: "technical_or_empty_payload", payload_keys: payloadKeys };
  }
  return { type: "ignored", content: "", ignore_reason: "unsupported_payload", payload_keys: payloadKeys };
}

async function downloadInboundMedia(message, type, media = {}) {
  if (type !== "audio") return media;
  if (typeof runtime.downloadMediaMessage !== "function") {
    return { ...media, download_status: "unavailable" };
  }
  try {
    const buffer = await runtime.downloadMediaMessage(
      message,
      "buffer",
      {},
      {
        logger: runtime.baileysLogger,
        reuploadRequest: runtime.socket?.updateMediaMessage?.bind(runtime.socket),
      }
    );
    if (!Buffer.isBuffer(buffer) || !buffer.length) {
      return { ...media, download_status: "empty" };
    }
    if (buffer.length > MEDIA_MAX_BYTES) {
      return { ...media, download_status: "too_large", size_bytes: buffer.length, max_bytes: MEDIA_MAX_BYTES };
    }
    return {
      ...media,
      download_status: "ok",
      size_bytes: buffer.length,
      data_base64: buffer.toString("base64"),
    };
  } catch (error) {
    logEvent("media_download_failed", { type, error: cleanText(error?.message || error) });
    return { ...media, download_status: "error", error: cleanText(error?.message || error).slice(0, 240) };
  }
}

async function postInboundToBobAdv(message, source = "whatsapp_qr_bridge", options = {}) {
  const identity = resolveMessageIdentity(message);
  const remoteJid = identity.remoteJid;
  if (!remoteJid) {
    logEvent("message_ignored", { reason: "missing_remote_jid" });
    return;
  }
  if (remoteJid.endsWith("@g.us")) {
    logEvent("message_ignored", { reason: "group_chat", remote_jid: remoteJid });
    return;
  }
  if (remoteJid === "status@broadcast") {
    logEvent("message_ignored", { reason: "status_broadcast" });
    return;
  }

  const extracted = options.extracted || extractInboundText(message);
  const { type, content, ignore_reason, payload_keys, placeholder_keys } = extracted;
  if (type === "ignored") {
    logEvent("message_ignored", { reason: ignore_reason || "ignored_payload", remote_jid: remoteJid, payload_keys });
    return;
  }
  if (type === "unavailable" && !options.deliverUnavailable) {
    const timerKey = pendingPlaceholderKey(message);
    if (timerKey && !pendingPlaceholderTimers.has(timerKey)) {
      if (typeof runtime.socket?.requestPlaceholderResend === "function") {
        runtime.socket
          .requestPlaceholderResend(message.key)
          .then((requestId) => {
            logEvent("placeholder_resend_requested", {
              remote_jid: remoteJid,
              id_tail: cleanText(message?.key?.id).slice(-6),
              request_id_tail: cleanText(requestId).slice(-6),
              placeholder_keys,
            });
          })
          .catch((error) => {
            logEvent("placeholder_resend_failed", {
              remote_jid: remoteJid,
              error: cleanText(error?.message || error).slice(0, 240),
              placeholder_keys,
            });
          });
      }
      const timer = setTimeout(() => {
        pendingPlaceholderTimers.delete(timerKey);
        void postInboundToBobAdv(message, source, {
          extracted,
          deliverUnavailable: true,
          sendReply: false,
          externalIdSuffix: ":conteudo-indisponivel",
        });
      }, 18_000);
      pendingPlaceholderTimers.set(timerKey, timer);
      logEvent("placeholder_waiting_for_content", {
        remote_jid: remoteJid,
        id_tail: cleanText(message?.key?.id).slice(-6),
        payload_keys,
        placeholder_keys,
      });
    }
    return;
  }
  if (!identity.contactIdentifier || !content) {
    logEvent("message_ignored", { reason: "empty_phone_or_content", remote_jid: remoteJid, type, payload_keys });
    return;
  }
  const media = await downloadInboundMedia(message, type, extracted.media || {});
  patchRuntime({ lastInboundAt: nowIso() });
  logEvent("inbound_received", {
    phone_tail: identity.phone ? identity.phone.slice(-4) : null,
    identifier_kind: identity.identifierKind,
    lid_tail: identity.lidJid ? jidUser(identity.lidJid).slice(-4) : null,
    type,
    media_status: media?.download_status || null,
    media_size: media?.size_bytes || null,
  });

  try {
    const response = await fetch(INBOUND_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(BRIDGE_TOKEN ? { "X-QR-Bridge-Token": BRIDGE_TOKEN } : {}),
      },
      body: JSON.stringify({
        phone: identity.contactIdentifier,
        text: content,
        name: cleanText(message?.pushName) || null,
        external_id: `${cleanText(message?.key?.id) || `${identity.contactIdentifier}:${Date.now()}`}${options.externalIdSuffix || ""}`,
        phone_number_id: "qr:default",
        message_type: type,
        send_reply: options.sendReply ?? true,
        raw: {
          source,
          remote_jid: remoteJid,
          lid_jid: identity.lidJid,
          phone_jid: identity.phoneJid,
          phone_resolved: Boolean(identity.phone),
          identifier_kind: identity.identifierKind,
          sender_pn: identity.senderPn,
          participant_pn: identity.participantPn,
          sender_lid: identity.senderLid,
          participant_lid: identity.participantLid,
          message_timestamp: message?.messageTimestamp || null,
          unavailable_reason: extracted.unavailable_reason || null,
          payload_keys: extracted.payload_keys || null,
          placeholder_keys: extracted.placeholder_keys || null,
          media,
        },
      }),
    });
    if (!response.ok) {
      const errorText = await response.text().catch(() => "");
      logEvent("inbound_callback_failed", { status: response.status, error: errorText.slice(0, 240) });
      patchRuntime({ lastError: `inbound_callback_failed: ${response.status}` });
      return;
    }
    logEvent("inbound_callback_ok", {
      phone_tail: identity.phone ? identity.phone.slice(-4) : null,
      identifier_kind: identity.identifierKind,
      status: response.status,
    });
  } catch (error) {
    logEvent("inbound_callback_failed", { error: cleanText(error?.message || error) });
    patchRuntime({ lastError: `inbound_callback_failed: ${cleanText(error?.message || error)}` });
  }
}

async function processInboundMessage(message, source = "live") {
  if (message?.key?.fromMe) {
    patchRuntime({ lastSelfMessageAt: nowIso() });
    logEvent("message_ignored", { reason: "from_me", id: cleanText(message?.key?.id) });
    return;
  }
  const extracted = extractInboundText(message);
  if (extracted.type !== "unavailable") clearPendingPlaceholder(message);
  await postInboundToBobAdv(message, source, { extracted });
}

function scheduleReconnect() {
  clearReconnectTimer();
  runtime.reconnectAttempts += 1;
  const delayMs = Math.min(120_000, 5_000 * Math.pow(2, Math.min(6, runtime.reconnectAttempts - 1)));
  runtime.reconnectTimer = setTimeout(() => {
    if (runtime.status === "connected" || runtime.starting) return;
    void startSession().catch((error) => {
      patchRuntime({ status: "failed", lastError: cleanText(error?.message || error) || "qr_reconnect_failed" });
    });
  }, delayMs);
}

async function startSession(options = {}) {
  const force = Boolean(options.force);
  if (force) {
    clearReconnectTimer();
    await closeSocket(false);
    runtime.socket = null;
    patchRuntime({ status: "disconnected", qr: null, qrExpiresAt: null, lastError: null });
    logEvent("session_force_restart", {});
  }

  if (!force && runtime.status === "connected" && runtime.socket) return;
  if (runtime.starting) {
    await runtime.starting;
    return;
  }
  if (!force && runtime.socket && runtime.status === "qr") return;

  runtime.starting = (async () => {
    await fs.mkdir(AUTH_DIR, { recursive: true });
    patchRuntime({ status: "connecting", lastError: null });
    logEvent("session_starting", { auth_available: await authCredentialsExist() });

    const { makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason, downloadMediaMessage, logger } =
      await importBaileys();
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const versionResult = await fetchLatestBaileysVersion().catch(() => ({ version: undefined }));

    await closeSocket(false);
    const socket = makeWASocket({
      auth: state,
      browser: ["LeonildaBob", "Chrome", "1.0.0"],
      logger,
      markOnlineOnConnect: false,
      printQRInTerminal: false,
      syncFullHistory: SYNC_RECENT_HISTORY,
      version: versionResult?.version,
    });

    runtime.socket = socket;
    runtime.downloadMediaMessage = downloadMediaMessage;
    runtime.baileysLogger = logger;
    socket.ev.on("creds.update", saveCreds);
    socket.ev.on("messages.upsert", async (event) => {
      const rows = Array.isArray(event?.messages) ? event.messages : [];
      if (rows.length) {
        patchRuntime({ lastUpsertAt: nowIso() });
        logEvent("messages_upsert", { count: rows.length, type: cleanText(event?.type) || null });
      }
      for (const message of rows) {
        await processInboundMessage(message, "whatsapp_qr_bridge");
      }
    });

    socket.ev.on("contacts.upsert", (contacts) => {
      for (const contact of contacts || []) rememberContactIdentity(contact, "contacts_upsert");
    });

    socket.ev.on("contacts.update", (contacts) => {
      for (const contact of contacts || []) rememberContactIdentity(contact, "contacts_update");
    });

    socket.ev.on("chats.phoneNumberShare", ({ lid, jid }) => {
      rememberLidMapping(lid, jid, "phone_number_share");
    });

    socket.ev.on("messaging-history.set", async (event) => {
      const rows = Array.isArray(event?.messages) ? event.messages : [];
      for (const contact of event?.contacts || []) rememberContactIdentity(contact, "history_contacts");
      if (!rows.length) return;
      patchRuntime({ lastHistoryAt: nowIso() });
      const recent = rows.filter((message) => isRecentEnough(message));
      logEvent("history_set", {
        count: rows.length,
        recent: recent.length,
        sync_type: event?.syncType ?? null,
        progress: event?.progress ?? null,
      });
      for (const message of recent) {
        await processInboundMessage(message, "whatsapp_qr_history");
      }
    });

    socket.ev.on("connection.update", async (update) => {
      if (update?.qr) {
        logEvent("qr_generated", { expires_at: addMsIso(QR_EXPIRES_MS) });
        patchRuntime({
          status: "qr",
          qr: update.qr,
          qrUpdatedAt: nowIso(),
          qrExpiresAt: addMsIso(QR_EXPIRES_MS),
          lastError: null,
        });
      }

      if (update?.connection === "open") {
        clearReconnectTimer();
        const socketUserId = cleanText(socket?.user?.id).split(":")[0].split("@")[0];
        logEvent("connection_open", { phone_tail: normalizePhone(socketUserId).slice(-4) });
        patchRuntime({
          status: "connected",
          qr: null,
          qrExpiresAt: null,
          phoneWaId: normalizePhone(socketUserId) || null,
          displayName: cleanText(socket?.user?.name) || cleanText(socket?.user?.verifiedName) || null,
          lastConnectedAt: nowIso(),
          lastError: null,
          reconnectAttempts: 0,
        });
      }

      if (update?.connection === "close") {
        const statusCode = Number(update?.lastDisconnect?.error?.output?.statusCode || 0);
        const loggedOut = statusCode === DisconnectReason.loggedOut;
        logEvent("connection_close", {
          status_code: statusCode,
          logged_out: loggedOut,
          error: cleanText(update?.lastDisconnect?.error?.message),
        });
        patchRuntime({
          status: loggedOut ? "disconnected" : "failed",
          qr: null,
          qrExpiresAt: null,
          lastDisconnectedAt: nowIso(),
          lastError: loggedOut
            ? null
            : cleanText(update?.lastDisconnect?.error?.message) || "connection_closed_reconnecting",
        });
        if (loggedOut) {
          clearReconnectTimer();
          runtime.socket = null;
          await fs.rm(AUTH_DIR, { recursive: true, force: true }).catch(() => undefined);
        } else {
          scheduleReconnect();
        }
      }
    });
  })();

  try {
    await runtime.starting;
  } catch (error) {
    patchRuntime({ status: "failed", lastError: cleanText(error?.message || error) || "qr_start_failed" });
    throw error;
  } finally {
    runtime.starting = null;
  }
}

async function disconnectSession() {
  clearReconnectTimer();
  await closeSocket(true);
  runtime.socket = null;
  await fs.rm(AUTH_DIR, { recursive: true, force: true }).catch(() => undefined);
  patchRuntime({
    status: "disconnected",
    qr: null,
    qrExpiresAt: null,
    phoneWaId: null,
    displayName: null,
    lastDisconnectedAt: nowIso(),
    lastError: null,
  });
  logEvent("session_disconnected", {});
}

async function resetSession() {
  clearReconnectTimer();
  await closeSocket(false);
  runtime.socket = null;
  await fs.rm(AUTH_DIR, { recursive: true, force: true }).catch(() => undefined);
  patchRuntime({
    status: "disconnected",
    qr: null,
    qrExpiresAt: null,
    phoneWaId: null,
    displayName: null,
    lastDisconnectedAt: nowIso(),
    lastError: null,
    reconnectAttempts: 0,
  });
  logEvent("session_reset", {});
  await startSession({ force: true });
}

async function sendText(to, text) {
  const recipientJid = normalizeRecipientJid(to);
  const body = cleanText(text);
  if (!recipientJid) throw new Error("qr_recipient_required");
  if (!body) throw new Error("qr_text_required");

  if (!runtime.socket || runtime.status !== "connected") {
    const hasAuth = await authCredentialsExist();
    if (!hasAuth) throw new Error("qr_session_not_connected");
    await startSession();
  }
  if (!runtime.socket || runtime.status !== "connected") throw new Error("qr_session_not_connected");

  logEvent("message_send_attempt", { jid_type: recipientJid.split("@").pop(), recipient_tail: recipientJid.slice(-12) });
  const result = await runtime.socket.sendMessage(recipientJid, { text: body });
  patchRuntime({ lastOutboundAt: nowIso() });
  logEvent("message_sent", { jid_type: recipientJid.split("@").pop(), recipient_tail: recipientJid.slice(-12) });
  return { provider_message_id: cleanText(result?.key?.id) || null, recipient_jid: recipientJid };
}

async function publicStatus() {
  const qrExpired =
    runtime.status === "qr" &&
    runtime.qrExpiresAt &&
    Number.isFinite(Date.parse(runtime.qrExpiresAt)) &&
    Date.parse(runtime.qrExpiresAt) <= Date.now();
  if (qrExpired) {
    patchRuntime({
      status: "disconnected",
      qr: null,
      qrExpiresAt: null,
      lastError: "qr_expired_generate_again",
    });
  }

  let qrDataUrl = null;
  if (runtime.qr) {
    try {
      const qrcode = await import("qrcode");
      qrDataUrl = await qrcode.toDataURL(runtime.qr, {
        margin: 1,
        width: 280,
        color: { dark: "#111b21", light: "#ffffff" },
      });
    } catch {
      qrDataUrl = null;
    }
  }

  return {
    enabled: true,
    session: {
      id: "default",
      label: "WhatsApp QR Leonilda Bob",
      status: runtime.status,
      phone_wa_id: runtime.phoneWaId,
      display_name: runtime.displayName,
      qr_expires_at: runtime.qrExpiresAt,
      last_connected_at: runtime.lastConnectedAt,
      last_disconnected_at: runtime.lastDisconnectedAt,
      last_upsert_at: runtime.lastUpsertAt,
      last_history_at: runtime.lastHistoryAt,
      last_inbound_at: runtime.lastInboundAt,
      last_outbound_at: runtime.lastOutboundAt,
      last_self_message_at: runtime.lastSelfMessageAt,
      last_error: runtime.lastError,
      runtime_connected: runtime.status === "connected" && Boolean(runtime.socket),
      runtime_starting: Boolean(runtime.starting),
      auth_available: await authCredentialsExist(),
      can_send: runtime.status === "connected" && Boolean(runtime.socket),
      needs_scan: runtime.status === "qr" || runtime.status === "disconnected",
      qr_data_url: qrDataUrl,
      diagnostics: {
        inbound_url: INBOUND_URL,
        auth_available: await authCredentialsExist(),
        sync_recent_history: SYNC_RECENT_HISTORY,
        history_lookback_ms: HISTORY_LOOKBACK_MS,
        reconnect_attempts: runtime.reconnectAttempts,
        socket_present: Boolean(runtime.socket),
        updated_at: runtime.updatedAt,
        events: runtime.events.slice(-8),
      },
      updated_at: runtime.updatedAt,
    },
  };
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
      if (body.length > 1_000_000) {
        req.destroy();
        reject(new Error("body_too_large"));
      }
    });
    req.on("end", () => {
      if (!body) return resolve({});
      try {
        resolve(JSON.parse(body));
      } catch {
        reject(new Error("invalid_json"));
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res, statusCode, payload) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(JSON.stringify(payload));
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url || "/", `http://${HOST}:${PORT}`);
    if (req.method === "GET" && url.pathname === "/status") {
      return sendJson(res, 200, await publicStatus());
    }
    if (req.method === "POST" && url.pathname === "/connect") {
      const body = await readJsonBody(req);
      await startSession({ force: Boolean(body.force) });
      return sendJson(res, 200, { ok: true, ...(await publicStatus()) });
    }
    if (req.method === "POST" && url.pathname === "/disconnect") {
      await disconnectSession();
      return sendJson(res, 200, { ok: true, ...(await publicStatus()) });
    }
    if (req.method === "POST" && url.pathname === "/reset") {
      await resetSession();
      return sendJson(res, 200, { ok: true, ...(await publicStatus()) });
    }
    if (req.method === "POST" && url.pathname === "/send") {
      const body = await readJsonBody(req);
      const result = await sendText(body.to || body.phone, body.text);
      return sendJson(res, 200, { ok: true, sent: true, status: "sent", ...result });
    }
    return sendJson(res, 404, { error: "not_found" });
  } catch (error) {
    return sendJson(res, 500, {
      ok: false,
      error: cleanText(error?.message || error) || "unexpected_error",
      ...(await publicStatus().catch(() => ({ enabled: true, session: null }))),
    });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[WHATSAPP_QR_BRIDGE] listening on ${HOST}:${PORT}`);
});
