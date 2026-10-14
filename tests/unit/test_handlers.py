import unittest

from sut.handlers import HandlerError, echo, sum_numbers, uppercase, word_count


class TestHandlers(unittest.TestCase):
    def test_echo(self):
        self.assertEqual(echo({"a": 1}), {"a": 1})

    def test_uppercase(self):
        self.assertEqual(uppercase({"text": "hej"}), {"text": "HEJ"})
        with self.assertRaises(HandlerError):
            uppercase({"text": 5})

    def test_sum(self):
        self.assertEqual(sum_numbers({"numbers": [1, 2, 3]}), {"sum": 6, "count": 3, "mean": 2})

    def test_sum_rejects_bad_input(self):
        for payload in [{}, {"numbers": []}, {"numbers": [1, "2"]}, {"numbers": [True]}, None]:
            with self.subTest(payload=payload):
                with self.assertRaises(HandlerError):
                    sum_numbers(payload)

    def test_word_count(self):
        out = word_count({"text": "The cat, the hat. A cat!"})
        self.assertEqual(out["words"], 6)
        self.assertEqual(out["unique"], 4)
        self.assertEqual(out["top"][0], ("cat", 2))


if __name__ == "__main__":
    unittest.main()
