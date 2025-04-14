import os
import json
import logging
import asyncio
from flask import Flask, send_file, jsonify
from twitchio.ext import commands
from google.oauth2.service_account import Credentials
import gspread

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Flask app setup
app = Flask(__name__)

# Content Security Policy header
CSP_HEADER = (
    "default-src 'self' https://cdn.glitch.global https://pyscript.net; "
    "script-src 'self' https://pyscript.net https://cdn.jsdelivr.net 'unsafe-eval' 'unsafe-inline'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' https://cdn.glitch.global data:; "
    "media-src https://cdn.glitch.global; "
    "connect-src 'self' https://cdn.jsdelivr.net https://pypi.org https://files.pythonhosted.org;"
)

# Load Google Sheets credentials from environment variable
try:
    logger.debug("Loading Google Sheets credentials from GOOGLE_CREDENTIALS environment variable")
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    if not creds_json:
        raise EnvironmentError("GOOGLE_CREDENTIALS environment variable not set")
    creds_dict = json.loads(creds_json)
    logger.info("Successfully loaded credentials from GOOGLE_CREDENTIALS")
except Exception as e:
    logger.error(f"Error loading Google Sheets credentials: {e}")
    raise

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
client = gspread.authorize(creds)

# Open the spreadsheet
spreadsheet = client.open("TwitchPuzzleLeaderboard")
worksheet = spreadsheet.get_worksheet(0)

# Twitch bot setup
bot = commands.Bot(
    token=os.environ['TWITCH_TOKEN'],
    client_id=os.environ['TWITCH_CLIENT_ID'],
    nick='nftopia_puzzle_bot',
    prefix='!',
    initial_channels=['nftopia']
)

# Game state
current_puzzle_index = 0
puzzle_answers = ["A1", "B2", "C3", "D4", "E5", "F6", "G7"]
current_answer = puzzle_answers[current_puzzle_index]

def update_leaderboard(guess, user):
    try:
        logger.info(f"Updating leaderboard for user: {user}")
        worksheet.append_row([user, guess, str(current_puzzle_index + 1), str(time.time())])
        logger.info("Leaderboard updated")
    except Exception as e:
        logger.error(f"Error updating leaderboard: {e}")

@app.route('/')
def serve_index():
    logger.info("Route / hit - attempting to serve index.html")
    response = send_file('index.html', mimetype='text/html')
    response.headers['Content-Security-Policy'] = CSP_HEADER
    logger.info("Successfully served index.html")
    logger.info(f"Response headers for /: {dict(response.headers)}")
    return response

@app.route('/guess/<guess>')
async def handle_guess(guess):
    global current_puzzle_index, current_answer
    logger.info(f"Received guess: {guess}")
    if guess.lower() == current_answer.lower():
        logger.info("Correct guess, advancing to next puzzle")
        current_puzzle_index += 1
        if current_puzzle_index > 6:
            current_puzzle_index = 0
        current_answer = puzzle_answers[current_puzzle_index]
        update_leaderboard(guess, "some_user")
        return jsonify({"status": "correct", "next_puzzle": f"puzzle{current_puzzle_index + 1:02d}_00000.png"})
    return jsonify({"status": "incorrect"})

@app.route('/health')
def health_check():
    return "OK", 200

@bot.event()
async def event_ready():
    logger.info("Bot connected to Twitch!")

@bot.command(name='g')
async def guess_command(ctx):
    guess = ctx.message.content.split(' ')[1] if len(ctx.message.content.split(' ')) > 1 else ''
    if not guess:
        await ctx.send("Please provide a guess! Example: !g A1")
        return
    logger.info(f"Twitch guess received: {guess} from user: {ctx.author.name}")
    if guess.lower() == current_answer.lower():
        await ctx.send(f"Correct guess by {ctx.author.name}! Moving to next puzzle.")
    else:
        await ctx.send(f"Incorrect guess by {ctx.author.name}. Try again!")

if __name__ == '__main__':
    logger.info("Main script starting")
    logger.info("Starting Flask on port 10000")
    loop = asyncio.get_event_loop()
    loop.create_task(bot.start())
    app.run(host='0.0.0.0', port=10000)
