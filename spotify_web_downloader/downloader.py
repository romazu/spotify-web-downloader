from __future__ import annotations

import datetime
import functools
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Callable, Any

import requests
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from pywidevine import Cdm, Device

from .constants import *
from .enums import RemuxMode
from .hardcoded_wvd import HARDCODED_WVD
from .models import DownloadQueueItem, UrlInfo
from .spotify_api import SpotifyApi


class Downloader:
    ILLEGAL_CHARACTERS_REGEX = r'[\\/:*?"<>|;]'

    def __init__(
        self,
        spotify_api: SpotifyApi,
        output_path: Path = Path("./Spotify"),
        temp_path: Path = Path("./temp"),
        wvd_path: Path = None,

        # TODO: Maybe use ExternalUtilities dataclass for all these utilities paths.
        ffmpeg_path: str = "ffmpeg",
        mp4box_path: str = "MP4Box",
        mp4decrypt_path: str = "mp4decrypt",
        aria2c_path: str = "aria2c",
        nm3u8dlre_path: str = "N_m3u8DL-RE",

        remux_mode: RemuxMode = RemuxMode.FFMPEG,
        date_tag_template: str = "%Y-%m-%dT%H:%M:%SZ",
        exclude_tags: str = None,
        truncate: int = 40,
        silence: bool = False,
    ):
        self.spotify_api = spotify_api
        self.output_path = output_path
        self.temp_path = temp_path
        self.wvd_path = wvd_path
        self.ffmpeg_path = ffmpeg_path
        self.mp4box_path = mp4box_path
        self.mp4decrypt_path = mp4decrypt_path
        self.aria2c_path = aria2c_path
        self.nm3u8dlre_path = nm3u8dlre_path
        self.remux_mode = remux_mode
        self.date_tag_template = date_tag_template
        self.exclude_tags = exclude_tags
        self.truncate = truncate
        self.silence = silence
        self._set_binaries_full_path()
        self._set_exclude_tags_list()
        self._set_truncate()
        self._set_subprocess_additional_args()

    def _set_binaries_full_path(self):
        self.ffmpeg_path_full = shutil.which(self.ffmpeg_path)
        self.mp4box_path_full = shutil.which(self.mp4box_path)
        self.mp4decrypt_path_full = shutil.which(self.mp4decrypt_path)
        self.aria2c_path_full = shutil.which(self.aria2c_path)
        self.nm3u8dlre_path_full = shutil.which(self.nm3u8dlre_path)

    def _set_exclude_tags_list(self):
        self.exclude_tags_list = (
            [i.lower() for i in self.exclude_tags.split(",")]
            if self.exclude_tags is not None
            else []
        )

    def _set_truncate(self):
        self.truncate = None if self.truncate < 4 else self.truncate

    def _set_subprocess_additional_args(self):
        if self.silence:
            self.subprocess_additional_args = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
        else:
            self.subprocess_additional_args = {}

    def set_cdm(self) -> None:
        if self.wvd_path:
            self.cdm = Cdm.from_device(Device.load(self.wvd_path))
        else:
            self.cdm = Cdm.from_device(Device.loads(HARDCODED_WVD))

    def get_url_info(self, url: str) -> UrlInfo:
        url_regex_result = re.search(r"(album|playlist|track|show|episode)/(\w{22})", url)
        if url_regex_result is None:
            raise Exception("Invalid URL")
        return UrlInfo(type=url_regex_result.group(1), id=url_regex_result.group(2))

    def get_download_queue(self, url_info: UrlInfo) -> list[DownloadQueueItem]:
        download_queue = []
        if url_info.type == "album":
            download_queue.extend(
                [
                    DownloadQueueItem(metadata=track_metadata)
                    for track_metadata in self.spotify_api.get_album(url_info.id)[
                        "tracks"
                    ]["items"]
                ]
            )
        elif url_info.type == "playlist":
            download_queue.extend(
                [
                    DownloadQueueItem(metadata=track_metadata["track"])
                    for track_metadata in self.spotify_api.get_playlist(url_info.id)[
                        "tracks"
                    ]["items"]
                ]
            )
        elif url_info.type == "track":
            download_queue.append(
                DownloadQueueItem(metadata=self.spotify_api.get_track(url_info.id))
            )
        elif url_info.type == "show":
            download_queue.extend(
                [
                    DownloadQueueItem(metadata=episode_metadata)
                    for episode_metadata in self.spotify_api.get_show(url_info.id)[
                        "episodes"
                    ]["items"]
                ]
            )
        elif url_info.type == "episode":
            download_queue.append(
                DownloadQueueItem(metadata=self.spotify_api.get_episode(url_info.id))
            )
        return download_queue

    def get_sanitized_string(self, dirty_string: str, is_folder: bool) -> str:
        dirty_string = re.sub(self.ILLEGAL_CHARACTERS_REGEX, "_", dirty_string)
        if is_folder:
            dirty_string = dirty_string[: self.truncate]
            if dirty_string.endswith("."):
                dirty_string = dirty_string[:-1] + "_"
        else:
            if self.truncate is not None:
                dirty_string = dirty_string[: self.truncate - 4]
        return dirty_string.strip()

    def get_datetime_from_metadata_date(self, metadata_gid_release_date: dict) -> datetime.datetime:
        if metadata_gid_release_date.get("day"):
            datetime_obj = datetime.datetime(
                year=metadata_gid_release_date["year"],
                month=metadata_gid_release_date["month"],
                day=metadata_gid_release_date["day"],
            )
        elif metadata_gid_release_date.get("month"):
            datetime_obj = datetime.datetime(
                year=metadata_gid_release_date["year"],
                month=metadata_gid_release_date["month"],
                day=1,
            )
        else:
            datetime_obj = datetime.datetime(
                year=metadata_gid_release_date["year"],
                month=1,
                day=1,
            )
        return datetime_obj

    def get_release_date_datetime_obj(self, metadata_gid: dict) -> datetime.datetime:
        metadata_gid_release_date = metadata_gid["album"]["date"]
        return self.get_datetime_from_metadata_date(metadata_gid_release_date)

    def get_release_date_tag(self, datetime_obj: datetime.datetime) -> str:
        return datetime_obj.strftime(self.date_tag_template)

    def get_artist(self, artist_list: list[dict]) -> str:
        if len(artist_list) == 1:
            return artist_list[0]["name"]
        return (
            ", ".join(i["name"] for i in artist_list[:-1])
            + f' & {artist_list[-1]["name"]}'
        )

    def get_cover_url(self, metadata_gid: dict, size: str) -> str:
        return "https://i.scdn.co/image/" + next(
            i["file_id"]
            for i in metadata_gid["album"]["cover_group"]["image"]
            if i["size"] == size
        )

    def get_encrypted_path(
        self,
        track_id: str,
        file_extension: str,
    ) -> Path:
        return self.temp_path / (f"{track_id}_encrypted" + file_extension)

    def get_decrypted_path(
        self,
        track_id: str,
        file_extension: str,
    ) -> Path:
        return self.temp_path / (f"{track_id}_decrypted" + file_extension)

    def get_remuxed_path(
        self,
        track_id: str,
        file_extension: str,
    ) -> Path:
        return self.temp_path / (f"{track_id}_remuxed" + file_extension)

    def decrypt_mp4decrypt(
        self,
        encrypted_path: Path,
        decrypted_path: Path,
        decryption_key: str,
    ):
        subprocess.run(
            [
                self.mp4decrypt_path_full,
                encrypted_path,
                "--key",
                f"1:{decryption_key}",
                decrypted_path,
            ],
            check=True,
            **self.subprocess_additional_args,
        )

    @staticmethod
    @functools.lru_cache()
    def get_image_bytes(url: str) -> bytes:
        return requests.get(url).content

    def apply_tags(self, fixed_location: Path, tags: dict, cover_url: str):
        to_apply_tags = [
            tag_name
            for tag_name in tags.keys()
            if tag_name not in self.exclude_tags_list
        ]
        mp4_tags = {}
        for tag_name in to_apply_tags:
            if tag_name in ("disc", "disc_total"):
                if mp4_tags.get("disk") is None:
                    mp4_tags["disk"] = [[0, 0]]
                if tag_name == "disc":
                    mp4_tags["disk"][0][0] = tags[tag_name]
                elif tag_name == "disc_total":
                    mp4_tags["disk"][0][1] = tags[tag_name]
            elif tag_name in ("track", "track_total"):
                if mp4_tags.get("trkn") is None:
                    mp4_tags["trkn"] = [[0, 0]]
                if tag_name == "track":
                    mp4_tags["trkn"][0][0] = tags[tag_name]
                elif tag_name == "track_total":
                    mp4_tags["trkn"][0][1] = tags[tag_name]
            elif tag_name == "compilation":
                mp4_tags["cpil"] = tags["compilation"]
            elif tag_name == "isrc":
                mp4_tags["----:com.apple.iTunes:ISRC"] = [
                    MP4FreeForm(tags["isrc"].encode("utf-8"))
                ]
            elif tag_name == "label":
                mp4_tags["----:com.apple.iTunes:LABEL"] = [
                    MP4FreeForm(tags["label"].encode("utf-8"))
                ]
            elif (
                MP4_TAGS_MAP.get(tag_name) is not None
                and tags.get(tag_name) is not None
            ):
                mp4_tags[MP4_TAGS_MAP[tag_name]] = [tags[tag_name]]
        if "cover" not in self.exclude_tags_list:
            mp4_tags["covr"] = [
                MP4Cover(
                    self.get_image_bytes(cover_url), imageformat=MP4Cover.FORMAT_JPEG
                )
            ]
        mp4 = MP4(fixed_location)
        mp4.clear()
        mp4.update(mp4_tags)
        mp4.save()

    def move_to_final_path(self, fixed_path: Path, final_path: Path):
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(fixed_path, final_path)

    @functools.lru_cache()
    def save_cover(self, cover_path: Path, cover_url: str):
        cover_path.write_bytes(self.get_image_bytes(cover_url))

    def cleanup_temp_path(self):
        shutil.rmtree(self.temp_path)


class DownloadManager:
    def __init__(
            self,
            logger,
            downloader,
            print_exceptions,
            item_name,
            on_error: Optional[Callable[[BaseException], Any]] = None
    ):
        self.logger = logger
        self.downloader = downloader
        self.print_exceptions = print_exceptions
        self.item_name = item_name
        self.on_error = on_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value is not None:
            error_msg = f'Failed to download "{self.item_name}"'
            if self.on_error:
                self.on_error(exc_value)
            self.logger.error(
                error_msg,
                exc_info=self.print_exceptions,
            )
        if self.downloader.temp_path.exists():
            self.logger.debug(f'Cleaning up "{self.downloader.temp_path}"')
            self.downloader.cleanup_temp_path()
        # Return False to propagate the exception, if any.
        return False
