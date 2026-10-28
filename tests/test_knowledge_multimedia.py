"""Real subtitle/codec originals through the existing authorized content endpoint."""

import base64
import io
import wave
from contextlib import contextmanager
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import av
import pytest
from PIL import Image
from test_knowledge_content import PATH, TOKEN, acquire_upload, post, principal, upload
from test_knowledge_content import content as content

from tianshu_memory import knowledge_media
from tianshu_memory.knowledge_sources import content_hash


def timed_audio(seconds=2, rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x10\x01" * round(seconds * rate))
    return output.getvalue()


def audiovisual(*, audio=True, subtitles=True, sound_gap=False, caption_records=None):
    output = io.BytesIO()
    from av.subtitles.subtitle import SubtitleSet

    with av.open(output, "w", format="mp4") as container:
        video = container.add_stream("mpeg4", rate=4)
        video.width, video.height, video.pix_fmt = 64, 48, "yuv420p"
        if audio:
            sound = container.add_stream("aac", rate=16000)
            sound.layout = "mono"
        if subtitles:
            captions = container.add_stream("mov_text")
            captions.time_base = Fraction(1, 1000)
            captions.codec_context.time_base = Fraction(1, 1000)
            with av.open(
                io.BytesIO(b"1\n00:00:00,500 --> 00:00:01,500\nCaption.\n"), format="srt"
            ) as source:
                list(source.decode(subtitles=0))
                captions.codec_context.subtitle_header = source.streams.subtitles[
                    0
                ].codec_context.subtitle_header
        for index in range(12):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (index * 12, 50, 20)))
            frame.pts, frame.time_base = index, Fraction(1, 4)
            for packet in video.encode(frame):
                container.mux(packet)
            if audio:
                frame = av.AudioFrame(format="s16", layout="mono", samples=4000)
                frame.planes[0].update(b"\x10\x01" * 4000)
                frame.sample_rate, frame.pts, frame.time_base = (
                    16000,
                    index * 4000 + (4000 if sound_gap and index >= 6 else 0),
                    Fraction(1, 16000),
                )
                for packet in sound.encode(frame):
                    container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        if audio:
            for packet in sound.encode():
                container.mux(packet)
        if subtitles:
            for start, end, text in caption_records or [(500, 1500, b"Actual embedded caption.")]:
                subset = SubtitleSet.create(
                    b"0,0,Default,,0,0,0,," + text, 0, end - start, pts=start
                )
                packet = captions.codec_context.encode_subtitle(subset)
                packet.stream, packet.time_base = captions, Fraction(1, 1000)
                container.mux(packet)
    return output.getvalue()


def read(h, ref, start, end, *, budget=1048576, p=None):
    return post(
        h,
        "read",
        {
            "principal": p or principal(h),
            "content_ref": ref,
            "range": {"unit": ref["coverage"]["unit"], "start": start, "end": end},
            "budget_bytes": budget,
        },
    )


