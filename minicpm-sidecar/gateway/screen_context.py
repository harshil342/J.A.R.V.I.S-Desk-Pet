"""Screen understanding and active window context module for DeskPet Jarvis.

Provides active foreground window inspection, process mapping, and native
screen text extraction (OCR) optimized for fast, accurate grounding on local
1B models.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from .log_setup import get_logger

log = get_logger("screen_context")


@dataclass
class ActiveWindowInfo:
    title: str
    app_name: str
    pid: int = 0
    filename: Optional[str] = None

    def formatted(self) -> str:
        parts = []
        if self.app_name and self.app_name != "unknown":
            parts.append(f"Application: {self.app_name}")
        if self.title:
            parts.append(f"Window Title: '{self.title}'")
        if self.filename:
            parts.append(f"Active File: '{self.filename}'")
        if not parts:
            return "No active foreground window detected."
        return ", ".join(parts) + "."

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "app_name": self.app_name,
            "pid": self.pid,
            "filename": self.filename,
            "summary": self.formatted(),
        }


def _extract_filename_from_title(title: str, app_name: str) -> Optional[str]:
    """Attempt to parse the active file name from common editor window titles."""
    if not title:
        return None
    app_lower = (app_name or "").lower()

    # VS Code / Cursor: "filename.ext - folder - Visual Studio Code"
    if "code" in app_lower or "cursor" in app_lower or "visual studio" in title.lower():
        parts = title.split(" - ")
        if len(parts) >= 2:
            cand = parts[0].strip().lstrip("● ")  # strip unsaved indicator
            if "." in cand and len(cand.split()) == 1:
                return cand

    # Notepad / Sublime / Text Editors: "filename.txt - Notepad"
    if " - " in title:
        cand = title.split(" - ")[0].strip().lstrip("*")
        if "." in cand and len(cand.split()) == 1:
            return cand

    return None


def get_active_window() -> str:
    """Return a human-friendly string describing the active foreground window."""
    info = get_active_window_info()
    return info.formatted()


def get_active_window_info() -> ActiveWindowInfo:
    """Inspect the current foreground window with cross-platform support."""
    sys_platform = platform.system()

    if sys_platform == "Windows":
        return _get_active_window_windows()
    elif sys_platform == "Darwin":
        return _get_active_window_macos()
    else:
        return _get_active_window_linux()


def _get_active_window_windows() -> ActiveWindowInfo:
    """Query Windows user32 for foreground HWND, title, and owning process."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            # Fallback to finding top active GUI process if foreground is 0
            return _find_top_active_gui_process_windows()

        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()

        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_pid = pid.value

        app_name = "unknown"
        if process_pid > 0:
            try:
                proc = psutil.Process(process_pid)
                app_name = proc.name()
            except Exception:
                pass

        # Prettify common process names
        pretty_names = {
            "code.exe": "Visual Studio Code",
            "cursor.exe": "Cursor",
            "chrome.exe": "Google Chrome",
            "msedge.exe": "Microsoft Edge",
            "firefox.exe": "Firefox",
            "windowsterminal.exe": "Windows Terminal",
            "wt.exe": "Windows Terminal",
            "powershell.exe": "PowerShell",
            "cmd.exe": "Command Prompt",
            "notepad.exe": "Notepad",
            "slack.exe": "Slack",
            "discord.exe": "Discord",
            "spotify.exe": "Spotify",
            "explorer.exe": "File Explorer",
        }
        display_app = pretty_names.get(app_name.lower(), app_name)
        filename = _extract_filename_from_title(title, display_app)

        return ActiveWindowInfo(
            title=title,
            app_name=display_app,
            pid=process_pid,
            filename=filename,
        )
    except Exception as exc:
        log.warning("Failed to query active window on Windows: %s", exc)
        return ActiveWindowInfo(title="", app_name="unknown", pid=0)


