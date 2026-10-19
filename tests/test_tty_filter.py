"""Tests for the terminal filter's sequence splitting."""
import unittest

from tests.scripts import tty_filter

ESC = b"\x1b"


class SplitSequencesTest(unittest.TestCase):
    def assertSplit(self, data, whole, rest):
        self.assertEqual(tty_filter.split_sequences(data), (whole, rest))

    def test_plain_text_is_whole(self):
        self.assertSplit(b"abc", b"abc", b"")

    def test_a_partial_escape_sequence_is_held(self):
        self.assertSplit(ESC, b"", ESC)
        self.assertSplit(ESC + b"[", b"", ESC + b"[")
        self.assertSplit(ESC + b"[?1003", b"", ESC + b"[?1003")

    def test_a_complete_escape_sequence_is_whole(self):
        self.assertSplit(ESC + b"[?1003h", ESC + b"[?1003h", b"")
        self.assertSplit(b"ab" + ESC + b"[A" + b"c", b"ab" + ESC + b"[A" + b"c", b"")

    def test_whole_sequences_before_a_partial_one_are_returned(self):
        self.assertSplit(b"ab" + ESC + b"[?10", b"ab", ESC + b"[?10")

    def test_an_ss3_sequence_needs_its_final_byte(self):
        self.assertSplit(ESC + b"O", b"", ESC + b"O")
        self.assertSplit(ESC + b"OP", ESC + b"OP", b"")

    def test_a_string_sequence_runs_to_bel_or_st(self):
        self.assertSplit(ESC + b"]0;title", b"", ESC + b"]0;title")
        bel = ESC + b"]0;title\x07"
        self.assertSplit(bel, bel, b"")
        st = ESC + b"]0;title" + ESC + b"\\"
        self.assertSplit(st, st, b"")

    def test_a_multibyte_character_is_whole(self):
        # U+00E9, U+20AC and U+1F600.
        for encoded in ("é", "€", "😀"):
            raw = encoded.encode()
            self.assertSplit(raw, raw, b"")

    def test_a_partial_multibyte_character_is_held(self):
        raw = "€".encode()
        self.assertSplit(raw[:1], b"", raw[:1])
        self.assertSplit(raw[:2], b"", raw[:2])

    def test_an_invalid_lead_ends_after_one_byte(self):
        # Neither byte may continue the character that started, so each is whole.
        self.assertSplit(b"\xe2a", b"\xe2a", b"")
        self.assertSplit(b"\x80a", b"\x80a", b"")

    def test_an_invalid_lead_at_the_end_is_held_only_while_it_is_last(self):
        self.assertSplit(b"\xe2", b"", b"\xe2")


class CharacterLengthTest(unittest.TestCase):
    def test_a_two_byte_character_is_two(self):
        self.assertEqual(tty_filter.character_length("é".encode(), 0), 2)

    def test_a_three_byte_character_is_three(self):
        self.assertEqual(tty_filter.character_length("€".encode(), 0), 3)

    def test_a_four_byte_character_is_four(self):
        self.assertEqual(tty_filter.character_length("😀".encode(), 0), 4)

    def test_a_partial_character_is_none(self):
        self.assertIsNone(tty_filter.character_length("😀".encode()[:3], 0))

    def test_a_lead_that_cannot_be_continued_ends_after_one_byte(self):
        self.assertEqual(tty_filter.character_length(b"\xf0\xf0\xf0\xf0", 0), 1)
        self.assertEqual(tty_filter.character_length(b"\x80a", 0), 1)


class PartialMotionSequenceTest(unittest.TestCase):
    def test_a_prefix_of_the_rewritten_sequence_is_held(self):
        on = tty_filter.ALL_MOTION_ON
        for size in range(1, len(on)):
            self.assertEqual(tty_filter.partial_motion_sequence(b"x" + on[:size]), on[:size])

    def test_a_whole_sequence_is_not_held(self):
        self.assertEqual(tty_filter.partial_motion_sequence(tty_filter.ALL_MOTION_ON), b"")

    def test_unrelated_output_is_not_held(self):
        self.assertEqual(tty_filter.partial_motion_sequence(b"abc"), b"")


if __name__ == "__main__":
    unittest.main()
