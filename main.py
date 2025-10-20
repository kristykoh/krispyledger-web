import os
import json
import logging
import asyncio 
from typing import Dict, Any, Optional

# Third-party libraries
from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

# Firebase Admin SDK imports
import firebase_admin
from firebase_admin import credentials, firestore

# --- Setup Logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- Conversation States ---
# States for user management (still text-based)
ADD_USER, REMOVE_USER = range(2) 
# States for the new, button-driven expense conversation
CHOOSING_PAYER, CHOOSING_PAYEE, TYPING_AMOUNT, TYPING_DESC = range(2, 6)

# --- UI Assets and Helpers ---
START_STICKER_ID = "CAACAgUAAxkBAANKaPYBrywD5hefpEij_UAdhoBzBlYAApIZAAIzVrBXhicq0dBBHfo2BA"
SETTLE_STICKER_ID = "CAACAgUAAxkBAANLaPYBv0rdel-B2DWPXw9fzsYEneEAApUZAAIzVrBX4g5-PwqYYwE2BA"
EXPENSE_STICKER_ID = "CAACAgUAAxkBAANZaPYFMY2-hhDFqWMrxJH3sAijDSQAAqIZAAIzVrBXQRy_bzCSPF02BA"

def main_menu_keyboard():
    """Main menu buttons."""
    keyboard = [
        [InlineKeyboardButton("➕ Add Expense", callback_data="add_expense")],
        [
            InlineKeyboardButton("📜 View Balances", callback_data="view_summary"),
            InlineKeyboardButton("🍰 Settle Up", callback_data="settle")
        ],
        [
            InlineKeyboardButton("🧾 View Expenses Log", callback_data="view_expenses_log"),
            InlineKeyboardButton("⚙️ Manage Users", callback_data="manage_users")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- Firestore Initialization ---

# Check for environment variables
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get('PORT', 8080))

# Initialize Firestore
db = None
if FIREBASE_CREDENTIALS_JSON:
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        logger.info("✅ Firestore client initialized successfully.")
    except Exception as e:
        logger.error(f"❌ Error initializing Firebase: {e}")
        db = None
else:
    logger.warning("⚠️ FIREBASE_CREDENTIALS_JSON not found. Running without persistence.")

# --- Database Utility Functions (Synchronous) ---
def get_chat_ref(chat_id: int):
    """Returns the Firestore document reference for a chat ledger."""
    if db:
        return db.collection("krispy_ledgers").document(str(chat_id))
    return None

def get_chat_data_sync(chat_id: int) -> Dict[str, Any]:
    """Synchronously fetches chat data from Firestore."""
    if not db:
        logger.warning(f"Database not initialized for chat {chat_id}. Returning default data.")
        return {"users": {}, "expenses": [], "next_expense_id": 1}

    doc_ref = get_chat_ref(chat_id)
    try:
        doc = doc_ref.get() 
        if doc.exists:
            data = doc.to_dict()
            return {
                "users": data.get("users", {}),
                "expenses": data.get("expenses", []),
                "next_expense_id": data.get("next_expense_id", 1),
            }
        else:
            logger.info(f"No ledger found for chat {chat_id}. Initializing new ledger.")
            return {"users": {}, "expenses": [], "next_expense_id": 1}
    except Exception as e:
        logger.error(f"Error fetching data for chat {chat_id}: {e}")
        return {"users": {}, "expenses": [], "next_expense_id": 1}


def save_chat_data_sync(chat_id: int, chat_data: Dict[str, Any]) -> None:
    """Synchronously saves chat data to Firestore."""
    if not db:
        return

    doc_ref = get_chat_ref(chat_id)
    try:
        doc_ref.set(chat_data) 
        logger.info(f"Data saved for chat {chat_id}.")
    except Exception as e:
        logger.error(f"Error saving data for chat {chat_id}: {e}")

# --- Helper Functions for Data Access (Asynchronous) ---

async def load_chat_data_async(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Loads chat data from Firestore into context.chat_data."""
    app_loop = asyncio.get_running_loop() 

    if "data_loaded" not in context.chat_data:
        logger.info(f"Asynchronously loading data for chat {chat_id}")
        data = await app_loop.run_in_executor(None, get_chat_data_sync, chat_id)
        
        context.chat_data.update(data)
        context.chat_data["data_loaded"] = True

def get_chat_data(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    """Retrieves chat data from context.chat_data."""
    if "users" not in context.chat_data:
         logger.error("🚨 get_chat_data called before data was loaded asynchronously.")
         return {"users": {}, "expenses": [], "next_expense_id": 1}
    
    return context.chat_data

async def save_chat_data_async(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saves chat data back to Firestore."""
    app_loop = asyncio.get_running_loop()
    data_to_save = {k: v for k, v in context.chat_data.items() if k != "data_loaded"}
    await app_loop.run_in_executor(None, save_chat_data_sync, chat_id, data_to_save)


# --- Ledger Logic Functions (Same as before) ---
def calculate_balances(chat_data: Dict[str, Any]) -> Dict[str, float]:
    """Calculates the net balance for each user."""
    balances = {name: 0.0 for name in chat_data["users"].keys()}

    for expense in chat_data["expenses"]:
        payer = expense["payer"]
        amount = expense["amount"]
        
        num_users = len(chat_data["users"])
        if num_users == 0: continue

        share = amount / num_users

        balances[payer] += amount

        for user in chat_data["users"].keys():
            balances[user] -= share

    return balances

def format_balances(balances: Dict[str, float]) -> str:
    """Formats the balances into a readable string."""
    if not balances:
        return "No users or expenses yet."

    output = ["**Current Balances:**"]
    for user, balance in balances.items():
        balance = round(balance, 2)
        if abs(balance) < 0.01:
            output.append(f"• {user}: Settled up. ✅")
        elif balance > 0:
            output.append(f"• {user}: is Owed **${balance:.2f}** 💸")
        else:
            output.append(f"• {user}: Owes **${-balance:.2f}** 💰")
    
    summary = simplify_settlements(balances)
    if summary:
        output.append("\n**Settlement Suggestions:**")
        output.extend(summary)

    return "\n".join(output)

def simplify_settlements(balances: Dict[str, float]) -> list[str]:
    """Generates simple settlement suggestions."""
    rounded_balances = {user: round(balance, 2) for user, balance in balances.items() if abs(balance) >= 0.01}
    
    debtors = {user: -balance for user, balance in rounded_balances.items() if balance < 0}
    creditors = {user: balance for user, balance in rounded_balances.items() if balance > 0}
    
    suggestions = []
    debtor_list = list(debtors.items())
    creditor_list = list(creditors.items())
    i, j = 0, 0 

    while i < len(debtor_list) and j < len(creditor_list):
        debtor_name, owed_amount = debtor_list[i]
        creditor_name, receives_amount = creditor_list[j]

        amount_to_settle = min(owed_amount, receives_amount)
        
        if amount_to_settle > 0.01:
            suggestions.append(
                f"• {debtor_name} pays {creditor_name} **${amount_to_settle:.2f}**"
            )

        debtor_list[i] = (debtor_name, owed_amount - amount_to_settle)
        creditor_list[j] = (creditor_name, receives_amount - amount_to_settle)
        
        if debtor_list[i][1] < 0.01: i += 1
        if creditor_list[j][1] < 0.01: j += 1

    return suggestions

# --- UI Content Generation ---

async def get_summary_text(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Generates the text for the View Balances button."""
    await load_chat_data_async(chat_id, context)
    chat_data = get_chat_data(context)
    
    if not chat_data["users"]:
        return "The ledger is empty! Use 'Manage Users' to add people."

    users_list = ", ".join(chat_data["users"].keys())
    balances = calculate_balances(chat_data)
    balances_text = format_balances(balances)

    return (
        f"**👥 Users in Ledger:** {users_list}\n\n"
        f"{balances_text}"
    )

async def get_expenses_log_text(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Generates the detailed log of expenses."""
    await load_chat_data_async(chat_id, context)
    chat_data = get_chat_data(context)
    
    expense_details = []
    if chat_data["expenses"]:
        expense_details.append("**Expenses Log:**")
        for exp in chat_data["expenses"]:
            expense_details.append(
                f"• ID {exp['id']} | Paid by **{exp['payer']}** | ${exp['amount']:.2f} for *{exp['description']}*"
            )
    else:
        expense_details.append("No expenses recorded yet.")
    
    return "\n".join(expense_details)

# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message with the main menu."""
    chat_id = update.effective_chat.id
    await load_chat_data_async(chat_id, context)
    
    welcome_message = "🌸 Welcome to **KrispyLedger**! Your quick and easy expense tracker. Tap a button to start:"
    
    # Send sticker first, then the menu message
    await update.message.reply_sticker(START_STICKER_ID)
    await update.message.reply_text(
        welcome_message, 
        parse_mode='Markdown', 
        reply_markup=main_menu_keyboard()
    )

async def clear_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears all users and expenses from the ledger (Used if /clear is typed)."""
    chat_id = update.effective_chat.id
    await load_chat_data_async(chat_id, context)
    chat_data = get_chat_data(context)
    
    chat_data["users"] = {}
    chat_data["expenses"] = []
    chat_data["next_expense_id"] = 1
    
    await save_chat_data_async(chat_id, context)
    await update.message.reply_sticker(SETTLE_STICKER_ID)
    await update.message.reply_text(
        "🗑️ Ledger cleared! All balances, users, and expenses have been removed.",
        reply_markup=main_menu_keyboard()
    )

# --- Conversation Entry Point (for /addexpense command) ---

async def start_add_expense_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the expense conversation when the user types /addexpense."""
    chat_id = update.effective_chat.id
    await load_chat_data_async(chat_id, context)
    chat_data = get_chat_data(context)
    users = list(chat_data["users"].keys())
    
    if not users:
        await update.message.reply_text(
            "❌ Please add users first using /adduser (via text command).", 
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    context.user_data['expense_data'] = {} 
    keyboard = [[InlineKeyboardButton(user, callback_data=f"payer_{user}")] for user in users]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")])
    
    # Send a new message to start the flow
    await update.message.reply_text("🧐 Who paid for the expense?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_PAYER


# --- Main Callback Query Handler ---

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all Inline Keyboard button presses."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    
    # --- Main Menu Handling (Conversation Entry/Exit) ---
    if data == "add_expense":
        # Simulate pressing the command
        return await start_add_expense_command(update, context)

    elif data == "view_summary":
        summary_text = await get_summary_text(chat_id, context)
        await query.edit_message_text(summary_text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
        
    elif data == "view_expenses_log":
        log_text = await get_expenses_log_text(chat_id, context)
        await query.edit_message_text(log_text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    elif data == "manage_users":
        await query.edit_message_text(
            "👥 **User Management**\n\nUse the following *text commands* (type them in the input bar) to manage users:\n"
            "• /adduser - Start adding users\n"
            "• /removeuser - Start removing users\n\n"
            "_Note: This requires typing names exactly._",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END
        
    elif data == "settle":
        # Clear all data
        await load_chat_data_async(chat_id, context)
        chat_data = get_chat_data(context)
        chat_data["users"] = {}
        chat_data["expenses"] = []
        chat_data["next_expense_id"] = 1
        await save_chat_data_async(chat_id, context)
        
        await query.message.reply_sticker(SETTLE_STICKER_ID)
        await query.edit_message_text("🍰 All balances cleared and ledger reset!", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
        
    elif data == "menu":
        await query.edit_message_text("🌸 Main menu:", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
        
    # --- Conversation State Transitions for ADD_EXPENSE (only valid during conversation) ---
    
    # CHOOSING_PAYER -> CHOOSING_PAYEE
    if data.startswith("payer_"):
        payer = data.split("_")[1]
        context.user_data['expense_data']['payer'] = payer
        
        chat_data = get_chat_data(context)
        users = list(chat_data["users"].keys())
        
        # Payee selection logic (only available users who aren't the payer)
        keyboard = [[InlineKeyboardButton(u, callback_data=f"payee_{u}")] for u in users if u != payer]
        keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu")])
        
        await query.edit_message_text(
            f"💰 Payer: **{payer}**\n\nWho owes the payer? (For a simple split, select the other person)",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return CHOOSING_PAYEE
        
    # CHOOSING_PAYEE -> TYPING_AMOUNT
    elif data.startswith("payee_"):
        payee = data.split("_")[1]
        
        # NOTE: We ignore the 'payee' in the final transaction logic since we use a general split
        # but we keep it here to follow the user's intended flow (if they want 1:1 splits later)
        context.user_data['expense_data']['payee'] = payee 
        
        # Remove the keyboard so the user can type the amount
        await query.edit_message_text(
            f"👤 Payer: **{context.user_data['expense_data']['payer']}**\n\n_Please send the **total amount** (e.g., 15.75) as a regular message._",
            parse_mode="Markdown"
        )
        return TYPING_AMOUNT

    return ConversationHandler.END


# --- Text Message Handlers (TYPING_AMOUNT / TYPING_DESC) ---

async def amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user typing the amount."""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Invalid amount. Please enter a positive number (e.g., 15.75).")
        return TYPING_AMOUNT
        
    context.user_data['expense_data']['amount'] = amount
    
    await update.message.reply_text("📝 Now, please send a description for this expense (e.g., Dinner):")
    return TYPING_DESC

async def desc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user typing the description and finalizes the expense."""
    chat_id = update.effective_chat.id
    await load_chat_data_async(chat_id, context)
    chat_data = get_chat_data(context)
    
    description = update.message.text.strip()
    if not description:
        await update.message.reply_text("Description cannot be empty. Please try again.")
        return TYPING_DESC

    expense_data = context.user_data["expense_data"]
    
    # Finalize expense data and use the general Splitwise model (split among all users)
    new_expense = {
        "id": chat_data["next_expense_id"],
        "payer": expense_data["payer"],
        "amount": expense_data["amount"],
        "description": description,
    }

    chat_data["expenses"].append(new_expense)
    chat_data["next_expense_id"] += 1
    
    await save_chat_data_async(chat_id, context)
    
    await update.message.reply_sticker(EXPENSE_STICKER_ID)
    summary_text = await get_summary_text(chat_id, context)
    
    await update.message.reply_text(
        f"✅ Expense recorded! ID {new_expense['id']}: **{description}** (Paid by {new_expense['payer']} for ${new_expense['amount']:.2f}).\n\n"
        f"{summary_text}",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    
    context.user_data.pop("expense_data", None) 
    return ConversationHandler.END

# --- User Management Handlers (Modified for UI consistency) ---

async def add_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation for adding users."""
    chat_id = update.effective_chat.id
    await load_chat_data_async(chat_id, context)
    # Ensure ReplyKeyboardRemove is used to clear any lingering keyboards
    await update.message.reply_text(
        "Please send the name of the user you want to add (e.g., Jane Doe). Send /done when finished.",
        reply_markup=ReplyKeyboardRemove() 
    )
    return ADD_USER

async def add_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the user name and adds it to the ledger."""
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(context)

    user_name = update.message.text.strip()

    if not user_name:
        await update.message.reply_text("User name cannot be empty. Please try again.")
        return ADD_USER

    if user_name in chat_data["users"]:
        await update.message.reply_text(f"User **{user_name}** is already in the ledger. Send another name or /done.", parse_mode='Markdown')
        return ADD_USER

    chat_data["users"][user_name] = {}
    
    await save_chat_data_async(chat_id, context)
    await update.message.reply_text(
        f"✅ User **{user_name}** added. Send another name or /done.", parse_mode='Markdown'
    )
    return ADD_USER

async def add_user_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ends the conversation for adding users and returns to the main menu."""
    await update.message.reply_text("Finished adding users. Returning to main menu.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def remove_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation for removing a user."""
    chat_id = update.effective_chat.id
    await load_chat_data_async(chat_id, context)
    chat_data = get_chat_data(context)
    
    if not chat_data["users"]:
        await update.message.reply_text("No users in the ledger to remove.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END

    user_names = ", ".join(chat_data["users"].keys())
    await update.message.reply_text(
        f"Who do you want to remove? This will also remove any expenses they paid.\nAvailable users: {user_names}\n\n_Please reply with the exact name._",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardRemove() # Remove keyboard for text entry
    )
    return REMOVE_USER

async def remove_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Removes the specified user and their associated expenses."""
    chat_id = update.effective_chat.id
    chat_data = get_chat_data(context)

    user_to_remove = update.message.text.strip()

    if user_to_remove not in chat_data["users"]:
        await update.message.reply_text(
            f"User **{user_to_remove}** not found. Please enter an exact name or /cancel.", 
            parse_mode='Markdown'
        )
        return REMOVE_USER

    chat_data["users"].pop(user_to_remove)
    original_expense_count = len(chat_data["expenses"])
    chat_data["expenses"] = [
        exp for exp in chat_data["expenses"] if exp["payer"] != user_to_remove
    ]
    expenses_removed_count = original_expense_count - len(chat_data["expenses"])

    await save_chat_data_async(chat_id, context)
    
    response = f"✅ User **{user_to_remove}** removed from the ledger."
    if expenses_removed_count > 0:
        response += f" Also removed {expenses_removed_count} expenses paid by them."

    await update.message.reply_text(response, parse_mode='Markdown', reply_markup=main_menu_keyboard())
    return ConversationHandler.END


# --- General Conversation Handlers ---

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the current conversation."""
    if "expense_data" in context.user_data:
        context.user_data.pop("expense_data")
        
    # Check if we need to reply to a message or edit a callback query
    if update.message:
        await update.message.reply_text("❌ Operation cancelled.", reply_markup=main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text("❌ Operation cancelled.", reply_markup=main_menu_keyboard())

    return ConversationHandler.END


async def handle_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs Errors caused by Updates."""
    logger.error(f"Update {update} caused error {context.error}")
    if update.effective_message:
        await update.effective_message.reply_text(
            "Oops! An internal error occurred. Please try the command again or use /start.",
            reply_markup=main_menu_keyboard()
        )

# --- Main Application Logic ---

def main() -> None:
    """Start the bot."""
    global BOT_TOKEN, WEBHOOK_URL 

    if not BOT_TOKEN or not WEBHOOK_URL:
        logger.error("❌ BOT_TOKEN or WEBHOOK_URL not set in environment variables.")
        return

    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    # --- Conversation Handlers ---
    
    # 1. Add User Conversation (Text-based)
    add_user_handler = ConversationHandler(
        entry_points=[CommandHandler("adduser", add_user_start)],
        states={
            ADD_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_user_name),
                CommandHandler("done", add_user_done),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # 2. Remove User Conversation (Text-based)
    remove_user_handler = ConversationHandler(
        entry_points=[CommandHandler("removeuser", remove_user_start)],
        states={
            REMOVE_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, remove_user_name)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    # 3. Add Expense Conversation (Button/Text mixed)
    expense_conv_handler = ConversationHandler(
        # Entry points: Button press OR Command type
        entry_points=[
            CallbackQueryHandler(button_handler, pattern="^add_expense$"),
            CommandHandler("addexpense", start_add_expense_command)
        ],
        states={
            # Button States (Handled by the general button_handler for selection)
            CHOOSING_PAYER: [
                CallbackQueryHandler(button_handler, pattern="^payer_.*$"),
                CallbackQueryHandler(button_handler, pattern="^menu$") 
            ],
            CHOOSING_PAYEE: [
                CallbackQueryHandler(button_handler, pattern="^payee_.*$"),
                CallbackQueryHandler(button_handler, pattern="^menu$")
            ],
            # Text Input States (Handled by specific MessageHandlers)
            TYPING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, amount_handler)
            ],
            TYPING_DESC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, desc_handler)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )


    # --- Command Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("clear", clear_ledger))

    # --- Conversation Handlers ---
    application.add_handler(add_user_handler)
    application.add_handler(remove_user_handler)
    application.add_handler(expense_conv_handler)
    
    # --- General Callback Handler (for main menu buttons that don't start a conversation) ---
    application.add_handler(
        CallbackQueryHandler(button_handler, pattern="^(view_summary|settle|manage_users|menu|view_expenses_log)$")
    )

    # --- Error Handler ---
    application.add_error_handler(handle_error)

    # --- Start the Bot (Webhook Mode) ---
    webhook_path = "/" + BOT_TOKEN 
    if WEBHOOK_URL.endswith('/'): WEBHOOK_URL = WEBHOOK_URL.rstrip('/')
    full_webhook_url = WEBHOOK_URL + webhook_path
    
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
    )
    logger.info(f"✅ Running in WEBHOOK mode. URL: {full_webhook_url} listening on port {PORT}")


if __name__ == "__main__":
    main()
