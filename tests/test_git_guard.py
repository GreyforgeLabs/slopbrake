"""Behaviour tests for the H5 git guard: the hook wrapper with real hook JSON, and check_command()."""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
HOOKS = HOME / "slopbrake/kit/common/.claude/hooks"
WRAPPER = HOOKS / "block-dangerous-git.sh"
GUARD = HOOKS / "git_guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("git_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hook(command, cwd=None, env=None, argv=None, raw=None):
    payload = raw if raw is not None else json.dumps({"tool_input": {"command": command}, "cwd": cwd or str(HOME)})
    return subprocess.run(argv or [str(WRAPPER)], input=payload, capture_output=True, text=True, env=env, check=False)


def hook_all(commands):
    """Run the wrapper for many commands concurrently; {command: CompletedProcess}."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(zip(commands, pool.map(hook, commands)))


# The cases verify's H5 proof shares (FIX-SPEC C5); they must keep these exits.
SHARED_BLOCK = [
    "git push --force origin main", "git push -f;echo done", 'bash -c "git reset --hard"', "git clean -fd",
    "git branch -D old", "git checkout .", "git restore .", "git commit --no-verify -m x",
    "git -c core.hooksPath=/dev/null commit -m x", "git push origin +main",
]
SHARED_ALLOW = [
    "git push origin feature/x", "git status", "git restore --staged .", 'git commit -m "never run git reset --hard"',
    'grep -rn "reset --hard" docs', "git push -u origin feature/x",
]

BLOCK = {
    "F1 force push glued to an operator or quote": [
        "git push --force;echo done", "git push -f;", 'bash -c "git push --force"', "sh -c 'git push --force'",
        "(git push -f)", "$(git push --force)", "git push --force&&echo ok", "git push -fu origin main",
        "git push -uf origin main", "git push -f|cat", "git push -f&", "git push -f)", "echo `git push -f`",
        'echo "$(git reset --hard)"', "git push --force # comment",
    ],
    "F2 equivalents": [
        "git restore --staged --worktree .", "git restore -SW .", "git restore -WS .", "git restore -- .",
        "git restore ./", "git checkout HEAD .", "git checkout -f", "git branch -df x", "git branch -fd x",
        "git branch --force -d x", "git branch --delete --force x", "git branch -d --force x", 'git push origin "+main"',
        "git push origin '+main'", "git push --mirror", "git push origin :main", "git push --delete origin main",
        "git push -d origin main", "git -P push -f", 'git -C "my dir" push --force', '"git" push -f',
        "\\git push -f", "git push origin +refs/heads/main:refs/heads/main", "git push origin main:main +dev",
    ],
    "F7 hook bypasses": [
        "git commit --no-verify -am wip", "git push --no-verify origin main", "git config core.hooksPath /dev/null",
        "git commit -n -m x", "git commit -anm x", "git commit -nm x", "git -c core.hooksPath=/dev/null push",
        "git -c core.hookspath= commit -m x", "git --config-env=core.hooksPath=HP commit -m x",
        "git config --global core.hooksPath x", "git config --local core.hooksPath x", "git config --unset core.hooksPath",
        "git config --unset-all core.hooksPath", "git config core.HooksPath x", "git config set core.hooksPath x",
        "git config unset core.hooksPath", "git config --remove-section core", "git merge --no-verify x",
        "git rebase --no-verify main", "git am --no-verify p.patch", "git commit --no-verif -m x",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit -m x",
    ],
    "F8 stale-pilot bypasses": [
        "git -c core.editor=vi push --force", "git --no-pager push -f", "git checkout HEAD -- .", "git\tpush\t-f",
        "git  push  --force", "git clean --force -d",
    ],
    "the rest of the C5 list": [
        "git push --force-with-lease", "git push --force-with-lease=main:abc origin main", "git push --force-if-includes",
        "git push --prune origin", "git push --forc origin main", "git reset --hard", "git reset --hard HEAD~1",
        "git reset --merge", "git clean --force", "git clean -xdf", "git clean -f -x", "git branch -M a b",
        "git branch --move --force a b", "git checkout --force main", "git checkout -- ./", "git checkout ':/'",
        "git checkout '*'", "git checkout ':(top)'", "git checkout main -- .", "git checkout -- ..",
        "git checkout -- ':!docs'", "git checkout HEAD~1 -- ./src/..", "git restore src/../.", "git restore -W .", "git restore --worktree :/", "git restore --source=HEAD~2 .",
        "git restore -s HEAD .", "git restore -sHEAD .", "git stash drop", "git stash clear", "git stash drop stash@{0}",
        "git update-ref -d refs/heads/x", "git reflog expire --expire=now --all", "git reflog delete HEAD@{1}",
        "git gc --prune=now", "git gc --prune=all", "git switch -f main", "git switch --discard-changes main",
    ],
    "wrappers, absolute paths and env prefixes": [
        "command git push -f", "exec git push -f", "env FOO=1 git push -f", "env -i git push -f", "nohup git push -f",
        "time git push -f", "time -p git push -f", "sudo git push -f", "sudo -u bob git push -f", "FOO=1 git push -f",
        "/usr/bin/git reset --hard", "./git push -f", "nice -n 5 git push -f", "timeout 10 git push -f",
        "xargs git branch -D < list", "env -S 'git push -f'", "env -S'git push -f'", "env -S 'git push -f \"' ", "! git push -f", "doas git push -f",
    ],
    "global options before the subcommand": [
        "git -C . -c a=b --no-pager push -f", "git --git-dir=.git --work-tree=. reset --hard",
        "git --git-dir .git reset --hard", "git --exec-path=/x push -f", "git --no-optional-locks clean -fd",
        "git -C sub -C deeper reset --hard", "git --literal-pathspecs checkout .",
    ],
    "shell payloads and command structure": [
        "eval 'git reset --hard'", "eval git reset --hard", 'bash -lc "git reset --hard"', 'bash -e -c "git reset --hard"',
        "zsh -c 'git clean -fd'", "bash -c 'bash -c \"git push -f\"'", "true || git reset --hard", "true | git reset --hard",
        "sleep 1 & git reset --hard", "cd x\ngit reset --hard", "git status\ngit push -f", "{ git push -f; }",
        "if true; then git reset --hard; fi", "for b in a c; do git branch -D $b; done", "git \\\n push -f",
        "bash <<EOF\ngit reset --hard\nEOF", "bash -s <<'EOF'\ngit clean -fd\nEOF", 'bash <<< "git reset --hard"',
        'echo "git reset --hard" | bash', "printf 'git push -f' | sh", "cat <<EOF > out\n$(git reset --hard)\nEOF",
        "cat <<EOF > out\nhi\nEOF\ngit reset --hard", "diff <(git reset --hard) x", "echo $(echo $(git push -f))",
    ],
    "git-run commands": [
        "git -c alias.nuke='reset --hard' nuke", "git -c alias.p='!git push -f' p", "git submodule foreach 'git reset --hard'",
        "git rebase -x 'git reset --hard' main", "git rebase --exec='git clean -fd' main", "git prune",
    ],
    "unparseable commands fall back to the raw-string check": [
        'git push -f "', "git reset --hard\necho 'unterminated", 'echo "$(git clean -fd', "git commit --no-verify 'x",
    ],
    "options between a shell's -c and its payload": [
        "bash -c -- 'git push -f'", "bash -c -x 'git push -f'", "bash -c -o pipefail 'git reset --hard'",
        "sh -c +e 'git push -f'", "bash -co pipefail 'git push -f'", "bash -eo pipefail -c 'git push -f'",
        "bash --norc -c 'git push -f'", "bash -c -e -- 'git clean -fd'",
    ],
    "heredocs and here-strings piped through cat into a shell": [
        "cat <<'EOF' | bash\ngit push -f\nEOF", "cat <<EOF2 | sh\ngit reset --hard\nEOF2", "cat <<< 'git push -f' | bash",
        "cat - <<'EOF' | bash -s\ngit clean -fd\nEOF",
        "echo 'git push -f' | bash -", "cat <<'EOF' | sh -\ngit push -f\nEOF",
    ],
    "exported git config": [
        "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null; git commit -m x",
        "export GIT_CONFIG_KEY_0=core.hooksPath\ngit commit -m x", "declare -x GIT_CONFIG_KEY_0=core.hooksPath; git commit -m x",
        "export GIT_CONFIG_KEY_0=core.hooksPath; bash -c 'git commit -m x'",
    ],
    "config-driven and indirect equivalents": [
        "git pull --no-verify", "git pull --rebase --no-verify origin main", "git -c clean.requireForce=false clean -d",
        "git -c clean.requireforce=0 clean -dx", "git -c remote.origin.mirror=true push origin",
        "git config alias.nuke '!git reset --hard'", "git config alias.nuke 'reset --hard'",
        "git config --global alias.p 'push -f'", "git config set alias.c 'commit --no-verify'",
        "stdbuf -oL git push -f", "stdbuf -o L git push -f", "setsid git push -f", "setsid -f git reset --hard",
        "coproc git push -f", "source <(echo git push -f)", ". <(echo 'git push -f')", "bash <(echo 'git push -f')",
        "bash < <(echo 'git push -f')", "git checkout --pathspec-from-file=- <<< .", "git restore --pathspec-from-file=list.txt",
    ],
    "command substitutions run as the command or its arguments": [
        "$(echo git push -f)", "`echo git push -f`", 'eval "$(echo git push -f)"', "eval $(echo git push -f)",
        "eval \"$(cat <<'EOF'\ngit push -f\nEOF\n)\"", "bash -c \"$(cat <<'EOF'\ngit push -f\nEOF\n)\"",
        "bash -c \"$(cat <<'EOF'\ncd /tmp\ngit reset --hard\nEOF\n)\"", "$(echo git) push -f", "sudo $(echo git push -f)",
        "command `echo git reset --hard`", "git $(echo push -f)", "git $(echo push) -f", 'eval "$(printf \'git clean -fd\')"',
    ],
}

ALLOW = {
    "F3 text that only mentions a command": [
        'git commit -m "docs: never run git reset --hard"', 'grep -rn "reset --hard" docs/', 'echo "git reset --hard"',
        'gh pr create --body "avoid git branch -D x"', "cat > notes.md <<'EOF'\nNever run git checkout . here\ngit reset --hard\nEOF",
        "cat > n.md <<EOF\ngit push --force\nEOF", "cat <<-EOF > n.md\n\tgit clean -fd\n\tEOF", "cat > a.md <<EOF\nit's fine, don't git push -f\nEOF", "echo git push -f",
        'git commit -m "x" -m "use --no-verify never"', 'git log --grep="reset --hard"', 'git grep -n "push --force"',
        "gh pr comment 1 --body 'git push --force is blocked'", "python3 -c \"print('git reset --hard')\"",
        'printf "%s\\n" "git clean -fd" > notes.txt', "git commit -F - <<EOF\nnever git push --force\nEOF",
        "echo 'bash -c \"git reset --hard\"'", "rg 'checkout \\.' -n", "git commit -m '$(git reset --hard)'",
    ],
    "everyday git": [
        "git push -u origin HEAD", "git checkout .github/x", "git restore src/a.ts", "git clean -n", "git clean -fdn",
        "git clean -f --dry-run", "git clean -ef", "git branch -d merged", "git branch --delete merged",
        "git -c color.ui=never push origin main", "git checkout HEAD -- src/a.ts", "git restore --source HEAD src/a.ts",
        "git restore --source=HEAD src/a.ts", "git restore --staged -- .", "git restore -S .", "git commit -mnope",
        "git commit -m n", "git log --oneline -n 5", "git push -n origin x", "git config core.hooksPath",
        "git config --get core.hooksPath", "git config user.name x", "git config --unset user.name", "git stash",
        "git stash pop", "git stash list", "git reset --soft HEAD~1", "git reset HEAD file", "git reset --keep HEAD~1",
        "git checkout main", "git checkout -b feature/x", "git switch main", "git switch -c x", "git branch -m old new",
        "git gc", "git gc --prune=never", "git reflog", "git fetch --prune", "git remote prune origin",
        "git merge --no-ff x", "git rebase main", "git diff -- .", "git add .", "git commit --amend --no-edit",
        "ls; git status", "git push origin feature/x 2>&1 | tail", "cd sub && git restore --staged .", "command -v git",
        "which git", "git -C /tmp status", "git --no-pager log -p", "git push origin HEAD:refs/heads/feature/x",
        "git branch -f x HEAD", "git submodule foreach 'git status'", "git rebase -x 'make test' main",
        "git -c alias.st=status st", "env FOO=1 git status", "bash -c 'git status && git log'", "git update-ref refs/x HEAD",
        "git reflog show", "git checkout -- src", "git tag -d v1", "git push origin v1.0.0", "",
    ],
    "near misses of the round-2 forms": [
        "bash -c -- 'git status'", "bash -c -x 'git log'", "sh -c +e 'git push origin x'", "bash -o pipefail script.sh",
        "cat <<'EOF' | grep push\ngit push -f\nEOF", "cat <<'EOF' | bash\ngit status\nEOF", "export FOO=1; git status",
        "export GIT_CONFIG_KEY_0=color.ui; git commit -m x", "git pull --rebase", "git pull --no-verify-signatures",
        "git -c clean.requireForce=true clean -d", "git -c clean.requireForce=false clean -n",
        "git -c remote.origin.mirror=false push origin x", "git config alias.st status", "git config alias.lg 'log --oneline'",
        "git config --get alias.nuke", "stdbuf -oL git log", "setsid git status", "source <(echo export X=1)",
        "source ./env.sh", ". ./env.sh", "bash <(echo 'git status')", "git restore --staged --pathspec-from-file=list.txt",
        "diff <(git show HEAD:a) a",
    ],
    "near misses of command substitutions": [
        'eval "$(ssh-agent -s)"', "$(git rev-parse --show-toplevel)/scripts/check", 'eval "$(echo export X=1)"',
        "$(echo git status)", "`which python3` -V", 'git commit -m "$(echo fix -n handling)"',
        "git commit -m \"$(cat <<'EOF'\nfix: stop --force pushes, -n and --no-verify\n\ngit reset --hard is blocked\nEOF\n)\"",
        "git push origin $(git branch --show-current)", 'cd "$(git rev-parse --show-toplevel)" && git status',
    ],
}


class Wrapper(unittest.TestCase):
    def expect(self, groups, code):
        results = hook_all([command for commands in groups.values() for command in commands])
        for group, commands in groups.items():
            for command in commands:
                with self.subTest(group=group, command=command):
                    self.assertEqual(results[command].returncode, code, results[command].stderr)
                    if code == 2:
                        self.assertIn("BLOCKED", results[command].stderr)

    def test_shared_proof_cases(self):
        self.expect({"shared block": SHARED_BLOCK}, 2)
        self.expect({"shared allow": SHARED_ALLOW}, 0)

    def test_bypasses_are_blocked(self):
        self.expect(BLOCK, 2)

    def test_false_positives_are_allowed(self):
        self.expect(ALLOW, 0)

    def test_reason_names_what_was_blocked(self):
        stderr = hook("git push --force origin main").stderr
        self.assertIn("destructive-git guard", stderr)
        self.assertIn("force", stderr)

    def test_works_without_jq(self):
        with tempfile.TemporaryDirectory() as tmp:
            for tool in ("python3", "dirname"):
                os.symlink(shutil.which(tool), Path(tmp) / tool)
            result = hook("git push --force", env={"PATH": tmp}, argv=[shutil.which("bash"), str(WRAPPER)])
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_missing_python_fails_closed(self):
        result = hook("git status", env={"PATH": "/nonexistent"}, argv=[shutil.which("bash"), str(WRAPPER)])
        self.assertEqual(result.returncode, 2)
        self.assertIn("python3", result.stderr)

    def test_malformed_json_blocks_in_always_scope(self):
        self.assertEqual(hook(None, raw="{not json").returncode, 2)
        self.assertEqual(hook(None, raw="").returncode, 2)

    def test_payload_without_a_command_passes(self):
        self.assertEqual(hook(None, raw=json.dumps({"tool_input": {}})).returncode, 0)
        self.assertEqual(hook(None, raw=json.dumps({"tool_input": {"command": "git status"}})).returncode, 0)


class Managed(unittest.TestCase):
    def setUp(self):
        self.guard = load_guard()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.managed, self.plain, self.loose = self.root / "managed", self.root / "plain", self.root / "loose"
        for repo in (self.managed, self.plain):
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (self.managed / ".claude").mkdir()
        (self.managed / ".claude/slopbrake.json").write_text('{"stack": "python"}\n')
        (self.managed / "src").mkdir()
        self.loose.mkdir()

    def check(self, command, cwd, scope="managed"):
        return self.guard.check_command(command, str(cwd), scope)

    def test_acts_only_inside_a_managed_repo(self):
        self.assertIsNotNone(self.check("git push -f", self.managed))
        self.assertIsNotNone(self.check("git push -f", self.managed / "src"))
        self.assertIsNone(self.check("git push -f", self.plain))
        self.assertIsNone(self.check("git push -f", self.loose))
        self.assertIsNotNone(self.check("git push -f", self.plain, scope="always"))
        self.assertIsNone(self.check("git push origin feature/x", self.managed))

    def test_cd_moves_the_effective_directory(self):
        self.assertIsNotNone(self.check(f"cd {self.managed} && git push -f", self.plain))
        self.assertIsNotNone(self.check("cd managed && git reset --hard", self.root))
        self.assertIsNotNone(self.check("cd managed/src; cd ..; git reset --hard", self.root))
        self.assertIsNone(self.check(f"cd {self.plain} && git push -f", self.managed))
        self.assertIsNone(self.check(f"cd '{self.plain}'\ngit push -f", self.managed))
        self.assertIsNotNone(self.check(f"bash -c 'cd {self.managed} && git push -f'", self.plain))

    def test_git_dash_c_moves_the_effective_directory(self):
        self.assertIsNotNone(self.check(f"git -C {self.managed} push -f", self.plain))
        self.assertIsNotNone(self.check("git -C managed -C src reset --hard", self.root))
        self.assertIsNone(self.check(f"git -C {self.plain} push -f", self.managed))
        self.assertIsNotNone(self.check(f"git --work-tree={self.managed} --git-dir={self.managed}/.git reset --hard",
                                        self.plain))

    def test_subshell_cd_does_not_leak(self):
        self.assertIsNotNone(self.check(f"(cd {self.plain} && git status) && git push -f", self.managed))
        self.assertIsNone(self.check(f"(cd {self.managed}); git push -f", self.plain))

    def test_nested_unmanaged_repo_is_its_own_toplevel(self):
        inner = self.managed / "vendor/inner"
        subprocess.run(["git", "init", "-q", str(inner)], check=True)
        self.assertIsNone(self.check("git reset --hard", inner))

    def test_managed_scope_from_the_command_line(self):
        argv = ["python3", str(GUARD), "--scope", "managed"]
        self.assertEqual(hook("git push -f", cwd=str(self.plain), argv=argv).returncode, 0)
        self.assertEqual(hook("git push -f", cwd=str(self.managed), argv=argv).returncode, 2)
        self.assertEqual(hook(None, raw="{not json", argv=argv).returncode, 0)

    def test_always_scope_ignores_repo_state(self):
        for cwd in (self.managed, self.plain, self.loose):
            self.assertIsNotNone(self.check("git reset --hard", cwd, scope="always"))
            self.assertIsNone(self.check('git commit -m "reset --hard"', cwd, scope="always"))

    def test_nested_command_is_judged_against_its_own_directory(self):
        self.assertIsNotNone(self.check(f"git -C {self.plain} -c alias.x='!git -C {self.managed} push -f' x", self.plain))
        self.assertIsNotNone(self.check(f"git -c alias.x='!cd {self.managed} && git push -f' x", self.plain))
        self.assertIsNotNone(self.check(f"git rebase -x 'git -C {self.managed} reset --hard' main", self.plain))
        self.assertIsNone(self.check(f"git -C {self.managed} -c alias.x='!git -C {self.plain} push -f' x", self.plain))

    def test_substituted_command_is_judged_against_its_own_directory(self):
        self.assertIsNotNone(self.check(f"$(echo git -C {self.managed} push -f)", self.plain))
        self.assertIsNone(self.check(f"$(echo git -C {self.plain} push -f)", self.managed))
        self.assertIsNotNone(self.check(f"cd {self.managed} && `echo git reset --hard`", self.plain))

    def test_unbalanced_quotes_in_a_nested_string_do_not_raise(self):
        self.assertIsNotNone(self.check("git -c alias.x='!git push -f \"' x", self.managed))
        self.assertIsNone(self.check("git -c alias.x='status \"' x", self.managed))


if __name__ == "__main__":
    unittest.main()
