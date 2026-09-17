"""Optional real-browser checks: QBOT_BROWSER_TESTS=1 (requires Chromium)."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import uvicorn

from bot_tools.storage import Identity, Store
from qbot_trickcal import trickcal_board as board
from qbot_trickcal import trickcal_web as web_module


@unittest.skipUnless(os.environ.get("QBOT_BROWSER_TESTS") == "1", "optional Chromium UI tests")
class CollectionBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import expect, sync_playwright

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        public_catalog = os.environ.get("QBOT_PUBLIC_CATALOG")
        if public_catalog:
            self.catalog = board._catalog_from_payload(json.loads(Path(public_catalog).read_text()))
        else:
            self.catalog = board._catalog_from_payload(board._reduce_catalog(
                [{"uid": i, "name": f"测试角色{i}", "resource_name": f"Role{i}",
                  "personality": i % 5, "available": True} for i in range(10001, 10013)],
                [{"uid": i * 10, "unit_uid": i, "step": 1, "node_type": 3,
                  "stat_type": "88", "stat_value": "30"} for i in range(10001, 10013)],
            ))
        self.uids = sorted(self.catalog.units)
        self.owner = Identity("browser-test-bot", "private:browser-test", "browser-test", True)
        self.store = Store(self.temp.name)
        self.boards = board.BoardStore(self.store)
        self.boards.add_units(self.owner, self.uids[:-2])
        self.first = self.uids[0]
        self.boards.set_selected_nodes(self.owner, self.first, [self.catalog.by_unit[self.first][0]], selected=True)
        self.addCleanup(patch.stopall)
        patch.object(board, "catalogue", new=AsyncMock(return_value=self.catalog)).start()

        async def portrait(uid, icon):
            root = os.environ.get("QBOT_PUBLIC_PORTRAITS")
            return next(Path(root).glob(f"{uid}-*.webp"), None) if root else None

        patch.object(web_module, "_portrait_file", new=portrait).start()
        app = FastAPI()
        self.web = web_module.install_trickcal_web(app, self.store)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        patch.dict(os.environ, {"TRICKCAL_WEB_PUBLIC_URL": self.base + "/tr-board/",
                               "TRICKCAL_WEB_SECURE_COOKIE": "false"}).start()
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()

        def stop_server():
            self.server.should_exit = True
            thread.join(timeout=5)
            sock.close()

        self.addCleanup(stop_server)
        self.pw = sync_playwright().start()
        self.addCleanup(self.pw.stop)
        self.browser = self.pw.chromium.launch(headless=True, args=["--no-sandbox"])
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        link, _ = self.web.issue(self.owner)
        self.page.goto(link)
        self.page.locator("#app").wait_for(state="visible")
        expect(self.page.locator("#catalogNote")).to_contain_text("Soshage")
        self.page.locator("#openCollectionButton").click()
        self.page.locator("#collectionDialog").wait_for(state="visible")

    def capture(self, name):
        root = os.environ.get("QBOT_UI_ARTIFACTS")
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(root) / name))

    def card(self, uid):
        return self.page.locator(f'[data-collection-unit="{uid}"]')

    def test_desktop_collection_behaviour(self):
        from playwright.sync_api import expect

        page = self.page
        expect(page.locator("#collectionGrid button")).to_have_count(len(self.uids))
        self.capture("collection-desktop.png")
        before = self.boards.get(self.owner)[0]
        # Cancelling a removal, including Escape after a prior confirmation,
        # must not clear ownership or any selected node.
        self.card(self.first).click()
        page.locator('#confirmDialog button[value="cancel"]').click()
        self.assertEqual(self.boards.get(self.owner)[0], before)
        page.locator('[data-collection-filter="unowned"]').click()
        expect(page.locator("#collectionGrid button")).to_have_count(2)
        uid = self.uids[-1]
        self.card(uid).click()
        expect(page.locator("#collectionGrid button")).to_have_count(1)
        expect(page.locator("#collectionNotice")).to_contain_text("未增加节点")
        self.assertEqual(self.boards.get(self.owner)[0]["boards"], before["boards"])
        page.locator('[data-collection-filter="all"]').click()
        page.locator("#collectionSearchInput").fill(str(uid))
        expect(page.locator("#collectionGrid button")).to_have_count(1)
        expect(self.card(uid)).to_have_attribute("aria-pressed", "true")
        self.card(uid).click()
        page.locator('#confirmDialog button[value="confirm"]').click()
        expect(self.card(uid)).to_have_attribute("aria-pressed", "false")
        page.locator("#collectionSearchInput").fill(str(self.first))
        self.card(self.first).click()
        page.keyboard.press("Escape")
        expect(page.locator("#confirmDialog")).not_to_be_visible()
        self.assertEqual(self.boards.get(self.owner)[0]["boards"], before["boards"])
        page.locator("#collectionSearchInput").fill("不存在的名字")
        expect(page.locator("#collectionEmpty")).to_be_visible()
        page.locator("#collectionSearchInput").fill("")
        # Failures are visible inside the top-layer dialog, with no fake success.
        page.route(f"**/api/units/{uid}/owned", lambda route: route.fulfill(
            status=503, content_type="application/json", body='{"detail":"测试保存失败"}'))
        self.card(uid).click()
        expect(page.locator("#collectionNotice")).to_have_text("测试保存失败")
        expect(self.card(uid)).to_have_attribute("aria-pressed", "false")
        page.unroute(f"**/api/units/{uid}/owned")
        page.locator("#ownAllButton").click()
        page.locator('#confirmDialog button[value="confirm"]').click()
        expect(page.locator("#collectionGrid button.owned")).to_have_count(len(self.uids))
        self.assertEqual(self.boards.get(self.owner)[0]["boards"], before["boards"])
        page.locator("#closeCollectionButton").click()
        # Main-board layer/stat/search/hide filters must never restrict collection.
        page.locator("#searchInput").fill("不存在")
        page.locator('[data-node-filter="selected"]').click()
        page.locator("#openCollectionButton").click()
        expect(page.locator("#collectionGrid button")).to_have_count(len(self.uids))
        page.keyboard.press("Escape")
        expect(page.locator("#collectionDialog")).not_to_be_visible()
        self.assertEqual(self.errors, [])

    def test_phone_grid_and_footer_stay_inside_viewport(self):
        from playwright.sync_api import expect

        page = self.page
        page.locator("#closeCollectionButton").click()
        for width, height in ((390, 844), (320, 568), (844, 390)):
            page.set_viewport_size({"width": width, "height": height})
            page.locator('[data-mobile-panel="resource"]').click()
            page.locator("#openCollectionButton").click()
            expect(page.locator("#collectionGrid button")).to_have_count(len(self.uids))
            dialog = page.locator("#collectionDialog").bounding_box()
            footer = page.locator(".collection-footer").bounding_box()
            self.assertGreaterEqual(dialog["x"], 0)
            self.assertLessEqual(dialog["x"] + dialog["width"], width)
            self.assertGreaterEqual(footer["y"], 0)
            self.assertLessEqual(footer["y"] + footer["height"], height)
            self.assertGreaterEqual(page.locator(".collection-scroll").bounding_box()["height"], 125)
            self.assertFalse(page.locator(".collection-scroll").evaluate("el => el.scrollWidth > el.clientWidth"))
            page.locator("#collectionSearchInput").fill(str(self.uids[-1]))
            expect(page.locator("#collectionGrid button")).to_have_count(1)
            page.locator("#collectionSearchInput").fill("")
            self.capture(f"collection-mobile-{width}.png")
            page.locator("#closeCollectionButton").click()
        self.assertEqual(self.errors, [])
