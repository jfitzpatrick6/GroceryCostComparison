"""
General-purpose cross-store product matching (#25).

Problem: a grocery-list item is free text a person typed ("chicken breast",
"2% milk", "yellow onion"). The scraped catalog (`grocery_prices_latest`)
holds real store listings, which are almost never that exact string - they
carry a brand, package size, and marketing language the list item doesn't
("Wellsley Farms Boneless Skinless Chicken Breasts, 4.5-6.5 lbs."). Before
this, app.py did an exact (or later, substring) match against `product`,
which only ever worked for the handful of items someone had hand-matched
(see the removed comments in grocery_list()/where_to_buy() this replaces).

v1 approach, in order of effort (per the issue - no embeddings/LLM):

1. Normalize both the list item and every catalog product into a set of
   significant tokens: lowercase, strip punctuation, drop pure numbers/
   package-size/unit words and generic marketing filler, singularize, and
   canonicalize a handful of common synonyms. This is the primary mechanism.
2. A product matches a list item if the *normalized item's tokens are a
   subset of the normalized product's tokens* - i.e. every meaningful word
   in "chicken breast" shows up somewhere in the product name, brand/size
   noise and all. This is deliberately not exact-phrase or word-order
   sensitive, since real catalog listings reorder and pad with brand names.
3. Manual aliases (MANUAL_ALIASES below) are consulted first, as an
   override/fallback for cases normalization gets wrong or doesn't cover
   (abbreviations, terms with no lexical overlap with how stores list them).
   They are not the primary path - normalization runs either way, just
   against the alias's phrase(s) instead of the raw item name.
4. Anything left with zero matches is returned as unmatched, not dropped -
   callers (app.py) are expected to show it as "no price found" rather than
   silently omitting it from the list (see where_to_buy()/grocery_list()).

Known limitations (v1, documented rather than solved - see the issue's
"no embeddings/LLM for v1" and the module docstring in app.py for where
this plugs in):

- Single generic-word queries ("milk", "cheese", "butter") are inherently
  ambiguous against a full store catalog - confirmed against a real 23k-row
  scrape (Tops/Aldi/BJs) while building this: a bare "butter" query
  initially resolved to "Peanut Butter", then "Coffee Creamer" for
  "coffee", then a bag of "Butter Puffed Corn" for a *different* store once
  the obvious cases were patched. DISQUALIFYING_MODIFIERS below is a
  hand-maintained patch for the specific cases found this way, not a
  general fix - it doesn't understand category ("dairy" vs "snack food") at
  all, just a growing blocklist of tokens that happened to falsely win.
  Expect more of these to exist for terms not exercised here; MIN_SCORE
  keeps it to a dull roar generally (favors matches with fewer *extra*
  tokens) but tightening it further trades away recall on genuinely
  multi-word product names, which is the more common case in a 30-50 item
  list - see the trade-off notes below MIN_SCORE. A household that keeps
  hitting a bad match for one of their regulars should add a MANUAL_ALIASES
  entry naming the product more specifically (e.g. "butter": ["salted
  butter sticks"]) rather than this module trying to guess every case.
- No real stemming library - just a few hand-rolled plural-suffix rules.
  Irregular plurals/forms not covered by those rules won't collapse
  together (e.g. "leaf" vs "leaves" IS handled; something structurally
  unusual might not be).
- Brand names aren't stripped explicitly (no maintained brand list) -
  they're tolerated as "extra" tokens on the product side, which is what
  makes token-subset matching work without one, but also means a query
  that accidentally *is* a brand-ish word could behave oddly.
- No fuzzy/typo tolerance (e.g. "chiken") - out of scope for v1.
"""

import re

# --- Normalization ----------------------------------------------------

