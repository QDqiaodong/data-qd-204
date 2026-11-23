"""Leak-free train/holdout batch construction from reviewed annotations.

One :func:`build_corpora` call handles one batch: the current five-field
dictionary is compiled with the existing :func:`morph_paths.process`
semantics, every signed token analysis is re-validated against the forms the
dictionary actually produces, and the still-usable documents are split into
deliverable train and holdout sides that share no exposure key, minimizing
the total ``drop_cost`` of the documents that cannot be delivered.  The full
data contract lives in ``docs/contract.md``.
"""
from .checks import array, fields, ident, integer, unique
from .model import features as _features
from .model import text as _text
from .service import process

MAX_DOCUMENTS = 12000
MAX_TOKENS = 60000
MAX_DROP_COST = 1000000
MAX_GOLD_RULES = 300
MAX_HISTORY_KEYS = 1000000

SIDE_TRAIN = "train"
SIDE_HOLDOUT = "holdout"
SIDES = (SIDE_TRAIN, SIDE_HOLDOUT)

REASON_INVALID = "invalid"
REASON_HISTORY = "history"
REASON_CONFLICT = "conflict"

CATEGORY_DOC_IDS = "doc_ids"
CATEGORY_SURFACES = "surfaces"
CATEGORY_LEMMAS = "lemmas"
CATEGORIES = (CATEGORY_DOC_IDS, CATEGORY_SURFACES, CATEGORY_LEMMAS)

# Document ids are unique within a batch, so only surfaces and lemmas can
# connect a train document with a holdout document inside one batch.
_CONFLICT_CATEGORIES = (CATEGORY_SURFACES, CATEGORY_LEMMAS)


class _Dinic:
    """Deterministic max-flow over the flat four-layer selection network."""

    __slots__ = ("graph",)

    def __init__(self, size):
        self.graph = [[] for _ in range(size)]

    def add_edge(self, source, target, capacity):
        self.graph[source].append([target, capacity, len(self.graph[target])])
        self.graph[target].append([source, 0, len(self.graph[source]) - 1])

    def max_flow(self, source, sink, limit):
        graph = self.graph
        size = len(graph)
        flow = 0
        while True:
            level = [-1] * size
            level[source] = 0
            queue = [source]
            for node in queue:
                for edge in graph[node]:
                    if edge[1] > 0 and level[edge[0]] < 0:
                        level[edge[0]] = level[node] + 1
                        queue.append(edge[0])
            if level[sink] < 0:
                return flow
            todo = [0] * size
            while True:
                pushed = self._push(source, sink, level, todo, limit)
                if not pushed:
                    break
                flow += pushed

    def _push(self, node, sink, level, todo, limit):
        # Augmenting paths are at most four edges long in this network, so the
        # recursion depth stays bounded by a small constant.
        if node == sink:
            return limit
        edges = self.graph[node]
        while todo[node] < len(edges):
            edge = edges[todo[node]]
            if edge[1] > 0 and level[edge[0]] == level[node] + 1:
                pushed = self._push(edge[0], sink, level, todo, min(limit, edge[1]))
                if pushed:
                    edge[1] -= pushed
                    self.graph[edge[0]][edge[2]][1] += pushed
                    return pushed
            todo[node] += 1
        return 0

    def reachable(self, source):
        seen = [False] * len(self.graph)
        seen[source] = True
        stack = [source]
        while stack:
            node = stack.pop()
            for edge in self.graph[node]:
                if edge[1] > 0 and not seen[edge[0]]:
                    seen[edge[0]] = True
                    stack.append(edge[0])
        return seen


def _parse_history(history):
    """Validate the historical exposure record into three frozen key sets."""
    if history is None:
        return {category: frozenset() for category in CATEGORIES}
    fields(history, CATEGORIES)
    doc_ids = frozenset(
        ident(value) for value in array(history[CATEGORY_DOC_IDS], 0, MAX_HISTORY_KEYS)
    )
    lemmas = frozenset(
        ident(value) for value in array(history[CATEGORY_LEMMAS], 0, MAX_HISTORY_KEYS)
    )
    surfaces = set()
    for value in array(history[CATEGORY_SURFACES], 0, MAX_HISTORY_KEYS):
        surface = _text(value)
        if surface != value:
            raise ValueError("historical surfaces must be NFC-normalized")
        surfaces.add(surface)
    return {
        CATEGORY_DOC_IDS: doc_ids,
        CATEGORY_SURFACES: frozenset(surfaces),
        CATEGORY_LEMMAS: lemmas,
    }


