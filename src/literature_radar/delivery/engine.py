"""Deliver structured results with an injected notification provider."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Mapping, Protocol

class Notifier(Protocol):
    def notify(self, title: str, message: str) -> None: ...

class NullNotifier:
    def notify(self, title: str, message: str) -> None:
        pass


class AlertNotifier(Protocol):
    def send(self, payload: dict, idempotency_key: str) -> None: ...


def dispatch_outbox(store, notifiers: Mapping[str, AlertNotifier], *, limit: int = 100,
                    topic_id: str | None = None,
                    allowed_paper_ids: set[str] | None = None,
                    allowed_versions: Mapping[str, str] | None = None) -> dict[str, int]:
    """Delivery errors are recorded locally and never unwind discovery commits."""
    counts = {"sent": 0, "failed": 0}
    attempted = []
    for _ in range(limit):
        claim_options = {"channels": tuple(notifiers), "exclude": tuple(attempted),
                         "topic_id": topic_id}
        if allowed_paper_ids is not None:
            claim_options["allowed_paper_ids"] = allowed_paper_ids
        if allowed_versions is not None:
            claim_options["allowed_versions"] = allowed_versions
        item = store.claim_notification(**claim_options)
        if item is None:
            break
        attempted.append(item["idempotency_key"])
        try:
            notifier = notifiers[item["channel"]]
            notifier.send(json.loads(item["payload_json"]), item["idempotency_key"])
        except Exception as exc:
            store.complete_notification(item["idempotency_key"], sent=False,
                                        error=type(exc).__name__)
            counts["failed"] += 1
        else:
            store.complete_notification(item["idempotency_key"], sent=True)
            counts["sent"] += 1
    return counts

def deliver(path: Path, result: dict, notifier: Notifier | None = None) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if notifier:
        for item in result["papers"]:
            if item["attention"]["priority"] == "HIGH":
                notifier.notify("Literature attention", item["paper"]["title"])
    return output
