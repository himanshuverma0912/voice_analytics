"""Enables ``python -m voice_analytics``.

This is the form container images and Airflow should invoke, rather than a
filesystem path: the module path is a stable contract, the file layout is not.
"""

import sys

from voice_analytics.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
