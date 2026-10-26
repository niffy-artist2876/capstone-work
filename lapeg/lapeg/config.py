"""Experiment configuration: one YAML file per run under experiments/.

Every run is reproducible from its config: RPC (by env var name, never the URL itself, so keys stay out of
the repo), block, seed and parameters. When no archive RPC is available, `allow_recent_block` lets a run
fork a recent block instead; the block actually used is recorded in the run output, but that run can only
be replayed on an archive RPC.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from lapeg import PROJECT_ROOT


def load_dotenv(path: Path = PROJECT_ROOT / ".env") -> list[str]:
    """Load KEY=VALUE lines from lapeg/.env into the environment (git-ignored; holds RPC keys).
    Variables already set in the shell win. Returns the names loaded, never the values."""
    loaded: list[str] = []
    if not path.exists():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


load_dotenv()

# Free public endpoint that serves archive state (checked 2026-10-22,
# data/discovered/archive_check_drpc.json). No key; eth_getLogs is capped at about 100 blocks per request,
# and requests occasionally time out.
PUBLIC_ARCHIVE_RPC = "https://eth.drpc.org"
# Anvil options that ride out the free tier's timeouts.
# Anvil's own request throttle stays on: without it the free endpoint answers HTTP 429 (public rate limit).
ANVIL_RETRY_ARGS = ["--retries", "10", "--fork-retry-backoff", "1000", "--timeout", "60000"]


class ForkConfig(BaseModel):
    rpc_url_env: str = "ARCHIVE_RPC_URL"
    fallback_rpc_url: str | None = None  # used only when rpc_url_env is unset; may be non-archive
    block: int | None = None  # pinned fork block (required for reproducible runs)
    allow_recent_block: bool = False  # if block is None: fork at head - recent_block_lag
    recent_block_lag: int = Field(default=8, ge=0)
    port: int | None = None  # None: pick a free port
    anvil_args: list[str] = Field(default_factory=list)
    startup_timeout_s: float = 90.0


class LogsConfig(BaseModel):
    """Where event logs come from. Separate from the fork RPC: free tiers cap log ranges differently."""

    rpc_url_env: str = "LOGS_RPC_URL"
    fallback_rpc_url: str | None = None
    chunk: int = Field(default=10_000, ge=1)  # blocks per eth_getLogs; halved on range errors
    min_chunk: int = Field(default=50, ge=1)
    pause_s: float = Field(default=0.5, ge=0)  # pause between requests (rate limits)


class MarketRef(BaseModel):
    name: str
    market_id: str

    @field_validator("market_id")
    @classmethod
    def _bytes32(cls, v: str) -> str:
        v = v.lower()
        if not (v.startswith("0x") and len(v) == 66 and all(c in "0123456789abcdef" for c in v[2:])):
            raise ValueError("market_id must be a 0x-prefixed 32-byte hex string")
        return v


class ExperimentConfig(BaseModel):
    name: str
    seed: int = 0
    fork: ForkConfig
    logs: LogsConfig | None = None
    morpho_discovery: Path = Path("data/discovered/morpho_blue.json")
    market: MarketRef
    cache_path: Path = Path("data/raw/rpc_cache.sqlite")
    results_dir: Path = Path("data/results")
    params: dict[str, Any] = Field(default_factory=dict)

    def path(self, p: Path) -> Path:
        """Resolve a config path relative to the project root."""
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_experiment(path: str | Path) -> ExperimentConfig:
    with open(path, encoding="utf-8") as f:
        return ExperimentConfig.model_validate(yaml.safe_load(f))


def resolve_rpc_url(src: ForkConfig | LogsConfig) -> tuple[str, str]:
    """Return (url, source). source is the env var name, or 'fallback: <url>' for the public default."""
    url = os.environ.get(src.rpc_url_env)
    if url:
        return url, src.rpc_url_env
    if src.fallback_rpc_url:
        return src.fallback_rpc_url, "fallback"
    raise RuntimeError(f"set {src.rpc_url_env} or give a fallback_rpc_url")


def resolve_fork_block(fork: ForkConfig, head: int) -> tuple[int, bool]:
    """Return (block, pinned). LAPEG_FORK_BLOCK in the environment overrides the config."""
    env_block = os.environ.get("LAPEG_FORK_BLOCK")
    if env_block:
        return int(env_block), True
    if fork.block is not None:
        return fork.block, True
    if fork.allow_recent_block:
        return head - fork.recent_block_lag, False
    raise RuntimeError("fork.block is not set and allow_recent_block is false")
