Look for: logic errors and wrong conditions; races (a write that depends on a read needs a lock or an atomic
statement); lost updates; missing transaction boundaries; error paths that leave state half-written; resources not
closed on every path; unbounded inputs; off-by-one limits; time compared as strings; behaviour that contradicts the
target repository's rules files or the task.
