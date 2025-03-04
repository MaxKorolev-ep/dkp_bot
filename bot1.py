import discord
from discord.ext import commands
from dotenv import load_dotenv
import os
import logging
import re
import json
import asyncio
import time
import datetime
import aiofiles
from github import Github

load_dotenv()
GUILD_ID = os.getenv("GUILD_ID")  # Загружаем как строку
if GUILD_ID is None:
    raise ValueError("GUILD_ID is missing. Check your .env file!")

GUILD_ID = int(GUILD_ID)  # Преобразуем в число после проверки

GUILD_ID = int(os.environ.get('GUILD_ID'))
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'ERROR')

intents = discord.Intents.all()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Путь к файлу, где будут храниться данные
DKP_FILE = "dkp_data.json"
dkp_lock = asyncio.Lock()
auctions = {}
AUC_LOG_FILE = "auc_log.json"
# Словарь для хранения ID сообщений об аукционах
auction_messages = {}


async def load_dkp_data():
    async with dkp_lock:
        try:
            with open(DKP_FILE, "r") as f:
                data = f.read().strip()
                return json.loads(data) if data else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

async def save_dkp_data(data):
    async with dkp_lock:
        with open(DKP_FILE, "w") as f:
            json.dump(data, f, indent=4)
            
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def add_members(ctx):
    """Adds all members of the server to the DKP database if they are not already present."""
    guild = ctx.guild
    dkp_data = await load_dkp_data()  # Загружаем существующую базу DKP

    added_members = 0

    for member in guild.members:
        if not member.bot:  # Игнорируем ботов
            user_id = str(member.id)
            if user_id not in dkp_data:
                dkp_data[user_id] = {
                    "display_name": member.display_name,
                    "dkp": 0  # Начальное значение DKP можно изменить
                }
                added_members += 1

    await save_dkp_data(dkp_data)  # Сохраняем обновленные данные

    await ctx.send(f"✅ Added {added_members} new members to the DKP database.")

@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def updm_names(ctx):
    """Updates the display names in the DKP database without erasing existing data."""
    dkp_data = await load_dkp_data()

    if ctx.guild is None:
        await ctx.send("This command must be used in a server.")
        return

    for member in ctx.guild.members:
        user_id = str(member.id)
        if user_id in dkp_data:
            dkp_data[user_id]["display_name"] = member.display_name

    await save_dkp_data(dkp_data)
    await ctx.send("Display names updated successfully.")



def format_seconds(seconds):
    seconds = int(seconds)  # Округление до целого числа
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    return f"{hours}h {minutes}m {seconds}s"


# Функция для проверки всех ставок пользователя
@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def mybids(ctx):
    """Displays all active bids placed by the user."""
    global auctions
    user = ctx.author
    user_bids = [
        f"**{name}** - {auction['item']}: **{bid['amount']} DKP**"
        for name, auction in auctions.items()
        for bid in auction["bids"]
        if bid["user"] == user.id
    ]

    if user_bids:
        await ctx.send(f"Your active bids:\n" + "\n".join(user_bids))
    else:
        await ctx.send("You have no active bids.")
# Обработчик ошибки, если команда используется слишком часто
@mybids.error
async def mybids_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")
# Функция для просмотра ставок на конкретный аукцион
@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def bids(ctx, auction_id: int):
    if auction_id not in auctions:
        await ctx.send(f"Auction with ID '{auction_id}' does not exist.")
        return

    auction = auctions[auction_id]

    if not auction["bids"]:
        await ctx.send(f"No bids for **{auction['item']}** (Auction ID: {auction_id}).")
        return

    bids_message = f"Bids for **{auction['item']}** (Auction ID: {auction_id}):\n"
    for bid in auction["bids"]:
        user = await bot.fetch_user(bid["user"])
        bids_message += f"{user.display_name}: {bid['amount']} DKP\n"

    await ctx.send(bids_message)
# Обработчик ошибки, если команда используется слишком часто
@bids.error
async def bids_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")

