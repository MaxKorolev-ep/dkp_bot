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
from discord.ext import commands, tasks
from discord import app_commands, ui, Interaction, Embed

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
# Храним время последних ставок: {auction_id: {user_id: timestamp}}
last_bid_times = {}
# Храним время последнего удаления ставки (ключ - user_id)
last_dbid_times = {}
# Список доступных ролей
AVAILABLE_ROLES = ["Tank", "DD", "Healer"]

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

# Функция для автодополнения списка активных аукционов (добавляем описание)
async def auction_autocomplete(interaction: discord.Interaction, current: str):
    """Предлагает пользователю список активных аукционов с их описанием"""
    return [
        app_commands.Choice(
            name=f"ID {auc_id}: {data['item']} ({data['description']})", 
            value=str(auc_id)
        )
        for auc_id, data in auctions.items()
        if current.lower() in str(auc_id) or current.lower() in data["item"].lower() or current.lower() in data["description"].lower()
    ][:25]  # Discord разрешает максимум 25 вариантов

# Функция для автодополнения списка активных ролей (добавляем описание)
async def role_autocomplete(interaction: discord.Interaction, current: str):
    """Автодополнение списка ролей."""
    return [
        app_commands.Choice(name=role, value=role)
        for role in AVAILABLE_ROLES
        if current.lower() in role.lower()
    ]

# Функция для добавления ролей
@bot.tree.command(name="addroles", description="Add yourself one or multiple roles: Tank, DD, Healer.")
@app_commands.autocomplete(roles=role_autocomplete)
async def add_roles(interaction: discord.Interaction, roles: str):
    """Позволяет пользователю добавить себе одну или несколько ролей."""
    guild = interaction.guild
    member = interaction.user

    # Разделяем введенные роли по запятой и убираем лишние пробелы
    selected_roles = [role.strip() for role in roles.split(",") if role.strip() in AVAILABLE_ROLES]

    if not selected_roles:
        await interaction.response.send_message(f"❌ No valid roles selected. Available roles: {', '.join(AVAILABLE_ROLES)}.", ephemeral=True)
        return

    added_roles = []
    for role_name in selected_roles:
        role_obj = discord.utils.get(guild.roles, name=role_name)
        if role_obj and role_obj not in member.roles:
            await member.add_roles(role_obj)
            added_roles.append(role_name)

    if added_roles:
        await interaction.response.send_message(f"✅ Added roles: {', '.join(added_roles)}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ You already have all selected roles.", ephemeral=True)

# Функция для показа людей из списка ролей
@bot.tree.command(name="listrole", description="Shows all members with the specified role.")
async def list_role(interaction: discord.Interaction, role: discord.Role):
    """Показывает всех пользователей с данной ролью."""
    members_with_role = [member.mention for member in role.members]

    if not members_with_role:
        await interaction.response.send_message(f"❌ No members have the role **{role.name}**.", ephemeral=True)
        return

    members_list = "\n".join(members_with_role)
    embed = discord.Embed(
        title=f"Members with role: {role.name}",
        description=members_list,
        color=role.color
    )

    await interaction.response.send_message(embed=embed)
            
@bot.command()
@commands.has_any_role('Leader')
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
@commands.has_any_role('Leader')
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
@bot.tree.command(name="mybids", description="Show all users bids.")
async def mybids(interaction: discord.Interaction):
    """Displays all active bids placed by the user."""
    global auctions
    user = interaction.user
    user_bids = [
        f"**{name}** - {auction['item']}: **{bid['amount']} DKP**"
        for name, auction in auctions.items()
        for bid in auction["bids"]
        if bid["user"] == user.id
    ]

    if user_bids:
        await interaction.response.send_message(f"Your active bids:\n" + "\n".join(user_bids), ephemeral=True)
    else:
        await interaction.response.send_message("You have no active bids.", ephemeral=True)

