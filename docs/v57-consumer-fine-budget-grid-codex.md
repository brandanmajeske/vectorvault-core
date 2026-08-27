# V-57 genuine-consumer fine budget grid — codex-vv

Status: `800` is the smallest passing limit tested. The exact packing threshold is
bounded above `750` and at or below `800`. Do not change the global default.

## Method

- Consumer: local `gemma4:12b` through Ollama, temperature 0, seed 57.
- Control: `max_tokens=4000`.
- Candidates: `800`, `810`, `820`, `830`, `840`, and `850`.
- Three fixed answerable tasks and five alternating-order repeats per arm.
- Model input: packed summaries only; no Vault tools or hydration.
- Actual packed tokens: `o200k_base`.
- Vault access: read-only under ambient `bmaj` credentials.

## Results

| Limit | Control correct | Candidate correct | Control tokens | Candidate tokens | Savings |
|---:|---:|---:|---:|---:|---:|
| 800 | 14/15 | 15/15 | 13,592 | 11,795 | 13.2% |
| 810 | 15/15 | 15/15 | 13,625 | 11,736 | 13.9% |
| 820 | 15/15 | 15/15 | 13,614 | 11,736 | 13.8% |
| 830 | 15/15 | 15/15 | 13,614 | 12,909 | 5.2% |
| 840 | 15/15 | 15/15 | 13,614 | 12,909 | 5.2% |
| 850 | 15/15 | 15/15 | 13,614 | 12,898 | 5.3% |

The single `800` control miss occurred on the working-set task with the same full
10-hit, 788-token pack used by every passing candidate run. It named only
`fetch_working_set`. Because context was identical and the candidate passed 5/5,
the miss is model-output variance, not candidate superiority.

## Packing steps

- `800–820`: working-set 10 hits/788 tokens; V-54 nine hits; V-56 eight hits.
  Every candidate answer passed. This is the efficient plateau.
- `830–850`: working-set unchanged; V-54 grows to ten hits and V-56 to nine hits.
  Accuracy stays perfect while savings fall to about 5%.
- Prior `750` evidence had only nine working-set hits/759 tokens and Gemma omitted
  `pin_working_set`. Therefore the relevant threshold lies in `(750, 800]`; this
  sweep does not establish the exact value.

Repeated V-54 retrievals produced two neighbor variants at some limits. Small
differences between `800`, `810`, and `820` totals are live retrieval variance,
not evidence that a larger limit saves more tokens.

## Decision

Use `800` for the next cross-machine opt-in test because it is the smallest passing
limit tested and retains substantially more savings than `830–850`. Do not promote
it globally. Confirm the full working-set pack, model accuracy, and retrieval
stability on the second Vault deployment before considering broader dogfood.

Raw results are in `dogfood/consumer-results-{800,810,820,830,840,850}-r5.json`.
