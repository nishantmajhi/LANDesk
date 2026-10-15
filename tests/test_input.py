"""Input translation: coordinates, key names and scroll amounts off the wire.

pynput is not installed in the test environment, so the injection itself is a
no-op here. What is under test is the decision each method makes before it
gets that far, which is where a hostile or buggy viewer would land.
"""

import unittest

from _support import server


class Coordinates(unittest.TestCase):
    def setUp(self):
        self.h = server.InputHandler(1920, 1080)

    def test_centre(self):
        self.assertEqual(self.h.to_pixels(0.5, 0.5), (960, 540))

    def test_origin(self):
        self.assertEqual(self.h.to_pixels(0.0, 0.0), (0, 0))

    def test_bottom_right_stays_on_screen(self):
        # 1.0 * 1920 is 1920, which is one past the last column.
        self.assertEqual(self.h.to_pixels(1.0, 1.0), (1919, 1079))

    def test_out_of_range_is_clamped(self):
        self.assertEqual(self.h.to_pixels(-5.0, 12.0), (0, 1079))

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(self.h.to_pixels("0.5", "0.5"), (960, 540))

    def test_rubbish_is_refused(self):
        for nx, ny in (("left", 0.5), (None, 0.5), ({}, 0.5), (0.5, [1])):
            self.assertIsNone(self.h.to_pixels(nx, ny))

    def test_nan_is_refused(self):
        self.assertIsNone(self.h.to_pixels(float("nan"), 0.5))

    def test_infinity_is_clamped_not_crashing(self):
        self.assertEqual(self.h.to_pixels(float("inf"), float("-inf")), (1919, 0))

    def test_on_mouse_move_does_not_raise_on_rubbish(self):
        self.h.on_mouse_move("left", None)


class Keys(unittest.TestCase):
    def setUp(self):
        self.h = server.InputHandler(800, 600)

    def test_named_key(self):
        self.assertIsNotNone(self.h.resolve_key("enter"))

    def test_names_are_case_insensitive(self):
        self.assertEqual(self.h.resolve_key("Enter"), self.h.resolve_key("enter"))

    def test_single_character_is_literal(self):
        self.assertEqual(self.h.resolve_key("a"), "a")

    def test_unknown_multi_character_name_is_refused(self):
        # The original passed these straight to pynput, which raises on any
        # string longer than one character - a viewer typo ended the session.
        self.assertIsNone(self.h.resolve_key("ctrl+alt+del"))
        self.assertIsNone(self.h.resolve_key("banana"))

    def test_non_strings_are_refused(self):
        for name in (None, 7, [], {}, b"a"):
            self.assertIsNone(self.h.resolve_key(name))

    def test_empty_is_refused(self):
        self.assertIsNone(self.h.resolve_key(""))

    def test_on_key_does_not_raise_on_rubbish(self):
        self.h.on_key("banana", True)
        self.h.on_key(None, False)


class Scroll(unittest.TestCase):
    def setUp(self):
        self.h = server.InputHandler(800, 600)

    def test_normal_amounts_pass_through(self):
        self.assertEqual(self.h.clamp_scroll(3), 3)
        self.assertEqual(self.h.clamp_scroll(-3), -3)

    def test_absurd_amounts_are_clamped(self):
        self.assertEqual(self.h.clamp_scroll(10 ** 9), self.h.MAX_SCROLL)
        self.assertEqual(self.h.clamp_scroll(-10 ** 9), -self.h.MAX_SCROLL)

    def test_floats_are_truncated(self):
        self.assertEqual(self.h.clamp_scroll(2.9), 2)

    def test_rubbish_becomes_no_scroll(self):
        for value in ("lots", None, [], {}, float("nan")):
            self.assertEqual(self.h.clamp_scroll(value), 0)

    def test_on_scroll_does_not_raise_on_rubbish(self):
        self.h.on_scroll("lots", None)


class Buttons(unittest.TestCase):
    def test_unknown_button_is_ignored(self):
        # The original mapped anything unrecognised to the left button, so a
        # viewer sending nonsense still clicked on things.
        h = server.InputHandler(800, 600)
        h.on_mouse_button("nonsense", True)
        h.on_mouse_button(None, True)


if __name__ == "__main__":
    unittest.main()
