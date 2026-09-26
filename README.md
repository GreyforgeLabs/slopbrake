<p align="center">
  <img src="brand/banner.png" alt="Slopbrake: brakes for the software factory" width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-38c8e8?style=flat-square"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-38c8e8?style=flat-square">
  <img alt="Stacks: Python and TypeScript" src="https://img.shields.io/badge/stacks-Python%20%C2%B7%20TypeScript-8b95a0?style=flat-square">
  <img alt="Built for Claude Code" src="https://img.shields.io/badge/harness-Claude%20Code-fda52b?style=flat-square">
  <img alt="No Python dependencies: standard library only" src="https://img.shields.io/badge/python%20deps-0%20(stdlib)-9fb896?style=flat-square">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-it-catches">What it catches</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="docs/RULES.md">The rules</a> ·
  <a href="#credits">Credits</a>
</p>

---

> Without brakes, a software factory becomes a slop cannon.
> — paraphrasing [Matt Pocock, *Fixing the PR Bottleneck*](https://www.youtube.com/watch?v=LlgiOCmFG_w)

Coding agents write code faster than anyone can review it. They also write **tests that pass forever and catch nothing**, reach into modules they shouldn't, and hand you a pile of PRs that all look equally safe to merge.

**Slopbrake** installs the brakes. Every change an agent makes goes through the same three layers. Every review comment that keeps coming back becomes a check that runs before the next change.

```mermaid
flowchart LR
    A([agent implements]) --> L1["<b>Layer 1 · scripts/check</b><br/>lint · types · boundaries<br/>tests · tautology · mutation floor"]
    L1 --> L2["<b>Layer 2 · reviewer</b><br/>fresh context<br/>commits its own fixes"]
    L2 --> D{door?}
    D -- two-way --> M([merge-eligible])
    D -- one-way --> H([a human reviews it])
    M -.-> R[/"retro: repeated findings become checks"/]
    H -.-> R
    R -.-> L1
```

## Quick start

```sh
uv tool install git+https://github.com/GreyforgeLabs/slopbrake    # or: pipx install git+https://…

slopbrake init   path/to/repo --dry-run   # see exactly what it would add
slopbrake init   path/to/repo             # add it; never overwrites files you own
slopbrake verify path/to/repo             # prove every rule bites
```

Python and TypeScript repos are supported. Every command takes `--json`. Exit codes: 0 ok, 1 a gap or a failed proof, 2 usage error.

### Every rule is proven, not configured

`verify` works in a throwaway git worktree. For each rule it plants a real violation and requires the check to **fail and name the rule**, then requires it to pass again once the violation is removed. A check that crashes can't pass a proof.

<p align="center"><img src="brand/verify.svg" alt="slopbrake verify output: fifteen proofs, all holding" width="720"></p>

## What it catches

A test like this passes forever and can never disagree with the code:

```python
from shop import discount
from shop.pricing import MEMBER_THRESHOLD

def test_threshold():
    assert MEMBER_THRESHOLD == 100               # restates the definition

def test_discount():
    total = 150
    assert discount(total, True) == total - 10   # recomputes the answer the way the code does
```

The gate says so:

<p align="center"><img src="brand/catch.svg" alt="scripts/check flags both tautological assertions" width="820"></p>

The fix is an independent expectation, a literal or a worked example: `assert discount(150, True) == 140`. Property checks (`out == sorted(out)`) and symbolic expectations (`validate(x) == ERR_TOO_LONG`) pass untouched. To suppress one assertion, write the reason: `# slopbrake: allow-tautology: 280 is the published API limit`.

| Rule | Catches | Python | TypeScript |
|:----:|---------|--------|------------|
| **T1** | Tautological tests: constants asserted against themselves, expected values rebuilt from the test's own inputs, `expect(true).toBe(true)` | AST check | `slopbrake/no-tautological-test` ESLint rule |
| **T4** | Tests that can't fail: mutation score below the floor, **on changed lines only** | stdlib mutator, in a copy of the tree | StrykerJS with `--mutate` line ranges |
| **D1** | Imports that reach past a module's entry points, and import cycles | stdlib port of Pocock's rules | dependency-cruiser, Pocock's config |
| **G1 · G2** | PR bodies without a Door and Blast Radius; a door declared lower than the rules compute | door classifier + PR-body check | same |
| **H5** | `push --force`, `reset --hard`, `clean -f`, `branch -D`, `checkout .` in agent sessions | Claude Code hook | same |
| **H6** | An agent stopping with red, uncommitted code (after 3 tries it may stop and report) | Claude Code Stop hook | same |

All 30+ rules, with their mechanisms and proofs, are in **[docs/RULES.md](docs/RULES.md)**.

## How it works

### Layer 1: one gate, everywhere

`scripts/check` is the one command. The pre-commit hook runs it with `--fast` (no mutation), and pre-push and CI run it in full. The Stop hook runs the fast gate before an agent may finish with uncommitted code. The checks are standard-library Python, so CI installs nothing. Mutation testing touches **only the lines you changed**, which keeps it fast enough to run on every push.

### Layer 2: a reviewer with fresh eyes that commits its fixes

The implementer has the most context pressure, so it doesn't carry the coding standards. The **reviewer** does (Pocock's push/pull split).
- Two reviewers run in parallel on separate axes and never see the implementer's conversation.
  - **Standards:** your `CODING_STANDARDS.md`, plus a Fowler smell baseline.
  - **Spec:** does the change do what the ticket asked, no less and no more?
