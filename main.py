import os
import random
import json
import aiohttp
from flask import Flask, Response, send_file, stream_with_context
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
logging.getLogger('twitchio').setLevel(logging.INFO)

app = Flask(__name__)

CSP_HEADER = (
    "default-src 'self' https://cdn.glitch.global https://pyscript.net; "
    "script-src 'self' https://pyscript.net https://cdn.jsdelivr.net 'unsafe-eval' 'unsafe-inline'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' https://cdn.glitch.global data:; "
    "media-src https://cdn.glitch.global; "
    "connect-src 'self' https://cdn.jsdelivr.net https://pypi.org blob:;"
)

required_env_vars = ['TWITCH_TOKEN', 'TWITCH_CLIENT_ID', 'GOOGLE_CREDENTIALS']
missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    raise EnvironmentError(f"Missing required environment variables: {', '.join(missing_vars)}")

try:
    creds_json = os.environ.get('GOOGLE_CREDENTIALS')
    creds_dict = json.loads(creds_json)
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    client = gspread.authorize(creds)
except Exception as e:
    logger.error(f"Failed to initialize Google Sheets credentials: {str(e)}")
    raise

SPREADSHEET_ID = '1amJa8alcwRwX-JnhbPjdrAUk16VXxlKjmWwXDCFvjSU'
SHEET_NAME = 'Sheet1'

leaderboard_cache = None
leaderboard_cache_timestamp = None
LEADERBOARD_CACHE_DURATION = 60
cache_lock = Lock()

class RateLimiter:
    def __init__(self, rate, per):
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.time()

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

