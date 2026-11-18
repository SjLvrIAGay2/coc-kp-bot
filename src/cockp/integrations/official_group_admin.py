"""Explicit official QQ group administration; only cards use the narrow bridge.

Current QQ v2 contracts: bot_state/member GET, restrict_chat_setting POST,
messages DELETE. No personal-account fallback exists for official operations.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
from string import Formatter
import time
from urllib.parse import quote, urlsplit

from cockp.core.contracts import Output
from cockp.core.security import guard_outputs
from cockp.integrations.group_config import group_options
from cockp.integrations.personal_admin_bridge import VerifiedTarget, is_qq_id
from cockp.integrations.qqbot_api import API_BASE
from cockp.runtime.messaging.inbound import parse_command
from cockp.runtime.store import dumps


class OfficialAdminError(ValueError):
    def __init__(self, code, *, unknown=False):
        super().__init__(code)
        self.code, self.unknown = code, unknown


def _opaque(value, maximum=256):
    return (isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:-]+", value))
            and len(value) <= maximum)


def _nickname_failure_marker(store):
    return None if store.path == ":memory:" else Path(store.path).resolve().parent / "official-nickname-sync.blocked"


def nickname_sync_blocked(store):
    """A failed privacy freeze requires operator repair, including after restart."""
    if getattr(store, "_official_nickname_preparation_blocked", False):
        return True
    try:
        marker = _nickname_failure_marker(store)
        if marker is not None and os.path.lexists(marker):
            return True
        return store.db.execute("SELECT 1 FROM official_admin_actions WHERE action='auto_card_prepare_failure' AND status='blocked' LIMIT 1").fetchone() is not None
    except Exception:
        return True


def record_nickname_preparation_failure(store, app_id, campaign_id, source_id, change_request_id=None):
    """Persist only a fixed safe error; a failed audit cannot reopen card writes."""
    store._official_nickname_preparation_blocked = True
    result = {"code": "automatic_nickname_preparation_failed", "requires_operator_review": True}
    try:
        marker = _nickname_failure_marker(store)
        if marker is not None:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write('{"code":"automatic_nickname_preparation_failed","requires_operator_review":true}\n')
                stream.flush()
                os.fsync(stream.fileno())
    except Exception:
        pass  # The process latch and independent database audit also deny writes.
    try:
        digest = hashlib.sha256(dumps([app_id, campaign_id, source_id, change_request_id]).encode()).hexdigest()
        source = store.db.execute("SELECT group_openid,user_openid FROM official_reply_sources WHERE source_id=?", (source_id,)).fetchone()
        store.db.execute("INSERT OR IGNORE INTO official_admin_actions "
            "(request_id,source_id,app_id,group_openid,actor_openid,campaign_id,action,fingerprint,status,result,created_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("auto-prepare-failure:" + digest, source_id, app_id,
            (source["group_openid"] or "") if source else "", source["user_openid"] if source else "", campaign_id,
            "auto_card_prepare_failure", digest, "blocked", dumps(result), time.time(), time.time()))
    except Exception:
        pass  # Never turn an already committed gameplay/import action into failure.


class OfficialGroupAdmin:
    def __init__(self, store, api, *, group_id, personal_bot_id=None, bridge_url=None, bridge_token=None,
                 verified_targets=None, bridge_client=None, clock=time.time):
        self.store, self.api, self.clock = store, api, clock
        if not is_qq_id(group_id) or (personal_bot_id is not None and not is_qq_id(personal_bot_id)):
            raise OfficialAdminError("official_group_scope_invalid")
        self.group_id, self.personal_bot_id = group_id, personal_bot_id
        self.bridge_url, self._bridge_token = bridge_url, bridge_token
        self.bridge_client = bridge_client or api.client
        self.verified_targets = dict(verified_targets or {})
        if any(not _opaque(member, 128) or not isinstance(target, VerifiedTarget) or target.group_id != group_id
               for member, target in self.verified_targets.items()):
            raise OfficialAdminError("verified_card_targets_invalid")
        if bridge_url is not None:
            try:
                parsed = urlsplit(bridge_url)
                valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                         and parsed.port is not None and 1 <= parsed.port <= 65535
                         and parsed.path in {"", "/"} and not parsed.username and not parsed.password
                         and not parsed.query and not parsed.fragment)
            except (TypeError, ValueError):
                valid = False
            if (not valid or personal_bot_id is None or not isinstance(bridge_token, str) or len(bridge_token) < 32
                    or not bridge_token.isascii() or any(c.isspace() or ord(c) < 33 for c in bridge_token)):
                raise OfficialAdminError("loopback_card_bridge_configuration_invalid")
        self.lock = asyncio.Lock()

    def _verified_source(self, data, message):
        if (not isinstance(data, dict) or message.private or message.group_id != self.group_id
                or data.get("private") is not False or data.get("internal_group") != self.group_id
                or data.get("app_id") != self.api.app_id or data.get("role") not in {"owner", "admin"}
                or message.group_role != data.get("role") or message.user_id != data.get("actor_id")
                or message.text != data.get("text")):
            raise OfficialAdminError("signed_group_admin_required")
        group = data.get("group_openid")
        member = data.get("user_openid")
        if not _opaque(group) or not _opaque(member) or group not in self.api.allowed_groups:
            raise OfficialAdminError("official_group_not_bound")
        row = self.store.db.execute(
            "SELECT i.message FROM official_event_inbox i JOIN official_wire_events w "
            "ON w.source_id=i.source_id AND w.fingerprint=i.fingerprint "
            "JOIN official_reply_sources s ON s.source_id=i.source_id "
            "JOIN official_group_bindings g ON g.group_openid=s.group_openid "
            "WHERE i.source_id=? AND g.internal_group=? AND s.group_openid=? "
            "AND s.user_openid=? AND s.private=0 AND "
            "w.event_type IN ('GROUP_MESSAGE_CREATE','GROUP_AT_MESSAGE_CREATE')",
            (message.message_id, self.group_id, group, member)).fetchone()
        if row is None or json.loads(row["message"]) != data:
            raise OfficialAdminError("signed_management_source_missing")
        campaign = self.store.active(self.group_id)
        if campaign is None:
            raise OfficialAdminError("active_campaign_required")
        return campaign

    def _claim(self, data, message, action, args, campaign):
        fingerprint = hashlib.sha256(dumps([data, action, args, campaign["id"]]).encode()).hexdigest()
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM official_admin_actions WHERE request_id=?",
                             (message.message_id,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise OfficialAdminError("management_request_conflict")
                return (json.loads(row["result"]) if row["result"] else
                        {"request_id": message.message_id, "action": action, "status": "unknown",
                         "code": "already_dispatched_or_in_progress"})
            db.execute("INSERT INTO official_admin_actions VALUES(?,?,?,?,?,?,?,?,?,NULL,?,NULL,NULL)",
                (message.message_id, message.message_id, self.api.app_id, data["group_openid"],
                 data["user_openid"], campaign["id"], action, fingerprint, "checking", self.clock()))
        return None

    def _finish(self, message, result):
        with self.store.transaction() as db:
            db.execute("UPDATE official_admin_actions SET status=?,result=?,completed_at=? WHERE request_id=?",
                       (result["status"], dumps(result), self.clock(), message.message_id))
        return result

    def _dispatching(self, message):
        self.store.db.execute("UPDATE official_admin_actions SET status='dispatching',dispatched_at=? WHERE request_id=?",
                              (self.clock(), message.message_id))

    async def _request(self, method, path, *, payload=None):
        try:
            token = await self.api.access_token()
        except Exception:
            raise OfficialAdminError("official_access_token_unavailable") from None
        try:
            kwargs = {"headers": {"Authorization": "QQBot " + token}, "timeout": 10,
                      "follow_redirects": False}
            if payload is not None:
                kwargs["json"] = payload
            response = await self.api.client.request(method, API_BASE + path, **kwargs)
        except Exception:
            raise OfficialAdminError("official_transport_unconfirmed", unknown=method != "GET") from None
        try:
            value = response.json() if response.content else {}
        except ValueError:
            value = None
        numeric = []
        if isinstance(value, dict):
            for key in ("code", "err_code"):
                if key in value:
                    code = value[key]
                    if type(code) is int and 0 <= code <= 9999999999:
                        numeric.append(code)
                    elif isinstance(code, str) and code.isdecimal() and len(code) <= 10:
                        numeric.append(int(code))
                    else:
                        numeric.append(None)
        if (not isinstance(value, dict) or None in numeric or len(set(numeric)) > 1):
            raise OfficialAdminError("official_response_unconfirmed", unknown=method != "GET")
        code = numeric[0] if numeric else 0
        if code:
            raise OfficialAdminError("official_permission_unavailable" if code == 11253 else "official_business_" + str(code),
                                     unknown=method != "GET" and response.status_code >= 500)
        if response.status_code != 200:
            unknown = response.status_code >= 500 or response.status_code in {201, 202, 408}
            raise OfficialAdminError("official_http_" + str(response.status_code), unknown=unknown and method != "GET")
        return value

    @staticmethod
    def _group_path(group):
        return "/v2/groups/" + quote(group, safe="")

    async def _bot_state(self, group):
        state = await self._request("GET", self._group_path(group) + "/bot_state")
        if state.get("member_role") not in {"owner", "admin", "member"} or not _opaque(state.get("member_openid")):
            raise OfficialAdminError("official_bot_state_invalid")
        return state

    def _recheck_campaign(self, campaign, action, duration):
        current = self.store.active(self.group_id)
        if current is None or current["id"] != campaign["id"]:
            raise OfficialAdminError("management_campaign_changed")
        options = group_options(current["state"])
        if options["group_admin_enabled"] is not True:
            raise OfficialAdminError("group_admin_disabled")
        if action == "ban":
            limit = options.get("max_ban_seconds")
            if type(limit) is not int or not 1 <= duration <= min(2592000, limit):
                raise OfficialAdminError("ban_duration_invalid")

    def _target_member(self, group, target):
        if not _opaque(target):
            raise OfficialAdminError("official_member_target_invalid")
        rows = self.store.db.execute(
            "SELECT member_openid FROM official_member_bindings WHERE group_openid=? "
            "AND (member_openid=? OR actor_id=?)", (group, target, target)).fetchall()
        identifiers = {row[0] for row in rows}
        if len(identifiers) != 1:
            raise OfficialAdminError("official_member_target_unbound")
        return next(iter(identifiers))

    async def _ordinary_member(self, group, target, bot):
        member = await self._request("GET", self._group_path(group) + "/members/" + quote(target, safe=""))
        if (member.get("member_openid") != target or member.get("member_role") != "member"
                or member.get("bot") is not False or target == bot["member_openid"]):
            raise OfficialAdminError("protected_or_unverified_target")

    def _recall_provenance(self, group, message_id):
        if not _opaque(message_id, 2048):
            raise OfficialAdminError("recall_message_id_invalid")
        now = self.clock()
        rows = self.store.db.execute(
            "SELECT i.message FROM official_reply_sources s JOIN official_event_inbox i USING(source_id) "
            "JOIN official_wire_events w ON w.source_id=i.source_id AND w.fingerprint=i.fingerprint "
            "WHERE s.group_openid=? AND s.msg_id=? AND s.private=0 "
            "AND w.event_type IN ('GROUP_MESSAGE_CREATE','GROUP_AT_MESSAGE_CREATE')",
            (group, message_id)).fetchall()
        for row in rows:
            value = json.loads(row["message"])
            stamp = value.get("timestamp")
            if (value.get("group_openid") == group and value.get("role") == "member"
                    and value.get("private") is False and value.get("message_id") == message_id
                    and type(stamp) in {int, float} and math.isfinite(stamp) and 0 <= now-stamp < 120):
                return "member"
        rows = self.store.db.execute(
            "SELECT p.receipt FROM official_delivery_parts p JOIN outbox o ON o.id=p.outbox_id "
            "JOIN official_outbox_sources b ON b.outbox_id=o.id "
            "JOIN official_reply_sources s ON s.source_id=b.source_id "
            "WHERE o.destination=? AND s.group_openid=? AND s.private=0 AND p.status='sent'",
            ("group:" + self.group_id, group)).fetchall()
        for row in rows:
            try:
                receipt = json.loads(row["receipt"])
            except (ValueError, TypeError):
                continue
            stamp = receipt.get("sent_at") if isinstance(receipt, dict) else None
            if (isinstance(receipt, dict) and receipt.get("status") == "sent"
                    and receipt.get("message_id") == message_id and type(stamp) in {int, float}
                    and math.isfinite(stamp) and 0 <= now-stamp < 120):
                return "self"
        raise OfficialAdminError("recall_same_group_recent_provenance_required")

    def _card_payload(self, message, group, args):
        if not self.personal_bot_id:
            raise OfficialAdminError("personal_card_bridge_unavailable")
        target, card = args.get("user_id"), args.get("card")
        if not isinstance(card, str) or len(card) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in card):
            raise OfficialAdminError("card_invalid")
        payload = {"request_id": "official-card:" + hashlib.sha256(message.message_id.encode()).hexdigest(),
                   "group_id": self.group_id, "target_qq_id": self.personal_bot_id, "card": card}
        if target == self.personal_bot_id:
            return payload
        matches = {member: record for member, record in self.verified_targets.items()
                   if target in {member, record.qq_id}}
        if len(matches) != 1:
            # A numeric legacy actor binding is not proof of a QQ/OpenID mapping.
            raise OfficialAdminError("verified_card_mapping_required")
        member, record = next(iter(matches.items()))
        self._target_member(group, member)
        payload.update(official_member_id=member, target_qq_id=record.qq_id)
        return payload

    def _automatic_source(self, source_id):
        row = self.store.db.execute(
            "SELECT i.message,s.group_openid,s.user_openid,s.private FROM official_reply_sources s "
            "JOIN official_event_inbox i USING(source_id) JOIN official_wire_events w "
            "ON w.source_id=i.source_id AND w.fingerprint=i.fingerprint "
            "WHERE s.source_id=? AND w.event_type IN ('GROUP_MESSAGE_CREATE','GROUP_AT_MESSAGE_CREATE','C2C_MESSAGE_CREATE')",
            (source_id,)).fetchone()
        if row is None:
            raise OfficialAdminError("automatic_nickname_source_missing")
        data = json.loads(row["message"])
        source = dict(row)
        if source["private"]:
            link = self.store.db.execute("SELECT group_openid FROM official_c2c_bindings WHERE user_openid=?",
                                          (source["user_openid"],)).fetchone()
            source["group_openid"] = link[0] if link else None
        binding = self.store.db.execute("SELECT 1 FROM official_group_bindings WHERE internal_group=? AND group_openid=?",
                                        (self.group_id, source["group_openid"])).fetchone()
        if (data.get("app_id") != self.api.app_id or data.get("private") is not bool(row["private"])
                or not row["private"] and data.get("group_openid") != source["group_openid"]
                or data.get("user_openid") != row["user_openid"]
                or not binding or source["group_openid"] not in self.api.allowed_groups):
            raise OfficialAdminError("automatic_nickname_source_missing")
        source["data"] = data
        return source

    def _private_resource_source(self, campaign, source_id, source, change_request_id=None):
        if source["private"]:
            return True
        try:
            command, args = parse_command(source["data"]["text"]) if source["data"]["text"].startswith("/") else ("", {})
        except Exception:
            return True  # Invalid commands cannot authorize a public resource sync.
        visibility = args.get("visibility", "public")
        if visibility != "public":
            return True
        action = campaign["state"].get("actions", {}).get(args.get("action_id"), {})
        if action.get("visibility", "public") != "public":
            return True
        for request_id in dict.fromkeys((source_id, change_request_id or source_id)):
            row = self.store.db.execute("SELECT result FROM requests WHERE campaign_id=? AND request_id=?",
                                         (campaign["id"], request_id)).fetchone()
            if not row:
                continue
            result = json.loads(row[0])
            if any(item.get("visibility", {}).get("scope", "public") != "public" for item in result.get("outputs", [])):
                return True
            event = self.store.db.execute("SELECT visibility FROM events WHERE id=?", (result.get("event_id"),)).fetchone()
            if event and json.loads(event[0]).get("scope", "public") != "public":
                return True
        return False

    @staticmethod
    def _resource_values(character):
        resources = character.get("resources", {})
        return {"character_name": character.get("name"), "hp": resources.get("hp"), "hp_max": resources.get("max_hp"),
                "san": resources.get("san"), "san_max": resources.get("max_san")}

    def _source_event_snapshot(self, campaign_id, source_id, change_request_id=None):
        """Resolve background installs through durable job provenance as well.

        Background request IDs are job IDs, while authorization remains bound
        to the original official upload/supplement source. No label or current
        user's filename participates in this association.
        """
        direct = self.store.db.execute(
            "SELECT e.snapshot,e.state_version,e.seq FROM requests r JOIN events e "
            "ON e.id=json_extract(r.result,'$.event_id') AND e.campaign_id=r.campaign_id "
            "WHERE r.campaign_id=? AND r.request_id=?", (campaign_id, source_id)).fetchone()
        if direct:
            if change_request_id is None or change_request_id == source_id:
                return direct
        return self.store.db.execute(
            "SELECT e.snapshot,e.state_version,e.seq FROM official_job_sources j "
            "JOIN requests r ON r.campaign_id=j.campaign_id AND (r.request_id=j.job_id||':install' "
            "OR substr(r.request_id,1,length(j.job_id)+9)=j.job_id||':install:') "
            "JOIN events e ON e.id=json_extract(r.result,'$.event_id') AND e.campaign_id=r.campaign_id "
            "WHERE j.campaign_id=? AND j.source_id=? AND e.actor_id=j.actor_id "
            "AND j.actor_id=json_extract((SELECT message FROM official_event_inbox WHERE source_id=j.source_id),'$.actor_id') "
            "AND (? IS NULL OR r.request_id=?) ORDER BY e.seq DESC LIMIT 1",
            (campaign_id, source_id, change_request_id, change_request_id)).fetchone()

    def _hidden_resource_hashes(self, campaign, source_id, change_request_id=None):
        previous = None
        event = self._source_event_snapshot(campaign["id"], source_id, change_request_id)
        if event:
            row = self.store.db.execute("SELECT snapshot FROM events WHERE campaign_id=? AND seq<? ORDER BY seq DESC LIMIT 1",
                                        (campaign["id"], event["seq"])).fetchone()
            previous = self.store.read_snapshot(row[0]) if row else None
        protected = {}
        for character_id, character in campaign["state"].get("characters", {}).items():
            before = self._resource_values(previous.get("characters", {}).get(character_id, {})) if previous else {}
            fields = {key: hashlib.sha256(dumps(value).encode()).hexdigest()
                      for key, value in self._resource_values(character).items() if before.get(key) != value}
            if fields:
                protected[character_id] = fields
        return protected

    def _protected_resource_fields(self, campaign_id):
        protected = {}
        for row in self.store.db.execute("SELECT result FROM official_admin_actions WHERE campaign_id=? AND action='auto_card_plan' "
                                         "ORDER BY json_extract(result,'$.plan.source_state_version'),created_at,rowid",
                                         (campaign_id,)):
            saved = json.loads(row[0]) if row[0] else {}
            plan = saved.get("plan", {})
            version = plan.get("source_state_version", -1)
            for character_id, fields in plan.get("hidden_resource_hashes", {}).items():
                for key, digest in fields.items():
                    previous = protected.setdefault(character_id, {}).get(key)
                    if previous is None or version >= previous["version"]:
                        protected[character_id][key] = {"hash": digest, "version": version}
            for character_id, fields in plan.get("public_resource_hashes", {}).items():
                for key, digest in fields.items():
                    previous = protected.get(character_id, {}).get(key)
                    if previous is not None and version > previous["version"] and previous["hash"] != digest:
                        protected[character_id].pop(key)
        return {character_id: {key: value["hash"] for key, value in fields.items()}
                for character_id, fields in protected.items()}

    @staticmethod
    def _nickname_template(template):
        if (not isinstance(template, str) or not 1 <= len(template) <= 200
                or any(ord(c) < 32 or ord(c) == 127 for c in template)):
            raise OfficialAdminError("nickname_template_invalid")
        try:
            fields = list(Formatter().parse(template))
        except ValueError:
            raise OfficialAdminError("nickname_template_invalid") from None
        allowed = {"character_name", "hp", "hp_max", "san", "san_max"}
        if any(field is not None and (field not in allowed or spec or conversion)
               for _, field, spec, conversion in fields):
            raise OfficialAdminError("nickname_template_invalid")

    def _auto_target(self, group, owner):
        if self.personal_bot_id and owner == self.personal_bot_id:
            return {"target_qq_id": self.personal_bot_id, "official_member_id": None, "mapping_evidence_hash": None}
        member = self._target_member(group, owner)
        verified = self.verified_targets.get(member)
        if verified is None:
            raise OfficialAdminError("verified_card_mapping_required")
        return {"target_qq_id": verified.qq_id, "official_member_id": member,
                "mapping_evidence_hash": hashlib.sha256(verified.evidence.encode()).hexdigest()}

    def _nickname_plan(self, campaign, source_id, source, change_request_id=None):
        ident = self._nickname_plan_id(self.api.app_id, campaign["id"], source_id, change_request_id)
        existing = self.store.db.execute("SELECT result FROM official_admin_actions WHERE request_id=?", (ident,)).fetchone()
        if existing:
            return ident, json.loads(existing["result"])["plan"]
        state = campaign["state"]
        options = group_options(state)
        template = options["nickname_template"]
        plan = {"source_state_version": campaign["version"], "source_config_version": state.get("config_version", 0),
                "template": template, "targets": [], "skipped": [], "group_openid": source["group_openid"],
                "synchronous_prepared": True, "source_id": source_id,
                "change_request_id": change_request_id or source_id}
        private_source = self._private_resource_source(campaign, source_id, source, change_request_id)
        plan["private_source"] = private_source
        if private_source:
            plan["hidden_resource_hashes"] = self._hidden_resource_hashes(campaign, source_id, change_request_id)
        else:
            plan["public_resource_hashes"] = {character_id: {key: hashlib.sha256(dumps(value).encode()).hexdigest()
                for key, value in self._resource_values(character).items()}
                for character_id, character in state.get("characters", {}).items()}
        protected = self._protected_resource_fields(campaign["id"])
        latest = {}
        for row in self.store.db.execute(
                "SELECT status,result FROM official_admin_actions WHERE app_id=? AND group_openid=? "
                "AND action IN ('auto_card','card') ORDER BY created_at DESC,rowid DESC",
                (self.api.app_id, source["group_openid"])):
            saved = json.loads(row["result"]) if row["result"] else {}
            target = saved.get("target_qq_id")
            if target:
                latest.setdefault(target, {**saved, "status": row["status"]})
        try:
            if private_source:
                raise OfficialAdminError("private_resource_source_blocked")
            self._nickname_template(template)
        except OfficialAdminError as exc:
            plan["skipped"].append({"code": exc.code})
        else:
            owners = {}
            for character_id, character in sorted(state.get("characters", {}).items()):
                if character.get("npc"):
                    continue
                owner = character.get("owner_id")
                if not isinstance(owner, str) or not owner:
                    plan["skipped"].append({"character_id": character_id, "code": "character_owner_missing"})
                    continue
                owners.setdefault(owner, []).append((character_id, character))
            for owner, characters in sorted(owners.items()):
                if len(characters) != 1:
                    plan["skipped"].append({"owner_id": owner, "code": "ambiguous_character_binding"})
                    continue
                character_id, character = characters[0]
                try:
                    target = self._auto_target(source["group_openid"], owner)
                    resources = character.get("resources", {})
                    keys = ("hp", "max_hp", "san", "max_san")
                    if (not isinstance(character.get("name"), str)
                            or any(type(resources.get(key)) is not int or resources[key] < 0 for key in keys)):
                        raise OfficialAdminError("character_resources_invalid")
                    values = {"character_name": character["name"], "hp": resources["hp"], "hp_max": resources["max_hp"],
                              "san": resources["san"], "san_max": resources["max_san"]}
                    used_fields = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
                    if any(key in used_fields and hashlib.sha256(dumps(values.get(key)).encode()).hexdigest() == digest
                           for key, digest in protected.get(character_id, {}).items()):
                        raise OfficialAdminError("private_resource_value_blocked")
                    card = template.format_map(values)
                    if not card or len(card) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in card):
                        raise OfficialAdminError("generated_card_invalid")
                    try:
                        guard_outputs([Output(text=card)], state)
                    except Exception:
                        raise OfficialAdminError("private_card_content_blocked") from None
                    card_hash = hashlib.sha256(card.encode()).hexdigest()
                    metadata = {**target, "owner_id": owner, "character_id": character_id,
                        "character_version": character.get("version", 1), "resource_values": values,
                        "template_fields": sorted(used_fields),
                        "resource_version": hashlib.sha256(dumps(values).encode()).hexdigest(), "card_hash": card_hash}
                    prior = latest.get(target["target_qq_id"])
                    if prior and prior.get("card_hash") == card_hash and prior["status"] in {"sent", "unknown", "dispatching", "checking"}:
                        plan["skipped"].append({**metadata, "code": "card_unchanged" if prior["status"] == "sent" else "previous_card_unconfirmed"})
                        continue
                    dispatch_id = "auto-card:" + hashlib.sha256(dumps([ident, owner, character_id]).encode()).hexdigest()
                    plan["targets"].append({**metadata, "card": card, "request_id": dispatch_id})
                except OfficialAdminError as exc:
                    plan["skipped"].append({"owner_id": owner, "character_id": character_id, "code": exc.code})
        fingerprint = hashlib.sha256(dumps(plan).encode()).hexdigest()
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO official_admin_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (ident, source_id, self.api.app_id, source["group_openid"], source["user_openid"],
                 campaign["id"], "auto_card_plan", fingerprint, "planned", dumps({"plan": plan}), self.clock()))
            saved = db.execute("SELECT result FROM official_admin_actions WHERE request_id=?", (ident,)).fetchone()
        return ident, json.loads(saved["result"])["plan"]

    @staticmethod
    def _nickname_plan_id(app_id, campaign_id, request_id, change_request_id=None):
        identity = [app_id, campaign_id, request_id]
        if change_request_id is not None:
            identity.append(change_request_id)
        return "auto-plan:" + hashlib.sha256(dumps(identity).encode()).hexdigest()

    def prepare_nickname_sync(self, campaign_id, request_id, *, change_request_id=None):
        if nickname_sync_blocked(self.store):
            return {"prepared": False, "status": "blocked"}
        try:
            return self._prepare_nickname_sync(campaign_id, request_id, change_request_id)
        except Exception:
            record_nickname_preparation_failure(self.store, self.api.app_id, campaign_id, request_id, change_request_id)
            return {"prepared": False, "status": "blocked"}

    def _prepare_nickname_sync(self, campaign_id, request_id, change_request_id):
        """Freeze synchronously at the state-change hook, before scheduling.

        Holding the store lock prevents another local mutation between reading
        the state and persisting the plan/privacy barrier. Formal requests use
        their own committed event snapshot, even after a delayed recovery.
        This method never performs HTTP IO or waits for the management lock.
        """
        with self.store._lock:
            campaign = self.store.active(self.group_id)
            if campaign is None or campaign["id"] != campaign_id:
                return {"prepared": False, "status": "disabled"}
            plan_id = self._nickname_plan_id(self.api.app_id, campaign_id, request_id, change_request_id)
            existing = self.store.db.execute("SELECT result FROM official_admin_actions WHERE request_id=? AND action='auto_card_plan'",
                                              (plan_id,)).fetchone()
            if existing:
                verified = json.loads(existing[0]).get("plan", {}).get("synchronous_prepared") is True
                return {"prepared": verified, "plan_id": plan_id,
                        "status": "existing" if verified else "legacy_plan_unverified"}
            current_options = group_options(campaign["state"])
            enabled = current_options["group_admin_enabled"] is True and current_options["auto_nickname_enabled"] is True
            try:
                source = self._automatic_source(request_id)
            except OfficialAdminError:
                if not enabled:
                    return {"prepared": False, "status": "disabled"}
                raise
            event = self._source_event_snapshot(campaign_id, request_id, change_request_id)
            if change_request_id is not None and event is None:
                raise OfficialAdminError("automatic_nickname_change_source_missing")
            if event:
                campaign = {**campaign, "state": self.store.read_snapshot(event["snapshot"]), "version": event["state_version"]}
            if not enabled and not self._private_resource_source(campaign, request_id, source, change_request_id):
                return {"prepared": False, "status": "disabled"}
            ident, plan = self._nickname_plan(campaign, request_id, source, change_request_id)
            return {"prepared": True, "plan_id": ident,
                    "status": "private" if plan["private_source"] else "prepared"}

    def _save_auto_result(self, ident, result):
        self.store.db.execute("UPDATE official_admin_actions SET status=?,result=?,completed_at=? WHERE request_id=?",
                              (result["status"], dumps(result), self.clock(), ident))
        return result

    async def _automatic_card(self, campaign, source_id, source, target):
        ident = target["request_id"]
        metadata = {key: target[key] for key in ("target_qq_id", "official_member_id", "character_id",
                    "character_version", "resource_version", "card_hash")}
        result = {**metadata, "request_id": ident, "action": "auto_card", "status": "unknown", "code": "already_dispatched_or_in_progress"}
        def claim():
            with self.store.transaction() as db:
                previous = db.execute("SELECT result FROM official_admin_actions WHERE request_id=?", (ident,)).fetchone()
                if previous:
                    return json.loads(previous[0]) if previous[0] else result
                db.execute("INSERT INTO official_admin_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (ident, source_id, self.api.app_id, source["group_openid"], source["user_openid"], campaign["id"],
                     "auto_card", hashlib.sha256(dumps(target).encode()).hexdigest(), "checking", dumps(result), self.clock()))
        previous = await self.store.arun(claim)
        if previous is not None:
            return previous
        dispatched = False
        try:
            if nickname_sync_blocked(self.store):
                raise OfficialAdminError("automatic_nickname_preparation_failed")
            current = self.store.active(self.group_id)
            options = group_options(current["state"]) if current else {}
            if current is None or current["id"] != campaign["id"]:
                raise OfficialAdminError("management_campaign_changed")
            if not (options.get("auto_nickname_enabled") is True and options.get("group_admin_enabled") is True):
                raise OfficialAdminError("automatic_nickname_disabled")
            latest_mapping = self._auto_target(source["group_openid"], target["owner_id"])
            if any(latest_mapping[key] != target[key] for key in latest_mapping):
                raise OfficialAdminError("verified_card_mapping_changed")
            character = current["state"].get("characters", {}).get(target["character_id"])
            if not character or character.get("owner_id") != target["owner_id"]:
                raise OfficialAdminError("character_binding_changed")
            current_values = self._resource_values(character)
            protected = self._protected_resource_fields(campaign["id"])
            used_fields = target.get("template_fields", list(target["resource_values"]))
            if any(key in used_fields and hashlib.sha256(dumps(current_values.get(key)).encode()).hexdigest() == digest
                   for key, digest in protected.get(target["character_id"], {}).items()):
                raise OfficialAdminError("private_resource_value_blocked")
            try:
                guard_outputs([Output(text=target["card"])], current["state"])
            except Exception:
                raise OfficialAdminError("private_card_content_blocked") from None
            if self.bridge_url is None:
                raise OfficialAdminError("personal_card_bridge_unavailable")
            payload = {"request_id": ident, "group_id": self.group_id,
                       "target_qq_id": target["target_qq_id"], "card": target["card"]}
            if target["official_member_id"] is not None:
                payload["official_member_id"] = target["official_member_id"]
            await self.store.awrite("UPDATE official_admin_actions SET status='dispatching',dispatched_at=? WHERE request_id=?",
                                   (self.clock(), ident))
            dispatched = True
            await self._bridge_card(payload)
            result.update(status="sent", code="")
        except asyncio.CancelledError:
            await self.store.arun(self._save_auto_result, ident, {**result, "status": "unknown", "code": "interrupted_no_auto_retry"})
            raise
        except Exception as exc:
            known = isinstance(exc, OfficialAdminError)
            result.update(status="unknown" if (exc.unknown if known else dispatched) else "failed",
                          code=exc.code if known else "automatic_card_unconfirmed")
        return await self.store.arun(self._save_auto_result, ident, result)

    async def sync_nicknames(self, campaign_id, request_id, *, plan_id=None):
        """Configuration 14+17 authorize a frozen, mapped, privacy-checked sync.

        Existing plans retain their template/resources across restarts. An
        ambiguous bridge result remains immutable and is never automatically
        resent. Every new source event gets its own plan; unchanged cards skip.
        """
        empty = {"campaign_id": campaign_id, "official_nickname": {"status": "disabled", "request_id": request_id,
                 "results": [], "skipped": []}, "outputs": []}
        async with self.lock:
            if nickname_sync_blocked(self.store):
                return {**empty, "official_nickname": {**empty["official_nickname"], "status": "blocked"}}
            campaign = self.store.active(self.group_id)
            if campaign is None or campaign["id"] != campaign_id:
                return empty
            options = group_options(campaign["state"])
            if not (options["auto_nickname_enabled"] is True and options["group_admin_enabled"] is True):
                return empty
            plan_id = plan_id or self._nickname_plan_id(self.api.app_id, campaign_id, request_id)
            previous = self.store.db.execute("SELECT result FROM official_admin_actions WHERE request_id=? "
                "AND action='auto_card_plan' AND source_id=? AND campaign_id=? AND app_id=?",
                (plan_id, request_id, campaign_id, self.api.app_id)).fetchone()
            if previous is None:
                return {**empty, "official_nickname": {**empty["official_nickname"], "status": "unprepared"}}
            source = self._automatic_source(request_id)
            saved = json.loads(previous[0])
            plan = saved["plan"]
            if plan.get("synchronous_prepared") is not True:
                return {**empty, "official_nickname": {**empty["official_nickname"], "status": "unprepared"}}
            if "summary" in saved:
                return {"campaign_id": campaign_id, "official_nickname": saved["summary"], "outputs": []}
            results = []
            for target in plan["targets"]:
                results.append(await self._automatic_card(campaign, request_id, source, target))
            summary = {"status": "processed", "request_id": request_id,
                "source_state_version": plan["source_state_version"], "source_config_version": plan["source_config_version"],
                "results": results, "skipped": plan["skipped"]}
            problems = [(entry.get("owner_id", entry.get("target_qq_id", "")), entry.get("code", ""))
                        for entry in results+plan["skipped"] if entry.get("status") not in {"sent"}
                        and entry.get("code") != "card_unchanged"]
            notice_key = hashlib.sha256(dumps([plan["source_config_version"], sorted(problems)]).encode()).hexdigest() if problems else None
            duplicate_notice = False
            if notice_key:
                for prior in self.store.db.execute("SELECT result FROM official_admin_actions WHERE action='auto_card_plan' "
                        "AND campaign_id=? AND request_id<>? AND result IS NOT NULL ORDER BY created_at DESC", (campaign_id, plan_id)):
                    prior_result = json.loads(prior[0])
                    if prior_result.get("notice_key") == notice_key:
                        duplicate_notice = True
                        break
            await self.store.awrite("UPDATE official_admin_actions SET status='processed',result=?,completed_at=? WHERE request_id=?",
                (dumps({"plan": plan, "summary": summary, "notice_key": notice_key}), self.clock(), plan_id))
            outputs = []
            if notice_key and not duplicate_notice and not plan.get("private_source", True):
                labels = {"verified_card_mapping_required": "缺少已确认的QQ映射", "official_member_target_unbound": "缺少已确认的QQ映射",
                    "ambiguous_character_binding": "一个账号绑定多个角色", "private_card_content_blocked": "名片内容未通过公开检查",
                    "personal_card_bridge_unavailable": "名片桥未接通", "personal_card_bridge_unconfirmed": "名片桥结果未确认",
                    "previous_card_unconfirmed": "此前相同名片结果未确认", "nickname_template_invalid": "名片模板无效",
                    "private_resource_source_blocked": "本次资源来自私聊或暗骰，保留私密",
                    "private_resource_value_blocked": "模板涉及尚未公开的资源值"}
                reasons = "、".join(sorted({labels.get(code, "资料、权限或设置尚未通过核验") for _, code in problems}))
                text = "自动群名片：部分成员未更新（" + reasons + "）。已保留逐项记录；未猜测账号映射，未自动重发结果未知的操作。"
                outputs = [{"text": text, "visibility": {"scope": "public"}}]
            return {"campaign_id": campaign_id, "official_nickname": summary, "outputs": outputs}

    async def _bridge_card(self, payload):
        if self.bridge_url is None:
            raise OfficialAdminError("personal_card_bridge_unavailable")
        try:
            response = await self.bridge_client.post(self.bridge_url.rstrip("/") + "/v1/group-card",
                json=payload, headers={"Authorization": "Bearer " + self._bridge_token},
                timeout=15, follow_redirects=False)
            value = response.json()
        except Exception:
            raise OfficialAdminError("personal_card_bridge_unconfirmed", unknown=True) from None
        if (response.status_code != 200 or not isinstance(value, dict)
                or value.get("request_id") != payload["request_id"]
                or value.get("action") != "set_group_card" or value.get("group_id") != self.group_id
                or value.get("target_qq_id") != payload["target_qq_id"]
                or value.get("status") not in {"sent", "failed", "unknown"}):
            raise OfficialAdminError("personal_card_bridge_unconfirmed", unknown=True)
        if value["status"] != "sent":
            raise OfficialAdminError("personal_card_bridge_rejected" if value["status"] == "failed"
                                     else "personal_card_bridge_unconfirmed", unknown=value["status"] == "unknown")

    @staticmethod
    def _render(result, campaign=None):
        action = {"ban": "禁言", "unban": "解除禁言", "recall": "撤回", "card": "修改群名片"}.get(result["action"], "管理")
        if result["status"] == "sent":
            text = "群管理：" + action + "已执行。"
        elif result["status"] == "unknown":
            text = ("名片桥不可用或执行结果未确认。" if result["action"] == "card" else "官方群管理执行结果未确认。") + "为避免重复操作，未自动重发。"
        else:
            detail = {
                "signed_group_admin_required": "需要本人在群中以群主或管理员身份发起。",
                "group_admin_disabled": "请由群主先开启配置14（群管理总开关）。",
                "official_permission_unavailable": "官方平台尚未向此机器人开放所需权限；本次未调用私人账号。",
                "official_bot_admin_required": "请先将官方机器人设为本群管理员。",
                "personal_card_bridge_unavailable": "改群名片需要本机名片桥，目前尚未接通。",
                "verified_card_mapping_required": "该成员尚未关联已核实的 QQ 号。名片桥当前只允许修改已配置账号自己的名片。",
                "recall_same_group_recent_provenance_required": "仅可撤回本群已核验的普通成员消息或机器人消息，且须在发送后2分钟内。",
                "protected_or_unverified_target": "目标不是已核验的普通成员，未执行。",
                "official_member_target_unbound": "请指定本群已记录的成员标识或已绑定账号，未猜测QQ号或昵称。",
                "ban_duration_invalid": "禁言秒数超出本团配置上限，最长不能超过30天。",
            }.get(result.get("code"), "本次未能通过官方权限或参数校验，未执行。")
            text = "群管理：" + action + "未执行。" + detail
        output = {"official_admin": result, "outputs": [{"text": text, "visibility": {"scope": "public"}}]}
        if campaign is not None:
            output["campaign_id"] = campaign["id"]
        return output

    async def __call__(self, data, message):
        action, args = "", {}
        campaign = None
        claimed = False
        dispatched = False
        try:
            action, args = parse_command(message.text)
            if action not in {"ban", "unban", "recall", "card"}:
                raise OfficialAdminError("management_action_not_allowed")
            campaign = self._verified_source(data, message)
            async with self.lock:
                previous = await self.store.arun(self._claim, data, message, action, args, campaign)
                if previous is not None:
                    return self._render(previous, campaign)
                claimed = True
                options = group_options(campaign["state"])
                if options["group_admin_enabled"] is not True:
                    raise OfficialAdminError("group_admin_disabled")
                allowed = {"ban": {"user_id", "duration"}, "unban": {"user_id"},
                           "recall": {"message_id"}, "card": {"user_id", "card"}}[action]
                if set(args) != allowed:
                    raise OfficialAdminError("management_parameters_invalid")
                group = data["group_openid"]
                # Cards have no official API equivalent. Their bridge performs
                # its own fresh login/status/QQ-role checks, so a whitelist-only
                # official bot_state endpoint must not block that capability.
                bot = None
                if action != "card":
                    bot = await self._bot_state(group)
                    if action != "recall" and bot["member_role"] not in {"admin", "owner"}:
                        raise OfficialAdminError("official_bot_admin_required")
                if action in {"ban", "unban"}:
                    target = self._target_member(group, args["user_id"])
                    await self._ordinary_member(group, target, bot)
                    duration = args.get("duration", 0)
                    limit = options.get("max_ban_seconds")
                    if (type(limit) is not int or not 0 <= limit <= 2592000 or type(duration) is not int
                            or action == "ban" and not 1 <= duration <= min(2592000, limit)):
                        raise OfficialAdminError("ban_duration_invalid")
                    expiry = datetime.fromtimestamp(self.clock()+duration, timezone.utc).isoformat(timespec="seconds")
                    payload = {"members": [{"op": "del" if action == "unban" else "add",
                        "member_openid": target, "mute_expire_at": "" if action == "unban" else expiry}]}
                    path = self._group_path(group) + "/restrict_chat_setting"
                elif action == "recall":
                    target_kind = self._recall_provenance(group, args["message_id"])
                    if target_kind != "self" and bot["member_role"] not in {"admin", "owner"}:
                        raise OfficialAdminError("official_bot_admin_required")
                    path = self._group_path(group) + "/messages/" + quote(args["message_id"], safe="")
                    payload = None
                else:
                    payload = self._card_payload(message, group, args)
                    if self.bridge_url is None:
                        raise OfficialAdminError("personal_card_bridge_unavailable")
                def dispatch():
                    with self.store.transaction():
                        self._recheck_campaign(campaign, action, args.get("duration", 0))
                        self._dispatching(message)
                await self.store.arun(dispatch)
                dispatched = True
                if action == "card":
                    await self._bridge_card(payload)
                else:
                    await self._request("DELETE" if action == "recall" else "POST", path, payload=payload)
                result = {"request_id": message.message_id, "action": action, "status": "sent", "code": ""}
                if action == "card":
                    result.update(target_qq_id=payload["target_qq_id"], card_hash=hashlib.sha256(payload["card"].encode()).hexdigest())
                await self.store.arun(self._finish, message, result)
                return self._render(result, campaign)
        except asyncio.CancelledError:
            if claimed:
                await self.store.arun(self._finish, message, {"request_id": message.message_id, "action": action,
                    "status": "unknown", "code": "interrupted_no_auto_retry"})
            raise
        except Exception as exc:
            known = isinstance(exc, OfficialAdminError)
            code = exc.code if known else "management_parameters_invalid" if not claimed else "management_unconfirmed"
            unknown = exc.unknown if known else dispatched
            result = {"request_id": message.message_id, "action": action, "status": "unknown" if unknown else "failed", "code": code}
            if claimed:
                await self.store.arun(self._finish, message, result)
            return self._render(result, campaign)
