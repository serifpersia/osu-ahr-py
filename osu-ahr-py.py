# -*- coding: utf-8 -*- # Ensure UTF-8 for potential special characters in titles/names
import irc.client
import json
import sys
import signal
import logging
import time
import traceback
import re
import requests
from collections import deque
import threading
from pathlib import Path
import shlex
import math
import copy

# --- Logging Setup ---
import logging

# --- Configuration ---
CONFIG_FILE = Path("config.json")

# Configure the root logger to INFO level to silence debug from irc.client
logging.basicConfig(
    level=logging.INFO,  # Root logger at INFO, so irc.client stays quiet unless INFO or higher
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Get the bot's logger and set it to DEBUG
log = logging.getLogger("OsuIRCBot")
log.setLevel(logging.INFO)  # Only OsuIRCBot logger will show DEBUG messages

# --- Global State ---
shutdown_requested = False
osu_api_token_cache = {'token': None, 'expiry': 0}

# --- Constants ---
OSU_MODES = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}
OSU_STATUSES = ["graveyard", "wip", "pending", "ranked", "approved", "qualified", "loved"] # Common statuses
MAX_LOBBY_SIZE = 16 # osu! standard max size
BOT_STATE_INITIALIZING = "INITIALIZING"
BOT_STATE_CONNECTED_WAITING = "CONNECTED_WAITING" # Connected to IRC, waiting for make/enter
BOT_STATE_JOINING = "JOINING" # In process of joining room
BOT_STATE_IN_ROOM = "IN_ROOM" # Successfully joined and operating in a room
BOT_STATE_SHUTTING_DOWN = "SHUTTING_DOWN"

# --- Simple Event Emitter (Minimal Change) ---
class TypedEvent:
    def __init__(self):
        self._listeners = []

    def on(self, listener):
        self._listeners.append(listener)
        def dispose():
            if listener in self._listeners: self._listeners.remove(listener)
        return dispose

    def emit(self, event_data):
        for listener in self._listeners[:]:
            try:
                listener(event_data)
            except Exception as e:
                log.error(f"Error in event listener: {e}\n{traceback.format_exc()}")

    def off(self, listener):
        if listener in self._listeners: self._listeners.remove(listener)

# --- Helper Function: Get osu! API v2 Token ---
def get_osu_api_token(client_id, client_secret):
    global osu_api_token_cache
    now = time.time()

    if osu_api_token_cache['token'] and now < osu_api_token_cache['expiry']:
        return osu_api_token_cache['token']

    log.info("Fetching new osu! API v2 token...")
    try:
        response = requests.post("https://osu.ppy.sh/oauth/token", data={
            'client_id': client_id, 'client_secret': client_secret,
            'grant_type': 'client_credentials', 'scope': 'public'
        }, timeout=15)
        response.raise_for_status()
        data = response.json()
        osu_api_token_cache['token'] = data['access_token']
        osu_api_token_cache['expiry'] = now + data['expires_in'] - 60
        log.info("Successfully obtained osu! API v2 token.")
        return osu_api_token_cache['token']
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        log.error(f"Failed to get/parse osu! API token: {e}")
        if hasattr(e, 'response') and e.response:
             log.error(f"Response content: {e.response.text[:500]}")
        osu_api_token_cache = {'token': None, 'expiry': 0}
        return None
    except Exception as e:
        log.error(f"Unexpected error getting API token: {e}", exc_info=True)
        osu_api_token_cache = {'token': None, 'expiry': 0}
        return None

