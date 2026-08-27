# V-57 genuine-consumer budget grid — codex-vv

Status: `850` is the only useful follow-up candidate in this narrow grid. Do not
change the global default.

## Method

- Consumer: local `gemma4:12b` through Ollama, temperature 0, seed 57.
- Control: `max_tokens=4000`.
- Candidates: `850`, `1000`, and `1250`.
- Three fixed answerable tasks, three repeats per arm and alternating arm order.
- Model input: packed summaries only; no Vault tools or extra hydration.
- Token measurement: `o200k_base` over packed summaries.
- Vault access: read-only under ambient `bmaj` credentials.

## Results

| Candidate | Control correct | Candidate correct | Control tokens | Candidate tokens | Savings |
|---:|---:|---:|---:|---:|---:|
| 850 | 8/9 | 9/9 | 8,164 | 7,752 | 5.0% |
| 1000 | 9/9 | 9/9 | 8,175 | 8,175 | 0% |
| 1250 | 9/9 | 9/9 | 8,153 | 8,153 | 0% |

At `850`, the working-set task received the same full 10-hit, 788-token pack in
both arms. This restores the context that `750` truncated to nine hits and 759
tokens. The single control failure named only `fetch_working_set`; candidate runs
with the same context named both pin and fetch. Because the relevant context was
identical, this is model-output variance and not evidence that the candidate is
better than control.

The `850` savings came from the ticket-result tasks while preserving their scored
answers. At `1000`, every tested control pack already fit below the candidate limit,
so the candidate saved nothing. `1250` also saved nothing in aggregate.

Live retrieval composition varied slightly between repeated calls even when the
budget was not binding. For example, one V-54 result set differed at `1250` despite
both arms fitting below the limit. This reinforces the need for repeated runs and
prevents interpreting individual pack differences as budget effects without a
matching token cutoff.

## Decision

Use `850` as the next opt-in dogfood candidate if another machine repeats this
test. It recovered the known `750` context loss and retained about 5% savings on
this run. Do not promote it globally: the task set and model sample remain small,
and model-output plus live retrieval variance are observable.

Raw results:

- `dogfood/consumer-results-850.json`
- `dogfood/consumer-results-1000.json`
- `dogfood/consumer-results-1250.json`
