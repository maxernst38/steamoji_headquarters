"""Outside data sources.

Kept apart from `storage/` on purpose: these modules reach the network and can
fail, rate-limit or return something unexpected, where a store only ever touches
the local disk. Everything here converts what it fetches into the plain records
`storage.catalog` already understands, so nothing downstream knows or cares
whether a match was typed in or imported.
"""
