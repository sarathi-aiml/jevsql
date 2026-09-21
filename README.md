# jevsql — text-to-SQL where the model never writes SQL

A weekend experiment with [Jev](https://typesafe.ai/), TypeSafe AI's "System One"
model that returns **typed, probability-calibrated decisions instead of text**.

## How Jev differs from a normal LLM

A generative LLM produces tokens: you send a prompt, it writes an answer one
token at a time, and if you need structure you parse it out and hope. Jev
never generates text at all. You send your program's **state** plus a battery
of **typed questions**, and it answers all of them in one parallel pass:

```python
r = client.system_one(
    state="I was charged twice for order A-104. Please refund the duplicate.",
    questions={
        "department": Choice(instructions="Which team should handle this",
                             criteria={"billing": "Payments/refunds",
                                       "technical": "Bugs",
                                       "sales": "Pricing questions"}),
        "frustration": Score(instructions="How frustrated is the customer",
                             criteria=["Calm", "Frustrated but civil", "Angry"]),
        "wants_refund": Noul(instructions="Customer explicitly asks for a refund"),
    },
)
r.answers["department"].choice       # "billing"
r.answers["department"].confidence   # 0.97  <- calibrated: right ~97% of the time
r.answers["frustration"].score       # 1.2   (between "Calm" and "Frustrated")
r.answers["wants_refund"].noul       # 0.94  (probability of yes)
```

Three primitives, that's the whole API: **Choice** (pick one of up to 255
options), **Score** (a position on an ordered scale), **Noul** (yes/no as a
probability). Your code branches on the answers the way it branches on any
other value. The distinction in practice:

| | Generative LLM (GPT-5, Claude, ...) | Jev (System One) |
|---|---|---|
| Output | Free text / JSON you parse | Typed values, no parsing |
| Structure errors | 0.6–45% depending on model/mode | 0% by construction |
| Confidence | Vibes ("I'm fairly sure...") | Calibrated probability per answer (RLCD-trained: 0.8 means right ~80% of the time) |
| 10 questions | ~10x the output cost/latency | ~same latency as 1 (parallel pass) |
| Latency | 2–6 s typical | 70–500 ms |
| Price | $1.25–$10 /MTok in + output billed | $0.042 /MTok in, **output free** |
| Good at | Composing anything: prose, code, SQL | Deciding: route, classify, rank, gate |
| Can't do | Be cheap/fast/calibrated enough to run on every record | Generate anything — no text, no code |

Neither replaces the other. Jev is the cheap, fast, calibrated decision layer;
generative models remain the composition layer. The natural architecture is a
cascade: Jev decides *what* to do on every request, a generative model is
invoked only for the fraction that needs one.

## The idea I tried: text-to-SQL

Traditional text-to-SQL asks a generative LLM to write a SQL string, then hopes
it referenced real tables, real columns, and nothing injectable. Jev's
constraint flips the design: **the model cannot hallucinate SQL because it
never produces any.** It only answers typed questions my code defines — which
table, which state, which column does the number 10,000 constrain — each with
a calibrated confidence. Code assembles those answers onto a parameterized
SELECT.

```
? how many companies in Kollin county texas with more than 10 employees

Typed slots (Jev, confidence-calibrated):
  intent     count        conf=1.00
  table      business     conf=1.00
  state      TX           conf=1.00
  county     COLLIN       conf=0.99     <- misspelling resolved semantically
  number     10 -> NoOfEmployees (>=)   conf=0.93

SQL built in 647ms of model time, est. cost ~$0.00025

SELECT COUNT(*) AS record_count FROM business
WHERE "StateCode" = 'TX' AND "EXcountyname" = 'COLLIN'
  AND TRY_TO_DOUBLE(TO_VARCHAR("NoOfEmployees")) >= 10
```

## How it works

```
--build-catalog (offline, no model):
  [Snowflake metadata] -> tables, columns, 1 sample row
                       -> deterministic probe: which column holds
                          state codes / county names (value regex)
                       -> jevsql_catalog.json  (desc/alias fields hand-editable)

query time:
  [prompt] -> pass 1: one parallel Jev call
              intent (count/list) | table (Choice over catalog) |
              state (50 states + none) | mentions_county (Noul)
           -> numbers extracted by regex in code ("5 million" -> 5000000)
           -> pass 2: per number, Jev picks the column it constrains
              + direction (>= / <= / =)
           -> pass 3 (only if a county was mentioned): Jev Choice over
              that state's actual county list, pulled live from the DB
           -> code assembles SQL from typed slots only
  any slot below 0.70 confidence -> refuse / ask, never guess
```

Three design points worth stealing:

- **Speculative fan-out.** Jev evaluates questions in parallel over one state
  ingestion, so asking ten questions costs barely more than one. Ask
  everything up front; let code decide what mattered.
- **Split exact from semantic.** Code does what code is good at (regex number
  parsing, value probing, SQL assembly). The model only makes judgment calls
  (which table, which column). Neither does the other's job.
