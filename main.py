import logging
import os
import json
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
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get('PORT', 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

# --- Persistence Setup (Simulated Database) ---
# NOTE: This local file approach relies on you manually committing and pushing 
# the all_ledgers.json file to Git to achieve persistence across restarts.
LEDGER_FILE = "all_ledgers.json"

def load_all_ledgers():
    """Load all ledgers (keyed by chat_id) from file."""
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE, 'r') as f:
                return json.load(f)
        except (IOError, json.JSONDecodeError):
            logger.warning("Could not load or parse ledger file. Starting fresh.")
            return {}
    return {}

def save_all_ledgers(data):
    """Save all ledgers data to file."""
    try:
        with open(LEDGER_FILE, 'w') as f:
            json.dump(data, f, indent=4)
    except IOError:
        logger.error("Could not save ledger file.")

# Global variable to hold all ledgers (keyed by chat_id)
all_ledgers = load_all_ledgers()

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- States ---
CHOOSING_PAYER, CHOOSING_PAYEE, TYPING_AMOUNT, TYPING_DESC = range(4)

# --- Chat-Specific Data Accessors ---

def get_chat_data(chat_id):
    """Retrieves the data structure for the specific chat, initializing if necessary."""
    # Data structure: {'ledger': {}, 'users': []}
    chat_data = all_ledgers.setdefault(str(chat_id), {'ledger': {}, 'users': []})
    return chat_data

def get_current_users(chat_id):
    """Get the list of user names for the current chat."""
    return get_chat_data(chat_id)['users']

def get_current_ledger(chat_id):
    """Get the actual expense data for the current chat."""
    return get_chat_data(chat_id)['ledger']

# --- Helper functions (Updated to require chat_id) ---
def add_expense(chat_id, payer, payee, amount, desc):
    ledger = get_current_ledger(chat_id)
    
    # Ensure nested dictionaries exist
    ledger.setdefault(payer, {})
    ledger.setdefault(payee, {})
    
    # Record the positive transaction (payer paid)
    ledger[payer].setdefault(payee, []).append({'amount': amount, 'desc': desc})
    # Record the negative transaction (payee owes, represented as -amount relative to payer)
    ledger[payee].setdefault(payer, []).append({'amount': -amount, 'desc': desc})
    
    save_all_ledgers(all_ledgers) # Save after every change

def format_ledger(chat_id):
    ledger = get_current_ledger(chat_id)
    USERS = get_current_users(chat_id)
    lines = ["🍓 *KrispyLedger Dashboard*\n"]
    any_entries = False
    balances = {}
    
    if not USERS:
        lines.append("❌ Users not set. Type /start.")
        return "\n".join(lines)
        
    for u1 in USERS:
        for u2 in USERS:
            if u1 == u2: continue
            
            if u1 in ledger and u2 in ledger[u1]:
                # Calculate the net balance between u1 and u2 (u1's perspective)
                total = sum(entry['amount'] for entry in ledger[u1][u2])
                
                # Check if u2 owes u1 (u1's balance is positive)
                if total > 0:
                    # Only show the debt once (e.g., Payee owes Payer, not Payer is owed by Payee)
                    if (u2, u1) not in balances:
                        balances[(u1, u2)] = total
                        
    if balances:
        any_entries = True
        for (payer, payee), total in balances.items():
            lines.append(f"💰 *{payee}* owes *{payer}*: ${total:.2f}")
            if payer in ledger and payee in ledger[payer]:
                 for entry in ledger[payer][payee]:
                     if entry['amount'] > 0:
                         lines.append(f"   - _{entry['desc']}_ : ${entry['amount']:.2f}")

    if not any_entries:
        lines.append("✨ No balances yet!")
    return "\n".join(lines)


def format_dashboard(chat_id):
    """Summary of net totals per user."""
    ledger = get_current_ledger(chat_id)
    USERS = get_current_users(chat_id)
    lines = ["📊 *KrispyLedger Summary*\n"]
    
    if not ledger or not USERS:
        lines.append("✨ No balances yet!")
        return "\n".join(lines)
    
    totals = {}
    for user in USERS:
        net_balance = 0
        if user in ledger:
            for payee, entries in ledger[user].items():
                net_balance += sum(entry['amount'] for entry in entries)
        totals[user] = net_balance

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

