import os
import sqlite3
import logging
import hmac
import hashlib
import json
import asyncio
from datetime import datetime, timedelta
from aiohttp import web, ClientSession
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, PreCheckoutQueryHandler, filters, ContextTypes
)
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN             = os.getenv("BOT_TOKEN", "")
ADMIN_ID          = int(os.getenv("ADMIN_ID", "0"))
STRIPE_TOKEN      = os.getenv("STRIPE_TOKEN", "")
NOWPAY_API_KEY    = os.getenv("NOWPAY_API_KEY", "")
NOWPAY_IPN_SECRET = os.getenv("NOWPAY_IPN_SECRET", "")
WEBHOOK_BASE_URL  = os.getenv("WEBHOOK_BASE_URL", "")
CRYPTO_OPTIONS = {
    "btc":     {"name": "₿ Bitcoin (BTC)",      "currency": "btc"},
    "usdtsol": {"name": "◎ USDT (Solana)",      "currency": "usdtsol"},
    "usdtbsc": {"name": "🔶 USDT (BEP20/BSC)",  "currency": "usdtbsc"},
}

PLANS = {
    "starter": {"name": "Starter", "price": 5,  "count": 10,  "days": 30},
    "pro":     {"name": "Pro",     "price": 15, "count": 50,  "days": 30},
    "elite":   {"name": "Elite",   "price": 30, "count": 150, "days": 30},
}

PROVIDERS = {
    "9proxy": {
        "name": "9Proxy",
        "icon": "🌐",
    },
    "soax": {
        "name": "Soax",
        "icon": "🔷",
    },
}

# Reminder schedule: days before expiry to notify
REMINDER_DAYS = [7, 3, 0]   # 0 = day of expiry

DB_PATH = "proxies.db"
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── DATABASE ──────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")

class DBWrapper:
    def __init__(self, conn, is_pg):
        self.conn = conn
        self.is_pg = is_pg

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.conn.rollback()
        else:
            self.conn.commit()
        self.conn.close()

    def execute(self, query, params=None):
        if not self.is_pg:
            query = query.replace("%s", "?")
        cur = self.conn.cursor()
        try:
            if params:
                cur.execute(query, params)
            else:
                cur.execute(query)
            return cur
        except Exception:
            cur.close()
            raise

    def executescript(self, script):
        if self.is_pg:
            with self.conn.cursor() as cur:
                cur.execute(script)
        else:
            script = script.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
            self.conn.executescript(script)

def db():
    if DATABASE_URL:
        import pg8000
        import ssl
        from urllib.parse import urlparse
        url = urlparse(DATABASE_URL)
        ssl_ctx = ssl.create_default_context()
        conn = pg8000.connect(
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port or 5432,
            database=url.path[1:],
            ssl_context=ssl_ctx
        )
        return DBWrapper(conn, is_pg=True)
    else:
        conn = sqlite3.connect(DB_PATH)
        return DBWrapper(conn, is_pg=False)

