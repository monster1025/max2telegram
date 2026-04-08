import asyncio
import os
import sys

from dotenv import load_dotenv
from pymax import MaxClient


def main() -> None:
    load_dotenv()

    phone = os.getenv("MAX_PHONE", "").strip()
    work_dir = os.getenv("MAX_WORK_DIR", "cache").strip() or "cache"

    if not phone:
        print("Error: MAX_PHONE environment variable is required", file=sys.stderr)
        sys.exit(1)

    client = MaxClient(phone=phone, work_dir=work_dir)
    print(f"Starting Max authentication for {phone}")
    print(f"Session will be stored in: {work_dir}")

    async def _run() -> None:
        await client.start()
        print(f"Authentication successful!")
        print(f"Me: {client.me.username or client.me.first_name} (id={client.me.id})")
        print("Session file saved. You can now run the main application.")
        await client.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
