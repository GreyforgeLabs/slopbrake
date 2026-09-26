# Coding standards

Judgement calls only; the reviewer enforces them. Anything mechanical lives in `scripts/check`: if a rule here could be a check, the retro turns it into one (see `docs/agents/retro-log.md`).

## Tests
- Tautological tests considered harmful. Expected values come from a literal, a worked example, or the spec.
- Test through the module's entry points. If you need to reach inside, the module is the wrong shape: say so.
- Mock only at system boundaries: external APIs, time, randomness, sometimes DB/FS. Never our own modules.
- Test names describe behaviour ("user can check out with valid cart"), not calls.

## Design
- Prefer deep modules: small interface, lots of behaviour. Apply the deletion test to new modules.
- One adapter is a hypothetical seam. Add a port only when two adapters exist.
- Several small entry points over one barrel.

## Change hygiene
- No scope creep: behaviour the ticket didn't ask for goes in a follow-up.
- Delete stale plan/spec docs when the work ships.
