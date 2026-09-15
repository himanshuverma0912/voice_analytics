"""Package entry point: ``python -m voice_analytics <command>``."""

import sys

from voice_analytics.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
