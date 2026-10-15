"""Run the LANDesk test suite.

    python tests/run.py            everything
    python tests/run.py auth       only tests/test_auth.py

Standard library only, so it runs wherever the project does. The TLS tests
need the `cryptography` package and skip themselves without it.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import _support  # noqa: E402  (sets up the import paths for everything else)


def main():
    pattern = "test_%s.py" % sys.argv[1] if len(sys.argv) > 1 else "test_*.py"
    suite = unittest.defaultTestLoader.discover(HERE, pattern=pattern)
    if not _support.TLS_AVAILABLE:
        print("note: cryptography is not installed, so the TLS tests will skip.\n"
              "      pip install cryptography\n")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
