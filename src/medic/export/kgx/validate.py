"""The KGX conformance gate.

Checks a built graph against the *installed* Biolink model rather than against a
hand-maintained belief about what Biolink contains. Run by ``just validate-kgx`` and by
``tests/test_kgx_export.py``.

The core of every record is re-validated through the Biolink pydantic class its ``category``
names — the same class the builders construct it with — so a file that was edited, or built
by an older exporter, is held to exactly the same rules: no undeclared property, no enum
value outside the permissible set, no list where the class wants a scalar, nothing required
missing. On top of that the gate checks what the pydantic class cannot: that the predicate
specialises what the association class restricts itself to, that every endpoint has a node,
that the few off-class Biolink slots the exporter writes exist and take permissible values,
and that invariant I-1 holds at the export boundary.

Severity is deliberate: anything that would make a consumer reject or mis-model the graph is
an **error**; anything that is merely unusual — an identifier prefix Biolink's list does not
mention, which MeDIC legitimately produces for ``DRON:`` and ``MedDRA:`` — is a **warning**
with a count, so real problems are not buried under noise.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from medic.curie_utils import get_prefix
from medic.export.kgx import biolink as bl

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Problem:
    severity: str
    message: str


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == ERROR]

    @property
    def warnings(self) -> list[Problem]:
        return [p for p in self.problems if p.severity == WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        self.problems.append(Problem(ERROR, message))

    def warn(self, message: str) -> None:
        self.problems.append(Problem(WARNING, message))


def _known_slot(name: str) -> bool:
    return name.replace("_", " ") in bl.biolink_slots()


def _check_properties(record: dict, kind: str, report: Report) -> bool:
    """Every un-namespaced property must be a Biolink slot; extensions must be namespaced.

    Returns whether the record is clean enough to hand to the class constructor — an unknown
    property would only be reported a second time there, less legibly.
    """
    clean = True
    for key in record:
        if key.startswith(bl.EXTENSION_PREFIX):
            bare = key[len(bl.EXTENSION_PREFIX):]
            if _known_slot(bare):
                report.error(
                    f"{kind} property '{key}' shadows Biolink slot '{bare}' — rename it"
                )
            continue
        if not _known_slot(key):
            report.error(
                f"{kind} property '{key}' is not a Biolink slot and is not namespaced "
                f"'{bl.EXTENSION_PREFIX}'"
            )
            clean = False
    return clean


def _single_category(record: dict, kind: str, report: Report) -> str:
    """The record's one category, or ``""`` after reporting why there is none."""
    record_id = record.get("id", "")
    categories = record.get("category") or []
    if isinstance(categories, str):
        categories = [categories]
    if not categories:
        report.error(f"{kind} {record_id} has no category")
        return ""
    if len(categories) != 1:
        # The pydantic class fixes `category` to itself; a consumer expands the hierarchy.
        report.error(f"{kind} {record_id} has {len(categories)} categories; expected one")
        return ""
    category = categories[0]
    if bl.model_name(category) not in bl.biolink_classes():
        report.error(f"{kind} {record_id} has unknown category '{category}'")
        return ""
    return category


def _validate_as(category: str, core: dict, kind: str, report: Report) -> None:
    """Re-run the record through the Biolink class its category names."""
    record_id = core.get("id", "")
    try:
        cls = bl.model_class(category)
    except AttributeError:
        report.error(f"{kind} {record_id}: category '{category}' has no pydantic class")
        return
    try:
        cls.model_validate(core)
    except ValidationError as exc:
        for err in exc.errors():
            where = ".".join(str(part) for part in err["loc"]) or "record"
            report.error(f"{kind} {record_id} is not a valid {cls.__name__}: "
                         f"{where}: {err['msg']}")


def _check_node(node: dict, report: Report, prefix_warnings: Counter) -> None:
    node_id = node.get("id", "")
    if not node_id:
        report.error("node without an id")
    clean = _check_properties(node, "node", report)
    category = _single_category(node, "node", report)
    if not category:
        return
    if clean:
        core = {k: v for k, v in node.items() if not k.startswith(bl.EXTENSION_PREFIX)}
        _validate_as(category, core, "node", report)

    allowed = bl.id_prefixes(category)
    prefix = node_id.split(":", 1)[0]
    if allowed and prefix not in allowed:
        prefix_warnings[(prefix, category)] += 1


