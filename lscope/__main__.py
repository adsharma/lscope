"""Allow running lscope as ``python -m lscope``."""
import sys
from lscope.main import main

sys.exit(main())