global_rate_limiter = RateLimiter(rate=20, per=5)

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
        self.event_queue = queue.Queue(maxsize=100)
        self.last_guess_times = {}
        self.cooldown_seconds = 60
        self.min_prize = 1
        self.max_prize = 50
        self.cached_leaderboard = None

    @classmethod
    async def create(cls, bot):
        instance = cls(bot)
        await instance.initialize_images()
        instance.initialize_puzzle()
        return instance

    async def initialize_images(self):
        async def validate_image(image_name):
            url = f"https://cdn.glitch.global/509f3353-63f2-4aa2-b309-108c09d4235e/{image_name}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.head(url, timeout=5) as response:
                        if response.status == 200:
                            logger.info(f"Validated puzzle image: {image_name}")
                            return image_name
                        else:
                            logger.debug(f"Image {image_name} not found: HTTP {response.status}")
                            return None
            except Exception as e:
                logger.debug(f"Error validating image {image_name}: {str(e)}")
                return None

        self.images = []
        max_attempts = 100
        tasks = [validate_image(f"puzzle{i:02d}_00000.png") for i in range(1, max_attempts + 1)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, str):
                self.images.append(result)
            else:
                break

        if not self.images:
            logger.warning("No puzzle images found, using placeholder")
            self.images = ["placeholder.png"]
            self.current_image = "placeholder.png"
        else:
            self.images.sort(key=lambda x: int(x.split('_')[0].replace('puzzle', '')))
            self.current_image = self.images[0]
            logger.info(f"Initialized with {len(self.images)} puzzle images: {self.images}")
            logger.info(f"Set initial current_image to: {self.current_image}")

    def load_leaderboard_from_sheet(self, force_refresh=False):
        global leaderboard_cache, leaderboard_cache_timestamp
        with cache_lock:
            if (not force_refresh and
                leaderboard_cache is not None and
                leaderboard_cache_timestamp is not None and
                (datetime.now() - leaderboard_cache_timestamp).total_seconds() < LEADERBOARD_CACHE_DURATION):
                self.cached_leaderboard = leaderboard_cache
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
                self.cached_leaderboard = leaderboard
                logger.info("Successfully loaded leaderboard from sheet")
                return leaderboard
            except Exception as e:
                logger.error(f"Failed to load leaderboard after retries: {str(e)}")
                if leaderboard_cache is not None:
                    logger.info("Returning cached leaderboard due to API error")
                    self.cached_leaderboard = leaderboard_cache
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
            self.cached_leaderboard = None
            logger.info(f"Updated leaderboard for user: {username}")
        except Exception as e:
            logger.error(f"Failed to update leaderboard: {str(e)}", exc_info=True)

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
            logger.info(f"Before setting current_image: current_image={self.current_image}, image_index={self.image_index}")
            self.current_image = self.images[self.image_index]
            self.image_index = (self.image_index + 1) % len(self.images)
            logger.info(f"After setting current_image: current_image={self.current_image}, image_index={self.image_index}")
        else:
            self.current_image = "placeholder.png"
        self.game_id += 1
        self.piece_id += 1
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
        start_time = time.time()
        try:
            username = username.lower().strip()
            now = time.time()
            last_guess_time = self.last_guess_times.get(username)
            time_since_last_guess = now - last_guess_time if last_guess_time else float('inf')

            if last_guess_time and time_since_last_guess < self.cooldown_seconds:
                remaining_time = round(self.cooldown_seconds - time_since_last_guess)
                await ctx.send(f"@{username} wait {remaining_time} seconds")
                logger.info(f"Guess rejected for {username} due to cooldown: {remaining_time}s remaining")
                return

            self.last_guess_times[username] = now

            logger.info(f"Processing guess: coord={coord}, username={username}, expected_coord={self.expected_coord}")
            if coord not in self.pieces:
                if coord == self.expected_coord:
                    section_index = self.natural_section
                    self.pieces[coord] = section_index
                    prize = self.get_random_prize()
                    await ctx.send(f"@{username} You won {prize} NFTOKEN!")
                    await ctx.send(f"!tip {username} {prize}")
                    self.update_leaderboard_in_sheet(username)

                    used_natural_sections = set(self.pieces.values())
                    used_side_sections = set(self.reverse_section_mapping[ns] for ns in used_natural_sections)
                    remaining_side_sections = [s for s in range(25) if s not in used_side_sections]

                    if not remaining_side_sections:
                        prize = self.get_random_prize()
                        await ctx.send(f"!tip all {prize}")
                        await ctx.send(f"Puzzle completed. Everyone won {prize} NFTOKEN!")
                        logger.info(f"Sending complete event for puzzle completion, prize: {prize}")
                        self.notify_event('complete', {'winner': 'Everyone', 'prize': prize})
                        await asyncio.sleep(8)
                        self.initialize_puzzle()
                    else:
                        max_attempts = 10
                        attempt = 0
                        while attempt < max_attempts:
                            if not remaining_side_sections:
                                prize = self.get_random_prize()
                                await ctx.send(f"!tip all {prize}")
                                await ctx.send(f"Puzzle completed. Everyone won {prize} NFTOKEN!")
                                logger.info(f"Sending complete event for puzzle completion, prize: {prize}")
                                self.notify_event('complete', {'winner': 'Everyone', 'prize': prize})
                                await asyncio.sleep(8)
                                self.initialize_puzzle()
                                break
                            new_piece = random.choice(remaining_side_sections)
                            if new_piece in used_side_sections:
                                remaining_side_sections.remove(new_piece)
                                attempt += 1
                                continue
                            self.current_piece = new_piece
                            break
                        if attempt >= max_attempts:
                            prize = self.get_random_prize()
                            await ctx.send(f"!tip all {prize}")
                            await ctx.send(f"Puzzle completed. Everyone won {prize} NFTOKEN!")
                            logger.info(f"Sending complete event for puzzle completion, prize: {prize}")
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
                        logger.info(f"Sending win event for {username}, prize: {prize}")
                        self.notify_event('win', {'winner': username, 'prize': prize})
                else:
                    self.guesses[coord] = 'miss'
                    await ctx.send(f"@{username} Wrong!")
            else:
                await ctx.send(f"@{username} {coord} has already been solved. Try another spot!")
            self.notify_state_update()
            logger.info(f"Guess processed for {username} in {time.time() - start_time:.3f}s")
        except Exception as e:
            logger.error(f"ERROR in guess method: {str(e)}", exc_info=True)
            await ctx.send(f"@{username} an error occurred while processing your guess. Please try again.")
            self.notify_state_update()

    def get_state(self):
        sorted_leaderboard = sorted((self.cached_leaderboard or self.load_leaderboard_from_sheet()).items(),
                                   key=lambda x: x[1], reverse=True)
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
        return state

    def notify_state_update(self):
        try:
            state = self.get_state()
            event_data = {'type': 'state', 'state': state, 'event': {}}
            self.event_queue.put_nowait(event_data)
            logger.info(f"Sent state update event with guesses: {state['guesses']}")
        except queue.Full:
            logger.warning("Event queue full, dropping state update")

    def notify_event(self, event_type, event_data):
        try:
            state = self.get_state()
            event = {'type': event_type, 'state': state, 'event': event_data}
            self.event_queue.put_nowait(event)
            logger.info(f"Sent {event_type} event: {event_data}")
        except queue.Full:
            logger.warning(f"Event queue full, dropping event: {event_type}")

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
        while True:
            try:
                event = game_state.event_queue.get(timeout=30)
                logger.info(f"Sending SSE event with guesses: {event['state']['guesses']}")
                yield f"data: {json.dumps(event)}\n\n"
                game_state.event_queue.task_done()
            except queue.Empty:
                event = {'type': 'ping', 'state': game_state.get_state(), 'event': {}}
                logger.info(f"Sending SSE ping event")
                yield f"data: {json.dumps(event)}\n\n"
            except (BrokenPipeError, ConnectionError, OSError) as e:
                logger.debug(f"SSE client disconnected: {str(e)}")
                break
            except Exception as e:
                logger.error(f"SSE stream error: {str(e)}")
                break
    return Response(stream_with_context(stream()), mimetype='text/event-stream')

