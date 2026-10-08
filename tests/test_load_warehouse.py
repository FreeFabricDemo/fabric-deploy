import os
import sys
import types
import unittest

sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))
azure = types.ModuleType("azure")
azure.__path__ = []
identity = types.ModuleType("azure.identity")
identity.DefaultAzureCredential = object
sys.modules.setdefault("azure", azure)
sys.modules.setdefault("azure.identity", identity)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

from load_warehouse import sql_statements


class SqlStatementsTests(unittest.TestCase):
    def test_split_at_semicolons_and_drop_comment_lines(self):
        script = "-- header\nDROP TABLE IF EXISTS {schema}.T;\n\n-- create\nCREATE TABLE {schema}.T AS\nSELECT 1 AS x;\n"
        self.assertEqual(sql_statements(script, "dbo_demo"),
                         ["DROP TABLE IF EXISTS dbo_demo.T", "CREATE TABLE dbo_demo.T AS\nSELECT 1 AS x"])

    def test_schema_placeholder_is_optional(self):
        self.assertEqual(sql_statements("SELECT 1;"), ["SELECT 1"])


if __name__ == "__main__":
    unittest.main()
