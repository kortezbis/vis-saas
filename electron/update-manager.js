"use strict";

const http = require("http");
const https = require("https");
const { URL } = require("url");
const { app, shell } = require("electron");

const DEFAULT_WEBSITE_URL = "https://www.viszmo.com";
const DEFAULT_RELEASE_PAGE = "https://github.com/kortezbis/vis-saas/releases";
const MANIFEST_TIMEOUT_MS = 5000;
const MAX_MANIFEST_BYTES = 512 * 1024;

let dashboardWindowProvider = () => null;
let log = () => {};
let updater = null;
let initialized = false;
let checkPromise = null;
let currentCheckIsManual = false;
let websiteManifest = null;

let status = {
  state: "idle",
  currentVersion: app.getVersion(),
  version: null,
  percent: 0,
  message: "",
  releaseNotes: "",
  required: false,
  source: null,
  downloadUrl: null,
  manual: false,
  websiteManifest: null,
};

function websiteOrigin() {
  return (process.env.VISZMO_WEBSITE_URL || DEFAULT_WEBSITE_URL).replace(/\/$/, "");
}

function manifestUrl() {
  return process.env.VISZMO_UPDATE_MANIFEST_URL || `${websiteOrigin()}/updates/viszmo.json`;
}

function safeExternalUrl(value, fallback = DEFAULT_RELEASE_PAGE) {
  try {
    const parsed = new URL(value || fallback);
    if (parsed.protocol === "https:" || parsed.protocol === "http:") {
      return parsed.toString();
    }
  } catch (_error) {
    // Fall back to the known release page below.
  }
  return fallback;
}

function compareVersions(left, right) {
  const parse = (value) => {
    const normalized = String(value || "0.0.0").trim().replace(/^v/i, "");
    const [corePart, prePart = ""] = normalized.split("+", 1)[0].split("-", 2);
    const core = corePart.split(".").map((part) => {
      const parsed = Number.parseInt(part, 10);
      return Number.isFinite(parsed) ? parsed : 0;
    });
    while (core.length < 3) core.push(0);
    return { core, pre: prePart };
  };

  const a = parse(left);
  const b = parse(right);
  for (let index = 0; index < Math.max(a.core.length, b.core.length); index += 1) {
    const result = (a.core[index] || 0) - (b.core[index] || 0);
    if (result !== 0) return result > 0 ? 1 : -1;
  }
  if (!a.pre && !b.pre) return 0;
  if (!a.pre) return 1;
  if (!b.pre) return -1;
  return a.pre.localeCompare(b.pre, undefined, { numeric: true });
}

function releaseNotesText(value) {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  return value
    .map((entry) => {
      if (!entry || typeof entry !== "object") return "";
      const version = entry.version ? `v${entry.version}: ` : "";
      return `${version}${entry.note || ""}`.trim();
    })
    .filter(Boolean)
    .join("\n");
}

function sanitizeRelease(value) {
  if (!value || typeof value !== "object") return null;
  const latestVersion = String(value.latestVersion || value.version || "").trim();
  if (!latestVersion) return null;
  const minimumVersion = String(value.minimumVersion || "").trim();
  return {
    latestVersion,
    minimumVersion,
    message: String(value.message || "").trim(),
    releaseNotesUrl: value.releaseNotesUrl ? safeExternalUrl(value.releaseNotesUrl) : null,
    downloadUrl: value.downloadUrl ? safeExternalUrl(value.downloadUrl) : null,
    required: Boolean(value.required),
  };
}

function sanitizeManifest(value) {
  if (!value || typeof value !== "object") return null;
  const web = sanitizeRelease(value.web);
  const desktop = sanitizeRelease(value.desktop);
  if (!web && !desktop) return null;
  return {
    schemaVersion: Number(value.schemaVersion || 1),
    generatedAt: String(value.generatedAt || "").trim(),
    web,
    desktop,
  };
}

function publicStatus() {
  return {
    ...status,
    currentVersion: app.getVersion(),
    websiteManifest,
  };
}

function emit(next) {
  status = {
    ...status,
    ...next,
    currentVersion: app.getVersion(),
    websiteManifest,
  };
  const window = dashboardWindowProvider();
  if (window && !window.isDestroyed()) {
    window.webContents.send("update-status", publicStatus());
  }
  return publicStatus();
}

