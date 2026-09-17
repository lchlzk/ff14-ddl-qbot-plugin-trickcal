"""Per-QQ-user Trickcal board tracking compatible with Soshage exports."""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
import hashlib
import json
import math
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from message_ui import help_panel, panel
from bot_tools import http_clients, media
from bot_tools.chinese_search import simplify_role_name
from bot_tools.request_cache import ResponseCache, singleflight
from bot_tools.storage import Identity, Store, ToolError
from .schema import ensure_board_schema


SOSHAGE_PAGE = "https://soshage.com/trickcal/collection#board"
SOSHAGE_API = "https://soshage.com/ztrickapi/zh-tw"
CRAYON_NOTE_REPOSITORY = "https://github.com/kai2002002-crayon/Crayon-note"
CRAYON_NOTE_CORE = (
    "https://api.github.com/repos/kai2002002-crayon/Crayon-note/contents/data_core.js?ref=main"
)
USER_AGENT = "NoneBot-QQ-Trickcal/1.5 (read-only public board catalogues)"
MAX_IMPORT_BYTES = 512 * 1024
MAX_ARRAY_ITEMS = 10_000
MAX_UNITS_BYTES = 1024 * 1024
MAX_BOARDS_BYTES = 4 * 1024 * 1024
MAX_CRAYON_NOTE_BYTES = 512 * 1024
CATALOG_TTL = 7 * 24 * 3600
CATALOG_REFRESH_RETRY = 10 * 60
IMPORT_SESSION_TTL = 10 * 60
CATALOG_VERSION = 5
GOLD_CRAYON_ITEM_ID = 610004
PERCENT_STATS = {88, 89, 92, 93, 95, 97, 99, 101, 103}
DOCUMENT_KEYS = {
    "version", "units", "cards", "pets", "boards", "filter", "stepFilter",
    "statFilter", "purpleWeight", "goldWeight",
}
BOARD_KEYS = {"selectedNodes", "plannedNodes"}
IMPORT_ALIASES = {"蜡笔板", "著色板", "board", "crayon"}
ACTION_ALIASES = {
    "": "overview", "总览": "overview", "查看": "overview", "status": "overview",
    "导入": "import", "import": "import",
    "角色": "unit", "使徒": "unit", "unit": "unit",
    "角色列表": "units", "使徒列表": "units", "units": "units",
    "未拥有": "unowned", "未擁有": "unowned", "未点亮": "unowned", "未點亮": "unowned",
    "点亮": "unlock", "點亮": "unlock", "获得": "unlock", "獲得": "unlock",
    "取消点亮": "remove_unit", "取消點亮": "remove_unit",
    "移除角色": "remove_unit", "反点": "remove_unit", "反點": "remove_unit",
    "加点": "mark", "加點": "mark", "mark": "mark",
    "撤销加点": "unmark", "撤銷加點": "unmark", "取消加点": "unmark",
    "取消加點": "unmark", "unmark": "unmark",
    "计划": "plan", "規劃": "plan", "plan": "plan",
    "未点": "missing", "未點": "missing", "缺口": "missing", "missing": "missing",
    "删除": "delete", "刪除": "delete", "delete": "delete",
    "网页": "web", "網頁": "web", "web": "web",
    "绑定": "bind", "綁定": "bind", "bind": "bind", "确认绑定": "bind",
    "帮助": "help", "幫助": "help", "help": "help",
}
PERCENT_NODE_GROUPS = {
    "攻击": ("百分比攻击", frozenset({88, 89})),
    "防御": ("百分比防御", frozenset({92, 93})),
    "生命": ("百分比血量", frozenset({95})),
    "暴击": ("百分比暴击/暴伤", frozenset({97, 101})),
    "暴抗": ("百分比暴抗/暴伤抗", frozenset({99, 103})),
}
PERCENT_GROUP_ALIASES = {
    "攻击": "攻击", "攻击力": "攻击", "百分比攻击": "攻击", "百分比攻击力": "攻击",
    "atk": "攻击", "物攻": "攻击", "魔攻": "攻击",
    "防御": "防御", "防御力": "防御", "百分比防御": "防御", "百分比防御力": "防御",
    "def": "防御", "物防": "防御", "魔防": "防御",
    "生命": "生命", "生命值": "生命", "血量": "生命", "百分比血量": "生命",
    "hp": "生命", "百分比hp": "生命",
    "暴击": "暴击", "暴击率": "暴击", "暴击/暴伤": "暴击",
    "百分比暴击": "暴击", "百分比暴击率": "暴击",
    "暴伤": "暴击", "暴击伤害": "暴击", "百分比暴击伤害": "暴击",
    "暴抗": "暴抗", "防爆": "暴抗", "暴抗/暴伤抗": "暴抗",
    "暴击抵抗": "暴抗", "百分比暴击抵抗": "暴抗",
    "暴伤抗": "暴抗", "暴击伤害抵抗": "暴抗", "百分比暴击伤害抵抗": "暴抗",
}
LAYER_NAMES = {1: "第一层", 2: "第二层", 3: "第三层"}
LAYER_ALIASES = {
    "1": 1, "1层": 1, "第1层": 1, "一层": 1, "第一层": 1, "一": 1,
    "2": 2, "2层": 2, "第2层": 2, "二层": 2, "第二层": 2, "二": 2,
    "3": 3, "3层": 3, "第3层": 3, "三层": 3, "第三层": 3, "三": 3,
}
BULK_UNIT_ALIASES = {"全部", "所有", "全角色", "所有角色", "all"}
ALL_PERCENT_ALIASES = {"全部", "所有", "全属性", "所有属性", "全部属性", "all"}
MAX_BATCH_UNITS = 100
CATALOG_TIMEZONE = timezone(timedelta(hours=8))
CATALOG_REFRESH_STATE = "system:trickcal-board-catalog-refresh"
CRAYON_NOTE_UNIT_BASE = 9_000_000_000
CRAYON_NOTE_NODE_BASE = 90_000_000_000
CRAYON_NOTE_PERSONALITIES = {
    "天真": 0, "冷静": 1, "冷靜": 1, "狂乱": 2, "狂亂": 2,
    "活泼": 3, "活潑": 3, "忧郁": 4, "憂鬱": 4,
}
CRAYON_NOTE_ATTRIBUTES = {
    "攻擊": (89, 88), "攻击": (89, 88),
    "防禦": (93, 92), "防御": (93, 92),
    "血量": (95, 0),
    "爆擊": (97, 101), "暴击": (97, 101),
    "爆抗": (99, 103), "暴抗": (99, 103),
}
STAT_LABELS = {
    1: "HP", 2: "SP", 3: "物理攻击", 4: "魔法攻击", 5: "物理防御",
    6: "魔法防御", 7: "暴击率", 8: "暴击伤害", 9: "暴击抵抗",
    10: "暴击伤害抵抗", 11: "SP恢复", 12: "攻击速度", 13: "治疗量",
    86: "全体物理攻击", 87: "全体魔法攻击", 88: "全体物理攻击",
    89: "全体魔法攻击", 90: "全体物理防御", 91: "全体魔法防御",
    92: "全体物理防御", 93: "全体魔法防御", 94: "全体HP",
    95: "全体HP", 96: "全体暴击率", 97: "全体暴击率",
    98: "全体暴击抵抗", 99: "全体暴击抵抗", 100: "全体暴击伤害",
    101: "全体暴击伤害", 102: "全体暴击伤害抵抗",
    103: "全体暴击伤害抵抗",
}
_catalog_cache = ResponseCache(max_bytes=48 * 1024 * 1024, max_entries=4)
_catalog_expiry: dict[tuple[int, str], float] = {}


class BoardCatalogError(ToolError):
    """The public board catalogue is temporarily unavailable."""


@dataclass(frozen=True)
class Catalog:
    units: dict[int, dict[str, Any]]
    nodes: dict[int, dict[str, Any]]
    by_unit: dict[int, tuple[int, ...]]
    fetched: float
    stale: bool = False
    sources: tuple[str, ...] = ("Soshage",)
    unavailable_units: frozenset[int] = frozenset()


@dataclass(frozen=True)
class BoardRef:
    """A web-session-safe board owner reference containing no raw QQ OpenID."""

    owner: str
    bot: str


class PaginatedReply(str):
    """A normal text reply carrying optional QQ pagination commands."""

    page: int
    pages: int
    previous_command: str | None
    next_command: str | None

    def __new__(
        cls, content: str, *, base_command: str, page: int, total: int,
        page_size: int = 8,
    ) -> "PaginatedReply":
        result = super().__new__(cls, content)
        result.page = page
        result.pages = max(1, math.ceil(total / page_size))
        result.previous_command = (
            f"{base_command} {page - 1}" if page > 1 else None
        )
        result.next_command = (
            f"{base_command} {page + 1}" if page < result.pages else None
        )
        return result


@dataclass(frozen=True)
class ReplyAction:
    label: str
    command: str
    style: int = 1


class ActionReply(str):
    """A text reply carrying sender-only QQ command buttons."""

    actions: tuple[ReplyAction, ...]

    def __new__(cls, content: str, actions: list[ReplyAction]) -> "ActionReply":
        result = super().__new__(cls, content)
        result.actions = tuple(actions)
        return result


def is_board_command(raw: str) -> bool:
    first = raw.strip().split(maxsplit=1)[0].casefold() if raw.strip() else ""
    return first in IMPORT_ALIASES


def _positive_int(value: Any, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and value > 0
        and value <= (maximum if maximum is not None else 2**53 - 1)
    )


def _unique_positive(values: Any) -> list[int] | None:
    if not isinstance(values, list) or len(values) > MAX_ARRAY_ITEMS:
        return None
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        if not _positive_int(value) or value in seen:
            return None
        seen.add(value)
        result.append(value)
    return result


def _pair_map(values: Any) -> list[list[int]] | None:
    if not isinstance(values, list) or len(values) > MAX_ARRAY_ITEMS:
        return None
    result: list[list[int]] = []
    seen: set[int] = set()
    for row in values:
        if (
            not isinstance(row, list) or len(row) != 2 or not _positive_int(row[0])
            or row[0] in seen or not _positive_int(row[1], 5)
        ):
            return None
        seen.add(row[0])
        result.append([row[0], row[1]])
    return result


