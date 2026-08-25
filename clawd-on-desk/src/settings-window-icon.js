"use strict";

const path = require("path");

// New AUMID for the Deskpet Assistant rebrand — also forces Windows to
// rebuild its icon-cache entry instead of showing the stale pre-rebrand
// taskbar icon it cached under the old id.
const WINDOWS_APP_USER_MODEL_ID = "com.deskpet.assistant";
const SETTINGS_WINDOW_TITLE = "Settings";
const SETTINGS_WINDOW_LAUNCH_ARG = "--open-settings-window";

function quoteWindowsCommandArg(value) {
  const text = String(value || "");
  return `"${text.replace(/"/g, '\\"')}"`;
}

function shouldOpenSettingsWindowFromArgv(argv) {
  return Array.isArray(argv) && argv.includes(SETTINGS_WINDOW_LAUNCH_ARG);
}

function getSettingsWindowIconPath({
  platform,
  isPackaged,
  resourcesPath,
  appDir,
  existsSync,
}) {
  if (platform === "darwin") return undefined;
  if (platform !== "win32") return undefined;

  const hasFile = typeof existsSync === "function" ? existsSync : () => true;
  const candidates = [];

  if (isPackaged) {
    candidates.push(
      path.join(resourcesPath || "", "app.asar.unpacked", "assets", "icons", "256x256.png"),
      path.join(resourcesPath || "", "app.asar", "assets", "icons", "256x256.png"),
      path.join(resourcesPath || "", "icon.ico")
    );
  } else {
    candidates.push(
      path.join(appDir || "", "assets", "icons", "256x256.png"),
      path.join(appDir || "", "assets", "icon.ico")
    );
  }

  return candidates.find((candidate) => candidate && hasFile(candidate));
}

function getWindowsShellIconPath({
  isPackaged,
  resourcesPath,
  appDir,
  existsSync,
}) {
  const hasFile = typeof existsSync === "function" ? existsSync : () => true;
  const candidates = isPackaged
    ? [
        path.join(resourcesPath || "", "icon.ico"),
        path.join(resourcesPath || "", "app.asar.unpacked", "assets", "icon.ico"),
        path.join(resourcesPath || "", "app.asar", "assets", "icon.ico"),
      ]
    : [
        path.join(appDir || "", "assets", "icon.ico"),
      ];

  return candidates.find((candidate) => candidate && hasFile(candidate));
}

function getSettingsWindowTaskbarDetails({
  platform,
  isPackaged,
  resourcesPath,
  appDir,
  execPath,
  appPath,
  existsSync,
}) {
  if (platform !== "win32") return null;

  const appIconPath = getWindowsShellIconPath({
    isPackaged,
    resourcesPath,
    appDir,
    existsSync,
  }) || getSettingsWindowIconPath({
    platform,
    isPackaged,
    resourcesPath,
    appDir,
    existsSync,
  });

  const relaunchParts = [execPath];
  if (!isPackaged && appPath) relaunchParts.push(appPath);
  relaunchParts.push(SETTINGS_WINDOW_LAUNCH_ARG);
  const relaunchCommand = relaunchParts
    .filter(Boolean)
    .map(quoteWindowsCommandArg)
    .join(" ");

  return {
    appId: WINDOWS_APP_USER_MODEL_ID,
    appIconPath,
    appIconIndex: 0,
    relaunchCommand,
    relaunchDisplayName: SETTINGS_WINDOW_TITLE,
  };
}

function applyWindowsAppUserModelId(app, platform = process.platform) {
  if (platform !== "win32") return;
  if (!app || typeof app.setAppUserModelId !== "function") return;
  app.setAppUserModelId(WINDOWS_APP_USER_MODEL_ID);
}

// Windows drops toast notifications whose AUMID isn't registered. The running
// process sets com.deskpet.assistant via setAppUserModelId (taskbar grouping),
// but the toast subsystem additionally needs that AUMID present in
// HKCU\Software\Classes\AppUserModelId AND a Start Menu shortcut. Without it,
// winotify's n.show() "succeeds" but nothing appears. Register both at startup
// so native reminders surface on every machine, not just where an installer did.
function registerAumidForToasts(app, platform = process.platform) {
  if (platform !== "win32") return;
  const { execFile } = require("child_process");
  const aumid = WINDOWS_APP_USER_MODEL_ID;
  const display = "Deskpet Assistant";

  // 1) Register the AUMID key so Windows shows the app in Notification settings
  //    and routes Action Center toasts to it.
  const regKey = `HKCU\\Software\\Classes\\AppUserModelId\\${aumid}`;
  execFile(
    "reg",
    ["add", regKey, "/ve", "/t", "REG_SZ", "/d", display, "/f"],
    { windowsHide: true },
    () => {}
  );

  // 2) Ensure a Start Menu shortcut exists pointing at this exe.
  let exePath = null;
  try {
    exePath = app && typeof app.getPath === "function" ? app.getPath("exe") : process.execPath;
  } catch (_) {
    exePath = process.execPath;
  }
  if (!exePath) return;
  const ps = [
    `$lnk = Join-Path $env:APPDATA 'Microsoft\\Windows\\Start Menu\\Programs\\Deskpet.lnk';`,
    `$s = New-Object -ComObject WScript.Shell;`,
    `$sc = $s.CreateShortcut($lnk);`,
    `$sc.TargetPath = ${JSON.stringify(exePath)};`,
    `$sc.WorkingDirectory = Split-Path ${JSON.stringify(exePath)};`,
    `$sc.Description = ${JSON.stringify(display)};`,
    `$sc.Save();`,
  ].join(" ");
  execFile(
    "powershell",
    ["-NoProfile", "-NonInteractive", "-Command", ps],
    { windowsHide: true },
    () => {}
  );
}

module.exports = {
  WINDOWS_APP_USER_MODEL_ID,
  SETTINGS_WINDOW_TITLE,
  SETTINGS_WINDOW_LAUNCH_ARG,
  getSettingsWindowIconPath,
  getWindowsShellIconPath,
  getSettingsWindowTaskbarDetails,
  shouldOpenSettingsWindowFromArgv,
  applyWindowsAppUserModelId,
  registerAumidForToasts,
};
