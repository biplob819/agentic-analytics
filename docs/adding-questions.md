# Adding questions

A question is one line of JSON in the pack's `questions.jsonl`. It counts in a run only after
the gold check has confirmed it on the engines.

## The format

```json
{"schema_version": "1.0", "id": "q-121", "text": "How many property sales were recorded in England and Wales in 2023?", "category": "aggregation", "expected": "answer", "gold_sql": {"clickhouse": "SELECT count() AS sales FROM uk.uk_price_paid WHERE toYear(date) = 2023", "snowflake": "SELECT COUNT(*) AS sales FROM ANALYTICS.UK.UK_PRICE_PAID WHERE YEAR(date) = 2023"}, "interpretations": [], "tolerance": {"rel": 1e-09, "abs": 1e-09, "columns": null}, "ordered": false, "top_n": null, "expects_empty": false, "author": "owner", "owner_reviewed": true}
```

| Field | Meaning |
|---|---|
| `id` | `q-` and three digits, unique in the pack |
| `category` | `aggregation`, `time`, `geography`, `top_n`, `yoy` or `ambiguous` |
| `expected` | `answer`, or `clarify` for (and only for) ambiguous questions |
| `gold_sql` | One query per engine the pack lists; none for ambiguous questions |
| `interpretations` | Ambiguous questions only: at least two distinct readings, each with SQL per engine |
| `tolerance` | Relative and absolute tolerance for numeric columns; `columns` names the columns it covers (null means all numeric columns), the rest match exactly |
| `ordered` | Whether row order matters |
| `top_n` | The N of a top-N question, set exactly for `top_n` questions |
| `expects_empty` | True when the right answer is no rows |
| `author`, `owner_reviewed` | Who wrote it, and whether the owner reviewed it |

## Writing gold SQL

The gold result is the yardstick for every score, so it is linted before it counts:

- Periods are absolute, or relative to the as-of date through the quoted `'{as_of}'`
  placeholder (used on both engines or neither); never "now".
- Exact functions only: no approximate distinct counts or quantiles. Medians use the
  interpolating median on both engines and get a 0.5% tolerance on their columns.
- No `min` or `max` over enum columns, whose order differs between engines.
- Text literals match the stored case (the UK pack stores place names in uppercase).
- Top-N questions end in `LIMIT n` with n equal to `top_n` and an `ORDER BY` with a
  tie-break column; the gold check also requires no tie at the cut. Other answer questions
  have no `LIMIT`.
- Every division on Snowflake casts an operand to `DOUBLE`.
- Keep gold results small: 200 rows or fewer.

## Check it

```bash
agentic-analytics pack check --pack my_pack
agentic-analytics gold --only q-121 --pack my_pack
```

`pack check` validates the line and lints its SQL with no accounts. `gold --only` runs the new
question's gold SQL on the engines and confirms it before it counts (it arrives with build node
Q2.2; until then the command validates the inputs and exits 2). Category quotas in `pack.yaml`
decide how many questions per category a run selects, with a fixed seed.

## Ambiguous questions

An ambiguous question has no single right answer, so it is scored on whether the backend asks
back. Give it at least two `interpretations` that return different results, so the ambiguity is
real. Backends that ask back on clear questions are penalised by the false-clarification rate,
which the dashboard shows beside the clarification rate.
