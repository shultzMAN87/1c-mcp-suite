"""
Триггер реиндексации Code RAG.

Запускается контейнером code-indexer при старте docker-compose.
Ждёт пока mcp-code-rag поднимется и через MCP-клиент дёргает у него инструмент code_reindex.
Использует штатный MCP SSE-клиент, а не самописный POST.
"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client

SSE_URL = os.environ.get("CODE_RAG_SSE_URL", "http://mcp-code-rag:8011/sse")
TOOL_NAME = "code_reindex"
WAIT_BEFORE_START = int(os.environ.get("REINDEX_INITIAL_DELAY", "15"))


async def run() -> int:
    print(f"[code-indexer] жду {WAIT_BEFORE_START}с, чтобы mcp-code-rag поднялся...", flush=True)
    await asyncio.sleep(WAIT_BEFORE_START)

    print(f"[code-indexer] подключаюсь к {SSE_URL}", flush=True)
    try:
        # Задача 3.2: добавляем Bearer-заголовок, если задан общий секрет.
        try:
            from mcp_auth import build_client_headers
            client_headers = build_client_headers()
        except Exception:
            client_headers = {}
        async with sse_client(SSE_URL, headers=client_headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                print(f"[code-indexer] вызов tool '{TOOL_NAME}'...", flush=True)
                result = await session.call_tool(TOOL_NAME, arguments={})

                for block in result.content:
                    text = getattr(block, "text", None)
                    if text is not None:
                        print(text, flush=True)
                    else:
                        print(repr(block), flush=True)

                if getattr(result, "isError", False):
                    print("[code-indexer] tool вернул isError=True", file=sys.stderr, flush=True)
                    return 1
    except Exception as e:
        print(f"[code-indexer] ошибка индексации: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return 1

    print("[code-indexer] индексация завершена успешно", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
