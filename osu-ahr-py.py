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

# --- Configuration ---
CONFIG_FILE = Path("config.json")

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("OsuIRCBot")

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
    # ... (Keep the TypedEvent class as it was) ...
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
    # ... (Keep get_osu_api_token as it was) ...
    global osu_api_token_cache
    now = time.time()

    if osu_api_token_cache['token'] and now < osu_api_token_cache['expiry']:
        return osu_api_token_cache['token']

    log.info("Fetching new osu! API v2 token...")
    try:
        response = requests.post("https://osu.ppy.sh/oauth/token", data={
            'client_id': client_id, 'client_secret': client_secret,
            'grant_type': 'client_credentials', 'scope': 'public'
        }, timeout=15) # Increased timeout slightly
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
    # ... (Keep get_beatmap_info as it was) ...
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
        response = requests.get(api_url, headers=headers, timeout=15) # Increased timeout
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
        log.warning(f"HTTP error fetching map {map_id}: {e.response.status_code}")
        if e.response.status_code != 404:
             log.error(f"Response: {e.response.text[:500]}")
        return None
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        log.error(f"Network/JSON error fetching map {map_id}: {e}")
        return None
    except Exception as e:
        log.error(f"Unexpected error fetching map {map_id}: {e}", exc_info=True)
        return None

