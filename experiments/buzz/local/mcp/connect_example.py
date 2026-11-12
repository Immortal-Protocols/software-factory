"""Read-only connection smoke test: python connect_example.py <agent-config.json>."""
import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    config = json.loads(Path(sys.argv[1]).read_text())["mcpServers"]["buzz"]
    async with httpx.AsyncClient(headers=config["headers"], trust_env=False,
                                 timeout=httpx.Timeout(30, read=60)) as http:
        async with streamable_http_client(config["url"], http_client=http) as (reader, writer, _):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                identity = await client.call_tool("buzz_identity", {})
                messages = await client.call_tool("buzz_read_messages", {"limit": 10})
                print(json.dumps({"identity": identity.model_dump(mode="json"),
                                  "messages": messages.model_dump(mode="json")}, indent=2))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
