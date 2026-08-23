import os
import sys
import json
import time
import re
import secrets
import string
import asyncio
import logging
from datetime import datetime
import discord
from discord.ext import commands
import docker
import psutil

# ---------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

CONFIG_FILE = "config.json"
DB_FILE = "vps_db.json"

if not os.path.exists(CONFIG_FILE):
    default_config = {
        "TOKEN": "YOUR_DISCORD_BOT_TOKEN_HERE",
        "PREFIX": "$",
        "ADMIN_IDS": [],
        "ANTINUKE_ENABLED": True,
        "DEFAULT_DATA_DIR": "./vps_data"
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(default_config, f, indent=4)

with open(CONFIG_FILE, "r") as f:
    config = json.load(f)

def load_db():
    if not os.path.exists(DB_FILE):
        return {"vps": {}, "admins": config.get("ADMIN_IDS", []), "antinuke": config.get("ANTINUKE_ENABLED", True)}
    try:
        with open(DB_FILE, "r") as f:
            data = json.load(f)
            if "admins" not in data:
                data["admins"] = config.get("ADMIN_IDS", [])
            if "antinuke" not in data:
                data["antinuke"] = config.get("ANTINUKE_ENABLED", True)
            return data
    except json.JSONDecodeError:
        return {"vps": {}, "admins": config.get("ADMIN_IDS", []), "antinuke": config.get("ANTINUKE_ENABLED", True)}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

try:
    docker_client = docker.from_env()
    logging.info("Connected to Docker daemon successfully.")
except Exception as err:
    logging.error(f"Failed to connect to Docker daemon: {err}")
    docker_client = None

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix=config.get("PREFIX", "$"), intents=intents)

def generate_password(length=14):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def parse_size_to_bytes(size_str: str) -> int:
    size_str = size_str.lower().strip()
    match = re.match(r"^(\d+)([mg])$", size_str)
    if not match:
        raise ValueError("Invalid format. Use numbers followed by 'm' or 'g' (e.g., 512m, 20g, 100g).")
    num, unit = match.groups()
    num = int(num)
    bytes_val = num * 1024 * 1024 if unit == "m" else num * 1024 * 1024 * 1024
    if bytes_val < 256 * 1024 * 1024:
        raise ValueError("Memory allocation too small. Specify at least 256m.")
    return bytes_val

def validate_host_capacity(requested_ram_bytes: int, requested_cpus: int):
    total_mem = psutil.virtual_memory().total
    total_cpus = os.cpu_count() or 1

    if requested_ram_bytes > total_mem:
        req_gb = round(requested_ram_bytes / (1024**3), 2)
        host_gb = round(total_mem / (1024**3), 2)
        raise ValueError(f"Requested RAM ({req_gb} GB) exceeds total Host VPS RAM ({host_gb} GB).")

    if requested_cpus > total_cpus:
        raise ValueError(f"Requested CPU cores ({requested_cpus}) exceeds total Host VPS cores ({total_cpus}).")

def is_admin():
    async def predicate(ctx):
        db = load_db()
        admins = db.get("admins", [])
        if ctx.author.id in admins or ctx.author.guild_permissions.administrator:
            return True
        await ctx.send("❌ **Access Denied:** You do not have permission to execute this command.")
        return False
    return commands.check(predicate)

def get_normalized_os(os_input: str) -> tuple:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", os_input).upper()
    cleaned = cleaned.replace("DEBAIN", "DEBIAN")
    
    mapping = {
        "UBUNTU2204": "ubuntu:22.04",
        "UBUNTU2004": "ubuntu:20.04",
        "DEBIAN10": "debian:10",
        "DEBIAN11": "debian:11",
        "DEBIAN12": "debian:12",
        "DEBIAN13": "debian:13"
    }
    
    if cleaned in mapping:
        return cleaned, mapping[cleaned]
    raise ValueError(f"Unsupported OS version: `{os_input}`. Supported: Ubuntu 20.04/22.04, Debian 10/11/12/13.")

