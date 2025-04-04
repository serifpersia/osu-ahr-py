# osu-ahr-py

A Python-based IRC bot designed to automate host rotation, beatmap checking, and other management tasks in osu! multiplayer lobbies. This bot connects to the osu! IRC server, monitors the chat in a specified multiplayer room, and enforces rules defined in a configuration file.

## Features

*   **Host Rotation:** Automatically manages the host queue and rotates hosts after each match or skip, ensuring fair play and participation.
*   **Map Checker:** Validates beatmaps against configurable rules (star rating, length, status, game mode) to maintain lobby standards. Automatically attempts to revert to the last valid map if an invalid one is chosen.
*   **Vote Skip:** Allows players to initiate a vote to skip the current host if they are unresponsive or unwanted.
*   **AFK Handling:** Automatically skips the current host if they remain inactive for a configurable duration (doesn't skip if they've recently selected a valid map and are waiting for players).
*   **Auto Start:** Automatically starts the match with a configurable delay once all players are ready and a valid map is selected.
*   **Host Commands:** Provides chat commands (`!start`, `!abort`) for the current host to manage the match flow.
*   **Admin Console:** Provides a command-line interface for administrators to control the bot, skip hosts, view the queue, and modify settings on the fly (changes are saved to `config.json`).
*   **osu! API Integration:** Uses the osu! API v2 to fetch beatmap information for accurate and reliable map checking.
*   **Configurable Rules:** Allows administrators to set specific criteria for allowed beatmaps and customize the bot's behavior extensively via `config.json`.
*   **Event-Driven Architecture:** Utilizes a simple event emitter for potential extensibility.
*   **Robust Error Handling:** Implements extensive logging and error handling to ensure stability and provide detailed diagnostics.
*   **Cross-Platform Support:** Includes scripts for both Windows (`run.bat`) and Linux/macOS (`run.sh`) to simplify bot execution.

## Prerequisites

*   **Python 3.7+:** The bot is written in Python 3 and requires a compatible interpreter.
*   **osu! Account:** An osu! account is needed to connect to the IRC server. You'll need your username and IRC password.
*   **osu! API v2 Credentials:** To use the map checker feature, you'll need an osu! API v2 client ID and secret. You can obtain these by creating a new application at [https://osu.ppy.sh/oauth/clients/new](https://osu.ppy.sh/oauth/clients/new). Set the Application Callback URL to `http://localhost`.
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
    *   `room_id`: The numerical ID of the multiplayer room. Only the numbers from the room link, e.g., if the room link is `https://osu.ppy.sh/community/matches/123456789` then the `room_id` is `123456789`. (Can be left blank to be prompted on startup).
    *   `osu_api_client_id`: Your osu! API v2 client ID (required for map checker).
    *   `osu_api_client_secret`: Your osu! API v2 client secret (required for map checker).

    The `config.json` file also contains sections for customizing features:

    *   `welcome_message`: Message sent when the bot joins the channel.
    *   `goodbye_message`: Message sent when the bot disconnects.
    *   `map_checker`: Settings for the map checker.
        *   `enabled`: `true` or `false`.
        *   `min_stars`, `max_stars`: Minimum/maximum star rating (0 to disable).
        *   `min_length_seconds`, `max_length_seconds`: Minimum/maximum map length in seconds (0 to disable).
        *   `violations_allowed`: Number of invalid map selections before skipping the host.
    *   `allowed_map_statuses`: List of allowed statuses (e.g., `"ranked"`, `"loved"`, `"qualified"`). Use `"all"` to allow any status. See constants in script for all options.
    *   `allowed_modes`: List of allowed game modes (e.g., `"osu"`, `"mania"`). Use `"all"` to allow any mode.
    *   `host_rotation`: Settings for host rotation.
        *   `enabled`: `true` or `false`.
    *   `vote_skip`: Settings for the player vote skip feature.
        *   `enabled`: `true` or `false`.
        *   `timeout_seconds`: How long a vote stays active.
        *   `threshold_type`: How the required votes are calculated (`"percentage"` or `"fixed"`).
        *   `threshold_value`: The percentage (e.g., `51` for >50%) or fixed number of votes required (excluding the host).
    *   `afk_handling`: Settings for automatic AFK host skipping.
        *   `enabled`: `true` or `false`.
        *   `timeout_seconds`: How long the host can be idle before being skipped.
    *   `auto_start`: Settings for automatically starting the match.
        *   `enabled`: `true` or `false`.
        *   `delay_seconds`: Delay after "All players ready" before sending `!mp start`.
    *   `auto_close_empty_room`: Settings for automatically closing the match.
        *   `enabled`: `true` or `false`.
        *   `delay_seconds`: Timer for empty room before sending `!mp close`. 
        *   

    **Example `config.json`:**
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
				"min_stars": 0.0,
				"max_stars": 10.0,
				"min_length_seconds": 0,
				"max_length_seconds": 0,
				"violations_allowed": 3
			},
			"allowed_map_statuses": [
				"ranked",
				"approved",
				"qualified",
				"loved",
				"graveyard"
			],
			"allowed_modes": [
				"all"
			],
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
				"enabled": false,
				"delay_seconds": 5
			},
			"auto_close_empty_room": {
				"enabled": true,
				"delay_seconds": 300
			}
	}
    ```

## Usage

1.  **Run the bot:**

    *   **Windows:** Execute `run.bat`.
    *   **Linux/macOS:** Execute `run.sh`. You may need to make the script executable first: `chmod +x run.sh`

    The `run.bat` and `run.sh` scripts handle virtual environment creation/activation and bot execution.

2.  **Enter Room ID (If Needed):** If a `room_id` is not already present or is invalid in `config.json`, the bot will prompt you to enter the osu! multiplayer room ID in the console during startup.

3.  **Admin Console:** The bot provides an admin console for controlling the bot and modifying settings. Type `help` for available commands. Changes made via the admin console (like `set_rotation`, `set_star_min`, etc.) are saved to `config.json` immediately.

    ```
    ADMIN > help
    --- Admin Console Commands ---
    ... (command list) ...
    ```

## Chat Commands

The following commands can be used by players in the osu! multiplayer room chat:

**General Commands:**

*   `!help`: Displays available chat commands and a brief rule summary (if map checker is enabled).
*   `!queue`: Shows the current host order (e.g., `Player1[1](Current), Player2[2]...`) if host rotation is enabled.
*   `!rules`: Displays the detailed current map selection rules (star rating, length, status, mode, violations).
*   `!voteskip`: Initiates or participates in a vote to skip the current host (if enabled).

**Host-Only Commands:**

*   `!skip`: Skips your turn as host (moves you to the back of the queue if rotation is enabled).
*   `!start [delay]`: Starts the match immediately or after an optional `delay` in seconds (e.g., `!start 5`).
*   `!abort`: Aborts the match start countdown.

## Contributing

Contributions are welcome! Please feel free to submit bug reports, feature requests, and pull requests.

## License

This project is licensed under the [MIT License](LICENSE).
