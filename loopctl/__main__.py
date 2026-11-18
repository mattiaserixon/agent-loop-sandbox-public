"""`python -m loopctl` runs the same CLI as loopctl/bin/loopctl."""

import sys

from loopctl.cli import main

sys.exit(main())
