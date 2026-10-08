"""Load a project's functional test data (<fabric-dir>/test_data/*.csv) into the Fabric TEST Warehouse.

Shared by all projects of the organisation (FreeFabricDemo/fabric-deploy, called by the reusable workflow).
Project files in <fabric-dir> (default ./Fabric of the calling repository): test_environment.json,
warehouse_schema.sql, schema.json, test_data/<TABLE>.csv.

Drops and recreates the 7 GSC_RR_* tables (Fabric/warehouse_schema.sql) and inserts the rows with multi-row INSERTs
(Fabric Warehouse writes one file per statement, so row-by-row inserts would be slow).

Authentication: Microsoft Entra token from azure-identity (DefaultAzureCredential: the GitHub Actions login of the
service principal, or `az login` locally). Requires pyodbc and the "ODBC Driver 18 for SQL Server".

Tables are created in the project's own schema (`warehouse_schema` in test_environment.json, e.g. dbo_bom), so
several projects can share one Warehouse. Tables of this project left in an old schema (`legacy_schemas` in
test_environment.json, e.g. dbo before the move to dbo_bom) are dropped first - only the project's own table names.

Optional "post_load_sql" in test_environment.json: a SQL file of the project (path relative to <fabric-dir>) run after
the CSV data, e.g. to generate volume test data inside the Warehouse with INSERT ... SELECT instead of committing huge
CSV files. {schema} in the file is replaced by warehouse_schema; statements are split at semicolons (no semicolon
inside string literals), lines starting with -- are skipped.

Usage:  python3 scripts/load_warehouse.py [--fabric-dir Fabric] [--server <sql endpoint>] [--database <warehouse>]
"""
import argparse
import csv
import datetime
import decimal
import json
import os
import struct

import pyodbc
from azure.identity import DefaultAzureCredential

SQL_COPT_SS_ACCESS_TOKEN = 1256
BATCH = 500


def connect(server, database):
    token = DefaultAzureCredential().get_token("https://database.windows.net/.default").token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token)}s", len(token), token)
    conn_str = (f"Driver={{ODBC Driver 18 for SQL Server}};Server={server},1433;Database={database};"
                "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60")
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}, autocommit=True)


def literal(value, col):
    """CSV text -> T-SQL literal following the column type ('' = NULL, as in Oracle)."""
    if value == "":
        return "NULL"
    t = col["type"]
    if t.startswith("DECIMAL"):
        return str(decimal.Decimal(value))
    if t.startswith("DATETIME2"):
        datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")   # validate
        return f"'{value}'"
    return "'" + value.replace("'", "''") + "'"


def sql_statements(text, schema=None):
    """Statements of a SQL script: split at semicolons, comment lines (--) dropped, {schema} replaced."""
    if schema is not None:
        text = text.replace("{schema}", schema)
    statements = []
    for part in text.split(";"):
        stmt = "\n".join(l for l in part.splitlines() if not l.strip().startswith("--")).strip()
        if stmt:
            statements.append(stmt)
    return statements


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--fabric-dir", default="Fabric")
    HERE = os.path.abspath(pre.parse_known_args()[0].fabric_dir)
    env = json.load(open(os.path.join(HERE, "test_environment.json"), encoding="utf-8"))
    data_source = env.get("data_source", "csv")
    if data_source == "synapse":
        from load_synapse import load_synapse_data
        load_synapse_data(env)
        return
    if data_source != "csv":
        raise SystemExit(f"unsupported data_source: {data_source}")
    ap = argparse.ArgumentParser(parents=[pre])
    ap.add_argument("--server", default=env["warehouse_server"])
    ap.add_argument("--database", default=env["warehouse_database"])
    a = ap.parse_args()
    ws = env["warehouse_schema"]
    legacy = [s for s in env.get("legacy_schemas", []) if s.lower() != ws.lower()]
    schema = json.load(open(os.path.join(HERE, "schema.json"), encoding="utf-8"))
    conn = connect(a.server, a.database)
    cur = conn.cursor()
    for old in legacy:
        for table in schema:
            cur.execute(f"DROP TABLE IF EXISTS {old}.{table}")
        print(f"Dropped the project's tables from the old schema {old} (if present)")
    ddl = open(os.path.join(HERE, "warehouse_schema.sql"), encoding="utf-8").read()
    for stmt in sql_statements(ddl):
        cur.execute(stmt)
    for table, cols in schema.items():
        with open(os.path.join(HERE, "test_data", f"{table}.csv"), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        header, data = rows[0], rows[1:]
        assert header == [c["name"] for c in cols], table
        col_list = ", ".join(header)
        for i in range(0, len(data), BATCH):
            values = ",\n".join("(" + ", ".join(literal(v, c) for v, c in zip(r, cols)) + ")" for r in data[i:i + BATCH])
            cur.execute(f"INSERT INTO {ws}.{table} ({col_list}) VALUES\n{values}")
        n = cur.execute(f"SELECT COUNT(*) FROM {ws}.{table}").fetchone()[0]
        print(f"{table}: {n} rows (expected {len(data)})")
        if n != len(data):
            raise SystemExit(f"row count mismatch in {table}")
    post_load = env.get("post_load_sql")
    if post_load:
        statements = sql_statements(open(os.path.join(HERE, post_load), encoding="utf-8").read(), ws)
        for i, stmt in enumerate(statements, 1):
            print(f"{post_load} {i}/{len(statements)}: {stmt.splitlines()[0][:100]}", flush=True)
            cur.execute(stmt)
        for table in schema:
            n = cur.execute(f"SELECT COUNT(*) FROM {ws}.{table}").fetchone()[0]
            print(f"{table}: {n} rows after {post_load}")


if __name__ == "__main__":
    main()
