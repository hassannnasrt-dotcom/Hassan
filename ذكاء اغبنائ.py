import os
import json
from datetime import datetime, timedelta
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from openai import OpenAI

# ───────────────────────────────────────────────
# إعدادات
# ───────────────────────────────────────────────
TELEGRAM_TOKEN = "8629819355:AAFk4mt9rq08vxSpKUdUPHQVBkMa7walWfo"
OPENAI_API_KEY = "AIzaSyDPIo5jE95q6PzO2eyAuivUn7Ra1FtTPI0"

client = OpenAI(api_key=OPENAI_API_KEY)

# ───────────────────────────────────────────────
# تخزين السياق والإحصائيات (في الذاكرة)
# للإنتاج استخدم Redis أو SQLite
# ───────────────────────────────────────────────
user_contexts: dict[int, list[dict]] = defaultdict(list)   # سجل المحادثة لكل مستخدم
user_stats:    dict[int, dict]       = defaultdict(lambda: {
    "total": 0,
    "tones": [],          # نبرة كل رسالة وردت
    "warnings": [],       # علامات تحذيرية مكتشفة
    "week_start": datetime.now().isoformat()
})
rate_limit:    dict[int, list]       = defaultdict(list)    # timestamps آخر الطلبات

MAX_CONTEXT   = 10   # عدد الرسائل المحفوظة في السياق
RATE_LIMIT_N  = 5    # عدد الرسائل المسموحة
RATE_LIMIT_W  = 60   # خلال عدد الثواني

# ───────────────────────────────────────────────
# مساعدات
# ───────────────────────────────────────────────
def check_rate_limit(user_id: int) -> bool:
    """True = مسموح، False = محظور مؤقتاً"""
    now = datetime.now()
    timestamps = rate_limit[user_id]
    # احذف الطلبات القديمة
    rate_limit[user_id] = [t for t in timestamps
                           if (now - t).seconds < RATE_LIMIT_W]
    if len(rate_limit[user_id]) >= RATE_LIMIT_N:
        return False
    rate_limit[user_id].append(now)
    return True


def build_context_messages(user_id: int, new_user_msg: str) -> list[dict]:
    """يبني قائمة الرسائل مع السياق المحفوظ"""
    system = {
        "role": "system",
        "content": (
            "أنت مساعد ذكي متخصص في التواصل العاطفي الصحي.\n"
            "مهامك:\n"
            "1. تحليل نبرة الرسالة (غاضبة / حزينة / باردة / محبة / قلقة).\n"
            "2. كشف المشاعر المخفية خلف الكلمات.\n"
            "3. تقديم ثلاثة ردود مقترحة بأساليب مختلفة:\n"
            "   - رد حازم: واضح وصريح لكن محترم\n"
            "   - رد ناعم: دافئ وعاطفي\n"
            "   - رد محايد: هادئ وعقلاني\n"
            "4. تنبيه المستخدم بلطف إن لاحظت علامات تحذيرية (تلاعب، إهانة، عزل...).\n\n"
            "الرد يكون بالعربية حصراً وبالتنسيق التالي بالضبط (JSON):\n"
            "{\n"
            '  "tone": "نبرة الرسالة",\n'
            '  "hidden_feelings": "المشاعر المخفية",\n'
            '  "replies": {\n'
            '    "firm": "الرد الحازم",\n'
            '    "soft": "الرد الناعم",\n'
            '    "neutral": "الرد المحايد"\n'
            "  },\n"
            '  "warning": "علامة تحذيرية إن وجدت وإلا null"\n'
            "}"
        )
    }

    history = user_contexts[user_id][-MAX_CONTEXT:]
    new_msg = {"role": "user",
               "content": f"رسالة من الطرف الآخر:\n{new_user_msg}"}

    return [system] + history + [new_msg]


def save_to_context(user_id: int, user_msg: str, assistant_raw: str):
    user_contexts[user_id].append({"role": "user",    "content": user_msg})
    user_contexts[user_id].append({"role": "assistant","content": assistant_raw})
    # لا نحتفظ بأكثر من MAX_CONTEXT * 2
    user_contexts[user_id] = user_contexts[user_id][-(MAX_CONTEXT * 2):]


def update_stats(user_id: int, tone: str, warning: str | None):
    stats = user_stats[user_id]
    stats["total"] += 1
    stats["tones"].append(tone)
    if warning:
        stats["warnings"].append({"warning": warning,
                                  "date": datetime.now().isoformat()})


