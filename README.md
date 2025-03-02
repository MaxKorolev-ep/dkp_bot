# Dragon Killing Point (DKP) Bot for Discord

The DKP bot is a simple and efficient tool designed to track points that your guild members can earn or lose in any RPG game, right on Discord.

## Features

* Tracking of points earned or lost by guild members.
* Easy to install and configure.
* Built with the Discord Python library.

## Prerequisites

Ensure that you have Docker and Docker Compose installed on your machine. If you haven't installed these yet, you can find instructions here:

* [Docker installation guide](https://docs.docker.com/get-docker/)
* [Docker Compose installation guide](https://docs.docker.com/compose/install/)

## Installation and Configuration

1. Clone this repository:

    ```
    git clone https://github.com/<your-username>/dkp-bot.git
    cd dkp-bot
    ```

2. Create a `.env` file in your project's root directory and add your bot's token and guild ID:

    ```
    ENV=.env
    DISCORD_TOKEN=<bot token>
    GUILD_ID=<discord guild id>
    ```

    Replace `<bot token>` and `<discord guild id>` with your Discord bot's token and guild ID respectively.

3. Use Docker Compose to build and run the bot:

    ```
    docker-compose build && docker-compose up -d
    ```

## License

This project is licensed under the MIT License. 

