# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic


import os
import re
import yt_dlp
import random
import asyncio
import aiohttp
import subprocess
from pathlib import Path

from py_yt import Playlist, VideosSearch

from anony import logger
from anony.helpers import Track, utils


CHUNK_DURATION = 300  # 5 minutes per chunk


class YouTube:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.cookies = []
        self.checked = False
        self.cookie_dir = "anony/cookies"
        self.warned = False
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )

    def get_cookies(self):
        if not self.checked:
            for file in os.listdir(self.cookie_dir):
                if file.endswith(".txt"):
                    self.cookies.append(f"{self.cookie_dir}/{file}")
            self.checked = True
        if not self.cookies:
            if not self.warned:
                self.warned = True
                logger.warning("Cookies are missing; downloads might fail.")
            return None
        return random.choice(self.cookies)

    async def save_cookies(self, urls: list[str]) -> None:
        logger.info("Saving cookies from urls...")
        async with aiohttp.ClientSession() as session:
            for url in urls:
                name = url.split("/")[-1]
                link = "https://batbin.me/raw/" + name
                async with session.get(link) as resp:
                    resp.raise_for_status()
                    with open(f"{self.cookie_dir}/{name}.txt", "wb") as fw:
                        fw.write(await resp.read())
        logger.info(f"Cookies saved in {self.cookie_dir}.")

    def valid(self, url: str) -> bool:
        return bool(re.match(self.regex, url))

    async def search(self, query: str, m_id: int, video: bool = False) -> Track | None:
        _search = VideosSearch(query, limit=1, with_live=False)
        results = await _search.next()
        if results and results["result"]:
            data = results["result"][0]
            return Track(
                id=data.get("id"),
                channel_name=data.get("channel", {}).get("name"),
                duration=data.get("duration"),
                duration_sec=utils.to_seconds(data.get("duration")),
                message_id=m_id,
                title=data.get("title")[:25],
                thumbnail=data.get("thumbnails", [{}])[-1].get("url").split("?")[0],
                url=data.get("link"),
                view_count=data.get("viewCount", {}).get("short"),
                video=video,
            )
        return None

    async def playlist(self, limit: int, user: str, url: str, video: bool) -> list[Track | None]:
        tracks = []
        try:
            plist = await Playlist.get(url)
            for data in plist["videos"][:limit]:
                track = Track(
                    id=data.get("id"),
                    channel_name=data.get("channel", {}).get("name", ""),
                    duration=data.get("duration"),
                    duration_sec=utils.to_seconds(data.get("duration")),
                    title=data.get("title")[:25],
                    thumbnail=data.get("thumbnails")[-1].get("url").split("?")[0],
                    url=data.get("link").split("&list=")[0],
                    user=user,
                    view_count="",
                    video=video,
                )
                tracks.append(track)
        except Exception:
            pass
        return tracks

    async def get_direct_url(self, video_id: str) -> str | None:
        """Extract the direct audio stream URL without downloading the file."""
        url = self.base + video_id
        cookie = self.get_cookies()

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "cookiefile": cookie,
            "format": "bestaudio[ext=webm][acodec=opus]/bestaudio",
        }

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        return None
                    if "url" in info:
                        return info["url"]
                    for fmt in info.get("requested_formats", []):
                        if fmt.get("acodec") != "none":
                            return fmt.get("url")
                    return None
                except Exception as ex:
                    logger.warning("Direct URL extraction failed: %s", ex)
                    return None

        return await asyncio.to_thread(_extract)

    async def download_chunk(
        self,
        video_id: str,
        direct_url: str,
        chunk_index: int,
        chunk_duration: int = CHUNK_DURATION,
    ) -> str | None:
        """Download a fixed-duration audio chunk via ffmpeg."""
        start_time = chunk_index * chunk_duration
        filename = f"downloads/{video_id}_chunk{chunk_index}.webm"

        if Path(filename).exists() and Path(filename).stat().st_size > 0:
            return filename

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", direct_url,
            "-t", str(chunk_duration),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-fflags", "+genpts",
            filename,
            "-loglevel", "error",
        ]

        def _run():
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=120)
                if result.returncode == 0 and Path(filename).exists():
                    if Path(filename).stat().st_size > 0:
                        return filename
                logger.warning(
                    "ffmpeg chunk %d failed: %s",
                    chunk_index,
                    result.stderr.decode(errors="ignore"),
                )
                return None
            except Exception as ex:
                logger.warning("Chunk download error (chunk %d): %s", chunk_index, ex)
                return None

        return await asyncio.to_thread(_run)

    async def download(self, video_id: str, video: bool = False) -> str | None:
        url = self.base + video_id
        ext = "mp4" if video else "webm"
        filename = f"downloads/{video_id}.{ext}"

        if Path(filename).exists():
            return filename

        cookie = self.get_cookies()
        base_opts = {
            "outtmpl": "downloads/%(id)s.%(ext)s",
            "quiet": True,
            "noplaylist": True,
            "geo_bypass": True,
            "no_warnings": True,
            "overwrites": False,
            "nocheckcertificate": True,
            "cookiefile": cookie,
            "concurrent_fragment_downloads": 5,
            "http_chunk_size": 10485760,
            "retries": 10,
            "fragment_retries": 10,
        }

        if video:
            ydl_opts = {
                **base_opts,
                "format": "(bestvideo[height<=?720][width<=?1280][ext=mp4])+(bestaudio)",
                "merge_output_format": "mp4",
            }
        else:
            ydl_opts = {
                **base_opts,
                "format": "bestaudio[ext=webm][acodec=opus]",
            }

        def _download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    ydl.download([url])
                except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError):
                    if cookie: self.cookies.remove(cookie)
                    return None
                except Exception as ex:
                    logger.warning("Download failed: %s", ex)
                    return None
            return filename

        return await asyncio.to_thread(_download)
