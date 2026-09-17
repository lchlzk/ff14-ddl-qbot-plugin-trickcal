"""Read-only Trickcal client: current GameKee data with a BWIKI fallback."""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
from weakref import WeakKeyDictionary

import httpx

from message_ui import help_panel, panel
from bot_tools import http_clients
from bot_tools.chinese_search import simplify_role_name
from bot_tools.request_cache import ResponseCache, singleflight
from . import trickcal_media


API_URL = "https://wiki.biligame.com/tk/api.php"
PAGE_URL = "https://wiki.biligame.com/tk/"
GAMEKEE_BASE = "https://www.gamekee.com"
GAMEKEE_PAGE = GAMEKEE_BASE + "/tr/"
USER_AGENT = "NoneBot-QQ-Trickcal/1.3 (read-only public wiki client)"
MAX_RESPONSE_BYTES = 768 * 1024
GAMEKEE_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CHARACTER_CARD_CACHE_TTL = 7 * 24 * 3600
CHARACTER_CARD_CACHE_MAX_BYTES = 64 * 1024 * 1024
CHARACTER_CARD_CACHE_MAX_FILES = 512
CHARACTER_CARD_CACHE_VERSION = 2
CHARACTER_LONG_CARD_CACHE_VERSION = 3
CST = timezone(timedelta(hours=8))
KINDS = {
    "角色": "使徒图鉴", "卡牌": "卡牌图鉴", "宠物": "宠物图鉴", "食物": "食物图鉴",
}
GAMEKEE_ROOTS = {"角色": 127885, "卡牌": 134671, "宠物": 150349}
ROLE_REGION_GROUPS = {"国服": "国际服", "韩服": "韩服"}
ROLE_REGION_ALIASES = {"国服": "国服", "国际服": "国服", "韩服": "韩服"}
ROLE_NAME_MAP_FILE = Path(__file__).with_name("trickcal_role_aliases.json")
ALIASES = {
    "角色": "角色", "使徒": "角色", "char": "角色", "character": "角色",
    "卡牌": "卡牌", "神器": "卡牌", "card": "卡牌", "宠物": "宠物", "pet": "宠物",
    "食物": "食物", "food": "食物", "攻略": "攻略", "guide": "攻略",
    "兑换码": "兑换码", "礼包码": "兑换码", "code": "兑换码", "codes": "兑换码",
    "搜索": "搜索", "search": "搜索", "随机": "随机角色", "随机角色": "随机角色",
    "random": "随机角色",
}
FIELD_LABELS = {
    "使徒名称", "稀有度", "性格", "职业", "攻击类型", "站位", "种族", "称号", "TMI",
    "角色描述", "最喜欢的东西", "最讨厌的东西", "卡牌名称", "卡牌类型", "宠物名称",
    "食物名称", "效果", "获取方式", "喜好度", "说明", "描述",
}
_cache = ResponseCache(max_bytes=8 * 1024 * 1024, max_entries=160)
_rate_states: WeakKeyDictionary = WeakKeyDictionary()


class TrickcalError(ValueError):
    """Safe message suitable for a QQ reply."""


class GameKeeUnavailable(TrickcalError):
    """GameKee could not be read, so callers may use the BWIKI fallback."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    snippet: str
    timestamp: str = ""


@dataclass(frozen=True)
class ParsedWiki:
    tables: tuple[tuple[tuple[str, ...], ...], ...]
    headings: tuple[str, ...]
    paragraphs: tuple[str, ...]


@dataclass(frozen=True)
class WikiPage:
    title: str
    revision: int
    updated: str
    parsed: ParsedWiki

    @property
    def url(self) -> str:
        return PAGE_URL + quote(self.title, safe="")


@dataclass(frozen=True)
class GameKeeEntry:
    entry_id: int
    content_id: int
    name: str
    aliases: str
    group: str
    updated_at: int

    @property
    def url(self) -> str:
        return f"{GAMEKEE_PAGE}{self.content_id}.html"


def _text(value: object, limit: int = 500) -> str:
    value = str(value or "").replace("\\n", " ").replace("\\r", " ")
    value = html.unescape(re.sub(r"<[^>]*>", " ", value))
    value = " ".join(value.split())
    return "".join(char for char in value if char.isprintable())[:limit]


def _normal(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace(" ", "")
    return simplify_role_name(normalized)


def _load_role_name_groups(path: Path = ROLE_NAME_MAP_FILE) -> tuple[tuple[str, ...], ...]:
    """Load editable Soshage/GameKee name equivalence classes."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取角色名称映射文件：{path}") from exc
    mappings = document.get("角色映射") if isinstance(document, dict) else None
    if not isinstance(mappings, dict) or not mappings:
        raise RuntimeError(f"角色名称映射文件格式不正确：{path}")
    groups: list[tuple[str, ...]] = []
    owners: dict[str, str] = {}
    for resource_name, aliases in mappings.items():
        if not isinstance(resource_name, str) or not resource_name.strip():
            raise RuntimeError(f"角色名称映射包含无效资源名：{path}")
        if not isinstance(aliases, list) or not aliases:
            raise RuntimeError(f"角色 {resource_name} 必须至少填写一个名称：{path}")
        names = tuple(dict.fromkeys(
            value.strip() for value in (resource_name, *aliases)
            if isinstance(value, str) and value.strip()
        ))
        if len(names) < 2:
            raise RuntimeError(f"角色 {resource_name} 的名称映射不足：{path}")
        for name in names:
            normalized = _normal(name)
            previous = owners.setdefault(normalized, resource_name)
            if previous != resource_name:
                raise RuntimeError(
                    f"角色名称“{name}”同时属于 {previous} 和 {resource_name}：{path}"
                )
        groups.append(names)
    return tuple(groups)


ROLE_NAME_GROUPS = _load_role_name_groups()


def role_name_group(query: str) -> tuple[str, ...]:
    """Return every configured name for the same Soshage character."""
    needle = _normal(query)
    for group in ROLE_NAME_GROUPS:
        if any(_normal(name) == needle for name in group):
            return group
    return ()


def _role_equivalents(query: str) -> tuple[str, ...]:
    needle = _normal(query)
    return tuple(
        name for name in role_name_group(query) if _normal(name) != needle
    )


