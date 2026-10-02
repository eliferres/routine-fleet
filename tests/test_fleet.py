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
from contextlib import contextmanager, redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import routine_fleet  # noqa: E402


@contextmanager
def quiet_stderr():
    """A work check's stderr is passed through to the real fd on purpose, so a
    test that crashes one on purpose has to mute the descriptor itself."""
    saved = os.dup(2)
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 2)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


SAYS_IDLE = "echo nothing-to-do"
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

    def write_check(self, name, body, executable=True, shebang="#!/bin/sh"):
        """A work check is a real executable script in a real temp directory."""
        folder = os.path.join(self.root, "checks")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w") as handle:
            handle.write(shebang + "\n" + body + "\n")
        if executable:
            os.chmod(path, 0o755)
        return "checks/" + name

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = routine_fleet.main(list(argv))
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
        events = routine_fleet.State(state).events()[0]
        self.assertEqual([e["event"] for e in events], ["start", "complete"])
        self.assertEqual(events[1]["exit"], 4)
        self.assertEqual(events[1]["slot"], "2026-03-02T08:00")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root writes through a read-only directory")
    def test_state_dir_under_a_read_only_parent_is_an_io_error(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND)])
        locked = os.path.join(self.root, "locked")
        os.makedirs(locked)
        os.chmod(locked, 0o555)
        self.addCleanup(os.chmod, locked, 0o755)
        state = os.path.join(locked, "state")
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T08:05", "run", "brief")
        self.assertEqual(code, 2)
        self.assertEqual(len(output.splitlines()), 1, output)
        self.assertIn("Cannot create marker directory", output)
        self.assertIn(os.path.join(state, "markers", "brief"), output)
        self.assertNotIn("TWIN REFUSED", output)

    def test_unknown_routine_is_refused(self):
        roster = self.write_roster([entry("brief", "0 8 * * *", OK_COMMAND)])
        code, output = self.run_cli("--roster", roster, "--now", "2026-03-02T08:05",
                                    "run", "ghost")
        self.assertEqual(code, 2)
        self.assertIn("not in the roster", output)


class TestErrorPrefix(FleetCase):
    """Errors answer as whatever the operator typed, installed command or file."""

    def error_line(self, argv0):
        original = sys.argv[0]
        sys.argv[0] = argv0
        try:
            code, output = self.run_cli("--roster", os.path.join(self.root, "absent.json"),
                                        "report")
        finally:
            sys.argv[0] = original
        self.assertEqual(code, 2)
        return output.strip()

    def test_installed_command_names_itself(self):
        self.assertTrue(self.error_line("/opt/homebrew/bin/routine-fleet")
                        .startswith("routine-fleet: "))

    def test_run_from_a_clone_names_the_file(self):
        self.assertTrue(self.error_line("./routine_fleet.py").startswith("routine_fleet.py: "))


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
            "0 8 * * * /opt/fleet/routine_fleet.py run brief  # fleet:brief",
            "0 3 * * * /opt/fleet/routine_fleet.py run sweep  # fleet:sweep",
            "0 5 * * * /opt/fleet/routine_fleet.py run stray  # fleet:stray",
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
            routine_fleet.main(["--roster", roster, "crontab", "--install-dir", "/opt/fleet"])
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


