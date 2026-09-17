"""Private, per-QQ-user web UI for the Trickcal crayon-board tracker."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from bot_tools.storage import Identity, Store, ToolError
from bot_tools import http_clients
from . import trickcal, trickcal_board as board
from .schema import ensure_board_schema


WEB_ROOT = Path(__file__).resolve().parent / "web"
PORTRAIT_BASE = "https://img.kusoge.xyz/trickcal/heroicons"
PORTRAIT_MAX_BYTES = 1024 * 1024
COOKIE = "qqbot_trickcal_session"
PAIR_COOKIE = "qqbot_trickcal_pairing"
LOGIN_TTL = 10 * 60
SESSION_TTL = 7 * 24 * 60 * 60
ACCOUNT_SESSION_TTL = 30 * 24 * 60 * 60
PAIRING_TTL = 10 * 60
API_TOKEN_TTL = 90 * 24 * 60 * 60
API_TOKEN_LIMIT = 5
API_RATE_WINDOW = 60
API_RATE_LIMIT = 60
LOGIN_WINDOW = 5 * 60
LOGIN_LIMIT = 5
PASSWORD_ROUNDS = 310_000
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
HEX64 = re.compile(r"[a-f0-9]{64}")
PAIRING_CODE = re.compile(r"[A-F0-9]{16}")
GROUP_LABELS = {
    "攻击": "攻击",
    "防御": "防御",
    "生命": "血量",
    "暴击": "暴击/暴伤",
    "暴抗": "暴抗/暴伤抗",
}
_portrait_locks: dict[str, asyncio.Lock] = {}


class ApiRateLimited(ToolError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _account_username(value: object) -> tuple[str, str]:
    username = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not 3 <= len(username) <= 32 or not re.fullmatch(r"[\w.@-]+", username):
        raise ToolError("账号应为 3～32 位中文、字母、数字或 . _ @ -。")
    return username, username.casefold()


def _account_password(value: object, username: str) -> str:
    password = str(value or "")
    if not 10 <= len(password) <= 128 or any(char in password for char in "\0\r\n"):
        raise ToolError("密码长度应为 10～128 个字符，不能包含换行。")
    if password.casefold() == username.casefold() or len(set(password)) < 4:
        raise ToolError("密码过于简单，请不要使用账号名或重复字符。")
    return password


def _password_hash(password: str, salt: str, rounds: int = PASSWORD_ROUNDS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds,
    ).hex()


def _role_search_names(resource_name: str, display_name: str) -> tuple[str, ...]:
    """Build browser search terms from the shared editable role-name table."""
    names = (display_name, resource_name, *trickcal.role_name_group(resource_name))
    expanded: list[str] = []
    for name in names:
        value = str(name or "").strip()
        if not value:
            continue
        expanded.append(value)
        simplified = board.simplify_role_name(value)
        if simplified:
            expanded.append(simplified)
    return tuple(dict.fromkeys(expanded))


def _empty_document() -> dict:
    return {"version": 1, "units": [], "boards": []}


def _portrait_cache_path(unit_uid: int, icon: str) -> Path:
    digest = hashlib.sha256(icon.encode("ascii")).hexdigest()[:12]
    root = Path(os.environ.get("BOT_DATA_DIR", "data"))
    return root / "cache" / "trickcal-board" / "portraits" / f"{unit_uid}-{digest}.webp"


def _valid_webp(body: bytes) -> bool:
    return (
        20 <= len(body) <= PORTRAIT_MAX_BYTES
        and body[:4] == b"RIFF"
        and body[8:12] == b"WEBP"
    )


def _write_portrait(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_bytes(body)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def _portrait_file(unit_uid: int, icon: str, transport=None) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", icon):
        return None
    path = _portrait_cache_path(unit_uid, icon)
    try:
        if _valid_webp(await asyncio.to_thread(path.read_bytes)):
            return path
    except OSError:
        pass
    lock_key = str(path)
    lock = _portrait_locks.setdefault(lock_key, asyncio.Lock())
    async with lock:
        try:
            if _valid_webp(await asyncio.to_thread(path.read_bytes)):
                return path
        except OSError:
            pass
        options: dict = {
            "timeout": httpx.Timeout(15, connect=8),
            "follow_redirects": False,
            "trust_env": False,
        }
        if transport is not None:
            options["transport"] = transport
        try:
            async with http_clients.client("trickcal-soshage-portraits", **options) as client:
                async with client.stream(
                    "GET", f"{PORTRAIT_BASE}/{quote(icon)}.webp",
                    headers={
                        "Accept": "image/webp",
                        "Referer": "https://soshage.com/trickcal/",
                        "User-Agent": "NoneBot-QQ-Trickcal/1.2",
                    },
                ) as response:
                    if response.status_code != 200:
                        return None
                    if "image/webp" not in response.headers.get("content-type", "").lower():
                        return None
                    body = bytearray()
                    async for chunk in response.aiter_bytes(65536):
                        body.extend(chunk)
                        if len(body) > PORTRAIT_MAX_BYTES:
                            return None
        except httpx.TransportError:
            return None
        raw = bytes(body)
        if not _valid_webp(raw):
            return None
        await asyncio.to_thread(_write_portrait, path, raw)
        return path


def _export_document(document: dict) -> dict:
    """Return a complete Soshage-compatible collection v1 document."""
    return {
        "version": 1,
        "units": document.get("units", []),
        "cards": document.get("cards", []),
        "pets": document.get("pets", []),
        "boards": document.get("boards", []),
        "filter": document.get("filter", []),
        "stepFilter": document.get("stepFilter", []),
        "statFilter": document.get("statFilter", []),
        "purpleWeight": document.get("purpleWeight", 8),
        "goldWeight": document.get("goldWeight", 1000),
    }


def public_url() -> str:
    raw = os.environ.get(
        "TRICKCAL_WEB_PUBLIC_URL", "http://127.0.0.1:8080/tr-board/"
    ).strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise ToolError("蜡笔板网页暂时不可用，请稍后重试或联系管理员。") from None
    if (
        parsed.scheme not in {"http", "https"} or not parsed.netloc
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise ToolError("蜡笔板网页暂时不可用，请稍后重试或联系管理员。")
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ToolError("蜡笔板网页暂时不可用，请稍后重试或联系管理员。")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/tr-board"
    if not path.endswith("/tr-board"):
        raise ToolError("蜡笔板网页暂时不可用，请稍后重试或联系管理员。")
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/", "", ""))


def _secure(response: Response, *, asset: bool = False) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
    )
    response.headers["Cache-Control"] = "public, max-age=3600" if asset else "no-store"
    return response


async def _json_body(request: Request, maximum: int = 16 * 1024) -> dict:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > maximum:
                raise HTTPException(413, "请求内容过大。")
        except ValueError:
            raise HTTPException(400, "请求格式不正确。") from None
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > maximum:
            raise HTTPException(413, "请求内容过大。")
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "请求格式不正确。") from None
    if not isinstance(value, dict):
        raise HTTPException(400, "请求格式不正确。")
    return value


async def _raw_body(request: Request, maximum: int) -> str:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > maximum:
                raise HTTPException(413, "导入文件不能超过 512 KiB。")
        except ValueError:
            raise HTTPException(400, "请求格式不正确。") from None
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > maximum:
            raise HTTPException(413, "导入文件不能超过 512 KiB。")
        raw.extend(chunk)
    try:
        return bytes(raw).decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "JSON 文件必须使用 UTF-8 编码。") from None


class BoardWeb:
    def __init__(self, store: Store):
        self.store = store
        ensure_board_schema(store)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS trickcal_web_links (
                    digest TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    bot TEXT NOT NULL,
                    created REAL NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_links_owner
                    ON trickcal_web_links(owner,bot);
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_links_expires
                    ON trickcal_web_links(expires);
                CREATE TABLE IF NOT EXISTS trickcal_web_sessions (
                    digest TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    bot TEXT NOT NULL,
                    csrf TEXT NOT NULL,
                    created REAL NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_sessions_owner
                    ON trickcal_web_sessions(owner,bot);
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_sessions_expires
                    ON trickcal_web_sessions(expires);
                CREATE TABLE IF NOT EXISTS trickcal_web_pairings (
                    request_digest TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    bot TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    browser_digest TEXT UNIQUE,
                    csrf TEXT NOT NULL DEFAULT '',
                    confirmation_digest TEXT UNIQUE,
                    created REAL NOT NULL,
                    expires REAL NOT NULL,
                    confirmed REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_pairings_owner
                    ON trickcal_web_pairings(owner,bot);
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_pairings_expires
                    ON trickcal_web_pairings(expires);
                CREATE TABLE IF NOT EXISTS trickcal_web_bot_pairings (
                    digest TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    created REAL NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_bot_pairings_owner
                    ON trickcal_web_bot_pairings(owner);
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_bot_pairings_expires
                    ON trickcal_web_bot_pairings(expires);
                CREATE TABLE IF NOT EXISTS trickcal_web_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    iterations INTEGER NOT NULL,
                    owner TEXT NOT NULL UNIQUE,
                    bot TEXT NOT NULL,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trickcal_web_login_attempts (
                    client TEXT PRIMARY KEY,
                    started REAL NOT NULL,
                    failures INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trickcal_web_api_tokens (
                    id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    bot TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created REAL NOT NULL,
                    expires REAL NOT NULL,
                    revoked REAL NOT NULL DEFAULT 0,
                    last_used REAL NOT NULL DEFAULT 0,
                    window_started REAL NOT NULL DEFAULT 0,
                    window_hits INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_trickcal_web_api_tokens_owner
                    ON trickcal_web_api_tokens(owner,revoked);
            """)

    @staticmethod
    def _purge(db: sqlite3.Connection, now: float) -> None:
        db.execute("DELETE FROM trickcal_web_links WHERE expires<=?", (now,))
        db.execute("DELETE FROM trickcal_web_sessions WHERE expires<=?", (now,))
        db.execute("DELETE FROM trickcal_web_pairings WHERE expires<=?", (now,))
        db.execute("DELETE FROM trickcal_web_bot_pairings WHERE expires<=?", (now,))
        db.execute(
            "DELETE FROM trickcal_web_login_attempts WHERE started<?",
            (now - LOGIN_WINDOW,),
        )

    @staticmethod
    def _create_session(db: sqlite3.Connection, owner: str, bot: str,
                        now: float, ttl: int) -> tuple[str, str, float]:
        session = secrets.token_hex(32)
        csrf = secrets.token_hex(24)
        expires = now + ttl
        db.execute(
            "INSERT INTO trickcal_web_sessions(digest,owner,bot,csrf,created,expires) "
            "VALUES(?,?,?,?,?,?)",
            (_digest(session), owner, bot, csrf, now, expires),
        )
        return session, csrf, expires

    def issue(self, who: Identity, now: float | None = None) -> tuple[str, float]:
        if not who.private:
            raise ToolError("网页登录链接只能在机器人私聊领取。")
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(36)
        expires = now + LOGIN_TTL
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = board._storage_owner(db, who)
            self._purge(db, now)
            db.execute(
                "DELETE FROM trickcal_web_links WHERE owner=? AND bot=?",
                (owner, who.bot),
            )
            db.execute(
                "INSERT INTO trickcal_web_links(digest,owner,bot,created,expires) VALUES(?,?,?,?,?)",
                (_digest(token), owner, who.bot, now, expires),
            )
        return public_url() + "#login=" + quote(token, safe=""), expires

    def issue_pairing(self, who: Identity,
                      now: float | None = None) -> tuple[str, str, float]:
        if who.private or not who.scope.startswith("group:"):
            raise ToolError("网页绑定码只能在 QQ 群里领取。")
        now = time.time() if now is None else now
        request_code = secrets.token_hex(8).upper()
        expires = now + PAIRING_TTL
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = board._storage_owner(db, who)
            self._purge(db, now)
            db.execute(
                "DELETE FROM trickcal_web_pairings WHERE owner=? AND bot=?",
                (owner, who.bot),
            )
            db.execute(
                "INSERT INTO trickcal_web_pairings"
                "(request_digest,owner,bot,scope,created,expires) VALUES(?,?,?,?,?,?)",
                (_digest(request_code), owner, who.bot, who.scope_key, now, expires),
            )
        return public_url(), request_code, expires

    def issue_bot_pairing(self, session: dict,
                          now: float | None = None) -> tuple[str, float]:
        """Create a one-time code that links another bot identity to this board."""
        session_owner = str(session.get("owner") or "")
        session_bot = str(session.get("bot") or "")
        if not HEX64.fullmatch(session_owner) or not session_bot:
            raise ToolError("登录状态无效，请重新登录后再试。")
        now = time.time() if now is None else now
        code = secrets.token_hex(8).upper()
        expires = now + PAIRING_TTL
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._purge(db, now)
            owner = board._storage_owner(db, board.BoardRef(session_owner, session_bot))
            db.execute("DELETE FROM trickcal_web_bot_pairings WHERE owner=?", (owner,))
            db.execute(
                "INSERT INTO trickcal_web_bot_pairings(digest,owner,created,expires) "
                "VALUES(?,?,?,?)",
                (_digest(code), owner, now, expires),
            )
        return code, expires

    def redeem_bot_pairing(self, who: Identity, code: object,
                           now: float | None = None) -> bool:
        """Link the current bot's QQ identity to an existing web board account."""
        if who.private or not who.scope.startswith("group:"):
            raise ToolError("请在新机器人所在的 QQ 群完成绑定。")
        supplied = str(code or "").strip().upper()
        if not PAIRING_CODE.fullmatch(supplied):
            return False
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._purge(db, now)
            row = db.execute(
                "SELECT owner FROM trickcal_web_bot_pairings "
                "WHERE digest=? AND expires>?",
                (_digest(supplied), now),
            ).fetchone()
            if row is None:
                return False
            source_owner = board._resolve_owner_alias(db, str(row["owner"]))
            target_owner = board._owner(who)
            target_alias = db.execute(
                "SELECT owner FROM trickcal_board_owner_alias WHERE alias=?",
                (target_owner,),
            ).fetchone()
            if target_alias is not None:
                linked_owner = board._resolve_owner_alias(db, target_owner)
                if linked_owner != source_owner:
                    raise ToolError("当前机器人身份已绑定另一份蜡笔板，不能直接覆盖。")
            elif target_owner != source_owner:
                progress = db.execute(
                    "SELECT 1 FROM trickcal_board_progress WHERE owner=?", (target_owner,)
                ).fetchone()
                account = db.execute(
                    "SELECT 1 FROM trickcal_web_accounts WHERE owner=?", (target_owner,)
                ).fetchone()
                if progress is not None or account is not None:
                    raise ToolError(
                        "当前机器人身份已有另一份蜡笔板，请先导出备份，不能直接覆盖。"
                    )
                db.execute(
                    "INSERT INTO trickcal_board_owner_alias(alias,owner) VALUES(?,?)",
                    (target_owner, source_owner),
                )
            legacy_owner = board._legacy_owner(who)
            db.execute(
                "INSERT OR REPLACE INTO trickcal_board_owner_alias(alias,owner) VALUES(?,?)",
                (legacy_owner, source_owner),
            )
            db.execute(
                "DELETE FROM trickcal_web_bot_pairings WHERE digest=?",
                (_digest(supplied),),
            )
        return True

    def begin_pairing(self, request_code: object,
                      now: float | None = None) -> tuple[str, str, str, float]:
        request_code = str(request_code or "").strip().upper()
        if not PAIRING_CODE.fullmatch(request_code):
            raise ToolError("绑定码格式不正确，请回群重新发送“/tr 蜡笔板 网页”。")
        now = time.time() if now is None else now
        browser_token = secrets.token_hex(32)
        confirmation_code = secrets.token_hex(8).upper()
        csrf = secrets.token_hex(24)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._purge(db, now)
            changed = db.execute(
                "UPDATE trickcal_web_pairings SET browser_digest=?,csrf=?,confirmation_digest=? "
                "WHERE request_digest=? AND expires>? AND browser_digest IS NULL",
                (_digest(browser_token), csrf, _digest(confirmation_code),
                 _digest(request_code), now),
            ).rowcount
            if not changed:
                raise ToolError("绑定码无效、已使用或已过期，请回群重新领取。")
        return browser_token, confirmation_code, csrf, now + PAIRING_TTL

    def confirm_pairing(self, who: Identity, confirmation_code: object,
                        now: float | None = None) -> None:
        if who.private or not who.scope.startswith("group:"):
            raise ToolError("请回到领取绑定码的 QQ 群完成确认。")
        confirmation_code = str(confirmation_code or "").strip().upper()
        if not PAIRING_CODE.fullmatch(confirmation_code):
            raise ToolError("网页回执码格式不正确。")
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = board._storage_owner(db, who)
            self._purge(db, now)
            changed = db.execute(
                "UPDATE trickcal_web_pairings SET confirmed=? "
                "WHERE owner=? AND bot=? AND scope=? AND confirmation_digest=? "
                "AND browser_digest IS NOT NULL AND confirmed=0 AND expires>?",
                (now, owner, who.bot, who.scope_key, _digest(confirmation_code), now),
            ).rowcount
            if not changed:
                raise ToolError("回执码无效、已确认或不属于你和当前群，请在网页重新开始绑定。")

    def pairing_status(self, browser_token: object,
                       now: float | None = None) -> dict:
        browser_token = str(browser_token or "")
        if not HEX64.fullmatch(browser_token):
            raise ToolError("网页绑定状态已失效，请回群重新领取绑定码。")
        now = time.time() if now is None else now
        with self.store.connect() as db:
            self._purge(db, now)
            row = db.execute(
                "SELECT owner,csrf,expires,confirmed FROM trickcal_web_pairings "
                "WHERE browser_digest=? AND expires>?",
                (_digest(browser_token), now),
            ).fetchone()
            if row is None:
                raise ToolError("网页绑定状态已失效，请回群重新领取绑定码。")
            account = db.execute(
                "SELECT username FROM trickcal_web_accounts WHERE owner=?",
                (row["owner"],),
            ).fetchone()
        return {
            "confirmed": bool(row["confirmed"]),
            "account_exists": account is not None,
            "username": str(account["username"]) if account else "",
            "csrf": str(row["csrf"]),
            "expires": float(row["expires"]),
        }

    def complete_pairing(self, browser_token: object, username: object = "",
                         password: object = "", now: float | None = None
                         ) -> tuple[str, str, float, bool, str]:
        browser_token = str(browser_token or "")
        if not HEX64.fullmatch(browser_token):
            raise ToolError("网页绑定状态已失效，请回群重新领取绑定码。")
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._purge(db, now)
            row = db.execute(
                "SELECT request_digest,owner,bot FROM trickcal_web_pairings "
                "WHERE browser_digest=? AND confirmed>0 AND expires>?",
                (_digest(browser_token), now),
            ).fetchone()
            if row is None:
                raise ToolError("群聊身份尚未确认，或绑定已经失效。")
            account = db.execute(
                "SELECT username FROM trickcal_web_accounts WHERE owner=?",
                (row["owner"],),
            ).fetchone()
            created = account is None
            if created:
                account_name, account_key = _account_username(username)
                account_password = _account_password(password, account_name)
                salt = secrets.token_hex(16)
                try:
                    db.execute(
                        "INSERT INTO trickcal_web_accounts"
                        "(username,username_key,salt,password_hash,iterations,owner,bot,created,updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (account_name, account_key, salt,
                         _password_hash(account_password, salt), PASSWORD_ROUNDS,
                         row["owner"], row["bot"], now, now),
                    )
                except sqlite3.IntegrityError:
                    raise ToolError("这个账号名已被使用，请换一个。") from None
            else:
                account_name = str(account["username"])
                db.execute(
                    "UPDATE trickcal_web_accounts SET bot=?,updated=? WHERE owner=?",
                    (row["bot"], now, row["owner"]),
                )
            session, csrf, expires = self._create_session(
                db, str(row["owner"]), str(row["bot"]), now,
                ACCOUNT_SESSION_TTL,
            )
            db.execute(
                "DELETE FROM trickcal_web_pairings WHERE request_digest=?",
                (row["request_digest"],),
            )
        return session, csrf, expires, created, account_name

    def account_login(self, username: object, password: object, client: str,
                      now: float | None = None) -> tuple[str, str, float, str]:
        now = time.time() if now is None else now
        supplied_name = unicodedata.normalize("NFKC", str(username or "")).strip()
        supplied_key = supplied_name.casefold()
        supplied_password = str(password or "")
        client_key = _digest("trickcal-user-login\0" + str(client or "unknown"))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._purge(db, now)
            attempt = db.execute(
                "SELECT started,failures FROM trickcal_web_login_attempts WHERE client=?",
                (client_key,),
            ).fetchone()
            if attempt and now - attempt["started"] < LOGIN_WINDOW and attempt["failures"] >= LOGIN_LIMIT:
                raise ToolError("登录尝试过多，请 5 分钟后再试。")
            row = db.execute(
                "SELECT username,salt,password_hash,iterations,owner,bot "
                "FROM trickcal_web_accounts WHERE username_key=?",
                (supplied_key,),
            ).fetchone()
            if not attempt or now - attempt["started"] >= LOGIN_WINDOW:
                db.execute(
                    "INSERT OR REPLACE INTO trickcal_web_login_attempts VALUES(?,?,1)",
                    (client_key, now),
                )
            else:
                db.execute(
                    "UPDATE trickcal_web_login_attempts SET failures=failures+1 WHERE client=?",
                    (client_key,),
                )
        salt = str(row["salt"]) if row else "00" * 16
        rounds = int(row["iterations"]) if row else PASSWORD_ROUNDS
        acceptable = (
            3 <= len(supplied_name) <= 32 and 10 <= len(supplied_password) <= 128
            and not any(char in supplied_password for char in "\0\r\n")
        )
        supplied_hash = _password_hash(supplied_password if acceptable else "", salt, rounds)
        valid = bool(
            acceptable and row
            and secrets.compare_digest(supplied_hash, str(row["password_hash"]))
        )
        if not valid:
            raise ToolError("账号或密码不正确。")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT password_hash FROM trickcal_web_accounts WHERE owner=?",
                (row["owner"],),
            ).fetchone()
            if current is None or not secrets.compare_digest(
                str(current["password_hash"]), str(row["password_hash"])
            ):
                raise ToolError("账号已更新，请重新登录。")
            db.execute(
                "DELETE FROM trickcal_web_login_attempts WHERE client=?", (client_key,)
            )
            session, csrf, expires = self._create_session(
                db, str(row["owner"]), str(row["bot"]), now,
                ACCOUNT_SESSION_TTL,
            )
        return session, csrf, expires, str(row["username"])

    def redeem(self, token: object, now: float | None = None) -> tuple[str, str, float]:
        token = str(token or "")
        if not TOKEN.fullmatch(token):
            raise ToolError("登录链接无效或已过期，请回到机器人私聊重新领取。")
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._purge(db, now)
            row = db.execute(
                "SELECT owner,bot FROM trickcal_web_links WHERE digest=? AND expires>?",
                (_digest(token), now),
            ).fetchone()
            if row is None:
                raise ToolError("登录链接无效或已过期，请回到机器人私聊重新领取。")
            db.execute("DELETE FROM trickcal_web_links WHERE digest=?", (_digest(token),))
            session, csrf, expires = self._create_session(
                db, str(row["owner"]), str(row["bot"]), now, SESSION_TTL,
            )
        return session, csrf, expires

    def session(self, token: object, now: float | None = None) -> dict | None:
        token = str(token or "")
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            return None
        now = time.time() if now is None else now
        with self.store.connect() as db:
            row = db.execute(
                "SELECT s.owner,s.bot,s.csrf,s.created,s.expires,"
                "COALESCE(a.username,'') AS username FROM trickcal_web_sessions s "
                "LEFT JOIN trickcal_web_accounts a ON a.owner=s.owner "
                "WHERE s.digest=? AND s.expires>?",
                (_digest(token), now),
            ).fetchone()
        if row is None or not HEX64.fullmatch(str(row["owner"])):
            return None
        return dict(row)

    def logout(self, token: object) -> None:
        token = str(token or "")
        if re.fullmatch(r"[a-f0-9]{64}", token):
            with self.store.connect() as db:
                db.execute(
                    "DELETE FROM trickcal_web_sessions WHERE digest=?", (_digest(token),)
                )

    @staticmethod
    def _account_owner(db: sqlite3.Connection, session: dict) -> str:
        owner = board._storage_owner(db, BoardWeb.ref(session))
        account = db.execute(
            "SELECT 1 FROM trickcal_web_accounts WHERE owner=?", (owner,)
        ).fetchone()
        if account is None:
            raise ToolError("请先创建蜡笔板账号，再生成 API 令牌。")
        return owner

    def api_tokens(self, session: dict, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self.store.connect() as db:
            owner = self._account_owner(db, session)
            rows = db.execute(
                "SELECT id,label,created,expires,last_used FROM trickcal_web_api_tokens "
                "WHERE owner=? AND revoked=0 AND expires>? ORDER BY created DESC",
                (owner, now),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_api_token(self, session: dict, label: object,
                         now: float | None = None) -> dict:
        now = time.time() if now is None else now
        name = " ".join(str(label or "").split())
        if not 1 <= len(name) <= 40 or any(ord(char) < 32 for char in name):
            raise ToolError("令牌名称应为 1～40 个可见字符。")
        token = "trb1_" + secrets.token_urlsafe(32)
        identifier = secrets.token_hex(12)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._account_owner(db, session)
            count = db.execute(
                "SELECT COUNT(*) FROM trickcal_web_api_tokens "
                "WHERE owner=? AND revoked=0 AND expires>?", (owner, now),
            ).fetchone()[0]
            if count >= API_TOKEN_LIMIT:
                raise ToolError("最多保留 5 个有效 API 令牌，请先撤销旧令牌。")
            db.execute(
                "INSERT INTO trickcal_web_api_tokens"
                "(id,digest,owner,bot,label,created,expires) VALUES(?,?,?,?,?,?,?)",
                (identifier, _digest(token), owner, str(session["bot"]),
                 name, now, now + API_TOKEN_TTL),
            )
        return {"id": identifier, "token": token, "label": name,
                "created": now, "expires": now + API_TOKEN_TTL}

    def revoke_api_token(self, session: dict, identifier: str,
                          now: float | None = None) -> bool:
        if not re.fullmatch(r"[a-f0-9]{24}", identifier):
            return False
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = self._account_owner(db, session)
            return bool(db.execute(
                "UPDATE trickcal_web_api_tokens SET revoked=? "
                "WHERE id=? AND owner=? AND revoked=0",
                (now, identifier, owner),
            ).rowcount)

    def api_ref(self, bearer: str, now: float | None = None) -> board.BoardRef | None:
        if not re.fullmatch(r"trb1_[A-Za-z0-9_-]{43}", bearer):
            return None
        now = time.time() if now is None else now
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id,owner,bot,window_started,window_hits "
                "FROM trickcal_web_api_tokens "
                "WHERE digest=? AND revoked=0 AND expires>?",
                (_digest(bearer), now),
            ).fetchone()
            if row is None:
                return None
            owner = board._resolve_owner_alias(db, str(row["owner"]))
            if db.execute("SELECT 1 FROM trickcal_web_accounts WHERE owner=?",
                          (owner,)).fetchone() is None:
                return None
            window = float(row["window_started"])
            hits = int(row["window_hits"])
            if now - window >= API_RATE_WINDOW:
                window, hits = now, 0
            if hits >= API_RATE_LIMIT:
                raise ApiRateLimited("API 请求过于频繁，请稍后重试。")
            db.execute(
                "UPDATE trickcal_web_api_tokens SET last_used=?,window_started=?,"
                "window_hits=? WHERE id=?",
                (now, window, hits + 1, row["id"]),
            )
            return board.BoardRef(owner, str(row["bot"]))

    @staticmethod
    def ref(session: dict) -> board.BoardRef:
        return board.BoardRef(str(session["owner"]), str(session["bot"]))

    async def state(self, session: dict) -> dict:
        catalog = await board.catalogue()
        ref = self.ref(session)
        saved = await run_in_threadpool(board.BoardStore(self.store).get, ref)
        document, updated = saved if saved is not None else (_empty_document(), 0.0)
        owned = {unit_uid for unit_uid, _rarity in document["units"]}
        board_by_unit = {unit_uid: value for unit_uid, value in document["boards"]}
        selected_ids = [
            node_uid for _unit_uid, value in document["boards"]
            for node_uid in value["selectedNodes"]
        ]
        planned_ids = [
            node_uid for _unit_uid, value in document["boards"]
            for node_uid in value["plannedNodes"]
        ]
        stats, gold, crayons, selected_count = board._node_stats(selected_ids, catalog)
        _planned_stats, planned_gold, planned_crayons, planned_count = board._node_stats(
            planned_ids, catalog
        )
        units = []
        progressed = 0
        total_percent = 0
        for unit_uid in catalog.units:
            unit_row = catalog.units[unit_uid]
            icon = unit_row["alias"].strip()
            display_name = board.simplify_role_name(unit_row["name"].strip())
            saved_board = board_by_unit.get(
                unit_uid, {"selectedNodes": [], "plannedNodes": []}
            )
            selected = set(saved_board["selectedNodes"])
            planned = set(saved_board["plannedNodes"])
            layers = []
            unit_total = unit_selected = 0
            for layer in board.LAYER_NAMES:
                nodes = []
                for group, (label, _types) in board.PERCENT_NODE_GROUPS.items():
                    node_ids = board._mark_node_ids(catalog, unit_uid, layer, (group,))
                    if not node_ids:
                        continue
                    node_uid = node_ids[0]
                    node = catalog.nodes[node_uid]
                    nodes.append({
                        "id": node_uid,
                        "group": group,
                        "label": GROUP_LABELS[group],
                        "selected": node_uid in selected,
                        "planned": node_uid in planned and node_uid not in selected,
                        "gold": node["gold"],
                        "gold_crayons": node.get("gold_crayons", 0),
                    })
                    unit_total += 1
                    unit_selected += int(node_uid in selected)
                if nodes:
                    layers.append({"layer": layer, "label": board.LAYER_NAMES[layer], "nodes": nodes})
            if unit_uid in owned:
                total_percent += unit_total
                progressed += int(unit_selected > 0)
            units.append({
                "id": unit_uid,
                "name": display_name,
                "alias": icon,
                "search_names": _role_search_names(icon, display_name),
                "personality": unit_row.get("personality", -1),
                "portrait": (
                    f"/tr-board/assets/portraits/{unit_uid}.webp"
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", icon) else ""
                ),
                "owned": unit_uid in owned,
                "selected": unit_selected if unit_uid in owned else 0,
                "total": unit_total,
                "layers": layers,
            })
        units.sort(key=lambda item: (not item["owned"], board._normal(item["name"] or item["alias"])))

        owned_rows = board._owned_rows(document)
        layer_summary = []
        for layer, label in board.LAYER_NAMES.items():
            groups = []
            for group, (_title, target_types) in board.PERCENT_NODE_GROUPS.items():
                done, total, _planned = board._percent_progress(
                    owned_rows, catalog, target_types
                )[layer]
                if total:
                    groups.append({
                        "group": group, "label": GROUP_LABELS[group],
                        "selected": done, "total": total,
                    })
            layer_summary.append({"layer": layer, "label": label, "groups": groups})
        return {
            "csrf": session["csrf"],
            "session_expires": session["expires"],
            "catalog": {
                "fetched": catalog.fetched, "stale": catalog.stale,
                "sources": list(catalog.sources),
            },
            "summary": {
                "owned": len(owned), "catalog_units": len(catalog.units),
                "progressed": progressed, "selected": selected_count,
                "total": total_percent, "planned": planned_count,
                "gold": gold, "gold_crayons": crayons,
                "planned_gold": planned_gold, "planned_gold_crayons": planned_crayons,
                "updated": updated, "stats": board._stats_lines(stats, limit=20),
            },
            "layers": layer_summary,
            "units": units,
        }


def install_trickcal_web(app: FastAPI, store: Store | None = None) -> BoardWeb:
    web = BoardWeb(store or Store())
    boards = board.BoardStore(web.store)

    def ok(data: object = None) -> JSONResponse:
        return _secure(JSONResponse({"ok": True, "data": data}))

    def safe(call):
        try:
            return call()
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from None

    async def safe_async(call):
        try:
            return await call()
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from None

    def require(request: Request, *, csrf: bool = False) -> dict:
        session = web.session(request.cookies.get(COOKIE))
        if session is None:
            raise HTTPException(401, "登录已失效，请使用蜡笔板账号登录；首次使用可在群聊领取绑定码。")
        if csrf:
            supplied = request.headers.get("x-trickcal-csrf", "")
            if not secrets.compare_digest(supplied, session["csrf"]):
                raise HTTPException(403, "页面凭据已失效，请刷新后重试。")
            origin = request.headers.get("origin")
            if origin:
                try:
                    if urlsplit(origin).netloc != request.headers.get("host", ""):
                        raise HTTPException(403, "拒绝跨站请求。")
                except ValueError:
                    raise HTTPException(403, "拒绝跨站请求。") from None
        return session

    def require_api_token(request: Request) -> board.BoardRef:
        authorization = request.headers.get("authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not credential:
            raise HTTPException(401, "请提供有效的 API 令牌。",
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            ref = web.api_ref(credential)
        except ApiRateLimited as exc:
            raise HTTPException(429, str(exc),
                                headers={"Retry-After": str(API_RATE_WINDOW)}) from None
        if ref is None:
            raise HTTPException(401, "API 令牌无效或已过期。",
                                headers={"WWW-Authenticate": "Bearer"})
        return ref

    def require_same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            return
        try:
            if urlsplit(origin).netloc != request.headers.get("host", ""):
                raise HTTPException(403, "拒绝跨站请求。")
        except ValueError:
            raise HTTPException(403, "拒绝跨站请求。") from None

    def set_login_cookie(response: Response, token: str, ttl: int) -> None:
        response.set_cookie(
            COOKIE, token, max_age=ttl, path="/tr-board", httponly=True,
            samesite="strict", secure=secure_cookie(),
        )

    def secure_cookie() -> bool:
        explicit = os.environ.get("TRICKCAL_WEB_SECURE_COOKIE", "").strip().lower()
        if explicit:
            return explicit in {"1", "true", "yes", "on"}
        return public_url().startswith("https://")

    @app.get("/tr-board", include_in_schema=False)
    @app.get("/tr-board/", include_in_schema=False)
    async def page() -> Response:
        return _secure(FileResponse(WEB_ROOT / "index.html", media_type="text/html; charset=utf-8"))

    @app.get("/tr-board/api-docs", include_in_schema=False)
    async def api_docs() -> Response:
        return _secure(FileResponse(WEB_ROOT / "api-docs.html", media_type="text/html; charset=utf-8"))

    @app.get("/tr-board/assets/{filename}", include_in_schema=False)
    async def asset(filename: str) -> Response:
        media = {
            "board.css": "text/css; charset=utf-8",
            "board.js": "text/javascript; charset=utf-8",
            "api-docs.css": "text/css; charset=utf-8",
        }.get(filename)
        if media is None:
            raise HTTPException(404)
        return _secure(FileResponse(WEB_ROOT / filename, media_type=media), asset=True)

    @app.get("/tr-board/assets/portraits/{unit_uid:int}.webp", include_in_schema=False)
    async def portrait(unit_uid: int) -> Response:
        catalog = await safe_async(board.catalogue)
        unit = catalog.units.get(unit_uid)
        if unit is None:
            raise HTTPException(404)
        path = await _portrait_file(unit_uid, unit["alias"].strip())
        if path is None:
            raise HTTPException(404)
        return _secure(FileResponse(path, media_type="image/webp"), asset=True)

    @app.post("/tr-board/api/account/login", include_in_schema=False)
    async def account_login(request: Request) -> Response:
        require_same_origin(request)
        data = await _json_body(request)
        client = request.client.host if request.client else "unknown"
        session, csrf, expires, username = await run_in_threadpool(
            lambda: safe(lambda: web.account_login(
                data.get("username"), data.get("password"), client,
            ))
        )
        response = ok({"csrf": csrf, "expires": expires, "username": username})
        set_login_cookie(response, session, ACCOUNT_SESSION_TTL)
        return response

    @app.post("/tr-board/api/pairing/start", include_in_schema=False)
    async def start_pairing(request: Request) -> Response:
        require_same_origin(request)
        data = await _json_body(request)
        browser_token, confirmation, csrf, expires = await run_in_threadpool(
            lambda: safe(lambda: web.begin_pairing(data.get("code")))
        )
        response = ok({
            "confirmation": confirmation, "csrf": csrf, "expires": expires,
        })
        response.set_cookie(
            PAIR_COOKIE, browser_token, max_age=PAIRING_TTL,
            path="/tr-board", httponly=True, samesite="strict",
            secure=secure_cookie(),
        )
        return response

    @app.post("/tr-board/api/bot-pairing", include_in_schema=False)
    async def issue_bot_pairing(request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        code, expires = await run_in_threadpool(web.issue_bot_pairing, session)
        return ok({"code": code, "expires": expires})

    @app.get("/tr-board/api/pairing/status", include_in_schema=False)
    async def pairing_status(request: Request) -> Response:
        result = await run_in_threadpool(
            lambda: safe(lambda: web.pairing_status(request.cookies.get(PAIR_COOKIE)))
        )
        return ok(result)

    @app.post("/tr-board/api/pairing/complete", include_in_schema=False)
    async def complete_pairing(request: Request) -> Response:
        require_same_origin(request)
        data = await _json_body(request)
        browser_token = request.cookies.get(PAIR_COOKIE)
        pairing = await run_in_threadpool(
            lambda: safe(lambda: web.pairing_status(browser_token))
        )
        supplied = request.headers.get("x-trickcal-pairing-csrf", "")
        if not secrets.compare_digest(supplied, pairing["csrf"]):
            raise HTTPException(403, "网页绑定凭据已失效，请重新开始。")
        session, csrf, expires, created, username = await run_in_threadpool(
            lambda: safe(lambda: web.complete_pairing(
                browser_token, data.get("username"), data.get("password"),
            ))
        )
        response = ok({
            "csrf": csrf, "expires": expires, "created": created,
            "username": username,
        })
        set_login_cookie(response, session, ACCOUNT_SESSION_TTL)
        response.delete_cookie(PAIR_COOKIE, path="/tr-board")
        return response

    @app.post("/tr-board/api/redeem", include_in_schema=False)
    async def redeem(request: Request) -> Response:
        data = await _json_body(request)
        session, csrf, expires = await run_in_threadpool(
            lambda: safe(lambda: web.redeem(data.get("token")))
        )
        response = ok({"csrf": csrf, "expires": expires})
        set_login_cookie(response, session, SESSION_TTL)
        return response

    @app.get("/tr-board/api/session", include_in_schema=False)
    async def current_session(request: Request) -> Response:
        session = await run_in_threadpool(require, request)
        return ok({
            "csrf": session["csrf"], "expires": session["expires"],
            "username": session.get("username", ""),
        })

    @app.post("/tr-board/api/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        del session
        await run_in_threadpool(web.logout, request.cookies.get(COOKIE))
        response = ok()
        response.delete_cookie(COOKIE, path="/tr-board")
        response.delete_cookie(PAIR_COOKIE, path="/tr-board")
        return response

    @app.get("/tr-board/api/api-tokens", include_in_schema=False)
    async def list_api_tokens(request: Request) -> Response:
        session = await run_in_threadpool(require, request)
        tokens = await run_in_threadpool(lambda: safe(lambda: web.api_tokens(session)))
        return ok(tokens)

    @app.post("/tr-board/api/api-tokens", include_in_schema=False)
    async def create_api_token(request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        data = await _json_body(request)
        result = await run_in_threadpool(
            lambda: safe(lambda: web.create_api_token(session, data.get("label")))
        )
        return ok(result)

    @app.delete("/tr-board/api/api-tokens/{identifier}", include_in_schema=False)
    async def revoke_api_token(identifier: str, request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        removed = await run_in_threadpool(
            lambda: safe(lambda: web.revoke_api_token(session, identifier))
        )
        if not removed:
            raise HTTPException(404, "没有找到这个 API 令牌。")
        return ok({"revoked": True})

    @app.get("/api/v1/tr-board/catalog", include_in_schema=False)
    async def public_catalog(request: Request) -> Response:
        ref = await run_in_threadpool(require_api_token, request)
        data = await safe_async(lambda: web.state({
            "owner": ref.owner, "bot": ref.bot, "csrf": "", "expires": 0,
        }))
        units = [{
            "id": unit["id"], "name": unit["name"],
            "alias": unit["alias"], "search_names": unit["search_names"],
            "personality": unit["personality"], "portrait": unit["portrait"],
            "layers": [{
                "layer": layer["layer"], "label": layer["label"],
                "nodes": [{key: node[key] for key in
                           ("id", "group", "label", "gold", "gold_crayons")}
                          for node in layer["nodes"]],
            } for layer in unit["layers"]],
        } for unit in data["units"]]
        return ok({"version": 1, "catalog": data["catalog"], "units": units})

    @app.get("/api/v1/tr-board/board", include_in_schema=False)
    async def public_board(request: Request) -> Response:
        ref = await run_in_threadpool(require_api_token, request)
        data = await safe_async(lambda: web.state({
            "owner": ref.owner, "bot": ref.bot, "csrf": "", "expires": 0,
        }))
        data.pop("csrf", None)
        data.pop("session_expires", None)
        return ok({"version": 1, **data})

    @app.get("/tr-board/api/state", include_in_schema=False)
    async def state(request: Request) -> Response:
        session = await run_in_threadpool(require, request)
        return ok(await safe_async(lambda: web.state(session)))

    @app.post("/tr-board/api/units/{unit_uid:int}/owned", include_in_schema=False)
    async def set_owned(unit_uid: int, request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        data = await _json_body(request)
        if type(data.get("owned")) is not bool:
            raise HTTPException(400, "owned 必须是布尔值。")
        catalog = await safe_async(board.catalogue)
        if unit_uid not in catalog.units:
            raise HTTPException(404, "没有这个角色。")
        ref = web.ref(session)
        if data["owned"]:
            changed = await run_in_threadpool(boards.add_unit, ref, unit_uid)
        else:
            changed, _remaining = await run_in_threadpool(boards.remove_units, ref, [unit_uid])
        return ok({"changed": bool(changed)})

    @app.post("/tr-board/api/units/owned-all", include_in_schema=False)
    async def own_all(request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        await _json_body(request)
        catalog = await safe_async(board.catalogue)
        changed, total = await run_in_threadpool(
            boards.add_units, web.ref(session), list(catalog.units)
        )
        return ok({"changed": changed, "total": total})

    @app.post("/tr-board/api/nodes", include_in_schema=False)
    async def set_node(request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        data = await _json_body(request)
        if type(data.get("unit")) is not int or type(data.get("layer")) is not int:
            raise HTTPException(400, "角色或层数格式不正确。")
        group = str(data.get("group") or "")
        if group not in board.PERCENT_NODE_GROUPS or data["layer"] not in board.LAYER_NAMES:
            raise HTTPException(400, "百分比节点类型不正确。")
        if type(data.get("selected")) is not bool:
            raise HTTPException(400, "selected 必须是布尔值。")
        catalog = await safe_async(board.catalogue)
        if data["unit"] not in catalog.units:
            raise HTTPException(404, "没有这个角色。")
        node_ids = board._mark_node_ids(catalog, data["unit"], data["layer"], (group,))
        if not node_ids:
            raise HTTPException(400, "这个角色在该层没有对应的百分比节点。")
        changed = await run_in_threadpool(
            boards.set_selected_nodes, web.ref(session), data["unit"], node_ids,
            selected=data["selected"],
        )
        return ok({"changed": changed})

    @app.post("/tr-board/api/nodes/bulk", include_in_schema=False)
    async def set_nodes_bulk(request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        data = await _json_body(request)
        if type(data.get("layer")) is not int or data["layer"] not in board.LAYER_NAMES:
            raise HTTPException(400, "层数格式不正确。")
        group = str(data.get("group") or "")
        if group != "all" and group not in board.PERCENT_NODE_GROUPS:
            raise HTTPException(400, "百分比节点类型不正确。")
        if type(data.get("selected")) is not bool:
            raise HTTPException(400, "selected 必须是布尔值。")
        catalog = await safe_async(board.catalogue)
        ref = web.ref(session)
        saved = await run_in_threadpool(boards.get, ref)
        if saved is None or not saved[0]["units"]:
            raise HTTPException(400, "你还没有点亮角色。")
        groups = tuple(board.PERCENT_NODE_GROUPS) if group == "all" else (group,)
        selections = {}
        for unit_uid, _rarity in saved[0]["units"]:
            node_ids = board._mark_node_ids(catalog, unit_uid, data["layer"], groups)
            if node_ids:
                selections[unit_uid] = node_ids
        changed, roles = await run_in_threadpool(
            boards.set_selected_nodes_bulk, ref, selections, selected=data["selected"]
        )
        return ok({"changed": changed, "roles": roles})

    @app.post("/tr-board/api/import", include_in_schema=False)
    async def import_board(request: Request) -> Response:
        session = await run_in_threadpool(require, request, csrf=True)
        raw = await _raw_body(request, board.MAX_IMPORT_BYTES)
        document = await run_in_threadpool(
            lambda: safe(lambda: boards.replace(web.ref(session), raw))
        )
        return ok({"owned": len(document["units"])})

    @app.get("/tr-board/api/export", include_in_schema=False)
    async def export_board(request: Request) -> Response:
        session = await run_in_threadpool(require, request)
        saved = await run_in_threadpool(boards.get, web.ref(session))
        if saved is None:
            raise HTTPException(404, "你还没有蜡笔板记录。")
        payload = json.dumps(
            _export_document(saved[0]), ensure_ascii=False, indent=2
        ).encode("utf-8")
        response = Response(payload, media_type="application/json; charset=utf-8")
        response.headers["Content-Disposition"] = 'attachment; filename="trickcal-board.json"'
        return _secure(response)

    return web
