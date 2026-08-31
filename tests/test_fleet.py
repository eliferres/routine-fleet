"""Hermetic tests: real rosters, real run logs, real temp directories.

Slot arithmetic is exercised with injected timestamps (`--now`), so nothing here
depends on the clock, on cron being installed, or on a routine actually running.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fleet  # noqa: E402

OK_COMMAND = ["/bin/sh", "-c", "exit 0"]
FAIL_COMMAND = ["/bin/sh", "-c", "exit 4"]


def entry(name, schedule, command=None, **extra):
    routine = {"name": name, "schedule": schedule, "runs": "routines/%s.md" % name,
               "owner": "owner@example.com"}
    if command:
        routine["command"] = command
    routine.update(extra)
    return routine


class FleetCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fleet-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "routines"))

    def write_roster(self, routines, **top):
        """Writes the roster and creates every prompt file it names, unless the
        caller asked for rot by passing `rot=[names]`."""
        rot = set(top.pop("rot", []))
        data = {"version": 1, "routines": routines}
        data.update(top)
        for routine in routines:
            if routine["name"] not in rot:
                with open(os.path.join(self.root, routine["runs"]), "w") as prompt:
                    prompt.write("# %s\n" % routine["name"])
        path = os.path.join(self.root, "fleet.json")
        with open(path, "w") as handle:
            json.dump(data, handle)
        return path

    def write_log(self, records):
        state = os.path.join(self.root, "state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "run-log.jsonl"), "w") as log:
            for record in records:
                log.write(json.dumps(record) + "\n")
        return state

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fleet.main(list(argv))
        return code, out.getvalue() + err.getvalue()


class TestGuard(FleetCase):
    def test_twin_refused_in_the_same_slot(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND)])
        state = os.path.join(self.root, "state")
        first, _ = self.run_cli("--roster", roster, "--state", state,
                                "--now", "2026-03-02T08:05", "run", "brief")
        second, output = self.run_cli("--roster", roster, "--state", state,
                                      "--now", "2026-03-02T08:59", "run", "brief")
        self.assertEqual(first, 0)
        self.assertEqual(second, 3)
        self.assertIn("TWIN REFUSED", output)

    def test_next_slot_is_allowed(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND)])
        state = os.path.join(self.root, "state")
        self.run_cli("--roster", roster, "--state", state,
                     "--now", "2026-03-02T08:05", "run", "brief")
        code, _ = self.run_cli("--roster", roster, "--state", state,
                               "--now", "2026-03-03T08:01", "run", "brief")
        self.assertEqual(code, 0)

    def test_run_records_start_and_completion_with_exit_code(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", FAIL_COMMAND)])
        state = os.path.join(self.root, "state")
        code, _ = self.run_cli("--roster", roster, "--state", state,
                               "--now", "2026-03-02T08:05", "run", "brief")
        self.assertEqual(code, 4)
        events = fleet.State(state).events()[0]
        self.assertEqual([e["event"] for e in events], ["start", "complete"])
        self.assertEqual(events[1]["exit"], 4)
        self.assertEqual(events[1]["slot"], "2026-03-02T08:00")

    def test_unknown_routine_is_refused(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND)])
        code, output = self.run_cli("--roster", roster, "--now", "2026-03-02T08:05",
                                    "run", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("not in the roster", output)


class TestWatchdog(FleetCase):
    def test_silence_is_flagged(self):
        roster = self.write_roster([entry("sweep", "0 2 * * *", OK_COMMAND)])
        state = self.write_log([{"name": "sweep", "slot": "2026-02-27T02:00",
                                 "event": "complete", "exit": 0}])
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T09:00", "report")
        self.assertEqual(code, 1)
        self.assertIn("MISSED", output)
        self.assertIn("2026-03-02 02:00", output)

    def test_rot_is_flagged(self):
        roster = self.write_roster([entry("audit", "0 6 * * 1", OK_COMMAND)], rot=["audit"])
        state = self.write_log([])
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T09:00", "report")
        self.assertEqual(code, 1)
        self.assertIn("ROTTED", output)
        self.assertIn("routines/audit.md is missing", output)

    def test_healthy_run_is_all_clear(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND)])
        state = self.write_log([{"name": "brief", "slot": "2026-03-02T08:00",
                                 "event": "complete", "exit": 0}])
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T09:00", "report")
        self.assertEqual(code, 0)
        self.assertIn("ALL CLEAR", output)

    def test_nonzero_exit_is_failed_not_ok(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND)])
        state = self.write_log([{"name": "brief", "slot": "2026-03-02T08:00",
                                 "event": "complete", "exit": 4}])
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T09:00", "report")
        self.assertEqual(code, 1)
        self.assertIn("FAILED", output)

    def test_grace_window_holds_a_fresh_slot_open(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND,
                                          grace_minutes=45)])
        state = self.write_log([{"name": "brief", "slot": "2026-03-01T08:00",
                                 "event": "complete", "exit": 0}])
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T08:10", "report")
        self.assertEqual(code, 0)
        self.assertIn("2026-03-01 08:00", output)

    def test_refused_twins_reach_the_report(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND)])
        state = self.write_log([{"name": "brief", "slot": "2026-03-02T08:00",
                                 "event": "complete", "exit": 0},
                                {"name": "brief", "slot": "2026-03-02T08:00",
                                 "event": "twin-refused"}])
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T09:00", "report")
        self.assertEqual(code, 1)
        self.assertIn("1 twin refused", output)

    def test_watchdog_checks_itself_last(self):
        roster = self.write_roster(
            [entry("guard", "0 9 * * *", OK_COMMAND), entry("brief", "0 8 * * *", OK_COMMAND)],
            watchdog="guard")
        state = self.write_log([])
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T10:00", "report")
        body = [line for line in output.splitlines() if "MISSED" in line]
        self.assertEqual(code, 1)
        self.assertEqual(len(body), 2)
        self.assertIn("guard", body[-1])
        self.assertIn("watchdog, checked last", body[-1])


class TestParity(FleetCase):
    def crontab(self, lines):
        path = os.path.join(self.root, "crontab.txt")
        with open(path, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def test_missing_extra_and_drifted_are_each_named(self):
        roster = self.write_roster([entry("brief", "0 8 * * *"), entry("sweep", "0 2 * * *"),
                                    entry("audit", "0 6 * * 1")])
        live = self.crontab([
            "0 8 * * * /opt/fleet/fleet.py run brief  # fleet:brief",
            "0 3 * * * /opt/fleet/fleet.py run sweep  # fleet:sweep",
            "0 5 * * * /opt/fleet/fleet.py run stray  # fleet:stray",
            "15 4 * * 0 /usr/local/bin/backup.sh",
        ])
        code, output = self.run_cli("--roster", roster, "parity", "--source", live)
        self.assertEqual(code, 1)
        self.assertIn("MISSING  audit", output)
        self.assertIn("EXTRA    stray", output)
        self.assertIn("DRIFTED  sweep", output)
        self.assertNotIn("backup", output)

    def test_generated_crontab_is_in_sync_with_its_roster(self):
        roster = self.write_roster([entry("brief", "0 8 * * *"), entry("audit", "0 6 * * 1")])
        out = io.StringIO()
        with redirect_stdout(out):
            fleet.main(["--roster", roster, "crontab", "--install-dir", "/opt/fleet"])
        live = self.crontab(out.getvalue().splitlines())
        code, output = self.run_cli("--roster", roster, "parity", "--source", live)
        self.assertEqual(code, 0)
        self.assertIn("IN SYNC", output)

    def test_json_adapter_reads_an_export(self):
        roster = self.write_roster([entry("brief", "0 8 * * *"), entry("audit", "0 6 * * 1")])
        export = os.path.join(self.root, "scheduler.json")
        with open(export, "w") as handle:
            json.dump([{"name": "brief", "schedule": "0 8 * * *"},
                       {"name": "audit", "schedule": "0 7 * * 1"}], handle)
        code, output = self.run_cli("--roster", roster, "parity", "--adapter", "json",
                                    "--source", export)
        self.assertEqual(code, 1)
        self.assertIn("DRIFTED  audit", output)
        self.assertNotIn("brief", output)


class TestValidate(FleetCase):
    def lint(self, routines, **top):
        return self.run_cli("--roster", self.write_roster(routines, **top), "validate")

    def test_a_good_roster_passes(self):
        code, output = self.lint([entry("brief", "0 8 * * *"), entry("guard", "0 9 * * *")],
                                 watchdog="guard")
        self.assertEqual(code, 0)
        self.assertIn("well formed", output)

    def test_duplicate_names_are_rejected(self):
        code, output = self.lint([entry("brief", "0 8 * * *"), entry("brief", "0 9 * * *")])
        self.assertEqual(code, 1)
        self.assertIn("duplicate name `brief`", output)

    def test_malformed_entries_are_each_reported(self):
        broken = entry("Bad Name", "0 8 * * *")
        del broken["owner"]
        broken["schedule"] = "0 99 * * *"
        broken["retries"] = 3
        code, output = self.run_cli("--roster", self.write_roster([broken]), "validate")
        self.assertEqual(code, 1)
        self.assertIn("`name` must match", output)
        self.assertIn("`owner` must be a non-empty string", output)
        self.assertIn("Expected `hour` in 0-23, got 99", output)
        self.assertIn("unknown key `retries`", output)

    def test_watchdog_must_name_a_real_routine(self):
        code, output = self.lint([entry("brief", "0 8 * * *")], watchdog="guard")
        self.assertEqual(code, 1)
        self.assertIn("`watchdog` names `guard`", output)


class TestShippedExamples(FleetCase):
    """The demo and templates are part of the contract, so CI runs them too."""

    def repo(self, *parts):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), *parts)

    def test_shipped_rosters_validate(self):
        for roster in ("demo/fleet.json", "templates/fleet.json"):
            code, output = self.run_cli("--roster", self.repo(roster), "validate")
            self.assertEqual(code, 0, output)

    def test_demo_report_shows_one_of_each_state(self):
        state = self.write_log([])
        shutil.copy(self.repo("demo", "state", "run-log.jsonl"),
                    os.path.join(state, "run-log.jsonl"))
        code, output = self.run_cli("--roster", self.repo("demo", "fleet.json"),
                                    "--state", state, "--now", "2026-03-02T09:00", "report")
        self.assertEqual(code, 1)
        self.assertIn("OK         daily-standup-brief", output)
        self.assertIn("MISSED     nightly-link-sweep", output)
        self.assertIn("ROTTED     weekly-access-audit", output)


if __name__ == "__main__":
    unittest.main()
