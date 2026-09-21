#!/usr/bin/env python3
"""jevsql — text-to-SQL where the model never writes SQL.

A catalog of Snowflake tables (columns, sample values, probed
state/county columns, editable descriptions) is built offline with
`--build-catalog`. At query time Jev (TypeSafe AI) makes only typed
judgments over that catalog: which table, which state, which column a
number constrains — each with a calibrated confidence. Code does the
exact parts (number parsing, column probing, SQL assembly). The model
cannot hallucinate SQL because it never produces any.

Usage:
  python jevsql.py --build-catalog      # (re)build jevsql_catalog.json
  python jevsql.py                      # interactive prompt
  python jevsql.py "your question"      # one-shot
  python jevsql.py --demo               # aliases + SQL only, no execution

Auth: set TYPESAFE_API_KEY, SNOWFLAKE_PAT, SNOWFLAKE_ACCOUNT, and
SNOWFLAKE_USER in the environment (see README).

To improve routing, edit "desc" fields in jevsql_catalog.json by hand
(rebuilds preserve desc and alias).
"""
import json
import os
import re
import sys
import time

import snowflake.connector
from typesafe_sdk import Choice, Noul, TypeSafeClient

CREDS = os.environ.get("JEVSQL_CREDS_FILE", "")  # optional local fallback file
CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jevsql_catalog.json")
CONF_THRESHOLD = 0.70
SKIP_DBS = "('SNOWFLAKE', 'INFORMATION_SCHEMA')"

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


# ---- credentials: env vars first; optional local file fallback -------------
# secrets are parsed in pure Python and never printed (no subprocess: forking
# after the Snowflake connector initializes segfaults on macOS)
def _local_cred(pattern: str) -> str:
    if not CREDS:
        return ""
    try:
        with open(CREDS) as f:
            m = re.search(pattern, f.read())
        return m.group(1).strip() if m else ""
    except Exception:
        return ""


def jev_key() -> str:
    return os.environ.get("TYPESAFE_API_KEY") or _local_cred(
        r"(?im)^.*jev.*api key.*\n+(apikey_\S+)") or sys.exit(
        "set TYPESAFE_API_KEY")


def sf_conn():
    pat = os.environ.get("SNOWFLAKE_PAT") or _local_cred(
        r"(?im)^\**Current PAT[^\n]*\n+\s*(\S{20,})") or sys.exit(
        "set SNOWFLAKE_PAT")
    user = os.environ.get("SNOWFLAKE_USER") or sys.exit("set SNOWFLAKE_USER")
    account = os.environ.get("SNOWFLAKE_ACCOUNT") or sys.exit("set SNOWFLAKE_ACCOUNT")
    return snowflake.connector.connect(
        user=user, password=pat, account=account, login_timeout=30)


def q(conn, sql: str, params=None):
    cur = conn.cursor()
    cur.execute(sql, params)
    return [d[0] for d in cur.description], cur.fetchall()


# ---------------------------------------------------------------- catalog ---
def _probe_col(conn, table: str, col: str, value_regex: str) -> bool:
    """Deterministic check: do this column's values look like the target?"""
    try:
        _, rows = q(conn,
                    f"SELECT COUNT_IF(UPPER(TO_VARCHAR(\"{col}\")) RLIKE %(rx)s)"
                    f" / NULLIF(COUNT(*), 0) FROM"
                    f" (SELECT \"{col}\" FROM {table}"
                    f" WHERE TO_VARCHAR(\"{col}\") <> '' LIMIT 200)",
                    {"rx": value_regex})
        return (rows[0][0] or 0) >= 0.8
    except Exception:
        return False


