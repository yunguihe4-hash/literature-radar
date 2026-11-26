"""Optional host application interfaces; no platform-specific implementation is required."""
from __future__ import annotations
from typing import Callable, Protocol

class SchedulerProvider(Protocol):
    def register(self, name: str, trigger: str, callback: Callable[[], None]) -> None: ...

class NoteExporter(Protocol):
    def export(self, topic_id: str, result: dict) -> None: ...
