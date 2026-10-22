"""Parse a Google Docs changelog and replay it into the document's text (COL-5).

Each changelog entry is a list (reverse-engineered; it matches harvard-vpal/gdocrevisions
and James Somers' write-up):
  [op, timestamp_ms, user_id, revision, session_id, session_seq, ...]

Ops handled. Indices are 1-based and inclusive:
  is    insert text                {ibi, s}
  ds    delete text                {si, ei}
  iss   insert suggested text      {ibi, s, sugid}
  dss   delete suggested text      {si, ei}   a suggestion rejected, withdrawn or edited away
  msfd  mark text for deletion     {si, ei}   a suggested deletion
  usfd  unmark text for deletion   {si, ei}
  mlti  several ops at once        {mts: [...]}   flattened
Style ops change no text, so the replay skips them:
  as    apply style
  sas   suggest a style        {si, ei, sugid}
  ras   reject a suggested style   {si, ei, sugid}   seen once, on a `sas` suggestion's ID
  astss style suggested text   {si, ei, st}   its author formatting it while suggesting
Anything else (entities and so on) is kept as it came, as kind "format" or "other".

Google's op for *accepting* a suggestion isn't known yet. Accepted text would stay flagged
as suggested, so an unknown op whose range covers suggested text is counted as a warning.

The text buffer starts with one end-of-body marker. The body is everything before it;
anything after it (footnotes and so on) is left out of `text()`.

Indices count UTF-16 code units, as Docs does, so an emoji or other character outside the
Basic Multilingual Plane takes two. The buffer holds one code unit per slot to match.
"""

from datetime import UTC, datetime

TEXT_KINDS = {
    "is": "insert",
    "ds": "delete",
    "iss": "suggest_insert",
    "dss": "remove_suggested",
    "msfd": "suggest_delete_mark",
    "usfd": "suggest_delete_unmark",
}
FORMAT_OPS = {"as", "sas", "ras", "astss"}
END_OF_BODY = "\x00"
# What each character is: plain text, a suggested insertion, or marked for suggested deletion.
PLAIN, SUGGESTED, MARKED = 0, 1, -1


def units(text: str) -> list[str]:
    """Text as UTF-16 code units, one per item (a character outside the BMP gives two)."""
    raw = text.encode("utf-16-le", "surrogatepass")
    return [raw[i : i + 2].decode("utf-16-le", "surrogatepass") for i in range(0, len(raw), 2)]


def joined(code_units: list[str]) -> str:
    """UTF-16 code units back into text. A pair split by an edit becomes U+FFFD."""
    raw = "".join(code_units).encode("utf-16-le", "surrogatepass")
    return raw.decode("utf-16-le", "replace")


def iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    when = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def flatten(op):
    if isinstance(op, list):
        for inner in op:
            yield from flatten(inner)
    elif isinstance(op, dict):
        if op.get("ty") == "mlti":
            yield from flatten(op.get("mts", []))
        else:
            yield op


def op_fields(op: dict) -> dict:
    op_type = op.get("ty")
    kind = TEXT_KINDS.get(op_type) or ("format" if op_type in FORMAT_OPS else "other")
    fields = {"kind": kind, "op_type": op_type}
    if op_type in ("is", "iss"):
        fields["index"] = op.get("ibi")
        fields["text"] = op.get("s", "")
    elif op_type in TEXT_KINDS:
        fields["start"] = op.get("si")
        fields["end"] = op.get("ei")
    else:
        fields["raw"] = op
    if "sugid" in op:
        fields["suggestion_id"] = op["sugid"]
    return fields


def edit_events(changelog: dict) -> list[dict]:
    """One event per op, in changelog order. `source` is "snapshot" or "changelog"."""
    events = []
    entries = [e for e in changelog.get("changelog") or [] if isinstance(e, list) and e]
    first_ts = entries[0][1] if entries and len(entries[0]) > 1 else None
    for op in flatten(changelog.get("chunkedSnapshot") or []):
        events.append({"source": "snapshot", "ts_ms": first_ts, "actor": None, "rev": 0})
        events[-1].update(op_fields(op))
    for entry in entries:
        ts_ms, user, rev, session, session_seq = (entry[1:6] + [None] * 5)[:5]
        for n, op in enumerate(flatten(entry[0])):
            event = {
                "source": "changelog",
                "ts_ms": ts_ms,
                # The changelog names editors by an internal Google ID only, never a name.
                "actor": f"gaia:{user}" if user is not None else None,
                "rev": rev,
                "session": session,
                "session_seq": session_seq,
                "op_in_rev": n,
            }
            event.update(op_fields(op))
            events.append(event)
    return events