# --- Helper Function: Save Configuration ---
def save_config(config_data, filepath=CONFIG_FILE):
    # ... (Keep save_config as it was) ...
    try:
        with filepath.open('w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
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
        self.config = config
        self.target_channel = None # Set when entering/making a room
        self.connection_registered = False
        self.bot_state = BOT_STATE_INITIALIZING # Bot's current operational state

        # Room Specific State (Reset when leaving/stopping)
        self.is_matching = False
        self.host_queue = deque()
        self.current_host = None
        self.last_host = None
        self.host_last_action_time = 0
        self.host_map_selected_valid = False
        self.players_in_lobby = set()
        self.current_map_id = 0
        self.current_map_title = ""
        self.last_valid_map_id = 0
        self.map_violations = {}
        self.vote_skip_active = False
        self.vote_skip_target = None
        self.vote_skip_initiator = None
        self.vote_skip_voters = set()
        self.vote_skip_start_time = 0
        # --- NEW State Variables for Auto-Close ---
        self.room_was_created_by_bot = False # Track if bot used 'make'
        self.empty_room_close_timer_active = False # Is the auto-close timer running?
        self.empty_room_timestamp = 0 # When did the room become empty?
        # --- END NEW State Variables ---

        # Make Room State
        self.waiting_for_make_response = False
        self.pending_room_password = None # Store password if provided with 'make'

        # Map Checker Credentials (Loaded once)
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
        if self.config['map_checker']['enabled'] and (not self.api_client_id or self.api_client_secret == 'YOUR_CLIENT_SECRET'):
            log.warning("Map checker enabled in config but API keys missing/default. Disabling map check feature.")
            self.config['map_checker']['enabled'] = False # Override config setting if keys invalid

        log.info("Bot instance initialized.")

    def reset_room_state(self):
        """Clears all state specific to being inside a room."""
        log.info("Resetting internal room state.")
        self.target_channel = None
        self.is_matching = False
        self.host_queue.clear()
        self.current_host = None
        self.last_host = None
        self.host_last_action_time = 0
        self.host_map_selected_valid = False
        self.players_in_lobby.clear()
        self.current_map_id = 0
        self.current_map_title = ""
        self.last_valid_map_id = 0
        self.map_violations.clear()
        self.clear_vote_skip("Room state reset") # Also clears vote state
        # --- NEW Resets ---
        self.room_was_created_by_bot = False
        self.empty_room_close_timer_active = False
        self.empty_room_timestamp = 0
        # --- End New Resets ---
        # Do NOT reset bot_state here, that's handled by the calling function (leave_room)
        # Do NOT reset waiting_for_make_response or pending_password here

    def log_feature_status(self):
        """Logs the status of major configurable features."""
        if self.bot_state != BOT_STATE_IN_ROOM: return # Only relevant in a room
        hr_enabled = self.config.get('host_rotation', {}).get('enabled', False)
        mc_enabled = self.config.get('map_checker', {}).get('enabled', False)
        vs_enabled = self.config.get('vote_skip', {}).get('enabled', False)
        afk_enabled = self.config.get('afk_handling', {}).get('enabled', False)
        as_enabled = self.config.get('auto_start', {}).get('enabled', False)
        ac_enabled = self.config.get('auto_close_empty_room', {}).get('enabled', False) # NEW
        ac_delay = self.config.get('auto_close_empty_room', {}).get('delay_seconds', 30) # NEW
        log.info(f"Features: Rotation:{hr_enabled}, MapCheck:{mc_enabled}, VoteSkip:{vs_enabled}, AFKCheck:{afk_enabled}, AutoStart:{as_enabled}, AutoClose:{ac_enabled}({ac_delay}s)") # Updated Log
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
        """Logs the current map checking rules to the console."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.config['map_checker']['enabled']:
            log.info("Map checker is disabled.")
            return
        mc = self.config['map_checker']
        statuses = self.config.get('allowed_map_statuses', ['all'])
        modes = self.config.get('allowed_modes', ['all'])
        log.info(f"Map Rules: Stars {mc.get('min_stars', 'N/A')}-{mc.get('max_stars', 'N/A')}, "
                 f"Len {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}, "
                 f"Status: {', '.join(statuses)}, Modes: {', '.join(modes)}")

    def display_map_rules_to_chat(self):
         """Sends map rule information to the chat. Aim for 1-2 messages."""
         if self.bot_state != BOT_STATE_IN_ROOM: return
         messages = []
         if self.config['map_checker']['enabled']:
             mc = self.config['map_checker']
             statuses = self.config.get('allowed_map_statuses', ['all'])
             modes = self.config.get('allowed_modes', ['all'])
             # Combine into fewer lines
             line1 = f"Rules: Stars {mc.get('min_stars', 'N/A')}*-{mc.get('max_stars', 'N/A')}*, Length {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}"
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
            self._request_shutdown()

    def _handle_channel_join_error(self, event, error_type):
        channel = event.arguments[0] if event.arguments else "UnknownChannel"
        log.error(f"Cannot join '{channel}': {error_type}.")
        if self.bot_state == BOT_STATE_JOINING and channel.lower() == self.target_channel.lower():
            log.warning(f"Failed to join target channel '{self.target_channel}' ({error_type}). Returning to waiting state.")
            self.send_private_message(self.config.get('username', 'Bot'), f"Failed to join room {channel}: {error_type}") # Inform user via PM
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
            self.bot_state = BOT_STATE_IN_ROOM # Officially in the room now
            # Reset state *after* confirming join, but *before* setting flags based on how we joined
            # Note: reset_room_state is called within on_privmsg for `make` or here for `enter`.
            # We need to ensure room_was_created_by_bot is set *after* the reset if it was a `make`.
            # For `enter`, the reset correctly sets it to False.

            # If we got here via 'enter', reset_room_state() was not called yet by on_privmsg. Call it now.
            if not self.room_was_created_by_bot: # If it's true, on_privmsg already reset and set it.
                 self.reset_room_state()
                 self.target_channel = channel # Re-set target channel after reset

            # Send welcome message if configured
            if self.config.get("welcome_message"):
                self.send_message(self.config["welcome_message"]) # Single message

            # Set password if pending from 'make' command (handled in on_privmsg now)
            # if self.pending_room_password: ... removed ...

            # Request initial state after a short delay
            threading.Timer(2.5, self.request_initial_settings).start()
            self.JoinedLobby.emit({'channel': channel})
        elif nick == connection.get_nickname():
            log.info(f"Joined other channel: {channel}")
        # Do NOT add players to queue/players_in_lobby here, wait for !mp settings or Bancho join message

    def on_part(self, connection, event):
        channel = event.target
        nick = event.source.nick
        if nick == connection.get_nickname() and channel == self.target_channel:
            log.info(f"Left channel {channel}.")
            # If the bot initiated the part (via 'stop' command), the state is already reset and set to WAITING
            # If kicked or channel closed, we might need to handle it.
            if self.bot_state == BOT_STATE_IN_ROOM: # Part was unexpected (kick, close?)
                 log.warning(f"Unexpectedly left channel {channel} (possibly due to !mp close). Returning to waiting state.")
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
        self.bot_state = BOT_STATE_SHUTTING_DOWN # Mark for shutdown
        self._request_shutdown()

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
                self.target_channel = f"#mp_{new_room_id}"
                self.bot_state = BOT_STATE_IN_ROOM  # Transition to IN_ROOM state immediately
                log.info(f"Bot automatically joined {self.target_channel}. State set to IN_ROOM.")

                # Reset room state to ensure a clean slate BEFORE setting the flag
                self.reset_room_state()
                self.target_channel = f"#mp_{new_room_id}"  # Re-set after reset
                # --- NEW: Mark room as created by bot ---
                self.room_was_created_by_bot = True
                log.info(f"Marking room {self.target_channel} as created by bot (for auto-close feature).")
                # --- END NEW ---

                # Send welcome message if configured
                if self.config.get("welcome_message"):
                    self.send_message(self.config["welcome_message"])

                # Clear password by default unless one was explicitly set
                if not self.pending_room_password:
                    log.info(f"No password specified in 'make' command. Clearing any default password for {self.target_channel}.")
                    self.send_message("!mp password")  # Clears the password
                # Set password if pending from 'make' command
                elif self.pending_room_password:
                    log.info(f"Setting password for room {self.target_channel} to '{self.pending_room_password}' as requested by 'make' command.")
                    self.send_message(f"!mp password {self.pending_room_password}")
                    self.pending_room_password = None  # Clear pending password

                # Request initial settings after a short delay
                threading.Timer(2.5, self.request_initial_settings).start()
                self.JoinedLobby.emit({'channel': self.target_channel})
            else:
                log.warning(f"Received PM from BanchoBot while waiting for 'make' response, but didn't match expected pattern: {message}")
                # Optional: Reset waiting_for_make_response after a timeout
                if self.waiting_for_make_response:
                    threading.Timer(10.0, lambda: setattr(self, 'waiting_for_make_response', False) if self.waiting_for_make_response else None).start()
                    log.warning("Started 10s timeout to reset waiting_for_make_response if no valid response received.")

    def on_pubmsg(self, connection, event):
        # Ignore public messages if not in a room
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
        """Initiates the process of joining a specific room."""
        if not self.connection.is_connected():
             log.error(f"Cannot join room {room_id}, not connected to IRC.")
             self.bot_state = BOT_STATE_SHUTTING_DOWN # Connection lost probably fatal
             self._request_shutdown()
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

        self.target_channel = f"#mp_{room_id}"
        self.bot_state = BOT_STATE_JOINING # Mark as attempting to join
        # Ensure the 'created by bot' flag is FALSE for rooms joined via 'enter'
        # This is handled by reset_room_state which will be called in on_join
        # self.room_was_created_by_bot = False # Set explicitly here? No, rely on reset.

        log.info(f"Attempting to join channel: {self.target_channel}")
        try:
            self.connection.join(self.target_channel)
            # Success handled by on_join, failure by on_err_*
        except irc.client.ServerNotConnectedError:
            log.warning("Connection lost before join command could be sent.")
            self.bot_state = BOT_STATE_SHUTTING_DOWN
            self._request_shutdown()
        except Exception as e:
            log.error(f"Error sending join command for {self.target_channel}: {e}", exc_info=True)
            self.reset_room_state() # Clear target channel
            self.bot_state = BOT_STATE_CONNECTED_WAITING


    def leave_room(self):
        """Leaves the current room and returns to the waiting state."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.target_channel:
            log.warning("Cannot leave room, not currently in one.")
            return

        # --- NEW: Cancel empty room timer before leaving ---
        if self.empty_room_close_timer_active:
            log.info(f"Manually leaving room '{self.target_channel}'. Cancelling empty room auto-close timer.")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0
        # --- End New ---

        log.info(f"Leaving room {self.target_channel}...")
        try:
            if self.connection.is_connected():
                self.connection.part(self.target_channel, "Leaving room (stop command)")
            else:
                log.warning("Cannot send PART command, disconnected.")
        except Exception as e:
            log.error(f"Error sending PART command for {self.target_channel}: {e}", exc_info=True)
        finally:
            # Reset state regardless of PART success
            self.reset_room_state()
            self.bot_state = BOT_STATE_CONNECTED_WAITING
            log.info("Returned to waiting state. Use 'make' or 'enter' in console.")


    # --- User Command Parsing (In Room) ---
    def parse_user_command(self, sender, message):
        """Parses commands sent by users IN THE ROOM."""
        if self.bot_state != BOT_STATE_IN_ROOM: return # Should not happen if called correctly
        if not message.startswith("!"): return

        parts = shlex.split(message)
        if not parts: return
        command = parts[0].lower()
        args = parts[1:] # Capture arguments

        # Commands available to everyone
        if command == '!queue':
            if self.config['host_rotation']['enabled']:
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
            vs_config = self.config['vote_skip']
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
                if self.config['host_rotation']['enabled']:
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
                # Optional: Check if map checker enabled and map is invalid?
                # if self.config['map_checker']['enabled'] and not self.host_map_selected_valid:
                #    self.send_message(f"Cannot start: Current map {self.current_map_id} is invalid or was not checked.")
                #    return

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
                return

            if command == '!abort':
                log.info(f"Host {sender} sending !mp abort")
                self.send_message("!mp abort")
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
            "Bot creator: serifpersia : )",
        ]
        if self.config['map_checker']['enabled']:
             mc = self.config['map_checker']
             rule_summary = f"Rules active: {mc.get('min_stars',0)}*-{mc.get('max_stars',0)}*, {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}. Use !rules for details."
             messages.append(rule_summary)

        self.send_message(messages)

    # --- BanchoBot Message Parsing (In Room) ---
    def parse_bancho_message(self, msg):
        if self.bot_state != BOT_STATE_IN_ROOM: return # Ignore if not in a room
        try:
            if " joined in slot " in msg:
                match = re.match(r"(.+?) joined in slot \d+\.", msg)
                if match: self.handle_player_join(match.group(1))
            elif " left the game." in msg:
                match = re.match(r"(.+?) left the game\.", msg)
                if match: self.handle_player_left(match.group(1))
            elif " became the host." in msg:
                match = re.match(r"(.+?) became the host\.", msg)
                if match: self.handle_host_change(match.group(1))
            elif "Beatmap changed to: " in msg:
                map_id_match = re.search(r"/(?:b|beatmaps)/(\d+)", msg)
                map_id_from_set_match = re.search(r"/beatmapsets/\d+#(?:osu|taiko|fruits|mania)/(\d+)", msg)
                map_title_match = re.match(r"Beatmap changed to: (.*?)\s*\(https://osu\.ppy\.sh/.*\)", msg)

                map_id = None
                if map_id_match:
                    map_id = int(map_id_match.group(1))
                elif map_id_from_set_match:
                    map_id = int(map_id_from_set_match.group(1))
                    log.debug(f"Extracted map ID {map_id} from beatmapset link.")

                if map_id:
                    title = map_title_match.group(1).strip() if map_title_match else "Unknown Title"
                    self.handle_map_change(map_id, title)
                else:
                    log.warning(f"Could not parse map ID from change msg: {msg}")
            elif msg == "The match has started!": self.handle_match_start()
            elif msg == "The match has finished!": self.handle_match_finish()
            elif msg == "All players are ready": self.handle_all_players_ready()
            elif msg.startswith("Room name:") or msg.startswith("History is "): pass
            elif msg.startswith("Beatmap: "): self._parse_initial_beatmap(msg)
            elif msg.startswith("Players:"): self._parse_player_count(msg)
            elif msg.startswith("Slot "): self._parse_slot_message(msg)
            elif msg.startswith("Team mode:") or msg.startswith("Win condition:") or msg.startswith("Active mods:") or msg.startswith("Free mods:"):
                 self.check_initialization_complete(msg) # Trigger final setup after last expected settings line
            elif " was kicked from the room." in msg:
                 match = re.match(r"(.+?) was kicked from the room\.", msg)
                 if match: self.handle_player_left(match.group(1)) # Treat kick like a leave
            elif " changed the room name to " in msg: pass
            elif " changed the password." in msg: pass
            elif " removed the password." in msg: pass
            elif " changed room size to " in msg:
                 self._parse_player_count_from_size_change(msg)
            elif msg == "Match Aborted": # Handle abort message from Bancho
                 log.info("BanchoBot reported: Match Aborted")
                 self.is_matching = False
                 if self.current_host:
                      self.reset_host_timers_and_state(self.current_host)
            elif msg == "Closed the match": # <<< NEW: Handle Bancho's close confirmation
                log.info(f"BanchoBot reported: Closed the match ({self.target_channel})")
                # The bot will receive a PART event shortly after this,
                # which triggers the state reset and return to WAITING via on_part.
                pass # No immediate action needed here, on_part handles the state change.

        except Exception as e:
            log.error(f"Error parsing Bancho msg: '{msg}' - {e}", exc_info=True)

    def _parse_initial_beatmap(self, msg):
        map_id_match = re.search(r"/(?:b|beatmaps)/(\d+)", msg)
        map_id_from_set_match = re.search(r"/beatmapsets/\d+#(?:osu|taiko|fruits|mania)/(\d+)", msg)
        map_title_match = re.match(r"Beatmap: https://.*?\s+(.+?)(?:\s+\[.+\])?$", msg)

        map_id = None
        if map_id_match:
            map_id = int(map_id_match.group(1))
        elif map_id_from_set_match:
            map_id = int(map_id_from_set_match.group(1))

        if map_id:
            self.current_map_id = map_id
            self.current_map_title = map_title_match.group(1).strip() if map_title_match else "Unknown Title (from settings)"
            log.info(f"Initial map set from settings: ID {self.current_map_id}, Title: {self.current_map_title}")
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
            log.debug(f"Parsed player count: {match.group(1)}")
        else:
             log.warning(f"Could not parse player count msg: {msg}")

    def _parse_player_count_from_size_change(self, msg):
        match = re.search(r"changed room size to (\d+)", msg)
        if match:
             log.info(f"Room size changed to {match.group(1)}. Player count may need updating via !mp settings or join/leave.")
        else:
             log.warning(f"Could not parse size change message: {msg}")

    def _parse_slot_message(self, msg):
        match = re.match(r"Slot (\d+)\s+(Not Ready|Ready)\s+https://osu\.ppy\.sh/u/\d+\s+([^\s].*?)(?:\s+\[(Host.*?)\])?$", msg)
        if not match:
            match_empty = re.match(r"Slot (\d+)\s+(Open|Locked)", msg)
            if match_empty:
                 log.debug(f"Ignoring empty/locked slot msg: {msg}")
            else:
                 log.debug(f"No player match in slot msg: {msg}")
            return

        slot, status, player_name, tags = match.groups()
        player_name = player_name.strip()
        is_host = tags and "Host" in tags

        if player_name and player_name.lower() != "banchobot":
             if player_name not in self.players_in_lobby:
                 log.debug(f"Adding '{player_name}' to players_in_lobby from settings.")
                 self.players_in_lobby.add(player_name)
                 # --- NEW: If player joins during timer, cancel it ---
                 if self.empty_room_close_timer_active:
                      log.info(f"Player '{player_name}' detected via settings while timer active. Cancelling auto-close timer.")
                      self.empty_room_close_timer_active = False
                      self.empty_room_timestamp = 0
                 # --- End New ---

             if is_host:
                 if self.current_host != player_name:
                     log.info(f"Identified host from settings: {player_name}")
                     self.current_host = player_name
                     self.reset_host_timers_and_state(player_name)

             if self.config['host_rotation']['enabled'] and player_name not in self.host_queue:
                 if player_name not in self.host_queue:
                    self.host_queue.append(player_name)
                    log.info(f"Added '{player_name}' to host queue from settings.")

    def check_initialization_complete(self, last_message_seen):
        log.info(f"Assuming !mp settings finished (last seen: '{last_message_seen[:30]}...'). Finalizing initial state.")
        self.initialize_lobby_state()

    def initialize_lobby_state(self):
        hr_enabled = self.config['host_rotation']['enabled']
        mc_enabled = self.config['map_checker']['enabled']

        log.info(f"Finalizing initial state. Players: {len(self.players_in_lobby)}. Rotation Enabled: {hr_enabled}. Queue: {list(self.host_queue)}, Identified Host: {self.current_host}")

        if hr_enabled:
            if not self.host_queue:
                log.warning("Rotation enabled, but no players found in !mp settings to initialize host queue.")
            elif self.current_host and self.current_host in self.host_queue:
                 if not self.host_queue or self.host_queue[0] != self.current_host:
                     try:
                         self.host_queue.remove(self.current_host)
                         self.host_queue.insert(0, self.current_host)
                         log.info(f"Moved identified host '{self.current_host}' to front of queue.")
                     except ValueError:
                          log.warning(f"Host '{self.current_host}' identified but vanished before queue reorder?")
                          if self.host_queue:
                              self.current_host = self.host_queue[0]
                              log.info(f"Setting host to first player in queue: {self.current_host}")
                              self.reset_host_timers_and_state(self.current_host)
                          else:
                               self.current_host = None
                 else:
                      self.reset_host_timers_and_state(self.current_host)

            elif self.host_queue: # Queue has players, but no host identified yet
                self.current_host = self.host_queue[0]
                log.info(f"No host identified from settings, assuming first player is host: {self.current_host}")
                self.reset_host_timers_and_state(self.current_host)

            if self.current_host:
                 log.info(f"Host rotation initialized. Current Host: {self.current_host}, Queue: {list(self.host_queue)}")
            else:
                 log.info("Host rotation enabled, but no host could be determined yet.")

        # Check initial map regardless of rotation, if checker enabled and host known
        if mc_enabled and self.current_map_id != 0 and self.current_host:
            log.info(f"Checking initial map ID {self.current_map_id} for host {self.current_host}.")
            self.check_map(self.current_map_id, self.current_map_title)
        elif mc_enabled:
            self.last_valid_map_id = 0
            self.host_map_selected_valid = False

        log.info(f"Initial lobby state setup complete. Player list: {self.players_in_lobby}")
        self.log_feature_status()

        # --- NEW: Check if room is already empty on join/init ---
        # (e.g. if bot restarts into an empty room it created before)
        # This is less likely if the bot closes it, but handles restarts.
        self._check_start_empty_room_timer()
        # --- End New ---


    def request_initial_settings(self):
        """Requests !mp settings after joining."""
        if self.bot_state != BOT_STATE_IN_ROOM:
            log.warning("Cannot request settings, not in a room.")
            return
        if self.connection.is_connected():
            log.info("Requesting initial room state with !mp settings")
            self.send_message("!mp settings")
        else:
            log.warning("Cannot request !mp settings, disconnected.")

    # --- Host Rotation & Player Tracking Logic (In Room) ---
    def handle_player_join(self, player_name):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not player_name or player_name.lower() == "banchobot": return

        # --- NEW: Cancel empty room timer if active ---
        if self.empty_room_close_timer_active:
            log.info(f"Player '{player_name}' joined. Cancelling empty room auto-close timer for {self.target_channel}.")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0
        # --- End New ---

        self.PlayerJoined.emit({'player': player_name})
        self.players_in_lobby.add(player_name)
        log.info(f"'{player_name}' joined. Lobby size: {len(self.players_in_lobby)}")

        if self.config['host_rotation']['enabled']:
            if player_name not in self.host_queue:
                self.host_queue.append(player_name)
                log.info(f"Added '{player_name}' to queue. Queue: {list(self.host_queue)}")
                if len(self.host_queue) == 1 and not self.current_host:
                    log.info(f"First player '{player_name}' joined while no host was assigned. Assigning host.")
                    self.send_message(f"!mp host {player_name}")
            else:
                log.info(f"'{player_name}' joined, but already in queue.")


    def handle_player_left(self, player_name):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not player_name: return

        self.PlayerLeft.emit({'player': player_name})
        was_host = (player_name == self.current_host)

        # Remove from general player list
        if player_name in self.players_in_lobby:
            self.players_in_lobby.remove(player_name)
            log.info(f"'{player_name}' left. Lobby size: {len(self.players_in_lobby)}")
        else:
             log.warning(f"'{player_name}' left but was not in tracked player list?")

        # Remove from host queue if rotation enabled
        hr_enabled = self.config['host_rotation']['enabled']
        queue_changed = False
        if hr_enabled and player_name in self.host_queue:
            try:
                self.host_queue.remove(player_name)
                log.info(f"Removed '{player_name}' from queue. Queue: {list(self.host_queue)}")
                queue_changed = True
            except ValueError:
                log.warning(f"'{player_name}' left but not found in queue for removal?")

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
            self.host_map_selected_valid = False
            if hr_enabled and not self.is_matching:
                log.info("Host left outside match, attempting to set next host.")
                self.set_next_host()
                if self.current_host:
                     self.display_host_queue()

        # --- NEW: Check if room is empty and start auto-close timer ---
        self._check_start_empty_room_timer()
        # --- End New ---

    # --- NEW HELPER METHOD ---
    def _check_start_empty_room_timer(self):
        """Checks if the room is empty and starts the auto-close timer if applicable."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        ac_config = self.config['auto_close_empty_room']

        # Conditions to start timer:
        # 1. Feature enabled
        # 2. Room was created by this bot instance
        # 3. Lobby is now empty (excluding bot)
        # 4. Timer is not already active
        if (ac_config['enabled'] and
                self.room_was_created_by_bot and
                len(self.players_in_lobby) == 0 and
                not self.empty_room_close_timer_active):

            delay = ac_config['delay_seconds']
            log.info(f"Room '{self.target_channel}' is now empty. Starting {delay}s auto-close timer.")
            self.empty_room_close_timer_active = True
            self.empty_room_timestamp = time.time()
            # Optionally send a message to chat? Probably too noisy.
            # self.send_message(f"Room empty. Auto-closing in {delay}s if no one joins.")

    def handle_host_change(self, player_name):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not player_name: return
        log.info(f"Bancho reported host changed to: {player_name}")

        if player_name == self.current_host:
             log.info(f"Host change message for '{player_name}', but they were already marked as host. Likely confirmation.")
             self.reset_host_timers_and_state(player_name)
             return

        previous_host = self.current_host
        self.current_host = player_name
        self.HostChanged.emit({'player': player_name, 'previous': previous_host})

        # Add player to lobby list if somehow missed
        if player_name not in self.players_in_lobby:
             log.warning(f"New host '{player_name}' wasn't in player list, adding.")
             self.players_in_lobby.add(player_name)
             # If adding the new host cancels the timer...
             if self.empty_room_close_timer_active:
                 log.info(f"New host '{player_name}' added. Cancelling empty room auto-close timer for {self.target_channel}.")
                 self.empty_room_close_timer_active = False
                 self.empty_room_timestamp = 0


        # Reset AFK timer, violations, valid map flag, and clear any voteskip against the *previous* host
        self.reset_host_timers_and_state(player_name) # Resets flag to False
        self.clear_vote_skip("new host assigned")

        # Reorder queue if rotation is enabled and the new host isn't at the front
        hr_enabled = self.config['host_rotation']['enabled']
        queue_changed = False
        if hr_enabled and self.host_queue:
             if player_name not in self.host_queue:
                 log.info(f"New host {player_name} wasn't in queue, adding to front.")
                 self.host_queue.appendleft(player_name) # Add to front
                 queue_changed = True
             elif self.host_queue[0] != player_name:
                 log.warning(f"Host changed to {player_name}, but they weren't front of queue ({self.host_queue[0] if self.host_queue else 'N/A'}). Reordering queue.")
                 try:
                     self.host_queue.remove(player_name)
                     self.host_queue.insert(0, player_name) # Use insert instead of appendleft after remove
                     queue_changed = True
                 except ValueError:
                      log.error(f"Failed to reorder queue for new host {player_name} - value error despite check.")

        if hr_enabled and queue_changed:
            self.display_host_queue()

    def reset_host_timers_and_state(self, host_name):
        """Resets AFK timer, map violations, and valid map flag for the current/new host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.debug(f"Resetting timers/state for host '{host_name}'.")
        self.host_last_action_time = time.time() # Reset AFK timer on host change/skip
        self.host_map_selected_valid = False # Reset valid map flag

        # Reset map violations
        if self.config['map_checker']['enabled']:
            self.map_violations.setdefault(host_name, 0)
            if self.map_violations.get(host_name, 0) != 0: # Use .get with default
                 log.info(f"Reset map violations for new/current host '{host_name}'.")
                 self.map_violations[host_name] = 0

    def rotate_and_set_host(self):
        """Rotates the queue (if enabled) and sets the new host via !mp host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        hr_enabled = self.config['host_rotation']['enabled']
        if not hr_enabled or len(self.host_queue) < 1:
            log.debug(f"Skipping host rotation (enabled={hr_enabled}, queue_size={len(self.host_queue)}).")
            if self.current_host:
                 self.reset_host_timers_and_state(self.current_host)
            return

        log.info(f"Attempting host rotation. Current queue: {list(self.host_queue)}")
        if len(self.host_queue) > 1:
            player_to_move = self.last_host if self.last_host and self.last_host in self.host_queue else self.host_queue[0]

            if player_to_move in self.host_queue:
                try:
                    moved_player = self.host_queue.popleft()
                    if moved_player == player_to_move:
                         self.host_queue.append(moved_player)
                         log.info(f"Rotated queue, moved '{moved_player}' to the back.")
                    else:
                         log.warning(f"Player at front '{moved_player}' wasn't the expected last host '{player_to_move}'. Rotating both.")
                         self.host_queue.append(moved_player)
                         try:
                              self.host_queue.remove(player_to_move)
                              self.host_queue.append(player_to_move)
                              log.info(f"Moved '{player_to_move}' to the back as well.")
                         except ValueError:
                              log.warning(f"Expected last host '{player_to_move}' not found for secondary move.")
                except IndexError:
                     log.warning("IndexError during rotation, queue likely empty unexpectedly.")
            else:
                log.warning(f"Player '{player_to_move}' intended for rotation not found in queue. Skipping move, rotating first element.")
                if self.host_queue:
                    first = self.host_queue.popleft()
                    self.host_queue.append(first)
                    log.info(f"Rotated queue (fallback), moved '{first}' to the back.")

        elif len(self.host_queue) == 1:
            log.info("Only one player in queue, no rotation needed.")
        else: # len == 0
             log.warning("Rotation triggered with empty queue.")
             self.current_host = None
             self.host_map_selected_valid = False
             return

        log.info(f"Queue after potential rotation: {list(self.host_queue)}")
        self.last_host = None # Clear last host after rotation logic
        self.set_next_host()
        self.display_host_queue()

    def set_next_host(self):
        """Sets the player at the front of the queue as the host via !mp host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.config['host_rotation']['enabled']: return

        if self.host_queue:
            next_host = self.host_queue[0]
            if next_host != self.current_host:
                log.info(f"Setting next host to '{next_host}' via !mp host...")
                self.send_message(f"!mp host {next_host}")
            else:
                log.info(f"'{next_host}' is already the host (or expected to be based on queue). Resetting timers.")
                self.reset_host_timers_and_state(next_host)
        else:
            log.warning("Host queue is empty, cannot set next host.")
            self.current_host = None
            self.host_map_selected_valid = False

    def skip_current_host(self, reason="No reason specified"):
        """Skips the current host, rotates queue (if enabled), and sets the next host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.current_host:
            log.warning("Attempted to skip host, but no host is currently assigned.")
            return

        skipped_host = self.current_host
        log.info(f"Skipping host '{skipped_host}'. Reason: {reason}. Queue: {list(self.host_queue)}")

        messages = [f"Host Skipped: {skipped_host}"]
        if reason and "self-skipped" not in reason.lower() and "vote" not in reason.lower() and "afk" not in reason.lower() and "violation" not in reason.lower():
             messages.append(f"Reason: {reason}")
        self.send_message(messages)

        self.clear_vote_skip(f"host '{skipped_host}' skipped")
        self.host_map_selected_valid = False

        if self.config['host_rotation']['enabled']:
            self.last_host = skipped_host
            self.rotate_and_set_host()
        else:
            log.warning("Host skipped, but rotation is disabled. Cannot set next host automatically. Clearing host.")
            self.send_message("!mp clearhost")
            self.current_host = None

    def display_host_queue(self):
        """Sends the current host queue to the chat as a single message if rotation is enabled."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.config['host_rotation']['enabled']:
            return
        if not self.connection.is_connected():
             log.warning("Cannot display queue, not connected.")
             return
        if not self.host_queue:
            self.send_message("Host queue is empty.")
            return

        queue_list = list(self.host_queue)
        queue_entries = []
        current_host_name = self.current_host

        for i, player in enumerate(queue_list):
            entry = f"{player}[{i+1}]"
            if player == current_host_name:
                entry += "(Current)"
            queue_entries.append(entry)

        queue_str = ", ".join(queue_entries)
        final_message = f"Host order: {queue_str}"
        self.send_message(final_message)

    # --- Match State Handling (In Room) ---
    def handle_match_start(self):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info(f"Match started with map ID {self.current_map_id}.")
        self.is_matching = True
        self.last_host = None
        self.clear_vote_skip("match started")
        self.host_map_selected_valid = False
        self.last_valid_map_id = 0
        self.MatchStarted.emit({'map_id': self.current_map_id})

    def handle_match_finish(self):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info("Match finished.")
        self.is_matching = False
        self.last_host = self.current_host
        log.debug(f"Marking '{self.last_host}' as last host after match finish.")

        self.MatchFinished.emit({})
        self.current_map_id = 0
        self.current_map_title = ""
        self.last_valid_map_id = 0
        self.host_map_selected_valid = False

        if self.config['host_rotation']['enabled']:
            log.info("Scheduling host rotation (1.5s delay) after match finish.")
            threading.Timer(1.5, self.rotate_and_set_host).start()
        else:
             if self.current_host:
                 self.reset_host_timers_and_state(self.current_host)

    def handle_all_players_ready(self):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info("All players are ready.")
        as_config = self.config['auto_start']
        if as_config['enabled'] and not self.is_matching:
            log.debug("Checking conditions for auto-start...")

            map_ok_for_auto_start = True
            if self.current_map_id == 0:
                log.warning("Auto-start: Cannot start, no map selected.")
                map_ok_for_auto_start = False
            elif self.config['map_checker']['enabled'] and not self.host_map_selected_valid:
                log.warning(f"Auto-start: Cannot start, current map {self.current_map_id} did not pass validation or needs checking.")
                map_ok_for_auto_start = False
            elif not self.current_host:
                 log.warning("Auto-start: Cannot start, no current host identified.")
                 map_ok_for_auto_start = False

            if map_ok_for_auto_start:
                delay = max(1, as_config.get('delay_seconds', 5))
                log.info(f"Auto-starting match with map {self.current_map_id} in {delay} seconds.")
                self.send_message(f"!mp start {delay}")
            else:
                log.info("Auto-start conditions not met.")

        elif not as_config['enabled']:
             log.debug("Auto-start is disabled.")


    # --- Map Checking Logic (In Room) ---
    def handle_map_change(self, map_id, map_title):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info(f"Map changed to ID: {map_id}, Title: {map_title}")
        self.current_map_id = map_id
        self.current_map_title = map_title
        self.host_map_selected_valid = False # Reset flag on *any* map change

        if self.config['map_checker']['enabled'] and self.current_host:
             self.check_map(map_id, map_title)
        elif self.config['map_checker']['enabled'] and not self.current_host:
             log.warning("Map changed, but cannot check rules: No current host identified.")
        elif not self.config['map_checker']['enabled']:
            if self.current_host:
                self.host_last_action_time = time.time()
                log.debug(f"Reset AFK timer for {self.current_host} due to map change (Checker Disabled).")

    def check_map(self, map_id, map_title):
        """Fetches map info and checks against configured rules."""
        if not self.config['map_checker']['enabled'] or not self.current_host:
             log.debug("Skipping map check (disabled or no host).")
             self.host_map_selected_valid = False
             return

        self.host_map_selected_valid = False

        log.info(f"Checking map {map_id} ('{map_title}') selected by {self.current_host}...")
        info = get_beatmap_info(map_id, self.api_client_id, self.api_client_secret)

        if info is None:
            self.reject_map(f"Could not get info for map ID {map_id}. It might not exist or API failed.", is_violation=False)
            return

        stars = info.get('stars')
        length = info.get('length')
        title = info.get('title', 'N/A')
        version = info.get('version', 'N/A')
        status = info.get('status', 'unknown')
        mode = info.get('mode', 'unknown')

        stars_str = f"{stars:.2f}*" if isinstance(stars, (float, int)) else "?.??*"
        length_str = self._format_time(length)

        violations = []
        mc = self.config['map_checker']
        allowed_statuses = self.config.get('allowed_map_statuses', ['all'])
        allowed_modes = self.config.get('allowed_modes', ['all'])

        is_status_allowed = 'all' in [s.lower() for s in allowed_statuses] or status.lower() in [s.lower() for s in allowed_statuses]
        if not is_status_allowed: violations.append(f"Status '{status}' not allowed")

        is_mode_allowed = 'all' in [m.lower() for m in allowed_modes] or mode.lower() in [m.lower() for m in allowed_modes]
        if not is_mode_allowed: violations.append(f"Mode '{mode}' not allowed")

        min_stars = mc.get('min_stars', 0)
        max_stars = mc.get('max_stars', 0)
        if stars is not None:
            epsilon = 0.001
            if min_stars > 0 and stars < min_stars - epsilon: violations.append(f"Stars ({stars_str}) < Min ({min_stars:.2f}*)")
            if max_stars > 0 and stars > max_stars + epsilon: violations.append(f"Stars ({stars_str}) > Max ({max_stars:.2f}*)")
        elif min_stars > 0 or max_stars > 0: violations.append("Could not verify star rating")

        min_len = mc.get('min_length_seconds', 0)
        max_len = mc.get('max_length_seconds', 0)
        if length is not None:
            if min_len > 0 and length < min_len: violations.append(f"Length ({length_str}) < Min ({self._format_time(min_len)})")
            if max_len > 0 and length > max_len: violations.append(f"Length ({length_str}) > Max ({self._format_time(max_len)})")
        elif min_len > 0 or max_len > 0: violations.append("Could not verify map length")

        if violations:
            reason = f"Map Rejected: {'; '.join(violations)}"
            log.warning(f"Map violation by {self.current_host}: {reason}")
            self.reject_map(reason, is_violation=True)
        else:
            self.host_map_selected_valid = True
            self.last_valid_map_id = map_id
            log.info(f"Map {map_id} accepted and marked as valid. ({stars_str}, {length_str}, {status}, {mode})")

            if self.current_host:
                self.host_last_action_time = time.time()
                log.debug(f"Reset AFK timer for {self.current_host} due to VALID map selection.")

            messages = [ f"Map OK: {title} [{version}] ({stars_str}, {length_str}, {status}, {mode})" ]
            self.send_message(messages)

            if self.current_host in self.map_violations and self.map_violations[self.current_host] > 0:
                 log.info(f"Resetting violations for {self.current_host} after valid pick.")
                 self.map_violations[self.current_host] = 0


    def reject_map(self, reason, is_violation=True):
        """Handles map rejection, sends messages, increments violation count, and attempts to revert map."""
        if not self.config['map_checker']['enabled'] or not self.current_host: return

        rejected_map_id = self.current_map_id
        rejected_map_title = self.current_map_title
        log.info(f"Rejecting map {rejected_map_id} ('{rejected_map_title}'). Reason: {reason}")

        self.host_map_selected_valid = False

        self.send_message(f"Map Check Failed: {reason}")

        self.current_map_id = 0
        self.current_map_title = ""

        revert_messages = []
        if self.last_valid_map_id != 0:
             log.info(f"Attempting to revert map to last valid ID: {self.last_valid_map_id}")
             revert_messages.append(f"!mp map {self.last_valid_map_id}")
        else:
             log.info("No previous valid map to revert to.")
             pass

        if revert_messages:
             threading.Timer(0.7, self.send_message, args=[revert_messages]).start()

        if not is_violation:
             log.debug("Map rejection was not due to rule violation. No violation counted.")
             return

        violation_limit = self.config['map_checker'].get('violations_allowed', 3)
        if violation_limit <= 0: return

        count = self.map_violations.get(self.current_host, 0) + 1
        self.map_violations[self.current_host] = count
        log.warning(f"{self.current_host} violation count: {count}/{violation_limit}")

        if count >= violation_limit:
            skip_message = f"Violation limit ({violation_limit}) reached for {self.current_host}. Skipping host."
            log.warning(f"Skipping host {self.current_host} due to map violations.")
            self.send_message(skip_message)
            self.skip_current_host(f"Reached map violation limit ({violation_limit})")
        else:
            remaining = violation_limit - count
            warn_message = f"Map Violation ({count}/{violation_limit}) for {self.current_host}. {remaining} remaining. Use !rules."
            self.send_message(warn_message)

    # --- Vote Skip Logic (In Room) ---
    def handle_vote_skip(self, voter):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        vs_config = self.config['vote_skip']
        if not vs_config['enabled'] or not self.current_host: return

        target_host = self.current_host
        timeout = vs_config.get('timeout_seconds', 60)

        if self.vote_skip_active and time.time() - self.vote_skip_start_time > timeout:
                 log.info(f"Vote skip for {self.vote_skip_target} expired.")
                 self.send_message(f"Vote to skip {self.vote_skip_target} failed (timeout).")
                 self.clear_vote_skip("timeout")

        if not self.vote_skip_active:
            self.vote_skip_active = True
            self.vote_skip_target = target_host
            self.vote_skip_initiator = voter
            self.vote_skip_voters = {voter}
            self.vote_skip_start_time = time.time()
            needed = self.get_votes_needed()
            log.info(f"Vote skip initiated by '{voter}' for host '{target_host}'. Needs {needed} votes.")
            self.send_message(f"{voter} started vote skip for {target_host}! !voteskip to agree. ({len(self.vote_skip_voters)}/{needed})")

        elif self.vote_skip_active and self.vote_skip_target == target_host:
            if voter in self.vote_skip_voters:
                log.debug(f"'{voter}' tried to vote skip again.")
                return

            self.vote_skip_voters.add(voter)
            needed = self.get_votes_needed()
            current_votes = len(self.vote_skip_voters)
            log.info(f"'{voter}' voted to skip '{target_host}'. Votes: {current_votes}/{needed}")

            if current_votes >= needed:
                log.info(f"Vote skip threshold reached for {target_host}. Skipping.")
                self.send_message(f"Vote skip passed! Skipping host {target_host}.")
                self.skip_current_host(f"Skipped by player vote ({current_votes}/{needed} votes)")
            else:
                 self.send_message(f"{voter} voted skip. ({current_votes}/{needed})")

        elif self.vote_skip_active and self.vote_skip_target != target_host:
             log.warning(f"'{voter}' tried !voteskip for '{target_host}', but active vote is for '{self.vote_skip_target}'. Clearing old.")
             self.clear_vote_skip("host changed mid-vote")
             self.send_message(f"Host changed during vote. Vote for {self.vote_skip_target} cancelled.")

    def get_votes_needed(self):
        """Calculates the number of votes required to skip the host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return 999
        vs_config = self.config['vote_skip']
        threshold_type = vs_config.get('threshold_type', 'percentage')
        threshold_value = vs_config.get('threshold_value', 51)

        # Eligible voters = total players - bot - host = total players - 1 (since bot isn't in players_in_lobby)
        eligible_voters = len(self.players_in_lobby) - 1
        if eligible_voters < 1: return 1

        if threshold_type == 'percentage':
            needed = math.ceil(eligible_voters * (threshold_value / 100.0))
            return max(1, int(needed))
        elif threshold_type == 'fixed':
            needed = int(threshold_value)
            return max(1, min(needed, eligible_voters)) # Cannot need more votes than eligible voters
        else:
            log.warning(f"Invalid vote_skip threshold_type '{threshold_type}'. Defaulting to percentage.")
            needed = math.ceil(eligible_voters * (threshold_value / 100.0))
            return max(1, int(needed))

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
        """Clears vote skip if the player was target/initiator, or removes voter."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if self.vote_skip_active and (player_name == self.vote_skip_target or player_name == self.vote_skip_initiator):
             self.send_message(f"Vote skip cancelled ({reason}: {player_name}).")
             self.clear_vote_skip(reason)
        elif self.vote_skip_active and player_name in self.vote_skip_voters:
             self.vote_skip_voters.remove(player_name)
             log.info(f"Removed leaving player '{player_name}' from vote skip voters. Remaining: {len(self.vote_skip_voters)}")
             needed = self.get_votes_needed()
             current_votes = len(self.vote_skip_voters)
             if current_votes >= needed:
                  log.info(f"Vote skip threshold reached for {self.vote_skip_target} after voter left. Skipping.")
                  self.send_message(f"Vote skip passed after voter left! Skipping host {self.vote_skip_target}.")
                  self.skip_current_host(f"Skipped by player vote ({current_votes}/{needed} votes after voter left)")

    def check_vote_skip_timeout(self):
        """Periodically checks if the active vote skip has timed out."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.vote_skip_active: return
        vs_config = self.config['vote_skip']
        timeout = vs_config.get('timeout_seconds', 60)

        if time.time() - self.vote_skip_start_time > timeout:
            log.info(f"Vote skip for '{self.vote_skip_target}' timed out.")
            self.send_message(f"Vote to skip {self.vote_skip_target} failed (timeout).")
            self.clear_vote_skip("timeout")


    # --- AFK Host Handling (In Room) ---
    def check_afk_host(self):
        """Periodically checks if the current host is AFK and skips them if enabled."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        afk_config = self.config['afk_handling']
        if not afk_config['enabled']: return
        if not self.current_host: return
        if self.is_matching: return

        if self.host_map_selected_valid:
             log.debug(f"AFK check for {self.current_host}: Skipped, host has selected a valid map.")
             return

        timeout = afk_config.get('timeout_seconds', 30)
        if timeout <= 0: return

        time_since_last_action = time.time() - self.host_last_action_time
        log.debug(f"AFK check for {self.current_host}. Idle time: {time_since_last_action:.1f}s / {timeout}s (Valid map selected: {self.host_map_selected_valid})")

        if time_since_last_action > timeout:
            log.warning(f"Host '{self.current_host}' exceeded AFK timeout ({timeout}s). Skipping.")
            self.send_message(f"Host {self.current_host} skipped due to inactivity ({timeout}s+).")
            self.skip_current_host(f"AFK timeout ({timeout}s)")

    # --- NEW METHOD for Periodic Check ---
    def check_empty_room_close(self):
        """Checks if an empty, bot-created room's timeout has expired and closes it."""
        if not self.empty_room_close_timer_active:
            return # Timer not active, nothing to do

        # Double-check if someone joined between the last check and now
        if len(self.players_in_lobby) > 0:
            log.info("check_empty_room_close: Player detected, cancelling timer.")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0
            return

        ac_config = self.config['auto_close_empty_room']
        delay = ac_config['delay_seconds']
        elapsed_time = time.time() - self.empty_room_timestamp

        log.debug(f"Checking empty room close timer: Elapsed {elapsed_time:.1f}s / {delay}s")

        if elapsed_time >= delay:
            log.warning(f"Empty room '{self.target_channel}' timeout ({delay}s) reached. Sending '!mp close'.")
            try:
                self.send_message("!mp close")
                # Deactivate timer immediately after sending command to prevent repeats
                self.empty_room_close_timer_active = False
                self.empty_room_timestamp = 0
                # NOTE: The bot's state transition back to WAITING will be handled
                # by on_part/on_kick when Bancho closes the room.
            except Exception as e:
                log.error(f"Failed to send '!mp close' command: {e}", exc_info=True)
                # Keep timer active? Or disable to prevent spamming? Let's disable.
                self.empty_room_close_timer_active = False
                self.empty_room_timestamp = 0

    # --- Utility Methods ---
    def send_message(self, message_or_list):
        """Sends a message to the current target_channel if in a room."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.target_channel:
            log.warning(f"Cannot send to channel, not in a room state: {message_or_list}")
            return
        self._send_irc_message(self.target_channel, message_or_list)

    def send_private_message(self, recipient, message_or_list):
        """Sends a private message to a specific user."""
        self._send_irc_message(recipient, message_or_list)

    def _send_irc_message(self, target, message_or_list):
        """Internal helper to send IRC messages with rate limiting."""
        if not self.connection.is_connected():
            log.warning(f"Cannot send, not connected: {message_or_list}")
            return

        messages = message_or_list if isinstance(message_or_list, list) else [message_or_list]
        delay = 0.5 # Delay between multiple messages

        for i, msg in enumerate(messages):
            if not msg: continue
            full_msg = str(msg)
            if not full_msg.startswith("!") and (full_msg.startswith("/") or full_msg.startswith(".")):
                 log.warning(f"Message starts with potentially unsafe character, prepending space: {full_msg[:20]}...")
                 full_msg = " " + full_msg

            max_len = 450
            if len(full_msg.encode('utf-8')) > max_len:
                log.warning(f"Truncating long message: {full_msg[:100]}...")
                encoded_msg = full_msg.encode('utf-8')
                while len(encoded_msg) > max_len:
                    encoded_msg = encoded_msg[:-1]
                try:
                    full_msg = encoded_msg.decode('utf-8', 'ignore') + "..."
                except Exception:
                    full_msg = full_msg[:max_len//4] + "..."

            try:
                if i > 0:
                    time.sleep(delay)
                log.info(f"SENDING to {target}: {full_msg}")
                self.connection.privmsg(target, full_msg)
                if target == self.target_channel:
                    self.SentMessage.emit({'message': full_msg})
            except irc.client.ServerNotConnectedError:
                log.warning("Failed to send message: Disconnected.")
                self._request_shutdown()
                break
            except Exception as e:
                log.error(f"Failed to send message to {target}: {e}", exc_info=True)
                time.sleep(1)

    def _request_shutdown(self):
        """Internal signal to start shutdown sequence."""
        global shutdown_requested
        if self.bot_state != BOT_STATE_SHUTTING_DOWN:
            log.info("Shutdown requested internally.")
            self.bot_state = BOT_STATE_SHUTTING_DOWN
            shutdown_requested = True

    # --- Admin Commands (Called from Console Input Thread) ---
    def admin_skip_host(self, reason="Admin command"):
        if self.bot_state != BOT_STATE_IN_ROOM:
             log.error("Admin Skip Failed: Bot not in a room.")
             print("Command failed: Bot is not currently in a room.")
             return
        if not self.connection.is_connected():
             log.error("Admin Skip Failed: Not connected.")
             print("Command failed: Not connected to IRC.")
             return
        log.info(f"Admin skip initiated. Reason: {reason}")
        self.skip_current_host(reason)

    def admin_show_queue(self):
        if self.bot_state != BOT_STATE_IN_ROOM:
            print("Cannot show queue: Bot not in a room.")
            return
        if not self.config['host_rotation']['enabled']:
            print("Host rotation is disabled.")
            return
        print("--- Host Queue (Console View) ---")
        if not self.host_queue:
            print("(Empty)")
        else:
            q_list = list(self.host_queue)
            current_host_in_queue = self.current_host and self.current_host in q_list
            for i, p in enumerate(q_list):
                status = ""
                if p == self.current_host: status = "(Current Host)"
                elif i == 1 and current_host_in_queue and len(q_list) > 1: status = "(Next Host)"
                elif i == 0 and not current_host_in_queue: status = "(Next Host)" # Edge case
                print(f"{i+1}. {p} {status}")
        print("---------------------------------")

    def admin_show_status(self):
        print("--- Bot Status (Console View) ---")
        print(f"Bot State: {self.bot_state}")
        print(f"Connected to IRC: {self.connection.is_connected()}")
        print(f"Current Channel: {self.target_channel if self.target_channel else 'None'}")
        print("-" * 10 + " Room Details " + "-" * 10 if self.bot_state == BOT_STATE_IN_ROOM else "")
        if self.bot_state == BOT_STATE_IN_ROOM:
            print(f" Current Host: {self.current_host if self.current_host else 'None'}")
            print(f" Match in Progress: {self.is_matching}")
            print(f" Players in Lobby: {len(self.players_in_lobby)} {list(self.players_in_lobby)}")
            print(f" Created by Bot: {self.room_was_created_by_bot}") # NEW
            print(f" Empty Close Timer: {'ACTIVE (' + str(int(time.time() - self.empty_room_timestamp)) + 's elapsed)' if self.empty_room_close_timer_active else 'Inactive'}") # NEW
            print("-" * 10 + " Features " + "-" * 10)
            print(f" Rotation Enabled: {self.config['host_rotation']['enabled']}")
            print(f" Map Check Enabled: {self.config['map_checker']['enabled']}")
            print(f" Vote Skip Enabled: {self.config['vote_skip']['enabled']}")
            print(f" AFK Check Enabled: {self.config['afk_handling']['enabled']}")
            print(f" Auto Start Enabled: {self.config['auto_start']['enabled']}")
            print(f" Auto Close Enabled: {self.config['auto_close_empty_room']['enabled']} (Delay: {self.config['auto_close_empty_room']['delay_seconds']}s)") # NEW
            print("-" * 10 + " Vote Skip " + "-" * 10)
            if self.vote_skip_active:
                 print(f" Vote Skip Active: Yes (Target: {self.vote_skip_target}, Voters: {len(self.vote_skip_voters)}/{self.get_votes_needed()}, Initiator: {self.vote_skip_initiator})")
            else:
                 print(" Vote Skip Active: No")
            print("-" * 10 + " Map Info " + "-" * 10)
            print(f" Current Map ID: {self.current_map_id if self.current_map_id else 'None'} ({self.current_map_title})")
            print(f" Last Valid Map ID: {self.last_valid_map_id if self.last_valid_map_id else 'None'}")
            print(f" Host Map Selected Valid (AFK Bypass): {self.host_map_selected_valid}")
            if self.config['map_checker']['enabled']:
                mc = self.config['map_checker']
                statuses = self.config.get('allowed_map_statuses', ['all'])
                modes = self.config.get('allowed_modes', ['all'])
                print("  Map Rules:")
                print(f"   Stars: {mc.get('min_stars',0):.2f}-{mc.get('max_stars',0):.2f}")
                print(f"   Len: {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}")
                print(f"   Status: {','.join(statuses)}")
                print(f"   Modes: {','.join(modes)}")
                print(f"   Violations: {mc.get('violations_allowed', 3)}")
            else:
                 print("  Map Rules: (Map Check Disabled)")
        print("---------------------------------")

    # --- Shutdown ---
    def shutdown(self, message="Client shutting down."):
        log.info("Initiating shutdown sequence...")
        conn_available = hasattr(self, 'connection') and self.connection and self.connection.is_connected()

        # Cancel any running timers on shutdown
        self.empty_room_close_timer_active = False

        if conn_available and self.config.get("goodbye_message") and self.target_channel:
            try:
                log.info(f"Sending goodbye message: '{self.config['goodbye_message']}'")
                # Don't use self.send_message as state might be wrong
                self.connection.privmsg(self.target_channel, self.config['goodbye_message'])
                time.sleep(0.5) # Brief pause for message delivery
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


# --- Configuration Loading/Generation ---
def load_or_generate_config(filepath):
    """Loads config from JSON file or generates a default one if not found.
       Prioritizes values from the existing file over defaults."""
    defaults = {
        "server": "irc.ppy.sh",
        "port": 6667,
        "username": "YourOsuUsername",
        "password": "YourOsuIRCPassword",
        "welcome_message": "Bot connected. !help for commands. !rules for map rules.",
        "goodbye_message": "Bot disconnecting.",
        "osu_api_client_id": 0,
        "osu_api_client_secret": "YOUR_CLIENT_SECRET",
        "map_checker": {
            "enabled": True, "min_stars": 0.0, "max_stars": 10.0,
            "min_length_seconds": 0, "max_length_seconds": 0, "violations_allowed": 3
        },
        "allowed_map_statuses": ["ranked", "approved", "qualified", "loved", "graveyard"],
        "allowed_modes": ["all"],
        "host_rotation": {"enabled": True},
        "vote_skip": {
            "enabled": True, "timeout_seconds": 60,
            "threshold_type": "percentage", "threshold_value": 51
        },
        "afk_handling": {"enabled": True, "timeout_seconds": 120},
        "auto_start": {"enabled": False, "delay_seconds": 5},
        # --- NEW SECTION ---
        "auto_close_empty_room": {
            "enabled": True,      # Enable by default
            "delay_seconds": 30   # Timeout in seconds
        }
        # --- END NEW SECTION ---
    }

    # --- Recursive Update Logic (Corrected) ---
    def merge_configs(base, updates):
        """Recursively merges 'updates' onto 'base'. 'updates' values take precedence."""
        merged = base.copy() # Start with a copy of the base (defaults)
        for key, value in updates.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                merged[key] = merge_configs(merged[key], value)
            else:
                merged[key] = value
        return merged

    try:
        if not filepath.exists():
            log.warning(f"Config file '{filepath}' not found. Generating default config.")
            log.warning("IMPORTANT: Please edit the generated config.json with your osu! username, IRC password, and API credentials!")
            config_to_write = defaults.copy()

            try:
                with filepath.open('w', encoding='utf-8') as f:
                    json.dump(config_to_write, f, indent=4, ensure_ascii=False)
                log.info(f"Default config file created at '{filepath}'. Please edit it and restart.")
                return defaults
            except (IOError, PermissionError) as e:
                 log.critical(f"Could not write default config file: {e}")
                 sys.exit(1)

        else:
            log.info(f"Loading configuration from '{filepath}'...")
            with filepath.open('r', encoding='utf-8') as f:
                user_config = json.load(f)

            final_config = merge_configs(defaults, user_config)

            # --- Validation ---
            required = ["server", "port", "username", "password"]
            missing = [k for k in required if not final_config.get(k) or final_config[k] in ["", "YourOsuUsername", "YourOsuIRCPassword"]]
            if missing:
                log.warning(f"Missing or default required config keys: {', '.join(missing)}. Please check '{filepath}'.")

            if not isinstance(final_config.get("port"), int):
                log.error("'port' must be an integer. Using default 6667.")
                final_config["port"] = defaults["port"]
            if not isinstance(final_config.get("osu_api_client_id"), int):
                 log.error("'osu_api_client_id' must be an integer. Using default 0.")
                 final_config["osu_api_client_id"] = defaults["osu_api_client_id"]

            # Nested config validation (Add new section)
            for section in ["map_checker", "host_rotation", "vote_skip", "afk_handling", "auto_start", "auto_close_empty_room"]: # Added new section
                 if not isinstance(final_config.get(section), dict):
                     log.warning(f"Config section '{section}' is not a dictionary. Resetting to default.")
                     final_config[section] = defaults[section]

            # List type validation
            for key in ["allowed_map_statuses", "allowed_modes"]:
                 if not isinstance(final_config.get(key), list):
                     log.warning(f"Config key '{key}' is not a list. Resetting to default.")
                     final_config[key] = defaults[key]

            # Ensure numeric values in sub-configs are correct type
            mc = final_config['map_checker']
            mc['min_stars'] = float(mc.get('min_stars', defaults['map_checker']['min_stars']))
            mc['max_stars'] = float(mc.get('max_stars', defaults['map_checker']['max_stars']))
            mc['min_length_seconds'] = int(mc.get('min_length_seconds', defaults['map_checker']['min_length_seconds']))
            mc['max_length_seconds'] = int(mc.get('max_length_seconds', defaults['map_checker']['max_length_seconds']))
            mc['violations_allowed'] = int(mc.get('violations_allowed', defaults['map_checker']['violations_allowed']))

            vs = final_config['vote_skip']
            vs['timeout_seconds'] = int(vs.get('timeout_seconds', defaults['vote_skip']['timeout_seconds']))
            if vs.get('threshold_type') == 'percentage':
                vs['threshold_value'] = float(vs.get('threshold_value', defaults['vote_skip']['threshold_value']))
            else: # fixed
                vs['threshold_value'] = int(vs.get('threshold_value', defaults['vote_skip']['threshold_value']))

            afk = final_config['afk_handling']
            afk['timeout_seconds'] = int(afk.get('timeout_seconds', defaults['afk_handling']['timeout_seconds']))

            auto_s = final_config['auto_start']
            auto_s['delay_seconds'] = int(auto_s.get('delay_seconds', defaults['auto_start']['delay_seconds']))

            # --- NEW: Validate auto_close_empty_room section ---
            ac = final_config['auto_close_empty_room']
            ac['enabled'] = bool(ac.get('enabled', defaults['auto_close_empty_room']['enabled']))
            ac['delay_seconds'] = int(ac.get('delay_seconds', defaults['auto_close_empty_room']['delay_seconds']))
            if ac['delay_seconds'] < 5: # Enforce a minimum delay
                log.warning("auto_close_empty_room delay_seconds cannot be less than 5. Setting to 5.")
                ac['delay_seconds'] = 5
            # --- END NEW Validation ---

            # Remove room_id if it exists
            if 'room_id' in final_config:
                del final_config['room_id']

            log.info(f"Configuration loaded and validated successfully from '{filepath}'.")
            return final_config

    except (json.JSONDecodeError, TypeError) as e:
        log.critical(f"Error parsing config file '{filepath}': {e}. Please check its format.")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Unexpected error loading config: {e}", exc_info=True)
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
def console_input_loop(bot_instance):
    """Handles admin commands entered in the console, adapting based on bot state."""
    global shutdown_requested

    while bot_instance.bot_state == BOT_STATE_INITIALIZING and not shutdown_requested:
        time.sleep(0.5)

    if shutdown_requested:
        log.info("Console input thread exiting early (shutdown requested before connection).")
        return

    log.info("Console input thread active. Type 'help' for available commands.")

    while not shutdown_requested:
        try:
            if bot_instance.bot_state == BOT_STATE_CONNECTED_WAITING:
                prompt = "make/enter/quit > "
            elif bot_instance.bot_state == BOT_STATE_IN_ROOM:
                prompt = "ADMIN (in room) > "
            elif bot_instance.bot_state == BOT_STATE_JOINING:
                prompt = "ADMIN (joining...) > "
            else:
                prompt = "ADMIN (?) > "
                time.sleep(0.5)
                continue

            time.sleep(0.1)
            command_line = input(prompt).strip()
            if not command_line: continue

            parts = shlex.split(command_line)
            command = parts[0].lower()
            args = parts[1:]

            # --- Commands available when CONNECTED_WAITING ---
            if bot_instance.bot_state == BOT_STATE_CONNECTED_WAITING:
                if command == "enter":
                    if len(args) != 1 or not args[0].isdigit():
                        print("Usage: enter <room_id>")
                        log.warning("Invalid 'enter' command usage.")
                        continue
                    room_id = args[0]
                    print(f"Attempting to enter room {room_id}...")
                    log.info(f"Console: Requesting join for room {room_id}")
                    bot_instance.join_room(room_id)

                elif command == "make":
                    if not args:
                        print("Usage: make <room_name> [password]")
                        log.warning("Invalid 'make' command usage (no name).")
                        continue
                    room_name = args[0]
                    password = args[1] if len(args) > 1 else None
                    print(f"Attempting to make room '{room_name}'...")
                    log.info(f"Console: Requesting room creation: Name='{room_name}', Password={'Yes' if password else 'No'}")
                    bot_instance.pending_room_password = password
                    bot_instance.waiting_for_make_response = True
                    bot_instance.send_private_message("BanchoBot", f"!mp make {room_name}")
                    print("Room creation command sent. Waiting for BanchoBot PM with room ID...")

                elif command in ["quit", "exit"]:
                    log.info("Console requested quit while waiting.")
                    bot_instance._request_shutdown()
                    break

                elif command == "help":
                     print("--- Available Commands (Waiting State) ---")
                     print("  enter <room_id>      - Join an existing multiplayer room.")
                     print("  make <name> [pass] - Create a new room (optionally with password).")
                     print("  status               - Show current bot status.")
                     print("  quit / exit          - Disconnect and exit the application.")
                     print("-------------------------------------------")

                elif command == "status":
                     bot_instance.admin_show_status()

                else:
                    print(f"Unknown command '{command}' in waiting state. Use 'enter', 'make', 'status', or 'quit'. Type 'help'.")


            # --- Commands available when IN_ROOM ---
            elif bot_instance.bot_state == BOT_STATE_IN_ROOM:
                config_changed = False

                # Bot Control
                if command in ["quit", "exit"]:
                    log.info("Console requested quit while in room.")
                    bot_instance._request_shutdown()
                    break

                elif command == "stop":
                    log.info("Console requested stop (leave room).")
                    print("Leaving room and returning to make/enter state...")
                    bot_instance.leave_room()

                elif command == "skip":
                    reason = " ".join(args) if args else "Admin command"
                    bot_instance.admin_skip_host(reason)

                elif command in ["queue", "q", "showqueue"]:
                    bot_instance.admin_show_queue()

                elif command in ["status", "info", "showstatus"]:
                     bot_instance.admin_show_status()

                # Lobby Settings (!mp)
                elif command == "set_password":
                     if not args: print("Usage: set_password <new_password|clear>"); continue
                     pw = args[0]
                     if pw.lower() == 'clear':
                         print("Admin: Removing lobby password.")
                         bot_instance.send_message("!mp password")
                     else:
                         print(f"Admin: Setting lobby password to '{pw}'")
                         bot_instance.send_message(f"!mp password {pw}")

                elif command == "set_size":
                     if not args or not args[0].isdigit(): print("Usage: set_size <number 1-16>"); continue
                     try:
                         size = int(args[0])
                         if 1 <= size <= MAX_LOBBY_SIZE:
                             print(f"Admin: Setting lobby size to {size}")
                             bot_instance.send_message(f"!mp size {size}")
                         else:
                             print(f"Invalid size. Must be between 1 and {MAX_LOBBY_SIZE}.")
                     except ValueError:
                         print("Invalid number for size.")

                elif command == "set_name":
                     if not args: print("Usage: set_name <new lobby name>"); continue
                     name = " ".join(args)
                     print(f"Admin: Setting lobby name to '{name}'")
                     bot_instance.send_message(f"!mp name {name}")


                # Bot Feature Toggles
                elif command == "set_rotation":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_rotation <true|false>"); continue
                    hr_config = bot_instance.config['host_rotation']
                    value = args[0].lower() in ['true', 'on']
                    if hr_config['enabled'] != value:
                        hr_config['enabled'] = value
                        print(f"Admin set Host Rotation to: {value}")
                        config_changed = True
                        if value and not bot_instance.host_queue and bot_instance.connection.is_connected():
                             log.info("Rotation enabled with empty queue, requesting !mp settings to populate.")
                             bot_instance.request_initial_settings()
                        elif not value:
                             bot_instance.host_queue.clear()
                             log.info("Rotation disabled by admin. Queue cleared.")
                             if bot_instance.connection.is_connected(): bot_instance.send_message("Host queue cleared (rotation disabled).")
                    else:
                        print(f"Host Rotation already set to {value}.")

                elif command == "set_map_check":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_map_check <true|false>"); continue
                    mc_config = bot_instance.config['map_checker']
                    value = args[0].lower() in ['true', 'on']
                    if mc_config['enabled'] != value:
                        if value and (not bot_instance.api_client_id or bot_instance.api_client_secret == 'YOUR_CLIENT_SECRET'):
                             print("Cannot enable map check: API credentials missing/invalid in config.")
                             log.warning("Admin attempted to enable map check without valid API keys.")
                             continue
                        mc_config['enabled'] = value
                        print(f"Admin set Map Checker to: {value}")
                        config_changed = True
                        if value and bot_instance.current_map_id != 0 and bot_instance.current_host and bot_instance.connection.is_connected():
                            log.info("Map checker enabled, re-validating current map.")
                            bot_instance.check_map(bot_instance.current_map_id, bot_instance.current_map_title)
                    else:
                        print(f"Map Checker already set to {value}.")

                elif command == "set_auto_start":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_auto_start <true|false>"); continue
                    as_config = bot_instance.config['auto_start']
                    value = args[0].lower() in ['true', 'on']
                    if as_config['enabled'] != value:
                        as_config['enabled'] = value
                        print(f"Admin set Auto Start to: {value}")
                        config_changed = True
                    else:
                        print(f"Auto Start already set to {value}.")

                # --- NEW: Admin Commands for Auto-Close ---
                elif command == "set_auto_close":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_auto_close <true|false>"); continue
                    ac_config = bot_instance.config['auto_close_empty_room']
                    value = args[0].lower() in ['true', 'on']
                    if ac_config['enabled'] != value:
                        ac_config['enabled'] = value
                        print(f"Admin set Auto Close Empty Room to: {value}")
                        config_changed = True
                        # If disabled, cancel any active timer
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
                        if value < 5: print("Delay must be at least 5 seconds."); continue # Match config validation
                        ac_config = bot_instance.config['auto_close_empty_room']
                        if ac_config['delay_seconds'] != value:
                            ac_config['delay_seconds'] = value
                            print(f"Admin set Auto Close Delay to: {value} seconds")
                            config_changed = True
                        else:
                            print(f"Auto Close Delay already set to {value} seconds.")
                    except ValueError:
                        print("Invalid number for delay seconds.")
                # --- END NEW Auto-Close Commands ---

                # Other Settings
                elif command == "set_auto_start_delay":
                    if not args or not args[0].isdigit(): print("Usage: set_auto_start_delay <seconds>"); continue
                    try:
                        value = int(args[0])
                        if value < 1: print("Delay must be at least 1 second."); continue
                        as_config = bot_instance.config['auto_start']
                        if as_config['delay_seconds'] != value:
                            as_config['delay_seconds'] = value
                            print(f"Admin set Auto Start Delay to: {value} seconds")
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
                       current_value = bot_instance.config['map_checker'].get('min_stars', 0)
                       if abs(current_value - value) > 0.001:
                           bot_instance.config['map_checker']['min_stars'] = value
                           print(f"Admin set Minimum Star Rating to: {value:.2f}*")
                           config_changed = True
                       else:
                           print(f"Minimum Star Rating already set to {value:.2f}*")
                   except ValueError: print("Invalid number for minimum stars.")

                elif command == "set_star_max":
                    if not args: print("Usage: set_star_max <number|0>"); continue
                    try:
                        value = float(args[0])
                        if value < 0: print("Max stars cannot be negative."); continue
                        current_value = bot_instance.config['map_checker'].get('max_stars', 0)
                        if abs(current_value - value) > 0.001:
                            bot_instance.config['map_checker']['max_stars'] = value
                            print(f"Admin set Maximum Star Rating to: {value:.2f}*")
                            config_changed = True
                        else:
                            print(f"Maximum Star Rating already set to {value:.2f}*")
                    except ValueError: print("Invalid number for maximum stars.")

                elif command == "set_min_len": # In seconds
                    if not args: print("Usage: set_min_len <seconds|0>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Min length cannot be negative."); continue
                        current_value = bot_instance.config['map_checker'].get('min_length_seconds', 0)
                        if current_value != value:
                            bot_instance.config['map_checker']['min_length_seconds'] = value
                            formatted_time = bot_instance._format_time(value)
                            print(f"Admin set Minimum Map Length to: {formatted_time} ({value}s)")
                            config_changed = True
                        else:
                            print(f"Minimum Map Length already set to {bot_instance._format_time(value)}")
                    except ValueError: print("Invalid number for minimum length seconds.")

                elif command == "set_max_len": # In seconds
                    if not args: print("Usage: set_max_len <seconds|0>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Max length cannot be negative."); continue
                        current_value = bot_instance.config['map_checker'].get('max_length_seconds', 0)
                        if current_value != value:
                            bot_instance.config['map_checker']['max_length_seconds'] = value
                            formatted_time = bot_instance._format_time(value)
                            print(f"Admin set Maximum Map Length to: {formatted_time} ({value}s)")
                            config_changed = True
                        else:
                             print(f"Maximum Map Length already set to {bot_instance._format_time(value)}")
                    except ValueError: print("Invalid number for maximum length seconds.")

                elif command == "set_statuses":
                    valid_statuses_lower = [s.lower() for s in OSU_STATUSES]
                    if not args:
                        current = ', '.join(bot_instance.config.get('allowed_map_statuses', ['all']))
                        print(f"Current allowed statuses: {current}")
                        print(f"Usage: set_statuses <status1> [status2...] or 'all'")
                        print(f"Available: {', '.join(OSU_STATUSES)}")
                        continue
                    if args[0].lower() == 'all': value = ['all']
                    else:
                        value = sorted([s.lower() for s in args if s.lower() in valid_statuses_lower])
                        if not value: print(f"Invalid status(es). Available: {', '.join(OSU_STATUSES)} or 'all'"); continue

                    current_value = sorted([s.lower() for s in bot_instance.config.get('allowed_map_statuses', ['all'])])
                    if current_value != value:
                        bot_instance.config['allowed_map_statuses'] = value
                        display_value = ', '.join(value)
                        print(f"Admin set Allowed Map Statuses to: {display_value}")
                        config_changed = True
                    else: print(f"Allowed Map Statuses already set to: {', '.join(value)}")

                elif command == "set_modes":
                    valid_modes_lower = [m.lower() for m in OSU_MODES.values()]
                    if not args:
                        current = ', '.join(bot_instance.config.get('allowed_modes', ['all']))
                        print(f"Current allowed modes: {current}")
                        print(f"Usage: set_modes <mode1> [mode2...] or 'all'")
                        print(f"Available: {', '.join(OSU_MODES.values())}")
                        continue
                    if args[0].lower() == 'all': value = ['all']
                    else:
                        value = sorted([m.lower() for m in args if m.lower() in valid_modes_lower])
                        if not value: print(f"Invalid mode(s). Available: {', '.join(OSU_MODES.values())} or 'all'"); continue

                    current_value = sorted([m.lower() for m in bot_instance.config.get('allowed_modes', ['all'])])
                    if current_value != value:
                        bot_instance.config['allowed_modes'] = value
                        display_value = ', '.join(value)
                        print(f"Admin set Allowed Game Modes to: {display_value}")
                        config_changed = True
                    else: print(f"Allowed Game Modes already set to: {', '.join(value)}")

                # Help Command (In Room)
                elif command == "help":
                   print("--- Admin Console Commands (In Room) ---")
                   print(" Room Control:")
                   print("  stop                - Leave the current room, return to make/enter.")
                   print("  skip [reason]       - Force skip the current host.")
                   print("  queue / q           - Show the current host queue (console).")
                   print("  status / info       - Show current bot and lobby status (console).")
                   print(" Lobby Settings (!mp):")
                   print("  set_password <pw|clear> - Set/remove the lobby password.")
                   print("  set_size <1-16>     - Change the lobby size.")
                   print("  set_name <name>     - Change the lobby name.")
                   print(" Bot Feature Toggles:")
                   print("  set_rotation <t/f>  - Enable/Disable host rotation.")
                   print("  set_map_check <t/f> - Enable/Disable map checker.")
                   print("  set_auto_start <t/f>- Enable/Disable auto starting match when ready.")
                   print("  set_auto_close <t/f>- Enable/Disable auto closing empty bot-created rooms.") # NEW
                   print(" Map Rules (Requires Map Check Enabled):")
                   print("  set_star_min <N>    - Set min star rating (0=off). Ex: set_star_min 4.5")
                   print("  set_star_max <N>    - Set max star rating (0=off). Ex: set_star_max 6.0")
                   print("  set_min_len <sec>   - Set min map length seconds (0=off). Ex: set_min_len 90")
                   print("  set_max_len <sec>   - Set max map length seconds (0=off). Ex: set_max_len 300")
                   print("  set_statuses <...>  - Set allowed map statuses (ranked, loved, etc. or 'all').")
                   print("  set_modes <...>     - Set allowed game modes (osu, mania, etc. or 'all').")
                   print(" Other Settings:")
                   print("  set_auto_start_delay <sec> - Set delay (seconds) for auto start (min 1).")
                   print("  set_auto_close_delay <sec> - Set delay (seconds >=5) for auto close.") # NEW
                   print(" General:")
                   print("  quit / exit         - Disconnect bot and exit application.")
                   print("  help                - Show this help message.")
                   print("------------------------------------------")
                   print("* Bot settings (rotation, map rules, etc.) are saved to config.json immediately.")
                   print("* Lobby settings (password, size, name) use !mp commands and are not saved by the bot.")

                # --- Unknown Command Handling (In Room) ---
                else:
                   print(f"Unknown command: '{command}'. Type 'help' for options.")

                # --- Save Config if Changed ---
                if config_changed:
                    config_to_save = copy.deepcopy(bot_instance.config)
                    if 'room_id' in config_to_save: del config_to_save['room_id']

                    setting_name_for_announce = command.replace('set_', '').replace('_',' ').title()
                    value_for_announce = args[0] if args else 'N/A'
                    if save_config(config_to_save):
                        print("Configuration changes saved to file.")
                        bot_instance.announce_setting_change(setting_name_for_announce, value_for_announce)
                    else:
                        print("ERROR: Failed to save configuration changes to file.")
                        log.error("Failed to save configuration changes to config.json after admin command.")

            # --- Commands available while JOINING ---
            elif bot_instance.bot_state == BOT_STATE_JOINING:
                 if command in ["quit", "exit"]:
                     log.info("Console requested quit while joining.")
                     bot_instance._request_shutdown()
                     break
                 elif command == "status":
                      bot_instance.admin_show_status()
                 else:
                      print(f"Command '{command}' ignored while joining room. Please wait. Use 'status' or 'quit'.")

        except EOFError:
            log.info("Console input closed (EOF). Requesting shutdown.")
            bot_instance._request_shutdown()
            break
        except KeyboardInterrupt:
            if not shutdown_requested:
                 log.info("Console KeyboardInterrupt. Requesting shutdown.")
            break
        except Exception as e:
            log.error(f"Error in console input loop: {e}", exc_info=True)
            print(f"An error occurred processing the command: {e}")
            time.sleep(1)

    log.info("Console input thread finished.")


# --- Main Execution ---
def main():
    global shutdown_requested

    config = load_or_generate_config(CONFIG_FILE)

    bot = None
    try:
        bot = OsuRoomBot(config)
        bot.bot_state = BOT_STATE_INITIALIZING
    except Exception as e:
        log.critical(f"Failed to initialize OsuRoomBot: {e}", exc_info=True)
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    console_thread = threading.Thread(target=console_input_loop, args=(bot,), daemon=True, name="AdminConsoleThread")
    console_thread.start()

    log.info(f"Connecting to {config['server']}:{config['port']} as {config['username']}...")
    try:
        bot.connect(
            server=config['server'],
            port=config['port'],
            nickname=config['username'],
            password=config['password'],
            username=config['username']
        )
    except irc.client.ServerConnectionError as e:
        log.critical(f"Connection failed: {e}")
        err_str = str(e).lower()
        if "nickname is already in use" in err_str: log.critical("Try changing username in config.json.")
        elif "incorrect password" in err_str or "authentication failed" in err_str: log.critical("Incorrect IRC password. Get/Check from osu! website.")
        elif "cannot assign requested address" in err_str or "temporary failure in name resolution" in err_str: log.critical(f"Network error connecting to {config['server']}.")
        else: log.critical(f"Unhandled server connection error: {e}")
        shutdown_requested = True
        bot.bot_state = BOT_STATE_SHUTTING_DOWN
    except Exception as e:
        log.critical(f"Unexpected error during bot.connect: {e}", exc_info=True)
        shutdown_requested = True
        bot.bot_state = BOT_STATE_SHUTTING_DOWN


    # --- Main Loop (IRC Processing + Periodic Checks based on State) ---
    log.info("Starting main processing loop...")
    last_periodic_check = 0
    check_interval = 5 # Seconds between AFK/Vote Timeout/Empty Close checks

    while not shutdown_requested:
        try:
            # Process IRC events
            if hasattr(bot, 'reactor') and bot.reactor:
                bot.reactor.process_once(timeout=0.2)
            else:
                 if not (hasattr(bot, 'connection') and bot.connection and bot.connection.is_connected()):
                      if bot.bot_state not in [BOT_STATE_INITIALIZING, BOT_STATE_SHUTTING_DOWN]:
                           log.warning("Connection lost and reactor unavailable. Requesting shutdown.")
                           bot._request_shutdown()
                 time.sleep(0.2)

            # --- State-dependent actions ---
            current_state = bot.bot_state # Cache state for this iteration

            if current_state == BOT_STATE_SHUTTING_DOWN:
                 break # Exit loop immediately if shutdown requested

            if current_state == BOT_STATE_IN_ROOM:
                # Perform periodic checks ONLY when in a room and connected
                if bot.connection.is_connected():
                    now = time.time()
                    if now - last_periodic_check > check_interval:
                        log.debug("Running periodic checks (AFK, Vote Timeout, Empty Close)...")
                        bot.check_afk_host()
                        bot.check_vote_skip_timeout()
                        bot.check_empty_room_close() # <<< NEW CALL
                        last_periodic_check = now
            elif current_state == BOT_STATE_CONNECTED_WAITING:
                 pass
            elif current_state == BOT_STATE_JOINING:
                 pass
            elif current_state == BOT_STATE_INITIALIZING:
                 pass


        except irc.client.ServerNotConnectedError:
            if bot.bot_state != BOT_STATE_SHUTTING_DOWN:
                 log.warning("Disconnected during processing loop. Requesting shutdown.")
                 bot._request_shutdown()
        except KeyboardInterrupt:
             if not shutdown_requested:
                 log.info("Main loop KeyboardInterrupt. Requesting shutdown.")
                 shutdown_requested = True
                 bot.bot_state = BOT_STATE_SHUTTING_DOWN
             break
        except Exception as e:
            log.error(f"Unhandled exception in main loop: {e}", exc_info=True)
            time.sleep(2)

    # --- Shutdown Sequence ---
    log.info("Main loop exited. Initiating final shutdown...")
    if bot:
        bot.bot_state = BOT_STATE_SHUTTING_DOWN
        bot.shutdown("Client shutting down normally.")

    log.info("Bot finished.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
         log.info(f"Program exited with code {e.code}.")
         sys.exit(e.code)
    except KeyboardInterrupt:
         log.info("\nMain execution interrupted by Ctrl+C during startup/shutdown. Exiting.")
         logging.shutdown()
    except Exception as e:
        log.critical(f"Critical error during main execution: {e}", exc_info=True)
        logging.shutdown()
        sys.exit(1)
    finally:
        logging.shutdown()