# Функция для ставки на конкретный аукцион
@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def bid(ctx, auction_id: int, amount: int):
    """Places a bid on an auction using the auction ID."""
    user = ctx.author

    # Проверяем, существует ли аукцион с таким ID
    auction = auctions.get(auction_id)
    if not auction:
        await ctx.send(f"Auction with ID '{auction_id}' does not exist.")
        return

    # Проверяем, не завершился ли аукцион
    if time.time() > auction["end_time"]:
        await ctx.send(f"The auction for **{auction['item']}** has ended!")
        return

    # Загружаем данные пользователя
    dkp_data = await load_dkp_data()
    user_dkp = dkp_data.get(str(user.id), {"dkp": 0})["dkp"]

    # Проверяем, есть ли уже ставка от этого пользователя и удаляем её перед расчетом
    existing_bid = next((b for b in auction["bids"] if b["user"] == user.id), None)
    if existing_bid:
        auction["bids"].remove(existing_bid)

    # Подсчет общей суммы ставок пользователя **без учета старой ставки**
    total_bids = sum(
        bid["amount"] for auc in auctions.values() for bid in auc["bids"] if bid["user"] == user.id
    )

    # Проверяем, превысит ли новая ставка лимит
    if total_bids + amount > user_dkp:
        await ctx.send(f"Total bids exceed your DKP balance ({user_dkp}). You cannot place this bid.")
        return

    # Добавляем новую ставку
    auction["bids"].append({"user": user.id, "amount": amount})

    # Обновляем текущую максимальную ставку
    highest_bid = max(auction["bids"], key=lambda b: b["amount"], default={"amount": 0})
    auction["highest_bid"] = highest_bid["amount"]
    auction["highest_bidder"] = highest_bid["user"]

    await ctx.send(f"{user.display_name} placed a bid of {amount} DKP.\n Auction ID: **{auction_id}** \n Item: **{auction['item']}**.")
# Обработчик ошибки, если команда используется слишком часто
@bid.error
async def bid_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")
# Функция для удаления ставки на конкретный аукцион
@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def dbid(ctx, auction_id: int):
    """Removes a user's bid from the auction by auction ID."""
    global auctions
    user = ctx.author

    # Проверяем, существует ли аукцион с таким ID
    if auction_id not in auctions:
        await ctx.send(f"Auction with ID '{auction_id}' does not exist.")
        return

    auction = auctions[auction_id]

    # Проверяем, есть ли ставка от этого пользователя
    existing_bid = next((b for b in auction["bids"] if b["user"] == user.id), None)
    if not existing_bid:
        await ctx.send(f"{user.display_name}, you have no bid in the auction with ID '{auction_id}'.")
        return

    # Удаляем ставку пользователя
    auction["bids"].remove(existing_bid)

    # Обновляем максимальную ставку
    if auction["bids"]:
        highest_bid = max(auction["bids"], key=lambda b: b["amount"])
        auction["highest_bid"] = highest_bid["amount"]
        auction["highest_bidder"] = highest_bid["user"]
    else:
        auction["highest_bid"] = 0
        auction["highest_bidder"] = None

    await ctx.send(f"{user.display_name}, your bid has been removed from the auction with ID '{auction_id}'.")
# Обработчик ошибки, если команда используется слишком часто
@dbid.error
async def dbid_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")
        
async def log_auction_creation(auction_id, auction_name, item, description, end_time):
    """Записывает в лог информацию о новом аукционе."""
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")

    # Загружаем текущие данные из файла
    if not os.path.exists(AUC_LOG_FILE):
        auction_log = {"last_id": 0, "auctions": {}}
    else:
        async with aiofiles.open(AUC_LOG_FILE, mode="r") as f:
            content = await f.read()
            auction_log = json.loads(content) if content.strip() else {"last_id": 0, "auctions": {}}

    # Обновляем last_id
    auction_log["last_id"] = auction_id

    # Добавляем новый аукцион в лог с уникальным идентификатором
    auction_log["auctions"][f"{auction_name}_{auction_id}"] = {
        "id": auction_id,
        "name": auction_name,
        "item": item,
        "description": description,
        "created_at": timestamp,
        "date_of_end": time.strftime("[%Y-%m-%d %H:%M:%S]", time.localtime(end_time)),
        "top_3_bids": []  # Заполнится в endauction
    }

    # Сохраняем изменения в файл
    async with aiofiles.open(AUC_LOG_FILE, mode="w") as f:
        await f.write(json.dumps(auction_log, indent=4, ensure_ascii=False))

