# Mutate a valid input and classify how the validator fails

Validators of uploaded or structured input (archives, metadata documents, manifests, config files,
API payloads) are meant to turn every bad input into a clean rejection: a 4xx-style response, a
`ValidationError`, a non-zero exit with a message. The defect is an input that escapes as a server
error or an unhandled exception instead, or one that is silently accepted. Finding one by reading
is slow. Mutating a known-good input many small ways and classifying each outcome is fast and
repeatable.

---

## 1. Pick the entry point and a valid fixture

- The **entry point** is the function or endpoint that receives the raw input: the upload view,
  the `parse_*`/`validate_*` function, the CLI command. Prefer the outermost one the project's own
  tests call, so the error mapping (exception to status code or message) is part of what is tested.
- The **fixture** is one input the project accepts: take it from the test suite's fixtures, not
  from a hand-written guess, so the baseline passes for the right reasons.

Run the baseline first. If the valid fixture is not accepted, fix the harness before mutating.

## 2. The mutations

Apply one mutation at a time to the valid fixture. The generic set:

| Family | Mutations |
|---|---|
| structure | drop each required field or member in turn; duplicate a field; reorder; nest one level deeper |
| types | replace each scalar with the wrong type (`""`, `0`, `[]`, `{}`, `null`, `true`) |
| sizes | empty value; a value 10x and 1000x the usual length; a huge count field |
| encodings | invalid UTF-8; a BOM; mixed line endings; NUL bytes; a non-NFC name |
| numbers | negative, zero, `NaN`, `1e309`, a number where a string is expected |
| paths and names | `../` components, absolute paths, a name that differs only in case or separators |
| archives | truncate at several offsets; corrupt one byte in the header and in the body; a member listed twice; a member whose declared size disagrees with its data; a link member |
| metadata | an unknown field; a field the spec deprecates; a version string the parser does not know |

For archives, mutate the bytes **and** rebuild valid archives that contain mutated members, so both
the container parser and the content validator are reached.

## 3. The harness

Fill in the marked parts and run it in an export (see `<plugin_root>/docs/reproduction-convention.md`),
with `TMPDIR` set to your private directory.

```python
"""Mutate a valid input; classify each outcome as accepted, rejected or crashed."""
import collections
import copy
import json
import sys

OUT = sys.argv[1]  # e.g. "$d/mutations.jsonl"


def load_valid():
    """FILL IN: return the valid fixture as a Python structure (dict for JSON/metadata) or bytes."""
    raise NotImplementedError


def submit(payload):
    """FILL IN: call the entry point; return (kind, detail).

    kind is "accepted", "rejected" (the project's documented failure: a 4xx status, its
    ValidationError, a clean exit code) or "crashed" (a 5xx, any other exception, a hang).
    """
    raise NotImplementedError


def mutations(valid):
    if isinstance(valid, dict):
        for key in list(valid):
            m = copy.deepcopy(valid); del m[key]; yield f"drop {key}", m
            for bad in ("", 0, [], {}, None, True, "x" * 100_000, float("nan")):
                m = copy.deepcopy(valid); m[key] = bad; yield f"{key}={bad!r:.20}", m
        m = copy.deepcopy(valid); m["unknown_field"] = "x"; yield "unknown field", m
    elif isinstance(valid, (bytes, bytearray)):
        for cut in (0, 1, len(valid) // 2, len(valid) - 1):
            yield f"truncate at {cut}", bytes(valid[:cut])
        for at in (0, 8, len(valid) // 2, len(valid) - 2):
            m = bytearray(valid); m[at] ^= 0xFF; yield f"flip byte {at}", bytes(m)
    # FILL IN: format-specific families from the table above.


def main():
    valid = load_valid()
    kind, detail = submit(valid)
    assert kind == "accepted", f"baseline not accepted: {detail}"
    tally = collections.Counter()
    with open(OUT, "w") as fh:
        for label, payload in mutations(valid):
            try:
                kind, detail = submit(payload)
            except Exception as exc:  # anything escaping submit() is a crash
                kind, detail = "crashed", f"{type(exc).__name__}: {exc}"[:200]
            tally[kind] += 1
            fh.write(json.dumps({"mutation": label, "kind": kind, "detail": str(detail)[:200]}) + "\n")
            if kind != "rejected":
                print(f"{kind:9} {label}: {str(detail)[:120]}")
    print(dict(tally))


if __name__ == "__main__":
    main()
```

## 4. Classify

- **crashed**: a finding. The input is bad, the user's mistake, and the server answers with a 5xx
  or an unhandled exception. Report the mutation, the traceback's last project frame and the
  status the documented contract promises.
- **accepted** when it should not be: a validation gap. Confirm against the spec or the docs that
  the mutated input is invalid before reporting it; some mutations (an unknown field) are
  legitimately tolerated.
- **rejected** with a message that leaks internals (a path, a stack, a SQL fragment): a finding of
  its own, usually lower severity.
- The same mutation **rejected on one entry point and crashed on another** (an upload view and its
  API twin): report both; the divergence points at the missing error mapping.

Keep `mutations.jsonl` with the report as the preserved harness output, and give each reported
mutation as a one-line reproducer.