def _find_top_active_gui_process_windows() -> ActiveWindowInfo:
    """Fallback when foreground HWND is 0 (e.g. desktop or non-interactive subshell)."""
    common_apps = [
        ("code.exe", "Visual Studio Code"),
        ("cursor.exe", "Cursor"),
        ("windowsterminal.exe", "Windows Terminal"),
        ("chrome.exe", "Google Chrome"),
        ("msedge.exe", "Microsoft Edge"),
    ]
    for proc_name, display_name in common_apps:
        for proc in psutil.process_iter(["name", "pid"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() == proc_name:
                    return ActiveWindowInfo(
                        title="",
                        app_name=display_name,
                        pid=proc.info["pid"],
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    return ActiveWindowInfo(title="", app_name="Desktop", pid=0)


def _get_active_window_macos() -> ActiveWindowInfo:
    """macOS active window detection via AppleScript."""
    try:
        script = 'tell application "System Events" to get name of first process whose frontmost is true'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
        app_name = (r.stdout or "").strip() or "unknown"
        return ActiveWindowInfo(title="", app_name=app_name)
    except Exception:
        return ActiveWindowInfo(title="", app_name="unknown")


def _get_active_window_linux() -> ActiveWindowInfo:
    """Linux active window detection via xdotool or xprop."""
    try:
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=3)
        title = (r.stdout or "").strip()
        return ActiveWindowInfo(title=title, app_name="Linux Application")
    except Exception:
        return ActiveWindowInfo(title="", app_name="unknown")


def get_running_apps_summary() -> str:
    """Scan and list major active developer and productivity applications."""
    monitored = {
        "code.exe": "VS Code",
        "cursor.exe": "Cursor",
        "chrome.exe": "Chrome",
        "msedge.exe": "Edge",
        "firefox.exe": "Firefox",
        "windowsterminal.exe": "Terminal",
        "slack.exe": "Slack",
        "discord.exe": "Discord",
        "spotify.exe": "Spotify",
        "docker desktop.exe": "Docker",
    }
    found = set()
    for proc in psutil.process_iter(["name"]):
        try:
            pname = (proc.info["name"] or "").lower()
            if pname in monitored:
                found.add(monitored[pname])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not found:
        return "No standard developer or productivity applications currently running."
    return "Running productivity applications: " + ", ".join(sorted(found)) + "."


def extract_screen_text(region: str = "screen") -> str:
    """Capture screen or active window and extract text via local OCR."""
    if platform.system() != "Windows":
        return "Screen OCR is currently supported natively on Windows."

    # Fast PowerShell script that takes a snapshot and extracts text
    ps_script = """
    Add-Type -AssemblyName System.Windows.Forms,System.Drawing
    $screen = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($screen.X, $screen.Y, 0, 0, $bmp.Size)
    
    # Save to temp memory stream for OCR
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose()
    $bmp.Dispose()
    $bytes = $ms.ToArray()
    $ms.Dispose()

    # Check for WinRT OCR
    try {
        [Windows.Media.Ocr.OcrEngine, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime] | Out-Null
        [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation.UniversalApiContract, ContentType = WindowsRuntime] | Out-Null
        
        $memStream = [System.IO.WindowsRuntimeStreamExtensions]::AsRandomAccessStream((New-Object System.IO.MemoryStream(,$bytes)))
        $decoder = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($memStream).GetAwaiter().GetResult()
        $softBmp = $decoder.GetSoftwareBitmapAsync().GetAwaiter().GetResult()
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
        
        if ($engine -ne $null) {
            $ocrResult = $engine.RecognizeAsync($softBmp).GetAwaiter().GetResult()
            Write-Output $ocrResult.Text
        } else {
            Write-Output "[OCR Engine unavailable]"
        }
    } catch {
        Write-Output "[OCR extraction completed without text]"
    }
    """

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=12,
        )
        out = (r.stdout or "").strip()
        if not out or out.startswith("[OCR"):
            # If WinRT OCR stream parsing wasn't supported, fall back gracefully
            return "Screen snapshot captured. No high-contrast readable text detected on screen."

        # Clean up lines and limit to 1500 chars to avoid overwhelming 1B context
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        cleaned = "\n".join(lines[:30])
        return cleaned[:1500]
    except Exception as exc:
        log.warning("Screen text extraction error: %s", exc)
        return f"Could not extract screen text: {exc}"


def inspect_screen(query: Optional[str] = None) -> str:
    """Combine active window metadata and visible screen OCR text into a unified context snapshot."""
    win_info = get_active_window_info()
    screen_text = extract_screen_text()

    parts = [
        f"Active Window: {win_info.formatted()}",
    ]
    if screen_text and not screen_text.startswith("Could not extract") and not screen_text.startswith("Screen snapshot captured. No high-contrast"):
        parts.append(f"Visible Screen Text:\n---\n{screen_text}\n---")
    else:
        parts.append(screen_text)

    return "\n".join(parts)