class TestCrontabForm(FleetCase):
    """The block has to name something the machine can actually execute: the
    installed command when installed, the source file when run from a clone."""

    def on_path(self, name):
        """A stand-in for the installed console script, on PATH and nowhere else."""
        bindir = os.path.join(self.root, "bin")
        os.makedirs(bindir, exist_ok=True)
        command = os.path.join(bindir, name)
        with open(command, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(command, 0o755)
        original = os.environ["PATH"]
        os.environ["PATH"] = bindir
        self.addCleanup(os.environ.__setitem__, "PATH", original)
        return command

    def crontab_as(self, argv0, *extra):
        original = sys.argv[0]
        sys.argv[0] = argv0
        try:
            roster = self.write_roster([entry("brief", "0 8 * * *")])
            _, output = self.run_cli("--roster", roster, "crontab", *extra)
        finally:
            sys.argv[0] = original
        return roster, output

    def test_installed_command_is_scheduled_by_its_resolved_path(self):
        # cron runs with a minimal PATH, so a bare command name never fires.
        command = self.on_path("routine-fleet")
        roster, output = self.crontab_as("routine-fleet")
        self.assertIn("0 8 * * * %s --roster %s run brief  # fleet:brief" % (command, roster),
                      output)
        self.assertIn("Generated by `routine-fleet crontab`", output)
        self.assertIn("`routine-fleet parity` reads those tags", output)
        self.assertNotIn("routine_fleet.py", output)

    def test_a_command_that_cannot_be_resolved_falls_back_to_its_name(self):
        self.on_path("something-else")
        roster, output = self.crontab_as("routine-fleet")
        self.assertIn("0 8 * * * routine-fleet --roster %s run brief  # fleet:brief" % roster,
                      output)

    def test_a_clone_schedules_the_module_where_it_actually_is(self):
        roster, output = self.crontab_as("./routine_fleet.py")
        runner = os.path.abspath(routine_fleet.__file__)
        self.assertTrue(os.path.exists(runner))
        self.assertIn("0 8 * * * %s --roster %s run brief  # fleet:brief" % (runner, roster),
                      output)
        self.assertIn("Generated by `routine_fleet.py crontab`", output)

    def test_an_install_dir_always_wins(self):
        _, output = self.crontab_as("/opt/homebrew/bin/routine-fleet",
                                    "--install-dir", "/opt/fleet")
        self.assertIn("0 8 * * * /opt/fleet/routine_fleet.py --roster "
                      "/opt/fleet/fleet.json run brief  # fleet:brief", output)
        self.assertIn("Generated by `routine_fleet.py crontab`", output)


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

    def test_a_bad_work_check_is_rejected(self):
        for bad in (5, "", "   ", True):
            roster = self.write_roster([entry("brief", "0 8 * * *", work_check=bad)])
            code, output = self.run_cli("--roster", roster, "validate")
            self.assertEqual(code, 1, "accepted work_check=%r" % bad)
            self.assertIn("`work_check` must be a non-empty string", output)

    def test_a_bad_work_check_seconds_is_rejected(self):
        for bad in (0, -1, 5.5, "5", True):
            roster = self.write_roster([entry("brief", "0 8 * * *")],
                                       work_check_seconds=bad)
            code, output = self.run_cli("--roster", roster, "validate")
            self.assertEqual(code, 1, "accepted work_check_seconds=%r" % bad)
            self.assertIn("`work_check_seconds` must be a positive integer", output)

    def test_a_good_work_check_passes(self):
        roster = self.write_roster([entry("brief", "0 8 * * *",
                                          work_check="checks/brief.sh")],
                                   work_check_seconds=3)
        code, _ = self.run_cli("--roster", roster, "validate")
        self.assertEqual(code, 0)

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



class TestWorkCheck(FleetCase):
    """Only a clean `nothing to do` may cancel a run. Everything else runs.

    The asymmetry is the point: a work check may save an empty run, but it may
    never be the reason a routine that had work to do stayed silent.
    """

    def touch_command(self, target):
        return ["/bin/sh", "-c", "touch %s" % target]

    def ran(self, target):
        return os.path.exists(target)

    def build(self, check_body, executable=True, shebang="#!/bin/sh", **top):
        """A one-routine fleet whose run leaves a trace on disk when it happens."""
        self.target = os.path.join(self.root, "it-ran")
        check = self.write_check("brief.sh", check_body, executable=executable,
                                 shebang=shebang)
        routines = [entry("brief", "0 8 * * *", self.touch_command(self.target),
                          work_check=check)]
        return self.write_roster(routines, **top)

    def invoke(self, roster, *before, **kwargs):
        state = kwargs.pop("state", os.path.join(self.root, "state"))
        now = kwargs.pop("now", "2026-03-02T08:05")
        return self.run_cli("--roster", roster, "--state", state, "--now", now,
                            *(list(before) + ["run", "brief"]))

    def test_a_clean_nothing_to_do_cancels_the_run(self):
        roster = self.build(SAYS_IDLE)
        code, _ = self.invoke(roster)
        self.assertEqual(code, 0)
        self.assertFalse(self.ran(self.target), "the routine ran despite an empty queue")

    def test_work_exists_runs_the_routine(self):
        roster = self.build("exit 0")
        code, _ = self.invoke(roster)
        self.assertEqual(code, 0)
        self.assertTrue(self.ran(self.target))

    def test_an_unexpected_exit_code_fails_open(self):
        roster = self.build("exit 9")
        self.invoke(roster)
        self.assertTrue(self.ran(self.target), "a broken check silenced the routine")

    def test_a_crashing_python_check_fails_open(self):
        """An uncaught exception exits 1. If 1 meant "nothing to do", the most
        common way for a check to break would look exactly like it working."""
        roster = self.build('raise RuntimeError("the check itself is broken")',
                            shebang="#!" + sys.executable)
        with quiet_stderr():
            self.invoke(roster)
        self.assertTrue(self.ran(self.target), "a crashed check silenced the routine")

    def test_a_shell_check_whose_queue_moved_fails_open(self):
        """The realistic rot: the check keeps running, its queue file is renamed
        by an unrelated change, and `set -e` exits 1 for the rest of time."""
        roster = self.build("set -e\ntest -s /nonexistent/queue.json\n" + SAYS_IDLE)
        with quiet_stderr():
            self.invoke(roster)
        self.assertTrue(self.ran(self.target), "a broken check silenced the routine")

    def test_plain_exit_1_is_not_nothing_to_do(self):
        roster = self.build("exit 1")
        with quiet_stderr():
            self.invoke(roster)
        self.assertTrue(self.ran(self.target), "exit 1 was read as nothing to do")

    def test_the_timeout_wrappers_own_failure_is_not_nothing_to_do(self):
        """GNU `timeout` exits 125 when the wrapper itself fails, and
        `timeout 30 ./probe.sh` is how a careful person writes a check."""
        roster = self.build("exit 125")
        with quiet_stderr():
            self.invoke(roster)
        self.assertTrue(self.ran(self.target), "exit 125 was read as nothing to do")

    def test_a_silent_check_that_exits_clean_is_not_nothing_to_do(self):
        """An empty or truncated script exits 0 and says nothing. Saying
        nothing is not saying there is nothing to do."""
        roster = self.build("exit 0")
        self.invoke(roster)
        self.assertTrue(self.ran(self.target), "silence was read as nothing to do")

    def test_the_word_alone_does_not_cancel_if_the_check_then_failed(self):
        roster = self.build(SAYS_IDLE + "; exit 3")
        with quiet_stderr():
            self.invoke(roster)
        self.assertTrue(self.ran(self.target), "a failed check still cancelled")

    def test_other_output_does_not_cancel(self):
        for chatter in ("nothing to do", "NOTHING-TO-DO", "idle", "nothing-to-do-yet",
                        "checking...\nnothing-to-do-ish"):
            with self.subTest(chatter=chatter):
                self.setUp()
                roster = self.build("echo '%s'" % chatter)
                self.invoke(roster)
                self.assertTrue(self.ran(self.target),
                                "%r was read as nothing to do" % chatter)

    def test_surrounding_whitespace_is_forgiven(self):
        roster = self.build("printf '  nothing-to-do \\n'")
        self.invoke(roster)
        self.assertFalse(self.ran(self.target), "a padded answer was not understood")

    def test_a_check_that_cannot_start_fails_open(self):
        roster = self.build(SAYS_IDLE, executable=False)
        code, output = self.invoke(roster)
        self.assertTrue(self.ran(self.target), "an unrunnable check silenced the routine")
        self.assertIn("could not start", output)

    def test_a_missing_check_fails_open(self):
        self.target = os.path.join(self.root, "it-ran")
        roster = self.write_roster([entry("brief", "0 8 * * *",
                                          self.touch_command(self.target),
                                          work_check="checks/gone.sh")])
        self.invoke(roster)
        self.assertTrue(self.ran(self.target), "a missing check silenced the routine")

    def test_a_hanging_check_is_killed_at_the_cap_and_the_routine_runs(self):
        roster = self.build("sleep 30; " + SAYS_IDLE, work_check_seconds=1)
        code, output = self.invoke(roster)
        self.assertTrue(self.ran(self.target), "a hung check silenced the routine")
        self.assertIn("ran past 1s", output)

    def test_ignore_work_check_runs_it_anyway(self):
        roster = self.build(SAYS_IDLE)
        self.invoke(roster, "--ignore-work-check")
        self.assertTrue(self.ran(self.target))

    def test_the_flag_first_after_the_name_is_named_not_swallowed(self):
        """`run <name> <command...>` takes the rest of the line, so a flag
        written there is the routine's command. Cancelling the run anyway, and
        exiting 0, would tell the operator they had forced a run when they had
        not. It stops and says where the flag goes."""
        roster = self.build(SAYS_IDLE)
        state = os.path.join(self.root, "state")
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T08:05",
                                    "run", "brief", "--ignore-work-check")
        self.assertEqual(code, 2)
        self.assertIn("goes before `run`", output)
        self.assertFalse(self.ran(self.target))

    def test_the_flag_with_anything_after_it_is_still_named(self):
        roster = self.build(SAYS_IDLE)
        state = os.path.join(self.root, "state")
        code, output = self.run_cli("--roster", roster, "--state", state,
                                    "--now", "2026-03-02T08:05",
                                    "run", "brief", "--ignore-work-check",
                                    "--", "/bin/echo", "hi")
        self.assertEqual(code, 2, "it silently cancelled a run asked to be forced")
        self.assertIn("goes before `run`", output)

    def test_an_explicit_dash_dash_hands_the_flag_to_the_routine(self):
        target = os.path.join(self.root, "reached")
        roster = self.build("exit 0")
        state = os.path.join(self.root, "state")
        code, _ = self.run_cli("--roster", roster, "--state", state,
                               "--now", "2026-03-02T08:05", "run", "brief",
                               "--", "/bin/sh", "-c",
                               "echo $1 > %s" % target, "sh", "--ignore-work-check")
        self.assertEqual(code, 0)
        with open(target) as handle:
            self.assertEqual(handle.read().strip(), "--ignore-work-check")

    def test_a_routine_with_no_work_check_is_told_too(self):
        """The guard used to skip these, so the flag became the routine's
        command, the run failed with a confusing errno and burned the slot."""
        self.write_roster([entry("plain", "0 8 * * *", OK_COMMAND)])
        roster = os.path.join(self.root, "fleet.json")
        code, output = self.run_cli("--roster", roster,
                                    "--state", os.path.join(self.root, "state"),
                                    "--now", "2026-03-02T08:05",
                                    "run", "plain", "--ignore-work-check")
        self.assertEqual(code, 2)
        self.assertIn("goes before `run`", output)

    def test_a_cancelled_run_does_not_spend_the_slot(self):
        """Work can turn up inside the same slot; the check must not close it."""
        roster = self.build(SAYS_IDLE)
        state = os.path.join(self.root, "state")
        self.invoke(roster, state=state)
        self.assertFalse(self.ran(self.target))
        # same slot, and now the check says there is work
        self.write_check("brief.sh", "exit 0")
        code, _ = self.invoke(roster, state=state, now="2026-03-02T08:40")
        self.assertEqual(code, 0, "the cancelled run had claimed the slot")
        self.assertTrue(self.ran(self.target))

    def test_a_real_run_still_refuses_its_twin(self):
        roster = self.build("exit 0")
        state = os.path.join(self.root, "state")
        self.invoke(roster, state=state)
        code, output = self.invoke(roster, state=state, now="2026-03-02T08:47")
        self.assertEqual(code, 3)
        self.assertIn("TWIN REFUSED", output)

    def test_the_run_log_records_the_skip_with_its_reason(self):
        roster = self.build(SAYS_IDLE)
        self.invoke(roster)
        with open(os.path.join(self.root, "state", "run-log.jsonl")) as log:
            records = [json.loads(line) for line in log if line.strip()]
        self.assertEqual([r["event"] for r in records], ["skipped"])
        self.assertEqual(records[0]["why"], "nothing to do")
        self.assertEqual(records[0]["check"], "checks/brief.sh")

    def test_the_check_is_told_which_routine_it_answers_for(self):
        seen = os.path.join(self.root, "seen")
        roster = self.build('printf "%s %s" "$FLEET_ROUTINE" "$FLEET_SLOT" > '
                            + seen + "; " + SAYS_IDLE)
        self.invoke(roster)
        with open(seen) as handle:
            self.assertEqual(handle.read(), "brief 2026-03-02T08:00")


