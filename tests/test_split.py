from __future__ import annotations

from takopi_linear.bridge import _split_text


def test_split_text_respects_max_chars() -> None:
    text = "\n\n".join([f"para {i} " + ("x" * 40) for i in range(10)])
    chunks = _split_text(text, max_chars=120)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 120 for chunk in chunks)

