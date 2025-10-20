import os
import json
import logging
import asyncio
from typing import Dict, Any, Optional

# Third-party libraries
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
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
ADD_USER, ADD_EXPENSE, REMOVE_USER = range(3)

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
        # Load the credentials JSON string
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        
        # Initialize the Firebase app
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        
        # Get the Firestore client
        db = firestore.client()
        logger.info("✅ Firestore client initialized successfully.")
    except Exception as e:
        logger.error(f"❌ Error initializing Firebase: {e}")
        db = None
else:
    logger.warning("⚠️ FIREBASE_CREDENTIALS_JSON not found. Running without persistence.")

# --- Database Utility Functions ---

def get_chat_ref(chat_id: int):
    """Returns the Firestore document reference for a chat ledger."""
    if db:
        # Ledger collection name (must match Firebase security rules)
        return db.collection("krispy_ledgers").document(str(chat_id))
    return None

def get_chat_data_sync(chat_id: int) -> Dict[str, Any]:
    """
    Synchronously fetches chat data from Firestore.
    NOTE: Removed 'async' and 'await' from the function signature and body 
    to fix TypeError: object DocumentSnapshot can't be used in 'await' expression.
    """
    if not db:
        logger.warning(f"Database not initialized for chat {chat_id}. Returning default data.")
        return {"users": {}, "expenses": [], "next_expense_id": 1}

    doc_ref = get_chat_ref(chat_id)
    try:
        # IMPORTANT FIX: Remove 'await' here. doc_ref.get() is synchronous.
        doc = doc_ref.get() 
        if doc.exists:
            # Firestore stores data in a dictionary
            data = doc.to_dict()
            # Ensure required keys exist with default values
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
    """
    Synchronously saves chat data to Firestore.
    NOTE: Removed 'async' and 'await' from the function signature and body 
    to fix TypeError: object DocumentSnapshot can't be used in 'await' expression.
    """
    if not db:
        return

    doc_ref = get_chat_ref(chat_id)
    try:
        # IMPORTANT FIX: Remove 'await' here. doc_ref.set() is synchronous.
        doc_ref.set(chat_data) 
        logger.info(f"Data saved for chat {chat_id}.")
    except Exception as e:
        logger.error(f"Error saving data for chat {chat_id}: {e}")

# --- Helper Functions for Data Access ---