def build_catalog(conn) -> dict:
    print("building catalog from Snowflake metadata...")
    old = {}
    if os.path.exists(CATALOG):
        with open(CATALOG) as f:
            old = json.load(f)

    _, dbs = q(conn, "SHOW DATABASES")
    tables = []
    for db in (r[1] for r in dbs):
        if db in ("SNOWFLAKE",) or db.startswith("USER$"):
            continue
        try:
            _, rows = q(conn,
                        f'SELECT table_catalog, table_schema, table_name, row_count'
                        f' FROM "{db}".INFORMATION_SCHEMA.TABLES'
                        f" WHERE table_schema <> 'INFORMATION_SCHEMA'"
                        f" AND row_count > 1000")
            tables += rows
        except Exception:
            pass
    tables.sort(key=lambda r: -(r[3] or 0))
    tables = tables[:254]

    catalog = {}
    for db, schema, tbl, n in tables:
        name = f'"{db}"."{schema}"."{tbl}"'
        try:
            _, cl_rows = q(conn,
                           f'SELECT column_name FROM "{db}".INFORMATION_SCHEMA.COLUMNS'
                           f" WHERE table_schema = %(s)s AND table_name = %(t)s"
                           f" ORDER BY ordinal_position",
                           {"s": schema, "t": tbl})
            cl = [r[0] for r in cl_rows]
        except Exception:
            continue
        entry = {"rows": n, "cols": cl,
                 "desc": old.get(name, {}).get("desc", ""),
                 "state_col": "none", "county_col": "none", "sample": {}}
        if old.get(name, {}).get("alias"):
            entry["alias"] = old[name]["alias"]
        try:
            names, rows = q(conn, f"SELECT * FROM {name} LIMIT 1")
            if rows:
                entry["sample"] = {c: str(v)[:14] for c, v in
                                   list(zip(names, rows[0]))[:8]}
        except Exception:
            pass
        # prefer physical-location columns over mailing-address ones
        for c in sorted((c for c in cl if "state" in c.lower()),
                        key=lambda c: "mail" in c.lower()):
            if _probe_col(conn, name, c, "^[A-Z]{2}$"):
                entry["state_col"] = c
                break
        for c in sorted((c for c in cl if "county" in c.lower()
                         and "code" not in c.lower()),
                        key=lambda c: "mail" in c.lower()):
            if _probe_col(conn, name, c, r"^[A-Z .'\-]{3,}$"):
                entry["county_col"] = c
                break
        catalog[name] = entry
        print(f"  {name}: {n:,} rows, state={entry['state_col']}, "
              f"county={entry['county_col']}")
    with open(CATALOG, "w") as f:
        json.dump(catalog, f, indent=1)
    print(f"wrote {CATALOG} ({len(catalog)} tables) — edit 'desc' fields to "
          "improve routing")
    return catalog


def load_catalog(conn) -> dict:
    if not os.path.exists(CATALOG):
        return build_catalog(conn)
    with open(CATALOG) as f:
        return json.load(f)


def _route_desc(name: str, meta: dict) -> str:
    """What Jev sees for each table when routing."""
    parts = []
    if meta["desc"]:
        parts.append(meta["desc"])
    parts.append(f"{meta['rows']:,} rows")
    parts.append("columns: " + ", ".join(meta["cols"][:12]))
    if meta["sample"]:
        parts.append("sample: " + ", ".join(
            f"{k}={v}" for k, v in list(meta["sample"].items())[:5] if v))
    return "; ".join(parts)


# ----------------------------------------------------------------- numbers --
NUM_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*(k|m|b|thousand|million|billion)?\b", re.I)
MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def extract_numbers(text: str) -> list[float]:
    out = []
    for num, suffix in NUM_RE.findall(text):
        v = float(num.replace(",", ""))
        out.append(v * MULT.get(suffix.lower(), 1) if suffix else v)
    return out


