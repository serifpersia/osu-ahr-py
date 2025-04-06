# osu-ahr-py

A Python-based IRC bot designed to automate host rotation, beatmap checking, and other management tasks in osu! multiplayer lobbies. This bot connects to the osu! IRC server, monitors the chat in a specified multiplayer room, and enforces rules defined in a configuration file.

## Features

*   **Host Rotation:** Automatically manages the host queue and rotates hosts after each match or skip, ensuring fair play and participation.
*   **Map Checker:**
    *   Validates beatmaps against configurable rules (star rating, length, status, game mode).
    *   **Map History:** Prevents maps played recently (within a configurable number of matches) from being picked again.
    *   Automatically attempts to revert to the last valid map if an invalid one is chosen.
*   **Enhanced Map Info:**
    *   **Hyperlinked Names:** Displays validated map names as clickable `[osu://b/...]` links in chat.
    *   **Mirror Links:** Optionally provides a direct mirror download link (e.g., catboy.best) alongside the official link.
    *   **Detailed Stats:** Shows relevant, mode-specific map details (Rating, Length, BPM, CS/AR/OD/HP/Keys, Object Counts) with labels when a map is validated.
*   **Vote Skip:** Allows players to initiate a vote to skip the current host if they are unresponsive or unwanted.
*   **AFK Handling:** Automatically skips the current host if they remain inactive for a configurable duration (doesn't skip if they've recently selected a valid map).
*   **Auto Start:** Automatically starts the match with a configurable delay once all players are ready and a valid map is selected.
*   **Host Commands:** Provides chat commands (`!start`, `!abort`) for the current host to manage the match flow.
*   **Admin Console:** Provides a command-line interface for administrators to control the bot, skip hosts, view the queue, and modify settings on the fly (changes are saved to `config.json`).
*   **osu! API Integration:** Uses the osu! API v2 to fetch beatmap information for accurate and reliable map checking and stat display.
*   **Configurable Rules:** Allows administrators to set specific criteria for allowed beatmaps and customize the bot's behavior extensively via `config.json`.
*   **Cross-Platform Support:** Includes scripts for both Windows (`run.bat`) and Linux/macOS (`run.sh`) to simplify bot execution.

## Prerequisites

*   **Python 3.7+:** The bot is written in Python 3 and requires a compatible interpreter.
*   **osu! Account:** An osu! account is needed to connect to the IRC server. You'll need your username and IRC password.
*   **osu! API v2 Credentials:** To use the map checker feature and detailed map info, you'll need an osu! API v2 client ID and secret. You can obtain these by creating a new application at [https://osu.ppy.sh/oauth/clients/new](https://osu.ppy.sh/oauth/clients/new). Set the Application Callback URL to `http://localhost`.
*   **`venv` Package:** The bot uses a virtual environment. Linux users may need to install the `venv` package using their distribution's package manager. For example, on Debian/Ubuntu:
    ```bash
    sudo apt-get update
    sudo apt-get install python3-venv
    ```

## Installation

Clone the repository or download the main branch zip:

```bash
git clone https://github.com/serifpersia/osu-ahr-py.git
cd osu-ahr-py
```

## Configuration

1.  **Create `config.json`:** If the `config.json` file doesn't exist, the bot will generate a default configuration file on its first run. It will be created in the same directory as the bot script.

