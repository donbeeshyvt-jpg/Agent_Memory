"""Helper for first-run-wizard.ps1: wire local llama_cpp_python provider into the
vault's llm_router.yaml so the steward can chat without degraded fallback.

Updates:
  - global_default.profile/model
  - providers.llama_cpp_local.model_path
  - (optional) providers.llama_cpp_local.path_prepend  for CUDA runtime DLL search

Usage:
  python scripts/_set_llm_router.py \
    --router-yaml <path> \
    --model <relative-or-absolute model path> \
    [--cuda-path <dir>] \
    [--json]

The --model value is written verbatim into both global_default.model and
providers.llama_cpp_local.model_path. Use sandbox-style relative paths
(e.g. "../../0_Models/.../foo.gguf") for vault-portability; the runtime's
_resolve_llama_model_path() walks vault_root/parent/parent.parent and
parent.parent/0_Models so relatives still locate the file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--router-yaml", required=True, help="Path to llm_router.yaml inside the vault.")
    ap.add_argument("--model", required=True, help="Model id / path (written verbatim).")
    ap.add_argument("--profile", default="llama_cpp_local", help="Provider profile id (default: llama_cpp_local).")
    ap.add_argument("--cuda-path", default="", help="Optional CUDA bin dir to prepend for DLL search.")
    ap.add_argument("--json", action="store_true", help="Print result as JSON.")
    args = ap.parse_args()

    p = Path(args.router_yaml)
    if not p.exists():
        print(f"[ERR] router yaml not found: {p}", file=sys.stderr)
        return 2

    raw = p.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    data.setdefault("global_default", {})
    data["global_default"]["profile"] = args.profile
    data["global_default"]["model"] = args.model

    data.setdefault("providers", {})
    data["providers"].setdefault(args.profile, {})
    data["providers"][args.profile]["model_path"] = args.model

    if args.cuda_path:
        data["providers"][args.profile]["path_prepend"] = [args.cuda_path]
    else:
        data["providers"][args.profile].pop("path_prepend", None)

    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    summary = {
        "ok": True,
        "router_yaml": str(p),
        "profile": args.profile,
        "model": args.model,
        "cuda_path": args.cuda_path,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"[OK] router_yaml={p}")
        print(f"[OK] profile={args.profile}")
        print(f"[OK] model={args.model}")
        if args.cuda_path:
            print(f"[OK] cuda_path={args.cuda_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
