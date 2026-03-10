# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic

import math
import asyncio
from pathlib import Path

from ntgcalls import (ConnectionNotFound, TelegramServerError,
                      RTMPStreamingUnsupported, ConnectionError)
from pyrogram.errors import (ChatSendMediaForbidden, ChatSendPhotosForbidden,
                             MessageIdInvalid)
from pyrogram.types import InputMediaPhoto, Message
from pytgcalls import PyTgCalls, exceptions, types
from pytgcalls.pytgcalls_session import PyTgCallsSession

from anony import app, config, db, lang, logger, queue, userbot, yt
from anony.helpers import Media, Track, buttons, thumb


class TgCall(PyTgCalls):
    def __init__(self):
        self.clients = []
        # chunk_states[chat_id] = {
        #   track, direct_url, current_chunk, total_chunks,
        #   current_path, next_path, next_task
        # }
        self.chunk_states: dict[int, dict] = {}

    async def pause(self, chat_id: int) -> bool:
        client = await db.get_assistant(chat_id)
        await db.playing(chat_id, paused=True)
        return await client.pause(chat_id)

    async def resume(self, chat_id: int) -> bool:
        client = await db.get_assistant(chat_id)
        await db.playing(chat_id, paused=False)
        return await client.resume(chat_id)

    async def stop(self, chat_id: int) -> None:
        client = await db.get_assistant(chat_id)
        queue.clear(chat_id)
        await db.remove_call(chat_id)

        # Clean up any active chunk state
        state = self.chunk_states.pop(chat_id, None)
        if state:
            task = state.get("next_task")
            if task and not task.done():
                task.cancel()
            for key in ("current_path", "next_path"):
                p = state.get(key)
                if p:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass

        try:
            await client.leave_call(chat_id, close=False)
        except Exception:
            pass

    async def play_media(
        self,
        chat_id: int,
        message: Message,
        media: Media | Track,
        seek_time: int = 0,
    ) -> None:
        client = await db.get_assistant(chat_id)
        _lang = await lang.get_lang(chat_id)
        _thumb = (
            await thumb.generate(media)
            if isinstance(media, Track)
            else config.DEFAULT_THUMB
        ) if config.THUMB_GEN else None

        if not media.file_path:
            await message.edit_text(_lang["error_no_file"].format(config.SUPPORT_CHAT))
            return await self.play_next(chat_id)

        stream = types.MediaStream(
            media_path=media.file_path,
            audio_parameters=types.AudioQuality.HIGH,
            video_parameters=types.VideoQuality.HD_720p,
            audio_flags=types.MediaStream.Flags.REQUIRED,
            video_flags=(
                types.MediaStream.Flags.AUTO_DETECT
                if media.video
                else types.MediaStream.Flags.IGNORE
            ),
            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
        )
        try:
            await client.play(
                chat_id=chat_id,
                stream=stream,
                config=types.GroupCallConfig(auto_start=False),
            )
            if not seek_time:
                media.time = 1
                await db.add_call(chat_id)
                text = _lang["play_media"].format(
                    media.url,
                    media.title,
                    media.duration,
                    media.user,
                )
                keyboard = buttons.controls(chat_id)
                try:
                    if _thumb:
                        await message.edit_media(
                            media=InputMediaPhoto(
                                media=_thumb,
                                caption=text,
                            ),
                            reply_markup=keyboard,
                        )
                    else:
                        await message.edit_text(text, reply_markup=keyboard)
                except (ChatSendMediaForbidden, ChatSendPhotosForbidden, MessageIdInvalid):
                    if _thumb:
                        sent = await app.send_photo(
                            chat_id=chat_id,
                            photo=_thumb,
                            caption=text,
                            reply_markup=keyboard,
                        )
                    else:
                        sent = await app.send_message(
                            chat_id=chat_id,
                            text=text,
                            reply_markup=keyboard,
                        )
                    media.message_id = sent.id
        except FileNotFoundError:
            await message.edit_text(_lang["error_no_file"].format(config.SUPPORT_CHAT))
            await self.play_next(chat_id)
        except exceptions.NoActiveGroupCall:
            await self.stop(chat_id)
            await message.edit_text(_lang["error_no_call"])
        except exceptions.NoAudioSourceFound:
            await message.edit_text(_lang["error_no_audio"])
            await self.play_next(chat_id)
        except (ConnectionError, ConnectionNotFound, TelegramServerError):
            await self.stop(chat_id)
            await message.edit_text(_lang["error_tg_server"])
        except RTMPStreamingUnsupported:
            await self.stop(chat_id)
            await message.edit_text(_lang["error_rtmp"])

    # ── Chunk streaming ────────────────────────────────────────────────────

    async def _bg_download_chunk(self, chat_id: int, chunk_index: int) -> str | None:
        """Background task: download the next chunk and store result in state."""
        state = self.chunk_states.get(chat_id)
        if not state:
            return None

        path = await yt.download_chunk(
            state["track"].id,
            state["direct_url"],
            chunk_index,
        )

        # Write result back into state so advance_chunk can find it
        if chat_id in self.chunk_states:
            self.chunk_states[chat_id]["next_path"] = path

        return path

    async def play_chunked(
        self,
        chat_id: int,
        message: Message,
        track: Track,
        direct_url: str,
    ) -> None:
        """
        Start chunk-pipeline playback:
          chunk 0 downloads → plays  
          chunk 1 downloads in background while chunk 0 plays  
          when chunk 0 ends → play chunk 1, delete chunk 0, download chunk 2 …
        """
        total_chunks = max(1, math.ceil(track.duration_sec / yt.CHUNK_DURATION))

        # Download first chunk before we can start playing
        chunk0_path = await yt.download_chunk(track.id, direct_url, 0)
        if not chunk0_path:
            # Fallback: full download
            logger.warning("Chunk 0 failed for %s, falling back to full download", track.id)
            track.file_path = await yt.download(track.id, video=track.video)
            return await self.play_media(chat_id, message, track)

        track.file_path = chunk0_path

        state = {
            "track": track,
            "direct_url": direct_url,
            "current_chunk": 0,
            "total_chunks": total_chunks,
            "current_path": chunk0_path,
            "next_path": None,
            "next_task": None,
        }
        self.chunk_states[chat_id] = state

        # Kick off download of chunk 1 in background while chunk 0 plays
        if total_chunks > 1:
            state["next_task"] = asyncio.create_task(
                self._bg_download_chunk(chat_id, 1)
            )

        await self.play_media(chat_id, message, track)

    async def advance_chunk(self, chat_id: int) -> None:
        """
        Called when the current chunk's stream ends.
        Moves to the next chunk, deletes the finished one, and
        pre-fetches the one after that.
        """
        state = self.chunk_states.get(chat_id)
        if not state:
            return await self.play_next(chat_id)

        current_chunk = state["current_chunk"]
        total_chunks = state["total_chunks"]
        next_chunk_index = current_chunk + 1

        # Delete the chunk that just finished
        prev_path = state.get("current_path")
        if prev_path:
            try:
                Path(prev_path).unlink(missing_ok=True)
            except Exception:
                pass

        # No more chunks → move to next song in queue
        if next_chunk_index >= total_chunks:
            self.chunk_states.pop(chat_id, None)
            task = state.get("next_task")
            if task and not task.done():
                task.cancel()
            # Also clean up the pre-fetched-but-unused next chunk if any
            if state.get("next_path"):
                try:
                    Path(state["next_path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            return await self.play_next(chat_id)

        # Wait for the pre-fetched next chunk
        next_path = state.get("next_path")
        if not next_path:
            task = state.get("next_task")
            if task:
                try:
                    next_path = await task
                except Exception:
                    next_path = None

        if not next_path:
            logger.warning("Next chunk download failed for chat %d", chat_id)
            self.chunk_states.pop(chat_id, None)
            return await self.play_next(chat_id)

        # Update state for the chunk we're about to play
        state["current_chunk"] = next_chunk_index
        state["current_path"] = next_path
        state["next_path"] = None
        state["next_task"] = None

        # Pre-fetch the chunk after that
        next_next_index = next_chunk_index + 1
        if next_next_index < total_chunks:
            state["next_task"] = asyncio.create_task(
                self._bg_download_chunk(chat_id, next_next_index)
            )

        # Play the next chunk (no message edit — now-playing card already shown)
        client = await db.get_assistant(chat_id)
        stream = types.MediaStream(
            media_path=next_path,
            audio_parameters=types.AudioQuality.HIGH,
            audio_flags=types.MediaStream.Flags.REQUIRED,
            video_flags=types.MediaStream.Flags.IGNORE,
        )
        try:
            await client.play(
                chat_id=chat_id,
                stream=stream,
                config=types.GroupCallConfig(auto_start=False),
            )
        except Exception as ex:
            logger.error("Chunk advance play error (chat %d): %s", chat_id, ex)
            self.chunk_states.pop(chat_id, None)
            await self.play_next(chat_id)

    # ── Queue helpers ──────────────────────────────────────────────────────

    async def replay(self, chat_id: int) -> None:
        if not await db.get_call(chat_id):
            return

        media = queue.get_current(chat_id)
        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_again"])
        await self.play_media(chat_id, msg, media)

    async def play_next(self, chat_id: int) -> None:
        # Always clear any leftover chunk state when moving to a new queue item
        state = self.chunk_states.pop(chat_id, None)
        if state:
            task = state.get("next_task")
            if task and not task.done():
                task.cancel()

        media = queue.get_next(chat_id)
        try:
            if media.message_id:
                await app.delete_messages(
                    chat_id=chat_id,
                    message_ids=media.message_id,
                    revoke=True,
                )
                media.message_id = 0
        except Exception:
            pass

        if not media:
            return await self.stop(chat_id)

        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_next"])
        if not media.file_path:
            media.file_path = await yt.download(media.id, video=media.video)
            if not media.file_path:
                await self.stop(chat_id)
                return await msg.edit_text(
                    _lang["error_no_file"].format(config.SUPPORT_CHAT)
                )

        media.message_id = msg.id
        await self.play_media(chat_id, msg, media)

    async def ping(self) -> float:
        pings = [client.ping for client in self.clients]
        return round(sum(pings) / len(pings), 2)

    async def decorators(self, client: PyTgCalls) -> None:
        @client.on_update()
        async def update_handler(_, update: types.Update) -> None:
            if isinstance(update, types.StreamEnded):
                if update.stream_type == types.StreamEnded.Type.AUDIO:
                    chat_id = update.chat_id
                    if chat_id in self.chunk_states:
                        await self.advance_chunk(chat_id)
                    else:
                        await self.play_next(chat_id)
            elif isinstance(update, types.ChatUpdate):
                if update.status in [
                    types.ChatUpdate.Status.KICKED,
                    types.ChatUpdate.Status.LEFT_GROUP,
                    types.ChatUpdate.Status.CLOSED_VOICE_CHAT,
                ]:
                    await self.stop(update.chat_id)

    async def boot(self) -> None:
        PyTgCallsSession.notice_displayed = True
        for ub in userbot.clients:
            client = PyTgCalls(ub, cache_duration=100)
            await client.start()
            self.clients.append(client)
            await self.decorators(client)
        logger.info("PyTgCalls client(s) started.")