class TestReplay(FleetCase):
    """A firing long after its slot is a scheduler catching up on a slot it
    slept through, not the schedule. The guard refuses it and the watchdog
    still reports the slot as missed."""

    def setUp(self):
        super(TestReplay, self).setUp()
        self.target = os.path.join(self.root, "ran")
        self.state = os.path.join(self.root, "state")

    def build(self, **extra):
        top = extra.pop("top", {})
        command = ["/bin/sh", "-c", "touch %s" % self.target]
        return self.write_roster([entry("brief", "0 8 * * *", command, **extra)], **top)

    def invoke(self, roster, now, *flags):
        return self.run_cli("--roster", roster, "--state", self.state,
                            "--now", now, *(flags + ("run", "brief")))

    def test_a_run_inside_the_window_runs(self):
        code, _ = self.invoke(self.build(), "2026-03-02T09:59")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.target))

    def test_a_run_hours_past_its_slot_is_refused(self):
        code, output = self.invoke(self.build(), "2026-03-02T14:00")
        self.assertEqual(code, 3)
        self.assertIn("LATE RUN REFUSED", output)
        self.assertIn("6h00m past its slot 2026-03-02T08:00", output)
        self.assertFalse(os.path.exists(self.target))

    def test_the_default_window_is_two_hours(self):
        roster = self.build()
        self.assertEqual(self.invoke(roster, "2026-03-02T10:01")[0], 3)
        self.assertEqual(routine_fleet.DEFAULT_REPLAY_WINDOW_MINUTES, 120)

    def test_a_refused_replay_does_not_spend_the_slot(self):
        roster = self.build()
        self.invoke(roster, "2026-03-02T14:00")
        self.assertFalse(os.path.exists(
            routine_fleet.State(self.state).marker_path(
                "brief", routine_fleet.parse_now("2026-03-02T08:00"))))

    def test_the_refusal_is_logged_and_the_slot_still_reads_missed(self):
        roster = self.build()
        self.invoke(roster, "2026-03-02T14:00")
        events = routine_fleet.State(self.state).events()[0]
        self.assertEqual([e["event"] for e in events], ["late-refused"])
        self.assertEqual(events[0]["late_minutes"], 360)
        code, output = self.run_cli("--roster", roster, "--state", self.state,
                                    "--now", "2026-03-02T15:00", "report")
        self.assertIn("MISSED", output)
        self.assertEqual(code, 1)

    def test_the_refusal_comes_before_the_work_check(self):
        """A run that will be refused must not pay for its check first."""
        probe = os.path.join(self.root, "checked")
        check = self.write_check("brief.sh", "touch %s; echo nothing-to-do" % probe)
        code, _ = self.invoke(self.build(work_check=check), "2026-03-02T14:00")
        self.assertEqual(code, 3)
        self.assertFalse(os.path.exists(probe))

    def test_the_window_is_set_per_roster_and_per_routine(self):
        roster = self.build(top={"replay_window_minutes": 480})
        self.assertEqual(self.invoke(roster, "2026-03-02T14:00")[0], 0)
        os.remove(self.target)
        roster = self.build(replay_window_minutes=30, top={"replay_window_minutes": 480})
        self.assertEqual(self.invoke(roster, "2026-03-03T08:31")[0], 3)

    def test_a_window_of_zero_turns_the_rule_off(self):
        roster = self.build(replay_window_minutes=0)
        self.assertEqual(self.invoke(roster, "2026-03-02T23:00")[0], 0)

    def test_allow_late_runs_a_deliberate_catch_up(self):
        code, _ = self.invoke(self.build(), "2026-03-02T14:00", "--allow-late")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.target))

    def test_allow_late_after_the_name_is_named_not_swallowed(self):
        code, output = self.run_cli("--roster", self.build(), "--state", self.state,
                                    "--now", "2026-03-02T14:00",
                                    "run", "brief", "--allow-late")
        self.assertEqual(code, 2)
        self.assertIn("--allow-late goes before `run`", output)
        self.assertFalse(os.path.exists(self.target))

    def test_a_bad_window_is_rejected(self):
        for bad in (-1, 5.5, "60", True):
            roster = self.write_roster([entry("brief", "0 8 * * *",
                                              replay_window_minutes=bad)],
                                       replay_window_minutes=bad)
            code, output = self.run_cli("--roster", roster, "validate")
            self.assertEqual(code, 1, "accepted replay_window_minutes=%r" % bad)
            self.assertIn("roster: `replay_window_minutes` must be a non-negative integer",
                          output)
            self.assertIn("routines[0]: `replay_window_minutes` must be a non-negative "
                          "integer", output)


