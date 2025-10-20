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

# --- Configuration for Deployment ---
# Render sets PORT and WEBHOOK_URL environment variables for web services.
# BOT_TOKEN must be set in your Render environment variables.
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get('PORT', 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- States ---
CHOOSING_PAYER, CHOOSING_PAYEE, TYPING_AMOUNT, TYPING_DESC = range(4)

# --- In-memory ledger ---
# NOTE: For persistent data across deployments/restarts, you would need to
# switch this to a database like PostgreSQL or Firestore.
ledger = {}  # {payer: {payee: [{'amount': 10, 'desc': 'Lunch'}]}}
USERS = ["Kristy", "You"]

# --- Helper functions ---
def add_expense(payer, payee, amount, desc):
    ledger.setdefault(payer, {})
    ledger.setdefault(payee, {})
    # Record the positive transaction (payer paid)
    ledger[payer].setdefault(payee, []).append({'amount': amount, 'desc': desc})
    # Record the negative transaction (payee owes, represented as -amount relative to payer)
    ledger[payee].setdefault(payer, []).append({'amount': -amount, 'desc': desc})

def format_ledger():
    lines = ["🍓 *KrispyLedger Dashboard*\n"]
    any_entries = False
    
    # Iterate through all unique pairs to show who owes who
    balances = {}
    
    for u1 in USERS:
        for u2 in USERS:
            if u1 == u2:
                continue
            
            # Check balance where u2 owes u1
            if u1 in ledger and u2 in ledger[u1]:
                total = sum(entry['amount'] for entry in ledger[u1][u2])
                if total > 0:
                    # We only want to show the net positive debt once
                    if (u2, u1) not in balances:
                        balances[(u1, u2)] = total
                        
    if balances:
        any_entries = True
        for (payer, payee), total in balances.items():
            lines.append(f"💰 *{payee}* owes *{payer}*: ${total:.2f}")
            # Optionally, list the transactions that make up this debt
            if payer in ledger and payee in ledger[payer]:
                 for entry in ledger[payer][payee]:
                     if entry['amount'] > 0:
                         lines.append(f"   - _{entry['desc']}_ : ${entry['amount']:.2f}")

    if not any_entries:
        lines.append("✨ No balances yet!")
    return "\n".join(lines)


def format_dashboard():
    """Summary of net totals per user."""
    lines = ["📊 *KrispyLedger Summary*\n"]
    if not ledger:
        lines.append("✨ No balances yet!")
    else:
        totals = {}
        # Calculate the net balance for every user
        for user in USERS:
            net_balance = 0
            # Sum up all transactions where 'user' is the key
            if user in ledger:
                for payee, entries in ledger[user].items():
                    net_balance += sum(entry['amount'] for entry in entries)
            totals[user] = net_balance

        for user in USERS:
            balance = totals.get(user, 0)
            if balance > 0:
                # Positive balance means the user is owed money
                lines.append(f"💸 *{user}* is owed ${balance:.2f}")
            elif balance < 0:
                # Negative balance means the user owes money
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
        # Check if the message is from a MessageHandler inside a ConversationHandler
        if update.message is None:
            await update.callback_query.answer("Please type the amount.")
            return TYPING_AMOUNT

        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("⚠️ Amount must be greater than zero.")
            return TYPING_AMOUNT

    except ValueError:
        await update.message.reply_text("⚠️ Enter a valid number (e.g., 15.50).")
        return TYPING_AMOUNT
    except Exception:
        # Fallback for unexpected update types in conversation
        await update.message.reply_text("⚠️ Please type a number for the amount.")
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
        format_dashboard(), # Changed to show summary after addition
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    # Clear user data for next conversation
    context.user_data.clear()
    return ConversationHandler.END

# --- Cancel handler ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Transaction cancelled.", reply_markup=main_menu_keyboard())
    # Clear user data
    context.user_data.clear()
    return ConversationHandler.END

# --- Main ---
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set.")
        return

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
    
    # --- Deployment Logic ---
    if WEBHOOK_URL and BOT_TOKEN:
        # Production deployment on Render using Webhook
        print(f"✅ Running in WEBHOOK mode. URL: {WEBHOOK_URL} listening on port {PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,  # The path Telegram sends updates to
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    else:
        # Local development using Polling
        print("⚠️ Running in POLLING mode (for local development only).")
        app.run_polling(poll_interval=1)

if __name__ == "__main__":
    main()
