"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("viszmoWidget", {
  close: () => ipcRenderer.invoke("close-widget"),
});
