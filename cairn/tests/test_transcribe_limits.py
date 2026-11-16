"""Speech to text: the length limit holds while decoding, not only by what the file's header claims."""

from __future__ import annotations

import wave

import pytest

from cairn.pipelines import transcribe


def _wav(path, seconds: int, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * rate * seconds)


def test_decoding_stops_past_the_limit(tmp_path):
    pytest.importorskip("av")
    pytest.importorskip("numpy")
    path = tmp_path / "long.wav"
    _wav(path, 30)
    with pytest.raises(transcribe.TranscribeError, match="over the limit"):
        transcribe._samples(str(path), cap=10)                # a header that understated 30 s would be stopped here
    assert len(transcribe._samples(str(path), cap=60)) == 30 * 16000
