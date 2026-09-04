#!/usr/bin/env python3
import sys
from pathlib import Path


# Python isolated mode deliberately omits the script directory from sys.path.  Admit only the
# resolved checkout containing this launcher, and keep it ahead of the invocation cwd/PYTHONPATH
# so an unrelated workstack package cannot shadow the source checkout.
CHECKOUT_ROOT = Path(__file__).resolve(strict=True).parent
sys.path.insert(0, str(CHECKOUT_ROOT))
sys.dont_write_bytecode = True

from workstack.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
