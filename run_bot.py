"""Command-line entry point for the NapCat QQ bot."""

import asyncio

from zhaoxinqbot.app import main


if __name__ == "__main__":
    asyncio.run(main())
