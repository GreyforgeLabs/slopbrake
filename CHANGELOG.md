# Changelog

## Unreleased

### Codex and OpenCode (H5, H6, H7)

- **New:** `slopbrake user-hooks install|uninstall|status --harness codex` merges `slopbrake-hook --harness codex pre-tool-use|post-tool-use|stop` into `~/.codex/hooks.json`, keeping other tools' entries. It refuses while `~/.codex/config.toml` lacks `[features] hooks = true` and never edits that file; `status` reminds you to trust the entries in Codex's `/hooks` screen and reports which ones `[hooks.state]` already trusts.
- **New:** `--harness opencode` installs the OpenCode plugin `~/.config/opencode/plugins/slopbrake.js` (one dependency-free module; it refuses to overwrite a file that is not its own). It guards `bash` before it runs, records `bash`, `edit`, `write` and `apply_patch` changes, and on `session.idle` runs the Stop gate for root sessions, sending a red report back as a new prompt at most 3 times in a row. It does nothing when `slopbrake-hook` is not on `PATH`.

## 0.2.0 (2026-10-05)

An audit of 0.1.0 in real use found that Slopbrake was installed but never fired, and that several of its guarantees were false while `verify` and `status` stayed green. This release makes the gate do what the README says. Rule IDs refer to [docs/RULES.md](docs/RULES.md).

### Hooks that actually run (H5, H6, H7)

