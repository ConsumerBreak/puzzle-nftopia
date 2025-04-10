import os
import random
import json
import sys
from flask import Flask, Response, send_file, make_response, request
from twitchio.ext import commands
import asyncio
import logging
import time
from collections import deque
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from threading import Lock

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables (Render uses os.environ directly, no .env file needed)
logger.debug("Environment variables loaded from os.environ")

required_env_vars = ['TWITCH_TOKEN', 'TWITCH_CLIENT_ID']
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

app = Flask(__name__)
APP_VERSION = str(int(time.time()))

CSP_HEADER = (
    "default-src 'self' https://cdn.glitch.global https://pyscript.net; "
    "script-src 'self' https://pyscript.net 'unsafe-eval'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' https://cdn.glitch.global data:; "
    "media-src https://cdn.glitch.global; "
    "connect-src 'self';"
)

@app.after_request
def add_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-App-Version"] = APP_VERSION
    response.headers["Content-Security-Policy"] = CSP_HEADER
    logger.info(f"Response headers for {request.path}: {dict(response.headers)}")
    return response

try:
    logger.debug("Loading Google Sheets credentials from /app/creds.json")
    with open('/app/creds.json', 'r') as f:
        creds_dict = json.load(f)
    logger.info("Successfully loaded credentials from /app/creds.json")
except Exception as e:
    logger.error(f"Error loading Google Sheets credentials: {e}")
    raise

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

SPREADSHEET_ID = '1Z4HvlFc0gssCh9TxWkSvc2VugARjlphHMCpSJFB2kuo'
SHEET_NAME = 'Sheet1'

leaderboard_cache = None
leaderboard_cache_timestamp = None
LEADERBOARD_CACHE_DURATION = 30
cache_lock = Lock()

class RateLimiter:
    def __init__(self, rate, per):
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.time()
        self.queue = deque()

    def refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        new_tokens = elapsed * (self.rate / self.per)
        self.tokens = min(self.rate, self.tokens + new_tokens)
        self.last_refill = now

    def consume(self):
        self.refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

global_rate_limiter = RateLimiter(rate=10, per=5)

def exponential_backoff(func, max_retries=5, base_delay=1):
    retries = 0
    while retries < max_retries:
        try:
            return func()
        except gspread.exceptions.APIError as e:
            if e.response.status_code == 429:
                retries += 1
                if retries == max_retries:
                    raise e
                delay = base_delay * (2 ** retries) + random.uniform(0, 1)
                logger.warning(f"Rate limit exceeded, retrying in {delay:.2f} seconds (attempt {retries}/{max_retries})")
                time.sleep(delay)
            else:
                raise e

