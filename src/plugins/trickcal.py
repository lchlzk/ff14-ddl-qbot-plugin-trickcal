"""Trickcal community Wiki queries and per-user Soshage board tracking."""
from __future__ import annotations

import asyncio
import time

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.qq import Bot, Message, MessageSegment
from nonebot.adapters.qq.event import C2CMessageCreateEvent, GroupMessageCreateEvent, MessageEvent
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from bot_tools import (
    community,
    qq_buttons,
    qq_image_upload,
    trickcal as service,
    trickcal_board,
    trickcal_media,
)
from bot_tools.storage import ToolError
from message_ui import error_panel, public_error_message
from bot_tools.plugin_runtime import bounded, command_eligible, get_store, identity


tr = on_command("tr", aliases={"trickcal"}, force_whitespace=True,
                rule=command_eligible, priority=10, block=True)
PENDING_IMPORTS: dict[tuple[str, str, str], float] = {}


def _visible_trickcal_error(error: ToolError) -> str:
    detail = str(error).strip()
    visible = public_error_message(
        error,
        "嘟嘟脸服务暂时不可用，请稍后重试或联系管理员。",
    )
    if visible != detail:
        logger.error("Suppressed internal Trickcal error: {}", detail)
    return visible


def _remember_pending(who, expires: float | None = None) -> None:
    PENDING_IMPORTS[trickcal_board.import_session_key(who)] = (
        time.time() + trickcal_board.IMPORT_SESSION_TTL if expires is None else expires
    )


def _forget_pending(who) -> None:
    PENDING_IMPORTS.pop(trickcal_board.import_session_key(who), None)


@get_driver().on_startup
async def restore_pending_board_imports() -> None:
    try:
        rows = await asyncio.to_thread(trickcal_board.BoardStore(get_store()).active_imports)
    except Exception as exc:
        logger.warning("Unable to restore Trickcal board upload waits ({})", type(exc).__name__)
        return
    PENDING_IMPORTS.clear()
    for owner, bot_id, scope, expires in rows:
        PENDING_IMPORTS[(owner, bot_id, scope)] = expires


def board_upload_event(bot: Bot, event: MessageEvent) -> bool:
    if not command_eligible(event):
        return False
    key = trickcal_board.import_session_key(identity(bot, event))
    expires = PENDING_IMPORTS.get(key, 0)
    if expires <= time.time():
        PENDING_IMPORTS.pop(key, None)
        return False
    return True


# A separate QQ file message has no slash-command text. Observe it before the
# optional chat responders and only consume it when this sender has an active
# board-import session in this exact conversation.
board_upload = on_message(rule=board_upload_event, priority=9, block=False)


@board_upload.handle()
async def handle_board_upload(bot: Bot, event: MessageEvent, matcher: Matcher) -> None:
    try:
        store, who = get_store(), identity(bot, event)
        board_store = trickcal_board.BoardStore(store)
        pending = await asyncio.to_thread(board_store.pending_import, who)
    except Exception as exc:
        logger.warning("Trickcal board pending-upload lookup failed ({})", type(exc).__name__)
        return
    if not pending:
        _forget_pending(who)
        return
    matcher.stop_propagation()
    attachments = getattr(event, "attachments", None) or []
    if len(attachments) != 1 or not trickcal_board.is_export_attachment(attachments[0]):
        await asyncio.to_thread(board_store.cancel_import, who)
        _forget_pending(who)
        reply = error_panel(
            "蜡笔板导入已取消：等待期间你的下一条消息不是一个 .json 文件。",
            hint="请重新发送 /tr 蜡笔板 导入，然后在 10 分钟内直接上传文件。",
        )
        await bot.send(event, MessageSegment.text(bounded(reply)))
        return
    try:
        await asyncio.to_thread(community.gate, store, who, "tr")
        reply = await trickcal_board.accept_pending_upload(
            store, who, attachments
        )
        if reply is None:
            _forget_pending(who)
            return
        _forget_pending(who)
    except ToolError as exc:
        reply = error_panel(_visible_trickcal_error(exc), hint="可在 10 分钟内重新上传 .json；取消请发送 /tr 蜡笔板 导入 取消。")
    except Exception as exc:
        logger.error("Trickcal board upload failed ({})", type(exc).__name__)
        reply = error_panel("蜡笔板文件暂时未能导入，请在 10 分钟内重新上传。")
    await bot.send(event, MessageSegment.text(bounded(reply)))