@app.route('/health')
def health_check():
    try:
        if not os.environ.get('TWITCH_TOKEN') or not os.environ.get('TWITCH_CLIENT_ID'):
            logger.error("Missing Twitch credentials in health check")
            return "Missing Twitch credentials", 500
        if not os.environ.get('GOOGLE_CREDENTIALS'):
            logger.error("Missing Google credentials in health check")
            return "Missing Google credentials", 500
        return "OK", 200
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return "Internal server error", 500

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
                logger.info("Twitch token validation successful")
                return True
            else:
                logger.error(f"Twitch token validation failed: HTTP {response.status}")
                return False

async def start_bot_with_delay(bot):
    logger.info("Starting Twitch bot delay...")
    max_retries = 5
    base_delay = 5
    for attempt in range(max_retries):
        await asyncio.sleep(5)
        logger.info(f"Attempting to start bot (attempt {attempt + 1}/{max_retries})...")
        try:
            if await test_twitch_token():
                logger.info("Token is valid, starting bot...")
                await bot.start()
                logger.info("Bot started successfully")
                return
            else:
                logger.error("Invalid token, retrying...")
        except Exception as e:
            logger.error(f"Failed to start Twitch bot: {str(e)}", exc_info=True)
        if attempt < max_retries - 1:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            logger.info(f"Retrying in {delay:.2f} seconds...")
            await asyncio.sleep(delay)
    logger.error("Max retries reached, bot failed to start. Please check TWITCH_TOKEN and network connectivity.")

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
        logger.error(f"Twitch bot error: {str(error)}", exc_info=True)
        if data:
            logger.debug(f"Error data: {data}")

    @bot.command(name='g', aliases=['G'])
    async def guess_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return

            guess = ctx.message.content.split(' ')[1] if len(ctx.message.content.split(' ')) > 1 else None
            if guess and guess.upper() in [f"{chr(65+i)}{j}" for i in range(5) for j in range(1, 6)]:
                await game_state.guess(guess.upper(), ctx.author.name, ctx)
            else:
                logger.debug(f"Ignored invalid command or coordinate: {ctx.message.content}")
                await ctx.send(f"@{ctx.author.name} invalid coordinate! Use format like A1, B3, etc.")
        except Exception as e:
            logger.error(f"ERROR in guess_command: {str(e)}", exc_info=True)
            await ctx.send(f"@{ctx.author.name} an error occurred while processing your guess. Please try again.")

    @bot.command(name='cool')
    async def cool_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return

            if ctx.author.name.lower() == 'nftopia':
                args = ctx.message.content.split(' ')
                if len(args) > 1:
                    new_cooldown = args[1]
                    result = game_state.set_cooldown(new_cooldown)
                    await ctx.send(result)
                else:
                    await ctx.send(f"@{ctx.author.name} please provide a cooldown duration (e.g., !cool 30)")
        except Exception as e:
            logger.error(f"ERROR in cool_command: {str(e)}", exc_info=True)
            if ctx.author.name.lower() == 'nftopia':
                await ctx.send(f"@{ctx.author.name} an error occurred while setting the cooldown. Please try again.")

    @bot.command(name='win')
    async def win_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return

            if ctx.author.name.lower() == 'nftopia':
                args = ctx.message.content.split(' ')
                if len(args) > 1:
                    prize_range = args[1]
                    result = game_state.set_prize(prize_range)
                    await ctx.send(result)
                else:
                    await ctx.send(f"@{ctx.author.name} please provide a prize range (e.g., !win 1-50)")
        except Exception as e:
            logger.error(f"ERROR in win_command: {str(e)}", exc_info=True)
            if ctx.author.name.lower() == 'nftopia':
                await ctx.send(f"@{ctx.author.name} an error occurred while setting the prize range. Please try again.")

    @bot.command(name='testwin')
    async def test_win_command(ctx):
        try:
            if not global_rate_limiter.consume():
                logger.info("Global rate limit exceeded, ignoring command")
                return

            if ctx.author.name.lower() == 'nftopia':
                logger.info(f"Triggering test win event for {ctx.author.name}")
                game_state.notify_event('win', {'winner': ctx.author.name, 'prize': game_state.get_random_prize()})
                await ctx.send(f"@{ctx.author.name} triggered a test win event!")
        except Exception as e:
            logger.error(f"ERROR in test_win_command: {str(e)}", exc_info=True)
            await ctx.send(f"@{ctx.author.name} an error occurred while triggering the test win event.")

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
