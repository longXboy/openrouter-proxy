#!/usr/bin/env python3
import httpx
import json

async def test_openrouter_stream():
    api_key = "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"

    async with httpx.AsyncClient() as client:
        req = client.build_request(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": "x-ai/grok-4-fast:free",
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": "how are you?"
                    }
                ]
            }
        )

        resp = await client.send(req, stream=True)
        print(f"Status: {resp.status_code}\n")

        chunk_num = 0
        async for line in resp.aiter_lines():
            chunk_num += 1
            print(f"[Chunk {chunk_num}] {line}")
            if line.startswith("data: {"):
                try:
                    data = json.loads(line[6:])
                    choices = data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        finish_reason = choices[0].get("finish_reason")
                        print(f"  -> content: {delta.get('content', 'N/A')}")
                        print(f"  -> finish_reason: {finish_reason}")
                except:
                    pass
            print()

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_openrouter_stream())
