#!/usr/bin/env python3
"""Portable, offline case register. All duplicate findings require human review.

Python 3.10+; standard library only. check and assess never modify files.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit


COLLECTIONS = ("cases", "sources", "searches", "claims", "images")
KINDS = {"artwork", "product", "artist_reference", "format_reference"}
STAGES = {"mentioned", "researched", "drafted", "delivered", "published"}
CLAIM_TYPES = {"fact", "artist_intent", "interpretation", "unverified"}
GENERIC_TITLES = {"untitled", "无题", "未命名", "sans titre", "senza titolo"}


class RegistryError(ValueError):
    """Input or operation failure; the original register is preserved."""


def empty_registry():
    return {"schema_version": 1, **{key: [] for key in COLLECTIONS}}


def normalize_text(value):
    """Normalize typography and spacing without guessing names or translations."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_url(value):
    """Remove only known tracking keys; retain path case, port and fragment.

    Meaningful query bytes, their ordering, duplicate keys and blanks are kept.
    Neither www nor default ports nor trailing slashes are removed.
    """
    parts = urlsplit(value)
    retained = []
    for segment in parts.query.split("&"):
        key = unquote_plus(segment.split("=", 1)[0]).casefold()
        if key.startswith("utm_") or key in {"gclid", "fbclid"}:
            continue
        retained.append(segment)
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(),
                       parts.path, "&".join(retained), parts.fragment))


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def is_web_url(value):
    if not nonempty(value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme.casefold() in {"http", "https"}
                and bool(parts.hostname) and parts.username is None
                and parts.password is None and (parts.port is None or parts.port > 0))
    except ValueError:
        return False


def _strings(value):
    return isinstance(value, list) and all(nonempty(item) for item in value)


def _case_errors(case, prefix):
    errors = []
    for key in ("id", "title"):
        if not nonempty(case.get(key)):
            errors.append(f"{prefix}.{key}: a nonempty string is required")
    if not isinstance(case.get("kind"), str) or case["kind"] not in KINDS:
        errors.append(f"{prefix}.kind: use one of {sorted(KINDS)}")
    if "artist" not in case or (case["artist"] is not None and not nonempty(case["artist"])):
        errors.append(f"{prefix}.artist: a nonempty string or null is required")
    if "year" not in case or (case["year"] is not None and type(case["year"]) is not int):
        errors.append(f"{prefix}.year: an integer or null is required")
    if not _strings(case.get("aliases")):
        errors.append(f"{prefix}.aliases: a list of nonempty strings is required")
    if "analyses" in case:
        analyses = case["analyses"]
        if not isinstance(analyses, dict):
            errors.append(f"{prefix}.analyses: an object keyed by analysis id is required")
        else:
            for analysis_id, analysis in analyses.items():
                if not nonempty(analysis_id) or not isinstance(analysis, dict):
                    errors.append(f"{prefix}.analyses: nonempty ids and object values are required")
                    continue
                for field in ("question", "takeaway"):
                    value = analysis.get(field)
                    if value is not None and not nonempty(value):
                        errors.append(f"{prefix}.analyses.{analysis_id}.{field}: a nonempty string or null is required")
    if not isinstance(case.get("stage"), str) or case["stage"] not in STAGES:
        errors.append(f"{prefix}.stage: use one of {sorted(STAGES)}")
    publication = case.get("publication")
    if not isinstance(publication, dict):
        errors.append(f"{prefix}.publication: an object is required")
    else:
        status = publication.get("status")
        if not isinstance(status, str) or status not in {"unknown", "published_confirmed"}:
            errors.append(f"{prefix}.publication.status: unknown or published_confirmed is required")
        evidence = publication.get("evidence")
        if not _strings(evidence):
            errors.append(f"{prefix}.publication.evidence: a list of nonempty strings is required")
        if status == "published_confirmed" and not evidence:
            errors.append(f"{prefix}.publication: a confirmed publication needs evidence")
        if case.get("stage") == "published" and status != "published_confirmed":
            errors.append(f"{prefix}: published stage needs published_confirmed evidence")
    return errors


