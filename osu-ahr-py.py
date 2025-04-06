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
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger("OsuIRCBot")
log.setLevel(logging.INFO)

# --- Configuration ---
CONFIG_FILE = Path("config.json")

# --- Global State ---
shutdown_requested = False
osu_api_token_cache = {'token': None, 'expiry': 0}

# --- Constants ---
OSU_MODES = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}
OSU_STATUSES_NUM = {-2: "graveyard", -1: "wip", 0: "pending", 1: "ranked", 2: "approved", 3: "qualified", 4: "loved"}
OSU_STATUSES_STR = {v: k for k, v in OSU_STATUSES_NUM.items()}
MAX_LOBBY_SIZE = 16
BOT_STATE_INITIALIZING = "INITIALIZING"
BOT_STATE_CONNECTED_WAITING = "CONNECTED_WAITING"
BOT_STATE_JOINING = "JOINING"
BOT_STATE_IN_ROOM = "IN_ROOM"
BOT_STATE_SHUTTING_DOWN = "SHUTTING_DOWN"

# --- Simple Event Emitter ---
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

    if osu_api_token_cache.get('token') and now < osu_api_token_cache.get('expiry', 0):
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
    except requests.exceptions.HTTPError as e:
        log.error(f"HTTP error getting osu! API token: {e.response.status_code}")
        if e.response.status_code == 401:
             log.error(" -> Unauthorized (401): Check your osu_api_client_id and osu_api_client_secret in config.json.")
        log.error(f"Response content: {e.response.text[:500]}")
        osu_api_token_cache = {'token': None, 'expiry': 0}
        return None
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        log.error(f"Failed to get/parse osu! API token: {e}")
        osu_api_token_cache = {'token': None, 'expiry': 0}
        return None
    except Exception as e:
        log.error(f"Unexpected error getting API token: {e}", exc_info=True)
        osu_api_token_cache = {'token': None, 'expiry': 0}
        return None

