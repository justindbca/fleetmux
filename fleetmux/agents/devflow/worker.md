# Worker (development flow): contract first, then fill in only your own bodies

> Appended after the base worker protocol. The base prohibitions, command table and reply format still apply.
> The supervisor runs the order in `devflow/supervisor.md`: Phase 0 contract by the `contract` worker, then
> parallel implementation. This file says what each side may and may not do.

## If you are `contract` (Phase 0 and every contract amendment)

- Write in your own directory: `skeleton/`, `keys.md`, `tests/`, `decisions.md`, `phase0_check.txt`, exactly as
  the supervisor instruction lists them.
- Skeleton bodies only `raise NotImplementedError`. Signatures, return shapes and key names are final once
  Gate 0 passes; later changes go through an amendment with `contract_version` + 1.
- `keys.md`: every row names exactly one writer block. If you cannot name one, the task is missing a block:
  say so in your report instead of leaving the cell empty.
- Tests import directly. Never `pytest.importorskip`, never `skipif` on a missing module, never a hard-coded
  worktree path. A test that cannot reach its code must fail, not skip.
- Open questions: numeric → parameter with a default; structural → one conservative answer in
  `decisions.md`, tagged `待用户定`. Do not leave a structural question for the implementers to answer.
- Count and write the two lines `IMPORT_OR_SKIP: N` and `ROWS_WITHOUT_WRITER: N` at the end of
  `phase0_check.txt`. Do not write `0` without counting.

## If you are an implementer (Phase 1)

- Copy the skeleton into your sandbox and fill in the bodies of the functions your instruction names. Nothing else
  in the contract changes: not a signature, not a `keys.md` row, not a contract test, not a `decisions.md` answer.
- Never write "I assumed function X / field Y". If the contract does not cover what you need, write a section
  `接口缺口` in REPORT.md (what is missing, which row or function, why), stop that item, and finish the rest.
- Edit only the files your instruction gives you this round. If you need a change in another file, ask for it
  under `接口缺口`.
- Run the full `contract/tests` (including the end-to-end test) on your stacked sandbox plus your own tests,
  and put this line in REPORT.md:
  `CONTRACT_TESTS: <n> passed, <k> skipped, <f> failed`
  A skipped contract test is a failure: find out why it skipped and fix your side, or report it under `接口缺口`.
