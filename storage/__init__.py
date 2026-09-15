"""Everything the project persists between runs.

Three stores, all keyed by (video, match) rather than by video: the broadcast
camera can change between matches, so anything scoped to a whole file is wrong for
every match after the first.
"""
