import base64
import uuid

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.views.debug import ExceptionReporter

from accounting.secrets import SealedSecret, SecretBox, SecretError, get_secret_box


def _key(byte: int, length: int = 32) -> str:
    return base64.b64encode(bytes([byte]) * length).decode("ascii")


KEY_1 = _key(1)
# Module-level so the literals do not show up in the source context lines of an error report.
SECRET_MARKER = "super-secret"
UNENCODABLE_SECRET = SECRET_MARKER + "-\ud800"
KEY_2 = _key(2)


def error_report(call, expected):
    """The HTML of a production error report (mail_admins, DEBUG off; frame locals included) for call().

    sensitive_variables() is honoured only when DEBUG is False, which is how tests run.

    Built from a plain try/except: assertRaises clears the traceback frames, which
    would make a "secret is absent" assertion pass vacuously.
    """
    try:
        call()
    except expected as exc:
        return ExceptionReporter(None, type(exc), exc, exc.__traceback__).get_traceback_html()
    raise AssertionError(f"{expected.__name__} was not raised")


class SecretBoxTests(SimpleTestCase):
    def setUp(self):
        self.connection_id = uuid.uuid4()
        self.box = SecretBox({1: KEY_1}, active_version=1)

    def test_roundtrip(self):
        sealed = self.box.seal("p@ssw0rd-Пароль", aad=self.connection_id)

        self.assertEqual(sealed.key_version, 1)
        self.assertEqual(self.box.open(sealed, aad=self.connection_id), "p@ssw0rd-Пароль")

    def test_seal_uses_random_nonce(self):
        first = self.box.seal("same", aad=self.connection_id)
        second = self.box.seal("same", aad=self.connection_id)

        self.assertNotEqual(first.ciphertext, second.ciphertext)

    def test_other_connection_aad_cannot_open(self):
        sealed = self.box.seal("secret", aad=self.connection_id)

        with self.assertRaises(SecretError):
            self.box.open(sealed, aad=uuid.uuid4())

    def test_tampered_ciphertext_rejected(self):
        sealed = self.box.seal("secret", aad=self.connection_id)
        tampered = bytearray(sealed.ciphertext)
        tampered[-1] ^= 0x01

        with self.assertRaises(SecretError):
            self.box.open(SealedSecret(bytes(tampered), sealed.key_version), aad=self.connection_id)

    def test_truncated_ciphertext_rejected(self):
        with self.assertRaises(SecretError):
            self.box.open(SealedSecret(b"short", 1), aad=self.connection_id)

    def test_unknown_key_version_rejected(self):
        sealed = self.box.seal("secret", aad=self.connection_id)

        with self.assertRaises(SecretError):
            self.box.open(SealedSecret(sealed.ciphertext, 99), aad=self.connection_id)

    def test_old_key_version_still_opens_after_rotation(self):
        sealed_v1 = SecretBox({1: KEY_1}, active_version=1).seal("secret", aad=self.connection_id)
        rotated = SecretBox({1: KEY_1, 2: KEY_2}, active_version=2)

        self.assertEqual(rotated.open(sealed_v1, aad=self.connection_id), "secret")
        self.assertEqual(rotated.seal("new", aad=self.connection_id).key_version, 2)

    def test_plaintext_not_in_ciphertext_and_repr(self):
        plaintext = "super-secret-token"
        sealed = self.box.seal(plaintext, aad=self.connection_id)

        self.assertNotIn(plaintext.encode(), sealed.ciphertext)
        self.assertNotIn(plaintext, repr(sealed))
        self.assertNotIn(plaintext, str(sealed))
        self.assertNotIn("b'", repr(sealed))
        self.assertNotIn(sealed.ciphertext.hex(), repr(sealed))
        self.assertIn("key_version=1", repr(sealed))

    def test_errors_do_not_leak_secrets(self):
        plaintext = "super-secret-token"
        sealed = self.box.seal(plaintext, aad=self.connection_id)

        with self.assertRaises(SecretError) as ctx:
            self.box.open(sealed, aad=uuid.uuid4())

        message = str(ctx.exception)
        for leaked in (plaintext, KEY_1, sealed.ciphertext.hex(), repr(sealed.ciphertext)):
            self.assertNotIn(leaked, message)

    def test_seal_rejects_unencodable_text_with_secret_error(self):
        with self.assertRaises(SecretError) as ctx:
            self.box.seal(UNENCODABLE_SECRET, aad=self.connection_id)

        self.assertNotIn(SECRET_MARKER, str(ctx.exception))
        self.assertTrue(ctx.exception.__suppress_context__)
        self.assertIsNone(ctx.exception.__cause__)

    def test_seal_frame_variables_are_hidden_in_error_reports(self):
        report = error_report(lambda: self.box.seal(UNENCODABLE_SECRET, aad=self.connection_id), SecretError)

        self.assertNotIn(SECRET_MARKER, report)
        self.assertIn("seal", report)


class GetSecretBoxTests(SimpleTestCase):
    def test_builds_box_from_settings(self):
        connection_id = uuid.uuid4()
        with override_settings(ACCOUNTING_SECRET_KEYS={1: KEY_1}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1):
            box = get_secret_box()
            sealed = box.seal("x", aad=connection_id)

        self.assertEqual(sealed.key_version, 1)
        with override_settings(ACCOUNTING_SECRET_KEYS={1: KEY_1, 2: KEY_2}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=2):
            self.assertEqual(get_secret_box().open(sealed, aad=connection_id), "x")

    def test_missing_keys_raise_improperly_configured(self):
        with override_settings(ACCOUNTING_SECRET_KEYS={}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1):
            with self.assertRaises(ImproperlyConfigured):
                get_secret_box()

    def test_active_version_missing_raises_improperly_configured(self):
        with override_settings(ACCOUNTING_SECRET_KEYS={1: KEY_1}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=2):
            with self.assertRaises(ImproperlyConfigured):
                get_secret_box()

    def test_bad_base64_raises_improperly_configured_without_key_material(self):
        bad = "not*valid*base64!!"
        with override_settings(ACCOUNTING_SECRET_KEYS={1: bad}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                get_secret_box()

        self.assertNotIn(bad, str(ctx.exception))

    def test_wrong_length_key_raises_improperly_configured_without_key_material(self):
        for length in (16, 31, 33, 64):
            short = _key(7, length)
            with self.subTest(length=length):
                with override_settings(ACCOUNTING_SECRET_KEYS={1: short}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1):
                    with self.assertRaises(ImproperlyConfigured) as ctx:
                        get_secret_box()
                self.assertNotIn(short, str(ctx.exception))

    def test_invalid_non_active_key_also_rejected(self):
        with override_settings(
            ACCOUNTING_SECRET_KEYS={1: _key(7, 16), 2: KEY_2}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=2
        ):
            with self.assertRaises(ImproperlyConfigured):
                get_secret_box()

    def test_key_material_is_hidden_in_error_reports(self):
        with override_settings(ACCOUNTING_SECRET_KEYS={1: _key(7, 16)}, ACCOUNTING_SECRET_ACTIVE_KEY_VERSION=1):
            report = error_report(get_secret_box, ImproperlyConfigured)

        self.assertNotIn(_key(7, 16), report)
        self.assertIn("_decode_key", report)
