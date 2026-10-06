# The rules

Each rule has an ID, a **mechanism**, and, where it can be checked, the proof that `slopbrake verify` runs. `verify` proves 10 rules (L1, D1, T1, T4, G1, G2, G4, H1, H5, H6) with 16 proofs; the rest are review and process guidance that the reviewer agent and the skills carry.

- **Check**: deterministic; it fails `scripts/check`.
- **Review**: a judgement call; the reviewer agent enforces it.
- **Process**: a workflow step, carried by a skill or a hook.

The PR-body checks (G1-G4, C3) run in the `pr` stage when there is a PR body: in CI on a pull request, or with `--body-file`/`$PR_BODY_FILE`. Outside a PR they skip, except G3's default-branch block.

The ideas come from Matt Pocock (see [Credits](../README.md#credits)). Rules marked *(ours)* are our extensions.

## L: the gate

**L1. One gate command.** *Check.* `scripts/check` runs lint, types, boundaries, tests, smoke, test quality, the mutation floor and the PR-body check. The pre-commit hook runs `scripts/check --fast` (no mutation); pre-push runs the full gate once per pushed ref, measured against the commit the remote already has; CI runs the full gate. A repo with no hook and no CI running its checks has no guardrail, and that is a finding in itself.
- **Skip is not pass.** A stage with nothing to check (no type checker configured, no smoke entry point, nothing changed, no PR context) prints `<stage>: skipped: <reason>`, exits 78 and shows as `skip` in the summary. It never fails the gate and never claims a `pass`.
- **Last green.** A full run (no `--fast`, no stage list) where every stage passed or skipped, with no uncommitted changes to tracked files, records HEAD as `refs/slopbrake/last-green/<branch>` (a `/` in the branch name is written `%2F`). When HEAD is on the base branch, base-relative checks (mutation, the door) measure from that ref instead of seeing nothing; after an amend or rebase, from the merge-base with it; with no ratchet yet, from the empty tree (every line is new; mutants are sampled). Never recorded on a detached HEAD; `SLOPBRAKE_NO_RECORD=1` opts out, and `init` starts the ratchet at HEAD.
- **Base order:** `--base` > `GITHUB_BASE_REF` > `SLOPBRAKE_BASE` > the branch's upstream > `origin/HEAD` > `main` > `master` > `origin/main` > `origin/master`. Checks print the base they used.
- The floor and test command live in the repo's files, not the environment: pre-push drops inherited `MUTATION_FLOOR`, `TEST_CMD` and base overrides.
- **The checked tree is the committed tree.** pre-commit refuses when a staged file also has unstaged edits; pre-push refuses a dirty tracked tree or a pushed ref that isn't HEAD. A human may override either with `--no-verify`; the git guard blocks that for agents.

*Proof:* a clean tree passes the full gate, and its summary reports every stage the kit's `scripts/check` defines as `pass` or `skip` (a trimmed `STAGES` fails it). `slopbrake status` also flags missing kit stages and an unconfigured types stage.

## T: tests that catch bugs

**T1. No tautological tests.** *Check + Review.* An expected value must come from an independent source: a literal, a worked example, or the spec. It must not be the implementation's constant, a `reduce`/`sum`/`len`/`Math.*` over the test's own inputs (inline or through a local variable), or `a + b` for `add(a, b)`. Re-running the code for a determinism or identity check (`rng(seed=1) == rng(seed=1)`) and properties built from the result (`out == sorted(out)`) pass. A test with no assertion at all can only fail by crashing, so it fails too: Python's `tautology_py.py` flags both; TypeScript has `slopbrake/no-tautological-test` and `slopbrake/expect-in-test` (wire both into your ESLint config; `status` reports a config that doesn't load the plugin). Suppress one with its reason: `# slopbrake: allow-tautology: <why>` or `allow-no-assert: <why>`. *(The lint rule is ours; Pocock keeps this in coding standards.)*
*Proof:* a seeded tautological test fails `test-quality`.

**T2. Test through public interfaces at agreed seams.** *Check (via D1) + Review.* Write the seams under test into the ticket before writing tests. Mock only at system boundaries: external APIs, time, randomness, and sometimes the database or filesystem.

**T3. Red before green.** *Process.* One failing test, then the least code that passes it. The PR's Evidence section quotes the red run. For a bug fix, the regression test must be shown failing on the unfixed code.

**T4. Tests must be able to fail.** *Check.* The mutation score on **changed source lines** since the base (L1) must meet a floor: 60% to start, raised by `/retro`. Python mutants flip comparisons and boundaries (`<` to `<=`), arithmetic (never string concatenation, whose crash kills nothing real), `and`/`or` and `not`, bump constants, rewrite strings, delete calls, return `None` and negate conditions; TypeScript uses StrykerJS. Nothing to mutate is a skip, never a 100% score. A line ending in `# slopbrake: no-mutate` is left alone and counted in the output (for equivalent mutants). Surviving mutants go to the reviewer as evidence. A change that only edits tests has nothing to mutate; T1's no-assertion check covers stripped assertions. *(Ours: the objective form of "a test that catches nothing".)*
*Proof:* an assertion-free test fails the floor; real assertions clear it.

**T5. Evidence of behaviour.** *Review.* "Tests pass" is not enough evidence for a user-visible change: show a before and an after.

## D: deep modules

**D1. Import boundaries.** *Check.* A package's public surface is its entry points: the package itself and the public modules at its root. Outside code imports only those, at **every package level** (from `app/ui/view.py`, `app.billing` is entered through its entry points like any top-level package); subpackages and `_private` modules and names are internal. Tests go through the top-level package's entry points, even for the package they live in (co-located `*.test.ts` files too). No import cycles. Python also counts literal `importlib.import_module`/`__import__` calls and discovers packages at the root, under `src/` and under `packages/*/` in monorepos; TypeScript counts `import type`.
*Proof:* pass, then a seeded deep import fails, then pass again after reverting.

**D2. Several small entry points, not one barrel.** *Review.*
**D3. No speculative seams.** *Review.* One adapter is a hypothetical seam; add a port when a second adapter exists.
**D4. Deepening pass.** *Process.* When modules get deepened, replace the shallow tests with tests at the new interface. Don't layer new tests on top of the old ones.

## R: the reviewer owns standards

**R1.** *Process.* The implementer isn't handed `CODING_STANDARDS.md`; it may pull it.
**R2.** *Process.* The reviewer runs in a fresh context. It gets the diff, the spec, the standards in full, and the gate output. It never sees the implementer's conversation.
**R3.** *Review.* Two axes, Standards and Spec, run in parallel and are reported separately. They are never merged or reranked.
**R4.** *Review.* The reviewer skips what tooling enforces. Anything mechanical that the gate missed is tagged `MECHANICAL`, for the retro.

## C: the reviewer commits instead of commenting

**C1. Fix in place.** *Process.* For each local, confident finding, commit `review: <finding>`. Findings that change the spec's behaviour, span many modules, or touch a one-way door go under Open questions instead.
**C2. Loop limit.** *Process.* At most 3 fix-and-gate rounds; after that, escalate. *(ours)*
**C3. Traceability.** *Check (PR body).* Every `review:` commit gets a line in the PR's Review log; the PR-body check fails when a `review:` commit on the branch has none.

## G: door triage

**G1. Every PR declares a Door and a Blast Radius.** *Check.*
*Proof:* a PR body without a Door fails.

**G2. The door is computed, then confirmed.** *Check.* Path and content rules in `.claude/door-rules.yml` compute a floor. Migrations, destructive SQL, auth, billing, secrets, deploy/CI, outbound messaging, runtime file deletion, and the gate itself (hooks, skills, the reviewer, `CLAUDE.md`, `scripts/check`, ESLint/Stryker/dependency-cruiser configs) are all one-way. The agent may raise the door but never lower it. The classifier uses the union of the rules at the base revision and in the change, and takes `ignore` lists only from the base, so a change can't delete or loosen the rules that judge it; with no rules at either, it fails instead of passing. *(The computed floor is ours.)*
*Proof:* a migration diff classifies one-way; a README typo classifies two-way; declaring a migration "two-way" fails.

**G3. Routing.** *Process + Check.* A two-way door with a green gate and a clean review is eligible to merge. A one-way door needs a human and a rollback plan (the PR-body check requires one). Humans review the classifier and its rules, not every two-way PR. Without a PR, the `pr` stage refuses an uncommitted one-way change on the default branch: commit it on a feature branch for review. A human may override with `git commit --no-verify`; the git guard (H5) blocks that for agents.

**G4. PR body shape.** *Check.* Summary (one visual), Evidence (before and after), Merge Danger, Review log, Open questions.
*Proof:* a complete one-way body passes.

## F: retro turns comments into checks

All *Process*, carried by the `/retro` skill.

**F1.** Hold a retro after any session where a human corrected the agent, and periodically across sessions.
**F2.** Inputs: session logs, review comments, `review:` commits, gate failures, `MECHANICAL` tags.
**F3.** Classify every finding:
- *mechanical* becomes a check;
- *judgement* becomes a line in `CODING_STANDARDS.md`;
- *navigation* becomes a pointer;
- *no-op* instructions get deleted;
- *missing information* gets the agent access to it.
**F4.** Every new check lands with a fixture that makes it fail, and is shown failing once.
**F5.** Record each finding in `docs/agents/retro-log.md`. A finding seen twice that is still prose is a failed retro.
**F6.** Keep `CODING_STANDARDS.md` small. Move items into checks, or behind pointers.

## H: context hygiene and harness

**H1.** *Check (`status` and `verify`, not a `scripts/check` stage).* `CLAUDE.md` is at most 40 lines: navigation pointers and the check command only.
*Proof:* the committed `CLAUDE.md` is 1 to 40 lines.
**H2.** *Process.* Plan state lives in files (specs, tickets, handoff notes). Clear the context rather than compacting it.
**H3.** *Process.* Non-trivial work goes grill → spec → tickets. Tickets form a dependency graph, not a list of phases.
**H4.** *Process.* Bugs: build a command that goes red on the reported symptom before forming any hypothesis.
**H5. Git guardrails.** *Check (hook).* Agent sessions can't run:
- force pushes in any spelling (`-f`, `--force-with-lease`, `+refspec`), pushes that delete or mirror refs (`--delete`, `:ref`, `--mirror`, `--prune`);
- anything that skips or switches off the git hooks (`--no-verify` on commit, push, merge, pull or am; `-c core.hooksPath=…`, `config core.hooksPath`);
- `reset --hard`/`--merge`, `clean -f`, `branch -D`/`-M`/`-C`, `checkout -f` or a whole-tree `checkout`/`restore`, `switch --discard-changes`, `stash drop`/`clear`;
- `update-ref -d`, `reflog expire`/`delete`, `gc --prune=now`, `prune`; and aliases, `rebase --exec` or `submodule foreach` that would run any of these.

The command is parsed like a shell would (quotes, `;`/`&&`, `bash -c`, `eval`, `env`/`sudo`/`xargs` wrappers, substitutions), and only git's own argv is judged, so a commit message or `grep` pattern that mentions `reset --hard` passes. A plain branch push is allowed. The hook fails closed when python3 is missing. It's a seatbelt, not a sandbox: values that come from variables or scripts written to a file aren't modelled.
*Proof:* 10 destructive commands are blocked; 6 everyday ones pass; and the hooks are wired in `.claude/settings.json` (not disabled, scripts executable).

**H6. Stop only when green.** *Check (hook).* With uncommitted code, the agent can't stop while `scripts/check --fast` is red. With a clean tree but commits no full green run has covered (since the branch's last green, or since it left the default branch), it runs the full gate, which records the new last green on success; mutation samples at most `SLOPBRAKE_STOP_MUTANTS` (40) mutants and all gates share a 540 s budget inside the 600 s hook timeout, so running out is a red attempt, never a hang. It gates the project and, when the agent works in another worktree of the same repo, that worktree too. After 3 red attempts in a session it may stop and report. *(ours)*
*Proof:* a red tree blocks the stop; a green tree allows it.

