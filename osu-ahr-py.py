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
import shlex # For safer command parsing

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

# --- IRC Bot Class ---
class OsuRoomBot(irc.client.SimpleIRCClient):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.target_channel = f"#mp_{config['room_id']}"
        self.connection_registered = False
        self.is_matching = False

        # Host Rotation (Status from config, no chat command to change)
        self.host_queue = deque()
        self.current_host = None
        self.last_host = None

        # Map Checker (Status from config, rules changed by admin console)
        self.api_client_id = self.config.get('osu_api_client_id', 0)
        self.api_client_secret = self.config.get('osu_api_client_secret', '')
        self.current_map_id = 0
        self.current_map_title = ""
        self.last_valid_map_id = 0
        self.map_violations = {}

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

        log.info(f"Bot initialized for {self.target_channel}. Host Rotation: {self.config['host_rotation']['enabled']}, Map Checker: {self.config['map_checker']['enabled']}")
        if self.config['map_checker']['enabled']:
            self.log_map_rules()

    def announce_setting_change(self, setting_name, new_value):
        """Sends a notification to the chat when an admin changes a setting."""
        if not self.connection.is_connected():
            log.warning("Cannot announce setting change, not connected.")
            return

        message = f"Admin updated setting: {setting_name} set to {new_value}"
        log.info(f"Announcing to chat: {message}")
        self.send_message(message)

        # Also log the full rules to console for completeness after any change
        self.log_map_rules()
        
    def log_map_rules(self):
        """Logs the current map checking rules to the console."""
        # Don't send this to chat unless requested by !help maybe? Keep logs clean.
        if not self.config['map_checker']['enabled']:
            log.info("Map checker is disabled.")
            return
        mc = self.config['map_checker']
        statuses = self.config.get('allowed_map_statuses', ['all'])
        modes = self.config.get('allowed_modes', ['all'])

        log.info(f"Map Rules: Stars {mc.get('min_stars', 'N/A')}-{mc.get('max_stars', 'N/A')}, "
                 f"Len {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}, "
                 f"Status: {', '.join(statuses)}, Modes: {', '.join(modes)}")
        # Log rotation/map_check status here too for clarity
        hr_enabled = self.config.get('host_rotation', {}).get('enabled', False)
        log.info(f"Settings: Rotation: {hr_enabled}, Map Check: {mc.get('enabled', False)}")


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
        try:
            connection.set_keepalive(60)
            log.info(f"Joining channel: {self.target_channel}")
            connection.join(self.target_channel)
            # Delay slightly to allow time for join confirmation before sending settings
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
            self.send_message("!mp settings")
        else:
            log.warning("Cannot request !mp settings, disconnected.")


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
                self.send_message(self.config["welcome_message"])
            # !mp settings requested in on_welcome via timer now
            self.JoinedLobby.emit({'channel': channel})
        elif nick == connection.get_nickname():
            log.info(f"Joined other channel: {channel}")

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

        if sender != connection.get_nickname():
            log.info(f"[{channel}] <{sender}> {message}")

        if sender == "BanchoBot":
            self.parse_bancho_message(message)
        else:
            # Handle user commands (!skip, !queue, !help)
            self.parse_user_command(sender, message)

    # --- User Command Parsing (Restricted) ---
    def parse_user_command(self, sender, message):
        """Parses commands sent by users in the channel (!skip, !queue, !help only)."""
        if not message.startswith("!"): return

        parts = shlex.split(message)
        if not parts: return
        command = parts[0].lower()
        # args = parts[1:] # Not currently needed for these simple commands

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

        # Command available only to current host (and if rotation is enabled)
        if command == '!skip':
            if self.config['host_rotation']['enabled']:
                if sender == self.current_host:
                    log.info(f"Host {sender} used !skip.")
                    self.skip_current_host("Host self-skipped")
                else:
                    # Inform non-hosts trying to skip
                    log.info(f"{sender} tried to use !skip (not host).")
                    self.send_message(f"Only the current host ({self.current_host}) can use !skip.")
            else:
                # Inform if rotation is off
                log.info(f"{sender} tried to use !skip (rotation disabled).")
                self.send_message("Host rotation is disabled, !skip command is inactive.")
            return

        # Ignore any other commands starting with ! from users/hosts
        log.debug(f"Ignoring unknown/restricted command '{command}' from {sender}.")


    def display_help_message(self):
        """Sends help information to the chat."""
        messages = [
            "--- Bot Help ---",
            "!queue : Shows the current host order (if rotation is enabled).",
            "!skip : (Host Only) Skips your turn as host.",
            "!help : Shows this message.",
        ]
        # Optionally add info about current rules if map checker is on
        if self.config['map_checker']['enabled']:
             mc = self.config['map_checker']
             statuses = self.config.get('allowed_map_statuses', ['all'])
             modes = self.config.get('allowed_modes', ['all'])
             messages.append("--- Current Map Rules ---")
             messages.append(f"Stars: {mc.get('min_stars', 'N/A')}-{mc.get('max_stars', 'N/A')}")
             messages.append(f"Length: {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}")
             messages.append(f"Status: {', '.join(statuses)}")
             messages.append(f"Modes: {', '.join(modes)}")
        messages.append("--- End Help ---")
        self.send_message(messages)


    # handle_settings_command FUNCTION IS REMOVED - No host settings commands


    # --- BanchoBot Message Parsing ---
    def parse_bancho_message(self, msg):
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
                map_id_match = re.search(r"/b/(\d+)", msg)
                map_title_match = re.match(r"Beatmap changed to: (.*?)\s*\(https://osu\.ppy\.sh/[bs]/\d+\)", msg)
                if map_id_match:
                    map_id = int(map_id_match.group(1))
                    title = map_title_match.group(1).strip() if map_title_match else "Unknown Title"
                    self.handle_map_change(map_id, title)
                else: log.warning(f"Could not parse map ID from change msg: {msg}")
            elif msg == "The match has started!": self.handle_match_start()
            elif msg == "The match has finished!": self.handle_match_finish()
            elif msg == "All players are ready": self.handle_all_players_ready()
            elif msg.startswith("Room name:") or msg.startswith("History is "): pass
            elif msg.startswith("Beatmap: "): self._parse_initial_beatmap(msg)
            elif msg.startswith("Slot "): self._parse_slot_message(msg)
            elif msg.startswith("Team mode:"): pass # Ignore settings lines
            elif msg.startswith("Active mods:"): pass
            elif msg.startswith("Players:"): pass
            # Add other Bancho messages if needed
        except Exception as e:
            log.error(f"Error parsing Bancho msg: '{msg}' - {e}", exc_info=True)

    def _parse_initial_beatmap(self, msg):
        map_id_match = re.search(r"/(?:b|beatmaps)/(\d+)", msg)
        map_title_match = re.match(r"Beatmap: https://.*?\s+(.+?)(?:\s+\[.+\])?$", msg)
        if map_id_match:
            self.current_map_id = int(map_id_match.group(1))
            self.current_map_title = map_title_match.group(1).strip() if map_title_match else "Unknown Title (from settings)"
            log.info(f"Initial map set from settings: ID {self.current_map_id}, Title: {self.current_map_title}")
        else:
            log.warning(f"Could not parse initial beatmap msg: {msg}")


    def _parse_slot_message(self, msg):
        # This needs to run even if rotation is disabled to identify the host
        match = re.match(r"Slot (\d+)\s+.*?\s+https://osu\.ppy\.sh/u/\d+\s+([^\s].*?)(?:\s+\[(Host.*?)\])?$", msg)
        if not match:
            log.debug(f"No player match in slot msg: {msg}")
            return

        slot, player_name, tags = match.groups()
        player_name = player_name.strip()
        is_host = tags and "Host" in tags

        if player_name and player_name.lower() != "banchobot":
            # Always identify host from settings
            if is_host:
                # Avoid triggering host change logic if host hasn't actually changed
                if self.current_host != player_name:
                    log.info(f"Identified host from settings: {player_name}")
                    self.current_host = player_name # Set directly here, handle_host_change is for Bancho messages
                    # Reset violations if needed (might already be 0)
                    if self.config['map_checker']['enabled']:
                         self.map_violations.setdefault(player_name, 0)
                         if self.map_violations[player_name] != 0:
                             log.info(f"Reset map violations for new host '{player_name}' identified from settings.")
                             self.map_violations[player_name] = 0

            # Only add to queue if rotation is enabled
            if self.config['host_rotation']['enabled'] and player_name not in self.host_queue:
                self.host_queue.append(player_name)
                log.info(f"Added '{player_name}' to queue from settings.")


    def initialize_lobby_state(self):
        """Finalizes host queue (if enabled) and checks initial map after !mp settings."""
        hr_enabled = self.config['host_rotation']['enabled']
        log.info(f"Finalizing initial state. Rotation Enabled: {hr_enabled}. Queue: {list(self.host_queue)}, Identified Host: {self.current_host}")

        if hr_enabled:
            if not self.host_queue:
                log.warning("Rotation enabled, but no players found in !mp settings to initialize host queue.")
                # Bot will just wait for players to join now
            elif self.current_host and self.current_host in self.host_queue:
                 # Ensure identified host is at the front if rotation is on
                 if self.host_queue[0] != self.current_host:
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
                              # Don't send !mp host, rely on Bancho's state or user action
                          else:
                               self.current_host = None # No host if queue is empty
            elif self.host_queue: # Queue has players, but no host identified yet
                self.current_host = self.host_queue[0] # Assume first is host
                log.info(f"No host identified, assuming first player is host: {self.current_host}")
                # Don't send !mp host, rely on Bancho state

            if self.current_host:
                 log.info(f"Host rotation initialized. Current Host: {self.current_host}, Queue: {list(self.host_queue)}")
                 self.display_host_queue() # Show initial queue if rotation on
            else:
                 log.info("Host rotation enabled, but no host could be determined yet.")

        # Check initial map regardless of rotation, if checker enabled and host known
        if self.current_map_id != 0 and self.config['map_checker']['enabled'] and self.current_host:
            log.info(f"Checking initial map ID {self.current_map_id} for host {self.current_host}.")
            self.check_map(self.current_map_id, self.current_map_title)
        elif self.config['map_checker']['enabled']:
            self.last_valid_map_id = 0 # Ensure no stale map ID initially


    # --- Host Rotation Logic ---
    def handle_player_join(self, player_name):
        self.PlayerJoined.emit({'player': player_name})
        if not self.config['host_rotation']['enabled'] or not player_name or player_name.lower() == "banchobot":
            return

        if player_name not in self.host_queue:
            self.host_queue.append(player_name)
            log.info(f"'{player_name}' joined, added to queue. Queue: {list(self.host_queue)}")
            # If queue was empty and no host, this player might become host
            if len(self.host_queue) == 1 and not self.current_host:
                 log.info(f"First player '{player_name}' joined, assuming host role.")
                 self.current_host = player_name
                 # Reset violations for the new host
                 if self.config['map_checker']['enabled']:
                     self.map_violations[player_name] = 0
            self.display_host_queue()
        else:
            log.info(f"'{player_name}' joined, but already in queue.")


    def handle_player_left(self, player_name):
        self.PlayerLeft.emit({'player': player_name})
        if not player_name: return
        was_host = (player_name == self.current_host)

        hr_enabled = self.config['host_rotation']['enabled']
        if hr_enabled and player_name in self.host_queue:
            try:
                self.host_queue.remove(player_name)
                log.info(f"'{player_name}' left, removed from queue. Queue: {list(self.host_queue)}")
                self.display_host_queue() # Update chat view
            except ValueError:
                log.warning(f"'{player_name}' left but not found in queue for removal?")

        if player_name in self.map_violations:
            del self.map_violations[player_name]
            log.debug(f"Removed violation count for leaving player '{player_name}'.")

        if was_host:
            log.info(f"Host '{player_name}' left.")
            self.current_host = None
            if hr_enabled and not self.is_matching:
                log.info("Host left outside match, setting next host.")
                self.set_next_host() # Assign next available player if rotation on


    def handle_host_change(self, player_name):
        """Handles Bancho reporting a host change (updates state, resets violations)."""
        if not player_name: return
        log.info(f"Bancho reported host changed to: {player_name}")
        previous_host = self.current_host
        self.current_host = player_name
        self.HostChanged.emit({'player': player_name, 'previous': previous_host})

        if self.config['map_checker']['enabled']:
            self.map_violations.setdefault(player_name, 0)
            if self.map_violations[player_name] != 0:
                 log.info(f"Reset map violations for new host '{player_name}'.")
                 self.map_violations[player_name] = 0

        hr_enabled = self.config['host_rotation']['enabled']
        if hr_enabled and self.host_queue:
             if self.host_queue[0] != player_name:
                 log.warning(f"Host changed to {player_name}, but they weren't front of queue ({self.host_queue[0] if self.host_queue else 'N/A'}). Reordering queue.")
                 try:
                     self.host_queue.remove(player_name)
                 except ValueError: pass
                 self.host_queue.insert(0, player_name)
                 self.display_host_queue() # Show updated order


    def rotate_and_set_host(self):
        """Rotates the queue and sets the new host if rotation is enabled."""
        hr_enabled = self.config['host_rotation']['enabled']
        if not hr_enabled or len(self.host_queue) < 1:
            log.debug(f"Skipping host rotation (enabled={hr_enabled}, queue_size={len(self.host_queue)}).")
            # If rotation disabled, host remains whoever Bancho says it is.
            return

        if len(self.host_queue) > 1:
            # Move the host who just finished (last_host) or the current front to the back
            player_to_move = self.last_host if self.last_host in self.host_queue else self.host_queue[0]
            try:
                self.host_queue.remove(player_to_move)
                self.host_queue.append(player_to_move)
                log.info(f"Rotated queue, moved '{player_to_move}' to the back.")
            except ValueError:
                log.warning(f"Player '{player_to_move}' to rotate not found? Rotating first element.")
                if len(self.host_queue) > 0:
                     first = self.host_queue.popleft()
                     self.host_queue.append(first)
                     log.info(f"Rotated queue (fallback), moved '{first}' to the back.")
        else:
            log.info("Only one player in queue, no rotation needed.")

        log.info(f"Queue after rotation: {list(self.host_queue)}")
        self.last_host = None
        self.set_next_host()
        self.display_host_queue()


    def set_next_host(self):
        """Sets the player at the front of the queue as the host via !mp host."""
        if not self.config['host_rotation']['enabled']: return

        if self.host_queue:
            next_host = self.host_queue[0]
            if next_host != self.current_host:
                log.info(f"Setting next host to '{next_host}' via !mp host...")
                self.send_message(f"!mp host {next_host}")
                # self.current_host will be updated by handle_host_change when Bancho confirms
            else:
                log.info(f"'{next_host}' is already the host (or expected to be based on queue).")
        else:
            log.warning("Host queue is empty, cannot set next host.")
            # Don't clear host automatically, maybe admin needs to assign manually


    def skip_current_host(self, reason=""):
        """Skips the current host (triggered by !skip command or admin console)."""
        if not self.config['host_rotation']['enabled']:
             log.warning("Attempted to skip host, but rotation is disabled.")
             # Maybe inform user if triggered by chat command? (Handled in parse_user_command)
             return

        if not self.current_host:
            log.warning("Attempted to skip host, but no host is currently assigned.")
            if "self-skipped" not in reason: # Don't spam chat if it was admin command
                 self.send_message("Cannot skip: No current host assigned.")
            return

        skipped_host = self.current_host
        log.info(f"Skipping host '{skipped_host}'. Reason: {reason}. Queue: {list(self.host_queue)}")
        messages = [f"Host Skipped: {skipped_host}"]
        if reason and "self-skipped" not in reason: messages.append(f"Reason: {reason}")
        self.send_message(messages)

        self.last_host = skipped_host
        # Rotate queue and set the *next* person as host
        self.rotate_and_set_host()


    def display_host_queue(self):
        """Sends the current host queue to the chat if rotation is enabled."""
        if not self.config['host_rotation']['enabled']:
            # Don't send anything if rotation is off, !queue handles the message
            return

        if not self.host_queue:
            self.send_message("Host queue is empty.")
            return

        queue_list = list(self.host_queue)
        messages = ["Current Host Queue:"]
        current_idx = -1
        next_idx = 0 if queue_list else -1

        if self.current_host and self.current_host in queue_list:
            try:
                current_idx = queue_list.index(self.current_host)
                if current_idx == 0 and len(queue_list) > 1:
                    next_idx = 1
            except ValueError:
                 current_idx = -1
                 next_idx = 0 # First person is next if host isn't in queue

        for i, player in enumerate(queue_list):
            prefix = ""
            if i == current_idx: prefix = "(Current Host)"
            elif i == next_idx and i != current_idx : prefix = "(Next Host)"
            messages.append(f"{i+1}. {player} {prefix}".strip())

        self.send_message(messages)


    # --- Match State Handling ---
    def handle_match_start(self):
        log.info(f"Match started with map ID {self.current_map_id}.")
        self.is_matching = True
        self.last_host = None
        self.MatchStarted.emit({'map_id': self.current_map_id})

    def handle_match_finish(self):
        log.info("Match finished.")
        self.is_matching = False
        self.last_host = self.current_host # Mark who just played for rotation logic
        self.MatchFinished.emit({})
        self.current_map_id = 0
        self.current_map_title = ""
        # Rotate host after a delay if enabled
        if self.config['host_rotation']['enabled']:
            log.info("Scheduling host rotation after match finish.")
            threading.Timer(1.5, self.rotate_and_set_host).start()

    def handle_all_players_ready(self):
        log.info("All players are ready.")
        # Add auto-start config option? For now, just check map validity.
        if self.config['map_checker']['enabled'] and not self.is_matching:
            if self.current_map_id == 0:
                log.warning("Ready state, but no map selected.")
            elif self.last_valid_map_id != self.current_map_id:
                log.warning(f"Ready state, but current map {self.current_map_id} failed validation or wasn't checked.")
            else:
                log.info("Ready state with validated map.")
                # Example: Auto-start if map is valid
                self.send_message("!mp start 5")


    # --- Map Checking Logic ---
    def handle_map_change(self, map_id, map_title):
        log.info(f"Map changed to ID: {map_id}, Title: {map_title}")
        self.current_map_id = map_id
        self.current_map_title = map_title
        self.last_valid_map_id = 0

        # Check map if checker is enabled AND we know who the host is
        if self.config['map_checker']['enabled'] and self.current_host:
             self.check_map(map_id, map_title)


    def check_map(self, map_id, map_title):
        """Fetches map info and checks against configured rules (admin-set)."""
        mc_enabled = self.config['map_checker']['enabled']
        # Need current_host to attribute violations correctly
        if not mc_enabled or not self.current_host:
             log.debug("Skipping map check (disabled or no host).")
             if not mc_enabled: self.last_valid_map_id = map_id
             return

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

        # 1. Check Status
        if 'all' not in allowed_statuses and status not in allowed_statuses:
            violations.append(f"Status '{status}' not allowed (Allowed: {', '.join(allowed_statuses)})")

        # 2. Check Mode
        if 'all' not in allowed_modes and mode not in allowed_modes:
             violations.append(f"Mode '{mode}' not allowed (Allowed: {', '.join(allowed_modes)})")

        # 3. Check Stars
        if stars is not None:
            min_stars = mc.get('min_stars', 0)
            max_stars = mc.get('max_stars', 0)
            if min_stars > 0 and stars < min_stars: violations.append(f"Stars ({stars_str}) < Min ({min_stars:.2f}*)")
            if max_stars > 0 and stars > max_stars: violations.append(f"Stars ({stars_str}) > Max ({max_stars:.2f}*)")
        elif mc.get('min_stars', 0) > 0 or mc.get('max_stars', 0) > 0:
             violations.append("Could not verify star rating")

        # 4. Check Length (min/max only)
        if length is not None:
            min_len = mc.get('min_length_seconds', 0)
            max_len = mc.get('max_length_seconds', 0)
            if min_len > 0 and length < min_len: violations.append(f"Length ({length_str}) < Min ({self._format_time(min_len)})")
            if max_len > 0 and length > max_len: violations.append(f"Length ({length_str}) > Max ({self._format_time(max_len)})")
        elif mc.get('min_length_seconds', 0) > 0 or mc.get('max_length_seconds', 0) > 0:
             violations.append("Could not verify map length")


        if violations:
            reason = f"Map Rejected: {'; '.join(violations)}"
            log.warning(f"Map violation by {self.current_host}: {reason}")
            self.reject_map(reason, is_violation=True)
        else:
            messages = [
                f"Map Check: {title} [{version}]",
                f"Stats: {stars_str}, {length_str}, Status: {status}, Mode: {mode}",
                "Result: Map Accepted!"
            ]
            log.info(f"Map {map_id} accepted. ({stars_str}, {length_str}, {status}, {mode})")
            self.send_message(messages)
            self.last_valid_map_id = map_id
            if self.current_host in self.map_violations:
                 if self.map_violations[self.current_host] > 0:
                    log.info(f"Resetting violations for {self.current_host} after valid pick.")
                    self.map_violations[self.current_host] = 0

    def reject_map(self, reason, is_violation=True):
        # Need current_host to attribute violation and potentially skip
        if not self.config['map_checker']['enabled'] or not self.current_host: return

        rejected_map_id = self.current_map_id
        rejected_map_title = self.current_map_title
        log.info(f"Rejecting map {rejected_map_id} ('{rejected_map_title}'). Reason: {reason}")

        messages = ["Map Check Failed:", reason]
        self.send_message(messages)

        self.current_map_id = 0
        self.current_map_title = ""
        self.last_valid_map_id = 0

        if not is_violation: return

        violation_limit = self.config['map_checker'].get('violations_allowed', 3)
        if violation_limit <= 0: return # Violations disabled

        count = self.map_violations.get(self.current_host, 0) + 1
        self.map_violations[self.current_host] = count
        log.warning(f"{self.current_host} violation count: {count}/{violation_limit}")

        if count >= violation_limit:
            messages = [
                f"Violation limit ({violation_limit}) reached for {self.current_host}.",
                "Skipping host."
            ]
            log.warning(f"Skipping host {self.current_host} due to map violations.")
            self.send_message(messages)
            # Ensure skip only happens if rotation is enabled
            if self.config['host_rotation']['enabled']:
                self.skip_current_host(f"Reached map violation limit ({violation_limit})")
            else:
                # If rotation off, maybe just clear host? Depends on desired behavior.
                 log.warning("Violation limit reached, but rotation is off. Cannot skip.")
                 # self.send_message("!mp clearhost") # Option
        else:
            remaining = violation_limit - count
            messages = [
                f"Map Violation Warning ({count}/{violation_limit}) for {self.current_host}.",
                f"Please choose a map matching the rules.",
                f"{remaining} violation(s) remaining this turn."
            ]
            self.send_message(messages)


    # --- Utility Methods ---
    def send_message(self, message_or_list):
        if not self.connection.is_connected() or not self.target_channel:
            log.warning(f"Cannot send, not connected/no channel: {message_or_list}")
            return

        messages = message_or_list if isinstance(message_or_list, list) else [message_or_list]
        delay = 0.35

        for i, msg in enumerate(messages):
            if not msg: continue
            full_msg = str(msg)
            max_len = 450
            if len(full_msg.encode('utf-8')) > max_len:
                log.warning(f"Truncating long message: {full_msg[:100]}...")
                encoded_msg = full_msg.encode('utf-8')
                while len(encoded_msg) > max_len:
                    encoded_msg = encoded_msg[:-1]
                try:
                    full_msg = encoded_msg.decode('utf-8', 'ignore') + "..."
                except UnicodeDecodeError:
                    full_msg = full_msg[:max_len//2] + "..."

            try:
                if i > 0: time.sleep(delay)
                self.connection.privmsg(self.target_channel, full_msg)
                log.debug(f"SENT to {self.target_channel}: {full_msg}")
                self.SentMessage.emit({'message': full_msg})
            except irc.client.ServerNotConnectedError:
                log.warning("Failed to send message: Disconnected.")
                self._request_shutdown()
                break
            except Exception as e:
                log.error(f"Failed to send message to {self.target_channel}: {e}")
                time.sleep(1)

    def _request_shutdown(self):
        global shutdown_requested
        if not shutdown_requested:
            log.info("Shutdown requested internally.")
            shutdown_requested = True

    # --- Admin Commands (Called from Console Input Thread) ---
    def admin_skip_host(self, reason="Admin command"):
        if not self.connection.is_connected():
             log.error("Admin Skip Failed: Not connected.")
             return

        # Use the existing skip logic, which checks for current host and rotation status
        log.info(f"Admin skip initiated. Reason: {reason}")
        self.skip_current_host(reason) # skip_current_host handles messages and rotation

    def admin_show_queue(self):
        if not self.config['host_rotation']['enabled']:
            print("Host rotation is disabled.")
            return
        print("--- Host Queue (Console View) ---")
        if not self.host_queue:
            print("(Empty)")
        else:
            q_list = list(self.host_queue)
            for i, p in enumerate(q_list):
                status = ""
                if p == self.current_host: status = "(Current Host)"
                elif i == 0: status = "(Next Host)"
                print(f"{i+1}. {p} {status}")
        print("---------------------------------")

    def admin_show_status(self):
        print("--- Bot Status (Console View) ---")
        print(f"Connected: {self.connection.is_connected()}")
        print(f"Channel: {self.target_channel}")
        print(f"Current Host: {self.current_host if self.current_host else 'None'}")
        print(f"Rotation Enabled: {self.config['host_rotation']['enabled']}")
        print(f"Map Check Enabled: {self.config['map_checker']['enabled']}")
        print(f"Match in Progress: {self.is_matching}")
        print(f"Current Map ID: {self.current_map_id if self.current_map_id else 'None'}")
        print(f"Last Valid Map ID: {self.last_valid_map_id if self.last_valid_map_id else 'None'}")
        if self.config['map_checker']['enabled']:
            mc = self.config['map_checker']
            statuses = self.config.get('allowed_map_statuses', ['all'])
            modes = self.config.get('allowed_modes', ['all'])
            print(" Rules:")
            print(f"  Stars: {mc.get('min_stars',0)}-{mc.get('max_stars',0)}")
            print(f"  Len: {self._format_time(mc.get('min_length_seconds'))}-{self._format_time(mc.get('max_length_seconds'))}")
            print(f"  Status: {','.join(statuses)}")
            print(f"  Modes: {','.join(modes)}")
        else:
             print(" Rules: (Map Check Disabled)")
        print("---------------------------------")


    # --- Shutdown ---
    def shutdown(self, message="Client shutting down."):
        log.info("Initiating shutdown sequence...")
        conn_available = hasattr(self, 'connection') and self.connection and self.connection.is_connected()

        if conn_available and self.config.get("goodbye_message"):
            try:
                log.info(f"Sending goodbye message: '{self.config['goodbye_message']}'")
                self.send_message(self.config['goodbye_message'])
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


# --- Configuration Loading/Generation ---
def load_or_generate_config(filepath):
    """Loads config from JSON file or generates a default one if not found."""
    defaults = {
        "server": "irc.ppy.sh",
        "port": 6667,
        "username": "YourOsuUsername",
        "password": "YourOsuIRCPassword",
        "room_id": "",
        "welcome_message": "Bot connected. Use !help for commands.",
        "goodbye_message": "Bot disconnecting.",
        "osu_api_client_id": 0,
        "osu_api_client_secret": "YOUR_CLIENT_SECRET",
        "map_checker": {
            "enabled": True,
            "min_stars": 0.0,
            "max_stars": 10.0,
            "min_length_seconds": 0,
            "max_length_seconds": 0,
            "violations_allowed": 3
        },
        "allowed_map_statuses": ["ranked", "approved", "qualified", "loved", "graveyard"],
        "allowed_modes": ["all"],
        "host_rotation": {
            "enabled": True
        }
    }
    try:
        if not filepath.exists():
            log.warning(f"Config file '{filepath}' not found. Generating default config.")
            log.warning("IMPORTANT: Please edit the generated config.json with your osu! username, IRC password, and API credentials!")
            try:
                with filepath.open('w') as f:
                    json.dump(defaults, f, indent=4)
                log.info(f"Default config file created at '{filepath}'. Please edit it and restart.")
                return defaults
            except IOError as e:
                 log.critical(f"Could not write default config file: {e}")
                 sys.exit(1)

        else:
            log.info(f"Loading configuration from '{filepath}'...")
            with filepath.open('r') as f:
                user_config = json.load(f)

            final_config = defaults.copy()
            for key, value in user_config.items():
                if isinstance(value, dict) and key in final_config and isinstance(final_config[key], dict):
                     for sub_key, sub_value in value.items():
                         if key in final_config and isinstance(final_config[key], dict):
                              final_config[key][sub_key] = sub_value
                         else:
                              final_config[key] = {sub_key: sub_value}
                else:
                    final_config[key] = value

            required = ["server", "port", "username", "password"]
            missing = [k for k in required if not final_config.get(k) or final_config[k] in ["", "YourOsuUsername", "YourOsuIRCPassword"]]
            if missing:
                log.warning(f"Missing or default required config keys: {', '.join(missing)}. Please check '{filepath}'.")

            if not isinstance(final_config["port"], int):
                log.error("'port' must be an integer. Using default 6667.")
                final_config["port"] = 6667
            if not isinstance(final_config["osu_api_client_id"], int):
                 log.error("'osu_api_client_id' must be an integer. Using default 0.")
                 final_config["osu_api_client_id"] = 0

            if not isinstance(final_config.get("allowed_map_statuses"), list):
                log.warning("'allowed_map_statuses' is not a list in config, using default.")
                final_config["allowed_map_statuses"] = defaults["allowed_map_statuses"]
            if not isinstance(final_config.get("allowed_modes"), list):
                log.warning("'allowed_modes' is not a list in config, using default.")
                final_config["allowed_modes"] = defaults["allowed_modes"]

            log.info(f"Configuration loaded successfully from '{filepath}'.")
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
    """Handles admin commands entered in the console."""
    global shutdown_requested
    log.info("Console input thread started. Type 'help' for commands.")
    while not shutdown_requested:
        try:
            command_line = input("ADMIN > ").strip()
            if not command_line: continue

            parts = shlex.split(command_line)
            command = parts[0].lower()
            args = parts[1:]

            # --- Bot Control ---
            if command == "quit" or command == "exit":
                log.info("Console requested quit.")
                shutdown_requested = True
                break

            elif command == "skip":
                reason = " ".join(args) if args else "Admin command"
                bot_instance.admin_skip_host(reason) # admin_skip_host calls skip_current_host which announces

            elif command == "queue" or command == "q":
                bot_instance.admin_show_queue()

            elif command == "status" or command == "info":
                 bot_instance.admin_show_status()

            # --- Admin Settings Modification ---
            elif command == "set_rotation": # New command
                if not args or args[0].lower() not in ['true', 'false']:
                    print("Usage: set_rotation <true|false>"); continue
                hr_config = bot_instance.config['host_rotation'] # Get the sub-dict
                value = args[0].lower() == 'true'
                if hr_config['enabled'] != value:
                    hr_config['enabled'] = value
                    print(f"Admin set Host Rotation to: {value}")
                    # Announce the change to chat
                    bot_instance.announce_setting_change("Host Rotation", value)
                    # Handle logic like requesting settings or clearing queue
                    if value and not bot_instance.host_queue:
                        log.info("Rotation enabled with empty queue, requesting !mp settings to populate.")
                        bot_instance.send_message("!mp settings")
                    elif not value:
                        bot_instance.host_queue.clear()
                        # current_host remains whoever Bancho says it is
                        log.info("Rotation disabled by admin. Queue cleared.")
                        bot_instance.send_message("Host queue cleared (rotation disabled).") # Inform chat
                else:
                    print(f"Host Rotation already set to {value}.")

            elif command == "set_map_check": # New command
                if not args or args[0].lower() not in ['true', 'false']:
                    print("Usage: set_map_check <true|false>"); continue
                mc_config = bot_instance.config['map_checker'] # Get the sub-dict
                value = args[0].lower() == 'true'
                if mc_config['enabled'] != value:
                     # Check for API keys before enabling
                    if value and (not bot_instance.api_client_id or bot_instance.api_client_secret == 'YOUR_CLIENT_SECRET'):
                         print("Cannot enable map check: API credentials missing/invalid in config.")
                         log.warning("Admin attempted to enable map check without valid API keys.")
                         continue # Prevent enabling

                    mc_config['enabled'] = value
                    print(f"Admin set Map Checker to: {value}")
                    # Announce the change to chat
                    bot_instance.announce_setting_change("Map Checker", value)
                    # Re-check current map if checker just got enabled
                    if value and bot_instance.current_map_id != 0 and bot_instance.current_host:
                        log.info("Map checker enabled, re-validating current map.")
                        bot_instance.check_map(bot_instance.current_map_id, bot_instance.current_map_title)
                else:
                    print(f"Map Checker already set to {value}.")


            elif command == "set_star_min":
                if not args: print("Usage: set_star_min <number|0>"); continue
                try:
                    value = float(args[0])
                    if value < 0: print("Min stars cannot be negative."); continue
                    current_value = bot_instance.config['map_checker'].get('min_stars', 0)
                    if current_value != value:
                        bot_instance.config['map_checker']['min_stars'] = value
                        print(f"Admin set Minimum Star Rating to: {value:.2f}*")
                        bot_instance.announce_setting_change("Min Stars", f"{value:.2f}*") # Announce
                    else:
                         print(f"Minimum Star Rating already set to {value:.2f}*")
                except ValueError:
                    print("Invalid number for minimum stars.")

            elif command == "set_star_max":
                if not args: print("Usage: set_star_max <number|0>"); continue
                try:
                    value = float(args[0])
                    if value < 0: print("Max stars cannot be negative."); continue
                    current_value = bot_instance.config['map_checker'].get('max_stars', 0)
                    if current_value != value:
                        bot_instance.config['map_checker']['max_stars'] = value
                        print(f"Admin set Maximum Star Rating to: {value:.2f}*")
                        bot_instance.announce_setting_change("Max Stars", f"{value:.2f}*") # Announce
                    else:
                        print(f"Maximum Star Rating already set to {value:.2f}*")
                except ValueError:
                    print("Invalid number for maximum stars.")

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
                        bot_instance.announce_setting_change("Min Length", formatted_time) # Announce
                    else:
                         print(f"Minimum Map Length already set to {bot_instance._format_time(value)}")
                except ValueError:
                    print("Invalid number for minimum length seconds.")

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
                        bot_instance.announce_setting_change("Max Length", formatted_time) # Announce
                    else:
                         print(f"Maximum Map Length already set to {bot_instance._format_time(value)}")
                except ValueError:
                    print("Invalid number for maximum length seconds.")

            elif command == "set_statuses":
                if not args:
                    current = ', '.join(bot_instance.config.get('allowed_map_statuses', ['all']))
                    print(f"Current allowed statuses: {current}")
                    print(f"Usage: set_statuses <status1> [status2...] or 'all'")
                    print(f"Available: {', '.join(OSU_STATUSES)}")
                    continue
                if args[0].lower() == 'all':
                    value = ['all']
                else:
                    value = sorted([s.lower() for s in args if s.lower() in OSU_STATUSES]) # Sort for consistent comparison
                    if not value:
                         print(f"Invalid or no valid status(es) provided. Available: {', '.join(OSU_STATUSES)} or 'all'")
                         continue
                current_value = sorted(bot_instance.config.get('allowed_map_statuses', ['all']))
                if current_value != value:
                    bot_instance.config['allowed_map_statuses'] = value
                    display_value = ', '.join(value)
                    print(f"Admin set Allowed Map Statuses to: {display_value}")
                    bot_instance.announce_setting_change("Allowed Statuses", display_value) # Announce
                else:
                    print(f"Allowed Map Statuses already set to: {', '.join(value)}")


            elif command == "set_modes":
                valid_modes = list(OSU_MODES.values())
                if not args:
                    current = ', '.join(bot_instance.config.get('allowed_modes', ['all']))
                    print(f"Current allowed modes: {current}")
                    print(f"Usage: set_modes <mode1> [mode2...] or 'all'")
                    print(f"Available: {', '.join(valid_modes)}")
                    continue
                if args[0].lower() == 'all':
                    value = ['all']
                else:
                    value = sorted([m.lower() for m in args if m.lower() in valid_modes]) # Sort
                    if not value:
                         print(f"Invalid or no valid mode(s) provided. Available: {', '.join(valid_modes)} or 'all'")
                         continue
                current_value = sorted(bot_instance.config.get('allowed_modes', ['all']))
                if current_value != value:
                    bot_instance.config['allowed_modes'] = value
                    display_value = ', '.join(value)
                    print(f"Admin set Allowed Game Modes to: {display_value}")
                    bot_instance.announce_setting_change("Allowed Modes", display_value) # Announce
                else:
                    print(f"Allowed Game Modes already set to: {', '.join(value)}")


            # --- Help ---
            elif command == "help":
                print("--- Admin Console Commands ---")
                print(" Bot Control:")
                print("  quit / exit         - Stop the bot gracefully.")
                print("  skip [reason]       - Force skip the current host.")
                print("  queue / q           - Show the current host queue (console).")
                print("  status / info       - Show current bot and lobby status (console).")
                print(" Settings:")
                print("  set_rotation <t/f>  - Enable/Disable host rotation. Ex: set_rotation true")
                print("  set_map_check <t/f> - Enable/Disable map checker. Ex: set_map_check true")
                print(" Map Rules (Admin Only):")
                print("  set_star_min <N>    - Set min star rating (0=off). Ex: set_star_min 4.5")
                print("  set_star_max <N>    - Set max star rating (0=off). Ex: set_star_max 6.0")
                print("  set_min_len <sec>   - Set min map length seconds (0=off). Ex: set_min_len 90")
                print("  set_max_len <sec>   - Set max map length seconds (0=off). Ex: set_max_len 300")
                print("  set_statuses <...>  - Set allowed map statuses (ranked, loved, etc. or 'all').")
                print("  set_modes <...>     - Set allowed game modes (osu, mania, etc. or 'all').")
                print(" Other:")
                print("  help                - Show this help message.")
                print("----------------------------")

            else:
                print(f"Unknown command: '{command}'. Type 'help' for options.")

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
            print("An error occurred processing the command.")
            time.sleep(1)

    log.info("Console input thread finished.")

# --- Main Execution ---
def main():
    global shutdown_requested

    config = load_or_generate_config(CONFIG_FILE)

    # --- Loop 2: Ask for Room ID ---
    while True:
        # Use room_id from config if already present and valid
        config_room_id = str(config.get('room_id', '')).strip()
        if config_room_id.isdigit():
             log.info(f"Using Room ID from config: {config_room_id}")
             config['room_id'] = config_room_id # Ensure it's stored correctly
             break
        # Otherwise, prompt the user
        try:
            room_id_input = input("Enter the osu! multiplayer room ID (numeric part only): ").strip()
            if room_id_input.isdigit():
                config['room_id'] = room_id_input
                log.info(f"Using Room ID: {config['room_id']}")
                # Optionally save back to config?
                # try:
                #     with CONFIG_FILE.open('w') as f:
                #          json.dump(config, f, indent=4)
                #     log.info(f"Saved room ID {config['room_id']} to config.json")
                # except Exception as e:
                #      log.error(f"Could not save room ID back to config: {e}")
                break
            else:
                print("Invalid input. Please enter only the numbers from the room link.")
        except EOFError:
            log.critical("Input closed during room ID prompt. Exiting.")
            sys.exit(1)
        except KeyboardInterrupt:
             log.info("\nOperation cancelled by user during room ID prompt. Exiting.")
             sys.exit(0)

    # --- Initialize Bot ---
    bot = OsuRoomBot(config)

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
        if "Nickname is already in use" in str(e):
             log.critical("Try changing the 'username' in config.json or ensure no other client is connected with that name.")
        elif "incorrect password" in str(e).lower():
            log.critical("Incorrect password. Get your IRC password from https://osu.ppy.sh/home/account/edit#legacy-api")
        sys.exit(1)
    except Exception as e:
        log.critical(f"Unexpected connection setup error: {e}", exc_info=True)
        sys.exit(1)

    # --- Start Console Input Thread ---
    console_thread = threading.Thread(target=console_input_loop, args=(bot,), daemon=True)
    console_thread.start()

    # --- Main Loop (IRC Processing) ---
    log.info("Starting main processing loop (Use console for admin commands, Ctrl+C to exit).")
    while not shutdown_requested:
        try:
            if hasattr(bot, 'reactor') and bot.reactor:
                bot.reactor.process_once(timeout=0.2)
            else:
                if not (hasattr(bot, 'connection') and bot.connection and bot.connection.is_connected()):
                    log.warning("Connection lost and reactor unavailable. Requesting shutdown.")
                    shutdown_requested = True
                time.sleep(0.1)

            if not bot.connection.is_connected() and bot.connection_registered:
                 # Check if we were previously registered and now aren't connected
                 log.warning("Connection lost unexpectedly. Requesting shutdown.")
                 shutdown_requested = True

        except irc.client.ServerNotConnectedError:
            if not shutdown_requested:
                 log.warning("Disconnected during processing loop.")
                 shutdown_requested = True
        except Exception as e:
            log.error(f"Unhandled exception in main loop: {e}", exc_info=True)
            time.sleep(1)

    # --- Shutdown Sequence ---
    log.info("Main loop exited.")
    if bot:
        bot.shutdown("Client shutting down normally.")
    time.sleep(0.5) # Allow threads to potentially finish logging
    log.info("Bot finished.")

if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
         log.info(f"Program exited with code {e.code}.")
         sys.exit(e.code)
    except KeyboardInterrupt:
         log.info("\nMain execution interrupted by Ctrl+C. Shutting down.")
         shutdown_requested = True
    except Exception as e:
        log.critical(f"Critical error during main execution: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logging.shutdown()