def _parse_documents(documents):
    """Validate the batch structurally and return it in canonical id order."""
    docs = array(documents, 0, MAX_DOCUMENTS)
    parsed = []
    total_tokens = 0
    for doc in docs:
        fields(doc, ("id", "side", "drop_cost", "tokens"))
        doc_id = ident(doc["id"])
        side = doc["side"]
        if side not in SIDES:
            raise ValueError("side must be 'train' or 'holdout'")
        cost = integer(doc["drop_cost"], 1, MAX_DROP_COST)
        tokens = []
        for token in array(doc["tokens"], 1, MAX_TOKENS):
            fields(token, ("text", "gold"))
            original = token["text"]
            surface = _text(original)
            gold = token["gold"]
            fields(gold, ("lemma", "rules", "features"))
            tokens.append(
                (
                    original,
                    surface,
                    ident(gold["lemma"]),
                    [ident(rule) for rule in array(gold["rules"], 0, MAX_GOLD_RULES)],
                    _features(gold["features"]),
                )
            )
        total_tokens += len(tokens)
        if total_tokens > MAX_TOKENS:
            raise ValueError("total token limit exceeded")
        parsed.append({"id": doc_id, "side": side, "cost": cost, "tokens": tokens})
    unique([doc["id"] for doc in parsed])
    parsed.sort(key=lambda doc: doc["id"])
    return parsed


def _history_match(doc, past):
    """Return one canonical historical hit for a holdout document, if any."""
    own = {
        CATEGORY_DOC_IDS: (doc["id"],),
        CATEGORY_SURFACES: doc["surfaces"],
        CATEGORY_LEMMAS: doc["lemmas"],
    }
    for category in CATEGORIES:
        hits = past[category] & frozenset(own[category])
        if hits:
            return {"category": category, "value": min(hits)}
    return None


def _select(train_docs, hold_docs):
    """Split usable documents into kept train/holdout lists.

    Conflicts form a bipartite graph (train side vs holdout side, edges when
    two documents share a surface or lemma key).  Dropped documents must be a
    vertex cover of that graph; the minimum-total-cost cover is the min s-t
    cut of a four-layer network source -> train -> shared keys -> holdout ->
    sink, where routing every conflict through one node per shared key keeps
    the network linear in the number of key occurrences instead of quadratic
    in the number of document pairs.
    """
    if not train_docs or not hold_docs:
        return train_docs, hold_docs
    infinity = (
        sum(doc["cost"] for doc in train_docs)
        + sum(doc["cost"] for doc in hold_docs)
        + 1
    )
    train_node = {doc["id"]: 2 + index for index, doc in enumerate(train_docs)}
    hold_node = {
        doc["id"]: 2 + len(train_docs) + index for index, doc in enumerate(hold_docs)
    }
    key_node = {}
    size = 2 + len(train_docs) + len(hold_docs)
    for doc in train_docs + hold_docs:
        for category in _CONFLICT_CATEGORIES:
            for value in sorted(doc[category]):
                key = (category, value)
                if key not in key_node:
                    key_node[key] = size
                    size += 1
    flow = _Dinic(size)
    source, sink = 0, 1
    for doc in train_docs:
        flow.add_edge(source, train_node[doc["id"]], doc["cost"])
    for doc in hold_docs:
        flow.add_edge(hold_node[doc["id"]], sink, doc["cost"])
    for doc in train_docs:
        node = train_node[doc["id"]]
        for category in _CONFLICT_CATEGORIES:
            for value in sorted(doc[category]):
                flow.add_edge(node, key_node[(category, value)], infinity)
    for doc in hold_docs:
        node = hold_node[doc["id"]]
        for category in _CONFLICT_CATEGORIES:
            for value in sorted(doc[category]):
                flow.add_edge(key_node[(category, value)], node, infinity)
    flow.max_flow(source, sink, infinity)
    reached = flow.reachable(source)
    kept_train = [doc for doc in train_docs if reached[train_node[doc["id"]]]]
    kept_hold = [doc for doc in hold_docs if not reached[hold_node[doc["id"]]]]
    return kept_train, kept_hold


