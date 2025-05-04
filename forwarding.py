import os
import sys
import pickle
import threading
import time
from datetime import datetime
from dotenv import load_dotenv, set_key, find_dotenv
import logging
import asyncio # Import asyncio for async Pyrogram
from pyrogram import Client, filters # Import Pyrogram Client and types
from pyrogram.enums import MessageMediaType # To help identify message types

# --- Flask Web Server Imports ---
from flask import Flask
# Flask threading is already imported
# --- End Flask Imports ---

# Set up basic logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv(find_dotenv())
ENV_PATH = find_dotenv()

# File to store the last processed message IDs for periodic forwarding
LAST_PROCESSED_IDS_FILE = "data/last_processed_message_ids.pickle"

# Mapping from Pyrogram MessageMediaType to the old string format
# Only includes types relevant for sending files based on your periodic logic
MESSAGE_TYPE_MAP = {
    MessageMediaType.VIDEO: "messageVideo",
    MessageMediaType.DOCUMENT: "messageDocument",
    MessageMediaType.PHOTO: "messagePhoto",
    # Add other types if needed based on your LIVE_INCLUDE_TYPES
}

# Global list of Pyrogram MessageMediaType to exclude from *all* processing
# (This corresponds to the old EXCLUDE_TYPES but mapped to Pyrogram enums if possible,
# or handled by checking message content explicitly if no direct MediaType enum)
# Note: Pyrogram often doesn't represent service messages with a media type.
# We'll check message attributes directly in the filtering logic.
EXCLUDE_PYROGRAM_TYPES = [
    # Pyrogram doesn't have direct MessageMediaType enums for most service messages.
    # We will exclude based on message object attributes/structure in the filtering logic.
]


# --- Flask Web Server Setup ---
app = Flask(__name__)

@app.route('/')
def index():
    """Simple route to respond to pings."""
    return "TeleCopy Script is Running (Ping OK)", 200

def run_flask_app():
    """Runs the Flask app in a separate thread."""
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Starting dummy web server on port {port}")
    try:
        # app.run is blocking, so this thread will stay alive here
        # use_reloader=False is important to prevent Flask from starting multiple threads/processes
        app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Failed to start Flask web server: {e}")

# --- End Flask Web Server Setup ---


def check_env_vars():
    """Checks for required environment variables and prints warnings if missing."""
    # PHONE, API_ID, API_HASH are still required by Pyrogram even with session string
    # SESSION_STRING is required for authentication
    required_periodic = [
        "PHONE", "API_ID", "API_HASH", "SESSION_STRING",
        "LIVE_SOURCE_CHATS", "LIVE_DEST_CHATS", "LIVE_INCLUDE_TYPES",
        "LIVE_CHECK_INTERVAL_SECONDS"
    ]
    missing = [var for var in required_periodic if not os.getenv(var)]

    if missing:
        logger.warning(f"Missing required environment variables for periodic forwarding: {missing}")
        logger.warning("Please ensure your .env file contains these values.")
        logger.warning("You will need to generate a SESSION_STRING using a separate script.")
        # Do not sys.exit, let the Pyrogram client initialization handle failure


# Modified for Pyrogram using session string
def initialize_pyrogram():
    """ Initializes and returns the Pyrogram client using session string. """
    os.makedirs("data", exist_ok=True)

    api_id = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    phone = os.getenv("PHONE") # Needed by Pyrogram client constructor
    session_string = os.getenv("SESSION_STRING")
    files_directory = os.getenv("FILES_DIRECTORY", "pyrogram_files") # Pyrogram's files dir

    if not api_id or not api_hash or not phone or not session_string:
         logger.error("Missing API_ID, API_HASH, PHONE, or SESSION_STRING in environment variables.")
         return None

    try:
        api_id = int(api_id)
    except ValueError:
        logger.error("Invalid API_ID format in .env. Must be an integer.")
        return None

    try:
        logger.info("Initializing Pyrogram client...")
        # The session name 'my_account' must match the one used in the session generation script
        client = Client(
            "my_account",
            api_id=api_id,
            api_hash=api_hash,
            session_string=session_string,
            phone_number=phone, # Required even with session string
            workdir=files_directory # Set the working directory
        )
        return client
    except Exception as e:
        logger.error(f"Error initializing Pyrogram client: {e}")
        return None

