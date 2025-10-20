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

# --- FIRESTORE DATABASE IMPORTS ---
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import Client # For type hinting

# --- Configuration for Deployment ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PORT = int(os.environ.get('PORT', 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

# --- FIRESTORE SETUP ---
# Environment variable for Firebase credentials JSON (e.g., 'FIREBASE_CREDENTIALS')
FIREBASE_CONFIG_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON")

# Global Firestore client and collection reference
db: Client = None
LEDGERS_COLLECTION = 'krispy_ledgers' # Collection to store all chat ledgers

def initialize_firestore():
    """Initializes Firebase Admin SDK using credentials from environment variable."""
    global db
    if db is not None:
        return # Already initialized

    if not FIREBASE_CONFIG_JSON:
        logging.error("FIREBASE_CREDENTIALS_JSON environment variable not set. Persistence will fail.")
        raise EnvironmentError("Firebase credentials not configured.")

    try:
        # Load credentials from JSON string
        cred_dict = json.loads(FIREBASE_CONFIG_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("✅ Firestore client initialized successfully.")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Firebase/Firestore: {e}")
        raise

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- States ---
CHOOSING_PAYER, CHOOSING_PAYEE, TYPING_AMOUNT, TYPING_DESC = range(4)

# --- Database Accessors (ASYNC for Firestore) ---

async def get_chat_data_async(chat_id):
    """
    Retrieves the data structure for the specific chat from Firestore, 
    initializing if necessary.
    """
    chat_id_str = str(chat_id)
    doc_ref = db.collection(LEDGERS_COLLECTION).document(chat_id_str)
    
    # Attempt to get the document
    doc = await doc_ref.get()

    if doc.exists:
        chat_data = doc.to_dict()
    else:
        # Initialize default structure if not found
        chat_data = {'ledger': {}, 'users': []}
        # Save the initial structure to Firestore immediately
        await doc_ref.set(chat_data)
        logger.info(f"Initialized new ledger for chat ID: {chat_id_str}")

    return chat_data

async def save_chat_data_async(chat_id, data):
    """Saves the entire chat data dictionary back to Firestore."""
    chat_id_str = str(chat_id)
    doc_ref = db.collection(LEDGERS_COLLECTION).document(chat_id_str)
    try:
        await doc_ref.set(data)
    except Exception as e:
        logger.error(f"Error saving data for chat {chat_id_str}: {e}")

# Note: We must fetch the data before modifying it and then save it.
# The previous global 'all_ledgers' and synchronous functions are replaced.

# --- Helper functions (Updated to be ASYNC and use Firestore) ---
async def add_expense(chat_id, payer, payee, amount, desc):
    chat_data = await get_chat_data_async(chat_id)
    ledger = chat_data['ledger']
    
    # Ensure nested dictionaries exist
    ledger.setdefault(payer, {})
    ledger.setdefault(payee, {})
    
    # Record the positive transaction (payer paid)
    ledger[payer].setdefault(payee, []).append({'amount': amount, 'desc': desc})
    # Record the negative transaction (payee owes, represented as -amount relative to payer)
    ledger[payee].setdefault(payer, []).append({'amount': -amount, 'desc': desc})
    
    await save_chat_data_async(chat_id, chat_data) # Save after every change

async def format_ledger(chat_id):
    chat_data = await get_chat_data_async(chat_id)
    ledger = chat_data['ledger']
    USERS = chat_data['users']
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


async def format_dashboard(chat_id):
    """Summary of net totals per user."""
    chat_data = await get_chat_data_async(chat_id)
    ledger = chat_data['ledger']
    USERS = chat_data['users']
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
    
    # Fetch data from Firestore
    chat_data = await get_chat_data_async(chat_id)
    
    # 1. Dynamic User Detection - Only set up if users are NOT already in the data structure
    if not chat_data['users']:
        user1 = update.effective_user.first_name or "User A"
        
        # Initialize with two users, requiring the second one to be renamed
        user2 = "Partner" 
        chat_data['users'] = [user1, user2]
        
        # Save the new initial data structure to Firestore
        await save_chat_data_async(chat_id, chat_data)

        message_text = (
            f"👋 Welcome! Tracking expenses between *{user1}* and *{user2}*.\n\n"
            f"Use the command `/rename Partner YourPartnerName` to set the correct name!"
        )

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
    chat_data = await get_chat_data_async(chat_id)
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
        
        await save_chat_data_async(chat_id, chat_data)
        await update.message.reply_text(f"✅ Renamed *{old_name}* to *{new_name}*. Current users: {', '.join(USERS)}", parse_mode="Markdown", reply_markup=main_menu_keyboard())

    except Exception as e:
        logger.error(f"Rename error: {e}")
        await update.message.reply_text("⚠️ An error occurred during rename. Please try again.")


# --- Command to ADD a new user ---
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_data = await get_chat_data_async(chat_id)
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
    await save_chat_data_async(chat_id, chat_data)
    
    await update.message.reply_text(
        f"✅ User *{new_name}* added! Current users: {', '.join(USERS)}", 
        parse_mode="Markdown", 
        reply_markup=main_menu_keyboard()
    )

# --- Command to DELETE a user ---
async def delete_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_data = await get_chat_data_async(chat_id)
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

    await save_chat_data_async(chat_id, chat_data)
    
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
    
    chat_data = await get_chat_data_async(chat_id)
    USERS = chat_data['users']
    
    if not USERS or len(USERS) < 2:
        # Note: If users are missing, we should ask them to restart
        await query.edit_message_text("❌ Users are not set. Please send /start to initialize your ledger.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    if data == "add_expense":
        # Payer choice keyboard uses all current USERS
        keyboard = [[InlineKeyboardButton(user, callback_data=f"payer_{user}")] for user in USERS]
        await query.edit_message_text("🧐 Who paid?", reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING_PAYER

    elif data == "view_ledger":
        ledger_text = await format_ledger(chat_id)
        await query.edit_message_text(ledger_text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data == "settle":
        chat_data['ledger'].clear()
        await save_chat_data_async(chat_id, chat_data)
        
        sticker_file_id = "CAACAgUAAxkBAANLaPYBv0rdel-B2DWPXw9fzsYEneEAApUZAAIzVrBX4g5-PwqYYwE2BA"
        await query.message.reply_sticker(sticker_file_id)
        await query.edit_message_text("🍰 All balances cleared!", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data == "summary":
        dashboard_text = await format_dashboard(chat_id)
        await query.edit_message_text(dashboard_text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
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

    # New async function call
    await add_expense(chat_id, payer, payee, amount, desc)

    sticker_file_id = "CAACAgUAAxkBAANZaPYFMY2-hhDFqWMrxJH3sAijDSQAAqIZAAIzVrBXQRy_bzCSPF02BA"
    await update.message.reply_sticker(sticker_file_id)
    
    # New async function call
    dashboard_text = await format_dashboard(chat_id)

    await update.message.reply_text(
        f"✅ Recorded: *{payer}* paid *{payee}* ${amount:.2f} for _{desc}_\n\n" +
        dashboard_text, 
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

    # Initialize Firestore BEFORE building the application
    try:
        initialize_firestore()
    except Exception:
        logger.error("Application shutdown due to missing/invalid Firebase configuration.")
        return # Halt execution if database fails to initialize

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
            url_path="webhook", 
            webhook_url=f"{WEBHOOK_URL}/webhook"
        )
    else:
        logger.info("⚠️ Running in POLLING mode (for local development only).")
        app.run_polling(poll_interval=1)

if __name__ == "__main__":
    main()
