"""Proactive Background Task & Reminder Dispatcher for DeskPet Jarvis.

Allows scheduling one-shot timers and recurring proactive checks that notify
the desktop pet via the Electron bridge when triggered.

One store, two doors: chat-created reminders ("remind me to…") and drawer
tasks (/api/tasks CRUD) land in the SAME durable pending-reminders.json file
that tools.py maintains. Entries written here carry via="task" so
tools.restore_reminders() leaves them for restore() below — no double-fire,
and everything survives a gateway restart.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .log_setup import get_logger

log = get_logger("task_dispatcher")

_bridge = None


def bind_bridge(bridge) -> None:
    """Bind pet bridge for proactive notifications."""
    global _bridge
    _bridge = bridge


# ── Durable store (shared with tools.py's reminder file) ─────────────────────
# Lazy imports of tools keep the module graph acyclic at import time.

_STORE_LOCK = threading.Lock()


def _store_path() -> Optional[Path]:
    try:
        from .tools import _reminders_file

        return _reminders_file()
    except Exception:
        return None


def _load_store_unlocked() -> list:
    path = _store_path()
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("could not read task store %s: %s", path.name, exc)
        return []
    return data if isinstance(data, list) else []


def _save_store_unlocked(items: list) -> None:
    path = _store_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        log.warning("could not persist tasks to %s: %s", path.name, exc)


def _persist_task(task: "ScheduledTask") -> None:
    with _STORE_LOCK:
        items = [r for r in _load_store_unlocked() if str(r.get("id")) != str(task.id)]
        items.append({
            "id": str(task.id),
            "fire_at": float(task.fire_at),
            "message": task.name,
            "name": task.name,
            "payload": task.payload,
            "recurring": False,
            "via": "task",
        })
        _save_store_unlocked(items)


def _persist_remove(task_id: str) -> None:
    with _STORE_LOCK:
        items = [r for r in _load_store_unlocked() if str(r.get("id")) != str(task_id)]
        _save_store_unlocked(items)


@dataclass
class ScheduledTask:
    id: str
    name: str
    delay_seconds: float
    payload: str = ""
    recurring: bool = False
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    target_time: float = 0.0
    fire_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TaskDispatcher:
    """Async background task scheduler for proactive alerts and reminders."""

    def __init__(self):
        self._tasks: Dict[str, ScheduledTask] = {}
        self._async_tasks: Dict[str, asyncio.Task] = {}
        self._running = False

    def schedule_task(
        self,
        name: str,
        delay_seconds: float,
        payload: str = "",
        recurring: bool = False,
        callback: Optional[Callable[[ScheduledTask], Any]] = None,
    ) -> ScheduledTask:
        """Schedule a task to execute after a specified delay."""
        task_id = str(uuid.uuid4())[:8]
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            now = 0.0
        target = now + max(0.1, delay_seconds)

        task = ScheduledTask(
            id=task_id,
            name=name,
            delay_seconds=delay_seconds,
            payload=payload,
            recurring=recurring,
            status="pending",
            target_time=target,
            fire_at=time.time() + max(0.1, delay_seconds),
        )
        self._tasks[task_id] = task

        if not task.recurring:
            try:
                _persist_task(task)
            except Exception as exc:
                log.warning("task persistence failed for %s: %s", task_id, exc)
        self._arm_async(task, callback)
        log.info("Scheduled task '%s' (%s) in %.1fs", name, task_id, delay_seconds)
        return task

    def _arm_async(self, task: ScheduledTask, callback: Optional[Callable[[ScheduledTask], Any]] = None,
                   *, overdue: bool = False) -> None:
        """Start the async runner for an already-tracked task (no re-persist)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop:
            self._async_tasks[task.id] = loop.create_task(
                self._runner(task, callback, overdue=overdue)
            )

    def _notify(self, task: ScheduledTask, *, overdue: bool) -> None:
        # Same door chat reminders use: native Windows toast + pet bubble.
        try:
            from .tools import _push_reminder

            _push_reminder({"message": task.name}, overdue=overdue)
            return
        except Exception as exc:
            log.warning("reminder push failed for task %s: %s", task.id, exc)
        # Fallback if tools is unusable: at least poke the bridge directly.
        if _bridge:
            try:
                _bridge.post("attention", event="TASK_ALERT", title=f"Reminder: {task.name}")
            except Exception as exc:
                log.warning("Bridge alert failed for task %s: %s", task.id, exc)

    async def _runner(self, task: ScheduledTask, callback: Optional[Callable[[ScheduledTask], Any]] = None,
                      *, overdue: bool = False):
        try:
            while True:
                await asyncio.sleep(max(0.1, task.delay_seconds))
                task.status = "triggered"
                log.info("Task '%s' (%s)%s triggered!", task.name, task.id, " (overdue)" if overdue else "")

                self._notify(task, overdue=overdue)

                if callback:
                    try:
                        res = callback(task)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as exc:
                        log.warning("Task callback error for %s: %s", task.id, exc)

                if not task.recurring:
                    task.status = "completed"
                    with _STORE_LOCK:
                        self._tasks.pop(task.id, None)
                    try:
                        _persist_remove(task.id)
                    except Exception as exc:
                        log.warning("task store cleanup failed for %s: %s", task.id, exc)
                    break
                # Recurring: re-arm from now so the cadence stays stable.
                task.delay_seconds = max(0.1, float(task.delay_seconds))
                task.fire_at = time.time() + task.delay_seconds
        except asyncio.CancelledError:
            task.status = "cancelled"
        finally:
            if task.id in self._async_tasks and not task.recurring:
                del self._async_tasks[task.id]

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a pending scheduled task."""
        if task_id in self._tasks:
            task = self._tasks[task_id]
            task.status = "cancelled"
            if task_id in self._async_tasks:
                self._async_tasks[task_id].cancel()
                del self._async_tasks[task_id]
            del self._tasks[task_id]
            try:
                _persist_remove(task_id)
            except Exception as exc:
                log.warning("task store cleanup failed for %s: %s", task_id, exc)
            log.info("Cancelled task %s ('%s')", task_id, task.name)
            return True
        return False

    def list_tasks(self) -> List[ScheduledTask]:
        """List all tracked scheduled tasks."""
        return list(self._tasks.values())

    def clear(self):
        """Cancel and remove all tasks."""
        for tid in list(self._async_tasks.keys()):
            self.cancel_task(tid)
        for tid in list(self._tasks.keys()):
            self.cancel_task(tid)

    def restore(self, now: Optional[float] = None) -> Dict[str, int]:
        """Boot-time recovery for drawer/dispatcher tasks.

        Re-arms future via="task" entries from the shared reminder store and
        fires overdue ones immediately (staggered), mirroring
        tools.restore_reminders(). Chat-created entries (no via="task") are
        left alone — tools owns those. Returns counters for logging.
        """
        now_ts = time.time() if now is None else float(now)
        fired = rearmed = dropped = 0
        with _STORE_LOCK:
            entries = [
                r for r in _load_store_unlocked()
                if isinstance(r, dict) and r.get("via") == "task" and r.get("id") and r.get("fire_at")
            ]
        for index, entry in enumerate(entries):
            try:
                fire_at = float(entry["fire_at"])
            except (KeyError, TypeError, ValueError):
                dropped += 1
                continue
            remaining = max(0.1, fire_at - now_ts)
            task = ScheduledTask(
                id=str(entry["id"]),
                name=str(entry.get("name") or entry.get("message") or "reminder"),
                delay_seconds=remaining,
                payload=str(entry.get("payload") or ""),
                recurring=False,
                target_time=0.0,
                fire_at=fire_at,
            )
            self._tasks[task.id] = task
            overdue = fire_at <= now_ts
            self._arm_async(task, overdue=overdue)
            if overdue:
                fired += 1
            else:
                rearmed += 1
        if dropped:
            log.warning("dropped %d malformed dispatcher entries", dropped)
        return {"fired": fired, "rearmed": rearmed, "dropped": dropped}


# Global default task dispatcher instance
default_task_dispatcher = TaskDispatcher()


def schedule_task_tool(name: str, delay_seconds: float, payload: str = "") -> str:
    """Tool wrapper: schedule a background proactive reminder or check."""
    try:
        task = default_task_dispatcher.schedule_task(name, float(delay_seconds), payload=payload)
        return f"Scheduled task '{task.name}' to trigger in {delay_seconds:.0f} seconds (ID: {task.id})."
    except Exception as exc:
        return f"Failed to schedule task: {exc}"
