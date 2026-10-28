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

Suggestions have no op of their own for their outcome (seen on a test doc, Oct 2026):
- Accepting a suggested insertion is one entry that deletes its text (`ds`) and inserts
  the same text again as plain text (`is`), with style ops (`as`) around it.
- Rejecting it is `dss`, and rejecting a suggested deletion is `usfd`. The suggestion's
  own author withdrawing it looks the same.
- A replacement is one suggestion ID with both parts: `msfd` on the old text and `iss`
  for the new.
- Someone can suggest deleting another person's suggested insertion, so a character can
  belong to one suggested insertion and be marked by another suggestion at once.
An unknown op on a suggestion still in the doc is counted as a warning, in case Google
adds an op we don't handle: one whose range covers suggested or marked text, or that
names such a suggestion's ID.

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
# A suggestion Google gave no ID. It's still a suggestion, but can't be followed.
NO_ID = ""


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
    """The doc's characters at one point in its history. Each character records the
    suggested insertion it belongs to and the suggested deletion that marks it, if any."""

    def __init__(self):
        self.chars = [END_OF_BODY]
        # A suggestion's ID, NO_ID for one without, or None for plain text.
        self.ins: list[str | None] = [None]
        self.marks: list[str | None] = [None]
        self.warnings: list[str] = []

    def clamp(self, index, low: int, high: int, what: str) -> int:
        if not isinstance(index, int) or not low <= index <= high:
            self.warnings.append(f"{what} index {index} outside {low}..{high}")
            return max(low, min(high, index)) if isinstance(index, int) else low
        return index

    def insert(self, index, text: str, sid: str | None = None) -> None:
        """Insert plain text, or a suggested insertion when `sid` isn't None."""
        at = self.clamp(index, 1, len(self.chars) + 1, "insert") - 1
        added = units(text)
        self.chars[at:at] = added
        self.ins[at:at] = [sid] * len(added)
        self.marks[at:at] = [None] * len(added)

    def span(self, start, end, what: str) -> tuple[int, int]:
        start = self.clamp(start, 1, len(self.chars), f"{what} start")
        end = self.clamp(end, start, len(self.chars), f"{what} end")
        return start - 1, end

    def delete(self, start, end) -> dict:
        """Delete a range. Returns its text, where it started, and the text each suggestion
        lost: the part it inserted, and the part it marked for deletion."""
        a, b = self.span(start, end, "delete")
        removed = {"start": a + 1, "text": joined(self.chars[a:b]), "ins": {}, "marks": {}}
        for part, ids in (("ins", self.ins), ("marks", self.marks)):
            for sid in {i for i in ids[a:b] if i}:
                chars = [c for c, i in zip(self.chars[a:b], ids[a:b], strict=True) if i == sid]
                removed[part][sid] = joined(chars)
        del self.chars[a:b], self.ins[a:b], self.marks[a:b]
        if END_OF_BODY in removed["text"]:
            self.warnings.append("deleted the end-of-body marker (the index model may be off)")
        return removed

    def mark(self, start, end, sid: str | None) -> tuple[str, set[str]]:
        """Mark a range for deletion by suggestion `sid`, or unmark it when `sid` is None.
        Returns the range's text and the suggestions whose marks it replaced."""
        a, b = self.span(start, end, "mark")
        before = {i for i in self.marks[a:b] if i}
        self.marks[a:b] = [sid] * (b - a)
        return joined(self.chars[a:b]), before

    def apply(self, event: dict) -> None:
        """Apply one edit event, filling in the text it deleted or marked. `event["removed"]`
        and `event["unmarked"]` say which suggestions it touched (see `delete` and `mark`)."""
        kind, sid = event.get("kind"), event.get("suggestion_id")
        if kind == "insert":
            self.insert(event["index"], event["text"])
        elif kind == "suggest_insert":
            self.insert(event["index"], event["text"], sid or NO_ID)
        elif kind in ("delete", "remove_suggested"):
            event["removed"] = self.delete(event["start"], event["end"])
            event["deleted_text"] = event["removed"]["text"]
        elif kind == "suggest_delete_mark":
            event["marked_text"], replaced = self.mark(event["start"], event["end"], sid or NO_ID)
            if replaced - {sid}:
                # Another suggested deletion's mark is gone without an outcome. We don't
                # know yet whether Google allows this, so it's counted.
                self.warnings.append("a suggested deletion replaced another one's mark")
        elif kind == "suggest_delete_unmark":
            event["marked_text"], event["unmarked"] = self.mark(event["start"], event["end"], None)
        elif kind == "other" and self.touches_suggestion(event.get("raw") or {}):
            # Maybe an op for a suggestion's outcome that the replay doesn't know.
            self.warnings.append(f"unhandled op {event.get('op_type')!r} on a suggestion")

    def touches_suggestion(self, op: dict) -> bool:
        """Whether an op names a suggestion still in the doc, or its range covers one."""
        if op.get("sugid") and op["sugid"] in self.live():
            return True
        start, end = op.get("si"), op.get("ei")
        if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end):
            return False
        pairs = zip(self.ins[start - 1 : end], self.marks[start - 1 : end], strict=True)
        return any(i is not None or m is not None for i, m in pairs)

    def ids_in(self, start, end) -> set[str]:
        """The suggestions with text in a range: inserted there, or marking it."""
        if not (isinstance(start, int) and isinstance(end, int)):
            return set()
        a, b = max(start, 1) - 1, max(end, 0)
        return {i for i in self.ins[a:b] + self.marks[a:b] if i}

    def live(self) -> set[str]:
        """The IDs of the suggestions that still have text in the doc."""
        return {i for i in self.ins + self.marks if i}

    def suggestion_text(self, sid: str, part: str = "insert") -> str:
        """The text that belongs to one suggestion now: what it inserts ("insert") or what it
        marks for deletion ("delete")."""
        end = self.body_end()
        ids = (self.ins if part == "insert" else self.marks)[:end]
        return joined([c for c, i in zip(self.chars[:end], ids, strict=True) if i == sid])

    def body_end(self) -> int:
        return self.chars.index(END_OF_BODY) if END_OF_BODY in self.chars else len(self.chars)

    def text(self) -> str:
        """The body, with pending suggested insertions shown."""
        return joined(self.chars[: self.body_end()])

    def base_text(self) -> str:
        """The body with every pending suggestion rejected. Text that one suggestion inserts
        and another marks for deletion is still a suggested insertion, so it's left out."""
        end = self.body_end()
        pairs = zip(self.chars[:end], self.ins[:end], strict=True)
        return joined([char for char, sid in pairs if sid is None])
