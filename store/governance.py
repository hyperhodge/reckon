#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Classification and presentation mode, spec-v0.2.md §2.1 and §8.5.
An earlier session, Phase 2 (PLAN.md row 6).

Two jobs, and they are in one module because they are two halves of the same
question: *what can this record say, and to whom.*

## Classification (§2.1)

§2.1 calls the governance block "the fields a client asks to see" -- and
earlier sessions stored it without ever reasoning about it. This makes it
queryable: which projects are classified, which are not, what tier they sit in,
and which ones are missing a classification a stage already owes.

The vocabularies live in `store.CLASSIFICATION_VOCABULARY` and which stage owes
which field is read out of `store.STAGE_REQUIREMENTS`. Nothing is restated here:
a second list of the four fields is a second place to be wrong.

**`is_ai_system` is three-valued -- yes | no | contested -- and never a bool.**
`store._coerce` already refuses the collapse. "contested" is the honest answer
for a system whose classification is argued about, which is most of the
interesting ones, and a boolean has nowhere to put it. There is a test.

## Presentation mode (§8.5)

    "Paths can be disclosive even when contents are not. The register renders
     in a presentation mode that resolves paths to opaque project labels, so it
     can be opened in front of one client without exposing another."

The redactor is enumerative, not heuristic. It walks `store.DISCLOSIVE_FIELDS`
-- a named list an earlier session shipped precisely so this session would have a list
rather than a grep -- and replaces each value with a stable opaque label. It
does not scan free text for things that look like paths, because a redactor
that guesses is one that will one day guess wrong in front of a client.

That puts a constraint back on the store, and it is worth stating plainly:
**a disclosive value must live in a named field or it cannot be redacted.**
Bodies are not exported and notes must never carry a path.

**Refs carry no meaning, by design.** `P-0007` says nothing about nanowiki, so
refs survive redaction untouched and remain the way to talk about a record in
front of someone who may not see its name. There is a test for that too.

### The one that is easy to miss

`store/refs.json` binds every ref to its natural key, and a project's natural
key is its slug. `nanowiki` is therefore in the export twice: once as
`projects.P-0007.name` and once as `refs.projects.P-0007`. Redacting the first
and shipping the second would be a presentation mode that leaks through its own
allocation register. Both are redacted, and the test asserts the value is absent
from the whole serialised payload rather than from the fields it remembered.

    python3 store/governance.py                 # what the register knows about classification
    python3 store/governance.py --queries       # the classification queries
    python3 store/governance.py --show P-0001
    python3 store/governance.py --present       # the redacted export (§8.5)
    python3 store/governance.py --present --out /tmp/for-a-client.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import store  # noqa: E402


# --------------------------------------------------------------------------
# Classification queries (§2.1)
# --------------------------------------------------------------------------

def classify_store(loaded):
    """Every project's governance block, in ref order."""
    return {ref: store.classify(project["fields"])
            for ref, project in sorted(loaded["projects"].items())}