class GameState:
    def __init__(self, bot):
        self.bot = bot
        self.images = ['puzzle01_00000.png']
        self.image_index = 0
        self.current_image = self.images[self.image_index]
        logger.info(f"Starting with image: {self.current_image}")
        self.pieces = {}
        self.guesses = {}
        self.current_piece = None
        self.side_piece_section = None
        self.natural_section = None
        self.expected_section = None
        self.expected_coord = None
        self.section_mapping = {}
        self.reverse_section_mapping = {}
        self.game_id = 0
        self.piece_id = 0
        self.last_guess_times = {}
        self.cooldown_seconds = 60
        self.min_prize = 1
        self.max_prize = 50
        self.last_winner = None
        self.last_prize = 0
        self.initialize_puzzle()

    def load_leaderboard_from_sheet(self):
        global leaderboard_cache, leaderboard_cache_timestamp
        with cache_lock:
            if (leaderboard_cache is not None and
                    leaderboard_cache_timestamp is not None and
                    (datetime.now() - leaderboard_cache_timestamp).total_seconds() < LEADERBOARD_CACHE_DURATION):
                logger.debug("Returning cached leaderboard")
                return leaderboard_cache

            def fetch_leaderboard():
                try:
                    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
                    records = sheet.get_all_values()
                    if not records or len(records) < 1:
                        sheet.append_row(['Username', 'Wins'])
                        return {}
                    if records[0] != ['Username', 'Wins']:
                        sheet.clear()
                        sheet.append_row(['Username', 'Wins'])
                        return {}
                    leaderboard = {row[0].lower().strip(): int(row[1] or 0) for row in records[1:] if row and row[0]}
                    return leaderboard
                except Exception as e:
                    logger.error(f"Failed to fetch leaderboard: {str(e)}")
                    raise e

            try:
                leaderboard = exponential_backoff(fetch_leaderboard)
                leaderboard_cache = leaderboard
                leaderboard_cache_timestamp = datetime.now()
                logger.debug("Successfully loaded leaderboard from sheet")
                return leaderboard
            except Exception as e:
                logger.error(f"Failed to load leaderboard after retries: {str(e)}")
                return leaderboard_cache or {}

    def update_leaderboard_in_sheet(self, username):
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
            records = sheet.get_all_values()
            if not records or records[0] != ['Username', 'Wins']:
                sheet.clear()
                sheet.append_row(['Username', 'Wins'])
                records = [['Username', 'Wins']]

            user_row = next((i + 2 for i, row in enumerate(records[1:]) if row and row[0].lower().strip() == username.lower().strip()), None)
            if user_row:
                current_wins = int(records[user_row - 1][1] or 0)
                sheet.update_cell(user_row, 2, current_wins + 1)
            else:
                sheet.append_row([username, 1])

            with cache_lock:
                global leaderboard_cache, leaderboard_cache_timestamp
                leaderboard_cache = None
                leaderboard_cache_timestamp = None
            logger.debug(f"Updated leaderboard for user: {username}")
        except Exception as e:
            logger.error(f"Failed to update leaderboard: {str(e)}")

    def initialize_puzzle(self):
        self.pieces = {}
        self.guesses = {}
        sections = list(range(25))
        random.shuffle(sections)
        self.section_mapping = {i: sections[i] for i in range(25)}
        self.reverse_section_mapping = {v: k for k, v in self.section_mapping.items()}
        remaining_sections = list(range(25))
        if remaining_sections:
            self.current_piece = remaining_sections.pop(0)
            self.side_piece_section = self.current_piece
            self.natural_section = self.section_mapping[self.current_piece]
            self.expected_section = self.current_piece
            self.expected_coord = self.index_to_coord(self.natural_section)
        if self.images:
            self.current_image = self.images[self.image_index]
        self.game_id += 1
        self.piece_id += 1
        logger.debug(f"Puzzle initialized with image: {self.current_image}, game_id: {self.game_id}")

    def cycle_image(self):
        if len(self.images) > 1:
            self.image_index = (self.image_index + 1) % len(self.images)
            self.current_image = self.images[self.image_index]
            logger.info(f"Cycled to image: {self.current_image} (index: {self.image_index}/{len(self.images)-1})")
        else:
            logger.info(f"Only one image available, staying on: {self.current_image}")

    def update_image_list(self, new_images):
        if new_images and new_images != self.images:
            self.images = sorted(new_images, key=lambda x: int(x.replace('puzzle', '').replace('_00000.png', '')))
            logger.info(f"Updated image list: {self.images}")
            if self.current_image not in self.images:
                self.image_index = 0
                self.current_image = self.images[self.image_index]
                logger.info(f"Reset current image to: {self.current_image}")

    def index_to_coord(self, index):
        row = index // 5
        col = index % 5
        return f"{chr(65 + row)}{col + 1}"

    def coord_to_index(self, coord):
        row = ord(coord[0].upper()) - 65
        col = int(coord[1]) - 1
        return row * 5 + col

    def set_cooldown(self, seconds):
        try:
            seconds = int(seconds)
            if seconds < 0:
                raise ValueError("Cooldown cannot be negative!")
            self.cooldown_seconds = seconds
            logger.debug(f"Cooldown set to {seconds}s")
            return f"Cooldown has been set to {seconds} seconds"
        except ValueError as e:
            logger.error(f"Invalid cooldown: {seconds}")
            return f"Invalid cooldown duration! {str(e)}"

    def set_prize(self, range_str):
        try:
            if '-' not in range_str:
                raise ValueError("Prize range must be in the format 'min-max' (e.g., 1-50)")
            min_val, max_val = map(int, range_str.split('-'))
            if min_val < 0 or max_val < 0:
                raise ValueError("Prize values cannot be negative!")
            if min_val > max_val:
                raise ValueError("Minimum prize cannot be greater than maximum prize!")
            self.min_prize = min_val
            self.max_prize = max_val
            logger.debug(f"Prize range set to {min_val}-{max_val}")
            return f"Prize range has been set to {min_val}-{max_val} NFTOKENS"
        except ValueError as e:
            logger.error(f"Invalid prize range: {range_str}")
            return f"Invalid prize range! {str(e)}"

    def get_random_prize(self):
        return random.randint(self.min_prize, self.max_prize)

    async def guess(self, coord, username, ctx):
        try:
            username = username.lower().strip()
            now = time.time()
            last_guess_time = self.last_guess_times.get(username)
            time_since_last_guess = now - last_guess_time if last_guess_time else float('inf')

            if last_guess_time and time_since_last_guess < self.cooldown_seconds:
                remaining_time = int(self.cooldown_seconds - time_since_last_guess)
                logger.debug(f"{username} on cooldown, {remaining_time}s left")
                await ctx.send(f"@{username} wait {remaining_time} seconds")
                return

            self.last_guess_times[username] = now

            logger.debug(f"Processing guess: coord={coord}, username={username}, expected_coord={self.expected_coord}")
            if coord not in self.pieces:
                if coord == self.expected_coord:
                    section_index = self.natural_section
                    self.pieces[coord] = section_index
                    prize = self.get_random_prize()
                    self.last_winner = username
                    self.last_prize = prize
                    logger.debug(f"Correct guess by {username} at {coord}, awarding {prize} NFTOKENS")
                    await ctx.send(f"Nice job, @{username}! Piece placed at {coord}. You won {prize} NFTOKENS!")
                    await ctx.send(f"!tip @{username} {prize}")
                    self.update_leaderboard_in_sheet(username)

                    if len(self.pieces) == 25:
                        prize = self.get_random_prize()
                        self.last_winner = "Everyone"
                        self.last_prize = prize
                        logger.info("Puzzle completed!")
                        await ctx.send(f"Puzzle completed! Everyone wins {prize} NFTOKENS!")
                        await ctx.send(f"!tip all {prize}")
                        await asyncio.sleep(8)
                        self.cycle_image()
                        self.initialize_puzzle()
                        logger.debug(f"New game started with image: {self.current_image}")
                        return

                    used_natural_sections = set(self.pieces.values())
                    used_side_sections = set(self.reverse_section_mapping[ns] for ns in used_natural_sections)
                    remaining_side_sections = [s for s in range(25) if s not in used_side_sections]

                    max_attempts = 10
                    attempt = 0
                    while attempt < max_attempts:
                        if not remaining_side_sections:
                            prize = self.get_random_prize()
                            self.last_winner = "Everyone"
                            self.last_prize = prize
                            logger.info("Puzzle completed (no sections left)!")
                            await ctx.send(f"Puzzle completed! Everyone wins {prize} NFTOKENS!")
                            await ctx.send(f"!tip all {prize}")
                            await asyncio.sleep(8)
                            self.cycle_image()
                            self.initialize_puzzle()
                            logger.debug(f"New game started with image: {self.current_image}")
                            return
                        new_piece = random.choice(remaining_side_sections)
                        if new_piece in used_side_sections:
                            remaining_side_sections.remove(new_piece)
                            attempt += 1
                            continue
                        self.current_piece = new_piece
                        break
                    if attempt >= max_attempts:
                        prize = self.get_random_prize()
                        self.last_winner = "Everyone"
                        self.last_prize = prize
                        logger.info("Puzzle completed (max attempts reached)!")
                        await ctx.send(f"Puzzle completed! Everyone wins {prize} NFTOKENS!")
                        await ctx.send(f"!tip all {prize}")
                        await asyncio.sleep(8)
                        self.cycle_image()
                        self.initialize_puzzle()
                        logger.debug(f"New game started with image: {self.current_image}")
                        return

                    self.side_piece_section = self.current_piece
                    self.natural_section = self.section_mapping[self.current_piece]
                    self.expected_section = self.current_piece
                    self.expected_coord = self.index_to_coord(self.natural_section)
                    self.piece_id += 1
                    self.guesses = {}
                    logger.debug(f"New piece set: {self.current_piece}, notifying win for {username}")
                else:
                    self.guesses[coord] = 'miss'
                    logger.debug(f"Incorrect guess by {username} at {coord}")
                    await ctx.send(f"@{username} Nope, try again!")
            else:
                logger.debug(f"{coord} already solved by {username}")
                await ctx.send(f"@{username} {coord} has already been solved.")
        except Exception as e:
            logger.error(f"Guess error: {str(e)}")
            await ctx.send(f"@{username} Error processing guess: {str(e)}")

    def get_state(self):
        leaderboard = self.load_leaderboard_from_sheet()
        sorted_leaderboard = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
        state = {
            'pieces': self.pieces,
            'guesses': self.guesses,
            'current_piece': self.current_piece,
            'natural_section': self.natural_section,
            'current_image': self.current_image,
            'leaderboard': sorted_leaderboard,
            'sectionMapping': self.section_mapping,
            'reverse_section_mapping': self.reverse_section_mapping,
            'baseMapping': self.section_mapping,
            'reverseBaseMapping': self.reverse_section_mapping,
            'gameId': self.game_id,
            'pieceId': self.piece_id,
            'cooldownSeconds': self.cooldown_seconds,
            'minPrize': self.min_prize,
            'maxPrize': self.max_prize,
            'images': self.images,
            'version': APP_VERSION,
            'last_winner': self.last_winner,
            'last_prize': self.last_prize
        }
        logger.debug(f"Game state served: current_image={self.current_image}, images={self.images}")
        return state

