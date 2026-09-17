from __future__ import annotations

import json
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from PIL import Image, ImageDraw

from qbot_trickcal import trickcal, trickcal_media


CHARACTER_HTML = """
<div class="mw-parser-output">
<p>首页 &gt; 使徒图鉴 &gt; 埃尔芬</p>
<h2>使徒信息</h2>
<table><tr><td>使徒名称</td><td>埃尔芬</td></tr>
<tr><td>稀有度</td><td>3星</td><td>性格</td><td>纯真</td></tr>
<tr><td>职业</td><td>输出</td><td>攻击类型</td><td>魔法</td></tr>
<tr><td>站位</td><td>后排</td><td>种族</td><td>仙灵</td></tr>
<tr><td>称号</td><td>仙灵女王</td><td>TMI</td><td>讨厌蔬菜</td></tr>
<tr><td>角色描述</td><td>喜欢甜点，也在用自己的方式努力。</td></tr></table>
<h2>使徒技能</h2>
<table><tr><td>技能名称</td><td>魔弹暴走</td></tr>
<tr><td>技能描述</td><td>对随机敌方目标造成范围魔法伤害。</td></tr>
<tr><td>技能效果</td><td>总魔法伤害：1118.7%</td></tr></table>
</div>
"""

ATLAS_HTML = """
<table><tr><th>使徒名称</th><th>稀有度</th><th>性格</th><th>职业</th><th>攻击类型</th><th>站位</th><th>种族</th></tr>
<tr><td>埃尔芬</td><td>3星</td><td>纯真</td><td>输出</td><td>魔法</td><td>后排</td><td>仙灵</td></tr></table>
"""


def editor(text):
    return {"type": "simpleEditor", "data": [{"type": "paragraph", "children": [{"text": text}]}]}


GAMEKEE_DOCUMENT = [
    {"type": "image", "title": "使徒形象", "content": [
        "//cdnimg-v2.gamekee.com/wiki2.0/images/test/portrait.png"
    ]},
    {"type": "character-profile", "data": {
        "title": "使徒信息",
        "name": editor("埃尔芬/Erpin"),
        "attrList": [
            {"title": editor("性格"), "content": editor("纯粹")},
            {"title": editor("职业"), "content": editor("输出")},
            {"title": editor("攻击类型"), "content": editor("魔法")},
            {"title": editor("站位"), "content": editor("后排")},
            {"title": editor("种族"), "content": editor("妖精")},
            {"title": editor("称号"), "content": editor("妖精女王")},
        ],
    }},
    {"type": "skill-info", "data": {
        "title": "使徒技能",
        "skillList": [{"name": editor("魔弹暴走"),
                       "desc": editor("对随机敌方目标造成范围魔法伤害。")}],
    }},
    {"type": "upgrade-info", "data": {
        "title": "食物喜好", "upgradeList": [
            {"title": editor("超喜欢"), "content": []},
            {"title": {"type": "simpleEditor", "data": [{"type": "paragraph", "children": [
                {"type": "image", "src": "//cdnimg-v2.gamekee.com/wiki2.0/images/test/cake.png"}
            ]}]}, "content": [editor("草莓蛋糕")]},
        ],
    }},
    {"type": "upgrade-info", "data": {
        "title": "蜡笔板全体加成", "upgradeList": [{
            "title": editor("Level 1"), "content": [{
                "type": "simpleEditor", "data": [{"type": "paragraph", "children": [
                    {"type": "image", "src": "//cdnimg-v2.gamekee.com/wiki2.0/images/test/crayon-a.png"},
                    {"type": "image", "src": "//cdnimg-v2.gamekee.com/wiki2.0/images/test/crayon-b.png"},
                ]}],
            }], "other": [editor("13")],
        }],
    }},
]


def text_node(text, node_type="paragraph"):
    return {"type": node_type, "children": [{"text": text}]}


