from __future__ import annotations

import html
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot_tools.storage import Identity, Store, ToolError
from qbot_trickcal import trickcal_board
from qbot_trickcal.trickcal_web import BoardWeb, _role_search_names, install_trickcal_web


PRIVATE = Identity("test-bot", "private:user", "raw-user-openid", True)
GROUP = Identity("test-bot", "group:one", "raw-user-openid", False, "owner")
OTHER = Identity("test-bot", "group:one", "another-openid", False, "member")
SECOND_BOT_GROUP = Identity(
    "second-bot", "group:two", "different-app-openid", False, "member"
)


def catalog() -> trickcal_board.Catalog:
    units = {
        10016: {"name": "艾爾芬", "alias": "Erpin", "personality": 0},
        10017: {"name": "涅爾", "alias": "Ner", "personality": 2},
    }
    nodes = {
        1: {"unit": 10016, "step": 1, "type": 1, "stat_type": "88,89",
            "stat_value": "7,7", "gold": 100, "gold_crayons": 1},
        2: {"unit": 10016, "step": 2, "type": 1, "stat_type": "95",
            "stat_value": "3", "gold": 200, "gold_crayons": 2},
        3: {"unit": 10017, "step": 1, "type": 1, "stat_type": "92,93",
            "stat_value": "5,5", "gold": 300, "gold_crayons": 3},
    }
    return trickcal_board.Catalog(
        units, nodes, {10016: (1, 2), 10017: (3,)}, fetched=1234.0
    )


def token_from(link: str) -> str:
    return parse_qs(urlsplit(link).fragment)["login"][0]


class BoardWebStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.web = BoardWeb(self.store)

    @patch.dict("os.environ", {"TRICKCAL_WEB_PUBLIC_URL": "https://bot.example.com/tr-board/"})
    def test_private_one_time_link_creates_seven_day_session_without_raw_openid(self):
        with self.assertRaises(ToolError):
            self.web.issue(GROUP, now=100)
        link, expires = self.web.issue(PRIVATE, now=100)
        self.assertEqual(expires, 700)
        self.assertTrue(link.startswith("https://bot.example.com/tr-board/#login="))
        token = token_from(link)
        session, csrf, session_expires = self.web.redeem(token, now=101)
        self.assertEqual(session_expires, 101 + 7 * 24 * 60 * 60)
        self.assertEqual(len(session), 64)
        self.assertEqual(len(csrf), 48)
        current = self.web.session(session, now=102)
        self.assertIsNotNone(current)
        self.assertNotIn(PRIVATE.user, json.dumps(current))
        with self.assertRaises(ToolError):
            self.web.redeem(token, now=103)
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT owner,bot,csrf FROM trickcal_web_sessions"
            ).fetchall()
        self.assertNotIn(PRIVATE.user, json.dumps([dict(row) for row in rows]))
        self.web.logout(session)
        self.assertIsNone(self.web.session(session, now=104))

    @patch.dict("os.environ", {"TRICKCAL_WEB_PUBLIC_URL": "https://bot.example.com/tr-board/"})
    def test_group_pairing_creates_separate_hashed_user_account(self):
        link, request_code, expires = self.web.issue_pairing(GROUP, now=100)
        self.assertEqual(link, "https://bot.example.com/tr-board/")
        self.assertEqual(len(request_code), 16)
        self.assertEqual(expires, 700)

        browser, confirmation, pairing_csrf, _ = self.web.begin_pairing(
            request_code.lower(), now=101
        )
        self.assertFalse(self.web.pairing_status(browser, now=102)["confirmed"])
        with self.assertRaises(ToolError):
            self.web.confirm_pairing(OTHER, confirmation, now=103)
        self.web.confirm_pairing(GROUP, confirmation.lower(), now=103)
        status = self.web.pairing_status(browser, now=104)
        self.assertTrue(status["confirmed"])
        self.assertFalse(status["account_exists"])
        self.assertEqual(status["csrf"], pairing_csrf)

        session, csrf, account_expires, created, username = self.web.complete_pairing(
            browser, "蜡笔用户", "Strong-Board-Password-2026", now=105,
        )
        self.assertTrue(created)
        self.assertEqual(username, "蜡笔用户")
        self.assertEqual(account_expires, 105 + 30 * 24 * 60 * 60)
        self.assertEqual(self.web.session(session, now=106)["username"], "蜡笔用户")
        with self.assertRaises(ToolError):
            self.web.pairing_status(browser, now=106)

        login, login_csrf, _, login_name = self.web.account_login(
            "蜡笔用户", "Strong-Board-Password-2026", "127.0.0.1", now=107,
        )
        self.assertEqual(login_name, "蜡笔用户")
        self.assertEqual(len(login_csrf), 48)
        self.assertIsNotNone(self.web.session(login, now=108))
        with self.assertRaisesRegex(ToolError, "账号或密码不正确"):
            self.web.account_login("蜡笔用户", "wrong-password", "127.0.0.2", now=109)
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM trickcal_web_accounts"
            )]
        stored = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("Strong-Board-Password-2026", stored)
        self.assertNotIn(GROUP.user, stored)

    def test_logged_in_board_can_link_a_different_bot_openid(self):
        boards = trickcal_board.BoardStore(self.store)
        self.assertTrue(boards.add_unit(GROUP, 10016, now=90))
        code, expires = self.web.issue_bot_pairing({
            "owner": trickcal_board._owner(GROUP), "bot": GROUP.bot,
        }, now=100)
        self.assertEqual(len(code), 16)
        self.assertEqual(expires, 700)
        self.assertTrue(self.web.redeem_bot_pairing(SECOND_BOT_GROUP, code, now=101))
        linked = boards.get(SECOND_BOT_GROUP)
        self.assertIsNotNone(linked)
        self.assertEqual(linked[0]["units"], [[10016, 1]])
        self.assertFalse(self.web.redeem_bot_pairing(SECOND_BOT_GROUP, code, now=102))

    def test_cross_bot_link_refuses_to_overwrite_another_board(self):
        boards = trickcal_board.BoardStore(self.store)
        boards.add_unit(GROUP, 10016, now=90)
        boards.add_unit(SECOND_BOT_GROUP, 10017, now=91)
        code, _expires = self.web.issue_bot_pairing({
            "owner": trickcal_board._owner(GROUP), "bot": GROUP.bot,
        }, now=100)
        with self.assertRaisesRegex(ToolError, "另一份蜡笔板"):
            self.web.redeem_bot_pairing(SECOND_BOT_GROUP, code, now=101)

    def test_api_token_is_hashed_scoped_expiring_revocable_and_rate_limited(self):
        _link, code, _ = self.web.issue_pairing(GROUP, now=100)
        browser, confirmation, _, _ = self.web.begin_pairing(code, now=101)
        self.web.confirm_pairing(GROUP, confirmation, now=102)
        login, _, _, created, _ = self.web.complete_pairing(
            browser, "board-user", "Strong-Board-Password-2026", now=103,
        )
        self.assertTrue(created)
        session = self.web.session(login, now=104)
        issued = self.web.create_api_token(session, "统计工具", now=105)
        self.assertTrue(issued["token"].startswith("trb1_"))
        self.assertEqual(issued["expires"], 105 + 90 * 24 * 60 * 60)
        with self.store.connect() as db:
            stored = [dict(row) for row in db.execute("SELECT * FROM trickcal_web_api_tokens")]
        self.assertNotIn(issued["token"], json.dumps(stored))
        listing = self.web.api_tokens(session, now=106)
        self.assertEqual(listing[0]["label"], "统计工具")
        self.assertNotIn("token", listing[0])
        self.assertEqual(self.web.api_ref(issued["token"], now=107).owner,
                         trickcal_board._owner(GROUP))
        boards = trickcal_board.BoardStore(self.store)
        boards.add_unit(GROUP, 10016, now=107)
        code, _ = self.web.issue_bot_pairing(session, now=108)
        self.assertTrue(self.web.redeem_bot_pairing(SECOND_BOT_GROUP, code, now=109))
        self.assertEqual(boards.get(self.web.api_ref(issued["token"], now=110))[0]["units"],
                         boards.get(SECOND_BOT_GROUP)[0]["units"])
        self.assertIsNone(self.web.api_ref(issued["token"] + "bad", now=107))
        with self.store.connect() as db:
            db.execute("UPDATE trickcal_web_api_tokens SET window_hits=60 WHERE id=?",
                       (issued["id"],))
        with self.assertRaisesRegex(ToolError, "频繁"):
            self.web.api_ref(issued["token"], now=108)
        self.assertIsNotNone(self.web.api_ref(issued["token"], now=168))
        self.assertTrue(self.web.revoke_api_token(session, issued["id"], now=169))
        self.assertFalse(self.web.revoke_api_token(session, issued["id"], now=170))
        self.assertIsNone(self.web.api_ref(issued["token"], now=170))
        expired = self.web.create_api_token(session, "短期测试", now=171)
        self.assertIsNone(self.web.api_ref(expired["token"], now=expired["expires"]))

    def test_api_token_cannot_be_created_from_unclaimed_web_session(self):
        link, _ = self.web.issue(PRIVATE, now=100)
        login, _, _ = self.web.redeem(token_from(link), now=101)
        session = self.web.session(login, now=102)
        with self.assertRaisesRegex(ToolError, "创建蜡笔板账号"):
            self.web.create_api_token(session, "viewer", now=103)


class BoardWebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        app = FastAPI()
        self.web = install_trickcal_web(app, self.store)
        self.client = TestClient(app)
        with patch.dict("os.environ", {
            "TRICKCAL_WEB_PUBLIC_URL": "http://127.0.0.1:8080/tr-board/",
            "TRICKCAL_WEB_SECURE_COOKIE": "false",
        }):
            link, _expires = self.web.issue(PRIVATE)
            login = self.client.post(
                "/tr-board/api/redeem", json={"token": token_from(link)}
            )
        self.assertEqual(login.status_code, 200, login.text)
        self.csrf = login.json()["data"]["csrf"]
        self.headers = {"X-Trickcal-CSRF": self.csrf}

    def post(self, path: str, body: dict):
        return self.client.post(path, json=body, headers=self.headers)

    @patch("qbot_trickcal.trickcal_web.board.catalogue", new_callable=AsyncMock)
    def test_page_state_individual_and_bulk_changes_share_bot_storage(self, fetch):
        fetch.return_value = catalog()
        page = self.client.get("/tr-board/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertIn("我的蜡笔板", page.text)
        self.assertIn("board.css?v=20260917-api1", page.text)
        self.assertIn('id="issueBotBindingButton"', page.text)
        self.assertIn('id="botBindingDialog"', page.text)
        self.assertIn('id="manageApiTokensButton"', page.text)
        self.assertIn('id="apiTokenDialog"', page.text)
        self.assertIn('href="/tr-board/api-docs"', page.text)
        self.assertIn('data-node-filter="selected"', page.text)
        self.assertIn('data-node-filter="unselected"', page.text)
        self.assertIn('data-mobile-panel="overview"', page.text)
        self.assertIn('data-mobile-panel="resource"', page.text)
        self.assertIn('id="overviewPanel"', page.text)
        self.assertIn('id="resourcePanel"', page.text)
        javascript = self.client.get("/tr-board/assets/board.js")
        self.assertEqual(javascript.status_code, 200)
        self.assertIn("unit.search_names", javascript.text)
        self.assertIn("toggleMobilePanel", javascript.text)
        self.assertNotIn("event.currentTarget.elements", javascript.text)
        self.assertNotIn("owned-toggle", javascript.text)
        self.assertNotIn('dataset.action = "owned"', javascript.text)
        self.assertIn("state.hideSelected && selected", javascript.text)
        self.assertIn("state.hideUnselected && !selected", javascript.text)
        self.assertIn("issueBotBinding", javascript.text)
        self.assertIn("createApiToken", javascript.text)

        docs = self.client.get("/tr-board/api-docs")
        self.assertEqual(docs.status_code, 200)
        self.assertIn("蜡笔板 API 文档", docs.text)
        self.assertIn("/api/v1/tr-board/catalog", docs.text)
        self.assertIn("/api/v1/tr-board/board", docs.text)
        self.assertIn("Authorization: Bearer", docs.text)
        self.assertIn("frame-ancestors 'none'", docs.headers["content-security-policy"])
        self.assertEqual(docs.headers["cache-control"], "no-store")
        examples = re.findall(r"<pre><code>(.*?)</code></pre>", docs.text, re.S)
        self.assertEqual(len(examples), 3)
        for example in examples[1:]:
            self.assertTrue(json.loads(html.unescape(example))["ok"])
        stylesheet = self.client.get("/tr-board/assets/api-docs.css")
        self.assertEqual(stylesheet.status_code, 200)
        self.assertIn("text/css", stylesheet.headers["content-type"])
        self.assertEqual(self.client.get("/tr-board/assets/unknown.css").status_code, 404)

        empty = self.client.get("/tr-board/api/state").json()["data"]
        self.assertEqual(empty["summary"]["owned"], 0)
        self.assertEqual(len(empty["units"]), 2)
        denied = self.client.post(
            "/tr-board/api/units/10016/owned", json={"owned": True}
        )
        self.assertEqual(denied.status_code, 403)

        owned = self.post("/tr-board/api/units/10016/owned", {"owned": True})
        self.assertEqual(owned.status_code, 200, owned.text)
        marked = self.post("/tr-board/api/nodes", {
            "unit": 10016, "layer": 1, "group": "攻击", "selected": True,
        })
        self.assertEqual(marked.status_code, 200, marked.text)
        current = self.client.get("/tr-board/api/state").json()["data"]
        self.assertEqual(current["summary"]["owned"], 1)
        self.assertEqual(current["summary"]["selected"], 1)
        self.assertEqual(current["summary"]["gold"], 100)
        self.assertEqual(current["summary"]["gold_crayons"], 1)
        self.assertEqual(current["units"][0]["name"], "艾尔芬")
        self.assertIn("艾爾芬", current["units"][0]["search_names"])
        self.assertEqual(current["units"][0]["personality"], 0)
        self.assertEqual(
            current["units"][0]["portrait"],
            "/tr-board/assets/portraits/10016.webp",
        )

        portrait_path = Path(self.temp.name) / "portrait.webp"
        portrait_path.write_bytes(b"RIFF\x0c\x00\x00\x00WEBPVP8 \x00\x00\x00\x00")
        with patch(
            "qbot_trickcal.trickcal_web._portrait_file",
            new_callable=AsyncMock,
            return_value=portrait_path,
        ):
            portrait = self.client.get("/tr-board/assets/portraits/10016.webp")
        self.assertEqual(portrait.status_code, 200)
        self.assertEqual(portrait.headers["content-type"], "image/webp")

        self.assertEqual(
            self.post("/tr-board/api/units/owned-all", {}).json()["data"]["changed"], 1
        )
        bulk = self.post("/tr-board/api/nodes/bulk", {
            "layer": 1, "group": "all", "selected": True,
        })
        self.assertEqual(bulk.status_code, 200, bulk.text)
        self.assertEqual(bulk.json()["data"], {"changed": 1, "roles": 1})

        # The exact same owner key is used by QQ commands in any conversation.
        saved = trickcal_board.BoardStore(self.store).get(GROUP)
        self.assertIsNotNone(saved)
        self.assertEqual(len(saved[0]["units"]), 2)
        selected = {node for _uid, value in saved[0]["boards"] for node in value["selectedNodes"]}
        self.assertEqual(selected, {1, 3})

    def test_web_search_names_use_the_shared_role_alias_table(self):
        names = _role_search_names("Rohne", "洛涅")
        self.assertIn("罗涅", names)
        self.assertIn("羅涅", names)
        self.assertIn("洛涅", names)
        self.assertIn("Rohne", names)

    @patch("qbot_trickcal.trickcal_web.board.catalogue", new_callable=AsyncMock)
    def test_read_only_third_party_api_and_owner_isolation(self, fetch):
        fetch.return_value = catalog()
        def make_account(who, username):
            _link, code, _expires = self.web.issue_pairing(who)
            browser, confirmation, _csrf, _expires = self.web.begin_pairing(code)
            self.web.confirm_pairing(who, confirmation)
            self.web.complete_pairing(browser, username, "Strong-Board-Password-2026")

        make_account(GROUP, "first-user")
        login = self.client.post("/tr-board/api/account/login", json={
            "username": "first-user", "password": "Strong-Board-Password-2026",
        })
        self.assertEqual(login.status_code, 200, login.text)
        csrf = login.json()["data"]["csrf"]
        self.assertEqual(self.client.get("/tr-board/api/api-tokens").json()["data"], [])
        self.assertEqual(self.client.post("/tr-board/api/api-tokens", json={
            "label": "只读统计",
        }).status_code, 403)
        created = self.client.post("/tr-board/api/api-tokens", json={
            "label": "只读统计",
        }, headers={"X-Trickcal-CSRF": csrf})
        self.assertEqual(created.status_code, 200, created.text)
        item = created.json()["data"]
        bearer = {"Authorization": "Bearer " + item["token"]}
        self.assertNotIn("token", self.client.get(
            "/tr-board/api/api-tokens").json()["data"][0])
        self.assertEqual(self.client.get("/api/v1/tr-board/board").status_code, 401)
        self.assertEqual(self.client.get(
            "/api/v1/tr-board/board?token=" + item["token"]).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/tr-board/board", headers={
            "Authorization": "Bearer bogus",
        }).status_code, 401)
        self.assertEqual(self.client.post("/tr-board/api/units/10016/owned", json={
            "owned": True,
        }, headers={"X-Trickcal-CSRF": csrf}).status_code, 200)
        board_response = self.client.get("/api/v1/tr-board/board", headers=bearer)
        self.assertEqual(board_response.status_code, 200, board_response.text)
        data = board_response.json()["data"]
        self.assertEqual(data["summary"]["owned"], 1)
        self.assertNotIn("csrf", data)
        self.assertNotIn("session_expires", data)
        self.assertNotIn(GROUP.user, board_response.text)
        catalogue = self.client.get("/api/v1/tr-board/catalog", headers=bearer)
        self.assertEqual(catalogue.status_code, 200, catalogue.text)
        self.assertEqual(len(catalogue.json()["data"]["units"]), 2)
        self.assertNotIn("owned", catalogue.text)
        self.assertEqual(self.client.post("/api/v1/tr-board/board", headers=bearer).status_code, 405)

        make_account(OTHER, "other-user")
        second = TestClient(self.client.app)
        login = second.post("/tr-board/api/account/login", json={
            "username": "other-user", "password": "Strong-Board-Password-2026",
        })
        second_csrf = login.json()["data"]["csrf"]
        wrong = second.delete("/tr-board/api/api-tokens/" + item["id"],
                              headers={"X-Trickcal-CSRF": second_csrf})
        self.assertEqual(wrong.status_code, 404)
        other_created = second.post("/tr-board/api/api-tokens", json={
            "label": "第二个用户",
        }, headers={"X-Trickcal-CSRF": second_csrf})
        other_bearer = {"Authorization": "Bearer " + other_created.json()["data"]["token"]}
        self.assertEqual(second.get("/api/v1/tr-board/board", headers=other_bearer)
                         .json()["data"]["summary"]["owned"], 0)
        with self.store.connect() as db:
            db.execute("UPDATE trickcal_web_api_tokens SET window_hits=60 WHERE id=?",
                       (item["id"],))
        limited = self.client.get("/api/v1/tr-board/board", headers=bearer)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)
        denied = self.client.delete("/tr-board/api/api-tokens/" + item["id"])
        self.assertEqual(denied.status_code, 403)
        removed = self.client.delete("/tr-board/api/api-tokens/" + item["id"],
                                     headers={"X-Trickcal-CSRF": csrf})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/tr-board/board", headers=bearer).status_code, 401)

    @patch("qbot_trickcal.trickcal_web.board.catalogue", new_callable=AsyncMock)
    def test_group_pairing_and_account_login_api(self, fetch):
        fetch.return_value = catalog()
        browser = TestClient(self.client.app)
        with patch.dict("os.environ", {
            "TRICKCAL_WEB_PUBLIC_URL": "http://127.0.0.1:8080/tr-board/",
            "TRICKCAL_WEB_SECURE_COOKIE": "false",
        }):
            _link, request_code, _expires = self.web.issue_pairing(GROUP)
            started = browser.post(
                "/tr-board/api/pairing/start", json={"code": request_code}
            )
        self.assertEqual(started.status_code, 200, started.text)
        pairing = started.json()["data"]
        self.assertIn("qqbot_trickcal_pairing", started.headers["set-cookie"])
        waiting = browser.get("/tr-board/api/pairing/status")
        self.assertFalse(waiting.json()["data"]["confirmed"])
        self.web.confirm_pairing(GROUP, pairing["confirmation"])
        confirmed = browser.get("/tr-board/api/pairing/status").json()["data"]
        self.assertTrue(confirmed["confirmed"])
        self.assertFalse(confirmed["account_exists"])

        denied = browser.post(
            "/tr-board/api/pairing/complete",
            json={"username": "网页用户", "password": "Secure-Web-Password-2026"},
        )
        self.assertEqual(denied.status_code, 403)
        completed = browser.post(
            "/tr-board/api/pairing/complete",
            json={"username": "网页用户", "password": "Secure-Web-Password-2026"},
            headers={"X-Trickcal-Pairing-CSRF": pairing["csrf"]},
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertTrue(completed.json()["data"]["created"])
        self.assertEqual(browser.get("/tr-board/api/state").status_code, 200)
        browser.post(
            "/tr-board/api/logout", json={},
            headers={"X-Trickcal-CSRF": completed.json()["data"]["csrf"]},
        )
        logged_in = browser.post("/tr-board/api/account/login", json={
            "username": "网页用户", "password": "Secure-Web-Password-2026",
        })
        self.assertEqual(logged_in.status_code, 200, logged_in.text)
        self.assertEqual(logged_in.json()["data"]["username"], "网页用户")

    def test_authenticated_page_can_issue_a_cross_bot_binding_code(self):
        denied = self.client.post("/tr-board/api/bot-pairing", json={})
        self.assertEqual(denied.status_code, 403)
        issued = self.post("/tr-board/api/bot-pairing", {})
        self.assertEqual(issued.status_code, 200, issued.text)
        data = issued.json()["data"]
        self.assertRegex(data["code"], r"^[A-F0-9]{16}$")
        self.assertGreater(data["expires"], 0)

    @patch("qbot_trickcal.trickcal_web.board.catalogue", new_callable=AsyncMock)
    def test_import_export_logout_and_validation(self, fetch):
        fetch.return_value = catalog()
        document = {
            "version": 1, "units": [[10016, 3]], "cards": [], "pets": [],
            "boards": [[10016, {"selectedNodes": [1], "plannedNodes": []}]],
            "filter": [], "stepFilter": [], "statFilter": [],
            "purpleWeight": 8, "goldWeight": 1000,
        }
        imported = self.client.post(
            "/tr-board/api/import", content=json.dumps(document),
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        exported = self.client.get("/tr-board/api/export")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.json()["boards"], document["boards"])
        self.assertIn("attachment", exported.headers["content-disposition"])
        wrong_origin = self.client.post(
            "/tr-board/api/nodes", json={
                "unit": 10016, "layer": 1, "group": "攻击", "selected": False,
            }, headers={**self.headers, "Origin": "https://evil.example"},
        )
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(
            self.client.post("/tr-board/api/logout", json={}, headers=self.headers).status_code,
            200,
        )
        self.assertEqual(self.client.get("/tr-board/api/state").status_code, 401)


if __name__ == "__main__":
    unittest.main()