def get_chat_data(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    """Retrieves chat data from context or loads it from Firestore (synchronously)."""
    chat_id = context.job.chat_id if context.job else context.effective_chat.id
    if "data_loaded" not in context.chat_data:
        # Load data from Firestore synchronously on first access
        data = get_chat_data_sync(chat_id)
        context.chat_data.update(data)
        context.chat_data["data_loaded"] = True
    return context.chat_data


def save_chat_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Saves chat data back to Firestore (synchronously)."""
    chat_id = context.effective_chat.id
    # We save the entire chat_data dictionary, excluding the 'data_loaded' flag
    data_to_save = {k: v for k, v in context.chat_data.items() if k != "data_loaded"}
    save_chat_data_sync(chat_id, data_to_save)

# --- Ledger Logic Functions ---

def calculate_balances(chat_data: Dict[str, Any]) -> Dict[str, float]:
    """Calculates the net balance for each user."""
    balances = {name: 0.0 for name in chat_data["users"].keys()}

    for expense in chat_data["expenses"]:
        payer = expense["payer"]
        amount = expense["amount"]
        
        # Simple split: evenly distributed among all users (including payer)
        num_users = len(chat_data["users"])
        if num_users == 0:
            continue

        share = amount / num_users

        # Payer is credited the full amount
        balances[payer] += amount

        # Each user (including payer) is debited their share
        for user in chat_data["users"].keys():
            balances[user] -= share

    return balances

def format_balances(balances: Dict[str, float]) -> str:
    """Formats the balances into a readable string."""
    if not balances:
        return "No users or expenses yet."

    output = ["**Current Balances:**"]
    for user, balance in balances.items():
        if abs(balance) < 0.01:
            output.append(f"• {user}: Settled up.")
        elif balance > 0:
            output.append(f"• {user}: is Owed **${balance:.2f}**")
        else:
            output.append(f"• {user}: Owes **${-balance:.2f}**")
    
    # Calculate simple one-to-one settlements (optional but helpful)
    summary = simplify_settlements(balances)
    if summary:
        output.append("\n**Settlement Suggestions:**")
        output.extend(summary)

    return "\n".join(output)

def simplify_settlements(balances: Dict[str, float]) -> list[str]:
    """Generates simple settlement suggestions."""
    # Round balances to avoid floating point issues
    rounded_balances = {user: round(balance, 2) for user, balance in balances.items() if abs(balance) >= 0.01}
    
    # Separate debtors (negative balance) and creditors (positive balance)
    debtors = {user: -balance for user, balance in rounded_balances.items() if balance < 0}
    creditors = {user: balance for user, balance in rounded_balances.items() if balance > 0}
    
    suggestions = []

    # Simple greedy algorithm for settlement
    debtor_list = list(debtors.items())
    creditor_list = list(creditors.items())

    # We use indices to track progress through the sorted lists
    i, j = 0, 0 

    while i < len(debtor_list) and j < len(creditor_list):
        debtor_name, owed_amount = debtor_list[i]
        creditor_name, receives_amount = creditor_list[j]

        # Find the minimum transaction amount
        amount_to_settle = min(owed_amount, receives_amount)
        
        if amount_to_settle > 0.01:
            suggestions.append(
                f"• {debtor_name} pays {creditor_name} **${amount_to_settle:.2f}**"
            )

        # Update remaining amounts
        debtor_list[i] = (debtor_name, owed_amount - amount_to_settle)
        creditor_list[j] = (creditor_name, receives_amount - amount_to_settle)
        
        # Move to the next person if their balance is settled
        if debtor_list[i][1] < 0.01:
            i += 1  # Debtor settled their debt
        
        if creditor_list[j][1] < 0.01:
            j += 1  # Creditor received full amount

    return suggestions

# --- Command Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and initializes the ledger."""
    chat_id = update.effective_chat.id
    
    # FIX: Get data using the synchronous function inside a helper that handles context
    # We call the synchronous helper function here, which handles the initial load from Firestore.
    chat_data = get_chat_data(context) 
    
    welcome_message = (
        "👋 Welcome to **KrispyLedger**! Your quick and easy expense tracker.\n\n"
        "**Your Ledger ID:** `{}`\n\n"
        "**Available Commands:**\n"
        "• /adduser - Start adding users to the ledger.\n"
        "• /addexpense - Start adding an expense.\n"
        "• /view - View current balances and expenses.\n"
        "• /removeuser - Remove a user from the ledger.\n"
        "• /clear - Clear all users and expenses.\n"
    ).format(chat_id)

    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def view_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the current users, expenses, and balances."""
    chat_data = get_chat_data(context)
    
    if not chat_data["users"]:
        await update.message.reply_text(
            "The ledger is empty! Use /adduser to add people and /addexpense to record costs."
        )
        return

    # 1. Users List
    users_list = ", ".join(chat_data["users"].keys())
    
    # 2. Expenses List
    expense_details = []
    if chat_data["expenses"]:
        expense_details.append("**Expenses:**")
        for exp in chat_data["expenses"]:
            expense_details.append(
                f"• ID {exp['id']} | **{exp['description']}** | Paid by {exp['payer']} | ${exp['amount']:.2f}"
            )
    else:
        expense_details.append("No expenses recorded yet. Use /addexpense.")

    # 3. Balances
    balances = calculate_balances(chat_data)
    balances_text = format_balances(balances)

    response = (
        f"**Users in Ledger:** {users_list}\n\n"
        f"{'\n'.join(expense_details)}\n\n"
        f"{balances_text}"
    )

    await update.message.reply_text(response, parse_mode='Markdown')

async def clear_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears all users and expenses from the ledger."""
    chat_data = get_chat_data(context)
    chat_data["users"] = {}
    chat_data["expenses"] = []
    chat_data["next_expense_id"] = 1
    
    save_chat_data(context)
    await update.message.reply_text(
        "🗑️ Ledger cleared! All users and expenses have been removed."
    )

# --- Add User Conversation Handlers ---

async def add_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation for adding users."""
    await update.message.reply_text(
        "Please send the name of the user you want to add (e.g., Jane Doe). Send /done when finished."
    )
    return ADD_USER

async def add_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the user name and adds it to the ledger."""
    user_name = update.message.text.strip()
    chat_data = get_chat_data(context)

    if not user_name:
        await update.message.reply_text("User name cannot be empty. Please try again.")
        return ADD_USER

    if user_name in chat_data["users"]:
        await update.message.reply_text(f"User **{user_name}** is already in the ledger. Send another name or /done.", parse_mode='Markdown')
        return ADD_USER

    # Add user
    chat_data["users"][user_name] = {}  # Placeholder for future user metadata
    
    save_chat_data(context)
    await update.message.reply_text(
        f"✅ User **{user_name}** added. Send another name or /done.", parse_mode='Markdown'
    )
    return ADD_USER

async def add_user_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ends the conversation for adding users."""
    await update.message.reply_text("Finished adding users. Use /view to check your ledger.")
    return ConversationHandler.END

# --- Add Expense Conversation Handlers ---
# Context data will store: {"payer": "", "amount": 0.0, "description": ""}

async def add_expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation for adding an expense."""
    chat_data = get_chat_data(context)
    
    if not chat_data["users"]:
        await update.message.reply_text("Please add users first using /adduser before adding expenses.")
        return ConversationHandler.END

    user_names = ", ".join(chat_data["users"].keys())
    context.user_data["expense_data"] = {} # Initialize temporary storage

    await update.message.reply_text(
        f"💰 Who paid for this expense? \nAvailable users: {user_names}",
        parse_mode='Markdown'
    )
    return ADD_EXPENSE

async def add_expense_payer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the payer and prompts for the amount."""
    payer_name = update.message.text.strip()
    chat_data = get_chat_data(context)

    if payer_name not in chat_data["users"]:
        await update.message.reply_text(
            f"User **{payer_name}** not found. Please type a name from the available users, or /cancel.", 
            parse_mode='Markdown'
        )
        return ADD_EXPENSE
    
    context.user_data["expense_data"]["payer"] = payer_name
    await update.message.reply_text(
        f"How much did **{payer_name}** pay? (e.g., 15.75)", 
        parse_mode='Markdown'
    )
    return ADD_EXPENSE

async def add_expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the amount and prompts for the description."""
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            await update.message.reply_text("Amount must be a positive number. Please try again.")
            return ADD_EXPENSE
        
        context.user_data["expense_data"]["amount"] = amount
        await update.message.reply_text(
            "What was this expense for? (e.g., Groceries, Dinner, Tickets)"
        )
        return ADD_EXPENSE
        
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a number (e.g., 15.75).")
        return ADD_EXPENSE

async def add_expense_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Processes the description and saves the expense."""
    description = update.message.text.strip()
    if not description:
        await update.message.reply_text("Description cannot be empty. Please try again.")
        return ADD_EXPENSE

    expense_data = context.user_data["expense_data"]
    chat_data = get_chat_data(context)

    # Finalize expense data
    new_expense = {
        "id": chat_data["next_expense_id"],
        "payer": expense_data["payer"],
        "amount": expense_data["amount"],
        "description": description,
    }

    chat_data["expenses"].append(new_expense)
    chat_data["next_expense_id"] += 1
    
    save_chat_data(context)

    await update.message.reply_text(
        f"✅ Expense recorded! ID {new_expense['id']}: **{description}** (Paid by {new_expense['payer']} for ${new_expense['amount']:.2f}).\n\nUse /view to see the new balances.",
        parse_mode='Markdown'
    )
    
    # Clear temporary state
    context.user_data.pop("expense_data", None) 
    return ConversationHandler.END

# --- Remove User Conversation Handlers (Simplified) ---

async def remove_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation for removing a user."""
    chat_data = get_chat_data(context)
    
    if not chat_data["users"]:
        await update.message.reply_text("No users in the ledger to remove.")
        return ConversationHandler.END

    user_names = ", ".join(chat_data["users"].keys())
    await update.message.reply_text(
        f"Who do you want to remove? This will also remove any expenses they paid.\nAvailable users: {user_names}",
        parse_mode='Markdown'
    )
    return REMOVE_USER

async def remove_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Removes the specified user and their associated expenses."""
    user_to_remove = update.message.text.strip()
    chat_data = get_chat_data(context)

    if user_to_remove not in chat_data["users"]:
        await update.message.reply_text(
            f"User **{user_to_remove}** not found. Please enter an exact name or /cancel.", 
            parse_mode='Markdown'
        )
        return REMOVE_USER

    # 1. Remove user
    chat_data["users"].pop(user_to_remove)

    # 2. Remove expenses paid by this user
    original_expense_count = len(chat_data["expenses"])
    chat_data["expenses"] = [
        exp for exp in chat_data["expenses"] if exp["payer"] != user_to_remove
    ]
    expenses_removed_count = original_expense_count - len(chat_data["expenses"])

    save_chat_data(context)
    
    response = f"✅ User **{user_to_remove}** removed from the ledger."
    if expenses_removed_count > 0:
        response += f" Also removed {expenses_removed_count} expenses paid by them."

    await update.message.reply_text(response, parse_mode='Markdown')
    return ConversationHandler.END


# --- General Conversation Handlers ---

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the current conversation."""
    # Clear any temporary user state used by the conversation
    if "expense_data" in context.user_data:
        context.user_data.pop("expense_data")

    await update.message.reply_text(
        "Operation cancelled. Use /view to check your ledger."
    )
    return ConversationHandler.END


async def handle_error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs Errors caused by Updates."""
    logger.error(f"Update {update} caused error {context.error}")
    # Optional: send a user-friendly message
    if update.effective_message:
        await update.effective_message.reply_text(
            "Oops! An internal error occurred. Please try the command again or use /start."
        )

# --- Main Application Logic ---

def main() -> None:
    """Start the bot."""
    if not BOT_TOKEN or not WEBHOOK_URL:
        logger.error("❌ BOT_TOKEN or WEBHOOK_URL not set in environment variables.")
        return

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    # --- Conversation Handlers ---
    add_user_handler = ConversationHandler(
        entry_points=[CommandHandler("adduser", add_user_start)],
        states={
            ADD_USER: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, add_user_name
                ),
                CommandHandler("done", add_user_done),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    add_expense_handler = ConversationHandler(
        entry_points=[CommandHandler("addexpense", add_expense_start)],
        states={
            ADD_EXPENSE: [
                # Payer
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, add_expense_payer
                ),
                # Amount (will re-enter ADD_EXPENSE if not a number)
                MessageHandler(
                    filters.Regex(r'^\d+(\.\d{1,2})?$') & ~filters.COMMAND, add_expense_amount
                ),
                # Description
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, add_expense_description
                ),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    remove_user_handler = ConversationHandler(
        entry_points=[CommandHandler("removeuser", remove_user_start)],
        states={
            REMOVE_USER: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, remove_user_name
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )


    # --- Command Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("view", view_ledger))
    application.add_handler(CommandHandler("clear", clear_ledger))

    # --- Conversation Handlers ---
    application.add_handler(add_user_handler)
    application.add_handler(add_expense_handler)
    application.add_handler(remove_user_handler)

    # --- Error Handler ---
    application.add_error_handler(handle_error)

    # --- Start the Bot (Webhook Mode) ---

    # Extract the token from the BOT_TOKEN variable for the webhook path
    # Telegram requires the URL path to include the token for verification
    webhook_path = "/" + BOT_TOKEN 
    
    # Check if the URL already has a trailing slash. If it does, we strip it.
    if WEBHOOK_URL.endswith('/'):
        WEBHOOK_URL = WEBHOOK_URL.rstrip('/')

    # This is the full URL Telegram needs to send updates to:
    full_webhook_url = WEBHOOK_URL + webhook_path
    
    # We must explicitly set the webhook to the full URL path
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=full_webhook_url,
    )
    logger.info(f"✅ Running in WEBHOOK mode. URL: {full_webhook_url} listening on port {PORT}")


if __name__ == "__main__":
    main()
