"""Run against an installed wheel as well as the source tree."""
import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bot_tools.storage import Identity, Store
from qbot_trickcal import runtime, trickcal, trickcal_board, trickcal_web


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(temp.name)

    def test_packaged_website_assets_and_name_table(self):
        app = FastAPI()
        trickcal_web.install_trickcal_web(app, self.store)
        client = TestClient(app)
        for url, mime in (
            ("/tr-board/", "text/html"),
            ("/tr-board/api-docs", "text/html"),
            ("/tr-board/assets/board.js", "javascript"),
            ("/tr-board/assets/board.css", "text/css"),
            ("/tr-board/assets/api-docs.css", "text/css"),
        ):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn(mime, response.headers["content-type"])
                self.assertGreater(len(response.content), 100)
        mapping = json.loads(trickcal.ROLE_NAME_MAP_FILE.read_text(encoding="utf-8"))
        self.assertIn("Ner", mapping["角色映射"])
        self.assertEqual(client.get("/api/v1/tr-board/board").status_code, 401)

    def test_plugin_schema_preserves_existing_progress(self):
        who = Identity("test-bot", "group:test", "test-member")
        boards = trickcal_board.BoardStore(self.store)
        raw = json.dumps({
            "version": 1, "units": [[10016, 3]],
            "boards": [[10016, {"selectedNodes": [100], "plannedNodes": [101]}]],
            "cards": [], "pets": [], "filter": [], "stepFilter": [], "statFilter": [],
            "purpleWeight": 8, "goldWeight": 1000,
        })
        boards.replace(who, raw, now=100)
        before = boards.get(who)
        # Reinitializing after an upgrade does not replace tables or records.
        trickcal_web.BoardWeb(Store(self.store.path))
        after = trickcal_board.BoardStore(self.store).get(who)
        self.assertEqual(before, after)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_mount_registers_single_cancellable_refresh_task(self):
        startup, shutdown = [], []
        fake_driver = SimpleNamespace(on_startup=startup.append, on_shutdown=shutdown.append)
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            app = FastAPI()
            with patch.object(runtime, "get_driver", return_value=fake_driver):
                web = runtime.install_web(app, store)
                route_count = len(app.routes)
                self.assertIs(runtime.install_web(app, store), web)
                self.assertEqual(len(app.routes), route_count)
            self.assertEqual(len(startup), 1)
            self.assertEqual(len(shutdown), 1)
            with patch.object(runtime, "refresh_catalogue_if_due", new_callable=AsyncMock) as refresh:
                await startup[0]()
                await startup[0]()
                await asyncio.sleep(0)
                refresh.assert_awaited_once_with(store)
                await shutdown[0]()
                await shutdown[0]()


if __name__ == "__main__":
    unittest.main()
