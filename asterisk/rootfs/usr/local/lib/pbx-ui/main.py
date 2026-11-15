#!/usr/bin/env python3
"""Entry point for the add-on web UI.

  main.py             run the web UI server
  main.py --generate  write pjsip_ui.conf / extensions_ui.conf and exit
"""

import logging
import os
import sys

from pbxui.server import Settings, generate, serve


def main():
    level = os.environ.get("PBX_UI_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="[pbx-ui] %(levelname)s: %(message)s",
    )
    settings = Settings()
    if "--generate" in sys.argv[1:]:
        generate(settings)
    else:
        serve(settings)


if __name__ == "__main__":
    main()
