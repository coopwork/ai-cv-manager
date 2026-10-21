import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "skills/job-apply/scripts/application_log.py"
SPEC = importlib.util.spec_from_file_location("application_log", SOURCE)
journal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(journal)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.clock = datetime(2026, 10, 21, 9, tzinfo=timezone.utc)
        self.mock_clock = patch.object(journal, "now", side_effect=lambda: self.clock)
        self.mock_clock.start()
        self.config = {
            "site": "hh.kz", "account_id": "candidate-1", "resume_id": "r1",
            "resume_title": "Инженер / Әзірлеуші", "languages": ["ru", "kk"],
            "cover_language": "ru", "timezone": "Asia/Almaty", "max_applications": 2,
            "allow_without_cover": False, "filters": {"region": "user-selected"},
        }
        self.data = {
            "url": "https://hh.kz/vacancy/100?utm_source=test",
            "vacancy_title": "Инженер", "company": "Компания",
            "cover_language": "ru", "cover_status": "included",
            "cover_letter": "Здравствуйте!\nӘ Ғ Қ Ң Ө Ұ Ү Һ І\nОпыт соответствует требованиям.",
        }

    def tearDown(self):
        self.mock_clock.stop()
        self.temp.cleanup()

    def start(self, **changes):
        result = journal.start(self.root, {**self.config, **changes}, journal.iso(self.clock))
        return journal.load_run(self.root, result["run_id"])

    def prepare(self, run, **changes):
        return journal.record(self.root, run, "prepared", {**self.data, **changes})["application_id"]

    def outcome(self, run, application_id, status="submitted", **changes):
        return journal.record(self.root, run, status,
                              {**self.data, "evidence": "Observed same-resume status", **changes},
                              application_id)

    def test_canonical_urls_and_language_paths(self):
        a = journal.canonical_vacancy("https://www.hh.ru/vacancy/100?hhtm=abc#tracking")
        b = journal.canonical_vacancy(self.data["url"])
        self.assertEqual(a["vacancy_key"], b["vacancy_key"])
        a = journal.canonical_vacancy("https://www.enbek.kz/kk/vacancy/AB_123?x=1")
        b = journal.canonical_vacancy("https://enbek.kz/ru/vacancy/AB_123")
        self.assertEqual(a["vacancy_key"], b["vacancy_key"])
        for bad in ("https://hh.kz.attacker.test/vacancy/100", "http://hh.kz/vacancy/100",
                    "https://user:secret@hh.kz/vacancy/100", "https://hh.kz:9222/vacancy/100"):
            with self.assertRaises(journal.JournalError):
                journal.canonical_vacancy(bad)

    def test_markdown_preserves_letter_and_local_time(self):
        run = self.start()
        op = self.prepare(run)
        result = self.outcome(run, op)
        body = Path(result["log_path"]).read_text(encoding="utf-8")
        self.assertIn("2026-10-21T14:00:00+05:00", body)
        self.assertIn(self.data["cover_letter"].splitlines()[1], body)
        self.assertEqual(result["counts"]["submitted"], 1)
        self.assertEqual(journal.latest_events(self.root)[op]["cover_letter"], self.data["cover_letter"])
        self.assertTrue(Path(result["log_path"]).resolve().is_relative_to(self.root))

    def test_same_resume_is_blocked_after_rename_and_cross_hh_domain(self):
        run = self.start()
        self.outcome(run, self.prepare(run))
        run["status"] = "finished"
        journal.write_json(self.root / "private/runs" / (run["run_id"] + ".json"), run)
        other = self.start(site="hh.ru", resume_title="Новое название")
        gate = journal.eligibility(self.root, other, journal.canonical_vacancy(self.data["url"]))
        self.assertFalse(gate["eligible"])
        self.assertEqual(gate["reason"], "submitted")

    def test_other_resume_and_other_account_can_apply(self):
        run = self.start()
        self.outcome(run, self.prepare(run))
        for changes in ({"resume_id": "r2"}, {"account_id": "candidate-2"}):
            other = self.start(**changes)
            self.assertTrue(journal.eligibility(self.root, other,
                            journal.canonical_vacancy(self.data["url"]))["eligible"])
            self.assertNotEqual(other["log_file"], run["log_file"])

    def test_unknown_reserves_quota_and_can_be_resolved(self):
        run = self.start(max_applications=1)
        op = self.prepare(run)
        self.outcome(run, op, "unknown")
        self.assertEqual(journal.summary(self.root, run)["reason"], "application_limit")
        with self.assertRaises(journal.JournalError):
            self.prepare(run, url="https://hh.kz/vacancy/101")
        self.outcome(run, op, "failed", reason="Confirmed no application in site history")
        self.assertTrue(journal.summary(self.root, run)["can_prepare"])
        self.prepare(run, url="https://hh.kz/vacancy/101")

    def test_one_hour_includes_preflight_and_outcome_can_be_recorded_after(self):
        started = self.clock - timedelta(minutes=59)
        result = journal.start(self.root, self.config, journal.iso(started))
        run = journal.load_run(self.root, result["run_id"])
        op = self.prepare(run)
        self.clock += timedelta(minutes=1)
        self.assertEqual(journal.summary(self.root, run)["reason"], "one_hour_deadline")
        with self.assertRaises(journal.JournalError):
            self.prepare(run, url="https://hh.kz/vacancy/101")
        self.outcome(run, op)
        with self.assertRaises(journal.JournalError):
            journal.start(self.root, self.config, journal.iso(started))

    def test_new_submission_requires_durable_preparation_and_evidence(self):
        run = self.start()
        with self.assertRaises(journal.JournalError):
            self.outcome(run, None)
        op = self.prepare(run)
        with self.assertRaises(journal.JournalError):
            self.outcome(run, op, evidence="")
        with self.assertRaises(journal.JournalError):
            self.outcome(run, op, cover_letter="Changed after sending")
        self.outcome(run, op)
        with self.assertRaises(journal.JournalError):
            self.outcome(run, op, "failed", reason="cannot overwrite success")

    def test_cannot_silently_drop_letter_or_change_language(self):
        run = self.start()
        with self.assertRaises(journal.JournalError):
            self.prepare(run, cover_status="unavailable")
        with self.assertRaises(journal.JournalError):
            self.prepare(run, cover_language="en")
        other = self.start(resume_id="r2", allow_without_cover=True)
        op = self.prepare(other, cover_status="unavailable")
        self.outcome(other, op)
        self.assertEqual(journal.latest_events(self.root)[op]["cover_status"], "unavailable")

    def test_website_history_blocks_without_using_new_quota(self):
        run = self.start()
        journal.record(self.root, run, "already_applied", {**self.data, "evidence": "Same resume in history"})
        self.assertEqual(journal.summary(self.root, run)["occupied_slots"], 0)
        with self.assertRaises(journal.JournalError):
            self.prepare(run)

    def test_corrupt_machine_record_fails_closed(self):
        run = self.start()
        self.prepare(run)
        path = self.root / run["log_file"]
        with path.open("a", encoding="utf-8") as stream:
            stream.write(journal.PREFIX + '{"status":"submitted"}\n')
        with self.assertRaises(journal.JournalError):
            journal.summary(self.root, run)

    def test_active_run_and_local_lock_prevent_parallel_mutation(self):
        self.start()
        with self.assertRaises(journal.JournalError):
            self.start()
        with journal.locked(self.root):
            with self.assertRaises(journal.JournalError):
                with journal.locked(self.root):
                    self.fail("Second lock was acquired")

    def test_config_and_output_containment(self):
        for changes in ({"max_applications": True}, {"max_applications": 0},
                        {"languages": []}, {"cover_language": "en"}, {"filters": {}}):
            with self.assertRaises(journal.JournalError):
                journal.validate_config({**self.config, **changes})
        run = self.start(resume_title="../../резюме/А:Б")
        self.prepare(run)
        self.assertTrue((self.root / run["log_file"]).resolve().is_relative_to(self.root / "logs"))
        with self.assertRaises(journal.JournalError):
            journal.safe_path(self.root, "../outside")

    def test_finish_is_persisted_and_prevents_new_preparation(self):
        run = self.start()
        result = journal.finish(self.root, run, "user_stop")
        self.assertEqual(result["finish_reason"], "user_stop")
        self.assertFalse(result["can_prepare"])
        loaded = journal.load_run(self.root, run["run_id"])
        self.assertEqual(loaded["status"], "finished")
        self.assertEqual(loaded["finish_reason"], "user_stop")
        with self.assertRaises(journal.JournalError):
            self.prepare(loaded)

    def test_outcomes_remain_ordered_if_wall_clock_moves_backwards(self):
        run = self.start()
        op = self.prepare(run)
        self.clock -= timedelta(seconds=2)
        self.outcome(run, op, "unknown")
        self.assertEqual(journal.latest_events(self.root)[op]["status"], "unknown")

    def test_public_cli_works_from_an_independently_copied_skill(self):
        installed = self.root / "installed-skill"
        shutil.copytree(SOURCE.parents[1], installed, ignore=shutil.ignore_patterns("__pycache__"))
        script = installed / "scripts/application_log.py"
        workspace = self.root / "workspace"
        workspace.mkdir()
        config_file = self.root / "config.json"
        config_file.write_text(json.dumps(self.config), encoding="utf-8")
        data_file = self.root / "data.json"
        data_file.write_text(json.dumps(self.data), encoding="utf-8")

        def cli(*args, expected=0):
            process = subprocess.run([sys.executable, str(script), "--workspace", str(workspace), *args],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, expected, process.stderr)
            return json.loads(process.stdout)

        result = cli("start", "--config", str(config_file), "--started-at",
                     datetime.now(timezone.utc).isoformat())
        run_id = result["run_id"]
        prepared = cli("record", "--run", run_id, "--status", "prepared", "--data", str(data_file))
        data_file.write_text(json.dumps({**self.data, "evidence": "Same resume confirmed in history"}),
                             encoding="utf-8")
        cli("record", "--run", run_id, "--status", "submitted", "--data", str(data_file),
            "--application-id", prepared["application_id"])
        gate = cli("check", "--run", run_id, "--url", self.data["url"], expected=2)
        self.assertEqual(gate["reason"], "submitted")
        result = cli("finish", "--run", run_id, "--reason", "user_stop")
        self.assertEqual(result["counts"]["submitted"], 1)
        self.assertEqual(result["finish_reason"], "user_stop")
        self.assertTrue(Path(result["log_path"]).is_relative_to(workspace))


if __name__ == "__main__":
    unittest.main()