COUPON_DOCUMENT = [
    text_node("未过期：", "header1"),
    text_node("国服", "header2"),
    *[text_node(code) for code in (
        "C108SPLIVE", "TIMETRAVEL", "RENEWA623", "ETERNAL", "POWERTOWER",
        "0621PUNI2", "CNTEST07", "CNTEST08", "CNTEST09", "CNTEST10", "CNTEST11",
    )],
    text_node("韩服", "header2"),
    text_node("BOL100MANGAE"),
    text_node("◈ 使用期限 ◈"),
    text_node("8月31日 公告发布后 ~ 9月10日 09:59（北京时间）"),
    text_node("TRICKCALCOM"),
    text_node("◈ 使用期限 ◈"),
    text_node("2026年6月18日（周四）定期维护后 至 另行通知时为止"),
    text_node("国际服", "header2"),
    text_node("SHOULDNOTSHOW"),
]


def tiny_png(color="orange"):
    output = io.BytesIO()
    Image.new("RGBA", (80, 100), color).save(output, "PNG")
    return output.getvalue()


def json_response(payload, status=200, headers=None):
    return httpx.Response(status, content=json.dumps(payload, ensure_ascii=False).encode(),
                          headers=headers or {"content-type": "application/json"})


class TrickcalParserTests(unittest.TestCase):
    def test_character_panel_hides_gamekee_tag_placeholder(self):
        document = [{"type": "character-profile", "data": {
            "attrList": [
                {"title": editor("TAG"),
                 "content": editor("不同 TAG 用不同 颜色 区分")},
            ],
        }}]
        entry = trickcal.GameKeeEntry(1, 2, "测试角色", "", "国际服3星使徒", 0)
        detail = {"title": "测试角色", "content_json": json.dumps(document)}

        reply = trickcal._gamekee_detail_panel("角色", entry, detail, "国服")

        self.assertNotIn("TAG：", reply)
        self.assertNotIn("不同 TAG", reply)

        document[0]["data"]["attrList"][0]["content"] = editor("自身SP恢复")
        detail["content_json"] = json.dumps(document)
        reply = trickcal._gamekee_detail_panel("角色", entry, detail, "国服")
        self.assertIn("TAG：自身SP恢复", reply)

    def test_long_card_separates_and_styles_each_skill(self):
        canvas = Image.new("RGB", (720, 20), "white")
        fonts = {
            "title": trickcal_media.font(24),
            "section": trickcal_media.font(22),
            "skill": trickcal_media.font(20),
            "skill_body": trickcal_media.font(20),
            "meta": trickcal_media.font(20),
            "body": trickcal_media.font(20),
            "footer": trickcal_media.font(15),
            "link": trickcal_media.font(16),
        }
        rows = trickcal_media._long_card_detail_rows(
            ImageDraw.Draw(canvas),
            "🍞 嘟嘟脸 · 测试\n\n技能摘要\n"
            "• 技能一：第一段说明。\n\n• 技能二：第二段说明。",
            fonts,
        )
        skill_rows = [row for row in rows if row.style == "skill"]
        body_rows = [row for row in rows if row.style == "skill_body"]

        self.assertEqual([row.text for row in skill_rows], ["● 技能一", "● 技能二"])
        self.assertEqual(len(body_rows), 2)
        self.assertGreaterEqual(skill_rows[1].gap_before, 15)

    def test_skill_parser_splits_badge_from_name_and_keeps_normal_attack(self):
        named = lambda title: {"type": "simpleEditor", "data": [{
            "type": "paragraph", "children": [{"text": title}],
        }]}
        first_name = {"type": "simpleEditor", "data": [{
            "type": "paragraph", "children": [
                {"text": "傻里傻气黑色行动      "},
                {"type": "image", "src": "//cdnimg-v2.gamekee.com/icon.png"},
                {"text": "SP恢复量：50/秒", "color": "blue"},
            ],
        }]}
        skills = [{"name": first_name, "desc": {
            "type": "simpleEditor", "data": [
                {"type": "paragraph", "children": [{"text": "提升友军攻击力。"}]},
                {"type": "paragraph", "children": [{"text": "攻击力增加：20%"}]},
            ],
        }}]
        cooldown_name = {"type": "simpleEditor", "data": [{
            "type": "paragraph", "children": [
                {"text": "投降！投降了啦……    "},
                {"text": "冷却时间：18秒", "color": "blue"},
            ],
        }]}
        skills.append({"name": cooldown_name, "desc": editor("技能说明")})
        for name in ("被动技能", "普通攻击"):
            skills.append({"name": named(name), "desc": editor("技能说明")})
        document = [{"type": "skill-info", "data": {
            "title": "使徒技能", "skillList": skills,
        }}]

        parsed = trickcal._gamekee_skills(document)

        self.assertEqual(parsed[0][0], "傻里傻气黑色行动")
        self.assertEqual(
            parsed[0][1].splitlines(),
            ["提升友军攻击力。"],
        )
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[1][0], "投降！投降了啦……")
        self.assertEqual(parsed[-1][0], "普通攻击")

    def test_skill_effect_summary_removes_exact_values_and_duration_rows(self):
        description = {"type": "simpleEditor", "data": [
            {"type": "paragraph", "children": [
                {"text": "对3名敌人造成150%物理伤害，持续6秒。"},
            ]},
            {"type": "paragraph", "children": [{"text": "挑衅持续时间：6秒"}]},
            {"type": "paragraph", "children": [{"text": "昏迷：无法进行任何行动。"}]},
            {"type": "paragraph", "children": [{"text": "每秒HP恢复：最大HP的0.3%"}]},
        ]}

        paragraphs = trickcal._skill_effect_paragraphs(description)

        self.assertEqual(paragraphs, (
            "对敌人造成物理伤害。",
            "昏迷：无法进行任何行动。",
        ))

    def test_role_matching_accepts_simplified_and_traditional_names(self):
        entries = (
            trickcal.GameKeeEntry(1, 2, "爱丽丝/Alice", "", "国际服", 0),
        )
        matches = trickcal._role_matches(entries, "愛麗絲", "国服")
        self.assertEqual(matches, entries)

    def test_role_matching_maps_cross_region_localized_names(self):
        pairs = (
            (0, "宁琉", 601401, "涅尔/Ner"),
            (680856, "斯皮奇", 603280, "斯碧琪/Speaki"),
            (694921, "罗涅", 601368, "洛涅/Rohne"),
            (680806, "艾尔芬", 601167, "埃尔芬/Erpin"),
            (695361, "奈雅", 604656, "奈亚/Naia"),
            (680804, "加薇雅", 603148, "加维亚/Gabia"),
            (680857, "马尔", 603151, "玛戈/Mago"),
            (680877, "佩佩", 601285, "薇尔薇特/Velvet"),
            (680865, "杰德", 601303, "婕德/Jade"),
            (680807, "库洛艾", 601370, "克萝伊/Chloe"),
            (680808, "谢蒂", 601400, "夏迪/Shady"),
            (680842, "希瑟图", 601402, "茜斯特/Sist"),
            (680812, "蒂亚娜", 601404, "黛安娜/Diana"),
            (680893, "路德", 601405, "鲁德/Rude"),
            (698479, "修帕", 619863, "舒胖/Shoupan"),
            (680901, "x㐅锡安㐅x", 601293, "x锡安x/xXionx"),
            (690347, "布兰切", 617006, "布蓝琪/Blanchet"),
            (686037, "艾琳娜", 601297, "埃蕾娜/Elena"),
            (680803, "优米", 688399, "优米/yomi"),
        )
        entries = []
        for index, (intl_id, intl_name, korean_id, korean_name) in enumerate(pairs):
            entries.extend((
                trickcal.GameKeeEntry(index * 2, intl_id, intl_name, "", "国际服3星使徒", 0),
                trickcal.GameKeeEntry(index * 2 + 1, korean_id, korean_name, "", "韩服3星使徒", 0),
            ))
        # These two used to steal 修帕 and 艾琳娜 through one-character fuzzy matching.
        entries.extend((
            trickcal.GameKeeEntry(100, 649560, "修罗/Suro", "", "韩服3星使徒", 0),
            trickcal.GameKeeEntry(101, 608627, "赛琳娜/Selline", "", "韩服3星使徒", 0),
        ))
        entries = tuple(entries)
        for _, intl_name, korean_id, _ in pairs:
            with self.subTest(name=intl_name):
                matches = trickcal._role_matches(entries, intl_name, "韩服")
                self.assertEqual([item.content_id for item in matches], [korean_id])

    def test_ner_uses_korean_exact_match_instead_of_international_fuzzy_guess(self):
        entries = (
            trickcal.GameKeeEntry(1, 680857, "马尔", "", "国际服3星使徒", 0),
            trickcal.GameKeeEntry(2, 601401, "涅尔/Ner", "ner,ne,nieer", "韩服3星使徒", 0),
            trickcal.GameKeeEntry(
                3, 699291, "涅尔（义愤）/NerRage", "nier,ner,nerrage", "韩服3星使徒", 0,
            ),
        )

        matches, region, fallback = trickcal._select_role_matches(entries, "涅尔", "国服")

        self.assertEqual([item.content_id for item in matches], [601401])
        self.assertEqual(region, "韩服")
        self.assertTrue(fallback)
        self.assertEqual(trickcal._role_matches(entries, "宁琉", "韩服")[0].content_id, 601401)

    def test_role_aliases_are_loaded_from_the_editable_json_file(self):
        self.assertEqual(
            trickcal._role_equivalents("寧琉"),
            ("Ner", "涅尔", "涅爾", "尼尔"),
        )
        self.assertTrue(trickcal.ROLE_NAME_MAP_FILE.is_file())
        document = json.loads(trickcal.ROLE_NAME_MAP_FILE.read_text(encoding="utf-8"))
        self.assertEqual(document["Soshage角色数"], 76)
        self.assertEqual(len(document["角色映射"]), 76)
        self.assertEqual(set(document["角色映射"]), {
            group[0] for group in trickcal.ROLE_NAME_GROUPS
        })

    def test_animated_portrait_uses_bounded_gamekee_thumbnail(self):
        raw_url = "//cdnimg-v2.gamekee.com/wiki2.0/images/w_464/h_580/a/portrait.gif"
        spec = trickcal_media.extract_character_visual(
            [{"type": "image", "title": "使徒形象", "content": [raw_url]}],
            "斯皮奇", "国服",
        )
        self.assertIsNotNone(spec)
        self.assertTrue(spec.portrait_url.endswith(
            "?" + trickcal_media.GAMEKEE_STATIC_GIF_QUERY
        ))
        self.assertTrue(trickcal_media.valid_image_url(spec.portrait_url))
        self.assertFalse(trickcal_media.valid_image_url(
            "https://cdnimg-v2.gamekee.com/wiki2.0/images/a.gif?x=unbounded"
        ))

    def test_character_card_disk_cache_expires_after_seven_days(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"BOT_DATA_DIR": temp}):
            key = trickcal._character_card_disk_key(601167, 1788564525, "韩服")
            image = b"\xff\xd8\xffcached-jpeg"
            trickcal._write_character_card_cache(key, image)
            path = Path(temp) / "cache" / "trickcal-cards" / f"{key}.jpg"
            self.assertEqual(trickcal._read_character_card_cache(key), image)
            expired = time.time() - trickcal.CHARACTER_CARD_CACHE_TTL - 1
            os.utime(path, (expired, expired))
            self.assertIsNone(trickcal._read_character_card_cache(key))
            self.assertFalse(path.exists())

    def test_character_tables_are_structured_without_html_noise(self):
        parsed = trickcal.parse_html(CHARACTER_HTML)
        page = trickcal.WikiPage("埃尔芬", 5987, "2026-04-12T21:10:52Z", parsed)
        reply = trickcal.format_page(page, "角色")
        self.assertIn("3星 · 纯真 · 输出", reply)
        self.assertIn("魔法 · 后排 · 仙灵", reply)
        self.assertIn("魔弹暴走", reply)
        self.assertIn("2026-04-13", reply)
        self.assertIn("https://wiki.biligame.com/tk/", reply)
        self.assertNotIn("<td>", reply)

    def test_command_aliases_direct_name_and_validation(self):
        self.assertEqual(trickcal.parse_command("埃尔芬"), ("角色", "埃尔芬"))
        self.assertEqual(trickcal.parse_command("char 埃尔芬"), ("角色", "埃尔芬"))
        self.assertEqual(trickcal.parse_command("礼包码"), ("兑换码", "国服"))
        self.assertEqual(trickcal.parse_command("兑换码 韩服"), ("兑换码", "韩服"))
        self.assertEqual(trickcal.parse_command("随机"), ("随机角色", "国服"))
        self.assertEqual(trickcal.parse_command("随机角色 韩服"), ("随机角色", "韩服"))
        with self.assertRaises(trickcal.TrickcalError):
            trickcal.parse_command("兑换码 国际服")
        with self.assertRaises(trickcal.TrickcalError):
            trickcal.parse_command("角色 " + "很" * 41)

    def test_directory_is_compact_and_complete(self):
        text = trickcal.directory()
        for command in ("角色", "卡牌", "宠物", "食物", "攻略", "兑换码", "搜索", "随机角色"):
            self.assertIn(command, text)
        self.assertLessEqual(len(text.encode()), 1800)


class TrickcalNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        trickcal._cache.clear()

    def transport(self):
        calls = []

        def handler(request: httpx.Request):
            calls.append(request)
            if request.url.host == "cdnimg-v2.gamekee.com":
                return httpx.Response(200, content=tiny_png(), headers={"content-type": "image/png"})
            self.assertEqual(request.url.host, "www.gamekee.com")
            if request.url.path == "/v1/entry/treesByPid":
                return json_response({"code": 0, "data": [
                    {"id": 127887, "name": "国际服3星使徒", "child": [{
                        "id": 127929, "content_id": 680806, "name": "艾尔芬",
                        "name_alias": "", "bind_updated": 1788564525,
                    }]},
                    {"id": 127886, "name": "韩服3星使徒", "child": [{
                        "id": 127928, "content_id": 601167, "name": "埃尔芬/Erpin",
                        "name_alias": "aef,aierfen", "bind_updated": 1788564525,
                    }, {
                        "id": 127930, "content_id": 699294, "name": "埃尔芬（王道）/ErpinRoyale",
                        "name_alias": "ErpinRoyale", "bind_updated": 1788564525,
                    }]},
                ]})
            if request.url.path in {"/v1/content/detail/601167", "/v1/content/detail/680806",
                                    "/v1/content/detail/699294"}:
                international = request.url.path.endswith("680806")
                royale = request.url.path.endswith("699294")
                return json_response({"code": 0, "data": {
                    "id": 680806 if international else (699294 if royale else 601167),
                    "title": "艾尔芬" if international else (
                        "埃尔芬（王道）/ErpinRoyale" if royale else "埃尔芬/Erpin"
                    ),
                    "updated_at": 1788564525,
                    "content_json": json.dumps(GAMEKEE_DOCUMENT, ensure_ascii=False),
                }})
            if request.url.path == "/v1/content/searchArticle":
                return json_response({"code": 0, "data": [{
                    "id": 719590, "title": "新手开荒攻略",
                    "summary": "新手开荒与资源规划", "updated_at": 1788346072,
                }]})
            return json_response({"code": 404, "data": None})

        return httpx.MockTransport(handler), calls

    async def test_character_lookup_uses_search_then_structured_page(self):
        transport, calls = self.transport()
        reply = await trickcal.dispatch("角色 埃尔芬", transport=transport)
        self.assertIn("嘟嘟脸 · 艾尔芬", reply)
        self.assertIn("数据区服：国服", reply)
        self.assertIn("3星 · 纯粹 · 输出 · 魔法", reply)
        self.assertIn("魔弹暴走", reply)
        self.assertIn("GameKee", reply)
        self.assertIn("https://www.gamekee.com/tr/680806.html", reply)
        self.assertEqual(len(calls), 2)
        # The same public lookup is served from the bounded cache.
        self.assertEqual(await trickcal.dispatch("角色 埃尔芬", transport=transport), reply)
        self.assertEqual(len(calls), 2)

    async def test_explicit_korean_region_and_country_falls_back_to_korean(self):
        transport, _ = self.transport()
        reply = await trickcal.dispatch("角色 埃尔芬 韩服", transport=transport)
        self.assertIn("数据区服：韩服", reply)
        self.assertIn("/601167.html", reply)
        fallback = await trickcal.dispatch("角色 埃尔芬（王道） 国服", transport=transport)
        self.assertIn("国服暂无该角色，已自动使用韩服资料", fallback)
        self.assertIn("/699294.html", fallback)

    async def test_character_card_contains_compact_public_wiki_visuals(self):
        transport, calls = self.transport()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"BOT_DATA_DIR": temp}):
            card = await trickcal.character_card("角色 埃尔芬 国服", transport=transport)
            self.assertIsInstance(card, bytes)
            with Image.open(io.BytesIO(card)) as rendered:
                self.assertEqual(rendered.format, "JPEG")
                self.assertEqual(rendered.size, (720, 540))
            media_calls = sum(call.url.host == "cdnimg-v2.gamekee.com" for call in calls)
            self.assertGreater(media_calls, 0)
            files = list((Path(temp) / "cache" / "trickcal-cards").glob("*.jpg"))
            self.assertEqual(len(files), 1)

            # Simulate a process restart: request metadata again, but reuse the
            # seven-day card file instead of downloading and rendering images.
            trickcal._cache.clear()
            self.assertEqual(
                await trickcal.character_card("角色 埃尔芬 国服", transport=transport), card
            )
            self.assertEqual(
                sum(call.url.host == "cdnimg-v2.gamekee.com" for call in calls), media_calls
            )

            long_card = await trickcal.character_card(
                "角色 埃尔芬 国服",
                result="🍞 嘟嘟脸 · 埃尔芬\n\n数据区服：国服\n技能摘要\n" + "角色资料。" * 120,
                transport=transport,
            )
            self.assertIsInstance(long_card, bytes)
            with Image.open(io.BytesIO(long_card)) as rendered:
                self.assertEqual(rendered.format, "JPEG")
                self.assertEqual(rendered.width, 720)
                self.assertGreater(rendered.height, 540)
                self.assertLessEqual(rendered.height, 2600)
            self.assertEqual(len(list((Path(temp) / "cache" / "trickcal-cards").glob("*.jpg"))), 2)

    async def test_character_markdown_uses_public_images_without_rendering(self):
        transport, calls = self.transport()
        result = await trickcal.dispatch("角色 埃尔芬", transport=transport)
        markdown = await trickcal.character_markdown(
            "角色 埃尔芬", result=result, transport=transport,
        )
        self.assertIsInstance(markdown, str)
        self.assertIn("![角色立绘 #232px #290px]", markdown)
        self.assertIn("**喜欢的食物**", markdown)
        self.assertIn("**蜡笔板全体加成**", markdown)
        self.assertFalse(any(call.url.host == "cdnimg-v2.gamekee.com" for call in calls))

    async def test_coupon_list_is_regioned_compact_deduplicated_and_capped(self):
        def handler(request):
            self.assertEqual(request.url.host, "www.gamekee.com")
            if request.url.path == "/v1/content/searchArticle":
                self.assertEqual(request.url.params.get("keyword"), "国服兑换码")
                return json_response({"code": 0, "data": [{
                    "id": 656618, "title": "最新兑换码",
                    "summary": "国服兑换码与韩服兑换码", "updated_at": 1788307112,
                }]})
            if request.url.path == "/v1/content/detail/656618":
                return json_response({"code": 0, "data": {
                    "id": 656618, "title": "最新兑换码", "updated_at": 1788307112,
                    "content_json": json.dumps(COUPON_DOCUMENT, ensure_ascii=False),
                }})
            return json_response({"code": 404, "data": None})

        transport = httpx.MockTransport(handler)
        chinese = await trickcal.dispatch("兑换码", transport=transport)
        self.assertIn("国服兑换码 · 最新 10 个", chinese)
        self.assertIn("C108SPLIVE", chinese)
        self.assertIn("过期：未注明", chinese)
        self.assertNotIn("CNTEST11", chinese)
        self.assertNotIn("SHOULDNOTSHOW", chinese)
        self.assertNotIn("gamekee.com", chinese)

        korean = await trickcal.dispatch("兑换码 韩服", transport=transport)
        self.assertIn("韩服兑换码 · 最新 2 个", korean)
        self.assertIn("BOL100MANGAE\n   过期：9月10日 09:59（北京时间）", korean)
        self.assertIn("TRICKCALCOM\n   过期：另行通知", korean)
        self.assertNotIn("C108SPLIVE", korean)

    async def test_random_role_comes_from_live_atlas(self):
        transport, calls = self.transport()
        fake_random = SimpleNamespace(choice=lambda values: values[0])
        with patch("qbot_trickcal.trickcal.random.SystemRandom", return_value=fake_random):
            reply = await trickcal.dispatch("随机角色", transport=transport)
        self.assertIn("艾尔芬", reply)
        self.assertIn("数据区服：国服", reply)
        self.assertEqual(len(calls), 2)

    async def test_random_role_reuses_the_selected_character_card(self):
        transport, _ = self.transport()
        fake_random = SimpleNamespace(choice=lambda values: values[0])
        with tempfile.TemporaryDirectory() as temp, \
             patch.dict(os.environ, {"BOT_DATA_DIR": temp}), \
             patch("qbot_trickcal.trickcal.random.SystemRandom", return_value=fake_random):
            reply = await trickcal.dispatch("随机角色", transport=transport)
            card = await trickcal.character_card(
                "随机角色", result=reply, transport=transport,
            )
        self.assertIsInstance(card, bytes)
        self.assertIn("/680806.html", reply)

    async def test_search_result_is_sanitized(self):
        def handler(request):
            self.assertEqual(request.url.host, "www.gamekee.com")
            return json_response({"code": 0, "data": [{
                "id": 700001, "title": "<b>新手攻略</b>",
                "summary": "<span class='searchmatch'>新手</span> &amp; 开荒",
                "updated_at": 1767225600,
            }]})
        reply = await trickcal.dispatch("搜索 新手", transport=httpx.MockTransport(handler))
        self.assertIn("新手攻略", reply)
        self.assertIn("新手 & 开荒", reply)
        self.assertNotIn("<span", reply)

    async def test_waf_html_and_oversized_responses_fail_safely(self):
        def waf(_request):
            return httpx.Response(200, text="security challenge", headers={"content-type": "text/html"})
        with self.assertRaisesRegex(trickcal.GameKeeUnavailable, "安全验证"):
            await trickcal._gamekee_request(
                "/v1/content/searchArticle", {"keyword": "WAF-unique"}, httpx.MockTransport(waf)
            )

        def huge(_request):
            return httpx.Response(200, content=b"{" + b"x" * (trickcal.GAMEKEE_MAX_RESPONSE_BYTES + 1),
                                  headers={"content-type": "application/json"})
        with self.assertRaisesRegex(trickcal.GameKeeUnavailable, "内容过大"):
            await trickcal._gamekee_request(
                "/v1/content/searchArticle", {"keyword": "huge-unique"}, httpx.MockTransport(huge)
            )

    async def test_gamekee_outage_falls_back_to_bwiki(self):
        calls = []

        def handler(request):
            calls.append(request.url.host)
            if request.url.host == "www.gamekee.com":
                return httpx.Response(503, headers={"content-type": "application/json"})
            params = request.url.params
            if params.get("list") == "search":
                return json_response({"query": {"search": [{
                    "title": "埃尔芬", "snippet": "使徒图鉴",
                    "timestamp": "2026-04-12T21:10:52Z",
                }]}})
            return json_response({"parse": {
                "title": "埃尔芬", "revid": 5987, "text": CHARACTER_HTML,
            }})

        reply = await trickcal.dispatch("角色 埃尔芬", transport=httpx.MockTransport(handler))
        self.assertIn("BWIKI", reply)
        self.assertEqual(calls, ["www.gamekee.com", "wiki.biligame.com", "wiki.biligame.com"])


if __name__ == "__main__":
    unittest.main()