def validate_registry(registry):
    """Return structural/evidence errors and warnings; never infer factual truth."""
    errors, warnings = [], []
    if not isinstance(registry, dict):
        return {"errors": ["registry must be a JSON object"], "warnings": []}
    if type(registry.get("schema_version")) is not int or registry.get("schema_version") != 1:
        errors.append("schema_version must be integer 1")
    arrays = {}
    for key in COLLECTIONS:
        value = registry.get(key)
        if not isinstance(value, list):
            errors.append(f"{key}: a list is required")
            arrays[key] = []
        else:
            arrays[key] = value
    indexes = {}
    for key in COLLECTIONS:
        index = {}
        for position, item in enumerate(arrays[key]):
            prefix = f"{key}[{position}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix}: an object is required")
                continue
            required_id = key in {"cases", "sources", "claims"}
            item_id = item.get("id")
            if required_id or "id" in item:
                if not nonempty(item_id):
                    errors.append(f"{prefix}.id: a nonempty string is required")
                elif item_id in index:
                    errors.append(f"{key}: duplicate id {item_id!r}")
                else:
                    index[item_id] = item
        indexes[key] = index

    for position, case in enumerate(arrays["cases"]):
        if isinstance(case, dict):
            errors.extend(_case_errors(case, f"cases[{position}]"))

    source_urls, families = {}, {}
    for position, source in enumerate(arrays["sources"]):
        if not isinstance(source, dict):
            continue
        prefix = f"sources[{position}]"
        if not is_web_url(source.get("url")):
            errors.append(f"{prefix}.url: an HTTP(S) URL without credentials is required")
        else:
            canonical = normalize_url(source["url"])
            if canonical in source_urls:
                warnings.append(f"{prefix}: same URL as source {source_urls[canonical]!r}; reuse its id")
            source_urls[canonical] = source.get("id")
        if not nonempty(source.get("source_type")):
            errors.append(f"{prefix}.source_type: a nonempty string is required")
        family = source.get("evidence_family_id")
        if family is not None:
            if not nonempty(family):
                errors.append(f"{prefix}.evidence_family_id: a nonempty string or null is required")
            else:
                families.setdefault(family, []).append(source.get("id"))
    for family, member_ids in families.items():
        if len(member_ids) > 1:
            warnings.append(f"sources {member_ids!r} share known evidence family {family!r}; do not count as independent confirmations")

    claim_index = indexes["claims"]
    for position, claim in enumerate(arrays["claims"]):
        if not isinstance(claim, dict):
            continue
        prefix = f"claims[{position}]"
        claim_type = claim.get("type")
        if not isinstance(claim_type, str) or claim_type not in CLAIM_TYPES:
            errors.append(f"{prefix}.type: use one of {sorted(CLAIM_TYPES)}")
        if not nonempty(claim.get("case_id")) or claim.get("case_id") not in indexes["cases"]:
            errors.append(f"{prefix}.case_id: must reference an existing case")
        if not nonempty(claim.get("text")):
            errors.append(f"{prefix}.text: a nonempty string is required")
        for key, target in (("sources", indexes["sources"]), ("premises", claim_index)):
            refs = claim.get(key)
            if not _strings(refs):
                errors.append(f"{prefix}.{key}: a list of nonempty reference ids is required")
            else:
                for ref in refs:
                    if ref not in target:
                        errors.append(f"{prefix}.{key}: unknown reference {ref!r}")
                if len(set(refs)) != len(refs):
                    errors.append(f"{prefix}.{key}: duplicate reference ids")
        if type(claim.get("publishable")) is not bool:
            errors.append(f"{prefix}.publishable: a boolean is required")
        if isinstance(claim_type, str) and claim_type in {"fact", "artist_intent"} and not claim.get("sources"):
            errors.append(f"{prefix}: {claim_type} needs at least one source")
        if claim_type == "interpretation" and not claim.get("premises"):
            errors.append(f"{prefix}: interpretation needs stated premise claim ids")
        if claim_type == "unverified" and claim.get("publishable") is True:
            errors.append(f"{prefix}: an unverified claim cannot be publishable")
        if "locator" in claim and claim["locator"] is not None and not nonempty(claim["locator"]):
            errors.append(f"{prefix}.locator: use a nonempty string or null")

    # Iterative graph walk handles long chains without recursion limits.
    graph = {claim_id: [ref for ref in item.get("premises", [])
                        if isinstance(ref, str) and ref in claim_index]
             for claim_id, item in claim_index.items()
             if isinstance(item.get("premises"), list)}
    state, cyclic = {}, set()
    for start in graph:
        if state.get(start) == 2:
            continue
        stack, path = [(start, False)], []
        while stack:
            node, leaving = stack.pop()
            if leaving:
                state[node] = 2
                if path and path[-1] == node:
                    path.pop()
                continue
            if state.get(node) == 1:
                cycle = path[path.index(node):] + [node] if node in path else [node]
                cyclic.update(cycle)
                errors.append("claim premises contain a cycle: " + " -> ".join(cycle))
                continue
            if state.get(node) == 2:
                continue
            state[node] = 1
            path.append(node)
            stack.append((node, True))
            stack.extend((ref, False) for ref in reversed(graph.get(node, [])))

    for claim_id, claim in claim_index.items():
        if claim.get("type") != "interpretation" or claim.get("publishable") is not True:
            continue
        seen, pending, grounded, unverified = set(), list(graph.get(claim_id, [])), False, False
        while pending:
            ref = pending.pop()
            if ref in seen:
                continue
            seen.add(ref)
            premise = claim_index[ref]
            if isinstance(premise.get("type"), str) and premise["type"] in {"fact", "artist_intent"} and premise.get("sources"):
                grounded = True
            if premise.get("type") == "unverified" or premise.get("publishable") is False:
                unverified = True
            pending.extend(graph.get(ref, []))
        if not grounded:
            errors.append(f"claim {claim_id!r}: publishable interpretation must reach a sourced fact or artist_intent premise")
        if unverified:
            errors.append(f"claim {claim_id!r}: publishable interpretation depends on an unpublished or unverified premise")

    for position, image in enumerate(arrays["images"]):
        if not isinstance(image, dict):
            continue
        prefix = f"images[{position}]"
        if not is_web_url(image.get("source_url")):
            errors.append(f"{prefix}.source_url: an HTTP(S) URL without credentials is required")
        case_id = image.get("case_id")
        if case_id is not None and (not nonempty(case_id) or case_id not in indexes["cases"]):
            errors.append(f"{prefix}.case_id: must reference an existing case or be null")
        sha = image.get("sha256")
        if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha)):
            errors.append(f"{prefix}.sha256: must be 64 hexadecimal characters or null")
        if image.get("role") is not None and not nonempty(image["role"]):
            errors.append(f"{prefix}.role: a nonempty string or null is required")
    return {"errors": errors, "warnings": warnings}


