"use strict";

const fs = require("fs");
const path = require("path");
const { app, safeStorage } = require("electron");

function sessionPath() {
  return path.join(app.getPath("userData"), "auth-session.json");
}

function publicSession(session) {
  if (!session || !session.access_token) {
    return { signedIn: false, email: "" };
  }
  return { signedIn: true, email: session.email || "" };
}

function accessToken() {
  return loadSession()?.access_token || "";
}

function saveSession(session) {
  const payload = JSON.stringify({
    access_token: session.access_token,
    refresh_token: session.refresh_token || "",
    email: session.email || "",
    savedAt: Date.now(),
  });
  const encrypted = safeStorage.isEncryptionAvailable()
    ? safeStorage.encryptString(payload).toString("base64")
    : Buffer.from(payload, "utf8").toString("base64");
  fs.writeFileSync(sessionPath(), JSON.stringify({ v: 1, data: encrypted }), "utf8");
}

function loadSession() {
  try {
    const raw = JSON.parse(fs.readFileSync(sessionPath(), "utf8"));
    const buffer = Buffer.from(String(raw.data || ""), "base64");
    const payload = safeStorage.isEncryptionAvailable()
      ? safeStorage.decryptString(buffer)
      : buffer.toString("utf8");
    const session = JSON.parse(payload);
    return session && session.access_token ? session : null;
  } catch (_error) {
    return null;
  }
}

function clearSession() {
  try {
    fs.unlinkSync(sessionPath());
  } catch (_error) {
    // Already signed out.
  }
}

function parseAuthUrl(url) {
  const parsed = new URL(url);
    const params = new URLSearchParams(parsed.search.replace(/^\?/, "") || (parsed.hash.startsWith("#") ? parsed.hash.slice(1) : parsed.hash));
  const accessToken = params.get("access_token") || "";
  if (!accessToken) {
    return null;
  }
  return {
    access_token: accessToken,
    refresh_token: params.get("refresh_token") || "",
    email: params.get("email") || "",
  };
}

module.exports = {
  accessToken,
  clearSession,
  loadSession,
  parseAuthUrl,
  publicSession,
  saveSession,
};
