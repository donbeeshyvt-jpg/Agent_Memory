"""V3-O.10 本地 LLM (llama-cpp-python) CUDA runtime 環境修復 — 一鍵可重現.

問題背景:
  llama-cpp-python 0.3.x 預設是 CUDA 12 build (ggml-cuda.dll), 但很多機器只裝到
  CUDA 11.x toolkit. ggml.dll 硬依賴 ggml-cuda.dll, 後者需要 cudart64_12.dll +
  cublas64_12.dll, 缺了就 "Could not find module llama.dll (or one of its
  dependencies)" → import llama_cpp 失敗 → 子任務本地分流全 fallback 線上.

修法 (此 script 自動做):
  1. pip install nvidia-cuda-runtime-cu12 nvidia-cublas-cu12 nvidia-cuda-nvrtc-cu12
     (官方 pip 包提供 CUDA 12 user-mode runtime DLL, 不必裝肥大的 CUDA toolkit)
  2. 把這些 DLL 複製進 llama_cpp/lib/ (跟 ggml-cuda.dll 同目錄, Windows DLL 搜尋
     第一優先同目錄 → 一定找得到; os.add_dll_directory 對「依賴的依賴」解析不可靠)
  3. 驗證 import llama_cpp 成功

何時跑:
  - 第一次設定本地 LLM
  - 重裝 / 升級 llama-cpp-python 後 (會清掉複製的 DLL)
  - 換機

用法:
  python scripts/setup_local_llm_cuda.py

前提:
  - 有 NVIDIA GPU + 夠新的 driver (RTX 3090 driver 581.57 支援 CUDA 12+)
  - 已 pip install llama-cpp-python (CUDA build)
"""

from __future__ import annotations

import glob
import os
import shutil
import site
import subprocess
import sys


_NVIDIA_PIP_PACKAGES = [
    "nvidia-cuda-runtime-cu12",  # cudart64_12.dll
    "nvidia-cublas-cu12",         # cublas64_12.dll + cublasLt64_12.dll
    "nvidia-cuda-nvrtc-cu12",     # nvrtc64_120_0.dll (JIT)
]


def _find_llama_cpp_lib() -> str | None:
    """找 llama_cpp/lib 目錄 (放 ggml-cuda.dll 的地方)."""
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        cand = os.path.join(sp, "llama_cpp", "lib")
        if os.path.isdir(cand):
            return cand
    return None


def _find_nvidia_dll_dirs() -> list[str]:
    """找 pip nvidia-*-cu12 包的 bin 目錄 (含 cudart/cublas/nvrtc DLL)."""
    dirs: set[str] = set()
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            if glob.glob(os.path.join(d, "*.dll")):
                dirs.add(d)
    return sorted(dirs)


def _pip_install(packages: list[str]) -> bool:
    print(f"[1/3] pip install CUDA 12 runtime: {', '.join(packages)}")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", *packages],
            check=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        print(f"  ❌ pip install 失敗: {exc}")
        return False


def _copy_cuda_dlls(lib_dir: str, nvidia_dirs: list[str]) -> int:
    print(f"[2/3] 複製 CUDA 12 DLL 進 {lib_dir}")
    copied = 0
    for nd in nvidia_dirs:
        for src in glob.glob(os.path.join(nd, "*.dll")):
            name = os.path.basename(src)
            dst = os.path.join(lib_dir, name)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
                print(f"  + {name}")
                copied += 1
    if copied == 0:
        print("  (已存在, 無需複製)")
    return copied


def _verify_import(lib_dir: str) -> bool:
    print("[3/3] 驗證 import llama_cpp")
    try:
        os.add_dll_directory(lib_dir)
    except Exception:
        pass
    try:
        import llama_cpp  # noqa: F401
        print(f"  ✅ import llama_cpp 成功! ver: {llama_cpp.__version__}")
        return True
    except Exception as exc:
        print(f"  ❌ 還是失敗: {str(exc)[:200]}")
        return False


def main() -> int:
    print("=== V3-O.10 本地 LLM CUDA runtime 修復 ===\n")

    lib_dir = _find_llama_cpp_lib()
    if not lib_dir:
        print("❌ 找不到 llama_cpp/lib — 請先 pip install llama-cpp-python")
        return 2
    print(f"llama_cpp lib: {lib_dir}\n")

    if not _pip_install(_NVIDIA_PIP_PACKAGES):
        return 2

    nvidia_dirs = _find_nvidia_dll_dirs()
    if not nvidia_dirs:
        print("❌ pip 裝完但找不到 nvidia/*/bin DLL — 檢查 pip 環境")
        return 2
    _copy_cuda_dlls(lib_dir, nvidia_dirs)

    print()
    ok = _verify_import(lib_dir)
    print()
    if ok:
        print("✅ 完成! 本地 llama-cpp-python (GPU) 可用.")
        print("   提醒: companion_config.yaml providers.local_gemma 設 n_gpu_layers: -1 跑 GPU")
        return 0
    print("⚠ import 仍失敗 — 可能 GPU driver 太舊或缺其他依賴.")
    print("   退路: companion_config.yaml local_gemma kind 改 ollama, 或 sub_tasks 改線上.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
