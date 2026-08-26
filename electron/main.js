"use strict";

const { app, BrowserWindow, ipcMain, shell, screen } = require("electron");
const http = require("http");
const https = require("https");
const net = require("net");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const authStore = require("./auth-store");
const updateManager = require("./update-manager");

const PROJECT_ROOT = path.resolve(__dirname, "..");
const BACKEND_HOST = "127.0.0.1";
const PROTOCOL = "viszmo";
const DEFAULT_WEBSITE_URL = "https://www.viszmo.com";
const APP_ICON = path.join(PROJECT_ROOT, "assets", "viszmo-icon.png");

let backendPort = null;
let backendUrl = "";
let backendProcess = null;
let dashboardWindow = null;
let splashWindow = null;
let splashShownAt = 0;
const SPLASH_MIN_MS = 3200;
const SPLASH_FADE_MS = 300;

function createSplash() {
  splashWindow = new BrowserWindow({
    width: 320,
    height: 400,
    frame: false,
    transparent: true,
    resizable: false,
    movable: true,
    center: true,
    show: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    backgroundColor: "#00000000",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  splashWindow.setMenuBarVisibility(false);
  splashWindow.on("closed", () => { splashWindow = null; });
  splashShownAt = Date.now();
  splashWindow.loadFile(path.join(__dirname, "splash.html"));
}

async function closeSplash() {
  if (!splashWindow || splashWindow.isDestroyed()) {
    return;
  }
  const remaining = SPLASH_MIN_MS - (Date.now() - splashShownAt);
  if (remaining > 0) {
    await new Promise((resolve) => setTimeout(resolve, remaining));
  }
  if (!splashWindow || splashWindow.isDestroyed()) {
    return;
  }
  try {
    await splashWindow.webContents.executeJavaScript(
      "document.getElementById('splash')?.classList.add('is-exiting')",
    );
    await new Promise((resolve) => setTimeout(resolve, SPLASH_FADE_MS));
  } catch (_error) {
    // Close even if the fade-out script does not run.
  }
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.destroy();
  }
  splashWindow = null;
}

function loadProjectEnv() {
  const envPath = path.join(PROJECT_ROOT, ".env");
  if (!fs.existsSync(envPath)) {
    return;
  }
  for (const line of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) {
      continue;
    }
    const index = trimmed.indexOf("=");
    if (index < 0) {
      continue;
    }
    const key = trimmed.slice(0, index).trim();
    let value = trimmed.slice(index + 1).trim();
    if (
      (value.startsWith("\"") && value.endsWith("\"")) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (process.env[key] == null) {
      process.env[key] = value;
    }
  }
}

function websiteAuthUrl() {
  const origin = (process.env.VISZMO_WEBSITE_URL || DEFAULT_WEBSITE_URL).replace(/\/$/, "");
  return `${origin}/desktop-auth`;
}

function websitePricingUrl(plan) {
  const configuredOrigin = process.env.VISZMO_WEBSITE_URL || DEFAULT_WEBSITE_URL;
  const origin = configuredOrigin.endsWith("/") ? configuredOrigin.slice(0, -1) : configuredOrigin;
  const selectedPlan = String(plan || "").trim().toLowerCase();
  if (!selectedPlan || selectedPlan === "all") {
    return `${origin}/pricing`;
  }
  const product = ["study", "homework", "bundle"].includes(selectedPlan) ? selectedPlan : "homework";
  return `${origin}/pricing?product=${product}`;
}

function writeStartupLog(message) {
  try {
    const logRoot = app.isReady()
      ? app.getPath("logs")
      : path.join(process.env.TEMP || process.cwd(), "Viszmo");
    fs.mkdirSync(logRoot, { recursive: true });
    fs.appendFileSync(
      path.join(logRoot, "startup.log"),
      `[${new Date().toISOString()}] ${message}\n`,
      "utf8",
    );
  } catch (_error) {
    // Startup diagnostics must never prevent the app from launching.
  }
}

function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, BACKEND_HOST, () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function getJson(url, options = {}) {
  return new Promise((resolve, reject) => {
    const transport = url.startsWith("https:") ? https : http;
    const request = transport.get(url, { headers: options.headers || {} }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        if (response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error(`HTTP ${response.statusCode} from ${url}`));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.setTimeout(3000, () => request.destroy(new Error("Request timed out")));
    request.on("error", reject);
  });
}

async function waitForHealth(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      await getJson(`${backendUrl}/health`);
      return;
    } catch (_error) {
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
  throw new Error("Viszmo's local agent service did not start.");
}

function backendCommand() {
  if (app.isPackaged) {
    const executable = process.platform === "win32" ? "viszmo-backend.exe" : "viszmo-backend";
    const bundled = path.join(process.resourcesPath, "backend", executable);
    if (!fs.existsSync(bundled)) {
      throw new Error(`Bundled backend is missing: ${bundled}`);
    }
    return { command: bundled, args: [], cwd: process.resourcesPath };
  }

  const python = process.env.VISZMO_PYTHON || (process.platform === "win32" ? "python" : "python3");
  return {
    command: python,
    args: [path.join(PROJECT_ROOT, "backend_server.py"), "--host", BACKEND_HOST, "--port", String(backendPort)],
    cwd: PROJECT_ROOT,
  };
}

async function startBackend() {
  const target = backendCommand();
  writeStartupLog(`Starting backend: ${target.command}`);
  const environment = {
    ...process.env,
    VISZMO_HOST: BACKEND_HOST,
    VISZMO_PORT: String(backendPort),
    VISZMO_PANEL_URL: backendUrl,
  };
  backendProcess = spawn(target.command, target.args, {
    cwd: target.cwd,
    env: environment,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  backendProcess.stdout.on("data", (data) => console.log(`[backend] ${String(data).trimEnd()}`));
  backendProcess.stderr.on("data", (data) => console.error(`[backend] ${String(data).trimEnd()}`));
  backendProcess.on("error", (error) => {
    console.error("Viszmo backend process error:", error);
    writeStartupLog(`Backend process error: ${error.stack || error}`);
  });
  backendProcess.on("exit", (code, signal) => {
    writeStartupLog(`Backend exited: code=${code}, signal=${signal}`);
  });
  await waitForHealth();
}

function createDashboard() {
  dashboardWindow = new BrowserWindow({
    width: 960,
    height: 720,
    minWidth: 720,
    minHeight: 560,
    show: false,
    resizable: true,
    autoHideMenuBar: true,
    icon: APP_ICON,
    backgroundColor: "#131314",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  dashboardWindow.once("ready-to-show", async () => {
    await closeSplash();
    if (dashboardWindow && !dashboardWindow.isDestroyed()) {
      dashboardWindow.show();
    }
  });
  dashboardWindow.on("closed", () => { dashboardWindow = null; });
  dashboardWindow.loadURL(`${backendUrl}/dashboard`);
}

function focusDashboard() {
  if (!dashboardWindow || dashboardWindow.isDestroyed()) {
    return;
  }
  if (dashboardWindow.isMinimized()) {
    dashboardWindow.restore();
  }
  dashboardWindow.show();
  dashboardWindow.focus();
}

function notifyAuthChanged() {
  if (dashboardWindow && !dashboardWindow.isDestroyed()) {
    dashboardWindow.webContents.send("auth-changed", authStore.publicSession(authStore.loadSession()));
  }
}

function handleAuthCallback(url) {
  const session = authStore.parseAuthUrl(url);
  if (!session) {
    writeStartupLog("Ignored auth callback because tokens were missing.");
    return;
  }
  authStore.saveSession(session);
  notifyAuthChanged();
  focusDashboard();
}

function findProtocolUrl(argv) {
  return (argv || []).find((arg) => typeof arg === "string" && arg.startsWith(`${PROTOCOL}://`));
}

function registerProtocolClient() {
  if (process.defaultApp) {
    if (process.argv.length >= 2) {
      app.setAsDefaultProtocolClient(PROTOCOL, process.execPath, [path.resolve(process.argv[1])]);
      return;
    }
  }
  app.setAsDefaultProtocolClient(PROTOCOL);
}

ipcMain.handle("hide-workspace", () => {
  if (dashboardWindow && !dashboardWindow.isDestroyed()) dashboardWindow.hide();
});

ipcMain.handle("relaunch-app", () => {
  // Let the renderer finish its click state before replacing the process.
  setTimeout(() => {
    stopBackend();
    app.relaunch();
    app.exit(0);
  }, 100);
  return { ok: true };
});

ipcMain.handle("get-auth", () => authStore.publicSession(authStore.loadSession()));
ipcMain.handle("get-entitlements", async () => {
  const session = authStore.loadSession();
  if (!session || !session.access_token) {
    return { authenticated: false, available: false, entitlements: {} };
  }
  try {
    const payload = await getJson(websiteOrigin() + "/api/entitlements", {
      headers: { Authorization: "Bearer " + session.access_token },
    });
    return { authenticated: true, available: true, ...payload };
  } catch (error) {
    writeStartupLog("Entitlement lookup failed: " + (error && error.message ? error.message : error));
    return { authenticated: true, available: false, entitlements: {} };
  }
});
ipcMain.handle("get-desktop-usage", async () => {
  const session = authStore.loadSession();
  if (!session || !session.access_token) {
    return { authenticated: false, available: false, allowed: false, remaining: 0 };
  }
  try {
    const payload = await getJson(websiteOrigin() + "/api/desktop-usage", {
      headers: { Authorization: "Bearer " + session.access_token },
    });
    return { authenticated: true, available: true, ...payload };
  } catch (error) {
    writeStartupLog("Desktop usage lookup failed: " + (error && error.message ? error.message : error));
    return { authenticated: true, available: false, allowed: false, remaining: 0 };
  }
});
ipcMain.handle("get-controller-auth", () => ({ accessToken: authStore.accessToken() }));
ipcMain.handle("start-auth", async () => {
  await shell.openExternal(websiteAuthUrl());
  return { ok: true };
});

ipcMain.handle("open-pricing", async (_event, plan) => {
  await shell.openExternal(websitePricingUrl(String(plan || "")));
  return { ok: true };
});

ipcMain.handle("sign-out", () => {
  authStore.clearSession();
  notifyAuthChanged();
  return authStore.publicSession(null);
});

ipcMain.handle("get-update-status", () => updateManager.getStatus());

ipcMain.handle("check-for-updates", () => updateManager.checkForUpdates(true));

ipcMain.handle("download-update", () => updateManager.downloadUpdate());

ipcMain.handle("install-update", () => updateManager.installUpdate());

ipcMain.handle("open-update-page", () => updateManager.openUpdatePage());

// ---- Desktop status widget ----
let widgetWindow = null;
let widgetEnabled = true;
// Internal hides (browser closed) must not flip the user's saved setting;
// only an explicit × click or settings toggle may do that.
let widgetSuppressNotify = false;
const WIDGET_WIDTH = 268;
const WIDGET_HEIGHT = 84;

function widgetBoundsPath() {
  return path.join(app.getPath("userData"), "widget-bounds.json");
}

function createWidget() {
  if (widgetWindow && !widgetWindow.isDestroyed()) return;
  let bounds = null;
  try {
    bounds = JSON.parse(fs.readFileSync(widgetBoundsPath(), "utf8"));
  } catch (error) { /* first run: default position */ }
  const area = screen.getPrimaryDisplay().workArea;
  const x = bounds && Number.isFinite(bounds.x) ? bounds.x : area.x + area.width - WIDGET_WIDTH - 24;
  const y = bounds && Number.isFinite(bounds.y) ? bounds.y : area.y + area.height - WIDGET_HEIGHT - 24;
  widgetWindow = new BrowserWindow({
    width: WIDGET_WIDTH,
    height: WIDGET_HEIGHT,
    x, y,
    frame: false,
    transparent: true,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "widget-preload.js"),
    },
  });
  widgetWindow.setAlwaysOnTop(true, "screen-saver");
  widgetWindow.loadURL(`${backendUrl}/widget`);
  widgetWindow.once("ready-to-show", () => widgetWindow.showInactive());
  const saveBounds = () => {
    if (!widgetWindow || widgetWindow.isDestroyed()) return;
    try {
      fs.writeFileSync(widgetBoundsPath(), JSON.stringify(widgetWindow.getBounds()));
    } catch (error) { /* bounds persistence is best-effort */ }
  };
  widgetWindow.on("moved", saveBounds);
  widgetWindow.on("closed", () => {
    widgetWindow = null;
    if (!widgetSuppressNotify && dashboardWindow && !dashboardWindow.isDestroyed()) {
      dashboardWindow.webContents.send("widget-visibility-changed", false);
    }
    widgetSuppressNotify = false;
  });
}

function closeWidgetWindow(notify) {
  widgetSuppressNotify = !notify;
  if (widgetWindow && !widgetWindow.isDestroyed()) widgetWindow.close();
  else widgetWindow = null;
}

async function pollWidget() {
  if (!backendUrl) return;
  let attached = false;
  try {
    const response = await fetch(`${backendUrl}/live-status`);
    if (response.ok) attached = Boolean((await response.json()).attached);
  } catch (error) {
    attached = false; // backend down: the widget has nothing to show anyway
  }
  if (widgetEnabled && attached) {
    if (!widgetWindow) createWidget();
  } else if (widgetWindow) {
    closeWidgetWindow(false);
  }
}

ipcMain.handle("set-widget", (_event, enabled) => {
  widgetEnabled = Boolean(enabled);
  if (widgetEnabled) {
    pollWidget();
  } else {
    closeWidgetWindow(true);
  }
  return { ok: true };
});

ipcMain.handle("close-widget", () => {
  // The widget's × is an explicit opt-out: the setting flips with it.
  widgetEnabled = false;
  closeWidgetWindow(true);
  return { ok: true };
});

setInterval(pollWidget, 2000);

function stopBackend() {
  if (backendProcess && !backendProcess.killed) {
    backendProcess.kill();
    backendProcess = null;
  }
}

loadProjectEnv();
registerProtocolClient();
// The status widget chimes on completion without any user gesture in that
// window, so autoplay restrictions must be lifted for it to be audible.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    const url = findProtocolUrl(argv);
    if (url) {
      handleAuthCallback(url);
    }
    focusDashboard();
  });
  app.on("open-url", (event, url) => {
    event.preventDefault();
    handleAuthCallback(url);
  });
  app.on("before-quit", stopBackend);
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  app.on("activate", () => {
    if (dashboardWindow && !dashboardWindow.isDestroyed()) dashboardWindow.show();
  });

  (async () => {
    try {
      backendPort = await findFreePort();
      backendUrl = `http://${BACKEND_HOST}:${backendPort}`;
      await app.whenReady();
      if (process.platform === "win32") {
        app.setAppUserModelId("com.viszmo.desktop");
      }
      createSplash();
      const launchUrl = findProtocolUrl(process.argv);
      if (launchUrl) {
        handleAuthCallback(launchUrl);
      }
      await startBackend();
      createDashboard();
      updateManager.initialize({
        getDashboardWindow: () => dashboardWindow,
        log: writeStartupLog,
      });
    } catch (error) {
      console.error("Viszmo could not start:", error);
      writeStartupLog(`Startup failure: ${error.stack || error}`);
      await closeSplash();
      stopBackend();
      app.quit();
    }
  })();
}