- **Broken:** Claude Code loads project hooks only from the directory a session starts in. Sessions started in `~` never ran the git guard or the Stop gate, and nothing said so.
- **New:** `slopbrake user-hooks install|uninstall|status|trust|untrust` wires `slopbrake-hook pre-tool-use|post-tool-use|stop` into `~/.claude/settings.json`. The guard acts in every repo with `.claude/slopbrake.json`; the Stop gate runs only for repos the session actually changed and that you trust (`init` trusts its repo; a cloned repo's scripts never run until you `user-hooks trust` it). Outside managed repos the hooks do nothing. `install` refuses while the `slopbrake` on `PATH` is too old, and an older install without `slopbrake-hook` fails harmlessly instead of blocking every command.
- **Broken:** the git guard was a regex with many bypasses (`push -f;`, `+main`, `--force-with-lease`, `restore`/`checkout` forms, `--no-verify`, `-c core.hooksPath=`) and false positives on commit messages and `grep` patterns. It failed open without `jq`.
- **Changed:** the guard is now `git_guard.py`, which parses the command like a shell (`bash -c`, `eval`, wrappers, substitutions, heredocs) and judges git's own argv. It blocks the forms above plus branch, stash, reflog, ref and object deletion, and aliases or `rebase --exec` that run them. The shell wrapper fails closed without python3.
- **Broken:** the Stop gate looked only at uncommitted code, so committing (or working in another worktree) let an agent stop with an unchecked change.
- **Changed:** a clean tree with commits no full green run has covered gets the full gate, with sampled mutants and one 540 s budget inside the hook's 600 s timeout. Worktrees of the same repo are gated too.

### The gate (L1)

- **Broken:** stages with nothing to run printed `pass` (types was an `echo`, smoke without a script, pr without a PR), and `verify` proved stages by name, so trimming `STAGES` in `scripts/check` switched off the real gate with all proofs green.
- **Changed:** a stage with nothing to check exits 78 and reports `skip <stage>` with its reason. The L1 proof fails unless the summary reports every stage the kit's `scripts/check` defines, and `status` flags missing stages and an unconfigured types stage.
- **Broken:** `MUTATION_FLOOR=0 git push` and `TEST_CMD=true` lowered the gate from the environment.
- **Changed:** `scripts/check` fixes the floor and the test command; pre-push drops inherited overrides and runs the full gate once per pushed ref, against the commit the remote already has.
- **Broken:** committing straight on the default branch with no remote (both pilots' workflow) measured mutation and the door against HEAD itself: nothing changed, so the floor and the door never ran on committed work.
- **New:** the last-green ratchet. A full, all-green run on a clean tree records `refs/slopbrake/last-green/<branch>` (`/` in the branch name written as `%2F`); on the default branch, checks measure from it, or from the merge-base with it after an amend or rebase, or from the empty tree when there is none yet. `init` starts it at HEAD.
- **Changed:** pre-commit refuses when a staged file also has unstaged edits, and pre-push refuses a dirty tree or a ref that isn't checked out: the gate checks the working tree, so it must be the tree being committed or pushed. A new branch is measured against what the remote already has.
- **Changed:** a change that touches only tests is mutation-checked against the modules those tests import (Python), so weakening an assertion no longer slips through. Marking every changed line `# slopbrake: no-mutate` fails the stage.
- **Changed:** Python repos get pytest when it's configured or declared anywhere (pytest-style tests were silently skipped under `unittest discover`), the project's interpreter (`.venv`, found at run time, also from a linked worktree), and a CI install that matches. The TypeScript CI is set up for the repo's package manager instead of always pnpm.

### Test quality (T1, T4)

- **Broken:** an assertion-free test cleared the mutation floor when the code used string `+` (the mutant crashed) and 0 mutants scored 100%. T1 missed its own headline form, `assert total(xs) == sum(xs)`, and the inline `reduce`/`Math.*` forms in TypeScript.
- **Changed:** T1 flags tests with no assertion (Python check; TypeScript `slopbrake/expect-in-test`), the direct aggregate forms and more shapes, and stops flagging determinism, identity and property checks. `# slopbrake: allow-no-assert: <why>` suppresses one.
- **Changed:** more mutation operators (boundaries, strings, deleted calls), no arithmetic mutants on string concatenation, nothing to mutate is a skip, `# slopbrake: no-mutate` for equivalent mutants, and timed-out mutants no longer leave orphaned processes or scratch copies.

### TypeScript (D1, T1, T4)

- **Broken:** under npm, `npm exec <tool> --flags` swallowed every flag (test-quality always red, Stryker lost `--mutate`), and with dependency-cruiser missing, `npm exec depcruise` installed a squatted registry placeholder that let boundaries **pass**.
- **Changed:** tools run from `node_modules/.bin` only; a missing tool fails its stage and names the install command. Boundaries fail when dependency-cruiser read nothing; `import type` and co-located tests are checked; the smoke stage runs a `smoke` package script.

### Boundaries (D1)

- **Broken:** Python boundaries were enforced only between top-level packages, so a single-package app had no enforcement at all; monorepos under `packages/` and `importlib` imports were invisible.
- **Changed:** entry points are enforced at every package level, plus `importlib.import_module`/`__import__`, namespace subpackages, `packages/*/` monorepos and stricter `TYPE_CHECKING` cycle handling.

### Doors (G1-G4)

- **Broken:** a PR could lower its own floor: the rules were read from the PR's tree, deleting `door-rules.yml` disabled the check, and `SLOPBRAKE_BASE` beat the CI base. Gate configs (Stryker, ESLint rules, dependency-cruiser, the reviewer, skills, `CLAUDE.md`) classified two-way.
- **Changed:** the base revision's rules count too, and `ignore` lists come from the base only; no rules anywhere is an error. The default rules cover many more one-way paths and destructive SQL spellings, with fewer false positives (prose, test fixtures, `.env.example`).
- **New:** without a PR, the `pr` stage refuses an uncommitted one-way change on the default branch: commit it on a feature branch for review. The PR-body check is stricter: the Door and Blast Radius must be in prose, not code blocks, and a one-way door's rollback must name a concrete step that isn't negated ("no way to restore" is not a plan).

### CLI

- **Fixed:** `init --dry-run` crashed on fresh Python repos; `verify` leaked a worktree when setup failed; environment errors exit 2 with one line instead of a traceback; `init` in a monorepo subdirectory no longer touches the parent repo's hooks.
- **Changed:** `verify` also fails when the Claude Code hooks aren't wired or are disabled, proves the committed HEAD only, and shows the failing stage. `status` reports a stale kit (by version and by file), unwired hooks and the missing ESLint plugin. `init` tells you to commit the kit on a feature branch.

### Community

Thanks to [@nixfred](https://github.com/nixfred) for the first community fixes, all in this release:
- [#1](https://github.com/GreyforgeLabs/slopbrake/pull/1): the git guard catches git's global options, `clean --force` and `checkout <ref> -- .`.
- [#2](https://github.com/GreyforgeLabs/slopbrake/pull/2): the diff parser no longer drops added lines that start with `++`.
- [#3](https://github.com/GreyforgeLabs/slopbrake/pull/3): a scalar or empty rule list in `door-rules.yml` is an error, not a silently wrong rule.

### Upgrading from 0.1.0

1. Install 0.2.0: `uv tool install --force git+https://github.com/GreyforgeLabs/slopbrake` (or `pipx install --force …`).
2. On a feature branch, run `slopbrake init . --update`. Kit-owned files (`scripts/slopbrake/`, `.claude/hooks/`, `.githooks/`, skills, the reviewer) are refreshed. Repo-owned files are kept and listed as `kept`; merge these by hand (the kit's versions are what `slopbrake init` writes into a fresh scratch repo of the same stack):
   - `scripts/check`: `STAGES=(lint types boundaries tests smoke test-quality mutation pr)`, with smoke; a fixed `export MUTATION_FLOOR=60` and a fixed `TEST_CMD` instead of the `${…:-…}` forms; a types stage that runs your type checker or returns 78 (skip); a smoke stage that returns 78 without `scripts/smoke.sh`.
   - `.claude/door-rules.yml`: the new `one_way` paths (gate configs, `.claude/agents/**`, `.claude/skills/**`, `CLAUDE.md`, secrets, more infra), the new `content_patterns`, the `ignore` list and the extra `content_ignore` entries. Keep each key once.
   - `.github/workflows/check.yml`: the setup and install steps for your package manager.
3. Commit on the branch: the update touches the gate, which is a one-way door, so the gate refuses it on the default branch. A human reviews and merges it.
4. `slopbrake user-hooks install`, so the hooks fire in sessions started outside the repo.
5. `slopbrake status .` should list no gaps, and `slopbrake verify .` should report 16 proofs holding.