async def log_auction_result(auction_id, top_3_bids):
    """Обновляет лог аукциона, добавляя top 3 bids."""
    if not os.path.exists(AUC_LOG_FILE):
        return

    # Загружаем текущий лог аукционов
    async with aiofiles.open(AUC_LOG_FILE, mode="r") as f:
        content = await f.read()
        auction_log = json.loads(content) if content.strip() else {"last_id": 0, "auctions": {}}

    # Проверяем, что аукцион с данным ID существует
    for auction_name, auction_data in auction_log["auctions"].items():
        if auction_data["id"] == auction_id:
            # Обновляем данные top_3_bids, сохраняем только ID пользователей и сумму ставки
            auction_data["top_3_bids"] = [
                {"user_id": bid["user"].id, "amount": bid["amount"]} for bid in top_3_bids
            ]
            
            # Сохраняем обновленный лог
            async with aiofiles.open(AUC_LOG_FILE, mode="w") as f:
                await f.write(json.dumps(auction_log, indent=4, ensure_ascii=False))
            break

# Функция для старта аукциона с уникальным именем
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def sauc(ctx, auction_name: str, item: str, description: str, duration: int):
    """Starts an auction for a specific item with the specified duration and description."""
    global auctions

    # Проверяем, есть ли уже аукцион с таким именем
    if auction_name in auctions:
        await ctx.send(f"Auction with the name '{auction_name}' already exists.")
        return

    # Загружаем текущие данные из лога
    if not os.path.exists(AUC_LOG_FILE):
        auction_id = 1
    else:
        async with aiofiles.open(AUC_LOG_FILE, mode="r") as f:
            content = await f.read()
            auction_log = json.loads(content) if content.strip() else {"last_id": 0, "auctions": {}}
            auction_id = int(auction_log["last_id"]) + 1  # Преобразуем last_id в целое число

    end_time = time.time() + duration

    # Создаем новый аукцион с уникальным ID
    auctions[auction_id] = {
    "id": auction_id,
    "item": item,
    "description": description,
    "highest_bid": 0,
    "highest_bidder": None,
    "bids": [],
    "end_time": end_time
    }

    # Логируем создание аукциона
    await log_auction_creation(auction_id, auction_name, item, description, end_time)
    
    # Получаем канал по имени (замените на свой канал)
    channel = discord.utils.get(ctx.guild.text_channels, name="test3")
    
    # Проверяем, найден ли канал
    if channel:
        # Отправляем сообщение в канал #auctions1
        auction_message = await channel.send(f"# Auction name: {auction_name} (__ID: {auction_id}__)\n## @everyone, the auction for **{item}** has started!\n### Trait: **{description}**\nBids are accepted for **{format_seconds(duration)}**. To place a bid, use the command: **!bid {auction_id} amount**.")
        auction_messages[auction_id] = auction_message.id
    else:
        await ctx.send("Error: Channel '#auctions1' not found.")
    # Запускаем таймер завершения аукциона
    await asyncio.sleep(duration)
    await endauction(ctx, auction_id)