game_state = None

@app.route('/')
def index():
    logger.info("Route / hit - attempting to serve index.html")
    try:
        response = make_response(send_file('index.html'))
        logger.info("Successfully served index.html")
        return response
    except Exception as e:
        logger.error(f"Error serving index.html: {str(e)}")
        return Response(f"Server error: {str(e)}", status=500)

@app.route('/test_static')
def test_static():
    logger.info("Route /test_static hit - serving static text")
    return Response("Minimal Flask Test - Static Works!", status=200)

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    full_path = os.path.join(app.root_path, 'assets', filename)
    logger.debug(f"Attempting to serve asset: {full_path}")
    if os.path.exists(full_path) and not os.path.isdir(full_path):
        logger.info(f"Asset found, serving: {full_path}")
        return send_file(full_path)
    logger.warning(f"Asset not found: {full_path}")
    return Response("Asset not found", status=404)

@app.route('/health')
def health_check():
    logger.info("Health check hit - returning 'OK' with 200")
    return "OK", 200

@app.route('/favicon.ico')
def favicon():
    logger.debug("Favicon requested - returning empty response")
    return Response("", mimetype='image/x-icon', status=204)

@app.route('/<path:path>')
def serve_file(path):
    full_path = os.path.join(app.root_path, path)
    logger.debug(f"Attempting to serve file: {full_path}")
    if os.path.exists(full_path) and not os.path.isdir(full_path):
        logger.info(f"File found, serving: {full_path}")
        return send_file(full_path)
    logger.warning(f"File not found: {full_path}")
    return Response("File not found", status=404)

