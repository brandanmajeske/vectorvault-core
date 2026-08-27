# V-57 genuine-consumer dogfood — codex-vv lane

Status: negative signal; do not change the default.

## Method

- Consumer: local `gemma4:12b` through Ollama chat completion.
- Inputs: packed VectorVault summaries only. The model had no Vault tools.
- Arms: control `max_tokens=4000`; candidate `max_tokens=750`.
- Order: alternated by repeat to reduce order bias.
- Tasks: three answerable workflows, repeated twice per arm.
- Generation: temperature 0 and seed 57.
- Scoring: predefined required concept groups, with exact answers retained in
  `dogfood/consumer-results-codex-v1.json`.
- Vault access: read-only retrieval under ambient `bmaj` credentials because the
  scoped role cannot be assumed with the current source-identity permission.

The harness and scorer were developed test-first. The focused tests and Ruff pass.

## Result

| Metric | Control 4000 | Candidate 750 |
|---|---:|---:|
| Runs | 6 | 6 |
| Correct | 6 | 4 |
| Accuracy | 100% | 66.7% |
| Actual packed tokens (`o200k_base`) | 5,439 | 4,426 |
| Extra retrievals/hydrations | 0 | 0 |

The candidate saved 1,013 packed tokens (18.6%) but failed the no-task-success-
regression gate. In both candidate repeats, the working-set handoff answer named
`fetch_working_set` but omitted `pin_working_set`. Both control repeats included
the pin and fetch mechanisms. V-54 and V-56 ticket-result answers were correct in
both arms.

## Pilot correction

An initial pilot used three golden questions whose summaries did not contain enough
detail to answer two tasks. Its keyword rubric also admitted one semantically wrong
answer. That pilot was rejected before the final run. The final tasks were limited
to facts explicitly present in the retrieved summaries, and the rubric was aligned
to each task before the reported deterministic rerun.

## Decision

This lane rejects `750` as a general default. It remains suitable only for workflows
that independently prove their required context survives. More peers and broader
tasks can refine the boundary, but this observed task regression is enough to block
a Vault-wide change.