# Removed update_config - config is now expected via .env for headless operation
# Removed list_chats - interactive chat listing is not needed
# Removed all bulk copy related functions: set_bulk_copy_source_destination, copy_message,
# get_all_messages, filter_messages_by_date, load_copied_messages, save_copied_messages,
# copy_messages_in_batch, get_bulk_copy_source_destination_ids, copy_past_messages, custom_copy_messages.


def load_last_processed_ids():
    """Loads the dictionary of last processed message IDs for periodic forwarding."""
    try:
        os.makedirs(os.path.dirname(LAST_PROCESSED_IDS_FILE) or '.', exist_ok=True)
        with open(LAST_PROCESSED_IDS_FILE, "rb") as f:
            last_ids = pickle.load(f)
            logger.info(f"Loaded last processed IDs for {len(last_ids)} chats for periodic forwarding.")
            return last_ids
    except (FileNotFoundError, EOFError):
        logger.info("No previous last processed IDs found for periodic forwarding.")
        return {}
    except Exception as e:
        logger.error(f"Error loading last processed IDs file: {e}")
        return {}

def save_last_processed_ids(last_ids):
    """Saves the dictionary of last processed message IDs for periodic forwarding."""
    os.makedirs(os.path.dirname(LAST_PROCESSED_IDS_FILE) or '.', exist_ok=True)
    try:
        with open(LAST_PROCESSED_IDS_FILE, "wb") as f:
            pickle.dump(last_ids, f)
        # logger.debug(f"Saved last processed IDs for {len(last_ids)} chats.") # Use debug level
    except Exception as e:
        logger.error(f"Error saving last processed IDs file: {e}")


def get_periodic_forward_config():
    """Retrieves configuration for periodic forwarding from env vars."""
    source_chats_str = os.getenv("LIVE_SOURCE_CHATS")
    dest_chats_str = os.getenv("LIVE_DEST_CHATS")
    include_types_str = os.getenv("LIVE_INCLUDE_TYPES")
    custom_caption = os.getenv("LIVE_CUSTOM_CAPTION", "")
    interval_str = os.getenv("LIVE_CHECK_INTERVAL_SECONDS", "3600")

    errors = []
    source_chat_ids = []
    dest_chat_ids = []
    include_types = [] # These are the *string* names from the env var
    interval = None

    if not source_chats_str:
        errors.append("LIVE_SOURCE_CHATS is not set in .env (comma-separated chat IDs).")
    else:
        try:
            source_chat_ids = [int(id.strip()) for id in source_chats_str.split(',') if id.strip()]
            if not source_chat_ids:
                 errors.append("LIVE_SOURCE_CHATS is set but contains no valid chat IDs.")
        except ValueError:
            errors.append("Invalid ID format in LIVE_SOURCE_CHATS. Use comma-separated integers.")

    if not dest_chats_str:
        errors.append("LIVE_DEST_CHATS is not set in .env (comma-separated chat IDs for periodic destinations).")
    else:
        try:
            dest_chat_ids = [int(id.strip()) for id in dest_chats_str.split(',') if id.strip()]
            if not dest_chat_ids:
                 errors.append("LIVE_DEST_CHATS is set but contains no valid chat IDs.")
        except ValueError:
            errors.append("Invalid ID format in LIVE_DEST_CHATS. Use comma-separated integers.")

    # Check if source and destination lists have the same length
    if source_chat_ids and dest_chat_ids and len(source_chat_ids) != len(dest_chat_ids):
        errors.append(f"Mismatch in count between LIVE_SOURCE_CHATS ({len(source_chat_ids)}) and LIVE_DEST_CHATS ({len(dest_chat_ids)}). They must be paired.")


    if not include_types_str:
        errors.append("LIVE_INCLUDE_TYPES is not set in .env (comma-separated message types, e.g., 'messageVideo,messageDocument').")
    else:
        include_types = [t.strip() for t in include_types_str.split(',') if t.strip()]
        if not include_types:
            errors.append("LIVE_INCLUDE_TYPES is set but contains no valid types.")

    try:
        interval = int(interval_str)
        if interval <= 0:
             errors.append("LIVE_CHECK_INTERVAL_SECONDS must be a positive integer.")
             interval = None
    except ValueError:
        errors.append("Invalid interval format in LIVE_CHECK_INTERVAL_SECONDS. Must be an integer.")
        interval = None

    # If there are validation errors, return None for all values
    if errors:
        logger.error("Periodic forwarding configuration errors:")
        for error in errors:
            logger.error(f"- {error}")
        return None, None, None, None, None

    logger.info("\nPeriodic Forwarding Configuration:")
    logger.info(f"  Source Chats: {source_chat_ids}")
    logger.info(f"  Destination Chats (Paired): {dest_chat_ids}")
    logger.info(f"  Include Types (string names): {include_types}")
    if custom_caption:
        logger.info(f"  Using Custom Caption: '{custom_caption}'")
    else:
        logger.info("  No Custom Caption set.")
    logger.info(f"  Check Interval: {interval} seconds ({interval/60:.1f} minutes)")

    return source_chat_ids, dest_chat_ids, include_types, custom_caption, interval

