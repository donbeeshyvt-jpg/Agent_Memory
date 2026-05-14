"""Agent Memory Core package."""

# 自動載入 .env (放在 agent-memory-core/ repo 根目錄)
# 讓 API key / Discord token 等敏感變數可以放在 .env 檔,
# 比 setx 寫 Windows registry 好刪除/管理,且 .env 已被 .gitignore 蓋住。
def _autoload_dotenv() -> None:
    try:
        from pathlib import Path
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return
    # repo 根目錄 = agent_memory/__init__.py 上兩層
    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    if env_path.exists():
        # override=False: 已存在的環境變數優先 (使用者顯式 setx 不會被 .env 蓋掉)
        load_dotenv(env_path, override=False)


_autoload_dotenv()
del _autoload_dotenv

__all__ = ["__version__"]
__version__ = "0.1.0"