@tr.handle()
async def handle_tr(bot: Bot, event: MessageEvent, args: Message = CommandArg()) -> None:
    raw = args.extract_plain_text().strip()
    character_card = None
    character_reply = False
    keyboard = None
    board_command = trickcal_board.is_board_command(raw)
    if not board_command and len(raw) > 100:
        await tr.finish(error_panel("查询内容过长，请控制在 100 字以内。"))
    if board_command and len(raw.encode("utf-8")) > trickcal_board.MAX_IMPORT_BYTES + 100:
        await tr.finish(error_panel("蜡笔板导入内容不能超过 512 KiB。"))
    try:
        store, who = get_store(), identity(bot, event)
        if board_command:
            action, _ = trickcal_board.parse_command(raw)
            if action != "help":
                await asyncio.to_thread(store.throttle, who.actor)
                await asyncio.to_thread(community.gate, store, who, "tr", True)
            reply = await trickcal_board.dispatch(
                store, who, raw, attachments=getattr(event, "attachments", None) or []
            )
            if isinstance(reply, trickcal_board.PaginatedReply):
                keyboard = qq_buttons.pagination_keyboard(
                    reply.previous_command, reply.next_command, event.get_user_id(),
                )
            elif isinstance(reply, trickcal_board.ActionReply):
                keyboard = qq_buttons.command_keyboard(
                    [(item.label, item.command, item.style) for item in reply.actions],
                    event.get_user_id(),
                )
            if action == "import":
                _, import_value = trickcal_board.parse_command(raw)
                if not import_value and not (getattr(event, "attachments", None) or []):
                    _remember_pending(who)
                elif import_value.casefold() in {"取消", "cancel"}:
                    _forget_pending(who)
        else:
            action, _ = service.parse_command(raw)
            if action != "帮助":
                await asyncio.to_thread(store.throttle, who.actor)
                await asyncio.to_thread(community.gate, store, who, "tr", True)
            reply = await service.dispatch(raw)
        if not board_command and action in {"角色", "随机角色"}:
            character_reply = True
            try:
                character_card = await service.character_card(raw, result=reply)
            except Exception as exc:
                # The text result and button remain usable if rendering is unavailable.
                logger.warning("Trickcal long character card unavailable ({})", type(exc).__name__)
            if action == "随机角色":
                region = service.parse_command(raw)[1]
            else:
                _, query = service.parse_command(raw)
                _, region = service._split_role_region(query)
            keyboard = qq_buttons.command_keyboard(
                [("🎲 随机角色", f"/tr 随机角色 {region}", 1)],
                event.get_user_id(),
                columns=1,
            )
    except (service.TrickcalError, ToolError) as exc:
        reply = error_panel(_visible_trickcal_error(exc), hint="发送 /tr 查看可用查询。")
    except Exception as exc:
        logger.error("Trickcal query failed ({})", type(exc).__name__)
        reply = error_panel("嘟嘟脸查询暂时未完成，请稍后重试。")
    rich_reply = str(bounded(reply))
    if character_reply and keyboard is not None:
        if character_card is not None:
            try:
                if isinstance(event, GroupMessageCreateEvent):
                    target = qq_image_upload.UploadTarget("group", event.group_openid)
                elif isinstance(event, C2CMessageCreateEvent):
                    target = qq_image_upload.UploadTarget("c2c", event.get_user_id())
                else:
                    target = None
                dimensions = trickcal_media.jpeg_dimensions(character_card)
                if target is None or dimensions is None:
                    raise RuntimeError("unsupported QQ role-card target")
                image_url = await qq_image_upload.public_image_url(
                    bot, target, character_card,
                )
                width, height = dimensions
                card_markdown = (
                    f"![嘟嘟脸角色长卡 #{width}px #{height}px]({image_url})"
                )
                await bot.send(
                    event,
                    MessageSegment.markdown(card_markdown)
                    + MessageSegment.keyboard(keyboard),
                )
                return
            except Exception as exc:
                logger.warning(
                    "Trickcal QQ-hosted long card unavailable ({}: {})",
                    type(exc).__name__, str(exc)[:300],
                )
        # Never split a role result from its button. If QQ's temporary image
        # hosting is unavailable, retain one atomic text+button message.
        try:
            await bot.send(
                event,
                MessageSegment.markdown(rich_reply) + MessageSegment.keyboard(keyboard),
            )
        except Exception as exc:
            logger.warning(
                "Trickcal one-message role fallback unavailable ({}: {})",
                type(exc).__name__, str(exc)[:300],
            )
            await bot.send(event, MessageSegment.text(rich_reply))
        return
    if keyboard is not None:
        # A real markdown segment may be combined with the keyboard and gives
        # the best UI when this bot has native-markdown permission.
        try:
            await bot.send(
                event,
                MessageSegment.markdown(rich_reply) + MessageSegment.keyboard(keyboard),
            )
            return
        except Exception as exc:
            logger.warning(
                "Trickcal markdown pagination unavailable ({}: {})",
                type(exc).__name__, str(exc)[:300],
            )
        # Personal/development bots commonly lack native markdown. Send the
        # normal text first, then try a keyboard-only follow-up which does not
        # require a markdown template.
        await bot.send(event, MessageSegment.text(rich_reply))
        try:
            await bot.send(event, MessageSegment.keyboard(keyboard))
        except Exception as exc:
            logger.warning(
                "Trickcal keyboard-only pagination unavailable ({}: {})",
                type(exc).__name__, str(exc)[:300],
            )
        return
    await tr.finish(bounded(reply))
