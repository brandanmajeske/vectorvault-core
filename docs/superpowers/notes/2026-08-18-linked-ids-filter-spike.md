# Spike: does S3 Vectors filter on list-membership for `linked_ids`?

## Question

`linked_ids` will be a new **filterable** metadata key holding a LIST of strings on
each S3 Vectors vector (the forward edge: "this memory supports/links these
canonical_ids"). The reverse query is: "find all memories whose `linked_ids` list
contains a given canonical_id X". Does S3 Vectors support this as a native metadata
filter, or does it need a DynamoDB fallback (a reverse edge index, analogous to
`canonical_index.py`)?

## Current filter usage in this repo

`src/vectorvault/memory_client.py`:

- `_query` (line 1203) passes `metadata_filter` straight through to
  `s3vectors.query_vectors(..., filter=metadata_filter)` — no local interpretation or
  rewriting of filter shape. Whatever S3 Vectors' `QueryVectors` API accepts is passed
  as-is.
- `retrieve_memory` (lines 425-428) builds:
  `{"$and": [{"status": "active"}, {"expires_at": {"$gt": now}}, ...user filters as {k: v}]}`.
  User-supplied filters are added as plain `{key: value}` equality conditions (implicit
  `$eq`), not explicit `{"$eq": ...}` objects. Other call sites (line 259, line 940-971)
  follow the same pattern: `$and` of plain-equality and comparison-operator conditions.

So the existing code already relies on S3 Vectors interpreting a bare
`{"key": value}` condition as `$eq`.

## AWS docs: S3 Vectors metadata filtering contract

Source: [Metadata filtering — Amazon S3 User Guide](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-metadata-filtering.html)

> S3 Vectors supports string, number, boolean, and list types of metadata with a size
> limit per vector.

Supported operators on filterable metadata:

| Operator | Valid Input Types | Description |
| --- | --- | --- |
| `$eq` | String, Number, Boolean | Exact match comparison for single values. **When comparing with an array metadata value, returns true if the input value matches any element in the array.** Example: `{"category": {"$eq": "documentary"}}` matches a vector with metadata `"category": ["documentary", "romance"]`. |
| `$ne` | String, Number, Boolean | Not equal comparison |
| `$gt` / `$gte` / `$lt` / `$lte` | Number | Numeric comparisons |
| `$in` | Non-empty array of primitives | Match any value in array |
| `$nin` | Non-empty array of primitives | Match none of the values in array |
| `$exists` | Boolean | Check if field exists |
| `$and` / `$or` | Non-empty array of filters | Logical combinators |

The `$eq` row directly answers the key question: when the **stored** metadata value
is a list and the **query** value is a scalar, S3 Vectors' `$eq` matches if the scalar
is an element of the stored list. This is exactly the `category: ["documentary",
"romance"]` example in the docs — same shape as `linked_ids: [X, Y, Z]` with a query
of `{"linked_ids": {"$eq": "X"}}` (or the shorthand bare-equality form
`{"linked_ids": "X"}`, consistent with how this codebase already writes conditions).

This is a genuine list-membership match, not merely "given value is a list, matches a
scalar field" — the docs are explicit about the array-valued-metadata direction.

## Decision

**`REVERSE_MECHANISM = "native"`**

The reverse query "find all memories whose `linked_ids` list contains X" can be
expressed as a native S3 Vectors metadata filter:

```python
{"linked_ids": {"$eq": "X"}}
```

or combined with existing conditions the same way `retrieve_memory` already does:

```python
{"$and": [{"status": "active"}, {"linked_ids": {"$eq": "X"}}, ...]}
```

No DynamoDB fallback / reverse edge index is required for the base case. Tasks 5-6
should implement the reverse lookup via `_query`'s existing `metadata_filter` /
`filter` plumbing, using an `$eq` (or explicit-scalar shorthand) condition against
`linked_ids`, consistent with the `$and`-of-conditions pattern already used in
`retrieve_memory` and `list_memories`.

### Caveats / follow-up considerations (not blocking the "native" decision)

- **`list_memories` uses DynamoDB `memory-index`, not `_query`** for exact
  `canonical_id` / `task_id`-GSI listings (see design-doc: DynamoDB is a best-effort
  index for listing patterns `QueryVectors`/`ListVectors` can't do). If the reverse
  `linked_ids` lookup needs to be *listing*-style (i.e., "list all", not "similarity
  search filtered by"), it still goes through `_query` (QueryVectors requires a query
  vector) rather than a S3 Vectors `ListVectors`-with-filter call — S3 Vectors'
  `ListVectors` API does not appear to accept a `filter` parameter in the same way (not
  verified in this spike; worth confirming in Task 5/6 if a listing-only reverse lookup
  is needed with no embedding available).
- Filterable metadata has a **2 KB per-vector size cap** (per the Bedrock KB blog
  post found during this spike) — `linked_ids` lists should stay bounded; not a
  concern for typical edge counts but worth a sanity check if a memory could
  accumulate many links.
- This spike used AWS documentation only (no live probe against `provider-dev`) per
  the task brief ("docs are sufficient — do NOT create or mutate any AWS resources").
  The `$eq`-on-array behavior is stated plainly and unambiguously in the official
  User Guide table, so no empirical verification was performed.
