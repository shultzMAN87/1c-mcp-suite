"""
Smoke 4.6.6 — проверяет, что 7 MCP-tools из PLAN.md (строка 476)
зарегистрированы на mcp-metadata-graph.
"""
import asyncio
import os
import sys

from mcp.client.sse import sse_client
from mcp import ClientSession


MCP_URL    = os.environ.get("MCP_URL", "http://mcp-metadata-graph:8001/sse")
MCP_SECRET = os.environ.get("MCP_SHARED_SECRET", "")

EXPECTED = [
    "code_callers",
    "code_callees",
    "code_call_path",
    "metadata_find_link_path",
    "metadata_attribute_type",
    "code_dead_procedures",
    "code_procedures_operating_on",
]


async def main() -> int:
    headers = {"Authorization": f"Bearer {MCP_SECRET}"} if MCP_SECRET else {}
    async with sse_client(MCP_URL, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)

            print(f"Tools available on mcp-metadata-graph: {len(names)}")
            print()
            print("Required by 4.6.6:")
            missing = []
            for e in EXPECTED:
                mark = "+" if e in names else "-"
                print(f"  {mark} {e}")
                if e not in names:
                    missing.append(e)

            print()
            if missing:
                print(f"✗ FAIL: missing {len(missing)} tool(s): {missing}")
                print()
                print("All registered tools:")
                for n in names:
                    print(f"  - {n}")
                return 1

            print(f"✓ PASS: all {len(EXPECTED)} tools registered")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))