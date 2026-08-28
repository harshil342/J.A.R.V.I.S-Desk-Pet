"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

describe("MCP IPC Surface", () => {
  it("exposes MCP server management methods in preload contract", () => {
    const fs = require("node:fs");
    const path = require("node:path");
    const chatPreloadText = fs.readFileSync(path.join(__dirname, "../src/preload-minicpm-chat.js"), "utf8");
    const settingsPreloadText = fs.readFileSync(path.join(__dirname, "../src/preload-settings.js"), "utf8");

    assert.ok(chatPreloadText.includes("mcpList: () => ipcRenderer.invoke(\"minicpm:mcp-list\")"));
    assert.ok(chatPreloadText.includes("mcpAdd: (serverConfig) => ipcRenderer.invoke(\"minicpm:mcp-add\""));
    assert.ok(chatPreloadText.includes("mcpRemove: (name) => ipcRenderer.invoke(\"minicpm:mcp-remove\""));
    assert.ok(chatPreloadText.includes("mcpReload: (name) => ipcRenderer.invoke(\"minicpm:mcp-reload\""));

    assert.ok(settingsPreloadText.includes("listMcpServers: () => ipcRenderer.invoke(\"minicpm:mcp-list\")"));
    assert.ok(settingsPreloadText.includes("addMcpServer: (serverConfig) => ipcRenderer.invoke(\"minicpm:mcp-add\""));
    assert.ok(settingsPreloadText.includes("removeMcpServer: (name) => ipcRenderer.invoke(\"minicpm:mcp-remove\""));
    assert.ok(settingsPreloadText.includes("reloadMcpServer: (name) => ipcRenderer.invoke(\"minicpm:mcp-reload\""));
  });
});
