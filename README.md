# osu-ahr-py

A Python-based IRC bot designed to automate host rotation and beatmap checking in osu! multiplayer lobbies. This bot connects to the osu! IRC server, monitors the chat in a specified multiplayer room, and enforces rules defined in a configuration file.

## Features

*   **Host Rotation:** Automatically manages the host queue and rotates hosts after each match, ensuring fair play and participation.
*   **Map Checker:** Validates beatmaps against configurable rules (star rating, length, status, game mode) to maintain lobby standards.
*   **Admin Console:** Provides a command-line interface for administrators to control the bot, skip hosts, view the queue, and modify settings.
*   **osu! API Integration:** Uses the osu! API v2 to fetch beatmap information for accurate and reliable map checking.
*   **Configurable Rules:**  Allows administrators to set specific criteria for allowed beatmaps and customize the bot's behavior.
*   **Event-Driven Architecture:** Utilizes a simple event emitter for extensibility and integration with other systems.
*   **Robust Error Handling:** Implements extensive logging and error handling to ensure stability and provide detailed diagnostics.
*   **Cross-Platform Support:** Includes scripts for both Windows (`run.bat`) and Linux (`run.sh`) to simplify bot execution.

## Prerequisites

*   **Python 3.7+:** The bot is written in Python 3 and requires a compatible interpreter.
*   **osu! Account:** An osu! account is needed to connect to the IRC server.  You'll need your username and IRC password.
*   **osu! API v2 Credentials:** To use the map checker feature, you'll need an osu! API v2 client ID and secret.  You can obtain these by creating a new application at [https://osu.ppy.sh/oauth/clients/new](https://osu.ppy.sh/oauth/clients/new).  Set the Application Callback URL to `http://localhost`.
*   **`venv` Package:**  The bot uses a virtual environment. Linux users may need to install the `venv` package using their distribution's package manager. For example, on Debian/Ubuntu:

    ```bash
    sudo apt-get update
    sudo apt-get install python3-venv
    ```

## Installation
Clone the repository or downlaod main branch zip
	
```bash
git clone https://github.com/serifpersia/osu-ahr-py.git
cd osu-ahr-py
 ```

 
## Configuration

1.  **Create `config.json`:**  If the `config.json` file doesn't exist, the bot will generate a default configuration file on its first run.  It will be created in the same directory as the bot script.

2.  **Edit `config.json`:**  Open the `config.json` file and modify the following settings:

    *   `server`: The IRC server address (usually `irc.ppy.sh`).
    *   `port`: The IRC server port (usually `6667`).
    *   `username`: Your osu! username.
    *   `password`: Your osu! IRC password.  Get this from [https://osu.ppy.sh/home/account/edit#legacy-api](https://osu.ppy.sh/home/account/edit#legacy-api).
    *   `room_id`: The numerical ID of the multiplayer room.  Only the numbers from the room link, e.g., if the room link is `https://osu.ppy.sh/community/matches/123456789` then the `room_id` is `123456789`.
    *   `osu_api_client_id`: Your osu! API v2 client ID.
    *   `osu_api_client_secret`: Your osu! API v2 client secret.

    The `config.json` file also contains sections for:

    *   `welcome_message`: Message sent when the bot joins the channel.
    *   `goodbye_message`: Message sent when the bot disconnects.
    *   `map_checker`: Settings for enabling the map checker and defining map rules (star rating, length in seconds, allowed statuses, and game modes).
        *   `enabled`: Enable or disable the map checker.
        *   `min_stars`: Minimum star rating (0 to disable).
        *   `max_stars`: Maximum star rating (0 to disable).
        *   `min_length_seconds`: Minimum map length in seconds (0 to disable).
        *   `max_length_seconds`: Maximum map length in seconds (0 to disable).
        *   `violations_allowed`: Number of invalid map selections a host can make before being skipped.
    *   `allowed_map_statuses`:  A list of allowed map statuses. Valid statuses are: `graveyard`, `wip`, `pending`, `ranked`, `approved`, `qualified`, `loved`, or `all` for all statuses.
    *   `allowed_modes`: A list of allowed game modes. Valid modes are: `osu`, `taiko`, `fruits`, `mania`, or `all` for all modes.
    *   `host_rotation`: Settings for enabling host rotation.
        *   `enabled`: Enable or disable host rotation.

    ```json
    {
      "server": "irc.ppy.sh",
      "port": 6667,
      "username": "YourOsuUsername",
      "password": "YourOsuIRCPassword",
      "room_id": "123456789",
      "welcome_message": "Bot connected. Use !help for commands.",
      "goodbye_message": "Bot disconnecting.",
      "osu_api_client_id": 1234,
      "osu_api_client_secret": "YOUR_CLIENT_SECRET",
      "map_checker": {
        "enabled": true,
        "min_stars": 4.0,
        "max_stars": 7.0,
        "min_length_seconds": 60,
        "max_length_seconds": 300,
        "violations_allowed": 3
      },
      "allowed_map_statuses": ["ranked", "approved", "loved"],
      "allowed_modes": ["osu"],
      "host_rotation": {
        "enabled": true
      }
    }
    ```

## Usage

1.  **Run the bot:**

    *   **Windows:** Execute `run.bat`.
    *   **Linux/macOS:** Execute `run.sh`.  You may need to make the script executable first: `chmod +x run.sh`

    The `run.bat` and `run.sh` scripts handle virtual environment activation and bot execution.

2.  **Enter Room ID:** If a `room_id` is not already present or is invalid in `config.json`, the bot will prompt you to enter the osu! multiplayer room ID in the console.

3.  **Admin Console:**  The bot provides an admin console for controlling the bot and modifying settings. Type `help` for available commands.

    ```
    ADMIN > help
    --- Admin Console Commands ---
    ```

## Chat Commands

The following commands can be used by players in the osu! multiplayer room:

*   `!queue`: Shows the current host queue (if host rotation is enabled).
*   `!skip`: (Host Only) Skips the current host's turn.
*   `!help`: Displays the available chat commands and current map rules (if map checker is enabled).

## Contributing

Contributions are welcome! Please feel free to submit bug reports, feature requests, and pull requests.

## License

This project is licensed under the [MIT License](LICENSE).
