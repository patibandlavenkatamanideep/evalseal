# Hand-written. Not promptfoo artifacts.

Every file in this directory was written by hand to make the importer refuse something.
**None of it came out of promptfoo**, none of it is evidence of anything, and it is kept
in a separate directory from the genuine exports so the two can never be mistaken for
each other.

| file | what the importer must refuse |
|---|---|
| `not-json.json` | not parseable at all |
| `json-array.json` | valid JSON, wrong shape — an array where an object belongs |
| `no-results-key.json` | an object with no `results`, e.g. a `promptfoo share` payload |
| `unknown-export-schema.json` | `results.version: 99`, a schema this adapter has not been read against |
| `no-rows.json` | a well-formed export with nothing in it |
| `contradictory-row.json` | `success: true` and `gradingResult.pass: false` on one row |
| `duplicate-repeat-index.json` | two rows of one case claiming `repeatIndex: 0` |
| `assertion-failed-but-success.json` | `failureReason: 1` with `success: true` |

The last three are the interesting ones. Each is a shape promptfoo should never produce,
and each would otherwise turn into a receipt that looks fine: a contradiction silently
resolved in favour of one field, two rows collapsing into one repeat, or a failed
assertion counted as a pass. Refusing is the only honest answer, and the message names
the field.