def setup_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS proxies (
                id           SERIAL PRIMARY KEY,
                host         TEXT    NOT NULL,
                port         INTEGER NOT NULL,
                username     TEXT,
                password     TEXT,
                protocol     TEXT    DEFAULT 'HTTP',
                assigned_to  BIGINT,
                expires      DATE,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id       BIGINT PRIMARY KEY,
                username      TEXT,
                full_name     TEXT,
                referred_by   BIGINT,
                joined_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS orders (
                id             SERIAL PRIMARY KEY,
                user_id        BIGINT NOT NULL,
                plan           TEXT    NOT NULL,
                amount         REAL    NOT NULL,
                proxy_count    INTEGER NOT NULL,
                payment_method TEXT    DEFAULT 'stripe',
                paid_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_crypto (
                payment_id TEXT PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                plan_key   TEXT    NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS reminders_sent (
                user_id    BIGINT NOT NULL,
                expires    DATE NOT NULL,
                days_before INTEGER NOT NULL,
                sent_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, expires, days_before)
            );
        """)

# ── HELPERS ───────────────────────────────────────────────────────────────────
def upsert_user(user, referred_by: int = None):
    with db() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id=%s", (user.id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET username=%s, full_name=%s WHERE user_id=%s",
                (user.username or "", user.full_name or "", user.id)
            )
        else:
            conn.execute(
                "INSERT INTO users (user_id, username, full_name, referred_by) VALUES (%s,%s,%s,%s)",
                (user.id, user.username or "", user.full_name or "", referred_by)
            )

def assign_proxies(user_id: int, plan_key: str, method: str = "stripe"):
    plan   = PLANS[plan_key]
    expiry = (datetime.now() + timedelta(days=plan["days"])).strftime("%Y-%m-%d")
    with db() as conn:
        rows = conn.execute(
            "SELECT id,host,port,username,password,protocol FROM proxies "
            "WHERE assigned_to IS NULL LIMIT %s", (plan["count"],)
        ).fetchall()
        result = []
        for pid, host, port, uname, pwd, proto in rows:
            conn.execute(
                "UPDATE proxies SET assigned_to=%s, expires=%s WHERE id=%s",
                (user_id, expiry, pid)
            )
            result.append(f"{proto}://{uname}:{pwd}@{host}:{port}" if uname
                          else f"{proto}://{host}:{port}")
        conn.execute(
            "INSERT INTO orders (user_id,plan,amount,proxy_count,payment_method) "
            "VALUES (%s,%s,%s,%s,%s)",
            (user_id, plan_key, plan["price"], len(rows), method)
        )
    return result, expiry

def pool_count():
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM proxies WHERE assigned_to IS NULL"
        ).fetchone()[0]

def admin_stats():
    with db() as conn:
        users      = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        orders     = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        revenue    = conn.execute("SELECT COALESCE(SUM(amount),0) FROM orders").fetchone()[0]
        stripe_rev = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE payment_method='stripe'"
        ).fetchone()[0]
        crypto_rev = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM orders WHERE payment_method='crypto'"
        ).fetchone()[0]
        active     = conn.execute(
            "SELECT COUNT(*) FROM proxies "
            "WHERE assigned_to IS NOT NULL AND expires>=CURRENT_DATE"
        ).fetchone()[0]
        pool       = pool_count()
        referrals  = conn.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL"
        ).fetchone()[0]
    return dict(users=users, orders=orders, revenue=revenue,
                stripe_rev=stripe_rev, crypto_rev=crypto_rev,
                active=active, pool=pool, referrals=referrals)

def get_referral_stats(user_id: int) -> dict:
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by=%s", (user_id,)
        ).fetchone()[0]
        paid = conn.execute(
            """SELECT COUNT(DISTINCT u.user_id)
               FROM users u
               JOIN orders o ON o.user_id = u.user_id
               WHERE u.referred_by=%s""", (user_id,)
        ).fetchone()[0]
        referrals = conn.execute(
            """SELECT u.username, u.full_name, u.joined_at,
                      COUNT(o.id) as order_count,
                      COALESCE(SUM(o.amount), 0) as total_spent
               FROM users u
               LEFT JOIN orders o ON o.user_id = u.user_id
               WHERE u.referred_by=%s
               GROUP BY u.user_id
               ORDER BY u.joined_at DESC
               LIMIT 10""", (user_id,)
        ).fetchall()
    return dict(total=total, paid=paid, referrals=referrals)

def is_admin(uid): return uid == ADMIN_ID

def referral_link(user_id: int, bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"

def build_receipt(user, plan_key: str, proxies: list, expiry: str, method: str) -> str:
    p = PLANS[plan_key]
    method_label = "💳 Card (Stripe)" if method == "stripe" else "🪙 Crypto"
    order_id     = f"{user.id}-{int(datetime.now().timestamp())}"
    proxy_lines  = "\n".join(f"  `{px}`" for px in proxies)
    return (
        f"🧾 *Order Receipt*\n"
        f"{'─' * 28}\n"
        f"Order ID:   `{order_id}`\n"
        f"Plan:       *{p['name']}* — ${p['price']}\n"
        f"Payment:    {method_label}\n"
        f"Proxies:    {len(proxies)} of {p['count']}\n"
        f"Expires:    *{expiry}*\n"
        f"Date:       {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"{'─' * 28}\n\n"
        f"*Your proxies:*\n{proxy_lines}\n\n"
        f"_Save this message. View proxies anytime via My Proxies._"
    )

# ── NOWPAYMENTS ───────────────────────────────────────────────────────────────
async def create_nowpay_payment(user_id: int, plan_key: str, pay_currency: str = "btc") -> dict:
    plan    = PLANS[plan_key]
    payload = {
        "price_amount":      plan["price"],
        "price_currency":    "usd",
        "pay_currency":      pay_currency,
        "order_id":          f"{user_id}_{plan_key}_{int(datetime.now().timestamp())}",
        "order_description": f"{plan['name']} Proxy Plan — {plan['count']} proxies",
    }
    if WEBHOOK_BASE_URL:
        payload["ipn_callback_url"] = f"{WEBHOOK_BASE_URL}/nowpay-ipn"
    async with ClientSession() as session:
        async with session.post(
            "https://api.nowpayments.io/v1/payment",
            json=payload,
            headers={"x-api-key": NOWPAY_API_KEY, "Content-Type": "application/json"}
        ) as resp:
            data = await resp.json()
    if "payment_id" not in data:
        raise Exception(f"NOWPayments error: {data}")
    with db() as conn:
        conn.execute(
            "INSERT INTO pending_crypto (payment_id,user_id,plan_key) VALUES (%s,%s,%s) "
            "ON CONFLICT (payment_id) DO UPDATE SET user_id=EXCLUDED.user_id, plan_key=EXCLUDED.plan_key",
            (str(data["payment_id"]), user_id, plan_key)
        )
    return data

def verify_nowpay_sig(body: bytes, sig: str) -> bool:
    expected = hmac.new(NOWPAY_IPN_SECRET.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, sig.lower())

# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒  Buy Proxies",   callback_data="buy")],
        [InlineKeyboardButton("📦  My Proxies",    callback_data="my_proxies")],
        [InlineKeyboardButton("👥  Referrals",     callback_data="referrals")],
        [InlineKeyboardButton("ℹ️  How It Works",  callback_data="how")],
        [InlineKeyboardButton("🆘  Support",       callback_data="support")],
    ])

def plans_menu():
    rows = []
    for key, p in PLANS.items():
        rows.append([InlineKeyboardButton(
            f"{p['name']}  —  ${p['price']}/mo  ({p['count']} proxies)",
            callback_data=f"plan_{key}"
        )])
    rows.append([InlineKeyboardButton("⬅️  Back", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def provider_select_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐  9Proxy", callback_data="provider_9proxy")],
        [InlineKeyboardButton("🔷  Soax",   callback_data="provider_soax")],
        [InlineKeyboardButton("⬅️  Back",   callback_data="back_main")],
    ])

def provider_plans_menu(provider_key: str):
    rows = []
    for key, p in PLANS.items():
        rows.append([InlineKeyboardButton(
            f"{p['name']}  —  ${p['price']}/mo  ({p['count']} proxies)",
            callback_data=f"plan_{key}"
        )])
    rows.append([InlineKeyboardButton("⬅️  Back", callback_data="buy")])
    return InlineKeyboardMarkup(rows)

def payment_method_menu(plan_key: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳  Pay with Card (Stripe)", callback_data=f"stripe_{plan_key}")],
        [InlineKeyboardButton("🪙  Pay with Crypto",        callback_data=f"crypto_{plan_key}")],
        [InlineKeyboardButton("⬅️  Change plan",            callback_data="buy")],
    ])

def crypto_select_menu(plan_key: str):
    rows = []
    for key, c in CRYPTO_OPTIONS.items():
        rows.append([InlineKeyboardButton(
            c["name"], callback_data=f"cpay_{key}_{plan_key}"
        )])
    rows.append([InlineKeyboardButton("⬅️  Back", callback_data=f"plan_{plan_key}")])
    return InlineKeyboardMarkup(rows)

# ── HANDLERS: PUBLIC ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args        = ctx.args
    referred_by = None

    if args and args[0].startswith("ref_"):
        try:
            referred_by = int(args[0].split("_")[1])
            if referred_by == update.effective_user.id:
                referred_by = None   # can't refer yourself
        except (ValueError, IndexError):
            referred_by = None

    upsert_user(update.effective_user, referred_by=referred_by)

    name = update.effective_user.first_name or "there"
    welcome = (
        f"👋 Hey {name}! Welcome to *ProxyShop*.\n\n"
        "Fast, reliable HTTP/SOCKS5 proxies delivered instantly after payment.\n\n"
        "What would you like to do?"
    )
    if referred_by:
        welcome = f"👋 Hey {name}! You were referred by a friend — welcome!\n\n" \
                  "Fast, reliable HTTP/SOCKS5 proxies delivered instantly after payment.\n\n" \
                  "What would you like to do?"

    await update.message.reply_text(
        welcome, parse_mode="Markdown", reply_markup=main_menu()
    )

async def cb_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.message.edit_text(
        "🛒 *Buy Proxies*\n\nSelect a proxy provider:",
        parse_mode="Markdown",
        reply_markup=provider_select_menu()
    )

async def cb_provider(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    provider_key = q.data.split("_", 1)[1]
    p = PROVIDERS[provider_key]
    lines = [f"{p['icon']} *{p['name']} — Choose a Plan*\n"]
    for key, plan in PLANS.items():
        lines.append(f"• *{plan['name']}*  —  ${plan['price']}/mo  ({plan['count']} proxies, {plan['days']} days)")
    lines.append("\nSelect a plan below to continue:")
    await q.message.edit_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=provider_plans_menu(provider_key)
    )

async def cb_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    plan_key = q.data.split("_", 1)[1]
    p = PLANS[plan_key]
    await q.message.edit_text(
        f"🔍 *{p['name']} Plan — ${p['price']}/month*\n\n"
        f"• {p['count']} proxies\n• {p['days']}-day access\n• HTTP + SOCKS5\n\n"
        "Choose your payment method:",
        parse_mode="Markdown", reply_markup=payment_method_menu(plan_key)
    )

# ── STRIPE ────────────────────────────────────────────────────────────────────
async def cb_stripe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    plan_key = q.data.split("_", 1)[1]
    p = PLANS[plan_key]
    if not STRIPE_TOKEN:
        await q.message.edit_text(
            "⚠️ Card payments not configured. Please use USDT or contact admin.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🪙 Pay USDT instead", callback_data=f"crypto_{plan_key}"),
                InlineKeyboardButton("⬅️ Back", callback_data="buy"),
            ]])
        ); return
    await ctx.bot.send_invoice(
        chat_id=q.message.chat_id,
        title=f"{p['name']} Proxy Plan",
        description=f"{p['count']} proxies · 30 days · instant delivery",
        payload=f"proxy_{plan_key}",
        provider_token=STRIPE_TOKEN,
        currency="USD",
        prices=[LabeledPrice(p["name"], p["price"] * 100)],
    )

async def precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user     = update.effective_user
    plan_key = update.message.successful_payment.invoice_payload.split("_", 1)[1]
    await _deliver_and_notify(ctx, user.id, user, plan_key, method="stripe")

# ── CRYPTO ────────────────────────────────────────────────────────────────────
async def cb_crypto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    plan_key = q.data.split("_", 1)[1]
    p = PLANS[plan_key]
    if not NOWPAY_API_KEY:
        await q.message.edit_text(
            "⚠️ Crypto payments not configured. Please use card or contact admin.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💳 Pay by card instead", callback_data=f"stripe_{plan_key}"),
                InlineKeyboardButton("⬅️ Back", callback_data="buy"),
            ]])
        ); return
    await q.message.edit_text(
        f"🪙 *Choose your crypto — {p['name']} Plan (${p['price']})*\n\n"
        "Select a currency to pay with:",
        parse_mode="Markdown",
        reply_markup=crypto_select_menu(plan_key)
    )

async def cb_crypto_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, currency_key, plan_key = q.data.split("_", 2)
    crypto = CRYPTO_OPTIONS[currency_key]
    p = PLANS[plan_key]
    await q.message.edit_text("⏳ Generating your payment address...")
    try:
        payment  = await create_nowpay_payment(q.from_user.id, plan_key, crypto["currency"])
        address  = payment.get("pay_address", "")
        amount   = payment.get("pay_amount", "")
        currency = payment.get("pay_currency", crypto["currency"]).upper()
        await q.message.edit_text(
            f"🪙 *Pay with {crypto['name']}*\n\n"
            f"Plan: *{p['name']}* — ${p['price']}\n\n"
            f"Send exactly:\n"
            f"`{amount} {currency}`\n\n"
            f"To this address:\n"
            f"`{address}`\n\n"
            f"⏱ Address valid for 60 minutes.\n"
            f"Proxies will be delivered here automatically once confirmed.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️  Change currency", callback_data=f"crypto_{plan_key}")],
                [InlineKeyboardButton("⬅️  Back",            callback_data="buy")],
            ])
        )
    except Exception as e:
        log.error(f"NOWPayments error: {e}")
        await q.message.edit_text(
            "❌ Could not generate payment address. Please try again or contact support.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🆘 Support", callback_data="support")
            ]])
        )

# ── SHARED DELIVERY ───────────────────────────────────────────────────────────
async def _deliver_and_notify(ctx, user_id: int, user_obj, plan_key: str, method: str):
    p = PLANS[plan_key]
    proxies, expiry = assign_proxies(user_id, plan_key, method)
    method_label    = "💳 Card (Stripe)" if method == "stripe" else "🪙 Crypto"

    if proxies:
        receipt = build_receipt(user_obj, plan_key, proxies, expiry, method)
        await ctx.bot.send_message(
            chat_id=user_id, text=receipt, parse_mode="Markdown"
        )
        if len(proxies) < p["count"]:
            await ctx.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ *Partial delivery:* You received {len(proxies)} of "
                    f"{p['count']} proxies. The pool is currently low — "
                    f"the admin has been notified and will send the rest shortly."
                ),
                parse_mode="Markdown"
            )
    else:
        await ctx.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Payment confirmed! The proxy pool is currently empty — "
                "the admin has been notified and will send your proxies shortly."
            )
        )

    username = getattr(user_obj, "username", None) or "N/A"
    status   = "✅ DELIVERED" if proxies else "⚠️ POOL EMPTY"
    await ctx.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"💰 *New Sale!*\n\n"
            f"👤 User: @{username} (`{user_id}`)\n"
            f"📦 Plan: *{p['name']}* (${p['price']})\n"
            f"💳 Method: {method_label}\n"
            f"🔢 Proxies: {len(proxies)}/{p['count']} assigned\n"
            f"📅 Expiry: {expiry}\n"
            f"Status: {status}"
        ),
        parse_mode="Markdown"
    )

# ── MY PROXIES ────────────────────────────────────────────────────────────────
async def cb_my_proxies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT host,port,username,password,protocol,expires FROM proxies "
            "WHERE assigned_to=%s AND expires>=CURRENT_DATE ORDER BY expires DESC",
            (q.from_user.id,)
        ).fetchall()
    if not rows:
        await q.message.edit_text(
            "You have no active proxies.\nUse *Buy Proxies* to get started!",
            parse_mode="Markdown", reply_markup=main_menu()
        ); return
    lines = ["*Your active proxies:*\n"]
    for host, port, uname, pwd, proto, exp in rows:
        entry = (f"`{proto}://{uname}:{pwd}@{host}:{port}`" if uname
                 else f"`{proto}://{host}:{port}`")
        lines.append(f"{entry}\n  └ expires *{exp}*")
    await q.message.edit_text(
        "\n\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data="back_main")
        ]])
    )

# ── REFERRALS ─────────────────────────────────────────────────────────────────
async def cb_referrals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    user_id      = q.from_user.id
    bot_username = (await ctx.bot.get_me()).username
    stats        = get_referral_stats(user_id)
    link         = referral_link(user_id, bot_username)

    lines = [
        "👥 *Your Referral Dashboard*\n",
        f"🔗 Your link:\n`{link}`\n",
        f"Total referred:   *{stats['total']}*",
        f"Converted (paid): *{stats['paid']}*\n",
    ]

    if stats["referrals"]:
        lines.append("*Recent referrals:*")
        for uname, fname, joined, orders, spent in stats["referrals"]:
            name    = f"@{uname}" if uname else fname or "Anonymous"
            status  = f"${spent:.0f} spent" if orders > 0 else "not yet purchased"
            joined_fmt = joined[:10] if joined else "?"
            lines.append(f"• {name} — joined {joined_fmt} — {status}")
    else:
        lines.append("_No referrals yet. Share your link to get started!_")

    lines.append("\n_Share your link — every person who joins is tracked here._")

    await q.message.edit_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back", callback_data="back_main")
        ]])
    )

# ── HOW / SUPPORT ─────────────────────────────────────────────────────────────
async def cb_how(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.message.edit_text(
        "ℹ️ *How It Works*\n\n"
        "1️⃣  Pick a plan\n"
        "2️⃣  Pay by card or USDT\n"
        "3️⃣  Proxies + receipt land in this chat instantly\n"
        "4️⃣  Use them for 30 days, renew any time\n\n"
        "*Proxy format:* `PROTOCOL://user:pass@host:port`\n\n"
        "You'll get reminder messages at 7 days, 3 days, and on expiry day.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Buy Now", callback_data="buy")],
            [InlineKeyboardButton("⬅️ Back",    callback_data="back_main")],
        ])
    )

async def cb_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.message.edit_text(
        "🆘 *Support* — reach out to the admin:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Message Admin", url=f"tg://user?id={ADMIN_ID}")],
            [InlineKeyboardButton("⬅️ Back",          callback_data="back_main")],
        ])
    )

async def cb_back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.message.edit_text("What would you like to do?", reply_markup=main_menu())

# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    s = admin_stats()
    await update.message.reply_text(
        f"🛠 *Admin Panel*\n\n"
        f"👥 Users:             {s['users']}\n"
        f"👥 Via referral:      {s['referrals']}\n"
        f"🧾 Total orders:      {s['orders']}\n"
        f"💰 Total revenue:     ${s['revenue']:.2f}\n"
        f"   ├ 💳 Stripe:       ${s['stripe_rev']:.2f}\n"
        f"   └ 🪙 Crypto:       ${s['crypto_rev']:.2f}\n"
        f"✅ Active proxies:    {s['active']}\n"
        f"🔵 Pool (available):  {s['pool']}\n\n"
        "*Commands:*\n"
        "`/addproxy host port user pass [HTTP]`\n"
        "`/addlist` — bulk add\n"
        "`/broadcast` — message all users\n"
        "`/checkreminders` — run reminder check now",
        parse_mode="Markdown"
    )

async def cmd_addproxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    args = ctx.args
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: `/addproxy host port user pass [HTTP|SOCKS5]`",
            parse_mode="Markdown"
        ); return
    host, port, uname, pwd = args[0], int(args[1]), args[2], args[3]
    proto = args[4].upper() if len(args) > 4 else "HTTP"
    with db() as conn:
        conn.execute(
            "INSERT INTO proxies (host,port,username,password,protocol) VALUES (%s,%s,%s,%s,%s)",
            (host, port, uname, pwd, proto)
        )
    await update.message.reply_text(
        f"✅ Added: `{proto}://{uname}:{pwd}@{host}:{port}`", parse_mode="Markdown"
    )

async def cmd_addlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if not ctx.args:
        await update.message.reply_text(
            "Format:\n`/addlist\nhost:port:user:pass:PROTOCOL`",
            parse_mode="Markdown"
        ); return
    lines = " ".join(ctx.args).split()
    added = failed = 0
    with db() as conn:
        for line in lines:
            parts = line.strip().split(":")
            if len(parts) < 4: failed += 1; continue
            host, port, uname, pwd = parts[0], int(parts[1]), parts[2], parts[3]
            proto = parts[4].upper() if len(parts) > 4 else "HTTP"
            conn.execute(
                "INSERT INTO proxies (host,port,username,password,protocol) VALUES (%s,%s,%s,%s,%s)",
                (host, port, uname, pwd, proto)
            )
            added += 1
    await update.message.reply_text(f"✅ Added {added} proxies. ❌ Failed: {failed}")

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    msg = " ".join(ctx.args)
    if not msg:
        await update.message.reply_text(
            "Usage: `/broadcast Your message`", parse_mode="Markdown"
        ); return
    with db() as conn:
        uids = [r[0] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    sent = failed = 0
    for uid in uids:
        try:
            await ctx.bot.send_message(chat_id=uid, text=f"📢 {msg}")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"📢 Sent: {sent} | Failed: {failed}")

async def cmd_checkreminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    sent = await run_expiry_reminders(ctx.bot)
    await update.message.reply_text(f"✅ Reminder check done. Sent: {sent}")

# ── EXPIRY REMINDERS ──────────────────────────────────────────────────────────
async def run_expiry_reminders(bot) -> int:
    """
    Called once per day. Checks all assigned proxies and sends reminders
    at 7 days, 3 days, and 0 days (day of expiry) before they expire.
    Only sends each reminder once (tracked in reminders_sent).
    """
    today   = datetime.now().date()
    sent_count = 0

    for days_before in REMINDER_DAYS:
        target_date = (today + timedelta(days=days_before)).strftime("%Y-%m-%d")

        with db() as conn:
            # Find users with proxies expiring on target_date
            rows = conn.execute(
                """SELECT DISTINCT p.assigned_to, p.expires
                   FROM proxies p
                   WHERE p.expires = %s
                     AND p.assigned_to IS NOT NULL""",
                (target_date,)
            ).fetchall()

        for user_id, expires in rows:
            # Check if we already sent this reminder
            with db() as conn:
                already = conn.execute(
                    "SELECT 1 FROM reminders_sent "
                    "WHERE user_id=%s AND expires=%s AND days_before=%s",
                    (user_id, expires, days_before)
                ).fetchone()

            if already:
                continue

            # Compose message
            if days_before == 0:
                urgency = "🔴 *Your proxies expire TODAY!*"
                cta     = "Renew now to avoid any interruption."
            elif days_before == 3:
                urgency = "🟠 *Your proxies expire in 3 days.*"
                cta     = "Renew soon to keep access without any gap."
            else:
                urgency = "🟡 *Your proxies expire in 7 days.*"
                cta     = "Plan ahead and renew when ready."

            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"{urgency}\n\n"
                        f"Expiry date: *{expires}*\n\n"
                        f"{cta}"
                    ),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 Renew Now", callback_data="buy")
                    ]])
                )
                # Mark as sent
                with db() as conn:
                    conn.execute(
                        "INSERT INTO reminders_sent (user_id,expires,days_before) VALUES (%s,%s,%s) "
                        "ON CONFLICT (user_id,expires,days_before) DO NOTHING",
                        (user_id, expires, days_before)
                    )
                sent_count += 1
                await asyncio.sleep(0.05)   # gentle rate limiting
            except Exception as e:
                log.warning(f"Could not send reminder to {user_id}: {e}")

    log.info(f"Expiry reminder run done. Sent: {sent_count}")
    return sent_count

async def daily_reminder_loop(bot):
    """Runs forever, triggering reminder check once every 24 hours."""
    while True:
        await run_expiry_reminders(bot)
        await asyncio.sleep(86400)   # 24 hours

# ── NOWPAYMENTS WEBHOOK ───────────────────────────────────────────────────────
async def nowpay_ipn(request: web.Request):
    body = await request.read()
    sig  = request.headers.get("x-nowpayments-sig", "")

    if not NOWPAY_IPN_SECRET:
        log.error("NOWPAY_IPN_SECRET not configured — rejecting IPN request")
        return web.Response(status=500, text="IPN secret not configured")

    if not verify_nowpay_sig(body, sig):
        log.warning("Invalid NOWPayments IPN signature")
        return web.Response(status=400, text="Invalid signature")

    try:
        data = json.loads(body)
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    log.info(f"NOWPayments IPN: {data}")

    if data.get("payment_status") != "finished":
        return web.Response(text="ok")

    payment_id = str(data.get("payment_id", ""))

    with db() as conn:
        row = conn.execute(
            "SELECT user_id,plan_key FROM pending_crypto WHERE payment_id=%s",
            (payment_id,)
        ).fetchone()

    if not row:
        log.warning(f"No pending order for payment_id {payment_id}")
        return web.Response(text="ok")

    user_id, plan_key = row

    # Look up real user info from DB for the receipt
    with db() as conn:
        user_row = conn.execute(
            "SELECT username, full_name FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()

    class _CryptoUser:
        def __init__(self, uid, uname, fname):
            self.id = uid
            self.username = uname
            self.full_name = fname or "User"
            self.first_name = fname or "User"

    crypto_user = _CryptoUser(
        user_id,
        user_row[0] if user_row else None,
        user_row[1] if user_row else None,
    )

    await _deliver_and_notify(
        request.app["bot"], user_id, crypto_user, plan_key, method="crypto"
    )

    with db() as conn:
        conn.execute(
            "DELETE FROM pending_crypto WHERE payment_id=%s", (payment_id,)
        )

    return web.Response(text="ok")

async def health(request: web.Request):
    return web.Response(text="ProxyShop bot is running ✅")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    setup_db()

    app_tg = Application.builder().token(TOKEN).build()

    # Public handlers
    app_tg.add_handler(CommandHandler("start",     cmd_start))
    app_tg.add_handler(CallbackQueryHandler(cb_buy,        pattern="^buy$"))
    app_tg.add_handler(CallbackQueryHandler(cb_provider,   pattern="^provider_"))
    app_tg.add_handler(CallbackQueryHandler(cb_plan,       pattern="^plan_"))
    app_tg.add_handler(CallbackQueryHandler(cb_stripe,     pattern="^stripe_"))
    app_tg.add_handler(CallbackQueryHandler(cb_crypto,     pattern="^crypto_"))
    app_tg.add_handler(CallbackQueryHandler(cb_crypto_pay, pattern="^cpay_"))
    app_tg.add_handler(CallbackQueryHandler(cb_my_proxies, pattern="^my_proxies$"))
    app_tg.add_handler(CallbackQueryHandler(cb_referrals,  pattern="^referrals$"))
    app_tg.add_handler(CallbackQueryHandler(cb_how,        pattern="^how$"))
    app_tg.add_handler(CallbackQueryHandler(cb_support,    pattern="^support$"))
    app_tg.add_handler(CallbackQueryHandler(cb_back_main,  pattern="^back_main$"))

    # Stripe
    app_tg.add_handler(PreCheckoutQueryHandler(precheckout))
    app_tg.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Admin
    app_tg.add_handler(CommandHandler("admin",          cmd_admin))
    app_tg.add_handler(CommandHandler("addproxy",       cmd_addproxy))
    app_tg.add_handler(CommandHandler("addlist",        cmd_addlist))
    app_tg.add_handler(CommandHandler("broadcast",      cmd_broadcast))
    app_tg.add_handler(CommandHandler("checkreminders", cmd_checkreminders))

    # Web server for IPN + health
    web_app = web.Application()
    web_app["bot"] = app_tg
    web_app.router.add_post("/nowpay-ipn", nowpay_ipn)
    web_app.router.add_get("/",            health)

    port = int(os.getenv("PORT", "8080"))

    async def run_all():
        await app_tg.initialize()
        await app_tg.start()
        await app_tg.updater.start_polling()

        runner = web.AppRunner(web_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        log.info(f"Bot running on port {port}")

        # Start daily reminder loop in background
        asyncio.create_task(daily_reminder_loop(app_tg.bot))

        await asyncio.Event().wait()

    asyncio.run(run_all())

if __name__ == "__main__":
    main()
