"""Safe extraction and compact rendering for public GameKee character art."""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw, UnidentifiedImageError

from bot_tools import http_clients
from bot_tools.media import RENDER_LOCK, font


MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_PIXELS = 12_000_000
ALLOWED_IMAGE_HOSTS = {"cdnimg-v2.gamekee.com"}
GAMEKEE_STATIC_GIF_QUERY = (
    "eo-img.format=webp&"
    "x-image-process=image%2Fformat%2Cwebp%2Fignore-error%2C1"
)


@dataclass(frozen=True)
class FoodVisual:
    group: str
    name: str
    image_url: str


@dataclass(frozen=True)
class BonusVisual:
    level: str
    icons: tuple[str, ...]


@dataclass(frozen=True)
class CharacterVisual:
    name: str
    region: str
    portrait_url: str
    foods: tuple[FoodVisual, ...]
    bonuses: tuple[BonusVisual, ...]


@dataclass(frozen=True)
class _DetailRow:
    text: str
    style: str
    gap_before: int = 0


def character_visual_markdown(spec: CharacterVisual) -> str:
    """Render public GameKee assets directly inside one QQ Markdown message."""
    lines: list[str] = []
    if valid_image_url(spec.portrait_url):
        lines.append(f"![角色立绘 #232px #290px]({spec.portrait_url})")
    if spec.foods:
        lines.append("**喜欢的食物**")
        for item in spec.foods[:4]:
            if valid_image_url(item.image_url):
                lines.append(
                    f"![食物 #40px #40px]({item.image_url}) "
                    f"{item.group}：{item.name}"
                )
    if spec.bonuses:
        lines.append("**蜡笔板全体加成**")
        for bonus in spec.bonuses[:3]:
            icons = " ".join(
                f"![蜡笔 #36px #36px]({url})"
                for url in bonus.icons[:6]
                if valid_image_url(url)
            )
            if icons:
                lines.append(f"{bonus.level.replace('Level', 'Lv.')} {icons}")
    return "\n".join(lines)


def _plain_text(value, limit: int = 100) -> str:
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
            else:
                for key in ("data", "children"):
                    if key in node:
                        visit(node[key])

    visit(value)
    return " ".join("".join(parts).split())[:limit]


def _walk_type(value, wanted: str):
    if isinstance(value, list):
        for item in value:
            yield from _walk_type(item, wanted)
    elif isinstance(value, dict):
        if value.get("type") == wanted:
            yield value
        for child in value.values():
            yield from _walk_type(child, wanted)


def _image_urls(value) -> tuple[str, ...]:
    result: list[str] = []
    for node in _walk_type(value, "image"):
        values = []
        if isinstance(node.get("src"), str):
            values.append(node["src"])
        if isinstance(node.get("content"), list):
            values.extend(item for item in node["content"] if isinstance(item, str))
        for url in values:
            normalized = "https:" + url if url.startswith("//") else url
            parsed = urlsplit(normalized)
            if parsed.path.casefold().endswith(".gif") and not parsed.query:
                normalized += "?" + GAMEKEE_STATIC_GIF_QUERY
            if valid_image_url(normalized) and normalized not in result:
                result.append(normalized)
    return tuple(result)


def valid_image_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in ALLOWED_IMAGE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
            and parsed.path.startswith("/wiki2.0/images/")
            and (
                not parsed.query
                or (
                    parsed.path.casefold().endswith(".gif")
                    and parsed.query == GAMEKEE_STATIC_GIF_QUERY
                )
            )
            and not parsed.fragment
        )
    except ValueError:
        return False


def _upgrade(document, title: str) -> dict:
    for node in _walk_type(document, "upgrade-info"):
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        if _plain_text(data.get("title"), 60) == title:
            return data
    return {}


