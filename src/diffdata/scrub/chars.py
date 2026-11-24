"""Characters the scrub reads past or reads as their plain form."""

import re

# Characters that show as nothing: Unicode's format characters (Cf), like a zero-width
# space, a soft hyphen or a direction mark, and the rest of what it calls default ignorable,
# like a variation selector or a Hangul filler. Text pasted from web pages or Word can hold
# them inside a name. A test checks the list against Python's own Unicode data.
INVISIBLE = (
    r"\u00ad\u034f\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u115f\u1160\u17b4\u17b5"
    r"\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0"
    r"\ufff0-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    r"\U0001d173-\U0001d17a\U000e0000-\U000e0fff"
)
HIDDEN = re.compile(f"[{INVISIBLE}]+")
# Wide letters from Chinese, Japanese and Korean keyboards (Ａ), narrow ones (ｱ) and styled
# ones from fancy-text tools (𝐀) are compared as their plain letter. Other compatibility
# forms keep their shape, so "Alex™" still ends at the ™.
PLAIN_FORMS = ("<wide>", "<narrow>", "<font>")
