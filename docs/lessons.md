# Lessons

Numbered L-n. Each lesson names the failure, the rule it produced, and where that rule is enforced.

## Carried from EvalKit

These are EvalKit's lessons whose rules apply here unchanged. IDs are EvalKit's.

| ID | Lesson | Rule | Enforced by |
| --- | --- | --- | --- |
| EK L-1 | A gate that has never failed is a gate nobody has checked | Every gate ships a `--self-test` that feeds it bad input and expects FAIL | `node_converge.py --self-test`, `tests/harness/` |
| EK L-2 | The proofs gate accepted a test name found only in a comment, a string or a skipped test | Proofs resolve through `ast` to a real, unskipped `def test_*`; `logical` can never be n/a | `node_converge.proof_problems` |
| EK L-3 | A failed `git diff` against a bad base was read as "no changes" | A tool error inside a gate is a FAIL | `node_converge.changed_files` |
| EK L-4 | Gate self-tests passed with the tools missing | A self-test requires the planted defect's identity in the output | `node_converge.self_test` |
| EK L-5 | Check-only node-start silently moved the changed-tests baseline | Check-only commands never write | `node_start.start` |
| EK L-8 | pydantic `frozen=True` is shallow | Sequences are tuples, mappings frozen, free-form JSON deep-frozen | N1.1 schemas |
| EK L-9 | pydantic floats accept infinity, which dumps as `null` | `allow_inf_nan=False` everywhere | N1.1 schemas |
| EK L-10 | A test looping over `__all__` can never find a missing export | Contract tests compare against an independent list taken from the spec | `test_evalkit_contract.py`, N1.1 |
| EK L-11 | Scripted text replacements silently matched nothing | Every scripted edit asserts its match count | builder practice |
| EK L-12 | A NaN or negative cost estimate switched the spend cap off | Every money value is checked finite and non-negative | N1.6 |
| EK L-15 | A background verifier rebuilt the shared `.venv` mid-converge | Verifiers set `UV_PROJECT_ENVIRONMENT` to a private scratch directory | verifier prompt |
| EK L-16 | The data card stated future work in the present tense | Docs say what is true now and name the node that makes the rest true | verifier |
| EK L-21 | Config and budget errors were recorded as provider transport failures | Only transient provider errors are `transport`; config errors stop the run | N1.6, R3.1 |
| EK L-27 | Float totals differed between Python 3.11 and 3.12 | `math.fsum` for every float total that reaches an output | M2.1 |
| EK L-32 | Implicit text encodings broke on Windows | Every text read and write names `encoding="utf-8"` | `test_text_encoding.py` |
| EK L-33 | Gold answers depended on arbitrary tie-breaks at a LIMIT cut | Top-N gold has no tie at the cut | GLD-03 |
| EK L-35 | A number guard missed "roughly 190k" and number words | Number extraction is tested on formats, words and near-misses | SCR-04, SCR-06 |
| EK L-36 | Resampling trials inside tasks before any()/all() biased pass@k down and pass^k up | pass^k is computed per question, then bootstrapped over questions only | AGG-05 |
| EK L-38 | Correct answers with extra helpful columns failed a strict column count | `sql_match(..., allow_extra_columns=True)` for agent answers | SCR-02 |
| EK L-39 | Guard, counter and executor each used a different cost number | One cost estimate feeds the guard, the counter and the cap | D4.1 |

## This project

| ID | Lesson | Rule | Enforced by |
| --- | --- | --- | --- |
| L-1 | W0.1 verifier: tests for workflows, the catalog and `.env.example` passed on every defect planted in them (a `.yaml` publish workflow, a moved capability, a duplicate key line, a `!.env` un-ignore) | Every check over a config or record file has a planted-defect test beside it, and file checks ask the real tool (`git check-ignore`) rather than reading the file | `test_workflow_check_catches_planted_defects`, `test_git_really_ignores_private_files`, `test_catalog_matches_tech_spec_capabilities` |