# --- Start command (Initial setup) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)
    
    # 1. Dynamic User Detection - Only set up if users are NOT already in the data structure
    if not chat_data['users']:
        user1 = update.effective_user.first_name or "User A"
        
        # Initialize with two users, requiring the second one to be renamed
        user2 = "Partner" 
        chat_data['users'] = [user1, user2]
        
        message_text = (
            f"👋 Welcome! Tracking expenses between *{user1}* and *{user2}*.\n\n"
            f"Use the command `/rename Partner YourPartnerName` to set the correct name!"
        )

        save_all_ledgers(all_ledgers)
        await update.message.reply_text(message_text, parse_mode="Markdown")
    
    sticker_file_id = "CAACAgUAAxkBAANKaPYBrywD5hefpEij_UAdhoBzBlYAApIZAAIzVrBXhicq0dBBHfo2BA"
    await update.message.reply_sticker(sticker_file_id)
    await update.message.reply_text(
        "🌸 Welcome to KrispyLedger! Tap a button to start:",
        reply_markup=main_menu_keyboard()
    )

# --- Rename command (for initial setup or fixing typos) ---
async def rename_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)
    USERS = chat_data['users']
    
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/rename OldName NewName`. Example: `/rename Partner John`")
        return
    
    old_name = context.args[0]
    new_name = context.args[1]
    
    if old_name not in USERS:
        await update.message.reply_text(f"User *{old_name}* not found in the list: {', '.join(USERS)}", parse_mode="Markdown")
        return
    if new_name in USERS:
        await update.message.reply_text(f"User *{new_name}* already exists.", parse_mode="Markdown")
        return
    
    try:
        # 1. Update name in the users list
        index = USERS.index(old_name)
        USERS[index] = new_name
        
        # 2. Update names in the ledger keys (Critical for data integrity)
        ledger = chat_data['ledger']
        # If the old name was a payer/payee key
        if old_name in ledger:
            ledger[new_name] = ledger.pop(old_name)
        # Check all other keys where old_name might be a nested key (payee)
        for payer, debts in ledger.items():
            if old_name in debts:
                debts[new_name] = debts.pop(old_name)
        
        save_all_ledgers(all_ledgers)
        await update.message.reply_text(f"✅ Renamed *{old_name}* to *{new_name}*. Current users: {', '.join(USERS)}", parse_mode="Markdown", reply_markup=main_menu_keyboard())

    except Exception as e:
        logger.error(f"Rename error: {e}")
        await update.message.reply_text("⚠️ An error occurred during rename. Please try again.")


# --- Command to ADD a new user ---
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)
    USERS = chat_data['users']
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/adduser NewName`. Example: `/adduser Mike`")
        return
    
    new_name = context.args[0].strip()
    
    if not new_name:
        await update.message.reply_text("User name cannot be empty.")
        return

    if new_name in USERS:
        await update.message.reply_text(f"User *{new_name}* already exists.", parse_mode="Markdown")
        return
    
    USERS.append(new_name)
    save_all_ledgers(all_ledgers)
    
    await update.message.reply_text(
        f"✅ User *{new_name}* added! Current users: {', '.join(USERS)}", 
        parse_mode="Markdown", 
        reply_markup=main_menu_keyboard()
    )

