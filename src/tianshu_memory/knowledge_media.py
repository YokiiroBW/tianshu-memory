"""Bounded decoding of Knowledge originals; no generated descriptions or fake transcripts."""

import base64
import io
import time
import wave
import zipfile

import av
from docx import Document
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .domain import Fault, require
from .knowledge_evidence import blocks
from .knowledge_sources import content_hash, decode

MAX_BYTES = 33_554_432
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MEDIA_TYPES = {
    "text/plain",
    "text/markdown",
    "text/html",
    "application/pdf",
    DOCX,
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "audio/wav",
    "audio/x-wav",
    "audio/mpeg",
    "audio/ogg",
    "audio/mp4",
}


def _container(raw):
    return av.open(io.BytesIO(raw))


def _duration(container, stream):
    if stream.duration is not None and stream.time_base is not None:
        return float(stream.duration * stream.time_base)
    return float(container.duration / av.time_base) if container.duration is not None else None


def _image(raw):
    image = Image.open(io.BytesIO(raw))
    require(image.width * image.height <= 20_000_000, "source_too_large", 413)
    image.load()
    return image


def _docx(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(
            sum(info.file_size for info in archive.infolist()) <= MAX_BYTES, "source_too_large", 413
        )
    document = Document(io.BytesIO(raw))
    return "\n".join(
        [p.text for p in document.paragraphs]
        + [
            "\t".join(cell.text for cell in row.cells)
            for table in document.tables
            for row in table.rows
        ]
    )


def prepare(raw, media, resolved=None):
    require(0 < len(raw) <= MAX_BYTES, "source_too_large", 413)
    require(media in MEDIA_TYPES, "unsupported", 415)
    text, total, unit = "", len(raw), "bytes"
    try:
        if media.startswith("text/"):
            text = decode(raw, media)
            kind, total, unit = (
                "article" if media == "text/html" else "text",
                len(text),
                "characters",
            )
        elif media == DOCX:
            text = _docx(raw)
            kind, total, unit = "text", len(text), "characters"
        elif media == "application/pdf":
            reader = PdfReader(io.BytesIO(raw))
            require(not reader.is_encrypted, "unsupported", 415)
            kind, total, unit = "text", len(reader.pages), "pages"
        elif media.startswith("image/"):
            with _image(raw):
                pass
            kind = "image"
        else:
            kind, unit = ("video" if media.startswith("video/") else "audio"), "seconds"
            with _container(raw) as container:
                streams = container.streams.video if kind == "video" else container.streams.audio
                require(bool(streams), "unsupported", 415)
                if kind == "video":
                    require(
                        streams[0].width * streams[0].height <= 20_000_000, "source_too_large", 413
                    )
                total = _duration(container, streams[0])
                require(total is None or 0 <= total <= 86400, "source_too_large", 413)
        require(len(text.encode("utf-8")) <= 4_194_304, "source_too_large", 413)
    except (
        av.error.FFmpegError,
        UnidentifiedImageError,
        zipfile.BadZipFile,
        ValueError,
        EOFError,
        PdfReadError,
        Image.DecompressionBombError,
    ) as error:
        raise Fault("unsupported", 415) from error
    units = blocks(text, None) if text else []
    return {
        "raw": raw,
        "digest": content_hash(raw),
        "text": text,
        "media": media,
        "resolved": resolved,
        "units": units,
        "index": [u["text"] for u in units],
        "provenance": {"content_kind": kind, "coverage_unit": unit, "coverage_total": total},
    }


def representation(raw, media, kind, source_hash, at=None):
    return {
        "kind": kind,
        "media_type": media,
        "sha256": content_hash(raw),
        "source_sha256": source_hash,
        "data_base64": base64.b64encode(raw).decode("ascii"),
        "at_seconds": at,
    }


def _jpeg(image, budget):
    image = image.convert("RGB")
    image.thumbnail((1280, 1280))
    while True:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        raw = buffer.getvalue()
        if len(raw) <= budget:
            return raw
        require(min(image.size) > 32, "source_too_large", 413)
        image.thumbnail((max(32, image.width // 2), max(32, image.height // 2)))


def read(version, requested, budget):
    raw, media, source_hash = version["raw"], version["media_type"], version["hash"]
    import json

    provenance = json.loads(version["provenance"])
    unit, total = provenance["coverage_unit"], provenance["coverage_total"]
    start, end = requested["start"], requested["end"]
    require(requested["unit"] == unit and 0 <= start < end, "invalid_input", 400)
    require(total is None or start < total, "invalid_input", 416)
    end = min(end, total) if total is not None else end
    result = {
        "text": None,
        "media_url": None,
        "representations": [],
        "gaps": [],
        "coverage": {"unit": unit, "start": start, "end": end},
        "complete": True,
    }
    budget = min(budget, 1_500_000)
    if unit == "characters":
        require(type(start) is int and type(end) is int, "invalid_input", 400)
        text = version["text"][start:end]
        served = text.encode("utf-8")[: min(budget, 100000)].decode("utf-8", errors="ignore")
        result.update(
            text=served, coverage={"unit": unit, "start": start, "end": start + len(served)}
        )
        if served != text:
            result.update(complete=False, gaps=["range_truncated"])
    elif unit == "pages":
        require(type(start) is int and type(end) is int, "invalid_input", 400)
        reader = PdfReader(io.BytesIO(raw))
        texts, used, actual_end = [], 0, start
        for index in range(start, min(end, start + 16)):
            text = reader.pages[index].extract_text() or ""
            size = len(text.encode("utf-8")) + (3 if texts else 0)
            if used + size > min(budget, 100000):
                break
            texts.append(text)
            used += size
            actual_end = index + 1
        gaps = ["page_images_not_extracted"]
        if not any(texts):
            gaps.append("no_text_layer")
        if actual_end < end:
            gaps.append("range_truncated")
        result.update(
            text="\n\f\n".join(texts),
            gaps=gaps,
            complete=False,
            coverage={"unit": unit, "start": start, "end": actual_end},
        )
    elif unit == "bytes":
        # A partial encoded image is not a decodable visual representation.
        require(start == 0 and end == len(raw), "invalid_input", 416)
        if len(raw) <= budget:
            result["representations"] = [representation(raw, media, "image", source_hash)]
        else:
            with _image(raw) as image:
                encoded = _jpeg(image, budget)
            result.update(
                representations=[representation(encoded, "image/jpeg", "image", source_hash)],
                complete=False,
                gaps=["image_resized"],
            )
    else:
        require(end - start <= 30, "invalid_input", 400)
        result = (
            _video(raw, source_hash, start, end, budget)
            if provenance["content_kind"] == "video"
            else _audio(raw, source_hash, start, end, budget)
        )
    return result


def _video(raw, source_hash, start, end, budget):
    frames, deadline, decoded = [], time.monotonic() + 5, 0
    limit = min(8, max(1, budget // 1024))
    with _container(raw) as container:
        stream = container.streams.video[0]
        require(stream.time_base is not None, "unsupported", 415)
        container.seek(int(start / float(stream.time_base)), stream=stream, backward=True)
        next_at = start
        for frame in container.decode(stream):
            decoded += 1
            require(time.monotonic() < deadline and decoded <= 4096, "source_timeout", 408)
            if frame.pts is None:
                continue
            at = float(frame.pts * frame.time_base)
            if at >= end:
                break
            if at < next_at:
                continue
            encoded = _jpeg(frame.to_image(), budget // limit)
            frames.append(representation(encoded, "image/jpeg", "video_frame", source_hash, at))
            next_at = at + max((end - start) / limit, 0.1)
            if len(frames) == limit:
                break
    require(bool(frames), "empty_source", 400)
    return {
        "text": None,
        "media_url": None,
        "representations": frames,
        "gaps": ["frames_sampled", "audio_not_transcribed"],
        "coverage": {
            "unit": "seconds",
            "start": frames[0]["at_seconds"],
            "end": frames[-1]["at_seconds"],
        },
        "complete": False,
    }


def _audio(raw, source_hash, start, end, budget):
    samples, sample_count, deadline, decoded = [], 0, time.monotonic() + 5, 0
    rate = 16000
    actual_start = actual_end = None
    with _container(raw) as container:
        stream = container.streams.audio[0]
        require(stream.time_base is not None, "unsupported", 415)
        container.seek(int(start / float(stream.time_base)), stream=stream, backward=True)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in container.decode(stream):
            decoded += 1
            require(time.monotonic() < deadline and decoded <= 4096, "source_timeout", 408)
            beyond_end = False
            for converted in resampler.resample(frame):
                if converted.pts is None:
                    continue
                at = float(converted.pts * converted.time_base)
                if at >= end:
                    beyond_end = True
                    break
                left = max(0, int((start - at) * rate))
                right = min(
                    converted.samples,
                    int((end - at) * rate),
                    max(0, (budget - 44) // 2 - sample_count) + left,
                )
                if right <= left:
                    continue
                samples.append(bytes(converted.planes[0])[left * 2 : right * 2])
                sample_count += right - left
                if actual_start is None:
                    actual_start = at + left / rate
                actual_end = at + right / rate
            if (
                beyond_end
                or actual_end is not None
                and (actual_end >= end - 1 / rate or sample_count * 2 + 44 >= budget)
            ):
                break
    require(bool(samples), "empty_source", 400)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"".join(samples))
    gaps = ["audio_not_transcribed"]
    if actual_start > start + 1 / rate or actual_end < end - 1 / rate:
        gaps.append("range_truncated")
    return {
        "text": None,
        "media_url": None,
        "representations": [
            representation(output.getvalue(), "audio/wav", "audio_clip", source_hash, actual_start)
        ],
        "gaps": gaps,
        "coverage": {"unit": "seconds", "start": actual_start, "end": actual_end},
        "complete": False,
    }
