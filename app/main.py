import asyncio
import logging
import sys
import container

from bot.bot import main

if __name__ == '__main__':
    container.services.is_running()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(main())