def extract_character_visual(document, name: str, region: str) -> CharacterVisual | None:
    portrait = ""
    for node in _walk_type(document, "image"):
        if _plain_text(node.get("title"), 40) == "使徒形象":
            urls = _image_urls(node)
            if urls:
                portrait = urls[0]
                break

    foods: list[FoodVisual] = []
    section = ""
    food_data = _upgrade(document, "食物喜好")
    for row in food_data.get("upgradeList") or []:
        if not isinstance(row, dict):
            continue
        title = _plain_text(row.get("title"), 40)
        marker = next((item for item in ("超喜欢", "喜欢", "讨厌") if item in title), "")
        if marker:
            section = marker
            continue
        if section not in {"超喜欢", "喜欢"}:
            continue
        food_name = _plain_text(row.get("content"), 40)
        urls = _image_urls(row.get("title"))
        if food_name and food_name not in {"0", "?"} and urls:
            foods.append(FoodVisual(section, food_name, urls[0]))
        if len(foods) == 4:
            break

    bonuses: list[BonusVisual] = []
    bonus_data = _upgrade(document, "蜡笔板全体加成")
    for row in bonus_data.get("upgradeList") or []:
        if not isinstance(row, dict):
            continue
        level = _plain_text(row.get("title"), 30)
        # GameKee renders the visible Level 1/2/3 crayon-board combinations
        # from `content`. `other` belongs to the separately collapsed
        # "查看升级效果" area and must not be presented as the board itself.
        icons = _image_urls(row.get("content"))[:6]
        if level and icons:
            bonuses.append(BonusVisual(level, icons))
        if len(bonuses) == 3:
            break

    if not portrait and not foods and not bonuses:
        return None
    return CharacterVisual(name[:40], region, portrait, tuple(foods), tuple(bonuses))


async def _download_images(urls: tuple[str, ...], transport=None) -> dict[str, bytes]:
    safe_urls = tuple(dict.fromkeys(url for url in urls if valid_image_url(url)))[:18]
    if not safe_urls:
        return {}
    options = dict(timeout=httpx.Timeout(15, connect=8), follow_redirects=False, trust_env=False)
    if transport is not None:
        options["transport"] = transport
    semaphore = asyncio.Semaphore(4)

    async with http_clients.client("trickcal-images", **options) as client:
        async def fetch(url: str):
            try:
                async with semaphore, client.stream("GET", url, headers={
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif",
                    "User-Agent": "NoneBot-QQ-Trickcal/1.2",
                    "Referer": "https://www.gamekee.com/tr/",
                }) as response:
                    if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
                        return url, b""
                    body = bytearray()
                    async for chunk in response.aiter_bytes(65536):
                        body.extend(chunk)
                        if len(body) > MAX_SOURCE_BYTES:
                            return url, b""
                    return url, bytes(body)
            except httpx.TransportError:
                return url, b""

        rows = await asyncio.gather(*(fetch(url) for url in safe_urls))
    return {url: body for url, body in rows if body}


def _open_image(data: bytes) -> Image.Image | None:
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                return None
            source.seek(0)  # Animated portraits use a compact first-frame preview.
            source.load()
            return source.convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        return None


def _fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    result = image.copy()
    result.thumbnail(size, Image.Resampling.LANCZOS)
    return result


def _paste_center(canvas: Image.Image, image: Image.Image, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    fitted = _fit(image, (x2 - x1, y2 - y1))
    x = x1 + (x2 - x1 - fitted.width) // 2
    y = y1 + (y2 - y1 - fitted.height) // 2
    canvas.paste(fitted, (x, y), fitted)


def _wrap_drawn_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    text_font,
    max_width: int,
    *,
    max_lines: int = 52,
) -> list[str]:
    """Wrap mixed Chinese/Latin text by rendered width while preserving paragraphs."""
    lines: list[str] = []
    for paragraph in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        paragraph = paragraph.rstrip()
        if not paragraph:
            if lines and lines[-1]:
                lines.append("")
            continue
        current = ""
        for character in paragraph:
            candidate = current + character
            if current and draw.textlength(candidate, font=text_font) > max_width:
                lines.append(current)
                current = character
                if len(lines) >= max_lines:
                    break
            else:
                current = candidate
        if len(lines) >= max_lines:
            break
        if current:
            lines.append(current)
        if len(lines) >= max_lines:
            break
    if len(lines) >= max_lines:
        lines = lines[:max_lines]
        tail = lines[-1].rstrip("…")
        lines[-1] = (tail[:-1] if tail else "") + "…"
    return lines