class _WikiParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[tuple[str, ...], ...]] = []
        self.headings: list[str] = []
        self.paragraphs: list[str] = []
        self.table_depth = 0
        self.skip_depth = 0
        self.current_table: list[tuple[str, ...]] | None = None
        self.current_row: list[str] | None = None
        self.cell_parts: list[str] | None = None
        self.block_tag = ""
        self.block_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "table":
            self.table_depth += 1
            if self.table_depth == 1:
                self.current_table = []
        elif tag == "tr" and self.table_depth == 1:
            self.current_row = []
        elif tag in {"td", "th"} and self.table_depth == 1 and self.current_row is not None:
            self.cell_parts = []
        elif not self.table_depth and tag in {"h1", "h2", "h3", "p", "li"}:
            self.block_tag, self.block_parts = tag, []
        elif tag == "br":
            if self.cell_parts is not None:
                self.cell_parts.append(" ")
            if self.block_parts is not None:
                self.block_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"td", "th"} and self.cell_parts is not None:
            value = _text("".join(self.cell_parts))
            if value and self.current_row is not None:
                self.current_row.append(value)
            self.cell_parts = None
        elif tag == "tr" and self.current_row is not None:
            if self.current_row and self.current_table is not None:
                self.current_table.append(tuple(self.current_row))
            self.current_row = None
        elif tag == "table" and self.table_depth:
            if self.table_depth == 1 and self.current_table:
                self.tables.append(tuple(self.current_table))
                self.current_table = None
            self.table_depth -= 1
        elif tag == self.block_tag and self.block_parts is not None:
            value = _text("".join(self.block_parts), 700)
            if value:
                target = self.headings if tag.startswith("h") else self.paragraphs
                if not target or target[-1] != value:
                    target.append(value)
            self.block_tag, self.block_parts = "", None

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.cell_parts is not None:
            self.cell_parts.append(data)
        elif self.block_parts is not None:
            self.block_parts.append(data)

    def result(self) -> ParsedWiki:
        return ParsedWiki(tuple(self.tables), tuple(self.headings), tuple(self.paragraphs))


def parse_html(source: str) -> ParsedWiki:
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise TrickcalError("Wiki 页面过大，已停止解析。")
    parser = _WikiParser()
    try:
        parser.feed(source)
        parser.close()
    except (ValueError, RecursionError):
        raise TrickcalError("Wiki 页面格式暂时无法解析。") from None
    return parser.result()


def _cache_get(key):
    entry = _cache.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    if entry:
        _cache.pop(key, None)
    return None


def _cache_put(key, value, ttl: float):
    _cache[key] = (time.monotonic() + ttl, value)


def _character_card_cache_dir() -> Path:
    return Path(os.environ.get("BOT_DATA_DIR", "data")) / "cache" / "trickcal-cards"


def _character_card_disk_key(content_id: int, updated_at: int, region: str) -> str:
    material = f"{CHARACTER_CARD_CACHE_VERSION}:{content_id}:{updated_at}:{region}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _read_character_card_cache(disk_key: str) -> bytes | None:
    path = _character_card_cache_dir() / f"{disk_key}.jpg"
    try:
        stat = path.stat()
        if time.time() - stat.st_mtime > CHARACTER_CARD_CACHE_TTL:
            path.unlink(missing_ok=True)
            return None
        if stat.st_size <= 0 or stat.st_size > 2 * 1024 * 1024:
            path.unlink(missing_ok=True)
            return None
        image = path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    return image if image.startswith(b"\xff\xd8\xff") else None