# Функция для окончания аукциона с уникальным именем
async def endauction(ctx, auction_id: int):
    """Ends the auction, announces the winner, the runner-up, and deducts DKP."""
    global auctions

    # Проверяем, существует ли аукцион с таким ID
    auction = next((auc for auc in auctions.values() if auc["id"] == auction_id), None)
    channel = discord.utils.get(ctx.guild.text_channels, name="test4")

    if not auction:
        await ctx.send(f"No auction found with ID {auction_id}.")
        return

    # Если ставки были, проводим расчет победителя
    if auction["highest_bidder"]:
        winner_id = auction["highest_bidder"]
        dkp_data = await load_dkp_data()

        # Убедимся, что у пользователя есть валидный баланс DKP
        user_data = dkp_data.get(str(winner_id))
        if user_data:
            user_dkp = user_data["dkp"]
            if isinstance(user_dkp, int):  # Проверим, что DKP является целым числом
                new_dkp = max(0, user_dkp - auction["highest_bid"])
                dkp_data[str(winner_id)]["dkp"] = new_dkp
            else:
                await channel.send(f"Error: Invalid DKP value for {winner_id}.")
                return

            await save_dkp_data(dkp_data)

            winner = await bot.fetch_user(winner_id)
            
             # Логируем изменение DKP
            description = f"winner of auction ID {auction_id}"
            await log_dkp_change(winner, auction['highest_bid'], "Remove", description)  # Логирование

            # Определяем топ-3 ставки
            sorted_bids = sorted(auction["bids"], key=lambda b: b["amount"], reverse=True)
            top_3_bids = [
                {"user": await bot.fetch_user(b["user"]), "amount": b["amount"]}
                for b in sorted_bids[:3]
            ]

            # Логируем результаты
            await log_auction_result(auction_id, top_3_bids)
            
             # Получаем канал по имени (замените на свой канал)
    
            # Формируем финальное сообщение
            result_message = f"# The auction with ID **{auction_id}** for item **{auction['item']}** has ended!\n"
            result_message += f"## Winner: **{winner.mention}** with a bid of {auction['highest_bid']} DKP.\n"

            if len(top_3_bids) > 1:
                runner_up = guild.get_member(top_3_bids[1]["user"].id)
                runner_up_bid = top_3_bids[1]["amount"]
                result_message += f"### Second bid: **{runner_up}** with a bid of {runner_up_bid} DKP.\n"

            if len(top_3_bids) > 2:
                third_place = guild.get_member(top_3_bids[1]["user"].id)
                third_place_bid = top_3_bids[2]["amount"]
                result_message += f"### Third bid: **{third_place}** with a bid of {third_place_bid} DKP.\n"
            
            await channel.send(result_message)

        else:
            await channel.send(f"Error: No DKP data for winner with ID {winner_id}.")
    else:
        await channel.send(f"# @everyone, the auction with ID **{auction_id}** (item: {auction['item']}) has ended, but no bids were placed.")

    # Удаляем сообщение о старте аукциона
    channel = discord.utils.get(ctx.guild.text_channels, name="test3")
    auction_message_id = auction_messages.get(auction_id)
    if auction_message_id:
        auction_message = await channel.fetch_message(auction_message_id)
        await auction_message.delete()
    # Удаляем аукцион из списка
    del auctions[auction_id]

# Функция для принудительной остановки аукциона с уникальным именем
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def fendauc(ctx, auction_name: str):
    """Forces the auction with the given name to end and prints the result."""
    global auctions

    # Проверяем, существует ли аукцион с таким ID
    auction = next((auc for auc in auctions.values() if auc["id"] == auction_id), None)

    if not auction:
        await ctx.send(f"No auction found with ID {auction_id}.")
        return

    # Если ставки были, проводим расчет победителя
    if auction["highest_bidder"]:
        winner_id = auction["highest_bidder"]
        dkp_data = await load_dkp_data()

        # Убедимся, что у пользователя есть валидный баланс DKP
        user_data = dkp_data.get(str(winner_id))
        if user_data:
            user_dkp = user_data["dkp"]
            if isinstance(user_dkp, int):  # Проверим, что DKP является целым числом
                new_dkp = max(0, user_dkp - auction["highest_bid"])
                dkp_data[str(winner_id)]["dkp"] = new_dkp
            else:
                await ctx.send(f"Error: Invalid DKP value for {winner_id}.")
                return

            await save_dkp_data(dkp_data)

            winner = await bot.fetch_user(winner_id)
            
             # Логируем изменение DKP
            description = f"winner of auction ID {auction_id}"
            await log_dkp_change(winner, auction['highest_bid'], "Remove", description)  # Логирование

            # Определяем топ-3 ставки
            sorted_bids = sorted(auction["bids"], key=lambda b: b["amount"], reverse=True)
            top_3_bids = [
                {"user": await bot.fetch_user(b["user"]), "amount": b["amount"]}
                for b in sorted_bids[:3]
            ]

            # Логируем результаты
            await log_auction_result(auction_id, top_3_bids)

            # Формируем финальное сообщение
            result_message = f"# The auction with ID **{auction_id}** for item **{auction['item']}** has ended!\n"
            result_message += f"## Winner: **{winner.mention}** with a bid of {auction['highest_bid']} DKP.\n"

            if len(top_3_bids) > 1:
                runner_up = guild.get_member(top_3_bids[1]["user"].id)
                runner_up_bid = top_3_bids[1]["amount"]
                result_message += f"### Second bid: **{runner_up}** with a bid of {runner_up_bid} DKP.\n"

            if len(top_3_bids) > 2:
                third_place = guild.get_member(top_3_bids[1]["user"].id)
                third_place_bid = top_3_bids[2]["amount"]
                result_message += f"### Third bid: **{third_place}** with a bid of {third_place_bid} DKP.\n"

            await ctx.send(result_message)

        else:
            await ctx.send(f"Error: No DKP data for winner with ID {winner_id}.")
    else:
        await ctx.send(f"# @everyone, the auction with ID **{auction_id}** (item: {auction['item']}) has ended, but no bids were placed.")

    # Удаляем аукцион из списка
    del auctions[auction_id]

