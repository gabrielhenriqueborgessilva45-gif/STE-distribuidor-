import os
import re
import sqlite3
import secrets
import asyncio
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
FORUM_CHANNEL_ID = int(os.getenv("FORUM_CHANNEL_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "pj_distribuidor.sqlite3")
PROCESS_REGEX = os.getenv(
    "PROCESS_REGEX",
    r"(?i)\b(?:processo\s*)?(\d{1,6}/\d{4})\b"
)

TAG_SEM_RELATOR = "Sem Relator"
TAG_SENTENCIADO = "Processo Sentenciado"
TAG_ARQUIVADO = "Processo Arquivado"
MAX_REDISTRIBUTIONS = 10

if not TOKEN:
    raise RuntimeError("Configure DISCORD_TOKEN no arquivo .env.")
if not FORUM_CHANNEL_ID:
    raise RuntimeError("Configure FORUM_CHANNEL_ID no arquivo .env.")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)
_auto_draw_task = None
db_lock = asyncio.Lock()
process_pattern = re.compile(PROCESS_REGEX)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ministers (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processes (
                process_number TEXT PRIMARY KEY,
                thread_id INTEGER NOT NULL UNIQUE,
                guild_id INTEGER NOT NULL,
                forum_channel_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'closed')),
                current_minister_id INTEGER,
                redistribution_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_reason TEXT,
                last_activity_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                process_number TEXT NOT NULL,
                action TEXT NOT NULL,
                previous_minister_id INTEGER,
                new_minister_id INTEGER,
                happened_at TEXT NOT NULL
            )
        """)
        # Migração segura: mantém o banco existente e adiciona o relógio de inatividade.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(processes)").fetchall()}
        if "last_activity_at" not in columns:
            conn.execute("ALTER TABLE processes ADD COLUMN last_activity_at REAL")
        # Processos antigos começam a contar os 5 minutos a partir desta atualização.
        conn.execute(
            "UPDATE processes SET last_activity_at=? WHERE last_activity_at IS NULL AND status='active' AND current_minister_id IS NULL",
            (time.time(),)
        )
        conn.commit()


def extract_process_number(thread_name):
    match = process_pattern.search(thread_name or "")
    return match.group(1) if match else None


def tag_names(thread):
    return {tag.name.strip().casefold() for tag in getattr(thread, "applied_tags", [])}


def is_closed_tag(names):
    return (
        TAG_SENTENCIADO.casefold() in names
        or TAG_ARQUIVADO.casefold() in names
    )


def close_reason(names):
    if TAG_SENTENCIADO.casefold() in names:
        return TAG_SENTENCIADO
    if TAG_ARQUIVADO.casefold() in names:
        return TAG_ARQUIVADO
    return "Encerrado"


async def process_forum_thread(thread):
    if thread.parent_id != FORUM_CHANNEL_ID:
        return

    names = tag_names(thread)
    number = extract_process_number(thread.name)
    if not number:
        return

    async with db_lock:
        with connect_db() as conn:
            # Fechamento tem prioridade, mesmo que Sem Relator também esteja aplicada.
            if is_closed_tag(names):
                existing = conn.execute(
                    "SELECT process_number FROM processes WHERE process_number = ?",
                    (number,)
                ).fetchone()
                if existing:
                    conn.execute("""
                        UPDATE processes
                        SET status='closed', closed_reason=?, updated_at=?
                        WHERE process_number=?
                    """, (close_reason(names), now_iso(), number))
                else:
                    # Registro mínimo para impedir que um processo encerrado seja sorteado depois.
                    conn.execute("""
                        INSERT OR IGNORE INTO processes
                        (process_number, thread_id, guild_id, forum_channel_id,
                         status, current_minister_id, redistribution_count,
                         created_at, updated_at, closed_reason, last_activity_at)
                        VALUES (?, ?, ?, ?, 'closed', NULL, 0, ?, ?, ?, NULL)
                    """, (
                        number, thread.id, thread.guild.id, thread.parent_id,
                        now_iso(), now_iso(), close_reason(names)
                    ))
                conn.commit()
                return

            # Só a tag exata "Sem Relator" permite cadastrar um processo.
            # Todas as outras tags são ignoradas.
            if TAG_SEM_RELATOR.casefold() not in names:
                return

            existing = conn.execute(
                "SELECT status FROM processes WHERE process_number = ?",
                (number,)
            ).fetchone()

            # Não reabre processos já sentenciados/arquivados.
            if existing and existing["status"] == "closed":
                return

            if existing:
                conn.execute("""
                    UPDATE processes
                    SET thread_id=?, guild_id=?, forum_channel_id=?, updated_at=?
                    WHERE process_number=? AND status='active'
                """, (thread.id, thread.guild.id, thread.parent_id, now_iso(), number))
            else:
                conn.execute("""
                    INSERT INTO processes
                    (process_number, thread_id, guild_id, forum_channel_id,
                     status, current_minister_id, redistribution_count,
                     created_at, updated_at, last_activity_at)
                    VALUES (?, ?, ?, ?, 'active', NULL, 0, ?, ?, ?)
                """, (
                    number, thread.id, thread.guild.id, thread.parent_id,
                    now_iso(), now_iso(), time.time()
                ))
            conn.commit()


async def automatic_draw_worker():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            cutoff = time.time() - 300
            async with db_lock:
                with connect_db() as conn:
                    due = conn.execute("""
                        SELECT process_number, thread_id FROM processes
                        WHERE status='active' AND current_minister_id IS NULL
                          AND last_activity_at IS NOT NULL AND last_activity_at <= ?
                    """, (cutoff,)).fetchall()
                    ministers = [row["user_id"] for row in conn.execute(
                        "SELECT user_id FROM ministers ORDER BY user_id"
                    ).fetchall()]

            if due and ministers:
                for item in due:
                    channel = bot.get_channel(item["thread_id"])
                    if channel is None:
                        try:
                            channel = await bot.fetch_channel(item["thread_id"])
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            continue
                    if not isinstance(channel, discord.Thread):
                        continue

                    names = tag_names(channel)
                    # Confere novamente as etiquetas antes do sorteio automático.
                    if is_closed_tag(names) or TAG_SEM_RELATOR.casefold() not in names:
                        continue

                    async with db_lock:
                        with connect_db() as conn:
                            current = conn.execute("""
                                SELECT current_minister_id, status, last_activity_at
                                FROM processes WHERE process_number=? AND thread_id=?
                            """, (item["process_number"], item["thread_id"])).fetchone()
                            if (not current or current["status"] != "active"
                                or current["current_minister_id"] is not None
                                or current["last_activity_at"] is None
                                or current["last_activity_at"] > cutoff):
                                continue
                            available = [row["user_id"] for row in conn.execute(
                                "SELECT user_id FROM ministers ORDER BY user_id"
                            ).fetchall()]
                            if not available:
                                continue
                            chosen = secrets.choice(available)
                            changed = conn.execute("""
                                UPDATE processes SET current_minister_id=?, updated_at=?
                                WHERE process_number=? AND status='active'
                                  AND current_minister_id IS NULL
                                  AND last_activity_at <= ?
                            """, (chosen, now_iso(), item["process_number"], cutoff)).rowcount
                            if not changed:
                                continue
                            conn.execute("""
                                INSERT INTO history
                                (process_number, action, previous_minister_id, new_minister_id, happened_at)
                                VALUES (?, 'sorteio automático', NULL, ?, ?)
                            """, (item["process_number"], chosen, now_iso()))
                            conn.commit()

                    await channel.send(
                        f"🎲 **Sorteio automático realizado!**\n"
                        f"📁 Processo: **{item['process_number']}**\n"
                        f"⚖️ Ministro relator: <@{chosen}>\n"
                        "O sorteio foi realizado após 5 minutos sem novas mensagens."
                    )
        except Exception as exc:
            print(f"Erro no sorteio automático: {repr(exc)}")
        await asyncio.sleep(15)


@bot.event
async def on_message(message):
    if message.author.bot or not isinstance(message.channel, discord.Thread):
        return
    if message.channel.parent_id != FORUM_CHANNEL_ID:
        return
    async with db_lock:
        with connect_db() as conn:
            conn.execute("""
                UPDATE processes SET last_activity_at=?
                WHERE thread_id=? AND status='active' AND current_minister_id IS NULL
            """, (time.time(), message.channel.id))
            conn.commit()


def admin_only():
    return app_commands.checks.has_permissions(administrator=True)


@bot.event
async def on_ready():
    global _auto_draw_task
    init_db()
    if _auto_draw_task is None or _auto_draw_task.done():
        _auto_draw_task = asyncio.create_task(automatic_draw_worker())
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild_obj)
            await bot.tree.sync(guild=guild_obj)
        else:
            await bot.tree.sync()
    except Exception as exc:
        print(f"Falha ao sincronizar comandos: {exc}")

    print(f"PJ Distribuidor de Processos conectado como {bot.user}.")

    # Recupera tópicos ativos já existentes no fórum após reiniciar.
    channel = bot.get_channel(FORUM_CHANNEL_ID)
    if isinstance(channel, discord.ForumChannel):
        try:
            async for thread in channel.archived_threads(limit=None):
                await process_forum_thread(thread)
        except Exception as exc:
            print(f"Não foi possível consultar tópicos arquivados: {exc}")
        for thread in channel.threads:
            await process_forum_thread(thread)
    else:
        print("Aviso: o canal configurado não foi encontrado ou não é um Fórum.")


@bot.event
async def on_thread_create(thread):
    await process_forum_thread(thread)


@bot.event
async def on_thread_update(before, after):
    await process_forum_thread(after)


@bot.tree.command(name="sortear", description="Sorteia um processo sem relator e um ministro.")
@admin_only()
async def sortear(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with db_lock:
        with connect_db() as conn:
            ministers = [
                row["user_id"] for row in conn.execute(
                    "SELECT user_id FROM ministers ORDER BY user_id"
                ).fetchall()
            ]
            processes = conn.execute("""
                SELECT process_number, thread_id
                FROM processes
                WHERE status='active' AND current_minister_id IS NULL
            """).fetchall()

            if not ministers:
                await interaction.followup.send(
                    "Não há ministros cadastrados. Use `/adicionar` primeiro.",
                    ephemeral=True
                )
                return
            if not processes:
                await interaction.followup.send(
                    "Não há processos disponíveis com a tag **Sem Relator**.",
                    ephemeral=True
                )
                return

            chosen_process = secrets.choice(processes)
            chosen_minister = secrets.choice(ministers)
            conn.execute("""
                UPDATE processes
                SET current_minister_id=?, updated_at=?
                WHERE process_number=? AND status='active'
                  AND current_minister_id IS NULL
            """, (chosen_minister, now_iso(), chosen_process["process_number"]))
            conn.execute("""
                INSERT INTO history
                (process_number, action, previous_minister_id, new_minister_id, happened_at)
                VALUES (?, 'sorteio inicial', NULL, ?, ?)
            """, (chosen_process["process_number"], chosen_minister, now_iso()))
            conn.commit()

    thread_link = (
        f"https://discord.com/channels/{interaction.guild_id}/"
        f"{chosen_process['thread_id']}"
    )
    await interaction.followup.send(
        f"**Sorteio realizado!**\n"
        f"Processo: **{chosen_process['process_number']}**\n"
        f"Ministro: <@{chosen_minister}>\n"
        f"Tópico: {thread_link}",
        ephemeral=False
    )


@bot.tree.command(name="sortear2", description="Redistribui um processo para outro ministro.")
@app_commands.describe(numero_processo="Número do processo, por exemplo 002/2026.")
@admin_only()
async def sortear2(interaction: discord.Interaction, numero_processo: str):
    number = numero_processo.strip()
    await interaction.response.defer(ephemeral=True)

    async with db_lock:
        with connect_db() as conn:
            process = conn.execute("""
                SELECT * FROM processes WHERE process_number=?
            """, (number,)).fetchone()

            if not process or process["status"] != "active":
                await interaction.followup.send(
                    "Processo não encontrado ou já sentenciado/arquivado.",
                    ephemeral=True
                )
                return
            if process["current_minister_id"] is None:
                await interaction.followup.send(
                    "Esse processo ainda não recebeu um ministro pelo sorteio inicial.",
                    ephemeral=True
                )
                return
            if process["redistribution_count"] >= MAX_REDISTRIBUTIONS:
                await interaction.followup.send(
                    "Esse processo já atingiu o limite de 10 redistribuições.",
                    ephemeral=True
                )
                return

            ministers = [
                row["user_id"] for row in conn.execute(
                    "SELECT user_id FROM ministers WHERE user_id != ?",
                    (process["current_minister_id"],)
                ).fetchall()
            ]
            if not ministers:
                await interaction.followup.send(
                    "Não há outro ministro elegível para receber este processo.",
                    ephemeral=True
                )
                return

            previous = process["current_minister_id"]
            new_minister = secrets.choice(ministers)
            new_count = process["redistribution_count"] + 1
            conn.execute("""
                UPDATE processes
                SET current_minister_id=?, redistribution_count=?, updated_at=?
                WHERE process_number=? AND status='active'
            """, (new_minister, new_count, now_iso(), number))
            conn.execute("""
                INSERT INTO history
                (process_number, action, previous_minister_id, new_minister_id, happened_at)
                VALUES (?, 'redistribuição', ?, ?, ?)
            """, (number, previous, new_minister, now_iso()))
            conn.commit()
            thread_id = process["thread_id"]

    link = f"https://discord.com/channels/{interaction.guild_id}/{thread_id}"
    await interaction.followup.send(
        f"**Redistribuição realizada!**\n"
        f"Processo: **{number}**\n"
        f"Ministro anterior: <@{previous}>\n"
        f"Novo ministro: <@{new_minister}>\n"
        f"Redistribuições: **{new_count}/10**\n"
        f"Tópico: {link}",
        ephemeral=False
    )


@bot.tree.command(name="adicionar", description="Adiciona um ministro ao cadastro de sorteio.")
@app_commands.describe(ministro="Membro que poderá receber processos.")
@admin_only()
async def adicionar(interaction: discord.Interaction, ministro: discord.Member):
    async with db_lock:
        with connect_db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM ministers WHERE user_id=?", (ministro.id,)
            ).fetchone()
            if exists:
                await interaction.response.send_message(
                    f"{ministro.mention} já está cadastrado como ministro.",
                    ephemeral=True
                )
                return
            conn.execute(
                "INSERT INTO ministers (user_id, added_at) VALUES (?, ?)",
                (ministro.id, now_iso())
            )
            conn.commit()
    await interaction.response.send_message(
        f"{ministro.mention} foi adicionado ao cadastro de ministros.",
        ephemeral=True
    )


@bot.tree.command(name="remover", description="Remove um ministro dos próximos sorteios.")
@app_commands.describe(ministro="Membro que deixará de participar dos próximos sorteios.")
@admin_only()
async def remover(interaction: discord.Interaction, ministro: discord.Member):
    async with db_lock:
        with connect_db() as conn:
            result = conn.execute(
                "DELETE FROM ministers WHERE user_id=?", (ministro.id,)
            )
            conn.commit()
            removed = result.rowcount > 0
    if not removed:
        await interaction.response.send_message(
            f"{ministro.mention} não está cadastrado como ministro.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"{ministro.mention} foi removido dos próximos sorteios. "
            "O histórico e as designações anteriores foram preservados.",
            ephemeral=True
        )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "Somente membros com permissão de Administrador podem usar esses comandos."
    else:
        message = f"Ocorreu um erro ao executar o comando: {error}"
        print(f"Erro em comando: {repr(error)}")

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    init_db()
    bot.run(TOKEN)