def _prune_character_card_cache(directory: Path) -> None:
    try:
        files = sorted(
            (item for item in directory.glob("*.jpg") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    now = time.time()
    kept_bytes = 0
    for index, path in enumerate(files):
        try:
            stat = path.stat()
            expired = now - stat.st_mtime > CHARACTER_CARD_CACHE_TTL
            over_limit = index >= CHARACTER_CARD_CACHE_MAX_FILES
            over_bytes = kept_bytes + stat.st_size > CHARACTER_CARD_CACHE_MAX_BYTES
            if expired or over_limit or over_bytes:
                path.unlink(missing_ok=True)
            else:
                kept_bytes += stat.st_size
        except OSError:
            continue


def _write_character_card_cache(disk_key: str, image: bytes) -> None:
    if not image.startswith(b"\xff\xd8\xff") or len(image) > 2 * 1024 * 1024:
        return
    directory = _character_card_cache_dir()
    path = directory / f"{disk_key}.jpg"
    temporary = directory / f".{disk_key}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(image)
        os.replace(temporary, path)
        _prune_character_card_cache(directory)
    except OSError:
        # A read-only/full data volume must not break the role query itself.
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def _pace(transport) -> None:
    if transport is not None:
        return
    loop = asyncio.get_running_loop()
    state = _rate_states.get(loop)
    if state is None:
        state = {"lock": asyncio.Lock(), "last": 0.0}
        _rate_states[loop] = state
    async with state["lock"]:
        delay = 1.0 - (loop.time() - state["last"])
        if delay > 0:
            await asyncio.sleep(delay)
        state["last"] = loop.time()


def _gamekee_path_ok(path: str) -> bool:
    return path in {"/v1/entry/treesByPid", "/v1/content/searchArticle"} or bool(
        re.fullmatch(r"/v1/content/detail/[1-9][0-9]{0,9}", path)
    )


@singleflight(lambda path, params=None, transport=None: (
    path, tuple(sorted((params or {}).items())), id(transport)
))
async def _gamekee_request(path: str, params: dict[str, str] | None = None, transport=None):
    """Read one of the small, fixed, anonymous endpoints used by GameKee itself."""
    if not _gamekee_path_ok(path):
        raise ValueError("unsupported GameKee path")
    params = dict(params or {})
    key = ("gamekee", path, tuple(sorted(params.items())), id(transport))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    await _pace(transport)
    options = dict(timeout=httpx.Timeout(14, connect=8), follow_redirects=False, trust_env=False)
    if transport is not None:
        options["transport"] = transport
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "game-alias": "tr",
        "device-num": "1",
        "Lang": "zh-cn",
    }
    try:
        async with http_clients.client("trickcal-gamekee", **options) as client:
            async with client.stream("GET", GAMEKEE_BASE + path, params=params,
                                     headers=headers) as response:
                if response.status_code in {403, 429}:
                    raise GameKeeUnavailable("GameKee 暂时限制了查询，请稍后再试。")
                if response.status_code >= 500:
                    raise GameKeeUnavailable("GameKee 服务暂时异常，请稍后再试。")
                if response.status_code != 200:
                    raise GameKeeUnavailable(f"GameKee 查询失败（HTTP {response.status_code}）。")
                if "json" not in response.headers.get("content-type", "").lower():
                    raise GameKeeUnavailable("GameKee 返回了安全验证页面，请稍后再试。")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > GAMEKEE_MAX_RESPONSE_BYTES:
                        raise GameKeeUnavailable("GameKee 返回内容过大，已停止下载。")
    except GameKeeUnavailable:
        raise
    except httpx.TransportError:
        raise GameKeeUnavailable("GameKee 连接超时或网络异常，请稍后再试。") from None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise GameKeeUnavailable("GameKee 返回了无法识别的数据。") from None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise GameKeeUnavailable("GameKee 返回格式不正确。")
    data = payload.get("data")
    ttl = 600 if path == "/v1/content/searchArticle" else (
        6 * 3600 if path.startswith("/v1/content/detail/") else 1800
    )
    _cache_put(key, data, ttl)
    return data


def _walk_nodes(value, wanted: str):
    if isinstance(value, list):
        for item in value:
            yield from _walk_nodes(item, wanted)
    elif isinstance(value, dict):
        if value.get("type") == wanted:
            yield value
        for child in value.values():
            yield from _walk_nodes(child, wanted)


def _visible_text(value, limit: int = 500) -> str:
    """Extract rendered editor text, excluding hidden popover/help data."""
    parts: list[str] = []

    def visit(node) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if isinstance(node.get("text"), str):
                parts.append(node["text"])
                return
            node_type = node.get("type")
            if node_type == "image":
                if node.get("alt"):
                    parts.append(str(node["alt"]))
                return
            if node_type in {"button", "paragraph"}:
                visit(node.get("children", []))
                return
            if node_type == "simpleEditor":
                visit(node.get("data", []))
                return
            if "children" in node:
                visit(node["children"])

    visit(value)
    return _text(" ".join(parts), limit)


def _visible_inline_text(value, limit: int = 500) -> str:
    """Extract one editor line without inserting spaces between styled spans."""
    parts: list[str] = []

    def visit(node) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            if isinstance(node.get("text"), str):
                parts.append(node["text"])
                return
            node_type = node.get("type")
            if node_type in {"image", "button"}:
                if node_type == "image" and node.get("alt"):
                    parts.append(str(node["alt"]))
                return
            if node_type == "simpleEditor":
                visit(node.get("data", []))
            elif "children" in node:
                visit(node["children"])

    visit(value)
    return _text("".join(parts), limit)


def _editor_paragraphs(value, limit: int = 560) -> tuple[str, ...]:
    """Keep GameKee editor paragraphs separate while enforcing one total limit."""
    nodes = value.get("data", []) if isinstance(value, dict) and value.get("type") == "simpleEditor" else value
    candidates = nodes if isinstance(nodes, list) else [nodes]
    result: list[str] = []
    remaining = limit
    for node in candidates:
        if remaining <= 0:
            break
        text = _visible_inline_text(node, remaining)
        if not text:
            continue
        result.append(text)
        remaining -= len(text)
    if not result:
        fallback = _visible_inline_text(value, limit)
        if fallback:
            result.append(fallback)
    return tuple(result)


def _skill_name_parts(value) -> tuple[str, str]:
    """Split the black skill title from the icon-following SP/cooldown badge."""
    data = value.get("data", []) if isinstance(value, dict) and value.get("type") == "simpleEditor" else []
    paragraph = next((node for node in data if isinstance(node, dict) and node.get("type") == "paragraph"), None)
    children = paragraph.get("children", []) if isinstance(paragraph, dict) else []
    title_parts: list[str] = []
    meta_parts: list[str] = []
    after_icon = False
    for child in children if isinstance(children, list) else []:
        if isinstance(child, dict) and child.get("type") == "image":
            after_icon = True
            continue
        text = _visible_inline_text(child, 120)
        if text:
            (meta_parts if after_icon else title_parts).append(text)
    title = _text("".join(title_parts), 180)
    meta = _text("".join(meta_parts), 100)
    # Some GameKee editors omit the separating icon and put the blue badge in
    # the same paragraph.  Text-field boundaries remain stable across both
    # layouts, so cut the title before SP/cooldown metadata as a second guard.
    detail_match = _SKILL_DETAIL_FIELD.search(title)
    if detail_match:
        if not meta:
            meta = _text(title[detail_match.start():], 100)
        title = _text(title[:detail_match.start()], 80)
    else:
        title = _text(title, 80)
    return (title or _visible_text(value, 80), meta)


_SKILL_DETAIL_FIELD = re.compile(
    r"(?:SP(?:需求|恢复)?量|冷却时间|(?:物理|魔法|总)?伤害(?:量)?|"
    r"(?:攻击力|防御力|HP|暴击|暴伤|暴抗|暴伤抗)(?:增加|减少|降低|提升|恢复)?(?:持续时间|量)?|"
    r"(?:增加|减少|降低|提升|恢复|控制|免疫)持续时间|"
    r"[\u4e00-\u9fffA-Za-z]{1,12}持续时间|发动概率|命中率)\s*[：:]",
    re.IGNORECASE,
)


_SKILL_NUMBER = re.compile(
    r"\d+(?:\.\d+)?(?:\s*[~～\-—至]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:%|/秒|秒|名|个|次|枚|层|回合|格|点|SP)?",
    re.IGNORECASE,
)


def _without_skill_numbers(text: str) -> str:
    """Remove exact values while leaving a readable description of the effect."""
    value = re.sub(
        r"(?:，|、)?\s*(?:持续|冷却|每隔)\s*"
        r"\d+(?:\.\d+)?(?:\s*[~～\-—至]\s*\d+(?:\.\d+)?)?\s*(?:秒|回合)",
        "", text,
    )
    value = _SKILL_NUMBER.sub("", value)
    value = re.sub(r"\s+([，。；：、！？])", r"\1", value)
    value = re.sub(r"([，；、])\s*([。；])", r"\2", value)
    value = re.sub(r"\s{2,}", " ", value)
    return value.strip(" ，,；;")


def _skill_effect_paragraphs(value, limit: int = 420) -> tuple[str, ...]:
    """Keep skill behaviour while dropping GameKee's numeric detail rows."""
    result: list[str] = []
    remaining = limit
    for paragraph in _editor_paragraphs(value, limit=limit):
        match = _SKILL_DETAIL_FIELD.search(paragraph)
        effect = paragraph[:match.start()].rstrip(" ；;，,") if match else paragraph
        if match and len(effect) <= 2:
            effect = ""
        effect = _text(_without_skill_numbers(effect), remaining)
        if not effect:
            continue
        result.append(effect)
        remaining -= len(effect)
        if remaining <= 0:
            break
    return tuple(result)


def _epoch_date(value: object) -> str:
    try:
        number = int(value or 0)
        if number > 0:
            return datetime.fromtimestamp(number, CST).strftime("%Y-%m-%d")
    except (OverflowError, OSError, TypeError, ValueError):
        pass
    return ""


def _flatten_entries(rows, parent: str = "") -> tuple[GameKeeEntry, ...]:
    result: list[GameKeeEntry] = []
    if not isinstance(rows, list):
        return ()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"), 100)
        group = parent or name
        try:
            content_id = int(row.get("content_id") or 0)
            entry_id = int(row.get("id") or 0)
            updated_at = int(row.get("bind_updated") or row.get("updated_at") or 0)
        except (TypeError, ValueError):
            continue
        if content_id and entry_id and name:
            result.append(GameKeeEntry(entry_id, content_id, name,
                                        _text(row.get("name_alias"), 180), parent, updated_at))
        result.extend(_flatten_entries(row.get("child"), group))
    return tuple(result)


async def _gamekee_entries(kind: str, *, transport=None) -> tuple[GameKeeEntry, ...]:
    root = GAMEKEE_ROOTS[kind]
    data = await _gamekee_request("/v1/entry/treesByPid", {"pid": str(root)}, transport)
    entries = _flatten_entries(data)
    if not entries:
        raise GameKeeUnavailable(f"GameKee 的{kind}目录暂时为空。")
    return entries


def _entry_terms(entry: GameKeeEntry) -> tuple[str, ...]:
    values = [entry.name, entry.aliases]
    values.extend(re.split(r"[/／,，;；]", entry.name + "," + entry.aliases))
    return tuple(value.strip() for value in values if value and value.strip())


def _entry_matches(entries: tuple[GameKeeEntry, ...], query: str) -> tuple[GameKeeEntry, ...]:
    needle = _normal(query)
    exact, partial = [], []
    for entry in entries:
        terms = tuple(_normal(term) for term in _entry_terms(entry))
        if needle in terms:
            exact.append(entry)
        elif any(needle in term for term in terms):
            partial.append(entry)
    candidates = exact or partial
    return tuple(sorted(candidates, key=lambda item: (
        0 if any(_normal(term) == needle for term in re.split(r"[/／]", item.name)) else 1,
        len(item.name), item.name,
    )))


def _entry_match_buckets(entries: tuple[GameKeeEntry, ...], query: str
                         ) -> tuple[tuple[GameKeeEntry, ...], tuple[GameKeeEntry, ...]]:
    needle = _normal(query)
    exact, partial = [], []
    for entry in entries:
        terms = tuple(_normal(term) for term in _entry_terms(entry))
        if needle in terms:
            exact.append(entry)
        elif any(needle in term for term in terms):
            partial.append(entry)
    key = lambda item: (
        0 if any(_normal(term) == needle for term in re.split(r"[/／]", item.name)) else 1,
        len(item.name), item.name,
    )
    return tuple(sorted(exact, key=key)), tuple(sorted(partial, key=key))


def _split_role_region(query: str) -> tuple[str, str]:
    """A missing suffix means the international/Chinese release (国服)."""
    query = " ".join(query.split())
    name, separator, suffix = query.rpartition(" ")
    region = ROLE_REGION_ALIASES.get(suffix) if separator else None
    if region:
        query = name
    return validate_query(query), region or "国服"


def _region_entries(entries: tuple[GameKeeEntry, ...], region: str) -> tuple[GameKeeEntry, ...]:
    marker = ROLE_REGION_GROUPS[region]
    return tuple(entry for entry in entries if marker in entry.group)


def _role_matches(entries: tuple[GameKeeEntry, ...], query: str, region: str) -> tuple[GameKeeEntry, ...]:
    pool = _region_entries(entries, region)
    candidates = (query, *_role_equivalents(query))
    exact: list[GameKeeEntry] = []
    partial: list[GameKeeEntry] = []
    for candidate in candidates:
        candidate_exact, candidate_partial = _entry_match_buckets(pool, candidate)
        exact.extend(candidate_exact)
        partial.extend(candidate_partial)
    # Check every equivalent name for an exact match before accepting a partial
    # result.  Never guess by one changed Chinese character: 涅尔 previously
    # became 马尔 in the international directory and blocked the Korean fallback.
    exact = list(dict.fromkeys(exact))
    partial = list(dict.fromkeys(partial))
    canonical_exact = [entry for entry in exact if _role_name_exact(entry, query)]
    return tuple(canonical_exact or exact or partial)


def _role_name_exact(entry: GameKeeEntry, query: str) -> bool:
    needles = {_normal(query), *(_normal(name) for name in _role_equivalents(query))}
    return any(_normal(term) in needles for term in re.split(r"[/／]", entry.name))


def _select_role_matches(entries: tuple[GameKeeEntry, ...], query: str,
                         region: str) -> tuple[tuple[GameKeeEntry, ...], str, bool]:
    matches = _role_matches(entries, query, region)
    if matches:
        return matches, region, False
    if region == "国服":
        korean = _role_matches(entries, query, "韩服")
        if korean:
            return korean, "韩服", True
    return (), region, False


def _role_not_found(entries: tuple[GameKeeEntry, ...], query: str, region: str) -> TrickcalError:
    other = "韩服" if region == "国服" else "国服"
    if _role_matches(entries, query, other):
        return TrickcalError(
            f"{region}资料暂未收录“{query}”；该角色可用 /tr 角色 {query} {other} 查询。"
        )
    return TrickcalError(f"GameKee 没有找到“{query}”对应的{region}角色资料。")


def _gamekee_result_panel(kind: str, query: str,
                           entries: tuple[GameKeeEntry, ...], region: str = "",
                           fallback: bool = False) -> str:
    if not entries:
        raise TrickcalError(f"GameKee 没有找到“{query}”对应的{kind}资料。")
    lines = []
    for index, entry in enumerate(entries[:6], 1):
        date = _epoch_date(entry.updated_at)
        lines.append(f"{index}. {entry.name}" + (f" · {date}" if date else ""))
        lines.append("   " + entry.url)
    note = "结果来自 GameKee 玩家 Wiki；输入更完整的名称可直接查看资料。"
    if region:
        note = (f"当前为{region}目录。" +
                ("国服暂无对应资料，已自动改查韩服。" if fallback else "") + note)
    return panel(f"嘟嘟脸 · {kind}搜索", lines, icon="🔎", footer=note)


def _profile_fields(document) -> tuple[dict[str, str], dict]:
    profiles = list(_walk_nodes(document, "character-profile"))
    if not profiles:
        return {}, {}
    profile = next((node for node in profiles
                    if _visible_text((node.get("data") or {}).get("title")) == "使徒信息"), profiles[0])
    data = profile.get("data") if isinstance(profile.get("data"), dict) else {}
    fields: dict[str, str] = {}
    for item in data.get("attrList") or []:
        if not isinstance(item, dict):
            continue
        label = _visible_text(item.get("title"), 40)
        value = _visible_text(item.get("content"), 240)
        if label and value:
            fields.setdefault(label, value)
    name = _visible_text(data.get("name"), 100)
    desc = _visible_text(data.get("desc"), 320)
    if name:
        fields.setdefault("名称", name)
    if desc:
        fields.setdefault("简介", desc)
    return fields, data


def _gamekee_skills(document) -> tuple[tuple[str, str], ...]:
    for node in _walk_nodes(document, "skill-info"):
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        if "技能" not in _text(data.get("title"), 60):
            continue
        result = []
        for item in data.get("skillList") or []:
            if not isinstance(item, dict):
                continue
            name, _badge = _skill_name_parts(item.get("name"))
            name = name or _text(item.get("label"), 80)
            paragraphs = _skill_effect_paragraphs(item.get("desc"))
            if name and paragraphs:
                result.append((name, "\n".join(paragraphs)))
        if len(result) <= 4:
            return tuple(result)
        normal_attack = next(
            (item for item in result if _normal(item[0]) in {"普通攻击", "一般攻击", "基本攻击"}),
            None,
        )
        selected = result[:4]
        if normal_attack is not None and normal_attack not in selected:
            selected = [*result[:3], normal_attack]
        return tuple(selected)
    return ()


def _gamekee_detail_panel(kind: str, entry: GameKeeEntry, detail: dict,
                           region: str = "", region_fallback: bool = False) -> str:
    raw = detail.get("content_json")
    document = []
    if isinstance(raw, str) and raw:
        if len(raw.encode("utf-8")) > GAMEKEE_MAX_RESPONSE_BYTES:
            raise GameKeeUnavailable("GameKee 角色资料过大，已停止解析。")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            document = []
    fields, _ = _profile_fields(document)
    lines: list[str] = []
    rarity = next(iter(re.findall(r"[123]星", entry.group)), "")
    if kind == "角色":
        if region:
            if region_fallback:
                lines.append("数据区服：韩服（国服暂无该角色，已自动使用韩服资料）")
            else:
                lines.append(f"数据区服：{region}" + ("（GameKee 国际服）" if region == "国服" else ""))
        summary = " · ".join(filter(None, (
            rarity, fields.get("性格"), fields.get("职业"), fields.get("攻击类型")
        )))
        detail_line = " · ".join(filter(None, (fields.get("站位"), fields.get("种族"))))
        if summary:
            lines.append(summary)
        if detail_line:
            lines.append(detail_line)
        for label in ("称号", "TMI", "TAG", "最喜欢的东西", "一句话", "简介"):
            value = fields.get(label)
            compact_value = re.sub(r"[\s：:，,。.!！]+", "", value or "").casefold()
            is_placeholder = label == "TAG" and compact_value == "不同tag用不同颜色区分"
            if value and not is_placeholder:
                shown = "最喜欢" if label == "最喜欢的东西" else label
                lines.append(f"{shown}：{value}")
        skills = _gamekee_skills(document)
        if skills:
            lines.extend(("", "技能摘要"))
            for index, (name, desc) in enumerate(skills):
                if index:
                    lines.append("")
                paragraphs = [line.strip() for line in desc.splitlines() if line.strip()]
                if paragraphs:
                    lines.append(f"• {name}：{paragraphs[0]}")
                    lines.extend(f"  {paragraph}" for paragraph in paragraphs[1:])
    else:
        for label, value in list(fields.items())[:8]:
            if label != "名称":
                lines.append(f"{label}：{value}")
    if not lines:
        summary = _text(detail.get("summary") or detail.get("desc"), 500)
        if summary:
            lines.append(summary)
        else:
            lines.append("已找到资料页；该页以图片或特殊组件为主，请打开来源查看完整内容。")
    updated = _epoch_date(detail.get("updated_at") or entry.updated_at)
    source = "GameKee" + (f" · 更新 {updated}" if updated else "")
    title = _text(detail.get("title") or entry.name, 100)
    return panel(f"嘟嘟脸 · {title}", lines, icon="🍞",
                 footer=f"玩家维护资料，可能存在延迟或错误。{source}\n{entry.url}")


async def _gamekee_lookup(kind: str, query: str, *, transport=None) -> str:
    entries = await _gamekee_entries(kind, transport=transport)
    region = ""
    if kind == "角色":
        query, region = _split_role_region(query)
        matches, actual_region, region_fallback = _select_role_matches(entries, query, region)
    else:
        matches = _entry_matches(entries, query)
        actual_region, region_fallback = "", False
    if not matches:
        if kind == "角色":
            raise _role_not_found(entries, query, region)
        raise TrickcalError(f"GameKee 没有找到“{query}”对应的{kind}资料。")
    first = matches[0]
    exact_name = _role_name_exact(first, query) if kind == "角色" else any(
        _normal(term) == _normal(query) for term in re.split(r"[/／]", first.name)
    )
    if len(matches) > 1 and not exact_name:
        return _gamekee_result_panel(kind, query, matches, actual_region, region_fallback)
    detail = await _gamekee_request(f"/v1/content/detail/{first.content_id}", transport=transport)
    if not isinstance(detail, dict):
        raise GameKeeUnavailable("GameKee 资料页返回格式不正确。")
    return _gamekee_detail_panel(kind, first, detail, actual_region, region_fallback)


async def _gamekee_articles(query: str, limit: int = 8, *, transport=None) -> tuple[dict, ...]:
    limit = max(1, min(int(limit), 50))
    data = await _gamekee_request(
        "/v1/content/searchArticle", {"keyword": query, "limit": str(limit)}, transport
    )
    if not isinstance(data, list):
        raise GameKeeUnavailable("GameKee 搜索结果格式不正确。")
    needle = _normal(query)
    rows = []
    for row in data:
        if not isinstance(row, dict):
            continue
        title = _text(row.get("title"), 120)
        summary = _text(row.get("summary"), 500)
        if needle not in _normal(title + summary):
            continue
        try:
            content_id = int(row.get("id") or 0)
            updated_at = int(row.get("updated_at") or row.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        if content_id and title:
            rows.append({"id": content_id, "title": title, "summary": summary,
                         "updated_at": updated_at})
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    return tuple(rows[:limit])


def _gamekee_article_panel(title: str, rows: tuple[dict, ...]) -> str:
    if not rows:
        raise TrickcalError("GameKee 没有找到相关资料，可以换一个关键词。")
    lines = []
    for index, row in enumerate(rows[:5], 1):
        date = _epoch_date(row["updated_at"])
        lines.append(f"{index}. {row['title']}" + (f" · {date}" if date else ""))
        if row["summary"]:
            lines.append("   " + row["summary"][:100])
        lines.append(f"   {GAMEKEE_PAGE}{row['id']}.html")
    return panel(title, lines, icon="🔎",
                 footer="结果来自 GameKee 玩家 Wiki，按更新时间排序。")


def _coupon_code_tokens(text: str) -> tuple[str, ...]:
    """Read code-shaped values without treating article prose as a code."""
    cleaned = _text(text, 800).strip()
    values: list[str] = []
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{4,31}", cleaned):
        values.append(cleaned)
    for match in re.finditer(
        r"(?:兑换码|礼包码)\s*[：:【\[]\s*([A-Za-z0-9][A-Za-z0-9_-]{4,31})",
        cleaned,
    ):
        values.append(match.group(1))
    return tuple(dict.fromkeys(values))


def _coupon_expiration(text: str) -> str:
    cleaned = _text(text, 800)
    if "另行通知" in cleaned:
        return "另行通知"
    if "永久有效" in cleaned or "长期有效" in cleaned:
        return "长期有效"
    timezone_note = "（北京时间）" if "北京时间" in cleaned else ""
    full_dates = re.findall(
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日(?:[^\d]{0,12}(\d{1,2}:\d{2}))?",
        cleaned,
    )
    if full_dates:
        year, month, day, clock = full_dates[-1]
        return f"{year}年{int(month)}月{int(day)}日" + (
            f" {clock}" if clock else ""
        ) + timezone_note
    chinese_dates = re.findall(
        r"(?<!年)(\d{1,2})月\s*(\d{1,2})日(?:[^\d]{0,12}(\d{1,2}:\d{2}))?",
        cleaned,
    )
    if chinese_dates:
        month, day, clock = chinese_dates[-1]
        return f"{int(month)}月{int(day)}日" + (
            f" {clock}" if clock else ""
        ) + timezone_note
    slash_dates = re.findall(
        r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?:\s*(\d{1,2}:\d{2}))?",
        cleaned,
    )
    if slash_dates:
        month, day, clock = slash_dates[-1]
        return f"{int(month)}月{int(day)}日" + (
            f" {clock}" if clock else ""
        ) + timezone_note
    return ""


def _parse_coupon_codes(document, region: str, limit: int = 10) -> tuple[tuple[str, str], ...]:
    """Parse the maintained GameKee list, which has explicit server headings."""
    if not isinstance(document, list):
        return ()
    current_region = ""
    records: list[list[str]] = []
    seen: set[str] = set()
    last_code_indexes: list[int] = []
    waiting_for_expiration = False
    for node in document:
        if not isinstance(node, dict):
            continue
        text = _visible_text(node, 800).strip()
        node_type = str(node.get("type") or "")
        if node_type.startswith("header") and text in {"国服", "韩服", "国际服"}:
            current_region = text
            last_code_indexes = []
            waiting_for_expiration = False
            continue
        if current_region != region or not text:
            continue
        codes = _coupon_code_tokens(text)
        if codes:
            last_code_indexes = []
            waiting_for_expiration = False
            for code in codes:
                normalized = code.casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                records.append([code, "未注明"])
                last_code_indexes.append(len(records) - 1)
        expiration = _coupon_expiration(text)
        has_expiration_label = any(
            marker in text for marker in ("使用期限", "领取时间", "有效期", "截止时间")
        )
        if expiration and (codes or waiting_for_expiration or has_expiration_label):
            for index in last_code_indexes:
                records[index][1] = expiration
            waiting_for_expiration = False
        elif has_expiration_label:
            waiting_for_expiration = True
    return tuple((code, expiration) for code, expiration in records[:max(1, min(limit, 10))])


async def _gamekee_coupon_panel(region: str, *, transport=None) -> str:
    rows = await _gamekee_articles("国服兑换码", 20, transport=transport)
    index = next((row for row in rows if _normal(row["title"]) == _normal("最新兑换码")), None)
    if index is None:
        raise GameKeeUnavailable("GameKee 的最新兑换码总表暂时不可用。")
    detail = await _gamekee_request(f"/v1/content/detail/{index['id']}", transport=transport)
    if not isinstance(detail, dict):
        raise GameKeeUnavailable("GameKee 的最新兑换码总表格式不正确。")
    raw_document = detail.get("content_json")
    if not isinstance(raw_document, str) or len(raw_document.encode("utf-8")) > GAMEKEE_MAX_RESPONSE_BYTES:
        raise GameKeeUnavailable("GameKee 的最新兑换码总表过大或格式不正确。")
    try:
        document = json.loads(raw_document)
    except json.JSONDecodeError:
        raise GameKeeUnavailable("GameKee 的最新兑换码总表格式不正确。") from None
    codes = _parse_coupon_codes(document, region, 10)
    if not codes:
        raise TrickcalError(f"GameKee 当前没有收录{region}兑换码。")
    lines: list[str] = []
    for index, (code, expiration) in enumerate(codes, 1):
        lines.extend((f"{index}. {code}", f"   过期：{expiration}"))
    return panel(f"{region}兑换码 · 最新 {len(codes)} 个", lines, icon="🔑")


@singleflight(lambda params, transport=None: (tuple(sorted(params.items())), id(transport)))
async def _request_json(params: dict[str, str], transport=None) -> dict:
    key = tuple(sorted(params.items()))
    cached = _cache_get(key)
    if cached is not None:
        return cached
    await _pace(transport)
    options = dict(timeout=httpx.Timeout(12, connect=8), follow_redirects=False, trust_env=False)
    if transport is not None:
        options["transport"] = transport
    try:
        async with http_clients.client("trickcal-bwiki", **options) as client:
            async with client.stream("GET", API_URL, params=params,
                                     headers={"Accept": "application/json", "User-Agent": USER_AGENT}) as response:
                if response.status_code in {403, 429}:
                    raise TrickcalError("嘟嘟脸 Wiki 暂时限制了查询，请稍后再试。")
                if response.status_code >= 500:
                    raise TrickcalError("嘟嘟脸 Wiki 服务暂时异常，请稍后再试。")
                if response.status_code != 200:
                    raise TrickcalError(f"嘟嘟脸 Wiki 查询失败（HTTP {response.status_code}）。")
                content_type = response.headers.get("content-type", "").lower()
                if "json" not in content_type:
                    raise TrickcalError("嘟嘟脸 Wiki 返回了安全验证页面，请稍后再试。")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise TrickcalError("嘟嘟脸 Wiki 返回内容过大，已停止下载。")
    except TrickcalError:
        raise
    except httpx.TransportError:
        raise TrickcalError("嘟嘟脸 Wiki 连接超时或网络异常，请稍后再试。") from None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TrickcalError("嘟嘟脸 Wiki 返回了无法识别的数据。") from None
    if not isinstance(payload, dict):
        raise TrickcalError("嘟嘟脸 Wiki 返回格式不正确。")
    error = payload.get("error")
    if isinstance(error, dict):
        code = _text(error.get("code"), 40)
        if code == "missingtitle":
            raise TrickcalError("没有找到这个 Wiki 页面。")
        raise TrickcalError("嘟嘟脸 Wiki 拒绝了本次查询。")
    _cache_put(key, payload, 900 if params.get("list") == "search" else 6 * 3600)
    return payload


async def search(query: str, limit: int = 8, *, transport=None) -> tuple[SearchResult, ...]:
    query = validate_query(query)
    payload = await _request_json({
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": str(max(1, min(limit, 10))), "srnamespace": "0",
        "srprop": "timestamp|snippet", "format": "json", "formatversion": "2",
    }, transport)
    rows = ((payload.get("query") or {}).get("search") or [])
    results = []
    for row in rows[:10]:
        if not isinstance(row, dict):
            continue
        title = _text(row.get("title"), 80)
        if title:
            results.append(SearchResult(title, _text(row.get("snippet"), 180),
                                        _text(row.get("timestamp"), 40)))
    return tuple(results)


async def fetch_page(title: str, updated: str = "", *, transport=None) -> WikiPage:
    title = validate_query(title, 80)
    payload = await _request_json({
        "action": "parse", "page": title, "redirects": "1",
        "prop": "text|revid", "format": "json", "formatversion": "2",
    }, transport)
    parsed = payload.get("parse")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("text"), str):
        raise TrickcalError("没有找到这个 Wiki 页面。")
    final_title = _text(parsed.get("title") or title, 80)
    return WikiPage(final_title, int(parsed.get("revid") or 0), updated,
                    parse_html(parsed["text"]))