# --- Command to DELETE a user ---
async def delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(chat_id)
    USERS = chat_data['users']
    ledger = chat_data['ledger']
    
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/deluser UserName`. Example: `/deluser Friend`")
        return
    
    del_name = context.args[0].strip()
    
    if del_name not in USERS:
        await update.message.reply_text(f"User *{del_name}* not found in the list: {', '.join(USERS)}", parse_mode="Markdown")
        return

    if len(USERS) <= 2:
        await update.message.reply_text("⚠️ Cannot delete. You must keep at least two users for expense tracking.", parse_mode="Markdown")
        return

    # 1. Remove from USERS list
    USERS.remove(del_name)
    
    # 2. Clean up ledger (critical step!)
    # Remove all ledger entries related to the deleted user
    
    # Remove del_name's key (as a payer)
    if del_name in ledger:
        del ledger[del_name]
        
    # Remove del_name as a payee from everyone else's ledger
    for payer in list(ledger.keys()): # Iterate over a copy in case keys change
        if del_name in ledger[payer]:
            del ledger[payer][del_name]
        # Clean up any empty entries after deletion
        if not ledger[payer]:
            del ledger[payer]

    save_all_ledgers(all_ledgers)
    
    await update.message.reply_text(
        f"🗑️ User *{del_name}* deleted and all related debt records cleared. Current users: {', '.join(USERS)}", 
        parse_mode="Markdown", 
        reply_markup=main_menu_keyboard()
    )


# --- CallbackQuery handler ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    
    USERS = get_current_users(chat_id)
    
    if not USERS or len(USERS) < 2:
        await query.edit_message_text("❌ Please run /start first to set up two users.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    if data == "add_expense":
        # Payer choice keyboard uses all current USERS
        keyboard = [[InlineKeyboardButton(user, callback_data=f"payer_{user}")] for user in USERS]
        await query.edit_message_text("🧐 Who paid?", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING_PAYER

    elif data == "view_ledger":
        await query.edit_message_text(format_ledger(chat_id), parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data == "settle":
        get_chat_data(chat_id)['ledger'].clear()
        save_all_ledgers(all_ledgers)
        
        sticker_file_id = "CAACAgUAAxkBAANLaPYBv0rdel-B2DWPXw9fzsYEneEAApUZAAIzVrBX4g5-PwqYYwE2BA"
        await query.message.reply_sticker(sticker_file_id)
        await query.edit_message_text("🍰 All balances cleared!", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data == "summary":
        await query.edit_message_text(format_dashboard(chat_id), parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data.startswith("payer_"):
        payer = data.split("_")[1]
        context.user_data['payer'] = payer
        # Payee choice keyboard uses all current USERS except the payer
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
        if update.message is None:
            return TYPING_AMOUNT 

        amount = float(update.message.text)
        if amount <= 0:
            await update.message.reply_text("⚠️ Amount must be greater than zero.")
            return TYPING_AMOUNT

    except ValueError:
        await update.message.reply_text("⚠️ Enter a valid number (e.g., 15.50).")
        return TYPING_AMOUNT
    except Exception:
        await update.message.reply_text("⚠️ Please type a number for the amount.")
        return TYPING_AMOUNT

    context.user_data['amount'] = amount
    await update.message.reply_text("📝 Enter a description:")
    return TYPING_DESC

# --- Description handler ---
async def desc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    desc = update.message.text
    payer = context.user_data['payer']
    payee = context.user_data['payee']
    amount = context.user_data['amount']

    add_expense(chat_id, payer, payee, amount, desc)

    sticker_file_id = "CAACAgUAAxkBAANZaPYFMY2-hhDFqWMrxJH3sAijDSQAAqIZAAIzVrBXQRy_bzCSPF02BA"
    await update.message.reply_sticker(sticker_file_id)

    await update.message.reply_text(
        f"✅ Recorded: *{payer}* paid *{payee}* ${amount:.2f} for _{desc}_\n\n" +
        format_dashboard(chat_id), 
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    context.user_data.clear()
    return ConversationHandler.END

# --- Cancel handler ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Transaction cancelled.", reply_markup=main_menu_keyboard())
    context.user_data.clear()
    return ConversationHandler.END

# --- Main ---
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable not set.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(button_handler, pattern="^(add_expense|view_ledger|settle|summary|payer_|payee_).*$")],
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
    app.add_handler(CommandHandler("rename", rename_user))
    app.add_handler(CommandHandler("adduser", add_user)) 
    app.add_handler(CommandHandler("deluser", delete_user)) 
    app.add_handler(conv)
    
    # --- Deployment Logic ---
    if WEBHOOK_URL and BOT_TOKEN:
        logger.info(f"✅ Running in WEBHOOK mode. URL: {WEBHOOK_URL} listening on port {PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN, 
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
        )
    else:
        logger.info("⚠️ Running in POLLING mode (for local development only).")
        app.run_polling(poll_interval=1)

if __name__ == "__main__":
    main()
