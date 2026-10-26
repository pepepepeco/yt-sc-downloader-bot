#!/usr/bin/env python3
"""
Telegram downloader bot v2: YouTube + SoundCloud, cloud-hosted, no phone needed
- User sends a link in DM
- Chooses 1 of 3 formats: 🎬 Video MP4 / 🎵 Music MP3 / 🖼 Photo (cover/thumbnail)
- Chooses 1 of 2 qualities per format:
    video: HD 720p / standard 360p
    audio: high 320 kbps / standard 128 kbps
    photo: high (largest thumbnail) / standard (medium, ~640px)
- Chooses destination (if CHANNEL_ID is set): DM only / channel only / both.
  For channel posting the bot must be added as admin of that channel.
- SoundCloud has no video, so only Music and Photo are offered for it.

Env vars:
  TELEGRAM_TOKEN   (required, from @BotFather - never paste in chat)
  ALLOWED_USER_ID  (optional, numeric Telegram id; if set, only that user can use it)
  CHANNEL_ID       (optional, e.g. @new_post0 or -100xxxxxxxxxx; enables channel posting)
  COOKIES_FILE     (optional, path to a cookies.txt; helps YouTube on datacenter IPs)
  WEBHOOK_URL      (optional; if set, bot runs in webhook mode for Render etc.)
  PORT             (webhook listen port, default 10000 on Render)

Telegram Bot API limit: a bot cannot send a file larger than 50 MB.
Files over that are rejected with a clear message, not silently dropped.
"""
import asyncio
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram Bot API limit

YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.|m\.)?(youtube\.com/(watch|shorts|live|embed|v)/|youtu\.be/|music\.youtube\.com/)\S+",
    re.IGNORECASE,
)
SOUNDCLOUD_RE = re.compile(r"(https?://)?(www\.|m\.)?soundcloud\.com/\S+", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def load_dotenv(path=".env"):
    """Tiny .env reader (no extra dependency) so a VPS install stays simple."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
PORT = int(os.environ.get("PORT", "10000"))


def resolve_cookies_file() -> tuple[str, bool]:
    """Pick the YouTube cookies file without ever logging its contents.

    Order: COOKIES_FILE env (if set and present), then Render's runtime
    secret file /etc/secrets/cookies.txt (added in the Render dashboard,
    never committed to the public repo), then a local cookies.txt.
    Returns (path, has_login_markers) where the markers are only the
    cookie *names* LOGIN_INFO and SAPISID, never values.
    """
    candidates = []
    env_path = os.environ.get("COOKIES_FILE", "").strip()
    if env_path:
        candidates.append(env_path)
    candidates.append("/etc/secrets/cookies.txt")
    candidates.append("cookies.txt")
    for path in candidates:
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                logged_in = ("LOGIN_INFO" in text) and ("SAPISID" in text)
            except OSError:
                logged_in = False
            return path, logged_in
    return "", False


COOKIES_FILE, COOKIES_LOGGED_IN = resolve_cookies_file()


def is_owner(user_id: int) -> bool:
    if not ALLOWED_USER_ID:
        return True
    try:
        return user_id == int(ALLOWED_USER_ID)
    except ValueError:
        return True


def detect_source(url: str) -> str | None:
    low = url.lower()
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    if "soundcloud.com" in low:
        return "soundcloud"
    return None


def format_keyboard(source: str) -> InlineKeyboardMarkup:
    # Photo (thumbnail/cover) works for both YouTube and SoundCloud.
    buttons = [
        [InlineKeyboardButton("🎵 موزیک MP3", callback_data="fmt:audio")],
        [InlineKeyboardButton("🖼 عکس (کاور/تامبنیل)", callback_data="fmt:photo")],
    ]
    if source != "soundcloud":
        buttons.append([InlineKeyboardButton("🎬 ویدیو MP4", callback_data="fmt:video")])
    return InlineKeyboardMarkup(buttons)


def quality_keyboard(fmt: str) -> InlineKeyboardMarkup:
    if fmt == "audio":
        buttons = [
            [InlineKeyboardButton("🔊 بالا — 320kbps", callback_data="q:audio_high")],
            [InlineKeyboardButton("🔉 استاندارد — 128kbps", callback_data="q:audio_std")],
        ]
    elif fmt == "photo":
        buttons = [
            [InlineKeyboardButton("🖼 بالا — بزرگ‌ترین کاور", callback_data="q:photo_high")],
            [InlineKeyboardButton("🖼 استاندارد — متوسط", callback_data="q:photo_std")],
        ]
    else:
        buttons = [
            [InlineKeyboardButton("🖥 HD — 720p", callback_data="q:video_hd")],
            [InlineKeyboardButton("📱 استاندارد — 360p", callback_data="q:video_sd")],
        ]
    return InlineKeyboardMarkup(buttons)


def destination_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 فقط به خودم (دایرکت)", callback_data="dest:dm")],
            [InlineKeyboardButton("📢 فقط توی کانال", callback_data="dest:channel")],
            [InlineKeyboardButton("👤📢 هم دایرکت، هم کانال", callback_data="dest:both")],
        ]
    )


def ydl_options(fmt: str, quality: str, out_dir: str) -> dict:
    opts: dict = {
        "outtmpl": os.path.join(out_dir, "%(title).80s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_args": {"youtube": {"player_client": ["web", "android", "ios"]}},
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if fmt == "audio":
        opts["format"] = "bestaudio/best"
        bitrate = "320" if quality == "high" else "128"
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,
            }
        ]
    else:
        height = "720" if quality == "hd" else "360"
        opts["format"] = (
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        )
        opts["merge_output_format"] = "mp4"
    return opts


def download_sync(url: str, fmt: str, quality: str) -> tuple[str, str]:
    """Returns (file_path, title). Raises yt_dlp.DownloadError on failure."""
    tmpdir = tempfile.mkdtemp(prefix="dl_")
    try:
        with yt_dlp.YoutubeDL(ydl_options(fmt, quality, tmpdir)) as ydl:
            info = ydl.extract_info(url, download=True)
            title = (info or {}).get("title") or "download"
        files = [
            os.path.join(tmpdir, f)
            for f in os.listdir(tmpdir)
            if os.path.isfile(os.path.join(tmpdir, f))
        ]
        if not files:
            raise RuntimeError("no file was produced")
        path = max(files, key=os.path.getsize)
        final_dir = tempfile.mkdtemp(prefix="dl_final_")
        final_path = os.path.join(final_dir, os.path.basename(path))
        shutil.move(path, final_path)
        return final_path, title
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _thumb_ext(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for ext in (".jpg", ".jpeg", ".png"):
        if path.endswith(ext):
            return ext
    return ".jpg"


def pick_thumbnail(info: dict, quality: str) -> str | None:
    """Pick a jpg/png thumbnail URL. high=largest, std=closest to 640px wide."""
    cands = []
    for t in info.get("thumbnails") or []:
        url = t.get("url") or ""
        if not url:
            continue
        if _thumb_ext(url) not in (".jpg", ".jpeg", ".png"):
            continue
        cands.append((int(t.get("width") or 0), url))
    if not cands:
        url = info.get("thumbnail") or ""
        return url or None
    cands.sort()
    if quality == "high":
        return cands[-1][1]
    # standard: closest to 640px, otherwise the middle one
    return min(cands, key=lambda c: abs(c[0] - 640))[1]


def download_photo_sync(url: str, quality: str) -> tuple[str, str]:
    """Download only the cover/thumbnail. Returns (file_path, title)."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_args": {"youtube": {"player_client": ["web", "android", "ios"]}},
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    title = info.get("title") or "cover"
    thumb_url = pick_thumbnail(info, quality)
    if not thumb_url:
        raise RuntimeError("no thumbnail found")
    tmpdir = tempfile.mkdtemp(prefix="dl_photo_")
    path = os.path.join(tmpdir, "cover" + _thumb_ext(thumb_url))
    req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(path, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    if os.path.getsize(path) == 0:
        raise RuntimeError("empty thumbnail")
    return path, title


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("این بات خصوصی است.")
        return
    channel_note = (
        f"\nبعد از انتخاب کیفیت، مقصد را هم انتخاب می‌کنی: دایرکت، کانال ({CHANNEL_ID})، یا هر دو."
        if CHANNEL_ID
        else ""
    )
    await update.message.reply_text(
        "سلام! 👋\n"
        "یک لینک یوتیوب یا ساندکلاد بفرست.\n"
        "بعد فرمت را انتخاب می‌کنی: 🎵 موزیک MP3، 🎬 ویدیو MP4، یا 🖼 عکس (کاور)\n"
        "و بعد کیفیت را: بالا یا استاندارد."
        f"{channel_note}\n\n"
        "⚠️ محدودیت تلگرام: فایل‌های بالای ۵۰ مگابایت ارسال نمی‌شوند؛ "
        "در آن صورت کیفیت استاندارد را انتخاب کن."
    )


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    text = update.message.text or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text("لطفاً یک لینک یوتیوب یا ساندکلاد بفرست.")
        return
    url = match.group(0).rstrip(").,")
    source = detect_source(url)
    if not source:
        await update.message.reply_text(
            "این لینک پشتیبانی نمی‌شود. فقط یوتیوب و ساندکلاد."
        )
        return
    context.user_data["pending_url"] = url
    context.user_data["pending_source"] = source
    note = ""
    if source == "soundcloud":
        note = "\n(ساندکلاد ویدیو ندارد، فقط موزیک و عکس قابل دانلود است.)"
    await update.message.reply_text(
        f"لینک دریافت شد. فرمت را انتخاب کن:{note}",
        reply_markup=format_keyboard(source),
    )


async def _send_to_destinations(query, context, fmt: str, path: str, title: str, dest: str):
    """Send the downloaded file to DM and/or channel according to dest."""
    caption = title[:900]
    targets = []
    if dest in ("dm", "both"):
        targets.append(query.from_user.id)
    if dest in ("channel", "both") and CHANNEL_ID:
        targets.append(CHANNEL_ID)
    bot = context.bot
    filename = os.path.basename(path)
    for chat_id in targets:
        with open(path, "rb") as fh:
            if fmt == "audio":
                await bot.send_audio(chat_id=chat_id, audio=fh, filename=filename, caption=caption)
            elif fmt == "video":
                await bot.send_video(chat_id=chat_id, video=fh, filename=filename, caption=caption)
            else:
                await bot.send_photo(chat_id=chat_id, photo=fh, filename=filename, caption=caption)


async def _do_download(query, context):
    url = context.user_data.get("pending_url")
    fmt = context.user_data.get("pending_fmt")
    quality = context.user_data.get("pending_quality")
    dest = context.user_data.get("pending_dest") or "dm"
    await query.edit_message_text("⏳ در حال دانلود... لطفاً صبر کن.")
    try:
        if fmt == "photo":
            path, title = await asyncio.to_thread(download_photo_sync, url, quality)
        else:
            path, title = await asyncio.to_thread(download_sync, url, fmt, quality)
    except Exception as exc:  # noqa: BLE001 - show a friendly message
        await query.edit_message_text(
            "❌ دانلود ناموفق بود.\n"
            "علت‌های معمول: لینک خصوصی/حذف‌شده است، یا یوتیوب آی‌پی سرور را "
            "مسدود کرده است. یک لینک دیگر یا کیفیت استاندارد را امتحان کن.\n"
            f"جزئیات فنی: {type(exc).__name__}"
        )
        return
    try:
        if fmt != "photo" and os.path.getsize(path) > MAX_FILE_SIZE:
            await query.edit_message_text(
                f"⚠️ فایل {os.path.getsize(path) // (1024 * 1024)} مگابایت است و تلگرام "
                "فایل بالای ۵۰ مگابایت را از بات قبول نمی‌کند.\n"
                "کیفیت استاندارد (۱۲۸kbps / ۳۶۰p) را انتخاب کن."
            )
            return
        try:
            await _send_to_destinations(query, context, fmt, path, title, dest)
        except Exception as exc:  # noqa: BLE001
            await query.edit_message_text(
                "❌ ارسال ناموفق بود. اگر مقصد کانال بود، مطمئن شو بات ادمین کانال است "
                f"و CHANNEL_ID درست تنظیم شده.\nجزئیات فنی: {type(exc).__name__}"
            )
            return
        await query.edit_message_text("✅ ارسال شد.")
    finally:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
    context.user_data.pop("pending_url", None)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_owner(query.from_user.id):
        return
    data = query.data or ""
    url = context.user_data.get("pending_url")
    if not url:
        await query.edit_message_text("لینک منقضی شده؛ لینک را دوباره بفرست.")
        return

    if data.startswith("fmt:"):
        fmt = data.split(":", 1)[1]
        context.user_data["pending_fmt"] = fmt
        await query.edit_message_text(
            "کیفیت را انتخاب کن:", reply_markup=quality_keyboard(fmt)
        )
        return

    if data.startswith("q:"):
        _, choice = data.split(":", 1)  # audio_high / audio_std / video_hd / video_sd / photo_high / photo_std
        fmt, quality = choice.split("_", 1)
        context.user_data["pending_quality"] = quality
        if CHANNEL_ID:
            await query.edit_message_text(
                "کجا بفرستم؟", reply_markup=destination_keyboard()
            )
            return
        context.user_data["pending_dest"] = "dm"
        await _do_download(query, context)
        return

    if data.startswith("dest:"):
        dest = data.split(":", 1)[1]
        context.user_data["pending_dest"] = dest
        await _do_download(query, context)
        return


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is not set. See .env.example")
    # Startup status only: path + whether login markers are present.
    # Never print cookie values.
    if COOKIES_FILE:
        print(f"YouTube cookies: found ({COOKIES_FILE}), "
              f"logged-in markers: {'yes' if COOKIES_LOGGED_IN else 'no'}",
              flush=True)
    else:
        print("YouTube cookies: not found - unauthenticated requests "
              "may hit YouTube bot-check", flush=True)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    if WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{TOKEN}",
        )
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
