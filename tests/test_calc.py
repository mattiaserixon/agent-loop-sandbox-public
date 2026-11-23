import unittest

from calc import add, sub


class TestAdd(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(5, 3), 8)
        self.assertEqual(add(2, -3), -1)
        self.assertEqual(add(0, 0), 0)


class TestSub(unittest.TestCase):
    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
        self.assertEqual(sub(2, 3), -1)


if __name__ == "__main__":
    unittest.main()