**H7. Hooks fire wherever the session starts.** *Process (opt-in).* Claude Code loads project hooks only from the session's starting directory, so a session started in `~` never runs H5 or H6. `slopbrake user-hooks install` adds `slopbrake-hook pre-tool-use|post-tool-use|stop` to `~/.claude/settings.json` (merged; `uninstall` removes only these). They act only in repos whose toplevel has `.claude/slopbrake.json`: the guard judges git commands aimed at them, and the Stop hook runs `require-green.sh` for each repo the session actually changed (an edit, or a Bash command after which the repo's status or HEAD differs), skipping the project when its own hooks already run. Because that runs the repo's own scripts, it does so only for trusted repos (`${XDG_CONFIG_HOME:-~/.config}/slopbrake/trusted.json`; `init` trusts its repo, `user-hooks trust|untrust <repo>` manage the list). Outside managed repos they do nothing and never break a session. `install` refuses until the `slopbrake` on `PATH` can run them; `user-hooks status` shows what's wired. *(ours)*

Supported harnesses: Claude Code (the default), Codex and OpenCode. `user-hooks install|uninstall|status --harness codex` merges the guard, the edit recording and the Stop gate into `~/.codex/hooks.json` beside other tools' entries; it refuses unless `~/.codex/config.toml` sets `[features] hooks = true` (slopbrake never edits that file), and Codex runs the new entries only after you trust them in its `/hooks` screen, which `status` reports. `--harness opencode` installs the plugin `~/.config/opencode/plugins/slopbrake.js`: it guards `bash`, records edits, and on a red Stop gate sends the report back as a new prompt, at most 3 times in a row per session. Trust is shared by every harness.

## Known limits

Slopbrake brakes hurried or careless agents; it is not a sandbox against a deliberately adversarial one. These evasions are documented, not checked, and the files involved are one-way doors so a human sees the edit:
- Environment overrides outside pre-push (`MUTATION_MAX`, `SLOPBRAKE_BASE`), a hand-written `git update-ref` on the last-green ref, deleting kit files, or editing `scripts/check`.
- A PR that edits its own CI gate: use GitHub branch protection to make CI authoritative.
- `git stash` to hide work from the Stop hook; one-way changes merged or cherry-picked to the default branch by hand.
- Shell data flow the git guard doesn't model (variables, scripts written to a file and then run).
- T1 evasions that need data flow to see; mutants killed by a crash on a non-literal operand.
- TypeScript changes that touch only tests are not mutation-checked yet (Python's are).
- Killing `mutation_py` with SIGKILL can orphan the test run it started.

## What we don't adopt

- Autonomous merges of one-way doors.
- Putting coding standards in `CLAUDE.md` "just in case".
- Depth metrics based on ratios (Pocock rejects them too).
