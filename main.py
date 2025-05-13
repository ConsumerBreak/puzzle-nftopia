import asyncio
import json
import os
import random
import time
from threading import Thread

import gspread
import requests
from flask import Flask, Response, jsonify, render_template, make_response
from flask_cors import CORS
from google.oauth2.service_account import Credentials
from twitchio.ext import commands

app = Flask(__name__)
CORS(app)

# Version Marker
app.logger.info("Running main.py version 2025-05-14-v2")

# Debug Environment Variables
app.logger.info("Listing environment variable keys: %s", list(os.environ.keys()))

# Twitch Bot Configuration
BOT_TOKEN = os.getenv('TWITCH_BOT_TOKEN')
BOT_NICK = 'break__bot'
app.logger.info(f"TWITCH_BOT_TOKEN is set: {'True' if BOT_TOKEN else 'False'}")
if not BOT_TOKEN:
    app.logger.warning("TWITCH_BOT_TOKEN not set. Bot functionality will be disabled.")
CHANNELS = ['ConsumerBreak']
bot = None

# Game State
game_state = {
    'pieces': {},
    'guesses': {},
    'current_piece': None,
    'natural_section': None,
    'current_image': '',
    'leaderboard': [],
    'sectionMapping': {},
    'reverse_section_mapping': {},
    'baseMapping': {},
    'reverseBaseMapping': {},
    'gameId': 0,
    'pieceId': 0,
    'cooldownSeconds': 60,
    'minPrize': 1,
    'maxPrize': 50
}
puzzle_images = []
image_index = 0
event_queue = []
last_event_timestamp = 0

