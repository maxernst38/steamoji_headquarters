"""Ratings and measures derived from match results.

Nothing here fetches or stores; it reads the catalog and returns numbers. That
keeps the arithmetic testable on its own and means a rating can never quietly
become the thing that persists instead of the match it came from.
"""
