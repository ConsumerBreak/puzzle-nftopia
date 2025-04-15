import os
import random
import json
import aiohttp
from flask import Flask, Response, send_file
from twitchio.ext import commands
import queue
import asyncio
import logging
import time
from collections import deque
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from threading import Lock
from hypercorn.config import Config
from hypercorn.asyncio import serve

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Set twitchio logging to DEBUG for more detailed output
logging.getLogger('twitchio').setLevel(logging.DEBUG)

app = Flask(__name__)

# Content Security Policy header
CSP_HEADER = (
    "default-src 'self' https://cdn.glitch.global https://pyscript.net; "
    "script-src 'self' https://pyscript.net https://cdn.jsdelivr.net 'unsafe-eval' 'unsafe-inline'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' https://cdn.glitch.global data:; "
    "media-src https://cdn.glitch.global; "
    "connect-src 'self' https://cdn.jsdelivr.net https://pypi.org blob:;"
)

# Check for required environment variables
required_env_vars = ['TWITCH_TOKEN', 'TWITCH_CLIENT_ID', 'GOOGLE_CREDENTIALS']
missing_vars = [var for var in required_env_vars if var not in os.environ]
if missing_vars:
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}.")

# Google Sheets setup
creds_json = os.environ.get('GOOGLE_CREDENTIALS')
creds_dict = json.loads(creds_json)
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
client = gspread.authorize(creds)

# Spreadsheet configuration
SPREADSHEET_ID = '1amJa8alcwRwX-JnhbPjdrAUk16VXxlKjmWwXDCFvjSU'
SHEET_NAME = 'Sheet1'

# Leaderboard cache
leaderboard_cache = None
leaderboard_cache_timestamp = None
LEADERBOARD_CACHE_DURATION = 30  # Cache for 30 seconds
cache_lock = Lock()

# Rate limiter for commands
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

# Message deduplication cache
class MessageDeduplicator:
    def __init__(self, window_seconds=5, max_size=1000):
        self.seen_messages = deque(maxlen=max_size)
        self.window_seconds = window_seconds

    def has_seen(self, message_id):
        current_time = time.time()
        # Clean up old entries
        while self.seen_messages and current_time - self.seen_messages[0][1] > self.window_seconds:
            self.seen_messages.popleft()
        # Check if message_id is in the cache
        for msg_id, _ in self.seen_messages:
            if msg_id == message_id:
                return True
        return False

    def add(self, message_id):
        self.seen_messages.append((message_id, time.time()))

message_deduplicator = MessageDeduplicator(window_seconds=5, max_size=1000)

# Command deduplication cache (additional layer)
class CommandDeduplicator:
    def __init__(self, window_seconds=5, max_size=1000):
        self.seen_commands = deque(maxlen=max_size)
        self.window_seconds = window_seconds

    def has_seen(self, command_id):
        current_time = time.time()
        while self.seen_commands and current_time - self.seen_commands[0][1] > self.window_seconds:
            self.seen_commands.popleft()
        for cmd_id, _ in self.seen_commands:
            if cmd_id == command_id:
                return True
        return False

    def add(self, command_id):
        self.seen_commands.append((command_id, time.time()))

command_deduplicator = CommandDeduplicator(window_seconds=5, max_size=1000)

# Exponential backoff for API calls
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

