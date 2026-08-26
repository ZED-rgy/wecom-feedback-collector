import importlib.util
import unittest

from wecom_feedback.adapters.windows_local_db import (
    PAGE_SIZE,
    SQLITE_HEADER,
    _decrypt_page,
    _derive_page_key,
    _generate_initial_vector,
    _quick_verify_key,
    _verify_key,
    decode_message_text,
)


class WindowsLocalDbTests(unittest.TestCase):
    def test_decode_plain_utf8_message(self):
        self.assertEqual(decode_message_text("@反馈助手 登录失败".encode()), "@反馈助手 登录失败")

    def test_known_page_one_iv(self):
        self.assertEqual(_generate_initial_vector(1).hex(), "20d7420f9c37a35dca6fe92a1c6999a9")

    @unittest.skipUnless(importlib.util.find_spec("Crypto"), "pycryptodome is optional")
    def test_synthetic_encrypted_page_round_trip(self):
        from Crypto.Cipher import AES

        raw_key = bytes.fromhex("00112233445566778899aabbccddeeff")
        plain = bytearray(PAGE_SIZE)
        plain[:16] = SQLITE_HEADER
        plain[16:24] = bytes.fromhex("1000020200402020")
        plain[100] = 0x0D
        encrypted = bytearray(plain)
        fragment = bytes(encrypted[16:24])
        page_key = _derive_page_key(raw_key, 1)
        iv = _generate_initial_vector(1)
        encrypted[:16] = AES.new(page_key, AES.MODE_CBC, iv).encrypt(bytes(encrypted[:16]))
        encrypted[16:] = AES.new(page_key, AES.MODE_CBC, iv).encrypt(bytes(encrypted[16:]))
        encrypted[8:16] = encrypted[16:24]
        encrypted[16:24] = fragment

        self.assertTrue(_quick_verify_key(raw_key, bytes(encrypted)))
        self.assertTrue(_verify_key(raw_key, bytes(encrypted)))
        self.assertEqual(_decrypt_page(raw_key, bytes(encrypted), 1), bytes(plain))


if __name__ == "__main__":
    unittest.main()