- A single fix-mode reviewer then **commits the fixes** as `review:` commits instead of leaving comments.
- Anything that changes behaviour or touches a one-way door goes under *Open questions* for a human.

### Door triage: spend human attention where it can't be undone

Every PR declares a **Door** and a **Blast Radius**. Slopbrake *computes a floor* from path and content rules. Migrations, `DROP TABLE`, auth, billing, deploy config, outbound email, runtime file deletion, and the gate itself are all one-way. The agent may raise the door but never lower it. Two-way doors with a green gate are merge-eligible; one-way doors wait for you.

```text
$ python3 scripts/slopbrake/door_classify.py --base main
door: one-way
  - touches migrations/0007_drop_legacy.sql (path rule '**/migrations/**')
  - touches migrations/0007_drop_legacy.sql (path rule '**/*.sql')
  - migrations/0007_drop_legacy.sql:1 adds 'DROP TABLE legacy_accounts;' (content rule 'DROP TABLE')
```

### Retro: comments become checks

`/retro` sorts every miss:
- *mechanical* becomes a check, with a fixture showing it fail;
- a *judgement call* becomes a line in `CODING_STANDARDS.md`;
- *navigation* trouble becomes a pointer;
- a *no-op* instruction gets deleted.

A finding that shows up twice and is still only prose counts as a failed retro.

## What a repo gets

<details>
<summary><b>Files <code>slopbrake init</code> writes</b> (kit-owned files are refreshed by <code>--update</code>; repo-owned files are written once and are yours)</summary>

| Path | Owner | Purpose |
|------|-------|---------|
| `scripts/check` | repo | The one gate command. Stage commands are yours to edit. |
| `scripts/slopbrake/` | kit | The standard-library checks. |
| `.githooks/`, `.github/workflows/check.yml` | kit / repo | Layer 1 locally and in CI. |
| `.claude/agents/reviewer.md` | kit | Two-axis reviewer; review-only and fix modes. |
| `.claude/skills/implement-ticket/` | kit | Ticket → seams → TDD → gate → parallel review → door → PR body. |
| `.claude/skills/{tdd,pr,retro,…}` | kit | Pocock's skills, vendored unmodified, with our additions appended. |
| `.claude/settings.json` | merged | The git-guard and Stop hooks, merged into your existing settings. |
| `.claude/door-rules.yml` | repo | What counts as a one-way door in this repo. |
| `CLAUDE.md`, `CODING_STANDARDS.md`, `docs/agents/` | repo | A 40-line navigation file; the reviewer's standards; the tracker and retro log. |
| TypeScript: `.dependency-cruiser.cjs`, `stryker.config.json`, `eslint-rules/slopbrake.mjs` | repo / kit | D1, T4, T1. |

</details>

## Where Slopbrake departs from the talks

