"""One isolated, offline acceptance run against an installed Literature Radar wheel.

Usage: python ops/stage6_fresh_install_acceptance.py --work-dir /tmp/empty-dir
Run with the newly installed virtual environment's Python. Nothing is written
outside --work-dir; provider responses are fictional fixtures.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.work_dir.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("--work-dir must be absent or empty")
    root.mkdir(parents=True, exist_ok=True)
    checkout = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(checkout))  # synthetic test helpers, never src/
    os.environ["LITERATURE_RADAR_PRIVATE_CONFIG_DIR"] = str(root / "config")

    import literature_radar
    from literature_radar.attention.calibration import train_due
    from literature_radar.database.academic import AcademicStore
    from literature_radar.delivery.cards import frontmatter
    from literature_radar.delivery.review_ui import set_properties
    from literature_radar.feedback.batches import batch_progress
    from literature_radar.feedback.bridge import sync_outbox
    from literature_radar.feedback.store import load_feedback
    from literature_radar.workflow.bootstrap import run_bootstrap
    from literature_radar.workflow.weekly import _rank_weekly_rows, _select_weekly_rows, run_weekly
    from tests.test_stage37_39_workflow import RichProvider, intake, record

    package = Path(literature_radar.__file__).resolve()
    if package.is_relative_to(checkout):
        raise AssertionError("Acceptance must import the installed wheel, not the source checkout")
    cli = Path(sys.executable).parent / "literature-radar"
    if not cli.is_file():
        raise AssertionError("Installed CLI entry point missing")

    def command(*parts: str) -> dict:
        result = subprocess.run([str(cli), *parts], cwd=root, text=True,
                                capture_output=True, check=True, env=os.environ.copy())
        return json.loads(result.stdout)

    config_root = root / "config"
    ids = ("fictional-alpha", "fictional-beta")
    for identifier in ids:
        specification = intake(identifier)
        if identifier.endswith("beta"):
            specification.update(name="Fictional Beta", questions=["signal modulation"],
                                 methods=["spectral assay"], target_species=["Fictivus beta"])
        intake_file = root / f"{identifier}.json"
        intake_file.write_text(json.dumps(specification), encoding="utf-8")
        assert command("topic", "init", "--config-root", str(config_root),
                       "--input", str(intake_file))["topic_id"] == identifier
        assert command("topic", "validate", "--config-dir", str(config_root / identifier),
                       "--format", "json")["status"] == "PASS"

    database = root / "data" / "academic.sqlite3"
    command("database", "init", "--database", str(database))
    assert database.is_file()
    (root / "vaults").mkdir()
    binding = config_root / "obsidian.json"
    setup = command("obsidian", "setup", "--binding-file", str(binding),
                    "--mode", "create", "--parent-dir", str(root / "vaults"),
                    "--topic-id", ids[0], "--topic-id", ids[1], "--yes")
    vault = Path(setup["vault_root"])
    installer = checkout / "deploy" / "obsidian-bridge" / "install.py"
    subprocess.run([sys.executable, str(installer), "--vault", str(vault)],
                   check=True, capture_output=True, text=True)
    assert (vault / ".obsidian/plugins/literature-radar-bridge/main.js").is_file()

    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    bootstrap_results = {}
    for identifier in ids:
        rows = [record(index, doi=f"10.0000/{identifier}-{index}") for index in range(1, 31)]
        for row in rows:
            row["source_id"] = f"{identifier}-{row['source_id']}"
        if identifier.endswith("beta"):
            for row in rows:
                row["title"] = row["title"].replace("Imaginaryus alpha signal mapping", "Fictivus beta signal modulation")
                row["abstract"] = "Fictivus beta signal modulation uses a spectral assay to compare invented states."
        output = root / "bootstrap" / identifier
        bootstrap_results[identifier] = run_bootstrap(
            config_root / identifier, database, output,
            provider_names=["openalex"], providers={"openalex": RichProvider("openalex", [rows])},
            now=now)
        assert bootstrap_results[identifier]["selected"] == 30
        assert not (output / "bootstrap-working.sqlite3").exists()
        assert batch_progress(config_root / identifier)["valid_feedback"] == 0
        assert train_due(config_root / identifier)["status"] == "NOT_ELIGIBLE"
        command("weekly", "activate", "--config-dir", str(config_root / identifier))

    assert len(AcademicStore(database).topic_papers(ids[0])) == 30
    assert len(AcademicStore(database).topic_papers(ids[1])) == 30
    weekly = {}
    for identifier in ids:
        article = record(31, date="2026-11-09", doi=f"10.0000/{identifier}-weekly")
        article["source_id"] = f"{identifier}-{article['source_id']}"
        if identifier.endswith("beta"):
            article["title"] = article["title"].replace("Imaginaryus alpha signal mapping", "Fictivus beta signal modulation")
            article["abstract"] = "Fictivus beta signal modulation uses a spectral assay to compare invented states."
        weekly[identifier] = run_weekly(config_root / identifier, database,
                                        root / "weekly" / identifier,
                                        provider_names=["openalex"],
                                        providers={"openalex": RichProvider("openalex", [[article]])},
                                        now=datetime(2026, 11, 10, 12, tzinfo=timezone.utc),
                                        notification_preview=True)
        assert weekly[identifier]["status"] in {"SUCCESS", "PARTIAL_SUCCESS"}
    assert (vault / "dashboard.md").is_file()
    assert list(vault.glob("Bootstrap/*/BOOTSTRAP_SEED/*.md"))
    assert list(vault.glob("Weekly/2026-11-10/*/*/*.md"))

    # Exercise the three tier selection contract without altering a user's model.
    tier_rows = [{"paper_id": tier.lower(), "title": tier, "priority": tier,
                  "attention_rank_sort_key": [0] * 8, "eligible": True,
                  "basic_qualified": True} for tier in ("LOW", "MEDIUM", "HIGH")]
    assert [item["priority"] for item in _select_weekly_rows(_rank_weekly_rows(tier_rows))] == [
        "HIGH", "MEDIUM", "LOW"]

    card = next(vault.glob(f"Bootstrap/{ids[0]}/BOOTSTRAP_SEED/*.md"))
    card.write_text(set_properties(card.read_text(encoding="utf-8"),
                                   {"feedback_decision": "keep", "review_status": "reviewed"}),
                    encoding="utf-8")
    meta = frontmatter(card.read_text(encoding="utf-8"))
    outbox = vault / ".obsidian/plugins/literature-radar-bridge/outbox.json"
    outbox.write_text(json.dumps({"version": 1, "events": [{
        "id": "fictional-review-1", "topic_id": ids[0], "paper_key": meta["paper_key"],
        "paper_id": meta["paper_id"], "doi": meta["doi"], "decision": "keep",
        "recorded_at": "2026-11-10T13:00:00+00:00", "path": str(card.relative_to(vault))}]}),
        encoding="utf-8")
    assert sync_outbox(binding, [config_root / item for item in ids], database)["imported"] == 1
    assert len(load_feedback(config_root / ids[0] / "feedback.json")["entries"]) == 1
    assert len(load_feedback(config_root / ids[1] / "feedback.json")["entries"]) == 0
    assert train_due(config_root / ids[0])["status"] == "NOT_ELIGIBLE"

    units = command("weekly", "systemd-generate", "--unit-dir", str(root / "units"),
                    "--cli", str(cli), "--config-root", str(config_root),
                    "--database", str(database), "--output-root", str(root / "scheduled"),
                    "--cards-dir", str(root / "cards"), "--unit-name", "fictional-radar",
                    "--timezone", "Europe/Berlin", "--weekday", "2", "--local-time", "14:30")
    assert Path(units["service"]).is_file() and Path(units["timer"]).is_file()
    assert "Persistent=true" in Path(units["timer"]).read_text(encoding="utf-8")
    print(json.dumps({"status": "PASS", "package": str(package), "topics": list(ids),
                      "bootstrap_selected": {key: value["selected"] for key, value in bootstrap_results.items()},
                      "weekly_status": {key: value["status"] for key, value in weekly.items()},
                      "vault": str(vault), "feedback_imported": 1,
                      "systemd_units": units}, indent=2))


if __name__ == "__main__":
    main()