@contextmanager
def subtitle_server(h):
    state = {"raw": b"WEBVTT\n\n00:00.000 --> 00:35.000\nFirst version.\n"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt")
            self.send_header("Content-Length", str(len(state["raw"])))
            self.end_headers()
            self.wfile.write(state["raw"])

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as listener:
        url = f"http://127.0.0.1:{listener.server_port}/original.vtt"
        h.config["knowledge_content"] = {"trusted_urls": [url]}
        h.save_config()
        thread = Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield url, state
        finally:
            listener.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize(
    "mime,raw",
    [
        (
            "application/x-subrip",
            b"1\n00:00:00,000 --> 00:00:03,000\nFirst supplied cue.\n\n2\n00:00:35,000 --> 00:00:38,000\nSecond supplied cue.\n",
        ),
        (
            "text/vtt",
            b"WEBVTT\n\nintro\n00:00.000 --> 00:03.000\nFirst supplied cue.\n\n00:35.000 --> 00:38.000 align:start\nSecond supplied cue.\n",
        ),
    ],
)
def test_subtitle_original_timeline_pages_and_exact_bytes(content, mime, raw):
    h = content
    ref = acquire_upload(h, raw, mime)["content_ref"]
    assert ref["kind"] == "text" and ref["coverage"] == {
        "unit": "seconds",
        "start": 0,
        "end": 38,
        "total": 38,
    }
    first = read(h, ref, 0, 38)
    assert first["coverage"]["end"] == 30 and "range_truncated" in first["gaps"]
    assert "First supplied cue." in first["text"] and "Second supplied cue." not in first["text"]
    second = read(h, ref, first["coverage"]["end"], 38)
    assert "Second supplied cue." in second["text"] and "First supplied cue." not in second["text"]
    assert not second["complete"] and not second["representations"]
    assert "没有提供声音或视频画面" in second["text"] and "audio_not_transcribed" in second["gaps"]
    original = h.client.post(
        PATH + "original",
        json={"principal": principal(h), "content_ref": ref, "range": None},
        headers=TOKEN,
    )
    assert original.content == raw and original.headers["x-source-sha256"] == content_hash(raw)


def test_named_plain_subtitle_and_plain_transcript_keep_distinct_coverage(content):
    h = content
    raw = b"WEBVTT\n\n00:00.000 --> 00:02.000\nActual named caption.\n"
    descriptor = post(
        h,
        "uploads",
        {
            "principal": principal(h),
            "filename": "provided.vtt",
            "media_type": "text/plain",
            "size": len(raw),
            "sha256": content_hash(raw),
        },
    )
    answer = h.client.put(
        PATH + "uploads/" + descriptor["upload_id"],
        content=raw,
        headers=TOKEN
        | {"Content-Type": "application/octet-stream", "X-Tianshu-Assertion-Ref": "origin-private"},
    )
    assert answer.status_code == 200
    ref = post(
        h,
        "acquire",
        {
            "principal": principal(h),
            "source": {"kind": "upload", "upload_id": descriptor["upload_id"]},
            "purpose": "read",
        },
    )["content_ref"]
    assert (
        ref["coverage"]["unit"] == "seconds"
        and "Actual named caption." in read(h, ref, 0, 2)["text"]
    )
    transcript = acquire_upload(h, b"Supplied transcript original.", "text/plain")["content_ref"]
    assert transcript["coverage"]["unit"] == "characters"
    assert (
        read(h, transcript, 0, transcript["coverage"]["total"])["text"]
        == "Supplied transcript original."
    )


def test_subtitle_url_revision_blocks_old_continuation_and_mid_decode_update(content, monkeypatch):
    h = content
    with subtitle_server(h) as (url, state):

        def acquire():
            return post(
                h,
                "acquire",
                {
                    "principal": principal(h),
                    "source": {"kind": "url", "url": url},
                    "purpose": "read",
                },
            )

        first = acquire()["content_ref"]
        assert read(h, first, 0, 35)["coverage"]["end"] == 30
        decoded = knowledge_media.read

        def update_after_decode(*args):
            result = decoded(*args)
            state["raw"] = b"WEBVTT\n\n00:00.000 --> 00:36.000\nUpdated original.\n"
            new = acquire()["content_ref"]
            assert new["object_id"] == first["object_id"] and new["version"] == 2
            return result

        monkeypatch.setattr(knowledge_media, "read", update_after_decode)
        answer = h.client.post(
            PATH + "read",
            json={
                "principal": principal(h),
                "content_ref": first,
                "range": {"unit": "seconds", "start": 30, "end": 35},
                "budget_bytes": 4096,
            },
            headers=TOKEN,
        )
        assert answer.status_code == 409 and "First version." not in answer.text


def test_malformed_subtitle_and_cue_budget_do_not_claim_progress(content):
    h = content
    descriptor = upload(h, b"WEBVTT\n\n00:99.000 --> 00:02.000\nBad clock\n", "text/vtt")
    answer = h.client.post(
        PATH + "acquire",
        json={
            "principal": principal(h),
            "source": {"kind": "upload", "upload_id": descriptor["upload_id"]},
            "purpose": "read",
        },
        headers=TOKEN,
    )
    assert answer.status_code == 415
    ref = acquire_upload(
        h, b"1\n00:00:00,000 --> 00:00:03,000\n" + b"x" * 3000, "application/x-subrip"
    )["content_ref"]
    answer = h.client.post(
        PATH + "read",
        json={
            "principal": principal(h),
            "content_ref": ref,
            "range": {"unit": "seconds", "start": 0, "end": 3},
            "budget_bytes": 1024,
        },
        headers=TOKEN,
    )
    assert answer.status_code == 413 and "data_base64" not in answer.text


def test_video_supplies_real_audio_and_embedded_text_with_one_budget(content):
    h, raw = content, audiovisual()
    ref = acquire_upload(h, raw, "video/mp4")["content_ref"]
    result = read(h, ref, 0, 2.5)
    kinds = [item["kind"] for item in result["representations"]]
    assert "audio_clip" in kinds and "video_frame" in kinds and len(kinds) <= 8
    assert "Actual embedded caption." in result["text"]
    assert "实际内嵌字幕" in result["text"] and "未进行语音转录" in result["text"]
    assert result["gaps"] == ["frames_sampled", "audio_not_transcribed"] and not result["complete"]
    clip = next(item for item in result["representations"] if item["kind"] == "audio_clip")
    with wave.open(io.BytesIO(base64.b64decode(clip["data_base64"]))) as wav:
        assert wav.getnframes() / wav.getframerate() == pytest.approx(2.5, abs=0.001)
    assert all(item["source_sha256"] == content_hash(raw) for item in result["representations"])
    small = read(h, ref, 0, 2.5, budget=1024)
    assert all(item["kind"] == "video_frame" for item in small["representations"])
    assert "存在音轨，但本次未提供声音" in small["text"]
    assert (
        len(small["text"].encode())
        + sum(len(base64.b64decode(item["data_base64"])) for item in small["representations"])
        <= 1024
    )
    assert len(small["representations"]) == 1 and small["coverage"]["end"] > 0
    next_frame = read(h, ref, small["coverage"]["end"], 2.5, budget=1024)
    assert next_frame["coverage"]["end"] > small["coverage"]["end"]
    caption_frame = read(h, ref, next_frame["coverage"]["end"], 2.5, budget=1024)
    assert "Actual embedded caption." in caption_frame["text"]


def test_silent_video_does_not_invent_audio_or_subtitles(content):
    h, raw = content, audiovisual(audio=False, subtitles=False)
    ref = acquire_upload(h, raw, "video/mp4")["content_ref"]
    result = read(h, ref, 0, 2)
    assert all(item["kind"] == "video_frame" for item in result["representations"])
    assert "原件没有音轨" in result["text"] and "没有可解码的文字字幕" in result["text"]
    assert not result["complete"]


def test_dense_embedded_captions_continue_before_unread_text(content):
    h = content
    raw = audiovisual(
        caption_records=[
            (index * 100, (index + 1) * 100, f"cue:{index}:".encode() + b"x" * 2000)
            for index in range(30)
        ]
    )
    ref = acquire_upload(h, raw, "video/mp4")["content_ref"]
    start, seen = 0, set()
    for _ in range(8):
        result = read(h, ref, start, 2.5)
        end = result["coverage"]["end"]
        assert end > start
        seen.update(index for index in range(25) if f"cue:{index}:" in result["text"])
        if end >= 2.5:
            break
        assert "range_truncated" in result["gaps"] and "字幕预算截断" in result["text"]
        start = end
    assert seen == set(range(25)) and end == pytest.approx(2.5)


def test_long_audio_pages_and_resampled_partial_continuation(content):
    h = content
    ref = acquire_upload(h, timed_audio(40, 44100), "audio/wav")["content_ref"]
    first = read(h, ref, 0, 40)
    assert first["coverage"]["end"] == pytest.approx(30, abs=1 / 16000)
    assert "range_truncated" in first["gaps"]
    continuation = read(h, ref, first["coverage"]["end"], 40)
    assert continuation["coverage"]["start"] == pytest.approx(30, abs=1 / 16000)
    assert continuation["coverage"]["end"] == pytest.approx(40, abs=1 / 16000)
    small = read(h, ref, 3, 10, budget=4096)
    assert 3 <= small["coverage"]["start"] < small["coverage"]["end"] < 10
    next_page = read(h, ref, small["coverage"]["end"], 10, budget=4096)
    assert next_page["coverage"]["start"] == pytest.approx(small["coverage"]["end"], abs=1 / 16000)
    assert "range_truncated" in small["gaps"]


def test_real_audio_timestamp_hole_is_not_concatenated_and_sound_tail_is_available(content):
    h = content
    ref = acquire_upload(h, audiovisual(subtitles=False, sound_gap=True), "video/mp4")[
        "content_ref"
    ]
    assert ref["coverage"]["total"] > 3
    first = read(h, ref, 0, 3)
    assert first["coverage"]["end"] < 2 and "range_truncated" in first["gaps"]
    resumed = read(h, ref, first["coverage"]["end"], 3)
    assert resumed["coverage"]["start"] > first["coverage"]["end"]
    tail = read(h, ref, 3, ref["coverage"]["total"])
    assert all(item["kind"] == "audio_clip" for item in tail["representations"])
    assert "本区间没有可提供的画面" in tail["text"]


def test_actor_subtitle_refresh_denies_revoked_role_and_no_user_progress(content, monkeypatch):
    h = content
    p = {
        "kind": "actor",
        "request_id": "subtitle:upload",
        "actor_id": "actor:solo",
        "operation_ref": "activity:subtitle",
    }
    raw = b"WEBVTT\n\n00:00.000 --> 00:02.000\nActor supplied cue.\n"
    descriptor = upload(h, raw, "text/vtt", p=p)
    acquired = post(
        h,
        "acquire",
        {
            "principal": {**p, "request_id": "subtitle:acquire"},
            "source": {"kind": "upload", "upload_id": descriptor["upload_id"]},
            "purpose": "read",
        },
    )
    ref = acquired["content_ref"]
    assert read(h, ref, 0, 2, p={**p, "request_id": "subtitle:read"})["text"]
    with h.store.transaction() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM records")]
    decode = knowledge_media.read

    def revoke_after_decode(*args):
        result = decode(*args)
        h.config["callers"]["companion"]["runtime_content"] = False
        h.save_config()
        return result

    monkeypatch.setattr(knowledge_media, "read", revoke_after_decode)
    answer = h.client.post(
        PATH + "read",
        json={
            "principal": {**p, "request_id": "subtitle:revoked"},
            "content_ref": ref,
            "range": {"unit": "seconds", "start": 0, "end": 2},
            "budget_bytes": 4096,
        },
        headers=TOKEN,
    )
    assert answer.status_code == 403 and "Actor supplied cue" not in answer.text
    with h.store.transaction() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM records")] == before