# Game state
class GameState:
    def __init__(self, bot):
        self.bot = bot
        self.images = []
        self.image_index = 0
        self.current_image = None
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
        self.event_queue = queue.Queue()
        self.last_guess_times = {}
        self.cooldown_seconds = 60  # Default from Replit
        self.min_prize = 1  # Default from Replit
        self.max_prize = 50  # Default from Replit

    @classmethod
    async def create(cls, bot):
        instance = cls(bot)
        await instance.initialize_images()
        instance.initialize_puzzle()
        return instance

    async def initialize_images(self):
        """Dynamically determine available puzzle images by attempting to fetch them."""
        self.images = []
        i = 1
        async with aiohttp.ClientSession() as session:
            while True:
                image_name = f"puzzle{i:02d}_00000.png"
                url = f"https://cdn.glitch.global/509f3353-63f2-4aa2-b309-108c09d4235e/{image_name}"
                try:
                    async with session.head(url) as response:
                        logger.info(f"Checking image {image_name}: HTTP Status {response.status}")
                        if response.status == 200:
                            self.images.append(image_name)
                            logger.info(f"Found puzzle image: {image_name}")
                            i += 1
                        else:
                            logger.info(f"No more puzzle images found after {image_name}, stopping at {i-1} puzzles")
                            break
                except Exception as e:
                    logger.error(f"Error checking puzzle image {image_name}: {str(e)}")
                    break
        if not self.images:
            logger.warning("No puzzle images found, using a placeholder")
            self.images = ["placeholder.png"]
        self.current_image = self.images[0] if self.images else "placeholder.png"
        logger.info(f"Initialized with {len(self.images)} puzzle images: {self.images}")
        logger.info(f"Set initial current_image to: {self.current_image}")

    def load_leaderboard_from_sheet(self):
        global leaderboard_cache, leaderboard_cache_timestamp
        with cache_lock:
            if (leaderboard_cache is not None and
                    leaderboard_cache_timestamp is not None and
                    (datetime.now() - leaderboard_cache_timestamp).total_seconds() < LEADERBOARD_CACHE_DURATION):
                logger.info("Returning cached leaderboard")
                return leaderboard_cache

            def fetch_leaderboard():
                try:
                    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
                    records = sheet.get_all_values()
                    logger.info(f"Fetched records from sheet: {records}")
                    if not records or len(records) < 1:
                        logger.info("Sheet is empty, initializing with header")
                        sheet.append_row(['Username', 'Wins'])
                        return {}
                    if records[0] != ['Username', 'Wins']:
                        logger.info("Sheet header incorrect, resetting sheet")
                        sheet.clear()
                        sheet.append_row(['Username', 'Wins'])
                        return {}
                    leaderboard = {}
                    for row in records[1:]:
                        if len(row) >= 2 and row[0]:
                            username = row[0].lower().strip()
                            try:
                                wins = int(row[1])
                            except (ValueError, IndexError):
                                wins = 0
                            leaderboard[username] = wins
                    logger.info(f"Parsed leaderboard: {leaderboard}")
                    return leaderboard
                except Exception as e:
                    logger.error(f"Failed to fetch leaderboard: {str(e)}")
                    raise e

            try:
                leaderboard = exponential_backoff(fetch_leaderboard)
                leaderboard_cache = leaderboard
                leaderboard_cache_timestamp = datetime.now()
                logger.info("Successfully loaded leaderboard from sheet")
                return leaderboard
            except Exception as e:
                logger.error(f"Failed to load leaderboard after retries: {str(e)}")
                if leaderboard_cache is not None:
                    logger.info("Returning cached leaderboard due to API error")
                    return leaderboard_cache
                return {}

    def update_leaderboard_in_sheet(self, username):
        try:
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
            records = sheet.get_all_values()
            if not records or records[0] != ['Username', 'Wins']:
                sheet.clear()
                sheet.append_row(['Username', 'Wins'])
                records = [['Username', 'Wins']]

            user_row = None
            for i, row in enumerate(records[1:], start=2):
                if len(row) >= 1 and row[0].lower().strip() == username.lower().strip():
                    user_row = i
                    break

            if user_row:
                current_wins = int(records[user_row - 1][1] or 0)
                sheet.update_cell(user_row, 2, current_wins + 1)
            else:
                sheet.append_row([username, 1])

            with cache_lock:
                global leaderboard_cache, leaderboard_cache_timestamp
                leaderboard_cache = None
                leaderboard_cache_timestamp = None
            logger.info(f"Updated leaderboard for user: {username}")
        except Exception as e:
            logger.error(f"Failed to update leaderboard: {str(e)}", exc_info=True)

    def initialize_puzzle(self):
        logger.info(f"Initializing puzzle for game ID {self.game_id + 1}")
        self.pieces = {}
        self.guesses = {}
        sections = list(range(25))
        random.shuffle(sections)
        self.section_mapping = {i: sections[i] for i in range(25)}
        self.reverse_section_mapping = {v: k for k, v in self.section_mapping.items()}
        logger.info(f"Section mapping created: {self.section_mapping}")
        remaining_sections = list(range(25))
        if remaining_sections:
            self.current_piece = remaining_sections.pop(0)
            self.side_piece_section = self.current_piece
            self.natural_section = self.section_mapping[self.current_piece]
            self.expected_section = self.current_piece
            self.expected_coord = self.index_to_coord(self.natural_section)
            logger.info(f"Set initial piece: current_piece={self.current_piece}, natural_section={self.natural_section}, expected_coord={self.expected_coord}")
        if self.images:
            logger.info(f"Before setting current_image: current_image={self.current_image}, image_index={self.image_index}")
            self.current_image = self.images[self.image_index]
            self.image_index = (self.image_index + 1) % len(self.images)
            logger.info(f"Selected puzzle image: {self.current_image} (index {self.image_index})")
        else:
            self.current_image = "placeholder.png"
            logger.warning("No images available, using placeholder.png")
        self.game_id += 1
        self.piece_id += 1
        logger.info(f"Puzzle initialized: game_id={self.game_id}, piece_id={self.piece_id}")
        self.notify_state_update()

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
            return f"Cooldown has been set to {seconds} seconds"
        except ValueError:
            return "Invalid cooldown duration! Please provide a non-negative integer."

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
            return f"Prize range has been set to {min_val}-{max_val} NFTOKEN"
        except ValueError as e:
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
                remaining_time = round(self.cooldown_seconds - time_since_last_guess)
                await ctx.send(f"@{username} wait {remaining_time} seconds")
                return

            self.last_guess_times[username] = now

            logger.info(f"Processing guess: coord={coord}, username={username}, expected_coord={self.expected_coord}")
            if coord not in self.pieces:
                if coord == self.expected_coord:
                    section_index = self.natural_section
                    self.pieces[coord] = section_index
                    prize = self.get_random_prize()
                    await ctx.send(f"@{username} Piece solved. You win {prize} NFTOKEN!")
                    await ctx.send(f"!tip {username} {prize}")
                    self.update_leaderboard_in_sheet(username)

                    used_natural_sections = set(self.pieces.values())
                    used_side_sections = set(self.reverse_section_mapping[ns] for ns in used_natural_sections)
                    remaining_side_sections = [s for s in range(25) if s not in used_side_sections]

                    if not remaining_side_sections:
                        prize = self.get_random_prize()
                        await ctx.send(f"Puzzle completed! Everyone wins {prize} NFTOKEN!")
                        await ctx.send(f"!tip all {prize}")
                        self.notify_event('complete', {'winner': 'Everyone', 'prize': prize})
                        await asyncio.sleep(8)
                        self.initialize_puzzle()
                        return

                    max_attempts = 10
                    attempt = 0
                    while attempt < max_attempts:
                        if not remaining_side_sections:
                            prize = self.get_random_prize()
                            await ctx.send(f"Puzzle completed! Everyone wins {prize} NFTOKEN!")
                            await ctx.send(f"!tip all {prize}")
                            self.notify_event('complete', {'winner': 'Everyone', 'prize': prize})
                            await asyncio.sleep(8)
                            self.initialize_puzzle()
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
                        await ctx.send(f"Puzzle completed! Everyone wins {prize} NFTOKEN!")
                        await ctx.send(f"!tip all {prize}")
                        self.notify_event('complete', {'winner': 'Everyone', 'prize': prize})
                        await asyncio.sleep(8)
                        self.initialize_puzzle()
                        return

                    self.side_piece_section = self.current_piece
                    self.natural_section = self.section_mapping[self.current_piece]
                    self.expected_section = self.current_piece
                    self.expected_coord = self.index_to_coord(self.natural_section)
                    self.piece_id += 1
                    self.guesses = {}
                    logger.info(f"Sending win event for {username}")
                    self.notify_event('win', {'winner': username, 'prize': prize})
                else:
                    self.guesses[coord] = 'miss'
                    await ctx.send(f"@{username} Wrong!")
            else:
                await ctx.send(f"@{username} {coord} has already been solved.")
            self.notify_state_update()
        except Exception as e:
            logger.error(f"ERROR in guess method: {str(e)}")
            self.notify_state_update()

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
            'maxPrize': self.max_prize
        }
        logger.info(f"Returning game state with current_image: {self.current_image}")
        return state

    def notify_state_update(self):
        try:
            event_data = {
                'type': 'state',
                'state': self.get_state(),
                'event': {},
                'timestamp': time.time()
            }
            self.event_queue.put_nowait(event_data)
            logger.info(f"Sent state update event: {json.dumps(event_data)}")
        except queue.Full:
            logger.warning("Event queue full, dropping state update")

    def notify_event(self, event_type, event_data):
        try:
            event = {
                'type': event_type,
                'state': self.get_state(),
                'event': event_data,
                'timestamp': time.time()
            }
            self.event_queue.put_nowait(event)
            logger.info(f"Sent event: {json.dumps(event)}")
        except queue.Full:
            logger.warning(f"Event queue full, dropping event: {event_type}")

