"""Real traffic for the bot: golden-set customers asking questions and reacting to the answers.

    uv run python -m target.traffic                # forever: 5-10 questions every 60-120 s
    uv run python -m target.traffic --burst 50     # 50 questions now, then exit
    uv run python -m target.traffic --once         # one cycle, then exit

Each simulated customer reacts to the reply it got: thumbs down when the answer fails its
golden checks, and "talk to a human" when it needed escalation but did not get it.
"""

import argparse
import asyncio
import random
import time
import uuid

import httpx

from target import config

CONCURRENCY = 10


async def ask(client: httpx.AsyncClient, item, sem: asyncio.Semaphore) -> dict | None:
    async with sem:
        session_id = f"traffic-{uuid.uuid4().hex[:12]}"
        try:
            resp = await client.post("/reply", json={
                "question": item.question, "golden_id": item.id,
                "source": "traffic", "session_id": session_id,
            })
        except httpx.HTTPError as exc:
            print(f"  {item.id}: transport error {exc!r}")
            return None
        if resp.status_code != 200:
            print(f"  {item.id}: HTTP {resp.status_code}")
            return None
        body = resp.json()
        meta, scores = body["meta"], body["meta"]["scores"]
        reactions = []
        if scores["eval_score"] < 0.75:
            reactions.append("thumbs_down")
        if item.expect_escalate and not scores["escalated"]:
            reactions.append("talk_to_human")
        for kind in reactions:
            await client.post("/feedback", json={
                "trace_id": meta["trace_id"], "kind": kind,
                "session_id": session_id, "source": "traffic",
            })
        return {"id": item.id, "score": scores["eval_score"], "latency_ms": meta["latency_ms"],
                "format_valid": scores["format_valid"], "v": meta["prompt_version"], "model": meta["model"]}


async def run_batch(n: int) -> None:
    items = list(config.golden_items().values())
    batch = [random.choice(items) for _ in range(n)]
    sem = asyncio.Semaphore(CONCURRENCY)
    started = time.perf_counter()
    async with httpx.AsyncClient(base_url=config.env("BOT_URL", "http://localhost:8000"), timeout=90) as client:
        results = [r for r in await asyncio.gather(*(ask(client, i, sem) for i in batch)) if r]
    if not results:
        print(f"batch of {n}: all failed")
        return
    mean = sum(r["score"] for r in results) / len(results)
    valid = sum(r["format_valid"] for r in results) / len(results)
    p50 = sorted(r["latency_ms"] for r in results)[len(results) // 2]
    versions = sorted({f"v{r['v']}/{r['model']}" for r in results})
    print(f"{time.strftime('%H:%M:%S')} batch {len(results)}/{n} ok  eval={mean:.2f}  "
          f"format_valid={valid:.0%}  p50={p50}ms  {', '.join(versions)}  ({time.perf_counter() - started:.1f}s)")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--burst", type=int, help="send N requests now and exit")
    parser.add_argument("--once", action="store_true", help="run one normal cycle and exit")
    parser.add_argument("--min-interval", type=float, default=60)
    parser.add_argument("--max-interval", type=float, default=120)
    args = parser.parse_args()
    if args.burst:
        await run_batch(args.burst)
        return
    while True:
        await run_batch(random.randint(5, 10))
        if args.once:
            return
        await asyncio.sleep(random.uniform(args.min_interval, args.max_interval))


if __name__ == "__main__":
    asyncio.run(main())
