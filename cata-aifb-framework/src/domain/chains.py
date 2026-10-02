"""Chain identity. Solana's entry is an INTERNAL SENTINEL, not a chain ID.

!!! PARTIAL MIRROR !!! The real AIFB repo owns src/domain/chains.py with the
PONS and Arc entries. On merge, ADD the Solana lines below to that file --
do not replace it. MERGE_MANIFEST.md lists the exact additions.

THE OPEN DECISION, stated here because this is where it bites. `chain_id` is
a plain int everywhere in AIFB: `launches.chain_id` is INT NOT NULL,
`TokenLaunch.chain_id` is int, and every per-chain dispatch dict is keyed by
int. Solana has no EVM chain ID, so a sentinel integer is required to fit the
existing schema without migrating it.

900001 is PROPOSED, NOT CONFIRMED. It is deliberately far outside the real
EVM chain-ID range so it cannot ever collide with a chain someone adds later.
It is load-bearing the moment any row is written with it -- changing it
afterwards means a data migration, not an edit -- so it needs owner sign-off
BEFORE the first production row. Flagged in
design/solana/PUMPFUN_ARCHITECTURE.md under "Open decisions".
"""
from __future__ import annotations

# --- existing AIFB chains, reproduced for context only -------------------
PONS_MAINNET_CHAIN_ID = 4663
PONS_TESTNET_CHAIN_ID = 46630
ARC_MAINNET_CHAIN_ID = 5042
ARC_TESTNET_CHAIN_ID = 5042002

# --- Solana: NOT REAL CHAIN IDS, internal sentinels only -----------------
# Solana does not have an EVM chain ID. These exist solely so a Solana launch
# can occupy the existing INT chain_id column. Never send either value to an
# EVM RPC, never show it to a user as a chain ID.
SOLANA_MAINNET_CHAIN_ID = 900001
SOLANA_DEVNET_CHAIN_ID = 900002

SOLANA_SENTINEL_CHAIN_IDS = frozenset({
    SOLANA_MAINNET_CHAIN_ID, SOLANA_DEVNET_CHAIN_ID})

# Lamports: 9 decimals. Solana's native currency has no contract address, so
# the address slot holds the zero pubkey -- the same sentinel pump.fun's own
# events use for "quoted in SOL", which keeps the two consistent.
SOLANA_NATIVE_ADDRESS = "11111111111111111111111111111111"
SOLANA_NATIVE_DECIMALS = 9

# These mirror the real file's per-chain dicts. Only the Solana rows are new.
# NOTE: these dicts assume ONE native currency per chain, which is true for
# PONS (ETH) and Arc (USDC) and is NOT fully true for pump.fun -- USDC-quoted
# coins are live on mainnet. This pass models native SOL only and FILTERS the
# rest out explicitly; see constants.is_native_sol_quote.
NATIVE_CURRENCY_BY_CHAIN: dict[int, tuple[str, int]] = {
    SOLANA_MAINNET_CHAIN_ID: (SOLANA_NATIVE_ADDRESS, SOLANA_NATIVE_DECIMALS),
    SOLANA_DEVNET_CHAIN_ID: (SOLANA_NATIVE_ADDRESS, SOLANA_NATIVE_DECIMALS),
}
NATIVE_CURRENCY_SYMBOL_BY_CHAIN: dict[int, str] = {
    SOLANA_MAINNET_CHAIN_ID: "SOL",
    SOLANA_DEVNET_CHAIN_ID: "SOL",
}
# No USD pricing pool is wired for Solana in this pass; None is the honest
# value and matches how the real dict marks a chain without one.
USD_PRICING_POOL_BY_CHAIN: dict[int, str | None] = {
    SOLANA_MAINNET_CHAIN_ID: None,
    SOLANA_DEVNET_CHAIN_ID: None,
}


def is_solana(chain_id: int) -> bool:
    return chain_id in SOLANA_SENTINEL_CHAIN_IDS