def validate_export(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ToolError("导入文件不能超过 512 KiB。")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ToolError("无法识别这份 JSON；请使用 Soshage 收藏页的“导出”文件。") from None
    if not isinstance(value, dict) or set(value) != DOCUMENT_KEYS or value.get("version") != 1:
        raise ToolError("不支持这份导出格式；目前仅兼容 Soshage collection v1。")

    units = _pair_map(value.get("units"))
    cards = _pair_map(value.get("cards"))
    pets = _unique_positive(value.get("pets"))
    filters = _unique_positive(value.get("filter"))
    step_filters = _unique_positive(value.get("stepFilter"))
    stat_filters = value.get("statFilter")
    if any(value is None for value in (units, cards, pets, filters, step_filters)):
        raise ToolError("导出文件中的收藏或筛选字段不正确。")
    if (
        not isinstance(stat_filters, list) or len(stat_filters) > MAX_ARRAY_ITEMS
        or any(not isinstance(v, str) or len(v.encode("utf-8")) > 255 for v in stat_filters)
    ):
        raise ToolError("导出文件中的属性筛选字段不正确。")
    if not _positive_int(value.get("purpleWeight")) or not _positive_int(value.get("goldWeight")):
        raise ToolError("导出文件中的权重字段不正确。")

    raw_boards = value.get("boards")
    if not isinstance(raw_boards, list) or len(raw_boards) > MAX_ARRAY_ITEMS:
        raise ToolError("导出文件中的蜡笔板数量不正确。")
    boards: list[list[Any]] = []
    seen_units: set[int] = set()
    for row in raw_boards:
        if (
            not isinstance(row, list) or len(row) != 2 or not _positive_int(row[0])
            or row[0] in seen_units or not isinstance(row[1], dict)
            or set(row[1]) != BOARD_KEYS
        ):
            raise ToolError("导出文件中的蜡笔板记录不正确。")
        selected = _unique_positive(row[1].get("selectedNodes"))
        planned = _unique_positive(row[1].get("plannedNodes"))
        if selected is None or planned is None or set(selected) & set(planned):
            raise ToolError("蜡笔节点必须是唯一正整数，且不能同时属于已点和计划。")
        seen_units.add(row[0])
        boards.append([row[0], {
            "selectedNodes": sorted(selected),
            "plannedNodes": sorted(planned),
        }])

    # Only the fields needed for board ownership and summaries are retained.
    return {
        "version": 1,
        "units": sorted(units, key=lambda row: row[0]),
        "boards": sorted(boards, key=lambda row: row[0]),
    }


def _owner(who: Identity | BoardRef) -> str:
    if isinstance(who, BoardRef):
        if not re.fullmatch(r"[a-f0-9]{64}", who.owner):
            raise ValueError("invalid board owner reference")
        return who.owner
    return hashlib.sha256(
        json.dumps(["trickcal:shared-user:v2", who.user]).encode()
    ).hexdigest()


def _legacy_owner(who: Identity) -> str:
    return hashlib.sha256(json.dumps([who.bot, who.user]).encode()).hexdigest()


def _resolve_owner_alias(db, owner: str) -> str:
    """Resolve cross-bot board identities without allowing alias cycles."""
    current = owner
    seen: set[str] = set()
    for _ in range(8):
        if current in seen:
            break
        seen.add(current)
        row = db.execute(
            "SELECT owner FROM trickcal_board_owner_alias WHERE alias=?", (current,)
        ).fetchone()
        if row is None:
            return current
        target = str(row["owner"])
        if not re.fullmatch(r"[a-f0-9]{64}", target) or target == current:
            return current
        current = target
    return current


def _storage_owner(db, who: Identity | BoardRef) -> str:
    """Resolve a shared owner and lazily retain access from older web sessions."""
    raw_owner = _owner(who)
    owner = _resolve_owner_alias(db, raw_owner)
    if isinstance(who, BoardRef):
        return owner
    legacy = _legacy_owner(who)
    if legacy == owner:
        return owner
    db.execute(
        "INSERT OR REPLACE INTO trickcal_board_owner_alias(alias,owner) VALUES(?,?)",
        (legacy, owner),
    )
    old = db.execute(
        "SELECT payload,updated FROM trickcal_board_progress WHERE owner=?", (legacy,)
    ).fetchone()
    current = db.execute(
        "SELECT updated FROM trickcal_board_progress WHERE owner=?", (owner,)
    ).fetchone()
    if old and (current is None or float(old["updated"]) > float(current["updated"])):
        db.execute(
            "INSERT OR REPLACE INTO trickcal_board_progress(owner,bot,payload,updated) "
            "VALUES(?,?,?,?)",
            (owner, who.bot, old["payload"], old["updated"]),
        )
    return owner


def import_session_key(who: Identity) -> tuple[str, str, str]:
    return _owner(who), who.bot, who.scope_key


class BoardStore:
    def __init__(self, store: Store):
        self.store = store
        ensure_board_schema(store)

    def replace(self, who: Identity, raw: str, now: float | None = None) -> dict[str, Any]:
        document = validate_export(raw)
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            db.execute(
                "INSERT OR REPLACE INTO trickcal_board_progress(owner,bot,payload,updated) VALUES(?,?,?,?)",
                (owner, who.bot, payload, time.time() if now is None else now),
            )
        return document

    def get(self, who: Identity | BoardRef) -> tuple[dict[str, Any], float] | None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            row = db.execute(
                "SELECT payload,updated FROM trickcal_board_progress WHERE owner=?",
                (owner,),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            raise ToolError("保存的蜡笔板数据已损坏，请重新导入。") from None
        return payload, float(row["updated"])

    @staticmethod
    def _saved_payload(document: dict[str, Any]) -> str:
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"))

    def add_unit(self, who: Identity, unit_uid: int, now: float | None = None) -> bool:
        """Add one owned character without requiring a new browser export."""
        added, _owned = self.add_units(who, [unit_uid], now=now)
        return bool(added)

    def add_units(
        self, who: Identity, unit_uids: list[int], now: float | None = None,
    ) -> tuple[int, int]:
        """Add multiple owned characters in one transaction without touching nodes."""
        updated = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            row = db.execute(
                "SELECT payload FROM trickcal_board_progress WHERE owner=?",
                (owner,),
            ).fetchone()
            document = json.loads(row["payload"]) if row else {
                "version": 1, "units": [], "boards": [],
            }
            owned = {existing_uid for existing_uid, _rarity in document["units"]}
            additions = sorted(set(unit_uids) - owned)
            # Rarity is not used by the tracker.  Keep a valid placeholder so
            # the normalized collection format remains compatible with v1.
            document["units"].extend([unit_uid, 1] for unit_uid in additions)
            document["units"].sort(key=lambda item: item[0])
            if additions:
                db.execute(
                    "INSERT OR REPLACE INTO trickcal_board_progress(owner,bot,payload,updated) VALUES(?,?,?,?)",
                    (owner, who.bot, self._saved_payload(document), updated),
                )
        return len(additions), len(document["units"])

    def remove_units(
        self, who: Identity, unit_uids: list[int], now: float | None = None,
    ) -> tuple[int, int]:
        """Remove owned characters and all of their selected/planned nodes atomically."""
        updated = time.time() if now is None else now
        targets = set(unit_uids)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            row = db.execute(
                "SELECT payload FROM trickcal_board_progress WHERE owner=?",
                (owner,),
            ).fetchone()
            if row is None:
                raise ToolError("你还没有蜡笔板记录。")
            try:
                document = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                raise ToolError("保存的蜡笔板数据已损坏，请重新导入。") from None
            owned = {unit_uid for unit_uid, _rarity in document["units"]}
            removed = owned & targets
            if removed:
                document["units"] = [
                    item for item in document["units"] if item[0] not in removed
                ]
                document["boards"] = [
                    item for item in document["boards"] if item[0] not in removed
                ]
                db.execute(
                    "UPDATE trickcal_board_progress SET payload=?,updated=?,bot=? WHERE owner=?",
                    (self._saved_payload(document), updated, who.bot, owner),
                )
        return len(removed), len(document["units"])

    def set_selected_nodes(
        self,
        who: Identity,
        unit_uid: int,
        node_ids: list[int],
        *,
        selected: bool,
        now: float | None = None,
    ) -> int:
        """Select or unselect public node IDs in one atomic user-owned update."""
        changed, _roles = self.set_selected_nodes_bulk(
            who, {unit_uid: node_ids}, selected=selected, now=now,
        )
        return changed

    def set_selected_nodes_bulk(
        self,
        who: Identity,
        selections: dict[int, list[int]],
        *,
        selected: bool,
        now: float | None = None,
    ) -> tuple[int, int]:
        """Select or unselect nodes for multiple owned characters in one transaction."""
        updated = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            row = db.execute(
                "SELECT payload FROM trickcal_board_progress WHERE owner=?",
                (owner,),
            ).fetchone()
            if row is None:
                raise ToolError("你还没有蜡笔板记录，请先导入 JSON 或点亮一个角色。")
            try:
                document = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                raise ToolError("保存的蜡笔板数据已损坏，请重新导入。") from None
            owned = {unit_uid for unit_uid, _ in document["units"]}
            if not set(selections).issubset(owned):
                raise ToolError("批量加点中包含尚未点亮的角色。")
            board_by_unit = {uid: board for uid, board in document["boards"]}
            changed = 0
            changed_roles = 0
            for unit_uid, node_ids in selections.items():
                board = board_by_unit.get(unit_uid, {"selectedNodes": [], "plannedNodes": []})
                selected_nodes = set(board["selectedNodes"])
                planned_nodes = set(board["plannedNodes"])
                before = len(selected_nodes)
                if selected:
                    selected_nodes.update(node_ids)
                    planned_nodes.difference_update(node_ids)
                else:
                    selected_nodes.difference_update(node_ids)
                unit_changed = abs(len(selected_nodes) - before)
                changed += unit_changed
                changed_roles += int(bool(unit_changed))
                board_by_unit[unit_uid] = {
                    "selectedNodes": sorted(selected_nodes),
                    "plannedNodes": sorted(planned_nodes),
                }
            document["boards"] = [
                [uid, board_value]
                for uid, board_value in sorted(board_by_unit.items())
                if board_value["selectedNodes"] or board_value["plannedNodes"]
            ]
            if changed:
                db.execute(
                    "UPDATE trickcal_board_progress SET payload=?,updated=?,bot=? WHERE owner=?",
                    (self._saved_payload(document), updated, who.bot, owner),
                )
        return changed, changed_roles

    def delete(self, who: Identity) -> bool:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            return bool(db.execute(
                "DELETE FROM trickcal_board_progress WHERE owner=?",
                (owner,),
            ).rowcount)

    def begin_import(self, who: Identity, now: float | None = None) -> float:
        now = time.time() if now is None else now
        expires = now + IMPORT_SESSION_TTL
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            db.execute("DELETE FROM trickcal_board_import_sessions WHERE expires<=?", (now,))
            db.execute(
                """INSERT OR REPLACE INTO trickcal_board_import_sessions
                   (owner,bot,scope,token,expires,claimed) VALUES(?,?,?,?,?,0)""",
                (owner, who.bot, who.scope_key, secrets.token_hex(16), expires),
            )
        return expires

    def pending_import(self, who: Identity, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            row = db.execute(
                """SELECT 1 FROM trickcal_board_import_sessions
                   WHERE owner=? AND bot=? AND scope=? AND expires>? AND claimed=0""",
                (owner, who.bot, who.scope_key, now),
            ).fetchone()
        return row is not None

    def active_imports(self, now: float | None = None) -> list[tuple[str, str, str, float]]:
        """Return unclaimed sessions for rebuilding the in-process event index."""
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM trickcal_board_import_sessions WHERE expires<=?", (now,))
            rows = db.execute(
                """SELECT owner,bot,scope,expires FROM trickcal_board_import_sessions
                   WHERE expires>? AND claimed=0""",
                (now,),
            ).fetchall()
        return [
            (str(row["owner"]), str(row["bot"]), str(row["scope"]), float(row["expires"]))
            for row in rows
        ]

    def claim_import(self, who: Identity, now: float | None = None) -> str | None:
        """Atomically claim one pending upload so duplicate QQ events cannot import twice."""
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            db.execute(
                "DELETE FROM trickcal_board_import_sessions WHERE owner=? AND bot=? AND expires<=?",
                (owner, who.bot, now),
            )
            row = db.execute(
                """SELECT token FROM trickcal_board_import_sessions
                   WHERE owner=? AND bot=? AND scope=? AND expires>? AND claimed=0""",
                (owner, who.bot, who.scope_key, now),
            ).fetchone()
            if row is None:
                return None
            changed = db.execute(
                """UPDATE trickcal_board_import_sessions SET claimed=1
                   WHERE owner=? AND bot=? AND scope=? AND token=? AND claimed=0""",
                (owner, who.bot, who.scope_key, row["token"]),
            ).rowcount
        return str(row["token"]) if changed else None

    def rearm_import(self, who: Identity, token: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            db.execute(
                """UPDATE trickcal_board_import_sessions SET claimed=0
                   WHERE owner=? AND bot=? AND scope=? AND token=? AND expires>?""",
                (owner, who.bot, who.scope_key, token, now),
            )

    def finish_import(self, who: Identity, token: str) -> None:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            db.execute(
                """DELETE FROM trickcal_board_import_sessions
                   WHERE owner=? AND bot=? AND scope=? AND token=?""",
                (owner, who.bot, who.scope_key, token),
            )

    def cancel_import(self, who: Identity) -> bool:
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = _storage_owner(db, who)
            return bool(db.execute(
                """DELETE FROM trickcal_board_import_sessions
                   WHERE owner=? AND bot=? AND scope=?""",
                (owner, who.bot, who.scope_key),
            ).rowcount)


def _cache_path() -> Path:
    return Path(os.environ.get("BOT_DATA_DIR", "data")) / "cache" / "trickcal-board" / f"catalog-v{CATALOG_VERSION}.json"


def _balanced_js_value(source: str, start: int, opening: str, closing: str) -> tuple[str, int]:
    """Read one balanced JS array/object without executing the remote source."""
    if start >= len(source) or source[start] != opening:
        raise BoardCatalogError("Crayon-note 资料格式暂时无法识别。")
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = start
    while index < len(source):
        char = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
        elif block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {'"', "'", "`"}:
            quote = char
        elif char == "/" and following == "/":
            line_comment = True
            index += 1
        elif char == "/" and following == "*":
            block_comment = True
            index += 1
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return source[start + 1:index], index + 1
        index += 1
    raise BoardCatalogError("Crayon-note 资料不完整，已忽略本次更新。")


def _js_assignment(source: str, name: str, opening: str, closing: str) -> str:
    match = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*{re.escape(opening)}", source)
    if match is None:
        raise BoardCatalogError(f"Crayon-note 缺少 {name}。")
    body, _end = _balanced_js_value(source, match.end() - 1, opening, closing)
    return body


def _js_string(value: str) -> str:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        raise BoardCatalogError("Crayon-note 含有无法识别的字符串。") from None
    if not isinstance(decoded, str):
        raise BoardCatalogError("Crayon-note 字符串字段格式不正确。")
    return decoded


def _js_string_property(source: str, name: str, *, required: bool = True) -> str:
    match = re.search(
        rf"(?<![\w$]){re.escape(name)}\s*:\s*(\"(?:\\.|[^\"\\])*\")",
        source,
    )
    if match is None:
        if required:
            raise BoardCatalogError(f"Crayon-note 角色缺少 {name}。")
        return ""
    return _js_string(match.group(1)).strip()


def _js_named_objects(source: str) -> list[tuple[str, str]]:
    """Return the top-level quoted-key object values from an object body."""
    result: list[tuple[str, str]] = []
    position = 0
    pattern = re.compile(r"\"((?:\\.|[^\"\\])*)\"\s*:\s*\{")
    while True:
        match = pattern.search(source, position)
        if match is None:
            break
        body, end = _balanced_js_value(source, match.end() - 1, "{", "}")
        result.append((_js_string('"' + match.group(1) + '"'), body))
        position = end
    return result


def _js_string_array_property(source: str, name: str) -> tuple[str, ...]:
    match = re.search(rf"(?<![\w$]){re.escape(name)}\s*:\s*\[", source)
    if match is None:
        raise BoardCatalogError(f"Crayon-note 路径缺少 {name}。")
    body, _end = _balanced_js_value(source, match.end() - 1, "[", "]")
    strings = tuple(
        _js_string(item.group(0)).strip()
        for item in re.finditer(r'\"(?:\\.|[^\"\\])*\"', body)
    )
    if not strings or re.sub(r'\"(?:\\.|[^\"\\])*\"|[\s,]', "", body):
        raise BoardCatalogError(f"Crayon-note 的 {name} 节点格式不正确。")
    return strings


def _crayon_resource_names(source: str) -> dict[str, str]:
    language_body = _js_assignment(source, "LANG_DICT", "{", "}")
    english = next((body for key, body in _js_named_objects(language_body) if key == "en"), None)
    if english is None:
        raise BoardCatalogError("Crayon-note 缺少英文角色名对照。")
    result: dict[str, str] = {}
    for match in re.finditer(
        r'(\"(?:\\.|[^\"\\])*\")\s*:\s*(\"(?:\\.|[^\"\\])*\")', english
    ):
        key, value = _js_string(match.group(1)).strip(), _js_string(match.group(2)).strip()
        if key and re.fullmatch(r"[A-Za-z0-9 ._-]{1,80}", value):
            result[key] = re.sub(r"[ .]+", "", value)
    return result


def _crayon_layer_rules(source: str) -> dict[int, tuple[int, int]]:
    """Return layer -> (percentage bonus, gold-crayon cost) from upstream labels."""
    rules: dict[int, tuple[int, int]] = {}
    pattern = re.compile(
        r"stats_layer_([123])_rule\s*:\s*\"\(Node\+(\d{1,2})%,\s*Crayon[×x](\d{1,2})\)\""
    )
    for layer, bonus, crayons in pattern.findall(source):
        rules[int(layer)] = (int(bonus), int(crayons))
    if set(rules) != {1, 2, 3}:
        raise BoardCatalogError("Crayon-note 缺少完整的层级加成规则。")
    return rules


def _parse_crayon_note(source: str, *, minimum_characters: int = 50) -> tuple[list[dict[str, Any]], dict[int, tuple[int, int]]]:
    """Parse the small declarative subset of data_core.js used by the board."""
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_CRAYON_NOTE_BYTES:
        raise BoardCatalogError("Crayon-note 资料大小不正确。")
    resources = _crayon_resource_names(source)
    paths: dict[tuple[str, str], dict[int, tuple[str, ...]]] = {}
    path_body = _js_assignment(source, "CRAYON_PATH_CONFIG", "{", "}")
    for race, race_body in _js_named_objects(path_body):
        for version, version_body in _js_named_objects(race_body):
            layers = {
                layer: _js_string_array_property(version_body, f"layer{layer}")
                for layer in (1, 2, 3)
            }
            if any(
                len(values) != layer + 1 or len(set(values)) != len(values)
                or any(value not in CRAYON_NOTE_ATTRIBUTES for value in values)
                for layer, values in layers.items()
            ):
                raise BoardCatalogError("Crayon-note 路径节点数量或属性不正确。")
            paths[(race, version)] = layers

    characters: list[dict[str, Any]] = []
    names: set[str] = set()
    initial_body = _js_assignment(source, "INITIAL_DATA", "[", "]")
    for match in re.finditer(r"\{([^{}]{1,1000})\}", initial_body):
        row = match.group(1)
        name = _js_string_property(row, "name")
        personality_name = _js_string_property(row, "personality")
        race = _js_string_property(row, "race")
        version = _js_string_property(row, "pathVersion")
        release_date = _js_string_property(row, "releaseDate", required=False)
        if not name or name in names or len(name) > 60:
            raise BoardCatalogError("Crayon-note 角色名称重复或不正确。")
        if personality_name not in CRAYON_NOTE_PERSONALITIES or (race, version) not in paths:
            raise BoardCatalogError(f"Crayon-note 角色“{name}”的路径资料不完整。")
        if release_date:
            try:
                datetime.fromisoformat(release_date)
            except ValueError:
                raise BoardCatalogError(f"Crayon-note 角色“{name}”的发布日期不正确。") from None
        names.add(name)
        characters.append({
            "name": name,
            "alias": resources.get(name, ""),
            "personality": CRAYON_NOTE_PERSONALITIES[personality_name],
            "layers": paths[(race, version)],
            "release_date": release_date,
        })
    if not minimum_characters <= len(characters) <= 300:
        raise BoardCatalogError("Crayon-note 角色目录数量异常，已忽略本次更新。")
    return characters, _crayon_layer_rules(source)


def _stable_crayon_id(base: int, *parts: object) -> int:
    digest = hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).digest()
    return base + int.from_bytes(digest[:4], "big")


def _soshage_layer_gold(nodes: list[list[Any]]) -> dict[int, int]:
    grouped: dict[int, Counter[int]] = {1: Counter(), 2: Counter(), 3: Counter()}
    for row in nodes:
        if row[2] in grouped and _node_stat_types({"stat_type": row[4]}) and row[6] > 0:
            grouped[row[2]][row[6]] += 1
    return {
        layer: values.most_common(1)[0][0] if values else 0
        for layer, values in grouped.items()
    }


def _merge_crayon_note(
    payload: dict[str, Any], source: str, *, now: float | None = None,
    minimum_characters: int = 50,
) -> dict[str, Any]:
    """Add missing Crayon-note roles/nodes while preserving precise Soshage rows."""
    characters, rules = _parse_crayon_note(source, minimum_characters=minimum_characters)
    now = time.time() if now is None else now
    units = payload["units"]
    nodes = payload["nodes"]
    unavailable = set(payload["unavailable_units"])
    units_by_id = {row[0]: row for row in units}
    alias_index: dict[str, set[int]] = {}
    name_index: dict[str, set[int]] = {}
    for row in units:
        if row[2]:
            alias_index.setdefault(_normal(row[2]), set()).add(row[0])
        if row[1]:
            name_index.setdefault(_normal(row[1]), set()).add(row[0])
    nodes_by_unit: dict[int, list[list[Any]]] = {}
    for row in nodes:
        nodes_by_unit.setdefault(row[1], []).append(row)
    layer_gold = _soshage_layer_gold(nodes)
    used_ids = set(units_by_id) | {row[0] for row in nodes}

    for character in characters:
        if character["release_date"]:
            if datetime.fromisoformat(character["release_date"]).timestamp() > now:
                continue
        candidates = set()
        if character["alias"]:
            candidates.update(alias_index.get(_normal(character["alias"]), ()))
        candidates.update(name_index.get(_normal(character["name"]), ()))
        # A supplemental source must not override Soshage's explicit availability.
        # Keep the hidden rows in the cache so both IDs and aliases remain known.
        if candidates & unavailable:
            continue
        if len(candidates) > 1:
            raise BoardCatalogError(
                f"Crayon-note 角色“{character['name']}”对应多个 Soshage 角色，已忽略本次更新。"
            )
        if candidates:
            unit_uid = next(iter(candidates))
        else:
            stable_name = character["alias"] or character["name"]
            unit_uid = _stable_crayon_id(CRAYON_NOTE_UNIT_BASE, "unit", stable_name)
            if unit_uid in used_ids:
                raise BoardCatalogError("Crayon-note 角色 ID 发生冲突，已忽略本次更新。")
            used_ids.add(unit_uid)
            unit_row = [
                unit_uid, character["name"][:60], character["alias"][:60],
                character["personality"],
            ]
            units.append(unit_row)
            units_by_id[unit_uid] = unit_row
            nodes_by_unit[unit_uid] = []

        current_nodes = nodes_by_unit.setdefault(unit_uid, [])
        for layer, attributes in character["layers"].items():
            bonus, crayons = rules[layer]
            for index, attribute in enumerate(attributes):
                stat_types = CRAYON_NOTE_ATTRIBUTES[attribute]
                target_types = set(stat_types) & PERCENT_STATS
                if any(
                    row[2] == layer
                    and _node_stat_types({"stat_type": row[4]}) & target_types
                    for row in current_nodes
                ):
                    continue
                stable_name = character["alias"] or character["name"]
                node_uid = _stable_crayon_id(
                    CRAYON_NOTE_NODE_BASE, "node", stable_name, layer, attribute, index,
                )
                if node_uid in used_ids:
                    raise BoardCatalogError("Crayon-note 节点 ID 发生冲突，已忽略本次更新。")
                used_ids.add(node_uid)
                values = [str(bonus * 10) if stat_type else "0" for stat_type in stat_types]
                node_row = [
                    node_uid, unit_uid, layer, 3,
                    ",".join(str(value) for value in stat_types), ",".join(values),
                    layer_gold[layer], crayons,
                ]
                nodes.append(node_row)
                current_nodes.append(node_row)
    payload["sources"] = ["Soshage", "Crayon-note"]
    return payload


def _catalog_from_payload(value: Any, *, stale: bool = False) -> Catalog | None:
    if not isinstance(value, dict) or value.get("version") != CATALOG_VERSION:
        return None
    fetched = value.get("fetched")
    raw_units, raw_nodes = value.get("units"), value.get("nodes")
    if not isinstance(fetched, (int, float)) or not isinstance(raw_units, list) or not isinstance(raw_nodes, list):
        return None
    unavailable_ids = _unique_positive(value.get("unavailable_units"))
    if unavailable_ids is None:
        return None
    unavailable = set(unavailable_ids)
    units: dict[int, dict[str, Any]] = {}
    for row in raw_units:
        if not isinstance(row, list) or len(row) != 4 or not _positive_int(row[0]):
            return None
        name, alias, personality = row[1], row[2], row[3]
        if (
            not isinstance(name, str) or not isinstance(alias, str)
            or type(personality) is not int or personality not in {-1, 0, 1, 2, 3, 4}
        ):
            return None
        units[row[0]] = {
            "name": name[:60], "alias": alias[:60], "personality": personality,
        }
    if not unavailable.issubset(units):
        return None
    units = {uid: row for uid, row in units.items() if uid not in unavailable}
    nodes: dict[int, dict[str, Any]] = {}
    by_unit: dict[int, list[int]] = {}
    for row in raw_nodes:
        if not isinstance(row, list) or len(row) != 8:
            return None
        uid, unit_uid, step, node_type, stat_type, stat_value, need_gold, gold_crayons = row
        if not all(_positive_int(v) for v in (uid, unit_uid, step)) or not isinstance(node_type, int):
            return None
        if (
            not isinstance(stat_type, str) or not isinstance(stat_value, str)
            or not isinstance(need_gold, int) or not isinstance(gold_crayons, int)
        ):
            return None
        if unit_uid not in units:
            continue
        nodes[uid] = {
            "unit": unit_uid, "step": step, "type": node_type,
            "stat_type": stat_type, "stat_value": stat_value, "gold": max(0, need_gold),
            "gold_crayons": max(0, gold_crayons),
        }
        by_unit.setdefault(unit_uid, []).append(uid)
    raw_sources = value.get("sources", ["Soshage"])
    if (
        not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= 4
        or any(not isinstance(source, str) or not source or len(source) > 40 for source in raw_sources)
    ):
        return None
    sources = tuple(dict.fromkeys(raw_sources))
    return Catalog(
        units, nodes, {key: tuple(value) for key, value in by_unit.items()},
        float(fetched), stale, sources, frozenset(unavailable),
    )


def _read_disk_catalog(*, allow_stale: bool = False) -> Catalog | None:
    path = _cache_path()
    try:
        if not allow_stale and time.time() - path.stat().st_mtime > CATALOG_TTL:
            return None
        if path.stat().st_size > MAX_BOARDS_BYTES:
            return None
        return _catalog_from_payload(json.loads(path.read_text("utf-8")), stale=allow_stale)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_disk_catalog(value: dict[str, Any]) -> None:
    path = _cache_path()
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(data.encode("utf-8")) > MAX_BOARDS_BYTES:
            return
        temporary.write_text(data, "utf-8")
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def _get_json(path: str, maximum: int, transport=None) -> Any:
    if path not in {"/unit", "/unit/board"}:
        raise ValueError("unsupported Soshage API path")
    options: dict[str, Any] = {
        "timeout": httpx.Timeout(40, connect=8), "follow_redirects": False, "trust_env": False,
    }
    if transport is not None:
        options["transport"] = transport
    try:
        async with http_clients.client("trickcal-soshage", **options) as client:
            async with client.stream("GET", SOSHAGE_API + path, headers={
                "Accept": "application/json", "Accept-Language": "zh-TW", "User-Agent": USER_AGENT,
            }) as response:
                if response.status_code != 200 or "json" not in response.headers.get("content-type", "").lower():
                    raise BoardCatalogError("Soshage 蜡笔板资料暂时不可用。")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > maximum:
                        raise BoardCatalogError("Soshage 蜡笔板资料响应过大，已停止下载。")
    except BoardCatalogError:
        raise
    except httpx.TransportError:
        raise BoardCatalogError("Soshage 蜡笔板资料连接超时或网络异常。") from None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BoardCatalogError("Soshage 蜡笔板资料格式暂时无法识别。") from None


async def _get_crayon_note_core(transport=None) -> str:
    options: dict[str, Any] = {
        "timeout": httpx.Timeout(30, connect=8), "follow_redirects": False, "trust_env": False,
    }
    if transport is not None:
        options["transport"] = transport
    try:
        async with http_clients.client("trickcal-crayon-note", **options) as client:
            async with client.stream("GET", CRAYON_NOTE_CORE, headers={
                "Accept": "application/vnd.github+json", "User-Agent": USER_AGENT,
            }) as response:
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code != 200 or "json" not in content_type:
                    raise BoardCatalogError("Crayon-note 蜡笔板资料暂时不可用。")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CRAYON_NOTE_BYTES:
                        raise BoardCatalogError("Crayon-note 资料响应过大，已停止下载。")
    except BoardCatalogError:
        raise
    except httpx.TransportError:
        raise BoardCatalogError("Crayon-note 资料连接超时或网络异常。") from None
    try:
        document = json.loads(body)
        if (
            not isinstance(document, dict) or document.get("type") != "file"
            or document.get("path") != "data_core.js" or document.get("encoding") != "base64"
            or not isinstance(document.get("content"), str)
            or not isinstance(document.get("size"), int)
            or not 1 <= document["size"] <= MAX_CRAYON_NOTE_BYTES
        ):
            raise ValueError
        encoded = re.sub(r"\s+", "", document["content"])
        if not encoded or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded):
            raise ValueError
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != document["size"] or len(raw) > MAX_CRAYON_NOTE_BYTES:
            raise ValueError
        return raw.decode("utf-8-sig")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise BoardCatalogError("Crayon-note 资料格式暂时无法识别。") from None


def _reduce_catalog(units_value: Any, boards_value: Any) -> dict[str, Any]:
    if not isinstance(units_value, list) or len(units_value) > 2_000:
        raise BoardCatalogError("Soshage 角色目录格式不正确。")
    if not isinstance(boards_value, list) or len(boards_value) > 100_000:
        raise BoardCatalogError("Soshage 蜡笔节点目录格式不正确。")
    units: list[list[Any]] = []
    unavailable: set[int] = set()
    for row in units_value:
        if not isinstance(row, dict) or not _positive_int(row.get("uid")):
            continue
        name = str(row.get("name") or "").strip()
        alias = str(row.get("resource_name") or row.get("icon") or "").strip()
        personality = row.get("personality")
        if type(personality) is not int or personality not in {0, 1, 2, 3, 4}:
            personality = -1
        units.append([row["uid"], name[:60], alias[:60], personality])
        if row.get("available") is False:
            unavailable.add(row["uid"])
    nodes: list[list[Any]] = []
    for row in boards_value:
        if not isinstance(row, dict):
            continue
        values = [row.get("uid"), row.get("unit_uid"), row.get("step"), row.get("node_type")]
        if not all(_positive_int(value) for value in values[:3]) or not isinstance(values[3], int):
            continue
        material_ids = str(row.get("need_item_ids") or "").split(",")
        material_values = str(row.get("need_item_values") or "").split(",")
        gold_crayons = 0
        for index, raw_item_id in enumerate(material_ids):
            try:
                if int(raw_item_id.strip()) != GOLD_CRAYON_ITEM_ID:
                    continue
                amount = int(float(material_values[index].strip()))
            except (ValueError, IndexError):
                continue
            gold_crayons += max(0, amount)
        nodes.append([
            values[0], values[1], values[2], values[3],
            str(row.get("stat_type") or ""), str(row.get("stat_value") or ""),
            max(0, int(row.get("need_gold") or 0)),
            gold_crayons,
        ])
    if not units or not nodes:
        raise BoardCatalogError("Soshage 公开目录当前为空。")
    return {
        "version": CATALOG_VERSION, "fetched": time.time(),
        "sources": ["Soshage"], "units": units, "nodes": nodes,
        "unavailable_units": sorted(unavailable),
    }


@singleflight(lambda transport=None, force_refresh=False: (
    id(transport), str(_cache_path()), bool(force_refresh)
))
async def catalogue(transport=None, force_refresh: bool = False) -> Catalog:
    key = (id(transport), str(_cache_path()))
    if not force_refresh:
        cached = _catalog_cache.get(key)
        if cached is not None and time.time() < _catalog_expiry.get(key, 0):
            return cached
        _catalog_cache.pop(key, None)
        _catalog_expiry.pop(key, None)
    if transport is None and not force_refresh:
        disk = await asyncio.to_thread(_read_disk_catalog)
        if disk is not None:
            _catalog_cache[key] = disk
            _catalog_expiry[key] = min(time.time() + CATALOG_TTL, disk.fetched + CATALOG_TTL)
            return disk
    try:
        units_value, boards_value, crayon_value = await asyncio.gather(
            _get_json("/unit", MAX_UNITS_BYTES, transport),
            _get_json("/unit/board", MAX_BOARDS_BYTES, transport),
            _get_crayon_note_core(transport),
            return_exceptions=True,
        )
        for value in (units_value, boards_value):
            if isinstance(value, BaseException):
                if isinstance(value, BoardCatalogError):
                    raise value
                raise BoardCatalogError("Soshage 蜡笔板资料暂时不可用。") from value
        reduced = _reduce_catalog(units_value, boards_value)
        if isinstance(crayon_value, str):
            try:
                reduced = _merge_crayon_note(reduced, crayon_value)
            except BoardCatalogError:
                # Crayon-note is an augmenting source. A bad or unavailable
                # revision must never take the established Soshage data down.
                pass
        result = _catalog_from_payload(reduced)
        if result is None:
            raise BoardCatalogError("Soshage 蜡笔板资料格式不正确。")
        if transport is None:
            await asyncio.to_thread(_write_disk_catalog, reduced)
        _catalog_cache[key] = result
        _catalog_expiry[key] = time.time() + CATALOG_TTL
        return result
    except BoardCatalogError:
        if transport is None:
            stale = await asyncio.to_thread(_read_disk_catalog, allow_stale=True)
            if stale is not None:
                _catalog_cache[key] = stale
                # Retry the public catalogue later without hammering it on every query.
                _catalog_expiry[key] = time.time() + 10 * 60
                return stale
        raise


def _daily_catalog_slot(now: float | None = None) -> datetime:
    """Latest 18:00 slot in the bot's UTC+8 operating timezone."""
    current = datetime.fromtimestamp(time.time() if now is None else now, CATALOG_TIMEZONE)
    slot = datetime.combine(current.date(), datetime_time(18, 0), CATALOG_TIMEZONE)
    if slot > current:
        slot -= timedelta(days=1)
    return slot


async def refresh_catalogue_if_due(
    store: Store, *, now: float | None = None, transport=None
) -> bool:
    """Refresh once for each daily 18:00 slot, retrying failures after 10 minutes."""
    now = time.time() if now is None else now
    slot = _daily_catalog_slot(now)
    slot_key = slot.isoformat()
    state = store.document(CATALOG_REFRESH_STATE)
    if state.get("completed_slot") == slot_key:
        return False
    if now - float(state.get("last_attempt", 0) or 0) < CATALOG_REFRESH_RETRY:
        return False
    with store.state(CATALOG_REFRESH_STATE) as value:
        value.update(last_attempt=now, target_slot=slot_key)
    refreshed = await catalogue(transport, force_refresh=True)
    if refreshed.stale:
        return False
    with store.state(CATALOG_REFRESH_STATE) as value:
        value.update(completed_slot=slot_key, completed=now, target_slot=slot_key)
    return True


def is_export_attachment(attachment: Any) -> bool:
    filename = str(getattr(attachment, "filename", "") or "")
    content_type = str(getattr(attachment, "content_type", "") or "").lower().split(";", 1)[0].strip()
    if content_type in {"application/json", "text/json"}:
        return True
    return (
        filename.lower().endswith(".json")
        and content_type.split("/", 1)[0] not in {"image", "audio", "video"}
    )


def valid_qq_export_url(url: str) -> bool:
    """Allow only QQ's first-party message and file-transfer HTTPS hosts."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in {None, 443}
        ):
            return False
        return media.valid_image_url(url) or host == "ftn.qq.com" or host.endswith(".ftn.qq.com")
    except ValueError:
        return False


async def fetch_qq_export(attachment: Any, transport=None) -> str:
    url = str(getattr(attachment, "url", "") or "")
    size = getattr(attachment, "size", None)
    if size is not None and (not isinstance(size, int) or size > MAX_IMPORT_BYTES):
        raise ToolError("导入文件不能超过 512 KiB。")
    if not url or not is_export_attachment(attachment):
        raise ToolError("请附上一份 Soshage 导出的 .json 文件。")
    if not valid_qq_export_url(url):
        raise ToolError("仅接受通过 QQ 直接上传的 JSON 文件，不接受网页链接。")
    options: dict[str, Any] = {"timeout": 20, "follow_redirects": False, "trust_env": False}
    if transport is not None:
        options["transport"] = transport
    try:
        async with http_clients.client("qq-board-import", **options) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_IMPORT_BYTES:
                        raise ToolError("导入文件不能超过 512 KiB。")
    except ToolError:
        raise
    except httpx.HTTPError:
        raise ToolError("QQ 中的导出文件暂时无法下载，请重新上传。") from None
    try:
        return bytes(body).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ToolError("导入文件必须是 UTF-8 JSON。") from None


def _percent_node_count(node_ids: list[int] | tuple[int, ...], catalog: Catalog | None) -> int:
    if catalog is None:
        return 0
    return sum(
        1 for node_uid in node_ids
        if (node := catalog.nodes.get(node_uid)) is not None and _node_stat_types(node)
    )


def _percent_summary(document: dict[str, Any], catalog: Catalog | None) -> tuple[int, int, int]:
    if catalog is None:
        return 0, 0, 0
    progress_roles = selected_count = planned_count = 0
    for _unit_uid, board in document["boards"]:
        selected = _percent_node_count(board["selectedNodes"], catalog)
        planned = _percent_node_count(board["plannedNodes"], catalog)
        selected_count += selected
        planned_count += planned
        progress_roles += bool(selected or planned)
    return progress_roles, selected_count, planned_count


def _normal(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = simplify_role_name(normalized)
    return "".join(ch for ch in normalized if ch.isalnum())


def _unit_label(unit_uid: int, catalog: Catalog | None) -> str:
    if catalog is None or unit_uid not in catalog.units:
        return f"角色 {unit_uid}"
    row = catalog.units[unit_uid]
    name = simplify_role_name(row["name"].strip())
    alias = row["alias"].strip()
    if name and alias and _normal(name) != _normal(alias):
        return f"{name}/{alias}"
    return name or alias or f"角色 {unit_uid}"


def _node_stats(
    node_ids: list[int], catalog: Catalog | None,
) -> tuple[dict[int, float], int, int, int]:
    totals: dict[int, float] = {}
    gold = 0
    gold_crayons = 0
    known = 0
    if catalog is None:
        return totals, gold, gold_crayons, known
    for node_uid in node_ids:
        node = catalog.nodes.get(node_uid)
        if node is None or not _node_stat_types(node):
            continue
        known += 1
        gold += node["gold"]
        gold_crayons += node.get("gold_crayons", 0)
        types = node["stat_type"].split(",")
        values = node["stat_value"].split(",")
        for index, raw_type in enumerate(types):
            try:
                stat_type = int(raw_type)
                amount = float(values[index])
            except (ValueError, IndexError):
                continue
            # Soshage uses separate stat IDs for flat and percentage bonuses.
            # The tracker intentionally reports only percentage attributes.
            if stat_type not in PERCENT_STATS or not math.isfinite(amount) or amount == 0:
                continue
            amount /= 10
            totals[stat_type] = totals.get(stat_type, 0) + amount
    return totals, gold, gold_crayons, known


def _format_number(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _stats_lines(stats: dict[int, float], *, limit: int = 16) -> list[str]:
    if not stats:
        return []
    lines: list[str] = []
    consumed: set[int] = set()
    for left, right, label in (
        (97, 101, "全体暴击/暴伤"),
        (99, 103, "全体暴抗/暴伤抗"),
    ):
        if left not in stats and right not in stats:
            continue
        consumed.update({left, right})
        left_value, right_value = stats.get(left, 0), stats.get(right, 0)
        if math.isclose(left_value, right_value, abs_tol=1e-9):
            amount = f"+{_format_number(left_value)}%"
        else:
            amount = f"+{_format_number(left_value)}% / +{_format_number(right_value)}%"
        lines.append(f"{label} {amount}")
    for key in sorted((key for key in stats if key not in consumed), key=lambda item: (item < 86, item)):
        lines.append(
            f"{STAT_LABELS.get(key, '属性 ' + str(key))} +{_format_number(stats[key])}%"
        )
    result = lines[:limit]
    if len(lines) > limit:
        result.append(f"另有 {len(lines) - limit} 项属性")
    return result


def _loaded(board_store: BoardStore, who: Identity) -> tuple[dict[str, Any], float]:
    row = board_store.get(who)
    if row is None:
        if who.private:
            web_help = (
                "网页使用方法：\n"
                "1. 在私聊发送 /tr 蜡笔板 网页，领取 10 分钟内有效的单次登录链接。\n"
                "2. 打开链接后创建独立的蜡笔板账号，以后可直接用账号密码登录。\n"
                "3. 登录网页后可点亮角色、增减节点或导入 Soshage JSON。"
            )
        else:
            web_help = (
                "网页使用方法：\n"
                "1. 在当前群发送 /tr 蜡笔板 网页，获取网页地址和绑定码。\n"
                "2. 打开网页，在“首次绑定”中输入绑定码，网页会生成回执码。\n"
                "3. 回到当前群发送 /tr 蜡笔板 绑定 回执码。\n"
                "4. 网页确认后创建蜡笔板账号；以后可直接用账号密码登录。\n"
                "已有账号时，也可登录网页点击“绑定新机器人”，再在本群发送网页生成的绑定命令。"
            )
        raise ToolError(
            "你还没有创建蜡笔板。\n\n"
            f"{web_help}\n\n"
            "也可直接发送 /tr 蜡笔板 点亮 角色名，"
            "或发送 /tr 蜡笔板 导入 后上传 Soshage JSON。"
        )
    return row


def _catalog_note(catalog: Catalog | None) -> str:
    if catalog is None:
        return "节点目录暂时不可用，本次无法统计百分比节点。"
    date = time.strftime("%Y-%m-%d", time.localtime(catalog.fetched))
    sources = " + ".join(catalog.sources)
    return f"{sources} 属性目录缓存于 {date}" + ("（当前使用过期缓存）" if catalog.stale else "")


async def _try_catalog(transport=None) -> Catalog | None:
    try:
        return await catalogue(transport)
    except BoardCatalogError:
        return None


def _owned_rows(document: dict[str, Any], catalog: Catalog | None = None) -> list[list[Any]]:
    """Filter the display by catalogue without altering saved ownership or progress."""
    board_by_unit = {unit_uid: board for unit_uid, board in document["boards"]}
    return [
        [unit_uid, board_by_unit.get(unit_uid, {"selectedNodes": [], "plannedNodes": []})]
        for unit_uid, _rarity in document["units"]
        if catalog is None or unit_uid not in catalog.unavailable_units
    ]


def _node_stat_types(node: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    for value in node["stat_type"].split(","):
        try:
            stat_type = int(value)
        except ValueError:
            continue
        if stat_type in PERCENT_STATS:
            result.add(stat_type)
    return result


def _percent_progress(
    rows: list[list[Any]], catalog: Catalog, target_types: frozenset[int]
) -> dict[int, tuple[int, int, int]]:
    """Return selected, total and planned-but-not-selected node counts by layer."""
    result: dict[int, list[int]] = {step: [0, 0, 0] for step in LAYER_NAMES}
    for unit_uid, board in rows:
        selected = set(board["selectedNodes"])
        planned = set(board["plannedNodes"])
        for node_uid in catalog.by_unit.get(unit_uid, ()):
            node = catalog.nodes[node_uid]
            step = node["step"]
            if step not in result or not (_node_stat_types(node) & target_types):
                continue
            result[step][1] += 1
            if node_uid in selected:
                result[step][0] += 1
            elif node_uid in planned:
                result[step][2] += 1
    return {step: tuple(counts) for step, counts in result.items()}


def _percent_layer_lines(rows: list[list[Any]], catalog: Catalog | None) -> list[str]:
    if catalog is None:
        return []
    progress_by_group = {
        group: _percent_progress(rows, catalog, target_types)
        for group, (_label, target_types) in PERCENT_NODE_GROUPS.items()
    }
    lines = ["百分比节点分层："]
    for step, layer_name in LAYER_NAMES.items():
        values: list[str] = []
        for group, progress in progress_by_group.items():
            done, total, _planned = progress[step]
            if total == 0:
                continue
            display_group = {
                "生命": "血量", "暴击": "暴击/暴伤", "暴抗": "暴抗/暴伤抗",
            }.get(group, group)
            values.append(f"{display_group} {done}/{total}（差 {total - done}）")
        if not values:
            continue
        lines.append(f"{layer_name}：" + " · ".join(values[:3]))
        if len(values) > 3:
            lines.append("　" + " · ".join(values[3:]))
    return lines if len(lines) > 1 else []


async def _overview(board_store: BoardStore, who: Identity, transport=None) -> str:
    document, updated = _loaded(board_store, who)
    catalog = await _try_catalog(transport)
    progress_count, selected_count, planned_count = _percent_summary(document, catalog)
    owned_rows = _owned_rows(document, catalog)
    selected_ids = [uid for _, board in document["boards"] for uid in board["selectedNodes"]]
    planned_ids = [uid for _, board in document["boards"] for uid in board["plannedNodes"]]
    selected_stats, selected_gold, selected_crayons, known_selected = _node_stats(selected_ids, catalog)
    planned_stats, planned_gold, planned_crayons, known_planned = _node_stats(planned_ids, catalog)
    lines = [
        f"拥有角色：{len(owned_rows)} 个 · 已有百分比进度：{progress_count} 个",
        f"百分比已点节点：{selected_count} 个 · 百分比计划节点：{planned_count} 个",
        f"数据更新时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(updated))}",
    ]
    layer_lines = _percent_layer_lines(owned_rows, catalog)
    if layer_lines:
        lines.extend(["", *layer_lines])
    selected_lines = _stats_lines(selected_stats, limit=14)
    if selected_lines:
        lines.extend(["", "当前累计百分比加成：", *selected_lines])
    if planned_stats:
        lines.extend(["", "计划新增百分比加成：", *_stats_lines(planned_stats, limit=10)])
    if catalog is not None:
        lines.extend([
            f"百分比节点已用金币：{selected_gold:,} · 已使用金蜡笔：{selected_crayons:,} 支",
            f"百分比节点计划金币：{planned_gold:,} · 计划金蜡笔：{planned_crayons:,} 支",
        ])
    return panel(
        "我的蜡笔板", lines, icon="🖍️",
        footer=_catalog_note(catalog) + "；仅统计百分比属性；/tr 蜡笔板 未点 攻击 或 防御。",
    )


def _board_rows(document: dict[str, Any]) -> list[list[Any]]:
    return [row for row in document["boards"] if row[1]["selectedNodes"] or row[1]["plannedNodes"]]


async def _unit_list(board_store: BoardStore, who: Identity, page_raw: str, transport=None) -> str:
    document, _ = _loaded(board_store, who)
    catalog = await _try_catalog(transport)
    try:
        page_number = int(page_raw or "1")
    except ValueError:
        raise ToolError("页码必须是正整数。") from None
    if page_number < 1 or page_number > 1000:
        raise ToolError("页码范围是 1～1000。")
    rows = _owned_rows(document, catalog)
    start = (page_number - 1) * 8
    lines = []
    for unit_uid, board in rows[start:start + 8]:
        selected_count = _percent_node_count(board["selectedNodes"], catalog)
        planned_count = _percent_node_count(board["plannedNodes"], catalog)
        lines.append(
            f"{_unit_label(unit_uid, catalog)} · 百分比已点 {selected_count} · 计划 {planned_count}"
        )
    if not lines:
        lines = ["这一页没有角色。"]
    content = panel(f"蜡笔板角色 · 第 {page_number} 页", lines, icon="🖍️",
                    footer=f"共 {len(rows)} 个角色；查看详情：/tr 蜡笔板 角色 名称或ID；每页 8 个。")
    return PaginatedReply(
        content, base_command="/tr 蜡笔板 角色列表", page=page_number, total=len(rows),
    )


async def _unowned_list(board_store: BoardStore, who: Identity, page_raw: str, transport=None) -> str:
    try:
        catalog = await catalogue(transport)
    except BoardCatalogError:
        raise ToolError("公开角色目录暂时不可用，无法计算未点亮角色。") from None
    try:
        page_number = int(page_raw or "1")
    except ValueError:
        raise ToolError("页码必须是正整数。") from None
    if page_number < 1 or page_number > 1000:
        raise ToolError("页码范围是 1～1000。")
    saved = await asyncio.to_thread(board_store.get, who)
    owned_ids = {unit_uid for unit_uid, _rarity in saved[0]["units"]} if saved else set()
    unit_uids = [unit_uid for unit_uid in sorted(catalog.units) if unit_uid not in owned_ids]
    start = (page_number - 1) * 8
    lines = [_unit_label(unit_uid, catalog) for unit_uid in unit_uids[start:start + 8]]
    if not lines:
        lines = ["这一页没有未点亮角色。"]
    content = panel(
        f"未点亮角色 · 第 {page_number} 页", lines, icon="🔎",
        footer=(
            f"共 {len(unit_uids)} 个角色尚未点亮；这里只比较公开角色目录与个人拥有记录。"
            "每页 8 个。"
        ),
    )
    return PaginatedReply(
        content, base_command="/tr 蜡笔板 未拥有", page=page_number, total=len(unit_uids),
    )


def _find_unit(
    rows: list[list[Any]], query: str, catalog: Catalog | None, *, owned: bool = True
) -> list[Any]:
    query = query.strip()
    if not query:
        raise ToolError("用法：/tr 蜡笔板 角色 名称或ID。")
    if query.isdigit():
        result = next((row for row in rows if row[0] == int(query)), None)
        if result is None:
            raise ToolError("你还没有点亮这个角色 ID。" if owned else "公开目录中没有这个角色 ID。")
        return result
    if catalog is None:
        raise ToolError("角色名称目录暂时不可用，请稍后重试；已知角色 ID 仍可直接查询。")
    needle = _normal(query)
    exact, partial, fuzzy = [], [], []
    for row in rows:
        unit = catalog.units.get(row[0], {})
        terms = [unit.get("name", ""), unit.get("alias", "")]
        normals = [_normal(term) for term in terms if term]
        if needle in normals:
            exact.append(row)
        elif needle and any(needle in term for term in normals):
            partial.append(row)
        elif needle and any(
            len(needle) == len(term)
            and sum(left != right for left, right in zip(needle, term)) == 1
            for term in normals
        ):
            # Accept a unique one-character localization difference, such as
            # the mainland/Taiwan renderings 埃尔芬 and 艾爾芬.
            fuzzy.append(row)
    matches = exact or partial or fuzzy
    if not matches:
        source = "你已点亮的角色中" if owned else "公开角色目录中"
        raise ToolError(source + "没有找到这个角色；可用简繁中文名、常见译名、英文名或数字 ID。")
    if len(matches) > 1:
        choices = "、".join(f"{_unit_label(row[0], catalog)}({row[0]})" for row in matches[:5])
        raise ToolError("名称不唯一，请改用角色 ID：" + choices)
    return matches[0]


async def _unit_detail(board_store: BoardStore, who: Identity, query: str, transport=None) -> str:
    document, _ = _loaded(board_store, who)
    catalog = await _try_catalog(transport)
    unit_uid, board = _find_unit(_owned_rows(document, catalog), query, catalog)
    selected = board["selectedNodes"]
    planned = board["plannedNodes"]
    total = _percent_node_count(catalog.by_unit.get(unit_uid, ()), catalog) if catalog is not None else 0
    selected_stats, selected_gold, selected_crayons, known_selected = _node_stats(selected, catalog)
    planned_stats, planned_gold, planned_crayons, known_planned = _node_stats(planned, catalog)
    lines = [
        f"角色 ID：{unit_uid}",
        f"百分比已点 {known_selected}" + (f"/{total}" if total else "") + f" · 计划 {known_planned}",
    ]
    layer_lines = _percent_layer_lines([[unit_uid, board]], catalog)
    if layer_lines:
        lines.extend(layer_lines)
    if selected_stats:
        lines.extend(["", "当前百分比加成：", *_stats_lines(selected_stats, limit=16)])
    if planned_stats:
        lines.extend(["", "计划新增百分比加成：", *_stats_lines(planned_stats, limit=12)])
    if catalog is not None:
        lines.extend([
            f"百分比节点已用金币：{selected_gold:,} · 已使用金蜡笔：{selected_crayons:,} 支",
            f"百分比节点计划金币：{planned_gold:,} · 计划金蜡笔：{planned_crayons:,} 支",
        ])
    actions: list[ReplyAction] = []
    if catalog is not None:
        selected_set = set(selected)
        button_group_names = {
            "攻击": "攻击", "防御": "防御", "生命": "血量",
            "暴击": "暴击", "暴抗": "暴抗",
        }
        for step in LAYER_NAMES:
            for group, (_label, target_types) in PERCENT_NODE_GROUPS.items():
                node_ids = [
                    node_uid for node_uid in catalog.by_unit.get(unit_uid, ())
                    if catalog.nodes[node_uid]["step"] == step
                    and _node_stat_types(catalog.nodes[node_uid]) & target_types
                ]
                if len(node_ids) == 1 and node_ids[0] not in selected_set:
                    actions.append(ReplyAction(
                        f"{step}层{button_group_names[group]}",
                        f"/tr 蜡笔板 加点 {unit_uid} {step} {group}",
                    ))
    footer = _catalog_note(catalog)
    if actions:
        footer += "；可点击下方按钮点亮未完成的百分比节点，仅修改你自己的记录。"
    content = panel(_unit_label(unit_uid, catalog) + " · 蜡笔板", lines, icon="🖍️",
                    footer=footer)
    return ActionReply(content, actions) if actions else content


def _percent_group(value: str) -> str:
    normalized = _normal(value)
    aliases = {_normal(alias): group for alias, group in PERCENT_GROUP_ALIASES.items()}
    group = aliases.get(normalized)
    if group is None:
        raise ToolError("百分比属性可用：攻击、防御、血量、暴击/暴伤、暴抗/暴伤抗（防爆）。")
    return group


async def _unlock(board_store: BoardStore, who: Identity, query: str, transport=None) -> str:
    if not query.strip():
        raise ToolError("用法：/tr 蜡笔板 点亮 角色名或ID；或 /tr 蜡笔板 点亮 全部。")
    try:
        catalog = await catalogue(transport)
    except BoardCatalogError:
        raise ToolError("公开角色目录暂时不可用，无法确认要点亮的角色。") from None
    if _normal(query) in {_normal(alias) for alias in BULK_UNIT_ALIASES}:
        added, owned = await asyncio.to_thread(
            board_store.add_units, who, list(catalog.units),
        )
        status = "已完成" if added else "无需更新"
        return panel(f"全部角色点亮{status}", [
            f"公共目录角色：{len(catalog.units)} 个",
            f"本次新增：{added} 个 · 现在拥有：{owned} 个",
            "仅记录角色拥有状态，没有点亮任何蜡笔节点。",
        ], icon="✨" if added else "ℹ️",
            footer="只修改你自己的记录；以后目录新增角色时可再次执行。导入 JSON 会覆盖手动记录。")
    catalog_rows = [[unit_uid, {"selectedNodes": [], "plannedNodes": []}]
                    for unit_uid in catalog.units]
    unit_uid, _board = _find_unit(catalog_rows, query, catalog, owned=False)
    added = await asyncio.to_thread(board_store.add_unit, who, unit_uid)
    if not added:
        return panel("角色已经点亮", [
            _unit_label(unit_uid, catalog), f"角色 ID：{unit_uid}",
        ], icon="ℹ️", footer="无需重复操作；可发送 /tr 蜡笔板 角色 名称 查看进度。")
    return panel("新角色已点亮", [
        _unit_label(unit_uid, catalog), f"角色 ID：{unit_uid}",
        "蜡笔节点从 0 开始，可继续使用“加点”命令记录进度。",
    ], icon="✨", footer="下次导入 Soshage JSON 时，会以导入文件中的完整状态为准。")


async def _remove_owned(board_store: BoardStore, who: Identity, value: str, transport=None) -> str:
    raw = value.strip()
    if not raw:
        raise ToolError(
            "用法：/tr 蜡笔板 取消点亮 角色名或逗号名单；"
            "清空全部请发送 /tr 蜡笔板 取消点亮 全部 确认。"
        )
    confirmed = False
    query = raw
    prefix, separator, final_word = raw.rpartition(" ")
    if separator and final_word.casefold() in {"确认", "confirm"}:
        query = prefix.strip()
        confirmed = True
    all_owned, unit_queries = _mark_unit_queries(query)
    document, _updated = _loaded(board_store, who)
    owned_rows = _owned_rows(document)
    if not owned_rows:
        raise ToolError("你的蜡笔板当前没有已点亮角色。")
    if all_owned:
        if not confirmed:
            raise ToolError(
                "这会移除全部已拥有角色及其所有节点记录。确定请发送："
                "/tr 蜡笔板 取消点亮 全部 确认"
            )
        unit_uids = [unit_uid for unit_uid, _board in owned_rows]
    else:
        catalog = await _try_catalog(transport)
        unit_uids = []
        for item in unit_queries:
            unit_uid, _board = _find_unit(owned_rows, item, catalog)
            if unit_uid not in unit_uids:
                unit_uids.append(unit_uid)
    removed, remaining = await asyncio.to_thread(
        board_store.remove_units, who, unit_uids,
    )
    if not removed:
        raise ToolError("没有找到可以取消点亮的角色。")
    scope = "全部角色" if all_owned else f"指定角色 {removed} 个"
    return panel("角色已取消点亮", [
        f"范围：{scope}",
        f"本次移除：{removed} 个 · 剩余拥有：{remaining} 个",
        "这些角色的已点节点与计划节点记录也已一并清除。",
    ], icon="✅", footer="只修改你自己的记录；重新导入 Soshage JSON 会以文件内容为准。")


def _parse_mark_value(value: str) -> tuple[str, int, str | None]:
    parts = value.rsplit(maxsplit=2)
    if len(parts) != 3:
        raise ToolError(
            "用法：/tr 蜡笔板 加点 角色名或逗号分隔名单 1|2|3 百分比属性；"
            "属性可写“全部”。"
        )
    unit_query, layer_raw, attribute_raw = parts
    layer = LAYER_ALIASES.get(layer_raw)
    if layer is None:
        raise ToolError("层数可用：1、2、3，也可写第一层、第二层、第三层。")
    group = None if _normal(attribute_raw) in {
        _normal(alias) for alias in ALL_PERCENT_ALIASES
    } else _percent_group(attribute_raw)
    return unit_query.strip(), layer, group


def _mark_unit_queries(value: str) -> tuple[bool, list[str]]:
    if _normal(value) in {_normal(alias) for alias in BULK_UNIT_ALIASES}:
        return True, []
    queries = [item.strip() for item in re.split(r"[,，、]+", value) if item.strip()]
    if not queries:
        raise ToolError("请填写角色名或数字 ID；多个角色可用逗号、中文逗号或顿号分隔。")
    if len(queries) > MAX_BATCH_UNITS:
        raise ToolError(f"一次最多批量操作 {MAX_BATCH_UNITS} 个角色。")
    return False, queries


def _mark_node_ids(
    catalog: Catalog, unit_uid: int, layer: int, groups: tuple[str, ...],
) -> list[int]:
    node_ids: set[int] = set()
    for group in groups:
        _label, target_types = PERCENT_NODE_GROUPS[group]
        matched = [
            node_uid for node_uid in catalog.by_unit.get(unit_uid, ())
            if catalog.nodes[node_uid]["step"] == layer
            and _node_stat_types(catalog.nodes[node_uid]) & target_types
        ]
        if len(matched) > 1:
            raise ToolError(
                f"{_unit_label(unit_uid, catalog)}的节点目录出现重复匹配，"
                "已停止全部操作以避免误加点。"
            )
        node_ids.update(matched)
    return sorted(node_ids)


async def _mark(
    board_store: BoardStore,
    who: Identity,
    value: str,
    *,
    selected: bool,
    transport=None,
) -> str:
    unit_query, layer, group = _parse_mark_value(value)
    try:
        catalog = await catalogue(transport)
    except BoardCatalogError:
        raise ToolError("百分比节点目录暂时不可用，无法准确记录加点。") from None
    groups = tuple(PERCENT_NODE_GROUPS) if group is None else (group,)
    label = "全部百分比属性" if group is None else PERCENT_NODE_GROUPS[group][0]
    all_owned, unit_queries = _mark_unit_queries(unit_query)
    if all_owned:
        saved = await asyncio.to_thread(board_store.get, who)
        if saved is None or not saved[0]["units"]:
            raise ToolError(
                "你还没有点亮角色。请先发送 /tr 蜡笔板 点亮 全部，"
                "或逐个点亮角色后再批量加点。"
            )
        selections: dict[int, list[int]] = {}
        for unit_uid, _rarity in saved[0]["units"]:
            node_ids = _mark_node_ids(catalog, unit_uid, layer, groups)
            if node_ids:
                selections[unit_uid] = node_ids
        if not selections:
            raise ToolError(f"已拥有角色的{LAYER_NAMES[layer]}都没有{label}节点。")
        changed, changed_roles = await asyncio.to_thread(
            board_store.set_selected_nodes_bulk, who, selections, selected=selected,
        )
        action = "加点" if selected else "撤销加点"
        status = "已记录" if changed else "无需更新"
        unchanged = len(selections) - changed_roles
        lines = [
            "范围：全部已点亮角色",
            f"节点：{LAYER_NAMES[layer]} · {label}",
            f"适用角色：{len(selections)} 个",
            f"本次变更：{changed_roles} 个角色 · {changed} 个节点",
        ]
        if unchanged:
            state = "已经点亮" if selected else "原本尚未点亮"
            lines.append(f"无需改变：{unchanged} 个角色（{state}）")
        return panel(f"蜡笔板批量{action}{status}", lines,
            icon="✅" if changed else "ℹ️",
            footer="只修改你自己的已拥有角色；下次导入 Soshage JSON 时，以导入文件为准。")
    catalog_rows = [[unit_uid, {"selectedNodes": [], "plannedNodes": []}]
                    for unit_uid in catalog.units]
    unit_uids: list[int] = []
    for query in unit_queries:
        unit_uid, _board = _find_unit(catalog_rows, query, catalog, owned=False)
        if unit_uid not in unit_uids:
            unit_uids.append(unit_uid)
    selections: dict[int, list[int]] = {}
    without_nodes: list[str] = []
    for unit_uid in unit_uids:
        node_ids = _mark_node_ids(catalog, unit_uid, layer, groups)
        if not node_ids:
            without_nodes.append(_unit_label(unit_uid, catalog))
        else:
            selections[unit_uid] = node_ids
    if without_nodes:
        names = "、".join(without_nodes[:8])
        suffix = f"等 {len(without_nodes)} 个角色" if len(without_nodes) > 8 else ""
        raise ToolError(
            f"{names}{suffix}的{LAYER_NAMES[layer]}没有{label}节点；"
            "本次没有修改任何记录。"
        )
    saved = await asyncio.to_thread(board_store.get, who)
    owned_ids = {uid for uid, _rarity in saved[0]["units"]} if saved else set()
    missing_owned = [unit_uid for unit_uid in unit_uids if unit_uid not in owned_ids]
    if missing_owned:
        if not selected:
            names = "、".join(_unit_label(unit_uid, catalog) for unit_uid in missing_owned[:8])
            raise ToolError(f"你还没有点亮这些角色，无法撤销其加点：{names}。")
        await asyncio.to_thread(board_store.add_units, who, missing_owned)
    changed, changed_roles = await asyncio.to_thread(
        board_store.set_selected_nodes_bulk, who, selections, selected=selected,
    )
    action = "加点" if selected else "撤销加点"
    if len(unit_uids) > 1:
        status = "已记录" if changed else "无需更新"
        unchanged = len(unit_uids) - changed_roles
        lines = [
            f"范围：指定角色 {len(unit_uids)} 个",
            f"节点：{LAYER_NAMES[layer]} · {label}",
            f"本次变更：{changed_roles} 个角色 · {changed} 个节点",
        ]
        if missing_owned:
            lines.append(f"同时点亮新角色：{len(missing_owned)} 个")
        if unchanged:
            state = "已经点亮" if selected else "原本尚未点亮"
            lines.append(f"无需改变：{unchanged} 个角色（{state}）")
        return panel(f"蜡笔板名单批量{action}{status}", lines,
            icon="✅" if changed else "ℹ️",
            footer="名单已完整校验后一次写入；下次导入 Soshage JSON 时，以导入文件为准。")
    unit_uid = unit_uids[0]
    status = "已记录" if changed else ("原本已经点亮" if selected else "原本尚未点亮")
    lines = [
        f"角色：{_unit_label(unit_uid, catalog)} · ID {unit_uid}",
        f"节点：{LAYER_NAMES[layer]} · {label}",
    ]
    if missing_owned:
        lines.append("已同时创建你的蜡笔板并点亮该角色。")
    return panel(f"蜡笔板{action}{status}", lines, icon="✅" if changed else "ℹ️",
        footer="只修改你自己的记录；下次导入 Soshage JSON 时，以导入文件为准。")


def _parse_missing_query(value: str) -> tuple[str, int | None, int]:
    parts = value.split()
    if not parts:
        raise ToolError("用法：/tr 蜡笔板 未点 [一层|二层|三层] 属性 [页码]。")
    page_number = 1
    # Keep the original `未点 攻击 2` syntax meaning page 2.  To filter a
    # layer, use an explicit layer word such as `二层`, or put bare `2` first.
    if len(parts) > 1 and parts[-1].isdigit() and not (
        len(parts) == 2 and parts[0] in LAYER_ALIASES
    ):
        page_number = int(parts.pop())
    if page_number < 1 or page_number > 1000:
        raise ToolError("页码范围是 1～1000。")
    layer: int | None = None
    if len(parts) == 1:
        attribute_raw = parts[0]
    elif len(parts) == 2:
        if parts[0] in LAYER_ALIASES:
            layer, attribute_raw = LAYER_ALIASES[parts[0]], parts[1]
        elif parts[1] in LAYER_ALIASES:
            attribute_raw, layer = parts[0], LAYER_ALIASES[parts[1]]
        else:
            raise ToolError("层数可写一层、二层、三层；例如：/tr 蜡笔板 未点 二层 攻击。")
    else:
        raise ToolError("用法：/tr 蜡笔板 未点 [一层|二层|三层] 属性 [页码]。")
    return _percent_group(attribute_raw), layer, page_number


async def _missing(board_store: BoardStore, who: Identity, value: str, transport=None) -> str:
    document, _ = _loaded(board_store, who)
    group, layer, page_number = _parse_missing_query(value)
    try:
        catalog = await catalogue(transport)
    except BoardCatalogError:
        raise ToolError("百分比节点目录暂时不可用，无法准确计算哪些角色未点。") from None
    label, target_types = PERCENT_NODE_GROUPS[group]
    incomplete: list[tuple[int, str, int, int, int, dict[int, tuple[int, int, int]]]] = []
    for unit_uid, board in _owned_rows(document):
        progress = _percent_progress([[unit_uid, board]], catalog, target_types)
        relevant = [progress[layer]] if layer is not None else list(progress.values())
        selected = sum(counts[0] for counts in relevant)
        total = sum(counts[1] for counts in relevant)
        planned = sum(counts[2] for counts in relevant)
        missing = total - selected
        if total and missing:
            incomplete.append((missing, _unit_label(unit_uid, catalog), unit_uid, selected, planned, progress))
    incomplete.sort(key=lambda row: (-row[0], _normal(row[1]), row[2]))
    start = (page_number - 1) * 8
    lines: list[str] = []
    for missing, unit_label, unit_uid, selected, planned, progress in incomplete[start:start + 8]:
        total = selected + missing
        if layer is None:
            layer_gaps = " · ".join(
                f"{LAYER_NAMES[step]}差{counts[1] - counts[0]}"
                for step, counts in progress.items() if counts[1] - counts[0]
            )
        else:
            layer_gaps = ""
        plan_note = f" · 已计划 {planned}" if planned else ""
        gap_note = f"（{layer_gaps}）" if layer_gaps else ""
        lines.append(
            f"{unit_label} · 已点 {selected}/{total} · 差 {missing}{plan_note}{gap_note}"
        )
    if not lines:
        if page_number == 1:
            scope = LAYER_NAMES[layer] if layer is not None else ""
            lines = [f"已拥有且目录可识别的角色，都已点完{scope}{label}节点。"]
        else:
            lines = ["这一页没有角色。"]
    scope = LAYER_NAMES[layer] if layer is not None else ""
    content = panel(
        f"未点{scope}{label} · 第 {page_number} 页", lines, icon="🔎",
        footer=f"共 {len(incomplete)} 个角色未点完{scope}{label}；只计算百分比节点。每页 8 个。",
    )
    base_command = f"/tr 蜡笔板 未点 {LAYER_NAMES[layer]} {group}" if layer is not None else (
        f"/tr 蜡笔板 未点 {group}"
    )
    return PaginatedReply(
        content, base_command=base_command, page=page_number, total=len(incomplete),
    )


async def _plan(board_store: BoardStore, who: Identity, page_raw: str, transport=None) -> str:
    document, _ = _loaded(board_store, who)
    catalog = await _try_catalog(transport)
    try:
        page_number = int(page_raw or "1")
    except ValueError:
        raise ToolError("页码必须是正整数。") from None
    if page_number < 1 or page_number > 1000:
        raise ToolError("页码范围是 1～1000。")
    rows = [
        (row, _percent_node_count(row[1]["plannedNodes"], catalog))
        for row in _board_rows(document)
    ]
    rows = [row for row in rows if row[1] > 0]
    rows.sort(key=lambda row: (-row[1], row[0][0]))
    start = (page_number - 1) * 8
    lines = []
    for (unit_uid, board), planned_count in rows[start:start + 8]:
        _, gold, crayons, _known = _node_stats(board["plannedNodes"], catalog)
        lines.append(
            f"{_unit_label(unit_uid, catalog)} · 百分比计划 {planned_count} 个"
            f" · 金币 {gold:,} · 金蜡笔 {crayons:,} 支"
        )
    if not lines:
        lines = ["这一页没有计划节点。"]
    content = panel(f"蜡笔板计划 · 第 {page_number} 页", lines, icon="🗒️",
                    footer=f"共 {len(rows)} 个角色有百分比节点计划；金币和金蜡笔只计算百分比节点；每页 8 个。")
    return PaginatedReply(
        content, base_command="/tr 蜡笔板 计划", page=page_number, total=len(rows),
    )


def help_text() -> str:
    return help_panel("蜡笔板追踪", [
        "/tr 蜡笔板 导入 · 再单独上传导出的 .json 文件",
        "/tr 蜡笔板 网页 · 群聊领取网页账号绑定码",
        "/tr 蜡笔板 绑定 绑定码 · 确认网页身份或绑定新机器人",
        "/tr 蜡笔板 导入 取消 · 取消等待上传",
        "/tr 蜡笔板 · 查看总进度与百分比加成",
        "/tr 蜡笔板 点亮 角色名或ID · 记录新角色",
        "/tr 蜡笔板 点亮 全部 · 一键记录所有角色",
        "/tr 蜡笔板 未拥有 [页码] · 查看尚未点亮角色",
        "/tr 蜡笔板 取消点亮 角色名或逗号名单",
        "/tr 蜡笔板 取消点亮 全部 确认 · 清空拥有记录",
        "/tr 蜡笔板 加点 角色名 1|2|3 属性|全部 · 可直接创建",
        "/tr 蜡笔板 加点 角色甲,角色乙 1|2|3 属性|全部 · 名单批量",
        "/tr 蜡笔板 加点 全部 1|2|3 属性|全部 · 全角色批量",
        "/tr 蜡笔板 撤销加点 角色名或逗号名单 1|2|3 属性|全部",
        "/tr 蜡笔板 撤销加点 全部 1|2|3 属性|全部",
        "/tr 蜡笔板 角色列表 [页码]",
        "/tr 蜡笔板 角色 名称或ID",
        "/tr 蜡笔板 未点 攻击|防御 [页码]",
        "/tr 蜡笔板 未点 血量|暴击/暴伤|暴抗/暴伤抗(防爆) [页码]",
        "/tr 蜡笔板 未点 一层|二层|三层 属性 [页码] · 按层筛选",
        "/tr 蜡笔板 计划 [页码]",
        "/tr 蜡笔板 删除 确认",
    ], footer="私聊和群聊均可导入；10 分钟内只检查发起人在当前会话的下一条消息，不是一个 .json 文件就取消。数据只属于发送者。")


def _import_success(document: dict[str, Any]) -> str:
    return panel("蜡笔板已导入", [
        f"拥有角色：{len(document['units'])} 个",
        "百分比节点、金币和金蜡笔将在查看时按最新公开目录计算。",
        "这份记录已替换你上一次导入的数据。",
    ], icon="✅", footer="只保存角色拥有状态与蜡笔节点，不保存 Soshage 账号。发送 /tr 蜡笔板 查看。")


async def accept_pending_upload(
    store: Store,
    who: Identity,
    attachments: list[Any],
    *,
    transport=None,
) -> str | None:
    """Import a separately uploaded file for the same user and conversation."""
    board_store = BoardStore(store)
    token = await asyncio.to_thread(board_store.claim_import, who)
    if token is None:
        return None
    try:
        if len(attachments) != 1:
            raise ToolError("请只上传一个 Soshage 导出的 .json 文件；等待状态仍然有效。")
        raw_document = await fetch_qq_export(attachments[0], transport=transport)
        document = await asyncio.to_thread(board_store.replace, who, raw_document)
    except BaseException:
        await asyncio.to_thread(board_store.rearm_import, who, token)
        raise
    await asyncio.to_thread(board_store.finish_import, who, token)
    return _import_success(document)


def parse_command(raw: str) -> tuple[str, str]:
    command, _, rest = raw.strip().partition(" ")
    if command.casefold() not in IMPORT_ALIASES:
        raise ToolError("不是蜡笔板命令。")
    action_raw, separator, value = rest.lstrip().partition(" ")
    if not rest.strip():
        return "overview", ""
    action = ACTION_ALIASES.get(action_raw.casefold()) or ACTION_ALIASES.get(action_raw)
    if action is None:
        raise ToolError("不认识这个蜡笔板操作。发送 /tr 蜡笔板 帮助 查看用法。")
    return action, value.strip() if separator else ""


async def dispatch(
    store: Store,
    who: Identity,
    raw: str,
    *,
    attachments: list[Any] | None = None,
    transport=None,
) -> str:
    action, value = parse_command(raw)
    if action == "help":
        return help_text()
    if action == "web":
        if value:
            raise ToolError("用法：/tr 蜡笔板 网页")
        from .trickcal_web import BoardWeb
        web = BoardWeb(store)
        if who.private:
            link, expires = await asyncio.to_thread(web.issue, who)
            return panel("蜡笔板网页登录", [
                "这是只属于你的单次登录链接，10 分钟内有效。",
                link,
                "也可以创建独立的蜡笔板账号，以后直接使用账号密码登录。",
            ], icon="🌐", footer=f"链接将在 {time.strftime('%H:%M', time.localtime(expires))} 过期；不要转发给其他人。")
        link, request_code, expires = await asyncio.to_thread(web.issue_pairing, who)
        return panel("蜡笔板网页账号绑定", [
            "1. 打开蜡笔板网页：", link,
            f"2. 在“首次绑定”输入绑定码：{request_code}",
            "3. 网页会生成回执码；回到本群发送：",
            "/tr 蜡笔板 绑定 回执码",
            "4. 网页确认身份后即可创建账号，已有账号会直接登录。",
        ], icon="🌐", footer=f"绑定码将在 {time.strftime('%H:%M', time.localtime(expires))} 过期；第二段回执必须由你本人在当前群发送。")
    if action == "bind":
        if who.private or not who.scope.startswith("group:"):
            raise ToolError("请在需要使用蜡笔板的 QQ 群完成绑定。")
        if not value or len(value.split()) != 1:
            raise ToolError("用法：/tr 蜡笔板 绑定 网页显示的绑定码")
        from .trickcal_web import BoardWeb
        web = BoardWeb(store)
        linked = await asyncio.to_thread(web.redeem_bot_pairing, who, value)
        if linked:
            return panel("新机器人绑定成功", [
                "当前机器人现在可以访问你的原有蜡笔板。",
                "网页或任一已绑定机器人中的修改都会使用同一份数据。",
            ], icon="✅", footer="绑定码已经失效；不要把新的绑定码转发给其他人。")
        await asyncio.to_thread(web.confirm_pairing, who, value)
        return panel("网页身份已确认", [
            "请回到刚才的网页，页面会自动继续。",
            "首次使用请创建蜡笔板账号；已有账号会直接登录。",
        ], icon="✅")
    board_store = BoardStore(store)
    if action == "import":
        attachments = attachments or []
        if value.casefold() in {"取消", "cancel"} and not attachments:
            cancelled = await asyncio.to_thread(board_store.cancel_import, who)
            if not cancelled:
                raise ToolError("当前会话没有等待上传的蜡笔板文件。")
            return panel("已取消蜡笔板导入", "之后上传的文件不会被机器人自动导入。", icon="✅")
        if value and attachments:
            raise ToolError("请只选择一种导入方式：粘贴 JSON，或附上一个 JSON 文件。")
        if len(attachments) > 1:
            raise ToolError("一次只能导入一个 JSON 文件。")
        raw_document = value
        if attachments:
            raw_document = await fetch_qq_export(attachments[0], transport=transport)
        if not raw_document:
            await asyncio.to_thread(board_store.begin_import, who)
            return panel("等待上传蜡笔板文件", [
                "请将下一条消息直接发送为 Soshage 导出的一个 .json 文件。",
                "文件必须由刚才发送命令的你，在当前会话中上传。",
                "等待时间：10 分钟；下一条不是 .json 会自动取消。",
            ], icon="📥", footer="私聊和群聊都支持。也可手动取消：/tr 蜡笔板 导入 取消")
        document = await asyncio.to_thread(board_store.replace, who, raw_document)
        return _import_success(document)
    if action == "delete":
        if value not in {"确认", "confirm"}:
            raise ToolError("删除后无法从机器人恢复。确定请发送：/tr 蜡笔板 删除 确认")
        deleted = await asyncio.to_thread(board_store.delete, who)
        if not deleted:
            raise ToolError("你还没有保存蜡笔板数据。")
        return panel("蜡笔板记录已删除", "只删除了你自己的导入记录。", icon="🗑️")
    if action == "overview":
        if value:
            raise ToolError("用法：/tr 蜡笔板")
        return await _overview(board_store, who, transport)
    if action == "unlock":
        return await _unlock(board_store, who, value, transport)
    if action == "unowned":
        return await _unowned_list(board_store, who, value, transport)
    if action == "remove_unit":
        return await _remove_owned(board_store, who, value, transport)
    if action == "mark":
        return await _mark(board_store, who, value, selected=True, transport=transport)
    if action == "unmark":
        return await _mark(board_store, who, value, selected=False, transport=transport)
    if action == "units":
        return await _unit_list(board_store, who, value, transport)
    if action == "unit":
        return await _unit_detail(board_store, who, value, transport)
    if action == "plan":
        return await _plan(board_store, who, value, transport)
    if action == "missing":
        return await _missing(board_store, who, value, transport)
    raise ToolError("不认识这个蜡笔板操作。")