def classification_queries(loaded):
    """The questions §2.1's block exists to answer.

    Each returns refs, never records: a query result that carries names is a
    query result that cannot be shown in presentation mode.
    """
    classified = classify_store(loaded)
    by_tier = {}
    for ref, result in classified.items():
        tier = result["fields"]["act_tier"]["value"] or "unclassified"
        by_tier.setdefault(str(tier), []).append(ref)
    return {
        # Missing a classification a stage already owes. This is the query the
        # brief asks for by name, and it is a subset of stage debt rather than a
        # new opinion about what is owed.
        "missing_at_a_stage_that_owes_it": {
            ref: result["missing_and_owed"]
            for ref, result in classified.items() if result["missing_and_owed"]},
        # Never classified at all. Different from the above and reported
        # differently: a captured shower thought owes nothing yet, and saying
        # "missing" about it would be the flat thirty-field form again.
        "never_classified": [ref for ref, result in classified.items()
                             if result["unclassified"]],
        # A value outside §2.1's own vocabulary. store.check() calls this a
        # problem; here it is a list.
        "invalid_values": {ref: result["invalid"]
                           for ref, result in classified.items() if result["invalid"]},
        "by_act_tier": {tier: sorted(refs) for tier, refs in sorted(by_tier.items())},
        # The three-valued field, kept three-valued. "contested" is a real
        # answer and it is the one worth a list of its own.
        "contested": [ref for ref, result in classified.items()
                      if result["fields"]["is_ai_system"]["value"] == "contested"],
        "ai_systems": [ref for ref, result in classified.items()
                       if result["fields"]["is_ai_system"]["value"] == "yes"],
        # §10 query 2. Added an earlier session (PLAN.md row 7) as one more entry here
        # rather than as a second classification reader elsewhere. `both` is
        # listed apart from `provider` deliberately: it IS provider for the
        # purpose of the question, and collapsing the two would throw away a
        # distinction §2.1's vocabulary makes on purpose. A project that has
        # never been classified is listed rather than assumed to be a deployer.
        "provider_not_deployer": _by_role(classified),
        # §2.1's third_party_output is the field that decides whether anything
        # leaves the building. Worth asking on its own.
        "output_reaches_a_third_party": [
            ref for ref, result in classified.items()
            if result["fields"]["third_party_output"]["value"] in ("client", "published")],
        "complete": [ref for ref, result in classified.items() if result["complete"]],
    }


def _by_role(classified):
    """§2.1's my_role, split into its own vocabulary and nothing else."""
    buckets = {"provider": [], "both": [], "deployer": [],
               "not_applicable": [], "unclassified": []}
    for ref, result in classified.items():
        value = result["fields"]["my_role"]["value"]
        if value == "provider":
            buckets["provider"].append(ref)
        elif value == "both":
            buckets["both"].append(ref)
        elif value == "deployer":
            buckets["deployer"].append(ref)
        elif value == "n/a":
            buckets["not_applicable"].append(ref)
        else:
            buckets["unclassified"].append(ref)
    return {name: sorted(refs) for name, refs in buckets.items()}


# --------------------------------------------------------------------------
# Presentation mode (§8.5)
# --------------------------------------------------------------------------

REDACTION = "[redacted -- presentation mode, §8.5]"


def opaque_label(ref, field):
    """A stable opaque label for one disclosive value.

    Stable so two mentions of the same project read as the same project, and
    opaque so neither says what it is. The ref is safe to use as the label
    because a ref carries no meaning -- that is the property that makes
    presentation mode possible at all, and it is asserted by a test.
    """
    if not ref:
        return REDACTION
    if field == "name":
        return f"Project {ref}"
    return f"[{ref} · {field}]"


def _redact_mapping(record, ref=None):
    ref = ref or record.get("ref")
    out = {}
    for key, value in record.items():
        if key in store.DISCLOSIVE_FIELDS and value not in (None, "", []):
            out[key] = opaque_label(ref, key)
        else:
            out[key] = value
    return out


