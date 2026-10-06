class AppError(Exception):
    """An error whose message is safe and useful to show to the user."""


def split_text(text: str, limit: int) -> list[str]:
    """Split text into pieces of at most `limit` chars, preferring sentence then word boundaries."""
    text = text.strip()
    pieces: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind(". ", 0, limit), text.rfind("? ", 0, limit), text.rfind("! ", 0, limit))
        if cut >= limit // 2:
            cut += 1  # keep the punctuation with its sentence
        else:
            cut = text.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces
