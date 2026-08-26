from __future__ import annotations

import sys

from wecom_feedback.main import main


if __name__ == "__main__":
    main(["desktop", *sys.argv[1:]])
