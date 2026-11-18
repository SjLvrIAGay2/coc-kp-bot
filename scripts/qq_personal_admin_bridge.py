"""Run the loopback-only group-card bridge; never restart or log in to QQ.

Configuration is read only from a private env file. No credential values, card
contents or gateway response bodies are printed. --check performs no network IO.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockp.integrations.personal_admin_bridge import (
    PersonalAdminBridge, PersonalBridgeConfig, PersonalBridgeError, is_qq_id,
    VerifiedTarget, create_personal_bridge_app,
)

ENV_PATH = ROOT / "config/private/personal-admin.env"
DATABASE_PATH = ROOT / "data/runtime/personal-admin.sqlite3"


def load_config(path=ENV_PATH):
    path = Path(path)
    if not path.is_file():
        raise PersonalBridgeError("private_bridge_env_missing")
    try:
        values = dotenv_values(path, encoding="utf-8", interpolate=False)
        bot_id = values.get("ONEBOT_SELF_ID")
        group_id = values.get("COCKP_GROUP_IDS")
        if not is_qq_id(bot_id) or not is_qq_id(group_id):
            raise PersonalBridgeError("personal_bridge_scope_invalid")
        raw = json.loads(values.get("QQ_PERSONAL_VERIFIED_TARGETS_JSON") or "{}")
        if not isinstance(raw, dict):
            raise PersonalBridgeError("verified_targets_invalid")
        targets = {}
        for member, record in raw.items():
            if not isinstance(record, dict) or set(record) != {"qq_id", "verified", "evidence", "group_id"}:
                raise PersonalBridgeError("verified_targets_invalid")
            targets[member] = VerifiedTarget(**record)
        return PersonalBridgeConfig(
            bot_id=bot_id, group_id=group_id,
            gateway_url=values.get("ONEBOT_API_URL") or "http://127.0.0.1:3000",
            gateway_token=values.get("ONEBOT_ACCESS_TOKEN") or "",
            bearer_token=values.get("QQ_PERSONAL_BRIDGE_TOKEN") or "",
            verified_targets=targets,
        )
    except PersonalBridgeError:
        raise
    except Exception:
        raise PersonalBridgeError("private_bridge_config_invalid") from None


async def run(args):
    config = load_config(args.env)
    if args.check:
        print(json.dumps({"mode": "personal_group_card_only", "extra_verified_mappings": len(config.verified_targets)}))
        return
    if type(args.port) is not int or not 1024 <= args.port <= 65535:
        raise PersonalBridgeError("loopback_port_invalid")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
        bridge = PersonalAdminBridge(config, args.database, client=client)
        import uvicorn
        server = uvicorn.Server(uvicorn.Config(create_personal_bridge_app(bridge),
            host="127.0.0.1", port=args.port, log_level="warning", access_log=False,
            proxy_headers=False))
        try:
            await server.serve()
        finally:
            bridge.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--env", type=Path, default=ENV_PATH)
    parser.add_argument("--database", type=Path, default=DATABASE_PATH)
    parser.add_argument("--port", type=int, default=19058)
    try:
        asyncio.run(run(parser.parse_args()))
    except PersonalBridgeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
