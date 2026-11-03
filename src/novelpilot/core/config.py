"""Configuration: YAML file layered under environment variables.

Precedence (highest first): explicit constructor args > environment > YAML >
built-in defaults.  Secrets are never stored in the YAML file -- only the
*names* of environment variables are, so a config file can be committed safely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config/default.yaml")


@dataclass(slots=True)
class ModelRoles:
    """Which model serves which job.

    Keeping these as four separate knobs (rather than one "model") is what
    lets the product be cheap on average: planning is a small fraction of the
    token bill, while prose is almost all of it.
    """

    plan: str | None = None
    prose: str | None = None
    summary: str | None = None
    judge: str | None = None

    def for_role(self, role: str) -> str | None:
        return getattr(self, role, None)


@dataclass(slots=True)
class Config:
    # --- storage ------------------------------------------------------
    data_dir: Path = Path(".novelpilot")
    db_path: Path = Path(".novelpilot/novelpilot.db")
    exports_dir: Path = Path("exports")

    # --- providers ----------------------------------------------------
    provider: str = "openai"
    base_url: str | None = None
    api_key_env: str | None = None
    models: ModelRoles = field(default_factory=ModelRoles)
    temperature: float = 0.85
    judge_temperature: float = 0.2

    # --- generation ---------------------------------------------------
    target_words: int = 80_000
    words_per_chapter: int = 2_500
    scenes_per_chapter: int = 3
    max_retries: int = 3
    request_timeout: float = 180.0
    continuity_check: bool = True
    auto_repair: bool = False

    # --- budget guards ------------------------------------------------
    max_cost_per_book_usd: float = 5.00
    max_tokens_per_book: int = 2_000_000
    daily_token_budget: int = 5_000_000

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    # -- loading -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None, **overrides: Any) -> "Config":
        cfg = cls()
        p = Path(path) if path else (DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None)
        if p and p.exists():
            cfg = cfg.merge(_load_yaml(p))
        cfg = cfg.merge(_load_env())
        clean = {k: v for k, v in overrides.items() if v is not None}
        if clean:
            cfg = cfg.merge(clean)
        return cfg

    def merge(self, data: dict[str, Any]) -> "Config":
        data = dict(data)
        models = data.pop("models", None)
        # YAML gives us strings; the dataclass expects Path objects.
        for key in ("data_dir", "db_path", "exports_dir"):
            if data.get(key) is not None:
                data[key] = Path(str(data[key]))
        cfg = replace(self, **data) if data else self
        if models:
            cfg.models = ModelRoles(
                **{
                    **{
                        "plan": self.models.plan,
                        "prose": self.models.prose,
                        "summary": self.models.summary,
                        "judge": self.models.judge,
                    },
                    **{k: v for k, v in models.items() if v is not None},
                }
            )
        return cfg


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is a hard dep, but be safe
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_env() -> dict[str, Any]:
    """Environment overrides.

    Only a small, explicit set of variables are honoured so that behaviour
    stays predictable.
    """
    env: dict[str, Any] = {}
    if v := os.getenv("NOVELPILOT_PROVIDER"):
        env["provider"] = v
    if v := os.getenv("NOVELPILOT_BASE_URL"):
        env["base_url"] = v
    if v := os.getenv("NOVELPILOT_DB_PATH"):
        env["db_path"] = Path(v)
    if v := os.getenv("NOVELPILOT_DATA_DIR"):
        env["data_dir"] = Path(v)
    if v := os.getenv("NOVELPILOT_TARGET_WORDS"):
        env["target_words"] = int(v)
    if v := os.getenv("NOVELPILOT_MAX_COST_PER_BOOK"):
        env["max_cost_per_book_usd"] = float(v)
    if v := os.getenv("NOVELPILOT_MODEL_PLAN"):
        env.setdefault("models", {})["plan"] = v
    if v := os.getenv("NOVELPILOT_MODEL_PROSE"):
        env.setdefault("models", {})["prose"] = v
    if v := os.getenv("NOVELPILOT_MODEL_SUMMARY"):
        env.setdefault("models", {})["summary"] = v
    if v := os.getenv("NOVELPILOT_MODEL_JUDGE"):
        env.setdefault("models", {})["judge"] = v
    return env
