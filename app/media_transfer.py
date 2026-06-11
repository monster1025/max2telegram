from pathlib import Path
from uuid import uuid4

import aiohttp
import structlog

from app.models.tasks import MediaItem

logger = structlog.get_logger(__name__)

_MAX_CDN_HEADERS = {
  "User-Agent": (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
  ),
}


def tmp_dir(data_dir: str) -> Path:
  path = Path(data_dir) / "tmp"
  path.mkdir(parents=True, exist_ok=True)
  return path


def _item_field(item: MediaItem | dict, name: str, default=None):
  if isinstance(item, MediaItem):
    return getattr(item, name, default)
  return item.get(name, default)


def _default_file_name(kind: str) -> str:
  if "photo" in kind:
    return f"{uuid4().hex}.jpg"
  if "video" in kind:
    return f"{uuid4().hex}.mp4"
  return f"{uuid4().hex}.bin"


def resolve_file_name(item: MediaItem | dict) -> str:
  kind = _item_field(item, "kind", "") or ""
  file_name = _item_field(item, "file_name")
  if file_name:
    return file_name
  return _default_file_name(kind)


def max_image_url_candidates(url: str) -> list[str]:
  normalized = url.strip()
  if not normalized:
    return []
  if normalized.startswith("//"):
    normalized = f"https:{normalized}"
  candidates = [normalized]
  if "i.oneme.ru" in normalized and "size=" not in normalized:
    sep = "&" if "?" in normalized else "?"
    for size in (512, 256, 128):
      sized = f"{normalized}{sep}size={size}"
      if sized not in candidates:
        candidates.append(sized)
  return candidates


async def download_url_to_file(
  session: aiohttp.ClientSession,
  url: str,
  dest: Path,
) -> bool:
  return await _download_url(session, url, dest)


async def download_max_image_url(
  session: aiohttp.ClientSession,
  url: str,
  dest: Path,
) -> bool:
  for candidate in max_image_url_candidates(url):
    if await _download_url(session, candidate, dest):
      if dest.stat().st_size > 0:
        return True
      dest.unlink(missing_ok=True)
  return False


async def _download_url(
  session: aiohttp.ClientSession,
  url: str,
  dest: Path,
) -> bool:
  try:
    async with session.get(url, headers=_MAX_CDN_HEADERS) as resp:
      if resp.status != 200:
        logger.warning(
          "media_download_http_error",
          url=url,
          status=resp.status,
        )
        return False
      dest.write_bytes(await resp.read())
    return True
  except aiohttp.ClientError as exc:
    logger.warning("media_download_failed", url=url, error=str(exc))
    return False


async def _resolve_max_url(client, item: MediaItem | dict) -> str | None:
  chat_id = _item_field(item, "max_chat_id")
  message_id = _item_field(item, "max_message_id")
  if chat_id is None or message_id is None:
    return _item_field(item, "url")

  file_id = _item_field(item, "max_file_id")
  if file_id is not None:
    file_req = await client.get_file_by_id(chat_id, message_id, file_id)
    return file_req.url if file_req else None

  video_id = _item_field(item, "max_video_id")
  if video_id is not None:
    video = await client.get_video_by_id(chat_id, message_id, video_id)
    return video.url if video else None

  return _item_field(item, "url")


async def download_max_media(
  client,
  items: list[dict],
  dest_dir: Path,
) -> list[dict]:
  if not items:
    return []

  downloaded: list[dict] = []
  async with aiohttp.ClientSession() as session:
    for item in items:
      url = await _resolve_max_url(client, item)
      if not url:
        logger.warning(
          "max_media_url_unresolved",
          kind=item.get("kind"),
          max_file_id=item.get("max_file_id"),
          max_video_id=item.get("max_video_id"),
        )
        continue

      file_name = resolve_file_name(item)
      dest = dest_dir / f"{uuid4().hex}_{file_name}"
      if not await _download_url(session, url, dest):
        logger.warning(
          "max_media_download_skipped",
          kind=item.get("kind"),
          max_file_id=item.get("max_file_id"),
          max_video_id=item.get("max_video_id"),
        )
        continue

      stored = dict(item)
      stored["local_path"] = str(dest)
      stored.pop("url", None)
      downloaded.append(stored)

  return downloaded


async def download_media_item(
  bot,
  session: aiohttp.ClientSession,
  item: MediaItem | dict,
  dest_dir: Path,
  *,
  max_client=None,
) -> Path | None:
  local_path = _item_field(item, "local_path")
  if local_path:
    path = Path(local_path)
    if path.is_file():
      return path
    logger.warning("media_local_path_missing", path=local_path)

  file_id = _item_field(item, "file_id")
  file_name = resolve_file_name(item)

  if file_id:
    tg_file = await bot.get_file(file_id)
    if tg_file.file_path is None:
      return None
    dest = dest_dir / file_name
    await bot.download_file(tg_file.file_path, destination=dest)
    return dest

  url = _item_field(item, "url")
  if url is None and max_client is not None:
    url = await _resolve_max_url(max_client, item)

  if url:
    dest = dest_dir / file_name
    if await _download_url(session, url, dest):
      return dest
    return None

  return None


def cleanup_paths(paths: list[Path]) -> None:
  for path in paths:
    try:
      path.unlink(missing_ok=True)
    except OSError:
      logger.warning("media_temp_cleanup_failed", path=str(path))
