# Questions on the Sui handover pack

Thanks for the pack — it was unusually good to work from. The README's
"lessons from going live" section did more for this project than the code did,
and I'll say why below.

**Context, so the questions land right.** I'm building the opposite half of
what you built: a read-only shadow logger that measures whether a cross-venue
dislocation exists and how long it survives, before any capital is risked. It
never trades. So I used exactly one endpoint from your pack — the FlowX quote
GET — and deliberately none of the execution path: no GraphQL, no object
resolver, no BCS encoder, no signer, no submitter. Several questions below are
about *measurement* rather than about your system, and I've tried to mark
which is which.

You were also right to correct me on `eth_call`. I'd claimed Sui had no
equivalent; `simulateTransaction` / `dryRunTransactionBlock` /
`devInspectTransactionBlock` plainly do, and the object model is the actual
reason a swap needs its own leg type. That's fixed in my notes.

## What the pack settled immediately

- The coin types, decimals (SUI 9, USDC 6) and the Q64.64 sqrt convention.
  I'd have reached for Q96 out of Uniswap habit and been wrong by 2^32.
- `includeSources=CETUS`, and *why* — see Q2.
- That public fullnode JSON-RPC is deprecated. Worth noting: a provider
  endpoint still serves it. `sui_getObject`, `suix_getCoinMetadata` and
  `sui_getLatestCheckpointSequenceNumber` all work fine for me through
  Alchemy, so your GraphQL-only stance may be stricter than strictly forced.
  Was that a deliberate precaution, or did you hit provider JSON-RPC failing
  too?
- Your gas lesson saved me from the error I was about to repeat in the other
  direction. I had an unmeasured $0.02-per-swap placeholder sitting in my EVM
  config, and your note that an inflated gas figure *hides* real opportunities
  is the inverse of the mistake I was guarding against.

## Questions, most useful first

### Q1. How do you get a frictionless (pre-cost) price for a DEX leg?

This is my biggest problem and I suspect you've solved it somewhere I can't
see.

I decompose every route into gross edge / fees / slippage / gas, so I need a
*frictionless* baseline per leg — the price at infinitesimal size, before fee
and impact. On an order book that's the touch. On Uniswap v3 I take
`QuoterV2` at a tiny notional and divide the pool fee back out; I cross-checked
that against the pool's own `slot0` and they agree to 0.013bps, so I trust it.

With FlowX I tried the same trick and it is **unsound**, which my own control
test caught:

```
same-venue round trip, SUI/USDC through FlowX:  gross +12.43 bps
```

A round trip through one venue cannot be positive before costs. The cause is
that a tiny quote and a real quote are *different routes* — see Q2 — so the
two directions' marginal prices aren't reciprocal and their product implies
free money.

My conclusion is that an aggregator cannot report its own frictionless price,
and only a direct pool read can. I've therefore kept a direct Cetus pool read
alongside the aggregator, not instead of it.

**Does your cost model need a pre-fee DEX price at all, or do you work purely
from `amountOut` and treat the whole thing as net?** If the latter, Q1 is my
problem alone and I'll stop looking for a trick that isn't there.

### Q2. Is a 3-hop route through CERT and BUCK expected and exercised?

`includeSources=CETUS` restricts the *source* but not the *assets*. Live, for
SUI→USDC, I get:

```
  1 SUI   1 path, 3 hops:   SUI -> CERT -> BUCK -> USDC   (fees 100/500/100)
 10 SUI   2 parallel paths: 5 SUI via that 3-hop route, 5 SUI direct
```

So it routes through Volo's staked SUI and Bucket's stablecoin. Your encoder
clearly anticipates this — `start_routing / next / finish_routing`, the
hop-chaining validation in `execution.py`, the parallel-path accumulation — so
this looks designed for rather than a surprise.

Three things I'd want to know before trusting it as a measurement:

1. **Have you actually executed a multi-hop route on mainnet, or only
   single-hop so far?** The encoder being correct in tests and correct against
   a live 3-hop `Route` object are different claims.
2. The 3-hop advantage I measured was tiny — 5,827,876 vs 5,827,788 raw USDC,
   about **0.15bps** — for three pools' worth of execution risk and two extra
   thin intermediates. Do you cap hop count, or accept whatever FlowX returns?
3. A route through CERT (a liquid-staking token) and BUCK (a small stablecoin)
   has a different risk profile from a direct swap. Does anything in your
   system constrain the *intermediate assets*, as opposed to the source?

