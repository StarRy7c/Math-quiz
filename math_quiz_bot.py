import logging
import random
import time
import asyncio
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Game Constants ---
DEFAULT_TIMEOUT_PER_Q = 20  # seconds
POINTS_CORRECT = 100
POINTS_PER_SECOND_SPEED_BONUS_MAX = 50  # Max bonus for answering instantly
STREAK_BONUS_PER_LEVEL = 10

CHEERING_QUOTES = [
    "Victory is sweetest when you've known defeat.",
    "A champion is afraid of losing. Everyone else is afraid of winning.",
    "The harder the battle, the sweeter the victory.",
    "You are a true champion! Well played!",
    "In the arena of quiz, you are the gladiator!",
    "Your mind is a finely-tuned weapon! Congratulations!",
]

# --- Game State ---
quizzes = {}  # group_id -> quiz_data
# quiz_data structure:
# {
#     "status": str, # "configuring", "active", "stopped"
#     "host_id": int,
#     "config": {"difficulty": str, "num_questions": int},
#     "active": bool,
#     "questions_data": list_of_question_dicts,
#     "current_q_index": int,
#     "current_question_details": dict, # {'text': str, 'answer': int}
#     "q_start_time": float,
#     "first_answerer_id": int, # User ID of the person who answered correctly first
#     "scores": {user_id: {"points": int, "streak": int, "username": str}},
#     "current_question_event": asyncio.Event,
#     "setup_message_id": int,
# }


# --- Question Generation ---
def generate_math_question(difficulty: str) -> dict:
    """Generates a math question with three numbers and two operators (+, -)."""
    if difficulty == "easy":
        nums = [random.randint(1, 25) for _ in range(3)]
    elif difficulty == "medium":
        nums = [random.randint(10, 75) for _ in range(3)]
    else:  # hard
        nums = [random.randint(20, 150) for _ in range(3)]

    ops = [random.choice(['+', '-']) for _ in range(2)]
    expression = f"{nums[0]} {ops[0]} {nums[1]} {ops[1]} {nums[2]}"

    # Safely evaluate the expression
    try:
        answer = eval(expression)
    except Exception as e:
        logger.error(f"Failed to evaluate expression '{expression}': {e}")
        # Fallback to a simpler, guaranteed-to-work expression
        nums = [random.randint(10, 50) for _ in range(2)]
        expression = f"{nums[0]} + {nums[1]}"
        answer = nums[0] + nums[1]

    return {
        "text": f"What is {expression}?",
        "answer": answer,
        "type": "math_text",
        "difficulty": difficulty
    }


# --- Command Handlers ---
async def quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts the interactive quiz setup process."""
    group_id = update.effective_chat.id
    user = update.effective_user

    if group_id in quizzes and quizzes[group_id].get("active"):
        await update.message.reply_text("⏳ A quiz is already running in this group!")
        return

    # Initialize a new quiz in 'configuring' state
    quizzes[group_id] = {
        "status": "configuring",
        "host_id": user.id,
        "config": {},
        "active": False, # Not active until fully configured
    }

    keyboard = [
        [InlineKeyboardButton("😄 Easy", callback_data="config:difficulty:easy")],
        [InlineKeyboardButton("🤔 Medium", callback_data="config:difficulty:medium")],
        [InlineKeyboardButton("🤯 Hard", callback_data="config:difficulty:hard")],
        [InlineKeyboardButton("❌ Cancel", callback_data="config:cancel:setup")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    msg = await update.message.reply_text(
        f"👋 Welcome, {user.first_name}!\n\n"
        "Let's set up a new quiz. First, choose the difficulty:",
        reply_markup=reply_markup
    )
    quizzes[group_id]["setup_message_id"] = msg.message_id


async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stops an ongoing or configuring quiz."""
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    quiz = quizzes.get(group_id)

    if not quiz:
        await update.message.reply_text("There is no quiz to stop.")
        return

    is_admin = await _is_admin(update, context)
    if user_id != quiz.get("host_id") and not is_admin:
        await update.message.reply_text("Only the quiz host or a group admin can stop the quiz.")
        return

    quiz["active"] = False
    quiz["status"] = "stopped"
    if quiz.get("current_question_event"):
        quiz["current_question_event"].set()

    stopper_name = update.effective_user.first_name
    await context.bot.send_message(chat_id=group_id, text=f"🚨 Quiz stopped by {stopper_name}.")

    # If quiz was active, show final scores. Otherwise, just clean up.
    if quiz.get("scores"):
        await show_leaderboard(group_id, context, final=True)

    if group_id in quizzes:
        del quizzes[group_id]
    logger.info(f"Quiz {group_id} stopped and data cleaned up.")