@app.route('/game_state')
def get_game_state():
    logger.info("Serving game state requested")
    try:
        if game_state is None:
            logger.warning("Game state not initialized yet")
            return Response("Server not ready", status=503)
        state = game_state.get_state()
        logger.debug(f"Game state served: current_image={state['current_image']}, images={state['images']}")
        return Response(json.dumps(state), mimetype='application/json')
    except Exception as e:
        logger.error(f"Error serving game state: {str(e)}")
        return Response(f"Server error: {str(e)}", status=500)

@app.route('/update_images', methods=['POST'])
def update_images():
    try:
        data = request.get_json()
        new_images = data.get('images', [])
        if new_images:
            game_state.update_image_list(new_images)
            logger.info(f"Received updated image list from frontend: {new_images}")
            return Response("Images updated", status=200)
        logger.warning("No images provided in update request")
        return Response("No images provided", status=400)
    except Exception as e:
        logger.error(f"Error updating images: {str(e)}")
        return Response(f"Server error: {str(e)}", status=500)

async def run_bot():
    global game_state
    logger.info("Starting Twitch bot")
    bot = commands.Bot(
        token=os.environ['TWITCH_TOKEN'],
        client_id=os.environ['TWITCH_CLIENT_ID'],
        nick='nftopia_bot',
        prefix='!',
        initial_channels=['nftopia']
    )

    @bot.event
    async def event_ready():
        logger.info(f"Bot connected to Twitch! Channels: {bot.connected_channels}")
        for channel in bot.connected_channels:
            await channel.send("nftopia_bot is online!")

    @bot.command(name='g')
    async def guess_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return
            logger.info(f"Received command: {ctx.message.content} from {ctx.author.name} in {ctx.channel.name}")
            guess = ctx.message.content.split(' ')[1] if len(ctx.message.content.split(' ')) > 1 else None
            if guess and guess.upper() in [f"{chr(65+i)}{j}" for i in range(5) for j in range(1, 6)]:
                await game_state.guess(guess.upper(), ctx.author.name, ctx)
            else:
                await ctx.send("Invalid coordinate! Use format like A1, B3, etc.")
        except Exception as e:
            logger.error(f"Guess command error: {e}")
            await ctx.send("An error occurred while processing your guess.")

    @bot.command(name='cool')
    async def cool_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return
            logger.info(f"Received command: {ctx.message.content} from {ctx.author.name} in {ctx.channel.name}")
            if ctx.author.name.lower() == 'nftopia':
                args = ctx.message.content.split(' ')
                if len(args) > 1:
                    result = game_state.set_cooldown(args[1])
                    await ctx.send(result)
                else:
                    await ctx.send("Please provide a cooldown duration (e.g., !cool 30)")
            else:
                await ctx.send("Only nftopia can use this command!")
        except Exception as e:
            logger.error(f"Cool command error: {e}")
            if ctx.author.name.lower() == 'nftopia':
                await ctx.send("Error setting cooldown!")

    @bot.command(name='win')
    async def win_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return
            logger.info(f"Received command: {ctx.message.content} from {ctx.author.name} in {ctx.channel.name}")
            if ctx.author.name.lower() == 'nftopia':
                args = ctx.message.content.split(' ')
                if len(args) > 1:
                    result = game_state.set_prize(args[1])
                    await ctx.send(result)
                else:
                    await ctx.send("Please provide a prize range (e.g., !win 1-50)")
            else:
                await ctx.send("Only nftopia can use this command!")
        except Exception as e:
            logger.error(f"Win command error: {e}")
            if ctx.author.name.lower() == 'nftopia':
                await ctx.send("Error setting prize range!")

    @bot.command(name='testwin')
    async def test_win_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return
            logger.info(f"Received command: {ctx.message.content} from {ctx.author.name} in {ctx.channel.name}")
            if ctx.author.name.lower() == 'nftopia':
                logger.debug(f"Triggering test win event for {ctx.author.name}")
                await ctx.send(f"Triggered a test win event for {ctx.author.name}!")
            else:
                await ctx.send("Only nftopia can use this command!")
        except Exception as e:
            logger.error(f"Testwin command error: {e}")
            if ctx.author.name.lower() == 'nftopia':
                await ctx.send("Error triggering test win!")

    @bot.command(name='ping')
    async def ping_command(ctx):
        logger.info(f"Ping command received from {ctx.author.name} in {ctx.channel.name}")
        await ctx.send("Pong! Bot is alive!")

    game_state = GameState(bot)

    try:
        await bot.start()
        logger.info("Bot started successfully")
    except Exception as e:
        logger.error(f"Bot start failed: {e}")
        raise

if __name__ == "__main__":
    logger.info("Main script starting")
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"Starting Flask on port {port}")
    # Run Flask and bot together (Render handles concurrency)
    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)