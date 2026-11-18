"""Run the official transport with durable usage accounting and no cumulative caps.

The token-only --probe never calls a model. Startup resumes only jobs with
durable verified official provenance. Secrets belong only in the explicit
private env file. The primary service never opens a OneBot gateway.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cockp.ai.auxiliary import AuxiliaryConfig, AuxiliaryPool, ROLES
from cockp.ai.evaluation_budget import BudgetTransport, evaluation_message
from cockp.ai.usage_ledger import UsageLedger
from cockp.ai.llm import DeepSeekProvider, ProviderError
from cockp.ai.context_budget import DEFAULT_WORKING_CONTEXT_TOKENS
from cockp.integrations.images import ImageQueue
from cockp.integrations.qqbot_api import QQBotAPI, QQBotAPIError
from cockp.integrations.qqbot_events import QQBotWebhookVerifier
from cockp.integrations.official_group_admin import OfficialGroupAdmin
from cockp.integrations.personal_admin_bridge import is_qq_id
from cockp.interfaces.official_app import create_official_app
from cockp.runtime.store import Store
from cockp.runtime.narration.budget import auto_compaction_config

CONTEXT_TOKEN_LIMIT = 1048576


class OfficialSessionError(ValueError):
    pass


def validate_existing_database(path):
    """The official runtime must not silently initialize an unrelated database."""
    if not Path(path).is_file():
        raise OfficialSessionError('existing_database_required')
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'schema_migrations', 'campaigns', 'provider_usage', 'official_event_inbox',
                        'official_group_bindings', 'official_member_bindings'}
            if not required <= tables:
                raise OfficialSessionError('existing_database_required')
    except sqlite3.Error:
        raise OfficialSessionError('existing_database_required') from None


def load_config(path):
    values = dotenv_values(path, encoding="utf-8", interpolate=False)
    if not re.fullmatch(r"[0-9]{1,20}", values.get("QQBOT_APP_ID") or "") or not values.get("QQBOT_APP_SECRET"):
        raise OfficialSessionError("official_credentials_required")
    if values.get("QQBOT_API_URL", "https://api.bot.qq.com") != "https://api.bot.qq.com":
        raise OfficialSessionError("official_endpoint_required")
    if values.get("COCKP_PERSONAL_ACCOUNT_MODE", "admin_only") != "admin_only":
        raise OfficialSessionError("personal_account_must_be_admin_only")
    for key, required in (("COCKP_AUX_MODEL", "deepseek-flash"), ("COCKP_AUX_BASE_URL", "https://api.deepseek.com")):
        if values.get(key, required) != required:
            raise OfficialSessionError("fixed_model_configuration_required")
    return values


class OfficialBudgetTransport(BudgetTransport):
    def __init__(self, *args, store, app_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.store, self.app_id = store, app_id

    async def handle_async_request(self, request):
        source = evaluation_message.get()
        if (not source.startswith("qqbot:" + self.app_id + ":")
                or not self.store.db.execute("SELECT 1 FROM official_event_inbox WHERE source_id=?", (source,)).fetchone()):
            raise ProviderError("verified_official_source_required")
        return await super().handle_async_request(request)


async def build_runtime(values, *, database_path, budget_path, private_dir, model_transport=None, api_client=None):
    validate_existing_database(database_path)
    group_id = values.get("QQBOT_EXPECTED_LEGACY_GROUP")
    owner_id = values.get("QQBOT_EXPECTED_LEGACY_OWNER")
    personal_id = values.get("QQ_PERSONAL_SELF_ID") or None
    if not is_qq_id(group_id) or not is_qq_id(owner_id) or (personal_id is not None and not is_qq_id(personal_id)):
        raise OfficialSessionError("configure_official_group_and_owner_ids")
    if values.get("QQ_PERSONAL_BRIDGE_URL") and not personal_id:
        raise OfficialSessionError("configure_personal_bridge_self_id")
    if not values.get("DEEPSEEK_API_KEY"):
        raise OfficialSessionError("existing_model_key_required")
    try:
        working_limit = int(values.get("COCKP_WORKING_CONTEXT_TOKEN_BUDGET", DEFAULT_WORKING_CONTEXT_TOKENS))
        if working_limit < DEFAULT_WORKING_CONTEXT_TOKENS:
            raise ValueError()
    except (ValueError, TypeError):
        raise OfficialSessionError("hosting_working_context_must_be_at_least_128k") from None
    try:
        compaction = auto_compaction_config(values)
    except ValueError:
        raise OfficialSessionError("invalid_context_compaction_configuration") from None
    try:
        ledger = UsageLedger(budget_path)
    except (sqlite3.Error, ValueError):
        raise OfficialSessionError("usage_ledger_invalid") from None
    private_dir = Path(private_dir).resolve()
    private_dir.mkdir(parents=True, exist_ok=True)
    os.environ.update(DEEPSEEK_API_KEY=values["DEEPSEEK_API_KEY"], COCKP_LLM="deepseek",
                      COCKP_DOCUMENT_ROOT=str(private_dir / "documents"), COCKP_IMAGES_ENABLED="false",
                      COCKP_CONTEXT_TOKEN_BUDGET=str(CONTEXT_TOKEN_LIMIT),
                      COCKP_WORKING_CONTEXT_TOKEN_BUDGET=str(working_limit),
                      COCKP_CONTEXT_AUTO_COMPACT=str(compaction.enabled).lower(),
                      COCKP_CONTEXT_COMPACT_TRIGGER_PERCENT=str(compaction.trigger_percent),
                      COCKP_CONTEXT_COMPACT_TARGET_PERCENT=str(compaction.target_percent),
                      COCKP_SHARED_FILES_ROOT=str(Path(database_path).resolve().parent / "shared-files"))
    store = Store(database_path)
    clients = {role: httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False,
        transport=OfficialBudgetTransport(ledger, role, store=store, app_id=values["QQBOT_APP_ID"],
                    private_dir=private_dir / "model-http", transport=model_transport)) for role in ("kp", *ROLES)}
    saved = store.db.execute("SELECT usage FROM provider_usage WHERE id='runtime:model'").fetchone()
    provider = DeepSeekProvider(client=clients["kp"], max_attempts=1, max_calls_per_action=5,
                               max_total_requests=None, max_output_tokens=600, timeout=45,
                               max_context_tokens=CONTEXT_TOKEN_LIMIT,
                               state=json.loads(saved[0]) if saved else None)
    configs = {role: AuxiliaryConfig(api_key=values["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com",
        model="deepseek-flash", max_attempts=1, max_calls_per_job=8, max_total_requests=None,
        max_output_tokens=350 if role == "router" else 16384 if role in {"character", "module"} else 700,
        max_context_tokens=CONTEXT_TOKEN_LIMIT,
        timeout=60, concurrency=1) for role in ROLES}
    saved_aux = store.db.execute("SELECT usage FROM provider_usage WHERE id='runtime:auxiliary'").fetchone()
    auxiliary = AuxiliaryPool(configs=configs, clients={r: clients[r] for r in ROLES},
                              states=json.loads(saved_aux[0]) if saved_aux else None)
    api = QQBotAPI(values["QQBOT_APP_ID"], values["QQBOT_APP_SECRET"], client=api_client)
    verifier = QQBotWebhookVerifier(app_id=values["QQBOT_APP_ID"], secret=values["QQBOT_APP_SECRET"])
    management = OfficialGroupAdmin(store, api, group_id=group_id, personal_bot_id=personal_id,
                                    bridge_url=values.get("QQ_PERSONAL_BRIDGE_URL") or None,
                                    bridge_token=values.get("QQ_PERSONAL_BRIDGE_TOKEN") or None)
    app = create_official_app(store=store, provider=provider, api=api, verifier=verifier, auxiliary=auxiliary,
        management=management, expected_group=group_id, legacy_owner=owner_id, legacy_bot_id=personal_id,
        enrollment_code_hash=values.get("QQBOT_ENROLLMENT_CODE_HASH", ""),
        image_queue=ImageQueue(None, global_enabled=False, max_calls_per_campaign=0, retries=0))
    return app, store, ledger, api, clients


async def run(args):
    values = load_config(args.env)
    if args.probe:
        api = QQBotAPI(values["QQBOT_APP_ID"], values["QQBOT_APP_SECRET"])
        try:
            print(json.dumps(await api.token_probe()))
        finally:
            await api.close()
        return
    app, store, ledger, api, clients = await build_runtime(values, database_path=args.database,
        budget_path=args.usage, private_dir=args.private_dir)
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                         workers=1, log_level="warning", access_log=False))
    try:
        await server.serve()
    finally:
        await app.state.service.close()
        for client in clients.values():
            await client.aclose()
        await api.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=ROOT / "data/runtime/state.sqlite3")
    parser.add_argument("--usage", "--budget", dest="usage", type=Path,
                        default=ROOT / "data/runtime/model-usage.sqlite3",
                        help="persistent model usage record (no cumulative allowance)")
    parser.add_argument("--private-dir", type=Path, default=ROOT / "data/runtime/private")
    parser.add_argument("--port", type=int, default=19057)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be an unprivileged local port")
    try:
        asyncio.run(run(args))
    except (OfficialSessionError, QQBotAPIError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
