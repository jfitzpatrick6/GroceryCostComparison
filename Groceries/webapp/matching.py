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


def score_match(query_tokens, product_tokens):
    """None if product_tokens doesn't contain every query token (i.e. not a
    match at all) or is disqualified by DISQUALIFYING_MODIFIERS; otherwise a
    0-1 closeness score - higher means the product name is "mostly" the
    query with little extra noise."""
    if not query_tokens or not product_tokens:
        return None
    if not query_tokens.issubset(product_tokens):
        return None
    if len(query_tokens) == 1:
        disqualifiers = DISQUALIFYING_MODIFIERS.get(next(iter(query_tokens)))
        if disqualifiers and product_tokens & disqualifiers:
            return None
    ratio = len(query_tokens) / len(product_tokens)
    if ratio < MIN_SCORE:
        return None
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
        tokens = normalize_tokens(row["product"])
        if not tokens:
            continue
        entry = dict(row)
        entry["_tokens"] = tokens
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
            score = score_match(query_tokens, row["_tokens"])
            if score is None:
                continue
            key = (row["store"], row["product"])
            if key not in best_by_key or score > best_by_key[key][0]:
                best_by_key[key] = (score, row)

    matches = []
    for score, row in best_by_key.values():
        match = {k: v for k, v in row.items() if k != "_tokens"}
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
