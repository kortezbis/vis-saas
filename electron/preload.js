"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("viszmo", {
  hideWorkspace: () => ipcRenderer.invoke("hide-workspace"),
  relaunch: () => ipcRenderer.invoke("relaunch-app"),
  getAuth: () => ipcRenderer.invoke("get-auth"),
  getEntitlements: () => ipcRenderer.invoke("get-entitlements"),
  getDesktopUsage: () => ipcRenderer.invoke("get-desktop-usage"),
  getControllerAuth: () => ipcRenderer.invoke("get-controller-auth"),
  openPricing: (plan) => ipcRenderer.invoke("open-pricing", plan),
  startAuth: () => ipcRenderer.invoke("start-auth"),
  signOut: () => ipcRenderer.invoke("sign-out"),
  getUpdateStatus: () => ipcRenderer.invoke("get-update-status"),
  checkForUpdates: () => ipcRenderer.invoke("check-for-updates"),
  downloadUpdate: () => ipcRenderer.invoke("download-update"),
  installUpdate: () => ipcRenderer.invoke("install-update"),
  openUpdatePage: () => ipcRenderer.invoke("open-update-page"),
  setWidget: (enabled) => ipcRenderer.invoke("set-widget", Boolean(enabled)),
  onWidgetVisibilityChanged: (callback) => {
    const listener = (_event, visible) => callback(visible);
    ipcRenderer.on("widget-visibility-changed", listener);
    return () => ipcRenderer.removeListener("widget-visibility-changed", listener);
  },
  onUpdateStatus: (callback) => {
    const listener = (_event, status) => callback(status);
    ipcRenderer.on("update-status", listener);
    return () => ipcRenderer.removeListener("update-status", listener);
  },
  onAuthChanged: (callback) => {
    const listener = (_event, session) => callback(session);
    ipcRenderer.on("auth-changed", listener);
    return () => ipcRenderer.removeListener("auth-changed", listener);
  },
});
