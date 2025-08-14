#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal Bot (EN) — TwelveData, Pro Styling, Target/SL Alerts
-----------------------------------------------------------
- Add signals via /addsignal (LONG/SHORT), multiple targets, stop loss, optional note
- Posts a styled card to your channel
- Monitors live prices using TwelveData
- Announces when each Target is hit and when Stop Loss triggers
- Persists to signals.json (survives restarts)
- Admin-only (whitelisted user IDs)
"""

import asyncio
import json
import logging
import os
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Dict, Optional

import aiohttp
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    AIORateLimiter,
    CommandHandler,
    ContextTypes,
)

# -------------------------------
# ========= CONFIG ==============
# You can override via env vars if you want.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8264639158:AAEEXtk27NGTjhDJHJZL-hM9nRKZ3nnqs0A")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002733217001"))
ADMIN_USER_IDS = {
    int(x)
    for x in os.getenv("ADMIN_USER_IDS", "8121424156").split(",")
    if x.strip().isdigit()
}
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "e86b976eb1f74edab9ae0ff466dc28f6")

PRICE_POLL_INTERVAL = float(os.getenv("PRICE_POLL_INTERVAL", "7"))  # seconds
SIGNALS_DB_PATH = os.getenv("SIGNALS_DB_PATH", "signals.json")

TD_PRICE_URL = "https://api.twelvedata.com/price?symbol={symbol}&apikey={key}"

# -------------------------------
# ========= DATA MODEL ==========
# -------------------------------

@dataclass
class Signal:
    id: int
    chat_id: int
    message_id: Optional[int] = None
    symbol: str = "EUR/USD"
    side: str = "LONG"  # LONG or SHORT
    entry: float = 0.0
    targets: List[float] = field(default_factory=list)
    stop: Optional[float] = None
    note: str = ""
    created_at: str = datetime.now(timezone.utc).isoformat()
    active: bool = True
    hit_targets: List[int] = field(default_factory=list)  # indices of hit targets
    provider: str = "twelvedata"

    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "Signal":
        return Signal(**d)


class SignalStore:
    def __init__(self, path: str):
        self.path = path
        self._signals: Dict[int, Signal] = {}
        self._next_id = 1
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._next_id = data.get("next_id", 1)
        self._signals = {int(k): Signal.from_dict(v) for k, v in data.get("signals", {}).items()}

    def save(self):
        data = {
            "next_id": self._next_id,
            "signals": {sid: s.to_dict() for sid, s in self._signals.items()},
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add(self, s: Signal) -> Signal:
        s.id = self._next_id
        self._signals[s.id] = s
        self._next_id += 1
        self.save()
        return s

    def get(self, sid: int) -> Optional[Signal]:
        return self._signals.get(sid)

    def all(self) -> List[Signal]:
        return sorted(self._signals.values(), key=lambda x: (not x.active, x.id))

    def delete(self, sid: int) -> bool:
        if sid in self._signals:
            del self._signals[sid]
            self.save()
            return True
        return False

    def update(self, s: Signal):
        self._signals[s.id] = s
        self.save()


STORE = SignalStore(SIGNALS_DB_PATH)

# -------------------------------
# ========== PROVIDER ===========
# -------------------------------

async def td_get_price(session: aiohttp.ClientSession, symbol: str) -> float:
    enc_symbol = urllib.parse.quote(symbol, safe="")
    url = TD_PRICE_URL.format(symbol=enc_symbol, key=TWELVEDATA_API_KEY)
    async with session.get(url, timeout=10) as resp:
        resp.raise_for_status()
        data = await resp.json()
        if "price" not in data:
            raise ValueError(f"TwelveData error: {data}")
        return float(data["price"])

# -------------------------------
# ========== RENDERING ==========
# -------------------------------

BULL="🟢"; BEAR="🔴"; TARGET="🎯"; STOP="🛑"; ALERT="⚡"; CHECK="✅"; CROSS="❌"; CLOCK="⏱️"; PUSH="📣"

def fmt_signal_card(s: Signal) -> str:
    side_icon = BULL if s.side.upper() == "LONG" else BEAR
    created = datetime.fromisoformat(s.created_at).strftime("%Y-%m-%d %H:%M UTC")
    tg_lines = "\n".join(
        [
            f"{TARGET} T{i+1}: <b>{t:,.5f}</b> {'('+CHECK+' Hit)' if i in s.hit_targets else ''}"
            for i, t in enumerate(s.targets)
        ]
    )
    note_line = f"\n📝 Note: <i>{s.note}</i>" if s.note else ""
    status = CHECK + " Active" if s.active else CROSS + " Inactive"
    body = (
        f"<b>{PUSH} SIGNAL {s.side.upper()} — {s.symbol.upper()}</b> {side_icon}\n"
        f"<code>Entry: {s.entry:,.5f} | SL: {s.stop if s.stop is not None else '-'} | Status: {status}</code>\n"
        f"{tg_lines}\n"
        f"{CLOCK} Created: {created}{note_line}"
    )
    return body

async def post_signal_to_channel(ctx: ContextTypes.DEFAULT_TYPE, s: Signal) -> int:
    text = fmt_signal_card(s)
    msg = await ctx.bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return msg.message_id

async def notify_update(bot, s: Signal, text: str):
    await bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML)
    if s.message_id:
        try:
            await bot.send_message(chat_id=CHANNEL_ID, text=text, reply_to_message_id=s.message_id, parse_mode=ParseMode.HTML)
        except Exception:
            pass

# -------------------------------
# ========= PERMISSIONS =========
# -------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

# -------------------------------
# ========= PARSING ARGS ========
# -------------------------------

def parse_kv_args(text: str) -> Dict[str, str]:
    parts = text.split()
    kv = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            kv[k.strip().lower()] = v.strip()
    return kv

# -------------------------------
# ========== HANDLERS ===========
# -------------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text(
        (
            "Welcome!\n"
            "Add signal example:\n"
            "/addsignal symbol=EUR/USD side=LONG entry=1.1050 targets=1.1100,1.1150 stop=1.1000 note=Breakout\n\n"
            "List: /list\nDelete: /delete 3"
        ).strip()
    )

async def addsignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return

    args_text = update.message.text.partition(" ")[2]
    kv = parse_kv_args(args_text)

    try:
        symbol = kv.get("symbol", "EUR/USD").upper()
        side = kv.get("side", "LONG").upper()
        entry = float(kv.get("entry")) if kv.get("entry") else 0.0
        targets = [float(x) for x in kv.get("targets", "").split(",") if x]
        stop = float(kv["stop"]) if "stop" in kv else None
        note = kv.get("note", "")
        if side not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG or SHORT")
    except Exception:
        await update.message.reply_text(
            "Format error. Example:\n"
            "/addsignal symbol=EUR/USD side=LONG entry=1.1050 targets=1.1100,1.1150 stop=1.1000 note=Breakout"
        )
        return

    s = Signal(
        id=0,
        chat_id=CHANNEL_ID,
        symbol=symbol,
        side=side,
        entry=entry,
        targets=targets,
        stop=stop,
        note=note,
    )
    s = STORE.add(s)
    s.message_id = await post_signal_to_channel(context, s)
    STORE.update(s)

    await update.message.reply_text(f"Signal #{s.id} created and posted to channel.")

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return

    items = STORE.all()
    if not items:
        await update.message.reply_text("No signals yet.")
        return
    lines = []
    for s in items:
        status = "Active" if s.active else "Inactive"
        lines.append(
            f"#{s.id} | {s.symbol} | {s.side} | entry {s.entry} | SL {s.stop} | targets {','.join(map(str, s.targets))} | {status}"
        )
    await update.message.reply_text("\n".join(lines))

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Access denied.")
        return
    parts = update.message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("Usage: /delete <id>")
        return
    sid = int(parts[1])
    ok = STORE.delete(sid)
    await update.message.reply_text("Deleted." if ok else "Not found.")

# -------------------------------
# ======== PRICE MONITOR ========
# -------------------------------

async def monitor_prices(app: Application):
    logging.info("Price monitor started")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                for s in STORE.all():
                    if not s.active:
                        continue
                    price = await td_get_price(session, s.symbol)

                    # Targets
                    for idx, tgt in enumerate(s.targets):
                        if idx in s.hit_targets:
                            continue
                        hit = False
                        if s.side == "LONG" and price >= tgt:
                            hit = True
                        if s.side == "SHORT" and price <= tgt:
                            hit = True
                        if hit:
                            s.hit_targets.append(idx)
                            STORE.update(s)
                            text = (
                                f"{ALERT} <b>Target Hit</b> — {s.symbol} {TARGET}\n"
                                f"{CHECK} T{idx+1} reached: <b>{tgt:,.5f}</b>\n"
                                f"Live price: <b>{price:,.5f}</b>"
                            )
                            await notify_update(app.bot, s, text)

                    # Stop Loss
                    if s.stop is not None and s.active:
                        stop_hit = False
                        if s.side == "LONG" and price <= s.stop:
                            stop_hit = True
                        if s.side == "SHORT" and price >= s.stop:
                            stop_hit = True
                        if stop_hit:
                            s.active = False
                            STORE.update(s)
                            text = (
                                f"{STOP} <b>Stop Loss</b> — {s.symbol}\n"
                                f"SL triggered at: <b>{s.stop:,.5f}</b> | Price: <b>{price:,.5f}</b>"
                            )
                            await notify_update(app.bot, s, text)

                await asyncio.sleep(PRICE_POLL_INTERVAL)
            except Exception as e:
                logging.exception("Monitor error: %s", e)
                await asyncio.sleep(PRICE_POLL_INTERVAL)

# -------------------------------
# ============= MAIN ============
# -------------------------------

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit("Set BOT_TOKEN first.")
    if not TWELVEDATA_API_KEY:
        raise SystemExit("Set TWELVEDATA_API_KEY first.")

    app: Application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("addsignal", addsignal_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))

    async def _run_monitor(_: ContextTypes.DEFAULT_TYPE):
        await monitor_prices(app)
    app.create_task(_run_monitor(None))

    await app.initialize()
    await app.start()
    logging.info("Bot started.")
    try:
        await app.updater.start_polling()
        await asyncio.Event().wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Exiting...")