def _check_edge(edge: dict, node_ids: set[str], report: Report) -> None:
    edge_id = edge.get("id", "")
    clean = _check_properties(edge, "edge", report)

    predicate = edge.get("predicate", "")
    if not bl.is_predicate(predicate):
        report.error(f"edge {edge_id} has unknown predicate '{predicate}'")

    category = _single_category(edge, "edge", report)
    if category:
        if "association" not in bl.model_name(category):
            report.error(f"edge {edge_id} category '{category}' is not an association class")
        constraint = bl.class_predicate_constraint(category)
        if constraint and constraint not in bl.predicate_ancestors(predicate):
            report.error(
                f"edge {edge_id} predicate '{predicate}' does not specialise "
                f"'{constraint}', which {category} requires"
            )
        if clean:
            try:
                fields = bl.model_class(category).model_fields
            except AttributeError:
                fields = {}
            core = {k: v for k, v in edge.items()
                    if not k.startswith(bl.EXTENSION_PREFIX)
                    and not (k in bl.OFF_CLASS_EDGE_SLOTS and k not in fields)}
            _validate_as(category, core, "edge", report)

    # `disease_context_qualifier` has range `disease`, and nothing on the way out enforces
    # it: UMLS and HP are on that class's `id_prefixes` so the constructor takes them, and
    # the field is typed `str` so it takes free text. Checked here because this is the only
    # place that can (#110). The grounded ids and the label's wording are on
    # `medic_disease_context_ids` / `medic_disease_context`, so this rejects a value in the
    # wrong place rather than a value that has nowhere else to go.
    context = edge.get("disease_context_qualifier")
    if context and get_prefix(context) != bl.DISEASE_CONTEXT_PREFIX:
        report.error(
            f"edge {edge_id} has disease_context_qualifier '{context}', which is not a "
            f"{bl.DISEASE_CONTEXT_PREFIX} term — the slot's range is `disease` and the "
            f"context belongs on medic_disease_context_ids or medic_disease_context"
        )

    # The off-class slots never meet the class constructor, so their enum ranges are
    # checked here. Existence was checked by _check_properties.
    for slot, enum_name in bl.OFF_CLASS_ENUMS.items():
        value = edge.get(slot)
        if value and value not in bl.enum_values(enum_name):
            report.error(f"edge {edge_id} has {slot} '{value}', not in {enum_name}")

    for end in ("subject", "object"):
        ref = edge.get(end)
        if not ref:
            report.error(f"edge {edge_id} has no {end}")
        elif ref not in node_ids:
            report.error(f"edge {edge_id} {end} '{ref}' has no node (dangling endpoint)")

    # Invariant I-1, echoed at the export boundary: a source may only speak for its own
    # jurisdiction. Cross-jurisdiction merging happens on the pair, never on one assertion.
    #
    # An unrecognised source is an ERROR, not a skip. The old default turned "I do not know
    # what this source is" into "there is nothing to check" — which is the failure mode where a
    # gate reports success precisely when it has lost track of the data. It hid every India
    # edge for the whole life of the export, because the map was keyed by authority (CDSCO)
    # and the exporter writes the ingester name (INDIA). The map is closed: a source appearing
    # here without an entry is exactly what a reader needs to be told about.
    source = (edge.get("medic_source") or "").upper()
    jurisdiction = (edge.get("medic_jurisdiction") or "").upper()
    if source and source not in bl.SOURCE_JURISDICTION:
        report.error(
            f"edge {edge_id} has source '{source}', which is not in SOURCE_JURISDICTION — "
            f"source isolation cannot be checked for it. Add it to the map."
        )
    else:
        expected = bl.SOURCE_JURISDICTION.get(source)
        if expected is not None and jurisdiction and jurisdiction != expected:
            report.error(
                f"edge {edge_id} violates source isolation: source '{source}' speaks for "
                f"{expected} but the edge claims jurisdiction '{jurisdiction}'"
            )


def check(nodes: list[dict], edges: list[dict]) -> Report:
    """Validate a built graph. Returns a report; ``report.ok`` is False on any error."""
    report = Report()
    prefix_warnings: Counter = Counter()

    node_ids: set[str] = set()
    for node in nodes:
        _check_node(node, report, prefix_warnings)
        node_ids.add(node.get("id", ""))

    for edge in edges:
        _check_edge(edge, node_ids, report)

    for (prefix, category), count in sorted(prefix_warnings.items()):
        report.warn(
            f"{count} node(s) use prefix '{prefix}' which Biolink does not list for "
            f"{category}"
        )

    # Not an error — the graph is still valid — but an edge whose primary source is the
    # aggregator means MeDIC could not attribute the claim, and that gap should be counted
    # rather than disappear into a plausible-looking infores id.
    unattributed = sum(
        1 for e in edges if e.get("primary_knowledge_source") == bl.AGGREGATOR
    )
    if unattributed:
        report.warn(
            f"{unattributed} edge(s) fall back to the aggregator "
            f"'{bl.AGGREGATOR}' as their primary knowledge source (unmapped source)"
        )
    return report


def _read_jsonl(path: Path) -> list[dict]:
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def check_files(nodes_path: Path, edges_path: Path) -> Report:
    return check(_read_jsonl(nodes_path), _read_jsonl(edges_path))