def _long_card_detail_rows(
    draw: ImageDraw.ImageDraw,
    details: str,
    fonts: dict[str, object],
) -> list[_DetailRow]:
    """Turn the plain-text reply into visually distinct long-card paragraphs."""
    rows: list[_DetailRow] = []
    first_content = True
    in_skills = False
    skill_count = 0
    pending_gap = 0

    def add_wrapped(text: str, style: str, width: int, gap: int = 0) -> None:
        wrapped = _wrap_drawn_text(
            draw, text, fonts[style], width, max_lines=24,
        )
        for index, line in enumerate(wrapped):
            rows.append(_DetailRow(line, style, gap if index == 0 else 0))

    normalized = str(details)
    for marker, replacement in (
        ("🍞 ", ""), ("ℹ️ ", ""), ("⚠️ ", "注意："), ("💡 ", "提示："),
    ):
        normalized = normalized.replace(marker, replacement)

    for raw_line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        skill_continuation = in_skills and skill_count > 0 and raw_line.startswith(("  ", "\t"))
        line = raw_line.strip()
        if not line:
            pending_gap = max(pending_gap, 12 if in_skills else 8)
            continue
        if all(character in "─━—-_ " for character in line):
            continue
        if first_content:
            add_wrapped(line, "title", 648)
            first_content = False
            pending_gap = 18
            continue
        if line == "技能摘要":
            add_wrapped(line, "section", 648, max(pending_gap, 22))
            in_skills = True
            pending_gap = 8
            continue
        if in_skills and line.startswith(("• ", "· ", "● ")):
            content = line[2:].strip()
            name, separator, description = content.partition("：")
            if not separator:
                name, separator, description = content.partition(":")
            if separator and name.strip():
                add_wrapped(
                    f"● {name.strip()}", "skill", 628,
                    max(pending_gap, 15 if skill_count else 8),
                )
                if description.strip():
                    add_wrapped(description.strip(), "skill_body", 624, 6)
                skill_count += 1
                pending_gap = 0
                continue
        if skill_continuation:
            add_wrapped(line, "skill_body", 624, max(pending_gap, 5))
            pending_gap = 0
            continue
        if line.startswith(("http://", "https://")):
            add_wrapped(line, "link", 648, max(pending_gap, 4))
        elif line.startswith("玩家维护资料"):
            add_wrapped(line, "footer", 648, max(pending_gap, 22))
        elif line.startswith("数据区服："):
            add_wrapped(line, "meta", 648, max(pending_gap, 4))
        else:
            add_wrapped(line, "body", 648, pending_gap)
        pending_gap = 0
    return rows


def render_long_character_card(visual_card: bytes, details: str) -> bytes | None:
    """Merge the visual summary and full role text into one vertical JPEG card."""
    try:
        with Image.open(io.BytesIO(visual_card)) as source:
            source.load()
            if source.size != (720, 540):
                return None
            visual = source.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    with RENDER_LOCK:
        fonts = {
            "title": font(24),
            "section": font(22),
            "skill": font(20),
            "skill_body": font(20),
            "meta": font(20),
            "body": font(20),
            "footer": font(15),
            "link": font(16),
        }
        small_font = font(14)
        measure = Image.new("RGB", (1, 1), "white")
        measure_draw = ImageDraw.Draw(measure)
        rows = _long_card_detail_rows(measure_draw, details, fonts)
        line_heights = {
            "title": 38,
            "section": 38,
            "skill": 33,
            "skill_body": 33,
            "meta": 34,
            "body": 34,
            "footer": 26,
            "link": 28,
        }
        rows_height = sum(line_heights[row.style] + row.gap_before for row in rows)
        details_height = max(150, rows_height + 82)
        height = min(2600, 540 + details_height)
        canvas = Image.new("RGB", (720, height), "#f5f7fb")
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(
            (12, 12, 708, height - 12), 22,
            fill="white", outline="#dce5f2", width=2,
        )
        # Reuse the seven-day visual card but keep one continuous outer panel.
        canvas.paste(visual.crop((14, 14, 706, 528)), (14, 14))
        draw.line((32, 540, 688, 540), fill="#e6ebf2", width=1)
        y = 560
        for row in rows:
            line_height = line_heights[row.style]
            y += row.gap_before
            if y + line_height > height - 42:
                break
            if row.style == "skill":
                draw.rounded_rectangle(
                    (36, y + 5, 41, y + line_height - 6), 3, fill="#4f78d1",
                )
                draw.text(
                    (51, y), row.text, font=fonts[row.style], fill="#3f69c5",
                    stroke_width=1, stroke_fill="#3f69c5",
                )
            elif row.style == "skill_body":
                draw.text((52, y), row.text, font=fonts[row.style], fill="#34445d")
            elif row.style in {"title", "section"}:
                color = "#24344d" if row.style == "title" else "#315f9f"
                draw.text(
                    (36, y), row.text, font=fonts[row.style], fill=color,
                    stroke_width=1, stroke_fill=color,
                )
            else:
                color = {
                    "meta": "#315f9f",
                    "footer": "#7d899a",
                    "link": "#3878b8",
                }.get(row.style, "#273a56")
                draw.text((36, y), row.text, font=fonts[row.style], fill=color)
            y += line_height
        draw.text(
            (36, height - 34), "点击消息下方按钮可继续随机角色",
            font=small_font, fill="#8a98aa",
        )
        output = io.BytesIO()
        canvas.save(output, "JPEG", quality=86, optimize=True, progressive=True)
        return output.getvalue()


