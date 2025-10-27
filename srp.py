#!/usr/bin/env python3
import os
import json
import subprocess
import asyncio
import logging
import re
import random
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo
from PIL import Image, ImageDraw, ImageFont

# === Setup logger ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# === Telegram API credentials ===
api_id = 27914983
api_hash = '3ee76f526ada3e20c389ebc9e76c3a68'

# === Config ===
batch_bot_username = '@not_those_videos_free_bot'
target_channel_id = -1003282977217
media_folder = "media_temp"
os.makedirs(media_folder, exist_ok=True)

client = TelegramClient('corning_session', api_id, api_hash)

# === Regex patterns ===
link_regex = re.compile(r'https://t\.me/([^?]+)\?start=([\w-]+)', re.IGNORECASE)
batch_link_regex = re.compile(r'https://t\.me/[^?]+\?start=[\w-]+', re.IGNORECASE)

# === Session state ===
active_session = False
media_from_bot = []
original_batch_msg = None
original_caption = None
first_msg_link = None
last_msg_link = None
bot_sender_id = None

# === Watermark settings ===
WATERMARK_TEXT = "TG - @That_stuff"
SCALE = 0.05  # 5% of the smaller video/image dimension
MARGIN = 10   # px padding from edges for static placements

# Try to locate a common font file for both PIL and ffmpeg drawtext
FONT_FILE_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]
FONT_FILE = None
for f in FONT_FILE_CANDIDATES:
    if os.path.exists(f):
        FONT_FILE = f
        break

# ======== Utility / FFmpeg helpers ========
def extract_video_info(file_path):
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration",
            "-of", "json", file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout or "{}")
        if not data.get("streams"):
            return {}
        stream = data["streams"][0]
        return {
            "width": int(stream.get("width", 0)),
            "height": int(stream.get("height", 0)),
            "duration": float(stream.get("duration", 0.0) or 0.0)
        }
    except Exception as e:
        logger.error(f"[FFprobe Error] {e}")
        return {}

