"""Copy configured Synapse serverless Gold views into a project's Fabric Warehouse schema."""
import re
import struct

import pyodbc
from azure.identity import DefaultAzureCredential

SQL_COPT_SS_ACCESS_TOKEN = 1256
SQL_SCOPE = "https://database.windows.net/.default"
BATCH = 500
TABLES = ("d_products", "d_regions", "d_dates", "f_planning_book")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(value):
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid SQL identifier: {value!r}")
    return f"[{value}]"


def fabric_type(data_type, max_length, precision, scale):
    source_type = data_type.lower()
    if source_type in ("nvarchar", "nchar", "varchar", "char"):
        if max_length == -1:
            return "VARCHAR(8000)"
        char_length = max(1, max_length)
        return f"VARCHAR({min(8000, max(char_length * 4, 1))})"
    if source_type in ("decimal", "numeric"):
        p = min(max(int(precision or 38), 1), 38)
        s = min(max(int(scale or 0), 0), p)
        return f"DECIMAL({p},{s})"
    if source_type in ("bigint", "int", "smallint", "tinyint", "bit", "float", "real",
                       "date", "datetime", "datetime2", "smalldatetime", "time"):
        return {"smalldatetime": "DATETIME2(0)", "datetime": "DATETIME2(3)"}.get(source_type, source_type.upper())
    if source_type == "money":
        return "DECIMAL(19,4)"
    if source_type == "smallmoney":
        return "DECIMAL(10,4)"
    if source_type == "uniqueidentifier":
        return "CHAR(36)"
    if source_type in ("binary", "varbinary"):
        if max_length == -1:
            return "VARBINARY(8000)"
        return f"VARBINARY({min(max(int(max_length), 1), 8000)})"
    raise ValueError(f"unsupported Synapse column type: {data_type}")


def connect(server, database, credential):
    token = credential.get_token(SQL_SCOPE).token.encode("utf-16-le")
    token_struct = struct.pack(f"<I{len(token)}s", len(token), token)
    conn_str = (f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{server},1433;Database={database};"
                "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60")
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token_struct}, autocommit=True)


def get_columns(cursor, source_schema, table):
    rows = cursor.execute("""
        SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
    """, source_schema, table).fetchall()
    if not rows:
        raise RuntimeError(f"source view {source_schema}.{table} has no visible columns")
    return [{"name": row[0], "type": fabric_type(row[1], row[2], row[3], row[4])} for row in rows]


def load_synapse_data(env, credential=None, connect_fn=connect):
    source = env["synapse"]
    source_schema = quote_identifier(source.get("schema", "dbo"))
    warehouse_schema = quote_identifier(env["warehouse_schema"])
    if set(source.get("tables", TABLES)) != set(TABLES):
        raise ValueError(f"Synapse source tables must be exactly: {', '.join(TABLES)}")

    credential = credential or DefaultAzureCredential()
    source_conn = connect_fn(source["server"], source["database"], credential)
    target_conn = connect_fn(env["warehouse_server"], env["warehouse_database"], credential)
    source_cursor, target_cursor = source_conn.cursor(), target_conn.cursor()
    metadata = {table: get_columns(source_cursor, source_schema[1:-1], table) for table in TABLES}

    target_cursor.execute(f"IF SCHEMA_ID(N'{warehouse_schema[1:-1]}') IS NULL EXEC('CREATE SCHEMA {warehouse_schema}')")
    for table, columns in metadata.items():
        target_table = f"{warehouse_schema}.{quote_identifier(table)}"
        target_cursor.execute(f"DROP TABLE IF EXISTS {target_table}")
        column_ddl = ",\n".join(f"    {quote_identifier(col['name'])} {col['type']} NULL" for col in columns)
        target_cursor.execute(f"CREATE TABLE {target_table} (\n{column_ddl}\n)")

        column_list = ", ".join(quote_identifier(col["name"]) for col in columns)
        placeholders = ", ".join("?" for _ in columns)
        insert_sql = f"INSERT INTO {target_table} ({column_list}) VALUES ({placeholders})"
        select_sql = f"SELECT {column_list} FROM {source_schema}.{quote_identifier(table)}"
        source_cursor.execute(select_sql)
        target_cursor.fast_executemany = True
        copied = 0
        while True:
            rows = source_cursor.fetchmany(BATCH)
            if not rows:
                break
            for row in rows:
                for value, col in zip(row, columns):
                    if isinstance(value, str) and col["type"].startswith("VARCHAR("):
                        limit = int(col["type"].removeprefix("VARCHAR(").removesuffix(")"))
                        if len(value.encode("utf-8")) > limit:
                            raise ValueError(f"{table}.{col['name']} contains text exceeding {limit} UTF-8 bytes")
            target_cursor.executemany(insert_sql, rows)
            copied += len(rows)
        actual = target_cursor.execute(f"SELECT COUNT_BIG(*) FROM {target_table}").fetchone()[0]
        print(f"{table}: {actual} rows (copied {copied})")
        if actual != copied:
            raise SystemExit(f"row count mismatch in {warehouse_schema}.{table}: expected {copied}, got {actual}")

    source_conn.close()
    target_conn.close()