def jpeg_dimensions(image: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(image)) as source:
            return source.size if source.format == "JPEG" else None
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def _render(spec: CharacterVisual, images: dict[str, bytes]) -> bytes | None:
    decoded = {url: image for url, body in images.items() if (image := _open_image(body)) is not None}
    if not decoded:
        return None
    with RENDER_LOCK:
        canvas = Image.new("RGB", (720, 540), "#f5f7fb")
        draw = ImageDraw.Draw(canvas)
        title_font, section_font, body_font, small_font = font(26), font(19), font(16), font(13)
        draw.rounded_rectangle((12, 12, 708, 528), 22, fill="white", outline="#dce5f2", width=2)
        draw.text((32, 27), f"{spec.name} · {spec.region}资料图", font=title_font, fill="#24344d")
        draw.text((598, 35), "GAMEKEE", font=small_font, fill="#8190a8")

        draw.rounded_rectangle((28, 72, 282, 490), 18, fill="#f2f6fc")
        portrait = decoded.get(spec.portrait_url)
        if portrait is not None:
            _paste_center(canvas, portrait, (42, 84, 268, 476))
        else:
            draw.text((76, 270), "角色图暂不可用", font=body_font, fill="#8997aa")

        right_x = 312
        draw.text((right_x, 82), "喜欢的食物", font=section_font, fill="#314868")
        if spec.foods:
            for index, item in enumerate(spec.foods[:4]):
                col, row = index % 2, index // 2
                x, y = right_x + col * 190, 116 + row * 82
                icon = decoded.get(item.image_url)
                if icon is not None:
                    _paste_center(canvas, icon, (x, y, x + 52, y + 52))
                badge = "超喜欢" if item.group == "超喜欢" else "喜欢"
                draw.text((x + 60, y + 2), badge, font=small_font,
                          fill="#e17878" if badge == "超喜欢" else "#7185a7")
                label = item.name if len(item.name) <= 8 else item.name[:7] + "…"
                draw.text((x + 60, y + 24), label, font=body_font, fill="#273a56")
        else:
            draw.text((right_x, 121), "资料页暂未填写", font=body_font, fill="#8997aa")

        bonus_y = 292
        draw.line((right_x, bonus_y - 16, 686, bonus_y - 16), fill="#e6ebf2", width=1)
        draw.text((right_x, bonus_y), "蜡笔板全体加成", font=section_font, fill="#314868")
        if spec.bonuses:
            for row_index, bonus in enumerate(spec.bonuses[:3]):
                y = bonus_y + 40 + row_index * 55
                draw.text((right_x, y + 8), bonus.level.replace("Level", "Lv."),
                          font=body_font, fill="#6a7b94")
                x = right_x + 68
                for url in bonus.icons[:6]:
                    icon = decoded.get(url)
                    if icon is not None:
                        _paste_center(canvas, icon, (x, y - 2, x + 43, y + 41))
                    x += 50
        else:
            draw.text((right_x, bonus_y + 42), "资料页暂未填写", font=body_font, fill="#8997aa")

        draw.text((312, 500), "玩家维护资料 · 图片已缩小，动图显示首帧", font=small_font, fill="#8a98aa")
        output = io.BytesIO()
        canvas.save(output, "JPEG", quality=84, optimize=True, progressive=True)
        return output.getvalue()


async def render_character_visual(spec: CharacterVisual, transport=None) -> bytes | None:
    urls = [spec.portrait_url]
    urls.extend(item.image_url for item in spec.foods)
    urls.extend(url for bonus in spec.bonuses for url in bonus.icons)
    images = await _download_images(tuple(urls), transport)
    return await asyncio.to_thread(_render, spec, images)
