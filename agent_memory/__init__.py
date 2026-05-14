"""Agent Memory Core package."""

# 自動載入 .env (放在 <vault_root>/.env, 跟著 brain 走,不在 repo 內)
# 讓 API key / Discord token 等敏感變數綁定到特定 brain,
# 比 setx 寫 Windows registry 好刪除/管理 (rm <vault>/.env 即可清光)。
def _resolve_vault_root():
    """順序找 vault: AGENT_MEMORY_VAULT env → user config → 預設 fallback"""
    import os
    from pathlib import Path

    # 1. 環境變數 (相容測試 / CI)
    explicit = os.environ.get("AGENT_MEMORY_VAULT", "")
    if explicit:
        p = Path(explicit)
        if p.exists():
            return p

    # 2. 使用者 config (~/.agent_memory/config.toml)
    try:
        cfg = Path.home() / ".agent_memory" / "config.toml"
        if cfg.exists():
            import re
            text = cfg.read_text(encoding="utf-8")
            m = re.search(r'root\s*=\s*"([^"]+)"', text)
            if m:
                p = Path(m.group(1).replace("\\\\", "\\"))
                if p.exists():
                    return p
    except Exception:  # noqa: BLE001
        pass

    # 3. 預設 fallback: <repo>/../SecondBrains/default_second_brain
    repo_root = Path(__file__).resolve().parent.parent
    candidate = repo_root.parent / "SecondBrains" / "default_second_brain"
    if candidate.exists():
        return candidate
    return None


def _autoload_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return
    vault = _resolve_vault_root()
    if vault is None:
        return
    env_file = vault / ".env"
    if env_file.exists():
        # override=False: 已存在的環境變數優先 (使用者顯式 setx 不會被 .env 蓋掉)
        load_dotenv(env_file, override=False)


_autoload_dotenv()
del _autoload_dotenv
del _resolve_vault_root

__all__ = ["__version__"]
__version__ = "0.1.0"