# --- Helper Function: Get Beatmap Info ---
def get_beatmap_info(map_id, client_id, client_secret):
    if not client_id or client_secret == "YOUR_CLIENT_SECRET":
        log.warning("osu! API credentials missing/default. Cannot check map.")
        return None

    token = get_osu_api_token(client_id, client_secret)
    if not token:
        log.error("Cannot check map info without API token.")
        return None

    api_url = f"https://osu.ppy.sh/api/v2/beatmaps/{map_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        log.debug(f"Requesting beatmap info for ID: {map_id}")
        response = requests.get(api_url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        log.debug(f"API response for {map_id}: {data}")
        return {
            'stars': data.get('difficulty_rating'),
            'length': data.get('total_length'), # Seconds
            'title': data.get('beatmapset', {}).get('title', 'Unknown Title'),
            'version': data.get('version', 'Unknown Difficulty'),
            'status': data.get('status', 'unknown'), # e.g., ranked, approved, qualified, loved etc.
            'mode': data.get('mode', 'unknown') # e.g., osu, mania, taiko, fruits
        }
    except requests.exceptions.HTTPError as e:
        # 404 is common if the map is restricted or doesn't exist, treat as warning
        if e.response.status_code == 404:
            log.warning(f"HTTP error 404 (Not Found) fetching map {map_id}. It might be deleted or restricted.")
        else:
            log.warning(f"HTTP error fetching map {map_id}: {e.response.status_code}")
            log.error(f"Response: {e.response.text[:500]}")
        return None # Treat 404 as fetch failure
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        log.error(f"Network/JSON error fetching map {map_id}: {e}")
        return None
    except Exception as e:
        log.error(f"Unexpected error fetching map {map_id}: {e}", exc_info=True)
        return None

# --- Helper Function: Save Configuration ---
def save_config(config_data, filepath=CONFIG_FILE):
    try:
        # Create a deep copy to avoid modifying the live config dict before saving
        config_to_save = copy.deepcopy(config_data)

        with filepath.open('w', encoding='utf-8') as f:
            json.dump(config_to_save, f, indent=4, ensure_ascii=False)
        log.info(f"Configuration saved successfully to '{filepath}'.")
        return True
    except (IOError, TypeError, PermissionError) as e:
        log.error(f"Could not save config file '{filepath}': {e}")
        return False
    except Exception as e:
        log.error(f"Unexpected error saving config: {e}", exc_info=True)
        return False


# --- IRC Bot Class ---
class OsuRoomBot(irc.client.SimpleIRCClient):
    def __init__(self, config):
        super().__init__()
        self.config = config # Store the loaded config
        # Create a runtime copy for modifications by admin commands
        # Saving writes the obscured version using save_config helper
        self.runtime_config = copy.deepcopy(config)

        self.target_channel = None # Set when entering/making a room
        self.connection_registered = False
        self.bot_state = BOT_STATE_INITIALIZING # Bot's current operational state

        # Room Specific State (Reset when leaving/stopping)
        self.is_matching = False
        self.host_queue = deque()
        self.current_host = None
        self.last_host = None # Player who last finished a map turn (used for rotation)
        self.host_last_action_time = 0 # For AFK check
        self.host_map_selected_valid = False # True if current host picked map passes checks (pauses AFK timer)
        self.players_in_lobby = set()
        self.current_map_id = 0
        self.current_map_title = ""
        self.last_valid_map_id = 0 # Last map ID that passed validation (used for revert)
        self.map_violations = {} # Tracks map rule violations per host {host: count}
        self.vote_skip_active = False
        self.vote_skip_target = None
        self.vote_skip_initiator = None
        self.vote_skip_voters = set()
        self.vote_skip_start_time = 0
        self.room_was_created_by_bot = False # Track if bot used 'make'
        self.empty_room_close_timer_active = False # Is the auto-close timer running?
        self.empty_room_timestamp = 0 # When did the room become empty?
        self.initial_slot_players = []  # List of (slot_num, player_name) tuples during settings parse
        
        # Make Room State
        self.waiting_for_make_response = False
        self.pending_room_password = None # Store password if provided with 'make'

        # Map Checker Credentials (Loaded once)
        # Use self.config for initial loading, but features use self.runtime_config
        self.api_client_id = self.config.get('osu_api_client_id', 0)
        self.api_client_secret = self.config.get('osu_api_client_secret', '')

        # Events
        self.JoinedLobby = TypedEvent()
        self.PlayerJoined = TypedEvent()
        self.PlayerLeft = TypedEvent()
        self.HostChanged = TypedEvent()
        self.MatchStarted = TypedEvent()
        self.MatchFinished = TypedEvent()
        self.SentMessage = TypedEvent()

        # Initial Validation (API Keys for Map Checker)
        if self.runtime_config['map_checker']['enabled'] and (not self.api_client_id or self.api_client_secret == 'YOUR_CLIENT_SECRET'):
            log.warning("Map checker enabled in config but API keys missing/default. Disabling map check feature.")
            self.runtime_config['map_checker']['enabled'] = False # Override runtime config setting

        log.info("Bot instance initialized.")

    def reset_room_state(self):
        """Clears all state specific to being inside a room."""
        log.info("Resetting internal room state.")
        self.target_channel = None
        self.is_matching = False
        self.host_queue.clear()
        self.current_host = None
        self.last_host = None # Reset last host marker
        self.host_last_action_time = 0
        self.host_map_selected_valid = False
        self.players_in_lobby.clear()
        self.current_map_id = 0
        self.current_map_title = ""
        self.last_valid_map_id = 0 # Reset last valid map
        self.map_violations.clear()
        self.clear_vote_skip("Room state reset") # Also clears vote state
        self.room_was_created_by_bot = False
        self.empty_room_close_timer_active = False
        self.empty_room_timestamp = 0
        # Do NOT reset bot_state here, that's handled by the calling function (leave_room)
        # Do NOT reset waiting_for_make_response or pending_password here

    def log_feature_status(self):
        """Logs the status of major configurable features using runtime_config."""
        if self.bot_state != BOT_STATE_IN_ROOM: return # Only relevant in a room
        hr_enabled = self.runtime_config.get('host_rotation', {}).get('enabled', False)
        mc_enabled = self.runtime_config.get('map_checker', {}).get('enabled', False)
        vs_enabled = self.runtime_config.get('vote_skip', {}).get('enabled', False)
        afk_enabled = self.runtime_config.get('afk_handling', {}).get('enabled', False)
        as_enabled = self.runtime_config.get('auto_start', {}).get('enabled', False)
        ac_enabled = self.runtime_config.get('auto_close_empty_room', {}).get('enabled', False)
        ac_delay = self.runtime_config.get('auto_close_empty_room', {}).get('delay_seconds', 30)
        log.info(f"Features: Rotation:{hr_enabled}, MapCheck:{mc_enabled}, VoteSkip:{vs_enabled}, AFKCheck:{afk_enabled}, AutoStart:{as_enabled}, AutoClose:{ac_enabled}({ac_delay}s)")
        if mc_enabled:
            self.log_map_rules() # Log rules if map check is on

    def announce_setting_change(self, setting_name, new_value):
        """Sends a notification to the chat when an admin changes a setting."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.connection.is_connected() or not self.target_channel:
            log.warning("Cannot announce setting change, not in a room or not connected.")
            return

        message = f"Admin updated setting: {setting_name} set to {new_value}"
        log.info(f"Announcing to chat: {message}")
        self.send_message(message) # Send as single message
        self.log_feature_status() # Re-log feature status after change

    def log_map_rules(self):
        """Logs the current map checking rules to the console using runtime_config."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.runtime_config['map_checker']['enabled']:
            log.info("Map checker is disabled.")
            return
        mc = self.runtime_config['map_checker']
        statuses = self.runtime_config.get('allowed_map_statuses', ['all'])
        modes = self.runtime_config.get('allowed_modes', ['all'])
        log.info(f"Map Rules: Stars {mc.get('min_stars', 'N/A'):.2f}-{mc.get('max_stars', 'N/A'):.2f}, "
                 f"Len {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}, "
                 f"Status: {', '.join(statuses)}, Modes: {', '.join(modes)}")

    def display_map_rules_to_chat(self):
         """Sends map rule information to the chat using runtime_config. Aim for 1-2 messages."""
         if self.bot_state != BOT_STATE_IN_ROOM: return
         messages = []
         if self.runtime_config['map_checker']['enabled']:
             mc = self.runtime_config['map_checker']
             statuses = self.runtime_config.get('allowed_map_statuses', ['all'])
             modes = self.runtime_config.get('allowed_modes', ['all'])
             # Combine into fewer lines
             line1 = f"Rules: Stars {mc.get('min_stars', 'N/A'):.2f}*-{mc.get('max_stars', 'N/A'):.2f}*, Length {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}"
             line2 = f"Status: {', '.join(statuses)}; Modes: {', '.join(modes)}; Violations: {mc.get('violations_allowed', 3)}"
             messages.append(line1)
             messages.append(line2)
         else:
             messages.append("Map checking is currently disabled.")
         self.send_message(messages) # Will send 1 or 2 messages

    def _format_time(self, seconds):
        if seconds is None or not isinstance(seconds, (int, float)) or seconds <= 0:
            return "N/A"
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

    # --- Core IRC Event Handlers ---
    def on_welcome(self, connection, event):
        if self.connection_registered:
            log.info("Received WELCOME event again (reconnect?), already initialized.")
            return
        log.info(f"Connected to {connection.server}:{connection.port} as {connection.get_nickname()}")
        self.connection_registered = True
        self.bot_state = BOT_STATE_CONNECTED_WAITING # Now waiting for user action
        log.info("Bot connected to IRC. Waiting for 'make' or 'enter' command in console.")
        # Consider setting keepalive if needed, though irc library might handle it
        # try:
        #     connection.set_keepalive(60)
        # except Exception as e:
        #     log.error(f"Error setting keepalive: {e}")

    def on_nicknameinuse(self, connection, event):
        old_nick = connection.get_nickname()
        new_nick = old_nick + "_"
        log.warning(f"Nickname '{old_nick}' in use. Trying '{new_nick}'")
        try:
            connection.nick(new_nick)
        except irc.client.ServerNotConnectedError:
            log.warning("Connection lost before nick change.")
            self._request_shutdown("Nickname change failed")

    def _handle_channel_join_error(self, event, error_type):
        channel = event.arguments[0] if event.arguments else "UnknownChannel"
        log.error(f"Cannot join '{channel}': {error_type}.")
        if self.bot_state == BOT_STATE_JOINING and channel.lower() == self.target_channel.lower():
            log.warning(f"Failed to join target channel '{self.target_channel}' ({error_type}). Returning to waiting state.")
            self.send_private_message(self.runtime_config.get('username', 'Bot'), f"Failed to join room {channel}: {error_type}") # Inform user via PM
            self.reset_room_state() # Clear target channel etc.
            self.bot_state = BOT_STATE_CONNECTED_WAITING # Go back to waiting

    def on_err_nosuchchannel(self, c, e): self._handle_channel_join_error(e, "No such channel/Invalid ID")
    def on_err_bannedfromchan(self, c, e): self._handle_channel_join_error(e, "Banned")
    def on_err_channelisfull(self, c, e): self._handle_channel_join_error(e, "Channel full")
    def on_err_inviteonlychan(self, c, e): self._handle_channel_join_error(e, "Invite only")
    def on_err_badchannelkey(self, c, e): self._handle_channel_join_error(e, "Bad key")

    def on_join(self, connection, event):
        channel = event.target
        nick = event.source.nick

        if nick == connection.get_nickname() and channel == self.target_channel:
            log.info(f"Successfully joined {channel}")
            self.bot_state = BOT_STATE_IN_ROOM

            if not self.room_was_created_by_bot:
                 self.reset_room_state()
                 self.target_channel = channel

            if self.runtime_config.get("welcome_message"):
                self.send_message(self.runtime_config["welcome_message"])

            # Request initial state after a delay
            settings_request_delay = 2.0
            log.info(f"Scheduling request for initial settings (!mp settings) in {settings_request_delay}s")
            # Use threading.Timer here is okay, as it just triggers the request later
            threading.Timer(settings_request_delay, self.request_initial_settings).start()
            self.JoinedLobby.emit({'channel': channel})

        elif nick == connection.get_nickname():
            log.info(f"Joined other channel: {channel} (Ignoring)")
        # Do NOT add players to queue/players_in_lobby here, wait for !mp settings or Bancho join message

    def on_part(self, connection, event):
        channel = event.target
        nick = event.source.nick
        if nick == connection.get_nickname() and channel == self.target_channel:
            log.info(f"Left channel {channel}.")
            # If the bot initiated the part (via 'stop' command or auto-close),
            # the state is likely already being handled or will be set to WAITING.
            # If kicked or channel closed unexpectedly (!mp close by user), handle it.
            if self.bot_state == BOT_STATE_IN_ROOM:
                 log.warning(f"Unexpectedly left channel {channel} while in IN_ROOM state (possibly !mp close by user, or kick). Returning to waiting state.")
                 self.reset_room_state()
                 self.bot_state = BOT_STATE_CONNECTED_WAITING

    def on_kick(self, connection, event):
        channel = event.target
        kicked_nick = event.arguments[0]
        if kicked_nick == connection.get_nickname() and channel == self.target_channel:
            log.warning(f"Kicked from channel {channel}. Returning to waiting state.")
            self.reset_room_state()
            self.bot_state = BOT_STATE_CONNECTED_WAITING

    def on_disconnect(self, connection, event):
        reason = event.arguments[0] if event.arguments else "Unknown reason"
        log.warning(f"Disconnected from server: {reason}")
        self.connection_registered = False
        if self.bot_state != BOT_STATE_SHUTTING_DOWN:
            log.warning("Unexpected disconnect, requesting shutdown.")
            self._request_shutdown(f"Disconnected: {reason}")

    def on_privmsg(self, connection, event):
        sender = event.source.nick
        message = event.arguments[0]
        log.info(f"[PRIVATE] <{sender}> {message}")

        # --- Handle !mp make response ---
        if sender == "BanchoBot" and self.waiting_for_make_response:
            log.debug(f"Checking PM from BanchoBot for 'make' response: {message}")
            # Example: "Created the tournament match https://osu.ppy.sh/mp/12345678 My Room Name"
            match = re.search(r"Created the tournament match https://osu\.ppy\.sh/mp/(\d+)", message)
            if match:
                new_room_id = match.group(1)
                log.info(f"Detected newly created room ID: {new_room_id} from BanchoBot PM.")
                self.waiting_for_make_response = False  # Stop waiting

                # Reset room state FIRST to ensure clean slate
                self.reset_room_state()
                self.target_channel = f"#mp_{new_room_id}"
                self.bot_state = BOT_STATE_IN_ROOM  # Transition to IN_ROOM state

                # Mark room as created by bot *after* reset and state change
                self.room_was_created_by_bot = True
                log.info(f"Bot automatically created and joined {self.target_channel}. State set to IN_ROOM. Marked as bot-created.")

                # Send welcome message if configured
                if self.runtime_config.get("welcome_message"):
                    self.send_message(self.runtime_config["welcome_message"])

                # Set password if pending from 'make' command
                if self.pending_room_password:
                    log.info(f"Setting password for room {self.target_channel} to '{self.pending_room_password}' as requested by 'make' command.")
                    self.send_message(f"!mp password {self.pending_room_password}")
                    self.pending_room_password = None  # Clear pending password
                else:
                    # If no password was given in 'make', ensure any osu! default password is cleared
                    log.info(f"No password specified in 'make'. Ensuring room {self.target_channel} has no password.")
                    self.send_message("!mp password") # Clears the password

                # Request initial settings after a short delay
                log.info("Scheduling request for initial settings (!mp settings) in 2.5s")
                threading.Timer(2.5, self.request_initial_settings).start()
                self.JoinedLobby.emit({'channel': self.target_channel})
            else:
                log.warning(f"Received PM from BanchoBot while waiting for 'make' response, but didn't match expected pattern: {message}")
                # Optional: Set a timer to eventually give up waiting_for_make_response
                if self.waiting_for_make_response:
                    def clear_wait_flag():
                        if self.waiting_for_make_response:
                             log.warning("Timeout waiting for BanchoBot 'make' PM response. Resetting flag.")
                             self.waiting_for_make_response = False
                             # Consider returning to WAITING state if stuck?
                             # if self.bot_state == BOT_STATE_JOINING: # Or check if target_channel is still None?
                             #      self.bot_state = BOT_STATE_CONNECTED_WAITING
                    threading.Timer(15.0, clear_wait_flag).start()


    def on_pubmsg(self, connection, event):
        # Ignore public messages if not fully in a room
        if self.bot_state != BOT_STATE_IN_ROOM or not self.target_channel:
            return

        sender = event.source.nick
        channel = event.target
        message = event.arguments[0]

        if channel.lower() != self.target_channel.lower(): return

        # Log user messages, ignore bot's own messages unless debugging needed
        if sender != connection.get_nickname():
            log.info(f"[{channel}] <{sender}> {message}")

        if sender == "BanchoBot":
            self.parse_bancho_message(message)
        else:
            # Handle user commands
            self.parse_user_command(sender, message)

    # --- Room Joining/Leaving ---
    def join_room(self, room_id):
        """Initiates the process of joining a specific room via 'enter' command."""
        if not self.connection.is_connected():
             log.error(f"Cannot join room {room_id}, not connected to IRC.")
             self._request_shutdown("Connection lost")
             return
        if self.bot_state == BOT_STATE_IN_ROOM:
             log.warning(f"Already in room {self.target_channel}. Use 'stop' before joining another.")
             return
        if self.bot_state == BOT_STATE_JOINING:
             log.warning(f"Already attempting to join a room ({self.target_channel}). Please wait.")
             return

        if not str(room_id).isdigit():
            log.error(f"Invalid room ID provided for joining: {room_id}")
            return

        # Ensure the 'created by bot' flag is FALSE for rooms joined via 'enter'
        # This will be handled by reset_room_state called within on_join if successful
        self.room_was_created_by_bot = False
        self.target_channel = f"#mp_{room_id}"
        self.bot_state = BOT_STATE_JOINING # Mark as attempting to join

        log.info(f"Attempting to join channel: {self.target_channel}")
        try:
            self.connection.join(self.target_channel)
            # Success handled by on_join, failure by on_err_*
        except irc.client.ServerNotConnectedError:
            log.warning("Connection lost before join command could be sent.")
            self._request_shutdown("Connection lost")
        except Exception as e:
            log.error(f"Error sending join command for {self.target_channel}: {e}", exc_info=True)
            self.reset_room_state() # Clear target channel
            self.bot_state = BOT_STATE_CONNECTED_WAITING


    # Modify cancellation helper to clear the flag
    def _cancel_pending_initialization(self):
        """Conceptually cancels pending initialization by clearing the flag."""
        # For threading.Timer, we can't easily cancel the timer itself reliably
        # across threads without storing the timer object.
        # Instead, clear the flag that the timer callback checks.
        if hasattr(self, '_initialization_pending') and self._initialization_pending:
             log.info("Cancelling pending initialization by clearing flag.")
             self._initialization_pending = False
        # else: Initialization not pending or flag attribute doesn't exist yet

    # Modify leave_room and shutdown to call the cancellation helper
    def leave_room(self):
        """Leaves the current room and returns to the waiting state."""
        self._cancel_pending_initialization() # Call cancellation helper
        if self.bot_state != BOT_STATE_IN_ROOM or not self.target_channel:
            log.warning("Cannot leave room, not currently in one.")
            return

        if self.empty_room_close_timer_active:
            log.info(f"Manually leaving room '{self.target_channel}'. Cancelling empty room auto-close timer.")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0

        log.info(f"Leaving room {self.target_channel}...")
        current_channel = self.target_channel
        try:
            if self.connection.is_connected():
                self.connection.part(current_channel, "Leaving room (stop command)")
            else:
                log.warning("Cannot send PART command, disconnected.")
        except Exception as e:
            log.error(f"Error sending PART command for {current_channel}: {e}", exc_info=True)
        finally:
            self.reset_room_state()
            self.bot_state = BOT_STATE_CONNECTED_WAITING
            log.info("Returned to waiting state. Use 'make' or 'enter' in console.")


    # --- User Command Parsing (In Room) ---
    def parse_user_command(self, sender, message):
        """Parses commands sent by users IN THE ROOM."""
        if self.bot_state != BOT_STATE_IN_ROOM: return # Should not happen if called correctly
        if not message.startswith("!"): return

        try:
            parts = shlex.split(message)
        except ValueError:
            log.warning(f"Could not parse command with shlex (likely quotes issue): {message}")
            parts = message.split() # Fallback to simple split

        if not parts: return
        command = parts[0].lower()
        args = parts[1:] # Capture arguments

        # Commands available to everyone
        if command == '!queue':
            if self.runtime_config['host_rotation']['enabled']:
                log.info(f"{sender} requested host queue.")
                self.display_host_queue()
            else:
                self.send_message("Host rotation is currently disabled.")
            return

        if command == '!help':
            log.info(f"{sender} requested help.")
            self.display_help_message()
            return

        if command == '!rules':
            log.info(f"{sender} requested rules.")
            self.display_map_rules_to_chat()
            return

        # Vote Skip Command
        if command == '!voteskip':
            vs_config = self.runtime_config['vote_skip']
            if not vs_config['enabled']:
                self.send_message("Vote skipping is disabled.")
                return
            if not self.current_host:
                self.send_message("There is no host to skip.")
                return
            if sender == self.current_host:
                self.send_message("You can't vote to skip yourself! Use !skip if host rotation is enabled.")
                return
            self.handle_vote_skip(sender)
            return

        # Commands available only to current host
        if sender == self.current_host:
            if command == '!skip':
                if self.runtime_config['host_rotation']['enabled']:
                    log.info(f"Host {sender} used !skip.")
                    self.skip_current_host("Host self-skipped")
                else:
                    log.info(f"{sender} tried to use !skip (rotation disabled).")
                    self.send_message("Host rotation is disabled, !skip command is inactive.")
                return

            if command == '!start':
                log.info(f"Host {sender} trying to use !start...")
                if self.is_matching:
                    self.send_message("Match is already in progress.")
                    return
                if self.current_map_id == 0:
                    self.send_message("No map selected to start.")
                    return
                # Check map validity if checker enabled (stricter start)
                if self.runtime_config['map_checker']['enabled'] and not self.host_map_selected_valid:
                   log.warning(f"Host {sender} tried !start but map {self.current_map_id} is not marked valid.")
                   self.send_message(f"Cannot start: Current map ({self.current_map_id}) is invalid or was not checked/accepted.")
                   return

                delay_str = ""
                delay_seconds = 0
                if args:
                    try:
                        delay_seconds = int(args[0])
                        if delay_seconds < 0: delay_seconds = 0
                        if delay_seconds > 300: delay_seconds = 300 # Max reasonable delay
                        delay_str = f" {delay_seconds}"
                    except ValueError:
                        self.send_message("Invalid delay for !start. Use a number like '!start 5'.")
                        return

                log.info(f"Host {sender} sending !mp start{delay_str}")
                self.send_message(f"!mp start{delay_str}")
                # Reset action timer on successful start command ? Maybe not needed, match start handles state.
                # self.host_last_action_time = time.time()
                return

            if command == '!abort':
                log.info(f"Host {sender} sending !mp abort")
                self.send_message("!mp abort")
                # Let Bancho's "Match Aborted" message handle state changes
                return

        # If command wasn't handled and sender wasn't host (or command invalid for host)
        elif command in ['!skip', '!start', '!abort']:
             log.info(f"{sender} tried to use host command '{command}' (not host).")
             self.send_message(f"Only the current host ({self.current_host}) can use {command}.")
             return

        # Ignore any other commands starting with ! from users/hosts
        log.debug(f"Ignoring unknown/restricted command '{command}' from {sender}.")

    def display_help_message(self):
        """Sends help information to the chat. Aim for 2-3 messages max."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        messages = [
            "osu-ahr-py bot help: !queue !skip !voteskip !rules !help",
            "Host Only: !start [delay_seconds] !abort",
            f"Bot Version/Info: [github.com/serifpersia/osu-ahr-py] | Active Features: R({self.runtime_config['host_rotation']['enabled']}) M({self.runtime_config['map_checker']['enabled']}) V({self.runtime_config['vote_skip']['enabled']}) A({self.runtime_config['afk_handling']['enabled']}) S({self.runtime_config['auto_start']['enabled']}) C({self.runtime_config['auto_close_empty_room']['enabled']})"
        ]
        if self.runtime_config['map_checker']['enabled']:
             mc = self.runtime_config['map_checker']
             rule_summary = f"Map Rules: {mc.get('min_stars',0):.2f}-{mc.get('max_stars',0):.2f}*, {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}. Use !rules."
             messages.insert(2, rule_summary) # Insert rules before bot info

        self.send_message(messages)

    # --- BanchoBot Message Parsing (In Room) ---
    def parse_bancho_message(self, msg):
        if self.bot_state != BOT_STATE_IN_ROOM: return # Ignore if not in a room
        log.debug(f"Parsing Bancho: {msg}") # More verbose logging for debugging Bancho interaction
        try:
            # Order matters: Check for more specific messages first
            if msg == "The match has started!": self.handle_match_start()
            elif msg == "The match has finished!": self.handle_match_finish()
            elif msg == "Match Aborted": self.handle_match_abort()
            elif msg == "Closed the match": self.handle_match_close()
            elif msg == "All players are ready": self.handle_all_players_ready()
            elif " became the host." in msg:
                match = re.match(r"(.+?) became the host\.", msg)
                if match: self.handle_host_change(match.group(1))
            elif " joined in slot " in msg:
                match = re.match(r"(.+?) joined in slot \d+\.", msg)
                if match: self.handle_player_join(match.group(1))
            elif " left the game." in msg:
                match = re.match(r"(.+?) left the game\.", msg)
                if match: self.handle_player_left(match.group(1))
            elif " was kicked from the room." in msg:
                 match = re.match(r"(.+?) was kicked from the room\.", msg)
                 if match: self.handle_player_left(match.group(1)) # Treat kick like a leave
            elif "Beatmap changed to: " in msg:
                # Regex to find beatmap ID from /b/, /beatmaps/, or #mode/ links
                # CORRECTED REGEX: Removed the strict follower (?:\s|\?|$) on the first alternative
                map_id_match = re.search(r"/(?:b|beatmaps)/(\d+)|/beatmapsets/\d+#(?:osu|taiko|fruits|mania)/(\d+)", msg)
                map_title_match = re.match(r"Beatmap changed to: (.*?)\s*\(https://osu\.ppy\.sh/.*\)", msg)

                map_id = None
                if map_id_match:
                    # Group 1 captures ID from /b/ or /beatmaps/
                    # Group 2 captures ID from /beatmapsets/...#mode/
                    map_id_str = map_id_match.group(1) or map_id_match.group(2) # Logic remains the same, but group 1 should now match correctly
                    if map_id_str:
                        try:
                            map_id = int(map_id_str)
                            log.debug(f"Extracted map ID {map_id} from map change msg.")
                        except ValueError:
                             log.error(f"Could not convert extracted map ID string '{map_id_str}' to int. Msg: {msg}")
                    # else: # This case should be less likely now
                    #      log.warning(f"Regex matched map change pattern, but no ID group captured? Msg: {msg}")

                if map_id:
                    title = map_title_match.group(1).strip() if map_title_match else "Unknown Title"
                    self.handle_map_change(map_id, title)
                else:
                    # This warning should no longer appear for messages like the example
                    log.warning(f"Could not parse map ID from change msg: {msg}")
                    
            # !mp settings parsing (less critical to be first)
            elif msg.startswith("Room name:"): pass # Ignore for now
            elif msg.startswith("History is "): pass # Ignore history link
            elif msg.startswith("Beatmap: "): self._parse_initial_beatmap(msg)
            elif msg.startswith("Players:"): self._parse_player_count(msg)
            elif msg.startswith("Slot "): 
                self._parse_slot_message(msg)
                log.debug(f"Detected slot message, passing to _parse_slot_message: {msg}")
                
            # Mods/Mode/Condition are used to detect end of !mp settings
            elif msg.startswith("Team mode:") or msg.startswith("Win condition:") or msg.startswith("Active mods:") or msg.startswith("Free mods:"):
                #self.check_initialization_complete(msg) # Trigger final setup after last expected settings line
                pass
            elif " changed the room name to " in msg: pass
            elif " changed the password." in msg: pass
            elif " removed the password." in msg: pass
            elif " changed room size to " in msg:
                self._parse_player_count_from_size_change(msg)
            # Ignore common informational messages
            elif "Stats for" in msg and "are:" in msg: pass
            elif re.match(r"\s*#\d+\s+.+?\s+-\s+\d+\s+-.+", msg): pass # Score lines
            elif msg.startswith("User"): pass # User not found etc.
            # Catch-all for potentially missed/new Bancho messages
            else:
                log.debug(f"Ignoring unrecognized BanchoBot message: {msg}")

        except Exception as e:
            log.error(f"Error parsing Bancho msg: '{msg}' - {e}", exc_info=True)

      
    def _parse_initial_beatmap(self, msg):
            # Regex adjusted for robustness
            map_id_match = re.search(r"/(?:b|beatmaps)/(\d+)|/beatmapsets/\d+#(?:osu|taiko|fruits|mania)/(\d+)", msg) # Use corrected regex here too
            # Simpler title regex: Capture everything after the URL and whitespace
            map_title_match = re.match(r"Beatmap: https?://osu\.ppy\.sh/.*\s+(.+)$", msg) # CORRECTED TITLE REGEX

            map_id = None
            if map_id_match:
                map_id_str = map_id_match.group(1) or map_id_match.group(2)
                if map_id_str:
                    try:
                        map_id = int(map_id_str)
                    except ValueError:
                         log.error(f"Could not convert initial map ID str '{map_id_str}' to int. Msg: {msg}")

            if map_id:
                self.current_map_id = map_id
                # Use the corrected title match result
                self.current_map_title = map_title_match.group(1).strip() if map_title_match else "Unknown Title (from settings)"
                log.info(f"Initial map set from settings: ID {self.current_map_id}, Title: '{self.current_map_title}'") # Log title correctly
                self.last_valid_map_id = 0
                self.host_map_selected_valid = False
            else:
                log.warning(f"Could not parse initial beatmap msg: {msg}")
                self.current_map_id = 0
                self.current_map_title = ""
                self.last_valid_map_id = 0
                self.host_map_selected_valid = False

    

    def _parse_player_count(self, msg):
        match = re.match(r"Players: (\d+)", msg)
        if match:
            num_players = int(match.group(1))
            log.debug(f"Parsed player count from settings: {num_players}")
            # This isn't super reliable for tracking, join/leave messages are better.
            # We mainly use it to know when !mp settings is progressing.
        else:
             log.warning(f"Could not parse player count msg: {msg}")

    def _parse_player_count_from_size_change(self, msg):
        match = re.search(r"changed room size to (\d+)", msg)
        if match:
             log.info(f"Room size changed to {match.group(1)}. Player list updated via join/leave.")
        else:
             log.warning(f"Could not parse size change message: {msg}")

    def _parse_slot_message(self, msg):
        log.debug(f"Attempting to parse slot message: '{msg}'")
        # Flexible regex: handle variable spaces, optional [Host] or [Team...]
        match = re.match(r"Slot (\d+)\s+(Not Ready|Ready)\s+https://osu\.ppy\.sh/u/\d+\s+(.+?)(?:\s+\[(Host|Team.*)\])?\s*$", msg)
        if not match:
            match_empty = re.match(r"Slot (\d+)\s+(Open|Locked)", msg)
            if match_empty:
                log.debug(f"Ignoring empty/locked slot msg: {msg}")
            else:
                log.debug(f"No player match in slot msg (regex failed): {msg}")
            return

        slot = int(match.group(1))
        status = match.group(2)
        player_name = match.group(3).strip()
        host_marker = match.group(4) == "Host" if match.group(4) else False

        log.debug(f"Matched: slot={slot}, status={status}, player_name='{player_name}', host_marker={host_marker}")

        if player_name and player_name.lower() != "banchobot":
            # Store slot and player name for initial ordering
            self.initial_slot_players.append((slot, player_name))
            log.info(f"Parsed slot {slot}: '{player_name}' from !mp settings.")

            # Add player to lobby list if not present
            if player_name not in self.players_in_lobby:
                log.info(f"Adding player '{player_name}' to lobby list from slot {slot}.")
                self.players_in_lobby.add(player_name)
                if self.empty_room_close_timer_active:
                    log.info(f"Player '{player_name}' detected via settings while timer active. Cancelling auto-close timer.")
                    self.empty_room_close_timer_active = False
                    self.empty_room_timestamp = 0

            # Set host if [Host] marker is present
            if host_marker:
                if self.current_host != player_name:
                    log.info(f"Host marker '[Host]' found for '{player_name}' in slot {slot}. Setting as tentative host.")
                    self.current_host = player_name

            # Add to queue if rotation enabled (ordering handled later)
            if self.runtime_config['host_rotation']['enabled']:
                if player_name not in self.host_queue:
                    self.host_queue.append(player_name)
                    log.info(f"Added '{player_name}' to host queue from slot {slot} (ordering pending).")

    def check_initialization_complete(self, last_message_seen):
        """Called when a message indicating the end of !mp settings is seen."""
        # This acts as a trigger to finalize the state based on parsed info
        log.info(f"Assuming !mp settings finished (last seen: '{last_message_seen[:30]}...'). Finalizing initial state.")
        self.initialize_lobby_state()

    def initialize_lobby_state(self):
        """Finalizes lobby state after !mp settings have been parsed (usually via timer)."""
        hr_enabled = self.runtime_config['host_rotation']['enabled']
        mc_enabled = self.runtime_config['map_checker']['enabled']
        # self.current_host might have been set tentatively by _parse_slot_message
        tentative_host = self.current_host # Get the value potentially set during parsing

        log.info(f"Finalizing initial state. Players: {len(self.players_in_lobby)}. Tentative Host from Settings: {tentative_host}. Rotation: {hr_enabled}. Initial Queue: {list(self.host_queue)}")

        # --- Host Rotation Initialization & Host Finalization ---
        if tentative_host:
            # A host WAS identified via [Host] tag in settings. Trust this.
            log.info(f"Host '{tentative_host}' identified via [Host] tag in !mp settings. Confirming internal state.") # <-- Updated Log

            if hr_enabled:
                # Ensure host is in queue and at the front (Existing logic is ok)
                if tentative_host not in self.host_queue:
                    log.warning(f"Confirmed host '{tentative_host}' not found in queue? Adding to front.")
                    self.host_queue.appendleft(tentative_host)
                elif self.host_queue[0] != tentative_host:
                    log.info(f"Moving confirmed host '{tentative_host}' to queue front.")
                    try:
                        self.host_queue.remove(tentative_host)
                        self.host_queue.appendleft(tentative_host)
                    except ValueError:
                        log.error(f"Error moving confirmed host '{tentative_host}' in queue.")

            # Reset timers/state for this CONFIRMED host.
            self.reset_host_timers_and_state(tentative_host)
            # self.current_host is already set correctly. NO !mp host needed.
            # **** ADD CONFIRMATION LOG ****
            log.info(f"Confirmed current host is '{self.current_host}'. No !mp host command needed.")

        else:
            # No host identified via [Host] tag in settings by the time initialization runs
            log.warning("No host identified via [Host] tag during !mp settings parse.") # Existing log is fine

            # **** REVISED PROACTIVE ASSIGNMENT ****
            if hr_enabled and self.host_queue:
                # Rotation ON, queue has players, but NO [Host] tag seen during parse.
                # This is the ONLY case we proactively assign host.
                potential_host = self.host_queue[0]
                log.warning(f"Rotation is ON, no host identified via settings, but players exist. Proactively assigning host to queue front: '{potential_host}'.")
                self.send_message(f"!mp host {potential_host}")
                # Set internal host tentatively; Bancho confirmation will trigger reset_timers via handle_host_change
                self.current_host = potential_host
                # Reset timers now based on this proactive assignment
                self.reset_host_timers_and_state(potential_host)

            elif not hr_enabled:
                 # Rotation OFF, no host tag seen.
                 log.info("Rotation is OFF. Bot will wait for Bancho host confirmation or other events.") # Updated Log
                 self.current_host = None # Ensure host is None
            elif hr_enabled and not self.host_queue:
                 # Rotation ON, but queue is empty (no players parsed).
                 log.info("Rotation is ON, but no players found in queue. Waiting for player join.")
                 self.current_host = None # Ensure host is None
            # **** END REVISED PROACTIVE ASSIGNMENT ****


        # Display queue if rotation enabled
        if hr_enabled:
            log.info(f"Final Initial Queue: {list(self.host_queue)}")
            self.display_host_queue() # Display queue state after potential assignment/confirmation

        # --- Initial Map Check ---
        # Run check ONLY if checker enabled, a host is now CONFIRMED/ASSIGNED, AND a map ID exists
        if mc_enabled and self.current_host and self.current_map_id != 0:
            log.info(f"Proceeding with initial map check for map {self.current_map_id} and current host {self.current_host}.")
            threading.Timer(1.0, self.check_map, args=[self.current_map_id, self.current_map_title]).start()
        elif mc_enabled:
            # Checker enabled, but conditions not met
            self.last_valid_map_id = 0
            self.host_map_selected_valid = False
            if not self.current_host:
                log.info("Initial map check skipped: No host confirmed/assigned yet.") # <-- Updated Log
            elif self.current_map_id == 0:
                log.info("Initial map check skipped: No initial map found.")

        # **** UPDATE FINAL LOG ****
        log.info(f"Initial lobby state setup complete. Final Current Host: {self.current_host}. Players: {list(self.players_in_lobby)}")
        self.log_feature_status() # Log final feature status

        # Check if room is empty right after initialization
        self._check_start_empty_room_timer()


    def request_initial_settings(self):
        if self.bot_state != BOT_STATE_IN_ROOM:
            log.warning("Cannot request settings, not in a room.")
            return
        if hasattr(self, '_initialization_pending') and self._initialization_pending:
            log.warning("Initialization already pending, ignoring.")
            return
        self._initialization_pending = False
        if self.connection.is_connected():
            log.info("Requesting initial state with !mp settings")
            self.send_message("!mp settings")
            init_delay = 5.0  # Increased from 3.5s to 5s
            log.info(f"Scheduling initialization in {init_delay} seconds...")
            self._initialization_pending = True
            timer = threading.Timer(init_delay, self._finalize_initialization_scheduled)
            timer.start()
        else:
            log.warning("Cannot request !mp settings, disconnected.")

    # --- NEW HELPER METHOD ---
    # Helper method called by the threading.Timer
    def _finalize_initialization_scheduled(self):
        """Calls initialize_lobby_state after timer delay, ensuring events are processed first."""
        # Clear the pending flag *before* processing, as this method marks the start of finalization
        if hasattr(self, '_initialization_pending') and self._initialization_pending:
            log.debug("Running timed initialization (_finalize_initialization_scheduled). Clearing pending flag.")
            self._initialization_pending = False
        else:
            # Timer fired but flag was already false? Should not happen if cancellation is correct.
            log.warning("_finalize_initialization_scheduled called but pending flag was not set.")
            # Return here to prevent potential duplicate execution if cancellation failed somehow
            return

        # Check if still in room state before proceeding
        if self.bot_state != BOT_STATE_IN_ROOM:
             log.warning("Timed initialization fired, but bot is no longer in IN_ROOM state. Aborting finalization.")
             return

        log.info("Timed initialization delay complete. Processing pending events before finalizing state...")
        try:
            # --- Explicitly process pending IRC events ---
            # Give it a very small timeout to process anything waiting
            if hasattr(self, 'reactor') and self.reactor:
                 self.reactor.process_once(timeout=0.05)
                 log.debug("Processed pending events.")
            else:
                 log.warning("Cannot process pending events, reactor not found.")
            # --- End event processing ---

            # Now proceed with initialization logic
            log.info("Proceeding with final lobby state setup.")
            self.initialize_lobby_state()
        except Exception as e:
             log.error(f"Error during timed initialization: {e}", exc_info=True)

    # --- Host Rotation & Player Tracking Logic (In Room) ---
    def handle_player_join(self, player_name):
        if player_name.lower() == "banchobot":
            log.debug("Ignoring BanchoBot join event.")
            return

        log.info(f"Player '{player_name}' joined the lobby.")
        self.players_in_lobby.add(player_name)

        if self.empty_room_close_timer_active:
            log.info(f"Player '{player_name}' joined while empty room timer active. Cancelling auto-close timer.")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0

        if self.runtime_config['host_rotation']['enabled']:
            if player_name not in self.host_queue:
                self.host_queue.append(player_name)
                log.info(f"Added '{player_name}' to host queue.")

            if not self.current_host:
                log.info(f"No current host. Assigning '{player_name}' as host.")
                self.send_message(f"!mp host {player_name}")
                self.current_host = player_name
                self.reset_host_timers_and_state(player_name)
            else:
                self.display_host_queue()


    def handle_player_left(self, player_name):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not player_name: return

        log.info(f"Processing player left: '{player_name}'")
        self.PlayerLeft.emit({'player': player_name})
        was_host = (player_name == self.current_host)
        was_last_host = (player_name == self.last_host) # Check if they were marked for rotation

        # Remove from general player list
        if player_name in self.players_in_lobby:
            self.players_in_lobby.remove(player_name)
            log.info(f"'{player_name}' left. Lobby size: {len(self.players_in_lobby)}")
        else:
             log.warning(f"'{player_name}' left but was not in tracked player list?")

        # Host Rotation Handling
        hr_enabled = self.runtime_config['host_rotation']['enabled']
        queue_changed = False
        if hr_enabled:
            if player_name in self.host_queue:
                try:
                    # Create a new deque without the player to preserve order easily
                    new_queue = deque(p for p in self.host_queue if p != player_name)
                    self.host_queue = new_queue
                    log.info(f"Removed '{player_name}' from queue. New Queue: {list(self.host_queue)}")
                    queue_changed = True
                except Exception as e: # Should not fail with list comprehension
                    log.error(f"Unexpected error removing '{player_name}' from queue: {e}", exc_info=True)
            else:
                log.warning(f"'{player_name}' left but not found in queue for removal?")

            # If the player who was marked as 'last_host' (to be rotated) leaves, clear the marker
            if was_last_host:
                log.info(f"Player '{player_name}' who was marked as last_host left. Clearing marker.")
                self.last_host = None

        # Clean up violation count
        if player_name in self.map_violations:
            del self.map_violations[player_name]
            log.debug(f"Removed violation count for leaving player '{player_name}'.")

        # Clean up vote skip if the leaver was involved
        self.clear_vote_skip_if_involved(player_name, "player left")

        # Handle host leaving
        if was_host:
            log.info(f"Host '{player_name}' left.")
            self.current_host = None
            self.host_map_selected_valid = False # No host means no valid map selection
            # If rotation enabled and match isn't running, immediately try to set the next host
            if hr_enabled and not self.is_matching:
                log.info("Host left outside match, attempting to set next host.")
                self.set_next_host() # This will pick from the updated queue
            # If rotation disabled, just clear host
            elif not hr_enabled:
                 log.info("Host left (rotation disabled). Host cleared.")

        # Display updated queue if it changed and rotation is on
        if hr_enabled and queue_changed:
             self.display_host_queue()

        # Check if room is now empty and potentially start auto-close timer
        self._check_start_empty_room_timer()

    def _check_start_empty_room_timer(self):
        """Checks if the room is empty and starts the auto-close timer if applicable."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        ac_config = self.runtime_config['auto_close_empty_room']

        # Conditions to start timer:
        # 1. Feature enabled in runtime_config
        # 2. Room was created by this bot instance
        # 3. Lobby is now empty (excluding bot)
        # 4. Timer is not already active
        is_empty = len(self.players_in_lobby) == 0
        log.debug(f"Checking empty room timer: Enabled={ac_config['enabled']}, BotCreated={self.room_was_created_by_bot}, Empty={is_empty}, TimerActive={self.empty_room_close_timer_active}")

        if (ac_config['enabled'] and
                self.room_was_created_by_bot and
                is_empty and
                not self.empty_room_close_timer_active):

            delay = ac_config['delay_seconds']
            log.info(f"Room '{self.target_channel}' is now empty. Starting {delay}s auto-close timer.")
            self.empty_room_close_timer_active = True
            self.empty_room_timestamp = time.time()
            # self.send_message(f"Room empty. Auto-closing in {delay}s if no one joins.") # Optional: Notify chat

    def handle_host_change(self, player_name):
        """Handles Bancho's 'became the host' message."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not player_name: return
        log.info(f"Bancho reported host changed to: '{player_name}'")

        # If message confirms current host, just reset timers/state
        if player_name == self.current_host:
             log.info(f"Host change message for '{player_name}', who is already the current host. Resetting timers/state.")
             self.reset_host_timers_and_state(player_name)
             return

        previous_host = self.current_host
        self.current_host = player_name
        self.HostChanged.emit({'player': player_name, 'previous': previous_host})

        # Ensure player is in lobby list (should be, but safety check)
        if player_name not in self.players_in_lobby:
             log.warning(f"New host '{player_name}' wasn't in player list, adding.")
             self.handle_player_join(player_name) # Use join logic to add + potentially cancel empty timer

        # Reset timers, violations, valid map flag for the NEW host
        self.reset_host_timers_and_state(player_name)

        # Clear any vote skip targeting the *previous* host
        if self.vote_skip_active and self.vote_skip_target == previous_host:
             self.send_message(f"Host changed to {player_name}. Cancelling vote skip for {previous_host}.")
             self.clear_vote_skip("host changed")
        # Also clear if targeting the new host (e.g., user manually used !mp host during vote)
        elif self.vote_skip_active and self.vote_skip_target == player_name:
             self.send_message(f"Host manually set to {player_name}. Cancelling pending vote skip for them.")
             self.clear_vote_skip("host changed to target")

        # Synchronize host queue if rotation is enabled
        hr_enabled = self.runtime_config['host_rotation']['enabled']
        queue_changed = False
        if hr_enabled:
             log.info(f"Synchronizing queue with new host '{player_name}'. Current Queue: {list(self.host_queue)}")
             if player_name not in self.host_queue:
                 log.info(f"New host '{player_name}' wasn't in queue, adding to front.")
                 self.host_queue.appendleft(player_name) # Add to front
                 queue_changed = True
             elif self.host_queue[0] != player_name:
                 log.warning(f"Host changed to '{player_name}', but they weren't front of queue ({self.host_queue[0] if self.host_queue else 'N/A'}). Moving to front.")
                 try:
                     # Efficiently move to front using remove + appendleft
                     self.host_queue.remove(player_name)
                     self.host_queue.appendleft(player_name)
                     queue_changed = True
                 except ValueError:
                      log.error(f"Failed to reorder queue for new host '{player_name}' - value error despite check.")
             else:
                  log.info(f"New host '{player_name}' is already at the front of the queue.")

             if queue_changed:
                 log.info(f"Queue synchronized. New Queue: {list(self.host_queue)}")
                 self.display_host_queue()

    def reset_host_timers_and_state(self, host_name):
        """Resets AFK timer, map validity flag, and optionally violations for the given host."""
        if self.bot_state != BOT_STATE_IN_ROOM or not host_name: return
        log.debug(f"Resetting timers/state for host '{host_name}'.")
        self.host_last_action_time = time.time() # Reset AFK timer start point
        log.debug(f"*** host_last_action_time set to {self.host_last_action_time:.1f} in reset_host_timers_and_state for {host_name}") # ADDED LOG
        self.host_map_selected_valid = False # New host turn starts with map needing validation

        # Reset map violations only if map checker is enabled
        if self.runtime_config['map_checker']['enabled']:
            # Ensure entry exists before checking/resetting
            self.map_violations.setdefault(host_name, 0)
            if self.map_violations[host_name] != 0:
                 log.info(f"Resetting map violations for host '{host_name}'.")
                 self.map_violations[host_name] = 0

    def rotate_and_set_host(self):
        """Rotates the queue (if needed) and sets the new host via !mp host. Called after match/skip."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        hr_enabled = self.runtime_config['host_rotation']['enabled']

        # --- Rotation Logic ---
        if hr_enabled and len(self.host_queue) > 1:
            log.info(f"Attempting host rotation. Last host: {self.last_host}. Queue Before: {list(self.host_queue)}")

            player_to_rotate = self.last_host # Player who just finished (or was skipped)

            # If the player who just played is still in the queue, move them to the back.
            if player_to_rotate and player_to_rotate in self.host_queue:
                try:
                    # Check if they are currently at the front (common case)
                    if self.host_queue[0] == player_to_rotate:
                        rotated_player = self.host_queue.popleft()
                        self.host_queue.append(rotated_player)
                        log.info(f"Rotated queue: Moved '{rotated_player}' (last host) from front to back.")
                    else:
                        # Less common: host changed mid-match/skip, last host is not at front.
                        # Still move them to the very end.
                        log.warning(f"Last host '{player_to_rotate}' was not at front of queue. Removing and appending to end.")
                        self.host_queue.remove(player_to_rotate)
                        self.host_queue.append(player_to_rotate)
                except Exception as e:
                    log.error(f"Error during queue rotation logic for '{player_to_rotate}': {e}", exc_info=True)
            elif not player_to_rotate:
                log.info("No specific last host marked for rotation (e.g., first round). Front player will proceed.")
                # No rotation needed, just proceed to set_next_host below.
            else: # player_to_rotate is set but not in queue (likely left)
                log.info(f"Last host '{player_to_rotate}' is no longer in queue. No rotation needed for them.")
                # No rotation needed, just proceed to set_next_host below.

            log.info(f"Queue After Rotation Logic: {list(self.host_queue)}")

        elif hr_enabled and len(self.host_queue) == 1:
            log.info("Only one player in queue, no rotation needed.")
        elif hr_enabled: # Queue is empty
             log.warning("Rotation triggered with empty queue. Cannot set host.")
             self.current_host = None # Ensure host is cleared
             self.last_host = None
             return # Exit early
        else: # Host rotation disabled
             log.debug("Host rotation is disabled. Resetting timers for current host (if any).")
             if self.current_host:
                  self.reset_host_timers_and_state(self.current_host)
             self.last_host = None # Clear marker even if rotation off
             return # Exit early


        # --- Set Next Host ---
        # Clear the last_host marker *after* rotation logic but *before* setting new host
        # This ensures it doesn't interfere with the next skip/rotation cycle if host setting fails
        self.last_host = None

        self.set_next_host()
        self.display_host_queue() # Show queue after potential rotation and host set attempt

    def set_next_host(self):
        """Sets the player at the front of the queue as the host via !mp host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.runtime_config['host_rotation']['enabled']: return

        if self.host_queue:
            next_host = self.host_queue[0]
            # Only send !mp host if they aren't already the host according to our state
            if next_host != self.current_host:
                log.info(f"Setting next host to '{next_host}' from queue front via !mp host...")
                self.send_message(f"!mp host {next_host}")
                # We anticipate Bancho's confirmation via handle_host_change to update self.current_host
                # and reset timers/state. Avoid preemptively setting self.current_host here.
            else:
                # If they are already host (e.g., host left, they became host automatically, then rotation was called)
                log.info(f"'{next_host}' is already the current host (or expected). Resetting timers.")
                self.reset_host_timers_and_state(next_host)
        else:
            log.warning("Host queue is empty, cannot set next host.")
            if self.current_host:
                log.info("Clearing previous host as queue is empty.")
                # Maybe send !mp clearhost? Or just clear internal state? Let's clear internal for now.
                self.current_host = None
                self.host_map_selected_valid = False

    def skip_current_host(self, reason="No reason specified"):
        """Skips the current host, rotates queue (if enabled), and sets the next host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.current_host:
            log.warning("Attempted to skip host, but no host is currently assigned.")
            # Try to set next host if rotation on and queue exists
            if self.runtime_config['host_rotation']['enabled'] and self.host_queue:
                 log.info("No current host to skip, but queue exists. Attempting to set next host.")
                 self.set_next_host()
            return

        skipped_host = self.current_host
        log.info(f"Skipping host '{skipped_host}'. Reason: {reason}. Queue Before: {list(self.host_queue)}")

        # Announce skip before clearing vote/rotating
        messages = [f"Host Skipped: {skipped_host}"]
        # Add reason unless it's redundant (e.g. from !voteskip)
        if reason and "vote" not in reason.lower():
             messages.append(f"Reason: {reason}")
        self.send_message(messages)

        # Clean up state related to the skipped host
        self.clear_vote_skip(f"host '{skipped_host}' skipped")
        self.host_map_selected_valid = False # Skipped host's map choice is irrelevant now
        self.current_host = None # Mark host as None immediately

        # If rotation enabled, mark skipped host and trigger rotation/set next
        if self.runtime_config['host_rotation']['enabled']:
            # Mark the skipped host so rotate_and_set_host knows who to move back
            self.last_host = skipped_host
            log.info(f"Marked '{skipped_host}' as last_host for rotation.")
            self.rotate_and_set_host()
        else: # Rotation disabled
            log.warning("Host skipped, but rotation is disabled. Cannot set next host automatically.")
            # Optionally clear host in osu! lobby, though maybe not necessary if someone else takes it
            # self.send_message("!mp clearhost")
            self.last_host = None # Clear marker even if rotation off

    def display_host_queue(self):
        """Sends the current host queue to the chat as a single message if rotation is enabled."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.runtime_config['host_rotation']['enabled']:
            # Optionally log or mention it's disabled if command was explicitly used?
            # For now, just return silently if called internally.
            return
        if not self.connection.is_connected():
             log.warning("Cannot display queue, not connected.")
             return

        if not self.host_queue:
            self.send_message("Host queue is empty.")
            return

        queue_list = list(self.host_queue)
        queue_entries = []
        current_host_name = self.current_host # Use the bot's understanding of current host

        for i, player in enumerate(queue_list):
            entry = f"{player}"
            if player == current_host_name:
                entry += " (Current)"
            # Mark next based on queue position relative to current host
            elif i == 0 and player != current_host_name: # If front is not current host, they are next
                 entry += " (Next)"
            elif i > 0 and queue_list[i-1] == current_host_name: # If previous player was host, this one is next
                 entry += " (Next)"

            queue_entries.append(f"{entry}[{i+1}]") # Add position number

        queue_str = " -> ".join(queue_entries)
        final_message = f"Host order: {queue_str}"
        self.send_message(final_message)

    # --- Match State Handling (In Room) ---
    def handle_match_start(self):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info(f"Match started with map ID {self.current_map_id} ('{self.current_map_title}').")
        self.is_matching = True
        self.last_host = None # Clear last host marker at start
        self.clear_vote_skip("match started")
        self.host_map_selected_valid = False # Flag is irrelevant during match
        self.MatchStarted.emit({'map_id': self.current_map_id})

    def handle_match_finish(self):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info("Match finished.")
        self.is_matching = False

        # Mark the player who was host when the match finished as 'last_host'
        # This is crucial for the rotation logic to know who just had their turn.
        if self.current_host:
             self.last_host = self.current_host
             log.info(f"Marking '{self.last_host}' as last host after match finish.")
        else:
             log.warning("Match finished but no current host was tracked? Rotation might be affected.")
             self.last_host = None # Ensure it's None if host unknown

        self.MatchFinished.emit({})
        self.current_map_id = 0 # Clear map info after finish
        self.current_map_title = ""
        self.last_valid_map_id = 0 # Clear last valid map too? Maybe keep for !retry? Let's clear for now.
        self.host_map_selected_valid = False

        # Trigger host rotation (if enabled) after a short delay
        if self.runtime_config['host_rotation']['enabled']:
            log.info("Scheduling host rotation (1.5s delay) after match finish.")
            threading.Timer(1.5, self.rotate_and_set_host).start()
        else:
             # If rotation disabled, just reset timers for the host who finished
             if self.current_host:
                 self.reset_host_timers_and_state(self.current_host)

    def handle_match_abort(self):
        """Handles 'Match Aborted' message from BanchoBot."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info("BanchoBot reported: Match Aborted")
        self.is_matching = False
        # DO NOT set self.last_host here - the host didn't finish their turn.
        # DO NOT call rotate_and_set_host here - host should remain the same.
        if self.current_host:
            log.info(f"Resetting timers for current host '{self.current_host}' after abort. Host rotation NOT triggered.")
            self.reset_host_timers_and_state(self.current_host)
        # Reset map state as the aborted map is no longer relevant/valid
        self.current_map_id = 0
        self.current_map_title = ""
        self.host_map_selected_valid = False
        # Don't clear last_valid_map_id, host might want to re-pick it.
        # Abort doesn't imply the map itself was bad.

        # Optional: Announce abort? Bancho already does.
        # self.send_message("Match aborted.")

    def handle_match_close(self):
        """Handles 'Closed the match' message from BanchoBot."""
        # This message confirms !mp close was successful.
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info(f"BanchoBot reported: Closed the match ({self.target_channel})")
        # The bot will receive a PART or KICK event shortly after this.
        # on_part / on_kick handles the state reset and transition back to WAITING.
        # No immediate state change needed here, just log confirmation.
        # If the auto-close timer triggered this, it's already deactivated.


    def handle_all_players_ready(self):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info("All players are ready.")
        as_config = self.runtime_config['auto_start']
        if not as_config['enabled'] or self.is_matching:
            log.debug(f"Auto-start check: Disabled({not as_config['enabled']}) or MatchInProgress({self.is_matching}). Skipping.")
            return

        log.debug("Checking conditions for auto-start...")
        map_ok_for_auto_start = True
        reason = ""

        if self.current_map_id == 0:
            map_ok_for_auto_start = False
            reason = "No map selected"
        elif self.runtime_config['map_checker']['enabled'] and not self.host_map_selected_valid:
            map_ok_for_auto_start = False
            reason = f"Map {self.current_map_id} not validated"
            log.warning(f"Auto-start prevented: Map {self.current_map_id} is not marked as valid.")
        elif not self.current_host:
             map_ok_for_auto_start = False
             reason = "No current host"

        if map_ok_for_auto_start:
            delay = max(1, as_config.get('delay_seconds', 5))
            log.info(f"Auto-starting match with map {self.current_map_id} in {delay} seconds.")
            self.send_message(f"!mp start {delay}")
        else:
            log.info(f"Auto-start conditions not met: {reason}.")


    # --- Map Checking Logic (In Room) ---
    def handle_map_change(self, map_id, map_title):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info(f"Map changed to ID: {map_id}, Title: '{map_title}'")
        self.current_map_id = map_id
        self.current_map_title = map_title
        self.host_map_selected_valid = False # ALWAYS reset flag on *any* map change, requires re-validation

        # Reset host AFK timer base time on *any* map change action by host
        if self.current_host:
             self.host_last_action_time = time.time()
             log.debug(f"*** host_last_action_time set to {self.host_last_action_time:.1f} in handle_map_change for {self.current_host}") # REVISED LOG
             
        # Check map if checker enabled AND we have a host
        if self.runtime_config['map_checker']['enabled']:
             if self.current_host:
                 # Use a short delay before check to prevent instant rejection if API is slow
                 # Also allows Bancho's message to fully appear before bot messages
                 threading.Timer(0.5, self.check_map, args=[map_id, map_title]).start()
             else:
                 log.warning("Map changed, but cannot check rules: No current host identified.")
        # If checker disabled, no check needed, AFK timer was reset above.


    def check_map(self, map_id, map_title):
        """Fetches map info and checks against configured rules. Called via handle_map_change."""
        # Re-check conditions as state might have changed during timer delay
        if not self.runtime_config['map_checker']['enabled'] or not self.current_host:
             log.debug("Skipping map check (disabled or no host after delay).")
             self.host_map_selected_valid = False # Ensure flag is false
             return

        # Ensure we are checking the *current* map selected, in case of rapid changes
        if map_id != self.current_map_id:
             log.info(f"Map check for {map_id} aborted, map changed again to {self.current_map_id} before check ran.")
             return

        log.info(f"Checking map {map_id} ('{map_title}') selected by {self.current_host}...")

        # Fetch map info
        info = get_beatmap_info(map_id, self.api_client_id, self.api_client_secret)

        # --- Handle API Failure ---
        if info is None:
            # API failed or map not found (e.g., 404)
            self.reject_map(f"Could not get info for map ID {map_id}. It might not exist, be restricted, or osu! API failed.", is_violation=False) # Not a rule violation
            return # Stop processing this map check

        # --- Extract Info ---
        stars = info.get('stars')
        length = info.get('length') # Seconds
        full_title = info.get('title', 'N/A')
        version = info.get('version', 'N/A')
        status = info.get('status', 'unknown').lower() # Normalize status
        mode_api = info.get('mode', 'unknown') # osu, taiko, fruits, mania

        stars_str = f"{stars:.2f}*" if isinstance(stars, (float, int)) else "N/A*"
        length_str = self._format_time(length)
        map_display_name = f"{full_title} [{version}]"

        log.debug(f"Map Info for {map_id}: Stars={stars_str}, Len={length_str}, Status={status}, Mode={mode_api}")

        # --- Check Rules ---
        violations = []
        mc = self.runtime_config['map_checker']
        allowed_statuses = [s.lower() for s in self.runtime_config.get('allowed_map_statuses', ['all'])]
        allowed_modes = [m.lower() for m in self.runtime_config.get('allowed_modes', ['all'])]

        # Status Check
        if 'all' not in allowed_statuses and status not in allowed_statuses:
            violations.append(f"Status '{status}' not allowed (Allowed: {', '.join(allowed_statuses)})")

        # Mode Check
        if 'all' not in allowed_modes and mode_api not in allowed_modes:
             violations.append(f"Mode '{mode_api}' not allowed (Allowed: {', '.join(allowed_modes)})")

        # Star Rating Check (handle None)
        min_stars = mc.get('min_stars', 0)
        max_stars = mc.get('max_stars', 0)
        if stars is not None:
            # Use a small epsilon for float comparisons
            epsilon = 0.001
            if min_stars > 0 and stars < min_stars - epsilon:
                violations.append(f"Stars ({stars_str}) < Min ({min_stars:.2f}*)")
            if max_stars > 0 and stars > max_stars + epsilon:
                violations.append(f"Stars ({stars_str}) > Max ({max_stars:.2f}*)")
        elif min_stars > 0 or max_stars > 0: # Rule exists but couldn't get stars
            violations.append("Could not verify star rating")

        # Length Check (handle None)
        min_len = mc.get('min_length_seconds', 0)
        max_len = mc.get('max_length_seconds', 0)
        if length is not None:
            if min_len > 0 and length < min_len:
                 violations.append(f"Length ({length_str}) < Min ({self._format_time(min_len)})")
            if max_len > 0 and length > max_len:
                 violations.append(f"Length ({length_str}) > Max ({self._format_time(max_len)})")
        elif min_len > 0 or max_len > 0: # Rule exists but couldn't get length
            violations.append("Could not verify map length")

        # --- Process Result ---
        if violations:
            reason = f"Map Rejected: {'; '.join(violations)}"
            log.warning(f"Map violation by {self.current_host} on map {map_id}: {reason}")
            self.reject_map(reason, is_violation=True) # This IS a rule violation
        else:
            # Map Accepted!
            self.host_map_selected_valid = True # Set flag to pause AFK timer
            self.last_valid_map_id = map_id    # Store this map as the last known good one
            log.info(f"Map {map_id} ('{map_display_name}') accepted for host {self.current_host}. Setting last_valid_map_id to {self.last_valid_map_id}.")
            log.info(f" -> Details: {stars_str}, {length_str}, Status: {status}, Mode: {mode_api}")

            # Reset AFK timer base time again on SUCCESSFUL validation (confirms host action)
            if self.current_host:
                self.host_last_action_time = time.time()
                log.debug(f"*** host_last_action_time set to {self.host_last_action_time:.1f} in check_map (SUCCESS) for {self.current_host}") # REVISED LOG

            # Announce map is okay
            self.send_message(f"Map OK: {map_display_name} ({stars_str}, {length_str}, {status}, {mode_api})")

            # Reset violation count for the host if they picked a valid map
            if self.current_host in self.map_violations and self.map_violations[self.current_host] > 0:
                 log.info(f"Resetting map violation count for {self.current_host} after valid pick.")
                 self.map_violations[self.current_host] = 0


    def reject_map(self, reason, is_violation=True):
        """Handles map rejection, sends messages, increments violation count (if applicable), and attempts to revert map."""
        # Check conditions again, host might have changed or checker disabled rapidly
        if not self.runtime_config['map_checker']['enabled'] or not self.current_host:
            log.warning(f"Map rejection triggered for '{reason}', but checker disabled or no host now. Aborting rejection.")
            return

        rejected_map_id = self.current_map_id
        rejected_map_title = self.current_map_title
        host_at_time_of_rejection = self.current_host # Capture host name for logging/messages

        log.info(f"Rejecting map {rejected_map_id} ('{rejected_map_title}') for host {host_at_time_of_rejection}. Reason: {reason}")

        # Ensure flags are set correctly after rejection
        self.host_map_selected_valid = False
        self.current_map_id = 0 # Mark current map as invalid internally
        self.current_map_title = ""

        # Announce rejection
        self.send_message(f"Map Check Failed for {host_at_time_of_rejection}: {reason}")

        # Attempt to revert map after a short delay
        revert_messages = []
        if self.last_valid_map_id != 0:
             log.info(f"Attempting to revert map to last valid ID: {self.last_valid_map_id} for host {host_at_time_of_rejection}.")
             revert_messages.append(f"!mp map {self.last_valid_map_id}")
        else:
             log.info(f"No previous valid map (last_valid_map_id is 0) to revert to for host {host_at_time_of_rejection}.")
             #revert_messages.append("!mp abortmap") # Alternative: just clear selection? Check osu! behavior. Let's try abortmap.
             # Or maybe just "!mp map 0" if that works? Let's stick to abortmap for now.

        if revert_messages:
             # Delay revert slightly to let rejection message appear first
             threading.Timer(0.5, self.send_message, args=[revert_messages]).start()

        # --- Handle Violations ---
        if not is_violation:
             log.debug("Map rejection was not due to rule violation (e.g., API error). No violation counted.")
             return # Do not count violation or skip host

        violation_limit = self.runtime_config['map_checker'].get('violations_allowed', 3)
        if violation_limit <= 0:
            log.debug("Violation limit is zero or negative, skipping violation tracking/host skip.")
            return # No violations tracked if limit is 0 or less

        # Increment violation count for the host
        count = self.map_violations.get(host_at_time_of_rejection, 0) + 1
        self.map_violations[host_at_time_of_rejection] = count
        log.warning(f"Map violation by {host_at_time_of_rejection} (Count: {count}/{violation_limit}). Map ID: {rejected_map_id}")

        # Check if violation limit reached
        if count >= violation_limit:
            skip_message = f"Map violation limit ({violation_limit}) reached for {host_at_time_of_rejection}. Skipping host."
            log.warning(f"Skipping host {host_at_time_of_rejection} due to map violations limit.")
            # Ensure message is sent before triggering skip logic
            self.send_message(skip_message)
            # Use a timer to allow message delivery before potentially rotating host
            threading.Timer(0.5, self.skip_current_host, args=[f"Reached map violation limit ({violation_limit})"]).start()
        else:
            # Warn the host about the violation
            remaining = violation_limit - count
            warn_message = f"Map Violation ({count}/{violation_limit}) for {host_at_time_of_rejection}. {remaining} remaining. Use !rules to check."
            self.send_message(warn_message)


    # --- Vote Skip Logic (In Room) ---
    def handle_vote_skip(self, voter):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        vs_config = self.runtime_config['vote_skip']
        if not vs_config['enabled'] or not self.current_host: return

        target_host = self.current_host # Target is always the current host
        timeout = vs_config.get('timeout_seconds', 60)

        # Check for existing expired vote first
        if self.vote_skip_active and time.time() - self.vote_skip_start_time > timeout:
                 log.info(f"Previous vote skip for {self.vote_skip_target} expired before new vote.")
                 # Don't send message here, let the new vote start fresh
                 self.clear_vote_skip("timeout before new vote")

        # Start a new vote if none active
        if not self.vote_skip_active:
            self.vote_skip_active = True
            self.vote_skip_target = target_host
            self.vote_skip_initiator = voter
            self.vote_skip_voters = {voter} # Initiator automatically votes 'yes'
            self.vote_skip_start_time = time.time()
            needed = self.get_votes_needed()
            log.info(f"Vote skip initiated by '{voter}' for host '{target_host}'. Needs {needed} votes ({len(self.vote_skip_voters)} already).")
            self.send_message(f"{voter} started vote skip for {target_host}! Type !voteskip to agree. ({len(self.vote_skip_voters)}/{needed})")
            # Check if threshold met immediately (e.g., fixed 1 vote needed)
            if len(self.vote_skip_voters) >= needed:
                 log.info(f"Vote skip threshold ({needed}) met immediately for {target_host}. Skipping.")
                 self.send_message(f"Vote skip passed instantly! Skipping host {target_host}.")
                 # Use timer for skip to allow message delivery
                 threading.Timer(0.5, self.skip_current_host, args=[f"Skipped by player vote ({len(self.vote_skip_voters)}/{needed} votes)"]).start()


        # Add vote to active poll for the correct host
        elif self.vote_skip_active and self.vote_skip_target == target_host:
            if voter in self.vote_skip_voters:
                log.debug(f"'{voter}' tried to vote skip again for {target_host}.")
                # Optional: self.send_message(f"{voter}, you already voted.")
                return

            self.vote_skip_voters.add(voter)
            needed = self.get_votes_needed()
            current_votes = len(self.vote_skip_voters)
            log.info(f"'{voter}' voted to skip '{target_host}'. Votes: {current_votes}/{needed}")

            # Check if threshold met
            if current_votes >= needed:
                log.info(f"Vote skip threshold reached for {target_host}. Skipping.")
                self.send_message(f"Vote skip passed! Skipping host {target_host}.")
                # Use timer for skip to allow message delivery
                threading.Timer(0.5, self.skip_current_host, args=[f"Skipped by player vote ({current_votes}/{needed} votes)"]).start()
            else:
                 # Announce progress
                 self.send_message(f"{voter} voted skip. ({current_votes}/{needed})")

        # Tried to vote skip while vote for *different* host is active
        elif self.vote_skip_active and self.vote_skip_target != target_host:
             log.warning(f"'{voter}' tried !voteskip for current host '{target_host}', but active vote is for '{self.vote_skip_target}'. Ignoring vote.")
             self.send_message(f"Cannot vote for {target_host} now, a vote for {self.vote_skip_target} is already active.")


    def get_votes_needed(self):
        """Calculates the number of votes required to skip the host based on runtime_config."""
        if self.bot_state != BOT_STATE_IN_ROOM: return 999
        vs_config = self.runtime_config['vote_skip']
        threshold_type = vs_config.get('threshold_type', 'percentage')
        threshold_value = vs_config.get('threshold_value', 51)

        # Eligible voters = total players in lobby - current host = N-1
        # Bot is not included in self.players_in_lobby
        eligible_voters = max(0, len(self.players_in_lobby) - 1)

        if eligible_voters < 1:
            return 1 # If only host is left, technically 1 vote (from no one) should pass? Or just impossible? Let's require 1.

        try:
            if threshold_type == 'percentage':
                # Calculate ceil(eligible * percentage / 100)
                needed = math.ceil(eligible_voters * (float(threshold_value) / 100.0))
                return max(1, int(needed)) # Ensure at least 1 vote needed
            elif threshold_type == 'fixed':
                needed = int(threshold_value)
                # Need at least 1, and cannot need more votes than eligible voters
                return max(1, min(needed, eligible_voters))
            else: # Default/fallback to percentage
                log.warning(f"Invalid vote_skip threshold_type '{threshold_type}'. Defaulting to percentage.")
                needed = math.ceil(eligible_voters * (float(threshold_value) / 100.0))
                return max(1, int(needed))
        except ValueError:
             log.error(f"Invalid threshold_value '{threshold_value}' for type '{threshold_type}'. Defaulting to 1 vote needed.")
             return 1

    def clear_vote_skip(self, reason=""):
        """Clears the current vote skip state."""
        if self.vote_skip_active:
            log.info(f"Clearing active vote skip for '{self.vote_skip_target}'. Reason: {reason}")
            self.vote_skip_active = False
            self.vote_skip_target = None
            self.vote_skip_initiator = None
            self.vote_skip_voters.clear()
            self.vote_skip_start_time = 0

    def clear_vote_skip_if_involved(self, player_name, reason="player involved left/kicked"):
        """Clears vote skip if the player was target/initiator, or removes voter and checks threshold."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.vote_skip_active: return

        # If the target host or the initiator leaves, cancel the vote entirely
        if player_name == self.vote_skip_target or player_name == self.vote_skip_initiator:
             log.info(f"Cancelling vote skip for '{self.vote_skip_target}' because {reason}: {player_name}")
             self.send_message(f"Vote skip cancelled ({reason}: {player_name}).")
             self.clear_vote_skip(reason)
        # If a voter (who wasn't initiator) leaves, just remove their vote and re-check
        elif player_name in self.vote_skip_voters:
             self.vote_skip_voters.remove(player_name)
             log.info(f"Removed leaving player '{player_name}' from vote skip voters. Remaining voters: {len(self.vote_skip_voters)}")
             needed = self.get_votes_needed()
             current_votes = len(self.vote_skip_voters)
             # Check if threshold met *after* removing the voter
             if current_votes >= needed:
                  log.info(f"Vote skip threshold reached for {self.vote_skip_target} after voter '{player_name}' left. Skipping.")
                  self.send_message(f"Vote skip passed after voter left! Skipping host {self.vote_skip_target}.")
                  # Use timer for skip to allow message delivery
                  threading.Timer(0.5, self.skip_current_host, args=[f"Skipped by player vote ({current_votes}/{needed} votes after voter left)"]).start()
             else:
                  log.info(f"Vote count now {current_votes}/{needed} after voter left.")
                  # Optional: Announce updated count? Might be noisy.
                  # self.send_message(f"Vote count updated ({current_votes}/{needed}) after player left.")

    def check_vote_skip_timeout(self):
        """Periodically checks if the active vote skip has timed out."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.vote_skip_active: return
        vs_config = self.runtime_config['vote_skip']
        timeout = vs_config.get('timeout_seconds', 60)

        if time.time() - self.vote_skip_start_time > timeout:
            log.info(f"Vote skip for '{self.vote_skip_target}' timed out ({timeout}s).")
            self.send_message(f"Vote to skip {self.vote_skip_target} failed (timeout).")
            self.clear_vote_skip("timeout")


      
# --- AFK Host Handling (In Room) ---
    def check_afk_host(self):
        """Periodically checks if the current host is AFK and skips them if enabled."""
        current_time_check = time.time()

        if self.bot_state != BOT_STATE_IN_ROOM: return
        afk_config = self.runtime_config['afk_handling']

        # Check conditions under which AFK check should NOT run
        if not afk_config['enabled']:
            # log.debug("AFK check: Disabled.") # Can be noisy, comment out if not needed
            return
        if not self.current_host:
            # log.debug("AFK check: No current host.")
            return
        if self.is_matching:
            # log.debug("AFK check: Match in progress.")
            return

        # Core AFK Pause Condition: If host has selected a VALID map, pause the timer
        if self.host_map_selected_valid:
             log.debug(f"AFK check for {self.current_host}: PAUSED (host_map_selected_valid is True). Map: {self.current_map_id}")
             return # Host is considered "active"

        # --- Proceed with timeout check ---
        timeout = afk_config.get('timeout_seconds', 120)
        if timeout <= 0:
            # log.debug("AFK check: Timeout <= 0, check disabled.")
             return # Timeout disabled

        last_action_time = self.host_last_action_time
        time_since_last_action = current_time_check - last_action_time

        # More detailed log BEFORE the check
        log.debug(f"AFK check for {self.current_host}: ValidMap={self.host_map_selected_valid}, "
                  f"IdleTime={time_since_last_action:.1f}s (Current: {current_time_check:.1f}, LastAction: {last_action_time:.1f}), "
                  f"Timeout={timeout}s")

        # Check if timeout exceeded
        if time_since_last_action > timeout:
            # --- Log RIGHT BEFORE skipping ---
            log.warning(f"AFK TIMEOUT MET for host '{self.current_host}'. "
                        f"Idle: {time_since_last_action:.1f}s > {timeout}s. Skipping.")
            # --- End log ---
            self.send_message(f"Host {self.current_host} skipped due to inactivity ({timeout}s+). Please pick a map promptly!")
            # Use timer for skip to allow message delivery
            threading.Timer(0.5, self.skip_current_host, args=[f"AFK timeout ({timeout}s)"]).start()
        else: # Optional log for when timeout NOT met
            log.debug(f"AFK check for {self.current_host}: Timeout NOT met.")

    


    # --- Auto Close Empty Room Check ---
    def check_empty_room_close(self):
        """Checks if an empty, bot-created room's timeout has expired and closes it."""
        if not self.empty_room_close_timer_active:
            return # Timer not running

        # Safety check: If someone joined since timer started, cancel it
        if len(self.players_in_lobby) > 0:
            log.info("check_empty_room_close: Player detected in lobby, cancelling auto-close timer.")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0
            return

        # Check timer expiry
        ac_config = self.runtime_config['auto_close_empty_room']
        delay = ac_config['delay_seconds']
        elapsed_time = time.time() - self.empty_room_timestamp

        log.debug(f"Checking empty room close timer: Elapsed {elapsed_time:.1f}s / {delay}s")

        if elapsed_time >= delay:
            log.warning(f"Empty room '{self.target_channel}' timeout ({delay}s) reached. Sending '!mp close'.")
            try:
                # Deactivate timer immediately *before* sending command to prevent repeats if send fails
                self.empty_room_close_timer_active = False
                self.empty_room_timestamp = 0
                self.send_message("!mp close")
                # State transition back to WAITING will be handled by on_part/on_kick triggered by Bancho.
            except Exception as e:
                log.error(f"Failed to send '!mp close' command: {e}", exc_info=True)
                # Timer already deactivated, just log the error.


    # --- Utility Methods ---
    def send_message(self, message_or_list):
        """Sends a message or list of messages to the current target_channel if in a room."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.target_channel:
            log.warning(f"Cannot send to channel (state={self.bot_state}, channel={self.target_channel}): {message_or_list}")
            return
        self._send_irc_message(self.target_channel, message_or_list)

    def send_private_message(self, recipient, message_or_list):
        """Sends a private message or list of messages to a specific user."""
        # Basic validation
        if not recipient or recipient.isspace():
             log.error(f"Cannot send PM, invalid recipient: '{recipient}'")
             return
        self._send_irc_message(recipient, message_or_list)

    def _send_irc_message(self, target, message_or_list):
        """Internal helper to send IRC messages with rate limiting and truncation."""
        if not self.connection.is_connected():
            log.warning(f"Cannot send, not connected: Target={target}, Msg={message_or_list}")
            return

        messages = message_or_list if isinstance(message_or_list, list) else [message_or_list]
        # Rate limiting: Adjust delay based on target (less delay for PMs?)
        delay = 0.6 if target.startswith("#mp_") else 0.3 # Slightly longer delay for channel messages

        for i, msg in enumerate(messages):
            if not msg: continue # Skip empty messages

            full_msg = str(msg)

            # Basic sanitation: Prevent accidental commands/highlighting if not intended
            if not full_msg.startswith("!") and (full_msg.startswith("/") or full_msg.startswith(".")):
                 log.warning(f"Message starts with potentially unsafe char, prepending space: {full_msg[:30]}...")
                 full_msg = " " + full_msg
            # Prevent self-highlight
            my_nick = self.connection.get_nickname()
            if my_nick and my_nick in full_msg:
                 # Replace with non-breaking space or similar if needed, for now just log
                 log.debug(f"Message contains own nick '{my_nick}'. Sending as is.")

            # Truncation logic (IRC limit is 512 bytes including CRLF, target, etc.)
            # Aim for ~450 bytes for the message payload itself for safety.
            max_len_bytes = 450
            encoded_msg = full_msg.encode('utf-8', 'ignore') # Encode early, ignore errors
            if len(encoded_msg) > max_len_bytes:
                log.warning(f"Truncating long message (>{max_len_bytes} bytes): {encoded_msg[:100]}...")
                # Truncate encoded bytes
                truncated_encoded = encoded_msg[:max_len_bytes]
                # Decode back, ignoring errors, add ellipsis
                try:
                     full_msg = truncated_encoded.decode('utf-8', 'ignore') + "..."
                except Exception: # Fallback if decode somehow fails
                     full_msg = full_msg[:max_len_bytes//4] + "..." # Crude fallback

            try:
                # Apply delay *before* sending the next message (if not the first)
                if i > 0:
                    time.sleep(delay)

                log.info(f"SEND -> {target}: {full_msg}")
                self.connection.privmsg(target, full_msg)
                # Emit event only for channel messages?
                if target == self.target_channel:
                    self.SentMessage.emit({'message': full_msg})

            except irc.client.ServerNotConnectedError:
                log.warning("Failed to send message: Disconnected.")
                self._request_shutdown("Disconnected during send")
                break # Stop trying to send more messages
            except Exception as e:
                log.error(f"Failed to send message to {target}: {e}", exc_info=True)
                time.sleep(1) # Pause briefly after error

    def _request_shutdown(self, reason=""):
        """Internal signal to start shutdown sequence."""
        global shutdown_requested
        if self.bot_state != BOT_STATE_SHUTTING_DOWN:
            log.info(f"Shutdown requested internally. Reason: {reason if reason else 'N/A'}")
            self.bot_state = BOT_STATE_SHUTTING_DOWN
            shutdown_requested = True # Set global flag

    # --- Admin Commands (Called from Console Input Thread) ---
    def admin_skip_host(self, reason="Admin command"):
        if self.bot_state != BOT_STATE_IN_ROOM:
             print("Command failed: Bot is not currently in a room.")
             return
        if not self.connection.is_connected():
             print("Command failed: Not connected to IRC.")
             return
        log.info(f"Admin skip host initiated. Reason: {reason}")
        print(f"Admin: Skipping current host ({self.current_host}). Reason: {reason}")
        # Call skip directly, it handles logging/messaging/rotation
        self.skip_current_host(reason)

    def admin_show_queue(self):
        if self.bot_state != BOT_STATE_IN_ROOM:
            print("Cannot show queue: Bot not in a room.")
            return
        if not self.runtime_config['host_rotation']['enabled']:
            print("Host rotation is disabled.")
            return
        print("--- Host Queue (Console View) ---")
        if not self.host_queue:
            print("(Empty)")
        else:
            q_list = list(self.host_queue)
            current_host_name = self.current_host
            for i, p in enumerate(q_list):
                status = ""
                if p == current_host_name: status = "(Current Host)"
                elif i == 0 and p != current_host_name: status = "(Next Host)"
                elif i > 0 and q_list[i-1] == current_host_name: status = "(Next Host)"
                print(f" {i+1}. {p} {status}")
        print("---------------------------------")

    def admin_show_status(self):
        print("--- Bot Status (Console View) ---")
        print(f"Bot State: {self.bot_state}")
        print(f"IRC Connected: {self.connection.is_connected()}")
        print(f"IRC Nick: {self.connection.get_nickname() if self.connection.is_connected() else 'N/A'}")
        print(f"Target Channel: {self.target_channel if self.target_channel else 'None'}")

        if self.bot_state == BOT_STATE_IN_ROOM:
            print("-" * 10 + " Room Details " + "-" * 10)
            print(f" Current Host: {self.current_host if self.current_host else 'None'}")
            print(f" Last Host (Finished Turn): {self.last_host if self.last_host else 'None'}")
            print(f" Match in Progress: {self.is_matching}")
            print(f" Players in Lobby ({len(self.players_in_lobby)}): {sorted(list(self.players_in_lobby))}")
            print(f" Room Created by Bot: {self.room_was_created_by_bot}")
            print(f" Empty Close Timer: {'ACTIVE (' + str(int(time.time() - self.empty_room_timestamp)) + 's elapsed)' if self.empty_room_close_timer_active else 'Inactive'}")

            print("-" * 10 + " Features (Runtime) " + "-" * 10)
            print(f" Rotation: {self.runtime_config['host_rotation']['enabled']}")
            if self.runtime_config['host_rotation']['enabled']: self.admin_show_queue() # Show queue if rotation on
            print(f" Map Check: {self.runtime_config['map_checker']['enabled']}")
            print(f" Vote Skip: {self.runtime_config['vote_skip']['enabled']}")
            print(f" AFK Check: {self.runtime_config['afk_handling']['enabled']} (Timeout: {self.runtime_config['afk_handling']['timeout_seconds']}s)")
            print(f" Auto Start: {self.runtime_config['auto_start']['enabled']} (Delay: {self.runtime_config['auto_start']['delay_seconds']}s)")
            print(f" Auto Close: {self.runtime_config['auto_close_empty_room']['enabled']} (Delay: {self.runtime_config['auto_close_empty_room']['delay_seconds']}s)")

            print("-" * 10 + " Vote Skip Status " + "-" * 10)
            if self.vote_skip_active:
                 elapsed = time.time() - self.vote_skip_start_time
                 timeout = self.runtime_config['vote_skip'].get('timeout_seconds', 60)
                 print(f" Vote Active: Yes")
                 print(f"  Target: {self.vote_skip_target}")
                 print(f"  Votes: {len(self.vote_skip_voters)} / {self.get_votes_needed()}")
                 print(f"  Voters: {self.vote_skip_voters}")
                 print(f"  Initiator: {self.vote_skip_initiator}")
                 print(f"  Time Left: {max(0, timeout - elapsed):.1f}s")
            else:
                 print(" Vote Active: No")

            print("-" * 10 + " Map Info " + "-" * 10)
            print(f" Current Map ID: {self.current_map_id if self.current_map_id else 'None'} ('{self.current_map_title}')")
            print(f" Host Map Valid Flag: {self.host_map_selected_valid} (Pauses AFK timer)")
            print(f" Last Valid Map ID: {self.last_valid_map_id if self.last_valid_map_id else 'None'} (Used for revert)")
            if self.runtime_config['map_checker']['enabled']:
                print("  Map Rules (Runtime):")
                mc = self.runtime_config['map_checker']
                statuses = self.runtime_config.get('allowed_map_statuses', ['all'])
                modes = self.runtime_config.get('allowed_modes', ['all'])
                print(f"   Stars: {mc.get('min_stars', 0):.2f} - {mc.get('max_stars', 0):.2f}")
                print(f"   Length: {self._format_time(mc.get('min_length_seconds'))} - {self._format_time(mc.get('max_length_seconds'))}")
                print(f"   Status: {', '.join(statuses)}")
                print(f"   Modes: {', '.join(modes)}")
                print(f"   Violations Allowed: {mc.get('violations_allowed', 3)}")
                if self.map_violations: print(f"   Current Violations: {dict(self.map_violations)}")
            else:
                 print("  Map Rules: (Map Check Disabled)")
        print("---------------------------------")

    def shutdown(self, message="Client shutting down."):
        """Initiates shutdown: cancels timers, sends goodbye/QUIT."""
        log.info("Initiating shutdown sequence...")
        self._cancel_pending_initialization()
        self.bot_state = BOT_STATE_SHUTTING_DOWN
        self.empty_room_close_timer_active = False

        conn_available = hasattr(self, 'connection') and self.connection and self.connection.is_connected()

        if conn_available and self.runtime_config.get("goodbye_message") and self.target_channel and self.target_channel.startswith("#mp_"):
            try:
                goodbye_msg = self.runtime_config['goodbye_message']
                log.info(f"Sending goodbye message to {self.target_channel}: '{goodbye_msg}'")
                self.connection.privmsg(self.target_channel, goodbye_msg)
                time.sleep(0.5)
            except Exception as e:
                log.error(f"Error sending goodbye message: {e}")

        log.info(f"Sending QUIT command ('{message}')...")
        try:
            if conn_available:
                self.connection.quit(message)
            else:
                log.warning("Cannot send QUIT, connection not available.")
        except irc.client.ServerNotConnectedError:
            log.warning("Cannot send QUIT, already disconnected.")
        except Exception as e:
            log.error(f"Unexpected error during connection.quit: {e}")
        finally:
            self.connection_registered = False
            if conn_available:
                try:
                    self.connection.disconnect()
                except Exception as e:
                    log.debug(f"Error during explicit disconnect: {e}")


# --- Configuration Loading/Generation ---
def load_or_generate_config(filepath):
    """Loads config from JSON file or generates a default one if not found.
       Prioritizes values from the existing file over defaults using recursive merge."""
    defaults = {
        "server": "irc.ppy.sh",
        "port": 6667,
        "username": "YourOsuUsername",
        "password": "YourOsuIRCPassword", # This should NOT be stored/loaded plainly, but provided at runtime ideally
        "welcome_message": "osu-ahr-py connected! Use !help for commands.",
        "goodbye_message": "Bot disconnecting.",
        "osu_api_client_id": 0, # Get from osu! website OAuth settings
        "osu_api_client_secret": "YOUR_CLIENT_SECRET", # Get from osu! website OAuth settings
        "map_checker": {
            "enabled": True,
            "min_stars": 0.0,
            "max_stars": 10.0, # 0 means no limit
            "min_length_seconds": 0, # 0 means no limit
            "max_length_seconds": 600, # Default 10 mins, 0 means no limit
            "violations_allowed": 3 # Skips host after this many invalid picks in a row
        },
        "allowed_map_statuses": ["ranked", "approved", "qualified", "loved"], # Add "graveyard", "pending", "wip" if desired
        "allowed_modes": ["all"], # ["osu", "taiko", "fruits", "mania"] or ["all"]
        "host_rotation": {
            "enabled": True
        },
        "vote_skip": {
            "enabled": True,
            "timeout_seconds": 60,
            "threshold_type": "percentage", # "percentage" or "fixed"
            "threshold_value": 51 # 51% or 51 votes (adjust based on type)
        },
        "afk_handling": {
            "enabled": True,
            "timeout_seconds": 120 # Seconds host can be idle (no valid map selected) before being skipped
        },
        "auto_start": {
            "enabled": False, # Enable cautiously, can be annoying if people aren't ready
            "delay_seconds": 5 # Seconds after "All players ready"
        },
        "auto_close_empty_room": {
            "enabled": True,      # Enable by default
            "delay_seconds": 60   # Timeout in seconds (increased default)
        }
    }

    # --- Recursive Merge Logic ---
    def merge_configs(base, updates):
        merged = base.copy()
        for key, value in updates.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                merged[key] = merge_configs(merged[key], value) # Recurse
            elif key not in ["password", "osu_api_client_secret"] or value not in ["", "YourOsuIRCPassword", "YOUR_CLIENT_SECRET"]:
                # Only update if it's not a sensitive default or an empty value overwriting a default
                # Exception: Allow overwriting non-sensitive defaults like username=""
                 if key not in ["password", "osu_api_client_secret"] or value:
                     merged[key] = value
            # else: Keep the default for sensitive fields if user provided default/empty
        return merged

    try:
        if not filepath.exists():
            log.warning(f"Config file '{filepath}' not found. Generating default config.")
            log.warning(">>> IMPORTANT: Edit 'config.json' with your osu! username, IRC password (from osu! settings page), and osu! API credentials (from osu! OAuth settings) before running again! <<<")
            # Use defaults directly for generation
            config_to_write = defaults.copy()
            # Obscure sensitive fields even in the default file written
            config_to_write['password'] = "YourOsuIRCPassword"
            config_to_write['osu_api_client_secret'] = "YOUR_CLIENT_SECRET"

            try:
                with filepath.open('w', encoding='utf-8') as f:
                    json.dump(config_to_write, f, indent=4, ensure_ascii=False)
                log.info(f"Default config file created at '{filepath}'. Please edit it and restart.")
                # Return defaults for this run, user needs to restart after editing
                return defaults
            except (IOError, PermissionError) as e:
                 log.critical(f"FATAL: Could not write default config file '{filepath}': {e}")
                 sys.exit(1)

        else:
            log.info(f"Loading configuration from '{filepath}'...")
            with filepath.open('r', encoding='utf-8') as f:
                user_config = json.load(f)

            # Merge user config onto defaults
            final_config = merge_configs(defaults, user_config)

            # --- Validation & Type Coercion ---
            required = ["server", "port", "username", "password", "osu_api_client_id", "osu_api_client_secret"]
            missing_or_default = []
            for k in required:
                val = final_config.get(k)
                is_missing = val is None
                is_default_sensitive = (k == "password" and val == "YourOsuIRCPassword") or \
                                     (k == "osu_api_client_secret" and val == "YOUR_CLIENT_SECRET") or \
                                     (k == "osu_api_client_id" and val == 0)
                is_default_username = (k == "username" and val == "YourOsuUsername")
                if is_missing or is_default_sensitive or is_default_username:
                    missing_or_default.append(k)

            if missing_or_default:
                log.critical(f"FATAL: Missing or default required config keys in '{filepath}': {', '.join(missing_or_default)}. Please edit the file with your actual credentials.")
                log.critical(" - Get IRC Password from: https://osu.ppy.sh/home/account/edit#legacy-api")
                log.critical(" - Get API Credentials from: https://osu.ppy.sh/home/account/edit#oauth")
                sys.exit(1) # Exit if critical info is missing/default

            # Ensure correct types for numeric/bool fields, using defaults if missing/wrong type
            final_config["port"] = int(final_config.get("port", defaults["port"]))
            final_config["osu_api_client_id"] = int(final_config.get("osu_api_client_id", defaults["osu_api_client_id"]))

            for section_key, default_section in defaults.items():
                 if isinstance(default_section, dict): # Validate nested dictionaries
                     user_section = final_config.get(section_key, {})
                     if not isinstance(user_section, dict):
                         log.warning(f"Config section '{section_key}' is not a dictionary. Resetting to default.")
                         final_config[section_key] = default_section
                         user_section = final_config[section_key] # Use the reset value

                     # Coerce types within the section based on defaults
                     for key, default_value in default_section.items():
                         user_value = user_section.get(key, default_value) # Use default if key missing
                         try:
                             if isinstance(default_value, bool): user_section[key] = bool(user_value)
                             elif isinstance(default_value, float): user_section[key] = float(user_value)
                             elif isinstance(default_value, int): user_section[key] = int(user_value)
                             elif isinstance(default_value, str): user_section[key] = str(user_value)
                             # Add list handling if needed
                         except (ValueError, TypeError):
                             log.warning(f"Invalid type for '{section_key}.{key}'. Found '{type(user_value).__name__}', expected '{type(default_value).__name__}'. Using default: {default_value}")
                             user_section[key] = default_value

            # List type validation
            for key in ["allowed_map_statuses", "allowed_modes"]:
                 if not isinstance(final_config.get(key), list):
                     log.warning(f"Config key '{key}' is not a list. Resetting to default.")
                     final_config[key] = defaults[key]

            # Specific value constraints
            if final_config['auto_close_empty_room']['delay_seconds'] < 5:
                log.warning("auto_close_empty_room delay_seconds cannot be less than 5. Setting to 5.")
                final_config['auto_close_empty_room']['delay_seconds'] = 5
            if final_config['auto_start']['delay_seconds'] < 1:
                log.warning("auto_start delay_seconds cannot be less than 1. Setting to 1.")
                final_config['auto_start']['delay_seconds'] = 1
            if final_config['afk_handling']['timeout_seconds'] <= 0:
                 if final_config['afk_handling']['enabled']:
                     log.warning("AFK handling enabled but timeout is <= 0. Setting timeout to 120s.")
                     final_config['afk_handling']['timeout_seconds'] = 120

            log.info(f"Configuration loaded and validated successfully from '{filepath}'.")
            return final_config

    except (json.JSONDecodeError, TypeError) as e:
        log.critical(f"FATAL: Error parsing config file '{filepath}': {e}. Please check its JSON format.")
        sys.exit(1)
    except Exception as e:
        log.critical(f"FATAL: Unexpected error loading config: {e}", exc_info=True)
        sys.exit(1)

# --- Signal Handling ---
def signal_handler(sig, frame):
    global shutdown_requested
    if not shutdown_requested:
        log.info(f"Shutdown signal ({signal.Signals(sig).name}) received. Stopping gracefully...")
        shutdown_requested = True
    else:
        log.warning("Shutdown already in progress.")


# --- Console Input Thread ---
import shlex
import logging
import time

# Assume BOT_STATE_IN_ROOM, MAX_LOBBY_SIZE, OSU_STATUSES, OSU_MODES constants exist
# Assume self.runtime_config, self.target_channel, etc. are attributes of bot_instance
# Assume bot_instance has methods like send_message, _request_shutdown, leave_room,
#       admin_skip_host, admin_show_queue, admin_show_status, save_config,
#       announce_setting_change, _format_time, request_initial_settings,
#       check_map, display_host_queue, etc.
# Assume log is a configured logger instance
# Assume shutdown_requested is a global boolean

log = logging.getLogger("OsuIRCBot") # Placeholder for logger

# --- Console Input Thread ---
def console_input_loop(bot_instance):
    """Handles admin commands entered in the console, adapting based on bot state."""
    global shutdown_requested

    # Wait brief moment for bot to potentially connect or fail early
    time.sleep(1.0)

    # Exit thread early if initial connection failed and triggered shutdown
    if shutdown_requested:
        log.info("Console input thread exiting early (shutdown requested during init/connection).")
        return

    log.info("Console input thread active. Type 'help' for available commands.")

    while not shutdown_requested:
        try:
            # Determine prompt based on state
            current_state = bot_instance.bot_state # Cache state
            if current_state == BOT_STATE_CONNECTED_WAITING:
                prompt = "make/enter/quit > "
            elif current_state == BOT_STATE_IN_ROOM:
                prompt = f"ADMIN [{bot_instance.target_channel}] > "
            elif current_state == BOT_STATE_JOINING:
                prompt = "ADMIN (joining...) > "
            elif current_state == BOT_STATE_INITIALIZING:
                 prompt = "ADMIN (initializing...) > "
                 time.sleep(0.5) # Wait if still initializing
                 continue
            elif current_state == BOT_STATE_SHUTTING_DOWN:
                 log.info("Console input loop stopping (shutdown in progress).")
                 break # Exit loop if shutting down
            else: # Unknown state?
                prompt = "ADMIN (?) > "
                time.sleep(0.5)
                continue

            # Get input (use try-except for potential EOFError on pipe close/etc)
            try:
                command_line = input(prompt).strip()
            except EOFError:
                log.info("Console input closed (EOF). Requesting shutdown.")
                bot_instance._request_shutdown("Console EOF")
                break
            except KeyboardInterrupt:
                # This should be caught by the main thread's signal handler, but handle here too just in case
                if not shutdown_requested:
                    log.info("Console KeyboardInterrupt. Requesting shutdown.")
                    bot_instance._request_shutdown("Console Ctrl+C")
                break

            if not command_line: continue # Skip empty input

            # Parse command
            try:
                parts = shlex.split(command_line)
            except ValueError:
                 log.warning(f"Could not parse console command with shlex: {command_line}")
                 parts = command_line.split() # Fallback

            if not parts: continue
            command = parts[0].lower()
            args = parts[1:]

            # --- State-Specific Command Handling ---

            # Commands always available (except during shutdown)
            if command in ["quit", "exit"]:
                log.info(f"Console requested quit (State: {current_state}).")
                bot_instance._request_shutdown("Console quit command")
                break
            elif command == "status":
                 bot_instance.admin_show_status()
                 continue # Don't fall through to unknown command

            # --- Commands available when CONNECTED_WAITING ---
            if current_state == BOT_STATE_CONNECTED_WAITING:
                if command == "enter":
                    if len(args) != 1 or not args[0].isdigit():
                        print("Usage: enter <room_id_number>")
                        log.warning("Invalid 'enter' command usage.")
                        continue
                    room_id = args[0]
                    print(f"Attempting to enter room mp_{room_id}...")
                    log.info(f"Console: Requesting join for room mp_{room_id}")
                    bot_instance.join_room(room_id) # join_room handles state change to JOINING

                elif command == "make":
                    if not args:
                        print("Usage: make <\"Room Name\"> [password]")
                        log.warning("Invalid 'make' command usage (no name).")
                        continue
                    room_name = args[0] # shlex handles quotes
                    password = args[1] if len(args) > 1 else None
                    print(f"Attempting to make room '{room_name}'...")
                    log.info(f"Console: Requesting room creation: Name='{room_name}', Password={'Yes' if password else 'No'}")
                    bot_instance.pending_room_password = password
                    bot_instance.waiting_for_make_response = True
                    # Send PM to BanchoBot to make the room
                    bot_instance.send_private_message("BanchoBot", f"!mp make {room_name}")
                    print("Room creation command sent. Waiting for BanchoBot PM with room ID...")
                    # Bot state remains WAITING until PM received or timeout

                elif command == "help":
                     print("\n--- Available Commands (Waiting State) ---")
                     print("  enter <room_id>      - Join an existing multiplayer room by ID.")
                     print("  make <\"name\"> [pass] - Create a new room (use quotes if name has spaces).")
                     print("  status               - Show current bot status.")
                     print("  quit / exit          - Disconnect and exit the application.")
                     print("-------------------------------------------\n")

                else:
                    print(f"Unknown command '{command}' in waiting state. Use 'enter', 'make', 'status', or 'quit'. Type 'help'.")


            # --- Commands available when IN_ROOM ---
            elif current_state == BOT_STATE_IN_ROOM:
                config_changed = False
                setting_name_for_announce = None
                value_for_announce = None

                # Bot Control
                if command == "stop":
                    log.info("Console requested stop (leave room).")
                    print("Leaving room and returning to make/enter state...")
                    bot_instance.leave_room() # Handles state change to WAITING

                # >>> START NEW COMMAND <<<
                elif command == "close_room":
                    log.info(f"Admin requested closing room {bot_instance.target_channel} via !mp close.")
                    print(f"Sending '!mp close' to {bot_instance.target_channel}...")
                    # Cancel auto-close timer if active, as we are closing manually
                    if bot_instance.empty_room_close_timer_active:
                         log.info("Admin closing room, cancelling active empty room timer.")
                         bot_instance.empty_room_close_timer_active = False
                         bot_instance.empty_room_timestamp = 0
                    bot_instance.send_message("!mp close")
                    # State change back to WAITING is handled by on_part/on_kick event
                # >>> END NEW COMMAND <<<

                elif command == "skip":
                    reason = " ".join(args) if args else "Admin command"
                    bot_instance.admin_skip_host(reason)

                elif command in ["queue", "q", "showqueue"]:
                    bot_instance.admin_show_queue()

                # Lobby Settings (!mp commands) - These are NOT saved to config
                elif command == "say":
                     if not args: print("Usage: say <message to send>"); continue
                     msg_to_send = " ".join(args)
                     print(f"Admin forcing send: {msg_to_send}")
                     bot_instance.send_message(msg_to_send)

                elif command == "set_password":
                     if not args: print("Usage: set_password <new_password|clear>"); continue
                     pw = args[0]
                     if pw.lower() == 'clear':
                         print("Admin: Sending !mp password (to clear).")
                         bot_instance.send_message("!mp password")
                     else:
                         print(f"Admin: Sending !mp password {pw}")
                         bot_instance.send_message(f"!mp password {pw}")

                elif command == "set_size":
                     if not args or not args[0].isdigit(): print("Usage: set_size <number 1-16>"); continue
                     try:
                         size = int(args[0])
                         if 1 <= size <= MAX_LOBBY_SIZE:
                             print(f"Admin: Sending !mp size {size}")
                             bot_instance.send_message(f"!mp size {size}")
                         else:
                             print(f"Invalid size. Must be between 1 and {MAX_LOBBY_SIZE}.")
                     except ValueError:
                         print("Invalid number for size.")

                elif command == "set_name":
                     if not args: print("Usage: set_name <\"new lobby name\">"); continue
                     name = " ".join(args) # shlex handles quotes
                     print(f"Admin: Sending !mp name {name}")
                     bot_instance.send_message(f"!mp name {name}")

                # Bot Feature Toggles & Settings (Saved to runtime_config and config.json)
                elif command == "set_rotation":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_rotation <true|false>"); continue
                    hr_config = bot_instance.runtime_config['host_rotation']
                    value = args[0].lower() in ['true', 'on']
                    if hr_config['enabled'] != value:
                        hr_config['enabled'] = value
                        print(f"Admin set Host Rotation to: {value}")
                        setting_name_for_announce = "Host Rotation"
                        value_for_announce = value
                        config_changed = True
                        # If enabling and queue is empty, request settings to populate
                        if value and not bot_instance.host_queue and bot_instance.connection.is_connected():
                             log.info("Rotation enabled by admin with empty queue, requesting !mp settings to populate.")
                             bot_instance.request_initial_settings()
                        elif not value: # If disabling
                             bot_instance.host_queue.clear()
                             log.info("Rotation disabled by admin. Queue cleared.")
                             if bot_instance.connection.is_connected(): bot_instance.send_message("Host rotation disabled by admin. Queue cleared.")
                        else: # If enabling and queue not empty
                              bot_instance.display_host_queue() # Show current queue after enabling
                    else:
                        print(f"Host Rotation already set to {value}.")

                elif command == "set_map_check":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_map_check <true|false>"); continue
                    mc_config = bot_instance.runtime_config['map_checker']
                    value = args[0].lower() in ['true', 'on']
                    if mc_config['enabled'] != value:
                        # Check API keys before enabling
                        if value and (not bot_instance.api_client_id or bot_instance.api_client_secret == 'YOUR_CLIENT_SECRET'):
                             print("ERROR: Cannot enable map check: API credentials missing/default in config.json.")
                             log.warning("Admin attempted to enable map check without valid API keys.")
                             continue # Prevent enabling

                        mc_config['enabled'] = value
                        print(f"Admin set Map Checker to: {value}")
                        setting_name_for_announce = "Map Checker"
                        value_for_announce = value
                        config_changed = True
                        # If enabling, re-check current map if one is selected
                        if value and bot_instance.current_map_id != 0 and bot_instance.current_host and bot_instance.connection.is_connected():
                            log.info("Map checker enabled by admin, re-validating current map.")
                            bot_instance.check_map(bot_instance.current_map_id, bot_instance.current_map_title)
                    else:
                        print(f"Map Checker already set to {value}.")

                elif command == "set_auto_start":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_auto_start <true|false>"); continue
                    as_config = bot_instance.runtime_config['auto_start']
                    value = args[0].lower() in ['true', 'on']
                    if as_config['enabled'] != value:
                        as_config['enabled'] = value
                        print(f"Admin set Auto Start to: {value}")
                        setting_name_for_announce = "Auto Start"
                        value_for_announce = value
                        config_changed = True
                    else:
                        print(f"Auto Start already set to {value}.")

                elif command == "set_auto_close":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_auto_close <true|false>"); continue
                    ac_config = bot_instance.runtime_config['auto_close_empty_room']
                    value = args[0].lower() in ['true', 'on']
                    if ac_config['enabled'] != value:
                        ac_config['enabled'] = value
                        print(f"Admin set Auto Close Empty Room to: {value}")
                        setting_name_for_announce = "Auto Close Empty Room"
                        value_for_announce = value
                        config_changed = True
                        # If disabled, cancel any active timer immediately
                        if not value and bot_instance.empty_room_close_timer_active:
                             log.info("Auto-close disabled by admin, cancelling active timer.")
                             bot_instance.empty_room_close_timer_active = False
                             bot_instance.empty_room_timestamp = 0
                    else:
                        print(f"Auto Close Empty Room already set to {value}.")

                elif command == "set_auto_close_delay":
                    if not args or not args[0].isdigit(): print("Usage: set_auto_close_delay <seconds>"); continue
                    try:
                        value = int(args[0])
                        if value < 5: print("Delay must be at least 5 seconds."); continue
                        ac_config = bot_instance.runtime_config['auto_close_empty_room']
                        if ac_config['delay_seconds'] != value:
                            ac_config['delay_seconds'] = value
                            print(f"Admin set Auto Close Delay to: {value} seconds")
                            setting_name_for_announce = "Auto Close Delay"
                            value_for_announce = f"{value}s"
                            config_changed = True
                        else:
                            print(f"Auto Close Delay already set to {value} seconds.")
                    except ValueError:
                        print("Invalid number for delay seconds.")

                elif command == "set_auto_start_delay":
                    if not args or not args[0].isdigit(): print("Usage: set_auto_start_delay <seconds>"); continue
                    try:
                        value = int(args[0])
                        if value < 1: print("Delay must be at least 1 second."); continue
                        as_config = bot_instance.runtime_config['auto_start']
                        if as_config['delay_seconds'] != value:
                            as_config['delay_seconds'] = value
                            print(f"Admin set Auto Start Delay to: {value} seconds")
                            setting_name_for_announce = "Auto Start Delay"
                            value_for_announce = f"{value}s"
                            config_changed = True
                        else:
                            print(f"Auto Start Delay already set to {value} seconds.")
                    except ValueError:
                        print("Invalid number for delay seconds.")

                # Map Rule Settings
                elif command == "set_star_min":
                   if not args: print("Usage: set_star_min <number|0>"); continue
                   try:
                       value = float(args[0])
                       if value < 0: print("Min stars cannot be negative."); continue
                       mc_config = bot_instance.runtime_config['map_checker']
                       current_value = mc_config.get('min_stars', 0)
                       if abs(current_value - value) > 0.001: # Compare floats carefully
                           mc_config['min_stars'] = value
                           print(f"Admin set Minimum Star Rating to: {value:.2f}*")
                           setting_name_for_announce = "Min Stars"
                           value_for_announce = f"{value:.2f}*"
                           config_changed = True
                       else:
                           print(f"Minimum Star Rating already set to {value:.2f}*")
                   except ValueError: print("Invalid number for minimum stars.")

                elif command == "set_star_max":
                    if not args: print("Usage: set_star_max <number|0>"); continue
                    try:
                        value = float(args[0])
                        if value < 0: print("Max stars cannot be negative."); continue
                        mc_config = bot_instance.runtime_config['map_checker']
                        current_value = mc_config.get('max_stars', 0)
                        if abs(current_value - value) > 0.001:
                            mc_config['max_stars'] = value
                            print(f"Admin set Maximum Star Rating to: {value:.2f}*")
                            setting_name_for_announce = "Max Stars"
                            value_for_announce = f"{value:.2f}*"
                            config_changed = True
                        else:
                            print(f"Maximum Star Rating already set to {value:.2f}*")
                    except ValueError: print("Invalid number for maximum stars.")

                elif command == "set_len_min": # Renamed for clarity, use seconds
                    if not args: print("Usage: set_len_min <seconds|0>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Min length cannot be negative."); continue
                        mc_config = bot_instance.runtime_config['map_checker']
                        current_value = mc_config.get('min_length_seconds', 0)
                        if current_value != value:
                            mc_config['min_length_seconds'] = value
                            formatted_time = bot_instance._format_time(value)
                            print(f"Admin set Minimum Map Length to: {formatted_time} ({value}s)")
                            setting_name_for_announce = "Min Length"
                            value_for_announce = formatted_time
                            config_changed = True
                        else:
                            print(f"Minimum Map Length already set to {bot_instance._format_time(value)}")
                    except ValueError: print("Invalid number for minimum length seconds.")

                elif command == "set_len_max": # Renamed for clarity, use seconds
                    if not args: print("Usage: set_len_max <seconds|0>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Max length cannot be negative."); continue
                        mc_config = bot_instance.runtime_config['map_checker']
                        current_value = mc_config.get('max_length_seconds', 0)
                        if current_value != value:
                            mc_config['max_length_seconds'] = value
                            formatted_time = bot_instance._format_time(value)
                            print(f"Admin set Maximum Map Length to: {formatted_time} ({value}s)")
                            setting_name_for_announce = "Max Length"
                            value_for_announce = formatted_time
                            config_changed = True
                        else:
                             print(f"Maximum Map Length already set to {bot_instance._format_time(value)}")
                    except ValueError: print("Invalid number for maximum length seconds.")

                elif command == "set_statuses":
                    valid_statuses_lower = [s.lower() for s in OSU_STATUSES] + ['all']
                    if not args:
                        current = ', '.join(bot_instance.runtime_config.get('allowed_map_statuses', ['all']))
                        print(f"Current allowed statuses: {current}")
                        print(f"Usage: set_statuses <status1> [status2...] or 'all'")
                        print(f"Available: {', '.join(OSU_STATUSES)}")
                        continue

                    input_statuses = [s.lower() for s in args]
                    if 'all' in input_statuses:
                         value = ['all']
                    else:
                        value = sorted([s for s in input_statuses if s in valid_statuses_lower and s != 'all'])
                        if not value: # Check if any valid statuses were entered
                            print(f"No valid statuses provided. Use 'all' or values from: {', '.join(OSU_STATUSES)}")
                            continue # Prevent setting empty list unless 'all' was intended

                    current_value = sorted(bot_instance.runtime_config.get('allowed_map_statuses', ['all']))
                    if current_value != value:
                        bot_instance.runtime_config['allowed_map_statuses'] = value
                        display_value = ', '.join(value)
                        print(f"Admin set Allowed Map Statuses to: {display_value}")
                        setting_name_for_announce = "Allowed Statuses"
                        value_for_announce = display_value
                        config_changed = True
                    else:
                        print(f"Allowed Map Statuses already set to: {', '.join(value)}")

                elif command == "set_modes":
                    valid_modes_lower = [m.lower() for m in OSU_MODES.values()] + ['all']
                    if not args:
                        current = ', '.join(bot_instance.runtime_config.get('allowed_modes', ['all']))
                        print(f"Current allowed modes: {current}")
                        print(f"Usage: set_modes <mode1> [mode2...] or 'all'")
                        print(f"Available: {', '.join(OSU_MODES.values())}")
                        continue

                    input_modes = [m.lower() for m in args]
                    if 'all' in input_modes:
                        value = ['all']
                    else:
                        value = sorted([m for m in input_modes if m in valid_modes_lower and m != 'all'])
                        if not value:
                            print(f"No valid modes provided. Use 'all' or values from: {', '.join(OSU_MODES.values())}")
                            continue

                    current_value = sorted(bot_instance.runtime_config.get('allowed_modes', ['all']))
                    if current_value != value:
                        bot_instance.runtime_config['allowed_modes'] = value
                        display_value = ', '.join(value)
                        print(f"Admin set Allowed Game Modes to: {display_value}")
                        setting_name_for_announce = "Allowed Modes"
                        value_for_announce = display_value
                        config_changed = True
                    else:
                        print(f"Allowed Game Modes already set to: {', '.join(value)}")

                # Help Command (In Room)
                elif command == "help":
                   print("\n--- Admin Console Commands (In Room) ---")
                   print(" Room Control:")
                   print("  stop                - Leave the current room, return to make/enter state.")
                   print("  close_room          - Send '!mp close' to Bancho to close the room.") # <<< UPDATED HELP
                   print("  skip [reason]       - Force skip the current host.")
                   print("  queue / q           - Show the current host queue (console).")
                   print("  status / info       - Show detailed bot and lobby status (console).")
                   print(" Lobby Settings (!mp) - Not saved to bot config:")
                   print("  say <message>       - Send a message as the bot to the lobby.")
                   print("  set_password <pw|clear> - Set/remove the lobby password.")
                   print("  set_size <1-16>     - Change the lobby size.")
                   print("  set_name <\"name\">   - Change the lobby name.")
                   print(" Bot Feature Toggles & Settings - Saved to config.json:")
                   print("  set_rotation <t/f>  - Enable/Disable host rotation.")
                   print("  set_map_check <t/f> - Enable/Disable map checker (needs API keys).")
                   print("  set_auto_start <t/f>- Enable/Disable auto starting match when ready.")
                   print("  set_auto_close <t/f>- Enable/Disable auto closing empty bot-created rooms.")
                   print("  set_auto_start_delay <sec> - Set delay (sec >=1) for auto start.")
                   print("  set_auto_close_delay <sec> - Set delay (sec >=5) for auto close.")
                   print(" Map Rules (Need Map Check Enabled) - Saved to config.json:")
                   print("  set_star_min <N>    - Set min star rating (0=off). Ex: set_star_min 4.5")
                   print("  set_star_max <N>    - Set max star rating (0=off). Ex: set_star_max 6.0")
                   print("  set_len_min <sec>   - Set min map length seconds (0=off). Ex: set_len_min 90")
                   print("  set_len_max <sec>   - Set max map length seconds (0=off). Ex: set_len_max 300")
                   print("  set_statuses <...>  - Set allowed map statuses (ranked, loved, etc. or 'all').")
                   print("  set_modes <...>     - Set allowed game modes (osu, mania, etc. or 'all').")
                   print(" General:")
                   print("  quit / exit         - Disconnect bot and exit application.")
                   print("  help                - Show this help message.")
                   print("------------------------------------------\n")

                # --- Unknown Command Handling (In Room) ---
                else:
                   print(f"Unknown command: '{command}' while in room. Type 'help' for options.")

                # --- Save Config if Changed by Admin ---
                if config_changed:
                    # Save the potentially modified runtime_config
                    if save_config(bot_instance.runtime_config): # save_config handles obscuring sensitive fields
                        print("Configuration changes saved to config.json.")
                        # Announce the change to chat if possible
                        if setting_name_for_announce and value_for_announce is not None:
                            bot_instance.announce_setting_change(setting_name_for_announce, value_for_announce)
                    else:
                        print("ERROR: Failed to save configuration changes to file.")
                        log.error("Failed to save runtime_config changes to config.json after admin command.")

            # --- Commands available while JOINING/INITIALIZING ---
            elif current_state in [BOT_STATE_JOINING, BOT_STATE_INITIALIZING]:
                 # Allow 'status' and 'quit'/'exit' (handled globally above)
                 if command not in ['status', 'quit', 'exit']:
                      print(f"Command '{command}' ignored while {current_state}. Please wait. Use 'status' or 'quit'.")

            # --- Fallback for unknown state or unhandled command ---
            # else: # This case should ideally not be reached if all states are handled
            #      print(f"Command '{command}' not applicable in current state ({current_state}).")


        except Exception as e:
            log.error(f"Error in console input loop: {e}", exc_info=True)
            print(f"\nAn error occurred processing the command: {e}")
            print(prompt, end='') # Reprint prompt


    log.info("Console input thread finished.")

# --- Main Execution ---
def main():
    global shutdown_requested

    config = load_or_generate_config(CONFIG_FILE)
    if not config: # load_or_generate_config now exits on critical failure
        sys.exit(1)

    # Validate credentials loaded from config before trying to connect
    if not config.get("username") or config["username"] == "YourOsuUsername" or \
       not config.get("password") or config["password"] == "YourOsuIRCPassword":
        log.critical("FATAL: Username or IRC Password missing/default in config.json. Please edit the file.")
        sys.exit(1)
    # API keys needed only if map check enabled, but warn if default
    if config['map_checker']['enabled'] and (not config.get("osu_api_client_id") or config.get("osu_api_client_secret") == "YOUR_CLIENT_SECRET"):
         log.warning("Map checker is enabled, but osu! API credentials missing/default in config.json. Map checking will be disabled.")
         # Bot init handles disabling it in runtime_config if keys invalid

    bot = None
    try:
        bot = OsuRoomBot(config) # Pass the loaded config
        bot.bot_state = BOT_STATE_INITIALIZING
    except Exception as e:
        log.critical(f"Failed to initialize OsuRoomBot: {e}", exc_info=True)
        sys.exit(1)

    # Setup signal handling for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start console input thread
    console_thread = threading.Thread(target=console_input_loop, args=(bot,), daemon=True, name="AdminConsoleThread")
    console_thread.start()

    # --- Connection Attempt ---
    log.info(f"Connecting to {config['server']}:{config['port']} as {config['username']}...")
    connection_successful = False
    try:
        # Connect using validated credentials from config
        bot.connect(
            server=config['server'],
            port=config['port'],
            nickname=config['username'],
            password=config['password'], # Use actual password from loaded config
            username=config['username'] # Often same as nickname for Bancho
        )
        connection_successful = True # Assume success if no exception before process_forever
    except irc.client.ServerConnectionError as e:
        log.critical(f"IRC Connection failed: {e}")
        err_str = str(e).lower()
        if "nickname is already in use" in err_str: log.critical(" -> Try changing 'username' in config.json or ensure no other client is using it.")
        elif "incorrect password" in err_str or "authentication failed" in err_str: log.critical(" -> Incorrect IRC password. Get/Check from osu! website account settings (Legacy API section).")
        elif "cannot assign requested address" in err_str or "temporary failure in name resolution" in err_str: log.critical(f" -> Network error connecting to {config['server']}. Check server address and your internet connection.")
        else: log.critical(f" -> Unhandled server connection error: {e}")
        bot._request_shutdown("Connection Error") # Trigger shutdown process
    except Exception as e:
        log.critical(f"Unexpected error during bot.connect call: {e}", exc_info=True)
        bot._request_shutdown("Connect Exception")

    # --- Main Loop ---
    if connection_successful:
        log.info("Starting main processing loop...")
        last_periodic_check = time.time()
        check_interval = 5 # Seconds between periodic checks

        while not shutdown_requested:
            try:
                # Process IRC events with a short timeout
                bot.reactor.process_once(timeout=0.2)

                # --- Periodic Checks (Only run when fully connected and in a room) ---
                current_state = bot.bot_state # Cache state
                if current_state == BOT_STATE_IN_ROOM and bot.connection.is_connected():
                    now = time.time()
                    if now - last_periodic_check >= check_interval:
                        bot.check_afk_host()
                        bot.check_vote_skip_timeout()
                        bot.check_empty_room_close()
                        last_periodic_check = now
                elif current_state == BOT_STATE_SHUTTING_DOWN:
                     break # Exit loop if shutdown requested

            except irc.client.ServerNotConnectedError:
                if bot.bot_state != BOT_STATE_SHUTTING_DOWN:
                    log.warning("Disconnected during processing loop. Requesting shutdown.")
                    bot._request_shutdown("Disconnected in main loop")
                break # Exit loop on disconnect
            except KeyboardInterrupt:
                 # Signal handler should catch this, but break just in case
                 if not shutdown_requested:
                     log.info("Main loop KeyboardInterrupt. Requesting shutdown.")
                     bot._request_shutdown("Main loop Ctrl+C")
                 break
            except Exception as e:
                # Log unexpected errors in the loop but try to continue
                log.error(f"Unhandled exception in main loop: {e}", exc_info=True)
                time.sleep(2) # Pause briefly after an unknown error

    # --- Shutdown Sequence ---
    log.info("Main loop exited or connection failed. Initiating final shutdown...")
    # Ensure shutdown flag is set if not already
    if not shutdown_requested:
        shutdown_requested = True
        if bot and bot.bot_state != BOT_STATE_SHUTTING_DOWN:
             bot.bot_state = BOT_STATE_SHUTTING_DOWN

    # Call bot's shutdown method if instance exists
    if bot:
        bot.shutdown("Client shutting down normally.") # Handles QUIT etc.

    # Wait briefly for console thread to potentially finish based on shutdown_requested flag
    log.info("Waiting for console thread to exit...")
    console_thread.join(timeout=2.0)
    if console_thread.is_alive():
        log.warning("Console thread did not exit cleanly.")

    log.info("osu-ahr-py finished.")


if __name__ == "__main__":
    main_exit_code = 0
    try:
        main()
    except SystemExit as e:
         log.info(f"Program exited with code {e.code}.")
         main_exit_code = e.code
    except KeyboardInterrupt:
         # This might catch Ctrl+C during initial setup before signal handler is fully active
         log.info("\nMain execution interrupted by Ctrl+C during startup. Exiting.")
         main_exit_code = 1
    except Exception as e:
        # Catch any truly unexpected top-level errors
        log.critical(f"CRITICAL UNHANDLED ERROR during execution: {e}", exc_info=True)
        main_exit_code = 1
    finally:
        logging.shutdown() # Ensure log handlers are flushed
        sys.exit(main_exit_code) # Exit with appropriate code