def extract_thumbnail(video_path, thumb_path):
    try:
        cmd = [
            "ffmpeg", "-i", video_path, "-ss", "00:00:01.000", "-vframes", "1", thumb_path, "-y"
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return os.path.exists(thumb_path)
    except Exception as e:
        logger.error(f"[Thumb Error] {e}")
        return False

# ======== Watermark functions ========
def apply_video_watermark(input_path, output_path):
    """
    Adds a moving text watermark using ffmpeg drawtext.
    Movement direction and random starting offset are chosen randomly.
    This function checks input file before running ffmpeg and logs stderr on failure.
    """
    try:
        # Safety checks: input must exist and be non-zero
        if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
            logger.error(f"[Skip] Input file missing or empty: {input_path}")
            return input_path

        info = extract_video_info(input_path)
        if not info:
            logger.warning("[Skip] Could not extract video info.")
            return input_path

        w = info["width"]
        h = info["height"]
        font_size = int(min(w, h) * SCALE)
        if font_size < 8:
            font_size = 8

        # Remove existing output if any
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass

        # Choose random direction
        direction = random.choice(["left_to_right", "right_to_left", "top_to_bottom", "bottom_to_top"])

        # Speed (t multiplier) - tuned so watermark moves at reasonable pace across sizes
        horiz_speed = max(30, w // 6)
        vert_speed = max(20, h // 8)

        # start offsets (random)
        start_x = random.randint(0, max(0, w // 4))
        start_y = random.randint(0, max(0, h // 4))

        # Build font specification for ffmpeg drawtext
        if FONT_FILE:
            # use explicit fontfile if available (safer)
            font_opts = f":fontfile='{FONT_FILE}':fontsize={font_size}:fontcolor=white"
        else:
            # fallback to font name
            font_opts = f":font='Sans':fontsize={font_size}:fontcolor=white"

        margin = 10
        # Use text_w/text_h variables inside ffmpeg expressions
        if direction == "left_to_right":
            # text moves from left to right
            vf_expr = (
                f"drawtext=text='{WATERMARK_TEXT}'{font_opts}:"
                f"x=mod(t*{horiz_speed}+{start_x},{w}-text_w-{margin}):"
                f"y={start_y}"
            )
        elif direction == "right_to_left":
            vf_expr = (
                f"drawtext=text='{WATERMARK_TEXT}'{font_opts}:"
                f"x=({w}-text_w-{margin})-mod(t*{horiz_speed}+{start_x},{w}-text_w-{margin}):"
                f"y={start_y}"
            )
        elif direction == "top_to_bottom":
            vf_expr = (
                f"drawtext=text='{WATERMARK_TEXT}'{font_opts}:"
                f"x={start_x}:"
                f"y=mod(t*{vert_speed}+{start_y},{h}-text_h-{margin})"
            )
        else:  # bottom_to_top
            vf_expr = (
                f"drawtext=text='{WATERMARK_TEXT}'{font_opts}:"
                f"x={start_x}:"
                f"y=({h}-text_h-{margin})-mod(t*{vert_speed}+{start_y},{h}-text_h-{margin})"
            )

        cmd = [
            "ffmpeg", "-i", input_path,
            "-vf", vf_expr,
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_path,
            "-y"
        ]

        logger.info(f"[FFmpeg] Watermarking video ({direction}) -> {output_path}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            logger.error(f"[FFmpeg Error] rc={proc.returncode} stderr={proc.stderr.strip()}")
            # remove any zero-byte output
            try:
                if os.path.exists(output_path) and os.path.getsize(output_path) == 0:
                    os.remove(output_path)
            except Exception:
                pass
            return input_path

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return output_path
        else:
            logger.error("[Overlay] FFmpeg finished but output missing or empty.")
            return input_path

    except Exception as e:
        logger.error(f"[Video Watermark Error] {e}")
        return input_path

def apply_image_watermark(input_path, output_path):
    """
    Adds two static white text watermarks at random positions on images using PIL.
    """
    try:
        if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
            logger.error(f"[Skip Image] Input file missing or empty: {input_path}")
            return input_path

        image = Image.open(input_path).convert("RGBA")
        draw = ImageDraw.Draw(image)

        # font size relative to the image (use smaller dimension)
        font_size = int(min(image.width, image.height) * SCALE)
        if font_size < 8:
            font_size = 8

        # Load a TTF font if available, otherwise fallback to default
        try:
            if FONT_FILE:
                font = ImageFont.truetype(FONT_FILE, font_size)
            else:
                font = ImageFont.truetype("arial.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

        text = WATERMARK_TEXT
        text_w, text_h = draw.textsize(text, font=font)

        # safe bounds
        max_x = max(0, image.width - text_w - MARGIN)
        max_y = max(0, image.height - text_h - MARGIN)

        # pick two distinct positions
        positions = []
        attempts = 0
        while len(positions) < 2 and attempts < 20:
            x = random.randint(MARGIN, max_x or MARGIN)
            y = random.randint(MARGIN, max_y or MARGIN)
            # avoid placing two too close
            if all(not (abs(x - px) < text_w // 2 and abs(y - py) < text_h // 2) for px, py in positions):
                positions.append((x, y))
            attempts += 1
        if len(positions) < 2:
            positions = [(MARGIN, MARGIN), (max_x, max_y)]

        # draw the text (white, full opacity, no shadow)
        for (x, y) in positions:
            draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

        # Save as JPEG/PNG depending on original extension
        base_ext = os.path.splitext(output_path)[1].lower()
        if base_ext == ".png":
            image.save(output_path, "PNG")
        else:
            image.convert("RGB").save(output_path, "JPEG", quality=95)

        return output_path

    except Exception as e:
        logger.error(f"[Image Watermark Error] {e}")
        return input_path

# ======== Media Processing ========
async def process_media(media_obj):
    """
    Download media, convert GIF->MP4 if needed, apply watermark,
    and return (final_file_path, thumb_path_or_None) ready to send.
    This version validates downloads and returns (None, None) when download fails.
    """
    file_path = await client.download_media(media_obj, file=media_folder)
    if not file_path:
        logger.error("[Download Error] download_media returned no path.")
        return None, None

    # If download returned a directory, pick newest file inside
    if os.path.isdir(file_path):
        files = sorted([os.path.join(file_path, f) for f in os.listdir(file_path)], key=os.path.getmtime, reverse=True)
        file_path = files[0] if files else None

    if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        logger.error(f"[Download Error] File not found or empty after download: {file_path}")
        return None, None

    ext = os.path.splitext(file_path)[1].lower()
    is_video = False
    is_gif = False

    if hasattr(media_obj, "document") and media_obj.document:
        for attr in media_obj.document.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                is_video = True

    if ext == ".gif":
        is_gif = True

    # If GIF, convert to MP4 first
    if is_gif:
        logger.info(f"Converting GIF -> MP4: {file_path}")
        mp4_path = os.path.join(media_folder, f"{os.path.splitext(os.path.basename(file_path))[0]}.mp4")
        cmd = [
            "ffmpeg", "-i", file_path,
            "-movflags", "+faststart",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-pix_fmt", "yuv420p",
            mp4_path, "-y"
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(mp4_path) or os.path.getsize(mp4_path) == 0:
            logger.error(f"[GIF->MP4 Error] rc={proc.returncode} stderr={proc.stderr.strip()}")
            return None, None
        try:
            os.remove(file_path)
        except Exception:
            pass
        file_path = mp4_path
        is_video = True
        ext = ".mp4"

    # Re-check extension if video after conversion
    if not is_video and ext in [".mp4", ".mkv", ".mov", ".webm", ".avi"]:
        is_video = True

    if is_video:
        logger.info(f"Processing video: {file_path}")
        watermarked_path = os.path.join(media_folder, f"wm_{os.path.basename(file_path)}")
        final_video = apply_video_watermark(file_path, watermarked_path)
        if not final_video or not os.path.exists(final_video) or os.path.getsize(final_video) == 0:
            logger.error(f"[Processing Error] Watermarked video missing: {final_video}")
            return None, None
        thumb_path = final_video.rsplit(".", 1)[0] + "_thumb.jpg"
        extract_thumbnail(final_video, thumb_path)
        return final_video, (thumb_path if os.path.exists(thumb_path) else None)
    else:
        logger.info(f"Processing image: {file_path}")
        watermarked_image_path = os.path.join(media_folder, f"wm_{os.path.basename(file_path)}")
        final_image = apply_image_watermark(file_path, watermarked_image_path)
        if not final_image or not os.path.exists(final_image) or os.path.getsize(final_image) == 0:
            logger.error(f"[Processing Error] Watermarked image missing: {final_image}")
            return None, None
        return final_image, None

# ======== Bot event handlers (high-level logic) ========
@client.on(events.NewMessage(chats=target_channel_id))
async def detect_batch_or_single_message(event):
    global active_session, original_batch_msg, original_caption

    msg = event.message

    if msg.grouped_id:
        logger.info("Batch detected, processing group of messages.")
        await asyncio.sleep(1.5)
        messages = await client.get_messages(msg.chat_id, limit=100)
        batch_messages = [m for m in messages if m.grouped_id == msg.grouped_id]

        for media_msg in batch_messages:
            text = (media_msg.text or "") + (media_msg.message or "")
            match = link_regex.search(text)
            if match:
                bot_username = '@' + match.group(1)
                file_id = match.group(2)
                original_batch_msg = media_msg
                original_caption = media_msg.text or media_msg.message or ''
                await start_fetch_session(bot_username, file_id)
                break

    elif msg.media and (msg.text or msg.message):
        text = msg.text or msg.message
        match = link_regex.search(text)
        if match:
            bot_username = '@' + match.group(1)
            file_id = match.group(2)
            logger.info("Single media message detected.")
            original_batch_msg = msg
            original_caption = text
            await start_fetch_session(bot_username, file_id)

async def start_fetch_session(bot_username, file_id):
    global active_session, media_from_bot, bot_sender_id

    if active_session:
        logger.warning("Session already active. Ignoring new request.")
        return

    active_session = True
    media_from_bot.clear()

    bot_entity = await client.get_entity(bot_username)
    bot_sender_id = bot_entity.id
    await client.send_message(bot_entity, f'/start {file_id}')
    logger.info(f"Sent /start {file_id} to {bot_username}")

    asyncio.create_task(timeout_monitor())

@client.on(events.NewMessage)
async def collect_bot_media(event):
    global media_from_bot

    if not active_session:
        return

    if event.media and not event.out and event.sender_id == bot_sender_id:
        media_from_bot.append(event.message.media)
        logger.info(f"Collected media ID: {event.message.id}")

async def timeout_monitor():
    global active_session, media_from_bot

    wait_time = 0
    while active_session:
        await asyncio.sleep(2)
        wait_time += 2

        if wait_time > 30:
            active_session = False
            logger.info("Session timeout. Proceeding to next step.")

            if len(media_from_bot) > 1:
                await handle_batch()
            elif len(media_from_bot) == 1:
                await handle_single_file()
            else:
                logger.warning("No media received.")
            break

async def handle_batch():
    global first_msg_link, last_msg_link, media_from_bot

    channel_id_clean = str(target_channel_id).replace('-100', '')
    first_msg_link = None
    last_msg_link = None

    for idx, media_obj in enumerate(media_from_bot):
        processed_file, thumb_file = await process_media(media_obj)
        if not processed_file:
            logger.error("Skipping file due to processing error.")
            continue

        # === Extract video info for attributes ===
        info = extract_video_info(processed_file)
        attributes = []
        if info:
            attributes.append(DocumentAttributeVideo(
                duration=int(info.get("duration", 0)),
                w=info.get("width", 0),
                h=info.get("height", 0),
                supports_streaming=True
            ))

        sent_msg = await client.send_file(
            target_channel_id,
            file=processed_file,
            thumb=thumb_file,
            attributes=attributes,
            supports_streaming=True
        )

        msg_link = f"https://t.me/c/{channel_id_clean}/{sent_msg.id}"

        if idx == 0:
            first_msg_link = msg_link
        last_msg_link = msg_link

        logger.info(f"Uploaded batch file to channel: {msg_link}")

        for f in [processed_file, thumb_file]:
            if f and os.path.exists(f):
                try:
                    os.remove(f)
                except:
                    pass

    media_from_bot.clear()
    await handle_batch_creator()

async def handle_single_file():
    global media_from_bot

    processed_file, thumb_file = await process_media(media_from_bot[0])
    if not processed_file:
        logger.error("Processing failed for single file; aborting upload.")
        media_from_bot.clear()
        return

        # === Extract video info for attributes ===
    info = extract_video_info(processed_file)
    attributes = []
    if info:
        attributes.append(DocumentAttributeVideo(
            duration=int(info.get("duration", 0)),
            w=info.get("width", 0),
            h=info.get("height", 0),
            supports_streaming=True
        ))

    sent_msg = await client.send_file(
        target_channel_id,
        file=processed_file,
        thumb=thumb_file,
        attributes=attributes,
        supports_streaming=True
    )

    media_from_bot.clear()
    logger.info("Uploaded single file to channel.")

    for f in [processed_file, thumb_file]:
        if f and os.path.exists(f):
            try:
                os.remove(f)
            except:
                pass

    await handle_single_file_link(sent_msg)

async def handle_batch_creator():
    batch_bot = await client.get_entity(batch_bot_username)

    await client.send_message(batch_bot, '/batch')
    logger.info("Sent /batch to batch bot.")

    await wait_for_reply(batch_bot, "first message")
    await client.send_message(batch_bot, first_msg_link)
    logger.info(f"Sent first message link: {first_msg_link}")

    await wait_for_reply(batch_bot, "last message")
    await client.send_message(batch_bot, last_msg_link)
    logger.info(f"Sent last message link: {last_msg_link}")

    batch_link = await wait_for_link(batch_bot)
    if batch_link:
        logger.info(f"Batch link generated: {batch_link}")
        await clean_caption_and_edit(batch_link)
    else:
        logger.error("Failed to generate batch link.")

async def handle_single_file_link(sent_msg):
    batch_bot = await client.get_entity(batch_bot_username)
    await client.send_message(batch_bot, '/genlink')
    logger.info("Sent /genlink to batch bot.")

    await wait_for_reply(batch_bot, "send")
    await client.forward_messages(batch_bot, sent_msg)
    logger.info("Forwarded single file to bot.")

    single_link = await wait_for_link(batch_bot)
    if single_link:
        logger.info(f"Single file link generated: {single_link}")
        await clean_caption_and_edit(single_link)
    else:
        logger.error("Failed to get single file link.")

async def clean_caption_and_edit(new_link):
    global original_batch_msg

    # Build the new formatted caption
    updated_caption = (
        "<b>HERE IS YOUR LINK 🔗</b>\n\n"
        f"<blockquote><b><a href=\"{new_link}\">{new_link}</a></b></blockquote>"
    )

    # Edit the message with HTML parsing
    await client.edit_message(
        target_channel_id,
        original_batch_msg.id,
        updated_caption,
        parse_mode="html"
    )

    logger.info("Edited original message caption with formatted new link (HTML with blockquote).")

async def wait_for_reply(bot_entity, expected_text):
    for _ in range(30):
        msgs = await client.get_messages(bot_entity, limit=1)
        if msgs and expected_text in (msgs[0].message or "").lower():
            return
        await asyncio.sleep(2)

async def wait_for_link(bot_entity):
    for _ in range(30):
        msgs = await client.get_messages(bot_entity, limit=1)
        if not msgs:
            await asyncio.sleep(2)
            continue
        match = batch_link_regex.search(msgs[0].message or "")
        if match:
            return match.group(0)
        await asyncio.sleep(2)
    return None

if __name__ == "__main__":
    print("✅ Bot running. Monitoring your private channel for media batches and single files...")
    client.start()
    client.run_until_disconnected()
