import logging
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- States ---
CHOOSING_PAYER, CHOOSING_PAYEE, TYPING_AMOUNT, TYPING_DESC = range(4)

# --- In-memory ledger ---
ledger = {}  # {payer: {payee: [{'amount': 10, 'desc': 'Lunch'}]}}
USERS = ["Kristy", "You"]

# --- Helper functions ---
def add_expense(payer, payee, amount, desc):
    ledger.setdefault(payer, {})
    ledger.setdefault(payee, {})
    ledger[payer].setdefault(payee, []).append({'amount': amount, 'desc': desc})
    ledger[payee].setdefault(payer, []).append({'amount': -amount, 'desc': desc})

def format_ledger():
    lines = ["🍓 *KrispyLedger Dashboard*\n"]
    any_entries = False
    for payer, debts in ledger.items():
        for payee, entries in debts.items():
            total = sum(entry['amount'] for entry in entries)
            if total > 0:
                any_entries = True
                lines.append(f"💰 *{payee}* owes *{payer}*: ${total:.2f}")
                for entry in entries:
                    if entry['amount'] > 0:
                        lines.append(f"   - _{entry['desc']}_ : ${entry['amount']:.2f}")
    if not any_entries:
        lines.append("✨ No balances yet!")
    return "\n".join(lines)

def format_dashboard():
    """Summary of totals per user."""
    lines = ["📊 *KrispyLedger Summary*\n"]
    if not ledger:
        lines.append("✨ No balances yet!")
    else:
        totals = {}
        for payer, debts in ledger.items():
            for payee, entries in debts.items():
                total_amount = sum(entry['amount'] for entry in entries)
                totals[payer] = totals.get(payer, 0) + total_amount
        for user in USERS:
            balance = totals.get(user, 0)
            if balance > 0:
                lines.append(f"💸 *{user}* is owed ${balance:.2f}")
            elif balance < 0:
                lines.append(f"💰 *{user}* owes ${-balance:.2f}")
            else:
                lines.append(f"✅ *{user}* is even")
    return "\n".join(lines)

def main_menu_keyboard():
    """Main menu buttons."""
    keyboard = [
        [InlineKeyboardButton("➕ Add Expense", callback_data="add_expense")],
        [
            InlineKeyboardButton("📜 View Ledger", callback_data="view_ledger"),
            InlineKeyboardButton("🍰 Settle Up", callback_data="settle")
        ],
        [InlineKeyboardButton("📊 Summary Dashboard", callback_data="summary")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Start command ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sticker_file_id = "CAACAgUAAxkBAANKaPYBrywD5hefpEij_UAdhoBzBlYAApIZAAIzVrBXhicq0dBBHfo2BA"
    await update.message.reply_sticker(sticker_file_id)
    await update.message.reply_text(
        "🌸 Welcome to KrispyLedger! Tap a button to start:",
        reply_markup=main_menu_keyboard()
    )

# --- CallbackQuery handler ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_expense":
        keyboard = [[InlineKeyboardButton(user, callback_data=f"payer_{user}")] for user in USERS]
        await query.edit_message_text("🧐 Who paid?", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING_PAYER

    elif data == "view_ledger":
        await query.edit_message_text(format_ledger(), parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data == "settle":
        ledger.clear()
        sticker_file_id = "CAACAgUAAxkBAANLaPYBv0rdel-B2DWPXw9fzsYEneEAApUZAAIzVrBX4g5-PwqYYwE2BA"
        await query.message.reply_sticker(sticker_file_id)
        await query.edit_message_text("🍰 All balances cleared!", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data == "summary":
        await query.edit_message_text(format_dashboard(), parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data.startswith("payer_"):
        payer = data.split("_")[1]
        context.user_data['payer'] = payer
        keyboard = [[InlineKeyboardButton(u, callback_data=f"payee_{u}")] for u in USERS if u != payer]
        await query.edit_message_text(
            f"💰 Payer: *{payer}*\nWho owes the payer?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return CHOOSING_PAYEE

    elif data.startswith("payee_"):
        payee = data.split("_")[1]
        context.user_data['payee'] = payee
        await query.edit_message_text(f"👤 Payee: *{payee}*\nEnter the amount:", parse_mode="Markdown")
        return TYPING_AMOUNT

# --- Amount handler ---
async def amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Enter a valid number.")
        return TYPING_AMOUNT
    context.user_data['amount'] = amount
    await update.message.reply_text("📝 Enter a description:")
    return TYPING_DESC

# --- Description handler ---
async def desc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text
    payer = context.user_data['payer']
    payee = context.user_data['payee']
    amount = context.user_data['amount']

    add_expense(payer, payee, amount, desc)

    sticker_file_id = "CAACAgUAAxkBAANZaPYFMY2-hhDFqWMrxJH3sAijDSQAAqIZAAIzVrBXQRy_bzCSPF02BA"
    await update.message.reply_sticker(sticker_file_id)

    await update.message.reply_text(
        f"✅ Recorded: *{payer}* paid *{payee}* ${amount:.2f} for _{desc}_\n\n" +
        format_ledger(),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return ConversationHandler.END

# --- Cancel handler ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# --- Main ---
def main():
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^(add_expense|view_ledger|settle|summary|payer_|payee_).*$")],
        states={
            CHOOSING_PAYER: [CallbackQueryHandler(button_handler, pattern="^payer_.*$")],
            CHOOSING_PAYEE: [CallbackQueryHandler(button_handler, pattern="^payee_.*$")],
            TYPING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_handler)],
            TYPING_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, desc_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    print("🌸 KrispyLedger Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
