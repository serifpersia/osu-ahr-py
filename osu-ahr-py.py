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
        self.target_channel = f"#mp_{config['room_id']}"
        self.connection_registered = False
        self.is_matching = False

        # Host Rotation
        self.host_queue = deque()
        self.current_host = None
        self.last_host = None # Who was host before the *last* rotation
        self.host_last_action_time = 0 # For AFK check
        # *** NEW: Flag to prevent AFK timeout when host selected valid map ***
        self.host_map_selected_valid = False

        # Player Tracking (simple list for vote skip threshold)
        self.players_in_lobby = set() # Keep track of who is actually in

        # Map Checker
        self.api_client_id = self.config.get('osu_api_client_id', 0)
        self.api_client_secret = self.config.get('osu_api_client_secret', '')
        self.current_map_id = 0
        self.current_map_title = ""
        self.last_valid_map_id = 0 # Store ID of last map that passed checks
        self.map_violations = {} # {player_name: count}

        # Vote Skip State
        self.vote_skip_active = False
        self.vote_skip_target = None
        self.vote_skip_initiator = None
        self.vote_skip_voters = set()
        self.vote_skip_start_time = 0

        # Events
        self.JoinedLobby = TypedEvent()
        self.PlayerJoined = TypedEvent()
        self.PlayerLeft = TypedEvent()
        self.HostChanged = TypedEvent()
        self.MatchStarted = TypedEvent()
        self.MatchFinished = TypedEvent()
        self.SentMessage = TypedEvent()

        # Initial Validation
        if self.config['map_checker']['enabled'] and (not self.api_client_id or self.api_client_secret == 'YOUR_CLIENT_SECRET'):
            log.warning("Map checker enabled in config but API keys missing/default. Disabling map check feature.")
            self.config['map_checker']['enabled'] = False # Override config setting if keys invalid

        log.info(f"Bot initialized for {self.target_channel}.")
        self.log_feature_status() # Log initial status of key features

    def log_feature_status(self):
        """Logs the status of major configurable features."""
        hr_enabled = self.config.get('host_rotation', {}).get('enabled', False)
        mc_enabled = self.config.get('map_checker', {}).get('enabled', False)
        vs_enabled = self.config.get('vote_skip', {}).get('enabled', False)
        afk_enabled = self.config.get('afk_handling', {}).get('enabled', False)
        as_enabled = self.config.get('auto_start', {}).get('enabled', False)
        log.info(f"Features: Rotation:{hr_enabled}, MapCheck:{mc_enabled}, VoteSkip:{vs_enabled}, AFKCheck:{afk_enabled}, AutoStart:{as_enabled}")
        if mc_enabled:
            self.log_map_rules() # Log rules if map check is on

    def announce_setting_change(self, setting_name, new_value):
        """Sends a notification to the chat when an admin changes a setting."""
        if not self.connection.is_connected():
            log.warning("Cannot announce setting change, not connected.")
            return

        message = f"Admin updated setting: {setting_name} set to {new_value}"
        log.info(f"Announcing to chat: {message}")
        self.send_message(message) # Send as single message
        self.log_feature_status() # Re-log feature status after change

    def log_map_rules(self):
        """Logs the current map checking rules to the console."""
        # ... (Keep log_map_rules as it was) ...
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
        # ... (Keep _format_time as it was) ...
        if seconds is None or not isinstance(seconds, (int, float)) or seconds <= 0:
            return "N/A"
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

    # --- Core IRC Event Handlers ---
    def on_welcome(self, connection, event):
        # ... (Keep on_welcome largely as it was, ensures initial settings request) ...
        if self.connection_registered:
            log.info("Received WELCOME event again (reconnect?), already initialized.")
            return
        log.info(f"Connected to {connection.server}:{connection.port} as {connection.get_nickname()}")
        self.connection_registered = True
        try:
            connection.set_keepalive(60)
            log.info(f"Joining channel: {self.target_channel}")
            connection.join(self.target_channel)
            # Request initial state after a short delay
            threading.Timer(2.5, self.request_initial_settings).start()
        except irc.client.ServerNotConnectedError:
            log.warning("Connection lost before join/keepalive.")
            self._request_shutdown()
        except Exception as e:
            log.error(f"Error during on_welcome setup: {e}", exc_info=True)
            self._request_shutdown()


    def request_initial_settings(self):
        """Requests !mp settings after joining."""
        if self.connection.is_connected():
            log.info("Requesting initial room state with !mp settings")
            # Clear potentially stale state before getting settings
            self.players_in_lobby.clear()
            self.host_queue.clear()
            self.current_host = None
            self.host_map_selected_valid = False # Reset flag
            self.last_valid_map_id = 0
            self.current_map_id = 0
            self.current_map_title = ""
            self.map_violations.clear()
            self.clear_vote_skip("initial settings request")
            self.send_message("!mp settings")
            # Initialization of queue/host happens after parsing settings messages
        else:
            log.warning("Cannot request !mp settings, disconnected.")

    # ... (Keep NicknameInUse, Channel Join Errors, on_join, on_disconnect, on_privmsg as they were) ...
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
        if channel.lower() == self.target_channel.lower():
            log.warning(f"Disconnecting because cannot join target channel '{self.target_channel}' ({error_type}).")
            self.shutdown(f"Cannot join {channel}: {error_type}")

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
            if self.config.get("welcome_message"):
                self.send_message(self.config["welcome_message"]) # Single message
            # !mp settings requested in on_welcome via timer now
            self.JoinedLobby.emit({'channel': channel})
        elif nick == connection.get_nickname():
            log.info(f"Joined other channel: {channel}")
        # Do NOT add players to queue/players_in_lobby here, wait for !mp settings or Bancho join message

    def on_disconnect(self, connection, event):
        reason = event.arguments[0] if event.arguments else "Unknown reason"
        log.warning(f"Disconnected from server: {reason}")
        self.connection_registered = False
        self._request_shutdown()

    def on_privmsg(self, connection, event):
        log.info(f"[PRIVATE] <{event.source.nick}> {event.arguments[0]}")


    def on_pubmsg(self, connection, event):
        sender = event.source.nick
        channel = event.target
        message = event.arguments[0]

        if channel != self.target_channel: return

        # Log user messages, ignore bot's own messages unless debugging needed
        if sender != connection.get_nickname():
            log.info(f"[{channel}] <{sender}> {message}")

        if sender == "BanchoBot":
            self.parse_bancho_message(message)
        else:
            # Handle user commands
            self.parse_user_command(sender, message)

    # --- User Command Parsing ---
    def parse_user_command(self, sender, message):
        """Parses commands sent by users in the channel."""
        if not message.startswith("!"): return

        parts = shlex.split(message)
        if not parts: return
        command = parts[0].lower()
        args = parts[1:] # Capture arguments

        # Commands available to everyone
        if command == '!queue':
            if self.config['host_rotation']['enabled']:
                log.info(f"{sender} requested host queue.")
                self.display_host_queue() # Will now send single message
            else:
                self.send_message("Host rotation is currently disabled.")
            return

        if command == '!help':
            log.info(f"{sender} requested help.")
            self.display_help_message()
            return

        if command == '!rules':
            log.info(f"{sender} requested rules.")
            self.display_map_rules_to_chat() # Sends 1-2 messages
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

            self.handle_vote_skip(sender) # Sends 1 message per vote/initiation
            return

        # Commands available only to current host
        if sender == self.current_host:
            if command == '!skip':
                if self.config['host_rotation']['enabled']:
                    log.info(f"Host {sender} used !skip.")
                    self.skip_current_host("Host self-skipped") # Sends 1-2 messages
                else:
                    log.info(f"{sender} tried to use !skip (rotation disabled).")
                    self.send_message("Host rotation is disabled, !skip command is inactive.")
                return

            # *** NEW: !start command ***
            if command == '!start':
                log.info(f"Host {sender} trying to use !start...")
                if self.is_matching:
                    self.send_message("Match is already in progress.")
                    return
                if self.current_map_id == 0:
                    self.send_message("No map selected to start.")
                    return
                # Optional: Check if map checker enabled and map is invalid?
                # if self.config['map_checker']['enabled'] and self.last_valid_map_id != self.current_map_id:
                #    self.send_message(f"Cannot start: Map {self.current_map_id} is invalid or was not checked.")
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
                self.send_message(f"!mp start{delay_str}") # Send command to Bancho
                return

            # *** NEW: !abort command ***
            if command == '!abort':
                log.info(f"Host {sender} sending !mp abort")
                # Abort usually works before match starts or during countdown
                # No need to check is_matching usually, let Bancho handle if it's too late
                self.send_message("!mp abort") # Send command to Bancho
                # Consider resetting host_map_selected_valid here? Maybe not necessary.
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


    # --- BanchoBot Message Parsing ---
    def parse_bancho_message(self, msg):
        # Use try-except to prevent crashes on unexpected Bancho formats
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
                # More robust regex for /b/ or /beatmaps/ or /beatmapsets/..#/
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
            # Check if the !mp settings processing is complete
            elif msg.startswith("Team mode:") or msg.startswith("Win condition:") or msg.startswith("Active mods:") or msg.startswith("Free mods:"):
                 self.check_initialization_complete(msg) # Trigger final setup after last expected settings line

            # Add other Bancho messages if needed
            elif " was kicked from the room." in msg:
                 match = re.match(r"(.+?) was kicked from the room\.", msg)
                 if match: self.handle_player_left(match.group(1)) # Treat kick like a leave
            elif " changed the room name to " in msg: pass # Can ignore or log
            elif " changed the password." in msg: pass # Can ignore or log
            elif " removed the password." in msg: pass # Can ignore or log
            elif " changed room size to " in msg:
                 self._parse_player_count_from_size_change(msg) # Update player count if size changes
            elif msg == "Match Aborted": # Handle abort message from Bancho
                 log.info("BanchoBot reported: Match Aborted")
                 self.is_matching = False # Ensure match state is reset if it was somehow true
                 # No automatic rotation on abort, host likely keeps turn
                 if self.current_host:
                      self.reset_host_timers_and_state(self.current_host) # Reset AFK timer


        except Exception as e:
            log.error(f"Error parsing Bancho msg: '{msg}' - {e}", exc_info=True)

    def _parse_initial_beatmap(self, msg):
        # ... (Keep _parse_initial_beatmap as it was) ...
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
            # Don't check map here, wait for host identification
            self.last_valid_map_id = 0 # Ensure not marked valid yet
            self.host_map_selected_valid = False
        else:
            log.warning(f"Could not parse initial beatmap msg: {msg}")
            self.current_map_id = 0
            self.current_map_title = ""
            self.last_valid_map_id = 0
            self.host_map_selected_valid = False

    def _parse_player_count(self, msg):
        """Parses 'Players: N' message to update player list size (for voteskip)."""
        # ... (Keep _parse_player_count as it was) ...
        match = re.match(r"Players: (\d+)", msg)
        if match:
            log.debug(f"Parsed player count: {match.group(1)}")
        else:
             log.warning(f"Could not parse player count msg: {msg}")

    def _parse_player_count_from_size_change(self, msg):
        # ... (Keep _parse_player_count_from_size_change as it was) ...
        match = re.search(r"changed room size to (\d+)", msg)
        if match:
             log.info(f"Room size changed to {match.group(1)}. Player count may need updating via !mp settings or join/leave.")
        else:
             log.warning(f"Could not parse size change message: {msg}")


    def _parse_slot_message(self, msg):
        """Parses slot info from !mp settings to identify host and populate initial queue/player list."""
        # ... (Keep _parse_slot_message as it was) ...
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

             if is_host:
                 if self.current_host != player_name:
                     log.info(f"Identified host from settings: {player_name}")
                     self.current_host = player_name
                     self.reset_host_timers_and_state(player_name) # Resets AFK timer, violations, host_map_selected_valid flag

             if self.config['host_rotation']['enabled'] and player_name not in self.host_queue:
                 # Ensure player is added before potential reordering in initialize_lobby_state
                 if player_name not in self.host_queue:
                    self.host_queue.append(player_name)
                    log.info(f"Added '{player_name}' to host queue from settings.")

    def check_initialization_complete(self, last_message_seen):
         """Called after seeing the last expected line from !mp settings. Finalizes host queue and checks initial map."""
         # ... (Keep check_initialization_complete as it was) ...
         log.info(f"Assuming !mp settings finished (last seen: '{last_message_seen[:30]}...'). Finalizing initial state.")
         self.initialize_lobby_state()


    def initialize_lobby_state(self):
        """Finalizes host queue (if enabled) and checks initial map after !mp settings."""
        # ... (Keep initialize_lobby_state largely as it was, but ensure host state reset) ...
        hr_enabled = self.config['host_rotation']['enabled']
        mc_enabled = self.config['map_checker']['enabled']

        log.info(f"Finalizing initial state. Players: {len(self.players_in_lobby)}. Rotation Enabled: {hr_enabled}. Queue: {list(self.host_queue)}, Identified Host: {self.current_host}")

        if hr_enabled:
            if not self.host_queue:
                log.warning("Rotation enabled, but no players found in !mp settings to initialize host queue.")
            elif self.current_host and self.current_host in self.host_queue:
                 # Ensure identified host is at the front if rotation is on
                 if not self.host_queue or self.host_queue[0] != self.current_host: # Check queue exists before accessing [0]
                     try:
                         self.host_queue.remove(self.current_host)
                         self.host_queue.insert(0, self.current_host)
                         log.info(f"Moved identified host '{self.current_host}' to front of queue.")
                     except ValueError:
                          log.warning(f"Host '{self.current_host}' identified but vanished before queue reorder?")
                          # If host left, the first person in queue is effectively the next host
                          if self.host_queue:
                              self.current_host = self.host_queue[0] # Assume first player is host now
                              log.info(f"Setting host to first player in queue: {self.current_host}")
                              self.reset_host_timers_and_state(self.current_host)
                          else:
                               self.current_host = None # No host if queue is empty
                 else:
                      # Host already at front, still ensure state is reset
                      self.reset_host_timers_and_state(self.current_host)

            elif self.host_queue: # Queue has players, but no host identified yet from slots
                self.current_host = self.host_queue[0] # Assume first is host
                log.info(f"No host identified from settings, assuming first player is host: {self.current_host}")
                self.reset_host_timers_and_state(self.current_host)
                # Bancho might send a "became host" message later, which is fine

            if self.current_host:
                 log.info(f"Host rotation initialized. Current Host: {self.current_host}, Queue: {list(self.host_queue)}")
                 # *** CHANGE: Don't display queue automatically here, wait for match finish/rotation ***
                 # self.display_host_queue() # REMOVED to reduce initial message burst
            else:
                 log.info("Host rotation enabled, but no host could be determined yet.")

        # Check initial map regardless of rotation, if checker enabled and host known
        if mc_enabled and self.current_map_id != 0 and self.current_host:
            log.info(f"Checking initial map ID {self.current_map_id} for host {self.current_host}.")
            self.check_map(self.current_map_id, self.current_map_title) # This will set host_map_selected_valid if appropriate
        elif mc_enabled:
            self.last_valid_map_id = 0 # Ensure no stale map ID initially
            self.host_map_selected_valid = False

        log.info(f"Initial lobby state setup complete. Player list: {self.players_in_lobby}")

    # --- Host Rotation & Player Tracking Logic ---
    def handle_player_join(self, player_name):
        if not player_name or player_name.lower() == "banchobot": return

        self.PlayerJoined.emit({'player': player_name})
        self.players_in_lobby.add(player_name)
        log.info(f"'{player_name}' joined. Lobby size: {len(self.players_in_lobby)}")

        # Host Rotation Logic
        if self.config['host_rotation']['enabled']:
            if player_name not in self.host_queue:
                self.host_queue.append(player_name)
                log.info(f"Added '{player_name}' to queue. Queue: {list(self.host_queue)}")
                if len(self.host_queue) == 1 and not self.current_host:
                     log.info(f"First player '{player_name}' joined while no host was assigned. Bancho will likely assign host.")
                # *** CHANGE: Do NOT display queue on join ***
                # self.display_host_queue() # REMOVED
            else:
                log.info(f"'{player_name}' joined, but already in queue.")


    def handle_player_left(self, player_name):
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
                # *** CHANGE: Do NOT display queue on leave ***
                # self.display_host_queue() # REMOVED
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
            self.host_map_selected_valid = False # Clear flag if host leaves
            # Don't immediately rotate if host leaves mid-match
            if hr_enabled and not self.is_matching:
                log.info("Host left outside match, attempting to set next host.")
                self.set_next_host() # Assign next available player if rotation on
                # *** CHANGE: Display queue *only* if host changed due to leave ***
                if self.current_host: # Check if a new host was actually set
                     self.display_host_queue()


    def handle_host_change(self, player_name):
        """Handles Bancho reporting a host change (updates state, resets timers/violations)."""
        if not player_name: return
        log.info(f"Bancho reported host changed to: {player_name}")

        if player_name == self.current_host:
             log.info(f"Host change message for '{player_name}', but they were already marked as host. Likely confirmation.")
             # Still reset timers just in case
             self.reset_host_timers_and_state(player_name)
             # Don't display queue here as it wasn't a functional change
             return

        previous_host = self.current_host
        self.current_host = player_name
        self.HostChanged.emit({'player': player_name, 'previous': previous_host})

        # Add player to lobby list if somehow missed
        if player_name not in self.players_in_lobby:
             log.warning(f"New host '{player_name}' wasn't in player list, adding.")
             self.players_in_lobby.add(player_name)

        # Reset AFK timer, violations, valid map flag, and clear any voteskip against the *previous* host
        self.reset_host_timers_and_state(player_name) # Resets flag to False
        self.clear_vote_skip("new host assigned")

        # Reorder queue if rotation is enabled and the new host isn't at the front
        hr_enabled = self.config['host_rotation']['enabled']
        queue_changed = False
        if hr_enabled and self.host_queue:
             # Ensure player exists in queue before trying to move
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

        # *** CHANGE: Display queue only if host changed *and* rotation is on ***
        # This usually happens after !mp host command or if host leaves/is skipped
        # Avoid displaying if Bancho just confirms host after map change etc. unless queue order changed
        # We display queue after rotate_and_set_host or set_next_host now
        # if hr_enabled and queue_changed:
        #     self.display_host_queue() # Display IF the queue order was modified


    def reset_host_timers_and_state(self, host_name):
         """Resets AFK timer, map violations, and valid map flag for the current/new host."""
         log.debug(f"Resetting timers/state for host '{host_name}'.")
         self.host_last_action_time = time.time() # Reset AFK timer on host change/skip
         self.host_map_selected_valid = False # Reset valid map flag
         # self.last_valid_map_id = 0 # DO NOT CLEAR last valid map ID here

         # Reset map violations
         if self.config['map_checker']['enabled']:
             self.map_violations.setdefault(host_name, 0)
             if self.map_violations[host_name] != 0:
                  log.info(f"Reset map violations for new/current host '{host_name}'.")
                  self.map_violations[host_name] = 0


    def rotate_and_set_host(self):
        """Rotates the queue (if enabled) and sets the new host via !mp host."""
        hr_enabled = self.config['host_rotation']['enabled']
        if not hr_enabled or len(self.host_queue) < 1:
            log.debug(f"Skipping host rotation (enabled={hr_enabled}, queue_size={len(self.host_queue)}).")
            # Host remains whoever Bancho says it is. Reset AFK timer for current host if they exist.
            if self.current_host:
                 self.reset_host_timers_and_state(self.current_host)
            return

        log.info(f"Attempting host rotation. Current queue: {list(self.host_queue)}")
        if len(self.host_queue) > 1:
            # Determine who just finished playing. Could be self.last_host if set, otherwise assume queue[0].
            player_to_move = self.last_host if self.last_host and self.last_host in self.host_queue else self.host_queue[0]

            if player_to_move in self.host_queue:
                try:
                    moved_player = self.host_queue.popleft() # Get first element
                    if moved_player == player_to_move:
                         self.host_queue.append(moved_player) # Move to back if it was the expected one
                         log.info(f"Rotated queue, moved '{moved_player}' to the back.")
                    else:
                         # This happens if last_host was set, but wasn't actually at the front.
                         # Put the person who *was* at the front back, and also move last_host back.
                         log.warning(f"Player at front '{moved_player}' wasn't the expected last host '{player_to_move}'. Rotating both.")
                         self.host_queue.append(moved_player) # Move front player back
                         try:
                              self.host_queue.remove(player_to_move) # Remove last_host from wherever they are
                              self.host_queue.append(player_to_move) # Add last_host to the very back
                              log.info(f"Moved '{player_to_move}' to the back as well.")
                         except ValueError:
                              log.warning(f"Expected last host '{player_to_move}' not found for secondary move.")
                except IndexError:
                     log.warning("IndexError during rotation, queue likely empty unexpectedly.")
            else:
                log.warning(f"Player '{player_to_move}' intended for rotation not found in queue. Skipping move, rotating first element.")
                if self.host_queue: # Ensure not empty before fallback rotate
                    first = self.host_queue.popleft()
                    self.host_queue.append(first)
                    log.info(f"Rotated queue (fallback), moved '{first}' to the back.")


        elif len(self.host_queue) == 1:
            log.info("Only one player in queue, no rotation needed.")
            # Still need to set them as host if they aren't already (e.g., after match finish)
        else: # len == 0
             log.warning("Rotation triggered with empty queue.")
             self.current_host = None # No one to make host
             self.host_map_selected_valid = False
             return

        # Now set the host to whoever is at the front
        log.info(f"Queue after potential rotation: {list(self.host_queue)}")
        self.last_host = None # Clear last host after rotation logic
        self.set_next_host() # This will trigger host change & state reset via Bancho message usually
        self.display_host_queue() # *** CHANGE: Display queue AFTER rotation/host set ***


    def set_next_host(self):
        """Sets the player at the front of the queue as the host via !mp host."""
        if not self.config['host_rotation']['enabled']: return

        if self.host_queue:
            next_host = self.host_queue[0]
            # Only send !mp host if the intended host is different from the current one
            if next_host != self.current_host:
                log.info(f"Setting next host to '{next_host}' via !mp host...")
                self.send_message(f"!mp host {next_host}")
                # We expect Bancho to confirm with "became the host.", which triggers handle_host_change
                # handle_host_change will then reset timers/state including host_map_selected_valid.
                # We display the queue AFTER the rotation sequence finishes now.
            else:
                log.info(f"'{next_host}' is already the host (or expected to be based on queue). Resetting timers.")
                self.reset_host_timers_and_state(next_host) # Ensure timers reset even if no !mp host sent
                # Display queue here too? Let's keep it displayed only after rotate_and_set_host call
        else:
            log.warning("Host queue is empty, cannot set next host.")
            self.current_host = None # Ensure host is cleared if queue is empty
            self.host_map_selected_valid = False


    def skip_current_host(self, reason="No reason specified"):
        """Skips the current host, rotates queue (if enabled), and sets the next host."""
        if not self.current_host:
            log.warning("Attempted to skip host, but no host is currently assigned.")
            # Don't spam chat unless it was a user command fail (handled elsewhere)
            return

        skipped_host = self.current_host
        log.info(f"Skipping host '{skipped_host}'. Reason: {reason}. Queue: {list(self.host_queue)}")

        # Announce skip (1-2 messages)
        messages = [f"Host Skipped: {skipped_host}"]
        if reason and "self-skipped" not in reason.lower() and "vote" not in reason.lower() and "afk" not in reason.lower() and "violation" not in reason.lower():
             messages.append(f"Reason: {reason}")
        self.send_message(messages)

        # Clear any active vote skip
        self.clear_vote_skip(f"host '{skipped_host}' skipped")

        # Clear valid map flag as host is being skipped
        self.host_map_selected_valid = False

        # If rotation is enabled, perform rotation logic
        if self.config['host_rotation']['enabled']:
             # Mark the skipped host so rotate_and_set_host knows who to move back
            self.last_host = skipped_host
            # Rotate queue and set the *next* person as host
            # rotate_and_set_host will handle displaying the queue after setting the new host.
            self.rotate_and_set_host()
        else:
            # If rotation is disabled, we can't automatically assign the next host.
            # Attempt to clear host via Bancho.
            log.warning("Host skipped, but rotation is disabled. Cannot set next host automatically. Clearing host.")
            self.send_message("!mp clearhost")
            self.current_host = None # Clear internal tracking


    # *** MODIFIED: Display host queue as a single message ***
    def display_host_queue(self):
        """Sends the current host queue to the chat as a single message if rotation is enabled."""
        if not self.config['host_rotation']['enabled']:
            # Don't send anything if rotation is off, !queue command handles the user message
            return

        if not self.connection.is_connected():
             log.warning("Cannot display queue, not connected.")
             return

        if not self.host_queue:
            self.send_message("Host queue is empty.")
            return

        queue_list = list(self.host_queue)
        queue_entries = []
        current_host_name = self.current_host # Cache for quick access

        for i, player in enumerate(queue_list):
            entry = f"{player}[{i+1}]" # Format as Player[Index]
            if player == current_host_name:
                entry += "(Current)" # Add marker for current host
            queue_entries.append(entry)

        # Join entries into a single string, separate with ", "
        queue_str = ", ".join(queue_entries)
        final_message = f"Host order: {queue_str}"

        # Send the single message (send_message handles truncation if too long)
        self.send_message(final_message)


    # --- Match State Handling ---
    def handle_match_start(self):
            log.info(f"Match started with map ID {self.current_map_id}.")
            self.is_matching = True
            self.last_host = None # Clear last_host marker at match start
            self.clear_vote_skip("match started") # Cancel any ongoing vote
            self.host_map_selected_valid = False # Map is now being played
            self.last_valid_map_id = 0 # Clear last valid map context at start
            self.MatchStarted.emit({'map_id': self.current_map_id})

    def handle_match_finish(self):
        log.info("Match finished.")
        self.is_matching = False
        self.last_host = self.current_host
        log.debug(f"Marking '{self.last_host}' as last host after match finish.")

        self.MatchFinished.emit({})
        self.current_map_id = 0 # Clear current map after finish
        self.current_map_title = ""
        self.last_valid_map_id = 0 # Clear last valid map context at finish
        self.host_map_selected_valid = False # Reset flag after match

        # Rotate host after a delay if enabled
        if self.config['host_rotation']['enabled']:
            log.info("Scheduling host rotation (1.5s delay) after match finish.")
            threading.Timer(1.5, self.rotate_and_set_host).start()
        else:
             if self.current_host:
                 self.reset_host_timers_and_state(self.current_host)


    def handle_all_players_ready(self):
        log.info("All players are ready.")
        # Auto-start logic
        as_config = self.config['auto_start']
        if as_config['enabled'] and not self.is_matching:
            log.debug("Checking conditions for auto-start...")

            map_ok_for_auto_start = True # Assume okay initially
            if self.current_map_id == 0:
                log.warning("Auto-start: Cannot start, no map selected.")
                map_ok_for_auto_start = False
            # *** CHANGED Condition: Use the valid flag if checker is enabled ***
            elif self.config['map_checker']['enabled'] and not self.host_map_selected_valid:
                # If checker is ON, the valid flag MUST be True
                log.warning(f"Auto-start: Cannot start, current map {self.current_map_id} did not pass validation or needs checking.")
                map_ok_for_auto_start = False
            elif not self.current_host:
                 log.warning("Auto-start: Cannot start, no current host identified.")
                 map_ok_for_auto_start = False
            # Add any other conditions here if needed

            if map_ok_for_auto_start:
                delay = max(1, as_config.get('delay_seconds', 5)) # Ensure delay is at least 1s
                log.info(f"Auto-starting match with map {self.current_map_id} in {delay} seconds.")
                self.send_message(f"!mp start {delay}")
            else:
                log.info("Auto-start conditions not met.") # Changed from debug to info

        elif not as_config['enabled']:
             log.debug("Auto-start is disabled.")


    # --- Map Checking Logic ---
    def handle_map_change(self, map_id, map_title):
        log.info(f"Map changed to ID: {map_id}, Title: {map_title}")
        self.current_map_id = map_id
        self.current_map_title = map_title
        # self.last_valid_map_id = 0 # REMOVED: Don't clear last valid on attempt
        self.host_map_selected_valid = False # Reset flag on *any* map change

        # Reset host AFK timer as changing map is an action
        # *** MOVED: Timer reset now happens only on SUCCESSFUL map check or host change ***
        # if self.current_host:
        #     self.host_last_action_time = time.time()
        #     log.debug(f"Reset AFK timer for {self.current_host} due to map change.")

        # Check map if checker is enabled AND we know who the host is
        if self.config['map_checker']['enabled'] and self.current_host:
             # check_map will set host_map_selected_valid = True if it passes
             self.check_map(map_id, map_title)
        elif self.config['map_checker']['enabled'] and not self.current_host:
             log.warning("Map changed, but cannot check rules: No current host identified.")
        elif not self.config['map_checker']['enabled']:
            # If checker is disabled, treat any map change as a valid host action for AFK timer
            if self.current_host:
                self.host_last_action_time = time.time()
                log.debug(f"Reset AFK timer for {self.current_host} due to map change (Checker Disabled).")


    def check_map(self, map_id, map_title):
        """Fetches map info and checks against configured rules."""
        # Ensure checker is enabled and host is known
        if not self.config['map_checker']['enabled'] or not self.current_host:
             log.debug("Skipping map check (disabled or no host).")
             self.host_map_selected_valid = False
             # self.last_valid_map_id = 0 # Don't clear here
             # If checker disabled, the timer reset is handled in handle_map_change
             return

        # Reset valid flag before check
        self.host_map_selected_valid = False
        # self.last_valid_map_id = 0 # REMOVED: Don't clear last valid here

        log.info(f"Checking map {map_id} ('{map_title}') selected by {self.current_host}...")
        info = get_beatmap_info(map_id, self.api_client_id, self.api_client_secret)

        if info is None:
            self.reject_map(f"Could not get info for map ID {map_id}. It might not exist or API failed.", is_violation=False)
            return

        # ... (rest of the violation checks remain the same) ...
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

        # Status Check
        is_status_allowed = 'all' in [s.lower() for s in allowed_statuses] or status.lower() in [s.lower() for s in allowed_statuses]
        if not is_status_allowed: violations.append(f"Status '{status}' not allowed") # Shorten msg

        # Mode check
        is_mode_allowed = 'all' in [m.lower() for m in allowed_modes] or mode.lower() in [m.lower() for m in allowed_modes]
        if not is_mode_allowed: violations.append(f"Mode '{mode}' not allowed") # Shorten msg

        # Star Check
        min_stars = mc.get('min_stars', 0)
        max_stars = mc.get('max_stars', 0)
        if stars is not None:
            epsilon = 0.001
            if min_stars > 0 and stars < min_stars - epsilon: violations.append(f"Stars ({stars_str}) < Min ({min_stars:.2f}*)")
            if max_stars > 0 and stars > max_stars + epsilon: violations.append(f"Stars ({stars_str}) > Max ({max_stars:.2f}*)")
        elif min_stars > 0 or max_stars > 0: violations.append("Could not verify star rating")

        # Length Check
        min_len = mc.get('min_length_seconds', 0)
        max_len = mc.get('max_length_seconds', 0)
        if length is not None:
            if min_len > 0 and length < min_len: violations.append(f"Length ({length_str}) < Min ({self._format_time(min_len)})")
            if max_len > 0 and length > max_len: violations.append(f"Length ({length_str}) > Max ({self._format_time(max_len)})")
        elif min_len > 0 or max_len > 0: violations.append("Could not verify map length")

        if violations:
            reason = f"Map Rejected: {'; '.join(violations)}"
            log.warning(f"Map violation by {self.current_host}: {reason}")
            self.reject_map(reason, is_violation=True) # This counts as a violation
        else:
            # Map accepted
            self.host_map_selected_valid = True
            self.last_valid_map_id = map_id # *** STORE last valid map ID ***
            log.info(f"Map {map_id} accepted and marked as valid. ({stars_str}, {length_str}, {status}, {mode})")

            # *** Reset AFK timer on successful map pick ***
            if self.current_host:
                self.host_last_action_time = time.time()
                log.debug(f"Reset AFK timer for {self.current_host} due to VALID map selection.")

            messages = [ f"Map OK: {title} [{version}] ({stars_str}, {length_str}, {status}, {mode})" ]
            self.send_message(messages)

            # Reset violation count
            if self.current_host in self.map_violations and self.map_violations[self.current_host] > 0:
                 log.info(f"Resetting violations for {self.current_host} after valid pick.")
                 self.map_violations[self.current_host] = 0


    def reject_map(self, reason, is_violation=True):
        """Handles map rejection, sends messages, increments violation count, and attempts to revert map."""
        if not self.config['map_checker']['enabled'] or not self.current_host: return

        rejected_map_id = self.current_map_id
        rejected_map_title = self.current_map_title
        log.info(f"Rejecting map {rejected_map_id} ('{rejected_map_title}'). Reason: {reason}")

        self.host_map_selected_valid = False # Ensure flag is false

        # Send rejection message to chat (1 message)
        self.send_message(f"Map Check Failed: {reason}")

        # Clear rejected map state internally *before* potentially reverting
        self.current_map_id = 0
        self.current_map_title = ""

        # *** NEW: Attempt to revert to the last valid map ***
        revert_messages = []
        if self.last_valid_map_id != 0:
             log.info(f"Attempting to revert map to last valid ID: {self.last_valid_map_id}")
             revert_messages.append(f"!mp map {self.last_valid_map_id}")
             revert_messages.append(f"Reverting to the last valid map...")
             # Don't clear last_valid_map_id here yet, wait for Bancho confirmation or next change
        else:
             log.info("No previous valid map to revert to.")
             # Optionally clear host's map? '!mp clearhost' might be too drastic.
             # '!mp map 0' might work but can be confusing. Best to just leave it.
             pass

        # Send revert command (if any) after a slight delay from rejection msg
        if revert_messages:
             # Add a small delay so the rejection message appears first
             threading.Timer(0.7, self.send_message, args=[revert_messages]).start()


        # Only proceed with violation counting/skipping if it was a rule violation
        if not is_violation:
             log.debug("Map rejection was not due to rule violation. No violation counted.")
             return

        # --- Violation Handling ---
        violation_limit = self.config['map_checker'].get('violations_allowed', 3)
        if violation_limit <= 0: return # Violations disabled

        count = self.map_violations.get(self.current_host, 0) + 1
        self.map_violations[self.current_host] = count
        log.warning(f"{self.current_host} violation count: {count}/{violation_limit}")

        if count >= violation_limit:
            skip_message = f"Violation limit ({violation_limit}) reached for {self.current_host}. Skipping host."
            log.warning(f"Skipping host {self.current_host} due to map violations.")
            # Send skip message *after* potential revert attempt is scheduled
            self.send_message(skip_message)
            self.skip_current_host(f"Reached map violation limit ({violation_limit})")
        else:
            remaining = violation_limit - count
            warn_message = f"Map Violation ({count}/{violation_limit}) for {self.current_host}. {remaining} remaining. Use !rules."
             # Send warn message *after* potential revert attempt is scheduled
            self.send_message(warn_message)


    # --- Vote Skip Logic ---
    # Keep handle_vote_skip, get_votes_needed, clear_vote_skip,
    # clear_vote_skip_if_involved, check_vote_skip_timeout mostly as they were.
    # Ensure messages sent are concise (typically 1 per event).

    def handle_vote_skip(self, voter):
        """Processes a !voteskip command from a player."""
        vs_config = self.config['vote_skip']
        if not vs_config['enabled'] or not self.current_host: return

        target_host = self.current_host
        timeout = vs_config.get('timeout_seconds', 60)

        # Check timeout first if a vote is active
        if self.vote_skip_active and time.time() - self.vote_skip_start_time > timeout:
                 log.info(f"Vote skip for {self.vote_skip_target} expired.")
                 self.send_message(f"Vote to skip {self.vote_skip_target} failed (timeout).") # 1 msg
                 self.clear_vote_skip("timeout")
                 # Allow starting a new vote immediately below

        # Start a new vote?
        if not self.vote_skip_active:
            self.vote_skip_active = True
            self.vote_skip_target = target_host
            self.vote_skip_initiator = voter
            self.vote_skip_voters = {voter}
            self.vote_skip_start_time = time.time()
            needed = self.get_votes_needed()
            log.info(f"Vote skip initiated by '{voter}' for host '{target_host}'. Needs {needed} votes.")
            self.send_message(f"{voter} started vote skip for {target_host}! !voteskip to agree. ({len(self.vote_skip_voters)}/{needed})") # 1 msg

        # Add vote to existing poll?
        elif self.vote_skip_active and self.vote_skip_target == target_host:
            if voter in self.vote_skip_voters:
                log.debug(f"'{voter}' tried to vote skip again.")
                # self.send_message(f"You already voted, {voter}.") # Optional: too spammy?
                return

            self.vote_skip_voters.add(voter)
            needed = self.get_votes_needed()
            current_votes = len(self.vote_skip_voters)
            log.info(f"'{voter}' voted to skip '{target_host}'. Votes: {current_votes}/{needed}")

            if current_votes >= needed:
                log.info(f"Vote skip threshold reached for {target_host}. Skipping.")
                self.send_message(f"Vote skip passed! Skipping host {target_host}.") # 1 msg
                # skip_current_host calls clear_vote_skip and handles rotation/display
                self.skip_current_host(f"Skipped by player vote ({current_votes}/{needed} votes)")
            else:
                 # Just announce the vote count update
                 self.send_message(f"{voter} voted skip. ({current_votes}/{needed})") # 1 msg

        elif self.vote_skip_active and self.vote_skip_target != target_host:
             log.warning(f"'{voter}' tried !voteskip for '{target_host}', but active vote is for '{self.vote_skip_target}'. Clearing old.")
             self.clear_vote_skip("host changed mid-vote")
             # Maybe send message: "Host changed, please use !voteskip again if needed."?
             self.send_message(f"Host changed during vote. Vote for {self.vote_skip_target} cancelled.") # 1 msg

    def get_votes_needed(self):
        """Calculates the number of votes required to skip the host."""
        # ... (Keep get_votes_needed as it was) ...
        vs_config = self.config['vote_skip']
        threshold_type = vs_config.get('threshold_type', 'percentage')
        threshold_value = vs_config.get('threshold_value', 51)

        eligible_voters = len(self.players_in_lobby) - 1
        if eligible_voters < 1: return 1

        if threshold_type == 'percentage':
            needed = math.ceil(eligible_voters * (threshold_value / 100.0))
            return max(1, int(needed))
        elif threshold_type == 'fixed':
            needed = int(threshold_value)
            return max(1, min(needed, eligible_voters))
        else:
            log.warning(f"Invalid vote_skip threshold_type '{threshold_type}'. Defaulting to percentage.")
            needed = math.ceil(eligible_voters * (threshold_value / 100.0))
            return max(1, int(needed))

    def clear_vote_skip(self, reason=""):
        """Clears the current vote skip state."""
        # ... (Keep clear_vote_skip as it was) ...
        if self.vote_skip_active:
            log.info(f"Clearing active vote skip for '{self.vote_skip_target}'. Reason: {reason}")
            self.vote_skip_active = False
            self.vote_skip_target = None
            self.vote_skip_initiator = None
            self.vote_skip_voters.clear()
            self.vote_skip_start_time = 0

    def clear_vote_skip_if_involved(self, player_name, reason="player involved left/kicked"):
        """Clears vote skip if the player was target/initiator, or removes voter."""
        # ... (Keep clear_vote_skip_if_involved as it was, check messages are minimal) ...
        if self.vote_skip_active and (player_name == self.vote_skip_target or player_name == self.vote_skip_initiator):
             self.send_message(f"Vote skip cancelled ({reason}: {player_name}).") # Inform chat vote is dead
             self.clear_vote_skip(reason)
        elif self.vote_skip_active and player_name in self.vote_skip_voters:
             self.vote_skip_voters.remove(player_name)
             log.info(f"Removed leaving player '{player_name}' from vote skip voters. Remaining: {len(self.vote_skip_voters)}")
             # Check if threshold met now
             needed = self.get_votes_needed()
             current_votes = len(self.vote_skip_voters)
             # Only announce/skip if threshold met *after* removal
             if current_votes >= needed:
                  log.info(f"Vote skip threshold reached for {self.vote_skip_target} after voter left. Skipping.")
                  self.send_message(f"Vote skip passed after voter left! Skipping host {self.vote_skip_target}.") # 1 msg
                  self.skip_current_host(f"Skipped by player vote ({current_votes}/{needed} votes after voter left)")
             # Don't announce otherwise, wait for timeout or more votes


    def check_vote_skip_timeout(self):
        """Periodically checks if the active vote skip has timed out."""
        if not self.vote_skip_active: return

        vs_config = self.config['vote_skip']
        timeout = vs_config.get('timeout_seconds', 60)

        if time.time() - self.vote_skip_start_time > timeout:
            log.info(f"Vote skip for '{self.vote_skip_target}' timed out.")
            # Send message only if vote was active and is now timing out
            self.send_message(f"Vote to skip {self.vote_skip_target} failed (timeout).") # 1 msg
            self.clear_vote_skip("timeout")


    # --- AFK Host Handling ---
    # *** MODIFIED: Check host_map_selected_valid flag ***
    def check_afk_host(self):
        """Periodically checks if the current host is AFK and skips them if enabled."""
        afk_config = self.config['afk_handling']
        if not afk_config['enabled']: return
        if not self.current_host: return
        if self.is_matching: return # Don't check during a match

        # *** NEW: Check the flag ***
        if self.host_map_selected_valid:
             log.debug(f"AFK check for {self.current_host}: Skipped, host has selected a valid map.")
             return # Don't timeout if host selected valid map and is waiting

        timeout = afk_config.get('timeout_seconds', 30)
        if timeout <= 0: return # Disabled via timeout value

        time_since_last_action = time.time() - self.host_last_action_time
        log.debug(f"AFK check for {self.current_host}. Idle time: {time_since_last_action:.1f}s / {timeout}s (Valid map selected: {self.host_map_selected_valid})") # Added flag to log

        if time_since_last_action > timeout:
            log.warning(f"Host '{self.current_host}' exceeded AFK timeout ({timeout}s). Skipping.")
            self.send_message(f"Host {self.current_host} skipped due to inactivity ({timeout}s+).") # 1 msg
            self.skip_current_host(f"AFK timeout ({timeout}s)") # skip handles rotation/display


    # --- Utility Methods ---
    def send_message(self, message_or_list):
        # ... (Keep send_message largely as it was, the delay and truncation are helpful) ...
        # Ensure it handles lists properly by iterating and sending with delay.
        if not self.connection.is_connected() or not self.target_channel:
            log.warning(f"Cannot send, not connected/no channel: {message_or_list}")
            return

        messages = message_or_list if isinstance(message_or_list, list) else [message_or_list]
        # Keep delay reasonable, 0.4-0.6s seems okay for Bancho usually.
        # If you still get limited, you might need to increase this or send fewer messages overall.
        delay = 0.5

        for i, msg in enumerate(messages):
            if not msg: continue
            full_msg = str(msg)
            # Basic sanitization
            if not full_msg.startswith("!") and (full_msg.startswith("/") or full_msg.startswith(".")):
                 log.warning(f"Message starts with potentially unsafe character, prepending space: {full_msg[:20]}...")
                 full_msg = " " + full_msg

            max_len = 450 # Keep conservative max length
            if len(full_msg.encode('utf-8')) > max_len:
                log.warning(f"Truncating long message: {full_msg[:100]}...")
                encoded_msg = full_msg.encode('utf-8')
                while len(encoded_msg) > max_len:
                    encoded_msg = encoded_msg[:-1]
                try:
                    full_msg = encoded_msg.decode('utf-8', 'ignore') + "..."
                except Exception:
                    full_msg = full_msg[:max_len//4] + "..." # Very conservative fallback

            try:
                # Apply delay *before* sending the second message onwards
                if i > 0:
                    time.sleep(delay)
                log.info(f"SENDING to {self.target_channel}: {full_msg}")
                self.connection.privmsg(self.target_channel, full_msg)
                self.SentMessage.emit({'message': full_msg})
            except irc.client.ServerNotConnectedError:
                log.warning("Failed to send message: Disconnected.")
                self._request_shutdown()
                break
            except Exception as e:
                log.error(f"Failed to send message to {self.target_channel}: {e}", exc_info=True)
                time.sleep(1) # Pause briefly after error

    def _request_shutdown(self):
        # ... (Keep _request_shutdown as it was) ...
        global shutdown_requested
        if not shutdown_requested:
            log.info("Shutdown requested internally.")
            shutdown_requested = True

    # --- Admin Commands (Called from Console Input Thread) ---
    # Keep admin commands as they were, they don't directly interact with the chat much.
    def admin_skip_host(self, reason="Admin command"):
        # ... (Keep admin_skip_host as it was) ...
        if not self.connection.is_connected():
             log.error("Admin Skip Failed: Not connected.")
             return
        log.info(f"Admin skip initiated. Reason: {reason}")
        self.skip_current_host(reason)

    def admin_show_queue(self):
        # ... (Keep admin_show_queue as it was) ...
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
        # ... (Keep admin_show_status as it was, added valid map flag display) ...
        print("--- Bot Status (Console View) ---")
        print(f"Connected: {self.connection.is_connected()}")
        print(f"Channel: {self.target_channel}")
        print(f"Current Host: {self.current_host if self.current_host else 'None'}")
        print(f"Match in Progress: {self.is_matching}")
        print(f"Players in Lobby: {len(self.players_in_lobby)} {list(self.players_in_lobby)}")
        print("-" * 10)
        print(f"Rotation Enabled: {self.config['host_rotation']['enabled']}")
        print(f"Map Check Enabled: {self.config['map_checker']['enabled']}")
        print(f"Vote Skip Enabled: {self.config['vote_skip']['enabled']}")
        print(f"AFK Check Enabled: {self.config['afk_handling']['enabled']}")
        print(f"Auto Start Enabled: {self.config['auto_start']['enabled']}")
        print("-" * 10)
        if self.vote_skip_active:
             print(f"Vote Skip Active: Yes (Target: {self.vote_skip_target}, Voters: {len(self.vote_skip_voters)}/{self.get_votes_needed()}, Initiator: {self.vote_skip_initiator})")
        else:
             print("Vote Skip Active: No")
        print("-" * 10)
        print(f"Current Map ID: {self.current_map_id if self.current_map_id else 'None'} ({self.current_map_title})")
        print(f"Last Valid Map ID: {self.last_valid_map_id if self.last_valid_map_id else 'None'}")
        # *** NEW: Display valid map flag status ***
        print(f"Host Map Selected Valid (AFK Bypass): {self.host_map_selected_valid}")
        if self.config['map_checker']['enabled']:
            mc = self.config['map_checker']
            statuses = self.config.get('allowed_map_statuses', ['all'])
            modes = self.config.get('allowed_modes', ['all'])
            print(" Rules:")
            print(f"  Stars: {mc.get('min_stars',0):.2f}-{mc.get('max_stars',0):.2f}")
            print(f"  Len: {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}")
            print(f"  Status: {','.join(statuses)}")
            print(f"  Modes: {','.join(modes)}")
            print(f"  Violations: {mc.get('violations_allowed', 3)}")
        else:
             print(" Rules: (Map Check Disabled)")
        print("---------------------------------")

    # --- Shutdown ---
    def shutdown(self, message="Client shutting down."):
        # ... (Keep shutdown as it was) ...
        log.info("Initiating shutdown sequence...")
        conn_available = hasattr(self, 'connection') and self.connection and self.connection.is_connected()

        if conn_available and self.config.get("goodbye_message"):
            try:
                log.info(f"Sending goodbye message: '{self.config['goodbye_message']}'")
                # Use a direct send, bypassing queue/delay if shutting down
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
# Keep the load_or_generate_config function exactly as it was in the PROVIDED script.
# It seems you had corrected it already.
def load_or_generate_config(filepath):
    """Loads config from JSON file or generates a default one if not found.
       Prioritizes values from the existing file over defaults."""
    defaults = {
        "server": "irc.ppy.sh",
        "port": 6667,
        "username": "YourOsuUsername",
        "password": "YourOsuIRCPassword",
        "room_id": "", # Default is empty, user file might override
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
        "auto_start": {"enabled": False, "delay_seconds": 5}
    }

    # --- Recursive Update Logic (Corrected) ---
    def merge_configs(base, updates):
        """Recursively merges 'updates' onto 'base'. 'updates' values take precedence."""
        merged = base.copy() # Start with a copy of the base (defaults)
        for key, value in updates.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                # If both are dicts, recurse
                merged[key] = merge_configs(merged[key], value)
            else:
                # Otherwise, the update value overwrites or adds
                merged[key] = value
        return merged

    try:
        if not filepath.exists():
            log.warning(f"Config file '{filepath}' not found. Generating default config.")
            log.warning("IMPORTANT: Please edit the generated config.json with your osu! username, IRC password, and API credentials!")
            # NOTE: Don't include room_id in the default written file.
            config_to_write = defaults.copy()
            if 'room_id' in config_to_write:
                 del config_to_write['room_id'] # Don't write room_id to default file

            try:
                with filepath.open('w', encoding='utf-8') as f:
                    json.dump(config_to_write, f, indent=4, ensure_ascii=False)
                log.info(f"Default config file created at '{filepath}'. Please edit it and restart.")
                # Return the full defaults (including room_id='') for the current run
                return defaults
            except (IOError, PermissionError) as e:
                 log.critical(f"Could not write default config file: {e}")
                 sys.exit(1)

        else:
            log.info(f"Loading configuration from '{filepath}'...")
            with filepath.open('r', encoding='utf-8') as f:
                user_config = json.load(f)

            # Merge user config onto defaults, user values take precedence
            final_config = merge_configs(defaults, user_config)

            # --- Validation ---
            required = ["server", "port", "username", "password"]
            missing = [k for k in required if not final_config.get(k) or final_config[k] in ["", "YourOsuUsername", "YourOsuIRCPassword"]]
            if missing:
                log.warning(f"Missing or default required config keys: {', '.join(missing)}. Please check '{filepath}'.")

            # Type checks for critical fields
            if not isinstance(final_config.get("port"), int):
                log.error("'port' must be an integer. Using default 6667.")
                final_config["port"] = defaults["port"]
            if not isinstance(final_config.get("osu_api_client_id"), int):
                 log.error("'osu_api_client_id' must be an integer. Using default 0.")
                 final_config["osu_api_client_id"] = defaults["osu_api_client_id"]

            # Nested config validation
            for section in ["map_checker", "host_rotation", "vote_skip", "afk_handling", "auto_start"]:
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
            # Ensure threshold_value is treated correctly based on type
            if vs.get('threshold_type') == 'percentage':
                vs['threshold_value'] = float(vs.get('threshold_value', defaults['vote_skip']['threshold_value']))
            else: # fixed
                vs['threshold_value'] = int(vs.get('threshold_value', defaults['vote_skip']['threshold_value']))


            afk = final_config['afk_handling']
            afk['timeout_seconds'] = int(afk.get('timeout_seconds', defaults['afk_handling']['timeout_seconds']))

            auto_s = final_config['auto_start']
            auto_s['delay_seconds'] = int(auto_s.get('delay_seconds', defaults['auto_start']['delay_seconds']))

            log.info(f"Configuration loaded and validated successfully from '{filepath}'.")

            # Ensure room_id is treated as empty string if not found or invalid after load
            loaded_room_id = str(final_config.get('room_id', "")).strip()
            if not loaded_room_id.isdigit():
                 final_config['room_id'] = "" # Set to empty if invalid/missing in file
            else:
                 final_config['room_id'] = loaded_room_id # Use the valid one from file

            return final_config

    except (json.JSONDecodeError, TypeError) as e:
        log.critical(f"Error parsing config file '{filepath}': {e}. Please check its format.")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Unexpected error loading config: {e}", exc_info=True)
        sys.exit(1)

# --- Signal Handling ---
def signal_handler(sig, frame):
    # ... (Keep signal_handler as it was) ...
    global shutdown_requested
    if not shutdown_requested:
        log.info(f"Shutdown signal ({signal.Signals(sig).name}) received. Stopping gracefully...")
        shutdown_requested = True
    else:
        log.warning("Shutdown already in progress.")


# --- Console Input Thread ---
# Keep the console_input_loop exactly as it was in the PROVIDED script.
# The version you provided already handles saving correctly and has the necessary commands.
def console_input_loop(bot_instance, original_room_id_from_file): # Renamed param for clarity
    """Handles admin commands entered in the console."""
    global shutdown_requested # Ensure access to global flag
    log.info("Console input thread started. Type 'help' for commands.")
    while not shutdown_requested:
        try:
            time.sleep(0.1) # Prevent tight loop on error/EOF
            command_line = input("ADMIN > ").strip()
            if not command_line: continue

            parts = shlex.split(command_line)
            command = parts[0].lower()
            args = parts[1:]

            # --- Function to prepare config for saving ---
            def prepare_config_for_save(current_config):
                """Creates a deep copy and ensures the file's original room_id is used."""
                config_to_save = copy.deepcopy(current_config)
                # Always revert room_id to the one loaded from file before saving
                config_to_save['room_id'] = original_room_id_from_file
                return config_to_save

            # --- Bot Control ---
            if command in ["quit", "exit", "stop"]:
                log.info("Console requested quit.")
                shutdown_requested = True
                break

            elif command == "skip":
                reason = " ".join(args) if args else "Admin command"
                bot_instance.admin_skip_host(reason)

            elif command in ["queue", "q", "showqueue"]:
                bot_instance.admin_show_queue()

            elif command in ["status", "info", "showstatus"]:
                 bot_instance.admin_show_status()

            # --- Admin Lobby Settings (These DON'T save config) ---
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
                     # Assume MAX_LOBBY_SIZE is defined elsewhere (e.g., 16)
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


            # --- Admin Bot Settings Modification (These trigger config saving) ---
            else:
                 config_changed = False # Flag specific to this command processing block

                 # Use 'elif' for all setting commands
                 if command == "set_rotation":
                     if not args or args[0].lower() not in ['true', 'false', 'on', 'off']: print("Usage: set_rotation <true|false>"); continue
                     hr_config = bot_instance.config['host_rotation']
                     value = args[0].lower() in ['true', 'on']
                     if hr_config['enabled'] != value:
                         hr_config['enabled'] = value
                         print(f"Admin set Host Rotation to: {value}")
                         config_changed = True # Mark change
                         # Announce AFTER potential save
                         # bot_instance.announce_setting_change("Host Rotation", value)
                         # Logic adjustments... (as before)
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
                         # Check API keys before enabling
                         if value and (not bot_instance.api_client_id or bot_instance.api_client_secret == 'YOUR_CLIENT_SECRET'):
                              print("Cannot enable map check: API credentials missing/invalid in config.")
                              log.warning("Admin attempted to enable map check without valid API keys.")
                              continue # Skip saving/announcing if check fails
                         mc_config['enabled'] = value
                         print(f"Admin set Map Checker to: {value}")
                         config_changed = True
                         # Announce AFTER potential save
                         # bot_instance.announce_setting_change("Map Checker", value)
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
                         # Announce AFTER potential save
                         # bot_instance.announce_setting_change("Auto Start", value)
                     else:
                         print(f"Auto Start already set to {value}.")

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
                             # Announce AFTER potential save
                             # bot_instance.announce_setting_change("Auto Start Delay", f"{value}s")
                         else:
                             print(f"Auto Start Delay already set to {value} seconds.")
                     except ValueError:
                         print("Invalid number for delay seconds.")

                 # --- Map Rule Settings ---
                 elif command == "set_star_min":
                    if not args: print("Usage: set_star_min <number|0>"); continue
                    try:
                        value = float(args[0])
                        if value < 0: print("Min stars cannot be negative."); continue
                        current_value = bot_instance.config['map_checker'].get('min_stars', 0)
                        if abs(current_value - value) > 0.001: # Float comparison
                            bot_instance.config['map_checker']['min_stars'] = value
                            print(f"Admin set Minimum Star Rating to: {value:.2f}*")
                            config_changed = True
                            # Announce AFTER potential save
                            # bot_instance.announce_setting_change("Min Stars", f"{value:.2f}*")
                        else:
                            print(f"Minimum Star Rating already set to {value:.2f}*")
                    except ValueError: print("Invalid number for minimum stars.")

                 elif command == "set_star_max":
                     if not args: print("Usage: set_star_max <number|0>"); continue
                     try:
                         value = float(args[0])
                         if value < 0: print("Max stars cannot be negative."); continue
                         current_value = bot_instance.config['map_checker'].get('max_stars', 0)
                         if abs(current_value - value) > 0.001: # Float comparison
                             bot_instance.config['map_checker']['max_stars'] = value
                             print(f"Admin set Maximum Star Rating to: {value:.2f}*")
                             config_changed = True
                             # Announce AFTER potential save
                             # bot_instance.announce_setting_change("Max Stars", f"{value:.2f}*")
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
                             # Announce AFTER potential save
                             # bot_instance.announce_setting_change("Min Length", formatted_time)
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
                             # Announce AFTER potential save
                             # bot_instance.announce_setting_change("Max Length", formatted_time)
                         else:
                              print(f"Maximum Map Length already set to {bot_instance._format_time(value)}")
                     except ValueError: print("Invalid number for maximum length seconds.")

                 elif command == "set_statuses":
                     # Assume OSU_STATUSES is defined globally
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
                         # Announce AFTER potential save
                         # bot_instance.announce_setting_change("Allowed Statuses", display_value)
                     else: print(f"Allowed Map Statuses already set to: {', '.join(value)}")

                 elif command == "set_modes":
                     # Assume OSU_MODES is defined globally
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
                         # Announce AFTER potential save
                         # bot_instance.announce_setting_change("Allowed Modes", display_value)
                     else: print(f"Allowed Game Modes already set to: {', '.join(value)}")

                 # --- Help Command ---
                 elif command == "help":
                    print("--- Admin Console Commands ---")
                    print(" Bot Control:")
                    print("  quit / exit / stop  - Stop the bot gracefully.")
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
                    # Add toggles for voteskip/afk if needed
                    print(" Map Rules (Requires Map Check Enabled):")
                    print("  set_star_min <N>    - Set min star rating (0=off). Ex: set_star_min 4.5")
                    print("  set_star_max <N>    - Set max star rating (0=off). Ex: set_star_max 6.0")
                    print("  set_min_len <sec>   - Set min map length seconds (0=off). Ex: set_min_len 90")
                    print("  set_max_len <sec>   - Set max map length seconds (0=off). Ex: set_max_len 300")
                    print("  set_statuses <...>  - Set allowed map statuses (ranked, loved, etc. or 'all').")
                    print("  set_modes <...>     - Set allowed game modes (osu, mania, etc. or 'all').")
                    print(" Other Settings:")
                    print("  set_auto_start_delay <sec> - Set delay (seconds) for auto start (min 1).")
                    # Add commands for vote skip/afk settings if needed
                    print(" Other:")
                    print("  help                - Show this help message.")
                    print("----------------------------")
                    print("* Bot settings (rotation, map rules, etc.) are saved to config.json immediately.")
                    print("* Lobby settings (password, size, name) use !mp commands and are not saved by the bot.")

                 # --- Unknown Command Handling ---
                 else:
                    print(f"Unknown command: '{command}'. Type 'help' for options.")


                 # --- Save Config if Changed ---
                 if config_changed:
                     config_to_save = prepare_config_for_save(bot_instance.config)
                     setting_name_for_announce = command.replace('set_', '').replace('_',' ').title() # Simple derive name
                     value_for_announce = args[0] if args else 'N/A' # Simple derive value
                     if save_config(config_to_save):
                         print("Configuration changes saved to file.")
                         # Announce the change *after* successful save
                         bot_instance.announce_setting_change(setting_name_for_announce, value_for_announce)
                     else:
                         print("ERROR: Failed to save configuration changes to file.")
                         log.error("Failed to save configuration changes to config.json after admin command.")


        except EOFError:
            log.info("Console input closed (EOF). Requesting shutdown.")
            shutdown_requested = True
            break
        except KeyboardInterrupt:
            if not shutdown_requested:
                 log.info("Console KeyboardInterrupt. Requesting shutdown.")
                 shutdown_requested = True
            break
        except Exception as e:
            log.error(f"Error in console input loop: {e}", exc_info=True)
            print(f"An error occurred processing the command: {e}")

    log.info("Console input thread finished.")


# --- Main Execution ---
# Keep the main function exactly as it was in the PROVIDED script.
# The version you provided already handles room ID prompting and uses the updated console loop.
def main():
    global shutdown_requested # Ensure access to global flag

    # Load config
    config = load_or_generate_config(CONFIG_FILE)
    initial_room_id_from_file = config.get('room_id', "") # Store original value

    # --- Ask for Room ID if needed ---
    current_room_id = str(config.get('room_id', '')).strip()
    if not current_room_id.isdigit():
        log.info("Room ID not found or invalid in config file.")
        while True:
            try:
                room_id_input = input("Enter the osu! multiplayer room ID (numeric part only): ").strip()
                if room_id_input.isdigit():
                    config['room_id'] = room_id_input # Update for this session
                    log.info(f"Using Room ID for this session: {config['room_id']}")
                    # Do NOT save config here.
                    break
                else:
                    print("Invalid input. Please enter only the numbers from the room link.")
            except EOFError: log.critical("Input closed. Exiting."); sys.exit(1)
            except KeyboardInterrupt: log.info("\nCancelled. Exiting."); sys.exit(0)
    else:
         log.info(f"Using Room ID from config: {config['room_id']}")

    # --- Initialize Bot ---
    bot = None
    try:
        bot = OsuRoomBot(config)
        # Store original file room_id in bot instance for console loop saving logic
        # bot.initial_room_id = initial_room_id_from_file # The console loop gets this directly now
    except Exception as e:
        log.critical(f"Failed to initialize OsuRoomBot: {e}", exc_info=True)
        sys.exit(1)

    # --- Setup Signal Handlers ---
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # --- Connect to IRC ---
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
        sys.exit(1)
    except Exception as e:
        log.critical(f"Unexpected error during bot.connect: {e}", exc_info=True)
        sys.exit(1)

    # --- Start Console Input Thread ---
    # Pass original room ID for saving logic
    console_thread = threading.Thread(target=console_input_loop, args=(bot, initial_room_id_from_file), daemon=True, name="AdminConsoleThread")
    console_thread.start()

    # --- Main Loop (IRC Processing + Periodic Checks) ---
    log.info("Starting main processing loop (Use console for admin commands, Ctrl+C or 'quit' command to exit).")
    last_periodic_check = 0
    check_interval = 5 # Seconds between AFK/Vote Timeout checks

    while not shutdown_requested:
        try:
            # Process IRC events
            if hasattr(bot, 'reactor') and bot.reactor:
                bot.reactor.process_once(timeout=0.2)
            else: # Fallback if reactor is missing/dies
                 if not (hasattr(bot, 'connection') and bot.connection and bot.connection.is_connected()):
                     if not shutdown_requested:
                        log.warning("Connection lost and reactor unavailable. Requesting shutdown.")
                        shutdown_requested = True
                 time.sleep(0.2)

            # Check connection status
            if bot.connection_registered and not bot.connection.is_connected():
                 if not shutdown_requested:
                      log.warning("Connection lost unexpectedly after being registered. Requesting shutdown.")
                      shutdown_requested = True
                 continue

            # Perform periodic checks if connected
            if bot.connection_registered and bot.connection.is_connected():
                now = time.time()
                if now - last_periodic_check > check_interval:
                    log.debug("Running periodic checks (AFK, Vote Timeout)...")
                    bot.check_afk_host()
                    bot.check_vote_skip_timeout()
                    last_periodic_check = now

        except irc.client.ServerNotConnectedError:
            if not shutdown_requested:
                 log.warning("Disconnected during processing loop. Requesting shutdown.")
                 shutdown_requested = True
        except KeyboardInterrupt: # Catch Ctrl+C in main loop
             if not shutdown_requested:
                 log.info("Main loop KeyboardInterrupt. Requesting shutdown.")
                 shutdown_requested = True
        except Exception as e:
            log.error(f"Unhandled exception in main loop: {e}", exc_info=True)
            time.sleep(2) # Avoid busy-waiting

    # --- Shutdown Sequence ---
    log.info("Main loop exited. Initiating shutdown...")
    if bot:
        bot.shutdown("Client shutting down normally.")

    log.info("Bot finished.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
         log.info(f"Program exited with code {e.code}.")
         sys.exit(e.code)
    except KeyboardInterrupt:
         log.info("\nMain execution interrupted by Ctrl+C. Shutting down.")
         # Signal handler should set shutdown_requested, allowing graceful exit
         time.sleep(1) # Give a moment for shutdown sequence
    except Exception as e:
        log.critical(f"Critical error during main execution: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logging.shutdown()