2.  **Edit `config.json`:** Open the `config.json` file and modify the core settings:

    *   `server`: The IRC server address (usually `irc.ppy.sh`).
    *   `port`: The IRC server port (usually `6667`).
    *   `username`: Your osu! username.
    *   `password`: Your osu! IRC password. Get this from [https://osu.ppy.sh/home/account/edit#legacy-api](https://osu.ppy.sh/home/account/edit#legacy-api).
    *   `osu_api_client_id`: Your osu! API v2 client ID (required for map checker/info).
    *   `osu_api_client_secret`: Your osu! API v2 client secret (required for map checker/info).

    The `config.json` file also contains sections for customizing features:

    *   `welcome_message`, `goodbye_message`: Bot join/leave messages.
    *   `map_checker`: Settings for the map checker.
        *   `enabled`: `true` or `false`.
        *   `min_stars`, `max_stars`: Star rating range (0 to disable).
        *   `min_length_seconds`, `max_length_seconds`: Map length range in seconds (0 to disable).
        *   `violations_allowed`: Invalid picks before host skip.
    *   `allowed_map_statuses`: List of allowed statuses (e.g., `"ranked"`, `"loved"`). Use `"all"` to allow any.
    *   `allowed_modes`: List of allowed game modes (e.g., `"osu"`, `"mania"`). Use `"all"` to allow any.
    *   **`maps_played_list`**: Settings for the map history feature.
        *   `enabled`: `true` or `false` (enables checking against recently played maps).
        *   `max_size`: How many maps to remember in the history (e.g., `5`).
    *   `host_rotation`: Settings for host rotation.
        *   `enabled`: `true` or `false`.
    *   `vote_skip`: Settings for the player vote skip feature.
        *   `enabled`: `true` or `false`.
        *   `timeout_seconds`: Vote duration.
        *   `threshold_type`: `"percentage"` or `"fixed"`.
        *   `threshold_value`: Percentage (e.g., `51`) or fixed number required.
    *   `afk_handling`: Settings for automatic AFK host skipping.
        *   `enabled`: `true` or `false`.
        *   `timeout_seconds`: Idle time before skip.
    *   `auto_start`: Settings for automatically starting the match.
        *   `enabled`: `true` or `false`.
        *   `delay_seconds`: Delay after "All ready".
    *   `auto_close_empty_room`: Settings for closing empty, bot-created rooms.
        *   `enabled`: `true` or `false`.
        *   `delay_seconds`: Timeout before sending `!mp close`.

    **Example `config.json` (with new section):**
    ```json
    {
        "server": "irc.ppy.sh",
        "port": 6667,
        "username": "YourOsuUsername",
        "password": "YourOsuIRCPassword",
        "welcome_message": "Bot connected. !help for commands. !rules for map rules.",
        "goodbye_message": "Bot disconnecting.",
        "osu_api_client_id": 0,
        "osu_api_client_secret": "YOUR_CLIENT_SECRET",
        "map_checker": {
            "enabled": true,
            "min_stars": 2.5,
            "max_stars": 3.66,
            "min_length_seconds": 30,
            "max_length_seconds": 230,
            "violations_allowed": 3
        },
        "allowed_map_statuses": [
            "ranked",
            "loved",
            "approved",
            "qualified"
        ],
        "allowed_modes": [
            "mania"
        ],
        "maps_played_list": {
            "enabled": true,
            "max_size": 5
        },
        "host_rotation": {
            "enabled": true
        },
        "vote_skip": {
            "enabled": true,
            "timeout_seconds": 60,
            "threshold_type": "percentage",
            "threshold_value": 51
        },
        "afk_handling": {
            "enabled": true,
            "timeout_seconds": 120
        },
        "auto_start": {
            "enabled": true,
            "delay_seconds": 2
        },
        "auto_close_empty_room": {
            "enabled": true,
            "delay_seconds": 60
        }
    }
    ```

## Usage

1.  **Run the bot:**

    *   **Windows:** Execute `run.bat`.
    *   **Linux/macOS:** Execute `run.sh`. You may need to make the script executable first: `chmod +x run.sh`

    The `run.bat` and `run.sh` scripts handle virtual environment creation/activation and bot execution.

2.  **Enter Room ID (If Needed):** If the bot is started without a specific room context (e.g., `config.json` is missing initial room info or the bot left a room via `stop`), it will wait in the `make/enter/quit` state. Use the console commands `enter <room_id>` or `make "<Room Name>" [password]` to join or create a lobby.

3.  **Admin Console:** The bot provides an admin console for controlling the bot and modifying settings. Type `help` for available commands. Changes made via the admin console (like `set_rotation`, `set_star_min`, `set_map_history_size`, etc.) are saved to `config.json` immediately.

    ```
    ADMIN > help
    --- Admin Console Commands ---
    ... (command list) ...
    ```

## Chat Commands

The following commands can be used by players in the osu! multiplayer room chat:

**General Commands:**

*   `!help`: Displays available chat commands, a brief rule summary, and bot source info.
*   `!queue`: Shows the current host order (if host rotation is enabled).
*   `!rules`: Displays the detailed current map selection rules (star rating, length, status, mode, violations, map history setting).
*   `!voteskip`: Initiates or participates in a vote to skip the current host (if enabled).

**Host-Only Commands:**

*   `!skip`: Skips your turn as host.
*   `!start [delay]`: Starts the match immediately or after an optional `delay` in seconds.
*   `!abort`: Aborts the match start countdown.

## Contributing

Contributions are welcome! Please feel free to submit bug reports, feature requests, and pull requests.

## License

This project is licensed under the [MIT License](LICENSE).
