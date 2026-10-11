# Strict serial merge queue

Graded Tasks merge one at a time; after each merge the full Regression suite reruns before the next Task enters. A failing Task is Ejected and re-enqueued at the back so it never blocks the Tasks behind it. We rejected speculative prefix batching (Zuul-style) and index-driven test selection because at demo scale (at most 5 Tasks, suite runtime in seconds) they cut latency nobody notices while adding cancellation, bisection, and missed-dynamic-coupling failure modes.
