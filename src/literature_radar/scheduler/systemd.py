"""Render isolated user-level systemd units for the weekly Topic scheduler."""
from __future__ import annotations

import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_UNIT_NAME = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_WEEKDAY = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _arg(value: str | Path) -> str:
    """Quote an ExecStart word and neutralize systemd specifier/variable expansion."""
    value = str(value)
    if not value or "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("systemd argument is empty or contains a control character")
    value = value.replace("%", "%%").replace("$", "$$")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def generate_weekly_units(unit_dir: Path, *, cli: Path, config_root: Path,
                          database: Path, output_root: Path, cards_dir: Path,
                          unit_name: str = "literature-radar-open-weekly",
                          timezone: str = "America/New_York", weekday: int = 0,
                          local_time: str = "09:00",
                          preview_notifications: bool = False) -> dict:
    """Create a new service/timer pair; existing units are never overwritten."""
    if not _UNIT_NAME.fullmatch(unit_name):
        raise ValueError("unit_name must be a lowercase systemd-safe stem")
    if weekday not in range(7):
        raise ValueError("weekday must be 0 (Monday) through 6 (Sunday)")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown IANA timezone") from exc
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time):
        raise ValueError("local_time must be HH:MM in 24-hour format")
    cli = Path(cli).expanduser().resolve(strict=True)
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise ValueError("cli must be an executable installed literature-radar entry point")
    paths = [Path(p).expanduser().resolve() for p in
             (config_root, database, output_root, cards_dir)]
    if not paths[0].is_dir():
        raise ValueError("config_root must exist before generating units")
    unit_dir = Path(unit_dir).expanduser().resolve()
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = unit_dir / f"{unit_name}.service"
    timer = unit_dir / f"{unit_name}.timer"
    if service.exists() or timer.exists():
        raise FileExistsError("Weekly unit already exists; choose another unit name or manage it explicitly")
    arguments = [cli, "weekly", "run-scheduled", "--config-root", paths[0],
                 "--database", paths[1], "--output-root", paths[2],
                 "--cards-dir", paths[3], "--timezone", timezone,
                 "--weekday", str(weekday), "--local-time", local_time]
    if preview_notifications:
        arguments.append("--preview-notifications")
    service_text = ("[Unit]\nDescription=Literature Radar weekly research scan\n"
                    "Wants=network-online.target\nAfter=network-online.target\n\n"
                    "[Service]\nType=oneshot\nExecStart=" + " ".join(map(_arg, arguments)) +
                    "\nTimeoutStartSec=4h\n")
    timer_text = ("[Unit]\nDescription=Literature Radar weekly schedule\n\n"
                  "[Timer]\nOnCalendar=" + _WEEKDAY[weekday] + " *-*-* " +
                  local_time + ":00 " + timezone + "\nPersistent=true\nUnit=" +
                  unit_name + ".service\n\n[Install]\nWantedBy=timers.target\n")
    created = []
    try:
        for path, content in ((service, service_text), (timer, timer_text)):
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {"service": str(service), "timer": str(timer),
            "unit_name": unit_name, "calendar": f"{_WEEKDAY[weekday]} {local_time} {timezone}"}
