# ProxyShop — Telegram Proxy Bot v3

Stripe + NOWPayments (USDT) · Receipts · Expiry reminders · Referral tracking

## Environment Variables

| Variable          | Where to get it                                        |
|-------------------|--------------------------------------------------------|
| BOT_TOKEN         | @BotFather → /newbot                                   |
| ADMIN_ID          | @userinfobot → send /start                             |
| STRIPE_TOKEN      | @BotFather → your bot → Payments → Stripe              |
| NOWPAY_API_KEY    | nowpayments.io → Store Settings → API Keys             |
| NOWPAY_IPN_SECRET | nowpayments.io → Store Settings → IPN Secret           |
| WEBHOOK_BASE_URL  | Your Railway URL e.g. https://proxyshop.up.railway.app |

## Railway Deploy
1. Push folder to GitHub repo
2. railway.app → New Project → Deploy from GitHub
3. Add all env vars in the Variables tab
4. Railway auto-sets PORT

## NOWPayments IPN Setup
After Railway gives you a URL:
1. nowpayments.io → Store Settings → IPN Settings
2. Callback URL: https://YOUR-RAILWAY-URL/nowpay-ipn
3. Copy IPN Secret → NOWPAY_IPN_SECRET env var

## Adding Proxies
Single:  /addproxy 123.45.67.89 8080 user pass HTTP
Bulk:    /addlist
         123.45.67.89:8080:user1:pass1:HTTP
         98.76.54.32:1080:user2:pass2:SOCKS5

## Admin Commands
/admin           — full stats dashboard
/addproxy        — add single proxy
/addlist         — bulk add proxies
/broadcast       — message all users
/checkreminders  — manually trigger reminder check

## Features
- Receipts: formatted order receipt sent immediately after payment
- Reminders: auto DMs at 7 days, 3 days, and day of expiry with renew button
- Referrals: /start?ref=USERID tracking, dashboard shows who joined + spent