def _index_keys(kept_docs):
    """Map each shared key to the kept documents holding it, ids sorted."""
    by_key = {}
    for doc in kept_docs:
        for category in _CONFLICT_CATEGORIES:
            for value in doc[category]:
                by_key.setdefault((category, value), []).append(doc["id"])
    return by_key


def _witness(doc, opposite_by_key):
    """One canonical kept opposite-side document sharing a key with ``doc``."""
    for category in _CONFLICT_CATEGORIES:
        for value in sorted(doc[category]):
            key = (category, value)
            if key in opposite_by_key:
                return {
                    "with": opposite_by_key[key][0],
                    "key": {"category": category, "value": value},
                }
    # A minimum-cost cover element always borders at least one kept document;
    # otherwise dropping it would improve the cover.
    raise RuntimeError("minimum cover element without a kept neighbour")


def _deliver(doc):
    return {
        "id": doc["id"],
        "tokens": [
            {
                "text": original,
                "gold": {
                    "lemma": lemma,
                    "rules": list(rules),
                    "features": dict(sorted(gold_features.items())),
                },
            }
            for original, _surface, lemma, rules, gold_features in doc["tokens"]
        ],
    }


def build_corpora(data, documents, history=None):
    """Build the train/holdout corpora and the batch exposure record.

    ``data`` is the complete existing five-field dictionary input;
    ``documents`` is the reviewed batch; ``history`` is the merged exposure
    of the batches actually used for training so far (``None`` on the first
    round).  Structural problems raise ``ValueError`` and reject the whole
    request; unusable annotations, historical hits and in-batch conflicts are
    reported per document in the ``dropped`` part of the result.
    """
    index = {}
    for form in process(data)["forms"]:
        index.setdefault(form["surface"], []).append(form)
    past = _parse_history(history)
    docs = _parse_documents(documents)

    dropped = []
    usable = []
    for doc in docs:
        surfaces = set()
        lemmas = set()
        bad_positions = []
        for position, (_original, surface, lemma, rules, gold_features) in enumerate(
            doc["tokens"]
        ):
            analyses = index.get(surface, ())
            surfaces.add(surface)
            lemmas.update(form["lemma"] for form in analyses)
            if not any(
                form["lemma"] == lemma
                and form["rules"] == rules
                and form["features"] == gold_features
                for form in analyses
            ):
                bad_positions.append(position)
        if bad_positions:
            dropped.append(
                {
                    "id": doc["id"],
                    "reason": REASON_INVALID,
                    "token_positions": bad_positions,
                }
            )
            continue
        doc["surfaces"] = surfaces
        doc["lemmas"] = lemmas
        if doc["side"] == SIDE_HOLDOUT:
            match = _history_match(doc, past)
            if match is not None:
                dropped.append({"id": doc["id"], "reason": REASON_HISTORY, "match": match})
                continue
        usable.append(doc)

    train_docs = [doc for doc in usable if doc["side"] == SIDE_TRAIN]
    hold_docs = [doc for doc in usable if doc["side"] == SIDE_HOLDOUT]
    kept_train, kept_hold = _select(train_docs, hold_docs)
    kept_train_ids = {doc["id"] for doc in kept_train}
    kept_hold_ids = {doc["id"] for doc in kept_hold}

    train_by_key = _index_keys(kept_train)
    hold_by_key = _index_keys(kept_hold)
    for doc in train_docs:
        if doc["id"] not in kept_train_ids:
            entry = {"id": doc["id"], "reason": REASON_CONFLICT}
            entry.update(_witness(doc, hold_by_key))
            dropped.append(entry)
    for doc in hold_docs:
        if doc["id"] not in kept_hold_ids:
            entry = {"id": doc["id"], "reason": REASON_CONFLICT}
            entry.update(_witness(doc, train_by_key))
            dropped.append(entry)
    dropped.sort(key=lambda entry: entry["id"])

    exposure = {
        CATEGORY_DOC_IDS: sorted(kept_train_ids),
        CATEGORY_SURFACES: sorted(
            {surface for doc in kept_train for surface in doc["surfaces"]}
        ),
        CATEGORY_LEMMAS: sorted(
            {lemma for doc in kept_train for lemma in doc["lemmas"]}
        ),
    }
    return {
        "train": [_deliver(doc) for doc in kept_train],
        "holdout": [_deliver(doc) for doc in kept_hold],
        "dropped": dropped,
        "exposure": exposure,
    }