def present(payload):
    """Redact a `store.export()` payload for presentation (§8.5).

    Enumerative over `store.DISCLOSIVE_FIELDS`. The four places a disclosive
    value can reach the export are handled explicitly rather than by a generic
    deep walk, because a generic walk cannot tell which ref a value belongs to
    and would have to fall back to a bare marker for all of them.
    """
    presented = dict(payload)
    presented["projects"] = {ref: _redact_mapping(fields, ref)
                             for ref, fields in payload.get("projects", {}).items()}
    presented["tasks"] = {ref: _redact_mapping(fields, ref)
                          for ref, fields in payload.get("tasks", {}).items()}
    presented["runs"] = [_redact_mapping(record) for record in payload.get("runs", [])]
    presented["superseded_runs"] = [_redact_mapping(record)
                                    for record in payload.get("superseded_runs", [])]
    presented["decisions"] = [_redact_decision(record)
                              for record in payload.get("decisions", [])]
    # A person's name never appears in shared data (prd-core.md §6 rule 5,
    # decided by the owner 13 September). Every field naming a person is `by` or
    # ends in `_by`, wherever it is nested, and all become one placeholder.
    for bucket in ("projects", "tasks", "runs", "superseded_runs", "decisions"):
        presented[bucket] = _placeholder_people(presented[bucket])

    # The allocation register, and it is the one that is easy to miss. A ref is
    # bound to a *natural key*, and a project's natural key is its slug while a
    # task's is its spec filename -- so refs.json holds "nanowiki" and
    # "TASK-002-nanowiki-split.md" whatever the project record says. Redacting
    # the record and shipping the register would be a presentation mode that
    # leaks through its own bookkeeping. The audit below caught exactly this on
    # the first run, which is the argument for the audit.
    #
    # `aliases` maps ref to ref and needs nothing: both halves carry no meaning.
    refs = payload.get("refs") or {}
    presented["refs"] = dict(refs)
    presented["refs"]["bindings"] = {
        ref: (dict(binding, natural_key=opaque_label(ref, "natural_key"))
              if binding.get("kind") in ("project", "task") else dict(binding))
        for ref, binding in sorted((refs.get("bindings") or {}).items())}
    # A rename's basis is free text a human wrote about the work, and the names
    # check found one naming a person on its first run, 13 September.
    for ref, binding in presented["refs"]["bindings"].items():
        if binding.get("rename_basis"):
            binding["rename_basis"] = opaque_label(ref, "rename_basis")

    presented["presentation_mode"] = True
    presented["redacted_fields"] = sorted(store.DISCLOSIVE_FIELDS)
    presented["basis"] = (
        "Presentation mode (§8.5): paths can be disclosive even when contents "
        "are not. Every value in a disclosive field is replaced with an opaque "
        "label keyed on the ref. Refs themselves carry no meaning and are not "
        "redacted -- P-0007 says nothing about what P-0007 is. "
        + str(payload.get("basis", "")))
    return presented


PERSON_PLACEHOLDER = "[Approver]"


# `superseded_in_part_by: P-0001-T14` and `consumed_by: an earlier session` share the
# suffix and name no one. A ref carries no meaning and must survive redaction.
NOT_A_PERSON = re.compile(r"P-\d{4}(?:-T\d+)?|session \d+")


def _is_person(key, item):
    return ((key == "by" or str(key).endswith("_by")) and isinstance(item, str)
            and item.strip() != "" and not NOT_A_PERSON.fullmatch(item.strip()))