# ───────────────────────────────────────────────
# أوامر
# ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💬 *مرحباً بك في بوت التواصل العاطفي*\n\n"
        "أرسل لي رسالة من شريكك/شريكتك وسأحللها وأقترح لك ردوداً مناسبة.\n\n"
        "📌 *الأوامر المتاحة:*\n"
        "/start — هذه الرسالة\n"
        "/clear — مسح سجل المحادثة\n"
        "/stats — ملخص أسبوعي لنمط التواصل\n"
        "/help — مساعدة"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_contexts[user_id].clear()
    await update.message.reply_text("✅ تم مسح سجل المحادثة.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    s = user_stats[user_id]
    total = s["total"]

    if total == 0:
        await update.message.reply_text("لا توجد إحصائيات بعد. أرسل رسائل أولاً! 😊")
        return

    # أكثر نبرة متكررة
    from collections import Counter
    tone_counts = Counter(s["tones"])
    top_tone = tone_counts.most_common(1)[0][0] if tone_counts else "—"

    warnings_count = len(s["warnings"])
    last_warnings = "\n".join(
        f"• {w['warning']}" for w in s["warnings"][-3:]
    ) or "لا توجد"

    text = (
        f"📊 *ملخص نمط تواصلك*\n\n"
        f"🔢 إجمالي الرسائل المحللة: {total}\n"
        f"🎭 النبرة الأكثر تكراراً: {top_tone}\n"
        f"⚠️ علامات تحذيرية مكتشفة: {warnings_count}\n"
        f"\n*آخر التحذيرات:*\n{last_warnings}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 *كيف يعمل البوت؟*\n\n"
        "1. أرسل الرسالة التي وصلتك من شريكك/شريكتك.\n"
        "2. سيحلل البوت النبرة والمشاعر المخفية.\n"
        "3. يقترح 3 ردود: حازم / ناعم / محايد.\n"
        "4. اختر الرد المناسب وانسخه!\n\n"
        "⚠️ البوت يحترم خصوصيتك ولا يحفظ البيانات على سيرفر خارجي."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ───────────────────────────────────────────────
# المعالج الرئيسي
# ───────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    user_text = update.message.text.strip()

    # Rate limit
    if not check_rate_limit(user_id):
        await update.message.reply_text(
            "⏳ أرسلت رسائل كثيرة بسرعة. انتظر دقيقة ثم حاول مجدداً."
        )
        return

    # إشعار "جاري التحليل"
    thinking = await update.message.reply_text("🔍 جاري تحليل الرسالة...")

    try:
        messages = build_context_messages(user_id, user_text)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            response_format={"type": "json_object"}
        )

        raw = response.choices[0].message.content
        data = json.loads(raw)

        tone            = data.get("tone", "—")
        hidden_feelings = data.get("hidden_feelings", "—")
        replies         = data.get("replies", {})
        warning         = data.get("warning")

        # حفظ السياق والإحصائيات
        save_to_context(user_id, user_text, raw)
        update_stats(user_id, tone, warning)

        # ─── بناء الرسالة ───
        msg = (
            f"🎭 *نبرة الرسالة:* {tone}\n"
            f"💭 *المشاعر المخفية:* {hidden_feelings}\n"
        )

        if warning:
            msg += f"\n⚠️ *تحذير:* {warning}\n"

        msg += "\n━━━━━━━━━━━━━━━━━\n*اختر أسلوب ردك:*"

        # أزرار الردود
        keyboard = [
            [InlineKeyboardButton("💪 حازم",   callback_data=f"reply|firm|{user_id}")],
            [InlineKeyboardButton("🌸 ناعم",   callback_data=f"reply|soft|{user_id}")],
            [InlineKeyboardButton("🧘 محايد",  callback_data=f"reply|neutral|{user_id}")],
        ]
        markup = InlineKeyboardMarkup(keyboard)

        # احفظ الردود في context.user_data مؤقتاً
        context.user_data["last_replies"] = replies

        await thinking.delete()
        await update.message.reply_text(msg, parse_mode="Markdown",
                                        reply_markup=markup)

    except json.JSONDecodeError:
        await thinking.delete()
        await update.message.reply_text(
            "⚠️ حدث خطأ في تحليل الرد. حاول مرة أخرى."
        )
    except Exception as e:
        await thinking.delete()
        await update.message.reply_text(
            f"❌ خطأ: {str(e)[:100]}"
        )


# ───────────────────────────────────────────────
# معالج الأزرار
# ───────────────────────────────────────────────
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    if parts[0] != "reply" or len(parts) < 3:
        return

    style   = parts[1]   # firm / soft / neutral
    replies = context.user_data.get("last_replies", {})

    labels = {"firm": "💪 الرد الحازم", "soft": "🌸 الرد الناعم", "neutral": "🧘 الرد المحايد"}
    reply_text = replies.get(style, "لم يتوفر هذا الرد.")

    await query.message.reply_text(
        f"*{labels.get(style, '')}:*\n\n{reply_text}",
        parse_mode="Markdown"
    )


# ───────────────────────────────────────────────
# التشغيل
# ───────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_button))

    print("✅ Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