function requestJson(target) {
  return new Promise((resolve, reject) => {
    let parsed;
    try {
      parsed = new URL(target);
    } catch (error) {
      reject(error);
      return;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      reject(new Error("Update manifest must use HTTP or HTTPS."));
      return;
    }
    const transport = parsed.protocol === "https:" ? https : http;
    const request = transport.get(parsed, (response) => {
      if (response.statusCode < 200 || response.statusCode >= 300) {
        response.resume();
        reject(new Error(`HTTP ${response.statusCode} from update manifest`));
        return;
      }
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
        if (body.length > MAX_MANIFEST_BYTES) {
          request.destroy(new Error("Update manifest is too large."));
        }
      });
      response.on("end", () => {
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.setTimeout(MANIFEST_TIMEOUT_MS, () => {
      request.destroy(new Error("Update manifest request timed out."));
    });
    request.on("error", reject);
  });
}

async function refreshWebsiteManifest() {
  try {
    const nextManifest = sanitizeManifest(await requestJson(manifestUrl()));
    if (!nextManifest) {
      throw new Error("Update manifest did not include a web or desktop release.");
    }
    websiteManifest = nextManifest;

    const release = nextManifest.desktop;
    const newerDesktopRelease = release && compareVersions(release.latestVersion, app.getVersion()) > 0;
    if (
      newerDesktopRelease &&
      (!updater || ["idle", "current", "disabled", "error"].includes(status.state))
    ) {
      const required = release.required || (
        release.minimumVersion && compareVersions(release.minimumVersion, app.getVersion()) > 0
      );
      emit({
        state: "available",
        version: release.latestVersion,
        message: release.message || `Viszmo ${release.latestVersion} is available.`,
        releaseNotes: release.message,
        required,
        source: "website-manifest",
        downloadUrl: release.downloadUrl || release.releaseNotesUrl || DEFAULT_RELEASE_PAGE,
        manual: false,
      });
    } else {
      emit({ websiteManifest: websiteManifest });
    }
    return websiteManifest;
  } catch (error) {
    log(`Update manifest check skipped: ${error.message || error}`);
    return null;
  }
}

function updaterLogger() {
  return {
    info: (...args) => log(args.join(" ")),
    warn: (...args) => log(`WARN: ${args.join(" ")}`),
    error: (...args) => log(`ERROR: ${args.join(" ")}`),
    debug: (...args) => log(`DEBUG: ${args.join(" ")}`),
  };
}

function configureUpdater() {
  if (!app.isPackaged && process.env.VISZMO_ENABLE_DEV_UPDATES !== "1") {
    emit({
      state: "disabled",
      message: "Updates are checked after the desktop app is installed.",
      source: null,
      manual: false,
    });
    return false;
  }

  try {
    updater = require("electron-updater").autoUpdater;
    updater.autoDownload = false;
    updater.autoInstallOnAppQuit = false;
    updater.allowPrerelease = false;
    updater.logger = updaterLogger();

    const configuredFeed = String(process.env.VISZMO_UPDATE_URL || "").trim();
    if (configuredFeed) {
      updater.setFeedURL({ provider: "generic", url: configuredFeed, channel: "latest" });
    }

    updater.on("checking-for-update", () => {
      emit({ state: "checking", message: "Checking for updates…", manual: currentCheckIsManual });
    });
    updater.on("update-available", (info) => {
      const release = websiteManifest && websiteManifest.desktop;
      const version = String(info && info.version ? info.version : "").trim();
      const required = Boolean(
        release && release.minimumVersion && compareVersions(release.minimumVersion, app.getVersion()) > 0,
      );
      emit({
        state: "available",
        version: version || (release && release.latestVersion) || null,
        message: `Viszmo ${version || "new"} is available. Download it when you are ready.`,
        releaseNotes: releaseNotesText(info && info.releaseNotes) || (release && release.message) || "",
        required,
        source: "electron-updater",
        downloadUrl: release && (release.downloadUrl || release.releaseNotesUrl),
        manual: currentCheckIsManual,
      });
    });
    updater.on("update-not-available", () => {
      emit({
        state: "current",
        version: null,
        percent: 0,
        message: "Viszmo is up to date.",
        source: null,
        manual: currentCheckIsManual,
      });
    });
    updater.on("download-progress", (progress) => {
      emit({
        state: "downloading",
        percent: Number(progress && progress.percent ? progress.percent : 0),
        message: `Downloading update… ${Math.round(Number(progress && progress.percent ? progress.percent : 0))}%`,
        manual: currentCheckIsManual,
      });
    });
    updater.on("update-downloaded", (info) => {
      const version = String(info && info.version ? info.version : status.version || "").trim();
      emit({
        state: "ready",
        version: version || status.version,
        percent: 100,
        message: `Viszmo ${version || "update"} is ready. Restart to install it.`,
        source: "electron-updater",
        manual: true,
      });
    });
    updater.on("update-cancelled", () => {
      emit({
        state: "available",
        message: `Viszmo ${status.version || "update"} is still available when you are ready.`,
        manual: true,
      });
    });
    updater.on("error", (error) => {
      log(`Desktop update error: ${error.stack || error}`);
      emit({
        state: "error",
        message: "We could not check for updates right now. You can try again later.",
        manual: currentCheckIsManual,
      });
    });
    return true;
  } catch (error) {
    log(`Desktop updater unavailable: ${error.stack || error}`);
    emit({
      state: "error",
      message: "Desktop updates are not configured for this build.",
      manual: false,
    });
    return false;
  }
}

async function checkForUpdates(manual = true) {
  if (checkPromise) return checkPromise;
  currentCheckIsManual = Boolean(manual);
  if (!updater) {
    await refreshWebsiteManifest();
    if (status.state !== "available") {
      emit({
        state: app.isPackaged ? "error" : "disabled",
        message: app.isPackaged
          ? "Desktop updates are not configured for this build."
          : "Updates are checked after the desktop app is installed.",
        manual: currentCheckIsManual,
      });
    }
    return publicStatus();
  }

  emit({ state: "checking", message: "Checking for updates…", manual: currentCheckIsManual });
  checkPromise = updater.checkForUpdates()
    .then(async (result) => {
      await refreshWebsiteManifest();
      return result;
    })
    .catch(async (error) => {
      log(`Desktop update check failed: ${error.stack || error}`);
      await refreshWebsiteManifest();
      if (status.state !== "available") {
        emit({
          state: "error",
          message: "We could not check for updates right now. You can try again later.",
          manual: currentCheckIsManual,
        });
      }
      return null;
    })
    .finally(() => {
      checkPromise = null;
    });
  await checkPromise;
  return publicStatus();
}

async function downloadUpdate() {
  if (status.source === "website-manifest" || !updater) {
    return openUpdatePage();
  }
  if (status.state !== "available") return publicStatus();
  emit({ state: "downloading", percent: 0, message: "Starting update download…", manual: true });
  try {
    await updater.downloadUpdate();
  } catch (error) {
    log(`Desktop update download failed: ${error.stack || error}`);
    emit({
      state: "error",
      message: "The update could not be downloaded. Please try again.",
      manual: true,
    });
  }
  return publicStatus();
}

function installUpdate() {
  if (!updater || status.state !== "ready") return publicStatus();
  updater.quitAndInstall();
  return publicStatus();
}

async function openUpdatePage() {
  const release = websiteManifest && websiteManifest.desktop;
  const target = safeExternalUrl(
    status.downloadUrl || (release && (release.downloadUrl || release.releaseNotesUrl)) || DEFAULT_RELEASE_PAGE,
  );
  await shell.openExternal(target);
  return { ...publicStatus(), openedUrl: target };
}

function initialize(options = {}) {
  if (initialized) return;
  initialized = true;
  dashboardWindowProvider = options.getDashboardWindow || dashboardWindowProvider;
  log = options.log || log;
  configureUpdater();

  void refreshWebsiteManifest();
  if (updater) {
    setTimeout(() => { void checkForUpdates(false); }, 2500);
  }
}

function getStatus() {
  return publicStatus();
}

module.exports = {
  initialize,
  getStatus,
  checkForUpdates,
  downloadUpdate,
  installUpdate,
  openUpdatePage,
  compareVersions,
};