For my purposes a 3-hop exotic route and a direct swap are different
instruments even at identical output, so I now record hop count and the
intermediates. But if you've had a multi-hop route behave badly live, that's
the single most useful thing you could tell me.

### Q3. Is the DEX fee a configured constant, and can it be?

`costs/model.py` sums `leg.fee_usd` for DEX legs, and `quotes.py` computes leg
fees from a `taker_fee_pct` passed in — which reads like a configured
percentage. Meanwhile the per-hop `fee` / `feeDenominator` in the quote's
`extra` seem to be carried through to `RouteHop.detail` for *encoding* (the
sqrt-price limit) rather than for costing.

The measured fee is not constant:

```
direct Cetus SUI/USDC pool   fee_rate 500/1e6  =  5 bps
3-hop route (100+500+100)                      =  7 bps
```

and which hops get chosen changes with size, so the fee changes with size.

I had this wrong worse than you possibly do: I hardcoded 25bps in my first cut
and the pool told me it was 5. I now sum the route's own per-hop fees.

**Two sub-questions.** Does your configured DEX percentage track the route, or
is it a fixed worst case? And — more importantly — since FlowX's `amountOut`
is already net of every pool fee, does adding a separate `dex_fee_usd` on top
risk double-counting it? Your `costs/model.py` docstring says venue fees are
never inside `quote_usd` and are added here exactly once, which is
unambiguous for an order-book leg and less obvious to me for an aggregator
quote. I may simply be missing where the DEX leg's `quote_usd` is made pre-fee.

### Q4. Where does the crossed-quote / glitch guard actually live?

The README's lesson is the most valuable thing in the pack for me:

> a single 10-second FlowX sample paying 2.8% above the market, and a buy
> price stuck at a stale value for minutes, below the same venue's sell price

A 2.8% glitch logged naively is a **280bps opportunity** that would dwarf every
real signal in my distribution, and I had no guard against it. I've since added
outlier rejection and crossed-quote detection on the strength of that
paragraph alone.

I couldn't find either guard in the shipped code — I assume they're in the
opportunity scanner, which isn't in the pack. **Could you say roughly how you
implement them?** Specifically: what do you compare a quote against to decide
it's an outlier? I'm using a slow-moving reference of my own earlier quotes,
which is self-referential and will drift with a genuine trend. Comparing
against the direct pool price seems strictly better, which is a third reason
I've kept the pool read.

### Q5. Is the gas figure single-hop or multi-hop?

You measured 0.0006–0.0016 SUI for a ~$5 swap. A 3-hop route touches three
pools and should cost more than a direct one. **Was that range measured on
single-hop swaps, multi-hop, or a mix?** It sets the minimum profitable size,
so I'd rather attribute it correctly than carry one number. I currently use
your upper bound and flag it in code as your measurement, not mine, and not at
my size.

### Q6. Which SUI/USDC pool, and does it matter to you?

The fixture's pool is `0x51e883ba…`. Live, FlowX routed through five others
(`0x6c545e78…`, `0x7e610a74…`, `0x4c50ba9d…`, `0x02629ace…`, `0x6dc4048a…`),
so there are several Cetus SUI/USDC pools at both the 100 and 500 tiers.

I pinned the fixture's pool for my direct read and it is evidently not the one
FlowX prefers. **Do you pin a pool anywhere, or always take FlowX's choice?**
If there's a canonical "deepest" pool I should be reading directly, that would
improve my baseline for Q1.

## Two things that might be worth a look in your system

Offered tentatively — I have a fraction of your code and you have live
results, so the prior is that I'm missing context.

1. **The possible DEX-fee double count in Q3.** If `amountOut` is already net
   and a configured `dex_fee_usd` is added, profit is understated by roughly
   the pool fee. Understating is the safe direction, so this would hide
   opportunities rather than lose money — which is exactly the failure mode
   your own gas lesson warns about.
2. **Q2.1, whether multi-hop has run live.** If every live swap so far has
   been single-hop, the first multi-hop route is an untested path in
   production, and the sqrt-price-limit clamping per hop is where I'd expect
   trouble.

## What I'd find most useful

In rough order: Q1 (do you need a pre-fee DEX price at all), Q2.1 (has
multi-hop run live), Q4 (how the glitch guard compares), then the rest.

And if you have the *distribution* of the gross SUI/USDC dislocation over
time — not individual opportunities, the whole series including the
unprofitable majority — that would be worth more to me than any code. My
measurements so far put the pair about 10bps short of covering costs, and the
question that decides everything is whether it ever spikes far enough, for
long enough, to clear them.
