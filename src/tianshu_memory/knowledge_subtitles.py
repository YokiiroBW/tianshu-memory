"""Read supplied subtitle originals and embedded text cues without generating speech."""

import html
import re
import time
from urllib.parse import urlsplit

import av

from .domain import require
from .knowledge_sources import decode

MEDIA_TYPES = {"text/vtt", "application/x-subrip", "application/srt", "text/srt"}
_TIME = r"(?:(\d{1,3}):)?(\d{2}):(\d{2})[,.](\d{3})"
_TIMING = re.compile(r"^" + _TIME + r"\s+-->\s+" + _TIME + r"(?:\s+.*)?$")


def format_for(media, filename=None):
    if media in MEDIA_TYPES:
        return media
    if media == "text/plain" and filename:
        path = urlsplit(filename).path.lower()
        if path.endswith(".vtt"):
            return "text/vtt"
        if path.endswith(".srt"):
            return "application/x-subrip"
    return None


def _seconds(parts):
    hours, minutes, seconds, millis = parts
    require(int(minutes) < 60 and int(seconds) < 60, "unsupported", 415)
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def _plain(text):
    # Preserve dialogue, remove formatting; never treat subtitle markup as an instruction.
    return html.unescape(re.sub(r"<[^>]*>|\{[^}]*\}", "", text)).replace(r"\N", "\n").strip()


def parse(raw, media):
    text = decode(raw, "text/plain").replace("\r\n", "\n").replace("\r", "\n")
    if media == "text/vtt":
        require(
            text.startswith("WEBVTT") and text[6:7] in {"", "\n", " ", "\t"}, "unsupported", 415
        )
        text = text.partition("\n\n")[2]
    cues = []
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        lines = block.splitlines()
        if not lines or lines[0].startswith(("NOTE", "STYLE", "REGION")):
            continue
        index = next((i for i in range(min(2, len(lines))) if "-->" in lines[i]), None)
        require(index is not None, "unsupported", 415)
        timing = _TIMING.fullmatch(lines[index].strip())
        require(timing is not None, "unsupported", 415)
        start, end = _seconds(timing.groups()[:4]), _seconds(timing.groups()[4:])
        require(0 <= start < end <= 86400, "unsupported", 415)
        value = _plain("\n".join(lines[index + 1 :]))
        if value:
            cues.append((start, end, value))
        require(len(cues) <= 100000, "source_too_large", 413)
    require(bool(cues), "empty_source", 400)
    return sorted(cues)


def embedded(container, start, end):
    """Packet timestamps and durations belong to the original subtitle stream."""
    if not container.streams.subtitles:
        return [], False
    stream = container.streams.subtitles[0]
    cues, deadline, packets = [], time.monotonic() + 5, 0
    # Scan from the start so a cue beginning before the requested point is retained.
    for packet in container.demux(stream):
        packets += 1
        require(time.monotonic() < deadline and packets <= 100000, "source_timeout", 408)
        if packet.pts is None or packet.time_base is None or not packet.duration:
            continue
        at, until = (
            float(packet.pts * packet.time_base),
            float((packet.pts + packet.duration) * packet.time_base),
        )
        if at >= end:
            break
        if until <= start:
            continue
        try:
            rectangles = packet.decode()
        except av.error.FFmpegError:
            # Bitmap/unsupported subtitle codecs do not license invented captions.
            return cues, False
        for rectangle in rectangles:
            if not hasattr(rectangle, "dialogue") and not hasattr(rectangle, "text"):
                return cues, False
            value = getattr(rectangle, "dialogue", None) or getattr(rectangle, "text", None)
            if value:
                value = (
                    value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
                )
                value = _plain(value)
                if value:
                    cues.append((max(0, at), until, value))
    return sorted(cues), True


def excerpt(cues, start, end, budget):
    """Whole cues only; a too-large first cue never advances past unread text."""
    parts, used, actual_end = [], 0, end
    for at, until, value in cues:
        if until <= start or at >= end:
            continue
        line = f"[{at:.3f}–{until:.3f}s] {value}"
        size = len(line.encode("utf-8")) + bool(parts)
        if used + size > budget:
            actual_end = max(start, min(end, at))
            break
        parts.append(line)
        used += size
    return "\n".join(parts), actual_end