# Функция для просмотра всех активных аукционов
@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def aucs(ctx):
    """Shows all active auctions."""
    global auctions

    if not auctions:
        await ctx.send("There are no active auctions at the moment.")
        return

    active_auctions_message = "Active Auctions:\n"
    for auction_name, auction in auctions.items():
        remaining_time = auction["end_time"] - time.time()
        active_auctions_message += f"**Auction ID: {auction_name}** - {auction['item']} (Time left: {format_seconds(remaining_time)})\n"

    await ctx.send(active_auctions_message)

async def log_dkp_change(user, amount, action, description=""):
    """Логирование изменений DKP в файл dkp_log.json с добавлением описания."""
    log_file = "dkp_log.json"
    user_id = str(user.id)
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    log_entry = {
        "timestamp": timestamp,
        "action": action.capitalize(),
        "amount": amount,
        "description": description
    }

    try:
        # Проверяем существование файла
        if not os.path.exists(log_file):
            dkp_log = {}  # Создаем новый словарь, если файла нет
        else:
            async with aiofiles.open(log_file, mode="r") as f:
                content = await f.read()
                dkp_log = json.loads(content) if content.strip() else {}

        # Обновляем логи пользователя
        if user_id not in dkp_log:
            dkp_log[user_id] = {
                "display_name": user.display_name,
                "logs": []
            }

        dkp_log[user_id]["logs"].append(log_entry)

        # Записываем обновленный лог в файл
        async with aiofiles.open(log_file, mode="w") as f:
            await f.write(json.dumps(dkp_log, indent=4, ensure_ascii=False))

        print(f"[LOG] Успешно записан лог: {log_entry}")

    except Exception as e:
        print(f"[ERROR] Ошибка при записи в dkp_log.json: {e}")

async def add_dkp(users, amount):
    """Добавляет DKP сразу нескольким пользователям."""
    dkp_data = await load_dkp_data()
    updated_users = []

    for user in users:
        user_id = str(user.id)

        if user_id not in dkp_data:
            dkp_data[user_id] = {
                "display_name": user.display_name,
                "dkp": 0
            }

        dkp_data[user_id]["dkp"] += amount
        updated_users.append(user.display_name)

    await save_dkp_data(dkp_data)

    print(f"[DKP] Added {amount} DKP to users: {', '.join(updated_users)}")  # Лог в консоль

async def sub_dkp(users, amount):
    """Удаляет DKP сразу у нескольких пользователей."""
    dkp_data = await load_dkp_data()
    updated_users = []

    for user in users:
        user_id = str(user.id)

        if user_id not in dkp_data:
            dkp_data[user_id] = {
                "display_name": user.display_name,
                "dkp": 0
            }

        dkp_data[user_id]["dkp"] = max(0, dkp_data[user_id]["dkp"] - amount)
        updated_users.append(user.display_name)

    await save_dkp_data(dkp_data)

    print(f"[DKP] Removed {amount} DKP from users: {', '.join(updated_users)}")  # Лог в консоль

# Command to add DKP points
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def adddkp(ctx, amount: int, description: str, *users: discord.Member):
    """Adds DKP points to a user."""
    await add_dkp(users, amount)  # Asynchronous call
     # Формируем список получателей
    user_names = ", ".join(user.display_name for user in users)
    await ctx.send(f"{user_names} has received {amount} DKP!\nReason: {description}.")
    for user in users:
        await log_dkp_change(user, amount, "added", description)

# Command to subtract DKP points
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def subdkp(ctx, amount: int, description: str, *users: discord.Member):
    """Removes DKP points from a user."""
    await sub_dkp(users, amount)  # Asynchronous call
    user_names = ", ".join(user.display_name for user in users)
    await ctx.send(f"{user_names} has lost {amount} DKP!\nReason: {description}.")
    for user in users:
        await log_dkp_change(user, amount, "removed", description)

@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def mydkp(ctx):
    """Shows the DKP points of the user who called the command."""
    dkp_data = await load_dkp_data()
    user_id = str(ctx.author.id)  # ID пользователя, который вызвал команду
    user_data = dkp_data.get(user_id, {"dkp": 0})
    dkp_points = user_data["dkp"]
    await ctx.send(f"{ctx.author.display_name} has {dkp_points} DKP.")