class DocState:
    """The doc's characters at one point in its history, with a suggestion flag on each."""

    def __init__(self):
        self.chars = [END_OF_BODY]
        self.flags = [PLAIN]
        # The suggestion each character belongs to, if any: a suggested insertion's ID, or
        # the ID of the suggested deletion that marks it.
        self.ids: list[str | None] = [None]
        self.warnings: list[str] = []

    def clamp(self, index, low: int, high: int, what: str) -> int:
        if not isinstance(index, int) or not low <= index <= high:
            self.warnings.append(f"{what} index {index} outside {low}..{high}")
            return max(low, min(high, index)) if isinstance(index, int) else low
        return index

    def insert(self, index, text: str, flag: int = PLAIN, sid: str | None = None) -> None:
        at = self.clamp(index, 1, len(self.chars) + 1, "insert") - 1
        added = units(text)
        self.chars[at:at] = added
        self.flags[at:at] = [flag] * len(added)
        self.ids[at:at] = [sid] * len(added)

    def span(self, start, end, what: str) -> tuple[int, int]:
        start = self.clamp(start, 1, len(self.chars), f"{what} start")
        end = self.clamp(end, start, len(self.chars), f"{what} end")
        return start - 1, end

    def delete(self, start, end) -> str:
        a, b = self.span(start, end, "delete")
        removed = joined(self.chars[a:b])
        del self.chars[a:b], self.flags[a:b], self.ids[a:b]
        if END_OF_BODY in removed:
            self.warnings.append("deleted the end-of-body marker (the index model may be off)")
        return removed

    def mark(self, start, end, flag: int, sid: str | None = None) -> str:
        a, b = self.span(start, end, "mark")
        self.flags[a:b] = [flag] * (b - a)
        self.ids[a:b] = [sid] * (b - a)
        return joined(self.chars[a:b])

    def apply(self, event: dict) -> None:
        """Apply one edit event, filling in the text it deleted or marked."""
        kind, sid = event.get("kind"), event.get("suggestion_id")
        if kind == "insert":
            self.insert(event["index"], event["text"])
        elif kind == "suggest_insert":
            self.insert(event["index"], event["text"], SUGGESTED, sid)
        elif kind in ("delete", "remove_suggested"):
            event["deleted_text"] = self.delete(event["start"], event["end"])
        elif kind == "suggest_delete_mark":
            event["marked_text"] = self.mark(event["start"], event["end"], MARKED, sid)
        elif kind == "suggest_delete_unmark":
            event["marked_text"] = self.mark(event["start"], event["end"], PLAIN)
        elif kind == "other" and self.touches_suggestion(event.get("raw") or {}):
            # Maybe Google's op for accepting a suggestion, which the replay doesn't know.
            self.warnings.append(f"unhandled op {event.get('op_type')!r} on suggested text")

    def touches_suggestion(self, op: dict) -> bool:
        start, end = op.get("si"), op.get("ei")
        if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end):
            return False
        return SUGGESTED in self.flags[start - 1 : end]

    def suggestion_text(self, sid: str) -> str:
        """The text that belongs to one suggestion now: what it inserts or marks."""
        end = self.body_end()
        return joined(
            [c for c, i in zip(self.chars[:end], self.ids[:end], strict=True) if i == sid]
        )

    def body_end(self) -> int:
        return self.chars.index(END_OF_BODY) if END_OF_BODY in self.chars else len(self.chars)

    def text(self) -> str:
        """The body, with pending suggested insertions shown."""
        return joined(self.chars[: self.body_end()])

    def base_text(self) -> str:
        """The body with every pending suggestion rejected."""
        end = self.body_end()
        pairs = zip(self.chars[:end], self.flags[:end], strict=True)
        return joined([char for char, flag in pairs if flag != SUGGESTED])
