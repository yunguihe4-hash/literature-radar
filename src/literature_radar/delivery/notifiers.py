"""Optional local alert channels. Neither requires the core to run on Linux."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from literature_radar.delivery.cards import _readable_name


class NotificationUnavailable(RuntimeError):
    pass


class UnavailableNotifier:
    def send(self, payload: dict, idempotency_key: str) -> None:
        raise NotificationUnavailable("configured notification channel is unavailable")


def _safe(value) -> str:
    return (str(value or "").replace("\r", " ").replace("\n", " ")
            .replace("<", "&lt;").replace(">", "&gt;").strip())


def render_alert(payload: dict) -> str:
    paper = payload["paper"]
    reason = payload["reason"]
    transfer = payload.get("transfer_explanation") or {}
    lines = [f"# {_safe(paper['title'])}", "",
             f"Priority: {_safe(payload['priority'])}",
             f"Authors: {_safe(', '.join(paper.get('authors') or []))}",
             f"Publication date: {_safe(paper.get('publication_date'))}",
             f"Sources: {_safe(', '.join(paper.get('sources') or []))}",
             f"DOI: {_safe(paper.get('doi'))}",
             f"Original: {_safe(paper.get('landing_url'))}", "",
             "## Recommendation",
             f"Relevance: {_safe(payload.get('relevance'))}",
             f"Transfer type: {_safe(payload.get('transfer_type'))}",
             f"Source species: {_safe(transfer.get('source_species'))}",
             f"Source research question: {_safe(transfer.get('source_research_question'))}",
             f"Source method: {_safe(transfer.get('source_method'))}",
             f"Target research question: {_safe(transfer.get('target_research_question'))}",
             f"Transfer rationale: {_safe(transfer.get('transfer_rationale'))}",
             f"Transfer evidence: {_safe(transfer.get('evidence_excerpt'))}",
             f"Research question: {_safe(payload.get('research_question'))}",
             f"Why recommended: {_safe(payload.get('why_recommended'))}",
             f"Questions: {_safe(', '.join(reason.get('questions') or []))}",
             f"Methods: {_safe(', '.join(reason.get('methods') or []))}",
             f"Applications: {_safe(', '.join(reason.get('applications') or []))}",
             f"Transfer: {_safe('; '.join(item.get('from', '?') + ' -> ' + item.get('to', '?') + ' (' + item['type'] +
                                               (', ' + item['scope'] if item.get('scope') else '') + ')'
                                               for item in reason.get('transfers') or []))}",
             "Evidence:"]
    lines.extend(f"- {_safe(item.get('field'))}: {_safe(item.get('excerpt') or item.get('phrase'))}"
                 for item in reason.get("evidence") or [])
    lines.extend(["", "Feedback: keep / must_read / later / discard.",
                  "Record a decision explicitly with:",
                  f"`literature-radar feedback --file {_safe(payload.get('feedback_file') or 'FEEDBACK_FILE')} --topic {_safe(payload['topic_id'])} "
                  f"--paper-key {_safe(paper['paper_id'])} --decision keep --reason YOUR_REASON`", ""])
    return "\n".join(lines)


class ObsidianNotifier:
    """Idempotent Inbox note; never writes the Knowledge tree."""
    def __init__(self, vault: Path):
        self.vault = Path(vault).expanduser().resolve()
        if not self.vault.is_dir():
            raise NotificationUnavailable("configured Vault directory does not exist")

    def send(self, payload: dict, idempotency_key: str) -> None:
        paper = payload["paper"]
        folder = self.vault / "Inbox" / "Literature"
        if not folder.resolve().is_relative_to(self.vault):
            raise NotificationUnavailable("Inbox resolves outside Vault")
        folder.mkdir(parents=True, exist_ok=True)
        import hashlib
        identity = hashlib.sha256(f"{payload['topic_id']}:{paper['paper_id']}".encode()).hexdigest()
        identity_marker = f"<!-- literature-radar-identity:{identity} -->"
        path = next((candidate for candidate in folder.glob("*.md")
                     if candidate.is_file() and not candidate.is_symlink()
                     and identity_marker in candidate.read_text(encoding="utf-8")), None)
        if path is None:
            path = folder / _readable_name(paper["title"], identity, folder)
        if path.is_symlink() or not path.resolve().is_relative_to(self.vault):
            raise NotificationUnavailable("Inbox note resolves outside Vault")
        marker = f"<!-- literature-radar-version:{payload['content_version']} -->"
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        if marker in previous:
            return
        section = marker + "\n" + render_alert(payload)
        content = previous + ("\n---\n\n" if previous else identity_marker + "\n") + section
        descriptor, temporary = tempfile.mkstemp(prefix=".literature-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try: os.unlink(temporary)
            except FileNotFoundError: pass
            raise


class DesktopNotifier:
    def send(self, payload: dict, idempotency_key: str) -> None:
        binary = shutil.which("notify-send")
        if not binary:
            raise NotificationUnavailable("desktop notifications are unavailable")
        title = _safe(payload["paper"]["title"])[:120]
        message = render_alert(payload)[:1000]
        result = subprocess.run([binary, "Literature attention: " + title, message],
                                capture_output=True, timeout=10, check=False)
        if result.returncode:
            raise NotificationUnavailable("desktop notification failed")


def configured_notifiers(bundle: dict, *, vault_override: Path | None = None,
                         desktop: bool = False) -> tuple[list[str], dict]:
    notification = bundle.get("delivery", {}).get("notification", {})
    channels, notifiers = [], {}
    if notification.get("enabled") and notification.get("provider") not in ("obsidian", "desktop"):
        channel = notification["provider"]
        channels.append(channel)
        notifiers[channel] = UnavailableNotifier()
    vault = vault_override or notification.get("extensions", {}).get("vault_path")
    if vault_override or (notification.get("enabled") and notification.get("provider") == "obsidian"):
        channels.append("obsidian")
        if vault:
            try:
                notifiers["obsidian"] = ObsidianNotifier(Path(vault))
            except NotificationUnavailable:
                notifiers["obsidian"] = UnavailableNotifier()
        else:
            notifiers["obsidian"] = UnavailableNotifier()
    if desktop or (notification.get("enabled") and notification.get("provider") == "desktop"):
        channels.append("desktop")
        notifiers["desktop"] = DesktopNotifier()
    return channels, notifiers