# Async function to map Pyrogram message object to our string type names
def get_pyrogram_message_type_string(message):
    """Attempts to get the string type name from a Pyrogram message object."""
    if message.empty:
        return "messageEmpty" # Example of handling specific Pyrogram types
    if message.service:
        # Pyrogram service messages have different structures
        # We might need to check message.service contents for specific types
        # For simplicity based on old EXCLUDE_TYPES, we'll broadly exclude service messages
        # if they fall into patterns like chat changes, calls, etc.
        # Pyrogram message object itself often indicates the service type
        # E.g., message.chat_photo_changed, message.chat_title_changed, message.group_chat_created, etc.
        # Let's handle common service message checks explicitly here
        if message.migrate_to_chat_id or message.migrate_from_chat_id or \
           message.new_chat_members or message.left_chat_member or \
           message.new_chat_photo or message.delete_chat_photo or \
           message.new_chat_title or message.group_chat_created or \
           message.supergroup_chat_created or message.channel_chat_created or \
           message.pinned_message or message.video_chat_scheduled or \
           message.video_chat_started or message.video_chat_ended or message.invite_to_video_chat or \
           message.auto_delete_time_changed:
             return "messageService" # Generic service type to exclude
        # Add checks for other service message types if necessary
        return "messageOtherService" # Catch-all for other service messages

    # Check media type for common file types
    if message.media:
        if message.media in MESSAGE_TYPE_MAP:
            return MESSAGE_TYPE_MAP[message.media]
        # Add other MessageMediaType mappings here if needed

    # Check for text message
    if message.text:
         return "messageText" # Or whatever string name you want for text

    # Check for sticker/gif/voice/video_note etc.
    # Pyrogram has specific attributes: message.sticker, message.animation, message.voice, message.video_note
    if message.sticker: return "messageSticker"
    if message.animation: return "messageAnimatedEmoji" # Map Pyrogram animation to old animated emoji type
    if message.voice: return "messageVoice"
    if message.video_note: return "messageVideoNote"
    if message.poll: return "messagePoll"
    if message.location: return "messageLocation"
    if message.venue: return "messageVenue"
    if message.contact: return "messageContact"
    if message.game: return "messageGame"
    if message.invoice: return "messageInvoice"
    if message.giveaway: return "messageGiveaway"
    if message.giveaway_launched: return "messageGiveawayLaunched"
    if message.message_auto_delete_timer_changed: return "messageAutoDeleteTimerChanged"
    if message.forum_topic_created: return "messageForumTopicCreated"
    if message.forum_topic_closed: return "messageForumTopicClosed"
    if message.forum_topic_reopened: return "messageForumTopicReopened"
    if message.forum_topic_edited: return "messageForumTopicEdited"
    if message.general_forum_topic_hidden: return "messageGeneralForumTopicHidden"
    if message.general_forum_topic_unhidden: return "messageGeneralForumTopicUnhidden"


    # If none of the above match, return a generic type or None
    return "messageUnknown" # Fallback type string