def _title_set(case):
    values = [case.get("title", ""), *case.get("aliases", [])]
    return {normalize_text(value) for value in values if isinstance(value, str) and value.strip()}


def _generic_title(titles):
    return any(title in GENERIC_TITLES or any(title.startswith(item + " (")
               for item in GENERIC_TITLES) for title in titles)


def _tokens(text):
    """Transparent keyword heuristic, not embeddings or a semantic verdict."""
    if not isinstance(text, str):
        return set()
    text = normalize_text(text)
    words = set(re.findall(r"[a-z0-9]+", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[pos:pos + 2] for pos in range(len(run) - 1))
        if len(run) == 1:
            words.add(run)
    return words


def _argument_records(case):
    """Keep legacy and per-article history visible without assuming a column."""
    records = []
    if "question" in case or "takeaway" in case or not case.get("analyses"):
        records.append((None, case))
    for analysis_id, analysis in case.get("analyses", {}).items():
        records.append((analysis_id, analysis))
    return records


def assess_candidate(registry, candidate, sources=None, images=None):
    """List risks without automatically accepting, merging or discarding a case."""
    risks = []
    candidate_arguments = _argument_records(candidate)
    history_records, incomplete_records = 0, 0
    candidate_titles = _title_set(candidate)
    artist = candidate.get("artist")
    generic = _generic_title(candidate_titles)
    if artist is None:
        risks.append({"category": "identity_uncertain", "case_ids": [],
                      "message": "Candidate artist is unknown; identify the maker before an identity decision."})
    if generic:
        risks.append({"category": "identity_uncertain", "case_ids": [],
                      "message": "Untitled or another generic title requires object-level human disambiguation; it is never automatically merged."})
    for old in registry["cases"]:
        overlap = candidate_titles & _title_set(old)
        if overlap:
            if artist is None or old.get("artist") is None:
                risks.append({"category": "identity_uncertain", "case_ids": [old["id"]],
                              "message": "A known title/alias overlaps, but at least one artist is unknown; this is not an identity match."})
            elif normalize_text(artist) == normalize_text(old["artist"]):
                if generic or _generic_title(_title_set(old)):
                    risks.append({"category": "identity_uncertain", "case_ids": [old["id"]],
                                  "message": "Generic titles can refer to different objects by the same artist; inspect catalogue or inventory evidence."})
                else:
                    same_year = (candidate.get("year") is not None
                                 and old.get("year") is not None
                                 and candidate["year"] == old["year"])
                    category = "identity_match" if same_year else "version_review"
                    message = ("Same known artist, title/alias and year: reuse the existing case id."
                               if same_year else "Same known artist and title/alias with a different or unknown year: review versions and use the existing case id before merging; do not silently discard a version.")
                    risks.append({"category": category, "case_ids": [old["id"]], "message": message})
        for old_analysis_id, old_argument in _argument_records(old):
            history_records += 1
            missing = [field for field in ("question", "takeaway") if not nonempty(old_argument.get(field))]
            if missing:
                incomplete_records += 1
                risks.append({"category": "argument_history_incomplete", "case_ids": [old["id"]],
                              "analysis_id": old_analysis_id, "missing_fields": missing,
                              "message": "Missing historical question/conclusion prevents full argument comparison; no-match does not mean novel."})
            for candidate_analysis_id, candidate_argument in candidate_arguments:
                for field in ("question", "takeaway"):
                    first, second = _tokens(candidate_argument.get(field)), _tokens(old_argument.get(field))
                    if not first or not second:
                        continue
                    score = len(first & second) / len(first | second)
                    if score >= 0.45:
                        risks.append({"category": "argument_overlap_warning", "case_ids": [old["id"]],
                                      "analysis_id": old_analysis_id, "candidate_analysis_id": candidate_analysis_id,
                                      "field": field, "keyword_jaccard": round(score, 3),
                                      "message": "Keyword overlap is only a warning. Compare the question, reasoning and conclusion; a low score is not evidence of novelty."})
    old_urls = {normalize_url(source["url"]): source["id"] for source in registry["sources"]}
    old_families = {}
    for source in registry["sources"]:
        family = source.get("evidence_family_id")
        if family:
            old_families.setdefault(family, []).append(source["id"])
    for source in sources or []:
        canonical = normalize_url(source["url"])
        if canonical in old_urls:
            risks.append({"category": "source_url_reuse", "source_ids": [old_urls[canonical]],
                          "message": "This source URL is already registered. Reuse its source id and record why it was consulted again."})
        family = source.get("evidence_family_id")
        if family and family in old_families:
            risks.append({"category": "evidence_family_overlap", "source_ids": old_families[family],
                          "message": "An explicitly identified evidence family is already present; these reports are not independent corroboration. Institution names alone are not grouped."})
    for image in images or []:
        for old in registry["images"]:
            sha, old_sha = image.get("sha256"), old.get("sha256")
            sha_match = bool(sha and old_sha and sha.casefold() == old_sha.casefold())
            url_match = normalize_url(image["source_url"]) == normalize_url(old["source_url"])
            if sha_match or url_match:
                risks.append({"category": "image_reuse", "case_ids": [old.get("case_id")],
                              "match": "sha256" if sha_match else "source_url",
                              "message": "This exact file or source URL was used before. Check whether reuse is deliberate. Different hashes do not prove perceptual novelty."})
    return {"risks": risks, "requires_human_review": True,
            "argument_coverage": {"historical_records_inspected": history_records,
                                  "historical_records_incomplete": incomplete_records,
                                  "covers_registered_history_only": True},
            "limits": ["Known aliases only; no guessed translations or abbreviated artist names.",
                       "Keyword comparisons do not establish semantic novelty.",
                       "No history outside this register is inspected; image edits and unregistered reposts need visual/source review."]}


def _deep_merge(old, new):
    result = copy.deepcopy(old)
    for key, value in new.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _item_key(collection, item):
    if nonempty(item.get("id")):
        return ("id", item["id"])
    if collection == "images":
        if nonempty(item.get("sha256")):
            return ("sha", item.get("case_id"), item["sha256"].casefold())
        if is_web_url(item.get("source_url")):
            return ("url", item.get("case_id"), normalize_url(item["source_url"]))
    return None


def merge_registry(registry, entry, operation):
    """Prepare and fully validate a merge, without writing any file."""
    if operation not in {"add", "update"}:
        raise RegistryError("operation must be add or update")
    if not isinstance(entry, dict):
        raise RegistryError("entry must be an object with collection arrays")
    if "schema_version" in entry and (type(entry["schema_version"]) is not int or entry["schema_version"] != 1):
        raise RegistryError("entry cannot change schema_version")
    result = copy.deepcopy(registry)
    for key, value in entry.items():
        if key not in COLLECTIONS and key != "schema_version":
            if operation == "add" and key in result and result[key] != value:
                raise RegistryError(f"add cannot overwrite top-level field {key!r}; use update")
            result[key] = (_deep_merge(result[key], value)
                           if isinstance(result.get(key), dict) and isinstance(value, dict)
                           else copy.deepcopy(value))
    for collection in COLLECTIONS:
        incoming = entry.get(collection, [])
        if not isinstance(incoming, list) or any(not isinstance(item, dict) for item in incoming):
            raise RegistryError(f"entry.{collection} must be a list of objects")
        seen = set()
        for item in incoming:
            item_key = _item_key(collection, item)
            if item_key is not None and item_key in seen:
                raise RegistryError(f"entry.{collection} contains a duplicate key {item_key!r}")
            if item_key is not None:
                seen.add(item_key)
            matches = [position for position, old in enumerate(result[collection])
                       if item_key is not None and _item_key(collection, old) == item_key]
            if matches:
                if operation == "add":
                    raise RegistryError(f"add cannot overwrite an existing {collection} id/key {item_key!r}; reference or update it")
                updated = _deep_merge(result[collection][matches[0]], item)
                if collection == "sources" and is_web_url(updated.get("url")):
                    other_urls = [old.get("id") for position, old in enumerate(result["sources"])
                                  if position != matches[0] and is_web_url(old.get("url"))
                                  and normalize_url(old["url"]) == normalize_url(updated["url"])]
                    if other_urls:
                        raise RegistryError(f"source URL already registered as {other_urls!r}; reuse the existing source id")
                result[collection][matches[0]] = updated
                continue
            if collection == "cases":
                case_errors = _case_errors(item, "entry.cases")
                if case_errors:
                    raise RegistryError("; ".join(case_errors))
                for risk in assess_candidate(result, item)["risks"]:
                    if risk["category"] in {"identity_match", "version_review"}:
                        raise RegistryError(f"{risk['category']}: first identify the existing case id {risk['case_ids']!r}; use update to record the reviewed version")
            if collection == "sources" and is_web_url(item.get("url")):
                same = [old.get("id") for old in result["sources"]
                        if is_web_url(old.get("url")) and normalize_url(old["url"]) == normalize_url(item["url"])]
                if same:
                    raise RegistryError(f"source URL already registered as {same!r}; reuse the existing source id")
            result[collection].append(copy.deepcopy(item))
    checked = validate_registry(result)
    if checked["errors"]:
        raise RegistryError("merged register is invalid: " + "; ".join(checked["errors"]))
    return result, checked["warnings"]


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read JSON {path}: {exc}") from exc


def _serialized(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


@contextmanager
def _write_lock(path):
    lock_path = path.with_name(path.name + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RegistryError(f"another writer or a stale lock exists: {lock_path}; do not remove a live writer's lock") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "created_utc": datetime.now(timezone.utc).isoformat()}))
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def init_file(path, seed=None):
    path = Path(path)
    registry = _load(seed) if seed is not None else empty_registry()
    checked = validate_registry(registry)
    if checked["errors"]:
        raise RegistryError("seed is invalid: " + "; ".join(checked["errors"]))
    payload = _serialized(registry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock(path):
        if path.exists():
            raise RegistryError(f"refusing to overwrite existing register: {path}")
        descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Same-filesystem hard link publishes a complete file and cannot overwrite.
            os.link(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return checked


def atomic_replace(path, registry, expected_sha256):
    """Caller holds the writer lock; detect external edits and preserve a backup."""
    path = Path(path)
    current = path.read_bytes()
    if hashlib.sha256(current).hexdigest() != expected_sha256:
        raise RegistryError("register changed after it was read; retry against the new contents")
    payload = _serialized(registry)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(path.name + f".backup-{stamp}-{uuid.uuid4().hex}.json")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Check again after preparation; atomic rename never writes partial JSON.
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
            raise RegistryError("register changed while preparing the update; original file was not replaced")
        with backup.open("xb") as stream:
            stream.write(current)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(backup)


def merge_file(path, entry_path, operation):
    path = Path(path)
    entry = _load(entry_path)
    with _write_lock(path):
        original_bytes = path.read_bytes()
        try:
            registry = json.loads(original_bytes.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryError(f"register is not readable JSON: {exc}") from exc
        checked = validate_registry(registry)
        if checked["errors"]:
            raise RegistryError("existing register is invalid: " + "; ".join(checked["errors"]))
        merged, warnings = merge_registry(registry, entry, operation)
        if merged == registry:
            return {"changed": False, "backup": None, "warnings": warnings}
        backup = atomic_replace(path, merged, hashlib.sha256(original_bytes).hexdigest())
        return {"changed": True, "backup": backup, "warnings": warnings}


def _assess_input(registry, value):
    if not isinstance(value, dict):
        raise RegistryError("candidate must be a full case object or {case: <full case>, sources: [], images: [], claims: []}")
    case = value.get("case", value)
    if not isinstance(case, dict):
        raise RegistryError("candidate.case must be an object")
    errors = _case_errors(case, "candidate")
    if errors:
        raise RegistryError("; ".join(errors))
    # Validate candidate claims/images against the existing context without writing it.
    context = copy.deepcopy(registry)
    context["cases"] = [old for old in context["cases"] if old["id"] != case["id"]] + [case]
    extras = {}
    for collection in ("sources", "images", "claims"):
        extra = value.get(collection, [])
        if not isinstance(extra, list) or any(not isinstance(item, dict) for item in extra):
            raise RegistryError(f"candidate.{collection} must be a list of objects")
        extras[collection] = extra
        for item in extra:
            key = _item_key(collection, item)
            matching = [old for old in context[collection] if key is not None and _item_key(collection, old) == key]
            if matching:
                if item != matching[0]:
                    raise RegistryError(f"candidate.{collection} redefines an existing id/key; reference it instead")
            else:
                context[collection].append(item)
    checked = validate_registry(context)
    if checked["errors"]:
        raise RegistryError("candidate context is invalid: " + "; ".join(checked["errors"]))
    result = assess_candidate(registry, case, extras["sources"], extras["images"])
    result["warnings"] = checked["warnings"]
    return result


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "check", "assess", "merge"):
        command = subparsers.add_parser(name)
        command.add_argument("--registry", required=True, help="path to the persistent JSON register")
        if name == "init":
            command.add_argument("--seed", help="validated initial JSON; output must not already exist")
        elif name == "assess":
            command.add_argument("--candidate", required=True,
                                 help="JSON full case (id,title,kind,artist,year,aliases,stage,publication), or {case: full case, sources: [], images: [], claims: []}; read-only")
        elif name == "merge":
            command.add_argument("--entry", required=True, help="JSON collection arrays; partial ID objects permitted for update")
            command.add_argument("--operation", required=True, choices=("add", "update"))
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = init_file(args.registry, args.seed)
        elif args.command == "merge":
            result = merge_file(args.registry, args.entry, args.operation)
        else:
            registry = _load(args.registry)
            result = validate_registry(registry)
            if not result["errors"] and args.command == "assess":
                result = _assess_input(registry, _load(args.candidate))
        success = not result.get("errors")
        print(json.dumps({"command": args.command, "ok": success, **result}, ensure_ascii=False, indent=2))
        return 0 if success else 2
    except (RegistryError, OSError, ValueError) as exc:
        print(json.dumps({"command": args.command, "ok": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