# ---------------------------------------------------------
# REAL HARDWARE PROVISIONING ENGINE
# ---------------------------------------------------------
def _provision_vps_sync(image_tag: str, ram_bytes: int, cpu_cores: int, disk_bytes: int, container_name: str, root_password: str):
    data_dir = os.path.abspath(config.get("DEFAULT_DATA_DIR", "./vps_data"))
    os.makedirs(f"{data_dir}/{container_name}", exist_ok=True)
    
    nano_cpus = int(cpu_cores * 1_000_000_000)

    try:
        docker_client.images.get(image_tag)
    except docker.errors.ImageNotFound:
        logging.info(f"Image {image_tag} not found locally. Pulling from Docker Hub...")
        docker_client.images.pull(image_tag)

    try:
        docker_client.images.get("tailscale/tailscale:latest")
    except docker.errors.ImageNotFound:
        docker_client.images.pull("tailscale/tailscale:latest")

    # Launch main OS container with hard hardware limits
    container = docker_client.containers.run(
        image=image_tag,
        name=container_name,
        command="bash -c 'apt-get update && apt-get install -y openssh-server procps neofetch && mkdir -p /var/run/sshd && echo \"root:" + root_password + "\" | chpasswd && sed -i \"s/#PermitRootLogin.*/PermitRootLogin yes/g\" /etc/ssh/sshd_config && /usr/sbin/sshd -D'",
        detach=True,
        tty=True,
        stdin_open=True,
        mem_limit=ram_bytes,
        memswap_limit=ram_bytes,
        nano_cpus=nano_cpus,
        volumes={
            f"{data_dir}/{container_name}": {"bind": "/data", "mode": "rw"},
            "/var/lib/lxcfs/proc/meminfo": {"bind": "/proc/meminfo", "mode": "rw"} if os.path.exists("/var/lib/lxcfs/proc/meminfo") else f"{data_dir}/{container_name}": {"bind": "/mnt_data", "mode": "rw"}
        },
        privileged=True
    )

    ts_container_name = f"ts-{container_name}"
    clean_hostname = container_name.replace("_", "-")
    
    ts_container = docker_client.containers.run(
        image="tailscale/tailscale:latest",
        name=ts_container_name,
        environment={
            "TS_HOSTNAME": clean_hostname,
            "TS_USERSPACE": "true"
        },
        network_mode=f"container:{container.id}",
        detach=True,
        privileged=True
    )

    time.sleep(3)

    auth_url = None
    for _ in range(10):
        try:
            exec_res = ts_container.exec_run("tailscale up --qr=false")
            output = exec_res.output.decode("utf-8", errors="ignore")
            
            match = re.search(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+", output)
            if match:
                auth_url = match.group(0)
                break

            logs = ts_container.logs().decode("utf-8", errors="ignore")
            match_logs = re.search(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+", logs)
            if match_logs:
                auth_url = match_logs.group(0)
                break
        except Exception as e:
            logging.warning(f"Tailscale link fetch attempt error: {e}")
            
        time.sleep(2)

    if not auth_url:
        container.stop()
        container.remove()
        ts_container.stop()
        ts_container.remove()
        raise RuntimeError("Failed to capture Tailscale Auth URL. Please verify container network access.")

    return container, auth_url

async def provision_vps(image_tag: str, ram_bytes: int, cpu_cores: int, disk_bytes: int, container_name: str, root_password: str):
    return await asyncio.to_thread(_provision_vps_sync, image_tag, ram_bytes, cpu_cores, disk_bytes, container_name, root_password)

# ---------------------------------------------------------
# BOT COMMANDS
# ---------------------------------------------------------
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user.name} ({bot.user.id})")

@bot.command(name="myvps")
async def cmd_myvps(ctx):
    db = load_db()
    user_vps = [(vps_id, info) for vps_id, info in db.get("vps", {}).items() if info.get("owner_id") == ctx.author.id]

    if not user_vps:
        await ctx.send("❌ **No active VPS instances found.**")
        return

    embed = discord.Embed(title="🖥️ Your Managed VPS Instances", color=discord.Color.green())
    for vps_id, info in user_vps:
        embed.add_field(
            name=f"Instance ID: {vps_id}",
            value=(
                f"**OS:** `{info['os']}` | **Status:** `{info.get('status', 'ACTIVE')}`\n"
                f"**CPU:** `{info['cpu']} Core(s)` | **RAM:** `{info['ram']}` | **Disk:** `{info['disk']}`\n"
                f"**SSH User:** `root` | **Password:** `{info.get('password', 'N/A')}`"
            ),
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command(name="create")
@is_admin()
async def cmd_create(ctx, ram: str, cpu: int, disk: str, os_type: str, user: discord.Member):
    """Syntax: $create <ram> <cpu> <disk> <os> <user>"""
    try:
        ram_bytes = parse_size_to_bytes(ram)
        disk_bytes = parse_size_to_bytes(disk)
        validate_host_capacity(ram_bytes, cpu)
        os_key, image_tag = get_normalized_os(os_type)
    except ValueError as e:
        await ctx.send(f"❌ **Hardware Limit Exceeded / Parameter Error:** {e}")
        return

    status_msg = await ctx.send(f"⏳ **[1/2]** Provisioning real hardware VPS ({ram} RAM, {cpu} Core(s)) for {user.mention}...")
    
    container_name = f"vps-{user.id}-{int(time.time())}"
    root_password = generate_password()

    try:
        container, login_url = await asyncio.wait_for(
            provision_vps(image_tag, ram_bytes, cpu, disk_bytes, container_name, root_password),
            timeout=90.0
        )

        await status_msg.edit(content=f"⏳ **[2/2]** Delivering Tailscale access link to {user.mention} via DM...")

        vps_id = container.id[:10]

        dm_embed = discord.Embed(
            title="🚀 Your VPS is Ready!",
            description="Click the link below to authorize this instance and attach it to your Tailscale network.",
            color=discord.Color.blue()
        )
        dm_embed.add_field(name="Instance ID", value=f"`{vps_id}`", inline=True)
        dm_embed.add_field(name="Allocated RAM", value=f"`{ram}`", inline=True)
        dm_embed.add_field(name="Allocated vCPU", value=f"`{cpu} Core(s)`", inline=True)
        dm_embed.add_field(name="Disk Storage", value=f"`{disk}`", inline=True)
        dm_embed.add_field(name="OS Distribution", value=f"`{os_key}`", inline=True)
        dm_embed.add_field(name="🔑 Tailscale Login Link", value=f"{login_url}", inline=False)
        dm_embed.add_field(name="🔐 SSH Credentials", value=f"**Port:** `22`\n**User:** `root`\n**Password:** `{root_password}`", inline=False)

        try:
            await user.send(embed=dm_embed)
        except discord.Forbidden:
            container.stop()
            container.remove()
            await status_msg.edit(content=f"❌ **Deployment Aborted:** Could not DM {user.mention}. Direct Messages must be enabled.")
            return

        db = load_db()
        db["vps"][vps_id] = {
            "container_id": container.id,
            "container_name": container_name,
            "owner_id": user.id,
            "owner_tag": str(user),
            "ram": ram,
            "cpu": cpu,
            "disk": disk,
            "os": os_key,
            "password": root_password,
            "status": "ACTIVE",
            "created_at": datetime.utcnow().isoformat()
        }
        save_db(db)

        await status_msg.edit(content=f"✅ **VPS Provisioned Successfully!**\n**ID:** `{vps_id}`\n**Assigned To:** {user.mention}\n📩 **Login link delivered to Direct Messages.**")

    except Exception as err:
        logging.error(f"Error provisioning VPS: {err}", exc_info=True)
        await status_msg.edit(content=f"❌ **Deployment Failed:** `{err}`")

if __name__ == "__main__":
    bot.run(config.get("TOKEN"))
      