# Define which string types are globally excluded
GLOBAL_EXCLUDE_STRING_TYPES = [
    "messageService", # Excludes common service messages caught by get_pyrogram_message_type_string
    "messageOtherService", # Excludes other service messages
    "messageEmpty",
    "messageUnknown", # Exclude unknown types by default
    "messageAnimatedEmoji", # Example: exclude specific types not covered by service check but listed in original
    # Add other types from the original EXCLUDE_TYPES list if needed and they map to a string type
    # Note: Types like messageChatChangePhoto, messageChatChangeTitle etc. are often service messages
    # and should be caught by the "messageService" check if get_pyrogram_message_type_string handles them.
]


# Modified process_periodic_batch to be async and use Pyrogram
async def process_periodic_batch(client: Client, source_chat_id: int, dest_id: int, include_types: list[str], custom_caption: str, last_processed_ids: dict):
    """
    Fetches and processes new messages for a single source chat, sends to specific dest_id
    using Pyrogram.
    """
    last_id_for_chat = last_processed_ids.get(source_chat_id)
    processed_count = 0
    errors_count = 0
    new_last_id = last_id_for_chat
    fetch_limit = 200 # How many recent messages to fetch to find new ones

    if last_id_for_chat is None:
        logger.info(f"Initializing last processed ID for chat {source_chat_id}...")
        try:
            # Fetch the latest message to set the initial last_processed_id
            async for message in client.get_chat_history(source_chat_id, limit=1):
                last_processed_ids[source_chat_id] = message.id
                new_last_id = message.id
                logger.info(f"✅ Initialized last processed ID for chat {source_chat_id} to {new_last_id}. Will forward new messages > {new_last_id} in the next interval.")
                return new_last_id, 0, 0 # Exit after initializing
            # If no messages found in chat
            last_processed_ids[source_chat_id] = 0
            new_last_id = 0
            logger.warning(f"⚠️ Could not fetch latest message for chat {source_chat_id} (might be empty). Setting last processed ID to 0. Will check for messages > 0 in the next interval.")
            return new_last_id, 0, 0


        except Exception as e:
            logger.error(f"Error initializing last processed ID for chat {source_chat_id}: {e}")
            return last_id_for_chat, 0, 0 # Return old id and no counts

    logger.info(f"Checking chat {source_chat_id} for new messages since ID {last_id_for_chat} to forward to {dest_id}...")

    new_messages_to_process = []
    try:
        # Fetch recent messages. Pyrogram fetches newest first.
        # We iterate until we hit a message that's not newer than last_id_for_chat
        async for message in client.get_chat_history(source_chat_id, limit=fetch_limit):
            if message.id > last_id_for_chat:
                new_messages_to_process.append(message)
            else:
                # We've reached messages we've already processed or older
                break
        # If we fetched the maximum limit and the oldest message fetched is still > last_id_for_chat,
        # it means there are more than `Workspace_limit` new messages. We'll only process up to `Workspace_limit`
        # in this interval. The next interval will pick up the rest.
        if len(new_messages_to_process) == fetch_limit and new_messages_to_process[-1].id > last_id_for_chat:
             logger.warning(f"Fetched {fetch_limit} new messages for chat {source_chat_id}, and the oldest fetched ID ({new_messages_to_process[-1].id}) is still newer than the last processed ID ({last_id_for_chat}). More new messages might exist. They will be picked up in the next interval.")

    except Exception as e:
        logger.error(f"Error fetching recent history for chat {source_chat_id}: {e}")
        return last_id_for_chat, 0, 0 # Return old id and no counts


    if new_messages_to_process:
        # Sort messages by ID to process them in chronological order
        new_messages_to_process.sort(key=lambda m: m.id)
        logger.info(f"Found {len(new_messages_to_process)} new messages in chat {source_chat_id} to process for {dest_id}.")

        for message in new_messages_to_process:
            msg_id = message.id
            msg_type_string = get_pyrogram_message_type_string(message) # Get our string representation
            logger.info(f"Processing new message {msg_id} (type {msg_type_string}) from {source_chat_id} for forwarding to {dest_id}")

            # Check global excludes first
            if msg_type_string in GLOBAL_EXCLUDE_STRING_TYPES:
                 logger.debug(f"Message {msg_id} (type {msg_type_string}) in {source_chat_id} is globally excluded. Skipping.")
                 continue

            # Check if the type is in the user's INCLUDE_TYPES list
            if msg_type_string not in include_types:
                logger.debug(f"Message {msg_id} (type {msg_type_string}) in {source_chat_id} is not in periodic INCLUDE_TYPES {include_types}. Skipping.")
                continue

            # Process message based on type for sending
            try:
                caption_text = custom_caption if custom_caption else message.caption # Use custom caption if set, otherwise use original
                caption_obj = caption_text # Pyrogram send methods usually take a string caption

                if message.media == MessageMediaType.VIDEO and message.video:
                    # Send video
                    await client.send_video(
                        chat_id=dest_id,
                        video=message.video.file_id, # Use file_id for remote file
                        caption=caption_obj
                    )
                    logger.info(f"Successfully sent message {msg_id} (messageVideo) as new message to {dest_id}.")
                    processed_count += 1

                elif message.media == MessageMediaType.DOCUMENT and message.document:
                     # Send document
                     await client.send_document(
                        chat_id=dest_id,
                        document=message.document.file_id, # Use file_id for remote file
                        caption=caption_obj
                     )
                     logger.info(f"Successfully sent message {msg_id} (messageDocument) as new message to {dest_id}.")
                     processed_count += 1

                elif message.media == MessageMediaType.PHOTO and message.photo:
                     # Send photo
                     await client.send_photo(
                        chat_id=dest_id,
                        photo=message.photo.file_id, # Use file_id for remote file
                        caption=caption_obj
                     )
                     logger.info(f"Successfully sent message {msg_id} (messagePhoto) as new message to {dest_id}.")
                     processed_count += 1

                elif message.text:
                     # Send text message
                     await client.send_message(
                        chat_id=dest_id,
                        text=message.text # Use message.text directly
                        # Note: custom_caption for text messages might replace the original text
                        # If you want to prepend/append, modify caption_obj accordingly
                     )
                     logger.info(f"Successfully sent message {msg_id} (messageText) as new message to {dest_id}.")
                     processed_count += 1

                # Add handling for other message types from your INCLUDE_TYPES if needed
                # e.g., elif message.media == MessageMediaType.ANIMATION and message.animation: pass # Already excluded by GLOBAL_EXCLUDE_STRING_TYPES

                else:
                     # Message type is in INCLUDE_TYPES but we don't have explicit sending logic for it yet
                     logger.warning(f"Message {msg_id} (type {msg_type_string}) from {source_chat_id} is in INCLUDE_TYPES but has no specific sending logic implemented. Skipping.")
                     errors_count += 1


            except Exception as e:
                logger.error(f"Exception during sending message {msg_id} (type {msg_type_string}) to {dest_id}: {e}")
                errors_count += 1

        # Update new_last_id to the ID of the latest message processed in this batch
        if new_messages_to_process:
             new_last_id = new_messages_to_process[-1].id

        # Update the dictionary passed in with the new last processed ID for this source chat
        last_processed_ids[source_chat_id] = new_last_id

        logger.info(f"✅ Finished processing batch for chat {source_chat_id} -> {dest_id}. Processed: {processed_count}, Errors: {errors_count}. New last processed ID for this chat: {new_last_id}")

    else:
        logger.info(f"No new messages found in chat {source_chat_id} since ID {last_id_for_chat}.")


    return new_last_id, processed_count, errors_count

