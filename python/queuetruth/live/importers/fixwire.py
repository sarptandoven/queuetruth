"""FIX tag=value wire format: message framing, BodyLength(9) / CheckSum(10) validation, field parsing and FIX
UTCTimestamp parsing (shared by the FIX log importer `queuetruth.live.importers.fix44` and the QuickFIX file
readers `queuetruth.live.importers.quickfix`).

Framing follows the FIX session layer: `8=<BeginString><SOH>9=<BodyLength><SOH>` then exactly BodyLength bytes,
then `10=<three digits><SOH>`. BodyLength counts the bytes after the BodyLength field's SOH up to and including
the SOH before `10=`; CheckSum is the sum of every byte before `10=` modulo 256. Messages are handled as latin-1
text so one character is one byte and checksums are computed on the original bytes. A log written with another
delimiter (`|`, or the two-character `^A` of `cat -v`) is normalised to SOH before framing; its checksum is
accepted in the SOH form (what the engine sent) or in the literal form.

Length-prefixed data fields (RawData 96, XmlData 213, EncodedText 355, ...) may contain SOH; they are split by
their declared length, as the FIX specification requires.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass

SOH = "\x01"

#: Length field -> data field (FIX 4.2-5.0SP2 `LENGTH` / `DATA` pairs).
LENGTH_DATA: dict[str, str] = {
    "90": "91",
    "93": "89",
    "95": "96",
    "212": "213",
    "348": "349",
    "350": "351",
    "352": "353",
    "354": "355",
    "356": "357",
    "358": "359",
    "360": "361",
    "362": "363",
    "364": "365",
    "445": "446",
    "618": "619",
    "621": "622",
}

#: Framing / validation problems (counted per input; see `frame`).
PROBLEMS = ("bad_checksum", "bad_body_length", "truncated", "malformed")

_TRAILER = re.compile(r"\x0110=(\d{3})(?:\x01|$)")
_UTC = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,12}))?$")


@dataclass
class Framed:
    """One framed message: `text` (SOH form, `8=` through the CheckSum field), `end` (index after it in the
    scanned string), `problem` (None or one of `PROBLEMS`; `truncated` / `malformed` messages have no fields)."""

    text: str
    end: int
    problem: str | None


def normalise(text: str, delim: str) -> str:
    """`text` with the log delimiter replaced by SOH."""
    return text if delim == SOH else text.replace(delim, SOH)


def detect_delimiter(line: str, option: str) -> str:
    """Delimiter of one log line: the option (`soh`, `|`, `^A` or any single character), or with `auto` SOH when
    present, else `^A`, else `|`."""
    opt = option.strip()
    if opt.lower() == "soh":
        return SOH
    if opt and opt.lower() != "auto":
        return opt
    if SOH in line:
        return SOH
    if "^A" in line:
        return "^A"
    return "|"


def _checksum(text: str) -> int:
    return sum(text.encode("latin-1", "replace")) % 256


def frame(s: str, start: int, *, pipe_delims: bool = False) -> Framed:
    """Frames the message starting at `s[start]` (`8=`), `s` in SOH form. BodyLength gives the end; when it is
    wrong the end is the first `<SOH>10=ddd<SOH>` after the header (problem `bad_body_length`); without a trailer
    the message is `truncated` (the caller may append a continuation line and retry). `pipe_delims` also accepts a
    checksum computed over `|` delimiters (logs whose writer replaced SOH before checksumming)."""
    a = s.find(SOH, start)
    if a < 0:
        return Framed(s[start:], len(s), "truncated")
    problem: str | None = None
    body_start = -1
    declared = -1
    if s.startswith("9=", a + 1):
        b = s.find(SOH, a + 1)
        if b < 0:
            return Framed(s[start:], len(s), "truncated")
        txt = s[a + 3 : b]
        if txt.isdecimal():
            declared = int(txt)
            body_start = b + 1
        else:
            problem = "bad_body_length"
            body_start = b + 1
    else:
        problem = "malformed"
        body_start = a + 1
    t = body_start + declared if declared >= 0 else -1
    end = -1
    cs_at = -1
    if t >= body_start and s.startswith("10=", t) and t + 6 <= len(s) and s[t + 3 : t + 6].isdecimal():
        if t + 6 == len(s) or s[t + 6] == SOH:
            cs_at = t
            end = min(t + 7, len(s))
    if end < 0:
        m = _TRAILER.search(s, max(body_start - 1, start))
        if m is None:
            if declared >= 0 and t + 7 > len(s):
                return Framed(s[start:], len(s), "truncated")
            return Framed(s[start:], len(s), problem or "truncated")
        cs_at = m.start() + 1
        end = m.end()
        if problem is None:
            problem = "bad_body_length"
    text = s[start:end]
    if problem == "malformed":
        return Framed(text, end, problem)
    want = int(s[cs_at + 3 : cs_at + 6])
    head = s[start:cs_at]
    got = _checksum(head)
    if got != want:
        alt = (got + 123 * head.count(SOH)) % 256 if pipe_delims else -1
        if alt != want and problem is None:
            problem = "bad_checksum"
    return Framed(text, end, problem)


def parse_fields(text: str) -> list[tuple[str, str]] | None:
    """`(tag, value)` pairs of a framed SOH message in wire order (repeating groups keep every occurrence);
    data fields are split by their length field. None when a field is not `<digits>=<value>`."""
    out: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    pending_len: int | None = None
    pending_tag: str | None = None
    while i < n:
        eq = text.find("=", i)
        if eq < 0:
            if text[i:].strip(SOH):
                return None
            break
        tag = text[i:eq]
        if not tag.isdecimal():
            return None
        if pending_tag is not None and tag == pending_tag and pending_len is not None:
            j = min(eq + 1 + pending_len, n)
            val = text[eq + 1 : j]
            if j < n and text[j] != SOH:
                k = text.find(SOH, j)
                j = n if k < 0 else k
                val = text[eq + 1 : j]
        else:
            k = text.find(SOH, eq + 1)
            j = n if k < 0 else k
            val = text[eq + 1 : j]
        out.append((tag, val))
        pending_tag, pending_len = None, None
        if tag in LENGTH_DATA and val.isdecimal():
            pending_tag, pending_len = LENGTH_DATA[tag], int(val)
        i = j + 1
    return out


def first_values(fields: list[tuple[str, str]]) -> dict[str, str]:
    """First value of every tag (header and body fields are unique; repeating-group members keep the first)."""
    f: dict[str, str] = {}
    for tag, val in fields:
        f.setdefault(tag, val)
    return f


def display(text: str) -> str:
    """Printable form of an SOH message (SOH -> `|`), used for the canonical `raw` columns."""
    return text.replace(SOH, "|")


def utc_ts(text: str) -> tuple[int, str, bool] | None:
    """FIX UTCTimestamp `YYYYMMDD-HH:MM:SS[.f{1,12}]` -> (UTC ns, precision, truncated): FIX 5.0SP2 allows
    picoseconds; digits beyond ns are truncated (`truncated` true). None when the text is not a UTCTimestamp."""
    m = _UTC.match(text.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(m.group(i)) for i in range(1, 7))
    if not (1 <= mo <= 12 and 1 <= d <= 31 and h <= 23 and mi <= 59 and s <= 60):
        return None
    frac = m.group(7) or ""
    secs = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
    prec = "s" if not frac else "ms" if len(frac) <= 3 else "us" if len(frac) <= 6 else "ns"
    return secs * 10**9 + int((frac + "000000000")[:9]), prec, len(frac) > 9


PRECISION_RANK = {"s": 0, "ms": 1, "us": 2, "ns": 3}


def coarsest(*precisions: str | None) -> str:
    """The coarsest of the given precisions (`ns` when none is given)."""
    ps = [p for p in precisions if p is not None]
    if not ps:
        return "ns"
    return min(ps, key=lambda p: PRECISION_RANK[p])
