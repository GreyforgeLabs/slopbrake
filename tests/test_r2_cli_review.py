"""Round-2 review follow-ups for cli: the B7 status gap below a covered top-level dir, test discovery for
the no-tests hint, listed ratchet-ref deletions and verify's SIGTERM reaching the gate's grandchildren."""
import os
import signal
import subprocess
import sys
import time
import unittest

from test_r2_cli import SCRUBBED, Scratch, sh


def outside(status):
    return [gap for gap in status["gaps"] if "outside every scripts/check glob" in gap]


class NestedLayoutGaps(Scratch):
    def ts_repo(self, *files):
        self.git_repo()
        self.write("package.json", '{"name": "t", "scripts": {"typecheck": "tsc --noEmit"}}')
        self.write("tsconfig.json", "{}")
        self.write("eslint.config.mjs", "import slopbrake from './eslint-rules/slopbrake.mjs';\nexport default [];\n")
        for rel in files:
            self.write(rel, "export const x = 1;\n")
        result = self.gf("init", str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.commit("install slopbrake")

    def test_dirs_below_a_covered_top_level_dir_but_outside_its_globs_are_gaps(self):
        self.ts_repo("packages/a/src/index.ts", "packages/b/src/index.ts", "test/a.test.ts")
        self.assertEqual(outside(self.status()), [])
        self.write("packages/a/tests/a.test.ts", "export const t = 1;\n")  # outside TEST_GLOBS: no T1
        self.write("packages/c/lib/index.ts", "export const c = 1;\n")  # outside --include: no T4
        self.write("packages/c/lib/deep/more.ts", "export const d = 1;\n")
        sh(["git", "add", "-A"], self.root)  # coverage counts tracked sources only
        gaps = outside(self.status())
        self.assertEqual(len(gaps), 1, gaps)
        self.assertIn("packages/a/tests/, packages/c/lib/;", gaps[0])

    def test_helpers_inside_a_covered_test_dir_are_not_gaps(self):
        self.ts_repo("src/index.ts", "tests/index.test.ts", "tests/helpers/fixtures.ts", "src/nested/deep.ts")
        self.assertEqual(outside(self.status()), [])


class TestDiscovery(Scratch):
    def test_a_unittest_discoverable_name_without_underscore_counts_as_a_test(self):
        self.git_repo()
        self.write("pyproject.toml", "[project]\nname = 'shop'\n")
        self.write("tests/testshop.py", "import unittest\n")
        steps = self.init_json()["next_steps"]
        self.assertFalse(any("add a first test" in s for s in steps), steps)


class RatchetDeletions(Scratch):
    def test_init_lists_the_old_ratchet_refs_it_deletes(self):
        self.python_repo()
        sh(["git", "update-ref", "refs/slopbrake/last-green", "HEAD"], self.root)  # pre-A1 flat ref
        sh(["git", "update-ref", "-d", "refs/slopbrake/last-green"], self.root)
        sh(["git", "update-ref", "refs/slopbrake/last-green/main/old", "HEAD"], self.root)  # pre-B1 nested ref
        for extra in (["--dry-run"], []):
            actions = {(a["path"], a["action"]) for a in self.init_json(*extra)["actions"]}
            self.assertIn(("refs/slopbrake/last-green/main/old", "delete"), actions, extra)
            self.assertIn(("refs/slopbrake/last-green/main", "create"), actions, extra)
        self.assertEqual(self.refs(), ["refs/slopbrake/last-green/main"])

    def test_the_human_output_names_the_deletion(self):
        self.python_repo()
        sh(["git", "update-ref", "refs/slopbrake/last-green/main/old", "HEAD"], self.root)
        out = self.gf("init", str(self.root), "--dry-run").stdout
        self.assertIn("delete  refs/slopbrake/last-green/main/old", out)


class VerifySigterm(Scratch):
    def test_sigterm_stops_the_gate_s_grandchildren(self):
        pidfile = self.root.parent / "grandchild.pid"
        self.python_repo()
        self.assertEqual(self.gf("init", str(self.root)).returncode, 0)
        self.write("scripts/check", f"#!/bin/sh\nsleep 300 &\necho $! > '{pidfile}'\nwait\n").chmod(0o755)
        self.commit("install slopbrake")
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
        env.update(self.env)
        proc = subprocess.Popen([sys.executable, "-m", "slopbrake", "verify", str(self.root)], cwd=self.root, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        grandchild = None
        try:
            deadline = time.monotonic() + 60
            while not pidfile.is_file() or not pidfile.read_text().strip():
                self.assertLess(time.monotonic(), deadline, "the gate never started")
                time.sleep(0.1)
            grandchild = int(pidfile.read_text())
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=30)
            deadline = time.monotonic() + 10
            while alive(grandchild) and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(alive(grandchild), "the gate's background child outlived verify")
        finally:
            if proc.poll() is None:
                proc.kill()
            if grandchild and alive(grandchild):
                os.kill(grandchild, signal.SIGKILL)
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1)
        self.assertEqual(list(self.tmpdir.iterdir()), [])


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is dead for our purposes
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