# Modified monitor_live to be async and loop through source-dest pairs
async def monitor_live(client: Client):
    """Starts periodic checking and forwarding of new messages from multiple sources to multiple destinations."""
    source_chat_ids, dest_chat_ids, include_types, custom_caption, interval = get_periodic_forward_config()

    # Check if config is valid and lists are paired
    if source_chat_ids is None or dest_chat_ids is None or include_types is None or interval is None or len(source_chat_ids) != len(dest_chat_ids):
        logger.error("🔴 Periodic forwarding cannot start due to configuration errors or unpaired source/destination lists.")
        return

    # Create the source to destination mapping
    source_dest_map = dict(zip(source_chat_ids, dest_chat_ids))

    logger.info("\nStarting periodic forwarding...")
    logger.info(f"  Monitoring {len(source_dest_map)} Source-Destination Pairs:")
    for src, dst in source_dest_map.items():
        logger.info(f"    - Source: {src} -> Destination: {dst}")
    logger.info(f"  Including Types (string names): {include_types}")
    if custom_caption:
        logger.info(f"  Using Custom Caption: '{custom_caption}'")
    else:
        logger.info("  No Custom Caption set.")
    logger.info(f"  Check Interval: {interval} seconds ({interval/60:.1f} minutes)")

    logger.info("Periodic forwarding is active. Press Ctrl+C to stop.")

    # Load state once at the beginning
    last_processed_ids = load_last_processed_ids()

    try:
        while True:
            start_time = time.time()
            logger.info(f"Starting periodic check for new messages at {datetime.now()}")

            total_processed_this_interval = 0
            total_errors_this_interval = 0
            # Work directly on the loaded dictionary as process_periodic_batch updates it
            updated_last_ids = last_processed_ids # Use the loaded dictionary reference

            # Loop through each source-destination pair
            for source_chat_id, dest_chat_id in source_dest_map.items():
                try:
                    new_last_id, processed_count, errors_count = await process_periodic_batch(
                        client, source_chat_id, dest_chat_id, include_types, custom_caption, updated_last_ids
                    )
                    # process_periodic_batch updates updated_last_ids dictionary internally

                    total_processed_this_interval += processed_count
                    total_errors_this_interval += errors_count

                except Exception as e:
                    logger.error(f"Unhandled exception processing chat pair {source_chat_id} -> {dest_chat_id} in interval: {e}")

            # Save state after processing all chats in the interval
            save_last_processed_ids(updated_last_ids)

            end_time = time.time()
            duration = end_time - start_time
            logger.info(f"Periodic check finished. Processed {total_processed_this_interval} messages with {total_errors_this_interval} errors in {duration:.2f} seconds.")

            sleep_time = interval - duration
            if sleep_time > 1:
                logger.info(f"Sleeping for {sleep_time:.1f} seconds until next check...")
                await asyncio.sleep(sleep_time) # Use asyncio.sleep in async functions
            elif sleep_time > 0:
                 await asyncio.sleep(sleep_time)
            else:
                logger.warning(f"Processing took longer than the interval ({duration:.2f}s > {interval}s). Checking immediately.")


    except asyncio.CancelledError:
        logger.info("\n🔴 Periodic forwarding task cancelled.")
        # Save state on cancellation might be good, but might happen during a write.
        # Let's rely on the periodic saves.
        pass
    except KeyboardInterrupt:
        logger.info("\n🔴 Stopping periodic forwarding via KeyboardInterrupt.")
        # asyncio.run handles KeyboardInterrupt by raising CancelledError
        pass
    except Exception as e:
        logger.error(f"An unexpected error occurred during periodic forwarding loop: {e}", exc_info=True)