def validate_query(query: str, limit: int = 40) -> str:
    query = " ".join(str(query).split())
    if not query or len(query) > limit or any(ord(char) < 32 for char in query):
        raise TrickcalError(f"查询内容不能为空，且不能超过 {limit} 个字。")
    return query


def _fields(parsed: ParsedWiki) -> dict[str, str]:
    result: dict[str, str] = {}
    for table in parsed.tables:
        for row in table:
            if len(row) == 2 and row[0] in FIELD_LABELS:
                result.setdefault(row[0], row[1])
            elif len(row) >= 4:
                for index in range(0, len(row) - 1, 2):
                    if row[index] in FIELD_LABELS:
                        result.setdefault(row[index], row[index + 1])
    return result


def _skills(parsed: ParsedWiki) -> tuple[tuple[str, str], ...]:
    skills = []
    seen = set()
    for table in parsed.tables:
        values = {row[0]: row[1] for row in table if len(row) == 2}
        name = values.get("技能名称", "")
        description = values.get("技能描述", "")
        effect = values.get("技能效果", "")
        key = (name, description, effect)
        if not description or key in seen:
            continue
        seen.add(key)
        label = name or "被动/普通攻击"
        body = description + (("；" + effect) if effect else "")
        skills.append((label, body))
        if len(skills) == 3:
            break
    return tuple(skills)


