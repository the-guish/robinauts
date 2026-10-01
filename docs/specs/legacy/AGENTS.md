# For coding agents working in docs/specs/legacy/

These specs describe the backend as it was before the split into shells, a
controller and agent engines. The code they describe is `robinauts.legacy`.

All of it is under review. Each spec, and each rule in it, may be kept,
rewritten for the new layers, or dropped. Nothing here has been decided yet.

Do not:

- build anything new on these specs, or cite them as the current contract.
  The contracts that stand are the specs outside this folder, starting with
  [../agent-engines.md](../agent-engines.md).
- edit them to match new work. They record what was; the new specs record
  what is.

Do:

- read them when a question is about `robinauts.legacy`, or about why the
  old design did something, and say which is which when you answer.
