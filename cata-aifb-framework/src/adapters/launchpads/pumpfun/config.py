"""PumpfunConfig -- this service's own config module.

Its OWN module by the isolation rule, not a variant of the EVM bridge's
BridgeConfig: "adding or changing anything about pump.fun never requires
touching PONS/Arc's production files" is only true if the config object is
separate too. The env-var NAMING follows the existing
{PREFIX}_HTTP_URL / {PREFIX}_HTTP_URL_SECONDARY_n convention so operators do
not have to learn a second shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.config import env_flag, env_int, env_str, env_url_chain
from src.domain.chains import SOLANA_DEVNET_CHAIN_ID, SOLANA_MAINNET_CHAIN_ID

# Stream names are dedicated to pump.fun, not shared with PONS/Arc. A shared
# stream would make one chain's consumer lag another chain's producer, and the
# isolation rule exists to prevent exactly that coupling.
STREAM_LAUNCHES = "pumpfun-token-launches"
STREAM_GRADUATIONS = "pumpfun-graduation-signals"
STREAM_MAXLEN = 100_000

COMPONENT = "pumpfun_collector"
HEARTBEAT_INTERVAL_S = 60


@dataclass
class PumpfunConfig:
    rpc_urls: list[str] = field(default_factory=list)
    geyser_url: str = ""
    geyser_token: str = ""
    network: str = "mainnet-beta"
    chain_id: int = SOLANA_MAINNET_CHAIN_ID
    enabled: bool = False
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str = ""
    operations_url: str = ""
    heartbeat_interval_s: int = HEARTBEAT_INTERVAL_S
    cold_start_signatures: int = 1000
    native_quote_only: bool = True

    @property
    def is_devnet(self) -> bool:
        return self.network != "mainnet-beta"

    def describe(self) -> dict:
        """Effective config for the one startup log line.

        Deliberately reports endpoint HOSTS and never full URLs: a provider
        URL carries its API key in the path, and a config dump is the most
        common way a key ends up in a log aggregator.
        """
        from urllib.parse import urlparse
        hosts = []
        for u in self.rpc_urls:
            try:
                hosts.append(urlparse(u).hostname or "?")
            except Exception:  # noqa: BLE001
                hosts.append("?")
        return {
            "network": self.network,
            "chain_id": self.chain_id,
            "enabled": self.enabled,
            "rpc_endpoint_hosts": hosts,
            "rpc_endpoint_count": len(self.rpc_urls),
            "geyser_configured": bool(self.geyser_url),
            "streams": [STREAM_LAUNCHES, STREAM_GRADUATIONS],
            "native_quote_only": self.native_quote_only,
            "cold_start_signatures": self.cold_start_signatures,
        }


def load_config() -> PumpfunConfig:
    network = env_str("SOLANA_NETWORK", "mainnet-beta")
    # NON_EVM_SOL_RPC_URL is included as a recognised fallback because that is
    # the name this deployment already uses for its Solana node.
    urls = env_url_chain("SOLANA_RPC", "NON_EVM_SOL_RPC_URL")
    if not urls:
        urls = ["https://api.mainnet-beta.solana.com"
                if network == "mainnet-beta" else
                "https://api.devnet.solana.com"]
    return PumpfunConfig(
        rpc_urls=urls,
        geyser_url=env_str("SOLANA_GEYSER_GRPC_URL"),
        geyser_token=env_str("SOLANA_GEYSER_GRPC_TOKEN"),
        network=network,
        chain_id=(SOLANA_MAINNET_CHAIN_ID if network == "mainnet-beta"
                  else SOLANA_DEVNET_CHAIN_ID),
        enabled=env_flag("PUMPFUN_COLLECTOR_ENABLED", False),
        redis_host=env_str("REDIS_HOST", "redis"),
        redis_port=env_int("REDIS_PORT", 6379),
        redis_password=env_str("REDIS_PASSWORD"),
        operations_url=env_str("OPERATIONS_URL", "http://operations:8116"),
        heartbeat_interval_s=env_int("PUMPFUN_HEARTBEAT_INTERVAL_S",
                                     HEARTBEAT_INTERVAL_S),
        cold_start_signatures=env_int("PUMPFUN_COLD_START_SIGNATURES", 1000),
        native_quote_only=not env_flag("PUMPFUN_ALLOW_TOKEN_QUOTES", False),
    )