def _placeholder_people(value):
    if isinstance(value, dict):
        return {key: (PERSON_PLACEHOLDER if _is_person(key, item)
                      else _placeholder_people(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_placeholder_people(item) for item in value]
    return value


def _people(value):
    """Every name held in a person field, however deeply nested."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_person(key, item):
                yield item
            else:
                yield from _people(item)
    elif isinstance(value, list):
        for item in value:
            yield from _people(item)


def _redact_decision(record):
    """A transition record. The override reason is free text written by a human
    under pressure, so it is dropped whole rather than scanned: the fact that a
    gate was overridden, and when, survives presentation, and what they said
    about it does not. Who did it becomes `[Approver]` in present(), with every
    other field naming a person.

    An ExportConsent record (row 8) is the same shape and loses the same two
    things, plus one the transitions do not have: `destination` is a filesystem
    path, and §8.5's own sentence is that paths can be disclosive even when
    contents are not. That the export happened, who asked, when, and how much
    it left in the clear all survive -- which is the whole point of the log.
    """
    out = dict(record)
    if out.get("override"):
        out["override"] = dict(out["override"])
        out["override"]["reason"] = REDACTION
    if out.get("kind") == store.EXPORT_CONSENT_KIND:
        if out.get("reason"):
            out["reason"] = REDACTION
        if out.get("destination"):
            out["destination"] = REDACTION
    return out


def audit_presentation(payload, presented):
    """Did any disclosive value survive? Serialise and look.

    This is the check worth having, because it does not trust the redactor's
    own list of what it touched -- it takes every disclosive value from the
    unredacted payload and asserts none of them appears anywhere in the
    presented one. A field the redactor forgot fails here.
    """
    secrets = set()
    for bucket in (payload.get("projects", {}), payload.get("tasks", {})):
        for fields in bucket.values():
            for field in store.DISCLOSIVE_FIELDS:
                value = fields.get(field)
                if isinstance(value, str) and value.strip() not in ("", "null"):
                    secrets.add(value)
    for record in list(payload.get("runs", [])) + list(payload.get("superseded_runs", [])):
        for field in store.DISCLOSIVE_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                secrets.add(value)
    # A Run's natural key is its `claude_session_id` -- a uuid, which names
    # nothing and is deliberately not redacted (see DISCLOSIVE_FIELDS). Only
    # project slugs and task spec filenames are secrets here.
    for binding in ((payload.get("refs") or {}).get("bindings") or {}).values():
        if not isinstance(binding, dict) or binding.get("kind") not in ("project", "task"):
            continue
        key = binding.get("natural_key")
        if isinstance(key, str) and key.strip():
            secrets.add(key)
    for binding in ((payload.get("refs") or {}).get("bindings") or {}).values():
        basis = binding.get("rename_basis") if isinstance(binding, dict) else None
        if isinstance(basis, str) and basis.strip():
            secrets.add(basis)
    # An ExportConsent's `destination` is a path and its `reason` is free text a
    # human wrote about the work. The log is inside the export it records, so a
    # presented export that shipped them would leak through its own audit trail
    # -- the same shape as the refs.json leak an earlier session found.
    for record in payload.get("decisions") or []:
        if not isinstance(record, dict) or record.get("kind") != store.EXPORT_CONSENT_KIND:
            continue
        for field in ("destination", "reason"):
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                secrets.add(value)
    # A person's name, from any field naming one (prd-core.md §6 rule 5). Matched
    # on word boundaries: a short first name is a substring of ordinary words.
    text = json.dumps(presented, sort_keys=True)
    people = {name for bucket in ("projects", "tasks", "runs", "superseded_runs", "decisions")
              for name in _people(payload.get(bucket, []))}
    leaked_people = {name for name in people
                     if re.search(r"(?<![0-9A-Za-z])" + re.escape(name) + r"(?![0-9A-Za-z])", text)}
    secrets |= people
    leaked = sorted(leaked_people | {secret for secret in secrets - people if secret in text})
    return {"checked": len(secrets), "leaked": leaked, "clean": not leaked}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Classification and presentation mode (§2.1, §8.5).")
    parser.add_argument("--root", default=str(store.ROOT))
    parser.add_argument("--queries", action="store_true")
    parser.add_argument("--show", metavar="REF")
    parser.add_argument("--present", action="store_true",
                        help="the export, redacted for presentation (§8.5)")
    parser.add_argument("--audit", action="store_true",
                        help="check the redacted export for surviving values")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--consent-by",
                        help="who is asking for the export. Required to WRITE one, "
                             "presented or not (§8.5).")
    parser.add_argument("--consent-reason",
                        help="what the export is for. Recorded in decisions/.")
    parser.add_argument("--out")
    return parser.parse_args(argv)


def main(argv=None, out=None):
    args = parse_args(argv)
    out = out or sys.stdout
    root = Path(args.root)
    register = store.RefRegister.load(root / "store" / "refs.json")
    loaded = store.load_store(root)

    def say(line=""):
        print(line, file=out)

    if args.present or args.audit:
        payload = store.export(loaded, register)
        presented = present(payload)
        audit = audit_presentation(payload, presented)
        if args.audit and not args.present:
            say(f"checked  {audit['checked']} disclosive value(s) from the "
                f"unredacted export")
            for value in audit["leaked"]:
                say(f"LEAKED   {value}")
            say("clean. No disclosive value survives presentation mode (§8.5)."
                if audit["clean"] else "NOT CLEAN.")
            return 0 if audit["clean"] else 1
        if not audit["clean"]:                 # never hand out a leaking export
            say("REFUSED  the redacted export still contains: "
                + ", ".join(audit["leaked"]))
            return 1
        # The consent step is the store's, imported rather than reimplemented:
        # one manifest, one record shape, one place to be wrong. Presentation
        # mode changes what the manifest SAYS -- redacted_fields is populated
        # and the audit result rides along -- and does not change whether the
        # write is gated. A redacted export is still an export leaving the
        # machine, and §8.5 puts the demand on the record either way.
        manifest = store.export_manifest(
            presented, presentation_mode=True, audit=audit,
            destination=args.out, unredacted=payload)
        for line in store.format_manifest(manifest):
            say(line)
        text = json.dumps(presented, indent=2, sort_keys=True)
        if not args.out:
            say(text)
            return 0
        try:
            record = store.record_export_consent(
                manifest, args.consent_by, args.consent_reason, root=root)
        except store.ConsentNotRecorded as refused:
            say(f"REFUSED  {refused}")
            say("Nothing was written.")
            return 1
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        say(f"-> {args.out}")
        say(f"consent recorded  {record['at']}  {record['by']}  "
            f"-> decisions/{record['at'][:7]}.jsonl")
        return 0

    if args.show:
        ref = register.resolve(args.show)
        if ref not in loaded["projects"]:
            say(f"{args.show}: not a project in the store.")
            return 1
        result = store.classify(loaded["projects"][ref]["fields"])
        if args.json:
            say(json.dumps(result, indent=2, sort_keys=True))
            return 0
        say(f"{ref}  stage {result['stage']}")
        for field, detail in result["fields"].items():
            mark = {"set": " ", "missing": "?", "invalid": "!"}[detail["state"]]
            owed = (f"owed from {detail['owed_from']}"
                    if detail["owed_now"] else f"not owed until {detail['owed_from']}"
                    if detail["owed_from"] else "no stage owes it")
            say(f" {mark} {field:<20} {str(detail['value']):<14} {owed}")
        say("complete." if result["complete"] else
            f"missing and owed: {', '.join(result['missing_and_owed']) or 'none'}")
        return 0

    queries = classification_queries(loaded)
    if args.json or args.queries and args.json:
        say(json.dumps(queries, indent=2, sort_keys=True))
        return 0
    if args.queries:
        for name, result in queries.items():
            say(f"{name}")
            if isinstance(result, dict):
                for key, value in result.items():
                    say(f"  {key}: {', '.join(value) if isinstance(value, list) else value}")
            else:
                say(f"  {', '.join(result) or '-'}")
            say()
        return 0

    classified = classify_store(loaded)
    say(f"classification  {len(classified)} project(s), "
        f"{len(queries['complete'])} complete, "
        f"{len(queries['never_classified'])} never classified")
    for ref, result in classified.items():
        block = "  ".join(
            f"{field}={result['fields'][field]['value']}"
            for field in store.CLASSIFICATION_FIELDS)
        say(f"{ref}  {result['stage']:<12} {block}")
    for ref, missing in queries["missing_at_a_stage_that_owes_it"].items():
        say(f"owed     {ref}: {', '.join(missing)} -- the stage already owes these (§3.1)")
    for ref, invalid in queries["invalid_values"].items():
        say(f"PROBLEM  {ref}: {', '.join(invalid)} outside §2.1's vocabulary")
    return 0


if __name__ == "__main__":
    sys.exit(main())
