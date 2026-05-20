import asyncio
import logging
import sys
from pathlib import Path

# Make the `app/` directory importable regardless of how this module is launched
# (`python app/main.py`, `python -m app.main`, systemd service, Docker, ...).
# Without this, `python -m app.main` fails on `import container` because the cwd
# isn't necessarily on sys.path.
_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import container  # noqa: E402

from bot.bot import main  # noqa: E402

if __name__ == '__main__':
    container.services.is_running()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())