# Обработчик ошибки, если команда используется слишком часто
@mydkp.error
async def mydkp_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")

@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def dkp(ctx, user: discord.Member):
    """Shows a user's DKP points."""
    dkp_data = await load_dkp_data()
    user_data = dkp_data.get(str(user.id), {"dkp": 0})
    dkp_points = user_data["dkp"]
    await ctx.send(f"{user.display_name} has {dkp_points} DKP.")

# Обработчик ошибки, если команда используется слишком часто
@dkp.error
async def dkp_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")


@bot.command()
@commands.cooldown(1, 30, commands.BucketType.guild)
async def topdkp(ctx):
    """Displays the top users with the highest DKP points."""
    dkp_data = await load_dkp_data()

    if not dkp_data:
        await ctx.send("No DKP data available.")
        return

    # Sort users by DKP points (descending order)
    top_users = sorted(dkp_data.items(), key=lambda x: x[1]["dkp"], reverse=True)

    # Create a message with the top 10 users
    top_message = "**🏆 Top DKP Players:**\n"
    for idx, (user_id, user_data) in enumerate(top_users[:10], 1):
        try:
            user = await bot.fetch_user(int(user_id))  # Fetch user object
            top_message += f"{idx}. {ctx.guild.get_member(user.id).display_name} — {user_data['dkp']} DKP\n"
        except:
            top_message += f"{idx}. Unknown user (ID {user_id}) — {user_data['dkp']} DKP\n"

    await ctx.send(top_message)
# Обработчик ошибки, если команда используется слишком часто
@topdkp.error
async def topdkp_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")
@bot.command()
@commands.cooldown(1, 30, commands.BucketType.guild) 
async def alldkp(ctx):
    """Displays a list of all users and their DKP points."""
    dkp_data = await load_dkp_data()
    
    if not dkp_data:
        await ctx.send("No users with DKP found.")
        return

    # Sort by DKP points in descending order
    all_users = sorted(dkp_data.items(), key=lambda x: x[1]["dkp"], reverse=True)

    # Create a message with DKP for each user
    all_dkp_message = "**📜 All Players and Their DKP:**\n"
    for user_id, user_data in all_users:
        try:
            user = await bot.fetch_user(int(user_id))
            all_dkp_message += f"{ctx.guild.get_member(user.id).display_name}: {user_data['dkp']} DKP\n"
        except:
            all_dkp_message += f"Unknown user (ID {user_id}): {user_data['dkp']} DKP\n"

    await ctx.send(all_dkp_message)
# Обработчик ошибки, если команда используется слишком часто
@alldkp.error
async def alldkp_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def duser(ctx, user: discord.Member):
    """Removes a user from the DKP database.."""
    dkp_data = await load_dkp_data()
    user_id = str(user.id)

    if user_id not in dkp_data:
        await ctx.send(f"{user.mention} is not in the DKP database.")
        return

    del dkp_data[user_id]  # Удаляем пользователя из базы
    await save_dkp_data(dkp_data)

    await ctx.send(f"{user.mention} has been removed from the DKP database.")

bot.remove_command("help")
@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
async def help(ctx):
    embed = discord.Embed(title="Help Menu", description="List of available commands", color=discord.Color.blue())

    embed.add_field(name="🛒 Auctions", value="!mybids - All users active bids\n!bid <auctionID> <amount> - Place a bid\n!bids <auctionID> - All members bids\n!aucs - list of all active auctions\n!dbid <auctionID> - Delete your bid", inline=False)
    embed.add_field(name="📊 DKP System", value="!dkp <user> - Show DKP\n!alldkp - list of all members points\n!topdkp - list of top 10 members", inline=False)
 
    await ctx.send(embed=embed)
# Обработчик ошибки, если команда используется слишком часто
@help.error
async def help_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")
    
@bot.command()
async def ahelp(ctx):
    embed = discord.Embed(title="Help Menu", description="List of available commands", color=discord.Color.blue())

    embed.add_field(name="🛠 Admin Commands", value="!duser <user> - Removes a user\n!subdkp <user> <amount> - Removes DKP points\n!adddkp <user> <amount> - Adds DKP points\n!fendauc <auction> - End auction manualy\n!sauc <name> <item> <trait> <duration> - Start an auction\n!updm_names - update all members display names in data\n!add_members - add all new members", inline=False)

    await ctx.send(embed=embed)

