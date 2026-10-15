"""The failed-authentication throttle, driven by a fake clock."""

import unittest

from _support import ratelimit


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def throttle(clock, **kw):
    kw.setdefault("max_failures", 5)
    kw.setdefault("lockout", 300.0)
    kw.setdefault("window", 300.0)
    return ratelimit.AuthThrottle(clock=clock, **kw)


ADDR = ("192.168.1.50", 51234)


class Lockout(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.t = throttle(self.clock)

    def test_fresh_address_may_try(self):
        self.assertFalse(self.t.is_locked(ADDR))
        self.assertEqual(self.t.retry_after(ADDR), 0.0)

    def test_below_the_limit_is_not_locked(self):
        for _ in range(4):
            self.assertFalse(self.t.record_failure(ADDR))
        self.assertFalse(self.t.is_locked(ADDR))

    def test_the_fifth_failure_locks_out(self):
        for _ in range(4):
            self.t.record_failure(ADDR)
        self.assertTrue(self.t.record_failure(ADDR))
        self.assertTrue(self.t.is_locked(ADDR))
        self.assertAlmostEqual(self.t.retry_after(ADDR), 300.0)

    def test_reconnecting_does_not_reset_the_count(self):
        # Every attempt arrives on a new TCP connection with a new source
        # port, so anything keyed on the port would count to one forever.
        for port in range(5):
            self.t.record_failure(("192.168.1.50", 40000 + port))
        self.assertTrue(self.t.is_locked(("192.168.1.50", 9999)))

    def test_other_addresses_are_unaffected(self):
        for _ in range(5):
            self.t.record_failure(ADDR)
        self.assertFalse(self.t.is_locked(("10.0.0.1", 1)))

    def test_lockout_expires_and_clears_the_history(self):
        for _ in range(5):
            self.t.record_failure(ADDR)
        self.clock.advance(301)
        self.assertFalse(self.t.is_locked(ADDR))
        # One more mistake must not lock the address out immediately.
        self.assertFalse(self.t.record_failure(ADDR))
        self.assertFalse(self.t.is_locked(ADDR))

    def test_failures_age_out_of_the_window(self):
        for _ in range(4):
            self.t.record_failure(ADDR)
        self.clock.advance(301)
        self.assertEqual(self.t.failure_count(ADDR), 0)
        self.assertFalse(self.t.record_failure(ADDR))

    def test_success_forgets_everything(self):
        for _ in range(4):
            self.t.record_failure(ADDR)
        self.t.record_success(ADDR)
        self.assertEqual(self.t.failure_count(ADDR), 0)
        self.assertFalse(self.t.record_failure(ADDR))

    def test_retry_after_counts_down(self):
        for _ in range(5):
            self.t.record_failure(ADDR)
        self.clock.advance(100)
        self.assertAlmostEqual(self.t.retry_after(ADDR), 200.0)


class Bookkeeping(unittest.TestCase):
    def test_key_handles_odd_peernames(self):
        self.assertEqual(ratelimit.AuthThrottle.key(("1.2.3.4", 5)), "1.2.3.4")
        self.assertEqual(ratelimit.AuthThrottle.key(("::1", 5, 0, 0)), "::1")
        self.assertEqual(ratelimit.AuthThrottle.key(None), "None")

    def test_table_does_not_grow_without_bound(self):
        clock = FakeClock()
        t = throttle(clock, max_tracked=50)
        for i in range(500):
            t.record_failure(("10.0.%d.%d" % (i // 256, i % 256), 1))
        self.assertLessEqual(len(t._failures), 51)

    def test_locked_addresses_survive_pruning(self):
        clock = FakeClock()
        t = throttle(clock, max_tracked=10)
        for _ in range(5):
            t.record_failure(ADDR)
        for i in range(100):
            t.record_failure(("10.0.%d.%d" % (i // 256, i % 256), 1))
        self.assertTrue(t.is_locked(ADDR))

    def test_max_failures_must_be_sane(self):
        with self.assertRaises(ValueError):
            ratelimit.AuthThrottle(max_failures=0)


if __name__ == "__main__":
    unittest.main()