- **Mutation testing** is ours: it's the objective test of "can this test fail?" It starts at a 60% floor on changed lines, and `/retro` raises it.
- **The tautology rule is a check, not only a standard.** Pocock's own retro skill says a mechanical rule "gets a deterministic check, full stop".
- **The door is computed, then confirmed.** Pocock has the agent declare it; we add a floor it can't lower.
- **One committer.** The two review axes run in parallel as report-only; a single fix-mode reviewer commits, so two agents never race on one branch.
- **`/implement-ticket`, not `/implement`**, because [Claude Code gives personal skills precedence over project skills](https://code.claude.com/docs/en/skills) and a personal `implement` would shadow it.

## Credits

Slopbrake stands on the work of **Matt Pocock** ([@mattpocock](https://github.com/mattpocock), [AI Hero](https://www.aihero.dev)). It turns his ideas into rules that fail a build. Where each idea comes from:

- **[`mattpocock/skills`](https://github.com/mattpocock/skills)**, vendored at `c55ee46` under its MIT license ([slopbrake/vendor/pocock-skills](slopbrake/vendor/pocock-skills)):
  - [`tdd`](https://github.com/mattpocock/skills/tree/main/skills/engineering/tdd): tautological and implementation-coupled tests, red before green, seams, vertical slices.
  - [`code-review`](https://github.com/mattpocock/skills/tree/main/skills/engineering/code-review): two axes kept apart, the Fowler smell baseline.
  - [`retro`](https://github.com/mattpocock/skills/tree/main/skills/in-progress/retro): mechanical findings become checks; implementation vs review context pressure.
  - [`pr`](https://github.com/mattpocock/skills/tree/main/skills/in-progress/pr): Summary, Evidence, Merge Danger, Door, Blast Radius.
  - [`setup-ts-deep-modules`](https://github.com/mattpocock/skills/tree/main/skills/in-progress/setup-ts-deep-modules): the dependency-cruiser rules and "prove the rules bite", the seed of `verify`.
  - [`codebase-design`](https://github.com/mattpocock/skills/tree/main/skills/engineering/codebase-design) and [`improve-codebase-architecture`](https://github.com/mattpocock/skills/tree/main/skills/engineering/improve-codebase-architecture): deep modules, the deletion test, "one adapter is a hypothetical seam".
  - [`git-guardrails-claude-code`](https://github.com/mattpocock/skills/tree/main/skills/misc/git-guardrails-claude-code): the destructive-git hook.
  - [`implement-spec`](https://github.com/mattpocock/skills/tree/main/skills/in-progress/implement-spec): one implementer fixes the review findings.
  - Vendored as-is: [`diagnosing-bugs`](https://github.com/mattpocock/skills/tree/main/skills/engineering/diagnosing-bugs), [`to-spec`](https://github.com/mattpocock/skills/tree/main/skills/engineering/to-spec), [`to-tickets`](https://github.com/mattpocock/skills/tree/main/skills/engineering/to-tickets), [`handoff`](https://github.com/mattpocock/skills/tree/main/skills/productivity/handoff), [`writing-for-agents`](https://github.com/mattpocock/skills/tree/main/skills/productivity/writing-for-agents), [`domain-modeling`](https://github.com/mattpocock/skills/tree/main/skills/engineering/domain-modeling), [`setup-matt-pocock-skills`](https://github.com/mattpocock/skills/tree/main/skills/engineering/setup-matt-pocock-skills).
- **[`mattpocock/dictionary-of-ai-coding`](https://github.com/mattpocock/dictionary-of-ai-coding)**: the vocabulary we use throughout.
- **Talks and writing**:
  - [*Fixing the PR Bottleneck*](https://www.youtube.com/watch?v=LlgiOCmFG_w): three layers, one-way and two-way doors, a reviewer that commits, retro.
  - [*Full Walkthrough: Workflow for AI Coding*](https://youtu.be/-QFHIoCo-Ko): standards pushed to the reviewer, the smart zone, grill → spec → tickets.
  - ["Tautological tests considered harmful"](https://x.com/mattpocockuk/status/2093068185830347088).
  - [*AI Skills with Matt Pocock*](https://newsletter.pragmaticengineer.com/p/ai-skills-with-matt-pocock) and [*Matt Pocock's Agentic Engineering Workflow*](https://www.youtube.com/watch?v=nQwJVHCtDDY): keep the harness small, and review the auto-merge decider rather than every PR.
- Related: **[`mattpocock/sandcastle`](https://github.com/mattpocock/sandcastle)** for running sandboxed agents.

Also: the `pr` skill's "shape of the change" section is [Dex Horthy](https://github.com/dexhorthy)'s `show-me` from [`humanlayer/skills`](https://github.com/humanlayer/skills). The TypeScript checks run on [dependency-cruiser](https://github.com/sverweij/dependency-cruiser) and [StrykerJS](https://github.com/stryker-mutator/stryker-js).

Any mistakes in turning these ideas into checks are ours, not his.

## Developing

`scripts/check` runs Slopbrake's own gate: pinned ruff, behaviour tests for every Python check (run through their command lines against seeded git repos), and RuleTester cases for the ESLint rule. The pre-commit hook and CI run it. The brand art is generated from source: `brand/make_logo.py`, `brand/src/banner.html` and `brand/make_terminal.py`.

## License

MIT © 2026 [Greyforge Labs](https://github.com/GreyforgeLabs). The vendored skills keep their own MIT license © 2026 Matt Pocock.
