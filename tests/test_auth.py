"""Password handling: the properties that make a captured handshake useless."""

import unittest

from _support import auth


class KeyDerivation(unittest.TestCase):
    def test_deterministic(self):
        salt = b"\x01" * 16
        self.assertEqual(auth.derive_key("hunter2", salt, 1000),
                         auth.derive_key("hunter2", salt, 1000))

    def test_salt_changes_the_key(self):
        # The original hashed the password with no salt at all, so one rainbow
        # table covered every LANDesk server in the world.
        a = auth.derive_key("hunter2", b"\x01" * 16, 1000)
        b = auth.derive_key("hunter2", b"\x02" * 16, 1000)
        self.assertNotEqual(a, b)

    def test_password_changes_the_key(self):
        salt = auth.new_salt()
        self.assertNotEqual(auth.derive_key("hunter2", salt, 1000),
                            auth.derive_key("hunter3", salt, 1000))

    def test_key_is_32_bytes(self):
        self.assertEqual(len(auth.derive_key("x", auth.new_salt(), 1000)), 32)

    def test_iterations_must_be_positive(self):
        with self.assertRaises(ValueError):
            auth.derive_key("x", auth.new_salt(), 0)

    def test_default_cost_is_not_token(self):
        # A single round of SHA-256 - what this replaced - is a wordlist away
        # from any human-chosen password.
        self.assertGreaterEqual(auth.PBKDF2_ITERATIONS, 100_000)

    def test_salt_and_nonce_are_random_and_long_enough(self):
        self.assertNotEqual(auth.new_salt(), auth.new_salt())
        self.assertNotEqual(auth.new_nonce(), auth.new_nonce())
        self.assertGreaterEqual(len(auth.new_salt()), 16)
        self.assertGreaterEqual(len(auth.new_nonce()), 32)


class ChallengeResponse(unittest.TestCase):
    def setUp(self):
        self.key = auth.derive_key("hunter2", b"salt" * 4, 1000)
        self.nonce = auth.new_nonce()
        self.channel = b"\xaa" * 32

    def test_correct_response_verifies(self):
        response = auth.client_response(self.key, self.nonce, self.channel)
        self.assertTrue(auth.verify(
            auth.client_response(self.key, self.nonce, self.channel), response))

    def test_response_does_not_carry_to_another_nonce(self):
        # This is the replay fix. The original sent SHA-256(password) every
        # time, so one captured handshake authenticated forever.
        response = auth.client_response(self.key, self.nonce, self.channel)
        other = auth.client_response(self.key, auth.new_nonce(), self.channel)
        self.assertFalse(auth.verify(other, response))

    def test_response_does_not_carry_to_another_channel(self):
        # This is what makes an unverified self-signed certificate safe: the
        # proof covers the certificate the client actually saw, so a machine
        # in the middle cannot pass it on to the real server.
        response = auth.client_response(self.key, self.nonce, self.channel)
        relayed = auth.client_response(self.key, self.nonce, b"\xbb" * 32)
        self.assertFalse(auth.verify(relayed, response))

    def test_wrong_password_does_not_verify(self):
        wrong = auth.derive_key("hunter3", b"salt" * 4, 1000)
        self.assertFalse(auth.verify(
            auth.client_response(self.key, self.nonce, self.channel),
            auth.client_response(wrong, self.nonce, self.channel)))

    def test_client_and_server_proofs_differ(self):
        # Otherwise the client's own response could be replayed back at it as
        # the server's proof, and any relay would look like the real server.
        self.assertNotEqual(auth.client_response(self.key, self.nonce, self.channel),
                            auth.server_confirmation(self.key, self.nonce, self.channel))

    def test_no_channel_still_works(self):
        response = auth.client_response(self.key, self.nonce)
        self.assertTrue(auth.verify(auth.client_response(self.key, self.nonce), response))

    def test_verify_tolerates_rubbish(self):
        expected = auth.client_response(self.key, self.nonce, self.channel)
        for provided in (None, 7, b"bytes", {}, [], ""):
            self.assertFalse(auth.verify(expected, provided))


if __name__ == "__main__":
    unittest.main()
