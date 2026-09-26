# The rules

Each rule has an ID, a **mechanism**, and, where it can be checked, the proof that `slopbrake verify` runs.

- **Check**: deterministic; it fails `scripts/check`.
- **Review**: a judgement call; the reviewer agent enforces it.
- **Process**: a workflow step, carried by a skill or a hook.

The ideas come from Matt Pocock (see [Credits](../README.md#credits)). Rules marked *(ours)* are our extensions.

## L: the gate

**L1. One gate command.** *Check.* `scripts/check` runs lint, types, boundaries, tests, smoke, test quality, the mutation floor and the PR-body check. The pre-commit hook runs `scripts/check --fast` (no mutation); pre-push and CI run the full gate. A repo with no hook and no CI running its checks has no guardrail, and that is a finding in itself.
*Proof:* a clean tree passes the full gate.

## T: tests that catch bugs

**T1. No tautological tests.** *Check + Review.* An expected value must come from an independent source: a literal, a worked example, or the spec. It must not be the implementation's constant, a `reduce`/`sum` over the test's own inputs, or `a + b` for `add(a, b)`. *(The lint rule is ours; Pocock keeps this in coding standards.)*
*Proof:* a seeded tautological test fails `test-quality`.

**T2. Test through public interfaces at agreed seams.** *Check (via D1) + Review.* Write the seams under test into the ticket before writing tests. Mock only at system boundaries: external APIs, time, randomness, and sometimes the database or filesystem.

**T3. Red before green.** *Process.* One failing test, then the least code that passes it. The PR's Evidence section quotes the red run. For a bug fix, the regression test must be shown failing on the unfixed code.

**T4. Tests must be able to fail.** *Check.* The mutation score on **changed lines** must meet a floor: 60% to start, raised by `/retro`. Surviving mutants go to the reviewer as evidence. *(Ours: the objective form of "a test that catches nothing".)*
*Proof:* an assertion-free test fails the floor; real assertions clear it.

**T5. Evidence of behaviour.** *Review.* "Tests pass" is not enough evidence for a user-visible change: show a before and an after.

## D: deep modules

**D1. Import boundaries.** *Check.* A module's public surface is its entry points. Outside code and tests import only those; everything in a subfolder is private. No import cycles.
*Proof:* pass, then a seeded deep import fails, then pass again after reverting.

**D2. Several small entry points, not one barrel.** *Review.*
**D3. No speculative seams.** *Review.* One adapter is a hypothetical seam; add a port when a second adapter exists.
**D4. Deepening pass.** *Process.* When modules get deepened, replace the shallow tests with tests at the new interface. Don't layer new tests on top of the old ones.

## R: the reviewer owns standards

**R1.** The implementer isn't handed `CODING_STANDARDS.md`; it may pull it.
**R2.** The reviewer runs in a fresh context. It gets the diff, the spec, the standards in full, and the gate output. It never sees the implementer's conversation.
**R3.** Two axes, Standards and Spec, run in parallel and are reported separately. They are never merged or reranked.
**R4.** The reviewer skips what tooling enforces. Anything mechanical that the gate missed is tagged `MECHANICAL`, for the retro.

## C: the reviewer commits instead of commenting

**C1. Fix in place.** For each local, confident finding, commit `review: <finding>`. Findings that change the spec's behaviour, span many modules, or touch a one-way door go under Open questions instead.
**C2. Loop limit.** At most 3 fix-and-gate rounds; after that, escalate. *(ours)*
**C3. Traceability.** Every `review:` commit gets a line in the PR's Review log.

## G: door triage

**G1. Every PR declares a Door and a Blast Radius.** *Check.*
*Proof:* a PR body without a Door fails.

**G2. The door is computed, then confirmed.** *Check.* Path and content rules in `.claude/door-rules.yml` compute a floor. Migrations, destructive SQL, auth, billing, deploy/CI, outbound messaging, runtime file deletion, and the gate itself are all one-way. The agent may raise the door but never lower it. *(The computed floor is ours.)*
*Proof:* a migration diff classifies one-way; a README typo classifies two-way; declaring a migration "two-way" fails.

**G3. Routing.** A two-way door with a green gate and a clean review is eligible to merge. A one-way door needs a human and a rollback plan. Humans review the classifier and its rules, not every two-way PR.

**G4. PR body shape.** *Check.* Summary (one visual), Evidence (before and after), Merge Danger, Review log, Open questions.
*Proof:* a complete one-way body passes.

## F: retro turns comments into checks

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

**H1.** `CLAUDE.md` is at most 40 lines: navigation pointers and the check command only. *Proof:* line count.
**H2.** Plan state lives in files (specs, tickets, handoff notes). Clear the context rather than compacting it.
**H3.** Non-trivial work goes grill → spec → tickets. Tickets form a dependency graph, not a list of phases.
**H4.** Bugs: build a command that goes red on the reported symptom before forming any hypothesis.
**H5. Git guardrails.** *Check (hook).* Agent sessions can't run `push --force`, `reset --hard`, `clean -f`, `branch -D`, `checkout .` or `restore .`. A plain branch push is allowed.
*Proof:* destructive commands are blocked; everyday ones pass.

**H6. Stop only when green.** *Check (hook).* With uncommitted code, the agent can't stop while `scripts/check --fast` is red. After 3 red attempts it may stop and report. *(ours)*
*Proof:* a red tree blocks the stop; a green tree allows it.

## What we don't adopt

- Autonomous merges of one-way doors.
- Putting coding standards in `CLAUDE.md` "just in case".
- Depth metrics based on ratios (Pocock rejects them too).