# --- Helper Function: Get Beatmap Info ---
def get_beatmap_info(map_id, client_id, client_secret):
    """Fetches detailed beatmap information from the osu! API v2."""
    if not client_id or not client_secret or client_secret == "YOUR_CLIENT_SECRET":
        log.warning("osu! API credentials missing/default. Cannot check map.")
        return None

    token = get_osu_api_token(client_id, client_secret)
    if not token:
        log.error("Cannot check map info without API token.")
        return None # Simplified error return

    api_url = f"https://osu.ppy.sh/api/v2/beatmaps/{map_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}

    try:
        log.debug(f"Requesting beatmap info for ID: {map_id}")
        response = requests.get(api_url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        log.debug(f"API response for {map_id}: {data}")

        status_num = data.get('status', 'unknown')
        # Handle cases where status might already be a string like 'ranked', 'loved' etc.
        status_str = status_num if isinstance(status_num, str) else OSU_STATUSES_NUM.get(status_num, 'unknown')

        beatmap_details = {
            'stars': data.get('difficulty_rating'),
            'length': data.get('total_length'),
            'title': data.get('beatmapset', {}).get('title', 'Unknown Title'),
            'version': data.get('version', 'Unknown Difficulty'),
            'status': status_str.lower(),
            'mode': data.get('mode', 'unknown'), # 'osu', 'taiko', 'fruits', 'mania'
            'beatmap_id': data.get('id'), # Beatmap specific ID
            'beatmapset_id': data.get('beatmapset_id'), # Beatmap SET ID
            'cs': data.get('cs'),
            'ar': data.get('ar'),
            'od': data.get('accuracy'), # Accuracy is OD
            'hp': data.get('drain'),     # Drain is HP
            'bpm': data.get('bpm'),
            'count_circles': data.get('count_circles'),
            'count_sliders': data.get('count_sliders'),
            'count_spinners': data.get('count_spinners')
        }
        return beatmap_details
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            log.warning(f"HTTP 404 fetching map {map_id}. It might be deleted or restricted.")
        else:
            log.warning(f"HTTP error fetching map {map_id}: {e.response.status_code}")
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
    try:
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
        self.config = config
        self.runtime_config = copy.deepcopy(config)
        self.api_client_id = self.runtime_config.get('osu_api_client_id', 0)
        self.api_client_secret = self.runtime_config.get('osu_api_client_secret', '')

        self.target_channel = None
        self.connection_registered = False
        self.bot_state = BOT_STATE_INITIALIZING

        # Room Specific State
        self.is_matching = False
        self.host_queue = deque()
        self.current_host = None
        self.is_rotating_host = False
        self.last_host = None
        self.host_last_action_time = 0
        self.host_map_selected_valid = False
        self.players_in_lobby = set()
        self.current_map_id = 0       # Map ID currently selected in lobby (even if invalid)
        self.current_map_title = ""   # Title for current_map_id
        self.last_valid_map_id = 0    # Last map ID that passed validation (used for revert and tracking finished map)
        self.map_violations = {}
        self.vote_skip_active = False
        self.vote_skip_target = None
        self.vote_skip_initiator = None
        self.vote_skip_voters = set()
        self.vote_skip_start_time = 0
        self.room_was_created_by_bot = False
        self.empty_room_close_timer_active = False
        self.empty_room_timestamp = 0
        self.initial_slot_players = []
        self._tentative_host_from_settings = None
        self._reverting_to_map_id = 0 # Flag to track if we are reverting
        self._expected_next_host = None # Track host expected after rotation cmd
        self._rotation_start_time = 0   # Safety timeout for rotation state
        
        
        # <<< Maps Played History State >>>
        played_list_config = self.runtime_config.get('maps_played_list', {})
        max_hist_size = played_list_config.get('max_size', 5)
        if not isinstance(max_hist_size, int) or max_hist_size < 0:
             log.warning(f"Invalid maps_played_list.max_size ({max_hist_size}). Using default 5.")
             max_hist_size = 5
             self.runtime_config['maps_played_list']['max_size'] = max_hist_size # Correct runtime config
        self.played_maps_history = deque(maxlen=max_hist_size)
        log.info(f"Initialized played maps history (Max Size: {max_hist_size}, Enabled: {played_list_config.get('enabled', False)})")
        # <<< END >>>

        # Make Room State
        self.waiting_for_make_response = False
        self.pending_room_password = None

        # Initialization State
        self._initialization_timer = None
        self._initialization_pending = False

        # Events
        self.JoinedLobby = TypedEvent()
        self.PlayerJoined = TypedEvent()
        self.PlayerLeft = TypedEvent()
        self.HostChanged = TypedEvent()
        self.MatchStarted = TypedEvent()
        self.MatchFinished = TypedEvent()
        self.SentMessage = TypedEvent()

        # Initial Validation
        if self.runtime_config['map_checker']['enabled'] and (not self.api_client_id or self.api_client_secret == 'YOUR_CLIENT_SECRET'):
            log.warning("Map checker enabled in config but API keys missing/default. Disabling map check feature.")
            self.runtime_config['map_checker']['enabled'] = False

        log.info("Bot instance initialized.")

    def reset_room_state(self):
        """Clears all state specific to being inside a room."""
        log.info("Resetting internal room state.")
        self._cancel_pending_initialization()
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
        self.clear_vote_skip("Room state reset")
        self.room_was_created_by_bot = False
        self.empty_room_close_timer_active = False
        self.empty_room_timestamp = 0
        self.initial_slot_players.clear()
        self._tentative_host_from_settings = None
        # <<< Clear played maps history >>>
        self.played_maps_history.clear()
        log.debug("Cleared played maps history.")
        # <<< END >>>
        self._reverting_to_map_id = 0
        self._expected_next_host = None
        self._rotation_start_time = 0

    def log_feature_status(self):
        """Logs the status of major configurable features using runtime_config."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        hr_enabled = self.runtime_config.get('host_rotation', {}).get('enabled', False)
        mc_enabled = self.runtime_config.get('map_checker', {}).get('enabled', False)
        vs_enabled = self.runtime_config.get('vote_skip', {}).get('enabled', False)
        afk_enabled = self.runtime_config.get('afk_handling', {}).get('enabled', False)
        as_enabled = self.runtime_config.get('auto_start', {}).get('enabled', False)
        ac_enabled = self.runtime_config.get('auto_close_empty_room', {}).get('enabled', False)
        ac_delay = self.runtime_config.get('auto_close_empty_room', {}).get('delay_seconds', 60)
        # <<< Maps Played List Status >>>
        mpl_config = self.runtime_config.get('maps_played_list', {})
        mpl_enabled = mpl_config.get('enabled', False)
        mpl_size = mpl_config.get('max_size', 0)
        # <<< END >>>

        # <<< Added MPL to log string >>>
        log.info(f"Features: Rotation:{hr_enabled}, MapCheck:{mc_enabled}, VoteSkip:{vs_enabled}, AFKCheck:{afk_enabled}, AutoStart:{as_enabled}, AutoClose:{ac_enabled}({ac_delay}s), MapHistory:{mpl_enabled}({mpl_size})")
        # <<< END >>>

        if mc_enabled:
            self.log_map_rules()

    def announce_setting_change(self, setting_name, new_value):
        """Sends a notification to the chat when an admin changes a setting."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.connection.is_connected() or not self.target_channel:
            log.warning("Cannot announce setting change, not in a room or not connected.")
            return

        message = f"Admin updated setting: {setting_name} set to {new_value}"
        log.info(f"Announcing to chat: {message}")
        self.send_message(message)
        self.log_feature_status() # Re-log feature status after change
        if setting_name == "Allowed Statuses": self.log_map_rules()
        if setting_name == "Allowed Modes": self.log_map_rules()
        # Re-check map if rules changed and map check is on
        if setting_name.startswith("Min") or setting_name.startswith("Max") or setting_name.startswith("Allowed"):
            if self.runtime_config['map_checker']['enabled'] and self.current_map_id != 0 and self.current_host:
                log.info(f"Map rules changed by admin ({setting_name}), re-validating current map {self.current_map_id}.")
                self.host_map_selected_valid = False
                threading.Timer(1.0, self.check_map, args=[self.current_map_id, self.current_map_title]).start()

        # <<< Handle resize of played history deque >>>
        if setting_name == "Maps Played List Size":
            try:
                 # Value should be int already from admin command validation
                 new_size = int(new_value)
                 if new_size >= 0 and self.played_maps_history.maxlen != new_size:
                     current_history = list(self.played_maps_history) # Get current items
                     # Recreate deque with new maxlen, preserving content order
                     self.played_maps_history = deque(current_history, maxlen=new_size)
                     log.info(f"Recreated played maps history deque with maxlen={new_size}.")
                 elif self.played_maps_history.maxlen == new_size:
                     log.debug(f"Played maps history size already {new_size}, no change needed.")
                 else:
                     log.error(f"Cannot resize played maps deque, invalid size announced: {new_size}.")
            except (ValueError, TypeError) as e:
                 log.error(f"Cannot resize played maps deque, announced value '{new_value}' is not a valid integer: {e}")
        # <<< END >>>

    def log_map_rules(self):
        """Logs the current map checking rules to the console using runtime_config."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.runtime_config['map_checker']['enabled']:
            log.info("Map checker is disabled.")
            return
        mc = self.runtime_config['map_checker']
        statuses = self.runtime_config.get('allowed_map_statuses', ['all'])
        modes = self.runtime_config.get('allowed_modes', ['all'])
        log.info(f"Map Rules: Stars {mc.get('min_stars', 0):.2f}-{mc.get('max_stars', 0):.2f}, "
                 f"Len {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}, "
                 f"Map Status: {', '.join(statuses)}, Modes: {', '.join(modes)}")

    def display_map_rules_to_chat(self):
         """Sends map rule information to the chat using runtime_config. Aim for 1-2 messages."""
         if self.bot_state != BOT_STATE_IN_ROOM: return
         messages = []
         if self.runtime_config['map_checker']['enabled']:
             mc = self.runtime_config['map_checker']
             statuses = self.runtime_config.get('allowed_map_statuses', ['all'])
             modes = self.runtime_config.get('allowed_modes', ['all'])
             line1 = f"Rules: Stars {mc.get('min_stars', 0):.2f}*-{mc.get('max_stars', 0):.2f}*, Length {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}"
             line2 = f"Status: {', '.join(statuses)}; Modes: {', '.join(modes)}; Violations: {mc.get('violations_allowed', 3)}"
             messages.append(line1)
             messages.append(line2)
             # <<< Add played map history info if enabled >>>
             mpl_config = self.runtime_config.get('maps_played_list', {})
             if mpl_config.get('enabled', False):
                  messages.append(f"History: Maps played in the last {mpl_config.get('max_size', 0)} finished matches cannot be picked again.")
             # <<< END >>>
         else:
             messages.append("Map checking is currently disabled.")
         self.send_message(messages) # Will send 1-3 messages

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
        self.bot_state = BOT_STATE_CONNECTED_WAITING
        log.info("Bot connected to IRC. Waiting for 'make' or 'enter' command in console.")

    def on_nicknameinuse(self, connection, event):
        old_nick = connection.get_nickname()
        new_nick = old_nick + "_"
        log.warning(f"Nickname '{old_nick}' in use. Trying '{new_nick}'")
        try:
            connection.nick(new_nick)
        except irc.client.ServerNotConnectedError:
            log.warning("Connection lost before nick change.")
            self._request_shutdown("Nickname change failed")
        except Exception as e:
            log.error(f"Unexpected error changing nick: {e}")
            self._request_shutdown("Nickname change failed")

    def _handle_channel_join_error(self, event, error_type):
        channel = event.arguments[0] if event.arguments else "UnknownChannel"
        log.error(f"Cannot join '{channel}': {error_type}.")
        if self.bot_state == BOT_STATE_JOINING and self.target_channel and channel.lower() == self.target_channel.lower():
            log.warning(f"Failed to join target channel '{self.target_channel}' ({error_type}). Returning to waiting state.")
            admin_user = self.runtime_config.get('username')
            if admin_user:
                self.send_private_message(admin_user, f"Failed to join room {channel}: {error_type}")
            self.reset_room_state()
            self.bot_state = BOT_STATE_CONNECTED_WAITING
        elif self.bot_state == BOT_STATE_JOINING:
             log.warning(f"Failed join event for '{channel}', but it wasn't the target channel ('{self.target_channel}'). Ignoring.")

    def on_err_nosuchchannel(self, c, e): self._handle_channel_join_error(e, "No such channel/Invalid ID")
    def on_err_bannedfromchan(self, c, e): self._handle_channel_join_error(e, "Banned")
    def on_err_channelisfull(self, c, e): self._handle_channel_join_error(e, "Channel full")
    def on_err_inviteonlychan(self, c, e): self._handle_channel_join_error(e, "Invite only")
    def on_err_badchannelkey(self, c, e): self._handle_channel_join_error(e, "Bad key")

    def on_join(self, connection, event):
        channel = event.target
        nick = event.source.nick
        if nick == connection.get_nickname() and self.target_channel and channel.lower() == self.target_channel.lower():
            log.info(f"Successfully joined {channel}")
            self.bot_state = BOT_STATE_IN_ROOM
            if not self.room_was_created_by_bot:
                 log.info("Entered existing room, resetting state before requesting settings.")
                 self.reset_room_state()
                 self.target_channel = channel
            if self.runtime_config.get("welcome_message"):
                threading.Timer(0.5, self.send_message, args=[self.runtime_config["welcome_message"]]).start()
            settings_request_delay = 2.0
            log.info(f"Scheduling request for initial settings (!mp settings) in {settings_request_delay}s")
            threading.Timer(settings_request_delay, self.request_initial_settings).start()
            self.JoinedLobby.emit({'channel': channel})
        elif nick == connection.get_nickname():
            log.info(f"Joined other channel: {channel} (Ignoring)")
        elif self.target_channel and channel.lower() == self.target_channel.lower():
            log.debug(f"User '{nick}' joined channel {channel}. Waiting for Bancho message for processing.")
            pass

    def on_part(self, connection, event):
        channel = event.target
        nick = event.source.nick
        if nick == connection.get_nickname() and self.target_channel and channel.lower() == self.target_channel.lower():
            log.info(f"Bot left channel {channel}.")
            if self.bot_state == BOT_STATE_IN_ROOM:
                 log.warning(f"Unexpectedly left channel {channel} while in IN_ROOM state. Returning to waiting state.")
                 self.reset_room_state()
                 self.bot_state = BOT_STATE_CONNECTED_WAITING
            elif self.bot_state == BOT_STATE_JOINING:
                 log.warning(f"Left channel {channel} while still in JOINING state. Returning to waiting state.")
                 self.reset_room_state()
                 self.bot_state = BOT_STATE_CONNECTED_WAITING
        elif self.target_channel and channel.lower() == self.target_channel.lower():
             log.debug(f"User '{nick}' left channel {channel}. Waiting for Bancho message for processing.")
             pass

    def on_kick(self, connection, event):
        channel = event.target
        kicked_nick = event.arguments[0]
        if kicked_nick == connection.get_nickname() and self.target_channel and channel.lower() == self.target_channel.lower():
            log.warning(f"Kicked from channel {channel}. Returning to waiting state.")
            self.reset_room_state()
            self.bot_state = BOT_STATE_CONNECTED_WAITING
        elif self.target_channel and channel.lower() == self.target_channel.lower():
             log.debug(f"User '{kicked_nick}' kicked from channel {channel}. Waiting for Bancho message for processing.")
             pass

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
        log_message = message
        if sender == "BanchoBot" and "Created the tournament match" in message:
            log_message = "Received BanchoBot PM (likely room creation confirmation)."
        log.info(f"[PRIVATE] <{sender}> {log_message}")
        if sender == "BanchoBot" and self.waiting_for_make_response:
            log.debug(f"Checking PM from BanchoBot for 'make' response: {message}")
            match = re.search(r"Created the tournament match https://osu\.ppy\.sh/mp/(\d+)", message)
            if match:
                new_room_id = match.group(1)
                log.info(f"Detected newly created room ID: {new_room_id} from BanchoBot PM.")
                self.waiting_for_make_response = False
                self.reset_room_state()
                self.target_channel = f"#mp_{new_room_id}"
                self.room_was_created_by_bot = True
                log.info(f"Bot automatically created room {self.target_channel}. Marked as bot-created.")
                
                self.bot_state = BOT_STATE_IN_ROOM  # Transition to IN_ROOM state immediately
                log.info(f"Bot automatically joined {self.target_channel}. State set to IN_ROOM.")
                
                # Clear password by default unless one was explicitly set
                if not self.pending_room_password:
                    log.info(f"No password specified in 'make' command. Clearing any default password for {self.target_channel}.")
                    self.send_message("!mp password")  # Clears the password
                # Set password if pending from 'make' command
                elif self.pending_room_password:
                    log.info(f"Setting password for room {self.target_channel} to '{self.pending_room_password}' as requested by 'make' command.")
                    self.send_message(f"!mp password {self.pending_room_password}")
                    self.pending_room_password = None  # Clear pending password  

            else:
                log.warning(f"Received PM from BanchoBot while waiting for 'make' response, but didn't match expected pattern: {message}")
                if self.waiting_for_make_response:
                    def clear_wait_flag():
                        if self.waiting_for_make_response:
                             log.warning("Timeout waiting for BanchoBot 'make' PM response. Resetting flag.")
                             self.waiting_for_make_response = False
                             if self.bot_state != BOT_STATE_IN_ROOM:
                                  self.bot_state = BOT_STATE_CONNECTED_WAITING
                    threading.Timer(20.0, clear_wait_flag).start()

    def on_pubmsg(self, connection, event):
        if self.bot_state != BOT_STATE_IN_ROOM or not self.target_channel:
            if not (event.source.nick == "BanchoBot" and self._initialization_pending):
                return
        sender = event.source.nick
        channel = event.target
        message = event.arguments[0]
        if not self.target_channel or channel.lower() != self.target_channel.lower(): return
        if sender != connection.get_nickname():
            log.info(f"[{channel}] <{sender}> {message}")
        if sender == "BanchoBot":
            self.parse_bancho_message(message)
        else:
            if not self._initialization_pending:
                self.parse_user_command(sender, message)
            else:
                log.debug(f"Ignoring user message from {sender} during initialization phase.")

    # --- Room Joining/Leaving ---
    def join_room(self, room_id):
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
        self.room_was_created_by_bot = False
        self.target_channel = f"#mp_{room_id}"
        self.bot_state = BOT_STATE_JOINING
        log.info(f"Attempting to join channel: {self.target_channel}")
        try:
            self.connection.join(self.target_channel)
        except irc.client.ServerNotConnectedError:
            log.warning("Connection lost before join command could be sent.")
            self._request_shutdown("Connection lost")
        except Exception as e:
            log.error(f"Error sending join command for {self.target_channel}: {e}", exc_info=True)
            self.reset_room_state()
            self.bot_state = BOT_STATE_CONNECTED_WAITING

    def _cancel_pending_initialization(self):
        if self._initialization_timer and self._initialization_timer.is_alive():
            log.info("Cancelling pending initialization timer.")
            self._initialization_timer.cancel()
        self._initialization_pending = False
        self._initialization_timer = None

    def leave_room(self):
        self._cancel_pending_initialization()
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
                self.connection.part(current_channel, "Leaving room (stop/close command)")
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
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not message.startswith("!"): return
        sender_clean = sender.strip()
        try:
            parts = shlex.split(message)
        except ValueError:
            log.warning(f"Could not parse command with shlex (likely quotes issue): {message}")
            parts = message.split()
        if not parts: return
        command = parts[0].lower()
        args = parts[1:]
        if command == '!queue':
            if self.runtime_config['host_rotation']['enabled']:
                log.info(f"{sender_clean} requested host queue.")
                self.display_host_queue()
            else:
                self.send_message("Host rotation is currently disabled.")
            return
        if command == '!help':
            log.info(f"{sender_clean} requested help.")
            self.display_help_message()
            return
        if command == '!rules':
            log.info(f"{sender_clean} requested rules.")
            self.display_map_rules_to_chat()
            return
        if command == '!voteskip':
            vs_config = self.runtime_config['vote_skip']
            if not vs_config['enabled']:
                self.send_message("Vote skipping is disabled.")
                return
            if not self.current_host:
                self.send_message("There is no host to skip.")
                return
            if sender_clean == self.current_host:
                self.send_message("You can't vote to skip yourself! Use !skip if host rotation is enabled.")
                return
            self.handle_vote_skip(sender_clean)
            return
        if sender_clean == self.current_host:
            if command == '!skip':
                if self.runtime_config['host_rotation']['enabled']:
                    log.info(f"Host {sender_clean} used !skip.")
                    self.skip_current_host("Host self-skipped")
                else:
                    log.info(f"{sender_clean} tried to use !skip (rotation disabled).")
                    self.send_message("Host rotation is disabled, !skip command is inactive.")
                return
            if command == '!start':
                log.info(f"Host {sender_clean} trying to use !start...")
                if self.is_matching:
                    self.send_message("Match is already in progress.")
                    return
                if self.current_map_id == 0:
                    self.send_message("No map selected to start.")
                    return
                # Check map validity if checker enabled (stricter start)
                if self.runtime_config['map_checker']['enabled'] and not self.host_map_selected_valid:
                   log.warning(f"Host {sender_clean} tried !start but map {self.current_map_id} is not marked valid.")
                   self.send_message(f"Cannot start: Current map ({self.current_map_id}) is invalid or was not checked/accepted.")
                   return

                delay_str = ""
                delay_seconds = 0
                if args:
                    try:
                        delay_seconds = int(args[0])
                        if delay_seconds < 0: delay_seconds = 0
                        if delay_seconds > 300: delay_seconds = 300
                        delay_str = f" {delay_seconds}"
                    except ValueError:
                        self.send_message("Invalid delay for !start. Use a number like '!start 5'.")
                        return
                log.info(f"Host {sender_clean} sending !mp start{delay_str}")
                self.send_message(f"!mp start{delay_str}")
                return
            if command == '!abort':
                log.info(f"Host {sender_clean} sending !mp abort")
                self.send_message("!mp abort")
                return
        elif command in ['!skip', '!start', '!abort']:
             log.info(f"{sender_clean} tried to use host command '{command}' (not host).")
             self.send_message(f"Only the current host ({self.current_host}) can use {command}.")
             return
        log.debug(f"Ignoring unknown/restricted command '{command}' from {sender_clean}.")

    def display_help_message(self):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        messages = [
            "osu-ahr-py bot help: !queue !skip !voteskip !rules !help",
            "Host Only: !start [delay_seconds] !abort",
        ]
        if self.runtime_config['map_checker']['enabled']:
             mc = self.runtime_config['map_checker']
             rule_summary = f"Map Rules: {mc.get('min_stars',0):.2f}-{mc.get('max_stars',0):.2f}*, {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}. Use !rules."
             messages.append(rule_summary)

        # <<< Added MPL(H) to feature list in help >>>
        mpl_enabled = self.runtime_config.get('maps_played_list', {}).get('enabled', False)
        messages.append(f"Bot Version/Info: [github.com/serifpersia/osu-ahr-py] | Active Features: R({self.runtime_config['host_rotation']['enabled']}) M({self.runtime_config['map_checker']['enabled']}) V({self.runtime_config['vote_skip']['enabled']}) A({self.runtime_config['afk_handling']['enabled']}) S({self.runtime_config['auto_start']['enabled']}) C({self.runtime_config['auto_close_empty_room']['enabled']}) H({mpl_enabled})")
        # <<< END >>>

        self.send_message(messages)

# --- BanchoBot Message Parsing (In Room) ---
    def parse_bancho_message(self, msg):
        # Allow processing settings messages during initialization phase
        is_settings_message = msg.startswith("Slot ") or \
                              msg.startswith("Room name:") or \
                              msg.startswith("Beatmap:") or \
                              msg.startswith("Team mode:") or \
                              msg.startswith("Win condition:") or \
                              msg.startswith("Active mods:") or \
                              msg.startswith("Players:")

        if self.bot_state != BOT_STATE_IN_ROOM and not (self._initialization_pending and is_settings_message):
            log.debug(f"Ignoring Bancho message while not IN_ROOM or during init (unless settings): {msg[:50]}...")
            return

        log.debug(f"Parsing Bancho: {msg}")
        try:
            # --- Match State ---
            if msg == "The match has started!": self.handle_match_start()
            elif msg == "The match has finished!": self.handle_match_finish()
            elif msg == "Match Aborted": self.handle_match_abort()
            elif msg == "Closed the match": self.handle_match_close()
            elif msg == "All players are ready": self.handle_all_players_ready()

            # --- Host & Player Changes ---
            elif " became the host." in msg:
                match = re.match(r"(.+?) became the host\.", msg)
                if match: self.handle_host_change(match.group(1).strip())
            elif " joined in slot " in msg:
                match = re.match(r"(.+?) joined in slot \d+\.", msg)
                if match: self.handle_player_join(match.group(1).strip())
            elif " left the game." in msg:
                match = re.match(r"(.+?) left the game\.", msg)
                if match: self.handle_player_left(match.group(1).strip())
            elif " was kicked from the room." in msg:
                 match = re.match(r"(.+?) was kicked from the room\.", msg)
                 if match: self.handle_player_left(match.group(1).strip())

            # --- Map Changes (Modified to handle both formats) ---
            elif msg.startswith("Host is changing map..."):
                 if self.current_host:
                     log.info(f"Host '{self.current_host}' started changing map. Resetting map validity flag and host action timer.")
                     self.host_map_selected_valid = False
                     # Reset AFK timer base immediately
                     self.host_last_action_time = time.time()
                     log.debug(f"*** host_last_action_time set to {self.host_last_action_time:.1f} in parse_bancho_message (Host is changing map...)")
                 else:
                     log.debug("Ignoring 'Host is changing map...' message, no current host tracked.")
            # <<< FIX: Check for both "Beatmap changed to:" and "Changed beatmap to " >>>
            elif msg.startswith("Beatmap changed to: ") or msg.startswith("Changed beatmap to "):
                # Use more robust regex to handle both cases and extract ID/Title
                # Regex tries to find /b/ link OR beatmapsets link first
                map_id_match = re.search(r"/(?:b|beatmaps)/(\d+)|/beatmapsets/\d+#(?:osu|taiko|fruits|mania)/(\d+)", msg)
                # Regex to get title is trickier if format varies, try common patterns
                # Pattern 1: "Beatmap changed to: Title (URL)"
                map_title_match1 = re.match(r"Beatmap changed to: (.*?)\s*\(https?://osu\.ppy\.sh/.*\)", msg)
                # Pattern 2: "Changed beatmap to URL Title" (less common but possible)
                map_title_match2 = re.match(r"Changed beatmap to https?://osu\.ppy\.sh/.*\s+(.+)$", msg)

                map_id = None
                if map_id_match:
                    map_id_str = map_id_match.group(1) or map_id_match.group(2)
                    if map_id_str:
                        try: map_id = int(map_id_str)
                        except ValueError: log.error(f"Could not convert map ID string '{map_id_str}' to int. Msg: {msg}")

                if map_id:
                    title = "Unknown Title" # Default
                    if map_title_match1:
                        title = map_title_match1.group(1).strip()
                    elif map_title_match2:
                         title = map_title_match2.group(1).strip()
                    # Add a fallback using API if title parsing failed? Maybe later.
                    log.debug(f"Map change message detected for ID: {map_id}, Parsed Title: '{title}'")
                    self.handle_map_change(map_id, title) # This now also resets the timer
                else:
                    log.warning(f"Could not parse map ID from map change msg: {msg}")
            # <<< END FIX >>>

            # --- Settings Parsing ---
            elif msg.startswith("Room name:"): pass
            elif msg.startswith("History is "): pass
            elif msg.startswith("Beatmap: "): self._parse_initial_beatmap(msg)
            elif msg.startswith("Players:"): self._parse_player_count(msg)
            elif msg.startswith("Slot "): self._parse_slot_message(msg)
            elif msg.startswith("Team mode:") or msg.startswith("Win condition:") or msg.startswith("Active mods:") or msg.startswith("Free mods"):
                 pass # End of settings block signals

            # --- Ignored Messages ---
            elif " changed the room name to " in msg: pass
            elif " changed the password." in msg: pass
            elif " removed the password." in msg: pass
            elif " changed room size to " in msg: pass
            elif msg.startswith("User not found"): pass
            elif msg == "The match has already been started": pass
            elif msg.startswith("Queued the match to start in "): pass
            elif msg.startswith("Match starts in "): pass
            elif " finished playing " in msg: pass
            elif msg.startswith("Stats for"): pass
            elif re.match(r"\s*#\d+\s+.+?\s+-\s+\d+\s+-.+", msg): pass # Score lines
            # Ignore redundant change messages if already handled
            # elif msg.startswith("Changed beatmap to "): pass # Now handled by the combined check above
            elif msg.startswith("Changed match host to "): pass # Already handled by "... became the host."

            else:
                log.debug(f"Ignoring unrecognized BanchoBot message: {msg}")
        except Exception as e:
            log.error(f"Error parsing Bancho msg: '{msg}' - {e}", exc_info=True)

    def _parse_initial_beatmap(self, msg):
            map_id_match = re.search(r"/(?:b|beatmaps)/(\d+)|/beatmapsets/\d+#(?:osu|taiko|fruits|mania)/(\d+)", msg)
            map_title_match = re.match(r"Beatmap: https?://osu\.ppy\.sh/.*\s+(.+)$", msg)
            map_id = None
            if map_id_match:
                map_id_str = map_id_match.group(1) or map_id_match.group(2)
                if map_id_str:
                    try: map_id = int(map_id_str)
                    except ValueError: log.error(f"Could not convert initial map ID str '{map_id_str}' to int. Msg: {msg}")
            if map_id:
                self.current_map_id = map_id
                self.current_map_title = map_title_match.group(1).strip() if map_title_match else "Unknown Title (from settings)"
                log.info(f"Initial map set from settings: ID {self.current_map_id}, Title: '{self.current_map_title}'")
                self.last_valid_map_id = 0 # Cannot assume initial map is valid yet
                self.host_map_selected_valid = False
            else:
                log.warning(f"Could not parse initial beatmap msg: {msg}")
                self.current_map_id = 0
                self.current_map_title = ""
                self.last_valid_map_id = 0
                self.host_map_selected_valid = False

    def _parse_player_count(self, msg):
        match = re.match(r"Players: (\d+)", msg)
        if match: log.debug(f"Parsed player count from settings: {match.group(1)}")
        else: log.warning(f"Could not parse player count msg: {msg}")

    def _parse_slot_message(self, msg):
        log.debug(f"Attempting to parse slot message: '{msg}'")
        match = re.match(r"Slot (\d+)\s+(?:Not Ready|Ready)\s+https://osu\.ppy\.sh/u/\d+\s+(.+)", msg)
        if not match:
            match_empty = re.match(r"Slot (\d+)\s+(Open|Locked)", msg)
            if match_empty: log.debug(f"Ignoring empty/locked slot msg: {msg}")
            else: log.debug(f"No player match in slot msg (regex failed): {msg}")
            return
        slot = int(match.group(1))
        full_player_string = match.group(2).strip()
        if not full_player_string or full_player_string.lower() == "banchobot":
            log.debug(f"Ignoring BanchoBot or empty player string in slot {slot}.")
            return
        player_name = full_player_string
        host_marker = False
        if '[' in full_player_string:
            player_name = full_player_string.split('[')[0].strip()
            host_marker = "[host / hidden]" in full_player_string.lower() or "[host]" in full_player_string.lower()
        if not player_name:
            log.warning(f"Parsed empty player name from slot {slot} string: '{full_player_string}'")
            return
        log.info(f"Parsed slot {slot}: '{player_name}' (Host: {host_marker}) from !mp settings.")
        self.initial_slot_players.append((slot, player_name))
        if player_name not in self.players_in_lobby:
            log.info(f"Adding player '{player_name}' to lobby list from slot {slot}.")
            self.players_in_lobby.add(player_name)
            if self.empty_room_close_timer_active:
                log.info(f"Player '{player_name}' detected via settings while timer active. Cancelling auto-close timer.")
                self.empty_room_close_timer_active = False
                self.empty_room_timestamp = 0
        if host_marker:
            if self._tentative_host_from_settings != player_name:
                 log.info(f"Host marker found for '{player_name}' in slot {slot}. Setting as tentative host.")
                 self._tentative_host_from_settings = player_name
        if self.runtime_config['host_rotation']['enabled']:
            if player_name not in self.host_queue:
                self.host_queue.append(player_name)
                log.info(f"Added '{player_name}' to host queue from slot {slot} (ordering pending).")

    def initialize_lobby_state(self):
        """Finalizes lobby state after !mp settings have been parsed (usually via timer)."""
        hr_enabled = self.runtime_config['host_rotation']['enabled']
        mc_enabled = self.runtime_config['map_checker']['enabled']
        tentative_host = self._tentative_host_from_settings
        log.info(f"Finalizing initial state. Players: {len(self.players_in_lobby)}. Tentative Host from Settings: {tentative_host}. Rotation: {hr_enabled}. Initial Queue: {list(self.host_queue)}")

        if hr_enabled and self.initial_slot_players:
             self.initial_slot_players.sort(key=lambda item: item[0])
             sorted_players = [p_name for slot, p_name in self.initial_slot_players]
             current_players_set = set(self.players_in_lobby)
             final_ordered_players = [p for p in sorted_players if p in current_players_set]
             new_queue = deque(p for p in final_ordered_players)
             self.host_queue = new_queue
             log.info(f"Host queue ordered by slot: {list(self.host_queue)}")

        if tentative_host and tentative_host in self.players_in_lobby:
            log.info(f"Host '{tentative_host}' identified via [Host] tag and present. Confirming internal state.")
            self.current_host = tentative_host
            if hr_enabled:
                if not self.host_queue or self.host_queue[0] != tentative_host:
                    log.info(f"Moving confirmed host '{tentative_host}' to queue front.")
                    try:
                        if tentative_host in self.host_queue: self.host_queue.remove(tentative_host)
                        self.host_queue.appendleft(tentative_host)
                    except Exception as e:
                        log.error(f"Error moving confirmed host '{tentative_host}' in queue: {e}")
            self.reset_host_timers_and_state(tentative_host)
            log.info(f"Confirmed current host is '{self.current_host}'. No !mp host command needed.")
        else:
            if not tentative_host: log.warning("No host identified via [Host] tag during !mp settings parse.")
            else: log.warning(f"Tentative host '{tentative_host}' from settings left before initialization. Finding new host.")

            if hr_enabled and self.host_queue:
                potential_host = self.host_queue[0]
                log.warning(f"Rotation is ON, no host confirmed/present. Proactively assigning host to queue front: '{potential_host}'.")
                self.send_message(f"!mp host {potential_host}")
                self.current_host = potential_host
                self.reset_host_timers_and_state(potential_host)
            else:
                 if not hr_enabled: log.info("Rotation is OFF. Bot will wait for Bancho host confirmation or other events.")
                 elif hr_enabled and not self.host_queue: log.info("Rotation is ON, but no players found in queue. Waiting for player join.")
                 self.current_host = None

        if hr_enabled:
            log.info(f"Final Initial Queue: {list(self.host_queue)}")
            self.display_host_queue()

        # Initial Map Check (if map exists and host confirmed/assigned)
        if mc_enabled and self.current_host and self.current_map_id != 0:
            log.info(f"Proceeding with initial map check for map {self.current_map_id} and current host {self.current_host}.")
            threading.Timer(1.0, self.check_map, args=[self.current_map_id, self.current_map_title]).start()
        elif mc_enabled:
            self.last_valid_map_id = 0 # Ensure no stale valid map
            self.host_map_selected_valid = False
            if not self.current_host: log.info("Initial map check skipped: No host confirmed/assigned yet.")
            elif self.current_map_id == 0: log.info("Initial map check skipped: No initial map found.")

        log.info(f"Initial lobby state setup complete. Final Current Host: {self.current_host}. Players: {list(self.players_in_lobby)}")
        self.log_feature_status()
        self._check_start_empty_room_timer()

    def request_initial_settings(self):
        """Requests !mp settings and schedules the finalization logic."""
        if self.bot_state != BOT_STATE_IN_ROOM:
            log.warning("Cannot request settings, not in a room.")
            return
        if self._initialization_pending:
            log.warning("Initialization already pending, ignoring duplicate request.")
            return
        if self.connection.is_connected():
            log.info("Requesting initial state with !mp settings")
            self.initial_slot_players.clear()
            self._tentative_host_from_settings = None
            self.send_message("!mp settings")
            init_delay = 5.0
            log.info(f"Scheduling initialization finalization in {init_delay} seconds...")
            self._initialization_pending = True
            if self._initialization_timer and self._initialization_timer.is_alive():
                self._initialization_timer.cancel()
            self._initialization_timer = threading.Timer(init_delay, self._finalize_initialization_scheduled)
            self._initialization_timer.start()
        else:
            log.warning("Cannot request !mp settings, disconnected.")

    def _finalize_initialization_scheduled(self):
        """Calls initialize_lobby_state after timer delay, checking flags."""
        if not self._initialization_pending:
            log.warning("_finalize_initialization_scheduled called but pending flag was not set or already cleared.")
            return
        self._initialization_pending = False
        self._initialization_timer = None
        if self.bot_state != BOT_STATE_IN_ROOM:
             log.warning("Timed initialization fired, but bot is no longer in IN_ROOM state. Aborting finalization.")
             return
        log.info("Timed initialization delay complete. Proceeding with final lobby state setup.")
        try:
            if hasattr(self, 'reactor') and self.reactor:
                 self.reactor.process_once(timeout=0.05)
                 log.debug("Processed any final pending events before finalizing.")
            self.initialize_lobby_state()
        except Exception as e:
             log.error(f"Error during timed initialization finalization: {e}", exc_info=True)

    # --- Host Rotation & Player Tracking Logic (In Room) ---
    def handle_player_join(self, player_name):
        """Handles Bancho's 'joined in slot' message."""
        if player_name.lower() == "banchobot":
            log.debug("Ignoring BanchoBot join event.")
            return
        player_name_clean = player_name.strip()
        if not player_name_clean: return
        if player_name_clean in self.players_in_lobby:
             log.debug(f"Player '{player_name_clean}' join message received, but already in lobby list.")
             if self.empty_room_close_timer_active:
                 log.info(f"Known player '{player_name_clean}' detected. Cancelling auto-close timer.")
                 self.empty_room_close_timer_active = False
                 self.empty_room_timestamp = 0
             return
        log.info(f"Player '{player_name_clean}' joined the lobby.")
        self.players_in_lobby.add(player_name_clean)
        self.PlayerJoined.emit({'player': player_name_clean})
        if self.empty_room_close_timer_active:
            log.info(f"Player '{player_name_clean}' joined while empty room timer active. Cancelling auto-close timer.")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0
        if self.runtime_config['host_rotation']['enabled']:
            if player_name_clean not in self.host_queue:
                self.host_queue.append(player_name_clean)
                log.info(f"Added '{player_name_clean}' to host queue.")
            if not self.current_host:
                log.info(f"No current host. Assigning '{player_name_clean}' as host.")
                self.send_message(f"!mp host {player_name_clean}")
                self.current_host = player_name_clean

    def handle_player_left(self, player_name):
        """Handles Bancho's 'left the game' or 'was kicked' message."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        player_name_clean = player_name.strip()
        if not player_name_clean: return
        log.info(f"Processing player left/kick: '{player_name_clean}'")
        was_in_lobby = player_name_clean in self.players_in_lobby
        was_host = (player_name_clean == self.current_host)
        was_last_host = (player_name_clean == self.last_host)
        if was_in_lobby:
            self.players_in_lobby.remove(player_name_clean)
            log.info(f"'{player_name_clean}' left/kicked. Lobby size: {len(self.players_in_lobby)}")
            self.PlayerLeft.emit({'player': player_name_clean})
        else: log.warning(f"'{player_name_clean}' left/kicked but was not in tracked player list?")
        hr_enabled = self.runtime_config['host_rotation']['enabled']
        queue_changed = False
        if hr_enabled:
            if player_name_clean in self.host_queue:
                try:
                    self.host_queue.remove(player_name_clean)
                    log.info(f"Removed '{player_name_clean}' from queue. New Queue: {list(self.host_queue)}")
                    queue_changed = True
                except ValueError: log.warning(f"'{player_name_clean}' was not found in queue for removal despite check?")
            else: log.warning(f"'{player_name_clean}' left but not found in queue?")
            if was_last_host:
                log.info(f"Player '{player_name_clean}' who was marked as last_host left. Clearing marker.")
                self.last_host = None
        if player_name_clean in self.map_violations:
            del self.map_violations[player_name_clean]
            log.debug(f"Removed violation count for leaving player '{player_name_clean}'.")
        self.clear_vote_skip_if_involved(player_name_clean, "player left/kicked")
        next_host_assigned = False
        if was_host:
            log.info(f"Host '{player_name_clean}' left.")
            self.current_host = None
            self.host_map_selected_valid = False
            self.host_last_action_time = 0
            if hr_enabled and not self.is_matching and self.host_queue:
                log.info("Host left outside match, attempting to set next host.")
                self.set_next_host()
                next_host_assigned = True
            elif not hr_enabled: log.info("Host left (rotation disabled). Host cleared.")
            elif hr_enabled and not self.host_queue: log.info("Host left, queue is now empty. No host to assign.")
        log.debug(f"Skipping queue display after player left (queue_changed={queue_changed}, next_host_assigned={next_host_assigned})")
        self._check_start_empty_room_timer()

    def _check_start_empty_room_timer(self):
        """Checks if the room is empty and starts the auto-close timer if applicable."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        ac_config = self.runtime_config['auto_close_empty_room']
        is_empty = len(self.players_in_lobby) == 0
        log.debug(f"Checking empty room timer: Enabled={ac_config['enabled']}, BotCreated={self.room_was_created_by_bot}, Empty={is_empty}, TimerActive={self.empty_room_close_timer_active}")
        if (ac_config['enabled'] and self.room_was_created_by_bot and is_empty and not self.empty_room_close_timer_active):
            delay = ac_config['delay_seconds']
            log.info(f"Room '{self.target_channel}' is now empty. Starting {delay}s auto-close timer.")
            self.empty_room_close_timer_active = True
            self.empty_room_timestamp = time.time()


    def handle_host_change(self, player_name):
        """Handles Bancho's 'became the host' message, considering rotation state."""
        if self.bot_state != BOT_STATE_IN_ROOM: return

        player_name_clean = player_name.strip()
        if not player_name_clean: return
        log.info(f"Bancho reported host changed to: '{player_name_clean}'")

        rotating_flag_was_true = self.is_rotating_host # Capture state at entry

        # If message confirms current tentative host, finalize it
        if player_name_clean == self.current_host:
             log.info(f"Host change message confirms '{player_name_clean}' is the current host. Finalizing state.")
             self.reset_host_timers_and_state(player_name_clean) # Resets last action time

             # <<< Check if this confirmation completes an expected rotation >>>
             if rotating_flag_was_true and player_name_clean == self._expected_next_host:
                 log.debug(f"Confirmed expected host '{player_name_clean}' during rotation. Resuming AFK checks.")
                 self.is_rotating_host = False
                 self._expected_next_host = None
                 self._rotation_start_time = 0
                 # Display queue because rotation completed
                 self.display_host_queue()
             elif rotating_flag_was_true:
                  log.warning(f"Host confirmed '{player_name_clean}', but expected host was '{self._expected_next_host}'. Still clearing rotation state.")
                  self.is_rotating_host = False
                  self._expected_next_host = None
                  self._rotation_start_time = 0
                  self.display_host_queue() # Display queue anyway as rotation sequence finished

             return # Exit early, no further sync needed

        # --- Host is genuinely changing to someone new (or confirming after assignment) ---
        previous_host = self.current_host
        self.current_host = player_name_clean # Update internal host state FIRST
        self.HostChanged.emit({'player': player_name_clean, 'previous': previous_host})

        # Ensure player is in lobby list
        if player_name_clean not in self.players_in_lobby:
             log.warning(f"New host '{player_name_clean}' wasn't in player list, adding.")
             self.handle_player_join(player_name_clean)

        # Reset timers, violations for the NEW confirmed host
        self.reset_host_timers_and_state(player_name_clean)

        # Clear vote skip targeting previous host
        if self.vote_skip_active and self.vote_skip_target == previous_host:
             log.info(f"Cancelling vote skip for {previous_host} (host changed).")
             self.send_message(f"Host changed to {player_name_clean}. Cancelling vote skip for {previous_host}.")
             self.clear_vote_skip("host changed")
        elif self.vote_skip_active and self.vote_skip_target == player_name_clean:
             log.info(f"Cancelling vote skip for {player_name_clean} (became host).")
             self.send_message(f"Host manually set to {player_name_clean}. Cancelling pending vote skip.")
             self.clear_vote_skip("host changed to target")

        # Synchronize host queue if rotation is enabled
        hr_enabled = self.runtime_config['host_rotation']['enabled']
        queue_changed_during_sync = False
        if hr_enabled:
            log.info(f"Synchronizing queue with new host '{player_name_clean}'. Current Queue: {list(self.host_queue)}")
            if player_name_clean not in self.host_queue:
                log.info(f"New host '{player_name_clean}' wasn't in queue, adding to front.")
                self.host_queue.appendleft(player_name_clean)
                queue_changed_during_sync = True
            elif self.host_queue and self.host_queue[0] != player_name_clean: # Check if queue not empty
                log.warning(f"Host changed to '{player_name_clean}', but they weren't front of queue. Moving to front.")
                try:
                    self.host_queue.remove(player_name_clean)
                    self.host_queue.appendleft(player_name_clean)
                    queue_changed_during_sync = True
                except ValueError: log.error(f"Failed to reorder queue for '{player_name_clean}' - value error.")
            elif not self.host_queue: # Queue was empty, add them
                log.info(f"Queue was empty, adding new host '{player_name_clean}' to front.")
                self.host_queue.appendleft(player_name_clean)
                queue_changed_during_sync = True
            else: log.info(f"New host '{player_name_clean}' is already front of queue.")

            # Display queue if it changed OR if rotation was flagged at the start
            # This ensures queue is shown after successful rotation confirmation
            if queue_changed_during_sync or rotating_flag_was_true:
                log.info(f"Queue updated/rotation confirmation. New Queue: {list(self.host_queue)}")
                self.display_host_queue()

        # <<< Check if this host change completes the expected rotation sequence >>>
        if rotating_flag_was_true and player_name_clean == self._expected_next_host:
            log.debug(f"Confirmed expected host '{player_name_clean}' after rotation completed. Resuming AFK checks.")
            self.is_rotating_host = False
            self._expected_next_host = None
            self._rotation_start_time = 0
        elif rotating_flag_was_true:
             # Host changed, but not to the one we specifically expected from rotation.
             # This could happen if another player used !mp host manually, or Bancho glitched.
             # Clear the rotation state anyway to prevent getting stuck.
             log.warning(f"Host changed to '{player_name_clean}', but expected host from rotation was '{self._expected_next_host}'. Clearing rotation state.")
             self.is_rotating_host = False
             self._expected_next_host = None
             self._rotation_start_time = 0

    def reset_host_timers_and_state(self, host_name):
        """Resets AFK timer base, violations for the given host."""
        if self.bot_state != BOT_STATE_IN_ROOM or not host_name: return
        if host_name != self.current_host:
            log.warning(f"Attempted to reset timers for '{host_name}', but current host is '{self.current_host}'. Ignoring stale reset.")
            return
        log.debug(f"Resetting timers/state for current host '{host_name}'.")
        current_time = time.time()
        self.host_last_action_time = current_time
        log.debug(f"*** host_last_action_time set to {current_time:.1f} in reset_host_timers_and_state for {host_name}")
        if self.runtime_config['map_checker']['enabled']:
            self.map_violations.setdefault(host_name, 0)
            if self.map_violations[host_name] != 0:
                 log.info(f"Resetting map violations for host '{host_name}'.")
                 self.map_violations[host_name] = 0
        # NOTE: DO NOT reset self.host_map_selected_valid here. That flag indicates
        # whether the *currently selected map* is valid. A host change doesn't
        # automatically validate the map they might have selected before the change.
        # handle_map_change and check_map control this flag.
    
    def rotate_and_set_host(self):
        """Rotates the queue (if needed) and attempts to set the new host via !mp host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return

        # <<< Store initial rotation state >>>
        was_rotating = self.is_rotating_host
        # Reset expectation at the start
        self._expected_next_host = None

        hr_enabled = self.runtime_config['host_rotation']['enabled']

        # --- Rotation Logic ---
        queue_rotated = False
        if hr_enabled and len(self.host_queue) > 1:
            log.info(f"Attempting host rotation. Last host marker: {self.last_host}. Queue Before: {list(self.host_queue)}")
            player_to_rotate = self.last_host
            if player_to_rotate and player_to_rotate in self.host_queue:
                try:
                    if self.host_queue[0] == player_to_rotate:
                        rotated_player = self.host_queue.popleft()
                        self.host_queue.append(rotated_player)
                        log.info(f"Rotated queue: Moved '{rotated_player}' (last host) from front to back.")
                        queue_rotated = True
                    else:
                        log.warning(f"Last host '{player_to_rotate}' was not at front of queue. Removing and appending to end.")
                        self.host_queue.remove(player_to_rotate)
                        self.host_queue.append(player_to_rotate)
                        queue_rotated = True
                except Exception as e: log.error(f"Error during queue rotation logic for '{player_to_rotate}': {e}", exc_info=True)
            elif not player_to_rotate: log.info("No specific last host marked for rotation. Front player will proceed.")
            else: log.info(f"Last host '{player_to_rotate}' is no longer in queue. No rotation needed for them.")
            log.info(f"Queue After Rotation Logic: {list(self.host_queue)}")
        elif hr_enabled and len(self.host_queue) == 1:
            log.info("Only one player in queue, no rotation needed.")
        elif hr_enabled: # Queue is empty
             log.warning("Rotation triggered with empty queue. Cannot set host.")
             self.current_host = None
             self.last_host = None
             # <<< Ensure flags cleared on early return >>>
             if was_rotating:
                  log.debug("Clearing rotation flags (empty queue).")
                  self.is_rotating_host = False
                  self._rotation_start_time = 0
             return # Exit early
        else: # Host rotation disabled
             log.debug("Host rotation is disabled. Resetting timers for current host (if any).")
             if self.current_host: self.reset_host_timers_and_state(self.current_host)
             self.last_host = None
             # <<< Ensure flags cleared on early return >>>
             if was_rotating:
                  log.debug("Clearing rotation flags (rotation disabled).")
                  self.is_rotating_host = False
                  self._rotation_start_time = 0
             return # Exit early

        # --- Set Next Host ---
        self.last_host = None # Clear marker *after* rotation logic
        next_host_in_queue = self.host_queue[0] if self.host_queue else None
        host_assigned = self.set_next_host() # Attempts to set host if needed

        # <<< Set expectation ONLY if host was actually assigned via !mp host >>>
        if host_assigned and next_host_in_queue:
            self._expected_next_host = next_host_in_queue
            log.info(f"Rotation expects '{self._expected_next_host}' to become host.")
            # Keep is_rotating_host True, set timer start
            if was_rotating: # Check the initial state
                 self._rotation_start_time = time.time()
                 log.debug(f"Rotation safety timer started ({self._rotation_start_time:.1f}). AFK check remains paused.")
            else:
                 log.warning("set_next_host assigned host, but was_rotating was False?")
                 self.is_rotating_host = True # Force on just in case
                 self._rotation_start_time = time.time()
        elif was_rotating and not host_assigned:
            # Host wasn't assigned (e.g., already host, or queue empty now)
            # but we *were* in a rotation sequence. Clear the flag now.
            log.debug("Rotation sequence finished, but no new host assigned via !mp host. Resuming AFK checks.")
            self.is_rotating_host = False
            self._rotation_start_time = 0
            # Display queue here? Maybe not needed if no change happened.
            # self.display_host_queue()

        # <<< REMOVED the final clearing logic from here. >>>
        # It's now handled above if host_assigned is False,
        # or by handle_host_change / safety timeout if host_assigned is True.
    def set_next_host(self):
        """Sets the player at the front of the queue as the host via !mp host. Returns True if assignment attempted."""
        if self.bot_state != BOT_STATE_IN_ROOM: return False
        if not self.runtime_config['host_rotation']['enabled']: return False
        if self.host_queue:
            next_host = self.host_queue[0]
            if next_host != self.current_host:
                log.info(f"Setting next host to '{next_host}' from queue front via !mp host...")
                self.send_message(f"!mp host {next_host}")
                self.current_host = next_host # Tentatively set
                return True
            else:
                log.info(f"'{next_host}' is already the current host. Resetting timers/state.")
                self.reset_host_timers_and_state(next_host)
                return False
        else:
            log.warning("Host queue is empty, cannot set next host.")
            if self.current_host:
                log.info("Clearing previous host as queue is empty.")
                self.current_host = None
                self.host_map_selected_valid = False
                self.host_last_action_time = 0
            return False
            
    def skip_current_host(self, reason="No reason specified"):
        """Skips the current host, rotates queue (if enabled), and sets the next host."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.current_host:
            log.warning("Attempted to skip host, but no host is currently assigned.")
            if self.runtime_config['host_rotation']['enabled'] and self.host_queue:
                 log.info("No current host to skip, but queue exists. Attempting to set next host.")
                 self.set_next_host()
            return
        skipped_host = self.current_host
        log.info(f"Skipping host '{skipped_host}'. Reason: {reason}. Queue Before: {list(self.host_queue)}")
        messages = [f"Host Skipped: {skipped_host}"]
        if reason and "vote" not in reason.lower() and "afk" not in reason.lower() and "violation" not in reason.lower():
             messages.append(f"Reason: {reason}")
        self.send_message(messages)
        self.clear_vote_skip(f"host '{skipped_host}' skipped")
        self.host_map_selected_valid = False
        self.current_host = None
        self.host_last_action_time = 0
        if self.runtime_config['host_rotation']['enabled']:
            self.last_host = skipped_host
            log.info(f"Marked '{skipped_host}' as last_host for rotation. Pausing AFK check.")
            self.is_rotating_host = True
            self.rotate_and_set_host()
        else:
            log.warning("Host skipped, but rotation is disabled. Cannot set next host automatically.")
            self.last_host = None

    def display_host_queue(self):
        """Sends the current host queue to the chat as a single message if rotation is enabled."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        if not self.runtime_config['host_rotation']['enabled']: return
        if not self.connection.is_connected():
             log.warning("Cannot display queue, not connected.")
             return
        if not self.host_queue:
            self.send_message("Host queue is empty.")
            return
        queue_list = list(self.host_queue)
        queue_entries = []
        current_host_name = self.current_host
        current_host_index = -1
        if current_host_name:
            try: current_host_index = queue_list.index(current_host_name)
            except ValueError: log.debug(f"Current host '{current_host_name}' not found in queue list for display.")
        for i, player in enumerate(queue_list):
            entry = f"{player}"
            is_current = (player == current_host_name)
            is_next = False
            if not is_current:
                if current_host_index != -1:
                    if i == (current_host_index + 1) % len(queue_list): is_next = True
                elif i == 0: is_next = True
            if is_current: entry += " (Current)"
            if is_next: entry += " (Next)"
            queue_entries.append(f"{entry}[{i+1}]")
        queue_str = " -> ".join(queue_entries)
        final_message = f"Host order: {queue_str}"
        self.send_message(final_message)

    # --- Match State Handling (In Room) ---
    def handle_match_start(self):
        # <<< MODIFIED: Use last_valid_map_id >>>
        if self.bot_state != BOT_STATE_IN_ROOM: return
        map_id_at_start = self.last_valid_map_id # Use the ID that was validated before match start
        log.info(f"Match started with map ID {map_id_at_start} ('{self.current_map_title}').")
        self.is_matching = True
        self.last_host = None # Clear last host marker at start
        self.clear_vote_skip("match started")
        self.host_map_selected_valid = False # Keep this False during match
        self.MatchStarted.emit({'map_id': map_id_at_start}) # Emit the validated map ID

    def handle_match_finish(self):
        # <<< MODIFIED: Added Played History Update >>>
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info("Match finished.")
        self.is_matching = False

        finished_map_id = self.last_valid_map_id # Map ID that was just completed
        played_list_config = self.runtime_config.get('maps_played_list', {})

        # Add finished map ID to history if feature enabled and ID is valid
        if played_list_config.get('enabled', False) and finished_map_id > 0:
            try:
                 map_id_to_add = int(finished_map_id)
                 # Avoid adding duplicates right after each other if somehow possible? Unlikely with queue logic.
                 # Check if deque is not empty before accessing last element
                 if not self.played_maps_history or self.played_maps_history[-1] != map_id_to_add:
                      self.played_maps_history.append(map_id_to_add)
                      log.info(f"Added map ID {map_id_to_add} to played history. History: {list(self.played_maps_history)}")
                 else:
                      log.debug(f"Map ID {map_id_to_add} is already the last item in history, not adding again.")
            except (ValueError, TypeError):
                 log.error(f"Could not add map ID '{finished_map_id}' to played history - invalid type.")
        elif finished_map_id <= 0:
            log.warning("Match finished, but last_valid_map_id was 0 or invalid. Cannot add to played history.")

        # Mark the player who was host when the match finished as 'last_host'
        if self.current_host:
             self.last_host = self.current_host
             log.info(f"Marking '{self.last_host}' as last host after match finish.")
        else:
             log.warning("Match finished but no current host was tracked? Rotation might be affected.")
             self.last_host = None

        self.MatchFinished.emit({'map_id': finished_map_id}) # Emit the ID of the map that finished
        self.current_map_id = 0 # Clear current map info after finish
        self.current_map_title = ""
        self.host_map_selected_valid = False # Reset flag
        # Don't clear last_valid_map_id here, it's useful for history

        # Trigger host rotation (if enabled) after a short delay
        if self.runtime_config['host_rotation']['enabled']:
            log.info("Scheduling host rotation (1.5s delay) after match finish. Pausing AFK check.")
            self.is_rotating_host = True # PAUSE AFK CHECK
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
        if self.current_host:
            log.info(f"Resetting timers for current host '{self.current_host}' after abort. Host rotation NOT triggered.")
            self.reset_host_timers_and_state(self.current_host)
        # Reset map state as the aborted map is no longer relevant/valid
        self.current_map_id = 0
        self.current_map_title = ""
        self.host_map_selected_valid = False
        # Do NOT add aborted map to played history
        # Keep last_valid_map_id, host might want to re-pick it or another valid one.

    def handle_match_close(self):
        """Handles 'Closed the match' message from BanchoBot."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info(f"BanchoBot reported: Closed the match ({self.target_channel})")
        if self.empty_room_close_timer_active:
            log.info("Match closed message received while auto-close timer was active (likely timer triggered).")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0

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
        # Use last_valid_map_id for check, as that's what would be played
        if self.last_valid_map_id == 0:
            map_ok_for_auto_start = False
            reason = "No valid map selected/validated yet"
        elif self.runtime_config['map_checker']['enabled'] and not self.host_map_selected_valid:
            # This check might be redundant if last_valid_map_id implies host_map_selected_valid was true, but keep for safety
            map_ok_for_auto_start = False
            reason = f"Current map {self.current_map_id} is not marked as valid"
            log.warning(f"Auto-start prevented: Map {self.current_map_id} validation flag is False (should not happen if last_valid_map_id > 0?).")
        elif not self.current_host:
             map_ok_for_auto_start = False
             reason = "No current host"

        if map_ok_for_auto_start:
            delay = max(1, as_config.get('delay_seconds', 5)) # Use configured delay
            log.info(f"Auto-starting match with map {self.last_valid_map_id} in {delay} seconds.")
            self.send_message(f"!mp start {delay}")
        else:
            log.info(f"Auto-start conditions not met: {reason}.")

    # --- Map Checking Logic (In Room) ---

    def handle_map_change(self, map_id, map_title):
        """Handles 'Beatmap changed to' message, considering reverts."""
        if self.bot_state != BOT_STATE_IN_ROOM: return
        log.info(f"Map changed to ID: {map_id}, Title: '{map_title}'")

        # Update internal state first
        self.current_map_id = map_id
        self.current_map_title = map_title
        self.host_map_selected_valid = False # Always reset this on change

        # Reset AFK timer base immediately on any map change attempt
        if self.current_host:
            self.host_last_action_time = time.time()
            log.debug(f"*** host_last_action_time set to {self.host_last_action_time:.1f} in handle_map_change for host {self.current_host}")

        # <<< Check if this change matches an ongoing revert >>>
        if self._reverting_to_map_id == map_id:
            log.info(f"Confirmed revert to map {map_id}. Resetting revert flag. AFK timer remains active, awaiting host action.")
            self.last_valid_map_id = map_id # Update last_valid since Bancho confirmed it
            self._reverting_to_map_id = 0  # Consume the flag
            # DO NOT call check_map or set host_map_selected_valid = True here.
            # The map is set, but the host still needs to act (e.g., !mp start).
        else:
            # This is a normal map change initiated by the host, or an unexpected change.
            log.debug(f"Processing normal map change to {map_id} (not a flagged revert).")
            self._reverting_to_map_id = 0 # Ensure flag is clear

            # Proceed with checking the map if checker is enabled
            if self.runtime_config['map_checker']['enabled']:
                 if self.current_host:
                     # Use a short delay for API call / rules check
                     threading.Timer(0.5, self.check_map, args=[map_id, map_title]).start()
                 else:
                     log.warning("Map changed, but cannot check rules: No current host identified.")
            else:
                # If checker is off, the map change itself is the valid action.
                # Timer was already reset above. Mark map as valid for starting.
                self.last_valid_map_id = map_id
                self.host_map_selected_valid = True # Treat as valid if checker off
                log.info(f"Map checker off. Marked map {map_id} as valid for starting.")
        # <<< END Revert Check >>>

    def check_map(self, map_id, map_title):
        """Fetches map info, checks rules (inc. history), and sends enhanced 'Map OK' message with labels."""
        # --- Initial checks ---
        # ... (same as before) ...
        if not self.runtime_config['map_checker']['enabled']:
             # ... (handle checker disabled) ...
             return
        if map_id != self.current_map_id: return
        current_host_for_check = self.current_host
        if not current_host_for_check: return

        log.info(f"Checking map {map_id} ('{map_title}') selected by {current_host_for_check}...")

        # --- Check Played History ---
        # ... (same as before) ...
        played_list_config = self.runtime_config.get('maps_played_list', {})
        if played_list_config.get('enabled', False) and map_id in self.played_maps_history:
            history_size = self.played_maps_history.maxlen if self.played_maps_history.maxlen is not None else 'infinite'
            reason = f"Map played recently (within last {history_size} maps)"
            log.warning(f"Map {map_id} rejected for host {current_host_for_check}: {reason}.")
            self.reject_map(reason, is_violation=True)
            return

        # --- Fetch Map Info ---
        # ... (same as before) ...
        info = get_beatmap_info(map_id, self.api_client_id, self.api_client_secret)
        if info is None:
            self.reject_map(f"Could not get info for map ID {map_id}.", is_violation=False)
            return

        # --- Extract Info ---
        # ... (same as before, extract stars, length, status, mode_api etc.) ...
        stars = info.get('stars')
        length = info.get('length')
        status = info.get('status', 'unknown').lower()
        mode_api = info.get('mode', 'unknown')
        # ... (extract other stats: cs_val, ar_val, etc.) ...
        cs_val = info.get('cs')
        ar_val = info.get('ar')
        od_val = info.get('od')
        hp_val = info.get('hp')
        bpm = info.get('bpm')
        count_circles = info.get('count_circles')
        count_sliders = info.get('count_sliders')
        count_spinners = info.get('count_spinners')
        beatmap_id = info.get('beatmap_id')
        beatmapset_id = info.get('beatmapset_id')
        full_title = info.get('title', 'N/A')
        version = info.get('version', 'N/A')

        stars_str = f"{stars:.2f}*" if stars is not None else "N/A"
        length_str = self._format_time(length) if length is not None else "N/A"
        map_display_name = f"{full_title} [{version}]" if full_title != 'N/A' and version != 'N/A' else map_title

        # --- Rule Checks (MODIFIED Violation Messages) ---
        violations = []
        mc = self.runtime_config['map_checker']
        allowed_statuses = [s.lower() for s in self.runtime_config.get('allowed_map_statuses', ['all'])]
        allowed_modes = [m.lower() for m in self.runtime_config.get('allowed_modes', ['all'])]

        # Status
        if 'all' not in allowed_statuses and status not in allowed_statuses:
            violations.append(f"Status '{status}' invalid (Allowed: {', '.join(allowed_statuses)})") # Added allowed list
        # Mode
        if 'all' not in allowed_modes and mode_api not in allowed_modes:
            violations.append(f"Mode '{mode_api}' invalid (Allowed: {', '.join(allowed_modes)})") # Added allowed list
        # Stars
        min_stars = mc.get('min_stars', 0); max_stars = mc.get('max_stars', 0)
        if stars is not None:
            epsilon = 0.001
            # <<< FIX: Include min/max values in violation string >>>
            if min_stars > 0 and stars < min_stars - epsilon:
                violations.append(f"Stars {stars_str} < Min ({min_stars:.2f}*)")
            if max_stars > 0 and stars > max_stars + epsilon:
                violations.append(f"Stars {stars_str} > Max ({max_stars:.2f}*)")
            # <<< END FIX >>>
        elif min_stars > 0 or max_stars > 0:
            violations.append("Star rating N/A")
        # Length
        min_len = mc.get('min_length_seconds', 0); max_len = mc.get('max_length_seconds', 0)
        if length is not None:
            # <<< FIX: Include min/max values in violation string >>>
            if min_len > 0 and length < min_len:
                 violations.append(f"Length {length_str} < Min ({self._format_time(min_len)})")
            if max_len > 0 and length > max_len:
                 violations.append(f"Length {length_str} > Max ({self._format_time(max_len)})")
            # <<< END FIX >>>
        elif min_len > 0 or max_len > 0:
            violations.append("Length N/A")
        # --- End Rule Checks ---

        # --- Process Result ---
        if current_host_for_check != self.current_host:
             # ... (handle host change during check) ...
             return

        if violations:
            # The reason now includes the specific values
            reason = f"Map Rejected: {'; '.join(violations)}" # Join the detailed violations
            log.warning(f"Map violation by {current_host_for_check} on map {map_id}: {reason}")
            self.reject_map(reason, is_violation=True)
        else:
            # --- Map Accepted ---
            # ... (same logic as before for setting flags, constructing links, constructing stats string) ...
            self.host_map_selected_valid = True
            self.last_valid_map_id = beatmap_id

            log.info(f"Map {beatmap_id} ('{map_display_name}') accepted for host {current_host_for_check}.")

            map_osu_link = self._format_map_link(beatmap_id, map_display_name)
            map_mirror_link = f" | Mirror: https://catboy.best/d/{beatmapset_id}n" if beatmapset_id else ""

            stats_list = []
            stats_list.append(f"Rating: {stars_str}")
            stats_list.append(f"Length: {length_str}")
            if bpm is not None: stats_list.append(f"BPM: {bpm:.1f}")

            # Mode-Specific Difficulty & Objects
            if mode_api == 'osu':
                if cs_val is not None: stats_list.append(f"CS: {cs_val:.1f}")
                # ... (rest of osu stats) ...
                if ar_val is not None: stats_list.append(f"AR: {ar_val:.1f}")
                if od_val is not None: stats_list.append(f"OD: {od_val:.1f}")
                if hp_val is not None: stats_list.append(f"HP: {hp_val:.1f}")
                if count_circles is not None: stats_list.append(f"Circles: {count_circles}")
                if count_sliders is not None: stats_list.append(f"Sliders: {count_sliders}")
                if count_spinners is not None: stats_list.append(f"Spinners: {count_spinners}")
            elif mode_api == 'taiko':
                if od_val is not None: stats_list.append(f"OD: {od_val:.1f}")
                # ... (rest of taiko stats) ...
                if hp_val is not None: stats_list.append(f"HP: {hp_val:.1f}")
                if count_circles is not None: stats_list.append(f"Hits: {count_circles}")
                if count_sliders is not None: stats_list.append(f"Rolls: {count_sliders}")
                if count_spinners is not None: stats_list.append(f"Spinners: {count_spinners}")
            elif mode_api == 'fruits':
                if cs_val is not None: stats_list.append(f"CS: {cs_val:.1f}")
                # ... (rest of fruits stats) ...
                if ar_val is not None: stats_list.append(f"AR: {ar_val:.1f}")
                if od_val is not None: stats_list.append(f"OD: {od_val:.1f}")
                if hp_val is not None: stats_list.append(f"HP: {hp_val:.1f}")
                if count_circles is not None: stats_list.append(f"Fruits: {count_circles}")
                if count_sliders is not None: stats_list.append(f"Droplets: {count_sliders}")
                if count_spinners is not None: stats_list.append(f"Bananas: {count_spinners}")
            elif mode_api == 'mania':
                if cs_val is not None: stats_list.append(f"Keys: {cs_val:.0f}K")
                # ... (rest of mania stats) ...
                if od_val is not None: stats_list.append(f"OD: {od_val:.1f}")
                if hp_val is not None: stats_list.append(f"HP: {hp_val:.1f}")
                if count_circles is not None: stats_list.append(f"Notes: {count_circles}")
                if count_sliders is not None: stats_list.append(f"LNs: {count_sliders}")

            stats_list.append(f"Status: {status}")
            stats_str = " | ".join(stats_list)
            final_stats_message = f"-> {stats_str}"

            self.send_message(f"Map OK: {map_osu_link}{map_mirror_link}")
            self.send_message(final_stats_message)

            self.reset_host_timers_and_state(current_host_for_check)
            log.debug(f"Host '{current_host_for_check}' proved activity.")
            if current_host_for_check in self.map_violations and self.map_violations[current_host_for_check] > 0:
                 log.info(f"Resetting map violation count for {current_host_for_check}.")
                 self.map_violations[current_host_for_check] = 0

    def _format_map_link(self, beatmap_id, display_name):
        """Formats a beatmap link for osu! chat."""
        if not beatmap_id or not display_name or display_name == "Unknown Title" or display_name == 'N/A':
            # Fallback if info is incomplete
            log.debug(f"Formatting map link: Falling back for ID {beatmap_id}, Name '{display_name}'")
            return f"Map ID {beatmap_id}" if beatmap_id else "Unknown Map"

        # Sanitize display name slightly for the link (osu! chat might be picky)
        # Replace brackets [] with parentheses () as osu! chat might parse [] incorrectly in links sometimes.
        sanitized_name = display_name.replace('[', '(').replace(']', ')')
        # Ensure no leading/trailing spaces remain
        sanitized_name = sanitized_name.strip()
        # Prevent excessively long names? (osu! might truncate anyway)
        max_len = 100 # Arbitrary limit
        if len(sanitized_name) > max_len:
             sanitized_name = sanitized_name[:max_len-3] + "..."

        log.debug(f"Formatting map link: ID={beatmap_id}, Sanitized Name='{sanitized_name}'")
        return f"[osu://b/{beatmap_id} {sanitized_name}]"

    def _format_time(self, seconds):
         # ... (existing _format_time code) ...
         if seconds is None or not isinstance(seconds, (int, float)) or seconds <= 0:
            return "N/A"
         return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
         
    def reject_map(self, reason, is_violation=True):
        """Handles map rejection, sends messages, increments violation count (if applicable), and attempts to revert map."""
        host_at_time_of_rejection = self.current_host
        if not host_at_time_of_rejection:
            log.warning(f"Map rejection triggered for '{reason}', but no host now. Aborting rejection.")
            return

        rejected_map_id = self.current_map_id
        rejected_map_title = self.current_map_title

        log.info(f"Rejecting map {rejected_map_id} ('{rejected_map_title}') for host {host_at_time_of_rejection}. Reason: {reason}")

        # Ensure flags are set correctly after rejection
        self.host_map_selected_valid = False
        self.current_map_id = 0
        self.current_map_title = ""
        self._reverting_to_map_id = 0 # Default: not reverting

        # Announce rejection
        self.send_message(f"Map Check Failed for {host_at_time_of_rejection}: {reason}")

        # Attempt to revert map after a short delay
        revert_messages = []
        # Check if there was a previously validated map to revert to
        if self.last_valid_map_id != 0 and self.last_valid_map_id != rejected_map_id:
             log.info(f"Attempting to revert map to last valid ID: {self.last_valid_map_id} for host {host_at_time_of_rejection}.")
             # <<< SET FLAG before sending command >>>
             self._reverting_to_map_id = self.last_valid_map_id
             revert_messages.append(f"!mp map {self.last_valid_map_id}")
        else:
             log.info(f"No suitable previous valid map (last={self.last_valid_map_id}, rejected={rejected_map_id}) to revert to. Aborting map.")
             self._reverting_to_map_id = 0 # Ensure flag is clear if aborting
             revert_messages.append("!mp abortmap")
             self.last_valid_map_id = 0 # Clear last valid since we couldn't revert

        if revert_messages:
             threading.Timer(0.5, self.send_message, args=[revert_messages]).start()

        # Handle Violations (only if map checker enabled and it's a rule violation)
        if not self.runtime_config['map_checker']['enabled'] or not is_violation:
             log.debug(f"Map rejection not violation or checker off. Violation Count: {self.map_violations.get(host_at_time_of_rejection, 0)}")
             return

        # --- Violation handling continues as before ---
        violation_limit = self.runtime_config['map_checker'].get('violations_allowed', 3)
        if violation_limit <= 0:
            log.debug("Violation limit is zero or negative, skipping violation tracking/host skip.")
            return
        # ... (rest of violation logic) ...
        count = self.map_violations.get(host_at_time_of_rejection, 0) + 1
        self.map_violations[host_at_time_of_rejection] = count
        log.warning(f"Map violation by {host_at_time_of_rejection} (Count: {count}/{violation_limit}). Map ID: {rejected_map_id}")
        if count >= violation_limit:
            # ... (skip logic) ...
            skip_message = f"Map violation limit ({violation_limit}) reached for {host_at_time_of_rejection}. Skipping host."
            log.warning(skip_message)
            self.send_message(skip_message)
            threading.Timer(0.5, self.skip_current_host, args=[f"Reached map violation limit ({violation_limit})"]).start()
        else:
            # ... (warn logic) ...
            remaining = violation_limit - count
            warn_message = f"Map Violation ({count}/{violation_limit}) for {host_at_time_of_rejection}. {remaining} remaining. Use !rules to check."
            self.send_message(warn_message)

    # --- Vote Skip Logic (In Room) ---
    def handle_vote_skip(self, voter):
        if self.bot_state != BOT_STATE_IN_ROOM: return
        vs_config = self.runtime_config['vote_skip']
        if not vs_config['enabled'] or not self.current_host: return
        target_host = self.current_host
        timeout = vs_config.get('timeout_seconds', 60)
        if self.vote_skip_active and time.time() - self.vote_skip_start_time > timeout:
                 log.info(f"Previous vote skip for {self.vote_skip_target} expired before new vote by {voter}.")
                 self.clear_vote_skip("timeout before new vote")
        if not self.vote_skip_active:
            self.vote_skip_active = True
            self.vote_skip_target = target_host
            self.vote_skip_initiator = voter
            self.vote_skip_voters = {voter}
            self.vote_skip_start_time = time.time()
            needed = self.get_votes_needed()
            log.info(f"Vote skip initiated by '{voter}' for host '{target_host}'. Needs {needed} votes ({len(self.vote_skip_voters)} already).")
            self.send_message(f"{voter} started vote skip for {target_host}! Type !voteskip to agree. ({len(self.vote_skip_voters)}/{needed})")
            if len(self.vote_skip_voters) >= needed:
                 log.info(f"Vote skip threshold ({needed}) met immediately for {target_host}. Skipping.")
                 self.send_message(f"Vote skip passed instantly! Skipping host {target_host}.")
                 threading.Timer(0.5, self.skip_current_host, args=[f"Skipped by player vote ({len(self.vote_skip_voters)}/{needed} votes)"]).start()
        elif self.vote_skip_active and self.vote_skip_target == target_host:
            if voter in self.vote_skip_voters:
                log.debug(f"'{voter}' tried to vote skip again for {target_host}.")
                return
            self.vote_skip_voters.add(voter)
            needed = self.get_votes_needed()
            current_votes = len(self.vote_skip_voters)
            log.info(f"'{voter}' voted to skip '{target_host}'. Votes: {current_votes}/{needed}")
            if current_votes >= needed:
                log.info(f"Vote skip threshold reached for {target_host}. Skipping.")
                self.send_message(f"Vote skip passed! Skipping host {target_host}.")
                threading.Timer(0.5, self.skip_current_host, args=[f"Skipped by player vote ({current_votes}/{needed} votes)"]).start()
            else:
                 self.send_message(f"{voter} voted skip. ({current_votes}/{needed})")
        elif self.vote_skip_active and self.vote_skip_target != target_host:
             log.warning(f"'{voter}' tried !voteskip for current host '{target_host}', but active vote is for '{self.vote_skip_target}'. Ignoring vote.")
             self.send_message(f"Cannot vote for {target_host} now, a vote for {self.vote_skip_target} is already active.")

    def get_votes_needed(self):
        """Calculates the number of votes required to skip the host based on runtime_config."""
        if self.bot_state != BOT_STATE_IN_ROOM: return 999
        vs_config = self.runtime_config['vote_skip']
        threshold_type = vs_config.get('threshold_type', 'percentage')
        threshold_value = vs_config.get('threshold_value', 51)
        eligible_voters = max(0, len(self.players_in_lobby) - 1)
        if self.current_host not in self.players_in_lobby: eligible_voters = max(0, len(self.players_in_lobby))
        if eligible_voters < 1: return 1
        try:
            if threshold_type == 'percentage':
                needed = math.ceil(eligible_voters * (float(threshold_value) / 100.0))
                return max(1, int(needed))
            elif threshold_type == 'fixed':
                needed = int(threshold_value)
                return max(1, min(needed, eligible_voters))
            else:
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
        player_name_clean = player_name.strip()
        if player_name_clean == self.vote_skip_target or player_name_clean == self.vote_skip_initiator:
             log.info(f"Cancelling vote skip for '{self.vote_skip_target}' because {reason}: {player_name_clean}")
             self.send_message(f"Vote skip cancelled ({reason}: {player_name_clean}).")
             self.clear_vote_skip(reason)
        elif player_name_clean in self.vote_skip_voters:
             self.vote_skip_voters.remove(player_name_clean)
             log.info(f"Removed leaving player '{player_name_clean}' from vote skip voters. Remaining voters: {len(self.vote_skip_voters)}")
             needed = self.get_votes_needed()
             current_votes = len(self.vote_skip_voters)
             if current_votes >= needed:
                  log.info(f"Vote skip threshold reached for {self.vote_skip_target} after voter '{player_name_clean}' left. Skipping.")
                  self.send_message(f"Vote skip passed after voter left! Skipping host {self.vote_skip_target}.")
                  threading.Timer(0.5, self.skip_current_host, args=[f"Skipped by player vote ({current_votes}/{needed} votes after voter left)"]).start()

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
        if self.is_rotating_host: log.debug("AFK check: Paused during host rotation."); return
        if not afk_config['enabled']: return
        if not self.current_host: return
        if self.is_matching: return
        # If host has selected a VALID map, they are NOT AFK.
        if self.host_map_selected_valid:
             log.debug(f"AFK check for {self.current_host}: PAUSED (host_map_selected_valid is True). Map: {self.current_map_id}")
             return
        timeout = afk_config.get('timeout_seconds', 120)
        if timeout <= 0: return
        last_action_time = self.host_last_action_time
        if last_action_time == 0:
             log.debug(f"AFK check for {self.current_host}: Last action time is 0, likely just became host. Skipping check.")
             self.host_last_action_time = current_time_check
             return
        time_since_last_action = current_time_check - last_action_time
        log.debug(f"AFK check for {self.current_host}: ValidMap={self.host_map_selected_valid}, "
                  f"IdleTime={time_since_last_action:.1f}s (Current: {current_time_check:.1f}, LastAction: {last_action_time:.1f}), "
                  f"Timeout={timeout}s")
        if time_since_last_action > timeout:
            log.warning(f"AFK TIMEOUT MET for host '{self.current_host}'. "
                        f"Idle: {time_since_last_action:.1f}s > {timeout}s. Skipping.")
            self.send_message(f"Host {self.current_host} skipped due to inactivity ({timeout}s+). Please pick a map promptly!")
            threading.Timer(0.5, self.skip_current_host, args=[f"AFK timeout ({timeout}s)"]).start()

    # --- Auto Close Empty Room Check ---
    def check_empty_room_close(self):
        """Checks if an empty, bot-created room's timeout has expired and closes it."""
        if not self.empty_room_close_timer_active: return
        if self.bot_state != BOT_STATE_IN_ROOM:
             log.warning("check_empty_room_close called but not in room state. Cancelling timer.")
             self.empty_room_close_timer_active = False
             self.empty_room_timestamp = 0
             return
        if len(self.players_in_lobby) > 0:
            log.info("check_empty_room_close: Player detected in lobby, cancelling auto-close timer.")
            self.empty_room_close_timer_active = False
            self.empty_room_timestamp = 0
            return
        ac_config = self.runtime_config['auto_close_empty_room']
        delay = ac_config['delay_seconds']
        elapsed_time = time.time() - self.empty_room_timestamp
        log.debug(f"Checking empty room close timer: Elapsed {elapsed_time:.1f}s / {delay}s")
        if elapsed_time >= delay:
            log.warning(f"Empty room '{self.target_channel}' timeout ({delay}s) reached. Sending '!mp close'.")
            try:
                self.empty_room_close_timer_active = False
                self.empty_room_timestamp = 0
                self.send_message("!mp close")
            except Exception as e:
                log.error(f"Failed to send '!mp close' command: {e}", exc_info=True)

    # --- Utility Methods ---
    def send_message(self, message_or_list):
        """Sends a message or list of messages to the current target_channel if in a room."""
        if self.bot_state != BOT_STATE_IN_ROOM or not self.target_channel:
            log.warning(f"Cannot send to channel (state={self.bot_state}, channel={self.target_channel}): {message_or_list}")
            return
        self._send_irc_message(self.target_channel, message_or_list)

    def send_private_message(self, recipient, message_or_list):
        """Sends a private message or list of messages to a specific user."""
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
        delay = 0.65 if target.startswith("#mp_") else 0.35
        for i, msg in enumerate(messages):
            if not msg: continue
            full_msg = str(msg)
            if not full_msg.startswith("!") and (full_msg.startswith("/") or full_msg.startswith(".")):
                 log.warning(f"Message starts with potentially unsafe char, prepending space: {full_msg[:30]}...")
                 full_msg = " " + full_msg
            my_nick = self.connection.get_nickname()
            if my_nick and my_nick in full_msg: log.debug(f"Message contains own nick '{my_nick}'. Sending as is.")
            max_len_bytes = 450
            try: encoded_msg = full_msg.encode('utf-8', 'ignore')
            except Exception as enc_e:
                 log.error(f"Error encoding message before sending: {enc_e}. Original: {full_msg[:50]}")
                 continue
            if len(encoded_msg) > max_len_bytes:
                log.warning(f"Truncating long message (>{max_len_bytes} bytes): {encoded_msg[:100]}...")
                truncated_encoded = encoded_msg[:max_len_bytes]
                try: full_msg = truncated_encoded.decode('utf-8', 'ignore') + "..."
                except Exception: full_msg = full_msg[:max_len_bytes//4] + "..."
            try:
                if i > 0: time.sleep(delay)
                log.info(f"SEND -> {target}: {full_msg}")
                self.connection.privmsg(target, full_msg)
                if target.startswith("#mp_") and target == self.target_channel:
                    self.SentMessage.emit({'message': full_msg})
            except irc.client.ServerNotConnectedError:
                log.warning("Failed to send message: Disconnected.")
                self._request_shutdown("Disconnected during send")
                break
            except Exception as e:
                log.error(f"Failed to send message to {target}: {e}", exc_info=True)
                time.sleep(1)

    def _request_shutdown(self, reason=""):
        """Internal signal to start shutdown sequence."""
        global shutdown_requested
        if self.bot_state != BOT_STATE_SHUTTING_DOWN:
            log.info(f"Shutdown requested internally. Reason: {reason if reason else 'N/A'}")
            self.bot_state = BOT_STATE_SHUTTING_DOWN
            shutdown_requested = True
            self._cancel_pending_initialization()
        else: log.warning("Shutdown already in progress.")

    # --- Admin Commands (Called from Console Input Thread) ---
    def admin_skip_host(self, reason="Admin command"):
        if self.bot_state != BOT_STATE_IN_ROOM: print("Command failed: Bot is not currently in a room."); return
        if not self.connection.is_connected(): print("Command failed: Not connected to IRC."); return
        if not self.current_host: print("Command failed: No host is currently assigned to skip."); return
        log.info(f"Admin skip host initiated. Reason: {reason}")
        print(f"Admin: Skipping current host ({self.current_host}). Reason: {reason}")
        self.skip_current_host(reason)

    def admin_show_queue(self):
        if self.bot_state != BOT_STATE_IN_ROOM: print("Cannot show queue: Bot not in a room."); return
        if not self.runtime_config['host_rotation']['enabled']: print("Host rotation is disabled."); return
        print("--- Host Queue (Console View) ---")
        if not self.host_queue: print("(Empty)")
        else:
            q_list = list(self.host_queue)
            current_host_name = self.current_host
            current_host_index = -1
            if current_host_name:
                 try: current_host_index = q_list.index(current_host_name)
                 except ValueError: pass
            for i, p in enumerate(q_list):
                status = ""
                is_current = (p == current_host_name)
                is_next = False
                if not is_current:
                    if current_host_index != -1:
                         if i == (current_host_index + 1) % len(q_list): is_next = True
                    elif i == 0: is_next = True
                if is_current: status = "(Current Host)"
                if is_next: status = "(Next Host)"
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
            afk_time_str = "N/A"
            if self.current_host and self.host_last_action_time > 0:
                 idle_sec = time.time() - self.host_last_action_time
                 afk_time_str = f"{idle_sec:.1f}s"
            print(f" Host Idle Time: {afk_time_str} (Since: {time.strftime('%H:%M:%S', time.localtime(self.host_last_action_time)) if self.host_last_action_time > 0 else 'N/A'})")
            print(f" Empty Close Timer: {'ACTIVE (' + str(int(time.time() - self.empty_room_timestamp)) + 's elapsed)' if self.empty_room_close_timer_active else 'Inactive'}")

            print("-" * 10 + " Features (Runtime) " + "-" * 10)
            print(f" Rotation: {self.runtime_config['host_rotation']['enabled']}")
            if self.runtime_config['host_rotation']['enabled']: self.admin_show_queue()
            print(f" Map Check: {self.runtime_config['map_checker']['enabled']}")
            print(f" Vote Skip: {self.runtime_config['vote_skip']['enabled']}")
            print(f" AFK Check: {self.runtime_config['afk_handling']['enabled']} (Timeout: {self.runtime_config['afk_handling']['timeout_seconds']}s)")
            print(f" Auto Start: {self.runtime_config['auto_start']['enabled']} (Delay: {self.runtime_config['auto_start']['delay_seconds']}s)")
            print(f" Auto Close: {self.runtime_config['auto_close_empty_room']['enabled']} (Delay: {self.runtime_config['auto_close_empty_room']['delay_seconds']}s)")
            # <<< Add Maps Played List status >>>
            mpl_config = self.runtime_config['maps_played_list']
            print(f" Maps Played History: {mpl_config['enabled']} (Size: {mpl_config['max_size']})")
            # <<< END >>>

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
            else: print(" Vote Active: No")

            print("-" * 10 + " Map Info " + "-" * 10)
            print(f" Current Map ID: {self.current_map_id if self.current_map_id else 'None'} ('{self.current_map_title}')")
            print(f" Host Map Valid Flag: {self.host_map_selected_valid} (Pauses AFK timer)")
            print(f" Last Valid Map ID: {self.last_valid_map_id if self.last_valid_map_id else 'None'} (Used for revert/play)")
            # <<< Add played maps history display >>>
            if mpl_config['enabled']:
                 history_list = list(self.played_maps_history)
                 print(f" Played Maps History ({len(history_list)}/{mpl_config['max_size']}): {history_list}")
            # <<< END >>>
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
                violations_str = ", ".join([f"{p}: {v}" for p, v in self.map_violations.items() if v > 0]) if any(v > 0 for v in self.map_violations.values()) else "{}"
                print(f"   Current Violations: {violations_str}")
            else: print("  Map Rules: (Map Check Disabled)")
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
                self._send_irc_message(self.target_channel, goodbye_msg)
                time.sleep(0.5)
            except Exception as e: log.error(f"Error sending goodbye message: {e}")
        log.info(f"Sending QUIT command ('{message}')...")
        try:
            if conn_available: self.connection.quit(message)
            else: log.warning("Cannot send QUIT, connection not available.")
        except irc.client.ServerNotConnectedError: log.warning("Cannot send QUIT, already disconnected.")
        except Exception as e: log.error(f"Unexpected error during connection.quit: {e}")
        finally:
            self.connection_registered = False
            if conn_available:
                try: self.connection.disconnect("Client shutdown")
                except Exception as e: log.debug(f"Error during explicit disconnect: {e}")


# --- Configuration Loading/Generation ---
def load_or_generate_config(filepath):
    """Loads config from JSON file or generates a default one if not found.
       Prioritizes values from the existing file over defaults using recursive merge."""
    defaults = {
        "server": "irc.ppy.sh",
        "port": 6667,
        "username": "YourOsuUsername",
        "password": "YourOsuIRCPassword",
        "welcome_message": "osu-ahr-py connected! Use !help for commands.",
        "goodbye_message": "Bot disconnecting.",
        "osu_api_client_id": 0,
        "osu_api_client_secret": "YOUR_CLIENT_SECRET",
        "map_checker": {
            "enabled": True,
            "min_stars": 0.0,
            "max_stars": 10.0,
            "min_length_seconds": 0,
            "max_length_seconds": 600,
            "violations_allowed": 3
        },
        "allowed_map_statuses": ["ranked", "approved", "qualified", "loved"],
        "allowed_modes": ["mania"],
        "host_rotation": {
            "enabled": True
        },
        "vote_skip": {
            "enabled": True,
            "timeout_seconds": 60,
            "threshold_type": "percentage",
            "threshold_value": 51
        },
        "afk_handling": {
            "enabled": True,
            "timeout_seconds": 120
        },
        "auto_start": {
            "enabled": True,
            "delay_seconds": 2
        },
        "auto_close_empty_room": {
            "enabled": True,
            "delay_seconds": 60
        },
        # <<< NEW: Maps Played List Config Defaults >>>
        "maps_played_list": {
            "enabled": True,      # Enable the feature by default
            "max_size": 5         # Remember the last 5 maps played by default
        }
        # <<< END >>>
    }

    def merge_configs(base, updates):
        # (Recursive merge logic remains the same)
        merged = base.copy()
        for key, value in updates.items():
            if key in merged:
                if isinstance(value, dict) and isinstance(merged[key], dict):
                    merged[key] = merge_configs(merged[key], value)
                elif not isinstance(value, dict) and value is not None:
                     is_sensitive_default = (key == "password" and value == "YourOsuIRCPassword") or \
                                            (key == "osu_api_client_secret" and value == "YOUR_CLIENT_SECRET") or \
                                            (key == "osu_api_client_id" and value == 0) or \
                                            (key == "username" and value == "YourOsuUsername")
                     if not is_sensitive_default: merged[key] = value
        return merged

    try:
        if not filepath.exists():
            log.warning(f"Config file '{filepath}' not found. Generating default config.")
            log.warning(">>> IMPORTANT: Edit 'config.json' with your osu! username, IRC password, and API credentials before running again! <<<")
            config_to_write = defaults.copy()
            try:
                with filepath.open('w', encoding='utf-8') as f:
                    json.dump(config_to_write, f, indent=4, ensure_ascii=False)
                log.info(f"Default config file created at '{filepath}'. Please edit it and restart.")
                return defaults
            except (IOError, PermissionError) as e:
                 log.critical(f"FATAL: Could not write default config file '{filepath}': {e}")
                 sys.exit(1)
        else:
            log.info(f"Loading configuration from '{filepath}'...")
            with filepath.open('r', encoding='utf-8') as f:
                user_config = json.load(f)

            final_config = merge_configs(defaults, user_config)

            # --- Validation & Type Coercion ---
            critical_keys = ["username", "password"]
            missing_critical = []
            for k in critical_keys:
                 val = final_config.get(k)
                 is_missing = val is None
                 is_default = (k == "password" and val == "YourOsuIRCPassword") or \
                              (k == "username" and val == "YourOsuUsername")
                 if is_missing or is_default: missing_critical.append(k)
            if missing_critical:
                 log.critical(f"FATAL: Missing or default required config keys: {', '.join(missing_critical)}. Edit {filepath}.")
                 sys.exit(1)

            if final_config['map_checker']['enabled']:
                 api_id = final_config.get("osu_api_client_id")
                 api_secret = final_config.get("osu_api_client_secret")
                 if not api_id or api_id == 0 or not api_secret or api_secret == "YOUR_CLIENT_SECRET":
                     log.warning(f"Map checker enabled, but API credentials missing/default. Disabling map check.")
                     final_config['map_checker']['enabled'] = False

            # Ensure correct types for numeric/bool fields in nested dicts
            for section_key, default_section in defaults.items():
                 if isinstance(default_section, dict):
                     user_section = final_config.get(section_key, {})
                     if not isinstance(user_section, dict):
                         log.warning(f"Config section '{section_key}' is not a dictionary. Resetting to default.")
                         final_config[section_key] = default_section.copy()
                         user_section = final_config[section_key]
                     for key, default_value in default_section.items():
                         user_value = user_section.get(key)
                         target_type = type(default_value)
                         if user_value is None: user_section[key] = default_value; continue
                         if not isinstance(user_value, target_type):
                             try:
                                 coerced_value = target_type(user_value)
                                 user_section[key] = coerced_value
                             except (ValueError, TypeError):
                                 log.warning(f"Invalid type for '{section_key}.{key}'. Found '{type(user_value).__name__}', expected '{target_type.__name__}'. Using default: {default_value}")
                                 user_section[key] = default_value

            # List type validation
            for key in ["allowed_map_statuses", "allowed_modes"]:
                 if not isinstance(final_config.get(key), list):
                     log.warning(f"Config key '{key}' is not a list. Resetting to default: {defaults[key]}")
                     final_config[key] = defaults[key][:]

            # Specific value constraints
            if final_config['auto_close_empty_room']['delay_seconds'] < 5: final_config['auto_close_empty_room']['delay_seconds'] = 5
            if final_config['auto_start']['delay_seconds'] < 1: final_config['auto_start']['delay_seconds'] = 1
            if final_config['afk_handling']['timeout_seconds'] <= 0 and final_config['afk_handling']['enabled']: final_config['afk_handling']['timeout_seconds'] = 120
            # <<< Validate maps_played_list size >>>
            if final_config['maps_played_list']['max_size'] < 0:
                 log.warning("maps_played_list max_size cannot be negative. Setting to 0 (effectively disabled unless enabled flag used).")
                 final_config['maps_played_list']['max_size'] = 0
            # <<< END >>>

            final_config['allowed_map_statuses'] = [str(s).lower() for s in final_config['allowed_map_statuses']]
            final_config['allowed_modes'] = [str(m).lower() for m in final_config['allowed_modes']]

            log.info(f"Configuration loaded and validated successfully from '{filepath}'.")
            return final_config

    except (json.JSONDecodeError, TypeError) as e:
        log.critical(f"FATAL: Error parsing config file '{filepath}': {e}. Check JSON format.")
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
    else: log.warning("Shutdown already in progress.")

# --- Console Input Loop ---
def console_input_loop(bot_instance):
    """Handles admin commands entered in the console, adapting based on bot state."""
    global shutdown_requested
    time.sleep(1.0) # Wait brief moment for bot to potentially connect/fail

    if shutdown_requested:
        log.info("Console input thread exiting early (shutdown requested during init/connection).")
        return

    log.info("Console input thread active. Type 'help' for available commands.")

    while not shutdown_requested:
        try:
            # Determine prompt based on state
            current_state = bot_instance.bot_state # Cache state
            prompt_prefix = "ADMIN"
            state_indicator = ""

            if current_state == BOT_STATE_CONNECTED_WAITING: state_indicator = "make/enter/quit"
            elif current_state == BOT_STATE_IN_ROOM: state_indicator = f"[{bot_instance.target_channel}]"
            elif current_state == BOT_STATE_JOINING: state_indicator = "(joining...)"
            elif current_state == BOT_STATE_INITIALIZING: state_indicator = "(initializing...)"
            elif current_state == BOT_STATE_SHUTTING_DOWN:
                 log.info("Console input loop stopping (shutdown in progress).")
                 break
            else: state_indicator = "(?)"

            # Construct prompt
            prompt = f"{state_indicator} > " if state_indicator == "make/enter/quit" else f"{prompt_prefix} {state_indicator} > "

            # Handle initialization state separately
            if current_state == BOT_STATE_INITIALIZING:
                 time.sleep(0.5)
                 continue

            # Get input
            try:
                command_line = input(prompt).strip()
                time.sleep(0.05) # Give other threads a tiny window to update state
            except EOFError:
                log.info("Console input closed (EOF). Requesting shutdown.")
                bot_instance._request_shutdown("Console EOF")
                break
            except KeyboardInterrupt:
                if not shutdown_requested:
                    log.info("Console KeyboardInterrupt. Requesting shutdown.")
                    bot_instance._request_shutdown("Console Ctrl+C")
                break

            if not command_line: continue

            # Re-check state *after* sleep, before parsing command
            current_state = bot_instance.bot_state
            if current_state == BOT_STATE_SHUTTING_DOWN: break # Exit if shutdown occurred during sleep

            # Parse command
            try:
                parts = shlex.split(command_line)
            except ValueError:
                 print(f"Warning: Could not parse command with shlex (check quotes?): {command_line}")
                 parts = command_line.split() # Fallback

            if not parts: continue
            command = parts[0].lower()
            args = parts[1:]

            # --- State-Specific Command Handling ---

            # Always available commands (check state again just in case)
            if command in ["quit", "exit"]:
                log.info(f"Console requested quit (State: {current_state}).")
                bot_instance._request_shutdown("Console quit command")
                break
            elif command in ["status", "info"]:
                 bot_instance.admin_show_status()
                 continue

            # --- WAITING State Commands ---
            if current_state == BOT_STATE_CONNECTED_WAITING:
                 if command == "enter":
                    if len(args) != 1 or not args[0].isdigit():
                        print("Usage: enter <room_id_number>")
                        continue
                    room_id = args[0]
                    print(f"Attempting to enter room mp_{room_id}...")
                    log.info(f"Console: Requesting join for room mp_{room_id}")
                    bot_instance.join_room(room_id)
                 elif command == "make":
                    if not args:
                        print("Usage: make <\"Room Name\"> [password]")
                        continue
                    room_name = args[0] # shlex handles quotes
                    password = args[1] if len(args) > 1 else None
                    print(f"Attempting to make room '{room_name}'...")
                    log.info(f"Console: Requesting room creation: Name='{room_name}', Password={'Yes' if password else 'No'}")
                    bot_instance.pending_room_password = password
                    bot_instance.waiting_for_make_response = True
                    bot_instance.send_private_message("BanchoBot", f"!mp make {room_name}")
                    print("Room creation command sent. Waiting for BanchoBot PM...")
                 elif command == "help":
                     print("\n--- Available Commands (Waiting State) ---")
                     print("  enter <room_id>      - Join an existing multiplayer room by ID.")
                     print("  make <\"name\"> [pass] - Create a new room (use quotes if name has spaces).")
                     print("  status / info        - Show current bot status.")
                     print("  quit / exit          - Disconnect and exit the application.")
                     print("-------------------------------------------\n")
                 else:
                    print(f"Unknown command '{command}' in waiting state. Use 'enter', 'make', 'status', or 'quit'. Type 'help'.")

            # --- IN_ROOM State Commands ---
            elif current_state == BOT_STATE_IN_ROOM:
                 # --- Add a final check just to be absolutely sure ---
                 if bot_instance.bot_state != BOT_STATE_IN_ROOM:
                     log.warning(f"Console command '{command}' entered, but state changed from IN_ROOM just before execution. Ignoring.")
                     print("State changed just before command execution, please try again.")
                     continue
                 # -----------------------------------------------------

                 config_changed = False
                 setting_name_for_announce = None
                 value_for_announce = None

                 # --- Room Control & Info ---
                 if command == "stop":
                    log.info("Console requested stop (leave room).")
                    print("Leaving room and returning to make/enter state...")
                    bot_instance.leave_room()
                 elif command == "close_room":
                    log.info(f"Admin requested closing room {bot_instance.target_channel} via !mp close.")
                    print(f"Sending '!mp close' to {bot_instance.target_channel}...")
                    if bot_instance.empty_room_close_timer_active:
                         log.info("Admin closing room, cancelling active empty room timer.")
                         bot_instance.empty_room_close_timer_active = False
                         bot_instance.empty_room_timestamp = 0
                    bot_instance.send_message("!mp close")
                 elif command == "skip":
                    reason = " ".join(args) if args else "Admin command"
                    bot_instance.admin_skip_host(reason)
                 elif command in ["queue", "q", "showqueue"]:
                    bot_instance.admin_show_queue()

                 # --- Lobby Settings (!mp) - Not Saved ---
                 elif command == "say":
                     if not args: print("Usage: say <message to send>"); continue
                     msg_to_send = " ".join(args)
                     print(f"Admin forcing send: {msg_to_send}")
                     bot_instance.send_message(msg_to_send)
                 elif command == "set_password":
                     if not args: print("Usage: set_password <new_password|clear>"); continue
                     pw = args[0]
                     msg_to_send = f"!mp password {pw}" if pw.lower() != 'clear' else "!mp password"
                     print(f"Admin: Sending {msg_to_send}")
                     bot_instance.send_message(msg_to_send)
                 elif command == "set_size": # <<< Check indentation here carefully
                     if not args or not args[0].isdigit(): print("Usage: set_size <number 1-16>"); continue
                     try: # <<< START TRY
                         size = int(args[0])
                         if 1 <= size <= MAX_LOBBY_SIZE:
                             print(f"Admin: Sending !mp size {size}")
                             bot_instance.send_message(f"!mp size {size}")
                         else:
                             print(f"Invalid size. Must be between 1 and {MAX_LOBBY_SIZE}.")
                     except ValueError: # <<< CORRESPONDING EXCEPT
                         print("Invalid number for size.")
                 elif command == "set_name":
                     if not args: print("Usage: set_name <\"new lobby name\">"); continue
                     name = " ".join(args)
                     print(f"Admin: Sending !mp name {name}")
                     bot_instance.send_message(f"!mp name {name}")

                 # --- Bot Feature Toggles (Saved) ---
                 elif command == "set_rotation":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_rotation <true|false>"); continue
                    hr_config = bot_instance.runtime_config['host_rotation']; value = args[0].lower() in ['true', 'on']
                    if hr_config['enabled'] != value:
                        hr_config['enabled'] = value; print(f"Admin set Host Rotation: {value}"); setting_name_for_announce = "Host Rotation"; value_for_announce = value; config_changed = True
                        if value and not bot_instance.host_queue and bot_instance.connection.is_connected(): log.info("Rotation enabled, requesting settings."); bot_instance.request_initial_settings()
                        elif not value: bot_instance.host_queue.clear(); log.info("Rotation disabled. Queue cleared."); bot_instance.send_message("Host rotation disabled.")
                        else: bot_instance.display_host_queue()
                    else: print(f"Host Rotation already {value}.")
                 elif command == "set_map_check":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_map_check <true|false>"); continue
                    mc_config = bot_instance.runtime_config['map_checker']; value = args[0].lower() in ['true', 'on']
                    if mc_config['enabled'] != value:
                        if value and (not bot_instance.api_client_id or bot_instance.api_client_secret == 'YOUR_CLIENT_SECRET'): print("ERROR: Cannot enable map check: API keys missing/default."); continue
                        mc_config['enabled'] = value; print(f"Admin set Map Checker: {value}"); setting_name_for_announce = "Map Checker"; value_for_announce = value; config_changed = True
                        if value and bot_instance.current_map_id != 0 and bot_instance.current_host and bot_instance.connection.is_connected(): log.info("Map check enabled, re-validating map."); bot_instance.host_map_selected_valid = False; threading.Timer(0.5, bot_instance.check_map, args=[bot_instance.current_map_id, bot_instance.current_map_title]).start()
                    else: print(f"Map Checker already {value}.")
                 elif command == "set_vote_skip":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_vote_skip <true|false>"); continue
                    vs_config = bot_instance.runtime_config['vote_skip']; value = args[0].lower() in ['true', 'on']
                    if vs_config['enabled'] != value:
                        vs_config['enabled'] = value; print(f"Admin set Vote Skip: {value}"); setting_name_for_announce = "Vote Skip"; value_for_announce = value; config_changed = True
                        if not value and bot_instance.vote_skip_active: bot_instance.clear_vote_skip("Disabled by admin"); bot_instance.send_message("Vote skip disabled. Current vote cancelled.")
                    else: print(f"Vote Skip already {value}.")
                 elif command == "set_auto_start":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_auto_start <true|false>"); continue
                    as_config = bot_instance.runtime_config['auto_start']; value = args[0].lower() in ['true', 'on']
                    if as_config['enabled'] != value: as_config['enabled'] = value; print(f"Admin set Auto Start: {value}"); setting_name_for_announce = "Auto Start"; value_for_announce = value; config_changed = True
                    else: print(f"Auto Start already {value}.")
                 elif command == "set_auto_close":
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_auto_close <true|false>"); continue
                    ac_config = bot_instance.runtime_config['auto_close_empty_room']; value = args[0].lower() in ['true', 'on']
                    if ac_config['enabled'] != value:
                        ac_config['enabled'] = value; print(f"Admin set Auto Close: {value}"); setting_name_for_announce = "Auto Close Empty Room"; value_for_announce = value; config_changed = True
                        if not value and bot_instance.empty_room_close_timer_active: log.info("Auto-close disabled, cancelling timer."); bot_instance.empty_room_close_timer_active = False; bot_instance.empty_room_timestamp = 0
                    else: print(f"Auto Close already {value}.")
                 elif command == "set_map_history": # Maps Played List Toggle
                    if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_map_history <true|false>"); continue
                    mpl_config = bot_instance.runtime_config['maps_played_list']; value = args[0].lower() in ['true', 'on']
                    if mpl_config['enabled'] != value:
                         mpl_config['enabled'] = value; print(f"Admin set Maps Played History: {value}"); setting_name_for_announce = "Maps Played History"; value_for_announce = value; config_changed = True
                         if not value: bot_instance.played_maps_history.clear(); log.info("Map history disabled, cleared history deque.")
                    else: print(f"Maps Played History already {value}.")

                 # <<< Maps Played List Size (Saved) >>>
                 elif command == "set_map_history_size":
                    if not args or not args[0].isdigit(): print("Usage: set_map_history_size <number>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Size must be >= 0."); continue # Allow 0 (disables history effect if enabled)
                        mpl_config = bot_instance.runtime_config['maps_played_list']
                        # Use get with a default that won't match to ensure update happens if key missing
                        if mpl_config.get('max_size', -1) != value:
                             mpl_config['max_size'] = value
                             print(f"Admin set Maps Played History Size: {value}")
                             setting_name_for_announce = "Maps Played List Size"
                             value_for_announce = value # Announce the integer value
                             config_changed = True
                             # Deque resizing is handled in announce_setting_change
                        else:
                            print(f"Maps Played History Size already set to {value}.")
                    except ValueError:
                        print("Invalid number for history size.")
                        
                 # --- Bot Delay/Timeout Settings (Saved) ---
                 elif command == "set_afk_timeout":
                    if not args or not args[0].isdigit(): print("Usage: set_afk_timeout <seconds>"); continue
                    try:
                        value = int(args[0])
                        if value <= 0: print("Timeout must be positive."); continue
                        afk_config = bot_instance.runtime_config['afk_handling']
                        if afk_config['timeout_seconds'] != value:
                            afk_config['timeout_seconds'] = value; print(f"Admin set AFK Timeout: {value} seconds"); setting_name_for_announce = "AFK Timeout"; value_for_announce = f"{value}s"; config_changed = True
                        else: print(f"AFK Timeout already set to {value} seconds.")
                    except ValueError: print("Invalid number for timeout seconds.")
                 elif command == "set_auto_start_delay":
                    if not args or not args[0].isdigit(): print("Usage: set_auto_start_delay <seconds>"); continue
                    try:
                        value = int(args[0])
                        if value < 1: print("Delay must be at least 1 second."); continue
                        as_config = bot_instance.runtime_config['auto_start']
                        if as_config['delay_seconds'] != value:
                            as_config['delay_seconds'] = value; print(f"Admin set Auto Start Delay: {value} seconds"); setting_name_for_announce = "Auto Start Delay"; value_for_announce = f"{value}s"; config_changed = True
                        else: print(f"Auto Start Delay already set to {value} seconds.")
                    except ValueError: print("Invalid number for delay seconds.")
                 elif command == "set_auto_close_delay":
                    if not args or not args[0].isdigit(): print("Usage: set_auto_close_delay <seconds>"); continue
                    try:
                        value = int(args[0])
                        if value < 5: print("Delay must be at least 5 seconds."); continue
                        ac_config = bot_instance.runtime_config['auto_close_empty_room']
                        if ac_config['delay_seconds'] != value:
                            ac_config['delay_seconds'] = value; print(f"Admin set Auto Close Delay: {value} seconds"); setting_name_for_announce = "Auto Close Delay"; value_for_announce = f"{value}s"; config_changed = True
                        else: print(f"Auto Close Delay already set to {value} seconds.")
                    except ValueError: print("Invalid number for delay seconds.")
                 elif command == "set_vote_skip_timeout":
                    if not args or not args[0].isdigit(): print("Usage: set_vote_skip_timeout <seconds>"); continue
                    try:
                        value = int(args[0])
                        if value <= 5: print("Timeout must be greater than 5 seconds."); continue
                        vs_config = bot_instance.runtime_config['vote_skip']
                        if vs_config['timeout_seconds'] != value:
                            vs_config['timeout_seconds'] = value; print(f"Admin set Vote Skip Timeout: {value} seconds"); setting_name_for_announce = "Vote Skip Timeout"; value_for_announce = f"{value}s"; config_changed = True
                        else: print(f"Vote Skip Timeout already set to {value} seconds.")
                    except ValueError: print("Invalid number for timeout seconds.")
                 elif command == "set_map_history_size": # Maps Played List Size
                    if not args or not args[0].isdigit(): print("Usage: set_map_history_size <number>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Size must be >= 0."); continue
                        mpl_config = bot_instance.runtime_config['maps_played_list']
                        if mpl_config.get('max_size', -1) != value: # Compare with current value
                             mpl_config['max_size'] = value; print(f"Admin set Maps Played History Size: {value}"); setting_name_for_announce = "Maps Played List Size"; value_for_announce = value; config_changed = True
                        else: print(f"Maps Played History Size already {value}.")
                    except ValueError: print("Invalid number.")

                 # --- Vote Skip Threshold (Saved) ---
                 elif command == "set_vote_skip_threshold_type":
                    if not args or args[0].lower() not in ['percentage', 'fixed']: print("Usage: set_vote_skip_threshold_type <percentage|fixed>"); continue
                    vs_config = bot_instance.runtime_config['vote_skip']; value = args[0].lower()
                    if vs_config['threshold_type'] != value:
                        vs_config['threshold_type'] = value; print(f"Admin set Vote Skip Threshold Type: {value}"); setting_name_for_announce = "Vote Skip Type"; value_for_announce = value; config_changed = True
                    else: print(f"Vote Skip Threshold Type already set to {value}.")
                 elif command == "set_vote_skip_threshold_value":
                    if not args: print("Usage: set_vote_skip_threshold_value <number>"); continue
                    try:
                        value = int(args[0])
                        if value < 1: print("Threshold value must be at least 1."); continue
                        vs_config = bot_instance.runtime_config['vote_skip']
                        if vs_config['threshold_value'] != value:
                            vs_config['threshold_value'] = value; print(f"Admin set Vote Skip Threshold Value: {value}"); setting_name_for_announce = "Vote Skip Value"; value_for_announce = value; config_changed = True
                        else: print(f"Vote Skip Threshold Value already set to {value}.")
                    except ValueError: print("Invalid number for threshold value.")

                 # --- Map Rule Settings (Saved) ---
                 elif command == "set_star_min":
                   if not args: print("Usage: set_star_min <number|0>"); continue
                   try:
                       value = float(args[0])
                       if value < 0: print("Min stars cannot be negative."); continue
                       mc_config = bot_instance.runtime_config['map_checker']
                       if abs(mc_config.get('min_stars', 0) - value) > 0.001:
                           mc_config['min_stars'] = value; print(f"Admin set Minimum Star Rating to: {value:.2f}*"); setting_name_for_announce = "Min Stars"; value_for_announce = f"{value:.2f}*"; config_changed = True
                       else: print(f"Minimum Star Rating already set to {value:.2f}*")
                   except ValueError: print("Invalid number for minimum stars.")
                 elif command == "set_star_max":
                    if not args: print("Usage: set_star_max <number|0>"); continue
                    try:
                        value = float(args[0])
                        if value < 0: print("Max stars cannot be negative."); continue
                        mc_config = bot_instance.runtime_config['map_checker']
                        if abs(mc_config.get('max_stars', 0) - value) > 0.001:
                            mc_config['max_stars'] = value; print(f"Admin set Maximum Star Rating to: {value:.2f}*"); setting_name_for_announce = "Max Stars"; value_for_announce = f"{value:.2f}*"; config_changed = True
                        else: print(f"Maximum Star Rating already set to {value:.2f}*")
                    except ValueError: print("Invalid number for maximum stars.")
                 elif command == "set_len_min":
                    if not args: print("Usage: set_len_min <seconds|0>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Min length cannot be negative."); continue
                        mc_config = bot_instance.runtime_config['map_checker']
                        if mc_config.get('min_length_seconds', 0) != value:
                            mc_config['min_length_seconds'] = value; formatted_time = bot_instance._format_time(value); print(f"Admin set Minimum Map Length to: {formatted_time} ({value}s)"); setting_name_for_announce = "Min Length"; value_for_announce = formatted_time; config_changed = True
                        else: print(f"Minimum Map Length already set to {bot_instance._format_time(value)}")
                    except ValueError: print("Invalid number for minimum length seconds.")
                 elif command == "set_len_max":
                    if not args: print("Usage: set_len_max <seconds|0>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Max length cannot be negative."); continue
                        mc_config = bot_instance.runtime_config['map_checker']
                        if mc_config.get('max_length_seconds', 0) != value:
                            mc_config['max_length_seconds'] = value; formatted_time = bot_instance._format_time(value); print(f"Admin set Maximum Map Length to: {formatted_time} ({value}s)"); setting_name_for_announce = "Max Length"; value_for_announce = formatted_time; config_changed = True
                        else: print(f"Maximum Map Length already set to {bot_instance._format_time(value)}")
                    except ValueError: print("Invalid number for maximum length seconds.")
                 elif command == "set_statuses":
                    valid_statuses_lower = [s.lower() for s in OSU_STATUSES_NUM.values()] + ['all']
                    if not args: current = ', '.join(bot_instance.runtime_config.get('allowed_map_statuses', ['all'])); print(f"Current allowed statuses: {current}\nUsage: set_statuses <status1> [status2...] or 'all'\nAvailable: {', '.join(OSU_STATUSES_NUM.values())}"); continue
                    input_statuses = [s.lower() for s in args]; value = ['all'] if 'all' in input_statuses else sorted([s for s in input_statuses if s in valid_statuses_lower and s != 'all'])
                    if not value: print(f"No valid statuses provided. Use 'all' or values from: {', '.join(OSU_STATUSES_NUM.values())}"); continue
                    current_value = sorted(bot_instance.runtime_config.get('allowed_map_statuses', ['all']))
                    if current_value != value:
                        bot_instance.runtime_config['allowed_map_statuses'] = value; display_value = ', '.join(value); print(f"Admin set Allowed Map Statuses to: {display_value}"); setting_name_for_announce = "Allowed Statuses"; value_for_announce = display_value; config_changed = True
                    else: print(f"Allowed Map Statuses already set to: {', '.join(value)}")
                 elif command == "set_modes":
                    valid_modes_lower = [m.lower() for m in OSU_MODES.values()] + ['all']
                    if not args: current = ', '.join(bot_instance.runtime_config.get('allowed_modes', ['all'])); print(f"Current allowed modes: {current}\nUsage: set_modes <mode1> [mode2...] or 'all'\nAvailable: {', '.join(OSU_MODES.values())}"); continue
                    input_modes = [m.lower() for m in args]; value = ['all'] if 'all' in input_modes else sorted([m for m in input_modes if m in valid_modes_lower and m != 'all'])
                    if not value: print(f"No valid modes provided. Use 'all' or values from: {', '.join(OSU_MODES.values())}"); continue
                    current_value = sorted(bot_instance.runtime_config.get('allowed_modes', ['all']))
                    if current_value != value:
                        bot_instance.runtime_config['allowed_modes'] = value; display_value = ', '.join(value); print(f"Admin set Allowed Game Modes to: {display_value}"); setting_name_for_announce = "Allowed Modes"; value_for_announce = display_value; config_changed = True
                    else: print(f"Allowed Game Modes already set to: {', '.join(value)}")
                 elif command == "set_violations_allowed":
                    if not args or not args[0].isdigit(): print("Usage: set_violations_allowed <number>"); continue
                    try:
                        value = int(args[0])
                        if value < 0: print("Cannot allow negative violations."); continue
                        mc_config = bot_instance.runtime_config['map_checker']
                        if mc_config.get('violations_allowed', 3) != value:
                            mc_config['violations_allowed'] = value; print(f"Admin set Map Violations Allowed to: {value}"); setting_name_for_announce = "Violations Allowed"; value_for_announce = value; config_changed = True
                        else: print(f"Map Violations Allowed already set to {value}.")
                    except ValueError: print("Invalid number for violations allowed.")

                 # --- Help and Unknown ---
                 elif command == "help":
                   print("\n--- Admin Console Commands (In Room) ---")
                   print(" Room Control:")
                   print("  stop                - Leave the current room.")
                   print("  close_room          - Send '!mp close' to close the room.")
                   print("  skip [reason]       - Force skip the current host.")
                   print("  queue / q           - Show the host queue (console).")
                   print("  status / info       - Show detailed bot/lobby status (console).")
                   print(" Lobby Settings (!mp):")
                   print("  say <message>       - Send a message as the bot.")
                   print("  set_password <pw|clear> - Set/remove lobby password.")
                   print("  set_size <1-16>     - Change lobby size.")
                   print("  set_name <\"name\">   - Change lobby name.")
                   print(" Bot Feature Toggles & Settings (Saved):")
                   print("  set_rotation <t/f>  - Enable/Disable host rotation.")
                   print("  set_map_check <t/f> - Enable/Disable map checker.")
                   print("  set_vote_skip <t/f> - Enable/Disable vote skipping.")
                   print("  set_auto_start <t/f>- Enable/Disable auto starting.")
                   print("  set_auto_close <t/f>- Enable/Disable auto closing empty room.")
                   print("  set_map_history <t/f> - Enable/Disable map played history check.")
                   print(" Map History (Saved):")
                   print("  set_map_history_size <N> - Set how many maps to remember (N >= 0).")
                   print(" Bot Delay/Timeout Settings (Saved):")
                   print("  set_afk_timeout <sec> - Set host AFK timeout (sec > 0).")
                   print("  set_auto_start_delay <sec> - Set delay (sec >=1) for auto start.")
                   print("  set_auto_close_delay <sec> - Set delay (sec >=5) for auto close.")
                   print("  set_vote_skip_timeout <sec> - Set timeout (sec > 5) for vote skip.")
                   print(" Vote Skip Threshold (Saved):")
                   print("  set_vote_skip_threshold_type <percentage|fixed>")
                   print("  set_vote_skip_threshold_value <number> (percentage 1-100 or fixed count >=1)")
                   print(" Map Rules (Need Map Check Enabled, Saved):")
                   print("  set_star_min <N>    - Set min stars (0=off).")
                   print("  set_star_max <N>    - Set max stars (0=off).")
                   print("  set_len_min <sec>   - Set min length seconds (0=off).")
                   print("  set_len_max <sec>   - Set max length seconds (0=off).")
                   print("  set_statuses <...>  - Set allowed statuses (ranked, loved, all, etc.).")
                   print("  set_modes <...>     - Set allowed modes (mania, all, etc.).")
                   print("  set_violations_allowed <N> - Set map violations before skip (N >= 0).")
                   print(" General:")
                   print("  quit / exit         - Disconnect bot and exit.")
                   print("  help                - Show this help message.")
                   print("------------------------------------------\n")
                 else:
                    print(f"Unknown command: '{command}' while in room. Type 'help'.")                 

                 # Save config if changed
                 if config_changed:
                    if save_config(bot_instance.runtime_config):
                        print("Configuration changes saved to config.json.")
                        if setting_name_for_announce and value_for_announce is not None:
                            # Ensure announce happens *after* potential state change (like deque resize)
                            threading.Timer(0.1, bot_instance.announce_setting_change, args=[setting_name_for_announce, value_for_announce]).start()
                    else:
                        print("ERROR: Failed to save configuration changes.")

            # --- JOINING State Commands ---
            elif current_state == BOT_STATE_JOINING:
                 if command == "help":
                    print("\n--- Available Commands (Joining State) ---")
                    print("  status / info        - Show current bot status.")
                    print("  quit / exit          - Cancel joining and exit.")
                    print("------------------------------------------\n")
                 elif command not in ['status', 'info', 'quit', 'exit', 'help']:
                     print(f"Command '{command}' ignored while joining. Please wait. Use 'status', 'info', 'quit', or 'help'.")

        except Exception as e:
            log.error(f"Error in console input loop: {e}", exc_info=True)
            print(f"\nAn error occurred processing the command: {e}")
            # Avoid printing prompt again immediately after error, let loop restart

    log.info("Console input thread finished.")

# --- Main Execution ---
def main():
    global shutdown_requested

    config = load_or_generate_config(CONFIG_FILE)
    # load_or_generate_config now exits on critical failure, no need to check return

    bot = None
    try:
        bot = OsuRoomBot(config) # Pass the loaded config
        bot.bot_state = BOT_STATE_INITIALIZING
    except Exception as e:
        log.critical(f"Failed to initialize OsuRoomBot: {e}", exc_info=True)
        sys.exit(1)

    # Setup signal handling
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start console input thread
    console_thread = threading.Thread(target=console_input_loop, args=(bot,), daemon=True, name="AdminConsoleThread")
    console_thread.start()

    # --- Connection Attempt ---
    log.info(f"Connecting to {config['server']}:{config['port']} as {config['username']}...")
    connection_successful = False
    try:
        bot.connect(
            server=config['server'], port=config['port'],
            nickname=config['username'], password=config['password'],
            username=config['username']
        )
        connection_successful = True
    except irc.client.ServerConnectionError as e:
        log.critical(f"IRC Connection failed: {e}")
        err_str = str(e).lower(); msg = ""
        if "nickname is already in use" in err_str: msg = "-> Nickname in use. Check config.json or if another client is connected."
        elif "incorrect password" in err_str or "authentication failed" in err_str: msg = "-> Incorrect IRC password. Get from osu! website account settings (Legacy API)."
        elif "cannot assign requested address" in err_str or "temporary failure" in err_str: msg = f"-> Network error connecting to {config['server']}. Check server address/internet."
        else: msg = f"-> Unhandled server connection error: {e}"
        log.critical(msg); bot._request_shutdown("Connection Error")
    except Exception as e:
        log.critical(f"Unexpected error during bot.connect call: {e}", exc_info=True)
        bot._request_shutdown("Connect Exception")

    # --- Main Loop ---
    if connection_successful:
        log.info("Starting main processing loop...")
        last_periodic_check = time.time()
        check_interval = 5 # Seconds between periodic checks
        ROTATION_SAFETY_TIMEOUT = 20 # Seconds to wait for host confirmation during rotation

        while not shutdown_requested:
            try:
                # Process IRC events with a short timeout
                bot.reactor.process_once(timeout=0.2)

                current_state = bot.bot_state # Cache state for checks
                if current_state == BOT_STATE_IN_ROOM and bot.connection.is_connected():
                    now = time.time()
                    if now - last_periodic_check >= check_interval:
                        # Run periodic checks
                        try: bot.check_afk_host()
                        except Exception as e: log.error(f"Error in check_afk_host: {e}", exc_info=True)
                        try: bot.check_vote_skip_timeout()
                        except Exception as e: log.error(f"Error in check_vote_skip_timeout: {e}", exc_info=True)
                        try: bot.check_empty_room_close()
                        except Exception as e: log.error(f"Error in check_empty_room_close: {e}", exc_info=True)

                        # <<< ADDED Safety check for stuck rotation state >>>
                        try:
                            # Check if rotation is flagged, a start time exists, and timeout exceeded
                            if bot.is_rotating_host and bot._rotation_start_time > 0 and \
                               (now - bot._rotation_start_time > ROTATION_SAFETY_TIMEOUT):
                                log.warning(f"Rotation state safety timeout ({ROTATION_SAFETY_TIMEOUT}s) hit. Expected host '{bot._expected_next_host}' never confirmed. Clearing rotation state.")
                                bot.is_rotating_host = False
                                bot._expected_next_host = None
                                bot._rotation_start_time = 0
                                # Display queue again to reflect the potentially unexpected state
                                bot.display_host_queue()
                        except Exception as e:
                            log.error(f"Error checking rotation timeout: {e}", exc_info=True)
                        # <<< END Safety check >>>

                        last_periodic_check = now # Reset check timer
                elif current_state == BOT_STATE_SHUTTING_DOWN:
                     break # Exit loop if shutdown requested

            except irc.client.ServerNotConnectedError:
                if bot.bot_state != BOT_STATE_SHUTTING_DOWN:
                    log.warning("Disconnected during processing loop. Requesting shutdown.")
                    bot._request_shutdown("Disconnected in main loop")
                break # Exit loop on disconnect
            except KeyboardInterrupt:
                 if not shutdown_requested:
                     log.info("Main loop KeyboardInterrupt. Requesting shutdown.")
                     bot._request_shutdown("Main loop Ctrl+C")
                 break # Exit loop on Ctrl+C
            except Exception as e:
                log.error(f"Unhandled exception in main loop: {e}", exc_info=True)
                time.sleep(2) # Pause briefly after an unknown error to prevent spamming logs

    # --- Shutdown Sequence ---
    log.info("Main loop exited or connection failed. Initiating final shutdown...")
    # Ensure shutdown flag is set even if loop exited unexpectedly
    if not shutdown_requested:
        shutdown_requested = True
        if bot and bot.bot_state != BOT_STATE_SHUTTING_DOWN:
             bot.bot_state = BOT_STATE_SHUTTING_DOWN

    if bot:
        bot.shutdown("Client shutting down normally.") # Handles QUIT, goodbye message etc.

    log.info("Waiting for console thread to exit...")
    console_thread.join(timeout=2.0) # Give console thread time to finish
    if console_thread.is_alive():
        log.warning("Console thread did not exit cleanly after timeout.")

    log.info("osu-ahr-py finished.")


# --- Entry Point ---
if __name__ == "__main__":
    main_exit_code = 0
    try:
        main()
    except SystemExit as e:
         log.info(f"Program exited with code {e.code}.")
         main_exit_code = e.code if isinstance(e.code, int) else 1
    except KeyboardInterrupt:
         # This catches Ctrl+C pressed *before* the main loop's try/except block
         log.info("\nMain execution interrupted by Ctrl+C during startup/shutdown. Exiting.")
         main_exit_code = 1
    except Exception as e:
        # Catch-all for unexpected errors during startup/shutdown
        log.critical(f"CRITICAL UNHANDLED ERROR during execution: {e}", exc_info=True)
        main_exit_code = 1
    finally:
        logging.shutdown() # Ensure log handlers are flushed
        # input("Press Enter to exit...") # Optional: Keep console open after script finishes
        sys.exit(main_exit_code)