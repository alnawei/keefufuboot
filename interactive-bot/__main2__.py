import os
import random
import time
from datetime import datetime, timedelta
from string import ascii_letters as letters

import httpx
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.helpers import mention_html

from db.database import SessionMaker, engine
from db.model import Base, FormnStatus, MediaGroupMesssage, MessageMap, User

from . import (
    admin_group_id,
    admin_user_ids,
    app_name,
    bot_token,
    is_delete_topic_as_ban_forever,
    is_delete_user_messages,
    logger,
    welcome_message,
    disable_captcha,
    message_interval,
)
from .utils import delete_message_later

# 创建表（使用的sqlite，是无法轻易alter表的。如果改动，需要删除重建。无法merge）
Base.metadata.create_all(bind=engine)
db = SessionMaker()


# 延时发送媒体组消息的回调
async def _send_media_group_later(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    media_group_id = job.data
    _, from_chat_id, target_id, dir = job.name.split("_")

    # 数据库内查找对应的媒体组消息。
    media_group_msgs = (
        db.query(MediaGroupMesssage)
        .filter(
            MediaGroupMesssage.media_group_id == media_group_id,
            MediaGroupMesssage.chat_id == from_chat_id,
        )
        .all()
    )
    chat = await context.bot.get_chat(target_id)
    if dir == "u2a":
        # 发送给群组
        u = db.query(User).filter(User.user_id == from_chat_id).first()
        message_thread_id = u.message_thread_id
        sents = await chat.send_copies(
            from_chat_id,
            [m.message_id for m in media_group_msgs],
            message_thread_id=message_thread_id,
        )
        for sent, msg in zip(sents, media_group_msgs):
            msg_map = MessageMap(
                user_chat_message_id=msg.message_id,
                group_chat_message_id=sent.message_id,
                user_id=u.user_id,
            )
            db.add(msg_map)
            db.commit()
    else:
        # 发送给用户
        sents = await chat.send_copies(
            from_chat_id, [m.message_id for m in media_group_msgs]
        )
        for sent, msg in zip(sents, media_group_msgs):
            msg_map = MessageMap(
                user_chat_message_id=sent.message_id,
                group_chat_message_id=msg.message_id,
                user_id=target_id,
            )
            db.add(msg_map)
            db.commit()


# 延时发送媒体组消息
async def send_media_group_later(
    delay: float,
    chat_id,
    target_id,
    media_group_id: int,
    dir,
    context: ContextTypes.DEFAULT_TYPE,
):
    name = f"sendmediagroup_{chat_id}_{target_id}_{dir}"
    context.job_queue.run_once(
        _send_media_group_later, delay, chat_id=chat_id, name=name, data=media_group_id
    )
    return name


def update_user_db(user: telegram.User):
    if db.query(User).filter(User.user_id == user.id).first():
        return
    u = User(
        user_id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
    )
    db.add(u)
    db.commit()


async def send_contact_card(
    chat_id, message_thread_id, user: User, update: Update, context: ContextTypes
):
    buttons = []
    buttons.append(
        [
            InlineKeyboardButton(
                f"{'🏆 高级会员' if user.is_premium else '✈️ 普通会员' }",
                url=f"https://github.com/",
            )
        ]
    )
    if user.username:
        buttons.append(
            [InlineKeyboardButton("👤 直接联络", url=f"https://t.me/{user.username}")]
        )

    user_photo = await context.bot.get_user_profile_photos(user.id)

    if user_photo.total_count:
        pic = user_photo.photos[0][-1].file_id
        await context.bot.send_photo(
            chat_id,
            photo=pic,
            caption=f"👤 {mention_html(user.id, user.first_name)}\n\n📱 {user.id}\n\n🔗 @{user.username if user.username else '无'}",
            message_thread_id=message_thread_id,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )
    else:
        await context.bot.send_contact(
            chat_id,
            phone_number="11111",
            first_name=user.first_name,
            last_name=user.last_name,
            message_thread_id=message_thread_id,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    update_user_db(user)
    # check whether is admin
    if user.id in admin_user_ids:
        logger.info(f"{user.first_name}({user.id}) is admin")
        try:
            bg = await context.bot.get_chat(admin_group_id)
            if bg.type == "supergroup" or bg.type == "group":
                logger.info(f"admin group is {bg.title}")
        except Exception as e:
            logger.error(f"admin group error {e}")
            await update.message.reply_html(
                f"⚠️⚠️后台管理群组设置错误，请检查配置。⚠️⚠️\n你需要确保已经将机器人 @{context.bot.username} 邀请入管理群组并且给与了管理员权限。\n错误细节：{e}\n请联系 @QS00008 获取技术支持。"
            )
            return ConversationHandler.END
        await update.message.reply_html(
            f"你好管理员 {user.first_name}({user.id})\n\n欢迎使用 {app_name} 机器人。\n\n 目前你的配置完全正确。可以在群组 <b> {bg.title} </b> 中使用机器人。"
        )
    else:
        # 1. 定义底部菜单按钮
            keyboard = [
                [KeyboardButton("🌐 官网地址"), KeyboardButton("✈️ Telegram专用代理")],
                [KeyboardButton("✨ 代开飞机会员"), KeyboardButton("🆙 飞机账号")],
                [KeyboardButton("🌍 全球VPN定制"), KeyboardButton("🔰 实名人脸")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            
            # 2. 发送带按钮的欢迎语
            await update.message.reply_html(
                f"{mention_html(user.id, user.full_name)} 同学: \n\n{welcome_message}",
                reply_markup=reply_markup
        )


async def check_human(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.user_data.get("is_human", False) == False:
        if context.user_data.get("is_human_error_time", 0) > time.time() - 120:
            # 2分钟内禁言
            await update.message.reply_html("你已经被禁言,请稍后再尝试。")
            return False
        
        file_name = random.choice(os.listdir("./assets/imgs"))
        code = file_name.replace("image_", "").replace(".png", "")
        file = f"./assets/imgs/{file_name}"
        codes = ["".join(random.sample(letters, 5)) for _ in range(0, 7)]
        codes.append(code)
        random.shuffle(codes)

        # 构建按钮及文字
        buttons = [
            InlineKeyboardButton(x, callback_data=f"vcode_{x}_{user.id}") for x in codes
        ]
        button_matrix = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
        caption = f"{mention_html(user.id, user.first_name)}请选择图片中的文字。回答错误将无法联系客服。"

        # 获取缓存的 file_id
        photo_cached = context.bot_data.get(f"image|{code}")
        
        try:
            if not photo_cached:
                raise BadRequest("No cached photo") # 没有缓存时故意抛出异常，进入重新上传的逻辑
            
            # 1. 尝试使用缓存的 file_id 发送
            sent = await update.message.reply_photo(
                photo=photo_cached,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(button_matrix),
                parse_mode="HTML",
            )
        except BadRequest:
            # 2. 如果 file_id 失效，或者初次读取，会进入此代码块，直接读取本地文件上传
            with open(file, "rb") as local_photo:
                sent = await update.message.reply_photo(
                    photo=local_photo,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(button_matrix),
                    parse_mode="HTML",
                )
            # 3. 存下最新有效的 file_id 覆盖掉旧数据
            biggest_photo = sorted(sent.photo, key=lambda x: x.file_size, reverse=True)[0]
            context.bot_data[f"image|{code}"] = biggest_photo.file_id

        context.user_data["vcode"] = code
        await delete_message_later(60, sent.chat.id, sent.message_id, context)
        return False
    return True


async def callback_query_vcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    code = query.data.split("_")[1]
    user_id = query.data.split("_")[2]
    if user_id == str(user.id):
        # 是正确的人点击
        if code == context.user_data.get("vcode"):
            # 点击合法
            await query.answer(f"正确，欢迎。")
            sent = await context.bot.send_message(
                update.effective_chat.id,
                f"{mention_html(user.id, user.first_name)} , 👋 欢迎咨询！为了更高效地解决您的问题：\n━━━━━━━━━━━━━━\n⚠️ <b>【售后必读】</b>\n请直接发送 <b>订单号</b> 和 <b>订单截图</b>\n<i>(很多问题在商品描述、官网公告中已有详细说明)</i>\n━━━━━━━━━━━━━━",
                parse_mode="HTML",
            )
            context.user_data["is_human"] = True
        else:
            await query.answer(f"~错误~，禁言2分钟")
            context.user_data["is_human_error_time"] = time.time()
    await query.message.delete()

# 处理所有悬挂按钮（Inline Keyboard）点击的调度中心
async def inline_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # 必须执行，否则用户点击后按钮会一直转圈
    
    data = query.data  # 获取点击的按钮暗号

    # 1. 转发给原有的验证码逻辑 (保持原有功能)
    if data.startswith("vcode_"):
        return await callback_query_vcode(update, context)

    # 2. 处理“网站打不开”点击
    elif data == "site_help":
        await query.message.reply_html(
            "<b>🚫 如遇到打不开网站的情况：</b>\n\n"
            "1️⃣ <b>使用谷歌浏览器</b>：兼容性最好。\n"
            "2️⃣ <b>更换网络</b>：关闭 WiFi，切换为手机流量试试。\n"
            "3️⃣ <b>更换高质量代理</b>：您的 VPN 线路可能已被滥用。\n"
            "4️⃣ <b>推荐方案</b>：购买好用的节点 <code>10U/月/100G流量</code>。\n"
            "5️⃣ <b>USDT支付</b>：确定商品后，统一选择 <b>USDT</b> 付款。"
        )

    # 3. 处理“查询订单”点击
    elif data == "order_help":
        await query.message.reply_html(
            "<b>🔍 如何查询订单：</b>\n\n"
            "进入平台官网页面 —— 找到<b>右上角</b>按钮 —— 输入购买时留的<b>联系方式</b>即可查询。"
        )
        # 逻辑：代理个人版
    elif data == "proxy_personal":
        await query.message.reply_html(
            "<b>✈️ 飞机代理个人版（Socks5）</b>\n\n"
            "<b>【月付】</b>\n"
            "20G     5U\n"
            "40G    10U\n"
            "60G    15U\n\n"
            "<b>【季付】</b>\n"
            "50G    15U\n"
            "100G   30U\n"
            "300G   45U\n\n"
            "<b>【年付】</b>\n"
            "500G   70U\n"
            "700G  100U\n"
            "1000G 130U\n\n"
            "<b>⚠️ 友情提醒：</b>\n"
            "代理含有异地风控系统，共享泛滥会导致冻结，所以<b>只能一个人使用</b>，一旦冻结不退不换！\n\n"
            "<b>📖 个人版代理使用教程：</b>\n"
            "1、在收藏夹点击链接，进入通道选择页面；\n"
            "<i>（如果进不去，需借助加速器节点）</i>\n"
            "2、进入页面后，关闭第三方加速器节点。点击绿色可连接通道，自动跳转，点击“连接代理”。\n"
            "3、线路不可用的情况，先删掉不可用的代理、重新去启用面板启用就行。另可能是设备连接了VPN，线路冲突，显示代理不可用，建议关闭VPN，重新启用代理。\n"
            "4、如出现网页点击启用不跳转，原因是网页未识别到设备上的电报软件，建议卸载浏览器和电报APP，重新安装尝试。"
        )

    # 逻辑：代理团队版
    elif data == "proxy_team":
        await query.message.reply_html(
            "<b>💠 MTProto 独享代理方案</b>\n\n"
            "<b>【资费标准】</b>\n"
            "◈ 独享代理：<code>80U / 月</code>\n" 
            "✅ 家庭  ✅ 团队  ✅ 公司\n\n"
            "◈ 广告推广插件：<code>+10U / 月</code>\n\n"
            "<b>【可选地区】</b>\n"
            "📍 新加坡、香港、日本、韩国\n"
            "📍 美国、加拿大、德国、印度等\n\n"
            "—— —— —— —— —— ——\n\n"
            "<b>❓ 什么是代理赞助商（Proxy Sponsor）推广？</b>\n\n"
            "<b>✅ 核心原理：</b>\n"
            "只要用户连接了您的代理，您的频道就会<b>强制置顶显示</b>在用户的会话列表最上方。\n\n"
            "<b>✅ 强制曝光：</b>\n"
            "只有当用户点击并关注了您的频道，该置顶才会消失。这是目前电报生态内<b>转化率最高、粉丝增长最快</b>的引流方式。\n\n"
            "<b>✅ 流量价值：</b>\n"
            "每日千人级别真实活跃用户连接，持续为您的频道注入精准流量，极大提升品牌/业务曝光度。\n\n"
            "📢 <i>需要开通请联系人工客服。</i>"
        )


async def forwarding_message_u2a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not disable_captcha:
        if not await check_human(update, context):
            return
    if message_interval:
        if context.user_data.get("last_message_time", 0) > time.time() - message_interval:
            await update.message.reply_html("请不要频繁发送消息。")
            return
        context.user_data["last_message_time"] = time.time()
    user = update.effective_user
    update_user_db(user)
    chat_id = admin_group_id
    attachment = update.message.effective_attachment
    # await update.message.forward(chat_id)
    u = db.query(User).filter(User.user_id == user.id).first()
    message_thread_id = u.message_thread_id
    if (
        f := db.query(FormnStatus)
        .filter(FormnStatus.message_thread_id == message_thread_id)
        .first()
    ):
        if f.status == "closed":
            await update.message.reply_html(
                "客服已经关闭对话。如需联系，请利用其他途径联络客服回复和你的对话。"
            )
            return
    if not message_thread_id:
        formn = await context.bot.create_forum_topic(
            chat_id,
            name=f"工单{random.randint(10000,99999)}|{user.full_name}|{user.id}",
        )
        message_thread_id = formn.message_thread_id
        u.message_thread_id = message_thread_id
        await context.bot.send_message(
            chat_id,
            f"新的用户 {mention_html(user.id, user.full_name)} 开始了一个新的会话。",
            message_thread_id=message_thread_id,
            parse_mode="HTML",
        )
        await send_contact_card(chat_id, message_thread_id, user, update, context)
        db.add(u)
        db.commit()

    # 构筑下发送参数
    params = {"message_thread_id": message_thread_id}
    if update.message.reply_to_message:
        # 用户引用了一条消息。我们需要找到这条消息在群组中的id
        reply_in_user_chat = update.message.reply_to_message.message_id
        if (
            msg_map := db.query(MessageMap)
            .filter(MessageMap.user_chat_message_id == reply_in_user_chat)
            .first()
        ):
            params["reply_to_message_id"] = msg_map.group_chat_message_id
    try:
        if update.message.media_group_id:
            msg = MediaGroupMesssage(
                chat_id=update.message.chat.id,
                message_id=update.message.message_id,
                media_group_id=update.message.media_group_id,
                is_header=False,
                caption_html=update.message.caption_html,
            )
            db.add(msg)
            db.commit()
            if update.message.media_group_id != context.user_data.get(
                "current_media_group_id", 0
            ):
                context.user_data["current_media_group_id"] = (
                    update.message.media_group_id
                )
                await send_media_group_later(
                    5, user.id, chat_id, update.message.media_group_id, "u2a", context
                )
            return
        else:
            chat = await context.bot.get_chat(chat_id)
            sent_msg = await chat.send_copy(
                update.effective_chat.id, update.message.id, **params
            )

        msg_map = MessageMap(
            user_chat_message_id=update.message.id,
            group_chat_message_id=sent_msg.message_id,
            user_id=user.id,
        )
        db.add(msg_map)
        db.commit()

    except BadRequest as e:
        if is_delete_topic_as_ban_forever:
            await update.message.reply_html(
                f"发送失败，你的对话已经被客服删除。请联系客服重新打开对话。"
            )
        else:
            u.message_thread_id = 0
            db.add(u)
            db.commit()
            await update.message.reply_html(
                f"发送失败，你的对话已经被客服删除。请再发送一条消息用来激活对话。"
            )
    except Exception as e:
        await update.message.reply_html(
            f"发送失败: {e}\n请联系 @QS00008 汇报这个错误。谢谢"
        )


async def forwarding_message_a2u(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_db(update.effective_user)
    message_thread_id = update.message.message_thread_id
    if not message_thread_id:
        # general message, ignore
        return
    user_id = 0
    if u := db.query(User).filter(User.message_thread_id == message_thread_id).first():
        user_id = u.user_id
    if not user_id:
        logger.debug(update.message)
        return
    if update.message.forum_topic_created:
        f = FormnStatus(
            message_thread_id=update.message.message_thread_id, status="opened"
        )
        db.add(f)
        db.commit()
        return
    if update.message.forum_topic_closed:
        await context.bot.send_message(
            user_id, "对话已经结束。对方已经关闭了对话。你的留言将被忽略。"
        )
        if (
            f := db.query(FormnStatus)
            .filter(FormnStatus.message_thread_id == update.message.message_thread_id)
            .first()
        ):
            f.status = "closed"
            db.add(f)
            db.commit()
        return
    if update.message.forum_topic_reopened:
        await context.bot.send_message(user_id, "对方重新打开了对话。可以继续对话了。")
        if (
            f := db.query(FormnStatus)
            .filter(FormnStatus.message_thread_id == update.message.message_thread_id)
            .first()
        ):
            f.status = "opened"
            db.add(f)
            db.commit()
        return
    if (
        f := db.query(FormnStatus)
        .filter(FormnStatus.message_thread_id == message_thread_id)
        .first()
    ):
        if f.status == "closed":
            await update.message.reply_html(
                "对话已经结束。希望和对方联系，需要打开对话。"
            )
            return
    chat_id = user_id
    # 构筑下发送参数
    params = {}
    if update.message.reply_to_message:
        # 群组中，客服回复了一条消息。我们需要找到这条消息在用户中的id
        reply_in_admin = update.message.reply_to_message.message_id
        if (
            msg_map := db.query(MessageMap)
            .filter(MessageMap.group_chat_message_id == reply_in_admin)
            .first()
        ):
            params["reply_to_message_id"] = msg_map.user_chat_message_id
    try:
        if update.message.media_group_id:
            msg = MediaGroupMesssage(
                chat_id=update.message.chat.id,
                message_id=update.message.message_id,
                media_group_id=update.message.media_group_id,
                is_header=False,
                caption_html=update.message.caption_html,
            )
            db.add(msg)
            db.commit()
            if update.message.media_group_id != context.application.user_data[
                user_id
            ].get("current_media_group_id", 0):
                context.application.user_data[user_id][
                    "current_media_group_id"
                ] = update.message.media_group_id
                await send_media_group_later(
                    5,
                    update.effective_chat.id,
                    user_id,
                    update.message.media_group_id,
                    "a2u",
                    context,
                )
            return
        else:
            chat = await context.bot.get_chat(chat_id)
            sent_msg = await chat.send_copy(
                update.effective_chat.id, update.message.id, **params
            )
        msg_map = MessageMap(
            group_chat_message_id=update.message.id,
            user_chat_message_id=sent_msg.message_id,
            user_id=user_id,
        )
        db.add(msg_map)
        db.commit()

    except Exception as e:
        await update.message.reply_html(
            f"发送失败: {e}\n请联系 @QS00008 汇报这个错误。谢谢"
        )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user.id in admin_user_ids:
        await update.message.reply_html("你没有权限执行此操作。")
        return
    await context.bot.delete_forum_topic(
        update.effective_chat.id, update.message.message_thread_id
    )
    if not is_delete_user_messages:
        return
    if (
        target_user := db.query(User)
        .filter(User.message_thread_id == update.message.message_thread_id)
        .first()
    ):
        all_messages_in_user_chat = (
            db.query(MessageMap).filter(MessageMap.user_id == target_user.user_id).all()
        )
        await context.bot.delete_messages(
            target_user.user_id,
            [msg.user_chat_message_id for msg in all_messages_in_user_chat],
        )


async def _broadcast(context: ContextTypes.DEFAULT_TYPE):
    users = db.query(User).all()
    msg_id, chat_id = context.job.data.split("_")
    success = 0
    failed = 0
    for u in users:
        try:
            chat = await context.bot.get_chat(u.user_id)
            await chat.send_copy(chat_id, msg_id)
            success += 1
        except Exception as e:
            failed += 1


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user.id in admin_user_ids:
        await update.message.reply_html("你没有权限执行此操作。")
        return

    if not update.message.reply_to_message:
        await update.message.reply_html(
            "这条指令需要回复一条消息，被回复的消息将被广播。"
        )
        return

    context.job_queue.run_once(
        _broadcast,
        0,
        data=f"{update.message.reply_to_message.id}_{update.effective_chat.id}",
    )


async def error_in_send_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "错误的消息类型。退出发送媒体组。后续对话将直接转发。"
    )
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error(f"Exception while handling an update: {context.error} ")
    logger.debug(f"Exception detail is :", exc_info=context.error)

# ==========================================
# 第一部分：定义菜单回复逻辑（必须在启动模块的上方）
# ==========================================
async def menu_auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    text_lower = text.lower()

    # 1. 完全匹配词库
    exact_match_words = ["1", "你好", "在吗", "有人吗", "客服", "hi", "人工", "人工客服", "hello"]

    # 2. 包含匹配词库
    include_match_words = [
        "快搜百万",
        "锁定低价",
        "退订广告",
    ]

    # 3. 核心判断
    is_exact = text_lower in exact_match_words
    is_include = any(word.lower() in text_lower for word in include_match_words)

    if is_exact or is_include:
        await update.message.reply_html(
            "🤖 <b>系统提示：</b>\n\n"
            "为了节约您的时间，请<b>直接详细说明您的问题</b>，或发送<b>订单截图</b>。"
        )
        return

    # 2. 第二优先级：识别菜单按钮（这里就是你原本的那些按钮逻辑）
    
    if text == "🌐 官网地址":
        # 1. 配置悬挂按钮
        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 点击进入官网主站", url="https://tgdomo.com")],
            [InlineKeyboardButton("❓ 网站打不开？", callback_data="site_help"),
             InlineKeyboardButton("📦 如何查询订单", callback_data="order_help")]
        ])

        # 2. 发送排版内容
        await update.message.reply_html(
            "<b>📑 官方使用教程 & 自助下单中心</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "📖 <b>官方使用教程</b>\n"
            "🔹 <a href='https://t.me/JiedianSsr/224 '>电报内置代理使用教程 (必看)</a>\n\n"
            "🔹 【可以认真阅读，每一句话都有用，购买商品了的用户，从第四条开始操作！】\n\n"
            "🛒 <b>自助下单中心</b>\n"
            "🔸 <a href='https://tgdomo.com?code=YT0zJmI9Mw%3D%3D'>TG 代理下单地址 (Telegram专用代理)</a>\n"
            "🔸 <a href='https://tgdomo.com?code=YT04JmI9NTA%3D'>机场节点下单地址 (小火箭)</a>\n"
            "🔸 <a href='https://tgdomo.com?code=YT01JmI9MTM3'>小火箭 Shadowrocket 独享账号</a>\n\n"
            "<i>(很多问题在商品描述、官网公告中已有详细说明)</i>",
            reply_markup=inline_kb,
            disable_web_page_preview=True # 建议开启，防止链接预览占地方
        )
        return
    elif text == "✈️ Telegram专用代理":
        # 定义悬挂按钮
        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 代理个人版", callback_data="proxy_personal"),
             InlineKeyboardButton("👥 代理团队版", callback_data="proxy_team")]
        ])

        # 发送介绍内容
        await update.message.reply_html(
            "<b>✈️ Telegram 官方内置专用代理 (Socks5/MTProto)</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "✅ <b>产品优势</b>：\n"
            "• 无需开启额外VPN，直连电报，不降速。\n"
            "• 隐藏真实IP，保护账号安全，防止被风控。\n"
            "• 支持一键配置，多台设备同步使用。\n\n"
            "💡 <i>请选择您需要的版本了解详细规格及价格：</i>",
            reply_markup=inline_kb
        )
        return
    elif text == "✨ 代开飞机会员":
        await update.message.reply_html(
            "<b>🌟 Telegram Premium 高级会员秒开</b>\n\n"
            "<b>【会员资费】</b>\n"
            "✈️  3个月会员：<code>25U</code>\n"
            "✈️  6个月会员：<code>45U</code>\n"
            "✈️ 12个月会员：<code>70U</code>\n"
            "📛 <b>无需密码：</b>只需提供 <u>用户名</u> 即可！\n\n"
            "<b>👑 开通会员六大特权：</b>\n"
            "1️⃣ <b>专属标志：</b>尊贵会员标识及动态头像\n"
            "2️⃣ <b>降低风险：</b>有效防止双向限制，降低注销风险\n"
            "3️⃣ <b>多端登录：</b>手机支持4开，电脑支持6开账号\n"
            "4️⃣ <b>专属表情：</b>会员专属贴纸、表情包无限使用\n"
            "5️⃣ <b>功能翻倍：</b>列表/群组/频道上限等多项功能翻倍\n"
            "6️⃣ <b>极速体验：</b>享受专线带宽，看片秒开不卡顿\n\n"
        )
        return

    elif text == "🆙 飞机账号":
        await update.message.reply_html(
            "<b>🆙 Telegram 成品账号（老号/稳定号）</b>\n\n"
            "<b>【规格与价格】</b>\n"
            "◈ 普通成品号：<code>10U</code>\n"
            "◈ 半年稳定号：<code>15U</code>\n"
            "◈ 一年超稳号：<code>20U</code>\n\n"
            "<b>【覆盖地区】</b>\n"
            "📍 美国、英国、法国、中国、香港、孟加拉、印度等随机地区\n\n"
        )
        return

    elif text == "🌍 全球VPN定制":
        await update.message.reply_html(
            "<b>🌍 全球跨平台 VPN 定制服务</b>\n\n"
            "<b>【产品优势】</b>\n"
            "✅ <b>不限制设备数量</b>，支持全家/全公司共享\n"
            "✅ 协议全支持：Socks5, HTTP, VMess, V2ray, 小火箭等\n"
            "✅ 连接方式：扫码一键配置、L2TP、PPTP、Windows端\n\n"
            "—— —— —— —— —— ——\n\n"
            "<b>🚩主流地区（香港、新加坡、日本、韩国、印尼、美国等）</b>\n"
            "🔹 普通线路：<code>60U / 月</code>\n"
            "🔹 优质纯净IP：<code>80U / 月</code>\n"
            "🔹 TikTok运营级：<code>120U / 月</code>\n\n"
            "<b>🚩小众及其他地区（包含中国一线/省会城市）</b>\n"
            "🔸 普通线路：<code>100U / 月</code>\n"
            "🔸 优质纯净IP：<code>120U / 月</code>\n"
            "🔸 TikTok运营级：<code>150U / 月</code>\n\n"
            "—— —— —— —— —— ——\n\n"
            "<b>🛠 其他定制说明：</b>\n"
            "◈ <b>特殊链接：</b>如需windows L2TP、PPTP 等特殊协议，加收 <code>40U</code>\n"
            "⚠️ <i>注：所有节点均保证带宽稳定，适合工作室、外贸及专业运营使用。</i>"
        )
        return

    elif text == "🔰 实名人脸":  # 或者是你对应的云业务按钮名
        await update.message.reply_html(
            "<b>☁️ 各类云平台实名账号</b>\n\n"
            "<b>【核心服务】</b>\n"
            "✅ 代付款  ✅ 代充值  ✅ 代购买\n\n"
            "<b>【支持平台】</b>\n"
            "☁️ 阿里云 ｜ 个人/企业实名\n"
            "☁️ 腾讯云 ｜ 个人/企业实名\n"
            "☁️ 华为云 ｜ 个人/企业实名\n"
            "☁️ 天翼云 ｜ 个人/企业实名\n"
            "☁️ 七牛云 ｜ 个人/企业实名\n"
            "☁️ 百度云 ｜ 个人/企业实名\n\n"
            "<b>【适用场景】</b>\n"
            "🔰 各种IDC、网站、CDN分发等\n\n"
            "<b>【资费标准】</b>\n"
            "💰 个人实名：<code>80U</code>\n"
            "💰 企业实名：<code>100U</code>\n\n"
            "✅ <i>有需要的客户请联系人工客服。</i>"
        )
        return

    return await forwarding_message_u2a(update, context)

# ==========================================
# 第二部分：程序的启动与注册模块
# ==========================================
if __name__ == "__main__":
    pickle_persistence = PicklePersistence(filepath=f"./assets/{app_name}.pickle")
    application = (
        ApplicationBuilder()
        .token(bot_token)
        .persistence(persistence=pickle_persistence)
        .build()
    )

    application.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))

       # 第一个：负责所有文字（拦截+按钮+文字转发）
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, menu_auto_reply)
    )

    # 第二个：负责图片和文件（不收文字，直接转发）
    application.add_handler(
        MessageHandler(
            (~filters.COMMAND & ~filters.TEXT) & filters.ChatType.PRIVATE, 
            forwarding_message_u2a
        )
    )
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & filters.Chat([admin_group_id]), forwarding_message_a2u
        )
    )
    application.add_handler(
        CommandHandler("clear", clear, filters.Chat([admin_group_id]))
    )
    application.add_handler(
        CommandHandler("broadcast", broadcast, filters.Chat([admin_group_id]))
    )
        # 这样写可以拦截所有悬挂按钮的点击
    application.add_handler(CallbackQueryHandler(inline_menu_callback))
    application.add_error_handler(error_handler)
    application.run_polling()
