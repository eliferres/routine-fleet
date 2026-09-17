"""The demo receipt: demo/transcript.json must be what the walkthrough really prints.

Every entry is replayed with bash inside a throwaway copy of the repo, under the
environment the README walkthrough sets up, and compared byte for byte after
machine paths become placeholders. The picture drawn from the transcript is
checked too, so neither can drift from a real run.

To regenerate the transcript from a real run:
    UPDATE_DEMO_TRANSCRIPT=1 python3 -m unittest tests.test_demo_transcript
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRANSCRIPT = os.path.join(REPO, "demo", "transcript.json")
PICTURE = os.path.join(REPO, "demo", "terminal.svg")
SVG = "{http://www.w3.org/2000/svg}"
ELLIPSIS = "…"
MACHINE_PATHS = ("/var/folders", "/private/var", "/tmp/", "/Users/", "/home/")


def load_transcript():
    with open(TRANSCRIPT, encoding="utf-8") as handle:
        return json.load(handle)


class TestDemoTranscript(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp(prefix="fleet-demo-")
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.checkout = os.path.join(self.scratch, "checkout")
        shutil.copytree(REPO, self.checkout,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "dist",
                                                      "*.egg-info", "markers"))
        # The walkthrough's `export FLEET_GUARD=... FLEET_STATE=...` and its `cp`.
        self.dirs = {}
        for name in ("fleet-guard", "fleet-state"):
            self.dirs[name] = os.path.join(self.scratch, name)
            os.makedirs(self.dirs[name])
        shutil.copy(os.path.join(REPO, "demo", "state", "run-log.jsonl"), self.dirs["fleet-state"])

    def placeholders(self):
        """Machine path -> stable placeholder, longest first so no path is half
        replaced. macOS hands out /var/... and resolves it to /private/var/..."""
        pairs = [(self.checkout, "/path/to/checkout")]
        pairs += [(path, "/path/to/" + name) for name, path in self.dirs.items()]
        both = []
        for path, placeholder in pairs:
            both.append((path, placeholder))
            both.append((os.path.realpath(path), placeholder))
        return sorted(set(both), key=lambda pair: -len(pair[0]))

    def replay(self, cmd):
        # `python3` in the transcript must be the interpreter running this suite.
        env = dict(os.environ, FLEET_GUARD=self.dirs["fleet-guard"],
                   FLEET_STATE=self.dirs["fleet-state"], PYTHONIOENCODING="utf-8",
                   PATH=self.python3_shim() + os.pathsep + os.environ.get("PATH", ""))
        result = subprocess.run(["bash", "-c", cmd], cwd=self.checkout, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = result.stdout.decode("utf-8").rstrip("\n")
        for path, placeholder in self.placeholders():
            out = out.replace(path, placeholder)
        return {"cmd": cmd, "out": out, "status": result.returncode}

    def python3_shim(self):
        shim_dir = os.path.join(self.scratch, "bin")
        shim = os.path.join(shim_dir, "python3")
        if not os.path.exists(shim):
            os.makedirs(shim_dir)
            os.symlink(sys.executable, shim)
        return shim_dir

    def test_transcript_matches_a_real_run(self):
        expected = load_transcript()
        actual = [self.replay(entry["cmd"]) for entry in expected]
        if os.environ.get("UPDATE_DEMO_TRANSCRIPT") == "1":
            with open(TRANSCRIPT, "w", encoding="utf-8") as handle:
                json.dump(actual, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            return
        for index, (want, got) in enumerate(zip(expected, actual), start=1):
            label = "entry %d `%s`" % (index, want["cmd"])
            self.assertEqual(want["out"], got["out"],
                             "%s: out differs\nexpected:\n%s\nactual:\n%s"
                             % (label, want["out"], got["out"]))
            self.assertEqual(want["status"], got["status"],
                             "%s: status expected %r, actual %r"
                             % (label, want["status"], got["status"]))

    def test_transcript_carries_no_machine_path(self):
        for entry in load_transcript():
            for fragment in MACHINE_PATHS:
                self.assertNotIn(fragment, entry["out"], entry["cmd"])

    def test_every_picture_row_comes_from_the_transcript(self):
        transcript = load_transcript()
        commands = [entry["cmd"] for entry in transcript]
        out_lines = [line for entry in transcript for line in entry["out"].splitlines()]
        rows = ET.parse(PICTURE).getroot().findall(SVG + "text")[1:]  # [0] is the title bar
        pending = []  # the chunks of a command wrapped across rows
        for row in rows:
            spans = row.findall(SVG + "tspan")
            if spans:
                pending = [spans[-1].text]
            elif row.get("class") == "cmd":
                self.assertTrue(pending and row.text.startswith("    "),
                                "continuation row without a command: %r" % row.text)
                pending.append(row.text[4:])
            else:
                text = row.text
                for fragment in MACHINE_PATHS:
                    self.assertNotIn(fragment, text)
                if text.endswith(ELLIPSIS):
                    text = text[:-1]
                self.assertTrue(any(line.startswith(text) for line in out_lines),
                                "picture row is not in the transcript: %r" % row.text)
                continue
            if not pending[-1].endswith(" \\"):
                joined = " ".join(chunk[:-2] if chunk.endswith(" \\") else chunk
                                  for chunk in pending)
                self.assertIn(joined, commands, "picture command is not in the transcript")
                pending = []
        self.assertEqual(pending, [], "picture ends inside a wrapped command")


if __name__ == "__main__":
    unittest.main()