# Функция для просмотра ставок на конкретный аукцион
@bot.tree.command(name="bids", description="Places a bid on an auction using the auction ID.")
@app_commands.autocomplete(auction_id=auction_autocomplete)
async def bids(interaction: discord.Interaction, auction_id: str):
    auction_id = int(auction_id)  # Преобразуем в int, так как autocomplete возвращает str
    if auction_id not in auctions:
        await interaction.response.send_message(f"Auction with ID '{auction_id}' does not exist.", ephemeral=True)
        return

    auction = auctions[auction_id]

    if not auction["bids"]:
        await interaction.response.send_message(f"No bids for **{auction['item']}** (Auction ID: {auction_id}).")
        return

    bids_message = f"Bids for **{auction['item']}** (Auction ID: {auction_id}):\n"
    for bid in auction["bids"]:
        guild = interaction.guild  # Get the server (guild)
        member = guild.get_member(bid["user"])  # Get the member from the guild

        if member:
            display_name = member.display_name  # Get the server nickname
        else:
            display_name = "Unknown User"  # Fallback in case the user is not in the server
        bids_message += f"{display_name}: {bid['amount']} DKP\n"
    await interaction.response.send_message(bids_message)

# Функция для ставки на конкретный аукцион
@bot.tree.command(name="bid", description="Places a bid on an auction using the auction ID.")
@app_commands.autocomplete(auction_id=auction_autocomplete)
async def bid(interaction: discord.Interaction, auction_id: str, amount: int):
    """Размещает ставку на аукцион с указанным ID. Если ставка перебита, предыдущему лидеру возвращается DKP."""
    user = interaction.user
    auction_id = int(auction_id)
    current_time = time.time()

    channel = discord.utils.get(interaction.guild.text_channels, name="💰bidschannel💰")
    if not channel:
        await interaction.response.send_message("Error: Channel '💰bidschannel💰' not found.", ephemeral=True)
        return

    auction = auctions.get(auction_id)
    if not auction:
        await interaction.response.send_message(f"Auction with ID '{auction_id}' does not exist.", ephemeral=True)
        return

    if current_time > auction["end_time"]:
        await interaction.response.send_message(f"The auction for **{auction['item']}** has ended!", ephemeral=True)
        return

    highest_bid = auction.get("highest_bid", 0)
    highest_bidder = auction.get("highest_bidder", None)  # ID текущего лидера

    # **Проверяем таймер (30 минут между ставками на один аукцион)**
    if auction_id in last_bid_times and user.id in last_bid_times[auction_id]:
        last_bid_time = last_bid_times[auction_id][user.id]
        time_since_last_bid = current_time - last_bid_time

        if time_since_last_bid < 10:  # 1800 секунд = 30 минут
            remaining_time = 10 - time_since_last_bid
            minutes = int(remaining_time // 60)
            seconds = int(remaining_time % 60)
            await interaction.response.send_message(
                f"⏳ {user.mention}, you can bid again in {minutes}m {seconds}s.",
                ephemeral=True
            )
            return
            
    if amount <= highest_bid + 99:
        await interaction.response.send_message(
            f"❌ Your bid must be **higher on 100** than the current highest bid (**{highest_bid} DKP**).",
            ephemeral=True
        )
        return

    # Загружаем данные DKP
    dkp_data = await load_dkp_data()
    user_dkp = dkp_data.get(str(user.id), {"dkp": 0})["dkp"]

    # Подсчет уже заблокированных (использованных) DKP
    locked_dkp = sum(
        bid["amount"] for auc in auctions.values() for bid in auc["bids"] if bid["user"] == user.id
    )

    # Проверяем, может ли пользователь сделать ставку
    available_dkp = user_dkp - locked_dkp  # Свободные DKP
    if amount > available_dkp:
        await interaction.response.send_message(
            f"❌ You only have **{available_dkp} DKP** available to bid. You cannot place this bid.",
            ephemeral=True
        )
        return

    # **Возвращаем DKP предыдущему лидеру (разблокируем, но не увеличиваем баланс)**
    if highest_bidder:
        prev_bid = next((b for b in auction["bids"] if b["user"] == highest_bidder), None)
        if prev_bid:
            auction["bids"].remove(prev_bid)  # Удаляем ставку из списка

        prev_leader = await bot.fetch_user(highest_bidder)
        await channel.send(f"🔄 {prev_leader.mention}, your **{highest_bid} DKP** have been unlocked.")

    # **Обновляем аукцион с новой ставкой**
    auction["highest_bid"] = amount
    auction["highest_bidder"] = user.id
    auction["bids"].append({"user": user.id, "amount": amount})
    
    time_left = auction["end_time"] - current_time
    if time_left < 300:  # 300 секунд = 5 минут
      auction["end_time"] = current_time + 300
      await channel.send(f"⏱️ Time extended! New end time for auction {auction_id} is in 5 minutes due to recent bid.")

    # **Обновляем таймер для пользователя**
    if auction_id not in last_bid_times:
        last_bid_times[auction_id] = {}
    last_bid_times[auction_id][user.id] = current_time

    embed = discord.Embed(
        description=f"## {user.display_name} placed a bid of **{amount} DKP**.\n"
                    f"## Auction ID: **{auction_id}**\n"
                    f"## Item: **{auction['item']}**.",
        color=discord.Color.green()
    )

    await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ Your bid of {amount} DKP for **{auction['item']}** has been placed.", ephemeral=True)

# Функция для удаления ставки на конкретный аукцион
@bot.tree.command(name="dbid", description="Admin removes a specific user's bid from an auction.")
@app_commands.autocomplete(auction_id=auction_autocomplete)
async def dbid(interaction: discord.Interaction, auction_id: str, member: discord.Member):
    """Администратор может удалить ставку конкретного пользователя по ID аукциона."""
    global auctions
    auction_id = int(auction_id)  # Преобразуем в int
    user = interaction.user

    # Проверяем, является ли пользователь администратором
    if not any(role.permissions.administrator for role in user.roles):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return

    # Проверяем, существует ли аукцион
    if auction_id not in auctions:
        await interaction.response.send_message(f"Auction with ID '{auction_id}' does not exist.", ephemeral=True)
        return

    auction = auctions[auction_id]

    # Проверяем, есть ли ставка от выбранного пользователя
    existing_bid = next((b for b in auction["bids"] if b["user"] == member.id), None)
    if not existing_bid:
        await interaction.response.send_message(f"❌ {member.display_name} has no bid in auction ID '{auction_id}'.", ephemeral=True)
        return

    # Удаляем ставку пользователя
    auction["bids"].remove(existing_bid)

    # Возвращаем баланс DKP пользователю
    dkp_data = await load_dkp_data()
    user_dkp = dkp_data.get(str(member.id), {"dkp": 0})["dkp"]
    dkp_data[str(member.id)]["dkp"] = user_dkp + existing_bid["amount"]
    await save_dkp_data(dkp_data)

    # Обновляем максимальную ставку
    if auction["bids"]:
        highest_bid = max(auction["bids"], key=lambda b: b["amount"])
        auction["highest_bid"] = highest_bid["amount"]
        auction["highest_bidder"] = highest_bid["user"]
    else:
        auction["highest_bid"] = 0
        auction["highest_bidder"] = None

    # Ищем канал для уведомления
    channel = discord.utils.get(interaction.guild.text_channels, name="💰bidschannel💰")
    if not channel:
        await interaction.response.send_message("Error: Channel '💰bidschannel💰' not found.", ephemeral=True)
        return

    # Отправляем уведомление
    embed = discord.Embed(
        description=f"## Admin {user.display_name} removed {member.display_name}'s bid from auction ID '{auction_id}'.\n"
                    f"## {existing_bid['amount']} DKP returned to {member.mention}.",
        color=discord.Color.red()
    )

    await channel.send(embed=embed)
    await interaction.response.send_message(
        f"✅ {member.display_name}'s bid has been removed from auction ID '{auction_id}', and their DKP has been refunded.",
        ephemeral=True
    )
        
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
@commands.has_any_role('Leader')
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
    channel = discord.utils.get(ctx.guild.text_channels, name="📢liveauctions📢")
    
    # Проверяем, найден ли канал
    if channel:
        # Отправляем сообщение в канал #auctions1
        embed = discord.Embed(
        title=f"Auction boss: {auction_name} (__ID: {auction_id}__)",
        description=f"# @everyone, the auction has started!\n"
                f"## Item: {item}\n"
                f"## Trait: {description}\n"
                f"### Bids are accepted for __{format_seconds(duration)}__.\n"
                f"### To place a bid, use the command: __/bid {auction_id} amount__.",
        color=discord.Color.random()  # Можно заменить на любой цвет, например, red, blue, purple и т. д.
        )
        # Отправляем сообщение в канал #auctions1 с встраиваемым сообщением
        auction_message = await channel.send(embed=embed)
        auction_messages[auction_id] = auction_message.id
    else:
        await ctx.send("Error: Channel '#auctions1' not found.")
        return  # ❗ Важно: остановить если нет канала

    # 🛡 Только после ВСЕХ записей запускаем фонового следящего
    async def auction_watcher():
        while True:
            await asyncio.sleep(5)  # сначала спим, чтобы не дергать каждый тик
            now = time.time()
            if auction_id not in auctions:
                break  # если аукцион удалили вручную
            if now >= auctions[auction_id]["end_time"]:
                await endauction(ctx, auction_id)
                break

    bot.loop.create_task(auction_watcher())

# Функция для окончания аукциона с уникальным именем
async def endauction(ctx, auction_id: int):
    """Ends the auction, announces the winner, the runner-up, and deducts DKP."""
    global auctions

    # Проверяем, существует ли аукцион с таким ID
    auction = next((auc for auc in auctions.values() if auc["id"] == auction_id), None)
    channel = discord.utils.get(ctx.guild.text_channels, name="🏆auctionsresult🏆")

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
            
            #Формируем финальное сообщение
            result_message = f"## Winner: **{winner.mention}** with a bid of {auction['highest_bid']} DKP.\n"

            if len(top_3_bids) > 1:
                runner_up = ctx.guild.get_member(top_3_bids[1]["user"].id)
                runner_up_bid = top_3_bids[1]["amount"]
                result_message += f"### Second bid: **{runner_up.display_name}** with a bid of {runner_up_bid} DKP.\n"

            if len(top_3_bids) > 2:
                third_place = ctx.guild.get_member(top_3_bids[2]["user"].id)
                third_place_bid = top_3_bids[2]["amount"]
                result_message += f"### Third bid: **{third_place.display_name}** with a bid of {third_place_bid} DKP.\n"
            
            embed = discord.Embed(
                description=f"# @everyone, the auction with ID **{auction_id}** for item **{auction['item']}: {auction['description']}** has ended!\n{result_message}",
                color=discord.Color.random()  # Можно заменить на любой цвет, например, red, blue, purple и т. д.
            )
            
            await channel.send(embed=embed)

        else:
            embed = discord.Embed(
                title=f"Error",
                description=f"No DKP data for winner with ID {winner_id}.",
                color=discord.Color.red()  # Можно заменить на любой цвет, например, red, blue, purple и т. д.
            )
            await channel.send(embed=embed)
    else:
        embed = discord.Embed(
                description=f"# @everyone, the auction with ID **{auction_id}** (item: {auction['item']}: {auction['description']}) has ended\n"
                            f"## But no bids were placed.",
                color=discord.Color.red()  # Можно заменить на любой цвет, например, red, blue, purple и т. д.
            )
        await channel.send(embed=embed)

    # Удаляем сообщение о старте аукциона
    channel = discord.utils.get(ctx.guild.text_channels, name="📢liveauctions📢")
    auction_message_id = auction_messages.get(auction_id)
    if auction_message_id:
        auction_message = await channel.fetch_message(auction_message_id)
        await auction_message.delete()
    # Удаляем аукцион из списка
    del auctions[auction_id]

# Функция для принудительной остановки аукциона с уникальным именем
@bot.command()
@commands.has_any_role('Leader')
async def fendauc(ctx, auction_id: int):
    """Ends the auction, announces the winner, the runner-up, and deducts DKP."""
    global auctions

    # Проверяем, существует ли аукцион с таким ID
    auction = next((auc for auc in auctions.values() if auc["id"] == auction_id), None)
    channel = discord.utils.get(ctx.guild.text_channels, name="🏆auctionsresult🏆")

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
            
            #Формируем финальное сообщение
            result_message = f"## Winner: **{winner.mention}** with a bid of {auction['highest_bid']} DKP.\n"

            if len(top_3_bids) > 1:
                runner_up = ctx.guild.get_member(top_3_bids[1]["user"].id)
                runner_up_bid = top_3_bids[1]["amount"]
                result_message += f"### Second bid: **{runner_up.display_name}** with a bid of {runner_up_bid} DKP.\n"

            if len(top_3_bids) > 2:
                third_place = ctx.guild.get_member(top_3_bids[2]["user"].id)
                third_place_bid = top_3_bids[2]["amount"]
                result_message += f"### Third bid: **{third_place.display_name}** with a bid of {third_place_bid} DKP.\n"
            
            embed = discord.Embed(
                description=f"# @everyone, the auction with ID **{auction_id}** for item **{auction['item']}** has ended!\n{result_message}",
                color=discord.Color.random()  # Можно заменить на любой цвет, например, red, blue, purple и т. д.
            )
            
            await channel.send(embed=embed)

        else:
            embed = discord.Embed(
                title=f"Error",
                description=f"No DKP data for winner with ID {winner_id}.",
                color=discord.Color.red()  # Можно заменить на любой цвет, например, red, blue, purple и т. д.
            )
            await channel.send(embed=embed)
    else:
        embed = discord.Embed(
                description=f"# @everyone, the auction with ID **{auction_id}** (item: {auction['item']}) has ended\n"
                            f"## But no bids were placed.",
                color=discord.Color.red()  # Можно заменить на любой цвет, например, red, blue, purple и т. д.
            )
        await channel.send(embed=embed)

    # Удаляем сообщение о старте аукциона
    channel = discord.utils.get(ctx.guild.text_channels, name="📢liveauctions📢")
    auction_message_id = auction_messages.get(auction_id)
    if auction_message_id:
        auction_message = await channel.fetch_message(auction_message_id)
        await auction_message.delete()
    # Удаляем аукцион из списка
    del auctions[auction_id]

# Функция для просмотра всех активных аукционов
@bot.tree.command(name="aucs", description="Shows all list of active auctions")
async def aucs(interaction: discord.Interaction):
    """Shows all active auctions, including the current highest bid and bidder."""
    global auctions

    if not auctions:
        await interaction.response.send_message("There are no active auctions at the moment.", ephemeral=True)
        return

    active_auctions_message = "**🎯 Active Auctions:**\n"
    for auction_id, auction in auctions.items():
        remaining_time = auction["end_time"] - time.time()
        highest_bid = auction.get("highest_bid", 0)
        highest_bidder_id = auction.get("highest_bidder")

        # Получаем имя пользователя, если есть
        if highest_bidder_id:
            try:
                highest_bidder = await bot.fetch_user(highest_bidder_id)
                member = interaction.guild.get_member(highest_bidder_id)
                bidder_name = member.display_name if member else "Unknown"
            except:
                bidder_name = "Unknown"
            bid_info = f" | 💰 Highest Bid: **{highest_bid} DKP** by **{bidder_name}**"
        else:
            bid_info = " | 💰 No bids yet"

        active_auctions_message += (
            f"**ID: {auction_id}** - {auction['item']} "
            f"(⏳ Time left: {format_seconds(remaining_time)}){bid_info}\n"
        )

    await interaction.response.send_message(active_auctions_message, ephemeral=True)


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
@commands.has_any_role('Leader')
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
@commands.has_any_role('Leader')
async def subdkp(ctx, amount: int, description: str, *users: discord.Member):
    """Removes DKP points from a user."""
    await sub_dkp(users, amount)  # Asynchronous call
    user_names = ", ".join(user.display_name for user in users)
    await ctx.send(f"{user_names} has lost {amount} DKP!\nReason: {description}.")
    for user in users:
        await log_dkp_change(user, amount, "removed", description)

# Command to checking DKP points
@bot.tree.command(name="mydkp", description="Shows your DKP points")
async def mydkp(interaction: discord.Interaction):
    """Shows the DKP points of the user who called the command."""
    dkp_data = await load_dkp_data()
    user_id = str(interaction.user.id)  # ID пользователя, который вызвал команду
    user_data = dkp_data.get(user_id, {"dkp": 0})
    dkp_points = user_data["dkp"]

    await interaction.response.send_message(f"{interaction.user.display_name} has {dkp_points} DKP.", ephemeral=True)

@bot.tree.command(name="dkp", description="Shows a user's DKP points")
async def dkp(interaction: discord.Interaction, user: discord.Member):
    """Shows a user's DKP points."""
    await interaction.response.defer()
    print(f"11111111")
    dkp_data = await load_dkp_data()
    print(f"sdfdsfds")

    user_data = dkp_data.get(str(user.id), {"dkp": 0})
    dkp_points = user_data["dkp"]
    await interaction.followup.send(f"{user.display_name} has {dkp_points} DKP.")
    

#Show DKP of top10 members
@bot.tree.command(name="topdkp", description="Shows top10 users")
async def topdkp(interaction: discord.Interaction):
    """Displays the top users with the highest DKP points."""
    dkp_data = await load_dkp_data()

    if not dkp_data:
        await interaction.response.send_message("No DKP data available.")
        return

    # Sort users by DKP points (descending order)
    top_users = sorted(dkp_data.items(), key=lambda x: x[1]["dkp"], reverse=True)

    # Create a message with the top 10 users
    top_message = "**🏆 Top DKP Players:**\n"
    for idx, (user_id, user_data) in enumerate(top_users[:10], 1):
        try:
            user = await interaction.client.fetch_user(int(user_id))  # ✅ Получаем пользователя правильно
            guild_user = interaction.guild.get_member(user.id)  # ✅ Теперь получаем имя в гильдии
            display_name = guild_user.display_name if guild_user else user.name
            top_message += f"{idx}. {display_name} — {user_data['dkp']} DKP\n"
        except:
            top_message += f"{idx}. Unknown user (ID {user_id}) — {user_data['dkp']} DKP\n"

    await interaction.response.send_message(top_message)

#Show DKP of all members
@bot.tree.command(name="alldkp", description="Shows all users") 
async def alldkp(interaction: discord.Interaction):
    """Displays a list of all users and their DKP points."""
    user = interaction.user
        # Проверяем, является ли пользователь администратором
    if not any(role.permissions.administrator for role in user.roles):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    
    await interaction.response.defer()  # ✅ Сообщаем Discord, что команда обрабатывается

    dkp_data = await load_dkp_data()
    
    if not dkp_data:
        await interaction.followup.send("No users with DKP found.")  # ✅ Используем followup
        return

    # Sort by DKP points in descending order
    all_users = sorted(dkp_data.items(), key=lambda x: x[1]["dkp"], reverse=True)

    # Create a message with DKP for each user
    all_dkp_message = "**📜 All Players and Their DKP:**\n"
    for user_id, user_data in all_users:
        try:
            user = await interaction.client.fetch_user(int(user_id))
            guild_user = interaction.guild.get_member(user.id)
            display_name = guild_user.display_name if guild_user else user.name
            all_dkp_message += f"{display_name}: {user_data['dkp']} DKP\n"
        except:
            all_dkp_message += f"Unknown user (ID {user_id}): {user_data['dkp']} DKP\n"

    await interaction.followup.send(all_dkp_message)  # ✅ Используем followup для отправки ответа

#Delete user from Data
@bot.command()
@commands.has_any_role('Leader')
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

#Open help tab
bot.remove_command("help")
@bot.tree.command(name="help", description="Displays available commands.")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(title="Help Menu", description="List of available commands", color=discord.Color.blue())

    embed.add_field(name="🛒 Auctions", value="/mybids - All users active bids\n/bid <auctionID> <amount> - Place a bid\n/bids <auctionID> - All members bids\n/aucs - list of all active auctions\n/dbid <auctionID> - Delete your bid", inline=False)
    embed.add_field(name="📊 DKP System", value="/mydkp - Show DKP/n/dkp <user> - Show users DKP\n/alldkp - list of all members points\n/topdkp - list of top 10 members", inline=False) 
    await interaction.response.send_message(embed=embed, ephemeral=True)  # ephemeral=True – только для пользователя

#Open admin help tab 
@bot.command()
async def ahelp(ctx):
    embed = discord.Embed(title="Help Menu", description="List of available commands", color=discord.Color.blue())

    embed.add_field(name="🛠 Admin Commands", value="!upload_git <> - upload reserv to git\n!dload_git - download from git reserv to git main!\n dload_loc - download from git reserv to local\n!duser <user> - Removes a user\n!subdkp <amount> <reason> <users> - Removes DKP points\n!adddkp <amount> <reason> <users> - Adds DKP points\n!fendauc <auction> - End auction manualy\n!sauc <name> <item> <trait> <duration> - Start an auction\n!updm_names - update all members display names in data\n!add_members - add all new members", inline=False)

    await ctx.send(embed=embed)

#Open users log
@bot.command()
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

#Open aucs log
@bot.command()
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

# Функция для загрузки файлов на GitHub
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def upload_git(ctx, github_token: str, repo_name: str, *files):
    """Загружает файлы в папку на GitHub."""
    
    # Авторизация через токен
    g = Github(github_token)
    repo = g.get_repo(repo_name)

    # Проверяем существование папки в репозитории
    try:
        contents = repo.get_contents("reserv")
    except:
        # Если папки нет, создаем её
        contents = repo.create_file("reserv/.empty", "Initial commit to create the folder", "")

    # Загрузка файлов в репозиторий
    for file_path in files:
        file_name = os.path.basename(file_path)
        with open(file_path, "r") as f:
            content = f.read()
        
        # Проверяем, существует ли файл, и обновляем его, если нужно
        try:
            file_in_repo = repo.get_contents(f"reserv/{file_name}")
            repo.update_file(file_in_repo.path, f"Update {file_name}", content, file_in_repo.sha)
            await ctx.send(f"Updated {file_name} in repository.")
        except:
            # Если файл не существует, создаем новый
            repo.create_file(f"reserv/{file_name}", f"Upload {file_name}", content)
            await ctx.send(f"Uploaded {file_name} to repository.")

# Функция для загрузки файлов из GitHub в GitHub резерв
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def dload_git(ctx, github_token: str, repo_name: str, files=None):
    if files is None:
        files = ["dkp_log.json", "auc_log.json", "dkp_data.json"]  # Список файлов по умолчанию
    
    # Авторизация через токен
    g = Github(github_token)
    repo = g.get_repo(repo_name)
    
    # Проходим по каждому файлу и загружаем его из папки reserv
    for file_name in files:
        try:
            # Получаем содержимое файла из папки 'reserv'
            file_content = repo.get_contents(f"reserv/{file_name}")
            content = file_content.decoded_content.decode("utf-8")

            # Проверяем, существует ли файл в корневой папке репозитория
            try:
                # Получаем информацию о файле в корневой папке репозитория
                file_in_repo = repo.get_contents(file_name)
                # Обновляем файл, если он существует
                repo.update_file(file_in_repo.path, f"Update {file_name}", content, file_in_repo.sha)
                await ctx.send(f"Successfully updated {file_name} in the root directory.")
            except:
                # Если файла нет в корневой папке, создаем его
                repo.create_file(file_name, f"Upload {file_name}", content)
                await ctx.send(f"Successfully uploaded {file_name} to the root directory.")
        
        except Exception as e:
            await ctx.send(f"Error downloading {file_name} from 'reserv' or uploading to root: {e}")

# Команда для скачивания файлов из папки reserv и сохранения в локальной папке проекта
@bot.command()
@commands.has_any_role('Admin', 'Moderator', 'Leader')
async def dload_loc(ctx, github_token: str, repo_name: str, files=None, local_folder=None):
    if files is None:
        files = ["dkp_log.json", "auc_log.json", "dkp_data.json"]  # Список файлов по умолчанию
    
    # Папка для сохранения файлов (если не указана, сохраняем в текущей папке проекта)
    if local_folder is None:
        local_folder = os.getcwd()  # Текущая рабочая директория

    # Проверка, существует ли локальная папка
    if not os.path.exists(local_folder):
        os.makedirs(local_folder)

    # Авторизация через токен
    g = Github(github_token)
    repo = g.get_repo(repo_name)
    
    # Проходим по каждому файлу и загружаем его из папки reserv
    for file_name in files:
        try:
            # Получаем содержимое файла из папки 'reserv'
            file_content = repo.get_contents(f"reserv/{file_name}")
            content = file_content.decoded_content.decode("utf-8")
            
            # Путь для сохранения файла в локальной папке
            local_file_path = os.path.join(local_folder, file_name)

            # Сохраняем файл в локальной папке
            with open(local_file_path, "w") as local_file:
                local_file.write(content)

            await ctx.send(f"Successfully downloaded {file_name} to the local folder.")
        
        except Exception as e:
            await ctx.send(f"Error downloading {file_name} from 'reserv' to the local folder: {e}")



@bot.event
async def on_ready():
    synced = await bot.tree.sync()
    print(f"Logged in as {bot.user}")
    print(f"Synced {len(synced)} commands: {[cmd.name for cmd in synced]}")

@bot.command()
async def clearsync(ctx):
    bot.tree.clear_commands(guild=None)  # Очистка всех команд
    await bot.tree.sync(guild=discord.Object(id=1318263619926102137))
    await ctx.send("Cleared and resynced commands!")


# Загружаем данные при старте
dkp_data = load_dkp_data()

# Запуск бота
bot.run(DISCORD_TOKEN)
