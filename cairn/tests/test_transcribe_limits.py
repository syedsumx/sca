"""Transcription's length limit holds for a recording whose header lies about its length.

Review finding: the limit (CAIRN_TRANSCRIBE_MAX_SECONDS) was checked against the length the container's header
states, and the child process then decoded every sample there was into memory. A WebM whose Duration says 2 s and
which holds ten minutes (or, within the 50 MB file limit, many hours) of audio passed the check, and the decoder
held all of it. The child now stops decoding at the limit and reports the file instead of transcribing it."""

from __future__ import annotations

import struct
import subprocess

import pytest

av = pytest.importorskip("av")
pytest.importorskip("numpy")


def _child_runs() -> None:
    """The child runs as ``python -I -m cairn.pipelines.transcribe``: Cairn must be installed, as in the image."""
    import sys

    if subprocess.run([sys.executable, "-I", "-c", "import cairn"], capture_output=True).returncode:
        pytest.skip("Cairn is not installed in this Python (the transcription child runs with -I)")


def _lying_webm(path, seconds: int, claimed: float) -> None:
    """A WebM of ``seconds`` of tone whose header says it lasts ``claimed`` seconds."""
    import numpy as np

    with av.open(str(path), "w", format="webm") as out:
        stream = out.add_stream("libopus", rate=48000)
        stream.bit_rate = 8000
        tone = (np.sin(np.arange(48000) * 2 * np.pi * 440 / 48000) * 8000).astype(np.int16).reshape(1, -1)
        for i in range(seconds):
            frame = av.AudioFrame.from_ndarray(tone, format="s16", layout="mono")
            frame.sample_rate, frame.pts = 48000, i * 48000
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    data = bytearray(path.read_bytes())
    i = data.find(bytes([0x44, 0x89]))                      # the segment's Duration element (milliseconds)
    size = data[i + 2] & 0x7F
    data[i + 3:i + 3 + size] = struct.pack(">d" if size == 8 else ">f", claimed * 1000)
    path.write_bytes(bytes(data))


def test_a_recording_longer_than_its_header_says_is_not_decoded_past_the_limit(tmp_path, monkeypatch):
    from cairn.pipelines import transcribe

    media = tmp_path / "call.webm"
    _lying_webm(media, 120, 2.0)
    assert transcribe.duration(str(media)) == pytest.approx(2.0)          # the header's claim passes the check
    with pytest.raises(transcribe.TranscribeError, match="longer than"):
        transcribe._samples(str(media), max_seconds=30)
    assert len(transcribe._samples(str(media), max_seconds=600)) == pytest.approx(120 * 16000, rel=0.01)

    # Through the child process, as files pipes run it: the file's row gets the reason, no text.
    _child_runs()
    monkeypatch.setenv("CAIRN_TRANSCRIBE", "on")
    monkeypatch.setenv("CAIRN_TRANSCRIBE_MAX_SECONDS", "30")
    monkeypatch.setattr(transcribe, "installed", lambda: True)
    text, seconds, error = transcribe.transcribe("call.webm", media.read_bytes())
    assert text is None and seconds == pytest.approx(2.0)
    assert error and "longer than" in error, error


def test_the_child_process_reports_the_limit_before_loading_a_model(tmp_path):
    """The length is checked before the model is loaded: a file over the limit costs no model load."""
    import sys

    from cairn.pipelines import transcribe

    _child_runs()
    media = tmp_path / "call.webm"
    _lying_webm(media, 60, 2.0)
    r = subprocess.run([sys.executable, "-I", "-m", "cairn.pipelines.transcribe", str(media), "no-such-model", "", "",
                        "1", "10"], capture_output=True, env={"PYTHONPATH": transcribe._package_root()}, timeout=120)
    assert r.returncode != 0 and b"longer than" in r.stderr, r.stderr[-500:]