# Async main function to handle client lifecycle and tasks
async def main_async():
    """Initializes client, starts Flask, and runs the periodic monitor."""
    os.makedirs("data", exist_ok=True)
    check_env_vars()

    # --- START THE DUMMY WEB SERVER THREAD ---
    # Start Flask thread before Telegram client
    flask_thread = threading.Thread(target=run_flask_app, daemon=True)
    try:
        flask_thread.start()
        logger.info(f"Dummy web server thread started.")
    except Exception as e:
        logger.error(f"⚠️ Failed to start dummy web server thread: {e}")
    # --- END START WEB SERVER ---

    # Initialize Pyrogram client
    client = initialize_pyrogram()
    if client is None:
        logger.error("Failed to initialize Pyrogram client. Exiting.")
        sys.exit(1)

    try:
        logger.info("Starting Pyrogram client...")
        await client.start()
        logger.info("✅ Pyrogram client started.")

        # Run the periodic monitor
        await monitor_live(client)

    except Exception as e:
        logger.error(f"An error occurred during client startup or periodic monitoring: {e}", exc_info=True)
    finally:
        if client and client.is_running:
            logger.info("Stopping Pyrogram client...")
            await client.stop()
            logger.info("Pyrogram client stopped.")
        # Flask thread is daemon, will exit with main process


# Main execution block
if __name__ == "__main__":
    # Check if essential Pyrogram vars are present before even trying asyncio
    if not all(os.getenv(var) for var in ["API_ID", "API_HASH", "PHONE", "SESSION_STRING"]):
         check_env_vars() # This will log warnings
         logger.error("Missing essential Pyrogram environment variables. Please update your .env file.")
         sys.exit(1)

    # Run the async main function
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Script interrupted by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred in the main loop: {e}", exc_info=True)