# Google Sheets Setup
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
SPREADSHEET_ID = '1amJa8alcwRwX-JnhbPjdrAUk16VXxlKjmWwXDCFvjSU'
sheet = None
credentials_json = os.getenv('GOOGLE_CREDENTIALS')
if credentials_json:
    try:
        credentials_dict = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(credentials_dict, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        app.logger.info("Google Sheets initialized successfully")
    except Exception as e:
        app.logger.error(f"Failed to initialize Google Sheets: {str(e)}")
        sheet = None
else:
    app.logger.warning("GOOGLE_CREDENTIALS not set. Leaderboard functionality will be disabled.")

# Initialize Puzzle Images (IPFS)
def init_puzzle_images():
    global puzzle_images, image_index
    ipfs_base_url = "https://gateway.pinata.cloud/ipfs/"
    ipfs_cids = [
        "bafybeice6fyptk5efjq63v5pci5whoqzhn47gwpssgd5g7kowmwmeq3buq",  # puzzle01_00000.png
        "bafybeicgmjfdclavzgt6wchopfnentwvw6c2jj7rk4jv4tbt5uoow7bmcy",  # puzzle02_00000.png
        "bafybeib5pg5el6whrqot3ap23amm6vhzx2n3ukjuac4kvznjornzy5iapq",  # puzzle03_00000.png
        "bafybeihyxddn2p6ymdqa6qtujbphtcg6sftnkjmgp5lxiahl2szirj56ji",  # puzzle04_00000.png
        "bafybeidkmchhllfiyuuamyrwfnh7kqomfo6mrrqrmfxvabqk5c4uakgcia",  # puzzle05_00000.png
        "bafybeic67y5b6iijp3tgt6twilxk2iwlh3owlgpwitq7xravdbcv7jojw4",  # puzzle06_00000.png
        "bafybeigvfq3g6lgazhas6ahq4xt27udj35p7mgndqplldydhz5c3327rau"   # puzzle07_00000.png
    ]
    puzzle_images = [f"{ipfs_base_url}{cid}" for cid in ipfs_cids]
    
    app.logger.info(f"Initialized with {len(puzzle_images)} puzzle images: {puzzle_images}")
    if puzzle_images:
        game_state['current_image'] = puzzle_images[0]
        app.logger.info(f"Set initial current_image to: {game_state['current_image']}")
        image_index = 0
    else:
        app.logger.error("No puzzle images found. Game will not function correctly.")

# Initialize Game
def init_game():
    global image_index
    if not puzzle_images:
        app.logger.error("No puzzle images available. Cannot initialize game.")
        return
    game_state['gameId'] += 1
    app.logger.info(f"Initializing puzzle for game ID {game_state['gameId']}")
    game_state['pieces'] = {}
    game_state['guesses'] = {}
    game_state['pieceId'] = 0
    sections = list(range(25))
    random.shuffle(sections)
    game_state['sectionMapping'] = {str(i): sections[i] for i in range(25)}
    game_state['reverse_section_mapping'] = {str(sections[i]): i for i in range(25)}
    game_state['baseMapping'] = game_state['sectionMapping'].copy()
    game_state['reverseBaseMapping'] = game_state['reverse_section_mapping'].copy()
    app.logger.info(f"Section mapping created: {game_state['sectionMapping']}")
    game_state['current_piece'] = 0
    game_state['natural_section'] = game_state['sectionMapping']['0']
    expected_row = game_state['natural_section'] // 5
    expected_col = game_state['natural_section'] % 5
    expected_coord = f"{chr(65 + expected_row)}{expected_col + 1}"
    app.logger.info(f"Set initial piece: current_piece={game_state['current_piece']}, natural_section={game_state['natural_section']}, expected_coord={expected_coord}")
    image_index = (image_index + 1) % len(puzzle_images)
    game_state['current_image'] = puzzle_images[image_index]
    app.logger.info(f"Selected puzzle image: {game_state['current_image']} (index {image_index + 1})")
    game_state['pieceId'] += 1
    app.logger.info(f"Puzzle initialized: game_id={game_state['gameId']}, piece_id={game_state['pieceId']}")
    send_state_update()

# Load Leaderboard
def load_leaderboard():
    if not sheet:
        app.logger.warning("Google Sheets not available. Returning empty leaderboard.")
        game_state['leaderboard'] = []
        return
    try:
        records = sheet.get_all_values()
        app.logger.info(f"Fetched records from sheet: {records}")
        leaderboard_dict = {}
        for row in records[1:]:
            if len(row) >= 2 and row[0] and row[1]:
                username = row[0].strip()
                try:
                    wins = int(row[1])
                    leaderboard_dict[username] = wins
                except ValueError:
                    continue
        app.logger.info(f"Parsed leaderboard: {leaderboard_dict}")
        game_state['leaderboard'] = sorted(leaderboard_dict.items(), key=lambda x: x[1], reverse=True)
        app.logger.info("Successfully loaded leaderboard from sheet")
    except Exception as e:
        app.logger.error(f"Failed to load leaderboard: {str(e)}")
        game_state['leaderboard'] = []

# Update Leaderboard
def update_leaderboard(username):
    if not sheet:
        app.logger.warning("Google Sheets not available. Skipping leaderboard update.")
        return
    try:
        records = sheet.get_all_values()
        leaderboard_dict = {row[0]: int(row[1]) for row in records[1:] if len(row) >= 2 and row[0] and row[1]}
        leaderboard_dict[username] = leaderboard_dict.get(username, 0) + 1
        updated_records = [['Username', 'Wins']] + [[k, str(v)] for k, v in leaderboard_dict.items()]
        sheet.clear()
        sheet.update('A1', updated_records)
        game_state['leaderboard'] = sorted(leaderboard_dict.items(), key=lambda x: x[1], reverse=True)
    except Exception as e:
        app.logger.error(f"Failed to update leaderboard: {str(e)}")

# Send State Update
def send_state_update():
    global last_event_timestamp
    last_event_timestamp = time.time()
    event = {
        'type': 'state',
        'state': game_state.copy(),
        'event': {},
        'timestamp': last_event_timestamp
    }
    app.logger.info(f"Sent state update event: {json.dumps(event)}")
    event_queue.append(event)

# Twitch Bot
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(token=BOT_TOKEN, prefix='!', initial_channels=CHANNELS, nick=BOT_NICK)

    async def event_ready(self):
        app.logger.info(f"Bot connected to Twitch! Nick: {self.nick}, Channels: {self.connected_channels}")

    async def event_message(self, message):
        if message.author.name.lower() == self.nick.lower():
            return
        await self.handle_commands(message)

    @commands.command()
    async def g(self, message):
        username = message.author.name.lower()
        guess = message.content.split(' ', 1)[1].strip().upper() if ' ' in message.content else None
        if not guess or len(guess) < 2 or guess[0] not in 'ABCDE' or not guess[1:].isdigit() or int(guess[1:]) < 1 or int(guess[1:]) > 5:
            await message.channel.send(f"@{username} Invalid coordinate! Use !g <coordinate> (e.g., !g A1)")
            return
        row = ord(guess[0]) - 65
        col = int(guess[1:]) - 1
        section = row * 5 + col
        natural_section = game_state['natural_section']
        if guess in game_state['pieces']:
            await message.channel.send(f"@{username} That spot is already revealed!")
            return
        if guess in game_state['guesses'] and game_state['guesses'][guess] == 'miss':
            await message.channel.send(f"@{username} You already guessed that spot!")
            return
        game_state['guesses'][guess] = 'miss'
        if section == natural_section:
            game_state['pieces'][guess] = natural_section
            prize = random.randint(game_state['minPrize'], game_state['maxPrize'])
            await message.channel.send(f"@{username} found a piece at {guess}! You won {prize} NFTOKEN!")
            update_leaderboard(username)
            event = {
                'type': 'win',
                'state': game_state.copy(),
                'event': {'winner': username, 'prize': prize},
                'timestamp': time.time()
            }
            event_queue.append(event)
            game_state['current_piece'] += 1
            game_state['pieceId'] += 1
            if game_state['current_piece'] >= 25:
                prize = random.randint(game_state['minPrize'], game_state['maxPrize'])
                await message.channel.send(f"Puzzle completed! Everyone wins {prize} NFTOKEN!")
                event = {
                    'type': 'complete',
                    'state': game_state.copy(),
                    'event': {'prize': prize},
                    'timestamp': time.time()
                }
                event_queue.append(event)
                init_game()
            else:
                game_state['natural_section'] = game_state['sectionMapping'][str(game_state['current_piece'])]
                send_state_update()
        else:
            await message.channel.send(f"@{username} guessed {guess} - no piece there!")
            send_state_update()

# Flask Routes
@app.route('/')
def index():
    app.logger.info("Rendering index.html version 2025-05-14-v2")
    try:
        csp = (
            "default-src 'self' https://pyscript.net https://cdn.jsdelivr.net; "
            "script-src 'self' https://pyscript.net https://cdn.jsdelivr.net 'unsafe-eval' 'unsafe-inline'; "
            "style-src 'self' https://pyscript.net https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self' ws: wss: https://cdn.jsdelivr.net https://pyscript.net https://gateway.pinata.cloud; "
            "img-src 'self' https://gateway.pinata.cloud data:; "
            "worker-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        app.logger.info(f"Applying CSP: {csp}")
        response = make_response(render_template('index.html'))
        response.headers['Content-Security-Policy'] = csp
        return response
    except Exception as e:
        app.logger.error(f"Failed to render index.html: {str(e)}")
        return "Error rendering page", 500

@app.route('/game_state')
def get_game_state():
    load_leaderboard()
    app.logger.info(f"Returning game state with current_image: {game_state['current_image']}")
    return jsonify(game_state)

@app.route('/events')
def events():
    def stream():
        while True:
            if event_queue:
                event = event_queue.pop(0)
                yield f"data: {json.dumps(event)}\n\n"
            time.sleep(0.1)
    return Response(stream(), mimetype='text/event-stream')

# Start Twitch Bot
def run_bot():
    global bot
    if not BOT_TOKEN:
        app.logger.error("Cannot start Twitch bot: TWITCH_BOT_TOKEN is not set")
        return
    app.logger.info("Starting Twitch bot...")
    time.sleep(5)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = Bot()
    try:
        bot.run()
    except Exception as e:
        app.logger.error(f"Twitch bot failed: {str(e)}")
    finally:
        loop.close()

if __name__ == '__main__':
    app.logger.info("Main script starting")
    init_puzzle_images()
    init_game()
    load_leaderboard()
    Thread(target=run_bot, daemon=True).start()
    app.logger.info("Starting Flask on port 10000 with hypercorn")
    from hypercorn.config import Config
    from hypercorn.asyncio import serve
    config = Config()
    config.bind = ["0.0.0.0:10000"]
    asyncio.run(serve(app, config))