class TestWorkCheckInTheReport(FleetCase):
    def roster_with_skip(self, records, **top):
        roster = self.write_roster([entry("brief", "0 8 * * *")], **top)
        state = self.write_log(records)
        return roster, state

    def report(self, roster, state, now="2026-03-02T09:00"):
        return self.run_cli("--roster", roster, "--state", state, "--now", now, "report")

    def test_a_skipped_slot_is_not_a_missed_one(self):
        roster, state = self.roster_with_skip([
            {"name": "brief", "slot": "2026-03-02T08:00", "event": "skipped",
             "at": "2026-03-02 08:05", "why": "nothing to do"}])
        code, output = self.report(roster, state)
        self.assertIn("SKIPPED", output)
        self.assertNotIn("MISSED", output)
        self.assertIn("ALL CLEAR", output)
        self.assertEqual(code, 0)

    def test_the_report_counts_the_skips_since_the_last_real_run(self):
        records = [{"name": "brief", "slot": "2026-02-%02dT08:00" % day,
                    "event": "skipped", "at": "2026-02-%02d 08:05" % day,
                    "why": "nothing to do"} for day in range(24, 29)]
        records.append({"name": "brief", "slot": "2026-03-02T08:00",
                        "event": "skipped", "at": "2026-03-02 08:05",
                        "why": "nothing to do"})
        roster, state = self.roster_with_skip(records)
        _, output = self.report(roster, state)
        self.assertIn("[6 since the last run]", output)

    def test_a_real_run_resets_the_count_and_outranks_a_skip_in_its_slot(self):
        roster, state = self.roster_with_skip([
            {"name": "brief", "slot": "2026-03-02T08:00", "event": "skipped",
             "at": "2026-03-02 08:01", "why": "nothing to do"},
            {"name": "brief", "slot": "2026-03-02T08:00", "event": "complete",
             "at": "2026-03-02 08:40", "exit": 0}])
        code, output = self.report(roster, state)
        self.assertIn("OK", output)
        self.assertNotIn("SKIPPED", output)
        self.assertEqual(code, 0)

    def test_one_slot_checked_three_times_counts_once(self):
        """Cron can fire more than once inside a slot. Three idle checks in one
        slot are one skipped slot, not three."""
        roster, state = self.roster_with_skip([
            {"name": "brief", "slot": "2026-03-02T08:00", "event": "skipped",
             "at": "2026-03-02 08:0%d" % n, "why": "nothing to do"} for n in (1, 3, 5)])
        _, output = self.report(roster, state)
        self.assertIn("[1 since the last run]", output)

    def test_a_slot_that_ran_is_not_counted_as_skipped(self):
        """Skipped early, then really ran, then a later slot went idle: one."""
        roster, state = self.roster_with_skip([
            {"name": "brief", "slot": "2026-03-01T08:00", "event": "skipped",
             "at": "2026-03-01 08:01", "why": "nothing to do"},
            {"name": "brief", "slot": "2026-03-01T08:00", "event": "complete",
             "at": "2026-03-01 08:40", "exit": 0},
            {"name": "brief", "slot": "2026-03-02T08:00", "event": "skipped",
             "at": "2026-03-02 08:05", "why": "nothing to do"}])
        _, output = self.report(roster, state)
        self.assertIn("[1 since the last run]", output)

    def test_a_log_line_with_no_slot_is_damaged_not_a_traceback(self):
        roster, state = self.roster_with_skip([
            {"name": "brief", "event": "skipped", "at": "2026-03-02 08:05"}])
        code, output = self.report(roster, state)
        self.assertIn("unreadable run-log line", output)
        self.assertEqual(code, 1)

    def test_a_line_that_is_not_json_is_counted_as_damaged(self):
        state = os.path.join(self.root, "state")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "run-log.jsonl"), "w") as log:
            log.write("{this is not json\n")
            log.write(json.dumps({"name": "brief", "slot": "2026-03-02T08:00",
                                  "event": "complete", "at": "2026-03-02 08:05",
                                  "exit": 0}) + "\n")
        roster = self.write_roster([entry("brief", "0 8 * * *")])
        code, output = self.report(roster, state)
        self.assertIn("1 unreadable run-log line", output)
        self.assertEqual(code, 1)

    def test_a_failed_run_after_skips_is_still_failed(self):
        roster, state = self.roster_with_skip([
            {"name": "brief", "slot": "2026-03-02T08:00", "event": "complete",
             "at": "2026-03-02 08:05", "exit": 4}])
        code, output = self.report(roster, state)
        self.assertIn("FAILED", output)
        self.assertEqual(code, 1)



if __name__ == "__main__":
    unittest.main()
