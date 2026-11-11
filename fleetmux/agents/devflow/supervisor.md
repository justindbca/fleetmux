# Development flow: tests first, interface frozen before anyone implements

> Role pack for code-development tasks. Attach with `start --roles <pack dir> --roster <pack dir>/roster.example.yaml`.
> It is appended after the base supervisor protocol. The base prohibitions, two-command check budget and
> decision JSON still apply. This file only adds the order of work and the gates for a code task.

Why this exists: when workers implement in parallel and each guesses the interface of the others, every
block passes its own tests and the merged chain still does not run. Seen in practice: fields that a
consumer reads but no block writes; two blocks taking opposite answers to the same open question
(timezone, when an event fires, a timeout); tests that skip when a dependency is missing and show green;
the same file edited by several workers in one round. The fix is order, not more review.

## D1. Two phases, in this order

**Phase 0 — contract (one worker, `contract`).** Nobody else starts until Gate 0 passes. Phase 0 delivers,
in its own directory `{{WORKSPACE}}/contract/`:

1. `skeleton/` — every new module and function the task needs, with the real signature and a body that only
   raises `NotImplementedError`. Importable. Paths mirror the repository.
2. `keys.md` — one table, one row per Redis key / stored field / event / file the task writes or reads:
   `key or field | type and unit/timezone | writer (exactly one block) | readers | when written`.
   Extend the project's existing field table if there is one; do not start a second document.
3. `tests/` — contract tests that call the skeleton through its public names: signatures, return shapes,
   key paths, and for every row of `keys.md` one test that the writer writes it and the reader reads it.
   Plus one end-to-end test through the whole chain with **no stand-ins** for blocks inside this task.
   Direct imports only: no `pytest.importorskip`, no `skipif` on a missing module, no hard-coded worktree path.
4. `decisions.md` — every open question the blocks must agree on. Numeric ones (thresholds, timeouts, counts)
   become parameters with a stated default. Structural ones (which time point, which timezone, which side
   owns a value) get **one** conservative answer written into the contract, tagged `待用户定`. Every block uses
   that answer; no block picks its own.
5. `phase0_check.txt` — the pytest output of `tests/` against `skeleton/` (expected: every failure is
   `NotImplementedError`), the `sha256sum` of every file in `skeleton/`, `keys.md`, `tests/`, `decisions.md`,
   and these two lines, counted by the contract worker:
   ```
   IMPORT_OR_SKIP: <number of ImportError / ModuleNotFoundError / skipped in the pytest output>
   ROWS_WITHOUT_WRITER: <number of keys.md rows whose writer cell is empty, "?", "TBD" or "待定">
   ```

**Gate 0** (one read-only command, then decide):

```
grep -E "^(IMPORT_OR_SKIP|ROWS_WITHOUT_WRITER): " {{WORKSPACE}}/contract/phase0_check.txt
```

Pass = both lines present and both `0`. Otherwise dispatch `contract` again naming the line that failed.
Do not dispatch implementers before Gate 0 passes.

**Phase 1 — implementation (parallel).** One instruction per block. Each instruction must contain:
the block's functions (from the skeleton), the frozen paths (`contract/skeleton`, `contract/keys.md`,
`contract/tests`, `contract/decisions.md`) with their sha256 from `phase0_check.txt`, the rows of
`keys.md` this block writes, and the test command (all of `contract/tests` plus the block's own tests).

- One writer per file per round. If two blocks need the same existing file, give it to one block; the other
  delivers a function the first one calls. Never send the same file to two workers in one round.
- An implementer copies the skeleton into its own sandbox and fills in the bodies of its own functions only.
  It does not change any signature, any `keys.md` row, any contract test or any `decisions.md` answer, and
  never invents an interface. If the contract is not enough, it writes `接口缺口` in its REPORT and stops
  that item.

## D2. Interface gap protocol

1. Read the `接口缺口` item (one `grep -n "接口缺口" -A5`).
2. Dispatch **only `contract`**: amend the contract, bump `contract_version` in `keys.md`, re-run and rewrite
   `phase0_check.txt` with new sha256 values.
3. Re-run Gate 0. Then re-dispatch every implementer whose rows or functions changed, quoting the new sha256.
   Implementers not affected continue.

Do not let an implementer fix the contract, and do not accept "I assumed function X" in a report.

## D3. Accepting a block

Each implementer REPORT must contain the line `CONTRACT_TESTS: <n> passed, <k> skipped, <f> failed` for the
full `contract/tests` run on its stacked sandbox. Two read-only commands:

```
grep -E "^CONTRACT_TESTS: " {{WORKSPACE}}/<worker>/REPORT.md
grep -hE "^\+\+\+ " {{WORKSPACE}}/<worker>/*.patch
```

Pass = `0 skipped, 0 failed`, no patched path is one of the contract test files, and no patched path is a
file given to another worker this round. (Signature changes show up as contract test failures.)

`COMPLETED` only after every block passes D3 **and** one worker (usually `contract`) has stacked all
candidates on the skeleton and run `contract/tests` including the end-to-end test: result in
`{{WORKSPACE}}/contract/integration_check.txt` with a `CONTRACT_TESTS:` line showing `0 skipped, 0 failed`. Any `skipped` there is a failure, not a pass.

## D4. What stays the same

Workers still write only in their own directory; candidates, patches and REPORT.md as the task goal says.
Pending decisions do not stop the task: they are recorded under `待用户定` and the run continues on the
contract's answer. Only a real safety event is `BLOCKED`.