# Multi-word synonyms, applied to the whole string before tokenizing (so
# order-sensitive phrases collapse before we lose word order to a set).
# Each replaces the left-hand phrase with a single canonical phrase - both
# sides of a match (list item and catalog product) go through the same
# substitution, so it doesn't matter which phrasing either one used.
_PHRASE_SYNONYMS = [
    (re.compile(r"\bconfectioners['’]?\s+sugar\b"), "powdered sugar"),
    (re.compile(r"\bgarbanzo\s+beans?\b"), "chickpeas"),
    (re.compile(r"\bchick\s*peas?\b"), "chickpeas"),
    (re.compile(r"\bgreen\s+onions?\b"), "scallion"),
    (re.compile(r"\bspring\s+onions?\b"), "scallion"),
    (re.compile(r"\bscallions?\b"), "scallion"),
    (re.compile(r"\bcorn\s*starch\b"), "cornstarch"),
    (re.compile(r"\bhalf\s*(?:&|and)\s*half\b"), "half and half"),
    (re.compile(r"\bbell\s+peppers?\b"), "bell pepper"),
    (re.compile(r"\bsweet\s+peppers?\b"), "bell pepper"),
    (re.compile(r"\bhamburger\s+meat\b"), "ground beef"),
    (re.compile(r"\begg\s*plant\b"), "eggplant"),
    (re.compile(r"\bpeanut\s+butter\b"), "peanut butter"),
    (re.compile(r"\ball[\s-]purpose\s+flour\b"), "all purpose flour"),
    (re.compile(r"\bap\s+flour\b"), "all purpose flour"),
    (re.compile(r"\bgreek\s+yog?hurt\b"), "greek yogurt"),
]

# Single-word canonicalizations, applied per-token after tokenizing/
# stemming. Keys and values are both post-stem forms.
_TOKEN_SYNONYMS = {
    "pop": "soda",
    "cola": "soda",
    "yoghurt": "yogurt",
    "courgette": "zucchini",
    "aubergine": "eggplant",
    "capsicum": "pepper",
    "cilantro": "cilantro",
    "coriander": "cilantro",  # rough approximation (leaves vs seed differ) - documented limitation
    "beef": "beef",
}

# Words that describe packaging/marketing, not product identity - dropped
# entirely when they appear as their own token. Deliberately conservative:
# doesn't include words that are sometimes the actual item ("bag" as in
# trash bags, "can" as in a canned good's own name) - those cases are a
# known gap, see the module docstring; MANUAL_ALIASES is the escape hatch.
_STRIP_WORDS = {
    "pack", "packs", "package", "packaged", "pk", "count", "ct", "value",
    "family", "size", "sizes", "bulk", "bundle",
    "assorted", "variety", "selection", "each", "ea", "piece", "pieces",
    "pc", "the", "a", "an", "and", "with", "of", "in", "for", "your",
    "new", "our",
    # "per" is a rate preposition, not part of a product name. Without it,
    # "Kirkwood Chicken Breasts, per lb" has "per" as its head noun, and a
    # single-token "chicken" query took the non-head penalty against a product
    # that is exactly chicken - which is how "boneless skinless chicken breast"
    # degraded from 0.800 to 0.560, breaking #99's explicit done bar.
    "per",
}
# Deliberately NOT stripped despite reading like marketing filler: "club"
# and "warehouse" - real data check turned up an Aldi private-label line
# literally named "Cheese Club" ("Cheese Club Macaroni and Cheese"); with
# "club" stripped its tokens collapsed to just {cheese, macaroni}, which
# then out-scored genuine cheese products for a plain "cheese" query. Left
# in as a token instead, at the cost of not stripping "warehouse club"-
# style language elsewhere - the false-match risk from stripping it
# outweighed the marketing-language cleanup it was buying.

