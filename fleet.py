#!/usr/bin/env python3
"""fleet.py — the operating pattern for a fleet of scheduled routines.

One roster is the source of truth. Every run goes through the guard, which
day-stamps its slot and refuses a twin. The watchdog reads only the roster and
the run log and says, per routine, whether the last due slot actually happened.
Parity diffs the live scheduler against the roster.

Subcommands: validate, run, report, parity, crontab. Zero dependencies.
Exit codes: 0 clean, 1 problems found, 2 usage/IO error, 3 twin refused.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

# A slot search walks back one minute at a time. Two months covers every
# monthly cadence; past that a routine has bigger problems than a late run.
MAX_LOOKBACK_MINUTES = 62 * 24 * 60
DEFAULT_GRACE_MINUTES = 30
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
CRON_TAG_RE = re.compile(r"#\s*fleet:([^\s#]+)\s*$")
ROUTINE_KEYS = {"name", "schedule", "runs", "owner", "command", "grace_minutes"}
ROSTER_KEYS = {"version", "timezone", "grace_minutes", "watchdog", "routines"}
CRON_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day-of-month", 1, 31),
               ("month", 1, 12), ("day-of-week", 0, 7))


class FleetError(Exception):
    """Anything the operator caused: a bad roster, a missing file, a bad flag."""


# ---------- cron ----------

def _parse_field(raw, label, lo, hi):
    values = set()
    for item in raw.split(","):
        if not item:
            raise FleetError("Expected `%s` to be a cron field, got an empty term in `%s`"
                             % (label, raw))
        step = 1
        if "/" in item:
            item, _, step_raw = item.partition("/")
            if not step_raw.isdigit() or int(step_raw) < 1:
                raise FleetError("Expected `%s` step to be a positive integer, got `%s`"
                                 % (label, step_raw))
            step = int(step_raw)
        if item == "*":
            start, end = lo, hi
        elif "-" in item.lstrip("-"):
            a, _, b = item.partition("-")
            start, end = _as_int(a, label, lo, hi), _as_int(b, label, lo, hi)
            if start > end:
                raise FleetError("Expected `%s` range to ascend, got `%s`" % (label, item))
        else:
            start = end = _as_int(item, label, lo, hi)
        values.update(range(start, end + 1, step))
    return values


def _as_int(raw, label, lo, hi):
    if not raw.isdigit():
        raise FleetError("Expected `%s` to be a number, got `%s`" % (label, raw))
    value = int(raw)
    if not lo <= value <= hi:
        raise FleetError("Expected `%s` in %d-%d, got %d" % (label, lo, hi, value))
    return value


class Cron(object):
    """A five-field cron expression, matched minute by minute."""

    def __init__(self, expr):
        parts = expr.split()
        if len(parts) != 5:
            raise FleetError("Expected a 5-field cron expression, got `%s` (%d fields)"
                             % (expr, len(parts)))
        self.expr = " ".join(parts)
        fields = [_parse_field(raw, label, lo, hi)
                  for raw, (label, lo, hi) in zip(parts, CRON_FIELDS)]
        self.minute, self.hour, self.dom, self.month, self.dow = fields
        if 7 in self.dow:
            self.dow = (self.dow - {7}) | {0}
        self.dom_bound = parts[2] != "*"
        self.dow_bound = parts[4] != "*"

    def matches(self, when):
        if when.minute not in self.minute or when.hour not in self.hour:
            return False
        if when.month not in self.month:
            return False
        dom_ok = when.day in self.dom
        dow_ok = ((when.weekday() + 1) % 7) in self.dow
        # Vixie cron: when both day fields are restricted, either one firing is enough.
        if self.dom_bound and self.dow_bound:
            return dom_ok or dow_ok
        if self.dom_bound:
            return dom_ok
        if self.dow_bound:
            return dow_ok
        return True

    def previous_slot(self, when):
        """The latest scheduled minute at or before `when`, or None if the
        expression has not fired inside the lookback window."""
        cursor = when.replace(second=0, microsecond=0)
        for _ in range(MAX_LOOKBACK_MINUTES):
            if self.matches(cursor):
                return cursor
            cursor -= timedelta(minutes=1)
        return None


# ---------- roster ----------

class Routine(object):
    def __init__(self, entry, roster_dir, default_grace):
        self.name = entry["name"]
        self.schedule = entry["schedule"]
        self.cron = Cron(self.schedule)
        self.owner = entry["owner"]
        self.runs = entry["runs"]
        self.runs_path = os.path.join(roster_dir, self.runs)
        self.command = entry.get("command")
        self.grace = entry.get("grace_minutes", default_grace)

    def argv(self):
        """The command the guard executes. `{path}` expands to the resolved
        prompt/script path, so a roster names one runner for many prompts."""
        template = self.command or [self.runs_path]
        return [arg.replace("{path}", self.runs_path) for arg in template]


class Roster(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.dir = os.path.dirname(self.path)
        try:
            with open(self.path) as handle:
                raw = json.load(handle)
        except IOError:
            raise FleetError("Roster not found: %s" % path)
        except ValueError as exc:
            raise FleetError("Roster is not valid JSON: %s (%s)" % (path, exc))
        self.problems = validate_roster(raw)
        if self.problems:
            raise FleetError("Roster is invalid; run `validate` for the list")
        self.watchdog = raw.get("watchdog")
        grace = raw.get("grace_minutes", DEFAULT_GRACE_MINUTES)
        self.routines = [Routine(e, self.dir, grace) for e in raw["routines"]]

    def get(self, name):
        for routine in self.routines:
            if routine.name == name:
                return routine
        raise FleetError("`%s` is not in the roster; a routine not in the roster "
                         "does not exist" % name)

    def ordered(self):
        """Roster order, except the watchdog, which checks itself last."""
        body = [r for r in self.routines if r.name != self.watchdog]
        tail = [r for r in self.routines if r.name == self.watchdog]
        return body + tail


def validate_roster(raw):
    """Structure only. Missing prompt files are rot, and rot is the watchdog's job."""
    problems = []
    if not isinstance(raw, dict):
        return ["roster: expected an object, got %s" % type(raw).__name__]
    for key in sorted(set(raw) - ROSTER_KEYS):
        problems.append("roster: unknown key `%s`" % key)
    entries = raw.get("routines")
    if not isinstance(entries, list) or not entries:
        return problems + ["roster: `routines` must be a non-empty list"]
    seen = set()
    for index, entry in enumerate(entries):
        where = "routines[%d]" % index
        if not isinstance(entry, dict):
            problems.append("%s: expected an object" % where)
            continue
        for key in sorted(set(entry) - ROUTINE_KEYS):
            problems.append("%s: unknown key `%s`" % (where, key))
        name = entry.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            problems.append("%s: `name` must match %s, got %r" % (where, NAME_RE.pattern, name))
        elif name in seen:
            problems.append("%s: duplicate name `%s`" % (where, name))
        else:
            seen.add(name)
        for key in ("runs", "owner"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                problems.append("%s: `%s` must be a non-empty string" % (where, key))
        schedule = entry.get("schedule")
        if not isinstance(schedule, str):
            problems.append("%s: `schedule` must be a cron string" % where)
        else:
            try:
                Cron(schedule)
            except FleetError as exc:
                problems.append("%s: %s" % (where, exc))
        command = entry.get("command")
        if command is not None and (not isinstance(command, list) or not command
                                    or not all(isinstance(a, str) for a in command)):
            problems.append("%s: `command` must be a non-empty list of strings" % where)
        grace = entry.get("grace_minutes")
        if grace is not None and (not isinstance(grace, int) or isinstance(grace, bool)
                                  or grace < 0):
            problems.append("%s: `grace_minutes` must be a non-negative integer" % where)
    watchdog = raw.get("watchdog")
    if watchdog is not None and watchdog not in seen:
        problems.append("roster: `watchdog` names `%s`, which is not a routine" % watchdog)
    return problems


# ---------- state: markers and the run log ----------

SLOT_FMT = "%Y-%m-%dT%H:%M"


class State(object):
    def __init__(self, directory):
        self.dir = os.path.abspath(directory)
        self.log_path = os.path.join(self.dir, "run-log.jsonl")

    def marker_path(self, name, slot):
        return os.path.join(self.dir, "markers", name, slot.strftime(SLOT_FMT) + ".marker")

    def claim(self, name, slot, stamp):
        """Create the slot marker, or report the slot is already taken. The
        exclusive create is the whole twin defence: two schedulers racing the
        same minute cannot both win it."""
        path = self.marker_path(name, slot)
        try:
            os.makedirs(os.path.dirname(path))
        except OSError:
            pass
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            return False
        with os.fdopen(handle, "w") as marker:
            marker.write(stamp + "\n")
        return True

    def append(self, record):
        try:
            os.makedirs(self.dir)
        except OSError:
            pass
        with open(self.log_path, "a") as log:
            log.write(json.dumps(record, sort_keys=True) + "\n")

    def events(self):
        """Parsed log records, newest last. Unreadable lines are counted, never
        guessed at: a corrupt log must not silently look like a healthy one."""
        records, damaged = [], 0
        if not os.path.exists(self.log_path):
            return records, damaged
        with open(self.log_path) as log:
            for line in log:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    damaged += 1
                    continue
                if isinstance(record, dict) and "name" in record and "event" in record:
                    records.append(record)
                else:
                    damaged += 1
        return records, damaged


# ---------- run: the guard ----------

def cmd_run(args, roster, state):
    routine = roster.get(args.name)
    now = args.now
    slot = routine.cron.previous_slot(now)
    if slot is None:
        raise FleetError("`%s` has no scheduled slot at or before %s"
                         % (routine.name, now.strftime(SLOT_FMT)))
    slot_id = slot.strftime(SLOT_FMT)
    stamp = now.isoformat(sep=" ", timespec="seconds")
    if not state.claim(routine.name, slot, stamp):
        state.append({"name": routine.name, "slot": slot_id, "event": "twin-refused",
                      "at": stamp})
        sys.stderr.write(
            "!!! TWIN REFUSED: %s already ran in slot %s.\n"
            "    Refusing to run it twice. Clear %s to force a re-run.\n"
            % (routine.name, slot_id, state.marker_path(routine.name, slot)))
        return 3
    argv = args.command or routine.argv()
    state.append({"name": routine.name, "slot": slot_id, "event": "start", "at": stamp})
    env = dict(os.environ, FLEET_ROUTINE=routine.name, FLEET_SLOT=slot_id,
               FLEET_RUNS=routine.runs_path)
    try:
        code = subprocess.call(argv, cwd=roster.dir, env=env)
    except OSError as exc:
        code = 127
        sys.stderr.write("!!! %s could not start: %s\n" % (routine.name, exc))
    state.append({"name": routine.name, "slot": slot_id, "event": "complete",
                  "at": stamp, "ended": datetime.now().isoformat(sep=" ", timespec="seconds"),
                  "exit": code})
    return code


# ---------- report: the watchdog ----------

FLAGGED = ("MISSED", "FAILED", "INCOMPLETE", "ROTTED", "NO-SLOT")


def assess(routine, now, by_slot, twins):
    if not os.path.exists(routine.runs_path):
        return "ROTTED", "%s is missing" % routine.runs
    due = routine.cron.previous_slot(now - timedelta(minutes=routine.grace))
    if due is None:
        return "NO-SLOT", "no slot in the last %d days" % (MAX_LOOKBACK_MINUTES // 1440)
    slot_id = due.strftime(SLOT_FMT)
    detail = "slot %s" % slot_id.replace("T", " ")
    if twins:
        detail += "  [%d twin refused]" % twins
    record = by_slot.get(slot_id)
    if record is None:
        return "MISSED", detail + "  no run recorded"
    if record["event"] != "complete":
        return "INCOMPLETE", detail + "  started, never completed"
    if record.get("exit") != 0:
        return "FAILED", detail + "  exit %s" % record.get("exit")
    return "OK", detail


def cmd_report(args, roster, state):
    records, damaged = state.events()
    now = args.now
    lines, flagged, twin_total = [], 0, 0
    for routine in roster.ordered():
        mine = [r for r in records if r["name"] == routine.name]
        by_slot = {}
        for record in mine:
            if record["event"] in ("start", "complete"):
                by_slot[record["slot"]] = record
        twins = sum(1 for r in mine if r["event"] == "twin-refused")
        twin_total += twins
        status, detail = assess(routine, now, by_slot, twins)
        if status in FLAGGED:
            flagged += 1
        suffix = "  (watchdog, checked last)" if routine.name == roster.watchdog else ""
        lines.append("  %-10s %-24s %s%s" % (status, routine.name, detail, suffix))
    total = len(roster.routines)
    unhealthy = flagged or twin_total or damaged
    print("FLEET REPORT  %s  %d routines  roster %s"
          % (now.strftime("%Y-%m-%d %H:%M"), total, os.path.basename(roster.path)))
    if flagged:
        print("!!! ATTENTION: %d of %d routines flagged" % (flagged, total))
    elif unhealthy:
        print("!!! ATTENTION: fleet ran, but something needs a human")
    else:
        print("ALL CLEAR")
    print("")
    print("\n".join(lines))
    if twin_total:
        print("\n%d twin run(s) refused. Something is invoking a routine twice per slot."
              % twin_total)
    if damaged:
        print("\n%d unreadable run-log line(s). The log is the only evidence there is."
              % damaged)
    return 1 if unhealthy else 0


# ---------- parity ----------

def read_crontab(source):
    """Fleet-managed crontab lines carry a `# fleet:<name>` tag. Untagged lines
    belong to someone else and are none of the roster's business."""
    text = _read_source(source, ["crontab", "-l"])
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tag = CRON_TAG_RE.search(line)
        if tag:
            found[tag.group(1)] = " ".join(line.split()[:5])
    return found


def read_json_export(source):
    """A scheduler export: a list of {name, schedule} objects, or a name->schedule map."""
    try:
        data = json.loads(_read_source(source, None))
    except ValueError as exc:
        raise FleetError("Scheduler export is not valid JSON: %s" % exc)
    if isinstance(data, dict) and "entries" in data:
        data = data["entries"]
    if isinstance(data, dict):
        return dict((k, " ".join(str(v).split())) for k, v in data.items())
    if isinstance(data, list):
        found = {}
        for entry in data:
            if not isinstance(entry, dict) or "name" not in entry or "schedule" not in entry:
                raise FleetError("Expected each export entry to carry `name` and `schedule`, "
                                 "got %r" % (entry,))
            found[entry["name"]] = " ".join(str(entry["schedule"]).split())
        return found
    raise FleetError("Expected the export to be a list or an object, got %s"
                     % type(data).__name__)


def _read_source(source, fallback_argv):
    if source == "-":
        return sys.stdin.read()
    if source:
        try:
            with open(source) as handle:
                return handle.read()
        except IOError as exc:
            raise FleetError("Cannot read scheduler state: %s" % exc)
    if fallback_argv is None:
        raise FleetError("This adapter needs `--source <file>` or `-` for stdin")
    try:
        return subprocess.check_output(fallback_argv).decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FleetError("Cannot read the live scheduler (%s): %s"
                         % (" ".join(fallback_argv), exc))


ADAPTERS = {"crontab": read_crontab, "json": read_json_export}


def diff_parity(roster, live):
    """Three columns of drift, each named. Missing = the fleet lost a routine on
    this machine; extra = something schedules work the roster never approved."""
    rows = []
    wanted = dict((r.name, r.cron.expr) for r in roster.routines)
    for name in sorted(wanted):
        if name not in live:
            rows.append(("MISSING", name, 'roster "%s", not scheduled here' % wanted[name]))
        elif live[name] != wanted[name]:
            rows.append(("DRIFTED", name, 'roster "%s", scheduler "%s"'
                         % (wanted[name], live[name])))
    for name in sorted(set(live) - set(wanted)):
        rows.append(("EXTRA", name, 'scheduler "%s", not in the roster' % live[name]))
    return rows


def cmd_parity(args, roster, _state):
    live = ADAPTERS[args.adapter](args.source)
    rows = diff_parity(roster, live)
    print("PARITY  roster %s  vs  %s %s"
          % (os.path.basename(roster.path), args.adapter, args.source or "(live)"))
    if not rows:
        print("IN SYNC: %d routines, no drift." % len(roster.routines))
        return 0
    print("!!! %d difference(s)\n" % len(rows))
    for status, name, detail in rows:
        print("  %-8s %-24s %s" % (status, name, detail))
    return 1


# ---------- crontab generation ----------

def cmd_crontab(args, roster, _state):
    # Cron needs paths as the machine will see them, which is rarely where the
    # roster is being edited — hence one install directory for both.
    install = os.path.abspath(args.install_dir) if args.install_dir else roster.dir
    runner = os.path.join(install, "fleet.py")
    roster_path = os.path.join(install, os.path.basename(roster.path))
    print("# Generated by `fleet.py crontab` from %s — edit the roster, not this block."
          % os.path.basename(roster.path))
    print("# Every line carries a `# fleet:<name>` tag; `fleet.py parity` reads those tags.")
    for routine in roster.routines:
        print('%s %s --roster %s run %s  # fleet:%s'
              % (routine.cron.expr, runner, roster_path, routine.name, routine.name))
    return 0


def cmd_validate(args, _roster, _state):
    with open(args.roster) as handle:
        problems = validate_roster(json.load(handle))
    if problems:
        print("!!! %s: %d problem(s)\n" % (args.roster, len(problems)))
        for problem in problems:
            print("  %s" % problem)
        return 1
    print("OK %s: roster is well formed." % args.roster)
    return 0


# ---------- cli ----------

def parse_now(raw):
    if raw is None:
        return datetime.now()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise FleetError("Expected `--now` as YYYY-MM-DDTHH:MM, got `%s`" % raw)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--roster", default="fleet.json", help="the canonical roster")
    parser.add_argument("--state", help="marker and run-log directory (default: <roster dir>/state)")
    parser.add_argument("--now", help="evaluate against this timestamp instead of the clock")
    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser("validate", help="lint the roster")

    run = subparsers.add_parser("run", help="run one routine through the guard")
    run.add_argument("name")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="optional `-- argv` overriding the roster command")

    subparsers.add_parser("report", help="the watchdog: did every routine run?")

    parity = subparsers.add_parser("parity", help="diff the live scheduler against the roster")
    parity.add_argument("--adapter", choices=sorted(ADAPTERS), default="crontab")
    parity.add_argument("--source", help="file to read, `-` for stdin, omit for `crontab -l`")

    crontab = subparsers.add_parser("crontab", help="print a crontab block from the roster")
    crontab.add_argument("--install-dir",
                         help="where fleet.py and the roster live on the scheduling machine")
    return parser


HANDLERS = {"validate": cmd_validate, "run": cmd_run, "report": cmd_report,
            "parity": cmd_parity, "crontab": cmd_crontab}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.subcommand:
        parser.print_help()
        return 2
    if getattr(args, "command", None) and args.command[0] == "--":
        args.command = args.command[1:]
    try:
        args.now = parse_now(args.now)
        # validate must run against an unparsed roster: that is the whole point of it.
        roster = None if args.subcommand == "validate" else Roster(args.roster)
        state_dir = args.state or os.path.join(
            os.path.dirname(os.path.abspath(args.roster)), "state")
        return HANDLERS[args.subcommand](args, roster, State(state_dir))
    except FleetError as exc:
        sys.stderr.write("fleet: %s\n" % exc)
        return 2
    except (IOError, ValueError) as exc:
        sys.stderr.write("fleet: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
