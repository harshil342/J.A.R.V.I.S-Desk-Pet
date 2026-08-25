"""Proactive Background Task & Reminder Dispatcher for DeskPet Jarvis.

Allows scheduling one-shot timers and recurring proactive checks that notify
the desktop pet via the Electron bridge when triggered.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .log_setup import get_logger

log = get_logger("task_dispatcher")

_bridge = None


def bind_bridge(bridge) -> None:
    """Bind pet bridge for proactive notifications."""
    global _bridge
    _bridge = bridge


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
            loop = asyncio.get_running_loop()
            now = loop.time()
        except RuntimeError:
            loop = None
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
        )
        self._tasks[task_id] = task

        if loop:
            async_t = loop.create_task(self._runner(task, callback))
            self._async_tasks[task_id] = async_t
        log.info("Scheduled task '%s' (%s) in %.1fs", name, task_id, delay_seconds)
        return task

    async def _runner(self, task: ScheduledTask, callback: Optional[Callable[[ScheduledTask], Any]] = None):
        try:
            while True:
                await asyncio.sleep(max(0.1, task.delay_seconds))
                task.status = "triggered"
                log.info("Task '%s' (%s) triggered!", task.name, task.id)

                # Send proactive event to Electron bridge
                if _bridge:
                    try:
                        _bridge.post("attention", event="TASK_ALERT", title=f"Reminder: {task.name}")
                    except Exception as exc:
                        log.warning("Bridge alert failed for task %s: %s", task.id, exc)

                if callback:
                    try:
                        res = callback(task)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception as exc:
                        log.warning("Task callback error for %s: %s", task.id, exc)

                if not task.recurring:
                    task.status = "completed"
                    break
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
        self._tasks.clear()


# Global default task dispatcher instance
default_task_dispatcher = TaskDispatcher()


def schedule_task_tool(name: str, delay_seconds: float, payload: str = "") -> str:
    """Tool wrapper: schedule a background proactive reminder or check."""
    try:
        task = default_task_dispatcher.schedule_task(name, float(delay_seconds), payload=payload)
        return f"Scheduled task '{task.name}' to trigger in {delay_seconds:.0f} seconds (ID: {task.id})."
    except Exception as exc:
        return f"Failed to schedule task: {exc}"
