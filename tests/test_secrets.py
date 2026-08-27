import unittest

from wecom_feedback.secrets import protect_secret, unprotect_secret


class SecretStorageTests(unittest.TestCase):
    def test_empty_secret_is_empty(self):
        self.assertIsNone(protect_secret(""))
        self.assertEqual(unprotect_secret(""), "")

    def test_round_trip_or_plaintext_fallback(self):
        value = "table-secret-测试"
        protected = protect_secret(value)
        if protected is None:
            # Non-Windows development environments have no DPAPI.
            self.assertEqual(unprotect_secret(value), value)
        else:
            self.assertTrue(protected.startswith("dpapi:"))
            self.assertEqual(unprotect_secret(protected), value)


if __name__ == "__main__":
    unittest.main()