# --- Callback Handler for Setup ---
async def handle_config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses during the quiz setup phase."""
    query = update.callback_query
    await query.answer()

    group_id = query.message.chat_id
    user_id = query.from_user.id
    quiz = quizzes.get(group_id)

    if not quiz or user_id != quiz.get("host_id"):
        await context.bot.send_message(
            chat_id=user_id,
            text="Only the person who started the quiz can configure it."
        )
        return

    if quiz.get("status") != "configuring":
        await query.edit_message_text("Configuration is already complete or has been cancelled.")
        return

    _, config_type, config_value = query.data.split(':')

    if config_type == "cancel":
        del quizzes[group_id]
        await query.edit_message_text("Quiz setup cancelled. 👋")
        return

    if config_type == "difficulty":
        quiz["config"]["difficulty"] = config_value
        keyboard = [
            [
                InlineKeyboardButton("5", callback_data="config:questions:5"),
                InlineKeyboardButton("10", callback_data="config:questions:10"),
                InlineKeyboardButton("15", callback_data="config:questions:15"),
                InlineKeyboardButton("20", callback_data="config:questions:20"),
            ],
            [InlineKeyboardButton("🔙 Back to Difficulty", callback_data="config:back:main")],
        ]
        await query.edit_message_text(
            f"Difficulty set to: *{config_value.capitalize()}*\n\nNow, how many questions?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

    elif config_type == "questions":
        quiz["config"]["num_questions"] = int(config_value)
        difficulty = quiz['config']['difficulty']
        num_questions = quiz['config']['num_questions']

        # Finalize setup and start the quiz
        quiz.update({
            "status": "active",
            "active": True,
            "questions_data": [generate_math_question(difficulty) for _ in range(num_questions)],
            "current_q_index": -1,
            "scores": {},
            "current_question_event": asyncio.Event(),
        })

        await query.edit_message_text(
            f"✅ *Quiz Setup Complete!*\n\n"
            f"  - *Difficulty:* {difficulty.capitalize()}\n"
            f"  - *Questions:* {num_questions}\n\n"
            "Get ready to type your answers! The first correct response wins the round.",
            parse_mode=ParseMode.MARKDOWN
        )
        
        await asyncio.sleep(3)
        context.application.create_task(run_quiz_loop(group_id, context))

    elif config_type == "back":
        # Go back to difficulty selection
        await quiz_command(query, context) # Re-trigger the initial setup message
        await query.message.delete() # Delete the old message


# --- Message Handler for Answers ---
async def handle_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming text messages to check for quiz answers."""
    group_id = update.effective_chat.id
    user_id = update.effective_user.id
    quiz = quizzes.get(group_id)

    # Filter out irrelevant messages
    if (not quiz or not quiz.get("active") or
            quiz.get("first_answerer_id") is not None or
            not quiz.get("q_start_time")):
        return

    try:
        user_answer = int(update.message.text.strip())
    except (ValueError, TypeError):
        return  # Message is not a number, so ignore it as an answer

    correct_answer = quiz["current_question_details"]["answer"]

    if user_answer == correct_answer:
        time_taken = time.time() - quiz["q_start_time"]
        quiz["first_answerer_id"] = user_id  # Lock the question!

        username = update.effective_user.first_name
        if user_id not in quiz["scores"]:
            quiz["scores"][user_id] = {"points": 0, "streak": 0, "username": username}
        quiz["scores"][user_id]["username"] = username

        # Calculate points
        time_bonus_factor = max(0, (DEFAULT_TIMEOUT_PER_Q - time_taken) / DEFAULT_TIMEOUT_PER_Q)
        speed_bonus = int(POINTS_PER_SECOND_SPEED_BONUS_MAX * time_bonus_factor)
        
        current_streak = quiz["scores"][user_id].get("streak", 0) + 1
        quiz["scores"][user_id]["streak"] = current_streak
        streak_bonus = (current_streak - 1) * STREAK_BONUS_PER_LEVEL
        
        points_earned = POINTS_CORRECT + speed_bonus + streak_bonus
        quiz["scores"][user_id]["points"] += points_earned

        # Announce winner and wake up the main loop
        await update.message.reply_text(
            f"🏆 Correct, *{username}*! You were first!\n"
            f"The answer was *{correct_answer}*.\n\n"
            f"+{points_earned} points! (Total: {quiz['scores'][user_id]['points']})",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Reset streaks for all other players
        for p_id in quiz["scores"]:
            if p_id != user_id:
                quiz["scores"][p_id]["streak"] = 0

        if quiz.get("current_question_event"):
            quiz["current_question_event"].set()


# --- Core Quiz Logic ---
async def run_quiz_loop(group_id: int, context: ContextTypes.DEFAULT_TYPE):
    """The main loop that runs the quiz, one question at a time."""
    quiz = quizzes.get(group_id)
    if not quiz: return

    num_questions = quiz["config"]["num_questions"]

    for i in range(num_questions):
        if not quiz.get("active"): break

        quiz.update({
            "current_q_index": i,
            "current_question_details": quiz["questions_data"][i],
            "first_answerer_id": None,
            "q_start_time": None,
        })
        
        question_details = quiz["current_question_details"]

        await context.bot.send_message(
            chat_id=group_id,
text=f"*--- Question {i + 1}/{num_questions} ---*\n\n*{question_details['text']}*",
            parse_mode=ParseMode.MARKDOWN
        )
        quiz["q_start_time"] = time.time()
        quiz["current_question_event"].clear()

        # Start a timeout task for the current question
        context.application.create_task(
            end_question_by_timeout(group_id, context, i, DEFAULT_TIMEOUT_PER_Q)
        )

        await quiz["current_question_event"].wait() # Wait for timeout or correct answer

        if not quiz.get("active"):
            logger.info(f"Quiz {group_id} stopped during question loop.")
            break

        if i < num_questions - 1:
            await show_leaderboard(group_id, context, mid_quiz=True)
            await context.bot.send_message(chat_id=group_id, text="Next question in 5 seconds...")
            await asyncio.sleep(5)

    if quiz.get("active"):
        quiz["active"] = False
        await context.bot.send_message(chat_id=group_id, text="🏁 Quiz Finished! 🏁")
        await show_leaderboard(group_id, context, final=True)
        if group_id in quizzes:
            del quizzes[group_id]


async def end_question_by_timeout(group_id: int, context: ContextTypes.DEFAULT_TYPE, q_index: int, timeout: int):
    """Ends the current question if the timeout is reached."""
    await asyncio.sleep(timeout)
    quiz = quizzes.get(group_id)

    # Check if this timeout is still relevant (i.e., question wasn't already answered or stopped)
    if (not quiz or not quiz.get("active") or
            quiz.get("current_q_index") != q_index or
            quiz.get("first_answerer_id") is not None):
        return

    # Reset all player streaks since no one answered
    for player_data in quiz["scores"].values():
        player_data["streak"] = 0

    await context.bot.send_message(
        chat_id=group_id,
        text=f"⏱ Time's up! No one answered correctly.\nThe correct answer was: *{quiz['current_question_details']['answer']}*",
        parse_mode=ParseMode.MARKDOWN
    )
    
    if quiz.get("current_question_event"):
        quiz["current_question_event"].set()


async def show_leaderboard(group_id: int, context: ContextTypes.DEFAULT_TYPE, mid_quiz: bool = False, final: bool = False):
    """Displays the current or final scores."""
    quiz = quizzes.get(group_id)
    if not quiz or not quiz.get("scores"):
        if final:
            await context.bot.send_message(chat_id=group_id, text="The quiz ended, but no scores were recorded!")
        return

    sorted_scores = sorted(quiz["scores"].items(), key=lambda item: item[1]["points"], reverse=True)
    title = "📊 Current Leaderboard" if mid_quiz else "🏆 Final Leaderboard"
    text = f"{title}:\n"
    placing_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

    for i, (user_id, data) in enumerate(sorted_scores[:10], 1): # Show top 10
        emoji = placing_emojis.get(i, f" {i}.")
        streak_icon = f"🔥x{data['streak']}" if data['streak'] > 1 else ""
        text += f"{emoji} *{data['username']}*: {data['points']} pts {streak_icon}\n"

    if not sorted_scores:
        text += "No scores recorded yet."
        
    await context.bot.send_message(chat_id=group_id, text=text, parse_mode=ParseMode.MARKDOWN)
    
    if final and sorted_scores:
        winner_name = sorted_scores[0][1]['username']
        cheering_quote = random.choice(CHEERING_QUOTES)
        await context.bot.send_message(
            chat_id=group_id,
            text=f"🎉 Congratulations to our grand winner, *{winner_name}*! 🎉\n\n_{cheering_quote}_",
            parse_mode=ParseMode.MARKDOWN
        )


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Helper to check if a user is an admin in the chat."""
    if update.effective_chat.type == 'private':
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
        return member.status in [member.ADMINISTRATOR, member.OWNER]
    except Exception as e:
        logger.error(f"Error checking admin status: {e}")
        return False


# --- Main Bot Setup ---
if __name__ == '__main__':
    TOKEN = "7276833801:AAEucg6nOZHCV4tDmYM8G1r0g5vjgFvcK_Y" # Replace with your bot's token

    if not TOKEN or len(TOKEN.split(':')) != 2:
        print("ERROR: Invalid Telegram Bot Token.")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("quiz", quiz_command))
    app.add_handler(CommandHandler("start", quiz_command)) # Alias for /quiz
    app.add_handler(CommandHandler("stopquiz", stop_quiz))
    app.add_handler(CallbackQueryHandler(handle_config_callback, pattern="^config:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_answer))
    
    logger.info("Bot is starting...")
    app.run_polling()
    logger.info("Bot has stopped.")