# Flask routes
@app.route('/')
def index():
    response = send_file('index.html', mimetype='text/html')
    response.headers['Content-Security-Policy'] = CSP_HEADER
    return response

@app.route('/game_state')
def get_game_state():
    return game_state.get_state()

@app.route('/events')
def sse():
    def stream():
        last_ping_time = 0
        PING_INTERVAL = 3  # Reduced from 5 seconds

        while True:
            try:
                try:
                    event = game_state.event_queue.get(timeout=PING_INTERVAL)
                    logger.info(f"Sending SSE event to client: {json.dumps(event)}")
                    yield f"data: {json.dumps(event)}\n\n"
                    last_ping_time = time.time()
                    continue
                except queue.Empty:
                    current_time = time.time()
                    if current_time - last_ping_time >= PING_INTERVAL:
                        event = {
                            'type': 'ping',
                            'state': game_state.get_state(),
                            'event': {},
                            'timestamp': current_time
                        }
                        logger.info(f"Sending SSE ping event: {json.dumps(event)}")
                        yield f"data: {json.dumps(event)}\n\n"
                        last_ping_time = current_time
                    time.sleep(0.1)
                    continue

            except (BrokenPipeError, ConnectionError, OSError) as e:
                logger.info(f"SSE client disconnected: {str(e)}")
                break
            except Exception as e:
                logger.error(f"SSE stream error: {str(e)}")
                break

    response = Response(stream(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Connection'] = 'keep-alive'
    return response

@app.route('/health')
def health_check():
    return "OK", 200

# Test Twitch token validity
async def test_twitch_token():
    token = os.environ['TWITCH_TOKEN']
    client_id = os.environ['TWITCH_CLIENT_ID']
    headers = {
        'Authorization': f'Bearer {token.replace("oauth:", "")}',
        'Client-Id': client_id
    }
    async with aiohttp.ClientSession() as session:
        async with session.get('https://id.twitch.tv/oauth2/validate', headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                logger.info(f"Twitch token validation successful: {data}")
                return True
            else:
                logger.error(f"Twitch token validation failed: HTTP {response.status}, {await response.text()}")
                return False

# Start the bot with a delay and detailed error handling
async def start_bot_with_delay(bot):
    logger.info("Starting Twitch bot delay...")
    await asyncio.sleep(5)
    logger.info("Twitch bot delay completed, attempting to start bot...")
    try:
        if await test_twitch_token():
            logger.info("Token is valid, starting bot...")
            await bot.start()
        else:
            logger.error("Skipping bot start due to invalid token.")
    except Exception as e:
        logger.error(f"Failed to start Twitch bot: {str(e)}", exc_info=True)
        logger.info("Retrying Twitch bot connection in 10 seconds...")
        await asyncio.sleep(10)
        try:
            if await test_twitch_token():
                await bot.start()
            else:
                logger.error("Retry skipped due to invalid token.")
        except Exception as e:
            logger.error(f"Retry failed: {str(e)}. Please check TWITCH_TOKEN and network connectivity.", exc_info=True)

# Main entry point
async def main():
    logger.info("Main script starting")

    bot = commands.Bot(
        token=os.environ['TWITCH_TOKEN'],
        client_id=os.environ['TWITCH_CLIENT_ID'],
        nick='nftopia_puzzle_bot',
        prefix='!',
        initial_channels=['nftopia']
    )

    @bot.event()
    async def event_ready():
        logger.info(f"Bot connected to Twitch! Nick: {bot.nick}, Channels: {[channel.name for channel in bot.connected_channels]}")

    @bot.event()
    async def event_error(error, data=None):
        logger.error(f"Twitch bot error: {str(error)}\nData: {data if data is not None else 'None'}")

    @bot.event()
    async def event_raw_data(data):
        logger.debug(f"Twitch raw data: {data}")

    @bot.event()
    async def event_message(message):
        # Skip system messages (where author is None)
        if message.author is None:
            logger.debug(f"Skipping system message: {message.content}")
            return

        # Use message ID for deduplication
        message_id = message.tags.get('id') if message.tags else None
        if not message_id:
            # Fallback if message ID is missing
            message_id = f"{message.timestamp}_{message.content}_{message.author.name if message.author else 'None'}"
            logger.warning(f"Message ID missing, using fallback ID: {message_id}")

        # Log message details for debugging
        logger.debug(f"Received message: ID={message_id}, Author={message.author.name if message.author else 'None'}, Content={message.content}, Tags={message.tags}")

        # Check if we've already processed this message
        if message_deduplicator.has_seen(message_id):
            logger.debug(f"Skipping duplicate message: ID={message_id}, Content={message.content}")
            return
        
        # Add message to deduplication cache
        message_deduplicator.add(message_id)

        # Skip messages from the bot itself
        if message.author.name.lower() == bot.nick.lower():
            logger.debug(f"Skipping bot's own message: ID={message_id}, Content={message.content}")
            return

        logger.debug(f"Processing message: ID={message_id}, From={message.author.name}, Content={message.content}")
        await bot.handle_commands(message)

    @bot.command(name='g', aliases=['G'])
    async def guess_command(ctx):
        try:
            # Rate limit check
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return

            # Command-level deduplication
            message_id = ctx.message.tags.get('id') if ctx.message.tags else f"{ctx.message.timestamp}_{ctx.message.content}"
            command_id = f"guess_{message_id}"
            if command_deduplicator.has_seen(command_id):
                logger.debug(f"Skipping duplicate guess command: ID={command_id}, Content={ctx.message.content}")
                return
            command_deduplicator.add(command_id)

            logger.debug(f"Processing guess command: ID={command_id}, Content={ctx.message.content}")

            guess = ctx.message.content.split(' ')[1] if len(ctx.message.content.split(' ')) > 1 else None
            if guess and guess.upper() in [f"{chr(65+i)}{j}" for i in range(5) for j in range(1, 6)]:
                await game_state.guess(guess.upper(), ctx.author.name, ctx)
            else:
                await ctx.send("Invalid coordinate! Use format like A1, B3, etc.")
        except Exception as e:
            logger.error(f"ERROR in guess_command: {str(e)}")
            await ctx.send("An error occurred while processing your guess. Please try again.")

    @bot.command(name='cool')
    async def cool_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return

            # Command-level deduplication
            message_id = ctx.message.tags.get('id') if ctx.message.tags else f"{ctx.message.timestamp}_{ctx.message.content}"
            command_id = f"cool_{message_id}"
            if command_deduplicator.has_seen(command_id):
                logger.debug(f"Skipping duplicate cool command: ID={command_id}, Content={ctx.message.content}")
                return
            command_deduplicator.add(command_id)

            logger.debug(f"Processing cool command: ID={command_id}, Content={ctx.message.content}")

            if ctx.author.name.lower() == 'nftopia':
                args = ctx.message.content.split(' ')
                if len(args) > 1:
                    new_cooldown = args[1]
                    result = game_state.set_cooldown(new_cooldown)
                    await ctx.send(result)
                else:
                    await ctx.send("Please provide a cooldown duration (e.g., !cool 30)")
        except Exception as e:
            logger.error(f"ERROR in cool_command: {str(e)}")
            if ctx.author.name.lower() == 'nftopia':
                await ctx.send("An error occurred while setting the cooldown. Please try again.")

    @bot.command(name='win')
    async def win_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return

            # Command-level deduplication
            message_id = ctx.message.tags.get('id') if ctx.message.tags else f"{ctx.message.timestamp}_{ctx.message.content}"
            command_id = f"win_{message_id}"
            if command_deduplicator.has_seen(command_id):
                logger.debug(f"Skipping duplicate win command: ID={command_id}, Content={ctx.message.content}")
                return
            command_deduplicator.add(command_id)

            logger.debug(f"Processing win command: ID={command_id}, Content={ctx.message.content}")

            if ctx.author.name.lower() == 'nftopia':
                args = ctx.message.content.split(' ')
                if len(args) > 1:
                    prize_range = args[1]
                    result = game_state.set_prize(prize_range)
                    await ctx.send(result)
                else:
                    await ctx.send("Please provide a prize range (e.g., !win 1-50)")
        except Exception as e:
            logger.error(f"ERROR in win_command: {str(e)}")
            if ctx.author.name.lower() == 'nftopia':
                await ctx.send("An error occurred while setting the prize range. Please try again.")

    @bot.command(name='testwin')
    async def test_win_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return

            # Command-level deduplication
            message_id = ctx.message.tags.get('id') if ctx.message.tags else f"{ctx.message.timestamp}_{ctx.message.content}"
            command_id = f"testwin_{message_id}"
            if command_deduplicator.has_seen(command_id):
                logger.debug(f"Skipping duplicate testwin command: ID={command_id}, Content={ctx.message.content}")
                return
            command_deduplicator.add(command_id)

            logger.debug(f"Processing testwin command: ID={command_id}, Content={ctx.message.content}")

            if ctx.author.name.lower() == 'nftopia':
                logger.info(f"Triggering test win event for {ctx.author.name}")
                game_state.notify_event('win', {'winner': ctx.author.name, 'prize': game_state.get_random_prize()})
                await ctx.send(f"Triggered a test win event for {ctx.author.name}!")
        except Exception as e:
            logger.error(f"ERROR in test_win_command: {str(e)}")
            await ctx.send("An error occurred while triggering the test win event.")

    global game_state
    game_state = await GameState.create(bot)

    bot_task = asyncio.create_task(start_bot_with_delay(bot))
    logger.info("Twitch bot task created")

    config = Config()
    config.bind = ["0.0.0.0:10000"]
    logger.info("Starting Flask on port 10000 with hypercorn")
    await serve(app, config)

if __name__ == '__main__':
    asyncio.run(main())
