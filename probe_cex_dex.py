"""One-shot CEX<->DEX net-edge probe: Coinbase spot vs Uniswap v3 on Base.

Prints the full cost decomposition for both directions, every fee tier and
several sizes, at two Coinbase fee assumptions. Reads nothing, writes nothing,
trades nothing -- it is a measurement, not a strategy.

Read the GROSS column first. That is the only number describing the market;
everything else is what you pay to touch it. A fat gross edge at the 0.30%
tier is not an opportunity, it is a pool whose mid has drifted because nobody
trades it.

Two caveats the output cannot express:

  SKEW. The Coinbase book and the on-chain quote are not simultaneous. The
  printed skew is how far apart they were. When the gross edge is of the same
  order as what ETH can move in that window, the "edge" is partly a
  measurement artifact -- the first phantom on the list this project was
  built around. Treat a single reading as a sample, never as a signal.

  ETH vs WETH. Treated as 1:1, which is true by the WETH contract, but
  wrapping costs gas that is NOT counted here. Neither is any transfer or
  bridge between venues: this assumes inventory already sitting on both
  sides, which is what a real spatial arb requires.

Usage: PYTHONPATH=. python probe_cex_dex.py [product] [--sizes 1000,10000]
"""
from __future__ import annotations

import asyncio
import sys
import time
from decimal import Decimal as D

from legs import Book, BookLeg
from netedge import evaluate
from venues.coinbase import CoinbaseMarketData
from venues.evm import (
    CANDIDATES,
    GAS_ONE_SWAP,
    TokenRegistry,
    client_for,
    measure_gas_usd,
    v3_deployment,
    v3_leg,
)

FEE_TIERS = (100, 500, 3000)
DEFAULT_SIZES = (D("1000"), D("10000"), D("50000"))

# Both are evaluated because the gap between them decides the whole strategy.
# 10bps is the pinned project assumption; ~60bps is the entry Advanced Trade
# taker tier, which is what you actually pay until volume says otherwise.
CB_FEE_CASES = ((D("10"), "10bps (pinned assumption)"),
                (D("60"), "60bps (entry Advanced Trade)"))


async def probe(product: str, sizes: tuple[D, ...]) -> int:
    base, quote = product.split("-")

    c = client_for("base")
    c.verify_chain_id()
    reg = TokenRegistry(c)
    reg.validate(CANDIDATES["base"])
    tok_in, tok_out = reg["WETH"], reg["USDC"]
    quoter = v3_deployment("base").quoter

    cb = CoinbaseMarketData(book_depth=50)
    products = await cb.products()
    if product not in products:
        print(f"{product} is not an online Coinbase spot product")
        await cb.aclose()
        c.close()
        return 2

    # Fetch both sides as close together as possible, then report the gap.
    raw = await cb.book(product, base, quote)
    await cb.aclose()
    if raw is None:
        print(f"no book for {product}")
        c.close()
        return 2
    ref = v3_leg(c, quoter, tok_in, tok_out, 500, D("1"))
    skew = abs(ref.ts_local - raw.ts_local) * 1000.0

    # Relabel so the path closes: the on-chain token is WETH, not ETH.
    book = Book(product, "WETH", quote, raw.bids, raw.asks,
                raw.ts_local, raw.ts_exchange)
    eth_usd = ref.marginal_out_per_in
    gas = measure_gas_usd(c, GAS_ONE_SWAP, eth_usd)

    print(f"block {c.block_number():,}   venue skew {skew:.0f}ms")
    print(f"{product:12s} bid {book.best_bid:,.2f}  ask {book.best_ask:,.2f}  "
          f"({len(book.bids)}x{len(book.asks)} levels)")
    print(f"base v3 500  mid {eth_usd:,.2f}   gas/swap ${gas:.4f}")
    print(f"top-of-book gap (cex bid vs dex mid): "
          f"{(D(book.best_bid) / eth_usd - 1) * 10000:+.1f}bps")
    if skew > 250:
        print(f"  WARNING skew {skew:.0f}ms is large; the gross edge below may "
              "be a timestamp artifact rather than a dislocation")
    print()

    best = None
    for fee_bps, label in CB_FEE_CASES:
        fee = fee_bps / 10000
        print(f"-- coinbase taker {label} --")
        for tier in FEE_TIERS:
            for size in sizes:
                legs = {
                    "dex->cex": [
                        v3_leg(c, quoter, tok_out, tok_in, tier, size),
                        BookLeg(book=book, asset_in="WETH", fee_rate=fee,
                                venue="coinbase"),
                    ],
                    "cex->dex": [
                        BookLeg(book=book, asset_in="USDC", fee_rate=fee,
                                venue="coinbase"),
                        v3_leg(c, quoter, tok_in, tok_out, tier, size / eth_usd),
                    ],
                }
                for name, path in legs.items():
                    r = evaluate(path, size, fixed_cost_usd=gas,
                                 start_asset_usd_price=D(1))
                    if not r.ok:
                        print(f"   t{tier:<5}{name} ${size:>8,.0f}  "
                              f"REJECTED {r.reason}")
                        continue
                    if best is None or r.net_edge_bps > best[0]:
                        best = (r.net_edge_bps, f"t{tier} {name} ${size:,.0f} @{label}")
                    print(f"   t{tier:<5}{name} ${size:>8,.0f}  "
                          f"net {r.net_edge_bps:+9.2f}bps  "
                          f"gross {r.gross_edge_bps:+8.2f} fees {r.fee_bps:+7.2f} "
                          f"slip {r.slippage_bps:+8.2f} gas {r.fixed_cost_bps:+6.2f}"
                          f"{'  [EXH]' if r.exhausted else ''}"
                          f"{'  <== POSITIVE' if r.net_edge_bps > 0 else ''}")
        print()

    if best:
        edge, where = best
        verdict = "OPEN" if edge > 0 else "CLOSED"
        print(f"best net edge {edge:+.2f}bps ({where})  -> {verdict}")
        if edge <= 0:
            print("  One reading, not a verdict on the strategy. What matters is "
                  "the DISTRIBUTION of the gross column over time and how long "
                  "each dislocation survives -- that is what shadow logging is "
                  "for, and what this one-shot cannot tell you.")
    c.close()
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    product = args[0] if args else "ETH-USDC"
    sizes = DEFAULT_SIZES
    for a in sys.argv[1:]:
        if a.startswith("--sizes="):
            sizes = tuple(D(x) for x in a.split("=", 1)[1].split(","))
    return asyncio.run(probe(product, sizes))


if __name__ == "__main__":
    raise SystemExit(main())