# Unit words that show up either attached to a number ("12 oz") or loose in
# a product name - dropped like _STRIP_WORDS. Mirrors the unit vocabulary
# already recognized by Groceries/units.py and the scrapers, so this stays
# consistent with what "size" actually looks like in this catalog.
_UNIT_WORDS = {
    "lb", "lbs", "pound", "pounds", "oz", "ounce", "ounces", "fl", "gal",
    "gallon", "gallons", "pt", "pint", "pints", "qt", "quart", "quarts",
    "l", "liter", "liters", "litre", "litres", "ml", "g", "gram", "grams",
    "kg", "kilogram", "kilograms", "ft", "feet", "foot", "in", "inch",
    "inches", "dozen", "doz",
}

_PURE_NUMBER = re.compile(r"^\d+([./]\d+)?$")
_WORD_SPLIT = re.compile(r"[^a-z0-9%]+")


def _stem(token):
    """Hand-rolled plural stripping - no external stemming dependency (see
    the module docstring's limitations). Ordered so more specific suffixes
    are checked before the generic trailing-s rule."""
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ves") and len(token) > 4:
        return token[:-3] + "f"
    if token.endswith("oes") and len(token) > 4:
        return token[:-2]
    if token.endswith("es") and len(token) > 4 and token[:-2].endswith(("s", "x", "z", "ch", "sh")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def normalize_tokens(text):
    """Text -> frozenset of significant, stemmed, synonym-canonical tokens.
    Empty/near-empty input (e.g. a product name that's all size/brand noise
    once stripped) returns an empty frozenset, which never matches anything
    - deliberately, rather than matching everything."""
    if not text:
        return frozenset()
    lowered = text.lower().replace("&", " and ")
    for pattern, replacement in _PHRASE_SYNONYMS:
        lowered = pattern.sub(replacement, lowered)

    tokens = set()
    for raw in _WORD_SPLIT.split(lowered):
        if not raw:
            continue
        if _PURE_NUMBER.match(raw):
            continue
        if raw in _STRIP_WORDS or raw in _UNIT_WORDS:
            continue
        stemmed = _stem(raw)
        if stemmed in _STRIP_WORDS or stemmed in _UNIT_WORDS:
            continue
        tokens.add(_TOKEN_SYNONYMS.get(stemmed, stemmed))
    return frozenset(tokens)


# --- Word-order signals (#99) --------------------------------------------
#
# score_match's ratio (query tokens / product tokens) cannot tell a branded
# version of the right product from a different product that merely mentions it.
# Both "Wellsley Farms Bacon" and "TOPS Bacon Chips" are three tokens containing
# "bacon", so both scored 0.333 for the query "bacon" - and since where_to_buy
# then picks the CHEAPEST per store, the wrong product wins whenever it costs
# less. Measured on the live 21,574-row catalog, that produced:
#
#   bacon -> "Breakfast Pizza With Bacon", "TOPS Bacon Chips"
#   eggs  -> "Aldi Potato Salad with Egg"
#   milk  -> "Goya Coconut Milk", "Carnation Evaporated Milk" (tied with real milk)
#
# Two structural signals separate most of these without a lexicon of every
# product name:
#
# 1. A connective before the query word means the query word is an INGREDIENT of a
#    composite, not the product. "Breakfast Pizza With Bacon" is a pizza; "Aldi
#    Potato Salad with Egg" is a salad. This has to be read from the RAW words,
#    because "with", "and" and "in" are all in _STRIP_WORDS and so vanish from the
#    normalized token set entirely.
# 2. The head noun. English grocery names put the product last ("Wellsley Farms
#    Bacon"), so a product whose final significant token is not one of the query's
#    is more likely something else that merely mentions it ("TOPS Bacon Chips" is
#    chips). Read from the normalized sequence so trailing counts and units -
#    which _STRIP_WORDS/_UNIT_WORDS already drop - don't masquerade as the head
#    noun ("Eggs 12 ct" normalizes to just ["egg"]).
#
# Both are PENALTIES, not rejections. Inverted names are common in this data
# ("Milk, Whole" has no query token last) and a hard rule would drop legitimate
# products; a penalty ranks them below a better candidate while still matching
# when nothing better exists. Combined with the low-confidence marker from #97 and
# the picker from #98, a surviving weak match is visible and correctable rather
# than silent.

# "w" is deliberately NOT here, even though "w/" abbreviates "with" on packaging:
# token_sequences() unconditionally rewrites "&" to " and ", so "A&W Cream Soda"
# tokenizes to [a, and, w, cream, soda] and a standalone "w" would make every A&W
# and B&W product look like a composite. 24 live rows carry a standalone "w", 12
# of them created by that rewrite, and "A&W Cream Soda" was confirmed lost. The
# "&" rewrite already catches genuine "X with Y" composites, so nothing is given
# up by leaving "w" out.
_INGREDIENT_CONNECTIVES = {
    "with", "and", "in", "containing", "contained", "topped", "filled", "stuffed",
}

# Multiplicative penalties. Chosen so a composite ("Breakfast Pizza With Bacon",
# ratio 0.333) drops below MIN_SCORE and stops matching at all, while a merely
# non-head-noun match ("TOPS Bacon Chips", 0.333) survives at 0.233 and still
# ranks below the real thing (0.333) - it may be the only bacon a store stocks.
COMPOSITE_PENALTY = 0.4
NON_HEAD_PENALTY = 0.7


def token_sequences(text):
    """(normalized tokens in order, raw lowercase words in order).

    The normalized list is what the head-noun check needs; the raw list is what
    the connective check needs, because _STRIP_WORDS removes exactly the words
    ("with", "and", "in") that carry the signal. Both come from one pass over the
    same lowercased/synonym-rewritten text so they cannot disagree about what the
    product name was.
    """
    if not text:
        return [], []
    lowered = text.lower().replace("&", " and ")
    for pattern, replacement in _PHRASE_SYNONYMS:
        lowered = pattern.sub(replacement, lowered)

    raw = [w for w in _WORD_SPLIT.split(lowered) if w]
    seq = []
    for word in raw:
        if _PURE_NUMBER.match(word):
            continue
        if word in _STRIP_WORDS or word in _UNIT_WORDS:
            continue
        stemmed = _stem(word)
        if stemmed in _STRIP_WORDS or stemmed in _UNIT_WORDS:
            continue
        seq.append(_TOKEN_SYNONYMS.get(stemmed, stemmed))
    return seq, raw


def _is_composite(query_tokens, raw_seq):
    """True when a query token appears immediately after a connective AND not
    before it - i.e. the product is a composite that merely contains the queried
    thing rather than being it.

    The "and not before it" half is what keeps canned goods working. "Cento Minced
    Clams in Clam Juice" has clams on both sides of "in": it IS clams, packed in
    juice. Treating that as a composite dropped 26 live rows including "Full
    Circle Tomatoes in Tomato Juice" and "Dole Pineapple Tidbits in Pineapple
    Juice". "Breakfast Pizza With Bacon" has bacon only after the connective, so
    it is still correctly a pizza.

    Checks each following word raw and normalized, because query tokens are
    stemmed/synonym-mapped and raw_seq is not.
    """
    first = None
    for i, word in enumerate(raw_seq[:-1]):
        if word in _INGREDIENT_CONNECTIVES:
            first = i
            break
    if first is None:
        return False

    def norm(word):
        stemmed = _stem(word)
        return _TOKEN_SYNONYMS.get(stemmed, stemmed)

    before = {norm(w) for w in raw_seq[:first]} | set(raw_seq[:first])
    if query_tokens & before:
        return False

    for i in range(first, len(raw_seq) - 1):
        if raw_seq[i] not in _INGREDIENT_CONNECTIVES:
            continue
        nxt = raw_seq[i + 1]
        if nxt in query_tokens or norm(nxt) in query_tokens:
            return True
    return False


# --- Manual aliases (override/fallback layer) --------------------------

# Keyed by the *raw* grocery-list item name, lowercased/stripped - not by
# normalized tokens, so this stays easy to read/extend by hand. Each value
# is a list of alternate phrases to normalize and match *instead of* the
# item's own name, for cases where normalization alone gets it wrong or
# finds nothing (abbreviations, terms with no lexical overlap with how a
# store lists the product). This is intentionally a short, illustrative
# starting set, not an attempt at completeness - add entries here as real
# misses turn up against a real catalog (the primary mechanism is
# normalization; this is the escape hatch, not the workhorse).
MANUAL_ALIASES = {
    "tp": ["toilet paper"],
    "toilet paper": ["toilet paper", "bath tissue"],
    "oj": ["orange juice"],
    "pb": ["peanut butter"],
    "pb&j": ["peanut butter", "jelly"],
    "ground turkey": ["ground turkey"],
    "veggies": ["vegetables"],
    "soda": ["soda", "pop"],
    "pop": ["soda", "pop"],
    "ap flour": ["all purpose flour"],
    "confectioners sugar": ["powdered sugar"],
    "powdered sugar": ["powdered sugar", "confectioners sugar"],
}


def search_terms_for(item_name):
    """The phrase(s) to actually normalize + match for one list item -
    MANUAL_ALIASES's override if the raw (lowercased/stripped) item name is
    a known key, otherwise just the item name itself."""
    key = (item_name or "").strip().lower()
    return MANUAL_ALIASES.get(key, [item_name])


# --- Matching ------------------------------------------------------------

# A candidate product must retain at least this fraction of its normalized
# tokens as "the part that matched the query" to count - i.e.
# len(query_tokens) / len(product_tokens) >= MIN_SCORE. This exists purely
# to keep a short/generic query (like "milk") from matching a product whose
# name is mostly unrelated marketing copy that happens to contain the word
# ("3 Musketeers Candy Milk Chocolate Bar Full Size"). It's a blunt
# instrument - see the module docstring's limitations - tuned empirically
# against a real scraped catalog (23k rows, three chains) rather than
# picked arbitrarily; lower and short queries get noisy, higher and
# legitimate multi-word product names (brand + descriptors) start getting
# excluded.
MIN_SCORE = 0.2

# Below this, a match is shown to the user with a visible "not sure" marker
# rather than presented as an answer (#97). MIN_SCORE decides what is allowed
# to match at all; this decides what the UI may present *confidently*. They are
# different questions, and conflating them is how a 0.25 match came to look
# identical to a 0.8 one.
#
# 0.45 is derived from measured scores on the live 21,574-row catalog, not from
# theory. Generic one-word queries that resolved to the wrong food scored
# 0.25-0.333 ("bacon" -> "TOPS Bacon Chips" and "Breakfast Pizza With Bacon",
# "eggs" -> "Aldi Potato Salad with Egg", "milk" -> "Goya Coconut Milk" tied with
# real milk). Queries that resolved correctly scored 0.5-0.8 ("chicken breasts"
# 0.667, "boneless skinless chicken breast" 0.800). 0.45 sits in the observed gap
# between the two populations.
#
# It is a presentation threshold, deliberately not a filter: raising MIN_SCORE to
# 0.45 instead would turn these into "no match", which is more honest but less
# useful than showing the candidate and saying the app isn't sure. See #99 for
# the scoring itself and #98 for letting the user pick from candidates.
LOW_CONFIDENCE_SCORE = 0.45

# For a handful of common single-word queries, a specific extra token on
# the product side means "this is a different food that happens to share
# the word", not "this is the same food, different brand" - token-subset
# matching alone can't tell "Butter" from "Peanut Butter", or "Coffee" from
# "Coffee Creamer"/"Coffee Filters", since the query's one token is
# genuinely present in the product either way and MIN_SCORE alone doesn't
# reliably separate them (both found matching genuine 23k-row catalog data
# for #25 - a bare "butter"/"coffee" query on the real Tops/Aldi/BJs data
# actually resolved to peanut butter / coffee creamer without this).
# Only applied when the *query* is a single token - a multi-word query like
# "peanut butter" already disambiguates itself via its own tokens, and
# doesn't need (or want) this. Same spirit as MANUAL_ALIASES: a short,
# hand-maintained patch for specific normalization misses, not a general
# solution - grow this from real misses rather than guessing at more of
# them upfront.
#
# #56 judgment call: is hand-patching this blocklist as misses turn up
# (still the approach here after this pass) good enough, or does it need a
# cheap structural fix instead (e.g. requiring the query and product to
# share a *category-defining* second token for single-word queries)? Kept
# it hand-patched. A category-based fix isn't actually cheaper - it just
# relocates the hand-maintained list from "tokens that disqualify X" to
# "which category X belongs to" plus a token->category map for every
# product token that could show up, which is strictly more bookkeeping for
# the same coverage, and still needs a per-term list (this one) for terms
# that don't cleanly own one category (e.g. "coffee" the drink vs "coffee"
# the flavor - both legitimately produce "coffee X" names). A miss here
# degrades to a wrong-but-visible price row, not a crash, so the cost of
# staying reactive is low and matches how MANUAL_ALIASES already treats the
# same kind of miss.
DISQUALIFYING_MODIFIERS = {
    # Added from misses measured on the live 21,574-row catalog for the query
    # "milk" (#99): "Goya Coconut Milk" and "Carnation Evaporated Milk" both
    # scored 0.333, tied with real milk, and where_to_buy then picks the cheapest
    # per store - so coconut milk could win on price and be presented as the
    # answer to "milk".
    #
    # The structural word-order rules above could not catch these: in both names
    # "milk" IS the head noun and there is no connective. Coconut milk and
    # evaporated milk are genuinely milk-shaped products that simply aren't what
    # someone writing "milk" on a grocery list means, and no positional signal
    # distinguishes them. This is the case a lexicon is actually for.
    #
    # Only applied for single-token queries (see score_match), so a list that says
    # "coconut milk" or "evaporated milk" still matches those products exactly.
    #
    # Entries beyond the two observed are the same class - plant-based or
    # shelf-stable products that are not drinking milk - included because leaving
    # them out means each one gets discovered by a wrong price rather than by a
    # test. Deliberately EXCLUDED: "chocolate" and "goat", which are real drinking
    # milk to most people, so rejecting them would be a wrong answer of its own.
    "milk": {
        "coconut", "evaporated", "condensed", "powdered", "dried",
        "almond", "soy", "oat", "rice", "cashew", "hazelnut", "macadamia",
    },
    "butter": {
        "peanut", "almond", "cashew", "sunflower", "cocoa", "shea", "apple",
        "cookie", "cookies", "cracker", "crackers", "popcorn", "spray",
        "twist", "twists", "curry", "squash", "syrup", "seasoning", "scotch",
        "corn", "puffed", "crouton",
        # Found auditing #56 (real, confirmed against normalize_tokens, not
        # guessed): "pecan" - "Butter Pecan Ice Cream" is a token-subset of
        # a bare "butter" query (score 0.25, above MIN_SCORE) with nothing
        # to block it - the reported bug. "bean"/"beans" and "lettuce" are
        # the same failure for two other real product categories ("Butter
        # Beans" and "Butter Lettuce" both score 0.5 against "butter" alone
        # with no disqualifier).
        "pecan", "bean", "beans", "lettuce",
    },
    "coffee": {
        "creamer", "filter", "cake", "mate", "flavored", "liqueur",
        # Found auditing #56: the same "flavor word + ice cream" pattern as
        # butter/pecan - "Coffee Ice Cream" scores 0.33 and "Vanilla Coffee
        # Ice Cream Bar" scores exactly 0.2 (right at MIN_SCORE) against a
        # bare "coffee" query, both confirmed via score_match directly.
        "ice", "cream",
    },
    "egg": {
        "noodle", "noodles", "roll", "rolls", "kinder", "joy", "chocolate",
        # Found auditing #56: "Egg Nog" (written as two words, as opposed to
        # "Eggnog" which is already a single token and safe) scores 0.5
        # against a bare "egg" query - a drink, not the grocery item "eggs".
        "nog",
    },
}


def score_match(query_tokens, product_tokens, token_seq=None, raw_seq=None):
    """None if product_tokens doesn't contain every query token (i.e. not a
    match at all) or is disqualified by DISQUALIFYING_MODIFIERS; otherwise a
    0-1 closeness score - higher means the product name is "mostly" the
    query with little extra noise.

    token_seq/raw_seq are the ordered forms from token_sequences() and are
    OPTIONAL: without them the word-order penalties are skipped and the score is
    exactly the old ratio. That keeps every existing caller and every hand-built
    catalog fixture working, and means a row that somehow lacks the sequences
    degrades to the previous behaviour rather than crashing or silently scoring
    zero. See #99 for why the penalties exist.
    """
    if not query_tokens or not product_tokens:
        return None
    if not query_tokens.issubset(product_tokens):
        return None
    if len(query_tokens) == 1:
        disqualifiers = DISQUALIFYING_MODIFIERS.get(next(iter(query_tokens)))
        if disqualifiers and product_tokens & disqualifiers:
            return None
    ratio = len(query_tokens) / len(product_tokens)

    # ELIGIBILITY is decided on the raw ratio, BEFORE any word-order penalty.
    # Applying MIN_SCORE after multiplying turned the penalty into a ban: a
    # 1-token query against a 4+-token product scores 0.25 x 0.7 = 0.175, under
    # MIN_SCORE, so the product stopped matching at all. Measured on the live
    # 21,812-row catalog that silently dropped correct matches wholesale - for
    # "cheese", 241 of the 395 rows that matched on master returned None,
    # including "Athenos Traditional Feta Cheese Chunk" and "Hormel Real Crumbled
    # Bacon, Original". A match that vanishes is worse than one that ranks low,
    # because where_to_buy simply omits it from the totals and nothing says so.
    if ratio < MIN_SCORE:
        return None

    # Both penalties apply to SINGLE-TOKEN queries only, which is the scope #99
    # describes and the same guard DISQUALIFYING_MODIFIERS already uses. Applying
    # them to multi-word queries was actively harmful: _PHRASE_SYNONYMS rewrites
    # "Half & Half" to "half and half", so the query's OWN connective made the
    # composite rule fire against its exact match, and "half and half" resolved to
    # "Southern Grove Pecan Halves" at all three stores - a confident wrong price,
    # the precise failure #99 exists to prevent. "macaroni and cheese", "bread and
    # butter pickles" and MANUAL_ALIASES["pb&j"] broke the same way.
    if len(query_tokens) == 1:
        score = ratio
        if raw_seq and _is_composite(query_tokens, raw_seq):
            score *= COMPOSITE_PENALTY
        # The head noun is the last significant token. No penalty when the product
        # name ends in a query token, the normal English grocery shape ("Wellsley
        # Farms Bacon"); penalized when it does not, because then the product is
        # something else that merely mentions the query ("TOPS Bacon Chips" is
        # chips). A ranking signal only - see the eligibility note above.
        if token_seq and not (query_tokens & {token_seq[-1]}):
            score *= NON_HEAD_PENALTY
        return score
    return ratio


def load_catalog(cur):
    """Fetches the entire latest-price catalog once (every store) and
    precomputes each product's normalized tokens - meant to be called once
    per request and reused across every list item, rather than one query
    per item (the previous per-item ILIKE queries in app.py did N separate
    round trips; this does one). 23k rows is small enough to hold in memory
    and normalize in Python for a self-hosted, single-request workload.

    `datetime` is selected even though matching never reads it. The view holds
    exactly one row per (product, store) - the most recent - so the per-store max
    of this column *is* that store's last scrape time. Carrying it lets app.py say
    how old the prices behind a comparison are (#62) with no extra query, instead
    of a `GROUP BY store` aggregate that would scan the whole history table a
    second time on every page load; one extra date per row is far cheaper than
    that. It does not affect matching, so tests that build catalog fixtures by
    hand are unaffected by not including it."""
    cur.execute("SELECT product, store, price, size, unit_price, unit, datetime FROM grocery_prices_latest")
    rows = cur.fetchall()
    catalog = []
    for row in rows:
        # One pass, not two. token_sequences() recomputes exactly what
        # normalize_tokens() does, and calling both doubled catalog
        # normalization: measured 551ms -> 1,079ms on the live 21,812-row catalog,
        # i.e. +528ms on every /list and /list/where-to-buy request.
        # frozenset(seq) is identical to normalize_tokens(product) - verified
        # equal across all 21,812 rows, 0 mismatches.
        seq, raw = token_sequences(row["product"])
        tokens = frozenset(seq)
        if not tokens:
            # Rows dropped here never reach the catalog, so a caller deriving
            # anything from the catalog sees a subset. For matching that is the
            # point - a product with no tokens cannot match. For #62's freshness
            # banner, which takes a per-store max over this same list, it can only
            # *under*-state how recent a store's data is, i.e. warn when it needn't.
            # That is the safe direction: a false "out of date" is an annoyance, a
            # false "current" is the thing this app must not say.
            continue
        entry = dict(row)
        entry["_tokens"] = tokens
        # Ordered forms for score_match's word-order penalties (#99). Computed
        # here rather than inside match_item because match_item runs per list item
        # against the whole catalog: once per row at load time is ~20k
        # computations instead of ~20k per item.
        entry["_token_seq"], entry["_raw_seq"] = seq, raw
        catalog.append(entry)
    return catalog


def match_item(catalog, item_name):
    """All catalog rows that match one grocery-list item name, each
    annotated with `match_score` (see score_match), sorted best-first
    (highest score, i.e. least extra noise, first). Empty list means
    unmatched - callers show that as "no price found", never drop the
    item (see #25's "done" bar)."""
    best_by_key = {}
    for term in search_terms_for(item_name):
        query_tokens = normalize_tokens(term)
        if not query_tokens:
            continue
        for row in catalog:
            # .get() rather than [] so a hand-built catalog fixture (or any caller
            # that predates #99) still works - score_match treats missing
            # sequences as "skip the word-order penalties".
            score = score_match(
                query_tokens, row["_tokens"],
                row.get("_token_seq"), row.get("_raw_seq"),
            )
            if score is None:
                continue
            key = (row["store"], row["product"])
            if key not in best_by_key or score > best_by_key[key][0]:
                best_by_key[key] = (score, row)

    matches = []
    for score, row in best_by_key.values():
        # Strip every underscore-prefixed working field rather than a hardcoded
        # list: _tokens, _token_seq and _raw_seq are the matcher's scratch state
        # and must not reach templates or any future serialization. A name list
        # here is how one gets forgotten the next time a field is added.
        match = {k: v for k, v in row.items() if not k.startswith("_")}
        match["match_score"] = score
        matches.append(match)
    matches.sort(key=lambda m: -m["match_score"])
    return matches


def best_per_store(matches):
    """Reduces a match_item() result to at most one product per store - the
    highest-scoring identity match, with price only breaking an exact-score
    tie. This matters: match_item() can legitimately return several loosely
    related products for a generic query (e.g. "milk" also matching a candy
    bar whose name contains the word), and callers that pick "cheapest"
    without this step could pick the candy bar over actual milk just
    because it's cheaper. Price should decide between *stores* selling the
    same identified product, not between unrelated products both claiming
    to satisfy the query."""
    best = {}
    for m in matches:
        current = best.get(m["store"])
        if current is None or (m["match_score"], -m["price"]) > (current["match_score"], -current["price"]):
            best[m["store"]] = m
    return best
