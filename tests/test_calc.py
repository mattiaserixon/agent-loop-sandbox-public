import unittest

from calc import sub


class TestSub(unittest.TestCase):
    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
        self.assertEqual(sub(2, 3), -1)


if __name__ == "__main__":
    unittest.main()