def page_kind(page: WikiPage) -> str:
    fields = _fields(page.parsed)
    if "使徒名称" in fields:
        return "角色"
    for kind, marker in (("卡牌", "卡牌"), ("宠物", "宠物"), ("食物", "食物")):
        if f"{marker}名称" in fields or any(marker + "图鉴" in text for text in page.parsed.paragraphs):
            return kind
    return "页面"


def _updated(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(CST).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def format_page(page: WikiPage, expected: str = "") -> str:
    kind = page_kind(page)
    if expected in KINDS and kind != expected:
        raise TrickcalError(f"“{page.title}”不是{expected}资料页。")
    fields = _fields(page.parsed)
    lines: list[str] = []
    if kind == "角色":
        summary = " · ".join(filter(None, (fields.get("稀有度"), fields.get("性格"), fields.get("职业"))))
        detail = " · ".join(filter(None, (fields.get("攻击类型"), fields.get("站位"), fields.get("种族"))))
        if summary:
            lines.append(summary)
        if detail:
            lines.append(detail)
        if fields.get("称号"):
            lines.append("称号：" + fields["称号"])
        if fields.get("角色描述"):
            lines.append("简介：" + _text(fields["角色描述"], 240))
        skills = _skills(page.parsed)
        if skills:
            lines.extend(("", "技能"))
            for name, description in skills:
                lines.append(f"• {name}：{_text(description, 190)}")
    else:
        for label in ("卡牌名称", "卡牌类型", "宠物名称", "食物名称", "效果", "喜好度", "获取方式", "说明", "描述"):
            value = fields.get(label)
            if value and value != page.title:
                lines.append(f"{label}：{_text(value, 260)}")
        if not lines:
            candidates = [p for p in page.parsed.paragraphs if len(p) > 8 and "Ctrl+D" not in p and "WIKI" not in p]
            lines.extend(_text(value, 350) for value in candidates[:3])
    if not lines:
        lines.append("页面存在，但暂时没有可整理的文字资料。")
    date = _updated(page.updated)
    source = "BWIKI"
    if date:
        source += " · 更新 " + date
    elif page.revision:
        source += f" · 修订 {page.revision}"
    return panel(f"嘟嘟脸 · {page.title}", lines, icon="🍞",
                 footer=f"玩家维护资料，可能存在延迟或错误。{source}\n{page.url}")


def format_results(title: str, results: tuple[SearchResult, ...]) -> str:
    if not results:
        raise TrickcalError("没有找到相关资料，可以换一个完整名称或关键词。")
    lines = []
    for row in results[:6]:
        date = _updated(row.timestamp)
        lines.append("• " + row.title + ((" · " + date) if date else ""))
        if row.snippet:
            lines.append("  " + row.snippet[:110])
    return panel(title, lines, icon="🔎",
                 footer="发送 /tr 搜索 关键词；资料来自玩家维护的嘟嘟脸 BWIKI。")


def _kind_hint(result: SearchResult, kind: str) -> bool:
    marker = KINDS[kind]
    return marker in result.snippet or result.title == marker


async def lookup(kind: str, query: str, *, transport=None) -> str:
    results = await search(query, transport=transport)
    candidates = tuple(row for row in results if _kind_hint(row, kind))
    exact = next((row for row in candidates if _normal(row.title) == _normal(query)), None)
    if exact is None and len(candidates) == 1:
        exact = candidates[0]
    if exact is None:
        if candidates:
            return format_results(f"{kind}搜索结果", candidates)
        raise TrickcalError(f"没有找到“{query}”对应的{kind}资料。")
    page = await fetch_page(exact.title, exact.timestamp, transport=transport)
    return format_page(page, kind)


def parse_command(raw: str) -> tuple[str, str]:
    raw = " ".join(raw.split())
    if not raw or raw.casefold() == "help":
        return "帮助", ""
    first, _, rest = raw.partition(" ")
    action = ALIASES.get(first.casefold()) or ALIASES.get(first)
    if action is None:
        return "角色", validate_query(raw)
    if action == "兑换码":
        if rest and rest not in {"国服", "韩服"}:
            raise TrickcalError("用法：/tr 兑换码 [国服|韩服]")
        return action, rest or "国服"
    if action == "随机角色":
        if rest and rest not in ROLE_REGION_ALIASES:
            raise TrickcalError("用法：/tr 随机角色 [国服|韩服]")
        return action, ROLE_REGION_ALIASES.get(rest, "国服")
    return action, validate_query(rest)


def directory() -> str:
    return help_panel("嘟嘟脸恶作剧 · 查询插件", [
        "/tr 角色 埃尔芬 [国服|韩服] · 默认国服",
        "角色结果附带缩小后的立绘、喜欢食物与蜡笔加成图",
        "/tr 神器 名称（/tr 卡牌 同义）· /tr 宠物 名称",
        "/tr 食物 名称 · 旧资料备用查询",
        "/tr 攻略 关键词 · 攻略搜索",
        "/tr 兑换码 [国服|韩服] · 默认国服，最多 10 个",
        "/tr 搜索 关键词 · 全站搜索",
        "/tr 随机角色 [国服|韩服] · 随机查看使徒",
        "/tr 蜡笔板 · 导入并追踪个人蜡笔板",
        "也可直接发送 /tr 埃尔芬。",
    ], footer="蜡笔板支持 Soshage 导出文件；其他资料优先 GameKee，故障时使用 BWIKI。不登录游戏。")


async def _random_role(*, transport=None) -> str:
    page = await fetch_page("使徒图鉴", transport=transport)
    names = []
    for table in page.parsed.tables:
        for row in table:
            if len(row) >= 7 and row[1] in {"1星", "2星", "3星"}:
                names.append(row[0])
    names = list(dict.fromkeys(names))
    if not names:
        raise TrickcalError("暂时无法从使徒图鉴取得角色列表。")
    return await lookup("角色", random.SystemRandom().choice(names), transport=transport)


async def _dispatch_bwiki(raw: str, *, transport=None) -> str:
    action, query = parse_command(raw)
    if action == "帮助":
        return directory()
    if action in KINDS:
        return await lookup(action, query, transport=transport)
    if action == "随机角色":
        return await _random_role(transport=transport)
    if action == "兑换码":
        page = await fetch_page("最新兑换码", transport=transport)
        return format_page(page)
    results = await search((query + " 攻略") if action == "攻略" else query, transport=transport)
    if action == "攻略":
        filtered = tuple(row for row in results if "攻略" in row.title or "攻略" in row.snippet)
        results = filtered or results
    return format_results("攻略搜索" if action == "攻略" else "Wiki 搜索", results)


async def _dispatch_gamekee(action: str, query: str, *, transport=None) -> str:
    if action in GAMEKEE_ROOTS:
        return await _gamekee_lookup(action, query, transport=transport)
    if action == "随机角色":
        entries = await _gamekee_entries("角色", transport=transport)
        region = ROLE_REGION_ALIASES.get(query, "国服")
        candidates = _region_entries(entries, region)
        if not candidates:
            raise GameKeeUnavailable(f"GameKee 的{region}角色目录暂时为空。")
        entry = random.SystemRandom().choice(candidates)
        detail = await _gamekee_request(f"/v1/content/detail/{entry.content_id}", transport=transport)
        if not isinstance(detail, dict):
            raise GameKeeUnavailable("GameKee 资料页返回格式不正确。")
        return _gamekee_detail_panel("角色", entry, detail, region)
    if action == "兑换码":
        return await _gamekee_coupon_panel(query, transport=transport)
    if action == "攻略":
        return _gamekee_article_panel("嘟嘟脸 · 攻略搜索",
                                       await _gamekee_articles(query, transport=transport))
    if action == "搜索":
        # GameKee's article search also includes current notices and event content.
        return _gamekee_article_panel("嘟嘟脸 · 全站搜索",
                                       await _gamekee_articles(query, transport=transport))
    raise GameKeeUnavailable("GameKee 暂未提供这一类结构化资料。")


async def dispatch(raw: str, *, transport=None) -> str:
    action, query = parse_command(raw)
    if action == "帮助":
        return directory()
    # GameKee's food page currently has no individual structured entries, so keep
    # the existing BWIKI lookup for this one legacy category.
    if action == "食物":
        return await _dispatch_bwiki(raw, transport=transport)
    try:
        return await _dispatch_gamekee(action, query, transport=transport)
    except GameKeeUnavailable:
        # Availability/format failures may fall back. A normal "not found" error
        # is deliberately not hidden by an older index.
        fallback_raw = raw
        if action == "角色":
            name, region = _split_role_region(query)
            fallback_raw = "角色 " + name
            result = await _dispatch_bwiki(fallback_raw, transport=transport)
            return result + f"\n\n⚠️ GameKee 暂不可用；BWIKI 备用结果不区分{region}/韩服。"
        if action == "随机角色":
            fallback_raw = "随机角色"
        if action == "兑换码":
            raise TrickcalError("GameKee 暂时不可用，无法可靠区分国服与韩服兑换码，请稍后再试。")
        return await _dispatch_bwiki(fallback_raw, transport=transport)


async def _character_card_for_entry(
    entry: GameKeeEntry,
    detail: dict,
    card_region: str,
    *,
    transport=None,
) -> bytes | None:
    """Render one resolved GameKee character through the shared seven-day cache."""
    title = _text(detail.get("title") or entry.name, 40)
    updated_at = int(detail.get("updated_at") or entry.updated_at or 0)
    cache_key = ("gamekee-character-card", entry.content_id,
                 updated_at, card_region, CHARACTER_CARD_CACHE_VERSION)
    cached = _cache_get(cache_key)
    if isinstance(cached, bytes):
        return cached
    disk_key = _character_card_disk_key(entry.content_id, updated_at, card_region)
    cached = await asyncio.to_thread(_read_character_card_cache, disk_key)
    if cached is not None:
        _cache_put(cache_key, cached, CHARACTER_CARD_CACHE_TTL)
        return cached
    raw_document = detail.get("content_json")
    if not isinstance(raw_document, str) or len(raw_document.encode("utf-8")) > GAMEKEE_MAX_RESPONSE_BYTES:
        return None
    try:
        document = json.loads(raw_document)
    except json.JSONDecodeError:
        return None
    spec = trickcal_media.extract_character_visual(document, title, card_region)
    if spec is None:
        return None
    image = await trickcal_media.render_character_visual(spec, transport)
    if image:
        _cache_put(cache_key, image, CHARACTER_CARD_CACHE_TTL)
        await asyncio.to_thread(_write_character_card_cache, disk_key, image)
    return image


async def _character_context(raw: str, *, result: str = "", transport=None):
    """Resolve the exact role/detail used by both Markdown and JPEG renderers."""
    action, query = parse_command(raw)
    if action not in {"角色", "随机角色"}:
        return None
    entries = await _gamekee_entries("角色", transport=transport)

    if action == "随机角色":
        region = ROLE_REGION_ALIASES.get(query, "国服")
        # dispatch() has already selected the random entry. Resolve that exact
        # content id from its source link so the text and image can never show
        # two different random characters.
        selected = re.search(
            re.escape(GAMEKEE_PAGE) + r"(\d+)\.html(?:\s|$)", str(result)
        )
        if selected is not None:
            content_id = int(selected.group(1))
            first = next((entry for entry in entries if entry.content_id == content_id), None)
        else:
            candidates = _region_entries(entries, region)
            first = random.SystemRandom().choice(candidates) if candidates else None
        if first is None:
            return None
        actual_region = "韩服" if ROLE_REGION_GROUPS["韩服"] in first.group else "国服"
        region_fallback = region == "国服" and actual_region == "韩服"
    else:
        query, region = _split_role_region(query)
        matches, actual_region, region_fallback = _select_role_matches(entries, query, region)
        if not matches:
            raise _role_not_found(entries, query, region)
        first = matches[0]
        exact_name = _role_name_exact(first, query)
        if len(matches) > 1 and not exact_name:
            return None

    detail = await _gamekee_request(f"/v1/content/detail/{first.content_id}", transport=transport)
    if not isinstance(detail, dict):
        return None
    card_region = "韩服·国服暂无" if region_fallback else actual_region
    return first, detail, card_region


async def character_markdown(raw: str, *, result: str = "", transport=None) -> str | None:
    """Return one-message QQ Markdown using GameKee's public image URLs."""
    context = await _character_context(raw, result=result, transport=transport)
    if context is None:
        return None
    entry, detail, card_region = context
    raw_document = detail.get("content_json")
    if not isinstance(raw_document, str) or len(raw_document.encode("utf-8")) > GAMEKEE_MAX_RESPONSE_BYTES:
        return None
    try:
        document = json.loads(raw_document)
    except json.JSONDecodeError:
        return None
    title = _text(detail.get("title") or entry.name, 40)
    spec = trickcal_media.extract_character_visual(document, title, card_region)
    if spec is None:
        return None
    markdown = trickcal_media.character_visual_markdown(spec)
    return markdown or None


async def character_card(raw: str, *, result: str = "", transport=None) -> bytes | None:
    """Return the cached visual JPEG, extended vertically when result text is supplied."""
    context = await _character_context(raw, result=result, transport=transport)
    if context is None:
        return None
    first, detail, card_region = context
    visual = await _character_card_for_entry(
        first, detail, card_region, transport=transport,
    )
    details = str(result).strip()
    if visual is None or not details:
        return visual

    material = (
        f"long:{CHARACTER_LONG_CARD_CACHE_VERSION}:".encode("ascii")
        + hashlib.sha256(visual).digest()
        + details.encode("utf-8")
    )
    digest = hashlib.sha256(material).hexdigest()
    cache_key = ("gamekee-character-long-card", digest)
    cached = _cache_get(cache_key)
    if isinstance(cached, bytes):
        return cached
    disk_key = "long-" + digest
    cached = await asyncio.to_thread(_read_character_card_cache, disk_key)
    if cached is not None:
        _cache_put(cache_key, cached, CHARACTER_CARD_CACHE_TTL)
        return cached
    image = await asyncio.to_thread(
        trickcal_media.render_long_character_card, visual, details,
    )
    if image:
        _cache_put(cache_key, image, CHARACTER_CARD_CACHE_TTL)
        await asyncio.to_thread(_write_character_card_cache, disk_key, image)
    return image
