from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from qbot_trickcal import trickcal_board
from bot_tools.storage import Identity, Store, ToolError


def export_document(*, selected=None, planned=None):
    return {
        "version": 1,
        "units": [[10016, 3]],
        "cards": [],
        "pets": [],
        "boards": [[10016, {
            "selectedNodes": [100] if selected is None else selected,
            "plannedNodes": [101] if planned is None else planned,
        }]],
        "filter": [],
        "stepFilter": [],
        "statFilter": [],
        "purpleWeight": 8,
        "goldWeight": 1000,
    }


def catalogue_transport():
    units = [
        {"uid": 10016, "name": "艾爾芬", "resource_name": "Erpin"},
        {"uid": 10017, "name": "艾雅", "resource_name": "Aya"},
    ]
    boards = [
        {"uid": 100, "unit_uid": 10016, "step": 1, "node_type": 5,
         "stat_type": "86,88", "stat_value": "10.0,25.0", "need_gold": 100,
         "need_item_ids": "610004", "need_item_values": "2"},
        {"uid": 101, "unit_uid": 10016, "step": 2, "node_type": 3,
         "stat_type": "5,92", "stat_value": "50.0,30.0", "need_gold": 200,
         "need_item_ids": "610004", "need_item_values": "4"},
        {"uid": 102, "unit_uid": 10016, "step": 3, "node_type": 3,
         "stat_type": "4,88", "stat_value": "12.0,20.0", "need_gold": 300,
         "need_item_ids": "610004", "need_item_values": "6"},
        {"uid": 103, "unit_uid": 10016, "step": 1, "node_type": 3,
         "stat_type": "94,95", "stat_value": "100.0,20.0", "need_gold": 100,
         "need_item_ids": "610004", "need_item_values": "2"},
        {"uid": 104, "unit_uid": 10016, "step": 2, "node_type": 3,
         "stat_type": "96,97,100,101", "stat_value": "10.0,10.0,10.0,10.0",
         "need_gold": 100, "need_item_ids": "610004", "need_item_values": "4"},
        {"uid": 105, "unit_uid": 10016, "step": 2, "node_type": 3,
         "stat_type": "4", "stat_value": "100.0", "need_gold": 999,
         "need_item_ids": "610004", "need_item_values": "99"},
        {"uid": 106, "unit_uid": 10016, "step": 1, "node_type": 3,
         "stat_type": "98,99,102,103", "stat_value": "10.0,10.0,10.0,10.0",
         "need_gold": 100, "need_item_ids": "610004", "need_item_values": "2"},
        {"uid": 200, "unit_uid": 10017, "step": 1, "node_type": 5,
         "stat_type": "86,88", "stat_value": "10.0,25.0", "need_gold": 100,
         "need_item_ids": "610004", "need_item_values": "2"},
        {"uid": 201, "unit_uid": 10017, "step": 3, "node_type": 5,
         "stat_type": "86,88", "stat_value": "10.0,20.0", "need_gold": 300,
         "need_item_ids": "610004", "need_item_values": "6"},
    ]

    def handler(request: httpx.Request):
        payload = units if request.url.path.endswith("/unit") else boards
        return httpx.Response(200, json=payload, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


def crayon_note_source():
    return r'''
const LANG_DICT = {
  "en": {
    stats_layer_1_rule: "(Node+3%, Crayon×2)",
    stats_layer_2_rule: "(Node+4%, Crayon×4)",
    stats_layer_3_rule: "(Node+5%, Crayon×6)",
    "艾爾芬": "Erpin", "芭莉耶": "Barie"
  }
};
const INITIAL_DATA = [
  { name: "艾爾芬", personality: "天真", race: "妖精", position: "後排", job: "輸出", pathVersion: "V2" },
  { name: "芭莉耶", personality: "憂鬱", race: "魔女", position: "後排", job: "輔助", pathVersion: "V4", releaseDate: "2026-09-10T17:00:00+09:00" }
];
const CRAYON_PATH_CONFIG = {
  "妖精": {
    "V2": { layer1: ["攻擊", "防禦"], layer2: ["攻擊", "防禦", "血量"], layer3: ["攻擊", "血量", "爆擊", "爆抗"] }
  },
  "魔女": {
    "V4": { layer1: ["血量", "爆擊"], layer2: ["防禦", "爆擊", "爆抗"], layer3: ["攻擊", "防禦", "血量", "爆擊"] }
  }
};
'''


class BoardValidationTests(unittest.TestCase):
    def test_crayon_note_parser_reads_roles_paths_and_layer_rules_without_eval(self):
        characters, rules = trickcal_board._parse_crayon_note(
            crayon_note_source(), minimum_characters=1,
        )

        self.assertEqual(rules, {1: (3, 2), 2: (4, 4), 3: (5, 6)})
        self.assertEqual(characters[0]["alias"], "Erpin")
        self.assertEqual(characters[1]["personality"], 4)
        self.assertEqual(characters[1]["layers"][1], ("血量", "爆擊"))

    def test_crayon_note_augments_soshage_without_replacing_precise_nodes(self):
        payload = trickcal_board._reduce_catalog(
            [{"uid": 10016, "name": "艾爾芬", "resource_name": "Erpin", "personality": 0}],
            [{
                "uid": 100, "unit_uid": 10016, "step": 1, "node_type": 5,
                "stat_type": "89,88", "stat_value": "30.0,30.0", "need_gold": 70000,
                "need_item_ids": "610004", "need_item_values": "2",
            }],
        )

        merged = trickcal_board._merge_crayon_note(
            payload, crayon_note_source(), now=2_000_000_000, minimum_characters=1,
        )
        catalog = trickcal_board._catalog_from_payload(merged)

        self.assertIsNotNone(catalog)
        self.assertEqual(catalog.sources, ("Soshage", "Crayon-note"))
        self.assertEqual(len(catalog.units), 2)
        self.assertEqual(catalog.nodes[100]["gold"], 70000)
        barie_uid = next(uid for uid, row in catalog.units.items() if row["alias"] == "Barie")
        self.assertGreater(barie_uid, trickcal_board.CRAYON_NOTE_UNIT_BASE)
        self.assertEqual(len(catalog.by_unit[barie_uid]), 9)
        barie_nodes = [catalog.nodes[uid] for uid in catalog.by_unit[barie_uid]]
        self.assertEqual({node["gold"] for node in barie_nodes if node["step"] == 1}, {70000})
        self.assertEqual({node["gold_crayons"] for node in barie_nodes if node["step"] == 3}, {6})

    def test_paired_critical_attributes_share_one_display_line(self):
        lines = trickcal_board._stats_lines({97: 12.5, 101: 12.5, 99: 8, 103: 8})
        self.assertEqual(lines, ["全体暴击/暴伤 +12.5%", "全体暴抗/暴伤抗 +8%"])

    def test_special_and_simplified_role_name_forms_share_one_search_key(self):
        self.assertEqual(trickcal_board._normal("艾爾芬"), trickcal_board._normal("艾尔芬"))
        self.assertEqual(
            trickcal_board._normal("x乂錫安乂x"),
            trickcal_board._normal("x㐅锡安㐅x"),
        )

    def test_current_soshage_v1_document_is_normalized(self):
        result = trickcal_board.validate_export(json.dumps(export_document()))
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["units"], [[10016, 3]])
        self.assertEqual(result["boards"][0][1]["selectedNodes"], [100])
        self.assertNotIn("cards", result)
        self.assertNotIn("purpleWeight", result)

    def test_invalid_overlap_unknown_fields_and_large_input_are_rejected(self):
        overlap = export_document(selected=[100], planned=[100])
        with self.assertRaisesRegex(ToolError, "不能同时"):
            trickcal_board.validate_export(json.dumps(overlap))
        unknown = export_document()
        unknown["secret"] = "x"
        with self.assertRaisesRegex(ToolError, "不支持"):
            trickcal_board.validate_export(json.dumps(unknown))
        with self.assertRaisesRegex(ToolError, "512 KiB"):
            trickcal_board.validate_export(" " * (trickcal_board.MAX_IMPORT_BYTES + 1))

    def test_board_commands_are_detected_without_capturing_character_queries(self):
        self.assertTrue(trickcal_board.is_board_command("蜡笔板 导入 {}"))
        self.assertTrue(trickcal_board.is_board_command("board"))
        self.assertFalse(trickcal_board.is_board_command("角色 埃尔芬"))
        self.assertEqual(trickcal_board.parse_command("蜡笔板 计划 2"), ("plan", "2"))
        self.assertEqual(trickcal_board.parse_command("蜡笔板 未点 攻击"), ("missing", "攻击"))
        self.assertEqual(trickcal_board.parse_command("蜡笔板 点亮 艾尔芬"), ("unlock", "艾尔芬"))
        self.assertEqual(trickcal_board.parse_command("蜡笔板 点亮 全部"), ("unlock", "全部"))
        self.assertEqual(trickcal_board.parse_command("蜡笔板 未点亮"), ("unowned", ""))
        self.assertEqual(
            trickcal_board.parse_command("蜡笔板 取消点亮 艾尔芬"),
            ("remove_unit", "艾尔芬"),
        )
        self.assertEqual(
            trickcal_board.parse_command("蜡笔板 反点 艾尔芬"),
            ("remove_unit", "艾尔芬"),
        )
        self.assertEqual(
            trickcal_board.parse_command("蜡笔板 加点 艾尔芬 2 攻击"),
            ("mark", "艾尔芬 2 攻击"),
        )
        self.assertEqual(
            trickcal_board.parse_command("蜡笔板 加点 全部 1 攻击"),
            ("mark", "全部 1 攻击"),
        )
        self.assertEqual(
            trickcal_board._parse_missing_query("二层 攻击"),
            ("攻击", 2, 1),
        )
        self.assertEqual(
            trickcal_board._parse_missing_query("攻击 三层 2"),
            ("攻击", 3, 2),
        )
        self.assertEqual(
            trickcal_board._parse_missing_query("攻击 2"),
            ("攻击", None, 2),
        )
        self.assertEqual(
            trickcal_board._parse_mark_value("艾尔芬, 艾雅 3 攻击"),
            ("艾尔芬, 艾雅", 3, "攻击"),
        )
        self.assertEqual(
            trickcal_board._parse_mark_value("全部 第一层 全部"),
            ("全部", 1, None),
        )

    def test_only_qq_message_and_file_cdn_urls_are_accepted(self):
        self.assertTrue(trickcal_board.valid_qq_export_url(
            "https://gzc-download.ftn.qq.com/ftn_handler/id?fname=collection.json"
        ))
        self.assertTrue(trickcal_board.valid_qq_export_url(
            "https://gchat.qpic.cn/collection.json"
        ))
        self.assertFalse(trickcal_board.valid_qq_export_url(
            "https://gzc-download.ftn.qq.com.evil.example/collection.json"
        ))
        self.assertFalse(trickcal_board.valid_qq_export_url(
            "http://gzc-download.ftn.qq.com/collection.json"
        ))


class BoardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.boards = trickcal_board.BoardStore(self.store)
        self.private = Identity("bot", "private:member", "member", True)
        self.group = Identity("bot", "group:one", "member", False)
        self.other = Identity("bot", "group:one", "other", False)

    def test_group_import_is_visible_to_same_user_in_private_not_other_users(self):
        self.boards.replace(self.group, json.dumps(export_document()), now=1234)
        private = self.boards.get(self.private)
        self.assertIsNotNone(private)
        self.assertEqual(private[1], 1234)
        self.assertIsNone(self.boards.get(self.other))

    def test_progress_is_shared_for_same_user_across_bots(self):
        self.boards.replace(self.private, json.dumps(export_document()), now=1234)
        other_bot = Identity("second-bot", "private:elsewhere", "member", True)
        shared = self.boards.get(other_bot)
        self.assertIsNotNone(shared)
        self.assertEqual(shared[1], 1234)

    def test_legacy_bot_scoped_progress_migrates_lazily(self):
        legacy_owner = trickcal_board._legacy_owner(self.private)
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO trickcal_board_progress(owner,bot,payload,updated) VALUES(?,?,?,?)",
                (legacy_owner, self.private.bot, json.dumps(export_document()), 456),
            )
        migrated = self.boards.get(self.private)
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated[1], 456)
        with self.store.connect() as db:
            alias = db.execute(
                "SELECT owner FROM trickcal_board_owner_alias WHERE alias=?", (legacy_owner,)
            ).fetchone()[0]
        self.assertNotEqual(alias, legacy_owner)

    def test_replace_and_confirmed_delete_only_affect_owner(self):
        self.boards.replace(self.private, json.dumps(export_document()))
        self.boards.replace(self.other, json.dumps(export_document(selected=[102], planned=[])))
        self.assertTrue(self.boards.delete(self.group))
        self.assertIsNone(self.boards.get(self.private))
        self.assertIsNotNone(self.boards.get(self.other))

    def test_pending_upload_is_bound_to_sender_and_exact_conversation(self):
        other_group = Identity("bot", "group:two", "member", False)
        self.boards.begin_import(self.group, now=100)
        self.assertTrue(self.boards.pending_import(self.group, now=101))
        self.assertFalse(self.boards.pending_import(self.private, now=101))
        self.assertFalse(self.boards.pending_import(other_group, now=101))
        self.assertFalse(self.boards.pending_import(self.other, now=101))
        token = self.boards.claim_import(self.group, now=102)
        self.assertIsNotNone(token)
        self.assertFalse(self.boards.pending_import(self.group, now=102))
        self.boards.rearm_import(self.group, token, now=103)
        self.assertTrue(self.boards.pending_import(self.group, now=103))
        self.boards.finish_import(self.group, token)
        self.assertFalse(self.boards.pending_import(self.group, now=103))

    def test_pending_upload_expires_and_can_be_cancelled(self):
        self.boards.begin_import(self.group, now=100)
        self.assertFalse(self.boards.pending_import(
            self.group, now=100 + trickcal_board.IMPORT_SESSION_TTL + 1
        ))
        self.boards.begin_import(self.group, now=200)
        self.assertTrue(self.boards.cancel_import(self.group))
        self.assertFalse(self.boards.cancel_import(self.group))


class BoardDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.group = Identity("bot", "group:one", "member", False)
        trickcal_board._catalog_cache.clear()
        trickcal_board._catalog_expiry.clear()

    async def test_web_login_supports_group_pairing_and_private_link(self):
        from qbot_trickcal.trickcal_web import BoardWeb

        with patch.dict(os.environ, {
            "TRICKCAL_WEB_PUBLIC_URL": "https://bot.example.com/tr-board/"
        }):
            group_reply = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 网页"
            )
        self.assertIn("https://bot.example.com/tr-board/", group_reply)
        self.assertIn("回执码", group_reply)
        request_code = re.search(r"[A-F0-9]{16}", group_reply).group(0)
        web = BoardWeb(self.store)
        browser, confirmation, _csrf, _expires = web.begin_pairing(request_code)
        confirmed = await trickcal_board.dispatch(
            self.store, self.group, f"蜡笔板 绑定 {confirmation}"
        )
        self.assertIn("网页身份已确认", confirmed)
        self.assertNotIn("机器人管理后台", confirmed)
        self.assertTrue(web.pairing_status(browser)["confirmed"])

        private = Identity("bot", "private:member", "member", True)
        with patch.dict(os.environ, {
            "TRICKCAL_WEB_PUBLIC_URL": "https://bot.example.com/tr-board/"
        }):
            reply = await trickcal_board.dispatch(self.store, private, "蜡笔板 网页")
        self.assertIn("https://bot.example.com/tr-board/#login=", reply)
        self.assertIn("10 分钟内有效", reply)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM trickcal_web_links").fetchone()[0], 1
            )

    async def test_web_binding_code_links_a_second_bot_to_the_same_board(self):
        from qbot_trickcal.trickcal_web import BoardWeb

        boards = trickcal_board.BoardStore(self.store)
        boards.add_unit(self.group, 10016)
        web = BoardWeb(self.store)
        code, _expires = web.issue_bot_pairing({
            "owner": trickcal_board._owner(self.group), "bot": self.group.bot,
        })
        second_bot = Identity(
            "second-bot", "group:another", "different-app-openid", False
        )
        reply = await trickcal_board.dispatch(
            self.store, second_bot, f"蜡笔板 绑定 {code}"
        )
        self.assertIn("新机器人绑定成功", reply)
        self.assertIn("同一份数据", reply)
        linked = boards.get(second_bot)
        self.assertIsNotNone(linked)
        self.assertEqual(linked[0]["units"], [[10016, 1]])

    async def test_empty_board_explains_group_and_private_web_login(self):
        with self.assertRaises(ToolError) as group_error:
            await trickcal_board.dispatch(self.store, self.group, "蜡笔板")
        group_message = str(group_error.exception)
        self.assertIn("网页使用方法", group_message)
        self.assertIn("当前群发送 /tr 蜡笔板 网页", group_message)
        self.assertIn("/tr 蜡笔板 绑定 回执码", group_message)

        private = Identity("bot", "private:member", "member", True)
        with self.assertRaises(ToolError) as private_error:
            await trickcal_board.dispatch(self.store, private, "蜡笔板")
        private_message = str(private_error.exception)
        self.assertIn("私聊发送 /tr 蜡笔板 网页", private_message)
        self.assertIn("单次登录链接", private_message)

    async def test_group_can_import_and_query_stats_role_and_plan(self):
        raw = json.dumps(export_document(selected=[100, 105]), separators=(",", ":"))
        imported = await trickcal_board.dispatch(self.store, self.group, "蜡笔板 导入 " + raw)
        self.assertIn("蜡笔板已导入", imported)
        self.assertIn("百分比节点、金币和金蜡笔将在查看时", imported)

        transport = catalogue_transport()
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            overview = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板", transport=transport
            )
            detail = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 角色 Erpin", transport=transport
            )
            simplified_detail = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 角色 艾尔芬", transport=transport
            )
            localized_detail = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 角色 埃尔芬", transport=transport
            )
            plan = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 计划", transport=transport
            )
        self.assertNotIn("全体物理攻击 +10", overview)
        self.assertIn("全体物理攻击 +2.5%", overview)
        self.assertIn("百分比已点节点：1 个 · 百分比计划节点：1 个", overview)
        self.assertIn("百分比节点已用金币：100 · 已使用金蜡笔：2 支", overview)
        self.assertIn("百分比节点计划金币：200 · 计划金蜡笔：4 支", overview)
        self.assertNotIn("1,099", overview)
        self.assertNotIn("101 支", overview)
        self.assertIn("计划新增百分比加成", overview)
        self.assertIn("第一层：攻击 1/1（差 0）", overview)
        self.assertIn("第二层：防御 0/1（差 1）", overview)
        self.assertNotIn("0/0", overview)
        self.assertIn("血量 0/1（差 1）", overview)
        self.assertIn("暴击/暴伤 0/1（差 1）", overview)
        self.assertIn("暴抗/暴伤抗 0/1（差 1）", overview)
        self.assertIn("艾尔芬/Erpin", detail)
        self.assertIn("艾尔芬/Erpin", simplified_detail)
        self.assertIn("艾尔芬/Erpin", localized_detail)
        self.assertNotIn("艾爾芬", detail)
        self.assertIn("第一层：攻击 1/1（差 0）", detail)
        self.assertNotIn("0/0", detail)
        self.assertIsInstance(detail, trickcal_board.ActionReply)
        self.assertEqual(
            [item.label for item in detail.actions],
            ["1层血量", "1层暴抗", "2层防御", "2层暴击", "3层攻击"],
        )
        self.assertEqual(
            [item.command for item in detail.actions],
            [
                "/tr 蜡笔板 加点 10016 1 生命",
                "/tr 蜡笔板 加点 10016 1 暴抗",
                "/tr 蜡笔板 加点 10016 2 防御",
                "/tr 蜡笔板 加点 10016 2 暴击",
                "/tr 蜡笔板 加点 10016 3 攻击",
            ],
        )
        self.assertIn("百分比已点 1/6 · 计划 1", detail)
        self.assertIn("百分比节点计划金币：200 · 计划金蜡笔：4 支", detail)
        self.assertIn("艾尔芬/Erpin · 百分比计划 1 个 · 金币 200 · 金蜡笔 4 支", plan)
        self.assertNotIn("· ID ", plan)

    async def test_missing_percent_nodes_includes_owned_units_without_board_progress(self):
        document = export_document()
        document["units"].append([10017, 3])
        raw = json.dumps(document, separators=(",", ":"))
        await trickcal_board.dispatch(self.store, self.group, "蜡笔板 导入 " + raw)

        transport = catalogue_transport()
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            attack = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 未点 百分比攻击力", transport=transport
            )
            defense = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 未点 防御", transport=transport
            )
            anti_crit = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 未点 防爆", transport=transport
            )
            first_attack = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 未点 一层 攻击", transport=transport
            )
            second_defense = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 未点 防御 二层", transport=transport
            )
            empty_detail = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 角色 Aya", transport=transport
            )
        self.assertIn("艾尔芬/Erpin", attack)
        self.assertIn("艾尔芬/Erpin · 已点 1/2 · 差 1（第三层差1）", attack)
        self.assertNotIn("· ID ", attack)
        self.assertNotIn("\n已点", attack)
        self.assertIn("第三层差1", attack)
        self.assertIn("艾雅/Aya", attack)
        self.assertIn("第一层差1", attack)
        self.assertIn("已计划 1", defense)
        self.assertNotIn("艾雅/Aya", defense)
        self.assertIn("艾尔芬/Erpin", anti_crit)
        self.assertIn("未点第一层百分比攻击", first_attack)
        self.assertIn("艾雅/Aya · 已点 0/1 · 差 1", first_attack)
        self.assertNotIn("（第一层", first_attack)
        self.assertIn("艾雅/Aya", first_attack)
        self.assertNotIn("艾尔芬/Erpin", first_attack)
        self.assertIn("未点第二层百分比防御", second_defense)
        self.assertIn("艾尔芬/Erpin", second_defense)
        self.assertNotIn("艾雅/Aya", second_defense)
        self.assertIn("第一层：攻击 0/1（差 1）", empty_detail)

    async def test_paginated_role_list_exposes_previous_and_next_commands(self):
        document = export_document(selected=[], planned=[])
        document["units"] = [[10000 + index, 1] for index in range(9)]
        document["boards"] = []
        await trickcal_board.dispatch(
            self.store, self.group,
            "蜡笔板 导入 " + json.dumps(document, separators=(",", ":")),
        )
        transport = catalogue_transport()
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            first = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 角色列表", transport=transport,
            )
            second = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 角色列表 2", transport=transport,
            )
        self.assertIsInstance(first, trickcal_board.PaginatedReply)
        self.assertIsNone(first.previous_command)
        self.assertEqual(first.next_command, "/tr 蜡笔板 角色列表 2")
        self.assertEqual(first.pages, 2)
        self.assertEqual(second.previous_command, "/tr 蜡笔板 角色列表 1")
        self.assertIsNone(second.next_command)
        self.assertNotIn("· ID ", first)
        self.assertNotIn("\n已点", first)

    async def test_unowned_list_uses_simplified_names_and_excludes_owned_roles(self):
        await trickcal_board.dispatch(
            self.store, self.group,
            "蜡笔板 导入 " + json.dumps(export_document(), separators=(",", ":")),
        )
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            reply = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 未拥有",
                transport=catalogue_transport(),
            )
        self.assertIsInstance(reply, trickcal_board.PaginatedReply)
        self.assertIn("未点亮角色", reply)
        self.assertIn("艾雅/Aya", reply)
        self.assertNotIn("艾尔芬/Erpin", reply)
        self.assertNotIn("艾爾芬", reply)

    async def test_remove_owned_list_clears_each_roles_board_nodes(self):
        document = export_document()
        document["units"].append([10017, 3])
        document["boards"].append([10017, {
            "selectedNodes": [200], "plannedNodes": [201],
        }])
        await trickcal_board.dispatch(
            self.store, self.group,
            "蜡笔板 导入 " + json.dumps(document, separators=(",", ":")),
        )
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            reply = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 取消点亮 艾尔芬，Aya",
                transport=catalogue_transport(),
            )
        self.assertIn("本次移除：2 个 · 剩余拥有：0 个", reply)
        saved, _updated = trickcal_board.BoardStore(self.store).get(self.group)
        self.assertEqual(saved["units"], [])
        self.assertEqual(saved["boards"], [])

    async def test_remove_all_owned_roles_requires_explicit_confirmation(self):
        await trickcal_board.dispatch(
            self.store, self.group,
            "蜡笔板 导入 " + json.dumps(export_document(), separators=(",", ":")),
        )
        with self.assertRaisesRegex(ToolError, "全部 确认"):
            await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 取消点亮 全部",
            )
        saved, _updated = trickcal_board.BoardStore(self.store).get(self.group)
        self.assertEqual(saved["units"], [[10016, 3]])
        reply = await trickcal_board.dispatch(
            self.store, self.group, "蜡笔板 取消点亮 全部 确认",
        )
        self.assertIn("范围：全部角色", reply)
        saved, _updated = trickcal_board.BoardStore(self.store).get(self.group)
        self.assertEqual(saved["units"], [])
        self.assertEqual(saved["boards"], [])

    async def test_owner_can_manually_unlock_mark_and_unmark_percentage_nodes(self):
        raw = json.dumps(export_document(), separators=(",", ":"))
        await trickcal_board.dispatch(self.store, self.group, "蜡笔板 导入 " + raw)
        transport = catalogue_transport()
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            unlocked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 点亮 Aya", transport=transport
            )
            marked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 埃尔芬 第三层 攻击", transport=transport
            )
            planned_marked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 Erpin 2 防御", transport=transport
            )
            critical_marked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 Erpin 2 暴伤", transport=transport
            )
            critical_duplicate = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 Erpin 2 暴击", transport=transport
            )
            critical_unmarked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 撤销加点 Erpin 2 暴击", transport=transport
            )
            anti_critical_marked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 Erpin 1 防爆", transport=transport
            )
            unmarked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 撤销加点 Erpin 3 攻击", transport=transport
            )
        self.assertIn("新角色已点亮", unlocked)
        self.assertIn("第三层 · 百分比攻击", marked)
        self.assertIn("第二层 · 百分比防御", planned_marked)
        self.assertIn("第二层 · 百分比暴击/暴伤", critical_marked)
        self.assertIn("原本已经点亮", critical_duplicate)
        self.assertIn("撤销加点已记录", critical_unmarked)
        self.assertIn("第一层 · 百分比暴抗/暴伤抗", anti_critical_marked)
        self.assertIn("撤销加点已记录", unmarked)
        document, _updated = trickcal_board.BoardStore(self.store).get(self.group)
        self.assertIn([10017, 1], document["units"])
        board = dict(document["boards"])[10016]
        self.assertEqual(board["selectedNodes"], [100, 101, 106])
        self.assertEqual(board["plannedNodes"], [])

    async def test_first_add_point_creates_board_and_unlocks_role_automatically(self):
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            reply = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 Aya 1 攻击",
                transport=catalogue_transport(),
            )
        self.assertIn("加点已记录", reply)
        self.assertIn("已同时创建你的蜡笔板并点亮该角色", reply)
        document, _updated = trickcal_board.BoardStore(self.store).get(self.group)
        self.assertEqual(document["units"], [[10017, 1]])
        self.assertEqual(document["boards"], [[10017, {
            "selectedNodes": [200], "plannedNodes": [],
        }]])

    async def test_can_unlock_all_and_bulk_mark_or_unmark_owned_roles(self):
        transport = catalogue_transport()
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            unlocked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 点亮 全部", transport=transport,
            )
            repeated = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 点亮 所有角色", transport=transport,
            )
            marked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 全部 1 攻击", transport=transport,
            )
            marked_again = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 all 第一层 攻击力", transport=transport,
            )
            unmarked = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 撤销加点 全角色 1 攻击", transport=transport,
            )
            all_attributes = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 加点 全部 1 全部", transport=transport,
            )
            all_attributes_removed = await trickcal_board.dispatch(
                self.store, self.group, "蜡笔板 撤销加点 全部 1 所有属性", transport=transport,
            )
        self.assertIn("本次新增：2 个", unlocked)
        self.assertIn("没有点亮任何蜡笔节点", unlocked)
        self.assertIn("本次新增：0 个", repeated)
        self.assertIn("适用角色：2 个", marked)
        self.assertIn("本次变更：2 个角色 · 2 个节点", marked)
        self.assertIn("批量加点无需更新", marked_again)
        self.assertIn("本次变更：2 个角色 · 2 个节点", unmarked)
        self.assertIn("第一层 · 全部百分比属性", all_attributes)
        self.assertIn("本次变更：2 个角色 · 4 个节点", all_attributes)
        self.assertIn("本次变更：2 个角色 · 4 个节点", all_attributes_removed)
        document, _updated = trickcal_board.BoardStore(self.store).get(self.group)
        self.assertEqual(document["units"], [[10016, 1], [10017, 1]])
        self.assertEqual(document["boards"], [])

    async def test_comma_separated_role_list_is_validated_then_changed_together(self):
        transport = catalogue_transport()
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            marked = await trickcal_board.dispatch(
                self.store, self.group,
                "蜡笔板 加点 埃尔芬， 艾雅 3 攻击",
                transport=transport,
            )
            removed = await trickcal_board.dispatch(
                self.store, self.group,
                "蜡笔板 撤销加点 Erpin、Aya 3 攻击",
                transport=transport,
            )
        self.assertIn("指定角色 2 个", marked)
        self.assertIn("本次变更：2 个角色 · 2 个节点", marked)
        self.assertIn("同时点亮新角色：2 个", marked)
        self.assertIn("本次变更：2 个角色 · 2 个节点", removed)
        document, _updated = trickcal_board.BoardStore(self.store).get(self.group)
        self.assertEqual(document["units"], [[10016, 1], [10017, 1]])
        self.assertEqual(document["boards"], [])

    async def test_invalid_role_in_explicit_batch_does_not_create_partial_record(self):
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            with self.assertRaisesRegex(ToolError, "本次没有修改任何记录"):
                await trickcal_board.dispatch(
                    self.store, self.group,
                    "蜡笔板 加点 艾尔芬,Aya 1 防御",
                    transport=catalogue_transport(),
                )
        self.assertIsNone(trickcal_board.BoardStore(self.store).get(self.group))

    async def test_manual_mark_rejects_layer_without_that_percentage_node(self):
        raw = json.dumps(export_document(), separators=(",", ":"))
        await trickcal_board.dispatch(self.store, self.group, "蜡笔板 导入 " + raw)
        with patch.dict(os.environ, {"BOT_DATA_DIR": self.temp.name}):
            with self.assertRaisesRegex(ToolError, "没有百分比防御节点"):
                await trickcal_board.dispatch(
                    self.store, self.group, "蜡笔板 加点 Erpin 1 防御",
                    transport=catalogue_transport(),
                )

    async def test_daily_catalogue_refresh_runs_once_for_each_18_clock_slot(self):
        scheduled = 1789034400.0  # 2026-09-10 18:00:00 UTC+8
        slot = trickcal_board._daily_catalog_slot(scheduled)
        self.assertEqual(slot.isoformat(), "2026-09-10T18:00:00+08:00")
        before = trickcal_board._daily_catalog_slot(scheduled - 1)
        self.assertEqual(before.isoformat(), "2026-09-09T18:00:00+08:00")
        tomorrow = trickcal_board._daily_catalog_slot(scheduled + 86400)
        self.assertEqual(tomorrow.isoformat(), "2026-09-11T18:00:00+08:00")
        fresh = trickcal_board.Catalog({}, {}, {}, scheduled)
        with patch.object(trickcal_board, "catalogue", AsyncMock(return_value=fresh)) as refresh:
            self.assertTrue(await trickcal_board.refresh_catalogue_if_due(
                self.store, now=scheduled
            ))
            self.assertFalse(await trickcal_board.refresh_catalogue_if_due(
                self.store, now=scheduled + 60
            ))
        refresh.assert_awaited_once_with(None, force_refresh=True)

    async def test_delete_requires_confirmation(self):
        raw = json.dumps(export_document())
        await trickcal_board.dispatch(self.store, self.group, "蜡笔板 导入 " + raw)
        with self.assertRaisesRegex(ToolError, "删除后"):
            await trickcal_board.dispatch(self.store, self.group, "蜡笔板 删除")
        reply = await trickcal_board.dispatch(self.store, self.group, "蜡笔板 删除 确认")
        self.assertIn("已删除", reply)

    async def test_json_attachment_import_uses_qq_cdn_and_size_limit(self):
        body = json.dumps(export_document()).encode()

        def handler(request: httpx.Request):
            self.assertEqual(request.url.host, "gchat.qpic.cn")
            return httpx.Response(200, content=body, headers={"content-type": "application/json"})

        attachment = SimpleNamespace(
            url="https://gchat.qpic.cn/export.json", filename="progress.json",
            content_type="application/json; charset=utf-8", size=len(body),
        )
        reply = await trickcal_board.dispatch(
            self.store, self.group, "蜡笔板 导入", attachments=[attachment],
            transport=httpx.MockTransport(handler),
        )
        self.assertIn("蜡笔板已导入", reply)

    async def test_command_then_separate_json_upload_imports_and_clears_wait(self):
        body = json.dumps(export_document()).encode()

        def handler(request: httpx.Request):
            self.assertEqual(request.url.host, "gzc-download.ftn.qq.com")
            return httpx.Response(200, content=body, headers={"content-type": "application/json"})

        attachment = SimpleNamespace(
            url="https://gzc-download.ftn.qq.com/ftn_handler/export?fname=progress.json",
            filename="progress.json",
            content_type="application/json", size=len(body),
        )
        waiting = await trickcal_board.dispatch(self.store, self.group, "蜡笔板 导入")
        self.assertIn("等待上传蜡笔板文件", waiting)
        self.assertTrue(trickcal_board.BoardStore(self.store).pending_import(self.group))
        imported = await trickcal_board.accept_pending_upload(
            self.store, self.group, [attachment], transport=httpx.MockTransport(handler)
        )
        self.assertIn("蜡笔板已导入", imported)
        self.assertFalse(trickcal_board.BoardStore(self.store).pending_import(self.group))
        self.assertIsNotNone(trickcal_board.BoardStore(self.store).get(self.group))

    async def test_failed_separate_upload_keeps_waiting(self):
        attachment = SimpleNamespace(
            url="https://gchat.qpic.cn/not-json.png", filename="not-json.png",
            content_type="image/png", size=10,
        )
        await trickcal_board.dispatch(self.store, self.group, "蜡笔板 导入")
        with self.assertRaisesRegex(ToolError, "json"):
            await trickcal_board.accept_pending_upload(self.store, self.group, [attachment])
        self.assertTrue(trickcal_board.BoardStore(self.store).pending_import(self.group))


if __name__ == "__main__":
    unittest.main()
