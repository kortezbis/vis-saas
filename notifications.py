"""Native Windows notifications for finished assignments.

Zero third-party dependencies: toasts go through the Windows Runtime
projection built into PowerShell, sound through the bundled system wav.
Everything is fire-and-forget from a daemon thread so a slow toast can
never delay task teardown or event pumping.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading

log = logging.getLogger("notifications")

DEFAULT_NOTIFICATIONS = 1

_TOAST_SCRIPT_TEMPLATE = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $template.GetElementsByTagName('text')
$null = $texts.Item(0).AppendChild($template.CreateTextNode('__TITLE__'))
$null = $texts.Item(1).AppendChild($template.CreateTextNode('__MESSAGE__'))
$toast = New-Object Windows.UI.Notifications.ToastNotification $template
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show($toast)
try {
  $player = New-Object System.Media.SoundPlayer 'C:\Windows\Media\Windows Notify System Generic.wav'
  $player.Play()
} catch {
  try { [System.Media.SystemSounds]::Asterisk.Play() } catch { }
}
""".strip()

_STATUS_TITLES = {
    "done": "Assignment finished",
    "error": "Assignment stopped",
    "aborted": "Assignment stopped",
    "copilot_complete": "Answer collection finished",
}

# Dashboard settings override the env default without a restart.
_runtime_enabled: bool | None = None


def set_runtime_enabled(value: bool | None) -> None:
    global _runtime_enabled
    _runtime_enabled = None if value is None else bool(value)


def enabled() -> bool:
    if _runtime_enabled is not None:
        return _runtime_enabled
    raw = os.getenv("VISZMO_NOTIFICATIONS", str(DEFAULT_NOTIFICATIONS))
    try:
        return bool(int(float(raw)))
    except (TypeError, ValueError):
        return True


def _escape(text: str) -> str:
    """Make text safe inside a PowerShell double-quoted here-string."""
    return (
        str(text or "")
        .replace("`", "``")
        .replace('"', '`"')
        .replace("$", "`$")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()[:180]
    )


def notify_task_event(event_type: str, detail: str = "") -> None:
    """Fire a toast + sound for a terminal task event. Never raises."""
    if not enabled():
        return
    title = _STATUS_TITLES.get(str(event_type))
    if title is None:
        return
    message = _escape(detail) or "Viszmo finished working on the assignment."
    thread = threading.Thread(
        target=_deliver,
        args=(title, message),
        daemon=True,
        name="viszmo-notify",
    )
    thread.start()


def _deliver(title: str, message: str) -> None:
    try:
        script = (
            _TOAST_SCRIPT_TEMPLATE.replace("__TITLE__", _escape(title))
            .replace("__MESSAGE__", message)
        )
        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creationflags,
            stdin=subprocess.DEVNULL,
        )
        if completed.returncode != 0 and (completed.stderr or "").strip():
            log.debug("Toast delivery noted an error: %s", (completed.stderr or "").strip()[:200])
    except Exception as exc:
        log.debug("Toast could not be shown: %s", exc)