# -------------------------------------------------------------------- query -
def run(question: str, conn, jev, catalog: dict, demo: bool = False) -> None:
    shown_name = lambda t: catalog[t].get("alias", t) if demo else t
    numbers = extract_numbers(question)  # exact computation: code, not model

    # ---- Pass 1: route to a table + global slots ----------------------------
    t0 = time.time()
    r1 = jev.system_one(
        state=question,
        questions={
            "intent": Choice(instructions="What does the user want back",
                             criteria={"count": "A count of matching records",
                                       "list": "The actual records/rows",
                                       "other": "Not answerable from a database table"}),
            "table": Choice(
                instructions="Which table best answers this question, judged by its "
                             "description, columns and sample values",
                criteria={n: _route_desc(n, m) for n, m in catalog.items()}),
            "state": Choice(instructions="Which US state the question is about",
                            criteria={**STATES, "none": "No specific state mentioned"}),
            "mentions_county": Noul(instructions="The question refers to a specific US county"),
        },
    )
    pass1_ms = (time.time() - t0) * 1000
    a = r1.answers
    core = {k: (a[k].choice, a[k].confidence) for k in ("intent", "table", "state")}
    low = [k for k in ("intent", "state") if core[k][1] < CONF_THRESHOLD]
    if low or core["intent"][0] == "other":
        shown = {k: (shown_name(v) if k == "table" else v, round(c, 2))
                 for k, (v, c) in core.items()}
        print(f"Not confident enough on {low or 'intent'} — please rephrase. "
              f"Understood so far: {shown}")
        return
    # table routing: many tables legitimately overlap, so accept the leader
    # above a lower floor and surface the alternates it weighed
    ranked = sorted(a["table"].probabilities.items(), key=lambda x: -x[1])
    alternates = [(shown_name(t), p) for t, p in ranked[1:4] if p >= 0.05]
    if core["table"][1] < 0.30:
        print("No table fits well enough. Best guesses: "
              + ", ".join(f"{shown_name(t)} ({p:.2f})" for t, p in ranked[:3]))
        return
    table = core["table"][0]
    meta = catalog[table]

    # ---- Pass 2: bind extracted numbers to columns --------------------------
    filters, unbound, pass2_ms = [], [], 0.0
    if numbers:
        col_choice = {c: c for c in meta["cols"][:254]}
        q2 = {}
        for i, n in enumerate(numbers):
            q2[f"num{i}_col"] = Choice(
                instructions=f"The question contains the number {n:g}. Which column "
                             "does it constrain, if any",
                criteria={**col_choice, "none": "It doesn't constrain any column "
                                                "(e.g. it's part of a name or date)"})
            q2[f"num{i}_op"] = Choice(
                instructions=f"How the number {n:g} is used as a threshold",
                criteria={"min": "At least / more than this value",
                          "max": "At most / less than this value",
                          "eq": "Exactly this value"})
        t0 = time.time()
        b = jev.system_one(state=question, questions=q2).answers
        pass2_ms = (time.time() - t0) * 1000
        for i, n in enumerate(numbers):
            colq = b[f"num{i}_col"]
            if colq.choice != "none" and colq.confidence >= CONF_THRESHOLD:
                filters.append((colq.choice, b[f"num{i}_op"].choice, n, colq.confidence))
            else:
                unbound.append((n, colq.choice, colq.confidence))

    # ---- Pass 3 (cascade): county resolution, scoped to state + table -------
    county, county_conf, pass3_ms = None, None, 0.0
    st_col, ct_col = meta["state_col"], meta["county_col"]
    if (a["mentions_county"].noul > 0.6 and core["state"][0] != "none"
            and ct_col != "none" and st_col != "none"):
        st = core["state"][0]
        _, county_rows = q(conn,
                           f'SELECT DISTINCT "{ct_col}" FROM {table}'
                           f' WHERE "{st_col}" = %(st)s AND "{ct_col}" <> \'\''
                           " ORDER BY 1 LIMIT 254", {"st": st})
        counties = [r[0] for r in county_rows]
        t0 = time.time()
        r3 = jev.system_one(state=question, questions={"county": Choice(
            instructions=f"Which {STATES[st]} county the question refers to",
            criteria={**{c: f"{c.title()} County, {STATES[st]}" for c in counties},
                      "none": "No county in this list matches"})})
        pass3_ms = (time.time() - t0) * 1000
        c3 = r3.answers["county"]
        if c3.choice != "none" and c3.confidence >= CONF_THRESHOLD:
            county, county_conf = c3.choice, c3.confidence

    # ---- Assemble SQL from typed slots only ---------------------------------
    where, params = [], {}
    if core["state"][0] != "none" and st_col != "none":
        where.append(f'"{st_col}" = %(state)s'); params["state"] = core["state"][0]
    if county:
        where.append(f'"{ct_col}" = %(county)s'); params["county"] = county
    ops = {"min": ">=", "max": "<=", "eq": "="}
    for col, op, v, _ in filters:
        where.append(f'TRY_TO_DOUBLE(TO_VARCHAR("{col}")) {ops[op]} {v:g}')
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    from_name = shown_name(table)
    if core["intent"][0] == "count":
        sql = f"SELECT COUNT(*) AS record_count FROM {from_name}{where_sql}"
    else:
        shown = ", ".join(f'"{c}"' for c in meta["cols"][:6])
        sql = f"SELECT {shown} FROM {from_name}{where_sql} LIMIT 25"
    if demo:  # inline literals for readability; demo mode never executes
        for k, v in params.items():
            sql = sql.replace(f"%({k})s", f"'{v}'")

    # ---- Report -------------------------------------------------------------
    print(f'\nQ: "{question}"\n')
    print("Typed slots (Jev, confidence-calibrated):")
    print(f"  {'intent':<10} {core['intent'][0]:<40} conf={core['intent'][1]:.2f}")
    print(f"  {'table':<10} {shown_name(table):<40} conf={core['table'][1]:.2f}")
    if alternates:
        print(f"  {'':<10} alternates: "
              + ", ".join(f"{t} ({p:.2f})" for t, p in alternates))
    if core["state"][0] != "none":
        print(f"  {'state':<10} {core['state'][0]:<40} conf={core['state'][1]:.2f}")
    if county:
        print(f"  {'county':<10} {county:<40} conf={county_conf:.2f}")
    for col, op, v, conf in filters:
        print(f"  {'number':<10} {f'{v:g} -> {col} ({ops[op]})':<40} conf={conf:.2f}")
    for n, guess, conf in unbound:
        print(f"  {'number':<10} {n:g} NOT APPLIED — best guess {guess} at "
              f"conf={conf:.2f}, below {CONF_THRESHOLD} threshold")
    total_ms = pass1_ms + pass2_ms + pass3_ms
    # input $0.042/MTok, output free; rough token estimate = chars/4
    est_cost = (len(question) + 24000) / 4 * 0.042 / 1e6
    print(f"\nSQL built in {total_ms:.0f}ms of model time "
          f"(pass1 {pass1_ms:.0f}"
          + (f" + numbers {pass2_ms:.0f}" if pass2_ms else "")
          + (f" + county {pass3_ms:.0f}" if pass3_ms else "")
          + f"ms), est. cost ~${est_cost:.5f}")
    print(f"\nSQL (assembled by code, zero model-written SQL):\n  {sql}\n")

    if demo:
        return
    t0 = time.time()
    names, rows = q(conn, sql, params)
    print(f"query ({(time.time() - t0) * 1000:.0f}ms):")
    widths = [max(len(str(c)), 12) for c in names]
    print("  " + "  ".join(str(c).ljust(w) for c, w in zip(names, widths)))
    for row in rows:
        print("  " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def main() -> None:
    conn = sf_conn()
    if "--build-catalog" in sys.argv:
        build_catalog(conn)
        return
    jev = TypeSafeClient(api_key=jev_key())
    catalog = load_catalog(conn)
    demo = "--demo" in sys.argv
    if demo:
        catalog = {k: v for k, v in catalog.items() if v.get("alias")}
        print("catalog:")
        for k, v in catalog.items():
            print(f"  {v['alias']:<18} {v['rows']:>12,} rows   "
                  f"{len(v['cols'])} columns")
    else:
        print(f"catalog: {len(catalog)} tables")
    args = [x for x in sys.argv[1:] if not x.startswith("--")]
    if args:
        run(args[0], conn, jev, catalog, demo)
        return
    print("jevsql — ask anything about the data (empty line to quit)")
    while True:
        try:
            question = input("\n? ").strip()
        except EOFError:
            break
        if not question:
            break
        run(question, conn, jev, catalog, demo)


if __name__ == "__main__":
    main()
