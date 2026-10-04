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

from load_synapse import fabric_type, quote_identifier


class FabricTypeTests(unittest.TestCase):
    def test_unicode_character_lengths_are_converted_to_utf8_capacity(self):
        self.assertEqual(fabric_type("nvarchar", 40, None, None), "VARCHAR(160)")
        self.assertEqual(fabric_type("nvarchar", -1, None, None), "VARCHAR(8000)")

    def test_numeric_and_temporal_types_are_preserved(self):
        self.assertEqual(fabric_type("decimal", None, 18, 4), "DECIMAL(18,4)")
        self.assertEqual(fabric_type("datetime", None, None, None), "DATETIME2(3)")
        self.assertEqual(fabric_type("date", None, None, None), "DATE")

    def test_identifiers_are_bracketed_and_validated(self):
        self.assertEqual(quote_identifier("f_planning_book"), "[f_planning_book]")
        with self.assertRaises(ValueError):
            quote_identifier("dbo; DROP TABLE users")

    def test_unknown_source_types_fail_instead_of_silently_coercing(self):
        with self.assertRaises(ValueError):
            fabric_type("sql_variant", None, None, None)


if __name__ == "__main__":
    unittest.main()
