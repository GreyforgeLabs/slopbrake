
## Our additions (Slopbrake)

Append these two sections after Merge Danger:

```markdown
## Review log

- <finding> (<rule/smell>) → <sha>

## Open questions

- <only things a human must decide>
```

- The door is computed first: run `python3 scripts/slopbrake/door_classify.py --base <base>`. You may raise two-way to one-way, never lower it. A one-way door needs a rollback or mitigation plan under Merge Danger, and leads the Summary with the one-way reason.
- Evidence must show a **Before** and an **After**. "Tests pass" alone is not evidence for a user-visible change.
- Write "none" under an empty Review log or Open questions heading; don't drop the heading.
- Check the body before publishing: `python3 scripts/slopbrake/pr_body_check.py --body-file <file> --base <base>`.