- **Calibration as UX.** When the user says "properties worth less than 500k"
  and the table has three near-identical value columns, Jev's confidence
  collapses to 0.48 and the filter is refused with the best guess shown —
  instead of silently picking one and returning a confidently wrong count.
  Rephrase as "assessed total value under 500k" and it binds at 0.99.

## More samples

Mixed criteria, both directions, one question:

```
? businesses in Texas with more than 50 employees and less than 5 million in sales

  intent     list         conf=0.81
  table      business     conf=1.00
  state      TX           conf=1.00
  number     50 -> NoOfEmployees (>=)   conf=0.94
  number     5e+06 -> Revenue (<=)      conf=0.87

SELECT ... FROM business WHERE "StateCode" = 'TX'
  AND TRY_TO_DOUBLE(TO_VARCHAR("NoOfEmployees")) >= 50
  AND TRY_TO_DOUBLE(TO_VARCHAR("Revenue")) <= 5000000 LIMIT 25
```

No state mentioned — no filter invented (447ms, whole country):

```
? How many businesses have more than 10,000 in revenue?

  intent     count        conf=1.00
  table      business     conf=0.91
  number     10000 -> Revenue (>=)      conf=0.97
```

Ambiguity refused, not guessed — the table has several near-identical value
columns, so "worth" honestly can't be resolved:

```
? properties in Iowa over 100 acres worth less than 500k

  number     100 -> ACRES (>=)          conf=1.00
  number     500000 NOT APPLIED — best guess ASSESSED_TOTAL_VALUE
             at conf=0.48, below 0.70 threshold
```

Rephrase precisely and the same filter binds at 0.99
("...with assessed total value under 500k").

Off-topic fails closed — zero SQL runs:

```
? what is the weather tomorrow

Not confident enough on intent — please rephrase.
Understood so far: {'intent': ('other', 1.0), 'table': ('business', 0.42),
                    'state': ('none', 1.0)}
```

## Measured time and cost

From my runs against a 4-table catalog (tables of 10M–266M rows):

| Metric | Measured |
|---|---|
| Model time to build SQL (1–2 questions) | 440–660 ms |
| Model time incl. county resolution (3 passes) | 650–860 ms |
| Est. input tokens per query | ~6K (catalog + questions) |
| Est. cost per query | **~$0.00025** ($0.042/MTok input, output free) |

### The same job on a generative LLM

Estimated for the identical task (same ~6K-token schema context in the prompt,
~250 output tokens of SQL/JSON), at published API list prices as of Sept 2026:

| | Jev (typed slots) | Claude Sonnet 4.5 ($3/$15 per MTok) | GPT-5 ($1.25/$10 per MTok) |
|---|---|---|---|
| Cost per query | ~$0.00025 | ~$0.022 | ~$0.010 |
| Relative cost | 1x | **~85x** | **~40x** |
| Typical latency | 0.4–0.9 s | 2–6 s | 2–6 s |
| Calibrated confidence | yes, per slot | no | no |
| Structured-output errors | 0% by construction | possible | possible |

Honest caveats: these are estimates, not benchmarks — token counts vary with
schema size, and generative latency varies with reasoning settings. And the
comparison is not apples-to-apples on *capability*: a generative model can
compose arbitrary SQL (joins, CTEs, window functions); Jev can only fill slots
for query shapes you pre-authored. That constraint is the point — for the
constrained shapes, you get the speed, the cost, and the impossibility of
hallucinated SQL. For open-ended analytics you still want a generative model
(or a proper semantic-layer product); the two compose naturally as a cascade.

## Reproduce it

Requires Python 3.10+, a Snowflake account (or port `q()`/`sf_conn()` to your
warehouse — the model layer doesn't care), and a TypeSafe API key
(waitlisted at their console, or via the Vercel AI gateway).

```bash
python -m venv .venv
.venv/bin/pip install typesafe-sdk snowflake-connector-python

export TYPESAFE_API_KEY=apikey_...
export SNOWFLAKE_USER=...
export SNOWFLAKE_ACCOUNT=...
export SNOWFLAKE_PAT=...          # personal access token (or password)

# 1. build the catalog from your warehouse metadata (one-time, no model calls)
.venv/bin/python jevsql.py --build-catalog

# 2. (recommended) edit jevsql_catalog.json: add a one-line "desc" to your
#    main tables, and an "alias" to the ones you want in demo mode.
#    A single sentence of description moved my routing confidence from
#    0.32-wrong-table to 0.91-right-table.

# 3. ask
.venv/bin/python jevsql.py "how many farms in Texas with more than 100 acres"
.venv/bin/python jevsql.py            # interactive prompt
.venv/bin/python jevsql.py --demo     # aliases + SQL only, no execution
```

Use a **read-only** database user. Keep `jevsql_catalog.json` out of git — it
contains your real table names, columns, and sample values (it's in
`.gitignore` here).

## Scope

Deliberately small: count/list intents, state/county filters, numeric
thresholds. No joins, no GROUP BY, no CTEs, no date logic — questions outside
the shapes fail closed with a low-confidence refusal instead of a guess.
"what is the weather tomorrow" → `intent=other @ 1.00`, zero SQL run.
