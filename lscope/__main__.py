"""Allow running lscope as ``python -m lscope``."""
import sys
import traceback
from lscope.main import main

try:
    sys.exit(main())
except Exception:
    traceback.print_exc()
    sys.exit(1)