@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def log(ctx, user: discord.Member):
    """Shows the DKP log history for a user."""
    log_file = "dkp_log.json"
    user_id = str(user.id)

    try:
        # Проверяем существование файла
        if not os.path.exists(log_file):
            await ctx.send("No DKP log file found.")
            return

        # Загружаем логи
        async with aiofiles.open(log_file, mode="r") as f:
            content = await f.read()
            dkp_logs = json.loads(content) if content.strip() else {}

        # Проверяем, есть ли логи для данного пользователя
        if user_id not in dkp_logs or "logs" not in dkp_logs[user_id]:
            await ctx.send(f"No logs found for {user.mention}.")
            return

        # Преобразуем записи логов в нужный формат
        formatted_logs = [
            f"{entry['timestamp']}, '{entry['action']}', {entry['amount']}, '{entry['description']}'"
            for entry in dkp_logs[user_id]["logs"]
        ]

        # Ограничиваем количество записей (например, до 10)
        log_text = "\n".join(formatted_logs[-10:])

        await ctx.send(f"**DKP Log for {ctx.guild.get_member(user.id).display_name}:**\n```{log_text}```")

    except Exception as e:
        await ctx.send(f"Error fetching logs: {e}")
# Обработчик ошибки, если команда используется слишком часто
@log.error
async def log_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")

@bot.command()
@commands.cooldown(1, 30, commands.BucketType.user)
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def alog(ctx, auction_id: int):
    """Shows the auction log history for a given auction ID."""
    auction_file = "auc_log.json"  # Укажи правильное название файла

    try:
        # Проверяем существование файла
        if not os.path.exists(auction_file):
            await ctx.send("No auction log file found.")
            return

        # Загружаем логи
        async with aiofiles.open(auction_file, mode="r") as f:
            content = await f.read()
            auctions_data = json.loads(content) if content.strip() else {}

        # Ищем нужный аукцион по ID
        auction = next(
            (auc for auc in auctions_data["auctions"].values() if auc["id"] == auction_id),
            None
        )

        if not auction:
            await ctx.send(f"No auction logs found for ID {auction_id}.")
            return

        # Формируем сообщение
        log_text = f"**Auction ID:** {auction['id']}\n"
        log_text += f"**Name:** {auction['name']}\n"
        log_text += f"**Item:** {auction['item']}\n"
        log_text += f"**Description:** {auction['description']}\n"
        log_text += f"**Created At:** {auction['created_at']}\n"
        log_text += f"**Ended At:** {auction['date_of_end']}\n"

        if auction["top_3_bids"]:
            log_text += "**Top 3 Bids:**\n"
            for bid in auction["top_3_bids"]:
                log_text += f"User ID: {bid['user_id']}, Amount: {bid['amount']} DKP\n"
        else:
            log_text += "**No bids placed.**"

        await ctx.send(f"```{log_text}```")

    except Exception as e:
        await ctx.send(f"Error fetching auction history: {e}")
# Обработчик ошибки, если команда используется слишком часто
@alog.error
async def alog_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"{ctx.author.mention}, wait {error.retry_after:.1f} s before next command reuse!")


@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
# Функция для загрузки файлов на GitHub
def upload_files_to_github(github_token, repo_name, files, folder="reserv"):
    # Авторизация через токен
    g = Github(github_token)
    repo = g.get_repo(repo_name)

    # Проверяем существование папки в репозитории
    try:
        contents = repo.get_contents(folder)
    except:
        # Если папки нет, создаем её
        contents = repo.create_file(f"{folder}/.empty", "Initial commit to create the folder", "")

    # Загрузка файлов в репозиторий
    for file_path in files:
        file_name = os.path.basename(file_path)
        with open(file_path, "r") as f:
            content = f.read()
        
        # Проверяем, существует ли файл, и обновляем его, если нужно
        try:
            file_in_repo = repo.get_contents(f"{folder}/{file_name}")
            repo.update_file(file_in_repo.path, f"Update {file_name}", content, file_in_repo.sha)
            print(f"Updated {file_name} in repository.")
        except:
            # Если файл не существует, создаем новый
            repo.create_file(f"{folder}/{file_name}", f"Upload {file_name}", content)
            print(f"Uploaded {file_name} to repository.")


# Загружаем данные при старте
dkp_data = load_dkp_data()

# Запуск бота
bot.run(DISCORD_TOKEN)
